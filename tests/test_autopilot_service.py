"""P0a domain-service tests: fail-closed gates and current-command issuance."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import count as _itertools_count
import json
import threading

import pytest
from sqlalchemy import event, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, SQLModel, create_engine, select

from app import (autopilot_ledger, autopilot_orchestration,
                 autopilot_plan_profiles, content, evidence_ledger,
                 repeat_intent, runtime)
from app.autopilot_contract import (
    AutopilotAckIn,
    RecordCommandPayload,
    TtsCommandPayload,
)
from app.autopilot_service import (
    INTERACTION_CUE_TYPE,
    P0A_FEATURE_ENV,
    P0A_SCOPE_KEY,
    REAL_SESSIONS_ENV,
    _select_p0a_content,
    AutopilotServiceError,
    acknowledge_device_drain,
    authorize_recording_command,
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
    VisitPlan,
)


NOW = datetime(2026, 7, 18, 8, 0, 0)
SESSION_ID = "S-P0A-SERVICE"
PATIENT_ID = "P-P0A-SERVICE"
CAPABILITY_HASH = "a" * 64
DEVICE_HASH = "b" * 64
START_KEY = "start-p0a-service-0001"
ACTOR_ID = "RESEARCHER-P0A"

REPEAT_PROTOCOL = repeat_intent.active_protocol()
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
        repeat_protocol_version_id=REPEAT_PROTOCOL.version_id,
        repeat_protocol_definition_digest=REPEAT_PROTOCOL.definition_digest,
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


def _bind_exact_demo20_profile(db: Session) -> TrainSession:
    train_session = db.get(TrainSession, SESSION_ID)
    assert train_session is not None
    plan_id = "VP-P0A-SERVICE-DEMO20"
    db.add(VisitPlan(
        plan_id=plan_id,
        protocol_slot_key="c" * 64,
        patient_id=train_session.patient_id,
        scheduled_date=NOW.date(),
        scheduled_time=NOW.time(),
        session_sitting_no=train_session.session_sitting_no,
        week_no=train_session.week_no,
        phase_type=train_session.phase_type,
        event_line=train_session.event_line,
        item_bank_version_id=train_session.item_bank_version_id,
        item_bank_definition_digest=train_session.item_bank_definition_digest,
        autopilot_protocol_version_id=(
            train_session.autopilot_protocol_version_id),
        autopilot_protocol_definition_digest=(
            train_session.autopilot_protocol_definition_digest),
        repeat_protocol_version_id=train_session.repeat_protocol_version_id,
        repeat_protocol_definition_digest=(
            train_session.repeat_protocol_definition_digest),
        autopilot_profile_version_id=(
            autopilot_plan_profiles.WEEK2_SINGLE20_DEMO_VERSION),
        autopilot_profile_definition_digest=(
            autopilot_plan_profiles.WEEK2_SINGLE20_DEMO_DIGEST),
        is_simulation=True,
        data_classification="simulation",
        status="started",
        revision=3,
        created_by=ACTOR_ID,
        created_at=NOW,
        updated_at=NOW,
        approved_by=ACTOR_ID,
        approved_at=NOW,
        started_by=ACTOR_ID,
        started_at=NOW,
    ))
    db.commit()
    train_session.visit_plan_id = plan_id
    train_session.autopilot_profile_version_id = (
        autopilot_plan_profiles.WEEK2_SINGLE20_DEMO_VERSION)
    train_session.autopilot_profile_definition_digest = (
        autopilot_plan_profiles.WEEK2_SINGLE20_DEMO_DIGEST)
    db.add(train_session)
    db.commit()
    return train_session


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
    item_index: int = 0,
) -> RuntimeCommand:
    item = bank.single_element[item_index]
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
        repeat_protocol_version_id=REPEAT_PROTOCOL.version_id,
        repeat_protocol_definition_digest=REPEAT_PROTOCOL.definition_digest,
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


def test_exact_demo20_starts_at_first_profile_position(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db, bank=BANK)
        train_session = _bind_exact_demo20_profile(db)

        resolution = autopilot_plan_profiles.resolve_for_session(
            train_session, bank=BANK, protocol=PROTOCOL)
        assert resolution.completion_scope == "demo_plan_only"
        assert resolution.resolved_position_count == 20
        assert resolution.unsupported_position_count == 0

        started = _start(db, bank=BANK)
        db.commit()
        assert started.status == "waiting_tts"
        assert started.command is not None
        assert started.command.item_ref == "itm-0001"
        issued = _current_tts(db)
        assert issued.item_id == resolution.plan.items[0].item_id
        assert issued.turn_seq == 1


def test_exact_demo20_completes_after_its_twentieth_position(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db, bank=BANK)
        train_session = _bind_exact_demo20_profile(db)
        resolution = autopilot_plan_profiles.resolve_for_session(
            train_session, bank=BANK, protocol=PROTOCOL)
        last_item = resolution.plan.items[-1]
        last_index, last_raw = next(
            (index, row)
            for index, row in enumerate(BANK.single_element)
            if row["item_id"] == last_item.item_id
        )
        command = _seed_purpose_tts(
            db,
            purpose="feedback",
            prompt_level=0,
            attempt_seq=1,
            speech_override=last_raw["success_line"],
            bank=BANK,
            item_index=last_index,
        )
        ack_key = _complete_tts(db, command)

        routed = route_tts_ended(
            db,
            session_id=SESSION_ID,
            command_key=command.idempotency_key,
            ack_idempotency_key=ack_key,
            bank=BANK,
            protocol=PROTOCOL,
            now=NOW,
        )
        db.commit()
        assert routed.status == "scope_completed"
        assert routed.command is None
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None
        assert state.status == "scope_completed"
        assert state.current_command_id is None


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


def test_start_fails_closed_on_exact_stale_protocol_version_with_zero_semantic_writes(
        service_engine, monkeypatch):
    """An old-but-well-formed protocol version binding -- not a corrupted or
    missing one like the sibling cases above -- must still fail start()
    before any control/runtime/command/event write, with the specific
    ``autopilot_protocol_version_mismatch`` code (not the digest-drift or
    missing-binding codes already covered by
    ``test_session_and_issued_command_fail_closed_on_definition_drift``).
    Never migrates or rebinds the stale session to the current protocol.
    """
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)

        # A genuine old binding pair, not "old version + still-current digest"
        # -- a real historical protocol revision's digest differs too.
        stale_version_id = "autopilot-v1-20260717"
        stale_digest = (
            "7e1f812972a07b80f4e88c01ae838254d2fbaae5e1f8c6d52aa06a18de61eccb")
        legacy = db.get(TrainSession, SESSION_ID)
        assert legacy is not None
        assert legacy.autopilot_protocol_version_id == PROTOCOL["protocol_version_id"]
        legacy.autopilot_protocol_version_id = stale_version_id
        legacy.autopilot_protocol_definition_digest = stale_digest
        db.add(legacy)
        db.commit()

        def _snapshot() -> tuple:
            """Detached primitive projection, taken fresh every call.

            Deliberately never returns raw ORM instances (or lists of them):
            SQLAlchemy's identity map hands back the *same* Python object,
            refreshed in place, for a given PK across queries on the same
            ``db``. A before/after pair built from live ORM rows can end up
            comparing an object against itself post-rollback -- silently
            "passing" even if a write briefly happened and was then rolled
            back, which is exactly the regression this test exists to catch.
            """
            train_session = db.get(TrainSession, SESSION_ID)
            runtime_state = db.get(SessionRuntimeState, SESSION_ID)
            live_state = db.get(LiveState, 1)
            autopilot_state = db.get(SessionAutopilotState, SESSION_ID)
            commands = list(db.exec(select(RuntimeCommand).where(
                RuntimeCommand.session_id == SESSION_ID,
            ).order_by(RuntimeCommand.id)))
            acks = list(db.exec(select(RuntimeCommandAck).where(
                RuntimeCommandAck.session_id == SESSION_ID,
            ).order_by(RuntimeCommandAck.id)))
            events = list(db.exec(select(AutopilotControlEvent).where(
                AutopilotControlEvent.session_id == SESSION_ID,
            ).order_by(AutopilotControlEvent.event_seq)))
            return (
                (train_session.session_id,
                 train_session.autopilot_protocol_version_id,
                 train_session.autopilot_protocol_definition_digest,
                 train_session.item_bank_version_id,
                 train_session.item_bank_definition_digest)
                if train_session is not None else None,
                (runtime_state.status, runtime_state.revision)
                if runtime_state is not None else None,
                (live_state.seq, live_state.session_json)
                if live_state is not None else None,
                (autopilot_state.mode, autopilot_state.status,
                 autopilot_state.revision, autopilot_state.current_command_id)
                if autopilot_state is not None else None,
                tuple(
                    (c.id, c.command_seq, c.kind, c.state) for c in commands),
                tuple(
                    (a.id, a.command_id, a.idempotency_key, a.ack_type)
                    for a in acks),
                tuple(
                    (e.id, e.event_seq, e.event_type, e.reason_code,
                     e.command_id)
                    for e in events),
            )

        before = _snapshot()
        assert before[0] == (
            SESSION_ID, stale_version_id, stale_digest,
            FIRST_ONLY_BANK.version_id,
            content.item_bank_definition_digest(FIRST_ONLY_BANK))
        assert before[1] == ("active", 0)
        assert before[3] is None
        assert before[4] == ()
        assert before[5] == ()
        assert before[6] == ()

        try:
            with pytest.raises(AutopilotServiceError) as caught:
                _start(db)
            assert caught.value.code == "autopilot_protocol_version_mismatch"
            after = _snapshot()
            assert after == before
        finally:
            db.rollback()


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


def test_start_admits_default_week2_plan_with_interaction_packages(
        service_engine, monkeypatch):
    """2026-08-21 交互数据包交付后：78 个位置全部可执行，完整周计划放行准入，
    首条命令仍是第一题（单要素）的冻结问句。"""
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db, bank=BANK)
        created = _start(db, bank=BANK)
        db.commit()
        assert created.replayed is False
        assert created.status == "waiting_tts"
        assert created.command is not None
        assert created.command.kind == "tts"
        assert created.command.payload.purpose == "question"
        assert created.command.payload.speech_text == (
            BANK.single_element[0]["initial_prompt"])
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None and state.status == "waiting_tts"


def test_start_still_rejects_full_plan_when_interaction_package_unavailable(
        service_engine, monkeypatch):
    """数据包装载被拆掉时，双/多要素位置回到独立缺口码并拒绝整周准入。"""
    _enable_p0a(monkeypatch)

    def unavailable(week_no, content_dir=None, protocol=None):
        raise content.FrozenContentUnavailable("测试:数据包不可用")

    monkeypatch.setattr(
        content, "load_autopilot_interaction_package", unavailable)
    with Session(service_engine) as db:
        _seed_ready(db, bank=BANK)
        with pytest.raises(AutopilotServiceError) as caught:
            _start(db, bank=BANK)
        assert caught.value.code == "autopilot_plan_not_fully_supported"
        assert caught.value.context["unsupported_position_count"] == 58
        assert caught.value.context["first_gap"] == {
            "code": "interaction_package_unavailable",
            "item_id": "DE_烟灰缸+烟",
            "turn_seq": 1,
            "response_role": "左命名",
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
            "takeover_ready": False,
            "current_command_kind": None,
            "position_item_id": None,
            "position_turn_seq": None,
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
            "takeover_ready": False,
            "current_command_kind": "tts",
            "position_item_id": FIRST_ONLY_BANK.single_element[0]["item_id"],
            "position_turn_seq": 1,
            "last_error_code": None,
        }
        assert _all_keys(active.model_dump()).isdisjoint({
            "command", "payload", "speech_text", "image_id", "item_ref",
            "item_id", "turn_key", "target_word", "answer",
            "issued_capability_token_hash", "issued_device_id_hash",
        })

        # takeover 端点只可能停在 paused/scope_completed/failed 且不留当前命令
        # （见 takeover_autopilot_to_manual 的 pause_required 与 CAS 条件）。把
        # 执行中的 waiting_tts 直接改成 manual 是安全释放契约里造不出来的组合，
        # 必须拒绝，而不是当成一次已完成的人工接管投影出去。
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None
        state.mode = "manual"
        db.commit()
        with pytest.raises(AutopilotServiceError) as unsafe_manual:
            get_autopilot_status(db, session_id=SESSION_ID)
        assert unsafe_manual.value.code == "autopilot_state_invalid"

        # 服务器仍持有控制权时不得声明空闲。
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None
        state.mode = "autonomous"
        state.status = "idle"
        state.current_command_id = None
        db.commit()
        with pytest.raises(AutopilotServiceError) as owned_idle:
            get_autopilot_status(db, session_id=SESSION_ID)
        assert owned_idle.value.code == "autopilot_state_invalid"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("research_session", "autopilot_real_sessions_disabled"),
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


def _cross_attempt_snapshot(db: Session) -> tuple:
    state = db.get(SessionAutopilotState, SESSION_ID)
    runtime = db.get(SessionRuntimeState, SESSION_ID)
    commands = [
        (c.id, c.kind, c.state, c.prompt_level, c.attempt_seq)
        for c in db.exec(select(RuntimeCommand).where(
            RuntimeCommand.session_id == SESSION_ID,
        ).order_by(RuntimeCommand.command_seq))
    ]
    events = [
        (e.event_type, e.reason_code, e.command_id)
        for e in db.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.session_id == SESSION_ID,
        ).order_by(AutopilotControlEvent.event_seq))
    ]
    attempts = [
        (a.id, a.processing_status, a.error_code)
        for a in db.exec(select(AttemptEvent).where(
            AttemptEvent.session_id == SESSION_ID,
        ).order_by(AttemptEvent.id))
    ]
    return (
        (state.status, state.current_command_id, state.revision,
         state.last_error_code, state.control_generation, state.runner_generation,
         state.lease_owner) if state is not None else None,
        (runtime.status, runtime.revision) if runtime is not None else None,
        tuple(commands), tuple(events), tuple(attempts),
    )


def test_stale_target_from_earlier_logical_attempt_is_pure_noop(
    service_engine, monkeypatch,
):
    """R1-foundation A: a target frozen on a completed-and-routed earlier
    attempt must never mutate the session once a newer logical attempt (a
    different record/raw_audio_id, same generation) is current — covers the
    stale-ASR/stale-judgement/route-lag failure shape at the shared CAS layer
    every one of those call sites funnels through.
    """
    _enable_p0a(monkeypatch)
    monkeypatch.setattr(content, "load_item_bank", lambda _path: FIRST_ONLY_BANK)
    with Session(service_engine) as db:
        _seed_ready(db)
        _seed_completed_autopilot_attempt(db, prompt_level=1)
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None and state.status == "processing_attempt"
        stale_record = db.exec(select(RuntimeCommand).where(
            RuntimeCommand.session_id == SESSION_ID,
            RuntimeCommand.kind == "record",
            RuntimeCommand.prompt_level == 0,
        )).one()
        stale_target = autopilot_orchestration.FrozenWorkerTarget(
            session_id=SESSION_ID,
            record_command_id=stale_record.id,
            raw_audio_id=stale_record.expected_raw_audio_id,
            control_generation=state.control_generation,
            runner_generation=state.runner_generation,
        )
        assert autopilot_orchestration.worker_target_still_current(
            db, stale_target) is False
        before = _cross_attempt_snapshot(db)

        staged = autopilot_orchestration.stage_processing_failure(
            db, session_id=SESSION_ID, error_code="autopilot_worker_exception",
            source="worker_exception", target=stale_target)
        assert staged is False
        db.rollback()

        assert _cross_attempt_snapshot(db) == before


def test_matching_target_pauses_exactly_once_and_clears_only_its_claims(
    service_engine, monkeypatch,
):
    """Positive case: a target that is still exactly current stages the
    failure pause exactly once; a repeat call against the same (now stale)
    target is a pure no-op.
    """
    _enable_p0a(monkeypatch)
    monkeypatch.setattr(content, "load_item_bank", lambda _path: FIRST_ONLY_BANK)
    with Session(service_engine) as db:
        _seed_ready(db)
        _seed_completed_autopilot_attempt(db, prompt_level=1)
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None and state.status == "processing_attempt"
        current_target = autopilot_orchestration.derive_worker_target(
            db, session_id=SESSION_ID)
        assert autopilot_orchestration.worker_target_still_current(
            db, current_target) is True

        staged = autopilot_orchestration.stage_processing_failure(
            db, session_id=SESSION_ID, error_code="autopilot_worker_exception",
            source="worker_exception", target=current_target)
        assert staged is True
        db.commit()

        after = db.get(SessionAutopilotState, SESSION_ID)
        assert after is not None
        assert after.status == "paused"
        assert after.current_command_id is None
        assert after.last_error_code == "autopilot_worker_exception"
        events = list(db.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.session_id == SESSION_ID,
            AutopilotControlEvent.event_type == "failure",
        )))
        assert len(events) == 1
        assert events[0].command_id == current_target.record_command_id

        # A second attempt against the same (now stale, since state moved to
        # paused) target must not double-pause or write a second event.
        again = autopilot_orchestration.stage_processing_failure(
            db, session_id=SESSION_ID, error_code="autopilot_worker_exception",
            source="worker_exception", target=current_target)
        assert again is False
        db.rollback()
        events_again = list(db.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.session_id == SESSION_ID,
            AutopilotControlEvent.event_type == "failure",
        )))
        assert len(events_again) == 1


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
            "takeover_ready": False,
            "current_command_kind": None,
            "position_item_id": None,
            "position_turn_seq": None,
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


def _drive_to_waiting_record(db: Session) -> tuple[RuntimeCommand, RuntimeCommand]:
    """Persist the question-TTS -> record handoff through the service ACK path."""
    _start(db)
    db.commit()
    tts = _current_tts(db)
    _apply_ack(db, tts, AutopilotAckIn(
        idempotency_key="ack-status-contract-tts-ended",
        ack_type="tts_ended",
        control_generation=1,
        runner_generation=1,
        command_revision=0,
        device_event_seq=1,
        media_ended=True,
    ))
    db.commit()
    state = db.get(SessionAutopilotState, SESSION_ID)
    assert state is not None and state.status == "waiting_recording"
    record = db.get(RuntimeCommand, state.current_command_id)
    assert record is not None and record.kind == "record"
    assert record.state == "pending"
    return tts, record


def _ack_record_started(db: Session, record_id: int) -> None:
    _apply_ack(db, db.get(RuntimeCommand, record_id), AutopilotAckIn(
        idempotency_key="ack-status-contract-record-started",
        ack_type="record_started",
        control_generation=1,
        runner_generation=1,
        command_revision=0,
        device_event_seq=2,
        mime_type="audio/webm;codecs=opus",
        sample_rate_hz=48_000,
        channels=1,
    ), now=NOW + timedelta(seconds=1))
    db.commit()


def _ack_record_stopped(db: Session, record: RuntimeCommand) -> None:
    """Complete the capture proof chain so the record command reaches succeeded."""
    raw_audio_id = record.expected_raw_audio_id
    assert raw_audio_id is not None
    checksum = "c" * 64
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
    record_id = record.id
    _ack_record_started(db, record_id)
    _apply_ack(db, db.get(RuntimeCommand, record_id), AutopilotAckIn(
        idempotency_key="ack-status-contract-record-stopped",
        ack_type="record_stopped",
        control_generation=1,
        runner_generation=1,
        command_revision=1,
        device_event_seq=3,
        raw_audio_id=raw_audio_id,
        receipt_server_seq=receipt.server_seq,
        checksum=checksum,
        byte_count=2048,
        duration_seconds=2.5,
        stop_reason="user_done",
    ), now=NOW + timedelta(seconds=2))
    db.commit()
    stored = db.get(RuntimeCommand, record_id)
    assert stored is not None and stored.state == "succeeded"


def test_account_status_projects_processing_attempt_with_the_succeeded_record(
        service_engine, monkeypatch):
    """processing_attempt 保留的是已完成的 record 命令，不是状态机矛盾。"""
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        tts, record = _drive_to_waiting_record(db)
        record_id = record.id
        _ack_record_stopped(db, record)
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None and state.status == "processing_attempt"
        assert state.current_command_id == record_id

        projected = get_autopilot_status(db, session_id=SESSION_ID)
        assert projected.scope_key == P0A_SCOPE_KEY
        assert projected.mode == "autonomous"
        assert projected.status == "processing_attempt"
        # 所有权穿越处理中状态，本修复不解锁人工控制。
        assert projected.server_owned is True
        assert projected.current_command_kind == "record"
        assert projected.last_error_code is None
        assert _all_keys(projected.model_dump()).isdisjoint({
            "command", "payload", "speech_text", "image_id", "item_ref",
            "item_id", "turn_key", "target_word", "answer", "raw_audio_id",
            "checksum", "issued_capability_token_hash", "issued_device_id_hash",
        })

        # 已完成的录音命令不是收麦持久态。
        state = db.get(SessionAutopilotState, SESSION_ID)
        state.status = "manual_draining"
        db.commit()
        with pytest.raises(AutopilotServiceError) as drained_terminal:
            get_autopilot_status(db, session_id=SESSION_ID)
        assert drained_terminal.value.code == "autopilot_state_invalid"

        # processing_attempt 只对应 record；指回 TTS 命令必须拒绝。
        state = db.get(SessionAutopilotState, SESSION_ID)
        state.status = "processing_attempt"
        state.current_command_id = tts.id
        db.commit()
        with pytest.raises(AutopilotServiceError) as wrong_kind:
            get_autopilot_status(db, session_id=SESSION_ID)
        assert wrong_kind.value.code == "autopilot_state_invalid"

        # 两个 generation 的漂移都仍先于 status↔kind 匹配被拒。
        state = db.get(SessionAutopilotState, SESSION_ID)
        state.current_command_id = record_id
        db.commit()
        for fence in ("control_generation", "runner_generation"):
            state = db.get(SessionAutopilotState, SESSION_ID)
            original = getattr(state, fence)
            setattr(state, fence, original + 1)
            db.commit()
            with pytest.raises(AutopilotServiceError) as stale_generation:
                get_autopilot_status(db, session_id=SESSION_ID)
            assert stale_generation.value.code == "autopilot_state_invalid"
            state = db.get(SessionAutopilotState, SESSION_ID)
            setattr(state, fence, original)
            db.commit()

        restored = get_autopilot_status(db, session_id=SESSION_ID)
        assert restored.status == "processing_attempt"
        assert restored.current_command_kind == "record"


def test_account_status_rejects_processing_attempt_without_a_finished_record(
        service_engine, monkeypatch):
    """处理中状态只接受已终态成功的录音命令，pending/started 一律拒绝。"""
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _tts, record = _drive_to_waiting_record(db)
        record_id = record.id

        # 同一条命令在 waiting_recording 下是合法的，所以下面的拒绝只可能来自
        # 状态改动本身，不是夹具把命令链搭坏了。
        pending = get_autopilot_status(db, session_id=SESSION_ID)
        assert pending.status == "waiting_recording"
        assert pending.current_command_kind == "record"
        assert pending.server_owned is True

        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None
        state.status = "processing_attempt"
        db.commit()
        with pytest.raises(AutopilotServiceError) as still_pending:
            get_autopilot_status(db, session_id=SESSION_ID)
        assert still_pending.value.code == "autopilot_state_invalid"

        state = db.get(SessionAutopilotState, SESSION_ID)
        state.status = "waiting_recording"
        db.commit()
        _ack_record_started(db, record_id)
        assert db.get(RuntimeCommand, record_id).state == "started"

        started = get_autopilot_status(db, session_id=SESSION_ID)
        assert started.status == "waiting_recording"
        assert started.current_command_kind == "record"
        assert started.server_owned is True

        state = db.get(SessionAutopilotState, SESSION_ID)
        state.status = "processing_attempt"
        db.commit()
        with pytest.raises(AutopilotServiceError) as still_started:
            get_autopilot_status(db, session_id=SESSION_ID)
        assert still_started.value.code == "autopilot_state_invalid"


def test_account_status_accepts_only_claimable_record_for_manual_draining(
        service_engine, monkeypatch):
    """manual_draining 是 autonomous + server_owned 的安全收麦持久态。

    生产目前没有已确认的写入者；这里只钉合法持久态契约，不代表该路径可执行。
    """
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        tts, record = _drive_to_waiting_record(db)
        record_id = record.id
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None
        state.status = "manual_draining"
        db.commit()

        pending = get_autopilot_status(db, session_id=SESSION_ID)
        assert pending.status == "manual_draining"
        assert pending.current_command_kind == "record"
        assert pending.mode == "autonomous"
        assert pending.server_owned is True

        state = db.get(SessionAutopilotState, SESSION_ID)
        state.current_command_id = tts.id
        db.commit()
        with pytest.raises(AutopilotServiceError) as wrong_kind:
            get_autopilot_status(db, session_id=SESSION_ID)
        assert wrong_kind.value.code == "autopilot_state_invalid"

        state = db.get(SessionAutopilotState, SESSION_ID)
        state.status = "waiting_recording"
        state.current_command_id = record_id
        db.commit()
        _ack_record_started(db, record_id)
        state = db.get(SessionAutopilotState, SESSION_ID)
        state.status = "manual_draining"
        db.commit()

        started = get_autopilot_status(db, session_id=SESSION_ID)
        assert started.status == "manual_draining"
        assert started.current_command_kind == "record"
        assert started.server_owned is True


@pytest.mark.parametrize("status", ["processing_attempt", "manual_draining"])
def test_autopilot_state_cannot_persist_those_statuses_without_a_command(
        service_engine, monkeypatch, status):
    """空 current_command_id 的处理中/收麦态在持久化层就被挡住。

    只证明这一格进不了库，所以 get_autopilot_status 收不到它；不代表服务层对
    这种行有任何额外契约，也不是一条可达状态。
    """
    _enable_p0a(monkeypatch)
    with Session(service_engine) as db:
        _seed_ready(db)
        _drive_to_waiting_record(db)
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None
        state.status = status
        state.current_command_id = None
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert db.get(
            SessionAutopilotState, SESSION_ID).status == "waiting_recording"


# ===========================================================================
# 交互数据包驱动的双要素(Shape B)/多要素(Shape C)自动带练引擎。
# ===========================================================================


DOUBLE_ONLY_BANK = replace(
    BANK,
    single_element=[],
    double_element=copy.deepcopy(BANK.double_element[:1]),
    multi_element=[],
    meta=_test_subset_meta(5),
)
MULTI_ONLY_BANK = replace(
    BANK,
    single_element=[],
    double_element=[],
    multi_element=copy.deepcopy(BANK.multi_element[:1]),
    meta=_test_subset_meta(4),
)
REAL_WK2_PACKAGE = content.load_autopilot_interaction_package(
    2, protocol=PROTOCOL)
_IX_STEP = _itertools_count(1)


def _ix_now() -> datetime:
    """严格单调的驱动时钟:每步 60s,保证回执/收据永远晚于命令发行。"""
    return NOW + timedelta(seconds=60 * next(_IX_STEP))


def _subset_interaction_package(bank: content.ItemBank) -> dict:
    package = copy.deepcopy(REAL_WK2_PACKAGE)
    keep = {row["item_id"]
            for row in (*bank.double_element, *bank.multi_element)}
    package["items"] = [
        item for item in package["items"] if item["item_id"] in keep]
    package["parent_item_bank_definition_digest"] = (
        content.item_bank_definition_digest(bank))
    return package


def _install_package(monkeypatch, bank: content.ItemBank) -> dict:
    package = _subset_interaction_package(bank)
    assert content.validate_autopilot_interaction_package(
        package, bank, PROTOCOL) == []
    monkeypatch.setattr(
        content, "load_autopilot_interaction_package",
        lambda week_no, content_dir=None, protocol=None: copy.deepcopy(package))
    return package


def _next_device_event_seq(db: Session) -> int:
    latest = db.exec(select(RuntimeCommandAck).where(
        RuntimeCommandAck.session_id == SESSION_ID,
        RuntimeCommandAck.device_id_hash == DEVICE_HASH,
    ).order_by(RuntimeCommandAck.device_event_seq.desc())).first()
    return latest.device_event_seq + 1 if latest is not None else 1


def _current_command(db: Session) -> RuntimeCommand:
    state = db.get(SessionAutopilotState, SESSION_ID)
    assert state is not None and state.current_command_id is not None
    command = db.get(RuntimeCommand, state.current_command_id)
    assert command is not None
    return command


def _ix_tts_ended(db: Session, tts: RuntimeCommand, *, bank: content.ItemBank):
    now = _ix_now()
    result = _apply_ack(db, tts, AutopilotAckIn(
        idempotency_key=f"ack-ix-tts-ended-{now.timestamp():.0f}",
        ack_type="tts_ended",
        control_generation=tts.control_generation,
        runner_generation=tts.runner_generation,
        command_revision=tts.revision,
        device_event_seq=_next_device_event_seq(db),
        media_ended=True,
    ), bank=bank, now=now)
    db.commit()
    return result


def _ix_record_stopped(db: Session, record: RuntimeCommand,
                       *, bank: content.ItemBank) -> None:
    now = _ix_now()
    step = next(_IX_STEP)
    checksum = format(step, "x").rjust(64, "0")
    byte_count = 4096 + step
    raw_audio_id = record.expected_raw_audio_id
    assert raw_audio_id is not None
    asset = db.get(AudioAssetRow, raw_audio_id)
    assert asset is not None
    asset.checksum = checksum
    asset.byte_count = byte_count
    asset.uploaded_at = now + timedelta(seconds=1)
    receipt = AudioCaptureReceipt(
        raw_audio_id=raw_audio_id,
        session_id=SESSION_ID,
        turn_key=record.turn_key,
        received_at=now + timedelta(seconds=1),
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
    result = _apply_ack(db, record, AutopilotAckIn(
        idempotency_key=f"ack-ix-rec-stop-{step:04d}",
        ack_type="record_stopped",
        control_generation=record.control_generation,
        runner_generation=record.runner_generation,
        command_revision=record.revision,
        device_event_seq=_next_device_event_seq(db),
        raw_audio_id=raw_audio_id,
        receipt_server_seq=receipt.server_seq,
        checksum=checksum,
        byte_count=byte_count,
        duration_seconds=2.5,
        stop_reason="user_done",
    ), bank=bank, now=now + timedelta(seconds=2))
    db.commit()
    assert result.status == "processing_attempt"


def _ix_seed_judged_attempt(
    db: Session,
    record: RuntimeCommand,
    *,
    answer_type: str,
    target_hit: bool,
    response_role: str,
    cue_type: str | None = None,
    asr_text: str = "测试回答",
) -> AttemptEvent:
    score = 1.0 if target_hit else 0.5 if answer_type == "部分正确" else 0.0
    attempt = AttemptEvent(
        session_id=SESSION_ID,
        item_id=record.item_id,
        turn_seq=record.turn_seq,
        response_role=response_role,
        attempt_seq=record.attempt_seq,
        raw_audio_id=record.expected_raw_audio_id,
        prompt_level=record.prompt_level,
        cue_type=cue_type,
        duration_seconds=2.5,
        asr_text=asr_text,
        asr_confidence=0.91,
        asr_engine_version="test-asr-v1",
        operational_answer_type=answer_type,
        operational_score=score,
        operational_needs_review=True,
        judge_mode="版本化规则",
        judge_engine_version="rubric/test-r1",
        judge_reason=None,
        matched_on="target" if target_hit else "none",
        contains_target=target_hit,
        judge_portrait_used=False,
        processing_status="completed",
        processing_generation=1,
        processed_at=_ix_now(),
        created_at=NOW + timedelta(seconds=2),
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
                "needs_review": True,
                "judge_mode": "版本化规则",
                "judge_engine_version": "rubric/test-r1",
                "matched_on": attempt.matched_on,
                "contains_target": target_hit,
                "truth_scope": "operational_only",
            }),
        created_at=NOW + timedelta(seconds=3),
        is_simulation=True,
    ))
    db.commit()
    stored = db.get(AttemptEvent, attempt.id)
    assert stored is not None
    return stored


def _ix_route(db: Session, attempt_id: int, *, bank: content.ItemBank):
    result = route_completed_attempt(
        db,
        session_id=SESSION_ID,
        attempt_id=attempt_id,
        bank=bank,
        protocol=PROTOCOL,
        now=_ix_now(),
    )
    db.commit()
    return result


def _ix_start(db: Session, *, bank: content.ItemBank):
    # 每个用例自己的时钟从头走:全局累计会超过设备能力 1 小时有效期。
    global _IX_STEP
    _IX_STEP = _itertools_count(1)
    created = _start(db, bank=bank)
    db.commit()
    return created


def _ix_answer_current_record(
    db: Session, *, bank: content.ItemBank, answer_type: str,
    target_hit: bool, response_role: str, cue_type: str | None = None,
    asr_text: str = "测试回答",
) -> AttemptEvent:
    record = _current_command(db)
    assert record.kind == "record"
    _ix_record_stopped(db, record, bank=bank)
    record = db.get(RuntimeCommand, record.id)
    return _ix_seed_judged_attempt(
        db, record, answer_type=answer_type, target_hit=target_hit,
        response_role=response_role, cue_type=cue_type, asr_text=asr_text)


def _namefix(side: str, bank: content.ItemBank) -> str:
    word = bank.double_element[0][f"{side}_word"]
    return PROTOCOL["double"][f"namefix_{side}"].replace("【物品名】", word)


def _command_stream(db: Session) -> list[tuple]:
    rows = list(db.exec(select(RuntimeCommand).order_by(
        RuntimeCommand.command_seq)))
    stream = []
    for row in rows:
        if row.kind == "tts":
            payload = json.loads(row.payload_json)
            stream.append((
                "tts", payload["purpose"], row.turn_key, row.prompt_level,
                row.attempt_seq, payload["speech_text"],
                payload.get("response_path")))
        else:
            stream.append((
                "record", None, row.turn_key, row.prompt_level,
                row.attempt_seq, None, None))
    return stream


def test_shape_b_full_hit_advances_silently_and_issues_next_question(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    _install_package(monkeypatch, DOUBLE_ONLY_BANK)
    item_id = DOUBLE_ONLY_BANK.double_element[0]["item_id"]
    with Session(service_engine) as db:
        _seed_ready(db, bank=DOUBLE_ONLY_BANK)
        created = _ix_start(db, bank=DOUBLE_ONLY_BANK)
        assert created.command is not None
        assert created.command.payload.purpose == "question"
        assert created.command.payload.speech_text == (
            "请看这张图片，您能告诉我图上左边的物品是什么吗？")

        tts = _current_command(db)
        routed = _ix_tts_ended(db, tts, bank=DOUBLE_ONLY_BANK)
        assert routed.status == "waiting_recording"
        attempt = _ix_answer_current_record(
            db, bank=DOUBLE_ONLY_BANK, answer_type="正确", target_hit=True,
            response_role="左命名")
        result = _ix_route(db, attempt.id, bank=DOUBLE_ONLY_BANK)
        # 命中:无口头反馈,直接推进并下发下一环节问句。
        assert result.status == "waiting_tts"
        assert result.replayed is False
        assert result.command is not None
        assert result.command.kind == "tts"
        assert result.command.payload.purpose == "question"
        assert result.command.turn_seq == 2
        assert result.command.payload.speech_text == "请告诉我它有什么作用或特点？"
        # 命中环节仍然收口一条 TurnEvent(自动执行永不写研究真值)。
        items = list(db.exec(select(ItemEvent)))
        turns = list(db.exec(select(TurnEvent)))
        assert len(items) == 1 and items[0].item_id == item_id
        assert len(turns) == 1
        assert turns[0].turn_seq == 1
        assert turns[0].response_role == "左命名"
        assert turns[0].ai_score == 1.0
        assert turns[0].score_locked is False
        assert turns[0].reviewed_score is None
        # 重放同一 attempt:幂等返回同一条下一问命令。
        replay = _ix_route(db, attempt.id, bank=DOUBLE_ONLY_BANK)
        assert replay.replayed is True
        assert replay.status == "waiting_tts"
        assert replay.command.command_key == result.command.command_key
        assert len(list(db.exec(select(TurnEvent)))) == 1


def test_shape_b_miss_speaks_correction_then_advances(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    _install_package(monkeypatch, DOUBLE_ONLY_BANK)
    with Session(service_engine) as db:
        _seed_ready(db, bank=DOUBLE_ONLY_BANK)
        _ix_start(db, bank=DOUBLE_ONLY_BANK)
        _ix_tts_ended(db, _current_command(db), bank=DOUBLE_ONLY_BANK)
        attempt = _ix_answer_current_record(
            db, bank=DOUBLE_ONLY_BANK, answer_type="偏题", target_hit=False,
            response_role="左命名")
        result = _ix_route(db, attempt.id, bank=DOUBLE_ONLY_BANK)
        assert result.status == "waiting_tts"
        assert result.command.payload.purpose == "tell_answer"
        assert result.command.turn_seq == 1
        assert result.command.prompt_level == 3
        assert result.command.attempt_seq == 1
        assert result.command.payload.speech_text == _namefix(
            "left", DOUBLE_ONLY_BANK)
        # 纠正句读完:tts_ended → advance → 下一环节问句。
        tell = _current_command(db)
        routed = _ix_tts_ended(db, tell, bank=DOUBLE_ONLY_BANK)
        assert routed.status == "waiting_tts"
        assert routed.command is not None
        assert routed.command.payload.purpose == "question"
        assert routed.command.turn_seq == 2
        # 未命中也收口 TurnEvent。
        turns = list(db.exec(select(TurnEvent)))
        assert len(turns) == 1 and turns[0].ai_score == 0.0


def test_shape_b_llm_semantic_correct_without_lexical_hit_does_not_advance(
        service_engine, monkeypatch):
    """推进成败只认二值 contains_target;语义"正确"但词法未命中仍走纠正。"""
    _enable_p0a(monkeypatch)
    _install_package(monkeypatch, DOUBLE_ONLY_BANK)
    with Session(service_engine) as db:
        _seed_ready(db, bank=DOUBLE_ONLY_BANK)
        _ix_start(db, bank=DOUBLE_ONLY_BANK)
        _ix_tts_ended(db, _current_command(db), bank=DOUBLE_ONLY_BANK)
        attempt = _ix_answer_current_record(
            db, bank=DOUBLE_ONLY_BANK, answer_type="正确", target_hit=False,
            response_role="左命名")
        result = _ix_route(db, attempt.id, bank=DOUBLE_ONLY_BANK)
        assert result.command.payload.purpose == "tell_answer"


def test_shape_b_five_turns_complete_scope_with_one_item_five_turn_events(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    _install_package(monkeypatch, DOUBLE_ONLY_BANK)
    item_id = DOUBLE_ONLY_BANK.double_element[0]["item_id"]
    roles = ("左命名", "左作用", "右命名", "右作用", "关系识别")
    with Session(service_engine) as db:
        _seed_ready(db, bank=DOUBLE_ONLY_BANK)
        _ix_start(db, bank=DOUBLE_ONLY_BANK)
        last_result = None
        for index, role in enumerate(roles):
            tts = _current_command(db)
            assert tts.kind == "tts" and tts.turn_seq == index + 1
            _ix_tts_ended(db, tts, bank=DOUBLE_ONLY_BANK)
            attempt = _ix_answer_current_record(
                db, bank=DOUBLE_ONLY_BANK, answer_type="正确",
                target_hit=True, response_role=role)
            last_result = _ix_route(db, attempt.id, bank=DOUBLE_ONLY_BANK)
        assert last_result is not None
        assert last_result.status == "scope_completed"
        assert last_result.command is None
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None
        assert state.status == "scope_completed"
        assert state.current_command_id is None
        events = list(db.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.event_type == "scope_complete")))
        assert len(events) == 1
        # 研究行面:1 双要素题 = 1 ItemEvent + 5 TurnEvent,评分键与冻结计划一致。
        items = list(db.exec(select(ItemEvent)))
        assert len(items) == 1
        assert items[0].item_id == item_id
        assert getattr(items[0].task_type, "value", items[0].task_type) == "双要素"
        turns = sorted(
            db.exec(select(TurnEvent)), key=lambda turn: turn.turn_seq)
        assert [turn.turn_seq for turn in turns] == [1, 2, 3, 4, 5]
        assert [turn.response_role for turn in turns] == list(roles)
        plan = runtime.build_session_plan(DOUBLE_ONLY_BANK, 2, "正式训练")
        assert [
            plan_turn.scoring_key for plan_turn in plan.items[0].turns
        ] == ["left_name", "left_function", "right_name", "right_function",
              "relation"]
        assert all(turn.score_locked is False for turn in turns)
        assert all(turn.reviewed_score is None for turn in turns)
        # 全命中场次的完整命令流水:每环节 question+record,无反馈 TTS。
        stream = _command_stream(db)
        assert [entry[:2] for entry in stream] == [
            ("tts", "question"), ("record", None),
        ] * 5
        # 幂等重放 scope 完成。
        final_attempt = db.exec(select(AttemptEvent).where(
            AttemptEvent.turn_seq == 5)).first()
        replay = _ix_route(db, final_attempt.id, bank=DOUBLE_ONLY_BANK)
        assert replay.replayed is True
        assert replay.status == "scope_completed"
        assert replay.command is None


def test_shape_c_full_first_recording_speaks_feedback_then_advances(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    _install_package(monkeypatch, MULTI_ONLY_BANK)
    with Session(service_engine) as db:
        _seed_ready(db, bank=MULTI_ONLY_BANK)
        created = _ix_start(db, bank=MULTI_ONLY_BANK)
        assert created.command.payload.speech_text == (
            "您好，您看看这张图片，这里像是什么地方？")
        _ix_tts_ended(db, _current_command(db), bank=MULTI_ONLY_BANK)
        attempt = _ix_answer_current_record(
            db, bank=MULTI_ONLY_BANK, answer_type="正确", target_hit=True,
            response_role="情境")
        result = _ix_route(db, attempt.id, bank=MULTI_ONLY_BANK)
        assert result.status == "waiting_tts"
        assert result.command.payload.purpose == "feedback"
        assert result.command.prompt_level == 0
        assert result.command.attempt_seq == 1
        assert result.command.payload.speech_text == "很好，您观察得很准确。"
        assert result.command.payload.response_path is None
        routed = _ix_tts_ended(db, _current_command(db), bank=MULTI_ONLY_BANK)
        assert routed.command.payload.purpose == "question"
        assert routed.command.turn_seq == 2


def test_shape_c_wrong_first_recording_cues_and_rerecords(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    _install_package(monkeypatch, MULTI_ONLY_BANK)
    with Session(service_engine) as db:
        _seed_ready(db, bank=MULTI_ONLY_BANK)
        _ix_start(db, bank=MULTI_ONLY_BANK)
        _ix_tts_ended(db, _current_command(db), bank=MULTI_ONLY_BANK)
        attempt = _ix_answer_current_record(
            db, bank=MULTI_ONLY_BANK, answer_type="未识别", target_hit=False,
            response_role="情境")
        result = _ix_route(db, attempt.id, bank=MULTI_ONLY_BANK)
        assert result.command.payload.purpose == "cue"
        assert result.command.prompt_level == 1
        assert result.command.attempt_seq == 2
        assert result.command.payload.response_path == "unknown"
        assert result.command.payload.speech_text == (
            "您再看看上边的指示牌，上面写着什么？")
        # cue 读完 → 第二次录音。
        routed = _ix_tts_ended(db, _current_command(db), bank=MULTI_ONLY_BANK)
        assert routed.status == "waiting_recording"
        record = _current_command(db)
        assert record.kind == "record"
        assert record.prompt_level == 1 and record.attempt_seq == 2
        # 第二次仍错(含沉默同路):告知句 → 推进。
        attempt2 = _ix_answer_current_record(
            db, bank=MULTI_ONLY_BANK, answer_type="未识别", target_hit=False,
            response_role="情境", cue_type=INTERACTION_CUE_TYPE)
        result2 = _ix_route(db, attempt2.id, bank=MULTI_ONLY_BANK)
        assert result2.command.payload.purpose == "tell_answer"
        assert result2.command.prompt_level == 3
        assert result2.command.attempt_seq == 2
        assert result2.command.payload.speech_text == (
            "您看上面的这个牌子，上面写着‘动物园’，所以这里是动物园。")
        # 只有第二次(终值)收口 TurnEvent,中间失败尝试只在逐次账本。
        turns = list(db.exec(select(TurnEvent)))
        assert len(turns) == 1
        assert turns[0].prompt_level == 1
        assert turns[0].cue_type == INTERACTION_CUE_TYPE


def test_shape_c_full_after_rerecord_speaks_path_bound_feedback(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    _install_package(monkeypatch, MULTI_ONLY_BANK)
    with Session(service_engine) as db:
        _seed_ready(db, bank=MULTI_ONLY_BANK)
        _ix_start(db, bank=MULTI_ONLY_BANK)
        _ix_tts_ended(db, _current_command(db), bank=MULTI_ONLY_BANK)
        attempt = _ix_answer_current_record(
            db, bank=MULTI_ONLY_BANK, answer_type="偏题", target_hit=False,
            response_role="情境")
        _ix_route(db, attempt.id, bank=MULTI_ONLY_BANK)
        _ix_tts_ended(db, _current_command(db), bank=MULTI_ONLY_BANK)
        attempt2 = _ix_answer_current_record(
            db, bank=MULTI_ONLY_BANK, answer_type="正确", target_hit=True,
            response_role="情境", cue_type=INTERACTION_CUE_TYPE)
        result = _ix_route(db, attempt2.id, bank=MULTI_ONLY_BANK)
        assert result.command.payload.purpose == "feedback"
        assert result.command.prompt_level == 1
        assert result.command.attempt_seq == 2
        assert result.command.payload.response_path == "unknown"
        assert result.command.payload.speech_text == "很好，您观察得很准确。"


def test_shape_c_silence_first_recording_tells_and_advances(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    _install_package(monkeypatch, MULTI_ONLY_BANK)
    with Session(service_engine) as db:
        _seed_ready(db, bank=MULTI_ONLY_BANK)
        _ix_start(db, bank=MULTI_ONLY_BANK)
        _ix_tts_ended(db, _current_command(db), bank=MULTI_ONLY_BANK)
        attempt = _ix_answer_current_record(
            db, bank=MULTI_ONLY_BANK, answer_type="沉默", target_hit=False,
            response_role="情境", asr_text="")
        result = _ix_route(db, attempt.id, bank=MULTI_ONLY_BANK)
        assert result.command.payload.purpose == "tell_answer"
        assert result.command.prompt_level == 3
        assert result.command.attempt_seq == 1
        assert result.command.payload.speech_text == (
            "您看上面的这个牌子，上面写着‘动物园’，所以这里是动物园。")


def test_shape_c_partial_maps_to_close_path_only_with_partial_branch(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    _install_package(monkeypatch, MULTI_ONLY_BANK)
    with Session(service_engine) as db:
        _seed_ready(db, bank=MULTI_ONLY_BANK)
        _ix_start(db, bank=MULTI_ONLY_BANK)
        # 环节1(无 partial 分支):部分正确归 wrong → unknown 路。
        _ix_tts_ended(db, _current_command(db), bank=MULTI_ONLY_BANK)
        attempt = _ix_answer_current_record(
            db, bank=MULTI_ONLY_BANK, answer_type="部分正确", target_hit=False,
            response_role="情境")
        result = _ix_route(db, attempt.id, bank=MULTI_ONLY_BANK)
        assert result.command.payload.purpose == "cue"
        assert result.command.payload.response_path == "unknown"
        # 走完环节1(第二次命中)进入环节2(事物,有 partial 分支)。
        _ix_tts_ended(db, _current_command(db), bank=MULTI_ONLY_BANK)
        attempt2 = _ix_answer_current_record(
            db, bank=MULTI_ONLY_BANK, answer_type="正确", target_hit=True,
            response_role="情境", cue_type=INTERACTION_CUE_TYPE)
        _ix_route(db, attempt2.id, bank=MULTI_ONLY_BANK)
        _ix_tts_ended(db, _current_command(db), bank=MULTI_ONLY_BANK)
        question2 = _current_command(db)
        assert question2.turn_seq == 1 or question2.turn_seq == 2
        # 上一步 tts_ended 是反馈(1,2) → advance → 环节2问句;再读问句开麦。
        if question2.kind == "tts":
            _ix_tts_ended(db, question2, bank=MULTI_ONLY_BANK)
        record2 = _current_command(db)
        assert record2.kind == "record" and record2.turn_seq == 2
        attempt3 = _ix_answer_current_record(
            db, bank=MULTI_ONLY_BANK, answer_type="部分正确", target_hit=False,
            response_role="事物")
        result3 = _ix_route(db, attempt3.id, bank=MULTI_ONLY_BANK)
        assert result3.command.payload.purpose == "cue"
        assert result3.command.payload.response_path == "close"
        assert result3.command.payload.speech_text == (
            "非常好，图中有小兔子。您再看看，栅栏后面还有什么动物？")


def test_interaction_tts_semantics_reject_out_of_table_cells(
        service_engine, monkeypatch):
    _enable_p0a(monkeypatch)
    package = _install_package(monkeypatch, DOUBLE_ONLY_BANK)
    with Session(service_engine) as db:
        _seed_ready(db, bank=DOUBLE_ONLY_BANK)
        train_session = db.get(TrainSession, SESSION_ID)
        selected = _select_p0a_content(
            train_session, DOUBLE_ONLY_BANK, PROTOCOL,
            item_id=DOUBLE_ONLY_BANK.double_element[0]["item_id"],
            turn_seq=1, interaction_package=package)
        item_id = selected.item_id

        def command_for(purpose, level, seq, speech, path=None):
            fields = {
                "speech_key": "p0a.test.1",
                "speech_text": speech,
                "purpose": purpose,
                "item_id": item_id,
                "turn_seq": 1,
                "cue_level": level,
            }
            if path is not None:
                fields["response_path"] = path
            payload = TtsCommandPayload.model_validate(fields)
            command = RuntimeCommand(
                idempotency_key="cmd-semantics-probe",
                session_id=SESSION_ID,
                command_seq=1,
                item_id=item_id,
                turn_seq=1,
                turn_key=f"{item_id}#1",
                attempt_seq=seq,
                prompt_level=level,
                scope_key=P0A_SCOPE_KEY,
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
            return command, payload

        from app.autopilot_service import _validate_tts_semantics

        question = selected.question_text
        tell = _namefix("left", DOUBLE_ONLY_BANK)
        # 合法格:question(0,1) 与 tell_answer(3,1)。
        _validate_tts_semantics(
            *command_for("question", 0, 1, question), selected)
        _validate_tts_semantics(
            *command_for("tell_answer", 3, 1, tell), selected)
        # Shape B 表外格全部拒绝。
        for purpose, level, seq, speech, path in (
            ("feedback", 0, 1, question, None),      # full 分支是静默推进
            ("cue", 1, 2, tell, "unknown"),          # 本环节没有重录分支
            ("cue", 2, 3, tell, None),               # 交互环节没有二级提示
            ("tell_answer", 3, 3, tell, None),       # 交互环节没有第三次录音
            ("question", 0, 1, tell, None),          # 话术与冻结问句不一致
        ):
            with pytest.raises(AutopilotServiceError) as caught:
                _validate_tts_semantics(
                    *command_for(purpose, level, seq, speech, path), selected)
            assert caught.value.code == "autopilot_command_invalid"


def test_shape_b_record_completed_scope_supports_drain_and_takeover(
        service_engine, monkeypatch):
    """静默推进收尾的 scope(最后命令是 record)也要能派生收麦目标/接管。"""
    _enable_p0a(monkeypatch)
    _install_package(monkeypatch, DOUBLE_ONLY_BANK)
    roles = ("左命名", "左作用", "右命名", "右作用", "关系识别")
    with Session(service_engine) as db:
        _seed_ready(db, bank=DOUBLE_ONLY_BANK)
        _ix_start(db, bank=DOUBLE_ONLY_BANK)
        for role in roles:
            _ix_tts_ended(db, _current_command(db), bank=DOUBLE_ONLY_BANK)
            attempt = _ix_answer_current_record(
                db, bank=DOUBLE_ONLY_BANK, answer_type="正确",
                target_hit=True, response_role=role)
            _ix_route(db, attempt.id, bank=DOUBLE_ONLY_BANK)
        status = get_autopilot_status(db, session_id=SESSION_ID)
        assert status.status == "scope_completed"
        assert status.takeover_ready is True
        last_record = db.exec(select(RuntimeCommand).where(
            RuntimeCommand.kind == "record",
        ).order_by(RuntimeCommand.command_seq.desc())).first()
        target = get_drain_target(
            db,
            session_id=SESSION_ID,
            capability_token_hash=CAPABILITY_HASH,
        )
        assert target.command_key == last_record.idempotency_key


def test_frozen_attempt_context_generalizes_to_interaction_positions(
        monkeypatch):
    """orchestration 权威输入推导:双/多要素环节的角色与 cue_type 口径。"""
    from types import SimpleNamespace

    _install_package(monkeypatch, DOUBLE_ONLY_BANK)
    session = TrainSession(
        session_id="S-CTX",
        patient_id="P-CTX",
        week_no=2,
        phase_type=PhaseType.正式训练,
        event_line=EventLine.正式训练,
        item_bank_version_id=DOUBLE_ONLY_BANK.version_id,
        is_simulation=True,
        data_classification="simulation",
    )
    item_id = DOUBLE_ONLY_BANK.double_element[0]["item_id"]
    record = SimpleNamespace(item_id=item_id, turn_seq=1, prompt_level=0)
    role, cue_type = autopilot_orchestration._frozen_attempt_context(
        session, record, DOUBLE_ONLY_BANK, PROTOCOL)
    assert (role, cue_type) == ("左命名", None)
    # Shape B 环节没有第二次录音:一级录音命令是伪造链,拒绝。
    forged = SimpleNamespace(item_id=item_id, turn_seq=1, prompt_level=1)
    with pytest.raises(
            autopilot_orchestration.AutopilotOrchestrationError) as caught:
        autopilot_orchestration._frozen_attempt_context(
            session, forged, DOUBLE_ONLY_BANK, PROTOCOL)
    assert caught.value.code == "autopilot_attempt_plan_mismatch"

    _install_package(monkeypatch, MULTI_ONLY_BANK)
    multi_session = TrainSession(
        session_id="S-CTX-M",
        patient_id="P-CTX-M",
        week_no=2,
        phase_type=PhaseType.正式训练,
        event_line=EventLine.正式训练,
        item_bank_version_id=MULTI_ONLY_BANK.version_id,
        is_simulation=True,
        data_classification="simulation",
    )
    multi_id = MULTI_ONLY_BANK.multi_element[0]["item_id"]
    first = SimpleNamespace(item_id=multi_id, turn_seq=1, prompt_level=0)
    assert autopilot_orchestration._frozen_attempt_context(
        multi_session, first, MULTI_ONLY_BANK, PROTOCOL) == ("情境", None)
    second = SimpleNamespace(item_id=multi_id, turn_seq=1, prompt_level=1)
    assert autopilot_orchestration._frozen_attempt_context(
        multi_session, second, MULTI_ONLY_BANK, PROTOCOL) == (
        "情境", INTERACTION_CUE_TYPE)
    # 多要素环节4(动作)max_recordings=1:一级录音同样拒绝。
    no_retry = SimpleNamespace(item_id=multi_id, turn_seq=4, prompt_level=1)
    with pytest.raises(
            autopilot_orchestration.AutopilotOrchestrationError) as caught:
        autopilot_orchestration._frozen_attempt_context(
            multi_session, no_retry, MULTI_ONLY_BANK, PROTOCOL)
    assert caught.value.code == "autopilot_attempt_plan_mismatch"


def test_week3_interaction_selection_works_with_real_weekly_content():
    """周闸放宽到协议∩题库支持周:第 3 周真实题库+数据包可选出交互内容。"""
    week3_bank = content.load_item_bank_for_week(3)
    week3_package = content.load_autopilot_interaction_package(
        3, protocol=PROTOCOL)
    assert content.validate_autopilot_interaction_package(
        week3_package, week3_bank, PROTOCOL) == []
    session = TrainSession(
        session_id="S-WK3",
        patient_id="P-WK3",
        week_no=3,
        phase_type=PhaseType.正式训练,
        event_line=EventLine.正式训练,
        item_bank_version_id=week3_bank.version_id,
        item_bank_definition_digest=(
            content.item_bank_definition_digest(week3_bank)),
        autopilot_protocol_version_id=PROTOCOL["protocol_version_id"],
        autopilot_protocol_definition_digest=(
            content.autopilot_protocol_definition_digest(PROTOCOL)),
        is_simulation=True,
        data_classification="simulation",
    )
    first_double = week3_bank.double_element[0]
    selected = _select_p0a_content(
        session, week3_bank, PROTOCOL,
        item_id=first_double["item_id"], turn_seq=1,
        interaction_package=week3_package)
    assert selected.task_type == "双要素"
    assert selected.response_role == "左命名"
    assert selected.target_word == first_double["left_word"]
    assert selected.question_text
    assert selected.branches["full"].kind == "advance_silent"
    # 第 9 周不存在:周闸拒绝。
    session_bad = TrainSession(
        session_id="S-WK9",
        patient_id="P-WK9",
        week_no=9,
        phase_type=PhaseType.正式训练,
        event_line=EventLine.正式训练,
        item_bank_version_id=week3_bank.version_id,
        item_bank_definition_digest=(
            content.item_bank_definition_digest(week3_bank)),
        autopilot_protocol_version_id=PROTOCOL["protocol_version_id"],
        autopilot_protocol_definition_digest=(
            content.autopilot_protocol_definition_digest(PROTOCOL)),
        is_simulation=True,
        data_classification="simulation",
    )
    with pytest.raises(AutopilotServiceError) as caught:
        _select_p0a_content(
            session_bad, week3_bank, PROTOCOL,
            item_id=first_double["item_id"], turn_seq=1,
            interaction_package=week3_package)
    assert caught.value.code == "autopilot_scope_unsupported"


# ---------------------------------------------------------------------------
# Real research sessions: their own explicit channel, isolated from the two
# simulation switches, with a hard cloud-processing precondition.
# ---------------------------------------------------------------------------

CLOUD_PROVIDER_ID = "aliyun-dashscope"
CLOUD_NOTICE_VERSION = "notice-v1"


def _enable_cloud_policy(monkeypatch) -> None:
    monkeypatch.setenv("CLOUD_PROCESSING_PROVIDER_ID", CLOUD_PROVIDER_ID)
    monkeypatch.setenv("CLOUD_PROCESSING_NOTICE_VERSION", CLOUD_NOTICE_VERSION)


def _set_channels(monkeypatch, channels: str) -> None:
    monkeypatch.delenv(P0A_FEATURE_ENV, raising=False)
    monkeypatch.delenv("ALLOW_SIMULATION_DATA", raising=False)
    monkeypatch.delenv(REAL_SESSIONS_ENV, raising=False)
    if channels in {"simulation_only", "both"}:
        _enable_p0a(monkeypatch)
    if channels in {"real_only", "both"}:
        monkeypatch.setenv(REAL_SESSIONS_ENV, "1")


def _seed_research_ready(db: Session) -> None:
    """Same ready topology as _seed_ready, reclassified as one real session."""
    _seed_ready(db)
    patient = db.get(Patient, PATIENT_ID)
    patient.is_simulation_subject = False
    patient.cloud_processing_allowed = True
    patient.cloud_processing_provider_id = CLOUD_PROVIDER_ID
    patient.cloud_processing_notice_version = CLOUD_NOTICE_VERSION
    patient.cloud_processing_consented_at = NOW
    patient.cloud_processing_revoked_at = None
    train_session = db.get(TrainSession, SESSION_ID)
    train_session.is_simulation = False
    train_session.data_classification = "research"
    db.add(patient)
    db.add(train_session)
    db.commit()


@pytest.mark.parametrize(
    ("channels", "session_kind", "expected"),
    [
        ("none", "simulation", "autopilot_p0a_disabled"),
        # 真实研究场次缺的是 REAL_SESSIONS_ENV:双通道全关时也不得教人开模拟开关。
        ("none", "research", "autopilot_real_sessions_disabled"),
        ("simulation_only", "simulation", "started"),
        ("simulation_only", "research", "autopilot_real_sessions_disabled"),
        ("real_only", "simulation", "autopilot_p0a_disabled"),
        ("real_only", "research", "started"),
        ("both", "simulation", "started"),
        ("both", "research", "started"),
    ],
)
def test_channel_switch_matrix_keeps_simulation_and_real_sessions_apart(
        service_engine, monkeypatch, channels, session_kind, expected):
    _set_channels(monkeypatch, channels)
    _enable_cloud_policy(monkeypatch)
    with Session(service_engine) as db:
        if session_kind == "research":
            _seed_research_ready(db)
        else:
            _seed_ready(db)
        if expected == "started":
            result = _start(db)
            db.commit()
            assert result.status == "waiting_tts"
            assert result.replayed is False
        else:
            with pytest.raises(AutopilotServiceError) as caught:
                _start(db)
            assert caught.value.code == expected
            db.rollback()
            assert db.get(SessionAutopilotState, SESSION_ID) is None
            assert list(db.exec(select(RuntimeCommand))) == []


@pytest.mark.parametrize(
    ("spoil", ), [
        ("policy_unconfigured", ),
        ("not_allowed", ),
        ("provider_mismatch", ),
        ("notice_mismatch", ),
        ("consent_missing_timestamp", ),
        ("revoked", ),
    ],
)
def test_real_session_start_requires_current_cloud_processing_authorization(
        service_engine, monkeypatch, spoil):
    _set_channels(monkeypatch, "real_only")
    _enable_cloud_policy(monkeypatch)
    with Session(service_engine) as db:
        _seed_research_ready(db)
        patient = db.get(Patient, PATIENT_ID)
        if spoil == "policy_unconfigured":
            monkeypatch.delenv("CLOUD_PROCESSING_PROVIDER_ID")
            monkeypatch.delenv("CLOUD_PROCESSING_NOTICE_VERSION")
        elif spoil == "not_allowed":
            patient.cloud_processing_allowed = False
        elif spoil == "provider_mismatch":
            patient.cloud_processing_provider_id = "some-other-provider"
        elif spoil == "notice_mismatch":
            patient.cloud_processing_notice_version = "notice-v0"
        elif spoil == "consent_missing_timestamp":
            patient.cloud_processing_consented_at = None
        elif spoil == "revoked":
            patient.cloud_processing_revoked_at = NOW
        db.add(patient)
        db.commit()
        with pytest.raises(AutopilotServiceError) as caught:
            _start(db)
        assert caught.value.code == "autopilot_cloud_processing_required"


def test_real_session_start_rejects_simulation_subject_profile(
        service_engine, monkeypatch):
    _set_channels(monkeypatch, "both")
    _enable_cloud_policy(monkeypatch)
    with Session(service_engine) as db:
        _seed_research_ready(db)
        patient = db.get(Patient, PATIENT_ID)
        patient.is_simulation_subject = True
        db.add(patient)
        db.commit()
        with pytest.raises(AutopilotServiceError) as caught:
            _start(db)
        assert caught.value.code == "autopilot_simulation_subject_forbidden"


@pytest.mark.parametrize(
    ("is_simulation", "data_classification"),
    [
        (True, "research"),
        (False, "simulation"),
    ],
)
def test_unprovable_classification_pairs_are_rejected_with_both_channels_open(
        service_engine, monkeypatch, is_simulation, data_classification):
    _set_channels(monkeypatch, "both")
    _enable_cloud_policy(monkeypatch)
    with Session(service_engine) as db:
        _seed_research_ready(db)
        train_session = db.get(TrainSession, SESSION_ID)
        train_session.is_simulation = is_simulation
        train_session.data_classification = data_classification
        db.add(train_session)
        db.commit()
        with pytest.raises(AutopilotServiceError) as caught:
            _start(db)
        assert caught.value.code == "autopilot_classification_invalid"


def test_real_session_record_chain_carries_research_classification(
        service_engine, monkeypatch):
    _set_channels(monkeypatch, "real_only")
    _enable_cloud_policy(monkeypatch)
    with Session(service_engine) as db:
        _seed_research_ready(db)
        _start(db)
        db.commit()
        command = _current_tts(db)
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
        assert routed.command is not None and routed.command.kind == "record"
        record = db.exec(select(RuntimeCommand).where(
            RuntimeCommand.kind == "record",
        )).one()
        asset = db.get(AudioAssetRow, record.expected_raw_audio_id)
        assert asset is not None
        assert asset.is_simulation is False
        assert asset.data_classification == "research"
        # The microphone authorization must state the honest classification.
        assert authorize_recording_command(
            db,
            session_id=SESSION_ID,
            command_key=record.idempotency_key,
            capability_token_hash=CAPABILITY_HASH,
            bank=FIRST_ONLY_BANK,
            protocol=PROTOCOL,
            now=NOW,
        ) is False


@pytest.mark.parametrize(
    ("source", "reason_code", "actor_type"),
    [
        ("patient_requested_pause", "patient_requested_pause", "device"),
        ("subject_withdrawal", "subject_withdrawn", "researcher"),
        (
            "cloud_processing_consent_revoked",
            "cloud_processing_revoked",
            "system",
        ),
    ],
)
def test_real_session_external_safety_chain_fences_current_command(
        service_engine, monkeypatch, source, reason_code, actor_type):
    _set_channels(monkeypatch, "real_only")
    _enable_cloud_policy(monkeypatch)
    with Session(service_engine) as db:
        _seed_research_ready(db)
        _start(db)
        db.commit()
        command = _current_tts(db)
        kwargs = {}
        if actor_type == "device":
            kwargs.update({
                "capability_token_hash": CAPABILITY_HASH,
                "idempotency_token": "real-session-patient-pause-0001",
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
        assert state is not None
        assert state.status == "paused"
        assert state.current_command_id is None
        assert state.last_error_code == reason_code
        latest = list(db.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))[-1]
        assert latest.event_type == "pause"
        assert latest.actor_type == actor_type
        assert json.loads(latest.payload_json) == {
            "reason_code": reason_code,
            "source": source,
        }
        if source == "subject_withdrawal":
            # Withdrawal closes the patient device through capability
            # revocation in the governance transaction; the drain-target
            # derivation intentionally never serves this source.
            with pytest.raises(AutopilotServiceError) as refused:
                get_drain_target(
                    db,
                    session_id=SESSION_ID,
                    capability_token_hash=CAPABILITY_HASH,
                )
            assert refused.value.code == "autopilot_drain_target_invalid"
            return
        # The fenced command still drains through the one physical closure.
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
            now=NOW + timedelta(seconds=2),
        )
        db.commit()
        assert drained.replayed is False


def test_real_session_researcher_takeover_reaches_manual_closure(
        service_engine, monkeypatch):
    _set_channels(monkeypatch, "real_only")
    _enable_cloud_policy(monkeypatch)
    with Session(service_engine) as db:
        _seed_research_ready(db)
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
        assert drained.replayed is False
        receipt = takeover_autopilot_to_manual(
            db,
            session_id=SESSION_ID,
            idempotency_key="takeover-real-session-0001",
            expected_revision=3,
            actor_id=ACTOR_ID,
            now=NOW + timedelta(seconds=3),
        )
        db.commit()
        assert receipt.mode == "manual"
        state = db.get(SessionAutopilotState, SESSION_ID)
        assert state is not None
        assert (state.mode, state.status) == ("manual", "paused")


def test_attempt_worker_submit_gate_opens_for_either_channel(monkeypatch):
    ran = threading.Event()

    def _worker(_session_id: str) -> None:
        ran.set()

    _set_channels(monkeypatch, "none")
    assert autopilot_orchestration.submit(
        "S-SUBMIT-CHANNEL-NONE", _worker) is False
    _set_channels(monkeypatch, "real_only")
    assert autopilot_orchestration.submit(
        "S-SUBMIT-CHANNEL-REAL", _worker) is True
    assert ran.wait(timeout=5)
