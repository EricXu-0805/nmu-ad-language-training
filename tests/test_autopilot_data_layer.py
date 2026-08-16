"""Contract tests for the server-authoritative autopilot persistence layer."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import event, inspect, text, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from app import autopilot_ledger
from app.enums import AudioStatus, EventLine, PhaseType
from app.models import (
    AudioAssetRow,
    AudioCaptureReceipt,
    AutopilotControlEvent,
    AttemptEvent,
    Patient,
    PatientDeviceCapability,
    RuntimeCommand,
    RuntimeCommandAck,
    Session as TrainSession,
    SessionAutopilotState,
    SessionRuntimeState,
)


@pytest.fixture
def autopilot_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'autopilot-data.sqlite'}",
        connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    SQLModel.metadata.create_all(engine)
    return engine


def _seed_session(session: Session, sid: str, pid: str) -> None:
    session.add(Patient(patient_id=pid, is_simulation_subject=True))
    session.add(TrainSession(
        session_id=sid,
        patient_id=pid,
        week_no=2,
        phase_type=PhaseType.正式训练,
        event_line=EventLine.正式训练,
        item_bank_version_id="wk2-v1-20260707",
        is_simulation=True,
        data_classification="simulation",
    ))
    # The command's issued-capability FK points to an existing session.  Flush the
    # parent rows first because the test fixture intentionally has no ORM
    # relationship that could otherwise communicate insert order to SQLAlchemy.
    session.flush()
    session.add(SessionRuntimeState(session_id=sid, status="active"))
    session.add(PatientDeviceCapability(
        token_hash=f"issued-cap-{sid}",
        session_id=sid,
        device_id_hash=f"issued-device-{sid}",
        active_session_key=None,
        created_at=datetime(2026, 1, 1),
        expires_at=datetime(2030, 1, 1),
    ))


def _command(
    sid: str,
    *,
    seq: int,
    kind: str = "tts",
    idempotency_key: str | None = None,
    predecessor_command_id: int | None = None,
    trigger_ack_idempotency_key: str | None = None,
    control_generation: int = 1,
    runner_generation: int = 1,
    state: str = "pending",
    revision: int = 0,
    succeeded_at: datetime | None = None,
    payload_json: str = "{}",
    expected_raw_audio_id: str | None = None,
    issued_capability_token_hash: str | None = None,
    issued_device_id_hash: str | None = None,
    issued_at: datetime | None = None,
) -> RuntimeCommand:
    return RuntimeCommand(
        idempotency_key=idempotency_key or f"cmd-{sid}-{seq}-{kind}",
        session_id=sid,
        command_seq=seq,
        item_id="SE_胡萝卜",
        turn_seq=1,
        turn_key="SE_胡萝卜#1",
        attempt_seq=1,
        prompt_level=0,
        control_generation=control_generation,
        runner_generation=runner_generation,
        issued_capability_token_hash=(
            issued_capability_token_hash or f"issued-cap-{sid}"),
        issued_device_id_hash=(issued_device_id_hash or f"issued-device-{sid}"),
        issued_at=issued_at or datetime(2026, 7, 18, 8, 0, 0),
        kind=kind,
        state=state,
        predecessor_command_id=predecessor_command_id,
        trigger_ack_idempotency_key=trigger_ack_idempotency_key,
        expected_raw_audio_id=expected_raw_audio_id,
        payload_json=payload_json,
        revision=revision,
        succeeded_at=succeeded_at,
    )


def _capability(
    sid: str,
    *,
    token_hash: str,
    device_hash: str,
    now: datetime,
) -> PatientDeviceCapability:
    return PatientDeviceCapability(
        token_hash=token_hash,
        session_id=sid,
        device_id_hash=device_hash,
        active_session_key=sid,
        created_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
    )


def _precreated_audio(sid: str, raw_audio_id: str) -> AudioAssetRow:
    """Server allocates and registers this row before issuing a record command."""
    return AudioAssetRow(
        raw_audio_id=raw_audio_id,
        session_id=sid,
        is_simulation=True,
        data_classification="simulation",
        turn_key="SE_胡萝卜#1",
        status=AudioStatus.recorded,
    )


def test_device_event_seq_fence_is_strict_bound_and_current(autopilot_engine):
    now = datetime(2026, 7, 18, 9, 0, 0)
    with Session(autopilot_engine) as session:
        _seed_session(session, "S-SEQ", "P-SEQ")
        session.commit()
        capability = _capability(
            "S-SEQ", token_hash="cap-seq", device_hash="device-seq", now=now)
        session.add(capability)
        session.commit()
        assert capability.last_autopilot_event_seq == 0

        assert autopilot_ledger.try_advance_device_autopilot_event_seq(
            session,
            capability_token_hash=capability.token_hash,
            session_id=capability.session_id,
            device_id_hash=capability.device_id_hash,
            candidate_seq=3,
            now=now,
        ) is True
        session.commit()

        # Exact/older delivery, or any token binding mismatch, cannot consume a
        # later number or roll the durable watermark backwards.
        for candidate_seq in (3, 2):
            assert autopilot_ledger.try_advance_device_autopilot_event_seq(
                session,
                capability_token_hash=capability.token_hash,
                session_id=capability.session_id,
                device_id_hash=capability.device_id_hash,
                candidate_seq=candidate_seq,
                now=now,
            ) is False
        assert autopilot_ledger.try_advance_device_autopilot_event_seq(
            session,
            capability_token_hash=capability.token_hash,
            session_id="another-session",
            device_id_hash=capability.device_id_hash,
            candidate_seq=4,
            now=now,
        ) is False
        assert autopilot_ledger.try_advance_device_autopilot_event_seq(
            session,
            capability_token_hash=capability.token_hash,
            session_id=capability.session_id,
            device_id_hash="another-device",
            candidate_seq=4,
            now=now,
        ) is False
        session.expire_all()
        assert session.get(
            PatientDeviceCapability, capability.token_hash,
        ).last_autopilot_event_seq == 3

        capability = session.get(PatientDeviceCapability, capability.token_hash)
        capability.active_session_key = None
        capability.recovery_only_at = now
        session.commit()
        assert autopilot_ledger.try_advance_device_autopilot_event_seq(
            session,
            capability_token_hash=capability.token_hash,
            session_id=capability.session_id,
            device_id_hash=capability.device_id_hash,
            candidate_seq=4,
            now=now,
        ) is False

        with pytest.raises(ValueError, match="positive integer"):
            autopilot_ledger.try_advance_device_autopilot_event_seq(
                session,
                capability_token_hash=capability.token_hash,
                session_id=capability.session_id,
                device_id_hash=capability.device_id_hash,
                candidate_seq=0,
                now=now,
            )

        capability.last_autopilot_event_seq = -1
        with pytest.raises(IntegrityError):
            session.commit()


def test_device_event_seq_fence_is_atomic_across_competing_sessions(
        autopilot_engine):
    now = datetime(2026, 7, 18, 9, 30, 0)
    with Session(autopilot_engine) as session:
        _seed_session(session, "S-SEQ-RACE", "P-SEQ-RACE")
        session.commit()
        session.add(_capability(
            "S-SEQ-RACE", token_hash="cap-seq-race",
            device_hash="device-seq-race", now=now))
        session.commit()

    ready = Barrier(2)

    def _advance() -> bool:
        with Session(autopilot_engine) as worker_session:
            ready.wait()
            advanced = autopilot_ledger.try_advance_device_autopilot_event_seq(
                worker_session,
                capability_token_hash="cap-seq-race",
                session_id="S-SEQ-RACE",
                device_id_hash="device-seq-race",
                candidate_seq=1,
                now=now,
            )
            worker_session.commit()
            return advanced

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: _advance(), range(2)))
    assert sorted(outcomes) == [False, True]
    with Session(autopilot_engine) as session:
        assert session.get(
            PatientDeviceCapability, "cap-seq-race",
        ).last_autopilot_event_seq == 1


def test_command_constraints_and_composite_session_foreign_keys(autopilot_engine):
    with Session(autopilot_engine) as session:
        _seed_session(session, "S-AUTO-1", "P-AUTO-1")
        _seed_session(session, "S-AUTO-2", "P-AUTO-2")
        session.commit()

        # A disabled, never-started control row may be generation zero.
        session.add(SessionAutopilotState(session_id="S-AUTO-1"))
        session.commit()

        # Commands/ACKs are only created after an actual run starts at generation 1.
        session.add(_command(
            "S-AUTO-1", seq=1, control_generation=0, runner_generation=1))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        tts = _command("S-AUTO-1", seq=1)
        session.add(tts)
        session.commit()
        assert tts.id is not None

        session.add(_command(
            "S-AUTO-1", seq=2, kind="record",
            idempotency_key="record-without-ended-proof"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        # The composite FK prevents a command in S2 from naming an S1 predecessor.
        session.add(_precreated_audio("S-AUTO-2", "raw-auto-2-cross"))
        session.commit()
        session.add(_command(
            "S-AUTO-2", seq=1, kind="record",
            predecessor_command_id=tts.id,
            trigger_ack_idempotency_key="ack-tts-ended-s1",
            expected_raw_audio_id="raw-auto-2-cross"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        # Current-command pointers are likewise constrained to their own session.
        state2 = SessionAutopilotState(
            session_id="S-AUTO-2", scope_key="p0a_sim_first_single_v1",
            mode="autonomous", status="waiting_tts",
            control_generation=1, runner_generation=1, current_command_id=tts.id)
        session.add(state2)
        with pytest.raises(IntegrityError):
            session.commit()


def test_record_command_consumes_predecessor_and_raw_audio_exactly_once(
        autopilot_engine):
    with Session(autopilot_engine) as session:
        _seed_session(session, "S-ONCE", "P-ONCE")
        session.commit()
        tts1 = _command("S-ONCE", seq=1, idempotency_key="tts-once-1")
        tts2 = _command("S-ONCE", seq=2, idempotency_key="tts-once-2")
        session.add_all([
            tts1, tts2,
            _precreated_audio("S-ONCE", "raw-once-1"),
            _precreated_audio("S-ONCE", "raw-once-2"),
        ])
        session.commit()
        record = _command(
            "S-ONCE", seq=3, kind="record", idempotency_key="record-once-1",
            predecessor_command_id=tts1.id,
            trigger_ack_idempotency_key="ack-ended-once-1",
            expected_raw_audio_id="raw-once-1")
        session.add(record)
        session.commit()

        # One tts_ended fact cannot authorize two record commands.
        session.add(_command(
            "S-ONCE", seq=4, kind="record", idempotency_key="record-reuse-tts",
            predecessor_command_id=tts1.id,
            trigger_ack_idempotency_key="ack-ended-once-1",
            expected_raw_audio_id="raw-once-2"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        # Nor can another TTS command reuse a raw id already allocated to record 1.
        session.add(_command(
            "S-ONCE", seq=4, kind="record", idempotency_key="record-reuse-raw",
            predecessor_command_id=tts2.id,
            trigger_ack_idempotency_key="ack-ended-once-2",
            expected_raw_audio_id="raw-once-1"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_ack_constraints_idempotency_and_append_only(autopilot_engine):
    now = datetime(2026, 7, 18, 8, 0, 0)
    with Session(autopilot_engine) as session:
        _seed_session(session, "S-ACK", "P-ACK")
        session.commit()
        capability = _capability(
            "S-ACK", token_hash="cap-hash-ack", device_hash="device-hash-ack", now=now)
        session.add(capability)
        command_row = _command(
            "S-ACK", seq=1,
            issued_capability_token_hash=capability.token_hash,
            issued_device_id_hash=capability.device_id_hash,
            issued_at=now - timedelta(seconds=1))
        session.add(command_row)
        session.commit()

        incomplete = RuntimeCommandAck(
            command_id=command_row.id,
            idempotency_key="ack-record-incomplete",
            session_id="S-ACK",
            ack_type="record_stopped",
            command_revision=0,
            control_generation=1,
            runner_generation=1,
            device_event_seq=1,
            device_id_hash=capability.device_id_hash,
            capability_token_hash=capability.token_hash,
            received_at=now,
        )
        session.add(incomplete)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        ack = RuntimeCommandAck(
            command_id=command_row.id,
            idempotency_key="ack-tts-started-1",
            session_id="S-ACK",
            ack_type="tts_started",
            command_revision=0,
            control_generation=1,
            runner_generation=1,
            device_event_seq=1,
            device_id_hash=capability.device_id_hash,
            capability_token_hash=capability.token_hash,
            payload_json=autopilot_ledger.encode_ack_payload(
                "tts_started", {"media_duration_ms": 500}),
            received_at=now,
        )
        session.add(ack)
        session.commit()

        duplicate_device_event = RuntimeCommandAck(
            command_id=command_row.id,
            idempotency_key="ack-tts-started-retry",
            session_id="S-ACK",
            ack_type="tts_started",
            command_revision=0,
            control_generation=1,
            runner_generation=1,
            device_event_seq=1,
            device_id_hash=capability.device_id_hash,
            capability_token_hash=capability.token_hash,
            received_at=now,
        )
        session.add(duplicate_device_event)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        stored = session.get(RuntimeCommandAck, ack.id)
        stored.payload_json = "{}"
        with pytest.raises(RuntimeError, match="只追加"):
            session.commit()
        session.rollback()
        stored = session.get(RuntimeCommandAck, ack.id)
        session.delete(stored)
        with pytest.raises(RuntimeError, match="只追加"):
            session.commit()


def test_control_event_is_append_only_and_payload_rejects_response_text(
        autopilot_engine):
    with Session(autopilot_engine) as session:
        _seed_session(session, "S-CONTROL", "P-CONTROL")
        session.commit()
        row = AutopilotControlEvent(
            idempotency_key="control-takeover-1",
            session_id="S-CONTROL",
            event_seq=1,
            event_type="takeover",
            scope_key="p0a_sim_first_single_v1",
            control_generation=2,
            runner_generation=1,
            actor_type="researcher",
            actor_id="RESEARCHER-1",
            reason_code="patient_requested_pause",
            payload_json=autopilot_ledger.encode_control_event_payload(
                "takeover", {"reason_code": "patient_requested_pause"}),
        )
        session.add(row)
        session.commit()
        row.reason_code = "changed"
        with pytest.raises(RuntimeError, match="只追加"):
            session.commit()

    with pytest.raises(ValueError, match="response/free-text"):
        autopilot_ledger.encode_control_event_payload(
            "takeover", {"response_text": "患者自由回答"})


def test_p0a_scope_completion_never_marks_full_intervention_complete(
        autopilot_engine):
    with Session(autopilot_engine) as session:
        _seed_session(session, "S-SCOPE", "P-SCOPE")
        session.commit()
        session.add(SessionAutopilotState(
            session_id="S-SCOPE",
            scope_key="p0a_sim_first_single_v1",
            mode="autonomous",
            status="scope_completed",
            control_generation=1,
            runner_generation=1,
        ))
        session.add(AutopilotControlEvent(
            idempotency_key="control-scope-complete-1",
            session_id="S-SCOPE",
            event_seq=1,
            event_type="scope_complete",
            scope_key="p0a_sim_first_single_v1",
            control_generation=1,
            runner_generation=1,
            actor_type="system",
            payload_json=autopilot_ledger.encode_control_event_payload(
                "scope_complete", {"completed_command_seq": 2}),
        ))
        session.commit()
        state = session.get(SessionAutopilotState, "S-SCOPE")
        assert state.status == "scope_completed"
        runtime = session.get(SessionRuntimeState, "S-SCOPE")
        assert runtime is not None
        assert runtime.status == "active"
        assert runtime.intervention_completed_at is None
        assert runtime.intervention_ended_by is None
        assert runtime.completed_at is None


def test_p0a_schema_rejects_full_intervention_completion_labels(
        autopilot_engine):
    with Session(autopilot_engine) as session:
        _seed_session(session, "S-NO-FULL", "P-NO-FULL")
        session.commit()
        session.add(SessionAutopilotState(
            session_id="S-NO-FULL",
            scope_key="p0a_sim_first_single_v1",
            mode="autonomous",
            status="intervention_completed",
            control_generation=1,
            runner_generation=1,
        ))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        for seq, event_type, from_status, to_status in (
            (1, "intervention_complete", None, None),
            (2, "start", "intervention_completed", "running"),
            (3, "start", "idle", "intervention_completed"),
        ):
            session.add(AutopilotControlEvent(
                idempotency_key=f"forbidden-full-label-{seq}",
                session_id="S-NO-FULL",
                event_seq=seq,
                event_type=event_type,
                scope_key="p0a_sim_first_single_v1",
                control_generation=1,
                runner_generation=1,
                actor_type="system",
                from_status=from_status,
                to_status=to_status,
            ))
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

    with pytest.raises(ValueError, match="unknown autopilot event type"):
        autopilot_ledger.encode_control_event_payload(
            "intervention_complete", {"completed_command_seq": 2})


def test_state_and_command_cas_generation_fences(autopilot_engine):
    now = datetime(2026, 7, 18, 9, 0, 0)
    with Session(autopilot_engine) as session:
        _seed_session(session, "S-CAS", "P-CAS")
        capability = _capability(
            "S-CAS", token_hash="cap-hash-cas", device_hash="device-hash-cas",
            now=now)
        session.add(capability)
        session.add(SessionAutopilotState(
            session_id="S-CAS", scope_key="p0a_sim_first_single_v1",
            mode="autonomous", status="idle",
            control_generation=1, runner_generation=0, revision=0))
        session.commit()

        assert autopilot_ledger.try_claim_autopilot_state(
            session, "S-CAS", owner="runner-a", expected_revision=0,
            expected_control_generation=1, now=now) is True
        session.commit()
        state = session.get(SessionAutopilotState, "S-CAS")
        assert (state.runner_generation, state.revision, state.lease_owner) == (
            1, 1, "runner-a")
        claim = autopilot_ledger.claim_from_autopilot_state(state)

        assert autopilot_ledger.try_claim_autopilot_state(
            session, "S-CAS", owner="runner-stale", expected_revision=0,
            expected_control_generation=1, now=now) is False
        assert autopilot_ledger.fenced_autopilot_update(
            session, claim, values={"status": "running"}, now=now) is True
        session.commit()
        assert autopilot_ledger.fenced_autopilot_update(
            session, claim, values={"status": "paused"}, now=now) is False

        command_row = _command(
            "S-CAS", seq=1,
            issued_capability_token_hash=capability.token_hash,
            issued_device_id_hash=capability.device_id_hash,
            issued_at=now - timedelta(seconds=1),
            payload_json=json.dumps({
                "schema_version": 1,
                "speech_key": "question:se_carrot:1:0",
                "speech_text": "请看这张图片，这是什么？",
                "purpose": "question",
                "item_id": "SE_胡萝卜",
                "turn_seq": 1,
                "cue_level": 0,
            }, ensure_ascii=False))
        session.add(command_row)
        session.flush()
        state = session.get(SessionAutopilotState, "S-CAS")
        state.status = "waiting_tts"
        state.current_command_id = command_row.id
        session.commit()
        assert autopilot_ledger.try_claim_runtime_command(
            session, command_row.id, owner="runner-a", expected_revision=0,
            control_generation=1, runner_generation=1, now=now) is True
        session.commit()
        command_row = session.get(RuntimeCommand, command_row.id)
        command_claim = autopilot_ledger.claim_from_runtime_command(command_row)

        with pytest.raises(ValueError, match="requires strong terminal ACK"):
            autopilot_ledger.fenced_command_transition(
                session, command_claim, expected_state="pending",
                next_state="succeeded", now=now)
        tts_ack = RuntimeCommandAck(
            command_id=command_row.id,
            idempotency_key="ack-tts-ended-cas",
            session_id="S-CAS",
            ack_type="tts_ended",
            command_revision=0,
            control_generation=1,
            runner_generation=1,
            device_event_seq=1,
            device_id_hash=capability.device_id_hash,
            capability_token_hash=capability.token_hash,
            payload_json=autopilot_ledger.encode_ack_payload(
                "tts_ended", {"media_ended": True}),
            received_at=now,
        )
        session.add(tts_ack)
        session.flush()
        assert autopilot_ledger.fenced_command_transition(
            session, command_claim, expected_state="pending",
            next_state="succeeded", terminal_ack=tts_ack, now=now) is True
        session.commit()
        terminal = session.get(RuntimeCommand, command_row.id)
        assert terminal.state == "succeeded" and terminal.revision == 1
        assert terminal.lease_owner is None and terminal.succeeded_at == now

        audio = _precreated_audio("S-CAS", "raw-cas-record")
        audio.checksum = "c" * 64
        audio.byte_count = 1024
        audio.uploaded_at = now
        session.add(audio)
        session.commit()
        record = _command(
            "S-CAS", seq=2, kind="record",
            issued_capability_token_hash=capability.token_hash,
            issued_device_id_hash=capability.device_id_hash,
            issued_at=now - timedelta(seconds=1),
            predecessor_command_id=terminal.id,
            trigger_ack_idempotency_key="ack-tts-ended-cas",
            expected_raw_audio_id="raw-cas-record",
            payload_json=json.dumps({
                "schema_version": 1,
                "raw_audio_id": "raw-cas-record",
                "turn_key": "SE_胡萝卜#1",
                "item_id": "SE_胡萝卜",
                "turn_seq": 1,
                "cue_level": 0,
                "max_duration_seconds": 30,
                "contains_direct_identifier": False,
            }, ensure_ascii=False))
        session.add(record)
        session.flush()
        state = session.get(SessionAutopilotState, "S-CAS")
        state.status = "waiting_recording"
        state.current_command_id = record.id
        session.commit()
        assert autopilot_ledger.try_claim_runtime_command(
            session, record.id, owner="runner-a", expected_revision=0,
            control_generation=1, runner_generation=1, now=now) is True
        session.commit()
        record_claim = autopilot_ledger.claim_from_runtime_command(
            session.get(RuntimeCommand, record.id))
        with pytest.raises(ValueError, match="record_stopped"):
            autopilot_ledger.fenced_command_transition(
                session, record_claim, expected_state="pending",
                next_state="succeeded", terminal_ack=tts_ack, now=now)
        receipt = AudioCaptureReceipt(
            raw_audio_id=audio.raw_audio_id,
            session_id="S-CAS",
            turn_key=audio.turn_key,
            received_at=now,
            duration_seconds=2.0,
            byte_count=audio.byte_count,
            checksum=audio.checksum,
            data_classification="simulation",
            is_simulation=True,
            contains_direct_identifier=False,
        )
        session.add(receipt)
        session.flush()
        stopped_ack = RuntimeCommandAck(
            command_id=record.id,
            idempotency_key="ack-record-stopped-cas",
            session_id="S-CAS",
            ack_type="record_stopped",
            command_revision=0,
            control_generation=1,
            runner_generation=1,
            device_event_seq=2,
            device_id_hash=capability.device_id_hash,
            capability_token_hash=capability.token_hash,
            payload_json=autopilot_ledger.encode_ack_payload(
                "record_stopped", {"stop_reason": "user_done"}),
            receipt_server_seq=receipt.server_seq,
            raw_audio_id=audio.raw_audio_id,
            checksum=audio.checksum,
            byte_count=audio.byte_count,
            duration_seconds=receipt.duration_seconds,
            received_at=now,
        )
        session.add(stopped_ack)
        session.flush()
        assert autopilot_ledger.fenced_command_transition(
            session, record_claim, expected_state="pending",
            next_state="succeeded", terminal_ack=stopped_ack, now=now)


def test_generic_state_cas_cannot_complete_scope_and_db_clears_command(
        autopilot_engine):
    now = datetime(2026, 7, 18, 9, 45, 0)
    with Session(autopilot_engine) as session:
        _seed_session(session, "S-NO-GENERIC-COMPLETE", "P-NO-GENERIC-COMPLETE")
        session.add(SessionAutopilotState(
            session_id="S-NO-GENERIC-COMPLETE",
            scope_key="p0a_sim_first_single_v1",
            mode="autonomous",
            status="running",
            control_generation=1,
            runner_generation=1,
            revision=3,
            lease_owner="runner-without-terminal-proof",
            lease_acquired_at=now - timedelta(seconds=1),
            lease_expires_at=now + timedelta(seconds=30),
        ))
        session.commit()
        state = session.get(SessionAutopilotState, "S-NO-GENERIC-COMPLETE")
        claim = autopilot_ledger.claim_from_autopilot_state(state)

        # The generic CAS has no persisted terminal command/ACK proof, so it can
        # never be used as a shortcut into the completed scope state.
        with pytest.raises(
                ValueError,
                match="invalid autopilot state transition running -> scope_completed"):
            autopilot_ledger.fenced_autopilot_update(
                session, claim, values={"status": "scope_completed"}, now=now)
        session.refresh(state)
        assert state.status == "running"

        command_row = _command("S-NO-GENERIC-COMPLETE", seq=1)
        session.add(command_row)
        session.commit()
        state.status = "scope_completed"
        state.current_command_id = command_row.id
        with pytest.raises(IntegrityError):
            session.commit()


def test_command_transition_rejects_identity_rewrite_and_local_time_skew(
        autopilot_engine):
    """Identity is immutable and default times share capability's UTC basis."""
    with Session(autopilot_engine) as session:
        claim = autopilot_ledger.CommandClaim(
            command_id=1, owner="runner", scope_key="p0a_sim_first_single_v1",
            control_generation=1, runner_generation=1, revision=0,
            kind="tts", state="pending",
            lease_expires_at=datetime(2026, 7, 18, 1, 0, 30),
        )
        for field in (
            "item_id", "turn_key", "attempt_seq", "prompt_level",
            "expected_raw_audio_id", "payload_json",
        ):
            with pytest.raises(ValueError, match="protected fields"):
                autopilot_ledger.fenced_command_transition(
                    session, claim, expected_state="pending", next_state="failed",
                    values={field: "mutated"},
                    now=datetime(2026, 7, 18, 1, 0, 0),
                )

    observed = autopilot_ledger.utc_now_naive()
    current_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((current_utc - observed).total_seconds()) < 2


def test_waiting_state_recovery_preserves_generation_and_delayed_ack(autopilot_engine):
    now = datetime(2026, 7, 18, 10, 0, 0)
    expired = now - timedelta(seconds=1)
    with Session(autopilot_engine) as session:
        _seed_session(session, "S-WAIT", "P-WAIT")
        session.commit()
        command_row = _command("S-WAIT", seq=1)
        session.add(command_row)
        session.flush()
        state = SessionAutopilotState(
            session_id="S-WAIT", scope_key="p0a_sim_first_single_v1",
            mode="autonomous", status="waiting_tts",
            control_generation=1, runner_generation=1, revision=4,
            current_command_id=command_row.id,
            lease_owner="dead-runner", lease_acquired_at=expired - timedelta(seconds=30),
            lease_expires_at=expired,
        )
        session.add(state)
        session.commit()

        # Generic claiming would advance the generation and stale the in-flight ACK,
        # so waiting states never use that path.
        assert autopilot_ledger.try_claim_autopilot_state(
            session, "S-WAIT", owner="new-runner", expected_revision=4,
            expected_control_generation=1, now=now) is False
        assert autopilot_ledger.try_recover_waiting_autopilot_state(
            session, "S-WAIT", current_command_id=command_row.id,
            owner="new-runner", expected_revision=4,
            expected_control_generation=1, expected_runner_generation=1,
            now=now) is True
        session.commit()
        recovered = session.get(SessionAutopilotState, "S-WAIT")
        assert recovered.runner_generation == 1
        assert recovered.revision == 5
        assert recovered.lease_owner == "new-runner"


def _seed_complete_capture_chain(session: Session) -> tuple[int, int]:
    now = datetime(2026, 7, 18, 11, 0, 0)
    _seed_session(session, "S-PROOF", "P-PROOF")
    session.commit()
    capability = _capability(
        "S-PROOF", token_hash="cap-hash-proof", device_hash="device-hash-proof", now=now)
    session.add(capability)
    tts = _command(
        "S-PROOF", seq=1, state="succeeded", revision=3, succeeded_at=now,
        issued_capability_token_hash=capability.token_hash,
        issued_device_id_hash=capability.device_id_hash,
        issued_at=now - timedelta(seconds=1),
        payload_json=json.dumps({
            "schema_version": 1,
            "speech_key": "question:se_carrot:1:0",
            "speech_text": "请看这张图片，这是什么？",
            "purpose": "question",
            "item_id": "SE_胡萝卜",
            "turn_seq": 1,
            "cue_level": 0,
        }, ensure_ascii=False))
    session.add(tts)
    session.flush()
    tts_ack = RuntimeCommandAck(
        command_id=tts.id,
        idempotency_key="ack-tts-ended-proof",
        session_id="S-PROOF",
        ack_type="tts_ended",
        command_revision=2,
        control_generation=1,
        runner_generation=1,
        device_event_seq=1,
        device_id_hash=capability.device_id_hash,
        capability_token_hash=capability.token_hash,
        payload_json=autopilot_ledger.encode_ack_payload(
            "tts_ended", {"media_ended": True, "media_duration_ms": 850}),
        received_at=now,
    )
    session.add(tts_ack)
    session.flush()

    # The server pre-registers this exact id before the record command is visible.
    checksum = "a" * 64
    audio = _precreated_audio("S-PROOF", "raw-proof-1")
    audio.checksum = checksum
    audio.byte_count = 2048
    audio.uploaded_at = now
    audio.contains_direct_identifier = False
    audio.delete_gate_passed = False
    session.add(audio)
    session.flush()

    record_payload = json.dumps({
        "schema_version": 1,
        "raw_audio_id": audio.raw_audio_id,
        "turn_key": "SE_胡萝卜#1",
        "item_id": "SE_胡萝卜",
        "turn_seq": 1,
        "cue_level": 0,
        "max_duration_seconds": 30,
        "contains_direct_identifier": False,
    }, ensure_ascii=False)
    record = _command(
        "S-PROOF", seq=2, kind="record", state="succeeded", revision=3,
        succeeded_at=now, predecessor_command_id=tts.id,
        issued_capability_token_hash=capability.token_hash,
        issued_device_id_hash=capability.device_id_hash,
        issued_at=now,
        trigger_ack_idempotency_key=tts_ack.idempotency_key,
        expected_raw_audio_id=audio.raw_audio_id,
        payload_json=record_payload)
    session.add(record)
    session.flush()

    receipt = AudioCaptureReceipt(
        raw_audio_id=audio.raw_audio_id,
        session_id="S-PROOF",
        turn_key=audio.turn_key,
        received_at=now,
        duration_seconds=2.5,
        byte_count=audio.byte_count,
        checksum=checksum,
        data_classification="simulation",
        is_simulation=True,
        contains_direct_identifier=False,
    )
    session.add(receipt)
    session.flush()
    stopped_ack = RuntimeCommandAck(
        command_id=record.id,
        idempotency_key="ack-record-stopped-proof",
        session_id="S-PROOF",
        ack_type="record_stopped",
        command_revision=2,
        control_generation=1,
        runner_generation=1,
        device_event_seq=2,
        device_id_hash=capability.device_id_hash,
        capability_token_hash=capability.token_hash,
        payload_json=autopilot_ledger.encode_ack_payload(
            "record_stopped", {"stop_reason": "silence"}),
        receipt_server_seq=receipt.server_seq,
        raw_audio_id=audio.raw_audio_id,
        checksum=checksum,
        byte_count=audio.byte_count,
        duration_seconds=receipt.duration_seconds,
        received_at=now,
    )
    session.add(stopped_ack)
    session.add(SessionAutopilotState(
        session_id="S-PROOF", scope_key="p0a_sim_first_single_v1",
        mode="autonomous", status="processing_attempt",
        control_generation=1, runner_generation=1, revision=8,
        current_command_id=record.id))
    session.commit()
    return record.id, receipt.server_seq


def test_only_tts_ended_and_exact_capture_receipt_unlock_attempt_proof(
        autopilot_engine):
    with Session(autopilot_engine) as session:
        record_id, _receipt_id = _seed_complete_capture_chain(session)
        proof = autopilot_ledger.verify_record_capture_for_attempt(session, record_id)
        assert proof.raw_audio_id == "raw-proof-1"
        assert proof.byte_count == 2048
        assert list(session.exec(select(AttemptEvent))) == []

        record = session.get(RuntimeCommand, record_id)
        tts = session.get(RuntimeCommand, record.predecessor_command_id)

        # Ordinary ORM callers cannot rewrite issued command identity at all.
        tts_payload = json.loads(tts.payload_json)
        tts_payload["purpose"] = "feedback"
        tts.payload_json = json.dumps(tts_payload, ensure_ascii=False)
        with pytest.raises(RuntimeError, match="禁止 ORM"):
            session.commit()
        session.rollback()

        # Simulate an out-of-band database tamper to ensure proof verification is
        # fail-closed even if a privileged actor bypasses the ORM guard.
        session.execute(update(RuntimeCommand).where(
            RuntimeCommand.id == tts.id,
        ).values(payload_json=json.dumps(tts_payload, ensure_ascii=False)))
        session.commit()
        session.expire_all()
        with pytest.raises(autopilot_ledger.AutopilotProofError, match="question/cue"):
            autopilot_ledger.verify_record_capture_for_attempt(session, record_id)
        tts_payload["purpose"] = "question"
        session.execute(update(RuntimeCommand).where(
            RuntimeCommand.id == tts.id,
        ).values(payload_json=json.dumps(tts_payload, ensure_ascii=False)))
        session.commit()
        session.expire_all()

        record = session.get(RuntimeCommand, record_id)
        record_payload = json.loads(record.payload_json)
        record_payload["raw_audio_id"] = "raw-from-another-command"
        session.execute(update(RuntimeCommand).where(
            RuntimeCommand.id == record_id,
        ).values(payload_json=json.dumps(record_payload, ensure_ascii=False)))
        session.commit()
        session.expire_all()
        with pytest.raises(autopilot_ledger.AutopilotProofError, match="capture evidence"):
            autopilot_ledger.verify_record_capture_for_attempt(session, record_id)
        record_payload["raw_audio_id"] = "raw-proof-1"
        record_payload["max_duration_seconds"] = 1
        session.execute(update(RuntimeCommand).where(
            RuntimeCommand.id == record_id,
        ).values(payload_json=json.dumps(record_payload, ensure_ascii=False)))
        session.commit()
        session.expire_all()
        with pytest.raises(autopilot_ledger.AutopilotProofError, match="duration exceeds"):
            autopilot_ledger.verify_record_capture_for_attempt(session, record_id)
        record_payload["max_duration_seconds"] = 30
        session.execute(update(RuntimeCommand).where(
            RuntimeCommand.id == record_id,
        ).values(payload_json=json.dumps(record_payload, ensure_ascii=False)))
        session.commit()
        session.expire_all()

        # An exported/deletable/deleted asset cannot be turned into a new Attempt.
        audio = session.get(AudioAssetRow, "raw-proof-1")
        audio.status = AudioStatus.deletable
        session.commit()
        with pytest.raises(autopilot_ledger.AutopilotProofError, match="audio asset"):
            autopilot_ledger.verify_record_capture_for_attempt(session, record_id)


@pytest.mark.parametrize("payload_json", [
    "{",
    '{"stop_reason": "silence"}',
    '{}',
    '{"stop_reason":"researcher_override"}',
    '{"stop_reason":"silence","unexpected":true}',
])
def test_terminal_record_proof_rejects_noncanonical_or_unbounded_ack_payload(
        autopilot_engine, payload_json):
    with Session(autopilot_engine) as session:
        record_id, _receipt_id = _seed_complete_capture_chain(session)
        session.execute(update(RuntimeCommandAck).where(
            RuntimeCommandAck.command_id == record_id,
            RuntimeCommandAck.ack_type == "record_stopped",
        ).values(payload_json=payload_json))
        session.commit()
        session.expire_all()

        with pytest.raises(
            autopilot_ledger.AutopilotProofError,
            match="payload is not canonical evidence",
        ):
            autopilot_ledger.verify_terminal_record_capture(session, record_id)


def test_terminal_record_proof_rejects_ack_after_command_success(
        autopilot_engine):
    with Session(autopilot_engine) as session:
        record_id, _receipt_id = _seed_complete_capture_chain(session)
        record = session.get(RuntimeCommand, record_id)
        assert record is not None and record.succeeded_at is not None
        session.execute(update(RuntimeCommandAck).where(
            RuntimeCommandAck.command_id == record_id,
            RuntimeCommandAck.ack_type == "record_stopped",
        ).values(received_at=record.succeeded_at + timedelta(microseconds=1)))
        session.commit()
        session.expire_all()

        with pytest.raises(
            autopilot_ledger.AutopilotProofError,
            match="success timestamp predates its terminal ACK",
        ):
            autopilot_ledger.verify_terminal_record_capture(session, record_id)


@pytest.mark.parametrize("bad_tts_ack_payload", [
    '{"media_ended": false, "media_duration_ms": 850}',
    '{"media_duration_ms": 850}',
    '{',
])
def test_terminal_tts_proof_rejects_noncanonical_media_ended_ack(
        autopilot_engine, bad_tts_ack_payload):
    # verify_terminal_tts_ack fails closed unless the tts_ended ACK is a canonical
    # media_ended=True fact — a record can only open after the cue truly finished.
    with Session(autopilot_engine) as session:
        record_id, _receipt_id = _seed_complete_capture_chain(session)
        session.execute(update(RuntimeCommandAck).where(
            RuntimeCommandAck.session_id == "S-PROOF",
            RuntimeCommandAck.ack_type == "tts_ended",
        ).values(payload_json=bad_tts_ack_payload))
        session.commit()
        session.expire_all()

        with pytest.raises(
            autopilot_ledger.AutopilotProofError,
            match="tts_ended ACK payload is not canonical evidence",
        ):
            autopilot_ledger.verify_terminal_record_capture(session, record_id)


def test_record_prerequisite_rejects_issue_before_terminal_tts_evidence(
        autopilot_engine):
    with Session(autopilot_engine) as session:
        record_id, _receipt_id = _seed_complete_capture_chain(session)
        record = session.get(RuntimeCommand, record_id)
        assert record is not None
        session.execute(update(RuntimeCommand).where(
            RuntimeCommand.id == record_id,
        ).values(issued_at=record.issued_at - timedelta(seconds=1)))
        session.commit()
        session.expire_all()

        with pytest.raises(
            autopilot_ledger.AutopilotProofError,
            match="record command predates terminal TTS evidence",
        ):
            autopilot_ledger.verify_terminal_record_capture(session, record_id)


def test_record_prerequisite_rejects_non_sequential_command_seq(autopilot_engine):
    with Session(autopilot_engine) as session:
        record_id, _receipt_id = _seed_complete_capture_chain(session)
        session.execute(update(RuntimeCommand).where(
            RuntimeCommand.id == record_id,
        ).values(command_seq=5))
        session.commit()
        session.expire_all()

        with pytest.raises(
            autopilot_ledger.AutopilotProofError,
            match="record command is not the next command after TTS",
        ):
            autopilot_ledger.verify_terminal_record_capture(session, record_id)


def test_record_prerequisite_rejects_foreign_device_issue(autopilot_engine):
    with Session(autopilot_engine) as session:
        record_id, _receipt_id = _seed_complete_capture_chain(session)
        session.execute(update(RuntimeCommand).where(
            RuntimeCommand.id == record_id,
        ).values(issued_device_id_hash="device-hash-intruder"))
        session.commit()
        session.expire_all()

        with pytest.raises(
            autopilot_ledger.AutopilotProofError,
            match="record command was issued to another device",
        ):
            autopilot_ledger.verify_terminal_record_capture(session, record_id)


def test_terminal_capture_rejects_non_autonomous_control_state(autopilot_engine):
    # A researcher takeover flips mode away from "autonomous"; a still-pending ACK
    # must not be allowed to finish the command and mint a patient attempt.
    with Session(autopilot_engine) as session:
        record_id, _receipt_id = _seed_complete_capture_chain(session)
        session.execute(update(SessionAutopilotState).where(
            SessionAutopilotState.session_id == "S-PROOF",
        ).values(mode="manual"))
        session.commit()
        session.expire_all()

        with pytest.raises(
            autopilot_ledger.AutopilotProofError,
            match="command scope is no longer autonomous and current",
        ):
            autopilot_ledger.verify_terminal_record_capture(session, record_id)


def test_stale_generation_ack_is_fenced_even_with_matching_audio(autopilot_engine):
    with Session(autopilot_engine) as session:
        record_id, _receipt_id = _seed_complete_capture_chain(session)
        state = session.get(SessionAutopilotState, "S-PROOF")
        state.control_generation = 2
        state.runner_generation = 2
        session.commit()
        with pytest.raises(autopilot_ledger.AutopilotProofError, match="no longer current"):
            autopilot_ledger.verify_record_capture_for_attempt(session, record_id)


def test_processing_attempt_recovery_preserves_capture_generation(autopilot_engine):
    now = datetime(2026, 7, 18, 11, 30, 0)
    with Session(autopilot_engine) as session:
        record_id, _receipt_id = _seed_complete_capture_chain(session)
        state = session.get(SessionAutopilotState, "S-PROOF")
        state.lease_owner = "dead-attempt-worker"
        state.lease_acquired_at = now - timedelta(minutes=1)
        state.lease_expires_at = now - timedelta(seconds=1)
        session.commit()

        # Generic claiming would bump runner_generation and invalidate the command
        # evidence; processing has its own generation-preserving recovery CAS.
        assert autopilot_ledger.try_claim_autopilot_state(
            session, "S-PROOF", owner="wrong-generic-worker",
            expected_revision=state.revision,
            expected_control_generation=state.control_generation,
            now=now,
        ) is False
        assert autopilot_ledger.try_recover_processing_attempt_state(
            session,
            "S-PROOF",
            current_record_command_id=record_id,
            owner="recovered-attempt-worker",
            expected_revision=state.revision,
            expected_control_generation=state.control_generation,
            expected_runner_generation=state.runner_generation,
            now=now,
        ) is True
        session.commit()
        recovered = session.get(SessionAutopilotState, "S-PROOF")
        assert recovered.runner_generation == 1
        assert recovered.lease_owner == "recovered-attempt-worker"
        assert autopilot_ledger.verify_record_capture_for_attempt(
            session, record_id).raw_audio_id == "raw-proof-1"


def test_recovery_ack_requires_command_issued_strictly_before_demotion(
        autopilot_engine):
    with Session(autopilot_engine) as session:
        record_id, _receipt_id = _seed_complete_capture_chain(session)
        record = session.get(RuntimeCommand, record_id)
        capability = session.get(
            PatientDeviceCapability, record.issued_capability_token_hash)
        # Keep the prerequisite TTS ACK strictly before demotion so this test
        # isolates the record command's equality boundary.
        session.execute(update(RuntimeCommand).where(
            RuntimeCommand.id == record.predecessor_command_id,
        ).values(
            issued_at=record.issued_at - timedelta(seconds=2),
            succeeded_at=record.issued_at - timedelta(seconds=1),
        ))
        session.execute(update(RuntimeCommandAck).where(
            RuntimeCommandAck.command_id == record.predecessor_command_id,
            RuntimeCommandAck.ack_type == "tts_ended",
        ).values(received_at=record.issued_at - timedelta(seconds=1)))
        capability.active_session_key = None
        capability.recovery_only_at = record.issued_at
        session.commit()

        with pytest.raises(
                autopilot_ledger.AutopilotProofError,
                match="issued after demotion"):
            autopilot_ledger.verify_record_capture_for_attempt(session, record_id)


def test_persisted_ack_retry_is_idempotent_before_current_generation_gate(
        autopilot_engine):
    with Session(autopilot_engine) as session:
        record_id, _receipt_id = _seed_complete_capture_chain(session)
        existing = session.exec(select(RuntimeCommandAck).where(
            RuntimeCommandAck.command_id == record_id,
            RuntimeCommandAck.ack_type == "record_stopped",
        )).one()
        facts = {
            name: getattr(existing, name)
            for name in (
                "command_id", "idempotency_key", "session_id", "ack_type",
                "command_revision", "control_generation", "runner_generation",
                "device_event_seq", "device_id_hash", "capability_token_hash",
                "payload_json", "receipt_server_seq", "raw_audio_id", "checksum",
                "byte_count", "duration_seconds",
            )
        }
        retry = RuntimeCommandAck(
            **facts, received_at=existing.received_at + timedelta(minutes=5))

        # A later processing worker may legitimately advance the state generation;
        # identical transport retry must still return the first receipt without
        # re-running that current-state gate or its side effects.
        state = session.get(SessionAutopilotState, "S-PROOF")
        state.runner_generation += 1
        session.commit()
        assert autopilot_ledger.resolve_ack_replay(session, retry) == "identical"
        retry.checksum = "b" * 64
        assert autopilot_ledger.resolve_ack_replay(session, retry) == "conflict"


def test_recovery_only_upload_is_limited_to_preallocated_current_record(
        autopilot_engine):
    now = datetime(2026, 7, 18, 12, 0, 0)
    ack_time = now - timedelta(minutes=2)
    with Session(autopilot_engine) as session:
        _seed_session(session, "S-RECOVERY", "P-RECOVERY")
        session.commit()
        capability = PatientDeviceCapability(
            token_hash="cap-hash-recovery",
            session_id="S-RECOVERY",
            device_id_hash="device-hash-recovery",
            active_session_key=None,
            created_at=now - timedelta(minutes=3),
            expires_at=now + timedelta(hours=1),
            recovery_only_at=now - timedelta(minutes=1),
        )
        session.add(capability)
        tts = _command(
            "S-RECOVERY", seq=1, state="succeeded", revision=1,
            succeeded_at=ack_time,
            issued_capability_token_hash=capability.token_hash,
            issued_device_id_hash=capability.device_id_hash,
            issued_at=ack_time - timedelta(seconds=1),
            payload_json=json.dumps({
                "schema_version": 1,
                "speech_key": "question:se_carrot:1:0",
                "speech_text": "请看这张图片，这是什么？",
                "purpose": "question",
                "item_id": "SE_胡萝卜",
                "turn_seq": 1,
                "cue_level": 0,
            }, ensure_ascii=False))
        session.add(tts)
        session.commit()
        session.add(RuntimeCommandAck(
            command_id=tts.id,
            idempotency_key="ack-tts-ended-recovery",
            session_id="S-RECOVERY",
            ack_type="tts_ended",
            command_revision=0,
            control_generation=1,
            runner_generation=1,
            device_event_seq=1,
            device_id_hash=capability.device_id_hash,
            capability_token_hash=capability.token_hash,
            payload_json=autopilot_ledger.encode_ack_payload(
                "tts_ended", {"media_ended": True}),
            received_at=ack_time,
        ))
        audio = _precreated_audio("S-RECOVERY", "raw-recovery-bound")
        session.add(audio)
        session.commit()
        record_payload = json.dumps({
            "schema_version": 1,
            "raw_audio_id": audio.raw_audio_id,
            "turn_key": audio.turn_key,
            "item_id": "SE_胡萝卜",
            "turn_seq": 1,
            "cue_level": 0,
            "max_duration_seconds": 30,
            "contains_direct_identifier": False,
        }, ensure_ascii=False)
        record = _command(
            "S-RECOVERY", seq=2, kind="record",
            issued_capability_token_hash=capability.token_hash,
            issued_device_id_hash=capability.device_id_hash,
            issued_at=ack_time,
            predecessor_command_id=tts.id,
            trigger_ack_idempotency_key="ack-tts-ended-recovery",
            expected_raw_audio_id=audio.raw_audio_id,
            payload_json=record_payload)
        session.add(record)
        session.flush()
        session.add(SessionAutopilotState(
            session_id="S-RECOVERY",
            scope_key="p0a_sim_first_single_v1",
            mode="autonomous",
            status="waiting_recording",
            control_generation=1,
            runner_generation=1,
            revision=4,
            current_command_id=record.id,
        ))
        session.commit()

        authorized = autopilot_ledger.verify_recovery_only_preallocated_upload(
            session,
            raw_audio_id=audio.raw_audio_id,
            capability_token_hash=capability.token_hash,
            device_id_hash=capability.device_id_hash,
            now=now,
        )
        assert authorized.id == record.id
        session.execute(update(RuntimeCommand).where(
            RuntimeCommand.id == record.id,
        ).values(issued_at=capability.recovery_only_at))
        session.commit()
        session.expire_all()
        with pytest.raises(
                autopilot_ledger.AutopilotProofError,
                match="recovery capability is not valid"):
            autopilot_ledger.verify_recovery_only_preallocated_upload(
                session,
                raw_audio_id=audio.raw_audio_id,
                capability_token_hash=capability.token_hash,
                device_id_hash=capability.device_id_hash,
                now=now,
            )
        with pytest.raises(autopilot_ledger.AutopilotProofError, match="not bound"):
            autopilot_ledger.verify_recovery_only_preallocated_upload(
                session,
                raw_audio_id="client-chosen-new-id",
                capability_token_hash=capability.token_hash,
                device_id_hash=capability.device_id_hash,
                now=now,
            )


def _alembic_config(db_path: Path) -> Config:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def test_autopilot_migration_is_additive_and_metadata_current(tmp_path):
    db_path = tmp_path / "autopilot-migration.sqlite"
    config = _alembic_config(db_path)
    command.upgrade(config, "f4b8c1d6a702")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO patient (patient_id, is_simulation_subject) "
            "VALUES ('P-LEGACY-AUTO', 1)"))
        connection.execute(text(
            "INSERT INTO session "
            "(session_id, patient_id, session_sitting_no, week_no, phase_type, "
            "event_line, item_bank_version_id, is_simulation, data_classification) "
            "VALUES ('S-LEGACY-AUTO', 'P-LEGACY-AUTO', 1, 2, '正式训练', "
            "'正式训练', 'wk2-v1-20260707', 1, 'simulation')"))
        connection.execute(text(
            "INSERT INTO patientdevicecapability "
            "(token_hash, session_id, device_id_hash, active_session_key, "
            "created_at, expires_at) VALUES "
            "('cap-legacy-auto', 'S-LEGACY-AUTO', 'device-legacy-auto', "
            "'S-LEGACY-AUTO', '2026-07-18 08:00:00', '2026-07-18 18:00:00')"))

    command.upgrade(config, "head")
    inspector = inspect(engine)
    assert {
        "sessionautopilotstate", "runtimecommand", "runtimecommandack",
        "autopilotcontrolevent",
    }.issubset(inspector.get_table_names())
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT patient_id FROM session WHERE session_id='S-LEGACY-AUTO'"
        )).scalar_one() == "P-LEGACY-AUTO"
        assert connection.execute(text(
            "SELECT COUNT(*) FROM runtimecommand")).scalar_one() == 0
        assert connection.execute(text(
            "SELECT last_autopilot_event_seq FROM patientdevicecapability "
            "WHERE token_hash='cap-legacy-auto'"
        )).scalar_one() == 0
        legacy_binding = connection.execute(text(
            "SELECT item_bank_definition_digest, "
            "autopilot_protocol_version_id, "
            "autopilot_protocol_definition_digest "
            "FROM session WHERE session_id='S-LEGACY-AUTO'"
        )).one()
        assert tuple(legacy_binding) == (None, None, None)
    assert "ck_patient_device_capability_autopilot_seq_nonnegative" in {
        constraint["name"]
        for constraint in inspector.get_check_constraints(
            "patientdevicecapability")
    }

    command.check(config)


def test_zero_to_head_creates_single_current_autopilot_schema(tmp_path):
    db_path = tmp_path / "autopilot-zero-to-head.sqlite"
    config = _alembic_config(db_path)
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == "141bc30e4580"
    command.check(config)


def test_definition_binding_migration_one_step_downgrade_upgrade(tmp_path):
    db_path = tmp_path / "definition-binding-roundtrip.sqlite"
    config = _alembic_config(db_path)
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")

    expected = {
        "item_bank_definition_digest",
        "autopilot_protocol_version_id",
        "autopilot_protocol_definition_digest",
    }
    runtime_expected = expected | {"item_bank_version_id", "response_role"}
    inspector = inspect(engine)
    assert expected <= {
        column["name"] for column in inspector.get_columns("visitplan")}
    assert expected <= {
        column["name"] for column in inspector.get_columns("session")}
    assert runtime_expected <= {
        column["name"] for column in inspector.get_columns("runtimecommand")}

    command.downgrade(config, "a4c8e2f1b703")
    inspector = inspect(engine)
    assert expected.isdisjoint({
        column["name"] for column in inspector.get_columns("visitplan")})
    assert expected.isdisjoint({
        column["name"] for column in inspector.get_columns("session")})
    assert runtime_expected.isdisjoint({
        column["name"] for column in inspector.get_columns("runtimecommand")})

    command.upgrade(config, "head")
    inspector = inspect(engine)
    assert expected <= {
        column["name"] for column in inspector.get_columns("visitplan")}
    assert expected <= {
        column["name"] for column in inspector.get_columns("session")}
    assert runtime_expected <= {
        column["name"] for column in inspector.get_columns("runtimecommand")}
    command.check(config)
