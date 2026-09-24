"""A provider outage may block AI continuation without discarding bedside facts."""
import pytest
from sqlmodel import Session, select

from app import asr, provider_readiness
from app.main import _run_p0a_attempt_worker
from app.models import (
    AttemptEvent, AuditLog, AutopilotControlEvent, AutopilotPositionAdjudication,
    Patient, RuntimeCommand, Session as TrainSession, SessionAutopilotState,
    SessionRuntimeState, TurnEvent,
)
from test_autopilot_api import (
    FIRST_ONLY_BANK, PATIENT_ID, SESSION_ID, TWO_ONLY_BANK, _EmptyTranscriptAsr,
    _adjudicate, _bind_api_bank, _device_next, _drive_to_processing_attempt,
    _enable_p0a, _resume, _start, api_clients as api_clients,
)


def _pause_and_drain(clients, command):
    assert clients.account.post(f"/sessions/{SESSION_ID}/pause").status_code == 200
    drained = clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/{command['command_key']}/drain-ack",
        headers=clients.device_headers)
    assert drained.status_code == 200, drained.text
    return drained.json()["state_revision"]


def _unavailable(db):
    projection = provider_readiness.readiness_projection(db).model_copy(update={
        "status": "required_capability_failed", "required_capabilities_ready": False})
    raise provider_readiness.ProviderReadinessConflict(projection)


@pytest.mark.parametrize("kind", ["skip_item", "confirmed_correct"])
@pytest.mark.parametrize("last_item", [False, True])
def test_outage_saves_once_and_waits_for_explicit_continuation(
        api_clients, monkeypatch, kind, last_item):
    _enable_p0a(monkeypatch)
    _bind_api_bank(api_clients, monkeypatch, FIRST_ONLY_BANK if last_item else TWO_ONLY_BANK)
    if kind == "confirmed_correct":
        _drive_to_processing_attempt(api_clients)
        monkeypatch.setattr(asr, "get_engine", lambda: _EmptyTranscriptAsr())
        _run_p0a_attempt_worker(SESSION_ID)
    else:
        assert _start(api_clients).status_code == 200
    command = _device_next(api_clients)
    assert command is not None
    revision = _pause_and_drain(api_clients, command)
    reason = "asr_misrecognized" if kind == "confirmed_correct" else "other"
    with Session(api_clients.engine) as db:
        original_commands = [row.model_dump() for row in db.exec(select(RuntimeCommand))]
        original_attempts = [row.model_dump() for row in db.exec(select(AttemptEvent))]
        original_controls = [row.model_dump() for row in db.exec(select(AutopilotControlEvent))]
    scheduled_before = list(api_clients.scheduled_attempts)
    real_readiness = provider_readiness.require_resume_ready
    monkeypatch.setattr(provider_readiness, "require_resume_ready", _unavailable)
    saved = _adjudicate(
        api_clients, key="adjudicate-outage-0001", expected_revision=revision,
        kind=kind, reason_code=reason, note="现场已核对")
    assert saved.status_code == 200, saved.text
    receipt = saved.json()
    assert receipt["status"] == "paused" and receipt["state_revision"] == revision + 1
    assert receipt["last_error_code"] == (
        "autopilot_scope_completed" if last_item else "autopilot_adjudication_saved")
    assert api_clients.scheduled_attempts == scheduled_before
    with Session(api_clients.engine) as db:
        assert [row.model_dump() for row in db.exec(select(RuntimeCommand))] == original_commands
        assert [row.model_dump() for row in db.exec(select(AttemptEvent))] == original_attempts
        assert [row.model_dump() for row in db.exec(select(AutopilotControlEvent))] == original_controls
        row = db.exec(select(AutopilotPositionAdjudication)).one()
        assert row.kind == ("confirmed_correct" if kind == "confirmed_correct" else "skipped")
        runtime_before_replay = db.get(SessionRuntimeState, SESSION_ID).model_dump()
        assert runtime_before_replay["status"] == "paused"
        if kind == "confirmed_correct":
            turn = db.exec(select(TurnEvent)).one()
            assert (turn.ai_answer_type, turn.ai_score, turn.reviewed_score, turn.score_locked) == (
                "沉默", 0.0, None, False)
        else:
            assert list(db.exec(select(TurnEvent))) == []

    # A recovered provider cannot turn a retried save into permission to run AI.
    monkeypatch.setattr(provider_readiness, "require_resume_ready", real_readiness)
    replay = _adjudicate(
        api_clients, key="adjudicate-outage-0001", expected_revision=revision,
        kind=kind, reason_code=reason, note="现场已核对")
    assert replay.status_code == 200, replay.text
    assert replay.json() == receipt
    with Session(api_clients.engine) as db:
        assert db.get(SessionRuntimeState, SESSION_ID).model_dump() == runtime_before_replay
        assert len(list(db.exec(select(AutopilotPositionAdjudication)))) == 1
        assert len(list(db.exec(select(AuditLog).where(
            AuditLog.action == "autopilot_position_adjudicated")))) == 1
        assert [row.model_dump() for row in db.exec(select(RuntimeCommand))] == original_commands
    again = _adjudicate(
        api_clients, key="adjudicate-outage-0002", expected_revision=receipt["state_revision"],
        kind=kind, reason_code=reason)
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["code"] == (
        "autopilot_scope_completed" if last_item else "autopilot_adjudication_already_recorded")
    resumed = _resume(api_clients, key="resume-saved-decision-0001",
                      expected_revision=receipt["state_revision"])
    if last_item:
        assert resumed.status_code == 409, resumed.text
        assert resumed.json()["detail"]["code"] == "autopilot_scope_completed"
    else:
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["status"] == "waiting_tts"
        assert resumed.json()["position_item_id"] == TWO_ONLY_BANK.single_element[1]["item_id"]


@pytest.mark.parametrize("blocker", ["recording", "withdrawal", "consent", "cloud"])
def test_outage_never_bypasses_governance_even_for_an_unanswered_skip(
        api_clients, monkeypatch, blocker):
    _enable_p0a(monkeypatch)
    _bind_api_bank(api_clients, monkeypatch, TWO_ONLY_BANK)
    assert _start(api_clients).status_code == 200
    command = _device_next(api_clients)
    revision = _pause_and_drain(api_clients, command)
    with Session(api_clients.engine) as db:
        patient = db.get(Patient, PATIENT_ID)
        if blocker == "recording":
            patient.recording_allowed = False
        elif blocker == "withdrawal":
            patient.withdrawal_status = "withdrawn"
        elif blocker == "consent":
            patient.consent_status = "拒绝"
        else:
            # This isolated fixture exercises the real-session cloud gate with
            # absent/revoked permission; it never calls an external provider.
            monkeypatch.setenv("ENABLE_AUTOPILOT_REAL_SESSIONS", "1")
            training = db.get(TrainSession, SESSION_ID)
            training.is_simulation = False
            training.data_classification = "research"
            db.add(training)
            patient.is_simulation_subject = False
            patient.cloud_processing_allowed = False
        db.add(patient)
        db.commit()
        state_before = db.get(SessionAutopilotState, SESSION_ID).model_dump()
        runtime_before = db.get(SessionRuntimeState, SESSION_ID).model_dump()
    monkeypatch.setattr(provider_readiness, "require_resume_ready", _unavailable)
    rejected = _adjudicate(
        api_clients, key="adjudicate-outage-gate-0001", expected_revision=revision,
        kind="skip_item", reason_code="other")
    assert rejected.status_code in (403, 409), rejected.text
    if blocker == "cloud":
        assert rejected.json()["detail"]["code"] == "autopilot_cloud_processing_required"
    with Session(api_clients.engine) as db:
        assert list(db.exec(select(AutopilotPositionAdjudication))) == []
        assert list(db.exec(select(TurnEvent))) == []
        assert db.get(SessionAutopilotState, SESSION_ID).model_dump() == state_before
        assert db.get(SessionRuntimeState, SESSION_ID).model_dump() == runtime_before
