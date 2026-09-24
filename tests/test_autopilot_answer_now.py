# ruff: noqa: F811 -- pytest deliberately injects the imported shared fixture.
"""Explicit early answers preserve playback and recording provenance."""
import json
from pathlib import Path
import sqlite3

import pytest
from alembic import command as migration
from alembic.config import Config
from sqlmodel import Session, select

from app import asr, autopilot_ledger, export
from app.main import _run_p0a_attempt_worker
from app.models import AttemptEvent, RuntimeCommand, RuntimeCommandAck
from scripts import verify_backup_snapshot as backup_guard
from test_autopilot_api import (  # noqa: F401 - shared isolated fixtures
    SESSION_ID, ApiClients, _ack_body, _device_next, _enable_p0a, _start,
    api_clients,
)
from test_autopilot_repeat import _complete_record, _RepeatAsr


def _post(clients, command, ack_type, seq, **facts):
    return clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/{command['command_key']}/acks",
        headers=clients.device_headers,
        json=_ack_body(command, ack_type=ack_type,
                       ack_key=f"answer-now-{ack_type}-0001", device_event_seq=seq, **facts))


def test_answer_now_cancels_playback_and_issues_exactly_one_authorized_record(api_clients, monkeypatch):
    _enable_p0a(monkeypatch)
    assert _start(api_clients).status_code == 200
    tts = _device_next(api_clients)
    started = _post(api_clients, tts, "tts_started", 1)
    assert started.status_code == 200, started.text
    tts = started.json()["command"]
    facts = {"media_stopped": True, "interrupt_reason": "answer_now", "media_duration_ms": 650}
    interrupted = _post(api_clients, tts, "tts_interrupted", 2, **facts)
    assert interrupted.status_code == 200, interrupted.text
    receipt = interrupted.json()
    assert receipt["command_state"] == "cancelled"
    assert receipt["status"] == "waiting_recording"
    record = receipt["command"]
    for key in ("item_ref", "turn_seq", "attempt_seq", "prompt_level", "control_generation", "runner_generation"):
        assert record[key] == tts[key]
    replay = _post(api_clients, tts, "tts_interrupted", 2, **facts)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["command"] is None
    permit = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/{record['command_key']}/recording-authorization",
        headers=api_clients.device_headers)
    assert permit.status_code == 200, permit.text
    assert permit.json()["recording_authorized"] is True
    with Session(api_clients.engine) as db:
        commands = list(db.exec(select(RuntimeCommand).order_by(RuntimeCommand.command_seq)))
        assert [(row.kind, row.state) for row in commands] == [("tts", "cancelled"), ("record", "pending")]
        acks = list(db.exec(select(RuntimeCommandAck).order_by(RuntimeCommandAck.device_event_seq)))
        assert [row.ack_type for row in acks] == ["tts_started", "tts_interrupted"]
        assert json.loads(acks[-1].payload_json) == facts
        assert autopilot_ledger.verify_tts_ended_prerequisite(db, commands[-1]).ack_type == "tts_interrupted"


@pytest.mark.parametrize("facts", [
    {"media_stopped": False, "interrupt_reason": "answer_now", "media_duration_ms": 500},
    {"media_stopped": True, "interrupt_reason": "answer_now"},
    {"media_stopped": True, "interrupt_reason": "answer_now", "media_duration_ms": 500, "media_ended": True},
])
def test_interruption_requires_honest_physical_stop_facts(api_clients, monkeypatch, facts):
    _enable_p0a(monkeypatch)
    assert _start(api_clients).status_code == 200
    tts = _device_next(api_clients)
    started = _post(api_clients, tts, "tts_started", 1)
    response = _post(api_clients, started.json()["command"], "tts_interrupted", 2, **facts)
    assert response.status_code == 422


def test_unstarted_tts_cannot_unlock_early_recording(api_clients, monkeypatch):
    _enable_p0a(monkeypatch)
    assert _start(api_clients).status_code == 200
    tts = _device_next(api_clients)
    response = _post(api_clients, tts, "tts_interrupted", 1,
                     media_stopped=True, interrupt_reason="answer_now", media_duration_ms=0)
    assert response.status_code == 409, response.text


def test_interrupted_question_and_cue_can_finish_capture_and_scoring_without_lowering_prompt_level(api_clients, monkeypatch):
    _enable_p0a(monkeypatch)
    monkeypatch.setenv("ENABLE_LLM_JUDGE", "0")
    monkeypatch.setattr(asr, "get_engine", lambda: engine)
    engine = _RepeatAsr("", "胡萝卜")
    assert _start(api_clients).status_code == 200
    for start_seq, level in ((1, 0), (4, 1)):
        tts = _device_next(api_clients)
        assert tts["prompt_level"] == level
        started = _post(api_clients, tts, "tts_started", start_seq)
        interrupted = _post(api_clients, started.json()["command"], "tts_interrupted", start_seq + 1,
                             media_stopped=True, interrupt_reason="answer_now", media_duration_ms=400)
        assert interrupted.status_code == 200, interrupted.text
        record = interrupted.json()["command"]
        assert record["prompt_level"] == level
        stopped, _audio = _complete_record(api_clients, record, suffix=f"early-{level}", device_seq=start_seq + 2)
        assert stopped["status"] == "processing_attempt"
        _run_p0a_attempt_worker(SESSION_ID)
    with Session(api_clients.engine) as db:
        attempts = list(db.exec(select(AttemptEvent).order_by(AttemptEvent.attempt_seq)))
        assert len(attempts) == 2
        assert [(row.prompt_level, row.processing_status) for row in attempts] == [(0, "completed"), (1, "completed")]
        assert attempts[-1].contains_target is True
        assert all(row.operational_needs_review is True for row in attempts)
        assert export._prompt_playback_evidence(db, attempts[-1]) == {
            "prompt_playback_outcome": "interrupted_answer_now", "prompt_played_ms": 400,
            "prompt_exposure_needs_review": True}
    feedback = _device_next(api_clients)
    assert feedback["payload"]["purpose"] == "feedback"
    started = _post(api_clients, feedback, "tts_started", 7)
    refused = _post(api_clients, started.json()["command"], "tts_interrupted", 8,
                    media_stopped=True, interrupt_reason="answer_now", media_duration_ms=400)
    assert refused.status_code == 409, refused.text


def test_natural_end_already_committed_rejects_late_interruption(api_clients, monkeypatch):
    _enable_p0a(monkeypatch)
    assert _start(api_clients).status_code == 200
    tts = _device_next(api_clients)
    started = _post(api_clients, tts, "tts_started", 1).json()["command"]
    ended = _post(api_clients, started, "tts_ended", 2, media_ended=True, media_duration_ms=900)
    assert ended.status_code == 200, ended.text
    interrupted = _post(api_clients, started, "tts_interrupted", 3,
                        media_stopped=True, interrupt_reason="answer_now", media_duration_ms=900)
    assert interrupted.status_code == 409, interrupted.text
    with Session(api_clients.engine) as db:
        assert len(list(db.exec(select(RuntimeCommand).where(RuntimeCommand.kind == "record")))) == 1


def _migration_config(clients: ApiClients):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", str(clients.engine.url))
    return config


def _migration_snapshot(database_path):
    with sqlite3.connect(database_path) as db:
        rows = {name: db.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall()
                for name in ("runtimecommand", "runtimecommandack", "audioassetrow", "sessionautopilotstate")}
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        return db.execute("SELECT version_num FROM alembic_version").fetchone()[0], rows


def test_migration_preserves_existing_commands_acks_and_referenced_audio(api_clients, monkeypatch):
    _enable_p0a(monkeypatch)
    assert _start(api_clients).status_code == 200
    tts = _device_next(api_clients)
    started = _post(api_clients, tts, "tts_started", 1).json()["command"]
    ended = _post(api_clients, started, "tts_ended", 2, media_ended=True, media_duration_ms=900)
    assert ended.status_code == 200
    # The API fixture uses metadata.create_all, whose whole-database DDL differs
    # from historical migrations. Exercise the actual old Alembic schema, with
    # the fixture's real HTTP-generated rows copied by explicit column names.
    old_database = Path(api_clients.engine.url.database).with_name("old-head.db")
    config = _migration_config(api_clients)
    config.set_main_option("sqlalchemy.url", f"sqlite:///{old_database}")
    migration.upgrade(config, "f4b2d8c1a635")
    with sqlite3.connect(api_clients.engine.url.database) as source, sqlite3.connect(old_database) as destination:
        destination.execute("PRAGMA foreign_keys=OFF")
        tables = source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        for (table,) in tables:
            columns = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")')]
            names = ", ".join(f'"{name}"' for name in columns)
            values = source.execute(f'SELECT {names} FROM "{table}"').fetchall()
            placeholders = ", ".join("?" for _ in columns)
            destination.executemany(f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})', values)
        destination.commit()
    old_head, before = _migration_snapshot(old_database)
    assert old_head == "f4b2d8c1a635"
    assert len(before["runtimecommandack"]) == 2
    with sqlite3.connect(old_database) as db:
        previous_triggers = db.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall()
    migration.upgrade(config, "head")
    head, after = _migration_snapshot(old_database)
    assert head == "a7c3e9d2b641"
    assert after == before
    with sqlite3.connect(old_database) as db:
        assert db.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall() == previous_triggers
        assert backup_guard._schema_contract_fingerprint(db) == backup_guard.CURRENT_RECOVERY_SCHEMA_SHA256


def test_downgrade_with_interruption_refuses_without_changing_head_or_data(api_clients, monkeypatch):
    _enable_p0a(monkeypatch)
    assert _start(api_clients).status_code == 200
    tts = _device_next(api_clients)
    started = _post(api_clients, tts, "tts_started", 1).json()["command"]
    interrupted = _post(api_clients, started, "tts_interrupted", 2,
                        media_stopped=True, interrupt_reason="answer_now", media_duration_ms=400)
    assert interrupted.status_code == 200, interrupted.text
    config = _migration_config(api_clients)
    migration.stamp(config, "a7c3e9d2b641")
    before = _migration_snapshot(api_clients.engine.url.database)
    with pytest.raises(RuntimeError, match="禁止回退"):
        migration.downgrade(config, "f4b2d8c1a635")
    assert _migration_snapshot(api_clients.engine.url.database) == before
