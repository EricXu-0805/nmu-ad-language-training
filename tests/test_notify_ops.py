"""nmu-alert@ 的告警正文：失败原因要在最前面，日志只取这一轮。

2026-09-28 os-security 那次告警只带了 `journalctl -u … -n 12`：检查结论整份重定向进了
os-security.state，journal 里只剩 systemd 自己那几行，六个待装的包一个都没出现在消息里。
"""
from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


notify = _load("notify_ops")
os_security = _load("os_security_check")

WEBHOOK = "https://discord.com/api/webhooks/123456/sentinel-webhook-token"
INVOCATION = "0123456789abcdef0123456789abcdef"
SECURITY = ["libssl3", "openssl", "libc6", "libc-bin", "sudo", "curl"]
PREVIOUS_RUN = "PREVIOUS-RUN Finished NMU OS security backlog check."


class FakeSystem:
    """假 systemctl/journalctl：只认 InvocationID 过滤的查询拿到的才是本轮。"""

    def __init__(self, *, invocation: str = INVOCATION, started_seconds_ago: float = 60.0,
                 output: str = "", manager: str = "", previous: str = PREVIOUS_RUN) -> None:
        self.invocation = invocation
        self.started_seconds_ago = started_seconds_ago
        self.output = output        # 单元进程自己的输出（_SYSTEMD_INVOCATION_ID）
        self.manager = manager      # systemd 关于这一轮的话（INVOCATION_ID）
        self.previous = previous    # 只有不按本轮过滤时才会混进来的上一轮
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ["systemctl", "show"]:
            started = int((time.monotonic() - self.started_seconds_ago) * 1_000_000)
            return SimpleNamespace(returncode=0, stderr="", stdout=(
                f"InvocationID={self.invocation}\n"
                f"InactiveExitTimestampMonotonic={started}\n"))
        if argv[0] == "journalctl":
            if self.invocation and f"_SYSTEMD_INVOCATION_ID={self.invocation}" in argv:
                body = self.output + (self.manager if "+" in argv else "")
            else:
                body = self.previous + "\n" + self.output + self.manager
            return SimpleNamespace(returncode=0, stdout=body, stderr="")
        raise AssertionError(f"没预料到的命令：{argv}")


class Outbox:
    def __init__(self, *outcomes: tuple[bool, str]) -> None:
        self.outcomes = list(outcomes) or [(True, "alert_sent status=204")]
        self.sent: list[tuple[str, str]] = []

    def __call__(self, webhook: str, content: str) -> tuple[bool, str]:
        self.sent.append((webhook, content))
        return self.outcomes[min(len(self.sent), len(self.outcomes)) - 1]


def _no_retry(_seconds):
    pytest.fail("发送成功时不该等待重试")


def _write_os_security_state(tmp_path: Path, backups: Path) -> None:
    """真跑一遍 os_security_check --json，得到与单元 `> state 2>&1` 同形的 state。"""
    simulation = tmp_path / "apt-simulation.txt"
    simulation.write_text("".join(
        f"Inst {name} [1.0-1] (1.0-2 Ubuntu:22.04/jammy-security [amd64])\n"
        for name in SECURITY), encoding="utf-8")
    lists = tmp_path / "apt-lists"
    lists.mkdir()
    (lists / "archive.ubuntu.com_ubuntu_dists_jammy-security_InRelease").write_text(
        "x", encoding="utf-8")
    captured = io.StringIO()
    with redirect_stdout(captured):
        code = os_security.main([
            "--json", "--simulate-file", str(simulation), "--lists-dir", str(lists)])
    assert code == 1
    (backups / "os-security.state").write_text(captured.getvalue(), encoding="utf-8")


@pytest.fixture
def backups(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("NMU_OPS_WEBHOOK", WEBHOOK)
    root = tmp_path / "backups"
    root.mkdir()
    return root


def _alert(backups: Path, unit: str, system: FakeSystem, outbox: Outbox) -> str:
    code = notify.main(["--unit", unit, "--backup-root", str(backups)],
                       run=system, send=outbox, sleep=_no_retry)
    assert code == 0
    [(_, content)] = outbox.sent
    return content


def test_os_security_alert_leads_with_the_pending_packages_of_this_run(tmp_path, backups):
    _write_os_security_state(tmp_path, backups)
    system = FakeSystem(manager=(
        "nmu-os-security.service: Main process exited, code=exited, status=1/FAILURE\n"))

    content = _alert(backups, "nmu-os-security", system, Outbox())

    for package in SECURITY:
        assert package in content
    assert "PREVIOUS-RUN" not in content
    assert "status=1/FAILURE" in content
    # 原因在最前面：包名先于日志出现
    assert content.index("libssl3") < content.index("status=1/FAILURE")
    assert any(f"_SYSTEMD_INVOCATION_ID={INVOCATION}" in call for call in system.calls)


def test_reason_is_capped_at_six_hundred_characters(backups):
    (backups / "restore-drill.state").write_text(
        "恢复演练失败：" + "甲" * 2000 + "\n", encoding="utf-8")

    content = _alert(backups, "nmu-restore-drill", FakeSystem(), Outbox())

    reason = content.split("):\n", 1)[1].split("\n", 1)[0]
    assert reason.startswith("恢复演练失败：")
    assert len(reason) == 600


def test_a_state_file_this_run_never_rewrote_is_labelled_as_the_previous_runs(backups):
    # capacity_check 在 OSError 路径上只打印 UNHEALTHY 就退出，不写 state：
    # 盘上那份还是昨天的 HEALTHY，原样端出来就是把旧结论当成这次的原因。
    state = backups / "capacity.state"
    state.write_text("[2026-10-01 21:10:04] HEALTHY used=61.2% free=4100MB\n",
                     encoding="utf-8")
    yesterday = time.time() - 86400
    os.utime(state, (yesterday, yesterday))
    system = FakeSystem(output=(
        "[2026-10-02 21:10:03] UNHEALTHY capacity_probe_failed err=5 "
        "reason=filesystem_probe_error\n"))

    content = _alert(backups, "nmu-capacity", system, Outbox())

    assert "本轮没有改写它" in content
    assert "capacity_probe_failed" in content


def test_backup_alert_quotes_the_last_line_of_backup_log(backups):
    (backups / "backup.log").write_text(
        "[2026-10-01 19:31:00] ok snapshot=20261001-193100 size=1.2M config=ok keep=14\n"
        "[2026-10-02 19:30:12] FAIL code=base_snapshot_failed rc=1 "
        "detail=ModuleNotFoundError: No module named 'sqlalchemy'\n",
        encoding="utf-8")

    content = _alert(backups, "nmu-backup", FakeSystem(), Outbox())

    assert "FAIL code=base_snapshot_failed" in content
    assert "ok snapshot=20261001-193100" not in content


def test_health_probe_reason_is_this_runs_own_output(backups):
    system = FakeSystem(
        output="curl: (22) The requested URL returned error: 502\n",
        manager="nmu-health.service: Main process exited, code=exited, status=22/n/a\n")

    content = _alert(backups, "nmu-health", system, Outbox())

    reason = content.split("):\n", 1)[1].split("\n", 1)[0]
    assert reason == "curl: (22) The requested URL returned error: 502"
    assert "status=22" in content
    assert "PREVIOUS-RUN" not in content


def test_without_an_invocation_id_the_tail_is_used_and_labelled(backups):
    system = FakeSystem(invocation="", output="Traceback: boom\n")

    content = _alert(backups, "nmu-backup-health", system, Outbox())

    fallback = [call for call in system.calls if "-u" in call]
    assert fallback and fallback[0][fallback[0].index("-n") + 1] == "12"
    assert "可能混有上一轮" in content
    assert "Traceback: boom" in content


def test_reason_sources_point_where_the_units_actually_write():
    units = ROOT / "deploy" / "systemd"
    assert str(notify.DEFAULT_BACKUP_ROOT) == "/opt/nmu/backups"
    for unit, (kind, filename) in notify.REASON_SOURCES.items():
        service = (units / f"{unit}.service").read_text(encoding="utf-8")
        if kind == "state":
            assert f"/opt/nmu/backups/{filename}" in service, unit
    daily = (ROOT / "scripts" / "vps-backup-daily.sh").read_text(encoding="utf-8")
    assert 'ROOT="${NMU_BACKUP_ROOT:-/opt/nmu/backups}"' in daily
    assert f'LOG="$ROOT/{notify.REASON_SOURCES["nmu-backup"][1]}"' in daily


def test_a_failed_send_is_retried_once_after_thirty_seconds_then_fails(backups, capsys):
    outbox = Outbox((False, "alert_send_failed err=timed out"))
    sleeps: list[float] = []

    code = notify.main(["--message", "异地备份拉取失败 rc=1"],
                       run=FakeSystem(), send=outbox, sleep=sleeps.append)

    assert code != 0
    assert len(outbox.sent) == 2
    assert sleeps == [30]
    captured = capsys.readouterr()
    assert captured.err.count("alert_send_failed") == 2
    assert WEBHOOK not in captured.out + captured.err


def test_a_send_that_recovers_on_the_retry_succeeds(backups):
    outbox = Outbox((False, "alert_send_http_429"), (True, "alert_sent status=204"))
    sleeps: list[float] = []

    code = notify.main(["--message", "x"], run=FakeSystem(), send=outbox, sleep=sleeps.append)

    assert code == 0
    assert len(outbox.sent) == 2 and sleeps == [30]


def test_a_webhook_that_leaked_into_a_log_is_not_forwarded(backups):
    system = FakeSystem(output=f"curl: (6) Could not resolve host for {WEBHOOK}\n")

    content = _alert(backups, "nmu-health", system, Outbox())

    assert "sentinel-webhook-token" not in content
