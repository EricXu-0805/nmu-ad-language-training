"""Explicit stopped-session activation never steals a running bedside slot."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import threading

import pytest
from sqlmodel import Session, select

from app import asr, auth, evidence_ledger
from app.main import _run_p0a_attempt_worker
from app.models import (
    AttemptCaptureProcessing, AttemptEvent, AutopilotControlEvent, LiveState,
    Patient, PatientDeviceCapability, ResearchUser, RuntimeCommand,
    Session as TrainSession, SessionAutopilotState, SessionRuntimeState,
)
import test_autopilot_api as harness
from test_autopilot_api import (
    BANK, OTHER_SESSION_ID, PATIENT_ID, SESSION_ID, _WorkerAsr, _device_next,
    _drive_to_processing_attempt, _enable_p0a, _resume, _start,
    api_clients as api_clients,
)


def _activate(clients, revision):
    return clients.account.post(f"/sessions/{SESSION_ID}/autopilot/activate", json={
        "expected_revision": revision,
    })


def _handshake(clients, sid):
    return clients.account.put("/live/state", json={"kind": "session", "payload": {
        "sessionId": sid, "weekNo": 2, "eventLine": "正式训练", "mode": "task",
        "itemBankVersionId": BANK.version_id,
    }})


def _pair(clients, device_id):
    paired = clients.device.post("/device/pair", headers={"X-Console-Pin": "24681024"},
                                 json={"deviceId": device_id})
    assert paired.status_code == 200, paired.text
    return {"X-Device-Capability": paired.json()["capability"]}


def _pause_and_drain(clients, command):
    paused = clients.account.post(f"/sessions/{SESSION_ID}/pause")
    assert paused.status_code == 200, paused.text
    drained = clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/{command['command_key']}/drain-ack",
        headers=clients.device_headers)
    assert drained.status_code == 200, drained.text
    return drained.json()["state_revision"]


def _yield_a(clients, monkeypatch, *, recording=False, drain=True):
    _enable_p0a(monkeypatch)
    if recording:
        command = _drive_to_processing_attempt(clients)["record"]
    else:
        assert _start(clients).status_code == 200
        command = _device_next(clients)
    if drain:
        revision = _pause_and_drain(clients, command)
    else:
        assert clients.account.post(f"/sessions/{SESSION_ID}/pause").status_code == 200
        revision = clients.account.get(
            f"/sessions/{SESSION_ID}/autopilot/status").json()["state_revision"]
    assert _handshake(clients, OTHER_SESSION_ID).status_code == 200
    return revision, command


def _snapshot(clients):
    with Session(clients.engine) as db:
        return {
            model.__name__: [row.model_dump(mode="json") for row in db.exec(select(model))]
            for model in (LiveState, SessionRuntimeState, SessionAutopilotState,
                          RuntimeCommand, AutopilotControlEvent, PatientDeviceCapability,
                          AttemptCaptureProcessing, AttemptEvent)
        }


def test_yielded_automatic_session_can_activate_pair_and_resume_without_manual_takeover(
        api_clients, monkeypatch):
    revision, original = _yield_a(api_clients, monkeypatch)
    b_headers = _pair(api_clients, "activation-other-device-0001")
    assert api_clients.account.post(f"/sessions/{OTHER_SESSION_ID}/pause").status_code == 200
    # Ordinary stale console handshakes remain forbidden even on a stopped A.
    assert _handshake(api_clients, SESSION_ID).json()["detail"]["code"] == "autopilot_manual_control_locked"
    before = _snapshot(api_clients)
    activated = _activate(api_clients, revision)
    assert activated.status_code == 200, activated.text
    assert (activated.json()["mode"], activated.json()["status"]) == ("autonomous", "paused")
    assert activated.json()["state_revision"] == revision
    after = _snapshot(api_clients)
    for model in ("RuntimeCommand", "AutopilotControlEvent", "SessionAutopilotState",
                  "AttemptCaptureProcessing", "AttemptEvent"):
        assert after[model] == before[model]
    assert json.loads(after["LiveState"][0]["session_json"])["paused"] is True
    assert json.loads(after["LiveState"][0]["session_json"])["sessionId"] == SESSION_ID
    assert api_clients.device.get("/live/state", headers=b_headers).status_code in (401, 403, 409)
    # Exact repeat activation is read-only, including wseq/runtime revisions.
    assert _activate(api_clients, revision).status_code == 200
    assert _snapshot(api_clients) == after
    not_paired = _resume(api_clients, key="activation-before-pair-0001", expected_revision=revision)
    assert not_paired.status_code == 409
    assert not_paired.json()["detail"]["code"] == "autopilot_device_not_paired"
    api_clients.device_headers = _pair(api_clients, "activation-return-device-0001")
    resumed = _resume(api_clients, key="activation-after-pair-0001", expected_revision=revision)
    assert resumed.status_code == 200, resumed.text
    next_command = _device_next(api_clients)
    assert next_command["item_ref"] == original["item_ref"]
    assert next_command["control_generation"] == original["control_generation"] + 1


@pytest.mark.parametrize("blocker", ["active", "hot_mic", "unclaimed_processing", "undrained_tts"])
def test_activation_never_displaces_a_running_or_unsafe_bedside_session(api_clients, monkeypatch, blocker):
    revision, _ = _yield_a(api_clients, monkeypatch)
    if blocker == "hot_mic":
        assert api_clients.account.post(f"/sessions/{OTHER_SESSION_ID}/pause").status_code == 200
        with Session(api_clients.engine) as db:
            live = db.get(LiveState, 1)
            live.patient_rec_json = json.dumps({"sessionId": OTHER_SESSION_ID, "active": True})
            db.add(live)
            db.commit()
    elif blocker in {"unclaimed_processing", "undrained_tts"}:
        api_clients.device_headers = _pair(api_clients, "activation-other-device-0002")
        with monkeypatch.context() as patch:
            patch.setattr(harness, "SESSION_ID", OTHER_SESSION_ID)
            patch.setattr(harness, "START_KEY", "activation-other-start-0001")
            if blocker == "unclaimed_processing":
                harness._drive_to_processing_attempt(api_clients)
            else:
                assert harness._start(api_clients).status_code == 200
                assert api_clients.account.post(f"/sessions/{OTHER_SESSION_ID}/pause").status_code == 200
    before = _snapshot(api_clients)
    rejected = _activate(api_clients, revision)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] in {
        "autopilot_activation_bedside_busy", "autopilot_patient_microphone_active"}
    assert _snapshot(api_clients) == before


@pytest.mark.parametrize("blocker", ["undrained", "revision", "withdrawn", "recording_revoked", "foreign_target"])
def test_activation_keeps_target_safety_identity_and_consent_gates(api_clients, monkeypatch, blocker):
    revision, _ = _yield_a(api_clients, monkeypatch, drain=blocker != "undrained")
    assert api_clients.account.post(f"/sessions/{OTHER_SESSION_ID}/pause").status_code == 200
    with Session(api_clients.engine) as db:
        if blocker in {"withdrawn", "recording_revoked"}:
            patient = db.get(Patient, PATIENT_ID)
            if blocker == "withdrawn":
                patient.withdrawal_status = "withdrawn"
            else:
                patient.recording_allowed = False
            db.add(patient)
        elif blocker == "foreign_target":
            target = db.get(TrainSession, SESSION_ID)
            target.trainer_id = "OTHER-RESEARCHER"
            db.add(target)
        db.commit()
    before = _snapshot(api_clients)
    rejected = _activate(api_clients, revision + (blocker == "revision"))
    assert rejected.status_code in (403, 404, 409), rejected.text
    assert _snapshot(api_clients) == before


def test_activation_cannot_steal_a_foreign_paused_session(api_clients, monkeypatch):
    revision, _ = _yield_a(api_clients, monkeypatch)
    assert api_clients.account.post(f"/sessions/{OTHER_SESSION_ID}/pause").status_code == 200
    with Session(api_clients.engine) as db:
        other = db.get(TrainSession, OTHER_SESSION_ID)
        other.trainer_id = "OTHER-RESEARCHER"
        db.add(other)
        db.commit()
    before = _snapshot(api_clients)
    rejected = _activate(api_clients, revision)
    assert rejected.status_code == 404
    assert _snapshot(api_clients) == before


def test_activation_keeps_saved_recording_and_resumes_it_after_repair(api_clients, monkeypatch):
    revision, original = _yield_a(api_clients, monkeypatch, recording=True)
    assert api_clients.account.post(f"/sessions/{OTHER_SESSION_ID}/pause").status_code == 200
    assert _activate(api_clients, revision).status_code == 200
    api_clients.device_headers = _pair(api_clients, "activation-return-device-0003")
    resumed = _resume(api_clients, key="activation-saved-recording-0001", expected_revision=revision)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "processing_attempt"
    monkeypatch.setattr(asr, "get_engine", lambda: _WorkerAsr(text=BANK.single_element[0]["target_word"]))
    _run_p0a_attempt_worker(SESSION_ID)
    with Session(api_clients.engine) as db:
        attempt = db.exec(select(AttemptEvent)).one()
        assert attempt.raw_audio_id == original["payload"]["raw_audio_id"]
        assert attempt.processing_status == "completed"
        record = db.exec(select(RuntimeCommand).where(RuntimeCommand.kind == "record")).one()
        assert record.idempotency_key == original["command_key"]


@pytest.mark.parametrize("principal", ["device", "anonymous", "caregiver_operator", "data_steward"])
def test_only_named_research_operations_can_activate(api_clients, monkeypatch, principal):
    revision, _ = _yield_a(api_clients, monkeypatch)
    assert api_clients.account.post(f"/sessions/{OTHER_SESSION_ID}/pause").status_code == 200
    client = api_clients.account
    headers = {}
    if principal == "device":
        client, headers = api_clients.device, api_clients.device_headers
    elif principal == "anonymous":
        client = api_clients.anonymous
    else:
        with Session(api_clients.engine) as db:
            user = db.exec(select(ResearchUser)).one()
            user.role = principal
            db.add(user)
            db.commit()
        auth.reset_for_tests()
    before = _snapshot(api_clients)
    response = client.post(f"/sessions/{SESSION_ID}/autopilot/activate",
                           json={"expected_revision": revision}, headers=headers)
    assert response.status_code in (401, 403), response.text
    assert _snapshot(api_clients) == before


def test_target_capture_claim_cannot_be_hidden_by_a_valid_drain_receipt(api_clients, monkeypatch):
    revision, _ = _yield_a(api_clients, monkeypatch, recording=True)
    assert api_clients.account.post(f"/sessions/{OTHER_SESSION_ID}/pause").status_code == 200
    with Session(api_clients.engine) as db:
        capture = db.exec(select(AttemptCaptureProcessing)).one()
        assert evidence_ledger.try_claim_capture(db, capture.id, owner="late-worker-claim")
        db.commit()
    before = _snapshot(api_clients)
    response = _activate(api_clients, revision)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "autopilot_activation_processing"
    assert _snapshot(api_clients) == before


def test_current_bedside_processing_claim_blocks_activation_even_after_drain(api_clients, monkeypatch):
    revision, _ = _yield_a(api_clients, monkeypatch)
    api_clients.device_headers = _pair(api_clients, "activation-other-device-0004")
    with monkeypatch.context() as patch:
        patch.setattr(harness, "SESSION_ID", OTHER_SESSION_ID)
        patch.setattr(harness, "START_KEY", "activation-other-start-0004")
        record = harness._drive_to_processing_attempt(api_clients)["record"]
    assert api_clients.account.post(f"/sessions/{OTHER_SESSION_ID}/pause").status_code == 200
    drained = api_clients.device.post(
        f"/sessions/{OTHER_SESSION_ID}/autopilot/commands/{record['command_key']}/drain-ack",
        headers=api_clients.device_headers)
    assert drained.status_code == 200, drained.text
    with Session(api_clients.engine) as db:
        capture = db.exec(select(AttemptCaptureProcessing)).one()
        assert evidence_ledger.try_claim_capture(db, capture.id, owner="late-other-worker-claim")
        db.commit()
    before = _snapshot(api_clients)
    response = _activate(api_clients, revision)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "autopilot_activation_bedside_busy"
    assert _snapshot(api_clients) == before


def test_stopped_withdrawn_other_patient_can_yield_without_reopening_their_session(api_clients, monkeypatch):
    revision, _ = _yield_a(api_clients, monkeypatch)
    assert api_clients.account.post(f"/sessions/{OTHER_SESSION_ID}/pause").status_code == 200
    with Session(api_clients.engine) as db:
        db.add(Patient(patient_id="P-ACTIVATION-OTHER", is_simulation_subject=True,
                       consent_status="已撤回", recording_allowed=False, withdrawal_status="withdrawn"))
        db.commit()
        other = db.get(TrainSession, OTHER_SESSION_ID)
        other.patient_id = "P-ACTIVATION-OTHER"
        db.add(other)
        db.commit()
        before = db.get(SessionRuntimeState, OTHER_SESSION_ID).model_dump()
    activated = _activate(api_clients, revision)
    assert activated.status_code == 200, activated.text
    with Session(api_clients.engine) as db:
        assert db.get(SessionRuntimeState, OTHER_SESSION_ID).model_dump() == before
        assert db.get(Patient, "P-ACTIVATION-OTHER").recording_allowed is False
        assert json.loads(db.get(LiveState, 1).session_json)["sessionId"] == SESSION_ID


def test_activation_rereads_bedside_authority_after_independent_process_resume(api_clients, monkeypatch):
    """A different interpreter's real HTTP resume cannot be overwritten by stale reads."""
    from app import main

    revision, _ = _yield_a(api_clients, monkeypatch)
    assert api_clients.account.post(f"/sessions/{OTHER_SESSION_ID}/pause").status_code == 200
    child = r'''
import sys
from sqlalchemy import event
from fastapi.testclient import TestClient
from app import auth, db
from app.main import app

@event.listens_for(db.engine, 'after_cursor_execute')
def hold_resume_commit(conn, cursor, statement, parameters, context, executemany):
    if statement.startswith('UPDATE sessionruntimestate SET'):
        print('RESUME_WRITE_LOCKED', flush=True)
        assert sys.stdin.readline().strip() == 'commit'

with TestClient(app) as client:
    login = client.post('/auth/login', json={'username': 'p0a-researcher', 'password': 'password1'})
    assert login.status_code == 200, login.text
    client.headers['X-CSRF-Token'] = client.cookies.get(auth.CSRF_COOKIE_NAME)
    response = client.post('/sessions/' + sys.argv[1] + '/resume')
    print('RESUMED=' + str(response.status_code), flush=True)
    assert response.status_code == 200, response.text
'''
    env = {**os.environ, "DATABASE_URL": str(api_clients.engine.url)}
    process = subprocess.Popen(
        [sys.executable, "-c", child, OTHER_SESSION_ID], env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    read_previous = threading.Event()
    real_runtime = main._runtime_row_for_update

    def observe_read(sid, db):
        value = real_runtime(sid, db)
        if sid == OTHER_SESSION_ID:
            read_previous.set()
        return value

    monkeypatch.setattr(main, "_runtime_row_for_update", observe_read)
    try:
        assert process.stdout.readline().strip() == "RESUME_WRITE_LOCKED"
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(_activate, api_clients, revision)
            read_before_commit = read_previous.wait(timeout=.2)
            process.stdin.write("commit\n")
            process.stdin.flush()
            response = pending.result(timeout=10)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert "RESUMED=200" in stdout
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "autopilot_activation_bedside_busy"
        assert not read_before_commit, "authority was read before acquiring the SQLite writer fence"
        with Session(api_clients.engine) as db:
            assert db.get(SessionRuntimeState, OTHER_SESSION_ID).status == "active"
            assert json.loads(db.get(LiveState, 1).session_json)["sessionId"] == OTHER_SESSION_ID
            assert db.get(SessionRuntimeState, SESSION_ID).status == "paused"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
