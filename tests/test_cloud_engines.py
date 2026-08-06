"""云引擎(DashScope TTS/ASR/判分)单测——全替身,零网络。

红线覆盖:白名单外文本永不出网;引擎失败恒降级;判分格式可疑一律回退规则。
"""
import base64
import importlib.util
import json
import logging
from types import SimpleNamespace

import pytest

from app import asr, content, llm_judge, tts
from app.enums import AnswerType
from app.judging import build_judge_input

BANK = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
WK = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")

WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 64


def _wav(n: int = 2000) -> bytes:
    return b"RIFF" + (n + 4).to_bytes(4, "little") + b"WAVE" + b"\x00" * n


# ---------------- 白名单闭集 ----------------

def test_allowlist_covers_bank_script_and_fixed_lines():
    allow = content.tts_allowlist(BANK, WK)
    it = BANK.single_element[0]
    assert it["initial_prompt"] in allow
    assert it["success_line"] in allow
    assert it["tell_answer"] in allow
    assert it["cues"]["1"]["text"] in allow
    d = BANK.double_element[0]
    assert d["relation_cue"] in allow and d["left_function_cue"] in allow
    assert "请看这张图片，这是什么？" in allow          # 前端固定问句
    assert "您好" in allow                              # 试听句
    assert WK["generic_fallback_line"] in allow
    robot = [s for s in WK["sections"] if s.get("speaker") == "机器人"]
    assert any(q["ask"] in allow for s in robot for q in s.get("questions") or [])
    assert "患者张三,回答记录" not in allow


def test_allowlist_slot_templates():
    allow = content.tts_allowlist(BANK, WK)
    # 属相闭集:模板展开成 12 个具体句,模板本身不进
    assert "好的，属兔啊，谢谢您告诉我。" in allow
    assert "好的，属龙啊，谢谢您告诉我。" in allow
    assert "好的，属【老人所说的属相】啊，谢谢您告诉我。" not in allow
    # 开放槽位(兴趣/活动):实例化后含老人自述=患者数据,模板与实例都不进白名单
    assert "您喜欢【老人所说的兴趣】呀，挺好的，谢谢您告诉我。" not in allow
    assert not any("【" in s for s in allow)
    # 槽位兜底句(固定文案)进白名单
    assert "好的，谢谢您告诉我。" in allow


# ---------------- 云 TTS:守卫/缓存/降级 ----------------

@pytest.fixture
def cloud_tts(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("TTS_VOICE_PATH", str(tmp_path / "missing.onnx"))  # 关掉本地降级层
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path / "tts-cache")
    calls: list[str] = []

    def fake_call(self, text):
        calls.append(text)
        return _wav()

    monkeypatch.setattr(tts.DashScopeCosyVoiceEngine, "_call", fake_call)
    eng = tts.DashScopeCosyVoiceEngine("cosyvoice-v2", "longyuan_v2", 0.9)
    monkeypatch.setattr(tts, "_engine", eng)
    yield eng, calls
    monkeypatch.setattr(tts, "_engine", None)


def test_cloud_tts_never_sends_non_allowlisted_text(cloud_tts):
    eng, calls = cloud_tts
    data, _, _ = tts.speak("患者张三,1938年生,回答了胡萝卜")
    assert data is None and calls == []                 # 红线:一个字都没出网


def test_cloud_tts_synthesizes_allowlisted_and_caches(cloud_tts):
    eng, calls = cloud_tts
    line = BANK.single_element[0]["initial_prompt"]
    data, ver, cached = tts.speak(line)
    assert data and not cached and ver == eng.version and calls == [line]
    data2, _, cached2 = tts.speak(line)
    assert data2 == data and cached2 and calls == [line]  # 第二次纯缓存,不再出网


def test_cloud_tts_discards_corrupt_or_oversized_cache(cloud_tts):
    eng, calls = cloud_tts
    line = BANK.single_element[0]["initial_prompt"]
    expected, _, _ = tts.speak(line)
    cache_path = next(tts.CACHE_DIR.glob("*.wav"))

    cache_path.write_bytes(b"not-a-wave" * 10)
    recovered, version, cached = tts.speak(line)
    assert recovered == expected and version == eng.version and not cached
    assert cache_path.read_bytes() == expected

    with cache_path.open("wb") as handle:
        handle.truncate(tts._MAX_CLOUD_TTS_BYTES + 1)
    recovered, version, cached = tts.speak(line)
    assert recovered == expected and version == eng.version and not cached
    assert cache_path.read_bytes() == expected
    assert calls == [line, line, line]


def test_cloud_wav_validator_enforces_16_mib_boundary(monkeypatch):
    assert tts._MAX_CLOUD_TTS_BYTES == 16 * 1024 * 1024
    monkeypatch.setattr(tts, "_MAX_CLOUD_TTS_BYTES", 64)
    exact = _wav(52)
    assert len(exact) == 64
    assert tts._validated_wav_bytes(exact) == exact

    with pytest.raises(ValueError, match="大小上限"):
        tts._validated_wav_bytes(_wav(53))
    with pytest.raises(ValueError, match="容器签名"):
        tts._validated_wav_bytes(b"not-a-wave" * 6)


def test_cloud_tts_error_degrades_not_500(cloud_tts, monkeypatch):
    eng, calls = cloud_tts

    def boom(self, text):
        raise RuntimeError("network down")

    monkeypatch.setattr(tts.DashScopeCosyVoiceEngine, "_call", boom)
    data, _, _ = tts.speak("您好")
    assert data is None                                  # 降级 None→204,不炸接口


@pytest.mark.skipif(not tts.DEFAULT_VOICE.exists(), reason="无本地 piper 模型")
def test_cloud_tts_failure_falls_back_to_piper(monkeypatch, tmp_path):
    if not tts.PiperTtsEngine(tts.DEFAULT_VOICE).available():
        pytest.skip("本机没有可用的 piper(缺模型或缺包),验不了这条降级")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path / "tts-cache")

    def boom(self, text):
        raise RuntimeError("network down")

    monkeypatch.setattr(tts.DashScopeCosyVoiceEngine, "_call", boom)
    monkeypatch.setattr(tts, "_engine", tts.DashScopeCosyVoiceEngine("cosyvoice-v2", "longyuan_v2", 0.9))
    data, ver, _ = tts.speak("您好")
    monkeypatch.setattr(tts, "_engine", None)
    assert data and ver.startswith("piper/")             # 云挂了,本地引擎顶上


def test_tts_engine_selection(monkeypatch):
    monkeypatch.setenv("TTS_ENGINE", "auto")
    assert not getattr(tts.get_engine(), "cloud", False)  # 无 Key:auto 落本地
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    assert isinstance(tts.get_engine(), tts.DashScopeQwenTtsEngine)   # auto=小语主音色苏瑶
    monkeypatch.setenv("TTS_ENGINE", "cosyvoice")
    assert isinstance(tts.get_engine(), tts.DashScopeCosyVoiceEngine)
    monkeypatch.setenv("TTS_ENGINE", "null")
    assert isinstance(tts.get_engine(), tts.NullTtsEngine)


def test_cloud_text_allowed_fail_closed(monkeypatch):
    # 独立 context 打补丁:对共享 monkeypatch 调 undo() 会连带撤销 autouse 隔离夹具
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(content, "CONTENT_DIR", content.CONTENT_DIR / "不存在")
        mp.setattr(tts, "_allow_cache", None)
        assert tts.cloud_text_allowed("您好") is False    # 白名单加载不了→云端一律不发
    monkeypatch.setattr(tts, "_allow_cache", None)
    assert tts.cloud_text_allowed("您好") is True


def test_bad_rate_env_never_crashes_chain(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path / "tts-cache")  # 隔离:别打真预合成缓存
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    monkeypatch.setenv("TTS_ENGINE", "auto")
    monkeypatch.setenv("TTS_CLOUD_RATE", "0,9")           # 配置坏值(逗号小数)
    eng = tts.get_engine()                                # 不许抛:回退默认语速
    assert isinstance(eng, tts.DashScopeQwenTtsEngine) and eng._rate == 0.9
    monkeypatch.setattr(tts, "_engine", None)
    monkeypatch.setenv("TTS_VOICE_PATH", str(tmp_path / "missing.onnx"))
    monkeypatch.setattr(tts.DashScopeQwenTtsEngine, "_call", lambda self, t: None)
    data, _, _ = tts.speak("您好")                        # 全链不 500,只降级
    assert data is None
    monkeypatch.setattr(tts, "_engine", None)


def test_qwen_tts_rate_in_cache_key_and_call(monkeypatch):
    # 语速必须进缓存键:调 TTS_CLOUD_RATE 后旧速缓存自然失效,不会全场命中旧语速
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    a = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)
    b = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.8)
    assert a.cache_params != b.cache_params
    seen = {}

    class FakeMMC:
        @staticmethod
        def call(**kw):
            seen.update(kw)

            class R:
                output = None
            return R()

    import dashscope
    monkeypatch.setattr(dashscope, "MultiModalConversation", FakeMMC)
    a.synthesize("您好")
    assert seen.get("speech_rate") == 0.9                 # 语速真传到了 API


def test_cosyvoice_partial_audio_not_cached(monkeypatch, tmp_path):
    # 中途 task-failed:SDK 返回半截音频 + get_response 非 task-finished → 作废不缓存
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")

    class FakeSynth:
        def __init__(self, **kw): ...
        def call(self, text, timeout_millis=None):
            return _wav()
        def get_response(self):
            return {"header": {"event": "task-failed"}}

    import dashscope.audio.tts_v2 as tts_v2
    monkeypatch.setattr(tts_v2, "SpeechSynthesizer", FakeSynth)
    eng = tts.DashScopeCosyVoiceEngine("cosyvoice-v2", "longyuan_v2", 0.9)
    assert eng.synthesize("您好") is None

    class FakeSynthOk(FakeSynth):
        def get_response(self):
            return {"header": {"event": "task-finished"}}

    monkeypatch.setattr(tts_v2, "SpeechSynthesizer", FakeSynthOk)
    assert eng.synthesize("您好") is not None


def test_cosyvoice_rejects_non_wav_or_oversized_sdk_bytes(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    monkeypatch.setattr(tts, "_MAX_CLOUD_TTS_BYTES", 64)
    eng = tts.DashScopeCosyVoiceEngine("cosyvoice-v2", "longyuan_v2", 0.9)

    for invalid in (b"not-a-wave" * 6, _wav(53)):
        monkeypatch.setattr(eng, "_call", lambda _text, value=invalid: value)
        assert eng.synthesize("您好") is None


# 下面这组守卫仍然逐条严格。qwen3-tts 改走官方流式之后不再下载 URL,所以它们
# 直接测守卫本身:挂在某个引擎上会让断言随该引擎的路径变化而空转。
def test_dashscope_download_upgrades_http_oss_url_over_https(monkeypatch):
    # 回归:OSS 临时地址常是 http://,签名与 scheme 无关,必须升 https 下载而不是丢弃。
    url = "http://dashscope-result.oss-cn-beijing.aliyuncs.com/a.wav?sig=x"
    got = {}

    class FakeResp:
        headers = {"Content-Type": "audio/wav", "Content-Length": "2012"}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def geturl(self): return got["url"]
        def read(self, _size=-1): return _wav()

    def fake_open(url, timeout=15):
        got["url"] = url
        return FakeResp()

    monkeypatch.setattr(tts, "_validated_dashscope_download_url",
                        lambda url: "https://" + url.split("://", 1)[-1])
    monkeypatch.setattr(tts, "_open_tts_download", fake_open)
    out = tts._download_dashscope_audio(url)
    assert out and out.startswith(b"RIFF")
    assert got["url"].startswith("https://")              # http→https 升级,签名不变,不走明文


def test_dashscope_download_rejects_untrusted_or_oversized_target(monkeypatch):
    # SSRF 守卫:环回/内网地址在发起请求之前就被拒。
    with pytest.raises(ValueError):
        tts._download_dashscope_audio("https://127.0.0.1/internal.wav")

    trusted = "https://dashscope-result.oss-cn-beijing.aliyuncs.com/a.wav"
    monkeypatch.setattr(tts, "_validated_dashscope_download_url", lambda url: url)

    class TooLarge:
        headers = {"Content-Type": "audio/wav", "Content-Length": str(17 * 1024 * 1024)}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def geturl(self): return "https://dashscope-result.oss-cn-beijing.aliyuncs.com/a.wav"
        def read(self, _size=-1): raise AssertionError("声明超限时不应读取响应体")

    monkeypatch.setattr(tts, "_open_tts_download", lambda *_a, **_k: TooLarge())
    with pytest.raises(ValueError):
        tts._download_dashscope_audio(trusted)


def test_dashscope_download_rejects_bad_metadata_or_non_wav(monkeypatch):
    trusted = "https://dashscope-result.oss-cn-beijing.aliyuncs.com/a.wav"
    monkeypatch.setattr(tts, "_validated_dashscope_download_url", lambda url: url)

    class Response:
        headers = {}
        payload = _wav()
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def geturl(self): return "https://dashscope-result.oss-cn-beijing.aliyuncs.com/a.wav"
        def read(self, _size=-1): return self.payload

    for label, content_type, length, payload in (
        ("非音频 Content-Type", "text/html", str(len(_wav())), _wav()),
        ("负数 Content-Length", "audio/wav", "-1", _wav()),
        ("下载到的不是 WAV", "audio/wav", "100", b"not-a-wave" * 10),
    ):
        response = Response()
        response.payload = payload
        response.headers = {"Content-Type": content_type, "Content-Length": length}
        monkeypatch.setattr(tts, "_open_tts_download", lambda *_a, **_k: response)
        with pytest.raises(ValueError):
            tts._download_dashscope_audio(trusted)


def test_inline_wav_base64_is_strict_and_bounded_before_and_after_decode(monkeypatch):
    monkeypatch.setattr(tts, "_MAX_CLOUD_TTS_BYTES", 64)

    exact = _wav(52)
    assert tts._validated_inline_wav_base64(
        base64.b64encode(exact).decode("ascii")) == exact

    invalid_envelopes = (
        "!!!!",
        base64.b64encode(b"not-a-wave" * 6).decode("ascii"),
        "A" * (tts._max_cloud_tts_base64_chars() + 1),
        base64.b64encode(_wav(53)).decode("ascii"),
    )
    for envelope in invalid_envelopes:
        with pytest.raises(ValueError):
            tts._validated_inline_wav_base64(envelope)


def test_fallback_piper_singleton_reused(monkeypatch, tmp_path):
    if importlib.util.find_spec("piper") is None:
        # 单例只在引擎可用时成立:包没装时 available() 恒假,每次调用都会重建一个
        # (便宜,且装上包后下一次即生效)。这条验的是可用状态下不逐句重载模型。
        pytest.skip("本机没装 piper,验不了降级层单例")
    fake_model = tmp_path / "voice.onnx"
    fake_model.write_bytes(b"onnx")                       # 模型不会被加载,内容无所谓
    monkeypatch.setenv("TTS_VOICE_PATH", str(fake_model))
    monkeypatch.setattr(tts, "_fallback_piper", None)
    a = tts._fallback_piper_engine()
    b = tts._fallback_piper_engine()
    assert a is b                                          # 云故障期不逐句重建/重载模型
    other = tmp_path / "other.onnx"
    other.write_bytes(b"onnx")
    monkeypatch.setenv("TTS_VOICE_PATH", str(other))
    assert tts._fallback_piper_engine() is not a           # 换模型路径即重建,不焊死
    monkeypatch.setattr(tts, "_fallback_piper", None)


# ---------------- 云 ASR:上下文偏置/降级 ----------------

def test_asr_auto_without_key_degrades_to_null(monkeypatch):
    monkeypatch.delenv("ASR_ENGINE", raising=False)
    assert isinstance(asr.get_engine(), asr.NullAsrEngine)


def test_asr_auto_with_key_uses_dashscope(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    monkeypatch.delenv("ASR_ENGINE", raising=False)
    assert isinstance(asr.get_engine(), asr.DashScopeAsrEngine)


def test_asr_cloud_transcribes_with_hotword_context(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    seen = {}

    def fake_call(self, audio_bytes, context):
        seen["context"] = context
        return asr._AsrCall(ok=True, text="这是胡萝卜")

    monkeypatch.setattr(asr.DashScopeAsrEngine, "_call", fake_call)
    eng = asr.DashScopeAsrEngine("qwen3-asr-flash")
    res = eng.transcribe(WEBM, asr.build_hotwords(BANK))
    assert res.asr_text == "这是胡萝卜" and res.hotword_hit
    assert "胡萝卜" in seen["context"]                    # 题库目标词喂进了偏置上下文


def test_asr_cloud_error_degrades(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")

    def boom(self, audio_bytes, context):
        raise RuntimeError("network down")

    monkeypatch.setattr(asr.DashScopeAsrEngine, "_call", boom)
    res = asr.DashScopeAsrEngine("qwen3-asr-flash").transcribe(WEBM, ["胡萝卜"])
    assert res.asr_text is None                          # 降级人工转写口径,不炸


def test_asr_cloud_successful_empty_transcript_is_silence_not_degradation(monkeypatch):
    """provider 明确成功、响应合法但一个字都没识别出来 = 沉默，不是技术失败。

    折成 None 的话，老人第一次没出声会被当成 ASR 不可用而暂停整场，冻结协议里
    那条 silence 一级提示永远播不出来。
    """
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    monkeypatch.setattr(
        asr.DashScopeAsrEngine, "_call",
        lambda self, audio_bytes, context: asr._AsrCall(ok=True, text=""))
    res = asr.DashScopeAsrEngine("qwen3-asr-flash").transcribe(WEBM, ["胡萝卜"])
    assert res.asr_text == ""                            # 空字符串，不是 None
    assert res.asr_text is not None
    assert res.hotword_hit is False
    assert res.asr_confidence is None


def test_asr_cloud_unsuccessful_call_stays_a_technical_failure(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    monkeypatch.setattr(
        asr.DashScopeAsrEngine, "_call",
        lambda self, audio_bytes, context: asr._AsrCall(ok=False))
    res = asr.DashScopeAsrEngine("qwen3-asr-flash").transcribe(WEBM, ["胡萝卜"])
    assert res.asr_text is None                          # 与"成功但空"严格区分


_NO_OUTPUT = object()
_NO_STATUS = object()


def _asr_response(*, status_code=200, choices=_NO_OUTPUT):
    fields = {}
    if status_code is not _NO_STATUS:
        fields["status_code"] = status_code
    fields["output"] = (
        None if choices is _NO_OUTPUT else SimpleNamespace(choices=choices))
    return SimpleNamespace(**fields)


def _asr_choice(content):
    return SimpleNamespace(message=SimpleNamespace(content=content))


_OK = asr._AsrCall(ok=True, text="胡萝卜")
_EMPTY = asr._AsrCall(ok=True, text="")
_BROKEN = asr._AsrCall(ok=False, text="")


@pytest.mark.parametrize(("case", "response", "expected"), [
    ("分段转写", _asr_response(choices=[_asr_choice([{"text": "胡萝卜"}])]), _OK),
    ("字符串转写", _asr_response(choices=[_asr_choice("胡萝卜")]), _OK),
    # 明确 200 + 合法转写结构 + 没有文本 → 运营层"本轮无转写"。
    ("分段但只有空白", _asr_response(choices=[_asr_choice([{"text": "  "}])]), _EMPTY),
    ("显式空字符串段", _asr_response(choices=[_asr_choice([{"text": ""}])]), _EMPTY),
    ("空字符串转写", _asr_response(choices=[_asr_choice("")]), _EMPTY),
    # 以下全部按技术失败 fail closed，绝不映射成"本轮无转写"。
    ("缺 status_code", _asr_response(status_code=_NO_STATUS,
                                     choices=[_asr_choice([{"text": "胡萝卜"}])]), _BROKEN),
    ("非 200", _asr_response(status_code=500,
                             choices=[_asr_choice([{"text": "胡萝卜"}])]), _BROKEN),
    ("status_code 非整数", _asr_response(status_code="200",
                                        choices=[_asr_choice([{"text": "x"}])]), _BROKEN),
    ("缺 output", _asr_response(), _BROKEN),
    ("空 choices", _asr_response(choices=[]), _BROKEN),
    ("空转写段列表", _asr_response(choices=[_asr_choice([])]), _BROKEN),
    ("段里缺 text", _asr_response(choices=[_asr_choice([{"foo": "bar"}])]), _BROKEN),
    ("段 text 不是字符串", _asr_response(choices=[_asr_choice([{"text": 42}])]), _BROKEN),
    ("混入坏段", _asr_response(
        choices=[_asr_choice([{"text": "胡"}, "萝卜"])]), _BROKEN),
    ("content 为 None", _asr_response(choices=[_asr_choice(None)]), _BROKEN),
    ("content 类型不对", _asr_response(choices=[_asr_choice(42)]), _BROKEN),
])
def test_asr_call_separates_empty_transcript_from_broken_response(
        monkeypatch, tmp_path, case, response, expected):
    from dashscope import MultiModalConversation

    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    # 患者音频副本只落测试临时目录，绝不写进 data/。
    monkeypatch.setattr(asr, "SCRATCH_DIR", tmp_path / "asr-scratch")
    monkeypatch.setattr(
        MultiModalConversation, "call",
        staticmethod(lambda **_kwargs: response))
    eng = asr.DashScopeAsrEngine("qwen3-asr-flash")
    assert eng._call(WEBM, "胡萝卜") == expected, case
    assert list((tmp_path / "asr-scratch").glob("asr-*")) == []   # 副本已删净


def test_asr_sniff_ext():
    assert asr._sniff_ext(WEBM) == ".webm"
    assert asr._sniff_ext(b"OggS" + b"\x00" * 8) == ".ogg"
    assert asr._sniff_ext(b"RIFF" + b"\x00" * 8) == ".wav"
    assert asr._sniff_ext(b"\xff\xf1junk") == ".webm"


# ---------------- Qwen 判分:严格解析/恒可降级 ----------------

def _ji():
    return build_judge_input(item_id="SE_胡萝卜", task_type="单要素", target_word="胡萝卜",
                             acceptable_expressions=("红萝卜",), asr_text="这是红萝卜")


def test_qwen_judge_parses_strict_json(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    payload = json.dumps({"answer_type": "正确", "score": 1, "needs_review": False, "reason": "命中可接受表达"},
                         ensure_ascii=False)
    monkeypatch.setattr(llm_judge.QwenJudge, "_call", lambda self, prompt: payload)
    j = llm_judge.QwenJudge("qwen-plus").judge(_ji())
    assert j is not None and j.answer_type is AnswerType.正确
    assert j.ai_score == 1.0 and j.ai_needs_review is False


def test_qwen_judge_accepts_fenced_json(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    fenced = '```json\n{"answer_type": "偏题", "score": 0, "needs_review": true, "reason": "x"}\n```'
    monkeypatch.setattr(llm_judge.QwenJudge, "_call", lambda self, prompt: fenced)
    j = llm_judge.QwenJudge("qwen-plus").judge(_ji())
    assert j is not None and j.answer_type is AnswerType.偏题 and j.ai_needs_review is True


@pytest.mark.parametrize("raw", [
    None, "", "不是JSON", '{"answer_type": "正确"}',            # 缺 score
    '{"answer_type": "满分", "score": 1}',                       # 枚举外类型
    '{"answer_type": "正确", "score": 0.7}',                     # 非法分值
    '["正确", 1]',                                               # 非对象
    # 下列值可被 Python 宽松转型，但不是与 prompt 一致的封闭 JSON 合同。
    '{"answer_type":"正确","score":"1","needs_review":0,"reason":{}}',
    # 回答类型与分值矛盾时不得任选一个当真。
    '{"answer_type":"正确","score":0,"needs_review":false,"reason":"x"}',
    '{"answer_type":"偏题","score":1,"needs_review":false,"reason":"x"}',
    # 缺字段、多字段和超长理由都必须回退到确定式规则。
    '{"answer_type":"正确","score":1,"reason":"x"}',
    '{"answer_type":"正确","score":1,"needs_review":false,"reason":"x","extra":1}',
    json.dumps({"answer_type": "正确", "score": 1,
                "needs_review": False, "reason": "x" * 501}),
])
def test_qwen_judge_suspicious_output_falls_back(monkeypatch, raw):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    monkeypatch.setattr(llm_judge.QwenJudge, "_call", lambda self, prompt: raw)
    assert llm_judge.QwenJudge("qwen-plus").judge(_ji()) is None


def test_qwen_judge_without_key_never_calls(monkeypatch):
    def boom(self, prompt):
        raise AssertionError("无 Key 不许出网")

    monkeypatch.setattr(llm_judge.QwenJudge, "_call", boom)
    assert llm_judge.QwenJudge("qwen-plus").judge(_ji()) is None


def test_llm_judge_auto_selection(monkeypatch):
    monkeypatch.delenv("LLM_JUDGE", raising=False)
    assert isinstance(llm_judge.get_engine(), llm_judge.OffLlmJudge)   # 无 Key:off
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    assert isinstance(llm_judge.get_engine(), llm_judge.QwenJudge)     # 有 Key:qwen
    monkeypatch.setenv("LLM_JUDGE", "off")
    assert isinstance(llm_judge.get_engine(), llm_judge.OffLlmJudge)   # 显式关优先


# ---------------- qwen3-tts 流式:官方 stream=True 直接拿字节 ----------------
#
# 真机实测:非流式请求本身是成功的(200/Serena/speech_rate=0.9),但它只回一个
# OSS URL。本机代理的 fake-IP DNS 把该域名解析到 198.18.0.0/15,SSRF 守卫按设计
# fail-closed,于是整条链降级。放宽守卫是错的;官方 stream=True 直接在响应里给
# 音频字节,连 URL 都不需要。
#
# 流式首块带的是"哨兵" RIFF 头:size 字段是 0x7fffffbf / 0x7fffff9b,不是真实
# 长度——必须在收全之后按实际长度封口,否则 wave 读到的帧数是天文数字。

_STREAM_FMT = (
    b"fmt " + (16).to_bytes(4, "little")
    + (1).to_bytes(2, "little")        # PCM
    + (1).to_bytes(2, "little")        # mono
    + (24000).to_bytes(4, "little")
    + (48000).to_bytes(4, "little")
    + (2).to_bytes(2, "little")
    + (16).to_bytes(2, "little")
)
_SENTINEL_RIFF = 0x7FFFFFBF
_SENTINEL_DATA = 0x7FFFFF9B


def _stream_header(first_pcm: bytes) -> bytes:
    """首块:哨兵 size 的 RIFF/WAVE/fmt/data 头 + 第一段 PCM。"""
    return (
        b"RIFF" + _SENTINEL_RIFF.to_bytes(4, "little") + b"WAVE"
        + _STREAM_FMT
        + b"data" + _SENTINEL_DATA.to_bytes(4, "little")
        + first_pcm
    )


class _StreamResponse:
    def __init__(self, data=None, finish_reason=None, status_code=200, code=None,
                 url=None):
        payload = {}
        if data is not None:
            payload["data"] = data
        if url is not None:
            payload["url"] = url
        self.status_code = status_code
        self.code = code
        self.output = SimpleNamespace(
            audio=payload or None, finish_reason=finish_reason)


def _stream_responses(chunks: list[bytes], *, url="http://oss.example/x.wav"):
    responses = [_StreamResponse(data=base64.b64encode(chunks[0]).decode("ascii"))]
    for chunk in chunks[1:]:
        responses.append(
            _StreamResponse(data=base64.b64encode(chunk).decode("ascii")))
    responses.append(_StreamResponse(finish_reason="stop", url=url))
    return responses


def _install_stream(monkeypatch, responses, seen=None):
    calls = {"n": 0}

    class FakeMMC:
        @staticmethod
        def call(**kw):
            calls["n"] += 1
            if seen is not None:
                seen.update(kw)
            return iter(responses)

    monkeypatch.setitem(
        __import__("sys").modules, "dashscope", SimpleNamespace(MultiModalConversation=FakeMMC))

    def no_download(*_a, **_k):
        raise AssertionError("流式已经拿到字节，不得再下载 URL")

    monkeypatch.setattr(tts, "_download_dashscope_audio", no_download)
    monkeypatch.setattr(tts, "_open_tts_download", no_download)
    return calls


def _pcm(seed: int, count: int) -> bytes:
    return bytes(((seed + i) % 251 for i in range(count)))


def test_qwen_failure_reasons_are_a_frozen_closed_set():
    """降级原因必须是固定闭集:动态原因会把 provider 文本带进日志。"""
    assert {reason.value for reason in tts._QwenTtsFailureReason} == {
        "api_key_missing", "provider_exception", "response_after_stop",
        "response_status_invalid", "response_error_code",
        "response_output_missing", "response_audio_missing",
        "finish_reason_invalid", "stream_not_finished", "stream_empty",
        "stream_chunk_invalid", "stream_size_exceeded", "wav_header_invalid",
        "wav_sentinel_invalid", "wav_format_invalid", "wav_payload_invalid",
        "wav_validation_invalid", "unexpected_internal",
    }
    failure = tts._QwenTtsFailure(tts._QwenTtsFailureReason.STREAM_EMPTY)
    assert isinstance(failure, ValueError)
    assert failure.reason is tts._QwenTtsFailureReason.STREAM_EMPTY
    assert str(failure) == "stream_empty"


_SECRETS = (
    "patient-text-secret", "sk-secret", "req-secret",
    "provider-message-secret", "https://secret.example/audio.wav",
    "base64-secret", "audio-bytes-secret",
)


def _assert_single_clean_warning(caplog, expected_reason: str):
    records = [r for r in caplog.records if r.name == "app.tts"]
    assert len(records) == 1, [r.getMessage() for r in records]
    record = records[0]
    assert record.levelname == "WARNING"
    assert record.getMessage() == f"qwen_tts_failed reason={expected_reason}"
    assert record.exc_info is None
    assert record.stack_info is None
    blob = caplog.text + record.getMessage() + repr(record.args)
    for secret in _SECRETS:
        assert secret not in blob, secret
    return record


_REASON_CASES = {
    "api_key_missing": ("no_key", None),
    "response_status_invalid": ("status", 500),
    "response_error_code": ("code", "Throttling"),
    "response_output_missing": ("no_output", None),
    "response_audio_missing": ("silent_middle", None),
    "finish_reason_invalid": ("bad_finish", "length"),
    "response_after_stop": ("after_stop", None),
    "stream_not_finished": ("never_stop", None),
    "stream_empty": ("stop_only", None),
    "stream_chunk_invalid": ("bad_b64", None),
    "stream_size_exceeded": ("oversize", None),
    "wav_header_invalid": ("bad_header", None),
    "wav_sentinel_invalid": ("bad_sentinel", None),
    "wav_format_invalid": ("bad_fmt", None),
    "wav_payload_invalid": ("unaligned", None),
    "provider_exception": ("boom", None),
}


def _responses_for(kind: str, extra):
    good = _stream_header(_pcm(1, 200))
    head = base64.b64encode(good).decode("ascii")
    if kind == "status":
        return [_StreamResponse(data=head, status_code=extra)]
    if kind == "code":
        return [_StreamResponse(data=head, code=extra)]
    if kind == "no_output":
        class _NoOutput:
            status_code = 200
            code = None
        return [_NoOutput()]
    if kind == "silent_middle":
        return [_StreamResponse(), _StreamResponse(finish_reason="stop")]
    if kind == "bad_finish":
        return [_StreamResponse(data=head, finish_reason=extra),
                _StreamResponse(finish_reason="stop")]
    if kind == "after_stop":
        return [_StreamResponse(data=head), _StreamResponse(finish_reason="stop"),
                _StreamResponse(finish_reason="stop")]
    if kind == "never_stop":
        return [_StreamResponse(data=head)]
    if kind == "stop_only":
        return [_StreamResponse(finish_reason="stop")]
    if kind == "bad_b64":
        return [_StreamResponse(data="!!!!"), _StreamResponse(finish_reason="stop")]
    if kind == "oversize":
        # 预算由调用方压到 64 字节,这个 envelope 解码前就顶穿剩余预算。
        return [_StreamResponse(data=base64.b64encode(_pcm(1, 400)).decode("ascii")),
                _StreamResponse(finish_reason="stop")]
    if kind == "bad_header":
        return [_StreamResponse(data=base64.b64encode(
            b"OggS" + good[4:]).decode("ascii")),
            _StreamResponse(finish_reason="stop")]
    if kind == "bad_sentinel":
        body = (b"RIFF" + (36 + 200).to_bytes(4, "little") + b"WAVE" + _fmt()
                + b"data" + (200).to_bytes(4, "little") + _pcm(1, 200))
        return [_StreamResponse(data=base64.b64encode(body).decode("ascii")),
                _StreamResponse(finish_reason="stop")]
    if kind == "bad_fmt":
        body = (b"RIFF" + _SENTINEL_RIFF.to_bytes(4, "little") + b"WAVE"
                + _fmt(channels=2)
                + b"data" + _SENTINEL_DATA.to_bytes(4, "little") + _pcm(1, 200))
        return [_StreamResponse(data=base64.b64encode(body).decode("ascii")),
                _StreamResponse(finish_reason="stop")]
    if kind == "unaligned":
        return [_StreamResponse(data=base64.b64encode(
            _stream_header(_pcm(1, 201))).decode("ascii")),
            _StreamResponse(finish_reason="stop")]
    raise AssertionError(kind)


@pytest.mark.parametrize("reason", sorted(_REASON_CASES))
def test_every_qwen_rejection_logs_exactly_one_safe_reason(
        monkeypatch, caplog, reason):
    """每个拒绝分支都映射到精确原因,并且只写一条不含任何秘密的日志。"""
    kind, extra = _REASON_CASES[reason]
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)
    if kind == "no_key":
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-secret")

    if kind == "boom":
        class FakeMMC:
            @staticmethod
            def call(**_kw):
                def _explode():
                    yield _StreamResponse(data=base64.b64encode(
                        _stream_header(_pcm(1, 200))).decode("ascii"))
                    raise RuntimeError("provider-message-secret req-secret")
                return _explode()

        monkeypatch.setitem(
            __import__("sys").modules, "dashscope",
            SimpleNamespace(MultiModalConversation=FakeMMC))
    elif kind != "no_key":
        if kind == "oversize":
            monkeypatch.setattr(tts, "_MAX_CLOUD_TTS_BYTES", 64)
        _install_stream(monkeypatch, _responses_for(kind, extra))

    with caplog.at_level(logging.WARNING, logger="app.tts"):
        assert eng.synthesize("patient-text-secret") is None

    _assert_single_clean_warning(caplog, reason)


def test_unexpected_internal_is_really_reachable_and_still_leaks_nothing(
        monkeypatch, caplog):
    """真正走通 unexpected_internal:内部路径抛的异常文本里带满秘密也不得外泄。

    不通过直接调用 _log_failure 造假:必须是一次真实 synthesize,流已经收全,
    然后封口那一步抛非 ValueError。异常本身携带 provider message、request id、
    URL、base64 与音频字节,正是最容易被 exc_info/traceback 带出去的形状。
    """
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-secret")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)
    _install_stream(monkeypatch, _stream_responses([_stream_header(_pcm(1, 400))]))

    def _explode(_raw):
        raise RuntimeError(
            "provider-message-secret req-secret "
            "https://secret.example/audio.wav base64-secret audio-bytes-secret")

    monkeypatch.setattr(tts, "_sealed_streamed_wav", _explode)

    with caplog.at_level(logging.WARNING, logger="app.tts"):
        assert eng.synthesize("patient-text-secret") is None

    record = _assert_single_clean_warning(caplog, "unexpected_internal")
    assert record.getMessage() == "qwen_tts_failed reason=unexpected_internal"


def test_a_secret_bearing_response_object_never_reaches_the_log(
        monkeypatch, caplog):
    """响应对象每个字段都真的塞了秘密,拒绝日志仍只有固定 enum。

    构造一个字段确实非空的响应,避免"断言了但根本没有东西可泄露"的空证明:
    status/code/request_id/message/audio.data/audio.url/原始音频字节全部带哨兵。
    走确定的 stream_chunk_invalid 分支——它真的读到了 audio.data 并尝试解码。
    """
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-secret")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)

    class _SecretBearingResponse:
        status_code = 200
        code = None
        request_id = "req-secret"
        message = "provider-message-secret"
        raw_audio = b"audio-bytes-secret"
        output = SimpleNamespace(
            audio={"data": "base64-secret",
                   "url": "https://secret.example/audio.wav"},
            finish_reason="null",
        )

    payload = _SecretBearingResponse()
    # 断言注入确实生效,否则这个测试会退化成空证明。
    assert payload.request_id == "req-secret"
    assert payload.message == "provider-message-secret"
    assert payload.output.audio["data"] == "base64-secret"
    assert payload.output.audio["url"] == "https://secret.example/audio.wav"
    assert payload.raw_audio == b"audio-bytes-secret"
    _install_stream(monkeypatch, [payload, _StreamResponse(finish_reason="stop")])

    with caplog.at_level(logging.WARNING, logger="app.tts"):
        assert eng.synthesize("patient-text-secret") is None

    record = _assert_single_clean_warning(caplog, "stream_chunk_invalid")
    # 逐个字段复核,不打印 response/repr。
    for field in (record.getMessage(), str(record.msg), repr(record.args),
                  caplog.text):
        for secret in _SECRETS:
            assert secret not in field, (secret, field)
    assert "audio-bytes-secret" not in str(record.args)


def test_qwen_wav_validation_failure_maps_to_its_own_reason(monkeypatch, caplog):
    """通用 WAV 校验(非流式结构)失败有独立原因,不与结构原因混淆。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-secret")
    monkeypatch.setattr(tts, "_validated_wav_bytes", lambda _data: (_ for _ in ()).throw(
        ValueError("provider-message-secret")))
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)
    _install_stream(monkeypatch, _stream_responses([_stream_header(_pcm(1, 400))]))

    with caplog.at_level(logging.WARNING, logger="app.tts"):
        assert eng.synthesize("patient-text-secret") is None

    _assert_single_clean_warning(caplog, "wav_validation_invalid")


def test_qwen_success_writes_no_failure_log(monkeypatch, caplog):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-secret")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)
    _install_stream(monkeypatch, _stream_responses([_stream_header(_pcm(1, 400))]))

    with caplog.at_level(logging.WARNING, logger="app.tts"):
        assert eng.synthesize("patient-text-secret") is not None

    assert [r for r in caplog.records if r.name == "app.tts"] == []


def test_qwen_logger_failure_is_swallowed(monkeypatch):
    """日志本身炸掉也只是观测能力受损,不能改变降级结果。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-secret")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)
    _install_stream(monkeypatch, [_StreamResponse(finish_reason="stop")])

    def _explode(*_a, **_k):
        raise RuntimeError("logging backend down")

    monkeypatch.setattr(tts.logger, "warning", _explode)
    assert eng.synthesize("patient-text-secret") is None


def test_qwen_provider_request_parameters_are_frozen(monkeypatch):
    """provider 请求参数是冻结契约:本批不得新增或改动任何一项。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-secret")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)
    seen: dict = {}
    _install_stream(
        monkeypatch, _stream_responses([_stream_header(_pcm(1, 400))]), seen)

    assert eng.synthesize("您好") is not None

    assert set(seen) == {
        "model", "text", "voice", "language_type", "stream", "speech_rate",
        "request_timeout",
    }
    assert "incremental_output" not in seen
    assert seen["stream"] is True
    assert seen["language_type"] == "Chinese"
    assert seen["speech_rate"] == 0.9
    assert seen["request_timeout"] == 15


def test_qwen_tts_streams_once_and_seals_the_real_riff_sizes(monkeypatch):
    """官方 stream=True:一次调用累计字节,按实际长度封口,不碰 URL。"""
    import struct
    import wave

    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)
    first, rest = _pcm(1, 400), [_pcm(7, 600), _pcm(13, 800)]
    seen: dict = {}
    calls = _install_stream(
        monkeypatch, _stream_responses([_stream_header(first), *rest]), seen)

    audio = eng.synthesize("您好")

    # 恰好一次 provider 调用,官方流式参数齐备。
    assert calls["n"] == 1
    assert seen["stream"] is True
    assert seen["language_type"] == "Chinese"
    assert seen["speech_rate"] == 0.9
    assert seen["voice"] == "Serena"

    assert audio is not None
    payload_len = len(first) + sum(len(chunk) for chunk in rest)
    # RIFF/data 两个 size 都必须换成真实长度,哨兵不得留下。
    assert struct.unpack_from("<I", audio, 4)[0] == len(audio) - 8
    data_at = audio.index(b"data")
    assert struct.unpack_from("<I", audio, data_at + 4)[0] == payload_len
    assert len(audio) == 44 + payload_len

    # 封口只改两个 size 字段:除 4:8 与 40:44 外,连一个 PCM 字节都不许动。
    streamed = _stream_header(first) + b"".join(rest)
    assert len(audio) == len(streamed)
    assert audio[0:4] == streamed[0:4]
    assert audio[8:40] == streamed[8:40]
    assert audio[44:] == streamed[44:]
    assert audio[44:] == first + b"".join(rest)

    with wave.open(__import__("io").BytesIO(audio), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 24000
        assert handle.getsampwidth() == 2
        assert handle.getnframes() == payload_len // 2
        assert handle.getnframes() > 0

    assert tts._validated_wav_bytes(audio) == audio


def test_qwen_tts_stream_rejects_every_malformed_shape(monkeypatch):
    """任何一处不对都返回 None:上层转 204,且绝不写缓存。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)
    good = _stream_header(_pcm(1, 200))

    cases = {
        "http 非 200": [_StreamResponse(status_code=500)],
        "错误码非空": [_StreamResponse(
            data=base64.b64encode(good).decode("ascii"), code="Throttling")],
        "没有任何音频": [_StreamResponse(finish_reason="stop")],
        "缺终态 stop": [_StreamResponse(data=base64.b64encode(good).decode("ascii"))],
        "终态非 stop": [
            _StreamResponse(data=base64.b64encode(good).decode("ascii")),
            _StreamResponse(finish_reason="length"),
        ],
        "非法 base64": [_StreamResponse(data="!!!!"),
                        _StreamResponse(finish_reason="stop")],
        "首块不是 RIFF": [
            _StreamResponse(data=base64.b64encode(b"OggS" + good[4:]).decode("ascii")),
            _StreamResponse(finish_reason="stop")],
        "首块不是 WAVE": [
            _StreamResponse(data=base64.b64encode(
                good[:8] + b"AVIX" + good[12:]).decode("ascii")),
            _StreamResponse(finish_reason="stop")],
        "缺 fmt 块": [
            _StreamResponse(data=base64.b64encode(
                b"RIFF" + _SENTINEL_RIFF.to_bytes(4, "little") + b"WAVE"
                + b"data" + _SENTINEL_DATA.to_bytes(4, "little") + _pcm(1, 100),
            ).decode("ascii")),
            _StreamResponse(finish_reason="stop")],
        "缺 data 块": [
            _StreamResponse(data=base64.b64encode(
                b"RIFF" + _SENTINEL_RIFF.to_bytes(4, "little") + b"WAVE"
                + _STREAM_FMT).decode("ascii")),
            _StreamResponse(finish_reason="stop")],
        "data 载荷为空": [
            _StreamResponse(data=base64.b64encode(_stream_header(b"")).decode("ascii")),
            _StreamResponse(finish_reason="stop")],
        "块头越界": [
            _StreamResponse(data=base64.b64encode(
                b"RIFF" + _SENTINEL_RIFF.to_bytes(4, "little") + b"WAVE" + b"fm",
            ).decode("ascii")),
            _StreamResponse(finish_reason="stop")],
        "终态后又来 data": [
            _StreamResponse(data=base64.b64encode(good).decode("ascii")),
            _StreamResponse(finish_reason="stop"),
            _StreamResponse(data=base64.b64encode(_pcm(3, 100)).decode("ascii"))],
        "output 畸形": [_StreamResponse(), _StreamResponse(finish_reason="stop")],
    }
    for label, responses in cases.items():
        _install_stream(monkeypatch, responses)
        assert eng.synthesize("您好") is None, label


def _fmt(*, audio_format=1, channels=1, rate=24000, byte_rate=48000,
         block_align=2, bits=16, size=16) -> bytes:
    return (
        b"fmt " + size.to_bytes(4, "little")
        + audio_format.to_bytes(2, "little") + channels.to_bytes(2, "little")
        + rate.to_bytes(4, "little") + byte_rate.to_bytes(4, "little")
        + block_align.to_bytes(2, "little") + bits.to_bytes(2, "little")
    )


def test_qwen_tts_stream_only_seals_the_exact_expected_pcm_layout(monkeypatch):
    """封口只对精确的 PCM mono/24k/16-bit 生效,其他布局一律 fail-closed。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)

    def stream_with(fmt_chunk: bytes, payload: bytes):
        head = (b"RIFF" + _SENTINEL_RIFF.to_bytes(4, "little") + b"WAVE"
                + fmt_chunk
                + b"data" + _SENTINEL_DATA.to_bytes(4, "little") + payload)
        return [_StreamResponse(data=base64.b64encode(head).decode("ascii")),
                _StreamResponse(finish_reason="stop")]

    rejected = {
        "非 PCM 编码": stream_with(_fmt(audio_format=3), _pcm(1, 200)),
        "立体声": stream_with(_fmt(channels=2), _pcm(1, 200)),
        "采样率不是 24000": stream_with(_fmt(rate=16000), _pcm(1, 200)),
        "byte_rate 不自洽": stream_with(_fmt(byte_rate=96000), _pcm(1, 200)),
        "block_align 不是 2": stream_with(_fmt(block_align=4), _pcm(1, 200)),
        "位深不是 16": stream_with(_fmt(bits=8), _pcm(1, 200)),
        "fmt 块长度异常": stream_with(_fmt(size=18) + b"\x00\x00", _pcm(1, 200)),
        "data 未按 block_align 对齐": stream_with(_fmt(), _pcm(1, 201)),
        "重复 fmt 块": [
            _StreamResponse(data=base64.b64encode(
                b"RIFF" + _SENTINEL_RIFF.to_bytes(4, "little") + b"WAVE"
                + _fmt() + _fmt()
                + b"data" + _SENTINEL_DATA.to_bytes(4, "little") + _pcm(1, 200),
            ).decode("ascii")),
            _StreamResponse(finish_reason="stop")],
        # 官方头实测固定 44 字节。任何前置 chunk 都会把 data 挪位,不是这条流。
        "RIFF 内插 JUNK": [
            _StreamResponse(data=base64.b64encode(
                b"RIFF" + _SENTINEL_RIFF.to_bytes(4, "little") + b"WAVE"
                + b"JUNK" + (2).to_bytes(4, "little") + b"\x00\x00"
                + _fmt()
                + b"data" + _SENTINEL_DATA.to_bytes(4, "little") + _pcm(1, 200),
            ).decode("ascii")),
            _StreamResponse(finish_reason="stop")],
        "fmt 不在 offset 12": [
            _StreamResponse(data=base64.b64encode(
                b"RIFF" + _SENTINEL_RIFF.to_bytes(4, "little") + b"WAVE"
                + b"LIST" + (4).to_bytes(4, "little") + b"INFO"
                + _fmt()
                + b"data" + _SENTINEL_DATA.to_bytes(4, "little") + _pcm(1, 200),
            ).decode("ascii")),
            _StreamResponse(finish_reason="stop")],
    }
    for label, responses in rejected.items():
        _install_stream(monkeypatch, responses)
        assert eng.synthesize("您好") is None, label

    _install_stream(monkeypatch, stream_with(_fmt(), _pcm(1, 200)))
    assert eng.synthesize("您好") is not None


def test_qwen_tts_stream_only_reseals_the_official_sentinel_sizes(monkeypatch):
    """只有官方哨兵 size 才允许被改写:正常或恶意 size 的 WAV 一律不碰。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)
    payload = _pcm(1, 200)

    def stream_sized(riff_size: int, data_size: int):
        head = (b"RIFF" + riff_size.to_bytes(4, "little") + b"WAVE" + _fmt()
                + b"data" + data_size.to_bytes(4, "little") + payload)
        return [_StreamResponse(data=base64.b64encode(head).decode("ascii")),
                _StreamResponse(finish_reason="stop")]

    rejected = {
        # 一个本来就自洽的普通 WAV:不是这条流式路径的产物,不该被重写。
        "真实 size 的普通 WAV": stream_sized(36 + len(payload), len(payload)),
        "只有 RIFF 是哨兵": stream_sized(_SENTINEL_RIFF, len(payload)),
        "只有 data 是哨兵": stream_sized(36 + len(payload), _SENTINEL_DATA),
        "伪造的其他哨兵": stream_sized(0x7FFFFFFF, 0x7FFFFFFF),
        "零 size": stream_sized(0, 0),
    }
    for label, responses in rejected.items():
        _install_stream(monkeypatch, responses)
        assert eng.synthesize("您好") is None, label

    _install_stream(monkeypatch, stream_sized(_SENTINEL_RIFF, _SENTINEL_DATA))
    assert eng.synthesize("您好") is not None


@pytest.mark.parametrize("mid_reason", [None, "null"])
def test_qwen_tts_stream_accepts_the_real_intermediate_finish_reason(
        monkeypatch, mid_reason):
    """真机中间块的 finish_reason 是字面字符串 "null",不是 None,两者都必须接受。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)
    first, second = _stream_header(_pcm(1, 400)), _pcm(7, 600)
    _install_stream(monkeypatch, [
        _StreamResponse(data=base64.b64encode(first).decode("ascii"),
                        finish_reason=mid_reason),
        _StreamResponse(data=base64.b64encode(second).decode("ascii"),
                        finish_reason=mid_reason),
        _StreamResponse(finish_reason="stop"),
    ])

    audio = eng.synthesize("您好")

    assert audio is not None
    assert len(audio) == 44 + 400 + 600


def test_qwen_tts_stream_rejects_other_intermediate_finish_reasons(monkeypatch):
    """除 None/"null" 外的中间终态值一律拒绝,不能当成"还没结束"继续收。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)
    head = base64.b64encode(_stream_header(_pcm(1, 400))).decode("ascii")
    for reason in ("length", "error", "NULL", "", "None", "stop_sequence"):
        _install_stream(monkeypatch, [
            _StreamResponse(data=head, finish_reason=reason),
            _StreamResponse(finish_reason="stop"),
        ])
        assert eng.synthesize("您好") is None, reason


def test_qwen_tts_stream_requires_an_explicit_success_status(monkeypatch):
    """status_code 必须显式存在且精确表示 200。

    真实 SDK 给的是 http.HTTPStatus.OK(IntEnum),所以按 int 子类接收;但 bool
    也是 int 子类,而 "200"/200.0 根本不是状态码,这些都必须 fail closed。
    """
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)
    head = base64.b64encode(_stream_header(_pcm(1, 400))).decode("ascii")

    class _NoStatus:
        def __init__(self, data=None, finish_reason=None):
            payload = {"data": data} if data is not None else None
            self.code = None
            self.output = SimpleNamespace(audio=payload, finish_reason=finish_reason)

    missing = [_NoStatus(data=head), _NoStatus(finish_reason="stop")]
    _install_stream(monkeypatch, missing)
    assert eng.synthesize("您好") is None, "缺失 status_code"

    # dashscope 1.26.3 的 streaming transport 给的是 http.HTTPStatus.OK(IntEnum),
    # 不是裸 int。按类型精确等于 int 去卡会把真机每一个包都判死。
    from http import HTTPStatus

    _install_stream(monkeypatch, [
        _StreamResponse(data=head, status_code=HTTPStatus.OK),
        _StreamResponse(finish_reason="stop", status_code=HTTPStatus.OK),
    ])
    assert eng.synthesize("您好") is not None, "真实 HTTPStatus.OK 必须被接受"

    for bad_status in ("200", True, 200.0, None, 201):
        responses = [
            _StreamResponse(data=head, status_code=bad_status),
            _StreamResponse(finish_reason="stop", status_code=bad_status),
        ]
        _install_stream(monkeypatch, responses)
        assert eng.synthesize("您好") is None, repr(bad_status)

    _install_stream(monkeypatch, _stream_responses([_stream_header(_pcm(1, 400))]))
    assert eng.synthesize("您好") is not None


def test_qwen_tts_stream_rejects_anything_after_the_first_stop(monkeypatch):
    """第一个 stop 之后任何 response 都拒绝,包括第二个空 stop。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)
    head = base64.b64encode(_stream_header(_pcm(1, 400))).decode("ascii")

    trailing = {
        "第二个空 stop": _StreamResponse(finish_reason="stop"),
        "终态后空 response": _StreamResponse(),
        "终态后又来 null": _StreamResponse(finish_reason="null"),
        "终态后再来音频": _StreamResponse(
            data=base64.b64encode(_pcm(3, 100)).decode("ascii")),
    }
    for label, extra in trailing.items():
        _install_stream(monkeypatch, [
            _StreamResponse(data=head),
            _StreamResponse(finish_reason="stop"),
            extra,
        ])
        assert eng.synthesize("您好") is None, label


def test_qwen_tts_stream_bounds_each_envelope_by_remaining_budget(monkeypatch):
    """解码前上限按剩余预算算,不是全局上限:跨块累计不能被单块上限放过。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    monkeypatch.setattr(tts, "_MAX_CLOUD_TTS_BYTES", 44 + 600)
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)

    # 直接看 helper:同一个 envelope 在预算充足时合法,在剩余预算不足时必须
    # 在"解码前"就被拒——而不是先解码出来再按累计上限回收。
    envelope = base64.b64encode(_pcm(5, 400)).decode("ascii")
    assert len(envelope) <= tts._max_cloud_tts_base64_chars()
    assert tts._decoded_stream_chunk(envelope, 0) == _pcm(5, 400)
    with pytest.raises(tts._QwenTtsFailure) as raised:
        tts._decoded_stream_chunk(envelope, 44 + 400)
    assert raised.value.reason is tts._QwenTtsFailureReason.STREAM_SIZE_EXCEEDED

    # 更强的证据:超出剩余预算的 envelope 根本不能走进 decoder。
    # 用独立的 context 只包这段 spy——共享的 monkeypatch.undo() 会把 conftest
    # 的 autouse 隔离(音频目录/云 Key)一起撤掉。
    decoded = {"n": 0}
    real_decode = base64.b64decode

    def counting_decode(*args, **kwargs):
        decoded["n"] += 1
        return real_decode(*args, **kwargs)

    with pytest.MonkeyPatch.context() as spy:
        spy.setattr(tts.base64, "b64decode", counting_decode)
        with pytest.raises(tts._QwenTtsFailure):
            tts._decoded_stream_chunk(envelope, 44 + 400)
        assert decoded["n"] == 0, "越界的 envelope 不得进入 base64 解码"
        assert tts._decoded_stream_chunk(envelope, 0) == _pcm(5, 400)
        assert decoded["n"] == 1

    # 端到端仍旧 fail-closed,且正好用满剩余预算的一次流式成功。
    first = _stream_header(_pcm(1, 400))
    _install_stream(monkeypatch, _stream_responses([first, _pcm(5, 400)]))
    assert eng.synthesize("您好") is None
    _install_stream(monkeypatch, _stream_responses([first, _pcm(5, 200)]))
    assert eng.synthesize("您好") is not None


def test_qwen_tts_stream_rejects_a_response_without_output(monkeypatch):
    """output 缺失是畸形响应,不能当成"这块没音频"静默跳过。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)
    head = base64.b64encode(_stream_header(_pcm(1, 400))).decode("ascii")

    class _NoOutput:
        status_code = 200
        code = None

    _install_stream(monkeypatch, [
        _StreamResponse(data=head), _NoOutput(), _StreamResponse(finish_reason="stop")])
    assert eng.synthesize("您好") is None

    _install_stream(monkeypatch, [
        _StreamResponse(data=head),
        _StreamResponse(data=None, finish_reason=None),
        _StreamResponse(finish_reason="stop")])
    assert eng.synthesize("您好") is None, "output.audio 为 None 也不是合法中间块"


def test_qwen_tts_stream_is_bounded_before_and_after_decode(monkeypatch):
    """单块解码前上限 + 累计解码后上限,超限必须在继续增长前 fail-closed。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    monkeypatch.setattr(tts, "_MAX_CLOUD_TTS_BYTES", 512)
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)

    # 单块解码前就超过 base64 上限:根本不解码。
    _install_stream(monkeypatch, [
        _StreamResponse(data="A" * (tts._max_cloud_tts_base64_chars() + 4)),
        _StreamResponse(finish_reason="stop"),
    ])
    assert eng.synthesize("您好") is None

    # 每块都合法,但累计越界:必须在越界那一刻停,不能先攒完再判。
    header = _stream_header(_pcm(1, 200))
    _install_stream(monkeypatch, _stream_responses(
        [header, _pcm(5, 200), _pcm(9, 200), _pcm(11, 200)]))
    assert eng.synthesize("您好") is None

    # 恰好贴着上限的一次流式仍然成功。
    monkeypatch.setattr(tts, "_MAX_CLOUD_TTS_BYTES", 44 + 400)
    _install_stream(monkeypatch, _stream_responses([_stream_header(_pcm(1, 400))]))
    assert eng.synthesize("您好") is not None


def test_qwen_tts_stream_cache_hit_does_not_call_the_provider(monkeypatch, tmp_path):
    """语速仍进缓存键;第二次是缓存命中,不再调用 provider。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path / "tts-cache")
    monkeypatch.setattr(tts, "cloud_text_allowed", lambda _text: True)
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)
    monkeypatch.setattr(tts, "get_autopilot_engine", lambda: eng)
    calls = _install_stream(
        monkeypatch, _stream_responses([_stream_header(_pcm(1, 400))]))

    first, version, cached_first = tts.speak_autopilot("您好")
    second, _, cached_second = tts.speak_autopilot("您好")

    assert first is not None and second == first
    assert version == "dashscope/qwen3-tts-flash/Serena"
    assert (cached_first, cached_second) == (False, True)
    assert calls["n"] == 1
    assert eng.cache_params == "speech_rate=0.9"


def test_qwen_tts_tolerates_explicit_empty_data_boundary_chunks(monkeypatch):
    """2026-08-07 线上实测:qwen3-tts-flash 会在 stop 前发 data="" 的边界块
    (键在、值空,finish_reason 仍是未完成)。带键而空=合法空块必须跳过;
    连 data 键都没有的 silent_middle 仍按畸形拒绝(防 payload 位置漂移)。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena", speech_rate=0.9)
    first, second = _pcm(1, 400), _pcm(7, 600)
    responses = [
        _StreamResponse(data=base64.b64encode(_stream_header(first)).decode("ascii")),
        _StreamResponse(data=""),                       # 中段边界块
        _StreamResponse(data=base64.b64encode(second).decode("ascii")),
        _StreamResponse(data="", finish_reason="stop"), # 终态也可能带空 data
    ]
    _install_stream(monkeypatch, responses)

    audio = eng.synthesize("您好")

    assert audio is not None
    assert audio[44:] == first + second
