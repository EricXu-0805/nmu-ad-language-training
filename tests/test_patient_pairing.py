"""按受试者配对码 + 绑定令牌 + /device/attach 自动跟场(零迁移)。"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import access_policy, auth, content, db, device_capability, patient_pairing
from app.main import app
from app.models import Patient, PatientDeviceCapability, ResearchUser


@pytest.fixture
def pairing_client(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", eng)
    # 配对/跟场测试不拥有自动化协议内容;种子场次不因无关的内容门禁
    # (2026-08-21 并发工线正给协议加 interaction_packages 登记)而红。
    monkeypatch.setattr(content, "validate_autopilot_protocol", lambda _p: [])
    SQLModel.metadata.create_all(eng)
    client = TestClient(app)
    client.test_engine = eng
    yield client
    client.close()


def _seed_two_sessions(client: TestClient) -> None:
    for suffix in ("ONE", "TWO"):
        assert client.post("/patients", json={
            "patient_id": f"P-{suffix}",
            "consent_status": "已同意",
            "consent_type": "本人同意",
            "mandarin_eligible": True,
            "recording_allowed": True,
            "is_simulation_subject": True,
            "secondary_use_allowed": True,
        }).status_code == 200
        assert client.post("/sessions", json={
            "session_id": f"S-{suffix}",
            "patient_id": f"P-{suffix}",
            "week_no": 2,
            "phase_type": "正式训练",
            "event_line": "正式训练",
            "item_bank_version_id": "wk2-v1-20260707",
            "is_simulation": True,
        }).status_code == 200
    _switch_live(client, "S-ONE")


def _switch_live(client: TestClient, session_id: str) -> None:
    response = client.put("/live/state", json={
        "kind": "session",
        "payload": {
            "sessionId": session_id,
            "weekNo": 2,
            "eventLine": "正式训练",
            "mode": "task",
            "itemBankVersionId": "wk2-v1-20260707",
        },
    })
    assert response.status_code == 200, response.text


def _login(client_engine, username: str, role: str) -> TestClient:
    with Session(client_engine) as session:
        session.add(ResearchUser(
            username=username,
            display_id=f"ACTOR-{username}",
            password_hash=auth.hash_password("password1"),
            role=role,
            created_at=datetime.now(),
        ))
        session.commit()
    client = TestClient(app)
    login = client.post("/auth/login", json={
        "username": username, "password": "password1",
    })
    assert login.status_code == 200, login.text
    client.headers["X-CSRF-Token"] = client.cookies.get(auth.CSRF_COOKIE_NAME)
    return client


# ---------------- 派生配对码(纯函数层) ----------------

def test_derived_pin_is_stable_six_digit_and_never_equals_console_pin(monkeypatch):
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    first = patient_pairing.derive_patient_pin("P-ONE")
    assert first == patient_pairing.derive_patient_pin("P-ONE")
    assert len(first) == 6 and first.isdigit()
    assert first != patient_pairing.derive_patient_pin("P-TWO")
    for pid in ("P-ONE", "P-TWO", "NMU-001", "A.b_c-9"):
        assert patient_pairing.derive_patient_pin(pid) != "24681024"


def test_pairing_fail_closed_without_console_pin(monkeypatch):
    monkeypatch.delenv("CONSOLE_PIN", raising=False)
    assert not patient_pairing.pairing_enabled()
    with pytest.raises(patient_pairing.PatientPairingUnavailable):
        patient_pairing.derive_patient_pin("P-ONE")
    with pytest.raises(patient_pairing.PatientPairingUnavailable):
        patient_pairing.issue_binding_token("P-ONE", "device-patient-000001")


def test_binding_token_round_trip_and_tamper_rejection(monkeypatch):
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    token = patient_pairing.issue_binding_token("P-ONE", "device-patient-000001")
    claims = patient_pairing.verify_binding_token(token)
    assert claims is not None
    assert claims.patient_id == "P-ONE"
    assert claims.device_id == "device-patient-000001"
    # 任意一个字符被改动即验签失败;换密钥(轮换 CONSOLE_PIN)同样全部作废。
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert patient_pairing.verify_binding_token(tampered) is None
    monkeypatch.setenv("CONSOLE_PIN", "13579124")
    assert patient_pairing.verify_binding_token(token) is None


# ---------------- 路由分类 ----------------

def test_attach_and_patch_routes_are_explicitly_classified():
    attach = access_policy.access_rule("POST", "/device/attach")
    assert attach.kind is access_policy.AccessKind.DEVICE_ATTACH
    patch = access_policy.access_rule("PATCH", "/patients/P-1")
    assert patch.kind is access_policy.AccessKind.ACCOUNT
    assert patch.roles == access_policy.TRAINING_OPERATION_ROLES
    # 既有的云授权专属规则不能被通用档案编辑规则吞掉。
    cloud = access_policy.access_rule("PATCH", "/patients/P-1/cloud-processing")
    assert cloud.label == "更新受试者云处理授权"


# ---------------- 配对与自动跟场(HTTP 全链) ----------------

def test_global_pin_pair_response_shape_unchanged(pairing_client, monkeypatch):
    _seed_two_sessions(pairing_client)
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    response = pairing_client.post(
        "/device/pair", headers={"X-Console-Pin": "24681024"},
        json={"deviceId": "legacy-device-0000001"})
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"capability", "sessionId", "expiresAt"}


def test_patient_pin_pairs_binding_and_capability_for_own_live_session(
        pairing_client, monkeypatch):
    _seed_two_sessions(pairing_client)
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    code = patient_pairing.derive_patient_pin("P-ONE")
    response = pairing_client.post(
        "/device/pair", headers={"X-Console-Pin": code},
        json={"deviceId": "patient-one-device-01"})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    body = response.json()
    assert set(body) == {"binding", "capability", "sessionId", "expiresAt"}
    assert body["sessionId"] == "S-ONE"
    live = pairing_client.get(
        "/live/state", headers={"X-Device-Capability": body["capability"]})
    assert live.status_code == 200, live.text
    # 绑定令牌不是设备路由凭据。
    as_credential = pairing_client.get(
        "/live/state", headers={"X-Device-Capability": body["binding"]})
    assert as_credential.status_code == 401


def test_other_patient_pin_gets_binding_only_and_attach_leaks_nothing(
        pairing_client, monkeypatch):
    _seed_two_sessions(pairing_client)
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    code = patient_pairing.derive_patient_pin("P-TWO")
    paired = pairing_client.post(
        "/device/pair", headers={"X-Console-Pin": code},
        json={"deviceId": "patient-two-device-01"})
    assert paired.status_code == 200, paired.text
    assert set(paired.json()) == {"binding"}
    binding = paired.json()["binding"]

    # 别人的场次 live 时:统一 409,响应体绝不出现他人研究编号/场次号。
    denied = pairing_client.post("/device/attach", json={
        "deviceId": "patient-two-device-01", "binding": binding})
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "device_attach_no_session"
    assert "P-ONE" not in denied.text and "S-ONE" not in denied.text

    # 本人开场后自动跟上;能力可用于老人端读口。
    # 切 live 是操作端动作;测试里临时回到开放回环身份完成,再恢复 PIN。
    monkeypatch.delenv("CONSOLE_PIN")
    _switch_live(pairing_client, "S-TWO")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    attached = pairing_client.post("/device/attach", json={
        "deviceId": "patient-two-device-01", "binding": binding})
    assert attached.status_code == 200, attached.text
    assert attached.headers["cache-control"] == "private, no-store"
    receipt = attached.json()
    assert set(receipt) == {"capability", "sessionId", "expiresAt"}
    assert receipt["sessionId"] == "S-TWO"
    live = pairing_client.get(
        "/live/state", headers={"X-Device-Capability": receipt["capability"]})
    assert live.status_code == 200, live.text


def test_attach_rotates_same_session_capability(pairing_client, monkeypatch):
    _seed_two_sessions(pairing_client)
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    code = patient_pairing.derive_patient_pin("P-ONE")
    paired = pairing_client.post(
        "/device/pair", headers={"X-Console-Pin": code},
        json={"deviceId": "patient-one-device-01"})
    binding = paired.json()["binding"]
    old_capability = paired.json()["capability"]
    attached = pairing_client.post("/device/attach", json={
        "deviceId": "patient-one-device-01", "binding": binding})
    assert attached.status_code == 200, attached.text
    # 同场次二次签发走轮换围栏:旧能力被撤销。
    denied = pairing_client.get(
        "/live/state", headers={"X-Device-Capability": old_capability})
    assert denied.status_code == 401
    assert denied.json()["code"] == "device_capability_revoked"
    with Session(pairing_client.test_engine) as s:
        active = [row for row in s.exec(select(PatientDeviceCapability)).all()
                  if row.revoked_at is None]
    assert len(active) == 1


def test_attach_never_steals_another_devices_live_capability(
        pairing_client, monkeypatch):
    """两台设备各持同一受试者的绑定令牌时,自动跟场不得互相吊销(对抗审查
    2026-08-22 坐实的拉锯战:每 4 秒互踢+每次轮换强制暂停训练)。轮换权只留给
    当面输码的人工配对;失联设备等能力过期或人工重配。"""
    _seed_two_sessions(pairing_client)
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    code = patient_pairing.derive_patient_pin("P-ONE")
    old_tablet = pairing_client.post(
        "/device/pair", headers={"X-Console-Pin": code},
        json={"deviceId": "patient-one-tablet-old1"})
    old_binding = old_tablet.json()["binding"]
    new_tablet = pairing_client.post(
        "/device/pair", headers={"X-Console-Pin": code},
        json={"deviceId": "patient-one-tablet-new1"})
    new_capability = new_tablet.json()["capability"]

    denied = pairing_client.post("/device/attach", json={
        "deviceId": "patient-one-tablet-old1", "binding": old_binding})
    assert denied.status_code == 409, denied.text
    # 对外与「没有场次」不可区分,不泄露另一台设备的存在。
    assert denied.json()["detail"]["code"] == "device_attach_no_session"

    alive = pairing_client.get(
        "/live/state", headers={"X-Device-Capability": new_capability})
    assert alive.status_code == 200, alive.text
    with Session(pairing_client.test_engine) as s:
        active = [row for row in s.exec(select(PatientDeviceCapability)).all()
                  if row.revoked_at is None]
    assert [row.device_id_hash for row in active] == [
        device_capability.device_id_hash("patient-one-tablet-new1")]


def test_forged_binding_is_rejected_and_locks_like_pin_bruteforce(
        pairing_client, monkeypatch):
    _seed_two_sessions(pairing_client)
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    code = patient_pairing.derive_patient_pin("P-ONE")
    paired = pairing_client.post(
        "/device/pair", headers={"X-Console-Pin": code},
        json={"deviceId": "patient-one-device-01"})
    binding = paired.json()["binding"]

    # 设备不匹配:令牌不可转移到别的设备。
    moved = pairing_client.post("/device/attach", json={
        "deviceId": "patient-one-device-99", "binding": binding})
    assert moved.status_code == 401
    assert moved.json()["detail"]["code"] == "device_binding_invalid"

    forged = binding[:-2] + "aa"
    for _ in range(7):
        response = pairing_client.post("/device/attach", json={
            "deviceId": "patient-one-device-01", "binding": forged})
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "device_binding_invalid"
    # 与 PIN 爆破同一把锁:连败后连合法令牌也 429,配对口同样锁住。
    locked = pairing_client.post("/device/attach", json={
        "deviceId": "patient-one-device-01", "binding": binding})
    assert locked.status_code == 429
    assert locked.json()["code"] == "auth_locked"
    pair_locked = pairing_client.post(
        "/device/pair", headers={"X-Console-Pin": "24681024"},
        json={"deviceId": "patient-one-device-01"})
    assert pair_locked.status_code == 429


def test_withdrawn_patient_binding_and_pin_stop_working(
        pairing_client, monkeypatch):
    _seed_two_sessions(pairing_client)
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    code = patient_pairing.derive_patient_pin("P-ONE")
    paired = pairing_client.post(
        "/device/pair", headers={"X-Console-Pin": code},
        json={"deviceId": "patient-one-device-01"})
    binding = paired.json()["binding"]
    with Session(pairing_client.test_engine) as s:
        patient = s.get(Patient, "P-ONE")
        patient.withdrawal_status = "withdrawn"
        s.add(patient)
        s.commit()
    denied = pairing_client.post("/device/attach", json={
        "deviceId": "patient-one-device-01", "binding": binding})
    assert denied.status_code == 401
    assert denied.json()["detail"]["code"] == "device_binding_revoked"
    stale_pin = pairing_client.post(
        "/device/pair", headers={"X-Console-Pin": code},
        json={"deviceId": "patient-one-device-01"})
    assert stale_pin.status_code == 401
    assert stale_pin.json()["code"] == "device_pair_pin_required"


def test_attach_unavailable_without_console_pin(pairing_client, monkeypatch):
    _seed_two_sessions(pairing_client)
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    code = patient_pairing.derive_patient_pin("P-ONE")
    binding = pairing_client.post(
        "/device/pair", headers={"X-Console-Pin": code},
        json={"deviceId": "patient-one-device-01"}).json()["binding"]
    monkeypatch.delenv("CONSOLE_PIN")
    response = pairing_client.post("/device/attach", json={
        "deviceId": "patient-one-device-01", "binding": binding})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "patient_binding_unavailable"


# ---------------- 配对码只对训练操作角色可见 ----------------

def test_pairing_code_visible_only_to_training_roles(pairing_client, monkeypatch):
    _seed_two_sessions(pairing_client)
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    eng = pairing_client.test_engine
    researcher = _login(eng, "pair-researcher", "researcher")
    steward = _login(eng, "pair-steward", "data_steward")
    try:
        rows = researcher.get("/patients").json()
        by_id = {row["patient_id"]: row for row in rows}
        assert by_id["P-ONE"]["pairing_code"] == (
            patient_pairing.derive_patient_pin("P-ONE"))
        assert by_id["P-ONE"]["pairing_code"].isdigit()
        steward_rows = steward.get("/patients").json()
        assert all(row["pairing_code"] is None for row in steward_rows)
        anonymous = pairing_client.get("/patients")
        assert anonymous.status_code == 401
    finally:
        researcher.close()
        steward.close()


def test_pairing_code_absent_without_console_pin(pairing_client, monkeypatch):
    _seed_two_sessions(pairing_client)
    rows = pairing_client.get("/patients").json()
    assert all(row["pairing_code"] is None for row in rows)
