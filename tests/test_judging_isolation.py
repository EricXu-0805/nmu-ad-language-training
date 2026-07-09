"""★画像不进判分 的契约测试——这是最高优先、不可逆的硬约束，测试即防线。"""
import dataclasses

import pytest

from app.judging import (
    JudgeInput, PORTRAIT_FIELDS, PortraitLeakError,
    assert_portrait_free, build_judge_input, resolve_response_text,
)


def test_judge_input_has_no_portrait_fields():
    names = {f.name for f in dataclasses.fields(JudgeInput)}
    assert names.isdisjoint(PORTRAIT_FIELDS), "JudgeInput 不得含任何画像字段"


def test_assert_portrait_free_flags_leak():
    with pytest.raises(PortraitLeakError):
        assert_portrait_free({"item_id": "x", "zodiac": "属牛"})
    with pytest.raises(PortraitLeakError):
        assert_portrait_free({"preferred_appellation": "王奶奶"})
    # 干净载荷不抛
    assert_portrait_free({"item_id": "x", "target_word": "胡萝卜"})


def test_build_judge_input_rejects_portrait_kwargs():
    with pytest.raises(PortraitLeakError):
        build_judge_input(item_id="d1", task_type="单要素", target_word="锚", interests="钓鱼")


def test_build_judge_input_forbids_portrait_used_true():
    with pytest.raises(PortraitLeakError):
        build_judge_input(item_id="d1", task_type="单要素", target_word="锚",
                          judge_portrait_used=True)


def test_build_judge_input_rejects_unknown_field():
    with pytest.raises(ValueError):
        build_judge_input(item_id="d1", task_type="单要素", target_word="锚", foo="bar")


def test_valid_build_and_text_resolution():
    ji = build_judge_input(
        item_id="d1", task_type="单要素", target_word="胡萝卜",
        acceptable_expressions=("红萝卜",), asr_text="红萝波", confirmed_response_text="红萝卜",
    )
    assert ji.judge_portrait_used is False
    assert resolve_response_text(ji) == "红萝卜"          # 优先人工确认
    ji2 = build_judge_input(item_id="d2", task_type="单要素", target_word="树", asr_text="书")
    assert resolve_response_text(ji2) == "书"              # 缺确认回退 ASR
