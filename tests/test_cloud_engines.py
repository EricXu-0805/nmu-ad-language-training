"""云引擎(DashScope TTS/ASR/判分)单测——全替身,零网络。

红线覆盖:白名单外文本永不出网;引擎失败恒降级;判分格式可疑一律回退规则。
"""
import base64
import json

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


def test_qwen_tts_downloads_http_oss_url_over_https(monkeypatch):
    # 回归:qwen3-tts-flash 常只回 http:// 的 OSS 临时地址,旧代码只认 https:// → 把成功结果丢了。
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena")
    monkeypatch.setattr(eng, "_call",
                        lambda text: {"data": "", "url": "http://dashscope-result.oss-cn-beijing.aliyuncs.com/a.wav?sig=x"})
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
    out = eng.synthesize("您好")
    assert out and out.startswith(b"RIFF")
    assert got["url"].startswith("https://")              # http→https 升级,签名不变,不走明文


def test_qwen_tts_rejects_untrusted_or_oversized_download(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena")
    monkeypatch.setattr(eng, "_call",
                        lambda text: {"data": "", "url": "https://127.0.0.1/internal.wav"})
    assert eng.synthesize("您好") is None

    monkeypatch.setattr(eng, "_call", lambda text: {
        "data": "", "url": "https://dashscope-result.oss-cn-beijing.aliyuncs.com/a.wav",
    })
    monkeypatch.setattr(tts, "_validated_dashscope_download_url", lambda url: url)

    class TooLarge:
        headers = {"Content-Type": "audio/wav", "Content-Length": str(17 * 1024 * 1024)}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def geturl(self): return "https://dashscope-result.oss-cn-beijing.aliyuncs.com/a.wav"
        def read(self, _size=-1): raise AssertionError("声明超限时不应读取响应体")

    monkeypatch.setattr(tts, "_open_tts_download", lambda *_a, **_k: TooLarge())
    assert eng.synthesize("您好") is None


def test_qwen_tts_rejects_bad_download_metadata_or_non_wav(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena")
    monkeypatch.setattr(eng, "_call", lambda text: {
        "data": "", "url": "https://dashscope-result.oss-cn-beijing.aliyuncs.com/a.wav",
    })
    monkeypatch.setattr(tts, "_validated_dashscope_download_url", lambda url: url)

    class Response:
        headers = {}
        payload = _wav()
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def geturl(self): return "https://dashscope-result.oss-cn-beijing.aliyuncs.com/a.wav"
        def read(self, _size=-1): return self.payload

    wrong_type = Response()
    wrong_type.headers = {"Content-Type": "text/html", "Content-Length": str(len(_wav()))}
    monkeypatch.setattr(tts, "_open_tts_download", lambda *_a, **_k: wrong_type)
    assert eng.synthesize("您好") is None

    negative_length = Response()
    negative_length.headers = {"Content-Type": "audio/wav", "Content-Length": "-1"}
    monkeypatch.setattr(tts, "_open_tts_download", lambda *_a, **_k: negative_length)
    assert eng.synthesize("您好") is None

    non_wav = Response()
    non_wav.payload = b"not-a-wave" * 10
    non_wav.headers = {"Content-Type": "audio/wav", "Content-Length": str(len(non_wav.payload))}
    monkeypatch.setattr(tts, "_open_tts_download", lambda *_a, **_k: non_wav)
    assert eng.synthesize("您好") is None


def test_qwen_tts_uses_inline_base64_when_present(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena")
    raw = _wav(92)
    monkeypatch.setattr(eng, "_call", lambda text: {"data": base64.b64encode(raw).decode(), "url": ""})

    def no_net(*a, **k):
        raise AssertionError("有内联 base64 就不该再下载 URL")

    monkeypatch.setattr("urllib.request.urlopen", no_net)
    assert eng.synthesize("您好") == raw


def test_qwen_inline_base64_is_strict_and_bounded_before_and_after_decode(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    monkeypatch.setattr(tts, "_MAX_CLOUD_TTS_BYTES", 64)
    eng = tts.DashScopeQwenTtsEngine("qwen3-tts-flash", "Serena")

    exact = _wav(52)
    monkeypatch.setattr(eng, "_call", lambda text: {
        "data": base64.b64encode(exact).decode("ascii"), "url": "",
    })
    assert eng.synthesize("您好") == exact

    invalid_envelopes = (
        "!!!!",
        base64.b64encode(b"not-a-wave" * 6).decode("ascii"),
        "A" * (tts._max_cloud_tts_base64_chars() + 1),
        base64.b64encode(_wav(53)).decode("ascii"),
    )
    for envelope in invalid_envelopes:
        monkeypatch.setattr(eng, "_call", lambda text, value=envelope: {
            "data": value, "url": "",
        })
        assert eng.synthesize("您好") is None


def test_fallback_piper_singleton_reused(monkeypatch, tmp_path):
    fake_model = tmp_path / "voice.onnx"
    fake_model.write_bytes(b"onnx")                       # available() 只查存在,不加载
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
        return "这是胡萝卜"

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
