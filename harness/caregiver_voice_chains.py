#!/usr/bin/env python3
"""照护员本机演练:尾静默自动收麦(VAD)的真实 Chrome 走查。

别的走查都喂循环连续音,「正在听您说」一出现就点「说完了可以点这里」,收麦理由钉死
user_done——平板 2026-09-20 起的尾静默自动收麦(stop_reason=silence)从没在真浏览器里
被证明会触发。这里两条链都不点按钮:

  链 S  说完就停:伪麦克风先「说」约 1.6 s 再静 7 s(Chrome 循环播放)。第 1 题 AI 带练,
        record_stopped 的 stop_reason 必须是 silence,并在 record_started 之后「有声段
        + 3 s 尾静默 + 2 s 余量」之内到达;录音照常登记、上传、回执,确定性 ASR 判对后
        场次照常走到反馈与第 2 题。
  链 C  一直在说:第 1 段录完立刻换成不停顿的「说话」,第 2 题同样不点按钮,只能撞
        作答窗口(max_duration)收麦——没有尾静默,VAD 绝不能判「说完」。

「一直在说」为什么不用旧的连续正弦:Chrome 的 WebRTC 降噪不到 1 s 就把稳态正弦当噪声
压掉约 18 dB——2026-10-02 四次实测,VAD 自己那只 analyser 在 14 s 窗口里有 13 s 读到
RMS<0.05。它照样撞 max_duration,靠的是 VAD「开口后累计有声 ≥450 ms」那道闸(AGC 起步
放大的 0.4–0.5 s 恰好卡在闸口上),不是「没有尾静默」,当不了对照。所以「说话」做成
95–140 Hz 滑音的音节串(每节 220 ms、节间 60 ms 静):降噪不把它当噪声;又低于提前回答
检测的 150–3800 Hz 语音带,问句里不会触发声音停播。

老人页挂一段只旁听的普查脚本(getUserMedia、track.stop、AudioContext、VAD 读的
analyser 帧、设备 ACK 发出那一拍),断言:任何时刻最多一条活麦克风轨道;每个命令边界
(除 record_started 外的 ACK 发出时)和每次录音取流前,活轨道与未关的 AudioContext
都是 0;每段录音恰好(VAD 开时)挂一个旁听 AudioContext,而且它真的「听到」了该听到的。

VAD 开关:平板若读了服务端 GET …/autopilot/voice-settings,就按回执定期望(VAD 关 →
链 S 也只能撞 max_duration);没读(老平板,VAD 恒开)就按平板常量。跑法见
scripts/run-caregiver-demo20.sh --browser-check voice-chains。
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import random
import re
import stat
import sys
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlsplit

# 启动器用 python -I 跑本文件,同目录不在 sys.path;只把仓库根加进去,复用同目录的沙箱工具。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.caregiver_browser_acceptance import (  # noqa: E402
    HARNESS_MARKER_HEADER,
    HARNESS_MARKER_VALUE,
    INSTANCE_MARKER_HEADER,
    BrowserAcceptanceConfig,
    BrowserAcceptanceError,
    _redacted_error,
    _same_acceptance_origin,
    _stable_error_code,
    _wait_for,
    resolve_browser_config,
)
from harness.caregiver_recovery_chains import ADMIN_PASSWORD_ENV, PATIENT_ID  # noqa: E402

RESULT_RECEIPT_NAME = "browser-voice-chains-result.json"
RECEIPT_SCHEMA = "caregiver-browser-voice-chains.v1"
FAKE_MIC_NAME = "browser-fake-microphone-voice.wav"

FAKE_MIC_RATE = 16_000
SYLLABLE_SECONDS = 0.22
SYLLABLE_GAP_SECONDS = 0.06
SYLLABLE_PEAK = 0.6
SYLLABLE_F0_HZ = (95.0, 140.0)
VOICED_SYLLABLES = 6
# 有声段到最后一节收尾为止;之后(含最后那段节间静)都是静音。
VOICED_SECONDS = (VOICED_SYLLABLES * (SYLLABLE_SECONDS + SYLLABLE_GAP_SECONDS)
                  - SYLLABLE_GAP_SECONDS)
HUSH_SECONDS = 7.0
HUSH_RMS = 0.001
CONTINUOUS_SYLLABLES = 22
# 与 web/src/patient/autopilotBrowserBargeIn.ts 的语音带下沿同值。
BARGE_IN_VOICE_BAND_LOW_HZ = 150

# 与 web/src/patient/autopilotVoiceActivity.ts 同值:平板没读服务端语音设置时用的就是它们。
TABLET_TRAILING_SILENCE_MS = 3_000
TABLET_SHORT_TRAILING_SILENCE_MS = 4_500
TABLET_FULL_UTTERANCE_MS = 900
TABLET_MIN_VOICED_MS = 450
SLACK_MS = 2_000
# 「一直在说」的链里旁听者至少要听到这么多有声:降噪若把说话压成静默,VAD 同样不会判停,
# 那是空转的通过。
CONTINUOUS_MIN_LOUD_MS = 5_000
# 普查脚本按帧粗分有声/静默的阈值(时域 RMS,满刻度 1.0)。
LISTENER_LOUD_RMS = 0.05

VOICE_SWITCH_ENVS = (
    "NMU_AUTOPILOT_VAD",
    "NMU_AUTOPILOT_VAD_TRAILING_MS",
    "NMU_AUTOPILOT_VAD_SHORT_TRAILING_MS",
    "NMU_AUTOPILOT_VOICE_BARGE_IN",
)
VOICE_SETTINGS_KEYS = frozenset({
    "vad_enabled", "vad_trailing_silence_ms", "vad_short_trailing_silence_ms", "barge_in_enabled",
})
_FALSY_SWITCH = frozenset({"0", "false", "off", "no"})
_ABSENT = object()
# 这些 ACK 发出那一拍,本条命令已收口、下一条还没开麦:麦克风与 AudioContext 都必须是 0。
_IDLE_BOUNDARY_ACKS = frozenset({
    "tts_started", "tts_ended", "tts_interrupted", "tts_failed", "record_stopped", "record_failed",
})
_VOICE_SETTINGS_PATH = re.compile(r"^/sessions/[^/]+/autopilot/voice-settings$")


@dataclass(frozen=True)
class VoiceChainsResult:
    session_id: str
    plan_id: str
    settings_source: str
    vad_expected: str
    trailing_ms: str
    silence_record_key: str
    silence_raw_audio_id: str
    silence_stop_reason: str
    silence_elapsed_ms: str
    silence_duration_seconds: str
    silence_vad_armed: str
    silence_vad_voiced_ms: str
    silence_listener_loud_ms: str
    continuous_record_key: str
    continuous_raw_audio_id: str
    continuous_stop_reason: str
    continuous_elapsed_ms: str
    continuous_duration_seconds: str
    continuous_vad_armed: str
    continuous_vad_voiced_ms: str
    continuous_listener_loud_ms: str
    max_live_tracks: str


# ----------------------------------------------------------------------------
# 伪麦克风
# ----------------------------------------------------------------------------
def _syllable_train(count: int, *, seed: int) -> list[float]:
    rng = random.Random(seed)
    on = round(SYLLABLE_SECONDS * FAKE_MIC_RATE)
    gap = round(SYLLABLE_GAP_SECONDS * FAKE_MIC_RATE)
    samples: list[float] = []
    phase = 0.0
    for _ in range(count):
        start_hz, end_hz = rng.uniform(*SYLLABLE_F0_HZ), rng.uniform(*SYLLABLE_F0_HZ)
        peak = SYLLABLE_PEAK * rng.uniform(0.75, 1.0)
        for index in range(on):
            position = index / on
            phase += 2 * math.pi * (start_hz + (end_hz - start_hz) * position) / FAKE_MIC_RATE
            edge = min(1.0, position / 0.25, (1 - position) / 0.25)
            samples.append(peak * (0.5 - 0.5 * math.cos(math.pi * edge)) * math.sin(phase))
        samples.extend([0.0] * gap)
    return samples


def voice_then_hush_samples() -> list[float]:
    speech = _syllable_train(VOICED_SYLLABLES, seed=20260921)
    voiced = round(VOICED_SECONDS * FAKE_MIC_RATE)
    rng = random.Random(20260922)
    width = HUSH_RMS * math.sqrt(3)
    hush = [rng.uniform(-width, width) for _ in range(round(HUSH_SECONDS * FAKE_MIC_RATE))]
    return speech[:voiced] + hush


def continuous_voice_samples() -> list[float]:
    # 以节间静收尾、以音节开头:Chrome 循环播放时接缝处也只有 60 ms 的停顿。
    return _syllable_train(CONTINUOUS_SYLLABLES, seed=20260923)


def _wav_bytes(samples: Sequence[float]) -> bytes:
    frames = bytearray()
    for value in samples:
        frames += max(-32768, min(32767, round(value * 32767))).to_bytes(2, "little", signed=True)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(FAKE_MIC_RATE)
        output.writeframes(bytes(frames))
    return buffer.getvalue()


def write_fake_microphone(path: Path, samples: Sequence[float], *, replace: bool = False) -> None:
    data = _wav_bytes(samples)
    if not replace:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            raise BrowserAcceptanceError("伪麦克风音频文件不是本次新建") from exc
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        path.chmod(0o600)
        return
    # 换片必须原子:Chrome 每开一条新采集流就重读一次这个文件,读到半个文件就是坏音频。
    fd, temp = tempfile.mkstemp(prefix=".fake-mic-", suffix=".wav", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


# ----------------------------------------------------------------------------
# 语音开关与期望(纯函数)
# ----------------------------------------------------------------------------
def configured_voice_switches(env: Mapping[str, str]) -> dict[str, bool]:
    """按共享接口读本次启动的语音开关;值与服务端同一套语义。"""
    def enabled(name: str) -> bool:
        return (env.get(name) or "").strip().lower() not in _FALSY_SWITCH

    return {
        "vad_enabled": enabled("NMU_AUTOPILOT_VAD"),
        "barge_in_enabled": enabled("NMU_AUTOPILOT_VOICE_BARGE_IN"),
        "windows_overridden": any((env.get(name) or "").strip() for name in (
            "NMU_AUTOPILOT_VAD_TRAILING_MS", "NMU_AUTOPILOT_VAD_SHORT_TRAILING_MS")),
    }


def voice_settings_shape_ok(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != VOICE_SETTINGS_KEYS:
        return False
    if any(type(value[name]) is not bool for name in ("vad_enabled", "barge_in_enabled")):
        return False
    trailing = value["vad_trailing_silence_ms"]
    short = value["vad_short_trailing_silence_ms"]
    return (type(trailing) is int and type(short) is int
            and 1_500 <= trailing <= 8_000 and 1_500 <= short <= 10_000)


def resolve_voice_expectation(observed: Sequence[Mapping], *, requested: int,
                              switches: Mapping[str, bool]) -> dict:
    """第 1 段录音开麦时平板该按哪套语音设置走。

    observed 是开麦前收到的、形状合法的服务端语音设置回执(按到达顺序);requested 是
    开麦前平板发出的读取请求数。读了却没拿到,按约定该次录音 VAD 关——那样链 S 证明
    不了任何东西,直接判失败。
    """
    if not observed:
        if requested:
            raise BrowserAcceptanceError("平板开麦前没拿到服务端语音设置(按约定这段录音 VAD 关),链 S 无从证明")
        if not (switches["vad_enabled"] and switches["barge_in_enabled"]) or switches["windows_overridden"]:
            raise BrowserAcceptanceError(
                "设置了 NMU_AUTOPILOT_* 语音开关,但平板没有读服务端语音设置"
                "(GET …/autopilot/voice-settings),开关无法生效")
        expectation = {
            "source": "tablet_default", "vad_enabled": True, "barge_in_enabled": True,
            "trailing_ms": TABLET_TRAILING_SILENCE_MS,
            "short_trailing_ms": TABLET_SHORT_TRAILING_SILENCE_MS,
        }
    else:
        latest = observed[-1]
        if (latest["vad_enabled"] is not switches["vad_enabled"]
                or latest["barge_in_enabled"] is not switches["barge_in_enabled"]):
            raise BrowserAcceptanceError("服务端语音设置回执与本次启动的 NMU_AUTOPILOT_* 开关不一致")
        expectation = {
            "source": "server", "vad_enabled": latest["vad_enabled"],
            "barge_in_enabled": latest["barge_in_enabled"],
            "trailing_ms": latest["vad_trailing_silence_ms"],
            "short_trailing_ms": latest["vad_short_trailing_silence_ms"],
        }
    if expectation["vad_enabled"] and expectation["trailing_ms"] > HUSH_SECONDS * 1000 - 1_000:
        raise BrowserAcceptanceError(
            f"尾静默窗口 {expectation['trailing_ms']} ms 盖不住伪麦克风 {HUSH_SECONDS:g} s 的静音段")
    return expectation


def silence_stop_budget_ms(expectation: Mapping, vad_voiced_ms: int | None) -> int:
    """record_started 之后,链 S 的 record_stopped 最晚该在多少毫秒内到达。"""
    window = expectation["trailing_ms"]
    if vad_voiced_ms is not None and vad_voiced_ms < TABLET_FULL_UTTERANCE_MS:
        window = expectation["short_trailing_ms"]
    return round(VOICED_SECONDS * 1000) + window + SLACK_MS


def expected_http_failure(method: str, path: str, status: int, code: str | None, *,
                          logged_in: bool, autopilot_started: bool, pause_requested: bool,
                          barge_in_enabled: bool) -> bool:
    if method == "GET" and path == "/auth/me":
        return not logged_in and status == 401 and code == "auth_me_not_logged_in"
    if method == "GET" and path.endswith("/autopilot/next"):
        if pause_requested:
            return status == 409 and code == "autopilot_runtime_inactive"
        return not autopilot_started and status == 409 and code == "autopilot_not_active"
    if method == "POST" and path.endswith("/barge-in-authorization") and status == 409:
        if pause_requested and code == "autopilot_runtime_inactive":
            # 暂停落在问句/线索播放期间:声音停播检测开麦后那第二次授权撞上已暂停的场次。
            return True
        # 关了声音停播开关时服务端照约定 409,平板走「检测不可用」那条老路。
        return not barge_in_enabled and code == "autopilot_barge_in_disabled"
    return False


def stop_fact_violations(body: Mapping, *, vad_expected: bool) -> list[str]:
    """record_stopped 里两个可选事实:老平板一个都不带;带了就必须自洽、与期望一致。"""
    armed = body.get("vad_armed", _ABSENT)
    voiced = body.get("vad_voiced_ms", _ABSENT)
    if armed is _ABSENT:
        return [] if voiced is _ABSENT else ["record_stopped 带了 vad_voiced_ms 却没带 vad_armed"]
    if type(armed) is not bool:
        return ["record_stopped 的 vad_armed 不是布尔值"]
    problems = []
    if armed is not vad_expected:
        problems.append(f"record_stopped 说 vad_armed={armed},与期望的 VAD {'开' if vad_expected else '关'}不符")
    if armed:
        if type(voiced) is not int or not 0 <= voiced <= 600_000:
            problems.append("VAD 开着,record_stopped 的 vad_voiced_ms 却不是 0..600000 的整数")
    elif voiced is not _ABSENT:
        problems.append("VAD 没开,record_stopped 却带了 vad_voiced_ms")
    return problems


def census_violations(census: Mapping, *, expect_vad: Sequence[bool]) -> list[str]:
    """老人页资源普查:最多一条活轨道;命令边界与开麦前全清零;每段录音的旁听者数目对。"""
    journal = census.get("journal")
    if not isinstance(journal, list):
        return ["老人页资源普查没有日志"]
    problems: list[str] = []
    if int(census.get("maxLiveTracks") or 0) > 1:
        problems.append(f"同一时刻有 {census['maxLiveTracks']} 条活麦克风轨道(最多 1 条)")
    record_streams: list[str] = []
    taps: dict[str, int] = {}
    for entry in journal:
        event = entry.get("event")
        # 「关了」按 AudioContext 真正进入 closed 算,不是调过 close() 就算。
        held = (entry.get("liveTracks"), entry.get("unclosedContexts"))
        if event == "ack_sent":
            ack = entry.get("ackType")
            if ack in _IDLE_BOUNDARY_ACKS and held != (0, 0):
                problems.append(f"{ack} 发出时仍有 {held[0]} 条活轨道、{held[1]} 个没关上的 AudioContext")
            elif ack == "record_started" and held[0] != 1:
                problems.append(f"record_started 发出时活轨道是 {held[0]} 条,不是录音器那 1 条")
        elif event == "gum_called" and entry.get("kind") == "record" and held != (0, 0):
            problems.append(f"录音器取流前仍有 {held[0]} 条活轨道、{held[1]} 个没关上的 AudioContext")
        elif event == "gum_resolved" and entry.get("kind") == "record":
            record_streams.append(str(entry.get("stream")))
        elif event == "context_tapped":
            stream = str(entry.get("stream"))
            taps[stream] = taps.get(stream, 0) + 1
    if len(record_streams) != len(expect_vad):
        problems.append(f"录音器取流 {len(record_streams)} 次,与录音命令 {len(expect_vad)} 条对不上")
    for index, (stream, vad) in enumerate(zip(record_streams, expect_vad), 1):
        if taps.get(stream, 0) != int(vad):
            problems.append(f"第 {index} 段录音挂了 {taps.get(stream, 0)} 个旁听 AudioContext(期望 {int(vad)})")
    final = tuple(census.get(name) for name in ("liveTracks", "openContexts", "unclosedContexts"))
    if final != (0, 0, 0):
        problems.append(f"收尾后仍有活轨道/未关/未落定的 AudioContext:{final}")
    return problems


def listener_violations(listener: Mapping | None, *, chain: str, expectation: Mapping) -> list[str]:
    """VAD 自己那只 analyser 读到的帧:链 S 先听到一句话再静满窗口;链 C 一直听到说话。"""
    if not expectation["vad_enabled"]:
        return [] if listener is None else [f"VAD 关着,链 {chain} 的录音却挂了旁听者"]
    if listener is None:
        return [f"VAD 开着,链 {chain} 的录音却没有旁听者读帧"]
    loud = int(listener.get("loudMs") or 0)
    if chain == "silence":
        problems = []
        if loud < TABLET_FULL_UTTERANCE_MS:
            problems.append(f"链 S 旁听者只听到 {loud} ms 有声,不够一句话({TABLET_FULL_UTTERANCE_MS} ms)")
        final_quiet = int(listener.get("finalQuietMs") or 0)
        if final_quiet < expectation["trailing_ms"] - 200:
            problems.append(f"链 S 收麦前只静了 {final_quiet} ms,不足尾静默窗口 {expectation['trailing_ms']} ms")
        return problems
    problems = []
    longest = int(listener.get("longestQuietMs") or 0)
    if longest >= expectation["trailing_ms"]:
        problems.append(f"链 C 的「一直在说」里出现了 {longest} ms 静默,不再是没有尾静默的对照")
    if loud < CONTINUOUS_MIN_LOUD_MS:
        problems.append(f"链 C 旁听者只听到 {loud} ms 有声:说话被压成了静默,max_duration 证明不了什么")
    return problems


# ----------------------------------------------------------------------------
# 结果收据
# ----------------------------------------------------------------------------
def _write_receipt(config: BrowserAcceptanceConfig, result: VoiceChainsResult) -> None:
    target = config.harness_root / RESULT_RECEIPT_NAME
    if target.exists() or target.is_symlink():
        raise BrowserAcceptanceError("语音链走查结果收据已存在,拒绝覆盖")
    payload = json.dumps({"schema": RECEIPT_SCHEMA, **result.__dict__},
                         sort_keys=True, separators=(",", ":"))
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)


def _read_receipt(root: Path) -> VoiceChainsResult:
    target = root / RESULT_RECEIPT_NAME
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except OSError as exc:
        raise BrowserAcceptanceError("缺少本次语音链走查结果收据") from exc
    try:
        facts = os.fstat(fd)
        if (not stat.S_ISREG(facts.st_mode) or stat.S_IMODE(facts.st_mode) != 0o600
                or facts.st_uid != os.getuid() or facts.st_nlink != 1
                or facts.st_size <= 0 or facts.st_size > 8_192):
            raise BrowserAcceptanceError("语音链走查结果收据不是本用户的私密普通文件")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            value = json.load(handle)
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(value, dict) or value.get("schema") != RECEIPT_SCHEMA:
        raise BrowserAcceptanceError("语音链走查结果收据格式无效")
    fields = {name: value.get(name) for name in VoiceChainsResult.__dataclass_fields__}
    if any(not isinstance(item, str) or not item for item in fields.values()):
        raise BrowserAcceptanceError("语音链走查结果收据缺少字段")
    return VoiceChainsResult(**fields)


# ----------------------------------------------------------------------------
# 真实 Chrome
# ----------------------------------------------------------------------------
_MEDIA_CENSUS_SCRIPT = r"""
(() => {
  const journal = [];
  const liveTracks = new Map();
  const contexts = new Map();
  const streamKinds = new Map();
  let nextTrackId = 1;
  let nextContextId = 1;
  let maxLiveTracks = 0;
  let maxOpenContexts = 0;
  const openContexts = () => {
    let count = 0;
    for (const entry of contexts.values()) if (!entry.closeCalled && !entry.closed) count += 1;
    return count;
  };
  const unclosedContexts = () => {
    let count = 0;
    for (const entry of contexts.values()) if (!entry.closed) count += 1;
    return count;
  };
  const note = (event, extra = {}) => {
    journal.push({ t: performance.now(), event, liveTracks: liveTracks.size,
      openContexts: openContexts(), unclosedContexts: unclosedContexts(), ...extra });
  };
  const ended = (track, how) => {
    const entry = liveTracks.get(track);
    if (entry === undefined) return;
    liveTracks.delete(track);
    note("track_ended", { track: entry.id, kind: entry.kind, how });
  };
  const nativeStop = MediaStreamTrack.prototype.stop;
  MediaStreamTrack.prototype.stop = function (...args) {
    const result = nativeStop.apply(this, args);
    ended(this, "stop");
    return result;
  };
  const nativeGetUserMedia = MediaDevices.prototype.getUserMedia;
  MediaDevices.prototype.getUserMedia = function (constraints) {
    // 录音器取流是 {audio: true};提前回答检测带回声消除等约束。按约束分辨两家。
    const kind = constraints && constraints.audio === true ? "record" : "monitor";
    note("gum_called", { kind });
    return nativeGetUserMedia.call(this, constraints).then((stream) => {
      streamKinds.set(stream.id, kind);
      for (const track of stream.getTracks()) {
        if (liveTracks.has(track) || track.readyState === "ended") continue;
        liveTracks.set(track, { id: nextTrackId++, kind });
        track.addEventListener("ended", () => ended(track, "ended"));
      }
      maxLiveTracks = Math.max(maxLiveTracks, liveTracks.size);
      note("gum_resolved", { kind, stream: stream.id });
      return stream;
    }, (error) => {
      note("gum_rejected", { kind });
      throw error;
    });
  };
  const NativeAudioContext = window.AudioContext;
  const nativeClose = NativeAudioContext.prototype.close;
  const nativeSource = NativeAudioContext.prototype.createMediaStreamSource;
  const closed = (context) => {
    const entry = contexts.get(context);
    if (entry === undefined || entry.closed) return;
    entry.closed = true;
    note("context_closed", { context: entry.id });
  };
  NativeAudioContext.prototype.close = function (...args) {
    const entry = contexts.get(this);
    if (entry !== undefined && !entry.closeCalled) {
      entry.closeCalled = true;
      note("context_close_called", { context: entry.id });
    }
    const result = nativeClose.apply(this, args);
    Promise.resolve(result).then(() => closed(this), () => {});
    return result;
  };
  NativeAudioContext.prototype.createMediaStreamSource = function (stream) {
    const entry = contexts.get(this);
    if (entry !== undefined && stream) {
      entry.stream = stream.id;
      entry.kind = streamKinds.get(stream.id) || "unknown";
      note("context_tapped", { context: entry.id, stream: stream.id, kind: entry.kind });
    }
    return nativeSource.call(this, stream);
  };
  window.AudioContext = class extends NativeAudioContext {
    constructor(...args) {
      super(...args);
      const entry = { id: nextContextId++, closeCalled: false, closed: false, stream: null,
        kind: null, frames: 0, loudMs: 0, quietRunMs: 0, longestQuietMs: 0, lastFrameAt: null };
      contexts.set(this, entry);
      this.addEventListener("statechange", () => { if (this.state === "closed") closed(this); });
      maxOpenContexts = Math.max(maxOpenContexts, openContexts());
      note("context_created", { context: entry.id });
    }
  };
  // 只统计录音流上那只 analyser(平板 VAD 自己读的帧),不改它读到的任何一个数。
  const nativeTimeDomain = AnalyserNode.prototype.getFloatTimeDomainData;
  AnalyserNode.prototype.getFloatTimeDomainData = function (array) {
    const result = nativeTimeDomain.call(this, array);
    try {
      const entry = contexts.get(this.context);
      if (entry !== undefined && entry.kind === "record") {
        let sum = 0;
        for (let index = 0; index < array.length; index += 1) sum += array[index] * array[index];
        const rms = array.length ? Math.sqrt(sum / array.length) : 0;
        const at = performance.now();
        const delta = entry.lastFrameAt === null ? 0 : Math.min(at - entry.lastFrameAt, 500);
        entry.lastFrameAt = at;
        entry.frames += 1;
        if (rms >= __LOUD_RMS__) {
          entry.loudMs += Math.min(delta, 130);
          entry.quietRunMs = 0;
        } else {
          entry.quietRunMs += delta;
          entry.longestQuietMs = Math.max(entry.longestQuietMs, entry.quietRunMs);
        }
      }
    } catch { /* 只旁听:统计坏了也不能影响平板读帧 */ }
    return result;
  };
  const nativeFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input && input.url;
      const matched = /^\/sessions\/[^/]+\/autopilot\/commands\/([^/]+)\/acks$/
        .exec(new URL(raw, location.href).pathname);
      if (matched && init && typeof init.body === "string") {
        const body = JSON.parse(init.body);
        if (body && typeof body.ack_type === "string") {
          note("ack_sent", { command: decodeURIComponent(matched[1]), ackType: body.ack_type });
        }
      }
    } catch { /* 只旁听:认不出的请求照常发出 */ }
    return nativeFetch.apply(this, arguments);
  };
  Object.defineProperty(window, "__nmuVoiceChainsCensus", {
    value: () => ({
      journal: journal.map((entry) => ({ ...entry })),
      maxLiveTracks, maxOpenContexts,
      liveTracks: liveTracks.size, openContexts: openContexts(), unclosedContexts: unclosedContexts(),
      listeners: [...contexts.values()].filter((entry) => entry.kind === "record").map((entry) => ({
        stream: entry.stream, kind: entry.kind, frames: entry.frames, loudMs: Math.round(entry.loudMs),
        longestQuietMs: Math.round(entry.longestQuietMs), finalQuietMs: Math.round(entry.quietRunMs),
      })),
    }),
    configurable: false, enumerable: false, writable: false,
  });
})();
""".replace("__LOUD_RMS__", repr(LISTENER_LOUD_RMS))


def _fact_text(body: Mapping, name: str) -> str:
    value = body.get(name, _ABSENT)
    if value is _ABSENT:
        return "absent"
    return json.dumps(value)


def run_voice_chains(config: BrowserAcceptanceConfig) -> VoiceChainsResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserAcceptanceError("本机没有可用的 Python Playwright;已停止,不会自动安装") from exc

    switches = configured_voice_switches(os.environ)
    fake_audio = config.harness_root / "tmp" / FAKE_MIC_NAME
    fake_audio.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_fake_microphone(fake_audio, voice_then_hush_samples())
    violations: list[str] = []
    obs: dict[str, object] = {
        "logged_in": {"caregiver": False},
        "session_id": None,
        "plan_id": None,
        "attached_session_ids": [],
        "autopilot_started": False,
        "pause_requested": False,
        "teardown_started": False,
        "commands": {},
        "command_order": [],
        "ack_types": {},
        "ack_at": {},
        "stop_bodies": {},
        "audio_posts": {},
        "audio_uploads": [],
        "audio_saved": [],
        "voice_settings": [],
        "voice_settings_requests": 0,
    }

    def violation(message: str) -> None:
        if len(violations) < 20:
            violations.append(message)

    def route_request(route) -> None:
        if _same_acceptance_origin(route.request.url, config.origin):
            route.continue_()
        else:
            violation("已拦截非本次本机地址的网络请求")
            route.abort("blockedbyclient")

    def posted_json(request) -> dict | None:
        try:
            posted = request.post_data_json
        except Exception:
            return None
        return posted if isinstance(posted, dict) else None

    def expected_failure(tag: str, request, path: str, status: int, response) -> bool:
        logged_in = obs["logged_in"]
        assert isinstance(logged_in, dict)
        return expected_http_failure(
            request.method, path, status, _stable_error_code(response) if status >= 400 else None,
            logged_in=bool(logged_in.get(tag)), autopilot_started=bool(obs["autopilot_started"]),
            pause_requested=bool(obs["pause_requested"]),
            barge_in_enabled=switches["barge_in_enabled"])

    def register_command(payload) -> None:
        # 平板拿命令有两条路:GET /autopilot/next,以及 tts_ended ACK 回执体里直接带下来的
        # 录音命令。两处形状同为 NextCommandProjection,统一登记一次。
        if not isinstance(payload, dict) or not isinstance(payload.get("command_key"), str):
            return
        commands = obs["commands"]
        order = obs["command_order"]
        assert isinstance(commands, dict) and isinstance(order, list)
        key = payload["command_key"]
        if key in commands:
            return
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        commands[key] = {
            "command_key": key,
            "command_seq": payload.get("command_seq"),
            "kind": payload.get("kind"),
            "item_ref": payload.get("item_ref"),
            "attempt_seq": payload.get("attempt_seq"),
            "purpose": inner.get("purpose"),
            "raw_audio_id": inner.get("raw_audio_id"),
            "max_duration_seconds": inner.get("max_duration_seconds"),
        }
        order.append(key)

    def observe_request(request) -> None:
        if request.method == "GET" and _VOICE_SETTINGS_PATH.match(urlsplit(request.url).path):
            obs["voice_settings_requests"] = int(obs["voice_settings_requests"]) + 1

    def observe_response(tag: str, response) -> None:
        request = response.request
        parsed = urlsplit(response.url)
        if not _same_acceptance_origin(response.url, config.origin):
            violation("浏览器收到了非本次本机地址的响应")
            return
        try:
            headers = {key.lower(): value for key, value in response.all_headers().items()}
        except Exception:
            violation("浏览器无法核对响应的实例标记")
            return
        if headers.get(INSTANCE_MARKER_HEADER) != config.instance_marker:
            violation("响应不属于本次启动的临时服务")
        if headers.get(HARNESS_MARKER_HEADER) != HARNESS_MARKER_VALUE:
            violation("响应不属于照护员本机演练环境")
        if response.status >= 400 and not expected_failure(tag, request, parsed.path, response.status, response):
            posted = posted_json(request)
            kind = f" kind={posted['kind']}" if posted and isinstance(posted.get("kind"), str) else ""
            violation(f"[{tag}] 非预期 HTTP {response.status} {request.method} {parsed.path} "
                      f"({_stable_error_code(response) or '-'}){kind}")
        logged_in = obs["logged_in"]
        assert isinstance(logged_in, dict)
        if request.method == "POST" and parsed.path == "/auth/login" and response.status == 200:
            logged_in[tag] = True
        if (request.method == "POST" and parsed.path.startswith("/caregiver/visit-plans/")
                and parsed.path.endswith("/start") and response.status == 200):
            try:
                payload = response.json()
                obs["plan_id"] = payload["plan_id"]
                obs["session_id"] = payload["session"]["session_id"]
            except Exception:
                violation("开始本次回执缺少完整计划或场次标识")
        if response.status == 200 and parsed.path in {"/device/pair", "/device/attach"}:
            try:
                attached = obs["attached_session_ids"]
                assert isinstance(attached, list)
                attached.append(response.json()["sessionId"])
            except Exception:
                violation("设备配对/续接回执缺少场次标识")
        if response.status == 200 and parsed.path.endswith("/autopilot/next"):
            try:
                register_command(response.json())
            except Exception:
                violation("下一条命令响应无法校验")
        if (request.method == "GET" and response.status == 200
                and _VOICE_SETTINGS_PATH.match(parsed.path)):
            try:
                settings = response.json()
            except Exception:
                settings = None
            if voice_settings_shape_ok(settings):
                found = obs["voice_settings"]
                assert isinstance(found, list)
                found.append(settings)
            else:
                violation("服务端语音设置回执不是约定的四个字段")
        if request.method == "POST" and parsed.path == "/audio" and response.status == 200:
            posted = posted_json(request)
            raw_id = str(posted.get("raw_audio_id")) if posted else "-"
            posts = obs["audio_posts"]
            assert isinstance(posts, dict)
            posts[raw_id] = int(posts.get(raw_id, 0)) + 1
        if (request.method == "PUT" and parsed.path.startswith("/audio/")
                and parsed.path.endswith("/blob") and response.status == 200):
            uploads = obs["audio_uploads"]
            assert isinstance(uploads, list)
            uploads.append(parsed.path.split("/")[-2])
        if request.method == "PUT" and parsed.path == "/live/state" and response.status == 200:
            posted = posted_json(request)
            if posted and posted.get("kind") == "audioSaved":
                saved = obs["audio_saved"]
                assert isinstance(saved, list)
                saved.append(str((posted.get("payload") or {}).get("rawAudioId")))
        if request.method == "POST" and parsed.path.endswith("/acks") and response.status == 200:
            posted = posted_json(request)
            ack_type = posted.get("ack_type") if posted else None
            key = parsed.path.split("/")[-2]
            if isinstance(ack_type, str):
                ack_types = obs["ack_types"]
                ack_at = obs["ack_at"]
                assert isinstance(ack_types, dict) and isinstance(ack_at, dict)
                ack_types.setdefault(key, []).append(ack_type)
                ack_at.setdefault((key, ack_type), time.monotonic())
                if ack_type == "record_stopped":
                    bodies = obs["stop_bodies"]
                    assert isinstance(bodies, dict)
                    bodies[key] = posted
            else:
                violation("设备命令 ACK 无法校验")
            try:
                register_command(response.json().get("command"))
            except Exception:
                violation("命令 ACK 回执体里的后续命令无法校验")
        session_id = obs["session_id"]
        if (request.method == "POST" and isinstance(session_id, str) and response.status == 200
                and parsed.path == f"/sessions/{session_id}/autopilot/start"):
            obs["autopilot_started"] = True

    def observe_request_failure(request) -> None:
        if bool(obs["teardown_started"]):
            return
        parsed = urlsplit(request.url)
        failure = str(request.failure or "")
        if request.method == "GET" and parsed.path == "/live/state" and "ERR_ABORTED" in failure:
            # 平板的实时状态长轮询由页面自己撤销(到客户端期限、能力重探、离线),不是断连。
            return
        if (request.method == "POST" and parsed.path.endswith("/barge-in-authorization")
                and "ERR_ABORTED" in failure):
            # 停播检测的授权请求挂在检测器自己的 AbortController 上:播完、暂停或换命令时
            # 检测器关闭,在途授权由平板撤销。真断连是 ERR_CONNECTION_* 或 5xx。
            return
        violation(f"浏览器网络请求未完成 {request.method} {parsed.path} ({failure or '-'})")

    def attach_page(tag: str, page) -> None:
        page.on("pageerror", lambda _error: violation(f"[{tag}] 页面运行出错"))

        def observe_console(message) -> None:
            if message.type != "error":
                return
            text = message.text
            if text.startswith("Failed to load resource: the server responded with a status of"):
                return
            violation(f"[{tag}] 页面控制台报错: {text[:200]}")

        page.on("console", observe_console)
        page.on("request", observe_request)
        page.on("response", lambda response: observe_response(tag, response))
        page.on("requestfailed", observe_request_failure)

    def raise_if_violated() -> None:
        if violations:
            raise BrowserAcceptanceError(violations[0])

    def acks(key: str) -> list[str]:
        ack_types = obs["ack_types"]
        assert isinstance(ack_types, dict)
        return list(ack_types.get(key, []))

    def elapsed_ms(key: str, first: str, second: str) -> int:
        ack_at = obs["ack_at"]
        assert isinstance(ack_at, dict)
        return round((ack_at[(key, second)] - ack_at[(key, first)]) * 1000)

    def commands() -> list[dict]:
        table = obs["commands"]
        order = obs["command_order"]
        assert isinstance(table, dict) and isinstance(order, list)
        return [table[key] for key in order]

    def started_record(item_ref: str, after_seq: int = 0) -> dict | None:
        for command in commands():
            if (command["kind"] == "record" and command["item_ref"] == item_ref
                    and isinstance(command["command_seq"], int) and command["command_seq"] > after_seq
                    and "record_started" in acks(command["command_key"])):
                return command
        return None

    def started_tts(after_seq: int, *, item_ref: str, purposes: set[str]) -> dict | None:
        for command in commands():
            if (command["kind"] == "tts" and command["item_ref"] == item_ref
                    and command["purpose"] in purposes
                    and isinstance(command["command_seq"], int) and command["command_seq"] > after_seq
                    and "tts_started" in acks(command["command_key"])):
                return command
        return None

    def wait_uploaded(record: dict, label: str, pages) -> dict:
        key = record["command_key"]
        _wait_for(lambda: "record_stopped" in acks(key), pages,
                  timeout_seconds=40, label=f"{label}不点按钮的自动收麦")
        bodies = obs["stop_bodies"]
        assert isinstance(bodies, dict)
        body = bodies[key]
        raw_id = body.get("raw_audio_id")
        if raw_id != record["raw_audio_id"]:
            raise BrowserAcceptanceError(f"{label}的 record_stopped 不是录音命令签发的那段录音")
        posts, uploads, saved = obs["audio_posts"], obs["audio_uploads"], obs["audio_saved"]
        assert isinstance(posts, dict) and isinstance(uploads, list) and isinstance(saved, list)
        if posts.get(raw_id) != 1 or uploads.count(raw_id) != 1 or saved.count(raw_id) != 1:
            raise BrowserAcceptanceError(f"{label}的录音没有恰好一次登记、上传与采集回执")
        return body

    browser = None
    contexts: list[object] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel="chrome", headless=True,
                args=[
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-background-networking", "--disable-component-update",
                    "--disable-default-apps", "--disable-sync",
                    "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                    f"--use-file-for-fake-audio-capture={fake_audio}",
                ],
            )
            caregiver_context = browser.new_context(base_url=config.origin, permissions=[], service_workers="block")
            patient_context = browser.new_context(base_url=config.origin, permissions=["microphone"], service_workers="block")
            contexts = [patient_context, caregiver_context]
            for context in contexts:
                context.route("**/*", route_request)
            patient_context.add_init_script(_MEDIA_CENSUS_SCRIPT)
            caregiver = caregiver_context.new_page()
            patient = patient_context.new_page()
            attach_page("caregiver", caregiver)
            attach_page("patient", patient)
            pages = (patient, caregiver)

            # ---- 0. 照护员开场、平板配对、开始练习 ----
            caregiver.goto(f"{config.origin}/console", wait_until="domcontentloaded")
            caregiver.get_by_label("用户名").fill(config.username)
            caregiver.get_by_label("密码").fill(config.password)
            caregiver.get_by_role("button", name="登录", exact=True).click()
            caregiver.get_by_role("heading", name="今天要做的练习").wait_for(state="visible", timeout=20_000)
            caregiver.get_by_role("listitem").filter(has_text=PATIENT_ID).get_by_role(
                "button", name="开始本次", exact=True).click()
            caregiver.get_by_role("heading", name=PATIENT_ID, exact=True).wait_for(state="visible", timeout=20_000)
            caregiver.get_by_text("老人画面还没打开", exact=True).wait_for(state="visible", timeout=20_000)
            _wait_for(lambda: isinstance(obs["session_id"], str), (caregiver,), timeout_seconds=10, label="开场回执")
            session_id = str(obs["session_id"])

            patient.goto(f"{config.origin}/patient", wait_until="domcontentloaded")
            pair_dialog = patient.get_by_role("dialog", name="连接这台平板", exact=True)
            pair_dialog.wait_for(state="visible", timeout=20_000)
            patient.get_by_label("配对码").fill(config.pin)
            patient.get_by_role("button", name="完成配对", exact=True).click()
            pair_dialog.wait_for(state="hidden", timeout=20_000)
            attached = obs["attached_session_ids"]
            assert isinstance(attached, list)
            # 配对框可能先于 /device/pair 的响应事件派发到本进程就收起来了:轮询,不一次性判。
            _wait_for(lambda: len(attached) >= 1, (patient,), timeout_seconds=10, label="平板配对回执")
            if attached[:1] != [session_id]:
                raise BrowserAcceptanceError("平板没有配到本场")
            caregiver.get_by_text("老人画面已打开", exact=True).wait_for(state="visible", timeout=25_000)

            start_practice = caregiver.get_by_role("button", name="开始练习", exact=True)
            start_practice.wait_for(state="visible", timeout=20_000)
            patient.get_by_role("button", name="点一下，开始", exact=True).click()
            patient.get_by_role("button", name="关闭语音朗读", exact=True).wait_for(state="visible", timeout=10_000)
            start_practice.click()
            caregiver.get_by_text("练习进行中", exact=True).wait_for(state="visible", timeout=20_000)

            # ---- 链 S:说完就停,不点按钮 ----
            _wait_for(lambda: started_record("itm-0001") is not None, pages,
                      timeout_seconds=45, label="第 1 题开麦")
            silence_record = started_record("itm-0001")
            assert silence_record is not None
            voice_settings = obs["voice_settings"]
            assert isinstance(voice_settings, list)
            expectation = resolve_voice_expectation(
                list(voice_settings), requested=int(obs["voice_settings_requests"]), switches=switches)
            patient.get_by_text("正在听您说", exact=True).wait_for(state="visible", timeout=10_000)
            silence_body = wait_uploaded(silence_record, "链 S ", pages)
            # 立刻换片:下一次取流(第 2 题问句里的声音停播检测、第 2 题开麦)读到的是不停顿的说话。
            write_fake_microphone(fake_audio, continuous_voice_samples(), replace=True)
            silence_key = silence_record["command_key"]
            silence_elapsed = elapsed_ms(silence_key, "record_started", "record_stopped")
            problems = stop_fact_violations(silence_body, vad_expected=expectation["vad_enabled"])
            voiced_fact = silence_body.get("vad_voiced_ms")
            voiced_fact = voiced_fact if type(voiced_fact) is int else None
            if expectation["vad_enabled"]:
                budget = silence_stop_budget_ms(expectation, voiced_fact)
                if silence_body.get("stop_reason") != "silence":
                    problems.append(f"链 S 的 stop_reason 是 {silence_body.get('stop_reason')},不是 silence")
                if not expectation["trailing_ms"] <= silence_elapsed <= budget:
                    problems.append(f"链 S 的 record_stopped 在 record_started 之后 {silence_elapsed} ms 到达,"
                                    f"不在 [{expectation['trailing_ms']}, {budget}] ms 内")
                if voiced_fact is not None and voiced_fact < TABLET_MIN_VOICED_MS:
                    problems.append(f"链 S 判了 silence,vad_voiced_ms 却只有 {voiced_fact}")
            elif silence_body.get("stop_reason") != "max_duration":
                problems.append(f"VAD 关着,链 S 的 stop_reason 却是 {silence_body.get('stop_reason')}")
            if problems:
                raise BrowserAcceptanceError(problems[0])
            seq = int(silence_record["command_seq"])
            _wait_for(lambda: started_tts(seq, item_ref="itm-0001", purposes={"feedback"}) is not None,
                      pages, timeout_seconds=30, label="链 S 判分后的反馈话术")
            feedback = started_tts(seq, item_ref="itm-0001", purposes={"feedback"})
            assert feedback is not None
            if feedback["command_seq"] != seq + 1:
                raise BrowserAcceptanceError("链 S 录音之后的下一条命令不是本题的反馈话术")
            _wait_for(lambda: started_tts(seq, item_ref="itm-0002", purposes={"question"}) is not None,
                      pages, timeout_seconds=30, label="第 2 题问句开始播放")
            raise_if_violated()

            # ---- 链 C:一直在说,不点按钮,只能撞作答窗口 ----
            _wait_for(lambda: started_record("itm-0002") is not None, pages,
                      timeout_seconds=30, label="第 2 题开麦")
            continuous_record = started_record("itm-0002")
            assert continuous_record is not None
            patient.get_by_text("正在听您说", exact=True).wait_for(state="visible", timeout=10_000)
            continuous_body = wait_uploaded(continuous_record, "链 C ", pages)
            continuous_key = continuous_record["command_key"]
            continuous_elapsed = elapsed_ms(continuous_key, "record_started", "record_stopped")
            limit = continuous_record["max_duration_seconds"]
            if not isinstance(limit, (int, float)) or isinstance(limit, bool):
                raise BrowserAcceptanceError("链 C 的录音命令没有作答上限")
            duration = continuous_body.get("duration_seconds")
            problems = stop_fact_violations(continuous_body, vad_expected=expectation["vad_enabled"])
            if continuous_body.get("stop_reason") != "max_duration":
                problems.append(f"链 C 的 stop_reason 是 {continuous_body.get('stop_reason')},不是 max_duration")
            # 平板的作答窗口 = 上限 − 1 s 停麦抖动容差(autopilotCaptureWindow.ts)。
            if (not isinstance(duration, (int, float)) or isinstance(duration, bool)
                    or not limit - 1.5 <= duration <= limit):
                problems.append(f"链 C 录音 {duration} s,不是撞作答窗口({limit - 1:g} s)收的麦")
            if continuous_elapsed < (limit - 1.5) * 1000:
                problems.append(f"链 C 的 record_stopped 在 {continuous_elapsed} ms 就到了,早于作答窗口")
            continuous_voiced = continuous_body.get("vad_voiced_ms")
            if type(continuous_voiced) is int and continuous_voiced < CONTINUOUS_MIN_LOUD_MS:
                problems.append(f"链 C 的 vad_voiced_ms 只有 {continuous_voiced}:说话没被 VAD 听到")
            if problems:
                raise BrowserAcceptanceError(problems[0])
            continuous_seq = int(continuous_record["command_seq"])
            _wait_for(lambda: started_tts(continuous_seq, item_ref="itm-0002",
                                          purposes={"cue", "feedback", "tell_answer"}) is not None,
                      pages, timeout_seconds=30, label="链 C 判分后的下一条话术")
            raise_if_violated()

            # ---- 收尾:照护员安全暂停,老人页的麦克风与音频图全部归零 ----
            pause_button = caregiver.get_by_role("button", name="暂停练习", exact=True)
            pause_button.wait_for(state="visible", timeout=20_000)
            _wait_for(pause_button.is_enabled, (caregiver,), timeout_seconds=20, label="「暂停练习」可点")
            obs["pause_requested"] = True
            pause_button.click()
            caregiver.get_by_text("练习已暂停", exact=True).wait_for(state="visible", timeout=20_000)
            patient.get_by_text("练习已暂停，请稍候", exact=True).wait_for(state="visible", timeout=20_000)

            def census() -> dict:
                value = patient.evaluate("() => window.__nmuVoiceChainsCensus()")
                return value if isinstance(value, dict) else {}

            def drained() -> bool:
                value = census()
                return (value.get("liveTracks"), value.get("unclosedContexts")) == (0, 0)

            _wait_for(drained, pages, timeout_seconds=15, label="暂停后老人页麦克风与音频图归零")
            snapshot = census()
            expect_vad = [expectation["vad_enabled"], expectation["vad_enabled"]]
            problems = census_violations(snapshot, expect_vad=expect_vad)
            listeners = {str(item.get("stream")): item for item in snapshot.get("listeners") or []
                         if isinstance(item, dict)}
            streams = [str(entry.get("stream")) for entry in snapshot.get("journal") or []
                       if entry.get("event") == "gum_resolved" and entry.get("kind") == "record"]
            heard = {chain: listeners.get(stream) for chain, stream in zip(("silence", "continuous"), streams)}
            for chain in ("silence", "continuous"):
                problems += listener_violations(heard.get(chain), chain=chain, expectation=expectation)
            if problems:
                raise BrowserAcceptanceError(problems[0])
            raise_if_violated()

            obs["teardown_started"] = True
            for context in contexts:
                context.close()
            contexts = []
            browser.close()
            browser = None

            def loud(chain: str) -> str:
                listener = heard.get(chain)
                return str(listener.get("loudMs")) if listener else "absent"

            result = VoiceChainsResult(
                session_id=session_id,
                plan_id=str(obs["plan_id"]),
                settings_source=expectation["source"],
                vad_expected="on" if expectation["vad_enabled"] else "off",
                trailing_ms=str(expectation["trailing_ms"]),
                silence_record_key=silence_key,
                silence_raw_audio_id=str(silence_body["raw_audio_id"]),
                silence_stop_reason=str(silence_body["stop_reason"]),
                silence_elapsed_ms=str(silence_elapsed),
                silence_duration_seconds=repr(silence_body["duration_seconds"]),
                silence_vad_armed=_fact_text(silence_body, "vad_armed"),
                silence_vad_voiced_ms=_fact_text(silence_body, "vad_voiced_ms"),
                silence_listener_loud_ms=loud("silence"),
                continuous_record_key=continuous_key,
                continuous_raw_audio_id=str(continuous_body["raw_audio_id"]),
                continuous_stop_reason=str(continuous_body["stop_reason"]),
                continuous_elapsed_ms=str(continuous_elapsed),
                continuous_duration_seconds=repr(continuous_body["duration_seconds"]),
                continuous_vad_armed=_fact_text(continuous_body, "vad_armed"),
                continuous_vad_voiced_ms=_fact_text(continuous_body, "vad_voiced_ms"),
                continuous_listener_loud_ms=loud("continuous"),
                max_live_tracks=str(snapshot.get("maxLiveTracks")),
            )
            _write_receipt(config, result)
            return result
    finally:
        obs["teardown_started"] = True
        for resource in [*contexts, browser]:
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        try:
            fake_audio.unlink(missing_ok=True)
        except OSError:
            pass


# ----------------------------------------------------------------------------
# 账本核验(项目 venv,同一临时库)
# ----------------------------------------------------------------------------
def _validate_voice_ledger(config, result: VoiceChainsResult) -> None:
    from sqlmodel import Session, select

    from app import autopilot_ledger, autopilot_service, db
    from app.models import (
        AttemptCaptureProcessing, AttemptEvent, AudioAssetRow, AutopilotControlEvent,
        RuntimeCommand, RuntimeCommandAck, Session as TrainSession, SessionRuntimeState,
    )
    from harness import caregiver_demo_harness

    caregiver_demo_harness._assert_imported_engine_binding(config.base, db)  # noqa: SLF001
    with Session(db.engine) as session, session.no_autoflush:
        if session.get(TrainSession, result.session_id) is None:
            raise BrowserAcceptanceError("账本没有本场")
        runtime = session.get(SessionRuntimeState, result.session_id)
        if runtime is None or runtime.status != "paused":
            raise BrowserAcceptanceError("收尾后场次 runtime 不是暂停态")
        controls = list(session.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.session_id == result.session_id)))
        if any(event.event_type == "failure" for event in controls):
            raise BrowserAcceptanceError("本场自动带练账本里出现了失败事件")

        def chain(label: str, key: str, raw_audio_id: str, stop_reason: str,
                  duration: str, armed: str, voiced: str) -> tuple[RuntimeCommand, AttemptEvent]:
            rows = list(session.exec(select(RuntimeCommand).where(
                RuntimeCommand.session_id == result.session_id,
                RuntimeCommand.idempotency_key == key)))
            if len(rows) != 1 or rows[0].kind != "record":
                raise BrowserAcceptanceError(f"账本缺少{label}的录音命令")
            record = rows[0]
            acks = list(session.exec(select(RuntimeCommandAck).where(
                RuntimeCommandAck.command_id == record.id).order_by(RuntimeCommandAck.device_event_seq)))
            if [ack.ack_type for ack in acks] != ["record_started", "record_stopped"]:
                raise BrowserAcceptanceError(f"{label}的设备回执不是开录、收麦各一次")
            try:
                proof = autopilot_ledger.verify_immutable_record_capture(session, record)
            except autopilot_ledger.AutopilotProofError as exc:
                raise BrowserAcceptanceError(f"账本无法重新证明{label}的录音命令、回执与收据") from exc
            payload = json.loads(acks[-1].payload_json)
            if payload.get("stop_reason") != stop_reason:
                raise BrowserAcceptanceError(f"{label}落账的 stop_reason 不是浏览器看到的 {stop_reason}")
            # 新平板多带的两个可选事实:账本里有就必须与浏览器发出的一致;老账本只有 stop_reason。
            for name, seen in (("vad_armed", armed), ("vad_voiced_ms", voiced)):
                if name in payload and json.dumps(payload[name]) != seen:
                    raise BrowserAcceptanceError(f"{label}落账的 {name} 与浏览器发出的不一致")
            if proof.raw_audio_id != raw_audio_id or proof.duration_seconds != float(duration):
                raise BrowserAcceptanceError(f"{label}落账的录音与浏览器看到的不是同一段")
            asset = session.get(AudioAssetRow, proof.raw_audio_id)
            if (asset is None or asset.uploaded_at is None or asset.checksum != proof.checksum
                    or asset.byte_count != proof.byte_count):
                raise BrowserAcceptanceError(f"{label}的录音没有真实上传")
            captures = list(session.exec(select(AttemptCaptureProcessing).where(
                AttemptCaptureProcessing.record_command_id == record.id)))
            attempt = (session.get(AttemptEvent, captures[0].final_attempt_id)
                       if len(captures) == 1 and captures[0].final_attempt_id is not None else None)
            if attempt is None or attempt.processing_status != "completed":
                raise BrowserAcceptanceError(f"{label}的录音没有完成识别与判分")
            return record, attempt

        silence_record, silence_attempt = chain(
            "链 S", result.silence_record_key, result.silence_raw_audio_id, result.silence_stop_reason,
            result.silence_duration_seconds, result.silence_vad_armed, result.silence_vad_voiced_ms)
        continuous_record, _ = chain(
            "链 C", result.continuous_record_key, result.continuous_raw_audio_id,
            result.continuous_stop_reason, result.continuous_duration_seconds,
            result.continuous_vad_armed, result.continuous_vad_voiced_ms)
        if silence_attempt.operational_answer_type != "正确":
            raise BrowserAcceptanceError("链 S 的回答没有按确定性 ASR 判成正确")

        def next_tts(record: RuntimeCommand):
            row = session.exec(select(RuntimeCommand).where(
                RuntimeCommand.session_id == result.session_id,
                RuntimeCommand.command_seq == record.command_seq + 1)).first()
            if row is None or row.kind != "tts":
                return None, None
            return row, autopilot_service.TtsCommandPayload.model_validate_json(row.payload_json).purpose

        _feedback, purpose = next_tts(silence_record)
        if purpose != "feedback" or continuous_record.item_id == silence_record.item_id:
            raise BrowserAcceptanceError("链 S 之后场次没有照常走到反馈与下一题")
        _cue, purpose = next_tts(continuous_record)
        if purpose not in {"cue", "feedback", "tell_answer"}:
            raise BrowserAcceptanceError("链 C 判分之后场次没有照常给出下一条话术")


def validate_voice_ledger() -> None:
    from harness import caregiver_demo_harness
    from harness.tts_ack_harness import HarnessConfigError

    try:
        config = caregiver_demo_harness.resolve_caregiver_config()
    except HarnessConfigError as exc:
        raise BrowserAcceptanceError("账本核验的临时目录或运行配置无效") from exc
    _validate_voice_ledger(config, _read_receipt(config.base.root))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="照护员真实 Chrome:尾静默自动收麦走查")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--voice-chains", action="store_true")
    group.add_argument("--verify-ledger", action="store_true")
    parser.add_argument("--origin")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    secrets = tuple(os.environ.get(name) or "" for name in (
        "NMU_CAREGIVER_PASSWORD", "CONSOLE_PIN", "NMU_CAREGIVER_HARNESS_INSTANCE", ADMIN_PASSWORD_ENV))
    try:
        if args.voice_chains:
            if not args.origin:
                raise BrowserAcceptanceError("语音链走查缺少本机地址")
            result = run_voice_chains(resolve_browser_config(args.origin))
            print(f"真实 Chrome 语音链已走完(语音设置来源 {result.settings_source},VAD {result.vad_expected},"
                  f"尾静默 {result.trailing_ms} ms,最多 {result.max_live_tracks} 条活轨道)")
            print(f"  链 S:stop_reason={result.silence_stop_reason},record_started→record_stopped "
                  f"{result.silence_elapsed_ms} ms,录音 {result.silence_duration_seconds} s,"
                  f"旁听有声 {result.silence_listener_loud_ms} ms,vad_armed={result.silence_vad_armed},"
                  f"vad_voiced_ms={result.silence_vad_voiced_ms}")
            print(f"  链 C:stop_reason={result.continuous_stop_reason},record_started→record_stopped "
                  f"{result.continuous_elapsed_ms} ms,录音 {result.continuous_duration_seconds} s,"
                  f"旁听有声 {result.continuous_listener_loud_ms} ms,vad_armed={result.continuous_vad_armed},"
                  f"vad_voiced_ms={result.continuous_vad_voiced_ms}")
        else:
            if args.origin:
                raise BrowserAcceptanceError("账本核验不接受网页地址")
            validate_voice_ledger()
            print("语音链账本核验已通过")
        return 0
    except BrowserAcceptanceError as exc:
        print(f"语音链走查失败:{_redacted_error(exc, secrets)}", file=sys.stderr)
        return 1
    except Exception as exc:  # fail closed without leaking browser call values
        print(f"语音链走查失败:{_redacted_error(exc, secrets)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
