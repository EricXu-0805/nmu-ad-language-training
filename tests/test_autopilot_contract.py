import json

import pytest
from pydantic import ValidationError

from app.autopilot_contract import (
    AutopilotAckIn,
    RecordCommandPayload,
    TtsCommandPayload,
    _ACK_FAILURE_STAGES,
    effect_after_tts_ended,
    transition_for_ack,
)


def test_tts_ended_is_the_only_success_ack_that_can_route_followup():
    assert transition_for_ack("tts", "pending", "tts_started").effect == "none"
    assert transition_for_ack("tts", "started", "tts_ended").effect == "route_after_tts"
    assert transition_for_ack("tts", "pending", "tts_ended").effect == "route_after_tts"
    with pytest.raises(ValueError):
        transition_for_ack("tts", "started", "record_started")


def test_tts_purpose_prevents_feedback_or_answer_from_reopening_microphone():
    assert effect_after_tts_ended("question") == "create_record"
    assert effect_after_tts_ended("cue") == "create_record"
    assert effect_after_tts_ended("feedback") == "advance"
    assert effect_after_tts_ended("tell_answer") == "advance"


def test_record_stopped_requires_receipt_before_attempt_effect():
    started = transition_for_ack("record", "pending", "record_started")
    stopped = transition_for_ack("record", "started", "record_stopped")
    assert (started.command_status, started.effect) == ("started", "none")
    assert (stopped.command_status, stopped.effect) == ("succeeded", "process_attempt")


@pytest.mark.parametrize("kind,status,ack", [
    ("tts", "succeeded", "tts_ended"),
    ("record", "succeeded", "record_stopped"),
    ("record", "cancelled", "record_started"),
    ("tts", "failed", "tts_started"),
])
def test_terminal_or_cancelled_command_rejects_late_ack(kind, status, ack):
    with pytest.raises(ValueError):
        transition_for_ack(kind, status, ack)


def test_record_stopped_ack_is_complete_and_strict():
    ack = AutopilotAckIn(
        idempotency_key="ack-record-0001",
        ack_type="record_stopped",
        control_generation=2,
        runner_generation=4,
        command_revision=3,
        device_event_seq=7,
        raw_audio_id="raw-0001",
        receipt_server_seq=9,
        checksum="a" * 64,
        byte_count=1234,
        duration_seconds=2.5,
        stop_reason="silence",
    )
    assert ack.raw_audio_id == "raw-0001"
    with pytest.raises(ValidationError):
        AutopilotAckIn(
            idempotency_key="ack-record-0002",
            ack_type="record_stopped",
            control_generation=2,
            runner_generation=4,
            command_revision=3,
            device_event_seq=8,
            raw_audio_id="raw-0002",
            receipt_server_seq=10,
            checksum="b" * 64,
            byte_count=100,
            stop_reason="silence",
        )


def test_non_stop_ack_cannot_smuggle_audio_or_error_fields():
    with pytest.raises(ValidationError):
        AutopilotAckIn(
            idempotency_key="ack-record-0003",
            ack_type="record_started",
            control_generation=1,
            runner_generation=1,
            command_revision=0,
            device_event_seq=1,
            raw_audio_id="raw-0003",
        )
    with pytest.raises(ValidationError):
        AutopilotAckIn(
            idempotency_key="ack-tts-000001",
            ack_type="tts_ended",
            control_generation=1,
            runner_generation=1,
            command_revision=0,
            device_event_seq=2,
            error_code="speaker_failed",
        )


def test_failed_ack_requires_bounded_machine_error_code():
    ok = AutopilotAckIn(
        idempotency_key="ack-tts-failed-1",
        ack_type="tts_failed",
        control_generation=1,
        runner_generation=1,
        command_revision=0,
        device_event_seq=1,
        error_code="audio_playback_failed",
    )
    assert ok.error_code == "audio_playback_failed"
    with pytest.raises(ValidationError):
        AutopilotAckIn(
            idempotency_key="ack-tts-failed-2",
            ack_type="tts_failed",
            control_generation=1,
            runner_generation=1,
            command_revision=0,
            device_event_seq=2,
        )
    with pytest.raises(ValidationError):
        AutopilotAckIn(
            idempotency_key="ack-tts-failed-3",
            ack_type="tts_failed",
            control_generation=1,
            runner_generation=1,
            command_revision=0,
            device_event_seq=3,
            error_code="invented_free_form_reason",
        )


def test_failure_stage_membership_is_pinned():
    # 双端手抄闭集：这里逐个成员钉死，前端 autopilotProtocol.test.ts 钉另一份。
    assert _ACK_FAILURE_STAGES == frozenset({
        "credential_rotated",
        "authority_changed",
        "blob_invalid",
        "media_decode_error",
        "play_rejected",
        "media_timeout",
        "fetch_failed",
        "executor_start_failed",
    })


def test_failed_ack_may_carry_a_closed_failure_stage():
    staged = AutopilotAckIn(
        idempotency_key="ack-tts-failed-4",
        ack_type="tts_failed",
        control_generation=1,
        runner_generation=1,
        command_revision=0,
        device_event_seq=4,
        error_code="audio_playback_failed",
        failure_stage="media_timeout",
    )
    assert staged.failure_stage == "media_timeout"
    assert staged.event_payload() == {
        "error_code": "audio_playback_failed",
        "failure_stage": "media_timeout",
    }
    bare = AutopilotAckIn(
        idempotency_key="ack-rec-failed-5",
        ack_type="record_failed",
        control_generation=1,
        runner_generation=1,
        command_revision=0,
        device_event_seq=5,
        error_code="recording_start_failed",
    )
    assert bare.failure_stage is None
    assert bare.event_payload() == {"error_code": "recording_start_failed"}


def test_failure_stage_outside_closed_set_is_rejected():
    with pytest.raises(ValidationError):
        AutopilotAckIn(
            idempotency_key="ack-tts-failed-6",
            ack_type="tts_failed",
            control_generation=1,
            runner_generation=1,
            command_revision=0,
            device_event_seq=6,
            error_code="audio_playback_failed",
            failure_stage="invented_stage",
        )


def test_non_failure_ack_cannot_carry_failure_stage():
    with pytest.raises(ValidationError):
        AutopilotAckIn(
            idempotency_key="ack-tts-ended-9",
            ack_type="tts_ended",
            control_generation=1,
            runner_generation=1,
            command_revision=0,
            device_event_seq=7,
            media_ended=True,
            failure_stage="media_timeout",
        )


def test_terminal_and_started_ack_facts_are_closed_and_semantic():
    ended = AutopilotAckIn(
        idempotency_key="ack-tts-ended-0001",
        ack_type="tts_ended",
        control_generation=1,
        runner_generation=1,
        command_revision=0,
        device_event_seq=1,
        media_ended=True,
        media_duration_ms=850,
    )
    assert ended.event_payload() == {
        "media_ended": True,
        "media_duration_ms": 850,
    }
    with pytest.raises(ValidationError):
        AutopilotAckIn(
            idempotency_key="ack-tts-ended-0002",
            ack_type="tts_ended",
            control_generation=1,
            runner_generation=1,
            command_revision=0,
            device_event_seq=2,
            media_ended=False,
        )

    started = AutopilotAckIn(
        idempotency_key="ack-record-started-0001",
        ack_type="record_started",
        control_generation=1,
        runner_generation=1,
        command_revision=0,
        device_event_seq=3,
        mime_type="audio/webm;codecs=opus",
        sample_rate_hz=48_000,
        channels=1,
    )
    assert started.event_payload() == {
        "mime_type": "audio/webm;codecs=opus",
        "sample_rate_hz": 48_000,
        "channels": 1,
    }


def test_command_payloads_are_turn_bound_and_forbid_extra_input():
    record = RecordCommandPayload(
        raw_audio_id="raw-command-0001",
        turn_key="SE_胡萝卜#1",
        item_id="SE_胡萝卜",
        turn_seq=1,
        cue_level=0,
        max_duration_seconds=30,
    )
    assert record.turn_key == "SE_胡萝卜#1"
    with pytest.raises(ValidationError):
        RecordCommandPayload(
            raw_audio_id="raw-command-0002",
            turn_key="SE_苹果#2",
            item_id="SE_苹果",
            turn_seq=1,
            cue_level=0,
            max_duration_seconds=30,
        )
    with pytest.raises(ValidationError):
        RecordCommandPayload(
            turn_key="SE_苹果#1",
            item_id="SE_苹果",
            turn_seq=1,
            cue_level=0,
            max_duration_seconds=30,
        )
    with pytest.raises(ValidationError):
        TtsCommandPayload(
            speech_key="question:SE_胡萝卜:1:0",
            speech_text="请看这张图片，这是什么？",
            purpose="question",
            item_id="SE_胡萝卜",
            turn_seq=1,
            cue_level=0,
            patient_name="不允许",
        )


@pytest.mark.parametrize("response_path", ["unknown", "close", "silence"])
@pytest.mark.parametrize("purpose", ["cue", "feedback"])
def test_level_one_cue_and_feedback_require_closed_response_path(
    response_path, purpose,
):
    payload = TtsCommandPayload(
        speech_key=f"p0a.{purpose}.2",
        speech_text="源协议冻结话术",
        purpose=purpose,
        item_id="SE_胡萝卜",
        turn_seq=1,
        cue_level=1,
        response_path=response_path,
    )
    assert payload.response_path == response_path

    with pytest.raises(ValidationError):
        TtsCommandPayload(
            speech_key=f"p0a.{purpose}.missing",
            speech_text="源协议冻结话术",
            purpose=purpose,
            item_id="SE_胡萝卜",
            turn_seq=1,
            cue_level=1,
        )


@pytest.mark.parametrize(
    ("purpose", "cue_level"),
    [("question", 0), ("cue", 2), ("feedback", 0),
     ("feedback", 2), ("tell_answer", 3)],
)
def test_response_path_is_forbidden_outside_level_one_path_bound_stages(
    purpose, cue_level,
):
    with pytest.raises(ValidationError):
        TtsCommandPayload(
            speech_key=f"p0a.{purpose}.{cue_level}",
            speech_text="源协议冻结话术",
            purpose=purpose,
            item_id="SE_胡萝卜",
            turn_seq=1,
            cue_level=cue_level,
            response_path="unknown",
        )


def test_forbidden_stage_rejects_explicit_null_and_serializes_no_path_key():
    question = TtsCommandPayload(
        speech_key="p0a.question.1",
        speech_text="请看图片",
        purpose="question",
        item_id="SE_胡萝卜",
        turn_seq=1,
        cue_level=0,
    )
    assert "response_path" not in question.model_dump(exclude_none=True)
    with pytest.raises(ValidationError):
        TtsCommandPayload(
            speech_key="p0a.question.null",
            speech_text="请看图片",
            purpose="question",
            item_id="SE_胡萝卜",
            turn_seq=1,
            cue_level=0,
            response_path=None,
        )


def test_response_path_rejects_free_form_branch_names():
    with pytest.raises(ValidationError):
        TtsCommandPayload(
            speech_key="p0a.cue.2",
            speech_text="源协议冻结话术",
            purpose="cue",
            item_id="SE_胡萝卜",
            turn_seq=1,
            cue_level=1,
            response_path="client_selected_branch",
        )


def _record_stopped(stop_reason: str = "silence", **facts) -> AutopilotAckIn:
    return AutopilotAckIn(
        idempotency_key="ack-record-vad-0001",
        ack_type="record_stopped",
        control_generation=1,
        runner_generation=1,
        command_revision=1,
        device_event_seq=2,
        raw_audio_id="raw-vad-0001",
        receipt_server_seq=3,
        checksum="c" * 64,
        byte_count=4096,
        duration_seconds=3.5,
        stop_reason=stop_reason,
        **facts,
    )


@pytest.mark.parametrize(("stop_reason", "facts", "payload"), [
    # 旧平板两个键都不发:历史 {"stop_reason"} 原样不变。
    ("user_done", {}, {"stop_reason": "user_done"}),
    ("user_done", {"vad_armed": None, "vad_voiced_ms": None}, {"stop_reason": "user_done"}),
    ("max_duration", {"vad_armed": False}, {"stop_reason": "max_duration", "vad_armed": False}),
    ("silence", {"vad_armed": True}, {"stop_reason": "silence", "vad_armed": True}),
    ("silence", {"vad_armed": True, "vad_voiced_ms": 0},
     {"stop_reason": "silence", "vad_armed": True, "vad_voiced_ms": 0}),
    ("user_done", {"vad_armed": True, "vad_voiced_ms": 600_000},
     {"stop_reason": "user_done", "vad_armed": True, "vad_voiced_ms": 600_000}),
])
def test_record_stopped_vad_facts_are_optional_and_persist_verbatim(stop_reason, facts, payload):
    ack = _record_stopped(stop_reason, **facts)
    assert ack.event_payload() == payload
    # 服务层会对 model_construct 实例按 python 形态重验;严格类型在重验里也不能松。
    assert AutopilotAckIn.model_validate(
        ack.model_dump(mode="python")).event_payload() == payload


@pytest.mark.parametrize("facts", [
    {"vad_voiced_ms": 1200},
    {"vad_armed": None, "vad_voiced_ms": 1200},
    {"vad_armed": False, "vad_voiced_ms": 0},
    {"vad_armed": 1},
    {"vad_armed": 0},
    {"vad_armed": "true"},
    {"vad_armed": "yes"},
    {"vad_armed": True, "vad_voiced_ms": True},
    {"vad_armed": True, "vad_voiced_ms": 1200.0},
    {"vad_armed": True, "vad_voiced_ms": "1200"},
    {"vad_armed": True, "vad_voiced_ms": -1},
    {"vad_armed": True, "vad_voiced_ms": 600_001},
])
def test_record_stopped_rejects_contradictory_or_mistyped_vad_facts(facts):
    with pytest.raises(ValidationError):
        _record_stopped(**facts)


@pytest.mark.parametrize("raw", [
    '{"vad_armed": 1}',
    '{"vad_armed": "true"}',
    '{"vad_armed": true, "vad_voiced_ms": true}',
    '{"vad_armed": true, "vad_voiced_ms": 1200.0}',
    '{"vad_armed": true, "vad_voiced_ms": 1e3}',
    '{"vad_voiced_ms": 1200}',
])
def test_record_stopped_vad_facts_are_strict_on_the_json_wire(raw):
    body = _record_stopped().model_dump(mode="json", exclude_none=True)
    body.update(json.loads(raw))
    with pytest.raises(ValidationError):
        AutopilotAckIn.model_validate_json(json.dumps(body))
    with pytest.raises(ValidationError):
        AutopilotAckIn.model_validate(body)


@pytest.mark.parametrize(("ack_type", "shape"), [
    ("tts_started", {}),
    ("tts_ended", {"media_ended": True}),
    ("tts_interrupted", {"media_stopped": True, "interrupt_reason": "voice_activity",
                         "media_duration_ms": 750}),
    ("tts_failed", {"error_code": "audio_playback_failed"}),
    ("record_started", {"mime_type": "audio/webm"}),
    ("record_failed", {"error_code": "recording_runtime_failed"}),
])
@pytest.mark.parametrize("facts", [
    {"vad_armed": True},
    {"vad_armed": False},
    {"vad_armed": True, "vad_voiced_ms": 1200},
])
def test_only_record_stopped_may_carry_vad_facts(ack_type, shape, facts):
    base = dict(
        idempotency_key="ack-not-stopped-0001",
        ack_type=ack_type,
        control_generation=1,
        runner_generation=1,
        command_revision=0,
        device_event_seq=1,
        **shape,
    )
    assert AutopilotAckIn(**base).event_payload() is not None
    with pytest.raises(ValidationError):
        AutopilotAckIn(**base, **facts)
