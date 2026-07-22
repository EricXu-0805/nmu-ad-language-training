"""Transactional state machine for independent formal assessments.

Every mutator only flushes; the HTTP/application boundary owns commit and must
roll back on :class:`AssessmentServiceError`.  No function accepts an
instrument name, definition digest, total score or formal-eligibility flag from
the client.  Those facts are frozen from the server registry at creation.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import math
import secrets
from typing import Callable, Literal, NoReturn

from sqlalchemy import and_, func, or_
from sqlmodel import Session, select

from . import assessment_definitions, session_admission
from .assessment_contract import (
    ApproveAssessmentDeferralIn,
    AssessmentDeferralOut,
    AssessmentEventCancellationOut,
    AssessmentEventCloseoutOut,
    AssessmentEventOut,
    AssessmentInstanceOut,
    AssessmentScoringEvidenceOut,
    AssessmentTodayOut,
    CancelAssessmentEventIn,
    CloseAssessmentEventIn,
    CompleteAssessmentInstanceIn,
    CreateAssessmentEventIn,
    MAX_ASSESSMENT_JSON_BYTES,
    StartAssessmentEventIn,
    SubmitAssessmentResponseIn,
)
from .models import (
    AssessmentCommand,
    AssessmentDeferralApproval,
    AssessmentEvent,
    AssessmentEventCloseout,
    AssessmentInstance,
    AssessmentItemResponse,
    AssessmentScoringEvidence,
    Patient,
)


ActorRole = Literal["researcher", "admin", "system", "local_m0"]
# The implementation must validate and atomically reserve a server-issued
# contextual receipt.  The global DB uniqueness constraint on the persisted
# digest is the final cross-worker/cross-subject reuse fence.
AssessmentArtifactAuthorizer = Callable[
    [Session, AssessmentEvent, AssessmentInstance, str, int, str], bool]
_TERMINAL_INSTANCE_STATUSES = frozenset({"completed", "approved_deferred"})


class AssessmentServiceError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _fail(status_code: int, code: str, message: str) -> NoReturn:
    raise AssessmentServiceError(status_code, code, message)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        _fail(422, "assessment_json_invalid", "assessment evidence must be finite JSON")


def _canonical_json(value: object, *, kind: str) -> str:
    encoded = _canonical_bytes(value)
    if len(encoded) > MAX_ASSESSMENT_JSON_BYTES:
        status_code = 422 if kind == "response" else 409
        _fail(
            status_code,
            f"assessment_{kind}_too_large",
            f"{kind} evidence exceeds 64 KiB",
        )
    return encoded.decode("utf-8")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_digest(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        return False
    return all(character in "0123456789abcdef" for character in value[7:])


def _is_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _request_hash(command_type: str, payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_bytes({
        "command_type": command_type,
        "payload": payload,
    })).hexdigest()


def _body_payload(body: object) -> dict[str, object]:
    payload = body.model_dump(mode="json", exclude={"idempotency_key"})  # type: ignore[attr-defined]
    return dict(payload)


def _slot_key(patient_id: str, timepoint: str) -> str:
    return _sha256_text(f"{patient_id}\x00{timepoint}")


def _definition_error(exc: assessment_definitions.AssessmentDefinitionError) -> NoReturn:
    status = 422 if exc.code in {
        "assessment_item_unknown",
        "assessment_response_invalid",
        "assessment_response_value_required",
    } else 409
    _fail(status, exc.code, exc.message)


def _bundle(bundle_id: str | None = None) -> assessment_definitions.RegisteredAssessmentBundle:
    try:
        return assessment_definitions.current_bundle(bundle_id)
    except assessment_definitions.AssessmentDefinitionError as exc:
        _definition_error(exc)


def _assert_actor_role(actor_role: str) -> None:
    if actor_role not in {"researcher", "admin", "system", "local_m0"}:
        _fail(403, "assessment_actor_forbidden", "actor role cannot mutate assessments")


def _assert_assigned(event: AssessmentEvent, actor_id: str, actor_role: str) -> None:
    _assert_actor_role(actor_role)
    if event.assigned_assessor_id != actor_id and actor_role != "admin":
        _fail(403, "assessment_assessor_mismatch", "assessment belongs to another assessor")


def _locked_patient(db: Session, patient_id: str) -> Patient:
    patient = db.exec(select(Patient).where(
        Patient.patient_id == patient_id,
    ).with_for_update()).first()
    if patient is None:
        _fail(404, "assessment_patient_missing", "participant profile does not exist")
    if session_admission.patient_content_sealed(patient):
        _fail(
            409,
            "assessment_patient_sealed",
            "participant content is sealed by withdrawal or consent refusal",
        )
    return patient


def _assert_patient_admission(patient: Patient, *, is_simulation: bool) -> None:
    issues = session_admission.patient_admission_issues(
        patient, is_simulation=is_simulation)
    if issues:
        _fail(409, "assessment_patient_ineligible", "; ".join(issues))


def _locked_event(db: Session, event_id: str) -> AssessmentEvent:
    event = db.exec(select(AssessmentEvent).where(
        AssessmentEvent.event_id == event_id,
    ).with_for_update()).first()
    if event is None:
        _fail(404, "assessment_event_missing", "assessment event does not exist")
    return event


def _locked_instance(
    db: Session,
    *,
    event: AssessmentEvent,
    instance_id: str,
) -> AssessmentInstance:
    instance = db.exec(select(AssessmentInstance).where(
        AssessmentInstance.instance_id == instance_id,
    ).with_for_update()).first()
    if instance is None or instance.event_id != event.event_id:
        _fail(404, "assessment_instance_missing", "assessment instance does not exist")
    if instance.patient_id != event.patient_id:
        _fail(409, "assessment_state_invalid", "assessment patient binding is inconsistent")
    return instance


def patient_id_for_event_fence(db: Session, event_id: str) -> str:
    patient_id = db.exec(select(AssessmentEvent.patient_id).where(
        AssessmentEvent.event_id == event_id,
    )).first()
    if patient_id is None:
        _fail(404, "assessment_event_missing", "assessment event does not exist")
    return patient_id


def assessor_id_for_event_fence(db: Session, event_id: str) -> str:
    actor_id = db.exec(select(AssessmentEvent.assigned_assessor_id).where(
        AssessmentEvent.event_id == event_id,
    )).first()
    if actor_id is None:
        _fail(404, "assessment_event_missing", "assessment event does not exist")
    return actor_id


def _instances(db: Session, event_id: str) -> list[AssessmentInstance]:
    return list(db.exec(select(AssessmentInstance).where(
        AssessmentInstance.event_id == event_id,
    ).order_by(AssessmentInstance.category_key)))


def _command_replay(
    db: Session,
    *,
    idempotency_key: str,
    command_type: str,
    request_hash: str,
    actor_id: str,
    actor_role: str,
) -> AssessmentEventOut | None:
    """Return the current projection for an already committed exact request.

    Idempotency here means exactly-once effect, not a byte-for-byte historical
    response.  ``AssessmentCommand`` retains the resulting revision/entity;
    the returned event may have advanced through later valid commands.
    """
    key_hash = _sha256_text(idempotency_key)
    command = db.exec(select(AssessmentCommand).where(
        AssessmentCommand.idempotency_key_sha256 == key_hash,
    )).first()
    if command is None:
        return None
    if (
        command.command_type != command_type
        or command.request_hash != request_hash
        or command.actor_id != actor_id
        or command.actor_role != actor_role
    ):
        _fail(
            409,
            "assessment_idempotency_conflict",
            "idempotency key is already bound to another assessment fact",
        )
    event = db.get(AssessmentEvent, command.event_id)
    if event is None or event.revision < command.resulting_event_revision:
        _fail(409, "assessment_state_invalid", "idempotency fact has no consistent event")
    patient = db.get(Patient, event.patient_id)
    if patient is None:
        _fail(409, "assessment_state_invalid", "idempotency fact has no participant")
    if session_admission.patient_content_sealed(patient):
        # A previously valid key cannot become a post-withdrawal read oracle.
        # The historical command remains in the immutable ledger, but ordinary
        # mutation replay no longer returns its event/item/result projection.
        _fail(
            409,
            "assessment_patient_sealed",
            "participant content is sealed by withdrawal or consent refusal",
        )
    return event_receipt(db, event.event_id)


def _next_command_seq(db: Session, event_id: str) -> int:
    current = db.exec(select(func.max(AssessmentCommand.command_seq)).where(
        AssessmentCommand.event_id == event_id,
    )).one()
    return int(current or 0) + 1


def _append_command(
    db: Session,
    *,
    event: AssessmentEvent,
    command_type: str,
    idempotency_key: str,
    request_hash: str,
    expected_event_revision: int,
    actor_id: str,
    actor_role: str,
    result_entity_type: str,
    result_entity_id: str,
    created_at: datetime,
    instance: AssessmentInstance | None = None,
    expected_target_revision: int | None = None,
    item_key: str | None = None,
    reason_code: str | None = None,
) -> None:
    db.add(AssessmentCommand(
        command_id=_new_id("acmd"),
        event_id=event.event_id,
        instance_id=instance.instance_id if instance is not None else None,
        item_key=item_key,
        command_seq=_next_command_seq(db, event.event_id),
        command_type=command_type,
        reason_code=reason_code,
        idempotency_key_sha256=_sha256_text(idempotency_key),
        request_hash=request_hash,
        expected_event_revision=expected_event_revision,
        resulting_event_revision=event.revision,
        expected_target_revision=expected_target_revision,
        resulting_target_revision=(
            instance.revision if expected_target_revision is not None else None),
        result_entity_type=result_entity_type,
        result_entity_id=result_entity_id,
        actor_id=actor_id,
        actor_role=actor_role,
        created_at=created_at,
    ))


def _assert_revision(event: AssessmentEvent, expected_revision: int) -> None:
    if event.revision != expected_revision:
        _fail(409, "assessment_revision_conflict", "assessment event revision changed")


def _assert_instance_revision(
    instance: AssessmentInstance, expected_revision: int
) -> None:
    if instance.revision != expected_revision:
        _fail(409, "assessment_instance_revision_conflict", "assessment instance revision changed")


def _registered_definition(
    event: AssessmentEvent,
    instance: AssessmentInstance,
) -> assessment_definitions.RegisteredAssessmentDefinition:
    bundle = _bundle(event.definition_bundle_id)
    if bundle.snapshot.bundle_digest != event.definition_bundle_digest:
        _fail(409, "assessment_definition_binding_invalid", "event bundle digest no longer matches registry")
    registered = bundle.definitions.get(instance.category_key)  # type: ignore[arg-type]
    if registered is None:
        _fail(409, "assessment_definition_binding_invalid", "instance category is absent from registry")
    snapshot = registered.snapshot
    frozen = {
        "definition_bundle_id": instance.definition_bundle_id,
        "definition_bundle_digest": instance.definition_bundle_digest,
        "definition_id": instance.definition_id,
        "instrument_id": instance.instrument_id,
        "instrument_version": instance.instrument_version,
        "definition_digest": instance.definition_digest,
        "item_set_digest": instance.item_set_digest,
        "administration_protocol_digest": instance.administration_protocol_digest,
        "response_schema_digest": instance.response_schema_digest,
        "result_schema_digest": instance.result_schema_digest,
        "missingness_rule_digest": instance.missingness_rule_digest,
        "stopping_rule_digest": instance.stopping_rule_digest,
        "scoring_algorithm_id": instance.scoring_algorithm_id,
        "scoring_algorithm_version": instance.scoring_algorithm_version,
        "scoring_algorithm_digest": instance.scoring_algorithm_digest,
        "score_min": instance.score_min,
        "score_max": instance.score_max,
        "score_direction": instance.score_direction,
        "score_rounding_rule": instance.score_rounding_rule,
        "automatic_scoring_permitted": instance.automatic_scoring_permitted,
        "item_response_storage_permitted": instance.item_response_storage_permitted,
        "result_storage_permitted": instance.result_storage_permitted,
        "result_export_permitted": instance.result_export_permitted,
        "required_item_count": instance.required_item_count,
    }
    current = {
        "definition_bundle_id": bundle.snapshot.bundle_id,
        "definition_bundle_digest": bundle.snapshot.bundle_digest,
        "definition_id": snapshot.definition_id,
        "instrument_id": snapshot.instrument_id,
        "instrument_version": snapshot.instrument_version,
        "definition_digest": snapshot.definition_digest,
        "item_set_digest": snapshot.item_set_digest,
        "administration_protocol_digest": snapshot.administration_protocol_digest,
        "response_schema_digest": snapshot.response_schema_digest,
        "result_schema_digest": snapshot.result_schema_digest,
        "missingness_rule_digest": snapshot.missingness_rule_digest,
        "stopping_rule_digest": snapshot.stopping_rule_digest,
        "scoring_algorithm_id": snapshot.scoring_algorithm_id,
        "scoring_algorithm_version": snapshot.scoring_algorithm_version,
        "scoring_algorithm_digest": snapshot.scoring_algorithm_digest,
        "score_min": snapshot.score_min,
        "score_max": snapshot.score_max,
        "score_direction": snapshot.score_direction,
        "score_rounding_rule": snapshot.score_rounding_rule,
        "automatic_scoring_permitted": snapshot.automatic_scoring_permitted,
        "item_response_storage_permitted": snapshot.item_response_storage_permitted,
        "result_storage_permitted": snapshot.result_storage_permitted,
        "result_export_permitted": snapshot.result_export_permitted,
        "required_item_count": len(snapshot.items),
    }
    if frozen != current:
        _fail(409, "assessment_definition_binding_invalid", "instance definition snapshot is inconsistent")
    return registered


def _evidence_out(row: AssessmentScoringEvidence) -> AssessmentScoringEvidenceOut:
    try:
        result = json.loads(row.result_json)
    except (TypeError, json.JSONDecodeError):
        _fail(409, "assessment_state_invalid", "scoring result JSON is invalid")
    if not isinstance(result, dict):
        _fail(409, "assessment_state_invalid", "scoring result must be an object")
    if row.result_json != _canonical_json(result, kind="result"):
        _fail(409, "assessment_state_invalid", "scoring result JSON is not canonical")
    expected_result_digest = _digest({
        "schema": "assessment-result.v1",
        "definition_digest": row.definition_digest,
        "item_response_set_digest": row.item_response_set_digest,
        "score": row.score,
        "result": result,
        "answered_item_count": row.answered_item_count,
        "missing_item_count": row.missing_item_count,
        "stopped_early": row.stopped_early,
        "stopping_reason_code": row.stopping_reason_code,
    })
    if (
        not all(_is_digest(value) for value in (
            row.definition_digest,
            row.item_response_set_digest,
            row.scoring_algorithm_digest,
            row.result_schema_digest,
            row.result_digest,
        ))
        or row.result_digest != expected_result_digest
    ):
        _fail(409, "assessment_state_invalid", "scoring evidence digest is invalid")
    return AssessmentScoringEvidenceOut(
        evidence_id=row.evidence_id,
        instance_id=row.instance_id,
        event_id=row.event_id,
        patient_id=row.patient_id,
        category_key=row.category_key,
        definition_digest=row.definition_digest,
        item_response_set_digest=row.item_response_set_digest,
        scoring_algorithm_id=row.scoring_algorithm_id,
        scoring_algorithm_version=row.scoring_algorithm_version,
        scoring_algorithm_digest=row.scoring_algorithm_digest,
        score=row.score,
        result=result,
        result_digest=row.result_digest,
        answered_item_count=row.answered_item_count,
        missing_item_count=row.missing_item_count,
        stopped_early=row.stopped_early,
        stopping_reason_code=row.stopping_reason_code,
        formal_outcome_eligible=row.formal_outcome_eligible,
        scored_at=row.scored_at,
    )


def _deferral_out(row: AssessmentDeferralApproval) -> AssessmentDeferralOut:
    return AssessmentDeferralOut(
        deferral_id=row.deferral_id,
        instance_id=row.instance_id,
        event_id=row.event_id,
        patient_id=row.patient_id,
        category_key=row.category_key,
        definition_digest=row.definition_digest,
        reason_code=row.reason_code,
        deferred_until=row.deferred_until,
        approved_by=row.approved_by,
        approved_role=row.approved_role,
        approved_at=row.approved_at,
    )


def _instance_out(db: Session, row: AssessmentInstance) -> AssessmentInstanceOut:
    evidence = db.exec(select(AssessmentScoringEvidence).where(
        AssessmentScoringEvidence.instance_id == row.instance_id,
    )).first()
    deferral = db.exec(select(AssessmentDeferralApproval).where(
        AssessmentDeferralApproval.instance_id == row.instance_id,
    )).first()
    _payloads, latest_response_rows = _latest_responses(db, row)
    if evidence is not None and (
        evidence.event_id != row.event_id
        or evidence.patient_id != row.patient_id
        or evidence.category_key != row.category_key
        or evidence.definition_digest != row.definition_digest
        or evidence.scoring_algorithm_id != row.scoring_algorithm_id
        or evidence.scoring_algorithm_version != row.scoring_algorithm_version
        or evidence.scoring_algorithm_digest != row.scoring_algorithm_digest
        or evidence.result_schema_digest != row.result_schema_digest
        or evidence.score_min != row.score_min
        or evidence.score_max != row.score_max
        or evidence.is_simulation != row.is_simulation
        or evidence.formal_outcome_eligible != row.formal_outcome_eligible
    ):
        _fail(409, "assessment_state_invalid", "scoring evidence binding is inconsistent")
    if deferral is not None and (
        deferral.event_id != row.event_id
        or deferral.patient_id != row.patient_id
        or deferral.category_key != row.category_key
        or deferral.definition_digest != row.definition_digest
        or deferral.approved_role not in {"admin", "local_m0"}
    ):
        _fail(409, "assessment_state_invalid", "deferral evidence binding is inconsistent")
    if row.status == "completed":
        if evidence is None or deferral is not None:
            _fail(409, "assessment_state_invalid", "completed instance lacks exact scoring evidence")
        expected_response_set_digest = _digest({
            "schema": "assessment-item-response-set.v1",
            "definition_digest": row.definition_digest,
            "responses": [
                {
                    "item_key": response.item_key,
                    "item_revision": response.item_revision,
                    "response_digest": response.response_digest,
                    "authorized_artifact_digest": response.authorized_artifact_digest,
                }
                for response in latest_response_rows
            ],
        })
        if evidence.item_response_set_digest != expected_response_set_digest:
            _fail(409, "assessment_state_invalid", "scoring evidence response-set digest is invalid")
    elif row.status == "approved_deferred":
        if deferral is None or evidence is not None:
            _fail(409, "assessment_state_invalid", "deferred instance lacks exact approval evidence")
    elif evidence is not None or deferral is not None:
        _fail(409, "assessment_state_invalid", "non-terminal instance has terminal evidence")
    return AssessmentInstanceOut(
        instance_id=row.instance_id,
        event_id=row.event_id,
        patient_id=row.patient_id,
        category_key=row.category_key,
        definition_bundle_id=row.definition_bundle_id,
        definition_bundle_digest=row.definition_bundle_digest,
        definition_id=row.definition_id,
        instrument_id=row.instrument_id,
        instrument_version=row.instrument_version,
        definition_digest=row.definition_digest,
        item_set_digest=row.item_set_digest,
        administration_protocol_digest=row.administration_protocol_digest,
        response_schema_digest=row.response_schema_digest,
        result_schema_digest=row.result_schema_digest,
        missingness_rule_digest=row.missingness_rule_digest,
        stopping_rule_digest=row.stopping_rule_digest,
        scoring_algorithm_id=row.scoring_algorithm_id,
        scoring_algorithm_version=row.scoring_algorithm_version,
        scoring_algorithm_digest=row.scoring_algorithm_digest,
        score_min=row.score_min,
        score_max=row.score_max,
        score_direction=row.score_direction,
        score_rounding_rule=row.score_rounding_rule,
        automatic_scoring_permitted=row.automatic_scoring_permitted,
        item_response_storage_permitted=row.item_response_storage_permitted,
        result_storage_permitted=row.result_storage_permitted,
        result_export_permitted=row.result_export_permitted,
        status=row.status,
        revision=row.revision,
        item_response_count=len(latest_response_rows),
        required_item_count=row.required_item_count,
        data_classification=row.data_classification,
        formal_outcome_eligible=row.formal_outcome_eligible,
        scoring_evidence=_evidence_out(evidence) if evidence is not None else None,
        deferral=_deferral_out(deferral) if deferral is not None else None,
        created_at=row.created_at,
        completed_at=row.completed_at,
        updated_at=row.updated_at,
    )


def _instance_terminal_digest(
    db: Session,
    event: AssessmentEvent,
    rows: list[AssessmentInstance],
) -> str:
    if len(rows) != 2 or any(
        row.status not in _TERMINAL_INSTANCE_STATUSES for row in rows
    ):
        _fail(
            409,
            "assessment_terminal_integrity_failed",
            "both category instances must be terminal",
        )
    terminal_rows: list[dict[str, object]] = []
    for row in rows:
        if row.status == "completed":
            evidence = db.exec(select(AssessmentScoringEvidence).where(
                AssessmentScoringEvidence.instance_id == row.instance_id,
            )).first()
            if evidence is None:
                _fail(
                    409,
                    "assessment_terminal_integrity_failed",
                    "completed instance lacks scoring evidence",
                )
            terminal_rows.append({
                "category_key": row.category_key,
                "status": row.status,
                "definition_digest": row.definition_digest,
                "result_digest": evidence.result_digest,
                "item_response_set_digest": evidence.item_response_set_digest,
            })
        else:
            deferral = db.exec(select(AssessmentDeferralApproval).where(
                AssessmentDeferralApproval.instance_id == row.instance_id,
            )).first()
            if deferral is None:
                _fail(
                    409,
                    "assessment_terminal_integrity_failed",
                    "deferred instance lacks approval evidence",
                )
            terminal_rows.append({
                "category_key": row.category_key,
                "status": row.status,
                "definition_digest": row.definition_digest,
                "reason_code": deferral.reason_code,
                "deferred_until": deferral.deferred_until.isoformat(),
                "approved_role": deferral.approved_role,
            })
    return _digest({
        "schema": "assessment-instance-terminal-set.v1",
        "event_id": event.event_id,
        "definition_bundle_digest": event.definition_bundle_digest,
        "instances": sorted(
            terminal_rows, key=lambda row: str(row["category_key"])),
    })


def _assert_command_chain(db: Session, event: AssessmentEvent) -> None:
    commands = list(db.exec(select(AssessmentCommand).where(
        AssessmentCommand.event_id == event.event_id,
    ).order_by(AssessmentCommand.command_seq)))
    if not commands:
        _fail(409, "assessment_state_invalid", "assessment event lacks its command ledger")
    instances = _instances(db, event.event_id)
    projected_instance_revisions = {
        row.instance_id: 1 for row in instances
    }
    projected_instance_statuses: dict[str, str | None] = {
        row.instance_id: None for row in instances
    }
    projected_instance_updated_at: dict[str, datetime | None] = {
        row.instance_id: None for row in instances
    }
    projected_instance_completed_at: dict[str, datetime | None] = {
        row.instance_id: None for row in instances
    }
    projected_event_status: str | None = None
    projected_event_updated_at: datetime | None = None
    response_entity_ids: set[str] = set()
    scoring_entity_ids: set[str] = set()
    deferral_entity_ids: set[str] = set()
    closeout_entity_ids: set[str] = set()
    prior_revision = 0
    for expected_seq, command in enumerate(commands, start=1):
        if (
            command.command_seq != expected_seq
            or command.expected_event_revision != prior_revision
            or command.resulting_event_revision != prior_revision + 1
        ):
            _fail(409, "assessment_state_invalid", "assessment command chain is discontinuous")
        if not (
            _is_hash(command.idempotency_key_sha256)
            and _is_hash(command.request_hash)
        ):
            _fail(409, "assessment_state_invalid", "assessment command hash is invalid")
        if (expected_seq == 1) != (command.command_type == "create"):
            _fail(409, "assessment_state_invalid", "assessment command chain has an invalid create fact")
        targets_instance = command.command_type in {"response", "complete", "defer"}
        if targets_instance != (
            command.instance_id is not None
            and command.expected_target_revision is not None
            and command.resulting_target_revision is not None
        ):
            _fail(409, "assessment_state_invalid", "assessment command target binding is invalid")
        if command.instance_id is not None:
            target = db.get(AssessmentInstance, command.instance_id)
            if target is None or target.event_id != event.event_id:
                _fail(409, "assessment_state_invalid", "assessment command targets another event")
            projected_target_revision = projected_instance_revisions.get(
                command.instance_id)
            if (
                projected_target_revision is None
                or command.expected_target_revision != projected_target_revision
                or command.resulting_target_revision != projected_target_revision + 1
            ):
                _fail(409, "assessment_state_invalid", "assessment target revision chain is discontinuous")
            projected_instance_revisions[command.instance_id] = (
                projected_target_revision + 1)
        if command.command_type == "response" and not (command.item_key or "").strip():
            _fail(409, "assessment_state_invalid", "response command lacks an item binding")
        if command.command_type != "response" and command.item_key is not None:
            _fail(409, "assessment_state_invalid", "non-response command has an item binding")

        if command.command_type in {"create", "start", "cancel"}:
            if (
                command.result_entity_type != "assessment_event"
                or command.result_entity_id != event.event_id
            ):
                _fail(409, "assessment_state_invalid", "assessment command event result binding is invalid")
            if command.command_type == "create":
                if (
                    projected_event_status is not None
                    or command.actor_id != event.created_by
                    or command.created_at != event.created_at
                    or command.reason_code is not None
                ):
                    _fail(409, "assessment_state_invalid", "assessment create command effect binding is invalid")
                projected_event_status = "due"
                projected_event_updated_at = command.created_at
                for instance_id in projected_instance_statuses:
                    projected_instance_statuses[instance_id] = "due"
                    projected_instance_updated_at[instance_id] = command.created_at
            elif command.command_type == "start":
                if (
                    projected_event_status != "due"
                    or any(status != "due" for status in projected_instance_statuses.values())
                    or command.created_at != event.started_at
                    or command.reason_code is not None
                ):
                    _fail(409, "assessment_state_invalid", "assessment start command effect binding is invalid")
                for instance_id, revision in projected_instance_revisions.items():
                    if revision != 1:
                        _fail(409, "assessment_state_invalid", "assessment start appears after instance mutation")
                    projected_instance_revisions[instance_id] = 2
                    projected_instance_statuses[instance_id] = "in_progress"
                    projected_instance_updated_at[instance_id] = command.created_at
                projected_event_status = "in_progress"
                projected_event_updated_at = command.created_at
            elif (
                projected_event_status != "due"
                or any(status != "due" for status in projected_instance_statuses.values())
                or command.reason_code != event.cancellation_reason_code
                or command.actor_id != event.cancelled_by
                or command.created_at != event.cancelled_at
            ):
                _fail(409, "assessment_state_invalid", "assessment cancellation command effect binding is invalid")
            else:
                projected_event_status = "cancelled"
                projected_event_updated_at = command.created_at
        elif command.command_type == "response":
            result = db.get(AssessmentItemResponse, command.result_entity_id)
            if (
                command.result_entity_type != "assessment_item_response"
                or result is None
                or result.event_id != event.event_id
                or result.instance_id != command.instance_id
                or result.item_key != command.item_key
                or result.event_revision != command.resulting_event_revision
                or result.instance_revision != command.resulting_target_revision
                or result.idempotency_key_sha256 != command.idempotency_key_sha256
                or result.request_hash != command.request_hash
                or result.submitted_by != command.actor_id
                or result.submitted_at != command.created_at
                or command.reason_code is not None
            ):
                _fail(409, "assessment_state_invalid", "response command result binding is invalid")
            if (
                projected_event_status != "in_progress"
                or projected_instance_statuses.get(result.instance_id) != "in_progress"
            ):
                _fail(409, "assessment_state_invalid", "response command crosses an invalid assessment state")
            projected_event_updated_at = command.created_at
            projected_instance_updated_at[result.instance_id] = command.created_at
            response_entity_ids.add(result.response_id)
        elif command.command_type == "complete":
            result = db.get(AssessmentScoringEvidence, command.result_entity_id)
            if (
                command.result_entity_type != "assessment_scoring_evidence"
                or result is None
                or result.event_id != event.event_id
                or result.instance_id != command.instance_id
                or result.scored_at != command.created_at
                or command.reason_code is not None
            ):
                _fail(409, "assessment_state_invalid", "completion command result binding is invalid")
            if (
                projected_event_status != "in_progress"
                or projected_instance_statuses.get(result.instance_id) != "in_progress"
            ):
                _fail(409, "assessment_state_invalid", "completion command crosses an invalid assessment state")
            projected_instance_statuses[result.instance_id] = "completed"
            projected_instance_updated_at[result.instance_id] = command.created_at
            projected_instance_completed_at[result.instance_id] = command.created_at
            projected_event_status = (
                "awaiting_closeout"
                if all(status in _TERMINAL_INSTANCE_STATUSES
                       for status in projected_instance_statuses.values())
                else "in_progress"
            )
            projected_event_updated_at = command.created_at
            scoring_entity_ids.add(result.evidence_id)
        elif command.command_type == "defer":
            result = db.get(AssessmentDeferralApproval, command.result_entity_id)
            if (
                command.result_entity_type != "assessment_deferral_approval"
                or result is None
                or result.event_id != event.event_id
                or result.instance_id != command.instance_id
                or result.approved_by != command.actor_id
                or result.approved_role != command.actor_role
                or result.approved_at != command.created_at
                or command.reason_code is not None
            ):
                _fail(409, "assessment_state_invalid", "deferral command result binding is invalid")
            if (
                projected_event_status != "in_progress"
                or projected_instance_statuses.get(result.instance_id) != "in_progress"
            ):
                _fail(409, "assessment_state_invalid", "deferral command crosses an invalid assessment state")
            projected_instance_statuses[result.instance_id] = "approved_deferred"
            projected_instance_updated_at[result.instance_id] = command.created_at
            projected_event_status = (
                "awaiting_closeout"
                if all(status in _TERMINAL_INSTANCE_STATUSES
                       for status in projected_instance_statuses.values())
                else "in_progress"
            )
            projected_event_updated_at = command.created_at
            deferral_entity_ids.add(result.deferral_id)
        elif command.command_type == "close":
            result = db.get(AssessmentEventCloseout, command.result_entity_id)
            if (
                command.result_entity_type != "assessment_event_closeout"
                or result is None
                or result.event_id != event.event_id
                or result.event_revision != command.resulting_event_revision
                or result.idempotency_key_sha256 != command.idempotency_key_sha256
                or result.request_hash != command.request_hash
                or result.closed_by != command.actor_id
                or result.closed_at != command.created_at
                or command.reason_code is not None
            ):
                _fail(409, "assessment_state_invalid", "closeout command result binding is invalid")
            if projected_event_status != "awaiting_closeout":
                _fail(409, "assessment_state_invalid", "closeout command crosses an invalid assessment state")
            projected_event_status = "closed"
            projected_event_updated_at = command.created_at
            closeout_entity_ids.add(result.closeout_id)
        else:
            _fail(409, "assessment_state_invalid", "assessment command type is invalid")
        prior_revision = command.resulting_event_revision
    if (
        prior_revision != event.revision
        or projected_event_status != event.status
        or projected_event_updated_at != event.updated_at
    ):
        _fail(409, "assessment_state_invalid", "event revision differs from its command ledger")
    if any(
        row.revision != projected_instance_revisions.get(row.instance_id)
        or row.status != projected_instance_statuses.get(row.instance_id)
        or row.updated_at != projected_instance_updated_at.get(row.instance_id)
        or row.completed_at != projected_instance_completed_at.get(row.instance_id)
        for row in instances
    ):
        _fail(409, "assessment_state_invalid", "instance state differs from its command ledger")
    persisted_response_ids = {
        row.response_id for row in db.exec(select(AssessmentItemResponse).where(
            AssessmentItemResponse.event_id == event.event_id))
    }
    persisted_scoring_ids = {
        row.evidence_id for row in db.exec(select(AssessmentScoringEvidence).where(
            AssessmentScoringEvidence.event_id == event.event_id))
    }
    persisted_deferral_ids = {
        row.deferral_id for row in db.exec(select(AssessmentDeferralApproval).where(
            AssessmentDeferralApproval.event_id == event.event_id))
    }
    persisted_closeout_ids = {
        row.closeout_id for row in db.exec(select(AssessmentEventCloseout).where(
            AssessmentEventCloseout.event_id == event.event_id))
    }
    if (
        response_entity_ids != persisted_response_ids
        or scoring_entity_ids != persisted_scoring_ids
        or deferral_entity_ids != persisted_deferral_ids
        or closeout_entity_ids != persisted_closeout_ids
    ):
        _fail(409, "assessment_state_invalid", "assessment result ledger contains an orphan or missing entity")


def event_receipt(db: Session, event_id: str) -> AssessmentEventOut:
    event = db.get(AssessmentEvent, event_id)
    if event is None:
        _fail(404, "assessment_event_missing", "assessment event does not exist")
    _assert_command_chain(db, event)
    rows = _instances(db, event.event_id)
    if len(rows) != 2 or {row.category_key for row in rows} != {
        "untrained_standardized_naming", "functional_communication",
    }:
        _fail(409, "assessment_state_invalid", "assessment event must have exactly two category instances")
    if not _is_digest(event.definition_bundle_digest):
        _fail(409, "assessment_state_invalid", "event bundle digest is invalid")
    for row in rows:
        if (
            row.patient_id != event.patient_id
            or row.definition_bundle_id != event.definition_bundle_id
            or row.definition_bundle_digest != event.definition_bundle_digest
            or row.is_simulation != event.is_simulation
            or row.data_classification != event.data_classification
            or row.formal_outcome_eligible != event.formal_outcome_eligible
        ):
            _fail(409, "assessment_state_invalid", "event and instance snapshots disagree")
        if not all(_is_digest(value) for value in (
            row.definition_bundle_digest,
            row.definition_digest,
            row.item_set_digest,
            row.administration_protocol_digest,
            row.response_schema_digest,
            row.result_schema_digest,
            row.missingness_rule_digest,
            row.stopping_rule_digest,
            row.scoring_algorithm_digest,
        )):
            _fail(409, "assessment_state_invalid", "instance definition digest is invalid")

    closeout = db.exec(select(AssessmentEventCloseout).where(
        AssessmentEventCloseout.event_id == event.event_id,
    )).first()
    if event.status == "closed":
        expected_terminal_digest = _instance_terminal_digest(db, event, rows)
        if (
            closeout is None
            or event.active_protocol_slot_key is not None
            or closeout.patient_id != event.patient_id
            or closeout.event_revision != event.revision
            or closeout.closed_at != event.closed_at
            or closeout.instance_terminal_digest != expected_terminal_digest
            or not _is_hash(closeout.idempotency_key_sha256)
            or not _is_hash(closeout.request_hash)
        ):
            _fail(409, "assessment_state_invalid", "closed event lacks exact closeout lock")
    elif closeout is not None:
        _fail(409, "assessment_state_invalid", "open event cannot have closeout evidence")

    if event.status in {"awaiting_closeout", "closed"} and any(
        row.status not in _TERMINAL_INSTANCE_STATUSES for row in rows
    ):
        _fail(409, "assessment_state_invalid", "event reached closeout before both instances became terminal")
    if event.status == "cancelled":
        if any(row.status != "due" for row in rows) or event.active_protocol_slot_key is not None:
            _fail(409, "assessment_state_invalid", "cancelled event must retain two untouched due instances")
        if (
            event.cancellation_reason_code not in {
                "schedule_changed", "participant_unavailable",
                "protocol_correction", "duplicate_event",
            }
            or not (event.cancelled_by or "").strip()
            or event.cancelled_at is None
        ):
            _fail(409, "assessment_state_invalid", "cancelled event lacks cancellation evidence")
        cancellation = AssessmentEventCancellationOut(
            reason_code=event.cancellation_reason_code,
            cancelled_by=event.cancelled_by,
            cancelled_at=event.cancelled_at,
        )
    else:
        if any(value is not None for value in (
            event.cancellation_reason_code, event.cancelled_by, event.cancelled_at,
        )):
            _fail(409, "assessment_state_invalid", "non-cancelled event has cancellation facts")
        cancellation = None

    closeout_out = None
    if closeout is not None:
        observation_flags = (
            closeout.fatigue_observed,
            closeout.distress_or_discomfort_observed,
            closeout.participant_declined_to_continue,
            closeout.staff_assistance_occurred,
            closeout.environment_interruption_occurred,
            closeout.device_or_network_interruption_occurred,
        )
        valid_note = (
            closeout.note is None
            or (closeout.note == closeout.note.strip()
                and 0 < len(closeout.note) <= 2000)
        )
        report_consistent = (
            closeout.report_status == "no_additional_observation"
            and not any(observation_flags)
            and closeout.note is None
        ) or (
            closeout.report_status == "observation_recorded"
            and (any(observation_flags) or closeout.note is not None)
        )
        if not valid_note or not report_consistent:
            _fail(409, "assessment_state_invalid", "closeout observation facts are inconsistent")
        closeout_out = AssessmentEventCloseoutOut(
            closeout_id=closeout.closeout_id,
            event_id=closeout.event_id,
            patient_id=closeout.patient_id,
            event_revision=closeout.event_revision,
            report_status=closeout.report_status,
            fatigue_observed=closeout.fatigue_observed,
            distress_or_discomfort_observed=closeout.distress_or_discomfort_observed,
            participant_declined_to_continue=closeout.participant_declined_to_continue,
            staff_assistance_occurred=closeout.staff_assistance_occurred,
            environment_interruption_occurred=closeout.environment_interruption_occurred,
            device_or_network_interruption_occurred=closeout.device_or_network_interruption_occurred,
            note=closeout.note,
            closed_by=closeout.closed_by,
            closed_at=closeout.closed_at,
        )
    return AssessmentEventOut(
        event_id=event.event_id,
        patient_id=event.patient_id,
        assigned_assessor_id=event.assigned_assessor_id,
        timepoint=event.timepoint,
        scheduled_date=event.scheduled_date,
        status=event.status,
        revision=event.revision,
        is_simulation=event.is_simulation,
        data_classification=event.data_classification,
        formal_outcome_eligible=event.formal_outcome_eligible,
        definition_bundle_id=event.definition_bundle_id,
        definition_bundle_digest=event.definition_bundle_digest,
        instances=[_instance_out(db, row) for row in rows],
        closeout=closeout_out,
        cancellation=cancellation,
        created_at=event.created_at,
        updated_at=event.updated_at,
    )


def create_event(
    db: Session,
    *,
    patient_id: str,
    body: CreateAssessmentEventIn,
    assigned_assessor_id: str,
    actor_id: str,
    actor_role: ActorRole,
    now: datetime | None = None,
    bundle_id: str | None = None,
) -> AssessmentEventOut:
    _assert_actor_role(actor_role)
    if actor_role not in {"researcher", "admin", "local_m0"}:
        _fail(403, "assessment_actor_forbidden", "actor cannot schedule assessment events")
    assigned_assessor_id = assigned_assessor_id.strip()
    if not assigned_assessor_id or len(assigned_assessor_id) > 200:
        _fail(422, "assessment_assessor_invalid", "assigned assessor is invalid")
    payload = {
        "patient_id": patient_id,
        "assigned_assessor_id": assigned_assessor_id,
        "bundle_id": bundle_id,
        **_body_payload(body),
    }
    request_hash = _request_hash("create", payload)
    replay = _command_replay(
        db, idempotency_key=body.idempotency_key, command_type="create",
        request_hash=request_hash, actor_id=actor_id, actor_role=actor_role)
    if replay is not None:
        return replay
    patient = _locked_patient(db, patient_id)
    replay = _command_replay(
        db, idempotency_key=body.idempotency_key, command_type="create",
        request_hash=request_hash, actor_id=actor_id, actor_role=actor_role)
    if replay is not None:
        return replay
    registered_bundle = _bundle(bundle_id)
    is_simulation = patient.is_simulation_subject is True
    _assert_patient_admission(patient, is_simulation=is_simulation)
    classification = session_admission.expected_classification(is_simulation)
    formal_eligible = bool(
        not is_simulation and registered_bundle.snapshot.formal_research_approved)
    slot_key = _slot_key(patient_id, body.timepoint)
    occupied = db.exec(select(AssessmentEvent.event_id).where(
        AssessmentEvent.active_protocol_slot_key == slot_key,
    )).first()
    if occupied is not None:
        _fail(409, "assessment_active_slot_conflict", "participant already has an open assessment at this timepoint")
    observed_at = now or _now()
    event = AssessmentEvent(
        event_id=_new_id("aev"),
        patient_id=patient_id,
        assigned_assessor_id=assigned_assessor_id,
        timepoint=body.timepoint,
        scheduled_date=body.scheduled_date,
        status="due",
        revision=1,
        is_simulation=is_simulation,
        data_classification=classification,
        formal_outcome_eligible=formal_eligible,
        definition_bundle_id=registered_bundle.snapshot.bundle_id,
        definition_bundle_digest=registered_bundle.snapshot.bundle_digest,
        active_protocol_slot_key=slot_key,
        created_by=actor_id,
        created_at=observed_at,
        updated_at=observed_at,
    )
    db.add(event)
    instances: list[AssessmentInstance] = []
    for snapshot in sorted(
        registered_bundle.snapshot.definitions, key=lambda row: row.category_key
    ):
        instance = AssessmentInstance(
            instance_id=_new_id("ain"),
            event_id=event.event_id,
            patient_id=patient_id,
            category_key=snapshot.category_key,
            definition_bundle_id=registered_bundle.snapshot.bundle_id,
            definition_bundle_digest=registered_bundle.snapshot.bundle_digest,
            definition_id=snapshot.definition_id,
            instrument_id=snapshot.instrument_id,
            instrument_version=snapshot.instrument_version,
            definition_digest=snapshot.definition_digest,
            item_set_digest=snapshot.item_set_digest,
            administration_protocol_digest=snapshot.administration_protocol_digest,
            response_schema_digest=snapshot.response_schema_digest,
            result_schema_digest=snapshot.result_schema_digest,
            missingness_rule_digest=snapshot.missingness_rule_digest,
            stopping_rule_digest=snapshot.stopping_rule_digest,
            scoring_algorithm_id=snapshot.scoring_algorithm_id,
            scoring_algorithm_version=snapshot.scoring_algorithm_version,
            scoring_algorithm_digest=snapshot.scoring_algorithm_digest,
            score_min=snapshot.score_min,
            score_max=snapshot.score_max,
            score_direction=snapshot.score_direction,
            score_rounding_rule=snapshot.score_rounding_rule,
            automatic_scoring_permitted=snapshot.automatic_scoring_permitted,
            item_response_storage_permitted=snapshot.item_response_storage_permitted,
            result_storage_permitted=snapshot.result_storage_permitted,
            result_export_permitted=snapshot.result_export_permitted,
            required_item_count=len(snapshot.items),
            status="due",
            revision=1,
            is_simulation=is_simulation,
            data_classification=classification,
            formal_outcome_eligible=formal_eligible,
            created_at=observed_at,
            updated_at=observed_at,
        )
        instances.append(instance)
        db.add(instance)
    db.flush()
    if len(instances) != 2:
        _fail(409, "assessment_state_invalid", "definition bundle did not create exactly two instances")
    _append_command(
        db, event=event, command_type="create",
        idempotency_key=body.idempotency_key, request_hash=request_hash,
        expected_event_revision=0, actor_id=actor_id, actor_role=actor_role,
        result_entity_type="assessment_event", result_entity_id=event.event_id,
        created_at=observed_at)
    db.flush()
    return event_receipt(db, event.event_id)


def start_event(
    db: Session,
    *,
    event_id: str,
    body: StartAssessmentEventIn,
    actor_id: str,
    actor_role: ActorRole,
    now: datetime | None = None,
) -> AssessmentEventOut:
    request_hash = _request_hash("start", {"event_id": event_id, **_body_payload(body)})
    replay = _command_replay(
        db, idempotency_key=body.idempotency_key, command_type="start",
        request_hash=request_hash, actor_id=actor_id, actor_role=actor_role)
    if replay is not None:
        return replay
    event = _locked_event(db, event_id)
    replay = _command_replay(
        db, idempotency_key=body.idempotency_key, command_type="start",
        request_hash=request_hash, actor_id=actor_id, actor_role=actor_role)
    if replay is not None:
        return replay
    _assert_assigned(event, actor_id, actor_role)
    _assert_revision(event, body.expected_event_revision)
    if event.status != "due":
        _fail(409, "assessment_transition_invalid", "only a due event can start")
    patient = _locked_patient(db, event.patient_id)
    _assert_patient_admission(patient, is_simulation=event.is_simulation)
    other_active_event = db.exec(select(AssessmentEvent.event_id).where(
        AssessmentEvent.patient_id == event.patient_id,
        AssessmentEvent.event_id != event.event_id,
        AssessmentEvent.status.in_({"in_progress", "awaiting_closeout"}),
    ).limit(1)).first()
    if other_active_event is not None:
        _fail(
            409,
            "assessment_patient_event_open",
            "participant already has another active assessment event",
        )
    rows = _instances(db, event.event_id)
    if len(rows) != 2 or any(row.status != "due" or row.revision != 1 for row in rows):
        _fail(409, "assessment_state_invalid", "due event has inconsistent instances")
    for row in rows:
        _registered_definition(event, row)
    observed_at = now or _now()
    event.status = "in_progress"
    event.started_at = observed_at
    event.revision += 1
    event.updated_at = observed_at
    db.add(event)
    for row in rows:
        row.status = "in_progress"
        row.revision += 1
        row.updated_at = observed_at
        db.add(row)
    _append_command(
        db, event=event, command_type="start",
        idempotency_key=body.idempotency_key, request_hash=request_hash,
        expected_event_revision=body.expected_event_revision,
        actor_id=actor_id, actor_role=actor_role,
        result_entity_type="assessment_event", result_entity_id=event.event_id,
        created_at=observed_at)
    db.flush()
    return event_receipt(db, event.event_id)


def cancel_event(
    db: Session,
    *,
    event_id: str,
    body: CancelAssessmentEventIn,
    actor_id: str,
    actor_role: ActorRole,
    now: datetime | None = None,
) -> AssessmentEventOut:
    request_hash = _request_hash("cancel", {"event_id": event_id, **_body_payload(body)})
    replay = _command_replay(
        db, idempotency_key=body.idempotency_key, command_type="cancel",
        request_hash=request_hash, actor_id=actor_id, actor_role=actor_role)
    if replay is not None:
        return replay
    event = _locked_event(db, event_id)
    replay = _command_replay(
        db, idempotency_key=body.idempotency_key, command_type="cancel",
        request_hash=request_hash, actor_id=actor_id, actor_role=actor_role)
    if replay is not None:
        return replay
    _assert_actor_role(actor_role)
    if event.assigned_assessor_id != actor_id and actor_role not in {"admin", "local_m0"}:
        _fail(403, "assessment_assessor_mismatch", "assessment belongs to another assessor")
    _assert_revision(event, body.expected_event_revision)
    if event.status != "due":
        _fail(409, "assessment_transition_invalid", "only an unstarted due event can be cancelled")
    _locked_patient(db, event.patient_id)
    rows = _instances(db, event.event_id)
    if len(rows) != 2 or any(row.status != "due" for row in rows):
        _fail(409, "assessment_state_invalid", "due event has touched instances")
    observed_at = now or _now()
    event.status = "cancelled"
    event.revision += 1
    event.active_protocol_slot_key = None
    event.cancellation_reason_code = body.reason_code
    event.cancelled_by = actor_id
    event.cancelled_at = observed_at
    event.updated_at = observed_at
    db.add(event)
    _append_command(
        db, event=event, command_type="cancel",
        idempotency_key=body.idempotency_key, request_hash=request_hash,
        expected_event_revision=body.expected_event_revision,
        actor_id=actor_id, actor_role=actor_role,
        result_entity_type="assessment_event", result_entity_id=event.event_id,
        reason_code=body.reason_code, created_at=observed_at)
    db.flush()
    return event_receipt(db, event.event_id)


def submit_response(
    db: Session,
    *,
    event_id: str,
    instance_id: str,
    item_key: str,
    body: SubmitAssessmentResponseIn,
    actor_id: str,
    actor_role: ActorRole,
    artifact_authorizer: AssessmentArtifactAuthorizer | None = None,
    now: datetime | None = None,
) -> AssessmentEventOut:
    request_hash = _request_hash("response", {
        "event_id": event_id,
        "instance_id": instance_id,
        "item_key": item_key,
        **_body_payload(body),
    })
    replay = _command_replay(
        db, idempotency_key=body.idempotency_key, command_type="response",
        request_hash=request_hash, actor_id=actor_id, actor_role=actor_role)
    if replay is not None:
        return replay
    event = _locked_event(db, event_id)
    replay = _command_replay(
        db, idempotency_key=body.idempotency_key, command_type="response",
        request_hash=request_hash, actor_id=actor_id, actor_role=actor_role)
    if replay is not None:
        return replay
    _assert_assigned(event, actor_id, actor_role)
    _assert_revision(event, body.expected_event_revision)
    if event.status != "in_progress":
        _fail(409, "assessment_transition_invalid", "responses require an in-progress event")
    _locked_patient(db, event.patient_id)
    instance = _locked_instance(db, event=event, instance_id=instance_id)
    _assert_instance_revision(instance, body.expected_instance_revision)
    if instance.status != "in_progress":
        _fail(409, "assessment_transition_invalid", "responses require an in-progress instance")
    registered = _registered_definition(event, instance)
    latest = db.exec(select(func.max(AssessmentItemResponse.item_revision)).where(
        AssessmentItemResponse.instance_id == instance.instance_id,
        AssessmentItemResponse.item_key == item_key,
    )).one()
    latest_revision = int(latest or 0)
    if latest_revision != body.expected_item_revision:
        _fail(409, "assessment_item_revision_conflict", "item response revision changed")
    raw_payload = body.response.model_dump(mode="python", exclude_unset=True)
    try:
        normalized = registered.validate_response(item_key, raw_payload).payload
    except assessment_definitions.AssessmentDefinitionError as exc:
        _definition_error(exc)
    if not isinstance(normalized, dict):
        _fail(409, "assessment_definition_adapter_invalid", "response validator returned a non-object")
    supplied_artifact = raw_payload.get("authorized_artifact_digest")
    normalized_artifact = normalized.get("authorized_artifact_digest")
    if normalized_artifact != supplied_artifact:
        _fail(409, "assessment_definition_adapter_invalid", "response validator changed artifact authority")
    if normalized_artifact is not None and (
        artifact_authorizer is None
        or not artifact_authorizer(
            db,
            event,
            instance,
            item_key,
            latest_revision + 1,
            str(normalized_artifact),
        )
    ):
        _fail(
            409,
            "assessment_artifact_not_authorized",
            "source artifact is not authorized for this assessment instance",
        )
    if normalized_artifact is not None:
        prior_artifact_use = db.exec(select(AssessmentItemResponse).where(
            AssessmentItemResponse.authorized_artifact_digest
            == str(normalized_artifact),
        )).first()
        if prior_artifact_use is not None:
            _fail(
                409,
                "assessment_artifact_already_bound",
                "source artifact receipt is already bound to another response",
            )
    response_json = _canonical_json(normalized, kind="response")
    response_digest = _digest({
        "schema": "assessment-item-response.v1",
        "definition_digest": instance.definition_digest,
        "item_key": item_key,
        "response": normalized,
    })
    observed_at = now or _now()
    expected_event_revision = event.revision
    expected_instance_revision = instance.revision
    event.revision += 1
    event.updated_at = observed_at
    instance.revision += 1
    instance.updated_at = observed_at
    response = AssessmentItemResponse(
        response_id=_new_id("air"),
        instance_id=instance.instance_id,
        event_id=event.event_id,
        patient_id=event.patient_id,
        item_key=item_key,
        item_revision=latest_revision + 1,
        expected_item_revision=latest_revision,
        instance_revision=instance.revision,
        event_revision=event.revision,
        response_json=response_json,
        response_digest=response_digest,
        authorized_artifact_digest=(
            str(normalized_artifact) if normalized_artifact is not None else None),
        idempotency_key_sha256=_sha256_text(body.idempotency_key),
        request_hash=request_hash,
        submitted_by=actor_id,
        submitted_at=observed_at,
    )
    db.add(event)
    db.add(instance)
    db.add(response)
    _append_command(
        db, event=event, instance=instance, command_type="response",
        idempotency_key=body.idempotency_key, request_hash=request_hash,
        expected_event_revision=expected_event_revision,
        expected_target_revision=expected_instance_revision,
        actor_id=actor_id, actor_role=actor_role,
        result_entity_type="assessment_item_response",
        result_entity_id=response.response_id, item_key=item_key,
        created_at=observed_at)
    db.flush()
    return event_receipt(db, event.event_id)


def _latest_responses(
    db: Session,
    instance: AssessmentInstance,
) -> tuple[dict[str, dict[str, object]], list[AssessmentItemResponse]]:
    rows = list(db.exec(select(AssessmentItemResponse).where(
        AssessmentItemResponse.instance_id == instance.instance_id,
    ).order_by(AssessmentItemResponse.item_key, AssessmentItemResponse.item_revision)))
    latest: dict[str, AssessmentItemResponse] = {}
    for row in rows:
        if row.event_id != instance.event_id or row.patient_id != instance.patient_id:
            _fail(409, "assessment_state_invalid", "stored response parent binding is inconsistent")
        prior = latest.get(row.item_key)
        if prior is None and (
            row.item_revision != 1 or row.expected_item_revision != 0
        ):
            _fail(409, "assessment_state_invalid", "item response history must begin at revision one")
        if prior is not None and (
            row.item_revision != prior.item_revision + 1
            or row.expected_item_revision != prior.item_revision
        ):
            _fail(409, "assessment_state_invalid", "item response revisions are discontinuous")
        if row.item_revision != row.expected_item_revision + 1:
            _fail(409, "assessment_state_invalid", "item response revision transition is invalid")
        latest[row.item_key] = row
    payloads: dict[str, dict[str, object]] = {}
    for key, row in latest.items():
        try:
            parsed = json.loads(row.response_json)
        except (TypeError, json.JSONDecodeError):
            _fail(409, "assessment_state_invalid", "stored response JSON is invalid")
        if not isinstance(parsed, dict):
            _fail(409, "assessment_state_invalid", "stored response must be an object")
        if row.response_json != _canonical_json(parsed, kind="response"):
            _fail(409, "assessment_state_invalid", "stored response JSON is not canonical")
        if parsed.get("authorized_artifact_digest") != row.authorized_artifact_digest:
            _fail(409, "assessment_state_invalid", "stored response artifact binding is inconsistent")
        expected_digest = _digest({
            "schema": "assessment-item-response.v1",
            "definition_digest": instance.definition_digest,
            "item_key": row.item_key,
            "response": parsed,
        })
        if not _is_digest(row.response_digest) or row.response_digest != expected_digest:
            _fail(409, "assessment_state_invalid", "stored response digest is invalid")
        payloads[key] = parsed
    return payloads, [latest[key] for key in sorted(latest)]


def _advance_after_terminal(
    event: AssessmentEvent,
    instances: list[AssessmentInstance],
) -> None:
    if all(row.status in _TERMINAL_INSTANCE_STATUSES for row in instances):
        event.status = "awaiting_closeout"


def complete_instance(
    db: Session,
    *,
    event_id: str,
    instance_id: str,
    body: CompleteAssessmentInstanceIn,
    actor_id: str,
    actor_role: ActorRole,
    now: datetime | None = None,
) -> AssessmentEventOut:
    request_hash = _request_hash("complete", {
        "event_id": event_id, "instance_id": instance_id, **_body_payload(body)})
    replay = _command_replay(
        db, idempotency_key=body.idempotency_key, command_type="complete",
        request_hash=request_hash, actor_id=actor_id, actor_role=actor_role)
    if replay is not None:
        return replay
    event = _locked_event(db, event_id)
    replay = _command_replay(
        db, idempotency_key=body.idempotency_key, command_type="complete",
        request_hash=request_hash, actor_id=actor_id, actor_role=actor_role)
    if replay is not None:
        return replay
    _assert_assigned(event, actor_id, actor_role)
    _assert_revision(event, body.expected_event_revision)
    if event.status != "in_progress":
        _fail(409, "assessment_transition_invalid", "completion requires an in-progress event")
    _locked_patient(db, event.patient_id)
    instance = _locked_instance(db, event=event, instance_id=instance_id)
    _assert_instance_revision(instance, body.expected_instance_revision)
    if instance.status != "in_progress":
        _fail(409, "assessment_transition_invalid", "instance is not available for completion")
    registered = _registered_definition(event, instance)
    payloads, latest_rows = _latest_responses(db, instance)
    allowed_keys = {item.item_key for item in registered.snapshot.items}
    if not set(payloads).issubset(allowed_keys):
        _fail(409, "assessment_state_invalid", "stored response contains an unknown item")
    try:
        score_result = registered.score_responses(payloads)
    except assessment_definitions.AssessmentDefinitionError as exc:
        _definition_error(exc)
    score = score_result.score
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)):
        _fail(409, "assessment_scoring_invalid", "definition scorer returned an invalid score")
    score = float(score)
    if score < instance.score_min or score > instance.score_max:
        _fail(409, "assessment_scoring_invalid", "definition scorer returned an out-of-range score")
    answered = score_result.answered_item_count
    missing = score_result.missing_item_count
    if (
        isinstance(answered, bool) or isinstance(missing, bool)
        or not isinstance(answered, int) or not isinstance(missing, int)
        or answered < 0 or missing < 0
        or answered + missing != instance.required_item_count
    ):
        _fail(409, "assessment_scoring_invalid", "definition scorer returned invalid completeness facts")
    if score_result.stopped_early != (score_result.stopping_reason_code is not None):
        _fail(409, "assessment_scoring_invalid", "stopping facts are inconsistent")
    if score_result.stopping_reason_code is not None and (
        not score_result.stopping_reason_code.strip()
        or len(score_result.stopping_reason_code) > 200
    ):
        _fail(409, "assessment_scoring_invalid", "stopping reason is invalid")
    if not isinstance(score_result.result, dict):
        _fail(409, "assessment_scoring_invalid", "definition scorer returned a non-object result")
    result_json = _canonical_json(score_result.result, kind="result")
    response_set_projection = {
        "schema": "assessment-item-response-set.v1",
        "definition_digest": instance.definition_digest,
        "responses": [
            {
                "item_key": row.item_key,
                "item_revision": row.item_revision,
                "response_digest": row.response_digest,
                "authorized_artifact_digest": row.authorized_artifact_digest,
            }
            for row in latest_rows
        ],
    }
    response_set_digest = _digest(response_set_projection)
    result_digest = _digest({
        "schema": "assessment-result.v1",
        "definition_digest": instance.definition_digest,
        "item_response_set_digest": response_set_digest,
        "score": score,
        "result": score_result.result,
        "answered_item_count": answered,
        "missing_item_count": missing,
        "stopped_early": score_result.stopped_early,
        "stopping_reason_code": score_result.stopping_reason_code,
    })
    observed_at = now or _now()
    expected_event_revision = event.revision
    expected_instance_revision = instance.revision
    evidence = AssessmentScoringEvidence(
        evidence_id=_new_id("ase"),
        instance_id=instance.instance_id,
        event_id=event.event_id,
        patient_id=event.patient_id,
        category_key=instance.category_key,
        definition_digest=instance.definition_digest,
        item_response_set_digest=response_set_digest,
        scoring_algorithm_id=instance.scoring_algorithm_id,
        scoring_algorithm_version=instance.scoring_algorithm_version,
        scoring_algorithm_digest=instance.scoring_algorithm_digest,
        result_schema_digest=instance.result_schema_digest,
        result_json=result_json,
        result_digest=result_digest,
        score=score,
        score_min=instance.score_min,
        score_max=instance.score_max,
        answered_item_count=answered,
        missing_item_count=missing,
        stopped_early=score_result.stopped_early,
        stopping_reason_code=score_result.stopping_reason_code,
        is_simulation=event.is_simulation,
        formal_outcome_eligible=event.formal_outcome_eligible,
        scored_by="server:" + instance.scoring_algorithm_id,
        scored_at=observed_at,
    )
    instance.status = "completed"
    instance.completed_at = observed_at
    instance.revision += 1
    instance.updated_at = observed_at
    event.revision += 1
    event.updated_at = observed_at
    all_instances = _instances(db, event.event_id)
    for index, row in enumerate(all_instances):
        if row.instance_id == instance.instance_id:
            all_instances[index] = instance
    _advance_after_terminal(event, all_instances)
    db.add(evidence)
    db.add(instance)
    db.add(event)
    _append_command(
        db, event=event, instance=instance, command_type="complete",
        idempotency_key=body.idempotency_key, request_hash=request_hash,
        expected_event_revision=expected_event_revision,
        expected_target_revision=expected_instance_revision,
        actor_id=actor_id, actor_role=actor_role,
        result_entity_type="assessment_scoring_evidence",
        result_entity_id=evidence.evidence_id, created_at=observed_at)
    db.flush()
    return event_receipt(db, event.event_id)


def approve_deferral(
    db: Session,
    *,
    event_id: str,
    instance_id: str,
    body: ApproveAssessmentDeferralIn,
    actor_id: str,
    actor_role: ActorRole,
    now: datetime | None = None,
) -> AssessmentEventOut:
    request_hash = _request_hash("defer", {
        "event_id": event_id, "instance_id": instance_id, **_body_payload(body)})
    replay = _command_replay(
        db, idempotency_key=body.idempotency_key, command_type="defer",
        request_hash=request_hash, actor_id=actor_id, actor_role=actor_role)
    if replay is not None:
        return replay
    event = _locked_event(db, event_id)
    replay = _command_replay(
        db, idempotency_key=body.idempotency_key, command_type="defer",
        request_hash=request_hash, actor_id=actor_id, actor_role=actor_role)
    if replay is not None:
        return replay
    if actor_role not in {"admin", "local_m0"}:
        _fail(403, "assessment_deferral_forbidden", "deferral approval requires an administrator")
    if not event.is_simulation and actor_role != "admin":
        _fail(403, "assessment_deferral_forbidden", "research deferral approval requires an administrator")
    _assert_revision(event, body.expected_event_revision)
    if event.status != "in_progress":
        _fail(409, "assessment_transition_invalid", "deferral requires an in-progress event")
    _locked_patient(db, event.patient_id)
    instance = _locked_instance(db, event=event, instance_id=instance_id)
    _assert_instance_revision(instance, body.expected_instance_revision)
    if instance.status != "in_progress":
        _fail(409, "assessment_transition_invalid", "instance is not available for deferral")
    _registered_definition(event, instance)
    observed_at = now or _now()
    if body.deferred_until <= observed_at.date():
        _fail(422, "assessment_deferral_date_invalid", "deferred_until must be a future date")
    expected_event_revision = event.revision
    expected_instance_revision = instance.revision
    approval = AssessmentDeferralApproval(
        deferral_id=_new_id("adf"),
        instance_id=instance.instance_id,
        event_id=event.event_id,
        patient_id=event.patient_id,
        category_key=instance.category_key,
        definition_digest=instance.definition_digest,
        reason_code=body.reason_code,
        deferred_until=body.deferred_until,
        approved_by=actor_id,
        approved_role=actor_role,
        approved_at=observed_at,
    )
    instance.status = "approved_deferred"
    instance.revision += 1
    instance.updated_at = observed_at
    event.revision += 1
    event.updated_at = observed_at
    all_instances = _instances(db, event.event_id)
    for index, row in enumerate(all_instances):
        if row.instance_id == instance.instance_id:
            all_instances[index] = instance
    _advance_after_terminal(event, all_instances)
    db.add(approval)
    db.add(instance)
    db.add(event)
    _append_command(
        db, event=event, instance=instance, command_type="defer",
        idempotency_key=body.idempotency_key, request_hash=request_hash,
        expected_event_revision=expected_event_revision,
        expected_target_revision=expected_instance_revision,
        actor_id=actor_id, actor_role=actor_role,
        result_entity_type="assessment_deferral_approval",
        result_entity_id=approval.deferral_id, created_at=observed_at)
    db.flush()
    return event_receipt(db, event.event_id)


def close_event(
    db: Session,
    *,
    event_id: str,
    body: CloseAssessmentEventIn,
    actor_id: str,
    actor_role: ActorRole,
    now: datetime | None = None,
) -> AssessmentEventOut:
    request_hash = _request_hash("close", {"event_id": event_id, **_body_payload(body)})
    replay = _command_replay(
        db, idempotency_key=body.idempotency_key, command_type="close",
        request_hash=request_hash, actor_id=actor_id, actor_role=actor_role)
    if replay is not None:
        return replay
    event = _locked_event(db, event_id)
    replay = _command_replay(
        db, idempotency_key=body.idempotency_key, command_type="close",
        request_hash=request_hash, actor_id=actor_id, actor_role=actor_role)
    if replay is not None:
        return replay
    _assert_assigned(event, actor_id, actor_role)
    _assert_revision(event, body.expected_event_revision)
    if event.status != "awaiting_closeout":
        _fail(409, "assessment_transition_invalid", "event is not ready for closeout")
    _locked_patient(db, event.patient_id)
    rows = _instances(db, event.event_id)
    terminal_digest = _instance_terminal_digest(db, event, rows)
    observed_at = now or _now()
    expected_event_revision = event.revision
    event.status = "closed"
    event.revision += 1
    event.active_protocol_slot_key = None
    event.closed_at = observed_at
    event.updated_at = observed_at
    closeout = AssessmentEventCloseout(
        closeout_id=_new_id("acl"),
        event_id=event.event_id,
        patient_id=event.patient_id,
        event_revision=event.revision,
        instance_terminal_digest=terminal_digest,
        report_status=body.report_status,
        fatigue_observed=body.fatigue_observed,
        distress_or_discomfort_observed=body.distress_or_discomfort_observed,
        participant_declined_to_continue=body.participant_declined_to_continue,
        staff_assistance_occurred=body.staff_assistance_occurred,
        environment_interruption_occurred=body.environment_interruption_occurred,
        device_or_network_interruption_occurred=body.device_or_network_interruption_occurred,
        note=body.note,
        idempotency_key_sha256=_sha256_text(body.idempotency_key),
        request_hash=request_hash,
        closed_by=actor_id,
        closed_at=observed_at,
    )
    db.add(event)
    db.add(closeout)
    _append_command(
        db, event=event, command_type="close",
        idempotency_key=body.idempotency_key, request_hash=request_hash,
        expected_event_revision=expected_event_revision,
        actor_id=actor_id, actor_role=actor_role,
        result_entity_type="assessment_event_closeout",
        result_entity_id=closeout.closeout_id, created_at=observed_at)
    db.flush()
    return event_receipt(db, event.event_id)


def today_events(
    db: Session,
    *,
    as_of_date: date,
    assigned_assessor_id: str | None = None,
) -> AssessmentTodayOut:
    # A queue is an operational recovery surface, not a calendar snapshot:
    # overdue due events remain visible, and already-started/awaiting-closeout
    # events must survive midnight until they are explicitly closed.
    statement = select(AssessmentEvent).where(or_(
        and_(
            AssessmentEvent.status == "due",
            AssessmentEvent.scheduled_date <= as_of_date,
        ),
        AssessmentEvent.status.in_({"in_progress", "awaiting_closeout"}),
    ))
    if assigned_assessor_id is not None:
        statement = statement.where(
            AssessmentEvent.assigned_assessor_id == assigned_assessor_id)
    rows = list(db.exec(statement.order_by(
        AssessmentEvent.created_at, AssessmentEvent.event_id)))
    visible_rows = []
    for row in rows:
        patient = db.get(Patient, row.patient_id)
        if patient is None or session_admission.patient_content_sealed(patient):
            continue
        visible_rows.append(row)
    return AssessmentTodayOut(
        as_of_date=as_of_date,
        events=[event_receipt(db, row.event_id) for row in visible_rows],
    )
