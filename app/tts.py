"""本地神经 TTS(可插拔、可关闭)——小语的声音,零云端。

口径:TTS 输入是题库/脚本的固定话术(未申报专利的研究 IP),和患者音频一样**不出机器**;
云端语音 API 属禁区(IP 外泄 + 医院内网常无外网)。引擎全本地:
- NullTtsEngine:未配置/模型缺失 → 降级,前端自动回退浏览器系统语音(再无则静音)。
- PiperTtsEngine:piper-tts(onnx,CPU 实时),模型文件在 TTS_VOICE_PATH
  (默认 data/tts/zh_CN-huayan-medium.onnx;下载: python -m piper.download_voices
  --data-dir data/tts zh_CN-huayan-medium)。机构 GPU 到位可换更大模型,调用面不变。
话术是闭集 → 按 sha256(engine+text) 落盘缓存 data/tts-cache/,同句只合成一次。
"""
from __future__ import annotations

import hashlib
import io
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

    def synthesize(self, text: str) -> bytes | None:
        return None


class PiperTtsEngine:
    """piper onnx 本地合成。模型懒加载 + 单线程锁(onnx session 非并发安全)。"""

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


def get_engine() -> TtsProvider:
    """按 TTS_ENGINE 选引擎(默认 piper,模型缺失自动降级 null;TTS_ENGINE=null 强制关)。"""
    kind = os.environ.get("TTS_ENGINE", "piper")
    if kind == "piper":
        voice = Path(os.environ.get("TTS_VOICE_PATH", str(DEFAULT_VOICE)))
        eng = PiperTtsEngine(voice)
        if eng.available():
            return eng
    return NullTtsEngine()


_engine: TtsProvider | None = None
_engine_lock = threading.Lock()

# 缓存键里带合成参数指纹:调 length_scale 等参数后旧缓存自然失效,不会全场命中旧语速
_SYN_PARAMS = "length_scale=1.15"


def engine() -> TtsProvider:
    """只缓存可用引擎;Null(未装/缺模型)不焊死——补装模型后下一次请求即生效,无须重启。"""
    global _engine
    with _engine_lock:
        if _engine is None or isinstance(_engine, NullTtsEngine):
            _engine = get_engine()
        return _engine


def speak(text: str) -> tuple[bytes | None, str, bool]:
    """合成一句话。返回 (wav字节|None, 引擎版本, 是否缓存命中)。同句缓存复用。
    引擎抛错一律按降级处理(None→204→前端回退系统语音),不 500——半配置状态不炸接口。"""
    eng = engine()
    if isinstance(eng, NullTtsEngine):
        return None, eng.version, False
    key = hashlib.sha256(f"{eng.version}\n{_SYN_PARAMS}\n{text}".encode()).hexdigest()
    cached = CACHE_DIR / f"{key}.wav"
    if cached.exists() and cached.stat().st_size > 44:      # 44=wav 头:崩溃残留的 0 字节占位不算命中
        return cached.read_bytes(), eng.version, True
    try:
        data = eng.synthesize(text)
    except Exception:
        return None, eng.version, False
    if data is None or len(data) <= 44:
        return None, eng.version, False
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = cached.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")  # 并发写不共 tmp
    tmp.write_bytes(data)
    tmp.replace(cached)
    return data, eng.version, False
