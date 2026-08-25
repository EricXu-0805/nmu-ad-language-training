"""照护员独立工作台的投影与呼叫幂等服务。

本模块不提交事务，不修改场次运行态。HTTP 适配层先做 owner
授权和状态锁定，再把暂停与只追加呼叫回执放在同一事务内。
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
import json
import secrets
from typing import NoReturn

from sqlmodel import Session as DBSession, select

from . import autopilot_plan_profiles, content
from .caregiver_contract import (
    CaregiverHelpRequestOut,
    CaregiverPlanOut,
    CaregiverSessionSummary,
)
from .models import (
    CAREGIVER_HELP_STATES,
    CaregiverHelpDisposition,
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
            # 不传 bank 会回落到第 2 周题库,第 3~8 周场次在这里静默解析失败,
            # 照护员工作台的进度/范围整块空白——按场次周次取(2..8 之外仍回落 2,
            # 与 autopilot_service._session_week_bank 同则)。
            week_no = session.week_no if 2 <= (session.week_no or 0) <= 8 else 2
            resolved = autopilot_plan_profiles.resolve_for_session(
                session, bank=content.load_item_bank_for_week(week_no))
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


# ---------------------------------------------------------------------------
# 求助四态
# ---------------------------------------------------------------------------
#: 通知通道的配置项。**没配就没配**——`delivered` 于是永远到不了，界面必须
#: 如实说"已登记，本机构还没配置通知谁"，不能含糊成"已通知"。
#: 放行清单原文：「求助通知给谁……必须由养老院确认后再实现」，所以工程只做
#: 通道，不替他们定通知对象。
HELP_NOTIFY_CHANNEL_ENV = "CAREGIVER_HELP_NOTIFY_CHANNEL"

_HELP_STATE_ORDER = {name: i for i, name in enumerate(CAREGIVER_HELP_STATES)}
#: 人可以追加的状态。`delivered` 不在里面——它只能由通道带着回执写入。
HUMAN_APPENDABLE_HELP_STATES = frozenset({"acknowledged", "resolved"})


class HelpDispositionRejected(RuntimeError):
    """稳定拒绝：带 code，不含姓名、电话或通道地址。"""

    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code = code
        self.message = message


def help_notify_channel_configured() -> bool:
    return bool((os.environ.get(HELP_NOTIFY_CHANNEL_ENV) or "").strip())


def _evidence_digest(*parts: str) -> str:
    return hashlib.sha256(
        "\x00".join(parts).encode("utf-8")).hexdigest()


def help_dispositions(
    db: DBSession, *, request_id: str,
) -> list[CaregiverHelpDisposition]:
    return list(db.exec(
        select(CaregiverHelpDisposition)
        .where(CaregiverHelpDisposition.request_id == request_id)
        .order_by(CaregiverHelpDisposition.created_at)))


def help_state(dispositions: list[CaregiverHelpDisposition]) -> str:
    """当前状态 = 已追加的转移里最靠后的那个；一条都没有就是"已登记"。

    四态可跳过：没有通知通道时 `delivered` 到不了，但工作人员当面走过来仍然
    能直接到 `acknowledged`——那是真的发生了，不能因为没配通道就否认。
    """
    reached = [d.state for d in dispositions if d.state in _HELP_STATE_ORDER]
    if not reached:
        return "recorded"
    return max(reached, key=lambda name: _HELP_STATE_ORDER[name])


def append_help_disposition(
    db: DBSession,
    *,
    request_id: str,
    state: str,
    actor_id: str,
    evidence: str,
    now: datetime | None = None,
) -> CaregiverHelpDisposition:
    """追加一条人类状态转移。**拒绝 `delivered`**——那不是人能声称的。"""
    if state not in HUMAN_APPENDABLE_HELP_STATES:
        raise HelpDispositionRejected(
            "help_state_not_human_appendable",
            "「已送达」只能由通知通道带着回执写入，不能由人声称")
    return _append_disposition(
        db, request_id=request_id, state=state, actor_id=actor_id,
        evidence=evidence, now=now)


def append_help_delivery_receipt(
    db: DBSession,
    *,
    request_id: str,
    channel_id: str,
    receipt: str,
    now: datetime | None = None,
) -> CaregiverHelpDisposition:
    """通道送达之后写回执。没配通道时直接拒绝，不留一条无根据的「已送达」。"""
    if not help_notify_channel_configured():
        raise HelpDispositionRejected(
            "help_notify_channel_unconfigured",
            "本机构还没有配置求助通知对象，无法产生送达回执")
    return _append_disposition(
        db, request_id=request_id, state="delivered", actor_id=channel_id,
        evidence=receipt, now=now)


def _append_disposition(
    db: DBSession, *, request_id: str, state: str, actor_id: str,
    evidence: str, now: datetime | None,
) -> CaregiverHelpDisposition:
    actor = (actor_id or "").strip()
    if not actor:
        raise HelpDispositionRejected(
            "help_disposition_actor_required",
            "求助处置必须记名——谁接的、谁处理的是追责事实")
    existing = {d.state for d in help_dispositions(db, request_id=request_id)}
    if state in existing:
        raise HelpDispositionRejected(
            "help_state_already_reached", f"这条求助已经到过「{state}」")
    row = CaregiverHelpDisposition(
        disposition_id=f"chd_{secrets.token_urlsafe(18)}",
        request_id=request_id,
        state=state,
        actor_id=actor,
        # 只存摘要不存正文：通道回执里可能带值班人姓名与电话。
        evidence_sha256=_evidence_digest(request_id, state, actor, evidence),
        created_at=now or datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(row)
    db.flush()
    return row


def help_status_projection(
    db: DBSession, *, request_id: str,
) -> dict[str, object]:
    """给界面的四态投影。**通道没配就明说没配**，不含糊成「已通知」。"""
    dispositions = help_dispositions(db, request_id=request_id)
    state = help_state(dispositions)
    configured = help_notify_channel_configured()
    return {
        "request_id": request_id,
        "state": state,
        "states": list(CAREGIVER_HELP_STATES),
        "notify_channel_configured": configured,
        # 没配通道时这一态结构上到不了，界面据此显示诚实文案而不是"等待送达"。
        "delivery_reachable": configured,
        "reached": [
            {"state": d.state, "actor_id": d.actor_id,
             "at": d.created_at.isoformat() + "Z"}
            for d in dispositions
        ],
    }
