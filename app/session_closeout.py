"""Pure, text-minimising contracts for session outcome and closeout records.

This module deliberately has no database dependency.  Callers project their
database rows onto the small protocols below, then persist the returned values
in the immutable ``SessionOutcomeSummary`` row.  Response/ASR text, judgement
reasons and interaction payloads are never read, returned or hashed.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import unicodedata
from typing import Protocol


SOURCE_SCHEMA_VERSION = "session-outcome-source-v1"
CLOSEOUT_SCHEMA_VERSION = "session-closeout.v1"
CLOSEOUT_REQUEST_SCHEMA_VERSION = "session-closeout-request-v1"

_ATTEMPT_STATUSES = {
    "received",
    "asr_completed",
    "completed",
    "technical_failure",
}
_CLOSEOUT_STATUSES = {
    "no_additional_observation",
    "observation_recorded",
}
CLOSEOUT_FLAG_FIELDS = (
    "fatigue_observed",
    "distress_or_discomfort_observed",
    "participant_declined_to_continue",
    "staff_assistance_occurred",
    "environment_interruption_occurred",
    "device_or_network_interruption_occurred",
)


class TurnEvidenceRow(Protocol):
    """One expected protocol turn and its closeout evidence projection."""

    item_id: str
    turn_seq: int
    matched: bool
    audio_evidenced: bool


@dataclass(frozen=True)
class SessionTurnEvidence:
    """Concrete text-free projection used by the API integration layer."""

    item_id: str
    turn_seq: int
    matched: bool
    audio_evidenced: bool


class AttemptEvidenceRow(Protocol):
    """Non-text fields from an attempt needed for the outcome summary."""

    item_id: str
    turn_seq: int
    attempt_seq: int
    raw_audio_id: str
    prompt_level: int
    processing_status: str
    operational_needs_review: bool | None


class InteractionEvidenceRow(Protocol):
    """Non-payload interaction fields needed for operational event counts."""

    event_seq: int
    event_type: str
    item_id: str | None
    turn_seq: int | None
    attempt_seq: int | None


@dataclass(frozen=True)
class SessionOutcomeValues:
    session_id: str
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

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class NormalizedCloseoutPayload:
    status: str
    fatigue_observed: bool
    distress_or_discomfort_observed: bool
    participant_declined_to_continue: bool
    staff_assistance_occurred: bool
    environment_interruption_occurred: bool
    device_or_network_interruption_occurred: bool
    note: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class CloseoutValidationError(ValueError):
    """Raised when a closeout payload cannot satisfy the research contract."""


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _optional_positive_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field)


def _strict_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_session_outcome_summary(
    *,
    session_id: str,
    schema_version: str,
    generator_version: str,
    item_bank_version_id: str,
    is_simulation: bool,
    data_classification: str,
    turn_evidence: Iterable[TurnEvidenceRow],
    attempts: Iterable[AttemptEvidenceRow],
    interactions: Iterable[InteractionEvidenceRow],
    generated_at: datetime,
) -> SessionOutcomeValues:
    """Build deterministic summary values from non-text evidence projections.

    Every ``turn_evidence`` row represents one expected frozen-plan turn.
    Duplicate or internally inconsistent evidence fails closed instead of being
    silently collapsed.  ``generated_at`` is persisted but intentionally omitted
    from ``source_digest`` so an identical source snapshot has an identical hash.
    """
    session_id = _required_text(session_id, "session_id")
    schema_version = _required_text(schema_version, "schema_version")
    generator_version = _required_text(generator_version, "generator_version")
    item_bank_version_id = _required_text(
        item_bank_version_id, "item_bank_version_id")
    is_simulation = _strict_bool(is_simulation, "is_simulation")
    if data_classification not in {"research", "simulation"}:
        raise ValueError("data_classification must be research or simulation")
    if is_simulation != (data_classification == "simulation"):
        raise ValueError("simulation flag and data classification disagree")
    if not isinstance(generated_at, datetime):
        raise ValueError("generated_at must be a datetime")

    canonical_turns: list[dict[str, object]] = []
    expected_keys: set[tuple[str, int]] = set()
    matched_keys: set[tuple[str, int]] = set()
    audio_keys: set[tuple[str, int]] = set()
    for row in turn_evidence:
        item_id = _required_text(row.item_id, "turn.item_id")
        turn_seq = _positive_int(row.turn_seq, "turn.turn_seq")
        matched = _strict_bool(row.matched, "turn.matched")
        audio_evidenced = _strict_bool(
            row.audio_evidenced, "turn.audio_evidenced")
        key = (item_id, turn_seq)
        if key in expected_keys:
            raise ValueError(f"duplicate expected turn: {item_id}#{turn_seq}")
        if audio_evidenced and not matched:
            raise ValueError("audio-evidenced turn must also be matched")
        expected_keys.add(key)
        if matched:
            matched_keys.add(key)
        if audio_evidenced:
            audio_keys.add(key)
        canonical_turns.append({
            "item_id": item_id,
            "turn_seq": turn_seq,
            "matched": matched,
            "audio_evidenced": audio_evidenced,
        })

    canonical_attempts: list[dict[str, object]] = []
    attempt_keys: set[tuple[str, int, int]] = set()
    completed_turn_keys: set[tuple[str, int]] = set()
    prompt_counts = [0, 0, 0, 0]
    completed_attempts = 0
    needs_review_attempts = 0
    technical_failure_attempts = 0
    for row in attempts:
        item_id = _required_text(row.item_id, "attempt.item_id")
        turn_seq = _positive_int(row.turn_seq, "attempt.turn_seq")
        attempt_seq = _positive_int(row.attempt_seq, "attempt.attempt_seq")
        raw_audio_id = _required_text(
            row.raw_audio_id, "attempt.raw_audio_id")
        prompt_level = row.prompt_level
        if (isinstance(prompt_level, bool) or not isinstance(prompt_level, int)
                or prompt_level < 0 or prompt_level > 3):
            raise ValueError("attempt.prompt_level must be an integer from 0 to 3")
        processing_status = _required_text(
            row.processing_status, "attempt.processing_status")
        if processing_status not in _ATTEMPT_STATUSES:
            raise ValueError("unsupported attempt processing_status")
        needs_review = row.operational_needs_review
        if needs_review is not None:
            needs_review = _strict_bool(
                needs_review, "attempt.operational_needs_review")

        turn_key = (item_id, turn_seq)
        if turn_key not in expected_keys:
            raise ValueError(f"attempt outside expected plan: {item_id}#{turn_seq}")
        key = (item_id, turn_seq, attempt_seq)
        if key in attempt_keys:
            raise ValueError(
                f"duplicate attempt: {item_id}#{turn_seq}@{attempt_seq}")
        attempt_keys.add(key)
        prompt_counts[prompt_level] += 1
        if processing_status == "completed":
            completed_attempts += 1
            completed_turn_keys.add(turn_key)
        if needs_review is True:
            needs_review_attempts += 1
        if processing_status == "technical_failure":
            technical_failure_attempts += 1
        canonical_attempts.append({
            "item_id": item_id,
            "turn_seq": turn_seq,
            "attempt_seq": attempt_seq,
            "raw_audio_id": raw_audio_id,
            "prompt_level": prompt_level,
            "processing_status": processing_status,
            "operational_needs_review": needs_review,
        })

    if not completed_turn_keys.issubset(matched_keys):
        raise ValueError("completed attempt turn must also be matched")

    canonical_interactions: list[dict[str, object]] = []
    interaction_sequences: set[int] = set()
    technical_pause_count = 0
    researcher_takeover_count = 0
    for row in interactions:
        event_seq = _positive_int(row.event_seq, "interaction.event_seq")
        if event_seq in interaction_sequences:
            raise ValueError(f"duplicate interaction event_seq: {event_seq}")
        interaction_sequences.add(event_seq)
        event_type = _required_text(row.event_type, "interaction.event_type")
        item_id = row.item_id
        if item_id is not None:
            item_id = _required_text(item_id, "interaction.item_id")
        turn_seq = _optional_positive_int(
            row.turn_seq, "interaction.turn_seq")
        attempt_seq = _optional_positive_int(
            row.attempt_seq, "interaction.attempt_seq")
        if event_type == "technical_pause":
            technical_pause_count += 1
        elif event_type == "researcher_takeover":
            researcher_takeover_count += 1
        canonical_interactions.append({
            "event_seq": event_seq,
            "event_type": event_type,
            "item_id": item_id,
            "turn_seq": turn_seq,
            "attempt_seq": attempt_seq,
        })

    canonical_turns.sort(key=lambda row: (row["item_id"], row["turn_seq"]))
    canonical_attempts.sort(key=lambda row: (
        row["item_id"], row["turn_seq"], row["attempt_seq"],
        row["raw_audio_id"],
    ))
    canonical_interactions.sort(key=lambda row: row["event_seq"])
    source_projection = {
        "source_schema_version": SOURCE_SCHEMA_VERSION,
        "session": {
            "session_id": session_id,
            "item_bank_version_id": item_bank_version_id,
            "is_simulation": is_simulation,
            "data_classification": data_classification,
        },
        "turns": canonical_turns,
        "attempts": canonical_attempts,
        "interactions": canonical_interactions,
    }
    source_digest = hashlib.sha256(
        _canonical_bytes(source_projection)).hexdigest()

    return SessionOutcomeValues(
        session_id=session_id,
        schema_version=schema_version,
        generator_version=generator_version,
        item_bank_version_id=item_bank_version_id,
        is_simulation=is_simulation,
        data_classification=data_classification,
        expected_turns=len(expected_keys),
        matched_turns=len(matched_keys),
        completed_attempt_turns=len(completed_turn_keys),
        audio_evidenced_turns=len(audio_keys),
        total_attempts=len(attempt_keys),
        completed_attempts=completed_attempts,
        needs_review_attempts=needs_review_attempts,
        technical_failure_attempts=technical_failure_attempts,
        prompt_level_0_count=prompt_counts[0],
        prompt_level_1_count=prompt_counts[1],
        prompt_level_2_count=prompt_counts[2],
        prompt_level_3_count=prompt_counts[3],
        technical_pause_count=technical_pause_count,
        researcher_takeover_count=researcher_takeover_count,
        source_digest=source_digest,
        generated_at=generated_at,
    )


def normalize_closeout_payload(
    payload: Mapping[str, object],
) -> NormalizedCloseoutPayload:
    """Strictly validate and canonicalise an account-authored closeout payload."""
    allowed = {"status", "note", *CLOSEOUT_FLAG_FIELDS}
    unknown = set(payload) - allowed
    if unknown:
        raise CloseoutValidationError(
            f"unknown closeout fields: {', '.join(sorted(unknown))}")

    status_value = payload.get("status")
    if not isinstance(status_value, str):
        raise CloseoutValidationError("status must be a string")
    status = status_value.strip()
    if status not in _CLOSEOUT_STATUSES:
        raise CloseoutValidationError("unsupported closeout status")

    flags: dict[str, bool] = {}
    for field in CLOSEOUT_FLAG_FIELDS:
        value = payload.get(field, False)
        if type(value) is not bool:
            raise CloseoutValidationError(f"{field} must be a boolean")
        flags[field] = value

    raw_note = payload.get("note")
    if raw_note is not None and not isinstance(raw_note, str):
        raise CloseoutValidationError("note must be a string or null")
    note = None
    if isinstance(raw_note, str):
        note = unicodedata.normalize("NFC", raw_note.strip()) or None
    if note is not None and len(note) > 2000:
        raise CloseoutValidationError("note must not exceed 2000 characters")

    has_observation = any(flags.values()) or note is not None
    if status == "no_additional_observation" and has_observation:
        raise CloseoutValidationError(
            "no_additional_observation cannot contain flags or a note")
    if status == "observation_recorded" and not has_observation:
        raise CloseoutValidationError(
            "observation_recorded requires at least one flag or a note")

    return NormalizedCloseoutPayload(status=status, note=note, **flags)


def closeout_request_hash(
    payload: Mapping[str, object] | NormalizedCloseoutPayload,
) -> str:
    """Return a stable SHA-256 hash for idempotency conflict detection."""
    normalized = (
        payload
        if isinstance(payload, NormalizedCloseoutPayload)
        else normalize_closeout_payload(payload)
    )
    canonical = {
        "request_schema_version": CLOSEOUT_REQUEST_SCHEMA_VERSION,
        **normalized.to_dict(),
    }
    return hashlib.sha256(_canonical_bytes(canonical)).hexdigest()


def closeout_operation_hash(
    payload: Mapping[str, object] | NormalizedCloseoutPayload,
    *,
    expected_revision: int,
) -> str:
    """Hash the full CAS command, not only its observation payload."""
    if (isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0):
        raise CloseoutValidationError(
            "expected_revision must be a non-negative integer")
    normalized = (
        payload
        if isinstance(payload, NormalizedCloseoutPayload)
        else normalize_closeout_payload(payload)
    )
    canonical = {
        "request_schema_version": CLOSEOUT_REQUEST_SCHEMA_VERSION,
        "expected_revision": expected_revision,
        "payload": normalized.to_dict(),
    }
    return hashlib.sha256(_canonical_bytes(canonical)).hexdigest()
