"""Exact playback/capture generations, real device capabilities and stop fences."""

from sqlmodel import Session, select

from app.models import LiveState, SessionRuntimeState, RapportPlaybackReceipt
from test_rapport_reply_pipeline import pipeline_client as shared_pipeline_client, _seed_scene
from test_backend_concurrency_privacy import _authenticated_owner

pipeline_client = shared_pipeline_client


def _scene(client, monkeypatch):
    _seed_scene(client)
    _authenticated_owner(client, monkeypatch)
    pair = client.post("/device/pair", headers={"X-Console-Pin": "24681024"},
                       json={"deviceId": "playback-device-0001"})
    assert pair.status_code == 200, pair.text
    capability = {"X-Device-Capability": pair.json()["capability"]}
    return capability, _present(client)


def _present(client):
    step = {"sessionId": "S-PIPE", "sectionKey": "介绍机构环境", "questionIdx": 0,
            "beat": "ask", "recording": "idle", "containsDirectIdentifier": False}
    response = client.put("/live/state", json={"kind": "rapportStep", "payload": step})
    assert response.status_code == 200, response.text
    return {"sectionKey": step["sectionKey"], "questionIdx": 0, "beat": "ask",
            "utteranceId": None, "wseq": response.json()["wseq"], "outcome": "played"}


def _post(client, capability, payload):
    return client.put("/sessions/S-PIPE/rapport/playback", headers=capability, json=payload)


def test_playback_is_device_only_idempotent_and_does_not_move_runtime(pipeline_client, monkeypatch):
    client = pipeline_client
    capability, payload = _scene(client, monkeypatch)
    assert client.get("/sessions/S-PIPE/rapport/playback").json() == {"receipt": None}
    denied = _post(client, {}, payload)
    assert denied.status_code == 403
    with Session(client.test_engine) as s:
        runtime_revision = s.get(SessionRuntimeState, "S-PIPE").revision
        live = s.exec(select(LiveState)).one()
        live_revision = (live.seq, live.command_wseq)
    recorded = _post(client, capability, payload)
    assert recorded.status_code == 200, recorded.text
    assert recorded.json() == {"receipt": payload}
    assert _post(client, capability, payload).json() == recorded.json()
    assert client.get("/sessions/S-PIPE/rapport/playback").json() == recorded.json()
    with Session(client.test_engine) as s:
        assert len(list(s.exec(select(RapportPlaybackReceipt)))) == 1
        assert s.get(SessionRuntimeState, "S-PIPE").revision == runtime_revision
        live = s.exec(select(LiveState)).one()
        assert (live.seq, live.command_wseq) == live_revision


def test_new_wseq_cannot_accept_old_same_question_completion(pipeline_client, monkeypatch):
    client = pipeline_client
    capability, old = _scene(client, monkeypatch)
    assert _post(client, capability, old).status_code == 200
    new = _present(client)
    assert new["wseq"] > old["wseq"]
    assert client.get("/sessions/S-PIPE/rapport/playback").json() == {"receipt": None}
    assert _post(client, capability, old).status_code == 409
    assert _post(client, capability, new).status_code == 200


def test_failed_generation_cannot_be_overwritten_by_late_played(pipeline_client, monkeypatch):
    client = pipeline_client
    capability, payload = _scene(client, monkeypatch)
    failed = dict(payload, outcome="failed")
    assert _post(client, capability, failed).status_code == 200
    assert _post(client, capability, payload).status_code == 409
    assert client.get("/sessions/S-PIPE/rapport/playback").json() == {"receipt": failed}


def test_pause_resume_and_device_rotation_fence_old_playback(pipeline_client, monkeypatch):
    client = pipeline_client
    capability, payload = _scene(client, monkeypatch)
    assert client.post("/sessions/S-PIPE/pause").status_code == 200
    assert _post(client, capability, payload).status_code == 409
    assert client.post("/sessions/S-PIPE/resume").status_code == 200
    assert _post(client, capability, payload).status_code == 409
    current = _present(client)
    assert _post(client, capability, current).status_code == 200
    rotated = client.post("/device/pair", headers={"X-Console-Pin": "24681024"},
                          json={"deviceId": "playback-device-0002"})
    assert rotated.status_code == 200, rotated.text
    assert _post(client, capability, current).status_code in {401, 409}
    replacement = {"X-Device-Capability": rotated.json()["capability"]}
    assert _post(client, replacement, current).status_code == 409
    assert client.get("/sessions/S-PIPE/rapport/playback").status_code == 409
    assert client.post("/sessions/S-PIPE/resume").status_code == 200
    assert client.get("/sessions/S-PIPE/rapport/playback").json() == {"receipt": None}
    assert _post(client, replacement, current).status_code == 409
    fresh = _present(client)
    assert _post(client, replacement, fresh).status_code == 200


def test_capture_receipt_preserves_exact_arm_generation(pipeline_client, monkeypatch):
    client = pipeline_client
    capability, _payload = _scene(client, monkeypatch)
    arm = client.put("/live/state", json={"kind": "rapportStep", "payload": {
        "sessionId": "S-PIPE", "sectionKey": "介绍机构环境", "questionIdx": 0,
        "beat": "ask", "recording": "armed", "recSeq": 1,
        "containsDirectIdentifier": False}})
    assert arm.status_code == 200, arm.text
    generation = arm.json()["wseq"]
    authorization = client.post("/sessions/S-PIPE/recording-authorization", headers=capability)
    assert authorization.status_code == 200, authorization.text
    assert authorization.json()["recording_wseq"] == generation
    registration = {"raw_audio_id": "generation-audio", "session_id": "S-PIPE",
                    "turn_key": "关系建立·介绍机构环境#0", "recording_wseq": generation}
    registered = client.post("/audio", headers=capability, json=registration)
    assert registered.status_code == 200, registered.text
    data = b"\x1a\x45\xdf\xa3synthetic-generation-audio"
    uploaded = client.put("/audio/generation-audio/blob", content=data,
                          headers={**capability, "Content-Type": "audio/webm"})
    assert uploaded.status_code == 200, uploaded.text
    # An unrelated newer presentation cannot relabel the immutable capture.
    _present(client)
    saved = client.put("/live/state", headers=capability, json={"kind": "audioSaved", "payload": {
        "sessionId": "S-PIPE", "rawAudioId": "generation-audio",
        "turnKey": registration["turn_key"], "durationSeconds": 2,
        "byteCount": len(data), "checksum": uploaded.json()["checksum"],
        "containsDirectIdentifier": False}})
    assert saved.status_code == 200, saved.text
    receipts = client.get("/sessions/S-PIPE/audio-receipts")
    assert receipts.status_code == 200, receipts.text
    assert receipts.json()["receipts"][0]["recording_wseq"] == generation
    changed = dict(registration, recording_wseq=generation + 1)
    assert client.post("/audio", headers=capability, json=changed).status_code == 409
