"""Authoritative subject-withdrawal transaction and governance boundaries."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import date, datetime, timedelta
import hashlib
import json
from threading import Event

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

from app import audio_store, auth, db as app_db
from app.db import get_session
from app.enums import AudioStatus, ConsentType, EventLine, PhaseType
from app.main import app
from app.models import (
    AssessmentEvent,
    AttemptEvent,
    AudioAssetRow,
    ItemEvent,
    LiveState,
    Patient,
    PatientDeviceCapability,
    PatientWithdrawalEvent,
    ResearchUser,
    Session as TrainSession,
    SessionRuntimeState,
    TurnConfirmationRevision,
    TurnEvent,
    VisitPlan,
    VisitPlanCommand,
)


@pytest.fixture
def withdrawal_clients(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'withdrawal.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(app_db, "engine", engine)
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    monkeypatch.setenv("ENABLE_AUDIO_DELETE", "1")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for username, display_id, role in (
            ("withdraw-researcher", "WITHDRAW-RESEARCHER", "researcher"),
            ("withdraw-steward", "WITHDRAW-STEWARD", "data_steward"),
            ("withdraw-admin", "WITHDRAW-ADMIN", "admin"),
        ):
            session.add(ResearchUser(
                username=username,
                display_id=display_id,
                password_hash=auth.hash_password("test-password-1"),
                role=role,
                created_at=datetime.now(),
            ))
        session.commit()

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override

    def logged_in(username: str) -> TestClient:
        client = TestClient(app)
        login = client.post("/auth/login", json={
            "username": username, "password": "test-password-1",
        })
        assert login.status_code == 200, login.text
        client.headers.update({
            "X-CSRF-Token": client.cookies.get(auth.CSRF_COOKIE_NAME),
        })
        return client

    clients = {
        "researcher": logged_in("withdraw-researcher"),
        "steward": logged_in("withdraw-steward"),
        "admin": logged_in("withdraw-admin"),
        "engine": engine,
    }
    try:
        yield clients
    finally:
        for key in ("researcher", "steward", "admin"):
            clients[key].close()
        app.dependency_overrides.clear()


WITHDRAWAL_PLAN_ID = "VP-WITHDRAW-APPROVED"
WITHDRAWAL_PLAN_CREATE_KEY = "withdrawal-plan-create-historical-0001"
WITHDRAWAL_PLAN_APPROVE_KEY = "withdrawal-plan-approve-historical-0001"


def _plan_command_hash(command_type: str, payload: dict[str, object]) -> str:
    """Independently reproduce the historical VisitPlan command digest."""
    normalized = {
        key: (value.isoformat() if isinstance(value, (date, datetime))
              else getattr(value, "value", value))
        for key, value in payload.items()
    }
    canonical = json.dumps(
        {"command_type": command_type, **normalized},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _withdrawal_plan_slot_key() -> str:
    canonical = "\x00".join((
        "P-WITHDRAW",
        "1",
        "2",
        PhaseType.正式训练.value,
        EventLine.正式训练.value,
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _seed_patient_scope(engine) -> bytes:
    now = datetime.now()
    scheduled_date = date.today()
    audio_bytes = b"\x1a\x45\xdf\xa3subject-withdrawal-audio"
    checksum = hashlib.sha256(audio_bytes).hexdigest()
    with Session(engine) as session:
        session.add(Patient(
            patient_id="P-WITHDRAW",
            is_simulation_subject=False,
            consent_status="active",
            consent_type=ConsentType.本人同意,
            mandarin_eligible=True,
            recording_allowed=True,
            cloud_processing_allowed=True,
            governance_revision=0,
        ))
        for sid, status in (
            ("S-WITHDRAW-ACTIVE", "active"),
            ("S-WITHDRAW-PAUSED", "paused"),
            ("S-WITHDRAW-COMPLETED", "completed"),
        ):
            session.add(TrainSession(
                session_id=sid,
                patient_id="P-WITHDRAW",
                week_no=2,
                phase_type=PhaseType.正式训练,
                event_line=EventLine.正式训练,
                item_bank_version_id="wk2-v1-20260707",
                is_simulation=False,
                data_classification="research",
            ))
            session.add(SessionRuntimeState(
                session_id=sid,
                status=status,
                revision=2,
                paused_at=now if status == "paused" else None,
                completed_at=now if status == "completed" else None,
                ended_by="EARLIER" if status == "completed" else None,
                end_reason="completion_gate_passed" if status == "completed" else None,
                updated_at=now,
            ))
        session.flush()
        session.add(AudioAssetRow(
            raw_audio_id="withdrawal-recorded-audio",
            session_id="S-WITHDRAW-ACTIVE",
            turn_key="SE_锚#1",
            audio_format="webm",
            status=AudioStatus.recorded,
            is_simulation=False,
            data_classification="research",
            checksum=checksum,
            byte_count=len(audio_bytes),
            uploaded_at=now,
        ))
        session.add(PatientDeviceCapability(
            token_hash="c" * 64,
            session_id="S-WITHDRAW-ACTIVE",
            device_id_hash="d" * 64,
            active_session_key="S-WITHDRAW-ACTIVE",
            created_at=now,
            expires_at=now + timedelta(minutes=30),
            last_seen_at=now,
        ))
        session.add(LiveState(
            id=1,
            seq=4,
            session_json='{"sessionId":"S-WITHDRAW-ACTIVE","paused":false}',
            cursor_json='{"sessionId":"S-WITHDRAW-ACTIVE","itemIdx":0,"turnIdx":0,"screen":"record","recording":"recording"}',
            patient_rec_json='{"active":true}',
        ))
        session.add(VisitPlan(
            plan_id=WITHDRAWAL_PLAN_ID,
            protocol_slot_key=_withdrawal_plan_slot_key(),
            patient_id="P-WITHDRAW",
            scheduled_date=scheduled_date,
            scheduled_time=None,
            queue_order=0,
            session_sitting_no=1,
            week_no=2,
            phase_type=PhaseType.正式训练,
            event_line=EventLine.正式训练,
            item_bank_version_id="wk2-v1-20260707",
            is_simulation=False,
            data_classification="research",
            status="approved",
            revision=2,
            created_by="WITHDRAW-RESEARCHER",
            created_at=now,
            updated_at=now,
            approved_by="WITHDRAW-RESEARCHER",
            approved_at=now,
        ))
        session.add(VisitPlanCommand(
            plan_id=WITHDRAWAL_PLAN_ID,
            event_seq=1,
            idempotency_key=WITHDRAWAL_PLAN_CREATE_KEY,
            command_type="create",
            request_hash=_plan_command_hash("create", {
                "idempotency_key": WITHDRAWAL_PLAN_CREATE_KEY,
                "patient_id": "P-WITHDRAW",
                "scheduled_date": scheduled_date,
                "scheduled_time": None,
                "queue_order": 0,
                "session_sitting_no": 1,
                "week_no": 2,
                "phase_type": PhaseType.正式训练,
                "event_line": EventLine.正式训练,
            }),
            actor_id="WITHDRAW-RESEARCHER",
            expected_revision=0,
            resulting_revision=1,
            created_at=now,
        ))
        session.add(VisitPlanCommand(
            plan_id=WITHDRAWAL_PLAN_ID,
            event_seq=2,
            idempotency_key=WITHDRAWAL_PLAN_APPROVE_KEY,
            command_type="approve",
            request_hash=_plan_command_hash("approve", {
                "plan_id": WITHDRAWAL_PLAN_ID,
                "idempotency_key": WITHDRAWAL_PLAN_APPROVE_KEY,
                "expected_revision": 1,
            }),
            actor_id="WITHDRAW-RESEARCHER",
            expected_revision=1,
            resulting_revision=2,
            created_at=now,
        ))
        session.commit()
    audio_store.save_blob("withdrawal-recorded-audio", audio_bytes, "audio/webm")
    return audio_bytes


def _command(reason: str = "participant_request", key: str | None = None) -> dict:
    return {
        "idempotency_key": key or (
            "withdrawal-test-0123456789abcdef-ABCDEFGH-9876543210"),
        "expected_governance_revision": 0,
        "reason_code": reason,
    }


def test_withdrawal_is_atomic_replayable_and_terminal_sessions_stay_terminal(
        withdrawal_clients):
    engine = withdrawal_clients["engine"]
    admin = withdrawal_clients["admin"]
    _seed_patient_scope(engine)
    with Session(engine) as session:
        session.add(AssessmentEvent(
            event_id="ASSESSMENT-WITHDRAW-FROZEN",
            patient_id="P-WITHDRAW",
            assigned_assessor_id="WITHDRAW-RESEARCHER",
            timepoint="pretest",
            scheduled_date=date.today(),
            status="in_progress",
            revision=2,
            is_simulation=False,
            data_classification="research",
            formal_outcome_eligible=False,
            definition_bundle_id="assessment-withdrawal-test-v1",
            definition_bundle_digest="sha256:" + "a" * 64,
            active_protocol_slot_key="d" * 64,
            created_by="WITHDRAW-RESEARCHER",
            started_at=datetime.now(),
        ))
        session.commit()

    # The immutable scheduling fact is visible to the research operator before
    # withdrawal (the current draft research content independently keeps this
    # legacy approval out of the bedside queue).
    before_plans = withdrawal_clients["researcher"].get(
        "/visit-plans", params={"patient_id": "P-WITHDRAW"})
    assert before_plans.status_code == 200, before_plans.text
    assert [row["plan_id"] for row in before_plans.json()] == [
        WITHDRAWAL_PLAN_ID]

    result = admin.post("/patients/P-WITHDRAW/withdrawal", json=_command())
    assert result.status_code == 200, result.text
    receipt = result.json()
    assert receipt["idempotent"] is False
    assert receipt["governance_revision"] == 1
    assert receipt["affected_session_count"] == 3
    assert receipt["affected_audio_count"] == 1
    assert receipt["reason_code"] == "participant_request"
    assert "idempotency_key" not in receipt

    replay = admin.post("/patients/P-WITHDRAW/withdrawal", json=_command())
    assert replay.status_code == 200
    assert replay.json()["event_id"] == receipt["event_id"]
    assert replay.json()["idempotent"] is True

    for role in ("researcher", "steward", "admin"):
        queue = withdrawal_clients[role].get("/visit-plans/today")
        assert queue.status_code == 200, queue.text
        assert queue.json()["plans"] == []
    for role in ("researcher", "steward"):
        plans = withdrawal_clients[role].get(
            "/visit-plans", params={"patient_id": "P-WITHDRAW"})
        assert plans.status_code == 200, plans.text
        assert plans.json() == []
    admin_plans = admin.get(
        "/visit-plans", params={"patient_id": "P-WITHDRAW"})
    assert admin_plans.status_code == 200, admin_plans.text
    assert [row["plan_id"] for row in admin_plans.json()] == [
        WITHDRAWAL_PLAN_ID]
    assert admin_plans.json()[0]["status"] == "approved"
    denied_start = admin.post(
        f"/visit-plans/{WITHDRAWAL_PLAN_ID}/start",
        json={
            "idempotency_key": "withdrawal-plan-start-denied-0001",
            "expected_revision": 2,
        },
    )
    assert denied_start.status_code == 409, denied_start.text
    assert denied_start.json()["detail"]["code"] == (
        "visit_plan_patient_ineligible")

    denied_cancel = admin.post(
        f"/visit-plans/{WITHDRAWAL_PLAN_ID}/cancel",
        json={
            "idempotency_key": "withdrawal-plan-cancel-denied-0001",
            "expected_revision": 2,
            "reason_code": "schedule_changed",
        },
    )
    assert denied_cancel.status_code == 409, denied_cancel.text
    assert denied_cancel.json()["detail"]["code"] == (
        "visit_plan_patient_withdrawn")
    denied_create = admin.post("/visit-plans", json={
        "patient_id": "P-WITHDRAW",
        "scheduled_date": date.today().isoformat(),
        "scheduled_time": "10:00:00",
        "queue_order": 2,
        "session_sitting_no": 2,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "idempotency_key": "withdrawal-plan-create-denied-0001",
    })
    assert denied_create.status_code == 409, denied_create.text
    assert denied_create.json()["detail"]["code"] == (
        "visit_plan_patient_withdrawn")

    runtime_cursor = {
        "screen": "present",
        "itemIdx": 0,
        "turnIdx": 0,
        "responseRole": "命名",
        "recording": "idle",
    }
    blocked_runtime_writes = (
        admin.put(
            "/sessions/S-WITHDRAW-ACTIVE/runtime/cursor",
            json=runtime_cursor),
        admin.post("/sessions/S-WITHDRAW-ACTIVE/pause"),
        admin.post("/sessions/S-WITHDRAW-ACTIVE/resume"),
        admin.post(
            "/sessions/S-WITHDRAW-ACTIVE/autopilot/start",
            json={
                "idempotency_key": "withdrawal-autopilot-start-denied-0001",
                "expected_revision": 0,
            }),
    )
    for blocked in blocked_runtime_writes:
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["detail"]["code"] == (
            "subject_withdrawn_content_unavailable")
    # Takeover is a safety-exit command, not a content/runtime reopen.  It may
    # cross a withdrawal only far enough to reconcile existing autonomous
    # ownership; this inactive fixture therefore reaches its domain gate rather
    # than being rejected by the ordinary withdrawn-content guard.
    safe_takeover = admin.post(
        "/sessions/S-WITHDRAW-ACTIVE/autopilot/takeover",
        json={
            "idempotency_key": "withdrawal-safe-takeover-domain-0001",
            "expected_revision": 0,
        },
    )
    assert safe_takeover.status_code == 409, safe_takeover.text
    assert safe_takeover.json()["detail"]["code"] == "autopilot_not_active"

    with Session(engine) as session:
        patient = session.get(Patient, "P-WITHDRAW")
        assert patient is not None
        assert patient.withdrawal_status == "withdrawn"
        assert patient.consent_status == "withdrawn"
        assert patient.cloud_processing_allowed is False
        assert patient.cloud_processing_revoked_at is not None
        assert patient.governance_revision == 1
        plan = session.get(VisitPlan, WITHDRAWAL_PLAN_ID)
        assert plan is not None
        assert plan.status == "approved"
        assert plan.protocol_slot_key == _withdrawal_plan_slot_key()
        assert plan.status == "approved" and plan.revision == 2
        frozen_assessment = session.get(
            AssessmentEvent, "ASSESSMENT-WITHDRAW-FROZEN")
        assert frozen_assessment is not None
        assert frozen_assessment.status == "in_progress"
        assert frozen_assessment.revision == 2
        assert frozen_assessment.closed_at is None
        assert frozen_assessment.active_protocol_slot_key == "d" * 64
        active = session.get(SessionRuntimeState, "S-WITHDRAW-ACTIVE")
        paused = session.get(SessionRuntimeState, "S-WITHDRAW-PAUSED")
        completed = session.get(SessionRuntimeState, "S-WITHDRAW-COMPLETED")
        assert active.status == paused.status == "aborted"
        assert active.end_reason == paused.end_reason == "subject_withdrawal"
        assert completed.status == "completed"
        assert completed.end_reason == "completion_gate_passed"
        audio = session.get(AudioAssetRow, "withdrawal-recorded-audio")
        assert audio.withdrawn is True
        assert audio.withdrawal_status == "isolated_by_subject_withdrawal"
        capability = session.get(PatientDeviceCapability, "c" * 64)
        assert capability.active_session_key is None
        assert capability.recovery_only_at is not None
        live = session.get(LiveState, 1)
        assert live.patient_rec_json is None
        assert '"runtimeStatus": "aborted"' in live.session_json
        assert '"screen": "thanks"' in live.cursor_json
        events = list(session.exec(select(PatientWithdrawalEvent)))
        assert len(events) == 1
        assert set(events[0].model_dump()) == {
            "event_id", "patient_id", "expected_revision", "new_revision",
            "idempotency_key_sha256", "request_fingerprint", "reason_code",
            "actor_display_id", "actor_role", "occurred_at",
            "affected_session_count", "affected_audio_count",
        }

    # Every post-withdrawal capture/AI edge is fenced.  Existing uploaded bytes
    # remain isolated for governance; no new registration or Attempt fact appears.
    recording = admin.post(
        "/sessions/S-WITHDRAW-ACTIVE/recording-authorization")
    assert recording.status_code == 409
    registration = admin.post("/audio", json={
        "raw_audio_id": "post-withdrawal-new-audio",
        "session_id": "S-WITHDRAW-ACTIVE",
        "turn_key": "SE_锚#1",
    })
    assert registration.status_code == 403
    assert registration.json()["detail"]["code"] == (
        "audio_registration_device_required")
    processing = admin.post(
        "/sessions/S-WITHDRAW-ACTIVE/attempts/process",
        json={
            "item_id": "SE_锚",
            "turn_seq": 1,
            "response_role": "命名",
            "raw_audio_id": "withdrawal-recorded-audio",
            "prompt_level": 0,
            "duration_seconds": 1,
        },
    )
    assert processing.status_code == 409
    assert admin.get("/audio/withdrawal-recorded-audio").status_code == 409

    patient_snapshot = admin.get("/patients/P-WITHDRAW")
    assert patient_snapshot.status_code == 200, patient_snapshot.text
    snapshot = patient_snapshot.json()
    expected = {
        "allowed": snapshot.get("cloud_processing_allowed"),
        "provider_id": snapshot.get("cloud_processing_provider_id"),
        "notice_version": snapshot.get("cloud_processing_notice_version"),
        "consented_at": snapshot.get("cloud_processing_consented_at"),
        "revoked_at": snapshot.get("cloud_processing_revoked_at"),
        "withdrawal_status": snapshot.get("withdrawal_status"),
        "governance_revision": snapshot["governance_revision"],
    }
    for client, allowed in ((withdrawal_clients["researcher"], False),
                            (admin, True)):
        denied = client.patch(
            "/patients/P-WITHDRAW/cloud-processing",
            json={
                "allowed": allowed,
                "expected": expected,
                "policy_provider_id": "aliyun-dashscope" if allowed else None,
                "policy_notice_version": "withdrawn-must-stay-locked" if allowed else None,
            },
        )
        assert denied.status_code == 409, denied.text
        assert denied.json()["detail"]["code"] == (
            "subject_withdrawn_cloud_processing_locked")
    with Session(engine) as session:
        patient = session.get(Patient, "P-WITHDRAW")
        assert patient is not None
        assert patient.cloud_processing_allowed == expected["allowed"]
        assert patient.cloud_processing_provider_id == expected["provider_id"]
        assert patient.cloud_processing_notice_version == expected["notice_version"]
        assert patient.cloud_processing_revoked_at.isoformat() == expected["revoked_at"]
        assert patient.governance_revision == expected["governance_revision"]
        assert session.get(AudioAssetRow, "post-withdrawal-new-audio") is None


def test_pause_cannot_stale_write_runtime_across_subject_withdrawal(
        withdrawal_clients, monkeypatch):
    """Model separate workers by disabling the legacy process-only Live lock."""
    engine = withdrawal_clients["engine"]
    admin = withdrawal_clients["admin"]
    _seed_patient_scope(engine)

    import app.main as main_mod

    monkeypatch.setattr(main_mod, "_LIVE_WRITE_LOCK", nullcontext())
    entered_pause = Event()
    release_pause = Event()
    withdrawal_started = Event()
    real_pause = main_mod.autopilot_service.pause_autonomous_scope_for_researcher

    def slow_pause(*args, **kwargs):
        entered_pause.set()
        assert release_pause.wait(timeout=5)
        return real_pause(*args, **kwargs)

    monkeypatch.setattr(
        main_mod.autopilot_service,
        "pause_autonomous_scope_for_researcher",
        slow_pause,
    )
    peer = TestClient(app)
    login = peer.post("/auth/login", json={
        "username": "withdraw-admin", "password": "test-password-1",
    })
    assert login.status_code == 200, login.text
    peer.headers.update({
        "X-CSRF-Token": peer.cookies.get(auth.CSRF_COOKIE_NAME),
    })

    def withdraw():
        withdrawal_started.set()
        return peer.post(
            "/patients/P-WITHDRAW/withdrawal",
            json=_command(key=(
                "withdrawal-pause-race-0123456789abcdef-ABCDEFGH")),
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            paused_future = pool.submit(
                admin.post, "/sessions/S-WITHDRAW-ACTIVE/pause")
            assert entered_pause.wait(timeout=5)
            withdrawn_future = pool.submit(withdraw)
            assert withdrawal_started.wait(timeout=5)
            # The subject fence, not the disabled Live process lock, keeps the
            # withdrawal transaction out until pause commits.
            Event().wait(0.05)
            assert not withdrawn_future.done()
            release_pause.set()
            paused = paused_future.result(timeout=10)
            withdrawn = withdrawn_future.result(timeout=10)
    finally:
        release_pause.set()
        peer.close()

    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"
    assert withdrawn.status_code == 200, withdrawn.text
    with Session(engine) as session:
        runtime = session.get(SessionRuntimeState, "S-WITHDRAW-ACTIVE")
        patient = session.get(Patient, "P-WITHDRAW")
        assert runtime is not None and runtime.status == "aborted"
        assert runtime.end_reason == "subject_withdrawal"
        assert runtime.revision >= 4
        assert patient is not None and patient.withdrawal_status == "withdrawn"
        assert list(session.exec(select(AttemptEvent).where(
            AttemptEvent.session_id == "S-WITHDRAW-ACTIVE"))) == []


def test_withdrawal_conflicts_permissions_tombstone_and_admin_delete(
        withdrawal_clients):
    engine = withdrawal_clients["engine"]
    researcher = withdrawal_clients["researcher"]
    steward = withdrawal_clients["steward"]
    admin = withdrawal_clients["admin"]
    _seed_patient_scope(engine)

    assert steward.post(
        "/patients/P-WITHDRAW/withdrawal", json=_command()).status_code == 403
    assert researcher.post(
        "/patients/P-WITHDRAW/withdrawal", json=_command()).status_code == 403
    assert admin.post(
        "/patients/P-WITHDRAW/withdrawal", json=_command()).status_code == 200
    assert admin.post(
        "/patients/P-WITHDRAW/withdrawal",
        json={**_command(), "unexpected_note": "不得接受自由文本"},
    ).status_code == 422
    conflict = admin.post(
        "/patients/P-WITHDRAW/withdrawal",
        json=_command("clinical_safety"),
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "withdrawal_idempotency_conflict"
    stale = admin.post(
        "/patients/P-WITHDRAW/withdrawal",
        json={**_command(key="withdrawal-new-0123456789abcdef-ABCDEFGH-1234567890"),
              "expected_governance_revision": 0},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "subject_already_withdrawn"
    with Session(engine) as session:
        session.add(Patient(
            patient_id="P-CROSS-KEY",
            is_simulation_subject=False,
            governance_revision=0,
        ))
        session.commit()
    cross_patient = admin.post(
        "/patients/P-CROSS-KEY/withdrawal", json=_command())
    assert cross_patient.status_code == 409
    assert cross_patient.json()["detail"]["code"] == (
        "withdrawal_idempotency_conflict")

    assert researcher.get("/patients/P-WITHDRAW/withdrawal").status_code == 403
    assert steward.get("/patients/P-WITHDRAW/withdrawal").status_code == 200
    assert admin.get("/patients/P-WITHDRAW/withdrawal").status_code == 200
    assert researcher.get("/governance/withdrawn-audio").status_code == 403
    assert steward.get("/governance/withdrawn-audio").status_code == 403
    governed = admin.get("/governance/withdrawn-audio")
    assert governed.status_code == 200
    assert governed.json() == [{
        "raw_audio_id": "withdrawal-recorded-audio",
        "session_id": "S-WITHDRAW-ACTIVE",
        "patient_id": "P-WITHDRAW",
        "status": "recorded",
        "withdrawn": True,
        "withdrawal_status": "isolated_by_subject_withdrawal",
        "delete_gate_passed": False,
        # 0 回执必须显式为 0/None,面板据此如实显示"尚无设备确认删除"。
        "local_copy_disposal_device_count": 0,
        "local_copy_disposal_last_at": None,
    }]

    assert researcher.delete(
        "/audio/withdrawal-recorded-audio",
        params={"source": "withdrawal", "session_id": "S-WITHDRAW-ACTIVE"},
    ).status_code == 403
    wrong_session = admin.delete(
        "/audio/withdrawal-recorded-audio",
        params={"source": "withdrawal", "session_id": "S-WRONG"},
    )
    assert wrong_session.status_code == 409
    removed = admin.delete(
        "/audio/withdrawal-recorded-audio",
        params={"source": "withdrawal", "session_id": "S-WITHDRAW-ACTIVE"},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["status"] == "deleted"
    assert audio_store.find_blob("withdrawal-recorded-audio") is None


def test_withdrawal_closes_intervention_review_and_blocks_all_content_writes(
        withdrawal_clients):
    engine = withdrawal_clients["engine"]
    researcher = withdrawal_clients["researcher"]
    steward = withdrawal_clients["steward"]
    admin = withdrawal_clients["admin"]
    secret = "WITHDRAWN-SECRET-ASR"
    now = datetime.now()
    with Session(engine) as session:
        session.add(Patient(
            patient_id="P-WITHDRAW-REVIEW",
            is_simulation_subject=False,
            consent_status="active",
            mandarin_eligible=True,
            recording_allowed=True,
            governance_revision=0,
        ))
        session.add(TrainSession(
            session_id="S-WITHDRAW-REVIEW",
            patient_id="P-WITHDRAW-REVIEW",
            week_no=2,
            phase_type=PhaseType.正式训练,
            event_line=EventLine.正式训练,
            item_bank_version_id="wk2-v1-20260707",
            trainer_id="WITHDRAW-RESEARCHER",
            is_simulation=False,
            data_classification="research",
        ))
        session.add(SessionRuntimeState(
            session_id="S-WITHDRAW-REVIEW",
            status="intervention_completed",
            revision=4,
            intervention_completed_at=now,
            intervention_ended_by="SERVER-AUTOPILOT",
            updated_at=now,
        ))
        session.flush()
        item = ItemEvent(
            session_id="S-WITHDRAW-REVIEW",
            item_id="SE_锚",
            task_type="单要素",
            item_set_type="训练集",
        )
        session.add(item)
        session.flush()
        turn = TurnEvent(
            item_event_id=item.id,
            turn_seq=1,
            response_role="命名",
            asr_text=secret,
            confirmation_revision=0,
        )
        session.add(turn)
        session.commit()
        session.refresh(turn)
        turn_id = int(turn.id)

    withdrawn = admin.post(
        "/patients/P-WITHDRAW-REVIEW/withdrawal",
        json={
            "idempotency_key": (
                "withdrawal-review-window-0123456789abcdef-ABCDEFGH"),
            "expected_governance_revision": 0,
            "reason_code": "participant_request",
        },
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["affected_session_count"] == 1

    confirm = researcher.patch(f"/turns/{turn_id}/confirm", json={
        "confirmed_response_text": "must-not-commit",
        "expected_revision": 0,
        "idempotency_key": "withdrawn-review-confirm-0001",
    })
    complete = researcher.post("/sessions/S-WITHDRAW-REVIEW/complete")
    closeout = researcher.put("/sessions/S-WITHDRAW-REVIEW/closeout", json={
        "idempotency_key": "withdrawn-review-closeout-0001",
        "expected_revision": 0,
        "report_status": "no_additional_observation",
    })
    for response in (confirm, complete, closeout):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == (
            "subject_withdrawn_content_unavailable")
        assert secret not in response.text

    tombstone = steward.get("/sessions/S-WITHDRAW-REVIEW/journal")
    assert tombstone.status_code == 200, tombstone.text
    assert tombstone.json()["session"]["content_state"] == (
        "withdrawn_tombstone")
    assert secret not in tombstone.text

    with Session(engine) as session:
        runtime = session.get(SessionRuntimeState, "S-WITHDRAW-REVIEW")
        assert runtime is not None
        assert runtime.status == "aborted"
        assert runtime.end_reason == "subject_withdrawal"
        turn = session.get(TurnEvent, turn_id)
        assert turn is not None
        assert turn.confirmed_response_text is None
        assert turn.confirmation_revision == 0
        assert list(session.exec(select(TurnConfirmationRevision).where(
            TurnConfirmationRevision.turn_id == turn_id))) == []


def test_server_owned_state_low_entropy_and_append_only_ledger(
        withdrawal_clients):
    researcher = withdrawal_clients["researcher"]
    admin = withdrawal_clients["admin"]
    engine = withdrawal_clients["engine"]
    assert researcher.post("/patients", json={
        "patient_id": "P-BYPASS", "withdrawal_status": "withdrawn",
    }).status_code == 422
    assert researcher.post("/patients", json={
        "patient_id": "P-BYPASS", "governance_revision": 7,
    }).status_code == 422
    assert researcher.post("/patients", json={
        "patient_id": "P-LOW-ENTROPY", "governance_revision": 0,
    }).status_code == 200
    low_entropy = admin.post(
        "/patients/P-LOW-ENTROPY/withdrawal",
        json={
            "idempotency_key": "a" * 32,
            "expected_governance_revision": 0,
            "reason_code": "participant_request",
        },
    )
    assert low_entropy.status_code == 422
    with Session(engine) as session:
        session.add(Patient(
            patient_id="P-STALE-CAS",
            is_simulation_subject=False,
            governance_revision=1,
        ))
        session.commit()
    stale = admin.post("/patients/P-STALE-CAS/withdrawal", json={
        "idempotency_key": "stale-cas-0123456789abcdef-ABCDEFGH-1234567890",
        "expected_governance_revision": 0,
        "reason_code": "ethics_or_protocol",
    })
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == (
        "patient_governance_revision_conflict")

    _seed_patient_scope(engine)
    assert admin.post(
        "/patients/P-WITHDRAW/withdrawal", json=_command()).status_code == 200
    with Session(engine) as session:
        row = session.exec(select(PatientWithdrawalEvent)).one()
        row.reason_code = "clinical_safety"
        with pytest.raises(RuntimeError, match="只追加"):
            session.commit()
        session.rollback()
        row = session.exec(select(PatientWithdrawalEvent)).one()
        with pytest.raises(RuntimeError, match="只追加"):
            session.delete(row)
            session.commit()


def test_withdrawal_migration_fresh_check_and_parent_roundtrip(tmp_path):
    db_path = tmp_path / "withdrawal-migration.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "governance_revision" in {
        column["name"] for column in inspector.get_columns("patient")}
    assert "patientwithdrawalevent" in inspector.get_table_names()
    assert "ix_patientwithdrawalevent_patient_id" in {
        index["name"] for index in inspector.get_indexes(
            "patientwithdrawalevent")}
    command.check(config)
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == (
                "b6d4f8a2c917")

    command.downgrade(config, "f2b7d4e9a106")
    inspector = inspect(engine)
    assert "patientwithdrawalevent" not in inspector.get_table_names()
    assert "governance_revision" not in {
        column["name"] for column in inspector.get_columns("patient")}
    command.upgrade(config, "head")
    command.check(config)
