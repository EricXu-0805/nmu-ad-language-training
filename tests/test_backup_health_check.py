from __future__ import annotations

from datetime import datetime
import importlib.util
from pathlib import Path

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
