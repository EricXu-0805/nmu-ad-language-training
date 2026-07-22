"""P0a domain-service tests: fail-closed gates and current-command issuance."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
import json
import threading

import pytest
from sqlalchemy import event, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, SQLModel, create_engine, select

from app import autopilot_ledger, autopilot_orchestration, content, evidence_ledger
from app.autopilot_contract import (
    AutopilotAckIn,
    RecordCommandPayload,
    TtsCommandPayload,
)
from app.autopilot_service import (
    P0A_FEATURE_ENV,
    P0A_SCOPE_KEY,
    AutopilotServiceError,
    acknowledge_device_drain,
    apply_device_ack,
    fence_autonomous_scope_for_external_stop,
    get_autopilot_status,
    get_drain_target,
    get_next_command,
    materialize_terminal_attempt_evidence,
    pause_autonomous_scope_for_researcher,
    route_completed_attempt,
    route_tts_ended,
    start_p0a,
    takeover_autopilot_to_manual,
)
from app.enums import EventLine, PhaseType
from app.models import (
    AudioAssetRow,
    AudioCaptureReceipt,
    AttemptEvent,
    AutopilotControlEvent,
    InteractionEvent,
    ItemEvent,
    LiveState,
    Patient,
    PatientDeviceCapability,
    RuntimeCommand,
    RuntimeCommandAck,
    Session as TrainSession,
    SessionAutopilotState,
    SessionRuntimeState,
    TurnEvent,
)


NOW = datetime(2026, 7, 18, 8, 0, 0)
SESSION_ID = "S-P0A-SERVICE"
PATIENT_ID = "P-P0A-SERVICE"
CAPABILITY_HASH = "a" * 64
DEVICE_HASH = "b" * 64
START_KEY = "start-p0a-service-0001"
ACTOR_ID = "RESEARCHER-P0A"

BANK = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")


def _test_subset_meta(position_count: int) -> dict:
    return {
        **BANK.meta,
        "source_protocol_position_count": position_count,
        "source_unstructured_positions": [],
    }


FIRST_ONLY_BANK = replace(
    BANK,
    single_element=[BANK.single_element[0]],
    double_element=[],
    multi_element=[],
    meta=_test_subset_meta(1),
)
TWO_ONLY_BANK = replace(
    BANK,
    single_element=BANK.single_element[:2],
    double_element=[],
    multi_element=[],
    meta=_test_subset_meta(2),
)
GAP_BANK = replace(
    BANK,
    single_element=copy.deepcopy([
        BANK.single_element[0], BANK.single_element[13],
    ]),
    double_element=[],
    multi_element=[],
    meta=_test_subset_meta(2),
)
GAP_BANK.single_element[1]["cues"]["1"]["variants"]["close"]["text"] = ""
PROTOCOL = content.load_autopilot_protocol(
    content.CONTENT_DIR / "autopilot_protocol_v1.json")


@pytest.fixture
def service_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'autopilot-service.sqlite'}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    SQLModel.metadata.create_all(engine)
    return engine


def _enable_p0a(monkeypatch) -> None:
    monkeypatch.setenv(P0A_FEATURE_ENV, "1")
    monkeypatch.setenv("ALLOW_SIMULATION_DATA", "1")


def _seed_ready(
    db: Session,
    *,
    session_id: str = SESSION_ID,
    patient_id: str = PATIENT_ID,
    bank: content.ItemBank = FIRST_ONLY_BANK,
    protocol: dict = PROTOCOL,
) -> None:
    db.add(Patient(
        patient_id=patient_id,
        is_simulation_subject=True,
        consent_status="已同意",
        recording_allowed=True,
    ))
    db.commit()
    db.add(TrainSession(
        session_id=session_id,
        patient_id=patient_id,
        week_no=2,
        phase_type=PhaseType.正式训练,
        event_line=EventLine.正式训练,
        item_bank_version_id=bank.version_id,
        item_bank_definition_digest=content.item_bank_definition_digest(bank),
        autopilot_protocol_version_id=protocol["protocol_version_id"],
        autopilot_protocol_definition_digest=(
            content.autopilot_protocol_definition_digest(protocol)),
        is_simulation=True,
        data_classification="simulation",
    ))
    db.commit()
    db.add(LiveState(
        id=1,
        seq=1,
        session_json=json.dumps({
            "sessionId": session_id,
            "weekNo": 2,
            "eventLine": "正式训练",
            "mode": "task",
            "itemBankVersionId": bank.version_id,
        }, ensure_ascii=False),
        updated_at=NOW,
    ))
    db.add(SessionRuntimeState(
        session_id=session_id,
        status="active",
        revision=0,
        updated_at=NOW,
    ))
    db.add(PatientDeviceCapability(
        token_hash=CAPABILITY_HASH,
        session_id=session_id,
        device_id_hash=DEVICE_HASH,
        active_session_key=session_id,
        created_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        last_seen_at=NOW - timedelta(minutes=1),
    ))
    db.commit()


def _start(db: Session, *, bank: content.ItemBank = FIRST_ONLY_BANK):
    return start_p0a(
        db,
        session_id=SESSION_ID,
        idempotency_key=START_KEY,
        expected_revision=0,
        actor_id=ACTOR_ID,
        bank=bank,
        protocol=PROTOCOL,
        now=NOW,
    )


def _current_tts(db: Session) -> RuntimeCommand:
    state = db.get(SessionAutopilotState, SESSION_ID)
    assert state is not None and state.current_command_id is not None
    command = db.get(RuntimeCommand, state.current_command_id)
    assert command is not None and command.kind == "tts"
    return command


def _filled_protocol_line(value: str) -> str:
    return value.replace("【物品名】", BANK.single_element[0]["target_word"])


def _speech_for(
    purpose: str,
    prompt_level: int,
    response_path: str | None = None,
) -> str:
    if purpose == "question":
        return BANK.single_element[0]["initial_prompt"]
    if purpose == "cue":
        if prompt_level == 1:
            return BANK.single_element[0]["cues"]["1"]["variants"][
                response_path or "unknown"
            ]["text"]
        return BANK.single_element[0]["cues"][str(prompt_level)]["text"]
    if purpose == "feedback":
        if prompt_level == 0:
            return BANK.single_element[0]["success_line"]
        if prompt_level == 1:
            return _filled_protocol_line(
                PROTOCOL["naming"]["success_after_cue1"][
                    response_path or "unknown"
                ])
        return _filled_protocol_line(PROTOCOL["naming"]["success_after_cue2"])
    if purpose == "tell_answer":
        return BANK.single_element[0]["tell_answer"]
    raise AssertionError(f"unsupported test TTS purpose: {purpose}")


def _seed_purpose_tts(
    db: Session,
    *,
    purpose: str,
    prompt_level: int,
    attempt_seq: int,
    response_path: str | None = None,
    speech_override: str | None = None,
    start_event_key: str | None = None,
    start_source: str = "test_seed",
    bank: content.ItemBank = FIRST_ONLY_BANK,
    protocol: dict = PROTOCOL,
) -> RuntimeCommand:
    item = BANK.single_element[0]
    payload_fields = {
        "speech_key": f"p0a.{purpose}.1",
        "speech_text": speech_override or _speech_for(
            purpose, prompt_level, response_path),
        "purpose": purpose,
        "item_id": item["item_id"],
        "turn_seq": 1,
        "cue_level": prompt_level,
    }
    if purpose in {"cue", "feedback"} and prompt_level == 1:
        payload_fields["response_path"] = response_path or "unknown"
    payload = TtsCommandPayload.model_validate(payload_fields)
    command = RuntimeCommand(
        idempotency_key=f"cmd-seeded-{purpose}-{prompt_level}",
        session_id=SESSION_ID,
        command_seq=1,
        item_id=item["item_id"],
        turn_seq=1,
        turn_key=f"{item['item_id']}#1",
        attempt_seq=attempt_seq,
        prompt_level=prompt_level,
        item_bank_version_id=bank.version_id,
        item_bank_definition_digest=content.item_bank_definition_digest(bank),
        autopilot_protocol_version_id=protocol["protocol_version_id"],
        autopilot_protocol_definition_digest=(
            content.autopilot_protocol_definition_digest(protocol)),
        response_role="命名",
        scope_key="p0a_sim_first_single_v1",
        control_generation=1,
        runner_generation=1,
        issued_capability_token_hash=CAPABILITY_HASH,
        issued_device_id_hash=DEVICE_HASH,
        issued_at=NOW,
        kind="tts",
        state="pending",
        payload_json=payload.model_dump_json(exclude_none=True),
        created_at=NOW,
        updated_at=NOW,
    )
    db.add(command)
    db.flush()
    db.add(SessionAutopilotState(
        session_id=SESSION_ID,
        scope_key="p0a_sim_first_single_v1",
        mode="autonomous",
        status="waiting_tts",
        control_generation=1,
        runner_generation=1,
        revision=1,
        next_command_seq=2,
        current_command_id=command.id,
        created_at=NOW,
        updated_at=NOW,
    ))
    db.add(AutopilotControlEvent(
        idempotency_key=(
            start_event_key or f"start-seeded-{purpose}-{prompt_level}"
        ),
        session_id=SESSION_ID,
        event_seq=1,
        event_type="start",
        scope_key="p0a_sim_first_single_v1",
        control_generation=1,
        runner_generation=1,
        command_id=command.id,
        actor_type="researcher",
        actor_id=ACTOR_ID,
        from_mode="disabled",
        to_mode="autonomous",
        from_status="idle",
        to_status="waiting_tts",
        payload_json=autopilot_ledger.encode_control_event_payload(
            "start", {"source": start_source}),
        created_at=NOW,
    ))
    db.commit()
    stored = db.get(RuntimeCommand, command.id)
    assert stored is not None
    return stored


def _complete_tts(
    db: Session,
    command: RuntimeCommand,
    *,
    ack_key: str = "ack-tts-ended-service-0001",
    media_ended: bool = True,
    now: datetime = NOW,
) -> str:
    owner = "test-terminal-ack-worker"
    assert autopilot_ledger.try_claim_runtime_command(
        db,
        command.id,
        owner=owner,
        expected_revision=command.revision,
        control_generation=command.control_generation,
        runner_generation=command.runner_generation,
        now=now,
    )
    db.commit()
    command = db.get(RuntimeCommand, command.id)
    claim = autopilot_ledger.claim_from_runtime_command(command)
    latest_ack = db.exec(select(RuntimeCommandAck).where(
        RuntimeCommandAck.session_id == command.session_id,
        RuntimeCommandAck.device_id_hash == DEVICE_HASH,
    ).order_by(RuntimeCommandAck.device_event_seq.desc())).first()
    next_device_event_seq = (
        latest_ack.device_event_seq + 1 if latest_ack is not None else 1)
    ack = RuntimeCommandAck(
        command_id=command.id,
        idempotency_key=ack_key,
        session_id=command.session_id,
        ack_type="tts_ended",
        command_revision=claim.revision,
        control_generation=command.control_generation,
        runner_generation=command.runner_generation,
        device_event_seq=next_device_event_seq,
        device_id_hash=DEVICE_HASH,
        capability_token_hash=CAPABILITY_HASH,
        payload_json=autopilot_ledger.encode_ack_payload("tts_ended", {
            "media_ended": media_ended,
            "media_duration_ms": 900,
        }),
        received_at=now,
    )
    db.add(ack)
    db.flush()
    assert autopilot_ledger.fenced_command_transition(
        db,
        claim,
        expected_state="pending",
        next_state="succeeded",
        terminal_ack=ack,
        now=now,
    )
    db.commit()
    return ack_key


def _seed_completed_autopilot_attempt(
    db: Session,
    *,
    prompt_level: int = 0,
    operational_answer_type: str = "偏题",
    contains_target: bool = False,
    operational_needs_review: bool = False,
    truth_scope: str = "operational_only",
    initial_answer_type: str = "偏题",
) -> tuple[RuntimeCommand, AttemptEvent]:
    """Build the full immutable question/cue/capture chain through one level."""

    def _complete_current_record(
        record: RuntimeCommand,
        *,
        answer_type: str,
        target_hit: bool,
        needs_review: bool,
        scope: str,
        step_now: datetime,
    ) -> AttemptEvent:
        assert record.expected_raw_audio_id is not None
        checksum = f"{record.prompt_level + 1:x}" * 64
        byte_count = 2048 + record.prompt_level
        asset = db.get(AudioAssetRow, record.expected_raw_audio_id)
        assert asset is not None
        asset.checksum = checksum
        asset.byte_count = byte_count
        asset.uploaded_at = step_now + timedelta(seconds=1)
        receipt = AudioCaptureReceipt(
            raw_audio_id=record.expected_raw_audio_id,
            session_id=SESSION_ID,
            turn_key=record.turn_key,
            received_at=step_now + timedelta(seconds=1),
            duration_seconds=2.5,
            byte_count=byte_count,
            checksum=checksum,
            data_classification="simulation",
            is_simulation=True,
            contains_direct_identifier=False,
        )
        db.add(receipt)
        db.commit()
        assert receipt.server_seq is not None
        latest_ack = db.exec(select(RuntimeCommandAck).where(
            RuntimeCommandAck.session_id == SESSION_ID,
            RuntimeCommandAck.device_id_hash == DEVICE_HASH,
        ).order_by(RuntimeCommandAck.device_event_seq.desc())).first()
        next_device_event_seq = (
            latest_ack.device_event_seq + 1 if latest_ack is not None else 1)
        stopped = AutopilotAckIn(
            idempotency_key=(
                f"ack-attempt-route-record-{record.prompt_level:02d}"
            ),
            ack_type="record_stopped",
            control_generation=record.control_generation,
            runner_generation=record.runner_generation,
            command_revision=record.revision,
            device_event_seq=next_device_event_seq,
            raw_audio_id=record.expected_raw_audio_id,
            receipt_server_seq=receipt.server_seq,
            checksum=checksum,
            byte_count=byte_count,
            duration_seconds=2.5,
            stop_reason="user_done",
        )
        processed = _apply_ack(
            db, record, stopped, now=step_now + timedelta(seconds=2))
        db.commit()
        assert processed.status == "processing_attempt"
        stored_record = db.get(RuntimeCommand, record.id)
        assert stored_record is not None and stored_record.state == "succeeded"

        score = 1.0 if answer_type == "正确" or target_hit else 0.0
        attempt = AttemptEvent(
            session_id=SESSION_ID,
            item_id=stored_record.item_id,
            turn_seq=stored_record.turn_seq,
            response_role="命名",
            attempt_seq=stored_record.attempt_seq,
            raw_audio_id=stored_record.expected_raw_audio_id,
            prompt_level=stored_record.prompt_level,
            cue_type=(
                None if stored_record.prompt_level == 0
                else BANK.single_element[0]["cues"][
                    str(stored_record.prompt_level)
                ]["cue_type"]
            ),
            duration_seconds=2.5,
            asr_text="测试回答",
            asr_confidence=0.91,
            asr_engine_version="test-asr-v1",
            operational_answer_type=answer_type,
            operational_score=score,
            operational_needs_review=needs_review,
            judge_mode="规则确定式",
            judge_engine_version="test-judge-v1",
            judge_reason="test",
            matched_on="target" if target_hit else "none",
            contains_target=target_hit,
            judge_portrait_used=False,
            processing_status="completed",
            processing_generation=1,
            processed_at=step_now + timedelta(seconds=3),
            created_at=step_now + timedelta(seconds=2),
            is_simulation=True,
        )
        db.add(attempt)
        db.flush()
        assert attempt.id is not None
        latest_event = db.exec(select(InteractionEvent).where(
            InteractionEvent.session_id == SESSION_ID,
        ).order_by(InteractionEvent.event_seq.desc())).first()
        db.add(InteractionEvent(
            session_id=SESSION_ID,
            event_seq=(latest_event.event_seq + 1 if latest_event else 1),
            item_id=attempt.item_id,
            turn_seq=attempt.turn_seq,
            attempt_id=attempt.id,
            attempt_seq=attempt.attempt_seq,
            event_type="judgement_completed",
            payload_json=evidence_ledger.encode_event_payload(
                "judgement_completed", {
                    "answer_type": answer_type,
                    "score": score,
                    "needs_review": needs_review,
                    "judge_mode": "规则确定式",
                    "judge_engine_version": "test-judge-v1",
                    "matched_on": attempt.matched_on,
                    "contains_target": target_hit,
                    "truth_scope": scope,
                }),
            created_at=step_now + timedelta(seconds=3),
            is_simulation=True,
        ))
        db.commit()
        stored_attempt = db.get(AttemptEvent, attempt.id)
        assert stored_attempt is not None
        return stored_attempt

    tts = _seed_purpose_tts(
        db, purpose="question", prompt_level=0, attempt_seq=1)
    for level in range(prompt_level + 1):
        step_now = NOW + timedelta(seconds=level * 10)
        ack_key = _complete_tts(
            db,
            tts,
            ack_key=f"ack-attempt-route-tts-{level:02d}",
            now=step_now,
        )
        routed = route_tts_ended(
            db,
            session_id=SESSION_ID,
            command_key=tts.idempotency_key,
            ack_idempotency_key=ack_key,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=step_now + timedelta(seconds=1),
        )
        db.commit()
        assert routed.command is not None and routed.command.kind == "record"
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None and state.current_command_id is not None
        record = db.get(RuntimeCommand, state.current_command_id)
        assert record is not None
        final_level = level == prompt_level
        attempt = _complete_current_record(
            record,
            answer_type=(operational_answer_type if final_level
                         else initial_answer_type),
            target_hit=(contains_target if final_level else False),
            needs_review=(operational_needs_review if final_level else False),
            scope=(truth_scope if final_level else "operational_only"),
            step_now=step_now + timedelta(seconds=1),
        )
        if final_level:
            return record, attempt
        next_route = route_completed_attempt(
            db,
            session_id=SESSION_ID,
            attempt_id=attempt.id,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=step_now + timedelta(seconds=5),
        )
        db.commit()
        assert next_route.command.payload.purpose == "cue"
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None and state.current_command_id is not None
        tts = db.get(RuntimeCommand, state.current_command_id)
        assert tts is not None and tts.kind == "tts"
    raise AssertionError("unreachable prompt-level builder")


def _all_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key for child in value.values() for key in _all_keys(child)
        }
    if isinstance(value, list):
        return {key for child in value for key in _all_keys(child)}
    return set()


def test_p0a_is_disabled_by_default_and_writes_nothing(
    service_engine, monkeypatch,
):
    monkeypatch.delenv(P0A_FEATURE_ENV, raising=False)
    with Session(service_engine) as db:
        _seed_ready(db)
        with pytest.raises(AutopilotServiceError) as caught:
            _start(db)
        assert caught.value.code == "autopilot_p0a_disabled"
        db.rollback()
        assert db.get(SessionAutopilotState, SESSION_ID) is None
        assert list(db.exec(select(RuntimeCommand))) == []
        assert list(db.exec(select(AutopilotControlEvent))) == []


def test_p0a_requires_both_feature_switches(service_engine, monkeypatch):
    monkeypatch.setenv(P0A_FEATURE_ENV, "1")
    monkeypatch.delenv("ALLOW_SIMULATION_DATA", raising=False)
    with Session(service_engine) as db:
        _seed_ready(db)
        with pytest.raises(AutopilotServiceError) as caught:
            _start(db)
        assert caught.value.code == "autopilot_p0a_disabled"


def test_start_is_idempotent_and_uses_raw_bank_initial_prompt(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        created = _start(db)
        db.commit()

        assert created.replayed is False
        assert created.status == "waiting_tts"
        assert created.command is not None
        assert created.command.kind == "tts"
        assert created.command.item_ref == "itm-0001"
        assert "image_id" not in created.command.model_dump()
        assert created.command.payload.speech_text == BANK.single_element[0]["initial_prompt"]
        assert created.command.payload.speech_text != "请看这张图片，这是什么？"
        assert created.command.payload.response_path is None
        state = db.get(SessionAutopilotState, SESSION_ID)
        runtime_state = db.get(SessionRuntimeState, SESSION_ID)
        assert state is not None
        assert (state.scope_key, state.mode, state.status) == (
            "p0a_sim_first_single_v1", "autonomous", "waiting_tts")
        assert (state.control_generation, state.runner_generation) == (1, 1)
        assert state.next_command_seq == 2
        assert runtime_state is not None and runtime_state.status == "active"
        assert runtime_state.intervention_completed_at is None
        issued = db.get(RuntimeCommand, state.current_command_id)
        assert issued is not None
        assert "response_path" not in json.loads(issued.payload_json)
        assert issued.item_bank_version_id == FIRST_ONLY_BANK.version_id
        assert issued.item_bank_definition_digest == (
            content.item_bank_definition_digest(FIRST_ONLY_BANK))
        assert issued.autopilot_protocol_version_id == (
            PROTOCOL["protocol_version_id"])
        assert issued.autopilot_protocol_definition_digest == (
            content.autopilot_protocol_definition_digest(PROTOCOL))
        assert issued.response_role == "命名"

        replay = _start(db)
        db.commit()
        assert replay.replayed is True
        assert replay.command == created.command
        assert len(list(db.exec(select(RuntimeCommand)))) == 1
        assert len(list(db.exec(select(AutopilotControlEvent)))) == 1

        projected = get_next_command(
            db,
            session_id=SESSION_ID,
            capability_token_hash=CAPABILITY_HASH,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW,
        )
        assert projected == created.command
        public = projected.model_dump()
        assert _all_keys(public).isdisjoint({
            "item_id", "image_id", "turn_key", "target_word", "cues", "tell_answer",
            "success_line", "acceptable_expressions",
            "issued_capability_token_hash", "issued_device_id_hash", "issued_at",
        })


def test_session_and_issued_command_fail_closed_on_definition_drift(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)

        legacy = db.get(TrainSession, SESSION_ID)
        assert legacy is not None
        legacy.item_bank_definition_digest = None
        legacy.autopilot_protocol_version_id = None
        legacy.autopilot_protocol_definition_digest = None
        db.add(legacy)
        db.commit()
        with pytest.raises(AutopilotServiceError) as caught:
            _start(db)
        assert caught.value.code == "autopilot_definition_binding_missing"

        legacy.item_bank_definition_digest = content.item_bank_definition_digest(
            FIRST_ONLY_BANK)
        legacy.autopilot_protocol_version_id = PROTOCOL["protocol_version_id"]
        legacy.autopilot_protocol_definition_digest = (
            content.autopilot_protocol_definition_digest(PROTOCOL))
        db.add(legacy)
        db.commit()

        drifted_bank = copy.deepcopy(FIRST_ONLY_BANK)
        drifted_bank.single_element[0]["initial_prompt"] += "（未升版修改）"
        with pytest.raises(AutopilotServiceError) as caught:
            _start(db, bank=drifted_bank)
        assert caught.value.code == "autopilot_content_digest_mismatch"

        drifted_protocol = copy.deepcopy(PROTOCOL)
        drifted_protocol["naming"]["success_after_cue2"] += "（未升版修改）"
        with pytest.raises(AutopilotServiceError) as caught:
            start_p0a(
                db,
                session_id=SESSION_ID,
                idempotency_key=START_KEY,
                expected_revision=0,
                actor_id=ACTOR_ID,
                bank=FIRST_ONLY_BANK,
                protocol=drifted_protocol,
                now=NOW,
            )
        assert caught.value.code == "autopilot_protocol_digest_mismatch"

        created = _start(db)
        db.commit()
        assert created.command is not None
        command = _current_tts(db)

        with pytest.raises(AutopilotServiceError) as caught:
            get_next_command(
                db,
                session_id=SESSION_ID,
                capability_token_hash=CAPABILITY_HASH,
                bank=drifted_bank,
                protocol=PROTOCOL,
                now=NOW,
            )
        assert caught.value.code == "autopilot_content_digest_mismatch"

        ack_key = _complete_tts(
            db, command, ack_key="ack-definition-drift-route-0001")
        with pytest.raises(AutopilotServiceError) as caught:
            route_tts_ended(
                db,
                session_id=SESSION_ID,
                command_key=command.idempotency_key,
                ack_idempotency_key=ack_key,
                bank=FIRST_ONLY_BANK,
                protocol=drifted_protocol,
                now=NOW + timedelta(seconds=1),
            )
        assert caught.value.code == "autopilot_protocol_digest_mismatch"


def test_legacy_runtime_command_binding_is_not_replayed(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _start(db)
        db.commit()
        command = _current_tts(db)
        db.execute(update(RuntimeCommand).where(
            RuntimeCommand.id == command.id,
        ).values(
            item_bank_version_id=None,
            item_bank_definition_digest=None,
            autopilot_protocol_version_id=None,
            autopilot_protocol_definition_digest=None,
            response_role=None,
        ))
        db.commit()

        with pytest.raises(AutopilotServiceError) as caught:
            get_next_command(
                db,
                session_id=SESSION_ID,
                capability_token_hash=CAPABILITY_HASH,
                bank=FIRST_ONLY_BANK,
                protocol=PROTOCOL,
                now=NOW,
            )
        assert caught.value.code == "autopilot_command_binding_missing"


def test_start_rejects_default_week2_plan_before_taking_control(
        service_engine, monkeypatch):
    """All structured and source-only gaps are counted before ownership."""
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db, bank=BANK)
        with pytest.raises(AutopilotServiceError) as caught:
            _start(db, bank=BANK)
        assert caught.value.code == "autopilot_plan_not_fully_supported"
        assert caught.value.context == {
            "unsupported_position_count": 60,
            "structured_unsupported_position_count": 50,
            "source_unstructured_position_count": 10,
            "source_protocol_position_count": 80,
            "first_gap": {
                "code": "operational_protocol_unavailable",
                "item_id": "DE_烟灰缸+烟",
                "turn_seq": 1,
                "response_role": "左命名",
            },
        }
        db.rollback()
        assert db.get(SessionAutopilotState, SESSION_ID) is None
        assert list(db.exec(select(RuntimeCommand))) == []
        assert list(db.exec(select(AutopilotControlEvent))) == []


@pytest.mark.parametrize("mutation", [
    "missing_inventory", "wrong_source_count", "invalid_unstructured_row",
])
def test_start_fails_closed_on_inconsistent_source_protocol_inventory(
    service_engine, monkeypatch, mutation,
):
    _enable_p0a(monkeypatch)
    broken_bank = copy.deepcopy(FIRST_ONLY_BANK)
    if mutation == "missing_inventory":
        broken_bank.meta.pop("source_unstructured_positions")
    elif mutation == "wrong_source_count":
        broken_bank.meta["source_protocol_position_count"] = 80
    else:
        broken_bank.meta["source_protocol_position_count"] = 2
        broken_bank.meta["source_unstructured_positions"] = [{
            "source_position_key": "week2:multi:test#1",
            "response_role": "情境识别",
            "source_paragraphs": [],
            "status": "awaiting_content_decision",
        }]

    with Session(service_engine) as db:
        _seed_ready(db, bank=broken_bank)
        with pytest.raises(AutopilotServiceError) as caught:
            _start(db, bank=broken_bank)
        assert caught.value.code == (
            "autopilot_source_protocol_inventory_invalid")
        db.rollback()
        assert db.get(SessionAutopilotState, SESSION_ID) is None
        assert list(db.exec(select(RuntimeCommand))) == []


def test_fresh_start_rejects_preexisting_manual_attempt_evidence(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    raw_audio_id = "raw-manual-before-p0a-0001"
    with Session(service_engine) as db:
        _seed_ready(db)
        db.add(AudioAssetRow(
            raw_audio_id=raw_audio_id,
            session_id=SESSION_ID,
            is_simulation=True,
            data_classification="simulation",
            turn_key=f"{BANK.single_element[0]['item_id']}#1",
            status="recorded",
        ))
        db.flush()
        db.add(AttemptEvent(
            session_id=SESSION_ID,
            item_id=BANK.single_element[0]["item_id"],
            turn_seq=1,
            response_role="命名",
            attempt_seq=1,
            raw_audio_id=raw_audio_id,
            prompt_level=0,
            duration_seconds=1.0,
            processing_status="completed",
            processing_generation=1,
            processed_at=NOW,
            created_at=NOW,
            is_simulation=True,
        ))
        db.commit()

        with pytest.raises(AutopilotServiceError) as caught:
            _start(db)
        assert caught.value.code == "autopilot_existing_manual_evidence"
        db.rollback()

        assert db.get(SessionAutopilotState, SESSION_ID) is None
        assert list(db.exec(select(RuntimeCommand))) == []
        assert list(db.exec(select(AutopilotControlEvent))) == []
        assert len(list(db.exec(select(AttemptEvent)))) == 1


def test_account_status_is_minimal_and_preserves_server_ownership(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        disabled = get_autopilot_status(db, session_id=SESSION_ID)
        assert disabled.model_dump() == {
            "scope_key": "disabled",
            "mode": "disabled",
            "status": "idle",
            "state_revision": 0,
            "server_owned": False,
            "current_command_kind": None,
            "last_error_code": None,
        }

        _start(db)
        db.commit()
        active = get_autopilot_status(db, session_id=SESSION_ID)
        assert active.model_dump() == {
            "scope_key": "p0a_sim_first_single_v1",
            "mode": "autonomous",
            "status": "waiting_tts",
            "state_revision": 1,
            "server_owned": True,
            "current_command_kind": "tts",
            "last_error_code": None,
        }
        assert _all_keys(active.model_dump()).isdisjoint({
            "command", "payload", "speech_text", "image_id", "item_ref",
            "item_id", "turn_key", "target_word", "answer",
            "issued_capability_token_hash", "issued_device_id_hash",
        })

        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None
        state.mode = "manual"
        db.commit()
        taken_over = get_autopilot_status(db, session_id=SESSION_ID)
        assert taken_over.mode == "manual"
        assert taken_over.server_owned is False
        assert taken_over.current_command_kind == "tts"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("research_session", "autopilot_simulation_required"),
        ("wrong_classification", "autopilot_classification_invalid"),
        ("plain_patient", "autopilot_simulation_subject_required"),
        ("consent_denied", "autopilot_consent_denied"),
        ("recording_unknown", "autopilot_recording_not_allowed"),
        ("recording_denied", "autopilot_recording_not_allowed"),
        ("withdrawn", "autopilot_subject_withdrawn"),
        ("wrong_week", "autopilot_live_session_mismatch"),
        ("wrong_phase", "autopilot_scope_unsupported"),
        ("live_foreign", "autopilot_live_session_mismatch"),
        ("runtime_missing", "autopilot_runtime_inactive"),
        ("runtime_paused", "autopilot_runtime_inactive"),
        ("device_missing", "autopilot_device_not_paired"),
        ("device_expired", "autopilot_device_not_active"),
        ("device_not_yet_valid", "autopilot_device_not_active"),
        ("device_recovery_only", "autopilot_device_not_paired"),
    ],
)
def test_start_gate_fails_closed(
    service_engine, monkeypatch, mutation, expected_code,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        patient = db.get(Patient, PATIENT_ID)
        train_session = db.get(TrainSession, SESSION_ID)
        live = db.get(LiveState, 1)
        runtime_state = db.get(SessionRuntimeState, SESSION_ID)
        capability = db.get(PatientDeviceCapability, CAPABILITY_HASH)
        assert all(row is not None for row in (
            patient, train_session, live, runtime_state, capability))

        if mutation == "research_session":
            train_session.is_simulation = False
            train_session.data_classification = "research"
        elif mutation == "wrong_classification":
            train_session.data_classification = "research"
        elif mutation == "plain_patient":
            patient.is_simulation_subject = False
        elif mutation == "consent_denied":
            patient.consent_status = "拒绝"
        elif mutation == "recording_unknown":
            patient.recording_allowed = None
        elif mutation == "recording_denied":
            patient.recording_allowed = False
        elif mutation == "withdrawn":
            patient.withdrawal_status = "withdrawn"
        elif mutation == "wrong_week":
            train_session.week_no = 3
        elif mutation == "wrong_phase":
            train_session.phase_type = PhaseType.基线测评
        elif mutation == "live_foreign":
            payload = json.loads(live.session_json)
            payload["sessionId"] = "S-OTHER"
            live.session_json = json.dumps(payload)
        elif mutation == "runtime_missing":
            db.delete(runtime_state)
        elif mutation == "runtime_paused":
            runtime_state.status = "paused"
        elif mutation == "device_missing":
            db.delete(capability)
        elif mutation == "device_expired":
            capability.expires_at = NOW
        elif mutation == "device_not_yet_valid":
            capability.created_at = NOW + timedelta(seconds=1)
        elif mutation == "device_recovery_only":
            capability.active_session_key = None
            capability.recovery_only_at = NOW
        db.commit()

        with pytest.raises(AutopilotServiceError) as caught:
            _start(db)
        assert caught.value.code == expected_code
        db.rollback()
        assert db.get(SessionAutopilotState, SESSION_ID) is None
        assert list(db.exec(select(RuntimeCommand))) == []


@pytest.mark.parametrize("missing", [
    "target_word", "image_id", "initial_prompt", "success_line", "tell_answer",
])
def test_start_rejects_incomplete_selected_item(
    service_engine, monkeypatch, missing,
):
    _enable_p0a(monkeypatch)
    broken_bank = copy.deepcopy(FIRST_ONLY_BANK)
    broken_bank.single_element[0][missing] = None
    with Session(service_engine) as db:
        _seed_ready(db, bank=broken_bank)
        with pytest.raises(AutopilotServiceError) as caught:
            start_p0a(
                db,
                session_id=SESSION_ID,
                idempotency_key=START_KEY,
                expected_revision=0,
                actor_id=ACTOR_ID,
                bank=broken_bank,
                protocol=PROTOCOL,
                now=NOW,
            )
        assert caught.value.code == "autopilot_plan_not_fully_supported"
        assert caught.value.context["unsupported_position_count"] == 1
        assert caught.value.context["first_gap"]["item_id"] == (
            BANK.single_element[0]["item_id"])


@pytest.mark.parametrize("cue_level", ["1", "2"])
def test_start_rejects_missing_selected_cue(
    service_engine, monkeypatch, cue_level,
):
    _enable_p0a(monkeypatch)
    broken_bank = copy.deepcopy(FIRST_ONLY_BANK)
    broken_bank.single_element[0]["cues"][cue_level]["text"] = ""
    with Session(service_engine) as db:
        _seed_ready(db, bank=broken_bank)
        with pytest.raises(AutopilotServiceError) as caught:
            start_p0a(
                db,
                session_id=SESSION_ID,
                idempotency_key=START_KEY,
                expected_revision=0,
                actor_id=ACTOR_ID,
                bank=broken_bank,
                protocol=PROTOCOL,
                now=NOW,
            )
        assert caught.value.code == "autopilot_plan_not_fully_supported"
        assert caught.value.context["unsupported_position_count"] == 1
        assert caught.value.context["first_gap"]["item_id"] == (
            BANK.single_element[0]["item_id"])


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing_branch", "autopilot_plan_not_fully_supported"),
        ("extra_branch", "autopilot_content_incomplete"),
        ("duplicate_source", "autopilot_content_incomplete"),
        ("legacy_drift", "autopilot_content_incomplete"),
    ],
)
def test_start_requires_exact_source_bound_cue1_variants(
    service_engine, monkeypatch, mutation, expected_code,
):
    _enable_p0a(monkeypatch)
    broken_bank = copy.deepcopy(FIRST_ONLY_BANK)
    cue1 = broken_bank.single_element[0]["cues"]["1"]
    if mutation == "missing_branch":
        cue1["variants"].pop("silence")
    elif mutation == "extra_branch":
        cue1["variants"]["invented"] = {
            "text": "客户端自造分支",
            "source_paragraph_index": 9999,
        }
    elif mutation == "duplicate_source":
        cue1["variants"]["close"]["source_paragraph_index"] = (
            cue1["variants"]["unknown"]["source_paragraph_index"])
    else:
        cue1["text"] = cue1["variants"]["close"]["text"]

    with Session(service_engine) as db:
        _seed_ready(db, bank=broken_bank)
        with pytest.raises(AutopilotServiceError) as caught:
            start_p0a(
                db,
                session_id=SESSION_ID,
                idempotency_key=START_KEY,
                expected_revision=0,
                actor_id=ACTOR_ID,
                bank=broken_bank,
                protocol=PROTOCOL,
                now=NOW,
            )
        assert caught.value.code == expected_code
        db.rollback()
        assert db.get(SessionAutopilotState, SESSION_ID) is None


def test_start_rejects_invalid_autopilot_protocol(service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    broken_protocol = copy.deepcopy(PROTOCOL)
    broken_protocol["naming"]["success_after_cue2"] = ""
    with Session(service_engine) as db:
        _seed_ready(db)
        with pytest.raises(AutopilotServiceError) as caught:
            start_p0a(
                db,
                session_id=SESSION_ID,
                idempotency_key=START_KEY,
                expected_revision=0,
                actor_id=ACTOR_ID,
                bank=FIRST_ONLY_BANK,
                protocol=broken_protocol,
                now=NOW,
            )
        assert caught.value.code == "autopilot_protocol_invalid"


@pytest.mark.parametrize(
    ("purpose", "prompt_level", "attempt_seq"),
    [("question", 0, 1), ("cue", 1, 2), ("cue", 2, 3)],
)
def test_question_or_cue_end_preallocates_exact_record_command(
    service_engine, monkeypatch, purpose, prompt_level, attempt_seq,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        command = _seed_purpose_tts(
            db,
            purpose=purpose,
            prompt_level=prompt_level,
            attempt_seq=attempt_seq,
        )
        ack_key = _complete_tts(db, command)

        routed = route_tts_ended(
            db,
            session_id=SESSION_ID,
            command_key=command.idempotency_key,
            ack_idempotency_key=ack_key,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW,
        )
        db.commit()
        assert routed.status == "waiting_recording"
        assert routed.replayed is False
        assert routed.command is not None and routed.command.kind == "record"
        assert routed.command.attempt_seq == attempt_seq
        assert routed.command.prompt_level == prompt_level
        public = routed.command.model_dump()
        assert _all_keys(public).isdisjoint({
            "item_id", "image_id", "turn_key", "target_word", "cues", "tell_answer",
            "issued_capability_token_hash", "issued_device_id_hash", "issued_at",
        })
        source_tts = TtsCommandPayload.model_validate_json(command.payload_json)
        assert public["payload"]["presentation_speech_key"] == source_tts.speech_key
        assert public["payload"]["presentation_speech_text"] == source_tts.speech_text
        assert public["payload"]["presentation_purpose"] == purpose

        commands = list(db.exec(select(RuntimeCommand).order_by(
            RuntimeCommand.command_seq)))
        assert len(commands) == 2
        record = commands[1]
        assert record.predecessor_command_id == command.id
        assert record.trigger_ack_idempotency_key == ack_key
        assert record.expected_raw_audio_id
        assert record.issued_capability_token_hash == CAPABILITY_HASH
        assert record.issued_device_id_hash == DEVICE_HASH
        assert record.issued_at == NOW
        payload = RecordCommandPayload.model_validate_json(record.payload_json)
        assert payload.raw_audio_id == record.expected_raw_audio_id
        assert payload.turn_key == record.turn_key
        assert payload.max_duration_seconds == PROTOCOL["silence_seconds"] + 5
        asset = db.get(AudioAssetRow, record.expected_raw_audio_id)
        assert asset is not None
        assert (asset.session_id, asset.turn_key) == (SESSION_ID, record.turn_key)
        assert asset.is_simulation is True
        assert asset.data_classification == "simulation"
        assert asset.checksum is None and asset.byte_count is None
        assert asset.uploaded_at is None
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None
        assert state.status == "waiting_recording"
        assert state.current_command_id == record.id

        replay = route_tts_ended(
            db,
            session_id=SESSION_ID,
            command_key=command.idempotency_key,
            ack_idempotency_key=ack_key,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW,
        )
        db.commit()
        assert replay.replayed is True
        assert replay.command == routed.command
        assert len(list(db.exec(select(RuntimeCommand)))) == 2
        assert len(list(db.exec(select(AudioAssetRow)))) == 1

        next_command = get_next_command(
            db,
            session_id=SESSION_ID,
            capability_token_hash=CAPABILITY_HASH,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW,
        )
        assert next_command == routed.command


@pytest.mark.parametrize(
    ("purpose", "prompt_level", "attempt_seq"),
    [("feedback", 0, 1), ("tell_answer", 3, 3)],
)
def test_terminal_speech_completes_only_p0a_scope(
    service_engine, monkeypatch, purpose, prompt_level, attempt_seq,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        command = _seed_purpose_tts(
            db,
            purpose=purpose,
            prompt_level=prompt_level,
            attempt_seq=attempt_seq,
        )
        ack_key = _complete_tts(db, command)

        routed = route_tts_ended(
            db,
            session_id=SESSION_ID,
            command_key=command.idempotency_key,
            ack_idempotency_key=ack_key,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW,
        )
        db.commit()
        assert routed.status == "scope_completed"
        assert routed.command is None
        assert list(db.exec(select(AudioAssetRow))) == []
        assert len(list(db.exec(select(RuntimeCommand)))) == 1

        state = db.get(SessionAutopilotState, SESSION_ID)
        runtime_state = db.get(SessionRuntimeState, SESSION_ID)
        assert state is not None
        assert state.status == "scope_completed"
        assert state.current_command_id is None
        assert runtime_state is not None
        assert runtime_state.status == "active"
        assert runtime_state.intervention_completed_at is None
        assert runtime_state.intervention_ended_by is None
        assert runtime_state.completed_at is None
        events = list(db.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))
        assert [event.event_type for event in events] == ["start", "scope_complete"]
        assert all(event.event_type != "intervention_complete" for event in events)

        replay = route_tts_ended(
            db,
            session_id=SESSION_ID,
            command_key=command.idempotency_key,
            ack_idempotency_key=ack_key,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW,
        )
        db.commit()
        assert replay.replayed is True
        assert len(list(db.exec(select(AutopilotControlEvent)))) == 2
        assert get_next_command(
            db,
            session_id=SESSION_ID,
            capability_token_hash=CAPABILITY_HASH,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW,
        ) is None


def test_terminal_feedback_advances_to_exact_next_frozen_position(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db, bank=TWO_ONLY_BANK)
        feedback = _seed_purpose_tts(
            db,
            purpose="feedback",
            prompt_level=0,
            attempt_seq=1,
            start_event_key=START_KEY,
            start_source="p0a_domain_service",
            bank=TWO_ONLY_BANK,
        )
        ack_key = _complete_tts(
            db, feedback, ack_key="ack-next-position-service-0001")

        advanced = route_tts_ended(
            db,
            session_id=SESSION_ID,
            command_key=feedback.idempotency_key,
            ack_idempotency_key=ack_key,
            bank=TWO_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW + timedelta(seconds=1),
        )
        db.commit()

        assert advanced.status == "waiting_tts"
        assert advanced.replayed is False
        assert advanced.command is not None
        assert advanced.command.item_ref == "itm-0002"
        assert "image_id" not in advanced.command.model_dump()
        assert advanced.command.turn_seq == 1
        assert advanced.command.attempt_seq == 1
        assert advanced.command.prompt_level == 0
        assert advanced.command.payload.purpose == "question"
        assert (
            advanced.command.payload.speech_text
            == BANK.single_element[1]["initial_prompt"]
        )
        commands = list(db.exec(select(RuntimeCommand).order_by(
            RuntimeCommand.command_seq)))
        assert len(commands) == 2
        successor = commands[1]
        assert successor.item_id == BANK.single_element[1]["item_id"]
        assert successor.predecessor_command_id is None
        assert successor.trigger_ack_idempotency_key is None

        replay = route_tts_ended(
            db,
            session_id=SESSION_ID,
            command_key=feedback.idempotency_key,
            ack_idempotency_key=ack_key,
            bank=TWO_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW + timedelta(seconds=2),
        )
        db.commit()
        assert replay.replayed is True
        assert replay.command == advanced.command
        assert len(list(db.exec(select(RuntimeCommand)))) == 2

        start_replay = _start(db, bank=TWO_ONLY_BANK)
        db.commit()
        assert start_replay.replayed is True
        assert start_replay.command == advanced.command
        assert len(list(db.exec(select(RuntimeCommand)))) == 2


def test_next_incomplete_position_pauses_without_skipping_and_requires_drain(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db, bank=GAP_BANK)
        feedback = _seed_purpose_tts(
            db, purpose="feedback", prompt_level=0, attempt_seq=1,
            bank=GAP_BANK)
        ack_key = _complete_tts(
            db, feedback, ack_key="ack-position-gap-service-0001")

        blocked = route_tts_ended(
            db,
            session_id=SESSION_ID,
            command_key=feedback.idempotency_key,
            ack_idempotency_key=ack_key,
            bank=GAP_BANK,
            protocol=PROTOCOL,
            now=NOW + timedelta(seconds=1),
        )
        db.commit()
        assert blocked.status == "paused"
        assert blocked.replayed is False and blocked.command is None
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None
        assert state.status == "paused"
        assert state.current_command_id is None
        assert state.last_error_code == "source_field_unavailable"
        assert len(list(db.exec(select(RuntimeCommand)))) == 1
        latest = list(db.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))[-1]
        assert latest.event_type == "pause"
        assert latest.actor_type == "system"
        assert json.loads(latest.payload_json) == {
            "reason_code": "source_field_unavailable",
            "source": "protocol_position_gap",
        }

        replay = route_tts_ended(
            db,
            session_id=SESSION_ID,
            command_key=feedback.idempotency_key,
            ack_idempotency_key=ack_key,
            bank=GAP_BANK,
            protocol=PROTOCOL,
            now=NOW + timedelta(seconds=2),
        )
        db.commit()
        assert replay.status == "paused" and replay.replayed is True
        assert len(list(db.exec(select(AutopilotControlEvent)))) == 2

        target = get_drain_target(
            db,
            session_id=SESSION_ID,
            capability_token_hash=CAPABILITY_HASH,
        )
        assert target.command_key == feedback.idempotency_key
        drained = acknowledge_device_drain(
            db,
            session_id=SESSION_ID,
            command_key=feedback.idempotency_key,
            capability_token_hash=CAPABILITY_HASH,
            now=NOW + timedelta(seconds=3),
        )
        db.commit()
        assert drained.replayed is False
        manual = takeover_autopilot_to_manual(
            db,
            session_id=SESSION_ID,
            idempotency_key="takeover-position-gap-service-0001",
            expected_revision=drained.state_revision,
            actor_id=ACTOR_ID,
            now=NOW + timedelta(seconds=4),
        )
        db.commit()
        assert manual.mode == "manual"


def test_tts_end_without_positive_persisted_media_fact_creates_nothing(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _start(db)
        command = _current_tts(db)
        db.commit()
        ack_key = _complete_tts(db, command, media_ended=False)

        with pytest.raises(AutopilotServiceError) as caught:
            route_tts_ended(
                db,
                session_id=SESSION_ID,
                command_key=command.idempotency_key,
                ack_idempotency_key=ack_key,
                bank=FIRST_ONLY_BANK,
                protocol=PROTOCOL,
                now=NOW,
            )
        assert caught.value.code == "autopilot_tts_ack_invalid"
        db.rollback()
        assert list(db.exec(select(AudioAssetRow))) == []
        assert len(list(db.exec(select(RuntimeCommand)))) == 1
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None and state.status == "waiting_tts"


def test_next_command_requires_exact_active_capability(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _start(db)
        db.commit()
        with pytest.raises(AutopilotServiceError) as caught:
            get_next_command(
                db,
                session_id=SESSION_ID,
                capability_token_hash="f" * 64,
                bank=FIRST_ONLY_BANK,
                protocol=PROTOCOL,
                now=NOW,
            )
        assert caught.value.code == "autopilot_device_not_paired"


def test_tts_purpose_cannot_relabel_an_allowlisted_line(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _seed_purpose_tts(
            db,
            purpose="feedback",
            prompt_level=0,
            attempt_seq=1,
            speech_override=BANK.single_element[0]["initial_prompt"],
        )
        with pytest.raises(AutopilotServiceError) as caught:
            get_next_command(
                db,
                session_id=SESSION_ID,
                capability_token_hash=CAPABILITY_HASH,
                bank=FIRST_ONLY_BANK,
                protocol=PROTOCOL,
                now=NOW,
            )
        assert caught.value.code == "autopilot_command_invalid"


def test_device_rotation_after_tts_ack_never_opens_microphone_on_new_device(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        command = _seed_purpose_tts(
            db, purpose="question", prompt_level=0, attempt_seq=1)
        ack_key = _complete_tts(db, command)

        original = db.get(PatientDeviceCapability, CAPABILITY_HASH)
        assert original is not None
        original.active_session_key = None
        original.recovery_only_at = NOW + timedelta(milliseconds=1)
        db.add(PatientDeviceCapability(
            token_hash="c" * 64,
            session_id=SESSION_ID,
            device_id_hash="d" * 64,
            active_session_key=SESSION_ID,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        ))
        db.commit()

        with pytest.raises(AutopilotServiceError) as caught:
            route_tts_ended(
                db,
                session_id=SESSION_ID,
                command_key=command.idempotency_key,
                ack_idempotency_key=ack_key,
                bank=FIRST_ONLY_BANK,
                protocol=PROTOCOL,
                now=NOW + timedelta(seconds=1),
            )
        assert caught.value.code == "autopilot_command_device_rotated"
        db.rollback()
        assert list(db.exec(select(AudioAssetRow))) == []
        assert len(list(db.exec(select(RuntimeCommand)))) == 1


def _apply_ack(
    db: Session,
    command: RuntimeCommand,
    ack: AutopilotAckIn,
    *,
    capability_hash: str = CAPABILITY_HASH,
    now: datetime = NOW,
    bank: content.ItemBank = FIRST_ONLY_BANK,
):
    return apply_device_ack(
        db,
        session_id=SESSION_ID,
        command_key=command.idempotency_key,
        capability_token_hash=capability_hash,
        ack=ack,
        bank=bank,
        protocol=PROTOCOL,
        now=now,
    )


def test_apply_ack_revalidates_media_fact_before_any_write(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _start(db)
        db.commit()
        command = _current_tts(db)
        invalid = AutopilotAckIn.model_construct(
            idempotency_key="ack-invalid-media-0001",
            ack_type="tts_ended",
            control_generation=1,
            runner_generation=1,
            command_revision=0,
            device_event_seq=1,
            media_ended=False,
        )
        with pytest.raises(AutopilotServiceError) as caught:
            _apply_ack(db, command, invalid)
        assert caught.value.code == "autopilot_ack_invalid"
        db.rollback()

        assert list(db.exec(select(RuntimeCommandAck))) == []
        assert list(db.exec(select(AudioAssetRow))) == []
        assert db.get(PatientDeviceCapability, CAPABILITY_HASH).last_autopilot_event_seq == 0
        stored = db.get(RuntimeCommand, command.id)
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert stored is not None and (stored.state, stored.revision) == ("pending", 0)
        assert state is not None and state.status == "waiting_tts"


def test_apply_tts_ack_happy_replay_conflict_and_lease_takeover(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _start(db)
        db.commit()
        command = _current_tts(db)

        # A live foreign lease is a stable busy conflict, never authenticity.
        db.execute(update(RuntimeCommand).where(
            RuntimeCommand.id == command.id,
        ).values(
            lease_owner="old-worker",
            lease_expires_at=NOW + timedelta(seconds=10),
        ))
        db.commit()
        started = AutopilotAckIn(
            idempotency_key="ack-tts-started-apply-0001",
            ack_type="tts_started",
            control_generation=1,
            runner_generation=1,
            command_revision=0,
            device_event_seq=1,
            media_duration_ms=900,
        )
        with pytest.raises(AutopilotServiceError) as caught:
            _apply_ack(db, command, started)
        assert caught.value.code == "autopilot_ack_cas_conflict"
        db.rollback()
        assert list(db.exec(select(RuntimeCommandAck))) == []
        assert db.get(PatientDeviceCapability, CAPABILITY_HASH).last_autopilot_event_seq == 0

        # Once expired, the ACK transaction takes its own lease and releases it
        # after the nonterminal started CAS.
        db.execute(update(RuntimeCommand).where(
            RuntimeCommand.id == command.id,
        ).values(lease_expires_at=NOW - timedelta(seconds=1)))
        db.commit()
        accepted = _apply_ack(db, command, started)
        db.commit()
        assert accepted.replayed is False
        assert (accepted.command_state, accepted.command_revision) == ("started", 1)
        stored = db.get(RuntimeCommand, command.id)
        assert stored is not None
        assert stored.lease_owner is None and stored.lease_expires_at is None

        replay = _apply_ack(db, stored, started, now=NOW + timedelta(seconds=1))
        db.commit()
        assert replay.replayed is True and replay.command is None
        assert len(list(db.exec(select(RuntimeCommandAck)))) == 1
        assert db.get(PatientDeviceCapability, CAPABILITY_HASH).last_autopilot_event_seq == 1

        conflict = started.model_copy(update={
            "device_event_seq": 2,
            "media_duration_ms": 901,
        })
        with pytest.raises(AutopilotServiceError) as caught:
            _apply_ack(db, stored, conflict, now=NOW + timedelta(seconds=1))
        assert caught.value.code == "autopilot_ack_conflict"
        db.rollback()

        ended = AutopilotAckIn(
            idempotency_key="ack-tts-ended-apply-0001",
            ack_type="tts_ended",
            control_generation=1,
            runner_generation=1,
            command_revision=1,
            device_event_seq=2,
            media_ended=True,
            media_duration_ms=900,
        )
        routed = _apply_ack(
            db, db.get(RuntimeCommand, command.id), ended,
            now=NOW + timedelta(seconds=1))
        db.commit()
        assert routed.replayed is False
        assert (routed.command_state, routed.command_revision) == ("succeeded", 2)
        assert routed.status == "waiting_recording"
        assert routed.command is not None and routed.command.kind == "record"
        assert len(list(db.exec(select(RuntimeCommandAck)))) == 2
        assert len(list(db.exec(select(AudioAssetRow)))) == 1
        assert db.get(PatientDeviceCapability, CAPABILITY_HASH).last_autopilot_event_seq == 2

        ended_replay = _apply_ack(
            db, db.get(RuntimeCommand, command.id), ended,
            now=NOW + timedelta(seconds=2))
        db.commit()
        assert ended_replay.replayed is True and ended_replay.command is None
        assert len(list(db.exec(select(RuntimeCommand)))) == 2
        assert len(list(db.exec(select(AudioAssetRow)))) == 1


def test_apply_ack_rejects_out_of_order_device_sequence_atomically(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _start(db)
        db.commit()
        command = _current_tts(db)
        started = AutopilotAckIn(
            idempotency_key="ack-seq-started-0001",
            ack_type="tts_started",
            control_generation=1,
            runner_generation=1,
            command_revision=0,
            device_event_seq=2,
        )
        _apply_ack(db, command, started)
        db.commit()

        ended = AutopilotAckIn(
            idempotency_key="ack-seq-ended-000001",
            ack_type="tts_ended",
            control_generation=1,
            runner_generation=1,
            command_revision=1,
            device_event_seq=1,
            media_ended=True,
        )
        with pytest.raises(AutopilotServiceError) as caught:
            _apply_ack(
                db, db.get(RuntimeCommand, command.id), ended,
                now=NOW + timedelta(seconds=1))
        assert caught.value.code == "autopilot_ack_sequence_invalid"
        db.rollback()

        stored = db.get(RuntimeCommand, command.id)
        assert stored is not None
        assert (stored.state, stored.revision) == ("started", 1)
        assert stored.lease_owner is None
        assert len(list(db.exec(select(RuntimeCommandAck)))) == 1
        assert db.get(PatientDeviceCapability, CAPABILITY_HASH).last_autopilot_event_seq == 2
        assert db.get(SessionAutopilotState, SESSION_ID).status == "waiting_tts"


def test_tts_ack_and_followup_route_roll_back_as_one_transaction(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _start(db)
        db.commit()
        command = _current_tts(db)

        def _reject_route_proof(*_args, **_kwargs):
            raise autopilot_ledger.AutopilotProofError("injected route failure")

        monkeypatch.setattr(
            autopilot_ledger,
            "verify_tts_ended_prerequisite",
            _reject_route_proof,
        )
        ended = AutopilotAckIn(
            idempotency_key="ack-route-rollback-0001",
            ack_type="tts_ended",
            control_generation=1,
            runner_generation=1,
            command_revision=0,
            device_event_seq=1,
            media_ended=True,
        )
        with pytest.raises(AutopilotServiceError) as caught:
            _apply_ack(db, command, ended)
        assert caught.value.code == "autopilot_tts_ack_invalid"
        db.rollback()

        stored = db.get(RuntimeCommand, command.id)
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert stored is not None
        assert (stored.state, stored.revision) == ("pending", 0)
        assert stored.lease_owner is None
        assert state is not None and state.status == "waiting_tts"
        assert list(db.exec(select(RuntimeCommandAck))) == []
        assert list(db.exec(select(AudioAssetRow))) == []
        assert len(list(db.exec(select(RuntimeCommand)))) == 1
        assert db.get(PatientDeviceCapability, CAPABILITY_HASH).last_autopilot_event_seq == 0


def test_apply_failure_ack_pauses_with_append_only_control_fact(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _start(db)
        db.commit()
        command = _current_tts(db)
        failed = AutopilotAckIn(
            idempotency_key="ack-tts-failed-apply-0001",
            ack_type="tts_failed",
            control_generation=1,
            runner_generation=1,
            command_revision=0,
            device_event_seq=1,
            error_code="audio_playback_failed",
        )
        result = _apply_ack(db, command, failed)
        db.commit()
        assert result.status == "paused"
        assert (result.command_state, result.command_revision) == ("failed", 1)

        state = db.get(SessionAutopilotState, SESSION_ID)
        stored = db.get(RuntimeCommand, command.id)
        runtime_state = db.get(SessionRuntimeState, SESSION_ID)
        assert state is not None
        assert state.status == "paused" and state.current_command_id is None
        assert state.last_error_code == "audio_playback_failed"
        assert stored is not None
        assert json.loads(stored.result_json) == {
            "error_code": "audio_playback_failed",
        }
        assert runtime_state is not None and runtime_state.status == "active"
        events = list(db.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))
        assert [event.event_type for event in events] == ["start", "failure"]
        assert events[-1].actor_type == "device"
        assert events[-1].to_status == "paused"
        assert json.loads(events[-1].payload_json) == {
            "error_code": "audio_playback_failed",
            "source": "device_ack",
        }


def test_apply_ack_rejects_device_rotation_before_sequence_or_ack_write(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    replacement_capability = "c" * 64
    with Session(service_engine) as db:
        _seed_ready(db)
        _start(db)
        db.commit()
        command = _current_tts(db)
        original = db.get(PatientDeviceCapability, CAPABILITY_HASH)
        assert original is not None
        original.active_session_key = None
        original.recovery_only_at = NOW + timedelta(milliseconds=1)
        db.add(PatientDeviceCapability(
            token_hash=replacement_capability,
            session_id=SESSION_ID,
            device_id_hash="d" * 64,
            active_session_key=SESSION_ID,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        ))
        db.commit()

        ended = AutopilotAckIn(
            idempotency_key="ack-rotated-device-0001",
            ack_type="tts_ended",
            control_generation=1,
            runner_generation=1,
            command_revision=0,
            device_event_seq=1,
            media_ended=True,
        )
        with pytest.raises(AutopilotServiceError) as caught:
            _apply_ack(
                db,
                command,
                ended,
                capability_hash=replacement_capability,
                now=NOW + timedelta(seconds=1),
            )
        assert caught.value.code == "autopilot_command_device_rotated"
        db.rollback()
        assert list(db.exec(select(RuntimeCommandAck))) == []
        assert db.get(
            PatientDeviceCapability, replacement_capability,
        ).last_autopilot_event_seq == 0
        stored = db.get(RuntimeCommand, command.id)
        assert stored is not None and stored.state == "pending"


def test_record_stopped_preflight_rollback_happy_replay_and_conflict(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _start(db)
        db.commit()
        tts = _current_tts(db)
        ended = AutopilotAckIn(
            idempotency_key="ack-record-flow-tts-end-0001",
            ack_type="tts_ended",
            control_generation=1,
            runner_generation=1,
            command_revision=0,
            device_event_seq=1,
            media_ended=True,
        )
        routed = _apply_ack(db, tts, ended)
        db.commit()
        assert routed.command is not None and routed.command.kind == "record"
        state = db.get(SessionAutopilotState, SESSION_ID)
        record = db.get(RuntimeCommand, state.current_command_id)
        assert record is not None and record.kind == "record"
        raw_audio_id = record.expected_raw_audio_id
        assert raw_audio_id is not None

        checksum = "e" * 64
        asset = db.get(AudioAssetRow, raw_audio_id)
        assert asset is not None
        asset.checksum = checksum
        asset.byte_count = 2048
        asset.uploaded_at = NOW + timedelta(seconds=1)
        receipt = AudioCaptureReceipt(
            raw_audio_id=raw_audio_id,
            session_id=SESSION_ID,
            turn_key=record.turn_key,
            received_at=NOW + timedelta(seconds=1),
            duration_seconds=2.5,
            byte_count=2048,
            checksum=checksum,
            data_classification="simulation",
            is_simulation=True,
            contains_direct_identifier=False,
        )
        db.add(receipt)
        db.commit()
        assert receipt.server_seq is not None

        started = AutopilotAckIn(
            idempotency_key="ack-record-flow-start-0001",
            ack_type="record_started",
            control_generation=1,
            runner_generation=1,
            command_revision=0,
            device_event_seq=2,
            mime_type="audio/webm;codecs=opus",
            sample_rate_hz=48_000,
            channels=1,
        )
        _apply_ack(
            db, record, started, now=NOW + timedelta(seconds=1))
        db.commit()

        stopped_bad = AutopilotAckIn(
            idempotency_key="ack-record-flow-stop-00001",
            ack_type="record_stopped",
            control_generation=1,
            runner_generation=1,
            command_revision=1,
            device_event_seq=3,
            raw_audio_id=raw_audio_id,
            receipt_server_seq=receipt.server_seq,
            checksum="f" * 64,
            byte_count=2048,
            duration_seconds=2.5,
            stop_reason="user_done",
        )
        with pytest.raises(AutopilotServiceError) as caught:
            _apply_ack(
                db,
                db.get(RuntimeCommand, record.id),
                stopped_bad,
                now=NOW + timedelta(seconds=2),
            )
        assert caught.value.code == "autopilot_record_capture_invalid"
        db.rollback()
        assert len(list(db.exec(select(RuntimeCommandAck)))) == 2
        assert db.get(PatientDeviceCapability, CAPABILITY_HASH).last_autopilot_event_seq == 2
        stored_record = db.get(RuntimeCommand, record.id)
        assert stored_record is not None
        assert (stored_record.state, stored_record.revision) == ("started", 1)
        assert db.get(SessionAutopilotState, SESSION_ID).status == "waiting_recording"

        stopped = stopped_bad.model_copy(update={"checksum": checksum})
        processed = _apply_ack(
            db,
            stored_record,
            stopped,
            now=NOW + timedelta(seconds=2),
        )
        db.commit()
        assert processed.replayed is False
        assert processed.status == "processing_attempt"
        assert (processed.command_state, processed.command_revision) == (
            "succeeded", 2)
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None
        assert state.status == "processing_attempt"
        assert state.current_command_id == record.id
        assert db.get(PatientDeviceCapability, CAPABILITY_HASH).last_autopilot_event_seq == 3

        replay = _apply_ack(
            db,
            db.get(RuntimeCommand, record.id),
            stopped,
            now=NOW + timedelta(seconds=3),
        )
        db.commit()
        assert replay.replayed is True and replay.command is None
        assert len(list(db.exec(select(RuntimeCommandAck)))) == 3

        conflict = stopped.model_copy(update={"byte_count": 2049})
        with pytest.raises(AutopilotServiceError) as caught:
            _apply_ack(
                db,
                db.get(RuntimeCommand, record.id),
                conflict,
                now=NOW + timedelta(seconds=3),
            )
        assert caught.value.code == "autopilot_ack_conflict"
        db.rollback()
        assert len(list(db.exec(select(RuntimeCommandAck)))) == 3


def test_apply_terminal_feedback_preserves_scope_boundary(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        command = _seed_purpose_tts(
            db, purpose="feedback", prompt_level=0, attempt_seq=1)
        ended = AutopilotAckIn(
            idempotency_key="ack-feedback-ended-apply-1",
            ack_type="tts_ended",
            control_generation=1,
            runner_generation=1,
            command_revision=0,
            device_event_seq=1,
            media_ended=True,
        )
        result = _apply_ack(db, command, ended, bank=FIRST_ONLY_BANK)
        db.commit()
        assert result.status == "scope_completed" and result.command is None
        assert result.command_state == "succeeded"
        assert list(db.exec(select(AudioAssetRow))) == []
        assert len(list(db.exec(select(RuntimeCommand)))) == 1
        state = db.get(SessionAutopilotState, SESSION_ID)
        runtime_state = db.get(SessionRuntimeState, SESSION_ID)
        assert state is not None
        assert state.status == "scope_completed" and state.current_command_id is None
        assert runtime_state is not None and runtime_state.status == "active"
        assert runtime_state.intervention_completed_at is None


@pytest.mark.parametrize(
    ("answer_type", "contains_target"),
    [("正确", True), ("偏题", True)],
)
def test_completed_attempt_success_requires_frozen_target_or_acceptable_hit(
    service_engine, monkeypatch, answer_type, contains_target,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        record, attempt = _seed_completed_autopilot_attempt(
            db,
            operational_answer_type=answer_type,
            contains_target=contains_target,
        )
        result = route_completed_attempt(
            db,
            session_id=SESSION_ID,
            attempt_id=attempt.id,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW + timedelta(seconds=4),
        )
        db.commit()

        assert result.replayed is False and result.status == "waiting_tts"
        assert result.command.kind == "tts"
        assert result.command.payload.purpose == "feedback"
        assert result.command.payload.speech_text == BANK.single_element[0]["success_line"]
        assert (result.command.prompt_level, result.command.attempt_seq) == (
            record.prompt_level, record.attempt_seq)
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None
        assert state.current_command_id is not None and state.status == "waiting_tts"
        assert state.lease_owner is None and state.lease_expires_at is None
        assert len(list(db.exec(select(RuntimeCommand)))) == 3
        items = list(db.exec(select(ItemEvent)))
        turns = list(db.exec(select(TurnEvent)))
        assert len(items) == 1 and len(turns) == 1
        assert (
            items[0].session_id,
            items[0].item_id,
            items[0].image_id,
            items[0].task_type.value,
            items[0].item_set_type.value,
            items[0].presentation_order,
        ) == (
            SESSION_ID,
            attempt.item_id,
            BANK.single_element[0]["image_id"],
            "单要素",
            "训练集",
            1,
        )
        assert (
            turns[0].item_event_id,
            turns[0].source_attempt_id,
            turns[0].response_role,
            turns[0].raw_audio_id,
            turns[0].asr_text,
            turns[0].ai_answer_type,
            turns[0].ai_score,
        ) == (
            items[0].id,
            attempt.id,
            attempt.response_role,
            attempt.raw_audio_id,
            attempt.asr_text,
            attempt.operational_answer_type,
            attempt.operational_score,
        )
        assert turns[0].confirmed_response_text is None
        assert turns[0].reviewer_id is None
        assert turns[0].reviewed_score is None
        assert turns[0].score_locked is False


@pytest.mark.parametrize(
    ("answer_type", "expected_path"),
    [
        ("沉默", "silence"),
        ("部分正确", "close"),
        ("上位词或相关词", "close"),
        ("偏题", "unknown"),
        # A semantic model verdict cannot override the source completion rule.
        ("正确", "unknown"),
    ],
)
def test_initial_failure_selects_and_persists_exact_source_cue1_path(
    service_engine, monkeypatch, answer_type, expected_path,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _record, attempt = _seed_completed_autopilot_attempt(
            db,
            operational_answer_type=answer_type,
            contains_target=False,
            operational_needs_review=(answer_type == "正确"),
        )
        result = route_completed_attempt(
            db,
            session_id=SESSION_ID,
            attempt_id=attempt.id,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW + timedelta(seconds=4),
        )
        db.commit()

        expected_line = BANK.single_element[0]["cues"]["1"]["variants"][
            expected_path
        ]["text"]
        assert result.command.payload.purpose == "cue"
        assert result.command.payload.response_path == expected_path
        assert result.command.payload.speech_text == expected_line
        stored = db.exec(select(RuntimeCommand).where(
            RuntimeCommand.idempotency_key == result.command.command_key,
        )).one()
        payload = TtsCommandPayload.model_validate_json(stored.payload_json)
        assert payload.response_path == expected_path
        assert payload.speech_text == expected_line
        if answer_type == "正确":
            stored_attempt = db.get(AttemptEvent, attempt.id)
            assert stored_attempt is not None
            assert stored_attempt.operational_needs_review is True
        assert list(db.exec(select(ItemEvent))) == []
        assert list(db.exec(select(TurnEvent))) == []


@pytest.mark.parametrize(
    ("initial_answer_type", "expected_path"),
    [
        ("偏题", "unknown"),
        ("部分正确", "close"),
        ("沉默", "silence"),
    ],
)
def test_level_one_success_reuses_first_failure_path_feedback(
    service_engine, monkeypatch, initial_answer_type, expected_path,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        record, attempt = _seed_completed_autopilot_attempt(
            db,
            prompt_level=1,
            initial_answer_type=initial_answer_type,
            operational_answer_type="正确",
            contains_target=True,
        )
        result = route_completed_attempt(
            db,
            session_id=SESSION_ID,
            attempt_id=attempt.id,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW + timedelta(seconds=15),
        )
        db.commit()

        expected_line = _filled_protocol_line(
            PROTOCOL["naming"]["success_after_cue1"][expected_path])
        assert result.command.payload.purpose == "feedback"
        assert result.command.payload.response_path == expected_path
        assert result.command.payload.speech_text == expected_line
        assert (result.command.prompt_level, result.command.attempt_seq) == (
            record.prompt_level, record.attempt_seq)


def test_level_two_success_uses_single_frozen_feedback_without_response_path(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        record, attempt = _seed_completed_autopilot_attempt(
            db,
            prompt_level=2,
            initial_answer_type="沉默",
            operational_answer_type="正确",
            contains_target=True,
        )
        result = route_completed_attempt(
            db,
            session_id=SESSION_ID,
            attempt_id=attempt.id,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW + timedelta(seconds=25),
        )
        db.commit()

        assert result.command.payload.purpose == "feedback"
        assert result.command.payload.response_path is None
        assert result.command.payload.speech_text == _filled_protocol_line(
            PROTOCOL["naming"]["success_after_cue2"])
        assert (result.command.prompt_level, result.command.attempt_seq) == (
            record.prompt_level, record.attempt_seq)


def test_attempt_route_replay_rejects_valid_but_wrong_response_branch(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _record, attempt = _seed_completed_autopilot_attempt(
            db, operational_answer_type="偏题", contains_target=False)
        created = route_completed_attempt(
            db,
            session_id=SESSION_ID,
            attempt_id=attempt.id,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW + timedelta(seconds=4),
        )
        db.commit()
        command = db.exec(select(RuntimeCommand).where(
            RuntimeCommand.idempotency_key == created.command.command_key,
        )).one()
        payload = TtsCommandPayload.model_validate_json(command.payload_json)
        tampered_payload = payload.model_copy(update={
            "response_path": "close",
            "speech_text": BANK.single_element[0]["cues"]["1"]["variants"][
                "close"
            ]["text"],
        }).model_dump_json()
        db.execute(update(RuntimeCommand).where(
            RuntimeCommand.id == command.id,
        ).values(payload_json=tampered_payload))
        db.commit()

        with pytest.raises(AutopilotServiceError) as caught:
            route_completed_attempt(
                db,
                session_id=SESSION_ID,
                attempt_id=attempt.id,
                bank=FIRST_ONLY_BANK,
                protocol=PROTOCOL,
                now=NOW + timedelta(seconds=5),
            )
        assert caught.value.code == "autopilot_idempotency_conflict"
        db.rollback()


def test_level_one_result_rejects_tampered_first_cue_path_chain(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        record, attempt = _seed_completed_autopilot_attempt(
            db,
            prompt_level=1,
            initial_answer_type="偏题",
            operational_answer_type="正确",
            contains_target=True,
        )
        assert record.predecessor_command_id is not None
        cue = db.get(RuntimeCommand, record.predecessor_command_id)
        assert cue is not None
        payload = TtsCommandPayload.model_validate_json(cue.payload_json)
        tampered_payload = payload.model_copy(update={
            "response_path": "close",
            "speech_text": BANK.single_element[0]["cues"]["1"]["variants"][
                "close"
            ]["text"],
        }).model_dump_json()
        db.execute(update(RuntimeCommand).where(
            RuntimeCommand.id == cue.id,
        ).values(payload_json=tampered_payload))
        db.commit()

        with pytest.raises(AutopilotServiceError) as caught:
            route_completed_attempt(
                db,
                session_id=SESSION_ID,
                attempt_id=attempt.id,
                bank=FIRST_ONLY_BANK,
                protocol=PROTOCOL,
                now=NOW + timedelta(seconds=15),
            )
        assert caught.value.code == "autopilot_attempt_sequence_invalid"
        db.rollback()


@pytest.mark.parametrize(
    ("prompt_level", "purpose", "next_prompt", "next_attempt", "speech_key"),
    [
        (0, "cue", 1, 2, "cues"),
        (1, "cue", 2, 3, "cues"),
        (2, "tell_answer", 3, 3, "tell_answer"),
    ],
)
def test_completed_incorrect_attempt_routes_two_cues_then_tell_answer(
    service_engine, monkeypatch,
    prompt_level, purpose, next_prompt, next_attempt, speech_key,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _record, attempt = _seed_completed_autopilot_attempt(
            db,
            prompt_level=prompt_level,
            operational_answer_type="偏题",
            contains_target=False,
        )
        result = route_completed_attempt(
            db,
            session_id=SESSION_ID,
            attempt_id=attempt.id,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW + timedelta(seconds=4),
        )
        db.commit()

        expected_speech = (
            BANK.single_element[0][speech_key][str(next_prompt)]["text"]
            if speech_key == "cues"
            else BANK.single_element[0][speech_key]
        )
        assert result.command.payload.purpose == purpose
        assert result.command.payload.speech_text == expected_speech
        assert (result.command.prompt_level, result.command.attempt_seq) == (
            next_prompt, next_attempt)
        expected_terminal_rows = 1 if prompt_level == 2 else 0
        assert len(list(db.exec(select(ItemEvent)))) == expected_terminal_rows
        assert len(list(db.exec(select(TurnEvent)))) == expected_terminal_rows


def test_terminal_evidence_materialization_replays_one_row_and_conflicts_fail_closed(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _record, attempt = _seed_completed_autopilot_attempt(
            db,
            operational_answer_type="正确",
            contains_target=True,
        )
        first = materialize_terminal_attempt_evidence(
            db,
            session_id=SESSION_ID,
            attempt_id=attempt.id,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW + timedelta(seconds=4),
        )
        second = materialize_terminal_attempt_evidence(
            db,
            session_id=SESSION_ID,
            attempt_id=attempt.id,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW + timedelta(seconds=4),
        )
        db.commit()

        assert first is not None and second is not None
        assert first.replayed is False and second.replayed is True
        assert (first.item_event_id, first.turn_event_id) == (
            second.item_event_id, second.turn_event_id)
        assert len(list(db.exec(select(ItemEvent)))) == 1
        assert len(list(db.exec(select(TurnEvent)))) == 1

        item = db.get(ItemEvent, first.item_event_id)
        assert item is not None
        item.presentation_order = 99
        db.add(item)
        db.commit()
        with pytest.raises(AutopilotServiceError) as caught:
            materialize_terminal_attempt_evidence(
                db,
                session_id=SESSION_ID,
                attempt_id=attempt.id,
                bank=FIRST_ONLY_BANK,
                protocol=PROTOCOL,
                now=NOW + timedelta(seconds=4),
            )
        assert caught.value.code == "autopilot_terminal_evidence_conflict"
        db.rollback()
        assert len(list(db.exec(select(TurnEvent)))) == 1


def test_terminal_evidence_position_constraints_reject_duplicate_rows(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        first = ItemEvent(
            session_id=SESSION_ID,
            item_id=BANK.single_element[0]["item_id"],
            image_id=BANK.single_element[0]["image_id"],
            task_type="单要素",
            item_set_type="训练集",
            presentation_order=1,
        )
        db.add(first)
        db.commit()
        db.add(ItemEvent(
            session_id=SESSION_ID,
            item_id=first.item_id,
            image_id=first.image_id,
            task_type="单要素",
            item_set_type="训练集",
            presentation_order=1,
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        stored = db.get(ItemEvent, first.id)
        assert stored is not None and stored.id is not None
        db.add(TurnEvent(
            item_event_id=stored.id,
            turn_seq=1,
            response_role="命名",
            raw_audio_id="raw-constraint-one",
        ))
        db.commit()
        db.add(TurnEvent(
            item_event_id=stored.id,
            turn_seq=1,
            response_role="其他角色",
            raw_audio_id="raw-constraint-two",
        ))
        with pytest.raises(IntegrityError):
            db.commit()


def test_completed_attempt_route_is_caller_transactional_and_idempotent(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _record, attempt = _seed_completed_autopilot_attempt(db)
        baseline_revision = db.get(SessionAutopilotState, SESSION_ID).revision

        staged = route_completed_attempt(
            db,
            session_id=SESSION_ID,
            attempt_id=attempt.id,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW + timedelta(seconds=4),
        )
        assert staged.replayed is False
        assert len(list(db.exec(select(RuntimeCommand)))) == 3
        db.rollback()
        assert len(list(db.exec(select(RuntimeCommand)))) == 2
        rolled_back_state = db.get(SessionAutopilotState, SESSION_ID)
        assert rolled_back_state is not None
        assert rolled_back_state.status == "processing_attempt"
        assert rolled_back_state.revision == baseline_revision

        created = route_completed_attempt(
            db,
            session_id=SESSION_ID,
            attempt_id=attempt.id,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW + timedelta(seconds=4),
        )
        db.commit()
        replay = route_completed_attempt(
            db,
            session_id=SESSION_ID,
            attempt_id=attempt.id,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW + timedelta(seconds=5),
        )
        db.commit()
        assert created.replayed is False and replay.replayed is True
        assert replay.command.command_key == created.command.command_key
        assert replay.state_revision == created.state_revision
        assert len(list(db.exec(select(RuntimeCommand)))) == 3


@pytest.mark.parametrize(
    "mutation",
    ["session", "raw_audio", "item", "turn", "attempt_seq", "prompt_level"],
)
def test_completed_attempt_must_exactly_match_capture_position(
    service_engine, monkeypatch, mutation,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _record, attempt = _seed_completed_autopilot_attempt(db)
        if mutation == "session":
            db.add(TrainSession(
                session_id="S-P0A-FOREIGN",
                patient_id=PATIENT_ID,
                week_no=2,
                phase_type=PhaseType.正式训练,
                event_line=EventLine.正式训练,
                item_bank_version_id=BANK.version_id,
                is_simulation=True,
                data_classification="simulation",
            ))
            db.flush()
            attempt.session_id = "S-P0A-FOREIGN"
        elif mutation == "raw_audio":
            db.add(AudioAssetRow(
                raw_audio_id="raw-foreign-attempt-route",
                session_id=SESSION_ID,
                is_simulation=True,
                data_classification="simulation",
                turn_key=attempt.item_id + "#1",
                status="recorded",
            ))
            db.flush()
            attempt.raw_audio_id = "raw-foreign-attempt-route"
        elif mutation == "item":
            attempt.item_id = "SE_FOREIGN"
        elif mutation == "turn":
            attempt.turn_seq = 2
        elif mutation == "attempt_seq":
            attempt.attempt_seq = 2
        else:
            attempt.prompt_level = 1
        db.commit()

        with pytest.raises(AutopilotServiceError) as caught:
            route_completed_attempt(
                db,
                session_id=SESSION_ID,
                attempt_id=attempt.id,
                bank=FIRST_ONLY_BANK,
                protocol=PROTOCOL,
                now=NOW + timedelta(seconds=4),
            )
        assert caught.value.code in {
            "autopilot_attempt_not_found", "autopilot_attempt_mismatch",
        }
        db.rollback()
        assert len(list(db.exec(select(RuntimeCommand)))) == 2
        assert db.get(SessionAutopilotState, SESSION_ID).status == "processing_attempt"


def test_completed_attempt_requires_operational_only_and_rechecks_capture_proof(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _record, attempt = _seed_completed_autopilot_attempt(
            db, truth_scope="research_truth")
        with pytest.raises(AutopilotServiceError) as caught:
            route_completed_attempt(
                db,
                session_id=SESSION_ID,
                attempt_id=attempt.id,
                bank=FIRST_ONLY_BANK,
                protocol=PROTOCOL,
                now=NOW + timedelta(seconds=4),
            )
        assert caught.value.code == "autopilot_attempt_boundary_invalid"
        db.rollback()

        judgement = db.exec(select(InteractionEvent).where(
            InteractionEvent.attempt_id == attempt.id,
            InteractionEvent.event_type == "judgement_completed",
        )).one()
        payload = evidence_ledger.validate_stored_payload(
            judgement.event_type, judgement.payload_json)
        payload["truth_scope"] = "operational_only"
        # Simulate a privileged out-of-band ledger tamper. Ordinary ORM writes
        # are rejected because InteractionEvent is append-only.
        db.execute(update(InteractionEvent).where(
            InteractionEvent.id == judgement.id,
        ).values(payload_json=evidence_ledger.encode_event_payload(
            judgement.event_type, payload)))
        asset = db.get(AudioAssetRow, attempt.raw_audio_id)
        assert asset is not None
        asset.checksum = "f" * 64
        db.commit()

        with pytest.raises(AutopilotServiceError) as caught:
            route_completed_attempt(
                db,
                session_id=SESSION_ID,
                attempt_id=attempt.id,
                bank=FIRST_ONLY_BANK,
                protocol=PROTOCOL,
                now=NOW + timedelta(seconds=4),
            )
        assert caught.value.code == "autopilot_record_capture_invalid"
        db.rollback()
        assert len(list(db.exec(select(RuntimeCommand)))) == 2


@pytest.mark.parametrize("terminal_change", ["technical_failure", "state_paused"])
def test_completed_attempt_route_fails_closed_on_technical_or_state_change(
    service_engine, monkeypatch, terminal_change,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _record, attempt = _seed_completed_autopilot_attempt(db)
        if terminal_change == "technical_failure":
            attempt.processing_status = "technical_failure"
            attempt.error_code = "judge_exception"
        else:
            state = db.get(SessionAutopilotState, SESSION_ID)
            assert state is not None
            state.status = "paused"
            state.current_command_id = None
        db.commit()

        with pytest.raises(AutopilotServiceError) as caught:
            route_completed_attempt(
                db,
                session_id=SESSION_ID,
                attempt_id=attempt.id,
                bank=FIRST_ONLY_BANK,
                protocol=PROTOCOL,
                now=NOW + timedelta(seconds=4),
            )
        assert caught.value.code in {
            "autopilot_attempt_not_completed", "autopilot_attempt_not_current",
        }
        db.rollback()
        assert len(list(db.exec(select(RuntimeCommand)))) == 2


def test_concurrent_completed_attempt_routes_create_at_most_one_tts(
    service_engine, monkeypatch,
):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _record, attempt = _seed_completed_autopilot_attempt(db)
        attempt_id = int(attempt.id)

    ready = threading.Barrier(2)

    def _worker():
        with Session(service_engine) as db:
            ready.wait(timeout=5)
            try:
                result = route_completed_attempt(
                    db,
                    session_id=SESSION_ID,
                    attempt_id=attempt_id,
                    bank=FIRST_ONLY_BANK,
                    protocol=PROTOCOL,
                    now=NOW + timedelta(seconds=4),
                )
                db.commit()
                return ("ok", result.replayed)
            except (AutopilotServiceError, OperationalError) as exc:
                db.rollback()
                return ("closed", getattr(exc, "code", "database_busy"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: _worker(), range(2)))
    assert any(status == "ok" for status, _detail in results)
    with Session(service_engine) as db:
        assert len(list(db.exec(select(RuntimeCommand)))) == 3
        replay = route_completed_attempt(
            db,
            session_id=SESSION_ID,
            attempt_id=attempt_id,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW + timedelta(seconds=5),
        )
        db.commit()
        assert replay.replayed is True
        assert len(list(db.exec(select(RuntimeCommand)))) == 3


def test_concurrent_exact_takeover_creates_one_audit_fact_and_replays(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    takeover_key = "takeover-concurrent-service-0001"
    with Session(service_engine) as db:
        _seed_ready(db)
        _start(db)
        db.commit()
        command = _current_tts(db)
        assert pause_autonomous_scope_for_researcher(
            db,
            session_id=SESSION_ID,
            actor_id=ACTOR_ID,
            now=NOW + timedelta(seconds=1),
        )
        db.commit()
        drained = acknowledge_device_drain(
            db,
            session_id=SESSION_ID,
            command_key=command.idempotency_key,
            capability_token_hash=CAPABILITY_HASH,
            now=NOW + timedelta(seconds=2),
        )
        db.commit()
        assert (drained.replayed, drained.state_revision) == (False, 3)

    ready = threading.Barrier(2)

    def _worker():
        with Session(service_engine) as db:
            ready.wait(timeout=5)
            try:
                receipt = takeover_autopilot_to_manual(
                    db,
                    session_id=SESSION_ID,
                    idempotency_key=takeover_key,
                    expected_revision=3,
                    actor_id=ACTOR_ID,
                    now=NOW + timedelta(seconds=3),
                )
                db.commit()
                return ("ok", receipt.mode)
            except (AutopilotServiceError, OperationalError) as exc:
                db.rollback()
                return ("closed", getattr(exc, "code", "database_busy"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: _worker(), range(2)))
    assert any(status == "ok" for status, _detail in results)

    with Session(service_engine) as db:
        state = db.get(SessionAutopilotState, SESSION_ID)
        takeover_events = list(db.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.event_type == "takeover")))
        assert state is not None
        assert (state.mode, state.status, state.revision) == (
            "manual", "paused", 4)
        assert len(takeover_events) == 1
        replay = takeover_autopilot_to_manual(
            db,
            session_id=SESSION_ID,
            idempotency_key=takeover_key,
            expected_revision=3,
            actor_id=ACTOR_ID,
            now=NOW + timedelta(seconds=4),
        )
        db.commit()
        assert replay.mode == "manual"
        assert replay.state_revision == 4
        assert len(list(db.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.event_type == "takeover")))) == 1


@pytest.mark.parametrize(
    ("source", "reason_code", "actor_type"),
    [
        ("patient_rec_failure", "microphone_permission_denied", "device"),
        ("session_abort", "session_aborted", "researcher"),
        (
            "cloud_processing_consent_revoked",
            "cloud_processing_revoked",
            "system",
        ),
    ],
)
def test_external_stop_atomically_fences_current_command_and_requires_drain(
        service_engine, monkeypatch, source, reason_code, actor_type):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _start(db)
        db.commit()
        command = _current_tts(db)
        kwargs = {}
        if actor_type == "device":
            kwargs.update({
                "capability_token_hash": CAPABILITY_HASH,
                "expected_item_id": command.item_id,
                "expected_turn_seq": command.turn_seq,
                "idempotency_token": "patient-rec-failure-service-0001",
            })
        elif actor_type == "researcher":
            kwargs["actor_id"] = ACTOR_ID

        assert fence_autonomous_scope_for_external_stop(
            db,
            session_id=SESSION_ID,
            reason_code=reason_code,
            source=source,
            actor_type=actor_type,
            now=NOW + timedelta(seconds=1),
            **kwargs,
        )
        db.commit()

        state = db.get(SessionAutopilotState, SESSION_ID)
        runtime_state = db.get(SessionRuntimeState, SESSION_ID)
        assert state is not None
        assert state.status == "paused"
        assert state.current_command_id is None
        assert state.last_error_code == reason_code
        assert state.lease_owner is None
        # Domain helper owns only the autonomous half; HTTP callers commit their
        # runtime pause/abort in this same surrounding transaction.
        assert runtime_state is not None and runtime_state.status == "active"
        latest = list(db.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))[-1]
        assert latest.event_type == "pause"
        assert latest.actor_type == actor_type
        assert latest.actor_id == (
            DEVICE_HASH if actor_type == "device"
            else ACTOR_ID if actor_type == "researcher"
            else None
        )
        assert latest.command_id == command.id
        assert json.loads(latest.payload_json) == {
            "reason_code": reason_code,
            "source": source,
        }

        # A repeated external stop is stable and cannot append another fence.
        assert fence_autonomous_scope_for_external_stop(
            db,
            session_id=SESSION_ID,
            reason_code=reason_code,
            source=source,
            actor_type=actor_type,
            now=NOW + timedelta(seconds=2),
            **kwargs,
        ) is False
        db.commit()
        assert len(list(db.exec(select(AutopilotControlEvent)))) == 2

        target = get_drain_target(
            db,
            session_id=SESSION_ID,
            capability_token_hash=CAPABILITY_HASH,
        )
        assert target.command_key == command.idempotency_key
        drained = acknowledge_device_drain(
            db,
            session_id=SESSION_ID,
            command_key=command.idempotency_key,
            capability_token_hash=CAPABILITY_HASH,
            now=NOW + timedelta(seconds=3),
        )
        db.commit()
        assert drained.replayed is False


@pytest.mark.parametrize(("governance_change", "attempt_error"), [
    ("patient_withdrawn", "patient is missing or withdrawn"),
    ("audio_isolated", "capture audio asset does not match stopped ACK"),
])
def test_terminal_media_proof_survives_governance_change_but_attempt_gate_does_not(
        service_engine, monkeypatch, governance_change, attempt_error):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        record, _attempt = _seed_completed_autopilot_attempt(db)
        if governance_change == "patient_withdrawn":
            patient = db.get(Patient, PATIENT_ID)
            assert patient is not None
            patient.withdrawal_status = "withdrawn_after_capture"
            db.add(patient)
        else:
            assert record.expected_raw_audio_id is not None
            audio = db.get(AudioAssetRow, record.expected_raw_audio_id)
            assert audio is not None
            audio.withdrawn = True
            audio.withdrawal_status = "isolated"
            db.add(audio)
        db.commit()

        terminal = autopilot_ledger.verify_terminal_record_capture(
            db, int(record.id))
        assert terminal.raw_audio_id == record.expected_raw_audio_id
        with pytest.raises(
                autopilot_ledger.AutopilotProofError,
                match=attempt_error):
            autopilot_ledger.verify_record_capture_for_attempt(
                db, int(record.id))


def test_scope_completed_terminal_tts_can_transfer_to_manual_without_drain(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        feedback = _seed_purpose_tts(
            db,
            purpose="feedback",
            prompt_level=0,
            attempt_seq=1,
        )
        ack_key = _complete_tts(
            db,
            feedback,
            ack_key="ack-scope-complete-takeover-0001",
        )
        completed = route_tts_ended(
            db,
            session_id=SESSION_ID,
            command_key=feedback.idempotency_key,
            ack_idempotency_key=ack_key,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW + timedelta(seconds=1),
        )
        db.commit()
        assert completed.status == "scope_completed"
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None
        target = get_drain_target(
            db,
            session_id=SESSION_ID,
            capability_token_hash=CAPABILITY_HASH,
        )
        assert target.model_dump() == {
            "command_key": feedback.idempotency_key,
            "state_revision": state.revision,
        }
        receipt = takeover_autopilot_to_manual(
            db,
            session_id=SESSION_ID,
            idempotency_key="takeover-scope-complete-0001",
            expected_revision=state.revision,
            actor_id=ACTOR_ID,
            now=NOW + timedelta(seconds=2),
        )
        db.commit()
        assert (receipt.mode, receipt.status, receipt.server_owned) == (
            "manual", "scope_completed", False)


@pytest.mark.parametrize("prompt_level", [0, 1, 2])
def test_authoritative_attempt_input_is_derived_only_from_capture_and_frozen_plan(
    service_engine, monkeypatch, prompt_level,
):
    _enable_p0a(monkeypatch)
    monkeypatch.setattr(
        content, "load_item_bank", lambda _path: FIRST_ONLY_BANK)
    with Session(service_engine) as db:
        _seed_ready(db)
        record, attempt = _seed_completed_autopilot_attempt(
            db, prompt_level=prompt_level)
        before_commands = len(list(db.exec(select(RuntimeCommand))))
        derived = autopilot_orchestration.derive_authoritative_attempt_input(
            db,
            session_id=SESSION_ID,
            now=NOW + timedelta(seconds=4),
        )
        expected_cue = (
            None if prompt_level == 0
            else BANK.single_element[0]["cues"][str(prompt_level)]["cue_type"]
        )
        assert derived.model_dump() == {
            "item_id": record.item_id,
            "turn_seq": record.turn_seq,
            "response_role": "命名",
            "raw_audio_id": record.expected_raw_audio_id,
            "prompt_level": record.prompt_level,
            "cue_type": expected_cue,
            "duration_seconds": 2.5,
        }
        assert attempt.attempt_seq == record.attempt_seq
        db.rollback()
        assert len(list(db.exec(select(RuntimeCommand)))) == before_commands
        assert db.get(SessionAutopilotState, SESSION_ID).status == "processing_attempt"


def test_attempt_submission_is_nonblocking_and_deduplicates_in_process(
    monkeypatch,
):
    _enable_p0a(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def _blocking_worker(_session_id: str):
        entered.set()
        release.wait(timeout=5)
        finished.set()

    try:
        assert autopilot_orchestration.submit(
            "S-P0A-SCHEDULER", _blocking_worker) is True
        assert entered.wait(timeout=2)
        # The first submit already returned while provider-like work is blocked.
        assert finished.is_set() is False
        assert autopilot_orchestration.submit(
            "S-P0A-SCHEDULER", _blocking_worker) is False
    finally:
        release.set()
    assert finished.wait(timeout=2)


def test_account_status_disabled_is_canonical_and_active_state_is_consistent(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        db.add(SessionAutopilotState(
            session_id=SESSION_ID,
            scope_key="disabled",
            mode="disabled",
            status="idle",
            revision=27,
        ))
        db.commit()
        disabled = get_autopilot_status(
            db, session_id=SESSION_ID)
        assert disabled.model_dump() == {
            "scope_key": "disabled",
            "mode": "disabled",
            "status": "idle",
            "state_revision": 0,
            "server_owned": False,
            "current_command_kind": None,
            "last_error_code": None,
        }

        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None
        state.scope_key = P0A_SCOPE_KEY
        state.mode = "autonomous"
        state.status = "paused"
        state.revision = 28
        state.last_error_code = "patient said target"
        db.add(state)
        db.commit()
        with pytest.raises(AutopilotServiceError) as unsafe_error:
            get_autopilot_status(db, session_id=SESSION_ID)
        assert unsafe_error.value.code == "autopilot_state_invalid"
