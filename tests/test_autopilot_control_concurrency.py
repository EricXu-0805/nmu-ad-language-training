"""Separate SQLite processes must agree on the pause/start control boundary."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import audit, auth, governance_lock, main
from app.models import (
    AuditLog, AutopilotControlEvent, LiveState, ResearchUser, RuntimeCommand,
    Session as TrainSession, SessionAutopilotState, SessionRuntimeState,
)
from test_autopilot_activation import _yield_a
from test_autopilot_api import (
    OTHER_SESSION_ID, SESSION_ID, _device_next, _enable_p0a, _start,
    api_clients as api_clients,
)


_CHILD_CONTROL = r'''
import json
import sys
from sqlalchemy import event
from fastapi.testclient import TestClient
from app import auth, content, db
from app.main import app
from test_autopilot_api import FIRST_ONLY_BANK

content.load_item_bank = lambda _path: FIRST_ONLY_BANK
action, session_id, revision = sys.argv[1:]
held = False

@event.listens_for(db.engine, 'after_cursor_execute')
def hold_control_commit(conn, cursor, statement, parameters, context, executemany):
    global held
    first_write = ('INSERT INTO runtimecommand ' if action == 'start'
                   else 'UPDATE sessionruntimestate SET')
    if not held and statement.startswith(first_write):
        held = True
        print('CONTROL_WRITE_LOCKED', flush=True)
        assert sys.stdin.readline().strip() == 'commit'

with TestClient(app) as client:
    login = client.post('/auth/login', json={
        'username': 'p0a-researcher', 'password': 'password1'})
    assert login.status_code == 200, login.text
    client.headers['X-CSRF-Token'] = client.cookies.get(auth.CSRF_COOKIE_NAME)
    if action == 'pause':
        response = client.post('/sessions/' + session_id + '/pause')
    else:
        response = client.post('/sessions/' + session_id + '/autopilot/' + action,
            json={'idempotency_key': 'cross-process-' + action + '-0001',
                  'expected_revision': int(revision)})
    print(json.dumps({'status': response.status_code, 'body': response.json()}), flush=True)
    assert response.status_code == 200, response.text
'''


@pytest.mark.parametrize("first_action", ["pause", "start", "resume"])
def test_pause_and_automatic_control_reread_after_other_process_commits(
        api_clients, monkeypatch, first_action):
    """Pause cannot miss a new scope; start cannot inherit pre-pause authority."""
    _enable_p0a(monkeypatch)
    revision = 0
    if first_action == "resume":
        assert _start(api_clients).status_code == 200
        command = _device_next(api_clients)
        assert api_clients.account.post(f"/sessions/{SESSION_ID}/pause").status_code == 200
        drained = api_clients.device.post(
            f"/sessions/{SESSION_ID}/autopilot/commands/{command['command_key']}/drain-ack",
            headers=api_clients.device_headers)
        assert drained.status_code == 200, drained.text
        revision = drained.json()["state_revision"]

    process = subprocess.Popen(
        [sys.executable, "-c", _CHILD_CONTROL, first_action, SESSION_ID, str(revision)],
        env={**os.environ, "DATABASE_URL": str(api_clients.engine.url),
             "PYTHONPATH": os.pathsep.join([
                 str(Path(__file__).resolve().parents[1]),
                 str(Path(__file__).resolve().parent)])},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    read_authority = threading.Event()
    request_entered = threading.Event()
    real_live = main._live_row_for_update

    def observe_live(db):
        result = real_live(db)
        read_authority.set()
        return result

    monkeypatch.setattr(main, "_live_row_for_update", observe_live)

    def second_control():
        request_entered.set()
        if first_action == "pause":
            return _start(api_clients)
        return api_clients.account.post(f"/sessions/{SESSION_ID}/pause")

    try:
        with ThreadPoolExecutor(max_workers=1) as reader:
            line = reader.submit(process.stdout.readline).result(timeout=15)
        assert line.strip() == "CONTROL_WRITE_LOCKED"
        with ThreadPoolExecutor(max_workers=1) as requests:
            pending = requests.submit(second_control)
            assert request_entered.wait(timeout=5)
            stale_authority_read = read_authority.wait(timeout=.2)
            process.stdin.write("commit\n")
            process.stdin.flush()
            response = pending.result(timeout=15)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert json.loads(stdout.strip())["status"] == 200

        with Session(api_clients.engine) as db:
            runtime = db.get(SessionRuntimeState, SESSION_ID)
            state = db.get(SessionAutopilotState, SESSION_ID)
            live = json.loads(db.get(LiveState, 1).session_json)
            commands = list(db.exec(select(RuntimeCommand).order_by(RuntimeCommand.command_seq)))
            assert runtime.status == "paused"
            assert live["paused"] is True
            if first_action == "pause":
                assert response.status_code == 409, response.text
                assert state is None
                assert commands == []
            else:
                assert response.status_code == 200, response.text
                assert state.status == "paused"
                assert state.current_command_id is None
                latest = db.exec(select(AutopilotControlEvent).order_by(
                    AutopilotControlEvent.event_seq.desc())).first()
                assert latest.event_type == "pause"
                assert latest.command_id == commands[-1].id
        assert not stale_authority_read, "control authority was read before the SQLite writer fence"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("action", ["start", "pause", "start_blocked"])
def test_writer_fenced_admin_control_keeps_its_independent_authorization_audit(
        api_clients, monkeypatch, action, capsys):
    """Supervision is recorded before the writer lock, including a refused attempt."""
    _enable_p0a(monkeypatch)
    if action == "pause":
        assert _start(api_clients).status_code == 200
    elif action == "start_blocked":
        assert api_clients.account.post(f"/sessions/{SESSION_ID}/pause").status_code == 200
    with Session(api_clients.engine) as db:
        db.add(ResearchUser(
            username="control-admin", display_id="ADMIN-CONTROL",
            password_hash=auth.hash_password("password1"), role="admin"))
        db.commit()
    with TestClient(main.app) as admin:
        login = admin.post("/auth/login", json={
            "username": "control-admin", "password": "password1"})
        assert login.status_code == 200, login.text
        admin.headers["X-CSRF-Token"] = admin.cookies.get(auth.CSRF_COOKIE_NAME)
        if action == "pause":
            response = admin.post(f"/sessions/{SESSION_ID}/pause")
        else:
            response = admin.post(f"/sessions/{SESSION_ID}/autopilot/start", json={
                "idempotency_key": "admin-control-start-0001", "expected_revision": 0})
        assert response.status_code == (409 if action == "start_blocked" else 200), response.text
    with Session(api_clients.engine) as db:
        supervision = list(db.exec(select(AuditLog).where(
            AuditLog.action == "session_operator_admin_supervision")))
        assert len(supervision) == 1
        assert supervision[0].actor == "ADMIN-CONTROL"
        assert supervision[0].session_id == SESSION_ID
        expected_action = "暂停场次" if action == "pause" else "启动自动驾驶"
        assert f"supervised_action={expected_action}" in supervision[0].summary
        assert audit.verify_chain(db)["ok"] is True
        if action == "start_blocked":
            assert db.get(SessionAutopilotState, SESSION_ID) is None
            assert list(db.exec(select(RuntimeCommand))) == []
    assert "audit_append_failed" not in capsys.readouterr().out


@pytest.mark.parametrize("bedside_busy", [False, True])
def test_admin_activation_audits_both_authorized_sessions_before_the_writer_fence(
        api_clients, monkeypatch, capsys, bedside_busy):
    revision, _ = _yield_a(api_clients, monkeypatch)
    if not bedside_busy:
        assert api_clients.account.post(f"/sessions/{OTHER_SESSION_ID}/pause").status_code == 200
    with Session(api_clients.engine) as db:
        db.add(ResearchUser(
            username="activation-admin", display_id="ADMIN-ACTIVATION",
            password_hash=auth.hash_password("password1"), role="admin"))
        db.commit()
    with TestClient(main.app) as admin:
        assert admin.post("/auth/login", json={
            "username": "activation-admin", "password": "password1"}).status_code == 200
        admin.headers["X-CSRF-Token"] = admin.cookies.get(auth.CSRF_COOKIE_NAME)
        response = admin.post(f"/sessions/{SESSION_ID}/autopilot/activate", json={
            "expected_revision": revision})
        assert response.status_code == (409 if bedside_busy else 200), response.text
    with Session(api_clients.engine) as db:
        supervision = list(db.exec(select(AuditLog).where(
            AuditLog.action == "session_operator_admin_supervision")))
        assert len(supervision) == 2
        assert {row.session_id for row in supervision} == {SESSION_ID, OTHER_SESSION_ID}
        assert all(row.actor == "ADMIN-ACTIVATION" for row in supervision)
        assert audit.verify_chain(db)["ok"] is True
        assert json.loads(db.get(LiveState, 1).session_json)["sessionId"] == (
            OTHER_SESSION_ID if bedside_busy else SESSION_ID)
        assert db.get(SessionAutopilotState, SESSION_ID).status == "paused"
        assert db.get(SessionRuntimeState, SESSION_ID).status == "paused"
    assert "audit_append_failed" not in capsys.readouterr().out


def test_activation_cannot_apply_prelock_authorization_to_a_different_bedside_session(
        api_clients, monkeypatch):
    revision, _ = _yield_a(api_clients, monkeypatch)
    assert api_clients.account.post(f"/sessions/{OTHER_SESSION_ID}/pause").status_code == 200
    third_id = "S-ACTIVATION-THIRD"
    with Session(api_clients.engine) as db:
        third = db.get(TrainSession, OTHER_SESSION_ID).model_dump()
        third.update(session_id=third_id, trainer_id="OTHER-RESEARCHER")
        db.add(TrainSession(**third))
        db.commit()
        db.add(SessionRuntimeState(session_id=third_id, status="paused"))
        db.commit()
    actual_fence = governance_lock.begin_sqlite_write_fence

    def switch_slot_before_fence(db):
        # Another writer has changed the bedside since its authorization was
        # read. Use a separate connection; no patient data or provider calls.
        with Session(api_clients.engine) as writer:
            live = writer.get(LiveState, 1)
            payload = json.loads(live.session_json)
            payload.update(sessionId=third_id, paused=True)
            live.session_json = json.dumps(payload)
            writer.add(live)
            writer.commit()
        actual_fence(db)

    monkeypatch.setattr(governance_lock, "begin_sqlite_write_fence", switch_slot_before_fence)
    response = api_clients.account.post(f"/sessions/{SESSION_ID}/autopilot/activate", json={
        "expected_revision": revision})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "autopilot_activation_bedside_busy"
    with Session(api_clients.engine) as db:
        assert json.loads(db.get(LiveState, 1).session_json)["sessionId"] == third_id
        assert db.get(SessionAutopilotState, SESSION_ID).revision == revision
        assert db.get(SessionRuntimeState, SESSION_ID).status == "paused"
