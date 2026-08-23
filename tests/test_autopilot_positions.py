from __future__ import annotations

from dataclasses import replace

from app import autopilot_positions, content


BANK = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")


def test_current_bank_traverses_every_sequentially_complete_position_before_first_gap():
    positions = autopilot_positions.build_positions(
        BANK, week_no=2, event_line="正式训练")
    # 2026-08-19 起题库含 20 单要素 + 10 双要素(各 5 轮) + 2 多要素(各 4 轮)。
    assert len(positions) == 20 + 10 * 5 + 2 * 4

    first = autopilot_positions.first_position_decision(
        BANK, week_no=2, event_line="正式训练")
    assert first.position == positions[0]
    assert first.gap is None

    current = first.position
    assert current is not None
    for expected in positions[1:20]:
        decision = autopilot_positions.next_position_decision(
            BANK,
            week_no=2,
            event_line="正式训练",
            current_item_id=current.item_id,
            current_turn_seq=current.turn_seq,
        )
        assert decision.position == expected
        assert decision.gap is None
        current = expected

    blocked = autopilot_positions.next_position_decision(
        BANK,
        week_no=2,
        event_line="正式训练",
        current_item_id=current.item_id,
        current_turn_seq=current.turn_seq,
    )
    assert blocked.position is None
    assert blocked.gap is not None
    assert blocked.gap.position == positions[20]
    assert blocked.gap.code == "interaction_package_unavailable"
    assert "DE_烟灰缸+烟#1:左命名" in blocked.gap.detail
    assert "缺本周自动交互数据包" in blocked.gap.detail


def test_gap_is_not_skipped_when_a_later_turn_has_a_rubric():
    first_double = dict(BANK.double_element[0])
    first_double["operational_rubrics"] = {
        "左作用": {
            "rubric_version": "test-r1",
            "decision_policy": "any_acceptable_expression",
            "acceptable_expressions": ["测试表达"],
            "required_concepts": [],
            "cues": {"1": "线索一", "2": "线索二"},
            "tell_answer": "测试答案",
        },
    }
    bank = replace(
        BANK,
        single_element=[],
        double_element=[first_double],
        multi_element=[],
    )
    first = autopilot_positions.first_position_decision(
        bank, week_no=2, event_line="正式训练")
    assert first.gap is not None
    assert first.gap.position.response_role == "左命名"
    assert first.gap.code == "interaction_package_unavailable"

    later = autopilot_positions.decision_for_position(
        bank,
        autopilot_positions.build_positions(
            bank, week_no=2, event_line="正式训练")[1],
    )
    assert later.gap is not None
    assert later.gap.position.response_role == "左作用"
    assert later.gap.code == "interaction_package_unavailable"


def test_incomplete_single_position_reports_exact_missing_frozen_fields():
    broken = dict(BANK.single_element[0])
    broken["cues"] = {"1": broken["cues"]["1"]}
    bank = replace(
        BANK,
        single_element=[broken],
        double_element=[],
        multi_element=[],
    )
    decision = autopilot_positions.first_position_decision(
        bank, week_no=2, event_line="正式训练")
    assert decision.gap is not None
    assert decision.gap.code == "source_field_unavailable"
    assert "cue2.text" in decision.gap.detail
    assert "cue2.cue_type" in decision.gap.detail


def test_single_position_requires_all_three_source_cue1_paths():
    broken = dict(BANK.single_element[0])
    broken_cues = {key: dict(value) for key, value in broken["cues"].items()}
    broken_cue1 = broken_cues["1"]
    broken_cue1["variants"] = dict(broken_cue1["variants"])
    broken_cue1["variants"].pop("silence")
    broken["cues"] = broken_cues
    bank = replace(
        BANK,
        single_element=[broken],
        double_element=[],
        multi_element=[],
    )

    decision = autopilot_positions.first_position_decision(
        bank, week_no=2, event_line="正式训练")
    assert decision.gap is not None
    assert decision.gap.code == "source_field_unavailable"
    assert "cue1.variants.silence.text" in decision.gap.detail
    assert "cue1.variants.silence.source_paragraph_index" in decision.gap.detail


def test_last_supported_position_completes_without_fabricating_a_successor():
    bank = replace(
        BANK,
        single_element=[BANK.single_element[0]],
        double_element=[],
        multi_element=[],
    )
    decision = autopilot_positions.next_position_decision(
        bank,
        week_no=2,
        event_line="正式训练",
        current_item_id=BANK.single_element[0]["item_id"],
        current_turn_seq=1,
    )
    assert decision.completed is True
    assert decision.position is None and decision.gap is None


# ---------------------------------------------------------------------------
# 交互数据包接入后的就绪判定(autopilot-interaction.v1 消费侧)。
# ---------------------------------------------------------------------------

PROTOCOL = content.load_autopilot_protocol(
    content.CONTENT_DIR / "autopilot_protocol_v1.json")
PACKAGE = content.load_autopilot_interaction_package(2, protocol=PROTOCOL)


def test_all_78_positions_are_runnable_with_the_shipped_interaction_package():
    assert content.validate_autopilot_interaction_package(
        PACKAGE, BANK, PROTOCOL) == []
    positions = autopilot_positions.build_positions(
        BANK, week_no=2, event_line="正式训练")
    assert len(positions) == 78
    gaps = [
        gap for position in positions
        if (gap := autopilot_positions.readiness_gap(
            BANK, position, interaction_package=PACKAGE)) is not None
    ]
    assert gaps == []


def test_double_and_multi_without_package_report_independent_gap_code():
    positions = autopilot_positions.build_positions(
        BANK, week_no=2, event_line="正式训练")
    double_first = positions[20]
    assert double_first.task_type == "双要素"
    gap = autopilot_positions.readiness_gap(BANK, double_first)
    assert gap is not None
    assert gap.code == "interaction_package_unavailable"
    multi_first = next(
        position for position in positions if position.task_type == "多要素")
    gap = autopilot_positions.readiness_gap(BANK, multi_first)
    assert gap is not None
    assert gap.code == "interaction_package_unavailable"


def test_single_element_positions_ignore_the_package_parameter():
    positions = autopilot_positions.build_positions(
        BANK, week_no=2, event_line="正式训练")
    single = positions[0]
    assert single.task_type == "单要素"
    assert autopilot_positions.readiness_gap(BANK, single) is None
    assert autopilot_positions.readiness_gap(
        BANK, single, interaction_package=PACKAGE) is None


def test_position_not_covered_by_package_turn_fails_closed():
    import copy as _copy

    tampered = _copy.deepcopy(PACKAGE)
    for item in tampered["items"]:
        if item["item_id"] == "DE_烟灰缸+烟":
            item["turns"][0]["response_role"] = "右命名"
    positions = autopilot_positions.build_positions(
        BANK, week_no=2, event_line="正式训练")
    double_first = positions[20]
    assert (double_first.item_id, double_first.response_role) == (
        "DE_烟灰缸+烟", "左命名")
    gap = autopilot_positions.readiness_gap(
        BANK, double_first, interaction_package=tampered)
    assert gap is not None
    assert gap.code == "interaction_package_unavailable"
    assert "左命名" in gap.detail


def test_bank_field_gaps_still_win_over_package_presence():
    broken_double = dict(BANK.double_element[0])
    broken_double["left_word"] = " "
    bank = replace(
        BANK,
        single_element=[],
        double_element=[broken_double],
        multi_element=[],
    )
    decision = autopilot_positions.first_position_decision(
        bank, interaction_package=PACKAGE, week_no=2, event_line="正式训练")
    assert decision.gap is not None
    assert decision.gap.code == "source_field_unavailable"
    assert "left_word" in decision.gap.detail


def test_next_position_decision_passes_package_through():
    current = autopilot_positions.build_positions(
        BANK, week_no=2, event_line="正式训练")[19]
    decision = autopilot_positions.next_position_decision(
        BANK,
        week_no=2,
        event_line="正式训练",
        current_item_id=current.item_id,
        current_turn_seq=current.turn_seq,
        interaction_package=PACKAGE,
    )
    assert decision.gap is None
    assert decision.position is not None
    assert decision.position.task_type == "双要素"
    assert decision.position.response_role == "左命名"
