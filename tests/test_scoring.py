import math

import pytest

from app.scoring import (
    SingleElementItem, score_single_element,
    DoubleElementItem, score_double_element_item, score_double_element, DE_WEIGHTS,
    MultiElementItem, score_multi_element,
)


# ---------- 单要素 ----------
def test_single_element_basic():
    items = [
        SingleElementItem("s1", final_correct=1, spontaneous_correct=1, prompt_level=0, duration_seconds=4.0),
        SingleElementItem("s2", final_correct=1, spontaneous_correct=0, prompt_level=1, duration_seconds=6.0),
        SingleElementItem("s3", final_correct=0, spontaneous_correct=0, prompt_level=3, duration_seconds=10.0),
        SingleElementItem("s4", final_correct=1, spontaneous_correct=0, prompt_level=2, duration_seconds=8.0),
    ]
    r = score_single_element(items)
    assert r["n"] == 4
    assert r["naming_accuracy"] == 0.75              # 3/4
    assert r["spontaneous_naming_accuracy"] == 0.25  # 1/4
    assert r["prompt_rate"] == 0.75                  # 3/4 被提示
    assert r["total_prompt_load"] == 6               # 0+1+3+2
    assert r["prompt_level_distribution"] == {0: 1, 1: 1, 2: 1, 3: 1}
    assert r["avg_time_per_item"] == 7.0
    assert r["total_task_time"] == 28.0


def test_single_element_rejects_spontaneous_with_prompt():
    with pytest.raises(ValueError):
        score_single_element([SingleElementItem("x", 1, 1, prompt_level=2)])


def test_single_element_rejects_empty():
    with pytest.raises(ValueError):
        score_single_element([])


# ---------- 双要素 ----------
def test_double_weights_sum_to_one():
    assert abs(sum(DE_WEIGHTS.values()) - 1.0) < 1e-9


def test_double_element_item_all_correct_is_one():
    it = DoubleElementItem("d1", left_name=1, left_function=1, right_name=1, right_function=1, relation=1)
    assert math.isclose(score_double_element_item(it), 1.0)


def test_double_element_item_partial():
    # 0.15·1 + 0.10·0 + 0.15·1 + 0.10·0 + 0.5·0.5 = 0.55
    it = DoubleElementItem("d2", left_name=1, left_function=0, right_name=1, right_function=0, relation=0.5)
    assert math.isclose(score_double_element_item(it), 0.55)


def test_double_element_aggregate():
    items = [
        DoubleElementItem("d1", 1, 1, 1, 1, 1),      # 1.00, 自发关系正确
        DoubleElementItem("d2", 1, 0, 1, 0, 0.5),    # 0.55, 关系相关但不完整
    ]
    r = score_double_element(items)
    assert math.isclose(r["weekly_de_score_percentile"], (1.0 + 0.55) / 2 * 100)
    assert r["spontaneous_relation_identification_rate"] == 0.5  # 只有 d1 relation==1


def test_double_element_bad_relation():
    with pytest.raises(ValueError):
        score_double_element_item(DoubleElementItem("bad", 1, 1, 1, 1, relation=0.7))


# ---------- 多要素 ----------
def test_multi_element_key_element_rate_only():
    items = [
        MultiElementItem("m1", key_elements={"情境": 1, "事物": 1, "人物": 0, "动作": 1}),  # 3/4
        MultiElementItem("m2", key_elements={"情境": 0, "事物": 1}),                          # 1/2
    ]
    r = score_multi_element(items)
    assert r["per_item"][0]["key_element_rate"] == 0.75
    assert r["per_item"][1]["key_element_rate"] == 0.5
    assert math.isclose(r["weekly_me_score_percentile"], (0.75 + 0.5) / 2 * 100)
    # 扩展列默认关闭
    assert "completeness" not in r["per_item"][0]


def test_multi_element_extensions_off_by_default_on_when_asked():
    it = MultiElementItem("m3", key_elements={"a": 1}, completeness=0.8)
    off = score_multi_element([it])
    assert "completeness" not in off["per_item"][0]
    on = score_multi_element([it], use_extensions=True)
    assert on["per_item"][0]["completeness"] == 0.8
