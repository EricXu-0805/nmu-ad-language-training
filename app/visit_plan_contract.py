"""Strict account-facing contracts for pre-session training arrangements."""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .enums import EventLine, PhaseType


_OPAQUE_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
# 建档口(create_patient)与本契约共用同一格式:编号在建档时漏进非 ASCII,
# 到安排训练必然 422,非技术用户会卡死在中途(2026-08-21 首测实录)。
PATIENT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_PATIENT_ID_PATTERN = PATIENT_ID_PATTERN
_PROFILE_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class VisitPlanCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(
        min_length=8, max_length=128, pattern=_OPAQUE_KEY_PATTERN)
    patient_id: str = Field(
        min_length=1, max_length=128, pattern=_PATIENT_ID_PATTERN)
    scheduled_date: date
    scheduled_time: time | None = None
    queue_order: int | None = Field(default=0, ge=0, le=100_000)
    session_sitting_no: int = Field(default=1, ge=1, le=1_000)
    week_no: int = Field(ge=1, le=8)
    phase_type: PhaseType
    event_line: EventLine
    # Omitting this selects the canonical full-source plan.  Only an explicit
    # version may select a demo profile, and its digest is always resolved
    # server-side: ``extra="forbid"`` rejects a client-supplied digest.
    autopilot_profile_version_id: str | None = Field(
        default=None, min_length=1, max_length=128,
        pattern=_PROFILE_VERSION_PATTERN)


class VisitPlanMutationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(
        min_length=8, max_length=128, pattern=_OPAQUE_KEY_PATTERN)
    # Zero is structurally valid but stale because persisted plans start at 1;
    # accepting it here lets the service return the stable 409 CAS conflict.
    expected_revision: int = Field(ge=0)


VisitPlanCancelReason = Literal[
    "schedule_changed",
    "participant_unavailable",
    "researcher_unavailable",
    "protocol_correction",
    "duplicate_plan",
]


class VisitPlanCancelIn(VisitPlanMutationIn):
    reason_code: VisitPlanCancelReason


class VisitPlanReceipt(BaseModel):
    """No names, notes or free text; only scheduling and provenance facts."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    patient_id: str
    scheduled_date: date
    scheduled_time: time | None
    queue_order: int | None
    session_sitting_no: int
    week_no: int
    phase_type: PhaseType
    event_line: EventLine
    item_bank_version_id: str
    # Required nullable server fact.  NULL is the canonical full-source plan;
    # the paired digest is deliberately never exposed to any account client.
    autopilot_profile_version_id: str | None
    is_simulation: bool
    data_classification: Literal["research", "simulation"]
    status: Literal["draft", "approved", "started", "cancelled"]
    revision: int = Field(ge=1)
    created_by: str
    created_at: datetime
    approved_by: str | None
    approved_at: datetime | None
    started_by: str | None
    started_at: datetime | None
    cancelled_by: str | None
    cancelled_at: datetime | None
    session_id: str | None


class VisitPlanTodayOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of_date: date
    plans: list[VisitPlanReceipt]
    # 已审核到期、但读取时未通过门禁复验(撤回/同意失效/题库或协议漂移)而被
    # 隐藏的安排条数。隐藏是正确的 fail-closed 行为;计数让床旁不再把
    # "被拦下"误读成"没有安排"。
    withheld_count: int = 0
