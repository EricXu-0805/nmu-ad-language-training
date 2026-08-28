"""CAS/idempotency and privacy regression tests for research confirmations."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import auth, db
from app.db import get_session
from app.main import app
from app.models import (
    AuditLog,
    ItemEvent,
    Patient,
    ResearchUser,
    Session as TrainSession,
    SessionRuntimeState,
    TurnConfirmationRevision,
    TurnEvent,
)


@pytest.fixture
def confirmation_env(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "13579024")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for username, display_id, role in (
            ("reviewer-a", "REVIEWER-A", "researcher"),
            ("reviewer-b", "REVIEWER-B", "researcher"),
            ("steward", "STEWARD", "data_steward"),
        ):
            session.add(ResearchUser(
                username=username,
                display_id=display_id,
                password_hash=auth.hash_password("password-2026"),
                role=role,
                created_at=datetime.now(),
            ))
        session.add(Patient(patient_id="P-CONFIRM", is_simulation_subject=True))
        session.add(TrainSession(
            session_id="S-CONFIRM",
            patient_id="P-CONFIRM",
            week_no=2,
            phase_type="正式训练",
            event_line="正式训练",
            item_bank_version_id="wk2-v1-20260707",
            trainer_id="REVIEWER-A",
            is_simulation=True,
            data_classification="simulation",
        ))
        session.commit()
        item = ItemEvent(
            session_id="S-CONFIRM", item_id="SE_锚", task_type="单要素",
            item_set_type="训练集", presentation_order=1)
        session.add(item)
        session.commit()
        session.refresh(item)
        turn = TurnEvent(
            item_event_id=item.id,
            turn_seq=1,
            response_role="命名",
            asr_text="原始转写",
        )
        session.add(turn)
        session.add(SessionRuntimeState(
            session_id="S-CONFIRM",
            status="intervention_completed",
            revision=1,
            intervention_completed_at=datetime.now(),
        ))
        session.commit()
        session.refresh(turn)
        turn_id = turn.id

    def override_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    yield engine, turn_id
    app.dependency_overrides.clear()


def _logged_in_client(username: str) -> TestClient:
    client = TestClient(app)
    response = client.post("/auth/login", json={
        "username": username,
        "password": "password-2026",
    })
    assert response.status_code == 200, response.text
    csrf = client.cookies.get(auth.CSRF_COOKIE_NAME)
    assert csrf
    client.headers.update({"X-CSRF-Token": csrf})
    return client


def _payload(text: str, revision: int, key: str) -> dict:
    return {
        "confirmed_response_text": text,
        "expected_revision": revision,
        "idempotency_key": key,
    }


def test_confirmation_is_named_cas_revision_with_text_free_append_only_ledger(
        confirmation_env):
    engine, turn_id = confirmation_env
    client = _logged_in_client("reviewer-a")
    secret_response = "患者的人工确认作答"
    body = _payload(secret_response, 0, "confirm-ledger-0001")

    created = client.patch(f"/turns/{turn_id}/confirm", json=body)
    assert created.status_code == 200, created.text
    assert created.json()["confirmation_revision"] == 1
    assert created.json()["confirmed_response_text"] == secret_response

    replay = client.patch(f"/turns/{turn_id}/confirm", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == created.json()

    with Session(engine) as session:
        rows = list(session.exec(select(TurnConfirmationRevision)))
        assert len(rows) == 1
        row = rows[0]
        assert row.turn_id == turn_id
        assert row.session_id == "S-CONFIRM"
        assert (row.expected_revision, row.revision) == (0, 1)
        assert row.actor_display_id == "REVIEWER-A"
        assert len(row.before_sha256) == len(row.after_sha256) == 64
        assert row.before_sha256 != row.after_sha256
        # 修订账本的所有字符串列都不得复制作答原文。
        ledger_strings = (
            row.session_id, row.actor_display_id, row.before_sha256,
            row.after_sha256, row.idempotency_key,
        )
        assert all(secret_response not in value for value in ledger_strings)
        audits = list(session.exec(select(AuditLog).where(
            AuditLog.turn_id == turn_id)))
        assert len(audits) == 2
        assert all(secret_response not in audit.summary for audit in audits)


def test_stale_parallel_writer_and_idempotency_mismatch_fail_closed(
        confirmation_env):
    engine, turn_id = confirmation_env
    reviewer_a = _logged_in_client("reviewer-a")
    reviewer_a_parallel = _logged_in_client("reviewer-a")
    first = _payload("第一版", 0, "confirm-race-a-0001")
    stale = _payload("并发旧版", 0, "confirm-race-b-0001")

    assert reviewer_a.patch(f"/turns/{turn_id}/confirm", json=first).status_code == 200
    conflict = reviewer_a_parallel.patch(f"/turns/{turn_id}/confirm", json=stale)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "turn_confirmation_revision_conflict"
    assert conflict.json()["detail"]["current_revision"] == 1

    changed_same_key = reviewer_a.patch(
        f"/turns/{turn_id}/confirm",
        json=_payload("偷换内容", 0, "confirm-race-a-0001"),
    )
    assert changed_same_key.status_code == 409
    assert changed_same_key.json()["detail"]["code"] == "turn_confirmation_idempotency_conflict"

    second = reviewer_a_parallel.patch(
        f"/turns/{turn_id}/confirm",
        json=_payload("第二版", 1, "confirm-race-b-0002"),
    )
    assert second.status_code == 200
    assert second.json()["confirmation_revision"] == 2

    superseded = reviewer_a.patch(f"/turns/{turn_id}/confirm", json=first)
    assert superseded.status_code == 409
    assert superseded.json()["detail"]["code"] == "turn_confirmation_replay_superseded"
    with Session(engine) as session:
        rows = list(session.exec(select(TurnConfirmationRevision).order_by(
            TurnConfirmationRevision.revision)))
        assert [(row.expected_revision, row.revision) for row in rows] == [(0, 1), (1, 2)]
        assert [row.actor_display_id for row in rows] == ["REVIEWER-A", "REVIEWER-A"]


def test_old_client_steward_and_post_lock_change_are_rejected(confirmation_env):
    engine, turn_id = confirmation_env
    reviewer = _logged_in_client("reviewer-a")
    steward = _logged_in_client("steward")

    old_client = reviewer.patch(f"/turns/{turn_id}/confirm", json={
        "confirmed_response_text": "旧客户端",
    })
    assert old_client.status_code == 422
    assert steward.patch(
        f"/turns/{turn_id}/confirm",
        json=_payload("越权修订", 0, "confirm-steward-0001"),
    ).status_code == 403

    accepted = reviewer.patch(
        f"/turns/{turn_id}/confirm",
        json=_payload("锁定前版本", 0, "confirm-lock-0001"),
    )
    assert accepted.status_code == 200
    with Session(engine) as session:
        turn = session.get(TurnEvent, turn_id)
        assert turn is not None
        turn.score_locked = True
        session.add(turn)
        session.commit()

    blocked = reviewer.patch(
        f"/turns/{turn_id}/confirm",
        json=_payload("锁定后篡改", 1, "confirm-lock-0002"),
    )
    assert blocked.status_code == 409
    with Session(engine) as session:
        turn = session.get(TurnEvent, turn_id)
        assert turn is not None
        assert turn.confirmed_response_text == "锁定前版本"
        assert turn.confirmation_revision == 1


def test_confirmation_ledger_rows_reject_orm_update_and_delete(confirmation_env):
    engine, turn_id = confirmation_env
    reviewer = _logged_in_client("reviewer-a")
    assert reviewer.patch(
        f"/turns/{turn_id}/confirm",
        json=_payload("只追加", 0, "confirm-append-0001"),
    ).status_code == 200

    with Session(engine) as session:
        row = session.exec(select(TurnConfirmationRevision)).one()
        row.actor_display_id = "TAMPER"
        session.add(row)
        with pytest.raises(RuntimeError, match="只追加"):
            session.commit()
        session.rollback()

        row = session.exec(select(TurnConfirmationRevision)).one()
        session.delete(row)
        with pytest.raises(RuntimeError, match="只追加"):
            session.commit()
