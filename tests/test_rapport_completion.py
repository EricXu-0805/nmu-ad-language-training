"""第 1 周关系建立独立完成合同(收据 148 D1)。

床旁结束与最终完成共用同一 rapport 证据判定:停在道别节、录音指令已收回、
全部录音通过绑定/分类/红线/字节核验、场次零评分行。
"""
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from starlette.testclient import TestClient

from app import db as app_db
from app import content, session_completion
from app.main import app, get_session
from app.models import AudioAssetRow, ItemEvent

@pytest.fixture(autouse=True)
def _isolate_app_overrides():
    yield
    app.dependency_overrides.clear()


BANK_VERSION = content.load_item_bank_for_week(2).version_id
SCRIPT = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
FAREWELL_KEY = SCRIPT["sections"][-1]["key"]
IDENTITY_KEY = session_completion.RAPPORT_IDENTITY_SECTION_KEY


def _client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    client = TestClient(app)
    client.test_engine = engine
    return client, engine


def _setup(session_id: str = "S-RAPPORT") -> TestClient:
    """匿名 local-M0 驱动:新录音登记只属于配对设备或本地开发模式。"""
    client, _engine = _client()
    app_db.engine = client.test_engine
    assert client.post("/patients", json={
        "patient_id": "P-RAPPORT",
        "is_simulation_subject": True,
        "consent_status": "已同意",
        "consent_type": "本人同意",
        "mandarin_eligible": True,
        "recording_allowed": True,
    }).status_code == 200
    response = client.post("/sessions", json={
        "session_id": session_id,
        "patient_id": "P-RAPPORT",
        "week_no": 1,
        "phase_type": "关系建立",
        "event_line": "关系建立环节",
        "item_bank_version_id": BANK_VERSION,
        "is_simulation": True,
    })
    assert response.status_code == 200, response.text
    handshake = client.put("/live/state", json={"kind": "session", "payload": {
        "sessionId": session_id,
        "weekNo": 1,
        "eventLine": "关系建立环节",
        "mode": "rapport",
        "itemBankVersionId": BANK_VERSION,
    }})
    assert handshake.status_code == 200, handshake.text
    return client


def _rapport(client: TestClient, section_key: str, *,
             question_idx: int = 0, recording: str = "idle",
             session_id: str = "S-RAPPORT") -> None:
    response = client.put("/live/state", json={"kind": "rapportStep", "payload": {
        "sessionId": session_id,
        "sectionKey": section_key,
        "questionIdx": question_idx,
        "recording": recording,
        "recSeq": 1,
        "containsDirectIdentifier": section_key == IDENTITY_KEY,
    }})
    assert response.status_code == 200, response.text


def _upload_rapport_audio(
        client: TestClient, raw_audio_id: str, section_key: str, *,
        contains_direct_identifier: bool,
        session_id: str = "S-RAPPORT") -> None:
    assert client.post("/audio", json={
        "raw_audio_id": raw_audio_id,
        "session_id": session_id,
        "turn_key": f"关系建立·{section_key}",
        "contains_direct_identifier": contains_direct_identifier,
    }).status_code == 200
    assert client.put(
        f"/audio/{raw_audio_id}/blob",
        content=b"\x1a\x45\xdf\xa3rapport-audio" + raw_audio_id.encode(),
        headers={"content-type": "audio/webm"},
    ).status_code == 200


def _finish(client: TestClient, session_id: str = "S-RAPPORT"):
    return client.post(f"/sessions/{session_id}/finish-intervention")


def _closeout(client: TestClient, key: str,
              session_id: str = "S-RAPPORT") -> None:
    response = client.put(f"/sessions/{session_id}/closeout", json={
        "idempotency_key": key,
        "expected_revision": 0,
        "report_status": "no_additional_observation",
    })
    assert response.status_code == 200, response.text


def _issue_codes(response) -> set[str]:
    detail = response.json()["detail"]
    return {issue["code"] for issue in detail["assessment"]["issues"]}


def test_rapport_full_chain_finishes_and_completes_at_farewell():
    client = _setup()
    _rapport(client, IDENTITY_KEY, question_idx=1)
    _upload_rapport_audio(
        client, "rap-audio-intro", IDENTITY_KEY, contains_direct_identifier=True)

    early = _finish(client)
    assert early.status_code == 409, early.text
    assert "rapport_not_at_farewell" in _issue_codes(early)
    assert early.json()["detail"]["assessment"]["protocol"] == "rapport"

    _rapport(client, FAREWELL_KEY)
    finished = _finish(client)
    assert finished.status_code == 200, finished.text
    assessment = finished.json()["interventionAssessment"]
    assert assessment["protocol"] == "rapport"
    assert assessment["ready"] is True
    assert assessment["audio_total"] == 1
    assert assessment["audio_verified"] == 1

    _closeout(client, "rapport-closeout-0001")
    completed = client.post("/sessions/S-RAPPORT/complete")
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "completed"
    assert completed.json()["completionAssessment"]["protocol"] == "rapport"

    summary = client.get("/sessions/S-RAPPORT/outcome-summary")
    assert summary.status_code == 200, summary.text
    assert summary.json()["expected_turns"] == 0


def test_armed_recording_command_blocks_rapport_finish():
    client = _setup()
    _rapport(client, FAREWELL_KEY, recording="armed")
    denied = _finish(client)
    assert denied.status_code == 409, denied.text
    assert "rapport_recording_active" in _issue_codes(denied)
    # 收回录音指令后即可结束。
    _rapport(client, FAREWELL_KEY)
    assert _finish(client).status_code == 200


def test_missing_position_and_identity_flag_fail_closed():
    client = _setup()
    # 从未广播过 rapport 位置 → 位置缺失。
    denied = _finish(client)
    assert denied.status_code == 409
    assert "rapport_position_missing" in _issue_codes(denied)

    # 自我介绍节录音未整段标记直接标识 → 红线钉。
    _upload_rapport_audio(
        client, "rap-audio-unflagged", IDENTITY_KEY,
        contains_direct_identifier=False)
    _rapport(client, FAREWELL_KEY)
    denied = _finish(client)
    assert denied.status_code == 409
    assert "rapport_identity_flag_missing" in _issue_codes(denied)


def test_forged_scoring_rows_and_foreign_turn_keys_block_completion():
    client = _setup()
    _rapport(client, FAREWELL_KEY)
    with Session(client.test_engine) as session:
        session.add(ItemEvent(
            session_id="S-RAPPORT", item_id="SE_伪造", task_type="单要素",
            item_set_type="训练集", presentation_order=1,
        ))
        session.add(AudioAssetRow(
            raw_audio_id="rap-audio-forged-key",
            session_id="S-RAPPORT",
            turn_key="SE_伪造#1",
            is_simulation=True,
            data_classification="simulation",
            byte_count=16,
            checksum="0" * 64,
        ))
        session.commit()
    denied = _finish(client)
    assert denied.status_code == 409
    codes = _issue_codes(denied)
    assert "rapport_unexpected_scoring_rows" in codes
    assert "rapport_audio_key_invalid" in codes


def test_forged_attempt_rows_block_even_after_intervention_completed():
    """finish 与 complete 共用同一判定:两终态之间伪造的 AI 处理行也拦。"""
    from app.models import AttemptEvent

    client = _setup()
    _rapport(client, FAREWELL_KEY)
    assert _finish(client).status_code == 200
    with Session(client.test_engine) as session:
        session.add(AttemptEvent(
            session_id="S-RAPPORT", item_id="SE_伪造", turn_seq=1,
            response_role="命名", attempt_seq=1, raw_audio_id="rap-none",
            prompt_level=0, asr_text="x", asr_confidence=0.5,
            asr_engine_version="forged", operational_answer_type="正确",
            operational_score=1, operational_needs_review=False,
            judge_mode="规则确定式", judge_engine_version="forged",
            processing_status="completed", is_simulation=True,
        ))
        session.commit()
    _closeout(client, "rapport-closeout-forged-attempt")
    denied = client.post("/sessions/S-RAPPORT/complete")
    assert denied.status_code == 409, denied.text
    assert "rapport_unexpected_scoring_rows" in _issue_codes(denied)


def test_registered_but_never_uploaded_audio_does_not_wedge_completion():
    """崩溃在字节上传前的登记行不构成服务端证据,也不永久卡死场次。"""
    client = _setup()
    _rapport(client, FAREWELL_KEY)
    with Session(client.test_engine) as session:
        session.add(AudioAssetRow(
            raw_audio_id="rap-audio-never-uploaded",
            session_id="S-RAPPORT",
            turn_key=f"关系建立·{FAREWELL_KEY}",
            is_simulation=True,
            data_classification="simulation",
        ))
        session.commit()
    finished = _finish(client)
    assert finished.status_code == 200, finished.text
    assessment = finished.json()["interventionAssessment"]
    assert assessment["audio_total"] == 0


def test_displaced_live_slot_fails_closed_on_recording_truth():
    """收麦证据只在 live 槽:槽被另一场次接管后,不可读=未收口,拒绝结束。"""
    client = _setup()
    _rapport(client, FAREWELL_KEY)
    assert client.post("/patients", json={
        "patient_id": "P-RAPPORT-B",
        "is_simulation_subject": True,
        "consent_status": "已同意",
        "consent_type": "本人同意",
        "mandarin_eligible": True,
        "recording_allowed": True,
    }).status_code == 200
    assert client.post("/sessions", json={
        "session_id": "S-RAPPORT-B",
        "patient_id": "P-RAPPORT-B",
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "item_bank_version_id": BANK_VERSION,
        "is_simulation": True,
    }).status_code == 200
    assert client.put("/live/state", json={"kind": "session", "payload": {
        "sessionId": "S-RAPPORT-B",
        "weekNo": 2,
        "eventLine": "正式训练",
        "mode": "task",
        "itemBankVersionId": BANK_VERSION,
    }}).status_code == 200

    denied = _finish(client)
    assert denied.status_code == 409, denied.text
    assert "rapport_recording_active" in _issue_codes(denied)

    # 接回本场次并确认收麦后即可结束。
    assert client.put("/live/state", json={"kind": "session", "payload": {
        "sessionId": "S-RAPPORT",
        "weekNo": 1,
        "eventLine": "关系建立环节",
        "mode": "rapport",
        "itemBankVersionId": BANK_VERSION,
    }}).status_code == 200
    _rapport(client, FAREWELL_KEY)
    assert _finish(client).status_code == 200


def test_week1_script_schema_pins_the_completion_contract_structure(tmp_path):
    """道别末节与自我介绍节在装载层钉死:坏档在准入前失败,不会卡死真实场次。"""
    import json as _json

    data = _json.loads(
        (content.CONTENT_DIR / "week1_script.json").read_text(encoding="utf-8"))
    reordered = dict(data)
    reordered["sections"] = [data["sections"][-1]] + data["sections"][:-1]
    bad_farewell = tmp_path / "week1_bad_farewell.json"
    bad_farewell.write_text(
        _json.dumps(reordered, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(content.FrozenContentUnavailable):
        content.load_week1_script(bad_farewell)

    renamed = _json.loads(_json.dumps(data))
    for section in renamed["sections"]:
        if section.get("key") == "自我介绍":
            section["key"] = "自我介绍改名"
    bad_identity = tmp_path / "week1_bad_identity.json"
    bad_identity.write_text(
        _json.dumps(renamed, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(content.FrozenContentUnavailable):
        content.load_week1_script(bad_identity)


def test_assess_rapport_completion_unit_boundaries():
    """脚本坏档/分类漂移/撤回/字节缺失逐项 fail-closed。"""
    class _Audio:
        def __init__(self, **kwargs):
            self.raw_audio_id = kwargs.get("raw_audio_id", "a1")
            self.session_id = kwargs.get("session_id", "S")
            self.turn_key = kwargs.get("turn_key", f"关系建立·{FAREWELL_KEY}")
            self.withdrawn = kwargs.get("withdrawn", False)
            self.withdrawal_status = kwargs.get("withdrawal_status", None)
            self.is_simulation = kwargs.get("is_simulation", True)
            self.data_classification = kwargs.get(
                "data_classification", "simulation")
            self.contains_direct_identifier = kwargs.get(
                "contains_direct_identifier", False)
            self.byte_count = kwargs.get("byte_count", 16)
            self.checksum = kwargs.get("checksum", "0" * 64)

    position = {"sectionKey": FAREWELL_KEY, "questionIdx": 0}

    def assess(script=SCRIPT, rapport=position, live_recording="idle",
               audios=(), items=0, blob=lambda _rid: True):
        return session_completion.assess_rapport_completion(
            script, rapport, live_recording, audios, items,
            session_id="S", is_simulation=True,
            data_classification="simulation", blob_exists=blob)

    assert assess().ready is True

    broken_script = {"sections": SCRIPT["sections"][:-1]}
    result = assess(script=broken_script)
    assert {"rapport_script_invalid"} <= {i.code for i in result.issues}

    result = assess(audios=[_Audio(data_classification="research",
                                   is_simulation=False)])
    assert {"audio_classification_mismatch"} == {i.code for i in result.issues}

    result = assess(audios=[_Audio(withdrawal_status="requested")])
    assert {"audio_withdrawn"} == {i.code for i in result.issues}

    result = assess(audios=[_Audio()], blob=lambda _rid: False)
    assert {"missing_audio_blob"} == {i.code for i in result.issues}

    result = assess(live_recording="recording")
    assert result.ready is False
    assert {"rapport_recording_active"} == {i.code for i in result.issues}

    # 设备麦克风真值回报仍在采集 → 指令面 idle 也不放行。
    result = session_completion.assess_rapport_completion(
        SCRIPT, position, "idle", (), 0,
        session_id="S", is_simulation=True,
        data_classification="simulation", blob_exists=lambda _r: True,
        patient_rec_active=True)
    assert {"rapport_recording_active"} == {i.code for i in result.issues}

    # 伪造 AI 处理行与题目行同罪。
    result = session_completion.assess_rapport_completion(
        SCRIPT, position, "idle", (), 0,
        session_id="S", is_simulation=True,
        data_classification="simulation", blob_exists=lambda _r: True,
        attempt_count=1)
    assert {"rapport_unexpected_scoring_rows"} == {i.code for i in result.issues}

    # 从未携带上传事实的登记行不计入证据、不阻断。
    result = assess(audios=[_Audio(byte_count=None, checksum=None)])
    assert result.ready is True
    assert result.audio_total == 0

    # 停在道别以外的位置(含道别节的非 0 问号)一律不放行。
    result = assess(rapport={"sectionKey": FAREWELL_KEY, "questionIdx": 2})
    assert result.at_farewell is False
