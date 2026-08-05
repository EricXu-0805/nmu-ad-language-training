"""数据表模型（M6 拥有）——共享数据契约的 DB 落地。

需 `pip install sqlmodel`。阶段0 核心逻辑（scoring/audio_gate/judging/enums）不依赖本文件，
可先无 DB 跑测试；本文件在接数据库时启用。

两条结构性硬约束在此体现：
  1. ★画像不进判分：Week1Profile 独立成表，与 ItemEvent/TurnEvent 评分链【无外键连接】，
     判分侧不得 join 本表（见 judging.py 的运行时守卫）。
  2. de_total / key_element_rate 【不落库存字段】——由 scoring.py 从分环节原始值计算，
     单一事实源；本表只存分环节锁定原始值。
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger, CheckConstraint, Column, ForeignKey, ForeignKeyConstraint,
    Index, Integer, UniqueConstraint, event as sa_event,
    inspect as sa_inspect,
)
from sqlmodel import Field, SQLModel  # type: ignore

from .enums import (
    AudioStatus, ConsentType, PhaseType, EventLine, TaskType, ItemSetType,
)


def _utc_now_naive() -> datetime:
    """Persist UTC in the project's timezone-naive database columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hex64_sql(column: str) -> str:
    """SQL for "this column really is 64 lowercase hex characters".

    ``lower(x) = x`` alone accepts 64 ``g``s, so the character class is the
    actual constraint; the length test bounds it.

    Expressed by stripping every allowed digit and requiring an empty
    remainder, because it has to compile on both SQLite and PostgreSQL:
    ``GLOB`` is SQLite-only and is a syntax error on the deployment target,
    while ``SIMILAR TO``/regex/``translate`` are not portable the other way.
    The migration carries a byte-identical copy of this function on purpose —
    it must never import from the application.
    """
    stripped = column
    for digit in "0123456789abcdef":
        stripped = f"replace({stripped}, '{digit}', '')"
    return f"(length({column}) = 64 AND {stripped} = '')"


# Explicit-repeat semantics are enabled only for rows created after the
# protocol was approved.  Historical rows keep both columns NULL; the database
# forbids filling exactly one of them, and no migration ever guesses a version
# for a row that never used one.  Both branches are stated positively so a NULL
# can never make the predicate itself evaluate to NULL and pass.
REPEAT_PROTOCOL_BINDING_CHECK = (
    "((repeat_protocol_version_id IS NULL AND "
    "repeat_protocol_definition_digest IS NULL) OR "
    "(repeat_protocol_version_id IS NOT NULL AND "
    "repeat_protocol_definition_digest IS NOT NULL AND "
    "length(trim(repeat_protocol_version_id)) > 0 AND "
    + _hex64_sql("repeat_protocol_definition_digest") + "))"
)

# A NULL/NULL profile pair is the permanent representation of the canonical
# full-source plan.  A populated pair is an explicit, immutable demo-profile
# binding.  State both branches positively: SQLite treats a CHECK that
# evaluates to NULL as passing, so relying on implication-style predicates
# would admit half-populated or malformed evidence.
AUTOPILOT_PROFILE_BINDING_CHECK = (
    "((autopilot_profile_version_id IS NULL AND "
    "autopilot_profile_definition_digest IS NULL) OR "
    "(autopilot_profile_version_id IS NOT NULL AND "
    "autopilot_profile_definition_digest IS NOT NULL AND "
    "length(trim(autopilot_profile_version_id)) > 0 AND "
    "autopilot_profile_version_id = trim(autopilot_profile_version_id) AND "
    + _hex64_sql("autopilot_profile_definition_digest") + "))"
)

AUTOPILOT_PROFILE_SIMULATION_CHECK = (
    "((autopilot_profile_version_id IS NULL AND "
    "autopilot_profile_definition_digest IS NULL) OR "
    "(autopilot_profile_version_id IS NOT NULL AND "
    "autopilot_profile_definition_digest IS NOT NULL AND "
    "is_simulation AND data_classification = 'simulation'))"
)

AUTOPILOT_PROFILE_SESSION_PLAN_LINK_CHECK = (
    "((autopilot_profile_version_id IS NULL AND "
    "autopilot_profile_definition_digest IS NULL) OR "
    "(autopilot_profile_version_id IS NOT NULL AND "
    "autopilot_profile_definition_digest IS NOT NULL AND "
    "visit_plan_id IS NOT NULL))"
)

# The pair check above is satisfied by an empty pair, so on its own it lets a
# fully-formed replay command carry no repeat binding at all.  A replay only
# exists because the protocol authorised it, so any replay provenance column
# forces a complete, well-formed binding.  Historical ordinary commands keep
# all three provenance columns NULL and are untouched by this.
REPLAY_REQUIRES_REPEAT_BINDING_CHECK = (
    "((replay_source_command_id IS NULL AND replay_ordinal IS NULL AND "
    "replay_source_payload_sha256 IS NULL) OR "
    "(repeat_protocol_version_id IS NOT NULL AND "
    "repeat_protocol_definition_digest IS NOT NULL AND "
    "length(trim(repeat_protocol_version_id)) > 0 AND "
    + _hex64_sql("repeat_protocol_definition_digest") + "))"
)


class Patient(SQLModel, table=True):
    __table_args__ = (
        CheckConstraint(
            "governance_revision >= 0",
            name="ck_patient_governance_revision_nonnegative"),
    )

    patient_id: str = Field(primary_key=True)
    # 模拟受试者必须显式标识，不允许用普通缺字段真人档案绕过研究准入。
    is_simulation_subject: bool = False
    dementia_severity: Optional[str] = None
    # 入组资格
    mandarin_eligible: Optional[bool] = None
    language_eligibility: Optional[str] = None
    # 合规字段（护栏2，全部 nullable 预留，SOP 未回也先留空）
    consent_status: Optional[str] = None
    consent_type: Optional[ConsentType] = None
    consent_time: Optional[datetime] = None
    consent_person: Optional[str] = None
    proxy_consent: Optional[bool] = None
    capacity_assessment_status: Optional[str] = None
    assent_obtained: Optional[bool] = None
    recording_allowed: Optional[bool] = None
    secondary_use_allowed: Optional[bool] = None
    # 云处理是独立于录音采集、研究参与和二次使用的版本化授权。provider/notice
    # 及时间只由服务器按当前非机密 policy 写入，客户端不得自报版本。
    cloud_processing_allowed: Optional[bool] = None
    cloud_processing_provider_id: Optional[str] = None
    cloud_processing_notice_version: Optional[str] = None
    cloud_processing_consented_at: Optional[datetime] = None
    cloud_processing_revoked_at: Optional[datetime] = None
    # Server-owned CAS fence for patient-level governance.  Ordinary profile
    # creation/update routes must never accept a client-selected value.
    governance_revision: int = 0
    withdrawal_status: Optional[str] = None
    ethics_approval_no: Optional[str] = None
    registration_no: Optional[str] = None


class PatientWithdrawalEvent(SQLModel, table=True):
    """Append-only authoritative receipt for a subject research withdrawal.

    The ledger deliberately stores no free text, patient answers, clinical
    notes, credentials, or plaintext idempotency keys.  ``Patient`` carries the
    current governance state while this table proves the exact transition.
    """
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key_sha256",
            name="uq_patient_withdrawal_idempotency_hash",
        ),
        UniqueConstraint(
            "patient_id", "new_revision",
            name="uq_patient_withdrawal_patient_revision",
        ),
        Index(
            "ix_patient_withdrawal_patient_occurred",
            "patient_id", "occurred_at",
        ),
        CheckConstraint(
            "reason_code IN ('participant_request','representative_request',"
            "'clinical_safety','ethics_or_protocol')",
            name="ck_patient_withdrawal_reason",
        ),
        CheckConstraint(
            "actor_role = 'admin'",
            name="ck_patient_withdrawal_actor_role",
        ),
        CheckConstraint(
            "expected_revision >= 0 AND new_revision = expected_revision + 1",
            name="ck_patient_withdrawal_revision_transition",
        ),
        CheckConstraint(
            "length(idempotency_key_sha256) = 64 AND "
            "length(request_fingerprint) = 64",
            name="ck_patient_withdrawal_hash_lengths",
        ),
        CheckConstraint(
            "length(trim(actor_display_id)) > 0",
            name="ck_patient_withdrawal_actor_nonempty",
        ),
        CheckConstraint(
            "affected_session_count >= 0 AND affected_audio_count >= 0",
            name="ck_patient_withdrawal_counts_nonnegative",
        ),
    )

    event_id: str = Field(primary_key=True)
    patient_id: str = Field(foreign_key="patient.patient_id", index=True)
    expected_revision: int
    new_revision: int
    idempotency_key_sha256: str
    request_fingerprint: str
    reason_code: str
    actor_display_id: str
    actor_role: str
    occurred_at: datetime = Field(default_factory=_utc_now_naive)
    affected_session_count: int = 0
    affected_audio_count: int = 0


@sa_event.listens_for(PatientWithdrawalEvent, "before_update")
@sa_event.listens_for(PatientWithdrawalEvent, "before_delete")
def _reject_patient_withdrawal_event_mutation(*_args) -> None:
    raise RuntimeError("PatientWithdrawalEvent 是只追加研究撤回账本")


class VisitPlan(SQLModel, table=True):
    """Pre-approved bedside visit plan; no patient-facing scheduling metadata.

    ``protocol_slot_key`` is the active-slot exclusion key.  Cancelling clears it
    to NULL, which releases the slot while preserving the immutable plan history.
    Starting a visit creates a :class:`Session` that points back through the sole
    ``Session.visit_plan_id`` link; the plan deliberately stores no session id.
    """
    __table_args__ = (
        UniqueConstraint(
            "protocol_slot_key", name="uq_visit_plan_protocol_slot_key"),
        Index(
            "ix_visit_plan_queue",
            "status", "scheduled_date", "queue_order", "scheduled_time"),
        Index("ix_visit_plan_patient_status", "patient_id", "status"),
        CheckConstraint(
            "status IN ('draft','approved','started','cancelled')",
            name="ck_visit_plan_status"),
        CheckConstraint("revision >= 1", name="ck_visit_plan_revision_positive"),
        CheckConstraint(
            "status = 'cancelled' OR protocol_slot_key IS NOT NULL",
            name="ck_visit_plan_active_slot"),
        CheckConstraint(
            "queue_order IS NULL OR (queue_order >= 0 AND queue_order <= 100000)",
            name="ck_visit_plan_queue_order_range"),
        CheckConstraint(
            "session_sitting_no >= 1",
            name="ck_visit_plan_sitting_positive"),
        CheckConstraint(
            "week_no >= 1 AND week_no <= 8",
            name="ck_visit_plan_week"),
        CheckConstraint(
            "data_classification IN ('research','simulation')",
            name="ck_visit_plan_data_classification"),
        CheckConstraint(
            "((item_bank_definition_digest IS NULL AND "
            "autopilot_protocol_version_id IS NULL AND "
            "autopilot_protocol_definition_digest IS NULL) OR "
            "(length(item_bank_definition_digest) = 64 AND "
            "lower(item_bank_definition_digest) = item_bank_definition_digest AND "
            "length(trim(autopilot_protocol_version_id)) > 0 AND "
            "length(autopilot_protocol_definition_digest) = 64 AND "
            "lower(autopilot_protocol_definition_digest) = "
            "autopilot_protocol_definition_digest))",
            name="ck_visit_plan_definition_binding_complete"),
        CheckConstraint(
            REPEAT_PROTOCOL_BINDING_CHECK,
            name="ck_visit_plan_repeat_binding_complete"),
        CheckConstraint(
            AUTOPILOT_PROFILE_BINDING_CHECK,
            name="ck_visit_plan_autopilot_profile_binding_complete"),
        CheckConstraint(
            AUTOPILOT_PROFILE_SIMULATION_CHECK,
            name="ck_visit_plan_autopilot_profile_simulation_only"),
    )

    plan_id: str = Field(primary_key=True)
    protocol_slot_key: Optional[str] = None
    patient_id: str = Field(foreign_key="patient.patient_id", index=True)
    scheduled_date: date
    scheduled_time: Optional[time] = None
    queue_order: Optional[int] = None
    session_sitting_no: int = 1
    week_no: int
    phase_type: PhaseType
    event_line: EventLine
    item_bank_version_id: str
    # Historical rows remain nullable.  Every new plan is populated by the
    # service and approval/start fail closed when this exact definition binding
    # is absent or no longer matches the current files.
    item_bank_definition_digest: Optional[str] = None
    autopilot_protocol_version_id: Optional[str] = None
    autopilot_protocol_definition_digest: Optional[str] = None
    autopilot_profile_version_id: Optional[str] = None
    autopilot_profile_definition_digest: Optional[str] = None
    repeat_protocol_version_id: Optional[str] = None
    repeat_protocol_definition_digest: Optional[str] = None
    is_simulation: bool = False
    data_classification: str = "research"
    status: str = "draft"
    revision: int = 1
    created_by: str
    created_at: datetime = Field(default_factory=_utc_now_naive)
    updated_at: datetime = Field(default_factory=_utc_now_naive)
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    started_by: Optional[str] = None
    started_at: Optional[datetime] = None
    cancelled_by: Optional[str] = None
    cancelled_at: Optional[datetime] = None


class VisitPlanCommand(SQLModel, table=True):
    """Append-only command receipt and idempotency ledger for visit plans."""
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_visit_plan_command_idempotency_key"),
        UniqueConstraint(
            "plan_id", "event_seq", name="uq_visit_plan_command_plan_event_seq"),
        Index(
            "ix_visit_plan_command_plan_created",
            "plan_id", "created_at"),
        CheckConstraint(
            "command_type IN ('create','approve','start','cancel')",
            name="ck_visit_plan_command_type"),
        CheckConstraint(
            "event_seq >= 1", name="ck_visit_plan_command_event_seq_positive"),
        CheckConstraint(
            "expected_revision >= 0",
            name="ck_visit_plan_command_expected_revision_nonnegative"),
        CheckConstraint(
            "resulting_revision = expected_revision + 1",
            name="ck_visit_plan_command_revision_transition"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    plan_id: str = Field(foreign_key="visitplan.plan_id", index=True)
    event_seq: int
    idempotency_key: str
    command_type: str
    request_hash: str
    actor_id: str
    expected_revision: int
    resulting_revision: int
    reason_code: Optional[str] = None
    created_at: datetime = Field(default_factory=_utc_now_naive)


@sa_event.listens_for(VisitPlanCommand, "before_update")
@sa_event.listens_for(VisitPlanCommand, "before_delete")
def _reject_visit_plan_command_mutation(*_args) -> None:
    raise RuntimeError("VisitPlanCommand 是只追加命令账本，禁止更新或删除")


class Session(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("visit_plan_id", name="uq_session_visit_plan_id"),
        CheckConstraint(
            "((item_bank_definition_digest IS NULL AND "
            "autopilot_protocol_version_id IS NULL AND "
            "autopilot_protocol_definition_digest IS NULL) OR "
            "(length(item_bank_definition_digest) = 64 AND "
            "lower(item_bank_definition_digest) = item_bank_definition_digest AND "
            "length(trim(autopilot_protocol_version_id)) > 0 AND "
            "length(autopilot_protocol_definition_digest) = 64 AND "
            "lower(autopilot_protocol_definition_digest) = "
            "autopilot_protocol_definition_digest))",
            name="ck_session_definition_binding_complete"),
        CheckConstraint(
            REPEAT_PROTOCOL_BINDING_CHECK,
            name="ck_session_repeat_binding_complete"),
        CheckConstraint(
            AUTOPILOT_PROFILE_BINDING_CHECK,
            name="ck_session_autopilot_profile_binding_complete"),
        CheckConstraint(
            AUTOPILOT_PROFILE_SIMULATION_CHECK,
            name="ck_session_autopilot_profile_simulation_only"),
        CheckConstraint(
            AUTOPILOT_PROFILE_SESSION_PLAN_LINK_CHECK,
            name="ck_session_autopilot_profile_requires_visit_plan"),
    )

    session_id: str = Field(primary_key=True)
    patient_id: str = Field(foreign_key="patient.patient_id", index=True)
    # Only started VisitPlans receive this link.  Legacy/direct sessions remain
    # NULL, and the unique constraint prevents one plan from starting twice.
    visit_plan_id: Optional[str] = Field(
        default=None, foreign_key="visitplan.plan_id")
    session_sitting_no: int = 1                 # 分日/暂停续做序号
    training_date: Optional[date] = None
    week_no: int                                # 1..8
    phase_type: PhaseType                        # 按事件显式绑定，不由 week_no 推导
    event_line: EventLine
    trainer_id: Optional[str] = None
    item_bank_version_id: str                    # 每场次绑冻结题库版本号
    item_bank_definition_digest: Optional[str] = None
    autopilot_protocol_version_id: Optional[str] = None
    autopilot_protocol_definition_digest: Optional[str] = None
    autopilot_profile_version_id: Optional[str] = None
    autopilot_profile_definition_digest: Optional[str] = None
    repeat_protocol_version_id: Optional[str] = None
    repeat_protocol_definition_digest: Optional[str] = None
    # 真实研究与模拟数据必须持久分流。默认 False，任何未显式标记的既有/新场次都按真实研究处理。
    is_simulation: bool = False
    # 新建行由服务端写 research/simulation；迁移时旧数据回填 legacy_unknown。
    data_classification: str = "research"


class ItemEvent(SQLModel, table=True):
    """一题一行（parent）。双/多要素的分环节在 TurnEvent。"""
    __table_args__ = (
        UniqueConstraint(
            "session_id", "item_id", name="uq_item_event_session_item"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="session.session_id", index=True)
    item_id: str = Field(index=True)
    image_id: Optional[str] = None
    task_type: TaskType
    item_set_type: ItemSetType
    difficulty_level: Optional[str] = None       # 仅记录，不据此分支
    presentation_order: Optional[int] = None
    random_seed: Optional[int] = None


class TurnEvent(SQLModel, table=True):
    """每环节一行（★ 支持双/多要素多轮）。单要素退化为单环节 turn_seq=1。"""
    __table_args__ = (
        CheckConstraint(
            "confirmation_revision >= 0",
            name="ck_turn_event_confirmation_revision_nonnegative"),
        UniqueConstraint(
            "item_event_id", "turn_seq",
            name="uq_turn_event_item_turn_seq"),
        UniqueConstraint("source_attempt_id", name="uq_turn_source_attempt_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    item_event_id: int = Field(foreign_key="itemevent.id", index=True)
    # 由逐次账本收口的终值环节显式指向权威 attempt，避免二次 AI 判类漂移。
    source_attempt_id: Optional[int] = Field(
        default=None, foreign_key="attemptevent.id", index=True)
    turn_seq: int
    response_role: Optional[str] = None          # 左命名/左作用/关系识别/关键要素…
    # 语音 & 识别
    raw_audio_id: Optional[str] = None
    asr_text: Optional[str] = None               # 写入即只读
    asr_confidence: Optional[float] = None
    confirmed_response_text: Optional[str] = None  # 与 asr_text 物理分字段
    # 研究复核确认文本的服务端 CAS 版本。0 表示尚未人工确认；
    # 每次成功修订只能 +1，并在 TurnConfirmationRevision 追加无原文摘要。
    confirmation_revision: int = 0
    # 时间
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    naming_latency_ms: Optional[int] = None
    # 提示/线索
    prompt_level: Optional[int] = None
    cue_type: Optional[str] = None
    # AI 判定（仅辅助，永不覆盖锁定分）
    ai_answer_type: Optional[str] = None
    ai_score: Optional[float] = None
    ai_needs_review: Optional[bool] = None
    ai_judge_mode: Optional[str] = None          # 规则确定式 / LLM辅助（JudgingMode,审计用）
    judge_portrait_used: bool = False            # 恒 False，审计用
    # 人工锁定评分（研究数据默认取此）
    reviewer_id: Optional[str] = None
    reviewed_score: Optional[float] = None
    score_locked: bool = False
    # 双要素分环节 0/1 或关系 1/0.5/0；多要素关键要素 0/1（原始锁定值，综合分由 scoring 算）
    element_value: Optional[float] = None


class TurnConfirmationRevision(SQLModel, table=True):
    """人工确认文本的 append-only 修订账本。

    账本只保存前后 SHA-256，不复制患者作答原文；当前权威文本仍只在
    TurnEvent.confirmed_response_text 中保存一份。
    """
    __table_args__ = (
        UniqueConstraint(
            "turn_id", "revision", name="uq_turn_confirmation_revision"),
        UniqueConstraint(
            "idempotency_key", name="uq_turn_confirmation_idempotency_key"),
        CheckConstraint(
            "expected_revision >= 0",
            name="ck_turn_confirmation_expected_revision_nonnegative"),
        CheckConstraint(
            "revision = expected_revision + 1",
            name="ck_turn_confirmation_revision_transition"),
        CheckConstraint(
            "length(before_sha256) = 64 AND length(after_sha256) = 64",
            name="ck_turn_confirmation_hash_lengths"),
        CheckConstraint(
            "length(trim(actor_display_id)) > 0",
            name="ck_turn_confirmation_actor_nonempty"),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 8 AND 200",
            name="ck_turn_confirmation_idempotency_key_length"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    turn_id: int = Field(foreign_key="turnevent.id", index=True)
    session_id: str = Field(foreign_key="session.session_id", index=True)
    revision: int
    expected_revision: int
    actor_display_id: str
    changed_at: datetime = Field(default_factory=_utc_now_naive)
    before_sha256: str
    after_sha256: str
    idempotency_key: str


@sa_event.listens_for(TurnConfirmationRevision, "before_update")
@sa_event.listens_for(TurnConfirmationRevision, "before_delete")
def _reject_turn_confirmation_revision_mutation(*_args) -> None:
    raise RuntimeError("TurnConfirmationRevision 是只追加修订账本")


class AttemptEvent(SQLModel, table=True):
    """AI 自动驾的逐次证据，不是人工锁定的研究真值。

    同一环节可有多次录音/转写/判类；``attempt_seq`` 由服务端递增。
    ``raw_audio_id`` 全局唯一，既是幂等键，也防止一段录音被重复归属。
    """
    __table_args__ = (
        UniqueConstraint("session_id", "item_id", "turn_seq", "attempt_seq",
                         name="uq_attempt_session_item_turn_seq"),
        UniqueConstraint("raw_audio_id", name="uq_attempt_raw_audio_id"),
        CheckConstraint("attempt_seq >= 1", name="ck_attempt_seq_positive"),
        CheckConstraint("prompt_level >= 0 AND prompt_level <= 3",
                        name="ck_attempt_prompt_level"),
        CheckConstraint(
            "processing_status IN ('received','asr_completed','completed','technical_failure')",
            name="ck_attempt_processing_status"),
        CheckConstraint("processing_generation >= 0",
                        name="ck_attempt_processing_generation_nonnegative"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="session.session_id", index=True)
    item_id: str = Field(index=True)
    turn_seq: int
    response_role: str
    attempt_seq: int
    raw_audio_id: str = Field(foreign_key="audioassetrow.raw_audio_id", index=True)
    prompt_level: int
    cue_type: Optional[str] = None
    duration_seconds: Optional[float] = None
    asr_text: Optional[str] = None
    asr_confidence: Optional[float] = None
    asr_engine_version: Optional[str] = None
    operational_answer_type: Optional[str] = None
    operational_score: Optional[float] = None
    operational_needs_review: Optional[bool] = None
    judge_mode: Optional[str] = None
    judge_engine_version: Optional[str] = None
    judge_reason: Optional[str] = None
    matched_on: Optional[str] = None
    contains_target: Optional[bool] = None
    judge_portrait_used: bool = False
    processing_status: str = "received"  # received / completed / technical_failure
    # 可恢复的处理租约：status 是阶段真源，owner/generation 是防陈旧
    # worker 回写的 fencing token。既有非终态行允许 owner 为空，升级后可立即接管。
    processing_owner: Optional[str] = None
    processing_lease_expires_at: Optional[datetime] = None
    processing_claimed_at: Optional[datetime] = None
    processing_generation: int = 0
    error_code: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)
    processed_at: Optional[datetime] = None
    is_simulation: bool = False


class AttemptCaptureProcessing(SQLModel, table=True):
    """Persistent, recoverable claim binding one succeeded record command to
    the AttemptEvent it will eventually produce.

    This row is admitted at ``record_stopped`` time, before ASR runs, so a
    worker can crash-safely resume ASR without re-consuming ``attempt_seq``.
    AttemptEvent creation itself is deferred until ASR resolves, so a future
    repeat-classifier can inspect the transcript before ``attempt_seq`` is
    ever committed. This batch always disposes a successful transcript as
    ``answer_candidate``; the repeat branch is intentionally inert. The
    answer text itself lives only in ``AttemptEvent.asr_text`` — this row
    never holds a second permanent copy of patient-derived transcript text.
    """
    __table_args__ = (
        UniqueConstraint("record_command_id",
                         name="uq_capture_processing_record_command"),
        UniqueConstraint("raw_audio_id",
                         name="uq_capture_processing_raw_audio_id"),
        UniqueConstraint("receipt_server_seq",
                         name="uq_capture_processing_receipt_seq"),
        UniqueConstraint("final_attempt_id",
                         name="uq_capture_processing_final_attempt_id"),
        UniqueConstraint("repeat_request_id",
                         name="uq_capture_processing_repeat_request_id"),
        CheckConstraint("proof_attempt_seq >= 1",
                        name="ck_capture_processing_proof_seq_positive"),
        CheckConstraint("proof_prompt_level >= 0 AND proof_prompt_level <= 3",
                        name="ck_capture_processing_proof_prompt_level"),
        CheckConstraint(
            "processing_status IN ('received','asr_completed','technical_failure')",
            name="ck_capture_processing_status"),
        CheckConstraint("processing_generation >= 0",
                        name="ck_capture_processing_generation_nonnegative"),
        CheckConstraint(
            "disposition IS NULL OR disposition IN "
            "('answer_candidate','repeat_replayed','repeat_limit_paused')",
            name="ck_capture_processing_disposition"),
        CheckConstraint(
            "((processing_status = 'asr_completed' AND disposition IS NOT NULL) OR "
            "(processing_status != 'asr_completed' AND disposition IS NULL))",
            name="ck_capture_processing_disposition_matches_status"),
        # Terminal outcomes are mutually exclusive at the database level: an
        # answer candidate binds an Attempt and never a repeat request; a
        # repeat disposition binds a repeat request and never an Attempt.
        CheckConstraint(
            "((processing_status = 'received' AND final_attempt_id IS NULL "
            "AND repeat_request_id IS NULL) OR "
            "(processing_status = 'technical_failure' "
            "AND final_attempt_id IS NOT NULL AND repeat_request_id IS NULL) OR "
            "(processing_status = 'asr_completed' "
            "AND disposition IS NOT NULL AND disposition = 'answer_candidate' "
            "AND final_attempt_id IS NOT NULL AND repeat_request_id IS NULL) OR "
            "(processing_status = 'asr_completed' AND disposition IS NOT NULL "
            "AND disposition IN ('repeat_replayed','repeat_limit_paused') "
            "AND final_attempt_id IS NULL AND repeat_request_id IS NOT NULL))",
            name="ck_capture_processing_final_attempt_matches_status"),
        CheckConstraint(
            REPEAT_PROTOCOL_BINDING_CHECK,
            name="ck_capture_processing_repeat_binding_complete"),
        CheckConstraint(
            "repeat_admission_semantics IN ('legacy_pre_repeat','repeat_bound')",
            name="ck_capture_processing_admission_semantics"),
        # The marker is what separates "admitted before the protocol existed"
        # from "admitted under it".  A legacy row can never acquire a repeat
        # binding, a repeat request or a repeat disposition; a bound row must
        # carry a complete, well-formed binding.
        CheckConstraint(
            "((repeat_admission_semantics = 'legacy_pre_repeat' AND "
            "repeat_protocol_version_id IS NULL AND "
            "repeat_protocol_definition_digest IS NULL AND "
            "repeat_request_id IS NULL AND "
            "(disposition IS NULL OR disposition = 'answer_candidate')) OR "
            "(repeat_admission_semantics = 'repeat_bound' AND "
            "repeat_protocol_version_id IS NOT NULL AND "
            "repeat_protocol_definition_digest IS NOT NULL AND "
            "length(trim(repeat_protocol_version_id)) > 0 AND "
            + _hex64_sql("repeat_protocol_definition_digest") + "))",
            name="ck_capture_processing_admission_marker_binding"),
        CheckConstraint(
            "((processing_owner IS NULL AND processing_lease_expires_at IS NULL) OR "
            "(processing_owner IS NOT NULL AND processing_lease_expires_at IS NOT NULL))",
            name="ck_capture_processing_lease_complete"),
        CheckConstraint(
            "(processing_owner IS NULL OR processing_claimed_at IS NOT NULL)",
            name="ck_capture_processing_claimed_at_when_owned"),
        CheckConstraint(
            "(processing_status = 'received' OR "
            "(processing_owner IS NULL AND processing_lease_expires_at IS NULL))",
            name="ck_capture_processing_terminal_lease_released"),
        CheckConstraint(
            "((processing_status = 'received' AND processed_at IS NULL) OR "
            "(processing_status != 'received' AND processed_at IS NOT NULL))",
            name="ck_capture_processing_terminal_processed_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    record_command_id: int = Field(foreign_key="runtimecommand.id", index=True)
    predecessor_command_id: int = Field(foreign_key="runtimecommand.id", index=True)
    receipt_server_seq: int = Field(
        foreign_key="audiocapturereceipt.server_seq", index=True)
    raw_audio_id: str = Field(foreign_key="audioassetrow.raw_audio_id", index=True)
    session_id: str = Field(foreign_key="session.session_id", index=True)
    item_id: str = Field(index=True)
    turn_seq: int
    proof_attempt_seq: int
    proof_prompt_level: int
    processing_status: str = "received"
    # 与 AttemptEvent 相同的可恢复租约结构；status 是阶段真源，
    # owner/generation 是防陈旧 worker 回写的 fencing token。
    processing_owner: Optional[str] = None
    processing_lease_expires_at: Optional[datetime] = None
    processing_claimed_at: Optional[datetime] = None
    processing_generation: int = 0
    asr_confidence: Optional[float] = None
    asr_engine_version: Optional[str] = None
    disposition: Optional[str] = None
    error_code: Optional[str] = None
    final_attempt_id: Optional[int] = Field(
        default=None, foreign_key="attemptevent.id", index=True)
    # Written once at admission and never by a caller: rows admitted before the
    # repeat protocol existed are backfilled ``legacy_pre_repeat`` by the
    # migration, and every row the running service admits is ``repeat_bound``.
    repeat_admission_semantics: str
    # Copied unchanged from the record command at admission; a historical
    # capture keeps NULL and its worker runs the pre-repeat answer flow.
    repeat_protocol_version_id: Optional[str] = None
    repeat_protocol_definition_digest: Optional[str] = None
    # The reverse half of a genuine one-to-one: AutopilotRepeatRequest holds a
    # unique foreign key back to this row, and this unique foreign key points at
    # it.  The pair is mutually dependent, so the constraint is emitted with
    # ``use_alter`` to keep table creation orderable.
    repeat_request_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            "repeat_request_id",
            Integer,
            ForeignKey("autopilotrepeatrequest.id",
                       name="fk_capture_processing_repeat_request",
                       use_alter=True),
            nullable=True,
            index=True,
        ))
    created_at: datetime = Field(default_factory=datetime.now)
    processed_at: Optional[datetime] = None
    is_simulation: bool = False


@sa_event.listens_for(AttemptCaptureProcessing, "before_update")
@sa_event.listens_for(AttemptCaptureProcessing, "before_delete")
def _reject_attempt_capture_processing_orm_mutation(*_args) -> None:
    """生命周期只能走 fenced Core UPDATE；ORM 层一律拒绝改写或删除。"""
    raise RuntimeError(
        "AttemptCaptureProcessing 禁止 ORM 更新或删除，须走 evidence_ledger 的 fenced Core 事务")


class AutopilotRepeatRequest(SQLModel, table=True):
    """Append-only record of one patient request to hear the prompt again.

    A repeat request is deliberately not an answer attempt: it consumes no
    ``attempt_seq``, no cue and no prompt level, and it never produces an
    :class:`AttemptEvent`.  Ordinal 1 issues a new TTS command carrying the
    predecessor command's exact canonical ``payload_json``; the audio itself is
    re-synthesized downstream.  Ordinal 2 in the same logical slot pauses for a
    researcher instead of replaying again.

    The patient's words are never persisted here.  Only the approved
    ``phrase_key`` and the SHA-256 of the normalized string are kept, so the
    ledger can be audited without holding a second copy of patient speech.
    """
    __table_args__ = (
        UniqueConstraint("capture_processing_id",
                         name="uq_repeat_request_capture_processing"),
        UniqueConstraint("session_id", "item_id", "turn_seq", "attempt_seq",
                         "prompt_level", "repeat_ordinal",
                         name="uq_repeat_request_logical_slot_ordinal"),
        UniqueConstraint("replay_command_id",
                         name="uq_repeat_request_replay_command"),
        UniqueConstraint("source_tts_command_id",
                         name="uq_repeat_request_source_command"),
        UniqueConstraint("session_id", "pause_control_event_seq",
                         name="uq_repeat_request_pause_event"),
        Index("ix_repeat_request_session_created", "session_id", "created_at"),
        ForeignKeyConstraint(
            ["record_command_id", "session_id"],
            ["runtimecommand.id", "runtimecommand.session_id"],
            name="fk_repeat_request_record_command_same_session"),
        ForeignKeyConstraint(
            ["source_tts_command_id", "session_id"],
            ["runtimecommand.id", "runtimecommand.session_id"],
            name="fk_repeat_request_source_command_same_session"),
        ForeignKeyConstraint(
            ["replay_command_id", "session_id"],
            ["runtimecommand.id", "runtimecommand.session_id"],
            name="fk_repeat_request_replay_command_same_session"),
        ForeignKeyConstraint(
            ["session_id", "pause_control_event_seq"],
            ["autopilotcontrolevent.session_id",
             "autopilotcontrolevent.event_seq"],
            name="fk_repeat_request_pause_event_same_session"),
        CheckConstraint("turn_seq >= 1", name="ck_repeat_request_turn_positive"),
        CheckConstraint("attempt_seq >= 1",
                        name="ck_repeat_request_attempt_positive"),
        CheckConstraint("prompt_level >= 0 AND prompt_level <= 3",
                        name="ck_repeat_request_prompt_level"),
        CheckConstraint("repeat_ordinal IN (1, 2)",
                        name="ck_repeat_request_ordinal"),
        CheckConstraint("outcome IN ('replayed','limit_paused')",
                        name="ck_repeat_request_outcome"),
        CheckConstraint(
            "((repeat_ordinal = 1 AND outcome = 'replayed' AND "
            "replay_command_id IS NOT NULL AND "
            "pause_control_event_seq IS NULL) OR "
            "(repeat_ordinal = 2 AND outcome = 'limit_paused' AND "
            "replay_command_id IS NULL AND "
            "pause_control_event_seq IS NOT NULL))",
            name="ck_repeat_request_ordinal_matches_outcome"),
        CheckConstraint(
            "pause_control_event_seq IS NULL OR pause_control_event_seq >= 1",
            name="ck_repeat_request_pause_event_seq_positive"),
        CheckConstraint(
            _hex64_sql("source_payload_sha256"),
            name="ck_repeat_request_source_payload_digest"),
        CheckConstraint(
            _hex64_sql("normalized_text_sha256"),
            name="ck_repeat_request_normalized_text_digest"),
        CheckConstraint(
            "length(trim(repeat_protocol_version_id)) > 0 AND "
            + _hex64_sql("repeat_protocol_definition_digest"),
            name="ck_repeat_request_protocol_binding"),
        CheckConstraint("length(trim(phrase_key)) > 0",
                        name="ck_repeat_request_phrase_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    capture_processing_id: int = Field(
        foreign_key="attemptcaptureprocessing.id", index=True)
    session_id: str = Field(foreign_key="session.session_id", index=True)
    item_id: str = Field(index=True)
    turn_seq: int
    attempt_seq: int
    prompt_level: int
    repeat_ordinal: int
    outcome: str                            # replayed / limit_paused
    record_command_id: int = Field(index=True)
    raw_audio_id: str = Field(
        foreign_key="audioassetrow.raw_audio_id", index=True)
    source_tts_command_id: int
    # SHA-256 of the source RuntimeCommand.payload_json UTF-8 bytes only.
    # It is not a digest of any synthesized TTS audio, and binds no WAV.
    source_payload_sha256: str
    replay_command_id: Optional[int] = None
    pause_control_event_seq: Optional[int] = None
    asr_confidence: Optional[float] = None
    asr_engine_version: Optional[str] = None
    repeat_protocol_version_id: str
    repeat_protocol_definition_digest: str
    # 只存批准协议里的封闭短语键与规范化文本摘要，绝不存原始转写。
    phrase_key: str
    normalized_text_sha256: str
    created_at: datetime = Field(default_factory=datetime.now)
    is_simulation: bool = False


@sa_event.listens_for(AutopilotRepeatRequest, "before_update")
@sa_event.listens_for(AutopilotRepeatRequest, "before_delete")
def _reject_autopilot_repeat_request_mutation(*_args) -> None:
    raise RuntimeError("AutopilotRepeatRequest 是只追加账本，禁止更新或删除")


class InteractionEvent(SQLModel, table=True):
    """运行交互只追加账本。

    ``payload_json`` 仅由服务端按 event_type 白名单生成，不保存回答文本或画像。
    应用无 update/delete 入口；``event_seq`` 在场次内服务端单调分配。
    """
    __table_args__ = (
        UniqueConstraint("session_id", "event_seq",
                         name="uq_interaction_session_event_seq"),
        CheckConstraint("event_seq >= 1", name="ck_interaction_event_seq_positive"),
        CheckConstraint(
            "event_type IN ('attempt_received','asr_completed','asr_failed',"
            "'judgement_completed','judgement_failed','cue_selected',"
            "'feedback_selected','technical_pause','researcher_takeover')",
            name="ck_interaction_event_type"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="session.session_id", index=True)
    event_seq: int
    item_id: Optional[str] = Field(default=None, index=True)
    turn_seq: Optional[int] = None
    attempt_id: Optional[int] = Field(default=None, foreign_key="attemptevent.id", index=True)
    attempt_seq: Optional[int] = None
    event_type: str = Field(index=True)
    payload_json: str = "{}"
    created_at: datetime = Field(default_factory=datetime.now)
    is_simulation: bool = False


@sa_event.listens_for(InteractionEvent, "before_update")
@sa_event.listens_for(InteractionEvent, "before_delete")
def _reject_interaction_event_mutation(*_args) -> None:
    raise RuntimeError("InteractionEvent 是只追加运行证据，禁止更新或删除")


class InteractionPresentationReceipt(SQLModel, table=True):
    """Durable receipt for one atomic bedside interaction presentation.

    InteractionEvent remains the immutable clinical/operational ledger.  This
    companion row stores only delivery-control facts needed to make an HTTP
    retry an exact read instead of a second event.  ``attempt_id`` is nullable
    for explicitly manual cues; when present it is unique so one completed
    attempt cannot branch into two different cue/feedback terminal actions.
    """
    __table_args__ = (
        UniqueConstraint(
            "session_id", "idempotency_key",
            name="uq_interaction_presentation_session_idempotency"),
        UniqueConstraint(
            "interaction_event_id",
            name="uq_interaction_presentation_event"),
        UniqueConstraint(
            "attempt_id",
            name="uq_interaction_presentation_attempt"),
        CheckConstraint(
            "length(trim(idempotency_key)) BETWEEN 16 AND 200",
            name="ck_interaction_presentation_idempotency_length"),
        CheckConstraint(
            "length(request_hash) = 64 AND lower(request_hash) = request_hash",
            name="ck_interaction_presentation_request_hash"),
        CheckConstraint(
            "live_seq >= 1",
            name="ck_interaction_presentation_live_seq_positive"),
        CheckConstraint(
            "command_wseq >= 0",
            name="ck_interaction_presentation_wseq_nonnegative"),
        CheckConstraint(
            "runtime_revision >= 1",
            name="ck_interaction_presentation_runtime_revision_positive"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="session.session_id", index=True)
    interaction_event_id: int = Field(
        foreign_key="interactionevent.id", index=True)
    attempt_id: Optional[int] = Field(
        default=None, foreign_key="attemptevent.id", index=True)
    idempotency_key: str = Field(index=True)
    request_hash: str
    cursor_json: str
    live_seq: int
    command_wseq: int
    runtime_revision: int
    created_at: datetime = Field(default_factory=datetime.now)


@sa_event.listens_for(InteractionPresentationReceipt, "before_update")
@sa_event.listens_for(InteractionPresentationReceipt, "before_delete")
def _reject_interaction_presentation_receipt_mutation(*_args) -> None:
    raise RuntimeError("InteractionPresentationReceipt 是只追加幂等回执")


class TechnicalPauseReceipt(SQLModel, table=True):
    """Durable receipt for one atomic researcher-triggered technical pause.

    The linked ``InteractionEvent`` remains the immutable evidence fact.  This
    row binds that fact to the exact active runtime/live snapshot consumed by
    the stop transaction, so an ambiguous HTTP retry can only read the same
    receipt and can never append a second pause or resurrect an old cursor.
    """
    __table_args__ = (
        UniqueConstraint(
            "session_id", "idempotency_key",
            name="uq_technical_pause_session_idempotency"),
        UniqueConstraint(
            "interaction_event_id",
            name="uq_technical_pause_interaction_event"),
        UniqueConstraint(
            "session_id", "expected_runtime_revision", "expected_live_wseq",
            name="uq_technical_pause_source_snapshot"),
        CheckConstraint(
            "length(trim(idempotency_key)) BETWEEN 16 AND 200",
            name="ck_technical_pause_idempotency_length"),
        CheckConstraint(
            "length(request_hash) = 64 AND lower(request_hash) = request_hash",
            name="ck_technical_pause_request_hash"),
        CheckConstraint(
            "expected_runtime_revision >= 0 AND runtime_revision >= 1",
            name="ck_technical_pause_runtime_revisions"),
        CheckConstraint(
            "expected_live_wseq >= 0 AND paused_cursor_wseq >= 0",
            name="ck_technical_pause_wseqs_nonnegative"),
        CheckConstraint(
            "live_seq >= 1",
            name="ck_technical_pause_live_seq_positive"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="session.session_id", index=True)
    interaction_event_id: int = Field(
        foreign_key="interactionevent.id", index=True)
    idempotency_key: str = Field(index=True)
    request_hash: str
    expected_runtime_revision: int
    expected_live_wseq: int
    runtime_revision: int
    paused_cursor_wseq: int
    live_seq: int
    cursor_json: str
    created_at: datetime = Field(default_factory=datetime.now)


@sa_event.listens_for(TechnicalPauseReceipt, "before_update")
@sa_event.listens_for(TechnicalPauseReceipt, "before_delete")
def _reject_technical_pause_receipt_mutation(*_args) -> None:
    raise RuntimeError("TechnicalPauseReceipt 是只追加幂等回执")


class Week1Profile(SQLModel, table=True):
    """★ 第1周画像——独立命名空间。判分链不得 join。只喂交互侧。"""
    patient_id: str = Field(primary_key=True, foreign_key="patient.patient_id")
    preferred_appellation: Optional[str] = None
    zodiac: Optional[str] = None
    interests: Optional[str] = None
    daily_activities: Optional[str] = None
    familiar_places: Optional[str] = None
    familiar_people: Optional[str] = None
    common_objects: Optional[str] = None
    willing_to_continue: Optional[bool] = None


class ScaleResult(SQLModel, table=True):
    """冻结前历史自由结果，仅供迁移隔离回看，永不作为正式研究量表合同。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    patient_id: str = Field(foreign_key="patient.patient_id", index=True)
    phase_type: PhaseType                         # 前测 / 后测 / 随访
    scale_name: str                               # 具体量表名（待 PI 定，字段先通用）
    subscale: Optional[str] = None
    score: Optional[float] = None
    assessed_at: Optional[datetime] = None
    assessor_id: Optional[str] = None


class AudioAssetRow(SQLModel, table=True):
    """音频资产（护栏1）。状态机逻辑见 audio_gate.py。"""
    raw_audio_id: str = Field(primary_key=True)
    session_id: Optional[str] = Field(default=None, foreign_key="session.session_id")
    # 无场次音频只允许显式模拟；有场次时由 Session.is_simulation 权威派生。
    is_simulation: bool = False
    data_classification: str = "research"
    # 录音落库时即绑定运行环节；即使尚未生成 TurnEvent/转写，重启后仍可恢复映射。
    turn_key: Optional[str] = None
    # 1=升级前设备曾持有 canonical turn_key；2=设备仅持有 opaque
    # 题位 ref。这是非秘密迁移标记，只用于精确恢复旧 outbox。
    patient_turn_ref_version: int = 2
    audio_format: str = "mp3"                     # 信度/争议子集用 wav（无损/高码率）
    status: AudioStatus = AudioStatus.recorded
    is_reliability_sample: bool = False
    withdrawn: bool = False
    withdrawal_status: Optional[str] = None       # 撤回旁路：withdrawal_requested→isolated（待 SOP）
    checksum: Optional[str] = None
    # 采集上传收据必须持久保存服务端观察到的长度和首次落盘时间。旧行迁移后
    # 保持 NULL，绝不根据可能已缺失/篡改的磁盘内容静默回填。
    byte_count: Optional[int] = Field(
        default=None, sa_column=Column(BigInteger, nullable=True))
    uploaded_at: Optional[datetime] = None
    contains_direct_identifier: bool = False      # 第1周自我介绍段默认置位
    export_batch_id: Optional[str] = None         # 导出批次（导出成功回写）
    delete_gate_passed: bool = False              # 闸门放行标记（审计）
    exported_at: Optional[datetime] = None


class AudioCaptureReceipt(SQLModel, table=True):
    """服务端确认完整音频原件后的只追加权威收据。

    ``server_seq`` 是跨录音全局单调序号；控制台用 ``after_seq`` 拉取时不会像
    LiveState 单槽那样丢掉轮询间连续完成的多条录音。raw_audio_id 一生只对应
    一张收据，相同事实可幂等重放，不同事实必须冲突。
    """
    __table_args__ = (
        UniqueConstraint("raw_audio_id", name="uq_audio_capture_receipt_raw_audio_id"),
        Index("ix_audio_capture_receipt_session_seq", "session_id", "server_seq"),
        CheckConstraint("byte_count > 0", name="ck_audio_capture_receipt_bytes_positive"),
        CheckConstraint(
            "duration_seconds >= 0 AND duration_seconds <= 21600",
            name="ck_audio_capture_receipt_duration"),
        CheckConstraint(
            "data_classification IN ('research','simulation')",
            name="ck_audio_capture_receipt_classification"),
    )

    server_seq: Optional[int] = Field(default=None, primary_key=True)
    raw_audio_id: str = Field(foreign_key="audioassetrow.raw_audio_id", index=True)
    session_id: str = Field(foreign_key="session.session_id", index=True)
    turn_key: str
    # Keep evidence chronology on the same UTC-naive basis as device
    # capabilities and server-authoritative commands.  Local-naive timestamps
    # make a valid receipt appear hours in the future on non-UTC deployments.
    received_at: datetime = Field(default_factory=_utc_now_naive)
    duration_seconds: float
    byte_count: int = Field(sa_column=Column(BigInteger, nullable=False))
    checksum: str
    data_classification: str
    is_simulation: bool = False
    contains_direct_identifier: bool = False


@sa_event.listens_for(AudioCaptureReceipt, "before_update")
@sa_event.listens_for(AudioCaptureReceipt, "before_delete")
def _reject_audio_capture_receipt_mutation(*_args) -> None:
    """ORM 层阻止误改/误删；治理修复必须走独立、审计化迁移。"""
    raise RuntimeError("AudioCaptureReceipt 是只追加证据，禁止更新或删除")


class LiveState(SQLModel, table=True):
    """仪器实时状态(单行,id=1)——跨设备同步的服务端真值。

    此前同步只靠浏览器 BroadcastChannel(仅同机双窗可用);此表让老人端平板与
    操作端电脑分居两台设备时仍能经内网同步:操作端写、两端轮询,seq 单调递增判新旧。
    只存指针(索引/级别/id),不存题目文本——内容仍以版本锁定的题库为唯一源。
    """
    id: int = Field(default=1, primary_key=True)
    seq: int = 0
    command_wseq: int = Field(
        default=0, sa_column=Column(BigInteger, nullable=False, default=0))
    session_json: Optional[str] = None       # 最新 session 握手(JSON)
    cursor_json: Optional[str] = None        # 最新游标
    rapport_json: Optional[str] = None       # 最新第1周步进
    audio_json: Optional[str] = None         # 最新老人端录音回报(audioSaved)
    patient_rec_json: Optional[str] = None   # 老人端麦克风真值上报(patientRec):自助开录操作端可见、可远程停
    # 老人端在线/画面确认只保存设备运行指针，不保存患者信息、题目文本或回答内容。
    patient_ack_session_id: Optional[str] = None
    patient_current_screen: Optional[str] = None
    patient_last_seen_at: Optional[datetime] = None
    patient_ack_seq: Optional[int] = Field(
        default=None, sa_column=Column(BigInteger, nullable=True))
    updated_at: Optional[datetime] = None


class SessionRuntimeState(SQLModel, table=True):
    """每场次可恢复的运行游标。

    与 LiveState 的“当前广播快照”分开保存，防止切换受试者/场次时覆盖上一场的续做位置。
    这里只存冻结计划中的位置和暂停状态；录音字节、回答、判分仍各走原有表和门禁。
    """
    session_id: str = Field(primary_key=True, foreign_key="session.session_id")
    status: str = "active"                   # active / paused / intervention_completed / completed / aborted / failed
    cursor_json: Optional[str] = None          # 正式训练安全恢复游标（录音恒 idle）
    rapport_json: Optional[str] = None         # 关系建立安全恢复游标（录音恒 idle）
    revision: int = 0
    paused_at: Optional[datetime] = None
    resumed_at: Optional[datetime] = None
    # 老人端自动干预结束与事后研究复核完成是两个不同时间点。
    intervention_completed_at: Optional[datetime] = None
    intervention_ended_by: Optional[str] = None
    completed_at: Optional[datetime] = None
    aborted_at: Optional[datetime] = None
    ended_by: Optional[str] = None
    end_reason: Optional[str] = None
    updated_at: Optional[datetime] = None


class SessionOutcomeSummary(SQLModel, table=True):
    """Immutable, text-free operational outcome snapshot for one session."""
    __table_args__ = (
        CheckConstraint(
            "data_classification IN ('research','simulation')",
            name="ck_session_outcome_summary_classification",
        ),
        CheckConstraint(
            "((is_simulation AND data_classification = 'simulation') OR "
            "((NOT is_simulation) AND data_classification = 'research'))",
            name="ck_session_outcome_summary_simulation_boundary",
        ),
        CheckConstraint(
            "expected_turns >= 0 AND matched_turns >= 0 AND "
            "completed_attempt_turns >= 0 AND audio_evidenced_turns >= 0 AND "
            "total_attempts >= 0 AND completed_attempts >= 0 AND "
            "needs_review_attempts >= 0 AND technical_failure_attempts >= 0 AND "
            "prompt_level_0_count >= 0 AND prompt_level_1_count >= 0 AND "
            "prompt_level_2_count >= 0 AND prompt_level_3_count >= 0 AND "
            "technical_pause_count >= 0 AND researcher_takeover_count >= 0",
            name="ck_session_outcome_summary_counts_nonnegative",
        ),
        CheckConstraint(
            "matched_turns <= expected_turns AND "
            "completed_attempt_turns <= matched_turns AND "
            "audio_evidenced_turns <= matched_turns",
            name="ck_session_outcome_summary_turn_count_order",
        ),
        CheckConstraint(
            "completed_attempts <= total_attempts AND "
            "needs_review_attempts <= total_attempts AND "
            "technical_failure_attempts <= total_attempts",
            name="ck_session_outcome_summary_attempt_count_order",
        ),
        CheckConstraint(
            "prompt_level_0_count + prompt_level_1_count + "
            "prompt_level_2_count + prompt_level_3_count = total_attempts",
            name="ck_session_outcome_summary_prompt_total",
        ),
        CheckConstraint(
            "length(schema_version) BETWEEN 1 AND 128 AND "
            "length(generator_version) BETWEEN 1 AND 128 AND "
            "length(item_bank_version_id) BETWEEN 1 AND 256",
            name="ck_session_outcome_summary_versions_nonempty",
        ),
        CheckConstraint(
            "length(source_digest) = 64",
            name="ck_session_outcome_summary_digest_length",
        ),
    )

    session_id: str = Field(primary_key=True, foreign_key="session.session_id")
    schema_version: str
    generator_version: str
    item_bank_version_id: str
    is_simulation: bool
    data_classification: str
    expected_turns: int
    matched_turns: int
    completed_attempt_turns: int
    audio_evidenced_turns: int
    total_attempts: int
    completed_attempts: int
    needs_review_attempts: int
    technical_failure_attempts: int
    prompt_level_0_count: int
    prompt_level_1_count: int
    prompt_level_2_count: int
    prompt_level_3_count: int
    technical_pause_count: int
    researcher_takeover_count: int
    source_digest: str
    generated_at: datetime


@sa_event.listens_for(SessionOutcomeSummary, "before_update")
@sa_event.listens_for(SessionOutcomeSummary, "before_delete")
def _reject_session_outcome_summary_mutation(*_args) -> None:
    raise RuntimeError("SessionOutcomeSummary 是不可变的场次快照")


class SessionCloseoutReport(SQLModel, table=True):
    """Researcher-authored post-session observation, lockable after review."""
    __table_args__ = (
        CheckConstraint(
            "status IN ('no_additional_observation','observation_recorded')",
            name="ck_session_closeout_report_status",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_session_closeout_report_revision_positive",
        ),
        CheckConstraint(
            "length(schema_version) BETWEEN 1 AND 128",
            name="ck_session_closeout_report_schema_version",
        ),
        CheckConstraint(
            "length(last_idempotency_key) BETWEEN 1 AND 200 AND "
            "length(last_request_hash) = 64",
            name="ck_session_closeout_report_idempotency_metadata",
        ),
        CheckConstraint(
            "length(trim(created_by)) > 0 AND length(trim(updated_by)) > 0",
            name="ck_session_closeout_report_actor_nonempty",
        ),
        CheckConstraint(
            "note IS NULL OR (length(note) <= 2000 AND length(trim(note)) > 0)",
            name="ck_session_closeout_report_note",
        ),
        CheckConstraint(
            "status != 'no_additional_observation' OR "
            "((NOT fatigue_observed) AND "
            "(NOT distress_or_discomfort_observed) AND "
            "(NOT participant_declined_to_continue) AND "
            "(NOT staff_assistance_occurred) AND "
            "(NOT environment_interruption_occurred) AND "
            "(NOT device_or_network_interruption_occurred) AND note IS NULL)",
            name="ck_session_closeout_report_no_observation_empty",
        ),
        CheckConstraint(
            "status != 'observation_recorded' OR "
            "(fatigue_observed OR distress_or_discomfort_observed OR "
            "participant_declined_to_continue OR staff_assistance_occurred OR "
            "environment_interruption_occurred OR "
            "device_or_network_interruption_occurred OR "
            "(note IS NOT NULL AND length(trim(note)) > 0))",
            name="ck_session_closeout_report_observation_present",
        ),
        CheckConstraint(
            "((locked_by IS NULL AND locked_at IS NULL) OR "
            "(locked_by IS NOT NULL AND locked_at IS NOT NULL))",
            name="ck_session_closeout_report_lock_tuple",
        ),
        CheckConstraint(
            "locked_by IS NULL OR length(trim(locked_by)) > 0",
            name="ck_session_closeout_report_locker_nonempty",
        ),
    )

    session_id: str = Field(primary_key=True, foreign_key="session.session_id")
    schema_version: str
    status: str
    fatigue_observed: bool = False
    distress_or_discomfort_observed: bool = False
    participant_declined_to_continue: bool = False
    staff_assistance_occurred: bool = False
    environment_interruption_occurred: bool = False
    device_or_network_interruption_occurred: bool = False
    note: Optional[str] = Field(default=None, max_length=2000)
    revision: int = 1
    last_idempotency_key: str
    last_request_hash: str
    created_by: str
    created_at: datetime = Field(default_factory=_utc_now_naive)
    updated_by: str
    updated_at: datetime = Field(default_factory=_utc_now_naive)
    locked_by: Optional[str] = None
    locked_at: Optional[datetime] = None


class AbnormalEvent(SQLModel, table=True):
    """异常/介入记录（phase 感知）。正式训练周的代说物品名/称呼→线索性介入、影响判分有效性。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="session.session_id", index=True)
    item_event_id: Optional[int] = Field(default=None, foreign_key="itemevent.id")
    phase_type: Optional[PhaseType] = None        # 记录发生时相位，供 phase 感知审计
    abnormal_type: Optional[str] = None           # AbnormalType 取值（如 长时间沉默/线索性介入）
    intervention_type: Optional[str] = None       # InterventionType 取值（如 代说物品名）
    affects_scoring_validity: bool = False        # 是否影响该环节/题判分有效性
    note: Optional[str] = None                    # caregiver_note / session_note
    created_at: Optional[datetime] = None


class AuditLog(SQLModel, table=True):
    """只追加的研究审计账本(防篡改 tamper-evident)。记录"谁在何时对研究真值做了什么",供论文溯源与核查。

    红线:summary 只记元数据(研究编号 + 动作 + 分值/类型),【绝不写入患者回答文本或姓名】——
    审计不能变成第二个泄露患者作答的地方。

    完整性保证的边界(诚实标注,勿夸大):
      * 哈希链:entry_hash=sha256(prev_hash|各字段)。改动/中间删除任一历史行 → 从该行起断链,verify 检出。
      * 高水位锚点(AuditAnchor):记录条数与链尾 hash,verify 比对 → 尾部截断/整表清空可检出(这是裸哈希链单靠自身检不出的)。
      * prev_hash 唯一约束:并发追加撞链(多 worker)在 DB 层被挡,不会静默分叉。
      * 【已知残余】裸 sha256 无密钥、锚点与账本同库:掌握 DB 写权且懂方案者可同时重算链+改锚点伪造。
        强保证需外部密钥 HMAC/逐条签名/把链尾定期锚定到外部不可变存储(WORM/公共时间戳)——留待后续。
        威胁模型:本工具服务可信研究者小组,审计用于诚实溯源、检出意外损坏与阻吓随手篡改。
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime
    actor: str                                    # 登录账号 display_id / "PIN/本地" / "system"
    action: str                                   # score_lock/response_confirm/scale_record/abnormal/audio_delete/data_export/login…
    patient_id: Optional[str] = Field(default=None, index=True)
    session_id: Optional[str] = Field(default=None, index=True)
    turn_id: Optional[int] = None
    summary: str                                  # 短元数据(无回答文本/姓名)
    prev_hash: str = Field(unique=True)           # 唯一 → 并发追加撞链在 DB 层被挡(防多 worker 静默分叉)
    entry_hash: str


class AuditAnchor(SQLModel, table=True):
    """审计高水位锚点(单行,id=1)。每次追加同事务更新条数与链尾 hash;
    verify 比对之 → 检出裸哈希链单靠自身检不出的"尾部截断/整表清空"。"""
    id: int = Field(default=1, primary_key=True)
    count: int = 0
    tip_hash: str = "0" * 64
    updated_at: Optional[datetime] = None


class ResearchUser(SQLModel, table=True):
    """研究者账号（公网部署的真实身份层，替代裸共享 PIN）。

    display_id 才是落到 TurnEvent.reviewer_id / ScaleResult.assessor_id 的审计标识，
    与登录名 username 解耦：登录名可改，历史锁分归属仍稳定指向 display_id。
    绝不存明文密码——password_hash 是 pbkdf2_sha256$iters$salt$hash（见 app/auth.py）。
    """
    __table_args__ = (
        UniqueConstraint(
            "display_id", name="uq_research_user_display_id"),
    )

    username: str = Field(primary_key=True)
    display_id: str                               # 审计身份（谁锁的分/谁评的量表）
    password_hash: str
    role: str = "researcher"                       # researcher / data_steward / admin
    disabled: bool = False                          # 停用即时生效（会话校验时否决）
    created_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None


class AuthSession(SQLModel, table=True):
    """服务端会话（可撤销：登出/停用立即失效，优于无状态 JWT）。

    表里只存 token 的 sha256（token_hash），明文 token 只在浏览器 httponly cookie 里——
    数据库即便泄露也无法据此伪造有效会话。
    """
    token_hash: str = Field(primary_key=True)
    username: str = Field(foreign_key="researchuser.username", index=True)
    created_at: datetime
    expires_at: datetime
    last_seen_at: Optional[datetime] = None


class PatientDeviceCapability(SQLModel, table=True):
    """Short-lived patient-device bearer bound to exactly one training session.

    Only SHA-256 digests are persisted.  The plaintext bearer is returned once by
    ``POST /device/pair`` and the random, non-PII device id is likewise stored only
    as a digest.
    """
    __table_args__ = (
        UniqueConstraint(
            "active_session_key",
            name="uq_patient_device_capability_active_session",
        ),
        CheckConstraint(
            "last_autopilot_event_seq >= 0",
            name="ck_patient_device_capability_autopilot_seq_nonnegative",
        ),
    )

    token_hash: str = Field(primary_key=True)
    session_id: str = Field(foreign_key="session.session_id", index=True)
    device_id_hash: str
    # Exactly one non-revoked token may hold a session's active slot.  NULL for
    # revoked rows so history remains available without blocking a later pair.
    active_session_key: Optional[str] = None
    created_at: datetime
    expires_at: datetime
    last_seen_at: Optional[datetime] = None
    # Durable device ACK high-water mark.  Exact idempotent replay is resolved
    # before this fence; only a genuinely new, strictly larger event advances it.
    last_autopilot_event_seq: int = 0
    # Session switch demotes an active token to ACK-recovery-only without making
    # already-persisted audio unrecoverable on the device.
    recovery_only_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class RuntimeCommand(SQLModel, table=True):
    """Server-authoritative TTS/record command.

    A command is an execution instruction, not a browser-local timer.  The
    control/runner generations fence stale controllers and workers; ``revision``
    fences stale writes within the same generation.  A record command must point
    to both its preceding TTS command and the exact ``tts_ended`` ACK idempotency
    key.  The service layer still verifies those cross-row facts before issuance.
    """
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_runtime_command_idempotency_key"),
        UniqueConstraint("id", "session_id", name="uq_runtime_command_id_session"),
        UniqueConstraint("session_id", "command_seq",
                         name="uq_runtime_command_session_seq"),
        UniqueConstraint("predecessor_command_id",
                         name="uq_runtime_command_predecessor"),
        UniqueConstraint("expected_raw_audio_id",
                         name="uq_runtime_command_expected_raw_audio"),
        UniqueConstraint("replay_source_command_id",
                         name="uq_runtime_command_replay_source"),
        ForeignKeyConstraint(
            ["predecessor_command_id", "session_id"],
            ["runtimecommand.id", "runtimecommand.session_id"],
            name="fk_runtime_command_predecessor_same_session"),
        ForeignKeyConstraint(
            ["replay_source_command_id", "session_id"],
            ["runtimecommand.id", "runtimecommand.session_id"],
            name="fk_runtime_command_replay_source_same_session"),
        Index("ix_runtime_command_session_state_lease",
              "session_id", "state", "lease_expires_at"),
        CheckConstraint("command_seq >= 1", name="ck_runtime_command_seq_positive"),
        CheckConstraint("turn_seq >= 1", name="ck_runtime_command_turn_positive"),
        CheckConstraint("attempt_seq >= 1", name="ck_runtime_command_attempt_positive"),
        CheckConstraint("prompt_level >= 0 AND prompt_level <= 3",
                        name="ck_runtime_command_prompt_level"),
        CheckConstraint("control_generation >= 1",
                        name="ck_runtime_command_control_generation"),
        CheckConstraint("runner_generation >= 1",
                        name="ck_runtime_command_runner_generation"),
        CheckConstraint("revision >= 0", name="ck_runtime_command_revision"),
        CheckConstraint("scope_key IN ('p0a_sim_first_single_v1')",
                        name="ck_runtime_command_scope"),
        CheckConstraint("kind IN ('tts','record')", name="ck_runtime_command_kind"),
        CheckConstraint(
            "state IN ('pending','started','succeeded','failed','cancelled')",
            name="ck_runtime_command_state"),
        CheckConstraint(
            "((lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))",
            name="ck_runtime_command_lease_complete"),
        CheckConstraint(
            "(state NOT IN ('succeeded','failed','cancelled') OR "
            "(lease_owner IS NULL AND lease_expires_at IS NULL))",
            name="ck_runtime_command_terminal_lease_released"),
        CheckConstraint(
            "((kind = 'tts' AND predecessor_command_id IS NULL AND "
            "trigger_ack_idempotency_key IS NULL AND expected_raw_audio_id IS NULL) OR "
            "(kind = 'record' AND predecessor_command_id IS NOT NULL AND "
            "trigger_ack_idempotency_key IS NOT NULL AND "
            "expected_raw_audio_id IS NOT NULL))",
            name="ck_runtime_command_record_prerequisite"),
        CheckConstraint(
            "(state != 'started' OR started_at IS NOT NULL)",
            name="ck_runtime_command_started_timestamp"),
        CheckConstraint(
            "(state != 'succeeded' OR succeeded_at IS NOT NULL)",
            name="ck_runtime_command_succeeded_timestamp"),
        CheckConstraint(
            "(state != 'failed' OR failed_at IS NOT NULL)",
            name="ck_runtime_command_failed_timestamp"),
        CheckConstraint(
            "(state != 'cancelled' OR cancelled_at IS NOT NULL)",
            name="ck_runtime_command_cancelled_timestamp"),
        CheckConstraint(
            "((item_bank_version_id IS NULL AND "
            "item_bank_definition_digest IS NULL AND "
            "autopilot_protocol_version_id IS NULL AND "
            "autopilot_protocol_definition_digest IS NULL AND "
            "response_role IS NULL) OR "
            "(length(trim(item_bank_version_id)) > 0 AND "
            "length(item_bank_definition_digest) = 64 AND "
            "lower(item_bank_definition_digest) = item_bank_definition_digest AND "
            "length(trim(autopilot_protocol_version_id)) > 0 AND "
            "length(autopilot_protocol_definition_digest) = 64 AND "
            "lower(autopilot_protocol_definition_digest) = "
            "autopilot_protocol_definition_digest AND "
            "length(trim(response_role)) > 0))",
            name="ck_runtime_command_definition_binding_complete"),
        CheckConstraint(
            REPEAT_PROTOCOL_BINDING_CHECK,
            name="ck_runtime_command_repeat_binding_complete"),
        CheckConstraint(
            "((replay_source_command_id IS NULL AND replay_ordinal IS NULL AND "
            "replay_source_payload_sha256 IS NULL) OR "
            "(replay_source_command_id IS NOT NULL AND "
            "replay_ordinal IS NOT NULL AND replay_ordinal = 1 AND "
            "replay_source_payload_sha256 IS NOT NULL AND "
            + _hex64_sql("replay_source_payload_sha256") + "))",
            name="ck_runtime_command_replay_provenance_complete"),
        CheckConstraint(
            "(kind != 'record' OR (replay_source_command_id IS NULL AND "
            "replay_ordinal IS NULL AND replay_source_payload_sha256 IS NULL))",
            name="ck_runtime_command_record_never_replays"),
        CheckConstraint(
            REPLAY_REQUIRES_REPEAT_BINDING_CHECK,
            name="ck_runtime_command_replay_requires_repeat_binding"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    idempotency_key: str
    session_id: str = Field(foreign_key="session.session_id", index=True)
    command_seq: int
    item_id: str = Field(index=True)
    turn_seq: int
    turn_key: str
    attempt_seq: int
    prompt_level: int
    # The complete interpretation contract is fixed when a command is issued.
    # Nullable is reserved for historical commands, which semantic execution
    # rejects instead of silently binding to today's files.
    item_bank_version_id: Optional[str] = None
    item_bank_definition_digest: Optional[str] = None
    autopilot_protocol_version_id: Optional[str] = None
    autopilot_protocol_definition_digest: Optional[str] = None
    response_role: Optional[str] = None
    # Copied unchanged from the session (and, for a record command, from its
    # predecessor TTS) so a recovery worker resolves the exact historical
    # repeat definition instead of whatever the deployment ships today.
    repeat_protocol_version_id: Optional[str] = None
    repeat_protocol_definition_digest: Optional[str] = None
    # Server-side provenance for a replayed TTS.  The patient device still sees
    # an ordinary, strictly validated question/cue command.
    replay_source_command_id: Optional[int] = Field(default=None, index=True)
    replay_ordinal: Optional[int] = None
    # SHA-256 of the source RuntimeCommand.payload_json UTF-8 bytes only.
    # It is not a digest of any synthesized TTS audio, and binds no WAV.
    replay_source_payload_sha256: Optional[str] = None
    # Legacy compatibility identifier for the simulation-only sequential scope;
    # it still never implies that the whole research intervention completed.
    scope_key: str = "p0a_sim_first_single_v1"
    control_generation: int
    runner_generation: int
    # Persist the exact device delivery identity.  A recovery-only capability may
    # finish only commands that were issued to that same device before demotion.
    issued_capability_token_hash: str = Field(
        foreign_key="patientdevicecapability.token_hash", index=True)
    issued_device_id_hash: str
    issued_at: datetime
    kind: str                               # tts / record
    state: str = "pending"                 # pending / started / succeeded / failed / cancelled
    # Every recording starts only after a persisted ended event for its preceding
    # TTS command.  These two fields make that provenance explicit and replayable.
    predecessor_command_id: Optional[int] = Field(default=None, index=True)
    trigger_ack_idempotency_key: Optional[str] = None
    expected_raw_audio_id: Optional[str] = Field(
        default=None, foreign_key="audioassetrow.raw_audio_id", index=True)
    payload_json: str = "{}"                # controlled command metadata; no patient response
    result_json: str = "{}"                 # controlled engine/device metadata
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    revision: int = 0
    created_at: datetime = Field(default_factory=_utc_now_naive)
    started_at: Optional[datetime] = None
    succeeded_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=_utc_now_naive)


@sa_event.listens_for(RuntimeCommand, "before_update")
@sa_event.listens_for(RuntimeCommand, "before_delete")
def _reject_runtime_command_orm_mutation(*_args) -> None:
    """Issued command identity is immutable; lifecycle uses fenced Core UPDATE."""
    raise RuntimeError("RuntimeCommand 已发行命令禁止 ORM 改写或删除")


class RuntimeCommandAck(SQLModel, table=True):
    """Append-only patient-device receipt for a command lifecycle event.

    Only digests of the capability and random device id are stored.  A
    ``record_stopped`` ACK carries an exact pointer and digest tuple for the
    separately append-only :class:`AudioCaptureReceipt`; an Attempt must not be
    created until the service-layer verifier proves those facts are identical.
    """
    __table_args__ = (
        ForeignKeyConstraint(
            ["command_id", "session_id"],
            ["runtimecommand.id", "runtimecommand.session_id"],
            name="fk_runtime_command_ack_command_same_session"),
        UniqueConstraint("command_id", "idempotency_key",
                         name="uq_runtime_command_ack_command_idempotency"),
        UniqueConstraint("command_id", "ack_type",
                         name="uq_runtime_command_ack_command_type"),
        UniqueConstraint("session_id", "device_id_hash", "device_event_seq",
                         name="uq_runtime_command_ack_device_seq"),
        Index("ix_runtime_command_ack_command_type", "command_id", "ack_type"),
        CheckConstraint("device_event_seq >= 1",
                        name="ck_runtime_command_ack_device_seq_positive"),
        CheckConstraint("command_revision >= 0",
                        name="ck_runtime_command_ack_command_revision"),
        CheckConstraint("control_generation >= 1",
                        name="ck_runtime_command_ack_control_generation"),
        CheckConstraint("runner_generation >= 1",
                        name="ck_runtime_command_ack_runner_generation"),
        CheckConstraint(
            "ack_type IN ('tts_started','tts_ended','tts_failed',"
            "'record_started','record_stopped','record_failed')",
            name="ck_runtime_command_ack_type"),
        CheckConstraint(
            "((ack_type = 'record_stopped' AND receipt_server_seq IS NOT NULL AND "
            "raw_audio_id IS NOT NULL AND checksum IS NOT NULL AND "
            "byte_count IS NOT NULL AND duration_seconds IS NOT NULL) OR "
            "(ack_type != 'record_stopped' AND receipt_server_seq IS NULL AND "
            "raw_audio_id IS NULL AND checksum IS NULL AND byte_count IS NULL AND "
            "duration_seconds IS NULL))",
            name="ck_runtime_command_ack_capture_tuple"),
        CheckConstraint(
            "byte_count IS NULL OR byte_count > 0",
            name="ck_runtime_command_ack_bytes_positive"),
        CheckConstraint(
            "duration_seconds IS NULL OR "
            "(duration_seconds >= 0 AND duration_seconds <= 21600)",
            name="ck_runtime_command_ack_duration"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    command_id: int = Field(index=True)
    idempotency_key: str
    session_id: str = Field(foreign_key="session.session_id", index=True)
    ack_type: str = Field(index=True)
    command_revision: int
    control_generation: int
    runner_generation: int
    device_event_seq: int
    # Digests only; never plaintext bearer/device identifiers.
    device_id_hash: str
    capability_token_hash: str = Field(
        foreign_key="patientdevicecapability.token_hash", index=True)
    payload_json: str = "{}"
    receipt_server_seq: Optional[int] = Field(
        default=None, foreign_key="audiocapturereceipt.server_seq", index=True)
    raw_audio_id: Optional[str] = Field(
        default=None, foreign_key="audioassetrow.raw_audio_id", index=True)
    checksum: Optional[str] = None
    byte_count: Optional[int] = Field(
        default=None, sa_column=Column(BigInteger, nullable=True))
    duration_seconds: Optional[float] = None
    received_at: datetime = Field(default_factory=_utc_now_naive)


@sa_event.listens_for(RuntimeCommandAck, "before_update")
@sa_event.listens_for(RuntimeCommandAck, "before_delete")
def _reject_runtime_command_ack_mutation(*_args) -> None:
    raise RuntimeError("RuntimeCommandAck 是只追加回执，禁止更新或删除")


class TtsServeEvidence(SQLModel, table=True):
    """服务端 TTS 实际返回的只追加证据：每次向设备返回(或降级)一句话术记一行。

    这是"某次交互实际使用了哪个 TTS 引擎"的唯一权威来源——engine_version 与
    cache_hit 由服务端合成路径产生，前端上报不被接受。文本只存 SHA-256
    （话术本就来自白名单闭集，账本仍不复制内容）。授权复核失败被丢弃的音频
    不记行：行存在 ⇔ 字节确实返回给了请求方。
    """
    __table_args__ = (
        CheckConstraint("source IN ('autopilot_command','live_speak')",
                        name="ck_tts_serve_source"),
        CheckConstraint("result IN ('served','degraded')",
                        name="ck_tts_serve_result"),
        CheckConstraint(
            "length(text_sha256) = 64 AND lower(text_sha256) = text_sha256",
            name="ck_tts_serve_text_sha256"),
        CheckConstraint(
            "(result = 'served' AND byte_count > 0) OR "
            "(result = 'degraded' AND byte_count IS NULL)",
            name="ck_tts_serve_bytes_match_result"),
        CheckConstraint(
            "(source = 'autopilot_command' AND command_id IS NOT NULL) OR "
            "(source = 'live_speak' AND command_id IS NULL)",
            name="ck_tts_serve_command_binding"),
        Index("ix_tts_serve_session_created", "session_id", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: Optional[str] = Field(
        default=None, foreign_key="session.session_id", index=True)
    command_id: Optional[int] = Field(
        default=None, foreign_key="runtimecommand.id", index=True)
    source: str
    engine_version: str
    cache_hit: bool = False
    result: str
    byte_count: Optional[int] = None
    text_sha256: str
    is_simulation: bool = False
    created_at: datetime = Field(default_factory=datetime.now)


@sa_event.listens_for(TtsServeEvidence, "before_update")
@sa_event.listens_for(TtsServeEvidence, "before_delete")
def _reject_tts_serve_evidence_mutation(*_args) -> None:
    raise RuntimeError("TtsServeEvidence 是只追加使用证据，禁止更新或删除")


class SessionAutopilotState(SQLModel, table=True):
    """One recoverable, CAS-protected autopilot control row per session."""
    __table_args__ = (
        ForeignKeyConstraint(
            ["current_command_id", "session_id"],
            ["runtimecommand.id", "runtimecommand.session_id"],
            name="fk_session_autopilot_current_command_same_session"),
        Index("ix_session_autopilot_state_lease", "lease_expires_at"),
        CheckConstraint("control_generation >= 0",
                        name="ck_session_autopilot_control_generation"),
        CheckConstraint("runner_generation >= 0",
                        name="ck_session_autopilot_runner_generation"),
        CheckConstraint("revision >= 0", name="ck_session_autopilot_revision"),
        CheckConstraint("next_command_seq >= 1",
                        name="ck_session_autopilot_next_command_seq"),
        CheckConstraint("scope_key IN ('disabled','p0a_sim_first_single_v1')",
                        name="ck_session_autopilot_scope"),
        CheckConstraint("mode IN ('disabled','autonomous','manual')",
                        name="ck_session_autopilot_mode"),
        CheckConstraint(
            "status IN ('idle','running','waiting_tts','waiting_recording',"
            "'processing_attempt','manual_draining','paused','scope_completed',"
            "'failed')",
            name="ck_session_autopilot_status"),
        CheckConstraint(
            "((lease_owner IS NULL AND lease_expires_at IS NULL AND "
            "lease_acquired_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND "
            "lease_acquired_at IS NOT NULL))",
            name="ck_session_autopilot_lease_complete"),
        CheckConstraint(
            "((scope_key = 'disabled' AND mode = 'disabled' AND status = 'idle' "
            "AND control_generation = 0 AND runner_generation = 0 "
            "AND current_command_id IS NULL) OR "
            "(scope_key = 'p0a_sim_first_single_v1' "
            "AND mode IN ('autonomous','manual')))",
            name="ck_session_autopilot_scope_mode"),
        CheckConstraint(
            "((status IN ('waiting_tts','waiting_recording','processing_attempt',"
            "'manual_draining') AND current_command_id IS NOT NULL) OR "
            "status NOT IN ('waiting_tts','waiting_recording','processing_attempt',"
            "'manual_draining'))",
            name="ck_session_autopilot_waiting_has_command"),
        CheckConstraint(
            "status != 'scope_completed' OR current_command_id IS NULL",
            name="ck_session_autopilot_completed_clears_command"),
    )

    session_id: str = Field(primary_key=True, foreign_key="session.session_id")
    scope_key: str = "disabled"
    mode: str = "disabled"                  # disabled / autonomous / manual
    status: str = "idle"                    # includes takeover-safe manual_draining
    control_generation: int = 0
    runner_generation: int = 0
    revision: int = 0
    next_command_seq: int = 1
    current_command_id: Optional[int] = Field(default=None, index=True)
    lease_owner: Optional[str] = None
    lease_acquired_at: Optional[datetime] = None
    lease_expires_at: Optional[datetime] = None
    last_error_code: Optional[str] = None
    created_at: datetime = Field(default_factory=_utc_now_naive)
    updated_at: datetime = Field(default_factory=_utc_now_naive)


class ProviderReadinessProbe(SQLModel, table=True):
    """Append-only metadata ledger for a patient-free synthetic provider probe.

    There is deliberately no column for an API key, probe prompt, transcript,
    answer text, generated audio, patient, session, turn, or audio asset.
    """
    __table_args__ = (
        Index("ix_provider_readiness_checked", "checked_at"),
        CheckConstraint(
            "length(config_fingerprint) = 64",
            name="ck_provider_readiness_fingerprint_length"),
        CheckConstraint(
            "expires_at > checked_at",
            name="ck_provider_readiness_expiry_after_check"),
        CheckConstraint(
            "length(trim(actor_display_id)) > 0",
            name="ck_provider_readiness_actor_nonempty"),
        CheckConstraint(
            "length(trim(runtime_contract)) > 0",
            name="ck_provider_readiness_contract_nonempty"),
        CheckConstraint(
            "NOT required_capabilities_ready OR "
            "((NOT tts_required OR tts_success) AND "
            "(NOT asr_required OR asr_success) AND "
            "(NOT llm_required OR llm_success))",
            name="ck_provider_readiness_required_successes"),
    )

    probe_id: str = Field(primary_key=True)
    schema_version: str
    runtime_contract: str
    config_fingerprint: str
    tts_engine_version: str
    asr_engine_version: str
    llm_engine_version: str
    tts_required: bool = True
    tts_success: bool = False
    tts_failure_code: Optional[str] = None
    asr_required: bool = True
    asr_success: bool = False
    asr_failure_code: Optional[str] = None
    llm_required: bool = False
    llm_configured: bool = False
    llm_success: bool = False
    llm_failure_code: Optional[str] = None
    required_capabilities_ready: bool = False
    all_configured_capabilities_ready: bool = False
    probe_failure_code: Optional[str] = None
    checked_at: datetime
    expires_at: datetime
    actor_display_id: str


@sa_event.listens_for(ProviderReadinessProbe, "before_update")
@sa_event.listens_for(ProviderReadinessProbe, "before_delete")
def _reject_provider_readiness_probe_mutation(*_args) -> None:
    raise RuntimeError("ProviderReadinessProbe 是只追加检查账本，禁止更新或删除")


class ExportBatch(SQLModel, table=True):
    """Recoverable, identifier-free authority for one export intent.

    The request fingerprint is keyed and the idempotency token is stored only as
    a SHA-256 digest.  Deliberately absent: patient/session/audio identifiers,
    response text, closeout notes, and credentials.
    """
    __table_args__ = (
        UniqueConstraint("idempotency_key_hash",
                         name="uq_export_batch_idempotency_key_hash"),
        UniqueConstraint("export_scope_hash",
                         name="uq_export_batch_export_scope_hash"),
        Index("ix_export_batch_status_created", "status", "created_at"),
        CheckConstraint(
            "status IN ('staging','artifacts_ready','published')",
            name="ck_export_batch_status"),
        CheckConstraint(
            "data_classification IN ('research','simulation')",
            name="ck_export_batch_classification"),
        CheckConstraint(
            "actor_role IN ('data_steward','admin')",
            name="ck_export_batch_actor_role"),
        CheckConstraint(
            "length(idempotency_key_hash) = 64 AND "
            "length(request_fingerprint) = 64 AND "
            "length(export_scope_hash) = 64",
            name="ck_export_batch_hash_lengths"),
        CheckConstraint(
            "manifest_sha256 IS NULL OR length(manifest_sha256) = 64",
            name="ck_export_batch_manifest_hash_length"),
        CheckConstraint(
            "((staging_owner_hash IS NULL AND "
            "staging_lease_expires_at IS NULL) OR "
            "(status = 'staging' AND staging_owner_hash IS NOT NULL AND "
            "length(staging_owner_hash) = 64 AND "
            "staging_lease_expires_at IS NOT NULL))",
            name="ck_export_batch_staging_lease"),
        CheckConstraint(
            "length(trim(actor_display_id)) > 0",
            name="ck_export_batch_actor_nonempty"),
        CheckConstraint(
            "((status = 'staging' AND artifacts_ready_at IS NULL AND "
            "published_at IS NULL) OR "
            "(status = 'artifacts_ready' AND artifacts_ready_at IS NOT NULL AND "
            "published_at IS NULL) OR "
            "(status = 'published' AND artifacts_ready_at IS NOT NULL AND "
            "published_at IS NOT NULL))",
            name="ck_export_batch_status_times"),
    )

    batch_id: str = Field(primary_key=True)
    schema_version: str = "export-batch.v1"
    idempotency_key_hash: str
    request_fingerprint: str
    export_scope_hash: str
    status: str = "staging"
    data_classification: str
    deidentified: bool = True
    actor_display_id: str
    actor_role: str
    result_metadata_json: str = "{}"
    manifest_sha256: Optional[str] = None
    publication_manifest_json: Optional[str] = None
    staging_owner_hash: Optional[str] = None
    staging_lease_expires_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utc_now_naive)
    artifacts_ready_at: Optional[datetime] = None
    published_at: Optional[datetime] = None


class ExportArtifact(SQLModel, table=True):
    """Immutable file receipt using only a safe path relative to its realm."""
    __table_args__ = (
        UniqueConstraint("batch_id", "realm", "relative_path",
                         name="uq_export_artifact_batch_realm_path"),
        Index("ix_export_artifact_batch_id", "batch_id"),
        Index("ix_export_artifact_batch_kind", "batch_id", "kind"),
        CheckConstraint(
            "kind IN ('csv','controlled_audio','manifest','staging_receipt')",
            name="ck_export_artifact_kind"),
        CheckConstraint(
            "realm IN ('research_analysis','research_controlled_audio',"
            "'simulation_analysis','simulation_controlled_audio')",
            name="ck_export_artifact_realm"),
        CheckConstraint("length(sha256) = 64",
                        name="ck_export_artifact_sha256_length"),
        CheckConstraint("byte_count >= 0",
                        name="ck_export_artifact_byte_count"),
        CheckConstraint(
            "length(relative_path) BETWEEN 1 AND 500 AND "
            "substr(relative_path, 1, 1) != '/'",
            name="ck_export_artifact_relative_path"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: str = Field(foreign_key="exportbatch.batch_id")
    realm: str
    kind: str
    relative_path: str
    sha256: str
    byte_count: int = Field(sa_column=Column(BigInteger, nullable=False))
    created_at: datetime = Field(default_factory=_utc_now_naive)


@sa_event.listens_for(ExportBatch, "before_delete")
def _reject_export_batch_delete(*_args) -> None:
    raise RuntimeError("ExportBatch 是持久导出事实，禁止删除")


@sa_event.listens_for(ExportBatch, "before_update")
def _guard_export_batch_transition(_mapper, _connection, target: ExportBatch) -> None:
    state = sa_inspect(target)
    changed = {
        attribute.key for attribute in state.attrs
        if attribute.history.has_changes()
    }
    status_history = state.attrs.status.history
    old_status = (
        status_history.deleted[0] if status_history.deleted
        else target.status
    )
    if old_status == "staging":
        allowed = {
            "result_metadata_json", "manifest_sha256",
            "publication_manifest_json", "status", "artifacts_ready_at",
            "staging_owner_hash", "staging_lease_expires_at",
        }
        if not changed <= allowed or target.status not in {
                "staging", "artifacts_ready"}:
            raise RuntimeError("ExportBatch staging 只允许发布意图与 artifacts_ready 转换")
        return
    if old_status == "artifacts_ready":
        if changed <= {"status", "published_at"} and target.status == "published":
            return
        raise RuntimeError("ExportBatch artifacts_ready 只允许原子转换为 published")
    raise RuntimeError("published ExportBatch 不可修改")


@sa_event.listens_for(ExportArtifact, "before_update")
@sa_event.listens_for(ExportArtifact, "before_delete")
def _reject_export_artifact_mutation(*_args) -> None:
    raise RuntimeError("ExportArtifact 是只追加文件收据，禁止更新或删除")


class AutopilotControlEvent(SQLModel, table=True):
    """Append-only audit trail for automation control and human takeover."""
    __table_args__ = (
        ForeignKeyConstraint(
            ["command_id", "session_id"],
            ["runtimecommand.id", "runtimecommand.session_id"],
            name="fk_autopilot_control_command_same_session"),
        UniqueConstraint("idempotency_key",
                         name="uq_autopilot_control_event_idempotency"),
        UniqueConstraint("session_id", "event_seq",
                         name="uq_autopilot_control_event_session_seq"),
        Index("ix_autopilot_control_event_session_created",
              "session_id", "created_at"),
        CheckConstraint("event_seq >= 1",
                        name="ck_autopilot_control_event_seq_positive"),
        CheckConstraint("control_generation >= 0",
                        name="ck_autopilot_control_event_control_generation"),
        CheckConstraint("runner_generation >= 0",
                        name="ck_autopilot_control_event_runner_generation"),
        CheckConstraint("scope_key IN ('disabled','p0a_sim_first_single_v1')",
                        name="ck_autopilot_control_event_scope"),
        CheckConstraint(
            "event_type IN ('start','pause','resume','takeover','drain_complete',"
            "'generation_bump','failure','scope_complete')",
            name="ck_autopilot_control_event_type"),
        CheckConstraint(
            "actor_type IN ('system','researcher','device')",
            name="ck_autopilot_control_event_actor_type"),
        CheckConstraint(
            "from_mode IS NULL OR from_mode IN ('disabled','autonomous','manual')",
            name="ck_autopilot_control_event_from_mode"),
        CheckConstraint(
            "to_mode IS NULL OR to_mode IN ('disabled','autonomous','manual')",
            name="ck_autopilot_control_event_to_mode"),
        CheckConstraint(
            "from_status IS NULL OR from_status IN "
            "('idle','running','waiting_tts','waiting_recording',"
            "'processing_attempt','manual_draining','paused','scope_completed',"
            "'failed')",
            name="ck_autopilot_control_event_from_status"),
        CheckConstraint(
            "to_status IS NULL OR to_status IN "
            "('idle','running','waiting_tts','waiting_recording',"
            "'processing_attempt','manual_draining','paused','scope_completed',"
            "'failed')",
            name="ck_autopilot_control_event_to_status"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    idempotency_key: str
    session_id: str = Field(foreign_key="session.session_id", index=True)
    event_seq: int
    event_type: str = Field(index=True)
    scope_key: str
    control_generation: int
    runner_generation: int
    command_id: Optional[int] = Field(default=None, index=True)
    actor_type: str
    actor_id: Optional[str] = None
    reason_code: Optional[str] = None
    from_mode: Optional[str] = None
    to_mode: Optional[str] = None
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    # Only controlled operational metadata belongs here; never ASR/response text.
    payload_json: str = "{}"
    created_at: datetime = Field(default_factory=_utc_now_naive)


@sa_event.listens_for(AutopilotControlEvent, "before_update")
@sa_event.listens_for(AutopilotControlEvent, "before_delete")
def _reject_autopilot_control_event_mutation(*_args) -> None:
    raise RuntimeError("AutopilotControlEvent 是只追加审计，禁止更新或删除")


# ---------------------------------------------------------------------------
# Independent formal assessment domain (pretest / posttest / followup).
# These rows deliberately do not reuse Training Session, ItemEvent, TurnEvent,
# SessionOutcomeSummary or SessionCloseoutReport.


class AssessmentEvent(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint(
            "active_protocol_slot_key",
            name="uq_assessment_event_active_protocol_slot"),
        Index("ix_assessment_event_due_queue", "status", "scheduled_date"),
        Index("ix_assessment_event_patient_timepoint", "patient_id", "timepoint"),
        CheckConstraint(
            "timepoint IN ('pretest','posttest','followup')",
            name="ck_assessment_event_timepoint"),
        CheckConstraint(
            "status IN "
            "('due','in_progress','awaiting_closeout','closed','cancelled')",
            name="ck_assessment_event_status"),
        CheckConstraint("revision >= 1", name="ck_assessment_event_revision"),
        CheckConstraint(
            "data_classification IN ('research','simulation')",
            name="ck_assessment_event_classification"),
        CheckConstraint(
            "((is_simulation AND data_classification = 'simulation' AND "
            "NOT formal_outcome_eligible) OR "
            "((NOT is_simulation) AND data_classification = 'research'))",
            name="ck_assessment_event_simulation_boundary"),
        CheckConstraint(
            "length(definition_bundle_digest) = 71 AND "
            "substr(definition_bundle_digest, 1, 7) = 'sha256:'",
            name="ck_assessment_event_bundle_digest"),
        CheckConstraint(
            "length(trim(assigned_assessor_id)) > 0",
            name="ck_assessment_event_assessor"),
        CheckConstraint(
            "((status = 'due' AND started_at IS NULL AND closed_at IS NULL AND "
            "cancelled_at IS NULL) OR "
            "(status IN ('in_progress','awaiting_closeout') AND "
            "started_at IS NOT NULL AND closed_at IS NULL AND cancelled_at IS NULL) OR "
            "(status = 'closed' AND started_at IS NOT NULL AND closed_at IS NOT NULL "
            "AND cancelled_at IS NULL) OR "
            "(status = 'cancelled' AND started_at IS NULL AND closed_at IS NULL "
            "AND cancelled_at IS NOT NULL))",
            name="ck_assessment_event_lifecycle_timestamps"),
        CheckConstraint(
            "((status IN ('due','in_progress','awaiting_closeout') AND "
            "active_protocol_slot_key IS NOT NULL AND "
            "length(active_protocol_slot_key) = 64) OR "
            "(status IN ('closed','cancelled') AND "
            "active_protocol_slot_key IS NULL))",
            name="ck_assessment_event_active_slot_lifecycle"),
        CheckConstraint(
            "((status = 'cancelled' AND "
            "cancellation_reason_code IN "
            "('schedule_changed','participant_unavailable',"
            "'protocol_correction','duplicate_event') AND "
            "length(trim(cancelled_by)) > 0) OR "
            "(status != 'cancelled' AND cancellation_reason_code IS NULL AND "
            "cancelled_by IS NULL))",
            name="ck_assessment_event_cancellation"),
    )

    event_id: str = Field(primary_key=True)
    patient_id: str = Field(foreign_key="patient.patient_id", index=True)
    assigned_assessor_id: str = Field(index=True)
    timepoint: str
    scheduled_date: date
    status: str = "due"
    revision: int = 1
    is_simulation: bool = False
    data_classification: str = "research"
    formal_outcome_eligible: bool = False
    definition_bundle_id: str
    definition_bundle_digest: str
    active_protocol_slot_key: Optional[str] = Field(default=None, index=True)
    created_by: str
    created_at: datetime = Field(default_factory=_utc_now_naive)
    updated_at: datetime = Field(default_factory=_utc_now_naive)
    started_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    cancellation_reason_code: Optional[str] = None
    cancelled_by: Optional[str] = None
    cancelled_at: Optional[datetime] = None


class AssessmentInstance(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint(
            "event_id", "category_key",
            name="uq_assessment_instance_event_category"),
        Index("ix_assessment_instance_patient_status", "patient_id", "status"),
        CheckConstraint(
            "category_key IN "
            "('untrained_standardized_naming','functional_communication')",
            name="ck_assessment_instance_category"),
        CheckConstraint(
            "status IN ('due','in_progress','completed','approved_deferred')",
            name="ck_assessment_instance_status"),
        CheckConstraint("revision >= 1", name="ck_assessment_instance_revision"),
        CheckConstraint("required_item_count >= 1",
                        name="ck_assessment_instance_required_items"),
        CheckConstraint("score_min < score_max",
                        name="ck_assessment_instance_score_range"),
        CheckConstraint(
            "score_direction IN ('higher_is_better','lower_is_better')",
            name="ck_assessment_instance_score_direction"),
        CheckConstraint(
            "NOT formal_outcome_eligible OR NOT is_simulation",
            name="ck_assessment_instance_simulation_boundary"),
        CheckConstraint(
            "((is_simulation AND data_classification = 'simulation' AND "
            "NOT formal_outcome_eligible) OR "
            "((NOT is_simulation) AND data_classification = 'research'))",
            name="ck_assessment_instance_classification"),
        CheckConstraint(
            "((status = 'completed' AND completed_at IS NOT NULL) OR "
            "(status != 'completed' AND completed_at IS NULL))",
            name="ck_assessment_instance_completion_timestamp"),
        CheckConstraint(
            "length(definition_bundle_digest) = 71 AND "
            "length(definition_digest) = 71 AND "
            "length(item_set_digest) = 71 AND "
            "length(administration_protocol_digest) = 71 AND "
            "length(response_schema_digest) = 71 AND "
            "length(result_schema_digest) = 71 AND "
            "length(missingness_rule_digest) = 71 AND "
            "length(stopping_rule_digest) = 71 AND "
            "length(scoring_algorithm_digest) = 71",
            name="ck_assessment_instance_digest_lengths"),
    )

    instance_id: str = Field(primary_key=True)
    event_id: str = Field(foreign_key="assessmentevent.event_id", index=True)
    patient_id: str = Field(foreign_key="patient.patient_id", index=True)
    category_key: str = Field(index=True)
    definition_bundle_id: str
    definition_bundle_digest: str
    definition_id: str
    instrument_id: str
    instrument_version: str
    definition_digest: str
    item_set_digest: str
    administration_protocol_digest: str
    response_schema_digest: str
    result_schema_digest: str
    missingness_rule_digest: str
    stopping_rule_digest: str
    scoring_algorithm_id: str
    scoring_algorithm_version: str
    scoring_algorithm_digest: str
    score_min: float
    score_max: float
    score_direction: str
    score_rounding_rule: str
    automatic_scoring_permitted: bool
    item_response_storage_permitted: bool
    result_storage_permitted: bool
    result_export_permitted: bool
    required_item_count: int
    status: str = "due"
    revision: int = 1
    is_simulation: bool = False
    data_classification: str = "research"
    formal_outcome_eligible: bool = False
    created_at: datetime = Field(default_factory=_utc_now_naive)
    updated_at: datetime = Field(default_factory=_utc_now_naive)
    completed_at: Optional[datetime] = None


class AssessmentItemResponse(SQLModel, table=True):
    """Append-only item response revision; payload combinations are definition-owned."""
    __table_args__ = (
        UniqueConstraint(
            "instance_id", "item_key", "item_revision",
            name="uq_assessment_response_item_revision"),
        UniqueConstraint(
            "idempotency_key_sha256",
            name="uq_assessment_response_idempotency_hash"),
        UniqueConstraint(
            "authorized_artifact_digest",
            name="uq_assessment_response_artifact_receipt_digest"),
        Index("ix_assessment_response_instance_item",
              "instance_id", "item_key", "item_revision"),
        CheckConstraint(
            "item_revision = expected_item_revision + 1",
            name="ck_assessment_response_item_revision_transition"),
        CheckConstraint(
            "instance_revision >= 2 AND event_revision >= 2",
            name="ck_assessment_response_parent_revisions"),
        CheckConstraint(
            "length(response_digest) = 71 AND "
            "(authorized_artifact_digest IS NULL OR "
            "length(authorized_artifact_digest) = 71)",
            name="ck_assessment_response_digests"),
        CheckConstraint(
            "length(response_json) <= 65536",
            name="ck_assessment_response_json_size"),
        CheckConstraint(
            "length(idempotency_key_sha256) = 64 AND length(request_hash) = 64",
            name="ck_assessment_response_request_hashes"),
    )

    response_id: str = Field(primary_key=True)
    instance_id: str = Field(
        foreign_key="assessmentinstance.instance_id", index=True)
    event_id: str = Field(foreign_key="assessmentevent.event_id", index=True)
    patient_id: str = Field(foreign_key="patient.patient_id", index=True)
    item_key: str = Field(index=True)
    item_revision: int
    expected_item_revision: int
    instance_revision: int
    event_revision: int
    response_json: str
    response_digest: str
    authorized_artifact_digest: Optional[str] = None
    idempotency_key_sha256: str
    request_hash: str
    submitted_by: str
    submitted_at: datetime = Field(default_factory=_utc_now_naive)


@sa_event.listens_for(AssessmentItemResponse, "before_update")
@sa_event.listens_for(AssessmentItemResponse, "before_delete")
def _reject_assessment_response_mutation(*_args) -> None:
    raise RuntimeError("AssessmentItemResponse 是只追加逐条目证据")


class AssessmentScoringEvidence(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("instance_id", name="uq_assessment_scoring_instance"),
        CheckConstraint(
            "answered_item_count >= 0 AND missing_item_count >= 0",
            name="ck_assessment_scoring_counts"),
        CheckConstraint(
            "score >= score_min AND score <= score_max",
            name="ck_assessment_scoring_range"),
        CheckConstraint(
            "length(definition_digest) = 71 AND "
            "length(item_response_set_digest) = 71 AND "
            "length(scoring_algorithm_digest) = 71 AND "
            "length(result_digest) = 71",
            name="ck_assessment_scoring_digests"),
        CheckConstraint(
            "length(result_json) <= 65536",
            name="ck_assessment_scoring_result_json_size"),
        CheckConstraint(
            "((stopped_early AND stopping_reason_code IS NOT NULL AND "
            "length(trim(stopping_reason_code)) BETWEEN 1 AND 200) OR "
            "((NOT stopped_early) AND stopping_reason_code IS NULL))",
            name="ck_assessment_scoring_stopping_facts"),
        CheckConstraint(
            "NOT formal_outcome_eligible OR NOT is_simulation",
            name="ck_assessment_scoring_simulation_boundary"),
    )

    evidence_id: str = Field(primary_key=True)
    instance_id: str = Field(
        foreign_key="assessmentinstance.instance_id", index=True)
    event_id: str = Field(foreign_key="assessmentevent.event_id", index=True)
    patient_id: str = Field(foreign_key="patient.patient_id", index=True)
    category_key: str
    definition_digest: str
    item_response_set_digest: str
    scoring_algorithm_id: str
    scoring_algorithm_version: str
    scoring_algorithm_digest: str
    result_schema_digest: str
    result_json: str
    result_digest: str
    score: float
    score_min: float
    score_max: float
    answered_item_count: int
    missing_item_count: int
    stopped_early: bool = False
    stopping_reason_code: Optional[str] = None
    is_simulation: bool = False
    formal_outcome_eligible: bool = False
    scored_by: str
    scored_at: datetime = Field(default_factory=_utc_now_naive)


@sa_event.listens_for(AssessmentScoringEvidence, "before_update")
@sa_event.listens_for(AssessmentScoringEvidence, "before_delete")
def _reject_assessment_scoring_mutation(*_args) -> None:
    raise RuntimeError("AssessmentScoringEvidence 是不可变计分证据")


class AssessmentDeferralApproval(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("instance_id", name="uq_assessment_deferral_instance"),
        CheckConstraint(
            "reason_code IN ('participant_unavailable','clinical_or_safety',"
            "'technical_failure','authorized_reschedule')",
            name="ck_assessment_deferral_reason"),
        CheckConstraint(
            "length(definition_digest) = 71",
            name="ck_assessment_deferral_definition_digest"),
        CheckConstraint(
            "length(trim(approved_by)) > 0",
            name="ck_assessment_deferral_approver"),
        CheckConstraint(
            "approved_role IN ('admin','local_m0')",
            name="ck_assessment_deferral_approver_role"),
    )

    deferral_id: str = Field(primary_key=True)
    instance_id: str = Field(
        foreign_key="assessmentinstance.instance_id", index=True)
    event_id: str = Field(foreign_key="assessmentevent.event_id", index=True)
    patient_id: str = Field(foreign_key="patient.patient_id", index=True)
    category_key: str
    definition_digest: str
    reason_code: str
    deferred_until: date
    approved_by: str
    approved_role: str
    approved_at: datetime = Field(default_factory=_utc_now_naive)


@sa_event.listens_for(AssessmentDeferralApproval, "before_update")
@sa_event.listens_for(AssessmentDeferralApproval, "before_delete")
def _reject_assessment_deferral_mutation(*_args) -> None:
    raise RuntimeError("AssessmentDeferralApproval 是不可变批准事实")


class AssessmentEventCloseout(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_assessment_closeout_event"),
        CheckConstraint(
            "report_status IN ('no_additional_observation','observation_recorded')",
            name="ck_assessment_closeout_status"),
        CheckConstraint("event_revision >= 2",
                        name="ck_assessment_closeout_event_revision"),
        CheckConstraint(
            "note IS NULL OR (length(note) <= 2000 AND length(trim(note)) > 0)",
            name="ck_assessment_closeout_note"),
        CheckConstraint(
            "report_status != 'no_additional_observation' OR "
            "((NOT fatigue_observed) AND "
            "(NOT distress_or_discomfort_observed) AND "
            "(NOT participant_declined_to_continue) AND "
            "(NOT staff_assistance_occurred) AND "
            "(NOT environment_interruption_occurred) AND "
            "(NOT device_or_network_interruption_occurred) AND note IS NULL)",
            name="ck_assessment_closeout_no_observation_empty"),
        CheckConstraint(
            "report_status != 'observation_recorded' OR "
            "(fatigue_observed OR distress_or_discomfort_observed OR "
            "participant_declined_to_continue OR staff_assistance_occurred OR "
            "environment_interruption_occurred OR "
            "device_or_network_interruption_occurred OR note IS NOT NULL)",
            name="ck_assessment_closeout_observation_present"),
        CheckConstraint(
            "length(instance_terminal_digest) = 71 AND "
            "length(idempotency_key_sha256) = 64 AND length(request_hash) = 64",
            name="ck_assessment_closeout_digests"),
    )

    closeout_id: str = Field(primary_key=True)
    event_id: str = Field(foreign_key="assessmentevent.event_id", index=True)
    patient_id: str = Field(foreign_key="patient.patient_id", index=True)
    event_revision: int
    instance_terminal_digest: str
    report_status: str
    fatigue_observed: bool = False
    distress_or_discomfort_observed: bool = False
    participant_declined_to_continue: bool = False
    staff_assistance_occurred: bool = False
    environment_interruption_occurred: bool = False
    device_or_network_interruption_occurred: bool = False
    note: Optional[str] = Field(default=None, max_length=2000)
    idempotency_key_sha256: str
    request_hash: str
    closed_by: str
    closed_at: datetime = Field(default_factory=_utc_now_naive)


@sa_event.listens_for(AssessmentEventCloseout, "before_update")
@sa_event.listens_for(AssessmentEventCloseout, "before_delete")
def _reject_assessment_closeout_mutation(*_args) -> None:
    raise RuntimeError("AssessmentEventCloseout 是不可变且已锁定的收尾事实")


class AssessmentCommand(SQLModel, table=True):
    """Append-only idempotent-effect and CAS ledger for assessment mutations.

    The ledger proves that one request produced one effect and records that
    effect's resulting revisions.  A later replay returns the *current*
    authoritative event projection rather than copying patient content into a
    historical response snapshot inside this command table.
    """
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key_sha256",
            name="uq_assessment_command_idempotency_hash"),
        UniqueConstraint(
            "event_id", "command_seq",
            name="uq_assessment_command_event_seq"),
        Index("ix_assessment_command_event_created", "event_id", "created_at"),
        CheckConstraint("command_seq >= 1",
                        name="ck_assessment_command_seq"),
        CheckConstraint(
            "command_type IN "
            "('create','start','response','complete','defer','close','cancel')",
            name="ck_assessment_command_type"),
        CheckConstraint(
            "expected_event_revision >= 0 AND "
            "resulting_event_revision = expected_event_revision + 1",
            name="ck_assessment_command_event_revision"),
        CheckConstraint(
            "((command_type IN ('response','complete','defer') AND "
            "instance_id IS NOT NULL AND expected_target_revision IS NOT NULL AND "
            "resulting_target_revision = expected_target_revision + 1) OR "
            "(command_type IN ('create','start','close','cancel') AND "
            "instance_id IS NULL AND expected_target_revision IS NULL AND "
            "resulting_target_revision IS NULL))",
            name="ck_assessment_command_target_revision"),
        CheckConstraint(
            "((command_type = 'response' AND item_key IS NOT NULL AND "
            "length(trim(item_key)) > 0) OR "
            "(command_type != 'response' AND item_key IS NULL))",
            name="ck_assessment_command_item_binding"),
        CheckConstraint(
            "((command_type IN ('create','start','cancel') AND "
            "result_entity_type = 'assessment_event' AND "
            "result_entity_id = event_id) OR "
            "(command_type = 'response' AND "
            "result_entity_type = 'assessment_item_response') OR "
            "(command_type = 'complete' AND "
            "result_entity_type = 'assessment_scoring_evidence') OR "
            "(command_type = 'defer' AND "
            "result_entity_type = 'assessment_deferral_approval') OR "
            "(command_type = 'close' AND "
            "result_entity_type = 'assessment_event_closeout'))",
            name="ck_assessment_command_result_entity"),
        CheckConstraint(
            "length(idempotency_key_sha256) = 64 AND length(request_hash) = 64",
            name="ck_assessment_command_hashes"),
        CheckConstraint(
            "actor_role IN ('researcher','admin','system','local_m0')",
            name="ck_assessment_command_actor_role"),
        CheckConstraint(
            "length(trim(actor_id)) > 0",
            name="ck_assessment_command_actor"),
        CheckConstraint(
            "((command_type = 'cancel' AND reason_code IN "
            "('schedule_changed','participant_unavailable',"
            "'protocol_correction','duplicate_event')) OR "
            "(command_type != 'cancel' AND reason_code IS NULL))",
            name="ck_assessment_command_reason"),
    )

    command_id: str = Field(primary_key=True)
    event_id: str = Field(foreign_key="assessmentevent.event_id", index=True)
    instance_id: Optional[str] = Field(
        default=None, foreign_key="assessmentinstance.instance_id", index=True)
    item_key: Optional[str] = None
    command_seq: int
    command_type: str
    reason_code: Optional[str] = None
    idempotency_key_sha256: str
    request_hash: str
    expected_event_revision: int
    resulting_event_revision: int
    expected_target_revision: Optional[int] = None
    resulting_target_revision: Optional[int] = None
    result_entity_type: str
    result_entity_id: str
    actor_id: str
    actor_role: str
    created_at: datetime = Field(default_factory=_utc_now_naive)


@sa_event.listens_for(AssessmentCommand, "before_update")
@sa_event.listens_for(AssessmentCommand, "before_delete")
def _reject_assessment_command_mutation(*_args) -> None:
    raise RuntimeError("AssessmentCommand 是只追加幂等/CAS账本")
