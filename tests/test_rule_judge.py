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
