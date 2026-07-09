"""评分纯函数（M6）——单/双/多要素。

口径来源：《行为指标评价 0628》表 + 会议决策评价体系 + 开发计划共享数据契约。
原则：纯函数、无副作用、不碰 DB；确定式计算。研究数据以人工锁定分为准，本模块
只把锁定的分环节原始值算成综合指标——是全项目「双/多要素综合分」的单一事实源
（M2 只采集分环节原始值 + 锁定，不自算 de_total / 关键要素率）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


def _check01(v, name: str) -> None:
    if v not in (0, 1):
        raise ValueError(f"{name} 必须为 0 或 1，收到 {v!r}")


# ============ 单要素 ============
@dataclass(frozen=True)
class SingleElementItem:
    item_id: str
    final_correct: int              # 最终是否命名正确（经提示后）0/1
    spontaneous_correct: int        # 是否自发（无提示）命名正确 0/1
    prompt_level: int               # 0 自发正确 / 1 轻提示 / 2 明确或语音线索 / 3 告知答案
    duration_seconds: float | None = None


def score_single_element(items: Sequence[SingleElementItem]) -> dict:
    n = len(items)
    if n == 0:
        raise ValueError("单要素题集为空")
    for it in items:
        _check01(it.final_correct, f"{it.item_id}.final_correct")
        _check01(it.spontaneous_correct, f"{it.item_id}.spontaneous_correct")
        if it.prompt_level not in (0, 1, 2, 3):
            raise ValueError(f"{it.item_id}.prompt_level 必须 0..3，收到 {it.prompt_level!r}")
        if it.spontaneous_correct == 1 and it.prompt_level != 0:
            raise ValueError(f"{it.item_id}: 自发命名正确必须 prompt_level=0")
        if it.spontaneous_correct == 1 and it.final_correct != 1:
            raise ValueError(f"{it.item_id}: 自发正确却 final_correct=0，口径矛盾")

    times = [it.duration_seconds for it in items if it.duration_seconds is not None]
    return {
        "n": n,
        "naming_accuracy": sum(it.final_correct for it in items) / n,
        "spontaneous_naming_accuracy": sum(it.spontaneous_correct for it in items) / n,
        "prompt_rate": sum(1 for it in items if it.prompt_level > 0) / n,
        "prompt_level_distribution": {lv: sum(1 for it in items if it.prompt_level == lv)
                                      for lv in (0, 1, 2, 3)},
        "total_prompt_load": sum(it.prompt_level for it in items),   # 提示强度之和（依赖程度）
        "avg_time_per_item": (sum(times) / len(times)) if times else None,
        "total_task_time": sum(times) if times else None,
    }


# ============ 双要素 ============
# 单张图片评分 = 0.15·左命名 + 0.10·左作用 + 0.15·右命名 + 0.10·右作用 + 0.5·关系识别
DE_WEIGHTS = {"left_name": 0.15, "left_function": 0.10,
              "right_name": 0.15, "right_function": 0.10, "relation": 0.50}
assert abs(sum(DE_WEIGHTS.values()) - 1.0) < 1e-9, "双要素权重必须和为 1.0"


@dataclass(frozen=True)
class DoubleElementItem:
    item_id: str
    left_name: int           # 命名正确 1 / 提示告知 0
    left_function: int        # 正确描述 1 / 不完全或未描述 0
    right_name: int
    right_function: int
    relation: float           # 自发识别正确 1 / 相关但不完整 0.5 / 未识别 0


def score_double_element_item(it: DoubleElementItem) -> float:
    for f in ("left_name", "left_function", "right_name", "right_function"):
        _check01(getattr(it, f), f"{it.item_id}.{f}")
    if it.relation not in (0, 0.5, 1):
        raise ValueError(f"{it.item_id}.relation 必须 0 / 0.5 / 1，收到 {it.relation!r}")
    return (DE_WEIGHTS["left_name"] * it.left_name
            + DE_WEIGHTS["left_function"] * it.left_function
            + DE_WEIGHTS["right_name"] * it.right_name
            + DE_WEIGHTS["right_function"] * it.right_function
            + DE_WEIGHTS["relation"] * it.relation)


def score_double_element(items: Sequence[DoubleElementItem]) -> dict:
    n = len(items)
    if n == 0:
        raise ValueError("双要素题集为空")
    per_item = [score_double_element_item(it) for it in items]
    return {
        "n": n,
        "per_item_de_total": per_item,
        "weekly_de_score_percentile": sum(per_item) / n * 100,          # 百分制
        "spontaneous_relation_identification_rate": sum(1 for it in items if it.relation == 1) / n,
    }


# ============ 多要素（只算关键要素识别；完整度/连贯性/信息单元默认关闭）============
@dataclass(frozen=True)
class MultiElementItem:
    item_id: str
    key_elements: dict            # {key_element_id: identified 0/1}，只列关键要素
    completeness: float | None = None   # 扩展列，默认关闭
    coherence: float | None = None
    info_units: int | None = None


def score_multi_element_item(it: MultiElementItem, use_extensions: bool = False) -> dict:
    if not it.key_elements:
        raise ValueError(f"{it.item_id}: 关键要素清单为空")
    for k, v in it.key_elements.items():
        _check01(v, f"{it.item_id}.key_element[{k}]")
    total = len(it.key_elements)
    identified = sum(it.key_elements.values())
    out = {
        "item_id": it.item_id,
        "total_key_elements": total,
        "identified_key_elements": identified,
        "key_element_rate": identified / total,
    }
    if use_extensions:
        out["completeness"] = it.completeness
        out["coherence"] = it.coherence
        out["info_units"] = it.info_units
    return out


def score_multi_element(items: Sequence[MultiElementItem], use_extensions: bool = False) -> dict:
    n = len(items)
    if n == 0:
        raise ValueError("多要素题集为空")
    per_item = [score_multi_element_item(it, use_extensions) for it in items]
    return {
        "n": n,
        "per_item": per_item,
        "weekly_me_score_percentile": sum(p["key_element_rate"] for p in per_item) / n * 100,
    }
