"""NMU 国内探针：从国内的 Globalping 探针连 TCP 443，状态变了才说话、只说一次。

10-28 起国内到 box A 整个 IP 的 TCP 被封，本机 nmu-health、纽约探针、Heartbeat 都从美国看，
一直是绿的。deploy/offbox/nmu_china_probe.py 从国内看；这里每条测试钉它文档串里的一条规则。
Globalping、Discord、keychain-get 全是假的，整份测试不联网。
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import stat
import subprocess
import sys
from types import SimpleNamespace
import urllib.error

import pytest

ROOT = Path(__file__).resolve().parents[1]
OFFBOX = ROOT / "deploy" / "offbox"


def _load(name: str):
    # 用模块的真名登记：国内探针 import nmu_offbox_probe 时拿到的就是这一份。
    spec = importlib.util.spec_from_file_location(name, OFFBOX / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


offbox = _load("nmu_offbox_probe")
china = _load("nmu_china_probe")

TARGET = "203.0.113.7"          # TEST-NET-3，不是生产地址
WEBHOOK = "https://discord.com/api/webhooks/1490/sentinel-webhook-token"
T0 = 1_793_589_420              # 某个 :17；具体哪天无所谓
HOUR = 3600
QUOTA = "244/250"


def result(city: str, asn: int, rcv: int, *, total: int = 3, status: str = "finished",
           source: str | None = None, country: str = "CN") -> dict:
    probe = {"continent": "AS", "region": "Eastern Asia", "country": country, "state": None,
             "city": city, "asn": asn, "network": "Some Carrier", "tags": [], "resolvers": []}
    if status == "finished":
        outcome = {"status": "finished", "rawOutput": "", "resolvedAddress": TARGET,
                   "resolvedHostname": None,
                   "stats": {"min": None, "avg": None, "max": None, "total": total, "rcv": rcv,
                             "drop": total - rcv, "loss": 100 if total and not rcv else 0},
                   "timings": [{"rtt": 141.2}] * rcv}
    else:
        outcome = {"status": status, "rawOutput": ""}
        if source is not None:
            outcome["failureSource"] = source
    return {"probe": probe, "result": outcome}


CN = [("Nanjing", 4134), ("Guangzhou", 4134), ("Wuhan", 4837), ("Zhenjiang", 9808),
      ("Shenzhen", 37963), ("Hangzhou", 45090)]
OPEN = [result(city, asn, 3) for city, asn in CN]
BLOCKED = [result(city, asn, 0) for city, asn in CN]      # 10-28 那样：握手全部超时
HALF = [result(city, asn, 3 if index < 3 else 0) for index, (city, asn) in enumerate(CN)]
RATE_LIMITED = (429, {"error": {"type": "rate_limit_exceeded",
                                "message": "API rate limit exceeded."}}, "0/250", None)

API_TROUBLE = {
    "rate_limited": ({"create": RATE_LIMITED}, "api_429"),
    "server_error": ({"create": (503, None, None, None)}, "api_503"),
    "no_probes": ({"create": (422, {"error": {"type": "no_probes_found",
                                              "message": "No matching IPv4 probes available."}},
                              None, None)}, "api_422"),
    "unreachable": ({"create": (None, None, None, "err=TimeoutError")}, "api_err=TimeoutError"),
    "garbled": ({"create": (202, None, None, None)}, "api_bad_reply"),
    "fetch_502": ({"fetch": (502, None, None, None)}, "api_502"),
    "fetch_429": ({"fetch": (429, None, None, None)}, "api_429"),
    "never_finishes": ({"pending": 10 ** 6}, "poll_timeout"),
    "too_few_probes": ({"results": OPEN[:3]}, "coverage"),
    "one_asn_only": ({"results": [result(city, 4134, 0) for city, _ in CN]}, "coverage"),
}

HTTPS_HOST = "203-0-113-7.sslip.io"     # TEST-NET-3 的 sslip.io 名字，不是生产地址
HTTPS_FLAGS = ("--https-host", HTTPS_HOST)
HTTPS_QUOTA = "238/250"
TLS_RESET = "Client network socket disconnected before secure TLS connection was established"


def https_result(city: str, asn: int, *, code: int = 200, authorized: bool = True,
                 certificate: bool = True, status: str = "finished", source: str | None = None,
                 raw: str = TLS_RESET, country: str = "CN") -> dict:
    """字段照 spec 的 FinishedHttpTestResult / FailedTestResult。"""
    probe = result(city, asn, 0, country=country)["probe"]
    if status != "finished":
        outcome = {"status": status, "rawOutput": raw}
        if source is not None:
            outcome["failureSource"] = source
        return {"probe": probe, "result": outcome}
    tls = {"authorized": authorized, "protocol": "TLSv1.3", "cipherName": "TLS_AES_128_GCM_SHA256",
           "createdAt": "2026-11-06T02:00:00.000Z", "expiresAt": "2027-02-04T02:00:00.000Z",
           "subject": {"CN": HTTPS_HOST, "alt": f"DNS:{HTTPS_HOST}"},
           "issuer": {"C": "US", "O": "Let's Encrypt", "CN": "E7"}, "keyType": "EC",
           "keyBits": 256, "serialNumber": "05:A1", "fingerprint256": "AB:CD", "publicKey": None}
    if not authorized:
        tls["error"] = "ERR_TLS_CERT_ALTNAME_INVALID"
    return {"probe": probe, "result": {
        "status": "finished", "rawOutput": f"HTTP/1.1 {code}", "rawHeaders": "", "rawBody": "1",
        "truncated": False, "headers": {}, "statusCode": code, "statusCodeName": "OK",
        "resolvedAddress": TARGET,
        "timings": {"total": 1800, "dns": 40, "tcp": 300, "tls": 610, "firstByte": 840,
                    "download": 1},
        "tls": tls if certificate else None}}


HTTPS_OK = [https_result(city, asn) for city, asn in CN]
HTTPS_BROKEN = {
    # 握手到一半被 RST：按 SNI 封域名、DNS 被污染指到别处、droplet → box A 断，探针看到的都是这样
    "tls_reset": ([https_result(city, asn, status="failed", source="target") for city, asn in CN],
                  "(连不上、超时或 TLS 断了)"),
    # 探针把 NXDOMAIN 记成 target，不是 resolver
    "nxdomain": ([https_result(city, asn, status="failed", source="target",
                               raw=f"ENOTFOUND {HTTPS_HOST}") for city, asn in CN],
                 "(连不上、超时或 TLS 断了)"),
    "dns": ([https_result(city, asn, status="failed", source="resolver",
                          raw=f"queryA ESERVFAIL {HTTPS_HOST}") for city, asn in CN],
            "(DNS 解析失败)"),
    "forbidden": ([https_result(city, asn, code=403) for city, asn in CN], "(HTTP 403)"),
    "bad_gateway": ([https_result(city, asn, code=502) for city, asn in CN], "(HTTP 502)"),
    "redirect": ([https_result(city, asn, code=301) for city, asn in CN], "(HTTP 301)"),
    "cert_not_verified": ([https_result(city, asn, authorized=False) for city, asn in CN],
                          "(证书没验过)"),
    "no_certificate": ([https_result(city, asn, certificate=False) for city, asn in CN],
                       "(证书没验过)"),
}


class Globalping:
    """假 Globalping：POST 回一个新测量 id，GET 先回 in-progress 再回结果；记下每次请求。
    ping 测量回 results，http 测量回 https；https_create / https_fetch 只管 http 那一次。"""

    def __init__(self) -> None:
        self.results = OPEN
        self.https = HTTPS_OK
        self.create = None          # None = 照常 202；否则原样回这个 Reply
        self.fetch = None
        self.https_create = None
        self.https_fetch = None
        self.pending = 1            # 每个测量的前几次 GET 还在 in-progress
        self.serial = 0
        self.polls = 0
        self.types: dict[int, str] = {}
        self.calls: list[tuple] = []

    def set(self, *, create=None, fetch=None, pending: int = 1, results=None,
            https_create=None, https_fetch=None, https=None) -> None:
        self.create, self.fetch, self.pending = create, fetch, pending
        self.https_create, self.https_fetch = https_create, https_fetch
        if results is not None:
            self.results = results
        if https is not None:
            self.https = https

    def __call__(self, method: str, url: str, body):
        self.calls.append((method, url, copy.deepcopy(body)))
        if method == "POST":
            assert url == f"{china.API}/measurements"
            http = body["type"] == "http"
            create = self.https_create if http else self.create
            if create is not None:
                return china.Reply(*create)
            self.serial += 1
            self.polls = 0
            self.types[self.serial] = body["type"]
            return china.Reply(202, {"id": f"m{self.serial}",
                                     "probesCount": len(self.https if http else self.results)},
                               HTTPS_QUOTA if http else QUOTA, None)
        assert method == "GET" and body is None
        assert url == f"{china.API}/measurements/m{self.serial}"
        http = self.types[self.serial] == "http"
        fetch = self.https_fetch if http else self.fetch
        if fetch is not None:
            return china.Reply(*fetch)
        results = self.https if http else self.results
        self.polls += 1
        if self.polls <= self.pending:
            running = [{"probe": item["probe"], "result": {"status": "in-progress", "rawOutput": ""}}
                       for item in results]
            return china.Reply(200, {"id": f"m{self.serial}", "status": "in-progress",
                                     "results": running}, None, None)
        return china.Reply(200, {"id": f"m{self.serial}", "type": self.types[self.serial],
                                 "status": "finished", "results": copy.deepcopy(results)},
                           None, None)

    def posts(self) -> int:
        return sum(1 for method, _, _ in self.calls if method == "POST")


class Keychain:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.reply = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"protocolVersion": 1, "values": {offbox.WEBHOOK_SECRET: WEBHOOK}}) + "\n")

    def __call__(self, argv, **_kwargs):
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        assert argv == [str(offbox.KEYCHAIN_GET), offbox.WEBHOOK_SECRET], \
            f"国内探针只该跑 keychain-get：{argv}"
        return self.reply


class Outbox:
    """假 Discord：按剧本成败；delivered 只记真送到的。"""

    def __init__(self, *outcomes: bool) -> None:
        self.outcomes = list(outcomes)
        self.tries: list[tuple[str, dict]] = []
        self.delivered: list[tuple[str, dict]] = []

    def __call__(self, url: str, payload: dict) -> tuple[bool, str]:
        self.tries.append((url, payload))
        if self.outcomes and not self.outcomes.pop(0):
            return False, "http_503"
        self.delivered.append((url, payload))
        return True, "status=204"


def kind_of(payload: dict) -> str:
    heads = {**china.HEADLINES, **{f"https_{kind}": head
                                   for kind, head in china.HTTPS_HEADLINES.items()}}
    found = [kind for kind, head in heads.items() if head in payload["content"]]
    assert len(found) == 1, payload["content"]
    return found[0]


class World:
    """一个探针目录 + 假 Globalping + 假 Discord + 假时钟；tick() 就是 cron 跑一轮。"""

    def __init__(self, home: Path) -> None:
        self.base = home / ".openclaw" / "workspace" / "nmu-probe"
        self.base.mkdir(parents=True)
        self.base.chmod(0o700)
        self.gp = Globalping()
        self.keychain = Keychain()
        self.outbox = Outbox()
        self.now = T0
        self.slept: list[float] = []
        self.flags: tuple[str, ...] = ()        # cron 那一行 --target 之后还带什么

    def run(self, *argv: str, advance: bool = True) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = china.main(list(argv), base=self.base, run=self.keychain, send=self.outbox,
                              api=self.gp, sleep=self.slept.append, clock=lambda: self.now)
        if advance:
            self.now += HOUR
        return code, out.getvalue(), err.getvalue()

    def tick(self, results=None, https=None) -> dict:
        if results is not None:
            self.gp.results = results
        if https is not None:
            self.gp.https = https
        code, out, err = self.run("--target", TARGET, *self.flags)
        # cron 那一轮什么都不往 stdout/stderr 写：cron.log 只该收 traceback。
        assert (code, out, err) == (0, "", "")
        return self.state()

    def state(self) -> dict:
        return json.loads((self.base / "china-state.json").read_text(encoding="utf-8"))

    def kinds(self) -> list[str]:
        return [kind_of(payload) for _, payload in self.outbox.delivered]

    def content(self, index: int) -> str:
        return self.outbox.delivered[index][1]["content"]

    def log_lines(self) -> list[str]:
        return (self.base / "china.log").read_text(encoding="utf-8").splitlines()

    def silence_until(self, value) -> None:
        (self.base / "silence-until").write_text(str(value), encoding="utf-8")


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def _iso(ts: float) -> str:
    return china._iso(ts)


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%m-%d %H:%M")


# ---------------------------------------------------------------------------
# 一轮怎么测、怎么判
# ---------------------------------------------------------------------------

def test_an_open_run_sends_nothing_and_records_the_counts(world):
    state = world.tick(OPEN)

    assert state["status"] == "up" and state["last_result"] == "up"
    assert state["last_counts"] == {"reached": 6, "probes": 6, "asns": 5, "blocked": []}
    assert state["last_quota"] == QUOTA and state["target"] == TARGET
    assert world.outbox.tries == [] and world.keychain.calls == []
    assert world.log_lines() == [f"{_iso(T0)} UP result=up ok reached=6/6 asns=5 "
                                 f"quota={QUOTA} m=m1"]


def test_the_request_names_only_the_target_and_asks_china_for_tcp_443(world):
    world.tick(OPEN)

    post, *gets = world.gp.calls
    assert post == ("POST", "https://api.globalping.io/v1/measurements", {
        "type": "ping", "target": TARGET, "locations": [{"country": "CN", "limit": 6}],
        "measurementOptions": {"protocol": "TCP", "port": 443, "packets": 3}})
    # 一次 in-progress、一次跑完；Globalping 要求同一个测量两次 GET 至少隔 500 ms
    assert [call[:2] for call in gets] == [("GET", "https://api.globalping.io/v1/measurements/m1")] * 2
    assert world.slept == [china.POLL_GAP_S] * 2 and china.POLL_GAP_S >= 0.5


def test_one_blocked_run_flips_nothing_and_the_next_open_run_forgets_it(world):
    world.tick(OPEN)
    state = world.tick(BLOCKED)

    assert state["status"] == "up"
    assert (state["candidate"], state["candidate_streak"]) == ("down", 1)
    assert world.outbox.tries == [] and world.keychain.calls == []
    assert world.log_lines()[-1].endswith(f"candidate=downx1 quota={QUOTA} m=m2")

    state = world.tick(OPEN)
    assert state["candidate"] is None and state["candidate_streak"] == 0


def test_only_real_answers_from_chinese_probes_count():
    results = [
        result("Nan<@>jing\n", 4134, 0),
        result("Wuhan", 4837, 3),
        result("Xian", 4134, 0, status="failed", source="target"),
        result("Beijing", 37963, 0, status="failed"),               # 没给 failureSource：算不通
        result("Shanghai", 4812, 0, status="failed", source="internal"),
        result("Guangzhou", 45090, 0, status="offline"),
        result("Hefei", 4134, 0, status="in-progress"),
        result("Hong Kong", 4760, 3, country="HK"),
        result("Zhengzhou", 24445, 0, total=0),
        {"probe": {"country": "CN", "city": "Nowhere"}, "result": OPEN[0]["result"]},
        "not a result",
    ]

    verdict = china.classify(results)

    assert (verdict.status, verdict.reached, verdict.probes, verdict.asns) == ("partial", 1, 4, 3)
    assert verdict.blocked == ["Nanjing AS4134", "Xian AS4134", "Beijing AS37963"]


# ---------------------------------------------------------------------------
# 验收 1-2：全超时报一次 DOWN 然后不出声；恢复报一次
# ---------------------------------------------------------------------------

def test_all_probes_timing_out_sends_exactly_one_down_then_stays_quiet(world):
    world.tick(BLOCKED)                 # T0
    state = world.tick(BLOCKED)         # T0+1h 第二轮才判 down

    assert state["status"] == "down" and state["since"] == _iso(T0)
    assert world.kinds() == ["down"]
    payload = world.outbox.delivered[0][1]
    content = payload["content"]
    assert content.startswith(f"<@{offbox.MENTION}> ")
    assert payload["allowed_mentions"] == {"users": [offbox.MENTION]}
    assert "从国内看打不开" in content and "0/6 个探针" in content and "5 个 ASN" in content
    assert TARGET in content and _day(T0) in content and "Nanjing AS4134" in content
    assert state["owed"] == []

    for _ in range(6):
        state = world.tick(BLOCKED)
    assert world.kinds() == ["down"] and state["status"] == "down"
    assert len(world.outbox.tries) == 1


def test_recovery_sends_exactly_one_up_with_the_outage_length(world):
    world.tick(BLOCKED)                 # T0 开始
    world.tick(BLOCKED)                 # T0+1h 判 down
    world.tick(OPEN)                    # T0+2h 第一次全通
    state = world.tick(OPEN)            # T0+3h 判 up

    assert world.kinds() == ["down", "up"]
    payload = world.outbox.delivered[1][1]
    assert "6/6 个探针" in payload["content"] and "持续约 2 小时" in payload["content"]
    assert "<@" not in payload["content"] and payload["allowed_mentions"] == {"parse": []}
    assert state["status"] == "up" and state["since"] == _iso(T0 + 2 * HOUR)
    assert state["outage_since"] is None

    for _ in range(3):
        world.tick(OPEN)
    assert world.kinds() == ["down", "up"]


def test_partial_is_its_own_state_and_only_a_worse_state_mentions_eric(world):
    world.tick(HALF)
    state = world.tick(HALF)

    assert state["status"] == "partial" and world.kinds() == ["partial"]
    first = world.content(0)
    assert first.startswith("<@") and "3/6 个探针" in first
    assert "Zhenjiang AS9808、Shenzhen AS37963、Hangzhou AS45090" in first
    assert "Nanjing" not in first

    world.tick(BLOCKED)
    world.tick(BLOCKED)                 # 更坏：@
    world.tick(HALF)
    world.tick(HALF)                    # 好转：不 @
    world.tick(OPEN)
    state = world.tick(OPEN)

    assert world.kinds() == ["partial", "down", "partial", "up"]
    assert world.content(1).startswith("<@")
    assert "<@" not in world.content(2) and "<@" not in world.content(3)
    assert "持续约 6 小时" in world.content(3)          # T0 → T0+6h 第一次全通
    assert state["status"] == "up" and state["owed"] == []


# ---------------------------------------------------------------------------
# 验收 3：API 出错、429、5xx 一律 unevaluable，永远翻不了状态
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("trouble", sorted(API_TROUBLE))
def test_api_trouble_is_unevaluable_and_never_flips_the_state(world, trouble):
    setup, reason = API_TROUBLE[trouble]
    world.tick(OPEN)
    world.gp.set(**setup)
    for _ in range(3):
        state = world.tick()

    assert state["status"] == "up" and state["candidate"] is None
    assert (state["last_result"], state["last_reason"]) == ("unevaluable", reason)
    assert state["unevaluable_streak"] == 3 and state["summary_24h"]["unevaluable"] == 3
    assert world.outbox.tries == [] and world.keychain.calls == []

    # 已经确认的 down 也一样：出错的几轮不会被当成「通了」
    world.gp.set(results=BLOCKED)
    world.tick()
    world.tick()
    world.gp.set(**setup)
    for _ in range(3):
        state = world.tick()
    assert state["status"] == "down" and world.kinds() == ["down"]
    assert state["unevaluable_streak"] == 3


def test_an_unevaluable_run_neither_confirms_nor_breaks_a_streak(world):
    world.tick(BLOCKED)
    world.gp.set(create=RATE_LIMITED)
    state = world.tick()

    assert state["status"] == "up" and state["candidate_streak"] == 1
    assert state["last_quota"] == "0/250"
    assert world.outbox.tries == []

    world.gp.set()
    state = world.tick(BLOCKED)
    assert state["status"] == "down" and state["since"] == _iso(T0)
    assert world.kinds() == ["down"]


# ---------------------------------------------------------------------------
# 验收 4：静默窗口（与纽约探针共用同一份 silence-until）
# ---------------------------------------------------------------------------

def test_the_silence_file_is_the_one_the_new_york_probe_reads():
    assert china.DIR == offbox.DIR
    assert china.SILENCE_FILE == offbox.SILENCE_FILE == "silence-until"
    assert china.STATE_FILE != offbox.STATE_FILE and china.LOG_FILE != offbox.LOG_FILE


def test_an_outage_that_starts_and_ends_inside_the_silence_sends_nothing(world):
    world.silence_until(T0 + 5 * HOUR)
    for shape in (BLOCKED, BLOCKED, BLOCKED, OPEN, OPEN):      # T0 … T0+4h
        state = world.tick(shape)

    assert state["status"] == "up" and state["owed"] == []
    world.tick(OPEN)                                            # 窗口已过
    assert world.outbox.tries == [] and world.keychain.calls == []
    assert all(line.endswith("silenced") for line in world.log_lines()[:5])


def test_still_down_after_the_silence_is_reported_late_from_the_real_start(world):
    world.silence_until(T0 + 2 * HOUR + 60)
    for _ in range(3):                  # T0、T0+1h（判 down）、T0+2h 都在窗口里
        state = world.tick(BLOCKED)
    assert state["status"] == "down" and world.outbox.tries == []

    state = world.tick(BLOCKED)         # T0+3h 窗口已过：补报
    assert world.kinds() == ["down"] and state["since"] == _iso(T0)
    assert _day(T0) in world.content(0)


def test_the_late_down_does_not_wait_for_an_evaluable_run(world):
    world.silence_until(T0 + HOUR + 60)
    world.tick(BLOCKED)
    world.tick(BLOCKED)                 # 窗口里判 down，不发
    world.gp.set(create=RATE_LIMITED)
    world.tick()                        # 窗口已过，这一轮 429：照样补报已确认的 down

    assert world.kinds() == ["down"]


def test_a_recovery_from_an_announced_down_is_sent_even_while_silenced(world):
    world.tick(BLOCKED)
    world.tick(BLOCKED)
    world.silence_until(world.now + 5 * HOUR)
    world.tick(OPEN)
    world.tick(OPEN)

    assert world.kinds() == ["down", "up"]


@pytest.mark.parametrize("text", ["tomorrow", str(T0 + 3 * offbox.DAY_S), str(T0 - HOUR)])
def test_a_silence_file_that_is_garbage_too_far_out_or_past_is_ignored(world, text):
    world.silence_until(text)
    world.tick(BLOCKED)
    world.tick(BLOCKED)

    assert world.kinds() == ["down"]


# ---------------------------------------------------------------------------
# 发送：欠着的下一轮补发，不重复；没发出去就恢复的合成一条
# ---------------------------------------------------------------------------

def test_a_failed_delivery_stays_owed_and_is_retried_without_duplicates(world):
    world.outbox = Outbox(False, False)         # 判 down 那一轮两次都发不出去
    world.tick(BLOCKED)
    state = world.tick(BLOCKED)

    assert world.outbox.delivered == [] and [m["kind"] for m in state["owed"]] == ["down"]
    assert len(world.outbox.tries) == 2 and world.slept[-1] == offbox.POST_GAP_S
    assert "owed=DOWN" in world.log_lines()[-1] and "send_error=http_503" in world.log_lines()[-1]

    state = world.tick(BLOCKED)
    assert world.kinds() == ["down"] and state["owed"] == []
    world.tick(BLOCKED)
    assert world.kinds() == ["down"] and len(world.outbox.tries) == 3


def test_an_undelivered_down_then_recovery_becomes_one_combined_message(world):
    world.outbox = Outbox(False, False, False, False)
    world.tick(BLOCKED)
    world.tick(BLOCKED)                 # T0+1h 判 down，没发出去
    world.tick(OPEN)                    # T0+2h 还是没发出去
    state = world.tick(OPEN)            # T0+3h 判 up：合成一条补报

    assert world.kinds() == ["combined"]
    content = world.content(0)
    assert "最坏时全部连不上" in content and "持续约 2 小时" in content and "没发出去" in content
    assert "<@" not in content
    assert state["owed"] == [] and state["status"] == "up"


def test_a_combined_recovery_names_the_worst_of_several_unsent_states(world):
    working = world.keychain.reply
    world.keychain.reply = SimpleNamespace(returncode=1, stderr="no such item", stdout="")
    world.tick(HALF)
    world.tick(HALF)                    # 判 partial,发不出去
    world.tick(BLOCKED)
    state = world.tick(BLOCKED)         # 再判 down,也发不出去
    assert [m["kind"] for m in state["owed"]] == ["partial", "down"]
    world.tick(OPEN)
    world.keychain.reply = working
    state = world.tick(OPEN)            # 判 up:合成一条,报最坏的那一档

    assert world.kinds() == ["combined"]
    assert "最坏时全部连不上" in world.content(0)
    assert state["owed"] == [] and state["status"] == "up"


def test_a_recovery_after_an_announced_partial_is_a_plain_up_not_a_combined_message(world):
    working = world.keychain.reply
    world.tick(HALF)
    world.tick(HALF)                    # 判 partial,发出去了
    world.keychain.reply = SimpleNamespace(returncode=1, stderr="no such item", stdout="")
    world.tick(BLOCKED)
    world.tick(BLOCKED)                 # 判 down,没发出去(它的 from 是 partial)
    world.tick(OPEN)
    world.keychain.reply = working
    state = world.tick(OPEN)

    assert world.kinds() == ["partial", "up"]
    assert state["owed"] == [] and state["status"] == "up"


def test_a_corrupt_gzip_body_is_unparseable_not_a_crash():
    body = gzip.compress(b'{"id": "m1"}')
    corrupt = body[:10] + bytes([0xFF]) * 12 + body[-8:]
    reply = china._reply(200, {"Content-Encoding": "gzip"}, corrupt)
    assert reply.code == 200 and reply.doc is None


def test_a_missing_webhook_keeps_the_message_owed(world):
    world.keychain.reply = SimpleNamespace(returncode=1, stderr="no such item", stdout="")
    world.tick(BLOCKED)
    state = world.tick(BLOCKED)

    assert [m["kind"] for m in state["owed"]] == ["down"] and world.outbox.tries == []
    assert world.log_lines()[-1].endswith("owed=DOWN send_error=keychain_get_rc=1")


def test_the_webhook_never_lands_in_the_state_or_the_log(world):
    world.tick(BLOCKED)
    world.tick(BLOCKED)

    assert world.kinds() == ["down"]
    written = "".join(path.read_text(encoding="utf-8") for path in world.base.iterdir())
    assert "sentinel-webhook-token" not in written and "discord.com/api/webhooks" not in written


# ---------------------------------------------------------------------------
# 状态文件
# ---------------------------------------------------------------------------

def test_state_and_log_are_private_and_a_broken_state_starts_over_as_up(world):
    world.tick(BLOCKED)
    path = world.base / "china-state.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((world.base / "china.log").stat().st_mode) == 0o600

    path.write_text("{half a file", encoding="utf-8")
    state = world.tick(BLOCKED)
    assert state["status"] == "up" and state["candidate_streak"] == 1

    # 形状对、值不自洽（down 却没有 outage_since）也当坏的：不然会发出没头没尾的「已恢复」
    half = china.fresh_state()
    half["status"] = half["announced"] = "down"
    path.write_text(json.dumps(half), encoding="utf-8")
    state = world.tick(OPEN)
    assert state["status"] == "up" and world.outbox.tries == []


# ---------------------------------------------------------------------------
# 验收 5：--plan / --dry-run 不联网；其余几种模式
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flag", ["--plan", "--dry-run"])
def test_plan_and_dry_run_touch_neither_the_network_nor_any_file(world, monkeypatch, flag):
    attempts = []

    def refuse(*args, **_kwargs):
        attempts.append(args)
        raise AssertionError("--plan 不该联网")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(china.urllib.request, "urlopen", refuse)
    out = io.StringIO()
    with redirect_stdout(out):
        code = china.main([flag, "--target", TARGET], base=world.base, run=refuse, send=refuse,
                          api=refuse, sleep=refuse)

    assert code == 0 and attempts == []
    text = out.getvalue()
    assert json.dumps(china.request_body(TARGET), ensure_ascii=False) in text
    assert china.SCHEDULE in text and "250" in text and "silence-until" in text
    assert list(world.base.iterdir()) == []


def test_plan_runs_as_cron_would_start_it_with_only_the_standard_library():
    # cron 用 python3 -B 起：脚本目录在 sys.path[0]，import nmu_offbox_probe 才找得到。
    done = subprocess.run([sys.executable, "-B", str(OFFBOX / "nmu_china_probe.py"),
                           "--plan", "--target", TARGET], capture_output=True, text=True,
                          timeout=30)

    assert done.returncode == 0, done.stderr
    assert '"target": "203.0.113.7"' in done.stdout


def test_check_measures_once_prints_the_verdict_and_writes_nothing(world):
    world.gp.results = BLOCKED
    code, out, err = world.run("--check", "--target", TARGET)

    assert code == 0 and err == ""
    first, second = out.splitlines()
    assert first == f"result=down reason=blocked reached=0/6 asns=5 measurement=m1 quota={QUOTA}"
    assert second.startswith("连不上的:Nanjing AS4134、Guangzhou AS4134")
    assert world.gp.posts() == 1
    assert world.outbox.tries == [] and world.keychain.calls == []
    assert list(world.base.iterdir()) == []


def test_check_exits_nonzero_when_it_cannot_tell(world):
    world.gp.set(create=RATE_LIMITED)
    code, out, _ = world.run("--check", "--target", TARGET)

    assert code == 1 and out.startswith("result=unevaluable reason=api_429")


def test_status_reads_the_state_without_any_network(world):
    code, out, _ = world.run("--status", advance=False)
    assert code == 0 and "state=missing" in out

    world.tick(BLOCKED)
    world.tick(BLOCKED)
    calls = len(world.gp.calls)
    code, out, _ = world.run("--status", advance=False)

    assert code == 0 and len(world.gp.calls) == calls
    first, second = out.splitlines()
    assert first.startswith(f"status=down since={_iso(T0)} target={TARGET} ")
    assert "owed=0" in first and "silence=none" in first and "reached=0/6 asns=5" in first
    assert second == "24h runs=2 up=0 partial=0 down=2 unevaluable=0"

    (world.base / "china-state.json").write_text("[]", encoding="utf-8")
    code, out, _ = world.run("--status", advance=False)
    assert code == 1 and "state=corrupt" in out


@pytest.mark.parametrize("target", ["https://203.0.113.7/health", "203.0.113.7:443",
                                    "203.0.113.7/health", "host.example?token=abc",
                                    "localhost", "-bad.example", "a b.example", ""])
def test_the_target_must_be_a_bare_public_host_or_ip(world, target):
    with pytest.raises(SystemExit) as raised:
        world.run("--target", target)

    assert raised.value.code == 2 and world.gp.calls == []


@pytest.mark.parametrize("argv", [[], ["--check"], ["--plan"], ["--status", "--target", TARGET]])
def test_target_is_required_except_for_status(world, argv):
    with pytest.raises(SystemExit) as raised:
        world.run(*argv)

    assert raised.value.code == 2 and world.gp.calls == []


# ---------------------------------------------------------------------------
# 真正的 HTTP 那一层（urlopen 换成假的）
# ---------------------------------------------------------------------------

def test_the_http_client_identifies_itself_and_reads_gzip_and_the_quota(monkeypatch):
    seen = []

    class Response:
        status = 202
        headers = {"Content-Encoding": "gzip", "X-RateLimit-Remaining": "244",
                   "X-RateLimit-Limit": "250"}

        def read(self):
            return gzip.compress(b'{"id": "m1", "probesCount": 6}')

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def fake_urlopen(request, timeout, context):
        seen.append((request, timeout))
        return Response()

    monkeypatch.setattr(china.urllib.request, "urlopen", fake_urlopen)
    reply = china._call("POST", f"{china.API}/measurements", china.request_body(TARGET))

    assert reply == china.Reply(202, {"id": "m1", "probesCount": 6}, "244/250", None)
    [(request, timeout)] = seen
    assert request.get_method() == "POST" and timeout == china.HTTP_TIMEOUT_S
    assert request.get_header("User-agent") == china.UA
    assert request.get_header("Accept-encoding") == "gzip"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == china.request_body(TARGET)


def test_http_errors_keep_their_code_and_a_dead_network_is_named(monkeypatch):
    def rate_limited(request, timeout, context):
        raise urllib.error.HTTPError(
            request.full_url, 429, "Too Many Requests",
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "250"},
            io.BytesIO(b'{"error": {"type": "rate_limit_exceeded"}}'))

    monkeypatch.setattr(china.urllib.request, "urlopen", rate_limited)
    assert china._call("POST", f"{china.API}/measurements", {}) == china.Reply(
        429, {"error": {"type": "rate_limit_exceeded"}}, "0/250", None)

    def dead(request, timeout, context):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(china.urllib.request, "urlopen", dead)
    assert china._call("GET", f"{china.API}/measurements/m1", None) == china.Reply(
        None, None, None, "err=TimeoutError")


# ---------------------------------------------------------------------------
# --https-host：同一批探针再 GET 一次 /build-id.txt，抓「TCP 通但 HTTPS 不通」
# ---------------------------------------------------------------------------

def _gap_run(world, https, rounds: int = 2) -> dict:
    world.flags = HTTPS_FLAGS
    for _ in range(rounds):
        state = world.tick(OPEN, https)
    return state


def test_https_reuses_the_tcp_probes_and_gets_the_static_file_not_health(world):
    world.flags = HTTPS_FLAGS
    state = world.tick(OPEN, HTTPS_OK)

    ping, http = [body for method, _, body in world.gp.calls if method == "POST"]
    assert ping == china.request_body(TARGET)
    # locations 是 TCP 那次的 id：同一批探针；不打 /health，免得喂绿 box A 上的机外死人开关
    assert http == {"type": "http", "target": HTTPS_HOST, "locations": "m1",
                    "measurementOptions": {"protocol": "HTTPS", "port": 443,
                                           "request": {"method": "GET", "path": "/build-id.txt"}}}
    assert [url for method, url, _ in world.gp.calls if method == "GET"] == \
        [f"{china.API}/measurements/m1"] * 2 + [f"{china.API}/measurements/m2"] * 2
    assert state["status"] == "up" and state["https"]["status"] == "up"
    assert state["https"]["last_counts"] == {"reached": 6, "probes": 6, "asns": 5, "blocked": []}
    assert world.outbox.tries == [] and world.keychain.calls == []
    assert world.log_lines() == [
        f"{_iso(T0)} UP result=up ok reached=6/6 asns=5 quota={QUOTA} m=m1 https=UP "
        f"https_result=up ok https_reached=6/6 https_asns=5 https_quota={HTTPS_QUOTA} https_m=m2"]


@pytest.mark.parametrize("style", sorted(HTTPS_BROKEN))
def test_tcp_open_but_https_broken_is_announced_once_with_the_likely_causes(world, style):
    broken, why = HTTPS_BROKEN[style]
    state = _gap_run(world, broken, rounds=1)
    assert state["https"]["candidate"] == "down" and world.outbox.tries == []

    state = world.tick(OPEN, broken)                   # T0+1h 第二轮才报

    assert world.kinds() == ["https_down"]
    assert state["status"] == "up" and state["candidate"] is None   # TCP 那条线不受影响
    assert state["https"]["status"] == "down" and state["https"]["since"] == _iso(T0)
    payload = world.outbox.delivered[0][1]
    content = payload["content"]
    assert content.startswith(f"<@{offbox.MENTION}> ")
    assert "TCP 通但 HTTPS 不通" in content and "0/6 个探针" in content and "5 个 ASN" in content
    assert f"<https://{HTTPS_HOST}/build-id.txt>" in content and _day(T0) in content
    assert f"Nanjing AS4134{why}" in content
    for cause in ("SNI", "DNS", "droplet → box A", "证书", "Caddy 没有这个地址", "解析器出错或超时"):
        assert cause in content
    assert world.log_lines()[-1].endswith("https_m=m4 sent=HTTPS_DOWN")

    for _ in range(3):
        world.tick(OPEN, broken)
    assert world.kinds() == ["https_down"]

    world.tick(OPEN, HTTPS_OK)
    state = world.tick(OPEN, HTTPS_OK)                 # 恢复也要连续两轮
    assert world.kinds() == ["https_down", "https_up"]
    recovered = world.content(1)
    assert "6/6 个探针" in recovered and "持续约 5 小时" in recovered and "<@" not in recovered
    assert state["https"]["status"] == "up" and state["https"]["outage_since"] is None


def test_a_mixed_https_set_counts_as_broken_and_names_only_the_failing_probes(world):
    mixed = [https_result(city, asn) for city, asn in CN[:3]] + [
        https_result("Zhenjiang", 9808, status="failed"),      # 没给 failureSource：算不通
        https_result("Shenzhen", 37963, status="failed", source="target"),
        https_result("Hangzhou", 45090, authorized=False)]
    _gap_run(world, mixed)

    assert world.kinds() == ["https_down"]
    content = world.content(0)
    assert "3/6 个探针" in content
    assert ("不通的:Zhenjiang AS9808(连不上、超时或 TLS 断了)、"
            "Shenzhen AS37963(连不上、超时或 TLS 断了)、Hangzhou AS45090(证书没验过)") in content
    assert "Nanjing" not in content


def test_a_thin_or_partial_https_probe_set_is_unevaluable_and_flips_nothing(world):
    thin = [https_result("Nanjing", 4134, status="failed", source="target"),
            https_result("Guangzhou", 4134, status="failed", source="internal"),
            https_result("Wuhan", 4837, status="offline", raw=""),
            https_result("Zhenjiang", 9808, status="in-progress", raw=""),
            https_result("Hong Kong", 4760, status="failed", source="target", country="HK"),
            {"probe": {"country": "CN", "city": "Nowhere"}, "result": HTTPS_OK[0]["result"]},
            "not a result"]
    verdict = china.classify(thin, judge=china._https_outcome)
    assert (verdict.status, verdict.reason, verdict.probes) == ("unevaluable", "coverage", 1)

    state = _gap_run(world, thin[:-1], rounds=3)
    assert state["https"]["status"] == "up" and state["https"]["candidate"] is None
    assert (state["https"]["last_result"], state["https"]["last_reason"]) == \
        ("unevaluable", "coverage")
    assert state["https"]["unevaluable_streak"] == 3 and world.outbox.tries == []


@pytest.mark.parametrize("shape", ["blocked", "half"])
def test_https_is_not_judged_when_tcp_itself_is_not_open(world, shape):
    tcp = {"blocked": BLOCKED, "half": HALF}[shape]
    world.flags = HTTPS_FLAGS
    for _ in range(3):
        state = world.tick(tcp, HTTPS_BROKEN["tls_reset"][0])

    # TCP 自己那条照报；HTTPS 那条不另报（不通是 TCP 不通带的）
    assert world.kinds() == [{"blocked": "down", "half": "partial"}[shape]]
    assert state["https"]["status"] == "up" and state["https"]["candidate"] is None
    reason = {"blocked": "tcp_down", "half": "tcp_partial"}[shape]
    assert (state["https"]["last_result"], state["https"]["last_reason"]) == \
        ("unevaluable", reason)


HTTPS_API_TROUBLE = {
    "https_429": ({"https_create": RATE_LIMITED}, "api_429", 2),
    "https_503": ({"https_create": (503, None, None, None)}, "api_503", 2),
    "https_fetch_502": ({"https_fetch": (502, None, None, None)}, "api_502", 2),
    # TCP 那次就 429：没有同一批探针可用，HTTPS 不测、不花额度
    "tcp_429": ({"create": RATE_LIMITED}, "tcp_unevaluable", 1),
}


@pytest.mark.parametrize("trouble", sorted(HTTPS_API_TROUBLE))
def test_https_api_trouble_is_unevaluable_and_never_flips_either_way(world, trouble):
    setup, reason, posts = HTTPS_API_TROUBLE[trouble]
    world.flags = HTTPS_FLAGS
    world.tick(OPEN, HTTPS_OK)
    world.gp.set(**setup)
    calls = len(world.gp.calls)
    for _ in range(3):
        state = world.tick()
    assert sum(1 for method, *_ in world.gp.calls[calls:] if method == "POST") == 3 * posts
    assert state["https"]["status"] == "up" and state["https"]["candidate"] is None
    assert (state["https"]["last_result"], state["https"]["last_reason"]) == \
        ("unevaluable", reason)
    assert world.outbox.tries == []

    # 已经报出去的「TCP 通但 HTTPS 不通」也一样：出错的几轮不会被当成「好了」
    world.gp.set(https=HTTPS_BROKEN["dns"][0])
    world.tick()
    world.tick()
    world.gp.set(**setup)
    for _ in range(3):
        state = world.tick()
    assert state["https"]["status"] == "down" and world.kinds() == ["https_down"]
    assert state["https"]["unevaluable_streak"] == 3


def test_https_follows_the_shared_silence_and_reports_late_from_the_real_start(world):
    world.silence_until(T0 + 2 * HOUR + 60)
    state = _gap_run(world, HTTPS_BROKEN["tls_reset"][0], rounds=3)    # T0 … T0+2h 都在窗口里
    assert state["https"]["status"] == "down" and world.outbox.tries == []

    world.tick(OPEN, HTTPS_BROKEN["tls_reset"][0])                       # 窗口已过：补报
    assert world.kinds() == ["https_down"] and _day(T0) in world.content(0)


def test_tcp_and_https_messages_of_one_round_go_out_tcp_first(world):
    world.flags = HTTPS_FLAGS
    world.tick(BLOCKED, HTTPS_OK)
    world.tick(BLOCKED, HTTPS_OK)                       # TCP 判 down
    world.tick(OPEN, HTTPS_BROKEN["forbidden"][0])
    state = world.tick(OPEN, HTTPS_BROKEN["forbidden"][0])   # TCP 恢复，同一轮 HTTPS 判 down

    assert world.kinds() == ["down", "up", "https_down"]
    assert world.log_lines()[-1].endswith("sent=UP,HTTPS_DOWN")
    assert state["owed"] == [] and state["https"]["owed"] == []


def test_an_undelivered_https_alert_stays_owed_and_a_quiet_recovery_is_combined(world):
    world.outbox = Outbox(False, False)
    state = _gap_run(world, HTTPS_BROKEN["dns"][0])
    assert [m["kind"] for m in state["https"]["owed"]] == ["down"] and state["owed"] == []
    assert world.log_lines()[-1].endswith("owed=HTTPS_DOWN send_error=http_503")

    world.outbox = Outbox(False, False)
    world.tick(OPEN, HTTPS_OK)                          # 还是没发出去
    world.outbox = Outbox()
    state = world.tick(OPEN, HTTPS_OK)                  # 判 up：合成一条补报
    assert world.kinds() == ["https_combined"]
    assert "当时 TCP 通但 HTTPS 不通" in world.content(0) and "没发出去" in world.content(0)
    assert state["https"]["owed"] == []


def test_a_v1_state_from_before_the_flag_loads_and_the_first_https_run_is_quiet(world):
    for shape in (BLOCKED, BLOCKED, OPEN, OPEN):       # 11-03 装上以后一直这样跑：没有 https
        world.tick(shape)
    before = world.state()
    assert before["version"] == 1 and "https" not in before
    assert world.kinds() == ["down", "up"]

    world.flags = HTTPS_FLAGS
    state = world.tick(OPEN, HTTPS_OK)

    assert world.kinds() == ["down", "up"]             # 升级后第一轮什么都不发
    kept = ("version", "target", "status", "since", "announced", "last_evaluated")
    assert {key: state[key] for key in kept} == {key: before[key] for key in kept} | {
        "last_evaluated": _iso(T0 + 4 * HOUR)}
    assert state["ring"][:-1] == before["ring"] and state["summary_24h"]["runs"] == 5
    assert state["https"] == {**china.fresh_https(), "target": HTTPS_HOST, "last_result": "up",
                              "last_reason": "ok",
                              "last_counts": {"reached": 6, "probes": 6, "asns": 5, "blocked": []}}


def test_upgrading_while_https_is_broken_still_waits_for_two_runs(world):
    world.tick(OPEN)
    world.tick(OPEN)
    state = _gap_run(world, HTTPS_BROKEN["cert_not_verified"][0], rounds=1)
    assert world.outbox.tries == [] and state["https"]["candidate_streak"] == 1

    world.tick(OPEN, HTTPS_BROKEN["cert_not_verified"][0])
    assert world.kinds() == ["https_down"]


def test_without_https_host_everything_is_byte_identical_to_before_the_flag(world, tmp_path):
    # 摘要是加 --https-host 之前那一版(main c051102)跑同一套剧本得到的。
    def sha(data) -> str:
        return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()

    for shape in (OPEN, BLOCKED, BLOCKED):
        world.tick(shape)
    world.gp.set(create=RATE_LIMITED)
    world.tick()
    world.gp.set()
    for shape in (HALF, HALF, OPEN, OPEN):
        world.tick(shape)
    assert "https" not in world.state()
    assert {body["type"] for method, _, body in world.gp.calls if method == "POST"} == {"ping"}
    payloads = [payload for _, payload in world.outbox.delivered]
    _, status, _ = world.run("--status", advance=False)
    fresh = World(tmp_path / "plan")
    _, plan, _ = fresh.run("--plan", "--target", TARGET)
    fresh.gp.results = HALF
    _, check, _ = fresh.run("--check", "--target", TARGET)

    assert {
        "state": sha((world.base / "china-state.json").read_bytes()),
        "log": sha((world.base / "china.log").read_bytes()),
        "payloads": sha(json.dumps(payloads, ensure_ascii=False, sort_keys=True)),
        "calls": sha(json.dumps(world.gp.calls, ensure_ascii=False, sort_keys=True)),
        "status": sha(status.replace(str(world.base), "<base>")),
        "plan": sha(plan.replace(str(fresh.base), "<base>")),
        "check": sha(check),
    } == {
        "state": "85c102a318e9a5d558d1bf747c5030c5f4e2f4f5c4ab36eb0d9bd4c9f01882df",
        "log": "99667429e744af743d2957204e615ca39d3e89f6d6d21503d6ed613404a10e96",
        "payloads": "3d961fdca75f952c9b0d646105c4a4ec1cb70436bff048145209d85490b51c9a",
        "calls": "4b13b7b0693576c3c571a34fdfbfe773f3ae543424eb2aaa30e9fa16478840ce",
        "status": "3fdfe9ae89954cfb76211fccc64fca4e74072fa824542e36a4784f3ba702dc19",
        "plan": "ec0bb77f3c45b26adb43f70f90386c2b521067215eb474fb977720a01c9af4e2",
        "check": "b83a2f8af58a6d72efc16f85e4068a9dba2c2085517b8b76daf30661a2b3e5b4",
    }


def test_a_broken_https_track_makes_the_whole_state_start_over(world):
    world.flags = HTTPS_FLAGS
    world.tick(BLOCKED, HTTPS_OK)
    path = world.base / "china-state.json"
    half = world.state()
    half["https"].update(status="down", announced="down")      # 没有 outage_since
    path.write_text(json.dumps(half), encoding="utf-8")

    state = world.tick(OPEN, HTTPS_OK)
    assert len(state["ring"]) == 1 and state["summary_24h"]["down"] == 0
    assert state["https"]["status"] == "up" and world.outbox.tries == []


def test_plan_with_https_host_prints_both_requests_and_the_rules_offline(world):
    code, out, _ = world.run("--plan", "--target", TARGET, *HTTPS_FLAGS)

    assert code == 0 and world.gp.calls == [] and list(world.base.iterdir()) == []
    assert json.dumps(china.request_body(TARGET), ensure_ascii=False) in out
    assert json.dumps(china.https_body(HTTPS_HOST, "<上面那次的 id>"), ensure_ascii=False) in out
    assert "tls.authorized=true" in out and "TCP 通但 HTTPS 不通" in out
    assert "每小时 12 个测试" in out


def test_check_with_https_host_measures_both_and_writes_nothing(world):
    world.gp.set(https=HTTPS_BROKEN["dns"][0])
    code, out, err = world.run("--check", "--target", TARGET, *HTTPS_FLAGS)

    assert code == 0 and err == ""
    first, second, third = out.splitlines()
    assert first == f"result=up reason=ok reached=6/6 asns=5 measurement=m1 quota={QUOTA}"
    assert second == (f"https result=down reason=blocked reached=0/6 asns=5 measurement=m2 "
                      f"quota={HTTPS_QUOTA} seen=down blocked")
    assert third.startswith("HTTPS 不通的:Nanjing AS4134(DNS 解析失败)、Guangzhou AS4134")
    assert world.gp.posts() == 2 and world.outbox.tries == []
    assert list(world.base.iterdir()) == []

    world.gp.set(https_create=RATE_LIMITED)
    code, out, _ = world.run("--check", "--target", TARGET, *HTTPS_FLAGS)
    assert code == 1 and "https result=unevaluable reason=api_429" in out


def test_status_shows_the_https_line_only_once_there_is_one(world):
    world.flags = HTTPS_FLAGS
    _gap_run(world, HTTPS_BROKEN["forbidden"][0])
    code, out, _ = world.run("--status", advance=False)

    assert code == 0
    first, second, third = out.splitlines()
    assert first.startswith("status=up ") and second.startswith("24h runs=2 ")
    assert third == (f"https status=down since={_iso(T0)} target={HTTPS_HOST} last_result=down "
                     f"last_reason=blocked unevaluable_streak=0 owed=0 reached=0/6 asns=5")


@pytest.mark.parametrize("argv", [
    ["--target", TARGET, "--https-host", "203.0.113.7"],
    ["--target", TARGET, "--https-host", f"https://{HTTPS_HOST}/build-id.txt"],
    ["--target", TARGET, "--https-host", f"{HTTPS_HOST}:443"],
    ["--target", TARGET, "--https-host", ""],
    ["--status", "--https-host", HTTPS_HOST],
])
def test_the_https_host_must_be_a_bare_domain(world, capsys, argv):
    with pytest.raises(SystemExit) as raised:
        china.main(argv, base=world.base, run=world.keychain, send=world.outbox, api=world.gp,
                   sleep=world.slept.append, clock=lambda: world.now)

    assert raised.value.code == 2 and world.gp.calls == []
    expected = "--status 不用 --https-host" if argv[0] == "--status" else "--https-host 要一个域名"
    assert expected in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 额度与 IP 切换
# ---------------------------------------------------------------------------

def test_the_schedule_fits_the_anonymous_quota_with_margin_and_cites_its_source():
    minute, hour, *_ = china.SCHEDULE.split()
    assert minute.isdigit() and hour == "*" and china.RUNS_PER_HOUR == 1
    # 避开纽约探针的 :x4 与 watchdog 的 :00/:05
    assert int(minute) % 10 != 4 and int(minute) not in (0, 5)
    assert china.PROBES * china.RUNS_PER_HOUR * 10 <= china.ANON_TESTS_PER_HOUR
    assert china.PROBES <= 50                      # 匿名单次最多 50 个探针
    assert china.PROBES >= china.MIN_PROBES and china.MIN_ASNS >= 2
    doc = china.__doc__
    assert "250" in doc and "https://api.globalping.io/v1/spec.yaml" in doc
    assert "https://globalping.io/credits" in doc and "X-RateLimit-Remaining" in doc


def test_https_doubles_the_tests_per_run_and_still_leaves_a_tenfold_margin(world):
    world.flags = HTTPS_FLAGS
    world.tick(OPEN, HTTPS_OK)

    posts = [body for method, _, body in world.gp.calls if method == "POST"]
    # http 那次复用 TCP 那次的探针：每次测量都是 PROBES 个测试
    assert [body["locations"] for body in posts] == [[{"country": "CN", "limit": china.PROBES}],
                                                     "m1"]
    per_hour = len(posts) * china.PROBES * china.RUNS_PER_HOUR
    assert per_hour == 12 and per_hour * 10 <= china.ANON_TESTS_PER_HOUR
    assert "6 × 2 = 12" in china.__doc__ and "12 × 10 = 120 ≤ 250" in china.__doc__
    assert "2026-11-05 读" in china.__doc__ and "tls.authorized" in china.__doc__


def test_the_script_does_not_carry_the_production_host():
    # IP 切换脚本按原样改写这几种写法；目标从 --target、--https-host 进来，这份文件切换时不用动。
    text = (OFFBOX / "nmu_china_probe.py").read_text(encoding="utf-8")
    assert "89-208-253-119" not in text and "89.208.253.119" not in text
    assert "147-182-176-237" not in text and "147.182.176.237" not in text
