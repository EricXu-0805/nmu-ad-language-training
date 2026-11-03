#!/usr/bin/env python3
"""把一条运维告警发到 Discord webhook。失败才调用,平时保持安静。

存在的理由:备份链 2026-07-23 起连续失败十四夜、异地拉取三周没跑过一次,
审计文件都如实写了,但没有任何通道把"FAIL"送到人眼前。这个脚本就是那条
通道:systemd OnFailure= / launchd 包装脚本在任务失败时调它,把失败事实
推到 Eric 盯着的 Discord 频道。

失败原因放在消息最前面,按单元去它真正写结论的地方取(REASON_SOURCES)。
2026-09-28 os-security 那次告警只带了 journal 尾部,而检查结论整份重定向进了
state 文件,六个待装的包一个都没出现在消息里。日志也只取失败的这一轮。

约束:
  - 只用标准库(certifi 有则用,没有不挂)——它必须能在"venv 坏了"的那种
    夜里工作,不能依赖任何可能坏掉的东西。
  - webhook URL 只从环境变量/--env-file 读,校验形状,绝不打印。
  - 消息只含单元名、主机标签、时间、失败原因、下一步和日志;调用方保证日志里没有秘密。
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.request

WEBHOOK_PREFIX = "https://discord.com/api/webhooks/"
MAX_CONTENT = 1800  # Discord 上限 2000,留余量给标头
REASON_CHARS = 600
RETRY_DELAY_SECONDS = 30
DEFAULT_BACKUP_ROOT = Path("/opt/nmu/backups")
# 单元 -> 失败原因写在哪,路径与 deploy/systemd/*.service 一致。
REASON_SOURCES = {
    "nmu-os-security": ("state", "os-security.state"),
    "nmu-restore-drill": ("state", "restore-drill.state"),
    "nmu-capacity": ("state", "capacity.state"),
    "nmu-backup-health": ("state", "health.state"),
    "nmu-backup": ("last_line", "backup.log"),
    "nmu-health": ("journal", None),
}
# 单元 -> 读告警的人下一步做什么,放在原因之后、日志之前。2026-11-02 os-security 那条
# 只有原因,原因里写着 full-upgrade,而内核类按规矩只在维护窗口里装。
NEXT_STEPS = {
    "nmu-os-security": (
        "内核元包(linux-generic 等四个)走维护窗口:在 Mac 上先改 "
        "deploy/maintenance/kernel_netplan_window.sh 的 ABI 常量、跑 check;"
        "netplan/grub/shim 等网络/引导类窗口脚本装不了,挑维护窗口手装 apt-get install 点名装,"
        "装完重启再跑窗口脚本的 verify-kernel;其余安全包先 apt-get -s install 看会不会带进"
        "内核/网络/引导类,不带再 apt-get install 点名装;绝不 apt-get full-upgrade;"
        "原因点名 nmu/nginx 跑在旧库上的,挑没人练的时候 systemctl restart nmu(或 nginx),"
        "再重跑 os_security_check.py"),
    "nmu-restore-drill": (
        "看 /opt/nmu/backups/restore-drill.state 全文,找负责人;别在 nmu 运行时手动补跑演练"
        "(交接文档第 6 节第 2 条:内存只有 1 GiB)"),
    "nmu-capacity": (
        "python3 /opt/nmu/app/scripts/capacity_reclaim.py 只读列出可清理项,负责人点名后才 "
        "--delete;别手工删 /opt/nmu/backups/daily/(交接文档 5.1 第 2 条)"),
    "nmu-backup-health": (
        "按交接文档 5.1;probe_stale/probe_never_ran_since_boot 看 nmu-health.timer 的 NEXT 列,"
        "probe_last_run_failed 按 5.2,offbox_probe_silent 是机外探针 30 分钟没来,查 OpenClaw 那台"),
    "nmu-backup": "按交接文档 5.1:tail -20 /opt/nmu/backups/backup.log 看失败码;别手工造快照",
    "nmu-health": (
        "按交接文档 5.2「网站打不开」;/health 200 而 /health/ready 503 是库或盘:df -h /、"
        "findmnt -no OPTIONS /、cat /opt/nmu/backups/capacity.state,再按 5.1 第 2 条;"
        "机外探针 nmu_offbox_probe.py --status 看纽约那头是不是也判 DOWN(它只打 /health)"),
}
_INVOCATION_ID = re.compile(r"[0-9a-f]{32}")
_WEBHOOK_URL = re.compile(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\S+")


def _load_env_file(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _capture(argv: list[str], run) -> str | None:
    try:
        result = run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _invocation(unit: str, run) -> tuple[str | None, float | None]:
    """(失败那一轮的 InvocationID, 那一轮开始的墙钟时刻);查不到的给 None。"""
    shown = _capture(["systemctl", "show", unit, "-p", "InvocationID",
                      "-p", "InactiveExitTimestampMonotonic"], run) or ""
    properties = {}
    for line in shown.splitlines():
        key, _, value = line.partition("=")
        properties[key.strip()] = value.strip()
    invocation = properties.get("InvocationID", "")
    started_usec = properties.get("InactiveExitTimestampMonotonic", "")
    started_at = None
    if started_usec.isdigit() and int(started_usec) > 0:
        # systemd 记的是开机以来的单调时钟;折成墙钟才能和 state 文件的 mtime 比。
        started_at = time.time() - (time.monotonic() - int(started_usec) / 1_000_000)
    return (invocation if _INVOCATION_ID.fullmatch(invocation) else None), started_at


def _state_summary(text: str) -> str:
    """os-security 的 state 是 JSON:把 failures 提到最前,待装的包名就在里面。

    notes 跟在后面:还跑着旧库、等重启窗口的其余单元只记在 notes 里,只发 failures
    它们就永远到不了人眼前。
    """
    try:
        facts = json.loads(text)
    except ValueError:
        return text
    if isinstance(facts, dict) and isinstance(facts.get("failures"), list) and facts["failures"]:
        summary = "；".join(str(item) for item in facts["failures"])
        notes = facts.get("notes")
        if isinstance(notes, list) and notes:
            summary += "\n另注:" + "；".join(str(item) for item in notes)
        return summary
    return text


def _reason(unit: str, backup_root: Path, invocation: str | None,
            started_at: float | None, lines: int, run) -> tuple[str, str] | None:
    """(来源, 原因正文);这个单元没登记原因来源、或拿不到本轮时给 None。"""
    kind, filename = REASON_SOURCES.get(unit.removesuffix(".service"), (None, None))
    if kind is None:
        return None
    if kind == "journal":
        if invocation is None:
            return None
        output = _capture(["journalctl", f"_SYSTEMD_INVOCATION_ID={invocation}",
                           "-n", str(lines), "-o", "cat", "--no-pager"], run)
        return "本轮输出", (output or "").strip() or "(本轮没有输出)"
    path = backup_root / filename
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        written = path.stat().st_mtime
    except OSError as error:
        return path.name, f"(读不到:{error.strerror or error})"
    if kind == "last_line":
        nonblank = [line for line in text.splitlines() if line.strip()]
        text = nonblank[-1] if nonblank else ""
        label = f"{path.name} 最后一行"
    else:
        text = _state_summary(text)
        label = path.name
    label += f",写于 {datetime.fromtimestamp(written):%F %T}"
    # 脚本在写结论之前就挂了时,盘上还是上一轮的结论,不能当成这次的原因端出去。
    if started_at is not None and written < started_at - 1:
        label += ",本轮没有改写它,下面是上一轮的"
    return label, text.strip() or "(空)"


def _journal(unit: str, invocation: str | None, lines: int, run) -> tuple[str, str]:
    """(标题, 正文);拿得到 InvocationID 就只取失败的这一轮。"""
    if invocation is not None:
        # _SYSTEMD_INVOCATION_ID 是单元进程自己的输出;systemd 关于这一轮的
        # 「Main process exited, status=…」只带 INVOCATION_ID,两样都要。
        output = _capture(["journalctl", f"_SYSTEMD_INVOCATION_ID={invocation}", "+",
                           f"INVOCATION_ID={invocation}", "-n", str(lines),
                           "-o", "cat", "--no-pager"], run)
        if output is not None:
            return "本轮日志:", output.strip() or "(本轮没有日志)"
    output = _capture(["journalctl", "-u", unit, "-n", str(lines),
                       "-o", "cat", "--no-pager"], run)
    title = "最近日志(没拿到本轮 InvocationID,可能混有上一轮):"
    if output is None:
        return title, "(journalctl 读不到)"
    return title, output.strip() or "(该单元没有日志)"


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _send(webhook: str, content: str) -> tuple[bool, str]:
    # Cloudflare 会 403 掉默认 Python-urllib UA 的 POST(GET 反而放行),自定义 UA 即可。
    request = urllib.request.Request(
        webhook, data=json.dumps({"content": content}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "User-Agent": "nmu-ops-alert/1"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10, context=_ssl_context()) as resp:
            status = resp.status
    except OSError as error:
        return False, f"alert_send_failed err={getattr(error, 'reason', error.__class__.__name__)}"
    if status // 100 != 2:
        return False, f"alert_send_http_{status}"
    return True, f"alert_sent status={status}"


def main(argv: list[str] | None = None, *, run=subprocess.run, send=_send,
         sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", help="失败的 systemd 单元名,取其失败原因与本轮 journal")
    parser.add_argument("--message", help="显式消息正文(与 --unit 二选一或并用)")
    parser.add_argument("--env-file", type=Path,
                        help="从这个 KEY=VALUE 文件补齐环境(launchd 场景)")
    parser.add_argument("--journal-lines", type=int, default=12)
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT,
                        help="state 文件与 backup.log 所在目录")
    args = parser.parse_args(argv)

    if not args.unit and not args.message:
        print("--unit 与 --message 至少给一个", file=sys.stderr)
        return 64

    if args.env_file is not None:
        try:
            _load_env_file(args.env_file)
        except OSError as error:
            print(f"env_file_unreadable err={error.errno}", file=sys.stderr)
            return 66

    webhook = os.environ.get("NMU_OPS_WEBHOOK", "")
    if not webhook.startswith(WEBHOOK_PREFIX):
        print("webhook_missing_or_bad_shape", file=sys.stderr)
        return 78

    # 生产机的 hostname 是 green-fiber-1.localdomain,读告警的人认不出是哪台。
    host = os.environ.get("NMU_OPS_HOST_LABEL", "").strip() or socket.gethostname()
    stamp = datetime.now().strftime("%F %T")
    parts = [f"🔴 NMU 运维告警 · {host} · {stamp}"]
    invocation = started_at = None
    if args.unit:
        parts.append(f"单元:{args.unit} 进入 failed")
        invocation, started_at = _invocation(args.unit, run)
        reason = _reason(args.unit, args.backup_root, invocation, started_at,
                         args.journal_lines, run)
        if reason is not None:
            source, text = reason
            parts.append(f"原因({source}):")
            parts.append(text[:REASON_CHARS])
        step = NEXT_STEPS.get(args.unit.removesuffix(".service"))
        if step is not None:
            parts.append(f"下一步:{step}")
    if args.message:
        parts.append(args.message)
    if args.unit:
        parts.extend(_journal(args.unit, invocation, args.journal_lines, run))
    # state 文件和 journal 的内容不归这里管;万一有人把 webhook 打进了日志,也不原样转发。
    content = _WEBHOOK_URL.sub("<webhook>", "\n".join(parts))
    if len(content) > MAX_CONTENT:
        content = content[:MAX_CONTENT] + "\n…(截断)"

    for attempt in range(2):
        if attempt:
            # 一次失败多半是限流或网络抖动;隔 30 秒再试一次,两次都不成就让告警单元失败。
            sleep(RETRY_DELAY_SECONDS)
        delivered, outcome = send(webhook, content)
        print(outcome, file=sys.stdout if delivered else sys.stderr)
        if delivered:
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
