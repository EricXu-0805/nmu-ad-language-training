"""P0a HTTP adapter: independent account/device principals and atomic ACK routing."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import hashlib
import json
import threading

import pytest
from fastapi import HTTPException, Response
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from app import (asr, audio_store, auth, autopilot_orchestration,
                 autopilot_service, content, db, device_capability,
                 evidence_ledger, llm_judge, provider_readiness,
                 repeat_intent)
from app import main as main_module
from app.llm_judge import LlmJudgement
from app.enums import AnswerType
from app.main import (
    AttemptProcessIn,
    _fail_closed_p0a_attempt_worker,
    _process_attempt,
    _run_p0a_attempt_worker,
    app,
)
from app.models import (
    AttemptCaptureProcessing,
    AttemptEvent,
    AuditLog,
    AudioAssetRow,
    AutopilotControlEvent,
    InteractionEvent,
    ItemEvent,
    LiveState,
    Patient,
    PatientDeviceCapability,
    PatientPauseReceipt,
    ProviderReadinessProbe,
    ResearchUser,
    RuntimeCommand,
    RuntimeCommandAck,
    Session as TrainSession,
    SessionAutopilotState,
    SessionOutcomeSummary,
    SessionRuntimeState,
    TechnicalPauseReceipt,
    TtsServeEvidence,
    TurnEvent,
    VisitPlan,
)


SESSION_ID = "S-P0A-HTTP"
OTHER_SESSION_ID = "S-P0A-HTTP-OTHER"
PATIENT_ID = "P-P0A-HTTP"
REPEAT_PROTOCOL = repeat_intent.active_protocol()
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
TWO_ONLY_BANK = replace(
    BANK,
    single_element=BANK.single_element[:2],
    double_element=[],
    multi_element=[],
    meta={
        **BANK.meta,
        "source_protocol_position_count": 2,
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
                repeat_protocol_version_id=REPEAT_PROTOCOL.version_id,
                repeat_protocol_definition_digest=(
                    REPEAT_PROTOCOL.definition_digest),
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
                repeat_protocol_version_id=REPEAT_PROTOCOL.version_id,
                repeat_protocol_definition_digest=(
                    REPEAT_PROTOCOL.definition_digest),
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


def _bind_api_bank(
        clients: ApiClients, monkeypatch,
        bank: content.ItemBank) -> None:
    """Bind a bounded bank before start without bypassing the public journey."""
    monkeypatch.setattr(content, "load_item_bank", lambda _path: bank)
    with Session(clients.engine) as session:
        train_session = session.get(TrainSession, SESSION_ID)
        assert train_session is not None
        train_session.item_bank_version_id = bank.version_id
        train_session.item_bank_definition_digest = (
            content.item_bank_definition_digest(bank))
        session.add(train_session)
        session.commit()


def _start(clients: ApiClients, **overrides):
    body = {"idempotency_key": START_KEY, "expected_revision": 0}
    body.update(overrides)
    return clients.account.post(
        f"/sessions/{SESSION_ID}/autopilot/start", json=body)


def _device_live(clients: ApiClients) -> dict:
    response = clients.device.get(
        "/live/state", headers=clients.device_headers)
    assert response.status_code == 200, response.text
    return response.json()


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


def _drive_to_processing_attempt(
        clients: ApiClients, *, stop_reason: str = "silence") -> dict:
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
            stop_reason=stop_reason,
            raw_audio_id=raw_audio_id,
            receipt_server_seq=receipt["serverSeq"],
            checksum=upload_fact["checksum"],
            byte_count=upload_fact["bytes"],
            duration_seconds=1.5,
        ),
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "processing_attempt"
    return {
        "record": record, "raw_audio_id": raw_audio_id,
        "receipt_server_seq": receipt["serverSeq"],
        "checksum": upload_fact["checksum"], "byte_count": upload_fact["bytes"],
        "duration_seconds": 1.5, "stop_reason": stop_reason,
    }


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


PATIENT_PAUSE_KEY = "patient_pause:0123456789abcdef0123456789abcdef"


def _patient_pause(clients: ApiClients, key: str = PATIENT_PAUSE_KEY):
    return clients.device.post(
        f"/sessions/{SESSION_ID}/patient-pause",
        headers=clients.device_headers,
        json={"idempotency_key": key},
    )


def test_patient_pause_is_capability_only_current_live_and_exactly_idempotent(
        api_clients: ApiClients, monkeypatch):
    """Account/anonymous/cross-session principals never reach the safety write."""
    account = api_clients.account.post(
        f"/sessions/{SESSION_ID}/patient-pause",
        json={"idempotency_key": PATIENT_PAUSE_KEY},
    )
    assert account.status_code == 403
    assert account.json()["detail"]["code"] == "device_capability_required"
    anonymous = api_clients.anonymous.post(
        f"/sessions/{SESSION_ID}/patient-pause",
        json={"idempotency_key": PATIENT_PAUSE_KEY},
    )
    assert anonymous.status_code == 401
    cross_session = api_clients.device.post(
        f"/sessions/{OTHER_SESSION_ID}/patient-pause",
        headers=api_clients.device_headers,
        json={"idempotency_key": PATIENT_PAUSE_KEY},
    )
    assert cross_session.status_code == 409
    assert cross_session.json()["detail"]["code"] == "device_session_mismatch"

    invalidated: list[str] = []
    real_processing = evidence_ledger.invalidate_processing_claims
    real_capture = evidence_ledger.invalidate_capture_processing_claims

    def track_processing(*args, **kwargs):
        invalidated.append("processing")
        return real_processing(*args, **kwargs)

    def track_capture(*args, **kwargs):
        invalidated.append("capture")
        return real_capture(*args, **kwargs)

    monkeypatch.setattr(
        evidence_ledger, "invalidate_processing_claims", track_processing)
    monkeypatch.setattr(
        evidence_ledger, "invalidate_capture_processing_claims", track_capture)
    with Session(api_clients.engine) as session:
        live = session.get(LiveState, 1)
        assert live is not None
        live.patient_rec_json = json.dumps({
            "active": True,
            "turnKey": "opaque-current-turn",
            "sessionId": SESSION_ID,
        })
        session.add(live)
        session.commit()

    first = _patient_pause(api_clients)
    assert first.status_code == 200, first.text
    assert first.json() == {
        "sessionId": SESSION_ID,
        "status": "paused",
        "runtimeRevision": 1,
        "liveSeq": 2,
        "eventSeq": 1,
        "idempotent": False,
    }
    assert invalidated == ["processing", "capture"]

    replay = _patient_pause(api_clients)
    assert replay.status_code == 200, replay.text
    assert replay.json() == {**first.json(), "idempotent": True}
    assert invalidated == ["processing", "capture"]
    different_key = _patient_pause(
        api_clients, "patient_pause:11111111111111111111111111111111")
    assert different_key.status_code == 409
    assert different_key.json()["detail"]["code"] == "patient_pause_runtime_inactive"

    with Session(api_clients.engine) as session:
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        live = session.get(LiveState, 1)
        events = list(session.exec(select(InteractionEvent).where(
            InteractionEvent.session_id == SESSION_ID)))
        receipts = list(session.exec(select(PatientPauseReceipt).where(
            PatientPauseReceipt.session_id == SESSION_ID)))
        patient = session.get(Patient, PATIENT_ID)
        train_session = session.get(TrainSession, SESSION_ID)
        assert runtime_state is not None
        assert (runtime_state.status, runtime_state.revision) == ("paused", 1)
        assert live is not None and live.patient_rec_json is None
        assert json.loads(live.session_json or "{}")["paused"] is True
        assert [event.event_type for event in events] == ["patient_requested_pause"]
        assert json.loads(events[0].payload_json) == {
            "request_id_sha256": hashlib.sha256(
                PATIENT_PAUSE_KEY.encode("utf-8")).hexdigest(),
        }
        assert len(receipts) == 1
        assert patient is not None and not (patient.withdrawal_status or "").strip()
        assert train_session is not None
        assert train_session.session_id == SESSION_ID  # pause never aborts/deletes the visit


def test_patient_pause_concurrent_exact_retry_commits_one_fact(
        api_clients: ApiClients):
    barrier = threading.Barrier(2)

    def submit():
        barrier.wait(timeout=5)
        with TestClient(app) as device:
            return device.post(
                f"/sessions/{SESSION_ID}/patient-pause",
                headers=api_clients.device_headers,
                json={"idempotency_key": PATIENT_PAUSE_KEY},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _index: submit(), range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    assert sorted(response.json()["idempotent"] for response in responses) == [False, True]
    with Session(api_clients.engine) as session:
        assert len(list(session.exec(select(InteractionEvent).where(
            InteractionEvent.event_type == "patient_requested_pause")))) == 1
        assert len(list(session.exec(select(PatientPauseReceipt)))) == 1


def test_patient_pause_old_receipt_cannot_authorize_a_later_pause_epoch(
        api_clients: ApiClients):
    first = _patient_pause(api_clients)
    assert first.status_code == 200, first.text

    resumed = api_clients.account.post(f"/sessions/{SESSION_ID}/resume")
    assert resumed.status_code == 200, resumed.text
    second_key = "patient_pause:22222222222222222222222222222222"
    second = _patient_pause(api_clients, second_key)
    assert second.status_code == 200, second.text

    obsolete = _patient_pause(api_clients)
    assert obsolete.status_code == 409
    assert obsolete.json()["detail"]["code"] == "patient_pause_replay_superseded"
    with Session(api_clients.engine) as session:
        assert len(list(session.exec(select(InteractionEvent).where(
            InteractionEvent.event_type == "patient_requested_pause")))) == 2
        assert len(list(session.exec(select(PatientPauseReceipt)))) == 2


@pytest.mark.parametrize("payload", [
    {},
    {"request_id_sha256": None},
    {"request_id_sha256": "A" * 64},
    {"request_id_sha256": "a" * 63},
])
def test_patient_pause_ledger_requires_one_lowercase_sha256(payload):
    with pytest.raises(ValueError, match="request_id_sha256"):
        evidence_ledger.validate_event_payload("patient_requested_pause", payload)
    assert evidence_ledger.validate_event_payload(
        "patient_requested_pause", {"request_id_sha256": "a" * 64}) == {
            "request_id_sha256": "a" * 64,
        }


def test_patient_pause_fences_autopilot_without_forging_technical_failure(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    started = _start(api_clients)
    assert started.status_code == 200, started.text

    paused = _patient_pause(api_clients)
    assert paused.status_code == 200, paused.text
    with Session(api_clients.engine) as session:
        control = session.get(SessionAutopilotState, SESSION_ID)
        assert control is not None
        assert control.status == "paused"
        assert control.current_command_id is None
        assert control.last_error_code == "patient_requested_pause"
        events = list(session.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.session_id == SESSION_ID).order_by(
                AutopilotControlEvent.event_seq)))
        assert [event.event_type for event in events] == ["start", "pause"]
        assert json.loads(events[-1].payload_json) == {
            "reason_code": "patient_requested_pause",
            "source": "patient_requested_pause",
        }
        interactions = list(session.exec(select(InteractionEvent).where(
            InteractionEvent.session_id == SESSION_ID)))
        assert [event.event_type for event in interactions] == [
            "patient_requested_pause"]


def test_patient_pause_rolls_back_fence_event_receipt_and_runtime_together(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    started = _start(api_clients)
    assert started.status_code == 200, started.text
    before_live = _device_live(api_clients)

    def fail_after_fence(*_args, **_kwargs):
        raise RuntimeError("patient-pause-rollback-sentinel")

    monkeypatch.setattr(
        main_module, "_pause_runtime_in_transaction", fail_after_fence)
    with pytest.raises(RuntimeError, match="patient-pause-rollback-sentinel"):
        _patient_pause(api_clients)

    with Session(api_clients.engine) as session:
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        assert control is not None and control.status == "waiting_tts"
        assert control.current_command_id is not None
        assert runtime_state is not None
        assert (runtime_state.status, runtime_state.revision) == ("active", 0)
        assert list(session.exec(select(InteractionEvent).where(
            InteractionEvent.event_type == "patient_requested_pause"))) == []
        assert list(session.exec(select(PatientPauseReceipt))) == []
        controls = list(session.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.session_id == SESSION_ID).order_by(
                AutopilotControlEvent.event_seq)))
        assert [event.event_type for event in controls] == ["start"]
    assert _device_live(api_clients) == before_live


def test_patient_pause_recovery_only_capability_is_rejected_before_handler(
        api_clients: ApiClients):
    token = api_clients.device_headers["X-Device-Capability"]
    token_hash = device_capability.token_hash(token)
    with Session(api_clients.engine) as session:
        capability = session.get(PatientDeviceCapability, token_hash)
        assert capability is not None
        capability.recovery_only_at = datetime.now()
        capability.active_session_key = None
        session.add(capability)
        session.commit()
    denied = _patient_pause(api_clients)
    assert denied.status_code == 401
    assert denied.json()["code"] == "device_capability_recovery_only"


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


def test_start_over_http_fails_closed_on_exact_stale_protocol_version_with_zero_writes(
        api_clients: ApiClients, monkeypatch):
    """HTTP-adapter counterpart of the service-level protocol-version-
    mismatch test: a genuine old (version, digest) binding pair -- not a
    corrupted or missing one -- must 409 with the exact stable code before
    any runtime/live/session-mutation/control/command/ack/event write.
    """
    _enable_p0a(monkeypatch)
    stale_version_id = "autopilot-v1-20260717"
    stale_digest = (
        "7e1f812972a07b80f4e88c01ae838254d2fbaae5e1f8c6d52aa06a18de61eccb")
    with Session(api_clients.engine) as session:
        legacy = session.get(TrainSession, SESSION_ID)
        assert legacy is not None
        assert legacy.autopilot_protocol_version_id == PROTOCOL["protocol_version_id"]
        legacy.autopilot_protocol_version_id = stale_version_id
        legacy.autopilot_protocol_definition_digest = stale_digest
        session.add(legacy)
        session.commit()

    def _snapshot():
        with Session(api_clients.engine) as session:
            train_session = session.get(TrainSession, SESSION_ID)
            runtime_state = session.get(SessionRuntimeState, SESSION_ID)
            live_rows = list(session.exec(select(LiveState)))
            return (
                (train_session.autopilot_protocol_version_id,
                 train_session.autopilot_protocol_definition_digest)
                if train_session is not None else None,
                (runtime_state.status, runtime_state.revision)
                if runtime_state is not None else None,
                tuple((row.id, row.seq, row.session_json) for row in live_rows),
                session.get(SessionAutopilotState, SESSION_ID),
                list(session.exec(select(RuntimeCommand))),
                list(session.exec(select(RuntimeCommandAck))),
                list(session.exec(select(AutopilotControlEvent))),
            )

    before = _snapshot()
    assert before[0] == (stale_version_id, stale_digest)
    assert before[3] is None
    assert before[4] == []
    assert before[5] == []
    assert before[6] == []

    denied = _start(api_clients)
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "autopilot_protocol_version_mismatch"

    after = _snapshot()
    assert after == before


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


def test_next_projection_omits_absent_response_path_over_real_http(
        api_clients: ApiClients, monkeypatch):
    """初始 question 的 /autopilot/next JSON 里不得出现 response_path。

    默认 response_model 会把 None 序列化成 response_path:null，设备端严格 parser
    因这个多余键拒绝整条 200 投影，老人端卡在"自动流程暂时无法确认"。
    """
    _enable_p0a(monkeypatch)
    assert _start(api_clients).status_code == 200

    response = api_clients.device.get(
        f"/sessions/{SESSION_ID}/autopilot/next",
        headers=api_clients.device_headers,
    )
    assert response.status_code == 200, response.text
    # 原始字节层面核对：不依赖 json() 的键遍历。
    assert "response_path" not in response.text
    projection = response.json()
    assert projection["kind"] == "tts"
    payload = projection["payload"]
    assert sorted(payload) == ["purpose", "speech_key", "speech_text"]
    assert payload["purpose"] == "question"
    assert (projection["prompt_level"], projection["attempt_seq"]) == (0, 1)


def test_ack_receipt_nests_the_same_omission_without_dropping_explicit_nulls(
        api_clients: ApiClients, monkeypatch):
    """首条 tts_started ACK 的嵌套 command 同样不得带 response_path。

    只给 /next 加路由级 exclude_none 是不够的：浏览器解析首条 question 之后立刻
    POST tts_started，收据里嵌的是同一个 NextCommandProjection，会在这里第二次
    被严格 parser 拒掉，黄金链照断。而路由级 exclude_none 又会顺手吃掉收据顶层
    那些必须显式保留的 null——所以省略必须落在字段上。
    """
    _enable_p0a(monkeypatch)
    assert _start(api_clients).status_code == 200
    command = _device_next(api_clients)
    assert command is not None and command["kind"] == "tts"

    ack_path = (f"/sessions/{SESSION_ID}/autopilot/commands/"
                f"{command['command_key']}/acks")
    body = _ack_body(
        command,
        ack_type="tts_started",
        ack_key="ack-response-path-omission-0001",
        device_event_seq=1,
    )
    acked = api_clients.device.post(
        ack_path, headers=api_clients.device_headers, json=body)
    assert acked.status_code == 200, acked.text
    assert "response_path" not in acked.text

    receipt = acked.json()
    nested = receipt["command"]
    assert nested["state"] == "started"
    assert sorted(nested["payload"]) == ["purpose", "speech_key", "speech_text"]

    # 精确重放的收据故意不投影命令。这个 null 必须原样留在 JSON 里——路由级
    # exclude_none 会把整个 command 键删掉，设备端的闭集校验随即失败。
    replayed = api_clients.device.post(
        ack_path, headers=api_clients.device_headers, json=body)
    assert replayed.status_code == 200, replayed.text
    replay_receipt = replayed.json()
    assert replay_receipt["replayed"] is True
    assert "command" in replay_receipt and replay_receipt["command"] is None


def test_non_null_first_level_response_path_survives_real_serialization():
    """字段级省略只吞掉缺席的分支证据；非空一级分支必须照常出现在 JSON 里。"""
    branch = autopilot_service.DeviceTtsPayload(
        speech_key="cue1.close",
        speech_text="一级线索话术",
        purpose="cue",
        response_path="close",
    )
    # 不传 exclude_none：省略规则挂在字段上，默认序列化就该是最终形状。
    serialized = branch.model_dump(mode="json")
    assert serialized["response_path"] == "close"
    assert sorted(serialized) == [
        "purpose", "response_path", "speech_key", "speech_text"]

    absent = autopilot_service.DeviceTtsPayload(
        speech_key="q.initial", speech_text="首次提问", purpose="question")
    assert "response_path" not in absent.model_dump(mode="json")
    assert json.loads(absent.model_dump_json()) == {
        "speech_key": "q.initial", "speech_text": "首次提问", "purpose": "question"}


def test_live_state_carries_cross_device_ownership_wake_only_when_autonomous(
        api_clients: ApiClients, monkeypatch):
    """跨设备唤醒：console 启动不推进 live seq，同一个 seq 上必须从 null 变出唤醒。"""
    _enable_p0a(monkeypatch)

    before = _device_live(api_clients)
    assert before["autopilotWake"] is None          # disabled scope 不发唤醒
    seq_before = before["seq"]

    started = _start(api_clients)
    assert started.status_code == 200, started.text
    assert (started.json()["mode"], started.json()["status"]) == (
        "autonomous", "waiting_tts")

    after = _device_live(api_clients)
    # 这正是真机故障的形状：seq 没动，患者端只有靠这个字段才知道该重探测一次。
    assert after["seq"] == seq_before
    wake = after["autopilotWake"]
    assert wake == {
        "sessionId": SESSION_ID,
        "stateRevision": started.json()["state_revision"],
    }
    assert wake["stateRevision"] >= 1
    # 投影只有这两个键：没有命令载荷/kind、没有 token、没有设备指纹、没有账号数据。
    assert sorted(wake) == ["sessionId", "stateRevision"]

    # 状态推进产生新 revision，唤醒随之前进。
    command = _device_next(api_clients)
    assert command is not None and command["kind"] == "tts"
    advanced = _device_live(api_clients)["autopilotWake"]
    assert advanced["sessionId"] == SESSION_ID
    assert advanced["stateRevision"] >= wake["stateRevision"]


def test_live_ownership_wake_requires_capability_bound_to_the_live_session(
        api_clients: ApiClients, monkeypatch):
    """唤醒读走的是同一道能力门禁：绑到别的场次的设备根本读不到这份快照。"""
    _enable_p0a(monkeypatch)
    assert _start(api_clients).status_code == 200

    with Session(api_clients.engine) as session:
        capability = session.exec(select(PatientDeviceCapability)).one()
        capability.session_id = OTHER_SESSION_ID
        capability.active_session_key = OTHER_SESSION_ID
        session.add(capability)
        session.commit()

    foreign = api_clients.device.get(
        "/live/state", headers=api_clients.device_headers)
    assert foreign.status_code in (401, 403, 409), foreign.text
    assert "autopilotWake" not in foreign.text


def test_manual_takeover_stops_the_cross_device_ownership_wake(
        api_clients: ApiClients, monkeypatch):
    """manual 不是服务器所有权：接管之后唤醒必须回到 null。"""
    _enable_p0a(monkeypatch)
    assert _start(api_clients).status_code == 200
    command = _device_next(api_clients)
    assert command is not None and command["kind"] == "tts"
    assert _device_live(api_clients)["autopilotWake"] is not None

    taken = _pause_drain_takeover(
        api_clients, command, takeover_key="takeover-wake-stop-0001")
    assert taken["mode"] == "manual"
    assert _device_live(api_clients)["autopilotWake"] is None


def test_exact_tts_is_server_derived_and_generic_text_route_is_locked(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    synthesized: list[str] = []

    def fake_speak(text: str):
        synthesized.append(text)
        return None, "exact-tts-test", False

    # The exact autopilot route has its own Qwen-only synthesizer; patching the
    # generic one would leave this test driving the real provider selection.
    monkeypatch.setattr("app.main.tts.speak_autopilot", fake_speak)
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
    # The shipped patient transport POSTs this route with no body and no
    # Content-Type; the success path must accept exactly that.
    exact = api_clients.device.post(
        exact_path, headers=api_clients.device_headers)
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

    monkeypatch.setattr("app.main.tts.speak_autopilot", blocked_speak)
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


@pytest.mark.parametrize("stop_kind", ["pause", "abort", "technical_pause"])
def test_stop_routes_use_one_cross_worker_lock_order(
        api_clients: ApiClients, monkeypatch, stop_kind):
    """All account stop paths fence Live/autopilot before runtime authority."""
    import app.main as main_mod

    _enable_p0a(monkeypatch)
    if stop_kind == "technical_pause":
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

    technical_body = None
    if stop_kind == "technical_pause":
        runtime_snapshot = api_clients.account.get(
            f"/sessions/{SESSION_ID}/runtime").json()
        live_snapshot = api_clients.account.get("/live/console-state").json()
        technical_body = {
            "idempotency_key": "technical-pause-lock-order-0001",
            "expected_revision": runtime_snapshot["revision"],
            "expected_live_wseq": live_snapshot["cursor"]["wseq"],
            "error_code": "client_audio",
        }

    order: list[str] = []
    position_checks = 0
    real_admission = main_mod._require_started_visit_plan_session
    real_live_lock = main_mod._live_row_for_update
    real_runtime_lock = main_mod._runtime_row_for_update
    real_pause = main_mod.autopilot_service.pause_autonomous_scope_for_researcher
    real_fence = main_mod.autopilot_service.fence_autonomous_scope_for_external_stop
    real_position_check = main_mod._technical_pause_active_position

    def track_admission(*args, **kwargs):
        order.append("session")
        return real_admission(*args, **kwargs)

    def track_live_lock(*args, **kwargs):
        order.append("live")
        return real_live_lock(*args, **kwargs)

    def track_runtime_lock(*args, **kwargs):
        order.append("runtime")
        return real_runtime_lock(*args, **kwargs)

    def track_pause(*args, **kwargs):
        order.append("autopilot")
        return real_pause(*args, **kwargs)

    def track_fence(*args, **kwargs):
        order.append("autopilot")
        return real_fence(*args, **kwargs)

    def track_position_check(*args, **kwargs):
        nonlocal position_checks
        position_checks += 1
        order.append(f"position_{position_checks}")
        return real_position_check(*args, **kwargs)

    monkeypatch.setattr(
        main_mod, "_require_started_visit_plan_session", track_admission)
    monkeypatch.setattr(main_mod, "_live_row_for_update", track_live_lock)
    monkeypatch.setattr(main_mod, "_runtime_row_for_update", track_runtime_lock)
    monkeypatch.setattr(
        main_mod.autopilot_service,
        "pause_autonomous_scope_for_researcher",
        track_pause,
    )
    monkeypatch.setattr(
        main_mod.autopilot_service,
        "fence_autonomous_scope_for_external_stop",
        track_fence,
    )
    monkeypatch.setattr(
        main_mod, "_technical_pause_active_position", track_position_check)

    if stop_kind == "pause":
        stopped = api_clients.account.post(f"/sessions/{SESSION_ID}/pause")
    elif stop_kind == "abort":
        stopped = api_clients.account.post(
            f"/sessions/{SESSION_ID}/abort",
            json={
                "reason_code": "researcher_decision",
                "expected_revision": 0,
                "idempotency_key": "abort-lock-order-researcher-0001",
            },
        )
    else:
        assert technical_body is not None
        stopped = api_clients.account.post(
            f"/sessions/{SESSION_ID}/technical-pause",
            json=technical_body,
        )
    assert stopped.status_code == 200, stopped.text

    assert order.index("session") < order.index("live") < order.index("autopilot")
    if stop_kind == "technical_pause":
        assert position_checks == 2
        assert (
            order.index("position_1")
            < order.index("autopilot")
            < order.index("position_2")
        )
    else:
        assert order.index("autopilot") < order.index("runtime")


def test_abort_revalidates_runtime_after_fence_and_rolls_back_on_race(
        api_clients: ApiClients, monkeypatch):
    """A post-preflight runtime change cannot commit a half-applied abort."""
    import app.main as main_mod

    _enable_p0a(monkeypatch)
    started = _start(api_clients)
    assert started.status_code == 200, started.text
    real_fence = main_mod.autopilot_service.fence_autonomous_scope_for_external_stop

    def fence_then_advance_runtime(db_session, **kwargs):
        changed = real_fence(db_session, **kwargs)
        db_session.execute(
            update(SessionRuntimeState)
            .where(SessionRuntimeState.session_id == SESSION_ID)
            .values(revision=SessionRuntimeState.revision + 1)
            .execution_options(synchronize_session=False)
        )
        return changed

    monkeypatch.setattr(
        main_mod.autopilot_service,
        "fence_autonomous_scope_for_external_stop",
        fence_then_advance_runtime,
    )
    raced = api_clients.account.post(
        f"/sessions/{SESSION_ID}/abort",
        json={
            "reason_code": "researcher_decision",
            "expected_revision": 0,
            "idempotency_key": "abort-post-lock-runtime-race-0001",
        },
    )
    assert raced.status_code == 409, raced.text
    assert raced.json()["detail"] == {
        "code": "session_abort_revision_conflict",
        "message": "场次运行修订已变化，请重新核对后中止",
        "current_revision": 1,
    }

    with Session(api_clients.engine) as session:
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        events = list(session.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.session_id == SESSION_ID).order_by(
                AutopilotControlEvent.event_seq)))
        assert control is not None and control.status == "waiting_tts"
        assert control.current_command_id is not None
        assert runtime_state is not None
        assert (runtime_state.status, runtime_state.revision) == ("active", 0)
        assert [event.event_type for event in events] == ["start"]


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
            repeat_protocol_version_id=REPEAT_PROTOCOL.version_id,
            repeat_protocol_definition_digest=(
                REPEAT_PROTOCOL.definition_digest),
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


class _EmptyTranscriptAsr(_WorkerAsr):
    """provider 明确成功、响应合法、但一个字都没识别出来。"""

    def transcribe(self, _audio_bytes, _hotwords):
        self.calls += 1
        return asr.AsrResult("", None, self.version)


class _DegradedAsr(_WorkerAsr):
    """引擎不可用/响应损坏的技术失败:asr_text 是 None,不是空字符串。"""

    def transcribe(self, _audio_bytes, _hotwords):
        self.calls += 1
        return asr.AsrResult(None, None, self.version)


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


class _BlockingWorkerJudge:
    """Judge provider double that blocks in ``judge`` until released."""

    version = "blocking-judge-test"
    data_boundary = "local"
    provider_id = None

    def __init__(self) -> None:
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def judge(self, _judge_input):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise AssertionError("test did not release blocked judge provider")
        return LlmJudgement(AnswerType.正确, 1.0, False, reason="命中目标词")


class _FailingBlockingWorkerJudge(_BlockingWorkerJudge):
    """Blocks like ``_BlockingWorkerJudge``, then raises on release."""

    def judge(self, _judge_input):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise AssertionError("test did not release blocked judge provider")
        raise RuntimeError("injected delayed judge failure")


class _WrongAnswerAsr(_WorkerAsr):
    """Fast, non-blocking ASR whose transcript never matches the target."""

    def transcribe(self, _audio_bytes, _hotwords):
        self.calls += 1
        return asr.AsrResult("西瓜", 0.9, self.version, hotword_hit=False)


class _FixedTranscriptAsr(_WorkerAsr):
    """Return one non-target transcript across a multi-attempt API journey."""

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def transcribe(self, _audio_bytes, _hotwords):
        self.calls += 1
        return asr.AsrResult(
            self.text,
            None if self.text == "" else 0.9,
            self.version,
            hotword_hit=False,
        )


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
        target = autopilot_orchestration.derive_worker_target(
            session, session_id=SESSION_ID)
        with pytest.raises(HTTPException) as caught:
            _process_attempt(
                SESSION_ID,
                wrong,
                Response(),
                session,
                control_plane="autopilot_worker",
                worker_target=target,
            )
        assert caught.value.status_code == 409
        assert caught.value.detail["code"] == "autopilot_attempt_input_mismatch"
        session.rollback()

    with Session(api_clients.engine) as session:
        assert list(session.exec(select(AttemptEvent))) == []
        assert list(session.exec(select(InteractionEvent))) == []
        state = session.get(SessionAutopilotState, SESSION_ID)
        assert state is not None and state.status == "processing_attempt"


def test_internal_worker_without_frozen_target_is_rejected_before_any_mutation(
        api_clients: ApiClients, monkeypatch):
    """A missing ``worker_target`` on the autopilot_worker plane is not a
    legitimate production call shape — it must fail before any provider I/O
    or write, never silently fall back to session-only behavior.
    """
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)
    body = AttemptProcessIn.model_validate(
        _valid_attempt_body(capture["raw_audio_id"]))
    never_called = _WorkerAsr()
    monkeypatch.setattr(asr, "get_engine", lambda: never_called)

    with Session(api_clients.engine) as session:
        with pytest.raises(RuntimeError, match="frozen worker_target"):
            _process_attempt(
                SESSION_ID,
                body,
                Response(),
                session,
                control_plane="autopilot_worker",
            )
        session.rollback()

    assert never_called.calls == 0
    with Session(api_clients.engine) as session:
        assert list(session.exec(select(AttemptEvent))) == []
        assert list(session.exec(select(InteractionEvent))) == []
        state = session.get(SessionAutopilotState, SESSION_ID)
        assert state is not None and state.status == "processing_attempt"
        assert state.current_command_id is not None


@pytest.mark.parametrize("field,mutate", [
    ("session_id", lambda t: t.session_id + "-OTHER"),
    ("record_command_id", lambda t: t.record_command_id + 999_000),
    ("raw_audio_id", lambda t: "raw-" + "0" * 32),
    ("control_generation", lambda t: t.control_generation + 1),
    ("runner_generation", lambda t: t.runner_generation + 1),
])
def test_internal_worker_rejects_a_target_mismatched_in_any_single_field(
        api_clients: ApiClients, monkeypatch, field, mutate):
    """Each of the 5 target fields is independently load-bearing. A target
    that mismatches in exactly one field, with everything else — including a
    ``body`` that legitimately matches the current authoritative attempt —
    unchanged, must still be rejected before any provider I/O or mutation. A
    coincidentally-matching body must never let a forged/lagged target ride
    through on any single field.
    """
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)
    body = AttemptProcessIn.model_validate(
        _valid_attempt_body(capture["raw_audio_id"]))
    never_called = _WorkerAsr()
    monkeypatch.setattr(asr, "get_engine", lambda: never_called)

    with Session(api_clients.engine) as session:
        real_target = autopilot_orchestration.derive_worker_target(
            session, session_id=SESSION_ID)
        session.rollback()
    mismatched_target = real_target.model_copy(
        update={field: mutate(real_target)})
    assert mismatched_target != real_target

    with Session(api_clients.engine) as session:
        with pytest.raises(autopilot_orchestration.AutopilotOrchestrationError) as caught:
            _process_attempt(
                SESSION_ID,
                body,
                Response(),
                session,
                control_plane="autopilot_worker",
                worker_target=mismatched_target,
            )
        assert caught.value.code == "autopilot_attempt_input_mismatch"
        session.rollback()

    assert never_called.calls == 0
    with Session(api_clients.engine) as session:
        assert list(session.exec(select(AttemptEvent))) == []
        assert list(session.exec(select(InteractionEvent))) == []
        state = session.get(SessionAutopilotState, SESSION_ID)
        assert state is not None and state.status == "processing_attempt"
        assert state.current_command_id is not None


def test_prepare_admission_independently_rechecks_target_within_its_own_lock(
        api_clients: ApiClients, monkeypatch):
    """A caller-side (pre-admission) target-freshness check and
    ``_prepare_capture_or_attempt_processing``'s own lock are two different
    transactions — the caller-side check alone cannot close the gap between
    them (control/runner generation could change in that gap even while
    ``body``/its raw audio stay identical). This deterministically proves
    admission's own lock-scoped target recheck is what actually protects it:
    the caller-side precheck is neutralized (forced to always report "still
    current"), and a forged target is still rejected — by admission's own
    independent recheck, not the (here defeated) precheck.
    """
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)
    body = AttemptProcessIn.model_validate(
        _valid_attempt_body(capture["raw_audio_id"]))
    never_called = _WorkerAsr()
    monkeypatch.setattr(asr, "get_engine", lambda: never_called)

    with Session(api_clients.engine) as session:
        real_target = autopilot_orchestration.derive_worker_target(
            session, session_id=SESSION_ID)
        session.rollback()
    forged_target = autopilot_orchestration.FrozenWorkerTarget(
        session_id=real_target.session_id,
        record_command_id=real_target.record_command_id + 999_000,
        raw_audio_id=real_target.raw_audio_id,
        control_generation=real_target.control_generation,
        runner_generation=real_target.runner_generation,
    )
    monkeypatch.setattr(
        autopilot_orchestration, "worker_target_still_current",
        lambda _db, _target: True)

    with Session(api_clients.engine) as session:
        with pytest.raises(HTTPException) as caught:
            _process_attempt(
                SESSION_ID, body, Response(), session,
                control_plane="autopilot_worker", worker_target=forged_target,
            )
        assert caught.value.status_code == 409
        assert caught.value.detail["code"] == "autopilot_attempt_input_mismatch"
        session.rollback()

    assert never_called.calls == 0
    with Session(api_clients.engine) as session:
        assert list(session.exec(select(AttemptEvent))) == []
        assert list(session.exec(select(InteractionEvent))) == []
        state = session.get(SessionAutopilotState, SESSION_ID)
        assert state is not None and state.status == "processing_attempt"
        assert state.current_command_id is not None


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


def test_first_operational_silence_plays_the_frozen_cue_then_reopens_the_mic(
        api_clients: ApiClients, monkeypatch):
    """第一次没听到 → 冻结的 silence 一级提示 → 提示播完后再自动开一次麦。

    这条链此前跑不到:成功但空转写被折成 None,当技术失败暂停整场。
    """
    _enable_p0a(monkeypatch)
    _drive_to_processing_attempt(api_clients)
    fake_asr = _EmptyTranscriptAsr()
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)

    _run_p0a_attempt_worker(SESSION_ID)

    item = BANK.single_element[0]
    expected_cue = item["cues"]["1"]["variants"]["silence"]["text"]
    with Session(api_clients.engine) as session:
        attempt = session.exec(select(AttemptEvent)).one()
        commands = list(session.exec(select(RuntimeCommand).order_by(
            RuntimeCommand.command_seq)))
        # 空转写是完成的 attempt,不是技术失败。
        assert attempt.processing_status == "completed"
        assert attempt.error_code is None
        assert attempt.asr_text == ""
        assert attempt.operational_answer_type == "沉默"
        assert attempt.contains_target is False
        assert attempt.prompt_level == 0 and attempt.attempt_seq == 1
        assert [(row.kind, row.state) for row in commands] == [
            ("tts", "succeeded"), ("record", "succeeded"), ("tts", "pending")]
        cue_payload = json.loads(commands[-1].payload_json)
        assert cue_payload["purpose"] == "cue"
        assert cue_payload["response_path"] == "silence"
        assert cue_payload["speech_text"] == expected_cue
        assert (commands[-1].prompt_level, commands[-1].attempt_seq) == (1, 2)
    assert fake_asr.calls == 1

    # 老人端拿到的正是这条冻结提示,分支证据随投影一起下发。
    cue = _device_next(api_clients)
    assert cue is not None and cue["kind"] == "tts"
    assert cue["payload"]["purpose"] == "cue"
    assert cue["payload"]["response_path"] == "silence"
    assert cue["payload"]["speech_text"] == expected_cue

    # 提示真的播完(tts_ended)之后,服务器同一笔事务里就把下一条录音命令开出来:
    # "第一次没听到,温和再问一次"——不需要任何按钮。
    ended = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/{cue['command_key']}/acks",
        headers=api_clients.device_headers,
        json=_ack_body(
            cue,
            ack_type="tts_ended",
            ack_key="ack-silence-cue-ended-0001",
            device_event_seq=3,
            media_ended=True,
            media_duration_ms=4_200,
        ),
    )
    assert ended.status_code == 200, ended.text
    reopened = ended.json()["command"]
    assert reopened["kind"] == "record" and reopened["state"] == "pending"
    assert (reopened["prompt_level"], reopened["attempt_seq"]) == (1, 2)
    # 作答窗口上限仍是冻结协议的 silence_seconds + 5。
    assert reopened["payload"]["max_duration_seconds"] == (
        PROTOCOL["silence_seconds"] + 5)
    # 提示按既有临床协议消费首轮 attempt/cue,没有新增第二个 attempt。
    with Session(api_clients.engine) as session:
        assert len(list(session.exec(select(AttemptEvent)))) == 1
        assert len(list(session.exec(select(RuntimeCommand)))) == 4


def test_asr_degradation_pauses_without_consuming_a_cue_or_inventing_an_answer(
        api_clients: ApiClients, monkeypatch):
    """技术失败(asr_text=None)仍是安全暂停:不判类、不发提示、不伪造回答。"""
    _enable_p0a(monkeypatch)
    _drive_to_processing_attempt(api_clients)
    fake_asr = _DegradedAsr()
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)

    _run_p0a_attempt_worker(SESSION_ID)
    with Session(api_clients.engine) as session:
        attempt = session.exec(select(AttemptEvent)).one()
        commands = list(session.exec(select(RuntimeCommand)))
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        assert attempt.processing_status == "technical_failure"
        assert attempt.error_code == "asr_degraded"
        assert attempt.asr_text is None
        assert attempt.operational_answer_type is None   # 没有伪造任何回答类型
        assert len(commands) == 2                        # 一条提示都没被消费
        assert control is not None and control.status == "paused"
        assert control.last_error_code == "asr_degraded"
        assert control.current_command_id is None
        assert runtime_state is not None and runtime_state.status == "paused"
    assert fake_asr.calls == 1


def test_max_duration_capture_with_real_speech_is_judged_by_text_not_stop_reason(
        api_clients: ApiClients, monkeypatch):
    """15 秒内完全可能有真话:stop_reason=max_duration 不得被当成沉默。"""
    _enable_p0a(monkeypatch)
    _drive_to_processing_attempt(api_clients, stop_reason="max_duration")
    fake_asr = _WorkerAsr()                              # 返回"胡萝卜"
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)

    _run_p0a_attempt_worker(SESSION_ID)
    with Session(api_clients.engine) as session:
        attempt = session.exec(select(AttemptEvent)).one()
        commands = list(session.exec(select(RuntimeCommand).order_by(
            RuntimeCommand.command_seq)))
        assert attempt.operational_answer_type == "正确"
        assert attempt.contains_target is True
        payload = json.loads(commands[-1].payload_json)
        assert payload["purpose"] == "feedback"
        assert payload["speech_text"] == BANK.single_element[0]["success_line"]
        assert "response_path" not in payload            # 零级成功不带分支证据


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
        # R1-foundation: AttemptEvent creation (and attempt_seq consumption) is
        # deferred until ASR resolves. Pause/takeover won the race before the
        # blocked ASR call returned, so no AttemptEvent/InteractionEvent was
        # ever created — only the pre-attempt capture claim was fenced.
        assert list(session.exec(select(AttemptEvent))) == []
        assert list(session.exec(select(InteractionEvent))) == []
        capture_row = session.exec(select(AttemptCaptureProcessing)).one()
        commands = list(session.exec(select(RuntimeCommand).order_by(
            RuntimeCommand.command_seq)))
        events = list(session.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)

        assert capture_row.raw_audio_id == capture["raw_audio_id"]
        assert capture_row.processing_status == "received"
        assert capture_row.final_attempt_id is None
        assert capture_row.processing_owner is None
        assert (
            capture_row.asr_confidence,
            capture_row.asr_engine_version, capture_row.disposition,
            capture_row.error_code,
        ) == (None, None, None, None)
        # Original record command and capture receipt survive untouched; only
        # the ephemeral in-process claim was invalidated.
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
        # R1-foundation: the delayed provider failure arrives after pause/
        # takeover already fenced the pre-attempt capture claim, so it is
        # observation-only — no AttemptEvent is ever created (attempt_seq is
        # never consumed) and no InteractionEvent is appended.
        assert list(session.exec(select(AttemptEvent))) == []
        assert list(session.exec(select(InteractionEvent))) == []
        capture_row = session.exec(select(AttemptCaptureProcessing)).one()
        commands = list(session.exec(select(RuntimeCommand).order_by(
            RuntimeCommand.command_seq)))
        events = list(session.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq)))
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)

        assert capture_row.raw_audio_id == capture["raw_audio_id"]
        assert capture_row.processing_status == "received"
        assert capture_row.final_attempt_id is None
        assert capture_row.processing_owner is None
        assert (
            capture_row.asr_engine_version,
            capture_row.error_code,
        ) == (None, None)
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


def test_dual_worker_race_on_fresh_capture_claims_exactly_once_and_completes_once(
        api_clients: ApiClients, monkeypatch):
    """Two concurrent recovery triggers before ASR ever ran must not double-process.

    R1-foundation: before ASR resolves, only the capture-processing claim
    exists (no AttemptEvent yet). A second concurrent trigger — e.g. another
    device polling GET /autopilot/next — must observe the active capture
    lease and back off, exactly like the existing AttemptEvent claim already
    did before this batch.
    """
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)
    blocked_asr = _BlockingWorkerAsr()
    monkeypatch.setattr(asr, "get_engine", lambda: blocked_asr)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_run_p0a_attempt_worker, SESSION_ID)
        assert blocked_asr.entered.wait(timeout=10), "first worker never entered ASR"
        second = pool.submit(_run_p0a_attempt_worker, SESSION_ID)
        second.result(timeout=10)
        blocked_asr.release.set()
        first.result(timeout=10)

    assert blocked_asr.calls == 1
    with Session(api_clients.engine) as session:
        attempts = list(session.exec(select(AttemptEvent)))
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt.processing_status == "completed"
        assert attempt.attempt_seq == 1
        assert attempt.raw_audio_id == capture["raw_audio_id"]
        capture_rows = list(session.exec(select(AttemptCaptureProcessing)))
        assert len(capture_rows) == 1
        assert capture_rows[0].processing_status == "asr_completed"
        assert capture_rows[0].disposition == "answer_candidate"
        assert capture_rows[0].final_attempt_id == attempt.id
        interactions = [event.event_type for event in session.exec(
            select(InteractionEvent).order_by(InteractionEvent.event_seq))]
        assert interactions.count("attempt_received") == 1
        assert interactions.count("asr_completed") == 1


def test_late_generation_result_after_newer_generation_materializes_is_observation_only(
        api_clients: ApiClients, monkeypatch):
    """A stale generation's ASR result must never fail-close a scope a newer
    generation already advanced into judgement.

    Deterministic interleaving: worker A claims the capture and blocks in
    ASR. While A is blocked, its lease is force-expired and worker B takes
    over (a newer generation), finishes ASR quickly, materializes the
    AttemptEvent directly to ``asr_completed``, and then blocks in
    judgement. Only then is A's stale ASR result released. Before this
    fix, A would reach the attempt_seq check/insert using its
    already-superseded claim, and the terminal-capture projection would
    report a false terminal outcome — either could trigger a spurious
    fail-close against a scope B still legitimately owns.
    """
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)

    a_asr = _BlockingWorkerAsr()
    b_judge = _BlockingWorkerJudge()

    monkeypatch.setattr(asr, "get_engine", lambda: a_asr)
    with ThreadPoolExecutor(max_workers=2) as pool:
        worker_a = pool.submit(_run_p0a_attempt_worker, SESSION_ID)
        assert a_asr.entered.wait(timeout=10), "worker A never entered ASR"

        # Force A's capture lease to expire so worker B can take over —
        # models a crashed/slow worker A without waiting out a real lease.
        with Session(api_clients.engine) as session:
            row = session.exec(select(AttemptCaptureProcessing).where(
                AttemptCaptureProcessing.raw_audio_id == capture["raw_audio_id"],
            )).one()
            session.execute(update(AttemptCaptureProcessing).where(
                AttemptCaptureProcessing.id == row.id,
            ).values(processing_lease_expires_at=datetime.now() - timedelta(seconds=1)))
            session.commit()

        b_asr = _WorkerAsr()
        monkeypatch.setattr(asr, "get_engine", lambda: b_asr)
        monkeypatch.setattr(llm_judge, "get_engine", lambda: b_judge)
        worker_b = pool.submit(_run_p0a_attempt_worker, SESSION_ID)
        assert b_judge.entered.wait(timeout=10), "worker B never entered judgement"

        # Only now release A's stale ASR result — after B already
        # materialized the AttemptEvent and moved on into judgement.
        a_asr.release.set()
        worker_a.result(timeout=10)

        with Session(api_clients.engine) as session:
            control = session.get(SessionAutopilotState, SESSION_ID)
            assert control is not None and control.status == "processing_attempt"
            attempts = list(session.exec(select(AttemptEvent)))
            assert len(attempts) == 1
            assert attempts[0].processing_status == "asr_completed"

        b_judge.release.set()
        worker_b.result(timeout=10)

    with Session(api_clients.engine) as session:
        attempts = list(session.exec(select(AttemptEvent)))
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt.processing_status == "completed"
        assert attempt.attempt_seq == 1
        capture_rows = list(session.exec(select(AttemptCaptureProcessing)))
        assert len(capture_rows) == 1
        assert capture_rows[0].final_attempt_id == attempt.id
        commands = list(session.exec(select(RuntimeCommand)))
        assert len(commands) == 3
        control = session.get(SessionAutopilotState, SESSION_ID)
        assert control is not None and control.status == "waiting_tts"
        interactions = [event.event_type for event in session.exec(
            select(InteractionEvent).order_by(InteractionEvent.event_seq))]
        assert interactions.count("attempt_received") == 1
        assert interactions.count("asr_completed") == 1
        assert interactions.count("judgement_completed") == 1
    assert a_asr.calls == 1
    assert b_asr.calls == 1
    assert b_judge.calls == 1


def _drive_followup_processing_attempt_via_current_cue(
        clients: ApiClients, *, suffix: str,
        tts_device_event_seq: int, record_device_event_seq: int,
        stop_reason: str = "silence") -> dict:
    """Play the current cue and persist its following capture through public APIs.

    Mirrors ``_drive_to_processing_attempt``'s TTS/record ACK sequence, but for
    a cue turn that a completed prior attempt opened — i.e. one step further
    along the same real device flow, not a shortcut or direct database seed.
    """
    cue = _device_next(clients)
    assert cue is not None and cue["kind"] == "tts"
    ended = clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/{cue['command_key']}/acks",
        headers=clients.device_headers,
        json=_ack_body(
            cue, ack_type="tts_ended",
            ack_key=f"ack-followup-cue-ended-{suffix}",
            device_event_seq=tts_device_event_seq,
            media_ended=True, media_duration_ms=4200,
        ),
    )
    assert ended.status_code == 200, ended.text
    record = ended.json()["command"]
    raw_audio_id = record["payload"]["raw_audio_id"]
    blob = b"\x1a\x45\xdf\xa3p0a-followup-attempt-" + suffix.encode("ascii")
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
        f"/sessions/{SESSION_ID}/autopilot/commands/{record['command_key']}/acks",
        headers=clients.device_headers,
        json=_ack_body(
            record, ack_type="record_stopped",
            ack_key=f"ack-followup-record-stopped-{suffix}",
            device_event_seq=record_device_event_seq, stop_reason=stop_reason,
            raw_audio_id=raw_audio_id, receipt_server_seq=receipt["serverSeq"],
            checksum=upload_fact["checksum"], byte_count=upload_fact["bytes"],
            duration_seconds=1.5,
        ),
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "processing_attempt"
    return {"record": record, "raw_audio_id": raw_audio_id}


def _drive_second_processing_attempt_via_current_cue(
        clients: ApiClients) -> dict:
    """Compatibility wrapper for the original two-attempt concurrency tests."""
    return _drive_followup_processing_attempt_via_current_cue(
        clients,
        suffix="cross-attempt-0001",
        tts_device_event_seq=3,
        record_device_event_seq=4,
    )


@pytest.mark.parametrize(
    ("transcript", "expected_path"),
    [
        ("", "silence"),
        ("萝卜", "close"),
        ("西瓜", "unknown"),
    ],
)
def test_three_failed_api_worker_rounds_end_with_tell_answer_and_no_fourth_mic(
        api_clients: ApiClients, monkeypatch, transcript, expected_path):
    """Run the complete three-capture HTTP/worker ladder without DB shortcuts.

    Each round persists upload + audioSaved + record_stopped evidence, then the
    authoritative worker consumes it. The final answer playback closes the one-
    position scope; no fourth record command may be issued.
    """
    _enable_p0a(monkeypatch)
    _drive_to_processing_attempt(api_clients)
    fake_asr = _FixedTranscriptAsr(transcript)
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)

    for attempt_index in range(1, 4):
        _run_p0a_attempt_worker(SESSION_ID)
        speech = _device_next(api_clients)
        assert speech is not None and speech["kind"] == "tts"
        if attempt_index == 1:
            assert speech["payload"]["purpose"] == "cue"
            assert speech["payload"]["response_path"] == expected_path
            assert (speech["prompt_level"], speech["attempt_seq"]) == (1, 2)
        elif attempt_index == 2:
            assert speech["payload"]["purpose"] == "cue"
            assert "response_path" not in speech["payload"]
            assert (speech["prompt_level"], speech["attempt_seq"]) == (2, 3)
        else:
            assert speech["payload"]["purpose"] == "tell_answer"
            assert speech["payload"]["speech_text"] == (
                BANK.single_element[0]["tell_answer"])
            assert (speech["prompt_level"], speech["attempt_seq"]) == (3, 3)
            break

        _drive_followup_processing_attempt_via_current_cue(
            api_clients,
            suffix=f"three-fail-{expected_path}-{attempt_index + 1}",
            tts_device_event_seq=(attempt_index * 2) + 1,
            record_device_event_seq=(attempt_index * 2) + 2,
        )

    with Session(api_clients.engine) as session:
        attempts = list(session.exec(select(AttemptEvent).order_by(
            AttemptEvent.attempt_seq)))
        commands = list(session.exec(select(RuntimeCommand).order_by(
            RuntimeCommand.command_seq)))
        assert [(row.attempt_seq, row.prompt_level, row.contains_target)
                for row in attempts] == [
                    (1, 0, False), (2, 1, False), (3, 2, False)]
        assert [(row.kind, row.state) for row in commands] == [
            ("tts", "succeeded"), ("record", "succeeded"),
            ("tts", "succeeded"), ("record", "succeeded"),
            ("tts", "succeeded"), ("record", "succeeded"),
            ("tts", "pending"),
        ]
        assert len(list(session.exec(select(ItemEvent)))) == 1
        turns = list(session.exec(select(TurnEvent)))
        assert len(turns) == 1
        assert turns[0].source_attempt_id == attempts[-1].id

    terminal = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{speech['command_key']}/acks",
        headers=api_clients.device_headers,
        json=_ack_body(
            speech,
            ack_type="tts_ended",
            ack_key=f"ack-three-fail-tell-ended-{expected_path}",
            device_event_seq=7,
            media_ended=True,
            media_duration_ms=4_200,
        ),
    )
    assert terminal.status_code == 200, terminal.text
    assert terminal.json()["status"] == "scope_completed"
    assert terminal.json()["command"] is None
    with Session(api_clients.engine) as session:
        assert len(list(session.exec(select(RuntimeCommand)))) == 7
        runtime = session.get(SessionRuntimeState, SESSION_ID)
        assert runtime is not None and runtime.status == "intervention_completed"
    assert fake_asr.calls == 3


@pytest.mark.parametrize("success_level", [1, 2])
def test_api_worker_success_after_cue_advances_exactly_one_frozen_position(
        api_clients: ApiClients, monkeypatch, success_level):
    """A cue1/cue2 target hit advances only after durable feedback ACK."""
    _enable_p0a(monkeypatch)
    _bind_api_bank(api_clients, monkeypatch, TWO_ONLY_BANK)
    _drive_to_processing_attempt(api_clients)
    failed_asr = _EmptyTranscriptAsr()
    monkeypatch.setattr(asr, "get_engine", lambda: failed_asr)
    _run_p0a_attempt_worker(SESSION_ID)

    for cue_level in range(1, success_level + 1):
        _drive_followup_processing_attempt_via_current_cue(
            api_clients,
            suffix=f"cue{cue_level}-success-round{cue_level + 1}",
            tts_device_event_seq=(cue_level * 2) + 1,
            record_device_event_seq=(cue_level * 2) + 2,
        )
        if cue_level < success_level:
            _run_p0a_attempt_worker(SESSION_ID)

    target_asr = _WorkerAsr()
    monkeypatch.setattr(asr, "get_engine", lambda: target_asr)
    _run_p0a_attempt_worker(SESSION_ID)
    feedback = _device_next(api_clients)
    assert feedback is not None and feedback["kind"] == "tts"
    assert feedback["payload"]["purpose"] == "feedback"
    if success_level == 1:
        assert feedback["payload"]["response_path"] == "silence"
    else:
        assert "response_path" not in feedback["payload"]
    assert (feedback["prompt_level"], feedback["attempt_seq"]) == (
        success_level, success_level + 1)
    with Session(api_clients.engine) as session:
        assert len(list(session.exec(select(RuntimeCommand)))) == (
            (success_level * 2) + 3)

    ended = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{feedback['command_key']}/acks",
        headers=api_clients.device_headers,
        json=_ack_body(
            feedback,
            ack_type="tts_ended",
            ack_key=f"ack-cue{success_level}-success-feedback-ended-0001",
            device_event_seq=(success_level * 2) + 3,
            media_ended=True,
            media_duration_ms=1_200,
        ),
    )
    assert ended.status_code == 200, ended.text
    assert ended.json()["status"] == "waiting_tts"
    successor = ended.json()["command"]
    assert successor is not None
    assert successor["item_ref"] == "itm-0002"
    assert successor["payload"]["purpose"] == "question"
    assert successor["payload"]["speech_text"] == (
        TWO_ONLY_BANK.single_element[1]["initial_prompt"])
    assert (successor["prompt_level"], successor["attempt_seq"]) == (0, 1)

    replay = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{feedback['command_key']}/acks",
        headers=api_clients.device_headers,
        json=_ack_body(
            feedback,
            ack_type="tts_ended",
            ack_key=f"ack-cue{success_level}-success-feedback-ended-0001",
            device_event_seq=(success_level * 2) + 3,
            media_ended=True,
            media_duration_ms=1_200,
        ),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["status"] == "waiting_tts"
    assert replay.json()["command"] is None
    assert _device_next(api_clients) == successor
    with Session(api_clients.engine) as session:
        attempts = list(session.exec(select(AttemptEvent).order_by(
            AttemptEvent.attempt_seq)))
        assert [(row.attempt_seq, row.prompt_level, row.contains_target)
                for row in attempts] == [
                    *((index + 1, index, False)
                      for index in range(success_level)),
                    (success_level + 1, success_level, True),
                ]
        assert len(list(session.exec(select(RuntimeCommand)))) == (
            (success_level * 2) + 4)
        assert len(list(session.exec(select(ItemEvent)))) == 1
        turns = list(session.exec(select(TurnEvent)))
        assert len(turns) == 1
        assert turns[0].source_attempt_id == attempts[-1].id
        assert turns[0].prompt_level == success_level
    assert failed_asr.calls == success_level
    assert target_asr.calls == 1


@pytest.mark.parametrize("failure_level", [1, 2])
@pytest.mark.parametrize("error_code", ["asr_degraded", "asr_exception"])
def test_api_worker_technical_failure_after_cue_pauses_without_next_prompt(
        api_clients: ApiClients, monkeypatch, error_code, failure_level):
    """A provider failure after cue1/cue2 pauses at the same content level."""
    _enable_p0a(monkeypatch)
    _drive_to_processing_attempt(api_clients)
    failed_asr = _EmptyTranscriptAsr()
    monkeypatch.setattr(asr, "get_engine", lambda: failed_asr)
    _run_p0a_attempt_worker(SESSION_ID)

    for cue_level in range(1, failure_level + 1):
        _drive_followup_processing_attempt_via_current_cue(
            api_clients,
            suffix=f"cue{cue_level}-technical-{error_code}",
            tts_device_event_seq=(cue_level * 2) + 1,
            record_device_event_seq=(cue_level * 2) + 2,
        )
        if cue_level < failure_level:
            _run_p0a_attempt_worker(SESSION_ID)

    failing_asr = (
        _DegradedAsr() if error_code == "asr_degraded"
        else _WorkerAsr(fails=True)
    )
    monkeypatch.setattr(asr, "get_engine", lambda: failing_asr)
    _run_p0a_attempt_worker(SESSION_ID)

    with Session(api_clients.engine) as session:
        attempts = list(session.exec(select(AttemptEvent).order_by(
            AttemptEvent.attempt_seq)))
        assert [(row.attempt_seq, row.prompt_level) for row in attempts] == [
            (index + 1, index) for index in range(failure_level + 1)]
        assert attempts[-1].processing_status == "technical_failure"
        assert attempts[-1].error_code == error_code
        assert attempts[-1].asr_text is None
        assert attempts[-1].operational_answer_type is None
        assert attempts[-1].contains_target is None
        commands = list(session.exec(select(RuntimeCommand).order_by(
            RuntimeCommand.command_seq)))
        assert len(commands) == 2 + (failure_level * 2)
        assert all(row.state == "succeeded" for row in commands)
        assert [json.loads(row.payload_json)["purpose"] for row in commands
                if row.kind == "tts"] == [
                    "question", *("cue" for _ in range(failure_level))]
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime = session.get(SessionRuntimeState, SESSION_ID)
        assert control is not None and control.status == "paused"
        assert control.current_command_id is None
        assert control.last_error_code == error_code
        assert runtime is not None and runtime.status == "paused"
        assert list(session.exec(select(ItemEvent))) == []
        assert list(session.exec(select(TurnEvent))) == []
        before_counts = (
            len(attempts),
            len(commands),
            len(list(session.exec(select(AutopilotControlEvent)))),
            len(list(session.exec(select(InteractionEvent)))),
            len(list(session.exec(select(AttemptCaptureProcessing)))),
        )
    _run_p0a_attempt_worker(SESSION_ID)
    with Session(api_clients.engine) as session:
        assert (
            len(list(session.exec(select(AttemptEvent)))),
            len(list(session.exec(select(RuntimeCommand)))),
            len(list(session.exec(select(AutopilotControlEvent)))),
            len(list(session.exec(select(InteractionEvent)))),
            len(list(session.exec(select(AttemptCaptureProcessing)))),
        ) == before_counts
    assert failed_asr.calls == failure_level
    assert failing_asr.calls == 1


def _live_snapshot(session: Session) -> tuple:
    control = session.get(SessionAutopilotState, SESSION_ID)
    runtime_state = session.get(SessionRuntimeState, SESSION_ID)
    commands = [
        (c.id, c.kind, c.state, c.prompt_level, c.attempt_seq)
        for c in session.exec(select(RuntimeCommand).where(
            RuntimeCommand.session_id == SESSION_ID,
        ).order_by(RuntimeCommand.command_seq))
    ]
    events = [
        (e.event_type, e.reason_code, e.command_id)
        for e in session.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.session_id == SESSION_ID,
        ).order_by(AutopilotControlEvent.event_seq))
    ]
    attempts = [
        (a.id, a.processing_status, a.error_code, a.processing_owner,
         a.processing_generation, a.processing_lease_expires_at)
        for a in session.exec(select(AttemptEvent).where(
            AttemptEvent.session_id == SESSION_ID,
        ).order_by(AttemptEvent.id))
    ]
    captures = [
        (c.id, c.processing_status, c.error_code, c.processing_owner,
         c.processing_generation, c.processing_lease_expires_at,
         c.final_attempt_id)
        for c in session.exec(select(AttemptCaptureProcessing).where(
            AttemptCaptureProcessing.session_id == SESSION_ID,
        ).order_by(AttemptCaptureProcessing.id))
    ]
    return (
        (control.mode, control.status, control.current_command_id,
         control.revision, control.next_command_seq, control.last_error_code,
         control.lease_owner, control.lease_acquired_at,
         control.lease_expires_at, control.control_generation,
         control.runner_generation) if control is not None else None,
        (runtime_state.status, runtime_state.revision)
        if runtime_state is not None else None,
        tuple(commands), tuple(events), tuple(attempts), tuple(captures),
    )


def _full_evidence_snapshot(session: Session) -> tuple:
    """``_live_snapshot`` plus every AI-fact/projection field a late worker
    could still corrupt without ever touching lease/generation fencing state:
    the full Attempt ASR/judge fact set (not just its lease fencing), the
    session's InteractionEvent ledger, and the ItemEvent/TurnEvent review-
    ledger projection ``materialize_terminal_attempt_evidence`` writes.
    ``_live_snapshot`` alone would miss a late worker wrongly appending a
    second ``judgement_completed`` interaction, or a duplicate/corrupted
    TurnEvent projection, since neither touches a lease/generation/command
    field.
    """
    attempts = tuple(
        (a.id, a.asr_text, a.asr_confidence, a.asr_engine_version,
         a.operational_answer_type, a.operational_score, a.operational_needs_review,
         a.judge_mode, a.judge_engine_version, a.judge_reason,
         a.matched_on, a.contains_target, a.error_code, a.processed_at)
        for a in session.exec(select(AttemptEvent).where(
            AttemptEvent.session_id == SESSION_ID,
        ).order_by(AttemptEvent.id))
    )
    interactions = tuple(
        (e.id, e.event_seq, e.event_type, e.attempt_id, e.payload_json)
        for e in session.exec(select(InteractionEvent).where(
            InteractionEvent.session_id == SESSION_ID,
        ).order_by(InteractionEvent.event_seq))
    )
    items = tuple(
        (i.id, i.item_id) for i in session.exec(select(ItemEvent).where(
            ItemEvent.session_id == SESSION_ID,
        ).order_by(ItemEvent.id))
    )
    turns = tuple(
        (t.id, t.item_event_id, t.turn_seq, t.source_attempt_id,
         t.ai_answer_type, t.ai_score, t.ai_needs_review, t.ai_judge_mode,
         t.reviewer_id, t.reviewed_score, t.score_locked, t.element_value)
        for t in session.exec(
            select(TurnEvent)
            .join(ItemEvent, TurnEvent.item_event_id == ItemEvent.id)
            .where(ItemEvent.session_id == SESSION_ID)
            .order_by(TurnEvent.id))
    )
    return (_live_snapshot(session), attempts, interactions, items, turns)


def test_stale_worker_that_lost_a_still_unrouted_completion_never_routes_then_fresh_recovery_routes_once(
        api_clients: ApiClients, monkeypatch):
    """Sharper counter-example than the cross-attempt races above: A loses its
    claim to B on the *same* logical attempt, and B is stopped right after
    committing the completed judgement — before B's own route call. A must
    still be pure observation (``claim_lost``, not merely a stale-target
    check, since the target is still exactly current at this point — nothing
    has routed yet). A fresh recovery worker then routes the completed-but-
    unrouted attempt exactly once.
    """
    _enable_p0a(monkeypatch)
    capture1 = _drive_to_processing_attempt(api_clients)

    a_asr = _BlockingWorkerAsr()  # succeeds when released; this is not a
                                   # fail-close case, A's own claim is simply
                                   # already gone by the time it returns.
    monkeypatch.setattr(asr, "get_engine", lambda: a_asr)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker_a = pool.submit(_run_p0a_attempt_worker, SESSION_ID)
        assert a_asr.entered.wait(timeout=10), "worker A never entered ASR"

        # Force A's capture lease to expire so B can take over.
        with Session(api_clients.engine) as session:
            row = session.exec(select(AttemptCaptureProcessing).where(
                AttemptCaptureProcessing.raw_audio_id == capture1["raw_audio_id"],
            )).one()
            session.execute(update(AttemptCaptureProcessing).where(
                AttemptCaptureProcessing.id == row.id,
            ).values(processing_lease_expires_at=datetime.now() - timedelta(seconds=1)))
            session.commit()

        # B completes the attempt for real (ASR+judge) via _process_attempt
        # directly — deliberately not calling _run_p0a_attempt_worker's own
        # post-processing route step, modeling a worker stopped/crashed right
        # after committing the completed judgement and before it ever reaches
        # route_completed_attempt.
        b_asr = _WorkerAsr()
        monkeypatch.setattr(asr, "get_engine", lambda: b_asr)
        with Session(api_clients.engine) as session:
            b_target = autopilot_orchestration.derive_worker_target(
                session, session_id=SESSION_ID)
            b_derived = autopilot_orchestration.derive_authoritative_attempt_input(
                session, session_id=SESSION_ID)
            session.rollback()
            b_body = AttemptProcessIn.model_validate(
                b_derived.model_dump(mode="python"))
            b_result = _process_attempt(
                SESSION_ID, b_body, Response(), session,
                control_plane="autopilot_worker", worker_target=b_target,
            )
        assert b_result.get("status") == "completed"
        assert b_result.get("idempotent") is not True

        with Session(api_clients.engine) as session:
            control = session.get(SessionAutopilotState, SESSION_ID)
            # B completed the judgement but never routed: still
            # processing_attempt, still pointing at the exact same record —
            # the target itself has not gone stale, only the claim has.
            assert control is not None and control.status == "processing_attempt"
            before = _live_snapshot(session)

        # Only now release A's stale (not itself failing) ASR result.
        a_asr.release.set()
        worker_a.result(timeout=10)

        with Session(api_clients.engine) as session:
            after = _live_snapshot(session)
    assert after == before
    assert a_asr.calls == 1
    assert b_asr.calls == 1

    # A fresh recovery worker now routes the completed-but-unrouted attempt
    # exactly once.
    _run_p0a_attempt_worker(SESSION_ID)
    with Session(api_clients.engine) as session:
        control = session.get(SessionAutopilotState, SESSION_ID)
        assert control is not None and control.status == "waiting_tts"
        commands = list(session.exec(select(RuntimeCommand).where(
            RuntimeCommand.session_id == SESSION_ID,
        )))
        # question tts, record, feedback tts — routed exactly once.
        assert len(commands) == 3


def test_stale_capture_asr_worker_across_logical_attempts_is_observation_only(
        api_clients: ApiClients, monkeypatch):
    """R1-foundation A, required test 1: worker A freezes a target on attempt
    #1, blocks in ASR; worker B takes the same capture over, completes it
    (silence) and routes a cue; the device answers the cue and reaches a
    brand-new processing_attempt #2. Only then is A released with a failed
    ASR result. A's stale target must never touch #2's state/claims.
    """
    _enable_p0a(monkeypatch)
    capture1 = _drive_to_processing_attempt(api_clients)

    a_asr = _BlockingWorkerAsr(fails=True)
    monkeypatch.setattr(asr, "get_engine", lambda: a_asr)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker_a = pool.submit(_run_p0a_attempt_worker, SESSION_ID)
        assert a_asr.entered.wait(timeout=10), "worker A never entered ASR"

        # Force A's capture lease to expire so worker B can take over — models
        # a crashed/slow worker A without waiting out a real lease.
        with Session(api_clients.engine) as session:
            row = session.exec(select(AttemptCaptureProcessing).where(
                AttemptCaptureProcessing.raw_audio_id == capture1["raw_audio_id"],
            )).one()
            session.execute(update(AttemptCaptureProcessing).where(
                AttemptCaptureProcessing.id == row.id,
            ).values(processing_lease_expires_at=datetime.now() - timedelta(seconds=1)))
            session.commit()

        # B completes the OLD attempt with an empty transcript (silence) — a
        # completed attempt, but not a matching answer, so routing issues a
        # cue for a genuinely new logical attempt rather than ending the plan.
        b_asr = _EmptyTranscriptAsr()
        monkeypatch.setattr(asr, "get_engine", lambda: b_asr)
        _run_p0a_attempt_worker(SESSION_ID)
        with Session(api_clients.engine) as session:
            control = session.get(SessionAutopilotState, SESSION_ID)
            assert control is not None and control.status == "waiting_tts"

        # Device answers the cue and reaches processing_attempt #2 — a
        # different record, different raw_audio_id, same control/runner
        # generation as #1 (no pause/resume happened in between).
        capture2 = _drive_second_processing_attempt_via_current_cue(api_clients)
        assert capture2["raw_audio_id"] != capture1["raw_audio_id"]
        assert capture2["record"]["command_key"] != capture1["record"]["command_key"]
        with Session(api_clients.engine) as session:
            control = session.get(SessionAutopilotState, SESSION_ID)
            assert control is not None and control.status == "processing_attempt"
            assert control.control_generation == 1 and control.runner_generation == 1
            before = _live_snapshot(session)

        # Only now release A's stale, failing ASR result.
        a_asr.release.set()
        worker_a.result(timeout=10)

        with Session(api_clients.engine) as session:
            after = _live_snapshot(session)
    assert after == before
    assert a_asr.calls == 1
    assert b_asr.calls == 1


def test_stale_judgement_worker_across_logical_attempts_is_observation_only(
        api_clients: ApiClients, monkeypatch):
    """R1-foundation A, required test 2: worker A freezes a target on attempt
    #1, completes its own ASR (a wrong answer), then blocks in judgement;
    worker B recovers the same claim, finishes judgement fast against A's own
    already-committed transcript (still wrong, so it routes a cue) and routes
    it; the device answers the cue and reaches a brand-new processing_attempt
    #2. Only then is A released with a failed judgement result. A's stale
    target must never touch #2's state/claims.
    """
    _enable_p0a(monkeypatch)
    capture1 = _drive_to_processing_attempt(api_clients)

    original_judge_engine = llm_judge.get_engine
    a_asr = _WrongAnswerAsr()
    a_judge = _FailingBlockingWorkerJudge()
    monkeypatch.setattr(asr, "get_engine", lambda: a_asr)
    monkeypatch.setattr(llm_judge, "get_engine", lambda: a_judge)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker_a = pool.submit(_run_p0a_attempt_worker, SESSION_ID)
        assert a_judge.entered.wait(timeout=10), "worker A never entered judgement"

        # Force A's attempt lease to expire so worker B can take over — A's
        # own ASR already committed by this point (asr_completed).
        with Session(api_clients.engine) as session:
            row = session.exec(select(AttemptEvent).where(
                AttemptEvent.raw_audio_id == capture1["raw_audio_id"],
            )).one()
            session.execute(update(AttemptEvent).where(
                AttemptEvent.id == row.id,
            ).values(processing_lease_expires_at=datetime.now() - timedelta(seconds=1)))
            session.commit()

        # B recovers the same claim; its judgement runs fast (the real
        # engine) against A's own already-committed wrong transcript.
        monkeypatch.setattr(llm_judge, "get_engine", original_judge_engine)
        _run_p0a_attempt_worker(SESSION_ID)
        with Session(api_clients.engine) as session:
            control = session.get(SessionAutopilotState, SESSION_ID)
            assert control is not None and control.status == "waiting_tts"
            attempts = list(session.exec(select(AttemptEvent)))
            assert len(attempts) == 1 and attempts[0].processing_status == "completed"
            assert attempts[0].contains_target is False

        capture2 = _drive_second_processing_attempt_via_current_cue(api_clients)
        assert capture2["raw_audio_id"] != capture1["raw_audio_id"]
        with Session(api_clients.engine) as session:
            control = session.get(SessionAutopilotState, SESSION_ID)
            assert control is not None and control.status == "processing_attempt"
            before = _live_snapshot(session)

        # Only now release A's stale, failing judgement result.
        a_judge.release.set()
        worker_a.result(timeout=10)

        with Session(api_clients.engine) as session:
            after = _live_snapshot(session)
    assert after == before
    assert a_asr.calls == 1
    assert a_judge.calls == 1


def test_stale_route_across_logical_attempts_is_observation_only(
        api_clients: ApiClients, monkeypatch):
    """R1-foundation A, required test 3: worker A's own route_completed_attempt
    call for attempt #1 is blocked (a real interleaving, not a simulated
    fail-close); a second, unblocked route call — modeling a fresh recovery —
    finishes routing the same (only) completed attempt for real, the device
    reaches a brand-new processing_attempt #2, and only then does A's blocked
    route call raise. A's stale target must never touch #2.

    ``_run_p0a_attempt_worker``'s route step holds the module-level
    ``_LIVE_WRITE_LOCK``/``device_capability._PAIR_LOCK`` (both plain
    ``RLock``s, not cross-thread-safe) around the route call. Blocking A's own
    call inside that section while holding those locks would deadlock the
    second (unblocked) call and the device ACKs below, which run in this same
    process. They are replaced with a no-op context manager for this test
    only; the real synchronization being exercised here is the DB-level
    generation/target CAS, not the in-process lock.
    """
    _enable_p0a(monkeypatch)
    monkeypatch.setattr(main_module, "_LIVE_WRITE_LOCK", contextlib.nullcontext())
    monkeypatch.setattr(device_capability, "_PAIR_LOCK", contextlib.nullcontext())
    capture1 = _drive_to_processing_attempt(api_clients)

    real_route_completed_attempt = autopilot_service.route_completed_attempt
    route_calls = {"n": 0}
    a_route_entered = threading.Event()
    a_route_release = threading.Event()

    def blocking_route(route_db, *, session_id, attempt_id, **kwargs):
        route_calls["n"] += 1
        if route_calls["n"] == 1:
            # Release A's own SQLite transaction before blocking — SQLite is
            # single-writer; an open transaction here would itself deadlock
            # the second call's real route at the DB layer, not just the
            # (now no-op) Python locks.
            route_db.rollback()
            a_route_entered.set()
            if not a_route_release.wait(timeout=10):
                raise AssertionError("test did not release blocked route call")
            raise RuntimeError("injected delayed route failure")
        return real_route_completed_attempt(
            route_db, session_id=session_id, attempt_id=attempt_id, **kwargs)

    fake_asr = _EmptyTranscriptAsr()
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)
    monkeypatch.setattr(autopilot_service, "route_completed_attempt", blocking_route)

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker_a = pool.submit(_run_p0a_attempt_worker, SESSION_ID)
        assert a_route_entered.wait(timeout=10), "worker A never entered route"

        # A second, unblocked route call — the shape of a fresh recovery
        # picking up the same still-unrouted completed attempt — finishes the
        # real routing for attempt #1.
        _run_p0a_attempt_worker(SESSION_ID)
        with Session(api_clients.engine) as session:
            control = session.get(SessionAutopilotState, SESSION_ID)
            assert control is not None and control.status == "waiting_tts"

        capture2 = _drive_second_processing_attempt_via_current_cue(api_clients)
        assert capture2["raw_audio_id"] != capture1["raw_audio_id"]
        with Session(api_clients.engine) as session:
            control = session.get(SessionAutopilotState, SESSION_ID)
            assert control is not None and control.status == "processing_attempt"
            before = _live_snapshot(session)

        # Only now release A's stale, failing route call.
        a_route_release.set()
        worker_a.result(timeout=10)

        with Session(api_clients.engine) as session:
            after = _live_snapshot(session)
    assert after == before
    assert route_calls["n"] == 2


def test_matching_target_route_failure_pauses_atomically_and_clears_only_its_claims(
        api_clients: ApiClients, monkeypatch):
    """R1-foundation A, required test 4 (positive case): a target that is
    still exactly current stages the fail-close pause exactly once, atomic
    with the runtime pause, and clears only the capture claim bound to that
    exact target — never a broader/narrower set.
    """
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)

    a_asr = _BlockingWorkerAsr()
    monkeypatch.setattr(asr, "get_engine", lambda: a_asr)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker_a = pool.submit(_run_p0a_attempt_worker, SESSION_ID)
        assert a_asr.entered.wait(timeout=10), "worker never entered ASR"

        with Session(api_clients.engine) as session:
            target = autopilot_orchestration.derive_worker_target(
                session, session_id=SESSION_ID)
            session.rollback()
            claim_before = session.exec(select(AttemptCaptureProcessing).where(
                AttemptCaptureProcessing.raw_audio_id == capture["raw_audio_id"],
            )).one()
            assert claim_before.processing_owner is not None
            assert claim_before.processing_lease_expires_at is not None

        _fail_closed_p0a_attempt_worker(
            SESSION_ID, "autopilot_attempt_route_failed", target=target)

        with Session(api_clients.engine) as session:
            control = session.get(SessionAutopilotState, SESSION_ID)
            runtime_state = session.get(SessionRuntimeState, SESSION_ID)
            events = list(session.exec(select(AutopilotControlEvent).where(
                AutopilotControlEvent.session_id == SESSION_ID,
                AutopilotControlEvent.event_type == "failure",
            )))
            claim_after = session.exec(select(AttemptCaptureProcessing).where(
                AttemptCaptureProcessing.raw_audio_id == capture["raw_audio_id"],
            )).one()
            assert control is not None and control.status == "paused"
            assert control.current_command_id is None
            assert control.last_error_code == "autopilot_attempt_route_failed"
            assert runtime_state is not None and runtime_state.status == "paused"
            assert len(events) == 1
            assert events[0].command_id == target.record_command_id
            assert claim_after.processing_owner is None
            assert claim_after.processing_lease_expires_at is None

            # Repeating the same (now stale, since state is paused) fail-close
            # must not double-pause or write a second event.
            again = autopilot_orchestration.stage_processing_failure(
                session, session_id=SESSION_ID,
                error_code="autopilot_attempt_route_failed",
                source="worker_exception", target=target)
            assert again is False
            session.rollback()
            events_again = list(session.exec(select(AutopilotControlEvent).where(
                AutopilotControlEvent.session_id == SESSION_ID,
                AutopilotControlEvent.event_type == "failure",
            )))
            assert len(events_again) == 1

        # Release the real blocked worker so its thread doesn't leak past the
        # test; its own now-invalidated claim makes this a graceful no-op.
        a_asr.release.set()
        worker_a.result(timeout=10)


def test_same_target_judgement_claim_lost_is_pure_observation_then_fresh_recovery_routes_once(
        api_clients: ApiClients, monkeypatch):
    """Codex audit item 1: the AttemptEvent-level ``claim_lost`` branch, not
    the earlier capture-level one. Worker A's own ASR commits for real (its
    transcript materializes the AttemptEvent), then A blocks in judgement
    holding the live AttemptEvent claim. Its lease is force-expired; worker B
    recovers the *same* AttemptEvent (the target is still exactly current --
    nothing has routed) and completes judgement for real, but is stopped
    before its own route call. Releasing A must hit
    ``_record_judgement_success``'s own CAS failure -> ``_claim_lost_payload``
    with ``control_plane="autopilot_worker"`` -> the ``claim_lost`` branch in
    ``_run_p0a_attempt_worker`` -- never ``route_completed_attempt``, never a
    fail-close pause, and a completely unchanged snapshot. A fresh recovery
    worker afterwards still routes the completed attempt exactly once.

    A's own returned result is spied directly (not just inferred from zero
    mutation) so ``result.get("claim_lost") is True`` is proven, not assumed.
    B calls the pre-patch original ``_process_attempt`` reference so the spy
    only ever observes A's own call.
    """
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)

    real_route_completed_attempt = autopilot_service.route_completed_attempt
    route_calls = {"n": 0}

    def counting_route(route_db, *, session_id, attempt_id, **kwargs):
        route_calls["n"] += 1
        return real_route_completed_attempt(
            route_db, session_id=session_id, attempt_id=attempt_id, **kwargs)

    monkeypatch.setattr(autopilot_service, "route_completed_attempt", counting_route)

    real_fail_closed = main_module._fail_closed_p0a_attempt_worker
    fail_close_calls = {"n": 0}

    def counting_fail_closed(*args, **kwargs):
        fail_close_calls["n"] += 1
        return real_fail_closed(*args, **kwargs)

    monkeypatch.setattr(
        main_module, "_fail_closed_p0a_attempt_worker", counting_fail_closed)

    # _run_p0a_attempt_worker calls ``_process_attempt`` by bare name, which
    # Python resolves through this module's globals -- patching the module
    # attribute is what lets this spy see exactly A's own call and return
    # value. B below calls ``original_process_attempt`` directly (the
    # reference captured before patching, identical to the test module's own
    # top-level ``_process_attempt`` import), so it is never recorded here.
    original_process_attempt = main_module._process_attempt
    a_results: list[dict] = []

    def spy_process_attempt(*args, **kwargs):
        result = original_process_attempt(*args, **kwargs)
        a_results.append(result)
        return result

    monkeypatch.setattr(main_module, "_process_attempt", spy_process_attempt)

    original_judge_engine = llm_judge.get_engine
    a_asr = _WrongAnswerAsr()
    a_judge = _BlockingWorkerJudge()
    monkeypatch.setattr(asr, "get_engine", lambda: a_asr)
    monkeypatch.setattr(llm_judge, "get_engine", lambda: a_judge)

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker_a = pool.submit(_run_p0a_attempt_worker, SESSION_ID)
        try:
            assert a_judge.entered.wait(timeout=10), "worker A never entered judgement"

            # A's own ASR already committed (asr_completed); force its
            # AttemptEvent lease to expire so B can take over the identical
            # target/attempt.
            with Session(api_clients.engine) as session:
                row = session.exec(select(AttemptEvent).where(
                    AttemptEvent.raw_audio_id == capture["raw_audio_id"],
                )).one()
                assert row.processing_status == "asr_completed"
                session.execute(update(AttemptEvent).where(
                    AttemptEvent.id == row.id,
                ).values(
                    processing_lease_expires_at=datetime.now() - timedelta(seconds=1)))
                session.commit()

            # B recovers the same AttemptEvent claim and completes judgement
            # for real, via the pre-patch original _process_attempt --
            # deliberately not _run_p0a_attempt_worker, so it never reaches
            # its own route step, and deliberately not the (now spied) module
            # attribute, so only A's own call is recorded above.
            monkeypatch.setattr(llm_judge, "get_engine", original_judge_engine)
            with Session(api_clients.engine) as session:
                b_target = autopilot_orchestration.derive_worker_target(
                    session, session_id=SESSION_ID)
                b_derived = autopilot_orchestration.derive_authoritative_attempt_input(
                    session, session_id=SESSION_ID)
                session.rollback()
                b_body = AttemptProcessIn.model_validate(
                    b_derived.model_dump(mode="python"))
                b_result = original_process_attempt(
                    SESSION_ID, b_body, Response(), session,
                    control_plane="autopilot_worker", worker_target=b_target,
                )
            assert b_result.get("status") == "completed"
            assert b_result.get("idempotent") is not True
            assert route_calls["n"] == 0  # B stopped before its own route call
            assert a_results == []  # B's call must never be recorded as A's

            with Session(api_clients.engine) as session:
                # B did not route: the frozen target itself is still exactly
                # current, only the claim moved.
                assert autopilot_orchestration.worker_target_still_current(
                    session, b_target) is True
                control = session.get(SessionAutopilotState, SESSION_ID)
                assert control is not None and control.status == "processing_attempt"
                before = _full_evidence_snapshot(session)
        finally:
            # Release and join before any assertion above can leave the pool
            # waiting on a thread that will never unblock, and before
            # monkeypatch tears down the provider/spy patches A's thread
            # still depends on.
            a_judge.release.set()
            worker_a.result(timeout=10)

        with Session(api_clients.engine) as session:
            after = _full_evidence_snapshot(session)
            # The frozen target is still exactly current after A's stale
            # judgement result was observed and discarded.
            assert autopilot_orchestration.worker_target_still_current(
                session, b_target) is True
    assert after == before
    assert route_calls["n"] == 0  # A never routed either
    assert fail_close_calls["n"] == 0  # A never fail-closed either
    assert a_asr.calls == 1
    assert a_judge.calls == 1

    # A's own result -- not inferred, directly observed -- proves it actually
    # took the AttemptEvent-level claim_lost branch.
    assert len(a_results) == 1
    assert a_results[0].get("status") == "completed"
    assert a_results[0].get("claim_lost") is True

    # A fresh recovery worker now routes the completed-but-unrouted attempt
    # exactly once.
    _run_p0a_attempt_worker(SESSION_ID)
    with Session(api_clients.engine) as session:
        control = session.get(SessionAutopilotState, SESSION_ID)
        assert control is not None and control.status == "waiting_tts"
        commands = list(session.exec(select(RuntimeCommand).where(
            RuntimeCommand.session_id == SESSION_ID,
        )))
        assert len(commands) == 3
    assert route_calls["n"] == 1
    assert fail_close_calls["n"] == 0


def test_manual_http_terminal_readback_never_leaks_claim_lost_marker(
        api_clients: ApiClients, monkeypatch):
    """Codex audit item 2: ``claim_lost`` is an ``autopilot_worker``-only
    internal dispatch signal. The exact same terminal-attempt read-back
    through ``_claim_lost_payload`` must never carry it on the
    ``manual_http`` plane -- device-side strict parsers reject unexpected
    extra response keys (see the docstring on ``_claim_lost_payload``).

    Both payloads are first proven to be the genuine terminal read-back this
    helper is documented to produce (status/idempotent), then proven
    identical to each other in every field except ``claim_lost`` itself --
    not just "missing a key", but "nothing else differs either" -- and the
    key is checked absent from both the dict and its serialized form.
    """
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)
    fake_asr = _WorkerAsr()
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)
    _run_p0a_attempt_worker(SESSION_ID)

    with Session(api_clients.engine) as session:
        attempt = session.exec(select(AttemptEvent).where(
            AttemptEvent.raw_audio_id == capture["raw_audio_id"],
        )).one()
        assert attempt.processing_status == "completed"
        # _claim_lost_payload only uses claim.attempt_id to re-read the
        # current (already terminal) row; a claim's own owner/generation are
        # irrelevant to this read-back path, exactly as a genuinely lost
        # claim's would be by the time this helper runs.
        stale_claim = evidence_ledger.AttemptClaim(
            attempt_id=attempt.id, owner="stale-owner", generation=0,
            stage="asr_completed", lease_expires_at=datetime.now())

        manual_payload = main_module._claim_lost_payload(
            stale_claim, session, Response(), control_plane="manual_http")
        assert manual_payload.get("status") == "completed"
        assert manual_payload.get("idempotent") is True
        assert "claim_lost" not in manual_payload
        assert "claim_lost" not in json.dumps(manual_payload, default=str)

        worker_payload = main_module._claim_lost_payload(
            stale_claim, session, Response(), control_plane="autopilot_worker")
        assert worker_payload.get("status") == "completed"
        assert worker_payload.get("idempotent") is True
        assert worker_payload.get("claim_lost") is True

        # The two payloads agree on every field except claim_lost itself --
        # the gating adds exactly one key, changes nothing else.
        worker_base = {
            key: value for key, value in worker_payload.items()
            if key != "claim_lost"
        }
        assert worker_base == manual_payload


def _judgement_fail_close_snapshot(session: Session, raw_audio_id: str) -> dict:
    """Field-precise snapshot for the judgement-stage fail-close test.

    Deliberately named fields, not an opaque tuple like ``_live_snapshot``:
    this test asserts exact revision/generation arithmetic (``+1``, not just
    "changed"). Also covers every field a late (post-pause) worker A could
    still corrupt without ever touching lease/generation fencing: full
    Attempt ASR/judge facts, full Capture identity/lifecycle facts, and the
    InteractionEvent/ItemEvent/TurnEvent projections A's own delayed
    judgement completion would append/create if it were ever wrongly allowed
    to proceed past the pause.
    """
    control = session.get(SessionAutopilotState, SESSION_ID)
    runtime_state = session.get(SessionRuntimeState, SESSION_ID)
    attempt = session.exec(select(AttemptEvent).where(
        AttemptEvent.raw_audio_id == raw_audio_id,
    )).one()
    capture_row = session.exec(select(AttemptCaptureProcessing).where(
        AttemptCaptureProcessing.raw_audio_id == raw_audio_id,
    )).one()
    interactions = tuple(
        (e.id, e.event_seq, e.event_type, e.attempt_id, e.payload_json)
        for e in session.exec(select(InteractionEvent).where(
            InteractionEvent.session_id == SESSION_ID,
        ).order_by(InteractionEvent.event_seq))
    )
    items = tuple(
        (i.id, i.item_id) for i in session.exec(select(ItemEvent).where(
            ItemEvent.session_id == SESSION_ID,
        ).order_by(ItemEvent.id))
    )
    turns = tuple(
        (t.id, t.item_event_id, t.turn_seq, t.source_attempt_id,
         t.ai_answer_type, t.ai_score, t.ai_needs_review, t.ai_judge_mode,
         t.reviewer_id, t.reviewed_score, t.score_locked, t.element_value)
        for t in session.exec(
            select(TurnEvent)
            .join(ItemEvent, TurnEvent.item_event_id == ItemEvent.id)
            .where(ItemEvent.session_id == SESSION_ID)
            .order_by(TurnEvent.id))
    )
    return {
        "control_status": control.status if control else None,
        "control_revision": control.revision if control else None,
        "control_current_command_id": control.current_command_id if control else None,
        "control_last_error_code": control.last_error_code if control else None,
        "runtime_status": runtime_state.status if runtime_state else None,
        "runtime_revision": runtime_state.revision if runtime_state else None,
        "attempt_status": attempt.processing_status,
        "attempt_owner": attempt.processing_owner,
        "attempt_lease_expires_at": attempt.processing_lease_expires_at,
        "attempt_claimed_at": attempt.processing_claimed_at,
        "attempt_generation": attempt.processing_generation,
        "attempt_judge_portrait_used": attempt.judge_portrait_used,
        "attempt_asr_text": attempt.asr_text,
        "attempt_asr_confidence": attempt.asr_confidence,
        "attempt_asr_engine_version": attempt.asr_engine_version,
        "attempt_operational_answer_type": attempt.operational_answer_type,
        "attempt_operational_score": attempt.operational_score,
        "attempt_operational_needs_review": attempt.operational_needs_review,
        "attempt_judge_mode": attempt.judge_mode,
        "attempt_judge_engine_version": attempt.judge_engine_version,
        "attempt_judge_reason": attempt.judge_reason,
        "attempt_matched_on": attempt.matched_on,
        "attempt_contains_target": attempt.contains_target,
        "attempt_error_code": attempt.error_code,
        "attempt_processed_at": attempt.processed_at,
        "capture_status": capture_row.processing_status,
        "capture_owner": capture_row.processing_owner,
        "capture_lease_expires_at": capture_row.processing_lease_expires_at,
        "capture_claimed_at": capture_row.processing_claimed_at,
        "capture_generation": capture_row.processing_generation,
        "capture_final_attempt_id": capture_row.final_attempt_id,
        "capture_asr_confidence": capture_row.asr_confidence,
        "capture_asr_engine_version": capture_row.asr_engine_version,
        "capture_disposition": capture_row.disposition,
        "capture_error_code": capture_row.error_code,
        "capture_processed_at": capture_row.processed_at,
        "capture_created_at": capture_row.created_at,
        "capture_id": capture_row.id,
        "capture_raw_audio_id": capture_row.raw_audio_id,
        "capture_session_id": capture_row.session_id,
        "capture_item_id": capture_row.item_id,
        "capture_turn_seq": capture_row.turn_seq,
        "capture_record_command_id": capture_row.record_command_id,
        "capture_proof_attempt_seq": capture_row.proof_attempt_seq,
        "capture_proof_prompt_level": capture_row.proof_prompt_level,
        "interactions": interactions,
        "items": items,
        "turns": turns,
    }


def test_matching_target_judgement_stage_failure_pauses_atomically_and_clears_only_its_attempt_claim(
        api_clients: ApiClients, monkeypatch):
    """Codex audit item 3, the positive counterpart of item 1: a target-bound
    fail-close while a worker is blocked in judgement -- i.e. while an
    AttemptEvent (not just the earlier capture row) holds the live claim --
    must still pause control+runtime atomically, write exactly one failure
    event, and clear only the exact target's AttemptEvent claim. The bound
    capture row must still correctly report its terminal ASR outcome and its
    final_attempt_id link, untouched by the pause. A second fail-close call,
    and A's own delayed judgement result finally landing, must cause no
    further change to any of it.
    """
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)

    a_asr = _WrongAnswerAsr()
    a_judge = _BlockingWorkerJudge()
    monkeypatch.setattr(asr, "get_engine", lambda: a_asr)
    monkeypatch.setattr(llm_judge, "get_engine", lambda: a_judge)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker_a = pool.submit(_run_p0a_attempt_worker, SESSION_ID)
        after_fail_close = None
        try:
            assert a_judge.entered.wait(timeout=10), "worker never entered judgement"

            with Session(api_clients.engine) as session:
                target = autopilot_orchestration.derive_worker_target(
                    session, session_id=SESSION_ID)
                session.rollback()
                before = _judgement_fail_close_snapshot(
                    session, capture["raw_audio_id"])
            assert before["attempt_status"] == "asr_completed"
            assert before["attempt_owner"] is not None
            assert before["attempt_lease_expires_at"] is not None
            assert before["capture_status"] == "asr_completed"
            assert before["capture_final_attempt_id"] is not None
            # Judgement never ran yet (A is still blocked in it): no
            # judgement_completed interaction and no review-ledger
            # projection exist before the pause either.
            assert before["attempt_judge_mode"] is None
            assert before["items"] == ()
            assert before["turns"] == ()

            _fail_closed_p0a_attempt_worker(
                SESSION_ID, "autopilot_attempt_route_failed", target=target)

            with Session(api_clients.engine) as session:
                events = list(session.exec(select(AutopilotControlEvent).where(
                    AutopilotControlEvent.session_id == SESSION_ID,
                    AutopilotControlEvent.event_type == "failure",
                )))
                after_fail_close = _judgement_fail_close_snapshot(
                    session, capture["raw_audio_id"])
            assert after_fail_close["control_status"] == "paused"
            assert (after_fail_close["control_revision"]
                    == before["control_revision"] + 1)
            assert after_fail_close["runtime_status"] == "paused"
            assert (after_fail_close["runtime_revision"]
                    == before["runtime_revision"] + 1)
            assert len(events) == 1
            assert events[0].command_id == target.record_command_id
            assert events[0].reason_code == "autopilot_attempt_route_failed"
            assert json.loads(events[0].payload_json) == {
                "error_code": "autopilot_attempt_route_failed",
                "source": "worker_exception",
            }
            assert after_fail_close["control_current_command_id"] is None
            assert (after_fail_close["control_last_error_code"]
                    == "autopilot_attempt_route_failed")
            # Exact target's AttemptEvent claim is cleared, its fencing
            # generation moved by exactly one, and its underlying ASR fact/
            # status is untouched (still recoverable, never forced to a
            # terminal status by a pause).
            assert after_fail_close["attempt_status"] == "asr_completed"
            assert after_fail_close["attempt_owner"] is None
            assert after_fail_close["attempt_lease_expires_at"] is None
            assert after_fail_close["attempt_claimed_at"] is None
            assert (after_fail_close["attempt_generation"]
                    == before["attempt_generation"] + 1)
            # The capture row -- generation, final_attempt_id link, every ASR
            # fact, and its full identity -- is completely untouched by this
            # pause: it was already terminal (asr_completed is not in the
            # recoverable capture set invalidate_capture_processing_claims
            # fences). Likewise the attempt's own ASR/judge content facts
            # (judgement never having run), and the InteractionEvent/
            # ItemEvent/TurnEvent projections (still empty of judgement):
            # this pause writes lease/generation/control-state fields only,
            # nothing else.
            for key in (
                    "capture_status", "capture_owner", "capture_lease_expires_at",
                    "capture_claimed_at", "capture_generation",
                    "capture_final_attempt_id", "capture_asr_confidence",
                    "capture_asr_engine_version", "capture_disposition",
                    "capture_error_code", "capture_processed_at",
                    "capture_created_at", "capture_id", "capture_raw_audio_id",
                    "capture_session_id", "capture_item_id", "capture_turn_seq",
                    "capture_record_command_id", "capture_proof_attempt_seq",
                    "capture_proof_prompt_level",
                    "attempt_asr_text", "attempt_asr_confidence",
                    "attempt_asr_engine_version", "attempt_operational_answer_type",
                    "attempt_operational_score", "attempt_operational_needs_review",
                    "attempt_judge_mode", "attempt_judge_engine_version",
                    "attempt_judge_reason", "attempt_matched_on",
                    "attempt_contains_target", "attempt_error_code",
                    "attempt_processed_at", "attempt_judge_portrait_used",
                    "interactions", "items", "turns"):
                assert after_fail_close[key] == before[key], key
            assert after_fail_close["items"] == ()
            assert after_fail_close["turns"] == ()

            # Repeating the same (now stale, since state is paused) fail-close
            # must not double-pause or write a second event.
            with Session(api_clients.engine) as session:
                again = autopilot_orchestration.stage_processing_failure(
                    session, session_id=SESSION_ID,
                    error_code="autopilot_attempt_route_failed",
                    source="worker_exception", target=target)
                assert again is False
                session.rollback()
                events_again = list(session.exec(select(AutopilotControlEvent).where(
                    AutopilotControlEvent.session_id == SESSION_ID,
                    AutopilotControlEvent.event_type == "failure",
                )))
                assert len(events_again) == 1
                repeat_snapshot = _judgement_fail_close_snapshot(
                    session, capture["raw_audio_id"])
            assert repeat_snapshot == after_fail_close
        finally:
            # Release and join before any assertion above can leave the pool
            # waiting on a thread that will never unblock, and before
            # monkeypatch tears down the provider patches A's thread still
            # depends on. A's own now-invalidated claim makes its resumed
            # judgement CAS a graceful no-op (see the same-target claim-lost
            # test above), not a second fail-close.
            a_judge.release.set()
            worker_a.result(timeout=10)

        with Session(api_clients.engine) as session:
            events_after_join = list(session.exec(select(AutopilotControlEvent).where(
                AutopilotControlEvent.session_id == SESSION_ID,
                AutopilotControlEvent.event_type == "failure",
            )))
            after_join = _judgement_fail_close_snapshot(
                session, capture["raw_audio_id"])
    assert len(events_after_join) == 1
    assert after_join == after_fail_close


def test_capture_processing_internals_never_leak_through_read_endpoints(
        api_clients: ApiClients, monkeypatch):
    """Governance: capture-processing internals are not part of any read contract.

    ``/attempts`` and ``/journal`` are the two general-purpose read endpoints
    that already project AttemptEvent/InteractionEvent. Neither queries
    AttemptCaptureProcessing at all (verified independently by source
    inspection); this guards against a future change silently starting to.
    """
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)
    fake_asr = _WorkerAsr()
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)
    _run_p0a_attempt_worker(SESSION_ID)

    with Session(api_clients.engine) as session:
        capture_row = session.exec(select(AttemptCaptureProcessing).where(
            AttemptCaptureProcessing.raw_audio_id == capture["raw_audio_id"],
        )).one()
        assert capture_row.processing_status == "asr_completed"

    capture_only_fields = (
        "record_command_id", "predecessor_command_id", "receipt_server_seq",
        "proof_attempt_seq", "proof_prompt_level", "disposition",
        "final_attempt_id",
    )
    attempts_response = api_clients.account.get(f"/sessions/{SESSION_ID}/attempts")
    assert attempts_response.status_code == 200, attempts_response.text
    journal_response = api_clients.account.get(f"/sessions/{SESSION_ID}/journal")
    assert journal_response.status_code == 200, journal_response.text
    for response in (attempts_response, journal_response):
        body_text = response.text
        for field in capture_only_fields:
            assert field not in body_text, (
                f"{field!r} leaked through {response.request.url}")


def test_record_stopped_exact_replay_keeps_single_capture_row_and_identity(
        api_clients: ApiClients, monkeypatch):
    """An exact record_stopped ACK replay must never duplicate or mutate the
    persistent capture-processing row admitted on the first delivery.
    """
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)

    with Session(api_clients.engine) as session:
        before = session.exec(select(AttemptCaptureProcessing).where(
            AttemptCaptureProcessing.raw_audio_id == capture["raw_audio_id"],
        )).one()
        before_snapshot = (
            before.id, before.record_command_id, before.predecessor_command_id,
            before.receipt_server_seq, before.raw_audio_id, before.session_id,
            before.item_id, before.turn_seq, before.proof_attempt_seq,
            before.proof_prompt_level, before.processing_status,
            before.processing_owner, before.processing_generation,
        )

    record = capture["record"]
    replay = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{record['command_key']}/acks",
        headers=api_clients.device_headers,
        json=_ack_body(
            record,
            ack_type="record_stopped",
            ack_key="ack-worker-record-stopped-0001",
            device_event_seq=2,
            stop_reason=capture["stop_reason"],
            raw_audio_id=capture["raw_audio_id"],
            receipt_server_seq=capture["receipt_server_seq"],
            checksum=capture["checksum"],
            byte_count=capture["byte_count"],
            duration_seconds=capture["duration_seconds"],
        ),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "processing_attempt"

    with Session(api_clients.engine) as session:
        rows = list(session.exec(select(AttemptCaptureProcessing).where(
            AttemptCaptureProcessing.raw_audio_id == capture["raw_audio_id"],
        )))
        assert len(rows) == 1
        after = rows[0]
        after_snapshot = (
            after.id, after.record_command_id, after.predecessor_command_id,
            after.receipt_server_seq, after.raw_audio_id, after.session_id,
            after.item_id, after.turn_seq, after.proof_attempt_seq,
            after.proof_prompt_level, after.processing_status,
            after.processing_owner, after.processing_generation,
        )
        assert after_snapshot == before_snapshot


def test_device_rotation_during_active_processing_fences_both_claims_and_pauses(
        api_clients: ApiClients, monkeypatch):
    """A same-session re-pair while a capture claim is active must fail-safe.

    Gap this closes: /device/pair used to revoke the old capability and
    commit a new one with zero awareness of autopilot state. A stale
    in-flight ASR result bound to the old device generation must not
    silently materialize and then route a command to the newly paired
    device that never asked for or saw the old recording.
    """
    _enable_p0a(monkeypatch)
    capture = _drive_to_processing_attempt(api_clients)
    blocked_asr = _BlockingWorkerAsr()
    monkeypatch.setattr(asr, "get_engine", lambda: blocked_asr)

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(_run_p0a_attempt_worker, SESSION_ID)
        assert blocked_asr.entered.wait(timeout=10), "worker never entered ASR"
        new_device = TestClient(app)
        try:
            paired = new_device.post(
                "/device/pair",
                headers={"X-Console-Pin": "246810"},
                json={"deviceId": "p0a-http-device-rotated-000002"},
            )
            assert paired.status_code == 200, paired.text
            new_device_headers = {
                "X-Device-Capability": paired.json()["capability"]}
        finally:
            blocked_asr.release.set()
        worker.result(timeout=10)

    with Session(api_clients.engine) as session:
        control = session.get(SessionAutopilotState, SESSION_ID)
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        capture_row = session.exec(select(AttemptCaptureProcessing).where(
            AttemptCaptureProcessing.raw_audio_id == capture["raw_audio_id"],
        )).one()
        assert list(session.exec(select(AttemptEvent))) == []
        assert control is not None and control.status == "paused"
        assert control.last_error_code == "autopilot_device_rotated"
        assert control.current_command_id is None
        assert runtime_state is not None and runtime_state.status == "paused"
        assert capture_row.processing_status == "received"
        assert capture_row.processing_owner is None
    assert blocked_asr.calls == 1

    # The scope and runtime are paused: the newly paired device gets no
    # autonomous command, fail-closed rather than a stale/empty 200.
    next_for_new_device = new_device.get(
        f"/sessions/{SESSION_ID}/autopilot/next", headers=new_device_headers)
    assert next_for_new_device.status_code == 409, next_for_new_device.text
    assert next_for_new_device.json()["detail"]["code"] == "autopilot_runtime_inactive"
    new_device.close()


def test_device_rotation_fencing_failure_rolls_back_capability_atomically(
        api_clients: ApiClients, monkeypatch):
    """A failure while fencing a rotation must undo the capability rotation too.

    There must be no window where a new capability is durable but the old
    generation's claim is still unfenced: pairing and its fencing commit in
    one transaction, or neither does.
    """
    _enable_p0a(monkeypatch)
    _drive_to_processing_attempt(api_clients)

    with Session(api_clients.engine) as session:
        old_capability = session.exec(select(PatientDeviceCapability).where(
            PatientDeviceCapability.session_id == SESSION_ID,
            PatientDeviceCapability.revoked_at.is_(None),
        )).one()
        old_token_hash = old_capability.token_hash

    def _boom(*_args, **_kwargs):
        raise autopilot_service.AutopilotServiceError(
            "test_injected_fencing_failure", "注入测试：换绑 fencing 失败")

    monkeypatch.setattr(
        autopilot_service, "fence_autonomous_scope_for_device_rotation", _boom)
    new_device = TestClient(app)
    try:
        response = new_device.post(
            "/device/pair",
            headers={"X-Console-Pin": "246810"},
            json={"deviceId": "p0a-http-device-fault-000003"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "test_injected_fencing_failure"

        with Session(api_clients.engine) as session:
            capabilities = list(session.exec(select(PatientDeviceCapability).where(
                PatientDeviceCapability.session_id == SESSION_ID,
            )))
            assert len(capabilities) == 1
            assert capabilities[0].token_hash == old_token_hash
            assert capabilities[0].revoked_at is None
            control = session.get(SessionAutopilotState, SESSION_ID)
            assert control is not None and control.status == "processing_attempt"

        # The old device's capability is untouched and still fully usable.
        still_works = api_clients.device.get(
            f"/sessions/{SESSION_ID}/autopilot/next",
            headers=api_clients.device_headers)
        assert still_works.status_code == 200, still_works.text
    finally:
        new_device.close()


def test_device_pair_capability_collision_rolls_back_entire_request_atomically(
        api_clients: ApiClients, monkeypatch):
    """A concurrent-pairing collision must fail the whole request, never
    partially commit off a stale ``had_active_capability``/LiveState
    snapshot taken moments before the collision.

    ``create_capability`` deliberately no longer catches/retries its own
    IntegrityError internally (an internal rollback-and-retry would silently
    invalidate the LiveState/session/runtime reads and the
    had_active_capability fact already taken earlier in this same
    transaction, without the caller's knowledge). The whole request must
    fail atomically instead, leaving the old capability and autopilot state
    completely untouched for the client to retry from a fresh snapshot.
    """
    _enable_p0a(monkeypatch)
    _drive_to_processing_attempt(api_clients)

    with Session(api_clients.engine) as session:
        old_capability = session.exec(select(PatientDeviceCapability).where(
            PatientDeviceCapability.session_id == SESSION_ID,
            PatientDeviceCapability.revoked_at.is_(None),
        )).one()
        old_token_hash = old_capability.token_hash

    def _boom(*_args, **_kwargs):
        raise IntegrityError(
            "INSERT", {}, Exception("simulated concurrent pairing collision"))

    monkeypatch.setattr(device_capability, "create_capability", _boom)
    new_device = TestClient(app)
    try:
        response = new_device.post(
            "/device/pair",
            headers={"X-Console-Pin": "246810"},
            json={"deviceId": "p0a-http-device-integrity-000004"},
        )
        assert response.status_code == 503, response.text
    finally:
        new_device.close()

    with Session(api_clients.engine) as session:
        capabilities = list(session.exec(select(PatientDeviceCapability).where(
            PatientDeviceCapability.session_id == SESSION_ID,
        )))
        assert len(capabilities) == 1
        assert capabilities[0].token_hash == old_token_hash
        assert capabilities[0].revoked_at is None
        control = session.get(SessionAutopilotState, SESSION_ID)
        assert control is not None and control.status == "processing_attempt"

    still_works = api_clients.device.get(
        f"/sessions/{SESSION_ID}/autopilot/next", headers=api_clients.device_headers)
    assert still_works.status_code == 200, still_works.text


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


def _exact_tts_path(command: dict) -> str:
    return (
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{command['command_key']}/tts"
    )


def test_exact_tts_serve_appends_command_bound_engine_evidence(
        api_clients: ApiClients, monkeypatch):
    """每次实际返回音频都追加一行服务端引擎证据，且精确绑定命令。"""
    _enable_p0a(monkeypatch)
    monkeypatch.setattr(
        "app.main.tts.speak_autopilot",
        lambda text: (b"RIFF-evidence-audio", "dashscope/qwen3-tts-flash/Serena", False))
    started = _start(api_clients)
    assert started.status_code == 200, started.text
    command = _device_next(api_clients)
    assert command is not None and command["kind"] == "tts"

    first = api_clients.device.post(
        _exact_tts_path(command), headers=api_clients.device_headers, json={})
    assert first.status_code == 200, first.text
    replay = api_clients.device.post(
        _exact_tts_path(command), headers=api_clients.device_headers, json={})
    assert replay.status_code == 200, replay.text

    with Session(api_clients.engine) as session:
        rows = list(session.exec(
            select(TtsServeEvidence).order_by(TtsServeEvidence.id)))
        assert len(rows) == 2
        command_row = session.exec(select(RuntimeCommand).where(
            RuntimeCommand.idempotency_key == command["command_key"],
        )).first()
        assert command_row is not None
        expected_sha = hashlib.sha256(
            BANK.single_element[0]["initial_prompt"].encode("utf-8")).hexdigest()
        for row in rows:
            assert row.source == "autopilot_command"
            assert row.command_id == command_row.id
            assert row.session_id == SESSION_ID
            assert row.engine_version == "dashscope/qwen3-tts-flash/Serena"
            assert row.cache_hit is False
            assert row.result == "served"
            assert row.byte_count == len(b"RIFF-evidence-audio")
            assert row.text_sha256 == expected_sha
            assert row.is_simulation is True


def test_exact_tts_degraded_serve_records_honest_degradation(
        api_clients: ApiClients, monkeypatch):
    """降级(204)同样如实入账：result=degraded、无字节数、引擎为实际尝试者。"""
    _enable_p0a(monkeypatch)
    # The strict path can only ever attribute a degradation to Qwen: a local
    # engine is not in its chain, so it can never be the honest attempt either.
    monkeypatch.setattr(
        "app.main.tts.speak_autopilot",
        lambda text: (None, "dashscope/qwen3-tts-flash/Serena", False))
    started = _start(api_clients)
    assert started.status_code == 200, started.text
    command = _device_next(api_clients)
    assert command is not None and command["kind"] == "tts"

    degraded = api_clients.device.post(
        _exact_tts_path(command), headers=api_clients.device_headers, json={})
    assert degraded.status_code == 204, degraded.text

    with Session(api_clients.engine) as session:
        rows = list(session.exec(select(TtsServeEvidence)))
        assert len(rows) == 1
        assert rows[0].result == "degraded"
        assert rows[0].byte_count is None
        assert rows[0].engine_version == "dashscope/qwen3-tts-flash/Serena"
        assert rows[0].command_id is not None


def test_discarded_tts_bytes_leave_no_usage_evidence(
        api_clients: ApiClients, monkeypatch):
    """授权复核失败被丢弃的音频不得留下任何"已使用"证据行。"""
    _enable_p0a(monkeypatch)
    synthesized: list[str] = []
    generic_calls: list[str] = []

    def fake_speak_autopilot(text: str):
        synthesized.append(text)
        return b"RIFF-discarded-audio", "dashscope/qwen3-tts-flash/Serena", False

    def unreachable_generic_speak(text: str):  # pragma: no cover - guard only
        generic_calls.append(text)
        raise AssertionError("exact route must not use the generic synthesizer")

    # This route is the exact one, so the strict seam is the one that has to be
    # replaced; patching the generic seam left the real Qwen resolver running
    # and the assertion below could pass on a repository cache hit instead.
    monkeypatch.setattr("app.main.tts.speak_autopilot", fake_speak_autopilot)
    monkeypatch.setattr("app.main.tts.speak", unreachable_generic_speak)
    started = _start(api_clients)
    assert started.status_code == 200, started.text
    command = _device_next(api_clients)
    assert command is not None and command["kind"] == "tts"

    real_text = autopilot_service.authorized_tts_text
    calls = {"n": 0}

    def flaky_authorized_tts_text(db, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise autopilot_service.AutopilotServiceError(
                "autopilot_command_not_current", "复核时命令已不是当前命令")
        return real_text(db, **kwargs)

    monkeypatch.setattr(
        autopilot_service, "authorized_tts_text", flaky_authorized_tts_text)
    discarded = api_clients.device.post(
        _exact_tts_path(command), headers=api_clients.device_headers, json={})
    assert discarded.status_code == 409, discarded.text
    assert discarded.json()["detail"]["code"] == "tts_authorization_changed"
    # The bytes really were produced and then really were thrown away: without
    # this the 409 alone would also be satisfied by a synthesis that never ran.
    assert len(synthesized) == 1
    assert generic_calls == []

    with Session(api_clients.engine) as session:
        assert list(session.exec(select(TtsServeEvidence))) == []


def test_generic_tts_speak_appends_live_speak_evidence(
        api_clients: ApiClients, monkeypatch):
    """通用 /tts/speak 路径的证据行：source=live_speak、无命令绑定、带场次。"""
    monkeypatch.setattr(
        "app.main.tts.speak",
        lambda text: (b"RIFF-live-audio", "piper/zh_CN-huayan-medium", True))
    spoken = api_clients.device.post(
        "/tts/speak",
        headers=api_clients.device_headers,
        json={"text": "你好呀"},
    )
    assert spoken.status_code == 200, spoken.text

    with Session(api_clients.engine) as session:
        rows = list(session.exec(select(TtsServeEvidence)))
        assert len(rows) == 1
        assert rows[0].source == "live_speak"
        assert rows[0].command_id is None
        assert rows[0].session_id == SESSION_ID
        assert rows[0].cache_hit is True
        assert rows[0].result == "served"


def test_ai_usage_endpoint_reports_actual_usage_not_probe(
        api_clients: ApiClients, monkeypatch):
    """/ai-usage 只聚合服务端实际使用账本；匿名不可读。"""
    _enable_p0a(monkeypatch)
    monkeypatch.setattr(
        "app.main.tts.speak_autopilot",
        lambda text: (b"RIFF-usage-audio", "dashscope/qwen3-tts-flash/Serena", False))
    started = _start(api_clients)
    assert started.status_code == 200, started.text
    command = _device_next(api_clients)
    assert command is not None
    served = api_clients.device.post(
        _exact_tts_path(command), headers=api_clients.device_headers, json={})
    assert served.status_code == 200, served.text

    denied = api_clients.anonymous.get(f"/sessions/{SESSION_ID}/ai-usage")
    assert denied.status_code in (401, 403), denied.text

    usage = api_clients.account.get(f"/sessions/{SESSION_ID}/ai-usage")
    assert usage.status_code == 200, usage.text
    body = usage.json()
    assert body["tts"]["engines"] == [{
        "engine_version": "dashscope/qwen3-tts-flash/Serena",
        "served": 1, "cache_hits": 0, "degraded": 0,
    }]
    assert body["asr"]["engines"] == []
    assert body["asr"]["degraded_attempts"] == 0
    assert body["judge"]["modes"] == []


def test_account_status_stays_readable_after_the_api_record_stop_chain(
        api_clients: ApiClients, monkeypatch):
    """录音收口进入 processing_attempt 后，账号端仍读得到控制状态。

    这一格由完整的后端 API 状态转换链走出来，不是直接改库构造的：tts_ended
    -> 上传 -> audioSaved -> record_stopped，全程走 TestClient 的 HTTP 路由。
    它不涉及真浏览器、真设备或真麦克风。状态查询必须投影出被保留的 record
    命令类型，而不是把合法的处理中状态当成状态机自相矛盾。
    """
    _enable_p0a(monkeypatch)
    _drive_to_processing_attempt(api_clients)

    status = api_clients.account.get(
        f"/sessions/{SESSION_ID}/autopilot/status")
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["scope_key"] == "p0a_sim_first_single_v1"
    assert body["mode"] == "autonomous"
    assert body["status"] == "processing_attempt"
    assert body["server_owned"] is True
    assert body["current_command_kind"] == "record"
    assert body["last_error_code"] is None
    assert body["state_revision"] >= 1
    assert _all_keys(body).isdisjoint({
        "command", "payload", "speech_text", "image_id", "item_ref",
        "item_id", "turn_key", "target_word", "cues", "tell_answer",
        "raw_audio_id", "checksum", "stop_reason", "receipt_server_seq",
        "issued_capability_token_hash", "issued_device_id_hash", "issued_at",
    })
