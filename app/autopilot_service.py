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
import math
import os
import re
import secrets
from typing import Literal

from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      model_serializer)
from sqlalchemy import or_, update
from sqlmodel import Session, select

from . import (autopilot_ledger, autopilot_plan_profiles, autopilot_positions,
               cloud_processing, content, evidence_ledger, patient_presentation,
               repeat_intent, runtime)
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
    AttemptCaptureProcessing,
    AttemptEvent,
    AutopilotControlEvent,
    AutopilotRepeatRequest,
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
# Real research sessions ride the same frozen engine but through their own
# explicit deployment switch.  The two simulation switches above keep their
# exact historical semantics; neither channel ever implies the other.
REAL_SESSIONS_ENV = "ENABLE_AUTOPILOT_REAL_SESSIONS"
P0A_SOURCE = "p0a_domain_service"
# Interaction-package consumption (autopilot-interaction.v1): the one gap /
# failure code for "this week's package is missing, unbound or uncovering",
# kept identical to the positions-module gap code so pauses and admission
# context stay precisely attributable.
INTERACTION_PACKAGE_GAP_CODE = "interaction_package_unavailable"
# AttemptEvent.cue_type for the one package-driven level-1 retry prompt.  The
# interaction packages freeze prompt text per turn but no bank cue_type label
# exists for them; this closed literal keeps the evidence column honest without
# inventing per-turn taxonomy the source never defined.
INTERACTION_CUE_TYPE = "交互提示"
# Frozen level-1 branch bookkeeping between the closed ResponsePath vocabulary
# and the interaction package's branch categories.  "close" (相关不准确) is the
# partial-concept branch, "unknown" (未提及/不知道) the wrong branch; silence is
# silence.  "full" deliberately has no path: a hit never issues a cue.
_INTERACTION_PATH_TO_CATEGORY: dict[str, str] = {
    "close": "partial", "unknown": "wrong", "silence": "silence",
}
_INTERACTION_CATEGORY_TO_PATH: dict[str, ResponsePath] = {
    category: path for path, category in _INTERACTION_PATH_TO_CATEGORY.items()
}

_TRUE_VALUES = frozenset({"1", "true", "yes"})
_DENIED_CONSENT = frozenset({
    "未同意", "已撤回", "拒绝", "不同意",
    "denied", "withdrawn", "refused", "declined", "rejected",
})
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
# Closed identity of the explicit-repeat safe pause.  It is deliberately not a
# technical failure code and must never be counted as one.
REPEAT_PAUSE_SOURCE = "repeat_intent_protocol"
REPEAT_LIMIT_REASON_CODE = "explicit_repeat_limit"
# Closed identity of the one terminal action a pre-protocol capture may still
# receive.  It *is* in _DRAINABLE_PAUSE_SOURCES, for one narrow reason: the
# patient device must be able to close its own screen.  Draining never resumes
# anything, and a researcher takeover afterwards flips the control plane to
# manual — at which point every manual continuation entry is refused, because
# _ensure_manual_plane_writable resolves the session's frozen repeat binding and
# this session never froze one.  So the scope finishes its single outstanding
# recording and then stops for good, without stranding the patient screen.
LEGACY_REPEAT_RECOVERY_REASON_CODE = "legacy_repeat_recovery_complete"
LEGACY_REPEAT_RECOVERY_SOURCE = "legacy_repeat_recovery"
# The frozen P0a prompt-level -> cue-type mapping, uniform across every
# approved single-element item.  A record command only ever exists for levels
# 0..2 (level 3 tells the answer and never records), so this is the complete
# contract, and stating it here keeps the provider-free terminal closure
# independent of whatever the item bank file says today.
LEGACY_CUE_TYPE_BY_PROMPT_LEVEL: dict[int, str | None] = {
    0: None, 1: "语义", 2: "排除式",
}
# The exact operational judgement contract, as _classify_operational emits it.
# Non-null/type checks alone would accept an invented answer type with an
# invented score, so both branches are closed against their real output shape.
LEGACY_JUDGEMENT_SCORE_BY_ANSWER_TYPE: dict[str, float] = {
    "正确": 1.0,
    "部分正确": 0.5,
    "上位词或相关词": 0.5,
    "偏题": 0.0,
    "重复": 0.0,
    "未识别": 0.0,
    "沉默": 0.0,
    "拒答": 0.0,
}
# rule_judge emits exactly these (answer_type, matched_on, needs_review) triples,
# mapped to what ``contains_target`` can be alongside them.  ``None`` means the
# branch genuinely leaves it free: a dialect synonym, a related term or a
# substring match may or may not also contain the target, and constraining
# those would need the current item bank, which the terminal readback must not
# depend on.  A target/acceptable hit always contains it; silence never does.
LEGACY_RULE_JUDGEMENTS: dict[tuple[str, str | None, bool], bool | None] = {
    ("正确", "target", False): True,
    ("正确", "acceptable", False): True,
    ("正确", "dialect", True): None,
    ("上位词或相关词", "upper", True): None,
    ("部分正确", "substring", True): None,
    ("未识别", None, True): None,
    ("拒答", "refusal", True): None,
    ("沉默", "silence", True): False,
}
# The Qwen judgement parser bounds a reason at 500 characters.
LEGACY_MAX_JUDGE_REASON_CHARS = 500
# 沉默/拒答 are interaction states produced only by the deterministic rules;
# the LLM branch can never emit them.
LEGACY_LLM_ANSWER_TYPES: frozenset[str] = frozenset({
    "正确", "部分正确", "上位词或相关词", "偏题", "重复", "未识别",
})
_EXTERNAL_FENCE_SOURCES = frozenset({
    "patient_rec_failure",
    "patient_requested_pause",
    "session_abort",
    "cloud_processing_consent_revoked",
    "subject_withdrawal",
})
_EXTERNAL_FENCE_ACTOR = {
    "patient_rec_failure": "device",
    "patient_requested_pause": "device",
    "session_abort": "researcher",
    "cloud_processing_consent_revoked": "system",
    "subject_withdrawal": "researcher",
}
_DRAINABLE_PAUSE_SOURCES = _EXTERNAL_FENCE_SOURCES | frozenset({
    "protocol_position_gap",
    # An explicit-repeat limit pause is a protocol decision, not a technical
    # failure; it still leaves a safely drainable paused scope behind.
    REPEAT_PAUSE_SOURCE,
    # Same shape for the legacy recovery stop: the patient device must still be
    # able to close out its screen.  Draining ends a device command; it never
    # resumes the scope, and resume itself stays refused for an unbound session.
    LEGACY_REPEAT_RECOVERY_SOURCE,
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

    @model_serializer(mode="wrap")
    def _omit_absent_response_path(self, handler):
        """缺席的分支证据必须**整个键消失**，不能序列化成 response_path:null。

        设备端严格 parser 按闭集校验字段集合，多一个 null 键就拒绝整条投影，老人端
        会停在"自动流程暂时无法确认"。省略挂在模型上而不是某条路由上：这个投影既走
        GET /autopilot/next，也嵌在 ACK 收据里返回，两个出口必须一致；而路由级
        exclude_none 又会顺手吃掉收据顶层那些必须显式保留的 null。
        用 model_serializer 而不是 Field(exclude_if=...)，是因为后者要 Pydantic 2.12+，
        而 requirements.txt 声明的下限是 2.7。
        """
        data = handler(self)
        if data.get("response_path", "") is None:
            data.pop("response_path")
        return data


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
    takeover_ready: bool
    current_command_kind: Literal["tts", "record"] | None = None
    # 只读展示投影:自动驾驶当前/最后触及的计划位置(当前命令,否则最后一条已
    # 签发命令)。不参与任何状态机/CAS 判定;disabled 收据恒为 null。
    position_item_id: str | None = Field(default=None, min_length=1, max_length=128)
    position_turn_seq: int | None = Field(default=None, ge=1)
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
    """The routed outcome of one terminal operational attempt.

    Speaking outcomes stay ``waiting_tts`` with the staged command.  The
    interaction hit path advances silently: the command is the *next
    position's* question, or — at a plan boundary — the scope pauses at a
    content gap or completes, with no command at all.
    """

    model_config = ConfigDict(extra="forbid")

    scope_key: Literal["p0a_sim_first_single_v1"] = P0A_SCOPE_KEY
    status: Literal["waiting_tts", "paused", "scope_completed"] = "waiting_tts"
    attempt_id: int = Field(ge=1)
    replayed: bool
    state_revision: int = Field(ge=0)
    command: NextCommandProjection | None


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


class RouteExplicitRepeatResult(BaseModel):
    """One committed explicit-repeat decision; carries no patient transcript."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["replayed", "limit_paused"]
    repeat_ordinal: int = Field(ge=1, le=2)
    state_revision: int = Field(ge=0)


@dataclass(frozen=True)
class _DefinitionBinding:
    item_bank_version_id: str
    item_bank_definition_digest: str
    autopilot_protocol_version_id: str
    autopilot_protocol_definition_digest: str


@dataclass(frozen=True)
class _ResolvedSessionTraversal:
    """The exact ordered plan used by one session in this process.

    Paired-null sessions intentionally keep the historical caller-provided bank
    behaviour used by isolated fixtures.  Only a paired-set profile needs the
    stricter canonical-parent/profile registry validation.
    """

    session_plan: runtime.SessionPlan
    positions: tuple[autopilot_positions.ProtocolPosition, ...]
    completion_scope: Literal["canonical_full_source", "demo_plan_only"]


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

    @property
    def task_type(self) -> str:
        return "单要素"


@dataclass(frozen=True)
class _InteractionBranch:
    """One resolved (kind, spoken text) branch of a frozen interaction turn."""

    kind: Literal["advance_silent", "speak_then_advance", "speak_then_rerecord"]
    text: str | None


@dataclass(frozen=True)
class _InteractionContent:
    """One executable double/multi-element turn frozen by the week's package.

    Duck-type compatible with :class:`_P0aContent` for every shared consumer
    (identity binding, projection, allowlist, record duration); the naming
    ladder fields deliberately do not exist here, so any single-element code
    path reaching an interaction turn fails loudly instead of guessing.
    """

    item_bank_version_id: str
    item_bank_definition_digest: str
    autopilot_protocol_version_id: str
    autopilot_protocol_definition_digest: str
    item_index: int
    item_id: str
    turn_seq: int
    response_role: str
    task_type: str
    image_id: str
    question_text: str
    max_recordings: int
    branches: dict[str, _InteractionBranch]
    after_rerecord: dict[str, _InteractionBranch] | None
    target_word: str | None
    allowed_tts_lines: frozenset[str]
    max_duration_seconds: int

    @property
    def initial_prompt(self) -> str:
        """The position-opening question line (shared advance/start contract)."""
        return self.question_text


_SelectedContent = _P0aContent | _InteractionContent


@dataclass(frozen=True)
class _P0aGate:
    train_session: TrainSession
    patient: Patient
    runtime_state: SessionRuntimeState
    active_capability: PatientDeviceCapability
    selected: _P0aContent


@dataclass(frozen=True)
class _AttemptRouteDecision:
    """One judged-attempt outcome.  ``advance_silent`` is the interaction
    packages' hit path: the turn is terminal but no speech is issued — the
    route advances the frozen plan and directly stages the next question."""

    purpose: Literal["cue", "feedback", "tell_answer", "advance_silent"]
    speech_text: str | None
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


def real_sessions_enabled() -> bool:
    """Autopilot on real research sessions requires its own explicit switch."""
    return _enabled(REAL_SESSIONS_ENV)


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


def _resolved_profile_plan(
    train_session: TrainSession,
    bank: content.ItemBank,
    protocol: dict,
) -> _ResolvedSessionTraversal:
    """Resolve the one session-frozen traversal used by every P0a consumer."""
    version_id = train_session.autopilot_profile_version_id
    definition_digest = train_session.autopilot_profile_definition_digest
    if version_id is None and definition_digest is None:
        event_line = _enum_value(train_session.event_line)
        try:
            plan = runtime.build_session_plan(
                bank, train_session.week_no, event_line)
        except ValueError as exc:
            raise AutopilotServiceError(
                "autopilot_content_unavailable",
                "当前冻结训练计划不可用",
            ) from exc
        return _ResolvedSessionTraversal(
            session_plan=plan,
            positions=autopilot_positions.plan_positions(plan),
            completion_scope="canonical_full_source",
        )
    try:
        resolved = autopilot_plan_profiles.resolve_for_session(
            train_session, bank=bank, protocol=protocol)
    except autopilot_plan_profiles.PlanProfileError as exc:
        raise AutopilotServiceError(exc.code, exc.message) from exc
    return _ResolvedSessionTraversal(
        session_plan=resolved.session_plan,
        positions=resolved.positions,
        completion_scope=resolved.completion_scope,
    )


def session_repeat_protocol(
    train_session: TrainSession,
) -> repeat_intent.RepeatIntentProtocol:
    """Resolve the exact repeat definition this session was started against.

    A session predating the approved protocol carries no binding and is refused
    outright: server-owned automation may not run a session whose repeat
    semantics were never frozen, because a patient asking to hear the prompt
    again would silently be scored as an answer.  Such a session needs a newly
    created one, not a halfway opt-in.  The binding is always resolved through
    the historical registry, never through today's active version.
    """
    version_id = train_session.repeat_protocol_version_id
    digest = train_session.repeat_protocol_definition_digest
    if version_id is None or digest is None:
        _fail(
            "autopilot_repeat_binding_missing",
            "场次缺少冻结的重复请求协议绑定；旧场次必须新建后才能自动执行",
        )
    try:
        return repeat_intent.protocol_for_binding(version_id, digest)
    except content.FrozenContentUnavailable as exc:
        raise AutopilotServiceError(
            "autopilot_repeat_protocol_unavailable",
            "场次绑定的重复请求协议已不可用或内容已变化",
        ) from exc


def _require_command_repeat_binding(
    command: RuntimeCommand,
    train_session: TrainSession,
) -> None:
    """Every issued command must carry the session's exact repeat binding.

    A NULL, half-filled or drifted binding means the command cannot be
    interpreted under the session's frozen repeat semantics, so it is rejected
    before any projection, device action, ACK or capture admission rather than
    silently falling back to the pre-repeat answer flow.
    """
    session_repeat_protocol(train_session)
    if (command.repeat_protocol_version_id is None
            or command.repeat_protocol_definition_digest is None
            or command.repeat_protocol_version_id
            != train_session.repeat_protocol_version_id
            or command.repeat_protocol_definition_digest
            != train_session.repeat_protocol_definition_digest):
        _fail(
            "autopilot_command_repeat_binding_mismatch",
            "命令的重复请求协议绑定缺失或与场次不一致",
        )


def _command_repeat_binding_fields(
    train_session: TrainSession,
) -> dict[str, str | None]:
    return {
        "repeat_protocol_version_id": train_session.repeat_protocol_version_id,
        "repeat_protocol_definition_digest": (
            train_session.repeat_protocol_definition_digest),
    }


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
        or plan.repeat_protocol_version_id
        != train_session.repeat_protocol_version_id
        or plan.repeat_protocol_definition_digest
        != train_session.repeat_protocol_definition_digest
        or plan.autopilot_profile_version_id
        != train_session.autopilot_profile_version_id
        or plan.autopilot_profile_definition_digest
        != train_session.autopilot_profile_definition_digest
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


def _load_interaction_package(
    train_session: TrainSession,
    bank: content.ItemBank,
    protocol: dict,
) -> dict:
    """Load and bind this session-week's frozen interaction package, or fail.

    The byte-pinned loader and the bank/protocol binding gate both live in
    ``content``; every failure collapses to the one independent gap code so a
    missing, tampered or unbound package pauses with a precise attribution
    instead of degrading into the single-element protocol codes.
    """
    week_no = train_session.week_no
    try:
        package = content.load_autopilot_interaction_package(
            week_no, protocol=protocol)
    except content.FrozenContentUnavailable as exc:
        raise AutopilotServiceError(
            INTERACTION_PACKAGE_GAP_CODE,
            f"第{week_no}周自动交互数据包不可用：{exc}",
        ) from exc
    issues = content.validate_autopilot_interaction_package(
        package, bank, protocol)
    if issues:
        _fail(
            INTERACTION_PACKAGE_GAP_CODE,
            f"第{week_no}周自动交互数据包未通过题库/协议绑定校验："
            + "；".join(issues[:5]),
        )
    return package


def _interaction_package_or_none(
    train_session: TrainSession,
    bank: content.ItemBank,
    protocol: dict,
) -> dict | None:
    """Gap-accounting variant: an unavailable package is a gap, not an error."""
    try:
        return _load_interaction_package(train_session, bank, protocol)
    except AutopilotServiceError:
        return None


def _plan_has_interaction_positions(
    positions: tuple[autopilot_positions.ProtocolPosition, ...],
) -> bool:
    return any(
        position.task_type in {"双要素", "多要素"} for position in positions)


def _resolve_interaction_branch(
    branch_row: object,
    bank_item: dict,
    protocol: dict,
    *,
    where: str,
) -> _InteractionBranch:
    kind = branch_row.get("kind") if isinstance(branch_row, dict) else None
    if kind not in {"advance_silent", "speak_then_advance", "speak_then_rerecord"}:
        _fail("autopilot_content_incomplete", f"{where} 分支 kind 非法")
    speech = branch_row.get("speech")
    if kind == "advance_silent":
        if speech is not None:
            _fail("autopilot_content_incomplete", f"{where} 静默推进分支不得携带话术")
        return _InteractionBranch(kind=kind, text=None)
    text = content.interaction_speech_text(speech, bank_item, protocol)
    if not isinstance(text, str) or not text.strip():
        _fail("autopilot_content_incomplete", f"{where} 分支话术引用解析不出文本")
    return _InteractionBranch(kind=kind, text=text.strip())


def _select_interaction_content(
    train_session: TrainSession,
    bank: content.ItemBank,
    protocol: dict,
    *,
    position: autopilot_positions.ProtocolPosition,
    package: dict,
    binding: _DefinitionBinding,
) -> _InteractionContent:
    rows = {
        "双要素": bank.double_element,
        "多要素": bank.multi_element,
    }[position.task_type]
    matches = [row for row in rows if row.get("item_id") == position.item_id]
    if len(matches) != 1:
        _fail("autopilot_content_incomplete", "训练计划与题库位置不一致")
    raw = matches[0]
    image_id = _required_text(raw.get("image_id"), "image_id")
    target_word: str | None = None
    if position.task_type == "双要素" and position.response_role in {
        "左命名", "右命名",
    }:
        target_key = (
            "left_word" if position.response_role == "左命名" else "right_word")
        target_word = _required_text(raw.get(target_key), target_key)
    else:
        rubric = content.operational_rubric_for(
            bank, position.item_id, position.response_role)
        if rubric is None:
            _fail(
                "operational_rubric_unavailable",
                f"{position.position_key}:{position.response_role} "
                "缺版本化 operational rubric",
            )
    turn = autopilot_positions.interaction_turn(package, position)
    if turn is None:
        _fail(
            INTERACTION_PACKAGE_GAP_CODE,
            f"{position.position_key}:{position.response_role} "
            "不在本周自动交互数据包冻结环节内",
        )
    where = f"{position.position_key}:{position.response_role}"
    question_row = turn.get("question")
    question_text = _required_text(
        question_row.get("text") if isinstance(question_row, dict) else None,
        "question.text",
    )
    branch_rows = turn.get("branches")
    if (not isinstance(branch_rows, dict)
            or not {"full", "wrong", "silence"} <= set(branch_rows)
            or not set(branch_rows) <= {"full", "partial", "wrong", "silence"}):
        _fail("autopilot_content_incomplete", f"{where} 分支集合非法")
    branches = {
        key: _resolve_interaction_branch(
            row, raw, protocol, where=f"{where}.branches.{key}")
        for key, row in branch_rows.items()
    }
    if branches["full"].kind == "speak_then_rerecord":
        _fail("autopilot_content_incomplete", f"{where} full 分支不得要求重录")
    has_rerecord = any(
        branch.kind == "speak_then_rerecord" for branch in branches.values())
    after_rows = turn.get("after_rerecord")
    max_recordings = turn.get("max_recordings")
    if (max_recordings not in (1, 2)
            or has_rerecord != (after_rows is not None)
            or max_recordings != (2 if has_rerecord else 1)):
        _fail(
            "autopilot_content_incomplete",
            f"{where} 分支结构与 max_recordings/after_rerecord 不一致",
        )
    after_rerecord: dict[str, _InteractionBranch] | None = None
    if after_rows is not None:
        if not isinstance(after_rows, dict) or set(after_rows) != {
            "full", "otherwise",
        }:
            _fail("autopilot_content_incomplete", f"{where} after_rerecord 集合非法")
        after_rerecord = {
            key: _resolve_interaction_branch(
                row, raw, protocol, where=f"{where}.after_rerecord.{key}")
            for key, row in after_rows.items()
        }
        if any(branch.kind == "speak_then_rerecord"
               for branch in after_rerecord.values()):
            _fail(
                "autopilot_content_incomplete",
                f"{where} after_rerecord 分支必须终止本环节",
            )
    allowed = content.tts_allowlist(
        bank, autopilot_protocol=protocol, interaction_package=package)
    required_lines = {question_text}
    required_lines |= {
        branch.text for branch in branches.values() if branch.text}
    if after_rerecord is not None:
        required_lines |= {
            branch.text for branch in after_rerecord.values() if branch.text}
    if not required_lines.issubset(allowed):
        _fail(
            "autopilot_tts_not_allowlisted",
            f"{where} 话术未全部进入 TTS 白名单",
        )
    silence_seconds = protocol.get("silence_seconds")
    if not isinstance(silence_seconds, int) or isinstance(silence_seconds, bool):
        _fail("autopilot_protocol_invalid", "P0a 协议沉默阈值格式非法")
    return _InteractionContent(
        item_bank_version_id=binding.item_bank_version_id,
        item_bank_definition_digest=binding.item_bank_definition_digest,
        autopilot_protocol_version_id=binding.autopilot_protocol_version_id,
        autopilot_protocol_definition_digest=(
            binding.autopilot_protocol_definition_digest),
        item_index=position.item_index,
        item_id=position.item_id,
        turn_seq=position.turn_seq,
        response_role=position.response_role,
        task_type=position.task_type,
        image_id=image_id,
        question_text=question_text,
        max_recordings=max_recordings,
        branches=branches,
        after_rerecord=after_rerecord,
        target_word=target_word,
        allowed_tts_lines=allowed,
        max_duration_seconds=silence_seconds + 5,
    )


def _select_p0a_content(
    train_session: TrainSession,
    bank: content.ItemBank,
    protocol: dict,
    *,
    item_id: str | None = None,
    turn_seq: int | None = None,
    interaction_package: dict | None = None,
) -> _SelectedContent:
    binding = _require_session_definition_binding(train_session, bank, protocol)
    protocol_weeks = tuple(protocol["supported_training_weeks"])
    if (train_session.week_no not in bank.supported_training_weeks
            or train_session.week_no not in protocol_weeks):
        _fail(
            "autopilot_scope_unsupported",
            "自动执行只允许题库与自动化协议共同明确支持的训练周模拟场次",
        )
    resolved = _resolved_profile_plan(train_session, bank, protocol)
    positions = resolved.positions
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
    if position.task_type in {"双要素", "多要素"}:
        package = interaction_package
        if package is None:
            package = _load_interaction_package(train_session, bank, protocol)
        gap = autopilot_positions.readiness_gap(
            bank, position, interaction_package=package)
        if gap is not None:
            _fail(gap.code, gap.detail)
        return _select_interaction_content(
            train_session, bank, protocol,
            position=position, package=package, binding=binding)
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
    resolved = _resolved_profile_plan(train_session, bank, protocol)
    if resolved.completion_scope == "demo_plan_only":
        positions = resolved.positions
        if not positions:
            _fail("autopilot_content_incomplete", "自动驾驶冻结计划没有可执行位置")
        interaction_package = (
            _interaction_package_or_none(train_session, bank, protocol)
            if _plan_has_interaction_positions(positions) else None
        )
        gaps = tuple(
            gap
            for position in positions
            if (gap := autopilot_positions.readiness_gap(
                bank, position,
                interaction_package=interaction_package)) is not None
        )
        if gaps:
            first = gaps[0]
            _fail(
                "autopilot_plan_not_fully_supported",
                f"模拟演示计划仍有 {len(gaps)} 个位置不可执行；"
                f"首个缺口为 {first.position.position_key}:"
                f"{first.position.response_role}",
                context={
                    "unsupported_position_count": len(gaps),
                    "structured_unsupported_position_count": len(gaps),
                    "source_unstructured_position_count": 0,
                    "source_protocol_position_count": bank.meta.get(
                        "source_protocol_position_count"),
                    "resolved_position_count": len(positions),
                    "completion_scope": resolved.completion_scope,
                    "first_gap": {
                        "code": first.code,
                        "item_id": first.position.item_id,
                        "turn_seq": first.position.turn_seq,
                        "response_role": first.position.response_role,
                    },
                },
            )
        for position in positions:
            _select_p0a_content(
                train_session,
                bank,
                protocol,
                item_id=position.item_id,
                turn_seq=position.turn_seq,
                interaction_package=interaction_package,
            )
        return

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

    interaction_package = (
        _interaction_package_or_none(train_session, bank, protocol)
        if _plan_has_interaction_positions(positions) else None
    )
    gaps = tuple(
        gap
        for position in positions
        if (gap := autopilot_positions.readiness_gap(
            bank, position,
            interaction_package=interaction_package)) is not None
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
            interaction_package=interaction_package,
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
    if not p0a_feature_enabled() and not real_sessions_enabled():
        # 分类先行:真实研究场次缺的是 REAL_SESSIONS_ENV,双通道全关时报
        # p0a_disabled 会教人去开两个模拟开关。场次缺失/不可分类仍走旧码。
        gate_session = db.get(TrainSession, session_id)
        if (gate_session is not None
                and gate_session.is_simulation is False
                and gate_session.data_classification == "research"):
            _fail(
                "autopilot_real_sessions_disabled",
                f"真实研究场次自动带练必须显式启用 {REAL_SESSIONS_ENV}",
            )
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
    # Stated here rather than inside the digest helper: content selection is a
    # pure function of the frozen bank/protocol and is deliberately reusable by
    # the pre-protocol recovery verifier, which has no repeat binding to check.
    # Every modern write path reaches this gate or checks the command binding
    # directly, so the enforcement point is unchanged.
    session_repeat_protocol(train_session)
    _require_plan_session_binding(db, train_session)
    if train_session.is_simulation is True:
        if not p0a_feature_enabled():
            _fail(
                "autopilot_p0a_disabled",
                f"P0a 必须显式启用 {P0A_FEATURE_ENV} 和 {SIMULATION_DATA_ENV}",
            )
        if train_session.data_classification != "simulation":
            _fail(
                "autopilot_classification_invalid",
                "P0a 场次必须明确归类为 simulation")
    elif (train_session.is_simulation is False
            and train_session.data_classification == "research"):
        if not real_sessions_enabled():
            _fail(
                "autopilot_real_sessions_disabled",
                f"真实研究场次自动带练必须显式启用 {REAL_SESSIONS_ENV}",
            )
    else:
        _fail(
            "autopilot_classification_invalid",
            "场次 is_simulation 与 data_classification 组合不可证明")
    if (_enum_value(train_session.phase_type) != "正式训练"
            or _enum_value(train_session.event_line) != "正式训练"):
        _fail("autopilot_scope_unsupported", "P0a 只允许正式训练事件线")

    patient = db.get(Patient, train_session.patient_id)
    if train_session.is_simulation is True:
        if patient is None or patient.is_simulation_subject is not True:
            _fail("autopilot_simulation_subject_required", "P0a 必须绑定专用模拟受试者")
    elif patient is None or patient.is_simulation_subject is True:
        _fail(
            "autopilot_simulation_subject_forbidden",
            "模拟受试者档案不得进入真实研究场次自动带练")
    consent_status = (patient.consent_status or "").strip().casefold()
    if consent_status in _DENIED_CONSENT:
        _fail("autopilot_consent_denied", "受试者存在明确拒绝或撤回状态")
    if patient.recording_allowed is not True:
        _fail("autopilot_recording_not_allowed", "P0a 录音授权必须明确为 true")
    if (patient.withdrawal_status or "").strip():
        _fail("autopilot_subject_withdrawn", "已撤回受试者不能启动或继续 P0a")
    if train_session.is_simulation is False:
        policy = cloud_processing.current_policy()
        cloud_authorized = (
            policy.configured
            and patient.cloud_processing_allowed is True
            and patient.cloud_processing_provider_id == policy.provider_id
            and patient.cloud_processing_notice_version == policy.notice_version
            and patient.cloud_processing_consented_at is not None
            and patient.cloud_processing_revoked_at is None
        )
        if not cloud_authorized:
            _fail(
                "autopilot_cloud_processing_required",
                "AI 判分要用云端转写；请先在受试者档案完成云处理授权，再启动自动带练",
            )

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


def _interaction_tell_lines(selected: _InteractionContent) -> frozenset[str]:
    """The closed set of first-recording answer-disclosure lines for one turn."""
    return frozenset(
        branch.text
        for category, branch in selected.branches.items()
        if category != "full"
        and branch.kind == "speak_then_advance"
        and branch.text
    )


def _validate_interaction_tts_semantics(
    command: RuntimeCommand,
    payload: TtsCommandPayload,
    selected: _InteractionContent,
) -> None:
    """Shape-aware (purpose, prompt_level, attempt_seq) table for one frozen
    interaction turn.  Every legal cell is enumerated explicitly; anything
    outside the table is rejected, exactly like the single-element ladder:

      question    (0,1): the turn's frozen question line
      cue         (1,2): the source-selected rerecord branch, response_path-bound
      feedback    (0,1): branches.full speak_then_advance line
      feedback    (1,2): after_rerecord.full line, response_path-bound to the cue
      tell_answer (3,1): one non-full speak_then_advance branch line
      tell_answer (3,2): after_rerecord.otherwise line
    """
    stage = (command.prompt_level, command.attempt_seq)
    if payload.purpose == "question":
        if stage != (0, 1):
            _fail("autopilot_command_invalid", "question 只能是首次零级提问")
        expected_line = selected.question_text
    elif payload.purpose == "cue":
        if stage != (1, 2):
            _fail("autopilot_command_invalid", "交互环节 cue 只能是一级重录提示")
        if payload.response_path is None:  # contract postcondition
            _fail("autopilot_command_invalid", "一级 cue 缺少冻结 response_path")
        category = _INTERACTION_PATH_TO_CATEGORY.get(payload.response_path)
        branch = selected.branches.get(category) if category else None
        if (branch is None or branch.kind != "speak_then_rerecord"
                or branch.text is None):
            _fail("autopilot_command_invalid", "cue 分支不属于该环节冻结重录分支")
        expected_line = branch.text
    elif payload.purpose == "feedback":
        if stage == (0, 1):
            full = selected.branches.get("full")
            if full is None or full.kind != "speak_then_advance" or not full.text:
                _fail("autopilot_command_invalid", "该环节 full 分支没有口头成功反馈")
            expected_line = full.text
        elif stage == (1, 2):
            if payload.response_path is None:  # contract postcondition
                _fail("autopilot_command_invalid", "一级 feedback 缺少冻结 response_path")
            category = _INTERACTION_PATH_TO_CATEGORY.get(payload.response_path)
            rerecord = selected.branches.get(category) if category else None
            after = selected.after_rerecord or {}
            after_full = after.get("full")
            if (rerecord is None or rerecord.kind != "speak_then_rerecord"
                    or after_full is None
                    or after_full.kind != "speak_then_advance"
                    or not after_full.text):
                _fail("autopilot_command_invalid", "重录后 feedback 与冻结结构不一致")
            expected_line = after_full.text
        else:
            _fail("autopilot_command_invalid", "feedback 提示等级非法")
    else:
        if command.prompt_level != 3 or command.attempt_seq not in {1, 2}:
            _fail("autopilot_command_invalid", "tell_answer 必须终止一次已判定录音")
        if command.attempt_seq == 1:
            tell_lines = _interaction_tell_lines(selected)
            if not tell_lines or payload.speech_text not in tell_lines:
                _fail("autopilot_command_invalid", "tell_answer 不在该环节冻结告知话术集内")
            expected_line = payload.speech_text
        else:
            after = selected.after_rerecord or {}
            otherwise = after.get("otherwise")
            if (otherwise is None or otherwise.kind != "speak_then_advance"
                    or not otherwise.text):
                _fail("autopilot_command_invalid", "重录后 tell_answer 与冻结结构不一致")
            expected_line = otherwise.text
    if payload.speech_text != expected_line:
        _fail("autopilot_command_invalid", "TTS purpose 与冻结话术不一致")
    if payload.speech_text not in selected.allowed_tts_lines:
        _fail("autopilot_command_invalid", "TTS 话术未进入冻结白名单")


def _validate_tts_semantics(
    command: RuntimeCommand,
    payload: TtsCommandPayload,
    selected: _SelectedContent,
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
    if isinstance(selected, _InteractionContent):
        _validate_interaction_tts_semantics(command, payload, selected)
        return
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
    # Repeat semantics are part of command identity: a projection is the last
    # point before the patient device acts on it, so an unbound or drifted
    # command must fail here too, not only on the routes that issue commands.
    owning_session = db.get(TrainSession, command.session_id)
    if owning_session is None:
        _fail("autopilot_command_invalid", "命令缺少所属场次")
    _require_command_repeat_binding(command, owning_session)
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
                or asset.data_classification != owning_session.data_classification
                or asset.is_simulation != owning_session.is_simulation
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
    return content.load_item_bank_for_week(2)


def _session_week_bank(db: Session, session_id: str) -> content.ItemBank:
    """Default bank resolution follows the session's own training week.

    The definition-binding gate still rejects any wrong bank afterwards; this
    only decides *which* frozen weekly bank a caller without an explicit bank
    gets, so weeks 3-8 stop silently resolving to the week-2 file.  A missing
    or out-of-range session falls back to week 2 and then fails on the session
    gate itself.
    """
    train_session = db.get(TrainSession, session_id)
    week_no = getattr(train_session, "week_no", None)
    if (not isinstance(week_no, int) or isinstance(week_no, bool)
            or not 2 <= week_no <= 8):
        week_no = 2
    try:
        return content.load_item_bank_for_week(week_no)
    except content.FrozenContentUnavailable as exc:
        raise AutopilotServiceError(
            "autopilot_content_unavailable",
            f"第{week_no}周冻结题库不可用",
        ) from exc


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
    resolved_bank = bank if bank is not None else _session_week_bank(db, session_id)
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
        **_command_repeat_binding_fields(gate.train_session),
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
        bank=bank if bank is not None else _session_week_bank(db, session_id),
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
        bank=bank if bank is not None else _session_week_bank(db, session_id),
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
    # A pre-attempt capture claim (ASR not yet resolved) needs the identical
    # fence: a late ASR result must not materialize an AttemptEvent after this
    # same transaction pauses the scope.
    evidence_ledger.invalidate_capture_processing_claims(
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


def fence_autonomous_scope_for_device_rotation(
    db: Session,
    *,
    session_id: str,
    now: datetime | None = None,
) -> bool:
    """Atomically pause a server-hosted P0a scope when its device is rotated.

    A same-session re-pair (``/device/pair`` issuing a new capability while an
    old one was still active) revokes the old capability and stages a new one
    entirely independently of autopilot state — it is not itself an ACK, pause,
    or withdrawal, so it never used to fence anything here. That left a real
    gap: an in-flight ASR/judgement result bound to the old device generation
    could still silently materialize an AttemptEvent and route a next command
    to the newly paired device, which never asked for or saw the old
    recording. ``device_capability.create_capability`` only stages/flushes —
    it never commits. The caller must invoke this function in that same
    still-uncommitted pairing transaction, after the capability rotation is
    staged, must pause SessionRuntimeState in the same transaction when this
    returns ``True``, and must commit the whole thing exactly once: pairing
    and its fencing land in one atomic snapshot, or neither does. Returns
    ``False`` when there is no autonomous P0a scope to fence (manual mode, or
    already paused/terminal) — an ordinary re-pair during manual operation
    must not be disrupted.
    """
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

    from_status = state.status
    command_id = state.current_command_id
    previous_revision = state.revision
    # Same fencing as pause_autonomous_scope_for_researcher: a stale in-flight
    # provider result from the old device generation must never persist ASR/
    # judgement facts or route a command after this transaction commits.
    evidence_ledger.invalidate_processing_claims(db, session_id=session_id)
    evidence_ledger.invalidate_capture_processing_claims(db, session_id=session_id)
    state.status = "paused"
    state.current_command_id = None
    state.revision += 1
    state.lease_owner = None
    state.lease_acquired_at = None
    state.lease_expires_at = None
    state.last_error_code = "autopilot_device_rotated"
    state.updated_at = observed_at
    db.add(state)
    db.add(AutopilotControlEvent(
        idempotency_key=_derived_key(
            "device-rotation-pause", session_id, state.control_generation,
            state.runner_generation, previous_revision),
        session_id=session_id,
        event_seq=_next_control_event_seq(db, session_id),
        event_type="pause",
        scope_key=P0A_SCOPE_KEY,
        control_generation=state.control_generation,
        runner_generation=state.runner_generation,
        command_id=command_id,
        actor_type="system",
        actor_id=None,
        reason_code="autopilot_device_rotated",
        from_mode="autonomous",
        to_mode="autonomous",
        from_status=from_status,
        to_status="paused",
        payload_json=autopilot_ledger.encode_control_event_payload(
            "pause", {
                "reason_code": "autopilot_device_rotated",
                "source": "device_rotation",
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
        "patient_requested_pause",
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
        if idempotency_token is None:
            _fail("autopilot_input_invalid", "设备停止缺少幂等标识")
        if source == "patient_rec_failure" and expected_item_id is None:
            _fail("autopilot_input_invalid", "设备停止缺少当前协议位置或失败幂等标识")
    elif capability_token_hash is not None:
        _fail("autopilot_input_invalid", "非设备停止不得携带设备能力")
    if source in {"session_abort", "subject_withdrawal"} and actor_id is None:
        _fail("autopilot_input_invalid", "中止场次或研究撤回必须绑定具名研究者")
    if source == "cloud_processing_consent_revoked" and actor_id is not None:
        _fail("autopilot_input_invalid", "云处理撤回必须记为系统治理事实")
    if source == "patient_requested_pause" and (
            expected_item_id is not None or expected_turn_seq is not None):
        _fail("autopilot_input_invalid", "老人主动暂停不得伪造当前题位故障")
    if source not in {"patient_rec_failure", "patient_requested_pause"} and (
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
    # Withdrawal/consent-revocation/patient-rec-failure/session-abort all route
    # through this external-stop path; fence any pre-attempt capture claim the
    # same way. Same-session device re-pairing does NOT route through here —
    # see fence_autonomous_scope_for_device_rotation, called from /device/pair.
    evidence_ledger.invalidate_capture_processing_claims(db, session_id=session_id)
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
            or command.state != "succeeded"
            or command.succeeded_at is None):
        return False
    if event.payload_json != autopilot_ledger.encode_control_event_payload(
            "scope_complete", {"completed_command_seq": command.command_seq}):
        return False
    if command.kind == "record":
        # Interaction silent-advance completion: the final plan position ended
        # on a judged hit, so the closing command is its succeeded recording.
        # The stopped-capture chain is the media-terminal proof, exactly as the
        # system-failure safety close accepts it.
        try:
            autopilot_ledger.verify_terminal_record_capture(db, command.id)
        except autopilot_ledger.AutopilotProofError:
            return False
        return True
    if command.kind != "tts":
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


def legacy_expected_attempt_facts(
    db: Session,
    *,
    record: RuntimeCommand,
    capture: AttemptCaptureProcessing,
    bank: content.ItemBank | None = None,
    protocol: dict | None = None,
) -> dict:
    """The only authoritative shape a legacy Attempt for this capture may have.

    Derived provider-free from immutable evidence alone — the record command,
    its capture receipt and the frozen bank/protocol — so the full verifier, the
    terminal pause probe, the pause stage and the drain matcher all close the
    persisted Attempt against exactly the same facts.  Without a single source
    here, ``response_role``/``cue_type``/``duration_seconds`` could be rewritten
    after judgement and still yield a valid drain target.
    """
    resolved_bank = (bank if bank is not None
                     else _session_week_bank(db, record.session_id))
    resolved_protocol = protocol or _default_protocol()
    train_session = db.get(TrainSession, record.session_id)
    if train_session is None:
        _fail("autopilot_session_unavailable", "场次不存在或不可用于 P0a")
    selected = _select_p0a_content(
        train_session, resolved_bank, resolved_protocol,
        item_id=record.item_id, turn_seq=record.turn_seq)
    receipt = db.get(AudioCaptureReceipt, capture.receipt_server_seq)
    if receipt is None:
        _fail("autopilot_record_capture_invalid", "采集回执不存在")
    cue_type: str | None = None
    if record.prompt_level != 0:
        if record.prompt_level not in {1, 2}:
            _fail("autopilot_command_invalid", "P0a 录音命令提示等级非法")
        rows = [row for row in resolved_bank.single_element
                if row.get("item_id") == selected.item_id]
        cues = rows[0].get("cues") if len(rows) == 1 else None
        cue = cues.get(str(record.prompt_level)) if isinstance(cues, dict) else None
        label = cue.get("cue_type") if isinstance(cue, dict) else None
        if not isinstance(label, str) or not label.strip():
            _fail("autopilot_content_incomplete", "冻结提示缺少 cue_type")
        cue_type = label.strip()
    return {
        "item_id": record.item_id,
        "turn_seq": record.turn_seq,
        "response_role": selected.response_role,
        "raw_audio_id": record.expected_raw_audio_id,
        "prompt_level": record.prompt_level,
        "cue_type": cue_type,
        "duration_seconds": receipt.duration_seconds,
    }


def legacy_attempt_matches_expected_facts(
    attempt: AttemptEvent, expected: dict,
) -> bool:
    """Field-by-field closure of a persisted Attempt against those facts."""
    return all(getattr(attempt, name) == value
               for name, value in expected.items())


def legacy_attempt_closes_persisted_evidence(
    db: Session,
    *,
    attempt: AttemptEvent,
    record: RuntimeCommand,
    capture: AttemptCaptureProcessing,
) -> bool:
    """Closure that depends on persisted evidence alone, never on today's files.

    Used by every provider-free terminal path — the pause probe, the pause
    stage and the drain matcher.  Those must stay correct after the item bank or
    autopilot protocol on disk changes: an already-judged legacy scope has to
    remain finishable, and a drain target already earned must not evaporate
    because a content file moved.  The frozen P0a contract supplies the only
    rule that is not a stored column: a record command exists for prompt levels
    0..2 only, level 0 carries no cue and a cued level always names one.
    """
    receipt = db.get(AudioCaptureReceipt, capture.receipt_server_seq)
    if (receipt is None
            or record.prompt_level not in LEGACY_CUE_TYPE_BY_PROMPT_LEVEL):
        return False
    if not (record.response_role or "").strip():
        return False
    return all((
        attempt.item_id == record.item_id,
        attempt.turn_seq == record.turn_seq,
        attempt.response_role == record.response_role,
        attempt.raw_audio_id == record.expected_raw_audio_id,
        attempt.prompt_level == record.prompt_level,
        attempt.duration_seconds == receipt.duration_seconds,
        attempt.cue_type
        == LEGACY_CUE_TYPE_BY_PROMPT_LEVEL[record.prompt_level],
    ))


def legacy_asr_facts_are_legal(
    *, text, confidence, engine_version, hotword_hit,
) -> bool:
    """The one closed ASR contract, shared by both sides of the commit.

    Used before the first durable write *and* by every terminal readback, so a
    transcript shape the prewrite gate would refuse can never be accepted later
    just because it was persisted consistently across every row.
    """
    if not isinstance(text, str) or len(text) > 2000:
        return False
    if not isinstance(engine_version, str) or not engine_version.strip():
        return False
    if not isinstance(hotword_hit, bool):
        return False
    if confidence is None:
        return True
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return False
    return math.isfinite(confidence) and 0.0 <= float(confidence) <= 1.0


def legacy_interactions_are_exact(
    db: Session,
    attempt: AttemptEvent,
    *,
    expect_judgement: bool,
) -> bool:
    """Exactly the interaction rows this legacy stage must have, in order.

    A subset test over ``event_type`` would accept a duplicated, reordered,
    rewritten or padded trail.  The ordered sequence is pinned, every row must
    close onto the exact attempt, every payload must carry exactly its own key
    set with the frozen constants this path always writes, and every variable
    fact must agree with the attempt it describes.  The payload encoder only
    rejects unknown keys, so it cannot be relied on to prove a key is present.
    """
    expected = ["attempt_received", "asr_completed"]
    if expect_judgement:
        expected.append("judgement_completed")
    rows = list(db.exec(select(InteractionEvent).where(
        InteractionEvent.session_id == attempt.session_id,
        InteractionEvent.attempt_id == attempt.id,
    ).order_by(InteractionEvent.event_seq)))
    if [row.event_type for row in rows] != expected:
        return False
    for row in rows:
        if (row.item_id != attempt.item_id
                or row.turn_seq != attempt.turn_seq
                or row.attempt_seq != attempt.attempt_seq
                or row.is_simulation != attempt.is_simulation):
            return False
        try:
            payload = json.loads(row.payload_json)
        except (TypeError, ValueError):
            return False
        if not isinstance(payload, dict):
            return False
        try:
            canonical = evidence_ledger.encode_event_payload(
                row.event_type, payload)
        except (TypeError, ValueError):
            return False
        if canonical != row.payload_json:
            return False
        if row.event_type == "attempt_received":
            expected_keys = {"raw_audio_id", "prompt_level", "cue_type",
                             "duration_seconds", "processing_status"}
            facts = (
                payload.get("raw_audio_id") == attempt.raw_audio_id,
                payload.get("prompt_level") == attempt.prompt_level,
                payload.get("cue_type") == attempt.cue_type,
                payload.get("duration_seconds") == attempt.duration_seconds,
                payload.get("processing_status") == "received",
            )
        elif row.event_type == "asr_completed":
            expected_keys = {"asr_engine_version", "asr_confidence",
                             "degraded", "hotword_hit"}
            facts = (
                payload.get("asr_engine_version") == attempt.asr_engine_version,
                payload.get("asr_confidence") == attempt.asr_confidence,
                payload.get("degraded") is False,
                # The same contract the prewrite gate applies, so a synchronised
                # forgery — an over-long transcript or an impossible confidence
                # written consistently to every row — cannot be read back as
                # valid terminal evidence.
                legacy_asr_facts_are_legal(
                    text=attempt.asr_text,
                    confidence=attempt.asr_confidence,
                    engine_version=attempt.asr_engine_version,
                    hotword_hit=payload.get("hotword_hit"),
                ),
            )
        else:
            expected_keys = {"answer_type", "score", "needs_review",
                             "judge_mode", "judge_engine_version", "matched_on",
                             "contains_target", "truth_scope"}
            facts = (
                payload.get("answer_type") == attempt.operational_answer_type,
                payload.get("score") == attempt.operational_score,
                payload.get("needs_review") == attempt.operational_needs_review,
                payload.get("judge_mode") == attempt.judge_mode,
                payload.get("judge_engine_version")
                == attempt.judge_engine_version,
                payload.get("matched_on") == attempt.matched_on,
                payload.get("contains_target") == attempt.contains_target,
                payload.get("truth_scope") == "operational_only",
            )
        if set(payload) != expected_keys or not all(facts):
            return False
    return True


def legacy_judgement_facts_are_legal(
    *, answer_type, score, needs_review, judge_mode, judge_engine_version,
    matched_on, judge_reason, judge_portrait_used, contains_target,
) -> bool:
    """The one closed judgement contract, shared by both sides of the commit.

    Used before the first durable judgement write *and* by every terminal
    readback, so a provider result that the readback would later refuse can
    never be persisted in the first place — which would strand the Attempt as
    completed-but-unclosable with no way to retry.
    """
    if (not isinstance(judge_portrait_used, bool) or judge_portrait_used is not False
            or not isinstance(needs_review, bool)
            or not isinstance(contains_target, bool)
            or not isinstance(score, (int, float)) or isinstance(score, bool)
            or not (judge_engine_version or "").strip()):
        return False
    expected_score = LEGACY_JUDGEMENT_SCORE_BY_ANSWER_TYPE.get(answer_type)
    if expected_score is None:
        return False
    score = float(score)
    if not math.isfinite(score) or score != expected_score:
        return False
    if judge_mode == "LLM辅助":
        # Forbidden on both sides of the commit by the one shared validator, so
        # this ambiguous version can never be persisted and then refused: a
        # custom provider claiming to be the deterministic rule engine is
        # rejected before the first durable write.
        if (matched_on is not None
                or judge_engine_version == "rule-1"
                or answer_type not in LEGACY_LLM_ANSWER_TYPES):
            return False
        return judge_reason is None or (
            isinstance(judge_reason, str) and bool(judge_reason.strip())
            and len(judge_reason) <= LEGACY_MAX_JUDGE_REASON_CHARS)
    if judge_mode == "规则确定式":
        if judge_reason is not None or judge_engine_version != "rule-1":
            return False
        key = (answer_type, matched_on, needs_review)
        if key not in LEGACY_RULE_JUDGEMENTS:
            return False
        expected_contains = LEGACY_RULE_JUDGEMENTS[key]
        return expected_contains is None or contains_target is expected_contains
    return False


def legacy_judgement_result_is_legal(result: object) -> bool:
    """Pre-commit gate over a freshly produced ``_classify_operational`` dict."""
    if not isinstance(result, dict):
        return False
    # Exact equality, not a subset: an unexpected extra key means the dict did
    # not come from ``_classify_operational`` at all, and a missing one means
    # the branch that produced it is not the closed contract either.
    exact_keys = {"answer_type", "ai_score", "needs_review", "judge_mode",
                  "judge_engine_version", "matched_on", "judge_reason",
                  "judge_portrait_used", "contains_target", "truth_scope"}
    if set(result) != exact_keys:
        return False
    if result["truth_scope"] != "operational_only":
        return False
    return legacy_judgement_facts_are_legal(
        answer_type=result["answer_type"],
        score=result["ai_score"],
        needs_review=result["needs_review"],
        judge_mode=result["judge_mode"],
        judge_engine_version=result["judge_engine_version"],
        matched_on=result["matched_on"],
        judge_reason=result["judge_reason"],
        judge_portrait_used=result["judge_portrait_used"],
        contains_target=result["contains_target"],
    )


def legacy_attempt_is_successfully_judged(
    db: Session,
    attempt: AttemptEvent,
) -> bool:
    """Only a completed, fully judged ordinary Attempt closes a legacy scope.

    A ``technical_failure`` — or a ``completed`` row missing any judgement fact
    or its required interaction trail — is emphatically not "recovery
    complete": stamping that reason code over it would claim the recording was
    resolved when it was not.
    """
    if (attempt.processing_status != "completed"
            or attempt.error_code is not None
            or attempt.asr_text is None
            or not (attempt.asr_engine_version or "").strip()
            or attempt.processed_at is None
            or not (attempt.operational_answer_type or "").strip()
            or not isinstance(attempt.operational_score, (int, float))
            or isinstance(attempt.operational_score, bool)
            or not isinstance(attempt.operational_needs_review, bool)
            or not isinstance(attempt.contains_target, bool)
            or not isinstance(attempt.judge_portrait_used, bool)
            or not (attempt.judge_engine_version or "").strip()):
        return False
    if not legacy_judgement_facts_are_legal(
            answer_type=attempt.operational_answer_type,
            score=attempt.operational_score,
            needs_review=attempt.operational_needs_review,
            judge_mode=attempt.judge_mode,
            judge_engine_version=attempt.judge_engine_version,
            matched_on=attempt.matched_on,
            judge_reason=attempt.judge_reason,
            judge_portrait_used=attempt.judge_portrait_used,
            contains_target=attempt.contains_target):
        return False
    return legacy_interactions_are_exact(db, attempt, expect_judgement=True)


def legacy_session_repeat_poison_reason(
    db: Session, *, session_id: str,
) -> Literal[
    "repeat_binding", "repeat_ledger", "bound_or_replayed_command"] | None:
    """Read-only: why this session can no longer be a pre-protocol chain.

    The legacy marker may only ever stand for "this whole session predates the
    repeat protocol".  A persisted repeat binding on the session or its plan, a
    repeat ledger row, or a single bound or replayed command anywhere in the
    session each contradict that, so every legacy path — the full verifier, the
    provider-free terminal pause and the drain matcher — has to refuse on
    exactly the same facts rather than on three drifting subsets.

    Deliberately free of provider, content, feature and device authority: a
    terminal pause that is already owed must never be stranded by a toggle
    flipped after the judgement committed.  Callers keep their own shape gates,
    so a missing session or plan is not guessed at here.  Never flushes, never
    mutates a row.
    """
    # Default autoflush would push a caller's uncommitted edit to the database
    # on the first read below, which is a write this helper must never cause.
    with db.no_autoflush:
        train_session = db.get(TrainSession, session_id)
        if train_session is not None:
            if (train_session.repeat_protocol_version_id is not None
                    or train_session.repeat_protocol_definition_digest
                    is not None):
                return "repeat_binding"
            plan = (db.get(VisitPlan, train_session.visit_plan_id)
                    if train_session.visit_plan_id else None)
            if plan is not None and (
                    plan.repeat_protocol_version_id is not None
                    or plan.repeat_protocol_definition_digest is not None):
                return "repeat_binding"
        if db.exec(select(AutopilotRepeatRequest).where(
                AutopilotRepeatRequest.session_id == session_id,
        )).first() is not None:
            return "repeat_ledger"
        if db.exec(select(RuntimeCommand).where(
            RuntimeCommand.session_id == session_id,
            or_(
                RuntimeCommand.repeat_protocol_version_id.is_not(None),
                RuntimeCommand.repeat_protocol_definition_digest.is_not(None),
                RuntimeCommand.replay_source_command_id.is_not(None),
                RuntimeCommand.replay_ordinal.is_not(None),
                RuntimeCommand.replay_source_payload_sha256.is_not(None),
            ),
        )).first() is not None:
            return "bound_or_replayed_command"
    return None


def _legacy_recovery_pause_matches(
    db: Session,
    event: AutopilotControlEvent,
    *,
    state: SessionAutopilotState,
    command: RuntimeCommand,
) -> bool:
    """Exact, database-aware proof of one legacy-recovery pause.

    A drain target may only be derived from a control fact that is genuinely
    closed.  Matching the payload's ``source``/``reason_code``/actor alone is far
    too weak here: it would accept a pause stamped onto a command that never
    recorded, onto a capture that is not the legacy one, or onto an Attempt that
    failed.  Everything the pause claims is therefore re-proved from the rows.
    """
    shape = (
        event.from_status == "processing_attempt",
        event.to_status == "paused",
        event.from_mode == "autonomous",
        event.to_mode == "autonomous",
        event.session_id == command.session_id,
        event.command_id == command.id,
        event.scope_key == command.scope_key,
        event.control_generation == command.control_generation,
        event.runner_generation == command.runner_generation,
        event.payload_json == autopilot_ledger.encode_control_event_payload(
            "pause", {
                "reason_code": LEGACY_REPEAT_RECOVERY_REASON_CODE,
                "source": LEGACY_REPEAT_RECOVERY_SOURCE,
            }),
        state.status == "paused",
        state.last_error_code == LEGACY_REPEAT_RECOVERY_REASON_CODE,
        command.kind == "record",
        command.state == "succeeded",
        command.expected_raw_audio_id is not None,
    )
    if not all(shape):
        return False
    # An already-paused legacy scope can still be poisoned afterwards; the drain
    # target must close with the same facts the verifier refuses on.
    if legacy_session_repeat_poison_reason(
            db, session_id=command.session_id) is not None:
        return False
    captures = list(db.exec(select(AttemptCaptureProcessing).where(
        AttemptCaptureProcessing.record_command_id == command.id)))
    if len(captures) != 1:
        return False
    capture = captures[0]
    capture_facts = (
        capture.session_id == command.session_id,
        capture.raw_audio_id == command.expected_raw_audio_id,
        capture.repeat_admission_semantics
        == evidence_ledger.LEGACY_PRE_REPEAT_ADMISSION,
        capture.repeat_protocol_version_id is None,
        capture.repeat_protocol_definition_digest is None,
        capture.repeat_request_id is None,
        capture.processing_status == "asr_completed",
        capture.disposition == "answer_candidate",
        capture.final_attempt_id is not None,
        capture.error_code is None,
        capture.processed_at is not None,
        capture.record_command_id == command.id,
        capture.predecessor_command_id == command.predecessor_command_id,
        capture.item_id == command.item_id,
        capture.turn_seq == command.turn_seq,
        capture.proof_attempt_seq == command.attempt_seq,
        capture.proof_prompt_level == command.prompt_level,
        event.idempotency_key == legacy_repeat_recovery_event_key(
            command.session_id, capture.id),
    )
    if not all(capture_facts):
        return False
    receipt = db.get(AudioCaptureReceipt, capture.receipt_server_seq)
    attempt = db.get(AttemptEvent, capture.final_attempt_id)
    if attempt is None or receipt is None:
        return False
    attempt_facts = (
        attempt.session_id == capture.session_id,
        attempt.raw_audio_id == capture.raw_audio_id,
        attempt.item_id == capture.item_id,
        attempt.turn_seq == capture.turn_seq,
        attempt.attempt_seq == capture.proof_attempt_seq,
        attempt.prompt_level == capture.proof_prompt_level,
        attempt.is_simulation == capture.is_simulation,
        attempt.duration_seconds == receipt.duration_seconds,
        # The ASR facts were double-written in one transaction; a later edit to
        # either copy must not still yield a drain target.
        attempt.asr_engine_version == capture.asr_engine_version,
        attempt.asr_confidence == capture.asr_confidence,
        receipt.raw_audio_id == capture.raw_audio_id,
        receipt.session_id == capture.session_id,
        receipt.turn_key == command.turn_key,
    )
    if not all(attempt_facts):
        return False
    if not legacy_attempt_closes_persisted_evidence(
            db, attempt=attempt, record=command, capture=capture):
        return False
    try:
        # Historical proof only: a legitimate later drain/takeover must not
        # invalidate it, but the issued chain identity is immutable and a
        # rewritten record/predecessor pair must never earn a drain target.
        autopilot_ledger.verify_immutable_record_capture(db, command)
    except autopilot_ledger.AutopilotProofError:
        return False
    return legacy_attempt_is_successfully_judged(db, attempt)


def _external_stop_pause_matches(
    db: Session,
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
    if payload["source"] == "patient_requested_pause":
        return event.actor_type == "device" and bool(event.actor_id)
    if payload["source"] == "cloud_processing_consent_revoked":
        return event.actor_type == "system" and event.actor_id is None
    if payload["source"] == "protocol_position_gap":
        return event.actor_type == "system" and event.actor_id is None
    if payload["source"] == REPEAT_PAUSE_SOURCE:
        return (event.actor_type == "system" and event.actor_id is None
                and event.reason_code == REPEAT_LIMIT_REASON_CODE)
    if payload["source"] == LEGACY_REPEAT_RECOVERY_SOURCE:
        return (event.actor_type == "system" and event.actor_id is None
                and event.reason_code == LEGACY_REPEAT_RECOVERY_REASON_CODE
                and _legacy_recovery_pause_matches(
                    db, event, state=state, command=command))
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
            db, latest, state=state, command=command))
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
                    db, latest, state=state, command=command)
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


def _takeover_is_ready(
    db: Session,
    *,
    state: SessionAutopilotState,
) -> bool:
    """Project whether the exact takeover endpoint can safely succeed now.

    A paused runtime is deliberately insufficient.  This projection reuses the
    same persisted command/control proof that the mutation requires, so the UI
    cannot guess readiness from a broad status label.
    """
    if (state.mode != "autonomous"
            or state.status not in {"paused", "scope_completed", "failed"}):
        return False
    try:
        _safe_takeover_proof(db, state=state)
    except AutopilotServiceError:
        return False
    return True


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


# Mirrors the write-side contract in autopilot_ledger.fenced_autopilot_update:
# a retained current command is proof of the state, so the projection must accept
# exactly the same kind/lifecycle pairs the ledger is allowed to persist.
_STATUS_CURRENT_COMMAND_CONTRACT: dict[str, tuple[str, frozenset[str]]] = {
    "waiting_tts": ("tts", autopilot_ledger.CLAIMABLE_COMMAND_STATES),
    "waiting_recording": ("record", autopilot_ledger.CLAIMABLE_COMMAND_STATES),
    "processing_attempt": ("record", frozenset({"succeeded"})),
    "manual_draining": ("record", autopilot_ledger.CLAIMABLE_COMMAND_STATES),
}


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
            takeover_ready=False,
            current_command_kind=None,
            position_item_id=None,
            position_turn_seq=None,
            last_error_code=None,
        )
    if state.scope_key != P0A_SCOPE_KEY:
        _fail("autopilot_state_invalid", "自动驾驶 scope 非法")
    if state.mode not in {"autonomous", "manual"}:
        _fail("autopilot_state_invalid", "自动驾驶控制模式非法")
    current_kind: Literal["tts", "record"] | None = None
    current_state: str | None = None
    position_command: RuntimeCommand | None = None
    if state.current_command_id is not None:
        command = db.get(RuntimeCommand, state.current_command_id)
        if (command is None or command.session_id != session_id
                or command.scope_key != state.scope_key
                or command.control_generation != state.control_generation
                or command.runner_generation != state.runner_generation
                or command.kind not in {"tts", "record"}):
            _fail("autopilot_state_invalid", "自动驾驶状态的当前命令不一致")
        current_kind = command.kind  # type: ignore[assignment]
        current_state = command.state
        position_command = command
    else:
        # paused/scope_completed/failed/manual 无当前命令:最后一条已签发命令
        # 就是自动带练最后触及的位置(观察面与接管恢复的只读展示来源)。
        position_command = db.exec(
            select(RuntimeCommand)
            .where(
                RuntimeCommand.session_id == session_id,
                RuntimeCommand.scope_key == state.scope_key,
            )
            .order_by(RuntimeCommand.command_seq.desc())  # type: ignore[union-attr]
        ).first()
    expected = _STATUS_CURRENT_COMMAND_CONTRACT.get(state.status)
    if expected is None:
        if current_kind is not None:
            _fail("autopilot_state_invalid", "自动驾驶状态与当前命令不一致")
    elif current_kind != expected[0] or current_state not in expected[1]:
        _fail("autopilot_state_invalid", "自动驾驶状态与当前命令不一致")
    # Ownership is a separate fact from the command chain: the server never
    # claims idle while it drives, and a released manual plane never retains a
    # command or an executing status.
    if state.mode == "autonomous" and state.status == "idle":
        _fail("autopilot_state_invalid", "服务器持有控制权时不得声明空闲")
    if state.mode == "manual" and (
            state.status not in {"paused", "scope_completed", "failed"}
            or current_kind is not None):
        _fail("autopilot_state_invalid", "人工接管状态不符合服务器释放契约")
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
        takeover_ready=_takeover_is_ready(db, state=state),
        current_command_kind=current_kind,
        position_item_id=(
            position_command.item_id if position_command is not None else None),
        position_turn_seq=(
            position_command.turn_seq if position_command is not None else None),
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
        tts: RuntimeCommand, ack_key: str, position_key: str) -> str:
    return _derived_key(
        "cmd-next-position", tts.session_id, tts.idempotency_key, ack_key,
        position_key)


def _existing_position_advance(
    db: Session,
    *,
    state: SessionAutopilotState,
    tts: RuntimeCommand,
    ack_key: str,
    selected: _SelectedContent,
    active_capability: PatientDeviceCapability,
) -> RuntimeCommand | None:
    key = _next_position_command_key(
        tts, ack_key, f"{selected.item_id}#{selected.turn_seq}")
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
    resolved_bank = bank if bank is not None else _session_week_bank(db, session_id)
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
    _require_command_repeat_binding(tts, gate.train_session)
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
    next_selected: _SelectedContent | None = None
    next_package: dict | None = None
    if effect == "advance":
        try:
            resolved_plan = _resolved_profile_plan(
                gate.train_session, resolved_bank, resolved_protocol)
            positions = resolved_plan.positions
            current_position = autopilot_positions.find_position(
                positions,
                item_id=tts.item_id,
                turn_seq=tts.turn_seq,
            )
            current_index = positions.index(current_position)
            if current_index + 1 >= len(positions):
                next_decision = autopilot_positions.PositionDecision(
                    completed=True)
            else:
                next_position = positions[current_index + 1]
                if next_position.task_type in {"双要素", "多要素"}:
                    next_package = _interaction_package_or_none(
                        gate.train_session, resolved_bank, resolved_protocol)
                next_decision = autopilot_positions.decision_for_position(
                    resolved_bank, next_position,
                    interaction_package=next_package)
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
                interaction_package=next_package,
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
            is_simulation=gate.train_session.is_simulation,
            data_classification=gate.train_session.data_classification,
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
            # Inherit provenance from the predecessor TTS command bound to this
            # recording — including a replayed one — instead of re-reading
            # today's active version.  That binding rests on the device's
            # canonical media-ended ACK for that command; it proves neither
            # that a person heard anything nor any audio identity.
            repeat_protocol_version_id=tts.repeat_protocol_version_id,
            repeat_protocol_definition_digest=(
                tts.repeat_protocol_definition_digest),
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
                tts, ack_idempotency_key, next_decision.position.position_key),
            session_id=session_id,
            command_seq=state.next_command_seq,
            item_id=next_selected.item_id,
            turn_seq=next_selected.turn_seq,
            turn_key=f"{next_selected.item_id}#{next_selected.turn_seq}",
            attempt_seq=1,
            prompt_level=0,
            **_command_definition_fields(next_selected),
            **_command_repeat_binding_fields(gate.train_session),
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
    owning_session = db.get(TrainSession, attempt.session_id)
    if (owning_session is None
            or attempt.is_simulation != owning_session.is_simulation
            or attempt.judge_portrait_used is not False):
        _fail(
            "autopilot_attempt_boundary_invalid",
            "P0a attempt 必须是无画像、与场次分类一致的运行证据")
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
            or event.is_simulation != attempt.is_simulation):
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
    if isinstance(selected, _InteractionContent):
        category = _interaction_category(first, selected)
        branch = selected.branches.get(category)
        path = _INTERACTION_CATEGORY_TO_PATH.get(category)
        if (path is None or branch is None
                or branch.kind != "speak_then_rerecord"):
            _fail(
                "autopilot_attempt_sequence_invalid",
                "首次作答判类与冻结重录分支不一致",
            )
        expected_path = path
    else:
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


def _interaction_category(
    attempt: AttemptEvent,
    selected: _InteractionContent,
) -> str:
    """Map one judged attempt to the package's closed branch category.

    Advancement is binary: only ``contains_target`` (lexical target hit for
    naming turns, rubric-binary correct for open answers) selects ``full``.
    ``partial`` exists only when the rubric classifier said 部分正确 *and* the
    frozen turn carries a partial branch; otherwise the answer is ``wrong``.
    An operational no-transcript attempt is ``silence``.  ai_score 0.5 alone
    never drives a success advance (关系识别锁分 0/1 口径, 2026-08-19).
    """
    if attempt.contains_target is True:
        return "full"
    if attempt.operational_answer_type == "沉默":
        return "silence"
    if (attempt.operational_answer_type == AnswerType.部分正确.value
            and "partial" in selected.branches):
        return "partial"
    return "wrong"


def _terminal_interaction_decision(
    branch: _InteractionBranch,
    *,
    category: str,
    prompt_level: int,
    attempt_seq: int,
    response_path: ResponsePath | None,
) -> _AttemptRouteDecision:
    if branch.kind == "advance_silent":
        return _AttemptRouteDecision(
            purpose="advance_silent",
            speech_text=None,
            prompt_level=prompt_level,
            attempt_seq=attempt_seq,
        )
    if branch.kind != "speak_then_advance" or not branch.text:
        _fail("autopilot_attempt_mismatch", "冻结分支不能终止本环节")
    if category == "full":
        return _AttemptRouteDecision(
            purpose="feedback",
            speech_text=branch.text,
            prompt_level=prompt_level,
            attempt_seq=attempt_seq,
            response_path=response_path,
        )
    return _AttemptRouteDecision(
        purpose="tell_answer",
        speech_text=branch.text,
        prompt_level=3,
        attempt_seq=attempt_seq,
    )


def _interaction_route_decision(
    db: Session,
    *,
    attempt: AttemptEvent,
    record: RuntimeCommand,
    selected: _InteractionContent,
) -> _AttemptRouteDecision:
    if attempt.prompt_level == 0:
        category = _interaction_category(attempt, selected)
        branch = selected.branches.get(category)
        if branch is None:
            _fail("autopilot_attempt_mismatch", "attempt 判类没有对应冻结分支")
        if branch.kind == "speak_then_rerecord":
            path = _INTERACTION_CATEGORY_TO_PATH.get(category)
            if (category == "full" or path is None
                    or selected.after_rerecord is None or not branch.text):
                _fail(
                    "autopilot_attempt_mismatch",
                    "重录分支与冻结环节结构不一致",
                )
            return _AttemptRouteDecision(
                purpose="cue",
                speech_text=branch.text,
                prompt_level=1,
                attempt_seq=2,
                response_path=path,
            )
        return _terminal_interaction_decision(
            branch,
            category=category,
            prompt_level=attempt.prompt_level,
            attempt_seq=attempt.attempt_seq,
            response_path=None,
        )
    if attempt.prompt_level == 1:
        if selected.after_rerecord is None:
            _fail("autopilot_attempt_mismatch", "该环节没有第二次录音")
        # Even a second recording must prove it followed the source-selected
        # first cue; a forged/tampered cue command must not silently converge.
        path = _initial_response_path_for_cued_attempt(
            db, attempt=attempt, record=record, selected=selected)
        if attempt.contains_target is True:
            return _terminal_interaction_decision(
                selected.after_rerecord["full"],
                category="full",
                prompt_level=attempt.prompt_level,
                attempt_seq=attempt.attempt_seq,
                response_path=path,
            )
        return _terminal_interaction_decision(
            selected.after_rerecord["otherwise"],
            category="otherwise",
            prompt_level=attempt.prompt_level,
            attempt_seq=attempt.attempt_seq,
            response_path=None,
        )
    _fail("autopilot_attempt_mismatch", "交互环节只处理零/一级录音 attempt")


def _attempt_route_decision(
    db: Session,
    *,
    attempt: AttemptEvent,
    record: RuntimeCommand,
    selected: _SelectedContent,
) -> _AttemptRouteDecision:
    if attempt.prompt_level not in {0, 1, 2}:
        _fail("autopilot_attempt_mismatch", "P0a 只处理零至二级录音 attempt")
    if attempt.attempt_seq != attempt.prompt_level + 1:
        _fail("autopilot_attempt_sequence_invalid", "attempt_seq 与分级提示序列不单调")
    if isinstance(selected, _InteractionContent):
        return _interaction_route_decision(
            db, attempt=attempt, record=record, selected=selected)
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
    protocol: dict,
) -> TerminalEvidenceMaterialization | None:
    """Stage the immutable operational projection for one terminal attempt.

    The autopilot state row is already locked by every caller.  Item/turn rows are
    therefore created in the same transaction as either the terminal judgement or
    its recovery route.  Existing exact rows are an idempotent replay; duplicate or
    contradictory rows are never guessed through.
    """
    if decision.purpose not in {"feedback", "tell_answer", "advance_silent"}:
        return None
    if attempt.id is None:
        _fail("autopilot_terminal_evidence_invalid", "terminal attempt 缺少主键")

    try:
        proof = autopilot_ledger.verify_record_capture_for_attempt(db, record.id)
    except autopilot_ledger.AutopilotProofError as exc:
        # A replay after the scope already advanced (interaction silent-advance
        # or a routed terminal speech) no longer has this record as the live
        # attempt-processing target.  Only an already-projected attempt may take
        # the immutable-proof route; a fresh write must still prove the live
        # target, so this never widens first-time materialization.
        already_projected = db.exec(select(TurnEvent).where(
            TurnEvent.source_attempt_id == attempt.id,
        )).first()
        if already_projected is None:
            raise AutopilotServiceError(
                "autopilot_terminal_evidence_invalid",
                "terminal attempt 缺少完整的录音采集证据",
            ) from exc
        try:
            proof = autopilot_ledger.verify_terminal_record_capture(
                db, record.id)
        except autopilot_ledger.AutopilotProofError as replay_exc:
            raise AutopilotServiceError(
                "autopilot_terminal_evidence_invalid",
                "terminal attempt 缺少完整的录音采集证据",
            ) from replay_exc
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

    plan = _resolved_profile_plan(
        train_session, bank, protocol).session_plan
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
        or plan_item.task_type != gate.selected.task_type
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
    resolved_bank = bank if bank is not None else _session_week_bank(db, session_id)
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
        protocol=resolved_protocol,
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


def _require_attempt_route_current(
    state: SessionAutopilotState,
    record: RuntimeCommand,
) -> None:
    if (state.mode != "autonomous" or state.status != "processing_attempt"
            or state.current_command_id != record.id
            or record.kind != "record" or record.state != "succeeded"
            or record.control_generation != state.control_generation
            or record.runner_generation != state.runner_generation):
        _fail("autopilot_attempt_not_current", "completed attempt 已不是当前处理目标")


def _require_attempt_capture_proof(
    db: Session,
    *,
    record: RuntimeCommand,
    attempt: AttemptEvent,
) -> None:
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


def _validate_existing_silent_advance(
    db: Session,
    *,
    state: SessionAutopilotState,
    record: RuntimeCommand,
    command: RuntimeCommand,
    next_selected: _SelectedContent,
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
            or command.item_id != next_selected.item_id
            or command.turn_seq != next_selected.turn_seq
            or command.turn_key
            != f"{next_selected.item_id}#{next_selected.turn_seq}"
            or command.attempt_seq != 1 or command.prompt_level != 0
            or command.predecessor_command_id is not None
            or command.trigger_ack_idempotency_key is not None
            or command.expected_raw_audio_id is not None):
        _fail("autopilot_idempotency_conflict", "attempt 静默推进幂等事实不一致")
    try:
        payload = TtsCommandPayload.model_validate_json(command.payload_json)
    except (TypeError, ValueError) as exc:
        raise AutopilotServiceError(
            "autopilot_idempotency_conflict", "attempt 静默推进 TTS 载荷非法") from exc
    if (payload.purpose != "question"
            or payload.speech_text != next_selected.initial_prompt
            or payload.response_path is not None):
        _fail("autopilot_idempotency_conflict", "attempt 静默推进的冻结话术已冲突")
    _validate_tts_semantics(command, payload, next_selected)
    return _project_command(
        db, command, next_selected, expected_capability=active_capability)


def _cas_attempt_route_terminal(
    db: Session,
    *,
    state: SessionAutopilotState,
    record: RuntimeCommand,
    values: dict,
    observed_at: datetime,
) -> None:
    """One CAS write from processing_attempt to a no-command terminal status."""
    if (state.lease_owner is not None and state.lease_expires_at is not None
            and state.lease_expires_at > observed_at):
        _fail("autopilot_attempt_route_cas_conflict", "attempt 路由状态正被其他 worker 占用")
    result = db.execute(
        update(SessionAutopilotState)
        .where(
            SessionAutopilotState.session_id == state.session_id,
            SessionAutopilotState.scope_key == state.scope_key,
            SessionAutopilotState.mode == "autonomous",
            SessionAutopilotState.status == "processing_attempt",
            SessionAutopilotState.current_command_id == record.id,
            SessionAutopilotState.control_generation == state.control_generation,
            SessionAutopilotState.runner_generation == state.runner_generation,
            SessionAutopilotState.revision == state.revision,
        )
        .values(
            current_command_id=None,
            revision=SessionAutopilotState.revision + 1,
            lease_owner=None,
            lease_acquired_at=None,
            lease_expires_at=None,
            updated_at=observed_at,
            **values,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        _fail("autopilot_attempt_route_cas_conflict", "attempt 路由状态 CAS 已失效")


def _silent_advance_gap_event_key(
    record: RuntimeCommand, attempt: AttemptEvent,
    gap: autopilot_positions.PositionGap,
) -> str:
    return _derived_key(
        "attempt-position-gap", record.session_id, record.id, attempt.id,
        gap.position.position_key, gap.code)


def _silent_advance_completion_event_key(
    record: RuntimeCommand, attempt: AttemptEvent,
) -> str:
    return _derived_key(
        "attempt-scope-complete", record.session_id, record.id, attempt.id)


def _route_silent_advance(
    db: Session,
    *,
    state: SessionAutopilotState,
    gate: _P0aGate,
    record: RuntimeCommand,
    attempt: AttemptEvent,
    bank: content.ItemBank,
    protocol: dict,
    observed_at: datetime,
) -> RouteCompletedAttemptResult:
    """Advance the frozen plan after a silent-hit terminal attempt.

    A hit on an interaction turn issues no speech: the plan cursor moves one
    position and the next question is staged directly — or, at the plan
    boundary, the scope pauses on a content gap / completes.  Same CAS/fenced
    discipline as the speaking route; the new terminal statuses are explicit
    result states, never a bypass.
    """
    session_id = record.session_id
    next_package: dict | None = None
    try:
        resolved_plan = _resolved_profile_plan(gate.train_session, bank, protocol)
        positions = resolved_plan.positions
        current_position = autopilot_positions.find_position(
            positions, item_id=record.item_id, turn_seq=record.turn_seq)
        current_index = positions.index(current_position)
        if current_index + 1 >= len(positions):
            next_decision = autopilot_positions.PositionDecision(completed=True)
        else:
            next_position = positions[current_index + 1]
            if next_position.task_type in {"双要素", "多要素"}:
                next_package = _interaction_package_or_none(
                    gate.train_session, bank, protocol)
            next_decision = autopilot_positions.decision_for_position(
                bank, next_position, interaction_package=next_package)
    except ValueError as exc:
        raise AutopilotServiceError(
            "autopilot_position_invalid", "当前命令无法在冻结计划中推进") from exc

    if next_decision.position is not None:
        next_selected = _select_p0a_content(
            gate.train_session, bank, protocol,
            item_id=next_decision.position.item_id,
            turn_seq=next_decision.position.turn_seq,
            interaction_package=next_package,
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
            projection = _validate_existing_silent_advance(
                db,
                state=state,
                record=record,
                command=existing,
                next_selected=next_selected,
                active_capability=active_capability,
            )
            return RouteCompletedAttemptResult(
                attempt_id=attempt.id,
                status="waiting_tts",
                replayed=True,
                state_revision=state.revision,
                command=projection,
            )
        _require_attempt_route_current(state, record)
        _require_attempt_capture_proof(db, record=record, attempt=attempt)
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
                "autopilot_attempt_route_cas_conflict",
                "attempt 路由租约事实不完整") from exc
        payload = TtsCommandPayload(
            speech_key=f"p0a.question.{claimed_state.next_command_seq}",
            speech_text=next_selected.initial_prompt,
            purpose="question",
            item_id=next_selected.item_id,
            turn_seq=next_selected.turn_seq,
            cue_level=0,
        )
        command = RuntimeCommand(
            idempotency_key=route_key,
            session_id=session_id,
            command_seq=claimed_state.next_command_seq,
            item_id=next_selected.item_id,
            turn_seq=next_selected.turn_seq,
            turn_key=f"{next_selected.item_id}#{next_selected.turn_seq}",
            attempt_seq=1,
            prompt_level=0,
            **_command_definition_fields(next_selected),
            **_command_repeat_binding_fields(gate.train_session),
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
            _fail("autopilot_state_invalid", "attempt 静默推进 TTS 命令未能持久化")
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
            db, command, next_selected, expected_capability=active_capability)
        return RouteCompletedAttemptResult(
            attempt_id=attempt.id,
            status="waiting_tts",
            replayed=False,
            state_revision=final_state.revision,
            command=projection,
        )

    if next_decision.gap is not None:
        gap = next_decision.gap
        event_key = _silent_advance_gap_event_key(record, attempt, gap)
        prior = db.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.idempotency_key == event_key,
        )).first()
        if prior is not None:
            if (state.status != "paused" or state.current_command_id is not None
                    or state.last_error_code != gap.code
                    or prior.session_id != session_id
                    or prior.event_type != "pause"
                    or prior.command_id != record.id
                    or prior.control_generation != state.control_generation
                    or prior.runner_generation != state.runner_generation):
                _fail("autopilot_idempotency_conflict", "协议位置缺口幂等事实不一致")
            return RouteCompletedAttemptResult(
                attempt_id=attempt.id,
                status="paused",
                replayed=True,
                state_revision=state.revision,
                command=None,
            )
        _require_attempt_route_current(state, record)
        _require_attempt_capture_proof(db, record=record, attempt=attempt)
        _cas_attempt_route_terminal(
            db,
            state=state,
            record=record,
            values={"status": "paused", "last_error_code": gap.code},
            observed_at=observed_at,
        )
        db.add(AutopilotControlEvent(
            idempotency_key=event_key,
            session_id=session_id,
            event_seq=_next_control_event_seq(db, session_id),
            event_type="pause",
            scope_key=P0A_SCOPE_KEY,
            control_generation=state.control_generation,
            runner_generation=state.runner_generation,
            command_id=record.id,
            actor_type="system",
            reason_code=gap.code,
            from_mode="autonomous",
            to_mode="autonomous",
            from_status="processing_attempt",
            to_status="paused",
            payload_json=autopilot_ledger.encode_control_event_payload(
                "pause", {
                    "reason_code": gap.code,
                    "source": "protocol_position_gap",
                }),
            created_at=observed_at,
        ))
        db.flush()
        db.expire(state)
        final_state = db.get(SessionAutopilotState, session_id)
        return RouteCompletedAttemptResult(
            attempt_id=attempt.id,
            status="paused",
            replayed=False,
            state_revision=final_state.revision if final_state else 0,
            command=None,
        )

    assert next_decision.completed
    event_key = _silent_advance_completion_event_key(record, attempt)
    prior = db.exec(select(AutopilotControlEvent).where(
        AutopilotControlEvent.idempotency_key == event_key,
    )).first()
    if prior is not None:
        if (state.status != "scope_completed"
                or state.current_command_id is not None
                or prior.session_id != session_id
                or prior.event_type != "scope_complete"
                or prior.command_id != record.id
                or prior.control_generation != state.control_generation
                or prior.runner_generation != state.runner_generation):
            _fail("autopilot_idempotency_conflict", "scope 完成幂等事实不一致")
        return RouteCompletedAttemptResult(
            attempt_id=attempt.id,
            status="scope_completed",
            replayed=True,
            state_revision=state.revision,
            command=None,
        )
    _require_attempt_route_current(state, record)
    _require_attempt_capture_proof(db, record=record, attempt=attempt)
    _cas_attempt_route_terminal(
        db,
        state=state,
        record=record,
        values={"status": "scope_completed"},
        observed_at=observed_at,
    )
    db.add(AutopilotControlEvent(
        idempotency_key=event_key,
        session_id=session_id,
        event_seq=_next_control_event_seq(db, session_id),
        event_type="scope_complete",
        scope_key=P0A_SCOPE_KEY,
        control_generation=state.control_generation,
        runner_generation=state.runner_generation,
        command_id=record.id,
        actor_type="system",
        from_mode="autonomous",
        to_mode="autonomous",
        from_status="processing_attempt",
        to_status="scope_completed",
        payload_json=autopilot_ledger.encode_control_event_payload(
            "scope_complete", {"completed_command_seq": record.command_seq}),
        created_at=observed_at,
    ))
    db.flush()
    db.expire(state)
    final_state = db.get(SessionAutopilotState, session_id)
    return RouteCompletedAttemptResult(
        attempt_id=attempt.id,
        status="scope_completed",
        replayed=False,
        state_revision=final_state.revision if final_state else 0,
        command=None,
    )


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
    resolved_bank = bank if bank is not None else _session_week_bank(db, session_id)
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

    _require_command_repeat_binding(record, gate.train_session)
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
        protocol=resolved_protocol,
    )
    if decision.purpose == "advance_silent":
        return _route_silent_advance(
            db,
            state=state,
            gate=gate,
            record=record,
            attempt=attempt,
            bank=resolved_bank,
            protocol=resolved_protocol,
            observed_at=observed_at,
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
        **_command_repeat_binding_fields(gate.train_session),
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


def repeat_replay_command_key(session_id: str, capture_id: int) -> str:
    """Derive the replay command idempotency key from capture identity + ordinal."""
    return _derived_key("cmd-repeat-replay", session_id, capture_id, 1)


def repeat_limit_event_key(session_id: str, capture_id: int) -> str:
    """Derive the limit-pause event idempotency key from capture identity."""
    return _derived_key("repeat-limit", session_id, capture_id)


def legacy_repeat_recovery_event_key(session_id: str, capture_id: int) -> str:
    """Derive the legacy-recovery pause key from the exact capture identity."""
    return _derived_key("legacy-repeat-recovery", session_id, capture_id)


def _require_repeat_source_tts(
    db: Session,
    *,
    state: SessionAutopilotState,
    record: RuntimeCommand,
    gate: _P0aGate,
) -> RuntimeCommand:
    """Prove the exact predecessor TTS command this recording answered.

    Only the recording's own persisted predecessor qualifies, and only when it
    is a succeeded question/cue TTS whose bound device submitted a canonical
    ``tts_ended``/``media_ended`` ACK on the same device, generation and frozen
    slot.  Nothing here is re-derived from today's item bank: the replay must
    reproduce the historical command's canonical ``payload_json``.

    That ACK is a device-submitted media fact.  It does not prove a person
    heard anything, and it carries no audio identity: no WAV checksum, artifact
    id or waveform binding exists on either side.
    """
    if (record.predecessor_command_id is None
            or record.trigger_ack_idempotency_key is None):
        _fail("autopilot_repeat_source_invalid", "录音命令缺少前置 TTS 溯源")
    source = db.exec(select(RuntimeCommand).where(
        RuntimeCommand.id == record.predecessor_command_id,
        RuntimeCommand.session_id == record.session_id,
    ).with_for_update()).first()
    if source is None or source.kind != "tts" or source.state != "succeeded":
        _fail("autopilot_repeat_source_invalid", "前置 TTS 命令不存在或未成功结束")
    if (source.item_id != record.item_id
            or source.turn_seq != record.turn_seq
            or source.turn_key != record.turn_key
            or source.attempt_seq != record.attempt_seq
            or source.prompt_level != record.prompt_level
            or source.scope_key != record.scope_key
            or source.control_generation != record.control_generation
            or source.runner_generation != record.runner_generation
            or source.issued_capability_token_hash
            != record.issued_capability_token_hash
            or source.issued_device_id_hash != record.issued_device_id_hash):
        _fail("autopilot_repeat_source_invalid", "前置 TTS 与录音命令不在同一冻结环节")
    _validate_command_identity(source, gate.selected)
    _require_command_repeat_binding(source, gate.train_session)
    _require_same_issued_active_device(source, gate.active_capability)
    _require_terminal_tts_ack(
        db, state, source, record.trigger_ack_idempotency_key)
    try:
        payload = TtsCommandPayload.model_validate_json(source.payload_json)
    except (TypeError, ValueError) as exc:
        raise AutopilotServiceError(
            "autopilot_repeat_source_invalid", "前置 TTS 载荷不符合封闭契约") from exc
    if payload.purpose not in {"question", "cue"}:
        _fail("autopilot_repeat_source_invalid", "只有提问或提示语音可以重播")
    _validate_tts_semantics(source, payload, gate.selected)
    return source


def _repeat_slot_requests(
    db: Session,
    *,
    session_id: str,
    item_id: str,
    turn_seq: int,
    attempt_seq: int,
    prompt_level: int,
) -> list[AutopilotRepeatRequest]:
    """Every repeat request already recorded for one logical attempt slot."""
    return list(db.exec(select(AutopilotRepeatRequest).where(
        AutopilotRepeatRequest.session_id == session_id,
        AutopilotRepeatRequest.item_id == item_id,
        AutopilotRepeatRequest.turn_seq == turn_seq,
        AutopilotRepeatRequest.attempt_seq == attempt_seq,
        AutopilotRepeatRequest.prompt_level == prompt_level,
    ).order_by(AutopilotRepeatRequest.repeat_ordinal).with_for_update()))


def route_explicit_repeat(
    db: Session,
    *,
    session_id: str,
    capture_claim: evidence_ledger.CaptureClaim,
    match: repeat_intent.RepeatMatch,
    asr_confidence: float | None = None,
    asr_engine_version: str | None = None,
    bank: content.ItemBank | None = None,
    protocol: dict | None = None,
    now: datetime | None = None,
) -> RouteExplicitRepeatResult:
    """Dispose one claimed capture as an explicit repeat request.

    The first request in a logical slot issues a new TTS command whose
    canonical ``payload_json`` is a byte-for-byte copy of the predecessor
    command's ``payload_json`` — the same frozen ``speech_text``, not the same
    audio.  A second request in the same slot stops
    automation and waits for a researcher instead of replaying forever.  Either
    way no AttemptEvent is created, ``attempt_seq``/cue/prompt level are left
    untouched, and the triggering recording, receipt and ASR evidence survive.

    This is a provider-free transaction participant: it flushes but never
    commits, and the caller must roll back on :class:`AutopilotServiceError`.
    """
    observed_at = _utc_naive(now) if now is not None else _utc_now_naive()
    resolved_bank = bank if bank is not None else _session_week_bank(db, session_id)
    resolved_protocol = protocol or _default_protocol()

    state = db.exec(select(SessionAutopilotState).where(
        SessionAutopilotState.session_id == session_id,
    ).with_for_update()).first()
    if state is None or state.scope_key != P0A_SCOPE_KEY:
        _fail("autopilot_not_active", "当前场次没有 P0a 自动驾驶状态")
    if (state.mode != "autonomous" or state.status != "processing_attempt"
            or state.current_command_id is None):
        _fail("autopilot_repeat_not_current", "重复请求已不是当前处理目标")

    record = db.exec(select(RuntimeCommand).where(
        RuntimeCommand.id == state.current_command_id,
        RuntimeCommand.session_id == session_id,
    ).with_for_update()).first()
    if (record is None or record.kind != "record" or record.state != "succeeded"
            or record.scope_key != state.scope_key
            or record.control_generation != state.control_generation
            or record.runner_generation != state.runner_generation):
        _fail("autopilot_repeat_capture_invalid", "重复请求缺少已成功的当前录音命令")

    capture = db.exec(select(AttemptCaptureProcessing).where(
        AttemptCaptureProcessing.id == capture_claim.capture_id,
    ).with_for_update()).first()
    if (capture is None
            or capture.session_id != session_id
            or capture.record_command_id != record.id
            or capture.predecessor_command_id != record.predecessor_command_id
            or capture.raw_audio_id != record.expected_raw_audio_id
            or capture.item_id != record.item_id
            or capture.turn_seq != record.turn_seq
            or capture.proof_attempt_seq != capture_claim.proof_attempt_seq
            or capture.proof_prompt_level != capture_claim.proof_prompt_level
            or capture.processing_status != "received"):
        _fail("autopilot_repeat_capture_invalid", "重复请求采集处理行与当前录音命令不一致")
    # The admission marker, not the mere presence of a binding, decides whether
    # this row may take the repeat path at all.  Both the persisted row and the
    # claim the worker has been holding across provider I/O must say
    # ``repeat_bound`` and must agree with each other, so a capture admitted
    # before the protocol existed can never be routed as a repeat request.
    if (capture.repeat_admission_semantics
            != evidence_ledger.REPEAT_BOUND_ADMISSION
            or capture_claim.repeat_admission_semantics
            != evidence_ledger.REPEAT_BOUND_ADMISSION
            or capture.repeat_admission_semantics
            != capture_claim.repeat_admission_semantics):
        _fail(
            "autopilot_repeat_admission_not_bound",
            "采集未按重复请求协议准入，禁止走重播路径",
        )
    # A capture with no frozen binding predates the protocol; it must stay on
    # the pre-repeat answer flow rather than opt in halfway through a session.
    if (capture.repeat_protocol_version_id is None
            or capture.repeat_protocol_definition_digest is None
            or capture.repeat_protocol_version_id != match.protocol_version_id
            or capture.repeat_protocol_definition_digest
            != match.protocol_definition_digest
            or capture.repeat_protocol_version_id
            != capture_claim.repeat_protocol_version_id
            or capture.repeat_protocol_definition_digest
            != capture_claim.repeat_protocol_definition_digest):
        _fail(
            "autopilot_repeat_binding_missing",
            "采集缺少冻结的重复请求协议绑定，禁止走重播路径",
        )

    gate = _require_gate(
        db,
        session_id,
        bank=resolved_bank,
        protocol=resolved_protocol,
        now=observed_at,
        position_item_id=record.item_id,
        position_turn_seq=record.turn_seq,
    )
    _validate_command_identity(record, gate.selected)
    _require_command_repeat_binding(record, gate.train_session)
    if (gate.train_session.repeat_protocol_version_id
            != capture.repeat_protocol_version_id
            or gate.train_session.repeat_protocol_definition_digest
            != capture.repeat_protocol_definition_digest):
        _fail(
            "autopilot_repeat_binding_missing",
            "采集绑定的重复请求协议与场次不一致",
        )

    try:
        proof = autopilot_ledger.verify_record_capture_for_attempt(db, record.id)
    except autopilot_ledger.AutopilotProofError as exc:
        raise AutopilotServiceError(
            "autopilot_record_capture_invalid",
            "重复请求路由前录音采集证据复核失败") from exc
    if (proof.session_id != session_id
            or proof.raw_audio_id != capture.raw_audio_id
            or proof.item_id != capture.item_id
            or proof.turn_seq != capture.turn_seq
            or proof.attempt_seq != capture.proof_attempt_seq
            or proof.prompt_level != capture.proof_prompt_level):
        _fail("autopilot_repeat_capture_invalid", "重复请求与 capture proof 不精确匹配")

    source = _require_repeat_source_tts(
        db, state=state, record=record, gate=gate)
    source_payload_sha256 = hashlib.sha256(
        source.payload_json.encode("utf-8")).hexdigest()

    if db.exec(select(AutopilotRepeatRequest).where(
            AutopilotRepeatRequest.capture_processing_id == capture.id,
    ).with_for_update()).first() is not None:
        _fail(
            "autopilot_repeat_idempotency_conflict",
            "该采集已有重复请求账本，但采集本身仍未终态",
        )

    prior = _repeat_slot_requests(
        db,
        session_id=session_id,
        item_id=capture.item_id,
        turn_seq=capture.turn_seq,
        attempt_seq=capture.proof_attempt_seq,
        prompt_level=capture.proof_prompt_level,
    )
    if not prior:
        ordinal = 1
    elif (len(prior) == 1 and prior[0].repeat_ordinal == 1
            and prior[0].outcome == "replayed"):
        ordinal = 2
    else:
        _fail(
            "autopilot_repeat_limit_exhausted",
            "该环节的重复请求账本已达上限或状态不一致",
        )

    if ordinal == 1:
        return _issue_repeat_replay(
            db,
            state=state,
            gate=gate,
            record=record,
            source=source,
            capture=capture,
            capture_claim=capture_claim,
            match=match,
            source_payload_sha256=source_payload_sha256,
            asr_confidence=asr_confidence,
            asr_engine_version=asr_engine_version,
            observed_at=observed_at,
        )
    return _pause_at_repeat_limit(
        db,
        state=state,
        gate=gate,
        record=record,
        source=source,
        capture=capture,
        capture_claim=capture_claim,
        match=match,
        first_request=prior[0],
        source_payload_sha256=source_payload_sha256,
        asr_confidence=asr_confidence,
        asr_engine_version=asr_engine_version,
        observed_at=observed_at,
    )


def _repeat_request_row(
    *,
    capture: AttemptCaptureProcessing,
    record: RuntimeCommand,
    source: RuntimeCommand,
    match: repeat_intent.RepeatMatch,
    ordinal: int,
    outcome: Literal["replayed", "limit_paused"],
    source_payload_sha256: str,
    replay_command_id: int | None,
    pause_control_event_seq: int | None,
    asr_confidence: float | None,
    asr_engine_version: str | None,
    observed_at: datetime,
) -> AutopilotRepeatRequest:
    return AutopilotRepeatRequest(
        capture_processing_id=capture.id,
        session_id=capture.session_id,
        item_id=capture.item_id,
        turn_seq=capture.turn_seq,
        attempt_seq=capture.proof_attempt_seq,
        prompt_level=capture.proof_prompt_level,
        repeat_ordinal=ordinal,
        outcome=outcome,
        record_command_id=record.id,
        raw_audio_id=capture.raw_audio_id,
        source_tts_command_id=source.id,
        source_payload_sha256=source_payload_sha256,
        replay_command_id=replay_command_id,
        pause_control_event_seq=pause_control_event_seq,
        asr_confidence=asr_confidence,
        asr_engine_version=asr_engine_version,
        repeat_protocol_version_id=capture.repeat_protocol_version_id,
        repeat_protocol_definition_digest=(
            capture.repeat_protocol_definition_digest),
        phrase_key=match.phrase_key,
        normalized_text_sha256=match.normalized_text_sha256,
        created_at=observed_at,
        is_simulation=capture.is_simulation,
    )


def _terminalize_repeat_capture(
    db: Session,
    *,
    capture: AttemptCaptureProcessing,
    capture_claim: evidence_ledger.CaptureClaim,
    request: AutopilotRepeatRequest,
    disposition: Literal["repeat_replayed", "repeat_limit_paused"],
    asr_confidence: float | None,
    asr_engine_version: str | None,
    observed_at: datetime,
) -> None:
    """Bind the capture to its repeat request; never to an AttemptEvent."""
    if request.id is None:  # pragma: no cover - flush postcondition
        _fail("autopilot_state_invalid", "重复请求账本未能持久化")
    if not evidence_ledger.fenced_capture_update(
        db,
        capture_claim,
        next_status="asr_completed",
        values={
            "asr_confidence": asr_confidence,
            "asr_engine_version": asr_engine_version,
            "disposition": disposition,
            "processed_at": observed_at,
            "repeat_request_id": request.id,
        },
    ):
        _fail("autopilot_repeat_capture_cas_conflict", "重复请求采集终态 CAS 已失效")
    db.expire(capture)


def _issue_repeat_replay(
    db: Session,
    *,
    state: SessionAutopilotState,
    gate: _P0aGate,
    record: RuntimeCommand,
    source: RuntimeCommand,
    capture: AttemptCaptureProcessing,
    capture_claim: evidence_ledger.CaptureClaim,
    match: repeat_intent.RepeatMatch,
    source_payload_sha256: str,
    asr_confidence: float | None,
    asr_engine_version: str | None,
    observed_at: datetime,
) -> RouteExplicitRepeatResult:
    """Replay command -> repeat ledger -> capture terminal -> state CAS."""
    session_id = state.session_id
    active_capability = _lock_active_attempt_route_device(
        db,
        session_id=session_id,
        expected=gate.active_capability,
        now=observed_at,
    )
    replay_key = repeat_replay_command_key(session_id, capture.id)
    if _command_by_key(db, session_id, replay_key) is not None:
        _fail(
            "autopilot_repeat_idempotency_conflict",
            "重播命令已存在但采集仍未终态",
        )

    owner = "repeat-replay-" + autopilot_ledger.new_lease_owner()
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
        _fail("autopilot_repeat_route_cas_conflict", "重复请求路由状态 CAS 已失效")
    db.expire(state)
    claimed_state = db.get(SessionAutopilotState, session_id)
    if claimed_state is None:  # pragma: no cover - locked-row invariant
        _fail("autopilot_state_invalid", "自动驾驶状态已消失")
    try:
        claim = autopilot_ledger.claim_from_autopilot_state(claimed_state)
    except ValueError as exc:  # pragma: no cover - helper postcondition
        raise AutopilotServiceError(
            "autopilot_repeat_route_cas_conflict", "重复请求路由租约事实不完整") from exc

    replay = RuntimeCommand(
        idempotency_key=replay_key,
        session_id=session_id,
        command_seq=claimed_state.next_command_seq,
        item_id=source.item_id,
        turn_seq=source.turn_seq,
        turn_key=source.turn_key,
        attempt_seq=source.attempt_seq,
        prompt_level=source.prompt_level,
        item_bank_version_id=source.item_bank_version_id,
        item_bank_definition_digest=source.item_bank_definition_digest,
        autopilot_protocol_version_id=source.autopilot_protocol_version_id,
        autopilot_protocol_definition_digest=(
            source.autopilot_protocol_definition_digest),
        response_role=source.response_role,
        repeat_protocol_version_id=source.repeat_protocol_version_id,
        repeat_protocol_definition_digest=(
            source.repeat_protocol_definition_digest),
        scope_key=P0A_SCOPE_KEY,
        control_generation=claim.control_generation,
        runner_generation=claim.runner_generation,
        issued_capability_token_hash=active_capability.token_hash,
        issued_device_id_hash=active_capability.device_id_hash,
        issued_at=observed_at,
        kind="tts",
        state="pending",
        # Byte-for-byte copy of the predecessor command's canonical
        # payload_json.  Re-generating it from today's item bank could
        # substitute another currently-valid line.  The audio itself is
        # synthesized again downstream; nothing here binds WAV bytes.
        payload_json=source.payload_json,
        replay_source_command_id=source.id,
        replay_ordinal=1,
        replay_source_payload_sha256=source_payload_sha256,
        created_at=observed_at,
        updated_at=observed_at,
    )
    db.add(replay)
    db.flush()
    if replay.id is None:  # pragma: no cover - SQLAlchemy invariant
        _fail("autopilot_state_invalid", "重播 TTS 命令未能持久化")
    if (replay.payload_json != source.payload_json
            or hashlib.sha256(replay.payload_json.encode("utf-8")).hexdigest()
            != source_payload_sha256):
        _fail("autopilot_repeat_replay_payload_invalid", "重播载荷不是源命令逐字节副本")

    request = _repeat_request_row(
        capture=capture,
        record=record,
        source=source,
        match=match,
        ordinal=1,
        outcome="replayed",
        source_payload_sha256=source_payload_sha256,
        replay_command_id=replay.id,
        pause_control_event_seq=None,
        asr_confidence=asr_confidence,
        asr_engine_version=asr_engine_version,
        observed_at=observed_at,
    )
    db.add(request)
    db.flush()

    _terminalize_repeat_capture(
        db,
        capture=capture,
        capture_claim=capture_claim,
        request=request,
        disposition="repeat_replayed",
        asr_confidence=asr_confidence,
        asr_engine_version=asr_engine_version,
        observed_at=observed_at,
    )

    if not autopilot_ledger.fenced_autopilot_update(
        db,
        claim,
        values={
            "status": "waiting_tts",
            "current_command_id": replay.id,
            "next_command_seq": claimed_state.next_command_seq + 1,
            "last_error_code": None,
        },
        release_lease=True,
        now=observed_at,
    ):
        _fail("autopilot_repeat_route_cas_conflict", "重复请求发行 CAS 已失效")
    db.flush()
    db.expire(claimed_state)
    final_state = db.get(SessionAutopilotState, session_id)
    if final_state is None:  # pragma: no cover - locked-row invariant
        _fail("autopilot_state_invalid", "自动驾驶状态已消失")
    # The patient device must still see an ordinary, strictly validated command.
    _project_command(
        db, replay, gate.selected, expected_capability=active_capability)
    return RouteExplicitRepeatResult(
        outcome="replayed",
        repeat_ordinal=1,
        state_revision=final_state.revision,
    )


def _pause_at_repeat_limit(
    db: Session,
    *,
    state: SessionAutopilotState,
    gate: _P0aGate,
    record: RuntimeCommand,
    source: RuntimeCommand,
    capture: AttemptCaptureProcessing,
    capture_claim: evidence_ledger.CaptureClaim,
    match: repeat_intent.RepeatMatch,
    first_request: AutopilotRepeatRequest,
    source_payload_sha256: str,
    asr_confidence: float | None,
    asr_engine_version: str | None,
    observed_at: datetime,
) -> RouteExplicitRepeatResult:
    """Pause event -> ordinal 2 ledger -> capture terminal -> control CAS."""
    session_id = state.session_id
    # Same device fence as the replay path: a rotation committed by another
    # process between the gate read and this transaction must lose here too,
    # before any event, ledger row or state change is written.
    _lock_active_attempt_route_device(
        db,
        session_id=session_id,
        expected=gate.active_capability,
        now=observed_at,
    )
    if (first_request.replay_command_id is None
            or first_request.replay_command_id != source.id
            or first_request.record_command_id == record.id
            or first_request.capture_processing_id == capture.id):
        _fail(
            "autopilot_repeat_slot_history_invalid",
            "第二次重复请求与已成功重播的账本事实不一致",
        )
    pause_key = repeat_limit_event_key(session_id, capture.id)
    if db.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.idempotency_key == pause_key,
    )).first() is not None:
        _fail(
            "autopilot_repeat_idempotency_conflict",
            "重复请求上限暂停事实已存在但采集仍未终态",
        )

    event_seq = _next_control_event_seq(db, session_id)
    event = AutopilotControlEvent(
        idempotency_key=pause_key,
        session_id=session_id,
        event_seq=event_seq,
        event_type="pause",
        scope_key=state.scope_key,
        control_generation=state.control_generation,
        runner_generation=state.runner_generation,
        command_id=record.id,
        actor_type="system",
        actor_id=None,
        reason_code=REPEAT_LIMIT_REASON_CODE,
        from_mode="autonomous",
        to_mode="autonomous",
        from_status="processing_attempt",
        to_status="paused",
        payload_json=autopilot_ledger.encode_control_event_payload(
            "pause", {
                "reason_code": REPEAT_LIMIT_REASON_CODE,
                "source": REPEAT_PAUSE_SOURCE,
            }),
        created_at=observed_at,
    )
    db.add(event)
    db.flush()

    request = _repeat_request_row(
        capture=capture,
        record=record,
        source=source,
        match=match,
        ordinal=2,
        outcome="limit_paused",
        source_payload_sha256=source_payload_sha256,
        replay_command_id=None,
        pause_control_event_seq=event.event_seq,
        asr_confidence=asr_confidence,
        asr_engine_version=asr_engine_version,
        observed_at=observed_at,
    )
    db.add(request)
    db.flush()
    bound_event = db.exec(select(AutopilotControlEvent).where(
        AutopilotControlEvent.session_id == request.session_id,
        AutopilotControlEvent.event_seq == request.pause_control_event_seq,
    )).first()
    if (bound_event is None or bound_event.id != event.id
            or bound_event.event_type != "pause"
            or bound_event.command_id != record.id
            or bound_event.reason_code != REPEAT_LIMIT_REASON_CODE
            or bound_event.control_generation != state.control_generation
            or bound_event.runner_generation != state.runner_generation):
        _fail(
            "autopilot_repeat_pause_binding_invalid",
            "重复请求账本与暂停事实的双向指针不一致",
        )

    _terminalize_repeat_capture(
        db,
        capture=capture,
        capture_claim=capture_claim,
        request=request,
        disposition="repeat_limit_paused",
        asr_confidence=asr_confidence,
        asr_engine_version=asr_engine_version,
        observed_at=observed_at,
    )

    result = db.execute(
        update(SessionAutopilotState)
        .where(
            SessionAutopilotState.session_id == session_id,
            SessionAutopilotState.scope_key == state.scope_key,
            SessionAutopilotState.mode == "autonomous",
            SessionAutopilotState.status == "processing_attempt",
            SessionAutopilotState.current_command_id == record.id,
            SessionAutopilotState.control_generation == state.control_generation,
            SessionAutopilotState.runner_generation == state.runner_generation,
            SessionAutopilotState.revision == state.revision,
        )
        .values(
            status="paused",
            current_command_id=None,
            revision=SessionAutopilotState.revision + 1,
            last_error_code=REPEAT_LIMIT_REASON_CODE,
            lease_owner=None,
            lease_acquired_at=None,
            lease_expires_at=None,
            updated_at=observed_at,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        _fail("autopilot_repeat_route_cas_conflict", "重复请求上限暂停 CAS 已失效")
    db.flush()
    db.expire(state)
    final_state = db.get(SessionAutopilotState, session_id)
    if final_state is None:  # pragma: no cover - locked-row invariant
        _fail("autopilot_state_invalid", "自动驾驶状态已消失")
    return RouteExplicitRepeatResult(
        outcome="limit_paused",
        repeat_ordinal=2,
        state_revision=final_state.revision,
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
    resolved_bank = bank if bank is not None else _session_week_bank(db, session_id)
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
    _require_command_repeat_binding(command, train_session)

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
        # Capture admission is the point where the frozen repeat binding is
        # copied forward.  An unbound or drifted command must never reach it:
        # a NULL copied into a capture would make a later "再说一遍" score as
        # an answer under the legacy flow.
        _require_command_repeat_binding(command, gate.train_session)
        state.status = "processing_attempt"
        state.current_command_id = command.id
        state.revision += 1
        state.last_error_code = None
        state.updated_at = observed_at
        db.add(state)
        db.flush()
        try:
            proof = autopilot_ledger.verify_record_capture_for_attempt(db, command.id)
        except autopilot_ledger.AutopilotProofError as exc:
            raise AutopilotServiceError(
                "autopilot_record_capture_invalid",
                "录音回执与服务端采集证据不一致",
            ) from exc
        # Admit the persistent capture-processing claim in this same transaction,
        # before any ASR runs. R1-foundation: this decouples audio-capture
        # admission from AttemptEvent/attempt_seq creation, which still happens
        # only after ASR resolves (see evidence_ledger.ensure_capture_processing).
        evidence_ledger.ensure_capture_processing(
            db,
            record_command_id=proof.command_id,
            predecessor_command_id=command.predecessor_command_id,
            receipt_server_seq=proof.receipt_server_seq,
            raw_audio_id=proof.raw_audio_id,
            session_id=proof.session_id,
            item_id=proof.item_id,
            turn_seq=proof.turn_seq,
            proof_attempt_seq=proof.attempt_seq,
            proof_prompt_level=proof.prompt_level,
            repeat_protocol_version_id=command.repeat_protocol_version_id,
            repeat_protocol_definition_digest=(
                command.repeat_protocol_definition_digest),
            is_simulation=gate.train_session.is_simulation,
            now=observed_at,
        )
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
