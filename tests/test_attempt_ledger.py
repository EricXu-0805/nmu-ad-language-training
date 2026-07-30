import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import asr, evidence_ledger, export, export_security, llm_judge
from app.db import get_session
from app.enums import AnswerType
from app.judging import PortraitLeakError
from app.llm_judge import LlmJudgement
from app.main import app
from app.models import (
    AttemptCaptureProcessing,
    AttemptEvent, InteractionEvent, Session as TrainSession,
    SessionCloseoutReport, SessionOutcomeSummary, SessionRuntimeState,
)


BANK_VERSION = "wk2-v1-20260707"


@pytest.fixture
def client_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    try:
        yield TestClient(app), engine
    finally:
        app.dependency_overrides.clear()


def _seed_session(client: TestClient, *, session_id: str = "S-AI",
                  patient_id: str = "P-AI") -> None:
    patient = client.post("/patients", json={
        "patient_id": patient_id,
        "is_simulation_subject": True,
        "consent_status": "已同意",
        "consent_type": "本人同意",
        "mandarin_eligible": True,
        "recording_allowed": True,
        "secondary_use_allowed": True,
    })
    assert patient.status_code == 200, patient.text
    session = client.post("/sessions", json={
        "session_id": session_id,
        "patient_id": patient_id,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "item_bank_version_id": BANK_VERSION,
        "is_simulation": True,
    })
    assert session.status_code == 200, session.text


def _seed_audio(client: TestClient, raw_audio_id: str, *, session_id: str = "S-AI",
                turn_key: str = "SE_锚#1", contains_identifier: bool = False,
                upload: bool = True) -> None:
    created = client.post("/audio", json={
        "raw_audio_id": raw_audio_id,
        "session_id": session_id,
        "turn_key": turn_key,
        "contains_direct_identifier": contains_identifier,
    })
    assert created.status_code == 200, created.text
    if upload:
        stored = client.put(
            f"/audio/{raw_audio_id}/blob", content=b"\x1aE\xdf\xa3attempt-audio",
            headers={"content-type": "audio/webm"})
        assert stored.status_code == 200, stored.text


def _body(raw_audio_id: str, **overrides) -> dict:
    body = {
        "item_id": "SE_锚",
        "turn_seq": 1,
        "response_role": "命名",
        "raw_audio_id": raw_audio_id,
        "prompt_level": 0,
        "duration_seconds": 2.5,
    }
    body.update(overrides)
    return body


class _FakeAsr:
    version = "fake-asr-1"
    data_boundary = "local"
    provider_id = None

    def __init__(self, text: str | None = "锚", *, raises: bool = False):
        self.text = text
        self.raises = raises
        self.calls = 0

    def transcribe(self, _audio_bytes, _hotwords):
        self.calls += 1
        if self.raises:
            raise RuntimeError("provider unavailable")
        return asr.AsrResult(self.text, 0.91 if self.text else None,
                             self.version, hotword_hit=bool(self.text))


class _FakeJudge:
    version = "fake-judge-1"
    data_boundary = "local"
    provider_id = None

    def __init__(self, *, raises: bool = False):
        self.raises = raises
        self.calls = 0

    def judge(self, _judge_input):
        self.calls += 1
        if self.raises:
            raise RuntimeError("judge unavailable")
        return LlmJudgement(AnswerType.正确, 1.0, False, reason="命中目标词")


def _install_engines(monkeypatch, fake_asr: _FakeAsr, fake_judge: _FakeJudge) -> None:
    monkeypatch.setattr("app.main.asr.get_engine", lambda: fake_asr)
    llm_judge.register_engine("attempt-test", fake_judge)
    monkeypatch.setenv("LLM_JUDGE", "attempt-test")


def _seed_recoverable_attempt(engine, raw_audio_id: str, *, stage: str,
                              lease_expires_at: datetime,
                              generation: int = 4) -> int:
    """Materialize a process-crash boundary without invoking either engine."""
    now = datetime.now()
    with Session(engine) as session:
        attempt = AttemptEvent(
            session_id="S-AI", item_id="SE_锚", turn_seq=1,
            response_role="命名", attempt_seq=1, raw_audio_id=raw_audio_id,
            prompt_level=0, duration_seconds=2.5,
            processing_status=stage,
            processing_owner="dead-worker",
            processing_lease_expires_at=lease_expires_at,
            processing_claimed_at=now - timedelta(minutes=5),
            processing_generation=generation,
            asr_text="锚" if stage == "asr_completed" else None,
            asr_confidence=0.91 if stage == "asr_completed" else None,
            asr_engine_version="crashed-asr-1" if stage == "asr_completed" else None,
            created_at=now - timedelta(minutes=5), is_simulation=True,
        )
        session.add(attempt)
        session.flush()
        session.add(InteractionEvent(
            session_id="S-AI", event_seq=1, item_id="SE_锚", turn_seq=1,
            attempt_id=attempt.id, attempt_seq=1, event_type="attempt_received",
            payload_json=evidence_ledger.encode_event_payload("attempt_received", {
                "raw_audio_id": raw_audio_id, "prompt_level": 0,
                "cue_type": None, "duration_seconds": 2.5,
                "processing_status": "received",
            }), created_at=attempt.created_at, is_simulation=True,
        ))
        if stage == "asr_completed":
            session.add(InteractionEvent(
                session_id="S-AI", event_seq=2, item_id="SE_锚", turn_seq=1,
                attempt_id=attempt.id, attempt_seq=1, event_type="asr_completed",
                payload_json=evidence_ledger.encode_event_payload("asr_completed", {
                    "asr_engine_version": "crashed-asr-1", "asr_confidence": 0.91,
                    "degraded": False, "hotword_hit": True,
                }), created_at=attempt.created_at, is_simulation=True,
            ))
        session.commit()
        return int(attempt.id)


def test_process_persists_provenance_and_raw_audio_is_idempotency_key(client_db, monkeypatch):
    client, _engine = client_db
    _seed_session(client)
    _seed_audio(client, "attempt-1")
    fake_asr, fake_judge = _FakeAsr(), _FakeJudge()
    _install_engines(monkeypatch, fake_asr, fake_judge)

    first = client.post("/sessions/S-AI/attempts/process", json=_body("attempt-1"))
    assert first.status_code == 200, first.text
    body = first.json()
    attempt = body["attempt"]
    assert body["status"] == "completed" and body["idempotent"] is False
    assert body["truth_scope"] == "operational_only"
    assert attempt["attempt_seq"] == 1 and attempt["asr_text"] == "锚"
    assert attempt["asr_engine_version"] == "fake-asr-1"
    assert attempt["operational_answer_type"] == "正确"
    assert attempt["judge_mode"] == "LLM辅助"
    assert attempt["judge_engine_version"] == "fake-judge-1"
    assert attempt["judge_reason"] == "命中目标词"
    assert attempt["judge_portrait_used"] is False
    assert [event["event_type"] for event in body["interactions"]] == [
        "attempt_received", "asr_completed", "judgement_completed",
    ]

    repeated = client.post("/sessions/S-AI/attempts/process", json=_body("attempt-1"))
    assert repeated.status_code == 200
    assert repeated.json()["idempotent"] is True
    assert repeated.json()["attempt"]["id"] == attempt["id"]
    assert fake_asr.calls == fake_judge.calls == 1

    _seed_audio(client, "attempt-2")
    second = client.post("/sessions/S-AI/attempts/process", json=_body("attempt-2"))
    assert second.status_code == 200
    assert second.json()["attempt"]["attempt_seq"] == 2
    ledger = client.get("/sessions/S-AI/attempts").json()
    assert len(ledger["attempts"]) == 2
    event_seqs = [event["event_seq"] for event in ledger["interactions"]]
    assert event_seqs == list(range(1, len(event_seqs) + 1))
    assert client.get("/sessions/S-AI/journal").json()["turns"] == []


@pytest.mark.parametrize(
    ("crash_stage", "expected_asr_calls", "seeded_events"),
    [("received", 1, ["attempt_received"]),
     ("asr_completed", 0, ["attempt_received", "asr_completed"])],
)
def test_expired_crash_stage_is_taken_over_without_duplicate_evidence(
        client_db, monkeypatch, crash_stage, expected_asr_calls, seeded_events):
    client, engine = client_db
    _seed_session(client)
    raw_audio_id = f"recover-{crash_stage}"
    _seed_audio(client, raw_audio_id)
    attempt_id = _seed_recoverable_attempt(
        engine, raw_audio_id, stage=crash_stage,
        lease_expires_at=datetime.now() - timedelta(seconds=1))
    fake_asr, fake_judge = _FakeAsr(), _FakeJudge()
    _install_engines(monkeypatch, fake_asr, fake_judge)

    recovered = client.post(
        "/sessions/S-AI/attempts/process", json=_body(raw_audio_id))
    assert recovered.status_code == 200, recovered.text
    body = recovered.json()
    assert body["status"] == "completed"
    assert body["idempotent"] is False and body["in_progress"] is False
    assert fake_asr.calls == expected_asr_calls
    assert fake_judge.calls == 1
    assert [row["event_type"] for row in body["interactions"]] == [
        "attempt_received", "asr_completed", "judgement_completed",
    ]
    assert [row["event_type"] for row in body["interactions"]
            ][:len(seeded_events)] == seeded_events
    assert "processing_owner" not in body["attempt"]
    assert "processing_lease_expires_at" not in body["attempt"]

    with Session(engine) as session:
        stored = session.get(AttemptEvent, attempt_id)
        assert stored is not None
        assert stored.processing_generation == 5
        assert stored.processing_owner is None
        assert stored.processing_lease_expires_at is None

    # 终态重试仅读取同一行，不再触发任何引擎或事件。
    repeated = client.post(
        "/sessions/S-AI/attempts/process", json=_body(raw_audio_id))
    assert repeated.status_code == 200
    assert repeated.json()["idempotent"] is True
    assert repeated.json()["attempt"]["id"] == attempt_id
    assert fake_asr.calls == expected_asr_calls and fake_judge.calls == 1


def test_nonexpired_claim_returns_202_and_different_request_still_conflicts(
        client_db, monkeypatch):
    client, engine = client_db
    _seed_session(client)
    _seed_audio(client, "leased-attempt")
    _seed_recoverable_attempt(
        engine, "leased-attempt", stage="received",
        lease_expires_at=datetime.now() + timedelta(seconds=60))
    fake_asr, fake_judge = _FakeAsr(), _FakeJudge()
    _install_engines(monkeypatch, fake_asr, fake_judge)

    waiting = client.post(
        "/sessions/S-AI/attempts/process", json=_body("leased-attempt"))
    assert waiting.status_code == 202, waiting.text
    assert waiting.headers["retry-after"] == str(
        waiting.json()["retry_after_seconds"])
    assert waiting.json()["status"] == "received"
    assert waiting.json()["in_progress"] is True
    assert waiting.json()["idempotent"] is True
    assert "processing_owner" not in waiting.json()["attempt"]
    assert fake_asr.calls == fake_judge.calls == 0

    conflict = client.post("/sessions/S-AI/attempts/process", json=_body(
        "leased-attempt", duration_seconds=3.0))
    assert conflict.status_code == 409
    assert fake_asr.calls == fake_judge.calls == 0
    with Session(engine) as session:
        events = list(session.exec(select(InteractionEvent)))
        assert [event.event_type for event in events] == ["attempt_received"]


def test_concurrent_exact_retry_observes_active_claim_and_calls_engines_once(
        tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'endpoint-race.db'}",
        connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    def override():
        with Session(engine) as session:
            yield session

    class _BlockingAsr(_FakeAsr):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def transcribe(self, audio_bytes, hotwords):
            self.calls += 1
            self.entered.set()
            assert self.release.wait(timeout=5), "test did not release ASR"
            return asr.AsrResult(
                self.text, 0.91, self.version, hotword_hit=True)

    app.dependency_overrides[get_session] = override
    first_client, retry_client = TestClient(app), TestClient(app)
    try:
        _seed_session(first_client)
        _seed_audio(first_client, "live-claim")
        fake_asr, fake_judge = _BlockingAsr(), _FakeJudge()
        _install_engines(monkeypatch, fake_asr, fake_judge)

        with ThreadPoolExecutor(max_workers=1) as pool:
            first_future = pool.submit(
                first_client.post, "/sessions/S-AI/attempts/process",
                json=_body("live-claim"))
            assert fake_asr.entered.wait(timeout=5)
            waiting = retry_client.post(
                "/sessions/S-AI/attempts/process", json=_body("live-claim"))
            assert waiting.status_code == 202, waiting.text
            assert waiting.json()["in_progress"] is True
            assert fake_asr.calls == 1 and fake_judge.calls == 0
            fake_asr.release.set()
            completed = first_future.result(timeout=5)

        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "completed"
        terminal_retry = retry_client.post(
            "/sessions/S-AI/attempts/process", json=_body("live-claim"))
        assert terminal_retry.status_code == 200
        assert terminal_retry.json()["idempotent"] is True
        assert fake_asr.calls == fake_judge.calls == 1
        with Session(engine) as session:
            events = list(session.exec(select(InteractionEvent).order_by(
                InteractionEvent.event_seq)))
            assert [event.event_type for event in events] == [
                "attempt_received", "asr_completed", "judgement_completed",
            ]
    finally:
        app.dependency_overrides.clear()


def test_atomic_expired_takeover_and_generation_fence_on_sqlite(tmp_path):
    """Separate DB connections model separate workers; exactly one CAS claim wins."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'claim-race.db'}",
        connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    expired_at = datetime.now() - timedelta(seconds=1)
    with Session(engine) as session:
        row = AttemptEvent(
            session_id="S-RACE", item_id="SE_锚", turn_seq=1,
            response_role="命名", attempt_seq=1, raw_audio_id="race-audio",
            prompt_level=0, processing_status="received",
            processing_owner="crashed", processing_lease_expires_at=expired_at,
            processing_claimed_at=expired_at - timedelta(seconds=10),
            processing_generation=7, is_simulation=True,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        attempt_id = int(row.id)

    barrier = threading.Barrier(2)

    def claim(owner: str) -> bool:
        with Session(engine) as session:
            barrier.wait(timeout=5)
            won = evidence_ledger.try_claim_attempt(
                session, attempt_id, owner=owner, now=datetime.now())
            if won:
                session.commit()
            else:
                session.rollback()
            return won

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ["worker-a", "worker-b"]))
    assert outcomes.count(True) == 1

    with Session(engine) as session:
        current = session.get(AttemptEvent, attempt_id)
        assert current is not None
        assert current.processing_generation == 8
        winning_claim = evidence_ledger.claim_from_attempt(current)

        stale_claim = evidence_ledger.AttemptClaim(
            attempt_id=attempt_id, owner="crashed", generation=7,
            stage="received", lease_expires_at=expired_at)
        assert evidence_ledger.fenced_attempt_update(
            session, stale_claim, expected_status="received",
            next_status="asr_completed", values={
                "asr_text": "stale", "asr_engine_version": "stale-asr",
            }) is False
        assert evidence_ledger.fenced_attempt_update(
            session, winning_claim, expected_status="received",
            next_status="asr_completed", values={
                "asr_text": "锚", "asr_engine_version": "winner-asr",
            }) is True
        session.commit()
        session.expire_all()
        advanced = session.get(AttemptEvent, attempt_id)
        assert advanced.processing_status == "asr_completed"
        assert advanced.asr_text == "锚"
        assert advanced.processing_generation == 8


def test_capture_processing_atomic_expired_takeover_and_generation_fence_on_sqlite(
        tmp_path):
    """Same CAS race as AttemptEvent's claim, one layer earlier.

    R1-foundation: the persistent capture-processing claim is made durable at
    record_stopped time, before ASR ever runs, so a future repeat-classifier
    can inspect ASR text before attempt_seq is consumed. Its claim/lease/
    generation fencing mirrors AttemptEvent's exactly.
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'capture-claim-race.db'}",
        connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    expired_at = datetime.now() - timedelta(seconds=1)
    with Session(engine) as session:
        row = AttemptCaptureProcessing(
            record_command_id=1, predecessor_command_id=2,
            receipt_server_seq=1, raw_audio_id="race-capture-audio",
            session_id="S-CAPTURE-RACE", item_id="SE_锚", turn_seq=1,
            proof_attempt_seq=1, proof_prompt_level=0,
            processing_status="received",
            processing_owner="crashed", processing_lease_expires_at=expired_at,
            processing_claimed_at=expired_at - timedelta(seconds=10),
            processing_generation=7, is_simulation=True,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        capture_id = int(row.id)

    barrier = threading.Barrier(2)

    def claim(owner: str) -> bool:
        with Session(engine) as session:
            barrier.wait(timeout=5)
            won = evidence_ledger.try_claim_capture(
                session, capture_id, owner=owner, now=datetime.now())
            if won:
                session.commit()
            else:
                session.rollback()
            return won

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ["worker-a", "worker-b"]))
    assert outcomes.count(True) == 1

    with Session(engine) as session:
        current = session.get(AttemptCaptureProcessing, capture_id)
        assert current is not None
        assert current.processing_generation == 8
        winning_claim = evidence_ledger.claim_from_capture(current)

        stale_claim = evidence_ledger.CaptureClaim(
            capture_id=capture_id, owner="crashed", generation=7,
            proof_attempt_seq=1, proof_prompt_level=0,
            lease_expires_at=expired_at)
        assert evidence_ledger.fenced_capture_update(
            session, stale_claim, next_status="asr_completed", values={
                "asr_engine_version": "stale-asr",
                "disposition": "answer_candidate", "error_code": None,
                "final_attempt_id": 999, "processed_at": datetime.now(),
            }) is False
        assert evidence_ledger.fenced_capture_update(
            session, winning_claim, next_status="asr_completed", values={
                "asr_engine_version": "winner-asr",
                "disposition": "answer_candidate", "error_code": None,
                "final_attempt_id": 999, "processed_at": datetime.now(),
            }) is True
        session.commit()
        session.expire_all()
        advanced = session.get(AttemptCaptureProcessing, capture_id)
        assert advanced.processing_status == "asr_completed"
        assert advanced.asr_engine_version == "winner-asr"
        assert advanced.disposition == "answer_candidate"
        assert advanced.final_attempt_id == 999
        assert advanced.processing_owner is None
        assert advanced.processing_lease_expires_at is None
        assert advanced.processing_generation == 8


def test_invalidate_capture_processing_claims_fences_in_flight_worker(tmp_path):
    """Pause/takeover/withdrawal must fence an in-flight capture claim.

    Mirrors invalidate_processing_claims for AttemptEvent: a late ASR result
    for a fenced generation may no longer transition the row.
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'capture-claim-invalidate.db'}",
        connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        row = AttemptCaptureProcessing(
            record_command_id=1, predecessor_command_id=2,
            receipt_server_seq=1, raw_audio_id="invalidate-capture-audio",
            session_id="S-CAPTURE-INVALIDATE", item_id="SE_锚", turn_seq=1,
            proof_attempt_seq=1, proof_prompt_level=0,
            processing_status="received",
            processing_owner="in-flight-worker",
            processing_lease_expires_at=datetime.now() + timedelta(seconds=60),
            processing_claimed_at=datetime.now(),
            processing_generation=3, is_simulation=True,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        capture_id = int(row.id)
        claim = evidence_ledger.claim_from_capture(row)

    with Session(engine) as session:
        fenced = evidence_ledger.invalidate_capture_processing_claims(
            session, session_id="S-CAPTURE-INVALIDATE")
        session.commit()
    assert fenced == 1

    with Session(engine) as session:
        current = session.get(AttemptCaptureProcessing, capture_id)
        assert current.processing_owner is None
        assert current.processing_lease_expires_at is None
        assert current.processing_generation == 4
        # The pre-fence claim can no longer transition the row.
        assert evidence_ledger.fenced_capture_update(
            session, claim, next_status="asr_completed", values={
                "asr_engine_version": "late-asr",
                "disposition": "answer_candidate", "error_code": None,
                "final_attempt_id": 42,
            }) is False


def test_ensure_capture_processing_is_idempotent_per_record_command(tmp_path):
    """record_stopped's admission never duplicates a row for the same command."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'capture-ensure-idempotent.db'}",
        connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        first = evidence_ledger.ensure_capture_processing(
            session, record_command_id=1, predecessor_command_id=2,
            receipt_server_seq=1, raw_audio_id="ensure-capture-audio",
            session_id="S-CAPTURE-ENSURE", item_id="SE_锚", turn_seq=1,
            proof_attempt_seq=1, proof_prompt_level=0, is_simulation=True,
        )
        session.commit()
        second = evidence_ledger.ensure_capture_processing(
            session, record_command_id=1, predecessor_command_id=2,
            receipt_server_seq=1, raw_audio_id="ensure-capture-audio",
            session_id="S-CAPTURE-ENSURE", item_id="SE_锚", turn_seq=1,
            proof_attempt_seq=1, proof_prompt_level=0, is_simulation=True,
        )
        session.commit()
        assert first.id == second.id
        rows = list(session.exec(select(AttemptCaptureProcessing)))
        assert len(rows) == 1


def test_ensure_capture_processing_integrity_error_only_rolls_back_its_own_savepoint(
        tmp_path):
    """A conflicting insert inside ensure_capture_processing must only unwind
    its own SAVEPOINT — never the caller's already-staged outer-transaction
    work (e.g. apply_device_ack's ACK/command/state changes earlier in the
    same request). A plain ``session.rollback()`` here would silently
    discard that prior work while the caller believes its request succeeded.
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'capture-savepoint-fault.db'}",
        connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        # Occupies the raw_audio_id unique slot under a different
        # record_command_id: ensure_capture_processing's own SELECT-by-
        # record_command_id finds nothing and proceeds to INSERT, which then
        # collides on raw_audio_id — a genuine, unrecoverable-by-record-
        # command-id conflict that must still fail loud without destroying
        # unrelated work.
        session.add(AttemptCaptureProcessing(
            record_command_id=999, predecessor_command_id=998,
            receipt_server_seq=1, raw_audio_id="conflicting-audio-id",
            session_id="S-SAVEPOINT", item_id="SE_锚", turn_seq=1,
            proof_attempt_seq=1, proof_prompt_level=0,
            processing_status="received", is_simulation=True,
        ))
        session.commit()

    with Session(engine) as session:
        # Stand-in for apply_device_ack's own earlier, still-uncommitted
        # work in the same outer transaction (an ACK/command/state write).
        marker = AttemptEvent(
            session_id="S-SAVEPOINT", item_id="SE_锚", turn_seq=1,
            response_role="命名", attempt_seq=1,
            raw_audio_id="unrelated-marker-audio",
            prompt_level=0, processing_status="received", is_simulation=True,
        )
        session.add(marker)
        session.flush()
        marker_id = marker.id

        with pytest.raises(IntegrityError):
            evidence_ledger.ensure_capture_processing(
                session, record_command_id=1, predecessor_command_id=2,
                receipt_server_seq=2, raw_audio_id="conflicting-audio-id",
                session_id="S-SAVEPOINT", item_id="SE_锚", turn_seq=1,
                proof_attempt_seq=1, proof_prompt_level=0, is_simulation=True,
            )

        # The outer transaction's own earlier write survives the failed
        # savepoint intact and is still committable.
        assert session.get(AttemptEvent, marker_id) is not None
        session.commit()

    with Session(engine) as session:
        assert session.get(AttemptEvent, marker_id) is not None
        conflicting_rows = list(session.exec(select(AttemptCaptureProcessing).where(
            AttemptCaptureProcessing.raw_audio_id == "conflicting-audio-id")))
        assert len(conflicting_rows) == 1
        assert conflicting_rows[0].record_command_id == 999


def test_claim_lease_migration_preserves_a8_attempts_and_makes_stalls_claimable(
        tmp_path):
    db_path = tmp_path / "attempt-lease-migration.db"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "a8b5d3f1c902")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        for attempt_id, status in enumerate(
                ("received", "asr_completed", "completed", "technical_failure"), 1):
            conn.execute(text(
                "INSERT INTO attemptevent "
                "(id, session_id, item_id, turn_seq, response_role, attempt_seq, "
                "raw_audio_id, prompt_level, processing_status, created_at, "
                "judge_portrait_used, is_simulation) "
                "VALUES (:id, 'S-LEGACY', 'SE_锚', 1, '命名', :seq, :audio, 0, "
                ":status, '2026-07-18 00:00:00', 0, 1)"
            ), {
                "id": attempt_id, "seq": attempt_id,
                "audio": f"legacy-audio-{attempt_id}", "status": status,
            })
        conn.execute(text(
            "INSERT INTO interactionevent "
            "(id, session_id, event_seq, item_id, turn_seq, attempt_id, attempt_seq, "
            "event_type, payload_json, created_at, is_simulation) "
            "VALUES (1, 'S-LEGACY', 1, 'SE_锚', 1, 1, 1, 'attempt_received', "
            "'{}', '2026-07-18 00:00:00', 1)"
        ))

    command.upgrade(config, "b9c6e4f2d013")
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, processing_status, processing_owner, "
            "processing_lease_expires_at, processing_claimed_at, "
            "processing_generation FROM attemptevent ORDER BY id"
        )).mappings().all()
    assert [row["processing_status"] for row in rows] == [
        "received", "asr_completed", "completed", "technical_failure",
    ]
    assert all(row["processing_owner"] is None for row in rows)
    assert all(row["processing_lease_expires_at"] is None for row in rows)
    assert all(row["processing_claimed_at"] is None for row in rows)
    assert all(row["processing_generation"] == 0 for row in rows)
    with engine.connect() as conn:
        linked = conn.execute(text(
            "SELECT attempt_id, event_type FROM interactionevent WHERE id = 1"
        )).one()
    assert linked.attempt_id == 1 and linked.event_type == "attempt_received"

    # 既有非终态行升级后不会被伪租约卡住，可用同一 CAS 立即接管。
    with Session(engine) as session:
        assert evidence_ledger.try_claim_attempt(
            session, 1, owner="post-migration-worker") is True
        session.commit()
        recovered = session.get(AttemptEvent, 1)
        assert recovered.processing_generation == 1
        assert recovered.processing_owner == "post-migration-worker"


def test_asr_degraded_and_judgement_exception_leave_safe_pause_evidence(
        client_db, monkeypatch):
    client, _engine = client_db
    _seed_session(client)
    _seed_audio(client, "degraded")
    _seed_audio(client, "judge-failure")
    assert client.put("/live/state", json={"kind": "session", "payload": {
        "sessionId": "S-AI", "weekNo": 2, "eventLine": "正式训练",
        "mode": "task", "itemBankVersionId": BANK_VERSION,
    }}).status_code == 200
    assert client.put("/live/state", json={"kind": "cursor", "payload": {
        "sessionId": "S-AI", "screen": "record", "itemIdx": 0, "turnIdx": 0,
        "responseRole": "命名", "cueLevel": 0, "recording": "recording",
        "selfStart": True,
    }}).status_code == 200
    degraded_asr, unused_judge = _FakeAsr(None), _FakeJudge()
    _install_engines(monkeypatch, degraded_asr, unused_judge)

    degraded = client.post(
        "/sessions/S-AI/attempts/process", json=_body("degraded"))
    assert degraded.status_code == 200
    assert degraded.json()["status"] == "technical_failure"
    assert degraded.json()["attempt"]["error_code"] == "asr_degraded"
    assert [row["event_type"] for row in degraded.json()["interactions"]] == [
        "attempt_received", "asr_failed", "technical_pause",
    ]
    assert unused_judge.calls == 0

    # 不依赖前端再发 /pause：失败证据、runtime paused 与老人端收麦同事务落库。
    runtime_after_asr = client.get("/sessions/S-AI/runtime").json()
    assert runtime_after_asr["status"] == "paused"
    assert runtime_after_asr["cursor"]["screen"] == "present"
    assert runtime_after_asr["cursor"]["recording"] == "idle"
    live_after_asr = client.get("/live/state").json()
    assert live_after_asr["session"]["paused"] is True
    assert live_after_asr["cursor"]["screen"] == "paused"
    assert live_after_asr["cursor"]["recording"] == "stopped"

    # 同 raw_audio_id 重试是纯幂等取回，不重复加 revision/wseq。
    repeated = client.post(
        "/sessions/S-AI/attempts/process", json=_body("degraded"))
    assert repeated.status_code == 200 and repeated.json()["idempotent"] is True
    assert client.get("/sessions/S-AI/runtime").json()["revision"] == runtime_after_asr["revision"]

    assert client.post("/sessions/S-AI/resume").json()["status"] == "active"
    good_asr, exploding_judge = _FakeAsr(), _FakeJudge(raises=True)
    _install_engines(monkeypatch, good_asr, exploding_judge)
    failed = client.post(
        "/sessions/S-AI/attempts/process", json=_body("judge-failure"))
    assert failed.status_code == 200
    assert failed.json()["status"] == "technical_failure"
    assert failed.json()["attempt"]["error_code"] == "judgement_exception"
    assert [row["event_type"] for row in failed.json()["interactions"]][-2:] == [
        "judgement_failed", "technical_pause",
    ]
    assert client.get("/sessions/S-AI/runtime").json()["status"] == "paused"


def test_rule_fallback_records_actual_engine_and_matched_on(client_db, monkeypatch):
    client, _engine = client_db
    _seed_session(client)
    _seed_audio(client, "rule-provenance")
    fake_asr = _FakeAsr("锚")
    monkeypatch.setattr("app.main.asr.get_engine", lambda: fake_asr)
    monkeypatch.setenv("LLM_JUDGE", "off")
    response = client.post(
        "/sessions/S-AI/attempts/process", json=_body("rule-provenance"))
    assert response.status_code == 200, response.text
    attempt = response.json()["attempt"]
    assert attempt["judge_mode"] == "规则确定式"
    assert attempt["judge_engine_version"] == "rule-1"
    assert attempt["matched_on"] == "target"
    assert attempt["judge_reason"] is None


def test_open_answer_without_frozen_rubric_is_a_technical_pause_not_success(
        client_db, monkeypatch):
    client, _engine = client_db
    _seed_session(client)
    _seed_audio(
        client, "missing-rubric", turn_key="DE_斧子+树#5")
    fake_asr, fake_judge = _FakeAsr("用斧子砍树"), _FakeJudge()
    _install_engines(monkeypatch, fake_asr, fake_judge)

    response = client.post("/sessions/S-AI/attempts/process", json=_body(
        "missing-rubric", item_id="DE_斧子+树", turn_seq=5,
        response_role="关系识别"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "technical_failure"
    assert body["attempt"]["error_code"] == "operational_rubric_unavailable"
    assert [row["event_type"] for row in body["interactions"]] == [
        "attempt_received", "asr_completed", "judgement_failed", "technical_pause",
    ]
    assert fake_judge.calls == 0
    assert client.get("/sessions/S-AI/runtime").json()["status"] == "paused"


def test_completed_attempt_authoritatively_populates_turn_without_second_judge_call(
        client_db, monkeypatch):
    client, _engine = client_db
    _seed_session(client)
    _seed_audio(client, "turn-source")
    fake_asr, fake_judge = _FakeAsr(), _FakeJudge()
    _install_engines(monkeypatch, fake_asr, fake_judge)
    processed = client.post(
        "/sessions/S-AI/attempts/process", json=_body("turn-source"))
    assert processed.status_code == 200
    attempt = processed.json()["attempt"]
    item = client.post("/sessions/S-AI/items", json={
        "item_id": "SE_锚", "task_type": "单要素",
    })
    assert item.status_code == 200, item.text

    conflict = client.post(f"/items/{item.json()['id']}/turns", json={
        "turn_seq": 1, "response_role": "命名", "raw_audio_id": "turn-source",
        "asr_text": "客户端伪造冲突",
    })
    assert conflict.status_code == 409
    turn = client.post(f"/items/{item.json()['id']}/turns", json={
        "turn_seq": 1, "response_role": "命名", "raw_audio_id": "turn-source",
    })
    assert turn.status_code == 200, turn.text
    row = turn.json()
    assert row["source_attempt_id"] == attempt["id"]
    assert row["asr_text"] == attempt["asr_text"]
    assert row["asr_confidence"] == attempt["asr_confidence"]
    assert row["prompt_level"] == attempt["prompt_level"]
    assert row["duration_seconds"] == attempt["duration_seconds"]
    assert row["ai_answer_type"] == attempt["operational_answer_type"]
    assert row["ai_score"] == attempt["operational_score"]
    assert row["ai_judge_mode"] == attempt["judge_mode"]
    assert row["judge_portrait_used"] is False

    # 旧前端即使追加 ai-judge 请求也只幂等读取，不会再调模型。
    repeated_judge = client.post(f"/turns/{row['id']}/ai-judge")
    assert repeated_judge.status_code == 200
    assert fake_judge.calls == 1
    assert client.post(f"/items/{item.json()['id']}/turns", json={
        "turn_seq": 1, "response_role": "命名", "raw_audio_id": "turn-source",
    }).status_code == 409


def test_technical_failure_attempt_cannot_populate_final_turn(client_db, monkeypatch):
    client, _engine = client_db
    _seed_session(client)
    _seed_audio(client, "failed-turn-source")
    _install_engines(monkeypatch, _FakeAsr(None), _FakeJudge())
    processed = client.post(
        "/sessions/S-AI/attempts/process", json=_body("failed-turn-source"))
    assert processed.json()["status"] == "technical_failure"
    item = client.post("/sessions/S-AI/items", json={
        "item_id": "SE_锚", "task_type": "单要素",
    }).json()
    denied = client.post(f"/items/{item['id']}/turns", json={
        "turn_seq": 1, "response_role": "命名",
        "raw_audio_id": "failed-turn-source",
    })
    assert denied.status_code == 409 and "technical_failure" in denied.json()["detail"]


def test_process_validates_frozen_position_audio_binding_blob_and_portrait_boundary(
        client_db, monkeypatch):
    client, _engine = client_db
    _seed_session(client)
    fake_asr, fake_judge = _FakeAsr(), _FakeJudge()
    _install_engines(monkeypatch, fake_asr, fake_judge)

    _seed_audio(client, "no-blob", upload=False)
    assert client.post(
        "/sessions/S-AI/attempts/process", json=_body("no-blob")).status_code == 409

    _seed_audio(client, "wrong-turn", turn_key="SE_花#1")
    assert client.post(
        "/sessions/S-AI/attempts/process", json=_body("wrong-turn")).status_code == 409

    _seed_audio(client, "wrong-role")
    assert client.post("/sessions/S-AI/attempts/process", json=_body(
        "wrong-role", response_role="左命名")).status_code == 409
    leaked = client.post("/sessions/S-AI/attempts/process", json={
        **_body("wrong-role"), "zodiac": "牛",
    })
    assert leaked.status_code == 422
    assert client.get("/sessions/S-AI/attempts").json()["attempts"] == []
    with pytest.raises(PortraitLeakError):
        evidence_ledger.encode_event_payload(
            "feedback_selected", {"feedback_key": "correct", "zodiac": "牛"})


def test_manual_events_are_strict_and_terminal_blocks_new_evidence_but_not_reads(
        client_db, monkeypatch):
    client, engine = client_db
    _seed_session(client)
    _seed_audio(client, "done-before-terminal")
    _seed_audio(client, "new-after-terminal")
    fake_asr, fake_judge = _FakeAsr(), _FakeJudge()
    _install_engines(monkeypatch, fake_asr, fake_judge)
    done = client.post(
        "/sessions/S-AI/attempts/process", json=_body("done-before-terminal"))
    assert done.status_code == 200
    attempt_id = done.json()["attempt"]["id"]

    plan = client.get("/sessions/S-AI/plan", params={
        "week_no": 2, "event_line": "正式训练",
    }).json()
    item_idx = next(
        idx for idx, row in enumerate(plan["items"])
        if row["item_id"] == "SE_锚")
    turn_idx = next(
        idx for idx, row in enumerate(plan["items"][item_idx]["turns"])
        if row["turn_seq"] == 1)
    handshake = client.put("/live/state", json={
        "kind": "session",
        "payload": {
            "sessionId": "S-AI", "weekNo": 2,
            "eventLine": "正式训练", "mode": "task",
            "itemBankVersionId": BANK_VERSION,
        },
    })
    assert handshake.status_code == 200, handshake.text
    cursor = {
        "sessionId": "S-AI", "screen": "present",
        "itemIdx": item_idx, "turnIdx": turn_idx,
        "responseRole": "命名", "cueLevel": 0,
        "recording": "idle", "selfStart": False,
    }
    assert client.put("/live/state", json={
        "kind": "cursor", "payload": cursor,
    }).status_code == 200
    live_cursor = client.get("/live/state").json()["cursor"]
    runtime_revision = client.get(
        "/sessions/S-AI/runtime").json()["revision"]
    feedback = client.post("/sessions/S-AI/interaction-presentations", json={
        "idempotency_key": "attempt-feedback-self-0001",
        "interaction": {
            "event_type": "feedback_selected",
            "attempt_id": attempt_id,
            "feedback_key": "self",
        },
        "cursor": {
            **cursor,
            "fbKey": "self", "fbItemId": "SE_锚",
            "wseq": live_cursor["wseq"],
            "expected_revision": runtime_revision,
        },
    })
    assert feedback.status_code == 200, feedback.text
    assert json.loads(feedback.json()["interaction"]["payload_json"]) == {
        "feedback_key": "self"}
    assert feedback.json()["cursor"]["fbKey"] == "self"
    assert feedback.json()["cursor"]["fbSeq"] == feedback.json()["wseq"]
    assert feedback.json()["idempotent"] is False

    legacy_feedback = client.post("/sessions/S-AI/interactions", json={
        "event_type": "feedback_selected",
        "attempt_id": attempt_id,
        "feedback_key": "correct",
    })
    assert legacy_feedback.status_code == 409, legacy_feedback.text
    assert legacy_feedback.json()["detail"]["code"] == (
        "interaction_presentation_atomic_required")
    assert client.post("/sessions/S-AI/interactions", json={
        "event_type": "feedback_selected", "attempt_id": attempt_id,
    }).status_code == 422
    assert client.post("/sessions/S-AI/interactions", json={
        "event_type": "feedback_selected", "attempt_id": attempt_id,
        "feedback_key": "correct", "error_code": "wrong_event_field",
    }).status_code == 422
    assert client.post("/sessions/S-AI/interactions", json={
        "event_type": "feedback_selected", "attempt_id": attempt_id,
        "feedback_key": "correct", "interests": "书法",
    }).status_code == 422
    assert client.post("/sessions/S-AI/interactions", json={
        "event_type": "researcher_takeover", "attempt_id": attempt_id,
        "reason_code": "stuck",
    }).status_code == 409

    with Session(engine) as session:
        runtime = session.get(SessionRuntimeState, "S-AI")
        assert runtime is not None
        runtime.status = "completed"
        runtime.revision += 1
        session.add(runtime)
        session.commit()

    # 同一幂等键只读取回，不新增事件；新音频/人工事件都被终态拦截。
    retry = client.post(
        "/sessions/S-AI/attempts/process", json=_body("done-before-terminal"))
    assert retry.status_code == 200 and retry.json()["idempotent"] is True
    assert client.post(
        "/sessions/S-AI/attempts/process", json=_body("new-after-terminal")).status_code == 409
    assert client.post("/sessions/S-AI/interactions", json={
        "event_type": "technical_pause", "error_code": "device_error",
    }).status_code == 409
    assert client.get("/sessions/S-AI/attempts").status_code == 200


def test_deidentified_export_includes_safe_attempt_and_interaction_ledgers(
        client_db, monkeypatch, tmp_path):
    client, engine = client_db
    _seed_session(client)
    _seed_audio(client, "identifier-attempt", contains_identifier=True)
    fake_asr, fake_judge = _FakeAsr("我叫张三"), _FakeJudge()
    _install_engines(monkeypatch, fake_asr, fake_judge)
    processed = client.post(
        "/sessions/S-AI/attempts/process", json=_body("identifier-attempt"))
    assert processed.status_code == 200

    with Session(engine) as session:
        train_session = session.get(TrainSession, "S-AI")
        assert train_session is not None
        intervention_completed_at = datetime(2026, 7, 19, 9, 0, 0)
        completed_at = intervention_completed_at + timedelta(minutes=5)
        actor = "TEST-EXPORTER"
        session.add(SessionRuntimeState(
            session_id="S-AI", status="completed", revision=2,
            intervention_completed_at=intervention_completed_at,
            completed_at=completed_at, ended_by=actor,
            end_reason="completion_gate_passed"))
        session.add(SessionOutcomeSummary(
            session_id="S-AI",
            schema_version="session-outcome-summary.v1",
            generator_version="test-export-closeout.v1",
            item_bank_version_id=train_session.item_bank_version_id,
            is_simulation=True,
            data_classification="simulation",
            expected_turns=1,
            matched_turns=1,
            completed_attempt_turns=1,
            audio_evidenced_turns=1,
            total_attempts=1,
            completed_attempts=1,
            needs_review_attempts=0,
            technical_failure_attempts=0,
            prompt_level_0_count=1,
            prompt_level_1_count=0,
            prompt_level_2_count=0,
            prompt_level_3_count=0,
            technical_pause_count=0,
            researcher_takeover_count=0,
            source_digest="c" * 64,
            generated_at=intervention_completed_at - timedelta(seconds=1),
        ))
        session.add(SessionCloseoutReport(
            session_id="S-AI",
            schema_version="session-closeout.v1",
            status="no_additional_observation",
            revision=2,
            last_idempotency_key="test-export-closeout",
            last_request_hash="d" * 64,
            created_by=actor,
            created_at=intervention_completed_at + timedelta(minutes=1),
            updated_by=actor,
            updated_at=completed_at,
            locked_by=actor,
            locked_at=completed_at,
        ))
        session.commit()
        result = export.export_session_bundle(
            session, "S-AI", deidentify=True, write_dir=tmp_path,
            idempotency_key="attempt-ledger-export-0123456789abcdef0123456789abcdef",
            actor_display_id="TEST-DATA-STEWARD", actor_role="data_steward")
    attempts = result["sheets"]["attempts"]
    interactions = result["sheets"]["interactions"]
    assert attempts[0]["asr_text"] == export_security.REDACTED_TEXT
    assert attempts[0]["judge_reason"] == export_security.REDACTED_TEXT
    assert attempts[0]["audio_code"] == export_security.pseudonymize_audio(
        "identifier-attempt")
    assert "raw_audio_id" not in attempts[0]
    assert "session_id" not in attempts[0]
    assert all("attempt_id" not in row and "session_id" not in row
               for row in interactions)
    assert all("created_at" not in row and "processed_at" not in row
               for row in attempts + interactions)
    assert all(row["audio_code"] == attempts[0]["audio_code"]
               for row in interactions)
    assert "identifier-attempt" not in str(interactions)
    assert "raw_audio_id" not in str(interactions)
    assert "S-AI" not in str(result["sheets"])
    assert "P-AI" not in str(result["sheets"])
    assert attempts[0]["truth_scope"] == "operational_only"
    assert attempts[0]["judge_portrait_used"] is False
    assert "我叫张三" not in str(interactions)
    assert "zodiac" not in str(interactions)
    assert {row["event_type"] for row in interactions} == {
        "attempt_received", "asr_completed", "judgement_completed",
    }
