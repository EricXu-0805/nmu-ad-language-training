"""Server-owned exact autopilot speech may only ever be Qwen.

The generic ``/tts/speak`` path is allowed to degrade to a local Piper voice —
an operator hears it and can react.  The exact autopilot path cannot: the
patient is alone in front of the screen, and a sudden different local voice
reading the prompt is indistinguishable to them from the real one.  So a Qwen
failure there must surface as an explicit degradation, never as another
engine's audio, even on a machine where Piper is perfectly usable.
"""
from __future__ import annotations

import shutil
import struct
import wave

import pytest

from app import content, tts


def _wav_bytes(marker: int = 1) -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(struct.pack("<h", marker) * 240)
    return buf.getvalue()


class _FailingQwen:
    """A real-shaped cloud engine whose synthesis always fails."""

    cloud = True
    version = "qwen3-tts-flash/Serena@0.9"
    cache_params = "rate=0.9"

    def __init__(self):
        self.calls = 0

    def available(self) -> bool:
        return True

    def synthesize(self, text: str) -> bytes | None:
        self.calls += 1
        return None


class _CountingPiper:
    """A local engine that is available and would happily answer."""

    cloud = False
    version = "piper/zh_CN-test"
    cache_params = "length_scale=1.15"

    def __init__(self):
        self.calls = 0

    def available(self) -> bool:
        return True

    def synthesize(self, text: str) -> bytes | None:
        self.calls += 1
        return _wav_bytes(2)


@pytest.fixture
def strict_env(monkeypatch, tmp_path):
    """Isolated cache, allow-listed text, and a Piper that is ready to answer."""
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path / "tts-cache")
    monkeypatch.setattr(tts, "_engine", None)
    monkeypatch.setattr(tts, "_fallback_piper", None)
    monkeypatch.setattr(tts, "cloud_text_allowed", lambda _text: True)
    qwen, piper = _FailingQwen(), _CountingPiper()
    monkeypatch.setattr(tts, "get_engine", lambda: qwen)
    monkeypatch.setattr(tts, "_fallback_piper_engine", lambda: piper)
    monkeypatch.setattr(tts, "get_autopilot_engine", lambda: qwen, raising=False)
    return qwen, piper


def test_the_autopilot_path_never_answers_with_a_local_voice(strict_env):
    """Qwen 失败 + Piper 可用：exact 通道必须降级，且本地引擎一次都不许被调用。"""
    qwen, piper = strict_env

    data, version, cached = tts.speak_autopilot("胡萝卜")

    assert data is None
    assert cached is False
    # The version must name the engine that was actually attempted, so the
    # degraded evidence row cannot claim a provider that never ran.
    assert version == qwen.version
    assert qwen.calls == 1
    assert piper.calls == 0, "the exact autopilot path must never reach Piper"


def test_the_generic_path_still_degrades_to_the_local_voice(strict_env):
    """通用 /tts/speak 语义不变：同样的失败仍旧降级到本地 Piper 并出声。"""
    qwen, piper = strict_env

    data, version, cached = tts.speak("胡萝卜")

    assert data == _wav_bytes(2)
    assert version == piper.version
    assert cached is False
    assert qwen.calls == 1
    assert piper.calls == 1


def test_a_working_qwen_still_serves_and_caches_on_the_autopilot_path(
        monkeypatch, tmp_path):
    """合法 Qwen 成功与缓存命中仍必须 200，降级只针对真实失败。"""

    class _WorkingQwen(_FailingQwen):
        def synthesize(self, text: str) -> bytes | None:
            self.calls += 1
            return _wav_bytes(3)

    qwen = _WorkingQwen()
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path / "tts-cache")
    monkeypatch.setattr(tts, "cloud_text_allowed", lambda _text: True)
    monkeypatch.setattr(tts, "get_autopilot_engine", lambda: qwen, raising=False)

    first = tts.speak_autopilot("胡萝卜")
    second = tts.speak_autopilot("胡萝卜")

    assert first == (_wav_bytes(3), qwen.version, False)
    assert second == (_wav_bytes(3), qwen.version, True)
    assert qwen.calls == 1, "a cache hit must not call the provider again"


def test_text_outside_the_cloud_allowlist_degrades_without_any_engine_call(
        monkeypatch, tmp_path):
    """白名单红线在 exact 通道同样 fail-closed，且不得转投本地引擎。"""
    qwen, piper = _FailingQwen(), _CountingPiper()
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path / "tts-cache")
    monkeypatch.setattr(tts, "cloud_text_allowed", lambda _text: False)
    monkeypatch.setattr(tts, "_fallback_piper_engine", lambda: piper)
    monkeypatch.setattr(tts, "get_autopilot_engine", lambda: qwen, raising=False)

    data, version, cached = tts.speak_autopilot("不在白名单里的句子")

    assert (data, version, cached) == (None, qwen.version, False)
    assert qwen.calls == 0
    assert piper.calls == 0


def test_the_autopilot_engine_resolver_is_an_explicit_injection_boundary(
        monkeypatch):
    """必须存在一个可显式替换的 exact 引擎入口，且生产默认精确是 Qwen。"""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("TTS_ENGINE", raising=False)
    monkeypatch.delenv("TTS_QWEN_MODEL", raising=False)
    monkeypatch.delenv("TTS_QWEN_VOICE", raising=False)
    assert callable(tts.get_autopilot_engine)

    resolved = tts.get_autopilot_engine()

    # Naming the concrete class and model is the point: "not Piper/Null" would
    # still pass if the resolver silently fell back to CosyVoice or a browser
    # shim, and the patient would hear a different voice either way.
    assert isinstance(resolved, tts.DashScopeQwenTtsEngine)
    assert resolved.cloud is True
    assert resolved.version == "dashscope/qwen3-tts-flash/Serena"
    assert not isinstance(resolved, (tts.PiperTtsEngine, tts.NullTtsEngine,
                                     tts.DashScopeCosyVoiceEngine))
    # Resolution alone must not reach the network: without a key it is simply
    # unavailable, and synthesis returns None before any client is built.
    assert resolved.available() is False
    assert resolved.synthesize("胡萝卜") is None


def test_a_missing_key_degrades_instead_of_selecting_any_local_engine(
        monkeypatch, tmp_path):
    """无 Key 的生产环境：exact 通道降级，绝不落到 Piper 或 Null。"""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("TTS_ENGINE", raising=False)
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path / "tts-cache")
    monkeypatch.setattr(tts, "cloud_text_allowed", lambda _text: True)
    piper = _CountingPiper()
    monkeypatch.setattr(tts, "_fallback_piper_engine", lambda: piper)

    resolved = tts.get_autopilot_engine()
    assert isinstance(resolved, tts.DashScopeQwenTtsEngine)
    data, version, cached = tts.speak_autopilot("胡萝卜")

    assert (data, cached) == (None, False)
    assert version == resolved.version
    assert piper.calls == 0


# ---------------------------------------------------------------------------
# cloud_text_allowed 的交互数据包接线（tts.py 自己的装载链，不打桩白名单）。
# 上面的测试都把 cloud_text_allowed 换成桩；这一节反过来，专测真实装载链：
# 逐周数据包并入白名单、装载失败按设计缩集合（fail-closed）、逐文件
# (mtime_ns, size) 缓存键让数据包字节一动就失效重建。
# ---------------------------------------------------------------------------

# 只存在于交互数据包的 verbatim 纠正句：namefix 模板展开只会给出「它叫鸟」，
# 题库字段是作用/关系讲解句——这些句子进白名单的唯一通道是数据包接线。
# 鸟句在 wk7+wk8 两个包里都有；钥匙句只在 wk7（单周失效测试要用它判别）。
PACKAGE_ONLY_LINE = "这个我们刚刚见过，它是一只鸟。"
WK7_ONLY_LINE = "这个我们刚刚见过，它是钥匙。"


def _wk7_fixture_lines() -> tuple[str, str]:
    """(数据包问句, wk7 题库字段句)——后者不经数据包接线也应在白名单。"""
    package = content.load_autopilot_interaction_package(7)
    question = package["items"][0]["turns"][0]["question"]["text"]
    bank = content.load_item_bank_for_week(7)
    bank_line = bank.double_element[0]["left_function_cue"]
    return question, bank_line


def test_cloud_allowlist_admits_interaction_package_lines(monkeypatch):
    monkeypatch.setattr(tts, "_allow_cache", None)
    question, bank_line = _wk7_fixture_lines()
    bank = content.load_item_bank_for_week(7)
    protocol = content.load_autopilot_protocol(
        content.CONTENT_DIR / "autopilot_protocol_v1.json")
    # 判别力自证：这两句不经数据包接线进不了白名单（防断言空转）。
    without_package = content.tts_allowlist(bank, autopilot_protocol=protocol)
    assert PACKAGE_ONLY_LINE not in without_package
    assert question not in without_package
    # 真链条：cloud_text_allowed 自己装载协议+逐周数据包并入白名单。
    assert tts.cloud_text_allowed(question) is True
    assert tts.cloud_text_allowed(PACKAGE_ONLY_LINE) is True
    # namefix 模板展开句照旧放行（协议模板×题库词，不依赖数据包）。
    expansion = protocol["double"]["namefix_left"].replace(
        "【物品名】", bank.double_element[0]["left_word"])
    assert tts.cloud_text_allowed(expansion) is True
    assert tts.cloud_text_allowed(bank_line) is True


def test_cloud_allowlist_fails_closed_when_package_loading_breaks(monkeypatch):
    question, bank_line = _wk7_fixture_lines()
    monkeypatch.setattr(tts, "_allow_cache", None)

    def broken(week_no, content_dir=None, protocol=None):
        raise content.FrozenContentUnavailable("接线断开演习")

    monkeypatch.setattr(content, "load_autopilot_interaction_package", broken)
    # 数据包装载全灭 → 只来自数据包的句子出白名单（正集合缩小=fail-closed），
    # 题库来源的句子不受牵连（单点坏档不放大成全项目云语音瘫痪）。
    assert tts.cloud_text_allowed(question) is False
    assert tts.cloud_text_allowed(PACKAGE_ONLY_LINE) is False
    assert tts.cloud_text_allowed(bank_line) is True


def test_package_byte_change_invalidates_cache_and_fails_closed(
        monkeypatch, tmp_path):
    content_copy = tmp_path / "content"
    content_copy.mkdir()
    for path in content.CONTENT_DIR.glob("*.json"):
        shutil.copy(path, content_copy / path.name)
    monkeypatch.setattr(content, "CONTENT_DIR", content_copy)
    monkeypatch.setattr(tts, "_allow_cache", None)
    assert tts.cloud_text_allowed(WK7_ONLY_LINE) is True
    # 数据包字节一动：逐文件 (mtime_ns, size) 缓存键必须失效重建，重建时
    # 字节 sha 与协议钉不符 → 该周整包退出白名单，而不是吃缓存继续放行。
    wk7 = content_copy / "autopilot_interaction_week7_v1.json"
    wk7.write_bytes(wk7.read_bytes() + b"\n")
    assert tts.cloud_text_allowed(WK7_ONLY_LINE) is False
    # 其他周与题库来源不受牵连：wk7+wk8 共句仍由 wk8 包放行。
    assert tts.cloud_text_allowed(PACKAGE_ONLY_LINE) is True
    bank2 = content.load_item_bank_for_week(2)
    assert tts.cloud_text_allowed(
        bank2.double_element[0]["left_function_cue"]) is True
