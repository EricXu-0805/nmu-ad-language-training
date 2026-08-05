#!/usr/bin/env python3
"""判定备份链当前是否健康,给一行机器可读结论,非零退出即不健康。

存在的理由:2026-07-23 到 2026-08-05,VPS 每晚备份连续失败十四次,`backup.log`
每次都老老实实写了 FAIL,没有任何人和任何程序去读它。审计真相文件只有在有人
定期读它的时候才是真相。

判据(全部 fail-closed,读不到就算不健康):
  - 最新一条 `ok` 的时间距现在不超过 --max-age-hours;
  - 最新 `ok` 之后累计的 FAIL 不超过 --max-consecutive-failures;
  - 最新 `ok` 声明的快照目录真的在盘上,且是目录不是软链接;
  - 备份根所在文件系统剩余空间不低于 --min-free-mb。

只用标准库:VPS 上跑这个脚本的解释器不保证有 SQLAlchemy(这正是十四夜失败的
起因),不能再给它加依赖。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path
import re
import shutil
import sys

LINE = re.compile(
    r"^\[(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] "
    r"(?P<verdict>ok|FAIL)\b(?P<rest>.*)$")
SNAPSHOT = re.compile(r"\bsnapshot=(?P<name>[0-9]{8}-[0-9]{6})\b")


class Unevaluable(Exception):
    """读不到审计事实。不能当成健康。"""


def _parse(log_path: Path) -> list[tuple[datetime, str, str]]:
    try:
        raw = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise Unevaluable(f"backup_log_unreadable path={log_path} err={error.errno}")
    entries: list[tuple[datetime, str, str]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        match = LINE.match(line)
        if match is None:
            raise Unevaluable("backup_log_line_unparsable")
        try:
            when = datetime.strptime(match["stamp"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise Unevaluable("backup_log_timestamp_invalid") from None
        entries.append((when, match["verdict"], match["rest"]))
    if not entries:
        raise Unevaluable("backup_log_empty")
    return entries


def evaluate(log_path: Path, daily_dir: Path, now: datetime, *,
             max_age_hours: float, max_consecutive_failures: int,
             min_free_mb: int) -> list[str]:
    """返回问题码列表。空列表 = 健康。"""
    entries = _parse(log_path)
    problems: list[str] = []

    last_ok_index = None
    for index in range(len(entries) - 1, -1, -1):
        if entries[index][1] == "ok":
            last_ok_index = index
            break
    if last_ok_index is None:
        return [f"no_successful_backup_ever entries={len(entries)}"]

    when, _, rest = entries[last_ok_index]
    age = now - when
    if age > timedelta(hours=max_age_hours):
        problems.append(
            f"last_backup_stale age_hours={age.total_seconds() / 3600:.1f} "
            f"limit={max_age_hours}")

    failures_since = sum(1 for entry in entries[last_ok_index + 1:] if entry[1] == "FAIL")
    if failures_since > max_consecutive_failures:
        problems.append(
            f"consecutive_failures count={failures_since} "
            f"limit={max_consecutive_failures}")

    snapshot = SNAPSHOT.search(rest)
    if snapshot is None:
        problems.append("last_ok_names_no_snapshot")
    else:
        published = daily_dir / snapshot["name"]
        if published.is_symlink() or not published.is_dir():
            problems.append(f"published_snapshot_missing name={snapshot['name']}")

    try:
        free_mb = shutil.disk_usage(daily_dir).free // (1024 * 1024)
    except OSError as error:
        problems.append(f"disk_probe_failed err={error.errno}")
    else:
        if free_mb < min_free_mb:
            problems.append(f"disk_space_low free_mb={free_mb} limit={min_free_mb}")

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-root", default="/opt/nmu/backups", type=Path)
    # 30 小时而不是 24:timer 带 RandomizedDelaySec=600,再留一次维护窗的余量。
    parser.add_argument("--max-age-hours", default=30.0, type=float)
    # 允许一次:偶发单夜失败第二天自愈不该半夜叫人。第二次就是趋势。
    parser.add_argument("--max-consecutive-failures", default=1, type=int)
    parser.add_argument("--min-free-mb", default=1024, type=int)
    parser.add_argument("--state-file", type=Path, default=None,
                        help="把结论写到这里(0600),给不方便读退出码的调用方")
    args = parser.parse_args(argv)

    root = args.backup_root
    now = datetime.now()
    try:
        problems = evaluate(
            root / "backup.log", root / "daily", now,
            max_age_hours=args.max_age_hours,
            max_consecutive_failures=args.max_consecutive_failures,
            min_free_mb=args.min_free_mb)
    except Unevaluable as error:
        verdict = f"UNHEALTHY {error}"
        code = 2
    else:
        verdict = "HEALTHY" if not problems else "UNHEALTHY " + "; ".join(problems)
        code = 0 if not problems else 1

    line = f"[{now:%F %T}] {verdict}"
    print(line)
    if args.state_file is not None:
        try:
            args.state_file.write_text(line + "\n", encoding="utf-8")
            args.state_file.chmod(0o600)
        except OSError as error:
            print(f"state_file_write_failed err={error.errno}", file=sys.stderr)
            code = code or 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
