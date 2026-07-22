"""P0a HTTP adapter: independent account/device principals and atomic ACK routing."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import json
import threading

import pytest
from fastapi import HTTPException, Response
from fastapi.testclient import TestClient
from sqlalchemy import delete, event
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from app import (asr, audio_store, auth, autopilot_orchestration,
                 autopilot_service, content, db, provider_readiness)
from app.main import (
    AttemptProcessIn,
    _process_attempt,
    _run_p0a_attempt_worker,
    app,
)
from app.models import (
    AttemptEvent,
    AuditLog,
    AudioAssetRow,
    AutopilotControlEvent,
    InteractionEvent,
    ItemEvent,
    LiveState,
    Patient,
    ProviderReadinessProbe,
    ResearchUser,
    RuntimeCommand,
    RuntimeCommandAck,
    Session as TrainSession,
    SessionAutopilotState,
    SessionOutcomeSummary,
    SessionRuntimeState,
    TechnicalPauseReceipt,
    TurnEvent,
    VisitPlan,
)


SESSION_ID = "S-P0A-HTTP"
OTHER_SESSION_ID = "S-P0A-HTTP-OTHER"
PATIENT_ID = "P-P0A-HTTP"
START_KEY = "start-p0a-http-0001"
BANK = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
FIRST_ONLY_BANK = replace(
    BANK,
    single_element=[BANK.single_element[0]],
    double_element=[],
    multi_element=[],
    meta={
        **BANK.meta,
        "source_protocol_position_count": 1,
        "source_unstructured_positions": [],
    },
)
PROTOCOL = content.load_autopilot_protocol(
    content.CONTENT_DIR / "autopilot_protocol_v1.json")


@dataclass
class ApiClients:
    account: TestClient
    device: TestClient
    anonymous: TestClient
    device_headers: dict[str, str]
    engine: object
    scheduled_attempts: list[str]


@pytest.fixture
def api_clients(monkeypatch, tmp_path) -> ApiClients:
    # A file-backed database gives concurrent worker/control requests independent
    # connections.  StaticPool's single shared sqlite3 connection makes an Event-
    # blocked provider test exercise DBAPI re-entrancy instead of our real fences.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'autopilot-api.db'}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    monkeypatch.setattr(db, "engine", engine)
    # API lifecycle tests exercise a bounded, fully supported bank.  The
    # production draft bank is covered separately by the all-plan refusal test.
    monkeypatch.setattr(
        content, "load_item_bank", lambda _path: FIRST_ONLY_BANK)
    scheduled_attempts: list[str] = []
    monkeypatch.setattr(
        autopilot_orchestration,
        "submit",
        lambda session_id, _worker: scheduled_attempts.append(session_id) or True,
    )
    SQLModel.metadata.create_all(engine)
    now = datetime.now()
    readiness_config = provider_readiness.capture_configuration()
    monkeypatch.setattr(
        provider_readiness, "capture_configuration",
        lambda **_kwargs: readiness_config)
    with Session(engine) as session:
        session.add(Patient(
            patient_id=PATIENT_ID,
            is_simulation_subject=True,
            consent_status="已同意",
            recording_allowed=True,
        ))
        session.commit()
        session.add_all([
            TrainSession(
                session_id=SESSION_ID,
                patient_id=PATIENT_ID,
                week_no=2,
                phase_type="正式训练",
                event_line="正式训练",
                item_bank_version_id=BANK.version_id,
                item_bank_definition_digest=(
                    content.item_bank_definition_digest(FIRST_ONLY_BANK)),
                autopilot_protocol_version_id=PROTOCOL["protocol_version_id"],
                autopilot_protocol_definition_digest=(
                    content.autopilot_protocol_definition_digest(PROTOCOL)),
                is_simulation=True,
                data_classification="simulation",
                trainer_id="ACTOR-P0A-HTTP",
            ),
            TrainSession(
                session_id=OTHER_SESSION_ID,
                patient_id=PATIENT_ID,
                week_no=2,
                phase_type="正式训练",
                event_line="正式训练",
                item_bank_version_id=BANK.version_id,
                item_bank_definition_digest=(
                    content.item_bank_definition_digest(FIRST_ONLY_BANK)),
                autopilot_protocol_version_id=PROTOCOL["protocol_version_id"],
                autopilot_protocol_definition_digest=(
                    content.autopilot_protocol_definition_digest(PROTOCOL)),
                is_simulation=True,
                data_classification="simulation",
                trainer_id="ACTOR-P0A-HTTP",
            ),
        ])
        session.commit()
        session.add_all([
            LiveState(
                id=1,
                seq=1,
                session_json=json.dumps({
                    "sessionId": SESSION_ID,
                    "weekNo": 2,
                    "eventLine": "正式训练",
                    "mode": "task",
                    "itemBankVersionId": BANK.version_id,
                }, ensure_ascii=False),
                updated_at=now,
            ),
            SessionRuntimeState(
                session_id=SESSION_ID,
                status="active",
                revision=0,
                updated_at=now,
            ),
            SessionRuntimeState(
                session_id=OTHER_SESSION_ID,
                status="active",
                revision=0,
                updated_at=now,
            ),
            ResearchUser(
                username="p0a-researcher",
                display_id="ACTOR-P0A-HTTP",
                password_hash=auth.hash_password("password1"),
                role="researcher",
                created_at=now,
            ),
            ProviderReadinessProbe(
                probe_id="prb_autopilot_api_fixture",
                schema_version=provider_readiness.SCHEMA_VERSION,
                runtime_contract=provider_readiness.RUNTIME_CONTRACT,
                config_fingerprint=readiness_config.fingerprint,
                tts_engine_version=readiness_config.tts_engine_version,
                asr_engine_version=readiness_config.asr_engine_version,
                llm_engine_version=readiness_config.llm_engine_version,
                tts_required=True,
                tts_success=True,
                asr_required=True,
                asr_success=True,
                llm_required=False,
                llm_configured=readiness_config.llm_configured,
                llm_success=readiness_config.llm_configured,
                llm_failure_code=(
                    None if readiness_config.llm_configured
                    else "llm_not_required_not_configured"),
                required_capabilities_ready=True,
                all_configured_capabilities_ready=True,
                checked_at=now,
                expires_at=now + timedelta(hours=1),
                actor_display_id="ACTOR-P0A-HTTP",
            ),
        ])
        session.commit()

    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    account = TestClient(app)
    login = account.post("/auth/login", json={
        "username": "p0a-researcher", "password": "password1",
    })
    assert login.status_code == 200, login.text
    csrf = account.cookies.get(auth.CSRF_COOKIE_NAME)
    assert csrf
    account.headers["X-CSRF-Token"] = csrf

    # This is a genuinely independent patient-device client: no account cookie.
    device = TestClient(app)
    paired = device.post(
        "/device/pair",
        headers={"X-Console-Pin": "246810"},
        json={"deviceId": "p0a-http-device-000001"},
    )
    assert paired.status_code == 200, paired.text
    device_headers = {"X-Device-Capability": paired.json()["capability"]}
    anonymous = TestClient(app)
    try:
        yield ApiClients(
            account, device, anonymous, device_headers, engine, scheduled_attempts)
    finally:
        account.close()
        device.close()
        anonymous.close()


def _enable_p0a(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_AUTOPILOT_P0A_SIMULATION", "1")
    monkeypatch.setenv("ALLOW_SIMULATION_DATA", "1")


def _start(clients: ApiClients, **overrides):
    body = {"idempotency_key": START_KEY, "expected_revision": 0}
    body.update(overrides)
    return clients.account.post(
        f"/sessions/{SESSION_ID}/autopilot/start", json=body)


def _device_next(clients: ApiClients) -> dict | None:
    response = clients.device.get(
        f"/sessions/{SESSION_ID}/autopilot/next",
        headers=clients.device_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _ack_body(command: dict, *, ack_type: str, ack_key: str,
              device_event_seq: int, **facts) -> dict:
    body = {
        "idempotency_key": ack_key,
        "ack_type": ack_type,
        "control_generation": command["control_generation"],
        "runner_generation": command["runner_generation"],
        "command_revision": command["command_revision"],
        "device_event_seq": device_event_seq,
    }
    body.update(facts)
    return body


def _drive_to_processing_attempt(clients: ApiClients) -> dict:
    """Use only public device APIs to persist a proof-complete P0a capture."""
    started = _start(clients)
    assert started.status_code == 200, started.text
    tts = _device_next(clients)
    assert tts is not None
    ended = clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{tts['command_key']}/acks",
        headers=clients.device_headers,
        json=_ack_body(
            tts,
            ack_type="tts_ended",
            ack_key="ack-worker-tts-ended-0001",
            device_event_seq=1,
            media_ended=True,
            media_duration_ms=800,
        ),
    )
    assert ended.status_code == 200, ended.text
    record = ended.json()["command"]
    raw_audio_id = record["payload"]["raw_audio_id"]
    blob = b"\x1a\x45\xdf\xa3p0a-authoritative-worker"
    uploaded = clients.device.put(
        f"/audio/{raw_audio_id}/blob",
        headers={**clients.device_headers, "content-type": "audio/webm"},
        content=blob,
    )
    assert uploaded.status_code == 200, uploaded.text
    upload_fact = uploaded.json()
    saved = clients.device.put(
        "/live/state",
        headers=clients.device_headers,
        json={
            "kind": "audioSaved",
            "payload": {
                "rawAudioId": raw_audio_id,
                "durationSeconds": 1.5,
                "byteCount": upload_fact["bytes"],
                "checksum": upload_fact["checksum"],
                "turnKey": record["payload"]["turn_ref"],
                "sessionId": SESSION_ID,
                "containsDirectIdentifier": False,
            },
        },
    )
    assert saved.status_code == 200, saved.text
    receipt = saved.json()["audioReceipt"]
    stopped = clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{record['command_key']}/acks",
        headers=clients.device_headers,
        json=_ack_body(
            record,
            ack_type="record_stopped",
            ack_key="ack-worker-record-stopped-0001",
            device_event_seq=2,
            stop_reason="silence",
            raw_audio_id=raw_audio_id,
            receipt_server_seq=receipt["serverSeq"],
            checksum=upload_fact["checksum"],
            byte_count=upload_fact["bytes"],
            duration_seconds=1.5,
        ),
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "processing_attempt"
    return {"record": record, "raw_audio_id": raw_audio_id}


def _valid_attempt_body(raw_audio_id: str, **overrides) -> dict:
    body = {
        "item_id": BANK.single_element[0]["item_id"],
        "turn_seq": 1,
        "response_role": "命名",
        "raw_audio_id": raw_audio_id,
        "prompt_level": 0,
        "duration_seconds": 1.5,
    }
    body.update(overrides)
    return body


def _all_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key for child in value.values() for key in _all_keys(child)
        }
    if isinstance(value, list):
        return {key for child in value for key in _all_keys(child)}
    return set()


def test_disabled_start_rolls_back_and_returns_stable_conflict(
        api_clients: ApiClients, monkeypatch):
    monkeypatch.delenv("ENABLE_AUTOPILOT_P0A_SIMULATION", raising=False)
    denied = _start(api_clients)
    assert denied.status_code == 409
    assert denied.json()["detail"]["code"] == "autopilot_p0a_disabled"
    with Session(api_clients.engine) as session:
        assert session.get(SessionAutopilotState, SESSION_ID) is None
        assert list(session.exec(select(RuntimeCommand))) == []


def test_start_without_current_provider_probe_fails_before_control_ownership(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    # A present key is configuration only, never a readiness proof.  The test
    # capture is injected and cannot perform network I/O.
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-presence-is-not-proof")
    with Session(api_clients.engine) as session:
        session.exec(delete(ProviderReadinessProbe))
        session.commit()

    denied = _start(api_clients)
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "provider_readiness_missing"
    assert denied.json()["detail"]["readiness"]["start_allowed"] is False
    with Session(api_clients.engine) as session:
        assert session.get(SessionAutopilotState, SESSION_ID) is None
        assert list(session.exec(select(RuntimeCommand))) == []
        assert list(session.exec(select(AutopilotControlEvent))) == []


def test_default_draft_week2_plan_returns_structured_gap_before_ownership(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    monkeypatch.setattr(content, "load_item_bank", lambda _path: BANK)
    with Session(api_clients.engine) as session:
        training = session.get(TrainSession, SESSION_ID)
        assert training is not None
        training.item_bank_definition_digest = (
            content.item_bank_definition_digest(BANK))
        session.add(training)
        session.commit()

    denied = _start(api_clients)
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"] == {
        "code": "autopilot_plan_not_fully_supported",
        "message": (
            "完整源协议仍有 60 个位置不受当前自动协议支持；"
            "首个缺口为 DE_烟灰缸+烟#1:左命名"
        ),
        "unsupported_position_count": 60,
        "structured_unsupported_position_count": 50,
        "source_unstructured_position_count": 10,
        "source_protocol_position_count": 80,
        "first_gap": {
            "code": "operational_protocol_unavailable",
            "item_id": "DE_烟灰缸+烟",
            "turn_seq": 1,
            "response_role": "左命名",
        },
    }
    with Session(api_clients.engine) as session:
        assert session.get(SessionAutopilotState, SESSION_ID) is None
        assert list(session.exec(select(RuntimeCommand))) == []
        assert list(session.exec(select(AutopilotControlEvent))) == []


def test_write_adapter_rolls_back_domain_and_integrity_failures(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)

    def staged_domain_failure(session: Session, **_kwargs):
        session.add(SessionAutopilotState(session_id=SESSION_ID))
        session.flush()
        raise autopilot_service.AutopilotServiceError(
            "autopilot_test_conflict", "可预期的领域冲突")

    monkeypatch.setattr(
        autopilot_service, "start_p0a", staged_domain_failure)
    domain = _start(api_clients)
    assert domain.status_code == 409
    assert domain.json()["detail"] == {
        "code": "autopilot_test_conflict",
        "message": "可预期的领域冲突",
    }
    with Session(api_clients.engine) as session:
        assert session.get(SessionAutopilotState, SESSION_ID) is None

    def staged_integrity_failure(session: Session, **_kwargs):
        session.add(SessionAutopilotState(session_id=SESSION_ID))
        session.flush()
        raise IntegrityError("autopilot test", {}, RuntimeError("collision"))

    monkeypatch.setattr(
        autopilot_service, "start_p0a", staged_integrity_failure)
    integrity = _start(api_clients)
    assert integrity.status_code == 409
    assert integrity.json()["detail"]["code"] == "autopilot_concurrency_conflict"
    with Session(api_clients.engine) as session:
        assert session.get(SessionAutopilotState, SESSION_ID) is None


def test_account_start_and_exact_device_get_are_separate_principals(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)

    anonymous_start = api_clients.anonymous.post(
        f"/sessions/{SESSION_ID}/autopilot/start",
        json={"idempotency_key": START_KEY, "expected_revision": 0},
    )
    assert anonymous_start.status_code == 401
    assert anonymous_start.json()["code"] == "account_required"
    device_start = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/start",
        headers=api_clients.device_headers,
        json={"idempotency_key": START_KEY, "expected_revision": 0},
    )
    assert device_start.status_code == 401
    assert device_start.json()["code"] == "account_required"

    started = _start(api_clients)
    assert started.status_code == 200, started.text
    payload = started.json()
    assert payload == {
        "scope_key": "p0a_sim_first_single_v1",
        "mode": "autonomous",
        "status": "waiting_tts",
        "state_revision": 1,
        "server_owned": True,
        "current_command_kind": "tts",
        "last_error_code": None,
    }
    assert _all_keys(payload).isdisjoint({
        "command", "payload", "speech_text", "image_id", "item_ref",
        "item_id", "turn_key", "target_word", "cues", "tell_answer",
        "issued_capability_token_hash", "issued_device_id_hash", "issued_at",
    })

    anonymous_status = api_clients.anonymous.get(
        f"/sessions/{SESSION_ID}/autopilot/status")
    assert anonymous_status.status_code == 401
    device_status = api_clients.device.get(
        f"/sessions/{SESSION_ID}/autopilot/status",
        headers=api_clients.device_headers)
    assert device_status.status_code == 401
    account_status = api_clients.account.get(
        f"/sessions/{SESSION_ID}/autopilot/status")
    assert account_status.status_code == 200
    assert account_status.json() == payload

    anonymous_next = api_clients.anonymous.get(
        f"/sessions/{SESSION_ID}/autopilot/next")
    assert anonymous_next.status_code == 401
    assert anonymous_next.json()["code"] == "device_pair_required"
    account_next = api_clients.account.get(
        f"/sessions/{SESSION_ID}/autopilot/next")
    assert account_next.status_code == 403
    exact = api_clients.device.get(
        f"/sessions/{SESSION_ID}/autopilot/next",
        headers=api_clients.device_headers,
    )
    assert exact.status_code == 200, exact.text
    assert exact.json()["kind"] == "tts"
    assert exact.json()["item_ref"] == "itm-0001"
    assert _all_keys(exact.json()).isdisjoint({
        "image_id", "item_id", "target_word", "cues", "tell_answer",
        "success_line", "acceptable_expressions",
    })

    cross_session = api_clients.device.get(
        f"/sessions/{OTHER_SESSION_ID}/autopilot/next",
        headers=api_clients.device_headers,
    )
    assert cross_session.status_code == 409
    assert cross_session.json()["detail"]["code"] == "device_session_mismatch"

    replay = _start(api_clients)
    assert replay.status_code == 200
    assert replay.json() == payload
    with Session(api_clients.engine) as session:
        assert len(list(session.exec(select(RuntimeCommand)))) == 1


def test_exact_tts_is_server_derived_and_generic_text_route_is_locked(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    synthesized: list[str] = []

    def fake_speak(text: str):
        synthesized.append(text)
        return None, "exact-tts-test", False

    monkeypatch.setattr("app.main.tts.speak", fake_speak)
    started = _start(api_clients)
    assert started.status_code == 200, started.text
    command = _device_next(api_clients)
    assert command is not None and command["kind"] == "tts"

    # A device cannot choose another allowlisted line (including the answer)
    # once the server owns the scope.
    generic = api_clients.device.post(
        "/tts/speak",
        headers=api_clients.device_headers,
        json={"text": BANK.single_element[0]["tell_answer"]},
    )
    assert generic.status_code == 409
    assert generic.json()["detail"]["code"] == "autopilot_manual_control_locked"
    assert synthesized == []

    exact_path = (
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{command['command_key']}/tts"
    )
    account_denied = api_clients.account.post(exact_path, json={})
    assert account_denied.status_code == 403
    wrong = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/wrong-command-0001/tts",
        headers=api_clients.device_headers,
        json={},
    )
    assert wrong.status_code == 409
    assert wrong.json()["detail"]["code"] == "autopilot_command_not_current"

    injected = api_clients.device.post(
        exact_path,
        headers=api_clients.device_headers,
        json={"text": BANK.single_element[0]["tell_answer"]},
    )
    assert injected.status_code == 422
    assert synthesized == []
    exact = api_clients.device.post(
        exact_path, headers=api_clients.device_headers, json={})
    assert exact.status_code == 204, exact.text
    assert exact.headers["x-tts-engine"] == "exact-tts-test"
    assert synthesized == [BANK.single_element[0]["initial_prompt"]]


def test_generic_tts_discards_provider_bytes_if_live_session_switches_in_flight(
        api_clients: ApiClients, monkeypatch):
    """A provider result belongs to the exact live revision that authorized it."""
    provider_entered = threading.Event()
    provider_release = threading.Event()
    stale_audio = b"RIFF-generic-stale-provider-audio"

    def blocked_speak(_text: str):
        provider_entered.set()
        assert provider_release.wait(10), "generic TTS provider was never released"
        return stale_audio, "blocked-generic-tts", False

    monkeypatch.setattr("app.main.tts.speak", blocked_speak)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(
            api_clients.account.post,
            "/tts/speak",
            json={"text": "只属于原场次的话术"},
        )
        assert provider_entered.wait(10), "generic TTS never reached provider I/O"
        try:
            switched = api_clients.account.put("/live/state", json={
                "kind": "session",
                "payload": {
                    "sessionId": OTHER_SESSION_ID,
                    "weekNo": 2,
                    "eventLine": "正式训练",
                    "mode": "task",
                    "itemBankVersionId": BANK.version_id,
                },
            })
            assert switched.status_code == 200, switched.text
        finally:
            provider_release.set()
        response = pending.result(timeout=10)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "tts_authorization_changed",
        "message": "语音合成期间场次或授权已变化，本次音频已丢弃",
        "action": "discard_synthesized_audio",
    }
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers.get("content-type", "").startswith("application/json")
    assert stale_audio not in response.content


def test_exact_tts_discards_provider_bytes_if_session_is_aborted_in_flight(
        api_clients: ApiClients, monkeypatch):
    """An exact command is reauthorized after slow provider I/O, not before only."""
    _enable_p0a(monkeypatch)
    started = _start(api_clients)
    assert started.status_code == 200, started.text
    command = _device_next(api_clients)
    assert command is not None and command["kind"] == "tts"

    provider_entered = threading.Event()
    provider_release = threading.Event()
    stale_audio = b"RIFF-exact-stale-provider-audio"

    def blocked_speak(_text: str):
        provider_entered.set()
        assert provider_release.wait(10), "exact TTS provider was never released"
        return stale_audio, "blocked-exact-tts", False

    monkeypatch.setattr("app.main.tts.speak", blocked_speak)
    exact_path = (
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{command['command_key']}/tts"
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(
            api_clients.device.post,
            exact_path,
            headers=api_clients.device_headers,
            json={},
        )
        assert provider_entered.wait(10), "exact TTS never reached provider I/O"
        try:
            aborted = api_clients.account.post(
                f"/sessions/{SESSION_ID}/abort",
                json={
                    "reason_code": "researcher_decision",
                    "expected_revision": 0,
                    "idempotency_key": "abort-during-blocked-exact-tts-0001",
                },
            )
            assert aborted.status_code == 200, aborted.text
            assert aborted.json()["status"] == "aborted"
        finally:
            provider_release.set()
        response = pending.result(timeout=10)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "tts_authorization_changed",
        "message": "语音合成期间场次或授权已变化，本次音频已丢弃",
        "action": "discard_synthesized_audio",
    }
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers.get("content-type", "").startswith("application/json")
    assert stale_audio not in response.content


def test_tts_ended_ack_atomically_persists_receipt_and_opens_one_record_slot(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    started = _start(api_clients)
    assert started.status_code == 200, started.text
    command = _device_next(api_clients)
    assert command is not None
    ack_path = (
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{command['command_key']}/acks"
    )

    account_cannot_ack = api_clients.account.post(
        ack_path,
        json=_ack_body(
            command,
            ack_type="tts_started",
            ack_key="ack-tts-started-http-0001",
            device_event_seq=1,
        ),
    )
    assert account_cannot_ack.status_code == 403

    tts_started = api_clients.device.post(
        ack_path,
        headers=api_clients.device_headers,
        json=_ack_body(
            command,
            ack_type="tts_started",
            ack_key="ack-tts-started-http-0001",
            device_event_seq=1,
        ),
    )
    assert tts_started.status_code == 200, tts_started.text
    started_receipt = tts_started.json()
    assert started_receipt["command_state"] == "started"
    assert started_receipt["command_revision"] == 1
    assert started_receipt["status"] == "waiting_tts"
    assert started_receipt["command"]["state"] == "started"

    ended_body = _ack_body(
        started_receipt["command"],
        ack_type="tts_ended",
        ack_key="ack-tts-ended-http-0001",
        device_event_seq=2,
        media_ended=True,
        media_duration_ms=900,
    )
    ended = api_clients.device.post(
        ack_path, headers=api_clients.device_headers, json=ended_body)
    assert ended.status_code == 200, ended.text
    ended_receipt = ended.json()
    assert ended_receipt["command_state"] == "succeeded"
    assert ended_receipt["status"] == "waiting_recording"
    assert ended_receipt["command"]["kind"] == "record"
    assert ended_receipt["command"]["state"] == "pending"
    assert ended_receipt["command"]["payload"]["turn_ref"] == "itm-0001#1"
    assert _all_keys(ended_receipt).isdisjoint({
        "item_id", "turn_key", "target_word", "cues", "tell_answer",
        "issued_capability_token_hash", "issued_device_id_hash", "issued_at",
    })

    # The HTTP success is observable only after ACK, succeeded TTS, preallocated
    # audio row, record command and state pointer have committed together.
    with Session(api_clients.engine) as session:
        commands = list(session.exec(select(RuntimeCommand).order_by(
            RuntimeCommand.command_seq)))
        acks = list(session.exec(select(RuntimeCommandAck).order_by(
            RuntimeCommandAck.device_event_seq)))
        assets = list(session.exec(select(AudioAssetRow)))
        state = session.get(SessionAutopilotState, SESSION_ID)
        assert [(row.kind, row.state) for row in commands] == [
            ("tts", "succeeded"), ("record", "pending"),
        ]
        assert [row.ack_type for row in acks] == ["tts_started", "tts_ended"]
        assert len(assets) == 1
        assert assets[0].raw_audio_id == ended_receipt["command"]["payload"]["raw_audio_id"]
        assert state is not None
        assert state.status == "waiting_recording"
        assert state.current_command_id == commands[1].id

    replay = api_clients.device.post(
        ack_path, headers=api_clients.device_headers, json=ended_body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["command"] is None

    conflict_body = dict(ended_body)
    conflict_body["media_duration_ms"] = 901
    conflict = api_clients.device.post(
        ack_path, headers=api_clients.device_headers, json=conflict_body)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "autopilot_ack_conflict"
    with Session(api_clients.engine) as session:
        assert len(list(session.exec(select(RuntimeCommandAck)))) == 2
        assert len(list(session.exec(select(RuntimeCommand)))) == 2


def test_cross_session_and_malformed_device_acks_fail_before_mutation(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    started = _start(api_clients)
    assert started.status_code == 200
    command = _device_next(api_clients)
    assert command is not None
    body = _ack_body(
        command,
        ack_type="tts_ended",
        ack_key="ack-tts-ended-http-cross",
        device_event_seq=1,
        media_ended=True,
    )
    cross = api_clients.device.post(
        f"/sessions/{OTHER_SESSION_ID}/autopilot/commands/{command['command_key']}/acks",
        headers=api_clients.device_headers,
        json=body,
    )
    assert cross.status_code == 409
    assert cross.json()["detail"]["code"] == "device_session_mismatch"

    malformed = dict(body)
    malformed["media_ended"] = False
    bad = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/{command['command_key']}/acks",
        headers=api_clients.device_headers,
        json=malformed,
    )
    assert bad.status_code == 422
    with Session(api_clients.engine) as session:
        assert list(session.exec(select(RuntimeCommandAck))) == []
        stored = session.exec(select(RuntimeCommand)).one()
        assert stored.state == "pending" and stored.revision == 0


def test_preallocated_record_upload_receipt_and_stopped_ack_form_one_exact_chain(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    first = _start(api_clients)
    assert first.status_code == 200, first.text
    tts = _device_next(api_clients)
    assert tts is not None
    tts_path = (
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{tts['command_key']}/acks"
    )
    ended = api_clients.device.post(
        tts_path,
        headers=api_clients.device_headers,
        json=_ack_body(
            tts,
            ack_type="tts_ended",
            ack_key="ack-tts-ended-http-record-chain",
            device_event_seq=1,
            media_ended=True,
            media_duration_ms=850,
        ),
    )
    assert ended.status_code == 200, ended.text
    record = ended.json()["command"]
    raw_audio_id = record["payload"]["raw_audio_id"]
    turn_ref = record["payload"]["turn_ref"]

    generic_auth = api_clients.device.post(
        f"/sessions/{SESSION_ID}/recording-authorization",
        headers=api_clients.device_headers,
    )
    assert generic_auth.status_code == 409
    assert generic_auth.json()["detail"]["code"] == (
        "autopilot_manual_control_locked")
    exact_auth_path = (
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{record['command_key']}/recording-authorization"
    )
    account_auth = api_clients.account.post(exact_auth_path)
    assert account_auth.status_code == 403
    wrong_auth = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        "wrong-record-command-0001/recording-authorization",
        headers=api_clients.device_headers,
    )
    assert wrong_auth.status_code == 409
    assert wrong_auth.json()["detail"]["code"] == (
        "autopilot_command_not_current")
    exact_auth = api_clients.device.post(
        exact_auth_path, headers=api_clients.device_headers)
    assert exact_auth.status_code == 200, exact_auth.text
    assert exact_auth.json() == {
        "allowed": True,
        "runtime_status": "active",
        "is_simulation": True,
    }

    # The patient recorder may always ACK the exact server-preallocated row;
    # ownership interlocks only reject a newly client-chosen raw id.
    registered = api_clients.device.post(
        "/audio",
        headers=api_clients.device_headers,
        json={
            "raw_audio_id": raw_audio_id,
            "session_id": SESSION_ID,
            "turn_key": turn_ref,
            "is_simulation": True,
            "contains_direct_identifier": False,
        },
    )
    assert registered.status_code == 200, registered.text
    assert registered.json() == {"raw_audio_id": raw_audio_id, "registered": True}

    blob = b"\x1a\x45\xdf\xa3p0a-http-record-chain"
    uploaded = api_clients.device.put(
        f"/audio/{raw_audio_id}/blob",
        headers={**api_clients.device_headers, "content-type": "audio/webm"},
        content=blob,
    )
    assert uploaded.status_code == 200, uploaded.text
    upload_fact = uploaded.json()
    saved = api_clients.device.put(
        "/live/state",
        headers=api_clients.device_headers,
        json={
            "kind": "audioSaved",
            "payload": {
                "rawAudioId": raw_audio_id,
                "durationSeconds": 1.25,
                "byteCount": upload_fact["bytes"],
                "checksum": upload_fact["checksum"],
                "turnKey": turn_ref,
                "sessionId": SESSION_ID,
                "containsDirectIdentifier": False,
            },
        },
    )
    assert saved.status_code == 200, saved.text
    receipt = saved.json()["audioReceipt"]
    assert receipt["rawAudioId"] == raw_audio_id

    record_path = (
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{record['command_key']}/acks"
    )
    record_started = api_clients.device.post(
        record_path,
        headers=api_clients.device_headers,
        json=_ack_body(
            record,
            ack_type="record_started",
            ack_key="ack-record-started-http-0001",
            device_event_seq=2,
            mime_type="audio/webm",
            sample_rate_hz=48_000,
            channels=1,
        ),
    )
    assert record_started.status_code == 200, record_started.text
    assert record_started.json()["command_state"] == "started"
    started_record = record_started.json()["command"]
    cannot_reopen = api_clients.device.post(
        exact_auth_path, headers=api_clients.device_headers)
    assert cannot_reopen.status_code == 409
    assert cannot_reopen.json()["detail"]["code"] == (
        "autopilot_command_not_current")

    stopped_body = _ack_body(
        started_record,
        ack_type="record_stopped",
        ack_key="ack-record-stopped-http-0001",
        device_event_seq=3,
        stop_reason="silence",
        raw_audio_id=raw_audio_id,
        receipt_server_seq=receipt["serverSeq"],
        checksum=upload_fact["checksum"],
        byte_count=upload_fact["bytes"],
        duration_seconds=1.25,
    )
    stopped = api_clients.device.post(
        record_path, headers=api_clients.device_headers, json=stopped_body)
    assert stopped.status_code == 200, stopped.text
    stopped_fact = stopped.json()
    assert stopped_fact["command_state"] == "succeeded"
    assert stopped_fact["status"] == "processing_attempt"
    assert stopped_fact["command"] is None
    assert api_clients.scheduled_attempts == [SESSION_ID]

    # A crash after the ACK commit is recoverable from the normal device poll.
    api_clients.scheduled_attempts.clear()
    recovering_poll = api_clients.device.get(
        f"/sessions/{SESSION_ID}/autopilot/next",
        headers=api_clients.device_headers,
    )
    assert recovering_poll.status_code == 200
    assert recovering_poll.json() is None
    assert api_clients.scheduled_attempts == [SESSION_ID]

    with Session(api_clients.engine) as session:
        state = session.get(SessionAutopilotState, SESSION_ID)
        commands = list(session.exec(select(RuntimeCommand).order_by(
            RuntimeCommand.command_seq)))
        acks = list(session.exec(select(RuntimeCommandAck).order_by(
            RuntimeCommandAck.device_event_seq)))
        assert state is not None and state.status == "processing_attempt"
        assert [(row.kind, row.state) for row in commands] == [
            ("tts", "succeeded"), ("record", "succeeded"),
        ]
        assert [row.ack_type for row in acks] == [
            "tts_ended", "record_started", "record_stopped",
        ]
        stopped_ack = acks[-1]
        assert (
            stopped_ack.raw_audio_id,
            stopped_ack.receipt_server_seq,
            stopped_ack.checksum,
            stopped_ack.byte_count,
            stopped_ack.duration_seconds,
        ) == (
            raw_audio_id,
            receipt["serverSeq"],
            upload_fact["checksum"],
            upload_fact["bytes"],
            1.25,
        )

    replay = api_clients.device.post(
        record_path, headers=api_clients.device_headers, json=stopped_body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert api_clients.scheduled_attempts == [SESSION_ID, SESSION_ID]

    mismatched = dict(stopped_body)
    mismatched["duration_seconds"] = 1.5
    conflict = api_clients.device.post(
        record_path, headers=api_clients.device_headers, json=mismatched)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "autopilot_ack_conflict"


def test_server_owned_scope_blocks_every_legacy_manual_write_and_resume(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    started = _start(api_clients)
    assert started.status_code == 200, started.text

    def assert_owned(response) -> None:
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "autopilot_manual_control_locked"

    cursor = {
        "sessionId": SESSION_ID,
        "screen": "present",
        "itemIdx": 0,
        "turnIdx": 0,
        "responseRole": "命名",
        "cueLevel": 0,
        "recording": "idle",
        "selfStart": False,
    }
    assert_owned(api_clients.account.put(
        "/live/state",
        json={"kind": "session", "payload": {
            "sessionId": SESSION_ID,
            "weekNo": 2,
            "eventLine": "正式训练",
            "mode": "task",
            "itemBankVersionId": BANK.version_id,
        }},
    ))
    assert_owned(api_clients.account.put(
        "/live/state", json={"kind": "cursor", "payload": cursor}))
    assert_owned(api_clients.account.put(
        "/live/state", json={"kind": "rapportStep", "payload": {
            "sessionId": SESSION_ID,
            "sectionKey": "greeting",
            "questionIdx": 0,
            "recording": "idle",
            "containsDirectIdentifier": False,
        }}))
    assert_owned(api_clients.account.put(
        f"/sessions/{SESSION_ID}/runtime/cursor", json=cursor))

    item = BANK.single_element[0]
    assert_owned(api_clients.account.post(
        f"/sessions/{SESSION_ID}/items",
        json={
            "item_id": item["item_id"],
            "task_type": "单要素",
            "item_set_type": "训练集",
            "image_id": item["image_id"],
        },
    ))
    with Session(api_clients.engine) as session:
        seeded_item = ItemEvent(
            session_id=SESSION_ID,
            item_id=item["item_id"],
            task_type="单要素",
            item_set_type="训练集",
            image_id=item["image_id"],
        )
        session.add(seeded_item)
        session.commit()
        session.refresh(seeded_item)
        item_event_id = seeded_item.id
        seeded_turn = TurnEvent(
            item_event_id=item_event_id,
            turn_seq=1,
            response_role="命名",
            confirmed_response_text="模拟确认文本",
            prompt_level=0,
        )
        session.add(seeded_turn)
        session.commit()
        session.refresh(seeded_turn)
        turn_event_id = seeded_turn.id
    assert item_event_id is not None
    assert turn_event_id is not None
    assert_owned(api_clients.account.post(
        f"/items/{item_event_id}/turns",
        json={
            "turn_seq": 1,
            "response_role": "命名",
            "raw_audio_id": "legacy-client-turn-audio",
        },
    ))
    assert_owned(api_clients.account.post(
        f"/sessions/{SESSION_ID}/interactions",
        json={
            "event_type": "cue_selected",
            "item_id": item["item_id"],
            "turn_seq": 1,
            "prompt_level": 1,
            "cue_type": "semantic",
        },
    ))
    legacy_technical_pause = api_clients.account.post(
        f"/sessions/{SESSION_ID}/interactions",
        json={
            "event_type": "technical_pause",
            "item_id": item["item_id"],
            "turn_seq": 1,
            "error_code": "legacy_client_failure",
        },
    )
    assert legacy_technical_pause.status_code == 409
    assert legacy_technical_pause.json()["detail"]["code"] == (
        "technical_pause_atomic_required")
    assert_owned(api_clients.account.patch(
        f"/turns/{turn_event_id}/confirm",
        json={
            "confirmed_response_text": "旧标签页修改",
            "expected_revision": 0,
            "idempotency_key": "test-autopilot-owned-confirm-0001",
        },
    ))
    assert_owned(api_clients.account.post(
        f"/turns/{turn_event_id}/ai-judge"))
    assert_owned(api_clients.account.patch(
        f"/turns/{turn_event_id}/lock",
        json={
            "reviewer_id": "legacy-reviewer",
            "element_value": 1,
            "prompt_level": 0,
        },
    ))
    assert_owned(api_clients.account.post(
        f"/sessions/{SESSION_ID}/abnormal",
        json={
            "abnormal_type": "legacy-note",
            "intervention_type": "代说物品名",
            "affects_scoring_validity": True,
        },
    ))
    assert_owned(api_clients.device.post(
        "/audio",
        headers=api_clients.device_headers,
        json={
            "raw_audio_id": "legacy-client-chosen-audio",
            "session_id": SESSION_ID,
            "turn_key": "itm-0001#1",
            "is_simulation": True,
            "contains_direct_identifier": False,
        },
    ))

    paused = api_clients.account.post(f"/sessions/{SESSION_ID}/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"
    with Session(api_clients.engine) as session:
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        commands = list(session.exec(select(RuntimeCommand)))
        events = list(session.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))
        assert control is not None
        assert control.mode == "autonomous"
        assert control.status == "paused"
        assert control.current_command_id is None
        assert runtime_state is not None and runtime_state.status == "paused"
        # The pending immutable command remains evidence, but can no longer
        # accept a new ACK because the state no longer points to it.
        assert len(commands) == 1 and commands[0].state == "pending"
        assert [event.event_type for event in events] == ["start", "pause"]
        assert events[-1].reason_code == "researcher_requested_pause"
        assert json.loads(events[-1].payload_json) == {
            "reason_code": "researcher_requested_pause",
            "source": "account_pause_endpoint",
        }

    assert_owned(api_clients.account.post(f"/sessions/{SESSION_ID}/resume"))
    assert_owned(api_clients.account.post(
        f"/sessions/{SESSION_ID}/interactions",
        json={
            "event_type": "feedback_selected",
            "item_id": item["item_id"],
            "turn_seq": 1,
            "feedback_key": "self",
        },
    ))


def test_atomic_technical_pause_is_the_authoritative_server_owned_stop(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    positioned = api_clients.account.put("/live/state", json={
        "kind": "cursor",
        "payload": {
            "sessionId": SESSION_ID,
            "screen": "present",
            "itemIdx": 0,
            "turnIdx": 0,
            "responseRole": "命名",
            "cueLevel": 0,
            "recording": "idle",
            "selfStart": False,
        },
    })
    assert positioned.status_code == 200, positioned.text
    started = _start(api_clients)
    assert started.status_code == 200, started.text
    runtime_before = api_clients.account.get(
        f"/sessions/{SESSION_ID}/runtime")
    live_before = api_clients.account.get("/live/console-state")
    assert runtime_before.status_code == live_before.status_code == 200
    runtime_snapshot = runtime_before.json()
    live_cursor = live_before.json()["cursor"]
    body = {
        "idempotency_key": "technical-pause-p0a-server-owned-0001",
        "expected_revision": runtime_snapshot["revision"],
        "expected_live_wseq": live_cursor["wseq"],
        "error_code": "client_audio",
    }

    stopped = api_clients.account.post(
        f"/sessions/{SESSION_ID}/technical-pause", json=body)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["runtime"]["status"] == "paused"
    assert stopped.json()["cursor"]["screen"] == "paused"
    replay = api_clients.account.post(
        f"/sessions/{SESSION_ID}/technical-pause", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent"] is True
    assert replay.json()["interaction"] == stopped.json()["interaction"]

    with Session(api_clients.engine) as session:
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        interactions = list(session.exec(select(InteractionEvent).where(
            InteractionEvent.session_id == SESSION_ID)))
        receipts = list(session.exec(select(TechnicalPauseReceipt).where(
            TechnicalPauseReceipt.session_id == SESSION_ID)))
        control_events = list(session.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.session_id == SESSION_ID).order_by(
                AutopilotControlEvent.event_seq)))
        assert control is not None and control.status == "paused"
        assert control.current_command_id is None
        assert runtime_state is not None and runtime_state.status == "paused"
        assert len(interactions) == len(receipts) == 1
        assert interactions[0].event_type == "technical_pause"
        assert [event.event_type for event in control_events] == ["start", "pause"]
        assert control_events[-1].reason_code == "client_audio"
        assert json.loads(control_events[-1].payload_json) == {
            "reason_code": "client_audio",
            "source": "atomic_technical_pause",
        }


def test_atomic_technical_pause_rolls_back_autopilot_fence_on_stop_failure(
        api_clients: ApiClients, monkeypatch):
    import app.main as main_mod

    _enable_p0a(monkeypatch)
    positioned = api_clients.account.put("/live/state", json={
        "kind": "cursor",
        "payload": {
            "sessionId": SESSION_ID,
            "screen": "present",
            "itemIdx": 0,
            "turnIdx": 0,
            "responseRole": "命名",
            "cueLevel": 0,
            "recording": "idle",
            "selfStart": False,
        },
    })
    assert positioned.status_code == 200, positioned.text
    started = _start(api_clients)
    assert started.status_code == 200, started.text
    runtime_snapshot = api_clients.account.get(
        f"/sessions/{SESSION_ID}/runtime").json()
    live_snapshot = api_clients.account.get("/live/console-state").json()
    body = {
        "idempotency_key": "technical-pause-p0a-rollback-0001",
        "expected_revision": runtime_snapshot["revision"],
        "expected_live_wseq": live_snapshot["cursor"]["wseq"],
        "error_code": "client_persistence",
    }

    def fail_runtime_pause(*_args, **_kwargs):
        raise RuntimeError("injected technical stop projection failure")

    monkeypatch.setattr(
        main_mod, "_pause_runtime_in_transaction", fail_runtime_pause)
    with pytest.raises(
            RuntimeError, match="injected technical stop projection failure"):
        api_clients.account.post(
            f"/sessions/{SESSION_ID}/technical-pause", json=body)

    with Session(api_clients.engine) as session:
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        assert control is not None and control.status == "waiting_tts"
        assert control.current_command_id is not None
        assert runtime_state is not None and runtime_state.status == "active"
        assert list(session.exec(select(InteractionEvent).where(
            InteractionEvent.session_id == SESSION_ID))) == []
        assert list(session.exec(select(TechnicalPauseReceipt).where(
            TechnicalPauseReceipt.session_id == SESSION_ID))) == []
        control_events = list(session.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.session_id == SESSION_ID).order_by(
                AutopilotControlEvent.event_seq)))
        assert [event.event_type for event in control_events] == ["start"]


def test_start_rejects_legacy_hot_mic_until_device_reports_stopped(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    hot_cursor = {
        "sessionId": SESSION_ID,
        "screen": "record",
        "itemIdx": 0,
        "turnIdx": 0,
        "responseRole": "命名",
        "cueLevel": 0,
        "recording": "armed",
        "selfStart": True,
    }
    armed = api_clients.account.put(
        "/live/state", json={"kind": "cursor", "payload": hot_cursor})
    assert armed.status_code == 200, armed.text

    cursor_blocked = _start(api_clients)
    assert cursor_blocked.status_code == 409
    assert cursor_blocked.json()["detail"]["code"] == (
        "autopilot_patient_microphone_active")
    with Session(api_clients.engine) as session:
        assert session.get(SessionAutopilotState, SESSION_ID) is None
        assert list(session.exec(select(RuntimeCommand))) == []

    active = api_clients.device.put(
        "/live/state",
        headers=api_clients.device_headers,
        json={"kind": "patientRec", "payload": {
            "active": True,
            "turnKey": "itm-0001#1",
            "sessionId": SESSION_ID,
        }},
    )
    assert active.status_code == 200, active.text
    stopped_command = dict(hot_cursor)
    stopped_command.update({
        "screen": "present", "recording": "stopped", "selfStart": False,
    })
    stopped = api_clients.account.put(
        "/live/state",
        json={"kind": "cursor", "payload": stopped_command},
    )
    assert stopped.status_code == 200, stopped.text

    device_still_hot = _start(api_clients)
    assert device_still_hot.status_code == 409
    assert device_still_hot.json()["detail"]["code"] == (
        "autopilot_patient_microphone_active")
    with Session(api_clients.engine) as session:
        assert session.get(SessionAutopilotState, SESSION_ID) is None
        assert list(session.exec(select(RuntimeCommand))) == []

    inactive = api_clients.device.put(
        "/live/state",
        headers=api_clients.device_headers,
        json={"kind": "patientRec", "payload": {
            "active": False,
            "turnKey": "itm-0001#1",
            "sessionId": SESSION_ID,
        }},
    )
    assert inactive.status_code == 200, inactive.text
    accepted = _start(api_clients)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "waiting_tts"


def test_patient_rec_failure_fences_autopilot_as_device_in_runtime_pause_txn(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    cursor = api_clients.account.put(
        "/live/state",
        json={
            "kind": "cursor",
            "payload": {
                "sessionId": SESSION_ID,
                "screen": "present",
                "itemIdx": 0,
                "turnIdx": 0,
                "responseRole": "命名",
                "cueLevel": 0,
                "recording": "stopped",
                "selfStart": False,
            },
        },
    )
    assert cursor.status_code == 200, cursor.text
    started = _start(api_clients)
    assert started.status_code == 200, started.text
    failure_id = "11111111-1111-4111-8111-111111111111"

    failed = api_clients.device.put(
        "/live/state",
        headers=api_clients.device_headers,
        json={
            "kind": "patientRec",
            "payload": {
                "active": False,
                "turnKey": "itm-0001#1",
                "sessionId": SESSION_ID,
                "failureCode": "microphone_permission_denied",
                "failureId": failure_id,
            },
        },
    )
    assert failed.status_code == 200, failed.text

    with Session(api_clients.engine) as session:
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        assert control is not None
        assert control.mode == "autonomous"
        assert control.status == "paused"
        assert control.current_command_id is None
        assert control.last_error_code == "microphone_permission_denied"
        assert runtime_state is not None and runtime_state.status == "paused"
        commands = list(session.exec(select(RuntimeCommand)))
        assert len(commands) == 1 and commands[0].state == "pending"
        events = list(session.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))
        assert [event.event_type for event in events] == ["start", "pause"]
        assert events[-1].actor_type == "device"
        assert events[-1].actor_id
        assert events[-1].reason_code == "microphone_permission_denied"
        assert json.loads(events[-1].payload_json) == {
            "reason_code": "microphone_permission_denied",
            "source": "patient_rec_failure",
        }
        interactions = list(session.exec(select(InteractionEvent).where(
            InteractionEvent.session_id == SESSION_ID)))
        assert len(interactions) == 1
        assert interactions[0].event_type == "technical_pause"
        assert interactions[0].attempt_id is None

    # Same device failure id is a pure ACK and cannot create a second fence.
    replay = api_clients.device.put(
        "/live/state",
        headers=api_clients.device_headers,
        json={
            "kind": "patientRec",
            "payload": {
                "active": False,
                "turnKey": "itm-0001#1",
                "sessionId": SESSION_ID,
                "failureCode": "microphone_permission_denied",
                "failureId": failure_id,
            },
        },
    )
    assert replay.status_code == 200, replay.text
    with Session(api_clients.engine) as session:
        assert len(list(session.exec(select(AutopilotControlEvent)))) == 2
        assert len(list(session.exec(select(InteractionEvent)))) == 1


def test_abort_fences_autopilot_before_committing_runtime_terminal_fact(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    started = _start(api_clients)
    assert started.status_code == 200, started.text

    aborted = api_clients.account.post(
        f"/sessions/{SESSION_ID}/abort",
        json={
            "reason_code": "researcher_decision",
            "expected_revision": 0,
            "idempotency_key": "abort-autopilot-researcher-decision-0001",
        },
    )
    assert aborted.status_code == 200, aborted.text
    assert aborted.json()["status"] == "aborted"

    with Session(api_clients.engine) as session:
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        assert control is not None
        assert control.mode == "autonomous"
        assert control.status == "paused"
        assert control.current_command_id is None
        assert control.last_error_code == "session_aborted"
        assert runtime_state is not None and runtime_state.status == "aborted"
        events = list(session.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))
        assert [event.event_type for event in events] == ["start", "pause"]
        assert events[-1].actor_type == "researcher"
        assert events[-1].actor_id == "ACTOR-P0A-HTTP"
        assert json.loads(events[-1].payload_json) == {
            "reason_code": "session_aborted",
            "source": "session_abort",
        }


def test_cloud_consent_revoke_fences_admitted_autopilot_and_runtime_together(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    now = datetime.now()
    with Session(api_clients.engine) as session:
        plan = VisitPlan(
            plan_id="VP-P0A-CLOUD-REVOKE",
            protocol_slot_key="slot-p0a-cloud-revoke",
            patient_id=PATIENT_ID,
            scheduled_date=now.date(),
            queue_order=0,
            session_sitting_no=1,
            week_no=2,
            phase_type="正式训练",
            event_line="正式训练",
            item_bank_version_id=BANK.version_id,
            item_bank_definition_digest=(
                content.item_bank_definition_digest(FIRST_ONLY_BANK)),
            autopilot_protocol_version_id=PROTOCOL["protocol_version_id"],
            autopilot_protocol_definition_digest=(
                content.autopilot_protocol_definition_digest(PROTOCOL)),
            is_simulation=True,
            data_classification="simulation",
            status="started",
            revision=3,
            created_by="ACTOR-P0A-HTTP",
            created_at=now,
            updated_at=now,
            approved_by="ACTOR-P0A-HTTP",
            approved_at=now,
            started_by="ACTOR-P0A-HTTP",
            started_at=now,
        )
        session.add(plan)
        session.flush()
        training = session.get(TrainSession, SESSION_ID)
        patient = session.get(Patient, PATIENT_ID)
        assert training is not None and patient is not None
        training.visit_plan_id = plan.plan_id
        training.training_date = now.date()
        training.trainer_id = plan.started_by
        patient.cloud_processing_allowed = True
        patient.cloud_processing_provider_id = "test-cloud-provider"
        patient.cloud_processing_notice_version = "notice-test-v1"
        patient.cloud_processing_consented_at = now
        session.add(training)
        session.add(patient)
        session.commit()

    started = _start(api_clients)
    assert started.status_code == 200, started.text
    snapshot_response = api_clients.account.get(f"/patients/{PATIENT_ID}")
    assert snapshot_response.status_code == 200, snapshot_response.text
    snapshot = snapshot_response.json()
    revoked = api_clients.account.patch(
        f"/patients/{PATIENT_ID}/cloud-processing",
        json={
            "allowed": False,
            "expected": {
                "allowed": snapshot["cloud_processing_allowed"],
                "provider_id": snapshot["cloud_processing_provider_id"],
                "notice_version": snapshot["cloud_processing_notice_version"],
                "consented_at": snapshot["cloud_processing_consented_at"],
                    "revoked_at": snapshot["cloud_processing_revoked_at"],
                    "withdrawal_status": snapshot["withdrawal_status"],
                    "governance_revision": snapshot["governance_revision"],
                },
            "policy_provider_id": None,
            "policy_notice_version": None,
        },
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["cloud_processing_allowed"] is False

    with Session(api_clients.engine) as session:
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        assert control is not None
        assert control.mode == "autonomous"
        assert control.status == "paused"
        assert control.current_command_id is None
        assert control.last_error_code == "cloud_processing_revoked"
        assert runtime_state is not None and runtime_state.status == "paused"
        events = list(session.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))
        assert [event.event_type for event in events] == ["start", "pause"]
        assert events[-1].actor_type == "system"
        assert events[-1].actor_id is None
        assert json.loads(events[-1].payload_json) == {
            "reason_code": "cloud_processing_revoked",
            "source": "cloud_processing_consent_revoked",
        }


def test_researcher_pause_rolls_back_control_if_runtime_projection_fails(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    started = _start(api_clients)
    assert started.status_code == 200, started.text

    import app.main as main_mod

    def fail_runtime_pause(*_args, **_kwargs):
        raise RuntimeError("injected runtime pause failure")

    monkeypatch.setattr(
        main_mod, "_pause_runtime_in_transaction", fail_runtime_pause)
    with pytest.raises(RuntimeError, match="injected runtime pause failure"):
        api_clients.account.post(f"/sessions/{SESSION_ID}/pause")

    with Session(api_clients.engine) as session:
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        events = list(session.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))
        assert control is not None
        assert control.status == "waiting_tts"
        assert control.current_command_id is not None
        assert control.revision == 1
        assert runtime_state is not None and runtime_state.status == "active"
        assert [event.event_type for event in events] == ["start"]


class _WorkerAsr:
    version = "worker-asr-v1"
    data_boundary = "local"
    provider_id = None

    def __init__(self, *, fails: bool = False):
        self.fails = fails
        self.calls = 0

    def transcribe(self, _audio_bytes, _hotwords):
        self.calls += 1
        if self.fails:
            raise RuntimeError("injected provider failure")
        return asr.AsrResult(
            "胡萝卜", 0.96, self.version, hotword_hit=True)


class _BlockingWorkerAsr(_WorkerAsr):
    """Provider double whose completion is controlled by persisted-state tests."""

    def __init__(self, *, fails: bool = False):
        super().__init__(fails=fails)
        self.entered = threading.Event()
        self.release = threading.Event()

    def transcribe(self, _audio_bytes, _hotwords):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise AssertionError("test did not release blocked ASR provider")
        if self.fails:
            raise RuntimeError("injected delayed provider failure")
        return asr.AsrResult(
            "胡萝卜", 0.96, self.version, hotword_hit=True)


def _pause_drain_takeover(
        clients: ApiClients, record: dict, *, takeover_key: str) -> dict:
    paused = clients.account.post(f"/sessions/{SESSION_ID}/pause")
    assert paused.status_code == 200, paused.text
    drained = clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{record['command_key']}/drain-ack",
        headers=clients.device_headers,
    )
    assert drained.status_code == 200, drained.text
    taken = clients.account.post(
        f"/sessions/{SESSION_ID}/autopilot/takeover",
        json={
            "idempotency_key": takeover_key,
            "expected_revision": drained.json()["state_revision"],
        },
    )
    assert taken.status_code == 200, taken.text
    assert (taken.json()["mode"], taken.json()["status"]) == (
        "manual", "paused")
    return taken.json()


def test_authoritative_worker_derives_attempt_and_routes_feedback_once(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)
    fake_asr = _WorkerAsr()
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)

    _run_p0a_attempt_worker(SESSION_ID)
    with Session(api_clients.engine) as session:
        attempts = list(session.exec(select(AttemptEvent)))
        commands = list(session.exec(select(RuntimeCommand).order_by(
            RuntimeCommand.command_seq)))
        state = session.get(SessionAutopilotState, SESSION_ID)
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt.processing_status == "completed"
        assert (
            attempt.item_id,
            attempt.turn_seq,
            attempt.response_role,
            attempt.raw_audio_id,
            attempt.prompt_level,
            attempt.cue_type,
            attempt.duration_seconds,
            attempt.attempt_seq,
        ) == (
            BANK.single_element[0]["item_id"],
            1,
            "命名",
            capture["raw_audio_id"],
            0,
            None,
            1.5,
            1,
        )
        assert len(commands) == 3
        assert [(row.kind, row.state) for row in commands] == [
            ("tts", "succeeded"), ("record", "succeeded"), ("tts", "pending")]
        payload = json.loads(commands[-1].payload_json)
        assert payload["purpose"] == "feedback"
        assert payload["speech_text"] == BANK.single_element[0]["success_line"]
        assert state is not None and state.status == "waiting_tts"
        assert state.current_command_id == commands[-1].id
    assert fake_asr.calls == 1

    # A duplicate/recovery trigger after routing neither reprocesses nor issues a
    # second speech command.
    _run_p0a_attempt_worker(SESSION_ID)
    with Session(api_clients.engine) as session:
        assert len(list(session.exec(select(AttemptEvent)))) == 1
        assert len(list(session.exec(select(RuntimeCommand)))) == 3
    assert fake_asr.calls == 1

    # Even an exact body for the now-terminal autonomous attempt cannot turn the
    # legacy HTTP adapter into an idempotent read side-channel.
    manual_readback = api_clients.account.post(
        f"/sessions/{SESSION_ID}/attempts/process",
        json=_valid_attempt_body(capture["raw_audio_id"]),
    )
    assert manual_readback.status_code == 409, manual_readback.text
    assert manual_readback.json()["detail"]["code"] == (
        "autopilot_manual_control_locked")

    # Start idempotency is an audit-fact replay, not a fresh handoff.  Once the
    # autonomous worker has created attempt/interaction evidence, an exact retry
    # must still return the canonical current status without duplicating facts.
    canonical = api_clients.account.get(
        f"/sessions/{SESSION_ID}/autopilot/status")
    assert canonical.status_code == 200, canonical.text
    with Session(api_clients.engine) as session:
        before_counts = (
            len(list(session.exec(select(AttemptEvent)))),
            len(list(session.exec(select(InteractionEvent)))),
            len(list(session.exec(select(RuntimeCommand)))),
            len(list(session.exec(select(AutopilotControlEvent)))),
        )
    for _ in range(2):
        replay = _start(api_clients)
        assert replay.status_code == 200, replay.text
        assert replay.json() == canonical.json()
    with Session(api_clients.engine) as session:
        assert (
            len(list(session.exec(select(AttemptEvent)))),
            len(list(session.exec(select(InteractionEvent)))),
            len(list(session.exec(select(RuntimeCommand)))),
            len(list(session.exec(select(AutopilotControlEvent)))),
        ) == before_counts
    assert fake_asr.calls == 1


def test_pure_autonomous_terminal_ack_automatically_finishes_intervention(
        api_clients: ApiClients, monkeypatch):
    """No legacy evidence or researcher finish POST is needed after final ACK."""
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)
    fake_asr = _WorkerAsr()
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)

    _run_p0a_attempt_worker(SESSION_ID)
    with Session(api_clients.engine) as session:
        item_rows = list(session.exec(select(ItemEvent)))
        turn_rows = list(session.exec(select(TurnEvent)))
        attempts = list(session.exec(select(AttemptEvent)))
        assert len(item_rows) == len(turn_rows) == len(attempts) == 1
        item, turn, attempt = item_rows[0], turn_rows[0], attempts[0]
        assert (
            item.session_id,
            item.item_id,
            item.task_type.value,
            item.item_set_type.value,
            item.presentation_order,
        ) == (SESSION_ID, attempt.item_id, "单要素", "训练集", 1)
        assert (
            turn.source_attempt_id,
            turn.raw_audio_id,
            turn.response_role,
            turn.asr_text,
            turn.ai_answer_type,
        ) == (
            attempt.id,
            capture["raw_audio_id"],
            attempt.response_role,
            attempt.asr_text,
            attempt.operational_answer_type,
        )
        assert turn.confirmed_response_text is None
        assert turn.reviewer_id is None
        assert turn.reviewed_score is None
        assert turn.score_locked is False

    feedback = _device_next(api_clients)
    assert feedback is not None
    assert feedback["kind"] == "tts"
    terminal = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{feedback['command_key']}/acks",
        headers=api_clients.device_headers,
        json=_ack_body(
            feedback,
            ack_type="tts_ended",
            ack_key="ack-worker-feedback-ended-0001",
            device_event_seq=3,
            media_ended=True,
            media_duration_ms=600,
        ),
    )
    assert terminal.status_code == 200, terminal.text
    assert terminal.json()["status"] == "scope_completed"
    assert terminal.json()["command"] is None
    replay = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{feedback['command_key']}/acks",
        headers=api_clients.device_headers,
        json=_ack_body(
            feedback,
            ack_type="tts_ended",
            ack_key="ack-worker-feedback-ended-0001",
            device_event_seq=3,
            media_ended=True,
            media_duration_ms=600,
        ),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["status"] == "scope_completed"
    with Session(api_clients.engine) as session:
        state = session.get(SessionRuntimeState, SESSION_ID)
        assert state is not None
        assert state.status == "intervention_completed"
        assert state.intervention_completed_at is not None
        assert state.intervention_ended_by == "SERVER-AUTOPILOT"
        assert len(list(session.exec(select(SessionOutcomeSummary)))) == 1
        assert len(list(session.exec(select(AuditLog).where(
            AuditLog.action == "session_intervention_auto_complete")))) == 1
        assert len(list(session.exec(select(ItemEvent)))) == 1
        assert len(list(session.exec(select(TurnEvent)))) == 1
        live = session.get(LiveState, 1)
        assert live is not None
        assert json.loads(live.session_json)["runtimeStatus"] == "intervention_completed"
        assert json.loads(live.cursor_json)["screen"] == "thanks"
        assert live.patient_rec_json is None
    assert fake_asr.calls == 1


def test_terminal_ack_is_retained_when_autofinish_assessment_fails_closed(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    _drive_to_processing_attempt(api_clients)
    monkeypatch.setattr(asr, "get_engine", lambda: _WorkerAsr())
    _run_p0a_attempt_worker(SESSION_ID)

    # Corrupt only the derived completion projection.  The final device playback
    # is still a real fact and must remain committed for diagnosis.
    with Session(api_clients.engine) as session:
        turn = session.exec(select(TurnEvent)).one()
        session.delete(turn)
        session.commit()

    feedback = _device_next(api_clients)
    assert feedback is not None and feedback["kind"] == "tts"
    ack_key = "ack-autofinish-assessment-gap-0001"
    terminal = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{feedback['command_key']}/acks",
        headers=api_clients.device_headers,
        json=_ack_body(
            feedback,
            ack_type="tts_ended",
            ack_key=ack_key,
            device_event_seq=3,
            media_ended=True,
            media_duration_ms=600,
        ),
    )
    assert terminal.status_code == 200, terminal.text
    assert terminal.json()["status"] == "failed"
    assert terminal.json()["command"] is None

    with Session(api_clients.engine) as session:
        assert session.exec(select(RuntimeCommandAck).where(
            RuntimeCommandAck.idempotency_key == ack_key)).one() is not None
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        assert runtime_state is not None and runtime_state.status == "active"
        assert runtime_state.intervention_completed_at is None
        assert session.get(SessionOutcomeSummary, SESSION_ID) is None
        control = session.get(SessionAutopilotState, SESSION_ID)
        assert control is not None
        assert control.status == "failed"
        assert control.last_error_code == (
            "intervention_completion_evidence_incomplete")
        events = list(session.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))
        assert [event.event_type for event in events[-2:]] == [
            "scope_complete", "failure"]
        failure = events[-1]
        assert failure.reason_code == "intervention_completion_evidence_incomplete"
        assert failure.from_status == "scope_completed"
        assert failure.to_status == "failed"
        assert json.loads(failure.payload_json) == {
            "audio_evidenced_turns": 0,
            "completed_attempt_turns": 0,
            "error_code": "intervention_completion_evidence_incomplete",
            "expected_turns": 1,
            "issue_codes": "missing_turn",
            "issue_count": 1,
            "matched_turns": 0,
            "source": "intervention_completion_assessment",
        }


def test_started_autopilot_rejects_valid_manual_attempt_process_before_audio_lookup(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    started = _start(api_clients)
    assert started.status_code == 200, started.text

    # This is schema-valid legacy input, including a syntactically valid audio
    # reference.  The ownership interlock must win before asset/idempotent lookup,
    # so an old tab cannot process or read back an autonomous attempt side-channel.
    body = _valid_attempt_body("raw-old-manual-tab-0001")
    for _ in range(2):
        denied = api_clients.account.post(
            f"/sessions/{SESSION_ID}/attempts/process", json=body)
        assert denied.status_code == 409, denied.text
        assert denied.json()["detail"]["code"] == (
            "autopilot_manual_control_locked")

    with Session(api_clients.engine) as session:
        assert list(session.exec(select(AttemptEvent))) == []
        assert list(session.exec(select(InteractionEvent))) == []
        state = session.get(SessionAutopilotState, SESSION_ID)
        assert state is not None
        assert (state.mode, state.status, state.revision) == (
            "autonomous", "waiting_tts", 1)


def test_internal_worker_rejects_non_authoritative_attempt_body_without_evidence(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)
    wrong = AttemptProcessIn.model_validate(_valid_attempt_body(
        capture["raw_audio_id"], duration_seconds=1.75))

    with Session(api_clients.engine) as session:
        with pytest.raises(HTTPException) as caught:
            _process_attempt(
                SESSION_ID,
                wrong,
                Response(),
                session,
                control_plane="autopilot_worker",
            )
        assert caught.value.status_code == 409
        assert caught.value.detail["code"] == "autopilot_attempt_input_mismatch"
        session.rollback()

    with Session(api_clients.engine) as session:
        assert list(session.exec(select(AttemptEvent))) == []
        assert list(session.exec(select(InteractionEvent))) == []
        state = session.get(SessionAutopilotState, SESSION_ID)
        assert state is not None and state.status == "processing_attempt"


def test_worker_technical_failure_atomically_pauses_runtime_and_autopilot(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    _drive_to_processing_attempt(api_clients)
    fake_asr = _WorkerAsr(fails=True)
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)

    _run_p0a_attempt_worker(SESSION_ID)
    with Session(api_clients.engine) as session:
        attempt = session.exec(select(AttemptEvent)).one()
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        events = list(session.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))
        assert attempt.processing_status == "technical_failure"
        assert attempt.error_code == "asr_exception"
        assert control is not None
        assert control.status == "paused" and control.current_command_id is None
        assert control.last_error_code == "asr_exception"
        assert runtime_state is not None and runtime_state.status == "paused"
        assert [event.event_type for event in events] == ["start", "failure"]
        assert events[-1].reason_code == "asr_exception"
        assert json.loads(events[-1].payload_json) == {
            "error_code": "asr_exception",
            "source": "attempt_processing",
        }
    assert fake_asr.calls == 1


def test_worker_exception_before_attempt_creation_fails_closed_without_advancing(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    _drive_to_processing_attempt(api_clients)
    monkeypatch.setattr(audio_store, "find_blob", lambda _raw_audio_id: None)

    _run_p0a_attempt_worker(SESSION_ID)
    with Session(api_clients.engine) as session:
        assert list(session.exec(select(AttemptEvent))) == []
        commands = list(session.exec(select(RuntimeCommand)))
        assert len(commands) == 2
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        failure = session.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.event_type == "failure")).one()
        assert control is not None and control.status == "paused"
        assert control.current_command_id is None
        assert control.last_error_code == "autopilot_worker_exception"
        assert runtime_state is not None and runtime_state.status == "paused"
        assert failure.reason_code == "autopilot_worker_exception"


def test_pause_drain_takeover_fences_a_provider_result_already_in_flight(
        api_clients: ApiClients, monkeypatch):
    """A response from the old autonomous generation is observation-only."""
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)
    blocked_asr = _BlockingWorkerAsr()
    monkeypatch.setattr(asr, "get_engine", lambda: blocked_asr)

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(_run_p0a_attempt_worker, SESSION_ID)
        assert blocked_asr.entered.wait(timeout=10), "worker never entered ASR"
        try:
            takeover = pool.submit(
                _pause_drain_takeover,
                api_clients,
                capture["record"],
                takeover_key="takeover-blocked-asr-success-0001",
            ).result(timeout=10)
            assert takeover["mode"] == "manual"
        finally:
            blocked_asr.release.set()
        worker.result(timeout=10)

    with Session(api_clients.engine) as session:
        attempt = session.exec(select(AttemptEvent)).one()
        interactions = list(session.exec(select(InteractionEvent).order_by(
            InteractionEvent.event_seq)))
        commands = list(session.exec(select(RuntimeCommand).order_by(
            RuntimeCommand.command_seq)))
        events = list(session.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)

        assert attempt.processing_status == "received"
        assert (
            attempt.asr_text,
            attempt.asr_confidence,
            attempt.asr_engine_version,
            attempt.operational_answer_type,
            attempt.operational_score,
            attempt.judge_engine_version,
        ) == (None, None, None, None, None, None)
        assert [event.event_type for event in interactions] == [
            "attempt_received"]
        assert [(command.kind, command.state) for command in commands] == [
            ("tts", "succeeded"), ("record", "succeeded")]
        assert [event.event_type for event in events] == [
            "start", "pause", "drain_complete", "takeover"]
        assert control is not None
        assert (control.mode, control.status, control.current_command_id) == (
            "manual", "paused", None)
        assert runtime_state is not None and runtime_state.status == "paused"
    assert blocked_asr.calls == 1


def test_old_provider_failure_cannot_repause_after_takeover_and_manual_resume(
        api_clients: ApiClients, monkeypatch):
    """A stale failure cannot undo the researcher's newer active runtime."""
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)
    blocked_asr = _BlockingWorkerAsr(fails=True)
    monkeypatch.setattr(asr, "get_engine", lambda: blocked_asr)

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(_run_p0a_attempt_worker, SESSION_ID)
        assert blocked_asr.entered.wait(timeout=10), "worker never entered ASR"
        try:
            pool.submit(
                _pause_drain_takeover,
                api_clients,
                capture["record"],
                takeover_key="takeover-blocked-asr-failure-0001",
            ).result(timeout=10)
            resumed = api_clients.account.post(f"/sessions/{SESSION_ID}/resume")
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()["status"] == "active"
            with Session(api_clients.engine) as session:
                resumed_revision = session.get(
                    SessionRuntimeState, SESSION_ID).revision
        finally:
            blocked_asr.release.set()
        worker.result(timeout=10)

    with Session(api_clients.engine) as session:
        attempt = session.exec(select(AttemptEvent)).one()
        interactions = list(session.exec(select(InteractionEvent).order_by(
            InteractionEvent.event_seq)))
        commands = list(session.exec(select(RuntimeCommand).order_by(
            RuntimeCommand.command_seq)))
        events = list(session.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)

        assert attempt.processing_status == "received"
        assert attempt.asr_text is None and attempt.asr_engine_version is None
        assert attempt.error_code is None
        assert [event.event_type for event in interactions] == [
            "attempt_received"]
        assert [(command.kind, command.state) for command in commands] == [
            ("tts", "succeeded"), ("record", "succeeded")]
        assert [event.event_type for event in events] == [
            "start", "pause", "drain_complete", "takeover"]
        assert control is not None
        assert (control.mode, control.status, control.current_command_id) == (
            "manual", "paused", None)
        assert runtime_state is not None
        assert (runtime_state.status, runtime_state.revision) == (
            "active", resumed_revision)
    assert blocked_asr.calls == 1


@pytest.mark.parametrize(("governance_change", "expected_error_code"), [
    ("patient_withdrawal", "autopilot_subject_withdrawn"),
    ("audio_quarantine", "autopilot_attempt_capture_invalid"),
    ("feature_disabled", "autopilot_p0a_disabled"),
])
def test_processing_attempt_governance_loss_skips_provider_and_pauses_atomically(
        api_clients: ApiClients, monkeypatch,
        governance_change: str, expected_error_code: str):
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)
    fake_asr = _WorkerAsr()
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)

    if governance_change == "feature_disabled":
        monkeypatch.delenv("ENABLE_AUTOPILOT_P0A_SIMULATION")
    else:
        with Session(api_clients.engine) as session:
            if governance_change == "patient_withdrawal":
                patient = session.get(Patient, PATIENT_ID)
                assert patient is not None
                patient.withdrawal_status = "withdrawn_during_processing"
                session.add(patient)
            else:
                audio = session.get(AudioAssetRow, capture["raw_audio_id"])
                assert audio is not None
                audio.withdrawn = True
                audio.withdrawal_status = "quarantined_during_processing"
                session.add(audio)
            session.commit()

    _run_p0a_attempt_worker(SESSION_ID)

    with Session(api_clients.engine) as session:
        assert list(session.exec(select(AttemptEvent))) == []
        assert list(session.exec(select(InteractionEvent))) == []
        commands = list(session.exec(select(RuntimeCommand).order_by(
            RuntimeCommand.command_seq)))
        events = list(session.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        live = session.get(LiveState, 1)

        assert [(command.kind, command.state) for command in commands] == [
            ("tts", "succeeded"), ("record", "succeeded")]
        assert [event.event_type for event in events] == ["start", "failure"]
        assert events[-1].reason_code == expected_error_code
        assert json.loads(events[-1].payload_json) == {
            "error_code": expected_error_code,
            "source": "worker_exception",
        }
        assert control is not None
        assert (control.mode, control.status, control.current_command_id) == (
            "autonomous", "paused", None)
        assert control.last_error_code == expected_error_code
        assert runtime_state is not None and runtime_state.status == "paused"
        assert live is not None
        assert json.loads(live.session_json)["paused"] is True
    assert fake_asr.calls == 0


def test_exact_drain_then_explicit_takeover_is_strict_idempotent_and_releases_manual(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    started = _start(api_clients)
    assert started.status_code == 200, started.text
    command = _device_next(api_clients)
    assert command is not None

    paused = api_clients.account.post(f"/sessions/{SESSION_ID}/pause")
    assert paused.status_code == 200, paused.text

    # A brand-new page context has no in-memory lastCommandKey.  /next cannot
    # disclose/replay a paused command, while the dedicated target projection
    # recovers exactly the opaque key needed for drain and nothing else.
    target_url = f"/sessions/{SESSION_ID}/autopilot/drain-target"
    assert api_clients.anonymous.get(target_url).status_code == 401
    assert api_clients.account.get(target_url).status_code == 403
    fresh_page = TestClient(app)
    try:
        paused_next = fresh_page.get(
            f"/sessions/{SESSION_ID}/autopilot/next",
            headers=api_clients.device_headers,
        )
        assert paused_next.status_code == 409
        assert not ({"command_key", "speech_text", "raw_audio_id"}
                    & _all_keys(paused_next.json()))
        target = fresh_page.get(
            target_url,
            headers=api_clients.device_headers,
        )
        assert target.status_code == 200, target.text
        assert target.json() == {
            "command_key": command["command_key"],
            "state_revision": 2,
        }
        assert set(target.json()) == {"command_key", "state_revision"}
        assert target.headers["cache-control"] == "private, no-store"
        recovered_command_key = target.json()["command_key"]
    finally:
        fresh_page.close()

    drain_url = (
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{recovered_command_key}/drain-ack"
    )
    anonymous = api_clients.anonymous.post(drain_url)
    assert anonymous.status_code == 401
    account_cannot_impersonate_device = api_clients.account.post(drain_url)
    assert account_cannot_impersonate_device.status_code == 403
    free_text = api_clients.device.post(
        drain_url,
        headers=api_clients.device_headers,
        json={"text": "client supplied drain claim"},
    )
    assert free_text.status_code == 422

    drained = api_clients.device.post(
        drain_url, headers=api_clients.device_headers)
    assert drained.status_code == 200, drained.text
    assert drained.json() == {"replayed": False, "state_revision": 3}
    replayed_drain = api_clients.device.post(
        drain_url, headers=api_clients.device_headers, json={})
    assert replayed_drain.status_code == 200, replayed_drain.text
    assert replayed_drain.json() == {"replayed": True, "state_revision": 3}
    replay_target = api_clients.device.get(
        f"/sessions/{SESSION_ID}/autopilot/drain-target",
        headers=api_clients.device_headers,
    )
    assert replay_target.status_code == 200, replay_target.text
    assert replay_target.json() == {
        "command_key": command["command_key"],
        "state_revision": 3,
    }

    takeover_url = f"/sessions/{SESSION_ID}/autopilot/takeover"
    takeover_body = {
        "idempotency_key": "takeover-p0a-http-0001",
        "expected_revision": 3,
    }
    assert api_clients.anonymous.post(
        takeover_url, json=takeover_body).status_code == 401
    assert api_clients.device.post(
        takeover_url,
        headers=api_clients.device_headers,
        json=takeover_body,
    ).status_code == 401
    extra_fact = api_clients.account.post(
        takeover_url,
        json={**takeover_body, "reason": "free text is forbidden"},
    )
    assert extra_fact.status_code == 422

    taken = api_clients.account.post(takeover_url, json=takeover_body)
    assert taken.status_code == 200, taken.text
    assert taken.json() == {
        "scope_key": "p0a_sim_first_single_v1",
        "mode": "manual",
        "status": "paused",
        "state_revision": 4,
        "server_owned": False,
        "current_command_kind": None,
        "last_error_code": None,
    }
    exact_retry = api_clients.account.post(takeover_url, json=takeover_body)
    assert exact_retry.status_code == 200, exact_retry.text
    assert exact_retry.json() == taken.json()
    changed_fact = api_clients.account.post(takeover_url, json={
        **takeover_body,
        "expected_revision": 4,
    })
    assert changed_fact.status_code == 409
    assert changed_fact.json()["detail"]["code"] == (
        "autopilot_idempotency_conflict")

    with Session(api_clients.engine) as session:
        state = session.get(SessionAutopilotState, SESSION_ID)
        events = list(session.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))
        assert state is not None
        assert (state.mode, state.status, state.revision) == (
            "manual", "paused", 4)
        assert state.current_command_id is None
        assert state.lease_owner is None
        assert [event.event_type for event in events] == [
            "start", "pause", "drain_complete", "takeover"]
        assert json.loads(events[-2].payload_json) == {
            "drained_command_id": events[-2].command_id,
        }
        assert json.loads(events[-1].payload_json) == {
            "expected_revision": 3,
            "reason_code": "researcher_explicit_takeover",
            "source": "account_takeover_endpoint",
        }
        assert not ({"text", "response_text", "transcript"}
                    & _all_keys(json.loads(events[-1].payload_json)))

    # Manual resume is deliberately impossible before takeover and available
    # immediately after the atomic ownership release.
    resumed = api_clients.account.post(f"/sessions/{SESSION_ID}/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "active"
    old_next = api_clients.device.get(
        f"/sessions/{SESSION_ID}/autopilot/next",
        headers=api_clients.device_headers,
    )
    assert old_next.status_code == 409
    assert old_next.json()["detail"]["code"] == "autopilot_not_active"
    old_ack = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{command['command_key']}/acks",
        headers=api_clients.device_headers,
        json=_ack_body(
            command,
            ack_type="tts_ended",
            ack_key="ack-after-manual-takeover-0001",
            device_event_seq=1,
            media_ended=True,
            media_duration_ms=500,
        ),
    )
    assert old_ack.status_code == 409
    assert old_ack.json()["detail"]["code"] == (
        "autopilot_command_not_current")
    old_target = api_clients.device.get(
        f"/sessions/{SESSION_ID}/autopilot/drain-target",
        headers=api_clients.device_headers,
    )
    assert old_target.status_code == 409
    assert old_target.json()["detail"]["code"] == (
        "autopilot_drain_target_unavailable")


def test_takeover_requires_exact_drain_and_drain_requires_issued_current_device(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    assert _start(api_clients).status_code == 200
    command = _device_next(api_clients)
    assert command is not None
    active_target = api_clients.device.get(
        f"/sessions/{SESSION_ID}/autopilot/drain-target",
        headers=api_clients.device_headers,
    )
    assert active_target.status_code == 409
    assert active_target.json()["detail"]["code"] == (
        "autopilot_drain_target_unavailable")
    still_running = api_clients.account.post(
        f"/sessions/{SESSION_ID}/autopilot/takeover",
        json={
            "idempotency_key": "takeover-before-pause-0001",
            "expected_revision": 1,
        },
    )
    assert still_running.status_code == 409
    assert still_running.json()["detail"]["code"] == (
        "autopilot_pause_required")
    assert api_clients.account.post(
        f"/sessions/{SESSION_ID}/pause").status_code == 200

    takeover = api_clients.account.post(
        f"/sessions/{SESSION_ID}/autopilot/takeover",
        json={
            "idempotency_key": "takeover-without-drain-0001",
            "expected_revision": 2,
        },
    )
    assert takeover.status_code == 409
    assert takeover.json()["detail"]["code"] == (
        "autopilot_takeover_drain_required")

    wrong_command = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        "wrong-command-key-0001/drain-ack",
        headers=api_clients.device_headers,
    )
    assert wrong_command.status_code == 409
    assert wrong_command.json()["detail"]["code"] == (
        "autopilot_drain_not_current")

    replacement = TestClient(app)
    try:
        paired = replacement.post(
            "/device/pair",
            headers={"X-Console-Pin": "246810"},
            json={"deviceId": "p0a-http-replacement-0001"},
        )
        assert paired.status_code == 200, paired.text
        replacement_headers = {
            "X-Device-Capability": paired.json()["capability"]}
        mismatched = replacement.post(
            f"/sessions/{SESSION_ID}/autopilot/commands/"
            f"{command['command_key']}/drain-ack",
            headers=replacement_headers,
        )
        assert mismatched.status_code == 409
        assert mismatched.json()["detail"]["code"] == (
            "autopilot_drain_device_mismatch")
        mismatched_target = replacement.get(
            f"/sessions/{SESSION_ID}/autopilot/drain-target",
            headers=replacement_headers,
        )
        assert mismatched_target.status_code == 409
        assert mismatched_target.json()["detail"]["code"] == (
            "autopilot_drain_device_mismatch")
    finally:
        replacement.close()

    with Session(api_clients.engine) as session:
        state = session.get(SessionAutopilotState, SESSION_ID)
        events = list(session.exec(select(AutopilotControlEvent)))
        assert state is not None and state.revision == 2
        assert state.mode == "autonomous"
        assert [event.event_type for event in events] == ["start", "pause"]


def test_device_failure_is_terminal_drain_proof_without_duplicate_event(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    assert _start(api_clients).status_code == 200
    command = _device_next(api_clients)
    assert command is not None
    failed = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{command['command_key']}/acks",
        headers=api_clients.device_headers,
        json=_ack_body(
            command,
            ack_type="tts_failed",
            ack_key="ack-terminal-device-failure-0001",
            device_event_seq=1,
            error_code="audio_playback_failed",
        ),
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "paused"
    # Control is already paused by the device failure; this call atomically
    # projects the runtime pause but must not fabricate a researcher pause event.
    assert api_clients.account.post(
        f"/sessions/{SESSION_ID}/pause").status_code == 200

    failure_target = api_clients.device.get(
        f"/sessions/{SESSION_ID}/autopilot/drain-target",
        headers=api_clients.device_headers,
    )
    assert failure_target.status_code == 200, failure_target.text
    assert failure_target.json() == {
        "command_key": command["command_key"],
        "state_revision": 2,
    }
    drained = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{command['command_key']}/drain-ack",
        headers=api_clients.device_headers,
    )
    assert drained.status_code == 200, drained.text
    assert drained.json() == {"replayed": True, "state_revision": 2}
    taken = api_clients.account.post(
        f"/sessions/{SESSION_ID}/autopilot/takeover",
        json={
            "idempotency_key": "takeover-device-failure-0001",
            "expected_revision": 2,
        },
    )
    assert taken.status_code == 200, taken.text
    assert (taken.json()["mode"], taken.json()["status"]) == (
        "manual", "paused")
    with Session(api_clients.engine) as session:
        events = list(session.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))
        assert [event.event_type for event in events] == [
            "start", "failure", "takeover"]


def test_drain_target_rejects_stale_generation_without_content_projection(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    assert _start(api_clients).status_code == 200
    assert api_clients.account.post(
        f"/sessions/{SESSION_ID}/pause").status_code == 200
    with Session(api_clients.engine) as session:
        state = session.get(SessionAutopilotState, SESSION_ID)
        assert state is not None
        state.control_generation += 1
        session.add(state)
        session.commit()

    stale = api_clients.device.get(
        f"/sessions/{SESSION_ID}/autopilot/drain-target",
        headers=api_clients.device_headers,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == (
        "autopilot_drain_target_invalid")
    assert not ({
        "command_key", "speech_text", "raw_audio_id", "item_id", "image_id",
    } & _all_keys(stale.json()))


def test_system_failure_proof_allows_drain_retry_and_takeover(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)
    monkeypatch.setattr(asr, "get_engine", lambda: _WorkerAsr(fails=True))
    _run_p0a_attempt_worker(SESSION_ID)

    # Governance can change after the immutable record_stopped proof.  It must
    # block new attempt processing without deadlocking safe ownership release.
    with Session(api_clients.engine) as session:
        patient = session.get(Patient, PATIENT_ID)
        audio = session.get(AudioAssetRow, capture["raw_audio_id"])
        assert patient is not None and audio is not None
        patient.withdrawal_status = "withdrawn_after_failure"
        audio.withdrawn = True
        audio.withdrawal_status = "isolated"
        session.add(patient)
        session.add(audio)
        session.commit()

    drained = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{capture['record']['command_key']}/drain-ack",
        headers=api_clients.device_headers,
    )
    assert drained.status_code == 200, drained.text
    with Session(api_clients.engine) as session:
        state = session.get(SessionAutopilotState, SESSION_ID)
        assert state is not None
        expected_revision = state.revision
        before = list(session.exec(select(AutopilotControlEvent)))
        assert [event.event_type for event in before] == ["start", "failure"]
    assert drained.json() == {
        "replayed": True,
        "state_revision": expected_revision,
    }

    taken = api_clients.account.post(
        f"/sessions/{SESSION_ID}/autopilot/takeover",
        json={
            "idempotency_key": "takeover-system-failure-0001",
            "expected_revision": expected_revision,
        },
    )
    assert taken.status_code == 200, taken.text
    assert taken.json()["mode"] == "manual"


def test_drain_and_takeover_domain_failures_roll_back_every_staged_fact(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    assert _start(api_clients).status_code == 200
    command = _device_next(api_clients)
    assert command is not None
    assert api_clients.account.post(
        f"/sessions/{SESSION_ID}/pause").status_code == 200
    drain_url = (
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{command['command_key']}/drain-ack"
    )

    real_drain = autopilot_service.acknowledge_device_drain

    def stage_then_reject_drain(session: Session, **kwargs):
        real_drain(session, **kwargs)
        raise autopilot_service.AutopilotServiceError(
            "autopilot_injected_drain_failure", "injected drain rollback")

    monkeypatch.setattr(
        autopilot_service, "acknowledge_device_drain", stage_then_reject_drain)
    rejected_drain = api_clients.device.post(
        drain_url, headers=api_clients.device_headers)
    assert rejected_drain.status_code == 409
    with Session(api_clients.engine) as session:
        state = session.get(SessionAutopilotState, SESSION_ID)
        assert state is not None and state.revision == 2
        assert [event.event_type for event in session.exec(
            select(AutopilotControlEvent).order_by(
                AutopilotControlEvent.event_seq))] == ["start", "pause"]

    monkeypatch.setattr(
        autopilot_service, "acknowledge_device_drain", real_drain)
    assert api_clients.device.post(
        drain_url, headers=api_clients.device_headers).status_code == 200
    real_takeover = autopilot_service.takeover_autopilot_to_manual

    def stage_then_reject_takeover(session: Session, **kwargs):
        real_takeover(session, **kwargs)
        raise autopilot_service.AutopilotServiceError(
            "autopilot_injected_takeover_failure", "injected takeover rollback")

    monkeypatch.setattr(
        autopilot_service,
        "takeover_autopilot_to_manual",
        stage_then_reject_takeover,
    )
    rejected_takeover = api_clients.account.post(
        f"/sessions/{SESSION_ID}/autopilot/takeover",
        json={
            "idempotency_key": "takeover-rollback-http-0001",
            "expected_revision": 3,
        },
    )
    assert rejected_takeover.status_code == 409
    with Session(api_clients.engine) as session:
        state = session.get(SessionAutopilotState, SESSION_ID)
        events = list(session.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))
        assert state is not None
        assert (state.mode, state.status, state.revision) == (
            "autonomous", "paused", 3)
        assert [event.event_type for event in events] == [
            "start", "pause", "drain_complete"]
