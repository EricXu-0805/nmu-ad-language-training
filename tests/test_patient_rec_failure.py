"""Patient microphone startup failure is an idempotent, fail-closed pause fact."""
from __future__ import annotations

from datetime import datetime
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app import auth, db
from app.main import app
from app.models import (AttemptEvent, InteractionEvent, ItemEvent, LiveState,
                        PatientDeviceCapability, ResearchUser,
                        SessionRuntimeState)


BANK_VERSION = "wk2-v1-20260707"
FIRST_FAILURE_ID = "11111111-1111-4111-8111-111111111111"
SECOND_FAILURE_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def failure_client(tmp_path, monkeypatch):
    # Never touch the developer/default DB.  File-backed SQLite also exercises
    # independent middleware/handler sessions against one explicit test database.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'patient-rec-failure.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 1},
    )
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    client = TestClient(app)
    client.test_engine = engine
    yield client
    client.close()
    engine.dispose()


def _switch_live(client: TestClient, session_id: str) -> None:
    response = client.put("/live/state", json={
        "kind": "session",
        "payload": {
            "sessionId": session_id,
            "weekNo": 2,
            "eventLine": "正式训练",
            "mode": "task",
            "itemBankVersionId": BANK_VERSION,
        },
    })
    assert response.status_code == 200, response.text


def _seed_two_sessions_at_first_turn(client: TestClient) -> None:
    for suffix in ("ONE", "TWO"):
        patient = client.post("/patients", json={
            "patient_id": f"P-{suffix}",
            "consent_status": "已同意",
            "consent_type": "本人同意",
            "mandarin_eligible": True,
            "recording_allowed": True,
            "is_simulation_subject": True,
            "secondary_use_allowed": True,
        })
        assert patient.status_code == 200, patient.text
        training = client.post("/sessions", json={
            "session_id": f"S-{suffix}",
            "patient_id": f"P-{suffix}",
            "week_no": 2,
            "phase_type": "正式训练",
            "event_line": "正式训练",
            "item_bank_version_id": BANK_VERSION,
            "is_simulation": True,
        })
        assert training.status_code == 200, training.text

    _switch_live(client, "S-ONE")
    cursor = client.put("/live/state", json={
        "kind": "cursor",
        "payload": {
            "sessionId": "S-ONE",
            "screen": "record",
            "itemIdx": 0,
            "turnIdx": 0,
            "responseRole": "命名",
            "cueLevel": 2,
            "recording": "idle",
            "selfStart": False,
        },
    })
    assert cursor.status_code == 200, cursor.text


def _pair(client: TestClient, device_id: str) -> dict[str, str]:
    response = client.post(
        "/device/pair",
        headers={"X-Console-Pin": "24681024"},
        json={"deviceId": device_id},
    )
    assert response.status_code == 200, response.text
    return {"X-Device-Capability": response.json()["capability"]}


def _admin_client(engine) -> TestClient:
    with Session(engine) as session:
        session.add(ResearchUser(
            username="admin-mic",
            display_id="ADMIN-MIC",
            password_hash=auth.hash_password("password1"),
            role="admin",
            created_at=datetime.now(),
        ))
        session.commit()
    return _admin_client_with_existing_user(engine)


def _admin_client_with_existing_user(_engine) -> TestClient:
    client = TestClient(app)
    login = client.post("/auth/login", json={
        "username": "admin-mic",
        "password": "password1",
    })
    assert login.status_code == 200, login.text
    client.headers["X-CSRF-Token"] = client.cookies.get(auth.CSRF_COOKIE_NAME)
    return client


def _failure_payload(*, failure_id: str = FIRST_FAILURE_ID,
                     failure_code: str = "microphone_permission_denied",
                     session_id: str = "S-ONE", turn_key: str = "itm-0001#1") -> dict:
    return {
        "kind": "patientRec",
        "payload": {
            "active": False,
            "turnKey": turn_key,
            "sessionId": session_id,
            "failureCode": failure_code,
            "failureId": failure_id,
        },
    }


def _database_snapshot(engine) -> dict:
    with Session(engine) as session:
        live = session.get(LiveState, 1)
        runtime = session.get(SessionRuntimeState, "S-ONE")
        interactions = list(session.exec(select(InteractionEvent).where(
            InteractionEvent.session_id == "S-ONE").order_by(
                InteractionEvent.event_seq)))
        return {
            "live_seq": live.seq,
            "session": json.loads(live.session_json),
            "live_cursor": json.loads(live.cursor_json),
            "patient_rec": json.loads(live.patient_rec_json)
            if live.patient_rec_json else None,
            "runtime_status": runtime.status,
            "runtime_revision": runtime.revision,
            "runtime_cursor": json.loads(runtime.cursor_json),
            "interactions": [
                {
                    "event_seq": row.event_seq,
                    "event_type": row.event_type,
                    "item_id": row.item_id,
                    "turn_seq": row.turn_seq,
                    "attempt_id": row.attempt_id,
                    "payload": json.loads(row.payload_json),
                }
                for row in interactions
            ],
            "attempt_count": len(list(session.exec(select(AttemptEvent).where(
                AttemptEvent.session_id == "S-ONE")))),
            "item_count": len(list(session.exec(select(ItemEvent).where(
                ItemEvent.session_id == "S-ONE")))),
        }


@pytest.mark.parametrize("payload_patch", [
    {"failureCode": "microphone_permission_denied"},
    {"failureId": FIRST_FAILURE_ID},
    {"failureCode": None, "failureId": None},
    {
        "active": True,
        "failureCode": "microphone_permission_denied",
        "failureId": FIRST_FAILURE_ID,
    },
    {"failureCode": "not_a_failure_code", "failureId": FIRST_FAILURE_ID},
    {"failureCode": "microphone_permission_denied", "failureId": "not-a-uuid"},
    {
        "failureCode": "microphone_permission_denied",
        "failureId": FIRST_FAILURE_ID,
        "unexpected": "field",
    },
])
def test_patient_rec_failure_schema_is_strict_and_atomic(
        failure_client, monkeypatch, payload_patch):
    _seed_two_sessions_at_first_turn(failure_client)
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    capability = _pair(failure_client, "device-mic-schema-00001")
    payload = {
        "active": False,
        "turnKey": "itm-0001#1",
        "sessionId": "S-ONE",
    }
    payload.update(payload_patch)

    before = _database_snapshot(failure_client.test_engine)
    response = failure_client.put(
        "/live/state", headers=capability,
        json={"kind": "patientRec", "payload": payload},
    )
    assert response.status_code == 422, response.text
    assert _database_snapshot(failure_client.test_engine) == before


def test_first_failure_pauses_once_and_replays_are_pure_acks(
        failure_client, monkeypatch):
    _seed_two_sessions_at_first_turn(failure_client)
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    capability = _pair(failure_client, "device-mic-failure-0001")
    before = _database_snapshot(failure_client.test_engine)

    first = failure_client.put(
        "/live/state", headers=capability, json=_failure_payload())
    assert first.status_code == 200, first.text
    after = _database_snapshot(failure_client.test_engine)

    assert first.json()["seq"] == before["live_seq"] + 1
    assert after["live_seq"] == before["live_seq"] + 1
    assert after["runtime_status"] == "paused"
    assert after["runtime_revision"] == before["runtime_revision"] + 1
    assert after["session"]["paused"] is True
    assert after["patient_rec"] == {
        **_failure_payload()["payload"],
        "turnKey": "SE_胡萝卜#1",
    }
    assert after["interactions"] == [{
        "event_seq": 1,
        "event_type": "technical_pause",
        "item_id": "SE_胡萝卜",
        "turn_seq": 1,
        "attempt_id": None,
        "payload": {
            "error_code": "microphone_permission_denied",
            "failure_id": FIRST_FAILURE_ID,
        },
    }]
    assert after["attempt_count"] == before["attempt_count"] == 0
    assert after["item_count"] == before["item_count"] == 0
    for key in ("itemIdx", "turnIdx", "responseRole", "cueLevel"):
        assert after["runtime_cursor"][key] == before["runtime_cursor"][key]
        assert after["live_cursor"][key] == before["live_cursor"][key]
    assert after["live_cursor"]["screen"] == "paused"
    assert after["live_cursor"]["recording"] == "stopped"

    same = failure_client.put(
        "/live/state", headers=capability, json=_failure_payload())
    assert same.status_code == 200, same.text
    assert same.json()["seq"] == after["live_seq"]
    assert _database_snapshot(failure_client.test_engine) == after

    conflict = failure_client.put(
        "/live/state", headers=capability,
        json=_failure_payload(failure_code="microphone_not_found"),
    )
    assert conflict.status_code == 409, conflict.text
    assert _database_snapshot(failure_client.test_engine) == after

    invalid_turn_conflict = failure_client.put(
        "/live/state", headers=capability,
        json=_failure_payload(turn_key="not-a-frozen-turn"),
    )
    assert invalid_turn_conflict.status_code == 409, invalid_turn_conflict.text
    assert _database_snapshot(failure_client.test_engine) == after

    different_while_paused = failure_client.put(
        "/live/state", headers=capability,
        json=_failure_payload(
            failure_id=SECOND_FAILURE_ID,
            failure_code="microphone_start_failed"),
    )
    assert different_while_paused.status_code == 200, different_while_paused.text
    assert different_while_paused.json()["seq"] == after["live_seq"]
    assert _database_snapshot(failure_client.test_engine) == after

    delayed_active = failure_client.put("/live/state", headers=capability, json={
        "kind": "patientRec",
        "payload": {
            "active": True,
            "turnKey": "itm-0001#1",
            "sessionId": "S-ONE",
        },
    })
    assert delayed_active.status_code == 200, delayed_active.text
    assert delayed_active.json()["seq"] == after["live_seq"]
    assert _database_snapshot(failure_client.test_engine) == after

    admin = _admin_client(failure_client.test_engine)
    try:
        resumed = admin.post("/sessions/S-ONE/resume")
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["status"] == "active"
    finally:
        admin.close()
    recovered = _database_snapshot(failure_client.test_engine)
    assert recovered["patient_rec"] is None
    assert recovered["runtime_status"] == "active"
    for key in ("itemIdx", "turnIdx", "responseRole", "cueLevel"):
        assert recovered["runtime_cursor"][key] == after["runtime_cursor"][key]
        assert recovered["live_cursor"][key] == after["runtime_cursor"][key]

    # The first commit succeeded but its HTTP ACK may have been lost.  Even after
    # an explicit resume and legitimate cursor advance, replaying that durable id
    # is a pure ACK rather than a second pause at the new turn.
    admin = _admin_client_with_existing_user(failure_client.test_engine)
    try:
        advanced = admin.put("/live/state", json={
            "kind": "cursor",
            "payload": {
                "sessionId": "S-ONE",
                "screen": "present",
                "itemIdx": 1,
                "turnIdx": 0,
                "responseRole": "命名",
                "cueLevel": 0,
                "recording": "idle",
                "selfStart": False,
            },
        })
        assert advanced.status_code == 200, advanced.text
    finally:
        admin.close()
    advanced_snapshot = _database_snapshot(failure_client.test_engine)
    assert advanced_snapshot["runtime_cursor"]["itemIdx"] == 1
    late_ack = failure_client.put(
        "/live/state", headers=capability, json=_failure_payload())
    assert late_ack.status_code == 200, late_ack.text
    assert late_ack.json()["seq"] == advanced_snapshot["live_seq"]
    assert _database_snapshot(failure_client.test_engine) == advanced_snapshot


def test_failure_rejects_stale_turn_cross_session_and_terminal_runtime(
        failure_client, monkeypatch):
    _seed_two_sessions_at_first_turn(failure_client)
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    capability = _pair(failure_client, "device-mic-reject-00001")
    before = _database_snapshot(failure_client.test_engine)

    stale_turn = failure_client.put(
        "/live/state", headers=capability,
        json=_failure_payload(turn_key="itm-0002#1"),
    )
    assert stale_turn.status_code == 409, stale_turn.text
    cross_session = failure_client.put(
        "/live/state", headers=capability,
        json=_failure_payload(session_id="S-TWO"),
    )
    assert cross_session.status_code == 409, cross_session.text
    assert _database_snapshot(failure_client.test_engine) == before

    with Session(failure_client.test_engine) as session:
        state = session.get(SessionRuntimeState, "S-ONE")
        state.status = "completed"
        session.add(state)
        session.commit()
    terminal_before = _database_snapshot(failure_client.test_engine)
    terminal = failure_client.put(
        "/live/state", headers=capability, json=_failure_payload())
    assert terminal.status_code == 409, terminal.text
    assert _database_snapshot(failure_client.test_engine) == terminal_before


def test_failure_rejects_recovery_only_and_revoked_device_generations(
        failure_client, monkeypatch):
    _seed_two_sessions_at_first_turn(failure_client)
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    old = _pair(failure_client, "device-mic-generation-01")

    replacement = _pair(failure_client, "device-mic-generation-02")
    revoked = failure_client.put(
        "/live/state", headers=old, json=_failure_payload())
    assert revoked.status_code == 401, revoked.text
    assert revoked.json()["code"] == "device_capability_revoked"

    # A current replacement remains authorized; switch before using it so this
    # test can also prove the demoted generation cannot create failure facts.
    admin = _admin_client(failure_client.test_engine)
    try:
        _switch_live(admin, "S-TWO")
        recovery_only = failure_client.put(
            "/live/state", headers=replacement, json=_failure_payload())
        assert recovery_only.status_code == 401, recovery_only.text
        assert recovery_only.json()["code"] == "device_capability_recovery_only"
    finally:
        admin.close()

    with Session(failure_client.test_engine) as session:
        rows = list(session.exec(select(PatientDeviceCapability).where(
            PatientDeviceCapability.session_id == "S-ONE")))
        assert len(rows) == 2
        assert any(row.revoked_at is not None for row in rows)
        assert any(row.recovery_only_at is not None for row in rows)
        assert list(session.exec(select(InteractionEvent))) == []
