#!/usr/bin/env python3
"""NMU 异地探针:从 OpenClaw VPS(纽约)每 10 分钟打一次 NMU 公网 /health,状态变了才发 Discord。

为什么要有它:本机那条 nmu-health 从同一台机器打回自己的公网地址
(deploy/systemd/nmu-health.service:11-12),机器宕了、机房出口断了,它和它的告警一起
没了。这一份跑在另一台机器上:OpenClaw VPS(openclaw-prod,DigitalOcean NYC1)的
user crontab 每小时 :04/:14/…/:54 调起它(crontab.line,外面套 flock)。

一轮怎么判:
  - 最多试 ATTEMPTS 次,每次 curl 最多 15 秒,中间隔 GAP_S 秒;HTTP 200 且正文是
    /health 的应答才算通。SPA 外壳也回 200,所以正文必须核。
  - 都不通,先探 Discord 的 API:连它都没有应答 = 纽约这头的问题,只记账、不算 NMU 的。
  - 名字解析失败(curl 6)再钉住 IP 探一次:钉 IP 就通 = sslip.io 的 DNS 坏了,照样算挂
    (用户就是靠这个域名进来的);钉 IP 也不通,报站点自己的原因。

状态规则(tests/test_offbox_probe.py 逐条钉住):
  1. 失败一轮 fail_streak 加一;UP 时连续第 DOWN_AFTER_RUNS 轮失败 → DOWN,
     down_since = 这一串的第一次失败,欠一条 DOWN。
  2. 纽约这头的失败只记账:不加、也不清 fail_streak。
  3. DOWN 之后第一次通 → UP,欠一条带中断时长的 RECOVERED。
  4. 发送失败的消息继续欠着,下一轮再发;发出去的不会再发第二次。
  5. DOWN 一直没发出去就恢复了 → 合成一条:中断多久、已恢复、当时的告警没发出去。
  6. 在静默窗口里开始又在窗口里结束的故障一条都不发;静默过了还挂着就补报 DOWN。
  7. silence-until 只在 now < 它 ≤ now + 24 小时时生效;内容不是整数秒就当没有静默。
  8. state.json 记 last_run、status、down_since、last_reason、owed、ring 与
     summary_24h {runs, fails, probe_side, p95_ms};写临时文件、fsync、os.replace,
     权限 0600;缺了或坏了就当全新的 UP。

DOWN @Eric,RECOVERED 不 @;中间不重复提醒,夜里的 Heartbeat 把 DOWN 列成 ERROR。
探针不调 OpenClaw 的命令行,只在有消息要发时才经 keychain-get 取 webhook,绝不打印它。
只用标准库(Python 3.12):OpenClaw 网关挂掉的夜里它也得照常跑。

用法:
  nmu_offbox_probe.py                     cron 那一轮;stdout 什么都不写,一行进 probe.log
  nmu_offbox_probe.py --dry-run [--state P] [--url U]
                                          照常探、照常判,不发消息、不写正式状态
  nmu_offbox_probe.py --status            只读 state.json,不联网
  nmu_offbox_probe.py --check-webhook     GET webhook,只打印 channel_id/guild_id/name
  nmu_offbox_probe.py --test-alert        真发一条标明是测试的消息,不重试
"""
from __future__ import annotations

import argparse
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple
import urllib.error
import urllib.request

URL = "https://89-208-253-119.sslip.io/health"
PIN = "89-208-253-119.sslip.io:443:89.208.253.119"
EXPECT = {"status": "ok", "service": "language-training-platform"}
CONTROL = "https://discord.com/api/v10/gateway"
CURL = "/usr/bin/curl"
ATTEMPTS = 3
TIMEOUT_S = 15
CONNECT_TIMEOUT_S = 8
GAP_S = 20
# 近 30 天 nmu.service 最长一次停机 4 分 51 秒(9/30 重启);两轮 = 10-22 分钟才报,
# 上线和重启都不会把人叫起来。
DOWN_AFTER_RUNS = 2
RING = 144
DAY_S = 24 * 3600
SILENCE_MAX_S = DAY_S
DIR = Path("/home/eric/.openclaw/workspace/nmu-probe")
STATE_FILE = "state.json"
LOG_FILE = "probe.log"
SILENCE_FILE = "silence-until"
LOG_MAX_LINES = 3000
KEYCHAIN_GET = Path("/home/eric/.openclaw/bin/keychain-get")
WEBHOOK_SECRET = "OPENCLAW_DISCORD_DAILY_WEBHOOK"
WEBHOOK_PREFIX = "https://discord.com/api/webhooks/"
MENTION = "1403490018265399327"
USERNAME = "NMU 异地探针"
# Cloudflare 会 403 掉默认 Python-urllib UA 的 POST,自定义 UA 即可(notify_ops 同款)。
UA = "nmu-offbox-probe/1"
POST_ATTEMPTS = 2
POST_GAP_S = 30
MAX_CONTENT = 1900
STATE_VERSION = 1
STAMP = "%Y-%m-%dT%H:%M:%SZ"
SHANGHAI = timezone(timedelta(hours=8))
CURL_REASONS = {6: "dns", 7: "refused", 28: "timeout", 35: "tls", 60: "tls"}
REASON_TEXT = {
    "dns": "域名解析失败,sslip.io",
    "refused": "连接被拒",
    "timeout": "超时",
    "tls": "TLS/证书错误",
    "body_mismatch": "返回 200 但不是 /health 的应答",
}
HEADLINES = {
    "down": "🔴 NMU 站点从纽约看打不开",
    "recovered": "✅ NMU 站点已恢复",
    "combined": "⚠️ NMU 站点中断后已恢复",
}
_WEBHOOK_URL = re.compile(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\S+")
# 只吃可打印 ASCII 且不含 <>:中文紧贴在链接后面时不会被吞进去。
_BARE_LINK = re.compile(r"(?<!<)(https?://[!-;=?-~]+)")
_SHAPE = {
    "version": int, "status": str, "fail_streak": int, "first_fail": (str, type(None)),
    "down_since": (str, type(None)), "down_reason": (str, type(None)),
    "down_announced": bool, "last_run": (str, type(None)), "last_reason": (str, type(None)),
    "owed": list, "ring": list, "summary_24h": dict,
}


class Attempt(NamedTuple):
    ok: bool
    reason: str
    ms: int | None
    answered: bool          # 拿到了 HTTP 应答,哪怕是 403/502


class Verdict(NamedTuple):
    ok: bool
    reason: str
    ms: int | None
    probe_side: bool


class WebhookError(Exception):
    """取 webhook 失败;消息里只有原因代号,不含 stdout,更不含 webhook 本身。"""


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(STAMP)


def _ts(text: str) -> float:
    return datetime.strptime(text, STAMP).replace(tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------- 探测

def fetch(url: str, *, pin: bool = False, expect: dict | None = EXPECT,
          run=subprocess.run) -> Attempt:
    argv = [CURL, "-sS", "-A", UA, "--connect-timeout", str(CONNECT_TIMEOUT_S),
            "--max-time", str(TIMEOUT_S), "-w", "\n%{http_code} %{time_total}"]
    if pin:
        argv += ["--resolve", PIN]
    argv.append(url)
    try:
        result = run(argv, capture_output=True, encoding="utf-8", errors="replace",
                     timeout=TIMEOUT_S + 10)
    except subprocess.TimeoutExpired:
        return Attempt(False, "timeout", None, False)
    except OSError:
        return Attempt(False, "curl_unavailable", None, False)
    if result.returncode != 0:
        reason = CURL_REASONS.get(result.returncode, f"curl_{result.returncode}")
        return Attempt(False, reason, None, False)
    body, _, tail = result.stdout.rpartition("\n")
    code, _, seconds = tail.strip().partition(" ")
    try:
        ms = round(float(seconds) * 1000)
    except ValueError:
        ms = None
    if code != "200":
        return Attempt(False, f"http_{code}", ms, True)
    if expect is not None and not _body_matches(body, expect):
        return Attempt(False, "body_mismatch", ms, True)
    return Attempt(True, "ok", ms, True)


def _body_matches(body: str, expect: dict) -> bool:
    try:
        doc = json.loads(body)
    except ValueError:
        return False
    return isinstance(doc, dict) and all(doc.get(key) == value for key, value in expect.items())


def run_probe(url: str = URL, *, run=subprocess.run, sleep=time.sleep) -> Verdict:
    last = None
    for attempt in range(ATTEMPTS):
        if attempt:
            sleep(GAP_S)
        last = fetch(url, run=run)
        if last.ok:
            return Verdict(True, "ok", last.ms, False)
    control = fetch(CONTROL, expect=None, run=run)
    if not control.answered:
        return Verdict(False, f"probe_side:{control.reason}", None, True)
    if last.reason == "dns":
        pinned = fetch(url, pin=True, run=run)
        if not pinned.ok:
            return Verdict(False, pinned.reason, None, False)
    return Verdict(False, last.reason, None, False)


# ---------------------------------------------------------------- 状态

def fresh_state() -> dict:
    return {"version": STATE_VERSION, "status": "up", "fail_streak": 0, "first_fail": None,
            "down_since": None, "down_reason": None, "down_announced": False,
            "last_run": None, "last_reason": None, "owed": [], "ring": [],
            "summary_24h": {"runs": 0, "fails": 0, "probe_side": 0, "p95_ms": None}}


def _read_state(path: Path) -> dict:
    """形状不对就抛:半份状态比没有状态危险,会发出没头没尾的 RECOVERED。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not all(
            key in raw and isinstance(raw[key], kind) for key, kind in _SHAPE.items()):
        raise ValueError("state_shape")
    if raw["version"] != STATE_VERSION or raw["status"] not in ("up", "down") \
            or raw["fail_streak"] < 0:
        raise ValueError("state_values")
    if raw["status"] == "down" and raw["down_since"] is None:
        raise ValueError("down_without_since")
    for key in ("first_fail", "down_since", "last_run"):
        if raw[key] is not None:
            _ts(raw[key])
    for message in raw["owed"]:
        if message["kind"] not in HEADLINES or not isinstance(message["reason"], str):
            raise ValueError("owed_shape")
        _ts(message["since"])
        if message["kind"] == "down":
            int(message["streak"])
        else:
            _ts(message["until"])
    for entry in raw["ring"]:
        _ts(entry["t"])
        if not isinstance(entry["r"], str) or not isinstance(entry["probe"], bool) \
                or not isinstance(entry["ms"], (int, type(None))):
            raise ValueError("ring_shape")
    return raw


def load_state(path: Path) -> dict:
    try:
        return _read_state(path)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return fresh_state()


def _write_private(path: Path, text: str) -> None:
    """临时文件 → fsync → os.replace;权限显式 0600,不信进程 umask。"""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def save_state(path: Path, state: dict) -> None:
    _write_private(path, json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


def _append_log(path: Path, line: str) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    lines.append(line)
    _write_private(path, "\n".join(lines[-LOG_MAX_LINES:]) + "\n")


def _silence_until(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    return int(text) if re.fullmatch(r"[0-9]{1,12}", text) else None


def is_silenced(path: Path, now: float) -> bool:
    until = _silence_until(path)
    return until is not None and now < until <= now + SILENCE_MAX_S


def summarize(ring: list, now: float) -> dict:
    recent = [entry for entry in ring if _ts(entry["t"]) > now - DAY_S]
    fast = sorted(entry["ms"] for entry in recent
                  if entry["r"] == "ok" and entry["ms"] is not None)
    return {
        "runs": len(recent),
        "fails": sum(1 for entry in recent if entry["r"] != "ok" and not entry["probe"]),
        "probe_side": sum(1 for entry in recent if entry["probe"]),
        "p95_ms": fast[math.ceil(0.95 * len(fast)) - 1] if fast else None,
    }


def decide(state: dict, verdict: Verdict, now: float, silenced: bool) -> dict:
    """纯函数:旧状态 + 这一轮的结论 → 新状态;要发的消息排在 owed 里。"""
    new = copy.deepcopy(state)
    stamp = _iso(now)
    new["last_run"] = stamp
    new["last_reason"] = verdict.reason
    new["ring"] = (new["ring"] + [{"t": stamp, "r": verdict.reason, "ms": verdict.ms,
                                   "probe": verdict.probe_side}])[-RING:]
    new["summary_24h"] = summarize(new["ring"], now)
    if verdict.probe_side:
        return new
    if verdict.ok:
        if new["status"] == "down":
            _close_incident(new, stamp)
        new.update(status="up", fail_streak=0, first_fail=None)
        return new
    new["fail_streak"] += 1
    if new["first_fail"] is None:
        new["first_fail"] = stamp
    if new["status"] == "up" and new["fail_streak"] >= DOWN_AFTER_RUNS:
        new.update(status="down", down_since=new["first_fail"], down_announced=False)
    if new["status"] == "down":
        new["down_reason"] = verdict.reason
        if not new["down_announced"] and not silenced:
            new["owed"].append({"kind": "down", "since": new["down_since"],
                                "reason": verdict.reason, "streak": new["fail_streak"]})
            new["down_announced"] = True
    return new


def _close_incident(state: dict, stamp: str) -> None:
    since = state["down_since"]
    pending = [index for index, message in enumerate(state["owed"])
               if message["kind"] == "down" and message["since"] == since]
    closing = {"since": since, "until": stamp, "reason": state["down_reason"] or "unknown"}
    if pending:
        state["owed"][pending[0]] = {"kind": "combined", **closing}
    elif state["down_announced"]:
        state["owed"].append({"kind": "recovered", **closing})
    state.update(down_since=None, down_reason=None, down_announced=False)


# ---------------------------------------------------------------- 消息

def _when(ts: float) -> str:
    utc = datetime.fromtimestamp(ts, timezone.utc)
    return f"{utc:%m-%d %H:%M} UTC(上海 {utc.astimezone(SHANGHAI):%m-%d %H:%M})"


def _reason_text(reason: str) -> str:
    if reason.startswith("http_"):
        return f"HTTP {reason[5:]}"
    gloss = REASON_TEXT.get(reason)
    return f"{gloss}({reason})" if gloss else reason


def render(message: dict) -> tuple[str, bool]:
    """(正文, 要不要 @Eric)。"""
    since = _ts(message["since"])
    reason = _reason_text(message["reason"])
    if message["kind"] == "down":
        return (f"{HEADLINES['down']} · 连续 {message['streak']} 轮失败\n"
                f"自 {_when(since)} 起 · 原因:{reason}\n"
                f"探的是 {URL}\n"
                "恢复时再发一条,中间不重复提醒。"), True
    until = _ts(message["until"])
    minutes = max(1, round((until - since) / 60))
    span = f"{_when(since)} → {_when(until)}"
    if message["kind"] == "recovered":
        return (f"{HEADLINES['recovered']}(从纽约看)· 中断约 {minutes} 分钟\n"
                f"{span} · 当时的原因:{reason}"), False
    return (f"{HEADLINES['combined']}(从纽约看)· 中断约 {minutes} 分钟\n"
            f"{span} · 原因:{reason}\n"
            "当时那条「打不开」的告警没发出去,这是补报。"), False


def webhook(run=subprocess.run) -> str:
    try:
        result = run([str(KEYCHAIN_GET), WEBHOOK_SECRET], capture_output=True,
                     encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise WebhookError(f"keychain_get_unavailable err={type(error).__name__}") from None
    if result.returncode != 0:
        raise WebhookError(f"keychain_get_rc={result.returncode}")
    # keychain-get 吐的是 {"values": {NAME: value}} 包封,不是裸值(2026-08-19 踩过)。
    try:
        value = json.loads(result.stdout)["values"][WEBHOOK_SECRET]
    except (ValueError, KeyError, TypeError):
        raise WebhookError("keychain_get_not_a_wrapper") from None
    if not isinstance(value, str) or not value.startswith(WEBHOOK_PREFIX):
        raise WebhookError("webhook_bad_shape")
    return value


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _send(url: str, payload: dict) -> tuple[bool, str]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=10, context=_ssl_context()) as resp:
            code = resp.status
    except urllib.error.HTTPError as error:
        return False, f"http_{error.code}"
    except OSError as error:
        return False, f"err={type(getattr(error, 'reason', error)).__name__}"
    if code // 100 != 2:
        return False, f"http_{code}"
    return True, f"status={code}"


def _get_json(url: str) -> tuple[bool, object]:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=10, context=_ssl_context()) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return False, f"http_{error.code}"
    except (OSError, ValueError) as error:
        return False, f"err={type(error).__name__}"


def post(url: str, text: str, *, mention: bool, send=_send, sleep=time.sleep,
         attempts: int = POST_ATTEMPTS) -> tuple[bool, str]:
    content = _BARE_LINK.sub(r"<\1>", _WEBHOOK_URL.sub("<webhook>", text))
    if mention:
        content = f"<@{MENTION}> {content}"
    payload = {"content": content[:MAX_CONTENT], "username": USERNAME,
               "allowed_mentions": {"users": [MENTION]} if mention else {"parse": []}}
    outcome = "not_sent"
    for attempt in range(attempts):
        if attempt:
            sleep(POST_GAP_S)
        delivered, outcome = send(url, payload)
        if delivered:
            return True, outcome
    return False, outcome


def _deliver(state: dict, path: Path, *, run, send, sleep) -> tuple[list[str], str | None]:
    if not state["owed"]:
        return [], None
    try:
        url = webhook(run)
    except WebhookError as error:
        return [], str(error)
    sent = []
    while state["owed"]:
        text, mention = render(state["owed"][0])
        delivered, outcome = post(url, text, mention=mention, send=send, sleep=sleep)
        if not delivered:
            return sent, outcome
        sent.append(state["owed"].pop(0)["kind"])
        # 发出去一条就落盘一次:后面那条失败或进程被杀,这一条也不会再发。
        save_state(path, state)
    return sent, None


# ---------------------------------------------------------------- 命令行

def _log_line(now: float, state: dict, verdict: Verdict, silenced: bool,
              sent: list[str], error: str | None) -> str:
    ms = verdict.ms if verdict.ms is not None else "-"
    parts = [_iso(now), state["status"].upper(), verdict.reason, f"ms={ms}",
             f"streak={state['fail_streak']}"]
    if silenced:
        parts.append("silenced")
    if sent:
        parts.append("sent=" + ",".join(kind.upper() for kind in sent))
    if state["owed"]:
        parts.append("owed=" + ",".join(m["kind"].upper() for m in state["owed"]))
    if error:
        parts.append(f"send_error={error}")
    return " ".join(parts)


def _cron_run(base: Path, *, run, send, sleep, clock) -> int:
    path = base / STATE_FILE
    state = load_state(path)
    verdict = run_probe(URL, run=run, sleep=sleep)
    now = clock()
    silenced = is_silenced(base / SILENCE_FILE, now)
    state = decide(state, verdict, now, silenced)
    # 先落盘再发:发送途中被杀,下一轮还记得欠着什么。
    save_state(path, state)
    sent, error = _deliver(state, path, run=run, send=send, sleep=sleep)
    _append_log(base / LOG_FILE, _log_line(now, state, verdict, silenced, sent, error))
    return 0


def _dry_run(args, base: Path, *, run, sleep, clock) -> int:
    real = base / STATE_FILE
    scratch = args.state
    if scratch is not None and scratch.resolve() == real.resolve():
        print(f"--state 不能指向正式的 {real}", file=sys.stderr)
        return 2
    state = load_state(real if scratch is None else scratch)
    verdict = run_probe(args.url or URL, run=run, sleep=sleep)
    now = clock()
    silenced = is_silenced(base / SILENCE_FILE, now)
    state = decide(state, verdict, now, silenced)
    would = ",".join(message["kind"].upper() for message in state["owed"]) or "-"
    # 当作已经发出去:自测下一轮才走得到 RECOVERED,而不是合成补报。
    state["owed"] = []
    if scratch is not None:
        save_state(scratch, state)
    ms = verdict.ms if verdict.ms is not None else "-"
    print(f"status={state['status']} result={verdict.reason} ms={ms} "
          f"streak={state['fail_streak']} would_send={would}"
          + (" silenced=1" if silenced else ""))
    return 0


def _silence_note(path: Path, now: float) -> str:
    if not path.exists():
        return "none"
    if is_silenced(path, now):
        return f"until:{_iso(_silence_until(path))}"
    return "ignored"


def _status(base: Path, now: float) -> int:
    path = base / STATE_FILE
    if not path.exists():
        print("state=missing(第一轮 :x4 跑完才有)")
        return 0
    try:
        state = _read_state(path)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print("state=corrupt(下一轮会当成全新的 UP 重来)")
        return 1
    age = int(now - _ts(state["last_run"])) if state["last_run"] else None
    words = [f"status={state['status']}", f"last_run={state['last_run']}", f"age_s={age}",
             f"streak={state['fail_streak']}", f"last_reason={state['last_reason']}",
             f"owed={len(state['owed'])}", f"silence={_silence_note(base / SILENCE_FILE, now)}"]
    if state["status"] == "down":
        words += [f"down_since={state['down_since']}", f"down_reason={state['down_reason']}"]
    summary = state["summary_24h"]
    print(" ".join(words))
    print(f"24h runs={summary['runs']} fails={summary['fails']} "
          f"probe_side={summary['probe_side']} p95_ms={summary['p95_ms']}")
    return 0


def _check_webhook(*, run, get) -> int:
    try:
        url = webhook(run)
    except WebhookError as error:
        print(error, file=sys.stderr)
        return 1
    ok, info = get(url)
    if not ok or not isinstance(info, dict):
        print(f"check_webhook_failed {info if not ok else 'not_an_object'}", file=sys.stderr)
        return 1
    # 应答里带着 token,只挑这三项打印。
    print(" ".join(f"{key}={info.get(key)}" for key in ("channel_id", "guild_id", "name")))
    return 0


def _test_alert(*, run, send) -> int:
    try:
        url = webhook(run)
    except WebhookError as error:
        print(error, file=sys.stderr)
        return 1
    text = (f"🧪 {USERNAME} · 测试消息(安装验收,不是故障)\n"
            f"从纽约看 {URL},每 10 分钟一次;连续 {DOWN_AFTER_RUNS} 轮失败才报「打不开」,"
            "恢复时再报一次。")
    # 只试一次:重试有可能让这条测试消息发两遍。
    delivered, outcome = post(url, text, mention=False, send=send, attempts=1)
    print(f"test_alert_{'sent' if delivered else 'failed'} {outcome}")
    return 0 if delivered else 1


def main(argv: list[str] | None = None, *, base: Path = DIR, run=subprocess.run,
         send=_send, get=_get_json, sleep=time.sleep, clock=time.time) -> int:
    parser = argparse.ArgumentParser(
        description="NMU 异地探针:从纽约探 /health,状态变了才发 Discord。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="照常探、照常判,不发消息、不写正式 state.json 与 probe.log")
    mode.add_argument("--status", action="store_true", help="只读 state.json,不联网")
    mode.add_argument("--check-webhook", action="store_true",
                      help="GET webhook,只打印 channel_id/guild_id/name")
    mode.add_argument("--test-alert", action="store_true",
                      help="真发一条标明是测试的消息(只试一次)")
    parser.add_argument("--state", type=Path, help="只配 --dry-run:读写这份草稿状态")
    parser.add_argument("--url", help="只配 --dry-run:探这个地址(自测用)")
    args = parser.parse_args(argv)
    if (args.state is not None or args.url is not None) and not args.dry_run:
        parser.error("--state / --url 只能跟 --dry-run 一起用")

    if args.status:
        return _status(base, clock())
    if args.check_webhook:
        return _check_webhook(run=run, get=get)
    if args.test_alert:
        return _test_alert(run=run, send=send)
    if args.dry_run:
        return _dry_run(args, base, run=run, sleep=sleep, clock=clock)
    return _cron_run(base, run=run, send=send, sleep=sleep, clock=clock)


if __name__ == "__main__":
    raise SystemExit(main())
