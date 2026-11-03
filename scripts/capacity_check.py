#!/usr/bin/env python3
"""检查当前磁盘水位与每日备份轮转的有限期容量预测。

每日任务先完整写入新快照，成功发布后保留最近 14 份。预测逐次模拟这个顺序，
不会把所有全量快照的写入量当成永久净增长，也不会把同日发布备份当成每日频率。
快照尺寸趋势取每天最大一份；所有实际快照仍计入当前队列与原始写入速率。

默认门槛保持 85% 占用、2048 MiB 可用、未来 30 天备份写入空间。预测涵盖
按合同成功轮转的每日备份（链接模型下另加 live 录音与库的增长，见下）；其他日志与文件、
未归属到快照块量的元数据增长没有测量。days_left 仅报告预测期内空间不足的时点；None 不代表
无限寿命或整个服务器安全。发布时的额外副本、临时保留目录须另做一次性预算。

备份打开录音硬链接去重（NMU_BACKUP_LINK_AUDIO=1）后，相邻快照共享未变录音的 inode：
共享 inode 只计一次，块量记在最后一个持有它的快照上。最新一份与更早快照共享录音时
改用链接模型：下一份仍先整份暂存（峰值照旧按整份），留下来的只是库等非录音部分加一天
的新录音；未变录音会被链进以后每一份，轮转只抵扣非录音块量。链接模型下 days_left 另计
live 数据的增长：data/audio 是快照之外的另一份，新录音在那里也多一份，库也跟着长，按
快照里录音与非录音两部分的尺寸趋势之和计（live_growth_mb_per_day）；不链接时仍不计。

只用标准库。快照不足两个不同日期时不猜测尺寸趋势。
"""
from __future__ import annotations

import argparse
from datetime import datetime
import errno
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys

STAMP_FORMAT = "%Y%m%d-%H%M%S"
DEFAULT_KEEP = 14
MIB = 1024 * 1024


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


def snapshot_storage_bytes(path: Path, *,
                           shared_inodes: dict[tuple[int, int], list[int]] | None = None,
                           owner: int = 0) -> tuple[int, int]:
    """(复制预算, 已分配字节)：文件取 max(逻辑, 块量)，目录计真实块量。

    现存快照被轮转时只能抵扣第二项，不能把稀疏文件的逻辑长度当可释放空间。
    不支持块计量的平台拒绝预测；读取失败也不能静默漏掉文件。给了 shared_inodes 时，
    多链接文件的块量不计入第二项，改记进该表 [持有者, 块量, st_nlink, 已见链接数]，
    由调用方按最后一个持有者统一抵扣。
    """
    meta = path.lstat()
    if stat.S_ISLNK(meta.st_mode):
        return 0, 0
    if not (stat.S_ISREG(meta.st_mode) or stat.S_ISDIR(meta.st_mode)):
        raise OSError(errno.ENOTSUP, "snapshot_file_type_unsupported", str(path))
    blocks = getattr(meta, "st_blocks", None)
    if blocks is None:
        raise OSError(errno.ENOTSUP, "snapshot_allocated_size_unavailable", str(path))
    allocated = blocks * 512
    if stat.S_ISREG(meta.st_mode):
        links = getattr(meta, "st_nlink", 1)
        if shared_inodes is not None and links > 1:
            record = shared_inodes.setdefault(
                (meta.st_dev, meta.st_ino), [owner, allocated, links, 0])
            record[0] = owner
            record[3] += 1
            return max(meta.st_size, allocated), 0
        return max(meta.st_size, allocated), allocated
    copy_budget = allocated
    for child in path.iterdir():
        child_budget, child_allocated = snapshot_storage_bytes(
            child, shared_inodes=shared_inodes, owner=owner)
        copy_budget += child_budget
        allocated += child_allocated
    return copy_budget, allocated


def _snapshot_directories(daily: Path) -> list[tuple[datetime, Path]]:
    """严格时间戳完成目录，按时间排列；不跟随符号链接。"""
    found: list[tuple[datetime, Path]] = []
    if not daily.is_dir():
        return found
    for child in sorted(daily.iterdir()):
        try:
            when = datetime.strptime(child.name, STAMP_FORMAT)
        except ValueError:
            continue
        meta = child.lstat()
        if stat.S_ISLNK(meta.st_mode) or not stat.S_ISDIR(meta.st_mode):
            continue
        found.append((when, child))
    return found


def snapshot_measurements(daily: Path) -> list[tuple[datetime, int, int]]:
    """(时刻, 复制预算, 轮转可抵扣的已分配字节)。

    多份快照硬链接共享的 inode 只计一次，记在最后一个持有它的快照上：只有轮转到那一份
    才真的释放。还有链接落在这些快照之外的（数到的链接少于 st_nlink），轮转永远释放
    不了，不抵扣。没有共享 inode 时与逐目录块量相同。
    """
    measurements: list[list] = []
    shared: dict[tuple[int, int], list[int]] = {}
    for when, child in _snapshot_directories(daily):
        copy_budget, allocated = snapshot_storage_bytes(
            child, shared_inodes=shared, owner=len(measurements))
        measurements.append([when, copy_budget, allocated])
    for owner, allocated, links, seen in shared.values():
        if seen == links:
            measurements[owner][2] += allocated
    return [(when, copy_budget, allocated) for when, copy_budget, allocated in measurements]


def linked_audio_profile(daily: Path) -> list[tuple[datetime, int, int, bool]]:
    """(时刻, 顶层 audio/ 复制预算, 非录音已分配字节, 录音是否与更早快照共享 inode)。

    非录音部分（库、清单、配置、导出、全部目录）从不被备份链接，块量逐份独立。
    """
    profile: list[tuple[datetime, int, int, bool]] = []
    seen: set[tuple[int, int]] = set()
    for when, child in _snapshot_directories(daily):
        audio_root = child / "audio"
        audio_budget = 0
        nonaudio = 0
        shares = False
        current: set[tuple[int, int]] = set()
        for directory, _dirnames, filenames in os.walk(child):
            directory_path = Path(directory)
            nonaudio += directory_path.lstat().st_blocks * 512
            for name in filenames:
                meta = (directory_path / name).lstat()
                if stat.S_ISLNK(meta.st_mode):
                    continue
                if directory_path != audio_root:
                    nonaudio += meta.st_blocks * 512
                    continue
                audio_budget += max(meta.st_size, meta.st_blocks * 512)
                if meta.st_nlink > 1:
                    key = (meta.st_dev, meta.st_ino)
                    shares = shares or key in seen
                    current.add(key)
        seen |= current
        profile.append((when, audio_budget, nonaudio, shares))
    return profile


def snapshot_series(daily: Path) -> list[tuple[datetime, int]]:
    """按时间排列的 (快照时刻, 保守复制预算)，保留原调用形状。"""
    return [(when, copy_budget) for when, copy_budget, _ in snapshot_measurements(daily)]


def growth_bytes_per_day(series: list[tuple[datetime, int]]) -> float | None:
    """按观测快照复制预算折算的 gross 速率，非 I/O 实测或轮转后净增长。"""
    if len(series) < 2:
        return None
    (first_at, _), (last_at, _) = series[0], series[-1]
    span_days = (last_at - first_at).total_seconds() / 86400
    if span_days <= 0:
        return None
    per_snapshot = sum(size for _, size in series) / len(series)
    snapshots_per_day = (len(series) - 1) / span_days
    return per_snapshot * snapshots_per_day


def daily_size_trend(series: list[tuple[datetime, int]]) -> tuple[int, float | None]:
    """每天最大尺寸的非负趋势，不将同日多份外推成每日多份。

    用所有日期的回归斜率和最近两个日期的斜率较大者，避免平滑掉最近增长。
    不外推负增长；最新日期最大尺寸仍作为下一份的起点。
    """
    by_day: dict[int, int] = {}
    for when, size in series:
        day = when.date().toordinal()
        by_day[day] = max(by_day.get(day, 0), size)
    days = sorted(by_day)
    if len(days) < 2:
        return len(days), None
    xs = [day - days[0] for day in days]
    ys = [by_day[day] for day in days]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / sum(
        (x - mean_x) ** 2 for x in xs)
    recent_slope = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
    return len(days), max(0.0, slope, recent_slope)


def observed_retention_keep(log_path: Path) -> int | None:
    """读取最近成功备份实际声明的 keep，配置不一致不能静默套用 14。"""
    if not log_path.exists():
        return None
    observed = None
    with log_path.open(encoding="utf-8") as stream:
        for line in stream:
            if re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
                        r"ok snapshot=\d{8}-\d{6}(?:\s|$)", line):
                match = re.search(r"(?:^|\s)keep=(\d+)(?:\s|$)", line)
                # A newer success without keep cannot inherit an older contract.
                observed = int(match.group(1)) if match else None
    return observed


def retention_projection(series: list[tuple[datetime, int]], *, keep: int,
                         horizon_days: float, free_bytes: int,
                         existing_allocated: list[int] | None = None,
                         linked_audio: list[int] | None = None) -> dict[str, object]:
    """保留全部当前快照；未来每天写完一份后才移除最旧快照。

    不知道下一次定时器距现在几小时，因此保守按立即发生计第 0 天。
    新增按复制预算预测，现存快照只抵扣真实已分配块；可用量来自 disk_usage。

    给了 linked_audio（逐份顶层 audio/ 的复制预算，与 series 对齐）就按录音链接模型：
    每天仍先整份暂存（峰值按整份），链接后留下非录音部分加一天的新录音；未变录音会被
    链进以后每一份，轮转只抵扣非录音部分，existing_allocated 此时应是各份非录音块量。
    这时 days_left 还要加上 live 数据的增长，peak 与 net_change 仍只算备份。
    """
    if linked_audio is not None and len(linked_audio) != len(series):
        raise ValueError("linked audio measurements must match the copy series")
    distinct_days, slope = daily_size_trend(series)
    latest_size = None
    if series:
        latest_day = series[-1][0].date()
        latest_size = max(size for when, size in series if when.date() == latest_day)
    nonaudio_slope = audio_slope = latest_nonaudio = None
    if linked_audio is not None and slope is not None:
        nonaudio = [(when, size - audio) for (when, size), audio in zip(series, linked_audio)]
        _, nonaudio_slope = daily_size_trend(nonaudio)
        _, audio_slope = daily_size_trend(
            [(when, audio) for (when, _), audio in zip(series, linked_audio)])
        latest_nonaudio = max(size for when, size in nonaudio if when.date() == latest_day)
    live_growth = None
    if slope is None:
        net_growth = None
    elif linked_audio is None:
        net_growth = slope * keep
    else:
        # 新录音只留一份；库等非录音部分每份各存一份，跟着保留份数放大。
        net_growth = audio_slope + nonaudio_slope * keep
        live_growth = audio_slope + nonaudio_slope
    result: dict[str, object] = {
        "next_snapshot_baseline_mb": round(latest_size / MIB, 4) if latest_size is not None else None,
        "next_snapshot_copy_fits": latest_size < free_bytes if latest_size is not None else None,
        "observed_snapshot_days": distinct_days,
        "snapshot_size_growth_mb_per_day": round(slope / MIB, 4) if slope is not None else None,
        "backup_net_retained_growth_mb_per_day": (
            round(net_growth / MIB, 4) if net_growth is not None else None),
        "live_growth_mb_per_day": (
            round(live_growth / MIB, 4) if live_growth is not None else None),
        "backup_peak_additional_mb": None,
        "backup_net_change_mb": None,
        "days_left": None,
        "projection_status": "insufficient_history",
    }
    if linked_audio is not None:
        result["next_snapshot_retained_cost_mb"] = (
            round((latest_nonaudio + nonaudio_slope + audio_slope) / MIB, 4)
            if latest_nonaudio is not None else None)
    if slope is None:
        return result
    assert latest_size is not None
    if existing_allocated is not None and len(existing_allocated) != len(series):
        raise ValueError("allocated snapshot measurements must match the copy series")
    queue = [float(size) for size in (
        existing_allocated if existing_allocated is not None else [size for _, size in series])]
    initial = retained = sum(queue)
    peak = 0.0
    days_left = None
    for day in range(math.ceil(horizon_days)):
        next_size = latest_size + slope * (day + 1)
        if linked_audio is None:
            retained += next_size
            queue.append(next_size)
            additional = retained - initial
        else:
            additional = retained + next_size - initial
            next_nonaudio = latest_nonaudio + nonaudio_slope * (day + 1)
            retained += next_nonaudio + audio_slope
            queue.append(next_nonaudio)
        peak = max(peak, additional)
        # 容量检查紧跟在备份之后跑：第 day 次备份时 live 已比现在又长了 day+1 天。
        live = live_growth * (day + 1) if live_growth is not None else 0.0
        if days_left is None and additional + live >= free_bytes:
            days_left = day
        # vps-backup-daily.sh publishes before rotating; never subtract first.
        while len(queue) > keep:
            retained -= queue.pop(0)
    result.update({
        "backup_peak_additional_mb": round(peak / MIB, 4),
        "backup_net_change_mb": round((retained - initial) / MIB, 4),
        "days_left": days_left,
        "projection_status": ("exhausted_within_horizon" if days_left is not None
                              else "no_exhaustion_within_horizon"),
    })
    return result


def evaluate(backup_root: Path, data_dir: Path | None, *, max_used_pct: float,
             min_free_mb: int, min_days_left: float,
             keep: int = DEFAULT_KEEP) -> dict[str, object]:
    if keep < 1 or not math.isfinite(min_days_left) or min_days_left < 0:
        raise ValueError("retention keep must be positive and horizon must be finite/nonnegative")
    usage = shutil.disk_usage(backup_root)
    used_pct = 100.0 * usage.used / usage.total if usage.total else 100.0
    free_mb = usage.free // MIB

    measurements = snapshot_measurements(backup_root / "daily")
    series = [(when, copy_budget) for when, copy_budget, _ in measurements]
    allocated = [actual for _, _, actual in measurements]
    gross_per_day = growth_bytes_per_day(series)
    profile = linked_audio_profile(backup_root / "daily")
    # 只有最新一份确实与更早快照共享录音，才认定下一份还会链接。
    linked = (bool(profile) and profile[-1][3]
              and [when for when, *_ in profile] == [when for when, _ in series])
    projection = retention_projection(
        series, keep=keep, horizon_days=min_days_left, free_bytes=usage.free,
        existing_allocated=[nonaudio for _, _, nonaudio, _ in profile] if linked else allocated,
        linked_audio=[audio for _, audio, _, _ in profile] if linked else None)
    if linked:
        rotation_credit = "nonaudio_allocated_bytes_only"
    elif any(shares for *_, shares in profile):
        rotation_credit = "last_link_allocated_bytes_only"
    else:
        rotation_credit = "existing_allocated_bytes_only"
    observed_keep = observed_retention_keep(backup_root / "backup.log")

    problems: list[str] = []
    if used_pct > max_used_pct:
        problems.append(f"disk_used_high pct={used_pct:.1f} limit={max_used_pct}")
    if free_mb < min_free_mb:
        problems.append(f"disk_free_low free_mb={free_mb} limit={min_free_mb}")
    if min_days_left > 0:
        if projection["projection_status"] == "insufficient_history":
            problems.append("projection_history_insufficient distinct_dates="
                            f"{projection['observed_snapshot_days']} required=2")
        if projection["next_snapshot_copy_fits"] is False:
            problems.append("next_snapshot_copy_insufficient baseline_mb="
                            f"{projection['next_snapshot_baseline_mb']} free_mb={free_mb}")
        if observed_keep is None:
            problems.append("retention_contract_unverified no_successful_keep_record")
    if observed_keep is not None and observed_keep != keep:
        problems.append(f"retention_keep_mismatch observed={observed_keep} configured={keep}")
    if len(series) > keep:
        problems.append(f"retention_count_exceeded snapshots={len(series)} keep={keep}")
    days_left = projection["days_left"]
    if days_left is not None:
        problems.append(
            f"projected_full_soon days_left={days_left} limit={min_days_left} "
            f"scope=daily_backup_retention{'_plus_live' if linked else ''}")

    gross_mb = round(gross_per_day / MIB, 2) if gross_per_day is not None else None
    facts: dict[str, object] = {
        "used_pct": round(used_pct, 1),
        "free_mb": free_mb,
        "snapshots": len(series),
        # Legacy field retained as raw write rate; no longer used as net growth.
        "backup_growth_mb_per_day": gross_mb,
        "backup_gross_write_mb_per_day": gross_mb,
        "retention_keep": keep,
        "observed_retention_keep": observed_keep,
        "retention_contract_status": (
            "unverified" if observed_keep is None else
            "keep_matches_success_log" if observed_keep == keep else "keep_mismatch"),
        "scheduled_snapshots_per_day": 1,
        "projection_horizon_days": min_days_left,
        "projection_scope": "scheduled_daily_backup_retention_only",
        "snapshot_measurement": "max_logical_allocated_files_plus_allocated_directories",
        "rotation_credit_measurement": rotation_credit,
        "total_days_left": None,
        "unmeasured_growth": ["live_data", "other_logs_and_files",
                              "unattributed_filesystem_metadata"],
        "unmodeled_backup_entrypoint_checks": ["reserve_bytes", "copy_growth_headroom",
                                               "metadata_allowance", "source_count_limits"],
        "assumptions": ["one_scheduled_snapshot_per_day", "successful_rotation_after_publish",
                        "one_off_release_copies_budgeted_separately"],
        **projection,
        "problems": problems,
    }
    if linked:
        facts["snapshot_model"] = "linked_audio"
        facts["assumptions"].append("unchanged_audio_relinked_into_every_new_snapshot")
        facts["projection_scope"] = "scheduled_daily_backup_retention_plus_live_audio_and_db"
        facts["unmeasured_growth"].remove("live_data")
    if data_dir is not None and data_dir.is_dir():
        facts["data_dir_mb"] = directory_bytes(data_dir) // MIB
    return facts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-root", type=Path, default=Path("/opt/nmu/backups"))
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--max-used-pct", type=float, default=85.0)
    parser.add_argument("--min-free-mb", type=int, default=2048)
    parser.add_argument("--min-days-left", type=float, default=30.0)
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                        help="每日备份成功后保留份数；必须与实际备份任务一致")
    parser.add_argument("--state-file", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.keep < 1 or not math.isfinite(args.min_days_left) or args.min_days_left < 0:
        parser.error("--keep must be positive; --min-days-left must be finite/nonnegative")

    try:
        facts = evaluate(
            args.backup_root, args.data_dir, max_used_pct=args.max_used_pct,
            min_free_mb=args.min_free_mb, min_days_left=args.min_days_left, keep=args.keep)
    except OSError as error:
        reason = (error.strerror if error.strerror in {
            "snapshot_allocated_size_unavailable", "snapshot_file_type_unsupported",
        } else "filesystem_probe_error")
        print(f"[{datetime.now():%F %T}] UNHEALTHY capacity_probe_failed "
              f"err={error.errno} reason={reason}")
        return 2

    problems = facts["problems"]
    assert isinstance(problems, list)
    verdict = "HEALTHY" if not problems else "UNHEALTHY " + "; ".join(problems)
    growth = facts["backup_growth_mb_per_day"]
    line = (f"[{datetime.now():%F %T}] {verdict} "
            f"used={facts['used_pct']}% free={facts['free_mb']}MB "
            f"gross_write={growth if growth is not None else '未知'}MB/d "
            f"net_retained_growth={facts['backup_net_retained_growth_mb_per_day']}MB/d "
            f"days_left={facts['days_left'] if facts['days_left'] is not None else '未知'} "
            f"scope={'daily_backups_plus_live' if 'snapshot_model' in facts else 'daily_backups_only'} "
            f"horizon={args.min_days_left}d "
            f"projection={facts['projection_status']} "
            f"retention_contract={facts['retention_contract_status']} total_days_left=未知")
    if "snapshot_model" in facts:
        line += (f" live_growth={facts['live_growth_mb_per_day']}MB/d"
                 f" snapshot_model={facts['snapshot_model']}")

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
