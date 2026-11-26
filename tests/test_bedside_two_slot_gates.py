"""两床并行 3a/3b:平板读写闸与停麦/终态/撤回投影都按场次找自己的床旁槽。

NMU_BEDSIDE_SLOTS=2 时 1 号槽(锚行 id=1)放 A 场、2 号槽(id=2)放 B 场。握手选槽
(第 3e 步)还没接,所以 B 场先照今天握手进 1 号槽,再整行挪进 2 号槽,模拟选槽的
结果;A 场随后握手进空出来的 1 号槽。每条用例都逐字节比对另一槽,证明写只落本槽。
自动带练服务层(第 3c 步)的槽位读法还没改,自动带练路由在这里替换服务层,只验
main.py 这一层的闸和投影。
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, SQLModel

from app import (auth, autopilot_orchestration, autopilot_service, db,
                 device_capability, main as main_module, session_completion)
from app.enums import ConsentType, EventLine, PhaseType
from app.main import app
from app.models import (LiveState, Patient, ResearchUser, SessionCloseoutReport,
                        SessionRuntimeState)
from app.models import Session as TrainSession
from app.runtime import PlanItem, PlanTurn, SessionPlan


BANK = "wk2-v1-20260707"
SESSION_A, SESSION_B = "S-BED-A", "S-BED-B"
TRAINER_A, TRAINER_B = "BED-RES-A", "BED-RES-B"
ELSEWHERE = "S-BED-ELSEWHERE"
COMMAND_KEY = "cmd-bed-0001"
_SLOT_COLUMNS = (
    "session_json", "cursor_json", "rapport_json", "audio_json",
    "patient_rec_json", "patient_ack_session_id", "patient_current_screen",
    "patient_last_seen_at", "patient_ack_seq",
)


def _activate(client: TestClient, session_id: str) -> None:
    handshake = client.put("/live/state", json={"kind": "session", "payload": {
        "sessionId": session_id, "weekNo": 2, "eventLine": "正式训练",
        "mode": "task", "itemBankVersionId": BANK,
    }})
    assert handshake.status_code == 200, handshake.status_code
    plan = client.get(f"/sessions/{session_id}/plan").json()
    cursor = client.put("/live/state", json={"kind": "cursor", "payload": {
        "sessionId": session_id, "screen": "present", "itemIdx": 0, "turnIdx": 0,
        "responseRole": plan["items"][0]["turns"][0]["response_role"],
        "cueLevel": 0, "recording": "idle",
    }})
    assert cursor.status_code == 200, cursor.status_code


def _capability(engine, session_id: str, device_id: str) -> dict[str, str]:
    with Session(engine) as s:
        token, _row = device_capability.create_capability(
            s, session_id=session_id, device_id=device_id)
        s.commit()
    return {"X-Device-Capability": token}


def _register(client: TestClient, headers: dict, session_id: str, raw_id: str):
    return client.post("/audio", headers=headers, json={
        "raw_audio_id": raw_id, "session_id": session_id, "turn_key": "itm-0001#1",
    })


def _move_anchor_to_slot_two(engine) -> None:
    with Session(engine) as s:
        anchor = s.get(LiveState, 1)
        s.add(LiveState(**{**anchor.model_dump(), "id": 2}))
        for name in _SLOT_COLUMNS:
            setattr(anchor, name, None)
        s.add(anchor)
        s.commit()


def _login(engine, username: str, display_id: str, role: str) -> TestClient:
    with Session(engine) as s:
        s.add(ResearchUser(
            username=username, display_id=display_id,
            password_hash=auth.hash_password("password1"), role=role,
            created_at=datetime.now(),
        ))
        s.commit()
    client = TestClient(app)
    login = client.post("/auth/login", json={
        "username": username, "password": "password1"})
    assert login.status_code == 200, login.status_code
    client.headers["X-CSRF-Token"] = client.cookies.get(auth.CSRF_COOKIE_NAME)
    return client


def _two_beds(monkeypatch, tmp_path, slots: str | None):
    if slots is None:
        monkeypatch.delenv("NMU_BEDSIDE_SLOTS", raising=False)
    else:
        monkeypatch.setenv("NMU_BEDSIDE_SLOTS", slots)
    engine = db.make_engine(f"sqlite:///{tmp_path / 'two-beds.sqlite'}")
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    client = TestClient(app)
    for session_id, patient_id, trainer in (
            (SESSION_A, "P-BED-A", TRAINER_A), (SESSION_B, "P-BED-B", TRAINER_B)):
        assert client.post("/patients", json={
            "patient_id": patient_id, "consent_status": "已同意",
            "consent_type": "本人同意", "mandarin_eligible": True,
            "recording_allowed": True, "is_simulation_subject": True,
        }).status_code == 200
        assert client.post("/sessions", json={
            "session_id": session_id, "patient_id": patient_id, "week_no": 2,
            "phase_type": "正式训练", "event_line": "正式训练",
            "item_bank_version_id": BANK, "is_simulation": True,
            "trainer_id": trainer,
        }).status_code == 200

    _activate(client, SESSION_B)
    tablet_b = _capability(engine, SESSION_B, "bed-tablet-b-00000001")
    assert _register(client, tablet_b, SESSION_B, "bed-b-registered").status_code == 200
    _move_anchor_to_slot_two(engine)
    _activate(client, SESSION_A)
    tablet_a = _capability(engine, SESSION_A, "bed-tablet-a-00000001")
    assert _register(client, tablet_a, SESSION_A, "bed-a-registered").status_code == 200

    beds = SimpleNamespace(
        engine=engine, client=client, tablet_a=tablet_a, tablet_b=tablet_b,
        researcher_a=_login(engine, "bed-res-a", TRAINER_A, "researcher"),
        researcher_b=_login(engine, "bed-res-b", TRAINER_B, "researcher"),
        admin=_login(engine, "bed-admin", "BED-ADMIN", "admin"),
    )
    return beds


def _close(beds) -> None:
    for client in (beds.client, beds.researcher_a, beds.researcher_b, beds.admin):
        client.close()
    beds.engine.dispose()


@pytest.fixture
def beds(monkeypatch, tmp_path):
    value = _two_beds(monkeypatch, tmp_path, "2")
    yield value
    _close(value)


@pytest.fixture
def single_slot_beds(monkeypatch, tmp_path):
    value = _two_beds(monkeypatch, tmp_path, None)
    yield value
    _close(value)


def _raw_slot(engine, slot_id: int):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM livestate WHERE id = :id"), {"id": slot_id}).one_or_none()
    return None if row is None else tuple(row)


def _slot_payloads(engine, slot_id: int) -> tuple[dict, dict | None, LiveState]:
    with Session(engine) as s:
        row = s.get(LiveState, slot_id)
        s.expunge(row)
    cursor = json.loads(row.cursor_json) if row.cursor_json else None
    return json.loads(row.session_json), cursor, row


def _set_slot_session(engine, slot_id: int, session_id: str) -> None:
    with Session(engine) as s:
        row = s.get(LiveState, slot_id)
        payload = json.loads(row.session_json)
        payload["sessionId"] = session_id
        row.session_json = json.dumps(payload, ensure_ascii=False)
        s.add(row)
        s.commit()


def _code(response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return detail.get("code")
    return body.get("code") if isinstance(body, dict) else None


def _ack_body(ack_type: str, key: str) -> dict:
    body = {
        "idempotency_key": key, "ack_type": ack_type,
        "control_generation": 1, "runner_generation": 1,
        "command_revision": 1, "device_event_seq": 1,
    }
    if ack_type == "record_failed":
        body["error_code"] = "microphone_unavailable"
    elif ack_type == "record_started":
        body["mime_type"] = "audio/webm"
    elif ack_type == "tts_ended":
        body["media_ended"] = True
    return body


def _stub_autopilot_service(monkeypatch, *, ack_status: str = "waiting_recording") -> list:
    calls: list[str] = []

    def authorize_recording(_s, *, session_id, **_kwargs):
        calls.append(f"recording:{session_id}")
        return True

    def authorize_barge_in(_s, *, session_id, **_kwargs):
        calls.append(f"barge_in:{session_id}")
        return True

    def apply_ack(_s, *, session_id, ack, **_kwargs):
        calls.append(f"ack:{session_id}")
        return autopilot_service.ApplyDeviceAckResult(
            ack_idempotency_key=ack.idempotency_key, ack_type=ack.ack_type,
            replayed=False, command_state="started", command_revision=2,
            status=ack_status, state_revision=3,
        )

    monkeypatch.setattr(autopilot_service, "authorize_recording_command", authorize_recording)
    monkeypatch.setattr(autopilot_service, "authorize_barge_in_monitor", authorize_barge_in)
    monkeypatch.setattr(autopilot_service, "apply_device_ack", apply_ack)
    return calls


def _one_turn_plan() -> SessionPlan:
    return SessionPlan(
        item_bank_version_id=BANK, week_no=2, event_line="正式训练",
        items=(PlanItem(
            item_id="SE_锚", task_type="单要素", image_id=None,
            presentation_order=3, turns=(PlanTurn(1, "命名"),)),),
    )


def _ready_assessments(monkeypatch) -> None:
    plan = _one_turn_plan()
    monkeypatch.setattr(main_module, "_assess_intervention_completion", lambda _sess, _s: (
        plan, session_completion.InterventionCompletionAssessment(
            expected_turns=1, matched_turns=1, completed_attempt_turns=1,
            audio_evidenced_turns=1, issues=())))
    monkeypatch.setattr(main_module, "_assess_session_completion", lambda _sess, _s: (
        plan, session_completion.CompletionAssessment(
            expected_turns=1, matched_turns=1, locked_turns=1, issues=(),
            audio_evidenced_turns=1)))
    monkeypatch.setattr(
        main_module, "_ensure_session_outcome_summary", lambda *_args: None)


def _tablet_routes(beds, session_id: str, headers: dict, *, register_id: str,
                   upload_id: str) -> dict:
    client = beds.client
    command = f"/sessions/{session_id}/autopilot/commands/{COMMAND_KEY}"
    return {
        "live_state": lambda: client.get("/live/state", headers=headers),
        "heartbeat": lambda: client.post("/live/patient-heartbeat", headers=headers, json={
            "session_id": session_id, "screen": "present"}),
        "recording_authorization": lambda: client.post(
            f"/sessions/{session_id}/recording-authorization", headers=headers),
        "autopilot_recording_authorization": lambda: client.post(
            f"{command}/recording-authorization", headers=headers),
        "barge_in_authorization": lambda: client.post(
            f"{command}/barge-in-authorization", headers=headers, json={
                "command_revision": 1, "control_generation": 1, "runner_generation": 1}),
        "autopilot_ack": lambda: client.post(
            f"{command}/acks", headers=headers,
            json=_ack_body("record_started", f"ack-{register_id}-0001")),
        "audio_registration": lambda: _register(client, headers, session_id, register_id),
        "audio_blob": lambda: client.put(
            f"/audio/{upload_id}/blob",
            headers={**headers, "content-type": "audio/webm"},
            content=b"\x1a\x45\xdf\xa3two-bedside-audio"),
        "presentation": lambda: client.get(
            f"/sessions/{session_id}/patient-presentation", headers=headers),
        "current_asset": lambda: client.get(
            f"/sessions/{session_id}/patient-asset/current", headers=headers),
    }


# ---------------- 3a 平板读写闸 ----------------

@pytest.mark.parametrize(("tablet", "session_id", "own", "other", "upload_id"), [
    ("tablet_b", SESSION_B, 2, 1, "bed-b-registered"),
    ("tablet_a", SESSION_A, 1, 2, "bed-a-registered"),
])
def test_each_tablet_passes_every_gate_and_writes_only_its_own_slot(
        beds, monkeypatch, tablet, session_id, own, other, upload_id):
    calls = _stub_autopilot_service(monkeypatch)
    headers = getattr(beds, tablet)
    untouched = _raw_slot(beds.engine, other)
    routes = _tablet_routes(beds, session_id, headers,
                            register_id=f"{upload_id}-new", upload_id=upload_id)

    results = {name: call() for name, call in routes.items()}

    assert {name: r.status_code for name, r in results.items()} == {
        name: 200 for name in routes}, {name: _code(r) for name, r in results.items()}
    assert calls == [f"recording:{session_id}", f"barge_in:{session_id}",
                     f"ack:{session_id}"]
    assert _raw_slot(beds.engine, other) == untouched
    live = results["live_state"].json()
    assert live["session"]["sessionId"] == session_id
    assert live["cursor"]["sessionId"] == session_id
    _session, _cursor, row = _slot_payloads(beds.engine, own)
    assert row.patient_ack_session_id == session_id
    assert row.patient_current_screen == "present"


def test_displaced_slot_two_capability_gets_device_session_changed_everywhere(
        beds, monkeypatch):
    calls = _stub_autopilot_service(monkeypatch)
    _set_slot_session(beds.engine, 2, ELSEWHERE)
    before = (_raw_slot(beds.engine, 1), _raw_slot(beds.engine, 2))
    routes = _tablet_routes(beds, SESSION_B, beds.tablet_b,
                            register_id="bed-b-after-move", upload_id="bed-b-registered")

    results = {name: call() for name, call in routes.items()}

    assert {name: (r.status_code, _code(r)) for name, r in results.items()} == {
        name: (409, "device_session_changed") for name in routes}
    assert calls == []
    assert (_raw_slot(beds.engine, 1), _raw_slot(beds.engine, 2)) == before


def test_single_slot_never_reads_slot_two(single_slot_beds, monkeypatch):
    """开关不开时 2 号行在库里也不被读:B 平板处处 device_session_changed,两行原样。"""
    beds = single_slot_beds
    calls = _stub_autopilot_service(monkeypatch)
    before = (_raw_slot(beds.engine, 1), _raw_slot(beds.engine, 2))
    routes = _tablet_routes(beds, SESSION_B, beds.tablet_b,
                            register_id="bed-b-single", upload_id="bed-b-registered")

    results = {name: call() for name, call in routes.items()}

    assert {name: (r.status_code, _code(r)) for name, r in results.items()} == {
        name: (409, "device_session_changed") for name in routes}
    assert calls == []
    assert (_raw_slot(beds.engine, 1), _raw_slot(beds.engine, 2)) == before
    live_a = beds.client.get("/live/state", headers=beds.tablet_a)
    assert live_a.status_code == 200
    assert live_a.json()["session"]["sessionId"] == SESSION_A

    paused = beds.researcher_b.post(f"/sessions/{SESSION_B}/pause")
    assert paused.status_code == 200, _code(paused)
    assert (_raw_slot(beds.engine, 1), _raw_slot(beds.engine, 2)) == before


def test_one_session_in_two_slots_fails_closed_with_closed_code(beds):
    with Session(beds.engine) as s:
        one, two = s.get(LiveState, 1), s.get(LiveState, 2)
        for name in _SLOT_COLUMNS:
            setattr(one, name, getattr(two, name))
        s.add(one)
        s.commit()
    before = (_raw_slot(beds.engine, 1), _raw_slot(beds.engine, 2))

    responses = [
        beds.client.get("/live/state", headers=beds.tablet_b),
        beds.client.post("/live/patient-heartbeat", headers=beds.tablet_b, json={
            "session_id": SESSION_B, "screen": "present"}),
    ]

    for response in responses:
        assert response.status_code == 409
        assert set(response.json()["detail"]) == {"code", "message"}
        assert response.json()["detail"]["code"] == "bedside_slot_conflict"
        assert SESSION_B not in response.text and "P-BED-B" not in response.text
    assert (_raw_slot(beds.engine, 1), _raw_slot(beds.engine, 2)) == before


# ---------------- 3b 停麦、终态、撤回投影 ----------------

def _runtime_revision(engine, session_id: str) -> int:
    with Session(engine) as s:
        return s.get(SessionRuntimeState, session_id).revision


def _assert_stop_projection(engine, slot_id: int, session_id: str) -> None:
    session, cursor, _row = _slot_payloads(engine, slot_id)
    assert session["sessionId"] == session_id
    assert session["paused"] is True
    assert cursor["sessionId"] == session_id
    assert (cursor["screen"], cursor["recording"]) == ("paused", "stopped")


def _assert_thanks_projection(engine, slot_id: int, session_id: str, status: str) -> None:
    session, cursor, _row = _slot_payloads(engine, slot_id)
    assert session["sessionId"] == session_id
    assert (session["runtimeStatus"], session["paused"]) == (status, False)
    assert cursor["sessionId"] == session_id
    assert (cursor["screen"], cursor["recording"]) == ("thanks", "stopped")


@pytest.mark.parametrize(("researcher", "session_id", "own", "other"), [
    ("researcher_b", SESSION_B, 2, 1),
    ("researcher_a", SESSION_A, 1, 2),
])
def test_pause_stops_only_its_own_slot(beds, researcher, session_id, own, other):
    untouched = _raw_slot(beds.engine, other)

    paused = getattr(beds, researcher).post(f"/sessions/{session_id}/pause")

    assert paused.status_code == 200, _code(paused)
    _assert_stop_projection(beds.engine, own, session_id)
    assert _raw_slot(beds.engine, other) == untouched


def test_autopilot_record_failure_technical_pause_stops_only_its_own_slot(
        beds, monkeypatch):
    _stub_autopilot_service(monkeypatch, ack_status="paused")
    untouched = _raw_slot(beds.engine, 1)

    acked = beds.client.post(
        f"/sessions/{SESSION_B}/autopilot/commands/{COMMAND_KEY}/acks",
        headers=beds.tablet_b, json=_ack_body("record_failed", "ack-bed-b-failed-0001"))

    assert acked.status_code == 200, _code(acked)
    _assert_stop_projection(beds.engine, 2, SESSION_B)
    assert _raw_slot(beds.engine, 1) == untouched


def test_abort_projects_thanks_only_into_its_own_slot(beds):
    untouched = _raw_slot(beds.engine, 1)

    aborted = beds.researcher_b.post(f"/sessions/{SESSION_B}/abort", json={
        "reason_code": "participant_declined",
        "expected_revision": _runtime_revision(beds.engine, SESSION_B),
        "idempotency_key": "abort-bed-b-participant-0001",
    })

    assert aborted.status_code == 200, _code(aborted)
    _assert_thanks_projection(beds.engine, 2, SESSION_B, "aborted")
    assert _raw_slot(beds.engine, 1) == untouched


def test_finish_then_complete_project_only_into_their_own_slot(beds, monkeypatch):
    _ready_assessments(monkeypatch)
    untouched = _raw_slot(beds.engine, 1)

    finished = beds.researcher_b.post(f"/sessions/{SESSION_B}/finish-intervention")

    assert finished.status_code == 200, _code(finished)
    _assert_thanks_projection(beds.engine, 2, SESSION_B, "intervention_completed")
    assert _raw_slot(beds.engine, 1) == untouched

    with Session(beds.engine) as s:
        s.add(SessionCloseoutReport(
            session_id=SESSION_B, schema_version="test-closeout",
            status="no_additional_observation",
            last_idempotency_key="closeout-bed-b-0001",
            last_request_hash="0" * 64, created_by=TRAINER_B, updated_by=TRAINER_B,
        ))
        s.commit()
    completed = beds.researcher_b.post(f"/sessions/{SESSION_B}/complete")

    assert completed.status_code == 200, _code(completed)
    _assert_thanks_projection(beds.engine, 2, SESSION_B, "completed")
    assert _raw_slot(beds.engine, 1) == untouched


def test_autopilot_final_ack_auto_close_projects_only_into_its_own_slot(
        beds, monkeypatch):
    _stub_autopilot_service(monkeypatch, ack_status="scope_completed")
    _ready_assessments(monkeypatch)
    untouched = _raw_slot(beds.engine, 1)

    acked = beds.client.post(
        f"/sessions/{SESSION_B}/autopilot/commands/{COMMAND_KEY}/acks",
        headers=beds.tablet_b, json=_ack_body("tts_ended", "ack-bed-b-final-0001"))

    assert acked.status_code == 200, _code(acked)
    _assert_thanks_projection(beds.engine, 2, SESSION_B, "intervention_completed")
    assert _raw_slot(beds.engine, 1) == untouched


def test_p0a_worker_auto_close_projects_only_into_its_own_slot(beds, monkeypatch):
    _ready_assessments(monkeypatch)
    target = SimpleNamespace(raw_audio_id="bed-b-worker-audio")
    monkeypatch.setattr(autopilot_orchestration, "derive_worker_target",
                        lambda _db, *, session_id: target)
    monkeypatch.setattr(
        autopilot_orchestration, "derive_authoritative_attempt_input",
        lambda _db, *, session_id: main_module.AttemptProcessIn(
            item_id="SE_锚", turn_seq=1, response_role="命名",
            raw_audio_id=target.raw_audio_id, prompt_level=0))
    monkeypatch.setattr(main_module, "_process_attempt", lambda *_args, **_kwargs: {
        "status": "completed", "attempt": {"id": 7}})
    monkeypatch.setattr(autopilot_orchestration, "worker_target_still_current",
                        lambda _db, _target: True)
    monkeypatch.setattr(autopilot_service, "route_completed_attempt",
                        lambda _db, **_kwargs: SimpleNamespace(status="scope_completed"))
    failures: list[str] = []
    monkeypatch.setattr(main_module, "_fail_closed_p0a_attempt_worker",
                        lambda _sid, code, **_kwargs: failures.append(code))
    untouched = _raw_slot(beds.engine, 1)

    main_module._run_p0a_attempt_worker(SESSION_B)

    assert failures == []
    _assert_thanks_projection(beds.engine, 2, SESSION_B, "intervention_completed")
    assert _raw_slot(beds.engine, 1) == untouched


def test_withdrawal_stops_only_the_withdrawn_subjects_slot(beds):
    now = datetime.now()
    with Session(beds.engine) as s:
        s.add(Patient(
            patient_id="P-BED-REAL", is_simulation_subject=False,
            consent_status="active", consent_type=ConsentType.本人同意,
            mandarin_eligible=True, recording_allowed=True,
            cloud_processing_allowed=True, governance_revision=0,
        ))
        s.add(TrainSession(
            session_id="S-BED-REAL", patient_id="P-BED-REAL", week_no=2,
            phase_type=PhaseType.正式训练, event_line=EventLine.正式训练,
            item_bank_version_id=BANK, is_simulation=False,
            data_classification="research", trainer_id=TRAINER_B,
        ))
        s.add(SessionRuntimeState(
            session_id="S-BED-REAL", status="active", revision=2, updated_at=now))
        two = s.get(LiveState, 2)
        two.session_json = json.dumps({"sessionId": "S-BED-REAL", "paused": False})
        two.cursor_json = json.dumps({
            "sessionId": "S-BED-REAL", "itemIdx": 0, "turnIdx": 0,
            "screen": "record", "recording": "recording"})
        two.patient_rec_json = json.dumps({"active": True})
        two.updated_at = now - timedelta(seconds=5)
        s.add(two)
        s.commit()
    untouched = _raw_slot(beds.engine, 1)

    withdrawn = beds.admin.post("/patients/P-BED-REAL/withdrawal", json={
        "idempotency_key": "withdrawal-bed-real-0123456789abcdef",
        "expected_governance_revision": 0,
        "reason_code": "participant_request",
    })

    assert withdrawn.status_code == 200, _code(withdrawn)
    session, cursor, row = _slot_payloads(beds.engine, 2)
    assert (session["sessionId"], session["runtimeStatus"]) == ("S-BED-REAL", "aborted")
    assert (cursor["screen"], cursor["recording"]) == ("thanks", "stopped")
    assert row.patient_rec_json is None
    assert _raw_slot(beds.engine, 1) == untouched
