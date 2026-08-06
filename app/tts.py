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

import base64
import binascii
import enum
import hashlib
import importlib.util
import io
import ipaddress
import json
import logging
import os
import socket
import stat
import threading
import wave
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("app.tts")


class _QwenTtsFailureReason(enum.Enum):
    """Closed set of reasons the exact Qwen path can degrade.

    Deliberately an enum and never a free-form string: the whole point is that
    a degradation can be diagnosed without any provider text, patient text,
    credential, request id or audio ever reaching a log line.
    """

    API_KEY_MISSING = "api_key_missing"
    PROVIDER_EXCEPTION = "provider_exception"
    RESPONSE_AFTER_STOP = "response_after_stop"
    RESPONSE_STATUS_INVALID = "response_status_invalid"
    RESPONSE_ERROR_CODE = "response_error_code"
    RESPONSE_OUTPUT_MISSING = "response_output_missing"
    RESPONSE_AUDIO_MISSING = "response_audio_missing"
    FINISH_REASON_INVALID = "finish_reason_invalid"
    STREAM_NOT_FINISHED = "stream_not_finished"
    STREAM_EMPTY = "stream_empty"
    STREAM_CHUNK_INVALID = "stream_chunk_invalid"
    STREAM_SIZE_EXCEEDED = "stream_size_exceeded"
    WAV_HEADER_INVALID = "wav_header_invalid"
    WAV_SENTINEL_INVALID = "wav_sentinel_invalid"
    WAV_FORMAT_INVALID = "wav_format_invalid"
    WAV_PAYLOAD_INVALID = "wav_payload_invalid"
    WAV_VALIDATION_INVALID = "wav_validation_invalid"
    UNEXPECTED_INTERNAL = "unexpected_internal"


class _QwenTtsFailure(ValueError):
    """Carries only the closed reason; its text is exactly the reason value."""

    def __init__(self, reason: _QwenTtsFailureReason):
        super().__init__(reason.value)
        self.reason = reason


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = DATA_DIR / "tts-cache"
DEFAULT_VOICE = DATA_DIR / "tts" / "zh_CN-huayan-medium.onnx"
_MAX_CLOUD_TTS_BYTES = 16 * 1024 * 1024
_DASHSCOPE_DOWNLOAD_SUFFIXES = ("aliyuncs.com",)


def _max_cloud_tts_base64_chars() -> int:
    """Largest canonical base64 envelope that can contain the byte budget."""
    return 4 * ((_MAX_CLOUD_TTS_BYTES + 2) // 3)


def _validated_wav_bytes(data: object) -> bytes:
    """Return bounded RIFF/WAVE bytes or reject the provider/cache payload.

    All cloud result shapes and the persistent cache share this exact boundary;
    otherwise a provider can bypass the URL-download checks by choosing inline
    bytes/base64, and a bad response can become a permanent cache hit.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("TTS 音频不是字节串")
    size = len(data)
    if size <= 44:
        raise ValueError("TTS 音频没有有效载荷")
    if size > _MAX_CLOUD_TTS_BYTES:
        raise ValueError("TTS 音频超过大小上限")
    payload = bytes(data)
    if not payload.startswith(b"RIFF") or payload[8:12] != b"WAVE":
        raise ValueError("TTS 音频容器签名无效")
    return payload


def _validated_inline_wav_base64(value: object) -> bytes:
    """Strictly decode a bounded base64 envelope, then validate decoded WAV."""
    if not isinstance(value, (str, bytes, bytearray)):
        raise ValueError("云 TTS base64 类型无效")
    if len(value) > _max_cloud_tts_base64_chars():
        raise ValueError("云 TTS base64 超过解码前上限")
    if isinstance(value, str):
        try:
            encoded = value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("云 TTS base64 不是 ASCII") from exc
    else:
        encoded = bytes(value)
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("云 TTS base64 非法") from exc
    return _validated_wav_bytes(decoded)


# 官方 qwen3-tts 流式:首块带的是哨兵 size 的 RIFF 头(0x7fffffbf/0x7fffff9b),
# 不是真实长度。收全之后必须按实际长度封口,否则 wave 读到的帧数是天文数字。
# 只认这一种精确布局:PCM/单声道/24000Hz/16bit;别的布局一律拒,不做通用盲修。
_STREAM_PCM_FORMAT = 1
_STREAM_CHANNELS = 1
_STREAM_SAMPLE_RATE = 24000
_STREAM_BITS = 16
_STREAM_BLOCK_ALIGN = 2
_STREAM_BYTE_RATE = _STREAM_SAMPLE_RATE * _STREAM_BLOCK_ALIGN
_STREAM_SENTINEL_RIFF_SIZE = 0x7FFFFFBF
_STREAM_SENTINEL_DATA_SIZE = 0x7FFFFF9B
_STREAM_HEADER_BYTES = 44
# 真机实测:中间块的 finish_reason 不是 Python None,而是字面字符串 "null";
# 只有最后一块才是 "stop"。把 "null" 当成终态会让每一次真实合成都失败。
_STREAM_UNFINISHED = (None, "null")


def _decoded_stream_chunk(value: object, accumulated: int) -> bytes:
    """Strictly decode one streamed base64 chunk within the byte budget.

    Bounded twice on purpose: the pre-decode ceiling stops a huge envelope from
    being decoded at all, and the running total is checked before the buffer is
    allowed to grow, so an over-long stream fails closed instead of after it has
    already been accumulated.
    """
    if not isinstance(value, (str, bytes, bytearray)):
        raise _QwenTtsFailure(_QwenTtsFailureReason.STREAM_CHUNK_INVALID)
    remaining = _MAX_CLOUD_TTS_BYTES - accumulated
    if remaining <= 0:
        raise _QwenTtsFailure(_QwenTtsFailureReason.STREAM_SIZE_EXCEEDED)
    # 上限按**剩余**预算算,不是全局预算:否则每一块单看都合规,却能靠块数把
    # 总量顶穿,而且那些字节已经先被解码出来了。
    if len(value) > 4 * ((remaining + 2) // 3):
        raise _QwenTtsFailure(_QwenTtsFailureReason.STREAM_SIZE_EXCEEDED)
    if isinstance(value, str):
        try:
            encoded = value.encode("ascii")
        except UnicodeEncodeError:
            raise _QwenTtsFailure(
                _QwenTtsFailureReason.STREAM_CHUNK_INVALID) from None
    else:
        encoded = bytes(value)
    try:
        chunk = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise _QwenTtsFailure(
            _QwenTtsFailureReason.STREAM_CHUNK_INVALID) from None
    if not chunk:
        raise _QwenTtsFailure(_QwenTtsFailureReason.STREAM_CHUNK_INVALID)
    if accumulated + len(chunk) > _MAX_CLOUD_TTS_BYTES:
        raise _QwenTtsFailure(_QwenTtsFailureReason.STREAM_SIZE_EXCEEDED)
    return chunk


def _sealed_streamed_wav(raw: bytes) -> bytes:
    """Rewrite the two sentinel sizes with the real ones, or refuse the stream.

    This accepts exactly one layout: the fixed 44-byte header this provider was
    observed to stream — ``RIFF`` at 0, ``WAVE`` at 8, a 16-byte ``fmt `` at 12,
    ``data`` at 36, PCM payload from 44 — carrying both official sentinel sizes.
    A different codec, sample rate or channel count, an unaligned payload, a
    truncated header, or any inserted chunk (``JUNK``/``LIST``) that would shift
    ``data`` off 36 is refused rather than patched into something ``wave`` would
    happily accept.  Only the two size fields are written; no audio byte moves.
    """
    if len(raw) <= _STREAM_HEADER_BYTES:
        raise _QwenTtsFailure(_QwenTtsFailureReason.WAV_PAYLOAD_INVALID)
    # 实测官方头固定 44 字节,没有任何前置 chunk。所以这里要求的是那一种精确布局,
    # 而不是"某处有个 data 块"——放行 JUNK/LIST 之类的插入会让 data 偏移浮动,
    # 那就不是我们观测过、并据此改写 size 的那条流了。
    if (raw[0:4] != b"RIFF" or raw[8:12] != b"WAVE"
            or raw[12:16] != b"fmt " or raw[36:40] != b"data"):
        raise _QwenTtsFailure(_QwenTtsFailureReason.WAV_HEADER_INVALID)
    if int.from_bytes(raw[16:20], "little") != 16:
        raise _QwenTtsFailure(_QwenTtsFailureReason.WAV_FORMAT_INVALID)
    # 只有官方流式那两个哨兵才允许被改写。一个 size 本来就自洽的 WAV 不是这条
    # 路径的产物,盲目重写它等于把任意(可能是伪造的)容器改成"看起来合法"。
    if int.from_bytes(raw[4:8], "little") != _STREAM_SENTINEL_RIFF_SIZE:
        raise _QwenTtsFailure(_QwenTtsFailureReason.WAV_SENTINEL_INVALID)
    if int.from_bytes(raw[40:44], "little") != _STREAM_SENTINEL_DATA_SIZE:
        raise _QwenTtsFailure(_QwenTtsFailureReason.WAV_SENTINEL_INVALID)
    (audio_format, channels, rate, byte_rate, block_align, bits) = (
        int.from_bytes(raw[20:22], "little"),
        int.from_bytes(raw[22:24], "little"),
        int.from_bytes(raw[24:28], "little"),
        int.from_bytes(raw[28:32], "little"),
        int.from_bytes(raw[32:34], "little"),
        int.from_bytes(raw[34:36], "little"),
    )
    if (audio_format != _STREAM_PCM_FORMAT
            or channels != _STREAM_CHANNELS
            or rate != _STREAM_SAMPLE_RATE
            or byte_rate != _STREAM_BYTE_RATE
            or block_align != _STREAM_BLOCK_ALIGN
            or bits != _STREAM_BITS):
        raise _QwenTtsFailure(_QwenTtsFailureReason.WAV_FORMAT_INVALID)
    payload = len(raw) - _STREAM_HEADER_BYTES
    if payload % _STREAM_BLOCK_ALIGN:
        raise _QwenTtsFailure(_QwenTtsFailureReason.WAV_PAYLOAD_INVALID)
    # 只动这两个 size 字段,PCM 一个字节都不碰。
    sealed = bytearray(raw)
    sealed[4:8] = (len(raw) - 8).to_bytes(4, "little")
    sealed[40:44] = payload.to_bytes(4, "little")
    return bytes(sealed)


def _read_validated_wav_cache(path: Path) -> bytes:
    """Read at most the TTS budget from one regular, non-symlink cache file."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("TTS 缓存不是普通文件")
        if metadata.st_size <= 44 or metadata.st_size > _MAX_CLOUD_TTS_BYTES:
            raise ValueError("TTS 缓存大小无效")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(_MAX_CLOUD_TTS_BYTES + 1)
        return _validated_wav_bytes(data)
    finally:
        os.close(descriptor)


def _discard_invalid_cache(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _validated_dashscope_download_url(url: str) -> str:
    """只接受阿里云公开 HTTPS OSS 地址；拒绝内网、特殊 IP、凭据和非 443 端口。"""
    raw = "https://" + url.split("://", 1)[1] if url.startswith("http://") else url
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").rstrip(".").lower()
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password
            or parsed.port not in (None, 443)
            or not any(host == suffix or host.endswith("." + suffix)
                       for suffix in _DASHSCOPE_DOWNLOAD_SUFFIXES)):
        raise ValueError("云 TTS 下载地址不在允许的阿里云 HTTPS 域")
    try:
        addresses = {
            item[4][0].split("%", 1)[0]
            for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise ValueError("云 TTS 下载域名无法解析") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("云 TTS 下载地址解析到非公网 IP")
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


def _open_tts_download(url: str, timeout: int = 15):
    """禁用自动重定向；3xx 必须失败，不能先访问未校验的跳转目标。"""
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
            return None

    opener = urllib.request.build_opener(NoRedirect)
    return opener.open(urllib.request.Request(url, headers={"Accept": "audio/*"}), timeout=timeout)


def _download_dashscope_audio(url: str) -> bytes:
    secure = _validated_dashscope_download_url(url)
    with _open_tts_download(secure, timeout=15) as response:
        # 防御性复核最终 URL；自定义/测试 transport 也不得把重定向结果偷偷喂回来。
        final_url = response.geturl() if hasattr(response, "geturl") else secure
        _validated_dashscope_download_url(final_url)
        headers = getattr(response, "headers", {})
        content_type = (headers.get("Content-Type", "") if headers else "").split(";", 1)[0].lower()
        if content_type and not (content_type.startswith("audio/")
                                 or content_type == "application/octet-stream"):
            raise ValueError("云 TTS 下载响应不是音频")
        declared = headers.get("Content-Length") if headers else None
        if declared:
            try:
                declared_size = int(declared)
                if declared_size < 0:
                    raise ValueError("云 TTS Content-Length 非法")
                if declared_size > _MAX_CLOUD_TTS_BYTES:
                    raise ValueError("云 TTS 音频超过下载上限")
            except ValueError as exc:
                if str(exc) in {"云 TTS Content-Length 非法", "云 TTS 音频超过下载上限"}:
                    raise
                raise ValueError("云 TTS Content-Length 非法") from exc
        data = response.read(_MAX_CLOUD_TTS_BYTES + 1)
        return _validated_wav_bytes(data)


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
        # 光有模型文件不算可用:包没装时 synthesize 恒返回 None,而降级链会把
        # last_version 记成 "piper/…",证据表里就多出一个从未运行的引擎。
        # find_spec 不执行模块体,不会把 onnxruntime 拖进内存。
        return self._voice_path.exists() and importlib.util.find_spec("piper") is not None

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
        try:
            return _validated_wav_bytes(data)
        except ValueError:
            return None

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
        """One official streaming call; the bytes arrive inline, never by URL.

        The non-streaming form only hands back a temporary OSS URL, and the
        SSRF guard on that download is deliberately strict.  Streaming removes
        the download entirely, so the guard stays exactly as it is and there is
        no second request: retrying non-streamed would bill twice and send the
        same patient-facing text to the provider a second time.
        """
        try:
            if not self.available():
                raise _QwenTtsFailure(_QwenTtsFailureReason.API_KEY_MISSING)
            raw = self._stream(text)
            try:
                return _validated_wav_bytes(_sealed_streamed_wav(raw))
            except _QwenTtsFailure:
                raise
            except ValueError:
                raise _QwenTtsFailure(
                    _QwenTtsFailureReason.WAV_VALIDATION_INVALID) from None
        except _QwenTtsFailure as failure:
            self._log_failure(failure.reason)
            return None
        except Exception:
            self._log_failure(_QwenTtsFailureReason.UNEXPECTED_INTERNAL)
            return None

    @staticmethod
    def _log_failure(reason: _QwenTtsFailureReason) -> None:
        """One fixed line per degradation, carrying only the closed reason.

        Never the synthesized text, the credential, a request id, the provider
        message or response, a URL, base64 or audio bytes — and no traceback,
        because an exception's own text can quote any of those.  A logging
        backend that is itself broken must not change the outcome.
        """
        try:
            logger.warning("qwen_tts_failed reason=%s", reason.value)
        except Exception:
            pass

    def _stream(self, text: str) -> bytes:
        buffer = bytearray()
        finished = False
        try:
            responses = iter(self._call(text))
        except _QwenTtsFailure:
            raise
        except Exception:
            raise _QwenTtsFailure(
                _QwenTtsFailureReason.PROVIDER_EXCEPTION) from None
        for response in self._iterate(responses):
            # 终态之后这条流就该结束了。多出来的任何一条(哪怕只是第二个空 stop)
            # 都说明我们对这条流的理解和服务端不一致,不能挑着收下。
            if finished:
                raise _QwenTtsFailure(
                    _QwenTtsFailureReason.RESPONSE_AFTER_STOP)
            status = getattr(response, "status_code", None)
            # SDK 的 streaming transport 给的是 http.HTTPStatus.OK(IntEnum),
            # 所以按 int 子类收;但 bool 也是 int 子类,而 "200"/200.0 不是状态码。
            if (not isinstance(status, int) or isinstance(status, bool)
                    or status != 200):
                raise _QwenTtsFailure(
                    _QwenTtsFailureReason.RESPONSE_STATUS_INVALID)
            if getattr(response, "code", None):
                raise _QwenTtsFailure(_QwenTtsFailureReason.RESPONSE_ERROR_CODE)
            out = getattr(response, "output", None)
            if out is None:
                raise _QwenTtsFailure(
                    _QwenTtsFailureReason.RESPONSE_OUTPUT_MISSING)
            audio = getattr(out, "audio", None)
            chunk = audio.get("data") if isinstance(audio, dict) else None
            reason = getattr(out, "finish_reason", None)
            if reason not in _STREAM_UNFINISHED:
                if reason != "stop":
                    raise _QwenTtsFailure(
                        _QwenTtsFailureReason.FINISH_REASON_INVALID)
                finished = True
            if chunk:
                buffer += _decoded_stream_chunk(chunk, len(buffer))
            elif not finished:
                # 中间块既没有音频也没有终态:畸形,不当成"这块没数据"跳过。
                raise _QwenTtsFailure(
                    _QwenTtsFailureReason.RESPONSE_AUDIO_MISSING)
        # URL 即使随终态一起回来也一律忽略:流式已经给过字节了。
        if not finished:
            raise _QwenTtsFailure(_QwenTtsFailureReason.STREAM_NOT_FINISHED)
        if not buffer:
            raise _QwenTtsFailure(_QwenTtsFailureReason.STREAM_EMPTY)
        return bytes(buffer)

    @staticmethod
    def _iterate(responses):
        """Yield provider responses, mapping transport faults to one reason.

        A generator that raises mid-stream is a provider fault like any other;
        the original exception is deliberately dropped rather than chained,
        because its text can quote the request, the credential or the response.
        """
        while True:
            try:
                yield next(responses)
            except StopIteration:
                return
            except _QwenTtsFailure:
                raise
            except Exception:
                raise _QwenTtsFailure(
                    _QwenTtsFailureReason.PROVIDER_EXCEPTION) from None

    def _call(self, text: str):
        from dashscope import MultiModalConversation
        return MultiModalConversation.call(model=self._model, text=text, voice=self._voice,
                                           language_type="Chinese", stream=True,
                                           speech_rate=self._rate,
                                           request_timeout=15)


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


def _synthesize_with(chain: list[TtsProvider], text: str) -> tuple[bytes | None, str, bool]:
    """跑一条给定的引擎链:白名单、缓存读写、WAV 校验全部共用一份实现。

    链由调用方决定,这里不追加任何降级层——通用链带本地 piper,自动驾驶链只有
    Qwen。返回值语义与 speak 一致。"""
    last_version = NullTtsEngine.version
    for eng in chain:
        if isinstance(eng, NullTtsEngine):
            continue
        last_version = eng.version
        if getattr(eng, "cloud", False) and not cloud_text_allowed(text):
            continue                                        # 红线:白名单外的文本不出网
        key = hashlib.sha256(f"{eng.version}\n{eng.cache_params}\n{text}".encode()).hexdigest()
        cached = CACHE_DIR / f"{key}.wav"
        try:
            return _read_validated_wav_cache(cached), eng.version, True
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            # 损坏、越界或软链接缓存不能成为永久命中；只移除这个摘要路径。
            _discard_invalid_cache(cached)
        try:
            data = eng.synthesize(text)
            if data is not None:
                data = _validated_wav_bytes(data)
        except Exception:
            data = None
        if data is None:
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


def speak(text: str) -> tuple[bytes | None, str, bool]:
    """合成一句话。返回 (wav字节|None, 引擎版本, 是否缓存命中)。同句缓存复用。
    任何一层(选引擎/读写缓存/合成)出错一律按降级处理(None→前端回退系统语音),
    不 500——半配置状态不炸接口。"""
    try:
        chain = _chain()
    except Exception:
        return None, NullTtsEngine.version, False
    return _synthesize_with(chain, text)


def get_autopilot_engine() -> TtsProvider:
    """server-owned exact 通道的唯一引擎解析口:永远只是 Qwen。

    显式函数而不是内联构造,是因为 synthetic harness 必须有一个可替换的注入点;
    生产默认这里绝不返回 piper/null/browser——老人独自面对屏幕时,一句忽然变成
    另一个本地嗓音的提示语,对他来说和真的没有区别。缺 Key 时照样返回 Qwen 引擎
    对象,好让降级证据诚实地记下"尝试过谁",而不是记一个从未运行的降级层。"""
    return _cloud_engine("qwen-tts")


def speak_autopilot(text: str) -> tuple[bytes | None, str, bool]:
    """exact 自动驾驶话术:成功即 Qwen,失败即明确降级,中间没有第三种结果。"""
    try:
        chain = [get_autopilot_engine()]
    except Exception:
        return None, NullTtsEngine.version, False
    return _synthesize_with(chain, text)
