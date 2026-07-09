"""判分输入契约（M4 判分链）——★画像不进判分（最高优先硬约束，不可逆）。

设计：JudgeInput 的字段集里【根本不含】任何画像字段；再加运行时守卫，任何画像键
一旦混入判分输入立即抛错。judge_portrait_used 恒 False，供 M6/审计核对。
交互链（个性化称呼/鼓励/提示措辞/拉回）另走 interaction.py，与本模块物理分离。
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields

# 画像字段命名空间（与共享数据契约 H 组一致）。判分输入出现其中任一键即违规。
PORTRAIT_FIELDS = frozenset({
    "preferred_appellation", "zodiac", "interests", "daily_activities",
    "familiar_places", "familiar_people", "common_objects", "willing_to_continue",
    "slot_value",  # 画像/插槽派生
})


class PortraitLeakError(RuntimeError):
    """判分输入混入画像字段——违反『画像不进判分』。"""


@dataclass(frozen=True)
class JudgeInput:
    """喂给判分器的全部输入。刻意不含画像字段。"""
    item_id: str
    task_type: str
    target_word: str
    acceptable_expressions: tuple[str, ...] = ()
    upper_terms: tuple[str, ...] = ()            # 上位词
    dialect_synonyms: tuple[str, ...] = ()       # 方言俗名
    confirmed_response_text: str | None = None   # 人工确认文本（优先）
    asr_text: str | None = None                  # ASR 原文（缺省回退）
    prompt_history: tuple[int, ...] = ()
    judge_portrait_used: bool = False            # 恒 False


def assert_portrait_free(payload: dict) -> None:
    """运行时守卫：判分载荷混入任何画像键即抛错。"""
    leaked = PORTRAIT_FIELDS & set(payload)
    if leaked:
        raise PortraitLeakError(
            f"判分输入混入画像字段 {sorted(leaked)}，违反『画像不进判分』硬约束")


def build_judge_input(**kwargs) -> JudgeInput:
    """构造判分输入的唯一入口：先过画像守卫，再拒绝把 judge_portrait_used 置真。"""
    assert_portrait_free(kwargs)
    if kwargs.get("judge_portrait_used"):
        raise PortraitLeakError("judge_portrait_used 必须恒为 False")
    allowed = {f.name for f in fields(JudgeInput)}
    unknown = set(kwargs) - allowed
    if unknown:
        raise ValueError(f"未知判分输入字段 {sorted(unknown)}")
    return JudgeInput(**kwargs)


def resolve_response_text(ji: JudgeInput) -> str | None:
    """取文口径：优先人工确认文本，缺省回退 ASR 原文。"""
    return ji.confirmed_response_text if ji.confirmed_response_text is not None else ji.asr_text


# 静态自检：JudgeInput 的字段名不得与任何画像字段重名（导入即验，越早炸越好）。
_INPUT_FIELD_NAMES = {f.name for f in fields(JudgeInput)}
_OVERLAP = PORTRAIT_FIELDS & _INPUT_FIELD_NAMES
if _OVERLAP:  # pragma: no cover - 结构性保证
    raise PortraitLeakError(f"JudgeInput 字段与画像字段重名：{sorted(_OVERLAP)}")
