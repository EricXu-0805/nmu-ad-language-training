"""规则确定式判分后端（M4，无 LLM）+ 提示/沉默状态机。

边界（开发计划§M4）：
  · 只吃 JudgeInput（画像自然缺席）+ 老人回答文本；只产 **AI 初评**，永不产锁定分。
  · 取文优先级：有 confirmed_response_text 用 confirmed、无则回退 asr_text。
  · 命中 target/acceptable/方言俗名 = 正确；上位词/相关/部分 = 部分正确且 needs_review；
    空 = 沉默、显式拒绝 = 拒答（沉默/拒答是交互状态，不属判分回答类型）。
  · 线索内容一律来自 M5 cue_text，本机只管分级/时序，不生成任何提示话术。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Optional

from .enums import AnswerType
from .judging import JudgeInput, resolve_response_text

# 显式拒答标记（交互状态，非判分回答类型）。
_REFUSAL_MARKERS = ("不知道", "不想说", "不晓得", "记不得", "记不起", "想不起",
                    "不会", "不说了", "算了", "不认识")
# 归一化时剥除的标点/空白。
_STRIP_CHARS = " \t\n\r，。、；：？！,.;:?!\"'“”‘’（）()【】[]{}—…·"


def normalize(text: Optional[str]) -> str:
    """去标点空白 + 全角转半角 + 英文小写，供确定式匹配。中文本身不改字。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = "".join(ch for ch in t if ch not in _STRIP_CHARS)
    return t.lower()


@dataclass(frozen=True)
class RuleJudgeResult:
    """规则后端产物 = AI 初评。answer_type / interaction_state 二者互斥其一有值。"""
    answer_type: Optional[AnswerType]        # 有文本回答时的判分类型
    interaction_state: Optional[str]          # "沉默" | "拒答"（无判分类型）
    ai_score: float                           # 初评参考分（0 / 0.5 / 1），非锁定分
    ai_needs_review: bool
    matched_on: Optional[str] = None          # 命中来源：target/acceptable/dialect/upper
    judge_portrait_used: bool = False         # 恒 False，审计用


def _is_refusal(raw: Optional[str]) -> bool:
    return bool(raw) and any(m in raw for m in _REFUSAL_MARKERS)


def judge_rule_based(ji: JudgeInput) -> RuleJudgeResult:
    """确定式判分。JudgeInput 结构上不含画像；judge_portrait_used 恒 False。"""
    raw = resolve_response_text(ji)
    if _is_refusal(raw):
        return RuleJudgeResult(None, "拒答", 0.0, True, matched_on="refusal")
    resp = normalize(raw)
    if not resp:
        return RuleJudgeResult(None, "沉默", 0.0, True, matched_on="silence")

    target = normalize(ji.target_word)
    accept = {normalize(x) for x in ji.acceptable_expressions}
    dialect = {normalize(x) for x in ji.dialect_synonyms}
    upper = {normalize(x) for x in ji.upper_terms}

    if resp == target or resp in accept:
        return RuleJudgeResult(AnswerType.正确, None, 1.0, False,
                               matched_on="target" if resp == target else "acceptable")
    if resp in dialect:
        # 方言俗名视为正确，但留人工复核（口径可能因地区而异）。
        return RuleJudgeResult(AnswerType.正确, None, 1.0, True, matched_on="dialect")
    if resp in upper:
        return RuleJudgeResult(AnswerType.上位词或相关词, None, 0.5, True, matched_on="upper")
    if target and (target in resp or resp in target):
        # 含目标词但不精确（多字/少字）——部分正确，交人工定夺。
        return RuleJudgeResult(AnswerType.部分正确, None, 0.5, True, matched_on="substring")
    return RuleJudgeResult(AnswerType.未识别, None, 0.0, True, matched_on=None)


# ============ 提示/沉默状态机 ============
# 线索分级：0 自发 → 1 轻度语义 → 2 明确/语音线索 → 3 告知答案。内容全部取自 M5。
@dataclass
class PromptState:
    """单环节的提示分级与沉默时序。cues/tell 由 M5 传入，本机绝不自造话术。

    cues 形如 {"1": "线索1文本", "2": "线索2文本"}；tell = 第3级告知话术。
    """
    item_id: str
    cues: dict                                # {"1": str, "2": str}
    tell_answer: Optional[str] = None
    silence_seconds: float = 10.0             # 默认 10s，与第2–8周口径一致
    level: int = 0
    lowest_success_cue_level: Optional[int] = None

    def should_prompt_on_silence(self, elapsed_seconds: float) -> bool:
        return elapsed_seconds >= self.silence_seconds

    def next_cue(self) -> tuple[int, Optional[str]]:
        """升一级并返回 (等级, 该级线索文本)。到 3 级=告知答案；不超过 3。"""
        if self.level >= 3:
            return 3, self.tell_answer
        self.level += 1
        if self.level == 3:
            return 3, self.tell_answer
        return self.level, self.cues.get(str(self.level))

    def mark_success(self) -> None:
        """本环节最终答对，记录达成时的最低成功线索等级。"""
        self.lowest_success_cue_level = self.level
