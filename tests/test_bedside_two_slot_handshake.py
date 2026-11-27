"""两床并行 3e/3f/3g:握手选槽、自动场切回、照护员、配对、准入闸、收麦真值与页面构建号。

NMU_BEDSIDE_SLOTS=2 时两个账号各自握手,第二个账号落进 2 号槽(id=2);两槽都新鲜时
第三个账号拿 409 bedside_slots_full。配对只认受试者专属码。每条用例都逐字节比对另一槽,
证明写只落本槽;开关不设的孪生用例证明单槽行为照旧。
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, select

from app import (auth, db, device_capability, governance_lock, main as main_module,
                 patient_pairing, visit_plan_service)
from app.main import app
from app.models import (AuditLog, LiveState, Patient, PatientDeviceCapability,
                        ResearchUser, SessionRuntimeState)
from app.models import Session as TrainSession
from test_autopilot_activation import _activate as _activate_autopilot, _yield_a
from test_autopilot_api import OTHER_SESSION_ID, SESSION_ID, api_clients as api_clients
from test_bedside_two_slot_gates import _SLOT_COLUMNS, _code, _raw_slot


BANK = "wk2-v1-20260707"
PIN = "24681024"
TA, TB, TC, TD = "HS-RES-A", "HS-RES-B", "HS-STAFF-C", "HS-CARE-D"
S_A, S_B, S_B2, S_AB, S_C, S_D, S_W = (
    "S-HS-A", "S-HS-B", "S-HS-B2", "S-HS-AB", "S-HS-C", "S-HS-D", "S-HS-W")
P_A, P_B, P_C, P_D, P_W = "P-HS-A", "P-HS-B", "P-HS-C", "P-HS-D", "P-HS-W"
_SESSIONS = (
    (S_A, P_A, TA, 2), (S_B, P_B, TB, 2), (S_B2, P_B, TB, 2), (S_AB, P_B, TA, 2),
    (S_C, P_C, TC, 2), (S_D, P_D, TD, 2), (S_W, P_W, TB, 1),
)
_IDENTIFIERS = (S_A, S_B, S_C, P_A, P_B, P_C, TA, TB, TC)
DEPLOYED_PAGE_BUILD = "4205954952" + "1" * 67


def _login(engine, username: str, display_id: str, role: str) -> TestClient:
    with Session(engine) as s:
        s.add(ResearchUser(
            username=username, display_id=display_id,
            password_hash=auth.hash_password("password1"), role=role,
            created_at=datetime.now()))
        s.commit()
    client = TestClient(app)
    login = client.post("/auth/login", json={"username": username, "password": "password1"})
    assert login.status_code == 200, login.status_code
    client.headers["X-CSRF-Token"] = client.cookies.get(auth.CSRF_COOKIE_NAME)
    return client


def _seed(seeder: TestClient, sessions) -> None:
    for patient_id in sorted({patient_id for _sid, patient_id, _trainer, _week in sessions}):
        assert seeder.post("/patients", json={
            "patient_id": patient_id, "consent_status": "已同意",
            "consent_type": "本人同意", "mandarin_eligible": True,
            "recording_allowed": True, "is_simulation_subject": True,
        }).status_code == 200
    for session_id, patient_id, trainer, week in sessions:
        phase, event = (("关系建立", "关系建立环节") if week == 1
                        else ("正式训练", "正式训练"))
        assert seeder.post("/sessions", json={
            "session_id": session_id, "patient_id": patient_id, "week_no": week,
            "phase_type": phase, "event_line": event,
            "item_bank_version_id": BANK, "is_simulation": True,
            "trainer_id": trainer,
        }).status_code == 200


def _open_beds(monkeypatch, tmp_path, slots: str | None, staff_role: str):
    if slots is None:
        monkeypatch.delenv("NMU_BEDSIDE_SLOTS", raising=False)
    else:
        monkeypatch.setenv("NMU_BEDSIDE_SLOTS", slots)
    monkeypatch.setenv("ENABLE_AUTOPILOT_P0A_SIMULATION", "1")
    engine = db.make_engine(f"sqlite:///{tmp_path / 'two-bed-handshake.sqlite'}")
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with TestClient(app) as seeder:
        _seed(seeder, _SESSIONS)
    monkeypatch.setenv("CONSOLE_PIN", PIN)
    return SimpleNamespace(
        engine=engine,
        a=_login(engine, "hs-a", TA, staff_role),
        b=_login(engine, "hs-b", TB, staff_role),
        c=_login(engine, "hs-c", TC, "admin"),
        d=_login(engine, "hs-d", TD, "caregiver_operator"),
        device=TestClient(app),
    )


def _close(beds) -> None:
    for client in (beds.a, beds.b, beds.c, beds.d, beds.device):
        client.close()
    beds.engine.dispose()


@pytest.fixture
def beds(monkeypatch, tmp_path, request):
    value = _open_beds(monkeypatch, tmp_path, "2", getattr(request, "param", "researcher"))
    yield value
    _close(value)


@pytest.fixture
def single_slot_beds(monkeypatch, tmp_path):
    value = _open_beds(monkeypatch, tmp_path, None, "researcher")
    yield value
    _close(value)


def _claim(client: TestClient, session_id: str, week: int = 2):
    return client.put("/live/state", json={"kind": "session", "payload": {
        "sessionId": session_id, "weekNo": week,
        "eventLine": "关系建立环节" if week == 1 else "正式训练",
        "mode": "rapport" if week == 1 else "task", "itemBankVersionId": BANK,
    }})


def _cursor(client: TestClient, session_id: str, item_idx: int = 0):
    plan = client.get(f"/sessions/{session_id}/plan").json()
    return {
        "sessionId": session_id, "screen": "present", "itemIdx": item_idx, "turnIdx": 0,
        "responseRole": plan["items"][item_idx]["turns"][0]["response_role"],
        "cueLevel": 0, "recording": "idle",
    }


def _push_cursor(client: TestClient, session_id: str, item_idx: int = 0):
    response = client.put("/live/state", json={
        "kind": "cursor", "payload": _cursor(client, session_id, item_idx)})
    assert response.status_code == 200, response.text
    return response.json()


def _rapport_step(client: TestClient, session_id: str, question: int = 1):
    response = client.put("/live/state", json={"kind": "rapportStep", "payload": {
        "sessionId": session_id, "sectionKey": "介绍机构环境", "questionIdx": question,
        "beat": "ask", "recording": "idle", "containsDirectIdentifier": False,
    }})
    assert response.status_code == 200, response.text
    return response.json()


def _holder(engine, slot_id: int) -> str | None:
    with Session(engine) as s:
        row = s.get(LiveState, slot_id)
        payload = json.loads(row.session_json) if row is not None and row.session_json else {}
    return payload.get("sessionId")


def _slot(engine, slot_id: int) -> LiveState:
    with Session(engine) as s:
        row = s.get(LiveState, slot_id)
        s.expunge(row)
    return row


def _capability(engine, session_id: str, device_id: str) -> dict[str, str]:
    with Session(engine) as s:
        token, _row = device_capability.create_capability(
            s, session_id=session_id, device_id=device_id)
        s.commit()
    return {"X-Device-Capability": token}


def _capability_rows(engine, session_id: str) -> list[PatientDeviceCapability]:
    with Session(engine) as s:
        return list(s.exec(select(PatientDeviceCapability).where(
            PatientDeviceCapability.session_id == session_id)))


def _audit_actions(engine) -> list[str]:
    with Session(engine) as s:
        return [row.action for row in s.exec(select(AuditLog).order_by(AuditLog.id))]


def _snapshot(engine) -> dict:
    with Session(engine) as s:
        return {
            model.__name__: [row.model_dump(mode="json") for row in s.exec(select(model))]
            for model in (LiveState, SessionRuntimeState, AuditLog, PatientDeviceCapability)
        }


def _two_beds_claimed(beds) -> None:
    assert _claim(beds.a, S_A).status_code == 200
    _push_cursor(beds.a, S_A)
    claimed = _claim(beds.b, S_B)
    assert claimed.status_code == 200, claimed.text
    _push_cursor(beds.b, S_B)


# ---------------- 3e 握手 ----------------

@pytest.mark.parametrize("beds", ["researcher", "admin"], indirect=True)
def test_second_account_takes_slot_two_and_leaves_slot_one_alone(beds):
    assert _claim(beds.a, S_A).status_code == 200
    for item_idx in (0, 1, 2):
        _push_cursor(beds.a, S_A, item_idx)
    tablet_a = _capability(beds.engine, S_A, "hs-tablet-a-000000001")
    slot_one = _raw_slot(beds.engine, 1)
    slot_one_seq = _slot(beds.engine, 1).seq
    audits = _audit_actions(beds.engine)

    claimed = _claim(beds.b, S_B)
    assert claimed.status_code == 200, claimed.text
    # 换持有者时 seq 取所有槽的最大值再加 1,平板换床后不会把新槽的帧当旧帧跳过。
    assert claimed.json()["seq"] == slot_one_seq + 1
    assert (_holder(beds.engine, 1), _holder(beds.engine, 2)) == (S_A, S_B)
    assert _raw_slot(beds.engine, 1) == slot_one
    assert _audit_actions(beds.engine) == audits
    [capability] = _capability_rows(beds.engine, S_A)
    assert (capability.active_session_key, capability.recovery_only_at) == (S_A, None)
    assert beds.device.get("/live/state", headers=tablet_a).json()["session"]["sessionId"] == S_A

    # 握手之后的游标和老人端麦克风回报也只落 2 号槽。
    _push_cursor(beds.b, S_B)
    tablet_b = _capability(beds.engine, S_B, "hs-tablet-b-000000001")
    rec = beds.device.put("/live/state", headers=tablet_b, json={
        "kind": "patientRec", "payload": {
            "sessionId": S_B, "active": True, "turnKey": "itm-0001#1"}})
    assert rec.status_code == 200, rec.text
    assert json.loads(_slot(beds.engine, 2).patient_rec_json)["active"] is True
    assert _raw_slot(beds.engine, 1) == slot_one


@pytest.mark.parametrize("beds", ["researcher", "admin"], indirect=True)
def test_third_account_gets_slots_full_without_side_effects(beds):
    _two_beds_claimed(beds)
    before = _snapshot(beds.engine)
    denied = _claim(beds.c, S_C)
    assert denied.status_code == 409, denied.text
    assert _code(denied) == "bedside_slots_full"
    assert not any(identifier in denied.text for identifier in _IDENTIFIERS)
    assert _snapshot(beds.engine) == before


@pytest.mark.parametrize("beds", ["researcher", "admin"], indirect=True)
def test_same_patient_new_session_returns_to_its_slot(beds):
    _two_beds_claimed(beds)
    tablet_b = _capability(beds.engine, S_B, "hs-tablet-b-000000002")
    slot_one = _raw_slot(beds.engine, 1)
    moved = _claim(beds.b, S_B2)
    assert moved.status_code == 200, moved.text
    assert (_holder(beds.engine, 1), _holder(beds.engine, 2)) == (S_A, S_B2)
    assert _raw_slot(beds.engine, 1) == slot_one
    # 只降级被挤掉的那一场。
    assert _capability_rows(beds.engine, S_B)[0].recovery_only_at is not None
    assert beds.device.get("/live/state", headers=tablet_b).status_code in (401, 403, 409)

    # 另一个账号给同一位受试者开的场也只能落这一槽,永不同时占两槽。
    before = _raw_slot(beds.engine, 2)
    other = _claim(beds.a, S_AB)
    holders = (_holder(beds.engine, 1), _holder(beds.engine, 2))
    if other.status_code == 200:
        assert holders == (S_A, S_AB)
    else:
        assert other.status_code == 404, other.text
        assert holders == (S_A, S_B2) and _raw_slot(beds.engine, 2) == before
    assert _raw_slot(beds.engine, 1) == slot_one


def test_two_one_two_shadow_does_not_pause_the_healthy_session(beds, monkeypatch):
    _two_beds_claimed(beds)
    # 2→1:S-B 在 2 号槽时切回单槽;1 号槽那一场已中止,S-B 重新握手接管 1 号槽。
    monkeypatch.setenv("NMU_BEDSIDE_SLOTS", "1")
    with Session(beds.engine) as s:
        aborted = s.get(SessionRuntimeState, S_A)
        aborted.status = "aborted"
        s.add(aborted)
        s.commit()
    assert _claim(beds.b, S_B).status_code == 200
    assert _holder(beds.engine, 1) == S_B
    assert _holder(beds.engine, 2) is None
    tablet_b = _capability(beds.engine, S_B, "hs-tablet-b-000000003")
    with Session(beds.engine) as s:
        aged = s.get(LiveState, 2)
        aged.updated_at = datetime.now() - timedelta(hours=1)
        s.add(aged)
        s.commit()

    # 1→2:第三场来开场,落进空出来的 2 号槽,不碰 1 号槽里健康的 S-B。
    monkeypatch.setenv("NMU_BEDSIDE_SLOTS", "2")
    slot_one = _raw_slot(beds.engine, 1)
    claimed = _claim(beds.c, S_C)
    assert claimed.status_code == 200, claimed.text
    assert (_holder(beds.engine, 1), _holder(beds.engine, 2)) == (S_B, S_C)
    assert _raw_slot(beds.engine, 1) == slot_one
    with Session(beds.engine) as s:
        runtime = s.get(SessionRuntimeState, S_B)
    assert runtime is None or runtime.status == "active"
    assert "bedside_slot_stale_takeover" not in _audit_actions(beds.engine)
    assert beds.device.get("/live/state", headers=tablet_b).json()["session"]["sessionId"] == S_B


def test_single_slot_handshake_is_unchanged(single_slot_beds):
    beds = single_slot_beds
    assert _claim(beds.a, S_A).status_code == 200
    slot_one = _raw_slot(beds.engine, 1)
    assert _claim(beds.b, S_B).status_code == 404
    assert _raw_slot(beds.engine, 1) == slot_one
    assert _raw_slot(beds.engine, 2) is None
    # 管理员照今天可以接管新鲜槽(留监督审计),不会多出 2 号槽。
    taken = _claim(beds.c, S_C)
    assert taken.status_code == 200, taken.text
    assert _holder(beds.engine, 1) == S_C and _raw_slot(beds.engine, 2) is None
    assert "session_operator_admin_supervision" in _audit_actions(beds.engine)


def test_loopback_handshake_also_chooses_a_slot(monkeypatch, tmp_path):
    """回环 M0 没有账号、场次也没有 trainer:第二场照样落进 2 号槽,不覆盖第一场。"""
    monkeypatch.setenv("NMU_BEDSIDE_SLOTS", "2")
    engine = db.make_engine(f"sqlite:///{tmp_path / 'two-bed-loopback.sqlite'}")
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with TestClient(app) as client:
        _seed(client, ((S_A, P_A, None, 2), (S_B, P_B, None, 2)))
        assert _claim(client, S_A).status_code == 200
        slot_one = _raw_slot(engine, 1)
        assert _claim(client, S_B).status_code == 200
        assert (_holder(engine, 1), _holder(engine, 2)) == (S_A, S_B)
        assert _raw_slot(engine, 1) == slot_one
    engine.dispose()


def test_caregiver_activation_and_status_use_their_own_slot(beds):
    assert _claim(beds.a, S_A).status_code == 200
    slot_one = _raw_slot(beds.engine, 1)
    activated = beds.d.put(f"/caregiver/sessions/{S_D}/activation")
    assert activated.status_code == 200, activated.text
    assert activated.json()["idempotent"] is False
    assert _holder(beds.engine, 2) == S_D
    repeated = beds.d.put(f"/caregiver/sessions/{S_D}/activation")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["idempotent"] is True
    assert repeated.json()["live_seq"] == activated.json()["live_seq"]

    tablet_d = _capability(beds.engine, S_D, "hs-tablet-d-000000001")
    beat = beds.device.post("/live/patient-heartbeat", headers=tablet_d, json={
        "session_id": S_D, "screen": "waiting"})
    assert beat.status_code == 200, beat.text
    status = beds.d.get(f"/caregiver/sessions/{S_D}/status")
    assert status.status_code == 200, status.text
    assert status.json()["active_bedside_session"] is True
    assert status.json()["patient_presence"]["online"] is True
    assert _raw_slot(beds.engine, 1) == slot_one


# ---------------- 3e 自动场切回(沿用 P0a 夹具:两场同一受试者、同一账号) ----------------

@pytest.fixture
def one_slot_while_pairing(monkeypatch):
    """api_clients 用全局 PIN 配对,只在单槽下成立。排在 api_clients 前面,先摘掉机器上
    导出的开关;用例要两槽时自己再设。"""
    monkeypatch.delenv("NMU_BEDSIDE_SLOTS", raising=False)


def _third_session(engine, *, other_patient: bool) -> str:
    third_id = "S-ACTIVATION-THIRD"
    with Session(engine) as s:
        third = s.get(TrainSession, OTHER_SESSION_ID).model_dump()
        third.update(session_id=third_id, trainer_id="OTHER-RESEARCHER")
        if other_patient:
            s.add(Patient(patient_id="P-ACT-THIRD", is_simulation_subject=True,
                          consent_status="已同意", recording_allowed=True))
            third["patient_id"] = "P-ACT-THIRD"
        s.add(TrainSession(**third))
        s.commit()
        s.add(SessionRuntimeState(session_id=third_id, status="paused" if not other_patient
                                  else "active", updated_at=datetime.now()))
        s.commit()
    return third_id


@pytest.mark.parametrize("change", ["holder", "slot"])
def test_activation_prelock_slot_change_is_bedside_busy_with_two_slots(
        one_slot_while_pairing, api_clients, monkeypatch, change):
    monkeypatch.setenv("NMU_BEDSIDE_SLOTS", "2")
    revision, _ = _yield_a(api_clients, monkeypatch)
    assert api_clients.account.post(f"/sessions/{OTHER_SESSION_ID}/pause").status_code == 200
    third_id = _third_session(api_clients.engine, other_patient=False)
    actual_fence = governance_lock.begin_sqlite_write_fence

    def change_bedside_before_fence(db_session):
        with Session(api_clients.engine) as writer:
            anchor = writer.get(LiveState, 1)
            if change == "holder":
                payload = json.loads(anchor.session_json)
                payload.update(sessionId=third_id, paused=True)
                anchor.session_json = json.dumps(payload)
            else:
                # 同一位上一场挪到了 2 号槽:持有者没变,槽变了,预授权同样作废。
                writer.add(LiveState(**{**anchor.model_dump(), "id": 2}))
                anchor.session_json = None
            writer.add(anchor)
            writer.commit()
        actual_fence(db_session)

    monkeypatch.setattr(governance_lock, "begin_sqlite_write_fence", change_bedside_before_fence)
    response = _activate_autopilot(api_clients, revision)
    assert response.status_code == 409, response.text
    assert _code(response) == "autopilot_activation_bedside_busy"
    with Session(api_clients.engine) as s:
        holders = [json.loads(row.session_json or "{}").get("sessionId")
                   for row in s.exec(select(LiveState))]
        assert SESSION_ID not in holders
        assert s.get(SessionRuntimeState, SESSION_ID).status == "paused"


def test_activation_returns_into_a_free_slot_beside_another_bed(
        one_slot_while_pairing, api_clients, monkeypatch):
    monkeypatch.setenv("NMU_BEDSIDE_SLOTS", "2")
    revision, _ = _yield_a(api_clients, monkeypatch)
    assert api_clients.account.post(f"/sessions/{OTHER_SESSION_ID}/pause").status_code == 200
    third_id = _third_session(api_clients.engine, other_patient=True)
    with Session(api_clients.engine) as s:
        anchor = s.get(LiveState, 1)
        payload = json.loads(anchor.session_json)
        payload.update(sessionId=third_id, paused=False)
        anchor.session_json = json.dumps(payload)
        anchor.updated_at = datetime.now()
        s.add(anchor)
        s.commit()
    slot_one = _raw_slot(api_clients.engine, 1)
    activated = _activate_autopilot(api_clients, revision)
    assert activated.status_code == 200, activated.text
    assert _raw_slot(api_clients.engine, 1) == slot_one
    with Session(api_clients.engine) as s:
        slot_two = s.get(LiveState, 2)
        session_payload = json.loads(slot_two.session_json)
        assert (session_payload["sessionId"], session_payload["paused"]) == (SESSION_ID, True)
        assert slot_two.seq == s.get(LiveState, 1).seq + 1
        assert s.get(SessionRuntimeState, third_id).status == "active"


def test_single_slot_activation_clears_a_shadow_left_in_slot_two(
        one_slot_while_pairing, api_clients, monkeypatch):
    """单槽下切回本场时,2 号行里还写着本场(2→1 前留下的)就清掉,别等再开两槽时成违例。"""
    revision, _ = _yield_a(api_clients, monkeypatch)
    assert api_clients.account.post(f"/sessions/{OTHER_SESSION_ID}/pause").status_code == 200
    with Session(api_clients.engine) as s:
        shadow = LiveState(**{**s.get(LiveState, 1).model_dump(), "id": 2})
        payload = json.loads(shadow.session_json)
        payload["sessionId"] = SESSION_ID
        shadow.session_json = json.dumps(payload)
        s.add(shadow)
        s.commit()
    activated = _activate_autopilot(api_clients, revision)
    assert activated.status_code == 200, activated.text
    assert (_holder(api_clients.engine, 1), _holder(api_clients.engine, 2)) == (SESSION_ID, None)


def test_activation_releases_the_same_patients_ended_session_in_the_other_slot(
        one_slot_while_pairing, api_clients, monkeypatch):
    monkeypatch.setenv("NMU_BEDSIDE_SLOTS", "2")
    revision, _ = _yield_a(api_clients, monkeypatch)
    ended = _third_session(api_clients.engine, other_patient=False)
    with Session(api_clients.engine) as s:
        for session_id in (OTHER_SESSION_ID, ended):
            runtime = s.get(SessionRuntimeState, session_id)
            runtime.status = "aborted"
            s.add(runtime)
        anchor = s.get(LiveState, 1)
        payload = json.loads(anchor.session_json)
        payload["sessionId"] = ended
        s.add(LiveState(id=2, seq=anchor.seq, session_json=json.dumps(payload),
                        updated_at=datetime.now()))
        s.commit()
    ended_tablet = _capability(api_clients.engine, ended, "hs-tablet-ended-000001")

    activated = _activate_autopilot(api_clients, revision)

    assert activated.status_code == 200, activated.text
    assert (_holder(api_clients.engine, 1), _holder(api_clients.engine, 2)) == (SESSION_ID, None)
    [capability] = _capability_rows(api_clients.engine, ended)
    assert (capability.active_session_key, capability.recovery_only_at is None) == (None, False)
    stale_tablet = api_clients.device.get("/live/state", headers=ended_tablet)
    assert (stale_tablet.status_code, _code(stale_tablet)) == (
        401, "device_capability_recovery_only")


# ---------------- 3f 配对 ----------------

def _pair(beds, code: str, device_id: str):
    return beds.device.post("/device/pair", headers={"X-Console-Pin": code},
                            json={"deviceId": device_id})


def test_global_pin_is_refused_with_two_slots(beds):
    assert _claim(beds.a, S_A).status_code == 200
    before = _snapshot(beds.engine)
    refused = _pair(beds, PIN, "hs-global-pin-0000001")
    assert refused.status_code == 409, refused.text
    assert _code(refused) == "device_pair_patient_code_required"
    assert not any(identifier in refused.text for identifier in _IDENTIFIERS)
    assert _snapshot(beds.engine) == before


def test_global_pin_still_pairs_with_one_slot(single_slot_beds):
    beds = single_slot_beds
    assert _claim(beds.a, S_A).status_code == 200
    paired = _pair(beds, PIN, "hs-global-pin-0000002")
    assert paired.status_code == 200, paired.text
    assert set(paired.json()) == {"capability", "sessionId", "expiresAt"}
    assert paired.json()["sessionId"] == S_A


def test_patient_codes_pair_and_attach_each_tablet_to_its_own_slot(beds):
    assert _claim(beds.a, S_A).status_code == 200
    early = _pair(beds, patient_pairing.derive_patient_pin(P_B), "hs-tablet-b-000000009")
    assert early.status_code == 200, early.text
    assert set(early.json()) == {"binding"}
    claimed = _claim(beds.b, S_B)
    assert claimed.status_code == 200, claimed.text

    paired_a = _pair(beds, patient_pairing.derive_patient_pin(P_A), "hs-tablet-a-000000009")
    assert paired_a.status_code == 200, paired_a.text
    assert set(paired_a.json()) == {"binding", "capability", "sessionId", "expiresAt"}
    assert paired_a.json()["sessionId"] == S_A
    attached_b = beds.device.post("/device/attach", json={
        "deviceId": "hs-tablet-b-000000009", "binding": early.json()["binding"]})
    assert attached_b.status_code == 200, attached_b.text
    assert set(attached_b.json()) == {"capability", "sessionId", "expiresAt"}
    assert attached_b.json()["sessionId"] == S_B
    for receipt, session_id in ((paired_a.json(), S_A), (attached_b.json(), S_B)):
        live = beds.device.get(
            "/live/state", headers={"X-Device-Capability": receipt["capability"]})
        assert live.status_code == 200, live.text
        assert live.json()["session"]["sessionId"] == session_id


def _set_status(engine, session_id: str, status: str) -> None:
    with Session(engine) as s:
        runtime = s.get(SessionRuntimeState, session_id)
        runtime.status = status
        s.add(runtime)
        s.commit()


def _abort(client: TestClient, engine, session_id: str, key: str) -> None:
    with Session(engine) as s:
        revision = s.get(SessionRuntimeState, session_id).revision
    aborted = client.post(f"/sessions/{session_id}/abort", json={
        "reason_code": "technical_failure", "expected_revision": revision,
        "idempotency_key": key})
    assert aborted.status_code == 200, aborted.text


def _orphan_audio_saved(raw_audio_id: str, session_id: str) -> dict:
    return {"kind": "audioSaved", "payload": {
        "rawAudioId": raw_audio_id, "durationSeconds": 2.5, "byteCount": 4321,
        "checksum": "ab" * 32, "turnKey": "itm-0001#1", "sessionId": session_id,
        "containsDirectIdentifier": False}}


@pytest.mark.parametrize("slots", ["2", None])
def test_new_session_releases_the_same_patients_ended_session_in_the_other_slot(
        monkeypatch, tmp_path, slots):
    """S-B 中止后同一位受试者的 S-B2 落进 1 号槽:2 号槽里的 S-B 要像单床被接管时一样放掉,
    旧平板拿 401 去重新配对,没传完的录音拿 410。开关不设的孪生走同一流程,行为本来如此。"""
    beds = _open_beds(monkeypatch, tmp_path, slots, "researcher")
    try:
        if slots == "2":
            _two_beds_claimed(beds)
        else:
            assert _claim(beds.b, S_B).status_code == 200
            _push_cursor(beds.b, S_B)
        code = patient_pairing.derive_patient_pin(P_B)
        paired = _pair(beds, code, "hs-tablet-b-000000020")
        assert paired.status_code == 200, paired.text
        assert paired.json()["sessionId"] == S_B
        old = {"X-Device-Capability": paired.json()["capability"]}
        registered = beds.device.post("/audio", headers=old, json={
            "raw_audio_id": "raw-hs-orphan-000001", "session_id": S_B,
            "turn_key": "itm-0001#1", "contains_direct_identifier": False})
        assert registered.status_code == 200, registered.text
        if slots == "2":
            _abort(beds.a, beds.engine, S_A, "hs-abort-a-0000000001")
        _abort(beds.b, beds.engine, S_B, "hs-abort-b-0000000001")

        claimed = _claim(beds.b, S_B2)

        assert claimed.status_code == 200, claimed.text
        assert (_holder(beds.engine, 1), _holder(beds.engine, 2)) == (S_B2, None)
        assert all(row.recovery_only_at is not None and row.active_session_key is None
                   for row in _capability_rows(beds.engine, S_B))
        stale_tablet = beds.device.get("/live/state", headers=old)
        assert (stale_tablet.status_code, _code(stale_tablet)) == (
            401, "device_capability_recovery_only")
        repaired = _pair(beds, code, "hs-tablet-b-000000020")
        assert repaired.status_code == 200, repaired.text
        assert repaired.json()["sessionId"] == S_B2
        new = {"X-Device-Capability": repaired.json()["capability"]}
        for headers in (new, old):
            orphan = beds.device.put(
                "/live/state", headers=headers,
                json=_orphan_audio_saved("raw-hs-orphan-000001", S_B))
            assert (orphan.status_code, _code(orphan)) == (410, "audio_terminal_disposition")
    finally:
        _close(beds)


def test_release_leaves_another_patients_ended_session_alone(beds):
    """只放同一位受试者的终态场:别人的终态场留在 2 号槽,它的平板照今天读到结束画面。"""
    _two_beds_claimed(beds)
    tablet_b = _capability(beds.engine, S_B, "hs-tablet-b-000000021")
    _abort(beds.b, beds.engine, S_B, "hs-abort-b-0000000002")
    _abort(beds.a, beds.engine, S_A, "hs-abort-a-0000000002")
    slot_two = _raw_slot(beds.engine, 2)

    claimed = _claim(beds.c, S_C)

    assert claimed.status_code == 200, claimed.text
    assert (_holder(beds.engine, 1), _holder(beds.engine, 2)) == (S_C, S_B)
    assert _raw_slot(beds.engine, 2) == slot_two
    [capability] = _capability_rows(beds.engine, S_B)
    assert (capability.active_session_key, capability.recovery_only_at) == (S_B, None)
    assert beds.device.get("/live/state", headers=tablet_b).status_code == 200


def test_finished_session_left_in_another_slot_does_not_block_pairing_or_console(beds):
    """同一受试者上一场的终态份还留在另一槽(单床时开了新场、再开两床才会留下):配对和
    console-state 只认没终态的那一场。"""
    _two_beds_claimed(beds)
    _set_status(beds.engine, S_A, "aborted")
    _set_status(beds.engine, S_B, "aborted")
    leftover = _slot(beds.engine, 2)
    claimed = _claim(beds.b, S_B2)
    assert claimed.status_code == 200, claimed.text
    # 两床下换场会把它放掉(上一条用例);这里原样放回去。
    assert (_holder(beds.engine, 1), _holder(beds.engine, 2)) == (S_B2, None)
    with Session(beds.engine) as s:
        two = s.get(LiveState, 2)
        for column in _SLOT_COLUMNS:
            setattr(two, column, getattr(leftover, column))
        s.add(two)
        s.commit()
    assert (_holder(beds.engine, 1), _holder(beds.engine, 2)) == (S_B2, S_B)

    def check(device_id: str) -> None:
        paired = _pair(beds, patient_pairing.derive_patient_pin(P_B), device_id)
        assert paired.status_code == 200, paired.text
        assert set(paired.json()) == {"binding", "capability", "sessionId", "expiresAt"}
        assert paired.json()["sessionId"] == S_B2
        state = beds.b.get("/live/console-state")
        assert state.status_code == 200, state.text
        assert state.json()["session"]["sessionId"] == S_B2

    check("hs-tablet-b-000000010")
    # 反过来放:终态份在 1 号槽、没终态的那一场在 2 号槽。
    with Session(beds.engine) as s:
        one, two = s.get(LiveState, 1), s.get(LiveState, 2)
        for column in ("session_json", "cursor_json", "rapport_json", "audio_json",
                       "patient_rec_json", "patient_ack_session_id"):
            first, second = getattr(one, column), getattr(two, column)
            setattr(one, column, second)
            setattr(two, column, first)
        s.add_all([one, two])
        s.commit()
    assert (_holder(beds.engine, 1), _holder(beds.engine, 2)) == (S_B, S_B2)
    check("hs-tablet-b-000000011")


def test_foreign_binding_attach_is_byte_identical_to_no_session(beds, capsys):
    paired = _pair(beds, patient_pairing.derive_patient_pin(P_C), "hs-tablet-c-000000009")
    assert set(paired.json()) == {"binding"}
    body = {"deviceId": "hs-tablet-c-000000009", "binding": paired.json()["binding"]}
    capsys.readouterr()
    nothing_live = beds.device.post("/device/attach", json=body)
    nothing_log = capsys.readouterr().out

    _two_beds_claimed(beds)
    capsys.readouterr()
    others_live = beds.device.post("/device/attach", json=body)
    others_log = capsys.readouterr().out
    assert nothing_live.status_code == others_live.status_code == 409
    assert others_live.content == nothing_live.content
    assert others_log == nothing_log == "[ops] code=device_attach_refused_no_session\n"


# ---------------- 3g 准入闸、收麦真值、页面构建号、console-state ----------------

def test_admission_gates_find_each_session_in_its_own_slot(beds):
    assert _claim(beds.a, S_A).status_code == 200
    assert _claim(beds.b, S_B).status_code == 200
    with Session(beds.engine) as s:
        # 只握了手、还没有 runtime 行的场,靠它自己那一槽是否新鲜来拦新工作。
        assert s.get(SessionRuntimeState, S_B) is None
        for patient_id, actor_id in ((P_A, TA), (P_B, TB)):
            with pytest.raises(visit_plan_service.VisitPlanError) as patient_open:
                visit_plan_service.assert_patient_ready_for_new_work(s, patient_id)
            assert patient_open.value.code == "visit_plan_patient_session_open"
            with pytest.raises(visit_plan_service.VisitPlanError) as actor_open:
                visit_plan_service.assert_actor_ready_for_new_work(s, actor_id)
            assert actor_open.value.code == "visit_plan_actor_session_open"
        visit_plan_service.assert_patient_ready_for_new_work(s, P_C)
        visit_plan_service.assert_actor_ready_for_new_work(s, TC)


def test_rapport_completion_reads_recording_truth_from_its_own_slot(beds):
    assert _claim(beds.a, S_A).status_code == 200
    assert _claim(beds.b, S_W, week=1).status_code == 200
    _rapport_step(beds.b, S_W)
    assert _holder(beds.engine, 2) == S_W
    with Session(beds.engine) as s:
        _plan, idle = main_module._assess_rapport_completion(s.get(TrainSession, S_W), s)
    assert idle.recording_idle is True
    with Session(beds.engine) as s:
        slot_two = s.get(LiveState, 2)
        slot_two.patient_rec_json = json.dumps({"sessionId": S_W, "active": True})
        s.add(slot_two)
        s.commit()
    with Session(beds.engine) as s:
        _plan, hot = main_module._assess_rapport_completion(s.get(TrainSession, S_W), s)
    assert hot.recording_idle is False


def test_two_tablets_alternating_heartbeats_audit_each_build_once(beds, monkeypatch):
    monkeypatch.setattr(main_module, "_PATIENT_PAGE_BUILD", None)
    monkeypatch.setattr(main_module, "_PATIENT_PAGE_BUILD_REPORTED", set())
    monkeypatch.setattr(main_module, "_deployed_page_build_id", lambda: DEPLOYED_PAGE_BUILD)
    _two_beds_claimed(beds)
    tablets = {S_A: _capability(beds.engine, S_A, "hs-tablet-a-000000004"),
               S_B: _capability(beds.engine, S_B, "hs-tablet-b-000000004")}
    for beat in range(20):
        session_id = S_A if beat % 2 == 0 else S_B
        response = beds.device.post("/live/patient-heartbeat", headers=tablets[session_id],
                                    json={"session_id": session_id, "screen": "waiting",
                                          "page_build_id": DEPLOYED_PAGE_BUILD})
        assert response.status_code == 200, response.text
        assert response.json()["patientPresence"]["page_build"] == "current"
    with Session(beds.engine) as s:
        audits = list(s.exec(select(AuditLog).where(AuditLog.action == "patient_page_build")))
    assert sorted(row.session_id for row in audits) == [S_A, S_B]
    for client in (beds.a, beds.b):
        presence = client.get("/live/console-state").json()["patientPresence"]
        assert presence["page_build"] == "current"


def test_page_build_memory_is_bounded_per_session(monkeypatch):
    monkeypatch.setenv("NMU_BEDSIDE_SLOTS", "2")
    monkeypatch.setattr(main_module, "_PATIENT_PAGE_BUILD", None)
    monkeypatch.setattr(main_module, "_PATIENT_PAGE_BUILD_REPORTED", set())
    limit = main_module._PATIENT_PAGE_BUILD_MAX
    for index in range(limit + 2):
        assert main_module._remember_patient_page_build(f"S-PB-{index}", DEPLOYED_PAGE_BUILD)
    kept = [f"S-PB-{index}" for index in range(2, limit + 2)]
    assert list(main_module._PATIENT_PAGE_BUILD) == kept
    assert {key[0] for key in main_module._PATIENT_PAGE_BUILD_REPORTED} == set(kept)
    # 还在表里的场次同一构建号不再记;被挤掉的场次回来算第一次。
    assert not main_module._remember_patient_page_build(kept[-1], DEPLOYED_PAGE_BUILD)
    assert main_module._remember_patient_page_build("S-PB-0", DEPLOYED_PAGE_BUILD)


def test_single_slot_page_build_memory_keeps_one_session(monkeypatch):
    """单床照今天只记一场:换场清空已报集合,A→B→A 每一拍换场都算第一次。"""
    monkeypatch.delenv("NMU_BEDSIDE_SLOTS", raising=False)
    monkeypatch.setattr(main_module, "_PATIENT_PAGE_BUILD", None)
    monkeypatch.setattr(main_module, "_PATIENT_PAGE_BUILD_REPORTED", set())
    remembered = [main_module._remember_patient_page_build(session_id, DEPLOYED_PAGE_BUILD)
                  for session_id in ("S-PB-A", "S-PB-B", "S-PB-A", "S-PB-B", "S-PB-A")]
    assert remembered == [True] * 5
    assert not main_module._remember_patient_page_build("S-PB-A", DEPLOYED_PAGE_BUILD)
    assert main_module._PATIENT_PAGE_BUILD == {"S-PB-A": DEPLOYED_PAGE_BUILD}
    assert main_module._PATIENT_PAGE_BUILD_REPORTED == {("S-PB-A", DEPLOYED_PAGE_BUILD)}


def test_console_state_defaults_to_the_callers_own_slot(beds):
    _two_beds_claimed(beds)
    for client, session_id in ((beds.a, S_A), (beds.b, S_B)):
        state = client.get("/live/console-state")
        assert state.status_code == 200, state.text
        assert state.json()["session"]["sessionId"] == session_id
        explicit = client.get("/live/console-state", params={"session_id": session_id})
        assert explicit.json() == state.json()
    assert beds.c.get("/live/console-state").json()["session"]["sessionId"] == S_A
    supervised = beds.c.get("/live/console-state", params={"session_id": S_B})
    assert supervised.json()["session"]["sessionId"] == S_B
    # 别人的场次不论在不在床旁、根本不存在,回的都是同一个 404:不能借回落到 1 号槽
    # (正好是自己那一场)探出别人的场次在不在某一床。
    probes = [beds.a.get("/live/console-state", params={"session_id": session_id})
              for session_id in (S_B, S_C, "S-HS-MISSING")]
    assert [probe.status_code for probe in probes] == [404, 404, 404]
    assert len({probe.content for probe in probes}) == 1


def test_console_state_single_slot_is_unchanged(single_slot_beds):
    beds = single_slot_beds
    assert _claim(beds.a, S_A).status_code == 200
    assert beds.a.get("/live/console-state").json()["session"]["sessionId"] == S_A
    assert beds.b.get("/live/console-state").status_code == 404
    assert beds.b.get("/live/console-state", params={"session_id": S_B}).status_code == 404


# ---------------- 计划没点名的几处:持久游标、恢复、训练前互动、播放回执 ----------------

def test_runtime_cursor_pause_and_resume_project_into_their_own_slot(beds):
    _two_beds_claimed(beds)
    slot_one = _raw_slot(beds.engine, 1)
    seq = _slot(beds.engine, 2).seq
    payload = _cursor(beds.b, S_B, 1)
    payload.pop("sessionId")
    moved = beds.b.put(f"/sessions/{S_B}/runtime/cursor", json=payload)
    assert moved.status_code == 200, moved.text
    slot_two = _slot(beds.engine, 2)
    assert (json.loads(slot_two.cursor_json)["itemIdx"], slot_two.seq) == (1, seq + 1)

    assert beds.b.post(f"/sessions/{S_B}/pause").status_code == 200
    assert json.loads(_slot(beds.engine, 2).session_json)["paused"] is True
    resumed = beds.b.post(f"/sessions/{S_B}/resume")
    assert resumed.status_code == 200, resumed.text
    slot_two = _slot(beds.engine, 2)
    assert json.loads(slot_two.session_json)["paused"] is False
    assert json.loads(slot_two.cursor_json)["screen"] == "present"
    assert _raw_slot(beds.engine, 1) == slot_one


def test_warmup_and_rapport_playback_use_their_own_slot(beds):
    assert _claim(beds.a, S_A).status_code == 200
    _push_cursor(beds.a, S_A)
    assert _claim(beds.b, S_W, week=1).status_code == 200
    step = _rapport_step(beds.b, S_W)
    slot_one = _raw_slot(beds.engine, 1)

    tablet_w = _capability(beds.engine, S_W, "hs-tablet-w-000000001")
    receipt = {"sectionKey": "介绍机构环境", "questionIdx": 1, "beat": "ask",
               "utteranceId": None, "wseq": step["wseq"], "outcome": "played"}
    played = beds.device.put(f"/sessions/{S_W}/rapport/playback", headers=tablet_w,
                             json=receipt)
    assert played.status_code == 200, played.text
    checked = beds.b.get(f"/sessions/{S_W}/rapport/playback")
    assert checked.status_code == 200, checked.text
    assert checked.json() == {"receipt": receipt}

    with Session(beds.engine) as s:
        revision = s.get(SessionRuntimeState, S_W).revision
    started = beds.b.post(f"/sessions/{S_W}/warmup", json={
        "action": "start", "topic": "weather", "expectedWseq": step["wseq"],
        "expectedRevision": revision, "idempotencyKey": "hs-warmup-start-0001"})
    assert started.status_code == 200, started.text
    assert json.loads(_slot(beds.engine, 2).rapport_json)["warmup"]["topic"] == "weather"
    assert _raw_slot(beds.engine, 1) == slot_one
