from dataclasses import dataclass

from app.runtime import PlanItem, PlanTurn, SessionPlan
from app.session_completion import (
    assess_completion_with_audio,
    assess_intervention_completion,
    assess_locked_research_truth,
)


@dataclass
class Item:
    id: int | None
    item_id: str
    task_type: str


@dataclass
class Turn:
    item_event_id: int
    turn_seq: int
    response_role: str | None
    score_locked: bool
    confirmed_response_text: str | None
    raw_audio_id: str | None = None
    source_attempt_id: int | None = None


@dataclass
class Audio:
    raw_audio_id: str
    session_id: str | None
    turn_key: str | None
    withdrawn: bool = False
    withdrawal_status: str | None = None
    is_simulation: bool = False
    data_classification: str = "research"


@dataclass
class Attempt:
    id: int
    session_id: str
    item_id: str
    turn_seq: int
    response_role: str
    raw_audio_id: str
    processing_status: str


def _plan() -> SessionPlan:
    return SessionPlan(
        item_bank_version_id="bank-v1",
        week_no=2,
        event_line="正式训练",
        items=(
            PlanItem("A", "单要素", None, 1, turns=(PlanTurn(1, "命名"),)),
            PlanItem("B", "双要素", None, 2, turns=(
                PlanTurn(1, "左命名"), PlanTurn(2, "关系识别"),
            )),
        ),
    )


def test_complete_only_when_every_planned_turn_has_one_locked_truth():
    result = assess_locked_research_truth(
        _plan(),
        [Item(10, "A", "单要素"), Item(20, "B", "双要素")],
        [
            Turn(10, 1, "命名", True, "苹果"),
            Turn(20, 1, "左命名", True, "杯子"),
            Turn(20, 2, "关系识别", True, "用杯子喝水"),
        ],
    )
    assert result.ready is True
    assert result.expected_turns == result.matched_turns == result.locked_turns == 3
    assert result.issues == ()


def test_missing_and_unlocked_turns_fail_closed():
    result = assess_locked_research_truth(
        _plan(),
        [Item(10, "A", "单要素"), Item(20, "B", "双要素")],
        [Turn(10, 1, "命名", False, "苹果")],
    )
    assert result.ready is False
    assert {issue.code for issue in result.issues} == {"unlocked_turn", "missing_turn"}
    assert result.locked_turns == 0


def test_duplicate_or_plan_mismatched_rows_block_completion():
    result = assess_locked_research_truth(
        _plan(),
        [
            Item(10, "A", "双要素"),
            Item(20, "B", "双要素"),
            Item(21, "B", "双要素"),
            Item(30, "OUTSIDE", "单要素"),
        ],
        [
            Turn(10, 1, "命名", True, "苹果"),
            Turn(10, 9, "计划外", True, "x"),
        ],
    )
    assert result.ready is False
    codes = {issue.code for issue in result.issues}
    assert {"unexpected_item", "task_type_mismatch", "unexpected_turn", "duplicate_item"} <= codes


def test_duplicate_turn_and_corrupt_locked_turn_block_completion():
    result = assess_locked_research_truth(
        _plan(),
        [Item(10, "A", "单要素"), Item(20, "B", "双要素")],
        [
            Turn(10, 1, "命名", True, "苹果"),
            Turn(10, 1, "命名", True, "苹果"),
            Turn(20, 1, "左命名", True, None),
            Turn(20, 2, "关系识别", True, "关系"),
        ],
    )
    assert result.ready is False
    assert {"duplicate_turn", "locked_without_confirmation"} <= {issue.code for issue in result.issues}


def test_empty_scoring_plan_is_not_treated_as_automatically_complete():
    empty = SessionPlan("bank-v1", 1, "关系建立环节", items=())
    result = assess_locked_research_truth(empty, [], [])
    assert result.ready is False
    assert result.issues[0].code == "unsupported_empty_plan"


def test_completion_requires_bound_existing_audio_for_every_planned_turn():
    items = [Item(10, "A", "单要素"), Item(20, "B", "双要素")]
    turns = [
        Turn(10, 1, "命名", True, "苹果", "audio-a", 1),
        Turn(20, 1, "左命名", True, "杯子", "audio-b1", 2),
        Turn(20, 2, "关系识别", True, "用杯子喝水", "audio-b2", 3),
    ]
    audios = [
        Audio("audio-a", "S", "A#1"),
        Audio("audio-b1", "S", "B#1"),
        Audio("audio-b2", "S", "B#2"),
    ]
    result = assess_completion_with_audio(
        _plan(), items, turns, audios, [
            Attempt(1, "S", "A", 1, "命名", "audio-a", "completed"),
            Attempt(2, "S", "B", 1, "左命名", "audio-b1", "completed"),
            Attempt(3, "S", "B", 2, "关系识别", "audio-b2", "completed"),
        ], session_id="S",
        is_simulation=False, data_classification="research",
        blob_exists=lambda raw_audio_id: raw_audio_id in {"audio-a", "audio-b1", "audio-b2"},
    )
    assert result.ready is True
    assert result.audio_evidenced_turns == result.expected_turns == 3


def test_simulation_completion_uses_same_audio_evidence_gate():
    plan = SessionPlan(
        "bank-v1", 2, "正式训练",
        items=(PlanItem("A", "单要素", None, 1,
                        turns=(PlanTurn(1, "命名"),)),),
    )
    result = assess_completion_with_audio(
        plan,
        [Item(10, "A", "单要素")],
        [Turn(10, 1, "命名", True, "苹果", "sim-audio", 1)],
        [Audio("sim-audio", "S-SIM", "A#1", is_simulation=True,
               data_classification="simulation")],
        [Attempt(1, "S-SIM", "A", 1, "命名", "sim-audio", "completed")],
        session_id="S-SIM", is_simulation=True,
        data_classification="simulation", blob_exists=lambda _raw: True,
    )
    assert result.ready is True and result.audio_evidenced_turns == 1


def test_completion_rejects_missing_mismatched_or_technical_failure_audio():
    items = [Item(10, "A", "单要素"), Item(20, "B", "双要素")]
    turns = [
        Turn(10, 1, "命名", True, "苹果", None),
        Turn(20, 1, "左命名", True, "杯子", "audio-wrong", 2),
        Turn(20, 2, "关系识别", True, "关系", "audio-failed", 3),
    ]
    audios = [
        Audio("audio-wrong", "OTHER", "B#1"),
        Audio("audio-failed", "S", "B#2"),
    ]
    result = assess_completion_with_audio(
        _plan(), items, turns, audios, [
            Attempt(2, "S", "B", 1, "左命名", "audio-wrong", "completed"),
            Attempt(3, "S", "B", 2, "关系识别", "audio-failed", "technical_failure"),
        ],
        session_id="S", is_simulation=False, data_classification="research",
        blob_exists=lambda _raw_audio_id: True,
    )
    codes = {issue.code for issue in result.issues}
    assert {"missing_audio_reference", "audio_binding_mismatch",
            "technical_failure_attempt"} <= codes
    assert result.ready is False and result.audio_evidenced_turns == 0


def test_intervention_completion_does_not_wait_for_human_truth_locking():
    items = [Item(10, "A", "单要素"), Item(20, "B", "双要素")]
    turns = [
        Turn(10, 1, "命名", False, None, "audio-a", 1),
        Turn(20, 1, "左命名", False, None, "audio-b1", 2),
        Turn(20, 2, "关系识别", False, None, "audio-b2", 3),
    ]
    audios = [
        Audio("audio-a", "S", "A#1"),
        Audio("audio-b1", "S", "B#1"),
        Audio("audio-b2", "S", "B#2"),
    ]
    attempts = [
        Attempt(1, "S", "A", 1, "命名", "audio-a", "completed"),
        Attempt(2, "S", "B", 1, "左命名", "audio-b1", "completed"),
        Attempt(3, "S", "B", 2, "关系识别", "audio-b2", "completed"),
    ]

    operational = assess_intervention_completion(
        _plan(), items, turns, audios, attempts,
        session_id="S", is_simulation=False, data_classification="research",
        blob_exists=lambda _raw_audio_id: True,
    )
    research = assess_completion_with_audio(
        _plan(), items, turns, audios, attempts,
        session_id="S", is_simulation=False, data_classification="research",
        blob_exists=lambda _raw_audio_id: True,
    )

    assert operational.ready is True
    assert operational.matched_turns == 3
    assert operational.completed_attempt_turns == 3
    assert operational.audio_evidenced_turns == 3
    assert research.ready is False
    assert {issue.code for issue in research.issues} == {"unlocked_turn"}


def test_intervention_completion_keeps_plan_attempt_and_audio_gates_closed():
    result = assess_intervention_completion(
        _plan(),
        [Item(10, "A", "单要素"), Item(20, "B", "双要素")],
        [
            Turn(10, 1, "命名", False, None, "audio-a", 1),
            Turn(20, 1, "左命名", False, None, "audio-b1", 2),
            Turn(20, 2, "关系识别", False, None, "audio-b2", 3),
            Turn(20, 9, "计划外", False, None, "audio-extra", 4),
        ],
        [
            Audio("audio-a", "S", "A#1"),
            Audio("audio-b1", "OTHER", "B#1"),
            Audio("audio-b2", "S", "B#2"),
        ],
        [
            Attempt(1, "S", "A", 1, "命名", "audio-a", "completed"),
            Attempt(2, "S", "B", 1, "左命名", "audio-b1", "completed"),
            Attempt(3, "S", "B", 2, "关系识别", "audio-b2", "technical_failure"),
        ],
        session_id="S", is_simulation=False, data_classification="research",
        blob_exists=lambda _raw_audio_id: True,
    )

    codes = {issue.code for issue in result.issues}
    assert {"unexpected_turn", "audio_binding_mismatch", "technical_failure_attempt"} <= codes
    assert result.ready is False
    assert result.completed_attempt_turns == 2
    assert result.audio_evidenced_turns == 1
