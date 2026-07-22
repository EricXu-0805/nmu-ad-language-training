"""服务端自动驾驶命令与设备回执的封闭契约。

设备回执是不可信输入：它只能报告有限的播放/录音事实，不能选择下一题、
提示等级或 AI 分支。服务层还必须用持久化的 command/session/generation 与
AudioCaptureReceipt 复核这些字段；本模块只负责结构和值域以及合法状态迁移。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


CommandKind = Literal["tts", "record"]
ResponsePath = Literal["unknown", "close", "silence"]
CommandStatus = Literal[
    "pending", "started", "succeeded", "failed", "cancelled",
]
AckType = Literal[
    "tts_started", "tts_ended", "tts_failed",
    "record_started", "record_stopped", "record_failed",
]

_ACK_ERROR_CODES = frozenset({
    "audio_playback_failed",
    "tts_cancelled",
    "microphone_denied",
    "microphone_unavailable",
    "recording_start_failed",
    "recording_runtime_failed",
    "recording_upload_failed",
    "device_command_timeout",
    "device_runtime_failed",
})


class TtsCommandPayload(BaseModel):
    """由服务端从冻结计划/协议生成；设备不能回填自由话术。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    speech_key: str = Field(
        min_length=1, max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$",
    )
    speech_text: str = Field(min_length=1, max_length=2_000)
    purpose: Literal["question", "cue", "feedback", "tell_answer"]
    item_id: str = Field(min_length=1, max_length=160)
    turn_seq: int = Field(ge=1)
    cue_level: int = Field(ge=0, le=3)
    # The first failed response selects one source-authored cue branch.  Persist
    # that branch on both the level-1 cue and its success feedback so a retry or
    # later semantic validation cannot silently substitute another valid line.
    response_path: ResponsePath | None = None

    @model_validator(mode="after")
    def _response_path_matches_stage(self) -> "TtsCommandPayload":
        path_bound_stage = (
            self.purpose == "cue" and self.cue_level == 1
        ) or (
            self.purpose == "feedback" and self.cue_level == 1
        )
        path_was_supplied = "response_path" in self.model_fields_set
        if (path_bound_stage
                and (not path_was_supplied or self.response_path is None)):
            raise ValueError(
                "response_path 必须且只能绑定一级提示及其成功反馈"
            )
        if not path_bound_stage and path_was_supplied:
            raise ValueError(
                "非一级路径阶段禁止携带 response_path"
            )
        return self


class RecordCommandPayload(BaseModel):
    """开麦指令只绑定一个冻结环节和一条预分配录音。

    ``raw_audio_id`` 必须在创建指令时由服务器生成并预建
    AudioAssetRow。设备只能向该 ID 补传，不得从同一 turn 的旧录音
    中挑选一条来满足回执。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    raw_audio_id: str = Field(
        min_length=1, max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$",
    )
    turn_key: str = Field(
        min_length=3, max_length=192,
        pattern=r"^[^#\r\n]{1,160}#[1-9][0-9]{0,5}$",
    )
    item_id: str = Field(min_length=1, max_length=160)
    turn_seq: int = Field(ge=1)
    cue_level: int = Field(ge=0, le=3)
    max_duration_seconds: int = Field(ge=1, le=300)
    contains_direct_identifier: bool = False

    @model_validator(mode="after")
    def _turn_key_matches(self) -> "RecordCommandPayload":
        if self.turn_key != f"{self.item_id}#{self.turn_seq}":
            raise ValueError("turn_key 必须与 item_id/turn_seq 精确一致")
        return self


class AutopilotAckIn(BaseModel):
    """设备回执载荷。

    ``idempotency_key`` 由设备为一次事实生成，并与 command_id 联合唯一。
    服务器接收时间才是排序真值；client_observed_ms 只用于诊断且不进入状态判断。
    """

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(
        min_length=8, max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
    )
    ack_type: AckType
    control_generation: int = Field(ge=1)
    runner_generation: int = Field(ge=1)
    # 设备必须回显它实际执行的命令版本；服务端用 CAS 拒绝被取消、重发
    # 或已由另一回执推进过的旧页面事实。
    command_revision: int = Field(ge=0)
    # 单个随机设备的递增事件序号，与设备摘要联合唯一，补充幂等键以检出
    # 同一设备乱序/回滚；服务端不使用客户端时间判先后。
    device_event_seq: int = Field(ge=1)
    client_observed_ms: int | None = Field(default=None, ge=0)
    # Closed, event-specific device facts.  Keeping these fields typed prevents
    # arbitrary strings from becoming research-control evidence.
    media_ended: bool | None = None
    media_duration_ms: int | None = Field(
        default=None, ge=0, le=21_600_000)
    mime_type: Literal[
        "audio/webm", "audio/webm;codecs=opus", "audio/ogg;codecs=opus",
        "audio/mp4",
    ] | None = None
    sample_rate_hz: int | None = Field(default=None, ge=8_000, le=192_000)
    channels: Literal[1, 2] | None = None
    stop_reason: Literal[
        "silence", "user_done", "max_duration", "stream_end",
    ] | None = None
    raw_audio_id: str | None = Field(
        default=None, min_length=1, max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$",
    )
    receipt_server_seq: int | None = Field(default=None, ge=1)
    checksum: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$",
    )
    byte_count: int | None = Field(default=None, gt=0)
    duration_seconds: float | None = Field(default=None, ge=0, le=21_600)
    error_code: str | None = Field(
        default=None, min_length=1, max_length=96,
        pattern=r"^[a-z][a-z0-9_]{0,95}$",
    )

    @model_validator(mode="after")
    def _shape_matches_ack(self) -> "AutopilotAckIn":
        receipt_fields = (
            self.raw_audio_id,
            self.receipt_server_seq,
            self.checksum,
            self.byte_count,
            self.duration_seconds,
        )
        if self.ack_type == "record_stopped":
            if any(value is None for value in receipt_fields):
                raise ValueError("record_stopped 必须携带完整录音收据事实")
            if self.error_code is not None:
                raise ValueError("成功收麦回执不得携带 error_code")
        elif any(value is not None for value in receipt_fields):
            raise ValueError("只有 record_stopped 可以携带录音收据字段")

        failed = self.ack_type in {"tts_failed", "record_failed"}
        if failed != (self.error_code is not None):
            raise ValueError("失败回执必须且只能携带 error_code")
        if self.error_code is not None and self.error_code not in _ACK_ERROR_CODES:
            raise ValueError("失败回执 error_code 不在封闭机器码集中")

        tts_fields = (self.media_ended, self.media_duration_ms)
        record_start_fields = (self.mime_type, self.sample_rate_hz, self.channels)
        if self.ack_type == "tts_started":
            if self.media_ended is not None or any(
                    value is not None for value in record_start_fields
            ) or self.stop_reason is not None:
                raise ValueError("tts_started 回执携带了非法事件事实")
        elif self.ack_type == "tts_ended":
            if self.media_ended is not True:
                raise ValueError("tts_ended 必须明确证明 media_ended=true")
            if any(value is not None for value in record_start_fields) \
                    or self.stop_reason is not None:
                raise ValueError("tts_ended 回执携带了非法事件事实")
        elif self.ack_type == "tts_failed":
            if any(value is not None for value in tts_fields + record_start_fields) \
                    or self.stop_reason is not None:
                raise ValueError("tts_failed 回执携带了非法事件事实")
        elif self.ack_type == "record_started":
            if self.mime_type is None:
                raise ValueError("record_started 必须携带受限 mime_type")
            if any(value is not None for value in tts_fields) \
                    or self.stop_reason is not None:
                raise ValueError("record_started 回执携带了非法事件事实")
        elif self.ack_type == "record_stopped":
            if self.stop_reason is None:
                raise ValueError("record_stopped 必须携带受限 stop_reason")
            if any(value is not None for value in tts_fields + record_start_fields):
                raise ValueError("record_stopped 回执携带了非法事件事实")
        elif self.ack_type == "record_failed":
            if any(value is not None for value in tts_fields + record_start_fields) \
                    or self.stop_reason is not None:
                raise ValueError("record_failed 回执携带了非法事件事实")
        return self

    def event_payload(self) -> dict[str, bool | int | str]:
        """Return the exact bounded payload persisted in RuntimeCommandAck."""
        if self.ack_type == "tts_started":
            return ({"media_duration_ms": self.media_duration_ms}
                    if self.media_duration_ms is not None else {})
        if self.ack_type == "tts_ended":
            payload: dict[str, bool | int | str] = {"media_ended": True}
            if self.media_duration_ms is not None:
                payload["media_duration_ms"] = self.media_duration_ms
            return payload
        if self.ack_type in {"tts_failed", "record_failed"}:
            assert self.error_code is not None
            return {"error_code": self.error_code}
        if self.ack_type == "record_started":
            assert self.mime_type is not None
            payload = {"mime_type": self.mime_type}
            if self.sample_rate_hz is not None:
                payload["sample_rate_hz"] = self.sample_rate_hz
            if self.channels is not None:
                payload["channels"] = self.channels
            return payload
        assert self.stop_reason is not None
        return {"stop_reason": self.stop_reason}


@dataclass(frozen=True)
class AckTransition:
    command_status: CommandStatus
    effect: Literal["none", "route_after_tts", "process_attempt", "pause"]


_TRANSITIONS: dict[tuple[CommandKind, CommandStatus, AckType], AckTransition] = {
    ("tts", "pending", "tts_started"): AckTransition("started", "none"),
    # 很短的音频可能在 started 回执丢包后直接到 ended；允许 ended 从 pending
    # 收敛，但绝不允许 started 触发录音。
    ("tts", "pending", "tts_ended"): AckTransition("succeeded", "route_after_tts"),
    ("tts", "started", "tts_ended"): AckTransition("succeeded", "route_after_tts"),
    ("tts", "pending", "tts_failed"): AckTransition("failed", "pause"),
    ("tts", "started", "tts_failed"): AckTransition("failed", "pause"),
    ("record", "pending", "record_started"): AckTransition("started", "none"),
    # 与 TTS 同理，stopped 的完整持久收据可以弥补 started 回执丢包。
    ("record", "pending", "record_stopped"): AckTransition("succeeded", "process_attempt"),
    ("record", "started", "record_stopped"): AckTransition("succeeded", "process_attempt"),
    ("record", "pending", "record_failed"): AckTransition("failed", "pause"),
    ("record", "started", "record_failed"): AckTransition("failed", "pause"),
}


def transition_for_ack(
        command_kind: CommandKind, command_status: CommandStatus,
        ack_type: AckType) -> AckTransition:
    """返回唯一合法迁移；乱序、跨类型或终态后新事实一律拒绝。"""
    transition = _TRANSITIONS.get((command_kind, command_status, ack_type))
    if transition is None:
        raise ValueError(
            f"非法自动驾驶回执迁移: {command_kind}/{command_status}/{ack_type}"
        )
    return transition


def effect_after_tts_ended(
        purpose: Literal["question", "cue", "feedback", "tell_answer"],
) -> Literal["create_record", "advance"]:
    """Only a heard question/cue opens the microphone.

    Feedback and answer disclosure are terminal speech for the current attempt;
    replaying them must never create another recording command.
    """
    return "create_record" if purpose in {"question", "cue"} else "advance"
