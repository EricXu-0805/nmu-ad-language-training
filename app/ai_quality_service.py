"""Read-only, deidentified projection for the AI quality dashboard.

The service deliberately applies account scope, consent/withdrawal fences and
the research small-cell threshold *before* loading attempt, transcript or
audio evidence.  It returns only one overall row for the requested research or
simulation partition; identifiers and source text never cross this boundary.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, fields
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import unicodedata
from typing import Iterable, Literal, Sequence

from sqlalchemy import Text, case, cast, func, literal, or_
from sqlalchemy.orm import load_only
from sqlmodel import Session as DBSession, select

from . import (
    audio_capture,
    audio_store,
    autopilot_ledger,
    content,
    evidence_ledger,
    runtime,
)
from .ai_quality_metrics import (
    AttemptQualityEvidence,
    OperationalMetrics,
    QualityDimensions,
    ResearchTruthMetrics,
    TurnQualityEvidence,
    aggregate_ai_quality,
)
from .enums import AudioStatus, ConsentType
from .models import (
    AttemptEvent,
    AutopilotControlEvent,
    AudioAssetRow,
    AudioCaptureReceipt,
    InteractionEvent,
    ItemEvent,
    Patient,
    Session as TrainSession,
    SessionRuntimeState,
    TechnicalPauseReceipt,
    TurnConfirmationRevision,
    TurnEvent,
)


SCHEMA_VERSION = "ai-quality-dashboard.v2"
RESEARCH_MIN_SUBJECTS_ENV = "AI_QUALITY_RESEARCH_MIN_SUBJECTS"

# One request is intentionally bounded for the current 1 GiB deployment.  A
# limit breach is an explicit error, never a partial aggregate.
MAX_VISIBLE_SESSIONS = 200
MAX_EXPECTED_TURNS = 20_000
# These are aggregate request budgets, applied with scalar SQL before any ORM
# evidence materialization or physical-audio hashing.  Per-table limits below
# remain defence in depth and make a breach an explicit non-partial response.
MAX_EVIDENCE_ROWS = 20_000
MAX_EVIDENCE_TEXT_BYTES = 8 * 1024 * 1024
MAX_AUDIO_VERIFY_BYTES = 256 * 1024 * 1024
MAX_ITEM_EVENTS = 20_000
MAX_TURN_EVENTS = 20_000
MAX_ATTEMPTS = 50_000
MAX_AUDIO_ASSETS = 50_000
MAX_AUDIO_RECEIPTS = 50_000
MAX_INTERACTIONS = 100_000
MAX_CONFIRMATION_REVISIONS = 50_000
MAX_TECHNICAL_PAUSE_RECEIPTS = 20_000
MAX_CONTROL_EVENTS = 20_000

_TERMINAL_SESSION_STATUSES = frozenset({"completed", "aborted", "failed"})
_CONSENT_GRANTED_STATUSES = frozenset({
    "已同意", "已取得", "已签署", "有效",
    "consented", "obtained", "signed", "active", "valid",
})
_CONSENT_DENIED_STATUSES = frozenset({
    "未同意", "已撤回", "拒绝", "不同意",
    "denied", "withdrawn", "refused", "declined", "rejected",
})
_BINARY_AI_TRUE = frozenset({"正确"})
# Only explicit semantic errors are binary negatives.  ASR failure, silence and
# refusal are missing/behavioural outcomes, not evidence that the participant's
# answer was wrong; they must stay outside TN/FN.
_BINARY_AI_FALSE = frozenset({"偏题", "重复"})
_CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")
_THRESHOLD_RE = re.compile(r"^[1-9][0-9]{0,2}$")
_TECHNICAL_PAUSE_KEY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,199}$")
_TECHNICAL_PAUSE_ERROR_RE = re.compile(r"^[a-z0-9._-]{1,64}$")
_AUTOPILOT_IDEMPOTENCY_KEY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_DEVICE_DRAIN_EVENT_KEY_RE = re.compile(
    r"^device-drain-[0-9a-f]{64}$")
_DEVICE_FAILURE_EVENT_KEY_RE = re.compile(
    r"^device-failure-[0-9a-f]{64}$")
_SCOPE_COMPLETE_EVENT_KEY_RE = re.compile(
    r"^scope-complete-[0-9a-f]{64}$")

_COVERAGE_FIELDS = (
    "visible_sessions",
    "included_sessions",
    "source_turns",
    "audio_evidenced_turns",
    "attempts_observed",
    "prompt_level_known_attempts",
    "processing_status_known_attempts",
    "latency_known_attempts",
    "ai_attempt_status_known_turns",
    "ai_judgement_status_known_turns",
    "asr_review_status_known_turns",
    "human_truth_locked_turns",
    "binary_eligible_reviewed_decisions",
    "binary_excluded_decisions",
)
_DIAGNOSTIC_FIELDS = (
    "restricted_or_withdrawn_sessions",
    "classification_inconsistent_sessions",
    "protocol_binding_invalid_sessions",
    "structural_invalid_evidence_records",
    "lineage_invalid_turns",
    "audio_evidence_unavailable_turns",
    "ai_attempt_status_unknown_turns",
    "ai_judgement_status_unknown_turns",
    "asr_review_status_unknown_turns",
    "human_truth_unavailable_turns",
    "binary_prediction_unavailable_turns",
    "latency_unavailable_attempts",
)

_VISIBILITY_SCOPE_BY_ROLE = {
    "researcher": "owner_sessions",
    "data_steward": "terminal_sessions",
    "admin": "all_sessions",
}


class QualityScopeTooLarge(RuntimeError):
    """The authorized partition contains more sessions than one request."""


class QualityEvidenceLimitExceeded(RuntimeError):
    """A bounded evidence collection exceeded its fixed resource limit."""

    def __init__(self, resource: str):
        super().__init__(resource)
        self.resource = resource


class QualitySnapshotUnavailable(RuntimeError):
    """The backend cannot provide one stable, read-only aggregate snapshot."""


def _begin_stable_read_snapshot(s: DBSession) -> None:
    """Start the quality projection's first and only database transaction.

    PostgreSQL's default READ COMMITTED level and pysqlite's legacy deferred
    BEGIN behaviour both allow later SELECTs to observe newer commits.  The
    dashboard spans a visibility query, resource preflight and nine evidence
    queries, so it must establish the backend-specific snapshot before the
    first SELECT.  Unknown backends and reused/dirty sessions fail closed.
    """
    if s.in_transaction():
        raise QualitySnapshotUnavailable
    try:
        dialect = s.get_bind().dialect.name
        if dialect == "postgresql":
            connection = s.connection(execution_options={
                "isolation_level": "REPEATABLE READ",
            })
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            return
        if dialect == "sqlite":
            # Merely issuing SELECT is not enough with pysqlite legacy
            # transaction control; an explicit BEGIN pins one read snapshot.
            connection = s.connection()
            connection.exec_driver_sql("BEGIN")
            return
    except QualitySnapshotUnavailable:
        raise
    except Exception as exc:
        raise QualitySnapshotUnavailable from exc
    raise QualitySnapshotUnavailable


def _release_stable_read_snapshot(s: DBSession) -> None:
    """Detach the complete projection input and end the database snapshot.

    Physical audio verification can be comparatively slow.  Keeping SQLite's
    read lock (or PostgreSQL's MVCC snapshot) across that work would make an
    observability request interfere with bedside writes.  Every ORM attribute
    consumed after this point is deliberately eager-loaded above.
    """
    try:
        s.expunge_all()
        s.rollback()
    except Exception as exc:
        raise QualitySnapshotUnavailable from exc


@dataclass(frozen=True)
class _Threshold:
    status: Literal["configured", "unconfigured", "invalid"]
    minimum: int | None


@dataclass
class _EvidenceRows:
    items: list[ItemEvent]
    turn_pairs: list[tuple[TurnEvent, str]]
    attempts: list[AttemptEvent]
    audios: list[AudioAssetRow]
    receipts: list[AudioCaptureReceipt]
    interactions: list[InteractionEvent]
    revision_pairs: list[tuple[TurnConfirmationRevision, str]]
    pause_receipts: list[TechnicalPauseReceipt]
    control_events: list[AutopilotControlEvent]


def _enum_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return "" if raw is None else str(raw)


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    rendered = value.astimezone(timezone.utc).isoformat()
    return rendered[:-6] + "Z" if rendered.endswith("+00:00") else rendered


def _parse_threshold() -> _Threshold:
    raw = os.environ.get(RESEARCH_MIN_SUBJECTS_ENV)
    if raw is None or raw == "":
        return _Threshold("unconfigured", None)
    if raw != raw.strip() or _THRESHOLD_RE.fullmatch(raw) is None:
        return _Threshold("invalid", None)
    value = int(raw)
    if value < 2 or value > 100:
        return _Threshold("invalid", None)
    return _Threshold("configured", value)


def _visible_sessions(
    s: DBSession,
    *,
    actor_id: str,
    actor_role: str,
    data_classification: str,
) -> list[TrainSession]:
    actor = actor_id.strip()
    if not actor or actor_role not in {"researcher", "data_steward", "admin"}:
        raise ValueError("unsupported named quality-dashboard principal")
    statement = select(TrainSession).where(
        TrainSession.data_classification == data_classification)
    if actor_role == "researcher":
        statement = statement.where(TrainSession.trainer_id == actor)
    elif actor_role == "data_steward":
        statement = statement.join(
            SessionRuntimeState,
            SessionRuntimeState.session_id == TrainSession.session_id,
        ).where(SessionRuntimeState.status.in_(_TERMINAL_SESSION_STATUSES))
    statement = statement.order_by(TrainSession.session_id).limit(
        MAX_VISIBLE_SESSIONS + 1)
    rows = list(s.exec(statement))
    if len(rows) > MAX_VISIBLE_SESSIONS:
        raise QualityScopeTooLarge
    return rows


def _bounded_rows(
    s: DBSession,
    statement,
    *,
    limit: int,
    resource: str,
) -> list:
    rows = list(s.exec(statement.limit(limit + 1)))
    if len(rows) > limit:
        raise QualityEvidenceLimitExceeded(resource)
    return rows


def _patient_is_research_eligible(patient: Patient) -> bool:
    if patient.is_simulation_subject:
        return False
    if (patient.consent_status or "").strip().casefold() not in (
            _CONSENT_GRANTED_STATUSES):
        return False
    if patient.consent_type is None:
        return False
    if patient.consent_type == ConsentType.代理同意加本人赞同:
        if patient.proxy_consent is not True or patient.assent_obtained is not True:
            return False
    return (
        patient.mandarin_eligible is True
        and patient.recording_allowed is True
        and not (patient.withdrawal_status or "").strip()
    )


def _patient_is_simulation_eligible(patient: Patient) -> bool:
    return (
        patient.is_simulation_subject is True
        and (patient.consent_status or "").strip().casefold()
        not in _CONSENT_DENIED_STATUSES
        and patient.recording_allowed is not False
        and not (patient.withdrawal_status or "").strip()
    )


def _preproject_sessions(
    s: DBSession,
    sessions: Sequence[TrainSession],
    *,
    data_classification: str,
) -> tuple[list[TrainSession], set[str], set[str]]:
    if not sessions:
        return [], set(), set()
    session_ids = [row.session_id for row in sessions]
    patient_ids = {row.patient_id for row in sessions}
    patients = {
        row.patient_id: row
        for row in s.exec(select(Patient).options(load_only(
            Patient.patient_id,
            Patient.is_simulation_subject,
            Patient.consent_status,
            Patient.consent_type,
            Patient.proxy_consent,
            Patient.assent_obtained,
            Patient.mandarin_eligible,
            Patient.recording_allowed,
            Patient.withdrawal_status,
        )).where(Patient.patient_id.in_(patient_ids)))
    }
    withdrawn_audio_sessions = set(s.exec(
        select(AudioAssetRow.session_id).where(
            AudioAssetRow.session_id.in_(session_ids),
            or_(
                AudioAssetRow.withdrawn.is_(True),
                func.trim(func.coalesce(AudioAssetRow.withdrawal_status, "")) != "",
            ),
        ).distinct()
    ))
    expected_simulation = data_classification == "simulation"
    restricted: set[str] = set()
    classification_bad: set[str] = set()
    included: list[TrainSession] = []
    for row in sessions:
        if row.is_simulation is not expected_simulation:
            classification_bad.add(row.session_id)
            continue
        patient = patients.get(row.patient_id)
        eligible = (
            _patient_is_simulation_eligible(patient)
            if patient is not None and expected_simulation
            else _patient_is_research_eligible(patient)
            if patient is not None
            else False
        )
        if not eligible or row.session_id in withdrawn_audio_sessions:
            restricted.add(row.session_id)
            continue
        included.append(row)
    return included, restricted, classification_bad


def _distinct_patients(sessions: Iterable[TrainSession]) -> int:
    return len({row.patient_id for row in sessions})


def _null_metrics(metric_type: type) -> dict[str, None]:
    return {field.name: None for field in fields(metric_type)}


def _dimensions(data_classification: str) -> dict[str, object]:
    return {
        "data_classification": data_classification,
        "week_no": None,
        "phase_type": None,
        "task_type": None,
        "content_group": None,
        "provider_id": None,
        "device_profile": None,
        "protocol_version": None,
        "asr_engine_version": None,
        "judge_engine_version": None,
    }


def _visibility_scope(actor_role: str) -> str:
    try:
        return _VISIBILITY_SCOPE_BY_ROLE[actor_role]
    except KeyError as exc:
        raise ValueError("unsupported named quality-dashboard principal") from exc


def _suppressed_payload(
    *,
    data_classification: str,
    visibility_scope: str,
    generated_at: datetime,
    reason: str,
    minimum: int | None,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso_utc(generated_at),
        "privacy": {
            "aggregation_only": True,
            "contains_patient_identifiers": False,
            "contains_audio": False,
            "contains_transcripts": False,
        },
        "rows": [{
            "visibility_scope": visibility_scope,
            "dimensions": _dimensions(data_classification),
            "suppression": {
                "status": "suppressed",
                "reason": reason,
                "minimum_distinct_subjects": minimum,
                # Never expose the exact small-cell cardinality.
                "distinct_subjects": None,
            },
            "coverage": {name: None for name in _COVERAGE_FIELDS},
            "diagnostics": {
                "status": "suppressed",
                "reason_counts": {name: None for name in _DIAGNOSTIC_FIELDS},
            },
            "operational": _null_metrics(OperationalMetrics),
            "research_truth": _null_metrics(ResearchTruthMetrics),
        }],
    }


def _plans_for_sessions(
    sessions: Sequence[TrainSession],
) -> tuple[dict[str, runtime.SessionPlan], set[str]]:
    banks_by_week: dict[int, tuple[content.ItemBank, str]] = {}

    def _bank_for_week(week_no: int) -> tuple[content.ItemBank, str]:
        key = week_no if week_no >= 2 else content.RAPPORT_ANCHOR_WEEK
        if key not in banks_by_week:
            bank = content.load_item_bank_for_week(key)
            banks_by_week[key] = (
                bank, content.item_bank_definition_digest(bank))
        return banks_by_week[key]

    protocol = content.load_autopilot_protocol(
        content.CONTENT_DIR / "autopilot_protocol_v1.json")
    if content.validate_autopilot_protocol(protocol):
        raise ValueError("current autopilot protocol is not valid")
    protocol_version = str(protocol["protocol_version_id"])
    protocol_digest = content.autopilot_protocol_definition_digest(protocol)
    plans: dict[str, runtime.SessionPlan] = {}
    invalid: set[str] = set()
    expected_turns = 0
    for row in sessions:
        try:
            bank, bank_digest = _bank_for_week(row.week_no)
        except content.TrainingWeekContentUnavailable:
            # 该周未登记结构化题库:场次按证据无效计,不拖垮整个看板。
            invalid.add(row.session_id)
            continue
        if (
            row.item_bank_version_id != bank.version_id
            or row.item_bank_definition_digest != bank_digest
            or row.autopilot_protocol_version_id != protocol_version
            or row.autopilot_protocol_definition_digest != protocol_digest
        ):
            invalid.add(row.session_id)
            continue
        try:
            plan = runtime.build_session_plan(
                bank, row.week_no, _enum_value(row.event_line))
        except (TypeError, ValueError):
            invalid.add(row.session_id)
            continue
        expected_turns += plan.total_turns()
        if expected_turns > MAX_EXPECTED_TURNS:
            raise QualityEvidenceLimitExceeded("expected_turns")
        plans[row.session_id] = plan
    return plans, invalid


def _count_rows(s: DBSession, model: type, condition) -> int:
    return int(s.exec(
        select(func.count()).select_from(model).where(condition)
    ).one() or 0)


def _loaded_text_bytes_upper_bound(
    s: DBSession,
    model: type,
    condition,
    columns: Sequence,
) -> int:
    # UTF-8 uses at most four bytes per Unicode scalar.  Multiplying SQL
    # character length by four intentionally overestimates ASCII and therefore
    # cannot let a multibyte transcript evade the byte budget.
    length_expression = literal(0)
    for column in columns:
        length_expression = (
            length_expression
            + func.length(func.coalesce(cast(column, Text), "")))
    value = s.exec(select(
        func.coalesce(func.sum(length_expression), 0)
    ).select_from(model).where(condition)).one()
    return int(value or 0) * 4


def _preflight_evidence_budget(
    s: DBSession,
    session_ids: Sequence[str],
) -> None:
    """Reject an oversized projection before loading rows or hashing blobs."""
    if not session_ids:
        return
    ids = tuple(session_ids)
    scopes = (
        (ItemEvent, ItemEvent.session_id.in_(ids)),
        (TurnEvent, TurnEvent.item_event_id.in_(
            select(ItemEvent.id).where(ItemEvent.session_id.in_(ids))
        )),
        (AttemptEvent, AttemptEvent.session_id.in_(ids)),
        (AudioAssetRow, AudioAssetRow.session_id.in_(ids)),
        (AudioCaptureReceipt, AudioCaptureReceipt.session_id.in_(ids)),
        (InteractionEvent, InteractionEvent.session_id.in_(ids)),
        (TurnConfirmationRevision,
         TurnConfirmationRevision.session_id.in_(ids)),
        (TechnicalPauseReceipt,
         TechnicalPauseReceipt.session_id.in_(ids)),
        (AutopilotControlEvent,
         AutopilotControlEvent.session_id.in_(ids)),
    )
    total_rows = 0
    for model, condition in scopes:
        total_rows += _count_rows(s, model, condition)
        if total_rows > MAX_EVIDENCE_ROWS:
            raise QualityEvidenceLimitExceeded("evidence_rows")

    text_scopes = (
        (ItemEvent, ItemEvent.session_id.in_(ids), (
            ItemEvent.session_id, ItemEvent.item_id, ItemEvent.task_type,
        )),
        (TurnEvent, TurnEvent.item_event_id.in_(
            select(ItemEvent.id).where(ItemEvent.session_id.in_(ids))), (
                TurnEvent.response_role, TurnEvent.raw_audio_id,
                TurnEvent.asr_text, TurnEvent.confirmed_response_text,
                TurnEvent.ai_answer_type, TurnEvent.reviewer_id,
        )),
        (AttemptEvent, AttemptEvent.session_id.in_(ids), (
            AttemptEvent.session_id, AttemptEvent.item_id,
            AttemptEvent.response_role, AttemptEvent.raw_audio_id,
            AttemptEvent.asr_text, AttemptEvent.operational_answer_type,
            AttemptEvent.processing_status, AttemptEvent.error_code,
            AttemptEvent.asr_engine_version,
            AttemptEvent.judge_engine_version,
        )),
        (AudioAssetRow, AudioAssetRow.session_id.in_(ids), (
            AudioAssetRow.raw_audio_id, AudioAssetRow.session_id,
            AudioAssetRow.data_classification, AudioAssetRow.turn_key,
            AudioAssetRow.status, AudioAssetRow.withdrawal_status,
            AudioAssetRow.checksum,
        )),
        (AudioCaptureReceipt, AudioCaptureReceipt.session_id.in_(ids), (
            AudioCaptureReceipt.raw_audio_id,
            AudioCaptureReceipt.session_id,
            AudioCaptureReceipt.turn_key,
            AudioCaptureReceipt.checksum,
            AudioCaptureReceipt.data_classification,
        )),
        (InteractionEvent, InteractionEvent.session_id.in_(ids), (
            InteractionEvent.session_id, InteractionEvent.item_id,
            InteractionEvent.event_type, InteractionEvent.payload_json,
        )),
        (TurnConfirmationRevision,
         TurnConfirmationRevision.session_id.in_(ids), (
             TurnConfirmationRevision.session_id,
             TurnConfirmationRevision.actor_display_id,
             TurnConfirmationRevision.before_sha256,
             TurnConfirmationRevision.after_sha256,
             TurnConfirmationRevision.idempotency_key,
        )),
        (TechnicalPauseReceipt,
         TechnicalPauseReceipt.session_id.in_(ids), (
             TechnicalPauseReceipt.session_id,
             TechnicalPauseReceipt.idempotency_key,
             TechnicalPauseReceipt.request_hash,
             TechnicalPauseReceipt.cursor_json,
         )),
        (AutopilotControlEvent,
         AutopilotControlEvent.session_id.in_(ids), (
             AutopilotControlEvent.idempotency_key,
             AutopilotControlEvent.session_id,
             AutopilotControlEvent.event_type,
             AutopilotControlEvent.scope_key,
             AutopilotControlEvent.actor_type,
             AutopilotControlEvent.actor_id,
             AutopilotControlEvent.reason_code,
             AutopilotControlEvent.from_mode,
             AutopilotControlEvent.to_mode,
             AutopilotControlEvent.from_status,
             AutopilotControlEvent.to_status,
             AutopilotControlEvent.payload_json,
         )),
    )
    total_text_bytes = 0
    for model, condition, columns in text_scopes:
        total_text_bytes += _loaded_text_bytes_upper_bound(
            s, model, condition, columns)
        if total_text_bytes > MAX_EVIDENCE_TEXT_BYTES:
            raise QualityEvidenceLimitExceeded("evidence_text_bytes")

    declared_audio_bytes = s.exec(select(func.coalesce(func.sum(case(
        (AudioAssetRow.byte_count > 0, AudioAssetRow.byte_count),
        else_=0,
    )), 0)).where(AudioAssetRow.session_id.in_(ids))).one()
    if int(declared_audio_bytes or 0) > MAX_AUDIO_VERIFY_BYTES:
        raise QualityEvidenceLimitExceeded("audio_verify_bytes")


def _load_evidence_rows(
    s: DBSession,
    session_ids: Sequence[str],
) -> _EvidenceRows:
    if not session_ids:
        return _EvidenceRows([], [], [], [], [], [], [], [], [])
    ids = tuple(session_ids)
    items = _bounded_rows(
        s,
        select(ItemEvent).options(load_only(
            ItemEvent.id,
            ItemEvent.session_id,
            ItemEvent.item_id,
            ItemEvent.task_type,
        )).where(ItemEvent.session_id.in_(ids)).order_by(ItemEvent.id),
        limit=MAX_ITEM_EVENTS,
        resource="item_events",
    )
    turn_pairs = _bounded_rows(
        s,
        select(TurnEvent, ItemEvent.session_id).options(load_only(
            TurnEvent.id,
            TurnEvent.item_event_id,
            TurnEvent.source_attempt_id,
            TurnEvent.turn_seq,
            TurnEvent.response_role,
            TurnEvent.raw_audio_id,
            TurnEvent.asr_text,
            TurnEvent.confirmed_response_text,
            TurnEvent.confirmation_revision,
            TurnEvent.prompt_level,
            TurnEvent.ai_answer_type,
            TurnEvent.ai_score,
            TurnEvent.judge_portrait_used,
            TurnEvent.reviewer_id,
            TurnEvent.reviewed_score,
            TurnEvent.score_locked,
            TurnEvent.element_value,
        )).join(
            ItemEvent, TurnEvent.item_event_id == ItemEvent.id,
        ).where(ItemEvent.session_id.in_(ids)).order_by(TurnEvent.id),
        limit=MAX_TURN_EVENTS,
        resource="observed_turns",
    )
    attempts = _bounded_rows(
        s,
        select(AttemptEvent).options(load_only(
            AttemptEvent.id,
            AttemptEvent.session_id,
            AttemptEvent.item_id,
            AttemptEvent.turn_seq,
            AttemptEvent.response_role,
            AttemptEvent.attempt_seq,
            AttemptEvent.raw_audio_id,
            AttemptEvent.prompt_level,
            AttemptEvent.asr_text,
            AttemptEvent.operational_answer_type,
            AttemptEvent.operational_score,
            AttemptEvent.judge_portrait_used,
            AttemptEvent.processing_status,
            AttemptEvent.error_code,
            AttemptEvent.asr_engine_version,
            AttemptEvent.judge_engine_version,
            AttemptEvent.created_at,
            AttemptEvent.processed_at,
            AttemptEvent.is_simulation,
        )).where(AttemptEvent.session_id.in_(ids)).order_by(AttemptEvent.id),
        limit=MAX_ATTEMPTS,
        resource="attempts",
    )
    audios = _bounded_rows(
        s,
        select(AudioAssetRow).options(load_only(
            AudioAssetRow.raw_audio_id,
            AudioAssetRow.session_id,
            AudioAssetRow.is_simulation,
            AudioAssetRow.data_classification,
            AudioAssetRow.turn_key,
            AudioAssetRow.status,
            AudioAssetRow.withdrawn,
            AudioAssetRow.withdrawal_status,
            AudioAssetRow.checksum,
            AudioAssetRow.byte_count,
            AudioAssetRow.uploaded_at,
            AudioAssetRow.contains_direct_identifier,
        )).where(AudioAssetRow.session_id.in_(ids)).order_by(
            AudioAssetRow.raw_audio_id),
        limit=MAX_AUDIO_ASSETS,
        resource="audio_assets",
    )
    receipts = _bounded_rows(
        s,
        select(AudioCaptureReceipt).options(load_only(
            AudioCaptureReceipt.server_seq,
            AudioCaptureReceipt.raw_audio_id,
            AudioCaptureReceipt.session_id,
            AudioCaptureReceipt.turn_key,
            AudioCaptureReceipt.byte_count,
            AudioCaptureReceipt.checksum,
            AudioCaptureReceipt.data_classification,
            AudioCaptureReceipt.is_simulation,
            AudioCaptureReceipt.contains_direct_identifier,
        )).where(
            AudioCaptureReceipt.session_id.in_(ids)).order_by(
                AudioCaptureReceipt.server_seq),
        limit=MAX_AUDIO_RECEIPTS,
        resource="audio_receipts",
    )
    interactions = _bounded_rows(
        s,
        select(InteractionEvent).options(load_only(
            InteractionEvent.id,
            InteractionEvent.session_id,
            InteractionEvent.event_seq,
            InteractionEvent.item_id,
            InteractionEvent.turn_seq,
            InteractionEvent.attempt_id,
            InteractionEvent.attempt_seq,
            InteractionEvent.event_type,
            InteractionEvent.payload_json,
            InteractionEvent.is_simulation,
        )).where(
            InteractionEvent.session_id.in_(ids)).order_by(InteractionEvent.id),
        limit=MAX_INTERACTIONS,
        resource="interactions",
    )
    revision_pairs = _bounded_rows(
        s,
        select(TurnConfirmationRevision, ItemEvent.session_id).options(load_only(
            TurnConfirmationRevision.id,
            TurnConfirmationRevision.turn_id,
            TurnConfirmationRevision.session_id,
            TurnConfirmationRevision.revision,
            TurnConfirmationRevision.expected_revision,
            TurnConfirmationRevision.actor_display_id,
            TurnConfirmationRevision.before_sha256,
            TurnConfirmationRevision.after_sha256,
            TurnConfirmationRevision.idempotency_key,
        )).join(
            TurnEvent, TurnConfirmationRevision.turn_id == TurnEvent.id,
        ).join(
            ItemEvent, TurnEvent.item_event_id == ItemEvent.id,
        ).where(ItemEvent.session_id.in_(ids)).order_by(
            TurnConfirmationRevision.id),
        limit=MAX_CONFIRMATION_REVISIONS,
        resource="confirmation_revisions",
    )
    pause_receipts = _bounded_rows(
        s,
        select(TechnicalPauseReceipt).options(load_only(
            TechnicalPauseReceipt.id,
            TechnicalPauseReceipt.session_id,
            TechnicalPauseReceipt.interaction_event_id,
            TechnicalPauseReceipt.idempotency_key,
            TechnicalPauseReceipt.request_hash,
            TechnicalPauseReceipt.expected_runtime_revision,
            TechnicalPauseReceipt.expected_live_wseq,
            TechnicalPauseReceipt.runtime_revision,
            TechnicalPauseReceipt.paused_cursor_wseq,
            TechnicalPauseReceipt.live_seq,
            TechnicalPauseReceipt.cursor_json,
        )).where(TechnicalPauseReceipt.session_id.in_(ids)).order_by(
            TechnicalPauseReceipt.id),
        limit=MAX_TECHNICAL_PAUSE_RECEIPTS,
        resource="technical_pause_receipts",
    )
    control_events = _bounded_rows(
        s,
        select(AutopilotControlEvent).options(load_only(
            AutopilotControlEvent.id,
            AutopilotControlEvent.idempotency_key,
            AutopilotControlEvent.session_id,
            AutopilotControlEvent.event_seq,
            AutopilotControlEvent.event_type,
            AutopilotControlEvent.scope_key,
            AutopilotControlEvent.control_generation,
            AutopilotControlEvent.runner_generation,
            AutopilotControlEvent.command_id,
            AutopilotControlEvent.actor_type,
            AutopilotControlEvent.actor_id,
            AutopilotControlEvent.reason_code,
            AutopilotControlEvent.from_mode,
            AutopilotControlEvent.to_mode,
            AutopilotControlEvent.from_status,
            AutopilotControlEvent.to_status,
            AutopilotControlEvent.payload_json,
        )).where(AutopilotControlEvent.session_id.in_(ids)).order_by(
            AutopilotControlEvent.id),
        limit=MAX_CONTROL_EVENTS,
        resource="autopilot_control_events",
    )
    return _EvidenceRows(
        items=items,
        turn_pairs=turn_pairs,
        attempts=attempts,
        audios=audios,
        receipts=receipts,
        interactions=interactions,
        revision_pairs=revision_pairs,
        pause_receipts=pause_receipts,
        control_events=control_events,
    )


def _loaded_text_width(rows: Iterable[object], attributes: Sequence[str]) -> int:
    """Mirror the SQL preflight's conservative UTF-8 upper bound in memory."""
    total = 0
    for row in rows:
        for attribute in attributes:
            value = getattr(row, attribute)
            if value is not None:
                total += len(str(value)) * 4
    return total


def _enforce_loaded_evidence_budget(rows: _EvidenceRows) -> None:
    """Recheck aggregate budgets from the exact rows about to be processed.

    This is defense in depth for engines or future call paths whose snapshot
    semantics regress.  It executes before any physical blob directory scan or
    hash and never returns a partial aggregate.
    """
    row_groups: tuple[Sequence[object], ...] = (
        rows.items,
        rows.turn_pairs,
        rows.attempts,
        rows.audios,
        rows.receipts,
        rows.interactions,
        rows.revision_pairs,
        rows.pause_receipts,
        rows.control_events,
    )
    if sum(len(group) for group in row_groups) > MAX_EVIDENCE_ROWS:
        raise QualityEvidenceLimitExceeded("evidence_rows")

    text_groups: tuple[tuple[Iterable[object], tuple[str, ...]], ...] = (
        (rows.items, ("session_id", "item_id", "task_type")),
        ((row for row, _session_id in rows.turn_pairs), (
            "response_role", "raw_audio_id", "asr_text",
            "confirmed_response_text", "ai_answer_type", "reviewer_id",
        )),
        (rows.attempts, (
            "session_id", "item_id", "response_role", "raw_audio_id",
            "asr_text", "operational_answer_type", "processing_status",
            "error_code", "asr_engine_version", "judge_engine_version",
        )),
        (rows.audios, (
            "raw_audio_id", "session_id", "data_classification", "turn_key",
            "status", "withdrawal_status", "checksum",
        )),
        (rows.receipts, (
            "raw_audio_id", "session_id", "turn_key", "checksum",
            "data_classification",
        )),
        (rows.interactions, (
            "session_id", "item_id", "event_type", "payload_json",
        )),
        ((row for row, _session_id in rows.revision_pairs), (
            "session_id", "actor_display_id", "before_sha256", "after_sha256",
            "idempotency_key",
        )),
        (rows.pause_receipts, (
            "session_id", "idempotency_key", "request_hash", "cursor_json",
        )),
        (rows.control_events, (
            "idempotency_key", "session_id", "event_type", "scope_key",
            "actor_type", "actor_id", "reason_code", "from_mode", "to_mode",
            "from_status", "to_status", "payload_json",
        )),
    )
    total_text_bytes = 0
    for group, attributes in text_groups:
        total_text_bytes += _loaded_text_width(group, attributes)
        if total_text_bytes > MAX_EVIDENCE_TEXT_BYTES:
            raise QualityEvidenceLimitExceeded("evidence_text_bytes")

    declared_audio_bytes = sum(
        max(int(row.byte_count or 0), 0) for row in rows.audios
    )
    if declared_audio_bytes > MAX_AUDIO_VERIFY_BYTES:
        raise QualityEvidenceLimitExceeded("audio_verify_bytes")


def _classification_inconsistent_sessions(
    rows: _EvidenceRows,
    *,
    expected_simulation: bool,
    data_classification: str,
) -> set[str]:
    bad: set[str] = set()
    for row in rows.attempts:
        if row.is_simulation is not expected_simulation:
            bad.add(row.session_id)
    for row in rows.interactions:
        if row.is_simulation is not expected_simulation:
            bad.add(row.session_id)
    for row in rows.audios:
        if (row.is_simulation is not expected_simulation
                or row.data_classification != data_classification):
            if row.session_id is not None:
                bad.add(row.session_id)
    for row in rows.receipts:
        if (row.is_simulation is not expected_simulation
                or row.data_classification != data_classification):
            bad.add(row.session_id)
    return bad


def _group_evidence(rows: _EvidenceRows) -> dict[str, dict[str, list]]:
    grouped: dict[str, dict[str, list]] = defaultdict(
        lambda: {
            "items": [], "turns": [], "attempts": [], "audios": [],
            "receipts": [], "interactions": [], "revisions": [],
            "pause_receipts": [], "control_events": [],
        })
    for row in rows.items:
        grouped[row.session_id]["items"].append(row)
    for row, session_id in rows.turn_pairs:
        grouped[session_id]["turns"].append(row)
    for row in rows.attempts:
        grouped[row.session_id]["attempts"].append(row)
    for row in rows.audios:
        if row.session_id is not None:
            grouped[row.session_id]["audios"].append(row)
    for row in rows.receipts:
        grouped[row.session_id]["receipts"].append(row)
    for row in rows.interactions:
        grouped[row.session_id]["interactions"].append(row)
    for row, session_id in rows.revision_pairs:
        grouped[session_id]["revisions"].append(row)
    for row in rows.pause_receipts:
        grouped[row.session_id]["pause_receipts"].append(row)
    for row in rows.control_events:
        grouped[row.session_id]["control_events"].append(row)
    return grouped


def _expected_positions(
    plan: runtime.SessionPlan,
) -> dict[tuple[str, int], tuple[runtime.PlanItem, runtime.PlanTurn]]:
    return {
        (item.item_id, turn.turn_seq): (item, turn)
        for item in plan.items
        for turn in item.turns
    }


def _week1_relationship_item_ids(session: TrainSession) -> frozenset[str]:
    if session.week_no != 1:
        return frozenset()
    script = content.load_week1_script(
        content.CONTENT_DIR / "week1_script.json")
    return frozenset(
        f"关系建立·{section['key']}"
        for section in script.get("sections", [])
        if isinstance(section, dict)
        and isinstance(section.get("key"), str)
        and section["key"]
    )


def _structural_evidence_invalid(
    plan: runtime.SessionPlan,
    grouped: dict[str, list],
) -> int:
    expected = _expected_positions(plan)
    expected_items = {item.item_id: item for item in plan.items}
    invalid = 0
    observed_item_ids: set[str] = set()
    item_by_id: dict[int, ItemEvent] = {}
    for row in grouped["items"]:
        plan_item = expected_items.get(row.item_id)
        if (row.item_id in observed_item_ids or plan_item is None
                or _enum_value(row.task_type) != plan_item.task_type):
            invalid += 1
        observed_item_ids.add(row.item_id)
        if row.id is not None:
            item_by_id[row.id] = row
    observed_turns: set[tuple[str, int]] = set()
    for row in grouped["turns"]:
        item = item_by_id.get(row.item_event_id)
        key = (item.item_id, row.turn_seq) if item is not None else ("", -1)
        expected_row = expected.get(key)
        if (key in observed_turns or expected_row is None
                or row.response_role != expected_row[1].response_role):
            invalid += 1
        observed_turns.add(key)
    observed_attempts: set[tuple[str, int, int]] = set()
    for row in grouped["attempts"]:
        key = (row.item_id, row.turn_seq)
        attempt_key = (row.item_id, row.turn_seq, row.attempt_seq)
        expected_row = expected.get(key)
        if (attempt_key in observed_attempts or expected_row is None
                or row.response_role != expected_row[1].response_role
                or row.attempt_seq < 1):
            invalid += 1
        observed_attempts.add(attempt_key)
    return invalid


def _audio_is_verified(
    session: TrainSession,
    attempt: AttemptEvent,
    audio_by_id: dict[str, AudioAssetRow],
    receipts_by_audio: dict[str, list[AudioCaptureReceipt]],
    blob_index: dict[str, Path],
    verification_cache: dict[str, bool] | None = None,
) -> bool:
    if (verification_cache is not None
            and attempt.raw_audio_id in verification_cache):
        return verification_cache[attempt.raw_audio_id]

    def result(value: bool) -> bool:
        if verification_cache is not None:
            verification_cache[attempt.raw_audio_id] = value
        return value

    audio = audio_by_id.get(attempt.raw_audio_id)
    receipts = receipts_by_audio.get(attempt.raw_audio_id, [])
    if audio is None or len(receipts) != 1:
        return result(False)
    checksum = (audio.checksum or "").strip()
    if (
        audio.session_id != session.session_id
        or audio.turn_key != f"{attempt.item_id}#{attempt.turn_seq}"
        or audio.is_simulation is not session.is_simulation
        or audio.data_classification != session.data_classification
        or audio.withdrawn
        or bool((audio.withdrawal_status or "").strip())
        or _enum_value(audio.status) == AudioStatus.deleted.value
        or isinstance(audio.byte_count, bool)
        or not isinstance(audio.byte_count, int)
        or audio.byte_count <= 0
        or audio.uploaded_at is None
        or _CHECKSUM_RE.fullmatch(checksum) is None
    ):
        return result(False)
    receipt = receipts[0]
    receipt_matches = (
        receipt.raw_audio_id == audio.raw_audio_id
        and receipt.session_id == session.session_id
        and receipt.turn_key == audio.turn_key
        and receipt.byte_count == audio.byte_count
        and receipt.checksum == checksum
        and receipt.data_classification == audio.data_classification
        and receipt.is_simulation is audio.is_simulation
        and receipt.contains_direct_identifier == audio.contains_direct_identifier
    )
    if not receipt_matches:
        return result(False)
    try:
        # Reject a missing, non-regular, symlinked or size-mismatched blob
        # before the more expensive SHA-256 pass.  The request-level declared
        # byte budget has already run before this function is reachable.
        path = blob_index.get(audio.raw_audio_id)
        if (path is None or path.is_symlink() or not path.is_file()
                or path.stat().st_size != audio.byte_count):
            return result(False)
        audio_capture.verify_persisted_audio(
            audio, max_bytes=audio.byte_count, indexed_path=path)
    except (audio_capture.AudioCaptureIntegrityError, OSError):
        # Disk paths and integrity details are deliberately not returned by the
        # aggregate projection.  Missing/corrupt bytes simply make this source
        # evidence unavailable.
        return result(False)
    return result(True)


def _confirmation_text_sha256(text: str | None) -> str:
    payload = b"\x00NULL" if text is None else b"\x01TEXT" + text.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _confirmation_ledger_valid(
    turn: TurnEvent,
    revisions_by_turn: dict[int, list[TurnConfirmationRevision]],
    *,
    session_id: str,
) -> bool:
    if turn.id is None or turn.confirmation_revision <= 0:
        return False
    revisions = sorted(
        revisions_by_turn.get(turn.id, []), key=lambda row: row.revision)
    if len(revisions) != turn.confirmation_revision:
        return False
    previous_after: str | None = None
    for expected_revision, row in enumerate(revisions, start=1):
        if (
            row.turn_id != turn.id
            or row.session_id != session_id
            or row.revision != expected_revision
            or row.expected_revision != expected_revision - 1
            or _CHECKSUM_RE.fullmatch(row.before_sha256 or "") is None
            or _CHECKSUM_RE.fullmatch(row.after_sha256 or "") is None
            or not (row.actor_display_id or "").strip()
            or not (row.idempotency_key or "").strip()
            or (previous_after is not None
                and not hmac.compare_digest(row.before_sha256, previous_after))
        ):
            return False
        previous_after = row.after_sha256
    if turn.confirmed_response_text is None or previous_after is None:
        return False
    return hmac.compare_digest(
        previous_after, _confirmation_text_sha256(turn.confirmed_response_text))


def _normalized_text(value: str | None) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def _attempt_latency_ms(
    attempt: AttemptEvent,
    audio: AudioAssetRow | None,
) -> float | None:
    """Measure server upload completion to classification completion.

    The caller supplies ``audio`` only after the immutable capture receipt and
    physical blob have been verified.  ``AttemptEvent.created_at`` is the later
    processing-request time and would hide queueing or delayed dispatch.
    """
    if attempt.processed_at is None or audio is None or audio.uploaded_at is None:
        return None
    uploaded_aware = (
        audio.uploaded_at.tzinfo is not None
        and audio.uploaded_at.utcoffset() is not None)
    processed_aware = (
        attempt.processed_at.tzinfo is not None
        and attempt.processed_at.utcoffset() is not None)
    if uploaded_aware != processed_aware:
        return None
    latency = (attempt.processed_at - audio.uploaded_at).total_seconds() * 1000
    return latency if latency >= 0 else None


def _ai_binary(answer_type: str | None) -> bool | None:
    normalized = (answer_type or "").strip()
    if normalized in _BINARY_AI_TRUE:
        return True
    if normalized in _BINARY_AI_FALSE:
        return False
    return None


def _same_number(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return float(left) == float(right)


def _turn_source_valid(
    session: TrainSession,
    item_id: str,
    turn: TurnEvent,
    source: AttemptEvent | None,
    *,
    audio_by_id: dict[str, AudioAssetRow],
    receipts_by_audio: dict[str, list[AudioCaptureReceipt]],
    blob_index: dict[str, Path],
    verification_cache: dict[str, bool],
) -> bool:
    return bool(
        source is not None
        and source.id == turn.source_attempt_id
        and source.session_id == session.session_id
        and source.item_id == item_id
        and source.turn_seq == turn.turn_seq
        and source.response_role == (turn.response_role or "")
        and source.raw_audio_id == turn.raw_audio_id
        and _enum_value(source.processing_status) == "completed"
        and source.is_simulation is session.is_simulation
        and source.judge_portrait_used is False
        and turn.judge_portrait_used is False
        and turn.prompt_level == source.prompt_level
        and turn.asr_text == source.asr_text
        and turn.ai_answer_type == source.operational_answer_type
        and _same_number(turn.ai_score, source.operational_score)
        and _audio_is_verified(
            session, source, audio_by_id, receipts_by_audio,
            blob_index,
            verification_cache)
    )


def _canonical_interaction_payload(
    row: InteractionEvent,
) -> dict[str, object] | None:
    try:
        payload = evidence_ledger.validate_stored_payload(
            row.event_type, row.payload_json)
        canonical = evidence_ledger.encode_event_payload(
            row.event_type, payload)
    except ValueError:
        return None
    return payload if canonical == row.payload_json else None


def _automatic_failure_pause_valid(
    event: InteractionEvent,
    predecessor: InteractionEvent | None,
    attempt: AttemptEvent | None,
) -> bool:
    """Recognize the server-owned AI failure transaction without a receipt.

    ``_finish_attempt_failure`` atomically terminates the Attempt, appends one
    ASR/judgement failure immediately followed by ``technical_pause``, and
    pauses runtime.  This exact immutable chain is the authority for automatic
    pauses; an unlinked legacy label remains invalid.
    """
    if (
        event.id is None
        or event.event_type != "technical_pause"
        or attempt is None
        or predecessor is None
        or predecessor.event_seq + 1 != event.event_seq
        or predecessor.session_id != event.session_id
        or predecessor.item_id != event.item_id
        or predecessor.turn_seq != event.turn_seq
        or predecessor.attempt_id != event.attempt_id
        or event.attempt_id != attempt.id
        or predecessor.attempt_seq != event.attempt_seq
        or event.attempt_seq != attempt.attempt_seq
        or predecessor.is_simulation is not event.is_simulation
        or attempt.is_simulation is not event.is_simulation
        or attempt.session_id != event.session_id
        or attempt.item_id != event.item_id
        or attempt.turn_seq != event.turn_seq
        or _enum_value(attempt.processing_status) != "technical_failure"
        or attempt.processed_at is None
    ):
        return False
    pause_payload = _canonical_interaction_payload(event)
    failure_payload = _canonical_interaction_payload(predecessor)
    if (
        pause_payload is None
        or failure_payload is None
        or set(pause_payload) != {"error_code"}
        or pause_payload.get("error_code") != attempt.error_code
        or failure_payload.get("error_code") != attempt.error_code
        or not isinstance(attempt.error_code, str)
        or _TECHNICAL_PAUSE_ERROR_RE.fullmatch(attempt.error_code) is None
    ):
        return False
    if predecessor.event_type == "asr_failed":
        engine_version = attempt.asr_engine_version
        return bool(
            set(failure_payload) == {"asr_engine_version", "error_code"}
            and (
                engine_version is None
                or (isinstance(engine_version, str) and bool(engine_version))
            )
            and failure_payload.get("asr_engine_version")
            == engine_version
        )
    if predecessor.event_type == "judgement_failed":
        engine_version = attempt.judge_engine_version
        return bool(
            set(failure_payload) == {"judge_engine_version", "error_code"}
            and (
                engine_version is None
                or (isinstance(engine_version, str) and bool(engine_version))
            )
            and failure_payload.get("judge_engine_version")
            == engine_version
        )
    return False


def _patient_rec_failure_pause_valid(
    event: InteractionEvent,
    expected_positions: dict[
        tuple[str, int], tuple[runtime.PlanItem, runtime.PlanTurn]],
    week1_item_ids: frozenset[str],
    failure_id_counts: dict[str, int],
) -> bool:
    """Recognize one idempotent, server-validated microphone-start failure."""
    if (
        event.id is None
        or event.event_type != "technical_pause"
        or event.attempt_id is not None
        or event.attempt_seq is not None
        or not isinstance(event.event_seq, int)
        or isinstance(event.event_seq, bool)
        or event.event_seq < 1
        or not isinstance(event.item_id, str)
    ):
        return False
    task_position_valid = bool(
        isinstance(event.turn_seq, int)
        and not isinstance(event.turn_seq, bool)
        and (event.item_id, event.turn_seq) in expected_positions
    )
    rapport_position_valid = bool(
        event.turn_seq is None and event.item_id in week1_item_ids)
    if not task_position_valid and not rapport_position_valid:
        return False
    payload = _canonical_interaction_payload(event)
    if payload is None or set(payload) != {"error_code", "failure_id"}:
        return False
    error_code = payload.get("error_code")
    failure_id = payload.get("failure_id")
    return bool(
        evidence_ledger.valid_patient_rec_failure_fact(
            error_code, failure_id)
        and isinstance(failure_id, str)
        and failure_id_counts.get(failure_id) == 1
    )


def _valid_pause_events(
    grouped: dict[str, list],
    plan: runtime.SessionPlan,
    *,
    week1_item_ids: frozenset[str],
) -> tuple[list[InteractionEvent], int, int]:
    expected_positions = _expected_positions(plan)
    interactions_by_id = {
        row.id: row for row in grouped["interactions"] if row.id is not None
    }
    attempts_by_id = {
        row.id: row for row in grouped["attempts"] if row.id is not None
    }
    interactions_by_seq = {
        row.event_seq: row for row in grouped["interactions"]
    }
    receipt_event_ids = {
        row.interaction_event_id for row in grouped["pause_receipts"]
    }
    failure_id_counts: dict[str, int] = defaultdict(int)
    for row in grouped["interactions"]:
        if row.event_type != "technical_pause":
            continue
        try:
            raw_payload = json.loads(row.payload_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        failure_id = (
            raw_payload.get("failure_id")
            if isinstance(raw_payload, dict) else None)
        if isinstance(failure_id, str):
            failure_id_counts[failure_id] += 1
    valid: list[InteractionEvent] = []
    invalid = 0
    seen_events: set[int] = set()
    for receipt in grouped["pause_receipts"]:
        event = interactions_by_id.get(receipt.interaction_event_id)
        payload = (
            _canonical_interaction_payload(event)
            if event is not None else None
        )
        try:
            cursor = json.loads(receipt.cursor_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            cursor = None
        error_code = payload.get("error_code") if isinstance(payload, dict) else None
        attempt = (
            attempts_by_id.get(event.attempt_id)
            if event is not None and event.attempt_id is not None else None
        )
        attempt_binding_valid = bool(
            event is not None
            and (
                (
                    event.attempt_id is None
                    and event.attempt_seq is None
                )
                or (
                    attempt is not None
                    and event.attempt_id == attempt.id
                    and event.attempt_seq == attempt.attempt_seq
                    and attempt.session_id == event.session_id
                    and attempt.item_id == event.item_id
                    and attempt.turn_seq == event.turn_seq
                    and attempt.is_simulation is event.is_simulation
                )
            )
        )
        request_fields = {
            "idempotency_key": receipt.idempotency_key,
            "expected_revision": receipt.expected_runtime_revision,
            "expected_live_wseq": receipt.expected_live_wseq,
            "error_code": error_code,
            "attempt_id": event.attempt_id if event is not None else None,
        }
        expected_request_hash = (
            evidence_ledger.technical_pause_request_hash(
                receipt.session_id, request_fields)
            if (
                event is not None
                and isinstance(error_code, str)
                and _TECHNICAL_PAUSE_ERROR_RE.fullmatch(error_code) is not None
            ) else None
        )
        cursor_wseq = (
            cursor.get("wseq") if isinstance(cursor, dict) else None)
        item_idx = (
            cursor.get("itemIdx") if isinstance(cursor, dict) else None)
        turn_idx = (
            cursor.get("turnIdx") if isinstance(cursor, dict) else None)
        cursor_position_valid = False
        if (
            event is not None
            and isinstance(item_idx, int)
            and not isinstance(item_idx, bool)
            and 0 <= item_idx < len(plan.items)
            and isinstance(turn_idx, int)
            and not isinstance(turn_idx, bool)
            and 0 <= turn_idx < len(plan.items[item_idx].turns)
        ):
            planned_item = plan.items[item_idx]
            planned_turn = planned_item.turns[turn_idx]
            cursor_position_valid = bool(
                planned_item.item_id == event.item_id
                and planned_turn.turn_seq == event.turn_seq
                and cursor.get("responseRole") == planned_turn.response_role
            )
        receipt_valid = bool(
            event is not None
            and event.id not in seen_events
            and event.session_id == receipt.session_id
            and event.event_type == "technical_pause"
            and attempt_binding_valid
            and isinstance(event.item_id, str)
            and bool(event.item_id)
            and isinstance(event.turn_seq, int)
            and not isinstance(event.turn_seq, bool)
            and event.turn_seq >= 1
            and (
                event.attempt_id is None
                or (
                    isinstance(event.attempt_id, int)
                    and not isinstance(event.attempt_id, bool)
                    and event.attempt_id >= 1
                )
            )
            and isinstance(payload, dict)
            and set(payload) == {"error_code"}
            and isinstance(error_code, str)
            and _TECHNICAL_PAUSE_ERROR_RE.fullmatch(error_code) is not None
            and isinstance(receipt.idempotency_key, str)
            and _TECHNICAL_PAUSE_KEY_RE.fullmatch(
                receipt.idempotency_key) is not None
            and _CHECKSUM_RE.fullmatch(receipt.request_hash or "")
            and expected_request_hash is not None
            and hmac.compare_digest(
                receipt.request_hash, expected_request_hash)
            and isinstance(receipt.expected_runtime_revision, int)
            and not isinstance(receipt.expected_runtime_revision, bool)
            and receipt.expected_runtime_revision >= 0
            and isinstance(receipt.expected_live_wseq, int)
            and not isinstance(receipt.expected_live_wseq, bool)
            and receipt.expected_live_wseq >= 0
            and isinstance(receipt.runtime_revision, int)
            and not isinstance(receipt.runtime_revision, bool)
            and receipt.runtime_revision == receipt.expected_runtime_revision + 1
            and isinstance(receipt.paused_cursor_wseq, int)
            and not isinstance(receipt.paused_cursor_wseq, bool)
            and receipt.paused_cursor_wseq > receipt.expected_live_wseq
            and isinstance(receipt.live_seq, int)
            and not isinstance(receipt.live_seq, bool)
            and receipt.live_seq >= 1
            and isinstance(cursor, dict)
            and cursor.get("sessionId") == receipt.session_id
            and "session_id" not in cursor
            and cursor.get("screen") == "paused"
            and cursor.get("recording") == "stopped"
            and cursor.get("selfStart") is False
            and "rawAudioId" not in cursor
            and cursor_position_valid
            and isinstance(cursor_wseq, int)
            and not isinstance(cursor_wseq, bool)
            and cursor_wseq == receipt.paused_cursor_wseq
        )
        if not receipt_valid:
            invalid += 1
            continue
        assert event is not None and event.id is not None
        seen_events.add(event.id)
        valid.append(event)
    for event in grouped["interactions"]:
        if (
            event.id is None
            or event.event_type != "technical_pause"
            or event.id in seen_events
            or event.id in receipt_event_ids
        ):
            continue
        attempt = (
            attempts_by_id.get(event.attempt_id)
            if event.attempt_id is not None else None
        )
        if (
            _automatic_failure_pause_valid(
                event,
                interactions_by_seq.get(event.event_seq - 1),
                attempt,
            )
            or _patient_rec_failure_pause_valid(
                event, expected_positions, week1_item_ids, failure_id_counts)
        ):
            seen_events.add(event.id)
            valid.append(event)
    # Anything outside the closed server-owned chains is a malformed record,
    # never a pause metric: either an atomic receipt failed validation or a bare
    # legacy label had neither an adjacent AI failure nor a device failure id.
    invalid += sum(
        row.event_type == "technical_pause" and row.id not in seen_events
        for row in grouped["interactions"]
    )
    worker_candidates: list[AutopilotControlEvent] = []
    worker_boundary_counts: dict[tuple[int, int, int], int] = defaultdict(int)
    for row in grouped["control_events"]:
        if row.event_type != "failure":
            continue
        try:
            payload = json.loads(row.payload_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("source") != "worker_exception":
            continue
        worker_candidates.append(row)
        if (
            isinstance(row.command_id, int)
            and not isinstance(row.command_id, bool)
        ):
            worker_boundary_counts[
                (row.command_id, row.control_generation, row.runner_generation)
            ] += 1
    valid_worker_pauses = 0
    for row in worker_candidates:
        boundary = (
            row.command_id,
            row.control_generation,
            row.runner_generation,
        )
        if (
            _worker_exception_control_pause_valid(row)
            and worker_boundary_counts.get(boundary) == 1
        ):
            valid_worker_pauses += 1
        else:
            invalid += 1
    return valid, valid_worker_pauses, invalid


def _canonical_control_payload(
    row: AutopilotControlEvent,
) -> dict[str, object] | None:
    try:
        payload = json.loads(row.payload_json)
        canonical = (
            autopilot_ledger.encode_control_event_payload(
                row.event_type, payload)
            if isinstance(payload, dict) else None
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if canonical == row.payload_json else None


def _worker_exception_control_pause_valid(
    row: AutopilotControlEvent,
) -> bool:
    payload = _canonical_control_payload(row)
    return bool(
        isinstance(payload, dict)
        and set(payload) == {"error_code", "source"}
        and payload.get("source") == "worker_exception"
        and payload.get("error_code") == row.reason_code
        and isinstance(row.reason_code, str)
        and _TECHNICAL_PAUSE_ERROR_RE.fullmatch(row.reason_code) is not None
        and isinstance(row.idempotency_key, str)
        and isinstance(row.command_id, int)
        and not isinstance(row.command_id, bool)
        and row.idempotency_key == autopilot_ledger.attempt_failure_event_key(
            row.session_id, row.command_id, row.reason_code)
        and row.session_id
        and row.event_seq >= 1
        and row.scope_key == "p0a_sim_first_single_v1"
        and row.control_generation >= 1
        and row.runner_generation >= 1
        and row.command_id >= 1
        and row.actor_type == "system"
        and row.actor_id is None
        and row.from_mode == "autonomous"
        and row.to_mode == "autonomous"
        and row.from_status == "processing_attempt"
        and row.to_status == "paused"
    )


def _takeover_predecessor_valid(
    takeover: AutopilotControlEvent,
    predecessor: AutopilotControlEvent | None,
) -> bool:
    """Verify the exact latest media-stop fact used by safe takeover.

    The mutation path accepts only the latest event in the same command and
    control/runner generation.  This projection mirrors those immutable control
    facts; it never treats a bare takeover label as proof of stopped media.
    """
    if (
        predecessor is None
        or predecessor.session_id != takeover.session_id
        or predecessor.event_seq + 1 != takeover.event_seq
        or predecessor.scope_key != takeover.scope_key
        or predecessor.control_generation != takeover.control_generation
        or predecessor.runner_generation != takeover.runner_generation
        or predecessor.command_id != takeover.command_id
        or predecessor.command_id is None
        or predecessor.from_mode != "autonomous"
        or predecessor.to_mode != "autonomous"
        or predecessor.to_status != takeover.from_status
    ):
        return False
    payload = _canonical_control_payload(predecessor)
    if payload is None:
        return False
    if predecessor.event_type == "drain_complete":
        return bool(
            takeover.from_status == "paused"
            and isinstance(predecessor.idempotency_key, str)
            and _DEVICE_DRAIN_EVENT_KEY_RE.fullmatch(
                predecessor.idempotency_key) is not None
            and predecessor.actor_type == "device"
            and predecessor.from_status == "paused"
            and predecessor.to_status == "paused"
            and payload == {"drained_command_id": takeover.command_id}
        )
    if predecessor.event_type == "failure":
        source = payload.get("source")
        base = bool(
            takeover.from_status in {"paused", "failed"}
            and predecessor.reason_code
            and payload.get("error_code") == predecessor.reason_code
            and set(payload) == {"error_code", "source"}
        )
        if not base:
            return False
        if source == "device_ack":
            return bool(
                isinstance(predecessor.idempotency_key, str)
                and _DEVICE_FAILURE_EVENT_KEY_RE.fullmatch(
                    predecessor.idempotency_key) is not None
                and predecessor.actor_type == "device"
                and predecessor.from_status in {
                    "waiting_tts", "waiting_recording"}
            )
        return bool(
            isinstance(predecessor.idempotency_key, str)
            and isinstance(predecessor.command_id, int)
            and not isinstance(predecessor.command_id, bool)
            and isinstance(predecessor.reason_code, str)
            and predecessor.idempotency_key
            == autopilot_ledger.attempt_failure_event_key(
                predecessor.session_id,
                predecessor.command_id,
                predecessor.reason_code,
            )
            and source in {"attempt_processing", "worker_exception"}
            and predecessor.actor_type == "system"
            and predecessor.from_status == "processing_attempt"
        )
    if predecessor.event_type == "scope_complete":
        completed_seq = payload.get("completed_command_seq")
        return bool(
            takeover.from_status == "scope_completed"
            and isinstance(predecessor.idempotency_key, str)
            and _SCOPE_COMPLETE_EVENT_KEY_RE.fullmatch(
                predecessor.idempotency_key) is not None
            and predecessor.actor_type == "system"
            and predecessor.from_status == "waiting_tts"
            and predecessor.to_status == "scope_completed"
            and set(payload) == {"completed_command_seq"}
            and isinstance(completed_seq, int)
            and not isinstance(completed_seq, bool)
            and completed_seq >= 1
        )
    return False


def _valid_takeover_count(grouped: dict[str, list]) -> tuple[int, int]:
    valid = invalid = 0
    events_by_seq = {
        row.event_seq: row for row in grouped["control_events"]
    }
    for row in grouped["control_events"]:
        if row.event_type != "takeover":
            continue
        payload = _canonical_control_payload(row)
        command_id_valid = (
            isinstance(row.command_id, int)
            and not isinstance(row.command_id, bool)
            and row.command_id >= 1
        )
        if (
            isinstance(payload, dict)
            and payload.get("reason_code") == row.reason_code
            and payload.get("source") == "account_takeover_endpoint"
            and isinstance(payload.get("expected_revision"), int)
            and not isinstance(payload.get("expected_revision"), bool)
            and payload["expected_revision"] >= 0
            and row.scope_key == "p0a_sim_first_single_v1"
            and row.control_generation >= 1
            and row.runner_generation >= 1
            and row.event_seq >= 2
            and command_id_valid
            and isinstance(row.idempotency_key, str)
            and _AUTOPILOT_IDEMPOTENCY_KEY_RE.fullmatch(
                row.idempotency_key) is not None
            and row.actor_type == "researcher"
            and bool((row.actor_id or "").strip())
            and row.reason_code == "researcher_explicit_takeover"
            and row.from_mode == "autonomous"
            and row.to_mode == "manual"
            and row.from_status == row.to_status
            and row.from_status in {"paused", "scope_completed", "failed"}
            and _takeover_predecessor_valid(
                row, events_by_seq.get(row.event_seq - 1))
        ):
            valid += 1
        else:
            invalid += 1
    # The legacy manual InteractionEvent route is not a valid production
    # takeover path; surface it as malformed evidence instead of counting it.
    invalid += sum(
        row.event_type == "researcher_takeover"
        for row in grouped["interactions"]
    )
    return valid, invalid


def _project_session(
    session: TrainSession,
    plan: runtime.SessionPlan,
    grouped: dict[str, list],
    *,
    data_classification: str,
    blob_index: dict[str, Path],
) -> tuple[list[TurnQualityEvidence], int, int]:
    expected = _expected_positions(plan)
    items_by_db_id = {
        row.id: row for row in grouped["items"] if row.id is not None
    }
    turns_by_position: dict[tuple[str, int], TurnEvent] = {}
    for row in grouped["turns"]:
        item = items_by_db_id.get(row.item_event_id)
        if item is not None:
            turns_by_position[(item.item_id, row.turn_seq)] = row
    attempts_by_position: dict[tuple[str, int], list[AttemptEvent]] = defaultdict(list)
    attempts_by_id: dict[int, AttemptEvent] = {}
    for row in grouped["attempts"]:
        attempts_by_position[(row.item_id, row.turn_seq)].append(row)
        if row.id is not None:
            attempts_by_id[row.id] = row
    for attempts in attempts_by_position.values():
        attempts.sort(key=lambda row: (row.attempt_seq, row.id or 0))
    audio_by_id = {row.raw_audio_id: row for row in grouped["audios"]}
    receipts_by_audio: dict[str, list[AudioCaptureReceipt]] = defaultdict(list)
    for row in grouped["receipts"]:
        receipts_by_audio[row.raw_audio_id].append(row)
    revisions_by_turn: dict[int, list[TurnConfirmationRevision]] = defaultdict(list)
    for row in grouped["revisions"]:
        revisions_by_turn[row.turn_id].append(row)
    verification_cache: dict[str, bool] = {}
    week1_item_ids = _week1_relationship_item_ids(session)

    pause_counts: dict[tuple[str, int], int] = defaultdict(int)
    takeover_counts: dict[tuple[str, int], int] = defaultdict(int)
    unpositioned_pause = 0
    structural_invalid_records = 0
    lineage_invalid_positions: set[tuple[str, int]] = set()
    (
        valid_pause_events,
        valid_unpositioned_control_pauses,
        invalid_pause_records,
    ) = _valid_pause_events(
        grouped, plan, week1_item_ids=week1_item_ids)
    unpositioned_pause += valid_unpositioned_control_pauses
    structural_invalid_records += invalid_pause_records
    valid_takeovers, invalid_takeovers = _valid_takeover_count(grouped)
    structural_invalid_records += invalid_takeovers
    for row in valid_pause_events:
        key = (row.item_id or "", row.turn_seq or -1)
        positioned = row.item_id is not None and row.turn_seq is not None and key in expected
        relationship_positioned = bool(
            row.turn_seq is None and row.item_id in week1_item_ids)
        has_any_position = row.item_id is not None or row.turn_seq is not None
        if not positioned and not relationship_positioned and has_any_position:
            structural_invalid_records += 1
            continue
        if positioned:
            pause_counts[key] += 1
        else:
            unpositioned_pause += 1

    ordered_positions = list(expected)
    if ordered_positions:
        # Session-level events without a frozen position attach once, never once
        # per expected turn (which would multiply them by N).
        first = ordered_positions[0]
        pause_counts[first] += unpositioned_pause
        takeover_counts[first] += valid_takeovers

    evidence: list[TurnQualityEvidence] = []
    dimensions = QualityDimensions(data_classification=data_classification)
    for key in ordered_positions:
        attempts = attempts_by_position.get(key, [])
        audio_results = [
            _audio_is_verified(
                session, attempt, audio_by_id, receipts_by_audio,
                blob_index,
                verification_cache)
            for attempt in attempts
        ]
        audio_evidenced = all(audio_results) if attempts else None
        attempt_evidence = tuple(
            AttemptQualityEvidence(
                prompt_level=row.prompt_level,
                processing_status=_enum_value(row.processing_status),
                latency_ms=_attempt_latency_ms(
                    row,
                    audio_by_id.get(row.raw_audio_id)
                    if audio_verified else None,
                ),
            )
            for row, audio_verified in zip(attempts, audio_results, strict=True)
        )
        turn = turns_by_position.get(key)
        preferred = (
            attempts_by_id.get(turn.source_attempt_id)
            if turn is not None and turn.source_attempt_id is not None
            else None
        )
        completed_judged = [
            row for row in attempts
            if _enum_value(row.processing_status) == "completed"
            and (row.operational_answer_type or "").strip()
        ]
        selected = (
            preferred
            if preferred in completed_judged
            else completed_judged[-1] if completed_judged else None
        )
        if not attempts:
            ai_attempted: bool | None = False
            ai_judged: bool | None = False
        elif audio_evidenced is not True:
            ai_attempted = None
            ai_judged = None
        else:
            ai_attempted = True
            if selected is not None:
                ai_judged = True
            elif any(
                    _enum_value(row.processing_status) == "completed"
                    for row in attempts):
                ai_judged = None
            else:
                ai_judged = False
        ai_prediction = (
            _ai_binary(selected.operational_answer_type)
            if ai_judged is True and selected is not None
            else None
        )

        asr_reviewed: bool | None = False
        asr_corrected: bool | None = None
        human_truth_locked: bool | None = False
        human_truth_correct: bool | None = None
        source_valid = False
        ledger_valid = False
        if turn is not None:
            source = attempts_by_id.get(turn.source_attempt_id or -1)
            source_valid = _turn_source_valid(
                session, key[0], turn, source,
                audio_by_id=audio_by_id,
                receipts_by_audio=receipts_by_audio,
                blob_index=blob_index,
                verification_cache=verification_cache,
            )
            if turn.source_attempt_id is not None and not source_valid:
                lineage_invalid_positions.add(key)
            if turn.confirmation_revision > 0:
                ledger_valid = _confirmation_ledger_valid(
                    turn, revisions_by_turn, session_id=session.session_id)
                if ledger_valid and source_valid:
                    asr_reviewed = True
                    asr_corrected = (
                        _normalized_text(turn.asr_text)
                        != _normalized_text(turn.confirmed_response_text)
                    )
                else:
                    asr_reviewed = None
                    if not ledger_valid:
                        lineage_invalid_positions.add(key)
            if turn.score_locked:
                score_lineage_valid = bool(
                    source_valid
                    and ledger_valid
                    and (turn.reviewer_id or "").strip()
                    and _same_number(turn.reviewed_score, turn.element_value)
                    and turn.reviewed_score is not None
                )
                if score_lineage_valid:
                    human_truth_locked = True
                    if float(turn.reviewed_score) == 1.0:
                        human_truth_correct = True
                    elif float(turn.reviewed_score) == 0.0:
                        human_truth_correct = False
                else:
                    human_truth_locked = None
                    lineage_invalid_positions.add(key)

        evidence.append(TurnQualityEvidence(
            dimensions=dimensions,
            eligible=True,
            attempts=attempt_evidence,
            audio_evidenced=audio_evidenced,
            ai_attempted=ai_attempted,
            ai_judged=ai_judged,
            ai_predicted_correct=ai_prediction,
            asr_reviewed=asr_reviewed,
            asr_corrected=asr_corrected,
            human_truth_locked=human_truth_locked,
            human_truth_correct=human_truth_correct,
            technical_pause_count=pause_counts.get(key, 0),
            researcher_takeover_count=takeover_counts.get(key, 0),
            operational_score=(
                selected.operational_score if selected is not None else None),
        ))

    if not ordered_positions and (unpositioned_pause or valid_takeovers):
        evidence.append(TurnQualityEvidence(
            dimensions=dimensions,
            eligible=False,
            attempts=(),
            audio_evidenced=None,
            ai_attempted=False,
            ai_judged=False,
            asr_reviewed=False,
            human_truth_locked=False,
            technical_pause_count=unpositioned_pause,
            researcher_takeover_count=valid_takeovers,
        ))
    return (
        evidence,
        structural_invalid_records,
        len(lineage_invalid_positions),
    )


def _unknown_sum(reason_counts: dict[str, int], *names: str) -> int:
    return sum(reason_counts.get(name, 0) for name in names)


def _released_payload(
    *,
    data_classification: str,
    visibility_scope: str,
    generated_at: datetime,
    threshold: _Threshold,
    distinct_patients: int,
    visible_sessions: int,
    included_sessions: int,
    source_turns: int,
    evidence: list[TurnQualityEvidence],
    restricted_sessions: int,
    classification_bad_sessions: int,
    protocol_binding_invalid_sessions: int,
    structural_invalid_evidence_records: int,
    lineage_invalid_turns: int,
) -> dict[str, object]:
    dashboard = aggregate_ai_quality(
        evidence,
        generated_at=generated_at,
        group_by=("data_classification",),
        trusted_low_cardinality_values={},
    )
    core = dashboard.rows[0] if dashboard.rows else None
    if core is None:
        operational = OperationalMetrics(
            eligible_turns=0,
            ai_attempted_turns=0,
            ai_judged_turns=0,
            asr_reviewed_turns=0,
            asr_corrected_turns=0,
            total_attempts=0,
            prompt_level_0_count=0,
            prompt_level_1_count=0,
            prompt_level_2_count=0,
            prompt_level_3_count=0,
            technical_failure_attempts=0,
            technical_pause_count=0,
            researcher_takeover_count=0,
            latency_sample_count=0,
            latency_p50_ms=None,
            latency_p95_ms=None,
        )
        truth = ResearchTruthMetrics(0, 0, 0, 0, 0)
        coverage_values = {
            "audio_evidenced_turns": 0,
            "attempts_observed": 0,
            "prompt_level_known_attempts": 0,
            "processing_status_known_attempts": 0,
            "latency_known_attempts": 0,
            "ai_attempt_status_known_turns": 0,
            "ai_judgement_status_known_turns": 0,
            "asr_review_status_known_turns": 0,
            "human_truth_locked_turns": 0,
            "binary_eligible_reviewed_decisions": 0,
            "binary_excluded_decisions": 0,
        }
    else:
        operational = core.operational
        truth = core.research_truth
        internal = core.coverage
        binary_eligible = internal.binary_comparison_eligible_turns
        coverage_values = {
            "audio_evidenced_turns": internal.audio_evidenced_turns,
            "attempts_observed": internal.attempts_observed,
            "prompt_level_known_attempts": internal.prompt_level_known_attempts,
            "processing_status_known_attempts": (
                internal.processing_status_known_attempts),
            "latency_known_attempts": internal.latency_known_attempts,
            "ai_attempt_status_known_turns": (
                internal.ai_attempt_status_known_turns),
            "ai_judgement_status_known_turns": (
                internal.ai_judgement_status_known_turns),
            "asr_review_status_known_turns": (
                internal.asr_review_status_known_turns),
            "human_truth_locked_turns": internal.human_truth_locked_turns,
            "binary_eligible_reviewed_decisions": binary_eligible,
            "binary_excluded_decisions": max(
                internal.human_truth_locked_turns - binary_eligible, 0),
        }

    diagnostics = {
        "restricted_or_withdrawn_sessions": restricted_sessions,
        "classification_inconsistent_sessions": classification_bad_sessions,
        "protocol_binding_invalid_sessions": protocol_binding_invalid_sessions,
        "structural_invalid_evidence_records": (
            structural_invalid_evidence_records),
        "lineage_invalid_turns": lineage_invalid_turns,
        "audio_evidence_unavailable_turns": sum(
            row.audio_evidenced is not True for row in evidence),
        "ai_attempt_status_unknown_turns": sum(
            row.ai_attempted is None for row in evidence),
        "ai_judgement_status_unknown_turns": sum(
            row.ai_judged is None for row in evidence),
        "asr_review_status_unknown_turns": sum(
            row.asr_reviewed is None for row in evidence),
        "human_truth_unavailable_turns": sum(
            row.human_truth_locked is not True
            or row.human_truth_correct is None
            for row in evidence),
        "binary_prediction_unavailable_turns": sum(
            row.ai_judged is True and row.ai_predicted_correct is None
            for row in evidence),
        "latency_unavailable_attempts": sum(
            attempt.latency_ms is None
            or isinstance(attempt.latency_ms, bool)
            or not isinstance(attempt.latency_ms, (int, float))
            or not math.isfinite(attempt.latency_ms)
            or attempt.latency_ms < 0
            for row in evidence
            for attempt in (row.attempts or ())
        ),
    }
    all_coverage = {
        "visible_sessions": visible_sessions,
        "included_sessions": included_sessions,
        "source_turns": source_turns,
        **coverage_values,
    }
    operational_payload = operational.to_dict()
    # The current automatic driver records attempts only at prompt contexts
    # 0..2. Level 3 is a terminal TTS tell-answer action and has no durable
    # bedside presentation receipt in this legacy evidence projection. A zero
    # would therefore be a false claim; keep it explicitly unknown.
    operational_payload["prompt_level_3_count"] = None
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso_utc(generated_at),
        "privacy": {
            "aggregation_only": True,
            "contains_patient_identifiers": False,
            "contains_audio": False,
            "contains_transcripts": False,
        },
        "rows": [{
            "visibility_scope": visibility_scope,
            "dimensions": _dimensions(data_classification),
            "suppression": {
                "status": (
                    "released" if data_classification == "research"
                    else "not_applicable"),
                "reason": None,
                "minimum_distinct_subjects": (
                    threshold.minimum if data_classification == "research"
                    else None),
                # Simulation has no research small-cell contract.  Keep this
                # field null rather than publishing synthetic cardinality.
                "distinct_subjects": (
                    distinct_patients if data_classification == "research"
                    else None),
            },
            "coverage": all_coverage,
            "diagnostics": {
                "status": "partial" if any(diagnostics.values()) else "complete",
                "reason_counts": diagnostics,
            },
            "operational": operational_payload,
            "research_truth": truth.to_dict(),
        }],
    }


def build_ai_quality_dashboard(
    s: DBSession,
    *,
    actor_id: str,
    actor_role: str,
    data_classification: Literal["research", "simulation"],
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Build exactly one authorized, overall-only v2 aggregate partition."""
    now = generated_at or datetime.now(timezone.utc)
    visibility_scope = _visibility_scope(actor_role)
    threshold = _parse_threshold()
    if data_classification == "research":
        # A total-subject threshold alone cannot protect sparse metric cells or
        # repeated-query differencing. Until a durable frozen cohort/release
        # epoch and complementary per-cell suppression exist, research metrics
        # remain wholly suppressed before reading any session or evidence.
        reason = (
            "research_threshold_unconfigured"
            if threshold.status == "unconfigured"
            else "research_threshold_invalid"
            if threshold.status == "invalid"
            else "research_release_not_frozen"
        )
        return _suppressed_payload(
            data_classification=data_classification,
            visibility_scope=visibility_scope,
            generated_at=now,
            reason=reason,
            minimum=None,
        )

    _begin_stable_read_snapshot(s)
    visible = _visible_sessions(
        s,
        actor_id=actor_id,
        actor_role=actor_role,
        data_classification=data_classification,
    )
    preliminary, restricted, classification_bad = _preproject_sessions(
        s, visible, data_classification=data_classification)

    plans, definition_bad = _plans_for_sessions(preliminary)
    candidate = [row for row in preliminary if row.session_id not in definition_bad]

    _preflight_evidence_budget(
        s, [row.session_id for row in candidate])
    loaded = _load_evidence_rows(s, [row.session_id for row in candidate])
    _enforce_loaded_evidence_budget(loaded)
    _release_stable_read_snapshot(s)
    try:
        blob_index = audio_store.index_blobs(
            row.raw_audio_id for row in loaded.audios)
    except (ValueError, audio_store.AudioStoreIntegrityError, OSError):
        # Storage topology details never cross the aggregate boundary. A bad
        # root, symlink or duplicate makes physical evidence unavailable for
        # this whole request, while still avoiding per-attempt directory scans.
        blob_index = {}
    evidence_classification_bad = _classification_inconsistent_sessions(
        loaded,
        expected_simulation=data_classification == "simulation",
        data_classification=data_classification,
    )
    classification_bad.update(evidence_classification_bad)
    grouped = _group_evidence(loaded)
    structural_bad: set[str] = set()
    structural_invalid_records = 0
    for row in candidate:
        invalid = _structural_evidence_invalid(
            plans[row.session_id], grouped[row.session_id])
        if invalid:
            structural_bad.add(row.session_id)
            structural_invalid_records += invalid
    excluded_after_projection = evidence_classification_bad | structural_bad
    included = [
        row for row in candidate if row.session_id not in excluded_after_projection
    ]
    distinct = _distinct_patients(included)

    evidence: list[TurnQualityEvidence] = []
    source_turns = 0
    lineage_invalid_turns = 0
    for row in included:
        plan = plans[row.session_id]
        source_turns += plan.total_turns()
        projected, invalid_records, invalid_lineage = _project_session(
            row,
            plan,
            grouped[row.session_id],
            data_classification=data_classification,
            blob_index=blob_index,
        )
        evidence.extend(projected)
        structural_invalid_records += invalid_records
        lineage_invalid_turns += invalid_lineage
    return _released_payload(
        data_classification=data_classification,
        visibility_scope=visibility_scope,
        generated_at=now,
        threshold=threshold,
        distinct_patients=distinct,
        visible_sessions=len(visible),
        included_sessions=len(included),
        source_turns=source_turns,
        evidence=evidence,
        restricted_sessions=len(restricted),
        classification_bad_sessions=len(classification_bad),
        protocol_binding_invalid_sessions=len(definition_bad),
        structural_invalid_evidence_records=structural_invalid_records,
        lineage_invalid_turns=lineage_invalid_turns,
    )
