from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import json

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


def test_projection_reports_required_capability_failure_on_a_current_config(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'required-failure.db'}")
    SQLModel.metadata.create_all(engine)
    now = datetime(2026, 7, 19, 10, 0, 0)
    config = _configuration(tts_engine=FakeTts(valid=False))
    result = provider_readiness.run_synthetic_probe(config)
    with Session(engine) as session:
        provider_readiness.persist_probe(
            session, result=result, actor_display_id="ADMIN-1", checked_at=now)
        session.commit()
        projection = provider_readiness.readiness_projection(
            session, configuration=config, now=now + timedelta(minutes=1))

    # The configuration never changed and the row is still fresh, so the failed
    # required capability is the only thing left that can refuse the start.
    assert projection.schema_version == "provider-readiness.v2"
    assert projection.matches_current_config is True
    assert projection.status == "required_capability_failed"
    assert projection.start_allowed is False
    assert projection.tts.success is False
    assert projection.tts.failure_code == "tts_audio_invalid"
    assert projection.required_capabilities_ready is False


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
    monkeypatch.setenv("CONSOLE_PIN", "13579024")

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


class _FakeQwenAutopilotEngine:
    version = "dashscope/qwen3-tts-flash/Serena"
    cloud = True

    def __init__(self, *, speech_rate: float = 0.9):
        self.cache_params = f"speech_rate={speech_rate}"
        self.calls: list[str] = []

    def synthesize(self, text: str):
        self.calls.append(text)
        return b"RIFF" + (b"\x00" * 80)


class _FakeGenericPiperEngine:
    version = "piper/zh_CN-huayan-medium"
    cache_params = "length_scale=1.15"
    cloud = False

    def synthesize(self, text: str):
        raise AssertionError(
            "generic engine must never be probed for the exact autopilot channel")


def test_default_configuration_resolves_autopilot_engine_not_generic(monkeypatch):
    autopilot_calls: list[int] = []
    generic_calls: list[int] = []
    qwen = _FakeQwenAutopilotEngine()

    def fake_autopilot_engine():
        autopilot_calls.append(1)
        return qwen

    def fake_generic_engine():
        generic_calls.append(1)
        return _FakeGenericPiperEngine()

    monkeypatch.setattr(provider_readiness.tts, "get_autopilot_engine", fake_autopilot_engine)
    monkeypatch.setattr(provider_readiness.tts, "engine", fake_generic_engine)

    config = provider_readiness.capture_configuration(
        asr_engine=FakeAsr(), llm_engine=FakeLlm())

    assert autopilot_calls == [1]
    assert generic_calls == []
    assert config.tts_engine_version == "dashscope/qwen3-tts-flash/Serena"


def test_explicit_tts_injection_bypasses_both_resolvers(monkeypatch):
    def _must_not_resolve():
        raise AssertionError("resolver must not run when tts_engine is injected")

    monkeypatch.setattr(provider_readiness.tts, "get_autopilot_engine", _must_not_resolve)
    monkeypatch.setattr(provider_readiness.tts, "engine", _must_not_resolve)

    config = provider_readiness.capture_configuration(
        tts_engine=FakeTts(), asr_engine=FakeAsr(), llm_engine=FakeLlm())

    assert config.tts_engine_version == FakeTts.version


# A frozen literal, deliberately NOT derived from provider_readiness.
# RUNTIME_CONTRACT.  An implementation that wrongly rewrites the response
# runtime_contract to the current constant must fail against this value.
LEGACY_RUNTIME_SENTINEL = "legacy_runtime_contract_v0_frozen_sentinel"

PROJECTION_KEYS = {
    "schema_version", "runtime_contract", "status", "start_allowed",
    "required_capabilities_ready", "all_configured_capabilities_ready",
    "matches_current_config", "tts", "asr", "llm",
    "checked_at", "expires_at", "actor_display_id", "probe_failure_code",
}
CAPABILITY_KEYS = {
    "required", "configured", "success", "engine_version", "failure_code"}


def _row_snapshot(engine, probe_id: str) -> dict:
    """Every scalar column, read through a brand-new Session.

    Re-reading inside the projecting session would hand back the same
    identity-mapped object and could not detect a rewrite at all.
    """
    with Session(engine) as reader:
        row = reader.exec(select(ProviderReadinessProbe).where(
            ProviderReadinessProbe.probe_id == probe_id)).one()
        return {
            name: getattr(row, name)
            for name in ProviderReadinessProbe.__table__.columns.keys()
        }


def _insert_legacy_row(session, *, probe_id, result, now,
                       schema_version, runtime_contract):
    """ProviderReadinessProbe is append-only, so a legacy row is a fresh INSERT.

    Every field except the two under test copies what a real probe would have
    produced, so nothing but schema/runtime can explain a refusal.
    """
    row = ProviderReadinessProbe(
        probe_id=probe_id,
        schema_version=schema_version,
        runtime_contract=runtime_contract,
        config_fingerprint=result.configuration.fingerprint,
        tts_engine_version=result.configuration.tts_engine_version,
        asr_engine_version=result.configuration.asr_engine_version,
        llm_engine_version=result.configuration.llm_engine_version,
        tts_required=result.tts.required, tts_success=result.tts.success,
        tts_failure_code=result.tts.failure_code,
        asr_required=result.asr.required, asr_success=result.asr.success,
        asr_failure_code=result.asr.failure_code,
        llm_required=result.llm.required,
        llm_configured=result.configuration.llm_configured,
        llm_success=result.llm.success, llm_failure_code=result.llm.failure_code,
        required_capabilities_ready=result.required_capabilities_ready,
        all_configured_capabilities_ready=result.all_configured_capabilities_ready,
        probe_failure_code=result.probe_failure_code,
        checked_at=now, expires_at=now + timedelta(minutes=30),
        actor_display_id="ADMIN-1",
    )
    session.add(row)
    session.commit()
    return row


def test_no_row_projection_reports_current_schema_and_blocks_start(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'no-row.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        projection = provider_readiness.readiness_projection(
            session, configuration=_configuration())

    assert projection.schema_version == "provider-readiness.v2"
    assert projection.status == "missing"
    assert projection.start_allowed is False
    assert projection.matches_current_config is False


def test_current_row_projects_current_schema_and_allows_start(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'current-row.db'}")
    SQLModel.metadata.create_all(engine)
    now = datetime(2026, 7, 19, 10, 0, 0)
    config = _configuration()
    result = provider_readiness.run_synthetic_probe(config)
    with Session(engine) as session:
        provider_readiness.persist_probe(
            session, result=result, actor_display_id="ADMIN-1", checked_at=now)
        session.commit()
        projection = provider_readiness.readiness_projection(
            session, configuration=config, now=now + timedelta(minutes=1))

    assert projection.schema_version == "provider-readiness.v2"
    assert projection.status == "ready"
    assert projection.matches_current_config is True
    assert projection.start_allowed is True


@pytest.mark.parametrize(
    "probe_id,runtime_contract,expected_runtime",
    [
        # Runtime contract forged equal to the current one: only the schema
        # version differs, so only the schema rule can explain the refusal.
        ("prb_legacy_current_runtime", None, None),
        # A genuinely old runtime contract must survive into the response
        # verbatim rather than being rewritten to today's constant.
        ("prb_legacy_sentinel_runtime", LEGACY_RUNTIME_SENTINEL,
         LEGACY_RUNTIME_SENTINEL),
    ],
)
def test_legacy_row_projects_as_v2_config_mismatch_without_any_rewrite(
        tmp_path, probe_id, runtime_contract, expected_runtime):
    engine = create_engine(f"sqlite:///{tmp_path / f'{probe_id}.db'}")
    SQLModel.metadata.create_all(engine)
    now = datetime(2026, 7, 19, 10, 0, 0)
    config = _configuration()
    result = provider_readiness.run_synthetic_probe(config)
    contract = runtime_contract or result.configuration.runtime_contract
    with Session(engine) as session:
        _insert_legacy_row(
            session, probe_id=probe_id, result=result, now=now,
            schema_version="provider-readiness.v1", runtime_contract=contract)

    before = _row_snapshot(engine, probe_id)
    assert before["schema_version"] == "provider-readiness.v1"
    assert before["runtime_contract"] == contract

    with Session(engine) as session:
        projection = provider_readiness.readiness_projection(
            session, configuration=config, now=now + timedelta(minutes=1))
        # The projecting session must not have staged a single change.
        assert not session.dirty
        assert not session.new
        assert not session.deleted

    # Response schema is the current constant even though the row is v1.
    assert projection.schema_version == "provider-readiness.v2"
    assert projection.runtime_contract == (
        expected_runtime if expected_runtime is not None else contract)
    assert projection.status == "config_mismatch"
    assert projection.matches_current_config is False
    assert projection.start_allowed is False

    after = _row_snapshot(engine, probe_id)
    assert after == before
    with Session(engine) as reader:
        assert len(list(reader.exec(select(ProviderReadinessProbe)))) == 1


def test_projection_keys_are_exact_and_carry_no_secret_or_payload(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'keys.db'}")
    SQLModel.metadata.create_all(engine)
    now = datetime(2026, 7, 19, 10, 0, 0)
    config = _configuration()
    result = provider_readiness.run_synthetic_probe(config)
    with Session(engine) as session:
        provider_readiness.persist_probe(
            session, result=result, actor_display_id="ADMIN-1", checked_at=now)
        session.commit()
        payload = provider_readiness.readiness_projection(
            session, configuration=config,
            now=now + timedelta(minutes=1)).model_dump(mode="json")

    assert set(payload) == PROJECTION_KEYS
    for capability in ("tts", "asr", "llm"):
        assert set(payload[capability]) == CAPABILITY_KEYS
    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden in (
        "config_fingerprint", "api_key", "audio", "transcript",
        provider_readiness.PROBE_TEXT, config.fingerprint,
    ):
        assert forbidden not in serialized


def test_http_projection_reports_current_schema_for_missing_and_ready(
        readiness_api):
    clients, _engine = readiness_api
    admin = clients["probe-admin"]

    missing = admin.get("/ai/provider-readiness")
    assert missing.status_code == 200, missing.text
    assert missing.json()["schema_version"] == "provider-readiness.v2"
    assert missing.json()["status"] == "missing"
    assert "config_fingerprint" not in missing.json()

    probed = admin.post("/ai/provider-readiness/probe")
    assert probed.status_code == 200, probed.text
    assert probed.json()["schema_version"] == "provider-readiness.v2"
    assert probed.json()["start_allowed"] is True
    assert "config_fingerprint" not in probed.json()


def test_old_schema_version_row_fails_closed_even_if_other_fields_forged_equal(tmp_path):
    # ProviderReadinessProbe is an append-only ledger (before_update is
    # rejected), so a legacy row is simulated with a fresh INSERT that copies
    # every field a real probe would have produced except schema_version,
    # never by mutating an already-persisted row.
    engine = create_engine(f"sqlite:///{tmp_path / 'schema-mismatch.db'}")
    SQLModel.metadata.create_all(engine)
    now = datetime(2026, 7, 19, 10, 0, 0)
    config = _configuration()
    result = provider_readiness.run_synthetic_probe(config)
    with Session(engine) as session:
        row = ProviderReadinessProbe(
            probe_id="prb_legacy_schema_mismatch_test",
            schema_version="provider-readiness.v1",
            runtime_contract=result.configuration.runtime_contract,
            config_fingerprint=result.configuration.fingerprint,
            tts_engine_version=result.configuration.tts_engine_version,
            asr_engine_version=result.configuration.asr_engine_version,
            llm_engine_version=result.configuration.llm_engine_version,
            tts_required=result.tts.required, tts_success=result.tts.success,
            tts_failure_code=result.tts.failure_code,
            asr_required=result.asr.required, asr_success=result.asr.success,
            asr_failure_code=result.asr.failure_code,
            llm_required=result.llm.required,
            llm_configured=result.configuration.llm_configured,
            llm_success=result.llm.success, llm_failure_code=result.llm.failure_code,
            required_capabilities_ready=result.required_capabilities_ready,
            all_configured_capabilities_ready=result.all_configured_capabilities_ready,
            probe_failure_code=result.probe_failure_code,
            checked_at=now, expires_at=now + timedelta(minutes=30),
            actor_display_id="ADMIN-1",
        )
        session.add(row)
        session.commit()

        projection = provider_readiness.readiness_projection(
            session, configuration=config, now=now + timedelta(minutes=1))

        assert projection.status == "config_mismatch"
        assert projection.start_allowed is False
        # The row stays v1; only the response schema is the current constant.
        assert projection.schema_version == "provider-readiness.v2"


def test_speech_rate_only_change_invalidates_fingerprint():
    slow = provider_readiness.capture_configuration(
        tts_engine=_FakeQwenAutopilotEngine(speech_rate=0.9),
        asr_engine=FakeAsr(), llm_engine=FakeLlm())
    fast = provider_readiness.capture_configuration(
        tts_engine=_FakeQwenAutopilotEngine(speech_rate=1.0),
        asr_engine=FakeAsr(), llm_engine=FakeLlm())

    assert slow.tts_engine_version == fast.tts_engine_version
    assert slow.fingerprint != fast.fingerprint


def test_default_autopilot_resolver_missing_key_fails_closed_without_network(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    transport_calls: list[str] = []

    def _sentinel_stream(self, text):
        # The real streaming/SDK transport boundary.  If the missing-key
        # path ever regresses to reach this, fail loudly rather than let a
        # real network attempt happen; synthesize() catches broad
        # Exception, so the explicit call-recording assertion below is the
        # actual proof, independent of whether this raise gets swallowed.
        transport_calls.append(text)
        raise AssertionError(
            "transport boundary reached on a missing-key probe: must fail "
            "closed before any provider call")

    monkeypatch.setattr(
        provider_readiness.tts.DashScopeQwenTtsEngine, "_stream", _sentinel_stream)

    # tts_engine is left unset so the real default get_autopilot_engine()
    # resolver runs and builds a genuine DashScopeQwenTtsEngine; only the
    # transport method is patched, never the whole engine.
    config = provider_readiness.capture_configuration(
        asr_engine=FakeAsr(), llm_engine=FakeLlm())
    result = provider_readiness.run_synthetic_probe(config)

    assert transport_calls == []
    assert result.tts.success is False
    assert result.tts.failure_code == "tts_audio_invalid"
    assert result.required_capabilities_ready is False


def test_cache_params_allowlists_exact_qwen_speech_rate_shape():
    descriptor = provider_readiness._cache_params(
        _FakeQwenAutopilotEngine(speech_rate=0.9))

    assert descriptor == "speech_rate=0.9"


def test_allowlist_accepts_every_tts_engine_that_actually_ships():
    # 上面几条用的都是假引擎，所以白名单可以整条与现实脱节而全绿:允许了
    # voice=synthetic(只存在于本文件的 FakeTts)，却漏掉 harness 里真正会被
    # 探针读到的 harness-synthetic-v1,整套 TTS ACK 演练直接起不来。这条把
    # 仓库里每个真引擎类的真值逐个喂进去,漏一个就红。
    from pathlib import Path

    from app import tts
    from harness import tts_ack_harness

    real_engines = (
        tts.NullTtsEngine(),
        tts.PiperTtsEngine(Path("/nonexistent/voice.onnx")),
        tts.DashScopeCosyVoiceEngine(
            model="cosyvoice-v2", voice="longyuan_v2", speech_rate=0.9),
        tts.DashScopeQwenTtsEngine(
            model="qwen3-tts-flash", voice="Serena", speech_rate=0.9),
        tts_ack_harness.DeterministicHarnessTtsEngine(),
    )

    for engine in real_engines:
        descriptor = provider_readiness._cache_params(engine)
        assert descriptor == (engine.cache_params or "")


def test_cache_params_tail_drift_past_200_chars_is_rejected_fail_closed():
    # Approved shapes are intentionally bounded (a handful of chars), so a
    # >200-char value can never be an approved shape -- rejecting it before
    # fingerprint construction, rather than truncating-then-hashing it, is
    # the correct no-collision behavior: two differing-only-after-200-chars
    # values must never be silently treated as the same probed config.
    prefix = "x" * 200

    class TailA:
        cache_params = prefix + "AAAA"

    class TailB:
        cache_params = prefix + "BBBB"

    with pytest.raises(ValueError) as excinfo_a:
        provider_readiness._cache_params(TailA())
    with pytest.raises(ValueError) as excinfo_b:
        provider_readiness._cache_params(TailB())

    message_a, message_b = str(excinfo_a.value), str(excinfo_b.value)
    assert message_a == message_b  # fixed message, never derived from the value
    assert "AAAA" not in message_a
    assert "BBBB" not in message_a
    assert prefix not in message_a

    with pytest.raises(ValueError):
        provider_readiness.capture_configuration(
            tts_engine=TailA(), asr_engine=FakeAsr(), llm_engine=FakeLlm())


def test_unapproved_cache_params_value_fails_closed_without_leaking_it():
    class SecretLikeTts:
        cache_params = "sk-live-AbCdEf1234567890SECRETLOOKING"

    with pytest.raises(ValueError) as excinfo:
        provider_readiness._cache_params(SecretLikeTts())

    message = str(excinfo.value)
    assert "sk-live" not in message
    assert SecretLikeTts.cache_params not in message

    with pytest.raises(ValueError):
        provider_readiness.capture_configuration(
            tts_engine=SecretLikeTts(), asr_engine=FakeAsr(), llm_engine=FakeLlm())
