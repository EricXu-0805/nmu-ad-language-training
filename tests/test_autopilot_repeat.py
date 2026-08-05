# ruff: noqa: F811 -- pytest resolves the imported `api_clients` fixture by name;
# the parameter deliberately shadows the import.
"""Explicit repeat: exact replay once, then a safe pause — never an answer.

Every flow here goes through the same public device APIs and the same internal
worker as the ordinary answer path.  Nothing is faked at the boundary that
matters: the repeat decision is made from a real successful ASR transcript.
"""
from __future__ import annotations

import csv
from datetime import timedelta
import hashlib
import json

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import (asr, autopilot_ledger, autopilot_service, export,
                 repeat_intent)
from app import main as main_module
from pathlib import Path

from app.main import _run_p0a_attempt_worker
from app.models import (
    AttemptCaptureProcessing,
    AttemptEvent,
    AudioAssetRow,
    AudioCaptureReceipt,
    Patient,
    AutopilotControlEvent,
    AutopilotRepeatRequest,
    ExportArtifact,
    InteractionEvent,
    RuntimeCommand,
    RuntimeCommandAck,
    Session as TrainSession,
    SessionAutopilotState,
    SessionOutcomeSummary,
    SessionRuntimeState,
    TurnEvent,
    VisitPlan,
)

from test_autopilot_api import (  # noqa: F401 — fixture re-export
    BANK,
    SESSION_ID,
    ApiClients,
    _ack_body,
    _device_next,
    _drive_to_processing_attempt,
    _enable_p0a,
    _start,
    api_clients,
)


class _RepeatAsr:
    """A provider that succeeds and returns exactly what the patient said."""

    version = "repeat-asr-v1"
    data_boundary = "local"
    provider_id = None

    def __init__(self, *texts: str):
        self.texts = list(texts)
        self.calls = 0

    def transcribe(self, _audio_bytes, _hotwords):
        text = self.texts[min(self.calls, len(self.texts) - 1)]
        self.calls += 1
        return asr.AsrResult(text, 0.93, self.version)


def _bind_repeat_protocol(clients: ApiClients) -> repeat_intent.RepeatIntentProtocol:
    """Freeze the approved protocol on the session, as VisitPlan.start does."""
    protocol = repeat_intent.active_protocol()
    with Session(clients.engine) as session:
        row = session.get(TrainSession, SESSION_ID)
        row.repeat_protocol_version_id = protocol.version_id
        row.repeat_protocol_definition_digest = protocol.definition_digest
        session.add(row)
        session.commit()
    return protocol


def _end_tts(clients: ApiClients, tts: dict, *, suffix: str, device_seq: int):
    return clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/{tts['command_key']}/acks",
        headers=clients.device_headers,
        json=_ack_body(
            tts, ack_type="tts_ended", ack_key=f"ack-{suffix}-tts-ended",
            device_event_seq=device_seq, media_ended=True,
            media_duration_ms=900,
        ),
    )


def _complete_record(clients: ApiClients, record: dict, *, suffix: str,
                     device_seq: int, stop_reason: str = "max_duration"):
    raw_audio_id = record["payload"]["raw_audio_id"]
    uploaded = clients.device.put(
        f"/audio/{raw_audio_id}/blob",
        headers={**clients.device_headers, "content-type": "audio/webm"},
        content=b"\x1a\x45\xdf\xa3" + suffix.encode("ascii"),
    )
    assert uploaded.status_code == 200, uploaded.text
    upload_fact = uploaded.json()
    saved = clients.device.put(
        "/live/state",
        headers=clients.device_headers,
        json={
            "kind": "audioSaved",
            "payload": {
                "rawAudioId": raw_audio_id,
                "durationSeconds": 1.5,
                "byteCount": upload_fact["bytes"],
                "checksum": upload_fact["checksum"],
                "turnKey": record["payload"]["turn_ref"],
                "sessionId": SESSION_ID,
                "containsDirectIdentifier": False,
            },
        },
    )
    assert saved.status_code == 200, saved.text
    receipt = saved.json()["audioReceipt"]
    stopped = clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{record['command_key']}/acks",
        headers=clients.device_headers,
        json=_ack_body(
            record, ack_type="record_stopped",
            ack_key=f"ack-{suffix}-record-stopped",
            device_event_seq=device_seq, stop_reason=stop_reason,
            raw_audio_id=raw_audio_id,
            receipt_server_seq=receipt["serverSeq"],
            checksum=upload_fact["checksum"],
            byte_count=upload_fact["bytes"],
            duration_seconds=1.5,
        ),
    )
    assert stopped.status_code == 200, stopped.text
    return stopped.json(), raw_audio_id


def _commands(session: Session) -> list[RuntimeCommand]:
    return list(session.exec(
        select(RuntimeCommand).order_by(RuntimeCommand.command_seq)))


@pytest.mark.parametrize("phrase", ["再说一遍", "我没听清", " 请再说一遍。 "])
def test_first_explicit_repeat_replays_the_exact_question_without_an_attempt(
        api_clients: ApiClients, monkeypatch, phrase):
    """问题播完后说"再说一遍"：重发同一 payload_json，不消费 attempt/cue/提示等级。

    本测试完全不调用 TTS 合成，也不主张音频身份：它证明的是命令层的
    payload_json 逐字节相同、两个摘要都等于这些字节的 SHA-256、解析后
    speech_text 相同。重播的 WAV 由下游 Qwen-only 链路重新合成。
    """
    _enable_p0a(monkeypatch)
    protocol = _bind_repeat_protocol(api_clients)
    capture = _drive_to_processing_attempt(api_clients)
    fake_asr = _RepeatAsr(phrase)
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)

    _run_p0a_attempt_worker(SESSION_ID)

    with Session(api_clients.engine) as session:
        # 没有 attempt、没有 attempt_received/asr_completed 交互证据。
        assert list(session.exec(select(AttemptEvent))) == []
        assert list(session.exec(select(InteractionEvent))) == []
        commands = _commands(session)
        assert [(row.kind, row.state) for row in commands] == [
            ("tts", "succeeded"), ("record", "succeeded"), ("tts", "pending")]
        source, record, replay = commands
        # payload_json 逐字节复制，不是"今天再生成一遍"。
        assert replay.payload_json == source.payload_json
        source_bytes = source.payload_json.encode("utf-8")
        assert replay.payload_json.encode("utf-8") == source_bytes
        assert replay.replay_source_command_id == source.id
        assert replay.replay_ordinal == 1
        # 两个摘要都严格是这些 payload_json UTF-8 字节的 SHA-256，与音频无关。
        expected_digest = hashlib.sha256(source_bytes).hexdigest()
        assert replay.replay_source_payload_sha256 == expected_digest
        request_row = session.exec(select(AutopilotRepeatRequest)).one()
        assert request_row.source_payload_sha256 == expected_digest
        # 冻结话术本身相同——这才是"再说一遍"对受试者的实际含义。
        assert (json.loads(replay.payload_json)["speech_text"]
                == json.loads(source.payload_json)["speech_text"])
        # 新命令身份：新 id / 新幂等键 / 新序号，仍是原 logical slot。
        assert replay.id != source.id
        assert replay.idempotency_key != source.idempotency_key
        assert replay.command_seq == source.command_seq + 2
        assert (replay.attempt_seq, replay.prompt_level) == (
            source.attempt_seq, source.prompt_level)
        assert replay.repeat_protocol_version_id == protocol.version_id
        assert replay.repeat_protocol_definition_digest == (
            protocol.definition_digest)

        request = session.exec(select(AutopilotRepeatRequest)).one()
        assert request.repeat_ordinal == 1
        assert request.outcome == "replayed"
        assert request.replay_command_id == replay.id
        assert request.pause_control_event_seq is None
        assert request.source_tts_command_id == source.id
        assert request.record_command_id == record.id
        assert request.raw_audio_id == capture["raw_audio_id"]
        assert (request.attempt_seq, request.prompt_level) == (1, 0)
        assert request.repeat_protocol_version_id == protocol.version_id
        assert request.repeat_protocol_definition_digest == (
            protocol.definition_digest)
        # 只留封闭短语键与规范化摘要，绝不留原始转写。
        assert request.phrase_key in protocol.phrase_by_normalized_text.values()
        assert len(request.normalized_text_sha256) == 64
        assert phrase.strip() not in json.dumps(
            {column: str(value) for column, value in vars(request).items()
             if not column.startswith("_")}, ensure_ascii=False)

        processing = session.exec(select(AttemptCaptureProcessing)).one()
        assert processing.processing_status == "asr_completed"
        assert processing.disposition == "repeat_replayed"
        assert processing.final_attempt_id is None
        assert processing.repeat_request_id == request.id
        assert processing.asr_engine_version == fake_asr.version
        # 触发录音、原始音频与采集回执全部保留。
        assert session.exec(select(AudioCaptureReceipt)).one().server_seq == (
            capture["receipt_server_seq"])

        control = session.get(SessionAutopilotState, SESSION_ID)
        assert control.status == "waiting_tts"
        assert control.current_command_id == replay.id
        assert control.last_error_code is None
        assert session.get(SessionRuntimeState, SESSION_ID).status == "active"
    assert fake_asr.calls == 1

    # 患者端拿到的仍是一条普通的问题命令，没有任何协议变更。
    projected = _device_next(api_clients)
    assert projected is not None and projected["kind"] == "tts"
    assert projected["payload"]["purpose"] == "question"
    assert set(projected["payload"]) == {"speech_key", "speech_text", "purpose"}

    # 重播播完 → 同一 logical slot 自动再开一次麦，不需要任何按钮。
    ended = _end_tts(api_clients, projected, suffix="replay1", device_seq=3)
    assert ended.status_code == 200, ended.text
    reopened = ended.json()["command"]
    assert reopened["kind"] == "record" and reopened["state"] == "pending"
    assert (reopened["attempt_seq"], reopened["prompt_level"]) == (1, 0)
    with Session(api_clients.engine) as session:
        second_record = session.exec(select(RuntimeCommand).where(
            RuntimeCommand.idempotency_key == reopened["command_key"])).one()
        # 第二次录音从 replay provenance 继承第一次 capture 的协议绑定。
        assert second_record.repeat_protocol_version_id == protocol.version_id
        assert second_record.repeat_protocol_definition_digest == (
            protocol.definition_digest)
        assert list(session.exec(select(AttemptEvent))) == []


def test_second_explicit_repeat_in_the_same_slot_pauses_instead_of_replaying(
        api_clients: ApiClients, monkeypatch):
    """同一环节第二次明确要求重播：记账 + 原子安全暂停，不再重播、不判错。"""
    _enable_p0a(monkeypatch)
    protocol = _bind_repeat_protocol(api_clients)
    _drive_to_processing_attempt(api_clients)
    fake_asr = _RepeatAsr("再说一遍", "没听清")
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)

    _run_p0a_attempt_worker(SESSION_ID)
    replay = _device_next(api_clients)
    assert replay is not None
    ended = _end_tts(api_clients, replay, suffix="replay1", device_seq=3)
    assert ended.status_code == 200, ended.text
    second_record = ended.json()["command"]
    stopped, second_audio_id = _complete_record(
        api_clients, second_record, suffix="second", device_seq=4,
        stop_reason="user_done")
    assert stopped["status"] == "processing_attempt"

    _run_p0a_attempt_worker(SESSION_ID)

    with Session(api_clients.engine) as session:
        requests = list(session.exec(select(AutopilotRepeatRequest).order_by(
            AutopilotRepeatRequest.repeat_ordinal)))
        assert [row.repeat_ordinal for row in requests] == [1, 2]
        limit = requests[1]
        assert limit.outcome == "limit_paused"
        assert limit.replay_command_id is None
        assert limit.pause_control_event_seq is not None
        assert limit.raw_audio_id == second_audio_id
        # 仍是同一 logical slot：attempt_seq / prompt_level 一个都没动。
        assert (limit.attempt_seq, limit.prompt_level) == (1, 0)
        assert limit.repeat_protocol_version_id == protocol.version_id

        assert list(session.exec(select(AttemptEvent))) == []
        commands = _commands(session)
        # 只有原问题、原录音、重播、第二条录音；没有第三条 TTS。
        assert [(row.kind, row.state) for row in commands] == [
            ("tts", "succeeded"), ("record", "succeeded"),
            ("tts", "succeeded"), ("record", "succeeded")]

        event = session.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.session_id == SESSION_ID,
            AutopilotControlEvent.event_seq == limit.pause_control_event_seq,
        )).one()
        assert event.event_type == "pause"
        assert event.actor_type == "system" and event.actor_id is None
        assert event.reason_code == "explicit_repeat_limit"
        assert json.loads(event.payload_json) == {
            "reason_code": "explicit_repeat_limit",
            "source": "repeat_intent_protocol",
        }
        assert event.from_status == "processing_attempt"
        assert event.to_status == "paused"

        control = session.get(SessionAutopilotState, SESSION_ID)
        assert control.status == "paused"
        assert control.current_command_id is None
        assert control.last_error_code == "explicit_repeat_limit"
        assert session.get(SessionRuntimeState, SESSION_ID).status == "paused"

        captures = list(session.exec(select(AttemptCaptureProcessing).order_by(
            AttemptCaptureProcessing.id)))
        assert [row.disposition for row in captures] == [
            "repeat_replayed", "repeat_limit_paused"]
        assert all(row.final_attempt_id is None for row in captures)
    assert fake_asr.calls == 2

    # 恢复 GET 与再次运行 worker 都不再新增账本或事件。
    _run_p0a_attempt_worker(SESSION_ID)
    with Session(api_clients.engine) as session:
        assert len(list(session.exec(select(AutopilotRepeatRequest)))) == 2
        assert len(list(session.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.reason_code == "explicit_repeat_limit")))) == 1
        assert list(session.exec(select(AttemptEvent))) == []


def test_transcript_that_merely_contains_a_repeat_phrase_is_still_an_answer(
        api_clients: ApiClients, monkeypatch):
    """"没听清，不过我觉得是苹果" 必须走普通回答，不能被当成重播请求丢掉。"""
    _enable_p0a(monkeypatch)
    _bind_repeat_protocol(api_clients)
    _drive_to_processing_attempt(api_clients)
    fake_asr = _RepeatAsr("没听清，不过我觉得是苹果")
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)

    _run_p0a_attempt_worker(SESSION_ID)

    with Session(api_clients.engine) as session:
        attempt = session.exec(select(AttemptEvent)).one()
        assert attempt.asr_text == "没听清，不过我觉得是苹果"
        assert attempt.processing_status == "completed"
        assert (attempt.attempt_seq, attempt.prompt_level) == (1, 0)
        assert list(session.exec(select(AutopilotRepeatRequest))) == []
        processing = session.exec(select(AttemptCaptureProcessing)).one()
        assert processing.disposition == "answer_candidate"
        assert processing.final_attempt_id == attempt.id
        assert processing.repeat_request_id is None


def _primitive_snapshot(session: Session) -> dict:
    """Everything the execution sheet requires to be unchanged on a refusal."""
    control = session.get(SessionAutopilotState, SESSION_ID)
    runtime_state = session.get(SessionRuntimeState, SESSION_ID)
    return {
        "plan": [
            (row.plan_id, row.status, row.revision,
             row.repeat_protocol_version_id,
             row.repeat_protocol_definition_digest)
            for row in session.exec(select(VisitPlan))
        ],
        "session": [
            (row.session_id, row.repeat_protocol_version_id,
             row.repeat_protocol_definition_digest)
            for row in session.exec(select(TrainSession))
        ],
        "commands": [
            (row.id, row.kind, row.state, row.command_seq, row.revision,
             row.repeat_protocol_version_id, row.replay_source_command_id,
             row.replay_ordinal, row.replay_source_payload_sha256)
            for row in session.exec(
                select(RuntimeCommand).order_by(RuntimeCommand.id))
        ],
        "captures": [
            (row.id, row.processing_status, row.disposition,
             row.final_attempt_id, row.repeat_request_id,
             row.processing_generation)
            for row in session.exec(
                select(AttemptCaptureProcessing).order_by(
                    AttemptCaptureProcessing.id))
        ],
        "attempts": [
            (row.id, row.attempt_seq, row.prompt_level, row.processing_status)
            for row in session.exec(select(AttemptEvent).order_by(AttemptEvent.id))
        ],
        "repeat_requests": [
            (row.id, row.repeat_ordinal, row.outcome)
            for row in session.exec(select(AutopilotRepeatRequest).order_by(
                AutopilotRepeatRequest.id))
        ],
        "control_events": [
            (row.event_seq, row.event_type, row.reason_code)
            for row in session.exec(select(AutopilotControlEvent).order_by(
                AutopilotControlEvent.event_seq))
        ],
        "control": None if control is None else (
            control.status, control.mode, control.revision,
            control.current_command_id, control.last_error_code,
            control.control_generation, control.runner_generation),
        "runtime": None if runtime_state is None else (
            runtime_state.status, runtime_state.revision),
    }


def _clear_session_repeat_binding(clients: ApiClients) -> None:
    """Force the pre-approval shape a historical session actually has."""
    with Session(clients.engine) as session:
        session.connection().exec_driver_sql(
            "UPDATE session SET repeat_protocol_version_id = NULL, "
            "repeat_protocol_definition_digest = NULL WHERE session_id = ?",
            (SESSION_ID,))
        session.commit()


def test_session_without_a_frozen_repeat_binding_is_refused_with_zero_writes(
        api_clients: ApiClients, monkeypatch):
    """旧协议场次必须安全拒绝：不能带着 NULL 绑定继续跑自动驾驶。"""
    _enable_p0a(monkeypatch)
    _clear_session_repeat_binding(api_clients)

    with Session(api_clients.engine) as session:
        before = _primitive_snapshot(session)

    started = _start(api_clients)
    assert started.status_code == 409, started.text
    assert started.json()["detail"]["code"] == "autopilot_repeat_binding_missing"
    nexted = api_clients.device.get(
        f"/sessions/{SESSION_ID}/autopilot/next",
        headers=api_clients.device_headers)
    assert nexted.status_code == 409, nexted.text

    with Session(api_clients.engine) as session:
        assert _primitive_snapshot(session) == before


def test_current_command_binding_wiped_after_issue_is_refused_everywhere(
        api_clients: ApiClients, monkeypatch):
    """已启用场次的当前命令被抹成 NULL/NULL：投影、设备动作、ACK 全部拒绝且零写。"""
    _enable_p0a(monkeypatch)
    _bind_repeat_protocol(api_clients)
    capture = _drive_to_processing_attempt(api_clients)

    with Session(api_clients.engine) as session:
        record = session.exec(select(RuntimeCommand).where(
            RuntimeCommand.kind == "record")).one()
        session.connection().exec_driver_sql(
            "UPDATE runtimecommand SET repeat_protocol_version_id = NULL, "
            "repeat_protocol_definition_digest = NULL WHERE id = ?",
            (record.id,))
        session.commit()
        before = _primitive_snapshot(session)

    # Projection refuses.
    nexted = api_clients.device.get(
        f"/sessions/{SESSION_ID}/autopilot/next",
        headers=api_clients.device_headers)
    assert nexted.status_code in {200, 204, 409}, nexted.text
    if nexted.status_code == 200:
        # processing_attempt projects no command at all; the ACK path below is
        # the real gate for this state.
        body = nexted.json()
        assert body is None or body.get("command") is None

    # A replayed record_stopped ACK for the same command refuses too.
    replayed_ack = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/"
        f"{capture['record']['command_key']}/acks",
        headers=api_clients.device_headers,
        json=_ack_body(
            capture["record"], ack_type="record_stopped",
            ack_key="ack-worker-record-stopped-0001",
            device_event_seq=2, stop_reason=capture["stop_reason"],
            raw_audio_id=capture["raw_audio_id"],
            receipt_server_seq=capture["receipt_server_seq"],
            checksum=capture["checksum"], byte_count=capture["byte_count"],
            duration_seconds=capture["duration_seconds"]),
    )
    assert replayed_ack.status_code == 409, replayed_ack.text
    assert replayed_ack.json()["detail"]["code"] == (
        "autopilot_command_repeat_binding_mismatch")

    # And the worker refuses without consuming the safe-pause path.
    fake_asr = _RepeatAsr("再说一遍")
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)
    _run_p0a_attempt_worker(SESSION_ID)

    with Session(api_clients.engine) as session:
        assert _primitive_snapshot(session) == before


def test_repeat_route_rejects_a_stale_repeat_digest_with_zero_semantic_writes(
        api_clients: ApiClients, monkeypatch):
    """场次绑定的协议摘要对不上历史注册表 → 稳定拒绝，且一行语义写入都不留。"""
    _enable_p0a(monkeypatch)
    _bind_repeat_protocol(api_clients)
    _drive_to_processing_attempt(api_clients)
    with Session(api_clients.engine) as session:
        session.connection().exec_driver_sql(
            "UPDATE session SET repeat_protocol_definition_digest = ? "
            "WHERE session_id = ?", ("f" * 64, SESSION_ID))
        session.commit()
        before = _primitive_snapshot(session)
        train_session = session.get(TrainSession, SESSION_ID)
        with pytest.raises(autopilot_service.AutopilotServiceError) as excinfo:
            autopilot_service.session_repeat_protocol(train_session)
        assert excinfo.value.code == "autopilot_repeat_protocol_unavailable"

    fake_asr = _RepeatAsr("再说一遍")
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)
    _run_p0a_attempt_worker(SESSION_ID)

    with Session(api_clients.engine) as session:
        # 完整 primitive 快照不变：没有新命令、没有 attempt、没有账本，
        # 也没有把配置性拒绝转成 worker 安全暂停。
        assert _primitive_snapshot(session) == before


def test_started_scope_rejects_a_command_whose_repeat_binding_drifts(
        api_clients: ApiClients, monkeypatch):
    """命令与场次的重复协议绑定不一致 → service 与 HTTP 都稳定 409。"""
    _enable_p0a(monkeypatch)
    _bind_repeat_protocol(api_clients)
    started = _start(api_clients)
    assert started.status_code == 200, started.text
    tts = _device_next(api_clients)
    assert tts is not None

    with Session(api_clients.engine) as session:
        command = session.exec(select(RuntimeCommand).where(
            RuntimeCommand.idempotency_key == tts["command_key"])).one()
        assert command.repeat_protocol_version_id is not None
        session.connection().exec_driver_sql(
            "UPDATE runtimecommand SET repeat_protocol_version_id = NULL, "
            "repeat_protocol_definition_digest = NULL WHERE id = ?",
            (command.id,))
        session.commit()
        before = _primitive_snapshot(session)

    ended = _end_tts(api_clients, tts, suffix="binding-drift", device_seq=1)
    assert ended.status_code == 409, ended.text
    assert ended.json()["detail"]["code"] == (
        "autopilot_command_repeat_binding_mismatch")
    with Session(api_clients.engine) as session:
        assert _primitive_snapshot(session) == before


def test_repeat_route_validates_the_active_device_at_transaction_time(
        api_clients: ApiClients, monkeypatch):
    """重播路由用事务当前时间校验活跃设备，而不是 ASR 前冻结的时间。

    第一轮实现把 ASR 之前取的本地时钟传进路由，让设备能力被误判为过期。
    这条回归同时证明修复没有削弱换绑/吊销：能力被撤销后仍然拒绝。
    """
    _enable_p0a(monkeypatch)
    _bind_repeat_protocol(api_clients)
    _drive_to_processing_attempt(api_clients)
    fake_asr = _RepeatAsr("再说一遍")
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)

    _run_p0a_attempt_worker(SESSION_ID)
    with Session(api_clients.engine) as session:
        assert session.exec(select(AutopilotRepeatRequest)).one().outcome == (
            "replayed")
        control = session.get(SessionAutopilotState, SESSION_ID)
        assert control.status == "waiting_tts"
        assert control.last_error_code is None


def _latest_ack_evidence_moment_utc(session: Session):
    """immutable terminal proof 真正比较的那条 UTC-naive ACK 证据时刻(微秒精度)。

    只锚定 ACK/receipt，**不含** ``AttemptCaptureProcessing.created_at``。当前正式
    准入路径调用 ``ensure_capture_processing(now=observed_at)``，所以那一列今天确实
    也是 UTC-naive、并没有被推到未来——收窄的理由不是它现在有偏差，而是：
    immutable terminal proof 根本不比较这一列，而模型层的 default 是
    ``datetime.now``(本地 naive)，任何一条不显式传 ``now`` 的写入都会引入混钟。
    锚点只该取 proof 真正比较的那些 UTC-naive 证据。

    也不能用 SQLite 的 ``CURRENT_TIMESTAMP``：那是秒精度，而 ACK 是微秒，同秒写入
    会被截断到证据之前，于是 ``revoked_at <= ack.received_at`` 在同秒与跨秒会走两条
    都合法、结论却相反的分支——测试就变成掷骰子。
    """
    moments = [row.received_at for row in session.exec(select(RuntimeCommandAck))]
    moments += [row.received_at for row in session.exec(
        select(AudioCaptureReceipt))]
    assert moments, "fixture 必须已经持久化 ACK/receipt 证据"
    return max(moments)


def _revoke_capability_at(session: Session, moment) -> None:
    """用 Python 绑定的微秒时刻吊销设备，不交给数据库取当前时间。"""
    session.connection().exec_driver_sql(
        "UPDATE patientdevicecapability SET revoked_at = ?",
        (moment.isoformat(sep=" "),))
    session.commit()


def test_device_revoked_after_terminal_capture_but_before_asr_safe_pauses(
        api_clients: ApiClients, monkeypatch):
    """录音已 terminal、ASR 之前吊销设备：不碰 provider，也不留任何 repeat 副作用。

    吊销时刻严格晚于已持久化的 capture/ACK 证据，所以 immutable terminal proof
    仍然成立，既有的设备失败规则可以安全收口——本用例断言的就是这条既有 device
    fail-close 真的跑了，而不是"整库零写"。
    """
    _enable_p0a(monkeypatch)
    _bind_repeat_protocol(api_clients)
    _drive_to_processing_attempt(api_clients)
    with Session(api_clients.engine) as session:
        # 严格晚于证据、又早于 worker 开跑:确定性地落在"录音已完成之后才吊销"。
        evidence = _latest_ack_evidence_moment_utc(session)
        revoked_at = evidence + timedelta(microseconds=1)
        # 只需要这一条时序事实:吊销严格晚于 ACK 证据,所以 immutable terminal
        # proof 仍然可验证。至于"worker 看得见这条 revoked 行",由程序顺序保证
        # ——下面的 commit 发生在 _run_p0a_attempt_worker 之前,不需要也不应该再
        # 拿墙钟去断言,那会把这条用例重新绑回快慢。
        assert revoked_at > evidence
        _revoke_capability_at(session, revoked_at)
        before = _primitive_snapshot(session)

    fake_asr = _RepeatAsr("再说一遍")
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)
    _run_p0a_attempt_worker(SESSION_ID)

    # provider 一次都没被调用，claim 也没被取走。
    assert fake_asr.calls == 0
    with Session(api_clients.engine) as session:
        after = _primitive_snapshot(session)
        # repeat 通道零副作用:没有账本行、没有重播命令、没有 Attempt。
        assert after["repeat_requests"] == []
        assert after["commands"] == before["commands"]
        assert after["attempts"] == before["attempts"]
        capture = session.exec(select(AttemptCaptureProcessing)).one()
        assert capture.processing_status == "received"
        assert capture.disposition is None
        assert capture.repeat_request_id is None
        # 设备失败规则安全收口:钉住 revoked 设备那个稳定错误码,否则任何一种
        # generic 暂停都会把这条用例误判成绿。
        control = session.get(SessionAutopilotState, SESSION_ID)
        assert control.status == "paused"
        assert control.last_error_code == "autopilot_device_not_paired"
        assert control.current_command_id is None
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        assert runtime_state.status == "paused"
        # 新增的那条控制事件必须是同码 failure，不是别的原因。
        added = [row for row in after["control_events"]
                 if row not in before["control_events"]]
        assert added == [(added[0][0], "failure", "autopilot_device_not_paired")]


def test_device_revoked_before_the_recording_evidence_refuses_with_zero_writes(
        api_clients: ApiClients, monkeypatch):
    """吊销时刻明确早于录音证据：连安全收口都无法证明，必须整库零写。

    这是与上面互补、且同样确定性的另一半:``revoked_at <= ack.received_at`` 让
    immutable terminal proof 拒绝,于是 worker 不做第二次未经证明的写入,恢复交给
    later GET。两种语义各自独立成例,不再挤在一个靠墙钟的用例里。
    """
    _enable_p0a(monkeypatch)
    _bind_repeat_protocol(api_clients)
    _drive_to_processing_attempt(api_clients)
    with Session(api_clients.engine) as session:
        # 早于全部证据一整分钟:同秒截断也不可能把它推到证据之后。
        _revoke_capability_at(
            session, _latest_ack_evidence_moment_utc(session) - timedelta(
                seconds=60))
        before = _primitive_snapshot(session)

    fake_asr = _RepeatAsr("再说一遍")
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)
    _run_p0a_attempt_worker(SESSION_ID)

    assert fake_asr.calls == 0
    with Session(api_clients.engine) as session:
        assert _primitive_snapshot(session) == before


def test_device_revoked_after_asr_blocks_the_repeat_route_without_repeat_effects(
        api_clients: ApiClients, monkeypatch):
    """ASR 之后并发吊销：走既有设备安全暂停，绝不留下任何 repeat 副作用。

    这条路径命中的是 foundation 早就有的 device fail-close，不是 repeat 专属分支；
    这里要证明的是 repeat route 没有在它之前抢先写任何东西。
    """
    _enable_p0a(monkeypatch)
    _bind_repeat_protocol(api_clients)
    _drive_to_processing_attempt(api_clients)

    class _RevokingAsr(_RepeatAsr):
        def transcribe(self, audio_bytes, hotwords):
            # 先真正产出 transcript,再吊销:这才是"ASR 之后并发吊销"。
            transcript = super().transcribe(audio_bytes, hotwords)
            with Session(api_clients.engine) as session:
                # 与前两条用例同一口径:Python 绑定的微秒时刻,严格晚于已持久化的
                # 采集证据。回填到录音之前会变成另一种语义,也会重新引入 DB
                # 墙钟的秒截断不确定性。
                _revoke_capability_at(
                    session, _latest_ack_evidence_moment_utc(session)
                    + timedelta(microseconds=1))
            return transcript

    with Session(api_clients.engine) as session:
        before = _primitive_snapshot(session)

    fake_asr = _RevokingAsr("再说一遍")
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)
    _run_p0a_attempt_worker(SESSION_ID)

    assert fake_asr.calls == 1
    with Session(api_clients.engine) as session:
        after = _primitive_snapshot(session)
        # No repeat evidence of any kind: no ledger row, no replay command,
        # no Attempt, and the capture never reached a repeat terminal state.
        assert after["repeat_requests"] == []
        assert after["commands"] == before["commands"]
        assert after["attempts"] == before["attempts"]
        capture = session.exec(select(AttemptCaptureProcessing)).one()
        assert capture.processing_status == "received"
        assert capture.disposition is None
        assert capture.repeat_request_id is None
        # 吊销发生在采集证据之后，immutable terminal proof 仍成立，所以既有的
        # 设备失败规则可以安全收口；这里钉住那个稳定错误码与同码 failure 事件。
        control = session.get(SessionAutopilotState, SESSION_ID)
        assert control.status == "paused"
        assert control.last_error_code == "autopilot_device_not_paired"
        assert control.current_command_id is None
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        assert runtime_state.status == "paused"
        added = [row for row in after["control_events"]
                 if row not in before["control_events"]]
        assert added == [(added[0][0], "failure", "autopilot_device_not_paired")]
        assert control.revision == before["control"][2] + 1


@pytest.mark.parametrize("stop_reason", ["user_done", "max_duration"])
def test_stop_reason_never_changes_the_repeat_decision(
        api_clients: ApiClients, monkeypatch, stop_reason):
    """点了"说完了"还是等到上限收麦，只由完整成功转写决定，结果必须一致。"""
    _enable_p0a(monkeypatch)
    _bind_repeat_protocol(api_clients)
    _drive_to_processing_attempt(api_clients, stop_reason=stop_reason)
    fake_asr = _RepeatAsr("再说一遍")
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)

    _run_p0a_attempt_worker(SESSION_ID)

    with Session(api_clients.engine) as session:
        request = session.exec(select(AutopilotRepeatRequest)).one()
        assert (request.repeat_ordinal, request.outcome) == (1, "replayed")
        assert (request.attempt_seq, request.prompt_level) == (1, 0)
        assert list(session.exec(select(AttemptEvent))) == []
        replay = session.exec(select(RuntimeCommand).where(
            RuntimeCommand.replay_ordinal == 1)).one()
        source = session.get(RuntimeCommand, replay.replay_source_command_id)
        assert replay.payload_json == source.payload_json


def test_explicit_repeat_after_cue1_keeps_that_prompt_level_and_attempt(
        api_clients: ApiClients, monkeypatch):
    """一级提示后说"没听清"：重播的是那条提示，不是原问题，且不消费二级提示。"""
    _enable_p0a(monkeypatch)
    _bind_repeat_protocol(api_clients)
    _drive_to_processing_attempt(api_clients)
    # An empty successful transcript is operational silence: it consumes the
    # first attempt and plays the frozen level-1 cue.
    fake_asr = _RepeatAsr("", "没听清")
    monkeypatch.setattr(asr, "get_engine", lambda: fake_asr)
    _run_p0a_attempt_worker(SESSION_ID)

    cue = _device_next(api_clients)
    assert cue is not None and cue["payload"]["purpose"] == "cue"
    ended = _end_tts(api_clients, cue, suffix="cue1", device_seq=3)
    assert ended.status_code == 200, ended.text
    cue_record = ended.json()["command"]
    assert (cue_record["attempt_seq"], cue_record["prompt_level"]) == (2, 1)
    stopped, _audio = _complete_record(
        api_clients, cue_record, suffix="cue1rec", device_seq=4)
    assert stopped["status"] == "processing_attempt"

    _run_p0a_attempt_worker(SESSION_ID)

    with Session(api_clients.engine) as session:
        request = session.exec(select(AutopilotRepeatRequest)).one()
        assert (request.repeat_ordinal, request.outcome) == (1, "replayed")
        # Still the cue-1 logical slot: no new attempt_seq, no level-2 cue.
        assert (request.attempt_seq, request.prompt_level) == (2, 1)
        replay = session.get(RuntimeCommand, request.replay_command_id)
        source = session.get(RuntimeCommand, request.source_tts_command_id)
        assert json.loads(source.payload_json)["purpose"] == "cue"
        assert replay.payload_json == source.payload_json
        assert (replay.attempt_seq, replay.prompt_level) == (2, 1)
        # The silence attempt from before the repeat is untouched and alone.
        attempts = list(session.exec(select(AttemptEvent)))
        assert len(attempts) == 1
        assert attempts[0].attempt_seq == 1
        assert attempts[0].prompt_level == 0
        control = session.get(SessionAutopilotState, SESSION_ID)
        assert control.status == "waiting_tts"
        assert control.current_command_id == replay.id


def test_repeat_enabled_session_keeps_silence_and_technical_failure_paths(
        api_clients: ApiClients, monkeypatch):
    """启用重播语义后，空串仍是沉默提示，None 仍是技术失败——detector 不抢这两条路。"""
    _enable_p0a(monkeypatch)
    _bind_repeat_protocol(api_clients)
    _drive_to_processing_attempt(api_clients)
    monkeypatch.setattr(asr, "get_engine", lambda: _RepeatAsr(""))
    _run_p0a_attempt_worker(SESSION_ID)

    with Session(api_clients.engine) as session:
        attempt = session.exec(select(AttemptEvent)).one()
        assert attempt.asr_text == ""
        assert attempt.operational_answer_type == "沉默"
        assert list(session.exec(select(AutopilotRepeatRequest))) == []
        cue = session.exec(select(RuntimeCommand).where(
            RuntimeCommand.kind == "tts",
            RuntimeCommand.state == "pending")).one()
        assert json.loads(cue.payload_json)["purpose"] == "cue"


def test_repeat_enabled_session_still_pauses_on_a_degraded_transcript(
        api_clients: ApiClients, monkeypatch):
    _enable_p0a(monkeypatch)
    _bind_repeat_protocol(api_clients)
    _drive_to_processing_attempt(api_clients)

    class _DegradedAsr(_RepeatAsr):
        def transcribe(self, _audio_bytes, _hotwords):
            self.calls += 1
            return asr.AsrResult(None, None, self.version)

    monkeypatch.setattr(asr, "get_engine", lambda: _DegradedAsr("unused"))
    _run_p0a_attempt_worker(SESSION_ID)

    with Session(api_clients.engine) as session:
        attempt = session.exec(select(AttemptEvent)).one()
        assert attempt.processing_status == "technical_failure"
        assert attempt.error_code == "asr_degraded"
        assert list(session.exec(select(AutopilotRepeatRequest))) == []
        control = session.get(SessionAutopilotState, SESSION_ID)
        assert control.status == "paused"
        assert control.last_error_code == "asr_degraded"
        assert session.get(SessionRuntimeState, SESSION_ID).status == "paused"


def _tamper(session: Session, statement: str, params: tuple) -> None:
    """Write a corrupted row a legitimate code path could never produce.

    ``ignore_check_constraints`` is used only here and only to simulate a
    restore/corruption: the CHECK constraints are exactly what the production
    path relies on, so the tamper has to bypass them to prove the *readback*
    verifier is independently load-bearing.  The window is one statement wide
    and is closed on the same connection even if the write raises — a pooled
    connection that stayed permissive would silently turn later CHECK negatives
    green.
    """
    connection = session.connection()
    connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
    try:
        connection.exec_driver_sql(statement, params)
    finally:
        # Reset on the *same* connection and before commit: commit returns the
        # connection to the pool, so a reset afterwards would land on a
        # different one and leave this connection permanently permissive.
        connection.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")
    session.commit()
    assert _ignore_check_constraints_is_off(session)


def _ignore_check_constraints_is_off(session: Session) -> bool:
    value = session.connection().exec_driver_sql(
        "PRAGMA ignore_check_constraints").scalar()
    return value in (0, None)


def test_tamper_helper_closes_its_constraint_window(
        api_clients: ApiClients, monkeypatch):
    """腐败写入窗口只开一条语句；helper 返回后同一条 CHECK 必须重新拦住。"""
    _enable_p0a(monkeypatch)
    _bind_repeat_protocol(api_clients)
    _drive_to_processing_attempt(api_clients)
    with Session(api_clients.engine) as session:
        command_id = session.exec(select(RuntimeCommand).where(
            RuntimeCommand.kind == "tts")).one().id

    # A lone replay_ordinal violates ck_runtime_command_replay_provenance_complete
    # and nothing else — no foreign key is involved.
    with Session(api_clients.engine) as session:
        _tamper(session,
                "UPDATE runtimecommand SET replay_ordinal = 1 WHERE id = ?",
                (command_id,))
        assert _ignore_check_constraints_is_off(session)
        assert session.get(RuntimeCommand, command_id).replay_ordinal == 1

    with Session(api_clients.engine) as session:
        assert _ignore_check_constraints_is_off(session)
        with pytest.raises(IntegrityError):
            session.connection().exec_driver_sql(
                "UPDATE runtimecommand SET replay_ordinal = 1 WHERE id = ?",
                (command_id,))
            session.commit()
        session.rollback()


def _seed_unrelated_attempt(clients: ApiClients) -> int:
    """A genuine AttemptEvent row, so the tamper hits the verifier not an FK."""
    with Session(clients.engine) as session:
        attempt = AttemptEvent(
            session_id=SESSION_ID, item_id=BANK.single_element[0]["item_id"],
            turn_seq=1, response_role="命名", attempt_seq=99,
            raw_audio_id=session.exec(select(AudioAssetRow)).first().raw_audio_id,
            prompt_level=0, processing_status="completed", is_simulation=True)
        session.add(attempt)
        session.commit()
        session.refresh(attempt)
        return attempt.id


def _replayed_repeat_chain(clients: ApiClients, monkeypatch) -> dict:
    _enable_p0a(monkeypatch)
    _bind_repeat_protocol(clients)
    _drive_to_processing_attempt(clients)
    monkeypatch.setattr(asr, "get_engine", lambda: _RepeatAsr("再说一遍"))
    _run_p0a_attempt_worker(SESSION_ID)
    with Session(clients.engine) as session:
        capture = session.exec(select(AttemptCaptureProcessing)).one()
        request = session.exec(select(AutopilotRepeatRequest)).one()
        return {
            "capture_id": capture.id,
            "request_id": request.id,
            "record_id": request.record_command_id,
            "source_id": request.source_tts_command_id,
            "replay_id": request.replay_command_id,
        }


@pytest.mark.parametrize("tamper", [
    "replay_payload_bytes",
    "ledger_source_digest",
    "replay_provenance_cleared",
    "capture_reverse_pointer",
    "replay_repeat_digest",
    "replay_device_identity",
    "record_control_generation",
    "record_repeat_digest",
    "record_predecessor_pointer",
    "source_repeat_digest",
    "source_slot_prompt_level",
    "source_device_identity",
    "source_content_binding",
    "source_payload_bytes",
    "request_slot_attempt_seq",
])
def test_replayed_terminal_readback_fails_closed_on_one_tampered_field(
        api_clients: ApiClients, monkeypatch, tamper):
    """终态复读逐项重证：链上任一字段被改都必须 fail closed，且零写。"""
    ids = _replayed_repeat_chain(api_clients, monkeypatch)
    statements = {
        "replay_payload_bytes": (
            "UPDATE runtimecommand SET payload_json = ? WHERE id = ?",
            ('{"schema_version":1}', ids["replay_id"])),
        "ledger_source_digest": (
            "UPDATE autopilotrepeatrequest SET source_payload_sha256 = ? "
            "WHERE id = ?", ("d" * 64, ids["request_id"])),
        "replay_provenance_cleared": (
            "UPDATE runtimecommand SET replay_source_command_id = NULL, "
            "replay_ordinal = NULL, replay_source_payload_sha256 = NULL "
            "WHERE id = ?", (ids["replay_id"],)),
        "capture_reverse_pointer": (
            "UPDATE attemptcaptureprocessing SET repeat_request_id = NULL "
            "WHERE id = ?", (ids["capture_id"],)),
        "replay_repeat_digest": (
            "UPDATE runtimecommand SET repeat_protocol_definition_digest = ? "
            "WHERE id = ?", ("e" * 64, ids["replay_id"])),
        "replay_device_identity": (
            "UPDATE runtimecommand SET issued_device_id_hash = ? WHERE id = ?",
            ("tampered-device", ids["replay_id"])),
        # The record command the capture was admitted from.
        "record_control_generation": (
            "UPDATE runtimecommand SET control_generation = 9 WHERE id = ?",
            (ids["record_id"],)),
        "record_repeat_digest": (
            "UPDATE runtimecommand SET repeat_protocol_definition_digest = ? "
            "WHERE id = ?", ("c" * 64, ids["record_id"])),
        "record_predecessor_pointer": (
            "UPDATE runtimecommand SET predecessor_command_id = NULL "
            "WHERE id = ?", (ids["record_id"],)),
        # The source TTS the replay copied.
        "source_repeat_digest": (
            "UPDATE runtimecommand SET repeat_protocol_definition_digest = ? "
            "WHERE id = ?", ("b" * 64, ids["source_id"])),
        "source_slot_prompt_level": (
            "UPDATE runtimecommand SET prompt_level = 2 WHERE id = ?",
            (ids["source_id"],)),
        "source_device_identity": (
            "UPDATE runtimecommand SET issued_device_id_hash = ? WHERE id = ?",
            ("rotated-device", ids["source_id"])),
        "source_content_binding": (
            "UPDATE runtimecommand SET item_bank_definition_digest = ? "
            "WHERE id = ?", ("a" * 64, ids["source_id"])),
        "source_payload_bytes": (
            "UPDATE runtimecommand SET payload_json = ? WHERE id = ?",
            ('{"schema_version":1}', ids["source_id"])),
        "request_slot_attempt_seq": (
            "UPDATE autopilotrepeatrequest SET attempt_seq = 9 WHERE id = ?",
            (ids["request_id"],)),
    }
    statement, params = statements[tamper]
    with Session(api_clients.engine) as session:
        _tamper(session, statement, params)

    with Session(api_clients.engine) as session:
        capture = session.get(AttemptCaptureProcessing, ids["capture_id"])
        with pytest.raises(RuntimeError, match="repeat evidence chain invalid"):
            main_module._capture_repeat_terminal_payload(capture, session)
        before = _primitive_snapshot(session)

    # A worker replay observes the same refusal: no route, no new pause.
    _run_p0a_attempt_worker(SESSION_ID)
    with Session(api_clients.engine) as session:
        assert _primitive_snapshot(session) == before


def _limit_paused_repeat_chain(clients: ApiClients, monkeypatch) -> dict:
    _enable_p0a(monkeypatch)
    _bind_repeat_protocol(clients)
    _drive_to_processing_attempt(clients)
    monkeypatch.setattr(asr, "get_engine", lambda: _RepeatAsr("再说一遍", "没听清"))
    _run_p0a_attempt_worker(SESSION_ID)
    replay = _device_next(clients)
    ended = _end_tts(clients, replay, suffix="limitchain", device_seq=3)
    assert ended.status_code == 200, ended.text
    _complete_record(
        clients, ended.json()["command"], suffix="limitrec", device_seq=4)
    _run_p0a_attempt_worker(SESSION_ID)
    with Session(clients.engine) as session:
        request = session.exec(select(AutopilotRepeatRequest).where(
            AutopilotRepeatRequest.repeat_ordinal == 2)).one()
        return {
            "capture_id": request.capture_processing_id,
            "request_id": request.id,
            "record_id": request.record_command_id,
            "event_seq": request.pause_control_event_seq,
        }


@pytest.mark.parametrize("tamper", [
    "pause_event_command_pointer",
    "pause_event_control_generation",
    "pause_event_runner_generation",
    "pause_event_reason_code",
    "pause_event_payload",
    "pause_event_from_status",
    "ledger_pause_pointer",
])
def test_limit_paused_terminal_readback_fails_closed_on_one_tampered_field(
        api_clients: ApiClients, monkeypatch, tamper):
    """ordinal2 终态同样逐项重证：暂停事实必须与处理中那条录音命令精确一致。"""
    ids = _limit_paused_repeat_chain(api_clients, monkeypatch)
    statements = {
        "pause_event_command_pointer": (
            "UPDATE autopilotcontrolevent SET command_id = NULL "
            "WHERE session_id = ? AND event_seq = ?",
            (SESSION_ID, ids["event_seq"])),
        "pause_event_control_generation": (
            "UPDATE autopilotcontrolevent SET control_generation = 9 "
            "WHERE session_id = ? AND event_seq = ?",
            (SESSION_ID, ids["event_seq"])),
        "pause_event_runner_generation": (
            "UPDATE autopilotcontrolevent SET runner_generation = 9 "
            "WHERE session_id = ? AND event_seq = ?",
            (SESSION_ID, ids["event_seq"])),
        "pause_event_reason_code": (
            "UPDATE autopilotcontrolevent SET reason_code = 'asr_degraded' "
            "WHERE session_id = ? AND event_seq = ?",
            (SESSION_ID, ids["event_seq"])),
        "pause_event_payload": (
            "UPDATE autopilotcontrolevent SET payload_json = ? "
            "WHERE session_id = ? AND event_seq = ?",
            ('{"reason_code":"explicit_repeat_limit","source":"device_ack"}',
             SESSION_ID, ids["event_seq"])),
        "pause_event_from_status": (
            "UPDATE autopilotcontrolevent SET from_status = 'waiting_recording' "
            "WHERE session_id = ? AND event_seq = ?",
            (SESSION_ID, ids["event_seq"])),
        "ledger_pause_pointer": (
            "UPDATE autopilotrepeatrequest SET pause_control_event_seq = NULL "
            "WHERE id = ?", (ids["request_id"],)),
    }
    statement, params = statements[tamper]
    with Session(api_clients.engine) as session:
        _tamper(session, statement, params)

    with Session(api_clients.engine) as session:
        capture = session.get(AttemptCaptureProcessing, ids["capture_id"])
        assert capture.disposition == "repeat_limit_paused"
        with pytest.raises(RuntimeError, match="repeat evidence chain invalid"):
            main_module._capture_repeat_terminal_payload(capture, session)
        before = _primitive_snapshot(session)

    _run_p0a_attempt_worker(SESSION_ID)
    with Session(api_clients.engine) as session:
        assert _primitive_snapshot(session) == before


def test_duplicate_worker_run_never_creates_a_second_repeat_request(
        api_clients: ApiClients, monkeypatch):
    """响应丢失后再跑一次 worker：不新增账本、不新增命令、不二次路由。"""
    _enable_p0a(monkeypatch)
    _bind_repeat_protocol(api_clients)
    _drive_to_processing_attempt(api_clients)
    monkeypatch.setattr(asr, "get_engine", lambda: _RepeatAsr("再说一遍"))
    _run_p0a_attempt_worker(SESSION_ID)

    with Session(api_clients.engine) as session:
        before = _primitive_snapshot(session)

    _run_p0a_attempt_worker(SESSION_ID)
    _run_p0a_attempt_worker(SESSION_ID)

    with Session(api_clients.engine) as session:
        assert _primitive_snapshot(session) == before
        assert len(list(session.exec(select(AutopilotRepeatRequest)))) == 1


def test_drained_and_taken_over_scope_can_still_reverify_its_limit_pause(
        api_clients: ApiClients, monkeypatch):
    """暂停事实是不可变历史证据。

    这里走真正的收口链：设备对同一条录音命令 drain-ack（正好检验我把
    ``repeat_intent_protocol`` 加进 drainable pause 白名单是否真的生效），研究者
    接管为 manual，产生真实后继 control event 与 CAS；之后旧 ordinal2 仍须重证成功
    且零写。
    """
    ids = _limit_paused_repeat_chain(api_clients, monkeypatch)
    with Session(api_clients.engine) as session:
        record_key = session.get(
            RuntimeCommand, ids["record_id"]).idempotency_key

    drained = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/{record_key}/drain-ack",
        headers=api_clients.device_headers)
    assert drained.status_code == 200, drained.text
    taken = api_clients.account.post(
        f"/sessions/{SESSION_ID}/autopilot/takeover",
        json={"idempotency_key": "takeover-after-repeat-limit-0001",
              "expected_revision": drained.json()["state_revision"]})
    assert taken.status_code == 200, taken.text
    assert taken.json()["mode"] == "manual"

    with Session(api_clients.engine) as session:
        control = session.get(SessionAutopilotState, SESSION_ID)
        assert control.mode == "manual"
        # Real successor control events exist beyond the frozen pause.
        assert len(list(session.exec(select(AutopilotControlEvent)))) > 3
        before = _primitive_snapshot(session)

    with Session(api_clients.engine) as session:
        capture = session.get(AttemptCaptureProcessing, ids["capture_id"])
        assert main_module._capture_repeat_terminal_payload(capture, session) == {
            "status": "repeat_limit_paused", "in_progress": False}
        assert _primitive_snapshot(session) == before


def test_repeat_ledger_records_a_protocol_phrase_key_and_real_digest(
        api_clients: ApiClients, monkeypatch):
    """账本里的 phrase_key 必须属于该 capture 绑定的协议，摘要必须是真 hex64。"""
    protocol = repeat_intent.active_protocol()
    ids = _replayed_repeat_chain(api_clients, monkeypatch)
    with Session(api_clients.engine) as session:
        request = session.get(AutopilotRepeatRequest, ids["request_id"])
        assert request.phrase_key == "repeat_again"
        assert request.phrase_key in set(
            protocol.phrase_by_normalized_text.values())
        assert repeat_intent.normalized_text_sha256("再说一遍") == (
            request.normalized_text_sha256)
        replay = session.get(RuntimeCommand, ids["replay_id"])
        assert replay.idempotency_key == (
            autopilot_service.repeat_replay_command_key(
                SESSION_ID, ids["capture_id"]))


@pytest.mark.parametrize("tamper", [
    "ledger_phrase_key_not_in_protocol",
    "ledger_normalized_digest_malformed",
    "ledger_normalized_digest_valid_but_wrong",
    "capture_final_attempt_id_points_at_a_real_attempt",
    "replay_idempotency_key",
    "capture_asr_engine_drift",
    "capture_status_not_terminal",
])
def test_chain_anchors_reject_protocol_and_idempotency_drift(
        api_clients: ApiClients, monkeypatch, tamper):
    ids = _replayed_repeat_chain(api_clients, monkeypatch)
    statements = {
        "ledger_phrase_key_not_in_protocol": (
            "UPDATE autopilotrepeatrequest SET phrase_key = 'not_a_phrase' "
            "WHERE id = ?", (ids["request_id"],)),
        "ledger_normalized_digest_malformed": (
            "UPDATE autopilotrepeatrequest SET normalized_text_sha256 = ? "
            "WHERE id = ?", ("Z" * 64, ids["request_id"])),
        # A perfectly well-formed digest of some *other* string must still fail:
        # the verifier recomputes it from the frozen phrase.
        "ledger_normalized_digest_valid_but_wrong": (
            "UPDATE autopilotrepeatrequest SET normalized_text_sha256 = ? "
            "WHERE id = ?",
            (repeat_intent.normalized_text_sha256("没听清"), ids["request_id"])),
        "replay_idempotency_key": (
            "UPDATE runtimecommand SET idempotency_key = 'cmd-forged-000001' "
            "WHERE id = ?", (ids["replay_id"],)),
        "capture_asr_engine_drift": (
            "UPDATE attemptcaptureprocessing SET asr_engine_version = 'other' "
            "WHERE id = ?", (ids["capture_id"],)),
        "capture_status_not_terminal": (
            "UPDATE attemptcaptureprocessing SET processing_status = 'received' "
            "WHERE id = ?", (ids["capture_id"],)),
        "capture_final_attempt_id_points_at_a_real_attempt": (
            "UPDATE attemptcaptureprocessing SET final_attempt_id = ? "
            "WHERE id = ?",
            (_seed_unrelated_attempt(api_clients), ids["capture_id"])),
    }
    statement, params = statements[tamper]
    with Session(api_clients.engine) as session:
        _tamper(session, statement, params)
    with Session(api_clients.engine) as session:
        capture = session.get(AttemptCaptureProcessing, ids["capture_id"])
        with pytest.raises(RuntimeError, match="repeat evidence chain invalid"):
            main_module._capture_repeat_terminal_payload(capture, session)


# --------------------------------------------------------------------------
# C. Schema negatives on genuinely legal rows, with foreign keys enforced.
# --------------------------------------------------------------------------

def _assert_rejected(session: Session, statement: str, params: tuple,
                     expected_fragment: str) -> None:
    """The write must be rejected, and by the constraint we are testing."""
    with pytest.raises(IntegrityError) as caught:
        session.connection().exec_driver_sql(statement, params)
        session.commit()
    session.rollback()
    assert expected_fragment in str(caught.value), str(caught.value)


def test_foreign_keys_are_enforced_on_the_repeat_tables(
        api_clients: ApiClients, monkeypatch):
    """先证明 FK 真的开着，否则后面的 orphan 负例只是假绿。"""
    ids = _replayed_repeat_chain(api_clients, monkeypatch)
    with Session(api_clients.engine) as session:
        assert session.connection().exec_driver_sql(
            "PRAGMA foreign_keys").scalar() == 1
        # The legal bidirectional link really exists.
        capture = session.get(AttemptCaptureProcessing, ids["capture_id"])
        request = session.get(AutopilotRepeatRequest, ids["request_id"])
        assert capture.repeat_request_id == request.id
        assert request.capture_processing_id == capture.id
        # An orphan reverse pointer is refused by the foreign key itself.
        _assert_rejected(
            session,
            "UPDATE attemptcaptureprocessing SET repeat_request_id = 987654 "
            "WHERE id = ?", (ids["capture_id"],), "FOREIGN KEY")
        # So is an orphan anchor pointer.
        _assert_rejected(
            session,
            "UPDATE autopilotrepeatrequest SET capture_processing_id = 987654 "
            "WHERE id = ?", (ids["request_id"],), "FOREIGN KEY")


@pytest.mark.parametrize("column,value,constraint", [
    ("repeat_ordinal", 3, "ck_repeat_request_ordinal"),
    ("outcome", "'invented'", "ck_repeat_request_outcome"),
    ("replay_command_id", "NULL", "ck_repeat_request_ordinal_matches_outcome"),
    ("source_payload_sha256", "'" + "g" * 64 + "'",
     "ck_repeat_request_source_payload_digest"),
    ("normalized_text_sha256", "'" + "b" * 63 + "'",
     "ck_repeat_request_normalized_text_digest"),
    ("repeat_protocol_definition_digest", "'" + "z" * 64 + "'",
     "ck_repeat_request_protocol_binding"),
    ("phrase_key", "'   '", "ck_repeat_request_phrase_key"),
    ("prompt_level", 4, "ck_repeat_request_prompt_level"),
    ("attempt_seq", 0, "ck_repeat_request_attempt_positive"),
    ("turn_seq", 0, "ck_repeat_request_turn_positive"),
])
def test_one_field_update_on_a_real_ledger_row_hits_its_named_check(
        api_clients: ApiClients, monkeypatch, column, value, constraint):
    """基线是真实流程写出来的合法行；只改一个字段，必须命中指定的那条 CHECK。"""
    ids = _replayed_repeat_chain(api_clients, monkeypatch)
    with Session(api_clients.engine) as session:
        _assert_rejected(
            session,
            f"UPDATE autopilotrepeatrequest SET {column} = {value} WHERE id = ?",
            (ids["request_id"],),
            f"CHECK constraint failed: {constraint}")


@pytest.mark.parametrize("assignment,constraint", [
    ("replay_ordinal = NULL",
     "ck_runtime_command_replay_provenance_complete"),
    ("replay_source_command_id = NULL",
     "ck_runtime_command_replay_provenance_complete"),
    ("replay_source_payload_sha256 = NULL",
     "ck_runtime_command_replay_provenance_complete"),
    ("replay_ordinal = 2", "ck_runtime_command_replay_provenance_complete"),
    ("replay_source_payload_sha256 = '" + "g" * 64 + "'",
     "ck_runtime_command_replay_provenance_complete"),
    ("repeat_protocol_definition_digest = '" + "g" * 64 + "'",
     "ck_runtime_command_repeat_binding_complete"),
    ("repeat_protocol_version_id = NULL",
     "ck_runtime_command_repeat_binding_complete"),
])
def test_one_field_update_on_a_real_replay_command_hits_its_named_check(
        api_clients: ApiClients, monkeypatch, assignment, constraint):
    ids = _replayed_repeat_chain(api_clients, monkeypatch)
    with Session(api_clients.engine) as session:
        _assert_rejected(
            session,
            f"UPDATE runtimecommand SET {assignment} WHERE id = ?",
            (ids["replay_id"],),
            f"CHECK constraint failed: {constraint}")


def test_a_record_command_may_never_carry_replay_provenance(
        api_clients: ApiClients, monkeypatch):
    ids = _replayed_repeat_chain(api_clients, monkeypatch)
    with Session(api_clients.engine) as session:
        _assert_rejected(
            session,
            "UPDATE runtimecommand SET replay_source_command_id = ?, "
            "replay_ordinal = 1, replay_source_payload_sha256 = ? WHERE id = ?",
            (ids["source_id"], "a" * 64, ids["record_id"]),
            "CHECK constraint failed: ck_runtime_command_record_never_replays")


@pytest.mark.parametrize("assignment,constraint", [
    ("repeat_protocol_version_id = NULL",
     "ck_capture_processing_repeat_binding_complete"),
    ("repeat_protocol_definition_digest = '" + "g" * 64 + "'",
     "ck_capture_processing_repeat_binding_complete"),
    ("disposition = 'invented'", "ck_capture_processing_disposition"),
    ("repeat_request_id = NULL",
     "ck_capture_processing_final_attempt_matches_status"),
])
def test_one_field_update_on_a_real_repeat_capture_hits_its_named_check(
        api_clients: ApiClients, monkeypatch, assignment, constraint):
    ids = _replayed_repeat_chain(api_clients, monkeypatch)
    with Session(api_clients.engine) as session:
        _assert_rejected(
            session,
            f"UPDATE attemptcaptureprocessing SET {assignment} WHERE id = ?",
            (ids["capture_id"],),
            f"CHECK constraint failed: {constraint}")


def test_post_ack_capability_revocation_does_not_retroactively_void_the_proof(
        api_clients: ApiClients, monkeypatch):
    """回执产生之后再吊销设备，不能追溯否定这条不可变证据。

    这正是把 ACK 验证拆成 static identity 与 live authority 两层的理由。
    这条测试只证明这一点：它**不**证明治理门禁会拦住新处理——此时该录音早已不是
    current processing target，那条路径会先因状态被拒，证明不了治理。真正的
    re-pair/withdraw/delete/consent 阻断必须在正在处理中的 capture 上走正式 API。
    """
    ids = _replayed_repeat_chain(api_clients, monkeypatch)
    with Session(api_clients.engine) as session:
        # Revoke strictly *after* the ACK landed. Anchoring on the stored
        # received_at keeps this independent of the server's local timezone,
        # which SQLite's UTC datetime('now') would otherwise skew.
        latest_ack = max(
            row.received_at
            for row in session.exec(select(RuntimeCommandAck)))
        session.connection().exec_driver_sql(
            "UPDATE patientdevicecapability SET revoked_at = ?",
            ((latest_ack + timedelta(seconds=1)).isoformat(sep=" "),))
        session.commit()

    with Session(api_clients.engine) as session:
        record = session.get(RuntimeCommand, ids["record_id"])
        # Immutable proof still verifies against the revoked-later capability.
        proof = autopilot_ledger.verify_immutable_record_capture(session, record)
        assert proof.raw_audio_id == record.expected_raw_audio_id
        capture = session.get(AttemptCaptureProcessing, ids["capture_id"])
        assert main_module._capture_repeat_terminal_payload(capture, session) == {
            "status": "repeat_replayed", "in_progress": False}


def test_post_hoc_withdrawal_does_not_retroactively_void_the_proof(
        api_clients: ApiClients, monkeypatch):
    """同上：撤回发生在终态之后，只证明证据不被追溯否定，不证明治理拦截。"""
    ids = _replayed_repeat_chain(api_clients, monkeypatch)
    with Session(api_clients.engine) as session:
        session.connection().exec_driver_sql(
            "UPDATE patient SET withdrawal_status = 'withdrawn'")
        session.commit()

    with Session(api_clients.engine) as session:
        record = session.get(RuntimeCommand, ids["record_id"])
        assert autopilot_ledger.verify_immutable_record_capture(
            session, record).command_id == record.id
        capture = session.get(AttemptCaptureProcessing, ids["capture_id"])
        assert main_module._capture_repeat_terminal_payload(capture, session) == {
            "status": "repeat_replayed", "in_progress": False}


def test_pause_event_after_the_capture_was_closed_out_is_refused(
        api_clients: ApiClients, monkeypatch):
    """上界：ordinal2 的暂停事实不能发生在 capture 已终态之后。"""
    ids = _limit_paused_repeat_chain(api_clients, monkeypatch)
    with Session(api_clients.engine) as session:
        capture = session.get(AttemptCaptureProcessing, ids["capture_id"])
        later = (capture.processed_at + timedelta(minutes=5)).isoformat(sep=" ")
        _tamper(session,
                "UPDATE autopilotcontrolevent SET created_at = ? "
                "WHERE session_id = ? AND event_seq = ?",
                (later, SESSION_ID, ids["event_seq"]))

    with Session(api_clients.engine) as session:
        capture = session.get(AttemptCaptureProcessing, ids["capture_id"])
        with pytest.raises(RuntimeError,
                           match="pause event postdates the capture"):
            main_module._capture_repeat_terminal_payload(capture, session)


def test_replay_issued_after_the_capture_was_closed_out_is_refused(
        api_clients: ApiClients, monkeypatch):
    ids = _replayed_repeat_chain(api_clients, monkeypatch)
    with Session(api_clients.engine) as session:
        capture = session.get(AttemptCaptureProcessing, ids["capture_id"])
        later = (capture.processed_at + timedelta(minutes=5)).isoformat(sep=" ")
        _tamper(session, "UPDATE runtimecommand SET issued_at = ? WHERE id = ?",
                (later, ids["replay_id"]))

    with Session(api_clients.engine) as session:
        capture = session.get(AttemptCaptureProcessing, ids["capture_id"])
        with pytest.raises(RuntimeError, match="replay command does not match"):
            main_module._capture_repeat_terminal_payload(capture, session)


def test_audio_upload_timestamp_uses_the_shared_utc_naive_clock(
        api_clients: ApiClients, monkeypatch):
    """上传时间必须与命令/回执/收据同一时基，否则东八区会凭空差 8 小时。"""
    _enable_p0a(monkeypatch)
    _bind_repeat_protocol(api_clients)
    capture = _drive_to_processing_attempt(api_clients)
    with Session(api_clients.engine) as session:
        asset = session.get(AudioAssetRow, capture["raw_audio_id"])
        receipt = session.exec(select(AudioCaptureReceipt).where(
            AudioCaptureReceipt.raw_audio_id == capture["raw_audio_id"])).one()
        record = session.exec(select(RuntimeCommand).where(
            RuntimeCommand.kind == "record")).one()
        assert asset.uploaded_at is not None
        # Same clock: upload sits inside the command's own window, and the
        # receipt is written after the bytes land.
        assert record.issued_at <= asset.uploaded_at <= receipt.received_at
        assert abs((asset.uploaded_at - receipt.received_at).total_seconds()) < 60


def _drive_ordinary_answer_after_replay(clients: ApiClients) -> str:
    """After the replay plays, answer normally so the bundle has a real contrast."""
    replay = _device_next(clients)
    assert replay is not None and replay["kind"] == "tts"
    ended = _end_tts(clients, replay, suffix="answerafter", device_seq=3)
    assert ended.status_code == 200, ended.text
    record = ended.json()["command"]
    _, raw_audio_id = _complete_record(
        clients, record, suffix="answerrec", device_seq=4)
    _run_p0a_attempt_worker(SESSION_ID)
    return raw_audio_id


def _drive_formal_closeout(clients: ApiClients) -> None:
    """Take the session all the way through the real server-owned closure.

    Autopilot finishes the scope and auto-completes the intervention (which is
    what writes the authoritative SessionOutcomeSummary); a researcher then
    confirms and locks the turn, saves the closeout and completes the session.
    Nothing here is hand-written into the ledgers.
    """
    feedback = _device_next(clients)
    assert feedback is not None and feedback["kind"] == "tts"
    terminal = _end_tts(clients, feedback, suffix="feedback", device_seq=5)
    assert terminal.status_code == 200, terminal.text
    assert terminal.json()["status"] == "scope_completed"

    with Session(clients.engine) as session:
        runtime_state = session.get(SessionRuntimeState, SESSION_ID)
        assert runtime_state.status == "intervention_completed"
        turn_id = session.exec(select(TurnEvent)).one().id

    confirmed = clients.account.patch(f"/turns/{turn_id}/confirm", json={
        "confirmed_response_text": "胡萝卜",
        "expected_revision": 0,
        "idempotency_key": "repeat-export-confirm-0001",
    })
    assert confirmed.status_code == 200, confirmed.text
    locked = clients.account.patch(f"/turns/{turn_id}/lock", json={
        "reviewer_id": "ACTOR-P0A-HTTP", "element_value": 1, "prompt_level": 0,
    })
    assert locked.status_code == 200, locked.text
    closeout = clients.account.put(f"/sessions/{SESSION_ID}/closeout", json={
        "idempotency_key": "repeat-export-closeout-0001",
        "expected_revision": 0,
        "report_status": "no_additional_observation",
    })
    assert closeout.status_code == 200, closeout.text
    completed = clients.account.post(f"/sessions/{SESSION_ID}/complete")
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "completed"

    with Session(clients.engine) as session:
        patient_id = session.get(TrainSession, SESSION_ID).patient_id
        patient = session.get(Patient, patient_id)
        # The only value this test sets by hand: a governance consent flag that
        # has no bedside API in this harness.
        patient.secondary_use_allowed = True
        session.add(patient)
        session.commit()


def test_real_runtime_repeat_ledger_exports_as_metadata_only(
        api_clients: ApiClients, monkeypatch, tmp_path):
    """账本由真实 runtime ordinal-1 流程产生，然后真的跑一次导出。

    同一场次里还有一次真实的普通回答，所以 attempts/interactions 不是空集合——
    对照组存在时才能证明 repeat 真的被排除，而不是恰好整张表都空。
    """
    ids = _replayed_repeat_chain(api_clients, monkeypatch)
    # The same ASR stub keeps returning 再说一遍; swap it for a real answer so the
    # second recording in this slot becomes an ordinary Attempt.
    # 胡萝卜 is the frozen target for this position, so the answer is judged
    # correct and the scope finishes normally.
    monkeypatch.setattr(asr, "get_engine", lambda: _RepeatAsr("胡萝卜"))
    answer_audio_id = _drive_ordinary_answer_after_replay(api_clients)
    _drive_formal_closeout(api_clients)
    # A dedicated output root: tmp_path also holds the fixture's own database
    # and audio store, which are not part of the exported bundle.
    out = tmp_path / "export-out"

    with Session(api_clients.engine) as session:
        asset = session.get(AudioAssetRow, session.get(
            AutopilotRepeatRequest, ids["request_id"]).raw_audio_id)
        expected_checksum, expected_size = asset.checksum, asset.byte_count
        result = export.export_session_bundle(
            session, SESSION_ID, write_dir=out,
            idempotency_key="repeat-runtime-export-0123456789abcdef01234567",
            actor_display_id="TEST-DATA-STEWARD", actor_role="data_steward")

    rows = result["sheets"]["repeat_audio_manifest"]
    assert len(rows) == 1
    assert list(rows[0]) == [
        "capture_kind", "repeat_ordinal", "outcome", "opaque_audio_code"]
    assert rows[0]["capture_kind"] == "explicit_repeat"
    assert rows[0]["repeat_ordinal"] == 1
    assert rows[0]["outcome"] == "replayed"
    opaque = rows[0]["opaque_audio_code"]

    # A real ordinary answer exists in the same bundle, and the repeat recording
    # appears in none of the clinical projections.
    attempts = result["sheets"]["attempts"]
    interactions = result["sheets"]["interactions"]
    assert len(attempts) == 1
    # Free text is redacted by the de-identified channel; the row itself is real.
    assert attempts[0]["asr_text"] == "[REDACTED]"
    assert attempts[0]["operational_answer_type"] is not None
    assert attempts[0]["attempt_seq"] == 1 and attempts[0]["prompt_level"] == 0
    assert interactions, "ordinary answer must leave interaction evidence"
    answer_code = attempts[0]["audio_code"]
    assert answer_code and answer_code != opaque
    assert answer_code in {r["audio_code"] for r in result["sheets"]["audio_manifest"]}
    assert opaque not in {r["audio_code"] for r in result["sheets"]["audio_manifest"]}
    assert all(r.get("audio_code") != opaque for r in interactions)
    turns = result["sheets"]["turns"]
    assert turns, "ordinary answer must reach the turn projection"
    assert any(r.get("source_attempt_seq") == 1 for r in turns)
    assert all(r.get("audio_code") != opaque for r in turns)
    # The researcher-locked truth really exists, so the score projection is a
    # genuine non-empty contrast rather than a vacuous all().
    item_scores = result["sheets"]["item_scores"]
    assert item_scores
    assert all(r.get("audio_code") != opaque for r in item_scores)
    assert any(r["task_type"] == "单要素" for r in item_scores)
    # The authoritative summary the server wrote at auto-completion agrees with
    # the ledgers: one ordinary Attempt, and the repeat is not counted.
    with Session(api_clients.engine) as session:
        summary = session.get(SessionOutcomeSummary, SESSION_ID)
        assert summary is not None
        assert summary.total_attempts == 1
        assert summary.completed_attempts == 1
        assert summary.technical_failure_attempts == 0
        assert len(list(session.exec(select(AttemptEvent)))) == 1
    assert len(result["sheets"]["repeat_audio_manifest"]) == 1

    # Structural proof that no internal identifier column exists anywhere,
    # which bare-integer substring search cannot give.
    forbidden_columns = {
        "request_id", "repeat_request_id", "capture_processing_id",
        "capture_id", "record_command_id", "source_tts_command_id",
        "replay_command_id", "pause_control_event_seq", "command_id",
        "phrase_key", "normalized_text_sha256", "source_payload_sha256",
        "raw_audio_id", "session_id", "patient_id",
    }
    for name, rows in result["sheets"].items():
        for row in rows:
            assert not (set(row) & forbidden_columns), (name, sorted(set(row)))

    # The controlled copy is byte- and size-exact.
    copies = [p for p in (out / "_controlled_audio").rglob(f"{opaque}.*")]
    assert len(copies) == 1
    assert copies[0].stat().st_size == expected_size
    assert hashlib.sha256(copies[0].read_bytes()).hexdigest() == expected_checksum

    # Read the files that were actually written, not just the in-memory sheets.
    manifest_files = list(out.rglob("repeat_audio_manifest.csv"))
    assert len(manifest_files) == 1
    with manifest_files[0].open(encoding="utf-8-sig", newline="") as handle:
        manifest_rows = list(csv.reader(handle))
    assert manifest_rows[0] == [
        "capture_kind", "repeat_ordinal", "outcome", "opaque_audio_code"]
    assert len(manifest_rows) == 2
    assert manifest_rows[1] == ["explicit_repeat", "1", "replayed", opaque]

    score_files = list(out.rglob("item_scores.csv"))
    assert len(score_files) == 1
    with score_files[0].open(encoding="utf-8-sig", newline="") as handle:
        score_rows = list(csv.reader(handle))
    assert score_rows[0] == ["session_code", "subject_code", "summary", "task_type"]
    assert len(score_rows) == 2
    assert dict(zip(score_rows[0], score_rows[1]))["task_type"] == "单要素"

    # Three-way closure on the controlled copy: artifact ledger, physical file
    # and the capture-time expectations must all agree.
    with Session(api_clients.engine) as session:
        artifacts = [
            row for row in session.exec(select(ExportArtifact).where(
                ExportArtifact.batch_id == result["batch_id"],
                ExportArtifact.kind == "controlled_audio"))
            if Path(row.relative_path).name.startswith(f"{opaque}.")
        ]
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.sha256 == expected_checksum
    assert artifact.byte_count == expected_size
    # relative_path is recorded against the controlled-audio root the writer
    # used, so resolve it there and prove it is that exact physical file.
    artifact_path = (out / "_controlled_audio" / artifact.relative_path)
    assert artifact_path.resolve() == copies[0].resolve()
    assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == artifact.sha256
    assert artifact_path.stat().st_size == artifact.byte_count

    # Nothing in the written bundle leaks the phrase, its digests or ids.
    with Session(api_clients.engine) as session:
        request = session.get(AutopilotRepeatRequest, ids["request_id"])
        replay = session.get(RuntimeCommand, ids["replay_id"])
        source = session.get(RuntimeCommand, ids["source_id"])
        record = session.get(RuntimeCommand, ids["record_id"])
        train = session.get(TrainSession, SESSION_ID)
        # Actual sensitive values, not merely their column names.
        forbidden = [
            "再说一遍", "repeat_again",
            repeat_intent.normalized_text_sha256("再说一遍"),
            request.normalized_text_sha256, request.source_payload_sha256,
            request.raw_audio_id, answer_audio_id,
            SESSION_ID, train.patient_id,
            # Bare autoincrement ids are single characters here and would match
            # any path by coincidence; the high-entropy identifiers below are
            # what a real leak would look like.
            replay.idempotency_key, source.idempotency_key,
            record.idempotency_key,
            "phrase_key", "normalized_text_sha256", "capture_processing_id",
            "repeat_request_id", "source_tts_command_id", "replay_command_id",
        ]
    written = [p for p in out.rglob("*") if p.is_file()]
    assert written
    for path in written:
        blob = path.read_bytes()
        relative = str(path.relative_to(out))
        for needle in forbidden:
            assert needle.encode("utf-8") not in blob, (relative, needle)
            assert needle not in relative, (relative, needle)

    with Session(api_clients.engine) as session:
        # The ledger, capture and command rows are untouched by exporting.
        request = session.get(AutopilotRepeatRequest, ids["request_id"])
        capture = session.get(AttemptCaptureProcessing, ids["capture_id"])
        assert (request.repeat_ordinal, request.outcome) == (1, "replayed")
        assert capture.disposition == "repeat_replayed"
        assert capture.final_attempt_id is None
        # Exactly one Attempt exists — the ordinary answer — and it is not the
        # repeat capture's.
        attempt_rows = list(session.exec(select(AttemptEvent)))
        assert len(attempt_rows) == 1
        assert attempt_rows[0].raw_audio_id == answer_audio_id
        assert attempt_rows[0].raw_audio_id != request.raw_audio_id
