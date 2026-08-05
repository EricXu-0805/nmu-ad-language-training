"""Re-verification of one committed explicit-repeat capture, data only.

Shared by the autopilot worker readback and by controlled export.  It performs
no HTTP work and holds no transaction, so both callers prove the same chain
instead of each maintaining its own idea of what "verified" means.
"""
from __future__ import annotations

from typing import NamedTuple
import hashlib

from sqlmodel import Session, select

from . import autopilot_ledger, autopilot_service, content, repeat_intent
from .models import (
    AttemptCaptureProcessing,
    AutopilotControlEvent,
    AudioCaptureReceipt,
    AutopilotRepeatRequest,
    RuntimeCommand,
    RuntimeCommandAck,
    Session as TrainSession,
    SessionAutopilotState,
)


class RepeatEvidenceError(RuntimeError):
    """Stable, non-identifying refusal; carries no transcript or row content."""


class RepeatEvidenceChain(NamedTuple):
    """The complete frozen chain one repeat capture was disposed against."""

    request: AutopilotRepeatRequest
    record: RuntimeCommand
    source: RuntimeCommand


def _fail(detail: str) -> RepeatEvidenceError:
    return RepeatEvidenceError(f"repeat evidence chain invalid: {detail}")


def verify_repeat_evidence_chain(
        capture: AttemptCaptureProcessing,
        s: Session) -> RepeatEvidenceChain:
    """Re-prove capture <-> request <-> record <-> source, field by field.

    Every link is checked against the *frozen capture*, not against whatever
    the session currently looks like: a repeat capture is terminal evidence and
    must still verify long after the scope has moved on.  Any single drifted
    field fails closed, so a corrupted or cross-session pointer can never be
    projected as a terminal outcome.
    """
    if capture.repeat_request_id is None:
        raise _fail("capture has no bound repeat request")
    request = s.get(AutopilotRepeatRequest, capture.repeat_request_id)
    if request is None:
        raise _fail("bound repeat request row is missing")
    if request.capture_processing_id != capture.id:
        raise _fail("request does not point back at this capture")
    capture_facts = (
        (request.session_id, capture.session_id),
        (request.item_id, capture.item_id),
        (request.turn_seq, capture.turn_seq),
        (request.attempt_seq, capture.proof_attempt_seq),
        (request.prompt_level, capture.proof_prompt_level),
        (request.raw_audio_id, capture.raw_audio_id),
        (request.record_command_id, capture.record_command_id),
        (request.is_simulation, capture.is_simulation),
        (request.repeat_protocol_version_id,
         capture.repeat_protocol_version_id),
        (request.repeat_protocol_definition_digest,
         capture.repeat_protocol_definition_digest),
    )
    if any(left != right for left, right in capture_facts):
        raise _fail("request and capture disagree on identity")

    record = s.get(RuntimeCommand, request.record_command_id)
    if record is None:
        raise _fail("triggering record command is missing")
    record_facts = (
        record.session_id == capture.session_id,
        record.kind == "record",
        record.item_id == capture.item_id,
        record.turn_seq == capture.turn_seq,
        record.turn_key == f"{capture.item_id}#{capture.turn_seq}",
        record.attempt_seq == capture.proof_attempt_seq,
        record.prompt_level == capture.proof_prompt_level,
        record.expected_raw_audio_id == capture.raw_audio_id,
        record.predecessor_command_id == capture.predecessor_command_id,
        record.scope_key == autopilot_service.P0A_SCOPE_KEY,
        record.repeat_protocol_version_id == capture.repeat_protocol_version_id,
        record.repeat_protocol_definition_digest
        == capture.repeat_protocol_definition_digest,
        # A record command never carries replay provenance of its own.
        record.replay_source_command_id is None,
        record.replay_ordinal is None,
        record.replay_source_payload_sha256 is None,
    )
    if not all(record_facts):
        raise _fail("record command does not match this capture")

    source = s.get(RuntimeCommand, request.source_tts_command_id)
    if source is None:
        raise _fail("source TTS command is missing")
    source_facts = (
        # The source is exactly the recording's own persisted predecessor.
        source.id == record.predecessor_command_id,
        source.session_id == record.session_id,
        source.kind == "tts",
        source.state == "succeeded",
        source.item_id == record.item_id,
        source.turn_seq == record.turn_seq,
        source.turn_key == record.turn_key,
        source.attempt_seq == record.attempt_seq,
        source.prompt_level == record.prompt_level,
        source.scope_key == record.scope_key,
        source.control_generation == record.control_generation,
        source.runner_generation == record.runner_generation,
        source.issued_capability_token_hash
        == record.issued_capability_token_hash,
        source.issued_device_id_hash == record.issued_device_id_hash,
        source.item_bank_version_id == record.item_bank_version_id,
        source.item_bank_definition_digest == record.item_bank_definition_digest,
        source.autopilot_protocol_version_id
        == record.autopilot_protocol_version_id,
        source.autopilot_protocol_definition_digest
        == record.autopilot_protocol_definition_digest,
        source.response_role == record.response_role,
        source.repeat_protocol_version_id == record.repeat_protocol_version_id,
        source.repeat_protocol_definition_digest
        == record.repeat_protocol_definition_digest,
    )
    if not all(source_facts):
        raise _fail("source TTS does not match the record command")
    # Re-hash the source rather than trusting the digest recorded next to it.
    if hashlib.sha256(source.payload_json.encode("utf-8")).hexdigest() != (
            request.source_payload_sha256):
        raise _fail("source payload no longer hashes to the ledger digest")

    # Anchor 1: the media chain itself — succeeded record command, canonical
    # record_stopped ACK, its tts_ended predecessor and the capture receipt.
    try:
        proof = autopilot_ledger.verify_immutable_record_capture(s, record)
    except autopilot_ledger.AutopilotProofError as exc:
        raise _fail(str(exc)) from exc
    # The proof is the authority; the capture row must agree with it field by
    # field, so a rewritten capture cannot borrow another recording's evidence.
    proof_facts = (
        proof.session_id == capture.session_id,
        proof.item_id == capture.item_id,
        proof.turn_seq == capture.turn_seq,
        proof.attempt_seq == capture.proof_attempt_seq,
        proof.prompt_level == capture.proof_prompt_level,
        proof.raw_audio_id == capture.raw_audio_id,
        proof.receipt_server_seq == capture.receipt_server_seq,
        proof.command_id == capture.record_command_id,
    )
    if not all(proof_facts):
        raise _fail("capture row disagrees with its own capture proof")
    # ``verify_terminal_record_capture`` proves the same chain plus liveness
    # (the scope is still autonomous and current). That extra half is correct at
    # write time but must not gate a historical readback: a legitimate later
    # drain/takeover flips the scope to manual, and the frozen evidence is still
    # exactly as true. Run it only while the scope really is still live.
    control_now = s.get(SessionAutopilotState, record.session_id)
    if (control_now is not None and control_now.mode == "autonomous"
            and control_now.scope_key == record.scope_key
            and control_now.control_generation == record.control_generation
            and control_now.runner_generation == record.runner_generation):
        try:
            autopilot_ledger.verify_terminal_record_capture(s, record.id)
        except autopilot_ledger.AutopilotProofError as exc:
            raise _fail(
                "record capture proof no longer verifies") from exc

    # Anchor 2: disposition, capture terminal shape and ledger outcome must map
    # to each other exactly; no repeat capture may carry an Attempt.
    expected_outcome = {
        "repeat_replayed": ("replayed", 1),
        "repeat_limit_paused": ("limit_paused", 2),
    }.get(capture.disposition)
    if expected_outcome is None:
        raise _fail("capture disposition is not a repeat outcome")
    if (request.outcome, request.repeat_ordinal) != expected_outcome:
        raise _fail("ledger outcome/ordinal contradicts the disposition")
    if (capture.processing_status != "asr_completed"
            or capture.final_attempt_id is not None):
        raise _fail("repeat capture is not a terminal attempt-free row")

    # Anchor 3: the ASR facts were written to both rows in the same transaction.
    if (request.asr_engine_version != capture.asr_engine_version
            or request.asr_confidence != capture.asr_confidence):
        raise _fail("capture and ledger disagree on the ASR facts")

    # Anchor 3b: the recorded phrase key really belongs to the frozen protocol
    # the capture was bound to, and the normalized digest is a real digest.
    try:
        bound_protocol = repeat_intent.protocol_for_binding(
            capture.repeat_protocol_version_id,
            capture.repeat_protocol_definition_digest)
    except content.FrozenContentUnavailable as exc:
        raise _fail(
            "capture's frozen repeat protocol is unavailable") from exc
    normalized_by_key = {
        key: normalized
        for normalized, key in bound_protocol.phrase_by_normalized_text.items()
    }
    frozen_phrase = normalized_by_key.get(request.phrase_key)
    if frozen_phrase is None:
        raise _fail("phrase key is not in the bound protocol")
    if request.normalized_text_sha256 != repeat_intent.normalized_text_sha256(
            frozen_phrase):
        raise _fail(
            "normalized text digest is not this frozen phrase's digest")

    # Anchor 3c: the whole chain closes back onto the session's frozen bindings.
    # Without this, an internally consistent but externally impossible chain —
    # commands carrying a repeat protocol the session never froze, or a
    # classification the session never had — would verify.
    train_session = s.get(TrainSession, capture.session_id)
    if train_session is None:
        raise _fail("capture has no training session")
    session_facts = (
        train_session.repeat_protocol_version_id
        == capture.repeat_protocol_version_id,
        train_session.repeat_protocol_definition_digest
        == capture.repeat_protocol_definition_digest,
        train_session.item_bank_version_id == record.item_bank_version_id,
        train_session.item_bank_definition_digest
        == record.item_bank_definition_digest,
        train_session.autopilot_protocol_version_id
        == record.autopilot_protocol_version_id,
        train_session.autopilot_protocol_definition_digest
        == record.autopilot_protocol_definition_digest,
        train_session.is_simulation == capture.is_simulation,
        train_session.is_simulation == request.is_simulation,
    )
    if not all(session_facts):
        raise _fail("chain does not close onto the session's frozen bindings")

    # Anchor 3d: the whole chain must run forward in time, link by link.
    stopped_ack = s.exec(select(RuntimeCommandAck).where(
        RuntimeCommandAck.command_id == record.id,
        RuntimeCommandAck.ack_type == "record_stopped",
    )).first()
    receipt = (None if stopped_ack is None
               else s.get(AudioCaptureReceipt, stopped_ack.receipt_server_seq))
    if stopped_ack is None or receipt is None:
        raise _fail("record command has no stopped ACK or capture receipt")
    ordered = [
        ("source issued", source.issued_at),
        ("source ended", source.succeeded_at),
        ("record issued", record.issued_at),
        ("capture receipt", receipt.received_at),
        ("stopped ACK", stopped_ack.received_at),
        ("record succeeded", record.succeeded_at),
        ("capture created", capture.created_at),
        ("request created", request.created_at),
    ]
    for (earlier_name, earlier), (later_name, later) in zip(ordered, ordered[1:]):
        if earlier is None or later is None or earlier > later:
            raise _fail(
                f"chain timestamps run backwards: {later_name} precedes {earlier_name}")
    if capture.processed_at is None or capture.processed_at < request.created_at:
        raise _fail("capture terminal timestamp predates its repeat request")

    # Anchor 4: a limit pause only exists after this slot really replayed once.
    # That replay's command/source pointers close against this recording's own
    # predecessor command, and their canonical payload_json bindings agree.
    # It proves no speech, audio or WAV identity.
    if request.repeat_ordinal == 2:
        first = s.exec(select(AutopilotRepeatRequest).where(
            AutopilotRepeatRequest.session_id == request.session_id,
            AutopilotRepeatRequest.item_id == request.item_id,
            AutopilotRepeatRequest.turn_seq == request.turn_seq,
            AutopilotRepeatRequest.attempt_seq == request.attempt_seq,
            AutopilotRepeatRequest.prompt_level == request.prompt_level,
            AutopilotRepeatRequest.repeat_ordinal == 1,
        )).first()
        if (first is None or first.outcome != "replayed"
                or first.replay_command_id is None
                or first.replay_command_id != request.source_tts_command_id
                or first.capture_processing_id == request.capture_processing_id
                or first.record_command_id == request.record_command_id):
            raise _fail(
                "limit pause is not preceded by this slot's own replay")
    return RepeatEvidenceChain(request=request, record=record, source=source)


def verify_repeat_replay_command(chain: RepeatEvidenceChain,
                                  s: Session, *,
                                  capture: AttemptCaptureProcessing) -> None:
    request, record, source = chain
    if request.outcome != "replayed" or request.replay_command_id is None:
        raise _fail("replayed disposition without a replay command")
    if request.pause_control_event_seq is not None:
        raise _fail("replayed disposition also carries a pause pointer")
    replay = s.get(RuntimeCommand, request.replay_command_id)
    if replay is None:
        raise _fail("replay command row is missing")
    facts = (
        replay.session_id == request.session_id,
        replay.kind == "tts",
        replay.replay_ordinal == 1,
        replay.replay_source_command_id == source.id,
        replay.replay_source_payload_sha256 == request.source_payload_sha256,
        # payload_json byte-for-byte, and independently re-hashed.  Audio is
        # re-synthesized downstream and is not compared here.
        replay.payload_json == source.payload_json,
        hashlib.sha256(replay.payload_json.encode("utf-8")).hexdigest()
        == request.source_payload_sha256,
        replay.item_id == request.item_id,
        replay.turn_seq == request.turn_seq,
        replay.turn_key == source.turn_key,
        replay.attempt_seq == request.attempt_seq,
        replay.prompt_level == request.prompt_level,
        replay.scope_key == record.scope_key,
        # Frozen content, repeat binding, generations and delivery device must
        # all be the source's, not today's.
        replay.item_bank_version_id == source.item_bank_version_id,
        replay.item_bank_definition_digest == source.item_bank_definition_digest,
        replay.autopilot_protocol_version_id
        == source.autopilot_protocol_version_id,
        replay.autopilot_protocol_definition_digest
        == source.autopilot_protocol_definition_digest,
        replay.response_role == source.response_role,
        replay.repeat_protocol_version_id == request.repeat_protocol_version_id,
        replay.repeat_protocol_definition_digest
        == request.repeat_protocol_definition_digest,
        replay.control_generation == source.control_generation,
        replay.runner_generation == source.runner_generation,
        replay.issued_capability_token_hash
        == source.issued_capability_token_hash,
        replay.issued_device_id_hash == source.issued_device_id_hash,
        replay.issued_at >= record.succeeded_at,
        # Bounded on both sides: the replay is issued after the request is
        # written and before the capture is closed out, never afterwards.
        replay.issued_at >= request.created_at,
        capture.processed_at is not None,
        capture.processed_at is not None and replay.issued_at <= capture.processed_at,
        # Derived from capture identity + ordinal, and issued after the source.
        replay.idempotency_key == autopilot_service.repeat_replay_command_key(
            request.session_id, request.capture_processing_id),
        replay.command_seq == record.command_seq + 1,
        replay.id != source.id,
    )
    if not all(facts):
        raise _fail("replay command does not match its ledger row")


def verify_repeat_limit_pause(chain: RepeatEvidenceChain, s: Session, *,
                              capture: AttemptCaptureProcessing) -> None:
    request, record, _source = chain
    if request.outcome != "limit_paused" or request.pause_control_event_seq is None:
        raise _fail("limit-paused disposition without a pause pointer")
    if request.replay_command_id is not None:
        raise _fail("limit-paused disposition also carries a replay command")
    event = s.exec(select(AutopilotControlEvent).where(
        AutopilotControlEvent.session_id == request.session_id,
        AutopilotControlEvent.event_seq == request.pause_control_event_seq,
    )).first()
    if event is None:
        raise _fail("pause control event is missing")
    # The pause must agree with the *processing record* it stopped, not merely
    # with whatever generation the control row happens to hold today.
    event_facts = (
        event.event_type == "pause",
        event.actor_type == "system",
        event.actor_id is None,
        event.reason_code == autopilot_service.REPEAT_LIMIT_REASON_CODE,
        event.command_id == record.id,
        event.session_id == record.session_id,
        event.scope_key == record.scope_key,
        event.control_generation == record.control_generation,
        event.runner_generation == record.runner_generation,
        event.from_mode == "autonomous",
        event.to_mode == "autonomous",
        event.from_status == "processing_attempt",
        event.to_status == "paused",
        # The canonical payload is exactly the two frozen keys, no more.
        event.payload_json == autopilot_ledger.encode_control_event_payload(
            "pause", {
                "reason_code": autopilot_service.REPEAT_LIMIT_REASON_CODE,
                "source": autopilot_service.REPEAT_PAUSE_SOURCE,
            }),
        event.idempotency_key == autopilot_service.repeat_limit_event_key(
            request.session_id, request.capture_processing_id),
    )
    if not all(event_facts):
        raise _fail("pause event does not match the processing record")
    if event.created_at < request.created_at:
        raise _fail("pause event predates its repeat request")
    if capture.processed_at is None or event.created_at > capture.processed_at:
        raise _fail("pause event postdates the capture it closed out")
    # Deliberately no assertion about the *current* control/runtime rows: the
    # pause is immutable historical evidence, and a researcher may legitimately
    # have resumed since. The write path proved paused-ness at commit time; a
    # readback must still verify long after the scope moved on.




def verify_repeat_capture(
        s: Session, capture: AttemptCaptureProcessing) -> RepeatEvidenceChain:
    """Full re-verification of one repeat capture, whichever outcome it holds."""
    chain = verify_repeat_evidence_chain(capture, s)
    if capture.disposition == "repeat_replayed":
        verify_repeat_replay_command(chain, s, capture=capture)
    else:
        verify_repeat_limit_pause(chain, s, capture=capture)
    return chain
