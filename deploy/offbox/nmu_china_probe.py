#!/usr/bin/env python3
"""NMU 国内探针:每小时请 Globalping 的 6 个国内探针连一次 NMU 的 TCP 443,状态变了才发 Discord。

为什么要有它:10-28 起国内到 box A 整个 IP 的 TCP 都被封了,本机 nmu-health、纽约的
nmu_offbox_probe.py、Heartbeat 的 check_bwg_nodes_live 都从美国看,一直是绿的,是代理
坏了才发现。这一份从国内看;等解封也靠它报。

还没装。设计上跟 nmu_offbox_probe.py 放同一个目录(openclaw-prod 的
~/.openclaw/workspace/nmu-probe/),发 Discord(keychain-get 取 webhook、UA、重试、
链接加尖括号)、0600 落盘、静默文件都直接用它那一份,所以要用 python3 -B 照常起,
不能加 -I/-P(脚本目录不在 sys.path 里就 import 不到它)。

一轮怎么判:
  - POST 一次测量:ping、protocol=TCP、port=443、3 个包,locations=[{country: CN, limit: 6}];
    只送 --target 给的公网 IP 或域名。每 2 秒 GET 一次结果,最多 45 次。
  - 每个探针:finished 且至少一个包握手成功 = 通;finished 一个没成、或 failed 且
    failureSource 不是 internal = 不通;offline、in-progress、failed+internal、不是 CN、
    没有 ASN 的不算数。
  - 算数的探针 ≥4 个、分属 ≥2 个 ASN 才下结论:全通 up,全不通 down,其余 partial。
  - Globalping 回 429/4xx/5xx、连不上、应答不像样、等不完、算数的探针不够 → unevaluable。

额度:匿名每个出口 IP 每小时 250 个测试(一个探针跑一次算一个),单次最多 50 个探针;
GET 单个测量每秒最多 2 次。出处:https://api.globalping.io/v1/spec.yaml 开头的
「Limits and credits」表与 https://globalping.io/credits(2026-11-02 读)。排程
17 * * * *(UTC,避开纽约探针的 :x4 和 watchdog 的 :00/:05):每小时 6 个测试,不到
额度的 3%,同一台机器上手动再测几十个也挤不掉它。这是文档写的数,没在 droplet 上量过;
每轮把应答头 X-RateLimit-Remaining/Limit 记进 china.log 的 quota=,装上一天就能核对。

状态规则(tests/test_china_probe.py 逐条钉住):
  1. 新结论连续 CONFIRM_RUNS 轮才改状态,since 记这一串的第一轮;判 down 要 1-2 小时,
     一个探针抽风一轮翻不了状态。
  2. unevaluable 不改状态、不动确认中的连胜,只把 unevaluable_streak 加一;之前已确认、因静默还没
     报的那条照样补报(不等下一次评得了)。
  3. 改到 down 或 partial 欠一条消息;比上次告诉 Eric 的更坏才 @Eric。
  4. 回到 up 欠一条带中断时长的「已恢复」;坏消息一直没发出去就恢复了 → 合成一条补报。
  5. 发送失败的继续欠着,下一轮再发;发出去的不会再发第二次。
  6. 静默文件就是纽约探针那一份 silence-until,规则也一样:只压新的坏消息;窗口里开始
     又结束的一条都不发;过了窗口还没好就补报,since 仍是真正开始的那一轮。
  7. china-state.json 记 status、since、announced、owed、last_counts、ring 与 summary_24h;
     写法同纽约探针(临时文件、fsync、os.replace、0600);缺了或坏了当全新的 up。

用法:
  nmu_china_probe.py --target HOST            cron 那一轮;stdout 什么都不写,一行进 china.log
  nmu_china_probe.py --plan --target HOST     打印要发的请求与判定规则;不联网、不读写文件
                                              (--dry-run 同义)
  nmu_china_probe.py --check --target HOST    真测一次(用掉 6 个测试额度),只打印结论;
                                              不发 Discord、不读写状态
  nmu_china_probe.py --status                 只读 china-state.json,不联网
"""
from __future__ import annotations

import argparse
import copy
import gzip
import http.client
import json
from pathlib import Path
import re
import subprocess
import time
from typing import NamedTuple
import urllib.error
import urllib.request
import zlib

import nmu_offbox_probe as offbox

API = "https://api.globalping.io/v1"
PORT = 443
PROBES = 6
PACKETS = 3
MIN_PROBES = 4
MIN_ASNS = 2
CONFIRM_RUNS = 2
POLL_GAP_S = 2
POLLS = 45
HTTP_TIMEOUT_S = 20
SCHEDULE = "17 * * * *"
RUNS_PER_HOUR = 1
ANON_TESTS_PER_HOUR = 250
RING = 168
DIR = offbox.DIR
STATE_FILE = "china-state.json"
LOG_FILE = "china.log"
SILENCE_FILE = offbox.SILENCE_FILE
UA = "nmu-china-probe/1"
STATE_VERSION = 1
UNEVALUABLE = "unevaluable"
RANK = {"up": 0, "partial": 1, "down": 2}
HEADLINES = {
    "down": "🔴 NMU 从国内看打不开",
    "partial": "🟠 NMU 从国内看部分打不开",
    "up": "✅ NMU 从国内看已恢复",
    "combined": "⚠️ NMU 从国内看中断后已恢复",
}
WORST = {"down": "全部连不上", "partial": "部分连不上"}
_TARGET = re.compile(r"(?=.{4,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                     r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+")
_MEASUREMENT_ID = re.compile(r"[A-Za-z0-9]{1,64}")
_SHAPE = {
    "version": int, "target": (str, type(None)), "status": str, "since": (str, type(None)),
    "outage_since": (str, type(None)), "announced": str, "candidate": (str, type(None)),
    "candidate_streak": int, "candidate_since": (str, type(None)),
    "last_run": (str, type(None)), "last_result": (str, type(None)),
    "last_reason": (str, type(None)), "last_quota": (str, type(None)),
    "last_evaluated": (str, type(None)), "last_counts": (dict, type(None)),
    "unevaluable_streak": int, "owed": list, "ring": list, "summary_24h": dict,
}
_iso, _ts = offbox._iso, offbox._ts


class Reply(NamedTuple):
    code: int | None        # None = 没拿到 HTTP 应答
    doc: object             # 解析好的 JSON;解析不了是 None
    quota: str | None       # X-RateLimit-Remaining/Limit
    error: str | None


class Verdict(NamedTuple):
    status: str
    reason: str
    reached: int
    probes: int
    asns: int
    blocked: list
    measurement: str | None
    quota: str | None


# ---------------------------------------------------------------- 测量

def request_body(target: str) -> dict:
    return {"type": "ping", "target": target,
            "locations": [{"country": "CN", "limit": PROBES}],
            "measurementOptions": {"protocol": "TCP", "port": PORT, "packets": PACKETS}}


def _reply(code: int, headers, raw: bytes) -> Reply:
    remaining, limit = headers.get("X-RateLimit-Remaining"), headers.get("X-RateLimit-Limit")
    quota = f"{remaining}/{limit}" if str(remaining).isdigit() and str(limit).isdigit() else None
    try:
        if (headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, EOFError, ValueError, zlib.error):
        doc = None
    return Reply(code, doc, quota, None)


def _call(method: str, url: str, body: dict | None) -> Reply:
    headers = {"User-Agent": UA, "Accept": "application/json", "Accept-Encoding": "gzip"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S,
                                    context=offbox._ssl_context()) as resp:
            return _reply(resp.status, resp.headers, resp.read())
    except urllib.error.HTTPError as error:
        try:
            raw = error.read()
        except (OSError, http.client.HTTPException):
            raw = b""
        return _reply(error.code, error.headers or {}, raw)
    except (OSError, http.client.HTTPException) as error:
        return Reply(None, None, None, f"err={type(getattr(error, 'reason', error)).__name__}")


def _outcome(result) -> bool | None:
    """通 True,不通 False,不算数 None。"""
    if not isinstance(result, dict):
        return None
    if result.get("status") == "failed":
        # internal 是 Globalping 自己的探针或 API 出错,与目标无关。
        return None if result.get("failureSource") == "internal" else False
    stats = result.get("stats")
    if result.get("status") != "finished" or not isinstance(stats, dict):
        return None
    total, rcv = stats.get("total"), stats.get("rcv")
    if type(total) is not int or type(rcv) is not int or total < 1:
        return None
    return rcv > 0


def _label(probe: dict) -> str:
    city = re.sub(r"[^0-9A-Za-z .'-]", "", str(probe.get("city") or ""))[:24].strip()
    return f"{city or '?'} AS{probe['asn']}"


def classify(results: list, *, measurement: str | None = None,
             quota: str | None = None) -> Verdict:
    counted = []
    for item in results:
        probe = item.get("probe") if isinstance(item, dict) else None
        if not isinstance(probe, dict) or probe.get("country") != "CN" \
                or type(probe.get("asn")) is not int:
            continue
        reached = _outcome(item.get("result"))
        if reached is not None:
            counted.append((reached, probe["asn"], _label(probe)))
    ok = sum(1 for reached, _, _ in counted if reached)
    asns = len({asn for _, asn, _ in counted})
    blocked = [label for reached, _, label in counted if not reached]
    if len(counted) < MIN_PROBES or asns < MIN_ASNS:
        status, reason = UNEVALUABLE, "coverage"
    elif ok == len(counted):
        status, reason = "up", "ok"
    elif ok == 0:
        status, reason = "down", "blocked"
    else:
        status, reason = "partial", "partial"
    return Verdict(status, reason, ok, len(counted), asns, blocked, measurement, quota)


def _unevaluable(reply: Reply, measurement: str | None, quota: str | None) -> Verdict:
    if reply.code is None:
        reason = f"api_{reply.error}"
    elif reply.code in (200, 202):
        reason = "api_bad_reply"
    else:
        reason = f"api_{reply.code}"
    return Verdict(UNEVALUABLE, reason, 0, 0, 0, [], measurement, quota)


def measure(target: str, *, api=_call, sleep=time.sleep) -> Verdict:
    created = api("POST", f"{API}/measurements", request_body(target))
    mid = created.doc.get("id") if isinstance(created.doc, dict) else None
    if created.code != 202 or not isinstance(mid, str) or not _MEASUREMENT_ID.fullmatch(mid):
        return _unevaluable(created, None, created.quota)
    for _ in range(POLLS):
        sleep(POLL_GAP_S)
        got = api("GET", f"{API}/measurements/{mid}", None)
        if got.code != 200 or not isinstance(got.doc, dict) \
                or not isinstance(got.doc.get("results"), list):
            return _unevaluable(got, mid, created.quota)
        if got.doc.get("status") != "in-progress":
            return classify(got.doc["results"], measurement=mid, quota=created.quota)
    # 没跑完的多半是在等超时的那几个,只拿跑完的下结论会偏向「通」。
    return Verdict(UNEVALUABLE, "poll_timeout", 0, 0, 0, [], mid, created.quota)


# ---------------------------------------------------------------- 状态

def _empty_summary() -> dict:
    return {"runs": 0, **{status: 0 for status in (*RANK, UNEVALUABLE)}}


def fresh_state() -> dict:
    return {"version": STATE_VERSION, "target": None, "status": "up", "since": None,
            "outage_since": None, "announced": "up", "candidate": None,
            "candidate_streak": 0, "candidate_since": None, "last_run": None,
            "last_result": None, "last_reason": None, "last_quota": None,
            "last_evaluated": None, "last_counts": None, "unevaluable_streak": 0,
            "owed": [], "ring": [], "summary_24h": _empty_summary()}


def _counts_ok(doc: dict) -> bool:
    return all(type(doc[key]) is int for key in ("reached", "probes", "asns")) \
        and isinstance(doc["blocked"], list) and all(isinstance(x, str) for x in doc["blocked"])


def _read_state(path: Path) -> dict:
    """形状不对就抛:半份状态比没有状态危险,会发出没头没尾的「已恢复」。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not all(
            key in raw and isinstance(raw[key], kind) for key, kind in _SHAPE.items()):
        raise ValueError("state_shape")
    if raw["version"] != STATE_VERSION or raw["status"] not in RANK \
            or raw["announced"] not in RANK or raw["candidate"] not in (None, *RANK) \
            or raw["candidate_streak"] < 0 or raw["unevaluable_streak"] < 0:
        raise ValueError("state_values")
    if (raw["status"] != "up" or raw["announced"] != "up") \
            and (raw["outage_since"] is None or raw["last_counts"] is None):
        raise ValueError("outage_without_since")
    if raw["last_counts"] is not None and not _counts_ok(raw["last_counts"]):
        raise ValueError("counts_shape")
    for key in ("since", "outage_since", "candidate_since", "last_run", "last_evaluated"):
        if raw[key] is not None:
            _ts(raw[key])
    for message in raw["owed"]:
        if message["kind"] not in HEADLINES or message["from"] not in RANK \
                or not isinstance(message["mention"], bool) \
                or not isinstance(message["target"], str) or not _counts_ok(message):
            raise ValueError("owed_shape")
        _ts(message["since"])
        if message["kind"] in ("up", "combined"):
            _ts(message["until"])
        if message["kind"] == "combined" and message["worst"] not in WORST:
            raise ValueError("owed_shape")
    for entry in raw["ring"]:
        _ts(entry["t"])
        if entry["s"] not in (*RANK, UNEVALUABLE) or type(entry["ok"]) is not int \
                or type(entry["n"]) is not int:
            raise ValueError("ring_shape")
    return raw


def load_state(path: Path) -> dict:
    try:
        return _read_state(path)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return fresh_state()


def summarize(ring: list, now: float) -> dict:
    recent = [entry["s"] for entry in ring if _ts(entry["t"]) > now - offbox.DAY_S]
    return {"runs": len(recent), **{status: recent.count(status)
                                    for status in (*RANK, UNEVALUABLE)}}


def decide(state: dict, verdict: Verdict, now: float, silenced: bool, target: str) -> dict:
    """纯函数:旧状态 + 这一轮的结论 → 新状态;要发的消息排在 owed 里。"""
    new = copy.deepcopy(state)
    stamp = _iso(now)
    new.update(target=target, last_run=stamp, last_result=verdict.status,
               last_reason=verdict.reason, last_quota=verdict.quota)
    new["ring"] = (new["ring"] + [{"t": stamp, "s": verdict.status, "ok": verdict.reached,
                                   "n": verdict.probes}])[-RING:]
    new["summary_24h"] = summarize(new["ring"], now)
    if verdict.status == UNEVALUABLE:
        new["unevaluable_streak"] += 1
    else:
        new.update(unevaluable_streak=0, last_evaluated=stamp,
                   last_counts={"reached": verdict.reached, "probes": verdict.probes,
                                "asns": verdict.asns, "blocked": verdict.blocked})
        _advance(new, verdict.status, stamp)
    # 评估不了的一轮也要排:静默刚过而状态早已确认,补报不该等下一次评得了。
    _queue(new, silenced)
    if new["status"] == "up":
        new["outage_since"] = None
    return new


def _advance(state: dict, seen: str, stamp: str) -> None:
    if seen == state["status"]:
        state.update(candidate=None, candidate_streak=0, candidate_since=None)
        return
    if seen != state["candidate"]:
        state.update(candidate=seen, candidate_streak=0, candidate_since=stamp)
    state["candidate_streak"] += 1
    if state["candidate_streak"] < CONFIRM_RUNS:
        return
    if state["status"] == "up":
        state["outage_since"] = state["candidate_since"]
    state.update(status=seen, since=state["candidate_since"], candidate=None,
                 candidate_streak=0, candidate_since=None)


def _queue(state: dict, silenced: bool) -> None:
    status, told = state["status"], state["announced"]
    if status == told or (status != "up" and silenced):
        return
    message = {"kind": status, "from": told, "since": state["since"], "target": state["target"],
               "mention": RANK[status] > RANK[told], **state["last_counts"]}
    if status == "up":
        unsent = [m for m in state["owed"] if m["kind"] in WORST]
        state["owed"] = [m for m in state["owed"] if m["kind"] not in WORST]
        message.update(since=state["outage_since"], until=state["since"])
        if unsent and unsent[0]["from"] == "up":
            message.update(kind="combined", worst=max((m["kind"] for m in unsent), key=RANK.get))
    state["owed"].append(message)
    state["announced"] = status


# ---------------------------------------------------------------- 消息

def _duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 120:
        return f"{minutes} 分钟"
    hours = round(minutes / 60)
    return f"{hours} 小时" if hours < 48 else f"{round(hours / 24)} 天"


def render(message: dict) -> tuple[str, bool]:
    """(正文, 要不要 @Eric)。"""
    kind = message["kind"]
    since = _ts(message["since"])
    seen = (f"国内 {message['reached']}/{message['probes']} 个探针连得上 TCP {PORT}"
            f"({message['asns']} 个 ASN)")
    if kind in WORST:
        lines = [f"{HEADLINES[kind]} · {seen}",
                 f"自 {offbox._when(since)} 起 · 探的是 {message['target']}"]
        if message["blocked"]:
            lines.append("连不上的:" + "、".join(message["blocked"]))
        lines.append("状态再变时再发一条,中间不重复提醒。")
        return "\n".join(lines), message["mention"]
    until = _ts(message["until"])
    span = (f"{offbox._when(since)} → {offbox._when(until)} · 持续约 {_duration(until - since)}")
    if kind == "up":
        return f"{HEADLINES['up']} · {seen}\n{span}", False
    return (f"{HEADLINES['combined']} · 最坏时{WORST[message['worst']]}\n{span}\n"
            "当时那条告警没发出去,这是补报。"), False


def _deliver(state: dict, path: Path, *, run, send, sleep) -> tuple[list[str], str | None]:
    if not state["owed"]:
        return [], None
    try:
        url = offbox.webhook(run)
    except offbox.WebhookError as error:
        return [], str(error)
    sent = []
    while state["owed"]:
        text, mention = render(state["owed"][0])
        delivered, outcome = offbox.post(url, text, mention=mention, send=send, sleep=sleep)
        if not delivered:
            return sent, outcome
        sent.append(state["owed"].pop(0)["kind"])
        # 发出去一条就落盘一次:后面那条失败或进程被杀,这一条也不会再发。
        offbox.save_state(path, state)
    return sent, None


# ---------------------------------------------------------------- 命令行

def _log_line(now: float, state: dict, verdict: Verdict, silenced: bool,
              sent: list[str], error: str | None) -> str:
    parts = [_iso(now), state["status"].upper(), f"result={verdict.status}", verdict.reason,
             f"reached={verdict.reached}/{verdict.probes}", f"asns={verdict.asns}"]
    if state["candidate"]:
        parts.append(f"candidate={state['candidate']}x{state['candidate_streak']}")
    if verdict.quota:
        parts.append(f"quota={verdict.quota}")
    if verdict.measurement:
        parts.append(f"m={verdict.measurement}")
    if silenced:
        parts.append("silenced")
    if sent:
        parts.append("sent=" + ",".join(kind.upper() for kind in sent))
    if state["owed"]:
        parts.append("owed=" + ",".join(m["kind"].upper() for m in state["owed"]))
    if error:
        parts.append(f"send_error={error}")
    return " ".join(parts)


def _cron_run(base: Path, target: str, *, run, send, api, sleep, clock) -> int:
    path = base / STATE_FILE
    state = load_state(path)
    verdict = measure(target, api=api, sleep=sleep)
    now = clock()
    silenced = offbox.is_silenced(base / SILENCE_FILE, now)
    state = decide(state, verdict, now, silenced, target)
    # 先落盘再发:发送途中被杀,下一轮还记得欠着什么。
    offbox.save_state(path, state)
    sent, error = _deliver(state, path, run=run, send=send, sleep=sleep)
    offbox._append_log(base / LOG_FILE, _log_line(now, state, verdict, silenced, sent, error))
    return 0


def _plan(base: Path, target: str) -> int:
    print(f"POST {API}/measurements {json.dumps(request_body(target), ensure_ascii=False)}")
    print(f"GET {API}/measurements/<id> 每 {POLL_GAP_S} 秒一次,最多 {POLLS} 次")
    print(f"判定:算数的探针 ≥{MIN_PROBES} 个、≥{MIN_ASNS} 个 ASN;全通 up、全不通 down、"
          f"其余 partial;新结论连续 {CONFIRM_RUNS} 轮才改状态;API 出错只记 {UNEVALUABLE}")
    print(f"排程 {SCHEDULE}(UTC)· 每小时 {PROBES * RUNS_PER_HOUR} 个测试,"
          f"匿名额度每个 IP 每小时 {ANON_TESTS_PER_HOUR} 个")
    print(f"状态 {base / STATE_FILE} · 日志 {base / LOG_FILE} · "
          f"静默 {base / SILENCE_FILE}(与纽约探针共用)")
    print("没联网、没读写文件;真测一次用 --check")
    return 0


def _check(target: str, *, api, sleep) -> int:
    verdict = measure(target, api=api, sleep=sleep)
    print(f"result={verdict.status} reason={verdict.reason} "
          f"reached={verdict.reached}/{verdict.probes} asns={verdict.asns} "
          f"measurement={verdict.measurement or '-'} quota={verdict.quota or '-'}")
    if verdict.blocked:
        print("连不上的:" + "、".join(verdict.blocked))
    return 1 if verdict.status == UNEVALUABLE else 0


def _status(base: Path, now: float) -> int:
    path = base / STATE_FILE
    if not path.exists():
        print("state=missing(没装,或第一轮还没跑)")
        return 0
    try:
        state = _read_state(path)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print("state=corrupt(下一轮会当成全新的 up 重来)")
        return 1
    age = int(now - _ts(state["last_run"])) if state["last_run"] else None
    words = [f"status={state['status']}", f"since={state['since']}",
             f"target={state['target']}", f"last_run={state['last_run']}", f"age_s={age}",
             f"last_result={state['last_result']}", f"last_reason={state['last_reason']}",
             f"unevaluable_streak={state['unevaluable_streak']}", f"owed={len(state['owed'])}",
             f"silence={offbox._silence_note(base / SILENCE_FILE, now)}"]
    if state["candidate"]:
        words.append(f"candidate={state['candidate']}x{state['candidate_streak']}")
    counts = state["last_counts"]
    if counts:
        words.append(f"reached={counts['reached']}/{counts['probes']} asns={counts['asns']}")
    summary = state["summary_24h"]
    print(" ".join(words))
    print("24h " + " ".join(f"{key}={summary.get(key)}"
                            for key in ("runs", *RANK, UNEVALUABLE)))
    return 0


def main(argv: list[str] | None = None, *, base: Path = DIR, run=subprocess.run,
         send=offbox._send, api=_call, sleep=time.sleep, clock=time.time) -> int:
    parser = argparse.ArgumentParser(
        description="NMU 国内探针:从国内 Globalping 探针测 TCP 443,状态变了才发 Discord。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", "--dry-run", dest="plan", action="store_true",
                      help="只打印要发给 Globalping 的请求与判定规则;不联网、不读写文件")
    mode.add_argument("--check", action="store_true",
                      help=f"真测一次(用掉 {PROBES} 个测试额度),只打印结论;不发 Discord、不读写状态")
    mode.add_argument("--status", action="store_true", help="只读 china-state.json,不联网")
    parser.add_argument("--target", help="要测的公网 IP 或域名;只送这一项给 Globalping")
    args = parser.parse_args(argv)
    if args.status:
        if args.target is not None:
            parser.error("--status 不用 --target")
        return _status(base, clock())
    if args.target is None or not _TARGET.fullmatch(args.target):
        parser.error("--target 要一个公网 IP 或域名(不带协议、端口、路径)")
    if args.plan:
        return _plan(base, args.target)
    if args.check:
        return _check(args.target, api=api, sleep=sleep)
    return _cron_run(base, args.target, run=run, send=send, api=api, sleep=sleep, clock=clock)


if __name__ == "__main__":
    raise SystemExit(main())
