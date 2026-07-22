"""M4 判分后端二:LLM 辅助判分(可插拔、恒可降级)——蓝图阶段4。

边界(与规则后端完全一致,只是产初评的方式不同):
  · 只吃 JudgeInput(结构上无画像;prompt 构造前再过一次运行时画像守卫)——
    所以发往云端的判分请求天然不含患者字段,与云 TTS 白名单红线同一口径;
  · 只产 AI 初评(answer_type/score/needs_review),**永不锁分、永不写提示话术(cue_text)**;
  · LLM_JUDGE 环境变量切换(默认 auto:有 DASHSCOPE_API_KEY → qwen,无 → off);
    引擎返回 None → 调用方回退规则后端,链路不断;
  · QwenJudge:阿里百炼 qwen-plus(LLM_JUDGE_MODEL),JSON 模式,解析失败/取值非法
    一律 None 降级——宁可回退规则,不吃一个格式可疑的初评。
"""
from __future__ import annotations

import json
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
    data_boundary: str
    provider_id: str | None

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
    data_boundary = "local"
    provider_id = None

    def judge(self, ji: JudgeInput) -> Optional[LlmJudgement]:
        return None


_VALID_SCORES = (0.0, 0.5, 1.0)


class QwenJudge:
    """阿里百炼 qwen 判分:产 AI 初评,永不锁分。任何异常/格式可疑 → None 回退规则。"""

    data_boundary = "cloud"
    provider_id = "aliyun-dashscope"

    def __init__(self, model: str):
        self._model = model
        self.version = f"dashscope/{model}"

    def available(self) -> bool:
        return bool(os.environ.get("DASHSCOPE_API_KEY"))

    def judge(self, ji: JudgeInput) -> Optional[LlmJudgement]:
        if not self.available():
            return None
        prompt = build_judge_prompt(ji)      # 组装即断言无画像键;云端只见题面+回答
        try:
            raw = self._call(prompt)
        except Exception:
            return None
        return self._parse(raw)

    def _call(self, prompt: str) -> str | None:
        from dashscope import Generation
        resp = Generation.call(model=self._model,
                               messages=[{"role": "user", "content": prompt}],
                               result_format="message",
                               response_format={"type": "json_object"},
                               temperature=0.1,
                               request_timeout=15)  # SDK 默认 300s,判分等不起
        out = getattr(resp, "output", None)
        choices = getattr(out, "choices", None) if out is not None else None
        if not choices:
            return None
        return choices[0].message.content

    def _parse(self, raw) -> Optional[LlmJudgement]:
        if not raw or not isinstance(raw, str):
            return None
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        try:
            at = AnswerType(data.get("answer_type"))
            score = float(data.get("score"))
        except (ValueError, TypeError):
            return None
        if score not in _VALID_SCORES:
            return None
        return LlmJudgement(answer_type=at, ai_score=score,
                            ai_needs_review=bool(data.get("needs_review", True)),
                            reason=str(data.get("reason", ""))[:500])


_ENGINES: dict[str, LlmJudgeProvider] = {"off": OffLlmJudge()}


class UnknownLlmJudge(OffLlmJudge):
    """未知 provider 不得被当成安全的本地 off 引擎。"""
    data_boundary = "unknown"

    def __init__(self, kind: str):
        self.version = f"unknown/{kind}"


def register_engine(name: str, engine: LlmJudgeProvider) -> None:
    """真实引擎接入点;测试亦经此注入替身。"""
    _ENGINES[name] = engine


def get_engine() -> LlmJudgeProvider:
    """按 LLM_JUDGE 选引擎；未知配置保留 unknown 边界供调用链 fail-closed。"""
    kind = os.environ.get("LLM_JUDGE", "auto")
    if kind == "auto":
        kind = "qwen" if os.environ.get("DASHSCOPE_API_KEY") else "off"
    if kind == "qwen" and "qwen" not in _ENGINES:
        _ENGINES["qwen"] = QwenJudge(os.environ.get("LLM_JUDGE_MODEL", "qwen-plus"))
    return _ENGINES.get(kind) or UnknownLlmJudge(kind)
