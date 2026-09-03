"""第1周 LLM 回应生成引擎:守卫与降级契约。

这里钉的是"生成文本必须先过守卫、任何越界都回落句库"这一层;
引擎真调云的行为不在单测范围(走 harness production 模式实测)。
"""
from __future__ import annotations

import json

from app import rapport_reply


# ---------------- validate_reply_text:每一类越界都必须被拒 ----------------

def test_a_clean_short_line_passes_and_is_stripped():
    assert rapport_reply.validate_reply_text("  您说得真好，谢谢您。 ") == "您说得真好，谢谢您。"


def test_non_string_and_empty_are_rejected():
    assert rapport_reply.validate_reply_text(None) is None
    assert rapport_reply.validate_reply_text(123) is None
    assert rapport_reply.validate_reply_text("") is None
    assert rapport_reply.validate_reply_text("   ") is None


def test_overlong_reply_is_rejected():
    # 边界钉成数字:上限常量被放宽时这条必须红,不许跟着常量一起漂。
    assert rapport_reply.MAX_REPLY_CHARS == 60
    assert rapport_reply.validate_reply_text("好" * 60) is not None
    assert rapport_reply.validate_reply_text("好" * 61) is None


def test_newline_and_control_chars_are_rejected():
    assert rapport_reply.validate_reply_text("第一句\n第二句") is None
    assert rapport_reply.validate_reply_text("有控制符\x07在里面") is None


def test_slot_marker_residue_is_rejected():
    assert rapport_reply.validate_reply_text("您喜欢【老人所说的兴趣】呀") is None
    assert rapport_reply.validate_reply_text('残留{"reply"}结构') is None


def test_identity_questions_are_rejected():
    # 姓名/年龄两问被明确排除在自由对话之外;生成器问出来就是越界。
    assert rapport_reply.validate_reply_text("对了，您叫什么名字呀？") is None
    assert rapport_reply.validate_reply_text("您今年多大年纪啦？") is None
    assert rapport_reply.validate_reply_text("您家住哪儿呀？") is None
    assert rapport_reply.validate_reply_text("那您贵姓呀？") is None
    assert rapport_reply.validate_reply_text("您的名字真好听。") is None
    assert rapport_reply.validate_reply_text("留个电话方便联系您。") is None


# ---------------- prompt:两个输入都必须进 prompt,硬规矩必须在场 ----------------

def test_prompt_embeds_ask_and_asr_text_and_hard_rules():
    prompt = rapport_reply.build_reply_prompt(
        "您平时喜欢做些什么呢？", "我喜欢听戏")
    assert "您平时喜欢做些什么呢？" in prompt
    assert "我喜欢听戏" in prompt
    assert "身份" in prompt          # 禁问身份信息
    assert "医疗" in prompt          # 禁医疗建议
    assert '{"reply"' in prompt      # 输出形状约定


# ---------------- QwenReplyEngine._parse:格式可疑一律 None ----------------

def _qwen() -> rapport_reply.QwenReplyEngine:
    return rapport_reply.QwenReplyEngine("qwen-plus")


def test_parse_accepts_exact_shape_and_fenced_json():
    assert _qwen()._parse('{"reply": "谢谢您跟我讲这些。"}') == "谢谢您跟我讲这些。"
    fenced = '```json\n{"reply": "谢谢您跟我讲这些。"}\n```'
    assert _qwen()._parse(fenced) == "谢谢您跟我讲这些。"


def test_parse_rejects_wrong_shapes():
    q = _qwen()
    assert q._parse(None) is None
    assert q._parse("") is None
    assert q._parse("不是 JSON") is None
    assert q._parse(json.dumps(["谢谢您"])) is None
    assert q._parse(json.dumps({"reply": "好的。", "extra": 1})) is None
    assert q._parse(json.dumps({"answer": "好的。"})) is None
    assert q._parse(json.dumps({"reply": "第一句\n第二句"})) is None


def test_generate_without_api_key_returns_none(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    assert _qwen().generate("问句", "回答") is None


# ---------------- 引擎注册与选择:未知配置必须保留 unknown 边界 ----------------

def test_engine_selection_defaults(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("RAPPORT_REPLY", "auto")
    eng = rapport_reply.get_engine()
    assert eng.version == "off"
    assert eng.data_boundary == "local"
    assert eng.generate("问", "答") is None


def test_unknown_engine_kind_keeps_unknown_boundary(monkeypatch):
    monkeypatch.setenv("RAPPORT_REPLY", "does-not-exist")
    eng = rapport_reply.get_engine()
    assert eng.data_boundary == "unknown"
    assert eng.generate("问", "答") is None


def test_auto_with_key_selects_cloud_qwen(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("RAPPORT_REPLY", "auto")
    eng = rapport_reply.get_engine()
    assert eng.data_boundary == "cloud"
    assert eng.provider_id == "aliyun-dashscope"


def test_registered_stub_engine_is_used(monkeypatch):
    class Stub:
        version = "stub/1"
        data_boundary = "local"
        provider_id = None

        def generate(self, ask, asr_text, history=(), round_no=1, max_rounds_value=1):
            return f"stub回应:{asr_text}"

    rapport_reply.register_engine("stub-test", Stub())
    monkeypatch.setenv("RAPPORT_REPLY", "stub-test")
    try:
        assert rapport_reply.get_engine().generate("问", "答") == "stub回应:答"
    finally:
        rapport_reply._ENGINES.pop("stub-test", None)


def test_validate_rejects_format_characters():
    """零宽字符(Cf)能穿过旧的 ASCII 控制符正则,却会让回放屏契约整场拒收。"""
    assert rapport_reply.validate_reply_text("好的\u200b，谢谢您。") is None
    assert rapport_reply.validate_reply_text("好的\u200d，谢谢您。") is None
    assert rapport_reply.validate_reply_text("好的，谢谢您。") == "好的，谢谢您。"


# ---------------- 多轮:轮次上限、历史进 prompt、末轮只收束 ----------------

def test_max_rounds_env_parsing(monkeypatch):
    monkeypatch.delenv("RAPPORT_MAX_ROUNDS", raising=False)
    assert rapport_reply.max_rounds() == 2
    monkeypatch.setenv("RAPPORT_MAX_ROUNDS", "3")
    assert rapport_reply.max_rounds() == 3
    monkeypatch.setenv("RAPPORT_MAX_ROUNDS", "0")
    assert rapport_reply.max_rounds() == 1
    monkeypatch.setenv("RAPPORT_MAX_ROUNDS", "99")
    assert rapport_reply.max_rounds() == 5
    monkeypatch.setenv("RAPPORT_MAX_ROUNDS", "abc")
    assert rapport_reply.max_rounds() == 2


def test_prompt_carries_history_and_round_position():
    prompt = rapport_reply.build_reply_prompt(
        "您平时喜欢做些什么呢？", "还有种花",
        history=(("我喜欢听戏", "听戏好呀，您常听哪出？"),), round_no=2, max_rounds_value=2)
    assert "我喜欢听戏" in prompt and "听戏好呀" in prompt   # 上一轮两边都在
    assert "还有种花" in prompt                               # 最新回答
    assert "第2轮" in prompt and "最多2轮" in prompt
    assert "最后一轮" in prompt and "不要再提任何问题" in prompt


def test_prompt_non_final_round_invites_more():
    prompt = rapport_reply.build_reply_prompt("问", "答", round_no=1, max_rounds_value=2)
    assert "最后一轮" not in prompt
    assert "追问" in prompt


def test_final_round_rejects_a_question():
    # 末轮之后不再开麦:留个问号等于让老人对着空气说话。
    assert rapport_reply.validate_reply_text("那您常去吗？", final=True) is None
    assert rapport_reply.validate_reply_text("那您常去吗?", final=True) is None
    assert rapport_reply.validate_reply_text("听着真好，谢谢您。", final=True) == "听着真好，谢谢您。"
    # 非末轮照常允许追问。
    assert rapport_reply.validate_reply_text("那您常去吗？") == "那您常去吗？"


def test_invites_reply_predicate_covers_more_than_question_marks():
    """末轮之后不再开麦:把话头递回老人的任何说法都算"还在追问"。"""
    assert rapport_reply.invites_reply("那您常去吗？") is True
    assert rapport_reply.invites_reply("那您常去吗。") is True       # 问号换句号照样是追问
    assert rapport_reply.invites_reply("再跟我讲讲您年轻时候的事吧。") is True
    assert rapport_reply.invites_reply("说说您的家人。") is True
    # 收束/感叹不是追问——误判成追问会让末轮句被大面积退回罐头句。
    assert rapport_reply.invites_reply("听着真好，谢谢您。") is False
    assert rapport_reply.invites_reply("有人作伴真好。") is False
    assert rapport_reply.invites_reply("身边有熟人，心里踏实。") is False


def test_inviting_tails_carry_no_stray_alphabet():
    """这张表被逐项核过:里面只能是中文语气词(曾混进过一个西里尔字母串)。"""
    for tail in rapport_reply._INVITING_TAILS:
        assert all("\u4e00" <= ch <= "\u9fff" for ch in tail), tail
