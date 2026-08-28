from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import auth, db, llm_judge
from app.db import get_session
from app.enums import AnswerType
from app.judging import PortraitLeakError, build_judge_input
from app.llm_judge import LlmJudgement, build_judge_prompt, get_engine
from app.main import app
from app.models import (AttemptEvent, ResearchUser, SessionRuntimeState,
                        TurnEvent)


@pytest.fixture
def client(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", eng)
    SQLModel.metadata.create_all(eng)

    def override():
        with Session(eng) as s:
            yield s

    app.dependency_overrides[get_session] = override
    test_client = TestClient(app)
    test_client.test_engine = eng
    yield test_client
    app.dependency_overrides.clear()


def _mk_turn(client) -> int:
    client.post("/patients", json={"patient_id": "PL", "consent_status": "已同意",
                                   "consent_type": "本人同意", "mandarin_eligible": True,
                                   "recording_allowed": True,
                                   "is_simulation_subject": True})
    client.post("/sessions", json={"session_id": "SL", "patient_id": "PL", "week_no": 2,
                                   "phase_type": "正式训练", "event_line": "正式训练",
                                   "item_bank_version_id": "wk2-v1-20260707",
                                   "is_simulation": True,
                                   "trainer_id": "LLM-REVIEWER"})
    ie = client.post("/sessions/SL/items", json={"item_id": "SE_锚", "task_type": "单要素"}).json()
    assert client.post("/audio", json={
        "raw_audio_id": "llm-turn-audio", "session_id": "SL",
        "turn_key": "SE_锚#1",
    }).status_code == 200
    assert client.put("/audio/llm-turn-audio/blob",
                      content=b"\x1a\x45\xdf\xa3llm-audio",
                      headers={"content-type": "audio/webm"}).status_code == 200
    with Session(client.test_engine) as session:
        session.add(AttemptEvent(
            session_id="SL", item_id="SE_锚", turn_seq=1,
            response_role="命名", attempt_seq=1, raw_audio_id="llm-turn-audio",
            prompt_level=0, asr_text="锚", asr_confidence=.9,
            asr_engine_version="test-asr", operational_answer_type="正确",
            operational_score=1, operational_needs_review=False,
            judge_mode="规则确定式", judge_engine_version="rule-test",
            processing_status="completed", is_simulation=True,
        ))
        session.commit()
    te = client.post(f"/items/{ie['id']}/turns",
                     json={"turn_seq": 1, "response_role": "命名",
                           "raw_audio_id": "llm-turn-audio"}).json()
    return te["id"]


# ---------------- LLM 判分骨架 ----------------
def test_default_off_falls_back_to_rules(client):
    tid = _mk_turn(client)
    j = client.post(f"/turns/{tid}/ai-judge").json()
    assert j["ai_judge_mode"] == "规则确定式" and j["ai_answer_type"] == "正确"


def test_environment_cannot_reopen_unbound_legacy_ai_judge(client, monkeypatch):
    tid = _mk_turn(client)
    with Session(client.test_engine) as session:
        turn = session.get(TurnEvent, tid)
        assert turn is not None
        turn.source_attempt_id = None
        session.add(turn)
        session.commit()
    monkeypatch.setenv("ALLOW_LEGACY_AI_ENDPOINTS", "1")

    blocked = client.post(f"/turns/{tid}/ai-judge")

    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "authoritative_attempt_required"


def test_ai_judge_rejects_corrupted_source_attempt_identity(client):
    tid = _mk_turn(client)
    with Session(client.test_engine) as session:
        turn = session.get(TurnEvent, tid)
        assert turn is not None and turn.source_attempt_id is not None
        attempt = session.get(AttemptEvent, turn.source_attempt_id)
        assert attempt is not None
        attempt.item_id = "SE_胡萝卜"
        session.add(attempt)
        session.commit()

    blocked = client.post(f"/turns/{tid}/ai-judge")

    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "authoritative_attempt_mismatch"


def test_ai_judge_rejects_corrupted_turn_projection(client):
    tid = _mk_turn(client)
    with Session(client.test_engine) as session:
        turn = session.get(TurnEvent, tid)
        assert turn is not None
        turn.ai_score = 0
        session.add(turn)
        session.commit()

    blocked = client.post(f"/turns/{tid}/ai-judge")

    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "authoritative_attempt_mismatch"


def test_llm_engine_used_when_enabled(client, monkeypatch):
    class FakeLlm:
        version = "fake-1"
        data_boundary = "local"
        provider_id = None
        calls = 0
        def judge(self, ji):
            self.calls += 1
            return LlmJudgement(AnswerType.部分正确, 0.5, True, reason="测试替身")
    fake = FakeLlm()
    llm_judge.register_engine("fake", fake)
    monkeypatch.setenv("LLM_JUDGE", "fake")
    tid = _mk_turn(client)
    j = client.post(f"/turns/{tid}/ai-judge").json()
    assert j["ai_judge_mode"] == "规则确定式" and j["ai_answer_type"] == "正确"
    assert fake.calls == 0  # completed source attempt 后绝不二次调用模型造成漂移
    with Session(client.test_engine) as session:
        session.add(SessionRuntimeState(
            session_id="SL", status="intervention_completed",
            revision=1, intervention_completed_at=datetime.now(),
        ))
        session.add(ResearchUser(
            username="llm-reviewer", display_id="LLM-REVIEWER",
            password_hash=auth.hash_password("password-2026"), role="researcher",
            created_at=datetime.now(),
        ))
        session.commit()
    assert client.post("/auth/login", json={
        "username": "llm-reviewer", "password": "password-2026",
    }).status_code == 200
    client.headers.update({"X-CSRF-Token": client.cookies.get(auth.CSRF_COOKIE_NAME)})
    client.patch(f"/turns/{tid}/confirm", json={
        "confirmed_response_text": "锚", "expected_revision": 0,
        "idempotency_key": "test-llm-confirm-0001",
    })
    locked = client.patch(f"/turns/{tid}/lock",
                          json={"reviewer_id": "R1", "element_value": 1, "prompt_level": 0})
    assert locked.status_code == 200
    # AI judging belongs to the bedside operational phase and cannot be
    # restarted from the later research-review window.
    assert client.post(f"/turns/{tid}/ai-judge").status_code == 409


def test_unregistered_engine_degrades_to_rules(client, monkeypatch):
    monkeypatch.setenv("LLM_JUDGE", "nonexistent")
    assert get_engine().version == "unknown/nonexistent"
    tid = _mk_turn(client)
    assert client.post(f"/turns/{tid}/ai-judge").json()["ai_judge_mode"] == "规则确定式"


def test_prompt_is_portrait_free_by_construction():
    ji = build_judge_input(item_id="x", task_type="单要素", target_word="锚", asr_text="锚")
    p = build_judge_prompt(ji)
    assert "锚" in p and "zodiac" not in p and "属相" not in p
    # 画像字段根本进不了 JudgeInput(构造即拒),prompt 无从泄漏
    with pytest.raises(PortraitLeakError):
        build_judge_input(item_id="x", task_type="单要素", target_word="锚", zodiac="属牛")


# ---------------- 老人端设备 PIN 门 ----------------
def test_pin_gate_blocks_writes_allows_reads(client, monkeypatch):
    pin = {"X-Console-Pin": "24681024"}
    # 先在本地开放模式建立测试场次，然后开启 PIN 模拟独立老人端。
    assert client.post("/patients", json={"patient_id": "PP", "consent_status": "已同意",
                                           "consent_type": "本人同意",
                                           "mandarin_eligible": True,
                                           "recording_allowed": True,
                                           "is_simulation_subject": True}).status_code == 200
    sess = {"session_id": "SPIN", "patient_id": "PP", "week_no": 2,
            "phase_type": "正式训练", "event_line": "正式训练",
            "item_bank_version_id": "wk2-v1-20260707", "is_simulation": True}
    assert client.post("/sessions", json=sess).status_code == 200
    handshake = client.put("/live/state", json={"kind": "session", "payload": {
        "sessionId": "SPIN", "weekNo": 2, "eventLine": "正式训练",
        "mode": "task", "itemBankVersionId": "wk2-v1-20260707",
    }})
    assert handshake.status_code == 200
    cursor = client.put("/live/state", json={"kind": "cursor", "payload": {
        "sessionId": "SPIN", "screen": "present", "itemIdx": 0, "turnIdx": 0,
        "responseRole": "命名", "cueLevel": 0, "recording": "idle",
    }})
    assert cursor.status_code == 200, cursor.text

    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    assert client.get("/health").status_code == 200
    assert client.get("/live/state").status_code == 401
    direct_pin = client.get("/live/state", headers=pin)
    assert direct_pin.status_code == 401
    assert direct_pin.json()["code"] == "device_pair_required"
    paired = client.post("/device/pair", headers=pin, json={
        "deviceId": "llm-pin-device-000001",
    })
    assert paired.status_code == 200, paired.text
    capability = {"X-Device-Capability": paired.json()["capability"]}
    assert client.get("/live/state", headers=capability).status_code == 200
    assert client.get("/sessions/SPIN/plan").status_code == 401
    assert client.get("/sessions/SPIN/plan", headers=capability).status_code == 200

    # 共享 PIN 不再是准管理员：控制台、档案、运行时均需具名账号。
    assert client.get("/live/console-state").status_code == 401
    denied_console = client.get("/live/console-state", headers=pin)
    assert denied_console.status_code == 401
    assert denied_console.json()["code"] == "account_required"
    assert client.get("/patients/PP").status_code == 401
    assert client.get("/patients/PP", headers=pin).status_code == 401
    assert client.get("/sessions/SPIN/runtime", headers=pin).status_code == 401
    assert client.post("/patients", json={"patient_id": "NO"}, headers=pin).status_code == 401
    for kind in ("session", "cursor", "rapportStep"):
        denied = client.put("/live/state", json={"kind": kind, "payload": {}}, headers=capability)
        assert denied.status_code == 401 and denied.json()["code"] == "device_capability_forbidden"

    # PIN 仅配对；短时 capability 才可完成老人端最小环。
    assert client.post("/sessions/SPIN/recording-authorization", headers=capability).status_code == 200
    heartbeat = {"session_id": "SPIN", "screen": "waiting",
                 "cursor_wseq": handshake.json()["wseq"]}
    assert client.post("/live/patient-heartbeat", json=heartbeat).status_code == 401
    assert client.post("/live/patient-heartbeat", json=heartbeat, headers=capability).status_code == 200
    assert client.put("/live/state", json={"kind": "patientRec", "payload": {
        "active": True, "turnKey": "itm-0001#1", "sessionId": "SPIN",
    }}, headers=capability).status_code == 200
    assert client.post("/audio", json={"raw_audio_id": "apin", "session_id": "SPIN",
                                                "turn_key": "itm-0001#1"},
                       headers=capability).status_code == 200
    upload = client.put("/audio/apin/blob", content=b"\x1a\x45\xdf\xa3voice",
                        headers={**capability, "content-type": "audio/webm"})
    assert upload.status_code == 200
    assert client.put("/live/state", json={"kind": "audioSaved", "payload": {
        "rawAudioId": "apin", "durationSeconds": 1, "turnKey": "itm-0001#1",
        "byteCount": upload.json()["bytes"], "checksum": upload.json()["checksum"],
        "containsDirectIdentifier": False,
        "sessionId": "SPIN",
    }}, headers=capability).status_code == 200

    # 元数据/原声只给具名账号，正确 PIN 也返回稳定的重登录信号。
    assert client.get("/audio/apin").status_code == 401
    assert client.get("/audio/apin/blob").status_code == 401
    assert client.get("/audio/apin", headers=pin).json()["code"] == "account_required"
    assert client.get("/audio/apin/blob", headers=pin).json()["code"] == "account_required"


def test_no_pin_configured_means_open(client, monkeypatch):
    monkeypatch.delenv("CONSOLE_PIN", raising=False)
    assert client.post("/patients", json={"patient_id": "PQ"}).status_code == 200
