"""自动驾驶(M1-B)单测:协议内容闸 + 白名单模板展开 + 逐轮判类端点 + 游标反馈键流转。

红线覆盖:反馈只走"键+本地查表",游标契约仍不载文本;判类端点只读不建 turn;
协议话术缺失即校验报错(留空不兜底,不许静默哑掉)。
"""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app import content
from app.db import get_session
from app.main import app

BANK = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
WK = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
PROTO = content.load_autopilot_protocol(content.CONTENT_DIR / "autopilot_protocol_v1.json")

BANK_VERSION = BANK.version_id


@pytest.fixture
def client():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)

    def override():
        with Session(eng) as s:
            yield s

    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------- 协议内容闸 ----------------

def test_repo_protocol_passes_validation():
    assert content.validate_autopilot_protocol(PROTO) == []


def test_protocol_validation_catches_missing_lines():
    broken = {"protocol_version_id": "x", "silence_seconds": 8,
              "naming": {"success_after_cue1": {"unknown": "好"}},
              "double": {}}
    issues = content.validate_autopilot_protocol(broken)
    assert any("10 秒" in i for i in issues)
    assert any("close" in i for i in issues) and any("silence" in i for i in issues)
    assert any("success_after_cue2" in i for i in issues)
    assert any("namefix_left" in i for i in issues)


# ---------------- 白名单模板展开(红线:云端只见闭集具体句) ----------------

def test_allowlist_expands_protocol_templates_with_bank_words():
    allow = content.tts_allowlist(BANK, WK, PROTO)
    assert "很好，经过提醒您马上就想起来了，我们继续下一个。" in allow      # 无槽位句原样进
    assert "很好，您已经说出来了，这就是胡萝卜。我们继续下一个。" in allow  # 目标词展开
    assert "很好，您想起来了，这就是胡萝卜。我们继续下一个。" in allow
    assert "很好，经过提示您说出来了，这就是胡萝卜。我们继续下一个。" in allow
    d = BANK.double_element[0]
    assert f"这个我们刚刚见过，它叫{d['left_word']}。" in allow            # 双要素纠正句
    assert f"这个我们刚刚也见过，它叫{d['right_word']}。" in allow
    assert not any("【物品名】" in s for s in allow)                        # 模板原文绝不进


# ---------------- 逐轮判类端点(只读,不建 turn) ----------------

def test_judge_classify_correct_answer(client):
    exact = client.post("/judge/classify", json={"item_id": "SE_胡萝卜", "response_role": "命名",
                                                 "text": "胡萝卜"}).json()
    assert exact["answer_type"] == "正确" and exact["judge_mode"] == "规则确定式"
    assert exact["contains_target"] is True
    # 含完整目标词的长句:判"部分正确"但 contains_target 真 → 自动驾驶算说对(表扬)
    contains = client.post("/judge/classify", json={"item_id": "SE_胡萝卜", "response_role": "命名",
                                                    "text": "这是胡萝卜"}).json()
    assert contains["answer_type"] == "部分正确" and contains["contains_target"] is True
    # 只说目标词子串"萝卜":部分正确但不含全词 → contains_target 假 → 按不准确升级,不当成功
    subword = client.post("/judge/classify", json={"item_id": "SE_胡萝卜", "response_role": "命名",
                                                   "text": "萝卜"}).json()
    assert subword["answer_type"] == "部分正确" and subword["contains_target"] is False


def test_judge_classify_open_role_has_no_deterministic_rubric(client):
    d = BANK.double_element[0]
    r = client.post("/judge/classify", json={"item_id": d["item_id"], "response_role": "关系识别",
                                             "text": "随便说说"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer_type"] is None and body["needs_review"] is True


def test_judge_classify_unknown_item_404_and_extra_field_422(client):
    assert client.post("/judge/classify", json={"item_id": "SE_不存在", "text": "x"}).status_code == 404
    r = client.post("/judge/classify", json={"item_id": "SE_胡萝卜", "text": "x",
                                             "patient_name": "张三"})
    assert r.status_code == 422                       # extra=forbid:画像/患者字段结构性进不来


def test_judge_classify_writes_nothing(client):
    _seed(client)
    client.post("/judge/classify", json={"item_id": "SE_胡萝卜", "text": "胡萝卜"})
    journal = client.get("/sessions/S-AP-1/journal").json()
    assert journal["items"] == []                     # 判类不建 item/turn


# ---------------- 游标反馈键(不载文本,老人端免 PIN 读口可见) ----------------

def _seed(client):
    assert client.post("/patients", json={"patient_id": "P-AP", "recording_allowed": True}).status_code == 200
    assert client.post("/sessions", json={
        "session_id": "S-AP-1", "patient_id": "P-AP", "week_no": 2,
        "phase_type": "正式训练", "event_line": "正式训练",
        "item_bank_version_id": BANK_VERSION,
    }).status_code == 200
    assert client.put("/live/state", json={"kind": "session", "payload": {
        "sessionId": "S-AP-1", "weekNo": 2, "eventLine": "正式训练",
        "mode": "task", "itemBankVersionId": BANK_VERSION, "wseq": 10,
    }}).status_code == 200


def test_cursor_fb_fields_roundtrip_to_patient(client):
    _seed(client)
    r = client.put("/live/state", json={"kind": "cursor", "payload": {
        "sessionId": "S-AP-1", "screen": "present", "itemIdx": 1, "turnIdx": 0,
        "responseRole": "命名", "cueLevel": 0, "recording": "idle",
        "fbKey": "cued2", "fbItemId": "SE_胡萝卜", "fbSeq": 3, "wseq": 11,
    }})
    assert r.status_code == 200, r.text
    cur = client.get("/live/state").json()["cursor"]
    assert cur["fbKey"] == "cued2" and cur["fbItemId"] == "SE_胡萝卜" and cur["fbSeq"] == 3


def test_cursor_rejects_unknown_fb_key_and_free_text(client):
    _seed(client)
    bad_key = client.put("/live/state", json={"kind": "cursor", "payload": {
        "sessionId": "S-AP-1", "screen": "present", "itemIdx": 0, "turnIdx": 0,
        "responseRole": "命名", "cueLevel": 0, "recording": "idle",
        "fbKey": "自由文本反馈", "wseq": 12,
    }})
    assert bad_key.status_code == 422                 # 键是枚举:游标永远载不了话术文本
    free_text = client.put("/live/state", json={"kind": "cursor", "payload": {
        "sessionId": "S-AP-1", "screen": "present", "itemIdx": 0, "turnIdx": 0,
        "responseRole": "命名", "cueLevel": 0, "recording": "idle",
        "fbText": "你答对了", "wseq": 13,
    }})
    assert free_text.status_code == 422               # extra=forbid
