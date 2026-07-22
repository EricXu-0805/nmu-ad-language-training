"""VisitPlan acceptance contract: prepare, approve, queue, and atomic start.

These tests intentionally exercise only account-facing HTTP routes.  Bedside
devices never receive draft scheduling metadata, and callers never choose the
opaque plan/session identifiers or research-classification facts.
"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app import auth, content, db, visit_plan_service
from app.main import app
from app.models import (
    AssessmentEvent,
    AuditLog,
    Patient,
    ResearchUser,
    Session as TrainSession,
    SessionCloseoutReport,
    SessionRuntimeState,
    VisitPlan,
    VisitPlanCommand,
)


BANK = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
PROTOCOL = content.load_autopilot_protocol(
    content.CONTENT_DIR / "autopilot_protocol_v1.json")
TODAY = date.today()
RECEIPT_KEYS = {
    "plan_id",
    "patient_id",
    "scheduled_date",
    "scheduled_time",
    "queue_order",
    "session_sitting_no",
    "week_no",
    "phase_type",
    "event_line",
    "item_bank_version_id",
    "is_simulation",
    "data_classification",
    "status",
    "revision",
    "created_by",
    "created_at",
    "approved_by",
    "approved_at",
    "started_by",
    "started_at",
    "cancelled_by",
    "cancelled_at",
    "session_id",
}
STARTED_SESSION_KEYS = {
    "session_id",
    "patient_id",
    "visit_plan_id",
    "session_sitting_no",
    "training_date",
    "week_no",
    "phase_type",
    "event_line",
    "trainer_id",
    "item_bank_version_id",
    "item_bank_definition_digest",
    "autopilot_protocol_version_id",
    "autopilot_protocol_definition_digest",
    "is_simulation",
    "data_classification",
}


@dataclass
class VisitClients:
    researcher: TestClient
    researcher_b: TestClient
    admin: TestClient
    steward: TestClient
    anonymous: TestClient
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


@pytest.fixture
def visit_clients(monkeypatch, tmp_path) -> VisitClients:
    # File-backed SQLite gives concurrent HTTP requests independent connections;
    # StaticPool shares one DB-API cursor and can fabricate driver-level races.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'visit-plans.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setenv("ALLOW_SIMULATION_DATA", "1")
    SQLModel.metadata.create_all(engine)
    password_hash = auth.hash_password("password1")
    with Session(engine) as session:
        for username, role in (
            ("visit-researcher", "researcher"),
            ("visit-researcher-b", "researcher"),
            ("visit-admin", "admin"),
            ("visit-steward", "data_steward"),
        ):
            session.add(ResearchUser(
                username=username,
                display_id=f"ACTOR-{username}",
                password_hash=password_hash,
                role=role,
                created_at=datetime.now(),
            ))
        for index in range(1, 13):
            session.add(Patient(
                patient_id=f"P-VISIT-{index:02d}",
                is_simulation_subject=True,
                consent_status="已同意",
                consent_type="本人同意",
                mandarin_eligible=True,
                recording_allowed=True,
                secondary_use_allowed=True,
            ))
        session.commit()

    monkeypatch.setenv("REQUIRE_AUTH", "1")
    clients = VisitClients(
        researcher=_login("visit-researcher"),
        researcher_b=_login("visit-researcher-b"),
        admin=_login("visit-admin"),
        steward=_login("visit-steward"),
        anonymous=TestClient(app),
        engine=engine,
    )
    try:
        yield clients
    finally:
        clients.researcher.close()
        clients.researcher_b.close()
        clients.admin.close()
        clients.steward.close()
        clients.anonymous.close()


def _plan_body(
        patient_id: str,
        key: str,
        *,
        scheduled_date: date = TODAY,
        scheduled_time: str | None = "09:30:00",
        queue_order: int | None = 1,
        session_sitting_no: int = 1,
        **overrides,
) -> dict:
    body = {
        "patient_id": patient_id,
        "scheduled_date": scheduled_date.isoformat(),
        "scheduled_time": scheduled_time,
        "queue_order": queue_order,
        "session_sitting_no": session_sitting_no,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "idempotency_key": key,
    }
    body.update(overrides)
    return body


def _create(
        client: TestClient,
        patient_id: str,
        key: str,
        **overrides,
) -> dict:
    response = client.post(
        "/visit-plans",
        json=_plan_body(patient_id, key, **overrides),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _command(
        client: TestClient,
        plan_id: str,
        action: str,
        *,
        key: str,
        expected_revision: int,
        reason_code: str | None = None,
):
    body = {
        "idempotency_key": key,
        "expected_revision": expected_revision,
    }
    if action == "cancel":
        body["reason_code"] = reason_code
    return client.post(f"/visit-plans/{plan_id}/{action}", json=body)


def test_started_plan_list_conceals_session_binding_from_non_owner(
        visit_clients: VisitClients):
    """The shared pre-session queue must stop being a session ownership index."""
    created = _create(
        visit_clients.researcher,
        "P-VISIT-01",
        "create-owner-projection-0001",
    )
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="approve-owner-projection-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()
    started_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="start-owner-projection-0001",
        expected_revision=approved["revision"],
    )
    assert started_response.status_code == 200, started_response.text
    started = started_response.json()
    assert started["session_id"]
    assert started["started_by"] == "ACTOR-visit-researcher"

    queued = _create(
        visit_clients.researcher,
        "P-VISIT-02",
        "create-shared-queue-0001",
    )
    queued_response = _command(
        visit_clients.researcher,
        queued["plan_id"],
        "approve",
        key="approve-shared-queue-0001",
        expected_revision=queued["revision"],
    )
    assert queued_response.status_code == 200, queued_response.text

    owner_rows = visit_clients.researcher.get(
        "/visit-plans", params={"patient_id": "P-VISIT-01"})
    assert owner_rows.status_code == 200, owner_rows.text
    assert owner_rows.json()[0]["session_id"] == started["session_id"]
    assert owner_rows.json()[0]["started_by"] == "ACTOR-visit-researcher"

    for non_owner in (visit_clients.researcher_b, visit_clients.steward):
        hidden = non_owner.get(
            "/visit-plans", params={"patient_id": "P-VISIT-01"})
        assert hidden.status_code == 200, hidden.text
        receipt = hidden.json()[0]
        assert receipt["status"] == "started"
        assert receipt["session_id"] is None
        assert receipt["started_by"] is None
        assert receipt["started_at"] == started["started_at"]
        assert non_owner.get(
            f"/sessions/{started['session_id']}").status_code != 200

        shared = non_owner.get(
            "/visit-plans", params={"patient_id": "P-VISIT-02"})
        assert shared.status_code == 200, shared.text
        assert shared.json()[0]["status"] == "approved"
        assert shared.json()[0]["plan_id"] == queued["plan_id"]

    admin_rows = visit_clients.admin.get(
        "/visit-plans", params={"patient_id": "P-VISIT-01"})
    assert admin_rows.status_code == 200, admin_rows.text
    assert admin_rows.json()[0]["session_id"] == started["session_id"]
    assert admin_rows.json()[0]["started_by"] == "ACTOR-visit-researcher"

    # Today's operational queue is pre-start only.  Prove the started binding
    # is absent rather than relying on clients to hide fields after receipt.
    today = visit_clients.researcher_b.get("/visit-plans/today")
    assert today.status_code == 200, today.text
    today_rows = today.json()["plans"]
    assert queued["plan_id"] in {row["plan_id"] for row in today_rows}
    assert started["plan_id"] not in {row["plan_id"] for row in today_rows}
    assert all(row["status"] == "approved" for row in today_rows)
    assert all(row["session_id"] is None for row in today_rows)
    assert all(row["started_by"] is None for row in today_rows)


@pytest.mark.parametrize(("patient_id", "overrides", "reason"), [
    (
        "P-VISIT-08",
        {"week_no": 1, "phase_type": "关系建立", "event_line": "关系建立环节"},
        "独立完成合同",
    ),
    (
        "P-VISIT-09",
        {"week_no": 1, "phase_type": "前测", "event_line": "基线测评窗"},
        "前测",
    ),
    (
        "P-VISIT-10",
        {"week_no": 3, "phase_type": "正式训练", "event_line": "正式训练"},
        "冻结训练计划",
    ),
])
def test_unsupported_protocol_may_be_drafted_but_cannot_be_approved(
        visit_clients: VisitClients, patient_id: str,
        overrides: dict, reason: str):
    created = _create(
        visit_clients.researcher,
        patient_id,
        f"create-unsupported-{patient_id}",
        **overrides,
    )

    denied = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key=f"approve-unsupported-{patient_id}",
        expected_revision=created["revision"],
    )

    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "visit_plan_protocol_unavailable"
    assert reason in denied.json()["detail"]["message"]
    with Session(visit_clients.engine) as session:
        plan = session.get(VisitPlan, created["plan_id"])
        assert plan is not None
        assert plan.status == "draft"
        assert plan.revision == 1
        assert _linked_session_for_test(session, created["plan_id"]) is None


def _linked_session_for_test(session: Session, plan_id: str) -> TrainSession | None:
    return session.exec(select(TrainSession).where(
        TrainSession.visit_plan_id == plan_id,
    )).first()


def test_start_revalidates_protocol_even_for_a_legacy_approved_plan(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-11",
        "create-legacy-approved-week3",
        week_no=3,
        phase_type="正式训练",
        event_line="正式训练",
    )
    with Session(visit_clients.engine) as session:
        plan = session.get(VisitPlan, created["plan_id"])
        assert plan is not None
        plan.status = "approved"
        plan.revision = 2
        plan.approved_by = "LEGACY-IMPORT"
        plan.approved_at = datetime.now()
        session.add(plan)
        session.commit()

    # The bedside queue must apply the same current protocol gate as start;
    # imported/stale approved rows are governance facts, not startable work.
    queue = visit_clients.researcher.get("/visit-plans/today")
    assert queue.status_code == 200, queue.text
    assert created["plan_id"] not in {
        row["plan_id"] for row in queue.json()["plans"]
    }

    denied = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="start-legacy-approved-week3",
        expected_revision=2,
    )

    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "visit_plan_protocol_unavailable"
    with Session(visit_clients.engine) as session:
        assert _linked_session_for_test(session, created["plan_id"]) is None


def test_week_two_real_research_still_fails_closed_on_content_readiness(
        visit_clients: VisitClients):
    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-12")
        assert patient is not None
        patient.is_simulation_subject = False
        patient.proxy_consent = False
        patient.assent_obtained = False
        session.add(patient)
        session.commit()

    created = _create(
        visit_clients.researcher,
        "P-VISIT-12",
        "create-week2-real-research",
    )
    assert created["is_simulation"] is False
    assert created["data_classification"] == "research"

    denied = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="approve-week2-real-research",
        expected_revision=created["revision"],
    )

    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "visit_plan_content_unavailable"
    assert "真实研究冻结/质控门禁" in denied.json()["detail"]["message"]


def test_direct_session_creation_cannot_bypass_approved_plan(
        visit_clients: VisitClients, monkeypatch):
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    denied = visit_clients.researcher.post("/sessions", json={
        "session_id": "S-DIRECT-BYPASS",
        "patient_id": "P-VISIT-01",
        "session_sitting_no": 1,
        "training_date": TODAY.isoformat(),
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "item_bank_version_id": BANK.version_id,
        "is_simulation": True,
    })
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "direct_session_creation_disabled"
    with Session(visit_clients.engine) as session:
        assert session.get(TrainSession, "S-DIRECT-BYPASS") is None


def _orphan_session(session_id: str, *, patient_id: str,
                    visit_plan_id: str | None) -> TrainSession:
    return TrainSession(
        session_id=session_id,
        patient_id=patient_id,
        visit_plan_id=visit_plan_id,
        session_sitting_no=1,
        training_date=TODAY,
        week_no=2,
        phase_type="正式训练",
        event_line="正式训练",
        item_bank_version_id=BANK.version_id,
        is_simulation=True,
        data_classification="simulation",
        # Historical evidence remains readable only to its explicit owner; an
        # orphaned VisitPlan link is not an ownership bypass.
        trainer_id="ACTOR-visit-researcher",
    )


def _live_handshake(session_id: str) -> dict:
    return {
        "kind": "session",
        "payload": {
            "sessionId": session_id,
            "weekNo": 2,
            "eventLine": "正式训练",
            "mode": "task",
            "itemBankVersionId": BANK.version_id,
        },
    }


def _assert_visit_plan_admission_rejected(response) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == (
        "session_visit_plan_admission_required")


def test_historical_orphan_sessions_are_read_only_and_cannot_reenter_runtime(
        visit_clients: VisitClients, monkeypatch):
    """NULL, missing and non-started plan links are all isolated, never grandfathered."""
    now = datetime.now()
    with Session(visit_clients.engine) as session:
        nonstarted = VisitPlan(
            plan_id="VP-LEGACY-NOT-STARTED",
            protocol_slot_key="P-VISIT-03:2:1",
            patient_id="P-VISIT-03",
            scheduled_date=TODAY,
            scheduled_time=time(11, 0),
            queue_order=11,
            session_sitting_no=1,
            week_no=2,
            phase_type="正式训练",
            event_line="正式训练",
            item_bank_version_id=BANK.version_id,
            is_simulation=True,
            data_classification="simulation",
            status="approved",
            revision=2,
            created_by="ACTOR-legacy-import",
            created_at=now,
            updated_at=now,
            approved_by="ACTOR-legacy-import",
            approved_at=now,
        )
        session.add(nonstarted)
        session.add(_orphan_session(
            "S-LEGACY-NULL", patient_id="P-VISIT-01", visit_plan_id=None))
        session.add(_orphan_session(
            "S-LEGACY-MISSING", patient_id="P-VISIT-02",
            visit_plan_id="VP-DOES-NOT-EXIST"))
        session.add(_orphan_session(
            "S-LEGACY-NOT-STARTED", patient_id="P-VISIT-03",
            visit_plan_id=nonstarted.plan_id))
        session.commit()

    # Disable the explicit pytest-only fixture escape to exercise deployment rules.
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    for session_id in (
            "S-LEGACY-NULL", "S-LEGACY-MISSING", "S-LEGACY-NOT-STARTED"):
        _assert_visit_plan_admission_rejected(
            visit_clients.researcher.put(
                "/live/state", json=_live_handshake(session_id)))
        _assert_visit_plan_admission_rejected(
            visit_clients.researcher.get(f"/sessions/{session_id}/runtime"))
        _assert_visit_plan_admission_rejected(
            visit_clients.researcher.get(f"/sessions/{session_id}/plan"))
        _assert_visit_plan_admission_rejected(
            visit_clients.researcher.get(
                f"/sessions/{session_id}/autopilot/status"))

    # Runtime writes and microphone admission are fenced by the same boundary.
    _assert_visit_plan_admission_rejected(visit_clients.researcher.put(
        "/sessions/S-LEGACY-NULL/runtime/cursor",
        json={
            "screen": "present", "itemIdx": 0, "turnIdx": 0,
            "responseRole": "命名", "recording": "idle",
        },
    ))
    _assert_visit_plan_admission_rejected(visit_clients.researcher.post(
        "/sessions/S-LEGACY-NULL/recording-authorization"))

    # Isolation is not deletion: account research review can still enumerate and
    # inspect the legacy facts without reopening a live/control channel.
    assert visit_clients.researcher.get(
        "/sessions/S-LEGACY-NULL").status_code == 200
    listed = visit_clients.researcher.get(
        "/patients/P-VISIT-01/sessions")
    assert listed.status_code == 200, listed.text
    assert {row["session_id"] for row in listed.json()} == {"S-LEGACY-NULL"}
    assert visit_clients.researcher.get(
        "/sessions/S-LEGACY-NULL/journal").status_code == 200


def test_started_visit_plan_session_remains_admitted_to_live_runtime(
        visit_clients: VisitClients, monkeypatch):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-04",
        "visit-create-admission-mainline-0001",
    )
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-admission-mainline-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()
    started_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-start-admission-mainline-0001",
        expected_revision=approved["revision"],
    )
    assert started_response.status_code == 200, started_response.text
    started = started_response.json()

    # Starting returns a plan transition receipt.  The browser then recovers
    # the server-created Session through this separate HTTP contract before it
    # may enter bedside runtime.  Keep the complete, exact payload explicit so
    # a strict cross-client decoder cannot silently drift from the API model.
    session_response = visit_clients.researcher.get(
        f"/sessions/{started['session_id']}")
    assert session_response.status_code == 200, session_response.text
    session_payload = session_response.json()
    assert set(session_payload) == STARTED_SESSION_KEYS
    assert session_payload == {
        "session_id": started["session_id"],
        "patient_id": "P-VISIT-04",
        "visit_plan_id": created["plan_id"],
        "session_sitting_no": created["session_sitting_no"],
        "training_date": TODAY.isoformat(),
        "week_no": created["week_no"],
        "phase_type": created["phase_type"],
        "event_line": created["event_line"],
        "trainer_id": started["started_by"],
        "item_bank_version_id": BANK.version_id,
        "item_bank_definition_digest": content.item_bank_definition_digest(BANK),
        "autopilot_protocol_version_id": PROTOCOL["protocol_version_id"],
        "autopilot_protocol_definition_digest": (
            content.autopilot_protocol_definition_digest(PROTOCOL)),
        "is_simulation": True,
        "data_classification": "simulation",
    }

    # The real VisitPlan vertical must pass even with all direct-session test
    # escapes disabled.
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    handshake = visit_clients.researcher.put(
        "/live/state", json=_live_handshake(started["session_id"]))
    assert handshake.status_code == 200, handshake.text
    runtime_response = visit_clients.researcher.get(
        f"/sessions/{started['session_id']}/runtime")
    assert runtime_response.status_code == 200, runtime_response.text
    assert runtime_response.json()["status"] == "active"


def _assert_iso_datetime(value: object) -> None:
    assert isinstance(value, str) and value
    datetime.fromisoformat(value.replace("Z", "+00:00"))


def _assert_receipt(
        payload: dict,
        *,
        status: str,
        revision: int,
        patient_id: str,
        approved_fact: bool | None = None,
) -> None:
    # Exact keys keep names, notes, free text, and other unnecessary identifiers
    # out of the scheduling/queue response contract.
    assert set(payload) == RECEIPT_KEYS
    assert payload["patient_id"] == patient_id
    assert payload["status"] == status
    assert payload["revision"] == revision
    assert isinstance(payload["plan_id"], str) and payload["plan_id"]
    assert payload["plan_id"] != patient_id
    assert payload["item_bank_version_id"] == BANK.version_id
    assert payload["is_simulation"] is True
    assert payload["data_classification"] == "simulation"
    assert payload["week_no"] == 2
    assert payload["phase_type"] == "正式训练"
    assert payload["event_line"] == "正式训练"
    assert payload["session_sitting_no"] >= 1
    assert payload["created_by"] in {
        "ACTOR-visit-researcher", "ACTOR-visit-admin"}
    _assert_iso_datetime(payload["created_at"])

    reached = {
        "approved": (
            status in {"approved", "started"}
            if approved_fact is None else approved_fact
        ),
        "started": status == "started",
        "cancelled": status == "cancelled",
    }
    for event, present in reached.items():
        actor = payload[f"{event}_by"]
        occurred_at = payload[f"{event}_at"]
        if present:
            assert actor in {"ACTOR-visit-researcher", "ACTOR-visit-admin"}
            _assert_iso_datetime(occurred_at)
        else:
            assert actor is None
            assert occurred_at is None
    if status == "started":
        assert isinstance(payload["session_id"], str) and payload["session_id"]
        assert payload["session_id"] != payload["plan_id"]
    else:
        assert payload["session_id"] is None


def test_create_derives_server_owned_fields_and_is_exactly_idempotent(
        visit_clients: VisitClients):
    body = _plan_body(
        "P-VISIT-01",
        "visit-create-contract-0001",
        scheduled_time="08:45:00",
        queue_order=7,
        session_sitting_no=2,
    )
    first = visit_clients.researcher.post("/visit-plans", json=body)
    assert first.status_code == 200, first.text
    payload = first.json()
    _assert_receipt(
        payload, status="draft", revision=1, patient_id="P-VISIT-01")
    assert payload["scheduled_date"] == TODAY.isoformat()
    assert time.fromisoformat(payload["scheduled_time"]) == time(8, 45)
    assert payload["queue_order"] == 7
    assert payload["session_sitting_no"] == 2

    replay = visit_clients.researcher.post("/visit-plans", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == payload

    conflict = visit_clients.researcher.post("/visit-plans", json={
        **body,
        "scheduled_time": "08:50:00",
    })
    assert conflict.status_code == 409, conflict.text


@pytest.mark.parametrize(("client_owned_field", "client_value"), [
    ("plan_id", "vp-client-controlled"),
    ("session_id", "S-client-controlled"),
    ("item_bank_version_id", BANK.version_id),
    ("is_simulation", True),
    ("data_classification", "simulation"),
    ("status", "draft"),
    ("revision", 99),
    ("created_by", "ACTOR-client-controlled"),
    ("patient_name", "client-controlled"),
    ("notes", "client-controlled"),
])
def test_create_forbids_client_owned_or_free_text_fields(
        visit_clients: VisitClients,
        client_owned_field: str,
        client_value: object):
    body = _plan_body(
        "P-VISIT-02",
        f"visit-extra-{client_owned_field.replace('_', '-')}-0001",
    )
    body[client_owned_field] = client_value
    denied = visit_clients.researcher.post("/visit-plans", json=body)
    assert denied.status_code == 422, denied.text


def test_approve_today_queue_and_start_form_one_atomic_idempotent_vertical(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-03",
        "visit-create-vertical-0001",
        scheduled_time="10:15:00",
        queue_order=3,
    )
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-vertical-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()
    _assert_receipt(
        approved, status="approved", revision=2, patient_id="P-VISIT-03")

    approve_replay = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-vertical-0001",
        expected_revision=created["revision"],
    )
    assert approve_replay.status_code == 200, approve_replay.text
    assert approve_replay.json() == approved

    today = visit_clients.researcher.get("/visit-plans/today")
    assert today.status_code == 200, today.text
    assert today.headers["cache-control"] == "private, no-store"
    assert today.headers["pragma"] == "no-cache"
    assert set(today.json()) == {"as_of_date", "plans"}
    assert today.json()["as_of_date"] == TODAY.isoformat()
    assert today.json()["plans"] == [approved]

    started_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-start-vertical-0001",
        expected_revision=approved["revision"],
    )
    assert started_response.status_code == 200, started_response.text
    started = started_response.json()
    _assert_receipt(
        started, status="started", revision=3, patient_id="P-VISIT-03")

    started_replay = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-start-vertical-0001",
        expected_revision=approved["revision"],
    )
    assert started_replay.status_code == 200, started_replay.text
    assert started_replay.json() == started

    with Session(visit_clients.engine) as session:
        sessions = list(session.exec(select(TrainSession)))
        runtimes = list(session.exec(select(SessionRuntimeState)))
        plan = session.get(VisitPlan, created["plan_id"])
        assert len(sessions) == 1
        assert len(runtimes) == 1
        assert plan is not None
        train_session = sessions[0]
        runtime = runtimes[0]
        assert train_session.session_id == started["session_id"]
        assert train_session.patient_id == "P-VISIT-03"
        assert train_session.training_date == TODAY
        assert train_session.session_sitting_no == 1
        assert train_session.item_bank_version_id == BANK.version_id
        assert plan.item_bank_definition_digest == (
            content.item_bank_definition_digest(BANK))
        assert plan.autopilot_protocol_version_id == (
            PROTOCOL["protocol_version_id"])
        assert plan.autopilot_protocol_definition_digest == (
            content.autopilot_protocol_definition_digest(PROTOCOL))
        assert train_session.item_bank_definition_digest == (
            plan.item_bank_definition_digest)
        assert train_session.autopilot_protocol_version_id == (
            plan.autopilot_protocol_version_id)
        assert train_session.autopilot_protocol_definition_digest == (
            plan.autopilot_protocol_definition_digest)
        assert train_session.is_simulation is True
        assert train_session.data_classification == "simulation"
        assert getattr(train_session, "visit_plan_id") == created["plan_id"]
        assert runtime.session_id == started["session_id"]
        assert (runtime.status, runtime.revision) == ("active", 0)

    after_start = visit_clients.researcher.get("/visit-plans/today")
    assert after_start.status_code == 200, after_start.text
    assert after_start.json()["plans"] == []


def test_abort_preserves_started_visit_slot_and_never_auto_reschedules(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-12",
        "create-abort-slot-preserved-0001",
    )
    approved = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="approve-abort-slot-preserved-0001",
        expected_revision=created["revision"],
    )
    assert approved.status_code == 200, approved.text
    started = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="start-abort-slot-preserved-0001",
        expected_revision=approved.json()["revision"],
    )
    assert started.status_code == 200, started.text
    receipt = started.json()

    with Session(visit_clients.engine) as session:
        before = session.get(VisitPlan, created["plan_id"])
        assert before is not None
        slot_key = before.protocol_slot_key
        linked = _linked_session_for_test(session, created["plan_id"])
        assert linked is not None
        linked_session_id = linked.session_id

    aborted = visit_clients.researcher.post(
        f"/sessions/{receipt['session_id']}/abort",
        json={
            "reason_code": "clinical_safety",
            "expected_revision": 0,
            "idempotency_key": "abort-visit-slot-clinical-safety-0001",
        },
    )
    assert aborted.status_code == 200, aborted.text
    assert aborted.json()["status"] == "aborted"

    with Session(visit_clients.engine) as session:
        after = session.get(VisitPlan, created["plan_id"])
        assert after is not None
        assert after.status == "started"
        assert after.protocol_slot_key == slot_key
        linked = _linked_session_for_test(session, created["plan_id"])
        assert linked is not None
        assert linked.session_id == linked_session_id == receipt["session_id"]

    replacement = visit_clients.researcher.post(
        "/visit-plans",
        json=_plan_body(
            "P-VISIT-12",
            "create-abort-slot-replacement-denied-0001",
        ),
    )
    assert replacement.status_code == 409, replacement.text


def test_concurrent_abort_cas_commits_one_reason_across_independent_sessions(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-11",
        "create-concurrent-abort-0001",
    )
    approved = _command(
        visit_clients.researcher, created["plan_id"], "approve",
        key="approve-concurrent-abort-0001",
        expected_revision=created["revision"],
    ).json()
    started_response = _command(
        visit_clients.researcher, created["plan_id"], "start",
        key="start-concurrent-abort-0001",
        expected_revision=approved["revision"],
    )
    assert started_response.status_code == 200, started_response.text
    session_id = started_response.json()["session_id"]

    peer = _login("visit-researcher")
    barrier = Barrier(2)

    def submit(client: TestClient, reason_code: str, key: str):
        barrier.wait()
        return client.post(f"/sessions/{session_id}/abort", json={
            "reason_code": reason_code,
            "expected_revision": 0,
            "idempotency_key": key,
        })

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    submit, visit_clients.researcher, "technical_failure",
                    "abort-concurrent-technical-failure-0001"),
                pool.submit(
                    submit, peer, "researcher_decision",
                    "abort-concurrent-researcher-decision-0002"),
            ]
            responses = [future.result(timeout=10) for future in futures]
    finally:
        peer.close()

    assert sorted(response.status_code for response in responses) == [200, 409]
    loser = next(response for response in responses if response.status_code == 409)
    assert loser.json()["detail"]["code"] == "session_already_aborted"
    winner = next(response for response in responses if response.status_code == 200)
    authoritative = visit_clients.researcher.get(
        f"/sessions/{session_id}/runtime")
    assert authoritative.status_code == 200
    assert authoritative.json()["endReason"] == winner.json()["endReason"]
    assert authoritative.json()["revision"] == 1

    with Session(visit_clients.engine) as session:
        audit_rows = list(session.exec(select(AuditLog).where(
            AuditLog.action == "session_abort",
            AuditLog.session_id == session_id,
        )))
    assert len(audit_rows) == 1


def test_researcher_cannot_switch_participant_before_safe_closeout(
        visit_clients: VisitClients):
    first = _create(
        visit_clients.researcher,
        "P-VISIT-04",
        "visit-switch-create-first-0001",
    )
    first_approved = _command(
        visit_clients.researcher, first["plan_id"], "approve",
        key="visit-switch-approve-first-0001",
        expected_revision=first["revision"],
    ).json()
    first_started = _command(
        visit_clients.researcher, first["plan_id"], "start",
        key="visit-switch-start-first-0001",
        expected_revision=first_approved["revision"],
    ).json()

    second = _create(
        visit_clients.researcher,
        "P-VISIT-05",
        "visit-switch-create-second-0001",
    )
    second_approved = _command(
        visit_clients.researcher, second["plan_id"], "approve",
        key="visit-switch-approve-second-0001",
        expected_revision=second["revision"],
    ).json()
    start_body = {
        "idempotency_key": "visit-switch-start-second-0001",
        "expected_revision": second_approved["revision"],
    }

    active_denied = visit_clients.researcher.post(
        f"/visit-plans/{second['plan_id']}/start", json=start_body)
    assert active_denied.status_code == 409
    assert active_denied.json()["detail"]["code"] == "visit_plan_actor_session_open"

    with Session(visit_clients.engine) as session:
        runtime = session.get(SessionRuntimeState, first_started["session_id"])
        runtime.status = "intervention_completed"
        runtime.revision += 1
        runtime.intervention_completed_at = datetime.now()
        session.add(runtime)
        session.commit()

    closeout_denied = visit_clients.researcher.post(
        f"/visit-plans/{second['plan_id']}/start", json=start_body)
    assert closeout_denied.status_code == 409
    assert closeout_denied.json()["detail"]["code"] == "visit_plan_actor_closeout_required"

    with Session(visit_clients.engine) as session:
        session.add(SessionCloseoutReport(
            session_id=first_started["session_id"],
            schema_version="session-closeout.v1",
            status="no_additional_observation",
            revision=1,
            last_idempotency_key="visit-switch-closeout-0001",
            last_request_hash="a" * 64,
            created_by="ACTOR-visit-researcher",
            updated_by="ACTOR-visit-researcher",
        ))
        session.commit()

    allowed = visit_clients.researcher.post(
        f"/visit-plans/{second['plan_id']}/start", json=start_body)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "started"


def test_different_actor_cannot_restart_patient_before_safe_closeout(
        visit_clients: VisitClients):
    first = _create(
        visit_clients.researcher,
        "P-VISIT-04",
        "visit-patient-closeout-create-first-0001",
        session_sitting_no=1,
    )
    first_approved = _command(
        visit_clients.researcher,
        first["plan_id"],
        "approve",
        key="visit-patient-closeout-approve-first-0001",
        expected_revision=first["revision"],
    ).json()
    first_started_response = _command(
        visit_clients.researcher,
        first["plan_id"],
        "start",
        key="visit-patient-closeout-start-first-0001",
        expected_revision=first_approved["revision"],
    )
    assert first_started_response.status_code == 200, first_started_response.text
    first_started = first_started_response.json()

    second = _create(
        visit_clients.researcher_b,
        "P-VISIT-04",
        "visit-patient-closeout-create-second-0001",
        session_sitting_no=2,
    )
    second_approved_response = _command(
        visit_clients.researcher_b,
        second["plan_id"],
        "approve",
        key="visit-patient-closeout-approve-second-0001",
        expected_revision=second["revision"],
    )
    assert second_approved_response.status_code == 200, second_approved_response.text
    second_approved = second_approved_response.json()

    with Session(visit_clients.engine) as session:
        runtime = session.get(
            SessionRuntimeState, first_started["session_id"])
        assert runtime is not None
        runtime.status = "intervention_completed"
        runtime.revision += 1
        runtime.intervention_completed_at = datetime.now()
        session.add(runtime)
        session.commit()

    start_body = {
        "idempotency_key": "visit-patient-closeout-start-second-0001",
        "expected_revision": second_approved["revision"],
    }
    denied = visit_clients.researcher_b.post(
        f"/visit-plans/{second['plan_id']}/start", json=start_body)
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == (
        "visit_plan_patient_closeout_required")

    with Session(visit_clients.engine) as session:
        session.add(SessionCloseoutReport(
            session_id=first_started["session_id"],
            schema_version="session-closeout.v1",
            status="no_additional_observation",
            revision=1,
            last_idempotency_key="visit-patient-closeout-report-0001",
            last_request_hash="c" * 64,
            created_by="ACTOR-visit-researcher",
            updated_by="ACTOR-visit-researcher",
        ))
        session.commit()

    allowed = visit_clients.researcher_b.post(
        f"/visit-plans/{second['plan_id']}/start", json=start_body)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "started"


def test_concurrent_start_for_same_actor_creates_only_one_active_session(
        visit_clients: VisitClients, monkeypatch):
    """Different plan rows cannot write-skew the actor active-work predicate.

    Disable the legacy process-wide VisitPlan lock to model requests arriving
    in distinct workers.  The subject/actor governance fence remains the
    authority and must make the second request observe the first active session.
    """
    plans: list[dict] = []
    for index, patient_id in enumerate(("P-VISIT-06", "P-VISIT-07"), 1):
        created = _create(
            visit_clients.researcher,
            patient_id,
            f"visit-actor-race-create-{index:04d}",
        )
        approved_response = _command(
            visit_clients.researcher,
            created["plan_id"],
            "approve",
            key=f"visit-actor-race-approve-{index:04d}",
            expected_revision=created["revision"],
        )
        assert approved_response.status_code == 200, approved_response.text
        plans.append(approved_response.json())

    import app.main as main_mod
    monkeypatch.setattr(main_mod, "_VISIT_PLAN_WRITE_LOCK", nullcontext())
    peer = _login("visit-researcher")
    barrier = Barrier(2)

    def start(client: TestClient, plan: dict, index: int):
        barrier.wait()
        return _command(
            client,
            plan["plan_id"],
            "start",
            key=f"visit-actor-race-start-{index:04d}",
            expected_revision=plan["revision"],
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(start, visit_clients.researcher, plans[0], 1),
                pool.submit(start, peer, plans[1], 2),
            ]
            responses = [future.result(timeout=10) for future in futures]
    finally:
        peer.close()

    assert sorted(response.status_code for response in responses) == [200, 409]
    loser = next(response for response in responses if response.status_code == 409)
    assert loser.json()["detail"]["code"] == "visit_plan_actor_session_open"
    with Session(visit_clients.engine) as session:
        sessions = list(session.exec(select(TrainSession)))
        assert len(sessions) == 1
        assert sessions[0].trainer_id == "ACTOR-visit-researcher"
        stored_plans = [session.get(VisitPlan, plan["plan_id"]) for plan in plans]
        assert sorted(plan.status for plan in stored_plans if plan is not None) == [
            "approved", "started"]


def test_training_start_rejects_actor_with_open_formal_assessment(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-09",
        "visit-assessment-gate-create-0001",
    )
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-assessment-gate-approve-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()
    with Session(visit_clients.engine) as session:
        session.add(AssessmentEvent(
            event_id="assessment-open-for-visit-gate",
            patient_id="P-VISIT-08",
            assigned_assessor_id="ACTOR-visit-researcher",
            timepoint="pretest",
            scheduled_date=TODAY,
            status="in_progress",
            revision=2,
            is_simulation=True,
            data_classification="simulation",
            formal_outcome_eligible=False,
            definition_bundle_id="assessment-bundle-test-v1",
            definition_bundle_digest="sha256:" + "a" * 64,
            active_protocol_slot_key="b" * 64,
            created_by="ACTOR-visit-researcher",
            started_at=datetime.now(),
        ))
        session.commit()

    denied = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-assessment-gate-start-0001",
        expected_revision=approved["revision"],
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == (
        "visit_plan_actor_assessment_open")
    with Session(visit_clients.engine) as session:
        assert _linked_session_for_test(session, created["plan_id"]) is None
        withdrawn_patient = session.get(Patient, "P-VISIT-08")
        assert withdrawn_patient is not None
        withdrawn_patient.withdrawal_status = "withdrawn"
        session.add(withdrawn_patient)
        session.commit()

    # Withdrawal freezes the historical in-progress event rather than forging
    # completion, but it releases the assessor's current-work slot.
    allowed = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-assessment-gate-start-0001",
        expected_revision=approved["revision"],
    )
    assert allowed.status_code == 200, allowed.text
    with Session(visit_clients.engine) as session:
        frozen = session.get(
            AssessmentEvent, "assessment-open-for-visit-gate")
        assert frozen is not None and frozen.status == "in_progress"


def test_training_start_rejects_patient_in_assessment_owned_by_another_actor(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-09",
        "visit-patient-assessment-gate-create-0001",
    )
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-patient-assessment-gate-approve-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()
    with Session(visit_clients.engine) as session:
        session.add(AssessmentEvent(
            event_id="assessment-open-for-patient-gate",
            patient_id="P-VISIT-09",
            assigned_assessor_id="ACTOR-another-researcher",
            timepoint="pretest",
            scheduled_date=TODAY - timedelta(days=1),
            status="awaiting_closeout",
            revision=4,
            is_simulation=True,
            data_classification="simulation",
            formal_outcome_eligible=False,
            definition_bundle_id="assessment-bundle-test-v1",
            definition_bundle_digest="sha256:" + "c" * 64,
            active_protocol_slot_key="d" * 64,
            created_by="ACTOR-another-researcher",
            started_at=datetime.now(),
        ))
        session.commit()

    denied = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-patient-assessment-gate-start-0001",
        expected_revision=approved["revision"],
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == (
        "visit_plan_patient_assessment_open")
    with Session(visit_clients.engine) as session:
        assert _linked_session_for_test(session, created["plan_id"]) is None


def test_consent_denied_assessment_releases_assessor_current_work_slot(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-10",
        "visit-consent-sealed-assessment-create-0001",
    )
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-consent-sealed-assessment-approve-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()
    with Session(visit_clients.engine) as session:
        sealed_patient = session.get(Patient, "P-VISIT-08")
        assert sealed_patient is not None
        sealed_patient.consent_status = "denied"
        session.add(sealed_patient)
        session.add(AssessmentEvent(
            event_id="assessment-sealed-consent-actor-slot",
            patient_id="P-VISIT-08",
            assigned_assessor_id="ACTOR-visit-researcher",
            timepoint="pretest",
            scheduled_date=TODAY,
            status="in_progress",
            revision=2,
            is_simulation=True,
            data_classification="simulation",
            formal_outcome_eligible=False,
            definition_bundle_id="assessment-bundle-test-v1",
            definition_bundle_digest="sha256:" + "e" * 64,
            active_protocol_slot_key="f" * 64,
            created_by="ACTOR-visit-researcher",
            started_at=datetime.now(),
        ))
        session.commit()

    allowed = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-consent-sealed-assessment-start-0001",
        expected_revision=approved["revision"],
    )
    assert allowed.status_code == 200, allowed.text


def test_revision_conflicts_cancel_replay_and_terminal_state_do_not_create_session(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-04",
        "visit-create-cancel-0001",
    )
    stale_approve = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-stale-0001",
        expected_revision=0,
    )
    assert stale_approve.status_code == 409, stale_approve.text

    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-cancel-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()

    duplicate_transition = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-second-0001",
        expected_revision=created["revision"],
    )
    assert duplicate_transition.status_code == 409, duplicate_transition.text

    stale_start = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-start-stale-0001",
        expected_revision=created["revision"],
    )
    assert stale_start.status_code == 409, stale_start.text

    cancelled_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "cancel",
        key="visit-cancel-contract-0001",
        expected_revision=approved["revision"],
        reason_code="schedule_changed",
    )
    assert cancelled_response.status_code == 200, cancelled_response.text
    cancelled = cancelled_response.json()
    _assert_receipt(
        cancelled,
        status="cancelled",
        revision=3,
        patient_id="P-VISIT-04",
        approved_fact=True,
    )

    cancel_replay = _command(
        visit_clients.researcher,
        created["plan_id"],
        "cancel",
        key="visit-cancel-contract-0001",
        expected_revision=approved["revision"],
        reason_code="schedule_changed",
    )
    assert cancel_replay.status_code == 200, cancel_replay.text
    assert cancel_replay.json() == cancelled

    after_cancel = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-start-cancelled-0001",
        expected_revision=cancelled["revision"],
    )
    assert after_cancel.status_code == 409, after_cancel.text
    with Session(visit_clients.engine) as session:
        assert list(session.exec(select(TrainSession))) == []
        assert list(session.exec(select(SessionRuntimeState))) == []


def test_cancel_reason_is_controlled_and_rejects_free_text(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-05",
        "visit-create-reason-0001",
    )
    free_text = _command(
        visit_clients.researcher,
        created["plan_id"],
        "cancel",
        key="visit-cancel-free-text-0001",
        expected_revision=created["revision"],
        reason_code="老人说今天不想来了",
    )
    assert free_text.status_code == 422, free_text.text
    extra_note = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/cancel",
        json={
            "idempotency_key": "visit-cancel-note-0001",
            "expected_revision": created["revision"],
            "reason_code": "schedule_changed",
            "note": "free text must not enter this command",
        },
    )
    assert extra_note.status_code == 422, extra_note.text


def test_today_queue_contains_only_approved_today_or_overdue_plans(
        visit_clients: VisitClients):
    approved_ids: set[str] = set()
    for patient_id, key, scheduled_date, queue_order in (
        ("P-VISIT-06", "visit-create-overdue-0001", TODAY - timedelta(days=1), 5),
        ("P-VISIT-07", "visit-create-today-0001", TODAY, 1),
    ):
        created = _create(
            visit_clients.researcher,
            patient_id,
            key,
            scheduled_date=scheduled_date,
            queue_order=queue_order,
        )
        approved = _command(
            visit_clients.researcher,
            created["plan_id"],
            "approve",
            key=key.replace("create", "approve"),
            expected_revision=created["revision"],
        )
        assert approved.status_code == 200, approved.text
        approved_ids.add(created["plan_id"])

    future = _create(
        visit_clients.researcher,
        "P-VISIT-08",
        "visit-create-future-0001",
        scheduled_date=TODAY + timedelta(days=1),
    )
    future_approved = _command(
        visit_clients.researcher,
        future["plan_id"],
        "approve",
        key="visit-approve-future-0001",
        expected_revision=future["revision"],
    )
    assert future_approved.status_code == 200, future_approved.text

    _create(
        visit_clients.researcher,
        "P-VISIT-09",
        "visit-create-draft-0001",
    )
    cancelled = _create(
        visit_clients.researcher,
        "P-VISIT-10",
        "visit-create-queue-cancel-0001",
    )
    cancelled_response = _command(
        visit_clients.researcher,
        cancelled["plan_id"],
        "cancel",
        key="visit-cancel-queue-0001",
        expected_revision=cancelled["revision"],
        reason_code="schedule_changed",
    )
    assert cancelled_response.status_code == 200, cancelled_response.text

    response = visit_clients.researcher.get("/visit-plans/today")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"
    payload = response.json()
    assert set(payload) == {"as_of_date", "plans"}
    assert payload["as_of_date"] == TODAY.isoformat()
    assert {plan["plan_id"] for plan in payload["plans"]} == approved_ids
    assert all(plan["status"] == "approved" for plan in payload["plans"])
    assert all(date.fromisoformat(plan["scheduled_date"]) <= TODAY
               for plan in payload["plans"])
    for plan in payload["plans"]:
        _assert_receipt(
            plan,
            status="approved",
            revision=2,
            patient_id=plan["patient_id"],
        )


def test_approve_and_start_revalidate_withdrawal_without_partial_mutation(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-11",
        "visit-create-withdrawal-0001",
    )
    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-11")
        assert patient is not None
        patient.withdrawal_status = "withdrawal_requested"
        session.add(patient)
        session.commit()

    denied_approve = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-withdrawn-0001",
        expected_revision=created["revision"],
    )
    assert denied_approve.status_code == 409, denied_approve.text

    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-11")
        assert patient is not None
        patient.withdrawal_status = None
        session.add(patient)
        session.commit()
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-after-restore-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()

    due_before_withdrawal = visit_clients.researcher.get("/visit-plans/today")
    assert due_before_withdrawal.status_code == 200, due_before_withdrawal.text
    assert created["plan_id"] in {
        row["plan_id"] for row in due_before_withdrawal.json()["plans"]
    }

    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-11")
        assert patient is not None
        patient.withdrawal_status = "withdrawn"
        session.add(patient)
        session.commit()
    due_after_withdrawal = visit_clients.researcher.get("/visit-plans/today")
    assert due_after_withdrawal.status_code == 200, due_after_withdrawal.text
    assert created["plan_id"] not in {
        row["plan_id"] for row in due_after_withdrawal.json()["plans"]
    }
    denied_start = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-start-withdrawn-0001",
        expected_revision=approved["revision"],
    )
    assert denied_start.status_code == 409, denied_start.text
    with Session(visit_clients.engine) as session:
        assert list(session.exec(select(TrainSession))) == []
        assert list(session.exec(select(SessionRuntimeState))) == []


def test_approve_and_start_revalidate_current_item_bank_version(
        visit_clients: VisitClients, monkeypatch):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-12",
        "visit-create-bank-drift-0001",
    )
    original_loader = content.load_item_bank
    drifted = replace(BANK, version_id="wk2-stale-test-version")
    monkeypatch.setattr(content, "load_item_bank", lambda _path: drifted)
    denied_approve = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-bank-drift-0001",
        expected_revision=created["revision"],
    )
    assert denied_approve.status_code == 409, denied_approve.text

    monkeypatch.setattr(content, "load_item_bank", original_loader)
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-bank-restored-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()

    monkeypatch.setattr(content, "load_item_bank", lambda _path: drifted)
    denied_start = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-start-bank-drift-0001",
        expected_revision=approved["revision"],
    )
    assert denied_start.status_code == 409, denied_start.text
    with Session(visit_clients.engine) as session:
        assert list(session.exec(select(TrainSession))) == []
        assert list(session.exec(select(SessionRuntimeState))) == []


def test_same_version_definition_drift_rejects_approve_start_and_replay(
        visit_clients: VisitClients, monkeypatch):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-10",
        "visit-create-definition-drift-0001",
    )
    drifted_bank = copy.deepcopy(BANK)
    drifted_bank.single_element[0]["initial_prompt"] += "（未升版修改）"
    assert drifted_bank.version_id == BANK.version_id
    monkeypatch.setattr(
        content, "load_item_bank", lambda _path: drifted_bank)
    denied_approve = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-definition-drift-0001",
        expected_revision=created["revision"],
    )
    assert denied_approve.status_code == 409, denied_approve.text
    assert denied_approve.json()["detail"]["code"] == (
        "visit_plan_content_digest_mismatch")

    monkeypatch.setattr(content, "load_item_bank", lambda _path: BANK)
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-definition-restored-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()

    drifted_protocol = copy.deepcopy(PROTOCOL)
    drifted_protocol["naming"]["success_after_cue2"] += "（未升版修改）"
    assert drifted_protocol["protocol_version_id"] == (
        PROTOCOL["protocol_version_id"])
    monkeypatch.setattr(
        content, "load_autopilot_protocol", lambda _path: drifted_protocol)
    denied_start = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-start-protocol-drift-0001",
        expected_revision=approved["revision"],
    )
    assert denied_start.status_code == 409, denied_start.text
    assert denied_start.json()["detail"]["code"] == (
        "visit_plan_protocol_digest_mismatch")

    # Even a previously successful approve receipt cannot be replayed after the
    # definition drifts; idempotency never turns stale content into authority.
    denied_replay = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-definition-restored-0001",
        expected_revision=created["revision"],
    )
    assert denied_replay.status_code == 409, denied_replay.text
    assert denied_replay.json()["detail"]["code"] == (
        "visit_plan_protocol_digest_mismatch")
    with Session(visit_clients.engine) as session:
        assert _linked_session_for_test(session, created["plan_id"]) is None


def test_delivery_manifest_rebinding_rejects_old_visit_plan(
        visit_clients: VisitClients, monkeypatch):
    """Changing private image bytes requires a new bound item-bank definition."""
    created = _create(
        visit_clients.researcher,
        "P-VISIT-10",
        "visit-create-asset-binding-drift-0001",
    )
    rebound_bank = copy.deepcopy(BANK)
    rebound_bank.meta["draft_revision"] = "2026-07-20.5-test"
    rebound_bank.meta["patient_asset_delivery_manifest"] = {
        "version_id": "wk2-private-webp-20260720.3",
        "definition_sha256": "a" * 64,
    }
    assert rebound_bank.version_id == BANK.version_id
    assert content.item_bank_definition_digest(rebound_bank) != (
        content.item_bank_definition_digest(BANK))
    monkeypatch.setattr(
        content, "load_item_bank", lambda _path: rebound_bank)

    denied = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-asset-binding-drift-0001",
        expected_revision=created["revision"],
    )

    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == (
        "visit_plan_content_digest_mismatch")
    with Session(visit_clients.engine) as session:
        assert _linked_session_for_test(session, created["plan_id"]) is None


def test_legacy_plan_without_definition_binding_cannot_be_approved(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-09",
        "visit-create-legacy-binding-0001",
    )
    with Session(visit_clients.engine) as session:
        plan = session.get(VisitPlan, created["plan_id"])
        assert plan is not None
        plan.item_bank_definition_digest = None
        plan.autopilot_protocol_version_id = None
        plan.autopilot_protocol_definition_digest = None
        session.add(plan)
        session.commit()

    denied = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-legacy-binding-0001",
        expected_revision=created["revision"],
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == (
        "visit_plan_definition_binding_missing")


def test_visit_plan_write_roles_are_researcher_or_admin_only(
        visit_clients: VisitClients):
    anonymous_read = visit_clients.anonymous.get("/visit-plans/today")
    assert anonymous_read.status_code == 401, anonymous_read.text
    anonymous_write = visit_clients.anonymous.post(
        "/visit-plans",
        json=_plan_body("P-VISIT-01", "visit-anonymous-create-0001"),
    )
    assert anonymous_write.status_code == 401, anonymous_write.text

    steward_create = visit_clients.steward.post(
        "/visit-plans",
        json=_plan_body("P-VISIT-01", "visit-steward-create-0001"),
    )
    assert steward_create.status_code == 403, steward_create.text

    researcher_plan = _create(
        visit_clients.researcher,
        "P-VISIT-01",
        "visit-researcher-role-0001",
    )
    steward_approve = _command(
        visit_clients.steward,
        researcher_plan["plan_id"],
        "approve",
        key="visit-steward-approve-0001",
        expected_revision=researcher_plan["revision"],
    )
    assert steward_approve.status_code == 403, steward_approve.text

    steward_read = visit_clients.steward.get("/visit-plans/today")
    assert steward_read.status_code == 200, steward_read.text
    admin_plan = _create(
        visit_clients.admin,
        "P-VISIT-02",
        "visit-admin-role-0001",
        session_sitting_no=2,
    )
    _assert_receipt(
        admin_plan, status="draft", revision=1, patient_id="P-VISIT-02")


def test_idempotent_replay_after_later_transitions_returns_original_receipt(
        visit_clients: VisitClients):
    create_body = _plan_body(
        "P-VISIT-01", "visit-history-create-0001", queue_order=17)
    create_response = visit_clients.researcher.post(
        "/visit-plans", json=create_body)
    assert create_response.status_code == 200, create_response.text
    created = create_response.json()

    approve_body = {
        "idempotency_key": "visit-history-approve-0001",
        "expected_revision": created["revision"],
    }
    approve_response = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/approve", json=approve_body)
    assert approve_response.status_code == 200, approve_response.text
    approved = approve_response.json()

    start_body = {
        "idempotency_key": "visit-history-start-0001",
        "expected_revision": approved["revision"],
    }
    start_response = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/start", json=start_body)
    assert start_response.status_code == 200, start_response.text
    started = start_response.json()

    # Idempotency is a response replay contract, not a shortcut to the mutable
    # current resource.  Every old key must keep returning the original fact.
    replay_create = visit_clients.researcher.post(
        "/visit-plans", json=create_body)
    replay_approve = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/approve", json=approve_body)
    replay_start = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/start", json=start_body)
    assert replay_create.status_code == 200, replay_create.text
    assert replay_approve.status_code == 200, replay_approve.text
    assert replay_start.status_code == 200, replay_start.text
    assert replay_create.json() == created
    assert replay_approve.json() == approved
    assert replay_start.json() == started

    with Session(visit_clients.engine) as session:
        commands = list(session.exec(
            select(VisitPlanCommand)
            .where(VisitPlanCommand.plan_id == created["plan_id"])
            .order_by(VisitPlanCommand.event_seq)))
        assert [(row.command_type, row.event_seq, row.resulting_revision)
                for row in commands] == [
            ("create", 1, 1),
            ("approve", 2, 2),
            ("start", 3, 3),
        ]


def test_withdrawal_blocks_historical_business_replays_without_session_leak(
        visit_clients: VisitClients):
    create_body = _plan_body(
        "P-VISIT-01", "visit-withdrawn-replay-create-0001")
    create_response = visit_clients.researcher.post(
        "/visit-plans", json=create_body)
    assert create_response.status_code == 200, create_response.text
    created = create_response.json()

    approve_body = {
        "idempotency_key": "visit-withdrawn-replay-approve-0001",
        "expected_revision": created["revision"],
    }
    approve_response = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/approve", json=approve_body)
    assert approve_response.status_code == 200, approve_response.text
    approved = approve_response.json()

    start_body = {
        "idempotency_key": "visit-withdrawn-replay-start-0001",
        "expected_revision": approved["revision"],
    }
    start_response = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/start", json=start_body)
    assert start_response.status_code == 200, start_response.text
    started = start_response.json()
    session_id = started["session_id"]
    assert isinstance(session_id, str) and session_id

    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-01")
        train_session = session.get(TrainSession, session_id)
        runtime = session.get(SessionRuntimeState, session_id)
        assert patient is not None
        assert train_session is not None
        assert runtime is not None
        session_snapshot = train_session.model_dump()
        runtime_snapshot = runtime.model_dump()
        patient.withdrawal_status = "withdrawn"
        session.add(patient)
        session.commit()

    replay_responses = (
        visit_clients.researcher.post("/visit-plans", json=create_body),
        visit_clients.researcher.post(
            f"/visit-plans/{created['plan_id']}/approve", json=approve_body),
        visit_clients.researcher.post(
            f"/visit-plans/{created['plan_id']}/start", json=start_body),
    )
    for replay in replay_responses:
        assert replay.status_code == 409, replay.text
        assert replay.json()["detail"] == {
            "code": "visit_plan_patient_withdrawn",
            "message": "受试者已进入撤回终态，该历史命令不再提供业务回执",
        }
        assert session_id not in replay.text

    with Session(visit_clients.engine) as session:
        sessions = list(session.exec(select(TrainSession)))
        runtimes = list(session.exec(select(SessionRuntimeState)))
        commands = list(session.exec(
            select(VisitPlanCommand)
            .where(VisitPlanCommand.plan_id == created["plan_id"])
            .order_by(VisitPlanCommand.event_seq)
        ))
        plan = session.get(VisitPlan, created["plan_id"])
        assert len(sessions) == 1
        assert len(runtimes) == 1
        assert len(commands) == 3
        assert plan is not None
        assert (plan.status, plan.revision) == ("started", 3)
        assert sessions[0].model_dump() == session_snapshot
        assert runtimes[0].model_dump() == runtime_snapshot


def test_cancel_replay_is_exact_before_withdrawal_and_hidden_afterward(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-02",
        "visit-withdrawn-cancel-create-0001",
    )
    cancel_body = {
        "idempotency_key": "visit-withdrawn-cancel-replay-0001",
        "expected_revision": created["revision"],
        "reason_code": "schedule_changed",
    }
    cancelled_response = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/cancel", json=cancel_body)
    assert cancelled_response.status_code == 200, cancelled_response.text
    cancelled = cancelled_response.json()

    replay_before_withdrawal = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/cancel", json=cancel_body)
    assert replay_before_withdrawal.status_code == 200
    assert replay_before_withdrawal.json() == cancelled

    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-02")
        assert patient is not None
        patient.withdrawal_status = "withdrawn"
        session.add(patient)
        session.commit()

    replay = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/cancel", json=cancel_body)
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"] == {
        "code": "visit_plan_patient_withdrawn",
        "message": "受试者已进入撤回终态，该历史命令不再提供业务回执",
    }
    assert cancelled["patient_id"] not in replay.text
    assert cancelled["scheduled_date"] not in replay.text
    assert cancelled["plan_id"] not in replay.text

    with Session(visit_clients.engine) as session:
        assert list(session.exec(select(TrainSession))) == []
        assert list(session.exec(select(SessionRuntimeState))) == []
        plan = session.get(VisitPlan, created["plan_id"])
        assert plan is not None
        assert (plan.status, plan.revision) == ("cancelled", 2)
        commands = list(session.exec(
            select(VisitPlanCommand)
            .where(VisitPlanCommand.plan_id == created["plan_id"])
            .order_by(VisitPlanCommand.event_seq)
        ))
        assert [(row.command_type, row.event_seq) for row in commands] == [
            ("create", 1),
            ("cancel", 2),
        ]


def test_concurrent_exact_create_replays_converge_on_one_fact(
        visit_clients: VisitClients):
    peer = _login("visit-researcher")
    body = _plan_body("P-VISIT-02", "visit-race-exact-create-0001")
    barrier = Barrier(2)

    def submit(client: TestClient):
        barrier.wait()
        response = client.post("/visit-plans", json=body)
        return response.status_code, response.json()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(submit, visit_clients.researcher)
            second_future = pool.submit(submit, peer)
            results = [first_future.result(), second_future.result()]
    finally:
        peer.close()

    assert [status for status, _payload in results] == [200, 200]
    assert results[0][1] == results[1][1]
    plan_id = results[0][1]["plan_id"]
    with Session(visit_clients.engine) as session:
        plans = list(session.exec(select(VisitPlan).where(
            VisitPlan.patient_id == "P-VISIT-02")))
        commands = list(session.exec(select(VisitPlanCommand).where(
            VisitPlanCommand.plan_id == plan_id)))
        assert len(plans) == 1
        assert len(commands) == 1
        assert commands[0].command_type == "create"


def test_concurrent_exact_approve_and_start_create_one_transition_and_session(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-08",
        "visit-race-mutation-create-0001",
    )
    peer = _login("visit-researcher")

    def race(action: str, body: dict):
        barrier = Barrier(2)

        def submit(client: TestClient):
            barrier.wait()
            response = client.post(
                f"/visit-plans/{created['plan_id']}/{action}", json=body)
            return response.status_code, response.json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(submit, visit_clients.researcher),
                pool.submit(submit, peer),
            ]
            return [future.result() for future in futures]

    try:
        approve_results = race("approve", {
            "idempotency_key": "visit-race-mutation-approve-0001",
            "expected_revision": created["revision"],
        })
        assert [status for status, _payload in approve_results] == [200, 200]
        assert approve_results[0][1] == approve_results[1][1]
        approved = approve_results[0][1]

        start_results = race("start", {
            "idempotency_key": "visit-race-mutation-start-0001",
            "expected_revision": approved["revision"],
        })
        assert [status for status, _payload in start_results] == [200, 200]
        assert start_results[0][1] == start_results[1][1]
        started = start_results[0][1]
    finally:
        peer.close()

    assert (started["status"], started["revision"]) == ("started", 3)
    with Session(visit_clients.engine) as session:
        commands = list(session.exec(
            select(VisitPlanCommand)
            .where(VisitPlanCommand.plan_id == created["plan_id"])
            .order_by(VisitPlanCommand.event_seq)))
        sessions = list(session.exec(select(TrainSession).where(
            TrainSession.visit_plan_id == created["plan_id"])))
        assert [row.command_type for row in commands] == [
            "create", "approve", "start"]
        assert len(sessions) == 1
        assert sessions[0].session_id == started["session_id"]
        assert session.get(SessionRuntimeState, started["session_id"]) is not None


def test_same_protocol_slot_race_conflicts_cleanly_and_cancel_releases_slot(
        visit_clients: VisitClients):
    peer = _login("visit-researcher")
    bodies = [
        _plan_body(
            "P-VISIT-03", "visit-slot-race-create-a-0001",
            scheduled_time="09:00:00", queue_order=1),
        _plan_body(
            "P-VISIT-03", "visit-slot-race-create-b-0001",
            scheduled_time="11:00:00", queue_order=2),
    ]
    barrier = Barrier(2)

    def submit(index: int, client: TestClient):
        barrier.wait()
        response = client.post("/visit-plans", json=bodies[index])
        return index, response.status_code, response.json()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(submit, 0, visit_clients.researcher),
                pool.submit(submit, 1, peer),
            ]
            results = [future.result() for future in futures]
    finally:
        peer.close()

    assert sorted(status for _index, status, _payload in results) == [200, 409]
    winner_index, _winner_status, winner = next(
        result for result in results if result[1] == 200)
    loser_index, _loser_status, loser = next(
        result for result in results if result[1] == 409)
    assert loser["detail"]["code"] == "visit_plan_protocol_slot_conflict"

    cancelled_response = _command(
        visit_clients.researcher,
        winner["plan_id"],
        "cancel",
        key="visit-slot-race-cancel-0001",
        expected_revision=winner["revision"],
        reason_code="schedule_changed",
    )
    assert cancelled_response.status_code == 200, cancelled_response.text
    cancelled = cancelled_response.json()
    assert cancelled["status"] == "cancelled"

    historical_create = visit_clients.researcher.post(
        "/visit-plans", json=bodies[winner_index])
    assert historical_create.status_code == 200, historical_create.text
    assert historical_create.json() == winner

    # The losing transaction left no ghost idempotency command.  Once cancel
    # atomically clears the active-slot key, the exact losing request can win.
    replacement_response = visit_clients.researcher.post(
        "/visit-plans", json=bodies[loser_index])
    assert replacement_response.status_code == 200, replacement_response.text
    replacement = replacement_response.json()
    assert replacement["plan_id"] != winner["plan_id"]
    assert time.fromisoformat(replacement["scheduled_time"]) == time(
        9 if loser_index == 0 else 11)

    with Session(visit_clients.engine) as session:
        plans = list(session.exec(select(VisitPlan).where(
            VisitPlan.patient_id == "P-VISIT-03")))
        commands = list(session.exec(select(VisitPlanCommand).where(
            VisitPlanCommand.plan_id.in_([row.plan_id for row in plans]))))
        assert sorted(row.status for row in plans) == ["cancelled", "draft"]
        assert next(row for row in plans if row.status == "cancelled").protocol_slot_key is None
        assert next(row for row in plans if row.status == "draft").protocol_slot_key
        assert sorted(row.command_type for row in commands) == [
            "cancel", "create", "create"]
        assert winner_index != loser_index


def test_start_integrity_failure_rolls_back_session_runtime_plan_and_command(
        visit_clients: VisitClients, monkeypatch):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-04",
        "visit-start-rollback-create-0001",
    )
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-start-rollback-approve-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()

    collision_id = "s-visit-start-atomic-collision"
    with Session(visit_clients.engine) as session:
        session.add(TrainSession(
            session_id=collision_id,
            patient_id="P-VISIT-05",
            session_sitting_no=99,
            training_date=TODAY,
            week_no=2,
            phase_type="正式训练",
            event_line="正式训练",
            trainer_id="ACTOR-fixture",
            item_bank_version_id=BANK.version_id,
            is_simulation=True,
            data_classification="simulation",
        ))
        session.add(SessionRuntimeState(
            session_id=collision_id,
            status="completed",
            revision=7,
            updated_at=datetime.now(),
        ))
        session.commit()

    original_new_id = visit_plan_service._new_id
    monkeypatch.setattr(
        visit_plan_service,
        "_new_id",
        lambda prefix: collision_id if prefix == "s" else original_new_id(prefix),
    )
    denied = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-start-rollback-start-0001",
        expected_revision=approved["revision"],
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "visit_plan_concurrency_conflict"

    with Session(visit_clients.engine) as session:
        plan = session.get(VisitPlan, created["plan_id"])
        assert plan is not None
        assert (plan.status, plan.revision, plan.started_by, plan.started_at) == (
            "approved", 2, None, None)
        assert list(session.exec(select(TrainSession).where(
            TrainSession.visit_plan_id == created["plan_id"]))) == []
        assert session.get(SessionRuntimeState, collision_id).revision == 7
        commands = list(session.exec(
            select(VisitPlanCommand)
            .where(VisitPlanCommand.plan_id == created["plan_id"])
            .order_by(VisitPlanCommand.event_seq)))
        assert [(row.command_type, row.event_seq) for row in commands] == [
            ("create", 1), ("approve", 2)]


def test_approve_and_start_revalidate_recording_and_simulation_identity(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-06",
        "visit-second-gate-create-0001",
    )
    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-06")
        assert patient is not None
        patient.recording_allowed = False
        session.add(patient)
        session.commit()

    denied_approve = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-second-gate-approve-denied-0001",
        expected_revision=created["revision"],
    )
    assert denied_approve.status_code == 409, denied_approve.text
    assert denied_approve.json()["detail"]["code"] == "visit_plan_patient_ineligible"

    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-06")
        assert patient is not None
        patient.recording_allowed = True
        session.add(patient)
        session.commit()
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-second-gate-approve-allowed-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()

    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-06")
        assert patient is not None
        patient.is_simulation_subject = False
        session.add(patient)
        session.commit()
    denied_start = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-second-gate-start-denied-0001",
        expected_revision=approved["revision"],
    )
    assert denied_start.status_code == 409, denied_start.text
    assert denied_start.json()["detail"]["code"] == "visit_plan_patient_ineligible"

    with Session(visit_clients.engine) as session:
        plan = session.get(VisitPlan, created["plan_id"])
        assert plan is not None
        assert (plan.status, plan.revision) == ("approved", 2)
        assert list(session.exec(select(TrainSession).where(
            TrainSession.visit_plan_id == created["plan_id"]))) == []
        commands = list(session.exec(select(VisitPlanCommand).where(
            VisitPlanCommand.plan_id == created["plan_id"])))
        assert sorted(row.command_type for row in commands) == ["approve", "create"]


def test_visit_plan_command_ledger_rejects_orm_update_and_delete(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-07",
        "visit-ledger-immutable-create-0001",
    )
    with Session(visit_clients.engine) as session:
        command = session.exec(select(VisitPlanCommand).where(
            VisitPlanCommand.plan_id == created["plan_id"])).one()
        command_id = command.id
        original_hash = command.request_hash
        command.request_hash = "0" * 64
        session.add(command)
        with pytest.raises(RuntimeError, match="只追加命令账本"):
            session.commit()
        session.rollback()

    with Session(visit_clients.engine) as session:
        command = session.get(VisitPlanCommand, command_id)
        assert command is not None
        assert command.request_hash == original_hash
        session.delete(command)
        with pytest.raises(RuntimeError, match="只追加命令账本"):
            session.commit()
        session.rollback()

    with Session(visit_clients.engine) as session:
        command = session.get(VisitPlanCommand, command_id)
        assert command is not None
        assert command.request_hash == original_hash
