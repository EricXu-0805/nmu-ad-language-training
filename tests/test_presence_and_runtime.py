from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine, inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401 —— 注册全部表
from app.db import get_session
from app.main import app


BANK_VERSION = "wk2-v1-20260707"
_HANDSHAKE_WSEQ = 0


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


def _patient(client, patient_id: str, recording_allowed: bool | None = True, headers=None):
    body = {"patient_id": patient_id}
    if recording_allowed is not None:
        body["recording_allowed"] = recording_allowed
    assert client.post("/patients", json=body, headers=headers).status_code == 200


def _training_session(client, session_id: str, patient_id: str, headers=None):
    response = client.post("/sessions", json={
        "session_id": session_id,
        "patient_id": patient_id,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "item_bank_version_id": BANK_VERSION,
    }, headers=headers)
    assert response.status_code == 200, response.text


def _handshake(client, session_id: str, headers=None, mode="task", wseq=None):
    global _HANDSHAKE_WSEQ
    if wseq is None:
        _HANDSHAKE_WSEQ += 10
        wseq = _HANDSHAKE_WSEQ
    return client.put("/live/state", json={"kind": "session", "payload": {
        "sessionId": session_id,
        "weekNo": 2 if mode == "task" else 1,
        "eventLine": "正式训练" if mode == "task" else "关系建立环节",
        "mode": mode,
        "itemBankVersionId": BANK_VERSION,
        "wseq": wseq,
    }}, headers=headers)


def _cursor(session_id: str | None = None, item_idx=0, turn_idx=0, **overrides):
    payload = {
        "screen": "present",
        "itemIdx": item_idx,
        "turnIdx": turn_idx,
        "responseRole": "命名",
        "cueLevel": 0,
        "recording": "idle",
        "selfStart": False,
        "wseq": 20,
    }
    if session_id:
        payload["sessionId"] = session_id
    payload.update(overrides)
    return payload


def test_patient_heartbeat_is_minimal_pin_protected_and_does_not_advance_command_seq(
        client, monkeypatch):
    pin = {"X-Console-Pin": "246810"}
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    _patient(client, "P-HB", headers=pin)
    _training_session(client, "S-HB", "P-HB", headers=pin)
    handshake = _handshake(client, "S-HB", headers=pin, wseq=100)
    assert handshake.status_code == 200
    issued_wseq = handshake.json()["wseq"]
    assert client.get("/sessions/S-HB/runtime").status_code == 401
    assert client.get("/sessions/S-HB/runtime", headers=pin).status_code == 200
    assert client.post("/sessions/S-HB/pause").status_code == 401
    before = client.get("/live/state").json()["seq"]

    heartbeat = {
        "session_id": "S-HB", "screen": "present", "cursor_wseq": issued_wseq,
        "client_ts": "2000-01-01T00:00:00",
    }
    assert client.post("/live/patient-heartbeat", json=heartbeat).status_code == 401
    response = client.post("/live/patient-heartbeat", json=heartbeat, headers=pin)
    assert response.status_code == 200, response.text
    presence = response.json()["patientPresence"]
    assert presence["session_id"] == "S-HB"
    assert presence["screen"] == "present" and presence["online"] is True
    assert presence["cursor_wseq"] == issued_wseq
    live = client.get("/live/state").json()
    assert live["seq"] == before  # 心跳不伪装成新的研究者命令
    assert "patientPresence" not in live and "audioSaved" not in live and "patientRec" not in live
    assert client.get("/live/console-state").status_code == 401
    console = client.get("/live/console-state", headers=pin).json()
    assert console["patientPresence"] == presence

    # 无 PIN 白名单只接最小字段：内容/回答等额外载荷 fail-closed。
    assert client.post("/live/patient-heartbeat", json={
        "session_id": "S-HB", "screen": "present", "answer_text": "敏感回答",
    }, headers=pin).status_code == 422
    assert client.post("/live/patient-heartbeat", json={
        "session_id": "other", "screen": "present",
    }, headers=pin).status_code == 409
    # "超前"的 ack 序号必须被接受:同机部署下患者端显示的游标常来自 BroadcastChannel
    # (客户端时钟域),在操作端 HTTP 写落库前天然超前于服务端 command_wseq;
    # 按超前拒绝会让每次推进后的第一拍心跳都 409,在场判定滞后甚至假离线。
    ahead = client.post("/live/patient-heartbeat", json={
        "session_id": "S-HB", "screen": "present", "cursor_wseq": issued_wseq + 1,
    }, headers=pin)
    assert ahead.status_code == 200
    assert ahead.json()["patientPresence"]["cursor_wseq"] == issued_wseq + 1

    # 在线状态只信服务器 last_seen；TTL 到期后读取即为离线，不需要额外写库任务。
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "PATIENT_ONLINE_TTL_SECONDS", -1)
    assert client.get("/live/console-state", headers=pin).json()["patientPresence"]["online"] is False


def test_runtime_cursor_is_persisted_per_session_and_restored_safely(client):
    _patient(client, "P-RT")
    _training_session(client, "S-RT-1", "P-RT")
    _training_session(client, "S-RT-2", "P-RT")

    first_handshake = _handshake(client, "S-RT-1", wseq=100)
    assert first_handshake.status_code == 200
    operational = _cursor("S-RT-1",
        screen="record", recording="armed", selfStart=True, rawAudioId="in-flight", wseq=101)
    cursor_write = client.put("/live/state", json={"kind": "cursor", "payload": operational})
    assert cursor_write.status_code == 200
    rt1 = client.get("/sessions/S-RT-1/runtime").json()
    assert rt1["cursor"]["itemIdx"] == 0
    assert rt1["cursor"]["screen"] == "present"
    assert rt1["cursor"]["recording"] == "idle"
    assert rt1["cursor"]["selfStart"] is False
    assert "rawAudioId" not in rt1["cursor"]
    assert rt1["cursor"]["wseq"] == cursor_write.json()["wseq"] > first_handshake.json()["wseq"]

    assert _handshake(client, "S-RT-2", wseq=200).status_code == 200
    assert client.put("/sessions/S-RT-2/runtime/cursor", json=_cursor(
        "S-RT-2", item_idx=1, wseq=201)).status_code == 200
    assert client.get("/sessions/S-RT-2/runtime").json()["cursor"]["itemIdx"] == 1

    # 切回第一场：从 DB 的每场次游标恢复，而不是沿用第二场或浏览器 localStorage。
    restored_handshake = _handshake(client, "S-RT-1", wseq=300)
    assert restored_handshake.status_code == 200
    restored = client.get("/live/state").json()["cursor"]
    assert restored["itemIdx"] == 0 and restored["recording"] == "idle"
    assert restored["wseq"] > client.get("/live/state").json()["session"]["wseq"]
    assert client.get("/sessions/S-RT-1/runtime").json()["cursor"]["wseq"] == restored["wseq"]


def test_pause_resume_persists_and_never_auto_rearms_microphone(client):
    _patient(client, "P-PAUSE")
    _training_session(client, "S-PAUSE", "P-PAUSE")
    _handshake(client, "S-PAUSE", wseq=100)
    client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-PAUSE", screen="record", recording="armed", selfStart=True, wseq=101)})

    paused = client.post("/sessions/S-PAUSE/pause")
    assert paused.status_code == 200 and paused.json()["status"] == "paused"
    paused_live = client.get("/live/state").json()["cursor"]
    assert paused_live["screen"] == "paused"
    assert paused_live["recording"] == "stopped"
    assert paused_live["selfStart"] is False
    assert paused_live["wseq"] > 101
    paused_runtime = client.get("/sessions/S-PAUSE/runtime").json()["cursor"]
    assert paused_runtime["screen"] == "present"
    assert paused_runtime["recording"] == "idle"
    assert paused_runtime["wseq"] == paused_live["wseq"]

    assert client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-PAUSE", item_idx=1, wseq=paused_live["wseq"] + 1)}).status_code == 409
    assert client.put("/sessions/S-PAUSE/runtime/cursor", json=_cursor(
        "S-PAUSE", item_idx=1, wseq=paused_live["wseq"] + 1)).status_code == 409

    resumed = client.post("/sessions/S-PAUSE/resume")
    assert resumed.status_code == 200 and resumed.json()["status"] == "active"
    resumed_live = client.get("/live/state").json()["cursor"]
    assert resumed_live["screen"] == "present"
    assert resumed_live["recording"] == "idle"
    assert resumed_live["selfStart"] is False
    assert resumed_live["wseq"] > paused_live["wseq"]
    assert resumed.json()["cursor"]["wseq"] == resumed_live["wseq"]

    # 幂等重试不会重复推进 runtime revision。
    revision = resumed.json()["revision"]
    assert client.post("/sessions/S-PAUSE/resume").json()["revision"] == revision


def test_pause_before_first_cursor_still_projects_a_patient_rest_screen(client):
    _patient(client, "P-EARLY-PAUSE")
    _training_session(client, "S-EARLY-PAUSE", "P-EARLY-PAUSE")
    handshake = _handshake(client, "S-EARLY-PAUSE")
    assert handshake.status_code == 200

    paused = client.post("/sessions/S-EARLY-PAUSE/pause")
    assert paused.status_code == 200 and paused.json()["status"] == "paused"
    live = client.get("/live/state").json()
    assert live["cursor"] is None and live["rapportStep"] is None
    assert live["session"]["paused"] is True
    assert live["session"]["wseq"] > handshake.json()["wseq"]

    resumed = client.post("/sessions/S-EARLY-PAUSE/resume")
    assert resumed.status_code == 200 and resumed.json()["status"] == "active"
    live_after = client.get("/live/state").json()
    assert live_after["session"]["paused"] is False
    assert live_after["session"]["wseq"] > live["session"]["wseq"]

    # 更窄的竞态：pause HTTP 比首个 session 握手先到。后到握手仍须投影休息屏。
    _training_session(client, "S-PAUSE-BEFORE-HS", "P-EARLY-PAUSE")
    assert client.post("/sessions/S-PAUSE-BEFORE-HS/pause").status_code == 200
    late_handshake = _handshake(client, "S-PAUSE-BEFORE-HS")
    assert late_handshake.status_code == 200
    late_live = client.get("/live/state").json()
    assert late_live["session"]["sessionId"] == "S-PAUSE-BEFORE-HS"
    assert late_live["session"]["paused"] is True
    assert late_live["cursor"] is None and late_live["rapportStep"] is None


def test_live_payload_contract_rejects_extra_content_and_public_projection_is_minimal(client):
    _patient(client, "P-MIN-LIVE")
    _training_session(client, "S-MIN-LIVE", "P-MIN-LIVE")
    leaked_session = _handshake(client, "S-MIN-LIVE")
    # 重发一份带研究内容的握手：边界应在入库前拒绝，而不是只靠 GET 隐藏。
    rejected_session = client.put("/live/state", json={"kind": "session", "payload": {
        "sessionId": "S-MIN-LIVE", "weekNo": 2, "mode": "task",
        "wseq": 2, "patientName": "不应进入 live",
    }})
    assert leaked_session.status_code == 200 and rejected_session.status_code == 422

    rejected_cursor = client.put("/live/state", json={"kind": "cursor", "payload": {
        **_cursor("S-MIN-LIVE"), "answerText": "敏感回答",
    }})
    assert rejected_cursor.status_code == 422

    accepted = client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-MIN-LIVE")})
    assert accepted.status_code == 200
    public = client.get("/live/state").json()
    assert set(public["session"]) == {
        "sessionId", "weekNo", "eventLine", "mode", "itemBankVersionId", "wseq",
    }
    assert "sourceWseq" not in public["cursor"] and "answerText" not in public["cursor"]


def test_runtime_recording_commands_obey_consent_and_score_lock_gates(client):
    _patient(client, "P-NOREC", recording_allowed=False)
    _training_session(client, "S-NOREC", "P-NOREC")
    _handshake(client, "S-NOREC")
    assert client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-NOREC", recording="armed")}).status_code == 409
    assert client.put("/sessions/S-NOREC/runtime/cursor", json=_cursor(
        "S-NOREC", selfStart=True)).status_code == 409

    _patient(client, "P-LOCK")
    _training_session(client, "S-LOCK", "P-LOCK")
    _handshake(client, "S-LOCK")
    item = client.post("/sessions/S-LOCK/items", json={
        "item_id": "SE_锚", "task_type": "单要素",
    }).json()
    turn = client.post(f"/items/{item['id']}/turns", json={
        "turn_seq": 1, "response_role": "命名", "prompt_level": 0,
    }).json()
    client.patch(f"/turns/{turn['id']}/confirm", json={"confirmed_response_text": "锚"})
    assert client.patch(f"/turns/{turn['id']}/lock", json={
        "reviewer_id": "R1", "element_value": 1, "prompt_level": 0,
    }).status_code == 200

    assert client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-LOCK", item_idx=2, recording="armed")}).status_code == 409
    assert client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-LOCK", item_idx=2)}).status_code == 200
    client.post("/sessions/S-LOCK/pause")
    client.post("/sessions/S-LOCK/resume")
    # 暂停/恢复只改运行游标，既不解锁也不允许改写已锁定回答。
    assert client.patch(f"/turns/{turn['id']}/confirm", json={
        "confirmed_response_text": "改写",
    }).status_code == 409


def test_relationship_pointer_is_persisted_and_pause_safe(client):
    _patient(client, "P-RAP")
    response = client.post("/sessions", json={
        "session_id": "S-RAP", "patient_id": "P-RAP", "week_no": 1,
        "phase_type": "关系建立", "event_line": "关系建立环节",
        "item_bank_version_id": BANK_VERSION,
    })
    assert response.status_code == 200
    assert _handshake(client, "S-RAP", mode="rapport", wseq=100).status_code == 200
    rapport = {"sessionId": "S-RAP", "sectionKey": "自我介绍", "questionIdx": 1,
               "recording": "armed", "recSeq": 1, "wseq": 101}
    rapport_write = client.put("/live/state", json={"kind": "rapportStep", "payload": rapport})
    assert rapport_write.status_code == 200
    saved = client.get("/sessions/S-RAP/runtime").json()["rapportStep"]
    assert saved["sectionKey"] == "自我介绍" and saved["questionIdx"] == 1
    assert saved["recording"] == "idle"
    assert saved["wseq"] == rapport_write.json()["wseq"]

    client.post("/sessions/S-RAP/pause")
    paused = client.get("/live/state").json()["rapportStep"]
    assert paused["paused"] is True and paused["recording"] == "stopped"
    client.post("/sessions/S-RAP/resume")
    resumed = client.get("/live/state").json()["rapportStep"]
    assert "paused" not in resumed and resumed["recording"] == "idle"


def test_server_wseq_is_monotonic_and_late_cross_session_cursor_is_rejected(client):
    _patient(client, "P-SEQ")
    _training_session(client, "S-SEQ-A", "P-SEQ")
    _training_session(client, "S-SEQ-B", "P-SEQ")

    first = _handshake(client, "S-SEQ-A", wseq=9_999_999_999_999_999)
    assert first.status_code == 200
    first_server_wseq = client.get("/live/state").json()["session"]["wseq"]
    # 服务端必须"重签"而不是"采纳"客户端序号:若采纳了这个天文数字,单调性断言
    # 依然全绿,快钟/恶意客户端就能永久压制之后所有真命令——单调性测不出这种回归。
    assert first_server_wseq < 9_999_999_999_999_999
    low = client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-SEQ-A", wseq=1)})
    lower = client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-SEQ-A", item_idx=1, wseq=0)})
    assert low.status_code == lower.status_code == 200
    assert first_server_wseq < low.json()["wseq"] < lower.json()["wseq"]
    assert client.get("/live/state").json()["cursor"]["wseq"] == lower.json()["wseq"]

    second = _handshake(client, "S-SEQ-B", wseq=0)
    assert second.status_code == 200
    switched = client.get("/live/state").json()
    assert switched["session"]["sessionId"] == "S-SEQ-B"
    assert switched["session"]["wseq"] > lower.json()["wseq"]

    # A 场旧请求迟到时必须拒绝，不能借当前 B 场上下文污染任一 live slot。
    late = client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-SEQ-A", item_idx=2, wseq=99_999_999_999_999_999)})
    assert late.status_code == 409
    assert client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        item_idx=2)}).status_code == 422
    unchanged = client.get("/live/state").json()
    assert unchanged["session"]["sessionId"] == "S-SEQ-B" and unchanged["cursor"] is None

    # 切回 A 时 session 和恢复游标都由服务端重新签发，均高于切场前全部快照。
    assert _handshake(client, "S-SEQ-A", wseq=0).status_code == 200
    restored = client.get("/live/state").json()
    assert restored["session"]["wseq"] > switched["session"]["wseq"]
    assert restored["cursor"]["wseq"] > restored["session"]["wseq"]
    assert restored["cursor"]["itemIdx"] == 1


def test_audio_turn_key_recovers_without_turn_event_and_rejects_unbound_keys(client):
    _patient(client, "P-AUDIO-MAP")
    _training_session(client, "S-AUDIO-MAP", "P-AUDIO-MAP")
    audio = client.post("/audio", json={
        "raw_audio_id": "audio-map", "session_id": "S-AUDIO-MAP", "turn_key": "SE_锚#1",
    })
    assert audio.status_code == 200 and audio.json()["turn_key"] == "SE_锚#1"
    journal = client.get("/sessions/S-AUDIO-MAP/journal").json()
    assert journal["turns"] == []
    assert journal["audios"][0]["turn_key"] == "SE_锚#1"
    assert client.post("/audio", json={
        "raw_audio_id": "audio-bad", "session_id": "S-AUDIO-MAP", "turn_key": "任意文本",
    }).status_code == 422

    relationship = client.post("/sessions", json={
        "session_id": "S-AUDIO-RAP", "patient_id": "P-AUDIO-MAP", "week_no": 1,
        "phase_type": "关系建立", "event_line": "关系建立环节",
        "item_bank_version_id": BANK_VERSION,
    })
    assert relationship.status_code == 200
    assert client.post("/audio", json={
        "raw_audio_id": "audio-rap", "session_id": "S-AUDIO-RAP",
        "turn_key": "关系建立·自我介绍",
    }).status_code == 200
    assert client.post("/audio", json={
        "raw_audio_id": "audio-rap-bad", "session_id": "S-AUDIO-RAP",
        "turn_key": "关系建立·不存在",
    }).status_code == 422


def test_runtime_cursor_expected_revision_blocks_stale_writer(client):
    """旧标签页/旧设备不能凭过期 revision 把游标倒回旧题(乐观并发守卫)。"""
    _patient(client, "P-REV")
    _training_session(client, "S-REV", "P-REV")
    _handshake(client, "S-REV", wseq=100)
    first = client.put("/sessions/S-REV/runtime/cursor", json=_cursor("S-REV", item_idx=2, wseq=101))
    assert first.status_code == 200
    current_revision = first.json()["revision"]

    # 过期 revision 一律 409,游标不动
    stale = client.put("/sessions/S-REV/runtime/cursor", json={
        **_cursor("S-REV", item_idx=0, wseq=102), "expected_revision": current_revision - 1,
    })
    assert stale.status_code == 409
    assert client.get("/sessions/S-REV/runtime").json()["cursor"]["itemIdx"] == 2

    # 带当前 revision 的写入放行
    fresh = client.put("/sessions/S-REV/runtime/cursor", json={
        **_cursor("S-REV", item_idx=3, wseq=103), "expected_revision": current_revision,
    })
    assert fresh.status_code == 200
    assert client.get("/sessions/S-REV/runtime").json()["cursor"]["itemIdx"] == 3


def test_patient_presence_cleared_on_session_switch(client):
    """换场必须清老人端在场信息:上一位受试者的'在线/正在看题'绝不能串到新场次。"""
    _patient(client, "P-SWITCH")
    _training_session(client, "S-SW-1", "P-SWITCH")
    _training_session(client, "S-SW-2", "P-SWITCH")
    _handshake(client, "S-SW-1", wseq=100)
    assert client.post("/live/patient-heartbeat", json={
        "session_id": "S-SW-1", "screen": "present",
    }).status_code == 200
    assert client.get("/live/console-state").json()["patientPresence"]["online"] is True

    _handshake(client, "S-SW-2", wseq=200)
    presence = client.get("/live/console-state").json()["patientPresence"]
    assert presence["session_id"] is None
    assert presence["online"] is False and presence["screen"] is None
    # 旧场次的迟到心跳也进不来
    assert client.post("/live/patient-heartbeat", json={
        "session_id": "S-SW-1", "screen": "present",
    }).status_code == 409


def test_runtime_writes_for_non_live_session_do_not_touch_live_row(client):
    """非当前 live 场次的 runtime 写入/暂停只落自己的恢复行,不得污染老人端正在看的场次。"""
    _patient(client, "P-ISO")
    _training_session(client, "S-ISO-LIVE", "P-ISO")
    _training_session(client, "S-ISO-BG", "P-ISO")
    _handshake(client, "S-ISO-LIVE", wseq=100)
    live_cursor = client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-ISO-LIVE", item_idx=1, wseq=101)})
    assert live_cursor.status_code == 200
    before = client.get("/live/state").json()

    assert client.put("/sessions/S-ISO-BG/runtime/cursor", json=_cursor(
        "S-ISO-BG", item_idx=4, wseq=102)).status_code == 200
    assert client.post("/sessions/S-ISO-BG/pause").status_code == 200

    after = client.get("/live/state").json()
    assert after["session"]["sessionId"] == "S-ISO-LIVE"
    assert after["seq"] == before["seq"]                      # live 没被当成新命令推进
    assert after["cursor"]["itemIdx"] == 1                    # 老人端画面不动
    assert "paused" not in after["session"] or after["session"]["paused"] is not True
    assert client.get("/sessions/S-ISO-BG/runtime").json()["status"] == "paused"


def test_forward_alembic_upgrade_preserves_existing_live_row(tmp_path):
    db_path = tmp_path / "forward.db"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "794480f21ed4")

    eng = sa_create_engine(f"sqlite:///{db_path}")
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO livestate "
            "(id, seq, session_json, cursor_json, rapport_json, audio_json, patient_rec_json, updated_at) "
            "VALUES (1, 7, :session, :cursor, NULL, NULL, NULL, NULL)"
        ), {"session": '{"sessionId":"legacy"}', "cursor": '{"itemIdx":2,"turnIdx":0}'})

    command.upgrade(config, "head")
    columns = {col["name"] for col in inspect(eng).get_columns("livestate")}
    assert {"command_wseq", "patient_ack_session_id", "patient_current_screen",
            "patient_last_seen_at", "patient_ack_seq"} <= columns
    assert "sessionruntimestate" in inspect(eng).get_table_names()
    assert "turn_key" in {col["name"] for col in inspect(eng).get_columns("audioassetrow")}
    with eng.connect() as conn:
        row = conn.execute(text("SELECT seq, session_json, cursor_json FROM livestate WHERE id=1")).one()
    assert row.seq == 7 and "legacy" in row.session_json and "itemIdx" in row.cursor_json
