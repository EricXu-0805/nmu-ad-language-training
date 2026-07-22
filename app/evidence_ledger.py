"""AI 运行证据账本的封闭事件和载荷契约。

互动表不是第二份转写或画像库。载荷只允许运行元数据，回答文本只保留在
AttemptEvent.asr_text，画像字段在这个边界被递归拒绝。
"""
from __future__ import annotations

import json
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import or_, update
from sqlmodel import Session

from .judging import PORTRAIT_FIELDS, PortraitLeakError
from .models import AttemptEvent


RECOVERABLE_ATTEMPT_STATUSES = frozenset({"received", "asr_completed"})
TERMINAL_ATTEMPT_STATUSES = frozenset({"completed", "technical_failure"})
ATTEMPT_LEASE_SECONDS = 90


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
