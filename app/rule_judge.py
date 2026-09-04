"""规则确定式判分后端（M4，无 LLM）+ 提示/沉默状态机。

边界（开发计划§M4）：
  · 只吃 JudgeInput（画像自然缺席）+ 老人回答文本；只产 **AI 初评**，永不产锁定分。
  · 取文优先级：有 confirmed_response_text 用 confirmed、无则回退 asr_text。
  · 命中 target/acceptable/方言俗名 = 正确；上位词/相关/部分 = 部分正确且 needs_review；
    空 = 沉默、显式拒绝 = 拒答（沉默/拒答是交互状态，不属判分回答类型）。
  · 线索内容一律来自 M5 cue_text，本机只管分级/时序，不生成任何提示话术。
"""
from __future__ import annotations

import re
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


# 老人答题时常见的语气/指示垫词。它们不改变「说的是哪个词」,只在判「这一段是不是
# 只有目标词」时从两端剥掉;长词在前,免得「这是」被拆成「这」「是」后残留。
_FILLER_TOKENS = ("应该是", "好像是", "这个是", "那个是", "这就是", "就是", "这是",
                  "那是", "这个", "那个", "嗯", "呃", "啊", "哦", "哎", "唉", "是",
                  "对", "吧", "的", "了", "呀", "嘛", "呢", "哈")
# 单字目标词旁边的单字垫词不剥(见 _peel_fillers):「对门」「花呢」「针对」是别的词,
# 不是「门」「花」「针」带语气(复核 2026-09-04)。「对，门」按标点分段后「对」是独立的
# 一段,照样算垫词。
# 否定/纠正:「不是窗帘」「没有花」不算说出了目标词。
_NEGATION_MARKERS = ("不是", "不对", "没有", "不", "没", "别", "非")


def _segments(raw: Optional[str]) -> list[str]:
    """按标点/空白切段,段内 NFKC+小写。分段保留了说话的边界,单靠 normalize 会把
    「对，门」和「对门」压成一样。"""
    if not raw:
        return []
    text = unicodedata.normalize("NFKC", raw).lower()
    out, cur = [], []
    for ch in text:
        if ch in _STRIP_CHARS:
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def _peel_fillers(seg: str, word: str) -> str:
    """从段的两端剥语气垫词;目标词是单字时只剥多字垫词。"""
    allow_single = len(word) >= 2
    changed = True
    while changed and seg:
        changed = False
        for tok in _FILLER_TOKENS:
            if len(tok) == 1 and not allow_single:
                continue
            if seg.startswith(tok) and seg != word:
                seg = seg[len(tok):]
                changed = True
            if seg.endswith(tok) and seg != word:
                seg = seg[:-len(tok)]
                changed = True
    return seg


def _is_word_repeated(seg: str, word: str) -> bool:
    """段就是 word 本身或 word 连说 k 遍(「窗帘窗帘」「花花」「汽车汽车汽车」)。"""
    return bool(word) and bool(seg) and len(seg) % len(word) == 0 \
        and seg == word * (len(seg) // len(word))


# 「一把刀」「一朵花」「这本书」:量词短语是命名的正常说法,不是别的词。只认这张闭集
# 里的量词,且只在段首、紧贴目标词之前。
_CLASSIFIER_PHRASE = re.compile(
    r"^(?:一|这|那|一个|这个|那个)?[个把朵只本条张根辆件双顶台座棵头匹块支枝扇盏面部颗粒片串杯碗瓶壶艘架幅尾口位]")


def _strip_classifier(seg: str, word: str) -> str:
    m = _CLASSIFIER_PHRASE.match(seg)
    if m and seg != word and seg[m.end():]:
        return seg[m.end():]
    return seg


def _segment_says_word(seg: str, word: str) -> bool:
    if any(m in seg for m in _NEGATION_MARKERS):
        return False
    peeled = _peel_fillers(seg, word)
    return (_is_word_repeated(peeled, word)
            or _is_word_repeated(_strip_classifier(peeled, word), word))


def _only_this_word(raw: Optional[str], word: str) -> bool:
    """整句是否只由 word(可连说多遍、分段重复)加语气垫词构成。

    「窗帘，窗帘。」「嗯，茶杯，茶杯。」「花花」「对，门」都算就是那个词——痴呆老人
    把目标词说两遍是常态,临床口径里「重复」指的是照搬上一题答案或复述问句,不是
    把正确的词多说一遍(0628 行为指标:「重复一题答案」)。
    「对门」「花瓶」「大树」「不是窗帘」都不是:它们进子串分支交人工,或留给 LLM。
    """
    if not word:
        return False
    segs = _segments(raw)
    if not segs:
        return False
    said = False
    for seg in segs:
        if _segment_says_word(seg, word):
            said = True
        elif _peel_fillers(seg, "__") != "":
            # 这一段既不是目标词也不是纯垫词段(「对，门」里的「对」是纯垫词段,可以;
            # 「锚，不对，轮船」里的「轮船」不行)。"__" 只是让剥词按多字规则全剥。
            return False
    return said


def word_present(raw: Optional[str], word: str) -> bool:
    """老人是否把 word 当作一个完整的词说了出来(供「初评与事实矛盾」闸使用)。

    多字目标词:某一段里含它即可(「大胡萝卜」也算说到了,带修饰归部分正确);
    单字目标词:必须是独立的一段(剥掉多字垫词后就是它),「花瓶」「书架」「鲸鱼」
    里的「花」「书」「鱼」不算——那是别的词,LLM 判偏题/重复是对的。
    任何一段带否定(「不是窗帘」)都不算。
    """
    word = normalize(word)
    if not word:
        return False
    for seg in _segments(raw):
        if any(m in seg for m in _NEGATION_MARKERS):
            continue
        if _segment_says_word(seg, word):
            return True
        if len(word) >= 2 and word in seg:
            return True
    return False


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
    # 目标词/可接受表达说了不止一遍、或前后带语气垫词(「窗帘，窗帘。」「嗯，茶杯」):
    # 仍然就是那个词。生产 2026-08-31~09-04 有 49 次这样的回答被云判分判成「重复」0 分,
    # 全部含目标词——这一格必须由确定式规则定,不问 LLM。
    if _only_this_word(raw, target):
        return RuleJudgeResult(AnswerType.正确, None, 1.0, False, matched_on="target")
    if any(word and _only_this_word(raw, word) for word in accept):
        return RuleJudgeResult(AnswerType.正确, None, 1.0, False, matched_on="acceptable")
    if resp in dialect or any(word and _only_this_word(raw, word) for word in dialect):
        # 方言俗名视为正确，但留人工复核（口径可能因地区而异）。
        return RuleJudgeResult(AnswerType.正确, None, 1.0, True, matched_on="dialect")
    if resp in upper:
        return RuleJudgeResult(AnswerType.上位词或相关词, None, 0.5, True, matched_on="upper")
    if any(word and (word in resp or resp in word) for word in (target, *accept)):
        # 含目标词/可接受表达但不精确(多字/少字)——部分正确,交人工定夺。
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
