"""单课题组工作区的共享边界合同。

只使用内存数据库与合成编号：Patient roster / patient-level 量表对具名
课题组共享，而场次内容仍按 trainer_id 隔离。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session as DBSession, SQLModel, create_engine

from app import access_policy, auth, db
from app.enums import ConsentType, EventLine, PhaseType
from app.main import app
from app.models import (Patient, ResearchUser, ScaleResult, Session,
                        SessionRuntimeState)


_PASSWORD = "synthetic-password-2026"
_SHARED_PATIENT_ID = "P-SYNTHETIC-SHARED"
_SESSION_PATIENT_ID = "P-SYNTHETIC-SESSION"
_OWNER_SESSION_ID = "S-SYNTHETIC-OWNER"


def _login(engine, username: str) -> TestClient:
    client = TestClient(app)
    response = client.post("/auth/login", json={
        "username": username,
        "password": _PASSWORD,
    })
    assert response.status_code == 200, response.text
    csrf = client.cookies.get(auth.CSRF_COOKIE_NAME)
    assert csrf
    client.headers.update({"X-CSRF-Token": csrf})
    return client


@pytest.fixture
def shared_workspace(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)

    password_hash = auth.hash_password(_PASSWORD)
    with DBSession(engine) as session:
        session.add(Patient(
            patient_id=_SHARED_PATIENT_ID,
            is_simulation_subject=False,
            dementia_severity="合成轻度",
            mandarin_eligible=True,
            consent_status="已同意",
            consent_type=ConsentType.本人同意,
            recording_allowed=True,
            secondary_use_allowed=True,
        ))
        session.add(Patient(
            patient_id=_SESSION_PATIENT_ID,
            is_simulation_subject=True,
            consent_status="已同意",
            consent_type=ConsentType.本人同意,
            mandarin_eligible=True,
            recording_allowed=True,
        ))
        session.add(Session(
            session_id=_OWNER_SESSION_ID,
            patient_id=_SESSION_PATIENT_ID,
            week_no=2,
            phase_type=PhaseType.正式训练,
            event_line=EventLine.正式训练,
            trainer_id="ACTOR-a",
            item_bank_version_id="synthetic-bank-v1",
            is_simulation=True,
            data_classification="simulation",
        ))
        session.add(SessionRuntimeState(
            session_id=_OWNER_SESSION_ID,
            status="active",
            revision=0,
        ))
        session.add(ScaleResult(
            patient_id=_SHARED_PATIENT_ID,
            phase_type="前测",
            scale_name="synthetic-legacy-scale",
            score=7,
            assessor_id="ACTOR-a",
        ))
        for username, role in (
            ("a", "researcher"),
            ("b", "researcher"),
            ("steward", "data_steward"),
            ("admin", "admin"),
            ("unknown", "future_unknown_role"),
        ):
            session.add(ResearchUser(
                username=username,
                display_id=f"ACTOR-{username}",
                password_hash=password_hash,
                role=role,
                created_at=datetime.now(),
            ))
        session.commit()

    clients = {
        name: _login(engine, name)
        for name in ("a", "b", "steward", "admin", "unknown")
    }
    clients["anonymous"] = TestClient(app)
    try:
        yield clients
    finally:
        for client in clients.values():
            client.close()
        engine.dispose()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path", [
    "/patients",
    "/patients/P-SYNTHETIC",
    "/patients/P-SYNTHETIC/scales",
])
def test_patient_workspace_reads_are_explicit_known_account_rules(method, path):
    rule = access_policy.access_rule(method, path)
    assert rule.kind is access_policy.AccessKind.ACCOUNT
    assert rule.roles == access_policy.KNOWN_ACCOUNT_ROLES


def test_named_study_team_shares_patient_scales_but_not_researcher_sessions(
        shared_workspace):
    owner = shared_workspace["a"]
    other = shared_workspace["b"]
    steward = shared_workspace["steward"]

    blocked_write = owner.post(f"/patients/{_SHARED_PATIENT_ID}/scales", json={
        "phase_type": "前测",
        "scale_name": "must-never-be-created",
        "score": 99,
        "assessor_id": "FORGED-BY-CLIENT",
    })
    assert blocked_write.status_code == 409, blocked_write.text
    assert blocked_write.json()["detail"]["code"] == "scale_protocol_not_frozen"

    roster_ids = {row["patient_id"] for row in other.get("/patients").json()}
    assert {_SHARED_PATIENT_ID, _SESSION_PATIENT_ID} <= roster_ids

    shared_rows = other.get(f"/patients/{_SHARED_PATIENT_ID}/scales")
    assert shared_rows.status_code == 200, shared_rows.text
    assert shared_rows.headers["cache-control"] == "private, no-store"
    assert len(shared_rows.json()) == 1
    assert shared_rows.json()[0]["assessor_id"] == "ACTOR-a"

    foreign_session = other.get(f"/sessions/{_OWNER_SESSION_ID}")
    missing_session = other.get("/sessions/S-SYNTHETIC-MISSING")
    assert foreign_session.status_code == missing_session.status_code == 404
    assert foreign_session.json() == missing_session.json() == {
        "detail": "场次不存在",
    }

    steward_rows = steward.get(f"/patients/{_SHARED_PATIENT_ID}/scales")
    assert steward_rows.status_code == 200, steward_rows.text
    assert steward_rows.json()[0]["assessor_id"] == "ACTOR-a"
    assert steward.post(f"/patients/{_SHARED_PATIENT_ID}/scales", json={
        "phase_type": "后测",
        "scale_name": "synthetic-forbidden-write",
        "score": 8,
    }).status_code == 403
    active_session = steward.get(f"/sessions/{_OWNER_SESSION_ID}")
    assert active_session.status_code == 403
    assert active_session.json()["detail"]["code"] == \
        "session_terminal_read_required"


def test_unknown_anonymous_and_withdrawn_reads_fail_closed(shared_workspace):
    other = shared_workspace["b"]
    administrator = shared_workspace["admin"]
    unknown = shared_workspace["unknown"]
    anonymous = shared_workspace["anonymous"]

    for path in ("/patients", f"/patients/{_SHARED_PATIENT_ID}/scales"):
        assert unknown.get(path).status_code == 403
        assert anonymous.get(path).status_code == 401

    withdrawal = administrator.post(
        f"/patients/{_SHARED_PATIENT_ID}/withdrawal",
        json={
            "idempotency_key": (
                "synthetic-shared-boundary-withdrawal-"
                "20260719-abcdefghijklmnopqrstuvwxyz"
            ),
            "expected_governance_revision": 0,
            "reason_code": "participant_request",
        },
    )
    assert withdrawal.status_code == 200, withdrawal.text

    blocked = other.get(f"/patients/{_SHARED_PATIENT_ID}/scales")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == \
        "subject_withdrawn_content_unavailable"
    assert blocked.headers["cache-control"] == "private, no-store"
    assert blocked.headers["pragma"] == "no-cache"
