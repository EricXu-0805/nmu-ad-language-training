"""Fail-closed research reads after withdrawal and abnormal item ownership."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import auth, db as app_db
from app.db import get_session
from app.enums import EventLine, ItemSetType, PhaseType, TaskType
from app.main import app
from app.models import (
    AbnormalEvent,
    AttemptEvent,
    AudioAssetRow,
    AudioCaptureReceipt,
    InteractionEvent,
    ItemEvent,
    Patient,
    ResearchUser,
    Session as TrainSession,
    TurnEvent,
)


@pytest.fixture
def account_client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(app_db, "engine", engine)
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(ResearchUser(
            username="withdrawal-reader",
            display_id="WITHDRAWAL-READER",
            password_hash=auth.hash_password("test-password-1"),
            role="researcher",
            created_at=datetime.now(),
        ))
        session.commit()

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    client = TestClient(app)
    login = client.post("/auth/login", json={
        "username": "withdrawal-reader", "password": "test-password-1",
    })
    assert login.status_code == 200, login.text
    client.headers.update({"X-CSRF-Token": client.cookies.get(auth.CSRF_COOKIE_NAME)})
    client.test_engine = engine
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


def _seed_session(session: Session, *, session_id: str, patient_id: str) -> ItemEvent:
    session.add(Patient(
        patient_id=patient_id,
        is_simulation_subject=True,
        consent_status="已同意",
        mandarin_eligible=True,
        recording_allowed=True,
    ))
    session.add(TrainSession(
        session_id=session_id,
        patient_id=patient_id,
        week_no=2,
        phase_type=PhaseType.正式训练,
        event_line=EventLine.正式训练,
        item_bank_version_id="wk2-v1-20260707",
        is_simulation=True,
        data_classification="simulation",
        trainer_id="WITHDRAWAL-READER",
    ))
    session.flush()
    item = ItemEvent(
        session_id=session_id,
        item_id="SE_锚",
        task_type=TaskType.单要素,
        item_set_type=ItemSetType.训练集,
        presentation_order=1,
    )
    session.add(item)
    session.flush()
    return item


def test_withdrawn_session_reads_return_only_content_free_tombstones(account_client):
    secret_values = {
        "withdrawal-audio-secret",
        "受试者说出的敏感回答",
        "人工确认的敏感回答",
        "模型判定敏感理由",
        "worker-secret-token",
        "现场自由文本敏感内容",
    }
    now = datetime.now()
    with Session(account_client.test_engine) as session:
        item = _seed_session(
            session, session_id="S-WITHDRAWN-READ", patient_id="P-WITHDRAWN-READ")
        audio = AudioAssetRow(
            raw_audio_id="withdrawal-audio-secret",
            session_id="S-WITHDRAWN-READ",
            turn_key="SE_锚#1",
            is_simulation=True,
            data_classification="simulation",
            checksum="a" * 64,
            byte_count=16,
            uploaded_at=now,
        )
        session.add(audio)
        session.flush()
        attempt = AttemptEvent(
            session_id="S-WITHDRAWN-READ",
            item_id="SE_锚",
            turn_seq=1,
            response_role="命名",
            attempt_seq=1,
            raw_audio_id=audio.raw_audio_id,
            prompt_level=0,
            asr_text="受试者说出的敏感回答",
            asr_confidence=0.9,
            operational_answer_type="偏题",
            operational_score=0,
            operational_needs_review=True,
            judge_reason="模型判定敏感理由",
            processing_status="completed",
            processing_owner="worker-secret-token",
            processing_claimed_at=now - timedelta(seconds=5),
            processing_lease_expires_at=now + timedelta(seconds=30),
            processing_generation=7,
            created_at=now,
            processed_at=now,
            is_simulation=True,
        )
        session.add(attempt)
        session.flush()
        session.add(TurnEvent(
            item_event_id=item.id,
            source_attempt_id=attempt.id,
            turn_seq=1,
            response_role="命名",
            raw_audio_id=audio.raw_audio_id,
            asr_text="受试者说出的敏感回答",
            confirmed_response_text="人工确认的敏感回答",
            ai_answer_type="偏题",
            ai_score=0,
        ))
        session.add(InteractionEvent(
            session_id="S-WITHDRAWN-READ",
            event_seq=1,
            item_id="SE_锚",
            turn_seq=1,
            attempt_id=attempt.id,
            attempt_seq=1,
            event_type="judgement_completed",
            payload_json='{"answer_type":"偏题","score":0}',
            is_simulation=True,
        ))
        session.add(AbnormalEvent(
            session_id="S-WITHDRAWN-READ",
            item_event_id=item.id,
            phase_type=PhaseType.正式训练,
            abnormal_type="其他",
            note="现场自由文本敏感内容",
            created_at=now,
        ))
        session.add(AudioCaptureReceipt(
            raw_audio_id=audio.raw_audio_id,
            session_id="S-WITHDRAWN-READ",
            turn_key="SE_锚#1",
            received_at=now,
            duration_seconds=1.5,
            byte_count=16,
            checksum="a" * 64,
            data_classification="simulation",
            is_simulation=True,
        ))
        session.commit()

    # Even before withdrawal, public research projections never expose worker
    # fencing fields.  Operational evidence remains available while authorized.
    attempts_before = account_client.get(
        "/sessions/S-WITHDRAWN-READ/attempts")
    assert attempts_before.status_code == 200
    projected_attempt = attempts_before.json()["attempts"][0]
    assert projected_attempt["asr_text"] == "受试者说出的敏感回答"
    for internal_field in {
        "processing_owner", "processing_claimed_at",
        "processing_lease_expires_at", "processing_generation",
    }:
        assert internal_field not in projected_attempt
    journal_before = account_client.get("/sessions/S-WITHDRAWN-READ/journal")
    assert journal_before.status_code == 200
    assert "processing_owner" not in journal_before.json()["attempts"][0]
    assert attempts_before.headers["cache-control"] == "private, no-store"
    assert journal_before.headers["cache-control"] == "private, no-store"

    with Session(account_client.test_engine) as session:
        patient = session.get(Patient, "P-WITHDRAWN-READ")
        patient.withdrawal_status = "withdrawn"
        session.add(patient)
        session.commit()

    attempts_after = account_client.get("/sessions/S-WITHDRAWN-READ/attempts")
    assert attempts_after.status_code == 200
    assert attempts_after.json() == {
        "attempts": [],
        "interactions": [],
        "truth_scope": "withdrawn_tombstone",
        "tombstone": {
            "schema_version": 1,
            "session_id": "S-WITHDRAWN-READ",
            "content_available": False,
            "reason_code": "subject_withdrawn",
            "record_counts": {"attempts": 1, "interactions": 1},
        },
    }

    journal_after = account_client.get("/sessions/S-WITHDRAWN-READ/journal")
    assert journal_after.status_code == 200
    journal = journal_after.json()
    assert journal["session"] == {
        "session_id": "S-WITHDRAWN-READ",
        "data_classification": "simulation",
        "is_simulation": True,
        "content_state": "withdrawn_tombstone",
    }
    assert journal["tombstone"]["record_counts"] == {
        "items": 1,
        "turns": 1,
        "audios": 1,
        "abnormal": 1,
        "attempts": 1,
        "interactions": 1,
        "audio_receipts": 1,
    }
    for collection in (
        "items", "turns", "audios", "abnormal", "attempts",
        "interactions", "audio_receipts",
    ):
        assert journal[collection] == []

    scores = account_client.get("/sessions/S-WITHDRAWN-READ/scores")
    assert scores.status_code == 409
    assert scores.json()["detail"]["code"] == "subject_withdrawn_content_unavailable"
    receipts = account_client.get("/sessions/S-WITHDRAWN-READ/audio-receipts")
    assert receipts.status_code == 409
    assert receipts.json()["detail"]["code"] == "subject_withdrawn_content_unavailable"
    assert scores.headers["cache-control"] == "private, no-store"
    assert receipts.headers["cache-control"] == "private, no-store"

    serialized = "\n".join((
        attempts_after.text, journal_after.text, scores.text, receipts.text,
    ))
    assert all(secret not in serialized for secret in secret_values)
    assert "P-WITHDRAWN-READ" not in serialized

    # An audio-level withdrawal must also hide its derived ASR/judgement even if
    # a legacy patient row has not yet received the study-withdrawal flag.
    with Session(account_client.test_engine) as session:
        patient = session.get(Patient, "P-WITHDRAWN-READ")
        patient.withdrawal_status = None
        patient.consent_status = "已同意"
        audio = session.get(AudioAssetRow, "withdrawal-audio-secret")
        audio.withdrawn = True
        session.add(patient)
        session.add(audio)
        session.commit()
    audio_withdrawn = account_client.get(
        "/sessions/S-WITHDRAWN-READ/attempts")
    assert audio_withdrawn.status_code == 200
    assert audio_withdrawn.json()["tombstone"]["reason_code"] == "recording_withdrawn"
    assert all(secret not in audio_withdrawn.text for secret in secret_values)

    # Older imports may encode withdrawal only in consent_status.  That is still
    # an explicit denial and cannot reopen content reads.
    with Session(account_client.test_engine) as session:
        patient = session.get(Patient, "P-WITHDRAWN-READ")
        patient.consent_status = "withdrawn"
        audio = session.get(AudioAssetRow, "withdrawal-audio-secret")
        audio.withdrawn = False
        session.add(patient)
        session.add(audio)
        session.commit()
    consent_withdrawn = account_client.get(
        "/sessions/S-WITHDRAWN-READ/journal")
    assert consent_withdrawn.status_code == 200
    assert consent_withdrawn.json()["tombstone"]["reason_code"] == "subject_withdrawn"
    assert all(secret not in consent_withdrawn.text for secret in secret_values)


def test_abnormal_item_event_must_belong_to_path_session(account_client):
    with Session(account_client.test_engine) as session:
        own_item = _seed_session(
            session, session_id="S-ABNORMAL-OWN", patient_id="P-ABNORMAL-OWN")
        foreign_item = _seed_session(
            session, session_id="S-ABNORMAL-FOREIGN", patient_id="P-ABNORMAL-FOREIGN")
        session.commit()
        own_id = int(own_item.id)
        foreign_id = int(foreign_item.id)

    foreign = account_client.post("/sessions/S-ABNORMAL-OWN/abnormal", json={
        "item_event_id": foreign_id,
        "abnormal_type": "环境噪声",
        "note": "不得落库",
    })
    assert foreign.status_code == 409
    assert foreign.json()["detail"]["code"] == "abnormal_item_session_mismatch"

    missing = account_client.post("/sessions/S-ABNORMAL-OWN/abnormal", json={
        "item_event_id": 999999,
        "abnormal_type": "环境噪声",
    })
    assert missing.status_code == 409
    assert missing.json() == foreign.json()

    with Session(account_client.test_engine) as session:
        assert list(session.exec(select(AbnormalEvent))) == []

    accepted = account_client.post("/sessions/S-ABNORMAL-OWN/abnormal", json={
        "item_event_id": own_id,
        "abnormal_type": "环境噪声",
    })
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["session_id"] == "S-ABNORMAL-OWN"
    assert accepted.json()["item_event_id"] == own_id
