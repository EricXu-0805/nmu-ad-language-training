"""Simulation-only server-authoritative autopilot domain service.

This module owns no HTTP route and performs no provider or filesystem I/O.  Every
mutation is flushed, but never committed: the caller owns the surrounding database
transaction and must roll it back when :class:`AutopilotServiceError` is raised.

The legacy ``P0a`` scope key remains a database/API compatibility identifier.  The
scope may now advance sequentially across every version-bound protocol position
that the server can prove complete.  It never skips a gap: a missing operational
rubric/protocol pauses server ownership and leaves the runtime intervention open.
The entire scope remains simulation-only.  The HTTP transaction adapter owns the
final ``SessionRuntimeState``/live projection because it also owns the immutable
outcome-summary and audio-integrity gates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import secrets
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import update
from sqlmodel import Session, select

from . import (autopilot_ledger, autopilot_positions, content, evidence_ledger,
               patient_presentation, runtime)
from .autopilot_contract import (
    AutopilotAckIn,
    RecordCommandPayload,
    ResponsePath,
    TtsCommandPayload,
    effect_after_tts_ended,
    transition_for_ack,
)
from .enums import AnswerType, AudioStatus
from .models import (
    AudioAssetRow,
    AudioCaptureReceipt,
    AttemptEvent,
    AutopilotControlEvent,
    InteractionEvent,
    ItemEvent,
    LiveState,
    Patient,
    PatientDeviceCapability,
    RuntimeCommand,
    RuntimeCommandAck,
    Session as TrainSession,
    SessionAutopilotState,
    SessionRuntimeState,
    TurnEvent,
    VisitPlan,
)


P0A_SCOPE_KEY = "p0a_sim_first_single_v1"
P0A_FEATURE_ENV = "ENABLE_AUTOPILOT_P0A_SIMULATION"
SIMULATION_DATA_ENV = "ALLOW_SIMULATION_DATA"
P0A_SOURCE = "p0a_domain_service"

_TRUE_VALUES = frozenset({"1", "true", "yes"})
_DENIED_CONSENT = frozenset({
    "未同意", "已撤回", "拒绝", "不同意",
    "denied", "withdrawn", "refused", "declined", "rejected",
})
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_EXTERNAL_FENCE_SOURCES = frozenset({
    "patient_rec_failure",
    "session_abort",
    "cloud_processing_consent_revoked",
    "subject_withdrawal",
})
_EXTERNAL_FENCE_ACTOR = {
    "patient_rec_failure": "device",
    "session_abort": "researcher",
    "cloud_processing_consent_revoked": "system",
    "subject_withdrawal": "researcher",
}
_DRAINABLE_PAUSE_SOURCES = _EXTERNAL_FENCE_SOURCES | frozenset({
    "protocol_position_gap",
})


class AutopilotServiceError(RuntimeError):
    """Stable, non-sensitive domain failure for a later HTTP adapter."""

    def __init__(self, code: str, message: str, *, context: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        # Machine-readable operational metadata only.  Never attach ASR text,
        # patient identifiers, answers, or device capabilities here.
        self.context = dict(context or {})


class DeviceTtsPayload(BaseModel):
    """Current-only device projection; canonical item ids stay server-side."""

    model_config = ConfigDict(extra="forbid")

    speech_key: str
    speech_text: str
    purpose: Literal["question", "cue", "feedback", "tell_answer"]
    response_path: ResponsePath | None = None


class DeviceRecordPayload(BaseModel):
    """An issued capture slot plus its exact preceding question/cue display."""

    model_config = ConfigDict(extra="forbid")

    raw_audio_id: str
    turn_ref: str
    max_duration_seconds: int = Field(ge=1, le=300)
    contains_direct_identifier: bool
    presentation_speech_key: str
    presentation_speech_text: str
    presentation_purpose: Literal["question", "cue"]


class NextCommandProjection(BaseModel):
    """Minimum projection needed by the paired patient device."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    command_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
    )
    command_seq: int = Field(ge=1)
    kind: Literal["tts", "record"]
    state: Literal["pending", "started"]
    command_revision: int = Field(ge=0)
    control_generation: int = Field(ge=1)
    runner_generation: int = Field(ge=1)
    item_ref: str
    turn_seq: int = Field(ge=1)
    attempt_seq: int = Field(ge=1)
    prompt_level: int = Field(ge=0, le=3)
    payload: DeviceTtsPayload | DeviceRecordPayload


class StartP0aResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_key: Literal["p0a_sim_first_single_v1"] = P0A_SCOPE_KEY
    status: str
    state_revision: int = Field(ge=0)
    replayed: bool
    command: NextCommandProjection | None


class AutopilotStatusReceipt(BaseModel):
    """Account-safe ownership/status projection with no patient command content."""

    model_config = ConfigDict(extra="forbid")

    scope_key: Literal["disabled", "p0a_sim_first_single_v1"]
    mode: Literal["disabled", "autonomous", "manual"]
    status: Literal[
        "idle", "running", "waiting_tts", "waiting_recording",
        "processing_attempt", "manual_draining", "paused",
        "scope_completed", "failed",
    ]
    state_revision: int = Field(ge=0)
    server_owned: bool
    current_command_kind: Literal["tts", "record"] | None = None
    last_error_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=96,
        pattern=r"^[a-z][a-z0-9_]{0,95}$",
    )


class DrainAckReceipt(BaseModel):
    """Opaque device receipt for one proven media-drain boundary."""

    model_config = ConfigDict(extra="forbid")

    replayed: bool
    state_revision: int = Field(ge=0)


class DrainTargetProjection(BaseModel):
    """Minimum refresh-recovery projection for one exact media drain."""

    model_config = ConfigDict(extra="forbid")

    command_key: str
    state_revision: int = Field(ge=0)


class RouteTtsEndedResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_key: Literal["p0a_sim_first_single_v1"] = P0A_SCOPE_KEY
    status: Literal["waiting_tts", "waiting_recording", "paused", "scope_completed"]
    replayed: bool
    command: NextCommandProjection | None


class RouteCompletedAttemptResult(BaseModel):
    """The next frozen speech selected from one terminal operational attempt."""

    model_config = ConfigDict(extra="forbid")

    scope_key: Literal["p0a_sim_first_single_v1"] = P0A_SCOPE_KEY
    status: Literal["waiting_tts"] = "waiting_tts"
    attempt_id: int = Field(ge=1)
    replayed: bool
    state_revision: int = Field(ge=0)
    command: NextCommandProjection


class ApplyDeviceAckResult(BaseModel):
    """Stable receipt for the single device-ACK mutation boundary.

    Exact transport retries deliberately do not project a possibly newer command:
    the capability that emitted the original ACK may since have been rotated out.
    """

    model_config = ConfigDict(extra="forbid")

    scope_key: Literal["p0a_sim_first_single_v1"] = P0A_SCOPE_KEY
    ack_idempotency_key: str
    ack_type: Literal[
        "tts_started", "tts_ended", "tts_failed",
        "record_started", "record_stopped", "record_failed",
    ]
    replayed: bool
    command_state: Literal[
        "pending", "started", "succeeded", "failed", "cancelled",
    ]
    command_revision: int = Field(ge=0)
    status: Literal[
        "idle", "running", "waiting_tts", "waiting_recording",
        "processing_attempt", "manual_draining", "paused",
        "scope_completed", "failed",
    ]
    state_revision: int = Field(ge=0)
    command: NextCommandProjection | None = None


@dataclass(frozen=True)
class _DefinitionBinding:
    item_bank_version_id: str
    item_bank_definition_digest: str
    autopilot_protocol_version_id: str
    autopilot_protocol_definition_digest: str


@dataclass(frozen=True)
class _P0aContent:
    item_bank_version_id: str
    item_bank_definition_digest: str
    autopilot_protocol_version_id: str
    autopilot_protocol_definition_digest: str
    item_index: int
    item_id: str
    turn_seq: int
    response_role: str
    target_word: str
    image_id: str
    initial_prompt: str
    cue1_unknown: str
    cue1_close: str
    cue1_silence: str
    cue2: str
    tell_answer: str
    success_line: str
    success_after_cue1_unknown: str
    success_after_cue1_close: str
    success_after_cue1_silence: str
    success_after_cue2: str
    allowed_tts_lines: frozenset[str]
    max_duration_seconds: int


@dataclass(frozen=True)
class _P0aGate:
    train_session: TrainSession
    patient: Patient
    runtime_state: SessionRuntimeState
    active_capability: PatientDeviceCapability
    selected: _P0aContent


@dataclass(frozen=True)
class _AttemptRouteDecision:
    purpose: Literal["cue", "feedback", "tell_answer"]
    speech_text: str
    prompt_level: int
    attempt_seq: int
    response_path: ResponsePath | None = None


@dataclass(frozen=True)
class TerminalEvidenceMaterialization:
    """One operational terminal attempt projected into the review ledger.

    This receipt deliberately says nothing about research truth.  In particular,
    the resulting :class:`TurnEvent` remains unlocked and has no confirmed text.
    """

    item_event_id: int
    turn_event_id: int
    replayed: bool


def _fail(code: str, message: str, *, context: dict | None = None) -> None:
    raise AutopilotServiceError(code, message, context=context)


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def p0a_feature_enabled() -> bool:
    """Both deployment switches must be explicit; the default is always off."""
    return _enabled(P0A_FEATURE_ENV) and _enabled(SIMULATION_DATA_ENV)


def _enum_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


def _required_text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("autopilot_content_incomplete", f"P0a 当前题缺少 {code}")
    return value.strip()


def _definition_binding(
    bank: content.ItemBank,
    protocol: dict,
) -> _DefinitionBinding:
    protocol_issues = content.validate_autopilot_protocol(protocol)
    if protocol_issues:
        _fail(
            "autopilot_protocol_invalid",
            "P0a 自动驾驶协议不完整：" + "；".join(protocol_issues),
        )
    return _DefinitionBinding(
        item_bank_version_id=bank.version_id,
        item_bank_definition_digest=content.item_bank_definition_digest(bank),
        autopilot_protocol_version_id=str(protocol["protocol_version_id"]),
        autopilot_protocol_definition_digest=(
            content.autopilot_protocol_definition_digest(protocol)),
    )


def _require_session_definition_binding(
    train_session: TrainSession,
    bank: content.ItemBank,
    protocol: dict,
) -> _DefinitionBinding:
    binding = _definition_binding(bank, protocol)
    values = (
        train_session.item_bank_definition_digest,
        train_session.autopilot_protocol_version_id,
        train_session.autopilot_protocol_definition_digest,
    )
    if any(value is None or not str(value).strip() for value in values):
        _fail(
            "autopilot_definition_binding_missing",
            "场次来自旧数据，缺少完整题库/自动化协议定义绑定",
        )
    if train_session.item_bank_version_id != binding.item_bank_version_id:
        _fail(
            "autopilot_content_version_mismatch",
            "场次绑定题库版本与当前题库不一致",
        )
    if (train_session.item_bank_definition_digest
            != binding.item_bank_definition_digest):
        _fail(
            "autopilot_content_digest_mismatch",
            "题库内容已变化，即使版本号未变也不能继续自动流程",
        )
    if (train_session.autopilot_protocol_version_id
            != binding.autopilot_protocol_version_id):
        _fail(
            "autopilot_protocol_version_mismatch",
            "场次绑定的自动化协议版本与当前版本不一致",
        )
    if (train_session.autopilot_protocol_definition_digest
            != binding.autopilot_protocol_definition_digest):
        _fail(
            "autopilot_protocol_digest_mismatch",
            "自动化协议内容已变化，即使版本号未变也不能继续自动流程",
        )
    return binding


def _require_plan_session_binding(
    db: Session,
    train_session: TrainSession,
) -> None:
    if train_session.visit_plan_id is None:
        return
    plan = db.get(VisitPlan, train_session.visit_plan_id)
    if plan is None or (
        plan.patient_id != train_session.patient_id
        or plan.item_bank_version_id != train_session.item_bank_version_id
        or plan.item_bank_definition_digest
        != train_session.item_bank_definition_digest
        or plan.autopilot_protocol_version_id
        != train_session.autopilot_protocol_version_id
        or plan.autopilot_protocol_definition_digest
        != train_session.autopilot_protocol_definition_digest
    ):
        _fail(
            "autopilot_plan_session_binding_mismatch",
            "场次与其训练安排的题库/自动化协议定义绑定不一致",
        )


def _fill_frozen_target(value: object, target_word: str, code: str) -> str:
    """Expand only the protocol's closed target slot, never ASR/client text."""
    line = _required_text(value, code).replace("【物品名】", target_word)
    if "【" in line or "】" in line:
        _fail("autopilot_protocol_invalid", f"P0a {code} 含未解析话术槽位")
    return line


def _load_live_payload(row: LiveState | None) -> dict:
    if row is None or not row.session_json:
        _fail("autopilot_live_session_missing", "当前没有完整的实时场次握手")
    try:
        payload = json.loads(row.session_json)
    except (TypeError, ValueError) as exc:
        raise AutopilotServiceError(
            "autopilot_live_session_invalid", "实时场次握手无法解析") from exc
    if not isinstance(payload, dict):
        _fail("autopilot_live_session_invalid", "实时场次握手格式非法")
    return payload


def _require_live_binding(
    db: Session,
    train_session: TrainSession,
) -> None:
    payload = _load_live_payload(db.get(LiveState, 1))
    live_session_id = payload.get("sessionId") or payload.get("session_id")
    expected_event = _enum_value(train_session.event_line)
    checks = {
        "sessionId": (live_session_id, train_session.session_id),
        "weekNo": (payload.get("weekNo"), train_session.week_no),
        "eventLine": (payload.get("eventLine"), expected_event),
        "itemBankVersionId": (
            payload.get("itemBankVersionId"), train_session.item_bank_version_id),
        "mode": (payload.get("mode"), "task"),
    }
    if isinstance(payload.get("weekNo"), bool):
        _fail("autopilot_live_session_mismatch", "实时场次周次格式非法")
    mismatched = [name for name, pair in checks.items() if pair[0] != pair[1]]
    if mismatched:
        _fail(
            "autopilot_live_session_mismatch",
            "实时场次握手与持久化场次不一致: " + ",".join(mismatched),
        )


def _require_active_device(
    db: Session,
    session_id: str,
    *,
    now: datetime,
    expected_token_hash: str | None = None,
) -> PatientDeviceCapability:
    rows = list(db.exec(select(PatientDeviceCapability).where(
        PatientDeviceCapability.session_id == session_id,
        PatientDeviceCapability.active_session_key == session_id,
        PatientDeviceCapability.revoked_at.is_(None),
    )))
    if len(rows) != 1:
        _fail("autopilot_device_not_paired", "当前场次没有唯一活跃的受试者设备")
    row = rows[0]
    if expected_token_hash is not None and row.token_hash != expected_token_hash:
        _fail("autopilot_device_not_paired", "设备能力未绑定当前场次")
    if (row.created_at > now or row.recovery_only_at is not None
            or row.expires_at <= now):
        _fail("autopilot_device_not_active", "受试者设备能力已失效或仅可恢复")
    return row


def _select_p0a_content(
    train_session: TrainSession,
    bank: content.ItemBank,
    protocol: dict,
    *,
    item_id: str | None = None,
    turn_seq: int | None = None,
) -> _P0aContent:
    binding = _require_session_definition_binding(train_session, bank, protocol)
    protocol_weeks = tuple(protocol["supported_training_weeks"])
    if (train_session.week_no != 2
            or train_session.week_no not in bank.supported_training_weeks
            or train_session.week_no not in protocol_weeks):
        _fail(
            "autopilot_scope_unsupported",
            "P0a 只允许题库与自动化协议共同明确支持的第 2 周模拟场次",
        )
    event_line = _enum_value(train_session.event_line)
    try:
        plan = runtime.build_session_plan(bank, train_session.week_no, event_line)
    except ValueError as exc:
        raise AutopilotServiceError(
            "autopilot_content_unavailable", "当前冻结训练计划不可用") from exc
    positions = autopilot_positions.plan_positions(plan)
    if not positions:
        _fail("autopilot_content_incomplete", "自动驾驶冻结计划没有可执行位置")
    if item_id is None and turn_seq is None:
        position = positions[0]
    elif isinstance(item_id, str) and isinstance(turn_seq, int) \
            and not isinstance(turn_seq, bool):
        try:
            position = autopilot_positions.find_position(
                positions, item_id=item_id, turn_seq=turn_seq)
        except ValueError as exc:
            raise AutopilotServiceError(
                "autopilot_position_invalid", "当前命令不属于冻结计划位置") from exc
    else:
        _fail("autopilot_position_invalid", "自动驾驶位置必须同时绑定题号与环节")
    gap = autopilot_positions.readiness_gap(bank, position)
    if gap is not None:
        _fail(gap.code, gap.detail)
    if position.task_type != "单要素" or position.response_role != "命名":
        _fail(
            "operational_protocol_unavailable",
            f"{position.position_key}:{position.response_role} 缺冻结自动协议",
        )

    rows = [row for row in bank.single_element
            if row.get("item_id") == position.item_id]
    if len(rows) != 1:
        _fail("autopilot_content_incomplete", "训练计划与题库位置不一致")
    raw = rows[0]
    item_id = _required_text(raw.get("item_id"), "item_id")
    target_word = _required_text(raw.get("target_word"), "target_word")
    image_id = _required_text(raw.get("image_id"), "image_id")
    initial_prompt = _required_text(raw.get("initial_prompt"), "initial_prompt")
    success_line = _required_text(raw.get("success_line"), "success_line")
    tell_answer = _required_text(raw.get("tell_answer"), "tell_answer")
    cues = raw.get("cues")
    if not isinstance(cues, dict):
        _fail("autopilot_content_incomplete", "P0a 当前题缺少 cues")
    cue1_row = cues.get("1")
    cue2_row = cues.get("2")
    if not isinstance(cue1_row, dict):
        _fail("autopilot_content_incomplete", "P0a 当前题缺少 cue1")
    variants = cue1_row.get("variants")
    response_paths = ("unknown", "close", "silence")
    if not isinstance(variants, dict) or set(variants) != set(response_paths):
        _fail(
            "autopilot_content_incomplete",
            "P0a 当前题 cue1 必须精确包含 unknown/close/silence 三条源分支",
        )
    cue1_lines: dict[str, str] = {}
    source_paragraphs: list[int] = []
    for response_path in response_paths:
        variant = variants.get(response_path)
        if not isinstance(variant, dict):
            _fail(
                "autopilot_content_incomplete",
                f"P0a 当前题 cue1.{response_path} 源分支格式非法",
            )
        cue1_lines[response_path] = _required_text(
            variant.get("text"), f"cue1.{response_path}.text")
        source_index = variant.get("source_paragraph_index")
        if (not isinstance(source_index, int) or isinstance(source_index, bool)
                or source_index < 0):
            _fail(
                "autopilot_content_incomplete",
                f"P0a 当前题 cue1.{response_path} 缺合法源段落定位",
            )
        source_paragraphs.append(source_index)
    if len(set(source_paragraphs)) != len(response_paths):
        _fail("autopilot_content_incomplete", "P0a cue1 三分支不能指向同一源段落")
    legacy_cue1 = _required_text(cue1_row.get("text"), "cue1.text")
    if legacy_cue1 != cue1_lines["unknown"]:
        _fail("autopilot_content_incomplete", "P0a cue1.text 与 unknown 源分支不一致")
    cue2 = _required_text(
        cue2_row.get("text") if isinstance(cue2_row, dict) else None,
        "cue2",
    )
    naming = protocol.get("naming")
    naming = naming if isinstance(naming, dict) else {}
    success_after_cue1 = naming.get("success_after_cue1")
    success_after_cue1 = (
        success_after_cue1 if isinstance(success_after_cue1, dict) else {})
    cue1_feedback_lines = {
        response_path: _fill_frozen_target(
            success_after_cue1.get(response_path),
            target_word,
            f"success_after_cue1.{response_path}",
        )
        for response_path in response_paths
    }
    success_after_cue2 = _fill_frozen_target(
        naming.get("success_after_cue2"), target_word, "success_after_cue2")
    allowed = content.tts_allowlist(bank, autopilot_protocol=protocol)
    required_lines = {
        initial_prompt,
        *cue1_lines.values(),
        cue2,
        tell_answer,
        success_line,
        *cue1_feedback_lines.values(),
        success_after_cue2,
    }
    if not required_lines.issubset(allowed):
        _fail("autopilot_tts_not_allowlisted", "P0a 当前题话术未全部进入 TTS 白名单")
    silence_seconds = protocol.get("silence_seconds")
    if not isinstance(silence_seconds, int) or isinstance(silence_seconds, bool):
        _fail("autopilot_protocol_invalid", "P0a 协议沉默阈值格式非法")
    return _P0aContent(
        item_bank_version_id=binding.item_bank_version_id,
        item_bank_definition_digest=binding.item_bank_definition_digest,
        autopilot_protocol_version_id=binding.autopilot_protocol_version_id,
        autopilot_protocol_definition_digest=(
            binding.autopilot_protocol_definition_digest),
        item_index=position.item_index,
        item_id=item_id,
        turn_seq=position.turn_seq,
        response_role=position.response_role,
        target_word=target_word,
        image_id=image_id,
        initial_prompt=initial_prompt,
        cue1_unknown=cue1_lines["unknown"],
        cue1_close=cue1_lines["close"],
        cue1_silence=cue1_lines["silence"],
        cue2=cue2,
        tell_answer=tell_answer,
        success_line=success_line,
        success_after_cue1_unknown=cue1_feedback_lines["unknown"],
        success_after_cue1_close=cue1_feedback_lines["close"],
        success_after_cue1_silence=cue1_feedback_lines["silence"],
        success_after_cue2=success_after_cue2,
        allowed_tts_lines=allowed,
        max_duration_seconds=silence_seconds + 5,
    )


def _require_entire_plan_supported(
    train_session: TrainSession,
    bank: content.ItemBank,
    protocol: dict,
) -> None:
    """Refuse ownership unless every frozen position is executable now.

    Admission is deliberately stronger than sequential routing.  A researcher
    must not be able to start a nominally automatic visit which is already known
    to need a human at position 14.  We still keep the per-position checks during
    routing as a defence against corrupted or drifted state.
    """
    event_line = _enum_value(train_session.event_line)
    try:
        positions = autopilot_positions.build_positions(
            bank,
            week_no=train_session.week_no,
            event_line=event_line,
        )
    except ValueError as exc:
        raise AutopilotServiceError(
            "autopilot_content_unavailable",
            "当前冻结训练计划不可用",
        ) from exc
    if not positions:
        _fail("autopilot_content_incomplete", "自动驾驶冻结计划没有可执行位置")

    gaps = tuple(
        gap
        for position in positions
        if (gap := autopilot_positions.readiness_gap(bank, position)) is not None
    )
    source_count = bank.meta.get("source_protocol_position_count")
    raw_unstructured = bank.meta.get("source_unstructured_positions")
    inventory_valid = (
        isinstance(source_count, int)
        and not isinstance(source_count, bool)
        and source_count >= len(positions)
        and isinstance(raw_unstructured, list)
    )
    source_unstructured: tuple[dict, ...] = ()
    if inventory_valid:
        rows: list[dict] = []
        seen_source_keys: set[str] = set()
        for raw in raw_unstructured:
            if not isinstance(raw, dict):
                inventory_valid = False
                break
            source_key = raw.get("source_position_key")
            role = raw.get("response_role")
            paragraphs = raw.get("source_paragraphs")
            status = raw.get("status")
            if (not isinstance(source_key, str) or not source_key.strip()
                    or source_key in seen_source_keys
                    or not isinstance(role, str) or not role.strip()
                    or not isinstance(paragraphs, list) or not paragraphs
                    or any(
                        not isinstance(value, int) or isinstance(value, bool)
                        or value < 0 for value in paragraphs)
                    or len(set(paragraphs)) != len(paragraphs)
                    or status not in {
                        "awaiting_content_decision", "awaiting_pi_rubric",
                    }):
                inventory_valid = False
                break
            seen_source_keys.add(source_key)
            rows.append(raw)
        source_unstructured = tuple(rows)
        if source_count != len(positions) + len(source_unstructured):
            inventory_valid = False
    if not inventory_valid:
        _fail(
            "autopilot_source_protocol_inventory_invalid",
            "源协议位置清单缺失或与结构化计划不一致，禁止接管",
        )

    unsupported_count = len(gaps) + len(source_unstructured)
    if unsupported_count:
        if gaps:
            first = gaps[0]
            first_gap = {
                "code": first.code,
                "item_id": first.position.item_id,
                "turn_seq": first.position.turn_seq,
                "response_role": first.position.response_role,
            }
            first_detail = (
                f"{first.position.position_key}:{first.position.response_role}")
        else:
            first_source = source_unstructured[0]
            first_gap = {
                "code": "source_protocol_position_unstructured",
                "source_position_key": first_source["source_position_key"],
                "response_role": first_source["response_role"],
            }
            first_detail = (
                f"{first_source['source_position_key']}:"
                f"{first_source['response_role']}")
        _fail(
            "autopilot_plan_not_fully_supported",
            f"完整源协议仍有 {unsupported_count} 个位置不受当前自动协议支持；"
            f"首个缺口为 {first_detail}",
            context={
                "unsupported_position_count": unsupported_count,
                "structured_unsupported_position_count": len(gaps),
                "source_unstructured_position_count": len(source_unstructured),
                "source_protocol_position_count": source_count,
                "first_gap": first_gap,
            },
        )

    # ``readiness_gap`` proves the position shape; selection additionally proves
    # the global protocol, TTS allowlist and exact row-level content contract.
    for position in positions:
        _select_p0a_content(
            train_session,
            bank,
            protocol,
            item_id=position.item_id,
            turn_seq=position.turn_seq,
        )


def _require_gate(
    db: Session,
    session_id: str,
    *,
    bank: content.ItemBank,
    protocol: dict,
    now: datetime,
    expected_token_hash: str | None = None,
    position_item_id: str | None = None,
    position_turn_seq: int | None = None,
    require_entire_plan_supported: bool = False,
) -> _P0aGate:
    if not p0a_feature_enabled():
        _fail(
            "autopilot_p0a_disabled",
            f"P0a 必须显式启用 {P0A_FEATURE_ENV} 和 {SIMULATION_DATA_ENV}",
        )
    train_session = db.get(TrainSession, session_id)
    if train_session is None:
        _fail("autopilot_session_unavailable", "场次不存在或不可用于 P0a")
    # Definition identity is the first semantic fence after locating the row.
    # A same-version in-place edit can never inherit an old session or command.
    _require_session_definition_binding(train_session, bank, protocol)
    _require_plan_session_binding(db, train_session)
    if train_session.is_simulation is not True:
        _fail("autopilot_simulation_required", "P0a 禁止用于真实研究场次")
    if train_session.data_classification != "simulation":
        _fail("autopilot_classification_invalid", "P0a 场次必须明确归类为 simulation")
    if (_enum_value(train_session.phase_type) != "正式训练"
            or _enum_value(train_session.event_line) != "正式训练"):
        _fail("autopilot_scope_unsupported", "P0a 只允许正式训练事件线")

    patient = db.get(Patient, train_session.patient_id)
    if patient is None or patient.is_simulation_subject is not True:
        _fail("autopilot_simulation_subject_required", "P0a 必须绑定专用模拟受试者")
    consent_status = (patient.consent_status or "").strip().casefold()
    if consent_status in _DENIED_CONSENT:
        _fail("autopilot_consent_denied", "受试者存在明确拒绝或撤回状态")
    if patient.recording_allowed is not True:
        _fail("autopilot_recording_not_allowed", "P0a 录音授权必须明确为 true")
    if (patient.withdrawal_status or "").strip():
        _fail("autopilot_subject_withdrawn", "已撤回受试者不能启动或继续 P0a")

    _require_live_binding(db, train_session)
    runtime_state = db.get(SessionRuntimeState, session_id)
    if runtime_state is None or runtime_state.status != "active":
        _fail("autopilot_runtime_inactive", "P0a 要求显式 active 的场次运行状态")
    terminal_facts = (
        runtime_state.intervention_completed_at,
        runtime_state.completed_at,
        runtime_state.aborted_at,
    )
    if any(value is not None for value in terminal_facts):
        _fail("autopilot_runtime_inactive", "场次运行状态含终态事实，禁止 P0a")
    active_capability = _require_active_device(
        db, session_id, now=now, expected_token_hash=expected_token_hash)
    if require_entire_plan_supported:
        _require_entire_plan_supported(train_session, bank, protocol)
    selected = _select_p0a_content(
        train_session,
        bank,
        protocol,
        item_id=position_item_id,
        turn_seq=position_turn_seq,
    )
    return _P0aGate(
        train_session=train_session,
        patient=patient,
        runtime_state=runtime_state,
        active_capability=active_capability,
        selected=selected,
    )


def _validate_idempotency_key(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDEMPOTENCY_KEY_RE.fullmatch(value) is None:
        _fail("autopilot_input_invalid", f"{label} 格式非法")
    return value


def _command_key() -> str:
    return "cmd-" + secrets.token_urlsafe(24)


def _raw_audio_id(db: Session) -> str:
    for _ in range(4):
        value = "raw-" + secrets.token_urlsafe(24)
        if db.get(AudioAssetRow, value) is None:
            return value
    _fail("autopilot_id_generation_failed", "无法分配唯一录音标识")


def _derived_key(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256(
        "\x00".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()
    return f"{prefix}-{digest}"


def _next_control_event_seq(db: Session, session_id: str) -> int:
    latest = db.exec(select(AutopilotControlEvent).where(
        AutopilotControlEvent.session_id == session_id,
    ).order_by(AutopilotControlEvent.event_seq.desc())).first()
    return (latest.event_seq if latest is not None else 0) + 1


def _command_by_key(
    db: Session,
    session_id: str,
    command_key: str,
) -> RuntimeCommand | None:
    return db.exec(select(RuntimeCommand).where(
        RuntimeCommand.session_id == session_id,
        RuntimeCommand.idempotency_key == command_key,
    )).first()


def _validate_command_identity(
    command: RuntimeCommand,
    selected: _P0aContent,
) -> None:
    binding_values = (
        command.item_bank_version_id,
        command.item_bank_definition_digest,
        command.autopilot_protocol_version_id,
        command.autopilot_protocol_definition_digest,
        command.response_role,
    )
    if any(value is None or not str(value).strip() for value in binding_values):
        _fail(
            "autopilot_command_binding_missing",
            "历史自动驾驶命令缺少完整定义与回答角色绑定",
        )
    if command.item_bank_version_id != selected.item_bank_version_id:
        _fail("autopilot_content_version_mismatch", "命令题库版本绑定已失效")
    if (command.item_bank_definition_digest
            != selected.item_bank_definition_digest):
        _fail("autopilot_content_digest_mismatch", "命令题库定义摘要已失效")
    if (command.autopilot_protocol_version_id
            != selected.autopilot_protocol_version_id):
        _fail("autopilot_protocol_version_mismatch", "命令自动化协议版本绑定已失效")
    if (command.autopilot_protocol_definition_digest
            != selected.autopilot_protocol_definition_digest):
        _fail("autopilot_protocol_digest_mismatch", "命令自动化协议定义摘要已失效")
    if command.response_role != selected.response_role:
        _fail("autopilot_command_response_role_mismatch", "命令回答角色绑定已失效")
    if (command.scope_key != P0A_SCOPE_KEY
            or command.item_id != selected.item_id
            or command.turn_seq != selected.turn_seq
            or command.turn_key != f"{selected.item_id}#{selected.turn_seq}"):
        _fail("autopilot_command_invalid", "当前命令不属于服务器冻结协议位置")


def _command_definition_fields(selected: _P0aContent) -> dict[str, str]:
    return {
        "item_bank_version_id": selected.item_bank_version_id,
        "item_bank_definition_digest": selected.item_bank_definition_digest,
        "autopilot_protocol_version_id": (
            selected.autopilot_protocol_version_id),
        "autopilot_protocol_definition_digest": (
            selected.autopilot_protocol_definition_digest),
        "response_role": selected.response_role,
    }


def _cue1_line(selected: _P0aContent, response_path: ResponsePath) -> str:
    return {
        "unknown": selected.cue1_unknown,
        "close": selected.cue1_close,
        "silence": selected.cue1_silence,
    }[response_path]


def _cue1_success_line(
    selected: _P0aContent,
    response_path: ResponsePath,
) -> str:
    return {
        "unknown": selected.success_after_cue1_unknown,
        "close": selected.success_after_cue1_close,
        "silence": selected.success_after_cue1_silence,
    }[response_path]


def _validate_tts_semantics(
    command: RuntimeCommand,
    payload: TtsCommandPayload,
    selected: _P0aContent,
) -> None:
    """Bind purpose to the exact frozen line and cue level.

    A generic allowlist membership check is insufficient: without this mapping an
    initial question could be relabelled as feedback and prematurely complete the
    automation scope.
    """
    if (payload.item_id != command.item_id
            or payload.turn_seq != command.turn_seq
            or payload.cue_level != command.prompt_level):
        _fail("autopilot_command_invalid", "TTS 命令与冻结环节不一致")
    expected_line: str
    if payload.purpose == "question":
        if command.prompt_level != 0 or command.attempt_seq != 1:
            _fail("autopilot_command_invalid", "question 只能是首次零级提问")
        expected_line = selected.initial_prompt
    elif payload.purpose == "cue":
        if command.prompt_level == 1 and command.attempt_seq == 2:
            if payload.response_path is None:  # contract postcondition
                _fail("autopilot_command_invalid", "一级 cue 缺少冻结 response_path")
            expected_line = _cue1_line(selected, payload.response_path)
        elif command.prompt_level == 2 and command.attempt_seq == 3:
            expected_line = selected.cue2
        else:
            _fail("autopilot_command_invalid", "cue 只能是一级或二级冻结提示")
    elif payload.purpose == "feedback":
        if command.prompt_level == 0 and command.attempt_seq == 1:
            expected_line = selected.success_line
        elif command.prompt_level == 1 and command.attempt_seq == 2:
            if payload.response_path is None:  # contract postcondition
                _fail("autopilot_command_invalid", "一级 feedback 缺少冻结 response_path")
            expected_line = _cue1_success_line(selected, payload.response_path)
        elif command.prompt_level == 2 and command.attempt_seq == 3:
            expected_line = selected.success_after_cue2
        else:
            _fail("autopilot_command_invalid", "feedback 提示等级非法")
    else:
        if command.prompt_level != 3 or command.attempt_seq != 3:
            _fail("autopilot_command_invalid", "tell_answer 必须是三级直接告知")
        expected_line = selected.tell_answer
    if payload.speech_text != expected_line:
        _fail("autopilot_command_invalid", "TTS purpose 与冻结话术不一致")
    if payload.speech_text not in selected.allowed_tts_lines:
        _fail("autopilot_command_invalid", "TTS 话术未进入冻结白名单")


def _require_same_issued_active_device(
    command: RuntimeCommand,
    active_capability: PatientDeviceCapability,
) -> None:
    """Never let a newly paired device record a prompt heard by another device."""
    if (command.issued_capability_token_hash != active_capability.token_hash
            or command.issued_device_id_hash != active_capability.device_id_hash):
        _fail(
            "autopilot_command_device_rotated",
            "命令发行后受试者设备已换绑，必须重新播放问题",
        )


def _project_command(
    db: Session,
    command: RuntimeCommand,
    selected: _P0aContent,
    *,
    expected_capability: PatientDeviceCapability | None = None,
) -> NextCommandProjection:
    _validate_command_identity(command, selected)
    if command.state not in {"pending", "started"}:
        _fail("autopilot_command_not_open", "当前命令已不是可执行状态")
    if command.control_generation < 1 or command.runner_generation < 1:
        _fail("autopilot_command_invalid", "当前命令 generation 非法")
    issued_capability = db.get(
        PatientDeviceCapability, command.issued_capability_token_hash)
    if (issued_capability is None
            or issued_capability.session_id != command.session_id
            or issued_capability.device_id_hash != command.issued_device_id_hash
            or command.issued_at < issued_capability.created_at
            or command.issued_at >= issued_capability.expires_at
            or (issued_capability.revoked_at is not None
                and command.issued_at >= issued_capability.revoked_at)
            or (issued_capability.recovery_only_at is not None
                and command.issued_at >= issued_capability.recovery_only_at)):
        _fail("autopilot_command_invalid", "当前命令缺少有效的发行设备绑定")
    if (expected_capability is not None
            and (command.issued_capability_token_hash != expected_capability.token_hash
                 or command.issued_device_id_hash != expected_capability.device_id_hash)):
        _fail("autopilot_command_device_mismatch", "当前命令不是发行给此活跃设备")
    common = dict(
        command_key=command.idempotency_key,
        command_seq=command.command_seq,
        kind=command.kind,
        state=command.state,
        command_revision=command.revision,
        control_generation=command.control_generation,
        runner_generation=command.runner_generation,
        item_ref=patient_presentation.item_ref(selected.item_index),
        turn_seq=command.turn_seq,
        attempt_seq=command.attempt_seq,
        prompt_level=command.prompt_level,
    )
    if command.kind == "tts":
        try:
            payload = TtsCommandPayload.model_validate_json(command.payload_json)
        except (TypeError, ValueError) as exc:
            raise AutopilotServiceError(
                "autopilot_command_invalid", "TTS 命令载荷不符合封闭契约") from exc
        _validate_tts_semantics(command, payload, selected)
        projected_payload = DeviceTtsPayload(
            speech_key=payload.speech_key,
            speech_text=payload.speech_text,
            purpose=payload.purpose,
            response_path=payload.response_path,
        )
    elif command.kind == "record":
        try:
            payload = RecordCommandPayload.model_validate_json(command.payload_json)
        except (TypeError, ValueError) as exc:
            raise AutopilotServiceError(
                "autopilot_command_invalid", "录音命令载荷不符合封闭契约") from exc
        if (payload.item_id != command.item_id
                or payload.turn_seq != command.turn_seq
                or payload.cue_level != command.prompt_level
                or payload.raw_audio_id != command.expected_raw_audio_id
                or payload.turn_key != command.turn_key):
            _fail("autopilot_command_invalid", "录音命令未绑定唯一预分配录音")
        asset = db.get(AudioAssetRow, payload.raw_audio_id)
        if (asset is None or asset.session_id != command.session_id
                or asset.turn_key != command.turn_key
                or asset.data_classification != "simulation"
                or asset.is_simulation is not True
                or asset.status != AudioStatus.recorded
                or asset.withdrawn or asset.delete_gate_passed):
            _fail("autopilot_command_invalid", "预分配录音资产不可用于当前命令")
        try:
            autopilot_ledger.verify_tts_ended_prerequisite(db, command)
        except autopilot_ledger.AutopilotProofError as exc:
            raise AutopilotServiceError(
                "autopilot_command_invalid", "录音命令缺少有效的 TTS 结束证据") from exc
        predecessor = db.get(RuntimeCommand, command.predecessor_command_id)
        if (predecessor is None
                or predecessor.kind != "tts"
                or predecessor.state != "succeeded"
                or predecessor.session_id != command.session_id
                or predecessor.item_id != command.item_id
                or predecessor.turn_seq != command.turn_seq
                or predecessor.turn_key != command.turn_key
                or predecessor.attempt_seq != command.attempt_seq
                or predecessor.prompt_level != command.prompt_level
                or predecessor.scope_key != command.scope_key
                or predecessor.control_generation != command.control_generation
                or predecessor.runner_generation != command.runner_generation
                or predecessor.issued_capability_token_hash
                != command.issued_capability_token_hash
                or predecessor.issued_device_id_hash
                != command.issued_device_id_hash):
            _fail(
                "autopilot_command_invalid",
                "录音命令的前置 TTS 身份与当前采集槽不一致",
            )
        try:
            predecessor_payload = TtsCommandPayload.model_validate_json(
                predecessor.payload_json)
        except (TypeError, ValueError) as exc:
            raise AutopilotServiceError(
                "autopilot_command_invalid",
                "录音命令的前置 TTS 载荷不符合封闭契约",
            ) from exc
        _validate_command_identity(predecessor, selected)
        _validate_tts_semantics(predecessor, predecessor_payload, selected)
        if predecessor_payload.purpose not in {"question", "cue"}:
            _fail(
                "autopilot_command_invalid",
                "只有问题或线索朗读可以产生后续录音命令",
            )
        projected_payload = DeviceRecordPayload(
            raw_audio_id=payload.raw_audio_id,
            turn_ref=patient_presentation.turn_ref(
                selected.item_index, payload.turn_seq),
            max_duration_seconds=payload.max_duration_seconds,
            contains_direct_identifier=payload.contains_direct_identifier,
            presentation_speech_key=predecessor_payload.speech_key,
            presentation_speech_text=predecessor_payload.speech_text,
            presentation_purpose=predecessor_payload.purpose,
        )
    else:
        _fail("autopilot_command_invalid", "未知自动驾驶命令类型")
    return NextCommandProjection(**common, payload=projected_payload)


def _projection_for_state(
    db: Session,
    state: SessionAutopilotState,
    selected: _P0aContent,
    *,
    expected_capability: PatientDeviceCapability | None = None,
) -> NextCommandProjection | None:
    if state.status in {
        "processing_attempt", "paused", "scope_completed", "failed", "idle",
    }:
        if state.status == "scope_completed" and state.current_command_id is not None:
            _fail("autopilot_state_invalid", "已完成 scope 仍引用当前命令")
        return None
    if state.status not in {"waiting_tts", "waiting_recording"}:
        _fail("autopilot_state_invalid", "当前自动驾驶状态不可投影命令")
    if state.current_command_id is None:
        _fail("autopilot_state_invalid", "等待状态缺少当前命令")
    command = db.get(RuntimeCommand, state.current_command_id)
    expected_kind = "tts" if state.status == "waiting_tts" else "record"
    if (command is None or command.session_id != state.session_id
            or command.scope_key != state.scope_key
            or command.kind != expected_kind
            or command.control_generation != state.control_generation
            or command.runner_generation != state.runner_generation):
        _fail("autopilot_state_invalid", "当前状态与命令 generation 不一致")
    return _project_command(
        db, command, selected, expected_capability=expected_capability)


def _default_bank() -> content.ItemBank:
    return content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")


def _default_protocol() -> dict:
    return content.load_autopilot_protocol(
        content.CONTENT_DIR / "autopilot_protocol_v1.json")


def start_p0a(
    db: Session,
    *,
    session_id: str,
    idempotency_key: str,
    expected_revision: int,
    actor_id: str,
    bank: content.ItemBank | None = None,
    protocol: dict | None = None,
    now: datetime | None = None,
) -> StartP0aResult:
    """Gate and atomically stage the first frozen protocol-position command.

    An exact retry returns the current command for the already-started scope and
    never creates a second command or control event.
    """
    idempotency_key = _validate_idempotency_key(idempotency_key, "idempotency_key")
    if (not isinstance(expected_revision, int) or isinstance(expected_revision, bool)
            or expected_revision < 0):
        _fail("autopilot_input_invalid", "expected_revision 必须是非负整数")
    actor_id = _required_text(actor_id, "actor_id")
    if len(actor_id) > 128:
        _fail("autopilot_input_invalid", "actor_id 过长")
    observed_at = _utc_naive(now) if now is not None else _utc_now_naive()
    resolved_bank = bank or _default_bank()
    resolved_protocol = protocol or _default_protocol()
    gate = _require_gate(
        db,
        session_id,
        bank=resolved_bank,
        protocol=resolved_protocol,
        now=observed_at,
        require_entire_plan_supported=True,
    )
    # This row is also the cross-process admission mutex used by the legacy
    # attempt endpoint.  In-memory locks alone are insufficient on PostgreSQL.
    locked_session = db.exec(select(TrainSession).where(
        TrainSession.session_id == session_id,
    ).with_for_update()).first()
    if locked_session is None:
        _fail("autopilot_session_unavailable", "场次不存在或不可用于 P0a")

    prior_event = db.exec(select(AutopilotControlEvent).where(
        AutopilotControlEvent.idempotency_key == idempotency_key,
    )).first()
    if prior_event is not None:
        if (prior_event.session_id != session_id or prior_event.event_type != "start"
                or prior_event.scope_key != P0A_SCOPE_KEY
                or prior_event.actor_type != "researcher"
                or prior_event.actor_id != actor_id
                or prior_event.payload_json != autopilot_ledger.encode_control_event_payload(
                    "start", {"source": P0A_SOURCE})):
            _fail("autopilot_idempotency_conflict", "启动幂等键已被其他事实使用")
        state = db.get(SessionAutopilotState, session_id)
        if state is None or state.scope_key != P0A_SCOPE_KEY:
            _fail("autopilot_state_invalid", "启动事件缺少对应自动驾驶状态")
        replay_gate = gate
        if state.current_command_id is not None:
            replay_command = db.get(RuntimeCommand, state.current_command_id)
            if (replay_command is None
                    or replay_command.session_id != session_id
                    or replay_command.scope_key != P0A_SCOPE_KEY):
                _fail("autopilot_state_invalid", "启动回放缺少一致的当前命令")
            replay_gate = _require_gate(
                db,
                session_id,
                bank=resolved_bank,
                protocol=resolved_protocol,
                now=observed_at,
                position_item_id=replay_command.item_id,
                position_turn_seq=replay_command.turn_seq,
            )
        return StartP0aResult(
            status=state.status,
            state_revision=state.revision,
            replayed=True,
            command=_projection_for_state(
                db, state, replay_gate.selected,
                expected_capability=replay_gate.active_capability),
        )

    # P0a is a fresh-scope handoff, never an overlay on legacy evidence. This
    # also makes a manual-claim-first race deterministically reject start.
    existing_attempt = db.exec(select(AttemptEvent).where(
        AttemptEvent.session_id == session_id,
    )).first()
    existing_interaction = db.exec(select(InteractionEvent).where(
        InteractionEvent.session_id == session_id,
    )).first()
    if existing_attempt is not None or existing_interaction is not None:
        _fail(
            "autopilot_existing_manual_evidence",
            "场次已有人工逐次或交互证据，禁止叠加 P0a 控制面",
        )

    state = db.exec(select(SessionAutopilotState).where(
        SessionAutopilotState.session_id == session_id,
    ).with_for_update()).first()
    current_revision = state.revision if state is not None else 0
    if current_revision != expected_revision:
        _fail("autopilot_revision_conflict", "自动驾驶状态 revision 已变化")
    if state is not None and not (
        state.scope_key == "disabled"
        and state.mode == "disabled"
        and state.status == "idle"
        and state.current_command_id is None
        and state.lease_owner is None
    ):
        _fail("autopilot_already_active", "场次已有自动驾驶状态，不能重复启动")
    existing_command = db.exec(select(RuntimeCommand).where(
        RuntimeCommand.session_id == session_id,
    )).first()
    if existing_command is not None:
        _fail("autopilot_state_invalid", "禁用状态下存在孤立自动驾驶命令")

    control_generation = max((state.control_generation if state else 0) + 1, 1)
    runner_generation = max((state.runner_generation if state else 0) + 1, 1)
    command_seq = state.next_command_seq if state is not None else 1
    tts_payload = TtsCommandPayload(
        speech_key=f"p0a.question.{command_seq}",
        speech_text=gate.selected.initial_prompt,
        purpose="question",
        item_id=gate.selected.item_id,
        turn_seq=gate.selected.turn_seq,
        cue_level=0,
    )
    command = RuntimeCommand(
        idempotency_key=_command_key(),
        session_id=session_id,
        command_seq=command_seq,
        item_id=gate.selected.item_id,
        turn_seq=gate.selected.turn_seq,
        turn_key=f"{gate.selected.item_id}#{gate.selected.turn_seq}",
        attempt_seq=1,
        prompt_level=0,
        **_command_definition_fields(gate.selected),
        scope_key=P0A_SCOPE_KEY,
        control_generation=control_generation,
        runner_generation=runner_generation,
        issued_capability_token_hash=gate.active_capability.token_hash,
        issued_device_id_hash=gate.active_capability.device_id_hash,
        issued_at=observed_at,
        kind="tts",
        state="pending",
        payload_json=tts_payload.model_dump_json(exclude_none=True),
        created_at=observed_at,
        updated_at=observed_at,
    )
    db.add(command)
    db.flush()
    if command.id is None:  # pragma: no cover - SQLAlchemy invariant
        _fail("autopilot_state_invalid", "首条自动驾驶命令未能持久化")

    from_mode = state.mode if state is not None else "disabled"
    from_status = state.status if state is not None else "idle"
    if state is None:
        state = SessionAutopilotState(
            session_id=session_id,
            created_at=observed_at,
        )
    state.scope_key = P0A_SCOPE_KEY
    state.mode = "autonomous"
    state.status = "waiting_tts"
    state.control_generation = control_generation
    state.runner_generation = runner_generation
    state.revision = current_revision + 1
    state.current_command_id = command.id
    state.next_command_seq = command_seq + 1
    state.last_error_code = None
    state.lease_owner = None
    state.lease_acquired_at = None
    state.lease_expires_at = None
    state.updated_at = observed_at
    db.add(state)
    db.add(AutopilotControlEvent(
        idempotency_key=idempotency_key,
        session_id=session_id,
        event_seq=_next_control_event_seq(db, session_id),
        event_type="start",
        scope_key=P0A_SCOPE_KEY,
        control_generation=control_generation,
        runner_generation=runner_generation,
        command_id=command.id,
        actor_type="researcher",
        actor_id=actor_id,
        from_mode=from_mode,
        to_mode="autonomous",
        from_status=from_status,
        to_status="waiting_tts",
        payload_json=autopilot_ledger.encode_control_event_payload(
            "start", {"source": P0A_SOURCE}),
        created_at=observed_at,
    ))
    db.flush()
    return StartP0aResult(
        status=state.status,
        state_revision=state.revision,
        replayed=False,
        command=_project_command(
            db, command, gate.selected,
            expected_capability=gate.active_capability),
    )


def get_next_command(
    db: Session,
    *,
    session_id: str,
    capability_token_hash: str,
    bank: content.ItemBank | None = None,
    protocol: dict | None = None,
    now: datetime | None = None,
) -> NextCommandProjection | None:
    """Return only the current open command for the exact active device/session."""
    observed_at = _utc_naive(now) if now is not None else _utc_now_naive()
    state = db.get(SessionAutopilotState, session_id)
    if state is None or state.scope_key != P0A_SCOPE_KEY or state.mode != "autonomous":
        _fail("autopilot_not_active", "当前场次没有运行中的 P0a")
    current = (db.get(RuntimeCommand, state.current_command_id)
               if state.current_command_id is not None else None)
    gate = _require_gate(
        db,
        session_id,
        bank=bank or _default_bank(),
        protocol=protocol or _default_protocol(),
        now=observed_at,
        expected_token_hash=capability_token_hash,
        position_item_id=current.item_id if current is not None else None,
        position_turn_seq=current.turn_seq if current is not None else None,
    )
    return _projection_for_state(
        db, state, gate.selected,
        expected_capability=gate.active_capability)


def _authorize_pending_device_command(
    db: Session,
    *,
    session_id: str,
    command_key: str,
    capability_token_hash: str,
    expected_kind: Literal["tts", "record"],
    bank: content.ItemBank | None = None,
    protocol: dict | None = None,
    now: datetime | None = None,
) -> tuple[_P0aGate, NextCommandProjection]:
    """Prove one current command before a high-impact patient-side action."""
    _validate_idempotency_key(command_key, "command_key")
    observed_at = _utc_naive(now) if now is not None else _utc_now_naive()
    command = _command_by_key(db, session_id, command_key)
    gate = _require_gate(
        db,
        session_id,
        bank=bank or _default_bank(),
        protocol=protocol or _default_protocol(),
        now=observed_at,
        expected_token_hash=capability_token_hash,
        position_item_id=command.item_id if command is not None else None,
        position_turn_seq=command.turn_seq if command is not None else None,
    )
    state = db.exec(select(SessionAutopilotState).where(
        SessionAutopilotState.session_id == session_id,
    ).with_for_update()).first()
    expected_status = "waiting_tts" if expected_kind == "tts" else "waiting_recording"
    if (state is None
            or state.scope_key != P0A_SCOPE_KEY
            or state.mode != "autonomous"
            or state.status != expected_status
            or command is None
            or command.id is None
            or state.current_command_id != command.id
            or command.kind != expected_kind
            or command.state != "pending"
            or command.scope_key != state.scope_key
            or command.control_generation != state.control_generation
            or command.runner_generation != state.runner_generation):
        _fail(
            "autopilot_command_not_current",
            "当前自动驾驶命令不允许执行该设备动作",
        )
    projection = _project_command(
        db,
        command,
        gate.selected,
        expected_capability=gate.active_capability,
    )
    if projection.kind != expected_kind or projection.state != "pending":
        _fail("autopilot_command_not_current", "自动驾驶命令已不再等待执行")
    return gate, projection


def authorize_recording_command(
    db: Session,
    *,
    session_id: str,
    command_key: str,
    capability_token_hash: str,
    bank: content.ItemBank | None = None,
    protocol: dict | None = None,
    now: datetime | None = None,
) -> bool:
    """Authorize microphone opening only for the exact pending record command."""
    gate, projection = _authorize_pending_device_command(
        db,
        session_id=session_id,
        command_key=command_key,
        capability_token_hash=capability_token_hash,
        expected_kind="record",
        bank=bank,
        protocol=protocol,
        now=now,
    )
    if not isinstance(projection.payload, DeviceRecordPayload):
        _fail("autopilot_command_invalid", "录音命令载荷非法")
    return gate.train_session.is_simulation


def authorized_tts_text(
    db: Session,
    *,
    session_id: str,
    command_key: str,
    capability_token_hash: str,
    bank: content.ItemBank | None = None,
    protocol: dict | None = None,
    now: datetime | None = None,
) -> str:
    """Derive the exact pending TTS command text; the device never submits text."""
    _gate, projection = _authorize_pending_device_command(
        db,
        session_id=session_id,
        command_key=command_key,
        capability_token_hash=capability_token_hash,
        expected_kind="tts",
        bank=bank,
        protocol=protocol,
        now=now,
    )
    if not isinstance(projection.payload, DeviceTtsPayload):
        _fail("autopilot_command_invalid", "TTS 命令载荷非法")
    return projection.payload.speech_text


def pause_autonomous_scope_for_researcher(
    db: Session,
    *,
    session_id: str,
    actor_id: str | None,
    reason_code: str = "researcher_requested_pause",
    source: Literal[
        "account_pause_endpoint", "atomic_technical_pause",
    ] = "account_pause_endpoint",
    now: datetime | None = None,
) -> bool:
    """Atomically fence an owned P0a scope before the runtime is paused.

    The HTTP adapter commits this mutation together with ``SessionRuntimeState``
    and the LiveState stop projection.  Clearing ``current_command_id`` makes any
    delayed, previously unseen device ACK fail the existing current-command
    fence; an exact ACK replay can still be acknowledged as the immutable fact it
    already is.  Terminal scope facts are deliberately preserved: runtime pause
    remains valid after scope completion/failure, but must not erase that terminal
    outcome.
    """
    observed_at = _utc_naive(now) if now is not None else _utc_now_naive()
    if actor_id is not None and len(actor_id) > 128:
        _fail("autopilot_input_invalid", "actor_id 过长")
    if (re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", reason_code) is None
            or source not in {
                "account_pause_endpoint", "atomic_technical_pause",
            }):
        _fail("autopilot_input_invalid", "研究者暂停原因或来源非法")
    state = db.exec(select(SessionAutopilotState).where(
        SessionAutopilotState.session_id == session_id,
    ).with_for_update()).first()
    if state is None or state.mode != "autonomous":
        return False
    if state.scope_key != P0A_SCOPE_KEY:
        _fail("autopilot_state_invalid", "自动驾驶 scope 非法")
    if state.status in {"paused", "scope_completed", "failed"}:
        return False

    from_status = state.status
    command_id = state.current_command_id
    previous_revision = state.revision
    # Provider calls cannot be cancelled reliably once issued.  Fence every
    # recoverable attempt claim in this same control transaction so a response
    # that arrives after pause/takeover cannot persist ASR/judgement facts or
    # pause a later manual runtime.
    evidence_ledger.invalidate_processing_claims(
        db,
        session_id=session_id,
    )
    state.status = "paused"
    state.current_command_id = None
    state.revision += 1
    state.lease_owner = None
    state.lease_acquired_at = None
    state.lease_expires_at = None
    state.updated_at = observed_at
    db.add(state)
    db.add(AutopilotControlEvent(
        idempotency_key=_derived_key(
            "researcher-pause", session_id, state.control_generation,
            state.runner_generation, previous_revision),
        session_id=session_id,
        event_seq=_next_control_event_seq(db, session_id),
        event_type="pause",
        scope_key=P0A_SCOPE_KEY,
        control_generation=state.control_generation,
        runner_generation=state.runner_generation,
        command_id=command_id,
        actor_type="researcher",
        actor_id=actor_id,
        reason_code=reason_code,
        from_mode="autonomous",
        to_mode="autonomous",
        from_status=from_status,
        to_status="paused",
        payload_json=autopilot_ledger.encode_control_event_payload(
            "pause", {
                "reason_code": reason_code,
                "source": source,
            }),
        created_at=observed_at,
    ))
    db.flush()
    db.expire(state)
    return True


def fence_autonomous_scope_for_external_stop(
    db: Session,
    *,
    session_id: str,
    reason_code: str,
    source: Literal[
        "patient_rec_failure",
        "session_abort",
        "cloud_processing_consent_revoked",
        "subject_withdrawal",
    ],
    actor_type: Literal["system", "researcher", "device"],
    actor_id: str | None = None,
    capability_token_hash: str | None = None,
    expected_item_id: str | None = None,
    expected_turn_seq: int | None = None,
    idempotency_token: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Fence an autonomous control plane in the external stop transaction.

    Runtime abort, cloud-consent revocation and the generic ``patientRec`` device
    failure path do not use the command-ACK endpoint, but they still must revoke
    the current server command and any in-flight provider claim atomically.  This
    helper only stages the autopilot half and never commits.  The caller must stage
    its runtime/LiveState stop in the same database transaction.

    A device failure is recorded as a device fact, never as a researcher action.
    Because this path lacks the command revision/generation tuple of a dedicated
    ACK, later manual takeover still requires the existing exact device drain.
    """
    if source not in _EXTERNAL_FENCE_SOURCES:  # defensive even for Literal callers
        _fail("autopilot_input_invalid", "外部停止 source 未进入封闭合集")
    if actor_type != _EXTERNAL_FENCE_ACTOR[source]:
        _fail("autopilot_input_invalid", "外部停止 source 与 actor 类型不一致")
    if (not isinstance(reason_code, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,95}", reason_code) is None):
        _fail("autopilot_input_invalid", "外部停止 reason_code 格式非法")
    if actor_id is not None and (
            not isinstance(actor_id, str) or not actor_id.strip()
            or len(actor_id) > 128):
        _fail("autopilot_input_invalid", "外部停止 actor_id 格式非法")
    if actor_type == "device":
        if not capability_token_hash:
            _fail("autopilot_input_invalid", "设备停止缺少当前能力摘要")
        if expected_item_id is None or idempotency_token is None:
            _fail("autopilot_input_invalid", "设备停止缺少当前协议位置或失败幂等标识")
    elif capability_token_hash is not None:
        _fail("autopilot_input_invalid", "非设备停止不得携带设备能力")
    if source in {"session_abort", "subject_withdrawal"} and actor_id is None:
        _fail("autopilot_input_invalid", "中止场次或研究撤回必须绑定具名研究者")
    if source == "cloud_processing_consent_revoked" and actor_id is not None:
        _fail("autopilot_input_invalid", "云处理撤回必须记为系统治理事实")
    if source != "patient_rec_failure" and (
            expected_item_id is not None or expected_turn_seq is not None
            or idempotency_token is not None):
        _fail("autopilot_input_invalid", "非设备停止不得携带设备位置事实")
    if (expected_item_id is None) != (expected_turn_seq is None):
        _fail("autopilot_input_invalid", "外部停止位置必须同时绑定题号与环节")
    if expected_turn_seq is not None and (
            not isinstance(expected_turn_seq, int)
            or isinstance(expected_turn_seq, bool) or expected_turn_seq < 1):
        _fail("autopilot_input_invalid", "外部停止 turn_seq 格式非法")
    if idempotency_token is not None and (
            not isinstance(idempotency_token, str)
            or len(idempotency_token) < 8 or len(idempotency_token) > 128):
        _fail("autopilot_input_invalid", "外部停止幂等标识格式非法")

    observed_at = _utc_naive(now) if now is not None else _utc_now_naive()
    state = db.exec(select(SessionAutopilotState).where(
        SessionAutopilotState.session_id == session_id,
    ).with_for_update()).first()
    if state is None or state.mode != "autonomous":
        return False
    if state.scope_key != P0A_SCOPE_KEY:
        _fail("autopilot_state_invalid", "自动驾驶 scope 非法")
    if state.status in {"paused", "scope_completed", "failed"}:
        return False
    if state.current_command_id is None:
        _fail("autopilot_state_invalid", "外部停止时自动驾驶缺当前命令")
    command = db.exec(select(RuntimeCommand).where(
        RuntimeCommand.id == state.current_command_id,
        RuntimeCommand.session_id == session_id,
    ).with_for_update()).first()
    if (command is None or not _command_matches_control_state(command, state)
            or command.state not in {"pending", "started", "succeeded"}):
        _fail("autopilot_state_invalid", "外部停止的当前命令与控制 generation 不一致")
    if (expected_item_id is not None
            and (command.item_id != expected_item_id
                 or command.turn_seq != expected_turn_seq)):
        _fail("autopilot_external_stop_position_mismatch", "设备失败与当前自动驾驶位置不一致")

    if actor_type == "device":
        capability = _require_active_device(
            db,
            session_id,
            now=observed_at,
            expected_token_hash=capability_token_hash,
        )
        _require_same_issued_active_device(command, capability)
        if actor_id is not None and actor_id != capability.device_id_hash:
            _fail("autopilot_external_stop_device_mismatch", "设备失败 actor 与能力凭据不一致")
        actor_id = capability.device_id_hash

    previous_revision = state.revision
    previous_status = state.status
    evidence_ledger.invalidate_processing_claims(db, session_id=session_id)
    result = db.execute(
        update(SessionAutopilotState)
        .where(
            SessionAutopilotState.session_id == session_id,
            SessionAutopilotState.scope_key == P0A_SCOPE_KEY,
            SessionAutopilotState.mode == "autonomous",
            SessionAutopilotState.status == previous_status,
            SessionAutopilotState.current_command_id == command.id,
            SessionAutopilotState.control_generation == state.control_generation,
            SessionAutopilotState.runner_generation == state.runner_generation,
            SessionAutopilotState.revision == previous_revision,
        )
        .values(
            status="paused",
            current_command_id=None,
            revision=SessionAutopilotState.revision + 1,
            last_error_code=reason_code,
            lease_owner=None,
            lease_acquired_at=None,
            lease_expires_at=None,
            updated_at=observed_at,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        _fail("autopilot_external_stop_cas_conflict", "外部停止的自动驾驶 CAS 已失效")
    event_token = idempotency_token or str(previous_revision)
    db.add(AutopilotControlEvent(
        idempotency_key=_derived_key(
            "external-stop", session_id, source, reason_code, event_token),
        session_id=session_id,
        event_seq=_next_control_event_seq(db, session_id),
        event_type="pause",
        scope_key=P0A_SCOPE_KEY,
        control_generation=state.control_generation,
        runner_generation=state.runner_generation,
        command_id=command.id,
        actor_type=actor_type,
        actor_id=actor_id,
        reason_code=reason_code,
        from_mode="autonomous",
        to_mode="autonomous",
        from_status=previous_status,
        to_status="paused",
        payload_json=autopilot_ledger.encode_control_event_payload(
            "pause", {"reason_code": reason_code, "source": source}),
        created_at=observed_at,
    ))
    db.flush()
    db.expire(state)
    return True


def _latest_control_event(
    db: Session,
    session_id: str,
) -> AutopilotControlEvent | None:
    return db.exec(select(AutopilotControlEvent).where(
        AutopilotControlEvent.session_id == session_id,
    ).order_by(AutopilotControlEvent.event_seq.desc())).first()


def _control_event_matches_state(
    event: AutopilotControlEvent,
    state: SessionAutopilotState,
) -> bool:
    return (
        event.session_id == state.session_id
        and event.scope_key == state.scope_key == P0A_SCOPE_KEY
        and event.control_generation == state.control_generation
        and event.runner_generation == state.runner_generation
    )


def _command_matches_control_state(
    command: RuntimeCommand,
    state: SessionAutopilotState,
) -> bool:
    return (
        command.id is not None
        and command.session_id == state.session_id
        and command.scope_key == state.scope_key == P0A_SCOPE_KEY
        and command.control_generation == state.control_generation
        and command.runner_generation == state.runner_generation
    )


def _require_issued_drain_device(
    db: Session,
    *,
    state: SessionAutopilotState,
    command: RuntimeCommand,
    capability_token_hash: str,
) -> PatientDeviceCapability:
    capability = db.get(PatientDeviceCapability, capability_token_hash)
    if (capability is None
            or capability.session_id != state.session_id
            or capability.active_session_key != state.session_id
            or capability.revoked_at is not None
            or command.issued_capability_token_hash != capability.token_hash
            or command.issued_device_id_hash != capability.device_id_hash):
        _fail(
            "autopilot_drain_device_mismatch",
            "收麦回执不属于该命令的当前发行设备",
        )
    return capability


def _device_failure_proves_media_terminal(
    db: Session,
    *,
    event: AutopilotControlEvent,
    command: RuntimeCommand,
    state: SessionAutopilotState,
) -> bool:
    if (not _control_event_matches_state(event, state)
            or event.event_type != "failure"
            or event.actor_type != "device"
            or event.command_id != command.id
            or event.from_mode != "autonomous"
            or event.to_mode != "autonomous"
            or event.to_status != state.status
            or command.state != "failed"
            or command.failed_at is None
            or not event.reason_code):
        return False
    expected_ack_type = "tts_failed" if command.kind == "tts" else \
        "record_failed" if command.kind == "record" else None
    if expected_ack_type is None:
        return False
    ack = db.exec(select(RuntimeCommandAck).where(
        RuntimeCommandAck.command_id == command.id,
        RuntimeCommandAck.ack_type == expected_ack_type,
    )).first()
    if (ack is None
            or ack.session_id != command.session_id
            or ack.control_generation != command.control_generation
            or ack.runner_generation != command.runner_generation
            or ack.capability_token_hash != command.issued_capability_token_hash
            or ack.device_id_hash != command.issued_device_id_hash
            or ack.command_revision + 1 != command.revision):
        return False
    expected_ack_payload = autopilot_ledger.encode_ack_payload(
        expected_ack_type, {"error_code": event.reason_code})
    expected_event_payload = autopilot_ledger.encode_control_event_payload(
        "failure", {
            "error_code": event.reason_code,
            "source": "device_ack",
        })
    return (
        ack.payload_json == expected_ack_payload
        and command.result_json == expected_ack_payload
        and event.payload_json == expected_event_payload
    )


def _system_failure_proves_media_terminal(
    db: Session,
    *,
    event: AutopilotControlEvent,
    command: RuntimeCommand,
    state: SessionAutopilotState,
) -> bool:
    if (not _control_event_matches_state(event, state)
            or event.event_type != "failure"
            or event.actor_type != "system"
            or event.command_id != command.id
            or event.from_mode != "autonomous"
            or event.to_mode != "autonomous"
            or event.to_status != state.status
            or command.kind != "record"
            or command.state != "succeeded"
            or command.succeeded_at is None
            or not event.reason_code):
        return False
    try:
        payload = json.loads(event.payload_json)
    except (TypeError, ValueError):
        return False
    if (not isinstance(payload, dict)
            or payload.get("error_code") != event.reason_code
            or payload.get("source") not in {
                "attempt_processing", "worker_exception"}
            or set(payload) != {"error_code", "source"}):
        return False
    try:
        autopilot_ledger.verify_terminal_record_capture(db, command.id)
    except autopilot_ledger.AutopilotProofError:
        return False
    return True


def _failure_proves_media_terminal(
    db: Session,
    *,
    event: AutopilotControlEvent,
    command: RuntimeCommand,
    state: SessionAutopilotState,
) -> bool:
    return _device_failure_proves_media_terminal(
        db, event=event, command=command, state=state,
    ) or _system_failure_proves_media_terminal(
        db, event=event, command=command, state=state,
    )


def _scope_completion_proves_media_terminal(
    db: Session,
    *,
    event: AutopilotControlEvent,
    command: RuntimeCommand,
    state: SessionAutopilotState,
) -> bool:
    if (not _control_event_matches_state(event, state)
            or event.event_type != "scope_complete"
            or event.actor_type != "system"
            or event.command_id != command.id
            or event.from_mode != "autonomous"
            or event.to_mode != "autonomous"
            or event.to_status != "scope_completed"
            or command.kind != "tts"
            or command.state != "succeeded"
            or command.succeeded_at is None):
        return False
    ack = db.exec(select(RuntimeCommandAck).where(
        RuntimeCommandAck.command_id == command.id,
        RuntimeCommandAck.ack_type == "tts_ended",
    )).first()
    if (ack is None
            or ack.session_id != command.session_id
            or ack.control_generation != command.control_generation
            or ack.runner_generation != command.runner_generation
            or ack.capability_token_hash != command.issued_capability_token_hash
            or ack.device_id_hash != command.issued_device_id_hash
            or ack.command_revision + 1 != command.revision):
        return False
    try:
        payload = json.loads(ack.payload_json)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("media_ended") is True
        and event.payload_json == autopilot_ledger.encode_control_event_payload(
            "scope_complete", {"completed_command_seq": command.command_seq})
    )


def _drain_event_matches(
    event: AutopilotControlEvent,
    *,
    state: SessionAutopilotState,
    command: RuntimeCommand,
    capability: PatientDeviceCapability | None = None,
) -> bool:
    if (not _control_event_matches_state(event, state)
            or event.event_type != "drain_complete"
            or event.actor_type != "device"
            or event.command_id != command.id
            or event.from_mode != "autonomous"
            or event.to_mode != "autonomous"
            or event.from_status != "paused"
            or event.to_status != "paused"):
        return False
    if capability is not None and event.actor_id != capability.device_id_hash:
        return False
    return event.payload_json == autopilot_ledger.encode_control_event_payload(
        "drain_complete", {"drained_command_id": command.id})


def _researcher_pause_matches(
    event: AutopilotControlEvent,
    *,
    state: SessionAutopilotState,
    command: RuntimeCommand,
) -> bool:
    return (
        _control_event_matches_state(event, state)
        and event.event_type == "pause"
        and event.actor_type == "researcher"
        and event.command_id == command.id
        and event.from_mode == "autonomous"
        and event.to_mode == "autonomous"
        and event.to_status == "paused"
        and event.reason_code == "researcher_requested_pause"
        and event.payload_json == autopilot_ledger.encode_control_event_payload(
            "pause", {
                "reason_code": "researcher_requested_pause",
                "source": "account_pause_endpoint",
            })
    )


def _external_stop_pause_matches(
    event: AutopilotControlEvent,
    *,
    state: SessionAutopilotState,
    command: RuntimeCommand,
) -> bool:
    """Verify a non-command-ACK stop before accepting the exact device drain."""
    if (not _control_event_matches_state(event, state)
            or event.event_type != "pause"
            or event.command_id != command.id
            or event.from_mode != "autonomous"
            or event.to_mode != "autonomous"
            or event.to_status != "paused"
            or event.reason_code != state.last_error_code
            or event.actor_type not in {"system", "researcher", "device"}):
        return False
    try:
        payload = json.loads(event.payload_json)
    except (TypeError, ValueError):
        return False
    if (not isinstance(payload, dict)
            or set(payload) != {"reason_code", "source"}
            or payload.get("reason_code") != event.reason_code
            or payload.get("source") not in _DRAINABLE_PAUSE_SOURCES):
        return False
    if payload["source"] == "patient_rec_failure":
        return event.actor_type == "device" and bool(event.actor_id)
    if payload["source"] == "cloud_processing_consent_revoked":
        return event.actor_type == "system" and event.actor_id is None
    if payload["source"] == "protocol_position_gap":
        return event.actor_type == "system" and event.actor_id is None
    return payload["source"] == "session_abort" and event.actor_type == "researcher"


def get_drain_target(
    db: Session,
    *,
    session_id: str,
    capability_token_hash: str,
) -> DrainTargetProjection:
    """Recover only the opaque exact command needed to finish a safe drain.

    This is intentionally not a command replay endpoint: no kind, item, prompt,
    recording id, or speech content is projected.  The latest closed control
    fact is the sole source of the target after a patient-page refresh.
    """
    if not isinstance(capability_token_hash, str) or not capability_token_hash:
        _fail("autopilot_drain_target_invalid", "设备能力摘要格式非法")
    state = db.exec(select(SessionAutopilotState).where(
        SessionAutopilotState.session_id == session_id,
    ).with_for_update()).first()
    if (state is None
            or state.scope_key != P0A_SCOPE_KEY
            or state.mode != "autonomous"
            or state.status not in {"paused", "failed", "scope_completed"}
            or state.current_command_id is not None):
        _fail(
            "autopilot_drain_target_unavailable",
            "当前自动驾驶状态没有可恢复的收麦目标",
        )
    latest = _latest_control_event(db, session_id)
    if (latest is None
            or latest.command_id is None
            or not _control_event_matches_state(latest, state)):
        _fail(
            "autopilot_drain_target_invalid",
            "最新自动驾驶控制事实缺少精确收麦目标",
        )
    command = db.get(RuntimeCommand, latest.command_id)
    if command is None or not _command_matches_control_state(command, state):
        _fail(
            "autopilot_drain_target_invalid",
            "收麦目标命令与当前 generation 不一致",
        )
    capability = _require_issued_drain_device(
        db,
        state=state,
        command=command,
        capability_token_hash=capability_token_hash,
    )
    closed = (
        (state.status == "paused" and _researcher_pause_matches(
            latest, state=state, command=command))
        or (state.status == "paused" and _external_stop_pause_matches(
            latest, state=state, command=command))
        or (state.status == "paused" and _drain_event_matches(
            latest, state=state, command=command, capability=capability))
        or _failure_proves_media_terminal(
            db, event=latest, command=command, state=state)
        or (state.status == "scope_completed"
            and _scope_completion_proves_media_terminal(
                db, event=latest, command=command, state=state))
    )
    if not closed:
        _fail(
            "autopilot_drain_target_invalid",
            "最新控制事实不能唯一派生安全收麦目标",
        )
    return DrainTargetProjection(
        command_key=command.idempotency_key,
        state_revision=state.revision,
    )


def acknowledge_device_drain(
    db: Session,
    *,
    session_id: str,
    command_key: str,
    capability_token_hash: str,
    now: datetime | None = None,
) -> DrainAckReceipt:
    """Prove that an exact paused command has stopped using patient media.

    Researcher pauses require a new device drain fact.  A terminal device/system
    failure or completed scope already carries the same closed media-stop proof,
    so those retries return success without manufacturing a second control event.
    """
    command_key = _validate_idempotency_key(command_key, "command_key")
    if not isinstance(capability_token_hash, str) or not capability_token_hash:
        _fail("autopilot_drain_invalid", "设备能力摘要格式非法")
    observed_at = _utc_naive(now) if now is not None else _utc_now_naive()
    state = db.exec(select(SessionAutopilotState).where(
        SessionAutopilotState.session_id == session_id,
    ).with_for_update()).first()
    command = db.exec(select(RuntimeCommand).where(
        RuntimeCommand.session_id == session_id,
        RuntimeCommand.idempotency_key == command_key,
    ).with_for_update()).first()
    if (state is None or command is None
            or state.scope_key != P0A_SCOPE_KEY
            or state.mode != "autonomous"
            or state.status not in {"paused", "scope_completed", "failed"}
            or not _command_matches_control_state(command, state)):
        _fail("autopilot_drain_not_current", "当前状态不接受该命令的收麦回执")
    capability = _require_issued_drain_device(
        db,
        state=state,
        command=command,
        capability_token_hash=capability_token_hash,
    )
    latest = _latest_control_event(db, session_id)
    if latest is None:
        _fail("autopilot_drain_proof_missing", "自动驾驶状态缺少暂停或终态审计")

    # An exact retry after a persisted drain never advances revision again.
    if _drain_event_matches(
            latest, state=state, command=command, capability=capability):
        return DrainAckReceipt(replayed=True, state_revision=state.revision)

    # Closed terminal facts already prove that playback/recording execution ended.
    # The patient client may still retry drain after observing runtime pause; keep
    # that retry successful and side-effect free.
    if (_failure_proves_media_terminal(
            db, event=latest, command=command, state=state)
            or (state.status == "scope_completed"
                and _scope_completion_proves_media_terminal(
                    db, event=latest, command=command, state=state))):
        return DrainAckReceipt(replayed=True, state_revision=state.revision)

    if (state.status != "paused"
            or not (
                _researcher_pause_matches(latest, state=state, command=command)
                or _external_stop_pause_matches(
                    latest, state=state, command=command)
            )):
        _fail(
            "autopilot_drain_pause_mismatch",
            "收麦回执与最新研究者暂停命令不一致",
        )

    previous_revision = state.revision
    result = db.execute(
        update(SessionAutopilotState)
        .where(
            SessionAutopilotState.session_id == session_id,
            SessionAutopilotState.scope_key == P0A_SCOPE_KEY,
            SessionAutopilotState.mode == "autonomous",
            SessionAutopilotState.status == "paused",
            SessionAutopilotState.current_command_id.is_(None),
            SessionAutopilotState.control_generation == state.control_generation,
            SessionAutopilotState.runner_generation == state.runner_generation,
            SessionAutopilotState.revision == previous_revision,
        )
        .values(
            revision=SessionAutopilotState.revision + 1,
            updated_at=observed_at,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        _fail("autopilot_drain_cas_conflict", "收麦状态已被其他请求推进")
    db.add(AutopilotControlEvent(
        idempotency_key=_derived_key(
            "device-drain", session_id, latest.idempotency_key, command.id),
        session_id=session_id,
        event_seq=latest.event_seq + 1,
        event_type="drain_complete",
        scope_key=P0A_SCOPE_KEY,
        control_generation=state.control_generation,
        runner_generation=state.runner_generation,
        command_id=command.id,
        actor_type="device",
        actor_id=capability.device_id_hash,
        reason_code="device_media_drained",
        from_mode="autonomous",
        to_mode="autonomous",
        from_status="paused",
        to_status="paused",
        payload_json=autopilot_ledger.encode_control_event_payload(
            "drain_complete", {"drained_command_id": command.id}),
        created_at=observed_at,
    ))
    db.flush()
    return DrainAckReceipt(
        replayed=False,
        state_revision=previous_revision + 1,
    )


def _safe_takeover_proof(
    db: Session,
    *,
    state: SessionAutopilotState,
) -> tuple[AutopilotControlEvent, RuntimeCommand]:
    latest = _latest_control_event(db, state.session_id)
    if latest is None or not _control_event_matches_state(latest, state):
        _fail("autopilot_takeover_proof_missing", "显式接管缺少当前 generation 安全证据")
    if latest.command_id is None:
        _fail("autopilot_takeover_proof_missing", "显式接管缺少精确命令证据")
    command = db.get(RuntimeCommand, latest.command_id)
    if command is None or not _command_matches_control_state(command, state):
        _fail("autopilot_takeover_proof_invalid", "显式接管命令证据无效")
    if state.status == "paused":
        if (_drain_event_matches(latest, state=state, command=command)
                or _failure_proves_media_terminal(
                    db, event=latest, command=command, state=state)):
            return latest, command
        _fail("autopilot_takeover_drain_required", "研究者暂停后必须先由发行设备完成收麦证明")
    if (state.status == "scope_completed"
            and _scope_completion_proves_media_terminal(
                db, event=latest, command=command, state=state)):
        return latest, command
    if (state.status == "failed"
            and _failure_proves_media_terminal(
                db, event=latest, command=command, state=state)):
        return latest, command
    _fail(
        "autopilot_pause_required",
        "自动驾驶仍在执行；必须先完成安全暂停",
    )


def takeover_autopilot_to_manual(
    db: Session,
    *,
    session_id: str,
    idempotency_key: str,
    expected_revision: int,
    actor_id: str,
    now: datetime | None = None,
) -> AutopilotStatusReceipt:
    """Release server ownership only after an exact persisted media-stop proof."""
    idempotency_key = _validate_idempotency_key(idempotency_key, "idempotency_key")
    if (not isinstance(expected_revision, int) or isinstance(expected_revision, bool)
            or expected_revision < 0):
        _fail("autopilot_input_invalid", "expected_revision 必须是非负整数")
    if not isinstance(actor_id, str) or not actor_id.strip() or len(actor_id) > 128:
        _fail("autopilot_input_invalid", "actor_id 格式非法")
    observed_at = _utc_naive(now) if now is not None else _utc_now_naive()
    expected_payload = autopilot_ledger.encode_control_event_payload(
        "takeover", {
            "reason_code": "researcher_explicit_takeover",
            "source": "account_takeover_endpoint",
            "expected_revision": expected_revision,
        })
    state = db.exec(select(SessionAutopilotState).where(
        SessionAutopilotState.session_id == session_id,
    ).with_for_update()).first()
    prior = db.exec(select(AutopilotControlEvent).where(
        AutopilotControlEvent.idempotency_key == idempotency_key,
    )).first()
    if prior is not None:
        if (state is None
                or prior.session_id != session_id
                or prior.event_type != "takeover"
                or prior.scope_key != P0A_SCOPE_KEY
                or prior.actor_type != "researcher"
                or prior.actor_id != actor_id
                or prior.to_mode != "manual"
                or prior.payload_json != expected_payload):
            _fail("autopilot_idempotency_conflict", "接管幂等键已被其他事实使用")
        if (state.scope_key != P0A_SCOPE_KEY
                or state.mode != "manual"
                or state.revision < expected_revision + 1
                or state.current_command_id is not None
                or state.lease_owner is not None):
            _fail("autopilot_state_invalid", "接管事件缺少一致的人工控制状态")
        return get_autopilot_status(db, session_id=session_id)

    if state is None or state.scope_key != P0A_SCOPE_KEY:
        _fail("autopilot_not_active", "当前场次没有可接管的 P0a 状态")
    if state.mode != "autonomous":
        _fail("autopilot_already_manual", "当前场次已不由自动驾驶持有")
    if state.revision != expected_revision:
        _fail("autopilot_revision_conflict", "自动驾驶状态 revision 已变化")
    if state.status in {
            "idle", "running", "waiting_tts", "waiting_recording",
            "processing_attempt", "manual_draining"}:
        _fail(
            "autopilot_pause_required",
            "自动驾驶仍在执行；必须先完成安全暂停",
        )
    proof, command = _safe_takeover_proof(db, state=state)
    previous_status = state.status
    result = db.execute(
        update(SessionAutopilotState)
        .where(
            SessionAutopilotState.session_id == session_id,
            SessionAutopilotState.scope_key == P0A_SCOPE_KEY,
            SessionAutopilotState.mode == "autonomous",
            SessionAutopilotState.status == previous_status,
            SessionAutopilotState.current_command_id.is_(None),
            SessionAutopilotState.control_generation == state.control_generation,
            SessionAutopilotState.runner_generation == state.runner_generation,
            SessionAutopilotState.revision == expected_revision,
        )
        .values(
            mode="manual",
            revision=SessionAutopilotState.revision + 1,
            current_command_id=None,
            lease_owner=None,
            lease_acquired_at=None,
            lease_expires_at=None,
            updated_at=observed_at,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        _fail("autopilot_takeover_cas_conflict", "接管状态已被其他请求推进")
    db.add(AutopilotControlEvent(
        idempotency_key=idempotency_key,
        session_id=session_id,
        event_seq=proof.event_seq + 1,
        event_type="takeover",
        scope_key=P0A_SCOPE_KEY,
        control_generation=state.control_generation,
        runner_generation=state.runner_generation,
        command_id=command.id,
        actor_type="researcher",
        actor_id=actor_id,
        reason_code="researcher_explicit_takeover",
        from_mode="autonomous",
        to_mode="manual",
        from_status=previous_status,
        to_status=previous_status,
        payload_json=expected_payload,
        created_at=observed_at,
    ))
    db.flush()
    db.expire(state)
    return get_autopilot_status(db, session_id=session_id)


def get_autopilot_status(
    db: Session,
    *,
    session_id: str,
) -> AutopilotStatusReceipt:
    """Return account-safe server ownership without projecting a command payload."""
    if db.get(TrainSession, session_id) is None:
        _fail("autopilot_session_unavailable", "场次不存在")
    state = db.get(SessionAutopilotState, session_id)
    if state is None or state.scope_key == "disabled":
        return AutopilotStatusReceipt(
            scope_key="disabled",
            mode="disabled",
            status="idle",
            # Disabled is one canonical ownership fact, not a projection of a
            # half-initialized row. This fixed value keeps refresh recovery exact.
            state_revision=0,
            server_owned=False,
            current_command_kind=None,
            last_error_code=None,
        )
    if state.scope_key != P0A_SCOPE_KEY:
        _fail("autopilot_state_invalid", "自动驾驶 scope 非法")
    if state.mode not in {"autonomous", "manual"}:
        _fail("autopilot_state_invalid", "自动驾驶控制模式非法")
    current_kind: Literal["tts", "record"] | None = None
    if state.current_command_id is not None:
        command = db.get(RuntimeCommand, state.current_command_id)
        if (command is None or command.session_id != session_id
                or command.scope_key != state.scope_key
                or command.control_generation != state.control_generation
                or command.runner_generation != state.runner_generation
                or command.kind not in {"tts", "record"}):
            _fail("autopilot_state_invalid", "自动驾驶状态的当前命令不一致")
        current_kind = command.kind  # type: ignore[assignment]
    command_matches_status = (
        (state.status == "waiting_tts" and current_kind == "tts")
        or (state.status == "waiting_recording" and current_kind == "record")
        or (state.status not in {"waiting_tts", "waiting_recording"}
            and current_kind is None)
    )
    if not command_matches_status:
        _fail("autopilot_state_invalid", "自动驾驶状态与当前命令不一致")
    if (state.last_error_code is not None
            and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", state.last_error_code) is None):
        _fail("autopilot_state_invalid", "自动驾驶错误码非法")
    return AutopilotStatusReceipt(
        scope_key=P0A_SCOPE_KEY,
        mode=state.mode,  # type: ignore[arg-type]
        status=state.status,  # type: ignore[arg-type]
        state_revision=state.revision,
        # Autonomous ownership persists across refresh, processing, pause, failure
        # and scope completion. Only an explicit future manual takeover releases it.
        server_owned=state.mode == "autonomous",
        current_command_kind=current_kind,
        last_error_code=state.last_error_code,
    )


def _require_terminal_tts_ack(
    db: Session,
    state: SessionAutopilotState,
    command: RuntimeCommand,
    ack_key: str,
) -> RuntimeCommandAck:
    if (command.id is None or command.kind != "tts" or command.state != "succeeded"
            or command.succeeded_at is None):
        _fail("autopilot_tts_not_ended", "TTS 命令尚无终态成功事实")
    ack = db.exec(select(RuntimeCommandAck).where(
        RuntimeCommandAck.command_id == command.id,
        RuntimeCommandAck.idempotency_key == ack_key,
        RuntimeCommandAck.ack_type == "tts_ended",
    )).first()
    if ack is None:
        _fail("autopilot_tts_ack_missing", "缺少持久化的 TTS 结束回执")
    if (ack.session_id != command.session_id
            or ack.control_generation != command.control_generation
            or ack.runner_generation != command.runner_generation
            or ack.command_revision + 1 != command.revision
            or state.control_generation != command.control_generation
            or state.runner_generation != command.runner_generation):
        _fail("autopilot_tts_ack_stale", "TTS 结束回执 generation 或 revision 已失效")
    capability = db.get(PatientDeviceCapability, ack.capability_token_hash)
    if (capability is None or capability.session_id != command.session_id
            or capability.device_id_hash != ack.device_id_hash
            or capability.created_at > ack.received_at
            or capability.expires_at <= ack.received_at
            or (capability.revoked_at is not None
                and capability.revoked_at <= ack.received_at)
            or (capability.recovery_only_at is not None
                and capability.recovery_only_at <= ack.received_at)):
        _fail("autopilot_tts_ack_invalid", "TTS 回执设备证据无效")
    if (command.issued_capability_token_hash != ack.capability_token_hash
            or command.issued_device_id_hash != ack.device_id_hash
            or command.issued_at > ack.received_at):
        _fail("autopilot_tts_ack_invalid", "TTS 回执不是由命令发行设备产生")
    try:
        payload = json.loads(ack.payload_json)
        canonical = autopilot_ledger.encode_ack_payload("tts_ended", payload)
    except (TypeError, ValueError) as exc:
        raise AutopilotServiceError(
            "autopilot_tts_ack_invalid", "TTS 结束回执载荷非法") from exc
    if (not isinstance(payload, dict) or payload.get("media_ended") is not True
            or canonical != ack.payload_json):
        _fail("autopilot_tts_ack_invalid", "TTS 结束回执没有可信 media_ended 事实")
    return ack


def _existing_record_route(
    db: Session,
    *,
    state: SessionAutopilotState,
    tts: RuntimeCommand,
    ack_key: str,
    selected: _P0aContent,
    active_capability: PatientDeviceCapability,
) -> RuntimeCommand | None:
    record_key = _derived_key(
        "cmd-record", tts.session_id, tts.idempotency_key, ack_key)
    record = _command_by_key(db, tts.session_id, record_key)
    if record is None:
        return None
    if (record.predecessor_command_id != tts.id
            or record.trigger_ack_idempotency_key != ack_key
            or state.status != "waiting_recording"
            or state.current_command_id != record.id
            or state.control_generation != record.control_generation
            or state.runner_generation != record.runner_generation):
        _fail("autopilot_idempotency_conflict", "TTS 路由幂等事实不一致")
    try:
        autopilot_ledger.verify_tts_ended_prerequisite(db, record)
    except autopilot_ledger.AutopilotProofError as exc:
        raise AutopilotServiceError(
            "autopilot_command_invalid", "既有录音命令缺少有效 TTS 证据") from exc
    _project_command(
        db, record, selected, expected_capability=active_capability)
    return record


def _existing_scope_completion(
    db: Session,
    *,
    state: SessionAutopilotState,
    tts: RuntimeCommand,
    ack_key: str,
) -> bool:
    event_key = _derived_key(
        "scope-complete", tts.session_id, tts.idempotency_key, ack_key)
    event = db.exec(select(AutopilotControlEvent).where(
        AutopilotControlEvent.idempotency_key == event_key,
    )).first()
    if event is None:
        return False
    if (event.session_id != tts.session_id or event.event_type != "scope_complete"
            or event.scope_key != P0A_SCOPE_KEY or event.command_id != tts.id
            or state.status != "scope_completed" or state.current_command_id is not None
            or event.control_generation != state.control_generation
            or event.runner_generation != state.runner_generation):
        _fail("autopilot_idempotency_conflict", "scope 完成幂等事实不一致")
    return True


def _next_position_command_key(
        tts: RuntimeCommand, ack_key: str,
        position: autopilot_positions.ProtocolPosition) -> str:
    return _derived_key(
        "cmd-next-position", tts.session_id, tts.idempotency_key, ack_key,
        position.position_key)


def _existing_position_advance(
    db: Session,
    *,
    state: SessionAutopilotState,
    tts: RuntimeCommand,
    ack_key: str,
    selected: _P0aContent,
    active_capability: PatientDeviceCapability,
) -> RuntimeCommand | None:
    key = _next_position_command_key(
        tts,
        ack_key,
        autopilot_positions.ProtocolPosition(
            item_index=selected.item_index,
            item_id=selected.item_id,
            task_type="单要素",
            image_id=selected.image_id,
            turn_seq=selected.turn_seq,
            response_role=selected.response_role,
        ),
    )
    command = _command_by_key(db, tts.session_id, key)
    if command is None:
        return None
    if (state.status != "waiting_tts" or state.current_command_id != command.id
            or command.kind != "tts" or command.state not in {"pending", "started"}
            or command.command_seq + 1 != state.next_command_seq
            or command.control_generation != state.control_generation
            or command.runner_generation != state.runner_generation
            or command.attempt_seq != 1 or command.prompt_level != 0
            or command.turn_key != f"{selected.item_id}#{selected.turn_seq}"):
        _fail("autopilot_idempotency_conflict", "跨位置推进幂等事实不一致")
    return command


def _position_gap_event_key(
        tts: RuntimeCommand, ack_key: str,
        gap: autopilot_positions.PositionGap) -> str:
    return _derived_key(
        "position-gap", tts.session_id, tts.idempotency_key, ack_key,
        gap.position.position_key, gap.code)


def _existing_position_gap(
    db: Session,
    *,
    state: SessionAutopilotState,
    tts: RuntimeCommand,
    ack_key: str,
    gap: autopilot_positions.PositionGap,
) -> bool:
    event = db.exec(select(AutopilotControlEvent).where(
        AutopilotControlEvent.idempotency_key
        == _position_gap_event_key(tts, ack_key, gap),
    )).first()
    if event is None:
        return False
    expected_payload = autopilot_ledger.encode_control_event_payload(
        "pause", {"reason_code": gap.code, "source": "protocol_position_gap"})
    if (state.status != "paused" or state.current_command_id is not None
            or state.last_error_code != gap.code
            or event.session_id != tts.session_id or event.event_type != "pause"
            or event.scope_key != P0A_SCOPE_KEY or event.command_id != tts.id
            or event.actor_type != "system" or event.actor_id is not None
            or event.reason_code != gap.code
            or event.control_generation != state.control_generation
            or event.runner_generation != state.runner_generation
            or event.payload_json != expected_payload):
        _fail("autopilot_idempotency_conflict", "协议位置缺口幂等事实不一致")
    return True


def route_tts_ended(
    db: Session,
    *,
    session_id: str,
    command_key: str,
    ack_idempotency_key: str,
    bank: content.ItemBank | None = None,
    protocol: dict | None = None,
    now: datetime | None = None,
) -> RouteTtsEndedResult:
    """Route a persisted terminal TTS ACK without any provider call.

    Question/cue speech pre-allocates exactly one ``AudioAssetRow`` before making
    its record command current.  Feedback/tell-answer speech advances exactly one
    frozen plan position, pauses at the first content gap, or completes only this
    P0a scope; it deliberately leaves ``SessionRuntimeState`` untouched.
    """
    command_key = _validate_idempotency_key(command_key, "command_key")
    ack_idempotency_key = _validate_idempotency_key(
        ack_idempotency_key, "ack_idempotency_key")
    observed_at = _utc_naive(now) if now is not None else _utc_now_naive()
    resolved_bank = bank or _default_bank()
    resolved_protocol = protocol or _default_protocol()
    tts = _command_by_key(db, session_id, command_key)
    gate = _require_gate(
        db,
        session_id,
        bank=resolved_bank,
        protocol=resolved_protocol,
        now=observed_at,
        position_item_id=tts.item_id if tts is not None else None,
        position_turn_seq=tts.turn_seq if tts is not None else None,
    )
    state = db.exec(select(SessionAutopilotState).where(
        SessionAutopilotState.session_id == session_id,
    ).with_for_update()).first()
    if state is None or tts is None or state.scope_key != P0A_SCOPE_KEY:
        _fail("autopilot_command_not_current", "TTS 命令不存在或不属于当前 P0a")
    _validate_command_identity(tts, gate.selected)
    _require_same_issued_active_device(tts, gate.active_capability)
    try:
        tts_payload = TtsCommandPayload.model_validate_json(tts.payload_json)
    except (TypeError, ValueError) as exc:
        raise AutopilotServiceError(
            "autopilot_command_invalid", "TTS 命令载荷不符合封闭契约") from exc

    effect = effect_after_tts_ended(tts_payload.purpose)
    existing_record = _existing_record_route(
        db,
        state=state,
        tts=tts,
        ack_key=ack_idempotency_key,
        selected=gate.selected,
        active_capability=gate.active_capability,
    )
    if existing_record is not None:
        return RouteTtsEndedResult(
            status="waiting_recording",
            replayed=True,
            command=_project_command(
                db, existing_record, gate.selected,
                expected_capability=gate.active_capability),
        )
    next_decision: autopilot_positions.PositionDecision | None = None
    next_selected: _P0aContent | None = None
    if effect == "advance":
        event_line = _enum_value(gate.train_session.event_line)
        try:
            next_decision = autopilot_positions.next_position_decision(
                resolved_bank,
                week_no=gate.train_session.week_no,
                event_line=event_line,
                current_item_id=tts.item_id,
                current_turn_seq=tts.turn_seq,
            )
        except ValueError as exc:
            raise AutopilotServiceError(
                "autopilot_position_invalid", "当前命令无法在冻结计划中推进") from exc
        if next_decision.position is not None:
            next_selected = _select_p0a_content(
                gate.train_session,
                resolved_bank,
                resolved_protocol,
                item_id=next_decision.position.item_id,
                turn_seq=next_decision.position.turn_seq,
            )
            existing_next = _existing_position_advance(
                db,
                state=state,
                tts=tts,
                ack_key=ack_idempotency_key,
                selected=next_selected,
                active_capability=gate.active_capability,
            )
            if existing_next is not None:
                return RouteTtsEndedResult(
                    status="waiting_tts",
                    replayed=True,
                    command=_project_command(
                        db, existing_next, next_selected,
                        expected_capability=gate.active_capability),
                )
        elif next_decision.gap is not None and _existing_position_gap(
                db,
                state=state,
                tts=tts,
                ack_key=ack_idempotency_key,
                gap=next_decision.gap):
            return RouteTtsEndedResult(
                status="paused", replayed=True, command=None)
        elif next_decision.completed and _existing_scope_completion(
                db, state=state, tts=tts, ack_key=ack_idempotency_key):
            return RouteTtsEndedResult(
                status="scope_completed", replayed=True, command=None)

    if (state.mode != "autonomous" or state.status != "waiting_tts"
            or state.current_command_id != tts.id
            or state.control_generation != tts.control_generation
            or state.runner_generation != tts.runner_generation):
        _fail("autopilot_command_not_current", "TTS 命令已不是当前等待命令")
    _validate_tts_semantics(tts, tts_payload, gate.selected)
    terminal_ack = _require_terminal_tts_ack(
        db, state, tts, ack_idempotency_key)

    if effect == "create_record":
        raw_audio_id = _raw_audio_id(db)
        db.add(AudioAssetRow(
            raw_audio_id=raw_audio_id,
            session_id=session_id,
            is_simulation=True,
            data_classification="simulation",
            turn_key=tts.turn_key,
            audio_format="webm",
            status=AudioStatus.recorded,
            withdrawn=False,
            contains_direct_identifier=False,
            delete_gate_passed=False,
        ))
        record_payload = RecordCommandPayload(
            raw_audio_id=raw_audio_id,
            turn_key=tts.turn_key,
            item_id=tts.item_id,
            turn_seq=tts.turn_seq,
            cue_level=tts.prompt_level,
            max_duration_seconds=gate.selected.max_duration_seconds,
            contains_direct_identifier=False,
        )
        record = RuntimeCommand(
            idempotency_key=_derived_key(
                "cmd-record", session_id, tts.idempotency_key, ack_idempotency_key),
            session_id=session_id,
            command_seq=state.next_command_seq,
            item_id=tts.item_id,
            turn_seq=tts.turn_seq,
            turn_key=tts.turn_key,
            attempt_seq=tts.attempt_seq,
            prompt_level=tts.prompt_level,
            **_command_definition_fields(gate.selected),
            scope_key=P0A_SCOPE_KEY,
            control_generation=tts.control_generation,
            runner_generation=tts.runner_generation,
            issued_capability_token_hash=gate.active_capability.token_hash,
            issued_device_id_hash=gate.active_capability.device_id_hash,
            issued_at=max(observed_at, terminal_ack.received_at, tts.succeeded_at),
            kind="record",
            state="pending",
            predecessor_command_id=tts.id,
            trigger_ack_idempotency_key=ack_idempotency_key,
            expected_raw_audio_id=raw_audio_id,
            payload_json=record_payload.model_dump_json(),
            created_at=observed_at,
            updated_at=observed_at,
        )
        db.add(record)
        db.flush()
        try:
            autopilot_ledger.verify_tts_ended_prerequisite(db, record)
        except autopilot_ledger.AutopilotProofError as exc:
            raise AutopilotServiceError(
                "autopilot_tts_ack_invalid", "TTS 证据不足，不能打开麦克风") from exc
        state.status = "waiting_recording"
        state.current_command_id = record.id
        state.next_command_seq += 1
        state.revision += 1
        state.updated_at = observed_at
        db.add(state)
        db.flush()
        return RouteTtsEndedResult(
            status="waiting_recording",
            replayed=False,
            command=_project_command(
                db, record, gate.selected,
                expected_capability=gate.active_capability),
        )

    assert next_decision is not None  # terminal TTS branch established above
    if next_decision.position is not None:
        assert next_selected is not None
        command = RuntimeCommand(
            idempotency_key=_next_position_command_key(
                tts, ack_idempotency_key, next_decision.position),
            session_id=session_id,
            command_seq=state.next_command_seq,
            item_id=next_selected.item_id,
            turn_seq=next_selected.turn_seq,
            turn_key=f"{next_selected.item_id}#{next_selected.turn_seq}",
            attempt_seq=1,
            prompt_level=0,
            **_command_definition_fields(next_selected),
            scope_key=P0A_SCOPE_KEY,
            control_generation=tts.control_generation,
            runner_generation=tts.runner_generation,
            issued_capability_token_hash=gate.active_capability.token_hash,
            issued_device_id_hash=gate.active_capability.device_id_hash,
            issued_at=max(observed_at, terminal_ack.received_at, tts.succeeded_at),
            kind="tts",
            state="pending",
            payload_json=TtsCommandPayload(
                speech_key=f"p0a.question.{state.next_command_seq}",
                speech_text=next_selected.initial_prompt,
                purpose="question",
                item_id=next_selected.item_id,
                turn_seq=next_selected.turn_seq,
                cue_level=0,
            ).model_dump_json(exclude_none=True),
            created_at=observed_at,
            updated_at=observed_at,
        )
        db.add(command)
        db.flush()
        state.status = "waiting_tts"
        state.current_command_id = command.id
        state.next_command_seq += 1
        state.revision += 1
        state.last_error_code = None
        state.updated_at = observed_at
        db.add(state)
        db.flush()
        return RouteTtsEndedResult(
            status="waiting_tts",
            replayed=False,
            command=_project_command(
                db, command, next_selected,
                expected_capability=gate.active_capability),
        )

    if next_decision.gap is not None:
        gap = next_decision.gap
        state.status = "paused"
        state.current_command_id = None
        state.revision += 1
        state.last_error_code = gap.code
        state.lease_owner = None
        state.lease_acquired_at = None
        state.lease_expires_at = None
        state.updated_at = observed_at
        db.add(state)
        db.add(AutopilotControlEvent(
            idempotency_key=_position_gap_event_key(
                tts, ack_idempotency_key, gap),
            session_id=session_id,
            event_seq=_next_control_event_seq(db, session_id),
            event_type="pause",
            scope_key=P0A_SCOPE_KEY,
            control_generation=state.control_generation,
            runner_generation=state.runner_generation,
            command_id=tts.id,
            actor_type="system",
            reason_code=gap.code,
            from_mode="autonomous",
            to_mode="autonomous",
            from_status="waiting_tts",
            to_status="paused",
            payload_json=autopilot_ledger.encode_control_event_payload(
                "pause", {
                    "reason_code": gap.code,
                    "source": "protocol_position_gap",
                }),
            created_at=observed_at,
        ))
        db.flush()
        return RouteTtsEndedResult(
            status="paused", replayed=False, command=None)

    # Every sequential protocol position has completed; this is still not the
    # intervention-completed research fact.
    assert next_decision.completed
    event_key = _derived_key(
        "scope-complete", session_id, tts.idempotency_key, ack_idempotency_key)
    state.status = "scope_completed"
    state.current_command_id = None
    state.revision += 1
    state.updated_at = observed_at
    db.add(state)
    db.add(AutopilotControlEvent(
        idempotency_key=event_key,
        session_id=session_id,
        event_seq=_next_control_event_seq(db, session_id),
        event_type="scope_complete",
        scope_key=P0A_SCOPE_KEY,
        control_generation=state.control_generation,
        runner_generation=state.runner_generation,
        command_id=tts.id,
        actor_type="system",
        from_mode="autonomous",
        to_mode="autonomous",
        from_status="waiting_tts",
        to_status="scope_completed",
        payload_json=autopilot_ledger.encode_control_event_payload(
            "scope_complete", {"completed_command_seq": tts.command_seq}),
        created_at=observed_at,
    ))
    db.flush()
    return RouteTtsEndedResult(
        status="scope_completed", replayed=False, command=None)


def fail_completed_scope_autofinish(
    db: Session,
    *,
    session_id: str,
    error_code: str,
    expected_turns: int,
    matched_turns: int,
    completed_attempt_turns: int,
    audio_evidenced_turns: int,
    issue_codes: tuple[str, ...],
    now: datetime | None = None,
) -> SessionAutopilotState:
    """Atomically ledger a completion-assessment failure after final playback.

    Only aggregate counts and machine issue codes enter the control ledger.  The
    participant's response/transcript is never copied into this control plane.
    """
    if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", error_code) is None:
        _fail("autopilot_input_invalid", "自动结束失败码格式非法")
    counts = (
        expected_turns,
        matched_turns,
        completed_attempt_turns,
        audio_evidenced_turns,
    )
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0
           for value in counts):
        _fail("autopilot_input_invalid", "自动结束证据计数非法")
    normalized_codes = tuple(sorted(set(issue_codes)))
    if (not normalized_codes
            or any(re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value) is None
                   for value in normalized_codes)):
        _fail("autopilot_input_invalid", "自动结束问题码格式非法")
    encoded_codes = ",".join(normalized_codes)
    if len(encoded_codes) > 128:
        _fail("autopilot_input_invalid", "自动结束问题码集合过长")

    observed_at = _utc_naive(now) if now is not None else _utc_now_naive()
    state = db.exec(select(SessionAutopilotState).where(
        SessionAutopilotState.session_id == session_id,
    ).with_for_update()).first()
    if (state is None or state.scope_key != P0A_SCOPE_KEY
            or state.mode != "autonomous"
            or state.status != "scope_completed"
            or state.current_command_id is not None):
        _fail("autopilot_state_invalid", "自动结束失败事实缺少已完成的权威范围")
    scope_event = db.exec(select(AutopilotControlEvent).where(
        AutopilotControlEvent.session_id == session_id,
        AutopilotControlEvent.event_type == "scope_complete",
        AutopilotControlEvent.control_generation == state.control_generation,
        AutopilotControlEvent.runner_generation == state.runner_generation,
    ).order_by(AutopilotControlEvent.event_seq.desc())).first()
    if scope_event is None:
        _fail("autopilot_state_invalid", "自动结束失败事实缺少范围完成账本")

    previous_revision = state.revision
    state.status = "failed"
    state.last_error_code = error_code
    state.lease_owner = None
    state.lease_acquired_at = None
    state.lease_expires_at = None
    state.revision += 1
    state.updated_at = observed_at
    db.add(state)
    db.add(AutopilotControlEvent(
        idempotency_key=_derived_key(
            "autofinish-failure", session_id, scope_event.idempotency_key,
            previous_revision, error_code),
        session_id=session_id,
        event_seq=_next_control_event_seq(db, session_id),
        event_type="failure",
        scope_key=P0A_SCOPE_KEY,
        control_generation=state.control_generation,
        runner_generation=state.runner_generation,
        command_id=scope_event.command_id,
        actor_type="system",
        reason_code=error_code,
        from_mode="autonomous",
        to_mode="autonomous",
        from_status="scope_completed",
        to_status="failed",
        payload_json=autopilot_ledger.encode_control_event_payload(
            "failure", {
                "error_code": error_code,
                "source": "intervention_completion_assessment",
                "expected_turns": expected_turns,
                "matched_turns": matched_turns,
                "completed_attempt_turns": completed_attempt_turns,
                "audio_evidenced_turns": audio_evidenced_turns,
                "issue_count": len(issue_codes),
                "issue_codes": encoded_codes,
            }),
        created_at=observed_at,
    ))
    db.flush()
    return state


def _lock_active_attempt_route_device(
    db: Session,
    *,
    session_id: str,
    expected: PatientDeviceCapability,
    now: datetime,
) -> PatientDeviceCapability:
    """Lock the one delivery capability before issuing post-judgement speech."""
    rows = list(db.exec(select(PatientDeviceCapability).where(
        PatientDeviceCapability.session_id == session_id,
        PatientDeviceCapability.active_session_key == session_id,
        PatientDeviceCapability.revoked_at.is_(None),
    ).with_for_update()))
    if len(rows) != 1:
        _fail("autopilot_device_not_paired", "当前场次没有唯一活跃的受试者设备")
    capability = rows[0]
    if (capability.token_hash != expected.token_hash
            or capability.device_id_hash != expected.device_id_hash):
        _fail("autopilot_command_device_rotated", "判类完成前受试者设备已换绑")
    if (capability.created_at > now or capability.expires_at <= now
            or capability.recovery_only_at is not None):
        _fail("autopilot_device_not_active", "受试者设备能力已失效或仅可恢复")
    return capability


def _require_completed_operational_attempt(
    db: Session,
    *,
    attempt: AttemptEvent,
    record: RuntimeCommand,
    selected: _P0aContent,
) -> dict:
    """Bind one terminal AttemptEvent to its operational-only judgement fact."""
    _validate_command_identity(record, selected)
    expected_identity = (
        attempt.session_id == record.session_id,
        attempt.raw_audio_id == record.expected_raw_audio_id,
        attempt.item_id == record.item_id == selected.item_id,
        attempt.turn_seq == record.turn_seq == selected.turn_seq,
        attempt.attempt_seq == record.attempt_seq,
        attempt.prompt_level == record.prompt_level,
        attempt.response_role == selected.response_role,
    )
    if not all(expected_identity):
        _fail("autopilot_attempt_mismatch", "completed attempt 与当前录音命令不一致")
    if (record.kind != "record" or record.state != "succeeded"
            or record.succeeded_at is None or record.scope_key != P0A_SCOPE_KEY):
        _fail("autopilot_attempt_mismatch", "attempt 缺少已成功的 P0a 录音命令")
    if (attempt.processing_status != "completed" or attempt.processed_at is None
            or attempt.processing_owner is not None
            or attempt.processing_lease_expires_at is not None
            or attempt.error_code is not None):
        _fail("autopilot_attempt_not_completed", "attempt 尚未完成或以技术失败收口")
    if attempt.is_simulation is not True or attempt.judge_portrait_used is not False:
        _fail("autopilot_attempt_boundary_invalid", "P0a attempt 必须是无画像的模拟运行证据")
    if (not isinstance(attempt.operational_answer_type, str)
            or not isinstance(attempt.contains_target, bool)):
        _fail("autopilot_attempt_judgement_invalid", "completed attempt 缺少完整运行判类")

    judgement_rows = list(db.exec(select(InteractionEvent).where(
        InteractionEvent.session_id == attempt.session_id,
        InteractionEvent.attempt_id == attempt.id,
        InteractionEvent.event_type == "judgement_completed",
    ).with_for_update()))
    if len(judgement_rows) != 1:
        _fail("autopilot_attempt_judgement_invalid", "attempt 必须有且仅有一条判类完成证据")
    event = judgement_rows[0]
    if (event.item_id != attempt.item_id or event.turn_seq != attempt.turn_seq
            or event.attempt_seq != attempt.attempt_seq
            or event.is_simulation is not True):
        _fail("autopilot_attempt_judgement_invalid", "attempt 判类证据位置不一致")
    try:
        payload = evidence_ledger.validate_stored_payload(
            event.event_type, event.payload_json)
    except ValueError as exc:
        raise AutopilotServiceError(
            "autopilot_attempt_judgement_invalid", "attempt 判类证据载荷非法") from exc
    if (payload.get("truth_scope") != "operational_only"
            or payload.get("answer_type") != attempt.operational_answer_type
            or payload.get("contains_target") is not attempt.contains_target):
        _fail("autopilot_attempt_boundary_invalid", "attempt 不是一致的 operational_only 证据")
    return payload


def _failed_response_path(attempt: AttemptEvent) -> ResponsePath:
    """Map one failed initial operational fact to the closed source branch."""
    if attempt.operational_answer_type == "沉默":
        return "silence"
    if attempt.operational_answer_type in {
        AnswerType.部分正确.value,
        AnswerType.上位词或相关词.value,
    }:
        return "close"
    return "unknown"


def _initial_response_path_for_cued_attempt(
    db: Session,
    *,
    attempt: AttemptEvent,
    record: RuntimeCommand,
    selected: _P0aContent,
) -> ResponsePath:
    """Derive the level-1 path from the exact persisted first-attempt chain.

    The current attempt and its cue command are not allowed to assert this path.
    We re-read the unique first AttemptEvent, its judgement event, terminal capture
    proof, and the actual cue command which opened the current recording slot.
    """
    first_rows = list(db.exec(select(AttemptEvent).where(
        AttemptEvent.session_id == attempt.session_id,
        AttemptEvent.item_id == attempt.item_id,
        AttemptEvent.turn_seq == attempt.turn_seq,
        AttemptEvent.attempt_seq == 1,
    ).with_for_update()))
    if len(first_rows) != 1:
        _fail(
            "autopilot_attempt_sequence_invalid",
            "一级提示后 attempt 缺少唯一首次作答证据",
        )
    first = first_rows[0]
    first_records = list(db.exec(select(RuntimeCommand).where(
        RuntimeCommand.session_id == attempt.session_id,
        RuntimeCommand.expected_raw_audio_id == first.raw_audio_id,
    ).with_for_update()))
    if len(first_records) != 1:
        _fail(
            "autopilot_attempt_sequence_invalid",
            "首次作答缺少唯一录音命令证据",
        )
    first_record = first_records[0]
    if (first.prompt_level != 0 or first.attempt_seq != 1
            or first_record.id is None or first.id is None
            or first_record.id == record.id):
        _fail("autopilot_attempt_sequence_invalid", "首次作答序列与提示级不一致")
    _require_completed_operational_attempt(
        db, attempt=first, record=first_record, selected=selected)
    try:
        proof = autopilot_ledger.verify_terminal_record_capture(db, first_record.id)
    except autopilot_ledger.AutopilotProofError as exc:
        raise AutopilotServiceError(
            "autopilot_attempt_sequence_invalid", "首次作答录音证据无法复核") from exc
    if (proof.session_id != first.session_id
            or proof.raw_audio_id != first.raw_audio_id
            or proof.item_id != first.item_id
            or proof.turn_seq != first.turn_seq
            or proof.attempt_seq != 1
            or proof.prompt_level != 0):
        _fail("autopilot_attempt_sequence_invalid", "首次作答与录音证据不一致")
    if first.contains_target is True:
        _fail("autopilot_attempt_sequence_invalid", "首次已命中冻结目标却又进入一级提示")
    expected_path = _failed_response_path(first)

    if record.predecessor_command_id is None:
        _fail("autopilot_attempt_sequence_invalid", "一级录音缺提示命令前件")
    cue = db.exec(select(RuntimeCommand).where(
        RuntimeCommand.id == record.predecessor_command_id,
        RuntimeCommand.session_id == attempt.session_id,
    ).with_for_update()).first()
    if cue is not None:
        _validate_command_identity(cue, selected)
    expected_route_key = _derived_key(
        "cmd-attempt", attempt.session_id, first_record.id, first.id)
    if (cue is None or cue.kind != "tts" or cue.state != "succeeded"
            or cue.idempotency_key != expected_route_key
            or cue.item_id != attempt.item_id
            or cue.turn_seq != attempt.turn_seq
            or cue.attempt_seq != 2 or cue.prompt_level != 1
            or record.attempt_seq != 2 or record.prompt_level != 1):
        _fail("autopilot_attempt_sequence_invalid", "一级提示与首次作答链路不一致")
    try:
        cue_payload = TtsCommandPayload.model_validate_json(cue.payload_json)
    except (TypeError, ValueError) as exc:
        raise AutopilotServiceError(
            "autopilot_attempt_sequence_invalid", "一级提示载荷非法") from exc
    _validate_tts_semantics(cue, cue_payload, selected)
    if cue_payload.response_path != expected_path:
        _fail("autopilot_attempt_sequence_invalid", "一级提示分支与首次作答证据不一致")
    return expected_path


def _attempt_route_decision(
    db: Session,
    *,
    attempt: AttemptEvent,
    record: RuntimeCommand,
    selected: _P0aContent,
) -> _AttemptRouteDecision:
    if attempt.prompt_level not in {0, 1, 2}:
        _fail("autopilot_attempt_mismatch", "P0a 只处理零至二级录音 attempt")
    if attempt.attempt_seq != attempt.prompt_level + 1:
        _fail("autopilot_attempt_sequence_invalid", "attempt_seq 与分级提示序列不单调")
    # Source completion is lexical: only a server-frozen target/acceptable hit
    # may advance.  A semantic/LLM "正确" without that hit remains a failed
    # operational attempt and must not bypass the cue path.
    succeeded = attempt.contains_target is True
    if succeeded:
        if attempt.prompt_level == 1:
            response_path = _initial_response_path_for_cued_attempt(
                db,
                attempt=attempt,
                record=record,
                selected=selected,
            )
            return _AttemptRouteDecision(
                purpose="feedback",
                speech_text=_cue1_success_line(selected, response_path),
                prompt_level=attempt.prompt_level,
                attempt_seq=attempt.attempt_seq,
                response_path=response_path,
            )
        if attempt.prompt_level == 2:
            return _AttemptRouteDecision(
                purpose="feedback",
                speech_text=selected.success_after_cue2,
                prompt_level=attempt.prompt_level,
                attempt_seq=attempt.attempt_seq,
            )
        return _AttemptRouteDecision(
            purpose="feedback",
            speech_text=selected.success_line,
            prompt_level=attempt.prompt_level,
            attempt_seq=attempt.attempt_seq,
        )
    if attempt.prompt_level == 0:
        response_path = _failed_response_path(attempt)
        return _AttemptRouteDecision(
            purpose="cue",
            speech_text=_cue1_line(selected, response_path),
            prompt_level=1,
            attempt_seq=attempt.attempt_seq + 1,
            response_path=response_path,
        )
    if attempt.prompt_level == 1:
        # Even a second failure must prove it followed the source-selected first
        # cue; otherwise a forged/tampered cue command could silently converge.
        _initial_response_path_for_cued_attempt(
            db,
            attempt=attempt,
            record=record,
            selected=selected,
        )
        return _AttemptRouteDecision(
            purpose="cue",
            speech_text=selected.cue2,
            prompt_level=2,
            attempt_seq=attempt.attempt_seq + 1,
        )
    return _AttemptRouteDecision(
        purpose="tell_answer",
        speech_text=selected.tell_answer,
        prompt_level=3,
        # Direct answer ends this turn; it does not invent a fourth recording.
        attempt_seq=attempt.attempt_seq,
    )


def _materialize_terminal_attempt_evidence(
    db: Session,
    *,
    gate: _P0aGate,
    record: RuntimeCommand,
    attempt: AttemptEvent,
    decision: _AttemptRouteDecision,
    bank: content.ItemBank,
) -> TerminalEvidenceMaterialization | None:
    """Stage the immutable operational projection for one terminal attempt.

    The autopilot state row is already locked by every caller.  Item/turn rows are
    therefore created in the same transaction as either the terminal judgement or
    its recovery route.  Existing exact rows are an idempotent replay; duplicate or
    contradictory rows are never guessed through.
    """
    if decision.purpose not in {"feedback", "tell_answer"}:
        return None
    if attempt.id is None:
        _fail("autopilot_terminal_evidence_invalid", "terminal attempt 缺少主键")

    try:
        proof = autopilot_ledger.verify_record_capture_for_attempt(db, record.id)
    except autopilot_ledger.AutopilotProofError as exc:
        raise AutopilotServiceError(
            "autopilot_terminal_evidence_invalid",
            "terminal attempt 缺少完整的录音采集证据",
        ) from exc
    if (
        proof.session_id != attempt.session_id
        or proof.item_id != attempt.item_id
        or proof.turn_seq != attempt.turn_seq
        or proof.attempt_seq != attempt.attempt_seq
        or proof.prompt_level != attempt.prompt_level
        or proof.raw_audio_id != attempt.raw_audio_id
        or proof.duration_seconds != attempt.duration_seconds
    ):
        _fail(
            "autopilot_terminal_evidence_conflict",
            "terminal attempt 与录音收据不精确匹配",
        )

    receipt = db.get(AudioCaptureReceipt, proof.receipt_server_seq)
    audio = db.get(AudioAssetRow, proof.raw_audio_id)
    train_session = gate.train_session
    expected_turn_key = f"{attempt.item_id}#{attempt.turn_seq}"
    receipt_matches = receipt is not None and (
        receipt.raw_audio_id == attempt.raw_audio_id
        and receipt.session_id == attempt.session_id
        and receipt.turn_key == expected_turn_key
        and receipt.checksum == proof.checksum
        and receipt.byte_count == proof.byte_count
        and receipt.duration_seconds == proof.duration_seconds
        and receipt.data_classification == train_session.data_classification
        and receipt.is_simulation == train_session.is_simulation
    )
    audio_matches = audio is not None and (
        audio.session_id == attempt.session_id
        and audio.turn_key == expected_turn_key
        and audio.checksum == proof.checksum
        and audio.byte_count == proof.byte_count
        and audio.uploaded_at is not None
        and _enum_value(audio.status) == AudioStatus.recorded.value
        and audio.data_classification == train_session.data_classification
        and audio.is_simulation == train_session.is_simulation
        and not audio.withdrawn
        and not bool((audio.withdrawal_status or "").strip())
        and not audio.delete_gate_passed
    )
    if not receipt_matches or not audio_matches:
        _fail(
            "autopilot_terminal_evidence_conflict",
            "terminal attempt 的录音资产、收据或数据分类不一致",
        )

    event_line = _enum_value(train_session.event_line)
    try:
        plan = runtime.build_session_plan(
            bank, train_session.week_no, event_line)
    except ValueError as exc:
        raise AutopilotServiceError(
            "autopilot_terminal_evidence_invalid", "场次冻结计划不可用") from exc
    if not 0 <= gate.selected.item_index < len(plan.items):
        _fail("autopilot_terminal_evidence_invalid", "terminal attempt 计划位置越界")
    plan_item = plan.items[gate.selected.item_index]
    plan_turns = [
        turn for turn in plan_item.turns
        if turn.turn_seq == attempt.turn_seq
        and turn.response_role == attempt.response_role
    ]
    if (
        train_session.item_bank_version_id != plan.item_bank_version_id
        or plan_item.item_id != attempt.item_id
        or plan_item.task_type != "单要素"
        or plan_item.image_id != gate.selected.image_id
        or len(plan_turns) != 1
    ):
        _fail(
            "autopilot_terminal_evidence_conflict",
            "terminal attempt 与场次冻结计划不一致",
        )

    item_rows = list(db.exec(select(ItemEvent).where(
        ItemEvent.session_id == attempt.session_id,
        ItemEvent.item_id == attempt.item_id,
    ).with_for_update()))
    if len(item_rows) > 1:
        _fail(
            "autopilot_terminal_evidence_conflict",
            "同一冻结计划题存在多条 ItemEvent",
        )
    if item_rows:
        item = item_rows[0]
        if (
            item.image_id != plan_item.image_id
            or _enum_value(item.task_type) != plan_item.task_type
            or _enum_value(item.item_set_type) != "训练集"
            or item.presentation_order != plan_item.presentation_order
            or item.difficulty_level is not None
            or item.random_seed is not None
        ):
            _fail(
                "autopilot_terminal_evidence_conflict",
                "已有 ItemEvent 与场次冻结计划冲突",
            )
    else:
        item = ItemEvent(
            session_id=attempt.session_id,
            item_id=plan_item.item_id,
            image_id=plan_item.image_id,
            task_type=plan_item.task_type,
            item_set_type="训练集",
            difficulty_level=None,
            presentation_order=plan_item.presentation_order,
            random_seed=None,
        )
        db.add(item)
        db.flush()
    if item.id is None:  # pragma: no cover - persisted/flush invariant
        _fail("autopilot_terminal_evidence_invalid", "ItemEvent 未能获得主键")

    turns_at_position = list(db.exec(select(TurnEvent).where(
        TurnEvent.item_event_id == item.id,
        TurnEvent.turn_seq == attempt.turn_seq,
    ).with_for_update()))
    if len(turns_at_position) > 1:
        _fail(
            "autopilot_terminal_evidence_conflict",
            "同一冻结计划环节存在多条 TurnEvent",
        )
    source_rows = list(db.exec(select(TurnEvent).where(
        TurnEvent.source_attempt_id == attempt.id,
    ).with_for_update()))
    if len(source_rows) > 1:
        _fail(
            "autopilot_terminal_evidence_conflict",
            "terminal attempt 被多条 TurnEvent 引用",
        )
    existing_turn = turns_at_position[0] if turns_at_position else None
    if source_rows and (existing_turn is None or source_rows[0].id != existing_turn.id):
        _fail(
            "autopilot_terminal_evidence_conflict",
            "terminal attempt 已被其他环节收口",
        )

    if existing_turn is not None:
        exact_projection = (
            existing_turn.response_role == attempt.response_role,
            existing_turn.source_attempt_id == attempt.id,
            existing_turn.raw_audio_id == attempt.raw_audio_id,
            existing_turn.asr_text == attempt.asr_text,
            existing_turn.asr_confidence == attempt.asr_confidence,
            existing_turn.duration_seconds == attempt.duration_seconds,
            existing_turn.prompt_level == attempt.prompt_level,
            existing_turn.cue_type == attempt.cue_type,
            existing_turn.ai_answer_type == attempt.operational_answer_type,
            existing_turn.ai_score == attempt.operational_score,
            existing_turn.ai_needs_review == attempt.operational_needs_review,
            existing_turn.ai_judge_mode == attempt.judge_mode,
            existing_turn.judge_portrait_used is False,
        )
        if not all(exact_projection):
            _fail(
                "autopilot_terminal_evidence_conflict",
                "已有 TurnEvent 与 terminal attempt 权威证据冲突",
            )
        if existing_turn.id is None:  # pragma: no cover - persisted invariant
            _fail("autopilot_terminal_evidence_invalid", "TurnEvent 缺少主键")
        return TerminalEvidenceMaterialization(
            item_event_id=item.id,
            turn_event_id=existing_turn.id,
            replayed=True,
        )

    turn = TurnEvent(
        item_event_id=item.id,
        source_attempt_id=attempt.id,
        turn_seq=attempt.turn_seq,
        response_role=attempt.response_role,
        raw_audio_id=attempt.raw_audio_id,
        asr_text=attempt.asr_text,
        asr_confidence=attempt.asr_confidence,
        duration_seconds=attempt.duration_seconds,
        prompt_level=attempt.prompt_level,
        cue_type=attempt.cue_type,
        ai_answer_type=attempt.operational_answer_type,
        ai_score=attempt.operational_score,
        ai_needs_review=attempt.operational_needs_review,
        ai_judge_mode=attempt.judge_mode,
        judge_portrait_used=False,
        confirmed_response_text=None,
        reviewer_id=None,
        reviewed_score=None,
        score_locked=False,
        element_value=None,
    )
    db.add(turn)
    db.flush()
    if turn.id is None:  # pragma: no cover - flush invariant
        _fail("autopilot_terminal_evidence_invalid", "TurnEvent 未能获得主键")
    return TerminalEvidenceMaterialization(
        item_event_id=item.id,
        turn_event_id=turn.id,
        replayed=False,
    )


def materialize_terminal_attempt_evidence(
    db: Session,
    *,
    session_id: str,
    attempt_id: int,
    bank: content.ItemBank | None = None,
    protocol: dict | None = None,
    now: datetime | None = None,
) -> TerminalEvidenceMaterialization | None:
    """Atomically create/replay ItemEvent + unlocked TurnEvent for P0a.

    Only an operationally terminal attempt (success feedback or final answer
    disclosure) is materialized.  Intermediate failed attempts stay solely in the
    append-only attempt/interaction ledger.
    """
    if (not isinstance(attempt_id, int) or isinstance(attempt_id, bool)
            or attempt_id < 1):
        _fail("autopilot_input_invalid", "attempt_id 必须是正整数")
    observed_at = _utc_naive(now) if now is not None else _utc_now_naive()
    resolved_bank = bank or _default_bank()
    resolved_protocol = protocol or _default_protocol()
    state = db.exec(select(SessionAutopilotState).where(
        SessionAutopilotState.session_id == session_id,
    ).with_for_update()).first()
    if (
        state is None
        or state.scope_key != P0A_SCOPE_KEY
        or state.mode != "autonomous"
        or state.status != "processing_attempt"
        or state.current_command_id is None
    ):
        _fail("autopilot_attempt_not_current", "terminal attempt 已不是当前处理目标")
    record = db.exec(select(RuntimeCommand).where(
        RuntimeCommand.id == state.current_command_id,
        RuntimeCommand.session_id == session_id,
    ).with_for_update()).first()
    attempt = db.exec(select(AttemptEvent).where(
        AttemptEvent.id == attempt_id,
        AttemptEvent.session_id == session_id,
    ).with_for_update()).first()
    if attempt is None or record is None:
        _fail("autopilot_attempt_not_found", "terminal attempt 或录音命令不存在")
    gate = _require_gate(
        db,
        session_id,
        bank=resolved_bank,
        protocol=resolved_protocol,
        now=observed_at,
        position_item_id=record.item_id,
        position_turn_seq=record.turn_seq,
    )
    _require_completed_operational_attempt(
        db, attempt=attempt, record=record, selected=gate.selected)
    decision = _attempt_route_decision(
        db, attempt=attempt, record=record, selected=gate.selected)
    return _materialize_terminal_attempt_evidence(
        db,
        gate=gate,
        record=record,
        attempt=attempt,
        decision=decision,
        bank=resolved_bank,
    )


def _validate_existing_attempt_route(
    db: Session,
    *,
    state: SessionAutopilotState,
    record: RuntimeCommand,
    command: RuntimeCommand,
    decision: _AttemptRouteDecision,
    selected: _P0aContent,
    active_capability: PatientDeviceCapability,
) -> NextCommandProjection:
    if (state.mode != "autonomous" or state.status != "waiting_tts"
            or state.current_command_id != command.id
            or command.kind != "tts" or command.state not in {"pending", "started"}
            or command.scope_key != P0A_SCOPE_KEY
            or command.control_generation != state.control_generation
            or command.runner_generation != state.runner_generation
            or command.control_generation != record.control_generation
            or command.runner_generation != record.runner_generation
            or command.command_seq + 1 != state.next_command_seq
            or command.item_id != record.item_id
            or command.turn_seq != record.turn_seq
            or command.turn_key != record.turn_key
            or command.attempt_seq != decision.attempt_seq
            or command.prompt_level != decision.prompt_level
            or command.predecessor_command_id is not None
            or command.trigger_ack_idempotency_key is not None
            or command.expected_raw_audio_id is not None):
        _fail("autopilot_idempotency_conflict", "attempt 路由幂等事实不一致")
    try:
        payload = TtsCommandPayload.model_validate_json(command.payload_json)
    except (TypeError, ValueError) as exc:
        raise AutopilotServiceError(
            "autopilot_idempotency_conflict", "attempt 路由 TTS 载荷非法") from exc
    if (payload.purpose != decision.purpose
            or payload.speech_text != decision.speech_text
            or payload.response_path != decision.response_path):
        _fail("autopilot_idempotency_conflict", "attempt 路由的冻结话术已冲突")
    _validate_tts_semantics(command, payload, selected)
    return _project_command(
        db, command, selected, expected_capability=active_capability)


def route_completed_attempt(
    db: Session,
    *,
    session_id: str,
    attempt_id: int,
    bank: content.ItemBank | None = None,
    protocol: dict | None = None,
    now: datetime | None = None,
) -> RouteCompletedAttemptResult:
    """Stage exactly one frozen TTS command from a completed P0a attempt.

    This is a provider-free transaction participant.  It flushes the capture-proof,
    attempt, command and state-CAS chain together but never commits.  The caller must
    roll back on every :class:`AutopilotServiceError`.
    """
    if (not isinstance(attempt_id, int) or isinstance(attempt_id, bool)
            or attempt_id < 1):
        _fail("autopilot_input_invalid", "attempt_id 必须是正整数")
    observed_at = _utc_naive(now) if now is not None else _utc_now_naive()
    resolved_bank = bank or _default_bank()
    resolved_protocol = protocol or _default_protocol()
    # Shared control-path lock order starts with the state row.  The current record
    # is locked before any state claim or command issuance on the first route.
    state = db.exec(select(SessionAutopilotState).where(
        SessionAutopilotState.session_id == session_id,
    ).with_for_update()).first()
    if state is None or state.scope_key != P0A_SCOPE_KEY:
        _fail("autopilot_not_active", "当前场次没有 P0a 自动驾驶状态")

    current_record: RuntimeCommand | None = None
    if state.status == "processing_attempt" and state.current_command_id is not None:
        current_record = db.exec(select(RuntimeCommand).where(
            RuntimeCommand.id == state.current_command_id,
            RuntimeCommand.session_id == session_id,
        ).with_for_update()).first()
    attempt = db.exec(select(AttemptEvent).where(
        AttemptEvent.id == attempt_id,
        AttemptEvent.session_id == session_id,
    ).with_for_update()).first()
    if attempt is None:
        _fail("autopilot_attempt_not_found", "completed attempt 不存在")
    record = current_record
    if record is None:
        record = db.exec(select(RuntimeCommand).where(
            RuntimeCommand.session_id == session_id,
            RuntimeCommand.expected_raw_audio_id == attempt.raw_audio_id,
        ).with_for_update()).first()
    if record is None:
        _fail("autopilot_attempt_mismatch", "attempt 找不到对应录音命令")

    gate = _require_gate(
        db,
        session_id,
        bank=resolved_bank,
        protocol=resolved_protocol,
        now=observed_at,
        position_item_id=record.item_id,
        position_turn_seq=record.turn_seq,
    )

    _require_completed_operational_attempt(
        db, attempt=attempt, record=record, selected=gate.selected)
    decision = _attempt_route_decision(
        db, attempt=attempt, record=record, selected=gate.selected)
    _materialize_terminal_attempt_evidence(
        db,
        gate=gate,
        record=record,
        attempt=attempt,
        decision=decision,
        bank=resolved_bank,
    )
    active_capability = _lock_active_attempt_route_device(
        db,
        session_id=session_id,
        expected=gate.active_capability,
        now=observed_at,
    )
    route_key = _derived_key(
        "cmd-attempt", session_id, record.id, attempt.id)
    existing = db.exec(select(RuntimeCommand).where(
        RuntimeCommand.session_id == session_id,
        RuntimeCommand.idempotency_key == route_key,
    ).with_for_update()).first()
    if existing is not None:
        projection = _validate_existing_attempt_route(
            db,
            state=state,
            record=record,
            command=existing,
            decision=decision,
            selected=gate.selected,
            active_capability=active_capability,
        )
        return RouteCompletedAttemptResult(
            attempt_id=attempt_id,
            replayed=True,
            state_revision=state.revision,
            command=projection,
        )

    if (state.mode != "autonomous" or state.status != "processing_attempt"
            or state.current_command_id != record.id
            or record.kind != "record" or record.state != "succeeded"
            or record.control_generation != state.control_generation
            or record.runner_generation != state.runner_generation):
        _fail("autopilot_attempt_not_current", "completed attempt 已不是当前处理目标")
    try:
        proof = autopilot_ledger.verify_record_capture_for_attempt(db, record.id)
    except autopilot_ledger.AutopilotProofError as exc:
        raise AutopilotServiceError(
            "autopilot_record_capture_invalid", "attempt 路由前录音采集证据复核失败") from exc
    if (proof.session_id != attempt.session_id
            or proof.raw_audio_id != attempt.raw_audio_id
            or proof.item_id != attempt.item_id
            or proof.turn_seq != attempt.turn_seq
            or proof.attempt_seq != attempt.attempt_seq
            or proof.prompt_level != attempt.prompt_level):
        _fail("autopilot_attempt_mismatch", "attempt 与 capture proof 不精确匹配")

    owner = "attempt-route-" + autopilot_ledger.new_lease_owner()
    if not autopilot_ledger.try_recover_processing_attempt_state(
        db,
        session_id,
        current_record_command_id=record.id,
        owner=owner,
        expected_revision=state.revision,
        expected_control_generation=state.control_generation,
        expected_runner_generation=state.runner_generation,
        now=observed_at,
    ):
        _fail("autopilot_attempt_route_cas_conflict", "attempt 路由状态 CAS 已失效")
    db.expire(state)
    claimed_state = db.get(SessionAutopilotState, session_id)
    if claimed_state is None:  # pragma: no cover - locked-row invariant
        _fail("autopilot_state_invalid", "自动驾驶状态已消失")
    try:
        claim = autopilot_ledger.claim_from_autopilot_state(claimed_state)
    except ValueError as exc:  # pragma: no cover - helper postcondition
        raise AutopilotServiceError(
            "autopilot_attempt_route_cas_conflict", "attempt 路由租约事实不完整") from exc

    payload_fields: dict[str, object] = {
        "speech_key": f"p0a.{decision.purpose}.{claimed_state.next_command_seq}",
        "speech_text": decision.speech_text,
        "purpose": decision.purpose,
        "item_id": record.item_id,
        "turn_seq": record.turn_seq,
        "cue_level": decision.prompt_level,
    }
    if decision.response_path is not None:
        payload_fields["response_path"] = decision.response_path
    payload = TtsCommandPayload.model_validate(payload_fields)
    command = RuntimeCommand(
        idempotency_key=route_key,
        session_id=session_id,
        command_seq=claimed_state.next_command_seq,
        item_id=record.item_id,
        turn_seq=record.turn_seq,
        turn_key=record.turn_key,
        attempt_seq=decision.attempt_seq,
        prompt_level=decision.prompt_level,
        **_command_definition_fields(gate.selected),
        scope_key=P0A_SCOPE_KEY,
        control_generation=claim.control_generation,
        runner_generation=claim.runner_generation,
        issued_capability_token_hash=active_capability.token_hash,
        issued_device_id_hash=active_capability.device_id_hash,
        issued_at=observed_at,
        kind="tts",
        state="pending",
        payload_json=payload.model_dump_json(exclude_none=True),
        created_at=observed_at,
        updated_at=observed_at,
    )
    db.add(command)
    db.flush()
    if command.id is None:  # pragma: no cover - SQLAlchemy invariant
        _fail("autopilot_state_invalid", "attempt 路由 TTS 命令未能持久化")
    if not autopilot_ledger.fenced_autopilot_update(
        db,
        claim,
        values={
            "status": "waiting_tts",
            "current_command_id": command.id,
            "next_command_seq": claimed_state.next_command_seq + 1,
            "last_error_code": None,
        },
        release_lease=True,
        now=observed_at,
    ):
        _fail("autopilot_attempt_route_cas_conflict", "attempt 路由发行 CAS 已失效")
    db.flush()
    db.expire(claimed_state)
    final_state = db.get(SessionAutopilotState, session_id)
    if final_state is None:  # pragma: no cover - locked-row invariant
        _fail("autopilot_state_invalid", "自动驾驶状态已消失")
    projection = _project_command(
        db, command, gate.selected, expected_capability=active_capability)
    return RouteCompletedAttemptResult(
        attempt_id=attempt_id,
        replayed=False,
        state_revision=final_state.revision,
        command=projection,
    )


def _validated_device_ack(ack: AutopilotAckIn) -> AutopilotAckIn:
    """Re-run validation even for an in-process ``model_construct`` instance."""
    if not isinstance(ack, AutopilotAckIn):
        _fail("autopilot_ack_invalid", "设备回执不符合封闭契约")
    try:
        return AutopilotAckIn.model_validate(ack.model_dump(mode="python"))
    except (TypeError, ValueError, ValidationError) as exc:
        raise AutopilotServiceError(
            "autopilot_ack_invalid", "设备回执不符合封闭契约") from exc


def _ack_candidate(
    *,
    command: RuntimeCommand,
    capability: PatientDeviceCapability,
    ack: AutopilotAckIn,
    received_at: datetime,
) -> RuntimeCommandAck:
    if command.id is None:  # pragma: no cover - persisted-query invariant
        _fail("autopilot_ack_rejected", "设备回执无法绑定当前命令")
    try:
        payload_json = autopilot_ledger.encode_ack_payload(
            ack.ack_type, ack.event_payload())
    except (TypeError, ValueError, AssertionError) as exc:
        raise AutopilotServiceError(
            "autopilot_ack_invalid", "设备回执事实无法安全编码") from exc
    return RuntimeCommandAck(
        command_id=command.id,
        idempotency_key=ack.idempotency_key,
        session_id=command.session_id,
        ack_type=ack.ack_type,
        command_revision=ack.command_revision,
        control_generation=ack.control_generation,
        runner_generation=ack.runner_generation,
        device_event_seq=ack.device_event_seq,
        device_id_hash=capability.device_id_hash,
        capability_token_hash=capability.token_hash,
        payload_json=payload_json,
        receipt_server_seq=ack.receipt_server_seq,
        raw_audio_id=ack.raw_audio_id,
        checksum=ack.checksum,
        byte_count=ack.byte_count,
        duration_seconds=ack.duration_seconds,
        received_at=received_at,
    )


def _ack_result(
    *,
    ack: AutopilotAckIn,
    command: RuntimeCommand,
    state: SessionAutopilotState,
    replayed: bool,
    next_command: NextCommandProjection | None = None,
) -> ApplyDeviceAckResult:
    return ApplyDeviceAckResult(
        ack_idempotency_key=ack.idempotency_key,
        ack_type=ack.ack_type,
        replayed=replayed,
        command_state=command.state,
        command_revision=command.revision,
        status=state.status,
        state_revision=state.revision,
        command=next_command,
    )


def _release_started_ack_lease(
    db: Session,
    *,
    command: RuntimeCommand,
    owner: str,
    expected_revision: int,
    now: datetime,
) -> None:
    """A receipt handler must not strand a nonterminal command behind its lease."""
    result = db.execute(
        update(RuntimeCommand)
        .where(
            RuntimeCommand.id == command.id,
            RuntimeCommand.session_id == command.session_id,
            RuntimeCommand.state == "started",
            RuntimeCommand.revision == expected_revision,
            RuntimeCommand.lease_owner == owner,
            RuntimeCommand.lease_expires_at > now,
        )
        .values(lease_owner=None, lease_expires_at=None, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        _fail("autopilot_ack_cas_conflict", "设备回执生命周期 CAS 已失效")


def _preflight_record_capture(
    db: Session,
    *,
    command: RuntimeCommand,
    ack: AutopilotAckIn,
    gate: _P0aGate,
    observed_at: datetime,
) -> None:
    """Prove the complete capture tuple before inserting its terminal ACK.

    The ledger verifier is intentionally run again after the terminal state move.
    This preflight prevents a missing FK target or contradictory upload from ever
    reaching ACK flush/CAS, while the post-transition proof guards the exact
    succeeded + processing_attempt chain.
    """
    if (ack.raw_audio_id is None or ack.receipt_server_seq is None
            or ack.checksum is None or ack.byte_count is None
            or ack.duration_seconds is None):
        _fail("autopilot_record_capture_invalid", "收麦回执缺少完整采集事实")
    receipt = db.exec(select(AudioCaptureReceipt).where(
        AudioCaptureReceipt.server_seq == ack.receipt_server_seq,
    ).with_for_update()).first()
    audio = db.exec(select(AudioAssetRow).where(
        AudioAssetRow.raw_audio_id == ack.raw_audio_id,
    ).with_for_update()).first()
    if receipt is None or audio is None:
        _fail("autopilot_record_capture_invalid", "服务端采集收据或录音原件不存在")
    try:
        payload = RecordCommandPayload.model_validate_json(command.payload_json)
    except (TypeError, ValueError) as exc:
        raise AutopilotServiceError(
            "autopilot_record_capture_invalid", "录音命令绑定事实非法") from exc

    tuple_matches = (
        command.kind == "record",
        command.expected_raw_audio_id == ack.raw_audio_id,
        receipt.server_seq == ack.receipt_server_seq,
        receipt.session_id == command.session_id,
        receipt.turn_key == command.turn_key,
        receipt.raw_audio_id == ack.raw_audio_id,
        receipt.checksum == ack.checksum,
        receipt.byte_count == ack.byte_count,
        receipt.duration_seconds == ack.duration_seconds,
        receipt.received_at >= command.issued_at,
        receipt.received_at <= observed_at,
        receipt.data_classification == gate.train_session.data_classification,
        receipt.is_simulation == gate.train_session.is_simulation,
        audio.session_id == command.session_id,
        audio.turn_key == command.turn_key,
        audio.raw_audio_id == ack.raw_audio_id,
        audio.checksum == ack.checksum,
        audio.byte_count == ack.byte_count,
        audio.uploaded_at is not None,
        audio.status == AudioStatus.recorded,
        not audio.withdrawn,
        not audio.delete_gate_passed,
        audio.data_classification == receipt.data_classification,
        audio.is_simulation == receipt.is_simulation,
        audio.contains_direct_identifier == receipt.contains_direct_identifier,
        payload.raw_audio_id == command.expected_raw_audio_id,
        payload.turn_key == command.turn_key,
        payload.item_id == command.item_id,
        payload.turn_seq == command.turn_seq,
        payload.cue_level == command.prompt_level,
        payload.contains_direct_identifier == receipt.contains_direct_identifier,
        ack.duration_seconds <= payload.max_duration_seconds,
        gate.runtime_state.status == "active",
        not bool((gate.patient.withdrawal_status or "").strip()),
    )
    if not all(tuple_matches):
        _fail("autopilot_record_capture_invalid", "录音回执与服务端采集证据不一致")


def apply_device_ack(
    db: Session,
    *,
    session_id: str,
    command_key: str,
    capability_token_hash: str,
    ack: AutopilotAckIn,
    bank: content.ItemBank | None = None,
    protocol: dict | None = None,
    now: datetime | None = None,
) -> ApplyDeviceAckResult:
    """Apply one untrusted device fact inside the caller's transaction.

    Lock order is always state -> command -> capability.  Exact immutable replay
    is classified before any current-generation, active-device or sequence side
    effect.  Every new ACK, lifecycle CAS and follow-up route/state effect is then
    flushed together; this function never commits.
    """
    command_key = _validate_idempotency_key(command_key, "command_key")
    if not isinstance(capability_token_hash, str) or not capability_token_hash:
        _fail("autopilot_ack_invalid", "设备能力摘要格式非法")
    ack = _validated_device_ack(ack)
    observed_at = _utc_naive(now) if now is not None else _utc_now_naive()

    # Deliberate global lock order for concurrent ACK/control paths.
    state = db.exec(select(SessionAutopilotState).where(
        SessionAutopilotState.session_id == session_id,
    ).with_for_update()).first()
    command = db.exec(select(RuntimeCommand).where(
        RuntimeCommand.session_id == session_id,
        RuntimeCommand.idempotency_key == command_key,
    ).with_for_update()).first()
    capability = db.exec(select(PatientDeviceCapability).where(
        PatientDeviceCapability.token_hash == capability_token_hash,
        PatientDeviceCapability.session_id == session_id,
    ).with_for_update()).first()
    if state is None or command is None or capability is None:
        _fail("autopilot_ack_rejected", "设备回执无法绑定当前命令与设备")

    # Exact transport replay remains side-effect free, but it may not revive a
    # command whose Session/content/protocol contract has drifted in place.
    resolved_bank = bank or _default_bank()
    resolved_protocol = protocol or _default_protocol()
    train_session = db.get(TrainSession, session_id)
    if train_session is None:
        _fail("autopilot_session_unavailable", "场次不存在或不可用于 P0a")
    _require_plan_session_binding(db, train_session)
    replay_selected = _select_p0a_content(
        train_session,
        resolved_bank,
        resolved_protocol,
        item_id=command.item_id,
        turn_seq=command.turn_seq,
    )
    _validate_command_identity(command, replay_selected)

    candidate = _ack_candidate(
        command=command,
        capability=capability,
        ack=ack,
        received_at=observed_at,
    )
    replay = autopilot_ledger.resolve_ack_replay(db, candidate)
    if replay == "identical":
        # Do not reveal a newer command to a capability that may now be rotated.
        return _ack_result(
            ack=ack, command=command, state=state, replayed=True)
    if replay == "conflict":
        _fail("autopilot_ack_conflict", "设备回执幂等事实或事件序号冲突")

    gate = _require_gate(
        db,
        session_id,
        bank=resolved_bank,
        protocol=resolved_protocol,
        now=observed_at,
        expected_token_hash=capability_token_hash,
        position_item_id=command.item_id,
        position_turn_seq=command.turn_seq,
    )
    if (gate.active_capability.token_hash != capability.token_hash
            or gate.active_capability.device_id_hash != capability.device_id_hash):
        _fail("autopilot_command_device_rotated", "当前命令发行设备已换绑")
    if (state.scope_key != P0A_SCOPE_KEY or state.mode != "autonomous"
            or state.current_command_id != command.id):
        _fail("autopilot_command_not_current", "设备回执命令已不是当前自主命令")
    expected_status = "waiting_tts" if command.kind == "tts" else "waiting_recording"
    if state.status != expected_status:
        _fail("autopilot_command_not_current", "自动驾驶状态不再等待此设备事实")
    if (command.scope_key != state.scope_key
            or command.control_generation != state.control_generation
            or command.runner_generation != state.runner_generation
            or ack.control_generation != command.control_generation
            or ack.runner_generation != command.runner_generation):
        _fail("autopilot_ack_stale", "设备回执 generation 已失效")
    if ack.command_revision != command.revision:
        _fail("autopilot_ack_stale", "设备回执 command revision 已失效")
    if (command.issued_capability_token_hash != capability.token_hash
            or command.issued_device_id_hash != capability.device_id_hash
            or command.issued_at > observed_at):
        _fail("autopilot_command_device_rotated", "设备不是当前命令的发行设备")

    # This proves frozen item/purpose/record provenance before consuming a device
    # sequence number.  It also rejects a valid allowlisted line under the wrong
    # semantic purpose.
    _project_command(
        db, command, gate.selected, expected_capability=gate.active_capability)
    try:
        transition = transition_for_ack(
            command.kind, command.state, ack.ack_type)  # type: ignore[arg-type]
    except ValueError as exc:
        raise AutopilotServiceError(
            "autopilot_ack_transition_invalid", "设备回执不符合当前命令生命周期") from exc

    if ack.ack_type == "record_stopped":
        _preflight_record_capture(
            db,
            command=command,
            ack=ack,
            gate=gate,
            observed_at=observed_at,
        )

    lease_owner = "device-ack-" + autopilot_ledger.new_lease_owner()
    if command.id is None or not autopilot_ledger.try_claim_runtime_command(
        db,
        command.id,
        owner=lease_owner,
        expected_revision=command.revision,
        control_generation=command.control_generation,
        runner_generation=command.runner_generation,
        now=observed_at,
    ):
        _fail("autopilot_ack_cas_conflict", "当前命令正被其他生命周期操作占用")
    if not autopilot_ledger.try_advance_device_autopilot_event_seq(
        db,
        capability_token_hash=capability.token_hash,
        session_id=session_id,
        device_id_hash=capability.device_id_hash,
        candidate_seq=ack.device_event_seq,
        now=observed_at,
    ):
        _fail("autopilot_ack_sequence_invalid", "设备事件序号乱序或能力已失效")

    db.expire(command)
    command = db.get(RuntimeCommand, command.id)
    if command is None:  # pragma: no cover - locked-row invariant
        _fail("autopilot_ack_cas_conflict", "当前命令已消失")
    claim = autopilot_ledger.claim_from_runtime_command(command)
    db.add(candidate)
    db.flush()
    transition_values = (
        {"result_json": candidate.payload_json}
        if transition.command_status == "failed" else None
    )
    if not autopilot_ledger.fenced_command_transition(
        db,
        claim,
        expected_state=command.state,  # type: ignore[arg-type]
        next_state=transition.command_status,
        terminal_ack=(
            candidate if transition.command_status == "succeeded" else None),
        values=transition_values,
        now=observed_at,
    ):
        _fail("autopilot_ack_cas_conflict", "设备回执生命周期 CAS 已失效")

    if transition.command_status == "started":
        _release_started_ack_lease(
            db,
            command=command,
            owner=lease_owner,
            expected_revision=claim.revision + 1,
            now=observed_at,
        )
    db.expire(command)
    command = db.get(RuntimeCommand, claim.command_id)
    if command is None:  # pragma: no cover - locked-row invariant
        _fail("autopilot_ack_cas_conflict", "当前命令已消失")

    if transition.effect == "route_after_tts":
        routed = route_tts_ended(
            db,
            session_id=session_id,
            command_key=command_key,
            ack_idempotency_key=ack.idempotency_key,
            bank=resolved_bank,
            protocol=resolved_protocol,
            now=observed_at,
        )
        db.expire(state)
        state = db.get(SessionAutopilotState, session_id)
        if state is None:  # pragma: no cover - locked-row invariant
            _fail("autopilot_state_invalid", "自动驾驶状态已消失")
        return _ack_result(
            ack=ack,
            command=command,
            state=state,
            replayed=False,
            next_command=routed.command,
        )

    if transition.effect == "process_attempt":
        state.status = "processing_attempt"
        state.current_command_id = command.id
        state.revision += 1
        state.last_error_code = None
        state.updated_at = observed_at
        db.add(state)
        db.flush()
        try:
            autopilot_ledger.verify_record_capture_for_attempt(db, command.id)
        except autopilot_ledger.AutopilotProofError as exc:
            raise AutopilotServiceError(
                "autopilot_record_capture_invalid",
                "录音回执与服务端采集证据不一致",
            ) from exc
        return _ack_result(
            ack=ack, command=command, state=state, replayed=False)

    if transition.effect == "pause":
        error_code = ack.error_code
        if error_code is None:  # pragma: no cover - contract invariant
            _fail("autopilot_ack_invalid", "失败回执缺少机器错误码")
        from_status = state.status
        state.status = "paused"
        state.current_command_id = None
        state.revision += 1
        state.last_error_code = error_code
        state.lease_owner = None
        state.lease_acquired_at = None
        state.lease_expires_at = None
        state.updated_at = observed_at
        db.add(state)
        db.add(AutopilotControlEvent(
            idempotency_key=_derived_key(
                "device-failure", session_id, command.id, ack.idempotency_key),
            session_id=session_id,
            event_seq=_next_control_event_seq(db, session_id),
            event_type="failure",
            scope_key=P0A_SCOPE_KEY,
            control_generation=state.control_generation,
            runner_generation=state.runner_generation,
            command_id=command.id,
            actor_type="device",
            reason_code=error_code,
            from_mode="autonomous",
            to_mode="autonomous",
            from_status=from_status,
            to_status="paused",
            payload_json=autopilot_ledger.encode_control_event_payload(
                "failure", {"error_code": error_code, "source": "device_ack"}),
            created_at=observed_at,
        ))
        db.flush()
        return _ack_result(
            ack=ack, command=command, state=state, replayed=False)

    # started: command is still current, but its short internal CAS lease has been
    # released so an immediate terminal device fact can advance it.
    next_command = _project_command(
        db, command, gate.selected, expected_capability=gate.active_capability)
    return _ack_result(
        ack=ack,
        command=command,
        state=state,
        replayed=False,
        next_command=next_command,
    )
