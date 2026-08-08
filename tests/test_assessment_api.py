"""HTTP authorization, readiness and end-to-end formal-assessment coverage."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app import auth, db, scale_protocol
from app.assessment_definitions import install_synthetic_bundle_for_testing
from app.main import app
from app.models import (
    AssessmentEvent,
    AssessmentItemResponse,
    AuditLog,
    Patient,
    ResearchUser,
)


READY = {
    "schema_version": "scale-protocol-readiness.v4",
    "status": "ready_for_research",
    "definition_ready": True,
    "definition_artifact_enforcement_ready": True,
    "definition_artifacts_ready": True,
    "formal_result_contract_ready": True,
    "workflow_policy_ready": True,
    "workflow_contract_ready": True,
    "workflow_policy_enforcement_ready": True,
    "workflow_ready": True,
    "ready_for_research": True,
    "instance_creation_enabled": True,
    "automatic_scoring_enabled": True,
    "training_metrics_are_formal_scale_results": False,
    "blocking_issues": [],
}


@dataclass
class AssessmentClients:
    researcher: TestClient
    researcher_b: TestClient
    admin: TestClient
    steward: TestClient
    engine: object


def _login(username: str) -> TestClient:
    client = TestClient(app)
    response = client.post("/auth/login", json={
        "username": username,
        "password": "password1",
    })
    assert response.status_code == 200, response.text
    csrf = client.cookies.get(auth.CSRF_COOKIE_NAME)
    assert csrf
    client.headers["X-CSRF-Token"] = csrf
    return client


@pytest.fixture
def assessment_clients(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'assessment-api.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("ALLOW_SIMULATION_DATA", "1")
    monkeypatch.setattr(
        scale_protocol, "scale_protocol_readiness", lambda: dict(READY))
    SQLModel.metadata.create_all(engine)
    password_hash = auth.hash_password("password1")
    with Session(engine) as session:
        for username, display_id, role in (
            ("assessment-a", "ASSESSOR-A", "researcher"),
            ("assessment-b", "ASSESSOR-B", "researcher"),
            ("assessment-admin", "ASSESSMENT-ADMIN", "admin"),
            ("assessment-steward", "ASSESSMENT-STEWARD", "data_steward"),
        ):
            session.add(ResearchUser(
                username=username,
                display_id=display_id,
                password_hash=password_hash,
                role=role,
                created_at=datetime.now(),
            ))
        session.add(Patient(
            patient_id="SIM-ASSESSMENT-API",
            is_simulation_subject=True,
            consent_status="已同意",
            recording_allowed=True,
            secondary_use_allowed=True,
        ))
        session.commit()

    with install_synthetic_bundle_for_testing():
        clients = AssessmentClients(
            researcher=_login("assessment-a"),
            researcher_b=_login("assessment-b"),
            admin=_login("assessment-admin"),
            steward=_login("assessment-steward"),
            engine=engine,
        )
        try:
            yield clients
        finally:
            clients.researcher.close()
            clients.researcher_b.close()
            clients.admin.close()
            clients.steward.close()


def _create(client: TestClient, *, timepoint: str = "pretest", key: str = "api-create-0001"):
    response = client.post(
        "/patients/SIM-ASSESSMENT-API/assessment-events",
        json={
            "timepoint": timepoint,
            "scheduled_date": date.today().isoformat(),
            "idempotency_key": key,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _instance(receipt: dict, category: str) -> dict:
    return next(row for row in receipt["instances"] if row["category_key"] == category)


def _assert_utc_z(*values: str) -> None:
    for value in values:
        assert value.endswith("Z"), value
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
        assert parsed.utcoffset() == timedelta(0)


def _submit_two(client: TestClient, receipt: dict, category: str) -> dict:
    instance = _instance(receipt, category)
    prefix = "naming" if category == "untrained_standardized_naming" else "functional"
    for index in (1, 2):
        response = client.put(
            f"/assessment-instances/{instance['instance_id']}/responses/{prefix}_{index:02d}",
            json={
                "response": {"value": index},
                "expected_event_revision": receipt["revision"],
                "expected_instance_revision": instance["revision"],
                "expected_item_revision": 0,
                "idempotency_key": f"api-response-{category}-{index:02d}",
            },
        )
        assert response.status_code == 200, response.text
        receipt = response.json()
        instance = _instance(receipt, category)
    return receipt


def test_http_serializes_sqlite_naive_assessment_times_as_utc_z(
        assessment_clients):
    clients = assessment_clients
    receipt = _create(clients.researcher, key="api-utc-contract-create-0001")
    _assert_utc_z(receipt["created_at"], receipt["updated_at"])
    for instance in receipt["instances"]:
        _assert_utc_z(instance["created_at"], instance["updated_at"])
        assert instance["completed_at"] is None

    # SQLite keeps the deployed historical convention (naive means UTC); the
    # response model, not a risky data migration, supplies the explicit zone.
    with Session(clients.engine) as session:
        stored = session.get(AssessmentEvent, receipt["event_id"])
        assert stored is not None
        assert stored.created_at.tzinfo is None
        assert stored.updated_at.tzinfo is None

    recovered = clients.researcher.get(
        f"/assessment-events/{receipt['event_id']}")
    assert recovered.status_code == 200, recovered.text
    recovered_receipt = recovered.json()
    _assert_utc_z(
        recovered_receipt["created_at"], recovered_receipt["updated_at"])
    for instance in recovered_receipt["instances"]:
        _assert_utc_z(instance["created_at"], instance["updated_at"])


def test_http_flow_is_owner_scoped_server_scored_and_atomically_closed(
        assessment_clients):
    clients = assessment_clients
    receipt = _create(clients.researcher)
    event_id = receipt["event_id"]
    assert receipt["assigned_assessor_id"] == "ASSESSOR-A"
    assert receipt["formal_outcome_eligible"] is False

    # Existence and state are concealed from another researcher. Data stewards
    # cannot read an active result container; admins can supervise it.
    assert clients.researcher_b.get(
        f"/assessment-events/{event_id}").status_code == 404
    assert clients.researcher_b.get(
        "/patients/SIM-ASSESSMENT-API/assessment-events").json() == []
    steward_active = clients.steward.get(f"/assessment-events/{event_id}")
    assert steward_active.status_code == 403
    assert steward_active.json()["detail"]["code"] == (
        "assessment_terminal_read_required")
    assert clients.admin.get(f"/assessment-events/{event_id}").status_code == 200

    today = clients.researcher.get("/assessment-events/today")
    assert today.status_code == 200
    assert [row["event_id"] for row in today.json()["events"]] == [event_id]
    assert today.headers["cache-control"] == "private, no-store"

    start_body = {
        "expected_event_revision": receipt["revision"],
        "idempotency_key": "api-start-0001",
    }
    started = clients.researcher.post(
        f"/assessment-events/{event_id}/start",
        json=start_body,
    )
    assert started.status_code == 200, started.text
    receipt = started.json()
    assert receipt["status"] == "in_progress"
    replayed_start = clients.researcher.post(
        f"/assessment-events/{event_id}/start",
        json=start_body,
    )
    assert replayed_start.status_code == 200, replayed_start.text
    assert replayed_start.json() == receipt

    naming = _instance(receipt, "untrained_standardized_naming")
    unauthorized_artifact = clients.researcher.put(
        f"/assessment-instances/{naming['instance_id']}/responses/naming_01",
        json={
            "response": {
                "value": 1,
                "authorized_artifact_digest": "sha256:" + "a" * 64,
            },
            "expected_event_revision": receipt["revision"],
            "expected_instance_revision": naming["revision"],
            "expected_item_revision": 0,
            "idempotency_key": "api-artifact-untrusted-0001",
        },
    )
    assert unauthorized_artifact.status_code == 409
    assert unauthorized_artifact.json()["detail"]["code"] == (
        "assessment_artifact_not_authorized")

    receipt = _submit_two(
        clients.researcher, receipt, "untrained_standardized_naming")
    naming = _instance(receipt, "untrained_standardized_naming")
    completed = clients.researcher.post(
        f"/assessment-instances/{naming['instance_id']}/complete",
        json={
            "expected_event_revision": receipt["revision"],
            "expected_instance_revision": naming["revision"],
            "idempotency_key": "api-complete-naming-0001",
        },
    )
    assert completed.status_code == 200, completed.text
    receipt = completed.json()
    evidence = _instance(
        receipt, "untrained_standardized_naming")["scoring_evidence"]
    assert evidence["score"] == 3
    assert evidence["result"] == {"total_score": 3.0}
    assert evidence["formal_outcome_eligible"] is False
    _assert_utc_z(evidence["scored_at"])
    completed_instance = _instance(
        receipt, "untrained_standardized_naming")
    _assert_utc_z(completed_instance["completed_at"], completed_instance["updated_at"])

    functional = _instance(receipt, "functional_communication")
    deferred = clients.admin.post(
        f"/assessment-instances/{functional['instance_id']}/approve-defer",
        json={
            "expected_event_revision": receipt["revision"],
            "expected_instance_revision": functional["revision"],
            "idempotency_key": "api-defer-functional-0001",
            "reason_code": "participant_unavailable",
            "deferred_until": (date.today() + timedelta(days=2)).isoformat(),
        },
    )
    assert deferred.status_code == 200, deferred.text
    receipt = deferred.json()
    assert receipt["status"] == "awaiting_closeout"
    assert _instance(receipt, "functional_communication")["deferral"][
        "approved_role"] == "admin"
    _assert_utc_z(_instance(
        receipt, "functional_communication")["deferral"]["approved_at"])

    closed = clients.researcher.post(
        f"/assessment-events/{event_id}/close",
        json={
            "expected_event_revision": receipt["revision"],
            "idempotency_key": "api-close-0001",
            "report_status": "observation_recorded",
            "fatigue_observed": True,
            "note": "观察到疲劳，已按协议停止",
        },
    )
    assert closed.status_code == 200, closed.text
    receipt = closed.json()
    assert receipt["status"] == "closed"
    assert receipt["closeout"]["switch_allowed"] is True
    _assert_utc_z(receipt["closeout"]["closed_at"], receipt["updated_at"])
    assert clients.steward.get(f"/assessment-events/{event_id}").status_code == 200
    assert clients.researcher.get("/assessment-events/today").json()["events"] == []

    with Session(clients.engine) as session:
        assert session.exec(select(AssessmentItemResponse)).all()
        summaries = [row.summary for row in session.exec(select(AuditLog))]
        assert any("assessment" in row.action for row in session.exec(select(AuditLog)))
        assert all("total_score" not in summary for summary in summaries)
        assert all("观察到疲劳" not in summary for summary in summaries)


def test_cancel_releases_slot_and_readiness_blocks_before_any_write(
        assessment_clients, monkeypatch):
    clients = assessment_clients
    receipt = _create(
        clients.researcher,
        timepoint="posttest",
        key="api-cancel-create-0001",
    )
    cancelled = clients.researcher.post(
        f"/assessment-events/{receipt['event_id']}/cancel",
        json={
            "expected_event_revision": receipt["revision"],
            "idempotency_key": "api-cancel-0001",
            "reason_code": "schedule_changed",
        },
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["cancellation"]["switch_allowed"] is True
    _assert_utc_z(cancelled.json()["cancellation"]["cancelled_at"])

    blocked_status = {
        **READY,
        "status": "awaiting_workflow_policy",
        "workflow_policy_ready": False,
        "workflow_ready": False,
        "ready_for_research": False,
        "instance_creation_enabled": False,
        "automatic_scoring_enabled": False,
        "blocking_issues": [{
            "code": "workflow_policy.closeout_rule_digest.not_ready",
            "category_key": "workflow_policy",
            "field": "closeout_rule_digest",
            "message": "test blocker",
        }],
    }
    monkeypatch.setattr(
        scale_protocol, "scale_protocol_readiness", lambda: blocked_status)
    blocked = clients.researcher.post(
        "/patients/SIM-ASSESSMENT-API/assessment-events",
        json={
            "timepoint": "followup",
            "scheduled_date": date.today().isoformat(),
            "idempotency_key": "api-readiness-blocked-0001",
        },
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == {
        "code": "formal_assessment_not_ready",
        "message": (
            "正式两表的 PI 定义、授权运行时包、工作流政策或平台合同尚未全部就绪；"
            "当前保持只读，禁止建立或推进正式评估"
        ),
        "readiness_status": "awaiting_workflow_policy",
        "blocking_codes": ["workflow_policy.closeout_rule_digest.not_ready"],
    }
    with Session(clients.engine) as session:
        events = session.exec(select(AssessmentEvent).where(
            AssessmentEvent.timepoint == "followup")).all()
        assert events == []


def test_consent_refusal_seals_queue_reads_and_idempotent_mutation_replay(
        assessment_clients):
    clients = assessment_clients
    receipt = _create(
        clients.researcher,
        timepoint="followup",
        key="api-sealed-create-0001",
    )
    start_body = {
        "expected_event_revision": receipt["revision"],
        "idempotency_key": "api-sealed-start-0001",
    }
    started = clients.researcher.post(
        f"/assessment-events/{receipt['event_id']}/start",
        json=start_body,
    )
    assert started.status_code == 200, started.text

    with Session(clients.engine) as session:
        patient = session.get(Patient, "SIM-ASSESSMENT-API")
        assert patient is not None
        patient.consent_status = "denied"
        session.add(patient)
        session.commit()

    queue = clients.researcher.get("/assessment-events/today")
    assert queue.status_code == 200, queue.text
    assert queue.json()["events"] == []

    read = clients.researcher.get(
        f"/assessment-events/{receipt['event_id']}")
    assert read.status_code == 409
    assert read.json()["detail"]["code"] == (
        "subject_withdrawn_content_unavailable")

    replay = clients.researcher.post(
        f"/assessment-events/{receipt['event_id']}/start",
        json=start_body,
    )
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == (
        "subject_withdrawn_content_unavailable")
    assert "instances" not in replay.text


def test_recording_authorization_issue_consume_and_reuse_fences(
        assessment_clients):
    """逐题录音授权收据(收据 150 S3):签发→消费恰好一次,五元组绑定。"""
    clients = assessment_clients
    receipt = _create(clients.researcher, key="api-rec-auth-create-0001")
    event_id = receipt["event_id"]
    started = clients.researcher.post(
        f"/assessment-events/{event_id}/start",
        json={
            "expected_event_revision": receipt["revision"],
            "idempotency_key": "api-rec-auth-start-0001",
        },
    )
    assert started.status_code == 200, started.text
    receipt = started.json()
    naming = _instance(receipt, "untrained_standardized_naming")
    instance_id = naming["instance_id"]

    issued = clients.researcher.post(
        f"/assessment-instances/{instance_id}/recording-authorizations",
        json={"item_key": "naming_01"},
    )
    assert issued.status_code == 200, issued.text
    grant = issued.json()
    assert grant["item_key"] == "naming_01"
    assert grant["item_revision"] == 1
    digest = grant["authorized_artifact_digest"]
    assert digest.startswith("sha256:") and len(digest) == 71

    # 冻结定义外的条目拒签。
    unknown = clients.researcher.post(
        f"/assessment-instances/{instance_id}/recording-authorizations",
        json={"item_key": "naming_99"},
    )
    assert unknown.status_code == 409
    assert unknown.json()["detail"]["code"] == "assessment_item_unknown"

    # 消费:携带收据的响应落库。
    response = clients.researcher.put(
        f"/assessment-instances/{instance_id}/responses/naming_01",
        json={
            "response": {"value": 1, "authorized_artifact_digest": digest},
            "expected_event_revision": receipt["revision"],
            "expected_instance_revision": naming["revision"],
            "expected_item_revision": 0,
            "idempotency_key": "api-rec-auth-response-0001",
        },
    )
    assert response.status_code == 200, response.text
    receipt = response.json()
    naming = _instance(receipt, "untrained_standardized_naming")

    with Session(clients.engine) as session:
        from app.models import AssessmentRecordingAuthorization

        row = session.exec(select(AssessmentRecordingAuthorization).where(
            AssessmentRecordingAuthorization.authorization_digest == digest,
        )).one()
        assert row.consumed_at is not None
        assert row.item_revision == 1

    # 已消费收据在新修订上复用 → 拒绝(authorizer 消费面)。
    reuse = clients.researcher.put(
        f"/assessment-instances/{instance_id}/responses/naming_01",
        json={
            "response": {"value": 2, "authorized_artifact_digest": digest},
            "expected_event_revision": receipt["revision"],
            "expected_instance_revision": naming["revision"],
            "expected_item_revision": 1,
            "idempotency_key": "api-rec-auth-response-0002",
        },
    )
    assert reuse.status_code == 409
    assert reuse.json()["detail"]["code"] == "assessment_artifact_not_authorized"


def test_recording_authorization_binds_item_and_revision_context(
        assessment_clients):
    """收据钉发签时的 item/revision:错题或修订前进后一律作废,须重签。"""
    clients = assessment_clients
    receipt = _create(clients.researcher, key="api-rec-auth2-create-0001")
    event_id = receipt["event_id"]
    started = clients.researcher.post(
        f"/assessment-events/{event_id}/start",
        json={
            "expected_event_revision": receipt["revision"],
            "idempotency_key": "api-rec-auth2-start-0001",
        },
    )
    assert started.status_code == 200, started.text
    receipt = started.json()
    naming = _instance(receipt, "untrained_standardized_naming")
    instance_id = naming["instance_id"]

    issued = clients.researcher.post(
        f"/assessment-instances/{instance_id}/recording-authorizations",
        json={"item_key": "naming_01"},
    )
    assert issued.status_code == 200, issued.text
    digest = issued.json()["authorized_artifact_digest"]

    # 用在另一条目 → 拒绝。
    wrong_item = clients.researcher.put(
        f"/assessment-instances/{instance_id}/responses/naming_02",
        json={
            "response": {"value": 1, "authorized_artifact_digest": digest},
            "expected_event_revision": receipt["revision"],
            "expected_instance_revision": naming["revision"],
            "expected_item_revision": 0,
            "idempotency_key": "api-rec-auth2-response-0001",
        },
    )
    assert wrong_item.status_code == 409
    assert wrong_item.json()["detail"]["code"] == (
        "assessment_artifact_not_authorized")

    # 无收据的普通响应把修订推进到 1 → 发签时绑定的 revision=1 已被占用,作废。
    plain = clients.researcher.put(
        f"/assessment-instances/{instance_id}/responses/naming_01",
        json={
            "response": {"value": 0},
            "expected_event_revision": receipt["revision"],
            "expected_instance_revision": naming["revision"],
            "expected_item_revision": 0,
            "idempotency_key": "api-rec-auth2-response-0002",
        },
    )
    assert plain.status_code == 200, plain.text
    receipt = plain.json()
    naming = _instance(receipt, "untrained_standardized_naming")
    stale = clients.researcher.put(
        f"/assessment-instances/{instance_id}/responses/naming_01",
        json={
            "response": {"value": 1, "authorized_artifact_digest": digest},
            "expected_event_revision": receipt["revision"],
            "expected_instance_revision": naming["revision"],
            "expected_item_revision": 1,
            "idempotency_key": "api-rec-auth2-response-0003",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "assessment_artifact_not_authorized"

    # 消费未发生:收据仍未消费,行不可删不可改(只追加账本)。
    with Session(clients.engine) as session:
        from app.models import AssessmentRecordingAuthorization

        row = session.exec(select(AssessmentRecordingAuthorization).where(
            AssessmentRecordingAuthorization.authorization_digest == digest,
        )).one()
        assert row.consumed_at is None
        row.item_key = "naming_03"
        with pytest.raises(RuntimeError, match="禁止更新"):
            session.commit()
        session.rollback()
