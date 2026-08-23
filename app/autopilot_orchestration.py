"""Crash-recoverable P0a attempt orchestration boundaries.

This module never calls a provider and never commits.  It derives the only valid
``AttemptProcessIn`` facts from the persisted capture chain, stages fail-closed
autopilot control facts, and owns a small in-process submission de-duplicator.  The
FastAPI adapter supplies the existing authoritative attempt processor and owns every
transaction.  A GET recovery trigger can safely submit the same session again after
a process restart because database leases and idempotency remain the real authority.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import re
import threading
from typing import Callable, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, update
from sqlmodel import Session, select

from . import (autopilot_ledger, autopilot_plan_profiles, autopilot_positions,
               autopilot_service, content, evidence_ledger, runtime)
from .autopilot_contract import TtsCommandPayload
from .enums import AudioStatus
from .models import (
    AttemptCaptureProcessing,
    AttemptEvent,
    AudioAssetRow,
    AudioCaptureReceipt,
    AutopilotControlEvent,
    InteractionEvent,
    LiveState,
    Patient,
    RuntimeCommand,
    RuntimeCommandAck,
    Session as TrainSession,
    SessionAutopilotState,
    SessionRuntimeState,
    TtsServeEvidence,
    VisitPlan,
)


_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9._-]{2,63}$")
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="p0a-attempt")
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: dict[str, Future[object]] = {}


class AutopilotOrchestrationError(RuntimeError):
    """Stable provider-free failure; callers must roll back the transaction."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AuthoritativeAttemptInput(BaseModel):
    """Closed input reconstructed from capture proof and the frozen plan only."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=160)
    turn_seq: int = Field(ge=1)
    response_role: str = Field(min_length=1, max_length=64)
    raw_audio_id: str = Field(
        min_length=1, max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$",
    )
    prompt_level: int = Field(ge=0, le=2)
    cue_type: str | None = Field(default=None, min_length=1, max_length=64)
    duration_seconds: float = Field(ge=0, le=600)


def _fail(code: str, message: str) -> None:
    raise AutopilotOrchestrationError(code, message)


def _event_key(session_id: str, command_id: int, error_code: str) -> str:
    return autopilot_ledger.attempt_failure_event_key(
        session_id, command_id, error_code)


def _next_control_event_seq(db: Session, session_id: str) -> int:
    latest = db.exec(select(AutopilotControlEvent).where(
        AutopilotControlEvent.session_id == session_id,
    ).order_by(AutopilotControlEvent.event_seq.desc())).first()
    return (latest.event_seq if latest is not None else 0) + 1


def _frozen_attempt_context(
    train_session: TrainSession,
    record: RuntimeCommand,
    bank: content.ItemBank,
    protocol: dict,
) -> tuple[str, str | None]:
    event_line = getattr(train_session.event_line, "value", train_session.event_line)
    try:
        if (
            train_session.autopilot_profile_version_id is not None
            or train_session.autopilot_profile_definition_digest is not None
        ):
            plan = autopilot_plan_profiles.resolve_for_session(
                train_session, bank=bank, protocol=protocol).session_plan
        else:
            plan = runtime.build_session_plan(
                bank, train_session.week_no, str(event_line))
    except (ValueError, autopilot_plan_profiles.PlanProfileError) as exc:
        raise AutopilotOrchestrationError(
            "autopilot_attempt_plan_invalid", "P0a 冻结计划不可用") from exc
    positions = autopilot_positions.plan_positions(plan)
    try:
        position = autopilot_positions.find_position(
            positions, item_id=record.item_id, turn_seq=record.turn_seq)
    except ValueError as exc:
        raise AutopilotOrchestrationError(
            "autopilot_attempt_plan_mismatch", "录音命令不属于冻结计划") from exc
    interaction_package: dict | None = None
    if position.task_type in {"双要素", "多要素"}:
        try:
            interaction_package = autopilot_service._load_interaction_package(  # noqa: SLF001
                train_session, bank, protocol)
        except autopilot_service.AutopilotServiceError as exc:
            raise AutopilotOrchestrationError(exc.code, exc.message) from exc
    gap = autopilot_positions.readiness_gap(
        bank, position, interaction_package=interaction_package)
    if gap is not None:
        _fail(gap.code, gap.detail)
    if position.task_type in {"双要素", "多要素"}:
        # Interaction turns: recording #1 is (L0,a1); recording #2 exists only
        # when the frozen turn carries a rerecord branch, is (L1,a2), and its
        # cue_type is the closed package-retry literal — bank cues never apply.
        if record.prompt_level == 0:
            return position.response_role, None
        if record.prompt_level != 1:
            _fail("autopilot_attempt_plan_mismatch", "交互环节录音提示等级非法")
        turn = autopilot_positions.interaction_turn(
            interaction_package, position)
        if not isinstance(turn, dict) or turn.get("max_recordings") != 2:
            _fail(
                "autopilot_attempt_plan_mismatch",
                "该交互环节没有第二次录音",
            )
        return position.response_role, autopilot_service.INTERACTION_CUE_TYPE
    if (position.task_type != "单要素"
            or position.response_role != "命名"):
        _fail("autopilot_attempt_plan_mismatch", "录音命令与 P0a 冻结环节不一致")
    if record.prompt_level == 0:
        return position.response_role, None
    if record.prompt_level not in {1, 2}:
        _fail("autopilot_attempt_plan_mismatch", "P0a 录音命令提示等级非法")
    rows = [row for row in bank.single_element
            if row.get("item_id") == position.item_id]
    if len(rows) != 1:
        _fail("autopilot_attempt_plan_mismatch", "P0a 题库与冻结位置不一致")
    cues = rows[0].get("cues")
    cue = cues.get(str(record.prompt_level)) if isinstance(cues, dict) else None
    cue_type = cue.get("cue_type") if isinstance(cue, dict) else None
    if not isinstance(cue_type, str) or not cue_type.strip():
        _fail("autopilot_attempt_plan_invalid", "P0a 冻结提示缺少 cue_type")
    return position.response_role, cue_type.strip()


class FrozenWorkerTarget(BaseModel):
    """Immutable identity of the one P0a attempt a worker is bound to.

    Must be captured once, in the same locked snapshot as (and immediately
    alongside) :func:`derive_authoritative_attempt_input`, before any provider
    I/O begins. Every ``control_plane="autopilot_worker"`` failure path must
    carry this exact target so a worker that blocked on provider I/O and
    returns late can never mutate a different, newer logical attempt that a
    faster worker has since taken over and completed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    record_command_id: int
    raw_audio_id: str = Field(min_length=1)
    control_generation: int = Field(ge=1)
    runner_generation: int = Field(ge=1)


def _lock_pending_command(
    db: Session, *, session_id: str,
) -> tuple[SessionAutopilotState, RuntimeCommand]:
    """Lock and validate the one currently-pending P0a attempt command.

    Shared by :func:`derive_authoritative_attempt_input` and
    :func:`derive_worker_target` so both read the identical invariant from the
    identical locked rows within the caller's transaction.
    """
    state = db.exec(select(SessionAutopilotState).where(
        SessionAutopilotState.session_id == session_id,
    ).with_for_update()).first()
    if (state is None or state.scope_key != autopilot_service.P0A_SCOPE_KEY
            or state.mode != "autonomous" or state.status != "processing_attempt"
            or state.current_command_id is None):
        _fail("autopilot_attempt_not_pending", "当前没有待处理的 P0a attempt")
    record = db.exec(select(RuntimeCommand).where(
        RuntimeCommand.id == state.current_command_id,
        RuntimeCommand.session_id == session_id,
    ).with_for_update()).first()
    if (record is None or record.kind != "record" or record.state != "succeeded"
            or record.scope_key != state.scope_key
            or record.control_generation != state.control_generation
            or record.runner_generation != state.runner_generation):
        _fail("autopilot_attempt_capture_invalid", "当前 attempt 缺少已成功录音命令")
    return state, record


def derive_worker_target(
    db: Session, *, session_id: str,
) -> FrozenWorkerTarget:
    """Freeze the immutable worker target from the current locked P0a snapshot.

    Callers must invoke this in the same transaction as, and never after
    releasing the locks taken by, :func:`derive_authoritative_attempt_input` —
    before any provider I/O begins.
    """
    state, record = _lock_pending_command(db, session_id=session_id)
    return FrozenWorkerTarget(
        session_id=session_id,
        record_command_id=record.id,
        raw_audio_id=record.expected_raw_audio_id,
        control_generation=state.control_generation,
        runner_generation=state.runner_generation,
    )


def worker_target_still_current(db: Session, target: FrozenWorkerTarget) -> bool:
    """Read-only: is this frozen target still exactly the session's live one?

    Verifies every field of ``target`` — including the exact raw audio, not
    just the command id/generations — against the current row. Used to skip a
    stale/lost-claim worker's redundant route attempt, and to reject a
    mismatched (forged or lagged) target before any provider I/O in
    :func:`autopilot_service` callers. Never used to authorize a mutation by
    itself — :func:`stage_processing_failure`'s own CAS remains the sole
    mutation gate, and ``route_completed_attempt`` keeps its own independent
    generation/state CAS regardless of this check.
    """
    state = db.exec(select(SessionAutopilotState).where(
        SessionAutopilotState.session_id == target.session_id,
    )).first()
    if state is None or state.scope_key != autopilot_service.P0A_SCOPE_KEY:
        return False
    if (state.status != "processing_attempt"
            or state.current_command_id != target.record_command_id
            or state.control_generation != target.control_generation
            or state.runner_generation != target.runner_generation):
        return False
    record = db.exec(select(RuntimeCommand).where(
        RuntimeCommand.id == target.record_command_id,
        RuntimeCommand.session_id == target.session_id,
    )).first()
    return record is not None and record.expected_raw_audio_id == target.raw_audio_id


def derive_authoritative_attempt_input(
    db: Session,
    *,
    session_id: str,
    now: datetime | None = None,
) -> AuthoritativeAttemptInput:
    """Lock and derive one process request from the current succeeded capture.

    No caller-supplied item, role, audio, prompt, cue or duration is accepted.
    The returned value is safe to pass to the existing authoritative processor only
    after this short read transaction is ended, so provider I/O never holds these
    control locks.
    """
    observed_at = (
        autopilot_service._utc_naive(now)  # noqa: SLF001 - sibling domain clock
        if now is not None else autopilot_service._utc_now_naive()  # noqa: SLF001
    )
    bank = autopilot_service._session_week_bank(db, session_id)  # noqa: SLF001
    protocol = content.load_autopilot_protocol(
        content.CONTENT_DIR / "autopilot_protocol_v1.json")
    _state, record = _lock_pending_command(db, session_id=session_id)
    try:
        gate = autopilot_service._require_gate(  # noqa: SLF001 - same bounded domain
            db,
            session_id,
            bank=bank,
            protocol=protocol,
            now=observed_at,
            position_item_id=record.item_id,
            position_turn_seq=record.turn_seq,
        )
    except autopilot_service.AutopilotServiceError as exc:
        raise AutopilotOrchestrationError(exc.code, exc.message) from exc
    try:
        autopilot_service._validate_command_identity(  # noqa: SLF001
            record, gate.selected)
        # Repeat binding is checked here, before any capture claim is taken, so
        # an unbound or drifted command leaves even the claim generation alone.
        autopilot_service._require_command_repeat_binding(  # noqa: SLF001
            record, gate.train_session)
    except autopilot_service.AutopilotServiceError as exc:
        raise AutopilotOrchestrationError(exc.code, exc.message) from exc
    try:
        proof = autopilot_ledger.verify_record_capture_for_attempt(db, record.id)
    except autopilot_ledger.AutopilotProofError as exc:
        raise AutopilotOrchestrationError(
            "autopilot_attempt_capture_invalid", "P0a attempt 采集证据链不完整") from exc
    classification_pair = (
        gate.train_session.is_simulation,
        gate.train_session.data_classification,
    )
    if classification_pair == (True, "simulation"):
        subject_consistent = gate.patient.is_simulation_subject is True
    elif classification_pair == (False, "research"):
        subject_consistent = gate.patient.is_simulation_subject is not True
    else:
        subject_consistent = False
    if not subject_consistent:
        _fail(
            "autopilot_attempt_boundary_invalid",
            "attempt orchestration 场次分类与受试者身份不一致")
    if proof.attempt_seq != proof.prompt_level + 1:
        _fail("autopilot_attempt_sequence_invalid", "capture attempt_seq 与提示序列不单调")

    response_role, cue_type = _frozen_attempt_context(
        gate.train_session, record, bank, protocol)
    derived = AuthoritativeAttemptInput(
        item_id=proof.item_id,
        turn_seq=proof.turn_seq,
        response_role=response_role,
        raw_audio_id=proof.raw_audio_id,
        prompt_level=proof.prompt_level,
        cue_type=cue_type,
        duration_seconds=proof.duration_seconds,
    )

    existing = db.exec(select(AttemptEvent).where(
        AttemptEvent.raw_audio_id == proof.raw_audio_id,
    ).with_for_update()).first()
    if existing is not None:
        exact = (
            existing.session_id == session_id,
            existing.item_id == derived.item_id,
            existing.turn_seq == derived.turn_seq,
            existing.response_role == derived.response_role,
            existing.raw_audio_id == derived.raw_audio_id,
            existing.prompt_level == derived.prompt_level,
            existing.cue_type == derived.cue_type,
            existing.duration_seconds == derived.duration_seconds,
            existing.attempt_seq == proof.attempt_seq,
            existing.is_simulation == gate.train_session.is_simulation,
        )
        if not all(exact):
            _fail("autopilot_attempt_existing_conflict", "已有 attempt 与 capture proof 不一致")
    else:
        latest_seq = db.exec(select(func.max(AttemptEvent.attempt_seq)).where(
            AttemptEvent.session_id == session_id,
            AttemptEvent.item_id == proof.item_id,
            AttemptEvent.turn_seq == proof.turn_seq,
        )).one()
        if int(latest_seq or 0) + 1 != proof.attempt_seq:
            _fail("autopilot_attempt_sequence_invalid", "服务端 attempt 序列与 capture proof 不一致")
    return derived


LEGACY_RECOVERY_INVALID = "autopilot_legacy_recovery_invalid"
# The three durable stages of one legacy recovery.  Each is separated from the
# next by a commit, so a crash or a lease takeover resumes at the stage the
# database actually reached instead of re-running a provider call.
LEGACY_STAGE_ASR = "needs_asr"
LEGACY_STAGE_JUDGEMENT = "needs_judgement"
LEGACY_STAGE_PAUSE = "needs_pause"


class LegacyRepeatRecoveryTarget(BaseModel):
    """The one pre-protocol capture a legacy recovery worker is bound to.

    Structural equality is the fence.  Ids and generations alone are far too
    weak: while a provider call is in flight the whole evidence chain underneath
    them could be rewritten — another recording's bytes, another prompt's
    payload, a re-issued ACK — and a re-derived target would still look
    self-consistent.  ``evidence_fingerprint`` therefore covers every immutable
    fact of the chain, so "the same target" means the same evidence, not merely
    the same row ids.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    # Frozen as a plain string so a provider wrapper never re-reads it from an
    # expired ORM row that could lazily reload a patient id changed mid-call.
    patient_id: str = Field(min_length=1)
    record_command_id: int
    source_command_id: int
    capture_id: int
    control_generation: int = Field(ge=1)
    runner_generation: int = Field(ge=1)
    state_revision: int = Field(ge=0)
    audio_checksum: str = Field(min_length=64, max_length=64)
    audio_byte_count: int = Field(ge=1)
    base_evidence_fingerprint: str = Field(min_length=64, max_length=64)
    stage_evidence_fingerprint: str = Field(min_length=64, max_length=64)
    attempt_input: AuthoritativeAttemptInput


class LegacyRepeatRecovery(NamedTuple):
    """A proved legacy target plus the durable stage it has actually reached."""

    target: LegacyRepeatRecoveryTarget
    stage: str
    attempt_id: int | None


def _legacy_fail(detail: str) -> None:
    _fail(LEGACY_RECOVERY_INVALID, detail)


def _row_facts(row: object, names: tuple[str, ...]) -> dict:
    return {name: getattr(row, name) for name in names}


def _canonical_digest(facts: object) -> str:
    return hashlib.sha256(json.dumps(
        facts, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=str).encode("utf-8")).hexdigest()


def _legacy_marker_capture(
    db: Session, record: RuntimeCommand, *, lock: bool,
) -> AttemptCaptureProcessing:
    """The record's single capture row, re-proved as a legacy-marker row.

    Resolved from both directions every time it is needed — by record command
    and again by ``raw_audio_id`` — so a crash re-entry after ASR or after the
    Attempt was written re-establishes the marker context from the media itself
    rather than carrying a stale in-memory classification forward.
    """
    query = select(AttemptCaptureProcessing).where(
        AttemptCaptureProcessing.record_command_id == record.id)
    rows = list(db.exec(query.with_for_update() if lock else query))
    if len(rows) != 1:
        _legacy_fail("当前录音命令没有唯一采集处理行")
    capture = rows[0]
    by_audio = db.exec(select(AttemptCaptureProcessing).where(
        AttemptCaptureProcessing.raw_audio_id == record.expected_raw_audio_id,
    )).first()
    if (by_audio is None or by_audio.id != capture.id
            or capture.raw_audio_id != record.expected_raw_audio_id
            or capture.session_id != record.session_id
            or capture.record_command_id != record.id
            or capture.predecessor_command_id != record.predecessor_command_id):
        _legacy_fail("采集处理行与当前录音命令/录音标识不闭合")
    if (capture.repeat_admission_semantics
            != evidence_ledger.LEGACY_PRE_REPEAT_ADMISSION):
        _legacy_fail("采集准入标记不是旧协议标记")
    if (capture.repeat_protocol_version_id is not None
            or capture.repeat_protocol_definition_digest is not None
            or capture.repeat_request_id is not None):
        _legacy_fail("旧协议采集不得携带任何重复请求绑定或账本指针")
    return capture


_COMMAND_FINGERPRINT_FIELDS = (
    "id", "idempotency_key", "session_id", "command_seq", "item_id", "turn_seq",
    "turn_key", "attempt_seq", "prompt_level", "scope_key",
    "control_generation", "runner_generation", "issued_capability_token_hash",
    "issued_device_id_hash", "issued_at", "kind", "state",
    "predecessor_command_id", "trigger_ack_idempotency_key",
    "expected_raw_audio_id", "result_json", "revision", "created_at",
    "started_at", "succeeded_at", "failed_at", "cancelled_at",
    "item_bank_version_id", "item_bank_definition_digest",
    "autopilot_protocol_version_id", "autopilot_protocol_definition_digest",
    "response_role", "repeat_protocol_version_id",
    "repeat_protocol_definition_digest", "replay_source_command_id",
    "replay_ordinal", "replay_source_payload_sha256",
)
_ACK_FINGERPRINT_FIELDS = (
    "id", "command_id", "idempotency_key", "session_id", "ack_type",
    "command_revision", "control_generation", "runner_generation",
    "device_event_seq", "device_id_hash", "capability_token_hash",
    "payload_json", "receipt_server_seq", "raw_audio_id", "checksum",
    "byte_count", "duration_seconds", "received_at",
)
_RECEIPT_FINGERPRINT_FIELDS = (
    "server_seq", "raw_audio_id", "session_id", "turn_key", "received_at",
    "duration_seconds", "byte_count", "checksum", "data_classification",
    "is_simulation", "contains_direct_identifier",
)
_AUDIO_FINGERPRINT_FIELDS = (
    "raw_audio_id", "session_id", "audio_format", "status",
    "is_reliability_sample", "withdrawn", "withdrawal_status", "checksum",
    "contains_direct_identifier", "export_batch_id", "delete_gate_passed",
    "exported_at", "turn_key", "is_simulation", "data_classification",
    "byte_count", "uploaded_at", "patient_turn_ref_version",
)
# Deliberately excludes every mutable processing column: the durable stage
# machine advances status/disposition/final_attempt/lease between the very
# checkpoints this fingerprint has to survive.
_CAPTURE_FINGERPRINT_FIELDS = (
    "id", "record_command_id", "predecessor_command_id", "receipt_server_seq",
    "raw_audio_id", "session_id", "item_id", "turn_seq", "proof_attempt_seq",
    "proof_prompt_level", "created_at", "is_simulation",
    "repeat_admission_semantics", "repeat_protocol_version_id",
    "repeat_protocol_definition_digest",
)
_SESSION_FINGERPRINT_FIELDS = (
    "session_id", "patient_id", "session_sitting_no", "training_date",
    "week_no", "phase_type", "event_line", "trainer_id", "item_bank_version_id",
    "item_bank_definition_digest", "autopilot_protocol_version_id",
    "autopilot_protocol_definition_digest", "is_simulation",
    "data_classification", "visit_plan_id", "repeat_protocol_version_id",
    "repeat_protocol_definition_digest",
)
_PLAN_FINGERPRINT_FIELDS = (
    "plan_id", "protocol_slot_key", "patient_id", "scheduled_date",
    "session_sitting_no", "week_no", "phase_type", "event_line",
    "item_bank_version_id",
    "item_bank_definition_digest", "autopilot_protocol_version_id",
    "autopilot_protocol_definition_digest", "is_simulation",
    "data_classification", "status", "created_by", "approved_by", "approved_at",
    "started_by", "started_at", "cancelled_by", "cancelled_at",
    "repeat_protocol_version_id", "repeat_protocol_definition_digest",
)
_PATIENT_FINGERPRINT_FIELDS = (
    "patient_id", "is_simulation_subject", "consent_status",
    "recording_allowed", "withdrawal_status", "cloud_processing_allowed",
    "cloud_processing_provider_id", "cloud_processing_revoked_at",
    "governance_revision",
)
# ``last_seen_at`` is never written by resolution and ``last_autopilot_event_seq``
# advances on every ordinary device ACK; neither says anything about authority,
# so both stay out of the fingerprint.
_CAPABILITY_FINGERPRINT_FIELDS = (
    "token_hash", "session_id", "device_id_hash", "active_session_key",
    "created_at", "expires_at", "recovery_only_at", "revoked_at",
)
_RUNTIME_FINGERPRINT_FIELDS = (
    "session_id", "status", "revision", "paused_at", "completed_at",
    "aborted_at", "intervention_completed_at",
)
_STATE_FINGERPRINT_FIELDS = (
    "session_id", "scope_key", "mode", "status", "control_generation",
    "runner_generation", "revision", "next_command_seq", "current_command_id",
    "last_error_code",
)
_TTS_EVIDENCE_FINGERPRINT_FIELDS = (
    "id", "session_id", "command_id", "source", "engine_version", "cache_hit",
    "result", "byte_count", "text_sha256", "is_simulation", "created_at",
)


def _legacy_evidence_fingerprint(db: Session, **rows: object) -> str:
    """Canonical digest of every immutable fact the recovery depends on.

    Row identity is not evidence identity.  A worker that returns from a
    provider call must be able to prove that the exact recording, the exact
    prompt payload, the exact ACKs, receipt, audio bytes, device capability,
    frozen content digests and live handshake are still the ones it froze — not
    merely that some self-consistent chain exists under the same ids.
    """
    source = rows["source"]
    record = rows["record"]
    serve_evidence = list(db.exec(select(TtsServeEvidence).where(
        TtsServeEvidence.command_id == source.id,
    ).order_by(TtsServeEvidence.id)))
    return _canonical_digest({
        "session": _row_facts(rows["train_session"], _SESSION_FINGERPRINT_FIELDS),
        "plan": _row_facts(rows["plan"], _PLAN_FINGERPRINT_FIELDS),
        "patient": _row_facts(rows["patient"], _PATIENT_FINGERPRINT_FIELDS),
        "capability": _row_facts(
            rows["capability"], _CAPABILITY_FINGERPRINT_FIELDS),
        "runtime": _row_facts(rows["runtime_state"], _RUNTIME_FINGERPRINT_FIELDS),
        "state": _row_facts(rows["state"], _STATE_FINGERPRINT_FIELDS),
        "source": {
            **_row_facts(source, _COMMAND_FINGERPRINT_FIELDS),
            # Hash the source command's canonical payload_json rather than
            # embedding a second copy of the frozen line.  This digest covers
            # the command payload only — never any synthesized audio.
            "payload_sha256": hashlib.sha256(
                source.payload_json.encode("utf-8")).hexdigest(),
        },
        "source_serve_evidence": [
            _row_facts(row, _TTS_EVIDENCE_FINGERPRINT_FIELDS)
            for row in serve_evidence],
        "source_ack": _row_facts(rows["source_ack"], _ACK_FINGERPRINT_FIELDS),
        "record": {
            **_row_facts(record, _COMMAND_FINGERPRINT_FIELDS),
            "payload_sha256": hashlib.sha256(
                record.payload_json.encode("utf-8")).hexdigest(),
        },
        "record_ack": _row_facts(rows["stopped_ack"], _ACK_FINGERPRINT_FIELDS),
        "receipt": _row_facts(rows["receipt"], _RECEIPT_FINGERPRINT_FIELDS),
        "audio": _row_facts(rows["audio"], _AUDIO_FINGERPRINT_FIELDS),
        "capture": _row_facts(rows["capture"], _CAPTURE_FINGERPRINT_FIELDS),
        "content": rows["content"],
        "live": rows["live"],
    })


def _legacy_stage_fingerprint(
    capture: AttemptCaptureProcessing,
    attempt_facts: object,
    interaction_facts: object,
) -> str:
    """Digest of exactly what a durable checkpoint is allowed to advance.

    A judgement-stage worker must still be able to prove the exact transcript
    and ASR facts it is about to judge have not been rewritten underneath it,
    so these move rather than disappear when the stage changes.
    """
    return _canonical_digest({
        "capture_stage": _row_facts(
            capture, _CAPTURE_STAGE_FINGERPRINT_FIELDS),
        "attempt": attempt_facts,
        "interactions": interaction_facts,
    })


_CAPTURE_STAGE_FINGERPRINT_FIELDS = (
    "processing_status", "disposition", "final_attempt_id", "asr_confidence",
    "asr_engine_version", "error_code", "processed_at",
)
_ATTEMPT_STAGE_FINGERPRINT_FIELDS = (
    "id", "session_id", "item_id", "turn_seq", "response_role", "attempt_seq",
    "raw_audio_id", "prompt_level", "cue_type", "duration_seconds",
    "asr_confidence", "asr_engine_version", "operational_answer_type",
    "operational_score", "operational_needs_review", "judge_mode",
    "judge_engine_version", "judge_reason", "matched_on", "contains_target",
    "judge_portrait_used", "processing_status", "processing_generation",
    "error_code", "created_at", "processed_at", "is_simulation",
)


def _legacy_attempt_stage_facts(db: Session, attempt: AttemptEvent | None) -> object:
    """Stage-specific Attempt facts; the transcript only ever as a digest."""
    if attempt is None:
        return None
    return {
        **_row_facts(attempt, _ATTEMPT_STAGE_FINGERPRINT_FIELDS),
        "asr_text_sha256": hashlib.sha256(
            attempt.asr_text.encode("utf-8")).hexdigest(),
    }


def _legacy_interaction_facts(db: Session, attempt: AttemptEvent | None) -> object:
    """Exact interaction cardinality and canonical payloads for this attempt.

    A subset test over ``event_type`` would accept duplicated or rewritten
    interaction rows; the whole ordered list is frozen instead.
    """
    if attempt is None:
        return None
    rows = list(db.exec(select(InteractionEvent).where(
        InteractionEvent.session_id == attempt.session_id,
        InteractionEvent.attempt_id == attempt.id,
    ).order_by(InteractionEvent.event_seq)))
    return [
        {
            "event_seq": row.event_seq,
            "event_type": row.event_type,
            "item_id": row.item_id,
            "turn_seq": row.turn_seq,
            "attempt_seq": row.attempt_seq,
            "payload_json": row.payload_json,
        }
        for row in rows
    ]


def _legacy_live_binding(db: Session, train_session: TrainSession) -> dict:
    payload = autopilot_service._load_live_payload(  # noqa: SLF001
        db.get(LiveState, 1))
    binding = {
        "sessionId": payload.get("sessionId") or payload.get("session_id"),
        "weekNo": payload.get("weekNo"),
        "eventLine": payload.get("eventLine"),
        "itemBankVersionId": payload.get("itemBankVersionId"),
        "mode": payload.get("mode"),
    }
    expected = {
        "sessionId": train_session.session_id,
        "weekNo": train_session.week_no,
        "eventLine": autopilot_service._enum_value(  # noqa: SLF001
            train_session.event_line),
        "itemBankVersionId": train_session.item_bank_version_id,
        "mode": "task",
    }
    if isinstance(binding["weekNo"], bool) or binding != expected:
        _legacy_fail("实时场次握手与旧协议场次不一致")
    return binding


def _frozen_speech_line(
    source: RuntimeCommand,
    payload: TtsCommandPayload,
    selected: object,
) -> str:
    """The exact line the frozen protocol authorises for this prompt level.

    Derived from the shared P0a content selector rather than re-read from the
    item bank here, so the protocol shape, supported weeks, TTS allowlist and
    row-level content contract are all the ones the modern path enforces.
    """
    if source.prompt_level == 0:
        return selected.initial_prompt
    if source.prompt_level == 2:
        return selected.cue2
    branch = {
        "unknown": selected.cue1_unknown,
        "close": selected.cue1_close,
        "silence": selected.cue1_silence,
    }.get(payload.response_path)
    if branch is None:
        _legacy_fail("一级提示缺少与命令载荷一致的源分支")
    return branch


def _verify_legacy_source_speech(
    db: Session,
    *,
    source: RuntimeCommand,
    record: RuntimeCommand,
    train_session: TrainSession,
    selected: object,
    source_ack: RuntimeCommandAck,
    receipt: AudioCaptureReceipt,
    stopped_ack: RuntimeCommandAck,
    capture: AttemptCaptureProcessing,
    plan_started_at: datetime,
    audio_uploaded_at: datetime,
) -> None:
    """Prove the speech was really served, really frozen, and really first."""
    try:
        payload = TtsCommandPayload.model_validate_json(source.payload_json)
    except (TypeError, ValueError) as exc:
        raise AutopilotOrchestrationError(
            LEGACY_RECOVERY_INVALID, "前置 TTS 载荷不符合封闭契约") from exc
    expected_purpose = "question" if source.prompt_level == 0 else "cue"
    payload_facts = (
        payload.purpose == expected_purpose,
        payload.item_id == source.item_id,
        payload.turn_seq == source.turn_seq,
        payload.cue_level == source.prompt_level,
        payload.speech_key == f"p0a.{expected_purpose}.{source.command_seq}",
        payload.speech_text == _frozen_speech_line(source, payload, selected),
        payload.speech_text in selected.allowed_tts_lines,
    )
    if not all(payload_facts):
        _legacy_fail("前置 TTS 载荷不是该环节的冻结话术")

    # Existence, uniqueness and content of the serve evidence are all provable
    # without a clock.  Its ``created_at`` deliberately stays out of the
    # ordering below: TtsServeEvidence is written with the local-naive
    # ``datetime.now()`` default while commands, ACKs and receipts are
    # UTC-naive, so comparing the two domains would read as an ~8-hour
    # backwards jump on any non-UTC host.  Only same-domain pairs are ordered.
    served = list(db.exec(select(TtsServeEvidence).where(
        TtsServeEvidence.command_id == source.id)))
    if len(served) != 1:
        _legacy_fail("前置 TTS 没有唯一的服务端播报证据")
    evidence = served[0]
    evidence_facts = (
        evidence.session_id == train_session.session_id,
        evidence.source == "autopilot_command",
        evidence.result == "served",
        isinstance(evidence.byte_count, int) and evidence.byte_count > 0,
        evidence.is_simulation == train_session.is_simulation,
        evidence.text_sha256 == hashlib.sha256(
            payload.speech_text.encode("utf-8")).hexdigest(),
    )
    if not all(evidence_facts):
        _legacy_fail("前置 TTS 播报证据与冻结话术不一致")

    # Every pair below is UTC-naive on both sides: RuntimeCommand.issued_at /
    # succeeded_at, RuntimeCommandAck.received_at, AudioCaptureReceipt.
    # received_at, AudioAssetRow.uploaded_at and VisitPlan.started_at all come
    # from the UTC-naive server clock, and AttemptCaptureProcessing.created_at
    # is written explicitly from the same ACK-path clock.
    ordered = (
        ("plan started", plan_started_at),
        ("source issued", source.issued_at),
        ("tts ended ACK", source_ack.received_at),
        ("source succeeded", source.succeeded_at),
        ("record issued", record.issued_at),
        ("audio uploaded", audio_uploaded_at),
        ("capture receipt", receipt.received_at),
        ("record stopped ACK", stopped_ack.received_at),
        ("record succeeded", record.succeeded_at),
        ("capture created", capture.created_at),
    )
    for (_earlier_name, earlier), (later_name, later) in zip(ordered, ordered[1:]):
        if earlier is None or later is None or earlier > later:
            _legacy_fail(f"旧链时间顺序在 {later_name} 处倒流")


def verify_legacy_pre_repeat_recovery(
    db: Session,
    *,
    session_id: str,
    now: datetime | None = None,
) -> LegacyRepeatRecovery:
    """Prove the exact pre-protocol capture that may still be finished once.

    A session admitted before the repeat protocol existed can no longer run the
    modern flow: every generic gate refuses it, which would otherwise strand its
    one outstanding recording forever.  This verifier is the only way that
    recording is ever processed again, and it proves the whole chain from first
    principles rather than reusing a gate that presumes a frozen repeat binding.

    It never accepts a NULL binding as evidence of anything.  The
    ``legacy_pre_repeat`` marker — written only by the d3 migration, never by a
    running service — is the sole admission signal, and everything else here is
    an independent proof that this really is that one untouched chain: a started
    VisitPlan really produced this session, today's frozen item bank and
    autopilot protocol really hash to what the chain recorded, and every media
    fact still agrees with itself.
    """
    observed_at = (
        autopilot_service._utc_naive(now)  # noqa: SLF001 - sibling domain clock
        if now is not None else autopilot_service._utc_now_naive()  # noqa: SLF001
    )
    # 旧协议恢复通道有意保持仅模拟：legacy_pre_repeat 标记只由 d3 迁移写入，
    # 真实研究场次结构上不存在这种前协议采集；ENABLE_AUTOPILOT_REAL_SESSIONS
    # 永远不为这条路径开门。
    if not autopilot_service.p0a_feature_enabled():
        _legacy_fail("P0a 未显式启用，旧协议采集不进入恢复通道")
    state, record = _lock_pending_command(db, session_id=session_id)
    capture = _legacy_marker_capture(db, record, lock=True)

    source = db.get(RuntimeCommand, record.predecessor_command_id)
    if source is None or source.kind != "tts" or source.state != "succeeded":
        _legacy_fail("前置 TTS 命令不存在或未成功结束")
    identity = (
        "session_id", "item_id", "turn_seq", "turn_key", "attempt_seq",
        "prompt_level", "scope_key", "control_generation", "runner_generation",
        "issued_capability_token_hash", "issued_device_id_hash",
    )
    if any(getattr(source, name) != getattr(record, name) for name in identity):
        _legacy_fail("前置 TTS 与录音命令不在同一冻结环节")
    if record.command_seq != source.command_seq + 1:
        _legacy_fail("录音命令不是前置 TTS 的下一条命令")
    for command in (source, record):
        if (command.replay_source_command_id is not None
                or command.replay_ordinal is not None
                or command.replay_source_payload_sha256 is not None):
            _legacy_fail("旧链命令带有重播溯源，不是未经重播的原始环节")

    train_session = db.get(TrainSession, session_id)
    if train_session is None:
        _legacy_fail("场次不存在")
    # The exception exists to finish one recording that a real started plan
    # produced.  An orphan session is not that, so a linked started plan is
    # mandatory rather than best effort.
    if train_session.visit_plan_id is None:
        _legacy_fail("旧协议恢复只允许修复由训练安排开出的场次")
    plan = db.get(VisitPlan, train_session.visit_plan_id)
    if plan is None:
        _legacy_fail("场次绑定的训练安排不存在")
    # The marker may only stand for "this whole session predates the protocol".
    # A persisted binding on the plan/session, a stray repeat ledger row or a
    # single bound/replayed command anywhere in the session means it does not,
    # so the exception no longer applies.  The terminal pause and the drain
    # matcher refuse on this same closed set.
    poison = autopilot_service.legacy_session_repeat_poison_reason(
        db, session_id=session_id)
    if poison == "repeat_binding":
        _legacy_fail("旧链上出现了重复请求协议绑定，不属于旧协议恢复")
    if poison == "repeat_ledger":
        _legacy_fail("旧协议场次出现了重复请求账本记录")
    if poison == "bound_or_replayed_command":
        _legacy_fail("旧协议场次中存在带重复请求绑定或重播溯源的命令")

    # Frozen content identity is recomputed from the deployed definitions and
    # compared to every stored binding; comparing the stored rows only to each
    # other would accept a chain frozen against content that no longer exists.
    bank = autopilot_service._session_week_bank(  # noqa: SLF001
        db, train_session.session_id)
    protocol = content.load_autopilot_protocol(
        content.CONTENT_DIR / "autopilot_protocol_v1.json")
    live_content = {
        "item_bank_version_id": bank.version_id,
        "item_bank_definition_digest": content.item_bank_definition_digest(bank),
        "autopilot_protocol_version_id": str(protocol["protocol_version_id"]),
        "autopilot_protocol_definition_digest": (
            content.autopilot_protocol_definition_digest(protocol)),
    }
    for row in (plan, train_session, source, record):
        if any(getattr(row, name) != value
               for name, value in live_content.items()):
            _legacy_fail("旧链冻结题库/自动化协议标识与当前定义不一致")

    plan_facts = (
        plan.status == "started",
        plan.patient_id == train_session.patient_id,
        plan.session_sitting_no == train_session.session_sitting_no,
        plan.week_no == train_session.week_no,
        autopilot_service._enum_value(plan.phase_type)  # noqa: SLF001
        == autopilot_service._enum_value(train_session.phase_type),  # noqa: SLF001
        autopilot_service._enum_value(plan.event_line)  # noqa: SLF001
        == autopilot_service._enum_value(train_session.event_line),  # noqa: SLF001
        plan.is_simulation == train_session.is_simulation,
        plan.data_classification == train_session.data_classification,
        bool((plan.protocol_slot_key or "").strip()),
        bool((plan.created_by or "").strip()),
        plan.scheduled_date is not None,
        bool((plan.approved_by or "").strip()) and plan.approved_at is not None,
        bool((plan.started_by or "").strip()) and plan.started_at is not None,
        plan.cancelled_by is None and plan.cancelled_at is None,
        plan.started_by == train_session.trainer_id,
        bool((train_session.trainer_id or "").strip()),
        train_session.training_date is not None,
        train_session.training_date is not None
        and plan.scheduled_date <= train_session.training_date,
        autopilot_service._enum_value(plan.phase_type) == "正式训练",  # noqa: SLF001
        autopilot_service._enum_value(plan.event_line) == "正式训练",  # noqa: SLF001
        autopilot_service._enum_value(  # noqa: SLF001
            train_session.phase_type) == "正式训练",
        autopilot_service._enum_value(  # noqa: SLF001
            train_session.event_line) == "正式训练",
    )
    if not all(plan_facts):
        _legacy_fail("训练安排未真正开场，或与场次的冻结身份不一致")

    if (train_session.is_simulation is not True
            or train_session.data_classification != "simulation"):
        _legacy_fail("旧协议恢复只允许模拟场次")
    patient = db.get(Patient, train_session.patient_id)
    consent = (patient.consent_status or "").strip().casefold() if patient else ""
    if (patient is None
            or patient.is_simulation_subject is not True
            or patient.recording_allowed is not True
            or bool((patient.withdrawal_status or "").strip())
            or consent in autopilot_service._DENIED_CONSENT):  # noqa: SLF001
        _legacy_fail("受试者缺少有效的模拟身份、录音授权或已撤回")

    runtime_state = db.get(SessionRuntimeState, session_id)
    if (runtime_state is None or runtime_state.status != "active"
            or runtime_state.intervention_completed_at is not None
            or runtime_state.completed_at is not None
            or runtime_state.aborted_at is not None):
        _legacy_fail("场次运行态不是可继续处理的 active")
    live_binding = _legacy_live_binding(db, train_session)

    try:
        capability = autopilot_service._require_active_device(  # noqa: SLF001
            db, session_id, now=observed_at)
    except autopilot_service.AutopilotServiceError as exc:
        raise AutopilotOrchestrationError(exc.code, exc.message) from exc
    if (capability.session_id != session_id
            or capability.token_hash != record.issued_capability_token_hash
            or capability.device_id_hash != record.issued_device_id_hash):
        _legacy_fail("当前活跃设备不是发行该录音命令的设备")

    try:
        proof = autopilot_ledger.verify_record_capture_for_attempt(db, record.id)
    except autopilot_ledger.AutopilotProofError as exc:
        raise AutopilotOrchestrationError(
            LEGACY_RECOVERY_INVALID, "旧协议采集证据链复核失败") from exc
    proof_facts = (
        proof.session_id == capture.session_id,
        proof.item_id == capture.item_id,
        proof.turn_seq == capture.turn_seq,
        proof.attempt_seq == capture.proof_attempt_seq,
        proof.prompt_level == capture.proof_prompt_level,
        proof.raw_audio_id == capture.raw_audio_id,
        proof.receipt_server_seq == capture.receipt_server_seq,
        proof.command_id == capture.record_command_id,
        capture.is_simulation == train_session.is_simulation,
    )
    if not all(proof_facts):
        _legacy_fail("采集行与自身采集证据不一致")

    receipt = db.get(AudioCaptureReceipt, proof.receipt_server_seq)
    audio = db.get(AudioAssetRow, proof.raw_audio_id)
    source_ack = db.exec(select(RuntimeCommandAck).where(
        RuntimeCommandAck.command_id == source.id,
        RuntimeCommandAck.ack_type == "tts_ended",
    )).first()
    stopped_ack = db.exec(select(RuntimeCommandAck).where(
        RuntimeCommandAck.command_id == record.id,
        RuntimeCommandAck.ack_type == "record_stopped",
    )).first()
    if (receipt is None or audio is None or source_ack is None
            or stopped_ack is None):
        _legacy_fail("旧链缺少采集回执、音频资产或必需的设备回执")
    if source_ack.idempotency_key != record.trigger_ack_idempotency_key:
        _legacy_fail("录音命令的触发回执不是该前置 TTS 的结束回执")
    media_facts = (
        receipt.raw_audio_id == capture.raw_audio_id,
        receipt.session_id == capture.session_id,
        receipt.turn_key == record.turn_key,
        receipt.checksum == proof.checksum,
        receipt.byte_count == proof.byte_count,
        receipt.duration_seconds == proof.duration_seconds,
        receipt.data_classification == train_session.data_classification,
        receipt.is_simulation == train_session.is_simulation,
        audio.session_id == capture.session_id,
        audio.turn_key == record.turn_key,
        audio.checksum == receipt.checksum,
        audio.byte_count == receipt.byte_count,
        audio.data_classification == receipt.data_classification,
        audio.is_simulation == receipt.is_simulation,
        audio.contains_direct_identifier == receipt.contains_direct_identifier,
        audio.status == AudioStatus.recorded,
        audio.uploaded_at is not None,
        not audio.withdrawn,
        not (audio.withdrawal_status or "").strip(),
        not audio.delete_gate_passed,
    )
    if not all(media_facts):
        _legacy_fail("采集回执、音频资产与录音命令的媒体事实不一致")

    if capture.proof_attempt_seq != capture.proof_prompt_level + 1:
        _legacy_fail("采集的 attempt 序号与提示等级不单调")
    try:
        selected = autopilot_service._select_p0a_content(  # noqa: SLF001
            train_session, bank, protocol,
            item_id=record.item_id, turn_seq=record.turn_seq)
    except autopilot_service.AutopilotServiceError as exc:
        raise AutopilotOrchestrationError(exc.code, exc.message) from exc
    response_role, cue_type = _frozen_attempt_context(
        train_session, record, bank, protocol)
    if (record.response_role != response_role
            or selected.response_role != response_role
            or selected.item_id != record.item_id
            or selected.turn_seq != record.turn_seq
            or selected.max_duration_seconds < receipt.duration_seconds):
        _legacy_fail("录音命令与冻结题库位置/应答角色/时长上限不一致")
    # One derivation, shared with the terminal probe, the pause stage and the
    # drain matcher, so none of them can accept a different set of facts.
    attempt_input = _legacy_expected_input(db, record, capture)
    if (attempt_input.response_role != response_role
            or attempt_input.cue_type != cue_type
            or attempt_input.raw_audio_id != proof.raw_audio_id
            or attempt_input.item_id != proof.item_id
            or attempt_input.turn_seq != proof.turn_seq
            or attempt_input.prompt_level != proof.prompt_level
            or attempt_input.duration_seconds != proof.duration_seconds):
        _legacy_fail("共享权威输入推导与采集证据不一致")

    _verify_legacy_source_speech(
        db, source=source, record=record, train_session=train_session,
        selected=selected, source_ack=source_ack, receipt=receipt,
        stopped_ack=stopped_ack, capture=capture,
        plan_started_at=plan.started_at, audio_uploaded_at=audio.uploaded_at)

    stage, attempt_id = _legacy_recovery_stage(
        db, session_id=session_id, capture=capture, record=record,
        expected=attempt_input)
    staged_attempt = (db.get(AttemptEvent, attempt_id)
                      if attempt_id is not None else None)
    base_fingerprint = _legacy_evidence_fingerprint(
        db, train_session=train_session, plan=plan, patient=patient,
        capability=capability, runtime_state=runtime_state, state=state,
        source=source, source_ack=source_ack, record=record,
        stopped_ack=stopped_ack, receipt=receipt, audio=audio, capture=capture,
        content=live_content, live=live_binding)
    stage_fingerprint = _legacy_stage_fingerprint(
        capture,
        _legacy_attempt_stage_facts(db, staged_attempt),
        _legacy_interaction_facts(db, staged_attempt))
    target = LegacyRepeatRecoveryTarget(
        session_id=session_id,
        patient_id=train_session.patient_id,
        record_command_id=record.id,
        source_command_id=source.id,
        capture_id=capture.id,
        control_generation=state.control_generation,
        runner_generation=state.runner_generation,
        state_revision=state.revision,
        audio_checksum=audio.checksum,
        audio_byte_count=audio.byte_count,
        base_evidence_fingerprint=base_fingerprint,
        stage_evidence_fingerprint=stage_fingerprint,
        attempt_input=attempt_input,
    )
    return LegacyRepeatRecovery(
        target=target, stage=stage, attempt_id=attempt_id)


def _legacy_expected_input(
    db: Session, record: RuntimeCommand, capture: AttemptCaptureProcessing,
) -> AuthoritativeAttemptInput:
    """The shared provider-free derivation, as this module's closed input type."""
    try:
        facts = autopilot_service.legacy_expected_attempt_facts(
            db, record=record, capture=capture)
    except autopilot_service.AutopilotServiceError as exc:
        raise AutopilotOrchestrationError(exc.code, exc.message) from exc
    try:
        return AuthoritativeAttemptInput(**facts)
    except ValueError as exc:
        raise AutopilotOrchestrationError(
            LEGACY_RECOVERY_INVALID, "旧协议权威输入不符合封闭契约") from exc


def _legacy_attempt_for(
    db: Session, capture: AttemptCaptureProcessing, record: RuntimeCommand,
    *, expected: AuthoritativeAttemptInput | None = None,
) -> AttemptEvent:
    """The one Attempt this capture already produced, proved field by field.

    The evidence-only closure always runs, so a rewritten ``response_role``,
    ``cue_type`` or ``duration_seconds`` can never be judged and then paused as
    a completed recovery.  ``expected`` adds the stricter, content-derived
    comparison and is supplied only on the pre-provider path: the terminal
    paths must keep working after an item-bank or protocol file changes.
    """
    attempt = db.get(AttemptEvent, capture.final_attempt_id)
    if attempt is None:
        _legacy_fail("采集绑定的 attempt 行已消失")
    by_audio = db.exec(select(AttemptEvent).where(
        AttemptEvent.raw_audio_id == capture.raw_audio_id,
    )).first()
    facts = (
        by_audio is not None and by_audio.id == attempt.id,
        attempt.session_id == capture.session_id,
        attempt.raw_audio_id == capture.raw_audio_id,
        attempt.item_id == capture.item_id,
        attempt.turn_seq == capture.turn_seq,
        attempt.attempt_seq == capture.proof_attempt_seq,
        attempt.prompt_level == capture.proof_prompt_level,
        attempt.is_simulation == capture.is_simulation,
        attempt.asr_engine_version == capture.asr_engine_version,
        attempt.asr_confidence == capture.asr_confidence,
    )
    if not all(facts):
        _legacy_fail("采集与其绑定 attempt 的身份不一致")
    if not autopilot_service.legacy_attempt_closes_persisted_evidence(
            db, attempt=attempt, record=record, capture=capture):
        _legacy_fail("已落库 attempt 与录音命令/回执的不可变事实不闭合")
    if expected is not None and (
            attempt.item_id != expected.item_id
            or attempt.turn_seq != expected.turn_seq
            or attempt.response_role != expected.response_role
            or attempt.raw_audio_id != expected.raw_audio_id
            or attempt.prompt_level != expected.prompt_level
            or attempt.cue_type != expected.cue_type
            or attempt.duration_seconds != expected.duration_seconds):
        _legacy_fail("已落库 attempt 与重新推导的权威输入不一致")
    return attempt


def _legacy_recovery_stage(
    db: Session, *, session_id: str, capture: AttemptCaptureProcessing,
    record: RuntimeCommand, expected: AuthoritativeAttemptInput,
) -> tuple[str, int | None]:
    """Which durable stage this capture has actually reached, or fail closed."""
    if capture.processing_status == "received":
        if (capture.disposition is not None
                or capture.final_attempt_id is not None
                or capture.error_code is not None
                or capture.processed_at is not None):
            _legacy_fail("旧协议采集的 received 状态与其终态字段矛盾")
        if db.exec(select(AttemptEvent).where(
                AttemptEvent.raw_audio_id == capture.raw_audio_id)).first():
            _legacy_fail("该录音已存在 attempt，但采集仍是 received")
        latest_seq = db.exec(select(func.max(AttemptEvent.attempt_seq)).where(
            AttemptEvent.session_id == session_id,
            AttemptEvent.item_id == capture.item_id,
            AttemptEvent.turn_seq == capture.turn_seq,
        )).one()
        if int(latest_seq or 0) + 1 != capture.proof_attempt_seq:
            _legacy_fail("采集的 attempt 序号不是当前服务端的下一序号")
        return LEGACY_STAGE_ASR, None
    if (capture.processing_status != "asr_completed"
            or capture.disposition != "answer_candidate"
            or capture.final_attempt_id is None
            or capture.error_code is not None
            or capture.processed_at is None):
        _legacy_fail("旧协议采集处于不可恢复的终态")
    attempt = _legacy_attempt_for(db, capture, record, expected=expected)
    latest_seq = db.exec(select(func.max(AttemptEvent.attempt_seq)).where(
        AttemptEvent.session_id == session_id,
        AttemptEvent.item_id == capture.item_id,
        AttemptEvent.turn_seq == capture.turn_seq,
    )).one()
    if int(latest_seq or 0) != capture.proof_attempt_seq:
        _legacy_fail("恢复中的 attempt 序号不是该环节的最新序号")
    if (attempt.processing_status == "asr_completed"
            and attempt.asr_text is not None
            and attempt.error_code is None):
        if not autopilot_service.legacy_interactions_are_exact(
                db, attempt, expect_judgement=False):
            _legacy_fail("待判分 attempt 的交互证据不是精确的两条规范记录")
        return LEGACY_STAGE_JUDGEMENT, attempt.id
    if autopilot_service.legacy_attempt_is_successfully_judged(db, attempt):
        return LEGACY_STAGE_PAUSE, attempt.id
    _legacy_fail("旧协议恢复的 attempt 既未待判分也不是成功判分终态")


def legacy_terminal_pause_owed(db: Session, *, session_id: str) -> bool:
    """Read-only: is the only thing still owed the terminal legacy pause?

    Deliberately provider-free and narrower than the full verifier.  Once the
    judgement has durably committed, finishing the scope needs no device, no
    content resolution and no provider authority — so a capability that expired
    or a feature toggle flipped after that commit must not be able to strand the
    session in ``processing_attempt`` forever.  This recognises exactly that one
    shape and opens no gate for a capture still owing ASR or judgement.
    """
    state = db.get(SessionAutopilotState, session_id)
    if (state is None or state.scope_key != autopilot_service.P0A_SCOPE_KEY
            or state.mode != "autonomous"
            or state.status != "processing_attempt"
            or state.current_command_id is None):
        return False
    record = db.exec(select(RuntimeCommand).where(
        RuntimeCommand.id == state.current_command_id,
        RuntimeCommand.session_id == session_id,
    )).first()
    if (record is None or record.kind != "record"
            or record.state != "succeeded"
            or record.expected_raw_audio_id is None
            or record.control_generation != state.control_generation
            or record.runner_generation != state.runner_generation):
        return False
    try:
        capture = _legacy_marker_capture(db, record, lock=False)
    except AutopilotOrchestrationError:
        return False
    if (capture.processing_status != "asr_completed"
            or capture.disposition != "answer_candidate"
            or capture.final_attempt_id is None
            or capture.error_code is not None
            or capture.processed_at is None):
        return False
    try:
        attempt = _legacy_attempt_for(db, capture, record)
    except AutopilotOrchestrationError:
        return False
    if not autopilot_service.legacy_attempt_is_successfully_judged(db, attempt):
        return False
    try:
        # The same immutable proof the pause stage runs, so the read-only probe
        # can never be more permissive than the write it schedules.
        autopilot_ledger.verify_immutable_record_capture(db, record)
    except autopilot_ledger.AutopilotProofError:
        return False
    if autopilot_service.legacy_session_repeat_poison_reason(
            db, session_id=session_id) is not None:
        return False
    return db.exec(select(AutopilotControlEvent).where(
        AutopilotControlEvent.idempotency_key
        == autopilot_service.legacy_repeat_recovery_event_key(
            session_id, capture.id),
    )).first() is None


def legacy_pre_repeat_recovery_pending(db: Session, *, session_id: str) -> bool:
    """Content-free read-only probe: may an internal recovery trigger fire?

    It runs the full verifier and answers only yes/no.  Anything weaker would
    let a corrupted legacy chain answer ``/next`` with a quiet 200 and a worker
    submission that then silently refuses; the HTTP layer must instead keep
    failing closed on the original gate error.  Nothing is written: the verifier
    only reads, and the caller owns the transaction.
    """
    try:
        verify_legacy_pre_repeat_recovery(db, session_id=session_id)
    except AutopilotOrchestrationError:
        return False
    return True


def stage_legacy_repeat_recovery_pause(
    db: Session,
    *,
    session_id: str,
    now: datetime | None = None,
) -> bool:
    """Stage the autopilot half of the terminal legacy-recovery pause.

    The caller must invoke its runtime/LiveState pause helper and commit in the
    same transaction, exactly as with :func:`stage_processing_failure`.  This is
    the only outcome a legacy recovery may produce: the one outstanding
    recording has become a completed, fully judged ordinary Attempt, and the
    scope then stops for a researcher rather than continuing under semantics it
    never froze.

    ``False`` means there is nothing to pause — the capture has not been
    finished yet, the scope already moved on, or another worker already recorded
    this exact pause.  Every such case is a pure no-op with no writes.
    """
    observed_at = (
        autopilot_service._utc_naive(now)  # noqa: SLF001
        if now is not None else autopilot_service._utc_now_naive()  # noqa: SLF001
    )
    state = db.exec(select(SessionAutopilotState).where(
        SessionAutopilotState.session_id == session_id,
    ).with_for_update()).first()
    if (state is None or state.scope_key != autopilot_service.P0A_SCOPE_KEY
            or state.mode != "autonomous"
            or state.status != "processing_attempt"
            or state.current_command_id is None):
        return False
    record = db.exec(select(RuntimeCommand).where(
        RuntimeCommand.id == state.current_command_id,
        RuntimeCommand.session_id == session_id,
    ).with_for_update()).first()
    if (record is None or record.kind != "record" or record.state != "succeeded"
            or record.control_generation != state.control_generation
            or record.runner_generation != state.runner_generation):
        return False
    # Re-establish the legacy marker from the persisted media, not from any
    # value the caller carried across the provider call.
    capture = _legacy_marker_capture(db, record, lock=True)
    if (capture.processing_status != "asr_completed"
            or capture.disposition != "answer_candidate"
            or capture.final_attempt_id is None):
        return False
    attempt = _legacy_attempt_for(db, capture, record)
    if not autopilot_service.legacy_attempt_is_successfully_judged(db, attempt):
        # A technical failure or a half-judged Attempt is not a completed
        # recovery; it must never acquire the recovery reason code.
        return False
    try:
        autopilot_ledger.verify_terminal_record_capture(db, record.id)
    except autopilot_ledger.AutopilotProofError as exc:
        raise AutopilotOrchestrationError(
            LEGACY_RECOVERY_INVALID, "旧协议恢复暂停前采集证据复核失败") from exc
    # Same transaction as the state/record/capture/attempt locks above, and
    # before the first durable write: a session poisoned after the judgement
    # committed must stop here rather than earn the recovery reason code.
    if autopilot_service.legacy_session_repeat_poison_reason(
            db, session_id=session_id) is not None:
        return False

    event_key = autopilot_service.legacy_repeat_recovery_event_key(
        session_id, capture.id)
    if db.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.idempotency_key == event_key,
    )).first() is not None:
        return False

    result = db.execute(
        update(SessionAutopilotState)
        .where(
            SessionAutopilotState.session_id == session_id,
            SessionAutopilotState.scope_key == state.scope_key,
            SessionAutopilotState.mode == "autonomous",
            SessionAutopilotState.status == "processing_attempt",
            SessionAutopilotState.current_command_id == record.id,
            SessionAutopilotState.control_generation == state.control_generation,
            SessionAutopilotState.runner_generation == state.runner_generation,
            SessionAutopilotState.revision == state.revision,
        )
        .values(
            status="paused",
            current_command_id=None,
            revision=SessionAutopilotState.revision + 1,
            last_error_code=(
                autopilot_service.LEGACY_REPEAT_RECOVERY_REASON_CODE),
            lease_owner=None,
            lease_acquired_at=None,
            lease_expires_at=None,
            updated_at=observed_at,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    # Fence any worker still holding a claim on this scope's media so a late
    # result cannot append evidence after the terminal pause commits.
    evidence_ledger.invalidate_processing_claims(
        db, session_id=session_id, raw_audio_id=record.expected_raw_audio_id)
    evidence_ledger.invalidate_capture_processing_claims(
        db, session_id=session_id, raw_audio_id=record.expected_raw_audio_id)
    db.add(AutopilotControlEvent(
        idempotency_key=event_key,
        session_id=session_id,
        event_seq=_next_control_event_seq(db, session_id),
        event_type="pause",
        scope_key=state.scope_key,
        control_generation=state.control_generation,
        runner_generation=state.runner_generation,
        command_id=record.id,
        actor_type="system",
        actor_id=None,
        reason_code=autopilot_service.LEGACY_REPEAT_RECOVERY_REASON_CODE,
        from_mode="autonomous",
        to_mode="autonomous",
        from_status="processing_attempt",
        to_status="paused",
        payload_json=autopilot_ledger.encode_control_event_payload(
            "pause", {
                "reason_code": (
                    autopilot_service.LEGACY_REPEAT_RECOVERY_REASON_CODE),
                "source": autopilot_service.LEGACY_REPEAT_RECOVERY_SOURCE,
            }),
        created_at=observed_at,
    ))
    db.flush()
    return True
def stage_processing_failure(
    db: Session,
    *,
    session_id: str,
    error_code: str,
    source: Literal["attempt_processing", "worker_exception"],
    target: FrozenWorkerTarget | None = None,
    now: datetime | None = None,
) -> bool:
    """Stage the autopilot half of an atomic runtime+autopilot pause.

    The caller must invoke its existing runtime/LiveState pause helper and commit in
    the same transaction.  ``False`` means there is no current P0a processing state
    to pause, or ``target`` does not (or no longer) match it exactly; it never
    broadens this safety path to ordinary sessions.

    ``target`` must be the exact :class:`FrozenWorkerTarget` the caller froze
    before any provider I/O. A missing or session-mismatched target is an
    immediate no-op, checked before this function ever locks or even reads the
    current row — there is no session-current fallback. Every other target
    field (command id, raw audio, both generations) is then re-checked against
    the current locked row: any mismatch (a newer worker has since taken over
    and completed/routed a different logical attempt) is also a pure no-op —
    ``False`` is returned without writing an event, changing
    state/revision/last_error, pausing runtime, or touching the new attempt's
    claims. The ``manual_http`` plane always passes ``target=None`` and is
    therefore always a no-op here — that plane never reaches a P0a
    ``processing_attempt`` row in the first place, since
    ``_ensure_manual_plane_writable`` already rejects any manual write while
    ``mode == "autonomous"``.
    """
    if target is None or target.session_id != session_id:
        return False
    if not isinstance(error_code, str) or _ERROR_CODE_RE.fullmatch(error_code) is None:
        error_code = "autopilot_worker_exception"
    observed_at = (
        autopilot_service._utc_naive(now)  # noqa: SLF001
        if now is not None else autopilot_service._utc_now_naive()  # noqa: SLF001
    )
    state = db.exec(select(SessionAutopilotState).where(
        SessionAutopilotState.session_id == session_id,
    ).with_for_update()).first()
    if state is None or state.scope_key != autopilot_service.P0A_SCOPE_KEY:
        return False
    if state.status != "processing_attempt" or state.current_command_id is None:
        return False
    if (state.current_command_id != target.record_command_id
            or state.control_generation != target.control_generation
            or state.runner_generation != target.runner_generation):
        # The frozen target no longer matches the current attempt: a newer
        # worker has already taken over. Never mutate someone else's attempt.
        return False
    record = db.exec(select(RuntimeCommand).where(
        RuntimeCommand.id == state.current_command_id,
        RuntimeCommand.session_id == session_id,
    ).with_for_update()).first()
    if (record is None or record.kind != "record" or record.state != "succeeded"
            or record.control_generation != state.control_generation
            or record.runner_generation != state.runner_generation):
        _fail("autopilot_failure_capture_invalid", "技术失败无法绑定当前录音命令")
    if record.expected_raw_audio_id != target.raw_audio_id:
        return False
    if (state.lease_owner is not None and state.lease_expires_at is not None
            and state.lease_expires_at > observed_at):
        _fail("autopilot_failure_state_busy", "attempt 路由状态正被其他 worker 占用")
    try:
        # Safety-close must survive feature disablement, withdrawal and audio
        # quarantine.  Those mutable gates correctly forbid new AI processing,
        # but cannot invalidate the immutable proof that patient media stopped.
        autopilot_ledger.verify_terminal_record_capture(db, record.id)
    except autopilot_ledger.AutopilotProofError as exc:
        raise AutopilotOrchestrationError(
            "autopilot_failure_capture_invalid", "技术失败前 capture proof 复核失败") from exc

    event_key = _event_key(session_id, record.id, error_code)
    prior = db.exec(select(AutopilotControlEvent).where(
        AutopilotControlEvent.idempotency_key == event_key,
    )).first()
    if prior is not None:
        _fail("autopilot_failure_event_conflict", "处理中状态仍存在但失败事实已存在")

    result = db.execute(
        update(SessionAutopilotState)
        .where(
            SessionAutopilotState.session_id == session_id,
            SessionAutopilotState.scope_key == state.scope_key,
            SessionAutopilotState.mode == "autonomous",
            SessionAutopilotState.status == "processing_attempt",
            SessionAutopilotState.current_command_id == target.record_command_id,
            SessionAutopilotState.control_generation == target.control_generation,
            SessionAutopilotState.runner_generation == target.runner_generation,
            SessionAutopilotState.revision == state.revision,
        )
        .values(
            status="paused",
            current_command_id=None,
            revision=SessionAutopilotState.revision + 1,
            last_error_code=error_code,
            lease_owner=None,
            lease_acquired_at=None,
            lease_expires_at=None,
            updated_at=observed_at,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        # Lost a race against another writer between our checks above and this
        # CAS — a newer worker just won. Quiet no-op, never a raised failure.
        return False
    # A safety-close may race an already-issued provider call.  Invalidate the
    # attempt lease in the same transaction as the control/runtime pause so the
    # late result cannot append patient-derived evidence.
    evidence_ledger.invalidate_processing_claims(
        db,
        session_id=session_id,
        raw_audio_id=record.expected_raw_audio_id,
    )
    # The provider call may still be in the pre-attempt capture-claim stage
    # (ASR not yet resolved); fence it the same way so a late ASR result
    # cannot materialize an AttemptEvent after this safety-close commits.
    evidence_ledger.invalidate_capture_processing_claims(
        db,
        session_id=session_id,
        raw_audio_id=record.expected_raw_audio_id,
    )
    db.add(AutopilotControlEvent(
        idempotency_key=event_key,
        session_id=session_id,
        event_seq=_next_control_event_seq(db, session_id),
        event_type="failure",
        scope_key=state.scope_key,
        control_generation=state.control_generation,
        runner_generation=state.runner_generation,
        command_id=record.id,
        actor_type="system",
        reason_code=error_code,
        from_mode="autonomous",
        to_mode="autonomous",
        from_status="processing_attempt",
        to_status="paused",
        payload_json=autopilot_ledger.encode_control_event_payload(
            "failure", {"error_code": error_code, "source": source}),
        created_at=observed_at,
    ))
    db.flush()
    return True


def submit(session_id: str, worker: Callable[[str], object]) -> bool:
    """Submit without waiting; duplicate in-process triggers share one worker.

    Database attempt/state leases remain authoritative across processes.  This map
    only prevents needless duplicate provider work in one API process.
    """
    # 任一通道开着都可能有需要处理的 attempt；per-session 的分类判定在
    # worker 内部的 _require_gate 里 fail-closed，这里只挡两通道全关的部署。
    if (not autopilot_service.p0a_feature_enabled()
            and not autopilot_service.real_sessions_enabled()):
        return False
    with _INFLIGHT_LOCK:
        existing = _INFLIGHT.get(session_id)
        if existing is not None and not existing.done():
            return False
        future = _EXECUTOR.submit(worker, session_id)
        _INFLIGHT[session_id] = future

        def _release(done: Future[object]) -> None:
            with _INFLIGHT_LOCK:
                if _INFLIGHT.get(session_id) is done:
                    _INFLIGHT.pop(session_id, None)

        future.add_done_callback(_release)
        return True


def inflight_for_tests(session_id: str) -> bool:
    """Read-only test probe; never used as a correctness or recovery signal."""
    with _INFLIGHT_LOCK:
        future = _INFLIGHT.get(session_id)
        return bool(future is not None and not future.done())
