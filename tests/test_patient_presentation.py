"""受试者设备只能获得当前游标的最小内容投影。"""
from __future__ import annotations

from datetime import datetime
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app import auth, content, db, patient_presentation
from app.main import app
from app.models import (
    AudioAssetRow, AudioCaptureReceipt, LiveState, ResearchUser,
)


@pytest.fixture
def presentation_client(tmp_path, monkeypatch):
    # 显式文件型临时库；不触及默认 data/app.db。
    engine = create_engine(
        f"sqlite:///{tmp_path / 'patient-presentation.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    client = TestClient(app)
    client.test_engine = engine
    yield client
    client.close()
    engine.dispose()


def _create_patient(client: TestClient, patient_id: str) -> None:
    response = client.post("/patients", json={
        "patient_id": patient_id,
        "consent_status": "已同意",
        "consent_type": "本人同意",
        "mandarin_eligible": True,
        "recording_allowed": True,
        "secondary_use_allowed": True,
        "is_simulation_subject": True,
    })
    assert response.status_code == 200, response.text


def _create_session(
        client: TestClient, *, session_id: str, patient_id: str, week_no: int) -> None:
    relationship = week_no == 1
    response = client.post("/sessions", json={
        "session_id": session_id,
        "patient_id": patient_id,
        "week_no": week_no,
        "phase_type": "关系建立" if relationship else "正式训练",
        "event_line": "关系建立环节" if relationship else "正式训练",
        "item_bank_version_id": "wk2-v1-20260707",
        "is_simulation": True,
        "trainer_id": "PRESENTATION-RESEARCHER",
    })
    assert response.status_code == 200, response.text


def _post_live_session(client: TestClient, session_id: str, *, week_no: int) -> None:
    relationship = week_no == 1
    response = client.put("/live/state", json={
        "kind": "session",
        "payload": {
            "sessionId": session_id,
            "weekNo": week_no,
            "eventLine": "关系建立环节" if relationship else "正式训练",
            "mode": "rapport" if relationship else "task",
            "itemBankVersionId": "wk2-v1-20260707",
        },
    })
    assert response.status_code == 200, response.text


def _pair(client: TestClient, device_id: str) -> dict[str, str]:
    response = client.post("/device/pair", headers={"X-Console-Pin": "24681024"}, json={
        "deviceId": device_id,
    })
    assert response.status_code == 200, response.text
    return {"X-Device-Capability": response.json()["capability"]}


def _account_client(engine, username: str = "presentation-researcher") -> TestClient:
    with Session(engine) as session:
        session.add(ResearchUser(
            username=username,
            display_id="PRESENTATION-RESEARCHER",
            password_hash=auth.hash_password("test-password-1"),
            role="researcher",
            created_at=datetime.now(),
        ))
        session.commit()
    client = TestClient(app)
    login = client.post("/auth/login", json={
        "username": username,
        "password": "test-password-1",
    })
    assert login.status_code == 200, login.text
    csrf = client.cookies.get(auth.CSRF_COOKIE_NAME)
    assert csrf
    client.headers["X-CSRF-Token"] = csrf
    return client


def _all_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _all_keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _all_keys(child)}
    return set()


def test_rapport_projection_rejects_unrecognized_speaker_role():
    with pytest.raises(ValueError, match="说话人未被允许"):
        patient_presentation.resolve_rapport_text(
            {
                "sections": [{
                    "key": "unsafe", "speaker": "未知角色",
                    "questions": [{"ask": "不应下发"}],
                }],
            },
            section_key="unsafe",
            question_idx=0,
        )


def test_task_device_gets_only_current_text_and_redacted_plan(
        presentation_client, monkeypatch):
    client = presentation_client
    _create_patient(client, "P-PRESENT")
    _create_session(client, session_id="S-PRESENT", patient_id="P-PRESENT", week_no=2)
    _create_patient(client, "P-FOREIGN")
    _create_session(client, session_id="S-FOREIGN", patient_id="P-FOREIGN", week_no=2)
    _post_live_session(client, "S-PRESENT", week_no=2)

    full_plan = client.get("/sessions/S-PRESENT/plan").json()
    first_item = full_plan["items"][0]
    first_turn = first_item["turns"][0]
    cursor_write = client.put("/live/state", json={
        "kind": "cursor",
        "payload": {
            "sessionId": "S-PRESENT",
            "screen": "present",
            "itemIdx": 0,
            "turnIdx": 0,
            "responseRole": first_turn["response_role"],
            "cueLevel": 1,
            "recording": "idle",
            "fbKey": "self",
            "fbItemId": first_item["item_id"],
            "fbSeq": 1,
        },
    })
    assert cursor_write.status_code == 200, cursor_write.text

    # 本地开放模式没有 auth_kind 可区分账号与设备，因此老人端
    # 必须走语义明确的专用路由，不能走会返回账号全量计划的 /plan。
    open_patient_plan = client.get("/sessions/S-PRESENT/patient-plan")
    assert open_patient_plan.status_code == 200, open_patient_plan.text
    assert open_patient_plan.headers["cache-control"] == "private, no-store"

    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    capability = _pair(client, "presentation-device-000001")

    anonymous = client.get("/sessions/S-PRESENT/patient-presentation")
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "device_pair_required"

    # 新的工作人员整包接口仍然只接受具名账号；设备 capability
    # 不能借老人端最小投影读取答案定义。
    for path in (
        "/content/item-bank-bundle",
        "/content/week1-script",
        "/content/autopilot-protocol",
    ):
        denied = client.get(path, headers=capability)
        assert denied.status_code == 401, (path, denied.text)
        assert denied.json()["code"] == "account_required"

    # 旧静态文件名永久消失，对任意凭据都只表现为不存在。
    for path in (
        "/content/item_bank_v1.json",
        "/content/week1_script.json",
        "/content/autopilot_protocol_v1.json",
    ):
        denied = client.get(path, headers=capability)
        assert denied.status_code == 404, (path, denied.text)
        assert denied.json()["code"] == "resource_not_found"

    device_plan_response = client.get("/sessions/S-PRESENT/plan", headers=capability)
    assert device_plan_response.status_code == 200, device_plan_response.text
    device_plan = device_plan_response.json()
    assert device_plan["items"]
    assert set(device_plan) == {
        "item_bank_version_id", "week_no", "event_line",
        "total_items", "total_turns", "items",
    }
    assert all(set(item) == {
        "item_ref", "task_type", "presentation_order", "turns",
    } for item in device_plan["items"])
    assert [item["item_ref"] for item in device_plan["items"]] == [
        f"itm-{idx:04d}" for idx in range(1, len(device_plan["items"]) + 1)
    ]
    assert all(set(turn) == {"turn_seq", "response_role"}
               for item in device_plan["items"] for turn in item["turns"])
    forbidden_plan_keys = {
        "target_word", "cues", "tell_answer", "success_line",
        "left_word", "right_word", "left_function_cue", "right_function_cue",
        "relation_cue", "operational_rubrics", "acceptable_expressions",
        "item_id", "scoring_key", "display",
        "image_id",
    }
    assert _all_keys(device_plan).isdisjoint(forbidden_plan_keys)

    # 老人端使用专用路由：它的最小投影不依赖 auth_kind，
    # 因此在本地开放模式也不会意外收到 canonical 答案计划。
    patient_plan = client.get(
        "/sessions/S-PRESENT/patient-plan", headers=capability)
    assert patient_plan.status_code == 200, patient_plan.text
    assert patient_plan.headers["cache-control"] == "private, no-store"
    assert patient_plan.json() == device_plan
    assert _all_keys(patient_plan.json()).isdisjoint(forbidden_plan_keys)
    assert open_patient_plan.json() == device_plan
    assert _all_keys(open_patient_plan.json()).isdisjoint(forbidden_plan_keys)

    response = client.get(
        "/sessions/S-PRESENT/patient-presentation", headers=capability)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    projected = response.json()
    assert set(projected) == {
        "schema_version", "mode", "session_id", "item_bank_version_id",
        "item_idx", "turn_idx", "item_ref", "turn_seq", "response_role",
        "cue_level", "cue_text", "feedback_key", "feedback_item_ref",
        "feedback_seq", "feedback_text", "wseq",
    }
    assert projected["mode"] == "task"
    assert projected["item_ref"] == "itm-0001"
    assert projected["feedback_item_ref"] == "itm-0001"
    assert projected["cue_text"] == first_item["display"]["cues"]["1"]
    assert projected["feedback_text"] == first_item["display"]["success_line"]
    assert _all_keys(projected).isdisjoint(forbidden_plan_keys)

    selector_attempt = client.get(
        "/sessions/S-PRESENT/patient-presentation?item_id=SE_guess",
        headers=capability,
    )
    assert selector_attempt.status_code == 422
    assert "不接受题目或脚本选择参数" in selector_attempt.json()["detail"]

    public_live = client.get("/live/state", headers=capability)
    assert public_live.status_code == 200
    assert "fbItemId" not in (public_live.json().get("cursor") or {})

    foreign = client.get(
        "/sessions/S-FOREIGN/patient-presentation", headers=capability)
    assert foreign.status_code == 409
    assert foreign.json()["detail"]["code"] == "device_session_mismatch"

    account = _account_client(client.test_engine)
    try:
        account_plan = account.get("/sessions/S-PRESENT/plan")
        assert account_plan.status_code == 200, account_plan.text
        account_display = account_plan.json()["items"][0]["display"]
        assert account_display["target_word"]
        assert account_display["cues"]["1"]
        assert account_display["tell_answer"]
        assert account_plan.json()["items"][0]["item_id"] == first_item["item_id"]
        assert account_plan.json()["items"][0]["turns"][0]["scoring_key"]

        # 显式 device bearer 必须压过同请求里已登录的账号 Cookie，
        # 否则共享浏览器会把 canonical 答案计划泄露给受试视图。
        explicit_device = account.get(
            "/sessions/S-PRESENT/plan", headers=capability)
        assert explicit_device.status_code == 200, explicit_device.text
        explicit_item = explicit_device.json()["items"][0]
        assert explicit_item["item_ref"] == "itm-0001"
        assert "item_id" not in explicit_item
        assert "scoring_key" not in explicit_device.text

        # 操作端切场后旧 token 只可做旧录音精确 ACK，不可继续读内容。
        _post_live_session(account, "S-FOREIGN", week_no=2)
        recovery_only = client.get(
            "/sessions/S-PRESENT/patient-presentation", headers=capability)
        assert recovery_only.status_code == 401
        assert recovery_only.json()["code"] == "device_capability_recovery_only"
    finally:
        account.close()


def test_rapport_device_gets_only_the_current_question(
        presentation_client, monkeypatch):
    client = presentation_client
    _create_patient(client, "P-RAPPORT-PRESENT")
    _create_session(
        client, session_id="S-RAPPORT-PRESENT",
        patient_id="P-RAPPORT-PRESENT", week_no=1)
    _post_live_session(client, "S-RAPPORT-PRESENT", week_no=1)
    written = client.put("/live/state", json={
        "kind": "rapportStep",
        "payload": {
            "sessionId": "S-RAPPORT-PRESENT",
            "sectionKey": "自我介绍",
            "questionIdx": 2,
            "recording": "idle",
            "containsDirectIdentifier": True,
        },
    })
    assert written.status_code == 200, written.text

    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    capability = _pair(client, "rapport-presentation-device-01")
    response = client.get(
        "/sessions/S-RAPPORT-PRESENT/patient-presentation",
        headers=capability,
    )
    assert response.status_code == 200, response.text
    projected = response.json()
    assert set(projected) == {
        "schema_version", "mode", "session_id", "script_version_id",
        "section_key", "question_idx", "speaker", "text", "wseq",
    }
    assert projected["mode"] == "rapport"
    assert projected["section_key"] == "自我介绍"
    assert projected["question_idx"] == 2
    assert projected["speaker"] == "机器人"
    assert projected["text"] == "您属什么呀？比如属鼠、属牛、属虎。"
    serialized = response.text
    assert "请问您叫什么名字" not in serialized
    assert "zodiac_closed_list" not in serialized
    assert "slots" not in serialized

    denied_script = client.get("/content/week1_script.json", headers=capability)
    assert denied_script.status_code == 404
    assert denied_script.json()["code"] == "resource_not_found"


def test_patient_presentation_maps_frozen_protocol_failure_to_503(
        presentation_client, monkeypatch, tmp_path):
    client = presentation_client
    _create_patient(client, "P-PRESENT-CONTENT-503")
    _create_session(
        client,
        session_id="S-PRESENT-CONTENT-503",
        patient_id="P-PRESENT-CONTENT-503",
        week_no=2,
    )
    _post_live_session(client, "S-PRESENT-CONTENT-503", week_no=2)
    plan = client.get("/sessions/S-PRESENT-CONTENT-503/plan").json()
    first = plan["items"][0]
    assert client.put("/live/state", json={
        "kind": "cursor",
        "payload": {
            "sessionId": "S-PRESENT-CONTENT-503",
            "screen": "present",
            "itemIdx": 0,
            "turnIdx": 0,
            "responseRole": first["turns"][0]["response_role"],
            "cueLevel": 1,
            "recording": "idle",
        },
    }).status_code == 200

    original_dir = content.CONTENT_DIR
    (tmp_path / "item_bank_v1.json").write_bytes(
        (original_dir / "item_bank_v1.json").read_bytes()
    )
    (tmp_path / "autopilot_protocol_v1.json").write_text(
        '{"protocol_version_id":', encoding="utf-8"
    )
    monkeypatch.setattr(content, "CONTENT_DIR", tmp_path)

    safe = TestClient(app, raise_server_exceptions=False)
    try:
        response = safe.get(
            "/sessions/S-PRESENT-CONTENT-503/patient-presentation")
    finally:
        safe.close()
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "frozen_content_unavailable"
    assert str(tmp_path) not in response.text
    assert "Traceback" not in response.text


def test_opaque_turn_ref_becomes_canonical_only_inside_server_ledgers(
        presentation_client, monkeypatch):
    client = presentation_client
    _create_patient(client, "P-OPAQUE")
    _create_session(client, session_id="S-OPAQUE", patient_id="P-OPAQUE", week_no=2)
    _post_live_session(client, "S-OPAQUE", week_no=2)
    account_plan = client.get("/sessions/S-OPAQUE/plan").json()
    canonical_item_id = account_plan["items"][0]["item_id"]
    canonical_turn_key = f"{canonical_item_id}#1"
    written = client.put("/live/state", json={
        "kind": "cursor",
        "payload": {
            "sessionId": "S-OPAQUE", "screen": "present",
            "itemIdx": 0, "turnIdx": 0, "responseRole": "命名",
            "cueLevel": 0, "recording": "idle",
        },
    })
    assert written.status_code == 200, written.text

    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    capability = _pair(client, "opaque-turn-device-0001")
    opaque_turn_key = "itm-0001#1"

    canonical_new = client.post("/audio", headers=capability, json={
        "raw_audio_id": "canonical-new-denied",
        "session_id": "S-OPAQUE",
        "turn_key": canonical_turn_key,
    })
    assert canonical_new.status_code == 422
    assert canonical_new.json()["detail"]["code"] == "device_opaque_turn_ref_required"

    future_position = client.post("/audio", headers=capability, json={
        "raw_audio_id": "future-position-denied",
        "session_id": "S-OPAQUE",
        "turn_key": "itm-0002#1",
    })
    assert future_position.status_code == 409
    assert "当前冻结位置" in future_position.json()["detail"]

    registered = client.post("/audio", headers=capability, json={
        "raw_audio_id": "opaque-current-audio",
        "session_id": "S-OPAQUE",
        "turn_key": opaque_turn_key,
    })
    assert registered.status_code == 200, registered.text
    assert registered.json() == {
        "raw_audio_id": "opaque-current-audio", "registered": True,
    }
    assert canonical_item_id not in registered.text

    canonical_probe = client.post("/audio", headers=capability, json={
        "raw_audio_id": "opaque-current-audio",
        "session_id": "S-OPAQUE",
        "turn_key": canonical_turn_key,
    })
    assert canonical_probe.status_code == 422
    assert canonical_probe.json()["detail"]["code"] == "device_opaque_turn_ref_required"

    rec = client.put("/live/state", headers=capability, json={
        "kind": "patientRec",
        "payload": {
            "active": True, "turnKey": opaque_turn_key,
            "sessionId": "S-OPAQUE",
        },
    })
    assert rec.status_code == 200, rec.text

    blob = b"\x1a\x45\xdf\xa3opaque-current-audio"
    uploaded = client.put(
        "/audio/opaque-current-audio/blob",
        headers={**capability, "content-type": "audio/webm"},
        content=blob,
    )
    assert uploaded.status_code == 200, uploaded.text
    upload_facts = uploaded.json()
    saved = client.put("/live/state", headers=capability, json={
        "kind": "audioSaved",
        "payload": {
            "rawAudioId": "opaque-current-audio",
            "durationSeconds": 1.25,
            "byteCount": upload_facts["bytes"],
            "checksum": upload_facts["checksum"],
            "turnKey": opaque_turn_key,
            "sessionId": "S-OPAQUE",
            "containsDirectIdentifier": False,
        },
    })
    assert saved.status_code == 200, saved.text

    with Session(client.test_engine) as session:
        asset = session.get(AudioAssetRow, "opaque-current-audio")
        assert asset is not None and asset.turn_key == canonical_turn_key
        receipt = session.get(AudioCaptureReceipt, saved.json()["audioReceipt"]["serverSeq"])
        assert receipt is not None and receipt.turn_key == canonical_turn_key
        live = session.get(LiveState, 1)
        assert json.loads(live.patient_rec_json)["turnKey"] == canonical_turn_key
        assert json.loads(live.audio_json)["turnKey"] == canonical_turn_key

        # 升级前已登记的 canonical outbox 只能做精确旧事实恢复。
        session.add(AudioAssetRow(
            raw_audio_id="legacy-registered-audio",
            session_id="S-OPAQUE",
            turn_key=canonical_turn_key,
            patient_turn_ref_version=1,
            is_simulation=True,
            data_classification="simulation",
        ))
        session.commit()

    legacy_ack = client.post("/audio", headers=capability, json={
        "raw_audio_id": "legacy-registered-audio",
        "session_id": "S-OPAQUE",
        "turn_key": canonical_turn_key,
    })
    assert legacy_ack.status_code == 200, legacy_ack.text
    assert legacy_ack.json() == {
        "raw_audio_id": "legacy-registered-audio", "registered": True,
    }
    assert canonical_item_id not in legacy_ack.text
