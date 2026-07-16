import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import audio_store, models  # noqa: F401  register tables
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
    # 重复建同 id 场次 → 干净 409(曾是主键冲突 500),前端凭它走"取回续做"
    assert client.post("/sessions", json=sess).status_code == 409
    got = client.get("/sessions/S1")
    assert got.status_code == 200 and got.json()["patient_id"] == "P404"
    assert client.get("/sessions/S404").status_code == 404
    bad_context = {**sess, "session_id": "SCTX", "phase_type": "关系建立",
                   "event_line": "关系建立环节"}
    assert client.post("/sessions", json=bad_context).status_code == 422
    bad_version = {**sess, "session_id": "SVER", "item_bank_version_id": "unknown"}
    assert client.post("/sessions", json=bad_version).status_code == 409


def test_item_bank_endpoint(client):
    d = client.get("/content/item-bank").json()
    assert d["single_count"] == 20 and d["double_count"] == 10
    assert d["multi_count"] == 0 and d["supported_training_weeks"] == [2]
    assert d["qc_status"] == "draft" and d["ready_for_research"] is False
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


def test_audio_gate_blocks_premature_delete(client, monkeypatch):
    _mk_session(client, sid="SA1", pid="PA1")
    client.post("/audio", json={"raw_audio_id": "a1", "session_id": "SA1"})
    # ★环境开关先放行:端点把 ENABLE_AUDIO_DELETE 检查放在闸门之前,若开关关着,
    # 下面每个 409 都来自"物理删除默认禁用"而非闸门——闸门断言全部变成空断言,
    # 端点绕过 request_delete 的回归将测不出来。
    monkeypatch.setenv("ENABLE_AUDIO_DELETE", "1")
    r = client.delete("/audio/a1")
    assert r.status_code == 409 and "导出" in r.json()["detail"]  # 未导出不能删(真闸门拒绝)
    client.put("/audio/a1/blob", content=b"fake-audio-bytes")     # 字节落库+登记 sha256
    assert client.post("/audio/a1/export").status_code == 409      # 禁止脱离场次盲翻状态
    ex = client.post("/sessions/SA1/export").json()
    assert ex["audio_touched"] == ["a1"]
    assert client.put("/audio/a1/blob", content=b"overwrite").status_code == 409
    r = client.delete("/audio/a1")
    assert r.status_code == 409 and "校验" in r.json()["detail"]  # 未校验不能删(真闸门拒绝)
    audio_store.find_blob("a1").write_bytes(b"source-was-changed") # 校验不得回头拿采集源文件冒充
    assert client.post("/audio/a1/checksum").status_code == 200   # 真校验通过
    # 闸门全绿但环境开关关闭:仍默认禁删——独立于闸门的第二道保险
    monkeypatch.delenv("ENABLE_AUDIO_DELETE")
    r = client.delete("/audio/a1")
    assert r.status_code == 409 and "默认禁用" in r.json()["detail"]
    monkeypatch.setenv("ENABLE_AUDIO_DELETE", "1")
    r = client.delete("/audio/a1")
    assert r.status_code == 200 and r.json()["bytes_deleted"] is True  # 放行才物理删字节
    assert client.get("/audio/a1/blob").status_code == 404        # 字节确已删除


def test_audio_checksum_requires_real_bytes(client):
    _mk_session(client, sid="SNB", pid="PNB")
    client.post("/audio", json={"raw_audio_id": "nb", "session_id": "SNB"})
    client.post("/sessions/SNB/export")
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


def test_recording_explicitly_denied_blocks_audio_registration(client):
    client.post("/patients", json={"patient_id": "PNOREC", "recording_allowed": False})
    client.post("/sessions", json={"session_id": "SNOREC", "patient_id": "PNOREC", "week_no": 2,
                                   "phase_type": "正式训练", "event_line": "正式训练",
                                   "item_bank_version_id": "wk2-v1-20260707"})
    denied = client.post("/audio", json={"raw_audio_id": "no-rec", "session_id": "SNOREC"})
    assert denied.status_code == 409
    assert "recording_allowed=false" in denied.json()["detail"]


def test_asr_hotwords_from_frozen_bank(client):
    d = client.get("/asr/hotwords").json()
    assert d["engine"] == "null-0"
    assert "胡萝卜" in d["hotwords"] and "斧子" in d["hotwords"] and "树" in d["hotwords"]
    assert "属牛" in d["hotwords"] or "牛" in d["hotwords"]       # 属相闭表并入


def test_audio_reliability_needs_review_before_delete(client, monkeypatch):
    _mk_session(client, sid="SREL", pid="PREL")
    client.post("/audio", json={"raw_audio_id": "rel", "session_id": "SREL",
                                "is_reliability_sample": True})
    client.put("/audio/rel/blob", content=b"rel-bytes")
    client.post("/sessions/SREL/export")
    client.post("/audio/rel/checksum")
    monkeypatch.setenv("ENABLE_AUDIO_DELETE", "1")  # 先放行开关,让 409 真正来自信度闸门
    r = client.delete("/audio/rel")
    assert r.status_code == 409 and "信度" in r.json()["detail"]  # 信度样本须先人工复核
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


def test_session_plan_uses_persisted_context_and_fails_closed(client):
    _mk_session(client, sid="SP2", pid="PP2")
    assert client.get("/sessions/SP2/plan").status_code == 200
    assert client.get("/sessions/SP2/plan", params={"week_no": 3}).status_code == 409
    assert client.get("/sessions/SP2/plan", params={"event_line": "基线测评窗"}).status_code == 409

    blocked = client.post("/sessions", json={"session_id": "SP3", "patient_id": "PP2", "week_no": 3,
                                             "phase_type": "正式训练", "event_line": "正式训练",
                                             "item_bank_version_id": "wk2-v1-20260707"})
    assert blocked.status_code == 409

    client.post("/sessions", json={"session_id": "SP1", "patient_id": "PP2", "week_no": 1,
                                   "phase_type": "关系建立", "event_line": "关系建立环节",
                                   "item_bank_version_id": "wk2-v1-20260707"})
    p1 = client.get("/sessions/SP1/plan").json()
    assert p1["total_items"] == 0 and p1["total_turns"] == 0


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


def test_lock_requires_confirmed_formal_valid_context(client):
    _mk_session(client, sid="SLK", pid="PLK")
    ie = client.post("/sessions/SLK/items",
                     json={"item_id": "SE_锚", "task_type": "单要素"}).json()
    te = client.post(f"/items/{ie['id']}/turns",
                     json={"turn_seq": 1, "response_role": "命名", "prompt_level": 0}).json()
    endpoint = f"/turns/{te['id']}/lock"
    assert client.patch(endpoint, json={"reviewer_id": "R1", "element_value": 1}).status_code == 409
    client.patch(f"/turns/{te['id']}/confirm", json={"confirmed_response_text": "锚"})
    assert client.patch(endpoint, json={"reviewer_id": " ", "element_value": 1}).status_code == 422
    assert client.patch(endpoint, json={"reviewer_id": "R1", "element_value": 2}).status_code == 422
    assert client.patch(endpoint, json={"reviewer_id": "R1", "element_value": 1,
                                        "prompt_level": 4}).status_code == 422

    bad_role = client.post(f"/items/{ie['id']}/turns",
                           json={"turn_seq": 2, "response_role": "命名", "prompt_level": 0}).json()
    client.patch(f"/turns/{bad_role['id']}/confirm", json={"confirmed_response_text": "锚"})
    assert client.patch(f"/turns/{bad_role['id']}/lock",
                        json={"reviewer_id": "R1", "element_value": 1}).status_code == 422

    client.post("/sessions", json={"session_id": "SLK1", "patient_id": "PLK", "week_no": 1,
                                   "phase_type": "关系建立", "event_line": "关系建立环节",
                                   "item_bank_version_id": "wk2-v1-20260707"})
    w1_item = client.post("/sessions/SLK1/items",
                          json={"item_id": "SE_锚", "task_type": "单要素"}).json()
    w1_turn = client.post(f"/items/{w1_item['id']}/turns",
                          json={"turn_seq": 1, "response_role": "命名", "prompt_level": 0}).json()
    client.patch(f"/turns/{w1_turn['id']}/confirm", json={"confirmed_response_text": "锚"})
    assert client.patch(f"/turns/{w1_turn['id']}/lock",
                        json={"reviewer_id": "R1", "element_value": 1}).status_code == 409


def test_read_only_session_recovery_endpoints(client):
    _mk_session(client, sid="SREC", pid="PREC")
    ie = client.post("/sessions/SREC/items",
                     json={"item_id": "SE_锚", "task_type": "单要素"}).json()
    client.post(f"/items/{ie['id']}/turns",
                json={"turn_seq": 1, "response_role": "命名", "prompt_level": 0})
    client.post("/audio", json={"raw_audio_id": "arec", "session_id": "SREC"})
    client.post("/sessions/SREC/abnormal", json={"abnormal_type": "环境噪声"})

    sessions = client.get("/patients/PREC/sessions").json()
    assert [row["session_id"] for row in sessions] == ["SREC"]
    journal = client.get("/sessions/SREC/journal").json()
    assert journal["session"]["session_id"] == "SREC"
    assert len(journal["items"]) == len(journal["turns"]) == 1
    assert journal["audios"][0]["raw_audio_id"] == "arec"
    assert journal["abnormal"][0]["abnormal_type"] == "环境噪声"


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
    _mk_session(client, sid="S1", pid="P-LIVE")
    assert client.post("/sessions", json={
        "session_id": "S2", "patient_id": "P-LIVE", "week_no": 2,
        "phase_type": "正式训练", "event_line": "正式训练",
        "item_bank_version_id": "wk2-v1-20260707",
    }).status_code == 200
    hs1 = client.put("/live/state", json={"kind": "session",
                          "payload": {"sessionId": "S1", "weekNo": 2, "mode": "task", "wseq": 1}})
    cur = client.put("/live/state", json={"kind": "cursor",
                          "payload": {"sessionId": "S1", "screen": "record", "itemIdx": 3,
                                      "turnIdx": 0, "responseRole": "命名", "cueLevel": 0,
                                      "recording": "armed", "wseq": 0}})
    assert client.post("/audio", json={"raw_audio_id": "a9", "session_id": "S1",
                                       "turn_key": "SE_树#1"}).status_code == 200
    assert client.put("/audio/a9/blob", content=b"live-audio").status_code == 200
    client.put("/live/state", json={"kind": "audioSaved",
                                    "payload": {"rawAudioId": "a9", "durationSeconds": 1.2,
                                                "turnKey": "SE_树#1",
                                                "sessionId": "S1"}})
    client.put("/live/state", json={"kind": "patientRec",
                                    "payload": {"active": True, "turnKey": "SE_树#1", "sessionId": "S1"}})
    d = client.get("/live/state").json()
    assert d["seq"] == 4 and d["cursor"]["itemIdx"] == 3
    assert d["session"]["wseq"] == hs1.json()["wseq"] < cur.json()["wseq"] == d["cursor"]["wseq"]
    assert "audioSaved" not in d and "patientRec" not in d          # 患者最小读口不泄露研究回报
    console = client.get("/live/console-state").json()
    assert console["audioSaved"]["rawAudioId"] == "a9"
    assert console["patientRec"]["active"] is True
    # 新场次握手 → 旧游标/录音回报/麦克风上报清空(防老人端串场)
    client.put("/live/state", json={"kind": "session",
                                    "payload": {"sessionId": "S2", "weekNo": 2, "mode": "task"}})
    d2 = client.get("/live/state").json()
    assert d2["session"]["sessionId"] == "S2" and d2["cursor"] is None
    console2 = client.get("/live/console-state").json()
    assert console2["audioSaved"] is None and console2["patientRec"] is None
    assert d2["seq"] == 5                                           # seq 只增不回卷
    assert client.put("/live/state", json={"kind": "bogus", "payload": {}}).status_code == 422


def test_tts_speak_degraded_and_validation(client, monkeypatch):
    import app.tts as tts_mod
    monkeypatch.setenv("TTS_ENGINE", "null")
    monkeypatch.setattr(tts_mod, "_engine", None)          # 复位懒加载单例,强制吃到 env
    assert client.get("/tts/speak", params={"text": ""}).status_code == 422
    assert client.get("/tts/speak", params={"text": "x" * 501}).status_code == 422
    r = client.get("/tts/speak", params={"text": "你好"})
    assert r.status_code == 204 and r.headers["x-tts-engine"] == "null-0"  # 降级→前端回退系统语音
    monkeypatch.setattr(tts_mod, "_engine", None)


def test_tts_piper_synthesis_and_cache(client, tmp_path, monkeypatch):
    import app.tts as tts_mod
    if not tts_mod.DEFAULT_VOICE.exists():
        pytest.skip("本机无 piper 语音模型(部署机可选)")
    monkeypatch.delenv("TTS_ENGINE", raising=False)
    monkeypatch.setattr(tts_mod, "_engine", None)
    monkeypatch.setattr(tts_mod, "CACHE_DIR", tmp_path / "tts-cache")
    r = client.get("/tts/speak", params={"text": "这是测试"})
    assert r.status_code == 200 and r.content[:4] == b"RIFF"
    assert r.headers["x-tts-cache"] == "miss"
    r2 = client.get("/tts/speak", params={"text": "这是测试"})
    assert r2.headers["x-tts-cache"] == "hit" and r2.content == r.content  # 同句只合成一次
    monkeypatch.setattr(tts_mod, "_engine", None)


def test_phase_aware_cue_intervention_flagged(client):
    _mk_session(client)
    ev = client.post("/sessions/SM0/abnormal",
                     json={"intervention_type": "代说物品名"}).json()
    # 正式训练周代说物品名 → 线索性介入 + 影响判分有效性
    assert ev["abnormal_type"] == "线索性介入" and ev["affects_scoring_validity"] is True
