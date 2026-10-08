"""Optional warmup is an isolated, manual presentation, never a training fact."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlmodel import Session, select

from app import content, session_completion, tts, warmup
from app.models import (AttemptEvent, AudioAssetRow, ItemEvent, LiveState,
                        RapportUtteranceEvent, SessionRuntimeState,
                        Session as TrainSession)
from test_rapport_reply_pipeline import (
    pipeline_client as shared_pipeline_client, _seed_scene,
    _seed_audio, _use_reply_stub, _StubAsr,
)
from test_backend_concurrency_privacy import _authenticated_owner

pipeline_client = shared_pipeline_client


def _scene(client):
    _seed_scene(client)
    response = client.put("/live/state", json={"kind": "rapportStep", "payload": {
        "sessionId": "S-PIPE", "sectionKey": "介绍机构环境", "questionIdx": 1,
        "beat": "ask", "recording": "idle", "containsDirectIdentifier": False,
    }})
    assert response.status_code == 200, response.text


def _body(client, action="start", **kwargs):
    with Session(client.test_engine) as s:
        live = s.get(LiveState, 1)
        state = s.get(SessionRuntimeState, "S-PIPE")
        wseq = json.loads(live.rapport_json)["wseq"]
        revision = state.revision
    body = {"action": action, "expectedWseq": wseq,
            "expectedRevision": revision,
            "idempotencyKey": f"warmup-{action}-{wseq}-{revision}"}
    if action == "start":
        body["topic"] = "weather"
    return dict(body, **kwargs)


def _post(client, body):
    return client.post("/sessions/S-PIPE/warmup", json=body)


def _action(client, action="start", **kwargs):
    response = _post(client, _body(client, action, **kwargs))
    assert response.status_code == 200, response.text
    return response.json()


def _counts(client):
    with Session(client.test_engine) as s:
        return [len(list(s.exec(select(model)))) for model in (
            ItemEvent, AttemptEvent, AudioAssetRow, RapportUtteranceEvent)]


def test_manual_module_never_changes_frozen_script_or_training_facts(pipeline_client):
    client = pipeline_client
    frozen = (content.CONTENT_DIR / "week1_script.json").read_bytes()
    _scene(client)
    initial = client.get("/sessions/S-PIPE/runtime").json()["rapportStep"]
    assert "warmup" not in initial
    before = _counts(client)
    bundle = client.get("/content/week1-warmup").json()
    assert [topic["id"] for topic in bundle["topics"]] == ["weather", "food", "sleep", "activity"]
    for topic in bundle["topics"]:
        started = _action(client, topic=topic["id"])
        step = started["rapportStep"]
        assert step["warmup"] == {
            "versionId": "wk1-warmup-v1", "topic": topic["id"],
            "beat": "ask", "seq": started["wseq"],
        }
        assert step["sectionKey"] == initial["sectionKey"]
        assert step["questionIdx"] == initial["questionIdx"]
        shown = client.get("/sessions/S-PIPE/patient-presentation").json()
        assert shown["text"] == topic["ask"]
        assert shown["script_version_id"] == "wk1-relationship-v1-20260702rev"
        assert shown["warmup"] == step["warmup"]
        replied = _action(client, "reply")
        assert client.get("/sessions/S-PIPE/patient-presentation").json()["text"] == topic["reply"]
        replayed = _action(client, "replay")
        assert replayed["wseq"] > replied["wseq"]
        assert replayed["rapportStep"]["warmup"]["beat"] == "reply"
        assert client.get("/sessions/S-PIPE/patient-presentation").json()["text"] == topic["reply"]
        _action(client, "end")
        shown = client.get("/sessions/S-PIPE/patient-presentation").json()
        assert shown["text"] is None and shown["speaker"] == "研究者"
        assert shown["warmup"]["beat"] == "done"
        continued = _action(client, "continue")["rapportStep"]
        assert "warmup" not in continued
        assert continued["sectionKey"] == initial["sectionKey"]
        assert continued["questionIdx"] == initial["questionIdx"]
    assert before == _counts(client) == [0, 0, 0, 0]
    assert (content.CONTENT_DIR / "week1_script.json").read_bytes() == frozen


def test_exact_retry_is_durable_and_never_allocates_second_playback(pipeline_client):
    client = pipeline_client
    _scene(client)
    body = _body(client)
    first = _post(client, body)
    assert first.status_code == 200, first.text
    assert _post(client, body).json() == first.json()
    assert _post(client, dict(body, topic="sleep")).status_code == 409
    replay = _action(client, "replay")
    assert replay["wseq"] > first.json()["wseq"]
    assert _post(client, body).status_code == 409
    end_body = _body(client, "end")
    ended = _post(client, end_body)
    assert _post(client, end_body).json() == ended.json()
    continue_body = _body(client, "continue")
    continued = _post(client, continue_body)
    assert _post(client, continue_body).json() == continued.json()


def test_concurrent_identical_start_has_one_generation(pipeline_client):
    client = pipeline_client
    _scene(client)
    body = _body(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _n: _post(client, body), range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert responses[0].json()["runtimeRevision"] == body["expectedRevision"] + 1


def test_missing_optional_content_cannot_block_stop_or_formal_return(pipeline_client, monkeypatch):
    client = pipeline_client
    _scene(client)
    _action(client)

    def unavailable():
        raise content.FrozenContentUnavailable("synthetic missing file")

    monkeypatch.setattr(warmup, "load_bundle", unavailable)
    assert _post(client, _body(client, "replay")).status_code == 503
    _action(client, "end")
    _action(client, "continue")


@pytest.mark.parametrize("change", [
    {"expectedRevision": -1}, {"expectedRevision": True}, {"expectedWseq": True},
    {"topic": "diagnosis"}, {"extra": "secret"}, {"topic": None},
    {"action": "reply", "topic": "weather"},
])
def test_closed_action_contract(pipeline_client, change):
    client = pipeline_client
    _scene(client)
    assert _post(client, dict(_body(client), **change)).status_code == 422


def test_revision_and_generation_are_both_required(pipeline_client):
    client = pipeline_client
    _scene(client)
    body = _body(client)
    assert _post(client, dict(body, expectedRevision=body["expectedRevision"] + 1)).status_code == 409
    assert _post(client, dict(body, expectedWseq=body["expectedWseq"] + 1)).status_code == 409
    assert "warmup" not in client.get("/sessions/S-PIPE/runtime").json()["rapportStep"]


def test_no_microphone_ordinary_navigation_or_old_reply_while_warmup_present(pipeline_client):
    client = pipeline_client
    _scene(client)
    formal = client.get("/sessions/S-PIPE/runtime").json()["rapportStep"]
    _action(client)
    for beat in ("ask", "done"):
        assert client.post("/sessions/S-PIPE/recording-authorization").json()["allowed"] is False
        for recording in ("idle", "armed"):
            stale = {key: value for key, value in formal.items() if key != "sourceWseq"}
            stale["recording"] = recording
            assert client.put("/live/state", json={"kind": "rapportStep", "payload": stale}).status_code == 409
        assert client.post("/sessions/S-PIPE/rapport/replies", json={
            "sectionKey": "介绍机构环境", "questionIdx": 1,
            "mode": "bank", "replyId": "k1"}).status_code == 409
        assert client.get("/sessions/S-PIPE/rapport/playback").status_code == 409
        if beat == "ask":
            assert _post(client, _body(client, "continue")).status_code == 409
            _action(client, "end")
    _action(client, "continue")
    assert client.post("/sessions/S-PIPE/recording-authorization").json()["allowed"] is True


@pytest.mark.parametrize("kind", ["command", "device"])
def test_start_requires_actual_idle_microphone(pipeline_client, kind):
    client = pipeline_client
    _scene(client)
    with Session(client.test_engine) as s:
        live = s.get(LiveState, 1)
        if kind == "command":
            step = json.loads(live.rapport_json)
            step["recording"] = "armed"
            live.rapport_json = json.dumps(step)
        else:
            live.patient_rec_json = json.dumps({"active": True, "sessionId": "S-PIPE"})
        s.add(live)
        s.commit()
    assert _post(client, _body(client)).status_code == 409


def test_pause_resume_and_handshake_recover_to_silent_done(pipeline_client):
    client = pipeline_client
    _scene(client)
    started = _action(client)
    assert client.post("/sessions/S-PIPE/pause").status_code == 200
    assert _post(client, _body(client, "replay")).status_code == 409
    assert client.post("/sessions/S-PIPE/resume").status_code == 200
    step = client.get("/sessions/S-PIPE/runtime").json()["rapportStep"]
    assert step["warmup"]["beat"] == "done"
    assert step["sectionKey"] == started["rapportStep"]["sectionKey"]
    assert client.get("/sessions/S-PIPE/patient-presentation").json()["text"] is None
    _action(client, topic="sleep")
    handshake = client.put("/live/state", json={"kind": "session", "payload": {
        "sessionId": "S-PIPE", "weekNo": 1, "mode": "rapport",
        "eventLine": "关系建立环节", "itemBankVersionId": "wk2-v1-20260707"}})
    assert handshake.status_code == 200, handshake.text
    shown = client.get("/sessions/S-PIPE/patient-presentation").json()
    assert shown["warmup"]["beat"] == "done" and shown["text"] is None


def test_same_session_handshake_cannot_erase_known_hot_microphone(pipeline_client):
    client = pipeline_client
    _scene(client)
    with Session(client.test_engine) as s:
        live = s.get(LiveState, 1)
        live.patient_rec_json = json.dumps({"sessionId": "S-PIPE", "active": True,
                                           "turnKey": "关系建立·介绍机构环境#1"})
        s.add(live)
        s.commit()
    response = client.put("/live/state", json={"kind": "session", "payload": {
        "sessionId": "S-PIPE", "weekNo": 1, "mode": "rapport",
        "eventLine": "关系建立环节", "itemBankVersionId": "wk2-v1-20260707"}})
    assert response.status_code == 200, response.text
    assert _post(client, _body(client)).status_code == 409


def test_remote_patient_receives_only_current_projection_and_cannot_operate(pipeline_client, monkeypatch):
    client = pipeline_client
    _scene(client)
    _authenticated_owner(client, monkeypatch)
    pair = client.post("/device/pair", headers={"X-Console-Pin": "24681024"},
                       json={"deviceId": "warmup-remote-device-0001"})
    assert pair.status_code == 200, pair.text
    cap = {"X-Device-Capability": pair.json()["capability"]}
    started = _action(client)
    live = client.get("/live/state", headers=cap)
    assert live.status_code == 200, live.text
    assert live.json()["rapportStep"]["warmup"] == started["rapportStep"]["warmup"]
    shown = client.get("/sessions/S-PIPE/patient-presentation", headers=cap)
    assert shown.status_code == 200
    assert shown.json()["text"] == warmup.load_bundle().topics[0].ask
    assert "warmupOperation" not in json.dumps(live.json())
    assert "warmupFence" not in json.dumps(live.json())
    assert "topics" not in shown.json()
    client.cookies.clear()  # A genuinely separate tablet has no operator cookie.
    assert client.post("/sessions/S-PIPE/warmup", headers=cap,
                       json=_body(client, "end")).status_code in {401, 403}
    assert client.get("/content/week1-warmup", headers=cap).status_code in {401, 403}


def test_warmup_denies_new_capture_but_preserves_preexisting_upload(pipeline_client):
    client = pipeline_client
    _scene(client)
    registration = {"raw_audio_id": "before-warmup", "session_id": "S-PIPE",
                    "turn_key": "关系建立·介绍机构环境#1"}
    assert client.post("/audio", json=registration).status_code == 200
    _action(client)
    assert client.post("/audio", json=registration).status_code == 200
    assert client.post("/audio", json=dict(registration, raw_audio_id="during-warmup")).status_code == 409
    saved = client.put("/audio/before-warmup/blob", content=b"\x1a\x45\xdf\xa3synthetic-old-capture",
                       headers={"Content-Type": "audio/webm"})
    assert saved.status_code == 200, saved.text
    reported = client.put("/live/state", json={"kind": "patientRec", "payload": {
        "sessionId": "S-PIPE", "turnKey": registration["turn_key"], "active": True}})
    assert reported.status_code == 200, reported.text
    runtime = client.get("/sessions/S-PIPE/runtime").json()
    assert runtime["status"] == "paused"
    assert runtime["rapportStep"]["warmup"]["beat"] == "done"
    assert _counts(client) == [0, 0, 1, 0]
    assert client.post("/sessions/S-PIPE/resume").status_code == 409
    stopped = client.put("/live/state", json={"kind": "patientRec", "payload": {
        "sessionId": "S-PIPE", "turnKey": registration["turn_key"], "active": False}})
    assert stopped.status_code == 200, stopped.text
    assert client.post("/sessions/S-PIPE/resume").status_code == 200
    _action(client, topic="food")


def test_tts_uses_current_fixed_line_and_late_audio_is_fenced(pipeline_client, monkeypatch):
    client = pipeline_client
    _scene(client)
    _action(client)
    ask = warmup.load_bundle().topics[0].ask
    monkeypatch.setattr(tts, "speak", lambda text: (b"synthetic-wav", "stub/1", False))
    assert client.post("/tts/speak", json={"text": ask}).status_code == 200
    assert client.post("/tts/speak", json={"text": "请问您叫什么名字呀？"}).status_code == 409
    entered, finish = Event(), Event()

    def delayed(text):
        entered.set()
        assert finish.wait(5)
        return b"synthetic-wav", "stub/1", False

    monkeypatch.setattr(tts, "speak", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, "/tts/speak", json={"text": ask})
        assert entered.wait(5)
        _action(client, "end")
        finish.set()
        assert future.result().status_code == 409
    assert client.post("/tts/speak", json={"text": ask}).status_code == 409


def test_all_fixed_lines_are_cloud_allowlisted_without_patient_text(monkeypatch):
    monkeypatch.setattr(tts, "_allow_cache", None)
    for topic in warmup.load_bundle().topics:
        assert tts.cloud_text_allowed(topic.ask)
        assert tts.cloud_text_allowed(topic.reply)
    assert not tts.cloud_text_allowed("患者刚才说了一个未冻结的句子")


def test_other_owner_and_terminal_sessions_cannot_control_warmup(pipeline_client, monkeypatch):
    client = pipeline_client
    _scene(client)
    _authenticated_owner(client, monkeypatch)
    body = _body(client)
    with Session(client.test_engine) as s:
        session = s.get(TrainSession, "S-PIPE")
        session.trainer_id = "ANOTHER-OPERATOR"
        s.add(session)
        s.commit()
    assert _post(client, body).status_code == 404
    with Session(client.test_engine) as s:
        session = s.get(TrainSession, "S-PIPE")
        session.trainer_id = "PIPELINE-RESEARCHER"
        state = s.get(SessionRuntimeState, "S-PIPE")
        state.status = "aborted"
        s.add(session)
        s.add(state)
        s.commit()
    assert _post(client, body).status_code == 409


def test_warmup_cannot_stand_in_for_formal_farewell_completion():
    script = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
    assessment = session_completion.assess_rapport_completion(
        script, {"sectionKey": "道别", "questionIdx": 0,
                 "warmup": {"versionId": "wk1-warmup-v1", "topic": "weather",
                            "beat": "done", "seq": 1}},
        "idle", [], 0, session_id="S-PIPE", is_simulation=True,
        data_classification="simulation", blob_exists=lambda _raw_id: True)
    assert not assessment.ready
    assert "rapport_warmup_pending" in {issue.code for issue in assessment.issues}


def test_late_formal_ai_reply_stays_fenced_after_warmup_and_return(pipeline_client, monkeypatch):
    client = pipeline_client
    _scene(client)
    _seed_audio(client, session_id="S-PIPE", raw_id="pre-warmup-answer",
                section="介绍机构环境", question=1)
    from app import asr
    monkeypatch.setattr(asr, "get_engine", lambda: _StubAsr("我认识这里的朋友"))
    entered, finish = Event(), Event()
    engine = _use_reply_stub(monkeypatch, "谢谢您分享。")

    def delayed(*args, **kwargs):
        entered.set()
        assert finish.wait(5)
        return "谢谢您分享。"

    monkeypatch.setattr(engine, "generate", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, "/sessions/S-PIPE/rapport/replies", json={
            "sectionKey": "介绍机构环境", "questionIdx": 1, "mode": "auto",
            "rawAudioId": "pre-warmup-answer"})
        assert entered.wait(5)
        _action(client)
        _action(client, "end")
        continued = _action(client, "continue")["rapportStep"]
        assert client.put("/live/state", json={"kind": "rapportStep", "payload": continued}).status_code == 200
        finish.set()
        assert future.result().status_code == 409
    with Session(client.test_engine) as s:
        assert list(s.exec(select(RapportUtteranceEvent))) == []
