from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import threading

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

from app import asr, audio_store, auth, cloud_processing, db, llm_judge
from app.enums import AnswerType
from app.llm_judge import LlmJudgement
from app.main import app
from app.models import (AttemptEvent, AudioAssetRow, AuditLog, Patient,
                        ResearchUser, SessionRuntimeState)


class SpyAsr:
    version = "dashscope/spy-asr"
    data_boundary = "cloud"
    provider_id = "aliyun-dashscope"

    def __init__(self, text_value: str = "锚"):
        self.calls = 0
        self.text_value = text_value

    def transcribe(self, _audio_bytes, _hotwords):
        self.calls += 1
        return asr.AsrResult(self.text_value, 0.9, self.version, hotword_hit=True)


class BlockingSpyAsr(SpyAsr):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def transcribe(self, audio_bytes, hotwords):
        self.calls += 1
        self.started.set()
        assert self.release.wait(timeout=5)
        return asr.AsrResult("锚", 0.9, self.version, hotword_hit=True)


class SpyCloudJudge:
    version = "dashscope/spy-judge"
    data_boundary = "cloud"
    provider_id = "aliyun-dashscope"

    def __init__(self):
        self.calls = 0

    def judge(self, _judge_input):
        self.calls += 1
        return LlmJudgement(AnswerType.正确, 1, False, "cloud")


def _add_user(engine, username="research"):
    with Session(engine) as session:
        session.add(ResearchUser(
            username=username, display_id=f"ACTOR-{username}",
            password_hash=auth.hash_password("password1"), role="researcher",
            created_at=datetime.now(),
        ))
        session.commit()


def _login(client: TestClient, username="research") -> None:
    response = client.post("/auth/login", json={
        "username": username, "password": "password1",
    })
    assert response.status_code == 200, response.text
    token = client.cookies.get(auth.CSRF_COOKIE_NAME)
    assert token
    client.headers.update({"X-CSRF-Token": token})


def _client(tmp_path, monkeypatch) -> TestClient:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cloud-consent.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    _add_user(engine)
    client = TestClient(app)
    client.test_engine = engine
    _login(client)
    return client


def _precreate_server_audio(
        client: TestClient, *, session_id: str, audio_id: str) -> None:
    """Start fixtures at a complete server-owned immutable audio boundary."""
    audio_bytes = b"\x1aE\xdf\xa3cloud-consent-audio"
    path, checksum = audio_store.save_blob(
        audio_id, audio_bytes, "audio/webm")
    with Session(client.test_engine) as session:
        session.add(AudioAssetRow(
            raw_audio_id=audio_id,
            session_id=session_id,
            turn_key="SE_锚#1",
            is_simulation=True,
            data_classification="simulation",
            audio_format=path.suffix.lstrip("."),
            checksum=checksum,
            byte_count=len(audio_bytes),
            uploaded_at=datetime.now(),
        ))
        session.commit()


def _seed_attempt(client: TestClient, *, patient_id: str, session_id: str,
                  audio_id: str) -> None:
    assert client.post("/patients", json={
        "patient_id": patient_id,
        "is_simulation_subject": True,
    }).status_code == 200
    assert client.post("/sessions", json={
        "session_id": session_id, "patient_id": patient_id, "week_no": 2,
        "phase_type": "正式训练", "event_line": "正式训练",
        "item_bank_version_id": "wk2-v1-20260707", "is_simulation": True,
        "trainer_id": "ACTOR-research",
    }).status_code == 200
    _precreate_server_audio(client, session_id=session_id, audio_id=audio_id)
    assert client.post("/audio", json={
        "raw_audio_id": audio_id, "session_id": session_id,
        "turn_key": "SE_锚#1",
    }).status_code == 200
    assert client.put(
        f"/audio/{audio_id}/blob", content=b"\x1aE\xdf\xa3cloud-consent-audio",
        headers={"content-type": "audio/webm"},
    ).status_code == 200


def _seed_admitted_attempt(
        client: TestClient, *, patient_id: str, audio_id: str) -> str:
    """Create the race fixture through the production VisitPlan admission path."""
    assert client.post("/patients", json={
        "patient_id": patient_id,
        "is_simulation_subject": True,
    }).status_code == 200
    created = client.post("/visit-plans", json={
        "idempotency_key": f"plan-create-{patient_id.lower()}",
        "patient_id": patient_id,
        "scheduled_date": datetime.now().date().isoformat(),
        "queue_order": 0,
        "session_sitting_no": 1,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
    })
    assert created.status_code == 200, created.text
    plan = created.json()
    plan_id = plan["plan_id"]
    approved = client.post(f"/visit-plans/{plan_id}/approve", json={
        "idempotency_key": f"plan-approve-{patient_id.lower()}",
        "expected_revision": plan["revision"],
    })
    assert approved.status_code == 200, approved.text
    started = client.post(f"/visit-plans/{plan_id}/start", json={
        "idempotency_key": f"plan-start-{patient_id.lower()}",
        "expected_revision": approved.json()["revision"],
    })
    assert started.status_code == 200, started.text
    session_id = started.json()["session_id"]
    assert session_id
    _precreate_server_audio(client, session_id=session_id, audio_id=audio_id)
    assert client.post("/audio", json={
        "raw_audio_id": audio_id,
        "session_id": session_id,
        "turn_key": "SE_锚#1",
    }).status_code == 200
    assert client.put(
        f"/audio/{audio_id}/blob", content=b"\x1aE\xdf\xa3cloud-consent-audio",
        headers={"content-type": "audio/webm"},
    ).status_code == 200
    return session_id


def _process(client: TestClient, session_id: str, audio_id: str):
    return client.post(f"/sessions/{session_id}/attempts/process", json={
        "item_id": "SE_锚", "turn_seq": 1, "response_role": "命名",
        "raw_audio_id": audio_id, "prompt_level": 0, "duration_seconds": 1,
    })


def _configure_policy(monkeypatch, notice="notice-2026-01"):
    monkeypatch.setenv(cloud_processing.PROVIDER_ID_ENV, "aliyun-dashscope")
    monkeypatch.setenv(cloud_processing.NOTICE_VERSION_ENV, notice)


def _cloud_authorization_body(
        client: TestClient, patient_id: str, allowed: bool) -> dict:
    patient = client.get(f"/patients/{patient_id}")
    assert patient.status_code == 200, patient.text
    snapshot = patient.json()
    body = {
        "allowed": allowed,
        "expected": {
            "allowed": snapshot.get("cloud_processing_allowed"),
            "provider_id": snapshot.get("cloud_processing_provider_id"),
            "notice_version": snapshot.get("cloud_processing_notice_version"),
            "consented_at": snapshot.get("cloud_processing_consented_at"),
            "revoked_at": snapshot.get("cloud_processing_revoked_at"),
            "withdrawal_status": snapshot.get("withdrawal_status"),
            "governance_revision": snapshot["governance_revision"],
        },
        "policy_provider_id": None,
        "policy_notice_version": None,
    }
    if allowed:
        policy = client.get("/cloud-processing/policy")
        assert policy.status_code == 200, policy.text
        body["policy_provider_id"] = policy.json()["provider_id"]
        body["policy_notice_version"] = policy.json()["notice_version"]
    return body


def test_legacy_asr_flag_cannot_reopen_unscoped_cloud_call(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    spy = SpyAsr()
    monkeypatch.setattr("app.main.asr.get_engine", lambda: spy)
    monkeypatch.setenv("ALLOW_LEGACY_AI_ENDPOINTS", "1")
    try:
        denied = client.post("/asr/transcribe/arbitrary-audio")
        assert denied.status_code == 409
        assert "永久关闭" in denied.json()["detail"]
        assert spy.calls == 0
    finally:
        client.close()


def test_unconsented_simulation_and_wrong_version_never_call_cloud_asr(
        tmp_path, monkeypatch):
    _configure_policy(monkeypatch)
    client = _client(tmp_path, monkeypatch)
    spy = SpyAsr()
    monkeypatch.setattr("app.main.asr.get_engine", lambda: spy)
    try:
        _seed_attempt(client, patient_id="P-NO-CLOUD", session_id="S-NO-CLOUD",
                      audio_id="A-NO-CLOUD")
        denied = _process(client, "S-NO-CLOUD", "A-NO-CLOUD")
        assert denied.status_code == 200
        assert denied.json()["status"] == "technical_failure"
        assert denied.json()["attempt"]["error_code"] == "cloud_processing_not_authorized"
        assert spy.calls == 0
        with Session(client.test_engine) as session:
            assert session.get(SessionRuntimeState, "S-NO-CLOUD").status == "paused"

        _seed_attempt(client, patient_id="P-STALE", session_id="S-STALE", audio_id="A-STALE")
        with Session(client.test_engine) as session:
            patient = session.get(Patient, "P-STALE")
            patient.cloud_processing_allowed = True
            patient.cloud_processing_provider_id = "aliyun-dashscope"
            patient.cloud_processing_notice_version = "old-notice"
            patient.cloud_processing_consented_at = datetime.now()
            session.add(patient)
            session.commit()
        stale = _process(client, "S-STALE", "A-STALE")
        assert stale.json()["attempt"]["error_code"] == "cloud_processing_not_authorized"
        assert spy.calls == 0
    finally:
        client.close()


def test_server_stamps_policy_and_authorized_cloud_asr_can_run(tmp_path, monkeypatch):
    _configure_policy(monkeypatch)
    client = _client(tmp_path, monkeypatch)
    spy = SpyAsr()
    monkeypatch.setattr("app.main.asr.get_engine", lambda: spy)
    monkeypatch.setenv("LLM_JUDGE", "off")
    try:
        _seed_attempt(client, patient_id="P-YES", session_id="S-YES", audio_id="A-YES")
        granted = client.patch(
            "/patients/P-YES/cloud-processing",
            json=_cloud_authorization_body(client, "P-YES", True))
        assert granted.status_code == 200, granted.text
        body = granted.json()
        assert body["cloud_processing_allowed"] is True
        assert body["cloud_processing_provider_id"] == "aliyun-dashscope"
        assert body["cloud_processing_notice_version"] == "notice-2026-01"
        assert body["cloud_processing_consented_at"]
        assert body["cloud_processing_revoked_at"] is None
        processed = _process(client, "S-YES", "A-YES")
        assert processed.status_code == 200, processed.text
        assert processed.json()["status"] == "completed"
        assert spy.calls == 1
    finally:
        client.close()


def test_revocation_succeeds_without_pausing_historical_orphan_session(
        tmp_path, monkeypatch):
    _configure_policy(monkeypatch)
    client = _client(tmp_path, monkeypatch)
    try:
        _seed_attempt(
            client,
            patient_id="P-ORPHAN-REVOKE",
            session_id="S-ORPHAN-REVOKE",
            audio_id="A-ORPHAN-REVOKE",
        )
        granted = client.patch(
            "/patients/P-ORPHAN-REVOKE/cloud-processing",
            json=_cloud_authorization_body(client, "P-ORPHAN-REVOKE", True),
        )
        assert granted.status_code == 200, granted.text
        revoked = client.patch(
            "/patients/P-ORPHAN-REVOKE/cloud-processing",
            json=_cloud_authorization_body(client, "P-ORPHAN-REVOKE", False),
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["cloud_processing_allowed"] is False
        with Session(client.test_engine) as session:
            runtime_state = session.get(
                SessionRuntimeState, "S-ORPHAN-REVOKE")
            # Revocation must not manufacture a live runtime row for an already
            # isolated historical/direct session.
            assert runtime_state is None
    finally:
        client.close()


def test_provider_without_explicit_boundary_is_fail_closed_and_never_called(
        tmp_path, monkeypatch):
    _configure_policy(monkeypatch)
    client = _client(tmp_path, monkeypatch)

    class UnknownProvider:
        version = "mystery-1"
        calls = 0

        def transcribe(self, _audio_bytes, _hotwords):
            self.calls += 1
            raise AssertionError("unknown provider must never be called")

    unknown = UnknownProvider()
    monkeypatch.setattr("app.main.asr.get_engine", lambda: unknown)
    try:
        _seed_attempt(client, patient_id="P-UNKNOWN", session_id="S-UNKNOWN",
                      audio_id="A-UNKNOWN")
        denied = _process(client, "S-UNKNOWN", "A-UNKNOWN")
        assert denied.status_code == 200
        assert denied.json()["attempt"]["error_code"] == "cloud_provider_boundary_unknown"
        assert unknown.calls == 0
    finally:
        client.close()


def test_revocation_during_asr_stops_cloud_llm_and_pauses_without_study_withdrawal(
        tmp_path, monkeypatch):
    _configure_policy(monkeypatch)
    client = _client(tmp_path, monkeypatch)
    second = TestClient(app)
    _login(second)
    blocking_asr = BlockingSpyAsr()
    cloud_judge = SpyCloudJudge()
    monkeypatch.setattr("app.main.asr.get_engine", lambda: blocking_asr)
    llm_judge.register_engine("consent-race-cloud", cloud_judge)
    monkeypatch.setenv("LLM_JUDGE", "consent-race-cloud")
    try:
        session_id = _seed_admitted_attempt(
            client, patient_id="P-RACE", audio_id="A-RACE")
        assert client.patch(
            "/patients/P-RACE/cloud-processing",
            json=_cloud_authorization_body(client, "P-RACE", True)).status_code == 200
        revoke_body = _cloud_authorization_body(second, "P-RACE", False)
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(_process, client, session_id, "A-RACE")
            assert blocking_asr.started.wait(timeout=5)
            revoke_future = pool.submit(
                second.patch,
                "/patients/P-RACE/cloud-processing",
                json=revoke_body,
            )
            # A successful revocation response is the privacy boundary.  It may
            # not overtake a provider call that already owns the subject egress
            # fence, otherwise the old request could begin/continue egress after
            # the researcher has been told revocation completed.
            assert not revoke_future.done()
            blocking_asr.release.set()
            processed = future.result(timeout=10)
            revoked = revoke_future.result(timeout=10)
            assert revoked.status_code == 200, revoked.text
        # 授权撤回会先把运行时安全暂停；已经在途的云 ASR 返回值不再进入
        # 转写、规则判类或 LLM 链，旧请求必须得到明确冲突而非伪装完成。
        assert processed.status_code == 409, processed.text
        assert processed.json()["detail"] == "场次已暂停，丢弃延迟的 attempt 处理结果"
        assert blocking_asr.calls == 1
        assert cloud_judge.calls == 0
        with Session(client.test_engine) as session:
            patient = session.get(Patient, "P-RACE")
            assert patient.cloud_processing_allowed is False
            assert patient.cloud_processing_revoked_at is not None
            assert patient.withdrawal_status is None
            assert session.get(SessionRuntimeState, session_id).status == "paused"
            attempt = session.exec(select(AttemptEvent).where(
                AttemptEvent.session_id == session_id)).one()
            # The ASR call owned the egress fence before revocation.  Its result
            # may therefore commit before the revoke transaction wins the live
            # fence, but no later cloud boundary or final judgement may run.
            assert attempt.processing_status in {"received", "asr_completed"}
            if attempt.processing_status == "received":
                assert attempt.asr_text is None
                assert attempt.asr_engine_version is None
            else:
                assert attempt.asr_text == "锚"
                assert attempt.asr_engine_version == blocking_asr.version
            assert attempt.operational_answer_type is None
            assert attempt.judge_engine_version is None
    finally:
        second.close()
        client.close()


def test_revocation_that_commits_before_cloud_asr_send_prevents_the_call(
        tmp_path, monkeypatch):
    """Close the authorization-check -> network-send TOCTOU deterministically."""
    _configure_policy(monkeypatch)
    client = _client(tmp_path, monkeypatch)
    second = TestClient(app)
    _login(second)
    spy = SpyAsr()
    monkeypatch.setattr("app.main.asr.get_engine", lambda: spy)
    monkeypatch.setenv("LLM_JUDGE", "off")
    try:
        session_id = _seed_admitted_attempt(
            client,
            patient_id="P-PRECALL-REVOKE",
            audio_id="A-PRECALL-REVOKE",
        )
        granted = client.patch(
            "/patients/P-PRECALL-REVOKE/cloud-processing",
            json=_cloud_authorization_body(
                client, "P-PRECALL-REVOKE", True),
        )
        assert granted.status_code == 200, granted.text
        revoke_body = _cloud_authorization_body(
            second, "P-PRECALL-REVOKE", False)
        original_build_hotwords = asr.build_hotwords
        revocation_statuses: list[int] = []

        def revoke_after_local_preparation(*args, **kwargs):
            revoked = second.patch(
                "/patients/P-PRECALL-REVOKE/cloud-processing",
                json=revoke_body,
            )
            revocation_statuses.append(revoked.status_code)
            return original_build_hotwords(*args, **kwargs)

        monkeypatch.setattr(
            "app.main.asr.build_hotwords", revoke_after_local_preparation)
        processed = _process(
            client, session_id, "A-PRECALL-REVOKE")

        assert revocation_statuses == [200]
        assert spy.calls == 0
        assert processed.status_code == 409, processed.text
        with Session(client.test_engine) as session:
            patient = session.get(Patient, "P-PRECALL-REVOKE")
            assert patient.cloud_processing_allowed is False
            assert patient.cloud_processing_revoked_at is not None
    finally:
        second.close()
        client.close()


def test_revocation_that_commits_before_cloud_llm_send_prevents_the_call(
        tmp_path, monkeypatch):
    """ASR authorization is never reused as permission for the later LLM call."""
    _configure_policy(monkeypatch)
    client = _client(tmp_path, monkeypatch)
    second = TestClient(app)
    _login(second)

    class LocalAsr:
        version = "local/asr-race-fixture"
        data_boundary = "local"
        provider_id = None

        def transcribe(self, _audio_bytes, _hotwords):
            return asr.AsrResult("锚", 0.9, self.version, hotword_hit=True)

    cloud_judge = SpyCloudJudge()
    monkeypatch.setattr("app.main.asr.get_engine", lambda: LocalAsr())
    try:
        session_id = _seed_admitted_attempt(
            client,
            patient_id="P-PRECALL-LLM-REVOKE",
            audio_id="A-PRECALL-LLM-REVOKE",
        )
        granted = client.patch(
            "/patients/P-PRECALL-LLM-REVOKE/cloud-processing",
            json=_cloud_authorization_body(
                client, "P-PRECALL-LLM-REVOKE", True),
        )
        assert granted.status_code == 200, granted.text
        revoke_body = _cloud_authorization_body(
            second, "P-PRECALL-LLM-REVOKE", False)
        revocation_statuses: list[int] = []

        def select_cloud_judge_after_revoke():
            if not revocation_statuses:
                revoked = second.patch(
                    "/patients/P-PRECALL-LLM-REVOKE/cloud-processing",
                    json=revoke_body,
                )
                revocation_statuses.append(revoked.status_code)
            return cloud_judge

        monkeypatch.setattr(
            "app.main.llm_judge.get_engine", select_cloud_judge_after_revoke)
        processed = _process(
            client, session_id, "A-PRECALL-LLM-REVOKE")

        assert revocation_statuses == [200]
        assert cloud_judge.calls == 0
        assert processed.status_code == 409, processed.text
        with Session(client.test_engine) as session:
            attempt = session.exec(select(AttemptEvent).where(
                AttemptEvent.session_id == session_id)).one()
            assert attempt.processing_status == "asr_completed"
            assert attempt.asr_text == "锚"
            assert attempt.operational_answer_type is None
            assert attempt.judge_engine_version is None
    finally:
        second.close()
        client.close()


def test_patch_requires_named_account_rejects_forged_version_and_audits_metadata(
        tmp_path, monkeypatch):
    _configure_policy(monkeypatch)
    client = _client(tmp_path, monkeypatch)
    try:
        assert client.post("/patients", json={
            "patient_id": "P-PATCH", "is_simulation_subject": True,
            "cloud_processing_allowed": True,
        }).status_code == 422
        assert client.post("/patients", json={
            "patient_id": "P-PATCH", "is_simulation_subject": True,
        }).status_code == 200

        anonymous = TestClient(app)
        pin = TestClient(app)
        monkeypatch.setenv("CONSOLE_PIN", "246810")
        try:
            assert anonymous.patch(
                "/patients/P-PATCH/cloud-processing", json={"allowed": True}).status_code == 401
            denied_pin = pin.patch(
                "/patients/P-PATCH/cloud-processing", json={
                    "allowed": True, "provider_id": "forged",
                }, headers={"X-Console-Pin": "246810"})
            assert denied_pin.status_code == 401
        finally:
            anonymous.close()
            pin.close()

        forged = client.patch("/patients/P-PATCH/cloud-processing", json={
            "allowed": True, "provider_id": "forged", "notice_version": "forged",
        })
        assert forged.status_code == 422
        assert client.patch(
            "/patients/P-PATCH/cloud-processing",
            json=_cloud_authorization_body(client, "P-PATCH", True)).status_code == 200
        assert client.patch(
            "/patients/P-PATCH/cloud-processing",
            json=_cloud_authorization_body(client, "P-PATCH", False)).status_code == 200
        with Session(client.test_engine) as session:
            rows = list(session.exec(select(AuditLog).where(
                AuditLog.patient_id == "P-PATCH").order_by(AuditLog.id)))
            assert [row.action for row in rows] == [
                "cloud_processing_consent_granted", "cloud_processing_consent_revoked"]
            assert all("回答" not in row.summary and "声纹" not in row.summary for row in rows)
    finally:
        client.close()


def test_patch_rejects_stale_authorization_and_policy_snapshots(tmp_path, monkeypatch):
    _configure_policy(monkeypatch)
    client = _client(tmp_path, monkeypatch)
    try:
        assert client.post("/patients", json={
            "patient_id": "P-CAS", "is_simulation_subject": True,
        }).status_code == 200
        stale = _cloud_authorization_body(client, "P-CAS", True)
        first = client.patch("/patients/P-CAS/cloud-processing", json=stale)
        assert first.status_code == 200, first.text

        changed = client.patch("/patients/P-CAS/cloud-processing", json={
            **stale, "allowed": False,
            "policy_provider_id": None, "policy_notice_version": None,
        })
        assert changed.status_code == 409, changed.text
        assert changed.json()["detail"]["code"] == "cloud_processing_state_changed"

        current = _cloud_authorization_body(client, "P-CAS", True)
        current["policy_notice_version"] = "superseded-notice"
        policy_changed = client.patch("/patients/P-CAS/cloud-processing", json=current)
        assert policy_changed.status_code == 409, policy_changed.text
        assert policy_changed.json()["detail"]["code"] == "cloud_processing_policy_changed"
    finally:
        client.close()


def test_cloud_consent_migration_preserves_existing_patient_as_unauthorized(
        tmp_path, monkeypatch):
    db_path = tmp_path / "cloud-consent-migration.db"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "b9c6e4f2d013")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO patient (patient_id, is_simulation_subject) VALUES ('P-OLD', 0)"))
    command.upgrade(config, "c2e8a4d7f901")
    columns = {column["name"] for column in inspect(engine).get_columns("patient")}
    assert {
        "cloud_processing_allowed", "cloud_processing_provider_id",
        "cloud_processing_notice_version", "cloud_processing_consented_at",
        "cloud_processing_revoked_at",
    } <= columns
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT patient_id, cloud_processing_allowed, cloud_processing_provider_id, "
            "cloud_processing_notice_version, cloud_processing_consented_at, "
            "cloud_processing_revoked_at FROM patient WHERE patient_id='P-OLD'"
        )).mappings().one()
    assert row["patient_id"] == "P-OLD"
    assert all(row[key] is None for key in row.keys() if key != "patient_id")
