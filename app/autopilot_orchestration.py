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
import re
import threading
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, update
from sqlmodel import Session, select

from . import (autopilot_ledger, autopilot_positions, autopilot_service, content,
               evidence_ledger, runtime)
from .models import (
    AttemptEvent,
    AutopilotControlEvent,
    RuntimeCommand,
    Session as TrainSession,
    SessionAutopilotState,
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
) -> tuple[str, str | None]:
    event_line = getattr(train_session.event_line, "value", train_session.event_line)
    try:
        plan = runtime.build_session_plan(
            bank, train_session.week_no, str(event_line))
    except ValueError as exc:
        raise AutopilotOrchestrationError(
            "autopilot_attempt_plan_invalid", "P0a 冻结计划不可用") from exc
    positions = autopilot_positions.plan_positions(plan)
    try:
        position = autopilot_positions.find_position(
            positions, item_id=record.item_id, turn_seq=record.turn_seq)
    except ValueError as exc:
        raise AutopilotOrchestrationError(
            "autopilot_attempt_plan_mismatch", "录音命令不属于冻结计划") from exc
    gap = autopilot_positions.readiness_gap(bank, position)
    if gap is not None:
        _fail(gap.code, gap.detail)
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
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
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
    except autopilot_service.AutopilotServiceError as exc:
        raise AutopilotOrchestrationError(exc.code, exc.message) from exc
    try:
        proof = autopilot_ledger.verify_record_capture_for_attempt(db, record.id)
    except autopilot_ledger.AutopilotProofError as exc:
        raise AutopilotOrchestrationError(
            "autopilot_attempt_capture_invalid", "P0a attempt 采集证据链不完整") from exc
    if (gate.train_session.is_simulation is not True
            or gate.train_session.data_classification != "simulation"
            or gate.patient.is_simulation_subject is not True):
        _fail("autopilot_attempt_boundary_invalid", "attempt orchestration 仅允许 P0a 模拟数据")
    if proof.attempt_seq != proof.prompt_level + 1:
        _fail("autopilot_attempt_sequence_invalid", "capture attempt_seq 与提示序列不单调")

    response_role, cue_type = _frozen_attempt_context(
        gate.train_session, record, bank)
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
            existing.is_simulation is True,
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
    if not autopilot_service.p0a_feature_enabled():
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
