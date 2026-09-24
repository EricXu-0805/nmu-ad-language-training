# ruff: noqa: F811 -- shared pytest fixture.
"""Control subjects need no invented training session to review locked scales."""
import csv
import io

import pytest
from sqlmodel import Session, select

from app import audit
from app.models import Patient, Session as TrainSession
from test_questionnaires import (  # noqa: F401
    _client, _create_record, _lock_gds_record, api_env,
)


@pytest.fixture(autouse=True)
def export_keys(monkeypatch):
    monkeypatch.setenv("DEIDENTIFICATION_KEY", "test-only-key-for-review-export-000000000")
    monkeypatch.setenv("DEIDENTIFICATION_KEY_ID", "review-test")


def _get(client, patient="P-Q1", dataset="items"):
    return client.get(f"/patients/{patient}/questionnaire-review.csv?dataset={dataset}")


@pytest.mark.parametrize("simulation", [False, True])
def test_zero_training_control_exports_only_locked_numbered_deidentified_items(api_env, simulation):
    researcher = _client("research-a")
    locked = _lock_gds_record(researcher, "否")
    _create_record(researcher, "P-Q1", "gds15_v1", phase="后测")
    with Session(api_env) as db:
        patient = db.get(Patient, "P-Q1")
        patient.study_arm = "空白对照=PRIVATE-LABEL"
        patient.is_simulation_subject = simulation
        db.add(patient)
        db.commit()
        assert list(db.exec(select(TrainSession))) == []
    response = _get(_client("steward"))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(rows) == 15
    assert sorted(int(row["item_number"]) for row in rows) == list(range(1, 16))
    assert {row["data_classification"] for row in rows} == {"simulation" if simulation else "research"}
    assert {row["is_frozen"] for row in rows} == {"False"}
    assert {row["export_kind"] for row in rows} == {"operational_questionnaire_review"}
    for private in ("P-Q1", locked["record_id"], "PRIVATE-LABEL"):
        assert private not in response.text
    totals = _get(_client("steward"), dataset="records")
    assert totals.status_code == 200, totals.text
    total_rows = list(csv.DictReader(io.StringIO(totals.content.decode("utf-8-sig"))))
    assert len(total_rows) == 1
    assert float(total_rows[0]["computed_total"]) == locked["computed_total"]
    with Session(api_env) as db:
        assert audit.verify_chain(db)["ok"] is True


def test_drafts_empty_and_superseded_history_are_explicit(api_env):
    researcher = _client("research-a")
    first = _lock_gds_record(researcher, "否")
    _create_record(researcher, "P-Q1", "gds15_v1")
    result = _get(_client("steward"), dataset="records")
    assert result.status_code == 200, result.text
    rows = list(csv.DictReader(io.StringIO(result.content.decode("utf-8-sig"))))
    assert len(rows) == 1
    assert rows[0]["phase_ordinal"] == "1"
    assert rows[0]["superseded_by_ordinal"] == "2"
    assert first["record_id"] not in result.text


def test_roles_withdrawal_missing_key_and_cross_origin_are_rejected(api_env, monkeypatch):
    researcher = _client("research-a")
    _lock_gds_record(researcher, "否")
    assert _get(researcher).status_code == 403
    steward = _client("steward")
    assert _get(steward, patient="P-WD").status_code == 409
    denied = steward.get("/patients/P-Q1/questionnaire-review.csv", headers={"Sec-Fetch-Site": "cross-site"})
    assert denied.status_code == 403
    monkeypatch.delenv("DEIDENTIFICATION_KEY")
    assert _get(steward).status_code == 503


def test_audit_failure_does_not_release_csv(api_env, monkeypatch):
    _lock_gds_record(_client("research-a"), "否")
    steward = _client("steward")
    def fail(*args, **kwargs):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(audit, "record", fail)
    response = _get(steward)
    assert response.status_code == 503
    assert "text/csv" not in response.headers["content-type"]
