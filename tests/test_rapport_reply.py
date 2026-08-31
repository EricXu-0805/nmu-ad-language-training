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

        def generate(self, ask, asr_text):
            return f"stub回应:{asr_text}"

    rapport_reply.register_engine("stub-test", Stub())
    monkeypatch.setenv("RAPPORT_REPLY", "stub-test")
    try:
        assert rapport_reply.get_engine().generate("问", "答") == "stub回应:答"
    finally:
        rapport_reply._ENGINES.pop("stub-test", None)
