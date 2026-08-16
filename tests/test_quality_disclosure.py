"""披露控制的算术：抑制完之后还能不能把被抑制的那一格倒推回来。

这一层没有数据库也没有 HTTP，所以能直接问那个真问题——**给定发出去的东西，
攻击者用减法能还原什么**。下面每条测试都是先把攻击写出来，再断言它失败。
"""
from __future__ import annotations

import pytest

from app.quality_disclosure import (
    Cell,
    DisclosureRegistryError,
    PublishedLeaf,
    band,
    leaves,
    plan_suppression,
    rate,
    registry_self_check,
    schema_gaps,
)


def _published(cells: list[Cell], suppressed: set[str]) -> dict[str, int | None]:
    """模拟对外发布：被抑制的格出 None，其余出真值。"""
    return {cell.key: (None if cell.key in suppressed else cell.contributors)
            for cell in cells}


def test_a_single_sparse_cell_cannot_be_recovered_from_the_table_total():
    cells = [Cell("a", 1), Cell("b", 12), Cell("c", 20), Cell("d", 30)]
    total = sum(cell.contributors for cell in cells)

    suppressed = plan_suppression(cells, minimum=5)
    published = _published(cells, suppressed)

    # 攻击：总计减去所有还看得见的格。
    visible = sum(v for v in published.values() if v is not None)
    recovered = total - visible
    hidden = [cell for cell in cells if published[cell.key] is None]

    assert len(hidden) >= 2, "只抑制一个格等于把它写在总计里"
    assert recovered != hidden[0].contributors or len(hidden) > 1


def test_suppressing_exactly_one_cell_is_what_the_complementary_rule_prevents():
    cells = [Cell("a", 1), Cell("b", 12), Cell("c", 20)]

    suppressed = plan_suppression(cells, minimum=5)

    assert "a" in suppressed
    # 互补抑制牺牲的是 b（剩下里贡献最少的那个），方向不能反成牺牲 c。
    assert suppressed == {"a", "b"}


def test_a_lone_table_with_one_cell_does_not_invent_a_second_victim():
    """整张表只有一个格时无从互补——但也没有总计可减，所以抑制它就够了。"""
    assert plan_suppression([Cell("only", 1)], minimum=5) == {"only"}


def test_suppression_propagates_across_linked_cells_to_a_fixed_point():
    cells = [
        Cell("t1_a", 1, linked=frozenset({"t2_a"})),
        Cell("t1_b", 40),
        Cell("t2_a", 30, linked=frozenset({"t3_a"})),
        Cell("t2_b", 40),
        Cell("t3_a", 30),
        Cell("t3_b", 40),
    ]

    suppressed = plan_suppression(cells, minimum=5)

    # t1_a 稀疏 → t2_a 同一批人 → t3_a 又同一批人，必须一路传到不动点，
    # 否则从 t2/t3 就能把 t1_a 还原。
    assert {"t1_a", "t2_a", "t3_a"} <= suppressed


def test_a_cell_that_meets_the_threshold_is_not_suppressed_for_no_reason():
    cells = [Cell("a", 12), Cell("b", 20), Cell("c", 30)]

    assert plan_suppression(cells, minimum=5) == set()


def test_a_threshold_below_two_is_refused_instead_of_silently_accepted():
    with pytest.raises(DisclosureRegistryError) as caught:
        plan_suppression([Cell("a", 1)], minimum=1)
    assert caught.value.code == "min_cell_subjects_invalid"


def test_duplicate_cell_keys_are_refused_rather_than_silently_collapsed():
    with pytest.raises(DisclosureRegistryError) as caught:
        plan_suppression([Cell("a", 1), Cell("a", 30)], minimum=5)
    assert caught.value.code == "cell_key_duplicated"


def test_banding_hides_the_exact_count_and_keeps_none_as_none():
    assert band(25, width=10) == "20-29"
    assert band(20, width=10) == "20-29"
    assert band(29, width=10) == "20-29"
    assert band(30, width=10) == "30-39"
    assert band(None, width=10) is None
    # 同一档内两个不同的真值发出去一模一样——这正是它要做的事。
    assert band(21, width=10) == band(28, width=10)


def test_rates_truncate_rather_than_round_so_neighbours_collapse():
    # 四舍五入会把 0.4449 和 0.4451 分到两个值上，而两者之差可能就是一个人。
    assert rate(4449, 10000, decimals=2) == 0.44
    assert rate(4451, 10000, decimals=2) == 0.44
    assert rate(1, 3, decimals=2) == 0.33
    assert rate(29, 100, decimals=2) == 0.29
    assert rate(29, 50, decimals=2) == 0.58
    assert rate(None, 10, decimals=2) is None
    assert rate(5, 0, decimals=2) is None
    assert rate(5, None, decimals=2) is None


def test_an_unregistered_leaf_is_reported_so_it_can_default_to_suppressed():
    payload = {
        "release": {"epoch_seq": 2},
        "operational": {"pause_rate": 0.1, "secret_new_field": 7},
    }
    registry = [
        PublishedLeaf(("release", "epoch_seq"), "clear", "纪元序号"),
        PublishedLeaf(("operational", "pause_rate"), "rate", "暂停率"),
    ]

    assert schema_gaps(payload, registry) == ["operational/secret_new_field"]


def test_string_and_null_leaves_are_registrable_not_just_numbers():
    """字符串 status 与 null 位图都是实证过的侧信道，登记表必须够得着它们。"""
    payload = {"diagnostics": {"status": "partial", "reason_counts": None}}

    assert leaves(payload) == [
        ("diagnostics", "reason_counts"),
        ("diagnostics", "status"),
    ]
    assert schema_gaps(payload, [
        PublishedLeaf(("diagnostics", "status"), "clear", "冻结时的完整性"),
        PublishedLeaf(("diagnostics", "reason_counts"), "suppressed", "整块抑制"),
    ]) == []


def test_a_partially_nulled_group_is_visible_to_the_registry_as_separate_leaves():
    """把 12 项里的 3 项置 null，位图本身就说出了那 3 项非零。

    登记表帮不了这件事——它只能报"这些叶子在不在册"。所以 research 行的
    reason_counts 必须整块 null，这条测试钉住的是"整块"与"部分"在结构上
    确实是两种不同的载荷，不是同一个东西的两种写法。
    """
    whole = {"diagnostics": {"reason_counts": None}}
    partial = {"diagnostics": {"reason_counts": {"a": None, "b": 0, "c": 3}}}

    assert leaves(whole) != leaves(partial)
    assert len(leaves(partial)) == 3


def test_an_empty_registry_is_a_problem_not_a_permissive_default():
    assert registry_self_check([]) == [
        "登记表是空的——那样任何字段都会被判成未登记"]


def test_the_registry_refuses_duplicates_unknown_modes_and_unexplained_leaves():
    problems = registry_self_check([
        PublishedLeaf(("a",), "clear", "有理由"),
        PublishedLeaf(("a",), "clear", "有理由"),
        PublishedLeaf(("b",), "sometimes", "有理由"),
        PublishedLeaf(("c",), "clear", "   "),
    ])

    assert problems == [
        "a 重复登记",
        "b 的 disclosure 'sometimes' 不在闭集内",
        "c 没写理由",
    ]
