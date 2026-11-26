"""两床并行 3c:自动带练的热麦检查、服务层握手绑定和 TTS 复核都按场次找自己的床旁槽。

NMU_BEDSIDE_SLOTS=2 时 1 号槽(锚行 id=1)放 A 场、2 号槽(id=2)放 B 场,两场都照
P0a 的冻结身份直接种表。服务层 _require_live_binding 和旧协议恢复的
_legacy_live_binding 用真实实现,不替身;TTS 只替换引擎。
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine, select

from app import (auth, autopilot_orchestration, autopilot_service, content, db,
                 device_capability, provider_readiness, repeat_intent, tts)
from app.main import app
from app.models import (LiveState, Patient, ProviderReadinessProbe,
                        RapportUtteranceEvent, ResearchUser, SessionAutopilotState,
                        SessionRuntimeState, TtsServeEvidence)
from app.models import Session as TrainSession


BANK = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
TWO_ONLY_BANK = replace(
    BANK,
    single_element=BANK.single_element[:2],
    double_element=[],
    multi_element=[],
    meta={**BANK.meta, "source_protocol_position_count": 2,
          "source_unstructured_positions": []},
)
PROTOCOL = content.load_autopilot_protocol(
    content.CONTENT_DIR / "autopilot_protocol_v1.json")
REPEAT_PROTOCOL = repeat_intent.active_protocol()
SESSION_A, SESSION_B = "S-AP-BED-A", "S-AP-BED-B"
BEDS = {SESSION_A: ("P-AP-BED-A", "AP-BED-RES-A", 1),
        SESSION_B: ("P-AP-BED-B", "AP-BED-RES-B", 2)}
_WAV = b"RIFF-two-bedside-tts-audio"


def _handshake(session_id: str, *, week_no: int = 2, event_line: str = "正式训练",
               mode: str = "task") -> str:
    return json.dumps({
        "sessionId": session_id, "weekNo": week_no, "eventLine": event_line,
        "mode": mode, "itemBankVersionId": TWO_ONLY_BANK.version_id,
    }, ensure_ascii=False)


def _login(username: str) -> TestClient:
    client = TestClient(app)
    login = client.post("/auth/login", json={
        "username": username, "password": "password1"})
    assert login.status_code == 200, login.status_code
    client.headers["X-CSRF-Token"] = client.cookies.get(auth.CSRF_COOKIE_NAME)
    return client


def _capability(engine, session_id: str, device_id: str) -> dict[str, str]:
    with Session(engine) as s:
        token, _row = device_capability.create_capability(
            s, session_id=session_id, device_id=device_id)
        s.commit()
    return {"X-Device-Capability": token}


def _seed(engine, now: datetime, readiness_config) -> None:
    with Session(engine) as s:
        for session_id, (patient_id, trainer, _slot) in BEDS.items():
            s.add(Patient(patient_id=patient_id, is_simulation_subject=True,
                          consent_status="已同意", recording_allowed=True))
            s.add(ResearchUser(
                username=trainer.lower(), display_id=trainer,
                password_hash=auth.hash_password("password1"), role="researcher",
                created_at=now))
        s.commit()
        for session_id, (patient_id, trainer, slot_id) in BEDS.items():
            s.add(TrainSession(
                session_id=session_id, patient_id=patient_id, week_no=2,
                phase_type="正式训练", event_line="正式训练",
                item_bank_version_id=TWO_ONLY_BANK.version_id,
                item_bank_definition_digest=content.item_bank_definition_digest(
                    TWO_ONLY_BANK),
                autopilot_protocol_version_id=PROTOCOL["protocol_version_id"],
                autopilot_protocol_definition_digest=(
                    content.autopilot_protocol_definition_digest(PROTOCOL)),
                repeat_protocol_version_id=REPEAT_PROTOCOL.version_id,
                repeat_protocol_definition_digest=REPEAT_PROTOCOL.definition_digest,
                is_simulation=True, data_classification="simulation",
                trainer_id=trainer,
            ))
        s.commit()
        for session_id, (_patient, _trainer, slot_id) in BEDS.items():
            s.add(LiveState(id=slot_id, seq=1, session_json=_handshake(session_id),
                            updated_at=now))
            s.add(SessionRuntimeState(
                session_id=session_id, status="active", revision=0, updated_at=now))
        s.add(ProviderReadinessProbe(
            probe_id="prb_two_bedside_autopilot",
            schema_version=provider_readiness.SCHEMA_VERSION,
            runtime_contract=provider_readiness.RUNTIME_CONTRACT,
            config_fingerprint=readiness_config.fingerprint,
            tts_engine_version=readiness_config.tts_engine_version,
            asr_engine_version=readiness_config.asr_engine_version,
            llm_engine_version=readiness_config.llm_engine_version,
            tts_required=True, tts_success=True,
            asr_required=True, asr_success=True,
            llm_required=False, llm_configured=readiness_config.llm_configured,
            llm_success=readiness_config.llm_configured,
            llm_failure_code=(None if readiness_config.llm_configured
                              else "llm_not_required_not_configured"),
            required_capabilities_ready=True, all_configured_capabilities_ready=True,
            checked_at=now, expires_at=now + timedelta(hours=1),
            actor_display_id="AP-BED-RES-A",
        ))
        s.commit()


def _autopilot_beds(monkeypatch, tmp_path, slots: str | None):
    if slots is None:
        monkeypatch.delenv("NMU_BEDSIDE_SLOTS", raising=False)
    else:
        monkeypatch.setenv("NMU_BEDSIDE_SLOTS", slots)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'two-bed-autopilot.sqlite'}",
        connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(content, "load_item_bank", lambda _path: TWO_ONLY_BANK)
    monkeypatch.setattr(autopilot_orchestration, "submit",
                        lambda _session_id, _worker: True)
    monkeypatch.setenv("ENABLE_AUTOPILOT_P0A_SIMULATION", "1")
    monkeypatch.setenv("ALLOW_SIMULATION_DATA", "1")
    SQLModel.metadata.create_all(engine)
    # 就绪闸按 naive-UTC 比较,照 test_autopilot_api 的写法取 UTC。
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    readiness_config = provider_readiness.capture_configuration()
    monkeypatch.setattr(provider_readiness, "capture_configuration",
                        lambda **_kwargs: readiness_config)
    _seed(engine, now, readiness_config)
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    return SimpleNamespace(
        engine=engine,
        device=TestClient(app),
        researcher={SESSION_A: _login("ap-bed-res-a"),
                    SESSION_B: _login("ap-bed-res-b")},
        tablet={SESSION_A: _capability(engine, SESSION_A, "ap-bed-tablet-a-0001"),
                SESSION_B: _capability(engine, SESSION_B, "ap-bed-tablet-b-0001")},
    )


def _close(beds) -> None:
    for client in (beds.device, *beds.researcher.values()):
        client.close()
    beds.engine.dispose()


@pytest.fixture
def beds(monkeypatch, tmp_path):
    value = _autopilot_beds(monkeypatch, tmp_path, "2")
    yield value
    _close(value)


@pytest.fixture
def single_slot_beds(monkeypatch, tmp_path):
    value = _autopilot_beds(monkeypatch, tmp_path, None)
    yield value
    _close(value)


def _raw_slot(engine, slot_id: int):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM livestate WHERE id = :id"), {"id": slot_id}).one_or_none()
    return None if row is None else tuple(row)


def _slot(engine, slot_id: int) -> LiveState:
    with Session(engine) as s:
        row = s.get(LiveState, slot_id)
        s.expunge(row)
    return row


def _hot_mic(engine, slot_id: int) -> None:
    with Session(engine) as s:
        row = s.get(LiveState, slot_id)
        row.patient_rec_json = json.dumps({"active": True})
        s.add(row)
        s.commit()


def _bump_slot(engine, slot_id: int) -> None:
    """模拟这一槽的场次又写了一拍:seq 前进。"""
    with Session(engine) as s:
        row = s.get(LiveState, slot_id)
        row.seq += 1
        row.updated_at = datetime.now()
        s.add(row)
        s.commit()


def _code(response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return detail.get("code")
    return body.get("code") if isinstance(body, dict) else None


def _start(beds, session_id: str):
    return beds.researcher[session_id].post(
        f"/sessions/{session_id}/autopilot/start",
        json={"idempotency_key": f"start-{session_id.lower()}-0001",
              "expected_revision": 0})


def _ack_body(command: dict, *, ack_type: str, key: str, device_event_seq: int,
              **facts) -> dict:
    return {
        "idempotency_key": key, "ack_type": ack_type,
        "control_generation": command["control_generation"],
        "runner_generation": command["runner_generation"],
        "command_revision": command["command_revision"],
        "device_event_seq": device_event_seq, **facts,
    }


# ---------------- 热麦检查只看本场的槽 ----------------

@pytest.mark.parametrize(("session_id", "other"), [(SESSION_B, 1), (SESSION_A, 2)])
def test_other_slot_hot_mic_does_not_block_start(beds, session_id, other):
    _hot_mic(beds.engine, other)
    untouched = _raw_slot(beds.engine, other)

    started = _start(beds, session_id)

    assert started.status_code == 200, _code(started)
    assert (started.json()["mode"], started.json()["status"]) == (
        "autonomous", "waiting_tts")
    assert _raw_slot(beds.engine, other) == untouched


@pytest.mark.parametrize(("session_id", "own"), [(SESSION_B, 2), (SESSION_A, 1)])
def test_own_slot_hot_mic_blocks_its_own_start(beds, session_id, own):
    _hot_mic(beds.engine, own)
    before = (_raw_slot(beds.engine, 1), _raw_slot(beds.engine, 2))

    started = _start(beds, session_id)

    assert (started.status_code, _code(started)) == (
        409, "autopilot_patient_microphone_active")
    with Session(beds.engine) as s:
        assert s.get(SessionAutopilotState, session_id) is None
    assert (_raw_slot(beds.engine, 1), _raw_slot(beds.engine, 2)) == before


def test_single_slot_autopilot_never_reads_slot_two(single_slot_beds):
    """开关不开时 2 号行在库里也不读:B 场照今天被握手绑定拦下,A 场不受 2 号槽热麦影响。"""
    beds = single_slot_beds
    _hot_mic(beds.engine, 2)
    before = _raw_slot(beds.engine, 2)

    started_b = _start(beds, SESSION_B)
    voice_b = beds.device.get(f"/sessions/{SESSION_B}/autopilot/voice-settings",
                              headers=beds.tablet[SESSION_B])
    started_a = _start(beds, SESSION_A)

    assert (started_b.status_code, _code(started_b)) == (
        409, "autopilot_live_session_mismatch")
    assert (voice_b.status_code, _code(voice_b)) == (409, "device_session_changed")
    assert started_a.status_code == 200, _code(started_a)
    assert _raw_slot(beds.engine, 2) == before


# ---------------- 2 号槽完整走一遍自动带练,1 号槽一字节不动 ----------------

def test_slot_two_autopilot_journey_never_touches_slot_one(beds, monkeypatch):
    monkeypatch.setattr(tts, "speak_autopilot",
                        lambda _text: (_WAV, "two-bed-tts-test", False))
    # 1 号槽热麦:哪一步误读锚行,就会被 autopilot_patient_microphone_active
    # 或 device_session_changed 拦下。
    _hot_mic(beds.engine, 1)
    slot_one = _raw_slot(beds.engine, 1)
    researcher, tablet, device = beds.researcher[SESSION_B], beds.tablet[SESSION_B], beds.device
    base = f"/sessions/{SESSION_B}/autopilot"

    def drain() -> int:
        target = device.get(f"{base}/drain-target", headers=tablet)
        assert target.status_code == 200, _code(target)
        drained = device.post(
            f"{base}/commands/{target.json()['command_key']}/drain-ack", headers=tablet)
        assert drained.status_code == 200, _code(drained)
        return drained.json()["state_revision"]

    started = _start(beds, SESSION_B)
    assert started.status_code == 200, _code(started)
    question = device.get(f"{base}/next", headers=tablet)
    assert question.status_code == 200, _code(question)
    command = question.json()
    assert command["kind"] == "tts"
    voice = device.get(f"{base}/voice-settings", headers=tablet)
    assert voice.status_code == 200, _code(voice)
    spoken = device.post(f"{base}/commands/{command['command_key']}/tts", headers=tablet)
    assert spoken.status_code == 200, _code(spoken)
    assert spoken.content == _WAV

    ack_path = f"{base}/commands/{command['command_key']}/acks"
    tts_started = device.post(ack_path, headers=tablet, json=_ack_body(
        command, ack_type="tts_started", key="ack-two-bed-started-0001",
        device_event_seq=1))
    assert tts_started.status_code == 200, _code(tts_started)
    tts_ended = device.post(ack_path, headers=tablet, json=_ack_body(
        tts_started.json()["command"], ack_type="tts_ended",
        key="ack-two-bed-ended-0001", device_event_seq=2,
        media_ended=True, media_duration_ms=900))
    assert tts_ended.status_code == 200, _code(tts_ended)
    record = tts_ended.json()["command"]
    assert record["kind"] == "record"
    authorized = device.post(
        f"{base}/commands/{record['command_key']}/recording-authorization",
        headers=tablet)
    assert authorized.status_code == 200, _code(authorized)

    assert researcher.post(f"/sessions/{SESSION_B}/pause").status_code == 200
    resumed = researcher.post(f"{base}/resume", json={
        "idempotency_key": "resume-two-bed-b-0001", "expected_revision": drain()})
    assert resumed.status_code == 200, _code(resumed)
    assert resumed.json()["status"] == "waiting_tts"
    slot_two = _slot(beds.engine, 2)
    assert json.loads(slot_two.session_json)["paused"] is False
    assert slot_two.patient_rec_json is None
    reissued = device.get(f"{base}/next", headers=tablet)
    assert reissued.status_code == 200, _code(reissued)
    assert reissued.json()["kind"] == "tts"

    assert researcher.post(f"/sessions/{SESSION_B}/pause").status_code == 200
    adjudicated = researcher.post(f"{base}/adjudicate", json={
        "idempotency_key": "adjudicate-two-bed-b-0001", "expected_revision": drain(),
        "kind": "skip_item", "reason_code": "participant_declined"})
    assert adjudicated.status_code == 200, _code(adjudicated)
    assert adjudicated.json()["position_item_id"] == (
        TWO_ONLY_BANK.single_element[1]["item_id"])

    assert _raw_slot(beds.engine, 1) == slot_one


# ---------------- TTS 合成后的复核只看本场的槽 ----------------

@pytest.mark.parametrize(("written_slot", "served"), [(1, True), (2, False)])
def test_slot_two_exact_tts_is_fenced_only_by_its_own_slot(
        beds, monkeypatch, written_slot, served):
    def speak_while_a_slot_moves(_text):
        _bump_slot(beds.engine, written_slot)
        return _WAV, "two-bed-tts-test", False

    monkeypatch.setattr(tts, "speak_autopilot", speak_while_a_slot_moves)
    started = _start(beds, SESSION_B)
    assert started.status_code == 200, _code(started)
    command = beds.device.get(f"/sessions/{SESSION_B}/autopilot/next",
                              headers=beds.tablet[SESSION_B]).json()

    spoken = beds.device.post(
        f"/sessions/{SESSION_B}/autopilot/commands/{command['command_key']}/tts",
        headers=beds.tablet[SESSION_B])

    with Session(beds.engine) as s:
        evidence = list(s.exec(select(TtsServeEvidence)))
    if served:
        assert spoken.status_code == 200, _code(spoken)
        assert spoken.content == _WAV
        assert [row.session_id for row in evidence] == [SESSION_B]
    else:
        assert (spoken.status_code, _code(spoken)) == (409, "tts_authorization_changed")
        assert _WAV not in spoken.content
        assert evidence == []


def test_slot_two_generic_tts_reads_the_tablets_own_slot(beds, monkeypatch):
    def speak_while_slot_one_moves(_text):
        _bump_slot(beds.engine, 1)
        return _WAV, "two-bed-generic-tts", False

    monkeypatch.setattr(tts, "speak", speak_while_slot_one_moves)

    spoken = beds.device.post("/tts/speak", headers=beds.tablet[SESSION_B],
                              json={"text": "请您看着屏幕"})

    assert spoken.status_code == 200, _code(spoken)
    assert spoken.content == _WAV


def test_slot_two_rapport_utterance_tts_reads_its_own_slot(beds, monkeypatch):
    session_id, patient_id = "S-AP-BED-W1", "P-AP-BED-W1"
    now = datetime.now()
    with Session(beds.engine) as s:
        s.add(Patient(patient_id=patient_id, is_simulation_subject=True,
                      consent_status="已同意", recording_allowed=True))
        s.commit()
        s.add(TrainSession(
            session_id=session_id, patient_id=patient_id, week_no=1,
            phase_type="关系建立", event_line="关系建立环节",
            item_bank_version_id=TWO_ONLY_BANK.version_id, is_simulation=True,
            data_classification="simulation", trainer_id="AP-BED-RES-B"))
        s.commit()
        line = "这里是我们一起聊天的地方"
        utterance = RapportUtteranceEvent(
            session_id=session_id, event_seq=1, section_key="介绍机构环境",
            question_idx=0, source="bank", origin="manual", reply_id="g1",
            text=line, text_sha256=hashlib.sha256(line.encode()).hexdigest(),
            is_simulation=True)
        s.add(utterance)
        s.add(SessionRuntimeState(
            session_id=session_id, status="active", revision=0, updated_at=now))
        s.commit()
        utterance_id = utterance.id
        slot_two = s.get(LiveState, 2)
        slot_two.session_json = _handshake(
            session_id, week_no=1, event_line="关系建立环节", mode="rapport")
        slot_two.rapport_json = json.dumps({
            "sessionId": session_id, "sectionKey": "介绍机构环境", "questionIdx": 0,
            "beat": "reply", "utteranceId": utterance_id, "recording": "idle",
        }, ensure_ascii=False)
        s.add(slot_two)
        s.commit()
    tablet = _capability(beds.engine, session_id, "ap-bed-tablet-w1-0001")

    def speak_while_slot_one_moves(_text):
        _bump_slot(beds.engine, 1)
        return _WAV, "two-bed-rapport-tts", False

    monkeypatch.setattr(tts, "speak_rapport_utterance", speak_while_slot_one_moves)

    spoken = beds.device.post(
        f"/sessions/{session_id}/rapport/utterances/{utterance_id}/tts", headers=tablet)

    assert spoken.status_code == 200, _code(spoken)
    assert spoken.content == _WAV
    with Session(beds.engine) as s:
        evidence = list(s.exec(select(TtsServeEvidence)))
    assert [(row.session_id, row.utterance_id) for row in evidence] == [
        (session_id, utterance_id)]


# ---------------- 服务层握手绑定:字段与指纹不因槽位而变 ----------------

def test_live_binding_from_slot_two_keeps_five_fields_and_fingerprint(
        beds, monkeypatch):
    with Session(beds.engine) as s:
        train_session = s.get(TrainSession, SESSION_B)
        autopilot_service._require_live_binding(s, train_session)  # noqa: SLF001
        from_slot_two = autopilot_orchestration._legacy_live_binding(  # noqa: SLF001
            s, train_session)

    # 同一场照今天单槽放进 1 号槽再取一次:绑定逐字段相同,证据指纹里的 live 段
    # 就是这份字典,所以指纹不因槽位而变。
    monkeypatch.delenv("NMU_BEDSIDE_SLOTS")
    with Session(beds.engine) as s:
        one, two = s.get(LiveState, 1), s.get(LiveState, 2)
        one.session_json, two.session_json = two.session_json, None
        s.add_all([one, two])
        s.commit()
    with Session(beds.engine) as s:
        from_slot_one = autopilot_orchestration._legacy_live_binding(  # noqa: SLF001
            s, s.get(TrainSession, SESSION_B))

    assert list(from_slot_two) == [
        "sessionId", "weekNo", "eventLine", "itemBankVersionId", "mode"]
    assert from_slot_two == from_slot_one
    digest = autopilot_orchestration._canonical_digest  # noqa: SLF001
    assert digest({"live": from_slot_two}) == digest({"live": from_slot_one})


def test_single_slot_live_binding_never_reads_slot_two(single_slot_beds):
    with Session(single_slot_beds.engine) as s:
        train_session = s.get(TrainSession, SESSION_B)
        with pytest.raises(autopilot_service.AutopilotServiceError) as service_error:
            autopilot_service._require_live_binding(s, train_session)  # noqa: SLF001
        with pytest.raises(autopilot_orchestration.AutopilotOrchestrationError) as legacy:
            autopilot_orchestration._legacy_live_binding(s, train_session)  # noqa: SLF001

    assert service_error.value.code == "autopilot_live_session_mismatch"
    assert legacy.value.code == autopilot_orchestration.LEGACY_RECOVERY_INVALID
