#!/usr/bin/env python3
"""判定备份链当前是否健康,给一行机器可读结论,非零退出即不健康。

存在的理由:2026-07-23 到 2026-08-05,VPS 每晚备份连续失败十四次,`backup.log`
每次都老老实实写了 FAIL,没有任何人和任何程序去读它。审计真相文件只有在有人
定期读它的时候才是真相。

判据(全部 fail-closed,读不到就算不健康):
  - 最新一条 `ok` 的时间距现在不超过 --max-age-hours;
  - 最新 `ok` 之后累计的 FAIL 不超过 --max-consecutive-failures;
  - 精确的 `SKIP code=backup_lock_busy` 只是锁忙记录，不算成功或失败;
  - 最新 `ok` 声明的快照目录真的在盘上,且是目录不是软链接;
  - 备份根所在文件系统剩余空间不低于 --min-free-mb;
  - 给了 --probe-unit 时,那个探针单元最近一次成功距今不超过 --probe-max-age-minutes。

只用标准库:VPS 上跑这个脚本的解释器不保证有 SQLAlchemy(这正是十四夜失败的
起因),不能再给它加依赖。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

LINE = re.compile(
    r"^\[(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] "
    r"(?P<verdict>ok|FAIL|SKIP)\b(?P<rest>.*)$")
SNAPSHOT = re.compile(r"\bsnapshot=(?P<name>[0-9]{8}-[0-9]{6})\b")
# 录音去重打开后 ok 行末尾的两个异常计数；开关关着时没有这两个字段。
LINK_ANOMALIES = (("cmp_mismatch", "audio_cmp_mismatch"),
                  ("outside_links", "audio_outside_links"))


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
        # The daily wrapper emits this one benign skip. Unknown skip reasons
        # or extra fields remain unreadable evidence, never silently ignored.
        if (match["verdict"] == "SKIP"
                and match["rest"] != " code=backup_lock_busy"):
            raise Unevaluable("backup_log_line_unparsable")
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

    # 只看最新一次成功：下一份快照已经另存了新副本，旧的那次由当时的告警交代。
    for field, code in LINK_ANOMALIES:
        count = re.search(rf"\b{field}=(?P<count>[0-9]+)\b", rest)
        if count is not None and int(count["count"]) > 0:
            problems.append(f"{code} count={int(count['count'])}")

    try:
        free_mb = shutil.disk_usage(daily_dir).free // (1024 * 1024)
    except OSError as error:
        problems.append(f"disk_probe_failed err={error.errno}")
    else:
        if free_mb < min_free_mb:
            problems.append(f"disk_space_low free_mb={free_mb} limit={min_free_mb}")

    return problems


def probe_staleness(unit: str, *, max_age_minutes: float, run=subprocess.run,
                    monotonic=time.monotonic) -> list[str]:
    """探针单元最近一次成功是否还在窗口内。返回问题码列表,空 = 新鲜。

    探针的定时器自己死掉时什么都不会失败:2026-09-30 重启后 nmu-health.timer 停在
    elapsed,ActiveState 仍是 active,没有一次 OnFailure。只能从这里问它最近一次成功
    是什么时候。最近一轮失败时 systemd 给不出更早那次成功的时刻,按不健康报。
    """
    try:
        done = run(["systemctl", "show", unit, "-p", "ActiveState", "-p", "Result",
                    "-p", "ExecMainStatus", "-p", "ExecMainExitTimestampMonotonic"],
                   capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        return [f"probe_unevaluable unit={unit} err={error.__class__.__name__}"]
    if done.returncode != 0:
        return [f"probe_unevaluable unit={unit} rc={done.returncode}"]
    facts: dict[str, str] = {}
    for line in done.stdout.splitlines():
        key, _, value = line.partition("=")
        facts[key.strip()] = value.strip()
    # 起跑时 systemd 把上一轮的退出时间清零;正在探说明定时器活着,结论归这一轮自己的 OnFailure。
    if facts.get("ActiveState") == "activating":
        return []
    exited = facts.get("ExecMainExitTimestampMonotonic", "")
    if not exited.isdigit():
        return [f"probe_unevaluable unit={unit} exit_timestamp={exited or 'missing'}"]
    if int(exited) == 0:
        return [f"probe_never_ran_since_boot unit={unit}"]
    if facts.get("Result") != "success":
        return [f"probe_last_run_failed unit={unit} result={facts.get('Result') or '?'} "
                f"status={facts.get('ExecMainStatus') or '?'}"]
    age_minutes = (monotonic() - int(exited) / 1_000_000) / 60
    if age_minutes > max_age_minutes:
        return [f"probe_stale unit={unit} age_minutes={age_minutes:.1f} "
                f"limit={max_age_minutes}"]
    return []


def main(argv: list[str] | None = None, *, run=subprocess.run,
         monotonic=time.monotonic) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-root", default="/opt/nmu/backups", type=Path)
    # 30 小时而不是 24:timer 带 RandomizedDelaySec=600,再留一次维护窗的余量。
    parser.add_argument("--max-age-hours", default=30.0, type=float)
    # 允许一次:偶发单夜失败第二天自愈不该半夜叫人。第二次就是趋势。
    parser.add_argument("--max-consecutive-failures", default=1, type=int)
    parser.add_argument("--min-free-mb", default=1024, type=int)
    parser.add_argument("--state-file", type=Path, default=None,
                        help="把结论写到这里(0600),给不方便读退出码的调用方")
    parser.add_argument("--probe-unit", default=None,
                        help="顺带核对这个探针单元最近一次成功的时间(生产给 nmu-health.service)")
    # 探针每 10 分钟一次:30 分钟 = 连着错过两次以上。
    parser.add_argument("--probe-max-age-minutes", default=30.0, type=float)
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
        problems = [str(error)]
        code = 2
    else:
        code = 0 if not problems else 1
    if args.probe_unit:
        stale = probe_staleness(args.probe_unit, max_age_minutes=args.probe_max_age_minutes,
                                run=run, monotonic=monotonic)
        problems.extend(stale)
        code = code or (1 if stale else 0)
    verdict = "HEALTHY" if not problems else "UNHEALTHY " + "; ".join(problems)

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
