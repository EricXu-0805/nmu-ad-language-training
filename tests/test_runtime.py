import pytest

from app.content import CONTENT_DIR, load_item_bank
from app.runtime import DOUBLE_ROLES, PlanCursor, build_session_plan


def _bank():
    return load_item_bank(CONTENT_DIR / "item_bank_v1.json")


def test_week1_has_no_scoring_items():
    plan = build_session_plan(_bank(), week_no=1, event_line="关系建立环节")
    assert plan.items == ()          # 关系建立周旁路判分


def test_week2_expands_single_double_and_multi_turns():
    plan = build_session_plan(_bank(), week_no=2, event_line="正式训练")
    singles = [it for it in plan.items if it.task_type == "单要素"]
    doubles = [it for it in plan.items if it.task_type == "双要素"]
    multis = [it for it in plan.items if it.task_type == "多要素"]
    assert len(singles) == 20 and len(doubles) == 10 and len(multis) == 2
    assert all(len(it.turns) == 1 for it in singles)
    assert all(len(it.turns) == 5 for it in doubles)
    # 双要素5环节 = 固定角色序，含关系识别在末
    assert tuple(t.response_role for t in doubles[0].turns) == DOUBLE_ROLES
    assert doubles[0].display["left_function_cue"]
    assert doubles[0].display["right_function_cue"]
    # 多要素4环节 = 情境/事物/人物/动作四个关键要素(2026-08-19 内容交付)
    assert all(len(it.turns) == 4 for it in multis)
    for it in multis:
        assert tuple(t.response_role for t in it.turns) == (
            "情境", "事物", "人物", "动作")
    # 20单 + 10双×5 + 2多×4 = 78 环节
    assert plan.total_turns() == 20 + 10 * 5 + 2 * 4


def test_unstructured_weeks_fail_closed_instead_of_reusing_week2():
    with pytest.raises(ValueError, match="第3周材料尚未结构化"):
        build_session_plan(_bank(), week_no=3, event_line="正式训练")


def test_cursor_walks_every_turn_in_order():
    plan = build_session_plan(_bank(), week_no=2, event_line="正式训练", max_items=3)
    cur = PlanCursor(plan)
    seen = []
    node = cur.current()
    guard = 0
    while node is not None and guard < 1000:
        item, turn = node
        seen.append((item.presentation_order, turn.turn_seq))
        node = cur.advance()
        guard += 1
    assert cur.done()
    assert len(seen) == plan.total_turns()
    assert seen == sorted(seen)          # (item序, turn序) 单调递增，无回跳
