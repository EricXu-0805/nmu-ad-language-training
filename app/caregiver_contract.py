"""照护员工作台的最小、无自由文本契约。"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .autopilot_service import AutopilotStatusReceipt
from .enums import EventLine, PhaseType


CaregiverRuntimeStatus = Literal[
    "active", "paused", "intervention_completed", "completed", "aborted", "failed",
]
CaregiverCompletionScope = Literal[
    "canonical_full_source", "demo_plan_only",
]
CaregiverHelpReason = Literal[
    "participant_distress",
    "participant_request",
    "clinical_concern",
    "technical_failure",
    "other_staff_needed",
]


class CaregiverPlanOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    participant_code: str
    scheduled_date: date
    scheduled_time: time | None
    queue_order: int | None
    session_sitting_no: int = Field(ge=1)
    week_no: int = Field(ge=1, le=8)
    phase_type: PhaseType
    event_line: EventLine
    is_simulation: bool
    data_classification: Literal["research", "simulation"]
    autopilot_profile_version_id: str | None
    completion_scope: CaregiverCompletionScope
    resolved_position_count: int = Field(ge=0)
    operational_demo_ready: bool
    revision: int = Field(ge=1)


class CaregiverSessionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    participant_code: str
    session_sitting_no: int = Field(ge=1)
    week_no: int = Field(ge=1, le=8)
    phase_type: PhaseType
    event_line: EventLine
    is_simulation: bool
    data_classification: Literal["research", "simulation"]
    autopilot_profile_version_id: str | None
    completion_scope: CaregiverCompletionScope | None
    resolved_position_count: int | None = Field(default=None, ge=0)
    operational_demo_ready: bool
    runtime_status: CaregiverRuntimeStatus
    runtime_revision: int = Field(ge=0)


class CaregiverTodayOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of_date: date
    plans: list[CaregiverPlanOut]
    withheld_count: int = Field(ge=0)
    current_session: CaregiverSessionSummary | None


class CaregiverStartOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    session: CaregiverSessionSummary


class CaregiverActivationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    runtime_status: CaregiverRuntimeStatus
    runtime_revision: int = Field(ge=0)
    live_seq: int = Field(ge=1)
    live_wseq: int = Field(ge=1)
    idempotent: bool


class CaregiverPresenceOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    online: bool
    screen: str | None
    last_seen_at: datetime | None


class CaregiverStatusOut(CaregiverSessionSummary):
    model_config = ConfigDict(extra="forbid")

    active_bedside_session: bool
    patient_presence: CaregiverPresenceOut
    autopilot: AutopilotStatusReceipt


class CaregiverHelpRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_code: CaregiverHelpReason
    idempotency_key: str = Field(
        min_length=16,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,199}$",
    )


class CaregiverHelpRequestOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    session_id: str
    reason_code: CaregiverHelpReason
    runtime_status: Literal["paused"]
    runtime_revision: int = Field(ge=0)
    created_at: datetime
    idempotent: bool


class CaregiverHelpDispositionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 有意不含 "delivered"：送达只能由通知通道带回执写入，不能由人在界面上声称。
    state: Literal["acknowledged", "resolved"]
    #: 现场说明（谁来了、怎么处理的）。**只入摘要不入库**——它可能带值班人
    #: 姓名与电话，正文留在机构自己的交接本里。
    note: str = Field(min_length=1, max_length=500)


class CaregiverHelpDispositionEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["delivered", "acknowledged", "resolved"]
    actor_id: str
    at: str


class CaregiverHelpStatusOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    state: Literal["recorded", "delivered", "acknowledged", "resolved"]
    states: list[str]
    notify_channel_configured: bool
    #: 没配通知对象时恒为 false。界面据此说"已登记，请当面叫人"，
    #: 不能显示"等待送达"——那是在暗示有人已经被通知到了。
    delivery_reachable: bool
    reached: list[CaregiverHelpDispositionEntry]
