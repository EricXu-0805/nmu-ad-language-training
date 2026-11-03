from __future__ import annotations

from datetime import datetime
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "backup_health_check",
    Path(__file__).resolve().parents[1] / "scripts" / "backup_health_check.py")
assert _SPEC is not None and _SPEC.loader is not None
health = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(health)


NOW = datetime(2026, 8, 6, 3, 0, 0)


def _root(tmp_path: Path, log_lines: list[str], snapshots: list[str]) -> Path:
    root = tmp_path / "backups"
    (root / "daily").mkdir(parents=True)
    for name in snapshots:
        (root / "daily" / name).mkdir()
    (root / "backup.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return root


def _evaluate(root: Path, **overrides):
    kwargs = {"max_age_hours": 30.0, "max_consecutive_failures": 1, "min_free_mb": 0}
    kwargs.update(overrides)
    return health.evaluate(root / "backup.log", root / "daily", NOW, **kwargs)


OK_LINE = ("[2026-08-06 02:30:00] ok snapshot=20260806-023000 size=924K "
           "config=ok keep=14")
LOCK_SKIP = "[2026-08-06 02:40:00] SKIP code=backup_lock_busy"


def test_fresh_published_snapshot_is_healthy(tmp_path):
    root = _root(tmp_path, [OK_LINE], ["20260806-023000"])

    assert _evaluate(root) == []


def test_stale_last_success_is_reported(tmp_path):
    # 这条正是 2026-07-23 → 08-05 的真实形态:日志里有 ok,只是老了十四天。
    root = _root(
        tmp_path,
        ["[2026-07-23 02:36:54] ok snapshot=20260723-023654 size=172K config=ok"],
        ["20260723-023654"])

    problems = _evaluate(root)

    assert len(problems) == 1
    assert problems[0].startswith("last_backup_stale ")
    assert "age_hours=336." in problems[0]


def test_two_failures_after_the_last_success_trip_the_trend_rule(tmp_path):
    root = _root(tmp_path, [
        OK_LINE,
        "[2026-08-06 02:40:00] FAIL code=base_snapshot_failed rc=1",
        "[2026-08-06 02:50:00] FAIL code=base_snapshot_failed rc=1",
    ], ["20260806-023000"])

    problems = _evaluate(root)

    assert problems == ["consecutive_failures count=2 limit=1"]


def test_a_single_failure_after_success_is_tolerated(tmp_path):
    root = _root(tmp_path, [
        OK_LINE,
        "[2026-08-06 02:40:00] FAIL code=base_snapshot_failed rc=1",
    ], ["20260806-023000"])

    assert _evaluate(root) == []


def test_lock_skip_is_neutral_and_does_not_poison_a_later_success(tmp_path):
    root = _root(tmp_path, [OK_LINE, LOCK_SKIP], ["20260806-023000"])
    assert _evaluate(root) == []

    (root / "daily" / "20260806-025000").mkdir()
    with (root / "backup.log").open("a", encoding="utf-8") as log:
        log.write("[2026-08-06 02:50:00] ok snapshot=20260806-025000 size=924K\n")
    assert _evaluate(root) == []


def test_recent_lock_skip_does_not_refresh_a_stale_success(tmp_path):
    root = _root(tmp_path, [
        "[2026-07-23 02:36:54] ok snapshot=20260723-023654 size=172K",
        LOCK_SKIP,
    ], ["20260723-023654"])
    problems = _evaluate(root)
    assert len(problems) == 1 and problems[0].startswith("last_backup_stale ")


def test_lock_skip_does_not_reset_failures_since_last_success(tmp_path):
    root = _root(tmp_path, [
        OK_LINE,
        "[2026-08-06 02:35:00] FAIL code=base_snapshot_failed rc=1",
        LOCK_SKIP,
        "[2026-08-06 02:50:00] FAIL code=base_snapshot_failed rc=1",
    ], ["20260806-023000"])
    assert _evaluate(root) == ["consecutive_failures count=2 limit=1"]


def test_only_lock_skip_has_no_successful_backup(tmp_path, capsys):
    root = _root(tmp_path, [LOCK_SKIP], [])
    assert _evaluate(root) == ["no_successful_backup_ever entries=1"]
    assert health.main(["--backup-root", str(root), "--min-free-mb", "0"]) == 1
    assert "UNHEALTHY no_successful_backup_ever" in capsys.readouterr().out


@pytest.mark.parametrize("body", [
    "SKIP", "SKIP code=unknown", "SKIP code=backup_lock_busy_extra",
    "SKIP code=backup_lock_busy snapshot=20260806-023000",
    "SKIP  code=backup_lock_busy", "SKIP code=backup_lock_busy ",
    "SKIP code=backup_lock_busy\ufffd", "SKIPPED code=backup_lock_busy",
])
def test_unknown_or_extended_skip_remains_unevaluable(tmp_path, body):
    root = _root(tmp_path, [OK_LINE, f"[2026-08-06 02:40:00] {body}"],
                 ["20260806-023000"])
    with pytest.raises(health.Unevaluable, match="^backup_log_line_unparsable$"):
        _evaluate(root)


def test_lock_skip_still_requires_a_valid_timestamp(tmp_path):
    root = _root(tmp_path, [
        OK_LINE, "[2026-02-30 02:40:00] SKIP code=backup_lock_busy",
    ], ["20260806-023000"])
    with pytest.raises(health.Unevaluable, match="^backup_log_timestamp_invalid$"):
        _evaluate(root)


def test_lock_skip_does_not_hide_missing_snapshot_or_low_space(tmp_path):
    root = _root(tmp_path, [OK_LINE, LOCK_SKIP], [])
    problems = _evaluate(root, min_free_mb=2**40)
    assert "published_snapshot_missing name=20260806-023000" in problems
    assert any(problem.startswith("disk_space_low ") for problem in problems)


def test_log_says_ok_but_the_snapshot_is_not_on_disk(tmp_path):
    # 只信日志会把"发布后被人删掉/被 rotate 吃掉"读成健康。
    root = _root(tmp_path, [OK_LINE], [])

    assert _evaluate(root) == ["published_snapshot_missing name=20260806-023000"]


def test_snapshot_path_that_is_a_symlink_is_not_accepted(tmp_path):
    root = _root(tmp_path, [OK_LINE], ["real"])
    (root / "daily" / "20260806-023000").symlink_to(root / "daily" / "real")

    assert _evaluate(root) == ["published_snapshot_missing name=20260806-023000"]


def test_never_succeeded_is_its_own_verdict(tmp_path):
    root = _root(tmp_path, [
        "[2026-08-05 19:30:00] FAIL code=base_snapshot_failed rc=1",
        "[2026-08-06 02:30:00] FAIL code=base_snapshot_failed rc=1",
    ], [])

    assert _evaluate(root) == ["no_successful_backup_ever entries=2"]


def test_low_disk_space_is_reported(tmp_path):
    root = _root(tmp_path, [OK_LINE], ["20260806-023000"])

    problems = _evaluate(root, min_free_mb=2**40)

    assert len(problems) == 1
    assert problems[0].startswith("disk_space_low ")


LINKED_OK_LINE = (OK_LINE + " source_bytes=4096 source_dirs=3 reserve_bytes=0 "
                  "linked=3 new_bytes=2048 cmp_mismatch=0 outside_links=0")


def test_linked_backup_with_clean_counts_is_healthy(tmp_path):
    root = _root(tmp_path, [LINKED_OK_LINE], ["20260806-023000"])

    assert _evaluate(root) == []


def test_kept_recording_whose_bytes_changed_is_reported(tmp_path):
    line = LINKED_OK_LINE.replace("cmp_mismatch=0", "cmp_mismatch=2")
    root = _root(tmp_path, [line], ["20260806-023000"])

    assert _evaluate(root) == ["audio_cmp_mismatch count=2 snapshot=20260806-023000"]


def test_kept_recording_with_a_link_outside_daily_is_reported(tmp_path):
    line = LINKED_OK_LINE.replace("outside_links=0", "outside_links=1")
    root = _root(tmp_path, [line], ["20260806-023000"])

    assert _evaluate(root) == ["audio_outside_links count=1 snapshot=20260806-023000"]


def _linked_ok(stamp: str, *, cmp_mismatch: int = 0, outside_links: int = 0) -> str:
    when = datetime.strptime(stamp, "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    return (f"[{when}] ok snapshot={stamp} size=924K config=ok keep=14 source_bytes=4096 "
            f"source_dirs=3 reserve_bytes=0 linked=3 new_bytes=2048 "
            f"cmp_mismatch={cmp_mismatch} outside_links={outside_links}")


def test_link_anomaly_hidden_by_a_later_backup_inside_the_window_is_still_reported(tmp_path):
    # 手动补跑(或第二天那份)链到上一份另存的新副本,计数归零;只看最后一行就漏报。
    root = _root(tmp_path, [
        _linked_ok("20260806-010000", cmp_mismatch=1, outside_links=4),
        _linked_ok("20260806-023000"),
    ], ["20260806-010000", "20260806-023000"])

    assert _evaluate(root) == [
        "audio_cmp_mismatch count=1 snapshot=20260806-010000",
        "audio_outside_links count=4 snapshot=20260806-010000",
    ]


def test_link_anomaly_older_than_the_window_is_no_longer_reported(tmp_path):
    # 窗口 = --max-age-hours(30 h):每天一次的检查之间不留空档;夜里那次的异常当晚和第二晚各报一次。
    root = _root(tmp_path, [
        _linked_ok("20260804-193000", cmp_mismatch=1),
        _linked_ok("20260805-193000"),
    ], ["20260804-193000", "20260805-193000"])

    assert _evaluate(root) == []


def test_unreadable_log_is_unevaluable_not_healthy(tmp_path):
    root = tmp_path / "backups"
    (root / "daily").mkdir(parents=True)

    with pytest.raises(health.Unevaluable) as excinfo:
        _evaluate(root)

    assert "backup_log_unreadable" in str(excinfo.value)


def test_garbage_line_is_unevaluable_not_silently_skipped(tmp_path):
    root = _root(tmp_path, [OK_LINE, "这一行不是审计格式"], ["20260806-023000"])

    with pytest.raises(health.Unevaluable) as excinfo:
        _evaluate(root)

    assert str(excinfo.value) == "backup_log_line_unparsable"


def test_cli_exit_codes_and_state_file(tmp_path, capsys):
    root = _root(tmp_path, [OK_LINE], ["20260806-023000"])
    state = tmp_path / "health.state"

    code = health.main([
        "--backup-root", str(root), "--min-free-mb", "0",
        "--max-age-hours", "1000000", "--state-file", str(state)])

    assert code == 0
    assert "HEALTHY" in capsys.readouterr().out
    assert "HEALTHY" in state.read_text(encoding="utf-8")
    assert state.stat().st_mode & 0o777 == 0o600


def test_cli_reports_nonzero_when_stale(tmp_path, capsys):
    root = _root(
        tmp_path,
        ["[2026-07-23 02:36:54] ok snapshot=20260723-023654 size=172K"],
        ["20260723-023654"])

    code = health.main([
        "--backup-root", str(root), "--min-free-mb", "0", "--max-age-hours", "30"])

    assert code == 1
    assert "UNHEALTHY" in capsys.readouterr().out


def test_cli_reports_two_when_facts_are_unreadable(tmp_path, capsys):
    root = tmp_path / "backups"
    (root / "daily").mkdir(parents=True)

    code = health.main(["--backup-root", str(root)])

    assert code == 2
    assert "UNHEALTHY backup_log_unreadable" in capsys.readouterr().out


BOOT_CLOCK = 50_000.0  # 单调时钟此刻：开机后 50000 秒


def _probe(**properties: str):
    """假 `systemctl show nmu-health.service`。"""
    values = {"ActiveState": "inactive", "Result": "success", "ExecMainStatus": "0",
              "ExecMainExitTimestampMonotonic": "0", **properties}

    def run(argv, **_kwargs):
        assert argv[:3] == ["systemctl", "show", "nmu-health.service"], argv
        return SimpleNamespace(returncode=0, stderr="", stdout="".join(
            f"{key}={value}\n" for key, value in values.items()))

    return run


def _exited(minutes_ago: float) -> str:
    return str(int((BOOT_CLOCK - minutes_ago * 60) * 1_000_000))


def _staleness(run) -> list[str]:
    return health.probe_staleness(
        "nmu-health.service", max_age_minutes=30, run=run, monotonic=lambda: BOOT_CLOCK)


def test_a_probe_that_succeeded_minutes_ago_is_fresh():
    assert _staleness(_probe(ExecMainExitTimestampMonotonic=_exited(9))) == []


def test_a_probe_whose_last_success_is_over_thirty_minutes_old_is_stale():
    problems = _staleness(_probe(ExecMainExitTimestampMonotonic=_exited(47)))

    assert problems == ["probe_stale unit=nmu-health.service age_minutes=47.0 limit=30"]


def test_a_probe_that_never_ran_since_boot_is_stale():
    # 2026-09-30 重启后的原样：定时器停在 elapsed，服务在这次开机里一次都没跑过。
    assert _staleness(_probe()) == ["probe_never_ran_since_boot unit=nmu-health.service"]


def test_a_failed_last_probe_is_not_a_recent_success():
    problems = _staleness(_probe(
        Result="exit-code", ExecMainStatus="22", ExecMainExitTimestampMonotonic=_exited(3)))

    assert problems == [
        "probe_last_run_failed unit=nmu-health.service result=exit-code status=22"]


def test_a_probe_running_right_now_is_alive():
    # 起跑时 systemd 把上一轮的退出时间清零，此刻的 0 不是「从没跑过」。
    assert _staleness(_probe(ActiveState="activating")) == []


def test_probe_state_that_cannot_be_read_is_a_problem_not_health():
    def run(argv, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "systemctl")

    problems = _staleness(run)

    assert len(problems) == 1
    assert problems[0].startswith("probe_unevaluable unit=nmu-health.service ")


def test_cli_reports_a_stale_probe_even_when_the_backup_is_fresh(tmp_path, capsys):
    root = _root(tmp_path, [OK_LINE], ["20260806-023000"])
    state = tmp_path / "health.state"

    code = health.main(
        ["--backup-root", str(root), "--min-free-mb", "0", "--max-age-hours", "1000000",
         "--probe-unit", "nmu-health.service", "--state-file", str(state)],
        run=_probe(ExecMainExitTimestampMonotonic=_exited(47)), monotonic=lambda: BOOT_CLOCK)

    assert code == 1
    assert "UNHEALTHY probe_stale unit=nmu-health.service" in capsys.readouterr().out
    assert "probe_stale" in state.read_text(encoding="utf-8")


def test_cli_keeps_both_verdicts_when_the_log_is_unreadable_and_the_probe_stale(
        tmp_path, capsys):
    root = tmp_path / "backups"
    (root / "daily").mkdir(parents=True)

    code = health.main(
        ["--backup-root", str(root), "--probe-unit", "nmu-health.service"],
        run=_probe(), monotonic=lambda: BOOT_CLOCK)

    assert code == 2
    output = capsys.readouterr().out
    assert "UNHEALTHY backup_log_unreadable" in output
    assert "probe_never_ran_since_boot" in output


def test_cli_without_probe_unit_never_asks_systemd(tmp_path):
    root = _root(tmp_path, [OK_LINE], ["20260806-023000"])

    def run(argv, **_kwargs):
        raise AssertionError(f"没给 --probe-unit 却查了 systemd：{argv}")

    assert health.main(["--backup-root", str(root), "--min-free-mb", "0",
                        "--max-age-hours", "1000000"], run=run) == 0


OFFBOX = "147.182.176.237"
OFFBOX_HIT = f'INFO:     {OFFBOX}:0 - "GET /health HTTP/1.1" 200 OK\n'


def _journal(stdout: str = "", *, returncode: int = 0):
    """假 `journalctl -u nmu --since=… -o cat`:uvicorn 访问日志的原样。"""
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        assert argv[:3] == ["journalctl", "-u", "nmu"], argv
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    run.calls = calls
    return run


def test_one_offbox_probe_request_in_the_window_means_it_is_alive():
    run = _journal('INFO:     127.0.0.1:0 - "GET /live/state HTTP/1.1" 200 OK\n' + OFFBOX_HIT)

    assert health.offbox_silence(OFFBOX, max_age_minutes=30, run=run) == []
    assert run.calls == [
        ["journalctl", "-u", "nmu", "--since=-1800s", "-o", "cat", "--no-pager"]]


def test_other_sources_and_other_paths_are_not_the_offbox_probe():
    run = _journal(
        'INFO:     203.0.113.9:0 - "GET /health HTTP/1.1" 200 OK\n'
        f'INFO:     {OFFBOX}:0 - "GET /health-selftest HTTP/1.1" 404 Not Found\n'
        f'INFO:     {OFFBOX}:0 - "GET /health/ready HTTP/1.1" 200 OK\n'
        f'INFO:     {OFFBOX}:0 - "HEAD /health HTTP/1.1" 200 OK\n'
        f'INFO:     127.0.0.1:0 - "GET /health?from={OFFBOX} HTTP/1.1" 200 OK\n')

    assert health.offbox_silence(OFFBOX, max_age_minutes=30, run=run) == [
        f"offbox_probe_silent source={OFFBOX} window_minutes=30"]
    # 来源只认整段 IP:47.182.176.237 不是 147.182.176.237 的一截。
    assert health.offbox_silence("47.182.176.237", max_age_minutes=30,
                                 run=_journal(OFFBOX_HIT)) == [
        "offbox_probe_silent source=47.182.176.237 window_minutes=30"]


def test_an_unreadable_journal_is_a_problem_not_a_live_probe():
    failed = health.offbox_silence(OFFBOX, max_age_minutes=30, run=_journal(returncode=1))

    def missing(argv, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "journalctl")

    absent = health.offbox_silence(OFFBOX, max_age_minutes=30, run=missing)

    assert failed == [f"offbox_probe_unevaluable source={OFFBOX} rc=1"]
    assert absent == [f"offbox_probe_unevaluable source={OFFBOX} err=FileNotFoundError"]


def test_cli_reports_a_silent_offbox_probe_even_when_backup_and_local_probe_are_fine(
        tmp_path, capsys):
    root = _root(tmp_path, [OK_LINE], ["20260806-023000"])
    state = tmp_path / "health.state"
    local = _probe(ExecMainExitTimestampMonotonic=_exited(9))

    def run(argv, **kwargs):
        if argv[0] == "journalctl":
            return _journal('INFO:     127.0.0.1:0 - "GET /health HTTP/1.1" 200 OK\n')(argv)
        return local(argv, **kwargs)

    code = health.main(
        ["--backup-root", str(root), "--min-free-mb", "0", "--max-age-hours", "1000000",
         "--probe-unit", "nmu-health.service", "--offbox-source", OFFBOX,
         "--state-file", str(state)],
        run=run, monotonic=lambda: BOOT_CLOCK)

    assert code == 1
    assert f"UNHEALTHY offbox_probe_silent source={OFFBOX}" in capsys.readouterr().out
    assert "offbox_probe_silent" in state.read_text(encoding="utf-8")
    assert health.main(
        ["--backup-root", str(root), "--min-free-mb", "0", "--max-age-hours", "1000000",
         "--offbox-source", OFFBOX], run=_journal(OFFBOX_HIT)) == 0
