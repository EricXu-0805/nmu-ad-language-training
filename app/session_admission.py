"""Shared, provider-free admission rules for plans and session creation."""
from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlmodel import Session

from . import content
from .enums import ConsentType, EventLine, PhaseType
from .models import LiveState, Patient


_CONSENT_GRANTED = frozenset({
    "已同意", "同意", "有效", "已获取", "已签署",
    "granted", "consented", "obtained", "signed", "active", "valid",
})
CONSENT_DENIED_STATUSES = frozenset({
    "未同意", "已撤回", "拒绝", "不同意",
    "denied", "withdrawn", "refused", "declined", "rejected",
})
_CONSENT_DENIED = CONSENT_DENIED_STATUSES


def patient_content_sealed(patient: Patient) -> bool:
    """True when ordinary reads/writes must not expose subject content."""
    return bool(
        (patient.withdrawal_status or "").strip()
        or (patient.consent_status or "").strip().casefold() in _CONSENT_DENIED
    )


def simulation_enabled() -> bool:
    return os.environ.get("ALLOW_SIMULATION_DATA", "").strip().lower() in {
        "1", "true", "yes",
    }


def expected_classification(is_simulation: bool) -> str:
    return "simulation" if is_simulation else "research"


def validate_protocol_context(
    week_no: int,
    phase_type: PhaseType | str,
    event_line: EventLine | str,
) -> list[str]:
    phase = getattr(phase_type, "value", phase_type)
    event = getattr(event_line, "value", event_line)
    valid = (
        (week_no == 1 and phase == "关系建立" and event == "关系建立环节")
        or (week_no == 1 and phase in {"基线测评", "前测"}
            and event == "基线测评窗")
        or (2 <= week_no <= 8 and phase == "正式训练" and event == "正式训练")
    )
    return [] if valid else ["week_no / phase_type / event_line 组合不符合已定事件线"]


def visit_plan_operational_issues(
    week_no: int,
    phase_type: PhaseType | str,
    event_line: EventLine | str,
) -> list[str]:
    """Return delivery-contract blockers for VisitPlan approval/start.

    This is deliberately narrower than ``validate_protocol_context``.  A
    context can belong to the study blueprint without yet having a frozen,
    independently completable software contract.  Draft scheduling facts may
    still be retained, but no such plan may be approved or started.

    Admitted bedside contracts: Week-1 relationship building (frozen script
    driver + rapport completion contract) and Weeks 2-8 formal training (one
    shared engine; per-week availability is decided by the separate content
    gate, whose frozen per-week bank is the "训练计划" artifact).  The Week-1
    baseline window is refused permanently here by design: standardized
    pre-tests run in the formal assessment-event domain, which owns its own
    scheduling, slot fencing and mutual exclusion — a parallel training
    session would duplicate that state.  Real-research admission always
    remains subject to the content readiness gate; this helper must never
    turn a simulation acceptance path into a research bypass.
    """
    phase = getattr(phase_type, "value", phase_type)
    event = getattr(event_line, "value", event_line)
    if 2 <= week_no <= 8 and phase == "正式训练" and event == "正式训练":
        return []
    if week_no == 1 and phase == "关系建立" and event == "关系建立环节":
        return []
    if (week_no == 1 and phase in {"基线测评", "前测"}
            and event == "基线测评窗"):
        return [f"第1周{phase}走正式量表评估事件工作流，不建训练场次"]
    return ["week_no / phase_type / event_line 组合没有可执行的床旁协议合同"]


def patient_admission_issues(
    patient: Patient,
    *,
    is_simulation: bool,
) -> list[str]:
    issues: list[str] = []
    consent = (patient.consent_status or "").strip().casefold()
    if is_simulation:
        if not simulation_enabled():
            issues.append("模拟数据路径未由部署者显式开启")
        if patient.is_simulation_subject is not True:
            issues.append("模拟场次只能绑定专用模拟档案")
        if consent in _CONSENT_DENIED:
            issues.append("模拟档案存在明确拒绝或撤回")
        if patient.recording_allowed is False:
            issues.append("recording_allowed=false")
    else:
        if patient.is_simulation_subject:
            issues.append("模拟档案不得进入真实研究")
        if consent not in _CONSENT_GRANTED:
            issues.append("consent_status 未明确为已同意/有效")
        if patient.consent_type is None:
            issues.append("consent_type 未填写")
        elif patient.consent_type == ConsentType.代理同意加本人赞同:
            if patient.proxy_consent is not True:
                issues.append("代理同意要求 proxy_consent=true")
            if patient.assent_obtained is not True:
                issues.append("代理同意要求 assent_obtained=true")
        if patient.mandarin_eligible is not True:
            issues.append("mandarin_eligible 必须明确为 true")
        if patient.recording_allowed is not True:
            issues.append("recording_allowed 必须明确为 true")
    if (patient.withdrawal_status or "").strip():
        issues.append("受试者已进入撤回流程")
    return issues


def content_admission_issues(
    bank: content.ItemBank,
    *,
    frozen_version_id: str,
    week_no: int,
    phase_type: PhaseType | str,
    is_simulation: bool,
) -> list[str]:
    issues: list[str] = []
    if bank.version_id != frozen_version_id:
        issues.append("当前题库版本与训练安排冻结版本不一致")
    readiness = content.content_readiness(bank)
    if not is_simulation and readiness["ready_for_research"] is not True:
        issues.append("当前题库未达到真实研究冻结/质控门禁")
    phase = getattr(phase_type, "value", phase_type)
    if phase == "正式训练" and week_no not in bank.supported_training_weeks:
        issues.append(f"第{week_no}周材料尚未结构化并校对")
    return issues


# ── 床旁 live 槽的"遗弃"判据(2026-09-03 生产实证的雷) ────────────────────
# 研究者训练中直接关页签走人 → 占着床旁槽的活跃/暂停场把下一场挡死。
# "遗弃"只看操作端(控制台)侧的 live 写时间 row.updated_at:它由每次
# 握手/推游标/推 rapport/老人端录音回报刷新。**绝不把老人端被动心跳
# (patient_last_seen_at)算进来**——两设备部署里平板固定床旁、每几秒心跳一次,
# 研究者关的是笔记本控制台;把心跳算进来会让平板一直开着的弃场永不判遗弃,
# 修复在最常见部署下失效(复核 P1)。


def bedside_stale_after() -> timedelta | None:
    """床旁槽遗弃阈值;None = 关闭(永不自动让位)。

    NMU_BEDSIDE_STALE_MINUTES:正整数=分钟阈值;0/负数=关闭;非数字=落回 15。
    """
    raw = (os.environ.get("NMU_BEDSIDE_STALE_MINUTES") or "15").strip()
    try:
        minutes = int(raw)
    except ValueError:
        minutes = 15
    if minutes <= 0:
        return None
    return timedelta(minutes=minutes)


def live_holder_session_id(live_row: LiveState | None) -> str | None:
    """当前持有床旁槽的场次 id(live 广播槽指向谁)。"""
    if live_row is None or not live_row.session_json:
        return None
    try:
        payload = json.loads(live_row.session_json)
    except (ValueError, TypeError):
        return None
    sid = payload.get("sessionId") if isinstance(payload, dict) else None
    return sid or None


def live_slot_stale(live_row: LiveState | None, now: datetime | None = None) -> bool:
    """床旁槽是否已被遗弃:控制台侧 live 写停了超过阈值。

    从未写过(updated_at 为空)的槽不谈守护,视为已遗弃。阈值关闭时永不遗弃。
    """
    threshold = bedside_stale_after()
    if threshold is None:
        return False
    if live_row is None or live_row.updated_at is None:
        return True
    return (now or datetime.now()) - live_row.updated_at > threshold


def runtime_stale(runtime, now: datetime | None = None) -> bool:
    """场次自己的活动时钟是否已放置超时。

    SessionRuntimeState.updated_at 由 START(建行)、推游标/rapport、暂停/恢复
    刷新——是干净的"研究者在这场上还有没有动作"信号,**不受老人端心跳影响**。
    """
    threshold = bedside_stale_after()
    if threshold is None:
        return False
    updated_at = getattr(runtime, "updated_at", None) if runtime is not None else None
    if updated_at is None:
        return False
    return (now or datetime.now()) - updated_at > threshold


def bedside_session_blocks_new_work(
    session_id: str,
    runtime,
    live_row: LiveState | None,
    now: datetime | None = None,
) -> bool:
    """活跃/暂停场是否应拦住新工作。

    只拦"仍被照看"的场;遗弃/被接管的放行——握手层会接管,超前拦死正是那颗雷。
    - 有 runtime 活动时钟(START 路径必有):按它是否放置超时判,刚开的场必新鲜=拦。
    - 无 runtime 时钟(裸握手等):回退到"是否仍持新鲜床旁槽"。
    遗弃只看控制台侧信号,绝不含老人端被动心跳。
    """
    if runtime is not None and getattr(runtime, "updated_at", None) is not None:
        return not runtime_stale(runtime, now)
    if live_holder_session_id(live_row) != session_id:
        return False
    return not live_slot_stale(live_row, now)


# ── 床旁槽数与选槽(两床并行,上限 2) ─────────────────────────────────────
# id=1 既是全局锁锚也是 1 号槽,id=2 是可选的 2 号槽;槽属于哪一场只从
# session_json.sessionId 推出。选槽是纯函数:调用方在 _LIVE_WRITE_LOCK 加锚行锁里
# 把各槽现状读成 BedsideSlotView 再调用。选中槽之后的动作恒为今天单槽对这个持有者
# 走的分支,N=1 时只看 1 号槽,所以和今天逐分支等同。

BEDSIDE_TERMINAL_STATUSES = frozenset({"completed", "aborted", "failed"})


def bedside_slot_count() -> int:
    """床旁槽数:NMU_BEDSIDE_SLOTS 去空白后恰为 "2" 才是 2,其余一律 1。每次现读。"""
    return 2 if (os.environ.get("NMU_BEDSIDE_SLOTS") or "").strip() == "2" else 1


class BedsideSlotError(Exception):
    """选槽 fail closed。只带闭集 code,不带场次或受试者标识。"""

    def __init__(self, code: Literal["bedside_slot_conflict", "bedside_slots_full"]):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BedsideSlotView:
    """一个床旁槽的现状;没有行的槽可以不传,按空槽算。

    status 是持有场次的 runtime 状态,None = 没有 runtime 行(照今天按 active 算);
    stale = live_slot_stale(row)。
    """
    slot_id: int
    session_id: str | None = None
    session_exists: bool = False
    status: str | None = None
    stale: bool = False
    patient_id: str | None = None
    trainer_id: str | None = None


@dataclass(frozen=True)
class BedsideSlotChoice:
    """action 对应今天单槽的分支:keep 本场已占;vacant 空槽(含持有场次已不存在);
    terminal_takeover 终态让位留审计;stale_takeover 遗弃槽 fence 加暂停再让位;
    protected 新鲜保护,要上一场的操作权(自动场切回另核上一场已暂停)。"""
    slot_id: int
    action: Literal["keep", "vacant", "terminal_takeover", "stale_takeover", "protected"]


def _same_key(left: str | None, right: str | None) -> bool:
    left = (left or "").strip()
    return bool(left) and left == (right or "").strip()


def choose_bedside_slot(
    slots: Iterable[BedsideSlotView],
    *,
    session_id: str,
    patient_id: str | None,
    trainer_id: str | None,
    slot_count: int,
    purpose: Literal["handshake", "autopilot_return"],
) -> BedsideSlotChoice:
    """按固定顺序给 session_id 选床旁槽;只读输入,不产生任何副作用。

    1 本场已占的槽;2 同一受试者的场次占着的槽;3 同一 trainer_id 的场次占着的槽;
    4 空或终态的槽取最小编号;5 遗弃槽,只在握手时可选;6 N=1 走今天的新鲜保护,
    N=2 回 bedside_slots_full。本场写在两槽、或本场的受试者已占两槽时回
    bedside_slot_conflict;别的场次或受试者的违例不拦,那几槽照普通占用处理。
    """
    if purpose not in {"handshake", "autopilot_return"}:
        raise ValueError(f"unknown bedside slot purpose {purpose!r}")
    given = {view.slot_id: view for view in slots}
    views = [given.get(slot_id) or BedsideSlotView(slot_id=slot_id)
             for slot_id in range(1, slot_count + 1)]

    def occupied(view: BedsideSlotView) -> bool:
        return (bool(view.session_id) and view.session_exists
                and view.status not in BEDSIDE_TERMINAL_STATUSES)

    # 终态场和已不存在的场不算占槽,它们的影子无害。本场或本场的受试者重复出现说明
    # 不变量已经破了(如 2→1→2 留下的影子),不知道该接哪一份,fail closed。别人的违例
    # 只拦别人:那一场自己的平板和握手本就 409,不能让一处违例连坐所有握手;它的槽
    # 照普通占用处理,遗弃了就照常让位。
    own_slots = [view for view in views if view.session_id == session_id]
    patient_slots = [view for view in views
                     if view.session_id == session_id
                     or (occupied(view) and _same_key(view.patient_id, patient_id))]
    if len(own_slots) > 1 or len(patient_slots) > 1:
        raise BedsideSlotError("bedside_slot_conflict")

    def occupied_action(view: BedsideSlotView) -> str:
        if purpose == "handshake" and view.stale:
            return "stale_takeover"
        return "protected"

    for view in views:
        if view.session_id == session_id:
            return BedsideSlotChoice(view.slot_id, "keep")
    for key, own in (("patient_id", patient_id), ("trainer_id", trainer_id)):
        for view in views:
            if occupied(view) and _same_key(getattr(view, key), own):
                return BedsideSlotChoice(view.slot_id, occupied_action(view))
    for view in views:
        if not view.session_id or not view.session_exists:
            return BedsideSlotChoice(view.slot_id, "vacant")
        if view.status in BEDSIDE_TERMINAL_STATUSES:
            return BedsideSlotChoice(view.slot_id, "terminal_takeover")
    if purpose == "handshake":
        for view in views:
            if view.stale:
                return BedsideSlotChoice(view.slot_id, "stale_takeover")
    if slot_count == 1:
        return BedsideSlotChoice(1, "protected")
    raise BedsideSlotError("bedside_slots_full")


def bedside_slot_rows(db: Session, slot_count: int | None = None) -> dict[int, LiveState | None]:
    """各床旁槽的行,按槽号;没有行的槽给 None。只读,不加锁,不建行。

    slot_count 不给就按开关;要看 2 号行里有没有影子时调用方显式给 2。
    """
    count = bedside_slot_count() if slot_count is None else slot_count
    return {slot_id: db.get(LiveState, slot_id) for slot_id in range(1, count + 1)}


def live_rows_for_session(db: Session, session_id: str | None) -> list[LiveState]:
    """本场所在的每一个床旁槽的行,按槽号;不在任何槽里回空表。只读,不加锁,不建行。

    同一场写在两槽属违例(代码退回单槽版再开两床时会留下),这里不报错:停麦、终态和
    撤回投影要落到每一份上,不能因为违例停不下来。N=1 只看锚行。
    """
    if not session_id:
        return []
    return [row for row in bedside_slot_rows(db).values()
            if live_holder_session_id(row) == session_id]


def live_row_for_session(db: Session, session_id: str | None) -> LiveState | None:
    """本场所在床旁槽的行;本场不在任何槽里就回锚行(id=1)。只读,不加锁,不建行。

    N=1 恒回锚行,和今天的 s.get(LiveState, 1) 一样;回锚行时调用方现有的
    「_live_session_id(row) != sid」判断原样生效。同一场命中两槽属违例,fail closed;
    停的路径改用 live_rows_for_session。
    """
    found = live_rows_for_session(db, session_id)
    if len(found) > 1:
        raise BedsideSlotError("bedside_slot_conflict")
    return found[0] if found else db.get(LiveState, 1)


def first_live_row_for_session(db: Session, session_id: str | None) -> LiveState | None:
    """停的路径要核对或回报的那一份:本场所在的第一个槽,不在任何槽里回锚行。违例不报错。"""
    found = live_rows_for_session(db, session_id)
    return found[0] if found else db.get(LiveState, 1)
