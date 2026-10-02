"""NMU 异地探针：状态变了才说话、只说一次；它自己停了，夜里的 Heartbeat 看得见。

本机那条 nmu-health 从同一台机器打回自己的公网地址（nmu-health.service:11-12），
机器或机房出口断了它看不见。异地探针跑在 OpenClaw VPS（纽约）的 user crontab 里，
每 10 分钟打一次 /health。这里每条测试钉一条规则，编号对应
deploy/offbox/nmu_offbox_probe.py 文档串里的规则 1-8。
"""
from __future__ import annotations

import ast
import builtins
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
OFFBOX = ROOT / "deploy" / "offbox"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"offbox_{name}", OFFBOX / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load("nmu_offbox_probe")

WEBHOOK = "https://discord.com/api/webhooks/1490/sentinel-webhook-token"
T0 = 1_790_000_040          # 2026-09-21 某个 :x4 附近；具体是哪天无所谓
RUN = 600                   # 每轮相隔 10 分钟

# curl 的四样东西：退出码、正文、HTTP 码、耗时秒数。
HEALTHY = (0, '{"status":"ok","service":"language-training-platform"}', 200, 0.240)
TIMEOUT = (28, "", 0, 15.0)
REFUSED = (7, "", 0, 0.02)
DNS_FAIL = (6, "", 0, 0.0)
SPA_SHELL = (0, "<!doctype html><title>南医大</title>", 200, 0.2)
BAD_GATEWAY = (0, "", 502, 0.05)
CONTROL_OK = (0, '{"url": "wss://gateway.discord.gg"}', 200, 0.09)
CONTROL_403 = (0, "error code: 1010", 403, 0.05)


class Net:
    """假 curl + 假 keychain-get：按 URL、有没有 --resolve 给结果，并记下每条 argv。"""

    def __init__(self) -> None:
        self.site = HEALTHY
        self.pinned = None          # None = 与不固定 IP 时一样
        self.control = CONTROL_OK
        self.keychain = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"protocolVersion": 1, "values": {probe.WEBHOOK_SECRET: WEBHOOK}}) + "\n")
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        if argv[0] == str(probe.KEYCHAIN_GET):
            assert argv[1:] == [probe.WEBHOOK_SECRET]
            return self.keychain
        assert argv[0] == probe.CURL, f"探针只该跑 curl 与 keychain-get：{argv}"
        url = argv[-1]
        if url == probe.CONTROL:
            shape = self.control
        elif "--resolve" in argv:
            shape = self.pinned or self.site
        else:
            shape = self.site
        rc, body, code, seconds = shape
        return SimpleNamespace(returncode=rc, stdout=f"{body}\n{code:03d} {seconds}",
                               stderr="" if rc == 0 else f"curl: ({rc}) simulated")

    def site_calls(self) -> list[list[str]]:
        return [call for call in self.calls
                if call[0] == probe.CURL and call[-1] != probe.CONTROL]

    def keychain_calls(self) -> int:
        return sum(1 for call in self.calls if call[0] == str(probe.KEYCHAIN_GET))


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
    content = payload["content"]
    found = [kind for kind, head in probe.HEADLINES.items() if head in content]
    assert len(found) == 1, content
    return found[0]


class World:
    """一个装好的探针目录 + 一张假网 + 一个假时钟；tick() 就是 cron 跑一轮。"""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.base = home / ".openclaw" / "workspace" / "nmu-probe"
        self.base.mkdir(parents=True)
        self.base.chmod(0o700)
        self.net = Net()
        self.outbox = Outbox()
        self.now = T0
        self.slept: list[float] = []
        self.webhook_gets: list[str] = []
        self.webhook_info: dict = {
            "id": "1490", "type": 1, "name": "daily-digest", "token": "sentinel-webhook-token",
            "channel_id": "1490232840397389894", "guild_id": "1490227330961117335",
            "url": WEBHOOK}

    def _get(self, url: str):
        self.webhook_gets.append(url)
        return True, dict(self.webhook_info)

    def run(self, *argv: str, advance: bool = True) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = probe.main(list(argv), base=self.base, run=self.net, send=self.outbox,
                              get=self._get, sleep=self.slept.append, clock=lambda: self.now)
        if advance:
            self.now += RUN
        return code, out.getvalue(), err.getvalue()

    def tick(self, site=None, *, control=None) -> dict:
        if site is not None:
            self.net.site = site
        if control is not None:
            self.net.control = control
        code, out, err = self.run()
        # cron 那一轮什么都不往 stdout/stderr 写：cron.log 只该收 traceback。
        assert (code, out, err) == (0, "", "")
        return self.state()

    def state(self) -> dict:
        return json.loads((self.base / "state.json").read_text(encoding="utf-8"))

    def kinds(self) -> list[str]:
        return [kind_of(payload) for _, payload in self.outbox.delivered]

    def log_lines(self) -> list[str]:
        return (self.base / "probe.log").read_text(encoding="utf-8").splitlines()

    def silence_until(self, text: str) -> None:
        (self.base / "silence-until").write_text(text, encoding="utf-8")


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def _iso(ts: float) -> str:
    return probe._iso(ts)


# ---------------------------------------------------------------------------
# 一轮怎么判：重试、分类
# ---------------------------------------------------------------------------

def test_a_healthy_run_sends_nothing_and_records_the_latency(world):
    state = world.tick(HEALTHY)

    assert state["status"] == "up" and state["fail_streak"] == 0
    assert state["last_reason"] == "ok" and state["last_run"] == _iso(T0)
    assert state["ring"][-1]["ms"] == 240
    assert world.outbox.tries == []
    # 一次就通：不重试、不探 Discord、不取 webhook
    assert len(world.net.calls) == 1 and world.slept == []
    assert world.log_lines() == [f"{_iso(T0)} UP ok ms=240 streak=0"]


def test_curl_is_asked_for_exactly_the_pinned_flags(world):
    world.tick(HEALTHY)

    [argv] = world.net.calls
    assert argv[0] == "/usr/bin/curl"
    assert argv[-1] == "https://89-208-253-119.sslip.io/health"
    joined = " ".join(argv)
    assert "-sS" in argv and f"-A {probe.UA}" in joined
    assert "--connect-timeout 8" in joined and "--max-time 15" in joined
    assert "--resolve" not in argv


def test_one_failed_run_sends_nothing(world):
    world.tick(HEALTHY)
    state = world.tick(TIMEOUT)

    assert state["status"] == "up" and state["fail_streak"] == 1
    assert world.outbox.tries == []
    # 一轮内试 3 次、两次间隔 20 秒，再探一次 Discord 判是不是本机的问题
    assert len(world.net.site_calls()) == 3 + 1      # 1 = 上一轮健康那次
    assert world.slept == [20, 20]
    assert world.net.calls[-1][-1] == probe.CONTROL


def test_two_failed_runs_send_exactly_one_down_dated_from_the_first_failure(world):
    world.tick(HEALTHY)
    world.tick(TIMEOUT)
    state = world.tick(TIMEOUT)

    assert state["status"] == "down"
    assert state["down_since"] == _iso(T0 + RUN)
    assert world.kinds() == ["down"]
    content = world.outbox.delivered[0][1]["content"]
    assert "从纽约看" in content and "timeout" in content
    assert "10-0" in content or "09-2" in content           # 带日期，不只是时分
    assert state["owed"] == []


def test_staying_down_sends_nothing_more(world):
    world.tick(TIMEOUT)
    world.tick(TIMEOUT)
    for shape in (TIMEOUT, REFUSED, BAD_GATEWAY, TIMEOUT):
        state = world.tick(shape)

    assert world.kinds() == ["down"]
    assert state["status"] == "down" and state["down_reason"] == "timeout"


def test_recovery_is_reported_once_with_the_outage_length(world):
    world.tick(HEALTHY)
    world.tick(TIMEOUT)                 # T0+600 第一次失败
    world.tick(TIMEOUT)                 # T0+1200 判 DOWN
    world.tick(HEALTHY)                 # T0+1800 恢复
    world.tick(HEALTHY)
    state = world.tick(HEALTHY)

    assert world.kinds() == ["down", "recovered"]
    content = world.outbox.delivered[1][1]["content"]
    assert "中断约 20 分钟" in content and "从纽约看" in content
    assert state["status"] == "up" and state["down_since"] is None


def test_a_failed_delivery_stays_owed_and_is_retried_next_run_without_duplicates(world):
    world.outbox = Outbox(False, False)          # 判 DOWN 那一轮两次都发不出去
    world.tick(TIMEOUT)
    state = world.tick(TIMEOUT)

    assert world.outbox.delivered == []
    assert [m["kind"] for m in state["owed"]] == ["down"]
    assert len(world.outbox.tries) == 2 and world.slept[-1] == 30
    assert "owed=DOWN" in world.log_lines()[-1] and "send_error=http_503" in world.log_lines()[-1]

    state = world.tick(TIMEOUT)                  # 仍然挂着：补发那条 DOWN，不是重复提醒
    assert world.kinds() == ["down"] and state["owed"] == []
    world.tick(TIMEOUT)
    world.tick(TIMEOUT)
    assert world.kinds() == ["down"]
    assert len(world.outbox.tries) == 3


def test_an_undelivered_down_then_recovery_becomes_one_combined_message(world):
    world.outbox = Outbox(False, False)
    world.tick(TIMEOUT)
    world.tick(TIMEOUT)                          # DOWN 没发出去
    state = world.tick(HEALTHY)                  # 恢复：合成一条补报

    assert world.kinds() == ["combined"]
    payload = world.outbox.delivered[0][1]
    assert "中断约 20 分钟" in payload["content"] and "没发出去" in payload["content"]
    assert "<@" not in payload["content"]
    assert state["owed"] == [] and state["status"] == "up"
    world.tick(HEALTHY)
    assert world.kinds() == ["combined"]


def test_a_probe_side_failure_is_recorded_but_never_counted(world):
    world.tick(HEALTHY)
    for _ in range(3):
        state = world.tick(TIMEOUT, control=TIMEOUT)     # 连 Discord 都打不通：是纽约这头的问题

    assert state["status"] == "up" and state["fail_streak"] == 0
    assert world.outbox.tries == []
    assert state["summary_24h"]["probe_side"] == 3 and state["summary_24h"]["fails"] == 0
    assert state["last_reason"].startswith("probe_side")
    assert world.net.keychain_calls() == 0

    state = world.tick(TIMEOUT, control=CONTROL_OK)
    assert state["fail_streak"] == 1                  # 不是 4


def test_a_control_answering_403_still_proves_the_probe_can_reach_the_internet(world):
    # Cloudflare 对某些 UA 回 403：那也是一个 HTTP 应答，说明纽约这头的网是通的。
    state = world.tick(TIMEOUT, control=CONTROL_403)

    assert state["fail_streak"] == 1 and state["summary_24h"]["probe_side"] == 0


def test_a_dns_only_failure_is_named_dns(world):
    world.net.pinned = HEALTHY                   # 绕过 DNS、钉到 IP 就通
    state = world.tick(DNS_FAIL)

    assert state["last_reason"] == "dns" and state["fail_streak"] == 1
    pinned = [call for call in world.net.calls if "--resolve" in call]
    assert len(pinned) == 1
    resolve = pinned[0][pinned[0].index("--resolve") + 1]
    assert resolve == "89-208-253-119.sslip.io:443:89.208.253.119"
    assert pinned[0][-1] == probe.URL             # Host 与 SNI 仍是真域名

    world.tick(DNS_FAIL)
    assert world.kinds() == ["down"]
    assert "dns" in world.outbox.delivered[0][1]["content"]


def test_when_the_pinned_request_also_fails_the_machine_itself_is_named(world):
    world.net.pinned = TIMEOUT
    state = world.tick(DNS_FAIL)

    assert state["last_reason"] == "timeout"


@pytest.mark.parametrize("shape", [
    SPA_SHELL,
    (0, '{"status":"ok","service":"some-other-service"}', 200, 0.2),
    (0, '["status", "ok"]', 200, 0.2),
], ids=["spa-shell", "other-service", "not-an-object"])
def test_a_200_with_the_wrong_body_counts_as_a_failure(world, shape):
    world.tick(shape)
    state = world.tick(shape)

    assert state["last_reason"] == "body_mismatch"
    assert world.kinds() == ["down"]


def test_extra_fields_in_the_health_body_are_fine(world):
    state = world.tick((0, '{"status":"ok","service":"language-training-platform",'
                           '"build":"af59c28"}', 200, 0.2))

    assert state["last_reason"] == "ok"


# ---------------------------------------------------------------------------
# 静默窗口
# ---------------------------------------------------------------------------

def test_silence_suppresses_an_incident_that_starts_and_ends_inside_it(world):
    world.silence_until(str(T0 + 90 * 60))
    world.tick(HEALTHY)
    world.tick(TIMEOUT)
    state = world.tick(TIMEOUT)
    assert state["status"] == "down"              # 状态照样记
    world.tick(TIMEOUT)
    state = world.tick(HEALTHY)

    assert world.outbox.tries == [] and state["owed"] == []
    assert state["status"] == "up"
    assert world.net.keychain_calls() == 0
    assert all("silenced" in line for line in world.log_lines())


def test_a_down_that_outlasts_the_silence_is_announced_when_the_silence_ends(world):
    world.silence_until(str(T0 + 1500))           # 维护说好 25 分钟
    world.tick(TIMEOUT)                           # T0
    world.tick(TIMEOUT)                           # T0+600：判 DOWN，但在静默里
    assert world.outbox.tries == []
    world.tick(TIMEOUT)                           # T0+1200：仍在静默
    assert world.outbox.tries == []
    state = world.tick(TIMEOUT)                   # T0+1800：静默过了还挂着 → 报

    assert world.kinds() == ["down"]
    assert state["down_since"] == _iso(T0)
    world.tick(HEALTHY)
    assert world.kinds() == ["down", "recovered"]


@pytest.mark.parametrize("text", [
    str(T0 + 25 * 3600),      # 超过 24 小时：不认
    "tomorrow",               # 垃圾：不认
    str(T0 - 60),             # 已经过期
    "",
    "1.79e9",
], ids=["over-24h", "garbage", "expired", "empty", "float"])
def test_a_silence_file_beyond_24h_or_garbage_is_ignored(world, text):
    world.silence_until(text)
    world.tick(TIMEOUT)
    world.tick(TIMEOUT)

    assert world.kinds() == ["down"]


def test_a_silence_of_exactly_24h_is_honoured(world):
    world.silence_until(f"{T0 + 24 * 3600}\n")
    world.tick(TIMEOUT)
    world.tick(TIMEOUT)

    assert world.outbox.tries == []


# ---------------------------------------------------------------------------
# 状态文件
# ---------------------------------------------------------------------------

def _full_shape(**fields) -> bytes:
    return json.dumps(dict(probe.fresh_state(), **fields)).encode("utf-8")


@pytest.mark.parametrize("content", [
    b"\x00\xffnot json at all",
    b'{"status": "down"}',
    # 字段齐全、类型也对，只是 DOWN 没有起点：按它发 RECOVERED 就是没头没尾的一条
    _full_shape(status="down", down_announced=True, fail_streak=2),
    _full_shape(status="up", owed=[{"kind": "recovered", "since": "昨天", "reason": "x"}]),
    None,                      # 把一份真的 DOWN 状态截掉一半
], ids=["garbage", "partial", "down-without-since", "owed-garbage", "truncated"])
def test_a_corrupt_state_file_never_produces_a_recovered(world, content):
    if content is None:
        world.tick(TIMEOUT)
        world.tick(TIMEOUT)
        world.outbox = Outbox()
        raw = (world.base / "state.json").read_bytes()
        content = raw[: len(raw) // 2]
    (world.base / "state.json").write_bytes(content)

    state = world.tick(HEALTHY)

    assert world.outbox.tries == []
    assert state["status"] == "up" and state["owed"] == []


def test_state_is_written_with_mode_0600_whatever_the_umask(world):
    previous = os.umask(0)
    try:
        world.tick(HEALTHY)
    finally:
        os.umask(previous)

    for name in ("state.json", "probe.log"):
        assert (world.base / name).stat().st_mode & 0o777 == 0o600, name


def test_a_state_write_that_dies_midway_leaves_the_old_file_whole(world, monkeypatch):
    world.tick(HEALTHY)
    before = (world.base / "state.json").read_bytes()

    def broken_fsync(_fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(probe.os, "fsync", broken_fsync)
    with pytest.raises(OSError):
        world.run()

    assert (world.base / "state.json").read_bytes() == before
    leftovers = sorted(p.name for p in world.base.iterdir())
    assert leftovers == ["probe.log", "state.json"]


def test_the_ring_keeps_one_day_and_the_summary_counts_it(world):
    for _ in range(probe.RING + 6):
        state = world.tick(HEALTHY)

    assert len(state["ring"]) == probe.RING == 144
    assert state["summary_24h"] == {"runs": 144, "fails": 0, "probe_side": 0, "p95_ms": 240}


def test_summary_p95_uses_nearest_rank_and_only_the_last_24h():
    now = T0 + 10 * 86400
    ring = [{"t": _iso(now - 86400 - 60 * i), "r": "ok", "ms": 99999, "probe": False}
            for i in range(1, 6)]                                       # 超过 24 小时
    ring += [{"t": _iso(now - 600 * i), "r": "ok", "ms": i, "probe": False}
             for i in range(1, 101)]                                    # 1..100 ms
    ring += [{"t": _iso(now - 30), "r": "timeout", "ms": None, "probe": False},
             {"t": _iso(now - 40), "r": "body_mismatch", "ms": 180, "probe": False},
             {"t": _iso(now - 50), "r": "probe_side:timeout", "ms": None, "probe": True}]

    summary = probe.summarize(ring, now)

    assert summary == {"runs": 103, "fails": 2, "probe_side": 1, "p95_ms": 95}
    assert probe.summarize([], now)["p95_ms"] is None


def test_probe_log_gets_one_line_per_run_and_is_trimmed(world, monkeypatch):
    monkeypatch.setattr(probe, "LOG_MAX_LINES", 5)
    for _ in range(8):
        world.tick(HEALTHY)

    lines = world.log_lines()
    assert len(lines) == 5
    assert lines[-1].startswith(_iso(T0 + 7 * RUN) + " UP ok")


# ---------------------------------------------------------------------------
# 发送面：webhook、提及、链接
# ---------------------------------------------------------------------------

def test_the_webhook_comes_from_the_keychain_wrapper_and_is_never_printed(world):
    world.tick(HEALTHY)
    assert world.net.keychain_calls() == 0          # 没东西要发就不碰凭证
    world.tick(TIMEOUT)
    world.tick(TIMEOUT)
    world.tick(HEALTHY)

    assert [url for url, _ in world.outbox.delivered] == [WEBHOOK, WEBHOOK]
    assert world.net.keychain_calls() == 2
    written = "".join(p.read_text(encoding="utf-8") for p in world.base.iterdir())
    assert "sentinel-webhook-token" not in written
    for _, payload in world.outbox.delivered:
        assert "sentinel-webhook-token" not in json.dumps(payload)


def test_a_bare_secret_on_stdout_is_not_mistaken_for_the_wrapper(world):
    # 2026-08-19 踩过：keychain-get 吐的是 JSON 包封，直接拿 stdout 当密钥会 401。
    world.net.keychain = SimpleNamespace(returncode=0, stdout=WEBHOOK + "\n", stderr="")
    world.tick(TIMEOUT)
    state = world.tick(TIMEOUT)

    assert world.outbox.tries == []
    assert [m["kind"] for m in state["owed"]] == ["down"]
    line = world.log_lines()[-1]
    assert "send_error=keychain_get_not_a_wrapper" in line
    assert "sentinel" not in line


def test_down_mentions_eric_and_recovered_does_not(world):
    world.tick(TIMEOUT)
    world.tick(TIMEOUT)
    world.tick(HEALTHY)

    (_, down), (_, recovered) = world.outbox.delivered
    assert down["content"].startswith("<@1403490018265399327>")
    assert down["allowed_mentions"] == {"users": ["1403490018265399327"]}
    assert "<@" not in recovered["content"]
    assert recovered["allowed_mentions"] == {"parse": []}
    assert down["username"] == recovered["username"] == "NMU 异地探针"


def test_links_are_wrapped_so_discord_shows_no_preview(world):
    world.tick(TIMEOUT)
    world.tick(TIMEOUT)

    content = world.outbox.delivered[0][1]["content"]
    assert f"<{probe.URL}>" in content
    assert re.search(r"(?<!<)https://", content) is None


def test_a_webhook_url_never_leaves_inside_a_message():
    sent: list[dict] = []
    probe.post(WEBHOOK, f"原因里混进了 {WEBHOOK} 这一串", mention=False,
               send=lambda _url, payload: (sent.append(payload), (True, "status=204"))[1],
               sleep=lambda _s: None)

    assert "sentinel-webhook-token" not in sent[0]["content"]


# ---------------------------------------------------------------------------
# 命令行的其余几种模式
# ---------------------------------------------------------------------------

def test_dry_run_never_posts_and_never_touches_the_real_state(world, tmp_path):
    scratch = tmp_path / "selftest" / "state.json"
    scratch.parent.mkdir()

    def dry(*extra: str) -> str:
        code, out, err = world.run("--dry-run", "--state", str(scratch), *extra)
        assert code == 0 and err == ""
        return out.strip()

    assert dry().endswith("would_send=-") and "status=up result=ok" in dry()
    bad = "https://89-208-253-119.sslip.io/health-selftest"
    world.net.site = SPA_SHELL
    first = dry("--url", bad)
    assert "status=up" in first and "streak=1" in first and "would_send=-" in first
    second = dry("--url", bad)
    assert "status=down result=body_mismatch" in second and "would_send=DOWN" in second
    world.net.site = HEALTHY
    assert "would_send=RECOVERED" in dry()

    assert world.outbox.tries == [] and world.net.keychain_calls() == 0
    assert sorted(p.name for p in world.base.iterdir()) == []
    assert any(call[-1] == bad for call in world.net.calls)


def test_dry_run_without_a_state_reads_the_real_one_but_never_writes_it(world):
    world.tick(TIMEOUT)
    before = {p.name: p.read_bytes() for p in world.base.iterdir()}

    code, out, _ = world.run("--dry-run")

    assert code == 0 and "status=down" in out and "would_send=DOWN" in out
    assert {p.name: p.read_bytes() for p in world.base.iterdir()} == before
    assert world.outbox.tries == []


def test_dry_run_refuses_to_use_the_real_state_as_its_scratch(world):
    code, _, err = world.run("--dry-run", "--state", str(world.base / "state.json"))

    assert code != 0 and "state.json" in err
    assert not (world.base / "state.json").exists()


@pytest.mark.parametrize("argv", [["--url", "https://example.org/health"],
                                  ["--state", "/tmp/x.json"],
                                  ["--status", "--url", "https://example.org/health"]])
def test_url_and_state_overrides_only_exist_for_dry_run(world, argv):
    with pytest.raises(SystemExit) as raised:
        world.run(*argv)

    assert raised.value.code == 2
    assert world.net.calls == []


def test_status_reads_the_state_without_any_network(world):
    code, out, _ = world.run("--status", advance=False)
    assert code == 0 and "state=missing" in out

    world.tick(TIMEOUT)
    world.tick(TIMEOUT)
    calls = len(world.net.calls)
    world.silence_until(str(world.now + 3600))
    code, out, _ = world.run("--status", advance=False)

    assert code == 0 and len(world.net.calls) == calls
    assert "status=down" in out and f"down_since={_iso(T0)}" in out
    assert "age_s=600" in out and "silence=until" in out
    assert "runs=2 fails=2" in out


def test_status_says_so_when_the_state_file_is_corrupt(world):
    (world.base / "state.json").write_text("{", encoding="utf-8")

    code, out, _ = world.run("--status")

    assert code == 1 and "state=corrupt" in out


def test_check_webhook_prints_only_channel_guild_and_name(world):
    code, out, err = world.run("--check-webhook")

    assert code == 0
    assert out.strip() == ("channel_id=1490232840397389894 guild_id=1490227330961117335 "
                           "name=daily-digest")
    assert "sentinel" not in out + err
    assert world.webhook_gets == [WEBHOOK] and world.outbox.tries == []


def test_test_alert_sends_exactly_one_labelled_message_and_never_retries(world):
    code, out, _ = world.run("--test-alert")

    assert code == 0 and out.strip() == "test_alert_sent status=204"
    [(_, payload)] = world.outbox.tries
    assert "测试" in payload["content"] and "<@" not in payload["content"]
    assert not (world.base / "state.json").exists()

    world.outbox = Outbox(False)
    code, out, _ = world.run("--test-alert")
    assert code == 1 and out.strip() == "test_alert_failed http_503"
    assert len(world.outbox.tries) == 1 and world.slept == []


def test_no_command_the_probe_runs_is_openclaw(world, tmp_path):
    # `openclaw` CLI 一次 8-10 秒、400MB，全机共用一个瓶颈；2026-09-08 并发调用让 armed
    # watchdog 重启了健康的网关。探针只许跑 curl 与 keychain-get。
    world.tick(HEALTHY)
    world.tick(DNS_FAIL)
    world.tick(TIMEOUT, control=TIMEOUT)
    world.tick(TIMEOUT, control=CONTROL_OK)
    world.tick(HEALTHY)
    world.run("--dry-run", "--state", str(tmp_path / "s.json"))
    world.run("--status")
    world.run("--check-webhook")
    world.run("--test-alert")

    assert {Path(call[0]).name for call in world.net.calls} == {"curl", "keychain-get"}
    source = (OFFBOX / "nmu_offbox_probe.py").read_text(encoding="utf-8")
    assert re.search(r"[\"']openclaw[\"']", source) is None


# ---------------------------------------------------------------------------
# crontab 那一行
# ---------------------------------------------------------------------------

def _minutes(field: str) -> set[int]:
    if field.startswith("*/"):
        return set(range(0, 60, int(field[2:])))
    span, _, step = field.partition("/")
    start, _, stop = span.partition("-")
    return set(range(int(start), int(stop or start) + 1, int(step or 1)))


def test_crontab_line_runs_under_flock_with_absolute_paths_off_the_busy_minutes():
    text = (OFFBOX / "crontab.line").read_text(encoding="utf-8")
    assert text.endswith("\n") and text.count("\n") == 1 and not text.startswith("#")
    fields = text.split()
    minute, rest, command = fields[0], fields[1:5], fields[5:]

    assert minute == "4-59/10" and rest == ["*", "*", "*", "*"]
    mine = _minutes(minute)
    assert mine == {4, 14, 24, 34, 44, 54}
    # watchdog v1 */5、watchdog v2 2-59/5、tripo :17、本机 nmu-health 整十分
    for busy in ("*/5", "2-59/5", "17", "0-59/10"):
        assert not mine & _minutes(busy), busy

    home = str(probe.DIR)
    assert command[:3] == ["/usr/bin/flock", "-n", f"{home}/.lock"]
    assert command[3:6] == ["/usr/bin/python3", "-B", f"{home}/nmu_offbox_probe.py"]
    assert command[6:] == [">>", f"{home}/cron.log", "2>&1"]
    assert all(part.startswith("/") for part in command if "/" in part and part != "2>&1")
    # watchdog v1 认 x-feed 进程的模式，探针路径不能被它认成 x-feed 而推迟重启判断
    assert re.search(r"send_x_digest|/x-feed/", text) is None
    assert "openclaw " not in text.replace(".openclaw/", "")


def test_the_probe_paths_live_under_the_openclaw_workspace():
    assert str(probe.DIR) == "/home/eric/.openclaw/workspace/nmu-probe"
    assert str(probe.KEYCHAIN_GET) == "/home/eric/.openclaw/bin/keychain-get"
    assert probe.WEBHOOK_SECRET == "OPENCLAW_DISCORD_DAILY_WEBHOOK"
    assert probe.DOWN_AFTER_RUNS == 2 and probe.SILENCE_MAX_S == 24 * 3600


# ---------------------------------------------------------------------------
# Heartbeat 那一行：探针自己停了不会有任何消息，只有 22:00 CT 的 Heartbeat 看得见
# ---------------------------------------------------------------------------

SYNTHETIC_HEARTBEAT = '''#!/usr/bin/env python3
"""与 OpenClaw heartbeat.py 同一副骨架：插入点、CHECKS 写法、名字都一样。"""
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path("/home/eric/__nmu_probe_test_nowhere__")
OK, WARN, ERROR, INFO = "OK", "WARN", "ERROR", "INFO"


@dataclass
class CheckResult:
    status: str
    label: str
    detail: str


def check_watchdog() -> CheckResult:
    return CheckResult(OK, "Watchdog", "healthy")


def check_maintenance() -> CheckResult:
    return CheckResult(OK, "Maintenance", "none")


CHECKS = [
    check_watchdog,
    check_maintenance,
]


def main():
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


@pytest.fixture
def heartbeat(world, monkeypatch):
    module = _load("heartbeat_check")
    monkeypatch.setattr(module, "HOME", world.home)
    # 让探针的假时钟贴着真时钟走：check_nmu_probe 用的是 heartbeat 进程自己的 now()。
    world.now = int(time.time()) - 3 * RUN
    return module


def _check(heartbeat):
    result = heartbeat.check_nmu_probe()
    return result.status, result.label, result.detail


def test_heartbeat_warns_when_the_probe_never_ran(heartbeat):
    status, label, detail = _check(heartbeat)

    assert (status, label) == ("WARN", "NMU 异地探针")
    assert "state.json" in detail


def test_heartbeat_reads_what_the_probe_writes(world, heartbeat):
    for _ in range(3):
        world.tick(HEALTHY)

    status, _, detail = _check(heartbeat)

    assert status == "OK"
    assert detail.startswith("healthy · 24h 3 轮 · p95 240 ms")


def test_heartbeat_calls_a_probe_that_stopped_25_minutes_ago_stale(world, heartbeat):
    world.now = int(time.time()) - 26 * 60
    world.tick(HEALTHY)

    status, _, detail = _check(heartbeat)

    assert status == "WARN" and detail.startswith("stale")


def test_heartbeat_reports_an_ongoing_outage_as_error(world, heartbeat):
    world.tick(TIMEOUT)
    world.tick(TIMEOUT)

    status, _, detail = _check(heartbeat)

    assert status == "ERROR"
    assert detail.startswith(f"DOWN since {world.state()['down_since']} · timeout")


def test_heartbeat_warns_about_failures_that_stayed_under_the_threshold(world, heartbeat):
    world.tick(HEALTHY)
    world.tick(TIMEOUT)
    world.tick(HEALTHY)

    status, _, detail = _check(heartbeat)

    assert status == "WARN" and "3 轮里 1 轮失败" in detail
    assert world.outbox.tries == []


def test_heartbeat_warns_about_messages_still_owed(world, heartbeat):
    world.outbox = Outbox(True, False, False)       # DOWN 发出去了，RECOVERED 两次都没发出去
    world.tick(TIMEOUT)
    world.tick(TIMEOUT)
    world.tick(HEALTHY)

    status, _, detail = _check(heartbeat)

    assert status == "WARN" and "1 条" in detail and "没发出去" in detail


@pytest.mark.parametrize("text", ["{", '{"last_run": "昨天"}', "[]"])
def test_a_broken_state_file_degrades_to_one_warn_line(world, heartbeat, text):
    (world.base / "state.json").write_text(text, encoding="utf-8")

    status, _, _ = _check(heartbeat)

    assert status == "WARN"


def test_the_inserted_function_only_uses_names_heartbeat_already_has():
    module = _load("heartbeat_check")
    snippet = module.block()
    assert snippet.startswith(module.BEGIN) and snippet.endswith(module.END)
    tree = ast.parse(snippet)
    [function] = tree.body
    assert isinstance(function, ast.FunctionDef) and function.name == "check_nmu_probe"
    assigned = {node.id for node in ast.walk(function)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    assigned |= {node.name for node in ast.walk(function)
                 if isinstance(node, ast.ExceptHandler) and node.name}
    loaded = {node.id for node in ast.walk(function)
              if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    free = loaded - assigned - set(dir(builtins))
    # heartbeat.py 顶部：import json；from datetime import datetime, timedelta, timezone；
    # HOME = Path("/home/eric")；OK/WARN/ERROR；@dataclass class CheckResult。
    assert free <= {"json", "datetime", "timezone", "HOME", "CheckResult", "OK", "WARN", "ERROR"}
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(function))


def test_insertion_hits_each_anchor_exactly_once_and_compiles():
    module = _load("heartbeat_check")

    updated = module.insert(SYNTHETIC_HEARTBEAT, module.block())

    compile(updated, "heartbeat.py", "exec")
    assert updated.count("def check_nmu_probe") == 1
    assert updated.index("def check_nmu_probe") < updated.index("CHECKS = [")
    assert "    check_watchdog,\n    check_nmu_probe,\n    check_maintenance,\n" in updated
    # 原文一个字节都没丢：去掉插进去的两段就回到原样
    restored = updated.replace("    check_nmu_probe,\n", "", 1)
    restored = restored.replace("\n\n" + module.block() + "\n", "", 1)
    assert restored == SYNTHETIC_HEARTBEAT


@pytest.mark.parametrize("source", [
    SYNTHETIC_HEARTBEAT.replace("    check_watchdog,\n", ""),
    SYNTHETIC_HEARTBEAT.replace("CHECKS = [", "CHECKS = [  # 被人改过"),
    SYNTHETIC_HEARTBEAT + "\n\nCHECKS = [\n    check_watchdog,\n]\n",
], ids=["no-watchdog-entry", "checks-line-changed", "anchors-twice"])
def test_insertion_refuses_when_an_anchor_does_not_hit_exactly_once(source):
    module = _load("heartbeat_check")

    with pytest.raises(module.InsertError):
        module.insert(source, module.block())


def test_insertion_refuses_to_run_twice():
    module = _load("heartbeat_check")
    once = module.insert(SYNTHETIC_HEARTBEAT, module.block())

    with pytest.raises(module.InsertError):
        module.insert(once, module.block())


def _run_helper(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-B", str(OFFBOX / "heartbeat_check.py"), *argv],
                          capture_output=True, text=True, timeout=60)


def test_the_insert_and_call_modes_work_on_a_real_file(tmp_path):
    original = tmp_path / "heartbeat.py"
    original.write_text(SYNTHETIC_HEARTBEAT, encoding="utf-8")
    original.chmod(0o640)
    staged = tmp_path / "heartbeat.py.nmu-probe.new"

    inserted = _run_helper("--insert", str(original), "--out", str(staged))
    assert inserted.returncode == 0, inserted.stderr
    assert original.read_text(encoding="utf-8") == SYNTHETIC_HEARTBEAT
    assert staged.stat().st_mode & 0o777 == 0o640
    again = _run_helper("--insert", str(staged), "--out", str(tmp_path / "twice"))
    assert again.returncode != 0 and not (tmp_path / "twice").exists()

    called = _run_helper("--call", str(staged))
    assert called.returncode == 0, called.stderr
    assert called.stdout.strip().startswith("WARN NMU 异地探针: 无 state.json")
    assert _run_helper("--call", str(original)).returncode != 0


@pytest.mark.parametrize("entries", [
    "    check_watchdog,\n    check_maintenance,\n    check_nmu_probe,\n",
    "    check_watchdog,\n    check_nmu_probe,\n    check_nmu_probe,\n    check_maintenance,\n",
], ids=["wrong-place", "twice"])
def test_call_rejects_a_heartbeat_whose_checks_list_is_off(tmp_path, entries):
    module = _load("heartbeat_check")
    updated = module.insert(SYNTHETIC_HEARTBEAT, module.block())
    good = "    check_watchdog,\n    check_nmu_probe,\n    check_maintenance,\n"
    staged = tmp_path / "heartbeat.py"
    staged.write_text(updated.replace(good, entries), encoding="utf-8")

    result = _run_helper("--call", str(staged))

    assert result.returncode == 1 and "CHECKS" in result.stderr
    assert result.stdout == ""


def test_the_insert_mode_never_overwrites_its_input(tmp_path):
    original = tmp_path / "heartbeat.py"
    original.write_text(SYNTHETIC_HEARTBEAT, encoding="utf-8")

    result = _run_helper("--insert", str(original), "--out", str(original))

    assert result.returncode != 0
    assert original.read_text(encoding="utf-8") == SYNTHETIC_HEARTBEAT
