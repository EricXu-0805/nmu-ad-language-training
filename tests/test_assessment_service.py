"""Independent formal-assessment state machine and evidence-chain tests."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from pydantic import ValidationError
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.assessment_contract import (
    ApproveAssessmentDeferralIn,
    AssessmentResponseValue,
    CancelAssessmentEventIn,
    CloseAssessmentEventIn,
    CompleteAssessmentInstanceIn,
    CreateAssessmentEventIn,
    StartAssessmentEventIn,
    SubmitAssessmentResponseIn,
)
from app.assessment_definitions import (
    build_synthetic_bundle,
    current_bundle,
    install_synthetic_bundle_for_testing,
    install_synthetic_bundles_for_testing,
    registered_bundles,
)
from app.assessment_service import (
    AssessmentServiceError,
    approve_deferral,
    cancel_event,
    close_event,
    complete_instance,
    create_event,
    event_receipt,
    start_event,
    submit_response,
    today_events,
)
from app.enums import ConsentType
from app.models import (
    AssessmentCommand,
    AssessmentDeferralApproval,
    AssessmentEvent,
    AssessmentEventCloseout,
    AssessmentInstance,
    AssessmentItemResponse,
    AssessmentScoringEvidence,
    Patient,
)


TODAY = date(2026, 7, 19)
ASSESSOR = "ASSESSOR-001"


@pytest.fixture
def engine():
    value = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(value)
    return value


@pytest.fixture
def db(engine):
    with Session(engine) as value:
        yield value


@pytest.fixture
def synthetic_bundle():
    with install_synthetic_bundle_for_testing() as bundle:
        yield bundle


def _add_simulation_patient(db: Session, patient_id: str = "SIM-ASSESS") -> Patient:
    row = Patient(
        patient_id=patient_id,
        is_simulation_subject=True,
        consent_status="active",
        recording_allowed=True,
    )
    db.add(row)
    db.commit()
    return row


def _add_research_patient(db: Session, patient_id: str = "P-ASSESS") -> Patient:
    row = Patient(
        patient_id=patient_id,
        consent_status="active",
        consent_type=ConsentType.本人同意,
        mandarin_eligible=True,
        recording_allowed=True,
    )
    db.add(row)
    db.commit()
    return row


def _create(
    db: Session,
    *,
    patient_id: str = "SIM-ASSESS",
    timepoint: str = "pretest",
    key: str = "assessment-create-0001",
):
    receipt = create_event(
        db,
        patient_id=patient_id,
        body=CreateAssessmentEventIn(
            timepoint=timepoint,
            scheduled_date=TODAY,
            idempotency_key=key,
        ),
        assigned_assessor_id=ASSESSOR,
        actor_id=ASSESSOR,
        actor_role="researcher",
        now=datetime(2026, 7, 19, 1, 0),
    )
    db.commit()
    return receipt


def _start(db: Session, receipt, key: str = "assessment-start-0001"):
    result = start_event(
        db,
        event_id=receipt.event_id,
        body=StartAssessmentEventIn(
            expected_event_revision=receipt.revision,
            idempotency_key=key,
        ),
        actor_id=ASSESSOR,
        actor_role="researcher",
        now=datetime(2026, 7, 19, 1, 5),
    )
    db.commit()
    return result


def _item_keys(instance) -> tuple[str, str]:
    if instance.category_key == "untrained_standardized_naming":
        return "naming_01", "naming_02"
    return "functional_01", "functional_02"


def _respond_all(
    db: Session,
    receipt,
    instance_id: str,
    *,
    artifact_digest: str | None = None,
    key_prefix: str,
):
    current = next(row for row in receipt.instances if row.instance_id == instance_id)
    for index, item_key in enumerate(_item_keys(current), start=1):
        item_artifact_digest = (
            f"{artifact_digest[:-1]}{index}"
            if artifact_digest is not None else None
        )
        response = AssessmentResponseValue(
            value=index,
            authorized_artifact_digest=item_artifact_digest,
        )
        receipt = submit_response(
            db,
            event_id=receipt.event_id,
            instance_id=instance_id,
            item_key=item_key,
            body=SubmitAssessmentResponseIn(
                response=response,
                expected_event_revision=receipt.revision,
                expected_instance_revision=current.revision,
                expected_item_revision=0,
                idempotency_key=f"{key_prefix}-{index:02d}",
            ),
            actor_id=ASSESSOR,
            actor_role="researcher",
            artifact_authorizer=(
                (lambda _db, _event, _instance, authorized_item_key,
                        authorized_item_revision, digest:
                    authorized_item_key == item_key
                    and authorized_item_revision == 1
                    and digest == item_artifact_digest)
                if item_artifact_digest is not None else None
            ),
            now=datetime(2026, 7, 19, 1, 5 + index),
        )
        db.commit()
        current = next(
            row for row in receipt.instances if row.instance_id == instance_id)
    return receipt


def _complete(db: Session, receipt, instance_id: str, key: str):
    current = next(row for row in receipt.instances if row.instance_id == instance_id)
    result = complete_instance(
        db,
        event_id=receipt.event_id,
        instance_id=instance_id,
        body=CompleteAssessmentInstanceIn(
            expected_event_revision=receipt.revision,
            expected_instance_revision=current.revision,
            idempotency_key=key,
        ),
        actor_id=ASSESSOR,
        actor_role="researcher",
        now=datetime(2026, 7, 19, 1, 20),
    )
    db.commit()
    return result


def test_production_registry_is_empty_and_creation_fails_closed(db):
    _add_research_patient(db)
    assert registered_bundles() == ()
    with pytest.raises(AssessmentServiceError) as caught:
        _create(db, patient_id="P-ASSESS")
    assert caught.value.code == "assessment_definitions_not_ready"
    db.rollback()
    assert db.exec(select(AssessmentEvent)).all() == []
    assert db.exec(select(AssessmentInstance)).all() == []


def test_create_freezes_exact_two_definitions_and_simulation_is_never_formal(
    db, synthetic_bundle,
):
    _add_simulation_patient(db)
    receipt = _create(db)
    assert receipt.status == "due"
    assert receipt.revision == 1
    assert receipt.data_classification == "simulation"
    assert receipt.formal_outcome_eligible is False
    assert receipt.definition_bundle_digest == synthetic_bundle.snapshot.bundle_digest
    assert {row.category_key for row in receipt.instances} == {
        "untrained_standardized_naming", "functional_communication",
    }
    assert all(row.status == "due" for row in receipt.instances)
    assert all(row.definition_bundle_digest == receipt.definition_bundle_digest
               for row in receipt.instances)
    assert all(row.data_classification == "simulation" for row in receipt.instances)
    assert all(row.formal_outcome_eligible is False for row in receipt.instances)
    assert all(row.automatic_scoring_permitted for row in receipt.instances)
    assert all(row.item_response_storage_permitted for row in receipt.instances)
    assert all(row.result_storage_permitted for row in receipt.instances)
    assert all(row.result_export_permitted is False for row in receipt.instances)
    assert len(db.exec(select(AssessmentCommand)).all()) == 1


def test_bundle_rollover_keeps_inflight_event_on_archived_definition(
    db, synthetic_bundle,
):
    _add_simulation_patient(db)
    _add_simulation_patient(db, "SIM-ASSESS-ROLLOVER-NEW")
    old_event = _create(db)
    new_bundle = build_synthetic_bundle(
        bundle_id="synthetic-two-outcomes-v2")

    with install_synthetic_bundles_for_testing(
        (synthetic_bundle, new_bundle),
        active_bundle_id=new_bundle.snapshot.bundle_id,
    ):
        assert current_bundle().snapshot.bundle_id == new_bundle.snapshot.bundle_id
        assert current_bundle(
            synthetic_bundle.snapshot.bundle_id,
        ).snapshot.bundle_id == synthetic_bundle.snapshot.bundle_id

        continued = _start(db, old_event, key="assessment-rollover-old-start")
        assert continued.definition_bundle_id == synthetic_bundle.snapshot.bundle_id

        created_after_rollover = _create(
            db,
            patient_id="SIM-ASSESS-ROLLOVER-NEW",
            key="assessment-rollover-new-create",
        )
        assert created_after_rollover.definition_bundle_id == (
            new_bundle.snapshot.bundle_id)
        assert {row.bundle_id for row in registered_bundles()} == {
            synthetic_bundle.snapshot.bundle_id,
            new_bundle.snapshot.bundle_id,
        }


def test_due_does_not_accept_responses_and_start_transitions_both_instances(
    db, synthetic_bundle,
):
    _add_simulation_patient(db)
    receipt = _create(db)
    first = receipt.instances[0]
    with pytest.raises(AssessmentServiceError) as caught:
        submit_response(
            db,
            event_id=receipt.event_id,
            instance_id=first.instance_id,
            item_key=_item_keys(first)[0],
            body=SubmitAssessmentResponseIn(
                response=AssessmentResponseValue(value=1),
                expected_event_revision=receipt.revision,
                expected_instance_revision=first.revision,
                expected_item_revision=0,
                idempotency_key="response-before-start-0001",
            ),
            actor_id=ASSESSOR,
            actor_role="researcher",
        )
    assert caught.value.code == "assessment_transition_invalid"
    db.rollback()

    receipt = _start(db, receipt)
    assert receipt.status == "in_progress"
    assert receipt.revision == 2
    assert all(row.status == "in_progress" and row.revision == 2
               for row in receipt.instances)


def test_read_path_rejects_instance_status_that_disagrees_with_command_chain(
    db, synthetic_bundle,
):
    _add_simulation_patient(db)
    receipt = _start(db, _create(db))
    db.exec(text(
        "UPDATE assessmentinstance SET status = 'due' "
        "WHERE event_id = :event_id"
    ), params={"event_id": receipt.event_id})
    db.commit()
    db.expire_all()

    with pytest.raises(AssessmentServiceError) as caught:
        event_receipt(db, receipt.event_id)
    assert caught.value.code == "assessment_state_invalid"


def test_start_rejects_another_active_timepoint_but_admin_can_supervise(
    db, synthetic_bundle,
):
    _add_simulation_patient(db)
    pretest = _create(db, timepoint="pretest", key="create-active-pretest")
    posttest = _create(db, timepoint="posttest", key="create-active-posttest")
    active = start_event(
        db,
        event_id=pretest.event_id,
        body=StartAssessmentEventIn(
            expected_event_revision=pretest.revision,
            idempotency_key="admin-supervised-start",
        ),
        actor_id="ADMIN-001",
        actor_role="admin",
    )
    db.commit()
    assert active.status == "in_progress"
    assert active.assigned_assessor_id == ASSESSOR

    with pytest.raises(AssessmentServiceError) as caught:
        start_event(
            db,
            event_id=posttest.event_id,
            body=StartAssessmentEventIn(
                expected_event_revision=posttest.revision,
                idempotency_key="second-timepoint-start",
            ),
            actor_id=ASSESSOR,
            actor_role="researcher",
        )
    assert caught.value.code == "assessment_patient_event_open"
    db.rollback()


def test_response_is_append_only_cas_idempotent_and_artifact_authorized(
    db, synthetic_bundle,
):
    _add_simulation_patient(db)
    receipt = _start(db, _create(db))
    first = receipt.instances[0]
    item_key = _item_keys(first)[0]
    artifact = "sha256:" + "a" * 64
    body = SubmitAssessmentResponseIn(
        response=AssessmentResponseValue(
            value=1, authorized_artifact_digest=artifact),
        expected_event_revision=receipt.revision,
        expected_instance_revision=first.revision,
        expected_item_revision=0,
        idempotency_key="response-idempotent-0001",
    )
    with pytest.raises(AssessmentServiceError) as caught:
        submit_response(
            db, event_id=receipt.event_id, instance_id=first.instance_id,
            item_key=item_key, body=body, actor_id=ASSESSOR,
            actor_role="researcher")
    assert caught.value.code == "assessment_artifact_not_authorized"
    db.rollback()

    result = submit_response(
        db, event_id=receipt.event_id, instance_id=first.instance_id,
        item_key=item_key, body=body, actor_id=ASSESSOR,
        actor_role="researcher",
        artifact_authorizer=(
            lambda _db, _event, _instance, authorized_item_key,
                    authorized_item_revision, digest: (
                authorized_item_key == item_key
                and authorized_item_revision == 1
                and digest == artifact)))
    db.commit()
    replay = submit_response(
        db, event_id=receipt.event_id, instance_id=first.instance_id,
        item_key=item_key, body=body, actor_id=ASSESSOR,
        actor_role="researcher")
    assert replay.revision == result.revision
    rows = db.exec(select(AssessmentItemResponse)).all()
    assert len(rows) == 1
    assert rows[0].authorized_artifact_digest == artifact
    assert rows[0].response_digest.startswith("sha256:")

    current = next(
        row for row in result.instances if row.instance_id == first.instance_id)
    reused = SubmitAssessmentResponseIn(
        response=AssessmentResponseValue(
            value=2, authorized_artifact_digest=artifact),
        expected_event_revision=result.revision,
        expected_instance_revision=current.revision,
        expected_item_revision=0,
        idempotency_key="response-artifact-reuse-0002",
    )
    with pytest.raises(AssessmentServiceError) as caught:
        submit_response(
            db,
            event_id=receipt.event_id,
            instance_id=first.instance_id,
            item_key=_item_keys(first)[1],
            body=reused,
            actor_id=ASSESSOR,
            actor_role="researcher",
            artifact_authorizer=(
                lambda _db, _event, _instance, _item_key,
                        _item_revision, digest: digest == artifact),
        )
    assert caught.value.code == "assessment_artifact_already_bound"
    db.rollback()

    changed = body.model_copy(update={
        "response": AssessmentResponseValue(value=2),
    })
    with pytest.raises(AssessmentServiceError) as caught:
        submit_response(
            db, event_id=receipt.event_id, instance_id=first.instance_id,
            item_key=item_key, body=changed, actor_id=ASSESSOR,
            actor_role="researcher")
    assert caught.value.code == "assessment_idempotency_conflict"
    db.rollback()

    row = db.get(AssessmentItemResponse, rows[0].response_id)
    row.response_json = '{"value":2}'
    db.add(row)
    with pytest.raises(RuntimeError, match="\u53ea\u8ffd\u52a0"):
        db.commit()
    db.rollback()

    # A direct database writer can satisfy the simple revision CHECK with a
    # forged first row at 2/1.  The authoritative read path must still reject a
    # history that does not begin at 1/0.
    db.exec(text(
        "UPDATE assessmentitemresponse "
        "SET item_revision = 2, expected_item_revision = 1 "
        "WHERE response_id = :response_id"
    ), params={"response_id": rows[0].response_id})
    db.commit()
    db.expire_all()
    with pytest.raises(AssessmentServiceError) as caught:
        event_receipt(db, receipt.event_id)
    assert caught.value.code == "assessment_state_invalid"


def test_command_result_entity_binding_is_verified_on_read(
    db, synthetic_bundle,
):
    _add_simulation_patient(db)
    receipt = _start(db, _create(db))
    instance = receipt.instances[0]
    result = submit_response(
        db,
        event_id=receipt.event_id,
        instance_id=instance.instance_id,
        item_key=_item_keys(instance)[0],
        body=SubmitAssessmentResponseIn(
            response=AssessmentResponseValue(value=1),
            expected_event_revision=receipt.revision,
            expected_instance_revision=instance.revision,
            expected_item_revision=0,
            idempotency_key="response-command-binding-0001",
        ),
        actor_id=ASSESSOR,
        actor_role="researcher",
    )
    db.commit()

    db.exec(text(
        "UPDATE assessmentcommand SET result_entity_id = :forged "
        "WHERE event_id = :event_id AND command_type = 'response'"
    ), params={"forged": "air_missing", "event_id": result.event_id})
    db.commit()
    db.expire_all()
    with pytest.raises(AssessmentServiceError) as caught:
        event_receipt(db, result.event_id)
    assert caught.value.code == "assessment_state_invalid"


def test_idempotent_replay_returns_current_projection_without_reapplying_effect(
    db, synthetic_bundle,
):
    _add_simulation_patient(db)
    receipt = _start(db, _create(db))
    instance = receipt.instances[0]
    first_key, second_key = _item_keys(instance)
    first_body = SubmitAssessmentResponseIn(
        response=AssessmentResponseValue(value=1),
        expected_event_revision=receipt.revision,
        expected_instance_revision=instance.revision,
        expected_item_revision=0,
        idempotency_key="response-current-projection-first",
    )
    after_first = submit_response(
        db,
        event_id=receipt.event_id,
        instance_id=instance.instance_id,
        item_key=first_key,
        body=first_body,
        actor_id=ASSESSOR,
        actor_role="researcher",
    )
    db.commit()
    current_instance = next(
        row for row in after_first.instances
        if row.instance_id == instance.instance_id)
    after_second = submit_response(
        db,
        event_id=receipt.event_id,
        instance_id=instance.instance_id,
        item_key=second_key,
        body=SubmitAssessmentResponseIn(
            response=AssessmentResponseValue(value=2),
            expected_event_revision=after_first.revision,
            expected_instance_revision=current_instance.revision,
            expected_item_revision=0,
            idempotency_key="response-current-projection-second",
        ),
        actor_id=ASSESSOR,
        actor_role="researcher",
    )
    db.commit()

    replay = submit_response(
        db,
        event_id=receipt.event_id,
        instance_id=instance.instance_id,
        item_key=first_key,
        body=first_body,
        actor_id=ASSESSOR,
        actor_role="researcher",
    )
    replay_instance = next(
        row for row in replay.instances if row.instance_id == instance.instance_id)
    assert replay.revision == after_second.revision
    assert replay_instance.item_response_count == 2
    assert len(db.exec(select(AssessmentItemResponse)).all()) == 2
    db.rollback()


def test_complete_uses_server_scorer_and_writes_immutable_evidence(
    db, synthetic_bundle,
):
    _add_simulation_patient(db)
    receipt = _start(db, _create(db))
    first = receipt.instances[0]
    receipt = _respond_all(
        db, receipt, first.instance_id, key_prefix="score-first")
    current = next(
        row for row in receipt.instances if row.instance_id == first.instance_id)
    body = CompleteAssessmentInstanceIn(
        expected_event_revision=receipt.revision,
        expected_instance_revision=current.revision,
        idempotency_key="complete-idempotent-0001",
    )
    receipt = complete_instance(
        db, event_id=receipt.event_id, instance_id=first.instance_id,
        body=body, actor_id=ASSESSOR, actor_role="researcher")
    db.commit()
    completed = next(
        row for row in receipt.instances if row.instance_id == first.instance_id)
    assert completed.status == "completed"
    assert completed.scoring_evidence.score == 3
    assert completed.scoring_evidence.result == {"total_score": 3.0}
    assert completed.scoring_evidence.answered_item_count == 2
    assert completed.scoring_evidence.missing_item_count == 0
    assert completed.scoring_evidence.stopped_early is False
    assert completed.scoring_evidence.formal_outcome_eligible is False
    assert receipt.status == "in_progress"

    replay = complete_instance(
        db, event_id=receipt.event_id, instance_id=first.instance_id,
        body=body, actor_id=ASSESSOR, actor_role="researcher")
    assert replay.revision == receipt.revision
    assert len(db.exec(select(AssessmentScoringEvidence)).all()) == 1

    evidence = db.exec(select(AssessmentScoringEvidence)).one()
    evidence.score = 4
    db.add(evidence)
    with pytest.raises(RuntimeError, match="\u4e0d\u53ef\u53d8"):
        db.commit()
    db.rollback()

    # The read path also verifies the content-addressed evidence, so a direct
    # database writer cannot make a syntactically valid replacement digest pass.
    db.exec(text(
        "UPDATE assessmentscoringevidence SET result_digest = :digest "
        "WHERE evidence_id = :evidence_id"
    ), params={
        "digest": "sha256:" + "0" * 64,
        "evidence_id": evidence.evidence_id,
    })
    db.commit()
    db.expire_all()
    with pytest.raises(AssessmentServiceError) as caught:
        event_receipt(db, receipt.event_id)
    assert caught.value.code == "assessment_state_invalid"


def test_deferral_is_admin_future_only_and_closeout_atomically_unlocks_switch(
    db, synthetic_bundle,
):
    _add_simulation_patient(db)
    receipt = _start(db, _create(db))
    first, second = receipt.instances
    receipt = _complete(
        db,
        _respond_all(db, receipt, first.instance_id, key_prefix="terminal-first"),
        first.instance_id,
        "complete-terminal-first",
    )
    second = next(
        row for row in receipt.instances if row.instance_id == second.instance_id)
    deferral = ApproveAssessmentDeferralIn(
        expected_event_revision=receipt.revision,
        expected_instance_revision=second.revision,
        idempotency_key="deferral-terminal-0001",
        reason_code="participant_unavailable",
        deferred_until=TODAY + timedelta(days=1),
    )
    with pytest.raises(AssessmentServiceError) as caught:
        approve_deferral(
            db, event_id=receipt.event_id, instance_id=second.instance_id,
            body=deferral, actor_id=ASSESSOR, actor_role="researcher",
            now=datetime(2026, 7, 19, 2, 0))
    assert caught.value.code == "assessment_deferral_forbidden"
    db.rollback()

    invalid_date = deferral.model_copy(update={
        "deferred_until": TODAY,
        "idempotency_key": "deferral-invalid-date-0001",
    })
    with pytest.raises(AssessmentServiceError) as caught:
        approve_deferral(
            db, event_id=receipt.event_id, instance_id=second.instance_id,
            body=invalid_date, actor_id="ADMIN-001", actor_role="admin",
            now=datetime(2026, 7, 19, 2, 0))
    assert caught.value.code == "assessment_deferral_date_invalid"
    db.rollback()

    receipt = approve_deferral(
        db, event_id=receipt.event_id, instance_id=second.instance_id,
        body=deferral, actor_id="ADMIN-001", actor_role="admin",
        now=datetime(2026, 7, 19, 2, 0))
    db.commit()
    assert receipt.status == "awaiting_closeout"
    deferred = next(
        row for row in receipt.instances if row.instance_id == second.instance_id)
    assert deferred.status == "approved_deferred"
    assert deferred.deferral.approved_role == "admin"
    assert deferred.scoring_evidence is None

    receipt = close_event(
        db,
        event_id=receipt.event_id,
        body=CloseAssessmentEventIn(
            expected_event_revision=receipt.revision,
            idempotency_key="close-terminal-0001",
            report_status="observation_recorded",
            fatigue_observed=True,
            note="  completed with observation  ",
        ),
        actor_id=ASSESSOR,
        actor_role="researcher",
        now=datetime(2026, 7, 19, 2, 5),
    )
    db.commit()
    assert receipt.status == "closed"
    assert receipt.closeout.switch_allowed is True
    assert receipt.closeout.note == "completed with observation"
    assert receipt.cancellation is None
    event = db.get(AssessmentEvent, receipt.event_id)
    assert event.active_protocol_slot_key is None
    assert len(db.exec(select(AssessmentEventCloseout)).all()) == 1

    # A closed/deferred timepoint can be scheduled again under a new event.
    rescheduled = _create(
        db, key="assessment-reschedule-0001", timepoint="pretest")
    assert rescheduled.event_id != receipt.event_id

    closeout = db.exec(select(AssessmentEventCloseout).where(
        AssessmentEventCloseout.event_id == receipt.event_id)).one()
    original_terminal_digest = closeout.instance_terminal_digest
    db.exec(text(
        "UPDATE assessmenteventcloseout SET instance_terminal_digest = :digest "
        "WHERE event_id = :event_id"
    ), params={
        "digest": "sha256:" + "0" * 64,
        "event_id": receipt.event_id,
    })
    db.commit()
    db.expire_all()
    with pytest.raises(AssessmentServiceError) as caught:
        event_receipt(db, receipt.event_id)
    assert caught.value.code == "assessment_state_invalid"

    db.exec(text(
        "UPDATE assessmenteventcloseout SET instance_terminal_digest = :digest "
        "WHERE event_id = :event_id"
    ), params={
        "digest": original_terminal_digest,
        "event_id": receipt.event_id,
    })
    db.exec(text(
        "UPDATE assessmentcommand SET actor_id = 'FORGED-ADMIN' "
        "WHERE event_id = :event_id AND command_type = 'defer'"
    ), params={"event_id": receipt.event_id})
    db.commit()
    db.expire_all()
    with pytest.raises(AssessmentServiceError) as caught:
        event_receipt(db, receipt.event_id)
    assert caught.value.code == "assessment_state_invalid"


def test_due_cancellation_is_audited_terminal_and_releases_active_slot(
    db, synthetic_bundle,
):
    _add_simulation_patient(db)
    receipt = _create(db)
    with pytest.raises(AssessmentServiceError) as caught:
        _create(db, key="assessment-duplicate-slot-0001")
    assert caught.value.code == "assessment_active_slot_conflict"
    db.rollback()

    body = CancelAssessmentEventIn(
        expected_event_revision=receipt.revision,
        idempotency_key="assessment-cancel-0001",
        reason_code="protocol_correction",
    )
    cancelled = cancel_event(
        db, event_id=receipt.event_id, body=body,
        actor_id=ASSESSOR, actor_role="researcher",
        now=datetime(2026, 7, 19, 3, 0))
    db.commit()
    assert cancelled.status == "cancelled"
    assert cancelled.cancellation.reason_code == "protocol_correction"
    assert cancelled.cancellation.switch_allowed is True
    assert cancelled.closeout is None
    assert all(row.status == "due" for row in cancelled.instances)
    event = db.get(AssessmentEvent, cancelled.event_id)
    assert event.active_protocol_slot_key is None
    command = db.exec(select(AssessmentCommand).where(
        AssessmentCommand.command_type == "cancel")).one()
    assert command.reason_code == "protocol_correction"

    replay = cancel_event(
        db, event_id=receipt.event_id, body=body,
        actor_id=ASSESSOR, actor_role="researcher")
    assert replay.status == "cancelled"
    assert len(db.exec(select(AssessmentCommand).where(
        AssessmentCommand.command_type == "cancel")).all()) == 1
    assert today_events(
        db, as_of_date=TODAY, assigned_assessor_id=ASSESSOR).events == []

    event.cancellation_reason_code = None
    with db.no_autoflush:
        with pytest.raises(AssessmentServiceError) as caught:
            event_receipt(db, cancelled.event_id)
    assert caught.value.code == "assessment_state_invalid"
    db.rollback()
    assert _create(db, key="assessment-after-cancel-0001").status == "due"

    db.exec(text(
        "UPDATE assessmentcommand SET actor_id = 'FORGED-ASSESSOR' "
        "WHERE event_id = :event_id AND command_type = 'cancel'"
    ), params={"event_id": cancelled.event_id})
    db.commit()
    db.expire_all()
    with pytest.raises(AssessmentServiceError) as caught:
        event_receipt(db, cancelled.event_id)
    assert caught.value.code == "assessment_state_invalid"


def test_withdrawal_seals_service_replay_and_removes_active_event_from_queue(
    db, synthetic_bundle,
):
    patient = _add_simulation_patient(db)
    due = _create(db)
    body = StartAssessmentEventIn(
        expected_event_revision=due.revision,
        idempotency_key="assessment-sealed-start-0001",
    )
    started = start_event(
        db,
        event_id=due.event_id,
        body=body,
        actor_id=ASSESSOR,
        actor_role="researcher",
        now=datetime(2026, 7, 19, 3, 5),
    )
    db.commit()
    assert started.status == "in_progress"

    patient.withdrawal_status = "withdrawn"
    db.add(patient)
    db.commit()
    assert today_events(
        db, as_of_date=TODAY + timedelta(days=1),
        assigned_assessor_id=ASSESSOR,
    ).events == []

    with pytest.raises(AssessmentServiceError) as caught:
        start_event(
            db,
            event_id=due.event_id,
            body=body,
            actor_id=ASSESSOR,
            actor_role="researcher",
        )
    assert caught.value.code == "assessment_patient_sealed"


def test_contract_rejects_client_score_invalid_closeout_and_oversized_response():
    with pytest.raises(ValidationError):
        CompleteAssessmentInstanceIn.model_validate({
            "expected_event_revision": 1,
            "expected_instance_revision": 1,
            "idempotency_key": "client-score-0001",
            "score": 99,
        })
    with pytest.raises(ValidationError):
        CloseAssessmentEventIn(
            expected_event_revision=1,
            idempotency_key="invalid-closeout-0001",
            report_status="no_additional_observation",
            fatigue_observed=True,
        )
    with pytest.raises(ValidationError, match="64 KiB"):
        AssessmentResponseValue(value="\u8001" * (64 * 1024))
    # Artifact-only evidence is structurally valid; each registered definition
    # decides whether that combination is meaningful for its own instrument.
    artifact_only = AssessmentResponseValue(
        authorized_artifact_digest="sha256:" + "b" * 64)
    assert "value" not in artifact_only.model_fields_set


def test_response_set_digest_binds_authorized_artifact(db, synthetic_bundle):
    _add_simulation_patient(db)
    _add_simulation_patient(db, "SIM-ARTIFACT-2")
    first_event = _start(db, _create(db, timepoint="pretest"))
    first_instance = first_event.instances[0]
    first_event = _respond_all(
        db, first_event, first_instance.instance_id,
        artifact_digest="sha256:" + "1" * 64,
        key_prefix="artifact-one",
    )
    first_event = _complete(
        db, first_event, first_instance.instance_id, "complete-artifact-one")
    first_digest = next(
        row for row in first_event.instances
        if row.instance_id == first_instance.instance_id
    ).scoring_evidence.item_response_set_digest

    second_event = _start(db, _create(
        db, patient_id="SIM-ARTIFACT-2", timepoint="posttest",
        key="assessment-artifact-two-create"),
        key="assessment-artifact-two-start",
    )
    second_instance = next(
        row for row in second_event.instances
        if row.category_key == first_instance.category_key)
    second_event = _respond_all(
        db, second_event, second_instance.instance_id,
        artifact_digest="sha256:" + "2" * 64,
        key_prefix="artifact-two",
    )
    second_event = _complete(
        db, second_event, second_instance.instance_id, "complete-artifact-two")
    second_digest = next(
        row for row in second_event.instances
        if row.instance_id == second_instance.instance_id
    ).scoring_evidence.item_response_set_digest
    assert first_digest != second_digest


def test_today_projection_and_audit_tables_contain_no_raw_response_columns(
    db, synthetic_bundle,
):
    _add_simulation_patient(db)
    receipt = _create(db)
    projection = today_events(db, as_of_date=TODAY, assigned_assessor_id=ASSESSOR)
    assert [row.event_id for row in projection.events] == [receipt.event_id]
    assert event_receipt(db, receipt.event_id) == receipt

    command_columns = {column["name"] for column in inspect(db.get_bind()).get_columns(
        "assessmentcommand")}
    assert "response_json" not in command_columns
    assert "result_json" not in command_columns
    assert "audio" not in " ".join(command_columns)
    assert db.exec(select(AssessmentDeferralApproval)).all() == []


def test_today_projection_keeps_overdue_and_cross_midnight_active_work_visible(
    db, synthetic_bundle,
):
    _add_simulation_patient(db)
    overdue = _create(db)
    stored = db.get(AssessmentEvent, overdue.event_id)
    assert stored is not None
    stored.scheduled_date = TODAY - timedelta(days=2)
    db.add(stored)
    db.commit()

    overdue_projection = today_events(
        db, as_of_date=TODAY, assigned_assessor_id=ASSESSOR)
    assert [row.event_id for row in overdue_projection.events] == [overdue.event_id]

    active = _start(db, event_receipt(db, overdue.event_id))
    next_day_projection = today_events(
        db, as_of_date=TODAY + timedelta(days=1),
        assigned_assessor_id=ASSESSOR,
    )
    assert [row.event_id for row in next_day_projection.events] == [active.event_id]

    future_due = _create(
        db,
        timepoint="posttest",
        key="assessment-future-queue-0001",
    )
    future_stored = db.get(AssessmentEvent, future_due.event_id)
    assert future_stored is not None
    future_stored.scheduled_date = TODAY + timedelta(days=3)
    db.add(future_stored)
    db.commit()
    projection = today_events(
        db, as_of_date=TODAY + timedelta(days=1),
        assigned_assessor_id=ASSESSOR,
    )
    assert [row.event_id for row in projection.events] == [active.event_id]
