"""Server-authoritative VisitPlan state machine and atomic session launch."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import hashlib
import json
import os
import secrets
from typing import Literal, NoReturn
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_
from sqlmodel import Session, select

from . import (
    autopilot_plan_profiles, content, repeat_intent, session_admission)
from .models import (
    AssessmentEvent,
    Patient,
    Session as TrainSession,
    SessionCloseoutReport,
    SessionRuntimeState,
    VisitPlan,
    VisitPlanCommand,
)
from .visit_plan_contract import (
    VisitPlanCancelIn,
    VisitPlanCreateIn,
    VisitPlanMutationIn,
    VisitPlanReceipt,
    VisitPlanTodayOut,
)


class VisitPlanError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _fail(status_code: int, code: str, message: str) -> NoReturn:
    raise VisitPlanError(status_code, code, message)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _research_today() -> date:
    timezone_name = os.environ.get("RESEARCH_TIMEZONE", "Asia/Shanghai").strip()
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        _fail(500, "visit_plan_timezone_invalid", "研究时区配置无效")
    return datetime.now(zone).date()


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _json_value(value: object) -> object:
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    raw = getattr(value, "value", value)
    return raw


def _request_hash(command_type: str, payload: dict[str, object]) -> str:
    canonical = json.dumps(
        {
            "command_type": command_type,
            **{key: _json_value(value) for key, value in payload.items()},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def _protocol_slot_key_for_values(
    *,
    patient_id: str,
    session_sitting_no: int,
    week_no: int,
    phase_type: object,
    event_line: object,
) -> str:
    canonical = "\x00".join((
        patient_id,
        str(session_sitting_no),
        str(week_no),
        _enum_value(phase_type),
        _enum_value(event_line),
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _protocol_slot_key(body: VisitPlanCreateIn) -> str:
    return _protocol_slot_key_for_values(
        patient_id=body.patient_id,
        session_sitting_no=body.session_sitting_no,
        week_no=body.week_no,
        phase_type=body.phase_type,
        event_line=body.event_line,
    )


def _bank_week_for(week_no: int) -> int:
    """周 1 关系建立不消耗题库，版本绑定锚定平台默认题库（周 2）。"""
    return week_no if week_no >= 2 else content.RAPPORT_ANCHOR_WEEK


def _load_bank(week_no: int, *, allow_draft_anchor: bool = False) -> content.ItemBank:
    """Load the bank for one plan's week; the per-week bank is its frozen 训练计划.

    ``allow_draft_anchor`` keeps the deliberate draft/approve split: scheduling
    facts for a not-yet-structured week may still be drafted (anchored to the
    platform default bank), but approve/start never falls back — the missing
    week surfaces as the honest content blocker.
    """
    try:
        return content.load_item_bank_for_week(_bank_week_for(week_no))
    except content.TrainingWeekContentUnavailable as exc:
        if allow_draft_anchor:
            return content.load_item_bank_for_week(content.RAPPORT_ANCHOR_WEEK)
        _fail(409, "visit_plan_content_unavailable", str(exc))


def _load_protocol() -> dict:
    return content.load_autopilot_protocol(
        content.CONTENT_DIR / "autopilot_protocol_v1.json")


def _active_repeat_protocol() -> repeat_intent.RepeatIntentProtocol:
    try:
        return repeat_intent.active_protocol()
    except content.FrozenContentUnavailable as exc:
        _fail(
            409,
            "visit_plan_repeat_protocol_unavailable",
            f"当前重复请求协议不可用：{exc}",
        )


def _repeat_binding_hash_fields() -> dict[str, str]:
    """Bind every plan command to the repeat definition in force when it ran.

    A replay issued across a protocol version change therefore fails the
    request-hash comparison instead of silently reusing the old receipt.
    """
    protocol = _active_repeat_protocol()
    return {
        "repeat_protocol_version_id": protocol.version_id,
        "repeat_protocol_definition_digest": protocol.definition_digest,
    }


# Exactly the seven frozen core codes.  Nothing else may become a public
# service code: an unrecognised core code collapses to the generic invalid
# code rather than minting a new one from string surgery.
_PROFILE_CODE_MAP: dict[str, tuple[str, int]] = {
    "plan_profile_binding_incomplete": (
        "visit_plan_profile_binding_incomplete", 409),
    "plan_profile_unknown": ("visit_plan_profile_unknown", 422),
    "plan_profile_digest_mismatch": (
        "visit_plan_profile_digest_mismatch", 409),
    "plan_profile_parent_mismatch": (
        "visit_plan_profile_parent_mismatch", 409),
    "plan_profile_invalid": ("visit_plan_profile_invalid", 409),
    "plan_profile_simulation_required": (
        "visit_plan_profile_simulation_required", 409),
    "plan_profile_context_mismatch": (
        "visit_plan_profile_context_mismatch", 422),
}


def _fail_profile(exc: autopilot_plan_profiles.PlanProfileError) -> NoReturn:
    """Map one structured core code; never classify by message text."""
    mapped, status = _PROFILE_CODE_MAP.get(
        exc.code, ("visit_plan_profile_invalid", 409))
    _fail(status, mapped, exc.message)


def _profile_binding_hash_fields(
    version_id: str | None,
    definition_digest: str | None,
) -> dict[str, str]:
    """Paired-null contributes nothing, keeping the legacy payload byte-exact.

    A half pair can never be hashed: callers must have passed it through
    :func:`_plan_profile_pair` first, and this raises rather than serialising
    the literal string ``"None"`` into an authoritative request hash.
    """
    if version_id is None and definition_digest is None:
        return {}
    if version_id is None or definition_digest is None:
        _fail(
            409,
            "visit_plan_profile_binding_incomplete",
            "自动演示计划 version/digest 必须成对存在，半绑定不可参与摘要",
        )
    return {
        "autopilot_profile_version_id": version_id,
        "autopilot_profile_definition_digest": definition_digest,
    }


def _plan_profile_pair(row: object) -> tuple[str | None, str | None]:
    """The one structural validator: only (None, None) and (set, set) exist.

    Every profile hash, resolution, runtime decision and projection goes
    through here first, so a half pair written past the database CHECK by a
    corrupted restore can never be treated as canonical or stringified into a
    hash payload.
    """
    version = getattr(row, "autopilot_profile_version_id", None)
    digest = getattr(row, "autopilot_profile_definition_digest", None)
    if (version is None) != (digest is None):
        _fail(
            409,
            "visit_plan_profile_binding_incomplete",
            "训练安排的自动演示计划 version/digest 必须成对存在",
        )
    return (version, digest)


def _definition_pair(definition: object) -> tuple[str | None, str | None]:
    if definition is None:
        return (None, None)
    return (
        definition.profile_version_id,
        definition.profile_definition_digest,
    )


def _resolved_request_profile(version_id: str | None):
    """Parse one explicitly requested definition against one bundle read.

    Returns ``(definition, bank, binding)``.  The bundle is returned rather
    than discarded so the caller can persist from the very same read that
    validated the profile, instead of taking a third unverified snapshot.

    A NULL selector never reaches the demo registry, so an unsupported
    canonical week keeps its existing create-then-block-at-approve behaviour
    instead of being refused early by a Week-2 resolver.
    """
    if version_id is None:
        return None, None, None
    # Demo plan profiles are Week-2-typed by schema literal; resolve them
    # against the Week-2 canonical bundle, then the profile context assert
    # rejects any non-Week-2 request explicitly.
    bank, protocol, binding = _current_definition_bundle(2)
    try:
        definition = autopilot_plan_profiles.resolve_requested_definition(
            version_id, bank=bank, protocol=protocol)
    except autopilot_plan_profiles.PlanProfileError as exc:
        _fail_profile(exc)
    return definition, bank, binding


def _same_resolution(first: object, second: object) -> bool:
    """Identity plus all four canonical parents must reproduce exactly."""
    return (
        _definition_pair(first) == _definition_pair(second)
        and first.parent_item_bank_version_id
        == second.parent_item_bank_version_id
        and first.parent_item_bank_definition_digest
        == second.parent_item_bank_definition_digest
        and first.parent_autopilot_protocol_version_id
        == second.parent_autopilot_protocol_version_id
        and first.parent_autopilot_protocol_definition_digest
        == second.parent_autopilot_protocol_definition_digest
    )


def _assert_bound_profile_identity(plan: VisitPlan) -> None:
    """Resolve a stored pair against its immutable registered definition.

    Resolved without today's bank/protocol on purpose.  The definition's four
    recorded parent identities are compared directly to the plan's own frozen
    columns, so a valid historical demo row is never dragged onto a future
    active bank or protocol just because the canonical content moved on.
    """
    version, digest = _plan_profile_pair(plan)
    if version is None:
        return
    try:
        definition = autopilot_plan_profiles.resolve_registered_binding(
            version, digest)
        assert definition is not None  # a set pair always resolves or raises
        autopilot_plan_profiles.assert_profile_subject_boundary(
            definition, is_simulation=plan.is_simulation)
        autopilot_plan_profiles.assert_profile_context_boundary(
            definition,
            week_no=plan.week_no,
            phase_type=plan.phase_type,
            event_line=plan.event_line,
        )
    except autopilot_plan_profiles.PlanProfileError as exc:
        _fail_profile(exc)
    if (
        definition.parent_item_bank_version_id != plan.item_bank_version_id
        or definition.parent_item_bank_definition_digest
        != plan.item_bank_definition_digest
        or definition.parent_autopilot_protocol_version_id
        != plan.autopilot_protocol_version_id
        or definition.parent_autopilot_protocol_definition_digest
        != plan.autopilot_protocol_definition_digest
    ):
        _fail(
            409,
            "visit_plan_profile_parent_mismatch",
            "历史自动演示计划绑定与其冻结题库/协议身份不一致",
        )


def _assert_profile_context(
    definition: object,
    *,
    week_no: int,
    phase_type: object,
    event_line: object,
) -> None:
    try:
        autopilot_plan_profiles.assert_profile_context_boundary(
            definition,
            week_no=week_no,
            phase_type=phase_type,
            event_line=event_line,
        )
    except autopilot_plan_profiles.PlanProfileError as exc:
        _fail_profile(exc)


def _assert_profile_subject(definition: object, *, is_simulation: bool) -> None:
    try:
        autopilot_plan_profiles.assert_profile_subject_boundary(
            definition, is_simulation=is_simulation)
    except autopilot_plan_profiles.PlanProfileError as exc:
        _fail_profile(exc)


def _profile_runtime_not_enabled() -> NoReturn:
    _fail(
        409,
        "visit_plan_profile_runtime_not_enabled",
        "20 题模拟演示计划尚未开放审核、开场与运行；当前只能保存或取消草稿",
    )


def _profile_runtime_enabled() -> bool:
    """Return whether the explicitly simulation-only demo may run now.

    Creating a draft remains possible without either switch.  Approve/start
    requires the same two deployment-owned switches as P0a itself, so a demo
    plan can never become an actionable bedside session in an ordinary or
    research process by accident.
    """
    return autopilot_plan_profiles.demo20_runtime_enabled()


def _assert_profile_mutation_allowed(plan: VisitPlan) -> None:
    """Allow a bound demo only in an explicitly enabled simulation process."""
    if (
        _plan_profile_pair(plan) != (None, None)
        and not _profile_runtime_enabled()
    ):
        _profile_runtime_not_enabled()


def _assert_caregiver_operational_demo20(
    plan: VisitPlan,
    *,
    bank: content.ItemBank,
    protocol: dict,
) -> autopilot_plan_profiles.ResolvedAutopilotPlan:
    """Collapse every D1B mismatch into one bedside-safe refusal.

    The core resolver remains the sole semantic authority.  The caregiver API
    intentionally does not reveal whether a hidden plan was canonical, had a
    malformed profile binding, drifted content, or merely lost a deployment
    switch.
    """
    try:
        return autopilot_plan_profiles.resolve_exact_runnable_demo20(
            plan, bank=bank, protocol=protocol)
    except autopilot_plan_profiles.PlanProfileError:
        _fail(
            409,
            "caregiver_plan_not_operational_demo20",
            "这项安排当前不是可开始的 20 题本机模拟练习，请刷新今日工作台",
        )


def _assert_profile_projectable(plan: VisitPlan, status: str) -> None:
    """Validate the stored pair without turning feature flags into a tombstone.

    Feature switches gate new approve/start and new autonomous ownership.  An
    already-approved or started demo must remain readable, abortable and
    closeable after a restart with the switches removed.
    """
    pair = _plan_profile_pair(plan)
    if pair != (None, None) and status in {"approved", "started"}:
        _assert_bound_profile_identity(plan)


def _assert_plan_profile_matches_request(
    plan: VisitPlan,
    expected: tuple[str | None, str | None],
    *,
    resolve_definition: bool,
) -> None:
    """Replay may only answer for the exact authoritative binding.

    Cancel deliberately passes ``resolve_definition=False``: a drifted or
    missing manifest must never strand a bad draft on its protocol slot.
    """
    if _plan_profile_pair(plan) != expected:
        _fail(
            409,
            "visit_plan_profile_digest_mismatch",
            "训练安排已存的自动演示计划绑定与本次请求事实不一致",
        )
    if resolve_definition:
        _assert_bound_profile_identity(plan)


def _assert_command_key_available(
    db: Session,
    *,
    idempotency_key: str,
    command_type: str,
    actor_id: str,
) -> None:
    """Pre-lock lookup that can only reject; it never projects a receipt.

    The authoritative mutation hash depends on the plan's stored profile pair,
    which is unknown until the row is locked, so no success may be returned
    here.
    """
    command = db.exec(select(VisitPlanCommand).where(
        VisitPlanCommand.idempotency_key == idempotency_key,
    )).first()
    if command is None:
        return
    if command.command_type != command_type or command.actor_id != actor_id:
        _fail(409, "visit_plan_idempotency_conflict", "幂等键已被另一个安排事实使用")


@dataclass(frozen=True)
class _DefinitionBinding:
    item_bank_version_id: str
    item_bank_definition_digest: str
    autopilot_protocol_version_id: str
    autopilot_protocol_definition_digest: str
    repeat_protocol_version_id: str
    repeat_protocol_definition_digest: str


def _current_definition_bundle(
    week_no: int, *, allow_draft_anchor: bool = False,
) -> tuple[content.ItemBank, dict, _DefinitionBinding]:
    bank = _load_bank(week_no, allow_draft_anchor=allow_draft_anchor)
    protocol = _load_protocol()
    protocol_issues = content.validate_autopilot_protocol(protocol)
    if protocol_issues:
        _fail(
            409,
            "visit_plan_protocol_invalid",
            "当前自动化协议不可执行：" + "；".join(protocol_issues),
        )
    repeat_protocol = _active_repeat_protocol()
    return bank, protocol, _DefinitionBinding(
        item_bank_version_id=bank.version_id,
        item_bank_definition_digest=content.item_bank_definition_digest(bank),
        autopilot_protocol_version_id=str(protocol["protocol_version_id"]),
        autopilot_protocol_definition_digest=(
            content.autopilot_protocol_definition_digest(protocol)),
        repeat_protocol_version_id=repeat_protocol.version_id,
        repeat_protocol_definition_digest=repeat_protocol.definition_digest,
    )


def _assert_plan_definition_binding(
    plan: VisitPlan,
    binding: _DefinitionBinding,
) -> None:
    values = (
        plan.item_bank_definition_digest,
        plan.autopilot_protocol_version_id,
        plan.autopilot_protocol_definition_digest,
    )
    if any(value is None or not str(value).strip() for value in values):
        _fail(
            409,
            "visit_plan_definition_binding_missing",
            "训练安排来自旧数据，缺少完整题库/自动化协议定义绑定，必须重建安排",
        )
    if plan.item_bank_version_id != binding.item_bank_version_id:
        _fail(
            409,
            "visit_plan_content_version_mismatch",
            "训练安排绑定的题库版本与当前版本不一致，须取消后按当前题库重建安排"
            "（未结构化周的早期草稿锚定平台默认题库，该周材料登记后出现此提示属预期）",
        )
    if plan.item_bank_definition_digest != binding.item_bank_definition_digest:
        _fail(
            409,
            "visit_plan_content_digest_mismatch",
            "题库内容已变化，即使版本号未变也不能继续使用该训练安排",
        )
    if plan.autopilot_protocol_version_id != binding.autopilot_protocol_version_id:
        _fail(
            409,
            "visit_plan_protocol_version_mismatch",
            "训练安排绑定的自动化协议版本与当前版本不一致",
        )
    if (plan.autopilot_protocol_definition_digest
            != binding.autopilot_protocol_definition_digest):
        _fail(
            409,
            "visit_plan_protocol_digest_mismatch",
            "自动化协议内容已变化，即使版本号未变也不能继续使用该训练安排",
        )
    _assert_plan_repeat_binding(plan)


def _assert_plan_repeat_binding(plan: VisitPlan) -> None:
    """Re-verify a frozen repeat binding against the historical registry.

    A plan created before the repeat protocol existed carries no binding and is
    refused: it may not be approved or started, because the resulting session
    would run automation without frozen repeat semantics.  Those plans must be
    recreated, not silently opted in.  A plan that did freeze a version must
    still resolve to that exact historical definition — this check deliberately
    does not force it onto today's active version.
    """
    version_id = plan.repeat_protocol_version_id
    digest = plan.repeat_protocol_definition_digest
    if version_id is None or digest is None:
        _fail(
            409,
            "visit_plan_repeat_binding_missing",
            "训练安排缺少冻结的重复请求协议绑定；旧安排必须重建后才能审批或开场",
        )
    try:
        repeat_intent.protocol_for_binding(version_id, digest)
    except content.FrozenContentUnavailable as exc:
        _fail(
            409,
            "visit_plan_repeat_protocol_unavailable",
            f"训练安排绑定的重复请求协议已不可用：{exc}",
        )


def _assert_session_copies_plan_binding(
    plan: VisitPlan,
    train_session: TrainSession,
) -> None:
    if (
        train_session.item_bank_version_id != plan.item_bank_version_id
        or train_session.item_bank_definition_digest
        != plan.item_bank_definition_digest
        or train_session.autopilot_protocol_version_id
        != plan.autopilot_protocol_version_id
        or train_session.autopilot_protocol_definition_digest
        != plan.autopilot_protocol_definition_digest
        or train_session.repeat_protocol_version_id
        != plan.repeat_protocol_version_id
        or train_session.repeat_protocol_definition_digest
        != plan.repeat_protocol_definition_digest
        or train_session.autopilot_profile_version_id
        != plan.autopilot_profile_version_id
        or train_session.autopilot_profile_definition_digest
        != plan.autopilot_profile_definition_digest
    ):
        _fail(
            409,
            "visit_plan_session_definition_mismatch",
            "场次没有逐项复制训练安排的题库/自动化协议定义绑定",
        )


def assert_started_profile_command_chain(
    db: Session,
    plan: VisitPlan,
    train_session: TrainSession,
) -> None:
    """Prove a paired-set runtime came from profile-bound create/approve/start.

    Matching mutable Plan/Session columns are not sufficient: without this
    append-only ledger proof a canonical history could be edited into the
    shorter simulation profile after it had already started.
    """
    profile_pair = _plan_profile_pair(plan)
    if profile_pair == (None, None):
        return
    _assert_bound_profile_identity(plan)
    _assert_session_copies_plan_binding(plan, train_session)
    commands = _assert_plan_operational_integrity(db, plan)
    if [row.command_type for row in commands] != ["create", "approve", "start"]:
        _fail(409, "visit_plan_profile_ledger_mismatch",
              "20 题模拟计划缺少完整的创建、审核与开场账本")
    create, approve, start = commands
    if (
        tuple(row.event_seq for row in commands) != (1, 2, 3)
        or tuple(row.expected_revision for row in commands) != (0, 1, 2)
        or tuple(row.resulting_revision for row in commands) != (1, 2, 3)
        or plan.status != "started"
        or plan.revision != 3
        or create.actor_id != plan.created_by
        or create.created_at != plan.created_at
        or approve.actor_id != plan.approved_by
        or approve.created_at != plan.approved_at
        or start.actor_id != plan.started_by
        or start.created_at != plan.started_at
    ):
        _fail(409, "visit_plan_profile_ledger_mismatch",
              "20 题模拟计划的命令账本与安排事实不一致")


def _linked_session(db: Session, plan_id: str) -> TrainSession | None:
    return db.exec(select(TrainSession).where(
        TrainSession.visit_plan_id == plan_id,
    )).first()


def _assert_plan_command_chain_state(
    db: Session,
    plan: VisitPlan,
) -> list[VisitPlanCommand]:
    """Prove the mutable plan projection matches its append-only commands.

    Feature flags may be removed after a demo started, but that must not make a
    hand-edited ``status`` or actor/timestamp tuple look like an authentic
    historical plan.  This check deliberately validates the generic command
    state for canonical and profile plans alike; the stricter profile runtime
    check below additionally recomputes the profile-bound request hashes.
    """
    commands = list(db.exec(
        select(VisitPlanCommand)
        .where(VisitPlanCommand.plan_id == plan.plan_id)
        .order_by(VisitPlanCommand.event_seq)
    ))
    if not commands:
        _fail(409, "visit_plan_state_invalid", "训练安排缺少命令账本")

    status = ""
    prior_revision = 0
    approved: VisitPlanCommand | None = None
    started: VisitPlanCommand | None = None
    cancelled: VisitPlanCommand | None = None
    for index, command in enumerate(commands, start=1):
        if (
            command.event_seq != index
            or command.expected_revision != prior_revision
            or command.resulting_revision != prior_revision + 1
        ):
            _fail(409, "visit_plan_state_invalid", "训练安排命令序列不连续")
        if index == 1:
            if command.command_type != "create":
                _fail(409, "visit_plan_state_invalid", "训练安排首个命令不是 create")
            status = "draft"
        elif status == "draft" and command.command_type == "approve":
            status = "approved"
            approved = command
        elif status in {"draft", "approved"} and command.command_type == "cancel":
            status = "cancelled"
            cancelled = command
        elif status == "approved" and command.command_type == "start":
            status = "started"
            started = command
        else:
            _fail(409, "visit_plan_state_invalid", "训练安排命令转移不合法")
        prior_revision = command.resulting_revision

    created = commands[0]
    if (
        plan.status != status
        or plan.revision != prior_revision
        or plan.created_by != created.actor_id
        or plan.created_at != created.created_at
        or plan.approved_by != (approved.actor_id if approved else None)
        or plan.approved_at != (approved.created_at if approved else None)
        or plan.started_by != (started.actor_id if started else None)
        or plan.started_at != (started.created_at if started else None)
        or plan.cancelled_by != (cancelled.actor_id if cancelled else None)
        or plan.cancelled_at != (cancelled.created_at if cancelled else None)
    ):
        _fail(409, "visit_plan_state_invalid", "训练安排与命令账本不一致")
    return commands


def _plan_repeat_hash_fields(plan: VisitPlan) -> dict[str, str]:
    version_id = plan.repeat_protocol_version_id
    digest = plan.repeat_protocol_definition_digest
    if version_id is None and digest is None:
        # Keep byte-exact projection for plans created before repeat binding
        # existed. They remain readable/cancellable; admission still refuses
        # them through ``_assert_plan_repeat_binding``.
        return {}
    if version_id is None or digest is None:
        _fail(409, "visit_plan_state_invalid", "训练安排的重复请求协议绑定不完整")
    return {
        "repeat_protocol_version_id": version_id,
        "repeat_protocol_definition_digest": digest,
    }


def _assert_non_cancelled_command_hashes(
    plan: VisitPlan,
    commands: list[VisitPlanCommand],
) -> None:
    """Bind the mutable Plan projection back to append-only commands.

    This runs for canonical and demo plans alike. Changing a plan's profile
    pair in either direction therefore cannot turn an old create command into
    a new plan mode before approve/start notices it.
    """
    if plan.status == "cancelled":
        return
    expected_slot_key = _protocol_slot_key_for_values(
        patient_id=plan.patient_id,
        session_sitting_no=plan.session_sitting_no,
        week_no=plan.week_no,
        phase_type=plan.phase_type,
        event_line=plan.event_line,
    )
    if plan.protocol_slot_key != expected_slot_key:
        _fail(409, "visit_plan_state_invalid", "训练安排的协议槽位与创建事实不一致")
    if plan.updated_at != commands[-1].created_at:
        _fail(409, "visit_plan_state_invalid", "训练安排的更新时间与最后命令不一致")

    repeat_fields = _plan_repeat_hash_fields(plan)
    profile_fields = _profile_binding_hash_fields(*_plan_profile_pair(plan))
    expected_hashes: list[str] = []
    observed_hashes: list[str] = []
    for command in commands:
        if command.command_type not in {"create", "approve", "start"}:
            continue
        if command.reason_code is not None:
            _fail(409, "visit_plan_state_invalid", "创建、审核或开场命令不应携带取消原因")
        if command.command_type == "create":
            expected_hash = _request_hash("create", {
                "idempotency_key": command.idempotency_key,
                "patient_id": plan.patient_id,
                "scheduled_date": plan.scheduled_date,
                "scheduled_time": plan.scheduled_time,
                "queue_order": plan.queue_order,
                "session_sitting_no": plan.session_sitting_no,
                "week_no": plan.week_no,
                "phase_type": plan.phase_type,
                "event_line": plan.event_line,
                **repeat_fields,
                **profile_fields,
            })
        else:
            expected_hash = _request_hash(command.command_type, {
                "plan_id": plan.plan_id,
                "idempotency_key": command.idempotency_key,
                "expected_revision": command.expected_revision,
                **repeat_fields,
                **profile_fields,
            })
        expected_hashes.append(expected_hash)
        observed_hashes.append(command.request_hash)
    if observed_hashes != expected_hashes:
        _fail(
            409,
            "visit_plan_state_invalid",
            "训练安排的命令摘要与当前计划模式不一致",
        )


def _assert_plan_operational_integrity(
    db: Session,
    plan: VisitPlan,
) -> list[VisitPlanCommand]:
    commands = _assert_plan_command_chain_state(db, plan)
    _assert_non_cancelled_command_hashes(plan, commands)
    return commands


def receipt_for(db: Session, plan: VisitPlan) -> VisitPlanReceipt:
    _assert_plan_operational_integrity(db, plan)
    _assert_profile_projectable(plan, plan.status)
    linked = _linked_session(db, plan.plan_id)
    if plan.status == "started" and linked is None:
        _fail(409, "visit_plan_state_invalid", "已启动安排缺少权威场次")
    if plan.status != "started" and linked is not None:
        _fail(409, "visit_plan_state_invalid", "未启动安排不应关联场次")
    if linked is not None:
        _assert_session_copies_plan_binding(plan, linked)
    return VisitPlanReceipt(
        plan_id=plan.plan_id,
        patient_id=plan.patient_id,
        scheduled_date=plan.scheduled_date,
        scheduled_time=plan.scheduled_time,
        queue_order=plan.queue_order,
        session_sitting_no=plan.session_sitting_no,
        week_no=plan.week_no,
        phase_type=plan.phase_type,
        event_line=plan.event_line,
        item_bank_version_id=plan.item_bank_version_id,
        autopilot_profile_version_id=plan.autopilot_profile_version_id,
        is_simulation=plan.is_simulation,
        data_classification=plan.data_classification,
        status=plan.status,
        revision=plan.revision,
        created_by=plan.created_by,
        created_at=plan.created_at,
        approved_by=plan.approved_by,
        approved_at=plan.approved_at,
        started_by=plan.started_by,
        started_at=plan.started_at,
        cancelled_by=plan.cancelled_by,
        cancelled_at=plan.cancelled_at,
        session_id=linked.session_id if linked is not None else None,
    )


def _receipt_for_viewer(
        db: Session, plan: VisitPlan, *,
        viewer_actor_id: str | None,
        viewer_role: str | None) -> VisitPlanReceipt:
    """Keep scheduling visible without indexing another operator's session."""
    receipt = receipt_for(db, plan)
    if (plan.status != "started"
            or viewer_role == "admin"
            or viewer_actor_id == "LOCAL-M0"):
        return receipt
    linked = db.get(TrainSession, receipt.session_id)
    if linked is not None and linked.trainer_id == viewer_actor_id:
        return receipt
    return receipt.model_copy(update={
        "session_id": None,
        "started_by": None,
    })


def _receipt_for_command(
    db: Session,
    *,
    plan: VisitPlan,
    target: VisitPlanCommand,
) -> VisitPlanReceipt:
    """Rebuild the exact historical response represented by one command.

    Returning ``receipt_for(plan)`` here would leak later transitions into an
    old idempotent response (for example, replaying create after start would
    suddenly return a session id).  The append-only command chain contains all
    transition actors/timestamps needed to project the original receipt.
    """
    commands = list(db.exec(
        select(VisitPlanCommand)
        .where(VisitPlanCommand.plan_id == plan.plan_id)
        .order_by(VisitPlanCommand.event_seq)
    ))
    if not commands:
        _fail(409, "visit_plan_state_invalid", "训练安排缺少命令账本")

    status = ""
    prior_revision = 0
    target_index: int | None = None
    target_status = ""
    for index, command in enumerate(commands, start=1):
        if (
            command.event_seq != index
            or command.expected_revision != prior_revision
            or command.resulting_revision != prior_revision + 1
        ):
            _fail(409, "visit_plan_state_invalid", "训练安排命令序列不连续")

        if index == 1:
            if command.command_type != "create":
                _fail(409, "visit_plan_state_invalid", "训练安排首个命令不是 create")
            status = "draft"
        elif status == "draft" and command.command_type == "approve":
            status = "approved"
        elif status in {"draft", "approved"} and command.command_type == "cancel":
            status = "cancelled"
        elif status == "approved" and command.command_type == "start":
            status = "started"
        else:
            _fail(409, "visit_plan_state_invalid", "训练安排命令转移不合法")

        prior_revision = command.resulting_revision
        if command.id == target.id:
            target_index = index
            target_status = status

    if target_index is None:
        _fail(409, "visit_plan_state_invalid", "幂等命令未出现在训练安排账本中")
    if plan.revision != prior_revision or plan.status != status:
        _fail(409, "visit_plan_state_invalid", "训练安排与命令账本不一致")
    _assert_non_cancelled_command_hashes(plan, commands)
    # The ledger chain and current-state consistency are now proven, so these
    # two gates refuse only what is genuinely a demo runtime projection.  The
    # current status is checked as well as the historical one: replaying a
    # create key against a Plan that is now paired-set approved/started must
    # fail closed rather than hand back its old draft receipt.
    _assert_profile_projectable(plan, plan.status)
    _assert_profile_projectable(plan, target_status)

    historical = commands[:target_index]
    created = historical[0]
    approved = next(
        (row for row in historical if row.command_type == "approve"), None)
    started = next(
        (row for row in historical if row.command_type == "start"), None)
    cancelled = next(
        (row for row in historical if row.command_type == "cancel"), None)

    linked = _linked_session(db, plan.plan_id) if target_status == "started" else None
    if target_status == "started" and linked is None:
        _fail(409, "visit_plan_state_invalid", "已启动幂等事实缺少权威场次")
    if linked is not None:
        _assert_session_copies_plan_binding(plan, linked)

    return VisitPlanReceipt(
        plan_id=plan.plan_id,
        patient_id=plan.patient_id,
        scheduled_date=plan.scheduled_date,
        scheduled_time=plan.scheduled_time,
        queue_order=plan.queue_order,
        session_sitting_no=plan.session_sitting_no,
        week_no=plan.week_no,
        phase_type=plan.phase_type,
        event_line=plan.event_line,
        item_bank_version_id=plan.item_bank_version_id,
        autopilot_profile_version_id=plan.autopilot_profile_version_id,
        is_simulation=plan.is_simulation,
        data_classification=plan.data_classification,
        status=target_status,
        revision=target.resulting_revision,
        created_by=created.actor_id,
        created_at=created.created_at,
        approved_by=approved.actor_id if approved is not None else None,
        approved_at=approved.created_at if approved is not None else None,
        started_by=started.actor_id if started is not None else None,
        started_at=started.created_at if started is not None else None,
        cancelled_by=cancelled.actor_id if cancelled is not None else None,
        cancelled_at=cancelled.created_at if cancelled is not None else None,
        session_id=linked.session_id if linked is not None else None,
    )


def _existing_command(
    db: Session,
    *,
    idempotency_key: str,
    command_type: str,
    request_hash: str,
    actor_id: str,
    expected_profile: tuple[str | None, str | None] = (None, None),
    resolve_profile_definition: bool = True,
    project: bool = True,
) -> VisitPlanReceipt | None:
    command = db.exec(select(VisitPlanCommand).where(
        VisitPlanCommand.idempotency_key == idempotency_key,
    )).first()
    if command is None:
        return None
    if (command.command_type != command_type
            or command.request_hash != request_hash
            or command.actor_id != actor_id):
        _fail(409, "visit_plan_idempotency_conflict", "幂等键已被另一个安排事实使用")
    plan = db.get(VisitPlan, command.plan_id)
    if plan is None or plan.revision < command.resulting_revision:
        _fail(409, "visit_plan_state_invalid", "幂等事实缺少一致的训练安排")
    # Serialize the replay decision against the authoritative governance row.
    # If withdrawal already owns this row, replay waits and observes the
    # terminal value; if replay owns it first, it linearizes before the later
    # withdrawal transaction.
    patient = db.exec(select(Patient).where(
        Patient.patient_id == plan.patient_id,
    ).with_for_update()).first()
    if patient is None:
        _fail(409, "visit_plan_patient_missing", "训练安排关联档案已不可用")
    if (patient.withdrawal_status or "").strip():
        # Historical mutation receipts are not governance read surfaces.  No
        # create/approve/start/cancel replay may resurrect patient, scheduling,
        # actor, or linked-session metadata in a stale client after withdrawal.
        _fail(
            409,
            "visit_plan_patient_withdrawn",
            "受试者已进入撤回终态，该历史命令不再提供业务回执",
        )
    # No replay may return before the persisted pair is proven equal to the
    # authoritative request pair: otherwise a tampered demo plan answers a
    # canonical key, or a tampered canonical plan answers a demo key.
    _assert_plan_profile_matches_request(
        plan, expected_profile,
        resolve_definition=resolve_profile_definition)
    if command_type in {"approve", "start"}:
        # Paired-set rows were already proven against their immutable
        # registered definition and their own frozen parents.  Reading the
        # current canonical bundle here would re-impose today's active
        # bank/protocol on a legitimately older demo binding.
        if _plan_profile_pair(plan) == (None, None):
            _bank, _protocol, binding = _current_definition_bundle(plan.week_no)
            _assert_plan_definition_binding(plan, binding)
        if command_type == "start":
            linked = _linked_session(db, plan.plan_id)
            if linked is None:
                _fail(409, "visit_plan_state_invalid", "已启动幂等事实缺少权威场次")
            _assert_session_copies_plan_binding(plan, linked)
    if not project:
        # Candidate validation only.  The caller has not yet proven locked
        # subject/context truth, so no success receipt may leave this path.
        return None
    return _receipt_for_command(db, plan=plan, target=command)


def _next_event_seq(db: Session, plan_id: str) -> int:
    current = db.exec(select(func.max(VisitPlanCommand.event_seq)).where(
        VisitPlanCommand.plan_id == plan_id,
    )).one()
    return int(current or 0) + 1


def _append_command(
    db: Session,
    *,
    plan: VisitPlan,
    command_type: Literal["create", "approve", "start", "cancel"],
    idempotency_key: str,
    request_hash: str,
    actor_id: str,
    expected_revision: int,
    resulting_revision: int,
    reason_code: str | None = None,
    created_at: datetime,
) -> None:
    db.add(VisitPlanCommand(
        plan_id=plan.plan_id,
        event_seq=_next_event_seq(db, plan.plan_id),
        idempotency_key=idempotency_key,
        command_type=command_type,
        request_hash=request_hash,
        actor_id=actor_id,
        expected_revision=expected_revision,
        resulting_revision=resulting_revision,
        reason_code=reason_code,
        created_at=created_at,
    ))


def _validate_context(body: VisitPlanCreateIn) -> None:
    issues = session_admission.validate_protocol_context(
        body.week_no, body.phase_type, body.event_line)
    if issues:
        _fail(422, "visit_plan_context_invalid", issues[0])


def create_plan(
    db: Session,
    *,
    body: VisitPlanCreateIn,
    actor_id: str,
    now: datetime | None = None,
) -> VisitPlanReceipt:
    observed_at = now or _now()
    payload = body.model_dump(mode="python")
    # Remove the key outright rather than hashing it as NULL: the canonical
    # request payload must stay byte-identical to the pre-D1A contract so every
    # historical create replay keeps resolving instead of becoming a conflict.
    requested_version = payload.pop("autopilot_profile_version_id", None)
    definition, _pre_bank, _pre_binding = _resolved_request_profile(
        requested_version)
    if definition is not None:
        _assert_profile_context(
            definition,
            week_no=body.week_no,
            phase_type=body.phase_type,
            event_line=body.event_line,
        )
    profile_pair = _definition_pair(definition)
    request_hash = _request_hash("create", {
        **payload,
        **_repeat_binding_hash_fields(),
        **_profile_binding_hash_fields(*profile_pair),
    })
    # Candidate validation only.  Returning a receipt here would answer a
    # historical demo replay before the locked Patient row is read, so a
    # subject that has since become a real participant would never be caught.
    _existing_command(
        db,
        idempotency_key=body.idempotency_key,
        command_type="create",
        request_hash=request_hash,
        actor_id=actor_id,
        expected_profile=profile_pair,
        project=False,
    )
    _validate_context(body)
    patient = db.exec(select(Patient).where(
        Patient.patient_id == body.patient_id,
    ).with_for_update()).first()
    if patient is None:
        _fail(404, "visit_plan_patient_missing", "受试者档案不存在")
    if (patient.withdrawal_status or "").strip():
        _fail(409, "visit_plan_patient_withdrawn", "已撤回受试者不能创建训练安排")
    is_simulation = patient.is_simulation_subject is True
    if definition is not None:
        # Re-resolve under the lock.  An in-place definition edit committed
        # between the two reads must never be laundered into a persisted
        # binding, and the bundle that validated the profile is the bundle we
        # persist from — no third, unverified canonical read.
        locked_definition, bank, binding = _resolved_request_profile(
            requested_version)
        if not _same_resolution(definition, locked_definition):
            _fail(
                409,
                "visit_plan_profile_digest_mismatch",
                "锁定前后解析到的自动演示计划定义不一致",
            )
        definition = locked_definition
        # Subject truth is the locked patient row, never the request.
        _assert_profile_subject(definition, is_simulation=is_simulation)
        _assert_profile_context(
            definition,
            week_no=body.week_no,
            phase_type=body.phase_type,
            event_line=body.event_line,
        )
    else:
        bank, _protocol, binding = _current_definition_bundle(
            body.week_no, allow_draft_anchor=True)
    # Only now, with locked identity, subject, context and the final persisted
    # bundle all fixed, may an idempotent replay return success.
    replay = _existing_command(
        db,
        idempotency_key=body.idempotency_key,
        command_type="create",
        request_hash=request_hash,
        actor_id=actor_id,
        expected_profile=profile_pair,
    )
    if replay is not None:
        return replay
    slot_key = _protocol_slot_key(body)
    occupied = db.exec(select(VisitPlan).where(
        VisitPlan.protocol_slot_key == slot_key,
    )).first()
    if occupied is not None:
        _fail(
            409,
            "visit_plan_protocol_slot_conflict",
            "该受试者的同一协议槽位已有未取消训练安排",
        )
    plan = VisitPlan(
        plan_id=_new_id("vp"),
        protocol_slot_key=slot_key,
        patient_id=body.patient_id,
        scheduled_date=body.scheduled_date,
        scheduled_time=body.scheduled_time,
        queue_order=body.queue_order,
        session_sitting_no=body.session_sitting_no,
        week_no=body.week_no,
        phase_type=body.phase_type,
        event_line=body.event_line,
        item_bank_version_id=bank.version_id,
        item_bank_definition_digest=binding.item_bank_definition_digest,
        autopilot_protocol_version_id=binding.autopilot_protocol_version_id,
        autopilot_protocol_definition_digest=(
            binding.autopilot_protocol_definition_digest),
        repeat_protocol_version_id=binding.repeat_protocol_version_id,
        repeat_protocol_definition_digest=(
            binding.repeat_protocol_definition_digest),
        autopilot_profile_version_id=profile_pair[0],
        autopilot_profile_definition_digest=profile_pair[1],
        is_simulation=is_simulation,
        data_classification=session_admission.expected_classification(is_simulation),
        status="draft",
        revision=1,
        created_by=actor_id,
        created_at=observed_at,
        updated_at=observed_at,
    )
    db.add(plan)
    db.flush()
    _append_command(
        db,
        plan=plan,
        command_type="create",
        idempotency_key=body.idempotency_key,
        request_hash=request_hash,
        actor_id=actor_id,
        expected_revision=0,
        resulting_revision=1,
        created_at=observed_at,
    )
    db.flush()
    return receipt_for(db, plan)


def _locked_plan(db: Session, plan_id: str) -> VisitPlan:
    plan = db.exec(select(VisitPlan).where(
        VisitPlan.plan_id == plan_id,
    ).with_for_update()).first()
    if plan is None:
        _fail(404, "visit_plan_missing", "训练安排不存在")
    return plan


def patient_id_for_plan_fence(db: Session, plan_id: str) -> str:
    """Resolve only the non-sensitive identity needed before transaction fencing.

    The mutator must still re-lock and revalidate the complete plan after it
    owns the fence.  Keeping this preflight deliberately narrow avoids making a
    stale plan snapshot authoritative while still letting the HTTP layer hold
    the SQLite companion lock through commit.
    """
    patient_id = db.exec(select(VisitPlan.patient_id).where(
        VisitPlan.plan_id == plan_id,
    )).first()
    if patient_id is None:
        _fail(404, "visit_plan_missing", "训练安排不存在")
    return patient_id


def _mutation_hash(
    command_type: str,
    plan_id: str,
    body: VisitPlanMutationIn | VisitPlanCancelIn,
    *,
    bind_repeat_protocol: bool = False,
    profile: tuple[str | None, str | None] = (None, None),
) -> str:
    payload: dict[str, object] = {
        "plan_id": plan_id,
        **body.model_dump(mode="python"),
    }
    if bind_repeat_protocol:
        payload.update(_repeat_binding_hash_fields())
    # A paired-null plan adds nothing here, so the pre-D1A canonical mutation
    # hashes stay byte-identical and their historical replays keep working.
    payload.update(_profile_binding_hash_fields(*profile))
    return _request_hash(command_type, payload)


def _assert_revision_and_status(
    plan: VisitPlan,
    *,
    expected_revision: int,
    expected_status: set[str],
) -> None:
    if plan.revision != expected_revision:
        _fail(409, "visit_plan_revision_conflict", "训练安排 revision 已变化")
    if plan.status not in expected_status:
        _fail(409, "visit_plan_transition_invalid", f"当前 {plan.status} 状态不允许该动作")


def _revalidate_admission(
    db: Session,
    plan: VisitPlan,
    *,
    require_caregiver_operational_demo20: bool = False,
) -> Patient:
    patient = db.exec(select(Patient).where(
        Patient.patient_id == plan.patient_id,
    ).with_for_update()).first()
    if patient is None:
        _fail(409, "visit_plan_patient_missing", "训练安排关联档案已不可用")
    bundle: tuple[content.ItemBank, dict, _DefinitionBinding] | None = None
    if require_caregiver_operational_demo20:
        bundle = _current_definition_bundle(plan.week_no)
        caregiver_bank, caregiver_protocol, caregiver_binding = bundle
        _assert_plan_definition_binding(plan, caregiver_binding)
        _assert_caregiver_operational_demo20(
            plan, bank=caregiver_bank, protocol=caregiver_protocol)
    patient_issues = session_admission.patient_admission_issues(
        patient, is_simulation=plan.is_simulation)
    if patient_issues:
        _fail(409, "visit_plan_patient_ineligible", "；".join(patient_issues))
    context_issues = session_admission.validate_protocol_context(
        plan.week_no, plan.phase_type, plan.event_line)
    if context_issues:
        _fail(409, "visit_plan_context_invalid", context_issues[0])
    operational_issues = session_admission.visit_plan_operational_issues(
        plan.week_no, plan.phase_type, plan.event_line)
    if operational_issues:
        _fail(
            409,
            "visit_plan_protocol_unavailable",
            "；".join(operational_issues),
        )
    if bundle is None:
        bundle = _current_definition_bundle(plan.week_no)
    bank, protocol, binding = bundle
    _assert_plan_definition_binding(plan, binding)
    content_issues = session_admission.content_admission_issues(
        bank,
        frozen_version_id=plan.item_bank_version_id,
        week_no=plan.week_no,
        phase_type=plan.phase_type,
        is_simulation=plan.is_simulation,
    )
    if content_issues:
        _fail(409, "visit_plan_content_unavailable", "；".join(content_issues))
    expected = session_admission.expected_classification(plan.is_simulation)
    if plan.data_classification != expected:
        _fail(409, "visit_plan_classification_invalid", "训练安排数据分类不一致")
    return patient


def approve_plan(
    db: Session,
    *,
    plan_id: str,
    body: VisitPlanMutationIn,
    actor_id: str,
    now: datetime | None = None,
) -> VisitPlanReceipt:
    observed_at = now or _now()
    # Pre-lock lookup identifies a candidate only: the authoritative mutation
    # hash depends on the plan's stored profile pair, which is not known until
    # the row is locked, so it may never return a receipt from here.
    _assert_command_key_available(
        db, idempotency_key=body.idempotency_key,
        command_type="approve", actor_id=actor_id)
    plan = _locked_plan(db, plan_id)
    profile_pair = _plan_profile_pair(plan)
    _assert_bound_profile_identity(plan)
    request_hash = _mutation_hash(
        "approve", plan_id, body, bind_repeat_protocol=True,
        profile=profile_pair)
    replay = _existing_command(
        db, idempotency_key=body.idempotency_key, command_type="approve",
        request_hash=request_hash, actor_id=actor_id,
        expected_profile=profile_pair)
    if replay is not None:
        return replay
    _assert_revision_and_status(
        plan, expected_revision=body.expected_revision,
        expected_status={"draft"})
    _assert_plan_operational_integrity(db, plan)
    # Refused only after the ledger, the stored pair, its parents and the
    # revision/status CAS are all proven, so a wrong hash, actor, command type
    # or revision reports its own error instead of hiding behind this one.
    _assert_profile_mutation_allowed(plan)
    _revalidate_admission(db, plan)
    plan.status = "approved"
    plan.revision += 1
    plan.approved_by = actor_id
    plan.approved_at = observed_at
    plan.updated_at = observed_at
    db.add(plan)
    _append_command(
        db, plan=plan, command_type="approve",
        idempotency_key=body.idempotency_key, request_hash=request_hash,
        actor_id=actor_id, expected_revision=body.expected_revision,
        resulting_revision=plan.revision, created_at=observed_at)
    db.flush()
    return receipt_for(db, plan)


def cancel_plan(
    db: Session,
    *,
    plan_id: str,
    body: VisitPlanCancelIn,
    actor_id: str,
    now: datetime | None = None,
) -> VisitPlanReceipt:
    observed_at = now or _now()
    _assert_command_key_available(
        db, idempotency_key=body.idempotency_key,
        command_type="cancel", actor_id=actor_id)
    plan = _locked_plan(db, plan_id)
    # Cancel must survive a drifted or missing manifest.  It hashes only the
    # pair already stored on the locked row and never resolves the current
    # definition, so a bad draft can always be retired and free its slot.
    profile_pair = _plan_profile_pair(plan)
    request_hash = _mutation_hash(
        "cancel", plan_id, body, profile=profile_pair)
    replay = _existing_command(
        db, idempotency_key=body.idempotency_key, command_type="cancel",
        request_hash=request_hash, actor_id=actor_id,
        expected_profile=profile_pair, resolve_profile_definition=False)
    if replay is not None:
        return replay
    patient = db.exec(select(Patient).where(
        Patient.patient_id == plan.patient_id,
    ).with_for_update()).first()
    if patient is None:
        _fail(409, "visit_plan_patient_missing", "训练安排关联档案已不可用")
    if (patient.withdrawal_status or "").strip():
        _fail(
            409,
            "visit_plan_patient_withdrawn",
            "受试者已撤回；普通训练安排接口不得再改写历史安排",
        )
    _assert_revision_and_status(
        plan, expected_revision=body.expected_revision,
        expected_status={"draft", "approved"})
    plan.status = "cancelled"
    plan.revision += 1
    plan.protocol_slot_key = None
    plan.cancelled_by = actor_id
    plan.cancelled_at = observed_at
    plan.updated_at = observed_at
    db.add(plan)
    _append_command(
        db, plan=plan, command_type="cancel",
        idempotency_key=body.idempotency_key, request_hash=request_hash,
        actor_id=actor_id, expected_revision=body.expected_revision,
        resulting_revision=plan.revision, reason_code=body.reason_code,
        created_at=observed_at)
    db.flush()
    return receipt_for(db, plan)


def assert_patient_ready_for_new_work(
    db: Session,
    patient_id: str,
    *,
    exclude_assessment_event_id: str | None = None,
) -> None:
    """Reject new work until this patient's bedside session is safely closed.

    Formal-assessment start can reuse this predicate while holding the same
    subject -> actor fence. Research review may finish asynchronously, but an
    ``intervention_completed`` session is switchable only after closeout.
    """
    sessions = list(db.exec(select(TrainSession).where(
        TrainSession.patient_id == patient_id,
    )))
    for train_session in sessions:
        runtime = db.get(SessionRuntimeState, train_session.session_id)
        if runtime is None or runtime.status in {"active", "paused"}:
            _fail(409, "visit_plan_patient_session_open", "该受试者仍有活跃或暂停场次")
        if (runtime.status == "intervention_completed"
                and db.get(
                    SessionCloseoutReport,
                    train_session.session_id,
                ) is None):
            _fail(
                409,
                "visit_plan_patient_closeout_required",
                "该受试者上一场床旁干预已结束但尚未保存现场收尾；收口后才能开始新工作",
            )
    assessment_statement = select(AssessmentEvent.event_id).where(
            AssessmentEvent.patient_id == patient_id,
            AssessmentEvent.status.in_({"in_progress", "awaiting_closeout"}),
        )
    if exclude_assessment_event_id is not None:
        assessment_statement = assessment_statement.where(
            AssessmentEvent.event_id != exclude_assessment_event_id)
    active_assessment_id = db.exec(assessment_statement.limit(1)).first()
    if active_assessment_id is not None:
        _fail(
            409,
            "visit_plan_patient_assessment_open",
            "该受试者仍有未收口的正式测评；必须先完成测评收尾，才能启动训练",
        )


def assert_actor_ready_for_new_work(
    db: Session,
    actor_id: str,
    *,
    exclude_assessment_event_id: str | None = None,
) -> None:
    """One researcher cannot silently switch away from unfinished bedside work.

    Callers own the actor-work advisory fence while this predicate is read.
    Formal-assessment start must acquire that same fence, making this combined
    training/assessment check one cross-worker admission predicate.
    """
    sessions = list(db.exec(select(TrainSession).where(
        TrainSession.trainer_id == actor_id,
    )))
    for train_session in sessions:
        runtime = db.get(SessionRuntimeState, train_session.session_id)
        if runtime is None or runtime.status in {"active", "paused"}:
            _fail(
                409,
                "visit_plan_actor_session_open",
                "当前账号仍有活跃或暂停场次；必须先安全收口，才能切换受试者",
            )
        if (runtime.status == "intervention_completed"
                and db.get(SessionCloseoutReport, train_session.session_id) is None):
            _fail(
                409,
                "visit_plan_actor_closeout_required",
                "当前账号上一场已结束床旁干预但尚未保存现场收尾；完成后才能切换受试者",
            )
    assessment_statement = (
        select(AssessmentEvent.event_id)
        .join(Patient, Patient.patient_id == AssessmentEvent.patient_id)
        .where(
            AssessmentEvent.assigned_assessor_id == actor_id,
            AssessmentEvent.status.in_({"in_progress", "awaiting_closeout"}),
            # Withdrawal freezes assessment facts in their historical state;
            # it must not forge closed/completed. Those frozen rows also must
            # not permanently occupy the assessor's current-work slot.
            or_(
                Patient.withdrawal_status.is_(None),
                func.trim(Patient.withdrawal_status) == "",
            ),
            func.lower(func.trim(func.coalesce(
                Patient.consent_status, ""))).not_in(
                    tuple(session_admission.CONSENT_DENIED_STATUSES)),
        )
    )
    if exclude_assessment_event_id is not None:
        assessment_statement = assessment_statement.where(
            AssessmentEvent.event_id != exclude_assessment_event_id)
    active_assessment_id = db.exec(assessment_statement.limit(1)).first()
    if active_assessment_id is not None:
        _fail(
            409,
            "visit_plan_actor_assessment_open",
            "当前账号仍有未收口的正式测评；必须先完成测评收尾，才能启动训练",
        )


# Compatibility aliases for older internal callers. New training and formal
# assessment admission code should use the public predicates above.
def _assert_no_open_session(db: Session, patient_id: str) -> None:
    assert_patient_ready_for_new_work(db, patient_id)


def _assert_actor_ready_for_next_session(db: Session, actor_id: str) -> None:
    assert_actor_ready_for_new_work(db, actor_id)


def start_plan(
    db: Session,
    *,
    plan_id: str,
    body: VisitPlanMutationIn,
    actor_id: str,
    require_caregiver_operational_demo20: bool = False,
    now: datetime | None = None,
) -> VisitPlanReceipt:
    observed_at = now or _now()
    _assert_command_key_available(
        db, idempotency_key=body.idempotency_key,
        command_type="start", actor_id=actor_id)
    plan = _locked_plan(db, plan_id)
    profile_pair = _plan_profile_pair(plan)
    _assert_bound_profile_identity(plan)
    request_hash = _mutation_hash(
        "start", plan_id, body, bind_repeat_protocol=True,
        profile=profile_pair)
    replay = _existing_command(
        db, idempotency_key=body.idempotency_key, command_type="start",
        request_hash=request_hash, actor_id=actor_id,
        expected_profile=profile_pair)
    if replay is not None:
        return replay
    _assert_revision_and_status(
        plan, expected_revision=body.expected_revision,
        expected_status={"approved"})
    _assert_plan_operational_integrity(db, plan)
    # Still before any Session, SessionRuntimeState, transition or command
    # write, but only after the ledger and CAS have had their say.
    if not require_caregiver_operational_demo20:
        _assert_profile_mutation_allowed(plan)
    _revalidate_admission(
        db,
        plan,
        require_caregiver_operational_demo20=(
            require_caregiver_operational_demo20),
    )
    actual_training_date = _research_today()
    if plan.scheduled_date > actual_training_date:
        _fail(409, "visit_plan_not_due", "未到安排日期，不能提前开场")
    assert_actor_ready_for_new_work(db, actor_id)
    assert_patient_ready_for_new_work(db, plan.patient_id)
    if _linked_session(db, plan.plan_id) is not None:
        _fail(409, "visit_plan_session_conflict", "该安排已关联场次")

    session_id = _new_id("s")
    train_session = TrainSession(
        session_id=session_id,
        patient_id=plan.patient_id,
        session_sitting_no=plan.session_sitting_no,
        training_date=actual_training_date,
        week_no=plan.week_no,
        phase_type=plan.phase_type,
        event_line=plan.event_line,
        trainer_id=actor_id,
        item_bank_version_id=plan.item_bank_version_id,
        item_bank_definition_digest=plan.item_bank_definition_digest,
        autopilot_protocol_version_id=plan.autopilot_protocol_version_id,
        autopilot_protocol_definition_digest=(
            plan.autopilot_protocol_definition_digest),
        repeat_protocol_version_id=plan.repeat_protocol_version_id,
        repeat_protocol_definition_digest=(
            plan.repeat_protocol_definition_digest),
        # Copied item by item, not defaulted.  Only paired-null reaches here in
        # D1A, and D1B will atomically carry a demo pair through this same line.
        autopilot_profile_version_id=plan.autopilot_profile_version_id,
        autopilot_profile_definition_digest=(
            plan.autopilot_profile_definition_digest),
        is_simulation=plan.is_simulation,
        data_classification=plan.data_classification,
        visit_plan_id=plan.plan_id,
    )
    runtime = SessionRuntimeState(
        session_id=session_id,
        status="active",
        revision=0,
        updated_at=observed_at,
    )
    db.add(train_session)
    db.add(runtime)
    plan.status = "started"
    plan.revision += 1
    plan.started_by = actor_id
    plan.started_at = observed_at
    plan.updated_at = observed_at
    db.add(plan)
    _append_command(
        db, plan=plan, command_type="start",
        idempotency_key=body.idempotency_key, request_hash=request_hash,
        actor_id=actor_id, expected_revision=body.expected_revision,
        resulting_revision=plan.revision, created_at=observed_at)
    db.flush()
    return receipt_for(db, plan)


def today_queue(
    db: Session,
    *,
    as_of_date: date | None = None,
    viewer_actor_id: str | None = None,
    viewer_role: str | None = None,
    require_caregiver_operational_demo20: bool = False,
) -> VisitPlanTodayOut:
    if require_caregiver_operational_demo20 and viewer_role != "caregiver_operator":
        _fail(
            500,
            "caregiver_queue_scope_invalid",
            "照护员可运行队列只能用于照护员视图",
        )
    current_date = as_of_date or _research_today()
    rows = list(db.exec(select(VisitPlan).where(
        VisitPlan.status == "approved",
        VisitPlan.scheduled_date <= current_date,
    )))
    # Scanned before the per-row admission filter below, which swallows
    # VisitPlanError with ``continue``.  An anomalous paired-set approved row
    # must fail the whole queue, never be silently dropped or downgraded into
    # an actionable canonical plan.
    for plan in rows:
        _assert_profile_projectable(plan, plan.status)
    # The queue is an operational bedside projection, not an immutable audit
    # listing.  Revalidate the current patient gate on every read so an approved
    # plan cannot remain visible after withdrawal, consent loss, or another
    # eligibility change.  ``start_plan`` repeats the same authority check under
    # row locks; this read filter is defence in depth and avoids touching the
    # append-only scheduling fact during the withdrawal transaction.
    admitted_rows: list[VisitPlan] = []
    # 各周题库独立冻结：逐周解析一次并缓存；该周不可解析(未登记/损坏)时,
    # 该周全部安排按防御性隐藏计入 withheld,不让坏内容进入床旁队列。
    bundles_by_week: dict[
        int, tuple[content.ItemBank, dict, _DefinitionBinding] | None] = {}

    def _bundle_for(
        week_no: int,
    ) -> tuple[content.ItemBank, dict, _DefinitionBinding] | None:
        key = _bank_week_for(week_no)
        if key not in bundles_by_week:
            try:
                bank, protocol, binding = _current_definition_bundle(key)
                bundles_by_week[key] = (bank, protocol, binding)
            except (VisitPlanError, content.FrozenContentUnavailable):
                # 未登记或坏档都只隔离该周(withheld 计数),不让单周内容故障
                # 以 503 掀翻整张床旁队列。approve/start 对坏档仍整体 503。
                bundles_by_week[key] = None
        return bundles_by_week[key]

    for plan in rows:
        patient = db.get(Patient, plan.patient_id)
        if patient is None:
            continue
        if session_admission.patient_admission_issues(
                patient, is_simulation=plan.is_simulation):
            continue
        if session_admission.validate_protocol_context(
                plan.week_no, plan.phase_type, plan.event_line):
            continue
        if session_admission.visit_plan_operational_issues(
                plan.week_no, plan.phase_type, plan.event_line):
            continue
        bundle = _bundle_for(plan.week_no)
        if bundle is None:
            continue
        bank, protocol, binding = bundle
        try:
            _assert_plan_definition_binding(plan, binding)
        except VisitPlanError:
            continue
        if session_admission.content_admission_issues(
                bank,
                frozen_version_id=plan.item_bank_version_id,
                week_no=plan.week_no,
                phase_type=plan.phase_type,
                is_simulation=plan.is_simulation,
        ):
            continue
        if plan.data_classification != session_admission.expected_classification(
                plan.is_simulation):
            continue
        if require_caregiver_operational_demo20:
            try:
                _assert_caregiver_operational_demo20(
                    plan, bank=bank, protocol=protocol)
            except VisitPlanError:
                continue
        admitted_rows.append(plan)
    withheld_count = len(rows) - len(admitted_rows)
    rows = admitted_rows
    rows.sort(key=lambda plan: (
        plan.scheduled_date,
        plan.scheduled_time is None,
        plan.scheduled_time or datetime.max.time(),
        plan.queue_order is None,
        plan.queue_order if plan.queue_order is not None else 100_001,
        plan.plan_id,
    ))
    return VisitPlanTodayOut(
        as_of_date=current_date,
        plans=[
            _receipt_for_viewer(
                db,
                plan,
                viewer_actor_id=viewer_actor_id,
                viewer_role=viewer_role,
            )
            for plan in rows
        ],
        withheld_count=withheld_count,
    )


def list_for_patient(
    db: Session,
    *,
    patient_id: str,
    include_withdrawn: bool = False,
    viewer_actor_id: str | None = None,
    viewer_role: str | None = None,
) -> list[VisitPlanReceipt]:
    patient = db.get(Patient, patient_id)
    if patient is None:
        _fail(404, "visit_plan_patient_missing", "受试者档案不存在")
    # Researchers and data stewards do not receive scheduling metadata for a
    # withdrawn subject.  An admin may retrieve the immutable scheduling
    # tombstone for governance; it remains unstartable because start_plan
    # revalidates the current Patient row under lock.
    rows = list(db.exec(select(VisitPlan).where(
        VisitPlan.patient_id == patient_id,
    )))
    # Detected before the withdrawal privacy early-return, otherwise an
    # anomalous paired-set approved/started row could hide behind an empty
    # list.  The empty-list behaviour itself is unchanged when nothing is
    # anomalous.
    for plan in rows:
        _assert_plan_operational_integrity(db, plan)
        _assert_profile_projectable(plan, plan.status)
    if ((patient.withdrawal_status or "").strip()
            and not include_withdrawn):
        return []
    rows.sort(key=lambda plan: (
        plan.scheduled_date,
        plan.scheduled_time is None,
        plan.scheduled_time or datetime.max.time(),
        plan.plan_id,
    ), reverse=True)
    receipts: list[VisitPlanReceipt] = []
    for plan in rows:
        receipts.append(_receipt_for_viewer(
            db,
            plan,
            viewer_actor_id=viewer_actor_id,
            viewer_role=viewer_role,
        ))
    return receipts
