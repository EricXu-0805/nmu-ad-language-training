"""Starting at a frozen item records omissions without manufacturing answers."""
import copy
from dataclasses import replace

import pytest
from sqlmodel import Session, select

from app import asr, content
from app.main import _run_p0a_attempt_worker
from app.models import (
    AttemptEvent, AutopilotControlEvent, AutopilotPositionAdjudication,
    ItemEvent, ResearchUser, RuntimeCommand, SessionOutcomeSummary, SessionRuntimeState, TurnEvent,
)
from test_autopilot_api import (
    BANK, SESSION_ID, TWO_ONLY_BANK, _WorkerAsr, _ack_body, _bind_api_bank,
    _device_next, _drive_issued_question_to_processing_attempt, _enable_p0a, _start,
    _interaction_subset_package, api_clients as api_clients,
)


def _selection(**overrides):
    return {
        "start_presentation_order": 2,
        "skip_reason_code": "trained_in_prior_sitting",
        "skip_note": "上场已完成，按现场记录继续",
        **overrides,
    }


def test_selected_start_is_atomic_and_exact_replay_does_not_duplicate(api_clients, monkeypatch):
    _enable_p0a(monkeypatch)
    _bind_api_bank(api_clients, monkeypatch, TWO_ONLY_BANK)
    started = _start(api_clients, **_selection())
    assert started.status_code == 200, started.text
    question = _device_next(api_clients)
    assert started.json()["position_item_id"] == TWO_ONLY_BANK.single_element[1]["item_id"]
    assert question["turn_seq"] == 1 and question["attempt_seq"] == 1
    replay = _start(api_clients, **_selection())
    assert replay.status_code == 200, replay.text
    assert replay.json() == started.json()
    with Session(api_clients.engine) as db:
        skipped = db.exec(select(AutopilotPositionAdjudication)).one()
        assert (skipped.item_id, skipped.turn_seq, skipped.presentation_order) == (
            TWO_ONLY_BANK.single_element[0]["item_id"], 1, 1)
        assert (skipped.kind, skipped.reason_code, skipped.note) == (
            "skipped", "trained_in_prior_sitting", "上场已完成，按现场记录继续")
        assert skipped.actor_id and skipped.source_attempt_id is None and skipped.turn_event_id is None
        assert len(list(db.exec(select(RuntimeCommand)))) == 1
        assert len(list(db.exec(select(AutopilotControlEvent)))) == 1
        for model in (ItemEvent, TurnEvent, AttemptEvent):
            assert list(db.exec(select(model))) == []


@pytest.mark.parametrize("changed", [
    {"start_presentation_order": 1, "skip_reason_code": None, "skip_note": None},
    {"skip_reason_code": "other"},
    {"skip_note": "与原启动不同的说明"},
])
def test_same_start_key_cannot_retarget_or_rewrite_reason(api_clients, monkeypatch, changed):
    _enable_p0a(monkeypatch)
    _bind_api_bank(api_clients, monkeypatch, TWO_ONLY_BANK)
    assert _start(api_clients, **_selection()).status_code == 200
    before = _device_next(api_clients)
    rejected = _start(api_clients, **_selection(**changed))
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "autopilot_idempotency_conflict"
    assert _device_next(api_clients) == before
    with Session(api_clients.engine) as db:
        assert len(list(db.exec(select(AutopilotPositionAdjudication)))) == 1


@pytest.mark.parametrize("selection", [
    _selection(start_presentation_order=0),
    _selection(start_presentation_order=True),
    _selection(start_presentation_order=1.5),
    _selection(start_presentation_order="2"),
    _selection(start_presentation_order=3),
    _selection(skip_reason_code=None),
    _selection(skip_reason_code="technical_retry"),
    _selection(skip_note="a\u200bb"),
    _selection(skip_note="a\nb"),
    _selection(skip_note="字" * 201),
    _selection(start_presentation_order=1),
])
def test_invalid_selection_leaves_no_command_or_skip_receipt(api_clients, monkeypatch, selection):
    _enable_p0a(monkeypatch)
    _bind_api_bank(api_clients, monkeypatch, TWO_ONLY_BANK)
    rejected = _start(api_clients, **selection)
    assert rejected.status_code in (409, 422), rejected.text
    with Session(api_clients.engine) as db:
        for model in (RuntimeCommand, AutopilotPositionAdjudication, AutopilotControlEvent):
            assert list(db.exec(select(model))) == []


def test_last_item_start_finishes_with_only_practised_turn_in_outcome(api_clients, monkeypatch):
    _enable_p0a(monkeypatch)
    _bind_api_bank(api_clients, monkeypatch, TWO_ONLY_BANK)
    assert _start(api_clients, **_selection()).status_code == 200
    _drive_issued_question_to_processing_attempt(api_clients, ack_prefix="selected-start")
    monkeypatch.setattr(asr, "get_engine", lambda: _WorkerAsr(
        text=TWO_ONLY_BANK.single_element[1]["target_word"]))
    _run_p0a_attempt_worker(SESSION_ID)
    feedback = _device_next(api_clients)
    assert feedback["payload"]["purpose"] == "feedback"
    ended = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/{feedback['command_key']}/acks",
        headers=api_clients.device_headers,
        json=_ack_body(feedback, ack_type="tts_ended", ack_key="selected-start-finish",
                       device_event_seq=3, media_ended=True, media_duration_ms=900))
    assert ended.status_code == 200, ended.text
    assert ended.json()["status"] == "scope_completed"
    with Session(api_clients.engine) as db:
        assert db.get(SessionRuntimeState, SESSION_ID).status == "intervention_completed"
        summary = db.get(SessionOutcomeSummary, SESSION_ID)
        assert (summary.expected_turns, summary.matched_turns,
                summary.completed_attempt_turns, summary.audio_evidenced_turns) == (1, 1, 1, 1)
        assert len(list(db.exec(select(AttemptEvent)))) == 1
        assert len(list(db.exec(select(AutopilotPositionAdjudication)))) == 1


def test_long_start_key_produces_bounded_multi_turn_skip_receipts(api_clients, monkeypatch):
    _enable_p0a(monkeypatch)
    bank = replace(BANK, single_element=BANK.single_element[:1],
                   double_element=BANK.double_element[:2], multi_element=[],
                   meta={**BANK.meta, "source_protocol_position_count": 11,
                         "source_unstructured_positions": []})
    _bind_api_bank(api_clients, monkeypatch, bank)
    package = _interaction_subset_package(bank)
    monkeypatch.setattr(content, "load_autopilot_interaction_package",
                        lambda *args, **kwargs: copy.deepcopy(package))
    started = _start(api_clients, idempotency_key="x" * 128,
                     **_selection(start_presentation_order=3, skip_reason_code="other", skip_note=""))
    assert started.status_code == 200, started.text
    with Session(api_clients.engine) as db:
        skipped = list(db.exec(select(AutopilotPositionAdjudication)))
        assert {row.presentation_order for row in skipped} == {1, 2}
        assert sum(row.presentation_order == 1 for row in skipped) == 1
        assert sum(row.presentation_order == 2 for row in skipped) == 5
        assert len({row.idempotency_key for row in skipped}) == 6
        assert all(len(row.idempotency_key) <= 128 and row.note is None for row in skipped)
        assert list(db.exec(select(AttemptEvent))) == []


@pytest.mark.parametrize("principal", ["device", "anonymous", "caregiver"])
def test_start_position_is_researcher_control_only(api_clients, monkeypatch, principal):
    _enable_p0a(monkeypatch)
    _bind_api_bank(api_clients, monkeypatch, TWO_ONLY_BANK)
    client = api_clients.account
    headers = {}
    if principal == "caregiver":
        with Session(api_clients.engine) as db:
            user = db.exec(select(ResearchUser)).one()
            user.role = "caregiver_operator"
            db.add(user)
            db.commit()
    elif principal == "device":
        client, headers = api_clients.device, api_clients.device_headers
    else:
        client = api_clients.anonymous
    denied = client.post(f"/sessions/{SESSION_ID}/autopilot/start", headers=headers,
                         json={"idempotency_key": "selected-start-role", "expected_revision": 0,
                               **_selection()})
    assert denied.status_code in (401, 403), denied.text
    with Session(api_clients.engine) as db:
        assert list(db.exec(select(RuntimeCommand))) == []
        assert list(db.exec(select(AutopilotPositionAdjudication))) == []
