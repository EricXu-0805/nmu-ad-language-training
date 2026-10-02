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
raw_audio_id、转写、判分理由原文、绝对日期或任何自由文本。判分理由在 SQL 里只取
length()，原文不进这个进程；要打印的分类码（stop_reason、error_code……）不像小写码
的一律记 other。默认只看真实场次（is_simulation=0），且只看至少有一条自动带练命令的
场次。

七组指标，数字都是 n / 中位 / p90（线性插值）/ 最大，单位秒；基线是 2026-09-16/17
养老院八场 149 轮的只读测量（收据 260 §八）：
  1. 提问念完 → 开麦：触发录音的 tts_ended → 该录音命令的 record_started；打断后开麦单列。
  2. 录音段：record_started → record_stopped 与平板报的 duration_seconds，按
     stop_reason 分；silence 收麦的 vad_voiced_ms；vad_armed 真/假/缺（旧平板不报）。
  3. 老人说完 → 机器人开口：record_stopped → 下一条 TTS 的 tts_started，拆成
     upload+ASR、判分（rule / rubric / LLM）、签发、取音+开播；中间隔了暂停/续弹的不算。
  4. LLM 判分：次数、理由长度、超 40 字占比；规则/rubric 判分超 1 s 的次数
     （LLM 超时退回规则判的旁证）。
  5. 话术播放：tts_interrupted 按打断原因；question/cue 等各自播完与被打断的次数。
  6. 研究者操作：裁定按种类/原因、续弹、暂停原因、老人主动暂停、接管、中止原因。
  7. 技术失败：technical_failure 的 attempt、tts_failed、record_failed，各按 error_code。
库头较旧、某张表或某列不在，就在那一项写 n/a: table … absent / column … absent，不猜。

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
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import statistics
import sys
from urllib.parse import quote


SCHEMA = "nmu.field-session-report.v1"
LOCAL_OFFSET_SECONDS = 8 * 3600
LOCAL_OFFSET = timedelta(seconds=LOCAL_OFFSET_SECONDS)
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

CODE = re.compile(r"[a-z][a-z0-9_]{0,95}")
STOP_REASONS = ("silence", "user_done", "max_duration", "stream_end")
TTS_PURPOSES = ("question", "cue", "feedback", "tell_answer")
JUDGE_MODES = {"规则确定式": "rule", "版本化规则": "rubric", "LLM辅助": "llm"}
JUDGE_LABELS = {"rule": "rule（规则确定式）", "rubric": "rubric（版本化规则）",
                "llm": "LLM（LLM辅助）"}
ABORT_REASONS = frozenset({
    "participant_declined", "clinical_safety", "technical_failure",
    "researcher_decision", "subject_withdrawal",
})
EXCLUDED_LABELS = {
    "no_attempt": "没有 attempt（如「再说一遍」）",
    "technical_failure": "技术失败",
    "not_judged": "attempt 没判完",
    "no_next_tts": "下一条命令不是 TTS",
    "crossed_pause": "中间隔了暂停/续弹（控制代际变了）",
    "next_tts_not_started": "下一条 TTS 没开播",
}

# 2026-09-16/17 养老院八场 149 轮（收据 260 §八）：117 段靠工作人员按键收麦、
# 32 段撞 14 s 上限，平板当时没有 VAD。
BASELINE_QUESTION_TO_MIC = {"median": 1.61, "p90": 2.01}
BASELINE_RECORDING = {"stops": 149, "silence": 0, "user_done": 117,
                      "max_duration": 32, "stream_end": 0,
                      "wall_median": 7.7, "wall_p90": 15.7}
BASELINE_ELDER_TO_ROBOT = {
    "total_median": 8.2, "total_p90": 10.51, "upload_asr_median": 2.94,
    "judge_median": 3.37, "issue_median": 0.04, "fetch_play_median": 1.91,
}


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
        self.asr_done: dict[int, datetime | None] = {}
        self.patient_pauses = 0
        self.control_events: list[dict] = []
        self.adjudications: list[dict] = []
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
            reason_missing = _absent(schema, "attemptevent", "judge_reason")
            if reason_missing:
                snap.absent["judge_reason"] = reason_missing
            # 判分理由只量长度（SQLite 的 length() 数的是字符），原文从不取出来。
            reason = "NULL" if reason_missing else "length(judge_reason)"
            for row in _rows_for(conn, "SELECT " + ", ".join(ATTEMPT_COLUMNS) + ", "
                                 + reason + " AS reason_chars FROM attemptevent "
                                 "WHERE session_id IN ({ids})", ids):
                snap.attempts.append({
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "raw_audio_id": row["raw_audio_id"],
                    "status": row["processing_status"],
                    "error_code": row["error_code"],
                    "judge_mode": row["judge_mode"],
                    "reason_chars": row["reason_chars"],
                    "created_at": _ts(row["created_at"]),
                    "processed_at": _ts(row["processed_at"]),
                })

        missing = _absent(schema, "interactionevent", "session_id", "attempt_id",
                          "event_type", "created_at")
        if missing:
            snap.absent["interactions"] = missing
        else:
            for row in _rows_for(conn, "SELECT attempt_id, MIN(created_at) AS at "
                                 "FROM interactionevent WHERE event_type = 'asr_completed' "
                                 "AND attempt_id IS NOT NULL AND session_id IN ({ids}) "
                                 "GROUP BY attempt_id", ids):
                snap.asr_done[row["attempt_id"]] = _ts(row["at"])
            for row in _rows_for(conn, "SELECT COUNT(*) AS n FROM interactionevent "
                                 "WHERE event_type = 'patient_requested_pause' "
                                 "AND session_id IN ({ids})", ids):
                snap.patient_pauses += row["n"]

        missing = _absent(schema, "autopilotcontrolevent", "session_id", "event_type",
                          "reason_code", "to_status")
        if missing:
            snap.absent["control"] = missing
        else:
            snap.control_events = [dict(row) for row in _rows_for(
                conn, "SELECT event_type, reason_code, to_status FROM "
                "autopilotcontrolevent WHERE session_id IN ({ids})", ids)]

        missing = _absent(schema, "autopilotpositionadjudication", "session_id", "kind",
                          "reason_code")
        if missing:
            snap.absent["adjudications"] = missing
        else:
            snap.adjudications = [dict(row) for row in _rows_for(
                conn, "SELECT kind, reason_code FROM autopilotpositionadjudication "
                "WHERE session_id IN ({ids})", ids)]

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


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def _code(value) -> str:
    if value is None:
        return "missing"
    return value if isinstance(value, str) and CODE.fullmatch(value) else "other"


def _tally(values) -> dict[str, int]:
    counts = Counter(_code(value) for value in values)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _to_utc(local: datetime | None) -> datetime | None:
    return None if local is None else local - LOCAL_OFFSET


def _span(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def _quantile(ordered: list[float], q: float) -> float:
    position = (len(ordered) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _stats(values) -> dict:
    ordered = sorted(value for value in values if value is not None)
    if not ordered:
        return {"n": 0, "median": None, "p90": None, "max": None}
    return {
        "n": len(ordered),
        "median": round(_quantile(ordered, 0.5), 3),
        "p90": round(_quantile(ordered, 0.9), 3),
        "max": round(ordered[-1], 3),
    }


def _share(count: int, total: int) -> float | None:
    return round(count / total, 3) if total else None


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
            "runtime_status": _code(runtime["status"]) if runtime else None,
        })
    return rows


# ---------------------------------------------------------------------------
# 七组指标
# ---------------------------------------------------------------------------

def _records(snap: Snapshot) -> list[dict]:
    return [command for command in snap.commands.values() if command["kind"] == "record"]


def _trigger(snap: Snapshot, record: dict) -> tuple[str | None, dict | None]:
    """录音命令记着解锁它的那条回执的幂等键：播完（tts_ended）或被打断（tts_interrupted）。"""
    for ack_type in ("tts_ended", "tts_interrupted"):
        ack = snap.acks.get(record["predecessor"], {}).get(ack_type)
        if ack is not None and ack["key"] == record["trigger_key"]:
            return ack_type, ack
    return None, None


def _question_to_mic(snap: Snapshot) -> dict:
    missing = snap.absent.get("commands") or snap.absent.get("acks")
    if missing:
        return {"na": missing}
    spans: dict[str, list] = {"tts_ended": [], "tts_interrupted": []}
    unmeasured = 0
    for record in _records(snap):
        started = snap.acks.get(record["id"], {}).get("record_started")
        trigger_type, trigger = _trigger(snap, record)
        span = _span(trigger["at"], started["at"]) if started and trigger else None
        if span is None:
            unmeasured += 1
            continue
        spans[trigger_type].append(span)
    return {
        "tts_ended_to_record_started": _stats(spans["tts_ended"]),
        "tts_interrupted_to_record_started": _stats(spans["tts_interrupted"]),
        "records_unmeasured": unmeasured,
        "baseline_2026_09_17": BASELINE_QUESTION_TO_MIC,
    }


def _recording(snap: Snapshot) -> dict:
    missing = snap.absent.get("commands") or snap.absent.get("acks")
    if missing:
        return {"na": missing}
    reasons: Counter = Counter()
    wall: dict[str, list] = defaultdict(list)
    device: dict[str, list] = defaultdict(list)
    overhead, voiced = [], []
    armed = {"true": 0, "false": 0, "missing": 0}
    for record in _records(snap):
        acks = snap.acks.get(record["id"], {})
        stop = acks.get("record_stopped")
        if stop is None:
            continue
        payload = stop["payload"]
        reason = payload.get("stop_reason")
        reason = reason if reason in STOP_REASONS else "other"
        reasons[reason] += 1
        started = acks.get("record_started")
        span = _span(started["at"], stop["at"]) if started else None
        duration = stop["duration"] if isinstance(stop["duration"], (int, float)) else None
        for key in ("all", reason):
            wall[key].append(span)
            device[key].append(duration)
        if span is not None and duration is not None:
            overhead.append(span - duration)
        # 两个 VAD 事实是可选的：旧平板一个都不发，照样参与其它统计。
        flag = payload.get("vad_armed")
        armed["true" if flag is True else "false" if flag is False else "missing"] += 1
        voiced_ms = payload.get("vad_voiced_ms")
        if reason == "silence" and type(voiced_ms) is int and 0 <= voiced_ms <= 600_000:
            voiced.append(voiced_ms)
    total = sum(reasons.values())
    shown = [*STOP_REASONS, *(["other"] if reasons["other"] else [])]
    return {
        "record_stopped": total,
        "stop_reasons": {reason: {"count": reasons[reason],
                                  "share": _share(reasons[reason], total)}
                         for reason in shown},
        "wall_seconds": {key: _stats(wall[key]) for key in ("all", *shown)},
        "device_duration_seconds": {key: _stats(device[key]) for key in ("all", *shown)},
        "stop_to_ack_overhead_seconds": _stats(overhead),
        "vad_armed": armed,
        "silence_vad_voiced_ms": _stats(voiced),
        "baseline_2026_09_17": BASELINE_RECORDING,
    }


def _following_commands(snap: Snapshot) -> dict[int, dict]:
    by_session: dict[str, list] = defaultdict(list)
    for command in snap.commands.values():
        by_session[command["session_id"]].append(command)
    following = {}
    for commands in by_session.values():
        commands.sort(key=lambda command: command["seq"])
        for current, after in zip(commands, commands[1:]):
            following[current["id"]] = after
    return following


def _elder_to_robot(snap: Snapshot) -> dict:
    missing = (snap.absent.get("commands") or snap.absent.get("acks")
               or snap.absent.get("attempts"))
    if missing:
        return {"na": missing}
    by_audio = {attempt["raw_audio_id"]: attempt for attempt in snap.attempts
                if attempt["raw_audio_id"]}
    following = _following_commands(snap)
    spans: dict[str, list] = defaultdict(list)
    by_mode: dict[str, list] = defaultdict(list)
    excluded = dict.fromkeys(EXCLUDED_LABELS, 0)
    for record in _records(snap):
        stop = snap.acks.get(record["id"], {}).get("record_stopped")
        if stop is None:
            continue
        attempt = by_audio.get(stop["raw_audio_id"])
        if attempt is None:
            excluded["no_attempt"] += 1
            continue
        if attempt["status"] == "technical_failure":
            excluded["technical_failure"] += 1
            continue
        judged = _to_utc(attempt["processed_at"])
        if attempt["status"] != "completed" or judged is None:
            excluded["not_judged"] += 1
            continue
        after = following.get(record["id"])
        if after is None or after["kind"] != "tts":
            excluded["no_next_tts"] += 1
            continue
        # 暂停与续弹都会换控制代际：中间那段是人按的暂停，不是延迟。
        if after["generation"] != record["generation"]:
            excluded["crossed_pause"] += 1
            continue
        spoken = snap.acks.get(after["id"], {}).get("tts_started")
        if spoken is None:
            excluded["next_tts_not_started"] += 1
            continue
        spans["total"].append(_span(stop["at"], spoken["at"]))
        asr_done = snap.asr_done.get(attempt["id"])
        if asr_done is None:
            spans["combined"].append(_span(stop["at"], judged))
        else:
            judge = _span(asr_done, attempt["processed_at"])
            spans["upload_asr"].append(_span(stop["at"], _to_utc(asr_done)))
            spans["judge"].append(judge)
            by_mode[JUDGE_MODES.get(attempt["judge_mode"], "other")].append(judge)
        spans["issue"].append(_span(judged, after["created_at"]))
        spans["fetch_play"].append(_span(after["created_at"], spoken["at"]))
    asr_missing = snap.absent.get("interactions")
    modes = ["rule", "rubric", "llm", *(["other"] if by_mode["other"] else [])]
    return {
        "total": _stats(spans["total"]),
        "upload_asr": {"na": asr_missing} if asr_missing else _stats(spans["upload_asr"]),
        "judge": {"na": asr_missing} if asr_missing else _stats(spans["judge"]),
        "judge_by_mode": ({"na": asr_missing} if asr_missing
                          else {mode: _stats(by_mode[mode]) for mode in modes}),
        "issue": _stats(spans["issue"]),
        "fetch_play": _stats(spans["fetch_play"]),
        "asr_plus_judge_without_asr_timestamp": _stats(spans["combined"]),
        "excluded": excluded,
        "baseline_2026_09_17": BASELINE_ELDER_TO_ROBOT,
    }


def _llm_judge(snap: Snapshot) -> dict:
    missing = snap.absent.get("attempts")
    if missing:
        return {"na": missing}
    completed = [attempt for attempt in snap.attempts if attempt["status"] == "completed"]
    llm = [attempt for attempt in completed
           if JUDGE_MODES.get(attempt["judge_mode"]) == "llm"]
    result: dict = {"llm_judged": len(llm)}
    reason_missing = snap.absent.get("judge_reason")
    if reason_missing:
        result.update(reason_chars={"na": reason_missing},
                      reason_missing={"na": reason_missing},
                      reason_over_40_chars={"na": reason_missing})
    else:
        lengths = [attempt["reason_chars"] for attempt in llm
                   if attempt["reason_chars"] is not None]
        over = sum(1 for length in lengths if length > 40)
        result.update(reason_chars=_stats(lengths),
                      reason_missing=len(llm) - len(lengths),
                      reason_over_40_chars={"count": over,
                                            "share": _share(over, len(lengths))})
    asr_missing = snap.absent.get("interactions")
    if asr_missing:
        result["rule_rubric_judging"] = {"na": asr_missing}
    else:
        timed = [
            _span(snap.asr_done.get(attempt["id"]), attempt["processed_at"])
            for attempt in completed
            if JUDGE_MODES.get(attempt["judge_mode"]) in ("rule", "rubric")
        ]
        timed = [span for span in timed if span is not None]
        result["rule_rubric_judging"] = {
            "timed": len(timed), "over_1s": sum(1 for span in timed if span > 1.0)}
    return result


def _prompt_playback(snap: Snapshot) -> dict:
    missing = snap.absent.get("commands") or snap.absent.get("acks")
    if missing:
        return {"na": missing}
    outcome_keys = ("ended", "interrupted", "failed", "no_terminal_ack")
    by_purpose = {purpose: dict.fromkeys(outcome_keys, 0) for purpose in TTS_PURPOSES}
    reasons, played = [], []
    for command in snap.commands.values():
        if command["kind"] != "tts":
            continue
        purpose = command["purpose"] if command["purpose"] in TTS_PURPOSES else "other"
        row = by_purpose.setdefault(purpose, dict.fromkeys(outcome_keys, 0))
        acks = snap.acks.get(command["id"], {})
        interrupted = acks.get("tts_interrupted")
        if "tts_ended" in acks:
            row["ended"] += 1
        elif interrupted is not None:
            row["interrupted"] += 1
        elif "tts_failed" in acks:
            row["failed"] += 1
        else:
            row["no_terminal_ack"] += 1
        if interrupted is not None:
            reasons.append(interrupted["payload"].get("interrupt_reason"))
            played_ms = interrupted["payload"].get("media_duration_ms")
            if type(played_ms) is int and played_ms >= 0:
                played.append(played_ms / 1000)
    return {
        "by_purpose": by_purpose,
        "interrupt_reasons": _tally(reasons),
        "interrupted_played_seconds": _stats(played),
    }


def _abort_reason(end_reason: str | None) -> str:
    if not end_reason:
        return "missing"
    # 中止原因落库是 abort:v1:<reason_code>:<revision>:<幂等键摘要>，只认中间那段码。
    value = end_reason.split(":")[2] if end_reason.startswith("abort:v1:") else end_reason
    return value if value in ABORT_REASONS else "other"


def _researcher_controls(snap: Snapshot) -> dict:
    control_missing = snap.absent.get("control")
    events = snap.control_events

    def of(event_type: str) -> list[dict]:
        return [event for event in events if event["event_type"] == event_type]

    adjudication_missing = snap.absent.get("adjudications")
    interaction_missing = snap.absent.get("interactions")
    runtime_missing = snap.absent.get("runtime")
    aborted = [state for state in snap.runtime.values() if state["status"] == "aborted"]
    return {
        "adjudications": {"na": adjudication_missing} if adjudication_missing else {
            "total": len(snap.adjudications),
            "by_kind": _tally(row["kind"] for row in snap.adjudications),
            "by_reason": _tally(row["reason_code"] for row in snap.adjudications),
        },
        "resumes": {"na": control_missing} if control_missing else {
            "total": len(of("resume")),
            "by_to_status": _tally(event["to_status"] for event in of("resume")),
        },
        "pauses_by_reason": ({"na": control_missing} if control_missing
                             else _tally(event["reason_code"] for event in of("pause"))),
        "patient_requested_pauses": ({"na": interaction_missing} if interaction_missing
                                     else snap.patient_pauses),
        "takeovers": {"na": control_missing} if control_missing else len(of("takeover")),
        "aborts": {"na": runtime_missing} if runtime_missing else {
            "total": len(aborted),
            "by_reason": _tally(_abort_reason(state["end_reason"]) for state in aborted),
        },
    }


def _technical_failures(snap: Snapshot) -> dict:
    ack_missing = snap.absent.get("commands") or snap.absent.get("acks")

    def failed_acks(ack_type: str) -> dict:
        if ack_missing:
            return {"na": ack_missing}
        codes = [acks[ack_type]["payload"].get("error_code")
                 for acks in snap.acks.values() if ack_type in acks]
        return {"total": len(codes), "by_error_code": _tally(codes)}

    attempt_missing = snap.absent.get("attempts")
    failed = [attempt["error_code"] for attempt in snap.attempts
              if attempt["status"] == "technical_failure"]
    return {
        "attempts": ({"na": attempt_missing} if attempt_missing
                     else {"total": len(failed), "by_error_code": _tally(failed)}),
        "tts_failed": failed_acks("tts_failed"),
        "record_failed": failed_acks("record_failed"),
    }


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
        "metrics": {
            "question_end_to_recording_start": _question_to_mic(snap),
            "recording": _recording(snap),
            "elder_stops_to_robot_speaks": _elder_to_robot(snap),
            "llm_judge": _llm_judge(snap),
            "prompt_playback": _prompt_playback(snap),
            "researcher_controls": _researcher_controls(snap),
            "technical_failures": _technical_failures(snap),
        },
    }


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

TABLE_HEAD = ["| 段 | n | 中位 | p90 | 最大 | 基线 9/17 中位 / p90 |",
              "|---|---|---|---|---|---|"]


def _cell(value) -> str:
    return "—" if value is None else str(value)


def _sec(value) -> str:
    return "—" if value is None else f"{value:.2f}"


def _num(value) -> str:
    return "—" if value is None else f"{value:g}"


def _pct(share) -> str:
    return "—" if share is None else f"{share * 100:.1f}%"


def _na(value) -> str | None:
    return f"n/a: {value['na']}" if isinstance(value, dict) and "na" in value else None


def _row(label: str, stats: dict, baseline: str = "—") -> str:
    if _na(stats):
        return f"| {label} | {_na(stats)} | | | | {baseline} |"
    return (f"| {label} | {stats['n']} | {_sec(stats['median'])} | "
            f"{_sec(stats['p90'])} | {_sec(stats['max'])} | {baseline} |")


def _inline(stats: dict, fmt=_num) -> str:
    return _na(stats) or (
        f"n={stats['n']} · 中位 {fmt(stats['median'])} · p90 {fmt(stats['p90'])}"
        f" · 最大 {fmt(stats['max'])}")


def _counts(value) -> str:
    return _na(value) or " · ".join(
        f"{code} {count}" for code, count in value.items()) or "无"


def _md_question_to_mic(metric: dict) -> list[str]:
    lines = ["## 1. 提问念完 → 开麦", ""]
    if _na(metric):
        return lines + [_na(metric), ""]
    base = metric["baseline_2026_09_17"]
    return lines + TABLE_HEAD + [
        _row("tts_ended → record_started", metric["tts_ended_to_record_started"],
             f"{base['median']:.2f} / {base['p90']:.2f}"),
        _row("tts_interrupted → record_started（打断后开麦）",
             metric["tts_interrupted_to_record_started"]),
        "",
        f"量不到的录音命令（没开麦或找不到触发它的回执）：{metric['records_unmeasured']} 条",
        "",
    ]


def _md_recording(metric: dict) -> list[str]:
    lines = ["## 2. 录音段与收麦原因", ""]
    if _na(metric):
        return lines + [_na(metric), ""]
    base = metric["baseline_2026_09_17"]
    lines += [f"record_stopped 共 {metric['record_stopped']} 条。", "",
              "| stop_reason | 条数 | 占比 | 基线 9/17 |", "|---|---|---|---|"]
    for reason, row in metric["stop_reasons"].items():
        baseline = f"{base[reason]} / {base['stops']}" if reason in base else "—"
        lines.append(f"| {reason} | {row['count']} | {_pct(row['share'])} | {baseline} |")
    lines += [""] + TABLE_HEAD
    for label, key, baseline in (
            ("墙钟 record_started → record_stopped", "wall_seconds",
             f"{base['wall_median']:.2f} / {base['wall_p90']:.2f}"),
            ("平板报的录音时长 duration_seconds", "device_duration_seconds", "—")):
        lines.append(_row(label, metric[key]["all"], baseline))
        lines += [_row(f"└ {reason}", stats)
                  for reason, stats in metric[key].items() if reason != "all"]
    armed = metric["vad_armed"]
    return lines + [
        _row("停录 → 回执开销 ≈ 墙钟 − 平板时长（含上传）",
             metric["stop_to_ack_overhead_seconds"]),
        "",
        f"vad_armed：true {armed['true']} · false {armed['false']} · "
        f"缺（旧平板没上报）{armed['missing']}",
        f"silence 收麦的 vad_voiced_ms（毫秒）：{_inline(metric['silence_vad_voiced_ms'])}",
        "",
    ]


def _md_elder_to_robot(metric: dict) -> list[str]:
    lines = ["## 3. 老人说完 → 机器人开口", ""]
    if _na(metric):
        return lines + [_na(metric), ""]
    base = metric["baseline_2026_09_17"]
    lines += TABLE_HEAD + [
        _row("合计 record_stopped → 下一条 tts_started", metric["total"],
             f"{base['total_median']:.2f} / {base['total_p90']:.2f}"),
        _row("upload+ASR：record_stopped → ASR 落行", metric["upload_asr"],
             f"{base['upload_asr_median']:.2f}"),
        _row("判分：ASR 落行 → 判完", metric["judge"], f"{base['judge_median']:.2f}"),
    ]
    if not _na(metric["judge_by_mode"]):
        lines += [_row(f"└ {JUDGE_LABELS.get(mode, mode)}", stats)
                  for mode, stats in metric["judge_by_mode"].items()]
    return lines + [
        _row("签发：判完 → 下一条命令建行", metric["issue"],
             f"{base['issue_median']:.2f}"),
        _row("取音+开播：命令建行 → tts_started", metric["fetch_play"],
             f"{base['fetch_play_median']:.2f}"),
        _row("没有 ASR 落行时刻的：record_stopped → 判完",
             metric["asr_plus_judge_without_asr_timestamp"]),
        "",
        "不计入：" + " · ".join(f"{EXCLUDED_LABELS[key]} {count}"
                             for key, count in metric["excluded"].items()),
        "",
        "平板先传完录音才发 record_stopped，所以 upload+ASR 这一段实际是服务端排队 + ASR；"
        "上传落在第 2 节「停录 → 回执开销」那一行。",
        "",
    ]


def _md_llm_judge(metric: dict) -> list[str]:
    lines = ["## 4. LLM 判分", ""]
    if _na(metric):
        return lines + [_na(metric), ""]
    over = metric["reason_over_40_chars"]
    slow = metric["rule_rubric_judging"]
    missing = metric["reason_missing"]
    return lines + [
        f"- LLM 判分 {metric['llm_judged']} 次",
        f"- 判分理由长度（字）：{_inline(metric['reason_chars'])}",
        "- 理由超过 40 字："
        + (_na(over) or f"{over['count']} 次（{_pct(over['share'])}）")
        + "；LLM 判分却没有理由：" + (_na(missing) or f"{missing} 次"),
        "- 规则/rubric 判分超过 1 s："
        + (_na(slow) or f"{slow['over_1s']} / {slow['timed']}")
        + "（分母 = 有 ASR 落行时刻可量的次数；LLM 超时退回规则判的旁证）",
        "",
    ]


def _md_prompt_playback(metric: dict) -> list[str]:
    lines = ["## 5. 话术播放", ""]
    if _na(metric):
        return lines + [_na(metric), ""]
    lines += ["| 话术 | 播完 tts_ended | 被打断 tts_interrupted | 失败 tts_failed | 没有终态回执 |",
              "|---|---|---|---|---|"]
    for purpose, row in metric["by_purpose"].items():
        lines.append(f"| {purpose} | {row['ended']} | {row['interrupted']} | "
                     f"{row['failed']} | {row['no_terminal_ack']} |")
    return lines + [
        "",
        f"打断原因：{_counts(metric['interrupt_reasons'])}",
        f"打断前已播（秒）：{_inline(metric['interrupted_played_seconds'], _sec)}",
        "",
    ]


def _md_researcher_controls(metric: dict) -> list[str]:
    adjudications = metric["adjudications"]
    resumes = metric["resumes"]
    aborts = metric["aborts"]
    patient = metric["patient_requested_pauses"]
    takeovers = metric["takeovers"]
    return [
        "## 6. 研究者操作",
        "",
        "- 现场裁定：" + (_na(adjudications) or (
            f"{adjudications['total']} 条；按种类 {_counts(adjudications['by_kind'])}；"
            f"按原因 {_counts(adjudications['by_reason'])}")),
        "- 续弹（resume）：" + (_na(resumes) or (
            f"{resumes['total']} 次；续到 {_counts(resumes['by_to_status'])}")),
        "- 暂停事件按原因：" + _counts(metric["pauses_by_reason"]),
        "- 老人主动暂停（patient_requested_pause 事件）：" + (_na(patient) or f"{patient} 次"),
        "- 接管（takeover）：" + (_na(takeovers) or f"{takeovers} 次"),
        "- 中止：" + (_na(aborts) or (
            f"{aborts['total']} 场；按原因 {_counts(aborts['by_reason'])}")),
        "",
    ]


def _md_technical_failures(metric: dict) -> list[str]:
    lines = ["## 7. 技术失败", ""]
    for label, key in (("technical_failure 的 attempt", "attempts"),
                       ("tts_failed 回执", "tts_failed"),
                       ("record_failed 回执", "record_failed")):
        row = metric[key]
        lines.append(f"- {label}：" + (_na(row) or (
            f"{row['total']}；{_counts(row['by_error_code'])}")))
    return lines + [""]


def render_markdown(report: dict) -> str:
    scope = report["scope"]
    clock = report["clock"]
    metrics = report["metrics"]
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
        "- 口径：n / 中位 / p90（线性插值）/ 最大，单位秒；基线 = 2026-09-16/17 养老院"
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
    lines.append("")
    lines += _md_question_to_mic(metrics["question_end_to_recording_start"])
    lines += _md_recording(metrics["recording"])
    lines += _md_elder_to_robot(metrics["elder_stops_to_robot_speaks"])
    lines += _md_llm_judge(metrics["llm_judge"])
    lines += _md_prompt_playback(metrics["prompt_playback"])
    lines += _md_researcher_controls(metrics["researcher_controls"])
    lines += _md_technical_failures(metrics["technical_failures"])
    return "\n".join(lines)


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
