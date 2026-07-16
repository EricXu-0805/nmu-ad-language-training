import pytest

from app.content import CONTENT_DIR, load_item_bank
from app.runtime import DOUBLE_ROLES, PlanCursor, build_session_plan


def _bank():
    return load_item_bank(CONTENT_DIR / "item_bank_v1.json")


def test_week1_has_no_scoring_items():
    plan = build_session_plan(_bank(), week_no=1, event_line="关系建立环节")
    assert plan.items == ()          # 关系建立周旁路判分


def test_week2_expands_single_and_double_turns():
    plan = build_session_plan(_bank(), week_no=2, event_line="正式训练")
    singles = [it for it in plan.items if it.task_type == "单要素"]
    doubles = [it for it in plan.items if it.task_type == "双要素"]
    assert len(singles) == 20 and len(doubles) == 10
    assert all(len(it.turns) == 1 for it in singles)
    assert all(len(it.turns) == 5 for it in doubles)
    # 双要素5环节 = 固定角色序，含关系识别在末
    assert tuple(t.response_role for t in doubles[0].turns) == DOUBLE_ROLES
    assert doubles[0].display["left_function_cue"]
    assert doubles[0].display["right_function_cue"]
    # 20单 + 10双×5 = 70 环节
    assert plan.total_turns() == 20 + 10 * 5


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
