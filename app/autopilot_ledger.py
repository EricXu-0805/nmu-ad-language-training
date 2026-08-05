"""Persistence contracts for the server-authoritative autopilot.

This module deliberately does not call TTS/ASR/judge providers and does not expose
HTTP endpoints.  Every claim helper performs one short conditional UPDATE; callers
must commit the claim before doing device or provider I/O.  Generation and revision
fences then prevent a late worker or ACK from mutating a newer control epoch.
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import or_, update
from sqlmodel import Session, select

from .autopilot_contract import RecordCommandPayload, TtsCommandPayload
from .enums import AudioStatus
from .models import (
    AudioAssetRow,
    AudioCaptureReceipt,
    Patient,
    PatientDeviceCapability,
    RuntimeCommand,
    RuntimeCommandAck,
    Session as TrainSession,
    SessionAutopilotState,
    SessionRuntimeState,
)


AUTOPILOT_LEASE_SECONDS = 30


def attempt_failure_event_key(
    session_id: str,
    command_id: int,
    error_code: str,
) -> str:
    """Derive the immutable idempotency key for one attempt failure boundary."""
    digest = hashlib.sha256(
        f"{session_id}\x00{command_id}\x00{error_code}".encode("utf-8")
    ).hexdigest()
    return f"attempt-failure-{digest}"
COMMAND_LEASE_SECONDS = 30
CLAIMABLE_AUTOPILOT_STATUSES = frozenset({
    "idle", "running",
})
WAITING_AUTOPILOT_STATUSES = frozenset({"waiting_tts", "waiting_recording"})
CLAIMABLE_COMMAND_STATES = frozenset({"pending", "started"})
TERMINAL_COMMAND_STATES = frozenset({"succeeded", "failed", "cancelled"})

_ACK_IDEMPOTENT_FIELDS = (
    "command_id", "idempotency_key", "session_id", "ack_type",
    "command_revision", "control_generation", "runner_generation",
    "device_event_seq", "device_id_hash", "capability_token_hash", "payload_json",
    "receipt_server_seq", "raw_audio_id", "checksum", "byte_count",
    "duration_seconds",
)


class AutopilotProofError(ValueError):
    """A persisted command/ACK/receipt chain is incomplete or contradictory."""


@dataclass(frozen=True)
class AutopilotClaim:
    session_id: str
    owner: str
    scope_key: str
    control_generation: int
    runner_generation: int
    revision: int
    lease_expires_at: datetime


@dataclass(frozen=True)
class CommandClaim:
    command_id: int
    owner: str
    scope_key: str
    control_generation: int
    runner_generation: int
    revision: int
    kind: Literal["tts", "record"]
    state: Literal["pending", "started"]
    lease_expires_at: datetime


@dataclass(frozen=True)
class AttemptCaptureProof:
    command_id: int
    session_id: str
    item_id: str
    turn_seq: int
    attempt_seq: int
    prompt_level: int
    raw_audio_id: str
    receipt_server_seq: int
    checksum: str
    byte_count: int
    duration_seconds: float


def new_lease_owner() -> str:
    """Return an opaque worker identifier; never derive it from user/device data."""
    return secrets.token_urlsafe(24)


def utc_now_naive() -> datetime:
    """Return UTC for comparison with PatientDeviceCapability timestamps."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def lease_deadline(*, now: datetime | None = None,
                   seconds: int = AUTOPILOT_LEASE_SECONDS) -> datetime:
    if seconds < 1:
        raise ValueError("lease seconds must be positive")
    return (now or utc_now_naive()) + timedelta(seconds=seconds)


def try_claim_autopilot_state(
    session: Session,
    session_id: str,
    *,
    owner: str,
    expected_revision: int,
    expected_control_generation: int,
    now: datetime | None = None,
    lease_seconds: int = AUTOPILOT_LEASE_SECONDS,
) -> bool:
    """Atomically claim an autonomous state row and advance its runner fence.

    ``manual_draining`` is intentionally not claimable: takeover is an explicit
    control transition, not an expired worker being silently replaced.  The two
    ``waiting_*`` states are also excluded because blindly increasing
    ``runner_generation`` would make the in-flight command and its delayed ACK stale.
    Use :func:`try_recover_waiting_autopilot_state` after checking the exact current
    command.  Commit a successful claim before any provider/device I/O.
    """
    claimed_at = now or utc_now_naive()
    result = session.execute(
        update(SessionAutopilotState)
        .where(
            SessionAutopilotState.session_id == session_id,
            SessionAutopilotState.mode == "autonomous",
            SessionAutopilotState.scope_key == "p0a_sim_first_single_v1",
            SessionAutopilotState.status.in_(CLAIMABLE_AUTOPILOT_STATUSES),
            SessionAutopilotState.revision == expected_revision,
            SessionAutopilotState.control_generation == expected_control_generation,
            or_(
                SessionAutopilotState.lease_owner.is_(None),
                SessionAutopilotState.lease_expires_at.is_(None),
                SessionAutopilotState.lease_expires_at <= claimed_at,
            ),
        )
        .values(
            lease_owner=owner,
            lease_acquired_at=claimed_at,
            lease_expires_at=lease_deadline(now=claimed_at, seconds=lease_seconds),
            runner_generation=SessionAutopilotState.runner_generation + 1,
            revision=SessionAutopilotState.revision + 1,
            updated_at=claimed_at,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def try_recover_waiting_autopilot_state(
    session: Session,
    session_id: str,
    *,
    current_command_id: int,
    owner: str,
    expected_revision: int,
    expected_control_generation: int,
    expected_runner_generation: int,
    now: datetime | None = None,
    lease_seconds: int = AUTOPILOT_LEASE_SECONDS,
) -> bool:
    """Transfer an expired waiting-state lease without invalidating its command ACK.

    The API must first load ``current_command_id`` and prove that its session and both
    generations match this state.  This CAS deliberately preserves
    ``runner_generation``; it only changes the worker owner/revision.  Thus an ACK
    already emitted by the paired device remains current while the old worker is
    fenced by owner + revision.
    """
    command = session.get(RuntimeCommand, current_command_id)
    if (command is None or command.session_id != session_id
            or command.control_generation != expected_control_generation
            or command.runner_generation != expected_runner_generation
            or command.state not in CLAIMABLE_COMMAND_STATES):
        return False
    claimed_at = now or utc_now_naive()
    result = session.execute(
        update(SessionAutopilotState)
        .where(
            SessionAutopilotState.session_id == session_id,
            SessionAutopilotState.mode == "autonomous",
            SessionAutopilotState.scope_key == command.scope_key,
            SessionAutopilotState.status.in_(WAITING_AUTOPILOT_STATUSES),
            SessionAutopilotState.current_command_id == current_command_id,
            SessionAutopilotState.revision == expected_revision,
            SessionAutopilotState.control_generation == expected_control_generation,
            SessionAutopilotState.runner_generation == expected_runner_generation,
            SessionAutopilotState.current_command_id.in_(
                select(RuntimeCommand.id).where(
                    RuntimeCommand.id == current_command_id,
                    RuntimeCommand.session_id == session_id,
                    RuntimeCommand.scope_key == command.scope_key,
                    RuntimeCommand.control_generation == expected_control_generation,
                    RuntimeCommand.runner_generation == expected_runner_generation,
                    RuntimeCommand.state.in_(CLAIMABLE_COMMAND_STATES),
                )
            ),
            or_(
                SessionAutopilotState.lease_owner.is_(None),
                SessionAutopilotState.lease_expires_at.is_(None),
                SessionAutopilotState.lease_expires_at <= claimed_at,
            ),
        )
        .values(
            lease_owner=owner,
            lease_acquired_at=claimed_at,
            lease_expires_at=lease_deadline(now=claimed_at, seconds=lease_seconds),
            revision=SessionAutopilotState.revision + 1,
            updated_at=claimed_at,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def try_recover_processing_attempt_state(
    session: Session,
    session_id: str,
    *,
    current_record_command_id: int,
    owner: str,
    expected_revision: int,
    expected_control_generation: int,
    expected_runner_generation: int,
    now: datetime | None = None,
    lease_seconds: int = AUTOPILOT_LEASE_SECONDS,
) -> bool:
    """Recover attempt processing without invalidating its capture generation.

    A generic runner claim increments ``runner_generation`` and would immediately
    make the already-persisted record command/ACK stale.  This dedicated CAS keeps
    both generations stable and rechecks the succeeded current record inside the
    UPDATE snapshot, so capture proof remains valid after a worker restart.
    """
    claimed_at = now or utc_now_naive()
    result = session.execute(
        update(SessionAutopilotState)
        .where(
            SessionAutopilotState.session_id == session_id,
            SessionAutopilotState.mode == "autonomous",
            SessionAutopilotState.scope_key == "p0a_sim_first_single_v1",
            SessionAutopilotState.status == "processing_attempt",
            SessionAutopilotState.current_command_id == current_record_command_id,
            SessionAutopilotState.revision == expected_revision,
            SessionAutopilotState.control_generation == expected_control_generation,
            SessionAutopilotState.runner_generation == expected_runner_generation,
            SessionAutopilotState.current_command_id.in_(
                select(RuntimeCommand.id).where(
                    RuntimeCommand.id == current_record_command_id,
                    RuntimeCommand.session_id == session_id,
                    RuntimeCommand.scope_key == "p0a_sim_first_single_v1",
                    RuntimeCommand.kind == "record",
                    RuntimeCommand.state == "succeeded",
                    RuntimeCommand.control_generation == expected_control_generation,
                    RuntimeCommand.runner_generation == expected_runner_generation,
                )
            ),
            or_(
                SessionAutopilotState.lease_owner.is_(None),
                SessionAutopilotState.lease_expires_at.is_(None),
                SessionAutopilotState.lease_expires_at <= claimed_at,
            ),
        )
        .values(
            lease_owner=owner,
            lease_acquired_at=claimed_at,
            lease_expires_at=lease_deadline(now=claimed_at, seconds=lease_seconds),
            revision=SessionAutopilotState.revision + 1,
            updated_at=claimed_at,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def claim_from_autopilot_state(state: SessionAutopilotState) -> AutopilotClaim:
    if not state.lease_owner or state.lease_expires_at is None:
        raise ValueError("autopilot state does not hold a complete lease")
    return AutopilotClaim(
        session_id=state.session_id,
        owner=state.lease_owner,
        scope_key=state.scope_key,
        control_generation=state.control_generation,
        runner_generation=state.runner_generation,
        revision=state.revision,
        lease_expires_at=state.lease_expires_at,
    )


def fenced_autopilot_update(
    session: Session,
    claim: AutopilotClaim,
    *,
    values: dict[str, Any],
    release_lease: bool = False,
    now: datetime | None = None,
    lease_seconds: int = AUTOPILOT_LEASE_SECONDS,
) -> bool:
    """CAS-update state only while the exact owner and both generations still win."""
    allowed_fields = {
        "status", "current_command_id", "next_command_seq", "last_error_code",
    }
    forbidden = set(values) - allowed_fields
    if forbidden:
        raise ValueError(
            f"fenced autopilot update contains protected fields: {sorted(forbidden)}")
    changed_at = now or utc_now_naive()
    current = session.get(SessionAutopilotState, claim.session_id)
    if (current is None or current.scope_key != claim.scope_key
            or current.control_generation != claim.control_generation
            or current.runner_generation != claim.runner_generation
            or current.revision != claim.revision
            or current.lease_owner != claim.owner
            or current.lease_expires_at is None
            or current.lease_expires_at <= changed_at):
        return False
    target_status = values.get("status", current.status)
    transitions = {
        "idle": {"idle", "running", "paused", "failed"},
        "running": {
            "running", "waiting_tts", "waiting_recording", "processing_attempt",
            "paused", "failed",
        },
        "waiting_tts": {"waiting_tts", "running", "paused", "failed"},
        "waiting_recording": {
            "waiting_recording", "processing_attempt", "manual_draining",
            "paused", "failed",
        },
        "processing_attempt": {
            "processing_attempt", "running", "waiting_tts", "paused", "failed",
        },
        "manual_draining": {"manual_draining", "paused", "failed"},
        "paused": {"paused", "running", "failed"},
        "failed": {"failed"},
    }
    if target_status not in transitions.get(current.status, set()):
        raise ValueError(
            f"invalid autopilot state transition {current.status} -> {target_status}")
    target_seq = values.get("next_command_seq", current.next_command_seq)
    if (not isinstance(target_seq, int) or isinstance(target_seq, bool)
            or target_seq < current.next_command_seq):
        raise ValueError("next_command_seq cannot move backwards")
    target_command_id = values.get("current_command_id", current.current_command_id)
    if target_status in {
            "waiting_tts", "waiting_recording", "processing_attempt",
            "manual_draining"}:
        if not isinstance(target_command_id, int):
            raise ValueError(f"{target_status} requires a current command")
        target_command = session.get(RuntimeCommand, target_command_id)
        if (target_command is None or target_command.session_id != claim.session_id
                or target_command.scope_key != claim.scope_key
                or target_command.control_generation != claim.control_generation
                or target_command.runner_generation != claim.runner_generation):
            raise ValueError("current command does not match the autopilot claim")
        expected_kind = "tts" if target_status == "waiting_tts" else "record"
        if target_command.kind != expected_kind:
            raise ValueError(f"{target_status} has the wrong command kind")
        expected_states = (
            {"succeeded"} if target_status == "processing_attempt"
            else CLAIMABLE_COMMAND_STATES)
        if target_command.state not in expected_states:
            raise ValueError(f"{target_status} has the wrong command state")
    next_values = dict(values)
    next_values.update(revision=SessionAutopilotState.revision + 1, updated_at=changed_at)
    if release_lease:
        next_values.update(
            lease_owner=None, lease_acquired_at=None, lease_expires_at=None)
    else:
        next_values["lease_expires_at"] = lease_deadline(
            now=changed_at, seconds=lease_seconds)
    result = session.execute(
        update(SessionAutopilotState)
        .where(
            SessionAutopilotState.session_id == claim.session_id,
            SessionAutopilotState.scope_key == claim.scope_key,
            SessionAutopilotState.control_generation == claim.control_generation,
            SessionAutopilotState.runner_generation == claim.runner_generation,
            SessionAutopilotState.revision == claim.revision,
            SessionAutopilotState.lease_owner == claim.owner,
            SessionAutopilotState.lease_expires_at > changed_at,
        )
        .values(**next_values)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def try_claim_runtime_command(
    session: Session,
    command_id: int,
    *,
    owner: str,
    expected_revision: int,
    control_generation: int,
    runner_generation: int,
    now: datetime | None = None,
    lease_seconds: int = COMMAND_LEASE_SECONDS,
) -> bool:
    """Atomically lease a pending/started command without changing lifecycle revision.

    ``revision`` is the device-visible command CAS.  Leasing is fenced separately by
    opaque owner + expiry and therefore must not consume the revision that a device
    ACK observed.  The ACK transaction later moves revision ``r`` to ``r + 1``.
    """
    claimed_at = now or utc_now_naive()
    result = session.execute(
        update(RuntimeCommand)
        .where(
            RuntimeCommand.id == command_id,
            RuntimeCommand.state.in_(CLAIMABLE_COMMAND_STATES),
            RuntimeCommand.revision == expected_revision,
            RuntimeCommand.control_generation == control_generation,
            RuntimeCommand.runner_generation == runner_generation,
            or_(
                RuntimeCommand.lease_owner.is_(None),
                RuntimeCommand.lease_expires_at.is_(None),
                RuntimeCommand.lease_expires_at <= claimed_at,
            ),
        )
        .values(
            lease_owner=owner,
            lease_expires_at=lease_deadline(now=claimed_at, seconds=lease_seconds),
            updated_at=claimed_at,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def claim_from_runtime_command(command: RuntimeCommand) -> CommandClaim:
    if command.id is None:
        raise ValueError("runtime command must be persisted")
    if command.state not in CLAIMABLE_COMMAND_STATES:
        raise ValueError(f"runtime command state {command.state} is not claimable")
    if not command.lease_owner or command.lease_expires_at is None:
        raise ValueError("runtime command does not hold a complete lease")
    return CommandClaim(
        command_id=command.id,
        owner=command.lease_owner,
        scope_key=command.scope_key,
        control_generation=command.control_generation,
        runner_generation=command.runner_generation,
        revision=command.revision,
        kind=command.kind,  # type: ignore[arg-type]
        state=command.state,  # type: ignore[arg-type]
        lease_expires_at=command.lease_expires_at,
    )


def fenced_command_transition(
    session: Session,
    claim: CommandClaim,
    *,
    expected_state: Literal["pending", "started"],
    next_state: Literal["started", "succeeded", "failed", "cancelled"],
    terminal_ack: RuntimeCommandAck | None = None,
    values: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> bool:
    """Move a leased command through its closed lifecycle under revision fencing."""
    allowed = {
        # Strong terminal ACKs may arrive after the best-effort started ACK was
        # lost.  Succeeded is still gated below to tts_ended/record_stopped only.
        "pending": {"started", "succeeded", "failed", "cancelled"},
        "started": {"succeeded", "failed", "cancelled"},
    }[expected_state]
    if next_state not in allowed:
        raise ValueError(f"invalid command transition {expected_state} -> {next_state}")
    if next_state == "succeeded":
        required_terminal_ack = {
            "tts": "tts_ended",
            "record": "record_stopped",
        }[claim.kind]
        if terminal_ack is None:
            raise ValueError(
                f"{claim.kind} success requires strong terminal ACK "
                f"{required_terminal_ack}")
        if terminal_ack.id is None:
            session.flush()
        if terminal_ack.id is None:
            raise ValueError("terminal ACK must be persisted before command success")
        persisted_ack = session.get(RuntimeCommandAck, terminal_ack.id)
        if persisted_ack is None:
            raise ValueError("terminal ACK is not persisted")
        command = session.get(RuntimeCommand, claim.command_id)
        if command is None:
            raise ValueError("command no longer exists")
        if (persisted_ack.ack_type != required_terminal_ack
                or persisted_ack.command_revision != claim.revision):
            raise ValueError(
                f"{claim.kind} success requires current {required_terminal_ack} ACK")
        _verify_ack_binding(
            session, command, persisted_ack,
            ack_type=required_terminal_ack,
            require_terminal_revision=False,
            require_current_command=True,
        )
    elif terminal_ack is not None:
        raise ValueError("terminal_ack is only valid for a succeeded transition")
    unauthorized = set(values or {}) - {"result_json"}
    if unauthorized:
        raise ValueError(
            f"command transition contains protected fields: {sorted(unauthorized)}")
    changed_at = now or utc_now_naive()
    next_values = dict(values or {})
    next_values.update(
        state=next_state,
        revision=RuntimeCommand.revision + 1,
        updated_at=changed_at,
    )
    timestamp_field = {
        "started": "started_at",
        "succeeded": "succeeded_at",
        "failed": "failed_at",
        "cancelled": "cancelled_at",
    }[next_state]
    next_values[timestamp_field] = changed_at
    if next_state in TERMINAL_COMMAND_STATES:
        next_values.update(lease_owner=None, lease_expires_at=None)
    result = session.execute(
        update(RuntimeCommand)
        .where(
            RuntimeCommand.id == claim.command_id,
            RuntimeCommand.scope_key == claim.scope_key,
            RuntimeCommand.state == expected_state,
            RuntimeCommand.control_generation == claim.control_generation,
            RuntimeCommand.runner_generation == claim.runner_generation,
            RuntimeCommand.revision == claim.revision,
            RuntimeCommand.lease_owner == claim.owner,
            RuntimeCommand.lease_expires_at > changed_at,
        )
        .values(**next_values)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


ACK_PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    "tts_started": frozenset({"media_duration_ms"}),
    "tts_ended": frozenset({"media_ended", "media_duration_ms"}),
    "tts_failed": frozenset({"error_code"}),
    "record_started": frozenset({"mime_type", "sample_rate_hz", "channels"}),
    "record_stopped": frozenset({"stop_reason"}),
    "record_failed": frozenset({"error_code"}),
}

RECORD_STOP_REASONS = frozenset({
    "silence", "user_done", "max_duration", "stream_end",
})

CONTROL_PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    "start": frozenset({"source"}),
    "pause": frozenset({"reason_code", "source"}),
    "resume": frozenset({"reason_code", "source"}),
    "takeover": frozenset({"reason_code", "source", "expected_revision"}),
    "drain_complete": frozenset({"drained_command_id"}),
    "generation_bump": frozenset({
        "previous_control_generation", "previous_runner_generation"}),
    "failure": frozenset({
        "error_code", "source", "expected_turns", "matched_turns",
        "completed_attempt_turns", "audio_evidenced_turns", "issue_count",
        "issue_codes",
    }),
    "scope_complete": frozenset({"completed_command_seq"}),
}

_FORBIDDEN_PAYLOAD_KEYS = frozenset({
    "answer", "answer_text", "asr_text", "confirmed_response_text", "patient_name",
    "response", "response_text", "transcript", "tts_text", "text",
})


def _encode_controlled_payload(
    event_type: str,
    payload: dict[str, Any],
    contract: dict[str, frozenset[str]],
) -> str:
    allowed = contract.get(event_type)
    if allowed is None:
        raise ValueError(f"unknown autopilot event type: {event_type}")
    unknown = set(payload) - allowed
    forbidden = set(payload) & _FORBIDDEN_PAYLOAD_KEYS
    if forbidden:
        raise ValueError(f"payload contains response/free-text fields: {sorted(forbidden)}")
    if unknown:
        raise ValueError(f"payload contains unauthorized fields: {sorted(unknown)}")
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None or isinstance(value, bool):
            clean[key] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{event_type}.{key} must be finite")
            clean[key] = value
        elif isinstance(value, str):
            if len(value) > 128:
                raise ValueError(f"{event_type}.{key} is too long")
            clean[key] = value
        else:
            raise ValueError(f"{event_type}.{key} must be a scalar")
    return json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def encode_ack_payload(ack_type: str, payload: dict[str, Any]) -> str:
    return _encode_controlled_payload(ack_type, payload, ACK_PAYLOAD_KEYS)


def encode_control_event_payload(event_type: str, payload: dict[str, Any]) -> str:
    return _encode_controlled_payload(event_type, payload, CONTROL_PAYLOAD_KEYS)


def resolve_ack_replay(
    session: Session,
    candidate: RuntimeCommandAck,
) -> Literal["new", "identical", "conflict"]:
    """Classify an ACK retry before any current-state/generation side effect.

    Server ``received_at`` is intentionally excluded: a retry has a later arrival
    time but must return the first immutable receipt.  Any reused idempotency key,
    ACK type, or device event sequence with different facts is a conflict.
    """
    rows = list(session.exec(select(RuntimeCommandAck).where(or_(
        (
            (RuntimeCommandAck.command_id == candidate.command_id)
            & (RuntimeCommandAck.idempotency_key == candidate.idempotency_key)
        ),
        (
            (RuntimeCommandAck.command_id == candidate.command_id)
            & (RuntimeCommandAck.ack_type == candidate.ack_type)
        ),
        (
            (RuntimeCommandAck.session_id == candidate.session_id)
            & (RuntimeCommandAck.device_id_hash == candidate.device_id_hash)
            & (RuntimeCommandAck.device_event_seq == candidate.device_event_seq)
        ),
    ))))
    if not rows:
        return "new"
    if len(rows) != 1:
        return "conflict"
    existing = rows[0]
    return ("identical" if all(
        getattr(existing, name) == getattr(candidate, name)
        for name in _ACK_IDEMPOTENT_FIELDS
    ) else "conflict")


def try_advance_device_autopilot_event_seq(
    session: Session,
    *,
    capability_token_hash: str,
    session_id: str,
    device_id_hash: str,
    candidate_seq: int,
    now: datetime | None = None,
) -> bool:
    """Atomically advance a current device capability's ACK high-water mark.

    The caller must run :func:`resolve_ack_replay` first.  An ``identical`` replay
    returns the original immutable ACK without calling this helper, so replay does
    not consume a sequence number.  For a genuinely new ACK this single conditional
    UPDATE proves token/session/device binding, current active-capability validity,
    and ``candidate_seq > last_autopilot_event_seq`` in the same database snapshot.

    Command identity and control/runner generations remain separate upper-layer
    gates.  Commit a successful advance in the same transaction as the new ACK.
    """
    if (not isinstance(candidate_seq, int) or isinstance(candidate_seq, bool)
            or candidate_seq < 1):
        raise ValueError("candidate device event sequence must be a positive integer")
    observed_at = now or utc_now_naive()
    result = session.execute(
        update(PatientDeviceCapability)
        .where(
            PatientDeviceCapability.token_hash == capability_token_hash,
            PatientDeviceCapability.session_id == session_id,
            PatientDeviceCapability.device_id_hash == device_id_hash,
            PatientDeviceCapability.active_session_key == session_id,
            PatientDeviceCapability.created_at <= observed_at,
            PatientDeviceCapability.expires_at > observed_at,
            or_(
                PatientDeviceCapability.revoked_at.is_(None),
                PatientDeviceCapability.revoked_at > observed_at,
            ),
            or_(
                PatientDeviceCapability.recovery_only_at.is_(None),
                PatientDeviceCapability.recovery_only_at > observed_at,
            ),
            PatientDeviceCapability.last_autopilot_event_seq < candidate_seq,
        )
        .values(last_autopilot_event_seq=candidate_seq)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def verify_recovery_only_preallocated_upload(
    session: Session,
    *,
    raw_audio_id: str,
    capability_token_hash: str,
    device_id_hash: str,
    now: datetime | None = None,
) -> RuntimeCommand:
    """Authorize the narrow recovery-only upload exception for an issued record.

    This does not write bytes.  It proves the server pre-allocated this exact audio
    id before session switch, the still-open record command is the session's current
    command, both generations are current, and the recovery capability is the same
    device.  The upload endpoint must re-run this proof inside its final mutation
    lock before publishing staged bytes.
    """
    observed_at = now or utc_now_naive()
    command = session.exec(select(RuntimeCommand).where(
        RuntimeCommand.expected_raw_audio_id == raw_audio_id,
        RuntimeCommand.kind == "record",
        RuntimeCommand.state.in_(CLAIMABLE_COMMAND_STATES),
    )).first()
    if command is None:
        raise AutopilotProofError("audio id is not bound to an open record command")
    state = session.get(SessionAutopilotState, command.session_id)
    if (state is None or state.scope_key != command.scope_key
            or state.mode != "autonomous"
            or state.status not in {"waiting_recording", "manual_draining"}
            or state.current_command_id != command.id
            or state.control_generation != command.control_generation
            or state.runner_generation != command.runner_generation):
        raise AutopilotProofError("record command is not the current recoverable state")
    capability = session.get(PatientDeviceCapability, capability_token_hash)
    if (capability is None or capability.session_id != command.session_id
            or capability.device_id_hash != device_id_hash
            or command.issued_capability_token_hash != capability_token_hash
            or command.issued_device_id_hash != device_id_hash
            or capability.recovery_only_at is None
            or command.issued_at >= capability.recovery_only_at
            or capability.recovery_only_at > observed_at
            or capability.expires_at <= observed_at
            or (capability.revoked_at is not None
                and capability.revoked_at <= observed_at)):
        raise AutopilotProofError("recovery capability is not valid for this record")
    audio = session.get(AudioAssetRow, raw_audio_id)
    if (audio is None or audio.session_id != command.session_id
            or audio.turn_key != command.turn_key
            or audio.status != AudioStatus.recorded or audio.withdrawn
            or audio.delete_gate_passed or audio.checksum is not None
            or audio.byte_count is not None or audio.uploaded_at is not None):
        raise AutopilotProofError("preallocated audio row is not an empty open capture")
    existing_receipt = session.exec(select(AudioCaptureReceipt).where(
        AudioCaptureReceipt.raw_audio_id == raw_audio_id)).first()
    if existing_receipt is not None:
        raise AutopilotProofError("preallocated audio already has a capture receipt")
    verify_tts_ended_prerequisite(session, command)
    try:
        payload = RecordCommandPayload.model_validate_json(command.payload_json)
    except (TypeError, ValueError) as exc:
        raise AutopilotProofError("record command payload violates its contract") from exc
    if (payload.raw_audio_id != raw_audio_id or payload.turn_key != command.turn_key
            or payload.item_id != command.item_id or payload.turn_seq != command.turn_seq
            or payload.cue_level != command.prompt_level):
        raise AutopilotProofError("record command payload does not bind this audio id")
    return command


def _verify_ack_capability(
    session: Session,
    ack: RuntimeCommandAck,
    *,
    allow_recovery_only: bool,
) -> PatientDeviceCapability:
    capability = session.get(PatientDeviceCapability, ack.capability_token_hash)
    if capability is None:
        raise AutopilotProofError("ACK capability digest does not exist")
    if capability.session_id != ack.session_id:
        raise AutopilotProofError("ACK capability belongs to another session")
    if capability.device_id_hash != ack.device_id_hash:
        raise AutopilotProofError("ACK device digest does not match its capability")
    if capability.created_at > ack.received_at:
        raise AutopilotProofError("ACK predates its capability")
    if capability.expires_at <= ack.received_at:
        raise AutopilotProofError("ACK was received after capability expiry")
    if capability.revoked_at is not None and capability.revoked_at <= ack.received_at:
        raise AutopilotProofError("ACK was received after capability revocation")
    if (not allow_recovery_only and capability.recovery_only_at is not None
            and capability.recovery_only_at <= ack.received_at):
        raise AutopilotProofError("recovery-only capability cannot ACK this command")
    return capability


def _verify_ack_static_identity(
    session: Session,
    command: RuntimeCommand,
    ack: RuntimeCommandAck,
    *,
    ack_type: str,
    require_terminal_revision: bool,
) -> PatientDeviceCapability:
    """Immutable ACK identity: nothing here can change after the ACK landed.

    Command/session binding, ACK type, both generations, the terminal revision
    transition, the issuing device and capability as they stood *at receipt
    time*, and issuance ordering.  A capability revoked later cannot retroactively
    invalidate an ACK it legitimately produced, so the checks below all compare
    against ``ack.received_at``.
    """
    if ack.command_id != command.id or ack.ack_type != ack_type:
        raise AutopilotProofError("ACK does not identify the expected command event")
    if ack.session_id != command.session_id:
        raise AutopilotProofError("ACK belongs to another session")
    if ack.control_generation != command.control_generation:
        raise AutopilotProofError("ACK control generation is stale")
    if ack.runner_generation != command.runner_generation:
        raise AutopilotProofError("ACK runner generation is stale")
    if require_terminal_revision and ack.command_revision + 1 != command.revision:
        raise AutopilotProofError("ACK command revision is stale")
    capability = _verify_ack_capability(
        session, ack, allow_recovery_only=(ack_type == "record_stopped"))
    if (command.issued_capability_token_hash != ack.capability_token_hash
            or command.issued_device_id_hash != ack.device_id_hash):
        raise AutopilotProofError("ACK was not emitted by the command's issued device")
    if command.issued_at > ack.received_at:
        raise AutopilotProofError("ACK predates command issuance")
    if (capability.recovery_only_at is not None
            and capability.recovery_only_at <= ack.received_at
            and command.issued_at >= capability.recovery_only_at):
        raise AutopilotProofError(
            "recovery-only capability cannot finish a command issued after demotion")
    return capability


def _verify_ack_live_authority(
    session: Session,
    command: RuntimeCommand,
    *,
    require_current_command: bool,
) -> None:
    """Mutable authority: is this command's scope still the live autonomous one.

    Required by every write path and by nothing that merely re-reads historical
    evidence — a legitimate drain, takeover or generation bump must not make an
    already-proved capture stop verifying.
    """
    state = session.get(SessionAutopilotState, command.session_id)
    if state is None:
        raise AutopilotProofError("session has no autopilot control state")
    if state.scope_key != command.scope_key or state.mode != "autonomous":
        raise AutopilotProofError("command scope is no longer autonomous and current")
    if require_current_command:
        if state.current_command_id != command.id:
            raise AutopilotProofError("ACK command is no longer the current command")
        expected_waiting_status = (
            "waiting_tts" if command.kind == "tts" else "waiting_recording")
        if state.status not in {expected_waiting_status, "manual_draining"}:
            raise AutopilotProofError("ACK command is not in its expected waiting state")
    if state.control_generation != command.control_generation:
        raise AutopilotProofError("command control generation is no longer current")
    if state.runner_generation != command.runner_generation:
        raise AutopilotProofError("command runner generation is no longer current")


def _verify_ack_binding(
    session: Session,
    command: RuntimeCommand,
    ack: RuntimeCommandAck,
    *,
    ack_type: str,
    require_terminal_revision: bool,
    require_current_command: bool = False,
    require_live_scope: bool = True,
) -> None:
    _verify_ack_static_identity(
        session, command, ack, ack_type=ack_type,
        require_terminal_revision=require_terminal_revision)
    if require_live_scope:
        _verify_ack_live_authority(
            session, command, require_current_command=require_current_command)


def verify_terminal_tts_ack(
    session: Session,
    tts_command: RuntimeCommand,
    *,
    ack_idempotency_key: str,
    require_current_command: bool,
    require_live_scope: bool = True,
) -> RuntimeCommandAck:
    """Verify one canonical persisted ``tts_ended`` fact for an exact command."""
    if tts_command.kind != "tts":
        raise AutopilotProofError("terminal TTS proof requires a TTS command")
    if tts_command.state != "succeeded" or tts_command.succeeded_at is None:
        raise AutopilotProofError("TTS command has not succeeded")
    ack = session.exec(select(RuntimeCommandAck).where(
        RuntimeCommandAck.command_id == tts_command.id,
        RuntimeCommandAck.idempotency_key == ack_idempotency_key,
        RuntimeCommandAck.ack_type == "tts_ended",
    )).first()
    if ack is None:
        raise AutopilotProofError("TTS trigger is not a persisted tts_ended ACK")
    try:
        payload = json.loads(ack.payload_json)
        if not isinstance(payload, dict) or payload.get("media_ended") is not True:
            raise ValueError("missing media_ended")
        if encode_ack_payload("tts_ended", payload) != ack.payload_json:
            raise ValueError("non-canonical TTS ACK payload")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AutopilotProofError(
            "tts_ended ACK payload is not canonical evidence") from exc
    _verify_ack_binding(
        session,
        tts_command,
        ack,
        ack_type="tts_ended",
        require_terminal_revision=True,
        require_current_command=require_current_command,
        require_live_scope=require_live_scope,
    )
    if tts_command.succeeded_at < ack.received_at:
        raise AutopilotProofError("TTS success timestamp predates its terminal ACK")
    return ack


def verify_tts_ended_prerequisite(
    session: Session,
    record_command: RuntimeCommand,
    *,
    require_live_scope: bool = True,
) -> RuntimeCommandAck:
    """Prove that a record command was issued after a real, current ``tts_ended`` ACK."""
    if record_command.kind != "record":
        raise AutopilotProofError("only a record command has a TTS prerequisite")
    if record_command.predecessor_command_id is None:
        raise AutopilotProofError("record command has no predecessor TTS command")
    if not record_command.trigger_ack_idempotency_key:
        raise AutopilotProofError("record command has no triggering ACK key")
    tts_command = session.get(RuntimeCommand, record_command.predecessor_command_id)
    if tts_command is None or tts_command.kind != "tts":
        raise AutopilotProofError("record predecessor is not a TTS command")
    identity = (
        "session_id", "item_id", "turn_seq", "turn_key", "attempt_seq",
        "prompt_level", "scope_key", "control_generation", "runner_generation",
    )
    if any(getattr(tts_command, name) != getattr(record_command, name) for name in identity):
        raise AutopilotProofError("record command identity differs from predecessor TTS")
    if record_command.command_seq != tts_command.command_seq + 1:
        raise AutopilotProofError("record command is not the next command after TTS")
    if (record_command.issued_capability_token_hash
            != tts_command.issued_capability_token_hash
            or record_command.issued_device_id_hash
            != tts_command.issued_device_id_hash):
        raise AutopilotProofError("record command was issued to another device")
    try:
        tts_payload = TtsCommandPayload.model_validate_json(tts_command.payload_json)
    except (TypeError, ValueError) as exc:
        raise AutopilotProofError("predecessor TTS payload violates its contract") from exc
    if (tts_payload.purpose not in {"question", "cue"}
            or tts_payload.item_id != tts_command.item_id
            or tts_payload.turn_seq != tts_command.turn_seq
            or tts_payload.cue_level != tts_command.prompt_level):
        raise AutopilotProofError(
            "only a turn-bound question/cue TTS may unlock recording")
    ack = verify_terminal_tts_ack(
        session,
        tts_command,
        ack_idempotency_key=record_command.trigger_ack_idempotency_key,
        require_current_command=False,
        require_live_scope=require_live_scope,
    )
    if record_command.issued_at < max(tts_command.succeeded_at, ack.received_at):
        raise AutopilotProofError("record command predates terminal TTS evidence")
    return ack


def verify_immutable_record_capture(
    session: Session,
    record: RuntimeCommand,
) -> AttemptCaptureProof:
    """The single re-verification of one stopped capture's immutable facts.

    Succeeded record command, canonical ``record_stopped`` ACK with its exact
    device/generation/revision binding, the capture receipt tuple, the record
    command payload contract, and the canonical ``tts_ended`` trigger for its
    predecessor.  Every reader that must re-prove a *historical* capture calls
    this — the repeat terminal readback and the controlled export both do.

    It deliberately says nothing about the current scope, runtime, patient
    governance or audio lifecycle: those are mutable, and a readback long after
    a legitimate drain, takeover or resume must not inherit them.
    :func:`verify_terminal_record_capture` layers the live gates on top.
    """
    if record.kind != "record":
        raise AutopilotProofError("record command does not exist")
    if record.state != "succeeded" or record.succeeded_at is None:
        raise AutopilotProofError("record command has not succeeded")
    verify_tts_ended_prerequisite(session, record, require_live_scope=False)
    ack = session.exec(select(RuntimeCommandAck).where(
        RuntimeCommandAck.command_id == record.id,
        RuntimeCommandAck.ack_type == "record_stopped",
    )).first()
    if ack is None:
        raise AutopilotProofError("record command has no stopped ACK")
    try:
        payload = json.loads(ack.payload_json)
        if (not isinstance(payload, dict)
                or payload.get("stop_reason") not in RECORD_STOP_REASONS
                or encode_ack_payload("record_stopped", payload) != ack.payload_json):
            raise ValueError("invalid or non-canonical record_stopped payload")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AutopilotProofError(
            "record_stopped ACK payload is not canonical evidence") from exc
    _verify_ack_binding(
        session, record, ack, ack_type="record_stopped",
        require_terminal_revision=True, require_live_scope=False)
    if record.succeeded_at < ack.received_at:
        raise AutopilotProofError(
            "record success timestamp predates its terminal ACK")
    if (ack.receipt_server_seq is None or ack.raw_audio_id is None
            or ack.checksum is None or ack.byte_count is None
            or ack.duration_seconds is None):
        raise AutopilotProofError("record stopped ACK has no complete capture tuple")
    if record.expected_raw_audio_id != ack.raw_audio_id:
        raise AutopilotProofError("record stopped ACK is not for the command-bound audio id")
    receipt = session.get(AudioCaptureReceipt, ack.receipt_server_seq)
    if receipt is None:
        raise AutopilotProofError("capture receipt does not exist")
    expected_receipt = (
        receipt.session_id == record.session_id,
        receipt.turn_key == record.turn_key,
        receipt.raw_audio_id == ack.raw_audio_id,
        receipt.checksum == ack.checksum,
        receipt.byte_count == ack.byte_count,
        receipt.duration_seconds == ack.duration_seconds,
    )
    if not all(expected_receipt):
        raise AutopilotProofError("stopped ACK does not exactly match capture receipt")
    try:
        command_payload = RecordCommandPayload.model_validate_json(record.payload_json)
    except (TypeError, ValueError) as exc:
        raise AutopilotProofError("record command payload violates its contract") from exc
    if (command_payload.turn_key != record.turn_key
            or command_payload.item_id != record.item_id
            or command_payload.turn_seq != record.turn_seq
            or command_payload.cue_level != record.prompt_level
            or command_payload.raw_audio_id != record.expected_raw_audio_id
            or command_payload.contains_direct_identifier
            != receipt.contains_direct_identifier):
        raise AutopilotProofError("record command payload differs from capture evidence")
    if receipt.duration_seconds > command_payload.max_duration_seconds:
        raise AutopilotProofError("capture duration exceeds record command limit")
    return AttemptCaptureProof(
        command_id=record.id,
        session_id=record.session_id,
        item_id=record.item_id,
        turn_seq=record.turn_seq,
        attempt_seq=record.attempt_seq,
        prompt_level=record.prompt_level,
        raw_audio_id=ack.raw_audio_id,
        receipt_server_seq=ack.receipt_server_seq,
        checksum=ack.checksum,
        byte_count=ack.byte_count,
        duration_seconds=ack.duration_seconds,
    )


def verify_terminal_record_capture(
    session: Session,
    command_id: int,
) -> AttemptCaptureProof:
    """Immutable capture proof plus the write-time live-scope requirement.

    Thin wrapper: the evidence itself is proved once, in
    :func:`verify_immutable_record_capture`; this adds only the fact that the
    command still belongs to an autonomous scope of the same generation, which
    the write paths require and a historical readback must not.
    """
    command = session.get(RuntimeCommand, command_id)
    if command is None:
        raise AutopilotProofError("record command does not exist")
    proof = verify_immutable_record_capture(session, command)
    _verify_ack_live_authority(session, command, require_current_command=False)
    return proof


def verify_record_capture_for_attempt(
    session: Session,
    command_id: int,
) -> AttemptCaptureProof:
    """Return immutable Attempt input only for the current active worker target."""
    command = session.get(RuntimeCommand, command_id)
    if command is None or command.kind != "record":
        raise AutopilotProofError("record command does not exist")
    state = session.get(SessionAutopilotState, command.session_id)
    if (state is None or state.mode != "autonomous"
            or state.scope_key != command.scope_key
            or state.status != "processing_attempt"
            or state.current_command_id != command.id):
        raise AutopilotProofError(
            "record command is not the current attempt-processing target")
    runtime_state = session.get(SessionRuntimeState, command.session_id)
    if runtime_state is None or runtime_state.status != "active":
        raise AutopilotProofError("training session is not active for AI processing")
    proof = verify_terminal_record_capture(session, command_id)

    # The proof above is deliberately immutable and remains usable to release a
    # stopped device after withdrawal/quarantine.  These mutable governance gates
    # belong only to new AI processing.
    receipt = session.get(AudioCaptureReceipt, proof.receipt_server_seq)
    audio = session.get(AudioAssetRow, proof.raw_audio_id)
    if receipt is None or audio is None:
        raise AutopilotProofError("capture audio asset does not exist")
    if (audio.session_id != command.session_id or audio.turn_key != command.turn_key
            or audio.checksum != proof.checksum or audio.byte_count != proof.byte_count
            or audio.uploaded_at is None or audio.withdrawn
            or audio.status != AudioStatus.recorded or audio.delete_gate_passed):
        raise AutopilotProofError("capture audio asset does not match stopped ACK")
    train_session = session.get(TrainSession, command.session_id)
    if train_session is None:
        raise AutopilotProofError("training session does not exist")
    patient = session.get(Patient, train_session.patient_id)
    if patient is None or bool((patient.withdrawal_status or "").strip()):
        raise AutopilotProofError("patient is missing or withdrawn")
    if (receipt.data_classification != train_session.data_classification
            or receipt.is_simulation != train_session.is_simulation):
        raise AutopilotProofError("capture receipt classification differs from session")
    if (audio.data_classification != receipt.data_classification
            or audio.is_simulation != receipt.is_simulation
            or audio.contains_direct_identifier != receipt.contains_direct_identifier):
        raise AutopilotProofError("capture receipt classification differs from audio asset")
    return proof
