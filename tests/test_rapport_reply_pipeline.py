"""第1周互动态:回应句生成管线 + 发声账本 + 按行发声端点。

覆盖:script/bank/auto 三种生成模式、云门禁与四级降级梯(没听清/没说话/
AI 不可用/未授权云处理)、auto 幂等、姓名年龄两问对自动管线的结构性拒绝、
rapportStep 的 utteranceId 槽位校验、按行发声端点的指针围栏与服务证据,
以及 tts 层"白名单旁路只对持久行合成开放"那道新闸。
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import io
import json
import wave

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app import asr, audio_store, db, rapport_reply, tts, patient_presentation
from app.asr import AsrResult
from app.main import app
from app.models import (
    AudioAssetRow, AudioCaptureReceipt, RapportUtteranceEvent, TtsServeEvidence,
)


@pytest.fixture
def pipeline_client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'rapport-pipeline.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir(mode=0o700)
    monkeypatch.setattr(audio_store, "AUDIO_DIR", audio_dir)
    monkeypatch.delenv("CLOUD_PROCESSING_PROVIDER_ID", raising=False)
    monkeypatch.delenv("CLOUD_PROCESSING_NOTICE_VERSION", raising=False)
    client = TestClient(app)
    client.test_engine = engine
    client.audio_dir = audio_dir
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


def _create_session(client: TestClient, *, session_id: str, patient_id: str) -> None:
    response = client.post("/sessions", json={
        "session_id": session_id,
        "patient_id": patient_id,
        "week_no": 1,
        "phase_type": "关系建立",
        "event_line": "关系建立环节",
        "item_bank_version_id": "wk2-v1-20260707",
        "is_simulation": True,
        "trainer_id": "PIPELINE-RESEARCHER",
    })
    assert response.status_code == 200, response.text


def _post_live_session(client: TestClient, session_id: str) -> None:
    response = client.put("/live/state", json={
        "kind": "session",
        "payload": {
            "sessionId": session_id, "weekNo": 1,
            "eventLine": "关系建立环节", "mode": "rapport",
            "itemBankVersionId": "wk2-v1-20260707",
        },
    })
    assert response.status_code == 200, response.text


def _seed_scene(client: TestClient, *, patient="P-PIPE", session="S-PIPE") -> None:
    _create_patient(client, patient)
    _create_session(client, session_id=session, patient_id=patient)
    _post_live_session(client, session)


def _seed_audio(client: TestClient, *, session_id: str, raw_id: str,
                section: str, question: int | None = 0) -> None:
    """默认按问绑定(「关系建立·<节>#<问>」,2026-09-04 起设备开麦即锁存问位);
    question=None 造旧版按节绑定的录音。"""
    payload = b"fake-webm-bytes-" + raw_id.encode()
    (client.audio_dir / f"{raw_id}.webm").write_bytes(payload)
    turn_key = patient_presentation.rapport_turn_key(section, question)
    identity = section == "自我介绍" and (question is None or question in (0, 1))
    with Session(client.test_engine) as s:
        s.add(AudioAssetRow(
            raw_audio_id=raw_id, session_id=session_id,
            turn_key=turn_key, audio_format="webm",
            is_simulation=True, data_classification="simulation",
            contains_direct_identifier=identity,
            byte_count=len(payload), checksum="c" * 64,
            uploaded_at=datetime.now(),
        ))
        s.add(AudioCaptureReceipt(
            raw_audio_id=raw_id, session_id=session_id,
            turn_key=turn_key, duration_seconds=2.0,
            byte_count=len(payload), checksum="c" * 64,
            data_classification="simulation", is_simulation=True,
            contains_direct_identifier=identity,
        ))
        s.commit()


class _StubAsr:
    provider_id = None
    data_boundary = "local"

    def __init__(self, text):
        self._text = text
        self.version = "stub-asr/1"
        self.calls = 0

    def transcribe(self, audio_bytes, hotwords):
        self.calls += 1
        return AsrResult(self._text, 0.9, self.version)


class _StubReply:
    provider_id = None
    data_boundary = "local"
    version = "stub-reply/1"

    def __init__(self, reply):
        self._reply = reply
        self.calls: list[dict] = []

    def generate(self, ask, asr_text, history=(), round_no=1, max_rounds_value=1):
        self.calls.append({"ask": ask, "asr": asr_text, "history": tuple(history),
                           "round": round_no, "max": max_rounds_value})
        reply = self._reply
        return reply(round_no) if callable(reply) else reply


def _use_reply_stub(monkeypatch, reply):
    engine = _StubReply(reply)
    rapport_reply.register_engine("pipeline-stub", engine)
    monkeypatch.setenv("RAPPORT_REPLY", "pipeline-stub")
    return engine


def _create_reply(client, session_id, body):
    return client.post(f"/sessions/{session_id}/rapport/replies", json=body)


def _write_reply_step(client, session_id, *, section, qidx, utterance_id=None,
                      reply_id=None, beat="reply"):
    payload = {
        "sessionId": session_id, "sectionKey": section, "questionIdx": qidx,
        "beat": beat, "recording": "idle",
        "containsDirectIdentifier": section == "自我介绍",
    }
    if utterance_id is not None:
        payload["utteranceId"] = utterance_id
    if reply_id is not None:
        payload["replyId"] = reply_id
    return client.put("/live/state", json={"kind": "rapportStep", "payload": payload})


def test_script_and_bank_modes_persist_the_spoken_line(pipeline_client):
    client = pipeline_client
    _seed_scene(client)
    scripted = _create_reply(client, "S-PIPE", {
        "sectionKey": "自我介绍", "questionIdx": 0, "mode": "script"})
    assert scripted.status_code == 200, scripted.text
    row = scripted.json()
    assert row["source"] == "script"
    assert row["text"] == "好的，认识您真开心。"

    banked = _create_reply(client, "S-PIPE", {
        "sectionKey": "自我介绍", "questionIdx": 3, "mode": "bank",
        "replyId": "a1"})
    assert banked.status_code == 200, banked.text
    assert banked.json()["source"] == "bank"
    assert banked.json()["text"] == "这个挺好的，您再多讲一点吧。"

    refused = _create_reply(client, "S-PIPE", {
        "sectionKey": "自我介绍", "questionIdx": 0, "mode": "bank",
        "replyId": "a1"})
    assert refused.status_code == 422
    with Session(client.test_engine) as s:
        rows = list(s.exec(select(RapportUtteranceEvent).order_by(
            RapportUtteranceEvent.event_seq)))
    assert [r.source for r in rows] == ["script", "bank"]
    assert all(r.origin == "manual" for r in rows)


def test_auto_mode_speaks_a_generated_line_and_is_idempotent(
        pipeline_client, monkeypatch):
    client = pipeline_client
    _seed_scene(client)
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-auto-1",
                section="介绍机构环境")
    monkeypatch.setattr(asr, "get_engine", lambda: _StubAsr("我喜欢去院子里晒太阳"))
    _use_reply_stub(monkeypatch, "晒太阳舒服，您常去院子里坐坐呀？")

    created = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "auto",
        "rawAudioId": "aud-auto-1"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["source"] == "llm"
    assert body["text"] == "晒太阳舒服，您常去院子里坐坐呀？"
    assert body["degradedReason"] is None

    replay = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "auto",
        "rawAudioId": "aud-auto-1"})
    assert replay.status_code == 200
    assert replay.json()["idempotent"] is True
    assert replay.json()["utteranceId"] == body["utteranceId"]

    with Session(client.test_engine) as s:
        rows = list(s.exec(select(RapportUtteranceEvent)))
        assert len(rows) == 1
        row = rows[0]
    assert row.origin == "auto"
    assert row.asr_text == "我喜欢去院子里晒太阳"
    assert row.asr_engine_version == "stub-asr/1"
    assert row.reply_engine_version == "stub-reply/1"
    assert row.raw_audio_id == "aud-auto-1"
    assert row.text_sha256 == hashlib.sha256(row.text.encode()).hexdigest()


def test_ai_usage_summary_counts_the_rapport_pipeline(pipeline_client, monkeypatch):
    """第1周 ASR/现编落在 RapportUtteranceEvent;/ai-usage 的 rapport 段必须数到它,
    否则第1周场次会误显示"ASR/判类 无记录"(与钱凯质量核查口径直接相关)。"""
    client = pipeline_client
    _seed_scene(client)
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-usage-1",
                section="介绍机构环境")
    monkeypatch.setattr(asr, "get_engine", lambda: _StubAsr("我年轻时在工厂做工"))
    _use_reply_stub(monkeypatch, "做工辛苦，那时候您在哪个厂呀？")
    created = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "auto",
        "rawAudioId": "aud-usage-1"})
    assert created.status_code == 200, created.text

    usage = client.get("/sessions/S-PIPE/ai-usage")
    assert usage.status_code == 200, usage.text
    rapport = usage.json()["rapport"]
    assert rapport["asr_engines"] == [{"engine_version": "stub-asr/1", "utterances": 1}]
    assert rapport["reply_engines"] == [{"engine_version": "stub-reply/1", "utterances": 1}]
    assert rapport["degraded"] == []


def test_ai_usage_summary_counts_rapport_degradation(pipeline_client, monkeypatch):
    """降级(老人没说话)也要进 rapport.degraded,现编引擎不计。"""
    client = pipeline_client
    _seed_scene(client)
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-usage-2",
                section="介绍机构环境")
    monkeypatch.setattr(asr, "get_engine", lambda: _StubAsr(""))  # 空转写=没说话
    _use_reply_stub(monkeypatch, "不该被用到")
    created = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "auto",
        "rawAudioId": "aud-usage-2"})
    assert created.status_code == 200, created.text
    assert created.json()["degradedReason"] == "asr_empty"

    rapport = client.get("/sessions/S-PIPE/ai-usage").json()["rapport"]
    assert rapport["degraded"] == [{"reason": "asr_empty", "count": 1}]
    assert rapport["reply_engines"] == []


def test_multi_round_counts_history_and_stops_at_limit(pipeline_client, monkeypatch):
    """同一问位:第1轮追问、第2轮(末轮)收束且历史进 prompt、第3轮 409 换问。"""
    client = pipeline_client
    _seed_scene(client)
    monkeypatch.setenv("RAPPORT_MAX_ROUNDS", "2")
    asr_stub = _StubAsr("我年轻时在纺织厂")
    monkeypatch.setattr(asr, "get_engine", lambda: asr_stub)
    engine = _use_reply_stub(
        monkeypatch, lambda r: "纺织厂啊，那时候忙不忙？" if r == 1 else "听您讲这些真好。")
    for n, raw in ((1, "aud-r1"), (2, "aud-r2")):
        _seed_audio(client, session_id="S-PIPE", raw_id=raw, section="介绍机构环境")
        created = _create_reply(client, "S-PIPE", {
            "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "auto",
            "rawAudioId": raw})
        assert created.status_code == 200, created.text
        body = created.json()
        assert (body["round"], body["maxRounds"], body["final"]) == (n, 2, n == 2)
        # 非末轮的现编句邀请老人接着说;末轮那句只收束,前端据此不再开麦。
        assert body["invitesMore"] is (n == 1)
    assert engine.calls[0]["round"] == 1 and engine.calls[0]["history"] == ()
    assert engine.calls[1]["round"] == 2
    assert engine.calls[1]["history"] == (("我年轻时在纺织厂", "纺织厂啊，那时候忙不忙？"),)

    _seed_audio(client, session_id="S-PIPE", raw_id="aud-r3", section="介绍机构环境")
    third = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "auto",
        "rawAudioId": "aud-r3"})
    # 聊满不是 409:老人仍然要有回应(端点的「老人永远有回应」契约),
    # 但一个字节都不再进云,且这句是收束句、不再邀请老人接着说。
    assert third.status_code == 200, third.text
    exhausted = third.json()
    assert exhausted["degradedReason"] == "round_limit"
    assert exhausted["invitesMore"] is False
    assert "？" not in exhausted["text"]
    assert asr_stub.calls == 2 and len(engine.calls) == 2
    # 收束行不计入轮次:再来一次仍然是收束,不会把轮次顶到 4、5……
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-r4", section="介绍机构环境")
    again = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "auto",
        "rawAudioId": "aud-r4"})
    assert again.status_code == 200, again.text
    assert again.json()["degradedReason"] == "round_limit"
    # 轮次不越界:聊满后的收束行不是「第 3 轮」,屏上不能出现「第 3 / 2 轮」。
    assert again.json()["round"] == 2 and again.json()["maxRounds"] == 2
    assert asr_stub.calls == 2
    # 换一问,轮次从 1 重数。
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-q1", section="介绍机构环境", question=1)
    fresh = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 1, "mode": "auto",
        "rawAudioId": "aud-q1"})
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["round"] == 1


def test_final_round_question_from_llm_falls_back_to_closing_line(
        pipeline_client, monkeypatch):
    """末轮 LLM 仍抛问题 → 守卫拒 → 回落 k1 收束句(不带问号),老人不会对着空气说话。"""
    client = pipeline_client
    _seed_scene(client)
    monkeypatch.setenv("RAPPORT_MAX_ROUNDS", "1")
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-f1", section="介绍机构环境")
    monkeypatch.setattr(asr, "get_engine", lambda: _StubAsr("我常去花园"))
    # 直接返回带问号的句子:管线里 QwenReplyEngine 会经 validate(final=True) 拒掉;
    # stub 绕过引擎自带守卫,所以这里模拟"引擎已拒绝"= 返回 None。
    _use_reply_stub(monkeypatch, None)
    created = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "auto",
        "rawAudioId": "aud-f1"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["final"] is True
    assert body["degradedReason"] == "llm_unavailable"
    assert "？" not in body["text"] and "?" not in body["text"]


def test_final_round_unclear_audio_uses_non_question_fallback(pipeline_client, monkeypatch):
    """末轮 ASR 失败:不能用带问号的 j1 让老人对着空气回答,改 j2。"""
    client = pipeline_client
    _seed_scene(client)
    monkeypatch.setenv("RAPPORT_MAX_ROUNDS", "1")
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-unclear", section="介绍机构环境")
    monkeypatch.setattr(asr, "get_engine", lambda: _StubAsr(None))
    created = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "auto",
        "rawAudioId": "aud-unclear"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["degradedReason"] == "asr_failed" and body["final"] is True
    assert body["replyId"] == "j2" and "？" not in body["text"]


def test_closing_bank_lines_never_invite_more(pipeline_client, monkeypatch):
    """降级回落句(j2/k1)说完不再开麦:句库自己标了 invites_more=false。"""
    client = pipeline_client
    _seed_scene(client)
    monkeypatch.setenv("RAPPORT_MAX_ROUNDS", "3")   # 留出非末轮空间,单独验这一条
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-empty", section="介绍机构环境")
    monkeypatch.setattr(asr, "get_engine", lambda: _StubAsr(""))   # 老人没说话 → j2
    _use_reply_stub(monkeypatch, "不该被用到")
    created = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "auto",
        "rawAudioId": "aud-empty"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["degradedReason"] == "asr_empty" and body["final"] is False
    assert body["replyId"] == "j2"
    assert body["invitesMore"] is False    # 收束句:非末轮也不许续麦


def test_repeat_unclear_asks_once_then_closes(pipeline_client, monkeypatch):
    """同一问位只请老人重说一次:第二次听不清改收束句,不反复要求重复。"""
    client = pipeline_client
    _seed_scene(client)
    monkeypatch.setenv("RAPPORT_MAX_ROUNDS", "3")
    monkeypatch.setattr(asr, "get_engine", lambda: _StubAsr(None))   # 恒 ASR 失败
    _use_reply_stub(monkeypatch, "不该被用到")
    first = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "auto",
        "rawAudioId": _seed_audio(client, session_id="S-PIPE", raw_id="aud-u1",
                                  section="介绍机构环境") or "aud-u1"})
    assert first.json()["replyId"] == "j1"          # 第一次:请老人再说一次
    assert first.json()["invitesMore"] is True
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-u2", section="介绍机构环境")
    second = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "auto",
        "rawAudioId": "aud-u2"})
    assert second.json()["replyId"] == "j2"         # 第二次:收束,不再要求重复
    assert second.json()["invitesMore"] is False


def test_endpoint_revalidates_generated_text_on_final_round(
        pipeline_client, monkeypatch):
    """末轮守卫必须由端点兜底:它长在 Qwen 引擎的解析里,换个引擎就绕过去了。"""
    client = pipeline_client
    _seed_scene(client)
    monkeypatch.setenv("RAPPORT_MAX_ROUNDS", "1")   # 第一轮即末轮
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-rev", section="介绍机构环境")
    monkeypatch.setattr(asr, "get_engine", lambda: _StubAsr("我常去花园"))
    # stub 绕过引擎自带守卫,直接交回一句还在追问的话。
    _use_reply_stub(monkeypatch, "花园好呀，您常去吗？")
    created = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "auto",
        "rawAudioId": "aud-rev"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["source"] != "llm"                     # 被端点拒了
    assert body["degradedReason"] == "llm_unavailable"
    assert "？" not in body["text"] and body["invitesMore"] is False


def test_generated_closing_line_does_not_invite_more(pipeline_client, monkeypatch):
    """非末轮的现编句若其实是收束句,不能续麦——对着一句道谢开麦,老人不知道说什么。"""
    client = pipeline_client
    _seed_scene(client)
    monkeypatch.setenv("RAPPORT_MAX_ROUNDS", "3")
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-close", section="介绍机构环境")
    monkeypatch.setattr(asr, "get_engine", lambda: _StubAsr("我常去花园"))
    _use_reply_stub(monkeypatch, "听着真好，谢谢您。")
    created = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "auto",
        "rawAudioId": "aud-close"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["source"] == "llm" and body["final"] is False
    assert body["invitesMore"] is False


def test_round_limit_is_not_counted_as_a_degradation(pipeline_client, monkeypatch):
    """聊满是正常收束,不能混进 AI 使用汇总的降级计数,轮次也不能越界显示。"""
    client = pipeline_client
    _seed_scene(client)
    monkeypatch.setenv("RAPPORT_MAX_ROUNDS", "1")
    monkeypatch.setattr(asr, "get_engine", lambda: _StubAsr("我常去花园"))
    _use_reply_stub(monkeypatch, "听着真好，谢谢您。")
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-l1", section="介绍机构环境")
    first = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "auto",
        "rawAudioId": "aud-l1"})
    assert first.status_code == 200
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-l2", section="介绍机构环境")
    over = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "auto",
        "rawAudioId": "aud-l2"})
    assert over.status_code == 200
    body = over.json()
    assert body["degradedReason"] == "round_limit"
    # 屏上不能出现「第 2 / 1 轮」。
    assert body["round"] <= body["maxRounds"]
    rapport = client.get("/sessions/S-PIPE/ai-usage").json()["rapport"]
    assert all(r["reason"] != "round_limit" for r in rapport["degraded"])


def test_identity_questions_never_start_the_auto_pipeline(
        pipeline_client, monkeypatch):
    """姓名/年龄两问:一个字节都不许进 ASR——拒绝发生在任何云调用之前。"""
    client = pipeline_client
    _seed_scene(client)
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-name-1",
                section="自我介绍")
    stub = _StubAsr("我叫王秀兰")
    monkeypatch.setattr(asr, "get_engine", lambda: stub)
    _use_reply_stub(monkeypatch, "不该被用到的句子")

    refused = _create_reply(client, "S-PIPE", {
        "sectionKey": "自我介绍", "questionIdx": 0, "mode": "auto",
        "rawAudioId": "aud-name-1"})
    assert refused.status_code == 409, refused.text
    assert "不开放自动回应" in refused.text
    assert stub.calls == 0
    with Session(client.test_engine) as s:
        assert list(s.exec(select(RapportUtteranceEvent))) == []


@pytest.mark.parametrize(("asr_text", "expect_reply", "expect_reason"), [
    (None, "j1", "asr_failed"),
    ("", "j2", "asr_empty"),
])
def test_unusable_transcript_falls_back_to_frozen_bank(
        pipeline_client, monkeypatch, asr_text, expect_reply, expect_reason):
    client = pipeline_client
    _seed_scene(client)
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-deg-1",
                section="介绍机构环境", question=1)
    monkeypatch.setattr(asr, "get_engine", lambda: _StubAsr(asr_text))
    _use_reply_stub(monkeypatch, "不该被用到的句子")

    created = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 1, "mode": "auto",
        "rawAudioId": "aud-deg-1"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["source"] == "bank"
    assert body["replyId"] == expect_reply
    assert body["degradedReason"] == expect_reason


def test_llm_unavailable_still_answers_from_the_bank(pipeline_client, monkeypatch):
    client = pipeline_client
    _seed_scene(client)
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-off-1",
                section="介绍机构环境", question=2)
    monkeypatch.setattr(asr, "get_engine", lambda: _StubAsr("我喜欢下棋"))
    monkeypatch.setenv("RAPPORT_REPLY", "off")

    created = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 2, "mode": "auto",
        "rawAudioId": "aud-off-1"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["source"] == "bank"
    assert body["replyId"] == "k1"
    assert body["degradedReason"] == "llm_unavailable"


def test_cloud_asr_without_subject_authorization_stays_local(
        pipeline_client, monkeypatch):
    """云引擎在场但受试者未授权:一个字节不出网,回应落回冻结句库。"""
    client = pipeline_client
    _seed_scene(client)
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-cloud-1",
                section="介绍机构环境", question=3)

    class _CloudAsr(_StubAsr):
        data_boundary = "cloud"
        provider_id = "aliyun-dashscope"

    stub = _CloudAsr("云端才能听到的话")
    monkeypatch.setattr(asr, "get_engine", lambda: stub)
    _use_reply_stub(monkeypatch, "不该被用到的句子")

    created = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 3, "mode": "auto",
        "rawAudioId": "aud-cloud-1"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["degradedReason"] == "cloud_not_authorized"
    assert body["source"] == "bank" and body["replyId"] == "k1"
    assert stub.calls == 0


def test_reply_step_only_accepts_a_matching_utterance(pipeline_client):
    client = pipeline_client
    _seed_scene(client)
    created = _create_reply(client, "S-PIPE", {
        "sectionKey": "自我介绍", "questionIdx": 3, "mode": "bank",
        "replyId": "c1"})
    uid = created.json()["utteranceId"]

    assert _write_reply_step(client, "S-PIPE", section="自我介绍", qidx=3,
                             utterance_id=uid).status_code == 200
    # 指向他问的行 / 问句拍带编号 / 双指针,一律拒绝
    assert _write_reply_step(client, "S-PIPE", section="介绍机构环境", qidx=0,
                             utterance_id=uid).status_code == 422
    assert _write_reply_step(client, "S-PIPE", section="自我介绍", qidx=3,
                             utterance_id=uid, beat="ask").status_code == 422
    assert _write_reply_step(client, "S-PIPE", section="自我介绍", qidx=3,
                             utterance_id=uid, reply_id="a1").status_code == 422
    assert _write_reply_step(client, "S-PIPE", section="自我介绍", qidx=3,
                             utterance_id=99_999).status_code == 422


def test_journal_exposes_the_utterance_ledger(pipeline_client):
    """回放屏的数据面:journal 携带发声账本行,键集合与前端 exact 契约同一半。"""
    client = pipeline_client
    _seed_scene(client)
    scripted = _create_reply(client, "S-PIPE", {
        "sectionKey": "自我介绍", "questionIdx": 0, "mode": "script"})
    assert scripted.status_code == 200, scripted.text
    banked = _create_reply(client, "S-PIPE", {
        "sectionKey": "自我介绍", "questionIdx": 3, "mode": "bank",
        "replyId": "a1"})
    assert banked.status_code == 200, banked.text
    journal = client.get("/sessions/S-PIPE/journal")
    assert journal.status_code == 200, journal.text
    rows = journal.json()["rapport_utterances"]
    assert [r["source"] for r in rows] == ["script", "bank"]
    assert [r["event_seq"] for r in rows] == [1, 2]
    assert rows[0]["text"] == "好的，认识您真开心。"
    assert rows[1]["reply_id"] == "a1"
    # 后端多键/少键都必须先在这里红,再去同步前端 exact 解析器。
    assert set(rows[0]) == {
        "id", "session_id", "event_seq", "section_key", "question_idx",
        "source", "origin", "reply_id", "text", "asr_text",
        "asr_engine_version", "reply_engine_version", "degraded_reason",
        "raw_audio_id", "text_sha256", "created_at", "is_simulation",
    }


def test_presentation_serves_the_persisted_text_without_leaking_ids(
        pipeline_client, monkeypatch):
    client = pipeline_client
    _seed_scene(client)
    created = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "bank",
        "replyId": "h2"})
    uid = created.json()["utteranceId"]
    assert _write_reply_step(client, "S-PIPE", section="介绍机构环境", qidx=0,
                             utterance_id=uid).status_code == 200

    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    pair = client.post("/device/pair", headers={"X-Console-Pin": "24681024"},
                       json={"deviceId": "pipeline-device-01"})
    assert pair.status_code == 200, pair.text
    capability = {"X-Device-Capability": pair.json()["capability"]}
    projected = client.get("/sessions/S-PIPE/patient-presentation",
                           headers=capability).json()
    assert projected["beat"] == "reply"
    assert projected["speaker"] == "机器人"
    assert projected["text"] == "听起来是个待着舒服的地方。"
    dumped = json.dumps(projected)
    assert "utteranceId" not in dumped and "replyId" not in dumped

    # 患者免 PIN 读口带发声记录编号(设备按它取音),但绝不带 rawAudioId。
    live = client.get("/live/state", headers=capability).json()
    assert live["rapportStep"]["utteranceId"] == uid
    assert "rawAudioId" not in live["rapportStep"]


def _tiny_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * 200)
    return buf.getvalue()


def test_utterance_tts_serves_current_pointer_only_and_records_evidence(
        pipeline_client, monkeypatch):
    client = pipeline_client
    _seed_scene(client)
    created = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "bank",
        "replyId": "g1"})
    uid = created.json()["utteranceId"]
    text = created.json()["text"]
    other = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "bank",
        "replyId": "g2"}).json()["utteranceId"]
    # 配对(PIN 激活)之后控制台匿名写就关门了:先把指针写好,再配设备。
    assert _write_reply_step(client, "S-PIPE", section="介绍机构环境", qidx=0,
                             utterance_id=uid).status_code == 200

    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    pair = client.post("/device/pair", headers={"X-Console-Pin": "24681024"},
                       json={"deviceId": "pipeline-device-02"})
    capability = {"X-Device-Capability": pair.json()["capability"]}
    synth_calls = []

    def _stub_synth(t):
        synth_calls.append(t)
        return (_tiny_wav(), "stub-tts/1", False)

    monkeypatch.setattr(tts, "speak_rapport_utterance", _stub_synth)

    # live 指着 uid,不是 other:拒绝必须发生在合成之前——不出网、不花钱、
    # 不留服务证据。合成后复核那道围栏挡得住字节,挡不住那次云调用。
    early = client.post(f"/sessions/S-PIPE/rapport/utterances/{other}/tts",
                        headers=capability)
    assert early.status_code == 409, early.text
    assert synth_calls == []
    with Session(client.test_engine) as s:
        assert list(s.exec(select(TtsServeEvidence))) == []

    served = client.post(f"/sessions/S-PIPE/rapport/utterances/{uid}/tts",
                         headers=capability)
    assert served.status_code == 200, served.text
    assert served.headers["content-type"].startswith("audio/wav")
    assert len(synth_calls) == 1

    with Session(client.test_engine) as s:
        rows = list(s.exec(select(TtsServeEvidence)))
        assert len(rows) == 1
        evidence = rows[0]
    assert evidence.source == "rapport_utterance"
    assert evidence.utterance_id == uid
    assert evidence.command_id is None
    assert evidence.result == "served"
    assert evidence.text_sha256 == hashlib.sha256(text.encode()).hexdigest()

    # 账号侧不许走设备发声通道
    refused = client.post(f"/sessions/S-PIPE/rapport/utterances/{uid}/tts")
    assert refused.status_code in {401, 403}, refused.text


def test_whitelist_bypass_only_exists_for_persisted_utterance_synthesis(
        tmp_path, monkeypatch):
    """tts 层那道新闸:同一句白名单外文本,默认链拒出网,授权链可合成。"""
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path / "tts-cache")

    class _CloudStub:
        cloud = True
        version = "stub-cloud/1"
        cache_params = ""

        def __init__(self):
            self.synth_calls = 0

        def available(self):
            return True

        def synthesize(self, text):
            self.synth_calls += 1
            return _tiny_wav()

    free_text = "这句是 LLM 现编的，不在任何冻结白名单里。"
    stub = _CloudStub()
    data, version, _ = tts._synthesize_with([stub], free_text)
    assert data is None and stub.synth_calls == 0

    data, version, cached = tts._synthesize_with(
        [stub], free_text, cloud_text_authorized=True)
    assert data is not None and stub.synth_calls == 1
    assert version == "stub-cloud/1"


def test_identity_recording_cannot_ride_an_open_question(
        pipeline_client, monkeypatch):
    """姓名那一问的录音(设备开麦时锁存 #0)顶着开放问位(#3)的名义请求自动回应:
    服务端按录音自己的问级键拒绝,一个字节不进 ASR。旧版按节绑定的录音分不清是
    哪一问,同样不开放。"""
    client = pipeline_client
    _seed_scene(client)
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-ride-1",
                section="自我介绍", question=0)
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-legacy-1",
                section="自我介绍", question=None)
    stub = _StubAsr("我叫王秀兰")
    monkeypatch.setattr(asr, "get_engine", lambda: stub)
    _use_reply_stub(monkeypatch, "不该被用到的句子")

    for raw in ("aud-ride-1", "aud-legacy-1"):
        refused = _create_reply(client, "S-PIPE", {
            "sectionKey": "自我介绍", "questionIdx": 3, "mode": "auto",
            "rawAudioId": raw})
        assert refused.status_code == 409, refused.text
        assert "不属于当前一问" in refused.text, raw
    assert stub.calls == 0
    with Session(client.test_engine) as s:
        assert list(s.exec(select(RapportUtteranceEvent))) == []


def test_zodiac_interest_activity_questions_run_the_auto_pipeline(
        pipeline_client, monkeypatch):
    """2026-09-04 Eric 拍板:自我介绍节的属相/兴趣/活动三问放行进云。录音绑到问之后,
    这三问走完整的 ASR→现编链;姓名/年龄两问仍在同一节里、仍被拒。"""
    client = pipeline_client
    _seed_scene(client)
    stub = _StubAsr("属牛")
    monkeypatch.setattr(asr, "get_engine", lambda: stub)
    _use_reply_stub(monkeypatch, "属牛的人踏实，您平时也这样吧？")
    for q in (2, 3, 4):
        _seed_audio(client, session_id="S-PIPE", raw_id=f"aud-open-{q}",
                    section="自我介绍", question=q)
        created = _create_reply(client, "S-PIPE", {
            "sectionKey": "自我介绍", "questionIdx": q, "mode": "auto",
            "rawAudioId": f"aud-open-{q}"})
        assert created.status_code == 200, (q, created.text)
        assert created.json()["source"] == "llm", q
    assert stub.calls == 3
    with Session(client.test_engine) as s:
        rows = list(s.exec(select(RapportUtteranceEvent)))
        assert [r.question_idx for r in rows] == [2, 3, 4]
        assets = {a.raw_audio_id: a for a in s.exec(select(AudioAssetRow))}
    assert all(assets[f"aud-open-{q}"].contains_direct_identifier is False for q in (2, 3, 4))


def test_rapport_turn_key_helpers_pin_the_contract():
    from app import content
    script = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
    keys = patient_presentation.rapport_allowed_turn_keys(script)
    assert "关系建立·自我介绍" in keys and "关系建立·自我介绍#4" in keys
    assert "关系建立·自我介绍#5" not in keys
    assert "关系建立·道别" in keys and "关系建立·道别#0" in keys and "关系建立·道别#1" not in keys
    assert patient_presentation.parse_rapport_turn_key("关系建立·自我介绍#2") == ("自我介绍", 2)
    assert patient_presentation.parse_rapport_turn_key("关系建立·自我介绍") == ("自我介绍", None)
    assert patient_presentation.parse_rapport_turn_key("SE_锚#1") is None
    assert patient_presentation.parse_rapport_turn_key("关系建立·") is None
    assert patient_presentation.rapport_identity_question_indices(script, "自我介绍") == frozenset({0, 1})
    assert patient_presentation.rapport_identity_question_indices(script, "介绍机构环境") == frozenset()
    for key, expected in (("关系建立·自我介绍", True), ("关系建立·自我介绍#0", True),
                          ("关系建立·自我介绍#1", True), ("关系建立·自我介绍#2", False),
                          ("关系建立·介绍机构环境#0", False), ("关系建立·道别", False)):
        assert patient_presentation.rapport_turn_requires_identity_flag(script, key) is expected, key


def test_manual_modes_reject_raw_audio_id(pipeline_client):
    client = pipeline_client
    _seed_scene(client)
    for mode, extra in (("bank", {"replyId": "a1"}), ("script", {})):
        refused = _create_reply(client, "S-PIPE", {
            "sectionKey": "自我介绍", "questionIdx": 3 if mode == "bank" else 0,
            "mode": mode, "rawAudioId": "aud-any-1", **extra})
        assert refused.status_code == 422, refused.text
        assert "仅用于 auto" in refused.text


def test_concurrent_auto_lands_a_single_row(pipeline_client, monkeypatch):
    """provider 窗口内另一次 auto 先落账:本次结果必须被丢弃,账本只有一行。"""
    client = pipeline_client
    _seed_scene(client)
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-race-1",
                section="介绍机构环境")

    class _RacingAsr(_StubAsr):
        def transcribe(self, audio_bytes, hotwords):
            with Session(client.test_engine) as rs:
                rs.add(RapportUtteranceEvent(
                    session_id="S-PIPE", event_seq=99,
                    section_key="介绍机构环境", question_idx=0,
                    source="bank", origin="auto", reply_id="j2",
                    text="没关系，那我们聊点别的吧。",
                    raw_audio_id="aud-race-1",
                    text_sha256="e" * 64, is_simulation=True))
                rs.commit()
            return super().transcribe(audio_bytes, hotwords)

    monkeypatch.setattr(asr, "get_engine", lambda: _RacingAsr("我喜欢晒太阳"))
    _use_reply_stub(monkeypatch, "晒太阳舒服呀。")

    created = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "auto",
        "rawAudioId": "aud-race-1"})
    assert created.status_code == 200, created.text
    assert created.json()["idempotent"] is True
    assert created.json()["replyId"] == "j2"
    with Session(client.test_engine) as s:
        assert len(list(s.exec(select(RapportUtteranceEvent)))) == 1


def test_revoked_cloud_consent_keeps_reply_synthesis_local(
        pipeline_client, monkeypatch):
    """llm 行发声时刻重查逐人云授权:未授权/已撤销一律本地嗓子,零出网。"""
    client = pipeline_client
    _seed_scene(client)
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-llm-1",
                section="介绍机构环境")
    monkeypatch.setattr(asr, "get_engine", lambda: _StubAsr("我喜欢下棋"))
    _use_reply_stub(monkeypatch, "下棋好，动脑又消遣。")
    created = _create_reply(client, "S-PIPE", {
        "sectionKey": "介绍机构环境", "questionIdx": 0, "mode": "auto",
        "rawAudioId": "aud-llm-1"})
    assert created.json()["source"] == "llm"
    uid = created.json()["utteranceId"]
    assert _write_reply_step(client, "S-PIPE", section="介绍机构环境", qidx=0,
                             utterance_id=uid).status_code == 200

    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    pair = client.post("/device/pair", headers={"X-Console-Pin": "24681024"},
                       json={"deviceId": "pipeline-device-03"})
    capability = {"X-Device-Capability": pair.json()["capability"]}
    captured: dict = {}

    def _capturing(text, **kw):
        captured.update(kw)
        return (_tiny_wav(), "stub-tts/1", False)

    monkeypatch.setattr(tts, "speak_rapport_utterance", _capturing)
    served = client.post(f"/sessions/S-PIPE/rapport/utterances/{uid}/tts",
                         headers=capability)
    assert served.status_code == 200, served.text
    # 受试者从未授权云处理(policy 也未配):合成必须被按到本地链。
    assert captured.get("allow_cloud") is False
