#!/usr/bin/env python3
"""养老院真实场次之后，只读生产库，回答「这一批改动在真场次里表现如何」。

生产上这样跑（场次结束后再跑：这台机器没开 WAL，读库那几十毫秒里床旁写入要等）：

    ssh root@<服务器> '/opt/nmu/venv/bin/python /opt/nmu/app/scripts/field_session_report.py --db /opt/nmu/app/data/app.db --since 2026-10-01'

其它开关：--session-count N 只看最近 N 场；--include-simulation 把模拟场次也算进来；
--json 输出机器可读结果（默认 Markdown）。

只读：库只经 sqlite3.connect("file:<path>?mode=ro", uri=True) 打开，另加
PRAGMA query_only，全部行在一个读事务里取完就关；路径不存在直接拒绝（mode=ro 也
不会替你建一个空库）。

只出聚合：场次按 training_date 先后编号 1..n，不出 session_id、patient_id、姓名、
raw_audio_id、转写、判分理由原文、绝对日期或任何自由文本。默认只看真实场次
（is_simulation=0），且只看至少有一条自动带练命令的场次。

时钟：RuntimeCommand / RuntimeCommandAck / 控制事件存 UTC；AttemptEvent、
InteractionEvent、TtsServeEvidence、AuditLog 存服务进程本地时间（生产 .env 设
TZ=Asia/Shanghai，即 +8 h）。脚本拿同一段录音的 record_stopped 回执与 attempt 落行
时间配对求偏移中位，不在 +8 h ± 60 s 内就拒绝出报告；在的话本地列一律按整 8 h 折回
UTC——不按测得的中位折，那个中位里含着几秒 ASR（attempt 是 ASR 出结果才落行的）。

退出码：0 = 出了报告；2 = 拒绝（库不存在或读不了、不是平台库、时钟偏移不对）。
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
import statistics
import sys
from urllib.parse import quote


SCHEMA = "nmu.field-session-report.v1"
LOCAL_OFFSET_SECONDS = 8 * 3600
OFFSET_TOLERANCE_SECONDS = 60

COMMAND_COLUMNS = (
    "id", "session_id", "command_seq", "kind", "predecessor_command_id",
    "trigger_ack_idempotency_key", "control_generation", "created_at", "payload_json",
)
ACK_COLUMNS = (
    "command_id", "session_id", "ack_type", "idempotency_key", "payload_json",
    "duration_seconds", "raw_audio_id", "received_at",
)
ATTEMPT_COLUMNS = (
    "id", "session_id", "raw_audio_id", "processing_status", "error_code",
    "judge_mode", "created_at", "processed_at",
)


class Refused(Exception):
    """稳定拒绝码 + 一句人话；不带路径、场次或任何行内容。"""

    def __init__(self, code: str, detail: str):
        super().__init__(code)
        self.code = code
        self.detail = detail


class Snapshot:
    """一个读事务里取完的行。算指标时库连接早已关掉。"""

    def __init__(self) -> None:
        self.head: str | None = None
        self.sessions: list[dict] = []
        self.excluded_without_commands = 0
        self.commands: dict[int, dict] = {}
        self.acks: dict[int, dict[str, dict]] = defaultdict(dict)
        self.attempts: list[dict] = []
        self.runtime: dict[str, dict] = {}
        self.absent: dict[str, str] = {}


def _ts(value) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _json_object(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _schema(conn: sqlite3.Connection) -> dict[str, frozenset[str]]:
    names = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")]
    return {
        name: frozenset(row[1] for row in conn.execute(
            'PRAGMA table_info("{}")'.format(name.replace('"', '""'))))
        for name in names
    }


def _absent(schema: dict[str, frozenset[str]], table: str, *columns: str) -> str | None:
    if table not in schema:
        return f"table {table} absent"
    for column in columns:
        if column not in schema[table]:
            return f"column {table}.{column} absent"
    return None


def _rows_for(conn: sqlite3.Connection, sql: str, session_ids: list[str]) -> list:
    """sql 里留 {ids} 占位，按 500 个一批绑参数：老 SQLite 的变量上限是 999。"""
    rows: list = []
    for start in range(0, len(session_ids), 500):
        chunk = session_ids[start:start + 500]
        rows.extend(conn.execute(sql.format(ids=",".join("?" * len(chunk))), chunk))
    return rows


def _open(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise Refused("db_missing", "--db 指向的路径上没有数据库文件")
    uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error as exc:
        raise Refused("db_unreadable", f"数据库打不开：{exc}") from None
    return conn


def _load_sessions(conn, schema, snap: Snapshot, *, since: date | None,
                   session_count: int | None, include_simulation: bool) -> None:
    missing = _absent(schema, "session", "session_id", "training_date", "week_no",
                      "is_simulation")
    if missing:
        raise Refused("not_platform_db", f"不像平台库：{missing}")
    with_commands = _absent(schema, "runtimecommand", "session_id", "created_at") is None
    first_command = (
        "(SELECT MIN(c.created_at) FROM runtimecommand c"
        " WHERE c.session_id = s.session_id)" if with_commands else "NULL")
    clauses: list[str] = []
    params: list[str] = []
    if not include_simulation:
        # 模型约定：没显式标成模拟的场次一律按真实研究处理。
        clauses.append("COALESCE(s.is_simulation, 0) = 0")
    if since is not None:
        clauses.append("s.training_date >= ?")
        params.append(since.isoformat())
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        "SELECT s.session_id, s.training_date, s.week_no, "
        f"{first_command} AS first_command_at FROM session s{where}", params).fetchall()
    kept = [row for row in rows
            if not with_commands or row["first_command_at"] is not None]
    snap.excluded_without_commands = len(rows) - len(kept)
    # 同一天的几场按第一条命令的时刻排；session_id 只做最后的稳定次序，从不输出。
    kept.sort(key=lambda row: (row["training_date"] or "",
                               row["first_command_at"] or "", row["session_id"]))
    if session_count is not None:
        kept = kept[-session_count:]
    snap.sessions = [{"session_id": row["session_id"], "week_no": row["week_no"]}
                     for row in kept]


def _load(conn: sqlite3.Connection, *, since: date | None, session_count: int | None,
          include_simulation: bool) -> Snapshot:
    snap = Snapshot()
    conn.execute("BEGIN")
    try:
        schema = _schema(conn)
        if _absent(schema, "alembic_version", "version_num") is None:
            heads = [row[0] for row in conn.execute(
                "SELECT version_num FROM alembic_version")]
            snap.head = heads[0] if len(heads) == 1 else None
        _load_sessions(conn, schema, snap, since=since, session_count=session_count,
                       include_simulation=include_simulation)
        ids = [session["session_id"] for session in snap.sessions]

        missing = _absent(schema, "runtimecommand", *COMMAND_COLUMNS)
        if missing:
            snap.absent["commands"] = missing
        else:
            for row in _rows_for(conn, "SELECT " + ", ".join(COMMAND_COLUMNS)
                                 + " FROM runtimecommand WHERE session_id IN ({ids})", ids):
                payload = _json_object(row["payload_json"]) if row["kind"] == "tts" else {}
                snap.commands[row["id"]] = {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "seq": row["command_seq"],
                    "kind": row["kind"],
                    "predecessor": row["predecessor_command_id"],
                    "trigger_key": row["trigger_ack_idempotency_key"],
                    "generation": row["control_generation"],
                    "created_at": _ts(row["created_at"]),
                    "purpose": payload.get("purpose"),
                }

        missing = _absent(schema, "runtimecommandack", *ACK_COLUMNS)
        if missing:
            snap.absent["acks"] = missing
        else:
            for row in _rows_for(conn, "SELECT " + ", ".join(ACK_COLUMNS)
                                 + " FROM runtimecommandack WHERE session_id IN ({ids})",
                                 ids):
                snap.acks[row["command_id"]][row["ack_type"]] = {
                    "key": row["idempotency_key"],
                    "payload": _json_object(row["payload_json"]),
                    "duration": row["duration_seconds"],
                    "raw_audio_id": row["raw_audio_id"],
                    "at": _ts(row["received_at"]),
                }

        missing = _absent(schema, "attemptevent", *ATTEMPT_COLUMNS)
        if missing:
            snap.absent["attempts"] = missing
        else:
            for row in _rows_for(conn, "SELECT " + ", ".join(ATTEMPT_COLUMNS)
                                 + " FROM attemptevent WHERE session_id IN ({ids})", ids):
                snap.attempts.append({
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "raw_audio_id": row["raw_audio_id"],
                    "status": row["processing_status"],
                    "error_code": row["error_code"],
                    "judge_mode": row["judge_mode"],
                    "created_at": _ts(row["created_at"]),
                    "processed_at": _ts(row["processed_at"]),
                })

        missing = _absent(schema, "sessionruntimestate", "session_id", "status",
                          "end_reason")
        if missing:
            snap.absent["runtime"] = missing
        else:
            for row in _rows_for(conn, "SELECT session_id, status, end_reason FROM "
                                 "sessionruntimestate WHERE session_id IN ({ids})", ids):
                snap.runtime[row["session_id"]] = {
                    "status": row["status"], "end_reason": row["end_reason"]}
    finally:
        conn.rollback()
    return snap


def _hms(seconds: float) -> str:
    sign = "-" if seconds < 0 else "+"
    whole, fraction = divmod(abs(seconds), 1)
    hours, rest = divmod(int(whole), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{sign}{hours}:{minutes:02d}:{secs + fraction:04.1f}"


def _clock(snap: Snapshot) -> dict:
    by_audio = {attempt["raw_audio_id"]: attempt for attempt in snap.attempts
                if attempt["raw_audio_id"]}
    offsets = []
    for acks in snap.acks.values():
        stop = acks.get("record_stopped")
        attempt = by_audio.get(stop["raw_audio_id"]) if stop else None
        if attempt and attempt["created_at"] and stop["at"]:
            offsets.append((attempt["created_at"] - stop["at"]).total_seconds())
    clock = {
        "status": "not_detectable",
        "pairs": len(offsets),
        "median_offset_seconds": None,
        "expected_offset_seconds": LOCAL_OFFSET_SECONDS,
        "tolerance_seconds": OFFSET_TOLERANCE_SECONDS,
    }
    if not offsets:
        return clock
    median = statistics.median(offsets)
    if abs(median - LOCAL_OFFSET_SECONDS) > OFFSET_TOLERANCE_SECONDS:
        raise Refused(
            "clock_offset_unexpected",
            f"本地时间列比 UTC 列快 {_hms(median)}（{len(offsets)} 对同一段录音的 "
            "record_stopped 回执与 attempt 落行），不在 +8 h ± 60 s 内；本地列怎么折回 "
            "UTC 没有依据，拒绝出报告。先核对生产 .env 的 TZ。")
    clock.update(status="ok", median_offset_seconds=round(median, 3))
    return clock


def _overview(snap: Snapshot) -> list[dict]:
    counts = {session["session_id"]: Counter() for session in snap.sessions}
    for command in snap.commands.values():
        tally = counts[command["session_id"]]
        tally["commands"] += 1
        if "record_stopped" in snap.acks.get(command["id"], {}):
            tally["record_stopped"] += 1
    for attempt in snap.attempts:
        counts[attempt["session_id"]]["attempts"] += 1
    rows = []
    for number, session in enumerate(snap.sessions, 1):
        tally = counts[session["session_id"]]
        runtime = snap.runtime.get(session["session_id"])
        rows.append({
            "n": number,
            "week_no": session["week_no"],
            "commands": None if "commands" in snap.absent else tally["commands"],
            "record_stopped": (None if "commands" in snap.absent or "acks" in snap.absent
                               else tally["record_stopped"]),
            "attempts": None if "attempts" in snap.absent else tally["attempts"],
            "runtime_status": runtime["status"] if runtime else None,
        })
    return rows


def build_report(db: Path, *, since: date | None = None,
                 session_count: int | None = None,
                 include_simulation: bool = False) -> dict:
    conn = _open(db)
    try:
        snap = _load(conn, since=since, session_count=session_count,
                     include_simulation=include_simulation)
    except sqlite3.Error as exc:
        raise Refused("db_unreadable", f"读库失败：{exc}") from None
    finally:
        conn.close()
    clock = _clock(snap)
    return {
        "schema": SCHEMA,
        "db_head": snap.head,
        "scope": {
            "include_simulation": include_simulation,
            "since": since.isoformat() if since else None,
            "session_count": session_count,
            "sessions": len(snap.sessions),
            "excluded_without_commands": snap.excluded_without_commands,
        },
        "clock": clock,
        "sessions": _overview(snap),
    }


def _cell(value) -> str:
    return "—" if value is None else str(value)


def render_markdown(report: dict) -> str:
    scope = report["scope"]
    clock = report["clock"]
    filters = ["模拟与真实场次" if scope["include_simulation"] else "真实场次"]
    if scope["since"]:
        filters.append(f"training_date ≥ {scope['since']}")
    if scope["session_count"]:
        filters.append(f"最近 {scope['session_count']} 场")
    filters.append("至少有一条自动带练命令")
    if clock["status"] == "ok":
        clock_line = (
            f"本地列比 UTC 列快 {_hms(clock['median_offset_seconds'])}"
            f"（{clock['pairs']} 对同一段录音的 record_stopped 回执 ↔ attempt 落行，"
            "中位里含几秒 ASR）；本地列按整 8 h 折回 UTC")
    else:
        clock_line = "没有可配对的录音，测不出偏移；用不到本地列的指标照常算"
    lines = [
        "# 现场场次报告（只读）",
        "",
        f"- 库头：{_cell(report['db_head'])}",
        f"- 范围：{'；'.join(filters)}。共 {scope['sessions']} 场，按 training_date "
        f"先后编号；范围内另有 {scope['excluded_without_commands']} 场没有命令，不计",
        f"- 时钟：{clock_line}",
        "- 口径：n / 中位 / p90（线性插值）/ 最大，单位秒；基线 = 2026-09-17 养老院"
        "八场 149 轮（收据 260 §八）",
        "",
        "## 场次一览",
        "",
        "| # | 周 | 命令 | record_stopped | attempt | 运行终态 |",
        "|---|---|---|---|---|---|",
    ]
    for row in report["sessions"]:
        lines.append(
            f"| {row['n']} | {_cell(row['week_no'])} | {_cell(row['commands'])} | "
            f"{_cell(row['record_stopped'])} | {_cell(row['attempts'])} | "
            f"{_cell(row['runtime_status'])} |")
    return "\n".join(lines) + "\n"


def _since(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("日期要写成 YYYY-MM-DD") from None


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError("要一个正整数")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="养老院场次之后的只读现场报告：只出聚合，不出任何标识符或自由文本。")
    parser.add_argument("--db", required=True, type=Path, help="SQLite 库路径（只读打开）")
    parser.add_argument("--since", type=_since, help="只看 training_date ≥ 该日，YYYY-MM-DD")
    parser.add_argument("--session-count", type=_positive, help="只看最近 N 场")
    parser.add_argument("--include-simulation", action="store_true",
                        help="把模拟场次也算进来（默认只看真实场次）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（默认 Markdown）")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.db, since=args.since,
                              session_count=args.session_count,
                              include_simulation=args.include_simulation)
    except Refused as exc:
        print(f"REFUSED code={exc.code}", file=sys.stderr)
        print(exc.detail, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
