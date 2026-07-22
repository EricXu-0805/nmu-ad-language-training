from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app import asr, auth, db, provider_readiness
from app.enums import AnswerType
from app.llm_judge import LlmJudgement
from app.main import app
from app.models import AuditLog, Patient, ProviderReadinessProbe, ResearchUser


class FakeTts:
    version = "fake-tts/model-v1"
    cache_params = "voice=synthetic"
    cloud = False
    data_boundary = "local"
    provider_id = None

    def __init__(self, *, valid: bool = True):
        self.valid = valid
        self.calls: list[str] = []

    def synthesize(self, text: str):
        self.calls.append(text)
        return b"RIFF" + (b"\x00" * 80) if self.valid else None


class FakeAsr:
    version = "fake-asr/model-v2"
    data_boundary = "local"
    provider_id = None

    def __init__(self, *, text: str | None = "您好"):
        self.text = text
        self.calls: list[tuple[bytes, tuple[str, ...]]] = []

    def transcribe(self, audio_bytes: bytes, hotwords):
        self.calls.append((audio_bytes, tuple(hotwords)))
        return asr.AsrResult(
            asr_text=self.text,
            asr_confidence=None,
            engine_version=self.version,
        )


class FakeLlm:
    version = "fake-llm/model-v3"
    data_boundary = "local"
    provider_id = None

    def __init__(self, *, valid: bool = True):
        self.valid = valid
        self.calls = []

    def judge(self, judge_input):
        self.calls.append(judge_input)
        if not self.valid:
            return None
        return LlmJudgement(
            answer_type=AnswerType.正确,
            ai_score=1.0,
            ai_needs_review=False,
            reason="synthetic",
        )


def _configuration(*, tts_engine=None, asr_engine=None, llm_engine=None):
    return provider_readiness.capture_configuration(
        tts_engine=tts_engine or FakeTts(),
        asr_engine=asr_engine or FakeAsr(),
        llm_engine=llm_engine or FakeLlm(),
    )


def test_configuration_fingerprint_detects_key_rotation_without_storing_key(monkeypatch):
    engines = (FakeTts(), FakeAsr(), FakeLlm())
    monkeypatch.setenv(
        provider_readiness.CREDENTIAL_FINGERPRINT_KEY_ENV,
        "readiness-fingerprint-secret-32-bytes-minimum",
    )
    monkeypatch.setenv("DASHSCOPE_API_KEY", "first-secret-value")
    first = provider_readiness.capture_configuration(
        tts_engine=engines[0], asr_engine=engines[1], llm_engine=engines[2])
    monkeypatch.setenv("DASHSCOPE_API_KEY", "rotated-secret-value")
    rotated = provider_readiness.capture_configuration(
        tts_engine=engines[0], asr_engine=engines[1], llm_engine=engines[2])
    monkeypatch.delenv("DASHSCOPE_API_KEY")
    absent = provider_readiness.capture_configuration(
        tts_engine=engines[0], asr_engine=engines[1], llm_engine=engines[2])

    assert first.fingerprint != rotated.fingerprint
    assert first.fingerprint != absent.fingerprint
    assert "secret" not in repr(first)


def test_cloud_key_without_fingerprint_secret_fails_before_provider_calls(monkeypatch):
    fake_tts, fake_asr, fake_llm = FakeTts(), FakeAsr(), FakeLlm()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "configured-provider-key")
    monkeypatch.delenv(
        provider_readiness.CREDENTIAL_FINGERPRINT_KEY_ENV, raising=False)
    config = provider_readiness.capture_configuration(
        tts_engine=fake_tts, asr_engine=fake_asr, llm_engine=fake_llm)

    result = provider_readiness.run_synthetic_probe(config)

    assert result.required_capabilities_ready is False
    assert result.probe_failure_code == "provider_credential_fingerprint_key_missing"
    assert fake_tts.calls == []
    assert fake_asr.calls == []
    assert fake_llm.calls == []


@pytest.mark.parametrize("fingerprint_key", [
    "too-short",
    "x" * 64,
    "abcdefghijkl" * 3,
    " configured-secret-with-adequate-variety-123456789 ",
])
def test_invalid_fingerprint_secret_fails_before_provider_calls(
        monkeypatch, fingerprint_key):
    fake_tts, fake_asr, fake_llm = FakeTts(), FakeAsr(), FakeLlm()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "configured-provider-key")
    monkeypatch.setenv(
        provider_readiness.CREDENTIAL_FINGERPRINT_KEY_ENV, fingerprint_key)

    result = provider_readiness.run_synthetic_probe(
        provider_readiness.capture_configuration(
            tts_engine=fake_tts, asr_engine=fake_asr, llm_engine=fake_llm))

    assert result.probe_failure_code == "provider_credential_fingerprint_key_invalid"
    assert fake_tts.calls == []
    assert fake_asr.calls == []
    assert fake_llm.calls == []


@pytest.mark.parametrize("reused_env", [
    "DASHSCOPE_API_KEY", "DEIDENTIFICATION_KEY", "CONSOLE_PIN",
])
def test_reused_fingerprint_secret_fails_closed(monkeypatch, reused_env):
    reused = "independent-secret-must-not-be-reused-1234567890"
    monkeypatch.setenv("DASHSCOPE_API_KEY", "configured-provider-key")
    monkeypatch.setenv(reused_env, reused)
    if reused_env == "DASHSCOPE_API_KEY":
        monkeypatch.setenv("DASHSCOPE_API_KEY", reused)
    monkeypatch.setenv(provider_readiness.CREDENTIAL_FINGERPRINT_KEY_ENV, reused)

    config = provider_readiness.capture_configuration(
        tts_engine=FakeTts(), asr_engine=FakeAsr(), llm_engine=FakeLlm())

    assert config.credential_fingerprint_ready is False
    assert config.credential_failure_code == "provider_credential_fingerprint_key_reused"


def test_no_cloud_key_local_probe_needs_no_fingerprint_secret(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv(
        provider_readiness.CREDENTIAL_FINGERPRINT_KEY_ENV, raising=False)

    result = provider_readiness.run_synthetic_probe(_configuration())

    assert result.required_capabilities_ready is True
    assert result.probe_failure_code is None


def test_synthetic_probe_chains_fixed_tts_audio_into_asr_and_validates_llm():
    fake_tts, fake_asr, fake_llm = FakeTts(), FakeAsr(), FakeLlm()
    result = provider_readiness.run_synthetic_probe(_configuration(
        tts_engine=fake_tts, asr_engine=fake_asr, llm_engine=fake_llm))

    assert result.required_capabilities_ready is True
    assert result.all_configured_capabilities_ready is True
    assert fake_tts.calls == [provider_readiness.PROBE_TEXT]
    assert len(fake_asr.calls) == 1
    assert fake_asr.calls[0][0].startswith(b"RIFF")
    assert fake_asr.calls[0][1] == (provider_readiness.PROBE_TEXT,)
    assert len(fake_llm.calls) == 1
    assert fake_llm.calls[0].item_id == "synthetic-provider-probe"
    assert not hasattr(fake_llm.calls[0], "patient_id")


def test_optional_llm_failure_is_explicit_and_never_mixed_into_all_ready():
    result = provider_readiness.run_synthetic_probe(_configuration(
        llm_engine=FakeLlm(valid=False)))

    assert result.llm.required is False
    assert result.llm.success is False
    assert result.llm.failure_code == "llm_result_empty"
    assert result.required_capabilities_ready is True
    assert result.all_configured_capabilities_ready is False


def test_projection_fails_closed_for_expiry_and_config_change(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'readiness.db'}")
    SQLModel.metadata.create_all(engine)
    now = datetime(2026, 7, 19, 10, 0, 0)
    config = _configuration()
    result = provider_readiness.run_synthetic_probe(config)
    with Session(engine) as session:
        provider_readiness.persist_probe(
            session, result=result, actor_display_id="ADMIN-1", checked_at=now)
        session.commit()
        fresh = provider_readiness.readiness_projection(
            session, configuration=config, now=now + timedelta(minutes=1))
        assert fresh.status == "ready"
        assert fresh.start_allowed is True

        expired = provider_readiness.readiness_projection(
            session, configuration=config, now=now + timedelta(hours=2))
        assert expired.status == "expired"
        assert expired.start_allowed is False

        changed_config = _configuration(asr_engine=FakeAsr())
        changed_config = replace(
            changed_config,
            fingerprint="f" * 64,
            asr_engine_version="fake-asr/model-v4",
        )
        mismatch = provider_readiness.readiness_projection(
            session, configuration=changed_config,
            now=now + timedelta(minutes=1))
        assert mismatch.status == "config_mismatch"
        assert mismatch.start_allowed is False


def test_persisted_ready_probe_becomes_mismatch_after_api_key_rotation(
        monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'key-rotation.db'}")
    SQLModel.metadata.create_all(engine)
    engines = (FakeTts(), FakeAsr(), FakeLlm())
    monkeypatch.setenv(
        provider_readiness.CREDENTIAL_FINGERPRINT_KEY_ENV,
        "independent-readiness-secret-with-variety-123456789",
    )
    monkeypatch.setenv("DASHSCOPE_API_KEY", "first-provider-key")
    first = provider_readiness.capture_configuration(
        tts_engine=engines[0], asr_engine=engines[1], llm_engine=engines[2])
    result = provider_readiness.run_synthetic_probe(first)
    now = datetime(2026, 7, 19, 10, 0, 0)
    with Session(engine) as session:
        provider_readiness.persist_probe(
            session, result=result, actor_display_id="ADMIN-1", checked_at=now)
        session.commit()
        assert provider_readiness.readiness_projection(
            session, configuration=first, now=now).status == "ready"

        monkeypatch.setenv("DASHSCOPE_API_KEY", "rotated-provider-key")
        rotated = provider_readiness.capture_configuration(
            tts_engine=engines[0], asr_engine=engines[1], llm_engine=engines[2])
        mismatch = provider_readiness.readiness_projection(
            session, configuration=rotated, now=now)
        assert mismatch.status == "config_mismatch"
        assert mismatch.start_allowed is False


def test_probe_ledger_schema_has_no_sensitive_payload_columns():
    forbidden = {
        "api_key", "key_hash", "audio", "audio_bytes", "transcript", "text",
        "answer", "patient_id", "session_id", "turn_id", "raw_audio_id",
    }
    assert forbidden.isdisjoint(ProviderReadinessProbe.__table__.columns.keys())


@pytest.fixture
def readiness_api(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'readiness-api.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    now = datetime.now()
    with Session(engine) as session:
        session.add_all([
            ResearchUser(
                username="probe-admin", display_id="PROBE-ADMIN",
                password_hash=auth.hash_password("password1"), role="admin",
                created_at=now,
            ),
            ResearchUser(
                username="probe-researcher", display_id="PROBE-RESEARCHER",
                password_hash=auth.hash_password("password1"), role="researcher",
                created_at=now,
            ),
            ResearchUser(
                username="probe-steward", display_id="PROBE-STEWARD",
                password_hash=auth.hash_password("password1"), role="data_steward",
                created_at=now,
            ),
        ])
        session.commit()
    config = _configuration()
    monkeypatch.setattr(
        provider_readiness, "capture_configuration", lambda **_kwargs: config)
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "135790")

    clients = {}
    for username in ("probe-admin", "probe-researcher", "probe-steward"):
        client = TestClient(app)
        login = client.post("/auth/login", json={
            "username": username, "password": "password1",
        })
        assert login.status_code == 200, login.text
        csrf = client.cookies.get(auth.CSRF_COOKIE_NAME)
        assert csrf
        client.headers["X-CSRF-Token"] = csrf
        clients[username] = client
    try:
        yield clients, engine
    finally:
        for client in clients.values():
            client.close()


def test_admin_runs_synthetic_probe_and_other_accounts_are_read_only(readiness_api):
    clients, engine = readiness_api
    researcher = clients["probe-researcher"]
    steward = clients["probe-steward"]
    admin = clients["probe-admin"]

    assert researcher.get("/ai/provider-readiness").json()["status"] == "missing"
    assert steward.get("/ai/provider-readiness").status_code == 200
    denied = researcher.post("/ai/provider-readiness/probe")
    assert denied.status_code == 403
    assert denied.json()["code"] == "role_forbidden"

    probed = admin.post("/ai/provider-readiness/probe")
    assert probed.status_code == 200, probed.text
    body = probed.json()
    assert body["start_allowed"] is True
    assert body["actor_display_id"] == "PROBE-ADMIN"
    assert body["tts"]["engine_version"] == "fake-tts/model-v1"
    assert "您好" not in probed.text
    assert "config_fingerprint" not in body
    assert "audio" not in ProviderReadinessProbe.__table__.columns

    with Session(engine) as session:
        rows = list(session.exec(select(ProviderReadinessProbe)))
        assert len(rows) == 1
        assert rows[0].actor_display_id == "PROBE-ADMIN"
        assert list(session.exec(select(Patient))) == []


def test_probe_receipt_commits_before_independent_audit_writer(
        readiness_api, monkeypatch):
    clients, engine = readiness_api
    observed_outer_transactions: list[bool] = []

    from app import audit as audit_module
    original_record = audit_module.record

    def observing_record(session, **kwargs):
        observed_outer_transactions.append(session.in_transaction())
        return original_record(session, **kwargs)

    monkeypatch.setattr(audit_module, "record", observing_record)
    response = clients["probe-admin"].post("/ai/provider-readiness/probe")

    assert response.status_code == 200, response.text
    assert observed_outer_transactions == [False]
    with Session(engine) as session:
        assert len(list(session.exec(select(ProviderReadinessProbe)))) == 1
        rows = list(session.exec(select(AuditLog).where(
            AuditLog.action == "provider_readiness_probe")))
        assert len(rows) == 1
        assert rows[0].actor == "PROBE-ADMIN"


def test_provider_configuration_change_during_probe_is_ledgered_and_blocked(
        readiness_api, monkeypatch):
    clients, engine = readiness_api
    before = _configuration()
    after = replace(before, fingerprint="b" * 64)
    captures = iter((before, after))
    monkeypatch.setattr(
        provider_readiness, "capture_configuration", lambda **_kwargs: next(captures))

    response = clients["probe-admin"].post("/ai/provider-readiness/probe")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "config_mismatch"
    assert response.json()["start_allowed"] is False
    with Session(engine) as session:
        row = provider_readiness.latest_probe(session)
        assert row is not None
        assert row.required_capabilities_ready is False
        assert row.probe_failure_code == "provider_config_changed_during_probe"
