from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import shutil

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
    return root


def _usage(total: int, used: int, free: int):
    return lambda _path: shutil._ntuple_diskusage(total, used, free)  # noqa: SLF001


def test_series_reads_the_timestamped_snapshots_in_order(tmp_path):
    root = _backups(tmp_path, [100, 200, 300])

    series = capacity.snapshot_series(root / "daily")

    assert [size for _, size in series] == [100, 200, 300]
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

    assert per_day == 1000.0


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

    assert facts["days_left"] == 5.0
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
                        _usage(1_000_000, 999_000, 1_000))

    facts = capacity.evaluate(root, None, max_used_pct=99.9, min_free_mb=0,
                              min_days_left=30.0)

    assert facts["days_left"] is None
    assert facts["problems"] == []


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
