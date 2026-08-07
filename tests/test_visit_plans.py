"""VisitPlan acceptance contract: prepare, approve, queue, and atomic start.

These tests intentionally exercise only account-facing HTTP routes.  Bedside
devices never receive draft scheduling metadata, and callers never choose the
opaque plan/session identifiers or research-classification facts.
"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
import hashlib
import json
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app import (
    auth, autopilot_plan_profiles, content, db, repeat_intent,
    visit_plan_service,
)
from app.main import app
from app.models import (
    AssessmentEvent,
    AttemptCaptureProcessing,
    AttemptEvent,
    AudioAssetRow,
    AuditLog,
    AutopilotControlEvent,
    AutopilotRepeatRequest,
    InteractionEvent,
    LiveState,
    Patient,
    PatientDeviceCapability,
    ResearchUser,
    RuntimeCommand,
    RuntimeCommandAck,
    Session as TrainSession,
    SessionAutopilotState,
    SessionCloseoutReport,
    SessionRuntimeState,
    VisitPlan,
    VisitPlanCommand,
)


BANK = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
PROTOCOL = content.load_autopilot_protocol(
    content.CONTENT_DIR / "autopilot_protocol_v1.json")
REPEAT_PROTOCOL = repeat_intent.active_protocol()
# 服务端的"今天"按 RESEARCH_TIMEZONE(默认 Asia/Shanghai)算,不是机器本地时区。
# date.today() 会让这套用例在 UTC 机器上每天有八小时整片翻红。
TODAY = visit_plan_service._research_today()
RECEIPT_KEYS = {
    "plan_id",
    "patient_id",
    "scheduled_date",
    "scheduled_time",
    "queue_order",
    "session_sitting_no",
    "week_no",
    "phase_type",
    "event_line",
    "item_bank_version_id",
    "autopilot_profile_version_id",
    "is_simulation",
    "data_classification",
    "status",
    "revision",
    "created_by",
    "created_at",
    "approved_by",
    "approved_at",
    "started_by",
    "started_at",
    "cancelled_by",
    "cancelled_at",
    "session_id",
}
STARTED_SESSION_KEYS = {
    "session_id",
    "patient_id",
    "visit_plan_id",
    "session_sitting_no",
    "training_date",
    "week_no",
    "phase_type",
    "event_line",
    "trainer_id",
    "item_bank_version_id",
    "item_bank_definition_digest",
    "autopilot_protocol_version_id",
    "autopilot_protocol_definition_digest",
    "repeat_protocol_version_id",
    "repeat_protocol_definition_digest",
    "autopilot_profile_version_id",
    "autopilot_profile_definition_digest",
    "is_simulation",
    "data_classification",
}


@dataclass
class VisitClients:
    researcher: TestClient
    researcher_b: TestClient
    admin: TestClient
    steward: TestClient
    anonymous: TestClient
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
def visit_clients(monkeypatch, tmp_path) -> VisitClients:
    # File-backed SQLite gives concurrent HTTP requests independent connections;
    # StaticPool shares one DB-API cursor and can fabricate driver-level races.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'visit-plans.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setenv("ALLOW_SIMULATION_DATA", "1")
    SQLModel.metadata.create_all(engine)
    password_hash = auth.hash_password("password1")
    with Session(engine) as session:
        for username, role in (
            ("visit-researcher", "researcher"),
            ("visit-researcher-b", "researcher"),
            ("visit-admin", "admin"),
            ("visit-steward", "data_steward"),
        ):
            session.add(ResearchUser(
                username=username,
                display_id=f"ACTOR-{username}",
                password_hash=password_hash,
                role=role,
                created_at=datetime.now(),
            ))
        for index in range(1, 13):
            session.add(Patient(
                patient_id=f"P-VISIT-{index:02d}",
                is_simulation_subject=True,
                consent_status="已同意",
                consent_type="本人同意",
                mandarin_eligible=True,
                recording_allowed=True,
                secondary_use_allowed=True,
            ))
        session.commit()

    monkeypatch.setenv("REQUIRE_AUTH", "1")
    clients = VisitClients(
        researcher=_login("visit-researcher"),
        researcher_b=_login("visit-researcher-b"),
        admin=_login("visit-admin"),
        steward=_login("visit-steward"),
        anonymous=TestClient(app),
        engine=engine,
    )
    try:
        yield clients
    finally:
        clients.researcher.close()
        clients.researcher_b.close()
        clients.admin.close()
        clients.steward.close()
        clients.anonymous.close()


def _plan_body(
        patient_id: str,
        key: str,
        *,
        scheduled_date: date = TODAY,
        scheduled_time: str | None = "09:30:00",
        queue_order: int | None = 1,
        session_sitting_no: int = 1,
        **overrides,
) -> dict:
    body = {
        "patient_id": patient_id,
        "scheduled_date": scheduled_date.isoformat(),
        "scheduled_time": scheduled_time,
        "queue_order": queue_order,
        "session_sitting_no": session_sitting_no,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "idempotency_key": key,
    }
    body.update(overrides)
    return body


def _create(
        client: TestClient,
        patient_id: str,
        key: str,
        **overrides,
) -> dict:
    response = client.post(
        "/visit-plans",
        json=_plan_body(patient_id, key, **overrides),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _command(
        client: TestClient,
        plan_id: str,
        action: str,
        *,
        key: str,
        expected_revision: int,
        reason_code: str | None = None,
):
    body = {
        "idempotency_key": key,
        "expected_revision": expected_revision,
    }
    if action == "cancel":
        body["reason_code"] = reason_code
    return client.post(f"/visit-plans/{plan_id}/{action}", json=body)


# --------------------------------------------------------------------------
# D1A autopilot demo profile: draft-only binding contract
#
# This release freezes the 20-item simulation demo at the draft stage.  Nothing
# here may approve, start, run or complete a demo scope; the canonical plan
# must keep behaving exactly as it did before the profile columns existed.
# --------------------------------------------------------------------------


DEMO_VERSION = autopilot_plan_profiles.WEEK2_SINGLE20_DEMO_VERSION
DEMO_DIGEST = autopilot_plan_profiles.WEEK2_SINGLE20_DEMO_DIGEST
# The exact pre-D1A create payload field set.  Byte compatibility is pinned by
# rebuilding the legacy hash from this literal list, not by re-running today's
# production helper against itself.
LEGACY_CREATE_FIELDS = (
    "idempotency_key", "patient_id", "scheduled_date", "scheduled_time",
    "queue_order", "session_sitting_no", "week_no", "phase_type", "event_line",
)


def _frozen_request_hash(command_type: str, payload: dict) -> str:
    """The pre-D1A canonical encoding, restated here instead of imported.

    Written out independently so a defect in the production hash helper cannot
    be hidden by comparing that helper against itself.
    """
    def encoded(value):
        if isinstance(value, (date, datetime, time)):
            return value.isoformat()
        return getattr(value, "value", value)

    canonical = json.dumps(
        {
            "command_type": command_type,
            **{key: encoded(value) for key, value in payload.items()},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Immutable pre-D1A evidence.  These are literal receipts, not values derived
# from today's globals: a fixture that recomputes both sides from live content
# would co-move with a regression and stay green.
LEGACY_DATE = date(2026, 7, 31)
LEGACY_TIME = time(9, 30)
LEGACY_BANK_VERSION = "wk2-v1-20260707"
LEGACY_BANK_DIGEST = (
    "80ca521df07de060b8f31f0c768c776ac4d2a62ef9114513aaae3c0cd5a016c4")
LEGACY_PROTOCOL_VERSION = "autopilot-v1-20260729"
LEGACY_PROTOCOL_DIGEST = (
    "51fe62990cc0bc6934b4fbe7c8d902369c0b9fd7e092136572da3fa375b989d9")
LEGACY_REPEAT_VERSION = "repeat-intent-v1-20260730-proposal"
LEGACY_REPEAT_DIGEST = (
    "51e8ce30d6273df52fc25011ed00ebc5fba15b30c9ed98b4ccc146b72e05484f")
LEGACY_STARTED_CREATE_HASH = (
    "ab06a584340613a60717ba98f9721e6ca56e0f4fa6000460e4fa11f229fee8ec")
LEGACY_STARTED_APPROVE_HASH = (
    "8f088e1579fa691b1c55665875a4e18e61dc5a3172b3ac370ee30f3f83830a4f")
LEGACY_STARTED_START_HASH = (
    "7230d030a02c379fceb1d2ee42e78918bd296071e3c2328ec150b45a6f60eb12")
LEGACY_CANCELLED_CREATE_HASH = (
    "1e521e4062167559a7f76169d5d6d3aa9aad7892985f6af2246eeb6f841c210d")
LEGACY_CANCELLED_CANCEL_HASH = (
    "8d6571b2ff63fa9c978c64c0a249a28a9dc36105791a2c37aad7e93ffd81d7df")
LEGACY_STARTED_SLOT = (
    "51d3e0e6411f99b5a2f844286946627af80f85e74695fa92eb8bca649d147cf5")
LEGACY_CANCELLED_SLOT = (
    "07d7f2bd33b03c6e26aa07f72c0cbd339194bc95ab12196b7f70a2adb09ce28b")


def _legacy_create_hash(body: dict) -> str:
    """The frozen pre-D1A create payload.

    Nine of these keys are the client-supplied create fields; the remaining two
    are the server-owned repeat pair that already existed before D1A.  The
    canonical payload therefore has eleven keys, and the new profile key is
    absent entirely rather than present as NULL.
    """
    client_fields = {
        "idempotency_key": body["idempotency_key"],
        "patient_id": body["patient_id"],
        "scheduled_date": date.fromisoformat(body["scheduled_date"]),
        "scheduled_time": (
            time.fromisoformat(body["scheduled_time"])
            if body["scheduled_time"] is not None else None),
        "queue_order": body["queue_order"],
        "session_sitting_no": body["session_sitting_no"],
        "week_no": body["week_no"],
        "phase_type": body["phase_type"],
        "event_line": body["event_line"],
    }
    assert set(client_fields) == set(LEGACY_CREATE_FIELDS)
    payload = {
        **client_fields,
        "repeat_protocol_version_id": LEGACY_REPEAT_VERSION,
        "repeat_protocol_definition_digest": LEGACY_REPEAT_DIGEST,
    }
    assert len(payload) == 11
    assert "autopilot_profile_version_id" not in payload
    return _frozen_request_hash("create", payload)


def _legacy_mutation_hash(
        command_type: str, plan_id: str, key: str, expected_revision: int,
        *, reason_code: str | None = None) -> str:
    payload: dict = {
        "plan_id": plan_id,
        "idempotency_key": key,
        "expected_revision": expected_revision,
    }
    if reason_code is not None:
        payload["reason_code"] = reason_code
    if command_type in {"approve", "start"}:
        payload["repeat_protocol_version_id"] = LEGACY_REPEAT_VERSION
        payload["repeat_protocol_definition_digest"] = LEGACY_REPEAT_DIGEST
    assert "autopilot_profile_version_id" not in payload
    return _frozen_request_hash(command_type, payload)


def _plan_row(engine, plan_id: str) -> VisitPlan:
    with Session(engine) as session:
        row = session.get(VisitPlan, plan_id)
        assert row is not None
        session.expunge(row)
        return row


_PROFILE_WRITE_MODELS = (
    VisitPlan, VisitPlanCommand, TrainSession, SessionRuntimeState)


def _write_counts(engine) -> dict:
    """Every column of every row, so a row *update* is detectable too.

    Counting rows would false-green an insert paired with a delete, and would
    miss a refusal that still mutated an existing Plan or command in place.
    Columns are read from ``__table__.columns`` at call time so a field added
    later is covered without maintaining a list.
    """
    snapshot: dict[str, dict] = {}
    with Session(engine) as session:
        for model in _PROFILE_WRITE_MODELS:
            names = tuple(column.name for column in model.__table__.columns)
            rows = [tuple(getattr(row, name) for name in names)
                    for row in session.exec(select(model))]
            snapshot[model.__name__] = {
                "columns": names, "rows": sorted(rows, key=repr)}
    return snapshot


def _force_profile_pair(
        engine, plan_id: str, version: str | None, digest: str | None,
        *, status: str | None = None) -> None:
    """Seed an anomalous stored pair the product path can never produce.

    A half pair is exactly what the database CHECK forbids, so this models a
    corrupted restore by suspending the check for the one seeding write.  The
    service must still refuse it on its own.
    """
    half_pair = (version is None) != (digest is None)
    with Session(engine) as session:
        if half_pair:
            session.connection().exec_driver_sql(
                "PRAGMA ignore_check_constraints=ON")
        row = session.get(VisitPlan, plan_id)
        assert row is not None
        row.autopilot_profile_version_id = version
        row.autopilot_profile_definition_digest = digest
        if status is not None:
            row.status = status
        session.add(row)
        session.commit()


def _add_research_patient(engine, patient_id: str) -> None:
    with Session(engine) as session:
        session.add(Patient(
            patient_id=patient_id,
            is_simulation_subject=False,
            consent_status="已同意",
            consent_type="本人同意",
            mandarin_eligible=True,
            recording_allowed=True,
            secondary_use_allowed=True,
        ))
        session.commit()


def _detail_code(response) -> str:
    payload = response.json()
    assert isinstance(payload.get("detail"), dict), response.text
    return payload["detail"]["code"]


# ---- contract shape -------------------------------------------------------


def test_client_supplied_profile_digest_is_rejected(visit_clients):
    response = visit_clients.researcher.post("/visit-plans", json=_plan_body(
        "P-VISIT-01", "create-digest-forbid-01",
        autopilot_profile_definition_digest=DEMO_DIGEST))

    assert response.status_code == 422, response.text


def test_simulation_demo_create_exposes_version_but_never_digest(visit_clients):
    created = _create(
        visit_clients.researcher, "P-VISIT-01", "create-demo-draft-01",
        autopilot_profile_version_id=DEMO_VERSION)

    assert created["autopilot_profile_version_id"] == DEMO_VERSION
    assert "autopilot_profile_definition_digest" not in created
    assert set(created) == RECEIPT_KEYS
    assert created["status"] == "draft"
    row = _plan_row(visit_clients.engine, created["plan_id"])
    assert row.autopilot_profile_version_id == DEMO_VERSION
    assert row.autopilot_profile_definition_digest == DEMO_DIGEST


def test_canonical_create_stays_paired_null(visit_clients):
    created = _create(
        visit_clients.researcher, "P-VISIT-02", "create-canon-null-01")

    assert created["autopilot_profile_version_id"] is None
    row = _plan_row(visit_clients.engine, created["plan_id"])
    assert row.autopilot_profile_version_id is None
    assert row.autopilot_profile_definition_digest is None


# The unsupported-canonical-week journey is already owned by
# ``test_unsupported_protocol_may_be_drafted_but_cannot_be_approved``; the only
# thing D1A adds there is that the receipt stays paired-null, asserted below.


def test_unsupported_canonical_draft_is_still_paired_null(visit_clients):
    """A NULL selector must never reach the Week-2 demo context gate."""
    created = _create(
        visit_clients.researcher, "P-VISIT-03", "create-w1rel-0001",
        week_no=1, phase_type="关系建立", event_line="关系建立环节")

    assert created["autopilot_profile_version_id"] is None
    row = _plan_row(visit_clients.engine, created["plan_id"])
    assert row.autopilot_profile_version_id is None
    assert row.autopilot_profile_definition_digest is None


# ---- create-time refusal, always with zero writes -------------------------


def test_unknown_profile_version_is_refused_without_writes(visit_clients):
    before = _write_counts(visit_clients.engine)

    response = visit_clients.researcher.post("/visit-plans", json=_plan_body(
        "P-VISIT-04", "create-unknown-prof-01",
        autopilot_profile_version_id="no-such-demo-v9"))

    assert response.status_code == 422, response.text
    assert _detail_code(response) == "visit_plan_profile_unknown"
    assert _write_counts(visit_clients.engine) == before


@pytest.mark.parametrize(
    "slug,overrides",
    [
        # Exactly one field moves away from the valid demo context each time,
        # so the failure can only be attributed to that field.
        ("week", {"week_no": 3}),
        ("phase", {"phase_type": "基线测评"}),
        ("event", {"event_line": "基线测评窗"}),
    ],
)
def test_demo_selector_with_wrong_context_is_refused_without_writes(
        visit_clients, slug, overrides):
    before = _write_counts(visit_clients.engine)

    response = visit_clients.researcher.post("/visit-plans", json=_plan_body(
        "P-VISIT-04", f"create-ctx-{slug}-0001",
        autopilot_profile_version_id=DEMO_VERSION, **overrides))

    assert response.status_code == 422, response.text
    assert _detail_code(response) == "visit_plan_profile_context_mismatch"
    assert _write_counts(visit_clients.engine) == before


def test_real_subject_cannot_select_the_demo_profile(visit_clients):
    _add_research_patient(visit_clients.engine, "P-REAL-01")
    before = _write_counts(visit_clients.engine)

    response = visit_clients.researcher.post("/visit-plans", json=_plan_body(
        "P-REAL-01", "create-real-demo-01",
        autopilot_profile_version_id=DEMO_VERSION))

    assert response.status_code == 409, response.text
    assert _detail_code(response) == "visit_plan_profile_simulation_required"
    assert _write_counts(visit_clients.engine) == before


@pytest.mark.parametrize(
    "core_code,expected_code,expected_status",
    [
        ("plan_profile_binding_incomplete",
         "visit_plan_profile_binding_incomplete", 409),
        ("plan_profile_digest_mismatch",
         "visit_plan_profile_digest_mismatch", 409),
        ("plan_profile_parent_mismatch",
         "visit_plan_profile_parent_mismatch", 409),
        ("plan_profile_invalid", "visit_plan_profile_invalid", 409),
    ],
)
def test_core_profile_codes_map_to_stable_service_codes_without_writes(
        visit_clients, monkeypatch, core_code, expected_code, expected_status):
    """Classification is structural: the service never parses a message."""
    def refuse(*_args, **_kwargs):
        raise autopilot_plan_profiles.PlanProfileError(core_code, "注入失败")

    monkeypatch.setattr(
        autopilot_plan_profiles, "resolve_requested_definition", refuse)
    before = _write_counts(visit_clients.engine)

    response = visit_clients.researcher.post("/visit-plans", json=_plan_body(
        "P-VISIT-04", f"create-map-{core_code[13:21]}-01",
        autopilot_profile_version_id=DEMO_VERSION))

    assert response.status_code == expected_status, response.text
    assert _detail_code(response) == expected_code
    assert _write_counts(visit_clients.engine) == before


def test_demo_create_reads_the_bundle_exactly_twice(visit_clients,
                                                    monkeypatch):
    """Pre-lock resolution and one lock-anchored resolution, nothing more.

    A third read is made fatal, so a successful create proves both that only
    two reads happened and that no post-lock read could mix parents into the
    persisted Plan.
    """
    real = visit_plan_service._current_definition_bundle
    reads = {"n": 0}

    def counted():
        reads["n"] += 1
        if reads["n"] > 2:
            raise AssertionError("third canonical bundle read")
        return real()

    monkeypatch.setattr(
        visit_plan_service, "_current_definition_bundle", counted)

    created = _create(
        visit_clients.researcher, "P-VISIT-12", "create-two-reads-01",
        autopilot_profile_version_id=DEMO_VERSION)

    assert reads["n"] == 2
    assert created["autopilot_profile_version_id"] == DEMO_VERSION
    row = _plan_row(visit_clients.engine, created["plan_id"])
    assert row.autopilot_profile_definition_digest == DEMO_DIGEST
    assert row.item_bank_version_id == BANK.version_id


def test_canonical_create_reads_the_bundle_exactly_once(visit_clients,
                                                        monkeypatch):
    """A NULL selector loads the canonical bundle once, after the lock."""
    real = visit_plan_service._current_definition_bundle
    reads = {"n": 0}

    def counted():
        reads["n"] += 1
        return real()

    monkeypatch.setattr(
        visit_plan_service, "_current_definition_bundle", counted)

    _create(visit_clients.researcher, "P-VISIT-12", "create-one-read-01")

    assert reads["n"] == 1


def test_definition_change_across_the_patient_lock_is_refused(visit_clients,
                                                              monkeypatch):
    """The second, locked resolution must reproduce the pinned identity."""
    real = autopilot_plan_profiles.resolve_requested_definition
    calls = {"n": 0}

    def drifting(version_id, *, bank, protocol):
        definition = real(version_id, bank=bank, protocol=protocol)
        calls["n"] += 1
        if calls["n"] == 1 or definition is None:
            return definition
        return replace(definition, profile_definition_digest="f" * 64)

    monkeypatch.setattr(
        autopilot_plan_profiles, "resolve_requested_definition", drifting)
    before = _write_counts(visit_clients.engine)

    response = visit_clients.researcher.post("/visit-plans", json=_plan_body(
        "P-VISIT-05", "create-toctou-prof-01",
        autopilot_profile_version_id=DEMO_VERSION))

    assert response.status_code == 409, response.text
    assert _detail_code(response) == "visit_plan_profile_digest_mismatch"
    assert calls["n"] >= 2
    assert _write_counts(visit_clients.engine) == before


# ---- replay may never answer for a tampered pair --------------------------


def test_demo_replay_refuses_after_the_stored_pair_is_cleared(visit_clients):
    created = _create(
        visit_clients.researcher, "P-VISIT-06", "create-tamper-null-01",
        autopilot_profile_version_id=DEMO_VERSION)
    _force_profile_pair(visit_clients.engine, created["plan_id"], None, None)

    replayed = visit_clients.researcher.post("/visit-plans", json=_plan_body(
        "P-VISIT-06", "create-tamper-null-01",
        autopilot_profile_version_id=DEMO_VERSION))

    assert replayed.status_code == 409, replayed.text
    assert _detail_code(replayed) == "visit_plan_profile_digest_mismatch"


def test_canonical_replay_refuses_after_a_demo_pair_is_injected(visit_clients):
    created = _create(
        visit_clients.researcher, "P-VISIT-06", "create-tamper-demo-01")
    _force_profile_pair(
        visit_clients.engine, created["plan_id"], DEMO_VERSION, DEMO_DIGEST)

    replayed = visit_clients.researcher.post(
        "/visit-plans",
        json=_plan_body("P-VISIT-06", "create-tamper-demo-01"))

    assert replayed.status_code == 409, replayed.text
    assert _detail_code(replayed) == "visit_plan_profile_digest_mismatch"


def test_same_idempotency_key_across_profiles_is_an_idempotency_conflict(
        visit_clients):
    _create(visit_clients.researcher, "P-VISIT-07", "create-cross-key-01")

    conflict = visit_clients.researcher.post("/visit-plans", json=_plan_body(
        "P-VISIT-07", "create-cross-key-01",
        autopilot_profile_version_id=DEMO_VERSION))

    assert conflict.status_code == 409, conflict.text
    assert _detail_code(conflict) == "visit_plan_idempotency_conflict"


def test_same_protocol_slot_across_profiles_is_a_slot_conflict(visit_clients):
    _create(visit_clients.researcher, "P-VISIT-08", "create-slot-canon-01")

    conflict = visit_clients.researcher.post("/visit-plans", json=_plan_body(
        "P-VISIT-08", "create-slot-demo-0001",
        autopilot_profile_version_id=DEMO_VERSION))

    assert conflict.status_code == 409, conflict.text
    assert _detail_code(conflict) == "visit_plan_protocol_slot_conflict"


# ---- demo drafts can never be approved, started or projected --------------


def test_demo_draft_approve_is_refused_with_zero_writes(visit_clients):
    """A demo draft reaches the runtime gate: draft is approve's legal CAS."""
    created = _create(
        visit_clients.researcher, "P-VISIT-09", "create-mut-approve-01",
        autopilot_profile_version_id=DEMO_VERSION)
    before = _write_counts(visit_clients.engine)

    refused = _command(
        visit_clients.researcher, created["plan_id"], "approve",
        key="cmd-mut-approve-0001", expected_revision=created["revision"])

    assert refused.status_code == 409, refused.text
    assert _detail_code(refused) == "visit_plan_profile_runtime_not_enabled"
    assert _write_counts(visit_clients.engine) == before
    row = _plan_row(visit_clients.engine, created["plan_id"])
    assert row.status == "draft"
    assert row.revision == created["revision"]


def test_demo_approved_start_is_refused_with_zero_writes(visit_clients):
    """Start needs an approved plan, otherwise CAS legitimately fires first.

    The frozen order puts revision/status CAS ahead of the runtime gate, so a
    draft would correctly answer ``visit_plan_transition_invalid``.  Seeding a
    consistent approved history is what actually exercises the gate.
    """
    created = _create(
        visit_clients.researcher, "P-VISIT-09", "create-mut-start-001",
        autopilot_profile_version_id=DEMO_VERSION)
    _seed_paired_set_history(
        visit_clients.engine, created["plan_id"], "approve",
        "cmd-seed-approve-001", created["revision"])
    before = _write_counts(visit_clients.engine)

    refused = _command(
        visit_clients.researcher, created["plan_id"], "start",
        key="cmd-mut-start-00001",
        expected_revision=created["revision"] + 1)

    assert refused.status_code == 409, refused.text
    assert _detail_code(refused) == "visit_plan_profile_runtime_not_enabled"
    assert _write_counts(visit_clients.engine) == before
    row = _plan_row(visit_clients.engine, created["plan_id"])
    assert row.status == "approved"


def _paired_set_mutation_hash(
        action: str, plan_id: str, key: str, expected_revision: int) -> str:
    """Independently rebuild the authoritative paired-set mutation hash.

    Constructed from the frozen payload shape rather than by calling the
    production mutation helper, so a defect in that helper cannot make this
    fixture agree with it.
    """
    payload: dict = {
        "plan_id": plan_id,
        "idempotency_key": key,
        "expected_revision": expected_revision,
        "repeat_protocol_version_id": REPEAT_PROTOCOL.version_id,
        "repeat_protocol_definition_digest": REPEAT_PROTOCOL.definition_digest,
        "autopilot_profile_version_id": DEMO_VERSION,
        "autopilot_profile_definition_digest": DEMO_DIGEST,
    }
    return _frozen_request_hash(action, payload)


def _seed_paired_set_history(
        engine, plan_id: str, action: str, key: str, revision: int) -> None:
    """A fully consistent demo ledger: real hash, aligned revision and status.

    ``start`` additionally receives the Session and RuntimeState its status
    implies, so the replay must traverse the whole chain before it is refused.
    """
    status = "approved" if action == "approve" else "started"
    now = datetime.now()
    with Session(engine) as session:
        plan = session.get(VisitPlan, plan_id)
        assert plan is not None
        if action == "start":
            session.add(VisitPlanCommand(
                plan_id=plan_id, event_seq=2,
                idempotency_key=f"{key}-approve",
                command_type="approve",
                request_hash=_paired_set_mutation_hash(
                    "approve", plan_id, f"{key}-approve", revision),
                actor_id="ACTOR-visit-researcher",
                expected_revision=revision, resulting_revision=revision + 1,
                created_at=now,
            ))
            plan.approved_by = "ACTOR-visit-researcher"
            plan.approved_at = now
        session.add(VisitPlanCommand(
            plan_id=plan_id,
            event_seq=3 if action == "start" else 2,
            idempotency_key=key,
            command_type=action,
            request_hash=_paired_set_mutation_hash(
                action, plan_id, key,
                revision + 1 if action == "start" else revision),
            actor_id="ACTOR-visit-researcher",
            expected_revision=revision + 1 if action == "start" else revision,
            resulting_revision=revision + 2 if action == "start" else revision + 1,
            created_at=now,
        ))
        plan.status = status
        plan.revision = revision + 2 if action == "start" else revision + 1
        plan.autopilot_profile_version_id = DEMO_VERSION
        plan.autopilot_profile_definition_digest = DEMO_DIGEST
        # Both histories pass through "approved", so the approval actor and
        # timestamp belong on either branch; ``updated_at`` tracks the last
        # transition.  Without these the row is not the complete, internally
        # consistent history this helper claims to build.
        plan.approved_by = "ACTOR-visit-researcher"
        plan.approved_at = now
        plan.updated_at = now
        if action == "start":
            plan.started_by = "ACTOR-visit-researcher"
            plan.started_at = now
            session.add(TrainSession(
                session_id=f"s-hist-{plan_id[-8:]}",
                patient_id=plan.patient_id,
                visit_plan_id=plan_id,
                session_sitting_no=plan.session_sitting_no,
                training_date=TODAY,
                week_no=plan.week_no,
                phase_type=plan.phase_type,
                event_line=plan.event_line,
                trainer_id="ACTOR-visit-researcher",
                item_bank_version_id=plan.item_bank_version_id,
                item_bank_definition_digest=plan.item_bank_definition_digest,
                autopilot_protocol_version_id=(
                    plan.autopilot_protocol_version_id),
                autopilot_protocol_definition_digest=(
                    plan.autopilot_protocol_definition_digest),
                repeat_protocol_version_id=plan.repeat_protocol_version_id,
                repeat_protocol_definition_digest=(
                    plan.repeat_protocol_definition_digest),
                autopilot_profile_version_id=DEMO_VERSION,
                autopilot_profile_definition_digest=DEMO_DIGEST,
                is_simulation=True,
                data_classification="simulation",
            ))
            session.add(SessionRuntimeState(
                session_id=f"s-hist-{plan_id[-8:]}",
                status="active", revision=0, updated_at=now,
            ))
        session.add(plan)
        session.commit()


@pytest.mark.parametrize("action", ["approve", "start"])
def test_exact_paired_set_history_reaches_the_stable_runtime_error(
        visit_clients, action):
    """A fully consistent demo ledger is refused only after it is validated."""
    created = _create(
        visit_clients.researcher, "P-VISIT-10", f"create-hist-{action}-01",
        autopilot_profile_version_id=DEMO_VERSION)
    key = f"cmd-hist-{action}-0001"
    revision = created["revision"]
    _seed_paired_set_history(
        visit_clients.engine, created["plan_id"], action, key, revision)
    before = _write_counts(visit_clients.engine)

    refused = _command(
        visit_clients.researcher, created["plan_id"], action, key=key,
        expected_revision=revision + 1 if action == "start" else revision)

    assert refused.status_code == 409, refused.text
    assert _detail_code(refused) == "visit_plan_profile_runtime_not_enabled"
    assert _write_counts(visit_clients.engine) == before


@pytest.mark.parametrize("action", ["approve", "start"])
def test_paired_set_replay_survives_a_future_active_bank_and_protocol(
        visit_clients, monkeypatch, action):
    """A historical demo row resolves from the registry, never from today.

    Every current-content loader is made fatal after the row exists, so the
    replay can only succeed by reading the immutable registered definition and
    the Plan's own frozen parents.  It must still end at the runtime gate,
    after its identity, ledger and Plan/Session facts have all passed.
    """
    created = _create(
        visit_clients.researcher, "P-VISIT-12", f"create-future-{action[:3]}",
        autopilot_profile_version_id=DEMO_VERSION)
    key = f"cmd-future-{action[:3]}-01"
    _seed_paired_set_history(
        visit_clients.engine, created["plan_id"], action, key,
        created["revision"])

    def boom(*_args, **_kwargs):
        raise AssertionError(
            "historical paired-set replay must not read current content")

    # The service's real route to current content is this bundle loader, not
    # the profile module's default helpers.  Without it the test would still
    # pass if paired-set replay regressed to today's parents.  Installed only
    # now, because the setup demo create legitimately reads the bundle twice.
    monkeypatch.setattr(
        visit_plan_service, "_current_definition_bundle", boom)
    monkeypatch.setattr(autopilot_plan_profiles, "_default_bank", boom)
    monkeypatch.setattr(autopilot_plan_profiles, "_default_protocol", boom)
    monkeypatch.setattr(
        autopilot_plan_profiles, "resolve_for_visit_plan", boom)
    before = _write_counts(visit_clients.engine)

    refused = _command(
        visit_clients.researcher, created["plan_id"], action, key=key,
        expected_revision=(
            created["revision"] + 1 if action == "start"
            else created["revision"]))

    assert refused.status_code == 409, refused.text
    assert _detail_code(refused) == "visit_plan_profile_runtime_not_enabled"
    assert _write_counts(visit_clients.engine) == before


def test_wrong_history_hash_is_not_masked_by_the_runtime_error(visit_clients):
    """A stale expected_revision changes the hash, so idempotency answers."""
    created = _create(
        visit_clients.researcher, "P-VISIT-11", "create-mask-hash-01",
        autopilot_profile_version_id=DEMO_VERSION)
    key = "cmd-mask-hash-00001"
    revision = created["revision"]
    _seed_paired_set_history(
        visit_clients.engine, created["plan_id"], "approve", key, revision)

    refused = _command(
        visit_clients.researcher, created["plan_id"], "approve",
        key=key, expected_revision=revision + 5)

    assert refused.status_code == 409, refused.text
    assert _detail_code(refused) == "visit_plan_idempotency_conflict"


def test_wrong_history_actor_is_not_masked_by_the_runtime_error(visit_clients):
    """The ledger stays intact; a different authenticated caller replays it.

    ``VisitPlanCommand`` is append-only and its model guard is never disabled,
    so the alternate actor comes from the second researcher client rather than
    from mutating the seeded row.
    """
    created = _create(
        visit_clients.researcher, "P-VISIT-11", "create-mask-actor-1",
        autopilot_profile_version_id=DEMO_VERSION)
    key = "cmd-mask-actor-00001"
    revision = created["revision"]
    _seed_paired_set_history(
        visit_clients.engine, created["plan_id"], "approve", key, revision)

    refused = _command(
        visit_clients.researcher_b, created["plan_id"], "approve",
        key=key, expected_revision=revision)

    assert refused.status_code == 409, refused.text
    assert _detail_code(refused) == "visit_plan_idempotency_conflict"


def test_today_queue_fails_closed_on_an_anomalous_demo_row(visit_clients):
    """The queue only selects approved rows, so that is the reachable case."""
    created = _create(
        visit_clients.researcher, "P-VISIT-11", "create-today-demo-01",
        autopilot_profile_version_id=DEMO_VERSION)
    _force_profile_pair(
        visit_clients.engine, created["plan_id"], DEMO_VERSION, DEMO_DIGEST,
        status="approved")

    queue = visit_clients.researcher.get("/visit-plans/today")

    assert queue.status_code == 409, queue.text
    assert _detail_code(queue) == "visit_plan_profile_runtime_not_enabled"


@pytest.mark.parametrize("status", ["approved", "started"])
def test_patient_listing_fails_closed_for_both_runtime_statuses(
        visit_clients, status):
    created = _create(
        visit_clients.researcher, "P-VISIT-07", f"create-both-{status[:4]}-1",
        autopilot_profile_version_id=DEMO_VERSION)
    _force_profile_pair(
        visit_clients.engine, created["plan_id"], DEMO_VERSION, DEMO_DIGEST,
        status=status)

    listing = visit_clients.researcher.get(
        "/visit-plans", params={"patient_id": "P-VISIT-07"})

    assert listing.status_code == 409, listing.text
    assert _detail_code(listing) == "visit_plan_profile_runtime_not_enabled"


def test_patient_listing_fails_closed_on_an_anomalous_demo_row(visit_clients):
    created = _create(
        visit_clients.researcher, "P-VISIT-11", "create-list-demo-01",
        autopilot_profile_version_id=DEMO_VERSION)
    _force_profile_pair(
        visit_clients.engine, created["plan_id"], DEMO_VERSION, DEMO_DIGEST,
        status="approved")

    listing = visit_clients.researcher.get(
        "/visit-plans", params={"patient_id": "P-VISIT-11"})

    assert listing.status_code == 409, listing.text
    assert _detail_code(listing) == "visit_plan_profile_runtime_not_enabled"


def test_demo_draft_listing_stays_governable(visit_clients):
    created = _create(
        visit_clients.researcher, "P-VISIT-12", "create-draft-list-01",
        autopilot_profile_version_id=DEMO_VERSION)

    listing = visit_clients.researcher.get(
        "/visit-plans", params={"patient_id": "P-VISIT-12"})

    assert listing.status_code == 200, listing.text
    rows = [row for row in listing.json()
            if row["plan_id"] == created["plan_id"]]
    assert len(rows) == 1
    assert rows[0]["autopilot_profile_version_id"] == DEMO_VERSION
    assert "autopilot_profile_definition_digest" not in rows[0]


# ---- cancel must survive definition drift ---------------------------------


def test_manifest_loss_cancel_journey_releases_the_slot(visit_clients,
                                                        monkeypatch):
    """One journey: lose the definition, cancel, verify hash, replay, reuse.

    Every profile resolution entry point is made fatal after the draft exists,
    so a single manifest read anywhere in cancel would fail the test outright.
    """
    key = "cancel-drift-000001"
    created = _create(
        visit_clients.researcher, "P-VISIT-09", "create-cancel-drift-1",
        autopilot_profile_version_id=DEMO_VERSION)
    revision = created["revision"]

    def gone(*_args, **_kwargs):
        raise AssertionError("cancel must not resolve the current manifest")

    # Scoped to exactly these four attributes.  A global ``monkeypatch.undo()``
    # would also revert the ``visit_clients`` fixture's own ``db.engine`` patch
    # and let the closing request escape the isolated test database entirely.
    with monkeypatch.context() as manifest_lost:
        for name in (
            "resolve_requested_definition",
            "resolve_registered_binding",
            "resolve_for_visit_plan",
            "_load_registered_definition",
        ):
            manifest_lost.setattr(autopilot_plan_profiles, name, gone)

        cancelled = _command(
            visit_clients.researcher, created["plan_id"], "cancel",
            key=key, expected_revision=revision,
            reason_code="protocol_correction")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["autopilot_profile_version_id"] == DEMO_VERSION

        # The stored pair, and only the stored pair, entered the cancel hash.
        assert _stored_hash(visit_clients.engine, key) == _frozen_request_hash(
            "cancel",
            {
                "plan_id": created["plan_id"],
                "idempotency_key": key,
                "expected_revision": revision,
                "reason_code": "protocol_correction",
                "autopilot_profile_version_id": DEMO_VERSION,
                "autopilot_profile_definition_digest": DEMO_DIGEST,
            },
        )

        replayed = _command(
            visit_clients.researcher, created["plan_id"], "cancel",
            key=key, expected_revision=revision,
            reason_code="protocol_correction")
        assert replayed.status_code == 200, replayed.text
        assert replayed.json() == cancelled.json()

        row = _plan_row(visit_clients.engine, created["plan_id"])
        assert row.protocol_slot_key is None
        assert row.autopilot_profile_version_id == DEMO_VERSION

    reused = _create(
        visit_clients.researcher, "P-VISIT-09", "create-slot-reuse-2")
    assert reused["autopilot_profile_version_id"] is None
    assert reused["status"] == "draft"


# ---- canonical byte compatibility -----------------------------------------


def _stored_hash(engine, key: str) -> str:
    with Session(engine) as session:
        command = session.exec(select(VisitPlanCommand).where(
            VisitPlanCommand.idempotency_key == key)).one()
        return command.request_hash


ACTOR = "ACTOR-visit-researcher"


def _seed_legacy_plan(session, *, plan_id, patient_id, slot_key, now):
    """A pre-D1A canonical Plan seeded entirely from the frozen constants.

    Nothing here reads ``TODAY`` or the live BANK/PROTOCOL/REPEAT globals: the
    row is immutable historical evidence, so it must not co-move with whatever
    the current content happens to be.  Profile pair stays NULL/NULL.
    """
    return VisitPlan(
        plan_id=plan_id,
        protocol_slot_key=slot_key,
        patient_id=patient_id,
        scheduled_date=LEGACY_DATE,
        scheduled_time=LEGACY_TIME,
        queue_order=1,
        session_sitting_no=1,
        week_no=2,
        phase_type="正式训练",
        event_line="正式训练",
        item_bank_version_id=LEGACY_BANK_VERSION,
        item_bank_definition_digest=LEGACY_BANK_DIGEST,
        autopilot_protocol_version_id=LEGACY_PROTOCOL_VERSION,
        autopilot_protocol_definition_digest=LEGACY_PROTOCOL_DIGEST,
        repeat_protocol_version_id=LEGACY_REPEAT_VERSION,
        repeat_protocol_definition_digest=LEGACY_REPEAT_DIGEST,
        autopilot_profile_version_id=None,
        autopilot_profile_definition_digest=None,
        is_simulation=True,
        data_classification="simulation",
        status="draft",
        revision=1,
        created_by=ACTOR,
        created_at=now,
        updated_at=now,
    )


def _legacy_command(*, plan_id, seq, key, command_type, request_hash,
                    expected_revision, now, reason_code=None):
    return VisitPlanCommand(
        plan_id=plan_id, event_seq=seq, idempotency_key=key,
        command_type=command_type, request_hash=request_hash, actor_id=ACTOR,
        expected_revision=expected_revision,
        resulting_revision=expected_revision + 1,
        reason_code=reason_code, created_at=now,
    )


def _seed_legacy_started_chain(engine, patient_id: str) -> dict:
    """create -> approve -> start, hashed by the test-side legacy algorithms."""
    plan_id = "vp_" + "L" * 24
    session_id = "s_" + "L" * 24
    body = _plan_body(
        patient_id, "legacy-create-started1", scheduled_date=LEGACY_DATE)
    # The independently rebuilt hashes must reproduce the literal receipts.
    assert _legacy_create_hash(body) == LEGACY_STARTED_CREATE_HASH
    assert _legacy_mutation_hash(
        "approve", plan_id, "legacy-approve-started", 1) == (
            LEGACY_STARTED_APPROVE_HASH)
    assert _legacy_mutation_hash(
        "start", plan_id, "legacy-start-started1", 2) == (
            LEGACY_STARTED_START_HASH)
    now = datetime(2026, 7, 31, 9, 30)
    with Session(engine) as session:
        plan = _seed_legacy_plan(
            session, plan_id=plan_id, patient_id=patient_id,
            slot_key=LEGACY_STARTED_SLOT, now=now)
        plan.status = "started"
        plan.revision = 3
        plan.approved_by = ACTOR
        plan.approved_at = now
        plan.started_by = ACTOR
        plan.started_at = now
        session.add(plan)
        session.add(_legacy_command(
            plan_id=plan_id, seq=1, key="legacy-create-started1",
            command_type="create", request_hash=LEGACY_STARTED_CREATE_HASH,
            expected_revision=0, now=now))
        session.add(_legacy_command(
            plan_id=plan_id, seq=2, key="legacy-approve-started",
            command_type="approve",
            request_hash=LEGACY_STARTED_APPROVE_HASH,
            expected_revision=1, now=now))
        session.add(_legacy_command(
            plan_id=plan_id, seq=3, key="legacy-start-started1",
            command_type="start",
            request_hash=LEGACY_STARTED_START_HASH,
            expected_revision=2, now=now))
        session.add(TrainSession(
            session_id=session_id, patient_id=patient_id,
            visit_plan_id=plan_id, session_sitting_no=1,
            training_date=LEGACY_DATE,
            week_no=2, phase_type="正式训练", event_line="正式训练",
            trainer_id=ACTOR,
            item_bank_version_id=LEGACY_BANK_VERSION,
            item_bank_definition_digest=LEGACY_BANK_DIGEST,
            autopilot_protocol_version_id=LEGACY_PROTOCOL_VERSION,
            autopilot_protocol_definition_digest=LEGACY_PROTOCOL_DIGEST,
            repeat_protocol_version_id=LEGACY_REPEAT_VERSION,
            repeat_protocol_definition_digest=LEGACY_REPEAT_DIGEST,
            autopilot_profile_version_id=None,
            autopilot_profile_definition_digest=None,
            is_simulation=True, data_classification="simulation",
        ))
        session.add(SessionRuntimeState(
            session_id=session_id, status="active", revision=0,
            updated_at=now))
        session.commit()
    return {"plan_id": plan_id, "session_id": session_id, "body": body}


def _seed_legacy_cancelled_chain(engine, patient_id: str) -> dict:
    plan_id = "vp_" + "C" * 24
    body = _plan_body(
        patient_id, "legacy-create-cancelled", scheduled_date=LEGACY_DATE)
    assert _legacy_create_hash(body) == LEGACY_CANCELLED_CREATE_HASH
    assert _legacy_mutation_hash(
        "cancel", plan_id, "legacy-cancel-cancelled", 1,
        reason_code="schedule_changed") == LEGACY_CANCELLED_CANCEL_HASH
    now = datetime(2026, 7, 31, 9, 30)
    with Session(engine) as session:
        # Seeded with the slot it held before cancellation, then released to
        # NULL exactly as ``cancel_plan`` leaves it.
        plan = _seed_legacy_plan(
            session, plan_id=plan_id, patient_id=patient_id,
            slot_key=LEGACY_CANCELLED_SLOT, now=now)
        plan.status = "cancelled"
        plan.revision = 2
        plan.protocol_slot_key = None
        plan.cancelled_by = ACTOR
        plan.cancelled_at = now
        plan.updated_at = now
        session.add(plan)
        session.add(_legacy_command(
            plan_id=plan_id, seq=1, key="legacy-create-cancelled",
            command_type="create", request_hash=LEGACY_CANCELLED_CREATE_HASH,
            expected_revision=0, now=now))
        session.add(_legacy_command(
            plan_id=plan_id, seq=2, key="legacy-cancel-cancelled",
            command_type="cancel",
            request_hash=LEGACY_CANCELLED_CANCEL_HASH,
            expected_revision=1, now=now, reason_code="schedule_changed"))
        session.commit()
    return {"plan_id": plan_id, "body": body}


def test_seeded_pre_d1a_history_replays_without_idempotency_conflict(
        visit_clients):
    """Authentic pre-D1A ledgers must still answer today's identical requests.

    The rows are seeded rather than produced by the current mutators, and every
    hash comes from the test-side frozen legacy algorithm, so this proves byte
    compatibility instead of comparing a helper with itself.
    """
    started = _seed_legacy_started_chain(visit_clients.engine, "P-VISIT-05")
    cancelled = _seed_legacy_cancelled_chain(visit_clients.engine, "P-VISIT-06")

    replay_create = visit_clients.researcher.post(
        "/visit-plans", json=started["body"])
    assert replay_create.status_code == 200, replay_create.text
    assert replay_create.json()["plan_id"] == started["plan_id"]
    assert replay_create.json()["status"] == "draft"
    assert replay_create.json()["autopilot_profile_version_id"] is None

    replay_approve = _command(
        visit_clients.researcher, started["plan_id"], "approve",
        key="legacy-approve-started", expected_revision=1)
    assert replay_approve.status_code == 200, replay_approve.text
    assert replay_approve.json()["status"] == "approved"

    replay_start = _command(
        visit_clients.researcher, started["plan_id"], "start",
        key="legacy-start-started1", expected_revision=2)
    assert replay_start.status_code == 200, replay_start.text
    assert replay_start.json()["status"] == "started"
    assert replay_start.json()["session_id"] == started["session_id"]

    replay_create_cancelled = visit_clients.researcher.post(
        "/visit-plans", json=cancelled["body"])
    assert replay_create_cancelled.status_code == 200, (
        replay_create_cancelled.text)
    assert replay_create_cancelled.json()["plan_id"] == cancelled["plan_id"]

    replay_cancel = _command(
        visit_clients.researcher, cancelled["plan_id"], "cancel",
        key="legacy-cancel-cancelled", expected_revision=1,
        reason_code="schedule_changed")
    assert replay_cancel.status_code == 200, replay_cancel.text
    assert replay_cancel.json()["status"] == "cancelled"
    assert replay_cancel.json()["autopilot_profile_version_id"] is None


# ---- session copy, admission gate and direct-session refusal --------------


def test_canonical_start_copies_a_null_profile_pair_onto_the_session(
        visit_clients):
    created = _create(
        visit_clients.researcher, "P-VISIT-12", "create-null-start-01")
    approved = _command(
        visit_clients.researcher, created["plan_id"], "approve",
        key="approve-null-start-01", expected_revision=created["revision"])
    assert approved.status_code == 200, approved.text
    started = _command(
        visit_clients.researcher, created["plan_id"], "start",
        key="start-null-start-001",
        expected_revision=approved.json()["revision"])
    assert started.status_code == 200, started.text

    with Session(visit_clients.engine) as session:
        train_session = session.get(
            TrainSession, started.json()["session_id"])
        assert train_session is not None
        assert train_session.autopilot_profile_version_id is None
        assert train_session.autopilot_profile_definition_digest is None


@pytest.mark.parametrize(
    "slug,version,digest",
    [
        ("version", DEMO_VERSION, None),
        ("digest", None, DEMO_DIGEST),
        ("both", DEMO_VERSION, DEMO_DIGEST),
    ],
)
def test_direct_session_creation_rejects_any_profile_field(
        visit_clients, monkeypatch, slug, version, digest):
    monkeypatch.setenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", "1")
    before = _write_counts(visit_clients.engine)

    response = visit_clients.researcher.post("/sessions", json={
        "session_id": f"s-direct-{slug}",
        "patient_id": "P-VISIT-01",
        "session_sitting_no": 1,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "item_bank_version_id": BANK.version_id,
        "is_simulation": True,
        "autopilot_profile_version_id": version,
        "autopilot_profile_definition_digest": digest,
    })

    assert response.status_code == 409, response.text
    assert _detail_code(response) == "direct_session_profile_forbidden"
    assert _write_counts(visit_clients.engine) == before


def _started_canonical_session(visit_clients, patient_id: str, slug: str):
    created = _create(
        visit_clients.researcher, patient_id, f"create-{slug}-0001")
    approved = _command(
        visit_clients.researcher, created["plan_id"], "approve",
        key=f"approve-{slug}-0001", expected_revision=created["revision"])
    assert approved.status_code == 200, approved.text
    started = _command(
        visit_clients.researcher, created["plan_id"], "start",
        key=f"start-{slug}-00001",
        expected_revision=approved.json()["revision"])
    assert started.status_code == 200, started.text
    return created, started.json()["session_id"]


@pytest.mark.parametrize(
    "slug,on_plan,on_session",
    [
        ("session-only", False, True),
        ("plan-only", True, False),
        ("matching", True, True),
    ],
)
def test_live_admission_closes_on_every_profile_pair_shape(
        visit_clients, monkeypatch, slug, on_plan, on_session):
    """A mismatch and a perfectly matching pair are both kept out of runtime.

    Uses the write-capable microphone endpoint and the whole admission write
    set, so a refusal that still mutated something cannot pass.
    """
    # The production admission must really run; the pytest escape would return
    # before any binding is compared.
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    created, session_id = _started_canonical_session(
        visit_clients, "P-VISIT-02", f"admit-{slug}")

    with Session(visit_clients.engine) as session:
        plan = session.get(VisitPlan, created["plan_id"])
        train_session = session.get(TrainSession, session_id)
        assert plan is not None and train_session is not None
        if on_plan:
            plan.autopilot_profile_version_id = DEMO_VERSION
            plan.autopilot_profile_definition_digest = DEMO_DIGEST
            session.add(plan)
        if on_session:
            train_session.autopilot_profile_version_id = DEMO_VERSION
            train_session.autopilot_profile_definition_digest = DEMO_DIGEST
            session.add(train_session)
        session.commit()

    before = _admission_write_set(visit_clients.engine)

    _assert_visit_plan_admission_rejected(visit_clients.researcher.post(
        f"/sessions/{session_id}/recording-authorization"))

    assert _admission_write_set(visit_clients.engine) == before


# ---- half-pair corruption across mutation and read surfaces ---------------


@pytest.mark.parametrize(
    "slug,version,digest",
    [("version-only", DEMO_VERSION, None), ("digest-only", None, DEMO_DIGEST)],
)
@pytest.mark.parametrize("surface", ["cancel", "receipt", "list"])
def test_half_pair_corruption_fails_closed_everywhere(
        visit_clients, slug, version, digest, surface):
    """A half pair is never canonical, never hashed and never projected."""
    created = _create(
        visit_clients.researcher, "P-VISIT-04",
        f"create-half-{surface[:4]}-{slug[:4]}")
    _force_profile_pair(
        visit_clients.engine, created["plan_id"], version, digest)
    before = _write_counts(visit_clients.engine)

    if surface == "cancel":
        response = _command(
            visit_clients.researcher, created["plan_id"], "cancel",
            key=f"cancel-half-{surface[:4]}-{slug[:4]}",
            expected_revision=created["revision"],
            reason_code="protocol_correction")
    elif surface == "receipt":
        response = visit_clients.researcher.post(
            "/visit-plans",
            json=_plan_body(
                "P-VISIT-04", f"create-half-{surface[:4]}-{slug[:4]}"))
    else:
        response = visit_clients.researcher.get(
            "/visit-plans", params={"patient_id": "P-VISIT-04"})

    assert response.status_code == 409, response.text
    assert _detail_code(response) == "visit_plan_profile_binding_incomplete"
    assert _write_counts(visit_clients.engine) == before


# ---- command receipt gates on the plan's current status -------------------


@pytest.mark.parametrize("status", ["approved", "started"])
def test_tampered_canonical_plan_reports_integrity_before_the_runtime_gate(
        visit_clients, status):
    """A canonical request against a force-written demo pair is an integrity fault.

    The request pair is ``(None, None)`` while the Plan now stores a demo pair,
    so the mismatch is the honest answer.  The runtime code must not mask it.
    """
    created = _create(
        visit_clients.researcher, "P-VISIT-05",
        f"create-now-{status[:4]}-001")
    _force_profile_pair(
        visit_clients.engine, created["plan_id"], DEMO_VERSION, DEMO_DIGEST,
        status=status)

    replayed = visit_clients.researcher.post(
        "/visit-plans",
        json=_plan_body("P-VISIT-05", f"create-now-{status[:4]}-001"))

    assert replayed.status_code == 409, replayed.text
    assert _detail_code(replayed) == "visit_plan_profile_digest_mismatch"


@pytest.mark.parametrize("action", ["approve", "start"])
def test_demo_create_key_replay_fails_closed_once_the_plan_is_runtime(
        visit_clients, action):
    """Only a pair-consistent whole row reaches the current-status gate.

    The create command is genuinely produced by the demo create path, so its
    hash and pair are authentic; the approve/start chain is then seeded around
    it.  Replaying the original demo body must fail closed on the Plan's
    current status rather than hand back its old draft receipt.
    """
    key = f"create-demo-now-{action[:3]}"
    created = _create(
        visit_clients.researcher, "P-VISIT-05", key,
        autopilot_profile_version_id=DEMO_VERSION)
    _seed_paired_set_history(
        visit_clients.engine, created["plan_id"], action,
        f"cmd-demo-now-{action[:3]}", created["revision"])

    replayed = visit_clients.researcher.post("/visit-plans", json=_plan_body(
        "P-VISIT-05", key, autopilot_profile_version_id=DEMO_VERSION))

    assert replayed.status_code == 409, replayed.text
    assert _detail_code(replayed) == "visit_plan_profile_runtime_not_enabled"


def test_withdrawn_early_return_still_detects_an_anomalous_demo_row(
        visit_clients):
    """Privacy must not hide a demo runtime anomaly from governance."""
    created = _create(
        visit_clients.researcher, "P-VISIT-06", "create-withdrawn-anom",
        autopilot_profile_version_id=DEMO_VERSION)
    _force_profile_pair(
        visit_clients.engine, created["plan_id"], DEMO_VERSION, DEMO_DIGEST,
        status="approved")
    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-06")
        assert patient is not None
        patient.withdrawal_status = "已撤回"
        session.add(patient)
        session.commit()

    listing = visit_clients.researcher.get(
        "/visit-plans", params={"patient_id": "P-VISIT-06"})

    assert listing.status_code == 409, listing.text
    assert _detail_code(listing) == "visit_plan_profile_runtime_not_enabled"


def test_withdrawn_early_return_is_unchanged_without_an_anomaly(visit_clients):
    _create(visit_clients.researcher, "P-VISIT-07", "create-withdrawn-ok01")
    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-07")
        assert patient is not None
        patient.withdrawal_status = "已撤回"
        session.add(patient)
        session.commit()

    listing = visit_clients.researcher.get(
        "/visit-plans", params={"patient_id": "P-VISIT-07"})

    assert listing.status_code == 200, listing.text
    assert listing.json() == []


def test_create_replay_refuses_after_the_subject_becomes_real(visit_clients):
    """The locked patient row is the subject authority, not the first read."""
    created = _create(
        visit_clients.researcher, "P-VISIT-08", "create-subject-flip1",
        autopilot_profile_version_id=DEMO_VERSION)
    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-08")
        assert patient is not None
        patient.is_simulation_subject = False
        session.add(patient)
        session.commit()
    before = _write_counts(visit_clients.engine)

    replayed = visit_clients.researcher.post("/visit-plans", json=_plan_body(
        "P-VISIT-08", "create-subject-flip1",
        autopilot_profile_version_id=DEMO_VERSION))

    assert replayed.status_code == 409, replayed.text
    assert _detail_code(replayed) == "visit_plan_profile_simulation_required"
    assert _write_counts(visit_clients.engine) == before
    assert created["autopilot_profile_version_id"] == DEMO_VERSION


def _apply_persisted_drift(engine, plan_id: str, shape: str) -> None:
    """Corrupt the stored row itself; never inject a synthetic exception.

    Each shape stays inside the database CHECKs so the real resolver and the
    real service comparison are what produce the error, which is the only way
    these assertions say anything about the historical-binding seam.
    """
    with Session(engine) as session:
        row = session.get(VisitPlan, plan_id)
        assert row is not None
        if shape == "unknown":
            # Complete, well-formed pair whose version is not registered.
            row.autopilot_profile_version_id = "unregistered-demo-v9"
            row.autopilot_profile_definition_digest = DEMO_DIGEST
        elif shape == "digest":
            # Real registered version, wrong but syntactically valid digest.
            row.autopilot_profile_version_id = DEMO_VERSION
            row.autopilot_profile_definition_digest = "f" * 64
        else:
            # Profile pair left authentic; exactly one frozen parent moves, so
            # only the service's own four-field comparison can catch it.
            row.item_bank_version_id = "wk2-v1-drifted-parent"
        session.add(row)
        session.commit()


@pytest.mark.parametrize(
    "shape,expected_code",
    [
        ("unknown", "visit_plan_profile_unknown"),
        ("digest", "visit_plan_profile_digest_mismatch"),
        ("parent", "visit_plan_profile_parent_mismatch"),
    ],
)
@pytest.mark.parametrize("action", ["approve", "start"])
def test_persisted_profile_drift_is_reported_before_the_runtime_gate(
        visit_clients, shape, expected_code, action):
    """A genuinely drifted stored row reports its own code, not the gate.

    ``plan_profile_parent_mismatch`` is deliberately not raised from
    ``resolve_registered_binding``: that helper does not own the Plan-parent
    comparison, so faking it there would assert nothing.
    """
    created = _create(
        visit_clients.researcher, "P-VISIT-09",
        f"create-drift-{shape}-{action[:3]}",
        autopilot_profile_version_id=DEMO_VERSION)
    if action == "start":
        _seed_paired_set_history(
            visit_clients.engine, created["plan_id"], "approve",
            f"seed-drift-{shape}-{action[:3]}", created["revision"])
    _apply_persisted_drift(visit_clients.engine, created["plan_id"], shape)
    # Snapshot after every fixture write, so only the refusal is measured.
    before = _write_counts(visit_clients.engine)

    refused = _command(
        visit_clients.researcher, created["plan_id"], action,
        key=f"cmd-drift-{shape}-{action[:3]}",
        expected_revision=(
            created["revision"] + 1 if action == "start"
            else created["revision"]))

    assert refused.status_code in {409, 422}, refused.text
    assert _detail_code(refused) == expected_code
    assert _write_counts(visit_clients.engine) == before


def test_started_session_projection_still_carries_both_profile_keys(
        visit_clients):
    created = _create(
        visit_clients.researcher, "P-VISIT-03", "create-session-keys-1")
    approved = _command(
        visit_clients.researcher, created["plan_id"], "approve",
        key="approve-session-keys-1", expected_revision=created["revision"])
    assert approved.status_code == 200, approved.text
    started = _command(
        visit_clients.researcher, created["plan_id"], "start",
        key="start-session-keys-01",
        expected_revision=approved.json()["revision"])
    assert started.status_code == 200, started.text

    read = visit_clients.researcher.get(
        f"/sessions/{started.json()['session_id']}")

    assert read.status_code == 200, read.text
    assert STARTED_SESSION_KEYS <= set(read.json())
    assert read.json()["autopilot_profile_version_id"] is None
    assert read.json()["autopilot_profile_definition_digest"] is None


def test_started_plan_list_conceals_session_binding_from_non_owner(
        visit_clients: VisitClients):
    """The shared pre-session queue must stop being a session ownership index."""
    created = _create(
        visit_clients.researcher,
        "P-VISIT-01",
        "create-owner-projection-0001",
    )
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="approve-owner-projection-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()
    started_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="start-owner-projection-0001",
        expected_revision=approved["revision"],
    )
    assert started_response.status_code == 200, started_response.text
    started = started_response.json()
    assert started["session_id"]
    assert started["started_by"] == "ACTOR-visit-researcher"

    queued = _create(
        visit_clients.researcher,
        "P-VISIT-02",
        "create-shared-queue-0001",
    )
    queued_response = _command(
        visit_clients.researcher,
        queued["plan_id"],
        "approve",
        key="approve-shared-queue-0001",
        expected_revision=queued["revision"],
    )
    assert queued_response.status_code == 200, queued_response.text

    owner_rows = visit_clients.researcher.get(
        "/visit-plans", params={"patient_id": "P-VISIT-01"})
    assert owner_rows.status_code == 200, owner_rows.text
    assert owner_rows.json()[0]["session_id"] == started["session_id"]
    assert owner_rows.json()[0]["started_by"] == "ACTOR-visit-researcher"

    for non_owner in (visit_clients.researcher_b, visit_clients.steward):
        hidden = non_owner.get(
            "/visit-plans", params={"patient_id": "P-VISIT-01"})
        assert hidden.status_code == 200, hidden.text
        receipt = hidden.json()[0]
        assert receipt["status"] == "started"
        assert receipt["session_id"] is None
        assert receipt["started_by"] is None
        assert receipt["started_at"] == started["started_at"]
        assert non_owner.get(
            f"/sessions/{started['session_id']}").status_code != 200

        shared = non_owner.get(
            "/visit-plans", params={"patient_id": "P-VISIT-02"})
        assert shared.status_code == 200, shared.text
        assert shared.json()[0]["status"] == "approved"
        assert shared.json()[0]["plan_id"] == queued["plan_id"]

    admin_rows = visit_clients.admin.get(
        "/visit-plans", params={"patient_id": "P-VISIT-01"})
    assert admin_rows.status_code == 200, admin_rows.text
    assert admin_rows.json()[0]["session_id"] == started["session_id"]
    assert admin_rows.json()[0]["started_by"] == "ACTOR-visit-researcher"

    # Today's operational queue is pre-start only.  Prove the started binding
    # is absent rather than relying on clients to hide fields after receipt.
    today = visit_clients.researcher_b.get("/visit-plans/today")
    assert today.status_code == 200, today.text
    today_rows = today.json()["plans"]
    assert queued["plan_id"] in {row["plan_id"] for row in today_rows}
    assert started["plan_id"] not in {row["plan_id"] for row in today_rows}
    assert all(row["status"] == "approved" for row in today_rows)
    assert all(row["session_id"] is None for row in today_rows)
    assert all(row["started_by"] is None for row in today_rows)


@pytest.mark.parametrize(("patient_id", "overrides", "reason"), [
    (
        "P-VISIT-08",
        {"week_no": 1, "phase_type": "关系建立", "event_line": "关系建立环节"},
        "独立完成合同",
    ),
    (
        "P-VISIT-09",
        {"week_no": 1, "phase_type": "前测", "event_line": "基线测评窗"},
        "前测",
    ),
    (
        "P-VISIT-10",
        {"week_no": 3, "phase_type": "正式训练", "event_line": "正式训练"},
        "冻结训练计划",
    ),
])
def test_unsupported_protocol_may_be_drafted_but_cannot_be_approved(
        visit_clients: VisitClients, patient_id: str,
        overrides: dict, reason: str):
    created = _create(
        visit_clients.researcher,
        patient_id,
        f"create-unsupported-{patient_id}",
        **overrides,
    )

    denied = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key=f"approve-unsupported-{patient_id}",
        expected_revision=created["revision"],
    )

    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "visit_plan_protocol_unavailable"
    assert reason in denied.json()["detail"]["message"]
    with Session(visit_clients.engine) as session:
        plan = session.get(VisitPlan, created["plan_id"])
        assert plan is not None
        assert plan.status == "draft"
        assert plan.revision == 1
        assert _linked_session_for_test(session, created["plan_id"]) is None


def _linked_session_for_test(session: Session, plan_id: str) -> TrainSession | None:
    return session.exec(select(TrainSession).where(
        TrainSession.visit_plan_id == plan_id,
    )).first()


def test_start_revalidates_protocol_even_for_a_legacy_approved_plan(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-11",
        "create-legacy-approved-week3",
        week_no=3,
        phase_type="正式训练",
        event_line="正式训练",
    )
    with Session(visit_clients.engine) as session:
        plan = session.get(VisitPlan, created["plan_id"])
        assert plan is not None
        plan.status = "approved"
        plan.revision = 2
        plan.approved_by = "LEGACY-IMPORT"
        plan.approved_at = datetime.now()
        session.add(plan)
        session.commit()

    # The bedside queue must apply the same current protocol gate as start;
    # imported/stale approved rows are governance facts, not startable work.
    # Withheld rows are hidden but counted, so the console can say "gate
    # re-check withheld N plans" instead of pretending nothing is scheduled.
    queue = visit_clients.researcher.get("/visit-plans/today")
    assert queue.status_code == 200, queue.text
    assert created["plan_id"] not in {
        row["plan_id"] for row in queue.json()["plans"]
    }
    assert queue.json()["withheld_count"] >= 1

    denied = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="start-legacy-approved-week3",
        expected_revision=2,
    )

    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "visit_plan_protocol_unavailable"
    with Session(visit_clients.engine) as session:
        assert _linked_session_for_test(session, created["plan_id"]) is None


def test_week_two_real_research_still_fails_closed_on_content_readiness(
        visit_clients: VisitClients):
    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-12")
        assert patient is not None
        patient.is_simulation_subject = False
        patient.proxy_consent = False
        patient.assent_obtained = False
        session.add(patient)
        session.commit()

    created = _create(
        visit_clients.researcher,
        "P-VISIT-12",
        "create-week2-real-research",
    )
    assert created["is_simulation"] is False
    assert created["data_classification"] == "research"

    denied = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="approve-week2-real-research",
        expected_revision=created["revision"],
    )

    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "visit_plan_content_unavailable"
    assert "真实研究冻结/质控门禁" in denied.json()["detail"]["message"]


def test_direct_session_creation_cannot_bypass_approved_plan(
        visit_clients: VisitClients, monkeypatch):
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    denied = visit_clients.researcher.post("/sessions", json={
        "session_id": "S-DIRECT-BYPASS",
        "patient_id": "P-VISIT-01",
        "session_sitting_no": 1,
        "training_date": TODAY.isoformat(),
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "item_bank_version_id": BANK.version_id,
        "is_simulation": True,
    })
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "direct_session_creation_disabled"
    with Session(visit_clients.engine) as session:
        assert session.get(TrainSession, "S-DIRECT-BYPASS") is None


def _orphan_session(session_id: str, *, patient_id: str,
                    visit_plan_id: str | None) -> TrainSession:
    return TrainSession(
        session_id=session_id,
        patient_id=patient_id,
        visit_plan_id=visit_plan_id,
        session_sitting_no=1,
        training_date=TODAY,
        week_no=2,
        phase_type="正式训练",
        event_line="正式训练",
        item_bank_version_id=BANK.version_id,
        is_simulation=True,
        data_classification="simulation",
        # Historical evidence remains readable only to its explicit owner; an
        # orphaned VisitPlan link is not an ownership bypass.
        trainer_id="ACTOR-visit-researcher",
    )


def _live_handshake(session_id: str) -> dict:
    return {
        "kind": "session",
        "payload": {
            "sessionId": session_id,
            "weekNo": 2,
            "eventLine": "正式训练",
            "mode": "task",
            "itemBankVersionId": BANK.version_id,
        },
    }


def _assert_visit_plan_admission_rejected(response) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == (
        "session_visit_plan_admission_required")


PLAN_SESSION_BINDING_DRIFTS = [
    pytest.param("item_bank_definition_digest", "d" * 64, id="item-digest"),
    pytest.param("autopilot_protocol_version_id", "autopilot-vX-drifted",
                 id="autopilot-version"),
    pytest.param("autopilot_protocol_definition_digest", "a" * 64,
                 id="autopilot-digest"),
    pytest.param("repeat_protocol_version_id", "repeat-intent-vX-drifted",
                 id="repeat-version"),
    pytest.param("repeat_protocol_definition_digest", "b" * 64,
                 id="repeat-digest"),
]


@pytest.mark.parametrize("column,value", PLAN_SESSION_BINDING_DRIFTS)
def test_every_frozen_binding_drift_closes_the_started_session_admission(
        visit_clients: VisitClients, monkeypatch, column, value):
    """计划与场次的任一冻结绑定漂移，人工写入口一律 409 且零写入。"""
    # The production admission must really run; the pytest escape would return
    # before any binding is compared.
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    created = _create(
        visit_clients.researcher, "P-VISIT-03",
        f"visit-create-drift-{column}"[:60],
        scheduled_time="10:15:00", queue_order=3)
    approved = _command(
        visit_clients.researcher, created["plan_id"], "approve",
        key=f"visit-approve-drift-{column}"[:60],
        expected_revision=created["revision"]).json()
    started = _command(
        visit_clients.researcher, created["plan_id"], "start",
        key=f"visit-start-drift-{column}"[:60],
        expected_revision=approved["revision"])
    assert started.status_code == 200, started.text

    with Session(visit_clients.engine) as session:
        train_session = session.exec(select(TrainSession).where(
            TrainSession.visit_plan_id == created["plan_id"])).one()
        session_id = train_session.session_id
        # Whatever the plan currently holds, the session must differ from it.
        plan = session.get(VisitPlan, created["plan_id"])
        assert getattr(plan, column) != value
        setattr(train_session, column, value)
        session.add(train_session)
        session.commit()

    before = _admission_write_set(visit_clients.engine)

    _assert_visit_plan_admission_rejected(visit_clients.researcher.post(
        f"/sessions/{session_id}/recording-authorization"))

    assert _admission_write_set(visit_clients.engine) == before


_ADMISSION_WRITE_MODELS = (
    VisitPlan,
    VisitPlanCommand,
    TrainSession,
    SessionRuntimeState,
    LiveState,
    SessionAutopilotState,
    AutopilotControlEvent,
    RuntimeCommand,
    RuntimeCommandAck,
    AttemptCaptureProcessing,
    AttemptEvent,
    InteractionEvent,
    AudioAssetRow,
    PatientDeviceCapability,
    AutopilotRepeatRequest,
    AuditLog,
)


def _admission_write_set(engine) -> dict:
    """Every column of every row of every table a manual write could move.

    Columns come from ``__table__.columns`` at call time rather than a
    hand-picked list, so a field added later is covered automatically and no
    maintained selection can silently drift and false-green. Rows sort by
    ``repr`` so ``None``, datetimes and mixed types order without raising.
    """
    snapshot: dict[str, dict] = {}
    with Session(engine) as session:
        for model in _ADMISSION_WRITE_MODELS:
            names = tuple(column.name for column in model.__table__.columns)
            rows = [tuple(getattr(row, name) for name in names)
                    for row in session.exec(select(model))]
            snapshot[model.__name__] = {
                "columns": names, "rows": sorted(rows, key=repr)}
    return snapshot


def test_a_frozen_repeat_binding_absent_from_the_registry_closes_manual_work(
        visit_clients: VisitClients, monkeypatch):
    """绑定成对且合法、但历史注册表里没有这个版本：必须稳定映射为另一个 409。

    这条覆盖 session_repeat_protocol_unavailable —— 与缺绑定不同的第二个映射。
    计划与场次两侧改成同一个 pair，所以 started admission 仍然通过，被挡住的
    只能是协议解析本身。
    """
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    created = _create(
        visit_clients.researcher, "P-VISIT-03", "visit-create-unavailable-0001",
        scheduled_time="10:15:00", queue_order=3)
    approved = _command(
        visit_clients.researcher, created["plan_id"], "approve",
        key="visit-approve-unavailable-0001",
        expected_revision=created["revision"]).json()
    started = _command(
        visit_clients.researcher, created["plan_id"], "start",
        key="visit-start-unavailable-0001",
        expected_revision=approved["revision"])
    assert started.status_code == 200, started.text

    # A constraint-valid pair that no historical registry file provides. Both
    # sides get the identical value, so plan/session admission still passes.
    absent_version = "repeat-intent-v404"
    absent_digest = "c" * 64
    with Session(visit_clients.engine) as session:
        train_session = session.exec(select(TrainSession).where(
            TrainSession.visit_plan_id == created["plan_id"])).one()
        session_id = train_session.session_id
        plan = session.get(VisitPlan, created["plan_id"])
        for row in (plan, train_session):
            row.repeat_protocol_version_id = absent_version
            row.repeat_protocol_definition_digest = absent_digest
            session.add(row)
        session.commit()

    before = _admission_write_set(visit_clients.engine)

    refused = visit_clients.researcher.post(
        f"/sessions/{session_id}/recording-authorization")

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == (
        "session_repeat_protocol_unavailable")
    assert _admission_write_set(visit_clients.engine) == before


def test_historical_orphan_sessions_are_read_only_and_cannot_reenter_runtime(
        visit_clients: VisitClients, monkeypatch):
    """NULL, missing and non-started plan links are all isolated, never grandfathered."""
    now = datetime.now()
    with Session(visit_clients.engine) as session:
        nonstarted = VisitPlan(
            plan_id="VP-LEGACY-NOT-STARTED",
            protocol_slot_key="P-VISIT-03:2:1",
            patient_id="P-VISIT-03",
            scheduled_date=TODAY,
            scheduled_time=time(11, 0),
            queue_order=11,
            session_sitting_no=1,
            week_no=2,
            phase_type="正式训练",
            event_line="正式训练",
            item_bank_version_id=BANK.version_id,
            is_simulation=True,
            data_classification="simulation",
            status="approved",
            revision=2,
            created_by="ACTOR-legacy-import",
            created_at=now,
            updated_at=now,
            approved_by="ACTOR-legacy-import",
            approved_at=now,
        )
        session.add(nonstarted)
        session.add(_orphan_session(
            "S-LEGACY-NULL", patient_id="P-VISIT-01", visit_plan_id=None))
        session.add(_orphan_session(
            "S-LEGACY-MISSING", patient_id="P-VISIT-02",
            visit_plan_id="VP-DOES-NOT-EXIST"))
        session.add(_orphan_session(
            "S-LEGACY-NOT-STARTED", patient_id="P-VISIT-03",
            visit_plan_id=nonstarted.plan_id))
        session.commit()

    # Disable the explicit pytest-only fixture escape to exercise deployment rules.
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    for session_id in (
            "S-LEGACY-NULL", "S-LEGACY-MISSING", "S-LEGACY-NOT-STARTED"):
        _assert_visit_plan_admission_rejected(
            visit_clients.researcher.put(
                "/live/state", json=_live_handshake(session_id)))
        _assert_visit_plan_admission_rejected(
            visit_clients.researcher.get(f"/sessions/{session_id}/runtime"))
        _assert_visit_plan_admission_rejected(
            visit_clients.researcher.get(f"/sessions/{session_id}/plan"))
        _assert_visit_plan_admission_rejected(
            visit_clients.researcher.get(
                f"/sessions/{session_id}/autopilot/status"))

    # Runtime writes and microphone admission are fenced by the same boundary.
    _assert_visit_plan_admission_rejected(visit_clients.researcher.put(
        "/sessions/S-LEGACY-NULL/runtime/cursor",
        json={
            "screen": "present", "itemIdx": 0, "turnIdx": 0,
            "responseRole": "命名", "recording": "idle",
        },
    ))
    _assert_visit_plan_admission_rejected(visit_clients.researcher.post(
        "/sessions/S-LEGACY-NULL/recording-authorization"))

    # Isolation is not deletion: account research review can still enumerate and
    # inspect the legacy facts without reopening a live/control channel.
    assert visit_clients.researcher.get(
        "/sessions/S-LEGACY-NULL").status_code == 200
    listed = visit_clients.researcher.get(
        "/patients/P-VISIT-01/sessions")
    assert listed.status_code == 200, listed.text
    assert {row["session_id"] for row in listed.json()} == {"S-LEGACY-NULL"}
    assert visit_clients.researcher.get(
        "/sessions/S-LEGACY-NULL/journal").status_code == 200


def test_started_visit_plan_session_remains_admitted_to_live_runtime(
        visit_clients: VisitClients, monkeypatch):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-04",
        "visit-create-admission-mainline-0001",
    )
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-admission-mainline-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()
    started_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-start-admission-mainline-0001",
        expected_revision=approved["revision"],
    )
    assert started_response.status_code == 200, started_response.text
    started = started_response.json()

    # Starting returns a plan transition receipt.  The browser then recovers
    # the server-created Session through this separate HTTP contract before it
    # may enter bedside runtime.  Keep the complete, exact payload explicit so
    # a strict cross-client decoder cannot silently drift from the API model.
    session_response = visit_clients.researcher.get(
        f"/sessions/{started['session_id']}")
    assert session_response.status_code == 200, session_response.text
    session_payload = session_response.json()
    assert set(session_payload) == STARTED_SESSION_KEYS
    assert session_payload == {
        "session_id": started["session_id"],
        "patient_id": "P-VISIT-04",
        "visit_plan_id": created["plan_id"],
        "session_sitting_no": created["session_sitting_no"],
        "training_date": TODAY.isoformat(),
        "week_no": created["week_no"],
        "phase_type": created["phase_type"],
        "event_line": created["event_line"],
        "trainer_id": started["started_by"],
        "item_bank_version_id": BANK.version_id,
        "item_bank_definition_digest": content.item_bank_definition_digest(BANK),
        "autopilot_protocol_version_id": PROTOCOL["protocol_version_id"],
        "autopilot_protocol_definition_digest": (
            content.autopilot_protocol_definition_digest(PROTOCOL)),
        "repeat_protocol_version_id": REPEAT_PROTOCOL.version_id,
        "repeat_protocol_definition_digest": (
            REPEAT_PROTOCOL.definition_digest),
        # A canonical start is paired-null on both sides, item by item.
        "autopilot_profile_version_id": None,
        "autopilot_profile_definition_digest": None,
        "is_simulation": True,
        "data_classification": "simulation",
    }
    assert created["autopilot_profile_version_id"] is None

    # The real VisitPlan vertical must pass even with all direct-session test
    # escapes disabled.
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    handshake = visit_clients.researcher.put(
        "/live/state", json=_live_handshake(started["session_id"]))
    assert handshake.status_code == 200, handshake.text
    runtime_response = visit_clients.researcher.get(
        f"/sessions/{started['session_id']}/runtime")
    assert runtime_response.status_code == 200, runtime_response.text
    assert runtime_response.json()["status"] == "active"


def _assert_iso_datetime(value: object) -> None:
    assert isinstance(value, str) and value
    datetime.fromisoformat(value.replace("Z", "+00:00"))


def _assert_receipt(
        payload: dict,
        *,
        status: str,
        revision: int,
        patient_id: str,
        approved_fact: bool | None = None,
) -> None:
    # Exact keys keep names, notes, free text, and other unnecessary identifiers
    # out of the scheduling/queue response contract.
    assert set(payload) == RECEIPT_KEYS
    assert payload["patient_id"] == patient_id
    assert payload["status"] == status
    assert payload["revision"] == revision
    assert isinstance(payload["plan_id"], str) and payload["plan_id"]
    assert payload["plan_id"] != patient_id
    assert payload["item_bank_version_id"] == BANK.version_id
    assert payload["is_simulation"] is True
    assert payload["data_classification"] == "simulation"
    assert payload["week_no"] == 2
    assert payload["phase_type"] == "正式训练"
    assert payload["event_line"] == "正式训练"
    assert payload["session_sitting_no"] >= 1
    assert payload["created_by"] in {
        "ACTOR-visit-researcher", "ACTOR-visit-admin"}
    _assert_iso_datetime(payload["created_at"])

    reached = {
        "approved": (
            status in {"approved", "started"}
            if approved_fact is None else approved_fact
        ),
        "started": status == "started",
        "cancelled": status == "cancelled",
    }
    for event, present in reached.items():
        actor = payload[f"{event}_by"]
        occurred_at = payload[f"{event}_at"]
        if present:
            assert actor in {"ACTOR-visit-researcher", "ACTOR-visit-admin"}
            _assert_iso_datetime(occurred_at)
        else:
            assert actor is None
            assert occurred_at is None
    if status == "started":
        assert isinstance(payload["session_id"], str) and payload["session_id"]
        assert payload["session_id"] != payload["plan_id"]
    else:
        assert payload["session_id"] is None


def test_create_derives_server_owned_fields_and_is_exactly_idempotent(
        visit_clients: VisitClients):
    body = _plan_body(
        "P-VISIT-01",
        "visit-create-contract-0001",
        scheduled_time="08:45:00",
        queue_order=7,
        session_sitting_no=2,
    )
    first = visit_clients.researcher.post("/visit-plans", json=body)
    assert first.status_code == 200, first.text
    payload = first.json()
    _assert_receipt(
        payload, status="draft", revision=1, patient_id="P-VISIT-01")
    assert payload["scheduled_date"] == TODAY.isoformat()
    assert time.fromisoformat(payload["scheduled_time"]) == time(8, 45)
    assert payload["queue_order"] == 7
    assert payload["session_sitting_no"] == 2

    replay = visit_clients.researcher.post("/visit-plans", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == payload

    conflict = visit_clients.researcher.post("/visit-plans", json={
        **body,
        "scheduled_time": "08:50:00",
    })
    assert conflict.status_code == 409, conflict.text


@pytest.mark.parametrize(("client_owned_field", "client_value"), [
    ("plan_id", "vp-client-controlled"),
    ("session_id", "S-client-controlled"),
    ("item_bank_version_id", BANK.version_id),
    ("is_simulation", True),
    ("data_classification", "simulation"),
    ("status", "draft"),
    ("revision", 99),
    ("created_by", "ACTOR-client-controlled"),
    ("patient_name", "client-controlled"),
    ("notes", "client-controlled"),
])
def test_create_forbids_client_owned_or_free_text_fields(
        visit_clients: VisitClients,
        client_owned_field: str,
        client_value: object):
    body = _plan_body(
        "P-VISIT-02",
        f"visit-extra-{client_owned_field.replace('_', '-')}-0001",
    )
    body[client_owned_field] = client_value
    denied = visit_clients.researcher.post("/visit-plans", json=body)
    assert denied.status_code == 422, denied.text


def test_approve_today_queue_and_start_form_one_atomic_idempotent_vertical(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-03",
        "visit-create-vertical-0001",
        scheduled_time="10:15:00",
        queue_order=3,
    )
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-vertical-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()
    _assert_receipt(
        approved, status="approved", revision=2, patient_id="P-VISIT-03")

    approve_replay = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-vertical-0001",
        expected_revision=created["revision"],
    )
    assert approve_replay.status_code == 200, approve_replay.text
    assert approve_replay.json() == approved

    today = visit_clients.researcher.get("/visit-plans/today")
    assert today.status_code == 200, today.text
    assert today.headers["cache-control"] == "private, no-store"
    assert today.headers["pragma"] == "no-cache"
    assert set(today.json()) == {"as_of_date", "plans", "withheld_count"}
    assert today.json()["as_of_date"] == TODAY.isoformat()
    assert today.json()["withheld_count"] == 0
    assert today.json()["plans"] == [approved]

    started_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-start-vertical-0001",
        expected_revision=approved["revision"],
    )
    assert started_response.status_code == 200, started_response.text
    started = started_response.json()
    _assert_receipt(
        started, status="started", revision=3, patient_id="P-VISIT-03")

    started_replay = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-start-vertical-0001",
        expected_revision=approved["revision"],
    )
    assert started_replay.status_code == 200, started_replay.text
    assert started_replay.json() == started

    with Session(visit_clients.engine) as session:
        sessions = list(session.exec(select(TrainSession)))
        runtimes = list(session.exec(select(SessionRuntimeState)))
        plan = session.get(VisitPlan, created["plan_id"])
        assert len(sessions) == 1
        assert len(runtimes) == 1
        assert plan is not None
        train_session = sessions[0]
        runtime = runtimes[0]
        assert train_session.session_id == started["session_id"]
        assert train_session.patient_id == "P-VISIT-03"
        assert train_session.training_date == TODAY
        assert train_session.session_sitting_no == 1
        assert train_session.item_bank_version_id == BANK.version_id
        assert plan.item_bank_definition_digest == (
            content.item_bank_definition_digest(BANK))
        assert plan.autopilot_protocol_version_id == (
            PROTOCOL["protocol_version_id"])
        assert plan.autopilot_protocol_definition_digest == (
            content.autopilot_protocol_definition_digest(PROTOCOL))
        assert train_session.item_bank_definition_digest == (
            plan.item_bank_definition_digest)
        assert train_session.autopilot_protocol_version_id == (
            plan.autopilot_protocol_version_id)
        assert train_session.autopilot_protocol_definition_digest == (
            plan.autopilot_protocol_definition_digest)
        assert plan.autopilot_profile_version_id is None
        assert plan.autopilot_profile_definition_digest is None
        assert train_session.autopilot_profile_version_id is None
        assert train_session.autopilot_profile_definition_digest is None
        assert train_session.is_simulation is True
        assert train_session.data_classification == "simulation"
        assert getattr(train_session, "visit_plan_id") == created["plan_id"]
        assert runtime.session_id == started["session_id"]
        assert (runtime.status, runtime.revision) == ("active", 0)

        visit_audits = list(session.exec(select(AuditLog).where(
            AuditLog.action.in_({
                "visit_plan_create",
                "visit_plan_approve",
                "visit_plan_start",
            }),
            AuditLog.patient_id == "P-VISIT-03",
        )))
        # Exact command replays are themselves visible audit reads of the
        # authoritative receipt, so this flow records create + two approve +
        # two start entries.  Every projection must still omit the opaque key.
        assert len(visit_audits) == 5
        assert all(created["plan_id"] not in row.summary
                   for row in visit_audits)
        assert all("plan=" not in row.summary for row in visit_audits)
        assert {row.action for row in visit_audits} == {
            "visit_plan_create",
            "visit_plan_approve",
            "visit_plan_start",
        }

    after_start = visit_clients.researcher.get("/visit-plans/today")
    assert after_start.status_code == 200, after_start.text
    assert after_start.json()["plans"] == []


def test_abort_preserves_started_visit_slot_and_never_auto_reschedules(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-12",
        "create-abort-slot-preserved-0001",
    )
    approved = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="approve-abort-slot-preserved-0001",
        expected_revision=created["revision"],
    )
    assert approved.status_code == 200, approved.text
    started = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="start-abort-slot-preserved-0001",
        expected_revision=approved.json()["revision"],
    )
    assert started.status_code == 200, started.text
    receipt = started.json()

    with Session(visit_clients.engine) as session:
        before = session.get(VisitPlan, created["plan_id"])
        assert before is not None
        slot_key = before.protocol_slot_key
        linked = _linked_session_for_test(session, created["plan_id"])
        assert linked is not None
        linked_session_id = linked.session_id

    aborted = visit_clients.researcher.post(
        f"/sessions/{receipt['session_id']}/abort",
        json={
            "reason_code": "clinical_safety",
            "expected_revision": 0,
            "idempotency_key": "abort-visit-slot-clinical-safety-0001",
        },
    )
    assert aborted.status_code == 200, aborted.text
    assert aborted.json()["status"] == "aborted"

    with Session(visit_clients.engine) as session:
        after = session.get(VisitPlan, created["plan_id"])
        assert after is not None
        assert after.status == "started"
        assert after.protocol_slot_key == slot_key
        linked = _linked_session_for_test(session, created["plan_id"])
        assert linked is not None
        assert linked.session_id == linked_session_id == receipt["session_id"]

    replacement = visit_clients.researcher.post(
        "/visit-plans",
        json=_plan_body(
            "P-VISIT-12",
            "create-abort-slot-replacement-denied-0001",
        ),
    )
    assert replacement.status_code == 409, replacement.text


def test_concurrent_abort_cas_commits_one_reason_across_independent_sessions(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-11",
        "create-concurrent-abort-0001",
    )
    approved = _command(
        visit_clients.researcher, created["plan_id"], "approve",
        key="approve-concurrent-abort-0001",
        expected_revision=created["revision"],
    ).json()
    started_response = _command(
        visit_clients.researcher, created["plan_id"], "start",
        key="start-concurrent-abort-0001",
        expected_revision=approved["revision"],
    )
    assert started_response.status_code == 200, started_response.text
    session_id = started_response.json()["session_id"]

    peer = _login("visit-researcher")
    barrier = Barrier(2)

    def submit(client: TestClient, reason_code: str, key: str):
        barrier.wait()
        return client.post(f"/sessions/{session_id}/abort", json={
            "reason_code": reason_code,
            "expected_revision": 0,
            "idempotency_key": key,
        })

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    submit, visit_clients.researcher, "technical_failure",
                    "abort-concurrent-technical-failure-0001"),
                pool.submit(
                    submit, peer, "researcher_decision",
                    "abort-concurrent-researcher-decision-0002"),
            ]
            responses = [future.result(timeout=10) for future in futures]
    finally:
        peer.close()

    assert sorted(response.status_code for response in responses) == [200, 409]
    loser = next(response for response in responses if response.status_code == 409)
    assert loser.json()["detail"]["code"] == "session_already_aborted"
    winner = next(response for response in responses if response.status_code == 200)
    authoritative = visit_clients.researcher.get(
        f"/sessions/{session_id}/runtime")
    assert authoritative.status_code == 200
    assert authoritative.json()["endReason"] == winner.json()["endReason"]
    assert authoritative.json()["revision"] == 1

    with Session(visit_clients.engine) as session:
        audit_rows = list(session.exec(select(AuditLog).where(
            AuditLog.action == "session_abort",
            AuditLog.session_id == session_id,
        )))
    assert len(audit_rows) == 1


def test_researcher_cannot_switch_participant_before_safe_closeout(
        visit_clients: VisitClients):
    first = _create(
        visit_clients.researcher,
        "P-VISIT-04",
        "visit-switch-create-first-0001",
    )
    first_approved = _command(
        visit_clients.researcher, first["plan_id"], "approve",
        key="visit-switch-approve-first-0001",
        expected_revision=first["revision"],
    ).json()
    first_started = _command(
        visit_clients.researcher, first["plan_id"], "start",
        key="visit-switch-start-first-0001",
        expected_revision=first_approved["revision"],
    ).json()

    second = _create(
        visit_clients.researcher,
        "P-VISIT-05",
        "visit-switch-create-second-0001",
    )
    second_approved = _command(
        visit_clients.researcher, second["plan_id"], "approve",
        key="visit-switch-approve-second-0001",
        expected_revision=second["revision"],
    ).json()
    start_body = {
        "idempotency_key": "visit-switch-start-second-0001",
        "expected_revision": second_approved["revision"],
    }

    active_denied = visit_clients.researcher.post(
        f"/visit-plans/{second['plan_id']}/start", json=start_body)
    assert active_denied.status_code == 409
    assert active_denied.json()["detail"]["code"] == "visit_plan_actor_session_open"

    with Session(visit_clients.engine) as session:
        runtime = session.get(SessionRuntimeState, first_started["session_id"])
        runtime.status = "intervention_completed"
        runtime.revision += 1
        runtime.intervention_completed_at = datetime.now()
        session.add(runtime)
        session.commit()

    closeout_denied = visit_clients.researcher.post(
        f"/visit-plans/{second['plan_id']}/start", json=start_body)
    assert closeout_denied.status_code == 409
    assert closeout_denied.json()["detail"]["code"] == "visit_plan_actor_closeout_required"

    with Session(visit_clients.engine) as session:
        session.add(SessionCloseoutReport(
            session_id=first_started["session_id"],
            schema_version="session-closeout.v1",
            status="no_additional_observation",
            revision=1,
            last_idempotency_key="visit-switch-closeout-0001",
            last_request_hash="a" * 64,
            created_by="ACTOR-visit-researcher",
            updated_by="ACTOR-visit-researcher",
        ))
        session.commit()

    allowed = visit_clients.researcher.post(
        f"/visit-plans/{second['plan_id']}/start", json=start_body)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "started"


def test_different_actor_cannot_restart_patient_before_safe_closeout(
        visit_clients: VisitClients):
    first = _create(
        visit_clients.researcher,
        "P-VISIT-04",
        "visit-patient-closeout-create-first-0001",
        session_sitting_no=1,
    )
    first_approved = _command(
        visit_clients.researcher,
        first["plan_id"],
        "approve",
        key="visit-patient-closeout-approve-first-0001",
        expected_revision=first["revision"],
    ).json()
    first_started_response = _command(
        visit_clients.researcher,
        first["plan_id"],
        "start",
        key="visit-patient-closeout-start-first-0001",
        expected_revision=first_approved["revision"],
    )
    assert first_started_response.status_code == 200, first_started_response.text
    first_started = first_started_response.json()

    second = _create(
        visit_clients.researcher_b,
        "P-VISIT-04",
        "visit-patient-closeout-create-second-0001",
        session_sitting_no=2,
    )
    second_approved_response = _command(
        visit_clients.researcher_b,
        second["plan_id"],
        "approve",
        key="visit-patient-closeout-approve-second-0001",
        expected_revision=second["revision"],
    )
    assert second_approved_response.status_code == 200, second_approved_response.text
    second_approved = second_approved_response.json()

    with Session(visit_clients.engine) as session:
        runtime = session.get(
            SessionRuntimeState, first_started["session_id"])
        assert runtime is not None
        runtime.status = "intervention_completed"
        runtime.revision += 1
        runtime.intervention_completed_at = datetime.now()
        session.add(runtime)
        session.commit()

    start_body = {
        "idempotency_key": "visit-patient-closeout-start-second-0001",
        "expected_revision": second_approved["revision"],
    }
    denied = visit_clients.researcher_b.post(
        f"/visit-plans/{second['plan_id']}/start", json=start_body)
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == (
        "visit_plan_patient_closeout_required")

    with Session(visit_clients.engine) as session:
        session.add(SessionCloseoutReport(
            session_id=first_started["session_id"],
            schema_version="session-closeout.v1",
            status="no_additional_observation",
            revision=1,
            last_idempotency_key="visit-patient-closeout-report-0001",
            last_request_hash="c" * 64,
            created_by="ACTOR-visit-researcher",
            updated_by="ACTOR-visit-researcher",
        ))
        session.commit()

    allowed = visit_clients.researcher_b.post(
        f"/visit-plans/{second['plan_id']}/start", json=start_body)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "started"


def test_concurrent_start_for_same_actor_creates_only_one_active_session(
        visit_clients: VisitClients, monkeypatch):
    """Different plan rows cannot write-skew the actor active-work predicate.

    Disable the legacy process-wide VisitPlan lock to model requests arriving
    in distinct workers.  The subject/actor governance fence remains the
    authority and must make the second request observe the first active session.
    """
    plans: list[dict] = []
    for index, patient_id in enumerate(("P-VISIT-06", "P-VISIT-07"), 1):
        created = _create(
            visit_clients.researcher,
            patient_id,
            f"visit-actor-race-create-{index:04d}",
        )
        approved_response = _command(
            visit_clients.researcher,
            created["plan_id"],
            "approve",
            key=f"visit-actor-race-approve-{index:04d}",
            expected_revision=created["revision"],
        )
        assert approved_response.status_code == 200, approved_response.text
        plans.append(approved_response.json())

    import app.main as main_mod
    monkeypatch.setattr(main_mod, "_VISIT_PLAN_WRITE_LOCK", nullcontext())
    peer = _login("visit-researcher")
    barrier = Barrier(2)

    def start(client: TestClient, plan: dict, index: int):
        barrier.wait()
        return _command(
            client,
            plan["plan_id"],
            "start",
            key=f"visit-actor-race-start-{index:04d}",
            expected_revision=plan["revision"],
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(start, visit_clients.researcher, plans[0], 1),
                pool.submit(start, peer, plans[1], 2),
            ]
            responses = [future.result(timeout=10) for future in futures]
    finally:
        peer.close()

    assert sorted(response.status_code for response in responses) == [200, 409]
    loser = next(response for response in responses if response.status_code == 409)
    assert loser.json()["detail"]["code"] == "visit_plan_actor_session_open"
    with Session(visit_clients.engine) as session:
        sessions = list(session.exec(select(TrainSession)))
        assert len(sessions) == 1
        assert sessions[0].trainer_id == "ACTOR-visit-researcher"
        stored_plans = [session.get(VisitPlan, plan["plan_id"]) for plan in plans]
        assert sorted(plan.status for plan in stored_plans if plan is not None) == [
            "approved", "started"]


def test_training_start_rejects_actor_with_open_formal_assessment(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-09",
        "visit-assessment-gate-create-0001",
    )
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-assessment-gate-approve-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()
    with Session(visit_clients.engine) as session:
        session.add(AssessmentEvent(
            event_id="assessment-open-for-visit-gate",
            patient_id="P-VISIT-08",
            assigned_assessor_id="ACTOR-visit-researcher",
            timepoint="pretest",
            scheduled_date=TODAY,
            status="in_progress",
            revision=2,
            is_simulation=True,
            data_classification="simulation",
            formal_outcome_eligible=False,
            definition_bundle_id="assessment-bundle-test-v1",
            definition_bundle_digest="sha256:" + "a" * 64,
            active_protocol_slot_key="b" * 64,
            created_by="ACTOR-visit-researcher",
            started_at=datetime.now(),
        ))
        session.commit()

    denied = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-assessment-gate-start-0001",
        expected_revision=approved["revision"],
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == (
        "visit_plan_actor_assessment_open")
    with Session(visit_clients.engine) as session:
        assert _linked_session_for_test(session, created["plan_id"]) is None
        withdrawn_patient = session.get(Patient, "P-VISIT-08")
        assert withdrawn_patient is not None
        withdrawn_patient.withdrawal_status = "withdrawn"
        session.add(withdrawn_patient)
        session.commit()

    # Withdrawal freezes the historical in-progress event rather than forging
    # completion, but it releases the assessor's current-work slot.
    allowed = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-assessment-gate-start-0001",
        expected_revision=approved["revision"],
    )
    assert allowed.status_code == 200, allowed.text
    with Session(visit_clients.engine) as session:
        frozen = session.get(
            AssessmentEvent, "assessment-open-for-visit-gate")
        assert frozen is not None and frozen.status == "in_progress"


def test_training_start_rejects_patient_in_assessment_owned_by_another_actor(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-09",
        "visit-patient-assessment-gate-create-0001",
    )
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-patient-assessment-gate-approve-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()
    with Session(visit_clients.engine) as session:
        session.add(AssessmentEvent(
            event_id="assessment-open-for-patient-gate",
            patient_id="P-VISIT-09",
            assigned_assessor_id="ACTOR-another-researcher",
            timepoint="pretest",
            scheduled_date=TODAY - timedelta(days=1),
            status="awaiting_closeout",
            revision=4,
            is_simulation=True,
            data_classification="simulation",
            formal_outcome_eligible=False,
            definition_bundle_id="assessment-bundle-test-v1",
            definition_bundle_digest="sha256:" + "c" * 64,
            active_protocol_slot_key="d" * 64,
            created_by="ACTOR-another-researcher",
            started_at=datetime.now(),
        ))
        session.commit()

    denied = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-patient-assessment-gate-start-0001",
        expected_revision=approved["revision"],
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == (
        "visit_plan_patient_assessment_open")
    with Session(visit_clients.engine) as session:
        assert _linked_session_for_test(session, created["plan_id"]) is None


def test_consent_denied_assessment_releases_assessor_current_work_slot(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-10",
        "visit-consent-sealed-assessment-create-0001",
    )
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-consent-sealed-assessment-approve-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()
    with Session(visit_clients.engine) as session:
        sealed_patient = session.get(Patient, "P-VISIT-08")
        assert sealed_patient is not None
        sealed_patient.consent_status = "denied"
        session.add(sealed_patient)
        session.add(AssessmentEvent(
            event_id="assessment-sealed-consent-actor-slot",
            patient_id="P-VISIT-08",
            assigned_assessor_id="ACTOR-visit-researcher",
            timepoint="pretest",
            scheduled_date=TODAY,
            status="in_progress",
            revision=2,
            is_simulation=True,
            data_classification="simulation",
            formal_outcome_eligible=False,
            definition_bundle_id="assessment-bundle-test-v1",
            definition_bundle_digest="sha256:" + "e" * 64,
            active_protocol_slot_key="f" * 64,
            created_by="ACTOR-visit-researcher",
            started_at=datetime.now(),
        ))
        session.commit()

    allowed = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-consent-sealed-assessment-start-0001",
        expected_revision=approved["revision"],
    )
    assert allowed.status_code == 200, allowed.text


def test_revision_conflicts_cancel_replay_and_terminal_state_do_not_create_session(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-04",
        "visit-create-cancel-0001",
    )
    stale_approve = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-stale-0001",
        expected_revision=0,
    )
    assert stale_approve.status_code == 409, stale_approve.text

    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-cancel-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()

    duplicate_transition = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-second-0001",
        expected_revision=created["revision"],
    )
    assert duplicate_transition.status_code == 409, duplicate_transition.text

    stale_start = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-start-stale-0001",
        expected_revision=created["revision"],
    )
    assert stale_start.status_code == 409, stale_start.text

    cancelled_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "cancel",
        key="visit-cancel-contract-0001",
        expected_revision=approved["revision"],
        reason_code="schedule_changed",
    )
    assert cancelled_response.status_code == 200, cancelled_response.text
    cancelled = cancelled_response.json()
    _assert_receipt(
        cancelled,
        status="cancelled",
        revision=3,
        patient_id="P-VISIT-04",
        approved_fact=True,
    )

    cancel_replay = _command(
        visit_clients.researcher,
        created["plan_id"],
        "cancel",
        key="visit-cancel-contract-0001",
        expected_revision=approved["revision"],
        reason_code="schedule_changed",
    )
    assert cancel_replay.status_code == 200, cancel_replay.text
    assert cancel_replay.json() == cancelled

    after_cancel = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-start-cancelled-0001",
        expected_revision=cancelled["revision"],
    )
    assert after_cancel.status_code == 409, after_cancel.text
    with Session(visit_clients.engine) as session:
        assert list(session.exec(select(TrainSession))) == []
        assert list(session.exec(select(SessionRuntimeState))) == []


def test_cancel_reason_is_controlled_and_rejects_free_text(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-05",
        "visit-create-reason-0001",
    )
    free_text = _command(
        visit_clients.researcher,
        created["plan_id"],
        "cancel",
        key="visit-cancel-free-text-0001",
        expected_revision=created["revision"],
        reason_code="老人说今天不想来了",
    )
    assert free_text.status_code == 422, free_text.text
    extra_note = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/cancel",
        json={
            "idempotency_key": "visit-cancel-note-0001",
            "expected_revision": created["revision"],
            "reason_code": "schedule_changed",
            "note": "free text must not enter this command",
        },
    )
    assert extra_note.status_code == 422, extra_note.text


def test_today_queue_contains_only_approved_today_or_overdue_plans(
        visit_clients: VisitClients):
    approved_ids: set[str] = set()
    for patient_id, key, scheduled_date, queue_order in (
        ("P-VISIT-06", "visit-create-overdue-0001", TODAY - timedelta(days=1), 5),
        ("P-VISIT-07", "visit-create-today-0001", TODAY, 1),
    ):
        created = _create(
            visit_clients.researcher,
            patient_id,
            key,
            scheduled_date=scheduled_date,
            queue_order=queue_order,
        )
        approved = _command(
            visit_clients.researcher,
            created["plan_id"],
            "approve",
            key=key.replace("create", "approve"),
            expected_revision=created["revision"],
        )
        assert approved.status_code == 200, approved.text
        approved_ids.add(created["plan_id"])

    future = _create(
        visit_clients.researcher,
        "P-VISIT-08",
        "visit-create-future-0001",
        scheduled_date=TODAY + timedelta(days=1),
    )
    future_approved = _command(
        visit_clients.researcher,
        future["plan_id"],
        "approve",
        key="visit-approve-future-0001",
        expected_revision=future["revision"],
    )
    assert future_approved.status_code == 200, future_approved.text

    _create(
        visit_clients.researcher,
        "P-VISIT-09",
        "visit-create-draft-0001",
    )
    cancelled = _create(
        visit_clients.researcher,
        "P-VISIT-10",
        "visit-create-queue-cancel-0001",
    )
    cancelled_response = _command(
        visit_clients.researcher,
        cancelled["plan_id"],
        "cancel",
        key="visit-cancel-queue-0001",
        expected_revision=cancelled["revision"],
        reason_code="schedule_changed",
    )
    assert cancelled_response.status_code == 200, cancelled_response.text

    response = visit_clients.researcher.get("/visit-plans/today")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"
    payload = response.json()
    assert set(payload) == {"as_of_date", "plans", "withheld_count"}
    assert payload["as_of_date"] == TODAY.isoformat()
    assert payload["withheld_count"] == 0
    assert {plan["plan_id"] for plan in payload["plans"]} == approved_ids
    assert all(plan["status"] == "approved" for plan in payload["plans"])
    assert all(date.fromisoformat(plan["scheduled_date"]) <= TODAY
               for plan in payload["plans"])
    for plan in payload["plans"]:
        _assert_receipt(
            plan,
            status="approved",
            revision=2,
            patient_id=plan["patient_id"],
        )


def test_approve_and_start_revalidate_withdrawal_without_partial_mutation(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-11",
        "visit-create-withdrawal-0001",
    )
    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-11")
        assert patient is not None
        patient.withdrawal_status = "withdrawal_requested"
        session.add(patient)
        session.commit()

    denied_approve = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-withdrawn-0001",
        expected_revision=created["revision"],
    )
    assert denied_approve.status_code == 409, denied_approve.text

    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-11")
        assert patient is not None
        patient.withdrawal_status = None
        session.add(patient)
        session.commit()
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-after-restore-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()

    due_before_withdrawal = visit_clients.researcher.get("/visit-plans/today")
    assert due_before_withdrawal.status_code == 200, due_before_withdrawal.text
    assert created["plan_id"] in {
        row["plan_id"] for row in due_before_withdrawal.json()["plans"]
    }

    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-11")
        assert patient is not None
        patient.withdrawal_status = "withdrawn"
        session.add(patient)
        session.commit()
    due_after_withdrawal = visit_clients.researcher.get("/visit-plans/today")
    assert due_after_withdrawal.status_code == 200, due_after_withdrawal.text
    assert created["plan_id"] not in {
        row["plan_id"] for row in due_after_withdrawal.json()["plans"]
    }
    denied_start = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-start-withdrawn-0001",
        expected_revision=approved["revision"],
    )
    assert denied_start.status_code == 409, denied_start.text
    with Session(visit_clients.engine) as session:
        assert list(session.exec(select(TrainSession))) == []
        assert list(session.exec(select(SessionRuntimeState))) == []


def test_approve_and_start_revalidate_current_item_bank_version(
        visit_clients: VisitClients, monkeypatch):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-12",
        "visit-create-bank-drift-0001",
    )
    original_loader = content.load_item_bank
    drifted = replace(BANK, version_id="wk2-stale-test-version")
    monkeypatch.setattr(content, "load_item_bank", lambda _path: drifted)
    denied_approve = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-bank-drift-0001",
        expected_revision=created["revision"],
    )
    assert denied_approve.status_code == 409, denied_approve.text

    monkeypatch.setattr(content, "load_item_bank", original_loader)
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-bank-restored-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()

    monkeypatch.setattr(content, "load_item_bank", lambda _path: drifted)
    denied_start = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-start-bank-drift-0001",
        expected_revision=approved["revision"],
    )
    assert denied_start.status_code == 409, denied_start.text
    with Session(visit_clients.engine) as session:
        assert list(session.exec(select(TrainSession))) == []
        assert list(session.exec(select(SessionRuntimeState))) == []


def test_same_version_definition_drift_rejects_approve_start_and_replay(
        visit_clients: VisitClients, monkeypatch):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-10",
        "visit-create-definition-drift-0001",
    )
    drifted_bank = copy.deepcopy(BANK)
    drifted_bank.single_element[0]["initial_prompt"] += "（未升版修改）"
    assert drifted_bank.version_id == BANK.version_id
    monkeypatch.setattr(
        content, "load_item_bank", lambda _path: drifted_bank)
    denied_approve = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-definition-drift-0001",
        expected_revision=created["revision"],
    )
    assert denied_approve.status_code == 409, denied_approve.text
    assert denied_approve.json()["detail"]["code"] == (
        "visit_plan_content_digest_mismatch")

    monkeypatch.setattr(content, "load_item_bank", lambda _path: BANK)
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-definition-restored-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()

    drifted_protocol = copy.deepcopy(PROTOCOL)
    drifted_protocol["naming"]["success_after_cue2"] += "（未升版修改）"
    assert drifted_protocol["protocol_version_id"] == (
        PROTOCOL["protocol_version_id"])
    monkeypatch.setattr(
        content, "load_autopilot_protocol", lambda _path: drifted_protocol)
    denied_start = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-start-protocol-drift-0001",
        expected_revision=approved["revision"],
    )
    assert denied_start.status_code == 409, denied_start.text
    assert denied_start.json()["detail"]["code"] == (
        "visit_plan_protocol_digest_mismatch")

    # Even a previously successful approve receipt cannot be replayed after the
    # definition drifts; idempotency never turns stale content into authority.
    denied_replay = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-definition-restored-0001",
        expected_revision=created["revision"],
    )
    assert denied_replay.status_code == 409, denied_replay.text
    assert denied_replay.json()["detail"]["code"] == (
        "visit_plan_protocol_digest_mismatch")
    with Session(visit_clients.engine) as session:
        assert _linked_session_for_test(session, created["plan_id"]) is None


def test_delivery_manifest_rebinding_rejects_old_visit_plan(
        visit_clients: VisitClients, monkeypatch):
    """Changing private image bytes requires a new bound item-bank definition."""
    created = _create(
        visit_clients.researcher,
        "P-VISIT-10",
        "visit-create-asset-binding-drift-0001",
    )
    rebound_bank = copy.deepcopy(BANK)
    rebound_bank.meta["draft_revision"] = "2026-07-20.5-test"
    rebound_bank.meta["patient_asset_delivery_manifest"] = {
        "version_id": "wk2-private-webp-20260720.3",
        "definition_sha256": "a" * 64,
    }
    assert rebound_bank.version_id == BANK.version_id
    assert content.item_bank_definition_digest(rebound_bank) != (
        content.item_bank_definition_digest(BANK))
    monkeypatch.setattr(
        content, "load_item_bank", lambda _path: rebound_bank)

    denied = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-asset-binding-drift-0001",
        expected_revision=created["revision"],
    )

    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == (
        "visit_plan_content_digest_mismatch")
    with Session(visit_clients.engine) as session:
        assert _linked_session_for_test(session, created["plan_id"]) is None


def test_legacy_plan_without_definition_binding_cannot_be_approved(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-09",
        "visit-create-legacy-binding-0001",
    )
    with Session(visit_clients.engine) as session:
        plan = session.get(VisitPlan, created["plan_id"])
        assert plan is not None
        plan.item_bank_definition_digest = None
        plan.autopilot_protocol_version_id = None
        plan.autopilot_protocol_definition_digest = None
        session.add(plan)
        session.commit()

    denied = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-approve-legacy-binding-0001",
        expected_revision=created["revision"],
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == (
        "visit_plan_definition_binding_missing")


def test_visit_plan_write_roles_are_researcher_or_admin_only(
        visit_clients: VisitClients):
    anonymous_read = visit_clients.anonymous.get("/visit-plans/today")
    assert anonymous_read.status_code == 401, anonymous_read.text
    anonymous_write = visit_clients.anonymous.post(
        "/visit-plans",
        json=_plan_body("P-VISIT-01", "visit-anonymous-create-0001"),
    )
    assert anonymous_write.status_code == 401, anonymous_write.text

    steward_create = visit_clients.steward.post(
        "/visit-plans",
        json=_plan_body("P-VISIT-01", "visit-steward-create-0001"),
    )
    assert steward_create.status_code == 403, steward_create.text

    researcher_plan = _create(
        visit_clients.researcher,
        "P-VISIT-01",
        "visit-researcher-role-0001",
    )
    steward_approve = _command(
        visit_clients.steward,
        researcher_plan["plan_id"],
        "approve",
        key="visit-steward-approve-0001",
        expected_revision=researcher_plan["revision"],
    )
    assert steward_approve.status_code == 403, steward_approve.text

    steward_read = visit_clients.steward.get("/visit-plans/today")
    assert steward_read.status_code == 200, steward_read.text
    admin_plan = _create(
        visit_clients.admin,
        "P-VISIT-02",
        "visit-admin-role-0001",
        session_sitting_no=2,
    )
    _assert_receipt(
        admin_plan, status="draft", revision=1, patient_id="P-VISIT-02")


def test_idempotent_replay_after_later_transitions_returns_original_receipt(
        visit_clients: VisitClients):
    create_body = _plan_body(
        "P-VISIT-01", "visit-history-create-0001", queue_order=17)
    create_response = visit_clients.researcher.post(
        "/visit-plans", json=create_body)
    assert create_response.status_code == 200, create_response.text
    created = create_response.json()

    approve_body = {
        "idempotency_key": "visit-history-approve-0001",
        "expected_revision": created["revision"],
    }
    approve_response = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/approve", json=approve_body)
    assert approve_response.status_code == 200, approve_response.text
    approved = approve_response.json()

    start_body = {
        "idempotency_key": "visit-history-start-0001",
        "expected_revision": approved["revision"],
    }
    start_response = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/start", json=start_body)
    assert start_response.status_code == 200, start_response.text
    started = start_response.json()

    # Idempotency is a response replay contract, not a shortcut to the mutable
    # current resource.  Every old key must keep returning the original fact.
    replay_create = visit_clients.researcher.post(
        "/visit-plans", json=create_body)
    replay_approve = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/approve", json=approve_body)
    replay_start = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/start", json=start_body)
    assert replay_create.status_code == 200, replay_create.text
    assert replay_approve.status_code == 200, replay_approve.text
    assert replay_start.status_code == 200, replay_start.text
    assert replay_create.json() == created
    assert replay_approve.json() == approved
    assert replay_start.json() == started

    with Session(visit_clients.engine) as session:
        commands = list(session.exec(
            select(VisitPlanCommand)
            .where(VisitPlanCommand.plan_id == created["plan_id"])
            .order_by(VisitPlanCommand.event_seq)))
        assert [(row.command_type, row.event_seq, row.resulting_revision)
                for row in commands] == [
            ("create", 1, 1),
            ("approve", 2, 2),
            ("start", 3, 3),
        ]


def test_withdrawal_blocks_historical_business_replays_without_session_leak(
        visit_clients: VisitClients):
    create_body = _plan_body(
        "P-VISIT-01", "visit-withdrawn-replay-create-0001")
    create_response = visit_clients.researcher.post(
        "/visit-plans", json=create_body)
    assert create_response.status_code == 200, create_response.text
    created = create_response.json()

    approve_body = {
        "idempotency_key": "visit-withdrawn-replay-approve-0001",
        "expected_revision": created["revision"],
    }
    approve_response = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/approve", json=approve_body)
    assert approve_response.status_code == 200, approve_response.text
    approved = approve_response.json()

    start_body = {
        "idempotency_key": "visit-withdrawn-replay-start-0001",
        "expected_revision": approved["revision"],
    }
    start_response = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/start", json=start_body)
    assert start_response.status_code == 200, start_response.text
    started = start_response.json()
    session_id = started["session_id"]
    assert isinstance(session_id, str) and session_id

    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-01")
        train_session = session.get(TrainSession, session_id)
        runtime = session.get(SessionRuntimeState, session_id)
        assert patient is not None
        assert train_session is not None
        assert runtime is not None
        session_snapshot = train_session.model_dump()
        runtime_snapshot = runtime.model_dump()
        patient.withdrawal_status = "withdrawn"
        session.add(patient)
        session.commit()

    replay_responses = (
        visit_clients.researcher.post("/visit-plans", json=create_body),
        visit_clients.researcher.post(
            f"/visit-plans/{created['plan_id']}/approve", json=approve_body),
        visit_clients.researcher.post(
            f"/visit-plans/{created['plan_id']}/start", json=start_body),
    )
    for replay in replay_responses:
        assert replay.status_code == 409, replay.text
        assert replay.json()["detail"] == {
            "code": "visit_plan_patient_withdrawn",
            "message": "受试者已进入撤回终态，该历史命令不再提供业务回执",
        }
        assert session_id not in replay.text

    with Session(visit_clients.engine) as session:
        sessions = list(session.exec(select(TrainSession)))
        runtimes = list(session.exec(select(SessionRuntimeState)))
        commands = list(session.exec(
            select(VisitPlanCommand)
            .where(VisitPlanCommand.plan_id == created["plan_id"])
            .order_by(VisitPlanCommand.event_seq)
        ))
        plan = session.get(VisitPlan, created["plan_id"])
        assert len(sessions) == 1
        assert len(runtimes) == 1
        assert len(commands) == 3
        assert plan is not None
        assert (plan.status, plan.revision) == ("started", 3)
        assert sessions[0].model_dump() == session_snapshot
        assert runtimes[0].model_dump() == runtime_snapshot


def test_cancel_replay_is_exact_before_withdrawal_and_hidden_afterward(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-02",
        "visit-withdrawn-cancel-create-0001",
    )
    cancel_body = {
        "idempotency_key": "visit-withdrawn-cancel-replay-0001",
        "expected_revision": created["revision"],
        "reason_code": "schedule_changed",
    }
    cancelled_response = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/cancel", json=cancel_body)
    assert cancelled_response.status_code == 200, cancelled_response.text
    cancelled = cancelled_response.json()

    replay_before_withdrawal = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/cancel", json=cancel_body)
    assert replay_before_withdrawal.status_code == 200
    assert replay_before_withdrawal.json() == cancelled

    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-02")
        assert patient is not None
        patient.withdrawal_status = "withdrawn"
        session.add(patient)
        session.commit()

    replay = visit_clients.researcher.post(
        f"/visit-plans/{created['plan_id']}/cancel", json=cancel_body)
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"] == {
        "code": "visit_plan_patient_withdrawn",
        "message": "受试者已进入撤回终态，该历史命令不再提供业务回执",
    }
    assert cancelled["patient_id"] not in replay.text
    assert cancelled["scheduled_date"] not in replay.text
    assert cancelled["plan_id"] not in replay.text

    with Session(visit_clients.engine) as session:
        assert list(session.exec(select(TrainSession))) == []
        assert list(session.exec(select(SessionRuntimeState))) == []
        plan = session.get(VisitPlan, created["plan_id"])
        assert plan is not None
        assert (plan.status, plan.revision) == ("cancelled", 2)
        commands = list(session.exec(
            select(VisitPlanCommand)
            .where(VisitPlanCommand.plan_id == created["plan_id"])
            .order_by(VisitPlanCommand.event_seq)
        ))
        assert [(row.command_type, row.event_seq) for row in commands] == [
            ("create", 1),
            ("cancel", 2),
        ]


def test_concurrent_exact_create_replays_converge_on_one_fact(
        visit_clients: VisitClients):
    peer = _login("visit-researcher")
    body = _plan_body("P-VISIT-02", "visit-race-exact-create-0001")
    barrier = Barrier(2)

    def submit(client: TestClient):
        barrier.wait()
        response = client.post("/visit-plans", json=body)
        return response.status_code, response.json()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(submit, visit_clients.researcher)
            second_future = pool.submit(submit, peer)
            results = [first_future.result(), second_future.result()]
    finally:
        peer.close()

    assert [status for status, _payload in results] == [200, 200]
    assert results[0][1] == results[1][1]
    plan_id = results[0][1]["plan_id"]
    with Session(visit_clients.engine) as session:
        plans = list(session.exec(select(VisitPlan).where(
            VisitPlan.patient_id == "P-VISIT-02")))
        commands = list(session.exec(select(VisitPlanCommand).where(
            VisitPlanCommand.plan_id == plan_id)))
        assert len(plans) == 1
        assert len(commands) == 1
        assert commands[0].command_type == "create"


def test_concurrent_exact_approve_and_start_create_one_transition_and_session(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-08",
        "visit-race-mutation-create-0001",
    )
    peer = _login("visit-researcher")

    def race(action: str, body: dict):
        barrier = Barrier(2)

        def submit(client: TestClient):
            barrier.wait()
            response = client.post(
                f"/visit-plans/{created['plan_id']}/{action}", json=body)
            return response.status_code, response.json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(submit, visit_clients.researcher),
                pool.submit(submit, peer),
            ]
            return [future.result() for future in futures]

    try:
        approve_results = race("approve", {
            "idempotency_key": "visit-race-mutation-approve-0001",
            "expected_revision": created["revision"],
        })
        assert [status for status, _payload in approve_results] == [200, 200]
        assert approve_results[0][1] == approve_results[1][1]
        approved = approve_results[0][1]

        start_results = race("start", {
            "idempotency_key": "visit-race-mutation-start-0001",
            "expected_revision": approved["revision"],
        })
        assert [status for status, _payload in start_results] == [200, 200]
        assert start_results[0][1] == start_results[1][1]
        started = start_results[0][1]
    finally:
        peer.close()

    assert (started["status"], started["revision"]) == ("started", 3)
    with Session(visit_clients.engine) as session:
        commands = list(session.exec(
            select(VisitPlanCommand)
            .where(VisitPlanCommand.plan_id == created["plan_id"])
            .order_by(VisitPlanCommand.event_seq)))
        sessions = list(session.exec(select(TrainSession).where(
            TrainSession.visit_plan_id == created["plan_id"])))
        assert [row.command_type for row in commands] == [
            "create", "approve", "start"]
        assert len(sessions) == 1
        assert sessions[0].session_id == started["session_id"]
        assert session.get(SessionRuntimeState, started["session_id"]) is not None


def test_same_protocol_slot_race_conflicts_cleanly_and_cancel_releases_slot(
        visit_clients: VisitClients):
    peer = _login("visit-researcher")
    bodies = [
        _plan_body(
            "P-VISIT-03", "visit-slot-race-create-a-0001",
            scheduled_time="09:00:00", queue_order=1),
        _plan_body(
            "P-VISIT-03", "visit-slot-race-create-b-0001",
            scheduled_time="11:00:00", queue_order=2),
    ]
    barrier = Barrier(2)

    def submit(index: int, client: TestClient):
        barrier.wait()
        response = client.post("/visit-plans", json=bodies[index])
        return index, response.status_code, response.json()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(submit, 0, visit_clients.researcher),
                pool.submit(submit, 1, peer),
            ]
            results = [future.result() for future in futures]
    finally:
        peer.close()

    assert sorted(status for _index, status, _payload in results) == [200, 409]
    winner_index, _winner_status, winner = next(
        result for result in results if result[1] == 200)
    loser_index, _loser_status, loser = next(
        result for result in results if result[1] == 409)
    assert loser["detail"]["code"] == "visit_plan_protocol_slot_conflict"

    cancelled_response = _command(
        visit_clients.researcher,
        winner["plan_id"],
        "cancel",
        key="visit-slot-race-cancel-0001",
        expected_revision=winner["revision"],
        reason_code="schedule_changed",
    )
    assert cancelled_response.status_code == 200, cancelled_response.text
    cancelled = cancelled_response.json()
    assert cancelled["status"] == "cancelled"

    historical_create = visit_clients.researcher.post(
        "/visit-plans", json=bodies[winner_index])
    assert historical_create.status_code == 200, historical_create.text
    assert historical_create.json() == winner

    # The losing transaction left no ghost idempotency command.  Once cancel
    # atomically clears the active-slot key, the exact losing request can win.
    replacement_response = visit_clients.researcher.post(
        "/visit-plans", json=bodies[loser_index])
    assert replacement_response.status_code == 200, replacement_response.text
    replacement = replacement_response.json()
    assert replacement["plan_id"] != winner["plan_id"]
    assert time.fromisoformat(replacement["scheduled_time"]) == time(
        9 if loser_index == 0 else 11)

    with Session(visit_clients.engine) as session:
        plans = list(session.exec(select(VisitPlan).where(
            VisitPlan.patient_id == "P-VISIT-03")))
        commands = list(session.exec(select(VisitPlanCommand).where(
            VisitPlanCommand.plan_id.in_([row.plan_id for row in plans]))))
        assert sorted(row.status for row in plans) == ["cancelled", "draft"]
        assert next(row for row in plans if row.status == "cancelled").protocol_slot_key is None
        assert next(row for row in plans if row.status == "draft").protocol_slot_key
        assert sorted(row.command_type for row in commands) == [
            "cancel", "create", "create"]
        assert winner_index != loser_index


def test_start_integrity_failure_rolls_back_session_runtime_plan_and_command(
        visit_clients: VisitClients, monkeypatch):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-04",
        "visit-start-rollback-create-0001",
    )
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-start-rollback-approve-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()

    collision_id = "s-visit-start-atomic-collision"
    with Session(visit_clients.engine) as session:
        session.add(TrainSession(
            session_id=collision_id,
            patient_id="P-VISIT-05",
            session_sitting_no=99,
            training_date=TODAY,
            week_no=2,
            phase_type="正式训练",
            event_line="正式训练",
            trainer_id="ACTOR-fixture",
            item_bank_version_id=BANK.version_id,
            is_simulation=True,
            data_classification="simulation",
        ))
        session.add(SessionRuntimeState(
            session_id=collision_id,
            status="completed",
            revision=7,
            updated_at=datetime.now(),
        ))
        session.commit()

    original_new_id = visit_plan_service._new_id
    monkeypatch.setattr(
        visit_plan_service,
        "_new_id",
        lambda prefix: collision_id if prefix == "s" else original_new_id(prefix),
    )
    denied = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-start-rollback-start-0001",
        expected_revision=approved["revision"],
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "visit_plan_concurrency_conflict"

    with Session(visit_clients.engine) as session:
        plan = session.get(VisitPlan, created["plan_id"])
        assert plan is not None
        assert (
            plan.autopilot_profile_version_id,
            plan.autopilot_profile_definition_digest,
        ) == (None, None)
        assert (plan.status, plan.revision, plan.started_by, plan.started_at) == (
            "approved", 2, None, None)
        assert list(session.exec(select(TrainSession).where(
            TrainSession.visit_plan_id == created["plan_id"]))) == []
        collision = session.get(TrainSession, collision_id)
        assert collision is not None
        assert (
            collision.autopilot_profile_version_id,
            collision.autopilot_profile_definition_digest,
        ) == (None, None)
        assert session.get(SessionRuntimeState, collision_id).revision == 7
        commands = list(session.exec(
            select(VisitPlanCommand)
            .where(VisitPlanCommand.plan_id == created["plan_id"])
            .order_by(VisitPlanCommand.event_seq)))
        assert [(row.command_type, row.event_seq) for row in commands] == [
            ("create", 1), ("approve", 2)]


def test_approve_and_start_revalidate_recording_and_simulation_identity(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-06",
        "visit-second-gate-create-0001",
    )
    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-06")
        assert patient is not None
        patient.recording_allowed = False
        session.add(patient)
        session.commit()

    denied_approve = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-second-gate-approve-denied-0001",
        expected_revision=created["revision"],
    )
    assert denied_approve.status_code == 409, denied_approve.text
    assert denied_approve.json()["detail"]["code"] == "visit_plan_patient_ineligible"

    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-06")
        assert patient is not None
        patient.recording_allowed = True
        session.add(patient)
        session.commit()
    approved_response = _command(
        visit_clients.researcher,
        created["plan_id"],
        "approve",
        key="visit-second-gate-approve-allowed-0001",
        expected_revision=created["revision"],
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()

    with Session(visit_clients.engine) as session:
        patient = session.get(Patient, "P-VISIT-06")
        assert patient is not None
        patient.is_simulation_subject = False
        session.add(patient)
        session.commit()
    denied_start = _command(
        visit_clients.researcher,
        created["plan_id"],
        "start",
        key="visit-second-gate-start-denied-0001",
        expected_revision=approved["revision"],
    )
    assert denied_start.status_code == 409, denied_start.text
    assert denied_start.json()["detail"]["code"] == "visit_plan_patient_ineligible"

    with Session(visit_clients.engine) as session:
        plan = session.get(VisitPlan, created["plan_id"])
        assert plan is not None
        assert (plan.status, plan.revision) == ("approved", 2)
        assert list(session.exec(select(TrainSession).where(
            TrainSession.visit_plan_id == created["plan_id"]))) == []
        commands = list(session.exec(select(VisitPlanCommand).where(
            VisitPlanCommand.plan_id == created["plan_id"])))
        assert sorted(row.command_type for row in commands) == ["approve", "create"]


def test_visit_plan_command_ledger_rejects_orm_update_and_delete(
        visit_clients: VisitClients):
    created = _create(
        visit_clients.researcher,
        "P-VISIT-07",
        "visit-ledger-immutable-create-0001",
    )
    with Session(visit_clients.engine) as session:
        command = session.exec(select(VisitPlanCommand).where(
            VisitPlanCommand.plan_id == created["plan_id"])).one()
        command_id = command.id
        original_hash = command.request_hash
        command.request_hash = "0" * 64
        session.add(command)
        with pytest.raises(RuntimeError, match="只追加命令账本"):
            session.commit()
        session.rollback()

    with Session(visit_clients.engine) as session:
        command = session.get(VisitPlanCommand, command_id)
        assert command is not None
        assert command.request_hash == original_hash
        session.delete(command)
        with pytest.raises(RuntimeError, match="只追加命令账本"):
            session.commit()
        session.rollback()

    with Session(visit_clients.engine) as session:
        command = session.get(VisitPlanCommand, command_id)
        assert command is not None
        assert command.request_hash == original_hash
