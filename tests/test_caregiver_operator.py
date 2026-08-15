"""Narrow caregiver-operator HTTP vertical and deny-by-default boundary."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import (
    autopilot_plan_profiles,
    auth,
    caregiver_service,
    db,
    provider_readiness,
    repeat_intent,
    visit_plan_contract,
    visit_plan_service,
)
from app import main as main_module
from app.main import app
from app.models import (
    CaregiverHelpDisposition,
    CaregiverHelpRequest,
    Patient,
    ProviderReadinessProbe,
    ResearchUser,
    Session as TrainSession,
    SessionRuntimeState,
    VisitPlan,
    VisitPlanCommand,
)


OWNER_SESSION = "S-CAREGIVER-OWN"
FOREIGN_SESSION = "S-CAREGIVER-FOREIGN"
PATIENT_ID = "P-CAREGIVER"
OWNER_ACTOR = "ACTOR-caregiver-owner"
FOREIGN_ACTOR = "ACTOR-caregiver-other"
DEMO_VERSION = autopilot_plan_profiles.WEEK2_SINGLE20_DEMO_VERSION
DEMO_DIGEST = autopilot_plan_profiles.WEEK2_SINGLE20_DEMO_DIGEST


@dataclass
class CaregiverClients:
    owner: TestClient
    other: TestClient
    engine: object


@dataclass
class CaregiverVisitPlanClients:
    researcher: TestClient
    owner: TestClient
    other: TestClient
    engine: object


def _login(username: str) -> TestClient:
    client = TestClient(app)
    response = client.post("/auth/login", json={
        "username": username,
        "password": "password1",
    })
    assert response.status_code == 200, response.text
    csrf = client.cookies.get(auth.CSRF_COOKIE_NAME)
    assert csrf
    client.headers["X-CSRF-Token"] = csrf
    return client


def _visit_plan_write_counts(engine) -> tuple[int, int, int, int]:
    with Session(engine) as session:
        return (
            len(list(session.exec(select(VisitPlan)))),
            len(list(session.exec(select(VisitPlanCommand)))),
            len(list(session.exec(select(TrainSession)))),
            len(list(session.exec(select(SessionRuntimeState)))),
        )


def _freeze_provider_configuration(monkeypatch):
    configuration = provider_readiness.capture_configuration()
    monkeypatch.setattr(
        provider_readiness,
        "capture_configuration",
        lambda **_kwargs: configuration,
    )
    return configuration


def _add_provider_probe(engine, monkeypatch, *, state: str = "ready"):
    """Append a network-free readiness fact for one isolated test database."""
    configuration = _freeze_provider_configuration(monkeypatch)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ready_capabilities = state != "required_capability_failed"
    checked_at = now - timedelta(hours=2) if state == "expired" else now
    expires_at = (
        now - timedelta(hours=1)
        if state == "expired"
        else now + timedelta(hours=1)
    )
    fingerprint = configuration.fingerprint
    if state == "config_mismatch":
        fingerprint = (
            "0" * 64 if configuration.fingerprint != "0" * 64 else "1" * 64)
    with Session(engine) as session:
        session.add(ProviderReadinessProbe(
            probe_id=f"prb_caregiver_{state}",
            schema_version=provider_readiness.SCHEMA_VERSION,
            runtime_contract=provider_readiness.RUNTIME_CONTRACT,
            config_fingerprint=fingerprint,
            tts_engine_version=configuration.tts_engine_version,
            asr_engine_version=configuration.asr_engine_version,
            llm_engine_version=configuration.llm_engine_version,
            tts_required=True,
            tts_success=ready_capabilities,
            tts_failure_code=(
                None if ready_capabilities else "tts_synthetic_probe_failed"),
            asr_required=True,
            asr_success=True,
            llm_required=False,
            llm_configured=configuration.llm_configured,
            llm_success=configuration.llm_configured,
            llm_failure_code=(
                None if configuration.llm_configured
                else "llm_not_required_not_configured"),
            required_capabilities_ready=ready_capabilities,
            all_configured_capabilities_ready=ready_capabilities,
            probe_failure_code=(
                None if ready_capabilities else "tts_synthetic_probe_failed"),
            checked_at=checked_at,
            expires_at=expires_at,
            actor_display_id="ACTOR-provider-admin",
        ))
        session.commit()
    if state == "expired":
        monkeypatch.setattr(
            provider_readiness,
            "_utc_now_naive",
            lambda: now + timedelta(hours=3),
        )
    return configuration


def _create_and_approve_plan(
    client: TestClient,
    *,
    key_suffix: str,
    session_sitting_no: int,
    demo20: bool,
) -> tuple[dict, dict]:
    payload = {
        "patient_id": PATIENT_ID,
        "scheduled_date": visit_plan_service._research_today().isoformat(),
        "scheduled_time": "09:30:00",
        "queue_order": session_sitting_no,
        "session_sitting_no": session_sitting_no,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "idempotency_key": f"caregiver-create-{key_suffix}",
    }
    if demo20:
        payload["autopilot_profile_version_id"] = DEMO_VERSION
    created_response = client.post("/visit-plans", json=payload)
    assert created_response.status_code == 200, created_response.text
    created = created_response.json()
    approved_response = client.post(
        f"/visit-plans/{created['plan_id']}/approve",
        json={
            "idempotency_key": f"caregiver-approve-{key_suffix}",
            "expected_revision": created["revision"],
        },
    )
    assert approved_response.status_code == 200, approved_response.text
    return created, approved_response.json()


@pytest.fixture
def caregiver_clients(monkeypatch) -> CaregiverClients:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    now = datetime.now()
    repeat_protocol = repeat_intent.active_protocol()
    with Session(engine) as session:
        session.add(Patient(
            patient_id=PATIENT_ID,
            is_simulation_subject=True,
            consent_status="已同意",
            recording_allowed=True,
        ))
        session.add_all([
            ResearchUser(
                username="caregiver-owner",
                display_id=OWNER_ACTOR,
                password_hash=auth.hash_password("password1"),
                role="caregiver_operator",
                created_at=now,
            ),
            ResearchUser(
                username="caregiver-other",
                display_id=FOREIGN_ACTOR,
                password_hash=auth.hash_password("password1"),
                role="caregiver_operator",
                created_at=now,
            ),
        ])
        for session_id, actor in (
            (OWNER_SESSION, OWNER_ACTOR),
            (FOREIGN_SESSION, FOREIGN_ACTOR),
        ):
            session.add(TrainSession(
                session_id=session_id,
                patient_id=PATIENT_ID,
                training_date=date.today(),
                week_no=2,
                phase_type="正式训练",
                event_line="正式训练",
                trainer_id=actor,
                item_bank_version_id="wk2-v1-20260707",
                repeat_protocol_version_id=repeat_protocol.version_id,
                repeat_protocol_definition_digest=(
                    repeat_protocol.definition_digest),
                is_simulation=True,
                data_classification="simulation",
            ))
            session.add(SessionRuntimeState(
                session_id=session_id,
                status="active",
                revision=0,
                updated_at=now,
            ))
        session.commit()

    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    monkeypatch.setenv("ALLOW_SIMULATION_DATA", "1")
    monkeypatch.setenv("ENABLE_AUTOPILOT_P0A_SIMULATION", "1")
    owner = _login("caregiver-owner")
    other = _login("caregiver-other")
    try:
        yield CaregiverClients(owner=owner, other=other, engine=engine)
    finally:
        owner.close()
        other.close()


@pytest.fixture
def caregiver_visit_plan_clients(
        monkeypatch, tmp_path) -> CaregiverVisitPlanClients:
    """A fresh, real scheduling chain with no pre-existing caregiver session."""
    today = visit_plan_service._research_today()
    monkeypatch.setattr(visit_plan_service, "_research_today", lambda: today)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'caregiver-visit-plan.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setenv("ALLOW_SIMULATION_DATA", "1")
    monkeypatch.setenv("ENABLE_AUTOPILOT_P0A_SIMULATION", "1")
    # This acceptance path must prove the real VisitPlan/session binding gate,
    # not the repository-wide direct-session convenience used by unit tests.
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    SQLModel.metadata.create_all(engine)
    now = datetime.now()
    password_hash = auth.hash_password("password1")
    with Session(engine) as session:
        session.add(Patient(
            patient_id=PATIENT_ID,
            is_simulation_subject=True,
            consent_status="已同意",
            consent_type="本人同意",
            mandarin_eligible=True,
            recording_allowed=True,
            secondary_use_allowed=True,
        ))
        session.add_all([
            ResearchUser(
                username="visit-plan-researcher",
                display_id="ACTOR-visit-plan-researcher",
                password_hash=password_hash,
                role="researcher",
                created_at=now,
            ),
            ResearchUser(
                username="visit-plan-caregiver",
                display_id=OWNER_ACTOR,
                password_hash=password_hash,
                role="caregiver_operator",
                created_at=now,
            ),
            ResearchUser(
                username="visit-plan-other-caregiver",
                display_id=FOREIGN_ACTOR,
                password_hash=password_hash,
                role="caregiver_operator",
                created_at=now,
            ),
        ])
        session.commit()

    monkeypatch.setenv("REQUIRE_AUTH", "1")
    clients = CaregiverVisitPlanClients(
        researcher=_login("visit-plan-researcher"),
        owner=_login("visit-plan-caregiver"),
        other=_login("visit-plan-other-caregiver"),
        engine=engine,
    )
    try:
        yield clients
    finally:
        clients.researcher.close()
        clients.owner.close()
        clients.other.close()


def test_caregiver_login_today_status_activation_and_logout(caregiver_clients):
    owner = caregiver_clients.owner
    today = owner.get("/caregiver/today")
    assert today.status_code == 200, today.text
    assert today.headers["cache-control"] == "private, no-store"
    assert today.json()["plans"] == []
    assert today.json()["current_session"] == {
        "session_id": OWNER_SESSION,
        "participant_code": PATIENT_ID,
        "session_sitting_no": 1,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "is_simulation": True,
        "data_classification": "simulation",
        "autopilot_profile_version_id": None,
        "completion_scope": "canonical_full_source",
        "resolved_position_count": 70,
        "operational_demo_ready": False,
        "runtime_status": "active",
        "runtime_revision": 0,
    }

    before = owner.get(f"/caregiver/sessions/{OWNER_SESSION}/status")
    assert before.status_code == 200, before.text
    assert before.json()["active_bedside_session"] is False
    assert before.json()["autopilot"]["takeover_ready"] is False
    assert "cursor" not in before.json()
    assert "rapportStep" not in before.json()

    activated = owner.put(
        f"/caregiver/sessions/{OWNER_SESSION}/activation")
    assert activated.status_code == 200, activated.text
    assert activated.json()["idempotent"] is False
    assert activated.json()["live_seq"] >= 1
    assert activated.json()["live_wseq"] >= 1

    replayed = owner.put(
        f"/caregiver/sessions/{OWNER_SESSION}/activation")
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["idempotent"] is True
    assert replayed.json()["live_seq"] == activated.json()["live_seq"]
    assert replayed.json()["live_wseq"] == activated.json()["live_wseq"]

    status = owner.get(f"/caregiver/sessions/{OWNER_SESSION}/status")
    assert status.status_code == 200, status.text
    assert status.json()["active_bedside_session"] is True
    assert set(status.json()) == {
        "session_id", "participant_code", "session_sitting_no", "week_no",
        "phase_type", "event_line", "runtime_status", "runtime_revision",
        "is_simulation", "data_classification",
        "autopilot_profile_version_id", "completion_scope",
        "resolved_position_count", "operational_demo_ready",
        "active_bedside_session", "patient_presence", "autopilot",
    }

    logout = owner.post("/auth/logout")
    assert logout.status_code == 200, logout.text
    assert owner.get("/caregiver/today").status_code == 401


def test_real_approved_plan_to_owned_caregiver_bedside_vertical(
        caregiver_visit_plan_clients, monkeypatch):
    """Exercise the real VisitPlan service; no start/queue boundary is mocked."""
    researcher = caregiver_visit_plan_clients.researcher
    owner = caregiver_visit_plan_clients.owner
    other = caregiver_visit_plan_clients.other
    today = visit_plan_service._research_today().isoformat()

    created_response = researcher.post("/visit-plans", json={
        "patient_id": PATIENT_ID,
        "scheduled_date": today,
        "scheduled_time": "09:30:00",
        "queue_order": 1,
        "session_sitting_no": 1,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "autopilot_profile_version_id": DEMO_VERSION,
        "idempotency_key": "caregiver-real-create-0001",
    })
    assert created_response.status_code == 200, created_response.text
    created = created_response.json()
    approved_response = researcher.post(
        f"/visit-plans/{created['plan_id']}/approve",
        json={
            "idempotency_key": "caregiver-real-approve-0001",
            "expected_revision": created["revision"],
        },
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()
    assert approved["status"] == "approved"
    _add_provider_probe(
        caregiver_visit_plan_clients.engine, monkeypatch, state="ready")

    queue_response = owner.get("/caregiver/today")
    assert queue_response.status_code == 200, queue_response.text
    queue = queue_response.json()
    assert queue["current_session"] is None
    assert [plan["plan_id"] for plan in queue["plans"]] == [created["plan_id"]]
    assert queue["plans"][0]["participant_code"] == PATIENT_ID
    assert queue["plans"][0] == {
        **queue["plans"][0],
        "is_simulation": True,
        "data_classification": "simulation",
        "autopilot_profile_version_id": DEMO_VERSION,
        "completion_scope": "demo_plan_only",
        "resolved_position_count": 20,
        "operational_demo_ready": True,
    }

    started_response = owner.post(
        f"/caregiver/visit-plans/{created['plan_id']}/start",
        json={
            "idempotency_key": "caregiver-real-start-000001",
            "expected_revision": approved["revision"],
        },
    )
    assert started_response.status_code == 200, started_response.text
    started = started_response.json()
    session_id = started["session"]["session_id"]
    assert started["session"]["runtime_status"] == "active"

    with Session(caregiver_visit_plan_clients.engine) as session:
        stored = session.get(TrainSession, session_id)
        assert stored is not None
        assert stored.visit_plan_id == created["plan_id"]
        assert stored.trainer_id == OWNER_ACTOR
        assert stored.autopilot_profile_version_id == DEMO_VERSION
        assert stored.autopilot_profile_definition_digest == DEMO_DIGEST

    # The new owner can activate and read the same session; another caregiver
    # receives the same not-found boundary as an unknown object.
    activated = owner.put(f"/caregiver/sessions/{session_id}/activation")
    assert activated.status_code == 200, activated.text
    status = owner.get(f"/caregiver/sessions/{session_id}/status")
    assert status.status_code == 200, status.text
    assert status.json()["session_id"] == session_id
    assert status.json()["active_bedside_session"] is True
    assert other.get(
        f"/caregiver/sessions/{session_id}/status").status_code == 404
    assert other.put(
        f"/caregiver/sessions/{session_id}/activation").status_code == 404


def test_caregiver_today_withholds_canonical_from_mixed_approved_queue(
        caregiver_visit_plan_clients, monkeypatch):
    researcher = caregiver_visit_plan_clients.researcher
    canonical, _ = _create_and_approve_plan(
        researcher,
        key_suffix="mixed-canonical-01",
        session_sitting_no=1,
        demo20=False,
    )
    exact, _ = _create_and_approve_plan(
        researcher,
        key_suffix="mixed-demo20-0001",
        session_sitting_no=2,
        demo20=True,
    )
    _add_provider_probe(
        caregiver_visit_plan_clients.engine, monkeypatch, state="ready")

    response = caregiver_visit_plan_clients.owner.get("/caregiver/today")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [row["plan_id"] for row in payload["plans"]] == [exact["plan_id"]]
    assert canonical["plan_id"] not in {
        row["plan_id"] for row in payload["plans"]}
    assert payload["withheld_count"] == 1
    assert payload["plans"][0]["operational_demo_ready"] is True


@pytest.mark.parametrize(
    "disabled_flag",
    ["ALLOW_SIMULATION_DATA", "ENABLE_AUTOPILOT_P0A_SIMULATION"],
)
def test_caregiver_today_withholds_exact_demo_when_either_flag_is_off(
        caregiver_visit_plan_clients, monkeypatch, disabled_flag):
    _create_and_approve_plan(
        caregiver_visit_plan_clients.researcher,
        key_suffix=f"today-flag-{disabled_flag.lower()}",
        session_sitting_no=1,
        demo20=True,
    )
    _add_provider_probe(
        caregiver_visit_plan_clients.engine, monkeypatch, state="ready")
    monkeypatch.delenv(disabled_flag, raising=False)

    response = caregiver_visit_plan_clients.owner.get("/caregiver/today")
    assert response.status_code == 200, response.text
    assert response.json()["plans"] == []
    assert response.json()["withheld_count"] == 1


@pytest.mark.parametrize(
    "state",
    ["missing", "expired", "config_mismatch", "required_capability_failed"],
)
def test_caregiver_today_withholds_all_due_plans_when_provider_is_not_ready(
        caregiver_visit_plan_clients, monkeypatch, state):
    _create_and_approve_plan(
        caregiver_visit_plan_clients.researcher,
        key_suffix=f"today-provider-{state}",
        session_sitting_no=1,
        demo20=True,
    )
    if state == "missing":
        _freeze_provider_configuration(monkeypatch)
    else:
        _add_provider_probe(
            caregiver_visit_plan_clients.engine, monkeypatch, state=state)

    response = caregiver_visit_plan_clients.owner.get("/caregiver/today")

    assert response.status_code == 200, response.text
    assert response.json()["plans"] == []
    assert response.json()["withheld_count"] == 1


@pytest.mark.parametrize(
    "state,expected_code",
    [
        ("missing", "provider_readiness_missing"),
        ("expired", "provider_readiness_expired"),
        ("config_mismatch", "provider_readiness_config_mismatch"),
        (
            "required_capability_failed",
            "provider_readiness_required_capability_failed",
        ),
    ],
)
def test_caregiver_start_provider_gate_is_stable_and_has_zero_writes(
        caregiver_visit_plan_clients, monkeypatch, state, expected_code):
    created, approved = _create_and_approve_plan(
        caregiver_visit_plan_clients.researcher,
        key_suffix=f"start-provider-{state}",
        session_sitting_no=1,
        demo20=True,
    )
    if state == "missing":
        _freeze_provider_configuration(monkeypatch)
    else:
        _add_provider_probe(
            caregiver_visit_plan_clients.engine, monkeypatch, state=state)
    before = _visit_plan_write_counts(caregiver_visit_plan_clients.engine)

    response = caregiver_visit_plan_clients.owner.post(
        f"/caregiver/visit-plans/{created['plan_id']}/start",
        json={
            "idempotency_key": f"caregiver-provider-{state}-start",
            "expected_revision": approved["revision"],
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": expected_code,
        "message": "语音服务未准备，请联系管理员",
    }
    assert _visit_plan_write_counts(caregiver_visit_plan_clients.engine) == before


def test_caregiver_start_revision_conflict_precedes_provider_gate(
        caregiver_visit_plan_clients, monkeypatch):
    created, approved = _create_and_approve_plan(
        caregiver_visit_plan_clients.researcher,
        key_suffix="start-provider-revision",
        session_sitting_no=1,
        demo20=True,
    )
    _freeze_provider_configuration(monkeypatch)
    before = _visit_plan_write_counts(caregiver_visit_plan_clients.engine)

    response = caregiver_visit_plan_clients.owner.post(
        f"/caregiver/visit-plans/{created['plan_id']}/start",
        json={
            "idempotency_key": "caregiver-provider-wrong-revision",
            "expected_revision": approved["revision"] + 1,
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "visit_plan_revision_conflict"
    assert _visit_plan_write_counts(caregiver_visit_plan_clients.engine) == before


def test_caregiver_open_session_precedes_later_provider_failure(
        caregiver_visit_plan_clients, monkeypatch):
    first_created, first_approved = _create_and_approve_plan(
        caregiver_visit_plan_clients.researcher,
        key_suffix="provider-priority-first",
        session_sitting_no=1,
        demo20=True,
    )
    second_created, second_approved = _create_and_approve_plan(
        caregiver_visit_plan_clients.researcher,
        key_suffix="provider-priority-second",
        session_sitting_no=2,
        demo20=True,
    )
    configuration = _add_provider_probe(
        caregiver_visit_plan_clients.engine, monkeypatch, state="ready")
    first = caregiver_visit_plan_clients.owner.post(
        f"/caregiver/visit-plans/{first_created['plan_id']}/start",
        json={
            "idempotency_key": "caregiver-provider-priority-first",
            "expected_revision": first_approved["revision"],
        },
    )
    assert first.status_code == 200, first.text

    # The already-open session is a durable business fact.  Even if provider
    # readiness changes afterwards, a second start must report that fact first
    # and leave every scheduling/runtime table unchanged.
    monkeypatch.setattr(
        provider_readiness,
        "capture_configuration",
        lambda **_kwargs: replace(configuration, fingerprint="f" * 64),
    )
    before = _visit_plan_write_counts(caregiver_visit_plan_clients.engine)
    second = caregiver_visit_plan_clients.owner.post(
        f"/caregiver/visit-plans/{second_created['plan_id']}/start",
        json={
            "idempotency_key": "caregiver-provider-priority-second",
            "expected_revision": second_approved["revision"],
        },
    )

    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "visit_plan_actor_session_open"
    assert _visit_plan_write_counts(caregiver_visit_plan_clients.engine) == before


def test_caregiver_canonical_start_is_refused_with_zero_writes(
        caregiver_visit_plan_clients):
    created, approved = _create_and_approve_plan(
        caregiver_visit_plan_clients.researcher,
        key_suffix="start-canonical-01",
        session_sitting_no=1,
        demo20=False,
    )
    before = _visit_plan_write_counts(caregiver_visit_plan_clients.engine)

    response = caregiver_visit_plan_clients.owner.post(
        f"/caregiver/visit-plans/{created['plan_id']}/start",
        json={
            "idempotency_key": "caregiver-start-canonical-01",
            "expected_revision": approved["revision"],
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == (
        "caregiver_plan_not_operational_demo20")
    assert _visit_plan_write_counts(caregiver_visit_plan_clients.engine) == before


@pytest.mark.parametrize(
    "disabled_flag",
    ["ALLOW_SIMULATION_DATA", "ENABLE_AUTOPILOT_P0A_SIMULATION"],
)
def test_caregiver_exact_start_flag_off_is_refused_with_zero_writes(
        caregiver_visit_plan_clients, monkeypatch, disabled_flag):
    created, approved = _create_and_approve_plan(
        caregiver_visit_plan_clients.researcher,
        key_suffix=f"start-flag-{disabled_flag.lower()}",
        session_sitting_no=1,
        demo20=True,
    )
    monkeypatch.delenv(disabled_flag, raising=False)
    before = _visit_plan_write_counts(caregiver_visit_plan_clients.engine)

    response = caregiver_visit_plan_clients.owner.post(
        f"/caregiver/visit-plans/{created['plan_id']}/start",
        json={
            "idempotency_key": f"caregiver-start-{disabled_flag.lower()}",
            "expected_revision": approved["revision"],
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == (
        "caregiver_plan_not_operational_demo20")
    assert _visit_plan_write_counts(caregiver_visit_plan_clients.engine) == before


def test_caregiver_successful_start_replays_after_flags_are_removed(
        caregiver_visit_plan_clients, monkeypatch):
    created, approved = _create_and_approve_plan(
        caregiver_visit_plan_clients.researcher,
        key_suffix="start-replay-0001",
        session_sitting_no=1,
        demo20=True,
    )
    configuration = _add_provider_probe(
        caregiver_visit_plan_clients.engine, monkeypatch, state="ready")
    body = {
        "idempotency_key": "caregiver-start-replay-000001",
        "expected_revision": approved["revision"],
    }
    first = caregiver_visit_plan_clients.owner.post(
        f"/caregiver/visit-plans/{created['plan_id']}/start", json=body)
    assert first.status_code == 200, first.text
    after_first = _visit_plan_write_counts(caregiver_visit_plan_clients.engine)

    monkeypatch.delenv("ALLOW_SIMULATION_DATA", raising=False)
    monkeypatch.delenv("ENABLE_AUTOPILOT_P0A_SIMULATION", raising=False)
    monkeypatch.setattr(
        provider_readiness,
        "capture_configuration",
        lambda **_kwargs: replace(configuration, fingerprint="f" * 64),
    )
    replay = caregiver_visit_plan_clients.owner.post(
        f"/caregiver/visit-plans/{created['plan_id']}/start", json=body)

    assert replay.status_code == 200, replay.text
    assert replay.json()["plan_id"] == first.json()["plan_id"]
    assert replay.json()["session"]["session_id"] == (
        first.json()["session"]["session_id"])
    assert replay.json()["session"]["operational_demo_ready"] is False
    assert _visit_plan_write_counts(caregiver_visit_plan_clients.engine) == (
        after_first)


def test_current_non_demo_session_can_close_safely_but_cannot_start_practice(
        caregiver_clients):
    start = caregiver_clients.owner.post(
        f"/sessions/{OWNER_SESSION}/autopilot/start",
        json={
            "idempotency_key": "non-demo-start-practice-0001",
            "expected_revision": 0,
        },
    )
    assert start.status_code == 409, start.text
    assert start.json()["detail"]["code"] == (
        "caregiver_session_not_operational_demo20")

    status = caregiver_clients.owner.get(
        f"/caregiver/sessions/{OWNER_SESSION}/status")
    assert status.status_code == 200, status.text
    assert status.json()["operational_demo_ready"] is False
    paused = caregiver_clients.owner.post(f"/sessions/{OWNER_SESSION}/pause")
    assert paused.status_code == 200, paused.text
    help_response = caregiver_clients.owner.post(
        f"/caregiver/sessions/{OWNER_SESSION}/help-requests",
        json={
            "reason_code": "other_staff_needed",
            "idempotency_key": "non-demo-help-request-0001",
        },
    )
    assert help_response.status_code == 200, help_response.text
    ended = caregiver_clients.owner.post(
        f"/sessions/{OWNER_SESSION}/abort",
        json={
            "reason_code": "technical_failure",
            "expected_revision": paused.json()["runtime_revision"],
            "idempotency_key": "non-demo-safe-end-000001",
        },
    )
    assert ended.status_code == 200, ended.text
    assert ended.json()["runtime_status"] == "aborted"


def test_caregiver_autopilot_provider_conflict_is_plain_and_redacted(
        caregiver_clients, monkeypatch):
    session_id = OWNER_SESSION
    _freeze_provider_configuration(monkeypatch)
    monkeypatch.setattr(
        autopilot_plan_profiles,
        "resolve_exact_runnable_demo20",
        lambda _session: object(),
    )
    monkeypatch.setattr(
        main_module,
        "_require_started_visit_plan_session",
        lambda *_args, **_kwargs: None,
    )

    response = caregiver_clients.owner.post(
        f"/sessions/{session_id}/autopilot/start",
        json={
            "idempotency_key": "caregiver-provider-start-practice",
            "expected_revision": 0,
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "provider_readiness_missing",
        "message": "语音服务未准备，请联系管理员",
    }
    assert "readiness" not in response.json()["detail"]


def test_caregiver_help_is_atomic_exact_replay_and_not_patient_pause(
        caregiver_clients):
    owner = caregiver_clients.owner
    assert owner.put(
        f"/caregiver/sessions/{OWNER_SESSION}/activation").status_code == 200
    body = {
        "reason_code": "clinical_concern",
        "idempotency_key": "caregiver-help-0000000001",
    }
    first = owner.post(
        f"/caregiver/sessions/{OWNER_SESSION}/help-requests", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["runtime_status"] == "paused"
    assert first.json()["runtime_revision"] == 1
    assert first.json()["idempotent"] is False

    replay = owner.post(
        f"/caregiver/sessions/{OWNER_SESSION}/help-requests", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == {**first.json(), "idempotent": True}

    conflict = owner.post(
        f"/caregiver/sessions/{OWNER_SESSION}/help-requests",
        json={**body, "reason_code": "technical_failure"},
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == (
        "caregiver_help_idempotency_conflict")

    with Session(caregiver_clients.engine) as session:
        runtime = session.get(SessionRuntimeState, OWNER_SESSION)
        rows = list(session.exec(select(CaregiverHelpRequest)))
        assert runtime is not None
        assert (runtime.status, runtime.revision) == ("paused", 1)
        assert len(rows) == 1
        assert rows[0].actor_id == OWNER_ACTOR
        assert rows[0].idempotency_key_sha256 != body["idempotency_key"]

    # 老人端安全暂停始终只接受已配对设备 capability；照护账号不是代理人。
    patient_pause = owner.post(
        f"/sessions/{OWNER_SESSION}/patient-pause",
        json={"idempotency_key": "patient-pause-0000000001"},
    )
    assert patient_pause.status_code == 403
    assert patient_pause.json()["code"] == "role_forbidden"


def test_caregiver_can_pause_but_receives_no_runtime_cursor(caregiver_clients):
    response = caregiver_clients.owner.post(
        f"/sessions/{OWNER_SESSION}/pause")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "session_id": OWNER_SESSION,
        "runtime_status": "paused",
        "runtime_revision": 1,
        "end_reason": None,
    }


def _raise_help(client, key: str = "caregiver-help-disp-000001") -> str:
    assert client.put(
        f"/caregiver/sessions/{OWNER_SESSION}/activation").status_code == 200
    response = client.post(
        f"/caregiver/sessions/{OWNER_SESSION}/help-requests",
        json={"reason_code": "other_staff_needed", "idempotency_key": key})
    assert response.status_code == 200, response.text
    return response.json()["request_id"]


def test_help_status_says_no_channel_is_configured_instead_of_awaiting_delivery(
        caregiver_clients, monkeypatch):
    monkeypatch.delenv(
        caregiver_service.HELP_NOTIFY_CHANNEL_ENV, raising=False)
    request_id = _raise_help(caregiver_clients.owner)

    response = caregiver_clients.owner.get(
        f"/caregiver/sessions/{OWNER_SESSION}/help-requests/{request_id}")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "request_id": request_id,
        "state": "recorded",
        "states": ["recorded", "delivered", "acknowledged", "resolved"],
        "notify_channel_configured": False,
        "delivery_reachable": False,
        "reached": [],
    }


def test_nobody_can_claim_delivery_over_http_even_with_a_channel_configured(
        caregiver_clients, monkeypatch):
    monkeypatch.setenv(
        caregiver_service.HELP_NOTIFY_CHANNEL_ENV, "station-3")
    request_id = _raise_help(caregiver_clients.owner)

    response = caregiver_clients.owner.post(
        f"/caregiver/sessions/{OWNER_SESSION}/help-requests/{request_id}"
        "/dispositions",
        json={"state": "delivered", "note": "我说它送到了"})

    # 契约层就把 delivered 挡在外面：它不是人能声称的状态，422 而不是 409。
    assert response.status_code == 422, response.text
    with Session(caregiver_clients.engine) as session:
        assert list(session.exec(select(CaregiverHelpDisposition))) == []


def test_someone_walking_over_can_be_recorded_without_any_channel(
        caregiver_clients, monkeypatch):
    monkeypatch.delenv(
        caregiver_service.HELP_NOTIFY_CHANNEL_ENV, raising=False)
    request_id = _raise_help(caregiver_clients.owner)
    url = (f"/caregiver/sessions/{OWNER_SESSION}/help-requests/{request_id}"
           "/dispositions")

    acknowledged = caregiver_clients.owner.post(
        url, json={"state": "acknowledged", "note": "护士长过来了"})
    assert acknowledged.status_code == 201, acknowledged.text
    assert acknowledged.json()["state"] == "acknowledged"
    assert acknowledged.json()["delivery_reachable"] is False

    resolved = caregiver_clients.owner.post(
        url, json={"state": "resolved", "note": "已扶回床上，训练改期"})
    assert resolved.status_code == 201, resolved.text
    assert resolved.json()["state"] == "resolved"
    assert [entry["state"] for entry in resolved.json()["reached"]] == [
        "acknowledged", "resolved"]
    assert all(entry["actor_id"] == OWNER_ACTOR
               for entry in resolved.json()["reached"])

    replay = caregiver_clients.owner.post(
        url, json={"state": "resolved", "note": "再点一次"})
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["code"] == "help_state_already_reached"


def test_help_disposition_note_never_reaches_the_database_or_the_response(
        caregiver_clients):
    request_id = _raise_help(caregiver_clients.owner)
    note = "王护士 13800000000 来处理"

    response = caregiver_clients.owner.post(
        f"/caregiver/sessions/{OWNER_SESSION}/help-requests/{request_id}"
        "/dispositions",
        json={"state": "acknowledged", "note": note})

    assert response.status_code == 201, response.text
    assert note not in response.text
    assert "13800000000" not in response.text
    with Session(caregiver_clients.engine) as session:
        rows = list(session.exec(select(CaregiverHelpDisposition)))
        assert len(rows) == 1
        serialized = repr(rows[0].model_dump())
        assert note not in serialized
        assert "13800000000" not in serialized


def test_a_foreign_caregiver_can_neither_read_nor_dispose_of_someone_elses_help(
        caregiver_clients):
    request_id = _raise_help(caregiver_clients.owner)
    base = f"/caregiver/sessions/{OWNER_SESSION}/help-requests/{request_id}"

    assert caregiver_clients.other.get(base).status_code == 404
    disposed = caregiver_clients.other.post(
        f"{base}/dispositions",
        json={"state": "acknowledged", "note": "我来处理"})
    assert disposed.status_code == 404, disposed.text
    with Session(caregiver_clients.engine) as session:
        assert list(session.exec(select(CaregiverHelpDisposition))) == []


def test_a_help_request_id_from_another_session_is_not_an_existence_oracle(
        caregiver_clients):
    request_id = _raise_help(caregiver_clients.owner)

    response = caregiver_clients.owner.get(
        f"/caregiver/sessions/{FOREIGN_SESSION}/help-requests/{request_id}")

    # 场次不是本人的 → 先在场次这一层 404，不泄露"这个呼叫号存在"。
    assert response.status_code == 404, response.text


def test_a_foreign_help_request_id_under_my_own_session_is_still_404(
        caregiver_clients):
    """真正走到"呼叫号属于别的场次"那条分支。

    上一条测试到不了它——场次判定先 404 了。这里让调用者拿着**自己拥有的**
    场次去套一个**别人场次**的呼叫号：场次这一关过得去，只剩呼叫归属这一关。
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    foreign_request_id = "CHR-FOREIGN-PROBE"
    with Session(caregiver_clients.engine) as session:
        session.add(CaregiverHelpRequest(
            request_id=foreign_request_id,
            session_id=FOREIGN_SESSION,
            actor_id=FOREIGN_ACTOR,
            reason_code="other_staff_needed",
            idempotency_key_sha256="a" * 64,
            request_hash="b" * 64,
            runtime_revision=0,
            created_at=now,
        ))
        session.commit()

    read = caregiver_clients.owner.get(
        f"/caregiver/sessions/{OWNER_SESSION}/help-requests/"
        f"{foreign_request_id}")
    assert read.status_code == 404, read.text

    disposed = caregiver_clients.owner.post(
        f"/caregiver/sessions/{OWNER_SESSION}/help-requests/"
        f"{foreign_request_id}/dispositions",
        json={"state": "acknowledged", "note": "我来处理"})
    assert disposed.status_code == 404, disposed.text
    with Session(caregiver_clients.engine) as session:
        assert list(session.exec(select(CaregiverHelpDisposition))) == []


def test_caregiver_finish_insufficient_evidence_has_no_research_diagnostics(
        caregiver_clients):
    response = caregiver_clients.owner.post(
        f"/sessions/{OWNER_SESSION}/finish-intervention")
    assert response.status_code == 409, response.text
    assert response.json() == {
        "detail": {
            "code": "caregiver_finish_not_ready",
            "message": "系统还没有确认本次已按计划做完；请根据现场情况选择其他结束原因，或联系负责人",
        },
    }
    serialized = response.text.lower()
    for forbidden in (
        "assessment", "expected_turns", "matched_turns", "item_id",
        "turn_seq", "response_role", "issue", "detail",
    ):
        if forbidden == "detail":
            continue  # FastAPI's standard envelope is named detail.
        assert forbidden not in serialized

    with Session(caregiver_clients.engine) as session:
        state = session.get(SessionRuntimeState, OWNER_SESSION)
        assert state is not None
        assert (state.status, state.revision) == ("active", 0)


@pytest.mark.parametrize(("method", "path", "body"), [
    ("GET", "/patients", None),
    ("GET", "/content/item-bank-bundle", None),
    ("GET", f"/sessions/{OWNER_SESSION}/runtime", None),
    ("GET", f"/sessions/{OWNER_SESSION}/plan", None),
    ("GET", f"/sessions/{OWNER_SESSION}/autopilot/status", None),
    ("GET", "/live/console-state", None),
    ("GET", "/audit", None),
    ("GET", "/exports/EXP-UNKNOWN", None),
    ("POST", f"/sessions/{OWNER_SESSION}/resume", None),
    ("POST", f"/sessions/{OWNER_SESSION}/complete", None),
    ("PUT", f"/sessions/{OWNER_SESSION}/closeout", {}),
    ("PUT", f"/sessions/{OWNER_SESSION}/runtime/cursor", {}),
    ("POST", f"/sessions/{OWNER_SESSION}/items", {}),
    ("POST", f"/sessions/{OWNER_SESSION}/export", None),
    ("POST", "/score/single", {}),
    ("POST", "/audio/UNKNOWN/checksum", None),
])
def test_caregiver_cannot_enter_research_content_score_audio_or_export_surfaces(
        caregiver_clients, method, path, body):
    response = caregiver_clients.owner.request(method, path, json=body)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "role_forbidden"


@pytest.mark.parametrize(("method", "path", "body"), [
    ("GET", f"/caregiver/sessions/{FOREIGN_SESSION}/status", None),
    ("PUT", f"/caregiver/sessions/{FOREIGN_SESSION}/activation", None),
    ("POST", f"/caregiver/sessions/{FOREIGN_SESSION}/help-requests", {
        "reason_code": "other_staff_needed",
        "idempotency_key": "foreign-help-000000000001",
    }),
    ("POST", f"/sessions/{FOREIGN_SESSION}/pause", None),
    ("POST", f"/sessions/{FOREIGN_SESSION}/finish-intervention", None),
    ("POST", f"/sessions/{FOREIGN_SESSION}/autopilot/start", {
        "idempotency_key": "foreign-autopilot-start-0001",
        "expected_revision": 0,
    }),
    ("POST", f"/sessions/{FOREIGN_SESSION}/autopilot/takeover", {
        "idempotency_key": "foreign-autopilot-takeover-01",
        "expected_revision": 0,
    }),
    # Owner concealment precedes the caregiver-specific reason restriction.
    ("POST", f"/sessions/{FOREIGN_SESSION}/abort", {
        "reason_code": "researcher_decision",
        "expected_revision": 0,
        "idempotency_key": "foreign-abort-000000000001",
    }),
])
def test_foreign_caregiver_session_access_is_uniform_404(
        caregiver_clients, method, path, body):
    response = caregiver_clients.owner.request(method, path, json=body)
    assert response.status_code == 404, response.text


def test_caregiver_cannot_use_researcher_decision_abort_reason(caregiver_clients):
    response = caregiver_clients.owner.post(
        f"/sessions/{OWNER_SESSION}/abort",
        json={
            "reason_code": "researcher_decision",
            "expected_revision": 0,
            "idempotency_key": "own-abort-reason-0000000001",
        },
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == (
        "caregiver_abort_reason_forbidden")


def test_caregiver_start_adapter_assigns_new_session_to_exact_actor(
        caregiver_clients, monkeypatch):
    now = datetime.now()

    monkeypatch.setattr(
        main_module.visit_plan_service,
        "patient_id_for_plan_fence",
        lambda _db, _plan_id: PATIENT_ID,
    )

    def fake_start(
        db_session,
        *,
        plan_id,
        body,
        actor_id,
        require_caregiver_operational_demo20,
    ):
        assert actor_id == OWNER_ACTOR
        assert require_caregiver_operational_demo20 is True
        session_id = "S-CAREGIVER-STARTED"
        db_session.add(TrainSession(
            session_id=session_id,
            patient_id=PATIENT_ID,
            training_date=date.today(),
            week_no=2,
            phase_type="正式训练",
            event_line="正式训练",
            trainer_id=actor_id,
            item_bank_version_id="wk2-v1-20260707",
            autopilot_profile_version_id=DEMO_VERSION,
            autopilot_profile_definition_digest=DEMO_DIGEST,
            is_simulation=True,
            data_classification="simulation",
            visit_plan_id=plan_id,
        ))
        db_session.add(SessionRuntimeState(
            session_id=session_id, status="active", revision=0, updated_at=now))
        db_session.flush()
        return visit_plan_contract.VisitPlanReceipt(
            plan_id=plan_id,
            patient_id=PATIENT_ID,
            scheduled_date=date.today(),
            scheduled_time=None,
            queue_order=0,
            session_sitting_no=1,
            week_no=2,
            phase_type="正式训练",
            event_line="正式训练",
            item_bank_version_id="wk2-v1-20260707",
            autopilot_profile_version_id=DEMO_VERSION,
            is_simulation=True,
            data_classification="simulation",
            status="started",
            revision=3,
            created_by="ACTOR-researcher",
            created_at=now,
            approved_by="ACTOR-researcher",
            approved_at=now,
            started_by=actor_id,
            started_at=now,
            cancelled_by=None,
            cancelled_at=None,
            session_id=session_id,
        )

    monkeypatch.setattr(
        main_module.visit_plan_service, "start_plan", fake_start)
    response = caregiver_clients.owner.post(
        "/caregiver/visit-plans/VP-CAREGIVER-READY/start",
        json={
            "idempotency_key": "caregiver-start-00000001",
            "expected_revision": 2,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["session"]["session_id"] == "S-CAREGIVER-STARTED"
    assert response.json()["session"]["runtime_status"] == "active"
    assert response.json()["session"]["operational_demo_ready"] is True
    with Session(caregiver_clients.engine) as session:
        row = session.get(TrainSession, "S-CAREGIVER-STARTED")
        assert row is not None and row.trainer_id == OWNER_ACTOR
