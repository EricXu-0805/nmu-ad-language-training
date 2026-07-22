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

from . import content, session_admission
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


def _protocol_slot_key(body: VisitPlanCreateIn) -> str:
    canonical = "\x00".join((
        body.patient_id,
        str(body.session_sitting_no),
        str(body.week_no),
        _enum_value(body.phase_type),
        _enum_value(body.event_line),
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_bank() -> content.ItemBank:
    return content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")


def _load_protocol() -> dict:
    return content.load_autopilot_protocol(
        content.CONTENT_DIR / "autopilot_protocol_v1.json")


@dataclass(frozen=True)
class _DefinitionBinding:
    item_bank_version_id: str
    item_bank_definition_digest: str
    autopilot_protocol_version_id: str
    autopilot_protocol_definition_digest: str


def _current_definition_bundle(
) -> tuple[content.ItemBank, dict, _DefinitionBinding]:
    bank = _load_bank()
    protocol = _load_protocol()
    protocol_issues = content.validate_autopilot_protocol(protocol)
    if protocol_issues:
        _fail(
            409,
            "visit_plan_protocol_invalid",
            "当前自动化协议不可执行：" + "；".join(protocol_issues),
        )
    return bank, protocol, _DefinitionBinding(
        item_bank_version_id=bank.version_id,
        item_bank_definition_digest=content.item_bank_definition_digest(bank),
        autopilot_protocol_version_id=str(protocol["protocol_version_id"]),
        autopilot_protocol_definition_digest=(
            content.autopilot_protocol_definition_digest(protocol)),
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
            "训练安排绑定的题库版本与当前版本不一致",
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
    ):
        _fail(
            409,
            "visit_plan_session_definition_mismatch",
            "场次没有逐项复制训练安排的题库/自动化协议定义绑定",
        )


def _linked_session(db: Session, plan_id: str) -> TrainSession | None:
    return db.exec(select(TrainSession).where(
        TrainSession.visit_plan_id == plan_id,
    )).first()


def receipt_for(db: Session, plan: VisitPlan) -> VisitPlanReceipt:
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
    if command_type in {"approve", "start"}:
        _bank, _protocol, binding = _current_definition_bundle()
        _assert_plan_definition_binding(plan, binding)
        if command_type == "start":
            linked = _linked_session(db, plan.plan_id)
            if linked is None:
                _fail(409, "visit_plan_state_invalid", "已启动幂等事实缺少权威场次")
            _assert_session_copies_plan_binding(plan, linked)
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
    request_hash = _request_hash("create", payload)
    replay = _existing_command(
        db,
        idempotency_key=body.idempotency_key,
        command_type="create",
        request_hash=request_hash,
        actor_id=actor_id,
    )
    if replay is not None:
        return replay
    _validate_context(body)
    patient = db.exec(select(Patient).where(
        Patient.patient_id == body.patient_id,
    ).with_for_update()).first()
    if patient is None:
        _fail(404, "visit_plan_patient_missing", "受试者档案不存在")
    # The first lookup is the fast replay path.  This second lookup is the
    # serialization-point check: on databases with row locks, another worker
    # may have committed the same command while this transaction waited for the
    # patient row.
    replay = _existing_command(
        db,
        idempotency_key=body.idempotency_key,
        command_type="create",
        request_hash=request_hash,
        actor_id=actor_id,
    )
    if replay is not None:
        return replay
    if (patient.withdrawal_status or "").strip():
        _fail(409, "visit_plan_patient_withdrawn", "已撤回受试者不能创建训练安排")
    bank, _protocol, binding = _current_definition_bundle()
    is_simulation = patient.is_simulation_subject is True
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
) -> str:
    return _request_hash(command_type, {
        "plan_id": plan_id,
        **body.model_dump(mode="python"),
    })


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


def _revalidate_admission(db: Session, plan: VisitPlan) -> Patient:
    patient = db.exec(select(Patient).where(
        Patient.patient_id == plan.patient_id,
    ).with_for_update()).first()
    if patient is None:
        _fail(409, "visit_plan_patient_missing", "训练安排关联档案已不可用")
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
    bank, _protocol, binding = _current_definition_bundle()
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
    request_hash = _mutation_hash("approve", plan_id, body)
    replay = _existing_command(
        db, idempotency_key=body.idempotency_key, command_type="approve",
        request_hash=request_hash, actor_id=actor_id)
    if replay is not None:
        return replay
    plan = _locked_plan(db, plan_id)
    replay = _existing_command(
        db, idempotency_key=body.idempotency_key, command_type="approve",
        request_hash=request_hash, actor_id=actor_id)
    if replay is not None:
        return replay
    _assert_revision_and_status(
        plan, expected_revision=body.expected_revision,
        expected_status={"draft"})
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
    request_hash = _mutation_hash("cancel", plan_id, body)
    replay = _existing_command(
        db, idempotency_key=body.idempotency_key, command_type="cancel",
        request_hash=request_hash, actor_id=actor_id)
    if replay is not None:
        return replay
    plan = _locked_plan(db, plan_id)
    replay = _existing_command(
        db, idempotency_key=body.idempotency_key, command_type="cancel",
        request_hash=request_hash, actor_id=actor_id)
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
    now: datetime | None = None,
) -> VisitPlanReceipt:
    observed_at = now or _now()
    request_hash = _mutation_hash("start", plan_id, body)
    replay = _existing_command(
        db, idempotency_key=body.idempotency_key, command_type="start",
        request_hash=request_hash, actor_id=actor_id)
    if replay is not None:
        return replay
    plan = _locked_plan(db, plan_id)
    replay = _existing_command(
        db, idempotency_key=body.idempotency_key, command_type="start",
        request_hash=request_hash, actor_id=actor_id)
    if replay is not None:
        return replay
    _assert_revision_and_status(
        plan, expected_revision=body.expected_revision,
        expected_status={"approved"})
    _revalidate_admission(db, plan)
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
) -> VisitPlanTodayOut:
    current_date = as_of_date or _research_today()
    rows = list(db.exec(select(VisitPlan).where(
        VisitPlan.status == "approved",
        VisitPlan.scheduled_date <= current_date,
    )))
    # The queue is an operational bedside projection, not an immutable audit
    # listing.  Revalidate the current patient gate on every read so an approved
    # plan cannot remain visible after withdrawal, consent loss, or another
    # eligibility change.  ``start_plan`` repeats the same authority check under
    # row locks; this read filter is defence in depth and avoids touching the
    # append-only scheduling fact during the withdrawal transaction.
    admitted_rows: list[VisitPlan] = []
    try:
        bank, _protocol, binding = _current_definition_bundle()
    except VisitPlanError:
        return VisitPlanTodayOut(as_of_date=current_date, plans=[])
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
        admitted_rows.append(plan)
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
    if ((patient.withdrawal_status or "").strip()
            and not include_withdrawn):
        return []
    rows = list(db.exec(select(VisitPlan).where(
        VisitPlan.patient_id == patient_id,
    )))
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
