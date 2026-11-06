#!/usr/bin/env python3
"""NMU 国内探针:每小时请 Globalping 的 6 个国内探针连一次 NMU 的 TCP 443,状态变了才发 Discord。

为什么要有它:10-28 起国内到 box A 整个 IP 的 TCP 都被封了,本机 nmu-health、纽约的
nmu_offbox_probe.py、Heartbeat 的 check_bwg_nodes_live 都从美国看,一直是绿的,是代理
坏了才发现。这一份从国内看;等解封也靠它报。

2026-11-03 装在 openclaw-prod 的 user crontab(:17,--target 是国内入口那台 droplet 的 IP)。
跟 nmu_offbox_probe.py 放同一个目录(~/.openclaw/workspace/nmu-probe/),发 Discord
(keychain-get 取 webhook、UA、重试、链接加尖括号)、0600 落盘、静默文件都直接用它那一份,
所以要用 python3 -B 照常起,不能加 -I/-P(脚本目录不在 sys.path 里就 import 不到它)。

一轮怎么判:
  - POST 一次测量:ping、protocol=TCP、port=443、3 个包,locations=[{country: CN, limit: 6}];
    只送 --target 给的公网 IP 或域名。每 2 秒 GET 一次结果,最多 45 次。
  - 每个探针:finished 且至少一个包握手成功 = 通;finished 一个没成、或 failed 且
    failureSource 不是 internal = 不通;offline、in-progress、failed+internal、不是 CN、
    没有 ASN 的不算数。
  - 算数的探针 ≥4 个、分属 ≥2 个 ASN 才下结论:全通 up,全不通 down,其余 partial。
  - Globalping 回 429/4xx/5xx、连不上、应答不像样、等不完、算数的探针不够 → unevaluable。

加了 --https-host 每轮再测一次 HTTPS(2026-11 加),抓 TCP 握手抓不到的:国内按 SNI 封这个
域名、国内 DNS 把 sslip.io 解析坏、droplet → box A 那一段断(haproxy 照样接 TCP)、证书坏:
  - TCP 那次建成(拿到测量 id)就再 POST 一次:http、target=--https-host、protocol=HTTPS、
    port=443、GET /build-id.txt,locations 填 TCP 那次的 id(同一批探针、同样的顺序)。
    不打 /health:PROXY protocol 上线前,经 droplet 来的请求在 box A 上都是 droplet 的 IP,
    backup_health_check.py 的机外死人开关数的正是这个 IP 的 GET /health,会被喂绿;
    /build-id.txt 是静态文件,发布的空闲闸本来就不数它。TCP 那次没建成就不测、不花额度。
  - 每个探针:finished、statusCode 是 200、tls.authorized 是 true = 通;finished 但证书
    没验过或不是 200、或 failed 且 failureSource 不是 internal = 不通;其余不算数,门槛同上。
  - 这一轮 TCP 是 up,HTTPS 的结论才算数:全通 up;有不通的就是「TCP 通但 HTTPS 不通」(down)。
    TCP 不是 up、或 HTTPS 那次 unevaluable → 这条线这一轮 unevaluable。TCP 本身照旧那样判。
  用到的字段(https://api.globalping.io/v1/spec.yaml 的 FinishedHttpTestResult、TlsCertificate、
  FailedTestResult、MeasurementLocations,2026-11-05 读):results[].result 的 status、failureSource
  (target/resolver/internal)、statusCode、tls(没有证书时是 null)、tls.authorized;locations 可以是
  上一次测量的 id。spec 只说 authorized 是「受信任的 CA 签的」;探针源码
  (jsdelivr/globalping-probe 的 src/command/handlers/http/undici.ts,同日读)用 tls.connect 带
  servername、rejectUnauthorized:false,取 tlsSocket.authorized,Node 在主机名对不上时也会置 false。
  timings 不用:dns/tls 可以是 null,failed 的结果 spec 里也没有它;rawOutput 照 spec 说的不解析。

额度:匿名每个出口 IP 每小时 250 个测试(一个探针跑一次算一个),单次最多 50 个探针;
GET 单个测量每秒最多 2 次。出处:https://api.globalping.io/v1/spec.yaml 开头的
「Limits and credits」表与 https://globalping.io/credits(2026-11-02 读)。排程
17 * * * *(UTC,避开纽约探针的 :x4 和 watchdog 的 :00/:05):每小时 6 个测试,不到
额度的 3%,同一台机器上手动再测几十个也挤不掉它。加 --https-host 是两次测量、各 6 个探针:
每小时 6 × 2 = 12 个,不到 5%;12 × 10 = 120 ≤ 250,仍留十倍余量;手动 --check 一次也是 12 个。
这是文档写的数,没在 droplet 上量过;每轮把应答头 X-RateLimit-Remaining/Limit 记进
china.log 的 quota=(HTTPS 那次的是 https_quota=),装上一天就能核对。

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
  8. HTTPS 那条线另记在 china-state.json 的 https 里,只有 up/down 两档,规则 1-6 照搬;
     它的消息排在 https.owed,同一轮 TCP 的先发。version 仍是 1:没有 https 的旧文件照常读,
     第一轮把这条线当 up,HTTPS 正常就什么都不发。不加 --https-host,发的请求、输出、日志、
     状态文件都与加这个参数之前逐字节一样(测试钉了摘要)。

用法:
  nmu_china_probe.py --target HOST [--https-host NAME]
                                              cron 那一轮;stdout 什么都不写,一行进 china.log
  nmu_china_probe.py --plan --target HOST     打印要发的请求与判定规则;不联网、不读写文件
                                              (--dry-run 同义)
  nmu_china_probe.py --check --target HOST    真测一次(用掉 6 个测试额度,加 --https-host 是
                                              12 个),只打印结论;不发 Discord、不读写状态
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
HTTPS_PATH = "/build-id.txt"
HTTPS_LEVELS = ("up", "down")
HTTPS_HEADLINES = {
    "down": "🟠 NMU 从国内看 TCP 通但 HTTPS 不通",
    "up": "✅ NMU 从国内看 HTTPS 已恢复",
    "combined": "⚠️ NMU 从国内看 HTTPS 中断后已恢复",
}
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
_HTTPS_SHAPE = {
    "target": (str, type(None)), "status": str, "since": (str, type(None)),
    "outage_since": (str, type(None)), "announced": str, "candidate": (str, type(None)),
    "candidate_streak": int, "candidate_since": (str, type(None)),
    "last_result": (str, type(None)), "last_reason": (str, type(None)),
    "last_counts": (dict, type(None)), "unevaluable_streak": int, "owed": list,
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


def https_body(host: str, probes_from: str) -> dict:
    return {"type": "http", "target": host, "locations": probes_from,
            "measurementOptions": {"protocol": "HTTPS", "port": PORT,
                                   "request": {"method": "GET", "path": HTTPS_PATH}}}


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


def _https_outcome(result) -> tuple[bool | None, str]:
    """(通 True / 不通 False / 不算数 None, 不通时接在探针名后面的原因)。"""
    if not isinstance(result, dict):
        return None, ""
    if result.get("status") == "failed":
        source = result.get("failureSource")
        if source == "internal":
            return None, ""
        return False, "(DNS 解析失败)" if source == "resolver" else "(连接或 TLS 断了)"
    code, tls = result.get("statusCode"), result.get("tls")
    if result.get("status") != "finished" or type(code) is not int:
        return None, ""
    if not isinstance(tls, dict) or tls.get("authorized") is not True:
        return False, "(证书没验过)"
    return (True, "") if code == 200 else (False, f"(HTTP {code})")


def _label(probe: dict) -> str:
    city = re.sub(r"[^0-9A-Za-z .'-]", "", str(probe.get("city") or ""))[:24].strip()
    return f"{city or '?'} AS{probe['asn']}"


def classify(results: list, *, measurement: str | None = None,
             quota: str | None = None, judge=None) -> Verdict:
    counted = []
    for item in results:
        probe = item.get("probe") if isinstance(item, dict) else None
        if not isinstance(probe, dict) or probe.get("country") != "CN" \
                or type(probe.get("asn")) is not int:
            continue
        reached, why = (_outcome(item.get("result")), "") if judge is None \
            else judge(item.get("result"))
        if reached is not None:
            counted.append((reached, probe["asn"], _label(probe) + why))
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


def measure(body: dict, *, api=_call, sleep=time.sleep, judge=None) -> Verdict:
    created = api("POST", f"{API}/measurements", body)
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
            return classify(got.doc["results"], measurement=mid, quota=created.quota,
                            judge=judge)
    # 没跑完的多半是在等超时的那几个,只拿跑完的下结论会偏向「通」。
    return Verdict(UNEVALUABLE, "poll_timeout", 0, 0, 0, [], mid, created.quota)


def measure_https(host: str, tcp: Verdict, *, api=_call, sleep=time.sleep) -> Verdict:
    if tcp.measurement is None:
        return Verdict(UNEVALUABLE, "no_tcp_measurement", 0, 0, 0, [], None, None)
    return measure(https_body(host, tcp.measurement), api=api, sleep=sleep, judge=_https_outcome)


def https_seen(tcp: Verdict, https: Verdict) -> tuple[str, str]:
    """HTTPS 这条线这一轮的 (结论, 原因)。"""
    if tcp.status != "up":
        return UNEVALUABLE, f"tcp_{tcp.status}"
    if https.status == UNEVALUABLE:
        return UNEVALUABLE, https.reason
    return ("up", "ok") if https.status == "up" else ("down", https.reason)


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


def fresh_https() -> dict:
    return {"target": None, "status": "up", "since": None, "outage_since": None,
            "announced": "up", "candidate": None, "candidate_streak": 0,
            "candidate_since": None, "last_result": None, "last_reason": None,
            "last_counts": None, "unevaluable_streak": 0, "owed": []}


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
    if "https" in raw:
        _check_https(raw["https"])
    return raw


def _check_https(track) -> None:
    if not isinstance(track, dict) or not all(
            key in track and isinstance(track[key], kind) for key, kind in _HTTPS_SHAPE.items()):
        raise ValueError("https_shape")
    if track["status"] not in HTTPS_LEVELS or track["announced"] not in HTTPS_LEVELS \
            or track["candidate"] not in (None, *HTTPS_LEVELS) \
            or track["candidate_streak"] < 0 or track["unevaluable_streak"] < 0:
        raise ValueError("https_values")
    if (track["status"] != "up" or track["announced"] != "up") \
            and (track["outage_since"] is None or track["last_counts"] is None):
        raise ValueError("https_outage_without_since")
    if track["last_counts"] is not None and not _counts_ok(track["last_counts"]):
        raise ValueError("https_counts_shape")
    for key in ("since", "outage_since", "candidate_since"):
        if track[key] is not None:
            _ts(track[key])
    for message in track["owed"]:
        if message["kind"] not in HTTPS_HEADLINES or message["from"] not in HTTPS_LEVELS \
                or not isinstance(message["mention"], bool) \
                or not isinstance(message["target"], str) or not _counts_ok(message):
            raise ValueError("https_owed_shape")
        _ts(message["since"])
        if message["kind"] in ("up", "combined"):
            _ts(message["until"])


def load_state(path: Path) -> dict:
    try:
        return _read_state(path)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return fresh_state()


def summarize(ring: list, now: float) -> dict:
    recent = [entry["s"] for entry in ring if _ts(entry["t"]) > now - offbox.DAY_S]
    return {"runs": len(recent), **{status: recent.count(status)
                                    for status in (*RANK, UNEVALUABLE)}}


def decide(state: dict, verdict: Verdict, now: float, silenced: bool, target: str, *,
           https: Verdict | None = None, https_host: str | None = None) -> dict:
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
    if https is not None:
        _decide_https(new.setdefault("https", fresh_https()), verdict, https, stamp, silenced,
                      https_host)
    return new


def _decide_https(track: dict, tcp: Verdict, https: Verdict, stamp: str, silenced: bool,
                  host: str) -> None:
    # 字段名与外层一致,_advance/_queue 原样复用;状态只会是 up/down。
    seen, reason = https_seen(tcp, https)
    track.update(target=host, last_result=seen, last_reason=reason)
    if seen == UNEVALUABLE:
        track["unevaluable_streak"] += 1
    else:
        track.update(unevaluable_streak=0,
                     last_counts={"reached": https.reached, "probes": https.probes,
                                  "asns": https.asns, "blocked": https.blocked})
        _advance(track, seen, stamp)
    _queue(track, silenced)
    if track["status"] == "up":
        track["outage_since"] = None


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


def render_https(message: dict) -> tuple[str, bool]:
    kind = message["kind"]
    since = _ts(message["since"])
    seen = (f"国内 {message['reached']}/{message['probes']} 个探针 GET {HTTPS_PATH} 拿到 200、"
            f"证书验过({message['asns']} 个 ASN)")
    if kind == "down":
        lines = [f"{HTTPS_HEADLINES['down']} · {seen}",
                 f"自 {offbox._when(since)} 起 · 探的是 https://{message['target']}{HTTPS_PATH}"
                 f" · 同一批探针 TCP {PORT} 都连得上"]
        if message["blocked"]:
            lines.append("不通的:" + "、".join(message["blocked"]))
        lines += ["多半是:「连接或 TLS 断了」= 国内按 SNI 封了这个域名、DNS 被污染指到别处,"
                  "或 droplet → box A 那一段断了(haproxy 照样接 TCP);「DNS 解析失败」= 国内解析不了"
                  " sslip.io;「证书没验过」= 证书过期、没签上或被换了;HTTP 403/5xx = box A 上的"
                  " Caddy 或应用拒了、坏了。",
                  "状态再变时再发一条,中间不重复提醒。"]
        return "\n".join(lines), message["mention"]
    until = _ts(message["until"])
    span = (f"{offbox._when(since)} → {offbox._when(until)} · 持续约 {_duration(until - since)}")
    if kind == "up":
        return f"{HTTPS_HEADLINES['up']} · {seen}\n{span}", False
    return (f"{HTTPS_HEADLINES['combined']} · 当时 TCP 通但 HTTPS 不通\n{span}\n"
            "当时那条告警没发出去,这是补报。"), False


def _deliver(state: dict, path: Path, *, run, send, sleep) -> tuple[list[str], str | None]:
    queues = [(state["owed"], render, "")]
    if "https" in state:
        queues.append((state["https"]["owed"], render_https, "https_"))
    if not any(owed for owed, _, _ in queues):
        return [], None
    try:
        url = offbox.webhook(run)
    except offbox.WebhookError as error:
        return [], str(error)
    sent = []
    for owed, show, prefix in queues:
        while owed:
            text, mention = show(owed[0])
            delivered, outcome = offbox.post(url, text, mention=mention, send=send, sleep=sleep)
            if not delivered:
                return sent, outcome
            sent.append(prefix + owed.pop(0)["kind"])
            # 发出去一条就落盘一次:后面那条失败或进程被杀,这一条也不会再发。
            offbox.save_state(path, state)
    return sent, None


# ---------------------------------------------------------------- 命令行

def _log_line(now: float, state: dict, verdict: Verdict, silenced: bool,
              sent: list[str], error: str | None, https: Verdict | None = None) -> str:
    parts = [_iso(now), state["status"].upper(), f"result={verdict.status}", verdict.reason,
             f"reached={verdict.reached}/{verdict.probes}", f"asns={verdict.asns}"]
    if state["candidate"]:
        parts.append(f"candidate={state['candidate']}x{state['candidate_streak']}")
    if verdict.quota:
        parts.append(f"quota={verdict.quota}")
    if verdict.measurement:
        parts.append(f"m={verdict.measurement}")
    owed = [m["kind"].upper() for m in state["owed"]]
    if https is not None:
        track = state["https"]
        parts += [f"https={track['status'].upper()}", f"https_result={track['last_result']}",
                  track["last_reason"], f"https_reached={https.reached}/{https.probes}",
                  f"https_asns={https.asns}"]
        if track["candidate"]:
            parts.append(f"https_candidate={track['candidate']}x{track['candidate_streak']}")
        if https.quota:
            parts.append(f"https_quota={https.quota}")
        if https.measurement:
            parts.append(f"https_m={https.measurement}")
        owed += ["HTTPS_" + m["kind"].upper() for m in track["owed"]]
    if silenced:
        parts.append("silenced")
    if sent:
        parts.append("sent=" + ",".join(kind.upper() for kind in sent))
    if owed:
        parts.append("owed=" + ",".join(owed))
    if error:
        parts.append(f"send_error={error}")
    return " ".join(parts)


def _cron_run(base: Path, target: str, https_host: str | None, *, run, send, api, sleep,
              clock) -> int:
    path = base / STATE_FILE
    state = load_state(path)
    verdict = measure(request_body(target), api=api, sleep=sleep)
    https = None if https_host is None else measure_https(https_host, verdict, api=api,
                                                          sleep=sleep)
    now = clock()
    silenced = offbox.is_silenced(base / SILENCE_FILE, now)
    state = decide(state, verdict, now, silenced, target, https=https, https_host=https_host)
    # 先落盘再发:发送途中被杀,下一轮还记得欠着什么。
    offbox.save_state(path, state)
    sent, error = _deliver(state, path, run=run, send=send, sleep=sleep)
    offbox._append_log(base / LOG_FILE,
                       _log_line(now, state, verdict, silenced, sent, error, https))
    return 0


def _plan(base: Path, target: str, https_host: str | None) -> int:
    print(f"POST {API}/measurements {json.dumps(request_body(target), ensure_ascii=False)}")
    print(f"GET {API}/measurements/<id> 每 {POLL_GAP_S} 秒一次,最多 {POLLS} 次")
    if https_host is not None:
        body = json.dumps(https_body(https_host, "<上面那次的 id>"), ensure_ascii=False)
        print(f"POST {API}/measurements {body}(上面那次建成才发)")
        print(f"GET {API}/measurements/<id> 同上")
    print(f"判定:算数的探针 ≥{MIN_PROBES} 个、≥{MIN_ASNS} 个 ASN;全通 up、全不通 down、"
          f"其余 partial;新结论连续 {CONFIRM_RUNS} 轮才改状态;API 出错只记 {UNEVALUABLE}")
    if https_host is not None:
        print(f"HTTPS:同一批探针要 HTTP 200 且 tls.authorized=true;这一轮 TCP 是 up 才算数,"
              f"全通 up、有不通的就是「TCP 通但 HTTPS 不通」;同样连续 {CONFIRM_RUNS} 轮才改状态,"
              f"API 出错只记 {UNEVALUABLE}")
    measurements = 1 if https_host is None else 2
    print(f"排程 {SCHEDULE}(UTC)· 每小时 {PROBES * RUNS_PER_HOUR * measurements} 个测试,"
          f"匿名额度每个 IP 每小时 {ANON_TESTS_PER_HOUR} 个")
    print(f"状态 {base / STATE_FILE} · 日志 {base / LOG_FILE} · "
          f"静默 {base / SILENCE_FILE}(与纽约探针共用)")
    print("没联网、没读写文件;真测一次用 --check")
    return 0


def _check(target: str, https_host: str | None, *, api, sleep) -> int:
    verdict = measure(request_body(target), api=api, sleep=sleep)
    print(f"result={verdict.status} reason={verdict.reason} "
          f"reached={verdict.reached}/{verdict.probes} asns={verdict.asns} "
          f"measurement={verdict.measurement or '-'} quota={verdict.quota or '-'}")
    if verdict.blocked:
        print("连不上的:" + "、".join(verdict.blocked))
    if https_host is None:
        return 1 if verdict.status == UNEVALUABLE else 0
    https = measure_https(https_host, verdict, api=api, sleep=sleep)
    seen, reason = https_seen(verdict, https)
    print(f"https result={https.status} reason={https.reason} "
          f"reached={https.reached}/{https.probes} asns={https.asns} "
          f"measurement={https.measurement or '-'} quota={https.quota or '-'} "
          f"seen={seen} {reason}")
    if https.blocked:
        print("HTTPS 不通的:" + "、".join(https.blocked))
    return 1 if UNEVALUABLE in (verdict.status, https.status) else 0


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
    track = state.get("https")
    if track is not None:
        words = [f"https status={track['status']}", f"since={track['since']}",
                 f"target={track['target']}", f"last_result={track['last_result']}",
                 f"last_reason={track['last_reason']}",
                 f"unevaluable_streak={track['unevaluable_streak']}", f"owed={len(track['owed'])}"]
        if track["candidate"]:
            words.append(f"candidate={track['candidate']}x{track['candidate_streak']}")
        counts = track["last_counts"]
        if counts:
            words.append(f"reached={counts['reached']}/{counts['probes']} asns={counts['asns']}")
        print(" ".join(words))
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
    parser.add_argument("--https-host",
                        help=f"再用同一批探针 GET https://<它>{HTTPS_PATH}(要 200、证书验过);"
                             f"每轮多用 {PROBES} 个测试额度")
    args = parser.parse_args(argv)
    if args.status:
        if args.target is not None:
            parser.error("--status 不用 --target")
        if args.https_host is not None:
            parser.error("--status 不用 --https-host")
        return _status(base, clock())
    if args.target is None or not _TARGET.fullmatch(args.target):
        parser.error("--target 要一个公网 IP 或域名(不带协议、端口、路径)")
    if args.https_host is not None and (not _TARGET.fullmatch(args.https_host)
                                        or args.https_host.replace(".", "").isdigit()):
        parser.error("--https-host 要一个域名(SNI 和证书都按它;不带协议、端口、路径,不能是 IP)")
    if args.plan:
        return _plan(base, args.target, args.https_host)
    if args.check:
        return _check(args.target, args.https_host, api=api, sleep=sleep)
    return _cron_run(base, args.target, args.https_host, run=run, send=send, api=api,
                     sleep=sleep, clock=clock)


if __name__ == "__main__":
    raise SystemExit(main())
