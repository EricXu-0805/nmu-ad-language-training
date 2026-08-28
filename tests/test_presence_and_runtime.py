from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine, inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import auth, content, db, main as main_mod, models  # noqa: F401 —— 注册全部表
from app.db import get_session
from app.main import app


BANK_VERSION = "wk2-v1-20260707"
_HANDSHAKE_WSEQ = 0


@pytest.fixture
def client(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", eng)
    SQLModel.metadata.create_all(eng)

    def override():
        with Session(eng) as s:
            yield s

    app.dependency_overrides[get_session] = override
    test_client = TestClient(app)
    test_client.test_engine = eng
    yield test_client
    app.dependency_overrides.clear()


def _patient(client, patient_id: str, recording_allowed: bool | None = True, headers=None):
    body = {"patient_id": patient_id, "consent_status": "已同意",
            "consent_type": "本人同意", "mandarin_eligible": True,
            "is_simulation_subject": True}
    if recording_allowed is not None:
        body["recording_allowed"] = recording_allowed
    assert client.post("/patients", json=body, headers=headers).status_code == 200


def _training_session(client, session_id: str, patient_id: str, headers=None):
    response = client.post("/sessions", json={
        "session_id": session_id,
        "patient_id": patient_id,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "item_bank_version_id": BANK_VERSION,
        "is_simulation": True,
        "trainer_id": "PRESENCE-REVIEWER",
    }, headers=headers)
    assert response.status_code == 200, response.text


def _login_confirmation_reviewer(client: TestClient) -> None:
    with Session(client.test_engine) as session:
        session.add(models.ResearchUser(
            username="presence-reviewer", display_id="PRESENCE-REVIEWER",
            password_hash=auth.hash_password("password-2026"), role="researcher",
        ))
        session.commit()
    assert client.post("/auth/login", json={
        "username": "presence-reviewer", "password": "password-2026",
    }).status_code == 200
    client.headers.update({"X-CSRF-Token": client.cookies.get(auth.CSRF_COOKIE_NAME)})


def _authoritative_turn(client, item_id: int):
    assert client.post("/audio", json={
        "raw_audio_id": "runtime-lock-audio", "session_id": "S-LOCK",
        "turn_key": "SE_锚#1",
    }).status_code == 200
    assert client.put("/audio/runtime-lock-audio/blob",
                      content=b"\x1a\x45\xdf\xa3runtime-audio",
                      headers={"content-type": "audio/webm"}).status_code == 200
    with Session(client.test_engine) as session:
        session.add(models.AttemptEvent(
            session_id="S-LOCK", item_id="SE_锚", turn_seq=1,
            response_role="命名", attempt_seq=1,
            raw_audio_id="runtime-lock-audio", prompt_level=0,
            asr_text="锚", asr_confidence=.9, asr_engine_version="test-asr",
            operational_answer_type="正确", operational_score=1,
            operational_needs_review=False, judge_mode="规则确定式",
            judge_engine_version="rule-test", processing_status="completed",
            is_simulation=True,
        ))
        session.commit()
    return client.post(f"/items/{item_id}/turns", json={
        "turn_seq": 1, "response_role": "命名",
        "raw_audio_id": "runtime-lock-audio",
    }).json()


def _handshake(client, session_id: str, headers=None, mode="task", wseq=None):
    global _HANDSHAKE_WSEQ
    if wseq is None:
        _HANDSHAKE_WSEQ += 10
        wseq = _HANDSHAKE_WSEQ
    return client.put("/live/state", json={"kind": "session", "payload": {
        "sessionId": session_id,
        "weekNo": 2 if mode == "task" else 1,
        "eventLine": "正式训练" if mode == "task" else "关系建立环节",
        "mode": mode,
        "itemBankVersionId": BANK_VERSION,
        "wseq": wseq,
    }}, headers=headers)


def _cursor(session_id: str | None = None, item_idx=0, turn_idx=0, **overrides):
    payload = {
        "screen": "present",
        "itemIdx": item_idx,
        "turnIdx": turn_idx,
        "responseRole": "命名",
        "cueLevel": 0,
        "recording": "idle",
        "selfStart": False,
        "wseq": 20,
    }
    if session_id:
        payload["sessionId"] = session_id
    payload.update(overrides)
    return payload


def _presentation_cursor(client: TestClient, session_id: str, base: dict,
                         **overrides) -> dict:
    """Bind a bedside command to the exact live/runtime snapshot just read."""
    live_cursor = client.get("/live/state").json()["cursor"]
    runtime = client.get(f"/sessions/{session_id}/runtime").json()
    payload = {**base, **overrides}
    payload["wseq"] = live_cursor["wseq"]
    payload["expected_revision"] = runtime["revision"]
    return payload


def _presentation_setup(client: TestClient, token: str, *, cue_level: int = 0):
    patient_id = f"P-PRESENT-{token}"
    session_id = f"S-PRESENT-{token}"
    _patient(client, patient_id)
    _training_session(client, session_id, patient_id)
    plan_response = client.get(
        f"/sessions/{session_id}/plan",
        params={"week_no": 2, "event_line": "正式训练"},
    )
    assert plan_response.status_code == 200, plan_response.text
    item = plan_response.json()["items"][0]
    turn = item["turns"][0]
    assert _handshake(client, session_id).status_code == 200
    base = _cursor(
        session_id,
        responseRole=turn["response_role"],
        cueLevel=cue_level,
    )
    current = client.put("/live/state", json={
        "kind": "cursor", "payload": base,
    })
    assert current.status_code == 200, current.text
    return session_id, item, turn, base


def _technical_pause_body(client: TestClient, session_id: str, key: str,
                          *, error_code: str = "client_microphone",
                          attempt_id: int | None = None) -> dict:
    runtime = client.get(f"/sessions/{session_id}/runtime").json()
    live_cursor = client.get("/live/state").json()["cursor"]
    body = {
        "idempotency_key": key,
        "expected_revision": runtime["revision"],
        "expected_live_wseq": live_cursor["wseq"],
        "error_code": error_code,
    }
    if attempt_id is not None:
        body["attempt_id"] = attempt_id
    return body


def _completed_attempt(client: TestClient, *, session_id: str, item: dict,
                       turn: dict, prompt_level: int = 0,
                       attempt_seq: int = 1) -> models.AttemptEvent:
    raw_audio_id = f"presentation-audio-{session_id}-{attempt_seq}"
    with Session(client.test_engine) as session:
        session.add(models.AudioAssetRow(
            raw_audio_id=raw_audio_id,
            session_id=session_id,
            is_simulation=True,
            data_classification="simulation",
            turn_key=f'{item["item_id"]}#{turn["turn_seq"]}',
        ))
        attempt = models.AttemptEvent(
            session_id=session_id,
            item_id=item["item_id"],
            turn_seq=turn["turn_seq"],
            response_role=turn["response_role"],
            attempt_seq=attempt_seq,
            raw_audio_id=raw_audio_id,
            prompt_level=prompt_level,
            asr_text="测试回答",
            asr_engine_version="test-asr",
            operational_answer_type="正确",
            operational_score=1,
            operational_needs_review=False,
            judge_mode="test",
            judge_engine_version="test-judge",
            processing_status="completed",
            is_simulation=True,
        )
        session.add(attempt)
        session.commit()
        session.refresh(attempt)
        session.expunge(attempt)
    return attempt


def test_cue_evidence_and_bedside_presentation_are_atomic_across_pause_and_wrapup(
        client):
    _patient(client, "P-CUE-ATOMIC")
    _training_session(client, "S-CUE-ATOMIC", "P-CUE-ATOMIC")
    plan = client.get(
        "/sessions/S-CUE-ATOMIC/plan",
        params={"week_no": 2, "event_line": "正式训练"},
    ).json()
    item = plan["items"][0]
    turn = item["turns"][0]
    assert _handshake(client, "S-CUE-ATOMIC").status_code == 200
    initial_cursor = _cursor(
        "S-CUE-ATOMIC",
        responseRole=turn["response_role"],
        cueLevel=0,
    )
    assert client.put("/live/state", json={
        "kind": "cursor", "payload": initial_cursor,
    }).status_code == 200

    presentation_attempt = 0

    def present(level: int):
        nonlocal presentation_attempt
        presentation_attempt += 1
        return client.post(
            "/sessions/S-CUE-ATOMIC/interaction-presentations",
            json={
                "idempotency_key": (
                    f"cue-atomic-{presentation_attempt:04d}-level-{level}"),
                "interaction": {
                    "event_type": "cue_selected",
                    "item_id": item["item_id"],
                    "turn_seq": turn["turn_seq"],
                    "prompt_level": level,
                    "cue_type": f"prompt_level_{level}",
                },
                "cursor": _presentation_cursor(
                    client, "S-CUE-ATOMIC", initial_cursor,
                    cueLevel=level, recording="idle", selfStart=False),
            },
        )

    first = present(1)
    assert first.status_code == 200, first.text
    assert first.json()["interaction"]["event_type"] == "cue_selected"
    assert first.json()["cursor"]["cueLevel"] == 1
    assert first.json()["cursor"]["wseq"] == first.json()["wseq"]
    assert client.get("/live/state").json()["cursor"]["cueLevel"] == 1
    runtime = client.get("/sessions/S-CUE-ATOMIC/runtime").json()
    assert runtime["cursor"]["cueLevel"] == 1

    legacy = client.post("/sessions/S-CUE-ATOMIC/interactions", json={
        "event_type": "cue_selected",
        "item_id": item["item_id"],
        "turn_seq": turn["turn_seq"],
        "prompt_level": 2,
        "cue_type": "prompt_level_2",
    })
    assert legacy.status_code == 409
    assert legacy.json()["detail"]["code"] == (
        "interaction_presentation_atomic_required")

    assert client.post("/sessions/S-CUE-ATOMIC/pause").status_code == 200
    paused = present(2)
    assert paused.status_code == 409
    assert paused.json()["detail"]["code"] == (
        "interaction_presentation_runtime_inactive")

    assert client.post("/sessions/S-CUE-ATOMIC/resume").status_code == 200
    thanks_cursor = {
        **initial_cursor,
        "screen": "thanks",
        "cueLevel": 1,
        "recording": "stopped",
        "selfStart": False,
    }
    assert client.put("/live/state", json={
        "kind": "cursor", "payload": thanks_cursor,
    }).status_code == 200
    after_thanks = present(2)
    assert after_thanks.status_code == 409
    assert after_thanks.json()["detail"]["code"] == (
        "interaction_presentation_position_changed")

    with Session(client.test_engine) as session:
        events = list(session.exec(select(models.InteractionEvent).where(
            models.InteractionEvent.session_id == "S-CUE-ATOMIC")))
        assert len(events) == 1
        assert events[0].event_type == "cue_selected"


def test_atomic_presentation_exact_replay_conflict_and_superseded_replay(client):
    session_id, item, turn, base = _presentation_setup(client, "IDEMP")
    body = {
        "idempotency_key": "atomic-idempotency-key-0001",
        "interaction": {
            "event_type": "cue_selected",
            "item_id": item["item_id"],
            "turn_seq": turn["turn_seq"],
            "prompt_level": 1,
            "cue_type": "prompt_level_1",
        },
        "cursor": _presentation_cursor(
            client, session_id, base, cueLevel=1),
    }

    first = client.post(
        f"/sessions/{session_id}/interaction-presentations", json=body)
    assert first.status_code == 200, first.text
    first_payload = first.json()
    assert first_payload["idempotent"] is False

    replay = client.post(
        f"/sessions/{session_id}/interaction-presentations", json=body)
    assert replay.status_code == 200, replay.text
    replay_payload = replay.json()
    assert replay_payload["idempotent"] is True
    for field in ("interaction", "cursor", "seq", "wseq", "runtimeRevision"):
        assert replay_payload[field] == first_payload[field]

    conflicting = deepcopy(body)
    conflicting["interaction"]["cue_type"] = "different_semantic_command"
    conflict = client.post(
        f"/sessions/{session_id}/interaction-presentations", json=conflicting)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == (
        "interaction_presentation_idempotency_conflict")

    assert client.post(f"/sessions/{session_id}/pause").status_code == 200
    superseded = client.post(
        f"/sessions/{session_id}/interaction-presentations", json=body)
    assert superseded.status_code == 409, superseded.text
    assert superseded.json()["detail"]["code"] == (
        "interaction_presentation_replay_superseded")

    with Session(client.test_engine) as session:
        events = list(session.exec(select(models.InteractionEvent).where(
            models.InteractionEvent.session_id == session_id)))
        receipts = list(session.exec(select(
            models.InteractionPresentationReceipt).where(
                models.InteractionPresentationReceipt.session_id == session_id)))
    assert len(events) == len(receipts) == 1
    assert receipts[0].interaction_event_id == events[0].id


def test_atomic_presentation_rejects_repeated_level_three_without_writes(client):
    session_id, item, turn, base = _presentation_setup(
        client, "LEVEL3", cue_level=2)
    first = client.post(
        f"/sessions/{session_id}/interaction-presentations",
        json={
            "idempotency_key": "atomic-level-three-first-0001",
            "interaction": {
                "event_type": "cue_selected",
                "item_id": item["item_id"],
                "turn_seq": turn["turn_seq"],
                "prompt_level": 3,
                "cue_type": "prompt_level_3",
            },
            "cursor": _presentation_cursor(
                client, session_id, base, cueLevel=3),
        },
    )
    assert first.status_code == 200, first.text
    before_live = client.get("/live/state").json()
    before_runtime = client.get(f"/sessions/{session_id}/runtime").json()

    repeated = client.post(
        f"/sessions/{session_id}/interaction-presentations",
        json={
            "idempotency_key": "atomic-level-three-repeat-0002",
            "interaction": {
                "event_type": "cue_selected",
                "item_id": item["item_id"],
                "turn_seq": turn["turn_seq"],
                "prompt_level": 3,
                "cue_type": "prompt_level_3",
            },
            "cursor": _presentation_cursor(
                client, session_id, base, cueLevel=3),
        },
    )
    assert repeated.status_code == 409, repeated.text
    assert repeated.json()["detail"]["code"] == (
        "interaction_presentation_cue_not_next")
    after_live = client.get("/live/state").json()
    after_runtime = client.get(f"/sessions/{session_id}/runtime").json()
    assert after_live["seq"] == before_live["seq"]
    assert after_live["cursor"] == before_live["cursor"]
    assert after_runtime["revision"] == before_runtime["revision"]
    assert after_runtime["cursor"] == before_runtime["cursor"]

    with Session(client.test_engine) as session:
        assert len(list(session.exec(select(models.InteractionEvent).where(
            models.InteractionEvent.session_id == session_id)))) == 1
        assert len(list(session.exec(select(
            models.InteractionPresentationReceipt).where(
                models.InteractionPresentationReceipt.session_id == session_id)))) == 1


def test_atomic_presentation_preflights_semantic_content_and_rolls_back(client):
    session_id, item, turn, base = _presentation_setup(client, "CONTENT")
    attempt = _completed_attempt(
        client, session_id=session_id, item=item, turn=turn)
    before_live = client.get("/live/state").json()
    before_runtime = client.get(f"/sessions/{session_id}/runtime").json()

    unavailable = client.post(
        f"/sessions/{session_id}/interaction-presentations",
        json={
            "idempotency_key": "atomic-unrenderable-feedback-0001",
            "interaction": {
                "event_type": "feedback_selected",
                "attempt_id": attempt.id,
                # namefix is a valid protocol key, but cannot be rendered for
                # this single-element naming item.
                "feedback_key": "namefix_l",
            },
            "cursor": _presentation_cursor(
                client, session_id, base,
                fbKey="namefix_l", fbItemId=item["item_id"]),
        },
    )
    assert unavailable.status_code == 409, unavailable.text
    assert unavailable.json()["detail"]["code"] == (
        "interaction_presentation_content_unavailable")
    after_live = client.get("/live/state").json()
    after_runtime = client.get(f"/sessions/{session_id}/runtime").json()
    assert after_live["seq"] == before_live["seq"]
    assert after_live["cursor"] == before_live["cursor"]
    assert after_runtime["revision"] == before_runtime["revision"]
    assert after_runtime["cursor"] == before_runtime["cursor"]
    with Session(client.test_engine) as session:
        assert list(session.exec(select(models.InteractionEvent).where(
            models.InteractionEvent.session_id == session_id))) == []
        assert list(session.exec(select(
            models.InteractionPresentationReceipt).where(
                models.InteractionPresentationReceipt.session_id == session_id))) == []


def test_interaction_presentation_maps_frozen_protocol_failure_to_503(
        client, monkeypatch, tmp_path):
    session_id, item, turn, base = _presentation_setup(client, "CONTENT-503")
    body = {
        "idempotency_key": "atomic-content-unavailable-503-0001",
        "interaction": {
            "event_type": "cue_selected",
            "item_id": item["item_id"],
            "turn_seq": turn["turn_seq"],
            "prompt_level": 1,
            "cue_type": "prompt_level_1",
        },
        "cursor": _presentation_cursor(
            client, session_id, base, cueLevel=1),
    }

    original_dir = content.CONTENT_DIR
    (tmp_path / "item_bank_v1.json").write_bytes(
        (original_dir / "item_bank_v1.json").read_bytes()
    )
    (tmp_path / "autopilot_protocol_v1.json").write_text(
        '{"protocol_version_id":', encoding="utf-8"
    )
    monkeypatch.setattr(content, "CONTENT_DIR", tmp_path)

    safe = TestClient(app, raise_server_exceptions=False)
    try:
        response = safe.post(
            f"/sessions/{session_id}/interaction-presentations", json=body)
    finally:
        safe.close()
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "frozen_content_unavailable"
    assert str(tmp_path) not in response.text
    assert "Traceback" not in response.text

    with Session(client.test_engine) as session:
        assert not list(session.exec(select(models.InteractionEvent).where(
            models.InteractionEvent.session_id == session_id)))
        assert not list(session.exec(select(
            models.InteractionPresentationReceipt).where(
                models.InteractionPresentationReceipt.session_id == session_id)))


def test_feedback_sequence_is_server_owned_and_attempt_has_one_branch(client):
    session_id, item, turn, base = _presentation_setup(client, "FEEDBACK")
    attempt = _completed_attempt(
        client, session_id=session_id, item=item, turn=turn)
    body = {
        "idempotency_key": "atomic-feedback-server-seq-0001",
        "interaction": {
            "event_type": "feedback_selected",
            "attempt_id": attempt.id,
            "feedback_key": "self",
        },
        "cursor": _presentation_cursor(
            client, session_id, base,
            fbKey="self", fbItemId=item["item_id"]),
    }
    spoofed_sequence = deepcopy(body)
    spoofed_sequence["idempotency_key"] = "atomic-feedback-spoofed-seq-0000"
    spoofed_sequence["cursor"]["fbSeq"] = 999_999
    rejected = client.post(
        f"/sessions/{session_id}/interaction-presentations",
        json=spoofed_sequence)
    assert rejected.status_code == 422, rejected.text

    first = client.post(
        f"/sessions/{session_id}/interaction-presentations", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["idempotent"] is False
    assert first.json()["cursor"]["fbSeq"] == first.json()["wseq"]

    replay = client.post(
        f"/sessions/{session_id}/interaction-presentations", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent"] is True
    assert replay.json()["interaction"]["id"] == first.json()["interaction"]["id"]

    second_branch = client.post(
        f"/sessions/{session_id}/interaction-presentations",
        json={
            "idempotency_key": "atomic-feedback-second-branch-0002",
            "interaction": {
                "event_type": "feedback_selected",
                "attempt_id": attempt.id,
                "feedback_key": "cued1_unknown",
            },
            "cursor": _presentation_cursor(
                client, session_id, base,
                fbKey="cued1_unknown", fbItemId=item["item_id"]),
        },
    )
    assert second_branch.status_code == 409, second_branch.text
    assert second_branch.json()["detail"]["code"] == (
        "interaction_presentation_attempt_already_resolved")
    with Session(client.test_engine) as session:
        events = list(session.exec(select(models.InteractionEvent).where(
            models.InteractionEvent.attempt_id == attempt.id)))
        receipts = list(session.exec(select(
            models.InteractionPresentationReceipt).where(
                models.InteractionPresentationReceipt.attempt_id == attempt.id)))
    assert len(events) == len(receipts) == 1


def test_atomic_presentation_requires_snapshot_and_rejects_stale_cas(client):
    session_id, item, turn, base = _presentation_setup(client, "CAS")
    valid = {
        "idempotency_key": "atomic-required-snapshot-0001",
        "interaction": {
            "event_type": "cue_selected",
            "item_id": item["item_id"],
            "turn_seq": turn["turn_seq"],
            "prompt_level": 1,
            "cue_type": "prompt_level_1",
        },
        "cursor": _presentation_cursor(
            client, session_id, base, cueLevel=1),
    }
    missing_key = deepcopy(valid)
    missing_key.pop("idempotency_key")
    missing_revision = deepcopy(valid)
    missing_revision["cursor"].pop("expected_revision")
    missing_wseq = deepcopy(valid)
    missing_wseq["cursor"].pop("wseq")
    for malformed in (missing_key, missing_revision, missing_wseq):
        response = client.post(
            f"/sessions/{session_id}/interaction-presentations", json=malformed)
        assert response.status_code == 422, response.text

    advanced = client.put("/live/state", json={
        "kind": "cursor", "payload": base,
    })
    assert advanced.status_code == 200, advanced.text
    stale_revision = client.post(
        f"/sessions/{session_id}/interaction-presentations", json=valid)
    assert stale_revision.status_code == 409, stale_revision.text
    assert stale_revision.json()["detail"]["code"] == (
        "interaction_presentation_revision_changed")

    bad_wseq = deepcopy(valid)
    bad_wseq["idempotency_key"] = "atomic-stale-wseq-snapshot-0002"
    bad_wseq["cursor"] = _presentation_cursor(
        client, session_id, base, cueLevel=1)
    bad_wseq["cursor"]["wseq"] -= 1
    stale_wseq = client.post(
        f"/sessions/{session_id}/interaction-presentations", json=bad_wseq)
    assert stale_wseq.status_code == 409, stale_wseq.text
    assert stale_wseq.json()["detail"]["code"] == (
        "interaction_presentation_wseq_changed")
    with Session(client.test_engine) as session:
        assert list(session.exec(select(models.InteractionEvent).where(
            models.InteractionEvent.session_id == session_id))) == []
        assert list(session.exec(select(
            models.InteractionPresentationReceipt).where(
                models.InteractionPresentationReceipt.session_id == session_id))) == []


def test_atomic_presentation_concurrent_cas_commits_one_command(client):
    session_id, item, turn, base = _presentation_setup(client, "RACE")
    cursor = _presentation_cursor(client, session_id, base, cueLevel=1)

    def body(key: str) -> dict:
        return {
            "idempotency_key": key,
            "interaction": {
                "event_type": "cue_selected",
                "item_id": item["item_id"],
                "turn_seq": turn["turn_seq"],
                "prompt_level": 1,
                "cue_type": "prompt_level_1",
            },
            "cursor": deepcopy(cursor),
        }

    requests = (
        body("atomic-concurrent-command-a-0001"),
        body("atomic-concurrent-command-b-0002"),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda payload: client.post(
                f"/sessions/{session_id}/interaction-presentations",
                json=payload),
            requests,
        ))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response for response in responses if response.status_code == 200)
    loser = next(response for response in responses if response.status_code == 409)
    assert winner.json()["idempotent"] is False
    assert loser.json()["detail"]["code"] in {
        "interaction_presentation_revision_changed",
        "interaction_presentation_wseq_changed",
    }
    with Session(client.test_engine) as session:
        events = list(session.exec(select(models.InteractionEvent).where(
            models.InteractionEvent.session_id == session_id)))
        receipts = list(session.exec(select(
            models.InteractionPresentationReceipt).where(
                models.InteractionPresentationReceipt.session_id == session_id)))
    assert len(events) == len(receipts) == 1


def test_atomic_technical_pause_commits_stop_evidence_and_exact_receipt(client):
    session_id, item, turn, _base = _presentation_setup(client, "TECH-STOP")
    body = _technical_pause_body(
        client, session_id, "technical-pause-exact-replay-0001")

    first = client.post(
        f"/sessions/{session_id}/technical-pause", json=body)
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["idempotent"] is False
    assert payload["runtime"]["status"] == "paused"
    assert payload["runtimeRevision"] == payload["runtime"]["revision"]
    assert payload["cursor"]["screen"] == "paused"
    assert payload["cursor"]["recording"] == "stopped"
    assert payload["cursor"]["wseq"] == payload["wseq"]
    assert payload["interaction"]["event_type"] == "technical_pause"
    assert payload["interaction"]["item_id"] == item["item_id"]
    assert payload["interaction"]["turn_seq"] == turn["turn_seq"]
    assert payload["interaction"]["payload_json"] == (
        '{"error_code":"client_microphone"}')

    replay = client.post(
        f"/sessions/{session_id}/technical-pause", json=body)
    assert replay.status_code == 200, replay.text
    replay_payload = replay.json()
    assert replay_payload["idempotent"] is True
    for field in (
            "interaction", "runtime", "cursor", "seq", "wseq",
            "runtimeRevision"):
        assert replay_payload[field] == payload[field]

    conflicting = {**body, "error_code": "client_audio"}
    conflict = client.post(
        f"/sessions/{session_id}/technical-pause", json=conflicting)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == (
        "technical_pause_idempotency_conflict")

    assert client.post(f"/sessions/{session_id}/resume").status_code == 200
    superseded = client.post(
        f"/sessions/{session_id}/technical-pause", json=body)
    assert superseded.status_code == 409, superseded.text
    assert superseded.json()["detail"]["code"] == (
        "technical_pause_replay_superseded")

    legacy = client.post(f"/sessions/{session_id}/interactions", json={
        "event_type": "technical_pause",
        "error_code": "legacy_split_pause",
    })
    assert legacy.status_code == 409, legacy.text
    assert legacy.json()["detail"]["code"] == (
        "technical_pause_atomic_required")
    with Session(client.test_engine) as session:
        events = list(session.exec(select(models.InteractionEvent).where(
            models.InteractionEvent.session_id == session_id)))
        receipts = list(session.exec(select(models.TechnicalPauseReceipt).where(
            models.TechnicalPauseReceipt.session_id == session_id)))
    assert len(events) == len(receipts) == 1
    assert receipts[0].interaction_event_id == events[0].id


def test_atomic_technical_pause_rolls_back_event_attempt_fence_and_stop(
        client, monkeypatch):
    session_id, item, turn, _base = _presentation_setup(client, "TECH-ROLLBACK")
    raw_audio_id = "technical-pause-rollback-audio"
    with Session(client.test_engine) as session:
        session.add(models.AudioAssetRow(
            raw_audio_id=raw_audio_id,
            session_id=session_id,
            is_simulation=True,
            data_classification="simulation",
            turn_key=f'{item["item_id"]}#{turn["turn_seq"]}',
        ))
        attempt = models.AttemptEvent(
            session_id=session_id,
            item_id=item["item_id"],
            turn_seq=turn["turn_seq"],
            response_role=turn["response_role"],
            attempt_seq=1,
            raw_audio_id=raw_audio_id,
            prompt_level=0,
            processing_status="received",
            processing_owner="slow-worker",
            processing_generation=4,
            is_simulation=True,
        )
        session.add(attempt)
        session.commit()
        session.refresh(attempt)
        attempt_id = attempt.id
    before_live = client.get("/live/state").json()
    before_runtime = client.get(f"/sessions/{session_id}/runtime").json()
    body = _technical_pause_body(
        client, session_id, "technical-pause-rollback-0001",
        attempt_id=attempt_id)

    def fail_runtime_pause(*_args, **_kwargs):
        raise RuntimeError("injected atomic technical pause failure")

    monkeypatch.setattr(
        main_mod, "_pause_runtime_in_transaction", fail_runtime_pause)
    with pytest.raises(
            RuntimeError, match="injected atomic technical pause failure"):
        client.post(f"/sessions/{session_id}/technical-pause", json=body)

    after_live = client.get("/live/state").json()
    after_runtime = client.get(f"/sessions/{session_id}/runtime").json()
    assert after_live["seq"] == before_live["seq"]
    assert after_live["cursor"] == before_live["cursor"]
    assert after_runtime == before_runtime
    with Session(client.test_engine) as session:
        stored_attempt = session.get(models.AttemptEvent, attempt_id)
        assert stored_attempt is not None
        assert stored_attempt.processing_owner == "slow-worker"
        assert stored_attempt.processing_generation == 4
        assert list(session.exec(select(models.InteractionEvent).where(
            models.InteractionEvent.session_id == session_id))) == []
        assert list(session.exec(select(models.TechnicalPauseReceipt).where(
            models.TechnicalPauseReceipt.session_id == session_id))) == []


def test_atomic_technical_pause_rejects_missing_and_stale_snapshots_without_writes(
        client):
    session_id, _item, _turn, base = _presentation_setup(client, "TECH-CAS")
    valid = _technical_pause_body(
        client, session_id, "technical-pause-cas-required-0001")
    for field in ("idempotency_key", "expected_revision", "expected_live_wseq"):
        malformed = dict(valid)
        malformed.pop(field)
        response = client.post(
            f"/sessions/{session_id}/technical-pause", json=malformed)
        assert response.status_code == 422, response.text

    advanced = client.put("/live/state", json={
        "kind": "cursor", "payload": base,
    })
    assert advanced.status_code == 200, advanced.text
    stale_revision = client.post(
        f"/sessions/{session_id}/technical-pause", json=valid)
    assert stale_revision.status_code == 409, stale_revision.text
    assert stale_revision.json()["detail"]["code"] == (
        "technical_pause_revision_changed")

    current = _technical_pause_body(
        client, session_id, "technical-pause-stale-wseq-0002")
    current["expected_live_wseq"] -= 1
    stale_wseq = client.post(
        f"/sessions/{session_id}/technical-pause", json=current)
    assert stale_wseq.status_code == 409, stale_wseq.text
    assert stale_wseq.json()["detail"]["code"] == (
        "technical_pause_wseq_changed")

    # Same wseq/item/turn is not sufficient when a damaged runtime row carries
    # a different semantic cursor (cue level, role, recording, recSeq, etc.).
    with Session(client.test_engine) as session:
        runtime_row = session.get(models.SessionRuntimeState, session_id)
        assert runtime_row is not None and runtime_row.cursor_json is not None
        damaged_cursor = json.loads(runtime_row.cursor_json)
        damaged_cursor["cueLevel"] = 2
        runtime_row.cursor_json = json.dumps(
            damaged_cursor, ensure_ascii=False)
        session.add(runtime_row)
        session.commit()
    divergent = _technical_pause_body(
        client, session_id, "technical-pause-diverged-cursor-0003")
    diverged_response = client.post(
        f"/sessions/{session_id}/technical-pause", json=divergent)
    assert diverged_response.status_code == 409, diverged_response.text
    assert diverged_response.json()["detail"]["code"] == (
        "technical_pause_snapshot_diverged")
    with Session(client.test_engine) as session:
        assert list(session.exec(select(models.InteractionEvent).where(
            models.InteractionEvent.session_id == session_id))) == []
        assert list(session.exec(select(models.TechnicalPauseReceipt).where(
            models.TechnicalPauseReceipt.session_id == session_id))) == []


def test_atomic_technical_pause_concurrent_snapshot_commits_once(client):
    session_id, _item, _turn, _base = _presentation_setup(client, "TECH-RACE")
    snapshot = _technical_pause_body(
        client, session_id, "technical-pause-race-command-a-0001")
    requests = (
        snapshot,
        {
            **snapshot,
            "idempotency_key": "technical-pause-race-command-b-0002",
        },
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda payload: client.post(
                f"/sessions/{session_id}/technical-pause", json=payload),
            requests,
        ))
    assert sorted(response.status_code for response in responses) == [200, 409]
    loser = next(response for response in responses if response.status_code == 409)
    assert loser.json()["detail"]["code"] in {
        "technical_pause_runtime_inactive",
        "technical_pause_revision_changed",
        "technical_pause_wseq_changed",
        "technical_pause_snapshot_already_committed",
    }
    with Session(client.test_engine) as session:
        events = list(session.exec(select(models.InteractionEvent).where(
            models.InteractionEvent.session_id == session_id)))
        receipts = list(session.exec(select(models.TechnicalPauseReceipt).where(
            models.TechnicalPauseReceipt.session_id == session_id)))
    assert len(events) == len(receipts) == 1


def test_patient_heartbeat_is_minimal_pin_protected_and_does_not_advance_command_seq(
        client, monkeypatch):
    pin = {"X-Console-Pin": "24681024"}
    # 研究者在开启独立老人端认证前已建好档案/场次并下发握手。
    _patient(client, "P-HB")
    _training_session(client, "S-HB", "P-HB")
    handshake = _handshake(client, "S-HB", wseq=100)
    assert handshake.status_code == 200
    issued_wseq = handshake.json()["wseq"]
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    assert client.get("/sessions/S-HB/runtime").status_code == 401
    assert client.get("/sessions/S-HB/runtime", headers=pin).status_code == 401
    assert client.post("/sessions/S-HB/pause").status_code == 401
    paired = client.post("/device/pair", headers=pin, json={
        "deviceId": "presence-device-000001",
    })
    assert paired.status_code == 200, paired.text
    capability = {"X-Device-Capability": paired.json()["capability"]}
    before = client.get("/live/state", headers=capability).json()["seq"]

    heartbeat = {
        "session_id": "S-HB", "screen": "present", "cursor_wseq": issued_wseq,
        "client_ts": "2000-01-01T00:00:00",
    }
    assert client.post("/live/patient-heartbeat", json=heartbeat).status_code == 401
    response = client.post("/live/patient-heartbeat", json=heartbeat, headers=capability)
    assert response.status_code == 200, response.text
    presence = response.json()["patientPresence"]
    assert presence["session_id"] == "S-HB"
    assert presence["screen"] == "present" and presence["online"] is True
    assert presence["cursor_wseq"] == issued_wseq
    live = client.get("/live/state", headers=capability).json()
    assert live["seq"] == before  # 心跳不伪装成新的研究者命令
    assert "patientPresence" not in live and "audioSaved" not in live and "patientRec" not in live
    assert client.get("/live/console-state").status_code == 401
    denied_console = client.get("/live/console-state", headers=pin)
    assert denied_console.status_code == 401
    assert denied_console.json()["code"] == "account_required"

    # capability 白名单只接最小字段：内容/回答等额外载荷 fail-closed。
    assert client.post("/live/patient-heartbeat", json={
        "session_id": "S-HB", "screen": "present", "answer_text": "敏感回答",
    }, headers=capability).status_code == 422
    assert client.post("/live/patient-heartbeat", json={
        "session_id": "other", "screen": "present",
    }, headers=capability).status_code == 409
    # "超前"的 ack 序号必须被接受:同机部署下患者端显示的游标常来自 BroadcastChannel
    # (客户端时钟域),在操作端 HTTP 写落库前天然超前于服务端 command_wseq;
    # 按超前拒绝会让每次推进后的第一拍心跳都 409,在场判定滞后甚至假离线。
    ahead = client.post("/live/patient-heartbeat", json={
        "session_id": "S-HB", "screen": "present", "cursor_wseq": issued_wseq + 1,
    }, headers=capability)
    assert ahead.status_code == 200
    assert ahead.json()["patientPresence"]["cursor_wseq"] == issued_wseq + 1

    # 在线状态只信服务器 last_seen；TTL 到期后读取即为离线，不需要额外写库任务。
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "PATIENT_ONLINE_TTL_SECONDS", -1)
    monkeypatch.delenv("CONSOLE_PIN")  # 回到本地开发模式检查服务端投影。
    assert client.get("/live/console-state").json()["patientPresence"]["online"] is False


def test_runtime_cursor_is_persisted_per_session_and_restored_safely(client):
    _patient(client, "P-RT")
    _training_session(client, "S-RT-1", "P-RT")
    _training_session(client, "S-RT-2", "P-RT")

    first_handshake = _handshake(client, "S-RT-1", wseq=100)
    assert first_handshake.status_code == 200
    operational = _cursor("S-RT-1",
        screen="record", recording="armed", selfStart=True, rawAudioId="in-flight", wseq=101)
    cursor_write = client.put("/live/state", json={"kind": "cursor", "payload": operational})
    assert cursor_write.status_code == 200
    rt1 = client.get("/sessions/S-RT-1/runtime").json()
    assert rt1["cursor"]["itemIdx"] == 0
    assert rt1["cursor"]["screen"] == "present"
    assert rt1["cursor"]["recording"] == "idle"
    assert rt1["cursor"]["selfStart"] is False
    assert "rawAudioId" not in rt1["cursor"]
    assert rt1["cursor"]["wseq"] == cursor_write.json()["wseq"] > first_handshake.json()["wseq"]

    assert _handshake(client, "S-RT-2", wseq=200).status_code == 200
    assert client.put("/sessions/S-RT-2/runtime/cursor", json=_cursor(
        "S-RT-2", item_idx=1, wseq=201)).status_code == 200
    assert client.get("/sessions/S-RT-2/runtime").json()["cursor"]["itemIdx"] == 1

    # 切回第一场：从 DB 的每场次游标恢复，而不是沿用第二场或浏览器 localStorage。
    restored_handshake = _handshake(client, "S-RT-1", wseq=300)
    assert restored_handshake.status_code == 200
    restored = client.get("/live/state").json()["cursor"]
    assert restored["itemIdx"] == 0 and restored["recording"] == "idle"
    assert restored["wseq"] > client.get("/live/state").json()["session"]["wseq"]
    assert client.get("/sessions/S-RT-1/runtime").json()["cursor"]["wseq"] == restored["wseq"]


def test_pause_resume_persists_and_never_auto_rearms_microphone(client):
    _patient(client, "P-PAUSE")
    _training_session(client, "S-PAUSE", "P-PAUSE")
    _handshake(client, "S-PAUSE", wseq=100)
    client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-PAUSE", screen="record", recording="armed", selfStart=True, wseq=101)})

    paused = client.post("/sessions/S-PAUSE/pause")
    assert paused.status_code == 200 and paused.json()["status"] == "paused"
    paused_live = client.get("/live/state").json()["cursor"]
    assert paused_live["screen"] == "paused"
    assert paused_live["recording"] == "stopped"
    assert paused_live["selfStart"] is False
    assert paused_live["wseq"] > 101
    paused_runtime = client.get("/sessions/S-PAUSE/runtime").json()["cursor"]
    assert paused_runtime["screen"] == "present"
    assert paused_runtime["recording"] == "idle"
    assert paused_runtime["wseq"] == paused_live["wseq"]

    assert client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-PAUSE", item_idx=1, wseq=paused_live["wseq"] + 1)}).status_code == 409
    assert client.put("/sessions/S-PAUSE/runtime/cursor", json=_cursor(
        "S-PAUSE", item_idx=1, wseq=paused_live["wseq"] + 1)).status_code == 409

    resumed = client.post("/sessions/S-PAUSE/resume")
    assert resumed.status_code == 200 and resumed.json()["status"] == "active"
    resumed_live = client.get("/live/state").json()["cursor"]
    assert resumed_live["screen"] == "present"
    assert resumed_live["recording"] == "idle"
    assert resumed_live["selfStart"] is False
    assert resumed_live["wseq"] > paused_live["wseq"]
    assert resumed.json()["cursor"]["wseq"] == resumed_live["wseq"]

    # 幂等重试不会重复推进 runtime revision。
    revision = resumed.json()["revision"]
    assert client.post("/sessions/S-PAUSE/resume").json()["revision"] == revision


def test_pause_before_first_cursor_still_projects_a_patient_rest_screen(client):
    _patient(client, "P-EARLY-PAUSE")
    _training_session(client, "S-EARLY-PAUSE", "P-EARLY-PAUSE")
    handshake = _handshake(client, "S-EARLY-PAUSE")
    assert handshake.status_code == 200

    paused = client.post("/sessions/S-EARLY-PAUSE/pause")
    assert paused.status_code == 200 and paused.json()["status"] == "paused"
    live = client.get("/live/state").json()
    assert live["cursor"] is None and live["rapportStep"] is None
    assert live["session"]["paused"] is True
    assert live["session"]["wseq"] > handshake.json()["wseq"]

    resumed = client.post("/sessions/S-EARLY-PAUSE/resume")
    assert resumed.status_code == 200 and resumed.json()["status"] == "active"
    live_after = client.get("/live/state").json()
    assert live_after["session"]["paused"] is False
    assert live_after["session"]["wseq"] > live["session"]["wseq"]

    # 更窄的竞态：pause HTTP 比首个 session 握手先到。后到握手仍须投影休息屏。
    _training_session(client, "S-PAUSE-BEFORE-HS", "P-EARLY-PAUSE")
    assert client.post("/sessions/S-PAUSE-BEFORE-HS/pause").status_code == 200
    late_handshake = _handshake(client, "S-PAUSE-BEFORE-HS")
    assert late_handshake.status_code == 200
    late_live = client.get("/live/state").json()
    assert late_live["session"]["sessionId"] == "S-PAUSE-BEFORE-HS"
    assert late_live["session"]["paused"] is True
    assert late_live["cursor"] is None and late_live["rapportStep"] is None


def test_live_payload_contract_rejects_extra_content_and_public_projection_is_minimal(client):
    _patient(client, "P-MIN-LIVE")
    _training_session(client, "S-MIN-LIVE", "P-MIN-LIVE")
    leaked_session = _handshake(client, "S-MIN-LIVE")
    # 重发一份带研究内容的握手：边界应在入库前拒绝，而不是只靠 GET 隐藏。
    rejected_session = client.put("/live/state", json={"kind": "session", "payload": {
        "sessionId": "S-MIN-LIVE", "weekNo": 2, "mode": "task",
        "wseq": 2, "patientName": "不应进入 live",
    }})
    assert leaked_session.status_code == 200 and rejected_session.status_code == 422

    rejected_cursor = client.put("/live/state", json={"kind": "cursor", "payload": {
        **_cursor("S-MIN-LIVE"), "answerText": "敏感回答",
    }})
    assert rejected_cursor.status_code == 422

    accepted = client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-MIN-LIVE")})
    assert accepted.status_code == 200
    public = client.get("/live/state").json()
    assert set(public["session"]) == {
        "sessionId", "weekNo", "eventLine", "mode", "itemBankVersionId", "wseq",
    }
    assert "sourceWseq" not in public["cursor"] and "answerText" not in public["cursor"]


def test_console_state_read_boundary_matches_frontend_exact_parsers(client):
    """P0-4 回归:写路径故意把诊断键 sourceWseq 存进 session_json/cursor_json;
    /live/console-state 若原样返回,前端 exactKeys 解析器会把 session 槽整槽拒收,
    useAudioSaved 因此永远不去拉录音收据账本——录音明明入库,研究者屏零反馈。
    console-state 的每个槽位都必须收敛到与 messages.ts 各解析器一致的白名单。"""
    _patient(client, "P-CONSOLE-PROJ")
    _training_session(client, "S-CONSOLE-PROJ", "P-CONSOLE-PROJ")
    assert _handshake(client, "S-CONSOLE-PROJ").status_code == 200
    assert client.put("/live/state", json={
        "kind": "cursor", "payload": _cursor("S-CONSOLE-PROJ"),
    }).status_code == 200

    # 陷阱确实存在于存储层:诊断键留在 json 里,只有读边界能挡住它。
    with Session(client.test_engine) as s:
        row = s.get(models.LiveState, 1)
        assert "sourceWseq" in json.loads(row.session_json)
        assert "sourceWseq" in json.loads(row.cursor_json)

    state = client.get("/live/console-state").json()
    # 与 web/src/sync/messages.ts parseSession/parseCursor 的 exactKeys 完全对应。
    session_allowed = {"sessionId", "weekNo", "eventLine", "mode",
                       "itemBankVersionId", "paused", "runtimeStatus", "wseq"}
    cursor_allowed = {"sessionId", "screen", "itemIdx", "turnIdx", "responseRole",
                      "cueLevel", "recording", "recSeq", "rawAudioId", "selfStart",
                      "fbKey", "fbItemId", "fbSeq", "wseq"}
    assert set(state["session"]) <= session_allowed, state["session"]
    assert set(state["cursor"]) <= cursor_allowed, state["cursor"]
    assert "sourceWseq" not in state["session"]
    assert "sourceWseq" not in state["cursor"]


def test_runtime_recording_commands_obey_consent_and_review_window_gates(client):
    _patient(client, "P-NOREC", recording_allowed=False)
    denied = client.post("/sessions", json={
        "session_id": "S-NOREC", "patient_id": "P-NOREC", "week_no": 2,
        "phase_type": "正式训练", "event_line": "正式训练",
        "item_bank_version_id": BANK_VERSION,
    })
    assert denied.status_code == 409 and "recording_allowed" in denied.json()["detail"]

    _patient(client, "P-LOCK")
    _training_session(client, "S-LOCK", "P-LOCK")
    _handshake(client, "S-LOCK")
    item = client.post("/sessions/S-LOCK/items", json={
        "item_id": "SE_锚", "task_type": "单要素",
    }).json()
    turn = _authoritative_turn(client, item["id"])
    assert client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-LOCK", item_idx=1)}).status_code == 200
    _login_confirmation_reviewer(client)

    # Human research truth must not mutate while the bedside intervention is
    # still active.  The operational cursor remains writable because no score
    # was prematurely frozen.
    early_confirm = client.patch(f"/turns/{turn['id']}/confirm", json={
        "confirmed_response_text": "锚",
        "expected_revision": 0,
        "idempotency_key": "test-presence-confirm-active-0001",
    })
    early_lock = client.patch(f"/turns/{turn['id']}/lock", json={
        "reviewer_id": "R1", "element_value": 1, "prompt_level": 0,
    })
    for response in (early_confirm, early_lock):
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == (
            "research_review_requires_intervention_completion")
        assert response.json()["detail"]["runtime_status"] == "active"

    assert client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-LOCK", item_idx=2, recording="armed")}).status_code == 200
    assert client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-LOCK", item_idx=2)}).status_code == 200
    assert client.post("/sessions/S-LOCK/pause").status_code == 200
    paused_confirm = client.patch(f"/turns/{turn['id']}/confirm", json={
        "confirmed_response_text": "暂停期改写",
        "expected_revision": 0,
        "idempotency_key": "test-presence-confirm-paused-0001",
    })
    assert paused_confirm.status_code == 409
    assert paused_confirm.json()["detail"] == {
        "code": "research_review_requires_intervention_completion",
        "message": "须先由服务端完成床旁干预并生成不可变汇总，当前状态为 paused；禁止修改研究确认回答",
        "runtime_status": "paused",
    }
    assert client.post("/sessions/S-LOCK/resume").status_code == 200


def test_relationship_pointer_is_persisted_and_pause_safe(client):
    _patient(client, "P-RAP")
    response = client.post("/sessions", json={
        "session_id": "S-RAP", "patient_id": "P-RAP", "week_no": 1,
        "phase_type": "关系建立", "event_line": "关系建立环节",
        "item_bank_version_id": BANK_VERSION, "is_simulation": True,
    })
    assert response.status_code == 200
    assert _handshake(client, "S-RAP", mode="rapport", wseq=100).status_code == 200
    rapport = {"sessionId": "S-RAP", "sectionKey": "自我介绍", "questionIdx": 1,
               "recording": "armed", "recSeq": 1,
               "containsDirectIdentifier": True, "wseq": 101}
    rapport_write = client.put("/live/state", json={"kind": "rapportStep", "payload": rapport})
    assert rapport_write.status_code == 200
    saved = client.get("/sessions/S-RAP/runtime").json()["rapportStep"]
    assert saved["sectionKey"] == "自我介绍" and saved["questionIdx"] == 1
    assert saved["recording"] == "idle"
    assert saved["wseq"] == rapport_write.json()["wseq"]

    client.post("/sessions/S-RAP/pause")
    paused = client.get("/live/state").json()["rapportStep"]
    assert paused["paused"] is True and paused["recording"] == "stopped"
    client.post("/sessions/S-RAP/resume")
    resumed = client.get("/live/state").json()["rapportStep"]
    assert "paused" not in resumed and resumed["recording"] == "idle"


def test_server_wseq_is_monotonic_and_late_cross_session_cursor_is_rejected(client):
    _patient(client, "P-SEQ")
    _training_session(client, "S-SEQ-A", "P-SEQ")
    _training_session(client, "S-SEQ-B", "P-SEQ")

    first = _handshake(client, "S-SEQ-A", wseq=9_999_999_999_999_999)
    assert first.status_code == 200
    first_server_wseq = client.get("/live/state").json()["session"]["wseq"]
    # 服务端必须"重签"而不是"采纳"客户端序号:若采纳了这个天文数字,单调性断言
    # 依然全绿,快钟/恶意客户端就能永久压制之后所有真命令——单调性测不出这种回归。
    assert first_server_wseq < 9_999_999_999_999_999
    low = client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-SEQ-A", wseq=1)})
    lower = client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-SEQ-A", item_idx=1, wseq=0)})
    assert low.status_code == lower.status_code == 200
    assert first_server_wseq < low.json()["wseq"] < lower.json()["wseq"]
    assert client.get("/live/state").json()["cursor"]["wseq"] == lower.json()["wseq"]

    second = _handshake(client, "S-SEQ-B", wseq=0)
    assert second.status_code == 200
    switched = client.get("/live/state").json()
    assert switched["session"]["sessionId"] == "S-SEQ-B"
    assert switched["session"]["wseq"] > lower.json()["wseq"]

    # A 场旧请求迟到时必须拒绝，不能借当前 B 场上下文污染任一 live slot。
    late = client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-SEQ-A", item_idx=2, wseq=99_999_999_999_999_999)})
    assert late.status_code == 409
    assert client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        item_idx=2)}).status_code == 422
    unchanged = client.get("/live/state").json()
    assert unchanged["session"]["sessionId"] == "S-SEQ-B" and unchanged["cursor"] is None

    # 切回 A 时 session 和恢复游标都由服务端重新签发，均高于切场前全部快照。
    assert _handshake(client, "S-SEQ-A", wseq=0).status_code == 200
    restored = client.get("/live/state").json()
    assert restored["session"]["wseq"] > switched["session"]["wseq"]
    assert restored["cursor"]["wseq"] > restored["session"]["wseq"]
    assert restored["cursor"]["itemIdx"] == 1


def test_audio_turn_key_recovers_without_turn_event_and_rejects_unbound_keys(client):
    _patient(client, "P-AUDIO-MAP")
    _training_session(client, "S-AUDIO-MAP", "P-AUDIO-MAP")
    audio = client.post("/audio", json={
        "raw_audio_id": "audio-map", "session_id": "S-AUDIO-MAP", "turn_key": "SE_锚#1",
    })
    assert audio.status_code == 200 and audio.json()["turn_key"] == "SE_锚#1"
    journal = client.get("/sessions/S-AUDIO-MAP/journal").json()
    assert journal["turns"] == []
    assert journal["audios"][0]["turn_key"] == "SE_锚#1"
    assert client.post("/audio", json={
        "raw_audio_id": "audio-bad", "session_id": "S-AUDIO-MAP", "turn_key": "任意文本",
    }).status_code == 422

    relationship = client.post("/sessions", json={
        "session_id": "S-AUDIO-RAP", "patient_id": "P-AUDIO-MAP", "week_no": 1,
        "phase_type": "关系建立", "event_line": "关系建立环节",
        "item_bank_version_id": BANK_VERSION, "is_simulation": True,
    })
    assert relationship.status_code == 200
    assert client.post("/audio", json={
        "raw_audio_id": "audio-rap", "session_id": "S-AUDIO-RAP",
        "turn_key": "关系建立·自我介绍",
    }).status_code == 200
    assert client.post("/audio", json={
        "raw_audio_id": "audio-rap-bad", "session_id": "S-AUDIO-RAP",
        "turn_key": "关系建立·不存在",
    }).status_code == 422


def test_runtime_cursor_expected_revision_blocks_stale_writer(client):
    """旧标签页/旧设备不能凭过期 revision 把游标倒回旧题(乐观并发守卫)。"""
    _patient(client, "P-REV")
    _training_session(client, "S-REV", "P-REV")
    _handshake(client, "S-REV", wseq=100)
    first = client.put("/sessions/S-REV/runtime/cursor", json=_cursor("S-REV", item_idx=2, wseq=101))
    assert first.status_code == 200
    current_revision = first.json()["revision"]

    # 过期 revision 一律 409,游标不动
    stale = client.put("/sessions/S-REV/runtime/cursor", json={
        **_cursor("S-REV", item_idx=0, wseq=102), "expected_revision": current_revision - 1,
    })
    assert stale.status_code == 409
    assert client.get("/sessions/S-REV/runtime").json()["cursor"]["itemIdx"] == 2

    # 带当前 revision 的写入放行
    fresh = client.put("/sessions/S-REV/runtime/cursor", json={
        **_cursor("S-REV", item_idx=3, wseq=103), "expected_revision": current_revision,
    })
    assert fresh.status_code == 200
    assert client.get("/sessions/S-REV/runtime").json()["cursor"]["itemIdx"] == 3


def test_patient_presence_cleared_on_session_switch(client):
    """换场必须清老人端在场信息:上一位受试者的'在线/正在看题'绝不能串到新场次。"""
    _patient(client, "P-SWITCH")
    _training_session(client, "S-SW-1", "P-SWITCH")
    _training_session(client, "S-SW-2", "P-SWITCH")
    _handshake(client, "S-SW-1", wseq=100)
    assert client.post("/live/patient-heartbeat", json={
        "session_id": "S-SW-1", "screen": "present",
    }).status_code == 200
    assert client.get("/live/console-state").json()["patientPresence"]["online"] is True

    _handshake(client, "S-SW-2", wseq=200)
    presence = client.get("/live/console-state").json()["patientPresence"]
    assert presence["session_id"] is None
    assert presence["online"] is False and presence["screen"] is None
    # 旧场次的迟到心跳也进不来
    assert client.post("/live/patient-heartbeat", json={
        "session_id": "S-SW-1", "screen": "present",
    }).status_code == 409


def test_runtime_writes_for_non_live_session_do_not_touch_live_row(client):
    """非当前 live 场次的 runtime 写入/暂停只落自己的恢复行,不得污染老人端正在看的场次。"""
    _patient(client, "P-ISO")
    _training_session(client, "S-ISO-LIVE", "P-ISO")
    _training_session(client, "S-ISO-BG", "P-ISO")
    _handshake(client, "S-ISO-LIVE", wseq=100)
    live_cursor = client.put("/live/state", json={"kind": "cursor", "payload": _cursor(
        "S-ISO-LIVE", item_idx=1, wseq=101)})
    assert live_cursor.status_code == 200
    before = client.get("/live/state").json()

    assert client.put("/sessions/S-ISO-BG/runtime/cursor", json=_cursor(
        "S-ISO-BG", item_idx=4, wseq=102)).status_code == 200
    assert client.post("/sessions/S-ISO-BG/pause").status_code == 200

    after = client.get("/live/state").json()
    assert after["session"]["sessionId"] == "S-ISO-LIVE"
    assert after["seq"] == before["seq"]                      # live 没被当成新命令推进
    assert after["cursor"]["itemIdx"] == 1                    # 老人端画面不动
    assert "paused" not in after["session"] or after["session"]["paused"] is not True
    assert client.get("/sessions/S-ISO-BG/runtime").json()["status"] == "paused"


def test_forward_alembic_upgrade_preserves_existing_live_row(tmp_path):
    db_path = tmp_path / "forward.db"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "794480f21ed4")

    eng = sa_create_engine(f"sqlite:///{db_path}")
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO livestate "
            "(id, seq, session_json, cursor_json, rapport_json, audio_json, patient_rec_json, updated_at) "
            "VALUES (1, 7, :session, :cursor, NULL, NULL, NULL, NULL)"
        ), {"session": '{"sessionId":"legacy"}', "cursor": '{"itemIdx":2,"turnIdx":0}'})

    command.upgrade(config, "head")
    columns = {col["name"] for col in inspect(eng).get_columns("livestate")}
    assert {"command_wseq", "patient_ack_session_id", "patient_current_screen",
            "patient_last_seen_at", "patient_ack_seq"} <= columns
    assert "sessionruntimestate" in inspect(eng).get_table_names()
    assert "turn_key" in {col["name"] for col in inspect(eng).get_columns("audioassetrow")}
    assert "is_simulation" in {col["name"] for col in inspect(eng).get_columns("session")}
    assert "is_simulation" in {col["name"] for col in inspect(eng).get_columns("audioassetrow")}
    assert "is_simulation_subject" in {
        col["name"] for col in inspect(eng).get_columns("patient")}
    session_columns = {col["name"]: col for col in inspect(eng).get_columns("session")}
    audio_columns = {col["name"]: col for col in inspect(eng).get_columns("audioassetrow")}
    assert "data_classification" in session_columns
    assert "data_classification" in audio_columns
    assert "legacy_unknown" in str(session_columns["data_classification"]["default"])
    assert "legacy_unknown" in str(audio_columns["data_classification"]["default"])
    assert {"attemptevent", "interactionevent",
            "interactionpresentationreceipt"} <= set(
                inspect(eng).get_table_names())
    attempt_uniques = {constraint["name"] for constraint in inspect(eng).get_unique_constraints(
        "attemptevent")}
    interaction_uniques = {constraint["name"] for constraint in inspect(eng).get_unique_constraints(
        "interactionevent")}
    assert {"uq_attempt_raw_audio_id", "uq_attempt_session_item_turn_seq"} <= attempt_uniques
    assert "uq_interaction_session_event_seq" in interaction_uniques
    receipt_uniques = {
        constraint["name"] for constraint in inspect(eng).get_unique_constraints(
            "interactionpresentationreceipt")
    }
    assert {
        "uq_interaction_presentation_session_idempotency",
        "uq_interaction_presentation_event",
        "uq_interaction_presentation_attempt",
    } <= receipt_uniques
    assert {"intervention_completed_at", "intervention_ended_by",
            "completed_at", "aborted_at", "ended_by", "end_reason"} <= {
        col["name"] for col in inspect(eng).get_columns("sessionruntimestate")
    }
    with eng.connect() as conn:
        row = conn.execute(text("SELECT seq, session_json, cursor_json FROM livestate WHERE id=1")).one()
    assert row.seq == 7 and "legacy" in row.session_json and "itemIdx" in row.cursor_json


def test_intervention_completion_migration_backfills_existing_completed_session(tmp_path):
    db_path = tmp_path / "completion-backfill.db"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "f7a4c9d2e531")

    eng = sa_create_engine(f"sqlite:///{db_path}")
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO patient (patient_id, is_simulation_subject) "
            "VALUES ('P-MIG', 1)"
        ))
        conn.execute(text(
            "INSERT INTO session "
            "(session_id, patient_id, session_sitting_no, week_no, phase_type, "
            "event_line, item_bank_version_id, is_simulation, data_classification) "
            "VALUES ('S-MIG', 'P-MIG', 1, 2, '正式训练', '正式训练', 'bank-v1', 1, 'simulation')"
        ))
        conn.execute(text(
            "INSERT INTO sessionruntimestate "
            "(session_id, status, revision, completed_at) "
            "VALUES ('S-MIG', 'completed', 1, '2026-07-17 12:34:56')"
        ))

    command.upgrade(config, "head")
    with eng.connect() as conn:
        row = conn.execute(text(
            "SELECT completed_at, intervention_completed_at, intervention_ended_by "
            "FROM sessionruntimestate WHERE session_id = 'S-MIG'"
        )).one()
    assert row.intervention_completed_at == row.completed_at
    assert row.intervention_ended_by is None
