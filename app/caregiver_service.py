"""照护员独立工作台的投影与呼叫幂等服务。

本模块不提交事务，不修改场次运行态。HTTP 适配层先做 owner
授权和状态锁定，再把暂停与只追加呼叫回执放在同一事务内。
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import secrets
from typing import NoReturn

from sqlmodel import Session as DBSession, select

from . import autopilot_plan_profiles
from .caregiver_contract import (
    CaregiverHelpRequestOut,
    CaregiverPlanOut,
    CaregiverSessionSummary,
)
from .models import (
    CaregiverHelpRequest,
    Session as TrainSession,
    SessionRuntimeState,
)
from .visit_plan_contract import VisitPlanReceipt


CURRENT_RUNTIME_STATUSES = frozenset({
    "active", "paused", "intervention_completed",
})


class CaregiverServiceError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _fail(status_code: int, code: str, message: str) -> NoReturn:
    raise CaregiverServiceError(status_code, code, message)


def plan_projection(receipt: VisitPlanReceipt) -> CaregiverPlanOut:
    """只投影已由照护员队列权威复验的精确 D1B 安排。"""
    if (
        receipt.autopilot_profile_version_id
        != autopilot_plan_profiles.WEEK2_SINGLE20_DEMO_VERSION
        or receipt.is_simulation is not True
        or receipt.data_classification != "simulation"
    ):
        _fail(
            409,
            "caregiver_plan_projection_not_operational_demo20",
            "今日工作台收到了非 20 题模拟安排，请刷新后重试",
        )
    return CaregiverPlanOut(
        plan_id=receipt.plan_id,
        participant_code=receipt.patient_id,
        scheduled_date=receipt.scheduled_date,
        scheduled_time=receipt.scheduled_time,
        queue_order=receipt.queue_order,
        session_sitting_no=receipt.session_sitting_no,
        week_no=receipt.week_no,
        phase_type=receipt.phase_type,
        event_line=receipt.event_line,
        is_simulation=receipt.is_simulation,
        data_classification=receipt.data_classification,
        autopilot_profile_version_id=receipt.autopilot_profile_version_id,
        completion_scope="demo_plan_only",
        resolved_position_count=20,
        operational_demo_ready=True,
        revision=receipt.revision,
    )


def _session_plan_projection(
    session: TrainSession,
) -> tuple[str | None, int | None, bool]:
    """Describe current work without blocking legacy/non-demo safety closure."""
    try:
        resolved = autopilot_plan_profiles.resolve_exact_runnable_demo20(
            session, require_runtime_enabled=False)
    except autopilot_plan_profiles.PlanProfileError:
        try:
            resolved = autopilot_plan_profiles.resolve_for_session(session)
        except (autopilot_plan_profiles.PlanProfileError, ValueError, OSError):
            return None, None, False
        return (
            resolved.completion_scope,
            resolved.resolved_position_count,
            False,
        )
    return (
        resolved.completion_scope,
        resolved.resolved_position_count,
        autopilot_plan_profiles.demo20_runtime_enabled(),
    )


def session_summary(
    session: TrainSession,
    runtime_state: SessionRuntimeState | None,
) -> CaregiverSessionSummary:
    status = runtime_state.status if runtime_state is not None else "active"
    revision = runtime_state.revision if runtime_state is not None else 0
    completion_scope, position_count, operational_demo_ready = (
        _session_plan_projection(session))
    return CaregiverSessionSummary(
        session_id=session.session_id,
        participant_code=session.patient_id,
        session_sitting_no=session.session_sitting_no,
        week_no=session.week_no,
        phase_type=session.phase_type,
        event_line=session.event_line,
        is_simulation=session.is_simulation,
        data_classification=session.data_classification,
        autopilot_profile_version_id=session.autopilot_profile_version_id,
        completion_scope=completion_scope,
        resolved_position_count=position_count,
        operational_demo_ready=operational_demo_ready,
        runtime_status=status,
        runtime_revision=revision,
    )


def current_session_for_actor(
    db: DBSession,
    *,
    actor_id: str,
) -> tuple[TrainSession, SessionRuntimeState | None] | None:
    """一个照护员同时最多有一个尚未研究收口的本人场次。"""
    candidates: list[tuple[TrainSession, SessionRuntimeState | None]] = []
    sessions = list(db.exec(select(TrainSession).where(
        TrainSession.trainer_id == actor_id,
    )))
    for session in sessions:
        runtime = db.get(SessionRuntimeState, session.session_id)
        status = runtime.status if runtime is not None else "active"
        if status in CURRENT_RUNTIME_STATUSES:
            candidates.append((session, runtime))
    if len(candidates) > 1:
        _fail(
            409,
            "caregiver_multiple_current_sessions",
            "当前账号同时有多个未收口场次，请暂停操作并联系管理员",
        )
    return candidates[0] if candidates else None


def _idempotency_digest(idempotency_key: str) -> str:
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


def _request_digest(*, session_id: str, actor_id: str, reason_code: str) -> str:
    canonical = json.dumps(
        {
            "actor_id": actor_id,
            "reason_code": reason_code,
            "session_id": session_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def existing_help_request(
    db: DBSession,
    *,
    session_id: str,
    actor_id: str,
    reason_code: str,
    idempotency_key: str,
) -> CaregiverHelpRequest | None:
    key_digest = _idempotency_digest(idempotency_key)
    row = db.exec(select(CaregiverHelpRequest).where(
        CaregiverHelpRequest.session_id == session_id,
        CaregiverHelpRequest.idempotency_key_sha256 == key_digest,
    )).first()
    if row is None:
        return None
    expected = _request_digest(
        session_id=session_id,
        actor_id=actor_id,
        reason_code=reason_code,
    )
    if row.actor_id != actor_id or row.request_hash != expected:
        _fail(
            409,
            "caregiver_help_idempotency_conflict",
            "同一呼叫幂等键已绑定不同请求，请刷新后重试",
        )
    return row


def append_help_request(
    db: DBSession,
    *,
    session_id: str,
    actor_id: str,
    reason_code: str,
    idempotency_key: str,
    runtime_revision: int,
    now: datetime | None = None,
) -> CaregiverHelpRequest:
    row = CaregiverHelpRequest(
        request_id=f"chr_{secrets.token_urlsafe(18)}",
        session_id=session_id,
        actor_id=actor_id,
        reason_code=reason_code,
        idempotency_key_sha256=_idempotency_digest(idempotency_key),
        request_hash=_request_digest(
            session_id=session_id,
            actor_id=actor_id,
            reason_code=reason_code,
        ),
        runtime_revision=runtime_revision,
        created_at=now or datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(row)
    db.flush()
    return row


def help_projection(
    row: CaregiverHelpRequest,
    *,
    idempotent: bool,
) -> CaregiverHelpRequestOut:
    return CaregiverHelpRequestOut(
        request_id=row.request_id,
        session_id=row.session_id,
        reason_code=row.reason_code,
        runtime_status="paused",
        runtime_revision=row.runtime_revision,
        created_at=row.created_at,
        idempotent=idempotent,
    )
