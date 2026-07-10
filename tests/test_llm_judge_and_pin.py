import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import llm_judge
from app.db import get_session
from app.enums import AnswerType
from app.judging import PortraitLeakError, build_judge_input
from app.llm_judge import LlmJudgement, build_judge_prompt, get_engine
from app.main import app


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


def _mk_turn(client) -> int:
    client.post("/patients", json={"patient_id": "PL"})
    client.post("/sessions", json={"session_id": "SL", "patient_id": "PL", "week_no": 2,
                                   "phase_type": "正式训练", "event_line": "正式训练",
                                   "item_bank_version_id": "wk2-v1-20260707"})
    ie = client.post("/sessions/SL/items", json={"item_id": "SE_锚", "task_type": "单要素"}).json()
    te = client.post(f"/items/{ie['id']}/turns",
                     json={"turn_seq": 1, "response_role": "命名", "asr_text": "锚"}).json()
    return te["id"]


# ---------------- LLM 判分骨架 ----------------
def test_default_off_falls_back_to_rules(client):
    tid = _mk_turn(client)
    j = client.post(f"/turns/{tid}/ai-judge").json()
    assert j["ai_judge_mode"] == "规则确定式" and j["ai_answer_type"] == "正确"


def test_llm_engine_used_when_enabled(client, monkeypatch):
    class FakeLlm:
        version = "fake-1"
        def judge(self, ji):
            return LlmJudgement(AnswerType.部分正确, 0.5, True, reason="测试替身")
    llm_judge.register_engine("fake", FakeLlm())
    monkeypatch.setenv("LLM_JUDGE", "fake")
    tid = _mk_turn(client)
    j = client.post(f"/turns/{tid}/ai-judge").json()
    assert j["ai_judge_mode"] == "LLM辅助" and j["ai_answer_type"] == "部分正确"
    assert j["ai_score"] == 0.5 and j["ai_needs_review"] is True
    # LLM 只产初评——锁定分仍须人工,且锁后 ai-judge 409(与规则后端同一约束)
    client.patch(f"/turns/{tid}/lock", json={"reviewer_id": "R1", "element_value": 1})
    assert client.post(f"/turns/{tid}/ai-judge").status_code == 409


def test_unregistered_engine_degrades_to_rules(client, monkeypatch):
    monkeypatch.setenv("LLM_JUDGE", "nonexistent")
    assert get_engine().version == "off"          # fail-degraded,不 fail-hard
    tid = _mk_turn(client)
    assert client.post(f"/turns/{tid}/ai-judge").json()["ai_judge_mode"] == "规则确定式"


def test_prompt_is_portrait_free_by_construction():
    ji = build_judge_input(item_id="x", task_type="单要素", target_word="锚", asr_text="锚")
    p = build_judge_prompt(ji)
    assert "锚" in p and "zodiac" not in p and "属相" not in p
    # 画像字段根本进不了 JudgeInput(构造即拒),prompt 无从泄漏
    with pytest.raises(PortraitLeakError):
        build_judge_input(item_id="x", task_type="单要素", target_word="锚", zodiac="属牛")


# ---------------- 操作端 PIN 门 ----------------
def test_pin_gate_blocks_writes_allows_reads(client, monkeypatch):
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    # 读不拦(老人端轮询无需先输 PIN)
    assert client.get("/health").status_code == 200
    assert client.get("/live/state").status_code == 200
    # 写没带 PIN → 401;带对 → 放行;带错 → 401
    assert client.post("/patients", json={"patient_id": "PP"}).status_code == 401
    assert client.post("/patients", json={"patient_id": "PP"},
                       headers={"X-Console-Pin": "246810"}).status_code == 200
    assert client.put("/live/state", json={"kind": "cursor", "payload": {}},
                      headers={"X-Console-Pin": "000000"}).status_code == 401


def test_no_pin_configured_means_open(client, monkeypatch):
    monkeypatch.delenv("CONSOLE_PIN", raising=False)
    assert client.post("/patients", json={"patient_id": "PQ"}).status_code == 200
