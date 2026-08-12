"""认证主体矩阵：匿名 / 老人端 PIN / 具名账号 / 未知角色。"""
from datetime import datetime

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import access_policy, audio_store, auth, db
from app.main import app
from app.models import (AttemptEvent, AudioAssetRow, AuditLog, AuthSession, ItemEvent,
                        Patient, PatientDeviceCapability, ResearchUser,
                        SessionCloseoutReport, SessionOutcomeSummary,
                        SessionRuntimeState, TurnEvent)


@pytest.fixture
def policy_client(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", eng)
    SQLModel.metadata.create_all(eng)
    client = TestClient(app)
    client.test_engine = eng
    yield client
    client.close()


def _seed_session(client: TestClient, *, patient_id: str = "P-POLICY",
                  session_id: str = "S-POLICY",
                  trainer_id: str = "ACTOR-research") -> None:
    assert client.post("/patients", json={
        "patient_id": patient_id,
        "consent_status": "已同意",
        "consent_type": "本人同意",
        "mandarin_eligible": True,
        "recording_allowed": True,
        "is_simulation_subject": True,
        "secondary_use_allowed": True,
    }).status_code == 200
    assert client.post("/sessions", json={
        "session_id": session_id,
        "patient_id": patient_id,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "item_bank_version_id": "wk2-v1-20260707",
        "is_simulation": True,
        "trainer_id": trainer_id,
    }).status_code == 200
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
    cursor = client.put("/live/state", json={
        "kind": "cursor",
        "payload": {
            "sessionId": session_id, "screen": "present",
            "itemIdx": 0, "turnIdx": 0, "responseRole": "命名",
            "cueLevel": 0, "recording": "idle",
        },
    })
    assert cursor.status_code == 200, cursor.text


def _pair_device(client: TestClient, *, pin: str = "246810",
                 device_id: str = "policy-device-00000001") -> dict[str, str]:
    response = client.post("/device/pair", headers={"X-Console-Pin": pin}, json={
        "deviceId": device_id,
    })
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"
    return {"X-Device-Capability": response.json()["capability"]}


def _add_user(eng, username: str, role: str) -> None:
    with Session(eng) as session:
        session.add(ResearchUser(
            username=username,
            display_id=f"ACTOR-{username}",
            password_hash=auth.hash_password("password1"),
            role=role,
            created_at=datetime.now(),
        ))
        session.commit()


def _logged_in_client(eng, username: str) -> TestClient:
    client = TestClient(app)
    response = client.post("/auth/login", json={
        "username": username, "password": "password1",
    })
    assert response.status_code == 200, response.text
    token = client.cookies.get(auth.CSRF_COOKIE_NAME)
    assert token
    client.headers.update({"X-CSRF-Token": token})
    return client


@pytest.mark.parametrize(("method", "path", "kind", "roles"), [
    ("GET", "/health", access_policy.AccessKind.PUBLIC, None),
    ("POST", "/tts/speak", access_policy.AccessKind.DEVICE,
     access_policy.TRAINING_OPERATION_ROLES),
    ("GET", "/live/state", access_policy.AccessKind.DEVICE, None),
    ("GET", "/sessions/S/plan", access_policy.AccessKind.DEVICE, None),
    ("GET", "/sessions/S/patient-plan", access_policy.AccessKind.DEVICE, None),
    ("GET", "/sessions/S/patient-asset/current",
     access_policy.AccessKind.DEVICE, None),
    ("GET", "/sessions/S/patient-presentation", access_policy.AccessKind.DEVICE, None),
    ("GET", "/sessions/S/autopilot/next", access_policy.AccessKind.DEVICE, None),
    ("GET", "/sessions/S/autopilot/drain-target",
     access_policy.AccessKind.DEVICE, None),
    ("POST", "/sessions/S/autopilot/commands/C/tts",
     access_policy.AccessKind.DEVICE, access_policy.TRAINING_OPERATION_ROLES),
    ("POST", "/sessions/S/autopilot/commands/C/recording-authorization",
     access_policy.AccessKind.DEVICE, access_policy.TRAINING_OPERATION_ROLES),
    ("POST", "/sessions/S/autopilot/commands/C/acks",
     access_policy.AccessKind.DEVICE, access_policy.TRAINING_OPERATION_ROLES),
    ("POST", "/sessions/S/autopilot/commands/C/drain-ack",
     access_policy.AccessKind.DEVICE, access_policy.TRAINING_OPERATION_ROLES),
    ("POST", "/sessions/S/recording-authorization", access_policy.AccessKind.DEVICE,
     access_policy.TRAINING_OPERATION_ROLES),
    ("POST", "/audio", access_policy.AccessKind.DEVICE,
     access_policy.TRAINING_OPERATION_ROLES),
    ("PUT", "/audio/A/blob", access_policy.AccessKind.DEVICE,
     access_policy.TRAINING_OPERATION_ROLES),
    ("POST", "/live/patient-heartbeat", access_policy.AccessKind.DEVICE,
     access_policy.TRAINING_OPERATION_ROLES),
    ("PUT", "/live/state", access_policy.AccessKind.DEVICE_LIVE_WRITE,
     access_policy.TRAINING_OPERATION_ROLES),
    ("GET", "/live/console-state", access_policy.AccessKind.ACCOUNT,
     access_policy.KNOWN_ACCOUNT_ROLES),
    ("GET", "/sessions/S/audio-receipts", access_policy.AccessKind.ACCOUNT,
     access_policy.KNOWN_ACCOUNT_ROLES),
    ("GET", "/sessions/S/outcome-summary", access_policy.AccessKind.ACCOUNT,
     access_policy.KNOWN_ACCOUNT_ROLES),
    ("GET", "/sessions/S/closeout", access_policy.AccessKind.ACCOUNT,
     access_policy.KNOWN_ACCOUNT_ROLES),
    ("PUT", "/sessions/S/closeout", access_policy.AccessKind.ACCOUNT,
     access_policy.TRAINING_OPERATION_ROLES),
    ("GET", "/sessions/S/autopilot/status", access_policy.AccessKind.ACCOUNT,
     access_policy.KNOWN_ACCOUNT_ROLES),
    ("GET", "/quality/ai-metrics", access_policy.AccessKind.ACCOUNT,
     access_policy.KNOWN_ACCOUNT_ROLES),
    ("POST", "/sessions/S/autopilot/start", access_policy.AccessKind.ACCOUNT,
     access_policy.CAREGIVER_SESSION_CONTROL_ROLES),
    ("POST", "/sessions/S/autopilot/takeover", access_policy.AccessKind.ACCOUNT,
     access_policy.CAREGIVER_SESSION_CONTROL_ROLES),
    ("GET", "/caregiver/today", access_policy.AccessKind.ACCOUNT,
     access_policy.CAREGIVER_ROLES),
    ("POST", "/caregiver/visit-plans/VP/start", access_policy.AccessKind.ACCOUNT,
     access_policy.CAREGIVER_ROLES),
    ("PUT", "/caregiver/sessions/S/activation", access_policy.AccessKind.ACCOUNT,
     access_policy.CAREGIVER_ROLES),
    ("GET", "/caregiver/sessions/S/status", access_policy.AccessKind.ACCOUNT,
     access_policy.CAREGIVER_ROLES),
    ("POST", "/caregiver/sessions/S/help-requests", access_policy.AccessKind.ACCOUNT,
     access_policy.CAREGIVER_ROLES),
    ("GET", "/visit-plans/today", access_policy.AccessKind.ACCOUNT,
     access_policy.KNOWN_ACCOUNT_ROLES),
    ("GET", "/visit-plans", access_policy.AccessKind.ACCOUNT,
     access_policy.KNOWN_ACCOUNT_ROLES),
    ("POST", "/visit-plans", access_policy.AccessKind.ACCOUNT,
     access_policy.TRAINING_OPERATION_ROLES),
    ("POST", "/visit-plans/VP/approve", access_policy.AccessKind.ACCOUNT,
     access_policy.TRAINING_OPERATION_ROLES),
    ("POST", "/visit-plans/VP/start", access_policy.AccessKind.ACCOUNT,
     access_policy.TRAINING_OPERATION_ROLES),
    ("POST", "/visit-plans/VP/cancel", access_policy.AccessKind.ACCOUNT,
     access_policy.TRAINING_OPERATION_ROLES),
    ("GET", "/cloud-processing/policy", access_policy.AccessKind.ACCOUNT,
     access_policy.KNOWN_ACCOUNT_ROLES),
    ("GET", "/content/scale-protocol", access_policy.AccessKind.ACCOUNT,
     access_policy.KNOWN_ACCOUNT_ROLES),
    ("PATCH", "/patients/P/cloud-processing", access_policy.AccessKind.ACCOUNT,
     access_policy.TRAINING_OPERATION_ROLES),
    ("POST", "/patients", access_policy.AccessKind.ACCOUNT,
     access_policy.TRAINING_OPERATION_ROLES),
    ("POST", "/patients/P/withdrawal", access_policy.AccessKind.ACCOUNT,
     access_policy.ADMIN_ROLES),
    ("GET", "/patients/P/withdrawal", access_policy.AccessKind.ACCOUNT,
     access_policy.DATA_GOVERNANCE_ROLES),
    ("GET", "/governance/withdrawn-audio", access_policy.AccessKind.ACCOUNT,
     access_policy.ADMIN_ROLES),
    ("GET", "/content/item-bank-bundle", access_policy.AccessKind.ACCOUNT,
     access_policy.KNOWN_ACCOUNT_ROLES),
    ("HEAD", "/content/week1-script", access_policy.AccessKind.ACCOUNT,
     access_policy.KNOWN_ACCOUNT_ROLES),
    ("GET", "/content/autopilot-protocol", access_policy.AccessKind.ACCOUNT,
     access_policy.KNOWN_ACCOUNT_ROLES),
    ("POST", "/patients/P/scales", access_policy.AccessKind.ACCOUNT,
     access_policy.TRAINING_OPERATION_ROLES),
    ("POST", "/sessions", access_policy.AccessKind.ACCOUNT,
     access_policy.TRAINING_OPERATION_ROLES),
    ("POST", "/sessions/S/pause", access_policy.AccessKind.ACCOUNT,
     access_policy.CAREGIVER_SESSION_CONTROL_ROLES),
    ("POST", "/sessions/S/finish-intervention", access_policy.AccessKind.ACCOUNT,
     access_policy.CAREGIVER_SESSION_CONTROL_ROLES),
    ("POST", "/sessions/S/abort", access_policy.AccessKind.ACCOUNT,
     access_policy.CAREGIVER_SESSION_CONTROL_ROLES),
    ("POST", "/sessions/S/resume", access_policy.AccessKind.ACCOUNT,
     access_policy.TRAINING_OPERATION_ROLES),
    ("POST", "/sessions/S/complete", access_policy.AccessKind.ACCOUNT,
     access_policy.TRAINING_OPERATION_ROLES),
    ("POST", "/sessions/S/attempts/process", access_policy.AccessKind.ACCOUNT,
     access_policy.TRAINING_OPERATION_ROLES),
    ("POST", "/sessions/S/abnormal", access_policy.AccessKind.ACCOUNT,
     access_policy.TRAINING_OPERATION_ROLES),
    ("PATCH", "/turns/1/lock", access_policy.AccessKind.ACCOUNT,
     access_policy.TRAINING_OPERATION_ROLES),
    ("GET", "/sessions/S/plan/extra", access_policy.AccessKind.ACCOUNT,
     access_policy.KNOWN_ACCOUNT_ROLES),
    ("GET", "/audit", access_policy.AccessKind.ACCOUNT,
     access_policy.DATA_GOVERNANCE_ROLES),
    ("POST", "/audio/A/checksum", access_policy.AccessKind.ACCOUNT,
     access_policy.DATA_GOVERNANCE_ROLES),
    ("POST", "/sessions/S/export", access_policy.AccessKind.ACCOUNT,
     access_policy.DATA_GOVERNANCE_ROLES),
    ("GET", "/exports/EXP-safe", access_policy.AccessKind.ACCOUNT,
     access_policy.DATA_GOVERNANCE_ROLES),
    ("GET", "/exports/EXP-safe/unknown", access_policy.AccessKind.ACCOUNT,
     access_policy.DATA_GOVERNANCE_ROLES),
    ("DELETE", "/audio/A", access_policy.AccessKind.ACCOUNT,
     access_policy.ADMIN_ROLES),
    ("POST", "/future-unclassified-mutation", access_policy.AccessKind.ACCOUNT,
     access_policy.ADMIN_ROLES),
    ("GET", "/patient", access_policy.AccessKind.PUBLIC, None),
])
def test_central_route_policy(method, path, kind, roles):
    rule = access_policy.access_rule(method, path)
    assert rule.kind == kind
    if roles is not None:
        assert rule.roles == roles


@pytest.mark.parametrize("path", [
    "/CONTENT/item_bank_v1.json",
    "/Content/item_bank_v1.json",
    "/cOnTeNt/item_bank_v1.json",
    "/content\\item_bank_v1.json",
    "/content%5Citem_bank_v1.json",
    "/content%255Citem_bank_v1.json",
    "/c%256Fntent/item_bank_v1.json",
])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_protected_api_root_aliases_never_become_public_static_paths(method, path):
    rule = access_policy.access_rule(method, path)
    assert rule.kind == access_policy.AccessKind.ACCOUNT
    assert rule.roles == access_policy.KNOWN_ACCOUNT_ROLES


@pytest.mark.parametrize("path", [
    "/CONTENT/item-bank-bundle",
    "/content\\item-bank-bundle",
    "/content%2Fitem-bank-bundle",
    "/c%256Fntent/item-bank-bundle",
])
def test_noncanonical_protected_roots_are_concealed_before_auth(path):
    assert access_policy.noncanonical_protected_namespace_alias(path)


@pytest.mark.parametrize("path", sorted(access_policy.STAFF_CONTENT_BUNDLE_PATHS))
def test_staff_content_bundle_canonical_paths_are_not_aliases(path):
    assert not access_policy.noncanonical_protected_namespace_alias(path)
    assert not access_policy.answer_bundle_alias_must_be_hidden(path)


@pytest.mark.parametrize("path", [
    "/content/item_bank_v1.json",
    "/content/week1_script.json",
    "/content/autopilot_protocol_v1.json",
    "/CONTENT/item-bank-bundle",
    "/content/ITEM-BANK-BUNDLE",
    "/content/item%252Dbank-bundle",
    "/content/item-bank-bundle/",
])
def test_legacy_and_nonexact_answer_bundle_paths_are_always_hidden(path):
    assert access_policy.answer_bundle_alias_must_be_hidden(path)


def test_every_mutation_route_has_an_explicit_access_policy():
    unclassified = [
        f"{method} {route.path}"
        for route in app.routes if isinstance(route, APIRoute)
        for method in sorted(route.methods or set())
        if method not in {"GET", "HEAD", "OPTIONS"}
        and not access_policy.mutation_route_is_classified(method, route.path)
    ]
    assert unclassified == [], (
        "新写路由必须显式选择主体与角色，不能依赖 admin-only 兜底："
        f"{unclassified}"
    )


def test_pin_is_limited_to_patient_device_workflow(policy_client, monkeypatch):
    _seed_session(policy_client)
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    monkeypatch.setattr("app.main.tts.speak", lambda _text: (None, "null-test", False))
    pin = {"X-Console-Pin": "246810"}

    # 认证开启后，即使是老人端最小读口也不再允许匿名访问。
    assert policy_client.get("/live/state").status_code == 401
    assert policy_client.get("/sessions/S-POLICY/plan").status_code == 401
    assert policy_client.post("/tts/speak", json={"text": "您好"}).status_code == 401
    direct_pin = policy_client.get("/live/state", headers=pin)
    assert direct_pin.status_code == 401
    assert direct_pin.json()["code"] == "device_pair_required"
    capability = _pair_device(policy_client)
    for path in (
            "/live/state",
            "/sessions/S-POLICY/plan",
            "/sessions/S-POLICY/patient-plan",
            "/sessions/S-POLICY/patient-presentation"):
        assert policy_client.get(path, headers=capability).status_code == 200, path
    assert policy_client.post(
        "/tts/speak", json={"text": "您好"}, headers=capability).status_code == 204
    assert policy_client.post(
        "/sessions/S-POLICY/recording-authorization", headers=capability).status_code == 200

    # 心跳与两种老人端录音回报可用；body 被中间件读取后仍能被路由解析。
    assert policy_client.post("/live/patient-heartbeat", headers=capability, json={
        "session_id": "S-POLICY", "screen": "present",
    }).status_code == 200
    assert policy_client.put("/live/state", headers=capability, json={
        "kind": "patientRec", "payload": {
            "active": True, "turnKey": "itm-0001#1", "sessionId": "S-POLICY",
        },
    }).status_code == 200

    registration = {
        "raw_audio_id": "pin-audio", "session_id": "S-POLICY", "turn_key": "itm-0001#1",
    }
    assert policy_client.post("/audio", headers=capability, json=registration).status_code == 200
    # 登记响应丢失后仍用原 POST 幂等恢复，无需给 PIN 开放 GET 元数据。
    assert policy_client.post("/audio", headers=capability, json=registration).status_code == 200
    assert policy_client.post("/audio", headers=capability, json={
        **registration, "turn_key": "itm-0002#1",
    }).status_code == 409
    upload = policy_client.put(
        "/audio/pin-audio/blob", headers={**capability, "content-type": "audio/webm"},
        content=b"\x1a\x45\xdf\xa3pin-policy-audio")
    assert upload.status_code == 200
    assert policy_client.put("/live/state", headers=capability, json={
        "kind": "audioSaved", "payload": {
            "rawAudioId": "pin-audio", "durationSeconds": 1,
            "byteCount": upload.json()["bytes"], "checksum": upload.json()["checksum"],
            "containsDirectIdentifier": False,
            "turnKey": "itm-0001#1", "sessionId": "S-POLICY",
        },
    }).status_code == 200

    # 同一 PUT 路由上的控制台 kind 必须在进入处理器前拒绝。
    for kind in ("session", "cursor", "rapportStep"):
        denied = policy_client.put("/live/state", headers=capability, json={
            "kind": kind, "payload": {},
        })
        assert denied.status_code == 401
        assert denied.json()["code"] == "device_capability_forbidden"

    # 下列每类后台能力均须具名账号；正确 PIN 稳定返回 account_required。
    account_only_requests = (
        ("GET", "/live/console-state", {}),
        ("GET", "/patients", {}),
        ("GET", "/patients/P-POLICY", {}),
        ("POST", "/sessions", {"json": {}}),
        ("GET", "/sessions/S-POLICY/runtime", {}),
        ("POST", "/sessions/S-POLICY/pause", {}),
        ("POST", "/sessions/S-POLICY/attempts/process", {"json": {}}),
        ("POST", "/sessions/S-POLICY/interactions", {"json": {}}),
        ("POST", "/sessions/S-POLICY/technical-pause", {"json": {}}),
        ("POST", "/sessions/S-POLICY/abnormal", {"json": {}}),
        ("GET", "/sessions/S-POLICY/journal", {}),
        ("GET", "/sessions/S-POLICY/audio-receipts", {}),
        ("POST", "/patients/P-POLICY/scales", {"json": {}}),
        ("GET", "/audit", {}),
        ("GET", "/quality/ai-metrics", {}),
        ("GET", "/audio/pin-audio", {}),
        ("GET", "/audio/pin-audio/blob", {}),
        ("POST", "/audio/pin-audio/checksum", {}),
        ("POST", "/audio/pin-audio/reliability-review", {}),
        ("DELETE", "/audio/pin-audio", {}),
        ("POST", "/sessions/S-POLICY/export", {}),
    )
    for method, path, kwargs in account_only_requests:
        denied = policy_client.request(method, path, headers=pin, **kwargs)
        assert denied.status_code == 401, (method, path, denied.text)
        assert denied.json()["code"] == "account_required"

    wrong_pin = policy_client.get(
        "/live/state", headers={"X-Console-Pin": "000000"})
    assert wrong_pin.status_code == 401
    assert wrong_pin.json()["code"] == "device_pair_required"


def test_generic_tts_account_must_own_current_live_session(
        policy_client, monkeypatch):
    _seed_session(policy_client, trainer_id="ACTOR-tts-owner")
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    capability = _pair_device(
        policy_client, device_id="tts-owner-device-0001")
    synthesized: list[str] = []

    def fake_speak(text: str):
        synthesized.append(text)
        return None, "tts-owner-test", False

    monkeypatch.setattr("app.main.tts.speak", fake_speak)
    eng = policy_client.test_engine
    for username, role in (
            ("tts-owner", "researcher"),
            ("tts-other", "researcher"),
            ("tts-admin", "admin"),
            ("tts-steward", "data_steward")):
        _add_user(eng, username, role)
    owner = _logged_in_client(eng, "tts-owner")
    other = _logged_in_client(eng, "tts-other")
    administrator = _logged_in_client(eng, "tts-admin")
    steward = _logged_in_client(eng, "tts-steward")
    try:
        foreign = other.post("/tts/speak", json={"text": "跨场次探测"})
        assert foreign.status_code == 404, foreign.text
        assert foreign.json() == {"detail": "场次不存在"}
        assert synthesized == []

        steward_denied = steward.post(
            "/tts/speak", json={"text": "非终态治理账号"})
        assert steward_denied.status_code == 403, steward_denied.text
        assert steward_denied.json()["code"] == "role_forbidden"
        assert synthesized == []

        assert owner.post(
            "/tts/speak", json={"text": "owner"}).status_code == 204
        assert administrator.post(
            "/tts/speak", json={"text": "admin"}).status_code == 204
        assert policy_client.post(
            "/tts/speak", headers=capability,
            json={"text": "device"}).status_code == 204
        assert synthesized == ["owner", "admin", "device"]
    finally:
        owner.close()
        other.close()
        administrator.close()
        steward.close()


def test_non_pair_routes_never_compare_pin_or_consume_pair_limiter(
        policy_client, monkeypatch):
    _seed_session(policy_client, patient_id="P-ORACLE", session_id="S-ORACLE")
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    auth.record_success("pair:testclient")
    cases = (
        ("GET", "/live/state", {}),
        ("GET", "/sessions/S-ORACLE/plan", {}),
        ("POST", "/audio", {"json": {}}),
        ("POST", "/live/patient-heartbeat", {"json": {}}),
        ("PUT", "/audio/missing/blob", {
            "content": b"\x1a\x45\xdf\xa3x", "headers": {"content-type": "audio/webm"}}),
        ("PUT", "/live/state", {"json": {}}),
    )
    for method, path, kwargs in cases:
        baseline = policy_client.request(method, path, **kwargs)
        correct_kwargs = dict(kwargs)
        correct_headers = dict(correct_kwargs.pop("headers", {}))
        correct_headers["X-Console-Pin"] = "246810"
        wrong_kwargs = dict(kwargs)
        wrong_headers = dict(wrong_kwargs.pop("headers", {}))
        wrong_headers["X-Console-Pin"] = "000000"
        correct = policy_client.request(
            method, path, headers=correct_headers, **correct_kwargs)
        wrong = policy_client.request(method, path, headers=wrong_headers, **wrong_kwargs)
        assert (correct.status_code, correct.content) == (
            baseline.status_code, baseline.content), (method, path, correct.text)
        assert (wrong.status_code, wrong.content) == (
            baseline.status_code, baseline.content), (method, path, wrong.text)

    for guess in range(20):
        response = policy_client.get(
            "/live/state", headers={"X-Console-Pin": f"{guess:06d}"})
        assert response.status_code == 401
        assert response.json()["code"] == "device_pair_required"
    assert not auth.is_locked("pair:testclient")
    assert _pair_device(
        policy_client, device_id="oracle-device-00000001")["X-Device-Capability"]


def test_pin_device_is_scoped_to_current_live_session(policy_client, monkeypatch):
    _seed_session(policy_client, patient_id="P-CURRENT", session_id="S-CURRENT")
    assert policy_client.post("/patients", json={
        "patient_id": "P-OTHER", "is_simulation_subject": True,
    }).status_code == 200
    assert policy_client.post("/sessions", json={
        "session_id": "S-OTHER", "patient_id": "P-OTHER", "week_no": 2,
        "phase_type": "正式训练", "event_line": "正式训练",
        "item_bank_version_id": "wk2-v1-20260707", "is_simulation": True,
    }).status_code == 200
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    pin = {"X-Console-Pin": "246810"}
    capability = _pair_device(
        policy_client, device_id="current-device-0000001")

    for suffix in ("plan", "patient-plan", "patient-presentation"):
        assert policy_client.get(
            f"/sessions/S-CURRENT/{suffix}", headers=capability).status_code == 200
        mismatch = policy_client.get(
            f"/sessions/S-OTHER/{suffix}", headers=capability)
        assert mismatch.status_code == 409
        assert mismatch.json()["detail"]["code"] == "device_session_mismatch"
    assert policy_client.post(
        "/sessions/S-CURRENT/recording-authorization", headers=capability,
    ).status_code == 200
    mismatch = policy_client.post(
        "/sessions/S-OTHER/recording-authorization", headers=capability)
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "device_session_mismatch"
    assert policy_client.get("/live/state", headers=pin).json()["code"] == "device_pair_required"


def test_named_account_and_role_allowlist_matrix(policy_client):
    _seed_session(policy_client)
    eng = policy_client.test_engine
    for username, role in (
        ("research", "researcher"),
        ("steward", "data_steward"),
        ("administrator", "admin"),
        ("mystery", "future_unknown_role"),
    ):
        _add_user(eng, username, role)

    researcher = _logged_in_client(eng, "research")
    steward = _logged_in_client(eng, "steward")
    administrator = _logged_in_client(eng, "administrator")
    mystery = _logged_in_client(eng, "mystery")
    try:
        # 老人端读口与账号工作台分离；active 场次只向其
        # 具名 operator 与可审计的 admin 监督开放。
        for client in (researcher, steward, administrator):
            patients = client.get("/patients")
            assert patients.status_code == 200
            assert patients.headers["cache-control"] == "private, no-store"
            assert patients.headers["pragma"] == "no-cache"
            assert "Cookie" in patients.headers["vary"]
        bedside_read_paths = (
            "/live/state",
            "/sessions/S-POLICY/plan",
            "/sessions/S-POLICY/patient-plan",
            "/sessions/S-POLICY/patient-presentation",
        )
        for path in bedside_read_paths:
            assert researcher.get(path).status_code == 200, path
            assert administrator.get(path).status_code == 200, path
            denied = steward.get(path)
            assert denied.status_code == 403, (path, denied.text)
            assert denied.json()["detail"]["code"] == "session_terminal_read_required"

        # A named training account controls the session, but it is not the
        # paired patient device and cannot manufacture microphone/presence facts.
        for client in (researcher, administrator):
            denied_heartbeat = client.post("/live/patient-heartbeat", json={
                "session_id": "S-POLICY", "screen": "present",
            })
            assert denied_heartbeat.status_code == 403
            assert denied_heartbeat.json()["detail"]["code"] == "device_capability_required"
            for kind, payload in (
                ("patientRec", {
                    "active": True, "turnKey": "itm-0001#1",
                    "sessionId": "S-POLICY",
                }),
                ("audioSaved", {
                    "rawAudioId": "account-must-not-ack", "durationSeconds": 1,
                    "byteCount": 1, "checksum": "0" * 64,
                    "containsDirectIdentifier": False,
                    "turnKey": "itm-0001#1", "sessionId": "S-POLICY",
                }),
                ("audioDisposalConfirmed", {
                    "code": "audio_terminal_disposition", "schemaVersion": 1,
                    "action": "discard_local_copy", "reason": "deleted",
                    "rawAudioId": "account-must-not-report",
                    "sessionId": "S-POLICY", "turnKey": "itm-0001#1",
                    "byteCount": 1, "checksum": "0" * 64,
                    "containsDirectIdentifier": False,
                }),
            ):
                denied_fact = client.put("/live/state", json={
                    "kind": kind, "payload": payload,
                })
                assert denied_fact.status_code == 403
                assert denied_fact.json()["code"] == "device_capability_required"

        # 审计、导出、checksum/信度复核只属于数据治理角色。
        denied_audit = researcher.get("/audit")
        assert denied_audit.status_code == 403
        assert denied_audit.headers["cache-control"] == "private, no-store"
        assert "Cookie" in denied_audit.headers["vary"]
        assert researcher.post("/audio/missing/checksum").status_code == 403
        assert researcher.post("/sessions/missing/export").status_code == 403
        assert researcher.get("/exports/EXP-safe/unknown").status_code == 403
        assert steward.get("/audit").status_code == 200
        assert steward.post("/audio/missing/checksum").status_code == 404
        assert steward.post("/sessions/missing/export", json={
            "idempotency_key": "export-access-steward-0123456789abcdef0123456789abcdef",
        }).status_code == 404
        assert steward.get("/exports/EXP-safe/unknown").status_code == 404
        assert administrator.get("/audit").status_code == 200
        assert administrator.post("/audio/missing/checksum").status_code == 404

        # data_steward 能做数据治理，但不能修改任何临床事实或训练运行态。
        clinical_writes = (
            ("POST", "/patients", {"json": {"patient_id": "P-STEWARD-DENIED"}}),
            ("POST", "/patients/P-POLICY/scales", {"json": {
                "phase_type": "前测", "scale_name": "CETI", "score": 1,
            }}),
            ("PATCH", "/patients/P-POLICY/cloud-processing", {"json": {"allowed": False}}),
            ("POST", "/sessions/S-POLICY/pause", {}),
            ("POST", "/sessions/S-POLICY/abnormal", {"json": {
                "abnormal_type": "设备", "note": "不得落库",
            }}),
            ("POST", "/sessions/S-POLICY/autopilot/start", {"json": {
                "idempotency_key": "steward-must-not-start",
                "expected_revision": 0,
            }}),
        )
        for method, path, kwargs in clinical_writes:
            denied = steward.request(method, path, **kwargs)
            assert denied.status_code == 403, (method, path, denied.text)
            assert denied.json()["code"] == "role_forbidden"

        # researcher/admin 能通过角色门，但旧自由量表容器在所有环境永久关闭。
        researcher_scale = researcher.post("/patients/P-POLICY/scales", json={
            "phase_type": "前测", "scale_name": "CETI", "score": 1,
        })
        administrator_scale = administrator.post("/patients/P-POLICY/scales", json={
            "phase_type": "后测", "scale_name": "CETI", "score": 2,
        })
        assert researcher_scale.status_code == administrator_scale.status_code == 409
        assert researcher_scale.json()["detail"]["code"] == "scale_protocol_not_frozen"
        assert administrator_scale.json()["detail"]["code"] == "scale_protocol_not_frozen"

        # 物理删除进一步收紧到 admin；管理员的 404 证明已通过角色门到达路由。
        assert researcher.delete("/audio/missing").status_code == 403
        assert steward.delete("/audio/missing").status_code == 403
        assert administrator.delete("/audio/missing").status_code == 404

        # 未知角色可自查账号，但对任何受保护业务接口 fail-closed。
        assert mystery.get("/auth/me").status_code == 200
        for path in ("/patients", "/live/state", "/sessions/S-POLICY/plan"):
            denied = mystery.get(path)
            assert denied.status_code == 403
            assert denied.json()["code"] == "role_forbidden"
    finally:
        researcher.close()
        steward.close()
        administrator.close()
        mystery.close()


def test_expired_cookie_plus_valid_pin_still_requests_account_login(policy_client, monkeypatch):
    _seed_session(policy_client)
    _add_user(policy_client.test_engine, "expired", "researcher")
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    response = policy_client.post("/auth/login", json={
        "username": "expired", "password": "password1",
    })
    assert response.status_code == 200
    assert policy_client.get("/patients").status_code == 200

    with Session(policy_client.test_engine) as session:
        for row in session.exec(select(AuthSession)):
            session.delete(row)
        session.commit()

    denied = policy_client.get(
        "/patients", headers={"X-Console-Pin": "246810"})
    assert denied.status_code == 401
    assert denied.json()["code"] == "account_required"


def test_sensitive_audio_reads_are_audited_to_named_actor(policy_client):
    _seed_session(policy_client, trainer_id="ACTOR-reader")
    assert policy_client.post("/audio", json={
        "raw_audio_id": "audited-audio", "session_id": "S-POLICY", "turn_key": "SE_锚#1",
    }).status_code == 200
    assert policy_client.put(
        "/audio/audited-audio/blob", content=b"\x1a\x45\xdf\xa3audited-audio",
        headers={"content-type": "audio/webm"}).status_code == 200

    _add_user(policy_client.test_engine, "reader", "researcher")
    reader = _logged_in_client(policy_client.test_engine, "reader")
    try:
        assert reader.get("/audio/audited-audio").status_code == 200
        assert reader.get("/audio/audited-audio/blob").status_code == 200
    finally:
        reader.close()

    with Session(policy_client.test_engine) as session:
        entries = list(session.exec(select(AuditLog).where(
            AuditLog.action.in_({"audio_metadata_read", "audio_blob_read"}))))
    assert {entry.action for entry in entries} == {
        "audio_metadata_read", "audio_blob_read",
    }
    assert {entry.actor for entry in entries} == {"ACTOR-reader"}


def test_researcher_own_session_cannot_probe_foreign_raw_audio_id(policy_client):
    """An authorized own session must not become a cross-session id oracle."""
    _seed_session(
        policy_client,
        patient_id="P-AUDIO-FOREIGN",
        session_id="S-AUDIO-FOREIGN",
        trainer_id="ACTOR-audio-owner",
    )
    foreign_registration = {
        "raw_audio_id": "foreign-owned-audio-id",
        "session_id": "S-AUDIO-FOREIGN",
        "turn_key": "SE_锚#1",
    }
    assert policy_client.post("/audio", json=foreign_registration).status_code == 200

    # The probing researcher owns a real, active session.  Authorization for
    # that requested session must not reveal whether their selected raw id is
    # already bound to another operator or does not exist at all.
    assert policy_client.post("/patients", json={
        "patient_id": "P-AUDIO-PROBER",
        "consent_status": "已同意",
        "consent_type": "本人同意",
        "mandarin_eligible": True,
        "recording_allowed": True,
        "is_simulation_subject": True,
        "secondary_use_allowed": True,
    }).status_code == 200
    assert policy_client.post("/sessions", json={
        "session_id": "S-AUDIO-PROBER",
        "patient_id": "P-AUDIO-PROBER",
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "item_bank_version_id": "wk2-v1-20260707",
        "is_simulation": True,
        "trainer_id": "ACTOR-audio-prober",
    }).status_code == 200
    with Session(policy_client.test_engine) as session:
        session.add(AudioAssetRow(
            raw_audio_id="orphan-audio-id",
            is_simulation=True,
            data_classification="simulation",
        ))
        session.commit()

    eng = policy_client.test_engine
    for username, role in (
            ("audio-owner", "researcher"),
            ("audio-prober", "researcher"),
            ("audio-admin", "admin"),
            ("audio-steward", "data_steward")):
        _add_user(eng, username, role)
    owner = _logged_in_client(eng, "audio-owner")
    prober = _logged_in_client(eng, "audio-prober")
    administrator = _logged_in_client(eng, "audio-admin")
    steward = _logged_in_client(eng, "audio-steward")
    try:
        probe_base = {"session_id": "S-AUDIO-PROBER"}
        foreign = prober.post("/audio", json={
            **probe_base, "raw_audio_id": "foreign-owned-audio-id",
        })
        missing = prober.post("/audio", json={
            **probe_base, "raw_audio_id": "truly-missing-audio-id",
        })
        expected_denial = {
            "detail": {
                "code": "audio_registration_device_required",
                "message": "新的录音登记必须由当前已配对受试者设备或服务端自动流程创建",
            },
        }
        assert foreign.status_code == missing.status_code == 403
        assert foreign.json() == missing.json() == expected_denial

        orphan = prober.post("/audio", json={
            "raw_audio_id": "orphan-audio-id", "is_simulation": True,
        })
        orphan_missing = prober.post("/audio", json={
            "raw_audio_id": "missing-orphan-audio-id", "is_simulation": True,
        })
        assert orphan.status_code == orphan_missing.status_code == 403
        assert orphan.json() == orphan_missing.json() == expected_denial

        with Session(eng) as session:
            assert session.get(AudioAssetRow, "truly-missing-audio-id") is None
            assert session.get(AudioAssetRow, "missing-orphan-audio-id") is None
            foreign_row = session.get(AudioAssetRow, "foreign-owned-audio-id")
            assert foreign_row is not None
            assert foreign_row.session_id == "S-AUDIO-FOREIGN"

        # Exact owner recovery and administrator supervision retain their
        # existing contract; the response exposes only the immutable ACK.
        for response in (
            owner.post("/audio", json=foreign_registration),
            administrator.post("/audio", json=foreign_registration),
        ):
            assert response.status_code == 200, response.text
            assert response.json() == {
                "raw_audio_id": "foreign-owned-audio-id",
                "registered": True,
            }

        # Data stewards keep the terminal read projection but still cannot use
        # the DEVICE write route to modify clinical/audio facts.
        active_read = steward.get("/audio/foreign-owned-audio-id")
        assert active_read.status_code == 404
        with Session(eng) as session:
            runtime = session.get(SessionRuntimeState, "S-AUDIO-FOREIGN")
            assert runtime is not None
            runtime.status = "completed"
            runtime.completed_at = datetime.now()
            session.add(runtime)
            session.commit()
        assert steward.get("/audio/foreign-owned-audio-id").status_code == 200
        denied_write = steward.post("/audio", json=foreign_registration)
        assert denied_write.status_code == 403
        assert denied_write.json()["code"] == "role_forbidden"
    finally:
        owner.close()
        prober.close()
        administrator.close()
        steward.close()


def test_protected_accounts_can_only_recover_existing_exact_audio_facts(
        policy_client, monkeypatch):
    """Owner/admin supervision cannot manufacture a raw id or first voice blob."""
    _seed_session(policy_client, trainer_id="ACTOR-audio-owner")
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    capability = _pair_device(
        policy_client, device_id="account-audio-boundary-0001")
    eng = policy_client.test_engine
    _add_user(eng, "audio-owner", "researcher")
    _add_user(eng, "audio-admin-boundary", "admin")
    owner = _logged_in_client(eng, "audio-owner")
    administrator = _logged_in_client(eng, "audio-admin-boundary")
    expected_denial = {
        "detail": {
            "code": "audio_registration_device_required",
            "message": "新的录音登记必须由当前已配对受试者设备或服务端自动流程创建",
        },
    }
    try:
        for client, raw_audio_id in (
                (owner, "owner-must-not-create-audio"),
                (administrator, "admin-must-not-create-audio")):
            denied = client.post("/audio", json={
                "raw_audio_id": raw_audio_id,
                "session_id": "S-POLICY",
                "turn_key": "itm-0001#1",
            })
            assert denied.status_code == 403, denied.text
            assert denied.json() == expected_denial

        registration = {
            "raw_audio_id": "device-registered-empty-slot",
            "session_id": "S-POLICY",
            "turn_key": "itm-0001#1",
        }
        created = policy_client.post(
            "/audio", headers=capability, json=registration)
        assert created.status_code == 200, created.text

        # Exact registration ACKs are supervisory/recovery reads and remain
        # available to the owner and an administrator.
        for client in (owner, administrator):
            exact_registration = client.post("/audio", json=registration)
            assert exact_registration.status_code == 200, exact_registration.text
            assert exact_registration.json() == {
                "raw_audio_id": "device-registered-empty-slot",
                "registered": True,
            }

        audio_bytes = b"\x1a\x45\xdf\xa3device-only-first-blob"
        for client in (owner, administrator):
            denied_upload = client.put(
                "/audio/device-registered-empty-slot/blob",
                headers={"content-type": "audio/webm"},
                content=audio_bytes,
            )
            assert denied_upload.status_code == 403, denied_upload.text
            assert denied_upload.json()["detail"]["code"] == (
                "device_capability_required")

        with Session(eng) as session:
            row = session.get(AudioAssetRow, "device-registered-empty-slot")
            assert row is not None
            assert (row.checksum, row.byte_count, row.uploaded_at) == (
                None, None, None)
        assert audio_store.find_blob("device-registered-empty-slot") is None

        uploaded = policy_client.put(
            "/audio/device-registered-empty-slot/blob",
            headers={**capability, "content-type": "audio/webm"},
            content=audio_bytes,
        )
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()["idempotent"] is False

        # Once the exact immutable byte fact exists, owner/admin may recover a
        # lost HTTP ACK but cannot replace it with different content.
        for client in (owner, administrator):
            exact_upload = client.put(
                "/audio/device-registered-empty-slot/blob",
                headers={"content-type": "audio/webm"},
                content=audio_bytes,
            )
            assert exact_upload.status_code == 200, exact_upload.text
            assert exact_upload.json()["idempotent"] is True
            conflicting = client.put(
                "/audio/device-registered-empty-slot/blob",
                headers={"content-type": "audio/webm"},
                content=b"\x1a\x45\xdf\xa3account-replacement-forbidden",
            )
            assert conflicting.status_code == 409, conflicting.text
    finally:
        owner.close()
        administrator.close()


def test_single_operator_blocks_second_researcher_and_admin_supervision_is_audited(
        policy_client, monkeypatch):
    _seed_session(policy_client, trainer_id="ACTOR-a")
    assert policy_client.post("/patients", json={
        "patient_id": "P-OPERATOR-B",
        "consent_status": "已同意",
        "consent_type": "本人同意",
        "mandarin_eligible": True,
        "recording_allowed": True,
        "is_simulation_subject": True,
    }).status_code == 200
    assert policy_client.post("/sessions", json={
        "session_id": "S-OPERATOR-B",
        "patient_id": "P-OPERATOR-B",
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "item_bank_version_id": "wk2-v1-20260707",
        "is_simulation": True,
        "trainer_id": "ACTOR-b",
    }).status_code == 200

    monkeypatch.setenv("CONSOLE_PIN", "246810")
    _pair_device(policy_client, device_id="operator-a-device-0001")

    eng = policy_client.test_engine
    for username, role in (("a", "researcher"), ("b", "researcher"),
                           ("steward-op", "data_steward"), ("admin-op", "admin")):
        _add_user(eng, username, role)
    operator_a = _logged_in_client(eng, "a")
    operator_b = _logged_in_client(eng, "b")
    steward = _logged_in_client(eng, "steward-op")
    administrator = _logged_in_client(eng, "admin-op")
    try:
        # The shared patient roster must not turn its session aggregates into a
        # cross-researcher recovery-index side channel.  Researchers see counts
        # for their own sessions, data stewards see terminal counts only, and an
        # administrator retains the full supervision projection.
        operator_a_rows = {
            row["patient_id"]: row for row in operator_a.get("/patients").json()
        }
        operator_b_rows = {
            row["patient_id"]: row for row in operator_b.get("/patients").json()
        }
        steward_rows = {
            row["patient_id"]: row for row in steward.get("/patients").json()
        }
        administrator_rows = {
            row["patient_id"]: row
            for row in administrator.get("/patients").json()
        }
        assert operator_a_rows["P-POLICY"]["session_count"] == 1
        assert operator_a_rows["P-OPERATOR-B"]["session_count"] == 0
        assert operator_b_rows["P-POLICY"]["session_count"] == 0
        assert operator_b_rows["P-OPERATOR-B"]["session_count"] == 1
        assert steward_rows["P-POLICY"]["session_count"] == 0
        assert steward_rows["P-OPERATOR-B"]["session_count"] == 0
        assert administrator_rows["P-POLICY"]["session_count"] == 1
        assert administrator_rows["P-OPERATOR-B"]["session_count"] == 1

        assert operator_b.get("/patients/P-POLICY/sessions").json() == []
        assert steward.get("/patients/P-POLICY/sessions").json() == []
        session_read_paths = (
            "/sessions/S-POLICY",
            "/sessions/S-POLICY/runtime",
            "/sessions/S-POLICY/plan",
            "/sessions/S-POLICY/patient-plan",
            "/sessions/S-POLICY/patient-presentation",
            "/sessions/S-POLICY/journal",
            "/sessions/S-POLICY/attempts",
            "/sessions/S-POLICY/audio-receipts",
            "/sessions/S-POLICY/scores",
            "/sessions/S-POLICY/outcome-summary",
            "/sessions/S-POLICY/closeout",
            "/sessions/S-POLICY/autopilot/status",
        )
        for path in session_read_paths:
            foreign = operator_b.get(path)
            missing = operator_b.get(path.replace("S-POLICY", "S-MISSING"))
            assert foreign.status_code == missing.status_code == 404, (
                path, foreign.text, missing.text)
            assert foreign.json() == missing.json() == {"detail": "场次不存在"}

            steward_denied = steward.get(path)
            assert steward_denied.status_code == 403, (
                path, steward_denied.text)
            assert steward_denied.json()["detail"]["code"] == (
                "session_terminal_read_required")

        # Live snapshots have no caller-supplied id, but they must apply the
        # same concealment to the foreign session currently occupying the slot.
        for path in ("/live/console-state", "/live/state"):
            denied = operator_b.get(path)
            assert denied.status_code == 404, (path, denied.text)
            assert denied.json() == {"detail": "场次不存在"}
            steward_denied = steward.get(path)
            assert steward_denied.status_code == 403, (
                path, steward_denied.text)
            assert steward_denied.json()["detail"]["code"] == (
                "session_terminal_read_required")

        for response in (
            operator_b.post("/sessions/S-POLICY/pause"),
            operator_b.post("/sessions/S-POLICY/abort", json={
                "reason_code": "researcher_decision",
                "expected_revision": 0,
                "idempotency_key": "abort-operator-b-forbidden-0001",
            }),
        ):
            assert response.status_code == 404, response.text
            assert response.json() == {"detail": "场次不存在"}

        # B owns the target session but not the currently live A session, so B
        # cannot switch the shared live slot and silently downgrade A's device.
        switch = operator_b.put("/live/state", json={
            "kind": "session",
            "payload": {
                "sessionId": "S-OPERATOR-B",
                "weekNo": 2,
                "eventLine": "正式训练",
                "mode": "task",
                "itemBankVersionId": "wk2-v1-20260707",
            },
        })
        assert switch.status_code == 404
        assert switch.json() == {"detail": "场次不存在"}
        with Session(eng) as session:
            capability = session.exec(select(PatientDeviceCapability)).one()
            assert capability.recovery_only_at is None

        assert len(operator_a.get("/patients/P-POLICY/sessions").json()) == 1
        assert operator_a.get("/sessions/S-POLICY/runtime").status_code == 200
        assert operator_a.get("/sessions/S-POLICY/attempts").status_code == 200
        assert operator_a.get("/sessions/S-POLICY/patient-plan").status_code == 200
        assert operator_a.get("/sessions/S-POLICY/patient-presentation").status_code == 200
        assert operator_a.get("/live/state").status_code == 200
        assert administrator.get("/sessions/S-POLICY/patient-plan").status_code == 200
        assert administrator.get("/sessions/S-POLICY/patient-presentation").status_code == 200
        assert administrator.get("/live/state").status_code == 200
        assert operator_a.post("/sessions/S-POLICY/pause").status_code == 200
        assert operator_a.post("/sessions/S-POLICY/resume").status_code == 200

        supervised = administrator.post("/sessions/S-POLICY/pause")
        assert supervised.status_code == 200, supervised.text
        with Session(eng) as session:
            entries = list(session.exec(select(AuditLog).where(
                AuditLog.action == "session_operator_admin_supervision",
                AuditLog.session_id == "S-POLICY",
            )))
        assert entries
        assert {entry.actor for entry in entries} == {"ACTOR-admin-op"}

        assert operator_a.post("/sessions/S-POLICY/resume").status_code == 200
        aborted = operator_a.post("/sessions/S-POLICY/abort", json={
            "reason_code": "participant_declined",
            "expected_revision": supervised.json()["revision"] + 1,
            "idempotency_key": "abort-operator-a-participant-declined-0001",
        })
        assert aborted.status_code == 200
        assert aborted.json()["endReason"] == "participant_declined"
    finally:
        operator_a.close()
        operator_b.close()
        steward.close()
        administrator.close()


def test_foreign_historical_mutations_are_concealed_before_state_diagnostics(
        policy_client, monkeypatch):
    """Orphan admission/runtime state must never become a cross-owner oracle."""
    _seed_session(
        policy_client,
        trainer_id="ACTOR-historical-owner",
    )
    with Session(policy_client.test_engine) as session:
        session.add(AudioAssetRow(
            raw_audio_id="historical-owner-audio",
            session_id="S-POLICY",
            turn_key="SE_锚#1",
            is_simulation=True,
            data_classification="simulation",
        ))
        session.add(AudioAssetRow(
            raw_audio_id="historical-unbound-audio",
            session_id=None,
            is_simulation=True,
            data_classification="simulation",
        ))
        session.commit()

    eng = policy_client.test_engine
    _add_user(eng, "historical-owner", "researcher")
    _add_user(eng, "historical-prober", "researcher")
    owner = _logged_in_client(eng, "historical-owner")
    prober = _logged_in_client(eng, "historical-prober")
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    try:
        cursor_body = {
            "screen": "present",
            "itemIdx": 0,
            "turnIdx": 0,
            "responseRole": "命名",
            "recording": "idle",
        }
        closeout_body = {
            "idempotency_key": "historical-closeout-0001",
            "expected_revision": 0,
            "report_status": "no_additional_observation",
        }
        abort_body = {
            "reason_code": "researcher_decision",
            "expected_revision": 0,
            "idempotency_key": "historical-abort-probe-0001",
        }
        autopilot_body = {
            "idempotency_key": "historical-autopilot-0001",
            "expected_revision": 0,
        }
        mutation_specs = (
            ("recording authorization", "POST", "recording-authorization", None),
            ("runtime cursor", "PUT", "runtime/cursor", cursor_body),
            ("finish intervention", "POST", "finish-intervention", None),
            ("closeout", "PUT", "closeout", closeout_body),
            ("complete", "POST", "complete", None),
            ("abort", "POST", "abort", abort_body),
            ("autopilot start", "POST", "autopilot/start", autopilot_body),
            ("autopilot takeover", "POST", "autopilot/takeover", autopilot_body),
        )
        for label, method, suffix, body in mutation_specs:
            kwargs = {"json": body} if body is not None else {}
            foreign = prober.request(
                method, f"/sessions/S-POLICY/{suffix}", **kwargs)
            missing = prober.request(
                method, f"/sessions/S-MISSING/{suffix}", **kwargs)
            assert foreign.status_code == missing.status_code == 404, (
                label, foreign.text, missing.text)
            assert foreign.json() == missing.json() == {
                "detail": "场次不存在",
            }

            owner_response = owner.request(
                method, f"/sessions/S-POLICY/{suffix}", **kwargs)
            assert owner_response.status_code == 409, (
                label, owner_response.text)
            assert owner_response.json()["detail"]["code"] == (
                "session_visit_plan_admission_required")

        session_handshake = {
            "kind": "session",
            "payload": {
                "sessionId": "S-POLICY",
                "weekNo": 2,
                "eventLine": "正式训练",
                "mode": "task",
                "itemBankVersionId": "wk2-v1-20260707",
            },
        }
        foreign_session_live = prober.put("/live/state", json=session_handshake)
        session_handshake["payload"]["sessionId"] = "S-MISSING"
        missing_session_live = prober.put("/live/state", json=session_handshake)
        assert foreign_session_live.status_code == missing_session_live.status_code == 404
        assert foreign_session_live.json() == missing_session_live.json() == {
            "detail": "场次不存在",
        }

        live_cursor = {
            "kind": "cursor",
            "payload": {
                "sessionId": "S-POLICY",
                **cursor_body,
            },
        }
        foreign_cursor_live = prober.put("/live/state", json=live_cursor)
        live_cursor["payload"]["sessionId"] = "S-MISSING"
        missing_cursor_live = prober.put("/live/state", json=live_cursor)
        assert foreign_cursor_live.status_code == missing_cursor_live.status_code == 404
        assert foreign_cursor_live.json() == missing_cursor_live.json() == {
            "detail": "live payload 场次不存在",
        }

        for audio_path, missing_path in (
            ("/audio/historical-owner-audio", "/audio/missing-audio"),
            ("/audio/historical-owner-audio/blob", "/audio/missing-audio/blob"),
            ("/audio/historical-unbound-audio", "/audio/missing-audio"),
            ("/audio/historical-unbound-audio/blob", "/audio/missing-audio/blob"),
        ):
            foreign = prober.get(audio_path)
            missing = prober.get(missing_path)
            assert foreign.status_code == missing.status_code == 404
            assert foreign.json() == missing.json() == {
                "detail": "音频不存在",
            }

        for audio_id in (
                "historical-owner-audio", "historical-unbound-audio"):
            foreign = prober.put(
                f"/audio/{audio_id}/blob",
                content=b"\x1a\x45\xdf\xa3must-not-persist",
                headers={"content-type": "audio/webm"},
            )
            missing = prober.put(
                "/audio/missing-audio/blob",
                content=b"\x1a\x45\xdf\xa3must-not-persist",
                headers={"content-type": "audio/webm"},
            )
            assert foreign.status_code == missing.status_code == 404
            assert foreign.json() == missing.json() == {
                "detail": "音频不存在",
            }

        # Reordering concealment must not accidentally admit the exact owner.
        owner_audio = owner.put(
            "/audio/historical-owner-audio/blob",
            content=b"\x1a\x45\xdf\xa3must-not-persist",
            headers={"content-type": "audio/webm"},
        )
        assert owner_audio.status_code == 409, owner_audio.text
        assert owner_audio.json()["detail"]["code"] == (
            "session_visit_plan_admission_required")
        with Session(eng) as session:
            for raw_id in (
                    "historical-owner-audio", "historical-unbound-audio"):
                row = session.get(AudioAssetRow, raw_id)
                assert row is not None
                assert row.checksum is None
                assert row.byte_count is None
    finally:
        owner.close()
        prober.close()


def test_owned_session_endpoints_conceal_foreign_attempt_and_audio_ids(
        policy_client):
    """Global evidence identifiers must not become cross-researcher oracles."""
    _seed_session(
        policy_client,
        patient_id="P-EVIDENCE-A",
        session_id="S-EVIDENCE-A",
        trainer_id="ACTOR-evidence-a",
    )
    assert policy_client.post("/patients", json={
        "patient_id": "P-EVIDENCE-B",
        "consent_status": "已同意",
        "consent_type": "本人同意",
        "mandarin_eligible": True,
        "recording_allowed": True,
        "is_simulation_subject": True,
    }).status_code == 200
    assert policy_client.post("/sessions", json={
        "session_id": "S-EVIDENCE-B",
        "patient_id": "P-EVIDENCE-B",
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "item_bank_version_id": "wk2-v1-20260707",
        "is_simulation": True,
        "trainer_id": "ACTOR-evidence-b",
    }).status_code == 200

    with Session(policy_client.test_engine) as session:
        foreign_audio = AudioAssetRow(
            raw_audio_id="foreign-evidence-audio",
            session_id="S-EVIDENCE-A",
            turn_key="SE_锚#1",
            is_simulation=True,
            data_classification="simulation",
        )
        own_item = ItemEvent(
            session_id="S-EVIDENCE-B",
            item_id="SE_锚",
            task_type="单要素",
            item_set_type="训练集",
        )
        session.add(foreign_audio)
        session.add(own_item)
        session.flush()
        foreign_attempt = AttemptEvent(
            session_id="S-EVIDENCE-A",
            item_id="SE_锚",
            turn_seq=1,
            response_role="命名",
            attempt_seq=1,
            raw_audio_id=foreign_audio.raw_audio_id,
            prompt_level=0,
            processing_status="technical_failure",
            error_code="foreign-private-status",
            is_simulation=True,
        )
        session.add(foreign_attempt)
        session.commit()
        session.refresh(foreign_attempt)
        session.refresh(own_item)
        foreign_attempt_id = int(foreign_attempt.id)
        own_item_id = int(own_item.id)

    _add_user(policy_client.test_engine, "evidence-b", "researcher")
    operator_b = _logged_in_client(policy_client.test_engine, "evidence-b")
    try:
        process_body = {
            "item_id": "SE_锚",
            "turn_seq": 1,
            "response_role": "命名",
            "prompt_level": 0,
            "duration_seconds": 1,
        }
        foreign_audio = operator_b.post(
            "/sessions/S-EVIDENCE-B/attempts/process",
            json={**process_body, "raw_audio_id": "foreign-evidence-audio"},
        )
        missing_audio = operator_b.post(
            "/sessions/S-EVIDENCE-B/attempts/process",
            json={**process_body, "raw_audio_id": "missing-evidence-audio"},
        )
        assert foreign_audio.status_code == missing_audio.status_code == 404
        assert foreign_audio.json() == missing_audio.json() == {
            "detail": "音频资产不存在",
        }

        interaction_body = {
            "event_type": "technical_pause",
            "error_code": "test_pause",
        }
        foreign_attempt_response = operator_b.post(
            "/sessions/S-EVIDENCE-B/interactions",
            json={**interaction_body, "attempt_id": foreign_attempt_id},
        )
        missing_attempt_response = operator_b.post(
            "/sessions/S-EVIDENCE-B/interactions",
            json={**interaction_body, "attempt_id": foreign_attempt_id + 1000},
        )
        assert (foreign_attempt_response.status_code
                == missing_attempt_response.status_code == 409)
        assert foreign_attempt_response.json() == missing_attempt_response.json() == {
            "detail": {
                "code": "technical_pause_atomic_required",
                "message": "技术暂停必须使用原子停止命令；旧交互入口永不单独记账",
            },
        }

        foreign_turn_source = operator_b.post(
            f"/items/{own_item_id}/turns",
            json={"turn_seq": 1, "raw_audio_id": "foreign-evidence-audio"},
        )
        missing_turn_source = operator_b.post(
            f"/items/{own_item_id}/turns",
            json={"turn_seq": 1, "raw_audio_id": "missing-evidence-audio"},
        )
        assert foreign_turn_source.status_code == missing_turn_source.status_code == 404
        assert foreign_turn_source.json() == missing_turn_source.json() == {
            "detail": "source attempt 不存在",
        }
        assert "foreign-private-status" not in foreign_turn_source.text
    finally:
        operator_b.close()


def test_completed_session_remains_owner_bound_and_steward_can_read_terminal(
        policy_client):
    """Closing a session must not turn its ASR/audio evidence into team-wide data."""
    _seed_session(policy_client, trainer_id="ACTOR-completed-owner")
    assert policy_client.post("/audio", json={
        "raw_audio_id": "completed-owner-audio",
        "session_id": "S-POLICY",
        "turn_key": "SE_锚#1",
    }).status_code == 200
    assert policy_client.put(
        "/audio/completed-owner-audio/blob",
        content=b"\x1a\x45\xdf\xa3completed-owner-audio",
        headers={"content-type": "audio/webm"},
    ).status_code == 200

    with Session(policy_client.test_engine) as session:
        session.add(AttemptEvent(
            session_id="S-POLICY",
            item_id="SE_锚",
            turn_seq=1,
            response_role="命名",
            attempt_seq=1,
            raw_audio_id="completed-owner-audio",
            prompt_level=0,
            asr_text="只有场次 owner 与终态数据管理员可读",
            processing_status="completed",
            is_simulation=True,
        ))
        runtime = session.get(SessionRuntimeState, "S-POLICY")
        assert runtime is not None
        runtime.status = "completed"
        runtime.revision += 1
        runtime.completed_at = datetime.now()
        runtime.ended_by = "ACTOR-completed-owner"
        runtime.end_reason = "completion_gate_passed"
        session.add(runtime)
        session.commit()

    eng = policy_client.test_engine
    for username, role in (
            ("completed-owner", "researcher"),
            ("completed-other", "researcher"),
            ("completed-steward", "data_steward")):
        _add_user(eng, username, role)
    owner = _logged_in_client(eng, "completed-owner")
    other = _logged_in_client(eng, "completed-other")
    steward = _logged_in_client(eng, "completed-steward")
    try:
        # Researcher lists are owner-scoped across every lifecycle state.
        assert len(owner.get("/patients/P-POLICY/sessions").json()) == 1
        assert other.get("/patients/P-POLICY/sessions").json() == []
        steward_rows = steward.get("/patients/P-POLICY/sessions")
        assert steward_rows.status_code == 200
        assert len(steward_rows.json()) == 1

        account_read_paths = (
            "/sessions/S-POLICY",
            "/sessions/S-POLICY/runtime",
            "/sessions/S-POLICY/plan",
            "/sessions/S-POLICY/patient-plan",
            "/sessions/S-POLICY/patient-presentation",
            "/sessions/S-POLICY/journal",
            "/sessions/S-POLICY/attempts",
            "/sessions/S-POLICY/audio-receipts",
            "/sessions/S-POLICY/scores",
        )
        for path in account_read_paths:
            denied = other.get(path)
            missing = other.get(path.replace("S-POLICY", "S-MISSING"))
            assert denied.status_code == missing.status_code == 404, (
                path, denied.text, missing.text)
            assert denied.json() == missing.json() == {"detail": "场次不存在"}

            owner_read = owner.get(path)
            assert owner_read.status_code == 200, (path, owner_read.text)
            steward_read = steward.get(path)
            assert steward_read.status_code == 200, (path, steward_read.text)

        denied_live = other.get("/live/state")
        assert denied_live.status_code == 404, denied_live.text
        assert denied_live.json() == {"detail": "场次不存在"}
        assert owner.get("/live/state").status_code == 200
        assert steward.get("/live/state").status_code == 200

        # Raw audio ids and withdrawal status are more sensitive than ordinary
        # session routing.  Foreign and unknown ids therefore share one exact
        # 404 projection, while the owner and terminal data steward retain their
        # authorized reads.
        for path, missing_path in (
            ("/audio/completed-owner-audio", "/audio/missing-owner-audio"),
            ("/audio/completed-owner-audio/blob", "/audio/missing-owner-audio/blob"),
        ):
            denied = other.get(path)
            missing = other.get(missing_path)
            assert denied.status_code == missing.status_code == 404
            assert denied.json() == missing.json() == {"detail": "音频不存在"}
            assert owner.get(path).status_code == 200
            assert steward.get(path).status_code == 200

        exact_registration = {
            "raw_audio_id": "completed-owner-audio",
            "session_id": "S-POLICY",
            "turn_key": "SE_锚#1",
        }
        missing_registration = {
            **exact_registration,
            "raw_audio_id": "missing-owner-audio",
        }
        foreign_replay = other.post("/audio", json=exact_registration)
        foreign_missing = other.post("/audio", json=missing_registration)
        assert foreign_replay.status_code == foreign_missing.status_code == 404
        assert foreign_replay.json() == foreign_missing.json() == {
            "detail": "场次不存在"}
        owner_replay = owner.post("/audio", json=exact_registration)
        assert owner_replay.status_code == 200, owner_replay.text
        assert owner_replay.json() == {
            "raw_audio_id": "completed-owner-audio",
            "registered": True,
        }

        owner_journal = owner.get("/sessions/S-POLICY/journal").json()
        assert owner_journal["attempts"][0]["asr_text"] == (
            "只有场次 owner 与终态数据管理员可读")

        # Missing terminal artifacts still authorize owner/steward first; a
        # different researcher must not learn whether those artifacts exist.
        for path in (
                "/sessions/S-POLICY/outcome-summary",
                "/sessions/S-POLICY/closeout"):
            denied = other.get(path)
            missing = other.get(path.replace("S-POLICY", "S-MISSING"))
            assert denied.status_code == missing.status_code == 404, (
                path, denied.text, missing.text)
            assert denied.json() == missing.json() == {"detail": "场次不存在"}
            assert owner.get(path).status_code == 404
            assert steward.get(path).status_code == 404

        with Session(eng) as session:
            withdrawn_audio = session.get(AudioAssetRow, "completed-owner-audio")
            assert withdrawn_audio is not None
            withdrawn_audio.withdrawn = True
            withdrawn_audio.withdrawal_status = "subject_withdrawn"
            session.add(withdrawn_audio)
            session.commit()
        assert other.get("/audio/completed-owner-audio").status_code == 404
        assert owner.get("/audio/completed-owner-audio").status_code == 409
        # An exact immutable registration ACK remains recoverable, but exposes
        # no AudioAssetRow or withdrawal fields.
        withdrawn_replay = owner.post("/audio", json=exact_registration)
        assert withdrawn_replay.status_code == 200, withdrawn_replay.text
        assert withdrawn_replay.json() == {
            "raw_audio_id": "completed-owner-audio",
            "registered": True,
        }
    finally:
        owner.close()
        other.close()
        steward.close()


def test_review_writes_are_owner_bound_and_admin_supervision_is_audited(
        policy_client):
    secret = "SECRET-ASR-OWNER-ONLY"
    _seed_session(policy_client, trainer_id="ACTOR-review-owner")
    with Session(policy_client.test_engine) as session:
        runtime = session.get(SessionRuntimeState, "S-POLICY")
        assert runtime is not None
        runtime.status = "intervention_completed"
        runtime.intervention_completed_at = datetime.now()
        runtime.revision += 1
        session.add(runtime)
        item = ItemEvent(
            session_id="S-POLICY", item_id="SE_锚",
            task_type="单要素", item_set_type="训练集")
        session.add(item)
        session.flush()
        turn = TurnEvent(
            item_event_id=item.id, turn_seq=1, response_role="命名",
            asr_text=secret, confirmation_revision=0)
        session.add(turn)
        session.add(SessionOutcomeSummary(
            session_id="S-POLICY",
            schema_version="session-outcome-summary.v1",
            generator_version="test-owner-guard.v1",
            item_bank_version_id="wk2-v1-20260707",
            is_simulation=True,
            data_classification="simulation",
            expected_turns=0,
            matched_turns=0,
            completed_attempt_turns=0,
            audio_evidenced_turns=0,
            total_attempts=0,
            completed_attempts=0,
            needs_review_attempts=0,
            technical_failure_attempts=0,
            prompt_level_0_count=0,
            prompt_level_1_count=0,
            prompt_level_2_count=0,
            prompt_level_3_count=0,
            technical_pause_count=0,
            researcher_takeover_count=0,
            source_digest="0" * 64,
            generated_at=datetime.now(),
        ))
        session.commit()
        session.refresh(item)
        session.refresh(turn)
        item_event_id = int(item.id)
        turn_id = int(turn.id)

    eng = policy_client.test_engine
    for username, role in (
            ("review-owner", "researcher"),
            ("review-other", "researcher"),
            ("review-admin", "admin")):
        _add_user(eng, username, role)
    owner = _logged_in_client(eng, "review-owner")
    other = _logged_in_client(eng, "review-other")
    administrator = _logged_in_client(eng, "review-admin")
    try:
        missing_item_id = item_event_id + 100_000
        missing_turn_id = turn_id + 100_000
        turn_cases = (
            ("PATCH", f"/turns/{turn_id}/confirm",
             f"/turns/{missing_turn_id}/confirm", {"json": {
                "confirmed_response_text": "cross-owner",
                "expected_revision": 0,
                "idempotency_key": "review-other-confirm-0001",
            }}),
            ("PATCH", f"/turns/{turn_id}/lock",
             f"/turns/{missing_turn_id}/lock", {"json": {
                "reviewer_id": "forged", "element_value": 1,
                "prompt_level": 0,
            }}),
            ("POST", f"/turns/{turn_id}/ai-judge",
             f"/turns/{missing_turn_id}/ai-judge", {}),
        )
        for method, foreign_path, missing_path, kwargs in turn_cases:
            foreign = other.request(method, foreign_path, **kwargs)
            missing = other.request(method, missing_path, **kwargs)
            assert foreign.status_code == missing.status_code == 404, (
                foreign_path, foreign.text, missing.text)
            assert foreign.json() == missing.json() == {"detail": "环节不存在"}
            assert secret not in foreign.text

        item_cases = (
            ("POST", f"/items/{item_event_id}/turns",
             f"/items/{missing_item_id}/turns", {"json": {"turn_seq": 2}}),
        )
        for method, foreign_path, missing_path, kwargs in item_cases:
            foreign = other.request(method, foreign_path, **kwargs)
            missing = other.request(method, missing_path, **kwargs)
            assert foreign.status_code == missing.status_code == 404, (
                foreign_path, foreign.text, missing.text)
            assert foreign.json() == missing.json() == {
                "detail": "题目事件不存在"}

        session_cases = (
            ("POST", "/sessions/S-POLICY/items", "/sessions/S-MISSING/items",
             {"json": {
                 "item_id": "SE_锚", "task_type": "单要素",
                 "item_set_type": "训练集",
             }}),
            ("PUT", "/sessions/S-POLICY/closeout",
             "/sessions/S-MISSING/closeout", {"json": {
                "idempotency_key": "review-other-closeout-0001",
                "expected_revision": 0,
                "report_status": "no_additional_observation",
            }}),
        )
        for method, foreign_path, missing_path, kwargs in session_cases:
            foreign = other.request(method, foreign_path, **kwargs)
            missing = other.request(method, missing_path, **kwargs)
            assert foreign.status_code == missing.status_code == 404, (
                foreign_path, foreign.text, missing.text)
            assert foreign.json() == missing.json() == {"detail": "场次不存在"}

        with Session(eng) as session:
            unchanged = session.get(TurnEvent, turn_id)
            assert unchanged is not None
            assert unchanged.confirmed_response_text is None
            assert unchanged.confirmation_revision == 0
            assert unchanged.score_locked is False
            assert session.get(SessionCloseoutReport, "S-POLICY") is None

        owned_confirm = owner.patch(f"/turns/{turn_id}/confirm", json={
            "confirmed_response_text": "owner-confirmed",
            "expected_revision": 0,
            "idempotency_key": "review-owner-confirm-0001",
        })
        assert owned_confirm.status_code == 200, owned_confirm.text
        owned_closeout = owner.put("/sessions/S-POLICY/closeout", json={
            "idempotency_key": "review-owner-closeout-0001",
            "expected_revision": 0,
            "report_status": "no_additional_observation",
        })
        assert owned_closeout.status_code == 200, owned_closeout.text

        supervised_confirm = administrator.patch(
            f"/turns/{turn_id}/confirm", json={
                "confirmed_response_text": "admin-supervised",
                "expected_revision": 1,
                "idempotency_key": "review-admin-confirm-0001",
            })
        assert supervised_confirm.status_code == 200, supervised_confirm.text
        supervised_closeout = administrator.put(
            "/sessions/S-POLICY/closeout", json={
                "idempotency_key": "review-admin-closeout-0001",
                "expected_revision": 1,
                "report_status": "no_additional_observation",
            })
        assert supervised_closeout.status_code == 200, supervised_closeout.text

        with Session(eng) as session:
            entries = list(session.exec(select(AuditLog).where(
                AuditLog.action == "session_operator_admin_supervision",
                AuditLog.session_id == "S-POLICY",
            )))
        assert len(entries) >= 2
        assert {entry.actor for entry in entries} == {"ACTOR-review-admin"}
        summaries = "\n".join(entry.summary for entry in entries)
        assert "修改研究确认回答" in summaries
        assert "保存现场收尾记录" in summaries
        assert secret not in summaries
    finally:
        owner.close()
        other.close()
        administrator.close()


def test_local_m0_mutation_fence_cannot_reopen_withdrawn_content(policy_client):
    _seed_session(policy_client, trainer_id="LOCAL-M0")
    with Session(policy_client.test_engine) as session:
        patient = session.get(Patient, "P-POLICY")
        assert patient is not None
        patient.withdrawal_status = "withdrawn"
        patient.consent_status = "withdrawn"
        runtime = session.get(SessionRuntimeState, "S-POLICY")
        assert runtime is not None
        runtime.status = "intervention_completed"
        runtime.intervention_completed_at = datetime.now()
        item = ItemEvent(
            session_id="S-POLICY", item_id="SE_锚",
            task_type="单要素", item_set_type="训练集")
        session.add(patient)
        session.add(runtime)
        session.add(item)
        session.flush()
        turn = TurnEvent(
            item_event_id=item.id, turn_seq=1, response_role="命名",
            asr_text="WITHDRAWN-CONTENT", confirmation_revision=0)
        session.add(turn)
        session.commit()
        session.refresh(turn)
        turn_id = int(turn.id)

    responses = (policy_client.post("/sessions/S-POLICY/items", json={
        "item_id": "SE_树", "task_type": "单要素",
    }),)
    for response in responses:
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == (
            "subject_withdrawn_content_unavailable")
        assert "WITHDRAWN-CONTENT" not in response.text

    with Session(policy_client.test_engine) as session:
        turn = session.get(TurnEvent, turn_id)
        assert turn is not None
        assert turn.confirmed_response_text is None
        assert session.exec(select(ItemEvent).where(
            ItemEvent.session_id == "S-POLICY")).all() == [
                session.get(ItemEvent, turn.item_event_id)]
