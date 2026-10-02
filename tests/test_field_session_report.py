"""scripts/field_session_report.py：养老院场次之后从生产库只读出的现场报告。

合成库用 SQLModel.metadata.create_all 建表、ORM 落行，存储格式与生产同口径：
RuntimeCommand / RuntimeCommandAck / 控制事件存 UTC，AttemptEvent / InteractionEvent
存服务进程本地时间（生产 TZ=Asia/Shanghai，+8 h）。时间全部写成相对每场 base 的秒数，
期望值都是手算出来的。

两场真实场次（ALPHA、BRAVO）覆盖七组指标；另有一场模拟、一场旧日期、一场没有命令的
真实场次，专门用来测范围筛选。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from sqlmodel import Session as DBSession, SQLModel, create_engine

from app.enums import EventLine, PhaseType
from app.models import (
    AttemptEvent,
    AutopilotControlEvent,
    AutopilotPositionAdjudication,
    InteractionEvent,
    Patient,
    RuntimeCommand,
    RuntimeCommandAck,
    Session as TrainSession,
    SessionRuntimeState,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "field_session_report.py"

SHANGHAI = timedelta(hours=8)

ALPHA = "SID-REAL-ALPHA-7c1d"
BRAVO = "SID-REAL-BRAVO-9e2f"
SIM = "SID-SIM-CHARLIE-4b8a"
OLD = "SID-REAL-OLD-DELTA-1f3c"
EMPTY = "SID-REAL-EMPTY-ECHO-6d0e"
PATIENTS = {
    ALPHA: "PID-ZHANG-SAN-0042",
    BRAVO: "PID-LI-SI-0077",
    SIM: "PID-SIM-WANG-WU-0013",
    OLD: "PID-ZHAO-LIU-0005",
    EMPTY: "PID-QIAN-QI-0099",
}

# 只要在输出里出现一次就算泄露的东西：回答原文、判分理由原文、研究者备注、话术原文。
ASR_TEXTS = ("老人回答苹果原文", "不知道呀原文", "那个螺丝原文", "好像是锅原文",
             "我想想原文", "茶杯茶杯原文")
REASON_20 = "理由甲" + "很" * 17
REASON_45 = "理由乙" + "长" * 42
NOTE = "研究者备注：老人说的是螺母不是刘世茂"
SPEECH_TEXT = "请您看看图上这个东西叫什么名字话术原文"
assert len(REASON_20) == 20 and len(REASON_45) == 45


class _Scene:
    """一场合成场次。所有时刻都是相对 base 的秒数，UTC 列与本地列各按生产口径落库。"""

    def __init__(self, db: DBSession, session_id: str, base: datetime,
                 local_offset: timedelta, simulation: bool):
        self.db = db
        self.sid = session_id
        self.base = base
        self.local_offset = local_offset
        self.simulation = simulation
        self.command_seq = 0
        self.device_seq = 0
        self.event_seq = 0
        self.control_seq = 0
        self.attempts = 0

    def utc(self, seconds: float) -> datetime:
        return self.base + timedelta(seconds=seconds)

    def local(self, seconds: float) -> datetime:
        return self.utc(seconds) + self.local_offset

    def _command(self, kind: str, created: float, generation: int,
                 payload: dict, **extra) -> RuntimeCommand:
        self.command_seq += 1
        command = RuntimeCommand(
            idempotency_key=f"{self.sid}-cmd-{self.command_seq}",
            session_id=self.sid,
            command_seq=self.command_seq,
            item_id=f"item-{self.command_seq}",
            turn_seq=1,
            turn_key=f"item-{self.command_seq}#1",
            attempt_seq=1,
            prompt_level=payload.get("cue_level", 0),
            control_generation=generation,
            runner_generation=1,
            issued_capability_token_hash="c" * 64,
            issued_device_id_hash="d" * 64,
            issued_at=self.utc(created),
            kind=kind,
            payload_json=json.dumps(payload, ensure_ascii=False),
            created_at=self.utc(created),
            updated_at=self.utc(created),
            **extra,
        )
        self.db.add(command)
        self.db.flush()
        return command

    def _ack(self, command: RuntimeCommand, ack_type: str, at: float,
             payload: dict, **capture) -> str:
        self.device_seq += 1
        key = f"{self.sid}-ack-{self.device_seq}"
        self.db.add(RuntimeCommandAck(
            command_id=command.id,
            idempotency_key=key,
            session_id=self.sid,
            ack_type=ack_type,
            command_revision=0,
            control_generation=command.control_generation,
            runner_generation=1,
            device_event_seq=self.device_seq,
            device_id_hash="d" * 64,
            capability_token_hash="c" * 64,
            payload_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            received_at=self.utc(at),
            **capture,
        ))
        self.db.flush()
        return key

    def tts(self, purpose: str, created: float, *, started: float | None = None,
            ended: float | None = None, interrupted: tuple | None = None,
            failed: tuple | None = None, generation: int = 1,
            cue_level: int = 0) -> tuple[RuntimeCommand, dict]:
        lifecycle: dict = {"state": "pending"}
        if started is not None:
            lifecycle = {"state": "started", "started_at": self.utc(started)}
        if ended is not None:
            lifecycle.update(state="succeeded", succeeded_at=self.utc(ended))
        elif interrupted is not None:
            lifecycle.update(state="cancelled", cancelled_at=self.utc(interrupted[0]))
        elif failed is not None:
            lifecycle.update(state="failed", failed_at=self.utc(failed[0]))
        command = self._command("tts", created, generation, {
            "schema_version": 1,
            "speech_key": f"p0a.{purpose}.{self.command_seq + 1}",
            "speech_text": SPEECH_TEXT,
            "purpose": purpose,
            "item_id": "item",
            "turn_seq": 1,
            "cue_level": cue_level,
        }, **lifecycle)
        keys = {}
        if started is not None:
            self._ack(command, "tts_started", started, {"media_duration_ms": 4000})
        if ended is not None:
            keys["ended"] = self._ack(command, "tts_ended", ended, {
                "media_duration_ms": 4000, "media_ended": True})
        if interrupted is not None:
            at, reason, played_ms = interrupted
            keys["interrupted"] = self._ack(command, "tts_interrupted", at, {
                "interrupt_reason": reason, "media_duration_ms": played_ms,
                "media_stopped": True})
        if failed is not None:
            at, code = failed
            self._ack(command, "tts_failed", at, {"error_code": code})
        return command, keys

    def record(self, tts: RuntimeCommand, trigger_key: str, *, started: float,
               stopped: float | None = None, stop_payload: dict | None = None,
               duration: float | None = None,
               failed: tuple | None = None) -> str:
        raw = f"{self.sid}-raw-{self.command_seq + 1}"
        lifecycle: dict = {"state": "started", "started_at": self.utc(started)}
        if stopped is not None:
            lifecycle.update(state="succeeded", succeeded_at=self.utc(stopped))
        elif failed is not None:
            lifecycle.update(state="failed", failed_at=self.utc(failed[0]))
        command = self._command("record", started - 0.2, tts.control_generation, {
            "schema_version": 1,
            "raw_audio_id": raw,
            "turn_key": f"item-{self.command_seq + 1}#1",
            "item_id": f"item-{self.command_seq + 1}",
            "turn_seq": 1,
            "cue_level": 0,
            "max_duration_seconds": 15,
        }, predecessor_command_id=tts.id, trigger_ack_idempotency_key=trigger_key,
            expected_raw_audio_id=raw, **lifecycle)
        self._ack(command, "record_started", started, {
            "channels": 1, "mime_type": "audio/webm", "sample_rate_hz": 48000})
        if stopped is not None:
            self._ack(command, "record_stopped", stopped, stop_payload,
                      receipt_server_seq=command.id, raw_audio_id=raw,
                      checksum="e" * 64, byte_count=1000,
                      duration_seconds=duration)
        if failed is not None:
            at, code = failed
            self._ack(command, "record_failed", at, {"error_code": code})
        return raw

    def interaction(self, event_type: str, at: float,
                    attempt: AttemptEvent | None = None) -> None:
        self.event_seq += 1
        self.db.add(InteractionEvent(
            session_id=self.sid,
            event_seq=self.event_seq,
            item_id=attempt.item_id if attempt else None,
            turn_seq=1 if attempt else None,
            attempt_id=attempt.id if attempt else None,
            attempt_seq=attempt.attempt_seq if attempt else None,
            event_type=event_type,
            payload_json="{}",
            created_at=self.local(at),
            is_simulation=self.simulation,
        ))
        self.db.flush()

    def attempt(self, raw: str, *, created: float, processed: float,
                status: str = "completed", judge_mode: str | None = None,
                judge_reason: str | None = None, error_code: str | None = None,
                asr_text: str = "", asr_event: bool = True) -> AttemptEvent:
        self.attempts += 1
        attempt = AttemptEvent(
            session_id=self.sid,
            item_id=f"attempt-item-{self.attempts}",
            turn_seq=1,
            response_role="命名",
            attempt_seq=1,
            raw_audio_id=raw,
            prompt_level=0,
            asr_text=asr_text,
            judge_mode=judge_mode,
            judge_reason=judge_reason,
            processing_status=status,
            error_code=error_code,
            created_at=self.local(created),
            processed_at=self.local(processed),
            is_simulation=self.simulation,
        )
        self.db.add(attempt)
        self.db.flush()
        if asr_event:
            # 生产上 attempt 行与 asr_completed 事件同一事务落库：ASR 出结果才建行。
            self.interaction("asr_completed", created, attempt)
        return attempt

    def control(self, event_type: str, at: float, reason_code: str,
                to_status: str, *, generation: int = 1,
                actor: str = "researcher") -> None:
        self.control_seq += 1
        self.db.add(AutopilotControlEvent(
            idempotency_key=f"{self.sid}-control-{self.control_seq}",
            session_id=self.sid,
            event_seq=self.control_seq,
            event_type=event_type,
            scope_key="p0a_sim_first_single_v1",
            control_generation=generation,
            runner_generation=1,
            actor_type=actor,
            actor_id="researcher-display-id" if actor == "researcher" else None,
            reason_code=reason_code,
            from_mode="autonomous",
            to_mode="autonomous",
            to_status=to_status,
            payload_json="{}",
            created_at=self.utc(at),
        ))
        self.db.flush()

    def adjudicate(self, attempt: AttemptEvent, kind: str, reason: str,
                   at: float) -> None:
        self.db.add(AutopilotPositionAdjudication(
            session_id=self.sid,
            item_id=attempt.item_id,
            turn_seq=1,
            presentation_order=1,
            kind=kind,
            reason_code=reason,
            note=NOTE,
            actor_id="researcher-display-id",
            source_attempt_id=attempt.id,
            turn_event_id=1,
            control_generation=1,
            state_revision=1,
            idempotency_key=f"{self.sid}-adjudicate-1",
            created_at=self.utc(at),
        ))
        self.db.flush()

    def runtime(self, status: str, end_reason: str) -> None:
        self.db.add(SessionRuntimeState(
            session_id=self.sid, status=status, end_reason=end_reason))
        self.db.flush()


def _new_scene(db: DBSession, session_id: str, training_date: date, week_no: int,
               base: datetime, local_offset: timedelta,
               simulation: bool = False) -> _Scene:
    db.add(Patient(patient_id=PATIENTS[session_id],
                   is_simulation_subject=simulation))
    db.add(TrainSession(
        session_id=session_id,
        patient_id=PATIENTS[session_id],
        training_date=training_date,
        week_no=week_no,
        phase_type=PhaseType.正式训练,
        event_line=EventLine.正式训练,
        item_bank_version_id="bank-v1",
        is_simulation=simulation,
        data_classification="simulation" if simulation else "research",
    ))
    db.flush()
    return _Scene(db, session_id, base, local_offset, simulation)


def _seed_alpha(scene: _Scene) -> None:
    c1, keys = scene.tts("question", 0.0, started=1.0, ended=6.0)
    raw = scene.record(c1, keys["ended"], started=7.2, stopped=13.2, duration=5.5,
                       stop_payload={"stop_reason": "silence", "vad_armed": True,
                                     "vad_voiced_ms": 2400})
    scene.attempt(raw, created=15.2, processed=16.7, judge_mode="LLM辅助",
                  judge_reason=REASON_20, asr_text=ASR_TEXTS[0])
    scene.tts("feedback", 16.75, started=18.25, ended=20.0)

    c4, keys = scene.tts("question", 20.1, started=21.0, ended=25.0)
    raw = scene.record(c4, keys["ended"], started=26.4, stopped=36.4, duration=9.6,
                       stop_payload={"stop_reason": "user_done", "vad_armed": False})
    scene.attempt(raw, created=39.4, processed=39.6, judge_mode="规则确定式",
                  asr_text=ASR_TEXTS[1])
    c6, keys = scene.tts("cue", 39.7, started=41.7, cue_level=1,
                         interrupted=(43.0, "voice_activity", 1300))
    # 旧平板：record_stopped 只有 stop_reason，没有 VAD 两个可选事实。
    raw = scene.record(c6, keys["interrupted"], started=43.4, stopped=57.6,
                       duration=14.0, stop_payload={"stop_reason": "max_duration"})
    scene.attempt(raw, created=60.1, processed=61.3, judge_mode="版本化规则",
                  asr_text=ASR_TEXTS[2])
    scene.tts("feedback", 61.35, started=63.15, ended=65.0)

    c9, keys = scene.tts("question", 65.1, started=66.0,
                         interrupted=(68.0, "answer_now", 2000))
    raw = scene.record(c9, keys["interrupted"], started=68.6, stopped=72.6,
                       duration=3.7, stop_payload={"stop_reason": "silence",
                                                   "vad_armed": True,
                                                   "vad_voiced_ms": 600})
    scene.attempt(raw, created=75.6, processed=75.6, status="technical_failure",
                  error_code="asr_exception", asr_event=False)
    scene.control("failure", 75.7, "asr_exception", "paused", actor="system")
    scene.control("resume", 190.0, "researcher_explicit_resume", "waiting_tts",
                  generation=2)

    c11, keys = scene.tts("question", 200.0, started=201.0, ended=204.0, generation=2)
    raw = scene.record(c11, keys["ended"], started=205.3, stopped=211.3, duration=5.8,
                       stop_payload={"stop_reason": "user_done", "vad_armed": True,
                                     "vad_voiced_ms": 3000})
    scene.attempt(raw, created=213.8, processed=214.0, judge_mode="规则确定式",
                  asr_text=ASR_TEXTS[3])
    scene.tts("feedback", 214.1, started=216.1, generation=2,
              failed=(217.0, "audio_playback_failed"))
    scene.runtime("completed", "completion_gate_passed")


def _seed_bravo(scene: _Scene) -> None:
    d1, keys = scene.tts("question", 0.0, started=0.8, ended=4.0)
    raw = scene.record(d1, keys["ended"], started=5.1, stopped=9.1, duration=3.6,
                       stop_payload={"stop_reason": "silence", "vad_armed": True,
                                     "vad_voiced_ms": 1800})
    scene.attempt(raw, created=11.6, processed=15.6, judge_mode="LLM辅助",
                  judge_reason=REASON_45, asr_text=ASR_TEXTS[4])
    scene.tts("feedback", 15.65, started=17.15, ended=19.0)

    d4, keys = scene.tts("question", 19.1, started=20.0, ended=24.0)
    raw = scene.record(d4, keys["ended"], started=25.0, stopped=31.0, duration=5.7,
                       stop_payload={"stop_reason": "user_done", "vad_armed": False})
    answered = scene.attempt(raw, created=33.0, processed=34.5,
                             judge_mode="规则确定式", asr_text=ASR_TEXTS[5])
    # 老人按了暂停，研究者续弹：下一条 TTS 已是新的控制代际，不算「说完→开口」。
    scene.interaction("patient_requested_pause", 34.8)
    scene.control("pause", 34.8, "patient_requested_pause", "paused", actor="device")
    scene.control("resume", 290.0, "researcher_explicit_resume", "waiting_tts",
                  generation=2)
    d6, keys = scene.tts("cue", 300.0, started=301.0, ended=305.0, generation=2,
                         cue_level=1)
    scene.record(d6, keys["ended"], started=306.2,
                 failed=(310.0, "microphone_unavailable"))
    scene.adjudicate(answered, "confirmed_correct", "asr_misrecognized", 315.0)
    scene.control("takeover", 320.0, "researcher_explicit_takeover",
                  "manual_draining", generation=2)
    scene.runtime("aborted", "abort:v1:participant_declined:7:" + "a" * 64)


def _seed_simulation(scene: _Scene) -> None:
    s1, keys = scene.tts("question", 0.0, started=1.0, ended=4.0)
    raw = scene.record(s1, keys["ended"], started=13.0, stopped=20.0, duration=6.5,
                       stop_payload={"stop_reason": "user_done", "vad_armed": False})
    scene.attempt(raw, created=22.0, processed=23.0, judge_mode="规则确定式")
    scene.tts("feedback", 23.05, started=25.0, ended=27.0)


def _seed_old(scene: _Scene) -> None:
    o1, keys = scene.tts("question", 0.0, started=1.0, ended=4.0)
    raw = scene.record(o1, keys["ended"], started=7.0, stopped=12.0, duration=4.6,
                       stop_payload={"stop_reason": "user_done"})
    scene.attempt(raw, created=14.0, processed=15.0, judge_mode="规则确定式")
    scene.tts("feedback", 15.05, started=17.0, ended=19.0)


def _build_db(path: Path, *, local_offset: timedelta = SHANGHAI) -> Path:
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    with DBSession(engine) as db:
        _seed_alpha(_new_scene(db, ALPHA, date(2026, 10, 3), 2,
                               datetime(2026, 10, 3, 1, 0, 0), local_offset))
        _seed_simulation(_new_scene(db, SIM, date(2026, 10, 3), 2,
                                    datetime(2026, 10, 3, 6, 0, 0), local_offset,
                                    simulation=True))
        _seed_bravo(_new_scene(db, BRAVO, date(2026, 10, 4), 3,
                               datetime(2026, 10, 4, 1, 0, 0), local_offset))
        _seed_old(_new_scene(db, OLD, date(2026, 9, 20), 2,
                             datetime(2026, 9, 20, 1, 0, 0), local_offset))
        _new_scene(db, EMPTY, date(2026, 10, 5), 4,
                   datetime(2026, 10, 5, 1, 0, 0), local_offset)
        db.commit()
    engine.dispose()
    return path


@pytest.fixture
def field_db(tmp_path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    return _build_db(directory / "app.db")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=60)


def _report(db: Path, *args: str) -> dict:
    done = _run("--db", str(db), "--json", *args)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 只读、拒绝与范围
# ---------------------------------------------------------------------------

def test_refuses_a_database_path_that_does_not_exist(tmp_path):
    missing = tmp_path / "nope" / "app.db"
    done = _run("--db", str(missing))
    assert done.returncode == 2
    assert done.stdout == ""
    assert "REFUSED code=db_missing" in done.stderr
    # mode=ro 不会顺手建一个空库出来。
    assert not missing.exists() and not missing.parent.exists()


@pytest.mark.parametrize("offset_hours", [0, 9])
def test_refuses_when_local_columns_are_not_eight_hours_ahead(tmp_path, offset_hours):
    db = _build_db(tmp_path / "app.db", local_offset=timedelta(hours=offset_hours))
    done = _run("--db", str(db), "--since", "2026-10-01")
    assert done.returncode == 2
    assert done.stdout == "", "偏移不对时不能先吐半份报告"
    assert "REFUSED code=clock_offset_unexpected" in done.stderr
    assert "+8 h" in done.stderr


def test_database_file_and_directory_are_untouched(field_db):
    before = _sha256(field_db)
    listing = sorted(p.name for p in field_db.parent.iterdir())
    assert _run("--db", str(field_db)).returncode == 0
    assert _run("--db", str(field_db), "--json", "--include-simulation").returncode == 0
    assert _sha256(field_db) == before
    assert sorted(p.name for p in field_db.parent.iterdir()) == listing


def test_clock_offset_is_detected_from_paired_captures(field_db):
    report = _report(field_db, "--since", "2026-10-01")
    assert report["clock"] == {
        "status": "ok",
        "pairs": 7,
        # 8 h + ASR 中位 2.5 s：attempt 是 ASR 出结果才落行的。
        "median_offset_seconds": pytest.approx(28802.5),
        "expected_offset_seconds": 28800,
        "tolerance_seconds": 60,
    }


def test_scope_is_real_sessions_with_commands_numbered_in_date_order(field_db):
    report = _report(field_db, "--since", "2026-10-01")
    assert report["scope"] == {
        "include_simulation": False,
        "since": "2026-10-01",
        "session_count": None,
        "sessions": 2,
        "excluded_without_commands": 1,
    }
    assert report["sessions"] == [
        {"n": 1, "week_no": 2, "commands": 13, "record_stopped": 5, "attempts": 5,
         "runtime_status": "completed"},
        {"n": 2, "week_no": 3, "commands": 7, "record_stopped": 2, "attempts": 2,
         "runtime_status": "aborted"},
    ]


def test_without_since_the_older_real_session_comes_first(field_db):
    report = _report(field_db)
    assert report["scope"]["sessions"] == 3
    assert [row["commands"] for row in report["sessions"]] == [3, 13, 7]


def test_simulation_sessions_only_with_the_flag(field_db):
    report = _report(field_db, "--since", "2026-10-01", "--include-simulation")
    assert report["scope"]["sessions"] == 3
    # 同一天的两场按第一条命令的时刻排：ALPHA 01:00Z 在前，模拟场 06:00Z 在后。
    assert [row["commands"] for row in report["sessions"]] == [13, 3, 7]


def test_session_count_keeps_only_the_latest(field_db):
    report = _report(field_db, "--since", "2026-10-01", "--session-count", "1")
    assert report["scope"]["sessions"] == 1
    assert report["sessions"] == [
        {"n": 1, "week_no": 3, "commands": 7, "record_stopped": 2, "attempts": 2,
         "runtime_status": "aborted"},
    ]


@pytest.mark.parametrize("extra", [[], ["--json"], ["--include-simulation"],
                                   ["--include-simulation", "--json"]])
def test_output_carries_no_identifier_date_or_free_text(field_db, extra):
    done = _run("--db", str(field_db), *extra)
    assert done.returncode == 0, done.stderr
    leaks = [*PATIENTS, *PATIENTS.values(), *ASR_TEXTS, REASON_20, REASON_45, NOTE,
             SPEECH_TEXT, "-raw-", "-ack-", "-cmd-", "-control-", "-adjudicate-",
             "abort:v1", "a" * 64, "c" * 64, "d" * 64, "researcher-display-id",
             "2026-10-03", "2026-10-04", "2026-09-20"]
    found = [value for value in leaks if value in done.stdout]
    assert found == []


def test_usage_docstring_carries_the_exact_production_command():
    text = SCRIPT.read_text(encoding="utf-8")
    assert ("'/opt/nmu/venv/bin/python /opt/nmu/app/scripts/field_session_report.py "
            "--db /opt/nmu/app/data/app.db --since 2026-10-01'") in text


# ---------------------------------------------------------------------------
# 七组指标（--since 2026-10-01 → ALPHA + BRAVO；期望值逐条手算）
# ---------------------------------------------------------------------------

def _stats(n: int, median: float, p90: float, maximum: float) -> dict:
    return {"n": n, "median": pytest.approx(median), "p90": pytest.approx(p90),
            "max": pytest.approx(maximum)}


EMPTY_STATS = {"n": 0, "median": None, "p90": None, "max": None}


@pytest.fixture
def metrics(field_db) -> dict:
    return _report(field_db, "--since", "2026-10-01")["metrics"]


def test_question_end_to_recording_start(metrics):
    assert metrics["question_end_to_recording_start"] == {
        # 1.2 1.4 1.3 1.1 1.0 1.2
        "tts_ended_to_record_started": _stats(6, 1.2, 1.35, 1.4),
        # 打断后开麦：0.4（voice_activity）、0.6（answer_now）
        "tts_interrupted_to_record_started": _stats(2, 0.5, 0.58, 0.6),
        "records_unmeasured": 0,
        "baseline_2026_09_17": {"median": 1.61, "p90": 2.01},
    }


def test_recording_length_by_stop_reason_and_vad_facts(metrics):
    recording = metrics["recording"]
    assert recording["record_stopped"] == 7
    assert recording["stop_reasons"] == {
        "silence": {"count": 3, "share": pytest.approx(0.429)},
        "user_done": {"count": 3, "share": pytest.approx(0.429)},
        "max_duration": {"count": 1, "share": pytest.approx(0.143)},
        "stream_end": {"count": 0, "share": 0.0},
    }
    assert recording["wall_seconds"] == {
        "all": _stats(7, 6.0, 11.68, 14.2),
        "silence": _stats(3, 4.0, 5.6, 6.0),
        "user_done": _stats(3, 6.0, 9.2, 10.0),
        "max_duration": _stats(1, 14.2, 14.2, 14.2),
        "stream_end": EMPTY_STATS,
    }
    assert recording["device_duration_seconds"] == {
        "all": _stats(7, 5.7, 11.36, 14.0),
        "silence": _stats(3, 3.7, 5.14, 5.5),
        "user_done": _stats(3, 5.8, 8.84, 9.6),
        "max_duration": _stats(1, 14.0, 14.0, 14.0),
        "stream_end": EMPTY_STATS,
    }
    assert recording["stop_to_ack_overhead_seconds"] == _stats(7, 0.3, 0.44, 0.5)
    # 旧平板只发 {"stop_reason"}：记作「缺」，照样参与其它统计。
    assert recording["vad_armed"] == {"true": 4, "false": 2, "missing": 1}
    assert recording["silence_vad_voiced_ms"] == _stats(3, 1800, 2280, 2400)
    assert recording["baseline_2026_09_17"] == {
        "stops": 149, "silence": 0, "user_done": 117, "max_duration": 32,
        "stream_end": 0, "wall_median": 7.7, "wall_p90": 15.7}


def test_elder_stops_to_robot_speaks_split(metrics):
    split = metrics["elder_stops_to_robot_speaks"]
    # 五段可配对：5.05 = 2.0+1.5+0.05+1.5，5.3，5.55，4.8，8.05。
    assert split["total"] == _stats(5, 5.3, 7.05, 8.05)
    assert split["upload_asr"] == _stats(5, 2.5, 2.8, 3.0)
    assert split["judge"] == _stats(5, 1.2, 3.0, 4.0)
    assert split["judge_by_mode"] == {
        "rule": _stats(2, 0.2, 0.2, 0.2),
        "rubric": _stats(1, 1.2, 1.2, 1.2),
        "llm": _stats(2, 2.75, 3.75, 4.0),
    }
    assert split["issue"] == _stats(5, 0.05, 0.1, 0.1)
    assert split["fetch_play"] == _stats(5, 1.8, 2.0, 2.0)
    assert split["asr_plus_judge_without_asr_timestamp"] == EMPTY_STATS
    assert split["excluded"] == {
        "no_attempt": 0, "technical_failure": 1, "not_judged": 0,
        "no_next_tts": 0, "crossed_pause": 1, "next_tts_not_started": 0}
    assert split["baseline_2026_09_17"] == {
        "total_median": 8.2, "total_p90": 10.51, "upload_asr_median": 2.94,
        "judge_median": 3.37, "issue_median": 0.04, "fetch_play_median": 1.91}


def test_llm_judge_reason_length_and_slow_rule_judging(metrics):
    assert metrics["llm_judge"] == {
        "llm_judged": 2,
        # 20 与 45 个汉字：按字符数，不是 UTF-8 字节数。
        "reason_chars": _stats(2, 32.5, 42.5, 45),
        "reason_missing": 0,
        "reason_over_40_chars": {"count": 1, "share": pytest.approx(0.5)},
        # 规则/rubric 四次里 1.2 s（rubric）与 1.5 s（规则）超过 1 s。
        "rule_rubric_judging": {"timed": 4, "over_1s": 2},
    }


def test_prompt_playback_outcomes(metrics):
    assert metrics["prompt_playback"] == {
        "by_purpose": {
            "question": {"ended": 5, "interrupted": 1, "failed": 0, "no_terminal_ack": 0},
            "cue": {"ended": 1, "interrupted": 1, "failed": 0, "no_terminal_ack": 0},
            "feedback": {"ended": 3, "interrupted": 0, "failed": 1, "no_terminal_ack": 0},
            "tell_answer": {"ended": 0, "interrupted": 0, "failed": 0,
                            "no_terminal_ack": 0},
        },
        "interrupt_reasons": {"answer_now": 1, "voice_activity": 1},
        "interrupted_played_seconds": _stats(2, 1.65, 1.93, 2.0),
    }


def test_researcher_controls(metrics):
    assert metrics["researcher_controls"] == {
        "adjudications": {"total": 1, "by_kind": {"confirmed_correct": 1},
                          "by_reason": {"asr_misrecognized": 1}},
        "resumes": {"total": 2, "by_to_status": {"waiting_tts": 2}},
        "pauses_by_reason": {"patient_requested_pause": 1},
        "patient_requested_pauses": 1,
        "takeovers": 1,
        "aborts": {"total": 1, "by_reason": {"participant_declined": 1}},
    }


def test_technical_failures(metrics):
    assert metrics["technical_failures"] == {
        "attempts": {"total": 1, "by_error_code": {"asr_exception": 1}},
        "tts_failed": {"total": 1, "by_error_code": {"audio_playback_failed": 1}},
        "record_failed": {"total": 1, "by_error_code": {"microphone_unavailable": 1}},
    }


def test_filters_change_the_aggregates(field_db):
    # 模拟场多一个 9.0 s：p90 = 1.4 + 0.4 × 7.6。
    simulation = _report(field_db, "--since", "2026-10-01", "--include-simulation")
    assert simulation["metrics"]["question_end_to_recording_start"][
        "tts_ended_to_record_started"] == _stats(7, 1.2, 4.44, 9.0)
    # 不给 --since：9/20 那场多一个 3.0 s。
    older = _report(field_db)
    assert older["metrics"]["question_end_to_recording_start"][
        "tts_ended_to_record_started"] == _stats(7, 1.2, 2.04, 3.0)
    latest = _report(field_db, "--since", "2026-10-01", "--session-count", "1")
    assert latest["metrics"]["question_end_to_recording_start"][
        "tts_ended_to_record_started"] == _stats(3, 1.1, 1.18, 1.2)


def test_markdown_prints_the_numbers_beside_the_baseline(field_db):
    done = _run("--db", str(field_db), "--since", "2026-10-01")
    assert done.returncode == 0, done.stderr
    for row in (
        "| tts_ended → record_started | 6 | 1.20 | 1.35 | 1.40 | 1.61 / 2.01 |",
        "| silence | 3 | 42.9% | 0 / 149 |",
        "| 墙钟 record_started → record_stopped | 7 | 6.00 | 11.68 | 14.20 | 7.70 / 15.70 |",
        "| 合计 record_stopped → 下一条 tts_started | 5 | 5.30 | 7.05 | 8.05 | 8.20 / 10.51 |",
        "| upload+ASR：record_stopped → ASR 落行 | 5 | 2.50 | 2.80 | 3.00 | 2.94 |",
        "| └ LLM（LLM辅助） | 2 | 2.75 | 3.75 | 4.00 | — |",
        "| question | 5 | 1 | 0 | 0 |",
        "vad_armed：true 4 · false 2 · 缺（旧平板没上报）1",
    ):
        assert row in done.stdout, row


def test_older_schema_reports_na_instead_of_guessing(tmp_path):
    db = _build_db(tmp_path / "app.db")
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TABLE autopilotpositionadjudication")
        conn.execute("DROP TABLE interactionevent")
        conn.execute("ALTER TABLE attemptevent DROP COLUMN judge_reason")
        conn.commit()
    finally:
        conn.close()
    metrics = _report(db, "--since", "2026-10-01")["metrics"]
    controls = metrics["researcher_controls"]
    assert controls["adjudications"] == {
        "na": "table autopilotpositionadjudication absent"}
    assert controls["patient_requested_pauses"] == {
        "na": "table interactionevent absent"}
    assert controls["resumes"] == {"total": 2, "by_to_status": {"waiting_tts": 2}}
    judge = metrics["llm_judge"]
    assert judge["llm_judged"] == 2
    assert judge["reason_chars"] == {"na": "column attemptevent.judge_reason absent"}
    assert judge["rule_rubric_judging"] == {"na": "table interactionevent absent"}
    split = metrics["elder_stops_to_robot_speaks"]
    assert split["upload_asr"] == {"na": "table interactionevent absent"}
    assert split["judge"] == {"na": "table interactionevent absent"}
    # 没有 ASR 落行时刻只能量 record_stopped → 判完：3.5 3.2 3.7 2.7 6.5。
    assert split["asr_plus_judge_without_asr_timestamp"] == _stats(5, 3.5, 5.38, 6.5)
    assert split["total"] == _stats(5, 5.3, 7.05, 8.05)

    done = _run("--db", str(db), "--since", "2026-10-01")
    assert done.returncode == 0, done.stderr
    assert "n/a: table autopilotpositionadjudication absent" in done.stdout
    assert "n/a: column attemptevent.judge_reason absent" in done.stdout


def test_without_the_ack_table_the_report_still_runs(tmp_path):
    db = _build_db(tmp_path / "app.db")
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TABLE runtimecommandack")
        conn.commit()
    finally:
        conn.close()
    report = _report(db, "--since", "2026-10-01")
    # 没有回执就没有配对：测不出偏移，用到回执的几项写 n/a，不拒绝、不猜。
    assert report["clock"]["status"] == "not_detectable"
    assert report["clock"]["pairs"] == 0
    metrics = report["metrics"]
    for key in ("question_end_to_recording_start", "recording",
                "elder_stops_to_robot_speaks", "prompt_playback"):
        assert metrics[key] == {"na": "table runtimecommandack absent"}, key
    assert metrics["technical_failures"]["tts_failed"] == {
        "na": "table runtimecommandack absent"}
    assert metrics["technical_failures"]["attempts"] == {
        "total": 1, "by_error_code": {"asr_exception": 1}}
    assert [row["record_stopped"] for row in report["sessions"]] == [None, None]
    assert _run("--db", str(db), "--since", "2026-10-01").returncode == 0
