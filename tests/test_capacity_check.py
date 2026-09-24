from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
import errno
from pathlib import Path
import shutil
import stat
from types import SimpleNamespace

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "capacity_check",
    Path(__file__).resolve().parents[1] / "scripts" / "capacity_check.py")
assert _SPEC is not None and _SPEC.loader is not None
capacity = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(capacity)


def _backups(tmp_path: Path, sizes_by_day: list[int]) -> Path:
    root = tmp_path / "backups"
    daily = root / "daily"
    daily.mkdir(parents=True)
    start = datetime(2026, 8, 1, 3, 30, 0)
    for index, size in enumerate(sizes_by_day):
        stamp = (start + timedelta(days=index)).strftime(capacity.STAMP_FORMAT)
        snapshot = daily / stamp
        snapshot.mkdir()
        (snapshot / "app.db").write_bytes(b"x" * size)
    if sizes_by_day:
        last = start + timedelta(days=len(sizes_by_day) - 1)
        (root / "backup.log").write_text(
            f"[{last:%F %T}] ok snapshot={last:%Y%m%d-%H%M%S} "
            "size=1M config=ok keep=14\n")
    return root


def _usage(total: int, used: int, free: int):
    return lambda _path: shutil._ntuple_diskusage(total, used, free)  # noqa: SLF001


def test_series_reads_the_timestamped_snapshots_in_order(tmp_path):
    root = _backups(tmp_path, [100, 200, 300])

    series = capacity.snapshot_series(root / "daily")

    snapshots = sorted((root / "daily").iterdir())
    assert [size for _, size in series] == [
        max((path / "app.db").stat().st_size, (path / "app.db").stat().st_blocks * 512)
        + path.stat().st_blocks * 512 for path in snapshots]
    assert series[0][0] < series[-1][0]


def test_directories_that_are_not_timestamps_are_skipped(tmp_path):
    root = _backups(tmp_path, [100])
    (root / "daily" / "quarantine-junk").mkdir()

    assert len(capacity.snapshot_series(root / "daily")) == 1


def test_a_symlinked_snapshot_is_not_counted(tmp_path):
    root = _backups(tmp_path, [100])
    (root / "daily" / "20260901-033000").symlink_to(root / "daily" / "20260801-033000")

    assert len(capacity.snapshot_series(root / "daily")) == 1


def test_growth_needs_two_points_and_says_so_instead_of_guessing_zero(tmp_path):
    root = _backups(tmp_path, [100])

    assert capacity.growth_bytes_per_day(capacity.snapshot_series(root / "daily")) is None


def test_growth_rate_matches_one_snapshot_per_day(tmp_path):
    # 三份等大快照、每天一份 → 一天新增就是一份的大小。
    root = _backups(tmp_path, [1000, 1000, 1000])

    per_day = capacity.growth_bytes_per_day(capacity.snapshot_series(root / "daily"))

    snapshot = root / "daily" / "20260801-033000"
    assert per_day == (max(1000, (snapshot / "app.db").stat().st_blocks * 512)
                       + snapshot.stat().st_blocks * 512)


def test_a_full_disk_is_reported(tmp_path, monkeypatch):
    root = _backups(tmp_path, [1000, 1000])
    monkeypatch.setattr(capacity.shutil, "disk_usage",
                        _usage(100_000, 95_000, 5_000))

    facts = capacity.evaluate(root, None, max_used_pct=85.0, min_free_mb=0,
                              min_days_left=0.0)

    assert any(p.startswith("disk_used_high") for p in facts["problems"])


def test_room_today_but_full_next_week_is_still_a_failure(tmp_path, monkeypatch):
    # 这条是真正的事故形态:今天看剩余空间宽裕,增长速率却让它下周就写满,
    # 而备份一旦写不下就从那晚起静默失败。
    root = _backups(tmp_path, [10_000_000, 10_000_000, 10_000_000])
    monkeypatch.setattr(capacity.shutil, "disk_usage",
                        _usage(1_000_000_000, 950_000_000, 50_000_000))

    facts = capacity.evaluate(root, None, max_used_pct=99.0, min_free_mb=0,
                              min_days_left=30.0)

    assert facts["days_left"] == 4  # Next scheduled copy may start immediately.
    assert any(p.startswith("projected_full_soon") for p in facts["problems"])


def test_a_healthy_disk_reports_no_problems(tmp_path, monkeypatch):
    root = _backups(tmp_path, [1_000, 1_000])
    monkeypatch.setattr(capacity.shutil, "disk_usage",
                        _usage(1_000_000_000, 100_000_000, 900_000_000))

    facts = capacity.evaluate(root, None, max_used_pct=85.0, min_free_mb=1,
                              min_days_left=30.0)

    assert facts["problems"] == []


def test_unknown_growth_never_invents_a_days_left_number(tmp_path, monkeypatch):
    root = _backups(tmp_path, [1_000])
    monkeypatch.setattr(capacity.shutil, "disk_usage",
                        _usage(1_000_000, 100_000, 900_000))

    facts = capacity.evaluate(root, None, max_used_pct=99.9, min_free_mb=0,
                              min_days_left=30.0)

    assert facts["days_left"] is None
    assert facts["next_snapshot_copy_fits"] is True
    assert facts["projection_status"] == "insufficient_history"
    assert any(p.startswith("projection_history_insufficient") for p in facts["problems"])


def test_data_directory_size_is_reported_when_given(tmp_path, monkeypatch):
    root = _backups(tmp_path, [1_000, 1_000])
    data = tmp_path / "data"
    (data / "audio").mkdir(parents=True)
    (data / "audio" / "a.webm").write_bytes(b"y" * (3 * 1024 * 1024))
    monkeypatch.setattr(capacity.shutil, "disk_usage",
                        _usage(1_000_000_000, 1_000, 999_999_000))

    facts = capacity.evaluate(data.parent / "backups", data, max_used_pct=85.0,
                              min_free_mb=0, min_days_left=0.0)

    assert facts["data_dir_mb"] == 3
    assert root.is_dir()


def test_cli_exit_codes(tmp_path, monkeypatch, capsys):
    root = _backups(tmp_path, [1_000, 1_000])
    monkeypatch.setattr(capacity.shutil, "disk_usage",
                        _usage(1_000_000_000, 100_000_000, 900_000_000))
    state = tmp_path / "capacity.state"

    healthy = capacity.main([
        "--backup-root", str(root), "--min-free-mb", "1",
        "--state-file", str(state)])
    assert healthy == 0
    assert "HEALTHY" in capsys.readouterr().out
    assert state.stat().st_mode & 0o777 == 0o600

    monkeypatch.setattr(capacity.shutil, "disk_usage",
                        _usage(100_000, 99_000, 1_000))
    unhealthy = capacity.main(["--backup-root", str(root)])
    assert unhealthy == 1
    assert "UNHEALTHY" in capsys.readouterr().out


def _series(sizes_mb: list[int]) -> list[tuple[datetime, int]]:
    start = datetime(2026, 8, 1, 3, 30)
    return [(start + timedelta(days=day), size * capacity.MIB)
            for day, size in enumerate(sizes_mb)]


def test_full_retention_window_has_write_cost_without_permanent_accumulation():
    # 14 x 100 MiB stay 1400 MiB after each successful rotation. The old model
    # wrongly exhausted 200 MiB after two days; actual copy peak needs 100 MiB.
    result = capacity.retention_projection(
        _series([100] * 14), keep=14, horizon_days=30, free_bytes=200 * capacity.MIB)

    assert result["backup_peak_additional_mb"] == 100
    assert result["backup_net_change_mb"] == 0
    assert result["backup_net_retained_growth_mb_per_day"] == 0
    assert result["days_left"] is None
    assert result["projection_status"] == "no_exhaustion_within_horizon"


def test_growing_snapshots_retain_growth_and_fail_even_with_rotation():
    # Each retained snapshot grows 10 MiB/day, hence 14 x 10 = 140 net MiB/day.
    # At event t, pre-rotation peak = 150*t + 90 MiB: event 29 exceeds 4400.
    result = capacity.retention_projection(
        _series(list(range(100, 240, 10))), keep=14, horizon_days=30,
        free_bytes=4400 * capacity.MIB)

    assert result["snapshot_size_growth_mb_per_day"] == 10
    assert result["backup_net_retained_growth_mb_per_day"] == 140
    assert result["backup_net_change_mb"] == 4200
    assert result["backup_peak_additional_mb"] == 4590
    assert result["days_left"] == 28  # First event can happen immediately.
    assert result["projection_status"] == "exhausted_within_horizon"


def test_same_day_release_snapshots_do_not_multiply_the_scheduled_daily_rate():
    series = _series([100] * 12)
    last = series[-1][0]
    series += [(last + timedelta(hours=1), 100 * capacity.MIB),
               (last + timedelta(hours=2), 100 * capacity.MIB)]

    result = capacity.retention_projection(
        series, keep=14, horizon_days=30, free_bytes=200 * capacity.MIB)

    assert capacity.growth_bytes_per_day(series) > 100 * capacity.MIB
    assert len(series) == 14
    assert result["observed_snapshot_days"] == 12
    assert result["backup_peak_additional_mb"] == 100
    assert result["backup_net_change_mb"] == 0
    assert result["days_left"] is None


def test_same_day_larger_copy_still_contributes_to_size_trend_and_current_queue():
    series = _series([100] * 12)
    last = series[-1][0]
    series += [(last + timedelta(hours=1), 150 * capacity.MIB),
               (last + timedelta(hours=2), 200 * capacity.MIB)]

    result = capacity.retention_projection(
        series, keep=14, horizon_days=1, free_bytes=1000 * capacity.MIB)

    assert result["snapshot_size_growth_mb_per_day"] == 100
    assert result["backup_peak_additional_mb"] == 300
    assert result["backup_net_change_mb"] == 200  # All three same-day copies remain.


def test_partial_retention_window_must_first_pay_for_the_missing_snapshots():
    result = capacity.retention_projection(
        _series([100] * 3), keep=14, horizon_days=30, free_bytes=1150 * capacity.MIB)

    assert result["backup_net_retained_growth_mb_per_day"] == 0
    assert result["backup_net_change_mb"] == 1100
    assert result["backup_peak_additional_mb"] == 1200
    assert result["days_left"] == 11


def test_partial_window_stops_accumulating_after_retention_fills():
    result = capacity.retention_projection(
        _series([100] * 3), keep=14, horizon_days=30, free_bytes=1250 * capacity.MIB)

    assert result["backup_peak_additional_mb"] == 1200
    assert result["backup_net_change_mb"] == 1100
    assert result["days_left"] is None


def test_rotation_cannot_release_old_snapshot_before_new_copy_is_complete():
    result = capacity.retention_projection(
        _series([100] * 14), keep=14, horizon_days=30, free_bytes=99 * capacity.MIB)

    assert result["backup_net_change_mb"] == 0
    assert result["backup_peak_additional_mb"] == 100
    assert result["days_left"] == 0


def test_many_snapshots_on_only_one_date_do_not_establish_daily_size_growth():
    start = datetime(2026, 8, 1, 3, 30)
    series = [(start + timedelta(minutes=n), 100 * capacity.MIB) for n in range(14)]
    result = capacity.retention_projection(
        series, keep=14, horizon_days=30, free_bytes=200 * capacity.MIB)

    assert result["observed_snapshot_days"] == 1
    assert result["snapshot_size_growth_mb_per_day"] is None
    assert result["backup_peak_additional_mb"] is None
    assert result["days_left"] is None
    assert result["projection_status"] == "insufficient_history"


def test_recent_size_growth_is_not_hidden_by_long_stable_history():
    result = capacity.retention_projection(
        _series([100] * 13 + [200]), keep=14, horizon_days=30,
        free_bytes=1000 * capacity.MIB)

    assert result["snapshot_size_growth_mb_per_day"] == 100
    assert result["days_left"] == 2  # Third copy reaches the entire remaining 1000 MiB.


def test_shrinking_history_never_assumes_unmeasured_future_space_recovery():
    result = capacity.retention_projection(
        _series([200, 150, 100]), keep=14, horizon_days=30,
        free_bytes=1250 * capacity.MIB)

    assert result["snapshot_size_growth_mb_per_day"] == 0
    assert result["backup_peak_additional_mb"] == 1200
    assert result["backup_net_change_mb"] == 950


def test_evaluate_reports_backup_scope_and_unknown_total_runway(tmp_path, monkeypatch):
    root = _backups(tmp_path, [1000] * 14)
    monkeypatch.setattr(capacity.shutil, "disk_usage", _usage(100_000, 80_000, 20_000))

    facts = capacity.evaluate(root, None, max_used_pct=85, min_free_mb=0,
                              min_days_left=30)

    assert facts["problems"] == []
    assert facts["days_left"] is None
    assert facts["total_days_left"] is None
    assert facts["projection_scope"] == "scheduled_daily_backup_retention_only"
    assert facts["unmeasured_growth"] == ["live_data", "other_logs_and_files",
                                          "unattributed_filesystem_metadata"]
    assert facts["backup_growth_mb_per_day"] == facts["backup_gross_write_mb_per_day"]
    assert facts["snapshot_measurement"] == "max_logical_allocated_files_plus_allocated_directories"
    assert facts["rotation_credit_measurement"] == "existing_allocated_bytes_only"
    assert facts["retention_contract_status"] == "keep_matches_success_log"


def test_failed_rotation_cannot_be_treated_as_an_ordinary_14_snapshot_window(tmp_path, monkeypatch):
    root = _backups(tmp_path, [1000] * 15)
    monkeypatch.setattr(capacity.shutil, "disk_usage", _usage(1_000_000, 1000, 999_000))

    facts = capacity.evaluate(root, None, max_used_pct=85, min_free_mb=0,
                              min_days_left=30)

    assert facts["snapshots"] == 15
    assert any(p.startswith("retention_count_exceeded") for p in facts["problems"])


def test_configured_keep_must_match_last_successful_backup_contract(tmp_path, monkeypatch):
    root = _backups(tmp_path, [1000] * 14)
    (root / "backup.log").write_text(
        "[2026-08-14 03:30:00] ok snapshot=20260814-033000 size=1M config=ok keep=14\n"
        "[2026-08-15 03:30:00] ok snapshot=20260815-033000 size=1M config=ok keep=28\n"
        "[2026-08-16 03:30:00] FAIL code=source_size_probe_failed\n")
    monkeypatch.setattr(capacity.shutil, "disk_usage", _usage(1_000_000, 1000, 999_000))

    mismatched = capacity.evaluate(root, None, max_used_pct=85, min_free_mb=0,
                                   min_days_left=30)
    matched = capacity.evaluate(root, None, max_used_pct=85, min_free_mb=0,
                                min_days_left=30, keep=28)

    assert mismatched["observed_retention_keep"] == 28
    assert any(p.startswith("retention_keep_mismatch") for p in mismatched["problems"])
    assert matched["retention_keep"] == 28
    assert matched["problems"] == []


def test_default_retention_and_daily_cadence_match_shipped_backup_contract():
    repo = Path(__file__).resolve().parents[1]
    daily_script = (repo / "scripts/vps-backup-daily.sh").read_text()
    timer = (repo / "deploy/systemd/nmu-backup.timer").read_text()
    service = (repo / "deploy/systemd/nmu-capacity.service").read_text()

    assert f"\nKEEP={capacity.DEFAULT_KEEP}\n" in daily_script
    assert "OnCalendar=*-*-* 19:30:00" in timer
    assert "--keep" not in service  # Service uses the checked default above.


def test_cli_keeps_original_thresholds_and_exposes_scope(tmp_path, monkeypatch, capsys):
    root = _backups(tmp_path, [1000] * 14)
    monkeypatch.setattr(capacity.shutil, "disk_usage",
                        _usage(20_000 * capacity.MIB, 17_001 * capacity.MIB,
                               2999 * capacity.MIB))
    assert capacity.main(["--backup-root", str(root)]) == 1
    assert "disk_used_high" in capsys.readouterr().out

    monkeypatch.setattr(capacity.shutil, "disk_usage",
                        _usage(20_000 * capacity.MIB, 1000 * capacity.MIB,
                               2047 * capacity.MIB))
    assert capacity.main(["--backup-root", str(root)]) == 1
    assert "disk_free_low" in capsys.readouterr().out

    monkeypatch.setattr(capacity.shutil, "disk_usage",
                        _usage(20_000 * capacity.MIB, 1000 * capacity.MIB,
                               19_000 * capacity.MIB))
    assert capacity.main(["--backup-root", str(root)]) == 0
    output = capsys.readouterr().out
    assert "scope=daily_backups_only horizon=30.0d" in output
    assert "total_days_left=未知" in output


def test_one_known_large_snapshot_still_reports_impossible_next_copy(tmp_path, monkeypatch):
    root = _backups(tmp_path, [1000])
    # Keep the filesystem fixture small; feed a measured 3 GiB snapshot baseline.
    stamp = datetime(2026, 8, 1, 3, 30)
    monkeypatch.setattr(capacity, "snapshot_measurements",
                        lambda _path: [(stamp, 3072 * capacity.MIB, 3072 * capacity.MIB)])
    monkeypatch.setattr(capacity.shutil, "disk_usage",
                        _usage(20_000 * capacity.MIB, 17_440 * capacity.MIB,
                               2560 * capacity.MIB))

    facts = capacity.evaluate(root, None, max_used_pct=99, min_free_mb=2048,
                              min_days_left=30)

    assert facts["projection_status"] == "insufficient_history"
    assert facts["snapshot_size_growth_mb_per_day"] is None
    assert facts["days_left"] is None  # No invented multi-day trend.
    assert facts["next_snapshot_baseline_mb"] == 3072
    assert facts["next_snapshot_copy_fits"] is False
    assert any(p.startswith("next_snapshot_copy_insufficient") for p in facts["problems"])
    assert any(p.startswith("projection_history_insufficient") for p in facts["problems"])


def test_unknown_history_fails_cli_without_mislabeling_healthy(tmp_path, monkeypatch, capsys):
    root = _backups(tmp_path, [1000])
    monkeypatch.setattr(capacity.shutil, "disk_usage",
                        _usage(10_000 * capacity.MIB, 1000 * capacity.MIB,
                               9000 * capacity.MIB))

    assert capacity.main(["--backup-root", str(root)]) == 1
    assert "] UNHEALTHY projection_history_insufficient" in capsys.readouterr().out


def test_missing_success_log_leaves_retention_contract_unverified(tmp_path, monkeypatch):
    root = _backups(tmp_path, [1000] * 14)
    (root / "backup.log").unlink()
    monkeypatch.setattr(capacity.shutil, "disk_usage", _usage(1_000_000, 1000, 999_000))

    facts = capacity.evaluate(root, None, max_used_pct=85, min_free_mb=0,
                              min_days_left=30)

    assert facts["observed_retention_keep"] is None
    assert facts["retention_contract_status"] == "unverified"
    assert any(p.startswith("retention_contract_unverified") for p in facts["problems"])


def test_failed_log_detail_cannot_supply_an_observed_success_contract(tmp_path):
    log = tmp_path / "backup.log"
    log.write_text(
        "[2026-08-15 03:30:00] ok snapshot=20260815-033000 size=1M config=ok keep=14\n"
        "[2026-08-16 03:30:00] FAIL code=base_snapshot_failed "
        "detail=ok snapshot=20260816-033000 size=1M config=ok keep=28\n")

    assert capacity.observed_retention_keep(log) == 14


def test_new_success_without_keep_does_not_reuse_an_older_contract(tmp_path):
    log = tmp_path / "backup.log"
    log.write_text(
        "[2026-08-15 03:30:00] ok snapshot=20260815-033000 size=1M config=ok keep=14\n"
        "[2026-08-16 03:30:00] ok snapshot=20260816-033000 size=1M config=ok\n")

    assert capacity.observed_retention_keep(log) is None


def test_copy_budget_includes_small_file_allocation_and_directory_blocks(tmp_path, monkeypatch):
    root = _backups(tmp_path, [1])
    snapshot = root / "daily" / "20260801-033000"
    audio = snapshot / "audio"
    audio.mkdir()
    (audio / "tiny.webm").write_bytes(b"x")
    original_lstat = Path.lstat
    measured = {
        snapshot: SimpleNamespace(st_mode=stat.S_IFDIR, st_blocks=8, st_size=128),
        audio: SimpleNamespace(st_mode=stat.S_IFDIR, st_blocks=8, st_size=128),
        snapshot / "app.db": SimpleNamespace(st_mode=stat.S_IFREG, st_blocks=8, st_size=1),
        audio / "tiny.webm": SimpleNamespace(st_mode=stat.S_IFREG, st_blocks=8, st_size=1),
    }
    monkeypatch.setattr(Path, "lstat", lambda path: measured.get(path) or original_lstat(path))

    assert capacity.snapshot_storage_bytes(snapshot) == (16_384, 16_384)
    assert capacity.snapshot_series(root / "daily")[0][1] == 16_384


def test_sparse_snapshot_copy_budget_cannot_be_credited_as_reclaimed_blocks():
    # Old copies use 1 MiB physically but require up to 100 MiB each to recopy.
    # Crediting 100 MiB when each old file rotates would falsely pass 200 MiB free.
    result = capacity.retention_projection(
        _series([100] * 14), keep=14, horizon_days=30,
        free_bytes=200 * capacity.MIB, existing_allocated=[capacity.MIB] * 14)

    assert result["backup_peak_additional_mb"] == 1486
    assert result["backup_net_change_mb"] == 1386
    assert result["days_left"] == 2


def test_sparse_file_keeps_separate_copy_budget_and_real_allocated_credit(tmp_path, monkeypatch):
    path = tmp_path / "sparse"
    path.write_bytes(b"x")
    monkeypatch.setattr(Path, "lstat", lambda _path: SimpleNamespace(
        st_mode=stat.S_IFREG, st_size=10 * capacity.MIB, st_blocks=8))

    assert capacity.snapshot_storage_bytes(path) == (10 * capacity.MIB, 4096)


def test_platform_without_allocated_block_measurement_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "file"
    path.write_bytes(b"x")
    monkeypatch.setattr(Path, "lstat", lambda _path: SimpleNamespace(
        st_mode=stat.S_IFREG, st_size=1))

    with pytest.raises(OSError, match="snapshot_allocated_size_unavailable") as error:
        capacity.snapshot_storage_bytes(path)
    assert error.value.errno == errno.ENOTSUP
