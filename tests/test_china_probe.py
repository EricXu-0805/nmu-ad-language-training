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


class Globalping:
    """假 Globalping：POST 回一个新测量 id，GET 先回 in-progress 再回结果；记下每次请求。"""

    def __init__(self) -> None:
        self.results = OPEN
        self.create = None          # None = 照常 202；否则原样回这个 Reply
        self.fetch = None
        self.pending = 1            # 每个测量的前几次 GET 还在 in-progress
        self.serial = 0
        self.polls = 0
        self.calls: list[tuple] = []

    def set(self, *, create=None, fetch=None, pending: int = 1, results=None) -> None:
        self.create, self.fetch, self.pending = create, fetch, pending
        if results is not None:
            self.results = results

    def __call__(self, method: str, url: str, body):
        self.calls.append((method, url, copy.deepcopy(body)))
        if method == "POST":
            assert url == f"{china.API}/measurements"
            if self.create is not None:
                return china.Reply(*self.create)
            self.serial += 1
            self.polls = 0
            return china.Reply(202, {"id": f"m{self.serial}", "probesCount": len(self.results)},
                               QUOTA, None)
        assert method == "GET" and body is None
        assert url == f"{china.API}/measurements/m{self.serial}"
        if self.fetch is not None:
            return china.Reply(*self.fetch)
        self.polls += 1
        if self.polls <= self.pending:
            running = [{"probe": item["probe"], "result": {"status": "in-progress", "rawOutput": ""}}
                       for item in self.results]
            return china.Reply(200, {"id": f"m{self.serial}", "status": "in-progress",
                                     "results": running}, None, None)
        return china.Reply(200, {"id": f"m{self.serial}", "type": "ping", "status": "finished",
                                 "results": copy.deepcopy(self.results)}, None, None)

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
    found = [kind for kind, head in china.HEADLINES.items() if head in payload["content"]]
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

    def run(self, *argv: str, advance: bool = True) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = china.main(list(argv), base=self.base, run=self.keychain, send=self.outbox,
                              api=self.gp, sleep=self.slept.append, clock=lambda: self.now)
        if advance:
            self.now += HOUR
        return code, out.getvalue(), err.getvalue()

    def tick(self, results=None) -> dict:
        if results is not None:
            self.gp.results = results
        code, out, err = self.run("--target", TARGET)
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


def test_the_script_does_not_carry_the_production_host():
    # IP 切换脚本按原样改写这两种写法；目标从 --target 进来，这份文件切换时不用动。
    text = (OFFBOX / "nmu_china_probe.py").read_text(encoding="utf-8")
    assert "89-208-253-119" not in text and "89.208.253.119" not in text
