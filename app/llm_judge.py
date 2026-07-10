"""M4 判分后端二:LLM 辅助判分(本地、可插拔、默认关闭)——蓝图阶段4。

边界(与规则后端完全一致,只是产初评的方式不同):
  · 只吃 JudgeInput(结构上无画像;prompt 构造前再过一次运行时画像守卫);
  · 只产 AI 初评(answer_type/score/needs_review),**永不锁分、永不写提示话术(cue_text)**;
  · LLM_JUDGE 环境变量切换,默认 off → 返回 None → 调用方回退规则后端,链路不断;
  · 真实引擎(本地 Ollama/vLLM 等,待机构 GPU)实现 LlmJudgeProvider 注册即可,调用面不变。
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Optional, Protocol

from .enums import AnswerType
from .judging import JudgeInput, assert_portrait_free, resolve_response_text


@dataclass(frozen=True)
class LlmJudgement:
    answer_type: AnswerType
    ai_score: float                  # 0 / 0.5 / 1 初评参考分
    ai_needs_review: bool
    reason: str = ""                 # 结构化理由(审计用,不进锁定分)


class LlmJudgeProvider(Protocol):
    version: str

    def judge(self, ji: JudgeInput) -> Optional[LlmJudgement]: ...


def build_judge_prompt(ji: JudgeInput) -> str:
    """判分 prompt 只由 JudgeInput 组装;组装前运行时断言无画像键(第二道防线)。"""
    fields = asdict(ji)
    assert_portrait_free(fields)
    resp = resolve_response_text(ji) or "（无回答）"
    return (
        "你是言语训练判分员。仅根据下列信息判定老人回答类型,输出 JSON "
        '{"answer_type": "正确|部分正确|上位词或相关词|偏题|重复|未识别", "score": 0|0.5|1, "needs_review": bool, "reason": str}。\n'
        f"题目类型:{ji.task_type}\n目标词:{ji.target_word}\n"
        f"可接受表达:{list(ji.acceptable_expressions)}\n上位词:{list(ji.upper_terms)}\n"
        f"方言俗名:{list(ji.dialect_synonyms)}\n老人回答:{resp}\n"
        "注意:你只产初评,不产最终分;拿不准一律 needs_review=true。"
    )


class OffLlmJudge:
    """默认引擎:恒不可用 → 调用方回退规则确定式。"""
    version = "off"

    def judge(self, ji: JudgeInput) -> Optional[LlmJudgement]:
        return None


_ENGINES: dict[str, LlmJudgeProvider] = {"off": OffLlmJudge()}


def register_engine(name: str, engine: LlmJudgeProvider) -> None:
    """真实本地引擎(Ollama/vLLM…)接入点;测试亦经此注入替身。"""
    _ENGINES[name] = engine


def get_engine() -> LlmJudgeProvider:
    """按 LLM_JUDGE 环境变量选引擎;未配置/未注册一律 off(fail-degraded)。"""
    return _ENGINES.get(os.environ.get("LLM_JUDGE", "off"), _ENGINES["off"])
