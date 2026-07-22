from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import audio_store, auth, db as app_db
from app.db import get_session
from app.main import _audio_capture_evidence_is_verified, app
from app.models import (AttemptEvent, AudioAssetRow, AudioCaptureReceipt, Patient,
                        ResearchUser, SessionAutopilotState, SessionRuntimeState,
                        TurnEvent)
from app.runtime import PlanItem, PlanTurn, SessionPlan


BANK_VERSION = "wk2-v1-20260707"


def _client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    client = TestClient(app)
    client.test_engine = engine
    return client, engine


def _login_confirmation_reviewer(client: TestClient) -> None:
    # 认证中间件与路由 dependency 必须读同一个测试库。
    app_db.engine = client.test_engine
    if client.get("/auth/me").status_code == 200:
        return
    with Session(client.test_engine) as session:
        session.add(ResearchUser(
            username="lifecycle-reviewer", display_id="LIFECYCLE-REVIEWER",
            password_hash=auth.hash_password("password-2026"), role="admin",
        ))
        session.commit()
    assert client.post("/auth/login", json={
        "username": "lifecycle-reviewer", "password": "password-2026",
    }).status_code == 200
    client.headers.update({"X-CSRF-Token": client.cookies.get(auth.CSRF_COOKIE_NAME)})


def _patient(client: TestClient, patient_id: str = "P-LIFE") -> None:
    response = client.post("/patients", json={
        "patient_id": patient_id,
        "is_simulation_subject": True,
        "consent_status": "已同意",
        "consent_type": "本人同意",
        "mandarin_eligible": True,
        "recording_allowed": True,
    })
    assert response.status_code == 200, response.text


def _session(client: TestClient, session_id: str = "S-LIFE",
             patient_id: str = "P-LIFE") -> None:
    response = client.post("/sessions", json={
        "session_id": session_id,
        "patient_id": patient_id,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "item_bank_version_id": BANK_VERSION,
        "is_simulation": True,
    })
    assert response.status_code == 200, response.text


def _one_turn_plan() -> SessionPlan:
    return SessionPlan(
        item_bank_version_id=BANK_VERSION,
        week_no=2,
        event_line="正式训练",
        items=(PlanItem(
            item_id="SE_锚",
            task_type="单要素",
            image_id=None,
            presentation_order=1,
            turns=(PlanTurn(1, "命名"),),
        ),),
    )


def _unlocked_authoritative_turn(
        client: TestClient, raw_audio_id: str = "life-operational-audio") -> tuple[int, int]:
    item = client.post("/sessions/S-LIFE/items", json={
        "item_id": "SE_锚", "task_type": "单要素", "presentation_order": 1,
    })
    assert item.status_code == 200, item.text
    with Session(client.test_engine) as db_session:
        existing_audio = db_session.get(AudioAssetRow, raw_audio_id)
    if existing_audio is None:
        assert client.post("/audio", json={
            "raw_audio_id": raw_audio_id, "session_id": "S-LIFE",
            "turn_key": "SE_锚#1",
        }).status_code == 200
        assert client.put(
            f"/audio/{raw_audio_id}/blob",
            content=b"\x1a\x45\xdf\xa3life-operational-audio",
            headers={"content-type": "audio/webm"},
        ).status_code == 200
    with Session(client.test_engine) as db_session:
        db_session.add(AttemptEvent(
            session_id="S-LIFE", item_id="SE_锚", turn_seq=1,
            response_role="命名", attempt_seq=1, raw_audio_id=raw_audio_id,
            prompt_level=0, asr_text="锚", asr_confidence=.9,
            asr_engine_version="test-asr", operational_answer_type="正确",
            operational_score=1, operational_needs_review=False,
            judge_mode="规则确定式", judge_engine_version="rule-test",
            processing_status="completed", is_simulation=True,
        ))
        db_session.commit()
    turn = client.post(f"/items/{item.json()['id']}/turns", json={
        "turn_seq": 1, "response_role": "命名", "prompt_level": 0,
        "raw_audio_id": raw_audio_id,
    })
    assert turn.status_code == 200, turn.text
    assert turn.json()["score_locked"] is False
    assert turn.json()["confirmed_response_text"] is None
    return item.json()["id"], turn.json()["id"]


def _handshake(client: TestClient):
    return client.put("/live/state", json={"kind": "session", "payload": {
        "sessionId": "S-LIFE",
        "weekNo": 2,
        "eventLine": "正式训练",
        "mode": "task",
        "itemBankVersionId": BANK_VERSION,
    }})


def _cursor(recording: str = "idle") -> dict:
    return {
        "sessionId": "S-LIFE",
        "screen": "record" if recording != "idle" else "present",
        "itemIdx": 0,
        "turnIdx": 0,
        "responseRole": "命名",
        "cueLevel": 0,
        "recording": recording,
        "selfStart": recording == "armed",
    }


def _save_no_additional_closeout(client: TestClient, key: str) -> dict:
    response = client.put("/sessions/S-LIFE/closeout", json={
        "idempotency_key": key,
        "expected_revision": 0,
        "report_status": "no_additional_observation",
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_incomplete_completion_is_structured_and_fail_closed(monkeypatch):
    client, _ = _client()
    try:
        _patient(client)
        _session(client)
        monkeypatch.setattr("app.main._session_plan_for_runtime",
                            lambda _session: _one_turn_plan())

        response = client.post("/sessions/S-LIFE/complete")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == "intervention_not_completed"
        assert detail["runtime_status"] == "active"
        assert client.get("/sessions/S-LIFE/runtime").json()["status"] == "active"
    finally:
        app.dependency_overrides.clear()


def test_admin_supervision_can_abort_without_inventing_an_actor_type():
    client, engine = _client()
    try:
        _patient(client)
        _session(client)
        _login_confirmation_reviewer(client)

        aborted = client.post("/sessions/S-LIFE/abort", json={
            "reason_code": "researcher_decision",
            "expected_revision": 0,
            "idempotency_key": "abort-admin-supervision-20260719-0001",
        })
        assert aborted.status_code == 200, aborted.text
        assert aborted.json()["status"] == "aborted"

        with Session(engine) as session:
            state = session.get(SessionRuntimeState, "S-LIFE")
            assert state is not None
            assert state.status == "aborted"
            assert state.ended_by == "LIFECYCLE-REVIEWER"
    finally:
        app.dependency_overrides.clear()


def test_locked_truth_without_bound_audio_cannot_complete(monkeypatch):
    client, _engine = _client()
    try:
        _patient(client)
        _session(client)
        monkeypatch.setattr("app.main._session_plan_for_runtime",
                            lambda _session: _one_turn_plan())
        item = client.post("/sessions/S-LIFE/items", json={
            "item_id": "SE_锚", "task_type": "单要素", "presentation_order": 1,
        }).json()
        # 模拟迁移前遗留/篡改行；正常 API 已不允许建立这种无 source attempt 的 Turn。
        with Session(_engine) as db_session:
            db_session.add(TurnEvent(
                item_event_id=item["id"], turn_seq=1, response_role="命名",
                confirmed_response_text="锚", prompt_level=0, score_locked=True,
                element_value=1, reviewed_score=1))
            # Simulate a migrated/bad review record that says bedside intervention
            # ended; the completion gate must still catch the missing audio proof.
            db_session.add(SessionRuntimeState(
                session_id="S-LIFE", status="intervention_completed", revision=1))
            db_session.commit()
        response = client.post("/sessions/S-LIFE/complete")
        assert response.status_code == 409
        assessment = response.json()["detail"]["assessment"]
        assert assessment["locked_turns"] == 1
        assert assessment["audio_evidenced_turns"] == 0
        assert {issue["code"] for issue in assessment["issues"]} >= {
            "missing_audio_reference",
        }
    finally:
        app.dependency_overrides.clear()


def test_product_audio_evidence_requires_immutable_capture_receipt():
    client, engine = _client()
    try:
        _patient(client)
        _session(client)
        raw_audio_id = "capture-receipt-required"
        assert client.post("/audio", json={
            "raw_audio_id": raw_audio_id,
            "session_id": "S-LIFE",
            "turn_key": "SE_锚#1",
        }).status_code == 200
        assert client.put(
            f"/audio/{raw_audio_id}/blob",
            content=b"\x1a\x45\xdf\xa3receipt-audio",
            headers={"content-type": "audio/webm"},
        ).status_code == 200

        with Session(engine) as db_session:
            row = db_session.get(AudioAssetRow, raw_audio_id)
            assert row is not None
            assert _audio_capture_evidence_is_verified(
                row, db_session, require_capture_receipt=False) is True
            assert _audio_capture_evidence_is_verified(
                row, db_session, require_capture_receipt=True) is False
            assert row.byte_count is not None and row.checksum is not None
            db_session.add(AudioCaptureReceipt(
                raw_audio_id=raw_audio_id,
                session_id="S-LIFE",
                turn_key="SE_锚#1",
                duration_seconds=1.0,
                byte_count=row.byte_count,
                checksum=row.checksum,
                data_classification=row.data_classification,
                is_simulation=row.is_simulation,
                contains_direct_identifier=row.contains_direct_identifier,
            ))
            db_session.commit()
            assert _audio_capture_evidence_is_verified(
                row, db_session, require_capture_receipt=True) is True
    finally:
        app.dependency_overrides.clear()


def test_research_truth_cannot_be_locked_without_server_audio_bytes(monkeypatch):
    client, engine = _client()
    try:
        _patient(client)
        _session(client)
        monkeypatch.setattr("app.main._session_plan_for_runtime",
                            lambda _session: _one_turn_plan())
        item = client.post("/sessions/S-LIFE/items", json={
            "item_id": "SE_锚", "task_type": "单要素", "presentation_order": 1,
        }).json()
        raw_audio_id = "missing-review-audio-bytes"
        assert client.post("/audio", json={
            "raw_audio_id": raw_audio_id,
            "session_id": "S-LIFE",
            "turn_key": "SE_锚#1",
        }).status_code == 200
        assert client.put(
            f"/audio/{raw_audio_id}/blob",
            content=b"\x1a\x45\xdf\xa3review-audio",
            headers={"content-type": "audio/webm"},
        ).status_code == 200
        with Session(engine) as db_session:
            db_session.add(AttemptEvent(
                session_id="S-LIFE", item_id="SE_锚", turn_seq=1,
                response_role="命名", attempt_seq=1,
                raw_audio_id=raw_audio_id, prompt_level=0,
                asr_text="锚", asr_confidence=.9,
                asr_engine_version="test-asr",
                operational_answer_type="正确", operational_score=1,
                operational_needs_review=False,
                judge_mode="规则确定式", judge_engine_version="rule-test",
                processing_status="completed", is_simulation=True,
            ))
            db_session.commit()
        turn_response = client.post(f"/items/{item['id']}/turns", json={
            "turn_seq": 1,
            "response_role": "命名",
            "prompt_level": 0,
            "raw_audio_id": raw_audio_id,
        })
        assert turn_response.status_code == 200, turn_response.text
        turn = turn_response.json()

        # Active bedside state is not a research-review window, even when an
        # operational attempt and its audio are already present.
        _login_confirmation_reviewer(client)
        for early in (
            client.patch(f"/turns/{turn['id']}/confirm", json={
                "confirmed_response_text": "锚",
                "expected_revision": 0,
                "idempotency_key": "test-lifecycle-confirm-active-0001",
            }),
            client.patch(f"/turns/{turn['id']}/lock", json={
                "reviewer_id": "R1", "element_value": 1, "prompt_level": 0,
            }),
        ):
            assert early.status_code == 409
            assert early.json()["detail"]["code"] == (
                "research_review_requires_intervention_completion")

        finished = client.post("/sessions/S-LIFE/finish-intervention")
        assert finished.status_code == 200, finished.text
        _login_confirmation_reviewer(client)
        assert client.patch(f"/turns/{turn['id']}/confirm", json={
            "confirmed_response_text": "锚",
            "expected_revision": 0,
            "idempotency_key": "test-lifecycle-confirm-missing-audio-0001",
        }).status_code == 200
        assert audio_store.delete_blob(raw_audio_id) is True
        denied = client.patch(f"/turns/{turn['id']}/lock", json={
            "reviewer_id": "R1", "element_value": 1, "prompt_level": 0,
        })
        assert denied.status_code == 409
        assert denied.json()["detail"]["code"] == (
            "research_review_audio_bytes_unavailable")
    finally:
        app.dependency_overrides.clear()


def test_finish_intervention_rejects_audio_replaced_after_upload(monkeypatch):
    client, _ = _client()
    try:
        _patient(client)
        _session(client)
        monkeypatch.setattr("app.main._session_plan_for_runtime",
                            lambda _session: _one_turn_plan())
        _unlocked_authoritative_turn(client)
        physical = audio_store.find_blob("life-operational-audio")
        assert physical is not None
        physical.write_bytes(b"\x1a\x45\xdf\xa3tampered-after-upload")

        denied = client.post("/sessions/S-LIFE/finish-intervention")
        assert denied.status_code == 409
        assessment = denied.json()["detail"]["assessment"]
        assert assessment["ready"] is False
        assert {issue["code"] for issue in assessment["issues"]} >= {
            "missing_audio_blob",
        }
        assert client.get("/sessions/S-LIFE/runtime").json()["status"] == "active"
    finally:
        app.dependency_overrides.clear()


def test_final_completion_rechecks_audio_integrity_after_lock(monkeypatch):
    client, _ = _client()
    try:
        _patient(client)
        _session(client)
        monkeypatch.setattr("app.main._session_plan_for_runtime",
                            lambda _session: _one_turn_plan())
        _item_id, turn_id = _unlocked_authoritative_turn(client)
        assert client.post(
            "/sessions/S-LIFE/finish-intervention").status_code == 200
        _login_confirmation_reviewer(client)
        assert client.patch(f"/turns/{turn_id}/confirm", json={
            "confirmed_response_text": "锚",
            "expected_revision": 0,
            "idempotency_key": "test-lifecycle-confirm-integrity-0001",
        }).status_code == 200
        assert client.patch(f"/turns/{turn_id}/lock", json={
            "reviewer_id": "R1", "element_value": 1, "prompt_level": 0,
        }).status_code == 200
        _save_no_additional_closeout(client, "closeout-integrity-recheck-0001")

        physical = audio_store.find_blob("life-operational-audio")
        assert physical is not None
        physical.write_bytes(b"\x1a\x45\xdf\xa3tampered-after-lock")
        denied = client.post("/sessions/S-LIFE/complete")
        assert denied.status_code == 409
        assessment = denied.json()["detail"]["assessment"]
        assert assessment["ready"] is False
        assert {issue["code"] for issue in assessment["issues"]} >= {
            "missing_audio_blob",
        }
        assert client.get("/sessions/S-LIFE/runtime").json()["status"] == (
            "intervention_completed")
        assert client.get("/sessions/S-LIFE/closeout").json()["locked"] is False
    finally:
        app.dependency_overrides.clear()


def test_finish_intervention_releases_bedside_before_research_review(monkeypatch):
    client, _ = _client()
    try:
        _patient(client)
        _session(client)
        monkeypatch.setattr("app.main._session_plan_for_runtime",
                            lambda _session: _one_turn_plan())
        assert _handshake(client).status_code == 200
        item_id, turn_id = _unlocked_authoritative_turn(client)

        finished = client.post("/sessions/S-LIFE/finish-intervention")
        assert finished.status_code == 200, finished.text
        body = finished.json()
        assert body["status"] == "intervention_completed"
        assert body["interventionCompletedAt"] is not None
        assert body["interventionEndedBy"] == "PIN/本地"
        assert body["completedAt"] is None
        assert body["interventionAssessment"] == {
            "ready": True,
            "expected_turns": 1,
            "matched_turns": 1,
            "completed_attempt_turns": 1,
            "audio_evidenced_turns": 1,
            "issues": [],
        }
        revision = body["revision"]
        repeated = client.post("/sessions/S-LIFE/finish-intervention")
        assert repeated.status_code == 200
        assert repeated.json()["revision"] == revision

        live = client.get("/live/state").json()
        assert live["session"]["runtimeStatus"] == "intervention_completed"
        assert live["cursor"]["screen"] == "thanks"
        assert live["cursor"]["recording"] == "stopped"
        assert client.get("/patients").json()[0]["unfinished_session_count"] == 0

        # P0a keeps this terminal control-plane row as immutable provenance.
        # It must not strand the later research review behind the old
        # autonomous-ownership gate.
        with Session(client.test_engine) as db_session:
            db_session.add(SessionAutopilotState(
                session_id="S-LIFE",
                scope_key="p0a_sim_first_single_v1",
                mode="autonomous",
                status="scope_completed",
                control_generation=1,
                runner_generation=1,
                revision=1,
            ))
            db_session.commit()

        operational_writes = [
            client.post("/sessions/S-LIFE/pause"),
            client.post("/sessions/S-LIFE/resume"),
            client.post("/sessions/S-LIFE/recording-authorization"),
            client.post("/audio", json={
                "raw_audio_id": "after-intervention", "session_id": "S-LIFE",
                "turn_key": "SE_锚#1",
            }),
            client.post("/sessions/S-LIFE/items", json={
                "item_id": "SE_树", "task_type": "单要素",
            }),
            client.post(f"/items/{item_id}/turns", json={
                "turn_seq": 1, "response_role": "命名",
                "raw_audio_id": "life-operational-audio",
            }),
        ]
        assert all(response.status_code == 409 for response in operational_writes)

        _login_confirmation_reviewer(client)
        confirmed = client.patch(f"/turns/{turn_id}/confirm", json={
            "confirmed_response_text": "锚",
            "expected_revision": 0,
            "idempotency_key": "test-lifecycle-confirm-release-0001",
        })
        assert confirmed.status_code == 200, confirmed.text
        abnormal = client.post("/sessions/S-LIFE/abnormal", json={
            "abnormal_type": "环境噪声",
        })
        assert abnormal.status_code == 200, abnormal.text
        locked = client.patch(f"/turns/{turn_id}/lock", json={
            "reviewer_id": "R1", "element_value": 1, "prompt_level": 0,
        })
        assert locked.status_code == 200, locked.text

        saved_closeout = _save_no_additional_closeout(
            client, "closeout-lifecycle-review-0001")
        assert saved_closeout["locked"] is False

        completed = client.post("/sessions/S-LIFE/complete")
        assert completed.status_code == 200, completed.text
        completed_body = completed.json()
        assert completed_body["status"] == "completed"
        assert completed_body["completedAt"] is not None
        assert completed_body["interventionCompletedAt"] == body["interventionCompletedAt"]
        assert client.get("/live/state").json()["session"]["runtimeStatus"] == "completed"
        locked_closeout = client.get("/sessions/S-LIFE/closeout")
        assert locked_closeout.status_code == 200
        assert locked_closeout.json()["locked"] is True
        assert locked_closeout.json()["revision"] == 2
        assert locked_closeout.json()["locked_by"] == "LIFECYCLE-REVIEWER"
        assert client.put("/sessions/S-LIFE/closeout", json={
            "idempotency_key": "closeout-after-complete-0002",
            "expected_revision": 2,
            "report_status": "no_additional_observation",
        }).status_code == 409
        final_revision = completed_body["revision"]
        after_final_retry = client.post("/sessions/S-LIFE/finish-intervention")
        assert after_final_retry.status_code == 200
        assert after_final_retry.json()["status"] == "completed"
        assert after_final_retry.json()["revision"] == final_revision

        audit_rows = client.get("/audit", params={"session_id": "S-LIFE"}).json()
        assert sum(row["action"] == "session_intervention_complete" for row in audit_rows) == 1
        assert sum(row["action"] == "session_complete" for row in audit_rows) == 1
    finally:
        app.dependency_overrides.clear()


def test_finish_intervention_fails_closed_without_ai_and_audio_evidence(monkeypatch):
    client, _ = _client()
    try:
        _patient(client)
        _session(client)
        monkeypatch.setattr("app.main._session_plan_for_runtime",
                            lambda _session: _one_turn_plan())

        response = client.post("/sessions/S-LIFE/finish-intervention")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["assessment"]["ready"] is False
        assert detail["assessment"]["expected_turns"] == 1
        assert detail["assessment"]["completed_attempt_turns"] == 0
        assert detail["assessment"]["audio_evidenced_turns"] == 0
        assert detail["assessment"]["issues"][0]["code"] == "missing_turn"
        assert client.get("/sessions/S-LIFE/runtime").json()["status"] == "active"
    finally:
        app.dependency_overrides.clear()


def test_finish_persists_text_free_summary_and_closeout_is_cas_idempotent(monkeypatch):
    client, engine = _client()
    try:
        _patient(client)
        _session(client)
        monkeypatch.setattr("app.main._session_plan_for_runtime",
                            lambda _session: _one_turn_plan())
        _unlocked_authoritative_turn(client)

        finished = client.post("/sessions/S-LIFE/finish-intervention")
        assert finished.status_code == 200, finished.text
        summary = client.get("/sessions/S-LIFE/outcome-summary")
        assert summary.status_code == 200, summary.text
        assert summary.headers["cache-control"] == "private, no-store"
        assert summary.json() | {
            "expected_turns": 1,
            "matched_turns": 1,
            "completed_attempt_turns": 1,
            "audio_evidenced_turns": 1,
            "total_attempts": 1,
            "completed_attempts": 1,
            "technical_failure_attempts": 0,
        } == summary.json()
        assert "锚" not in summary.text
        assert len(summary.json()["source_digest"]) == 64
        replay = client.post("/sessions/S-LIFE/finish-intervention")
        assert replay.status_code == 200
        assert replay.json()["outcomeSummaryAvailable"] is True

        missing = client.get("/sessions/S-LIFE/closeout")
        assert missing.status_code == 404
        empty_observation = client.put("/sessions/S-LIFE/closeout", json={
            "idempotency_key": "closeout-empty-observation-0001",
            "expected_revision": 0,
            "report_status": "observation_recorded",
        })
        assert empty_observation.status_code == 422

        request = {
            "idempotency_key": "closeout-cas-create-0001",
            "expected_revision": 0,
            "report_status": "no_additional_observation",
        }
        created = client.put("/sessions/S-LIFE/closeout", json=request)
        assert created.status_code == 200, created.text
        assert created.json()["revision"] == 1
        assert created.json()["idempotent"] is False
        exact_replay = client.put("/sessions/S-LIFE/closeout", json=request)
        assert exact_replay.status_code == 200
        assert exact_replay.json()["revision"] == 1
        assert exact_replay.json()["idempotent"] is True

        changed_replay = client.put("/sessions/S-LIFE/closeout", json={
            **request,
            "report_status": "observation_recorded",
            "note": "设备重连后继续。",
        })
        assert changed_replay.status_code == 409
        assert changed_replay.json()["detail"]["code"] == "closeout_idempotency_conflict"

        updated = client.put("/sessions/S-LIFE/closeout", json={
            "idempotency_key": "closeout-cas-update-0002",
            "expected_revision": 1,
            "report_status": "observation_recorded",
            "device_or_network_interruption_occurred": True,
            "note": "设备重连后继续。",
        })
        assert updated.status_code == 200, updated.text
        assert updated.json()["revision"] == 2
        stale = client.put("/sessions/S-LIFE/closeout", json={
            "idempotency_key": "closeout-cas-stale-0003",
            "expected_revision": 1,
            "report_status": "no_additional_observation",
        })
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "closeout_revision_conflict"

        audit_rows = client.get("/audit", params={"session_id": "S-LIFE"}).json()
        assert "设备重连后继续" not in str(audit_rows)
        assert sum(row["action"] == "session_closeout_record" for row in audit_rows) == 2

        # Immutable snapshot cannot be edited through an ORM session either.
        from app.models import SessionOutcomeSummary
        with Session(engine) as db_session:
            row = db_session.get(SessionOutcomeSummary, "S-LIFE")
            assert row is not None
            row.total_attempts = 99
            db_session.add(row)
            try:
                db_session.commit()
            except RuntimeError as exc:
                db_session.rollback()
                assert "不可变" in str(exc)
            else:
                raise AssertionError("immutable outcome summary unexpectedly updated")
    finally:
        app.dependency_overrides.clear()


def test_withdrawal_closes_summary_and_closeout_reads_and_writes(monkeypatch):
    client, engine = _client()
    try:
        _patient(client)
        _session(client)
        monkeypatch.setattr("app.main._session_plan_for_runtime",
                            lambda _session: _one_turn_plan())
        _unlocked_authoritative_turn(client)
        assert client.post(
            "/sessions/S-LIFE/finish-intervention").status_code == 200
        saved = client.put("/sessions/S-LIFE/closeout", json={
            "idempotency_key": "closeout-before-withdrawal-0001",
            "expected_revision": 0,
            "report_status": "observation_recorded",
            "note": "现场备注不得在撤回后返回。",
        })
        assert saved.status_code == 200, saved.text

        with Session(engine) as db_session:
            patient = db_session.get(Patient, "P-LIFE")
            assert patient is not None
            patient.withdrawal_status = "withdrawn"
            db_session.add(patient)
            db_session.commit()

        for path in (
            "/sessions/S-LIFE/outcome-summary",
            "/sessions/S-LIFE/closeout",
        ):
            response = client.get(path)
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == (
                "subject_withdrawn_content_unavailable")
            assert "现场备注不得" not in response.text
        write = client.put("/sessions/S-LIFE/closeout", json={
            "idempotency_key": "closeout-after-withdrawal-0002",
            "expected_revision": 1,
            "report_status": "no_additional_observation",
        })
        assert write.status_code == 409
        assert write.json()["detail"]["code"] == (
            "subject_withdrawn_content_unavailable")
    finally:
        app.dependency_overrides.clear()


def test_complete_is_atomic_idempotent_and_all_runtime_inputs_stay_closed(monkeypatch):
    client, _ = _client()
    try:
        _patient(client)
        _session(client)
        monkeypatch.setattr("app.main._session_plan_for_runtime",
                            lambda _session: _one_turn_plan())

        assert _handshake(client).status_code == 200
        authorization = client.post("/sessions/S-LIFE/recording-authorization")
        assert authorization.status_code == 200
        assert authorization.json() == {
            "allowed": True, "runtime_status": "active", "is_simulation": True,
        }
        assert client.put("/live/state", json={
            "kind": "cursor", "payload": _cursor("armed"),
        }).status_code == 200
        pre_registered = client.post("/audio", json={
            "raw_audio_id": "before-terminal",
            "session_id": "S-LIFE",
            "turn_key": "SE_锚#1",
        })
        assert pre_registered.status_code == 200, pre_registered.text
        assert client.put("/audio/before-terminal/blob", content=b"\x1a\x45\xdf\xa3audio",
                          headers={"content-type": "audio/webm"}).status_code == 200
        item_id, turn_id = _unlocked_authoritative_turn(
            client, "before-terminal")

        premature = client.post("/sessions/S-LIFE/complete")
        assert premature.status_code == 409
        assert premature.json()["detail"]["code"] == "intervention_not_completed"
        finished = client.post("/sessions/S-LIFE/finish-intervention")
        assert finished.status_code == 200, finished.text
        assert finished.json()["status"] == "intervention_completed"

        _login_confirmation_reviewer(client)
        assert client.patch(f"/turns/{turn_id}/confirm", json={
            "confirmed_response_text": "锚",
            "expected_revision": 0,
            "idempotency_key": "test-lifecycle-confirm-complete-0001",
        }).status_code == 200
        locked = client.patch(f"/turns/{turn_id}/lock", json={
            "reviewer_id": "R1", "element_value": 1, "prompt_level": 0,
        })
        assert locked.status_code == 200, locked.text

        _save_no_additional_closeout(
            client, "closeout-lifecycle-complete-0001")

        completed = client.post("/sessions/S-LIFE/complete")
        assert completed.status_code == 200, completed.text
        body = completed.json()
        assert body["status"] == "completed"
        assert body["completedAt"] is not None
        assert body["endedBy"] == "LIFECYCLE-REVIEWER"
        assert body["completionAssessment"]["ready"] is True
        revision = body["revision"]

        live = client.get("/live/state").json()
        assert live["cursor"]["screen"] == "thanks"
        assert live["cursor"]["recording"] == "stopped"
        assert live["cursor"]["selfStart"] is False
        assert client.get("/live/console-state").json()["patientRec"] is None

        repeated = client.post("/sessions/S-LIFE/complete")
        assert repeated.status_code == 200
        assert repeated.json()["revision"] == revision
        audit_rows = client.get("/audit", params={"session_id": "S-LIFE"}).json()
        assert sum(row["action"] == "session_complete" for row in audit_rows) == 1

        blocked = [
            client.put("/sessions/S-LIFE/runtime/cursor", json=_cursor()),
            client.post("/sessions/S-LIFE/pause"),
            client.post("/sessions/S-LIFE/resume"),
            client.post("/sessions/S-LIFE/recording-authorization"),
            client.put("/live/state", json={"kind": "cursor", "payload": _cursor()}),
            client.put("/live/state", json={"kind": "rapportStep", "payload": {
                "sessionId": "S-LIFE", "sectionKey": "自我介绍",
                "questionIdx": 0, "recording": "idle",
                "containsDirectIdentifier": True,
            }}),
            _handshake(client),
            client.post("/audio", json={
                "raw_audio_id": "after-terminal", "session_id": "S-LIFE",
                "turn_key": "SE_锚#1",
            }),
            client.post("/sessions/S-LIFE/items", json={
                "item_id": "SE_猫", "task_type": "单要素",
            }),
            client.post(f"/items/{item_id}/turns", json={
                "turn_seq": 1, "response_role": "命名",
            }),
        ]
        assert all(response.status_code == 409 for response in blocked[:7])
        # Once a named account is active, a brand-new raw id is rejected at the
        # stronger provenance boundary before terminal-runtime diagnostics.
        assert blocked[7].status_code == 403
        assert blocked[7].json()["detail"]["code"] == (
            "audio_registration_device_required")
        assert all(response.status_code == 409 for response in blocked[8:])
        # 只有已落服务端事实的完全相同字节可作为 ACK 丢失重放；
        # 它不会重开场次或改变音频。不同字节仍在终态 fail closed。
        lost_upload_ack = client.put(
            "/audio/before-terminal/blob", content=b"\x1a\x45\xdf\xa3audio",
            headers={"content-type": "audio/webm"})
        assert lost_upload_ack.status_code == 200
        assert lost_upload_ack.json()["idempotent"] is True
        assert client.put(
            "/audio/before-terminal/blob", content=b"\x1a\x45\xdf\xa3different",
            headers={"content-type": "audio/webm"}).status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_abort_is_explicit_idempotent_and_list_endpoints_expose_lifecycle():
    client, _ = _client()
    try:
        _patient(client)
        _session(client)
        assert client.get("/patients/P-LIFE/sessions").json()[0]["runtime_status"] == "active"
        assert client.get("/patients").json()[0]["unfinished_session_count"] == 1
        _item_id, turn_id = _unlocked_authoritative_turn(client)
        turn = {"id": turn_id}
        assert client.post("/sessions/S-LIFE/abort", json={"reason": "   "}).status_code == 422
        stale = client.post("/sessions/S-LIFE/abort", json={
            "reason_code": "participant_declined",
            "expected_revision": 1,
            "idempotency_key": "abort-lifecycle-stale-revision-0000",
        })
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "session_abort_revision_conflict"
        abort_key = "abort-lifecycle-participant-declined-0001"
        aborted = client.post("/sessions/S-LIFE/abort", json={
            "reason_code": "participant_declined",
            "expected_revision": 0,
            "idempotency_key": abort_key,
        })
        assert aborted.status_code == 200, aborted.text
        assert aborted.json()["status"] == "aborted"
        assert aborted.json()["abortedAt"] is not None
        assert aborted.json()["endReason"] == "participant_declined"
        revision = aborted.json()["revision"]
        assert client.post("/sessions/S-LIFE/abort", json={
            "reason_code": "participant_declined",
            "expected_revision": 0,
            "idempotency_key": abort_key,
        }).json()["revision"] == revision
        conflict = client.post("/sessions/S-LIFE/abort", json={
            "reason_code": "technical_failure",
            "expected_revision": 0,
            "idempotency_key": abort_key,
        })
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "session_abort_idempotency_conflict"
        second_operation = client.post("/sessions/S-LIFE/abort", json={
            "reason_code": "participant_declined",
            "expected_revision": 0,
            "idempotency_key": "abort-lifecycle-second-operation-0002",
        })
        assert second_operation.status_code == 409
        assert second_operation.json()["detail"]["code"] == "session_already_aborted"

        sessions = client.get("/patients/P-LIFE/sessions").json()
        assert sessions[0]["runtime_status"] == "aborted"
        summary = client.get("/patients").json()[0]
        assert summary["consent_status"] == "已同意"
        assert summary["research_eligible"] is False
        assert any("模拟档案" in issue for issue in summary["research_eligibility_issues"])
        assert summary["unfinished_session_count"] == 0
        _login_confirmation_reviewer(client)
        retained = client.delete(
            "/audio/life-operational-audio",
            params={"session_id": "S-LIFE", "source": "manual"},
        )
        assert retained.status_code == 409
        assert retained.json()["detail"]["code"] == "aborted_session_evidence_retained"
        frozen_writes = [
            client.patch(f"/turns/{turn['id']}/confirm", json={
                "confirmed_response_text": "锚",
                "expected_revision": 0,
                "idempotency_key": "test-lifecycle-confirm-aborted-0001",
            }),
            client.post(f"/turns/{turn['id']}/ai-judge"),
            client.patch(f"/turns/{turn['id']}/lock", json={
                "reviewer_id": "R1", "element_value": 1, "prompt_level": 0,
            }),
            client.post("/sessions/S-LIFE/abnormal", json={
                "abnormal_type": "长时间沉默",
            }),
        ]
        assert all(response.status_code == 409 for response in frozen_writes)
        audit_rows = client.get("/audit", params={"session_id": "S-LIFE"}).json()
        assert sum(row["action"] == "session_abort" for row in audit_rows) == 1
    finally:
        app.dependency_overrides.clear()


def test_patient_summary_reports_fail_closed_eligibility_gaps():
    client, _ = _client()
    try:
        assert client.post("/patients", json={"patient_id": "P-GAPS"}).status_code == 200
        row = client.get("/patients").json()[0]
        assert row["research_eligible"] is False
        assert row["unfinished_session_count"] == 0
        assert any("consent_status" in issue for issue in row["research_eligibility_issues"])
        assert any("recording_allowed" in issue for issue in row["research_eligibility_issues"])
    finally:
        app.dependency_overrides.clear()


def test_recording_authorization_rechecks_current_withdrawal_status():
    client, engine = _client()
    try:
        _patient(client)
        _session(client)
        assert client.post("/sessions/S-LIFE/recording-authorization").status_code == 200
        with Session(engine) as db_session:
            patient = db_session.get(Patient, "P-LIFE")
            assert patient is not None
            patient.withdrawal_status = "withdrawal_requested"
            db_session.add(patient)
            db_session.commit()

        revoked = client.post("/sessions/S-LIFE/recording-authorization")
        assert revoked.status_code == 409
        assert "撤回" in revoked.json()["detail"]
    finally:
        app.dependency_overrides.clear()
