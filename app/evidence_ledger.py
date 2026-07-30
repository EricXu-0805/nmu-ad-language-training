"""AI 运行证据账本的封闭事件和载荷契约。

互动表不是第二份转写或画像库。载荷只允许运行元数据，回答文本只保留在
AttemptEvent.asr_text，画像字段在这个边界被递归拒绝。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .judging import PORTRAIT_FIELDS, PortraitLeakError
from .models import AttemptCaptureProcessing, AttemptEvent


RECOVERABLE_ATTEMPT_STATUSES = frozenset({"received", "asr_completed"})
TERMINAL_ATTEMPT_STATUSES = frozenset({"completed", "technical_failure"})
ATTEMPT_LEASE_SECONDS = 90
RECOVERABLE_CAPTURE_STATUSES = frozenset({"received"})
TERMINAL_CAPTURE_STATUSES = frozenset({"asr_completed", "technical_failure"})
CAPTURE_LEASE_SECONDS = 90
PATIENT_REC_FAILURE_ID_PATTERN = (
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[1-8][0-9A-Fa-f]{3}-"
    r"[89AaBb][0-9A-Fa-f]{3}-[0-9A-Fa-f]{12}$")
PATIENT_REC_FAILURE_CODES = frozenset({
    "microphone_start_timeout",
    "microphone_permission_denied",
    "microphone_not_found",
    "microphone_start_failed",
    "recording_authorization_failed",
})
_PATIENT_REC_FAILURE_ID_RE = re.compile(PATIENT_REC_FAILURE_ID_PATTERN)


@dataclass(frozen=True)
class AttemptClaim:
    """A persisted attempt-processing lease plus its fencing generation."""

    attempt_id: int
    owner: str
    generation: int
    stage: Literal["received", "asr_completed"]
    lease_expires_at: datetime


def new_claim_owner() -> str:
    """Return an opaque per-request owner token; never derive it from host/user data."""
    return secrets.token_urlsafe(24)


def lease_deadline(*, now: datetime | None = None,
                   seconds: int = ATTEMPT_LEASE_SECONDS) -> datetime:
    return (now or datetime.now()) + timedelta(seconds=seconds)


def has_active_lease(attempt: AttemptEvent, *, now: datetime | None = None) -> bool:
    return bool(
        attempt.processing_status in RECOVERABLE_ATTEMPT_STATUSES
        and attempt.processing_owner
        and attempt.processing_lease_expires_at
        and attempt.processing_lease_expires_at > (now or datetime.now())
    )


def claim_from_attempt(attempt: AttemptEvent) -> AttemptClaim:
    if attempt.id is None:
        raise ValueError("attempt must be persisted before it can be claimed")
    if attempt.processing_status not in RECOVERABLE_ATTEMPT_STATUSES:
        raise ValueError(f"attempt status {attempt.processing_status} is not recoverable")
    if not attempt.processing_owner or attempt.processing_lease_expires_at is None:
        raise ValueError("attempt does not hold a complete processing lease")
    return AttemptClaim(
        attempt_id=attempt.id,
        owner=attempt.processing_owner,
        generation=attempt.processing_generation,
        stage=attempt.processing_status,
        lease_expires_at=attempt.processing_lease_expires_at,
    )


def try_claim_attempt(session: Session, attempt_id: int, *, owner: str,
                      now: datetime | None = None,
                      lease_seconds: int = ATTEMPT_LEASE_SECONDS) -> bool:
    """Atomically claim an unowned/expired recoverable attempt.

    A single conditional UPDATE is the concurrency boundary. PostgreSQL obtains a row
    lock for the update; SQLite serializes writers. A competing worker observes either
    the renewed lease or a terminal stage and therefore cannot also claim generation N.
    The caller must commit before invoking an external engine.
    """
    claimed_at = now or datetime.now()
    result = session.execute(
        update(AttemptEvent)
        .where(
            AttemptEvent.id == attempt_id,
            AttemptEvent.processing_status.in_(RECOVERABLE_ATTEMPT_STATUSES),
            or_(
                AttemptEvent.processing_owner.is_(None),
                AttemptEvent.processing_lease_expires_at.is_(None),
                AttemptEvent.processing_lease_expires_at <= claimed_at,
            ),
        )
        .values(
            processing_owner=owner,
            processing_claimed_at=claimed_at,
            processing_lease_expires_at=lease_deadline(
                now=claimed_at, seconds=lease_seconds),
            processing_generation=AttemptEvent.processing_generation + 1,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def invalidate_processing_claims(
    session: Session,
    *,
    session_id: str,
    raw_audio_id: str | None = None,
) -> int:
    """Fence every in-flight worker covered by the selected session/audio scope.

    Safe pause/takeover must win against a provider call that has already left the
    process.  Such a call cannot be cancelled reliably, so the control transaction
    advances ``processing_generation`` and clears its lease.  Any late worker then
    loses the existing :func:`fenced_attempt_update` CAS and may only read back the
    persisted state.

    This changes lease metadata only.  ASR/judgement facts and terminal evidence
    remain immutable, and a later explicit manual retry may claim the recoverable
    attempt as a new generation.
    """
    conditions = [
        AttemptEvent.session_id == session_id,
        AttemptEvent.processing_status.in_(RECOVERABLE_ATTEMPT_STATUSES),
    ]
    if raw_audio_id is not None:
        conditions.append(AttemptEvent.raw_audio_id == raw_audio_id)
    result = session.execute(
        update(AttemptEvent)
        .where(*conditions)
        .values(
            processing_owner=None,
            processing_lease_expires_at=None,
            processing_claimed_at=None,
            processing_generation=AttemptEvent.processing_generation + 1,
        )
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def fenced_attempt_update(session: Session, claim: AttemptClaim, *,
                          expected_status: Literal["received", "asr_completed"],
                          next_status: Literal[
                              "asr_completed", "completed", "technical_failure"],
                          values: dict[str, Any]) -> bool:
    """Update an attempt only while ``claim`` is still the newest owner generation.

    Lease expiry alone does not invalidate a finishing worker: expiry makes takeover
    possible. Whichever conditional UPDATE (takeover or completion) wins first fences
    out the other, avoiding a gap where nobody can safely persist a completed call.
    """
    allowed_next = {
        "received": {"asr_completed", "technical_failure"},
        "asr_completed": {"completed", "technical_failure"},
    }[expected_status]
    if next_status not in allowed_next:
        raise ValueError(f"invalid attempt transition {expected_status} -> {next_status}")
    forbidden = {
        "id", "processing_owner", "processing_generation", "processing_status",
        "processing_lease_expires_at", "processing_claimed_at",
    } & set(values)
    if forbidden:
        raise ValueError(f"fenced update contains protected fields: {sorted(forbidden)}")
    transition_values = dict(values)
    transition_values["processing_status"] = next_status
    if next_status in TERMINAL_ATTEMPT_STATUSES:
        transition_values.update(
            processing_owner=None,
            processing_lease_expires_at=None,
        )
    else:
        transition_values["processing_lease_expires_at"] = lease_deadline()
    result = session.execute(
        update(AttemptEvent)
        .where(
            AttemptEvent.id == claim.attempt_id,
            AttemptEvent.processing_status == expected_status,
            AttemptEvent.processing_owner == claim.owner,
            AttemptEvent.processing_generation == claim.generation,
        )
        .values(**transition_values)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


@dataclass(frozen=True)
class CaptureClaim:
    """A persisted capture-processing lease plus its fencing generation.

    ``proof_attempt_seq``/``proof_prompt_level`` are copied from the row at
    claim time so the eventual AttemptEvent can be created using the exact
    server-issued sequence instead of re-deriving/guessing it.
    """

    capture_id: int
    owner: str
    generation: int
    proof_attempt_seq: int
    proof_prompt_level: int
    lease_expires_at: datetime


def claim_from_capture(capture: AttemptCaptureProcessing) -> CaptureClaim:
    if capture.id is None:
        raise ValueError("capture must be persisted before it can be claimed")
    if capture.processing_status not in RECOVERABLE_CAPTURE_STATUSES:
        raise ValueError(f"capture status {capture.processing_status} is not recoverable")
    if not capture.processing_owner or capture.processing_lease_expires_at is None:
        raise ValueError("capture does not hold a complete processing lease")
    return CaptureClaim(
        capture_id=capture.id,
        owner=capture.processing_owner,
        generation=capture.processing_generation,
        proof_attempt_seq=capture.proof_attempt_seq,
        proof_prompt_level=capture.proof_prompt_level,
        lease_expires_at=capture.processing_lease_expires_at,
    )


def has_active_capture_lease(
        capture: AttemptCaptureProcessing, *, now: datetime | None = None) -> bool:
    return bool(
        capture.processing_status in RECOVERABLE_CAPTURE_STATUSES
        and capture.processing_owner
        and capture.processing_lease_expires_at
        and capture.processing_lease_expires_at > (now or datetime.now())
    )


def try_claim_capture(session: Session, capture_id: int, *, owner: str,
                      now: datetime | None = None,
                      lease_seconds: int = CAPTURE_LEASE_SECONDS) -> bool:
    """Atomically claim an unowned/expired capture row. Mirrors :func:`try_claim_attempt`.

    The caller must commit before invoking ASR; a competing worker observes
    either the renewed lease or a terminal disposition and cannot also claim
    generation N.
    """
    claimed_at = now or datetime.now()
    result = session.execute(
        update(AttemptCaptureProcessing)
        .where(
            AttemptCaptureProcessing.id == capture_id,
            AttemptCaptureProcessing.processing_status.in_(RECOVERABLE_CAPTURE_STATUSES),
            or_(
                AttemptCaptureProcessing.processing_owner.is_(None),
                AttemptCaptureProcessing.processing_lease_expires_at.is_(None),
                AttemptCaptureProcessing.processing_lease_expires_at <= claimed_at,
            ),
        )
        .values(
            processing_owner=owner,
            processing_claimed_at=claimed_at,
            processing_lease_expires_at=lease_deadline(
                now=claimed_at, seconds=lease_seconds),
            processing_generation=AttemptCaptureProcessing.processing_generation + 1,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def invalidate_capture_processing_claims(
    session: Session,
    *,
    session_id: str,
    raw_audio_id: str | None = None,
) -> int:
    """Fence every in-flight capture worker covered by the session/audio scope.

    Mirrors :func:`invalidate_processing_claims` for the pre-attempt capture
    claim: pause/takeover/withdrawal/feature-disable must fence both claims in
    the same control transaction so a late ASR result cannot create an
    AttemptEvent after the runtime has already been paused/withdrawn.
    """
    conditions = [
        AttemptCaptureProcessing.session_id == session_id,
        AttemptCaptureProcessing.processing_status.in_(RECOVERABLE_CAPTURE_STATUSES),
    ]
    if raw_audio_id is not None:
        conditions.append(AttemptCaptureProcessing.raw_audio_id == raw_audio_id)
    result = session.execute(
        update(AttemptCaptureProcessing)
        .where(*conditions)
        .values(
            processing_owner=None,
            processing_lease_expires_at=None,
            processing_claimed_at=None,
            processing_generation=AttemptCaptureProcessing.processing_generation + 1,
        )
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def confirm_capture_claim(session: Session, capture_id: int, *,
                          owner: str, generation: int,
                          now: datetime | None = None) -> bool:
    """Atomically prove ``owner``/``generation`` still own this ``received`` row.

    A real conditional UPDATE, not a SELECT-then-compare: a SELECT (even with
    ``.with_for_update()``) is not a genuine mutual-exclusion primitive on
    SQLite, which ignores ``FOR UPDATE``, and an in-process lock does not
    cover a multi-process deployment either way. This UPDATE's WHERE clause is
    evaluated atomically against the current committed row on any engine, so
    a stale/fenced-out caller's owner/generation simply fails to match and
    the call safely returns ``False`` — no separate locking primitive needed.

    Refreshing the lease is a harmless side effect; owner and generation are
    left untouched, so the caller's existing :class:`CaptureClaim` remains
    valid for a later :func:`fenced_capture_update` using the same
    generation. Callers that must do work (sequence checks, inserts) *before*
    that final fenced write use this first to fail fast: a stale/fenced-out
    worker must never reach a sequence check or insert using an
    already-superseded claim.
    """
    result = session.execute(
        update(AttemptCaptureProcessing)
        .where(
            AttemptCaptureProcessing.id == capture_id,
            AttemptCaptureProcessing.processing_status == "received",
            AttemptCaptureProcessing.processing_owner == owner,
            AttemptCaptureProcessing.processing_generation == generation,
        )
        .values(processing_lease_expires_at=lease_deadline(now=now))
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def fenced_capture_update(session: Session, claim: CaptureClaim, *,
                         next_status: Literal["asr_completed", "technical_failure"],
                         values: dict[str, Any]) -> bool:
    """Transition a capture row out of ``received`` only while ``claim`` still owns it.

    Both terminal outcomes clear owner/lease the same way
    :func:`fenced_attempt_update` clears them for AttemptEvent's terminal
    statuses; a capture row has no non-terminal outcome after ``received``.
    """
    if next_status not in {"asr_completed", "technical_failure"}:
        raise ValueError(f"invalid capture transition received -> {next_status}")
    forbidden = {
        "id", "processing_owner", "processing_generation", "processing_status",
        "processing_lease_expires_at", "processing_claimed_at",
        "record_command_id", "predecessor_command_id", "receipt_server_seq",
        "raw_audio_id", "session_id", "item_id", "turn_seq",
        "proof_attempt_seq", "proof_prompt_level", "created_at", "is_simulation",
    } & set(values)
    if forbidden:
        raise ValueError(f"fenced capture update contains protected fields: {sorted(forbidden)}")
    transition_values = dict(values)
    transition_values["processing_status"] = next_status
    transition_values["processing_owner"] = None
    transition_values["processing_lease_expires_at"] = None
    result = session.execute(
        update(AttemptCaptureProcessing)
        .where(
            AttemptCaptureProcessing.id == claim.capture_id,
            AttemptCaptureProcessing.processing_status == "received",
            AttemptCaptureProcessing.processing_owner == claim.owner,
            AttemptCaptureProcessing.processing_generation == claim.generation,
        )
        .values(**transition_values)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def _capture_identity_tuple(
    *, predecessor_command_id: int, receipt_server_seq: int, raw_audio_id: str,
    session_id: str, item_id: str, turn_seq: int, proof_attempt_seq: int,
    proof_prompt_level: int, is_simulation: bool,
) -> tuple:
    return (predecessor_command_id, receipt_server_seq, raw_audio_id, session_id,
            item_id, turn_seq, proof_attempt_seq, proof_prompt_level, is_simulation)


def _verify_capture_identity(
        row: AttemptCaptureProcessing, *, record_command_id: int, **identity: Any,
) -> None:
    expected = _capture_identity_tuple(**identity)
    actual = _capture_identity_tuple(
        predecessor_command_id=row.predecessor_command_id,
        receipt_server_seq=row.receipt_server_seq,
        raw_audio_id=row.raw_audio_id,
        session_id=row.session_id,
        item_id=row.item_id,
        turn_seq=row.turn_seq,
        proof_attempt_seq=row.proof_attempt_seq,
        proof_prompt_level=row.proof_prompt_level,
        is_simulation=row.is_simulation,
    )
    if expected != actual:
        raise ValueError(
            "capture-processing identity conflict for record_command_id="
            f"{record_command_id}: existing row does not match requested facts")


def ensure_capture_processing(
    session: Session, *,
    record_command_id: int,
    predecessor_command_id: int,
    receipt_server_seq: int,
    raw_audio_id: str,
    session_id: str,
    item_id: str,
    turn_seq: int,
    proof_attempt_seq: int,
    proof_prompt_level: int,
    is_simulation: bool,
    now: datetime | None = None,
) -> AttemptCaptureProcessing:
    """Idempotently admit one succeeded record command's persistent capture claim.

    Safe to call more than once for the same ``record_command_id``: a prior
    row is returned unchanged, never re-inserted or overwritten — but only
    after every immutable identity field is verified to match exactly; a
    conflicting replay fails closed rather than silently returning the wrong
    row. The caller must hold whatever row locks make ``record_command_id``
    stable for the duration of this call (the same transaction that just
    verified the record/receipt/audio capture proof).
    """
    identity = dict(
        predecessor_command_id=predecessor_command_id,
        receipt_server_seq=receipt_server_seq, raw_audio_id=raw_audio_id,
        session_id=session_id, item_id=item_id, turn_seq=turn_seq,
        proof_attempt_seq=proof_attempt_seq, proof_prompt_level=proof_prompt_level,
        is_simulation=is_simulation,
    )
    existing = session.exec(select(AttemptCaptureProcessing).where(
        AttemptCaptureProcessing.record_command_id == record_command_id,
    ).with_for_update()).first()
    if existing is not None:
        _verify_capture_identity(
            existing, record_command_id=record_command_id, **identity)
        return existing
    row = AttemptCaptureProcessing(
        record_command_id=record_command_id,
        processing_status="received",
        created_at=now or datetime.now(),
        **identity,
    )
    try:
        # A plain ``session.rollback()`` here would discard the caller's
        # entire outer transaction — e.g. apply_device_ack's ACK/command/
        # state changes staged earlier in the same request. Scope the risky
        # insert to its own SAVEPOINT so a conflict only unwinds this insert;
        # the surrounding transaction's prior work survives untouched.
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        # A concurrent admission for the same record command won the insert
        # race between our SELECT and this flush. Reload and verify exact
        # identity rather than trusting the record_command_id hit alone.
        recovered = session.exec(select(AttemptCaptureProcessing).where(
            AttemptCaptureProcessing.record_command_id == record_command_id,
        )).first()
        if recovered is None:
            raise
        _verify_capture_identity(
            recovered, record_command_id=record_command_id, **identity)
        return recovered
    return row


EVENT_PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    "attempt_received": frozenset({
        "raw_audio_id", "prompt_level", "cue_type", "duration_seconds",
        "processing_status",
    }),
    "asr_completed": frozenset({
        "asr_engine_version", "asr_confidence", "degraded", "hotword_hit",
    }),
    "asr_failed": frozenset({"asr_engine_version", "error_code"}),
    "judgement_completed": frozenset({
        "answer_type", "score", "needs_review", "judge_mode",
        "judge_engine_version", "matched_on", "contains_target",
        "truth_scope",
    }),
    "judgement_failed": frozenset({"judge_engine_version", "error_code"}),
    "cue_selected": frozenset({"prompt_level", "cue_type"}),
    "feedback_selected": frozenset({"feedback_key"}),
    # failure_id is optional: AI/manual technical pauses predate device-failure
    # idempotency and continue to carry only error_code.
    "technical_pause": frozenset({"error_code", "failure_id"}),
    "researcher_takeover": frozenset({"reason_code"}),
}

MANUAL_EVENT_TYPES = frozenset({
    "cue_selected", "feedback_selected", "technical_pause", "researcher_takeover",
})

_MAX_STRING_VALUE = 256


def technical_pause_request_hash(
    session_id: str,
    request_fields: dict[str, Any],
) -> str:
    """Hash the exact atomic technical-pause command contract.

    Both the write path and read-only quality projection use this helper.  Keeping
    the canonicalization here prevents a dashboard-side approximation from
    accepting a receipt that the command endpoint could never have produced.
    Callers remain responsible for validating the request field types before
    invoking the helper.
    """
    canonical = {
        "schema_version": 1,
        "session_id": session_id,
        **{key: value for key, value in request_fields.items() if value is not None},
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def valid_patient_rec_failure_fact(
    error_code: object,
    failure_id: object,
) -> bool:
    """Validate the closed patient-device microphone failure identity."""
    return bool(
        isinstance(error_code, str)
        and error_code in PATIENT_REC_FAILURE_CODES
        and isinstance(failure_id, str)
        and _PATIENT_REC_FAILURE_ID_RE.fullmatch(failure_id) is not None
    )


def _assert_no_portrait_keys(value: Any) -> None:
    if isinstance(value, dict):
        leaked = PORTRAIT_FIELDS & set(value)
        if leaked:
            raise PortraitLeakError(
                f"互动证据载荷混入画像字段 {sorted(leaked)}")
        for nested in value.values():
            _assert_no_portrait_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _assert_no_portrait_keys(nested)


def validate_event_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """验证并返回可序列化的载荷副本。

    不允许嵌套对象/数组，也不允许长文本：事件账本只存受控键和短元数据。
    """
    allowed = EVENT_PAYLOAD_KEYS.get(event_type)
    if allowed is None:
        raise ValueError(f"未知 interaction event_type: {event_type}")
    if not isinstance(payload, dict):
        raise ValueError("interaction payload 必须是对象")
    _assert_no_portrait_keys(payload)
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"{event_type} payload 含未授权字段 {sorted(unknown)}")
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            clean[key] = None
        elif isinstance(value, bool):
            clean[key] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{event_type}.{key} 必须是有限数")
            clean[key] = value
        elif isinstance(value, str):
            if len(value) > _MAX_STRING_VALUE:
                raise ValueError(f"{event_type}.{key} 过长")
            clean[key] = value
        else:
            raise ValueError(f"{event_type}.{key} 只允许标量值")
    return clean


def encode_event_payload(event_type: str, payload: dict[str, Any]) -> str:
    return json.dumps(
        validate_event_payload(event_type, payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def validate_stored_payload(event_type: str, payload_json: str) -> dict[str, Any]:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{event_type} payload_json 不是合法 JSON") from exc
    return validate_event_payload(event_type, payload)
