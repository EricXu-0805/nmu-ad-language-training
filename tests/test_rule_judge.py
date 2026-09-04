from app.enums import AnswerType
from app.judging import build_judge_input
from app.rule_judge import PromptState, judge_rule_based, normalize


def _ji(target, asr=None, confirmed=None, **kw):
    return build_judge_input(item_id="x", task_type="单要素", target_word=target,
                             asr_text=asr, confirmed_response_text=confirmed, **kw)


def test_normalize_strips_punct_and_space():
    assert normalize(" 胡萝卜。") == "胡萝卜"
    assert normalize("ABC, def") == "abcdef"


def test_exact_target_is_correct_no_review():
    r = judge_rule_based(_ji("胡萝卜", asr="胡萝卜"))
    assert r.answer_type == AnswerType.正确 and r.ai_score == 1.0 and r.ai_needs_review is False


def test_prefers_confirmed_over_asr():
    r = judge_rule_based(_ji("胡萝卜", asr="红萝波", confirmed="胡萝卜"))
    assert r.answer_type == AnswerType.正确 and r.matched_on == "target"


def test_acceptable_expression_correct():
    r = judge_rule_based(_ji("番茄", asr="西红柿", acceptable_expressions=("西红柿",)))
    assert r.answer_type == AnswerType.正确 and r.matched_on == "acceptable"


def test_upper_term_is_partial_needs_review():
    r = judge_rule_based(_ji("麻雀", asr="鸟", upper_terms=("鸟",)))
    assert r.answer_type == AnswerType.上位词或相关词 and r.ai_needs_review is True and r.ai_score == 0.5


def test_silence_and_refusal_are_interaction_states():
    assert judge_rule_based(_ji("锚", asr="")).interaction_state == "沉默"
    assert judge_rule_based(_ji("锚", asr="不知道")).interaction_state == "拒答"


def test_unrecognized_needs_review():
    r = judge_rule_based(_ji("锚", asr="轮船"))
    assert r.answer_type == AnswerType.未识别 and r.ai_needs_review is True and r.ai_score == 0.0


def test_judge_never_uses_portrait():
    # JudgeInput 结构上无画像字段；结果溯源标记恒 False
    assert judge_rule_based(_ji("锚", asr="锚")).judge_portrait_used is False


def test_prompt_state_climbs_to_tell_answer():
    ps = PromptState(item_id="SE_花", cues={"1": "它是植物", "2": "它开在春天"},
                     tell_answer="这个叫花")
    assert ps.should_prompt_on_silence(10.0) and not ps.should_prompt_on_silence(3.0)
    assert ps.next_cue() == (1, "它是植物")
    assert ps.next_cue() == (2, "它开在春天")
    assert ps.next_cue() == (3, "这个叫花")
    assert ps.next_cue() == (3, "这个叫花")     # 不越级
    ps.mark_success()
    assert ps.lowest_success_cue_level == 3


# ── 目标词说了不止一遍仍是正确(2026-09-04 钱凯:「回答正确但 AI 判定为 0 分」) ──
# 生产 8/31~9/4 有 49 次「窗帘，窗帘。」「嗯，茶杯，茶杯。」「花花」被云判分判成
# 「重复」0 分,全部含目标词。临床口径(0628 行为指标)的「重复」是「重复一题答案」——
# 照搬上一题的答案/复述问句,不是把正确的词多说一遍。这一格由确定式规则定。

def test_target_said_twice_is_correct_no_review():
    r = judge_rule_based(_ji("窗帘", asr="窗帘，窗帘。"))
    assert (r.answer_type, r.matched_on, r.ai_needs_review, r.ai_score) == (
        AnswerType.正确, "target", False, 1.0)
    r3 = judge_rule_based(_ji("汽车", asr="汽车汽车汽车。"))
    assert r3.answer_type == AnswerType.正确 and r3.matched_on == "target"


def test_filler_words_around_target_still_correct():
    for asr in ("嗯，茶杯，茶杯。", "呃，茶杯。", "这是茶杯。", "应该是茶杯吧。", "茶杯，对，茶杯。"):
        r = judge_rule_based(_ji("茶杯", asr=asr))
        assert r.answer_type == AnswerType.正确 and r.matched_on == "target", asr


def test_reduplicated_single_char_target_is_correct():
    # 「花花」「鸡鸡」:目标词是单字时叠音就是那个词
    assert judge_rule_based(_ji("花", asr="花花。")).answer_type == AnswerType.正确
    assert judge_rule_based(_ji("鸡", asr="鸡鸡。")).answer_type == AnswerType.正确


def test_acceptable_expression_said_twice_is_correct():
    r = judge_rule_based(_ji("刀子", asr="刀，刀。", acceptable_expressions=("刀",)))
    assert r.answer_type == AnswerType.正确 and r.matched_on == "acceptable"
    assert r.ai_needs_review is False


def test_dialect_said_twice_is_correct_with_review():
    r = judge_rule_based(_ji("番茄", asr="洋柿子，洋柿子。", dialect_synonyms=("洋柿子",)))
    assert r.answer_type == AnswerType.正确 and r.matched_on == "dialect"
    assert r.ai_needs_review is True


def test_extra_content_beyond_target_stays_partial_for_review():
    # 多字/带修饰不是「只有那个词」:仍走子串分支交人工,不把「母螺母」「大树」抬成满分
    for target, asr in (("螺母", "母螺母。"), ("树", "大树，这是大树。"), ("窗帘", "窗窗帘。")):
        r = judge_rule_based(_ji(target, asr=asr))
        assert r.answer_type == AnswerType.部分正确 and r.matched_on == "substring", asr
        assert r.ai_needs_review is True


def test_other_word_is_not_rescued_by_filler_stripping():
    r = judge_rule_based(_ji("锚", asr="嗯，这是轮船。"))
    assert r.answer_type == AnswerType.未识别
    # 目标词旁边还有别的实词(不是垫词):不能算「只有那个词」,走子串分支交人工
    r2 = judge_rule_based(_ji("锚", asr="锚，不对，轮船。"))
    assert r2.answer_type == AnswerType.部分正确 and r2.matched_on == "substring"


def test_single_char_target_next_to_single_char_filler_is_a_different_word():
    """「对门」「花呢」「针对」是别的词,不是「门」「花」「针」带语气(复核 2026-09-04)。
    但「对，门」按标点分段后「对」是独立的一段,照样算垫词。"""
    for target, asr in (("门", "对门。"), ("花", "花呢。"), ("针", "针对。"), ("烟", "烟嘛")):
        r = judge_rule_based(_ji(target, asr=asr))
        assert r.answer_type == AnswerType.部分正确 and r.matched_on == "substring", asr
    for target, asr in (("门", "对，门。"), ("门", "嗯，门。"), ("门", "这是门。"), ("门", "门门。")):
        r = judge_rule_based(_ji(target, asr=asr))
        assert r.answer_type == AnswerType.正确 and r.matched_on == "target", asr


def test_negated_target_is_not_correct():
    for target, asr in (("窗帘", "不是窗帘。"), ("窗帘", "没有窗帘"), ("花", "不是花")):
        r = judge_rule_based(_ji(target, asr=asr))
        assert r.answer_type == AnswerType.部分正确 and r.matched_on == "substring", asr


def test_acceptable_expression_substring_is_partial_not_unrecognized():
    r = judge_rule_based(_ji("刀子", asr="小刀。", acceptable_expressions=("刀",)))
    assert r.answer_type == AnswerType.部分正确 and r.matched_on == "substring"


def test_classifier_phrase_is_the_word_itself():
    """「一把刀」「一朵花」「这本书」「一个胡萝卜」是命名的正常说法,不是别的词。"""
    assert judge_rule_based(_ji("刀子", asr="一把刀。", acceptable_expressions=("刀",))).matched_on == "acceptable"
    for target, asr in (("花", "一朵花。"), ("书", "这本书"), ("胡萝卜", "一个胡萝卜。"), ("门", "那扇门")):
        r = judge_rule_based(_ji(target, asr=asr))
        assert r.answer_type == AnswerType.正确 and r.matched_on == "target", asr
    # 修饰语不是量词:仍交人工
    r = judge_rule_based(_ji("胡萝卜", asr="一根大胡萝卜"))
    assert r.answer_type == AnswerType.部分正确 and r.matched_on == "substring"


def test_word_present_is_word_level():
    from app.rule_judge import word_present
    assert word_present("大胡萝卜，这是大胡萝卜。", "胡萝卜") is True
    assert word_present("嗯，胡萝，卜。", "胡萝卜") is False   # 被标点拆开,按分段不算整词
    assert word_present("花瓶。", "花") is False
    assert word_present("书架", "书") is False
    assert word_present("对，花。", "花") is True
    assert word_present("花花。", "花") is True
    assert word_present("不是窗帘。", "窗帘") is False
    assert word_present("锚，不对，轮船。", "锚") is True
