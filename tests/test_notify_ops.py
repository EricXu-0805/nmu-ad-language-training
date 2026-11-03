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


def _write_os_security_state(tmp_path: Path, backups: Path, monkeypatch,
                             security: list[str] = SECURITY,
                             stale: dict[str, str] | None = None) -> None:
    """真跑一遍 os_security_check --json，得到与单元 `> state 2>&1` 同形的 state。

    stale：{单元: 库路径}，假 /proc 里还映射着这个已被替换的库的进程。
    """
    simulation = tmp_path / "apt-simulation.txt"
    simulation.write_text("Calculating upgrade...\n" + "".join(
        f"Inst {name} [1.0-1] (1.0-2 Ubuntu:22.04/jammy-security [amd64])\n"
        for name in security), encoding="utf-8")
    lists = tmp_path / "apt-lists"
    lists.mkdir()
    (lists / "archive.ubuntu.com_ubuntu_dists_jammy-security_InRelease").write_text(
        "x", encoding="utf-8")
    proc = tmp_path / "proc"
    proc.mkdir()
    for pid, (unit, library) in enumerate((stale or {}).items(), start=800):
        (proc / str(pid)).mkdir()
        (proc / str(pid) / "cgroup").write_text(f"0::/system.slice/{unit}\n", encoding="utf-8")
        (proc / str(pid) / "maps").write_text(
            f"7f38fcedf000-7f38fcf0a000 r-xp 00000000 08:03 131330     {library} (deleted)\n",
            encoding="utf-8")
    (tmp_path / "boot").mkdir()
    monkeypatch.setattr(os_security, "REBOOT_REQUIRED", tmp_path / "no-reboot-required")
    monkeypatch.setattr(os_security.os, "geteuid", lambda: 0)
    captured = io.StringIO()
    with redirect_stdout(captured):
        code = os_security.main([
            "--json", "--simulate-file", str(simulation), "--lists-dir", str(lists),
            "--proc-dir", str(proc), "--boot-dir", str(tmp_path / "boot")])
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


def test_os_security_alert_leads_with_the_pending_packages_of_this_run(
        tmp_path, backups, monkeypatch):
    _write_os_security_state(tmp_path, backups, monkeypatch)
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


def test_os_security_alert_leads_with_a_stale_library_and_lists_the_notes_after(
        tmp_path, backups, monkeypatch):
    """夜里自动装完补丁、积压是 0，nmu 还跑着旧 libssl：告警打头就是它，
    其余还在旧库上的单元作为提示跟在后面，不在 Discord 里消失。"""
    _write_os_security_state(tmp_path, backups, monkeypatch, security=[], stale={
        "nmu.service": "/usr/lib/x86_64-linux-gnu/libssl.so.3",
        "sing-box.service": "/usr/lib/x86_64-linux-gnu/libc.so.6",
    })
    system = FakeSystem(manager=(
        "nmu-os-security.service: Main process exited, code=exited, status=1/FAILURE\n"))

    content = _alert(backups, "nmu-os-security", system, Outbox())

    reason = content.split("):\n", 1)[1]
    assert reason.startswith("nmu.service")
    assert "libssl.so.3" in reason and "systemctl restart nmu" in reason
    assert (content.index("systemctl restart nmu") < content.index("sing-box.service")
            < content.index("status=1/FAILURE"))


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


@pytest.mark.parametrize("unit", sorted(notify.REASON_SOURCES))
def test_every_alerting_unit_gets_its_next_step_before_the_journal(backups, unit):
    system = FakeSystem(manager=f"{unit}.service: Main process exited, status=1/FAILURE\n")

    content = _alert(backups, unit, system, Outbox())

    step = f"下一步:{notify.NEXT_STEPS[unit]}"
    assert step in content
    assert content.index(step) < content.index("本轮日志:")


def test_a_unit_without_a_registered_next_step_gets_no_next_step_line(backups):
    content = _alert(backups, "nmu-caddy", FakeSystem(output="caddy exited 1\n"), Outbox())

    assert "caddy exited 1" in content
    assert "下一步" not in content


def test_no_next_step_ever_recommends_a_full_upgrade():
    """2026-11-02 那条 os-security 告警的原因写着 apt-get full-upgrade;内核类只在维护窗口里装。"""
    for unit, step in notify.NEXT_STEPS.items():
        assert step.replace("绝不 apt-get full-upgrade", "").count("full-upgrade") == 0, unit
    os_step = notify.NEXT_STEPS["nmu-os-security"]
    assert "绝不 apt-get full-upgrade" in os_step
    assert "kernel_netplan_window.sh" in os_step and "跑 check" in os_step
    assert (ROOT / "deploy" / "maintenance" / "kernel_netplan_window.sh").is_file()
    assert (ROOT / "scripts" / "capacity_reclaim.py").is_file()


def test_the_host_label_replaces_the_machine_hostname(backups, monkeypatch):
    monkeypatch.setattr(notify.socket, "gethostname", lambda: "green-fiber-1.localdomain")
    monkeypatch.setenv("NMU_OPS_HOST_LABEL", "NMU 生产机 box A")
    labelled = _alert(backups, "nmu-backup", FakeSystem(), Outbox())
    monkeypatch.delenv("NMU_OPS_HOST_LABEL")
    plain = _alert(backups, "nmu-backup", FakeSystem(), Outbox())

    assert labelled.startswith("🔴 NMU 运维告警 · NMU 生产机 box A · ")
    assert "green-fiber-1" not in labelled
    assert plain.startswith("🔴 NMU 运维告警 · green-fiber-1.localdomain · ")
    unit = (ROOT / "deploy" / "systemd" / "nmu-alert@.service").read_text(encoding="utf-8")
    assert 'Environment="NMU_OPS_HOST_LABEL=NMU 生产机 box A"' in unit


def test_a_long_journal_is_still_truncated_and_the_next_step_survives(backups):
    system = FakeSystem(manager="甲" * 5000 + "\n")

    content = _alert(backups, "nmu-os-security", system, Outbox())

    assert len(content) == notify.MAX_CONTENT + len("\n…(截断)")
    assert f"下一步:{notify.NEXT_STEPS['nmu-os-security']}" in content
