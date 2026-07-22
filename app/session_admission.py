"""Shared, provider-free admission rules for plans and session creation."""
from __future__ import annotations

import os

from . import content
from .enums import ConsentType, EventLine, PhaseType
from .models import Patient


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

    P0 currently admits only the exact Week-2 formal-training vertical.  Its
    real-research admission remains subject to the separate content readiness
    gate; this helper must never turn a simulation acceptance path into a
    research bypass.
    """
    phase = getattr(phase_type, "value", phase_type)
    event = getattr(event_line, "value", event_line)
    if week_no == 2 and phase == "正式训练" and event == "正式训练":
        return []
    if week_no == 1 and phase == "关系建立" and event == "关系建立环节":
        return ["第1周关系建立尚未冻结独立完成合同，禁止审核或开场"]
    if (week_no == 1 and phase in {"基线测评", "前测"}
            and event == "基线测评窗"):
        return [f"第1周{phase}尚未冻结独立完成合同，禁止审核或开场"]
    if 3 <= week_no <= 8 and phase == "正式训练" and event == "正式训练":
        return [f"第{week_no}周尚无冻结训练计划和独立完成合同，禁止审核或开场"]
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
