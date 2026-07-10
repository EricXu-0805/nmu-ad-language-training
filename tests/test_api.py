import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  register tables
from app.db import get_session
from app.main import app


@pytest.fixture
def client():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)

    def override():
        with Session(eng) as s:
            yield s

    app.dependency_overrides[get_session] = override
    yield TestClient(app)            # 不用 with：不触发 lifespan，避免动到文件库
    app.dependency_overrides.clear()


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_patient_with_consent_fields(client):
    body = {"patient_id": "P001", "dementia_severity": "轻度", "mandarin_eligible": True,
            "consent_type": "本人同意", "recording_allowed": True}
    r = client.post("/patients", json=body)
    assert r.status_code == 200, r.text
    assert client.get("/patients/P001").json()["consent_type"] == "本人同意"
    assert client.post("/patients", json=body).status_code == 409  # 重复


def test_session_needs_patient_and_version(client):
    sess = {"session_id": "S1", "patient_id": "P404", "week_no": 2,
            "phase_type": "正式训练", "event_line": "正式训练", "item_bank_version_id": "wk2-v1-20260707"}
    assert client.post("/sessions", json=sess).status_code == 404  # 患者不存在
    client.post("/patients", json={"patient_id": "P404"})
    assert client.post("/sessions", json=sess).status_code == 200


def test_item_bank_endpoint(client):
    d = client.get("/content/item-bank").json()
    assert d["single_count"] == 20 and d["double_count"] == 10
    assert d["errors"] == []
    assert any("SE_花" in w for w in d["warnings"])


def test_score_double_all_correct_is_100(client):
    items = [{"item_id": "d1", "left_name": 1, "left_function": 1,
              "right_name": 1, "right_function": 1, "relation": 1}]
    d = client.post("/score/double", json=items).json()
    assert d["weekly_de_score_percentile"] == 100.0


def test_score_single_validation_error(client):
    bad = [{"item_id": "x", "final_correct": 1, "spontaneous_correct": 1, "prompt_level": 2}]
    assert client.post("/score/single", json=bad).status_code == 422  # 自发正确却有提示


def test_judge_rejects_portrait_at_boundary(client):
    r = client.post("/judge/build-input", json={
        "item_id": "d1", "task_type": "单要素", "target_word": "锚", "zodiac": "属牛"})
    assert r.status_code == 400
    assert "画像" in r.json()["detail"]


def test_judge_valid_prefers_confirmed(client):
    r = client.post("/judge/build-input", json={
        "item_id": "d1", "task_type": "单要素", "target_word": "胡萝卜",
        "asr_text": "红萝波", "confirmed_response_text": "红萝卜"})
    assert r.status_code == 200
    assert r.json()["resolved_text"] == "红萝卜"


def test_audio_gate_blocks_premature_delete(client):
    client.post("/audio", json={"raw_audio_id": "a1"})
    assert client.delete("/audio/a1").status_code == 409          # 未导出不能删
    client.put("/audio/a1/blob", content=b"fake-audio-bytes")     # 字节落库+登记 sha256
    client.post("/audio/a1/export")
    assert client.delete("/audio/a1").status_code == 409          # 未校验不能删
    assert client.post("/audio/a1/checksum").status_code == 200   # 真校验通过
    r = client.delete("/audio/a1")
    assert r.status_code == 200 and r.json()["bytes_deleted"] is True  # 放行才物理删字节
    assert client.get("/audio/a1/blob").status_code == 404        # 字节确已删除


def test_audio_checksum_requires_real_bytes(client):
    client.post("/audio", json={"raw_audio_id": "nb"})
    client.post("/audio/nb/export")
    assert client.post("/audio/nb/checksum").status_code == 409   # 无字节禁止盲翻状态


def test_audio_blob_roundtrip_and_asr_degraded(client):
    client.post("/audio", json={"raw_audio_id": "rt"})
    up = client.put("/audio/rt/blob", content=b"\x1aEwebm-ish",
                    headers={"content-type": "audio/webm"}).json()
    assert up["format"] == "webm" and len(up["checksum"]) == 64
    assert client.get("/audio/rt/blob").content == b"\x1aEwebm-ish"
    # Null 引擎降级:asr_text=None,人工转写路径不断
    tr = client.post("/asr/transcribe/rt").json()
    assert tr["degraded"] is True and tr["asr_text"] is None
    assert client.post("/asr/transcribe/none-x").status_code == 404


def test_asr_hotwords_from_frozen_bank(client):
    d = client.get("/asr/hotwords").json()
    assert d["engine"] == "null-0"
    assert "胡萝卜" in d["hotwords"] and "斧子" in d["hotwords"] and "树" in d["hotwords"]
    assert "属牛" in d["hotwords"] or "牛" in d["hotwords"]       # 属相闭表并入


def test_audio_reliability_needs_review_before_delete(client):
    client.post("/audio", json={"raw_audio_id": "rel", "is_reliability_sample": True})
    client.put("/audio/rel/blob", content=b"rel-bytes")
    client.post("/audio/rel/export")
    client.post("/audio/rel/checksum")
    assert client.delete("/audio/rel").status_code == 409         # 信度样本须先人工复核
    client.post("/audio/rel/reliability-review")
    assert client.delete("/audio/rel").status_code == 200


def _mk_session(client, sid="SM0", pid="PM0"):
    client.post("/patients", json={"patient_id": pid, "mandarin_eligible": True})
    client.post("/sessions", json={"session_id": sid, "patient_id": pid, "week_no": 2,
                                   "phase_type": "正式训练", "event_line": "正式训练",
                                   "item_bank_version_id": "wk2-v1-20260707"})


def test_session_plan_expands_turns(client):
    _mk_session(client)
    d = client.get("/sessions/SM0/plan", params={"week_no": 2, "event_line": "正式训练"}).json()
    assert d["total_items"] == 30 and d["total_turns"] == 20 + 10 * 5


def test_m0_end_to_end_single_item(client):
    """建档→建场次→建题→录环节→改写→AI初评→人工锁分→重建评分→去标识导出。"""
    _mk_session(client)
    ie = client.post("/sessions/SM0/items",
                     json={"item_id": "SE_锚", "task_type": "单要素"}).json()
    te = client.post(f"/items/{ie['id']}/turns",
                     json={"turn_seq": 1, "response_role": "命名",
                           "asr_text": "毛", "prompt_level": 0}).json()
    # 人工改写为 confirmed（不覆盖 asr_text 原文）
    c = client.patch(f"/turns/{te['id']}/confirm", json={"confirmed_response_text": "锚"}).json()
    assert c["asr_text"] == "毛" and c["confirmed_response_text"] == "锚"
    # 规则 AI 初评：取文优先 confirmed → 命中 target → 正确
    j = client.post(f"/turns/{te['id']}/ai-judge").json()
    assert j["ai_answer_type"] == "正确" and j["ai_needs_review"] is False
    # 人工锁分
    lk = client.patch(f"/turns/{te['id']}/lock",
                      json={"reviewer_id": "R1", "element_value": 1, "prompt_level": 0}).json()
    assert lk["score_locked"] is True
    assert client.patch(f"/turns/{te['id']}/lock",
                        json={"reviewer_id": "R1", "element_value": 0}).status_code == 409  # 不可重复锁
    # 锁后不得改 confirmed
    assert client.patch(f"/turns/{te['id']}/confirm",
                        json={"confirmed_response_text": "别的"}).status_code == 409
    # 重建评分
    sc = client.get("/sessions/SM0/scores").json()
    assert sc["single"]["naming_accuracy"] == 1.0
    # 去标识导出：不带直接标识
    ex = client.post("/sessions/SM0/export").json()
    assert ex["deidentified"] is True and ex["sheet_counts"]["turns"] == 1


def test_ai_judge_no_deterministic_target_is_human_only(client):
    _mk_session(client)
    ie = client.post("/sessions/SM0/items",
                     json={"item_id": "DE_斧子+树", "task_type": "双要素"}).json()
    te = client.post(f"/items/{ie['id']}/turns",
                     json={"turn_seq": 5, "response_role": "关系识别", "asr_text": "用斧子砍树"}).json()
    j = client.post(f"/turns/{te['id']}/ai-judge").json()
    assert j["ai_answer_type"] is None and j["ai_needs_review"] is True   # 关系无确定式口径→纯人工


def test_scale_entry_list_and_export_integration(client):
    client.post("/patients", json={"patient_id": "PS1"})
    r = client.post("/patients/PS1/scales",
                    json={"phase_type": "前测", "scale_name": "CETI", "score": 42.0, "assessor_id": "A1"})
    assert r.status_code == 200 and r.json()["scale_name"] == "CETI"
    lst = client.get("/patients/PS1/scales").json()
    assert len(lst) == 1 and lst[0]["score"] == 42.0
    # 未建档患者 → 404
    assert client.post("/patients/P404x/scales",
                       json={"phase_type": "前测", "scale_name": "CETI"}).status_code == 404
    # 录入的量表进导出(scales 表),且去标识通道不带 assessor_id
    client.post("/sessions", json={"session_id": "SPS1", "patient_id": "PS1", "week_no": 2,
                                   "phase_type": "正式训练", "event_line": "正式训练",
                                   "item_bank_version_id": "wk2-v1-20260707"})
    ex = client.post("/sessions/SPS1/export").json()
    assert ex["sheet_counts"]["scales"] == 1


def test_live_state_roundtrip_and_session_reset(client):
    assert client.get("/live/state").json()["seq"] == 0            # 初始为空
    client.put("/live/state", json={"kind": "session",
                                    "payload": {"sessionId": "S1", "weekNo": 2, "mode": "task"}})
    client.put("/live/state", json={"kind": "cursor",
                                    "payload": {"itemIdx": 3, "turnIdx": 1, "recording": "armed"}})
    client.put("/live/state", json={"kind": "audioSaved",
                                    "payload": {"rawAudioId": "a9", "turnKey": "SE_锚#1"}})
    d = client.get("/live/state").json()
    assert d["seq"] == 3 and d["cursor"]["itemIdx"] == 3 and d["audioSaved"]["rawAudioId"] == "a9"
    # 新场次握手 → 旧游标/录音回报清空(防老人端串场)
    client.put("/live/state", json={"kind": "session", "payload": {"sessionId": "S2", "weekNo": 1}})
    d2 = client.get("/live/state").json()
    assert d2["session"]["sessionId"] == "S2" and d2["cursor"] is None and d2["audioSaved"] is None
    assert d2["seq"] == 4                                           # seq 只增不回卷
    assert client.put("/live/state", json={"kind": "bogus", "payload": {}}).status_code == 422


def test_phase_aware_cue_intervention_flagged(client):
    _mk_session(client)
    ev = client.post("/sessions/SM0/abnormal",
                     json={"intervention_type": "代说物品名"}).json()
    # 正式训练周代说物品名 → 线索性介入 + 影响判分有效性
    assert ev["abnormal_type"] == "线索性介入" and ev["affects_scoring_validity"] is True
