"""Frozen explicit-repeat protocol: registry, exact detector and migration.

These tests never touch the default database and never call a provider: the
detector is a pure function over a versioned definition on disk.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, inspect, text

from app import autopilot_ledger, content, repeat_intent
from app.autopilot_contract import RecordCommandPayload, TtsCommandPayload


REPEAT_REVISION = "d3f8b5c1a704"
CURRENT_HEAD = "b3e7c5a9d214"
# Compatibility export consumed by the zero-modification legacy recovery test.
HEAD = CURRENT_HEAD
PARENT = "c7d4f9a1e603"
APPROVED_VERSION_ID = "repeat-intent-v1-20260730-proposal"
APPROVED_DIGEST = (
    "51e8ce30d6273df52fc25011ed00ebc5fba15b30c9ed98b4ccc146b72e05484f"
)
APPROVED_PHRASES = {
    "再说一遍": "repeat_again",
    "请再说一遍": "repeat_again_polite",
    "我没听清": "did_not_hear_clearly_self",
    "没听清": "did_not_hear_clearly",
    "我没听见": "did_not_hear_self",
    "没听见": "did_not_hear",
    "我没听到": "did_not_catch_self",
    "没听到": "did_not_catch",
}


def _config(db_path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


BANK = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
PROTOCOL = content.load_autopilot_protocol(
    content.CONTENT_DIR / "autopilot_protocol_v1.json")
LEGACY_ITEM = BANK.single_element[0]
LEGACY_ITEM_ID = LEGACY_ITEM["item_id"]
LEGACY_TURN_SEQ = 1
LEGACY_TURN_KEY = f"{LEGACY_ITEM_ID}#{LEGACY_TURN_SEQ}"
LEGACY_RESPONSE_ROLE = "命名"
LEGACY_MAX_DURATION_SECONDS = int(PROTOCOL["silence_seconds"]) + 5
LEGACY_AUDIO_BYTES = b"\x1a\x45\xdf\xa3legacy-pre-repeat-capture"
LEGACY_DEVICE_TOKEN = "legacy-pre-repeat-device-capability-token-01"
LEGACY_DEVICE_ID = "legacy-pre-repeat-device-000001"


@dataclass(frozen=True)
class LegacyChain:
    """Identifiers of one complete pre-protocol capture chain inserted at c7."""

    patient_id: str
    plan_id: str
    session_id: str
    source_command_id: int
    record_command_id: int
    capture_id: int
    raw_audio_id: str
    receipt_server_seq: int
    device_token: str
    device_id_hash: str
    capability_token_hash: str
    item_id: str
    turn_seq: int
    turn_key: str
    checksum: str
    byte_count: int
    duration_seconds: float
    audio_bytes: bytes
    item_bank_version_id: str
    item_bank_definition_digest: str
    autopilot_protocol_version_id: str
    autopilot_protocol_definition_digest: str


def _sql_value(value):
    # SQLAlchemy persists naive DATETIME in this exact textual form; format it
    # here rather than relying on sqlite3's deprecated default adapter.
    return value.isoformat(sep=" ") if isinstance(value, datetime) else value


def _insert(connection, table: str, values: dict) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(_sql_value(value) for value in values.values()))


def _insert_c7_populated_capture_chain(
    db_path,
    *,
    session_id: str = "S-LEGACY-C7",
    patient_id: str = "P-LEGACY-C7",
    plan_id: str = "VP-LEGACY-C7",
    raw_audio_id: str = "raw-legacy-pre-repeat-000001",
    item_bank_definition_digest: str | None = None,
    now: datetime | None = None,
    audio_bytes: bytes = LEGACY_AUDIO_BYTES,
) -> LegacyChain:
    """Insert one complete, legal pre-repeat capture chain into a c7 database.

    Every value here is the shape a real P0a run leaves behind at
    ``processing_attempt``: a started plan, its linked session, an active
    device capability, the succeeded question TTS with its canonical
    ``tts_ended`` ACK and serve evidence, the succeeded recording with its
    canonical ``record_stopped`` ACK, the capture receipt, the uploaded audio
    asset, and the ``received`` capture-processing row.  Nothing is written
    through the head schema: the whole chain exists before ``d3`` ever runs, so
    the ``legacy_pre_repeat`` marker can only come from the migration itself.
    """
    base = now or datetime.now(timezone.utc).replace(tzinfo=None)
    t0 = base - timedelta(seconds=60)
    t1 = base - timedelta(seconds=50)
    t2 = base - timedelta(seconds=45)
    t3 = base - timedelta(seconds=40)
    t4 = base - timedelta(seconds=35)
    t5 = base - timedelta(seconds=30)
    t6 = base - timedelta(seconds=25)
    bank_digest = (item_bank_definition_digest
                   or content.item_bank_definition_digest(BANK))
    protocol_version_id = str(PROTOCOL["protocol_version_id"])
    protocol_digest = content.autopilot_protocol_definition_digest(PROTOCOL)
    capability_token_hash = hashlib.sha256(
        LEGACY_DEVICE_TOKEN.encode("ascii")).hexdigest()
    device_id_hash = hashlib.sha256(
        LEGACY_DEVICE_ID.encode("ascii")).hexdigest()
    checksum = hashlib.sha256(audio_bytes).hexdigest()
    byte_count = len(audio_bytes)
    duration_seconds = 1.5

    tts_payload = TtsCommandPayload(
        speech_key="p0a.question.1",
        speech_text=LEGACY_ITEM["initial_prompt"],
        purpose="question",
        item_id=LEGACY_ITEM_ID,
        turn_seq=LEGACY_TURN_SEQ,
        cue_level=0,
    ).model_dump_json(exclude_none=True)
    record_payload = RecordCommandPayload(
        raw_audio_id=raw_audio_id,
        turn_key=LEGACY_TURN_KEY,
        item_id=LEGACY_ITEM_ID,
        turn_seq=LEGACY_TURN_SEQ,
        cue_level=0,
        max_duration_seconds=LEGACY_MAX_DURATION_SECONDS,
        contains_direct_identifier=False,
    ).model_dump_json(exclude_none=True)

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert(connection, "patient", {
            "patient_id": patient_id,
            "is_simulation_subject": 1,
            "consent_status": "已同意",
            "recording_allowed": 1,
            "governance_revision": 0,
        })
        _insert(connection, "visitplan", {
            "plan_id": plan_id,
            "protocol_slot_key": f"{patient_id}#2#正式训练#1",
            "patient_id": patient_id,
            "scheduled_date": t0.date().isoformat(),
            "session_sitting_no": 1,
            "week_no": 2,
            "phase_type": "正式训练",
            "event_line": "正式训练",
            "item_bank_version_id": BANK.version_id,
            "item_bank_definition_digest": bank_digest,
            "autopilot_protocol_version_id": protocol_version_id,
            "autopilot_protocol_definition_digest": protocol_digest,
            "is_simulation": 1,
            "data_classification": "simulation",
            "status": "started",
            "revision": 3,
            "created_by": "ACTOR-LEGACY",
            "created_at": t0,
            "updated_at": t0,
            "approved_by": "ACTOR-LEGACY",
            "approved_at": t0,
            "started_by": "ACTOR-LEGACY",
            "started_at": t0,
        })
        _insert(connection, "session", {
            "session_id": session_id,
            "patient_id": patient_id,
            "session_sitting_no": 1,
            "week_no": 2,
            "phase_type": "正式训练",
            "event_line": "正式训练",
            "trainer_id": "ACTOR-LEGACY",
            "training_date": t0.date().isoformat(),
            "item_bank_version_id": BANK.version_id,
            "item_bank_definition_digest": bank_digest,
            "autopilot_protocol_version_id": protocol_version_id,
            "autopilot_protocol_definition_digest": protocol_digest,
            "is_simulation": 1,
            "data_classification": "simulation",
            "visit_plan_id": plan_id,
        })
        _insert(connection, "sessionruntimestate", {
            "session_id": session_id,
            "status": "active",
            "revision": 0,
            "updated_at": t0,
        })
        _insert(connection, "livestate", {
            "id": 1,
            "seq": 1,
            "command_wseq": 0,
            "session_json": json.dumps({
                "sessionId": session_id,
                "weekNo": 2,
                "eventLine": "正式训练",
                "mode": "task",
                "itemBankVersionId": BANK.version_id,
            }, ensure_ascii=False),
            "updated_at": t0,
        })
        _insert(connection, "patientdevicecapability", {
            "token_hash": capability_token_hash,
            "session_id": session_id,
            "device_id_hash": device_id_hash,
            "active_session_key": session_id,
            "created_at": t0,
            "expires_at": base + timedelta(days=1),
            "last_seen_at": t0,
            "last_autopilot_event_seq": 2,
        })
        _insert(connection, "runtimecommand", {
            "id": 1,
            "idempotency_key": "cmd-legacy-question-0001",
            "session_id": session_id,
            "command_seq": 1,
            "item_id": LEGACY_ITEM_ID,
            "turn_seq": LEGACY_TURN_SEQ,
            "turn_key": LEGACY_TURN_KEY,
            "attempt_seq": 1,
            "prompt_level": 0,
            "item_bank_version_id": BANK.version_id,
            "item_bank_definition_digest": bank_digest,
            "autopilot_protocol_version_id": protocol_version_id,
            "autopilot_protocol_definition_digest": protocol_digest,
            "response_role": LEGACY_RESPONSE_ROLE,
            "scope_key": "p0a_sim_first_single_v1",
            "control_generation": 1,
            "runner_generation": 1,
            "issued_capability_token_hash": capability_token_hash,
            "issued_device_id_hash": device_id_hash,
            "issued_at": t1,
            "kind": "tts",
            "state": "succeeded",
            "payload_json": tts_payload,
            "result_json": "{}",
            "revision": 1,
            "created_at": t1,
            "succeeded_at": t2,
            "updated_at": t2,
        })
        _insert(connection, "ttsserveevidence", {
            "session_id": session_id,
            "command_id": 1,
            "source": "autopilot_command",
            "engine_version": "legacy-tts-v1",
            "cache_hit": 1,
            "result": "served",
            "byte_count": 2048,
            "text_sha256": hashlib.sha256(
                LEGACY_ITEM["initial_prompt"].encode("utf-8")).hexdigest(),
            "is_simulation": 1,
            "created_at": t1,
        })
        _insert(connection, "runtimecommandack", {
            "id": 1,
            "command_id": 1,
            "idempotency_key": "ack-legacy-tts-ended-0001",
            "session_id": session_id,
            "ack_type": "tts_ended",
            "command_revision": 0,
            "control_generation": 1,
            "runner_generation": 1,
            "device_event_seq": 1,
            "device_id_hash": device_id_hash,
            "capability_token_hash": capability_token_hash,
            "payload_json": autopilot_ledger.encode_ack_payload(
                "tts_ended", {"media_ended": True, "media_duration_ms": 800}),
            "received_at": t2,
        })
        _insert(connection, "audioassetrow", {
            "raw_audio_id": raw_audio_id,
            "session_id": session_id,
            "audio_format": "webm",
            "status": "recorded",
            "is_reliability_sample": 0,
            "withdrawn": 0,
            "checksum": checksum,
            "contains_direct_identifier": 0,
            "delete_gate_passed": 0,
            "turn_key": LEGACY_TURN_KEY,
            "is_simulation": 1,
            "data_classification": "simulation",
            "byte_count": byte_count,
            "uploaded_at": t3,
            "patient_turn_ref_version": 2,
        })
        _insert(connection, "runtimecommand", {
            "id": 2,
            "idempotency_key": "cmd-legacy-record-0001",
            "session_id": session_id,
            "command_seq": 2,
            "item_id": LEGACY_ITEM_ID,
            "turn_seq": LEGACY_TURN_SEQ,
            "turn_key": LEGACY_TURN_KEY,
            "attempt_seq": 1,
            "prompt_level": 0,
            "item_bank_version_id": BANK.version_id,
            "item_bank_definition_digest": bank_digest,
            "autopilot_protocol_version_id": protocol_version_id,
            "autopilot_protocol_definition_digest": protocol_digest,
            "response_role": LEGACY_RESPONSE_ROLE,
            "scope_key": "p0a_sim_first_single_v1",
            "control_generation": 1,
            "runner_generation": 1,
            "issued_capability_token_hash": capability_token_hash,
            "issued_device_id_hash": device_id_hash,
            "issued_at": t2,
            "kind": "record",
            "state": "succeeded",
            "predecessor_command_id": 1,
            "trigger_ack_idempotency_key": "ack-legacy-tts-ended-0001",
            "expected_raw_audio_id": raw_audio_id,
            "payload_json": record_payload,
            "result_json": "{}",
            "revision": 1,
            "created_at": t2,
            "succeeded_at": t5,
            "updated_at": t5,
        })
        _insert(connection, "audiocapturereceipt", {
            "server_seq": 1,
            "raw_audio_id": raw_audio_id,
            "session_id": session_id,
            "turn_key": LEGACY_TURN_KEY,
            "received_at": t4,
            "duration_seconds": duration_seconds,
            "byte_count": byte_count,
            "checksum": checksum,
            "data_classification": "simulation",
            "is_simulation": 1,
            "contains_direct_identifier": 0,
        })
        _insert(connection, "runtimecommandack", {
            "id": 2,
            "command_id": 2,
            "idempotency_key": "ack-legacy-record-stopped-0001",
            "session_id": session_id,
            "ack_type": "record_stopped",
            "command_revision": 0,
            "control_generation": 1,
            "runner_generation": 1,
            "device_event_seq": 2,
            "device_id_hash": device_id_hash,
            "capability_token_hash": capability_token_hash,
            "payload_json": autopilot_ledger.encode_ack_payload(
                "record_stopped", {"stop_reason": "silence"}),
            "receipt_server_seq": 1,
            "raw_audio_id": raw_audio_id,
            "checksum": checksum,
            "byte_count": byte_count,
            "duration_seconds": duration_seconds,
            "received_at": t5,
        })
        _insert(connection, "attemptcaptureprocessing", {
            "id": 1,
            "record_command_id": 2,
            "predecessor_command_id": 1,
            "receipt_server_seq": 1,
            "raw_audio_id": raw_audio_id,
            "session_id": session_id,
            "item_id": LEGACY_ITEM_ID,
            "turn_seq": LEGACY_TURN_SEQ,
            "proof_attempt_seq": 1,
            "proof_prompt_level": 0,
            "processing_status": "received",
            "processing_generation": 0,
            "created_at": t6,
            "is_simulation": 1,
        })
        _insert(connection, "sessionautopilotstate", {
            "session_id": session_id,
            "scope_key": "p0a_sim_first_single_v1",
            "mode": "autonomous",
            "status": "processing_attempt",
            "control_generation": 1,
            "runner_generation": 1,
            "revision": 3,
            "next_command_seq": 3,
            "current_command_id": 2,
            "created_at": t1,
            "updated_at": t6,
        })
        _insert(connection, "autopilotcontrolevent", {
            "id": 1,
            "idempotency_key": "start-legacy-0001",
            "session_id": session_id,
            "event_seq": 1,
            "event_type": "start",
            "scope_key": "p0a_sim_first_single_v1",
            "control_generation": 1,
            "runner_generation": 1,
            "command_id": 1,
            "actor_type": "researcher",
            "actor_id": "ACTOR-LEGACY",
            "from_mode": "disabled",
            "to_mode": "autonomous",
            "from_status": "idle",
            "to_status": "waiting_tts",
            "payload_json": json.dumps({"source": "p0a_domain_service"}),
            "created_at": t1,
        })
        connection.commit()
        assert list(connection.execute("PRAGMA foreign_key_check")) == []
    finally:
        connection.close()

    return LegacyChain(
        patient_id=patient_id,
        plan_id=plan_id,
        session_id=session_id,
        source_command_id=1,
        record_command_id=2,
        capture_id=1,
        raw_audio_id=raw_audio_id,
        receipt_server_seq=1,
        device_token=LEGACY_DEVICE_TOKEN,
        device_id_hash=device_id_hash,
        capability_token_hash=capability_token_hash,
        item_id=LEGACY_ITEM_ID,
        turn_seq=LEGACY_TURN_SEQ,
        turn_key=LEGACY_TURN_KEY,
        checksum=checksum,
        byte_count=byte_count,
        duration_seconds=duration_seconds,
        audio_bytes=audio_bytes,
        item_bank_version_id=BANK.version_id,
        item_bank_definition_digest=bank_digest,
        autopilot_protocol_version_id=protocol_version_id,
        autopilot_protocol_definition_digest=protocol_digest,
    )


_REPEAT_BINDING_COLUMNS = (
    "repeat_protocol_version_id", "repeat_protocol_definition_digest")
_CAPTURE_REPEAT_COLUMNS = _REPEAT_BINDING_COLUMNS + (
    "repeat_request_id", "repeat_admission_semantics")


def _columns(connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _table_sql(connection, table: str) -> str:
    return connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()[0]


def _snapshot(connection) -> dict:
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'")]
    return {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in sorted(tables)
    }


def test_d3_populated_c7_upgrade_marks_only_existing_capture_legacy_and_preserves_chain(
        tmp_path):
    """A real pre-protocol chain survives d3 with exactly one new fact: the marker."""
    db_path = tmp_path / "populated-c7.sqlite"
    config = _config(db_path)
    command.upgrade(config, PARENT)

    connection = sqlite3.connect(db_path)
    try:
        for table in ("visitplan", "session", "runtimecommand"):
            assert not (_columns(connection, table) & set(_REPEAT_BINDING_COLUMNS)), (
                f"{table} already carries repeat binding columns at {PARENT}")
        assert not (_columns(connection, "attemptcaptureprocessing")
                    & set(_CAPTURE_REPEAT_COLUMNS))
        assert "autopilotrepeatrequest" not in {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()

    chain = _insert_c7_populated_capture_chain(db_path)

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        assert list(connection.execute("PRAGMA foreign_key_check")) == []
        before = _snapshot(connection)
        before_pointers = connection.execute(
            "SELECT record_command_id, predecessor_command_id, "
            "receipt_server_seq, raw_audio_id, session_id, item_id, turn_seq, "
            "proof_attempt_seq, proof_prompt_level, processing_status, "
            "processing_generation, disposition, final_attempt_id, is_simulation "
            "FROM attemptcaptureprocessing").fetchall()
        before_commands = connection.execute(
            "SELECT id, command_seq, kind, state, predecessor_command_id, "
            "expected_raw_audio_id, trigger_ack_idempotency_key, payload_json, "
            "revision FROM runtimecommand ORDER BY id").fetchall()
        before_state = connection.execute(
            "SELECT status, current_command_id, control_generation, "
            "runner_generation, revision, next_command_seq "
            "FROM sessionautopilotstate").fetchall()
        before_tts_evidence = connection.execute(
            "SELECT session_id, command_id, source, engine_version, cache_hit, "
            "result, byte_count, text_sha256, is_simulation "
            "FROM ttsserveevidence").fetchall()
    finally:
        connection.close()

    command.upgrade(config, REPEAT_REVISION)

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        assert list(connection.execute("PRAGMA foreign_key_check")) == []

        marker = [row for row in connection.execute(
            "PRAGMA table_info(attemptcaptureprocessing)")
            if row[1] == "repeat_admission_semantics"]
        assert len(marker) == 1
        assert marker[0][3] == 1, "marker column must be NOT NULL after d3"
        assert marker[0][4] is None, "marker column must have no database default"

        capture_sql = _table_sql(connection, "attemptcaptureprocessing")
        assert "ck_capture_processing_admission_semantics" in capture_sql
        assert "ck_capture_processing_admission_marker_binding" in capture_sql

        # The migration's one and only write: the pre-existing capture is marked
        # legacy, and nothing about it is otherwise re-interpreted.
        assert connection.execute(
            "SELECT id, repeat_admission_semantics, repeat_protocol_version_id, "
            "repeat_protocol_definition_digest, repeat_request_id, disposition "
            "FROM attemptcaptureprocessing").fetchall() == [
                (chain.capture_id, "legacy_pre_repeat", None, None, None, None)]
        assert connection.execute(
            "SELECT COUNT(*) FROM attemptcaptureprocessing "
            "WHERE repeat_admission_semantics != 'legacy_pre_repeat'"
        ).fetchone()[0] == 0

        for table, key in (("visitplan", "plan_id"), ("session", "session_id")):
            assert connection.execute(
                f"SELECT repeat_protocol_version_id, "
                f"repeat_protocol_definition_digest FROM {table}").fetchall() == [
                    (None, None)], f"{table} must keep an empty repeat binding"
        assert connection.execute(
            "SELECT id, repeat_protocol_version_id, "
            "repeat_protocol_definition_digest, replay_source_command_id, "
            "replay_ordinal, replay_source_payload_sha256 "
            "FROM runtimecommand ORDER BY id").fetchall() == [
                (chain.source_command_id, None, None, None, None, None),
                (chain.record_command_id, None, None, None, None, None)]

        after = _snapshot(connection)
        assert after == {**before, "autopilotrepeatrequest": 0}
        assert connection.execute(
            "SELECT record_command_id, predecessor_command_id, "
            "receipt_server_seq, raw_audio_id, session_id, item_id, turn_seq, "
            "proof_attempt_seq, proof_prompt_level, processing_status, "
            "processing_generation, disposition, final_attempt_id, is_simulation "
            "FROM attemptcaptureprocessing").fetchall() == before_pointers
        assert connection.execute(
            "SELECT id, command_seq, kind, state, predecessor_command_id, "
            "expected_raw_audio_id, trigger_ack_idempotency_key, payload_json, "
            "revision FROM runtimecommand ORDER BY id").fetchall() == before_commands
        assert connection.execute(
            "SELECT status, current_command_id, control_generation, "
            "runner_generation, revision, next_command_seq "
            "FROM sessionautopilotstate").fetchall() == before_state
        assert connection.execute(
            "SELECT session_id, command_id, source, engine_version, cache_hit, "
            "result, byte_count, text_sha256, is_simulation "
            "FROM ttsserveevidence").fetchall() == before_tts_evidence
    finally:
        connection.close()


def test_active_protocol_is_exactly_the_approved_version_and_digest():
    protocol = repeat_intent.active_protocol()

    assert protocol.version_id == APPROVED_VERSION_ID
    assert protocol.definition_digest == APPROVED_DIGEST
    assert protocol.max_replay_count_per_logical_slot == 1
    assert protocol.second_request_reason_code == "explicit_repeat_limit"
    assert protocol.simulation_only is True
    assert protocol.phrase_by_normalized_text == APPROVED_PHRASES


def test_normalization_runs_the_frozen_step_order_with_the_frozen_boundary_set():
    protocol = repeat_intent.active_protocol()

    assert protocol.steps == (
        "unicode_nfkc",
        "strip_unicode_whitespace",
        "strip_boundary_chars_repeatedly",
        "strip_unicode_whitespace",
    )
    assert protocol.boundary_chars == "，。！？、,.!?"
    # The second whitespace trim is load-bearing: punctuation removal can expose
    # whitespace that the first trim could not see.
    assert repeat_intent.normalize(protocol, " 再说一遍 。 ") == "再说一遍"
    assert repeat_intent.normalize(protocol, "。。，再说一遍！！") == "再说一遍"
    # Inner characters are folded by NFKC but never removed: the full-width
    # comma becomes ASCII and stays in place, so the string can still only match
    # a frozen phrase as a whole.
    assert repeat_intent.normalize(
        protocol, "没听清，不过我觉得是苹果") == "没听清,不过我觉得是苹果"
    assert len(repeat_intent.normalize(
        protocol, "没听清，不过我觉得是苹果")) == len("没听清，不过我觉得是苹果")


def test_normalized_text_hash_is_the_utf8_sha256_of_the_final_string():
    import hashlib

    protocol = repeat_intent.active_protocol()
    match = repeat_intent.detect(protocol, "  我没听清!!  ")

    assert match is not None
    assert match.phrase_key == "did_not_hear_clearly_self"
    assert match.normalized_text_sha256 == hashlib.sha256(
        "我没听清".encode("utf-8")).hexdigest()
    assert match.protocol_version_id == APPROVED_VERSION_ID
    assert match.protocol_definition_digest == APPROVED_DIGEST


@pytest.mark.parametrize("phrase,phrase_key", sorted(APPROVED_PHRASES.items()))
def test_every_approved_phrase_hits_with_allowed_whitespace_and_punctuation(
        phrase, phrase_key):
    protocol = repeat_intent.active_protocol()

    for candidate in (phrase, f" {phrase} ", f"{phrase}。", f"，{phrase}！",
                      f"　{phrase}　"):
        match = repeat_intent.detect(protocol, candidate)
        assert match is not None, candidate
        assert match.phrase_key == phrase_key


def test_nfkc_variants_hit_the_same_frozen_phrase():
    protocol = repeat_intent.active_protocol()

    # Full-width exclamation normalizes into the boundary set; the ideographic
    # space normalizes into ordinary whitespace.
    assert repeat_intent.detect(protocol, "我没听见！").phrase_key == (
        "did_not_hear_self")
    assert repeat_intent.detect(protocol, "！！我没听见？").phrase_key == (
        "did_not_hear_self")


@pytest.mark.parametrize("text_value", [
    None,
    "",
    "   ",
    "。。。",
    "没听清，不过我觉得是苹果",
    "请再说一遍题目后我回答苹果",
    "再说一遍吧",
    "没听清清",
    "没听",
    "我 没 听 清",
    123,
])
def test_non_repeat_transcripts_never_match(text_value):
    protocol = repeat_intent.active_protocol()

    assert repeat_intent.detect(protocol, text_value) is None


def test_registry_resolves_historical_bindings_and_fails_closed_on_drift(tmp_path):
    registry = repeat_intent.load_registry()
    assert set(registry) == {APPROVED_VERSION_ID}

    resolved = repeat_intent.protocol_for_binding(
        APPROVED_VERSION_ID, APPROVED_DIGEST)
    assert resolved.definition_digest == APPROVED_DIGEST

    with pytest.raises(content.FrozenContentUnavailable):
        repeat_intent.protocol_for_binding("repeat-intent-unknown", APPROVED_DIGEST)
    with pytest.raises(content.FrozenContentUnavailable):
        repeat_intent.protocol_for_binding(APPROVED_VERSION_ID, "0" * 64)
    with pytest.raises(content.FrozenContentUnavailable):
        repeat_intent.protocol_for_binding(APPROVED_VERSION_ID, "not-a-digest")
    with pytest.raises(content.FrozenContentUnavailable):
        repeat_intent.protocol_for_binding(None, None)


def test_registry_keeps_both_versions_loadable_so_recovery_can_pick_the_old_one(
        tmp_path):
    """A deployment upgrade must not rewrite the definition an old row used."""
    directory = tmp_path / "repeat_intent_protocols"
    directory.mkdir()
    source = (repeat_intent.REPEAT_INTENT_DIR
              / "repeat_intent_protocol_v1_20260730.json")
    original = json.loads(source.read_text(encoding="utf-8"))
    (directory / "v1.json").write_bytes(source.read_bytes())

    upgraded = dict(original)
    upgraded["protocol_version_id"] = "repeat-intent-v2-test"
    upgraded["phrases"] = original["phrases"] + [
        {"phrase_key": "say_again_once_more", "text": "麻烦再说一次"},
    ]
    (directory / "v2.json").write_bytes(
        json.dumps(upgraded, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8") + b"\n")

    registry = repeat_intent.load_registry(directory)
    assert set(registry) == {APPROVED_VERSION_ID, "repeat-intent-v2-test"}

    old = repeat_intent.protocol_for_binding(
        APPROVED_VERSION_ID, APPROVED_DIGEST, directory=directory)
    new = repeat_intent.protocol_for_binding(
        "repeat-intent-v2-test",
        repeat_intent.definition_digest(upgraded),
        directory=directory,
    )
    # A row frozen against v1 keeps v1 semantics even though v2 is deployed.
    assert repeat_intent.detect(old, "麻烦再说一次") is None
    assert repeat_intent.detect(new, "麻烦再说一次").phrase_key == (
        "say_again_once_more")
    assert repeat_intent.detect(old, "再说一遍").phrase_key == "repeat_again"


def test_repeat_migration_is_single_head_and_roundtrips_on_an_empty_database(
        tmp_path):
    db_path = tmp_path / "repeat-intent.sqlite"
    config = _config(db_path)
    assert ScriptDirectory.from_config(config).get_heads() == [CURRENT_HEAD]

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "autopilotrepeatrequest" in inspector.get_table_names()
    capture_columns = {
        row["name"] for row in inspector.get_columns("attemptcaptureprocessing")}
    assert {
        "repeat_protocol_version_id",
        "repeat_protocol_definition_digest",
        "repeat_request_id",
    }.issubset(capture_columns)
    command_columns = {
        row["name"] for row in inspector.get_columns("runtimecommand")}
    assert {
        "repeat_protocol_version_id",
        "repeat_protocol_definition_digest",
        "replay_source_command_id",
        "replay_ordinal",
        "replay_source_payload_sha256",
    }.issubset(command_columns)
    for table in ("visitplan", "session"):
        columns = {row["name"] for row in inspector.get_columns(table)}
        assert {
            "repeat_protocol_version_id",
            "repeat_protocol_definition_digest",
        }.issubset(columns)

    command.downgrade(config, PARENT)
    inspector = inspect(engine)
    assert "autopilotrepeatrequest" not in inspector.get_table_names()
    assert "repeat_request_id" not in {
        row["name"] for row in inspector.get_columns("attemptcaptureprocessing")}

    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version"
        )).scalar_one() == CURRENT_HEAD


def _head_db_with_legacy_chain(tmp_path, name: str):
    """A head database whose only capture really came through the c7 upgrade."""
    db_path = tmp_path / name
    config = _config(db_path)
    command.upgrade(config, PARENT)
    chain = _insert_c7_populated_capture_chain(db_path)
    command.upgrade(config, CURRENT_HEAD)
    return db_path, chain


def test_downgrade_refuses_while_any_repeat_evidence_still_exists(tmp_path):
    """Repeat evidence has no Attempt; downgrading could only lie about it."""
    db_path = tmp_path / "repeat-evidence.sqlite"
    config = _config(db_path)
    command.upgrade(config, "head")

    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO autopilotrepeatrequest ("
            "capture_processing_id, session_id, item_id, turn_seq, attempt_seq,"
            " prompt_level, repeat_ordinal, outcome, record_command_id,"
            " raw_audio_id, source_tts_command_id, source_payload_sha256,"
            " replay_command_id, pause_control_event_seq,"
            " repeat_protocol_version_id, repeat_protocol_definition_digest,"
            " phrase_key, normalized_text_sha256, created_at, is_simulation) "
            "VALUES (1, 'S-DOWNGRADE', 'SE_1', 1, 1, 0, 1, 'replayed', 2,"
            " 'raw-1', 3, ?, 4, NULL, ?, ?, 'repeat_again', ?,"
            " '2026-07-30 00:00:00', 1)",
            ("a" * 64, APPROVED_VERSION_ID, APPROVED_DIGEST, "b" * 64),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="explicit repeat"):
        command.downgrade(config, PARENT)

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version"
        )).scalar_one() == REPEAT_REVISION
        assert connection.execute(text(
            "SELECT COUNT(*) FROM autopilotrepeatrequest")).scalar_one() == 1


def test_active_protocol_refuses_an_edited_definition_that_keeps_the_version_id(
        tmp_path):
    """同一 version id 下内容被改写 → 不是"新版本"，是未批准内容，必须拒绝启用。"""
    directory = tmp_path / "repeat_intent_protocols"
    directory.mkdir()
    source = (repeat_intent.REPEAT_INTENT_DIR
              / "repeat_intent_protocol_v1_20260730.json")
    tampered = json.loads(source.read_text(encoding="utf-8"))
    tampered["phrases"] = tampered["phrases"] + [
        {"phrase_key": "unapproved_phrase", "text": "换一个"},
    ]
    assert tampered["protocol_version_id"] == APPROVED_VERSION_ID
    (directory / "v1.json").write_bytes(
        json.dumps(tampered, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8") + b"\n")

    # The registry still loads it (history must stay resolvable) ...
    registry = repeat_intent.load_registry(directory)
    assert set(registry) == {APPROVED_VERSION_ID}
    # ... but it can never become the active definition a new plan freezes.
    with pytest.raises(content.FrozenContentUnavailable):
        repeat_intent.active_protocol(directory)
    # And resolving it under the approved digest fails closed too.
    with pytest.raises(content.FrozenContentUnavailable):
        repeat_intent.protocol_for_binding(
            APPROVED_VERSION_ID, APPROVED_DIGEST, directory=directory)


def test_active_identity_is_pinned_by_both_halves():
    assert repeat_intent.ACTIVE_REPEAT_INTENT_VERSION_ID == APPROVED_VERSION_ID
    assert repeat_intent.ACTIVE_REPEAT_INTENT_DEFINITION_DIGEST == APPROVED_DIGEST


def test_model_and_migration_state_the_same_constraint_text():
    """模型与迁移的 CHECK 文本必须逐字一致，否则两边约束会悄悄分叉。"""
    import importlib.util

    from app import models

    spec = importlib.util.spec_from_file_location(
        "repeat_migration",
        "alembic/versions/d3f8b5c1a704_explicit_repeat_intent_ledger.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert (migration._REPEAT_BINDING_CHECK
            == models.REPEAT_PROTOCOL_BINDING_CHECK)
    assert migration._hex64_sql("x") == models._hex64_sql("x")


_LEDGER_COLUMNS = {
    "capture_processing_id": 1,
    "session_id": "S-CK",
    "item_id": "SE_1",
    "turn_seq": 1,
    "attempt_seq": 1,
    "prompt_level": 0,
    "repeat_ordinal": 1,
    "outcome": "replayed",
    "record_command_id": 2,
    "raw_audio_id": "raw-ck",
    "source_tts_command_id": 3,
    "source_payload_sha256": "a" * 64,
    "replay_command_id": 4,
    "pause_control_event_seq": None,
    "repeat_protocol_version_id": APPROVED_VERSION_ID,
    "repeat_protocol_definition_digest": APPROVED_DIGEST,
    "phrase_key": "repeat_again",
    "normalized_text_sha256": "b" * 64,
    "created_at": "2026-07-30 00:00:00",
    "is_simulation": 1,
}


def _insert_ledger(connection, **overrides):
    row = {**_LEDGER_COLUMNS, **overrides}
    columns = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    connection.execute(
        f"INSERT INTO autopilotrepeatrequest ({columns}) VALUES ({placeholders})",
        tuple(row.values()))


@pytest.mark.parametrize("overrides", [
    {"repeat_ordinal": 3},
    {"outcome": "something_else"},
    # ordinal 1 must be a replay with a command pointer and no pause pointer
    {"replay_command_id": None},
    {"pause_control_event_seq": 7},
    # ordinal 2 must be a pause with a pause pointer and no replay pointer
    {"repeat_ordinal": 2, "outcome": "limit_paused", "replay_command_id": 4,
     "pause_control_event_seq": None},
    {"repeat_ordinal": 2, "outcome": "limit_paused", "replay_command_id": None,
     "pause_control_event_seq": None},
    {"repeat_ordinal": 2, "outcome": "replayed"},
    # digests must really be 64 lowercase hex, not merely 64 lowercase chars
    {"source_payload_sha256": "g" * 64},
    {"normalized_text_sha256": "G" * 64},
    {"normalized_text_sha256": "b" * 63},
    {"repeat_protocol_definition_digest": "z" * 64},
    {"repeat_protocol_version_id": "   "},
    {"phrase_key": " "},
    {"turn_seq": 0},
    {"attempt_seq": 0},
    {"prompt_level": 4},
])
def test_repeat_ledger_rejects_every_inconsistent_shape(tmp_path, overrides):
    """所有必填列都给全，确保失败真的来自目标 CHECK，而不是别的 NOT NULL。"""
    db_path = tmp_path / "ledger-check.sqlite"
    command.upgrade(_config(db_path), "head")
    connection = sqlite3.connect(db_path)
    try:
        _insert_ledger(connection)          # the valid baseline really inserts
        connection.execute("DELETE FROM autopilotrepeatrequest")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_ledger(connection, **overrides)
    finally:
        connection.close()


def _baseline_rows(chain: LegacyChain) -> dict[str, dict]:
    """One fully legal row per table: every NOT NULL column, FK and other CHECK.

    A negative case is then produced by breaking exactly one repeat/marker
    field.  Without this, an insert naming only the field under test fails on an
    unrelated NOT NULL column and the CHECK is never reached — a false green.
    """
    base = datetime(2026, 7, 30, 12, 0, 0)
    return {
        "visitplan": {
            "plan_id": "VP-BASELINE",
            "protocol_slot_key": f"{chain.patient_id}#2#正式训练#2",
            "patient_id": chain.patient_id,
            "scheduled_date": "2026-07-30",
            "session_sitting_no": 2,
            "week_no": 2,
            "phase_type": "正式训练",
            "event_line": "正式训练",
            "item_bank_version_id": chain.item_bank_version_id,
            "item_bank_definition_digest": chain.item_bank_definition_digest,
            "autopilot_protocol_version_id": (
                chain.autopilot_protocol_version_id),
            "autopilot_protocol_definition_digest": (
                chain.autopilot_protocol_definition_digest),
            "is_simulation": 1,
            "data_classification": "simulation",
            "status": "draft",
            "revision": 1,
            "created_by": "ACTOR-LEGACY",
            "created_at": base,
            "updated_at": base,
            "repeat_protocol_version_id": APPROVED_VERSION_ID,
            "repeat_protocol_definition_digest": APPROVED_DIGEST,
        },
        "session": {
            "session_id": "S-BASELINE",
            "patient_id": chain.patient_id,
            "session_sitting_no": 2,
            "week_no": 2,
            "phase_type": "正式训练",
            "event_line": "正式训练",
            "trainer_id": "ACTOR-LEGACY",
            "item_bank_version_id": chain.item_bank_version_id,
            "item_bank_definition_digest": chain.item_bank_definition_digest,
            "autopilot_protocol_version_id": (
                chain.autopilot_protocol_version_id),
            "autopilot_protocol_definition_digest": (
                chain.autopilot_protocol_definition_digest),
            "is_simulation": 1,
            "data_classification": "simulation",
            "repeat_protocol_version_id": APPROVED_VERSION_ID,
            "repeat_protocol_definition_digest": APPROVED_DIGEST,
        },
        "runtimecommand": {
            "id": 3,
            "idempotency_key": "cmd-baseline-0003",
            "session_id": chain.session_id,
            "command_seq": 3,
            "item_id": chain.item_id,
            "turn_seq": chain.turn_seq,
            "turn_key": chain.turn_key,
            "attempt_seq": 1,
            "prompt_level": 0,
            "item_bank_version_id": chain.item_bank_version_id,
            "item_bank_definition_digest": chain.item_bank_definition_digest,
            "autopilot_protocol_version_id": (
                chain.autopilot_protocol_version_id),
            "autopilot_protocol_definition_digest": (
                chain.autopilot_protocol_definition_digest),
            "response_role": LEGACY_RESPONSE_ROLE,
            "scope_key": "p0a_sim_first_single_v1",
            "control_generation": 1,
            "runner_generation": 1,
            "issued_capability_token_hash": chain.capability_token_hash,
            "issued_device_id_hash": chain.device_id_hash,
            "issued_at": base,
            "kind": "tts",
            "state": "pending",
            "payload_json": "{}",
            "result_json": "{}",
            "revision": 0,
            "created_at": base,
            "updated_at": base,
            "repeat_protocol_version_id": APPROVED_VERSION_ID,
            "repeat_protocol_definition_digest": APPROVED_DIGEST,
        },
        "attemptcaptureprocessing": {
            "id": 2,
            "record_command_id": 5,
            "predecessor_command_id": 4,
            "receipt_server_seq": 2,
            "raw_audio_id": "raw-baseline-000002",
            "session_id": chain.session_id,
            "item_id": chain.item_id,
            "turn_seq": chain.turn_seq,
            "proof_attempt_seq": 2,
            "proof_prompt_level": 1,
            "processing_status": "received",
            "processing_generation": 0,
            "created_at": base,
            "is_simulation": 1,
            "repeat_admission_semantics": "repeat_bound",
            "repeat_protocol_version_id": APPROVED_VERSION_ID,
            "repeat_protocol_definition_digest": APPROVED_DIGEST,
        },
    }


def _add_capture_baseline_parents(connection, chain: LegacyChain) -> None:
    """The extra audio/receipt/command rows a second capture row needs."""
    base = datetime(2026, 7, 30, 12, 0, 0)
    _insert(connection, "audioassetrow", {
        "raw_audio_id": "raw-baseline-000002",
        "session_id": chain.session_id,
        "audio_format": "webm",
        "status": "recorded",
        "is_reliability_sample": 0,
        "withdrawn": 0,
        "checksum": "c" * 64,
        "contains_direct_identifier": 0,
        "delete_gate_passed": 0,
        "turn_key": chain.turn_key,
        "is_simulation": 1,
        "data_classification": "simulation",
        "byte_count": 32,
        "uploaded_at": base,
        "patient_turn_ref_version": 2,
    })
    _insert(connection, "audiocapturereceipt", {
        "server_seq": 2,
        "raw_audio_id": "raw-baseline-000002",
        "session_id": chain.session_id,
        "turn_key": chain.turn_key,
        "received_at": base,
        "duration_seconds": 1.0,
        "byte_count": 32,
        "checksum": "c" * 64,
        "data_classification": "simulation",
        "is_simulation": 1,
        "contains_direct_identifier": 0,
    })
    common = {
        "session_id": chain.session_id,
        "item_id": chain.item_id,
        "turn_seq": chain.turn_seq,
        "turn_key": chain.turn_key,
        "attempt_seq": 2,
        "prompt_level": 1,
        "scope_key": "p0a_sim_first_single_v1",
        "control_generation": 1,
        "runner_generation": 1,
        "issued_capability_token_hash": chain.capability_token_hash,
        "issued_device_id_hash": chain.device_id_hash,
        "issued_at": base,
        "state": "succeeded",
        "succeeded_at": base,
        "payload_json": "{}",
        "result_json": "{}",
        "revision": 1,
        "created_at": base,
        "updated_at": base,
    }
    _insert(connection, "runtimecommand", {
        "id": 4, "idempotency_key": "cmd-baseline-cue-0004", "command_seq": 4,
        "kind": "tts", **common})
    _insert(connection, "runtimecommand", {
        "id": 5, "idempotency_key": "cmd-baseline-record-0005", "command_seq": 5,
        "kind": "record", "predecessor_command_id": 4,
        "trigger_ack_idempotency_key": "ack-baseline-0004",
        "expected_raw_audio_id": "raw-baseline-000002", **common})


BINDING_PAIR_NEGATIVES = [
    pytest.param({"repeat_protocol_definition_digest": None},
                 id="version-only"),
    pytest.param({"repeat_protocol_version_id": None}, id="digest-only"),
    pytest.param({"repeat_protocol_definition_digest": "g" * 64},
                 id="64-non-hex-digest"),
    pytest.param({"repeat_protocol_definition_digest": APPROVED_DIGEST.upper()},
                 id="uppercase-hex-digest"),
    pytest.param({"repeat_protocol_definition_digest": APPROVED_DIGEST[:63]},
                 id="short-digest"),
    pytest.param({"repeat_protocol_version_id": "   "}, id="blank-version"),
]


@pytest.mark.parametrize("override", BINDING_PAIR_NEGATIVES)
@pytest.mark.parametrize("table,constraint", [
    ("visitplan", "ck_visit_plan_repeat_binding_complete"),
    ("session", "ck_session_repeat_binding_complete"),
    ("runtimecommand", "ck_runtime_command_repeat_binding_complete"),
])
def test_repeat_binding_pair_check_rejects_half_and_non_hex(
        tmp_path, table, constraint, override):
    """完整合法基线先证明可插，再只破坏一个绑定列，异常必须点名目标 CHECK。"""
    db_path, chain = _head_db_with_legacy_chain(
        tmp_path, f"binding-{table}.sqlite")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        baseline = _baseline_rows(chain)[table]
        _insert(connection, table, baseline)      # the baseline really inserts
        connection.execute(f"DELETE FROM {table} WHERE rowid = last_insert_rowid()")
        with pytest.raises(sqlite3.IntegrityError) as excinfo:
            _insert(connection, table, {**baseline, **override})
        assert str(excinfo.value) == f"CHECK constraint failed: {constraint}"
    finally:
        connection.close()


# On attemptcaptureprocessing the marker CHECK's ``repeat_bound`` branch repeats
# the pair CHECK's second branch verbatim, so it *subsumes* it: no half-filled
# or malformed pair can violate one without violating the other, and there is no
# legal row shape that isolates the pair CHECK on this table.  Claiming a single
# target constraint here would be false, so the acceptance is stated as the
# exact two-name set, both names are proved to exist in the schema, and the
# marker CHECK is separately isolated by a shape the pair CHECK accepts.
CAPTURE_BINDING_CONSTRAINTS = frozenset({
    "ck_capture_processing_repeat_binding_complete",
    "ck_capture_processing_admission_marker_binding",
})


@pytest.mark.parametrize("override", BINDING_PAIR_NEGATIVES)
def test_capture_repeat_binding_pair_is_rejected_by_both_named_checks(
        tmp_path, override):
    db_path, chain = _head_db_with_legacy_chain(
        tmp_path, "binding-capture.sqlite")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _add_capture_baseline_parents(connection, chain)
        baseline = _baseline_rows(chain)["attemptcaptureprocessing"]
        _insert(connection, "attemptcaptureprocessing", baseline)
        connection.execute(
            "DELETE FROM attemptcaptureprocessing WHERE id = ?", (baseline["id"],))
        table_sql = _table_sql(connection, "attemptcaptureprocessing")
        for name in CAPTURE_BINDING_CONSTRAINTS:
            assert name in table_sql
        with pytest.raises(sqlite3.IntegrityError) as excinfo:
            _insert(connection, "attemptcaptureprocessing",
                    {**baseline, **override})
        assert str(excinfo.value) in {
            f"CHECK constraint failed: {name}"
            for name in CAPTURE_BINDING_CONSTRAINTS}
    finally:
        connection.close()


_SEMANTICS = "ck_capture_processing_admission_semantics"
_MARKER_BINDING = "ck_capture_processing_admission_marker_binding"
_PAIR = "ck_capture_processing_repeat_binding_complete"

CAPTURE_MARKER_NEGATIVES = [
    # NOT NULL, and the column has no database default to fall back on.
    pytest.param({"repeat_admission_semantics": "__omit__"},
                 frozenset({"NOT NULL constraint failed: "
                            "attemptcaptureprocessing.repeat_admission_semantics"}),
                 id="marker-omitted"),
    pytest.param({"repeat_admission_semantics": None},
                 frozenset({"NOT NULL constraint failed: "
                            "attemptcaptureprocessing.repeat_admission_semantics"}),
                 id="marker-null"),
    # An unknown marker matches neither branch of the marker CHECK either, so
    # both named CHECKs are genuinely violated; the closed-set CHECK is isolated
    # separately below by keeping the binding shape legal for its own branch.
    pytest.param({"repeat_admission_semantics": "repeat_unbound"},
                 frozenset({f"CHECK constraint failed: {_SEMANTICS}",
                            f"CHECK constraint failed: {_MARKER_BINDING}"}),
                 id="marker-not-in-closed-set"),
    pytest.param({"repeat_admission_semantics": ""},
                 frozenset({f"CHECK constraint failed: {_SEMANTICS}",
                            f"CHECK constraint failed: {_MARKER_BINDING}"}),
                 id="marker-empty-string"),
    # Legacy rows with a complete, well-formed binding satisfy the pair CHECK,
    # so only the marker CHECK can reject them.
    pytest.param({"repeat_admission_semantics": "legacy_pre_repeat"},
                 frozenset({f"CHECK constraint failed: {_MARKER_BINDING}"}),
                 id="legacy-with-both-bindings"),
    pytest.param({"repeat_admission_semantics": "legacy_pre_repeat",
                  "repeat_protocol_definition_digest": None},
                 frozenset({f"CHECK constraint failed: {_MARKER_BINDING}",
                            f"CHECK constraint failed: {_PAIR}"}),
                 id="legacy-with-version-only"),
    pytest.param({"repeat_admission_semantics": "legacy_pre_repeat",
                  "repeat_protocol_version_id": None},
                 frozenset({f"CHECK constraint failed: {_MARKER_BINDING}",
                            f"CHECK constraint failed: {_PAIR}"}),
                 id="legacy-with-digest-only"),
    # An empty pair is legal for the pair CHECK, so this isolates the marker
    # CHECK exactly: without it a bound capture could carry no binding at all.
    pytest.param({"repeat_protocol_version_id": None,
                  "repeat_protocol_definition_digest": None},
                 frozenset({f"CHECK constraint failed: {_MARKER_BINDING}"}),
                 id="bound-with-no-binding-at-all"),
]


@pytest.mark.parametrize("override,expected", CAPTURE_MARKER_NEGATIVES)
def test_capture_admission_marker_rejects_every_inconsistent_shape(
        tmp_path, override, expected):
    """标记本身、标记与绑定的组合，都必须由具名约束或 NOT NULL 精确拒绝。"""
    db_path, chain = _head_db_with_legacy_chain(
        tmp_path, "capture-marker.sqlite")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _add_capture_baseline_parents(connection, chain)
        baseline = _baseline_rows(chain)["attemptcaptureprocessing"]
        _insert(connection, "attemptcaptureprocessing", baseline)
        connection.execute(
            "DELETE FROM attemptcaptureprocessing WHERE id = ?", (baseline["id"],))
        broken = {key: value
                  for key, value in {**baseline, **override}.items()
                  if value != "__omit__"}
        with pytest.raises(sqlite3.IntegrityError) as excinfo:
            _insert(connection, "attemptcaptureprocessing", broken)
        assert str(excinfo.value) in expected
    finally:
        connection.close()


def test_every_capture_repeat_constraint_is_declared_by_name(tmp_path):
    """三条具名约束都必须真的存在，负例的"击中其一"才不是缺失的伪装。"""
    db_path, _chain = _head_db_with_legacy_chain(
        tmp_path, "capture-constraints.sqlite")
    connection = sqlite3.connect(db_path)
    try:
        table_sql = _table_sql(connection, "attemptcaptureprocessing")
        for name in (_SEMANTICS, _MARKER_BINDING, _PAIR):
            assert name in table_sql
    finally:
        connection.close()


def test_capture_admission_marker_accepts_a_complete_bound_row(tmp_path):
    """正例：完整 repeat_bound 行必须真的能插入，负例才有意义。"""
    db_path, chain = _head_db_with_legacy_chain(
        tmp_path, "capture-marker-positive.sqlite")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _add_capture_baseline_parents(connection, chain)
        _insert(connection, "attemptcaptureprocessing",
                _baseline_rows(chain)["attemptcaptureprocessing"])
        connection.commit()
        assert list(connection.execute("PRAGMA foreign_key_check")) == []
        assert connection.execute(
            "SELECT repeat_admission_semantics FROM attemptcaptureprocessing "
            "ORDER BY id").fetchall() == [
                ("legacy_pre_repeat",), ("repeat_bound",)]
    finally:
        connection.close()


REPLAY_PROVENANCE_NEGATIVES = [
    pytest.param({"replay_ordinal": None, "replay_source_payload_sha256": None},
                 id="source-only"),
    pytest.param({"replay_source_command_id": None,
                  "replay_source_payload_sha256": None}, id="ordinal-only"),
    pytest.param({"replay_source_command_id": None, "replay_ordinal": None},
                 id="digest-only"),
    pytest.param({"replay_source_payload_sha256": None}, id="source-and-ordinal"),
    pytest.param({"replay_source_command_id": None}, id="ordinal-and-digest"),
    pytest.param({"replay_ordinal": None}, id="source-and-digest"),
    pytest.param({"replay_ordinal": 2}, id="ordinal-above-the-single-replay"),
    pytest.param({"replay_ordinal": 0}, id="ordinal-zero"),
    pytest.param({"replay_source_payload_sha256": "g" * 64},
                 id="64-non-hex-payload-digest"),
    pytest.param({"replay_source_payload_sha256": "A" * 64},
                 id="uppercase-payload-digest"),
]


@pytest.mark.parametrize("override", REPLAY_PROVENANCE_NEGATIVES)
def test_replay_provenance_check_rejects_every_partial_shape(
        tmp_path, override):
    """重播溯源三元组的完整基线可插；只破坏一列必须撞上具名 CHECK。"""
    db_path, chain = _head_db_with_legacy_chain(
        tmp_path, "provenance.sqlite")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        baseline = {
            **_baseline_rows(chain)["runtimecommand"],
            "replay_source_command_id": chain.source_command_id,
            "replay_ordinal": 1,
            "replay_source_payload_sha256": "d" * 64,
        }
        _insert(connection, "runtimecommand", baseline)
        connection.execute("DELETE FROM runtimecommand WHERE id = ?",
                           (baseline["id"],))
        with pytest.raises(sqlite3.IntegrityError) as excinfo:
            _insert(connection, "runtimecommand", {**baseline, **override})
        assert "ck_runtime_command_replay_provenance_complete" in str(excinfo.value)
    finally:
        connection.close()


def test_record_command_may_never_carry_replay_provenance(tmp_path):
    """录音命令永远不是重播；这条独立 CHECK 也要有完整基线的负例。"""
    db_path, chain = _head_db_with_legacy_chain(
        tmp_path, "record-never-replays.sqlite")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _add_capture_baseline_parents(connection, chain)
        baseline = {
            **_baseline_rows(chain)["runtimecommand"],
            "id": 6,
            "idempotency_key": "cmd-baseline-record-0006",
            "command_seq": 6,
            "kind": "record",
            "predecessor_command_id": 3,
            "trigger_ack_idempotency_key": "ack-baseline-0006",
            "expected_raw_audio_id": "raw-baseline-000006",
        }
        _insert(connection, "audioassetrow", {
            "raw_audio_id": "raw-baseline-000006",
            "session_id": chain.session_id,
            "audio_format": "webm",
            "status": "recorded",
            "is_reliability_sample": 0,
            "withdrawn": 0,
            "contains_direct_identifier": 0,
            "delete_gate_passed": 0,
            "turn_key": chain.turn_key,
            "is_simulation": 1,
            "data_classification": "simulation",
            "patient_turn_ref_version": 2,
        })
        _insert(connection, "runtimecommand",
                _baseline_rows(chain)["runtimecommand"])
        _insert(connection, "runtimecommand", baseline)
        connection.execute("DELETE FROM runtimecommand WHERE id = 6")
        with pytest.raises(sqlite3.IntegrityError) as excinfo:
            _insert(connection, "runtimecommand", {
                **baseline,
                "replay_source_command_id": chain.source_command_id,
                "replay_ordinal": 1,
                "replay_source_payload_sha256": "d" * 64,
            })
        assert "ck_runtime_command_record_never_replays" in str(excinfo.value)
    finally:
        connection.close()


def test_capture_repeat_request_pointer_is_a_unique_foreign_key(tmp_path):
    db_path = tmp_path / "reverse-pointer.sqlite"
    command.upgrade(_config(db_path), "head")
    connection = sqlite3.connect(db_path)
    try:
        foreign_keys = {
            row[2] for row in connection.execute(
                "PRAGMA foreign_key_list(attemptcaptureprocessing)")
        }
        assert "autopilotrepeatrequest" in foreign_keys
        uniques = [
            row[1] for row in connection.execute(
                "PRAGMA index_list(attemptcaptureprocessing)") if row[2]
        ]
        unique_columns = {
            tuple(r[2] for r in connection.execute(f'PRAGMA index_info("{name}")'))
            for name in uniques
        }
        assert ("repeat_request_id",) in unique_columns
    finally:
        connection.close()


def test_downgrade_guard_blocks_replay_provenance_only(tmp_path):
    """只有 replay provenance（任一列非空）也必须阻断降级。"""
    db_path = tmp_path / "provenance-only.sqlite"
    config = _config(db_path)
    command.upgrade(config, "head")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO runtimecommand (idempotency_key, session_id, "
            "command_seq, item_id, turn_seq, turn_key, attempt_seq, "
            "prompt_level, scope_key, control_generation, runner_generation, "
            "issued_capability_token_hash, issued_device_id_hash, issued_at, "
            "kind, state, payload_json, result_json, revision, created_at, "
            "updated_at, replay_ordinal) VALUES "
            "('k-1','S-D',1,'SE_1',1,'SE_1#1',1,0,'p0a_sim_first_single_v1',"
            "1,1,'tok','dev','2026-07-30 00:00:00','tts','pending','{}','{}',"
            "0,'2026-07-30 00:00:00','2026-07-30 00:00:00', NULL)")
        # Only the provenance hash is set: still repeat evidence.  The CHECK
        # forbids that shape, so the guard is exercised through a direct write
        # with constraints momentarily off, exactly as a corrupt restore would
        # look to the downgrade.
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE runtimecommand SET replay_source_payload_sha256 = ?",
            ("c" * 64,))
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="replay"):
        command.downgrade(config, PARENT)

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT version_num FROM alembic_version"
        )).scalar_one() == REPEAT_REVISION


def test_hex64_check_compiles_without_sqlite_only_operators():
    """64-lowerhex 约束必须能在 PostgreSQL 上编译:GLOB 是 SQLite 方言专属。

    生产部署目标是 PostgreSQL。GLOB 在 PG 上是 syntax error,整条 d3 迁移会在
    真实部署时炸掉,而全 SQLite 的测试套件永远看不见。
    """
    from sqlalchemy.dialects import postgresql, sqlite
    from sqlalchemy.schema import CreateTable

    from app import models as app_models

    postgres = postgresql.dialect()
    tables = {
        app_models.RuntimeCommand.__table__,
        app_models.AttemptCaptureProcessing.__table__,
        app_models.AutopilotRepeatRequest.__table__,
        app_models.Session.__table__,
        app_models.VisitPlan.__table__,
    }
    for table in tables:
        ddl = str(CreateTable(table).compile(dialect=postgres))
        assert "GLOB" not in ddl.upper(), f"{table.name} 的 CHECK 用了 SQLite 专属 GLOB"
    # 迁移里的那份副本必须同样可移植。
    spec = importlib.util.spec_from_file_location(
        "_d3_migration_under_test",
        Path(__file__).resolve().parent.parent / "alembic" / "versions"
        / "d3f8b5c1a704_explicit_repeat_intent_ledger.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for expression in (module._REPEAT_BINDING_CHECK,
                       module._REPLAY_PROVENANCE_CHECK,
                       module._REPLAY_REQUIRES_REPEAT_BINDING_CHECK):
        assert "GLOB" not in expression.upper()
    # 两份仍必须逐字一致,否则迁移与模型会悄悄分叉。
    assert module._hex64_sql("x") == app_models._hex64_sql("x")
    del sqlite


@pytest.mark.parametrize("value,ok", [
    ("a" * 64, True),
    ("0123456789abcdef" * 4, True),
    ("f" * 64, True),
    ("g" * 64, False),
    ("A" * 64, False),
    ("z" + "a" * 63, False),
    ("a" * 63, False),
    ("a" * 65, False),
    ("é" + "a" * 63, False),
    ("-" + "a" * 63, False),
    (" " + "a" * 63, False),
    ("a" * 63 + "!", False),
])
def test_hex64_semantics_hold_on_sqlite(tmp_path, value, ok):
    """语义闭集在 SQLite 上逐个钉住;PG 上由真实 cluster gate 另行验证。"""
    from app import models as app_models

    connection = sqlite3.connect(tmp_path / "hex64.sqlite")
    try:
        connection.execute(
            "CREATE TABLE probe (digest TEXT NOT NULL CHECK ("
            + app_models._hex64_sql("digest") + "))")
        if ok:
            connection.execute("INSERT INTO probe VALUES (?)", (value,))
            assert connection.execute(
                "SELECT COUNT(*) FROM probe").fetchone()[0] == 1
        else:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO probe VALUES (?)", (value,))
    finally:
        connection.close()


_CAPTURE_REPEAT_TUPLE = (
    "SELECT id, repeat_admission_semantics, repeat_protocol_version_id, "
    "repeat_protocol_definition_digest, repeat_request_id, disposition "
    "FROM attemptcaptureprocessing WHERE id = :id"
)


def test_downgrade_guard_blocks_marker_only_corruption_and_preserves_row(
        tmp_path):
    """只把 legacy marker 改成 repeat_bound 也必须阻断降级，且原行分毫不动。

    这是最难察觉的一种腐坏：绑定、账本、终态全是 NULL，只有准入语义被改写。
    降级必须在任何 DDL 之前拒绝，而不是先删列再发现。
    """
    db_path, chain = _head_db_with_legacy_chain(
        tmp_path, "marker-only-corruption.sqlite")
    config = _config(db_path)
    connection = sqlite3.connect(db_path)
    try:
        # Only the admission marker moves; nothing else is invented, so no
        # earlier guard can be hit by accident.
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE attemptcaptureprocessing "
            "SET repeat_admission_semantics = 'repeat_bound' WHERE id = ?",
            (chain.capture_id,))
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError) as raised:
        command.downgrade(config, PARENT)
    assert str(raised.value) == (
        "存在按重复请求协议准入的采集处理行，禁止降级："
        "降级后无法区分它与协议之前的历史采集")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT version_num FROM alembic_version"
        )).scalar_one() == REPEAT_REVISION
        assert conn.execute(
            text(_CAPTURE_REPEAT_TUPLE), {"id": chain.capture_id},
        ).one() == (chain.capture_id, "repeat_bound", None, None, None, None)


def test_downgrade_guard_blocks_terminal_only_corruption_and_preserves_row(
        tmp_path):
    """只把 disposition 改成 repeat 终态也必须阻断降级，且原行分毫不动。"""
    db_path, chain = _head_db_with_legacy_chain(
        tmp_path, "terminal-only-corruption.sqlite")
    config = _config(db_path)
    connection = sqlite3.connect(db_path)
    try:
        # The marker stays legacy, bindings and request stay NULL: only the
        # terminal disposition contradicts a pre-protocol capture.
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE attemptcaptureprocessing "
            "SET disposition = 'repeat_replayed' WHERE id = ?",
            (chain.capture_id,))
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError) as raised:
        command.downgrade(config, PARENT)
    assert str(raised.value) == (
        "存在 repeat 终态采集处理行，禁止降级："
        "这些采集没有 AttemptEvent，降级无法诚实表示")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT version_num FROM alembic_version"
        )).scalar_one() == REPEAT_REVISION
        assert conn.execute(
            text(_CAPTURE_REPEAT_TUPLE), {"id": chain.capture_id},
        ).one() == (chain.capture_id, "legacy_pre_repeat", None, None, None,
                    "repeat_replayed")


def test_downgrade_guard_blocks_a_repeat_bound_terminal_capture(tmp_path):
    db_path = tmp_path / "terminal-capture-only.sqlite"
    config = _config(db_path)
    command.upgrade(config, "head")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO attemptcaptureprocessing (record_command_id, "
            "predecessor_command_id, receipt_server_seq, raw_audio_id, "
            "session_id, item_id, turn_seq, proof_attempt_seq, "
            "proof_prompt_level, processing_status, processing_generation, "
            "disposition, repeat_request_id, repeat_protocol_version_id, "
            "repeat_protocol_definition_digest, repeat_admission_semantics, "
            "created_at, processed_at, is_simulation) VALUES "
            "(1,2,1,'raw-t','S-T','SE_1',1,1,0,'asr_completed',1,"
            "'repeat_replayed',9,?,?,'repeat_bound','2026-07-30 00:00:00',"
            "'2026-07-30 00:00:00',1)",
            (APPROVED_VERSION_ID, APPROVED_DIGEST))
        connection.commit()
    finally:
        connection.close()

    # A repeat terminal capture always carries a frozen binding, so the
    # binding guard is the one that fires first; either way the downgrade is
    # refused and nothing is erased.
    with pytest.raises(RuntimeError, match="已冻结重复请求协议绑定的采集处理行"):
        command.downgrade(config, PARENT)

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT version_num FROM alembic_version"
        )).scalar_one() == REPEAT_REVISION
        assert conn.execute(text(
            "SELECT COUNT(*) FROM attemptcaptureprocessing")).scalar_one() == 1


def test_repeat_audio_manifest_contract_is_the_exact_approved_allowlist():
    """受控 audio manifest 对 repeat 的投影只有批准的四个键。"""
    from app import export

    assert export.REPEAT_AUDIO_MANIFEST_FIELDS == (
        "capture_kind", "repeat_ordinal", "outcome", "opaque_audio_code")
    forbidden = export.REPEAT_AUDIO_MANIFEST_FORBIDDEN_FIELDS
    assert {"phrase_key", "normalized_text_sha256", "capture_processing_id",
            "repeat_request_id", "record_command_id", "source_tts_command_id",
            "replay_command_id"}.issubset(forbidden)
    assert not (set(export.REPEAT_AUDIO_MANIFEST_FIELDS) & forbidden)


def test_replay_provenance_without_a_repeat_binding_is_rejected_by_name(tmp_path):
    """完整 replay 三元组 + version/digest 双 NULL：单列 CHECK 会放行，跨列不行。"""
    db_path, chain = _head_db_with_legacy_chain(
        tmp_path, "replay-needs-binding.sqlite")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        baseline = {
            **_baseline_rows(chain)["runtimecommand"],
            "replay_source_command_id": chain.source_command_id,
            "replay_ordinal": 1,
            "replay_source_payload_sha256": "d" * 64,
        }
        _insert(connection, "runtimecommand", baseline)
        connection.execute("DELETE FROM runtimecommand WHERE id = ?",
                           (baseline["id"],))
        # Both halves NULL is a legal *pair*, so only the cross-field CHECK can
        # reject a replay command that carries no repeat binding at all.
        double_null = {**baseline,
                       "repeat_protocol_version_id": None,
                       "repeat_protocol_definition_digest": None}
        with pytest.raises(sqlite3.IntegrityError) as excinfo:
            _insert(connection, "runtimecommand", double_null)
        assert str(excinfo.value) == (
            "CHECK constraint failed: "
            "ck_runtime_command_replay_requires_repeat_binding")
        table_sql = _table_sql(connection, "runtimecommand")
        assert "ck_runtime_command_repeat_binding_complete" in table_sql
        assert "ck_runtime_command_replay_requires_repeat_binding" in table_sql
    finally:
        connection.close()


DOWNGRADE_BLOCKING_BINDINGS = [
    pytest.param("visitplan", "plan_id", "VP-BASELINE", "训练安排", id="visitplan"),
    pytest.param("session", "session_id", "S-BASELINE", "场次", id="session"),
    pytest.param("runtimecommand", "id", 3, "运行命令", id="runtimecommand"),
]


@pytest.mark.parametrize("table,key,value,label", DOWNGRADE_BLOCKING_BINDINGS)
def test_downgrade_refuses_while_any_frozen_repeat_binding_exists(
        tmp_path, table, key, value, label):
    """已冻结的协议绑定本身就是不可重建的证据，降级必须拒绝。"""
    db_path, chain = _head_db_with_legacy_chain(
        tmp_path, f"downgrade-binding-{table}.sqlite")
    config = _config(db_path)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert(connection, table, _baseline_rows(chain)[table])
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match=label):
        command.downgrade(config, PARENT)

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT version_num FROM alembic_version"
        )).scalar_one() == REPEAT_REVISION


def test_downgrade_refuses_while_any_repeat_bound_capture_exists(tmp_path):
    db_path, chain = _head_db_with_legacy_chain(
        tmp_path, "downgrade-bound-capture.sqlite")
    config = _config(db_path)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _add_capture_baseline_parents(connection, chain)
        _insert(connection, "attemptcaptureprocessing",
                _baseline_rows(chain)["attemptcaptureprocessing"])
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="重复请求协议准入|采集处理行"):
        command.downgrade(config, PARENT)

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT version_num FROM alembic_version"
        )).scalar_one() == REPEAT_REVISION
        assert conn.execute(text(
            "SELECT COUNT(*) FROM attemptcaptureprocessing")).scalar_one() == 2


def _chain_identity(connection) -> dict:
    """Every fact the populated legacy chain must still assert after a cycle."""
    return {
        "plan": connection.execute(
            "SELECT plan_id, protocol_slot_key, patient_id, status, revision, "
            "approved_by, started_by, item_bank_definition_digest, "
            "autopilot_protocol_definition_digest FROM visitplan").fetchall(),
        "session": connection.execute(
            "SELECT session_id, patient_id, visit_plan_id, trainer_id, "
            "training_date, item_bank_definition_digest, "
            "autopilot_protocol_definition_digest, is_simulation, "
            "data_classification FROM session").fetchall(),
        "commands": connection.execute(
            "SELECT id, idempotency_key, command_seq, kind, state, "
            "predecessor_command_id, trigger_ack_idempotency_key, "
            "expected_raw_audio_id, payload_json, revision, issued_at, "
            "succeeded_at FROM runtimecommand ORDER BY id").fetchall(),
        "acks": connection.execute(
            "SELECT id, command_id, idempotency_key, ack_type, payload_json, "
            "receipt_server_seq, raw_audio_id, checksum, byte_count, "
            "duration_seconds, received_at FROM runtimecommandack "
            "ORDER BY id").fetchall(),
        "serve": connection.execute(
            "SELECT session_id, command_id, source, engine_version, cache_hit, "
            "result, byte_count, text_sha256, is_simulation "
            "FROM ttsserveevidence ORDER BY id").fetchall(),
        "receipt": connection.execute(
            "SELECT server_seq, raw_audio_id, session_id, turn_key, "
            "duration_seconds, byte_count, checksum, data_classification, "
            "is_simulation, contains_direct_identifier "
            "FROM audiocapturereceipt").fetchall(),
        "audio": connection.execute(
            "SELECT raw_audio_id, session_id, audio_format, status, checksum, "
            "byte_count, uploaded_at, turn_key, withdrawn, delete_gate_passed, "
            "is_simulation, data_classification FROM audioassetrow").fetchall(),
        "capture": connection.execute(
            "SELECT id, record_command_id, predecessor_command_id, "
            "receipt_server_seq, raw_audio_id, session_id, item_id, turn_seq, "
            "proof_attempt_seq, proof_prompt_level, processing_status, "
            "processing_generation, disposition, final_attempt_id, created_at, "
            "is_simulation FROM attemptcaptureprocessing").fetchall(),
        "state": connection.execute(
            "SELECT session_id, scope_key, mode, status, control_generation, "
            "runner_generation, revision, next_command_seq, current_command_id "
            "FROM sessionautopilotstate").fetchall(),
    }


def test_populated_legacy_chain_survives_a_full_d3_c7_d3_cycle(tmp_path):
    """真实旧链必须能 c7→d3→c7→d3 往返，且每一步都完整、外键干净。"""
    db_path = tmp_path / "legacy-roundtrip.sqlite"
    config = _config(db_path)
    command.upgrade(config, PARENT)
    chain = _insert_c7_populated_capture_chain(db_path)
    command.upgrade(config, REPEAT_REVISION)

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        assert list(connection.execute("PRAGMA foreign_key_check")) == []
        first = _chain_identity(connection)
        assert connection.execute(
            "SELECT repeat_admission_semantics FROM attemptcaptureprocessing"
        ).fetchall() == [("legacy_pre_repeat",)]
    finally:
        connection.close()

    # The guard must let genuinely all-NULL legacy data back down ...
    command.downgrade(config, PARENT)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        assert list(connection.execute("PRAGMA foreign_key_check")) == []
        assert _chain_identity(connection) == first
        assert not (_columns(connection, "attemptcaptureprocessing")
                    & set(_CAPTURE_REPEAT_COLUMNS))
        for table in ("visitplan", "session", "runtimecommand"):
            assert not (_columns(connection, table)
                        & set(_REPEAT_BINDING_COLUMNS))
        assert "autopilotrepeatrequest" not in {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()

    # ... and the marker must be reconstructed, identically, on the way back up.
    command.upgrade(config, REPEAT_REVISION)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        assert list(connection.execute("PRAGMA foreign_key_check")) == []
        assert _chain_identity(connection) == first
        assert connection.execute(
            "SELECT id, repeat_admission_semantics, repeat_protocol_version_id, "
            "repeat_protocol_definition_digest, repeat_request_id "
            "FROM attemptcaptureprocessing").fetchall() == [
                (chain.capture_id, "legacy_pre_repeat", None, None, None)]
        assert connection.execute(
            "SELECT repeat_protocol_version_id, "
            "repeat_protocol_definition_digest, replay_source_command_id "
            "FROM runtimecommand ORDER BY id").fetchall() == [
                (None, None, None), (None, None, None)]
        marker = [row for row in connection.execute(
            "PRAGMA table_info(attemptcaptureprocessing)")
            if row[1] == "repeat_admission_semantics"][0]
        assert marker[3] == 1 and marker[4] is None
    finally:
        connection.close()
