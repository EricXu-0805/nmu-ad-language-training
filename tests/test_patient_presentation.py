"""受试者设备只能获得当前游标的最小内容投影。"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

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
        "section_key", "question_idx", "beat", "speaker", "text", "wseq",
    }
    assert projected["mode"] == "rapport"
    assert projected["section_key"] == "自我介绍"
    assert projected["question_idx"] == 2
    assert projected["beat"] == "ask"
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


def _write_rapport(client, session_id, *, section_key, question_idx, beat):
    return client.put("/live/state", json={
        "kind": "rapportStep",
        "payload": {
            "sessionId": session_id,
            "sectionKey": section_key,
            "questionIdx": question_idx,
            "beat": beat,
            "recording": "idle",
            "containsDirectIdentifier": section_key == "自我介绍",
        },
    })


def test_elder_hears_the_scripted_reply_after_answering(
        presentation_client, monkeypatch):
    """老人答完必须听见机器人的回应；今天这句只由研究者当面代说，屏上一片安静。"""
    client = presentation_client
    _create_patient(client, "P-RAPPORT-REPLY")
    _create_session(
        client, session_id="S-RAPPORT-REPLY",
        patient_id="P-RAPPORT-REPLY", week_no=1)
    _post_live_session(client, "S-RAPPORT-REPLY", week_no=1)
    written = _write_rapport(
        client, "S-RAPPORT-REPLY",
        section_key="自我介绍", question_idx=0, beat="reply")
    assert written.status_code == 200, written.text

    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    capability = _pair(client, "rapport-reply-device-01")
    projected = client.get(
        "/sessions/S-RAPPORT-REPLY/patient-presentation",
        headers=capability,
    ).json()
    assert projected["beat"] == "reply"
    assert projected["speaker"] == "机器人"
    assert projected["text"] == "好的，认识您真开心。"
    # 问句不能顺带泄给同一拍：老人这一刻只该听见回应。
    assert "请问您叫什么名字" not in json.dumps(projected, ensure_ascii=False)


def test_a_reply_the_frozen_script_never_wrote_is_refused_not_invented(
        presentation_client):
    """介绍机构环境那四问脚本没写回应句——工程不得代拟一句读给老人听。"""
    client = presentation_client
    _create_patient(client, "P-RAPPORT-NOREPLY")
    _create_session(
        client, session_id="S-RAPPORT-NOREPLY",
        patient_id="P-RAPPORT-NOREPLY", week_no=1)
    _post_live_session(client, "S-RAPPORT-NOREPLY", week_no=1)
    refused = _write_rapport(
        client, "S-RAPPORT-NOREPLY",
        section_key="介绍机构环境", question_idx=0, beat="reply")
    assert refused.status_code == 422, refused.text
    assert "回应句" in refused.json()["detail"]

    accepted = _write_rapport(
        client, "S-RAPPORT-NOREPLY",
        section_key="介绍机构环境", question_idx=0, beat="ask")
    assert accepted.status_code == 200, accepted.text


def test_every_scripted_reply_line_can_be_spoken_by_the_cloud_voice():
    """回应句必须在云 TTS 白名单里，否则老人听见的是另一把嗓子（本地降级引擎）。"""
    script = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
    # 第1周没有题库（关系建立不判分）；白名单在生产里是各周并集，取任一结构化周即可。
    bank = content.load_item_bank_for_week(2)
    allowed = content.tts_allowlist(bank, script)
    spoken = [
        patient_presentation.rapport_reply_line(script, section["key"], idx)
        for section in script["sections"]
        if section.get("speaker") == "机器人"
        for idx, question in enumerate(section.get("questions") or [])
        if question.get("success")
    ]
    assert spoken, "冻结脚本一句回应都没写"
    assert [line for line in spoken if line not in allowed] == []
    # 带槽位的三句改说脚本自己写的备用句；占位符绝不能被念出来。
    assert all("【" not in line for line in spoken), spoken
    assert spoken[0] == "好的，认识您真开心。"
    assert spoken[2] == "好的，谢谢您告诉我。"


def _reply_bank():
    return content.load_week1_reply_bank(
        content.CONTENT_DIR / "week1_reply_bank_v1.json")


def test_open_reply_bank_never_applies_to_the_name_and_age_questions():
    """姓名/年龄那两问的回答是直接身份信息——不进选句这条路。

    属相/兴趣/活动(q2-4)自 2026-09-04 起进云(收据 251),排除名单只剩这两问。
    """
    bank = _reply_bank()
    for idx in (0, 1):
        assert not patient_presentation.rapport_reply_allowed_here(
            bank, "自我介绍", idx)
    for idx in (2, 3, 4):
        assert patient_presentation.rapport_reply_allowed_here(bank, "自我介绍", idx)
    assert patient_presentation.rapport_reply_allowed_here(bank, "介绍机构环境", 0)
    excluded = {(row["section_key"], row["question_idx"]) for row in bank["excluded"]}
    assert excluded == {("自我介绍", 0), ("自我介绍", 1)}
    caps = {(row["section_key"], row["question_idx"]): row.get("max_rounds")
            for row in bank["applies_to"]}
    # 属相一问单轮(2026-09-05 Eric 拍板);其余问位不设问位上限。
    assert caps[("自我介绍", 2)] == 1
    assert all(cap is None for key, cap in caps.items() if key != ("自我介绍", 2))


def test_every_open_reply_can_be_spoken_by_the_cloud_voice():
    """回应库不进白名单，老人听见的会是平板系统语音——同一场对话两把嗓子。"""
    bank = _reply_bank()
    allowed = content.tts_allowlist(
        content.load_item_bank_for_week(2),
        content.load_week1_script(content.CONTENT_DIR / "week1_script.json"),
        week1_reply_bank=bank,
    )
    missing = [row["text"] for row in bank["replies"] if row["text"] not in allowed]
    assert missing == [], missing
    assert bank["fallback"] in allowed


def test_a_reply_id_from_another_question_is_refused(
        presentation_client, monkeypatch):
    """回应句编号必须绑当前一问：姓名那一问不许借开放回应库说话。"""
    client = presentation_client
    _create_patient(client, "P-RAPPORT-BANK")
    _create_session(
        client, session_id="S-RAPPORT-BANK",
        patient_id="P-RAPPORT-BANK", week_no=1)
    _post_live_session(client, "S-RAPPORT-BANK", week_no=1)

    def write(section_key, question_idx, reply_id, beat="reply"):
        return client.put("/live/state", json={
            "kind": "rapportStep",
            "payload": {
                "sessionId": "S-RAPPORT-BANK", "sectionKey": section_key,
                "questionIdx": question_idx, "beat": beat, "replyId": reply_id,
                "recording": "idle",
                "containsDirectIdentifier": section_key == "自我介绍",
            },
        })

    refused = write("自我介绍", 0, "a1")
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == "当前一问不接受这条回应句"
    assert write("自我介绍", 3, "zzz").status_code == 422
    assert write("自我介绍", 3, "a1", beat="ask").status_code == 422

    accepted = write("自我介绍", 3, "a1")
    assert accepted.status_code == 200, accepted.text

    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    capability = _pair(client, "rapport-bank-device-01")
    projected = client.get(
        "/sessions/S-RAPPORT-BANK/patient-presentation", headers=capability).json()
    assert projected["beat"] == "reply"
    assert projected["text"] == "这个挺好的，您再多讲一点吧。"
    # 编号是内部键，不该跟着投影下去让配对设备去枚举整个回应库。
    assert "replyId" not in projected and "a1" not in json.dumps(projected)


_ROUNDS_TS = Path(__file__).resolve().parents[1] / "web" / "src" / "console" / "relationship" / "rapportRounds.ts"


def test_identity_slot_fields_match_between_console_and_server():
    """哪几问是直接身份信息,前端(rapportRounds.ts)与服务端各钉一份;两份不同步时
    前端会把不该进云的问位当成可自动回应、或把能进云的问位整段标成含标识。"""
    import re
    source = _ROUNDS_TS.read_text(encoding="utf-8")
    match = re.search(r"IDENTITY_SLOT_FIELDS[^=]*=\s*new Set\(\[([^\]]*)\]\)", source)
    assert match, "rapportRounds.ts 里找不到 IDENTITY_SLOT_FIELDS 字面量"
    frontend = frozenset(re.findall(r'"([^"]+)"', match.group(1)))
    assert frontend == patient_presentation.RAPPORT_IDENTITY_SLOT_FIELDS


def _rapport_step(client: TestClient, session_id: str, *, section: str, qidx: int,
                  recording: str = "idle") -> None:
    response = client.put("/live/state", json={"kind": "rapportStep", "payload": {
        "sessionId": session_id, "sectionKey": section, "questionIdx": qidx,
        "recording": recording, "recSeq": 1,
        "containsDirectIdentifier": section == "自我介绍" and qidx in (0, 1),
    }})
    assert response.status_code == 200, response.text


def test_week1_device_registration_keeps_the_arm_time_question_after_advance(
        presentation_client, monkeypatch):
    """老人还在说、研究者已点到下一问:设备用开麦那一刻锁存的问位登记,服务端指针
    已经在下一问也要收下(改成按问之前就是按节比对,这里不能收紧);换节仍拒。
    旧版页面(节级键)登记也走同一道按节比对,部署后没刷新的老人端不会卡死。"""
    client = presentation_client
    _create_patient(client, "P-LATE")
    _create_session(client, session_id="S-LATE", patient_id="P-LATE", week_no=1)
    _post_live_session(client, "S-LATE", week_no=1)
    _rapport_step(client, "S-LATE", section="自我介绍", qidx=1)
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    capability = _pair(client, "late-register-device-0001")

    late = client.post("/audio", headers=capability, json={
        "raw_audio_id": "late-q0", "session_id": "S-LATE",
        "turn_key": "关系建立·自我介绍#0", "contains_direct_identifier": True,
    })
    assert late.status_code == 200, late.text
    assert late.json() == {"raw_audio_id": "late-q0", "registered": True}

    other_section = client.post("/audio", headers=capability, json={
        "raw_audio_id": "wrong-section", "session_id": "S-LATE",
        "turn_key": "关系建立·介绍机构环境#0",
    })
    assert other_section.status_code == 409, other_section.text
    assert "当前冻结位置" in other_section.json()["detail"]

    legacy = client.post("/audio", headers=capability, json={
        "raw_audio_id": "legacy-bundle", "session_id": "S-LATE",
        "turn_key": "关系建立·自我介绍", "contains_direct_identifier": True,
    })
    assert legacy.status_code == 200, legacy.text
    with Session(client.test_engine) as s:
        keys = {a.raw_audio_id: a.turn_key for a in s.exec(select(AudioAssetRow).where(
            AudioAssetRow.session_id == "S-LATE"))}
    assert keys == {"late-q0": "关系建立·自我介绍#0", "legacy-bundle": "关系建立·自我介绍"}


def test_week1_microphone_failure_from_the_tablet_pauses_the_session(
        presentation_client, monkeypatch):
    """第1周老人端麦克风失败上报必须落账并暂停场次。此前这条路把「节#问」喂给自动
    带练围栏(要求题号与环节同有同无)一律 422:故障不落账、场次不暂停(收据 251 复核坐实)。
    迟到一问的上报(设备锁存的问位比服务端指针早一问)同样收下。"""
    from app.models import InteractionEvent, SessionRuntimeState
    client = presentation_client
    _create_patient(client, "P-MIC")
    _create_session(client, session_id="S-MIC", patient_id="P-MIC", week_no=1)
    _post_live_session(client, "S-MIC", week_no=1)
    _rapport_step(client, "S-MIC", section="自我介绍", qidx=3, recording="armed")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    capability = _pair(client, "mic-failure-device-0001")

    failure = client.put("/live/state", headers=capability, json={
        "kind": "patientRec", "payload": {
            "active": False, "turnKey": "关系建立·自我介绍#2", "sessionId": "S-MIC",
            "failureCode": "microphone_permission_denied",
            "failureId": "123e4567-e89b-42d3-a456-426614174000",
        }})
    assert failure.status_code == 200, failure.text
    with Session(client.test_engine) as s:
        state = s.get(SessionRuntimeState, "S-MIC")
        assert state is not None and state.status == "paused"
        events = [e for e in s.exec(select(InteractionEvent).where(
            InteractionEvent.session_id == "S-MIC"))
            if e.event_type == "technical_pause"]
    assert [(e.item_id, e.turn_seq) for e in events] == [("关系建立·自我介绍#2", None)]

    foreign = client.put("/live/state", headers=capability, json={
        "kind": "patientRec", "payload": {
            "active": False, "turnKey": "关系建立·介绍机构环境#0", "sessionId": "S-MIC",
            "failureCode": "microphone_permission_denied",
            "failureId": "223e4567-e89b-42d3-a456-426614174000",
        }})
    assert foreign.status_code == 409, foreign.text
