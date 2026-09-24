"""Quality counts preserve distinct pauses after an explicitly resumed capture."""
import pytest
from sqlmodel import Session

from app import autopilot_ledger
from app.models import AutopilotControlEvent
from test_ai_quality_api import (
    _client, _quality, _seed_patient, _seed_session, _worker_failure_control_event,
    quality_env as quality_env,
)


@pytest.mark.parametrize("tamper", [None, "record", "actor", "source", "gap", "base", "suffix"])
def test_repeat_failure_needs_exact_saved_capture_resume_proof(quality_env, tamper):
    sid = "S-RESUMED-WORKER"
    base = autopilot_ledger.attempt_failure_event_key(sid, 73, "autopilot_worker_exception")
    with Session(quality_env.engine) as db:
        _seed_patient(db, "P-RESUMED-WORKER", simulation=True)
        _seed_session(db, sid, "P-RESUMED-WORKER", trainer_id="RESEARCH-A", simulation=True, week_no=2)
        if tamper != "base":
            db.add(_worker_failure_control_event(
                session_id=sid, event_seq=1, command_id=73,
                error_code="autopilot_worker_exception"))
        db.add(AutopilotControlEvent(
            idempotency_key="quality-resume-saved-capture", session_id=sid,
            event_seq=2, event_type="resume", scope_key="p0a_sim_first_single_v1",
            control_generation=1, runner_generation=1,
            command_id=74 if tamper == "record" else 73,
            actor_type="system" if tamper == "actor" else "researcher",
            actor_id="RESEARCH-A", reason_code="researcher_explicit_resume",
            from_mode="autonomous", to_mode="autonomous", from_status="paused",
            to_status="processing_attempt",
            payload_json=autopilot_ledger.encode_control_event_payload("resume", {
                "reason_code": "researcher_explicit_resume",
                "source": "unverified_source" if tamper == "source" else "account_resume_endpoint",
            })))
        db.add(_worker_failure_control_event(
            session_id=sid, event_seq=4 if tamper == "gap" else 3,
            command_id=73, error_code="autopilot_worker_exception",
            idempotency_key=f"{base}-r{9 if tamper == 'suffix' else 2}"))
        db.commit()
    row = _quality(_client("admin")).json()["rows"][0]
    assert row["operational"]["technical_pause_count"] == (
        2 if tamper is None else 0 if tamper == "base" else 1)
    assert row["diagnostics"]["reason_counts"]["structural_invalid_evidence_records"] == (
        0 if tamper is None else 1)


def test_resumed_worker_with_different_error_still_counts_two_proven_pauses(quality_env):
    sid = "S-RESUMED-DIFFERENT-ERROR"
    with Session(quality_env.engine) as db:
        _seed_patient(db, "P-RESUMED-DIFFERENT-ERROR", simulation=True)
        _seed_session(db, sid, "P-RESUMED-DIFFERENT-ERROR",
                      trainer_id="RESEARCH-A", simulation=True, week_no=2)
        db.add(_worker_failure_control_event(
            session_id=sid, event_seq=1, command_id=73,
            error_code="autopilot_worker_exception"))
        db.add(AutopilotControlEvent(
            idempotency_key="quality-resume-different-error", session_id=sid,
            event_seq=2, event_type="resume", scope_key="p0a_sim_first_single_v1",
            control_generation=1, runner_generation=1, command_id=73,
            actor_type="researcher", actor_id="RESEARCH-A",
            reason_code="researcher_explicit_resume", from_mode="autonomous",
            to_mode="autonomous", from_status="paused", to_status="processing_attempt",
            payload_json=autopilot_ledger.encode_control_event_payload("resume", {
                "reason_code": "researcher_explicit_resume", "source": "account_resume_endpoint"})))
        # A different error uses its first/base key, yet belongs to episode 2.
        db.add(_worker_failure_control_event(
            session_id=sid, event_seq=3, command_id=73,
            error_code="autopilot_attempt_result_invalid"))
        db.commit()
    row = _quality(_client("admin")).json()["rows"][0]
    assert row["operational"]["technical_pause_count"] == 2
    assert row["diagnostics"]["reason_counts"]["structural_invalid_evidence_records"] == 0
