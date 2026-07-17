"""小语的声音——可插拔 TTS(云端优先,本地降级,可关闭)。

口径(2026-07-16 更新,Eric 确认备案已完成):云语音 API 解禁,但守两条红线:
  1. **白名单守卫**:发往云端的文本只能是题库/脚本/固定 UI 话术的闭集
     (content.tts_allowlist),永不携带患者字段;白名单外→云引擎拒合成,fail-closed。
  2. **一次合成永久缓存**:话术闭集按 sha256(引擎+参数+文本) 落盘 data/tts-cache/,
     同句只出一次网;预合成脚本 scripts/presynthesize_tts.py 可离线打满缓存。

引擎(TTS_ENGINE 选择,默认 auto):
- auto:有 DASHSCOPE_API_KEY → qwen-tts(Eric 2026-07-17 耳测拍板:苏瑶,龙媛偏闷);
  无 → piper;piper 模型也缺 → null。
- qwen-tts:阿里百炼 qwen3-tts-flash(TTS_QWEN_MODEL),音色 Serena 苏瑶
  (TTS_QWEN_VOICE,温柔女声),语速 TTS_CLOUD_RATE(默认 0.9,老人节奏——
  speech_rate 参数官方文档未列但真机实测有效:0.7 时音频时长 +39%≈1/0.7)。
- cosyvoice:阿里百炼 cosyvoice-v2(TTS_COSY_MODEL),音色 longyuan_v2 龙媛
  (TTS_COSY_VOICE),同用 TTS_CLOUD_RATE;耳测备选。
  (v3-plus 需工作空间单独开通;当前 Key 的空间未开,默认落 v2——同款龙媛音色。)
  (模型/音色环境变量按引擎分家——共用一个变量做 A/B 耳测会互相污染。)
- piper:本地 onnx(TTS_VOICE_PATH,默认 data/tts/zh_CN-huayan-medium.onnx),
  云端不可用/文本不在白名单时的降级层;文本不出机器,无白名单限制。
- null:恒降级(前端回退浏览器系统语音,再无则静音)。
云引擎失败(断网/欠费/超时)自动落回 piper→null,链路永不 500。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import wave
from pathlib import Path
from typing import Protocol

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = DATA_DIR / "tts-cache"
DEFAULT_VOICE = DATA_DIR / "tts" / "zh_CN-huayan-medium.onnx"


class TtsProvider(Protocol):
    version: str

    def synthesize(self, text: str) -> bytes | None: ...


class NullTtsEngine:
    """占位引擎:恒降级(返回 None → 前端回退系统语音)。"""
    version = "null-0"
    cloud = False
    cache_params = ""

    def synthesize(self, text: str) -> bytes | None:
        return None


class PiperTtsEngine:
    """piper onnx 本地合成。模型懒加载 + 单线程锁(onnx session 非并发安全)。"""

    cloud = False
    # 缓存键里带合成参数指纹:调参后旧缓存自然失效,不会全场命中旧语速
    cache_params = "length_scale=1.15"

    def __init__(self, voice_path: Path):
        self._voice_path = voice_path
        self._voice = None
        self._lock = threading.Lock()
        self.version = f"piper/{voice_path.stem}"

    def available(self) -> bool:
        return self._voice_path.exists()

    def synthesize(self, text: str) -> bytes | None:
        if not self.available():
            return None
        with self._lock:
            if self._voice is None:
                from piper import PiperVoice  # 延迟 import:未装 piper 的部署机不加载
                self._voice = PiperVoice.load(str(self._voice_path))
            from piper import SynthesisConfig
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                # length_scale 1.15:比常速略慢——老人节奏,与浏览器语音 rate 0.85 同一意图
                self._voice.synthesize_wav(text, w, syn_config=SynthesisConfig(length_scale=1.15))
            return buf.getvalue()


class DashScopeCosyVoiceEngine:
    """阿里百炼 CosyVoice 云合成(wav 24k mono)。显式 speech_rate——老人慢速可精确控制。"""

    cloud = True

    def __init__(self, model: str, voice: str, speech_rate: float):
        self._model = model
        self._voice = voice
        self._rate = speech_rate
        self.version = f"dashscope/{model}/{voice}"
        self.cache_params = f"speech_rate={speech_rate}"

    def available(self) -> bool:
        return bool(os.environ.get("DASHSCOPE_API_KEY"))

    def synthesize(self, text: str) -> bytes | None:
        if not self.available():
            return None
        data = self._call(text)
        return bytes(data) if isinstance(data, (bytes, bytearray)) else None

    def _call(self, text: str):
        from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer
        synth = SpeechSynthesizer(model=self._model, voice=self._voice,
                                  format=AudioFormat.WAV_24000HZ_MONO_16BIT,
                                  speech_rate=self._rate)
        data = synth.call(text, timeout_millis=15000)
        # 中途 task-failed 时 SDK 可能返回已收到的半截音频——没跑到 task-finished
        # 一律作废,防止残句被永久写进缓存(缓存投毒)
        resp = synth.get_response() or {}
        if (resp.get("header") or {}).get("event") != "task-finished":
            return None
        return data


class DashScopeQwenTtsEngine:
    """阿里百炼 qwen3-tts-flash 云合成(小语主音色:苏瑶)。
    speech_rate 官方文档未列但真机实测有效(0.7→时长+39%≈1/0.7);万一后端哪天
    静默丢弃该参数,只是回到常速,不炸链路。"""

    cloud = True

    def __init__(self, model: str, voice: str, speech_rate: float = 1.0):
        self._model = model
        self._voice = voice
        self._rate = speech_rate
        self.version = f"dashscope/{model}/{voice}"
        self.cache_params = f"speech_rate={speech_rate}"

    def available(self) -> bool:
        return bool(os.environ.get("DASHSCOPE_API_KEY"))

    def synthesize(self, text: str) -> bytes | None:
        if not self.available():
            return None
        audio = self._call(text)
        if not isinstance(audio, dict):
            return None
        b64 = audio.get("data")
        if b64:
            import base64
            try:
                return base64.b64decode(b64)
            except Exception:
                return None
        url = audio.get("url")
        if url and url.startswith(("http://", "https://")):
            # 百炼 qwen3-tts 常只回 http:// 的 OSS 临时地址;OSS 查询串签名与 scheme 无关,
            # 强制升 https 下载(不走明文),旧代码只认 https:// 会把成功结果整个丢弃。
            import urllib.request
            secure = "https://" + url.split("://", 1)[1]
            with urllib.request.urlopen(secure, timeout=15) as r:  # noqa: S310 —— 百炼签名临时下载地址
                return r.read()
        return None

    def _call(self, text: str):
        from dashscope import MultiModalConversation
        resp = MultiModalConversation.call(model=self._model, text=text, voice=self._voice,
                                           language_type="Chinese", stream=False,
                                           speech_rate=self._rate,
                                           request_timeout=15)
        out = getattr(resp, "output", None)
        audio = getattr(out, "audio", None) if out is not None else None
        return dict(audio) if audio else None


def _env_float(name: str, default: float) -> float:
    """配置坏值(如 '0,9')不许炸链路——解析失败回退默认值,引擎照常工作。"""
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _cloud_engine(kind: str) -> TtsProvider:
    if kind == "qwen-tts":
        return DashScopeQwenTtsEngine(
            model=os.environ.get("TTS_QWEN_MODEL", "qwen3-tts-flash"),
            voice=os.environ.get("TTS_QWEN_VOICE", "Serena"),
            speech_rate=_env_float("TTS_CLOUD_RATE", 0.9))
    return DashScopeCosyVoiceEngine(
        model=os.environ.get("TTS_COSY_MODEL", "cosyvoice-v2"),
        voice=os.environ.get("TTS_COSY_VOICE", "longyuan_v2"),
        speech_rate=_env_float("TTS_CLOUD_RATE", 0.9))


def _piper_engine() -> PiperTtsEngine:
    return PiperTtsEngine(Path(os.environ.get("TTS_VOICE_PATH", str(DEFAULT_VOICE))))


def get_engine() -> TtsProvider:
    """按 TTS_ENGINE 选主引擎(auto/cosyvoice/qwen-tts/piper/null);不可用逐级降级。"""
    kind = os.environ.get("TTS_ENGINE", "auto")
    if kind == "null":
        return NullTtsEngine()
    if kind == "auto":
        kind = "qwen-tts" if os.environ.get("DASHSCOPE_API_KEY") else "piper"
    if kind in ("cosyvoice", "qwen-tts"):
        eng = _cloud_engine(kind)
        if eng.available():
            return eng
        kind = "piper"
    if kind == "piper":
        eng = _piper_engine()
        if eng.available():
            return eng
    return NullTtsEngine()


_engine: TtsProvider | None = None
_engine_lock = threading.Lock()


def engine() -> TtsProvider:
    """只缓存可用引擎;Null(未装/缺模型/缺 Key)不焊死——补齐后下一次请求即生效,无须重启。"""
    global _engine
    with _engine_lock:
        if _engine is None or isinstance(_engine, NullTtsEngine):
            _engine = get_engine()
        return _engine


# 云 TTS 白名单(题库+脚本+固定话术闭集)。缓存键=逐文件 (mtime_ns, size) 元组:
# 不用 max(mtime)——保留时间戳的恢复/回滚(cp -p/rsync -t)会被 max 掩蔽,
# 紧急下架的句子在窗口内仍会被放行上云;逐文件 ns+size 让任何单文件变动都失效重建。
_allow_cache: tuple[tuple, frozenset[str]] | None = None
_allow_lock = threading.Lock()


def cloud_text_allowed(text: str) -> bool:
    """红线守卫:白名单加载失败/文本不在集合 → False(fail-closed,云端一个字都不发)。"""
    global _allow_cache
    from . import content
    paths = [content.CONTENT_DIR / "item_bank_v1.json",
             content.CONTENT_DIR / "week1_script.json",
             content.CONTENT_DIR / "autopilot_protocol_v1.json"]
    try:
        with _allow_lock:
            stats = [p.stat() for p in paths]
            key = tuple(x for st in stats for x in (st.st_mtime_ns, st.st_size))
            if _allow_cache is None or _allow_cache[0] != key:
                bank = content.load_item_bank(paths[0])
                wk = json.loads(paths[1].read_text(encoding="utf-8"))
                proto = json.loads(paths[2].read_text(encoding="utf-8"))
                _allow_cache = (key, content.tts_allowlist(bank, wk, proto))
            return text.strip() in _allow_cache[1]
    except Exception:
        return False


# 降级层 piper 单例:不缓存则云故障期每句都重载 onnx 模型(实测每句多 0.4-2.3s)。
# 模型缺失的实例不钉死——之后补装 onnx,下一次降级即可用,无须重启。
_fallback_piper: PiperTtsEngine | None = None
_fallback_lock = threading.Lock()


def _fallback_piper_engine() -> PiperTtsEngine:
    global _fallback_piper
    with _fallback_lock:
        want = Path(os.environ.get("TTS_VOICE_PATH", str(DEFAULT_VOICE)))
        if (_fallback_piper is None or _fallback_piper._voice_path != want
                or not _fallback_piper.available()):
            _fallback_piper = PiperTtsEngine(want)
        return _fallback_piper


def _chain() -> list[TtsProvider]:
    """主引擎 + 降级层:云引擎后面垫一层本地 piper(可用才垫)。"""
    eng = engine()
    chain: list[TtsProvider] = [eng]
    if getattr(eng, "cloud", False):
        piper = _fallback_piper_engine()
        if piper.available():
            chain.append(piper)
    return chain


def speak(text: str) -> tuple[bytes | None, str, bool]:
    """合成一句话。返回 (wav字节|None, 引擎版本, 是否缓存命中)。同句缓存复用。
    任何一层(选引擎/读写缓存/合成)出错一律按降级处理(None→前端回退系统语音),
    不 500——半配置状态不炸接口。"""
    last_version = NullTtsEngine.version
    try:
        chain = _chain()
    except Exception:
        return None, last_version, False
    for eng in chain:
        if isinstance(eng, NullTtsEngine):
            continue
        last_version = eng.version
        if getattr(eng, "cloud", False) and not cloud_text_allowed(text):
            continue                                        # 红线:白名单外的文本不出网
        key = hashlib.sha256(f"{eng.version}\n{eng.cache_params}\n{text}".encode()).hexdigest()
        cached = CACHE_DIR / f"{key}.wav"
        try:
            if cached.exists() and cached.stat().st_size > 44:  # 44=wav 头:0 字节残留不算命中
                return cached.read_bytes(), eng.version, True
        except OSError:
            pass                                            # 缓存读不了按未命中,现场合成
        try:
            data = eng.synthesize(text)
        except Exception:
            data = None
        if data is None or len(data) <= 44:
            continue
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = cached.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")  # 并发写不共 tmp
            tmp.write_bytes(data)
            tmp.replace(cached)
        except OSError:
            pass                                            # 磁盘满/只读:合成已成功,照常返回
        return data, eng.version, False
    return None, last_version, False
