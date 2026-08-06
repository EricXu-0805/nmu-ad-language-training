#!/usr/bin/env python3
"""把一条运维告警发到 Discord webhook。失败才调用,平时保持安静。

存在的理由:备份链 2026-07-23 起连续失败十四夜、异地拉取三周没跑过一次,
审计文件都如实写了,但没有任何通道把"FAIL"送到人眼前。这个脚本就是那条
通道:systemd OnFailure= / launchd 包装脚本在任务失败时调它,把失败事实
推到 Eric 盯着的 Discord 频道。

约束:
  - 只用标准库(certifi 有则用,没有不挂)——它必须能在"venv 坏了"的那种
    夜里工作,不能依赖任何可能坏掉的东西。
  - webhook URL 只从环境变量/--env-file 读,校验形状,绝不打印。
  - 消息只含单元名、主机名、时间和日志尾部;调用方保证日志里没有秘密。
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import urllib.request

WEBHOOK_PREFIX = "https://discord.com/api/webhooks/"
MAX_CONTENT = 1800  # Discord 上限 2000,留余量给标头


def _load_env_file(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _journal_tail(unit: str, lines: int) -> str:
    try:
        result = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "-o", "cat", "--no-pager"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return "(journalctl 不可用)"
    if result.returncode != 0:
        return "(journalctl 读取失败)"
    return result.stdout.strip() or "(该单元没有日志)"


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", help="失败的 systemd 单元名,取其 journal 尾部")
    parser.add_argument("--message", help="显式消息正文(与 --unit 二选一或并用)")
    parser.add_argument("--env-file", type=Path,
                        help="从这个 KEY=VALUE 文件补齐环境(launchd 场景)")
    parser.add_argument("--journal-lines", type=int, default=12)
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

    host = socket.gethostname()
    stamp = datetime.now().strftime("%F %T")
    parts = [f"🔴 NMU 运维告警 · {host} · {stamp}"]
    if args.unit:
        parts.append(f"单元:{args.unit} 进入 failed")
    if args.message:
        parts.append(args.message)
    if args.unit:
        parts.append("最近日志:")
        parts.append(_journal_tail(args.unit, args.journal_lines))
    content = "\n".join(parts)
    if len(content) > MAX_CONTENT:
        content = content[:MAX_CONTENT] + "\n…(截断)"

    # Cloudflare 会 403 掉默认 Python-urllib UA 的 POST(GET 反而放行),自定义 UA 即可。
    request = urllib.request.Request(
        webhook, data=json.dumps({"content": content}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "User-Agent": "nmu-ops-alert/1"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10, context=_ssl_context()) as resp:
            status = resp.status
    except OSError as error:
        print(f"alert_send_failed err={getattr(error, 'reason', error.__class__.__name__)}",
              file=sys.stderr)
        return 1
    if status // 100 != 2:
        print(f"alert_send_http_{status}", file=sys.stderr)
        return 1
    print(f"alert_sent status={status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
