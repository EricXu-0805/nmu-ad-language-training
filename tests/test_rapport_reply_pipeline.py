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

from app import asr, audio_store, db, rapport_reply, tts
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
                section: str) -> None:
    payload = b"fake-webm-bytes-" + raw_id.encode()
    (client.audio_dir / f"{raw_id}.webm").write_bytes(payload)
    with Session(client.test_engine) as s:
        s.add(AudioAssetRow(
            raw_audio_id=raw_id, session_id=session_id,
            turn_key=f"关系建立·{section}", audio_format="webm",
            is_simulation=True, data_classification="simulation",
            contains_direct_identifier=section == "自我介绍",
            byte_count=len(payload), checksum="c" * 64,
            uploaded_at=datetime.now(),
        ))
        s.add(AudioCaptureReceipt(
            raw_audio_id=raw_id, session_id=session_id,
            turn_key=f"关系建立·{section}", duration_seconds=2.0,
            byte_count=len(payload), checksum="c" * 64,
            data_classification="simulation", is_simulation=True,
            contains_direct_identifier=section == "自我介绍",
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

    def generate(self, ask, asr_text):
        return self._reply


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
                section="介绍机构环境")
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
                section="介绍机构环境")
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
                section="介绍机构环境")

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


def test_identity_section_recording_cannot_ride_an_open_question(
        pipeline_client, monkeypatch):
    """P1 复核发现:录音只绑到节——自我介绍节的录音顶着开放问位名义也必须被拒。"""
    client = pipeline_client
    _seed_scene(client)
    _seed_audio(client, session_id="S-PIPE", raw_id="aud-ride-1",
                section="自我介绍")
    stub = _StubAsr("我叫王秀兰")
    monkeypatch.setattr(asr, "get_engine", lambda: stub)
    _use_reply_stub(monkeypatch, "不该被用到的句子")

    refused = _create_reply(client, "S-PIPE", {
        "sectionKey": "自我介绍", "questionIdx": 3, "mode": "auto",
        "rawAudioId": "aud-ride-1"})
    assert refused.status_code == 409, refused.text
    assert "不开放自动回应" in refused.text
    assert stub.calls == 0
    with Session(client.test_engine) as s:
        assert list(s.exec(select(RapportUtteranceEvent))) == []


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
