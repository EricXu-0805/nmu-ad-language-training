#!/usr/bin/env python3
"""在磁盘写满之前就把它说出来，而不是等某一夜备份 fail-closed 才发现。

`backup_health_check.py` 只回答"现在还剩多少"。这个脚本回答"照这个速度还能撑
几天"——因为真正的事故形态是：录音和每日全量快照一起长，某一天备份写不下，
从那晚起备份静默失败，而剩余空间在失败前一天看还很宽裕。

增长速率从 `daily/` 下已有快照的**时间戳和实际字节数**回归出来，不靠猜：
每份快照目录名就是它的时刻，所以这本身就是一条真实的时间序列。

这个速率只含备份的增长。live `data/` 自己也在长（新录音先落在那里，才被下一份
快照复制走），所以 `days_left` 是**偏乐观的上界**，不是精确剩余寿命。真正逼近
写满时，实际到期会比它早。

判据（任一不成立即非零退出）：
  - 文件系统占用率不超过 --max-used-pct；
  - 剩余空间不低于 --min-free-mb；
  - 按实测增长速率，距离写满不少于 --min-days-left 天。

只用标准库。
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys

STAMP_FORMAT = "%Y%m%d-%H%M%S"


def directory_bytes(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        try:
            meta = child.lstat()
        except OSError:
            continue
        if child.is_symlink() or not child.is_file():
            continue
        total += meta.st_size
    return total


def snapshot_series(daily: Path) -> list[tuple[datetime, int]]:
    """按时间排好的 (快照时刻, 该快照字节数)。名字不合规的目录直接跳过。"""
    series: list[tuple[datetime, int]] = []
    if not daily.is_dir():
        return series
    for child in sorted(daily.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        try:
            when = datetime.strptime(child.name, STAMP_FORMAT)
        except ValueError:
            continue
        series.append((when, directory_bytes(child)))
    return series


def growth_bytes_per_day(series: list[tuple[datetime, int]]) -> float | None:
    """一天新增多少字节。少于两份快照就给不出速率，返回 None 而不是猜 0。

    用首尾两点而不是最小二乘：快照集是滚动保留的，中间被 rotate 掉的份数会让
    拟合出来的斜率偏小，而这个数字偏小的方向恰恰是"报得太乐观"。
    """
    if len(series) < 2:
        return None
    (first_at, _), (last_at, _) = series[0], series[-1]
    span_days = (last_at - first_at).total_seconds() / 86400
    if span_days <= 0:
        return None
    per_snapshot = sum(size for _, size in series) / len(series)
    snapshots_per_day = (len(series) - 1) / span_days
    return per_snapshot * snapshots_per_day


def evaluate(backup_root: Path, data_dir: Path | None, *, max_used_pct: float,
             min_free_mb: int, min_days_left: float) -> dict[str, object]:
    usage = shutil.disk_usage(backup_root)
    used_pct = 100.0 * usage.used / usage.total if usage.total else 100.0
    free_mb = usage.free // (1024 * 1024)

    series = snapshot_series(backup_root / "daily")
    per_day = growth_bytes_per_day(series)
    days_left = (usage.free / per_day) if per_day and per_day > 0 else None

    problems: list[str] = []
    if used_pct > max_used_pct:
        problems.append(f"disk_used_high pct={used_pct:.1f} limit={max_used_pct}")
    if free_mb < min_free_mb:
        problems.append(f"disk_free_low free_mb={free_mb} limit={min_free_mb}")
    if days_left is not None and days_left < min_days_left:
        problems.append(
            f"projected_full_soon days_left={days_left:.1f} limit={min_days_left}")

    facts: dict[str, object] = {
        "used_pct": round(used_pct, 1),
        "free_mb": free_mb,
        "snapshots": len(series),
        "backup_growth_mb_per_day": (
            round(per_day / (1024 * 1024), 2) if per_day is not None else None),
        "days_left": round(days_left, 1) if days_left is not None else None,
        "problems": problems,
    }
    if data_dir is not None and data_dir.is_dir():
        facts["data_dir_mb"] = directory_bytes(data_dir) // (1024 * 1024)
    return facts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-root", type=Path, default=Path("/opt/nmu/backups"))
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--max-used-pct", type=float, default=85.0)
    parser.add_argument("--min-free-mb", type=int, default=2048)
    parser.add_argument("--min-days-left", type=float, default=30.0)
    parser.add_argument("--state-file", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        facts = evaluate(
            args.backup_root, args.data_dir, max_used_pct=args.max_used_pct,
            min_free_mb=args.min_free_mb, min_days_left=args.min_days_left)
    except OSError as error:
        print(f"[{datetime.now():%F %T}] UNHEALTHY capacity_probe_failed "
              f"err={error.errno}")
        return 2

    problems = facts["problems"]
    assert isinstance(problems, list)
    verdict = "HEALTHY" if not problems else "UNHEALTHY " + "; ".join(problems)
    growth = facts["backup_growth_mb_per_day"]
    line = (f"[{datetime.now():%F %T}] {verdict} "
            f"used={facts['used_pct']}% free={facts['free_mb']}MB "
            f"growth={growth if growth is not None else '未知'}MB/d "
            f"days_left={facts['days_left'] if facts['days_left'] is not None else '未知'}")

    print(json.dumps(facts, ensure_ascii=False, indent=2) if args.json else line)
    if args.state_file is not None:
        try:
            args.state_file.write_text(line + "\n", encoding="utf-8")
            args.state_file.chmod(0o600)
        except OSError as error:
            print(f"state_file_write_failed err={error.errno}", file=sys.stderr)
            return 1
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
