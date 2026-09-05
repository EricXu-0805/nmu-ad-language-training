"""HTTP/privacy regression tests for the deidentified AI quality dashboard."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import (
    ai_quality_service,
    audio_store,
    auth,
    autopilot_ledger,
    content,
    db,
    evidence_ledger,
    repeat_intent,
    runtime,
)
from app.db import get_session
from app.main import app
from app.models import (
    AttemptEvent,
    AutopilotControlEvent,
    AudioAssetRow,
    AudioCaptureReceipt,
    InteractionEvent,
    ItemEvent,
    Patient,
    ResearchUser,
    Session as TrainSession,
    SessionRuntimeState,
    TechnicalPauseReceipt,
    TurnConfirmationRevision,
    TurnEvent,
)


BANK = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
PROTOCOL = content.load_autopilot_protocol(
    content.CONTENT_DIR / "autopilot_protocol_v1.json")
BANK_DIGEST = content.item_bank_definition_digest(BANK)
PROTOCOL_DIGEST = content.autopilot_protocol_definition_digest(PROTOCOL)
REPEAT_PROTOCOL = repeat_intent.active_protocol()
PLAN = runtime.build_session_plan(BANK, 2, "正式训练")
FIRST_ITEM = PLAN.items[0]
FIRST_TURN = FIRST_ITEM.turns[0]
PASSWORD = "quality-password-2026"


@dataclass
class QualityEnv:
    engine: object


@pytest.fixture
def quality_env(monkeypatch) -> QualityEnv:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "13579024")
    monkeypatch.delenv(
        ai_quality_service.RESEARCH_MIN_SUBJECTS_ENV, raising=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for username, display_id, role in (
            ("research-a", "RESEARCH-A", "researcher"),
            ("research-b", "RESEARCH-B", "researcher"),
            ("steward", "STEWARD", "data_steward"),
            ("admin", "ADMIN", "admin"),
        ):
            session.add(ResearchUser(
                username=username,
                display_id=display_id,
                password_hash=auth.hash_password(PASSWORD),
                role=role,
                created_at=datetime.now(),
            ))
        session.commit()

    def override_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    yield QualityEnv(engine=engine)
    app.dependency_overrides.clear()


def _client(username: str) -> TestClient:
    client = TestClient(app)
    response = client.post("/auth/login", json={
        "username": username,
        "password": PASSWORD,
    })
    assert response.status_code == 200, response.text
    csrf = client.cookies.get(auth.CSRF_COOKIE_NAME)
    assert csrf
    client.headers.update({"X-CSRF-Token": csrf})
    return client


def _seed_patient(
    session: Session,
    patient_id: str,
    *,
    simulation: bool,
    withdrawn: str | None = None,
) -> None:
    session.add(Patient(
        patient_id=patient_id,
        is_simulation_subject=simulation,
        consent_status="已同意",
        consent_type="本人同意",
        mandarin_eligible=True,
        recording_allowed=True,
        withdrawal_status=withdrawn,
    ))


def _seed_session(
    session: Session,
    session_id: str,
    patient_id: str,
    *,
    trainer_id: str,
    simulation: bool,
    week_no: int = 1,
    runtime_status: str | None = None,
) -> TrainSession:
    row = TrainSession(
        session_id=session_id,
        patient_id=patient_id,
        week_no=week_no,
        phase_type="关系建立" if week_no == 1 else "正式训练",
        event_line="关系建立环节" if week_no == 1 else "正式训练",
        trainer_id=trainer_id,
        item_bank_version_id=BANK.version_id,
        item_bank_definition_digest=BANK_DIGEST,
        autopilot_protocol_version_id=PROTOCOL["protocol_version_id"],
        autopilot_protocol_definition_digest=PROTOCOL_DIGEST,
        repeat_protocol_version_id=REPEAT_PROTOCOL.version_id,
        repeat_protocol_definition_digest=REPEAT_PROTOCOL.definition_digest,
        is_simulation=simulation,
        data_classification="simulation" if simulation else "research",
    )
    session.add(row)
    if runtime_status is not None:
        session.add(SessionRuntimeState(
            session_id=session_id,
            status=runtime_status,
            revision=1,
        ))
    return row


def _quality(client: TestClient, classification: str = "simulation"):
    return client.get(
        f"/quality/ai-metrics?data_classification={classification}")


def _derived_control_key(prefix: str, token: str) -> str:
    return f"{prefix}-{hashlib.sha256(token.encode('utf-8')).hexdigest()}"


def test_query_and_named_account_boundary_and_empty_simulation_v2(quality_env):
    anonymous = TestClient(app)
    denied = _quality(anonymous)
    assert denied.status_code in {401, 403}
    pin_only = anonymous.get(
        "/quality/ai-metrics?data_classification=simulation",
        headers={"X-Console-Pin": "13579024"},
    )
    assert pin_only.status_code in {401, 403}

    admin = _client("admin")
    assert admin.get("/quality/ai-metrics").status_code == 422
    assert admin.get(
        "/quality/ai-metrics?data_classification=simulation&group_by=week",
    ).status_code == 422
    assert admin.get(
        "/quality/ai-metrics?data_classification=unknown",
    ).status_code == 422

    response = _quality(admin)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"
    payload = response.json()
    assert set(payload) == {"schema_version", "generated_at", "privacy", "rows"}
    assert payload["schema_version"] == "ai-quality-dashboard.v2"
    assert payload["privacy"] == {
        "aggregation_only": True,
        "contains_patient_identifiers": False,
        "contains_audio": False,
        "contains_transcripts": False,
    }
    assert len(payload["rows"]) == 1
    row = payload["rows"][0]
    assert row["visibility_scope"] == "all_sessions"
    assert row["suppression"] == {
        "status": "not_applicable",
        "reason": None,
        "minimum_distinct_subjects": None,
        "distinct_subjects": None,
    }
    assert row["coverage"]["visible_sessions"] == 0
    assert row["coverage"]["included_sessions"] == 0
    assert row["operational"]["eligible_turns"] == 0
    assert row["dimensions"]["data_classification"] == "simulation"
    assert all(
        value is None
        for key, value in row["dimensions"].items()
        if key != "data_classification"
    )


def test_role_scope_terminal_steward_and_missing_patient_fail_closed(quality_env):
    with Session(quality_env.engine) as session:
        for patient_id in ("P-A", "P-B", "P-C"):
            _seed_patient(session, patient_id, simulation=True)
        _seed_session(
            session, "S-A", "P-A", trainer_id="RESEARCH-A", simulation=True)
        _seed_session(
            session, "S-B", "P-B", trainer_id="RESEARCH-B", simulation=True,
            runtime_status="completed")
        _seed_session(
            session, "S-C", "P-C", trainer_id="RESEARCH-B", simulation=True,
            runtime_status="active")
        # A migrated/broken foreign-key row is restricted rather than crashing
        # or being silently treated as an eligible subject.
        _seed_session(
            session, "S-MISSING", "P-MISSING", trainer_id="RESEARCH-A",
            simulation=True)
        session.commit()

    researcher = _quality(_client("research-a")).json()["rows"][0]
    assert researcher["visibility_scope"] == "owner_sessions"
    assert researcher["coverage"]["visible_sessions"] == 2
    assert researcher["coverage"]["included_sessions"] == 1
    assert researcher["diagnostics"]["reason_counts"][
        "restricted_or_withdrawn_sessions"] == 1

    steward = _quality(_client("steward")).json()["rows"][0]
    assert steward["visibility_scope"] == "terminal_sessions"
    assert steward["coverage"]["visible_sessions"] == 1
    assert steward["coverage"]["included_sessions"] == 1

    admin = _quality(_client("admin")).json()["rows"][0]
    assert admin["visibility_scope"] == "all_sessions"
    assert admin["coverage"]["visible_sessions"] == 4
    assert admin["coverage"]["included_sessions"] == 3


def test_unbound_or_mismatched_frozen_definitions_are_excluded(quality_env):
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-BINDING", simulation=True)
        session.add(TrainSession(
            session_id="S-UNBOUND",
            patient_id="P-BINDING",
            week_no=2,
            phase_type="正式训练",
            event_line="正式训练",
            trainer_id="RESEARCH-A",
            item_bank_version_id=BANK.version_id,
            is_simulation=True,
            data_classification="simulation",
        ))
        mismatch = _seed_session(
            session, "S-MISMATCH", "P-BINDING", trainer_id="RESEARCH-A",
            simulation=True, week_no=2)
        mismatch.item_bank_definition_digest = "b" * 64
        session.add(mismatch)
        session.commit()

    row = _quality(_client("admin")).json()["rows"][0]
    assert row["coverage"]["visible_sessions"] == 2
    assert row["coverage"]["included_sessions"] == 0
    assert row["coverage"]["source_turns"] == 0
    assert row["diagnostics"]["reason_counts"][
        "protocol_binding_invalid_sessions"] == 2
    assert row["diagnostics"]["reason_counts"][
        "structural_invalid_evidence_records"] == 0
    assert row["diagnostics"]["reason_counts"][
        "lineage_invalid_turns"] == 0


@pytest.mark.parametrize(("configured", "reason"), [
    (None, "research_threshold_unconfigured"),
    (" 2", "research_threshold_invalid"),
    ("1", "research_threshold_invalid"),
    ("101", "research_threshold_invalid"),
])
def test_research_threshold_fails_before_evidence_load(
        quality_env, monkeypatch, configured, reason):
    if configured is None:
        monkeypatch.delenv(
            ai_quality_service.RESEARCH_MIN_SUBJECTS_ENV, raising=False)
    else:
        monkeypatch.setenv(
            ai_quality_service.RESEARCH_MIN_SUBJECTS_ENV, configured)

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("suppressed research must not read cohort or evidence")

    monkeypatch.setattr(
        ai_quality_service, "_visible_sessions", forbidden_read)
    monkeypatch.setattr(
        ai_quality_service, "_begin_stable_read_snapshot", forbidden_read)
    response = _quality(_client("admin"), "research")
    assert response.status_code == 200, response.text
    row = response.json()["rows"][0]
    assert row["suppression"]["status"] == "suppressed"
    assert row["suppression"]["reason"] == reason
    assert row["suppression"]["distinct_subjects"] is None
    assert all(value is None for value in row["coverage"].values())
    assert all(value is None for value in row["operational"].values())
    assert all(value is None for value in row["research_truth"].values())
    assert all(
        value is None
        for value in row["diagnostics"]["reason_counts"].values()
    )


def test_configured_research_stays_suppressed_before_any_cohort_read(
        quality_env, monkeypatch):
    """门槛配对了也不发：还要有人具名指定谁可以读、并且真切过一次纪元。

    拒绝码是"没人被授权读"而不是"还没切纪元"——角色闸有意排在最前，
    不在白名单的人不该从拒绝码里学到纪元存不存在。
    """
    monkeypatch.setenv(ai_quality_service.RESEARCH_MIN_SUBJECTS_ENV, "2")

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("unfrozen research release must not read cohort")

    monkeypatch.setattr(ai_quality_service, "_visible_sessions", forbidden_read)
    monkeypatch.setattr(
        ai_quality_service, "_begin_stable_read_snapshot", forbidden_read)
    row = _quality(_client("admin"), "research").json()["rows"][0]
    assert row["suppression"] == {
        "status": "suppressed",
        "reason": "research_release_reader_not_authorized",
        "minimum_distinct_subjects": None,
        "distinct_subjects": None,
    }
    assert all(value is None for value in row["coverage"].values())
    assert all(value is None for value in row["operational"].values())
    assert all(value is None for value in row["research_truth"].values())
    assert all(
        value is None
        for value in row["diagnostics"]["reason_counts"].values()
    )


def _seed_verified_attempt(
    session: Session,
    *,
    session_id: str,
    raw_audio_id: str,
    processing_status: str,
    answer_type: str | None,
    persist_blob: bool = True,
    uploaded_at: datetime | None = None,
    processing_requested_at: datetime | None = None,
    processed_at: datetime | None = None,
    error_code: str | None = None,
    asr_engine_version: str | None = None,
    judge_engine_version: str | None = None,
) -> AttemptEvent:
    now = datetime.now()
    upload_completed = uploaded_at or now
    processing_requested = processing_requested_at or now
    processing_completed = processed_at or (
        processing_requested + timedelta(milliseconds=125))
    blob = b"quality-dashboard-audio:" + raw_audio_id.encode("ascii")
    checksum = hashlib.sha256(blob).hexdigest()
    if persist_blob:
        _path, saved_checksum, idempotent = audio_store.save_blob_atomic(
            raw_audio_id, blob, "audio/webm")
        assert saved_checksum == checksum
        assert idempotent is False
    session.add(AudioAssetRow(
        raw_audio_id=raw_audio_id,
        session_id=session_id,
        is_simulation=True,
        data_classification="simulation",
        turn_key=f"{FIRST_ITEM.item_id}#{FIRST_TURN.turn_seq}",
        status="recorded",
        checksum=checksum,
        byte_count=len(blob),
        uploaded_at=upload_completed,
    ))
    session.add(AudioCaptureReceipt(
        raw_audio_id=raw_audio_id,
        session_id=session_id,
        turn_key=f"{FIRST_ITEM.item_id}#{FIRST_TURN.turn_seq}",
        received_at=upload_completed,
        duration_seconds=1.0,
        byte_count=len(blob),
        checksum=checksum,
        data_classification="simulation",
        is_simulation=True,
    ))
    attempt = AttemptEvent(
        session_id=session_id,
        item_id=FIRST_ITEM.item_id,
        turn_seq=FIRST_TURN.turn_seq,
        response_role=FIRST_TURN.response_role,
        attempt_seq=1,
        raw_audio_id=raw_audio_id,
        prompt_level=0,
        asr_text="秘密转写",
        operational_answer_type=answer_type,
        operational_score=1.0 if answer_type == "正确" else None,
        processing_status=processing_status,
        error_code=error_code,
        asr_engine_version=asr_engine_version,
        judge_engine_version=judge_engine_version,
        created_at=processing_requested,
        processed_at=processing_completed,
        is_simulation=True,
    )
    session.add(attempt)
    session.commit()
    session.refresh(attempt)
    return attempt


def _seed_atomic_pause_receipt(
    session: Session,
    *,
    session_id: str,
    event_seq: int,
    idempotency_key: str,
    expected_runtime_revision: int,
    expected_live_wseq: int,
    error_code: str,
    item_id: str | None = FIRST_ITEM.item_id,
    turn_seq: int | None = FIRST_TURN.turn_seq,
    attempt_id: int | None = None,
    request_hash: str | None = None,
    runtime_revision: int | None = None,
    paused_cursor_wseq: int | None = None,
    cursor_updates: dict[str, object] | None = None,
) -> InteractionEvent:
    attempt = session.get(AttemptEvent, attempt_id) if attempt_id is not None else None
    event = InteractionEvent(
        session_id=session_id,
        event_seq=event_seq,
        event_type="technical_pause",
        item_id=item_id,
        turn_seq=turn_seq,
        attempt_id=attempt_id,
        attempt_seq=attempt.attempt_seq if attempt is not None else None,
        payload_json=evidence_ledger.encode_event_payload(
            "technical_pause", {"error_code": error_code}),
        is_simulation=True,
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    paused_wseq = (
        expected_live_wseq + 1
        if paused_cursor_wseq is None else paused_cursor_wseq
    )
    cursor: dict[str, object] = {
        "sessionId": session_id,
        "screen": "paused",
        "itemIdx": 0,
        "turnIdx": 0,
        "responseRole": FIRST_TURN.response_role,
        "recording": "stopped",
        "selfStart": False,
        "wseq": paused_wseq,
    }
    cursor.update(cursor_updates or {})
    request_fields = {
        "idempotency_key": idempotency_key,
        "expected_revision": expected_runtime_revision,
        "expected_live_wseq": expected_live_wseq,
        "error_code": error_code,
        "attempt_id": attempt_id,
    }
    session.add(TechnicalPauseReceipt(
        session_id=session_id,
        interaction_event_id=event.id,
        idempotency_key=idempotency_key,
        request_hash=(
            request_hash
            or evidence_ledger.technical_pause_request_hash(
                session_id, request_fields)
        ),
        expected_runtime_revision=expected_runtime_revision,
        expected_live_wseq=expected_live_wseq,
        runtime_revision=(
            expected_runtime_revision + 1
            if runtime_revision is None else runtime_revision
        ),
        paused_cursor_wseq=paused_wseq,
        live_seq=event_seq + 1,
        cursor_json=json.dumps(cursor, ensure_ascii=False),
    ))
    session.commit()
    return event


def test_quality_latency_includes_queue_time_after_verified_upload(quality_env):
    upload_completed = datetime.now()
    processing_requested = upload_completed + timedelta(seconds=5)
    processing_completed = processing_requested + timedelta(milliseconds=125)
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-LATENCY", simulation=True)
        _seed_session(
            session, "S-LATENCY", "P-LATENCY", trainer_id="RESEARCH-A",
            simulation=True, week_no=2)
        session.commit()
        _seed_verified_attempt(
            session,
            session_id="S-LATENCY",
            raw_audio_id="A-LATENCY",
            processing_status="completed",
            answer_type="正确",
            uploaded_at=upload_completed,
            processing_requested_at=processing_requested,
            processed_at=processing_completed,
        )

    row = _quality(_client("admin")).json()["rows"][0]
    assert row["operational"]["latency_sample_count"] == 1
    assert row["operational"]["latency_p50_ms"] == 5125
    assert row["operational"]["latency_p95_ms"] == 5125


def _seed_locked_truth(
    session: Session,
    *,
    session_id: str,
    raw_audio_id: str,
    answer_type: str,
) -> None:
    attempt = _seed_verified_attempt(
        session,
        session_id=session_id,
        raw_audio_id=raw_audio_id,
        processing_status="completed",
        answer_type=answer_type,
    )
    item = ItemEvent(
        session_id=session_id,
        item_id=FIRST_ITEM.item_id,
        task_type=FIRST_ITEM.task_type,
        item_set_type="训练集",
        presentation_order=1,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    confirmed = "秘密转写"
    turn = TurnEvent(
        item_event_id=item.id,
        source_attempt_id=attempt.id,
        turn_seq=FIRST_TURN.turn_seq,
        response_role=FIRST_TURN.response_role,
        raw_audio_id=attempt.raw_audio_id,
        asr_text=attempt.asr_text,
        confirmed_response_text=confirmed,
        confirmation_revision=1,
        prompt_level=attempt.prompt_level,
        ai_answer_type=attempt.operational_answer_type,
        ai_score=attempt.operational_score,
        score_locked=True,
        reviewer_id="RESEARCH-A",
        reviewed_score=1.0,
        element_value=1.0,
    )
    session.add(turn)
    session.commit()
    session.refresh(turn)
    session.add(TurnConfirmationRevision(
        turn_id=turn.id,
        session_id=session_id,
        revision=1,
        expected_revision=0,
        actor_display_id="RESEARCH-A",
        before_sha256=ai_quality_service._confirmation_text_sha256(None),
        after_sha256=ai_quality_service._confirmation_text_sha256(confirmed),
        idempotency_key=f"quality-confirm-{raw_audio_id.lower()}",
    ))
    session.commit()


def test_authoritative_pause_and_takeover_receipts_count_once(
        quality_env):
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-TECH", simulation=True)
        _seed_session(
            session, "S-TECH", "P-TECH", trainer_id="RESEARCH-A",
            simulation=True, week_no=2)
        session.commit()
        attempt = _seed_verified_attempt(
            session,
            session_id="S-TECH",
            raw_audio_id="A-TECH",
            processing_status="technical_failure",
            answer_type=None,
        )
        _seed_atomic_pause_receipt(
            session,
            session_id="S-TECH",
            event_seq=1,
            idempotency_key="quality-pause-positioned-0001",
            expected_runtime_revision=1,
            expected_live_wseq=1,
            error_code="device_failure",
            attempt_id=attempt.id,
        )
        _seed_atomic_pause_receipt(
            session,
            session_id="S-TECH",
            event_seq=2,
            idempotency_key="quality-pause-positioned-0002",
            expected_runtime_revision=2,
            expected_live_wseq=2,
            error_code="network_failure",
        )
        # A receipt with a partial position could never be emitted by the
        # atomic endpoint.  Even with a matching hash/cursor it stays invalid.
        _seed_atomic_pause_receipt(
            session,
            session_id="S-TECH",
            event_seq=3,
            idempotency_key="quality-pause-partial-0003",
            expected_runtime_revision=3,
            expected_live_wseq=3,
            error_code="partial_position",
            turn_seq=None,
        )
        command_id = 73
        session.add(AutopilotControlEvent(
            idempotency_key=_derived_control_key(
                "device-drain", "quality-drain-0001"),
            session_id="S-TECH",
            event_seq=1,
            event_type="drain_complete",
            scope_key="p0a_sim_first_single_v1",
            control_generation=1,
            runner_generation=1,
            command_id=command_id,
            actor_type="device",
            actor_id="device-hash",
            reason_code="device_media_drained",
            from_mode="autonomous",
            to_mode="autonomous",
            from_status="paused",
            to_status="paused",
            payload_json=autopilot_ledger.encode_control_event_payload(
                "drain_complete", {"drained_command_id": command_id}),
        ))
        takeover_payload = {
            "reason_code": "researcher_explicit_takeover",
            "source": "account_takeover_endpoint",
            "expected_revision": 3,
        }
        session.add(AutopilotControlEvent(
            idempotency_key="quality-takeover-0001",
            session_id="S-TECH",
            event_seq=2,
            event_type="takeover",
            scope_key="p0a_sim_first_single_v1",
            control_generation=1,
            runner_generation=1,
            command_id=command_id,
            actor_type="researcher",
            actor_id="RESEARCH-A",
            reason_code="researcher_explicit_takeover",
            from_mode="autonomous",
            to_mode="manual",
            from_status="paused",
            to_status="paused",
            payload_json=autopilot_ledger.encode_control_event_payload(
                "takeover", takeover_payload),
        ))
        session.commit()

    row = _quality(_client("admin")).json()["rows"][0]
    assert row["coverage"]["source_turns"] == PLAN.total_turns()
    assert row["coverage"]["audio_evidenced_turns"] == 1
    assert row["operational"]["total_attempts"] == 1
    assert row["operational"]["ai_attempted_turns"] == 1
    assert row["operational"]["ai_judged_turns"] == 0
    assert row["operational"]["technical_failure_attempts"] == 1
    assert row["operational"]["technical_pause_count"] == 2
    assert row["operational"]["researcher_takeover_count"] == 1
    assert row["diagnostics"]["reason_counts"][
        "structural_invalid_evidence_records"] == 2
    # Technical failure is not a negative AI decision and therefore not FN.
    assert row["research_truth"]["false_negative"] == 0
    assert row["research_truth"]["reviewed_decisions"] == 0


@pytest.mark.parametrize("tamper", [
    "request_hash",
    "cursor_session",
    "cursor_state",
    "cursor_capture_active",
    "cursor_position",
    "runtime_revision",
    "unpositioned",
    "cross_attempt",
])
def test_unprovable_atomic_pause_receipt_is_not_counted(
        quality_env, tamper):
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-PAUSE-FORGED", simulation=True)
        _seed_session(
            session, "S-PAUSE-FORGED", "P-PAUSE-FORGED",
            trainer_id="RESEARCH-A", simulation=True, week_no=2)
        session.commit()
        kwargs: dict[str, object] = {}
        if tamper == "request_hash":
            kwargs["request_hash"] = "f" * 64
        elif tamper == "cursor_session":
            kwargs["cursor_updates"] = {"sessionId": "S-OTHER"}
        elif tamper == "cursor_state":
            kwargs["cursor_updates"] = {"screen": "present"}
        elif tamper == "cursor_capture_active":
            kwargs["cursor_updates"] = {
                "selfStart": True,
                "rawAudioId": "raw-forged",
            }
        elif tamper == "cursor_position":
            kwargs["cursor_updates"] = {"itemIdx": 1}
        elif tamper == "runtime_revision":
            kwargs["runtime_revision"] = 7
        elif tamper == "unpositioned":
            kwargs.update(item_id=None, turn_seq=None)
        elif tamper == "cross_attempt":
            other_item = PLAN.items[1]
            other_turn = other_item.turns[0]
            cross_attempt = AttemptEvent(
                session_id="S-PAUSE-FORGED",
                item_id=other_item.item_id,
                turn_seq=other_turn.turn_seq,
                response_role=other_turn.response_role,
                attempt_seq=1,
                raw_audio_id="A-CROSS-ATTEMPT",
                prompt_level=0,
                processing_status="technical_failure",
                is_simulation=True,
            )
            session.add(cross_attempt)
            session.commit()
            session.refresh(cross_attempt)
            kwargs["attempt_id"] = cross_attempt.id
        _seed_atomic_pause_receipt(
            session,
            session_id="S-PAUSE-FORGED",
            event_seq=1,
            idempotency_key="quality-pause-forged-0001",
            expected_runtime_revision=1,
            expected_live_wseq=1,
            error_code="device_failure",
            **kwargs,
        )

    row = _quality(_client("admin")).json()["rows"][0]
    assert row["operational"]["technical_pause_count"] == 0
    assert row["diagnostics"]["reason_counts"][
        "structural_invalid_evidence_records"] == 2


@pytest.mark.parametrize(("failure_type", "engine_version"), [
    ("asr_failed", "quality-engine-v1"),
    ("judgement_failed", "quality-engine-v1"),
    ("asr_failed", None),
    ("judgement_failed", None),
])
def test_server_owned_attempt_failure_pause_is_counted_without_researcher_receipt(
        quality_env, failure_type, engine_version):
    error_code = "provider_timeout"
    engine_key = (
        "asr_engine_version"
        if failure_type == "asr_failed" else "judge_engine_version")
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-AUTO-PAUSE", simulation=True)
        _seed_session(
            session, "S-AUTO-PAUSE", "P-AUTO-PAUSE",
            trainer_id="RESEARCH-A", simulation=True, week_no=2)
        session.commit()
        attempt = _seed_verified_attempt(
            session,
            session_id="S-AUTO-PAUSE",
            raw_audio_id="A-AUTO-PAUSE",
            processing_status="technical_failure",
            answer_type=None,
            error_code=error_code,
            asr_engine_version=(
                engine_version if failure_type == "asr_failed" else None),
            judge_engine_version=(
                engine_version if failure_type == "judgement_failed" else None),
        )
        common = {
            "session_id": "S-AUTO-PAUSE",
            "item_id": FIRST_ITEM.item_id,
            "turn_seq": FIRST_TURN.turn_seq,
            "attempt_id": attempt.id,
            "attempt_seq": attempt.attempt_seq,
            "is_simulation": True,
        }
        session.add(InteractionEvent(
            event_seq=1,
            event_type=failure_type,
            payload_json=evidence_ledger.encode_event_payload(
                failure_type, {
                    engine_key: engine_version,
                    "error_code": error_code,
                }),
            **common,
        ))
        session.add(InteractionEvent(
            event_seq=2,
            event_type="technical_pause",
            payload_json=evidence_ledger.encode_event_payload(
                "technical_pause", {"error_code": error_code}),
            **common,
        ))
        session.commit()

    row = _quality(_client("admin")).json()["rows"][0]
    assert row["operational"]["technical_pause_count"] == 1
    assert row["diagnostics"]["reason_counts"][
        "structural_invalid_evidence_records"] == 0


@pytest.mark.parametrize("tamper", [
    "missing_predecessor",
    "nonadjacent_predecessor",
    "wrong_error",
    "wrong_engine",
    "nonterminal_attempt",
    "cross_attempt",
])
def test_unprovable_attempt_failure_pause_is_not_counted(
        quality_env, tamper):
    error_code = "provider_timeout"
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-AUTO-PAUSE-BAD", simulation=True)
        _seed_session(
            session, "S-AUTO-PAUSE-BAD", "P-AUTO-PAUSE-BAD",
            trainer_id="RESEARCH-A", simulation=True, week_no=2)
        session.commit()
        attempt = _seed_verified_attempt(
            session,
            session_id="S-AUTO-PAUSE-BAD",
            raw_audio_id="A-AUTO-PAUSE-BAD",
            processing_status=(
                "completed" if tamper == "nonterminal_attempt"
                else "technical_failure"),
            answer_type=None,
            error_code=error_code,
            asr_engine_version="quality-asr-v1",
        )
        predecessor_attempt = attempt
        if tamper == "cross_attempt":
            predecessor_attempt = AttemptEvent(
                session_id="S-AUTO-PAUSE-BAD",
                item_id=FIRST_ITEM.item_id,
                turn_seq=FIRST_TURN.turn_seq,
                response_role=FIRST_TURN.response_role,
                attempt_seq=2,
                raw_audio_id="A-AUTO-PAUSE-CROSS",
                prompt_level=1,
                processing_status="technical_failure",
                error_code=error_code,
                asr_engine_version="quality-asr-v1",
                processed_at=datetime.now(),
                is_simulation=True,
            )
            session.add(predecessor_attempt)
            session.commit()
            session.refresh(predecessor_attempt)
        if tamper != "missing_predecessor":
            session.add(InteractionEvent(
                session_id="S-AUTO-PAUSE-BAD",
                event_seq=1,
                item_id=FIRST_ITEM.item_id,
                turn_seq=FIRST_TURN.turn_seq,
                attempt_id=predecessor_attempt.id,
                attempt_seq=predecessor_attempt.attempt_seq,
                event_type="asr_failed",
                payload_json=evidence_ledger.encode_event_payload(
                    "asr_failed", {
                        "asr_engine_version": (
                            "wrong-engine" if tamper == "wrong_engine"
                            else "quality-asr-v1"),
                        "error_code": (
                            "other_failure" if tamper == "wrong_error"
                            else error_code),
                    }),
                is_simulation=True,
            ))
        session.add(InteractionEvent(
            session_id="S-AUTO-PAUSE-BAD",
            event_seq=3 if tamper == "nonadjacent_predecessor" else 2,
            item_id=FIRST_ITEM.item_id,
            turn_seq=FIRST_TURN.turn_seq,
            attempt_id=attempt.id,
            attempt_seq=attempt.attempt_seq,
            event_type="technical_pause",
            payload_json=evidence_ledger.encode_event_payload(
                "technical_pause", {"error_code": error_code}),
            is_simulation=True,
        ))
        session.commit()

    row = _quality(_client("admin")).json()["rows"][0]
    assert row["operational"]["technical_pause_count"] == 0
    assert row["diagnostics"]["reason_counts"][
        "structural_invalid_evidence_records"] == 1


def test_patient_microphone_failure_pause_is_counted_from_closed_device_fact(
        quality_env):
    failure_id = "123e4567-e89b-12d3-a456-426614174000"
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-DEVICE-PAUSE", simulation=True)
        _seed_session(
            session, "S-DEVICE-PAUSE", "P-DEVICE-PAUSE",
            trainer_id="RESEARCH-A", simulation=True, week_no=2)
        session.add(InteractionEvent(
            session_id="S-DEVICE-PAUSE",
            event_seq=1,
            item_id=FIRST_ITEM.item_id,
            turn_seq=FIRST_TURN.turn_seq,
            event_type="technical_pause",
            payload_json=evidence_ledger.encode_event_payload(
                "technical_pause", {
                    "error_code": "microphone_start_failed",
                    "failure_id": failure_id,
                }),
            is_simulation=True,
        ))
        session.commit()

    row = _quality(_client("admin")).json()["rows"][0]
    assert row["operational"]["technical_pause_count"] == 1
    assert row["diagnostics"]["reason_counts"][
        "structural_invalid_evidence_records"] == 0


@pytest.mark.parametrize("question_suffix", ["", "#0"])
def test_week1_microphone_failure_uses_frozen_relationship_section(
        quality_env, question_suffix):
    """故障位置既可能是旧版节级键,也可能是 2026-09-04 起的问级键,都算结构合法。"""
    script = content.load_week1_script(
        content.CONTENT_DIR / "week1_script.json")
    section_key = script["sections"][0]["key"] + question_suffix
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-WEEK1-DEVICE-PAUSE", simulation=True)
        _seed_session(
            session, "S-WEEK1-DEVICE-PAUSE", "P-WEEK1-DEVICE-PAUSE",
            trainer_id="RESEARCH-A", simulation=True, week_no=1)
        session.add(InteractionEvent(
            session_id="S-WEEK1-DEVICE-PAUSE",
            event_seq=1,
            item_id=f"关系建立·{section_key}",
            turn_seq=None,
            event_type="technical_pause",
            payload_json=evidence_ledger.encode_event_payload(
                "technical_pause", {
                    "error_code": "microphone_permission_denied",
                    "failure_id": "123e4567-e89b-12d3-a456-426614174000",
                }),
            is_simulation=True,
        ))
        session.commit()

    row = _quality(_client("admin")).json()["rows"][0]
    assert row["coverage"]["source_turns"] == 0
    assert row["operational"]["technical_pause_count"] == 1
    assert row["diagnostics"]["reason_counts"][
        "structural_invalid_evidence_records"] == 0


@pytest.mark.parametrize("tamper", [
    "invalid_code",
    "invalid_failure_id",
    "unpositioned",
    "unknown_position",
    "has_attempt",
    "noncanonical",
    "duplicate_failure_id",
])
def test_unprovable_patient_microphone_failure_pause_is_not_counted(
        quality_env, tamper):
    failure_id = "123e4567-e89b-12d3-a456-426614174000"
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-DEVICE-PAUSE-BAD", simulation=True)
        _seed_session(
            session, "S-DEVICE-PAUSE-BAD", "P-DEVICE-PAUSE-BAD",
            trainer_id="RESEARCH-A", simulation=True, week_no=2)
        session.commit()
        attempt = None
        if tamper == "has_attempt":
            attempt = _seed_verified_attempt(
                session,
                session_id="S-DEVICE-PAUSE-BAD",
                raw_audio_id="A-DEVICE-PAUSE-BAD",
                processing_status="completed",
                answer_type="正确",
            )
        payload = {
            "error_code": (
                "unknown_microphone_failure"
                if tamper == "invalid_code" else "microphone_start_failed"),
            "failure_id": (
                "not-a-uuid" if tamper == "invalid_failure_id" else failure_id),
        }
        encoded = evidence_ledger.encode_event_payload(
            "technical_pause", payload)
        if tamper == "noncanonical":
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        event_count = 2 if tamper == "duplicate_failure_id" else 1
        for index in range(event_count):
            session.add(InteractionEvent(
                session_id="S-DEVICE-PAUSE-BAD",
                event_seq=index + 1,
                item_id=(
                    None if tamper == "unpositioned"
                    else "UNKNOWN-ITEM" if tamper == "unknown_position"
                    else FIRST_ITEM.item_id),
                turn_seq=(
                    None if tamper == "unpositioned"
                    else FIRST_TURN.turn_seq),
                attempt_id=attempt.id if attempt is not None else None,
                attempt_seq=(
                    attempt.attempt_seq if attempt is not None else None),
                event_type="technical_pause",
                payload_json=encoded,
                is_simulation=True,
            ))
        session.commit()

    row = _quality(_client("admin")).json()["rows"][0]
    assert row["operational"]["technical_pause_count"] == 0
    assert row["diagnostics"]["reason_counts"][
        "structural_invalid_evidence_records"] == (
            2 if tamper == "duplicate_failure_id" else 1)


def _worker_failure_control_event(
    *,
    session_id: str,
    event_seq: int,
    command_id: int | None,
    error_code: str,
    idempotency_key: str | None = None,
    **overrides,
) -> AutopilotControlEvent:
    values = {
        "idempotency_key": (
            idempotency_key
            or autopilot_ledger.attempt_failure_event_key(
                session_id, command_id or 0, error_code)),
        "session_id": session_id,
        "event_seq": event_seq,
        "event_type": "failure",
        "scope_key": "p0a_sim_first_single_v1",
        "control_generation": 1,
        "runner_generation": 1,
        "command_id": command_id,
        "actor_type": "system",
        "reason_code": error_code,
        "from_mode": "autonomous",
        "to_mode": "autonomous",
        "from_status": "processing_attempt",
        "to_status": "paused",
        "payload_json": autopilot_ledger.encode_control_event_payload(
            "failure", {
                "error_code": error_code,
                "source": "worker_exception",
            }),
    }
    values.update(overrides)
    return AutopilotControlEvent(**values)


def test_fail_closed_worker_control_pause_is_counted_without_attempt(
        quality_env):
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-WORKER-PAUSE", simulation=True)
        _seed_session(
            session, "S-WORKER-PAUSE", "P-WORKER-PAUSE",
            trainer_id="RESEARCH-A", simulation=True, week_no=2)
        session.add(_worker_failure_control_event(
            session_id="S-WORKER-PAUSE",
            event_seq=1,
            command_id=73,
            error_code="autopilot_worker_exception",
        ))
        session.commit()

    row = _quality(_client("admin")).json()["rows"][0]
    assert row["operational"]["technical_pause_count"] == 1
    assert row["diagnostics"]["reason_counts"][
        "structural_invalid_evidence_records"] == 0


@pytest.mark.parametrize("tamper", [
    "missing_command",
    "invalid_idempotency",
    "wrong_digest",
    "wrong_generation",
    "wrong_status",
    "noncanonical_payload",
    "duplicate_boundary",
])
def test_unprovable_fail_closed_worker_control_pause_is_not_counted(
        quality_env, tamper):
    session_id = "S-WORKER-PAUSE-BAD"
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-WORKER-PAUSE-BAD", simulation=True)
        _seed_session(
            session, session_id, "P-WORKER-PAUSE-BAD",
            trainer_id="RESEARCH-A", simulation=True, week_no=2)
        command_id = None if tamper == "missing_command" else 73
        overrides: dict[str, object] = {}
        if tamper == "invalid_idempotency":
            overrides["idempotency_key"] = "worker-failure"
        elif tamper == "wrong_digest":
            overrides["idempotency_key"] = f"attempt-failure-{'f' * 64}"
        elif tamper == "wrong_generation":
            overrides["control_generation"] = 0
        elif tamper == "wrong_status":
            overrides["from_status"] = "waiting_recording"
        elif tamper == "noncanonical_payload":
            overrides["payload_json"] = json.dumps({
                "error_code": "autopilot_worker_exception",
                "source": "worker_exception",
            }, sort_keys=True)
        session.add(_worker_failure_control_event(
            session_id=session_id,
            event_seq=1,
            command_id=command_id,
            error_code="autopilot_worker_exception",
            **overrides,
        ))
        if tamper == "duplicate_boundary":
            session.add(_worker_failure_control_event(
                session_id=session_id,
                event_seq=2,
                command_id=command_id,
                error_code="autopilot_worker_crash",
            ))
        session.commit()

    row = _quality(_client("admin")).json()["rows"][0]
    assert row["operational"]["technical_pause_count"] == 0
    assert row["diagnostics"]["reason_counts"][
        "structural_invalid_evidence_records"] == (
            2 if tamper == "duplicate_boundary" else 1)


@pytest.mark.parametrize("proof_kind", [
    "drain_complete",
    "device_failure",
    "scope_complete",
])
def test_takeover_requires_and_accepts_exact_adjacent_safe_proof(
        quality_env, proof_kind):
    command_id = 73
    status = "scope_completed" if proof_kind == "scope_complete" else "paused"
    if proof_kind == "drain_complete":
        proof = {
            "event_type": "drain_complete",
            "actor_type": "device",
            "actor_id": "device-hash",
            "reason_code": "device_media_drained",
            "from_status": "paused",
            "to_status": "paused",
            "payload_json": autopilot_ledger.encode_control_event_payload(
                "drain_complete", {"drained_command_id": command_id}),
        }
    elif proof_kind == "device_failure":
        proof = {
            "event_type": "failure",
            "actor_type": "device",
            "actor_id": "device-hash",
            "reason_code": "microphone_failed",
            "from_status": "waiting_recording",
            "to_status": "paused",
            "payload_json": autopilot_ledger.encode_control_event_payload(
                "failure", {
                    "error_code": "microphone_failed",
                    "source": "device_ack",
                }),
        }
    else:
        proof = {
            "event_type": "scope_complete",
            "actor_type": "system",
            "actor_id": None,
            "reason_code": None,
            "from_status": "waiting_tts",
            "to_status": "scope_completed",
            "payload_json": autopilot_ledger.encode_control_event_payload(
                "scope_complete", {"completed_command_seq": 3}),
        }
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-TAKEOVER-PROOF", simulation=True)
        _seed_session(
            session, "S-TAKEOVER-PROOF", "P-TAKEOVER-PROOF",
            trainer_id="RESEARCH-A", simulation=True, week_no=2)
        session.add(AutopilotControlEvent(
            idempotency_key=_derived_control_key(
                {
                    "drain_complete": "device-drain",
                    "device_failure": "device-failure",
                    "scope_complete": "scope-complete",
                }[proof_kind],
                f"quality-proof-{proof_kind}",
            ),
            session_id="S-TAKEOVER-PROOF",
            event_seq=1,
            scope_key="p0a_sim_first_single_v1",
            control_generation=1,
            runner_generation=1,
            command_id=command_id,
            from_mode="autonomous",
            to_mode="autonomous",
            **proof,
        ))
        takeover_payload = {
            "reason_code": "researcher_explicit_takeover",
            "source": "account_takeover_endpoint",
            "expected_revision": 3,
        }
        session.add(AutopilotControlEvent(
            idempotency_key=f"quality-takeover-{proof_kind}",
            session_id="S-TAKEOVER-PROOF",
            event_seq=2,
            event_type="takeover",
            scope_key="p0a_sim_first_single_v1",
            control_generation=1,
            runner_generation=1,
            command_id=command_id,
            actor_type="researcher",
            actor_id="RESEARCH-A",
            reason_code="researcher_explicit_takeover",
            from_mode="autonomous",
            to_mode="manual",
            from_status=status,
            to_status=status,
            payload_json=autopilot_ledger.encode_control_event_payload(
                "takeover", takeover_payload),
        ))
        session.commit()

    row = _quality(_client("admin")).json()["rows"][0]
    assert row["operational"]["researcher_takeover_count"] == 1
    assert row["diagnostics"]["reason_counts"][
        "structural_invalid_evidence_records"] == 0


@pytest.mark.parametrize("tamper", [
    "missing_command",
    "missing_predecessor",
    "wrong_command",
    "wrong_generation",
    "wrong_status",
    "invalid_idempotency",
    "invalid_predecessor_idempotency",
    "wrong_proof_payload",
])
def test_unprovable_takeover_is_not_counted(quality_env, tamper):
    command_id = 73
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-TAKEOVER-FORGED", simulation=True)
        _seed_session(
            session, "S-TAKEOVER-FORGED", "P-TAKEOVER-FORGED",
            trainer_id="RESEARCH-A", simulation=True, week_no=2)
        if tamper != "missing_predecessor":
            proof_command = 74 if tamper == "wrong_command" else command_id
            drained_command = (
                99 if tamper == "wrong_proof_payload" else proof_command)
            session.add(AutopilotControlEvent(
                idempotency_key=(
                    "invalid-proof-key"
                    if tamper == "invalid_predecessor_idempotency"
                    else _derived_control_key(
                        "device-drain", "quality-drain-forged-0001")),
                session_id="S-TAKEOVER-FORGED",
                event_seq=1,
                event_type="drain_complete",
                scope_key="p0a_sim_first_single_v1",
                control_generation=(
                    2 if tamper == "wrong_generation" else 1),
                runner_generation=1,
                command_id=proof_command,
                actor_type="device",
                actor_id="device-hash",
                reason_code="device_media_drained",
                from_mode="autonomous",
                to_mode="autonomous",
                from_status="paused",
                to_status="paused",
                payload_json=autopilot_ledger.encode_control_event_payload(
                    "drain_complete", {
                        "drained_command_id": drained_command,
                    }),
            ))
        takeover_payload = {
            "reason_code": "researcher_explicit_takeover",
            "source": "account_takeover_endpoint",
            "expected_revision": 3,
        }
        session.add(AutopilotControlEvent(
            idempotency_key=(
                "short" if tamper == "invalid_idempotency"
                else "quality-takeover-forged-0001"),
            session_id="S-TAKEOVER-FORGED",
            event_seq=2,
            event_type="takeover",
            scope_key="p0a_sim_first_single_v1",
            control_generation=1,
            runner_generation=1,
            command_id=None if tamper == "missing_command" else command_id,
            actor_type="researcher",
            actor_id="RESEARCH-A",
            reason_code="researcher_explicit_takeover",
            from_mode="autonomous",
            to_mode="manual",
            from_status="paused",
            to_status="failed" if tamper == "wrong_status" else "paused",
            payload_json=autopilot_ledger.encode_control_event_payload(
                "takeover", takeover_payload),
        ))
        session.commit()

    row = _quality(_client("admin")).json()["rows"][0]
    assert row["operational"]["researcher_takeover_count"] == 0
    assert row["diagnostics"]["reason_counts"][
        "structural_invalid_evidence_records"] == 1


def test_bare_legacy_pause_and_takeover_events_are_not_authoritative(
        quality_env):
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-BARE", simulation=True)
        _seed_session(
            session, "S-BARE", "P-BARE", trainer_id="RESEARCH-A",
            simulation=True, week_no=2)
        session.commit()
        session.add(InteractionEvent(
            session_id="S-BARE",
            event_seq=1,
            event_type="technical_pause",
            payload_json=evidence_ledger.encode_event_payload(
                "technical_pause", {"error_code": "legacy_failure"}),
            is_simulation=True,
        ))
        session.add(InteractionEvent(
            session_id="S-BARE",
            event_seq=2,
            event_type="researcher_takeover",
            payload_json=evidence_ledger.encode_event_payload(
                "researcher_takeover", {"reason_code": "legacy_manual"}),
            is_simulation=True,
        ))
        session.commit()

    row = _quality(_client("admin")).json()["rows"][0]
    assert row["operational"]["technical_pause_count"] == 0
    assert row["operational"]["researcher_takeover_count"] == 0
    assert row["diagnostics"]["reason_counts"][
        "structural_invalid_evidence_records"] == 2


def test_interaction_evidence_is_append_only_at_the_orm_boundary(quality_env):
    with Session(quality_env.engine) as session:
        row = InteractionEvent(
            session_id="S-APPEND-ONLY",
            event_seq=1,
            event_type="technical_pause",
            payload_json=evidence_ledger.encode_event_payload(
                "technical_pause", {"error_code": "test_failure"}),
            is_simulation=True,
        )
        session.add(row)
        session.commit()
        session.refresh(row)

        row.payload_json = "{}"
        with pytest.raises(RuntimeError, match="只追加"):
            session.commit()
        session.rollback()

        stored = session.get(InteractionEvent, row.id)
        session.delete(stored)
        with pytest.raises(RuntimeError, match="只追加"):
            session.commit()


@pytest.mark.parametrize("physical_state", ["missing", "corrupt"])
def test_receipt_without_intact_physical_blob_is_not_audio_evidence(
        quality_env, physical_state):
    raw_audio_id = f"A-{physical_state.upper()}"
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-PHYSICAL", simulation=True)
        _seed_session(
            session, "S-PHYSICAL", "P-PHYSICAL", trainer_id="RESEARCH-A",
            simulation=True, week_no=2)
        session.commit()
        _seed_verified_attempt(
            session,
            session_id="S-PHYSICAL",
            raw_audio_id=raw_audio_id,
            processing_status="completed",
            answer_type="正确",
            persist_blob=physical_state != "missing",
        )
        if physical_state == "corrupt":
            blob_path = audio_store.find_blob(raw_audio_id)
            assert blob_path is not None
            blob_path.write_bytes(b"corrupt-after-receipt")

    row = _quality(_client("admin")).json()["rows"][0]
    assert row["coverage"]["attempts_observed"] == 1
    assert row["coverage"]["audio_evidenced_turns"] == 0
    assert row["operational"]["total_attempts"] == 1
    assert row["operational"]["ai_attempted_turns"] is None
    assert row["operational"]["ai_judged_turns"] is None
    assert row["diagnostics"]["reason_counts"][
        "audio_evidence_unavailable_turns"] >= 1


def test_physical_blob_os_error_is_unavailable_without_path_leak(
        quality_env, monkeypatch):
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-OSERROR", simulation=True)
        _seed_session(
            session, "S-OSERROR", "P-OSERROR", trainer_id="RESEARCH-A",
            simulation=True, week_no=2)
        session.commit()
        _seed_verified_attempt(
            session,
            session_id="S-OSERROR",
            raw_audio_id="A-OSERROR",
            processing_status="completed",
            answer_type="正确",
        )

    secret_path = "/private/secret/audio/A-OSERROR.webm"

    def fail_with_os_error(_row, **_kwargs):
        raise OSError(secret_path)

    monkeypatch.setattr(
        ai_quality_service.audio_capture,
        "verify_persisted_audio",
        fail_with_os_error,
    )
    response = _quality(_client("admin"))
    assert response.status_code == 200, response.text
    assert response.json()["rows"][0]["coverage"][
        "audio_evidenced_turns"] == 0
    assert secret_path not in response.text


def test_frozen_content_failure_uses_path_free_service_error(
        quality_env, monkeypatch):
    secret_path = "/private/secret/item_bank_v1.json"

    def fail_content(_path):
        raise content.FrozenContentUnavailable(secret_path)

    # 题库按周惰性装载:先落一个场次,坏内容才会真的被触达。
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-FROZEN", simulation=True)
        _seed_session(
            session, "S-FROZEN", "P-FROZEN", trainer_id="RESEARCH-A",
            simulation=True, week_no=2)
        session.commit()
    monkeypatch.setattr(content, "load_item_bank", fail_content)
    response = _quality(_client("admin"))
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "frozen_content_unavailable"
    assert secret_path not in response.text


def test_locked_truth_projection_and_payload_never_expose_source_text_or_ids(
        quality_env):
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-SECRET", simulation=True)
        _seed_session(
            session, "S-SECRET", "P-SECRET", trainer_id="RESEARCH-A",
            simulation=True, week_no=2)
        session.commit()
        _seed_locked_truth(
            session,
            session_id="S-SECRET",
            raw_audio_id="A-SECRET",
            answer_type="正确",
        )

    response = _quality(_client("admin"))
    assert response.status_code == 200, response.text
    row = response.json()["rows"][0]
    assert row["research_truth"] == {
        "reviewed_decisions": 1,
        "true_positive": 1,
        "true_negative": 0,
        "false_positive": 0,
        "false_negative": 0,
    }
    assert row["coverage"]["human_truth_locked_turns"] == 1
    assert row["coverage"]["binary_eligible_reviewed_decisions"] == 1
    assert row["coverage"]["binary_excluded_decisions"] == 0
    rendered = response.text
    for secret in ("P-SECRET", "S-SECRET", "A-SECRET", "秘密转写"):
        assert secret not in rendered


@pytest.mark.parametrize(
    ("answer_type", "reviewed", "false_negative", "binary_excluded"),
    [
        ("偏题", 1, 1, 0),
        ("重复", 1, 1, 0),
        ("未识别", 0, 0, 1),
        ("沉默", 0, 0, 1),
        ("拒答", 0, 0, 1),
    ],
)
def test_only_explicit_semantic_errors_are_binary_negative_decisions(
        quality_env, answer_type, reviewed, false_negative, binary_excluded):
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-BINARY", simulation=True)
        _seed_session(
            session, "S-BINARY", "P-BINARY", trainer_id="RESEARCH-A",
            simulation=True, week_no=2)
        session.commit()
        _seed_locked_truth(
            session,
            session_id="S-BINARY",
            raw_audio_id="A-BINARY",
            answer_type=answer_type,
        )

    row = _quality(_client("admin")).json()["rows"][0]
    assert row["coverage"]["human_truth_locked_turns"] == 1
    assert row["research_truth"]["reviewed_decisions"] == reviewed
    assert row["research_truth"]["false_negative"] == false_negative
    assert row["coverage"]["binary_eligible_reviewed_decisions"] == reviewed
    assert row["coverage"]["binary_excluded_decisions"] == binary_excluded
    assert reviewed + binary_excluded == 1


def test_classification_mismatch_excludes_entire_session(quality_env):
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-MIX", simulation=True)
        _seed_session(
            session, "S-MIX", "P-MIX", trainer_id="RESEARCH-A",
            simulation=True, week_no=2)
        session.commit()
        session.add(AttemptEvent(
            session_id="S-MIX",
            item_id=FIRST_ITEM.item_id,
            turn_seq=FIRST_TURN.turn_seq,
            response_role=FIRST_TURN.response_role,
            attempt_seq=1,
            raw_audio_id="A-MIX",
            prompt_level=0,
            processing_status="technical_failure",
            is_simulation=False,
        ))
        session.commit()

    row = _quality(_client("admin")).json()["rows"][0]
    assert row["coverage"]["visible_sessions"] == 1
    assert row["coverage"]["included_sessions"] == 0
    assert row["diagnostics"]["reason_counts"][
        "classification_inconsistent_sessions"] == 1
    assert row["operational"]["total_attempts"] == 0


def test_more_than_200_visible_sessions_fails_explicitly(quality_env):
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-LIMIT", simulation=True)
        for index in range(ai_quality_service.MAX_VISIBLE_SESSIONS + 1):
            _seed_session(
                session, f"S-LIMIT-{index:03d}", "P-LIMIT",
                trainer_id="RESEARCH-A", simulation=True, week_no=1)
        session.commit()

    scope = _quality(_client("admin"))
    assert scope.status_code == 413
    assert scope.json()["detail"]["code"] == "quality_scope_too_large"
    assert "S-LIMIT" not in scope.text


def test_fact_resource_limit_fails_explicitly_without_truncation(
        quality_env, monkeypatch):
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-LIMIT", simulation=True)
        _seed_session(
            session, "S-LIMIT", "P-LIMIT", trainer_id="RESEARCH-A",
            simulation=True, week_no=2)
        session.commit()
        session.add(AttemptEvent(
            session_id="S-LIMIT",
            item_id=FIRST_ITEM.item_id,
            turn_seq=FIRST_TURN.turn_seq,
            response_role=FIRST_TURN.response_role,
            attempt_seq=1,
            raw_audio_id="A-LIMIT",
            prompt_level=0,
            processing_status="technical_failure",
            is_simulation=True,
        ))
        session.commit()

    admin = _client("admin")
    monkeypatch.setattr(ai_quality_service, "MAX_ATTEMPTS", 0)
    evidence = _quality(admin)
    assert evidence.status_code == 409
    assert evidence.json()["detail"] == {
        "code": "quality_evidence_limit_exceeded",
        "message": "质量证据量超过单次安全处理上限，未返回部分结果",
        "resource": "attempts",
    }


def test_combined_row_budget_rejects_before_orm_evidence_load(
        quality_env, monkeypatch):
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-ROW-BUDGET", simulation=True)
        _seed_session(
            session, "S-ROW-BUDGET", "P-ROW-BUDGET",
            trainer_id="RESEARCH-A", simulation=True, week_no=2)
        session.commit()
        session.add(AttemptEvent(
            session_id="S-ROW-BUDGET",
            item_id=FIRST_ITEM.item_id,
            turn_seq=FIRST_TURN.turn_seq,
            response_role=FIRST_TURN.response_role,
            attempt_seq=1,
            raw_audio_id="A-ROW-BUDGET",
            prompt_level=0,
            processing_status="technical_failure",
            is_simulation=True,
        ))
        session.commit()

    monkeypatch.setattr(ai_quality_service, "MAX_EVIDENCE_ROWS", 0)

    def forbidden_load(*_args, **_kwargs):
        raise AssertionError("row preflight must run before ORM evidence load")

    monkeypatch.setattr(
        ai_quality_service, "_load_evidence_rows", forbidden_load)
    response = _quality(_client("admin"))
    assert response.status_code == 409
    assert response.json()["detail"]["resource"] == "evidence_rows"


def test_text_and_audio_budgets_are_exact_and_fail_closed(
        quality_env, monkeypatch):
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-BYTE-BUDGET", simulation=True)
        _seed_session(
            session, "S-BYTE-BUDGET", "P-BYTE-BUDGET",
            trainer_id="RESEARCH-A", simulation=True, week_no=2)
        session.add(AttemptEvent(
            session_id="S-BYTE-BUDGET",
            item_id=FIRST_ITEM.item_id,
            turn_seq=FIRST_TURN.turn_seq,
            response_role=FIRST_TURN.response_role,
            attempt_seq=1,
            raw_audio_id="A-BYTE-BUDGET",
            prompt_level=0,
            asr_text="中文 transcript",
            processing_status="technical_failure",
            is_simulation=True,
        ))
        session.add(AudioAssetRow(
            raw_audio_id="A-BYTE-BUDGET",
            session_id="S-BYTE-BUDGET",
            is_simulation=True,
            data_classification="simulation",
            turn_key=f"{FIRST_ITEM.item_id}#{FIRST_TURN.turn_seq}",
            status="recorded",
            byte_count=17,
        ))
        session.commit()

        text_bytes = ai_quality_service._loaded_text_bytes_upper_bound(
            session,
            AttemptEvent,
            AttemptEvent.session_id == "S-BYTE-BUDGET",
            (
                AttemptEvent.session_id,
                AttemptEvent.item_id,
                AttemptEvent.response_role,
                AttemptEvent.raw_audio_id,
                AttemptEvent.asr_text,
                AttemptEvent.operational_answer_type,
                AttemptEvent.processing_status,
            ),
        )
        text_bytes += ai_quality_service._loaded_text_bytes_upper_bound(
            session,
            AudioAssetRow,
            AudioAssetRow.session_id == "S-BYTE-BUDGET",
            (
                AudioAssetRow.raw_audio_id,
                AudioAssetRow.session_id,
                AudioAssetRow.data_classification,
                AudioAssetRow.turn_key,
                AudioAssetRow.status,
                AudioAssetRow.withdrawal_status,
                AudioAssetRow.checksum,
            ),
        )
        monkeypatch.setattr(
            ai_quality_service, "MAX_EVIDENCE_TEXT_BYTES", text_bytes)
        monkeypatch.setattr(ai_quality_service, "MAX_AUDIO_VERIFY_BYTES", 17)
        ai_quality_service._preflight_evidence_budget(
            session, ["S-BYTE-BUDGET"])

        monkeypatch.setattr(
            ai_quality_service, "MAX_EVIDENCE_TEXT_BYTES", text_bytes - 1)
        with pytest.raises(ai_quality_service.QualityEvidenceLimitExceeded) as text_error:
            ai_quality_service._preflight_evidence_budget(
                session, ["S-BYTE-BUDGET"])
        assert text_error.value.resource == "evidence_text_bytes"

        monkeypatch.setattr(
            ai_quality_service, "MAX_EVIDENCE_TEXT_BYTES", text_bytes)
        monkeypatch.setattr(ai_quality_service, "MAX_AUDIO_VERIFY_BYTES", 16)
        with pytest.raises(ai_quality_service.QualityEvidenceLimitExceeded) as audio_error:
            ai_quality_service._preflight_evidence_budget(
                session, ["S-BYTE-BUDGET"])
        assert audio_error.value.resource == "audio_verify_bytes"


def test_underreported_or_growing_physical_blob_never_exceeds_hash_budget(
        quality_env, monkeypatch, tmp_path):
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-PHYSICAL-BUDGET", simulation=True)
        _seed_session(
            session, "S-PHYSICAL-BUDGET", "P-PHYSICAL-BUDGET",
            trainer_id="RESEARCH-A", simulation=True, week_no=2)
        session.commit()
        _seed_verified_attempt(
            session,
            session_id="S-PHYSICAL-BUDGET",
            raw_audio_id="A-PHYSICAL-BUDGET",
            processing_status="completed",
            answer_type="正确",
        )
        # Simulate a privileged/out-of-band database corruption; ordinary ORM
        # updates to the append-only capture receipt are correctly rejected.
        session.execute(update(AudioAssetRow).where(
            AudioAssetRow.raw_audio_id == "A-PHYSICAL-BUDGET",
        ).values(byte_count=1))
        session.execute(update(AudioCaptureReceipt).where(
            AudioCaptureReceipt.raw_audio_id == "A-PHYSICAL-BUDGET",
        ).values(byte_count=1))
        session.commit()

    def forbidden_hash(*_args, **_kwargs):
        raise AssertionError("size mismatch must fail before SHA-256")

    monkeypatch.setattr(
        ai_quality_service.audio_capture,
        "verify_persisted_audio",
        forbidden_hash,
    )
    row = _quality(_client("admin")).json()["rows"][0]
    assert row["coverage"]["audio_evidenced_turns"] == 0

    path = tmp_path / "growing.webm"
    path.write_bytes(b"0123456789")
    with pytest.raises(audio_store.AudioBlobTooLarge):
        audio_store.sha256_file(path, max_bytes=9, chunk_size=4)


def test_quality_build_indexes_audio_directory_once_per_request(
        quality_env, monkeypatch):
    with Session(quality_env.engine) as session:
        for suffix in ("ONE", "TWO"):
            _seed_patient(session, f"P-INDEX-{suffix}", simulation=True)
            _seed_session(
                session, f"S-INDEX-{suffix}", f"P-INDEX-{suffix}",
                trainer_id="RESEARCH-A", simulation=True, week_no=2)
        session.commit()
        for suffix in ("ONE", "TWO"):
            _seed_verified_attempt(
                session,
                session_id=f"S-INDEX-{suffix}",
                raw_audio_id=f"A-INDEX-{suffix}",
                processing_status="completed",
                answer_type="正确",
            )

    original_index = audio_store.index_blobs
    calls = 0

    def counted_index(raw_audio_ids):
        nonlocal calls
        calls += 1
        return original_index(raw_audio_ids)

    def forbidden_per_id_lookup(*_args, **_kwargs):
        raise AssertionError("quality projection must not rescan the directory per id")

    monkeypatch.setattr(audio_store, "index_blobs", counted_index)
    monkeypatch.setattr(audio_store, "find_blob", forbidden_per_id_lookup)
    response = _quality(_client("admin"))
    assert response.status_code == 200, response.text
    assert calls == 1
    assert response.json()["rows"][0]["coverage"]["audio_evidenced_turns"] == 2


def test_batch_audio_index_fails_closed_on_unapproved_or_duplicate_blob(
        quality_env, monkeypatch, tmp_path):
    monkeypatch.setattr(audio_store, "AUDIO_DIR", tmp_path)
    (tmp_path / "A-INDEX.webm").write_bytes(b"approved")
    (tmp_path / "A-INDEX.bin").write_bytes(b"unexpected")

    with pytest.raises(audio_store.AudioStoreIntegrityError):
        audio_store.index_blobs(["A-INDEX"])

    (tmp_path / "A-INDEX.bin").unlink()
    (tmp_path / "A-INDEX.wav").write_bytes(b"duplicate")
    with pytest.raises(audio_store.AudioStoreIntegrityError):
        audio_store.index_blobs(["A-INDEX"])

    (tmp_path / "A-INDEX.wav").unlink()
    (tmp_path / "A-INDEX.webm.bak").write_bytes(b"shadow-copy")
    with pytest.raises(audio_store.AudioStoreIntegrityError):
        audio_store.index_blobs(["A-INDEX"])


def test_simulation_quality_rate_limit_is_no_store_and_research_is_not_consumed(
        quality_env):
    admin = _client("admin")
    assert _quality(admin).status_code == 200
    assert _quality(admin).status_code == 200
    limited = _quality(admin)
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert limited.headers["cache-control"] == "private, no-store"
    # The research branch is fully suppressed before expensive evidence access
    # and therefore remains independently available.
    research = _quality(admin, "research")
    assert research.status_code == 200
    assert research.json()["rows"][0]["suppression"]["status"] == "suppressed"


def test_snapshot_backend_contract_is_explicit_and_fail_closed():
    class FakeConnection:
        def __init__(self):
            self.statements: list[str] = []

        def exec_driver_sql(self, statement: str):
            self.statements.append(statement)

    class FakeSession:
        def __init__(self, dialect: str, *, active: bool = False):
            self.dialect = dialect
            self.active = active
            self.connection_options: list[dict[str, object]] = []
            self.connection_value = FakeConnection()

        def in_transaction(self):
            return self.active

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

        def connection(self, **kwargs):
            self.connection_options.append(kwargs)
            return self.connection_value

    postgres = FakeSession("postgresql")
    ai_quality_service._begin_stable_read_snapshot(postgres)  # type: ignore[arg-type]
    assert postgres.connection_options == [{
        "execution_options": {"isolation_level": "REPEATABLE READ"},
    }]
    assert postgres.connection_value.statements == ["SET TRANSACTION READ ONLY"]

    sqlite = FakeSession("sqlite")
    ai_quality_service._begin_stable_read_snapshot(sqlite)  # type: ignore[arg-type]
    assert sqlite.connection_options == [{}]
    assert sqlite.connection_value.statements == ["BEGIN"]

    for unavailable in (FakeSession("mysql"), FakeSession("sqlite", active=True)):
        with pytest.raises(ai_quality_service.QualitySnapshotUnavailable):
            ai_quality_service._begin_stable_read_snapshot(  # type: ignore[arg-type]
                unavailable)
        assert unavailable.connection_options == []


def test_sqlite_quality_snapshot_does_not_drift_after_concurrent_commit(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'quality-snapshot.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "PRAGMA journal_mode=WAL").scalar_one().lower() == "wal"
    with Session(engine) as seed:
        seed.add(Patient(patient_id="P-SNAPSHOT-ONE", is_simulation_subject=True))
        seed.commit()

    with Session(engine) as snapshot, Session(engine) as writer:
        ai_quality_service._begin_stable_read_snapshot(snapshot)
        assert len(list(snapshot.exec(select(Patient)))) == 1
        writer.add(Patient(
            patient_id="P-SNAPSHOT-TWO", is_simulation_subject=True))
        writer.commit()
        assert len(list(snapshot.exec(select(Patient)))) == 1


def test_quality_releases_sqlite_snapshot_before_physical_audio_work(
        tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'quality-release.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 0.25},
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as seed:
        _seed_patient(seed, "P-SNAPSHOT-BASE", simulation=True)
        _seed_session(
            seed, "S-SNAPSHOT-BASE", "P-SNAPSHOT-BASE",
            trainer_id="ADMIN", simulation=True, week_no=2)
        seed.commit()

    physical_gate_reached = 0

    def write_while_physical_verification_would_run(_raw_audio_ids):
        nonlocal physical_gate_reached
        physical_gate_reached += 1
        with Session(engine) as writer:
            _seed_patient(writer, "P-SNAPSHOT-LATER", simulation=True)
            _seed_session(
                writer, "S-SNAPSHOT-LATER", "P-SNAPSHOT-LATER",
                trainer_id="ADMIN", simulation=True, week_no=2)
            writer.commit()
        return {}

    monkeypatch.setattr(
        audio_store, "index_blobs", write_while_physical_verification_would_run)
    with Session(engine) as quality_session:
        result = ai_quality_service.build_ai_quality_dashboard(
            quality_session,
            actor_id="ADMIN",
            actor_role="admin",
            data_classification="simulation",
        )
        assert not quality_session.in_transaction()

    assert physical_gate_reached == 1
    assert result["rows"][0]["coverage"]["visible_sessions"] == 1
    with Session(engine) as verify:
        assert len(list(verify.exec(select(TrainSession)))) == 2


def test_quality_endpoint_enters_snapshot_before_first_database_read(
        quality_env, monkeypatch):
    original = ai_quality_service._begin_stable_read_snapshot
    observed = 0

    def checked_snapshot(session):
        nonlocal observed
        observed += 1
        assert not session.in_transaction()
        original(session)

    monkeypatch.setattr(
        ai_quality_service, "_begin_stable_read_snapshot", checked_snapshot)
    response = _quality(_client("admin"))
    assert response.status_code == 200, response.text
    assert observed == 1


def test_loaded_budget_is_rechecked_before_any_audio_directory_scan(
        quality_env, monkeypatch):
    with Session(quality_env.engine) as session:
        _seed_patient(session, "P-POSTLOAD-BUDGET", simulation=True)
        _seed_session(
            session, "S-POSTLOAD-BUDGET", "P-POSTLOAD-BUDGET",
            trainer_id="RESEARCH-A", simulation=True, week_no=2)
        session.commit()

    fake_rows = ai_quality_service._EvidenceRows(
        items=[SimpleNamespace(
            session_id="S-POSTLOAD-BUDGET", item_id="I", task_type="T")],
        turn_pairs=[], attempts=[], audios=[], receipts=[], interactions=[],
        revision_pairs=[], pause_receipts=[], control_events=[],
    )
    monkeypatch.setattr(ai_quality_service, "MAX_EVIDENCE_ROWS", 0)
    monkeypatch.setattr(
        ai_quality_service, "_load_evidence_rows", lambda *_args: fake_rows)

    def forbidden_index(*_args, **_kwargs):
        raise AssertionError("post-load budget must run before audio directory scan")

    monkeypatch.setattr(audio_store, "index_blobs", forbidden_index)
    response = _quality(_client("admin"))
    assert response.status_code == 409, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.json()["detail"] == {
        "code": "quality_evidence_limit_exceeded",
        "message": "质量证据量超过单次安全处理上限，未返回部分结果",
        "resource": "evidence_rows",
    }


def test_loaded_budget_rechecks_text_and_declared_audio_bytes(monkeypatch):
    item = SimpleNamespace(session_id="S", item_id="I", task_type="T")
    rows = ai_quality_service._EvidenceRows(
        items=[item], turn_pairs=[], attempts=[], audios=[], receipts=[],
        interactions=[], revision_pairs=[], pause_receipts=[], control_events=[],
    )
    monkeypatch.setattr(ai_quality_service, "MAX_EVIDENCE_ROWS", 10)
    monkeypatch.setattr(ai_quality_service, "MAX_EVIDENCE_TEXT_BYTES", 11)
    with pytest.raises(ai_quality_service.QualityEvidenceLimitExceeded) as text_error:
        ai_quality_service._enforce_loaded_evidence_budget(rows)
    assert text_error.value.resource == "evidence_text_bytes"

    audio = SimpleNamespace(
        raw_audio_id="", session_id="", data_classification="", turn_key="",
        status="", withdrawal_status="", checksum="", byte_count=6,
    )
    audio_rows = ai_quality_service._EvidenceRows(
        items=[], turn_pairs=[], attempts=[], audios=[audio], receipts=[],
        interactions=[], revision_pairs=[], pause_receipts=[], control_events=[],
    )
    monkeypatch.setattr(ai_quality_service, "MAX_EVIDENCE_TEXT_BYTES", 100)
    monkeypatch.setattr(ai_quality_service, "MAX_AUDIO_VERIFY_BYTES", 5)
    with pytest.raises(ai_quality_service.QualityEvidenceLimitExceeded) as audio_error:
        ai_quality_service._enforce_loaded_evidence_budget(audio_rows)
    assert audio_error.value.resource == "audio_verify_bytes"
