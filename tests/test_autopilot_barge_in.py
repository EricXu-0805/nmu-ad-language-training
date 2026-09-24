# ruff: noqa: F811 -- pytest injects the imported isolated fixture.
"""Local voice monitoring cannot become capture or bypass current authority."""
from datetime import datetime

import pytest
from sqlmodel import Session, select

from app import asr, export
from app.main import _run_p0a_attempt_worker
from app.models import (AttemptEvent, AudioAssetRow, Patient,
                        PatientDeviceCapability, RuntimeCommand,
                        RuntimeCommandAck, SessionRuntimeState)
from test_autopilot_api import (  # noqa: F401
    PATIENT_ID, SESSION_ID, _device_next, _enable_p0a, _start, api_clients,
)
from test_autopilot_answer_now import _post
from test_autopilot_repeat import _complete_record, _RepeatAsr


def _started(clients, monkeypatch):
    _enable_p0a(monkeypatch)
    assert _start(clients).status_code == 200
    pending = _device_next(clients)
    response = _post(clients, pending, "tts_started", 1)
    assert response.status_code == 200, response.text
    return response.json()["command"]


def _authorization(clients, command, **changes):
    body = {key: command[key] for key in (
        "command_revision", "control_generation", "runner_generation")}
    body.update(changes)
    return clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/{command['command_key']}/barge-in-authorization",
        headers=clients.device_headers, json=body)


def test_monitor_authorization_is_repeatable_but_creates_no_audio_or_ack(api_clients, monkeypatch):
    command = _started(api_clients, monkeypatch)
    for _ in range(2):
        result = _authorization(api_clients, command)
        assert result.status_code == 200, result.text
        assert result.json() == {"allowed": True, "barge_in_authorized": True,
                                 "runtime_status": "active", "is_simulation": True}
    with Session(api_clients.engine) as db:
        assert list(db.exec(select(AudioAssetRow))) == []
        assert len(list(db.exec(select(RuntimeCommand)))) == 1
        assert [row.ack_type for row in db.exec(select(RuntimeCommandAck))] == ["tts_started"]
    path = f"/sessions/{SESSION_ID}/autopilot/commands/{command['command_key']}/recording-authorization"
    denied = api_clients.device.post(path, headers=api_clients.device_headers)
    assert denied.status_code == 409


@pytest.mark.parametrize("field", ["command_revision", "control_generation", "runner_generation"])
def test_monitor_rejects_stale_command_identity(api_clients, monkeypatch, field):
    command = _started(api_clients, monkeypatch)
    assert _authorization(api_clients, command, **{field: command[field] + 1}).status_code == 409


@pytest.mark.parametrize("changes", [{"command_revision": True}, {"control_generation": "1"},
                                     {"runner_generation": 0}, {"capture": True}])
def test_monitor_request_is_closed_and_strict(api_clients, monkeypatch, changes):
    command = _started(api_clients, monkeypatch)
    assert _authorization(api_clients, command, **changes).status_code == 422


@pytest.mark.parametrize("revocation", ["consent", "recording", "withdrawal", "pause", "device", "recovery"])
def test_permission_returning_late_rechecks_authority(api_clients, monkeypatch, revocation):
    command = _started(api_clients, monkeypatch)
    assert _authorization(api_clients, command).status_code == 200
    with Session(api_clients.engine) as db:
        patient = db.get(Patient, PATIENT_ID)
        if revocation == "consent":
            patient.consent_status = "withdrawn"
        elif revocation == "recording":
            patient.recording_allowed = False
        elif revocation == "withdrawal":
            patient.withdrawal_status = "withdrawn"
        elif revocation == "pause":
            db.get(SessionRuntimeState, SESSION_ID).status = "paused"
        else:
            device = db.exec(select(PatientDeviceCapability)).first()
            if revocation == "device":
                device.revoked_at = datetime.now()
            else:
                device.recovery_only_at = datetime.now()
        db.commit()
    assert _authorization(api_clients, command).status_code in {401, 403, 409}


def test_pending_prompt_and_account_cannot_monitor(api_clients, monkeypatch):
    _enable_p0a(monkeypatch)
    assert _start(api_clients).status_code == 200
    command = _device_next(api_clients)
    # Revision zero is not a started command, even when a caller supplies one.
    assert _authorization(api_clients, command, command_revision=1).status_code == 409
    path = f"/sessions/{SESSION_ID}/autopilot/commands/{command['command_key']}/barge-in-authorization"
    for client in (api_clients.account, api_clients.anonymous):
        assert client.post(path, json={"command_revision": 1, "control_generation": 1,
                                      "runner_generation": 1}).status_code in {401, 403}


def test_voice_interruption_preserves_score_review_and_distinct_export(api_clients, monkeypatch):
    command = _started(api_clients, monkeypatch)
    monkeypatch.setenv("ENABLE_LLM_JUDGE", "0")
    monkeypatch.setattr(asr, "get_engine", lambda: _RepeatAsr("胡萝卜"))
    assert _authorization(api_clients, command).status_code == 200
    facts = {"media_stopped": True, "interrupt_reason": "voice_activity", "media_duration_ms": 750}
    response = _post(api_clients, command, "tts_interrupted", 2, **facts)
    assert response.status_code == 200, response.text
    record = response.json()["command"]
    assert record["prompt_level"] == command["prompt_level"]
    assert _authorization(api_clients, command).status_code == 409
    assert _post(api_clients, command, "tts_interrupted", 2, **facts).json()["replayed"] is True
    _complete_record(api_clients, record, suffix="voice-barge", device_seq=3)
    _run_p0a_attempt_worker(SESSION_ID)
    with Session(api_clients.engine) as db:
        attempt = db.exec(select(AttemptEvent)).one()
        assert attempt.processing_status == "completed"
        assert attempt.contains_target is True
        assert attempt.operational_needs_review is True
        assert export._prompt_playback_evidence(db, attempt) == {
            "prompt_playback_outcome": "interrupted_voice_activity", "prompt_played_ms": 750,
            "prompt_exposure_needs_review": True}
    feedback = _device_next(api_clients)
    started = _post(api_clients, feedback, "tts_started", 4)
    assert started.status_code == 200, started.text
    assert _authorization(api_clients, started.json()["command"]).status_code == 409
