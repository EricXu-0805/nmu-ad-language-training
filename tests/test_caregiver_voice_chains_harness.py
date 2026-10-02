"""语音链走查(尾静默自动收麦)的纯契约测试:不开浏览器、不起服务。"""
from __future__ import annotations

import json
import math
import wave
from pathlib import Path

import pytest

from harness import caregiver_voice_chains as voice
from harness.caregiver_browser_acceptance import BrowserAcceptanceError


def _read_wav(path: Path) -> tuple[int, list[float]]:
    with wave.open(str(path), "rb") as source:
        assert source.getnchannels() == 1
        assert source.getsampwidth() == 2
        rate = source.getframerate()
        raw = source.readframes(source.getnframes())
    return rate, [int.from_bytes(raw[i:i + 2], "little", signed=True) / 32768
                  for i in range(0, len(raw), 2)]


def _rms(samples: list[float]) -> float:
    return math.sqrt(sum(value * value for value in samples) / len(samples))


def _syllables(samples: list[float], rate: int) -> list[list[float]]:
    """按 ≥40 ms 的近零段切开,返回每一节有声片段。"""
    quiet_run = int(0.04 * rate)
    pieces: list[list[float]] = []
    current: list[float] = []
    zeros = 0
    for value in samples:
        if abs(value) < 1e-4:
            zeros += 1
            if zeros >= quiet_run and current:
                pieces.append(current)
                current = []
            continue
        zeros = 0
        current.append(value)
    if current:
        pieces.append(current)
    return pieces


def _crossing_hz(segment: list[float], rate: int) -> float:
    crossings = sum(1 for left, right in zip(segment, segment[1:]) if (left < 0) != (right < 0))
    return crossings / 2 / (len(segment) / rate)


def _longest_quiet_seconds(samples: list[float], rate: int, threshold: float) -> float:
    longest = run = 0
    for value in samples:
        run = run + 1 if abs(value) < threshold else 0
        longest = max(longest, run)
    return longest / rate


def test_fake_microphone_speaks_then_stays_near_silent(tmp_path):
    path = tmp_path / "voice.wav"
    voice.write_fake_microphone(path, voice.voice_then_hush_samples())
    rate, samples = _read_wav(path)
    assert rate == voice.FAKE_MIC_RATE
    assert path.stat().st_mode & 0o777 == 0o600
    voiced_end = round(voice.VOICED_SECONDS * rate)
    voiced = samples[:voiced_end]
    hush = samples[voiced_end:]
    assert 1.5 <= voice.VOICED_SECONDS <= 1.7
    assert 6.5 <= len(hush) / rate <= 7.5
    assert _rms(voiced) > 0.15
    assert _rms(hush) < 0.003
    assert max(abs(value) for value in hush) < 0.01
    # 有声段的最后一节在 VOICED_SECONDS 处正好收尾到零,不是被半截切断的。
    assert max(abs(value) for value in voiced[-int(0.002 * rate):]) < 0.02


def test_fake_speech_stays_below_the_barge_in_voice_band():
    """音节基频必须低于提前回答检测的 150 Hz 下沿,否则问句里就触发了声音停播。"""
    rate = voice.FAKE_MIC_RATE
    speech = voice.voice_then_hush_samples()[:round(voice.VOICED_SECONDS * rate)]
    for samples in (speech, voice.continuous_voice_samples()):
        pieces = [piece for piece in _syllables(samples, rate) if len(piece) > 0.1 * rate]
        assert pieces
        for piece in pieces:
            assert 85 <= _crossing_hz(piece, rate) <= voice.BARGE_IN_VOICE_BAND_LOW_HZ - 5


def test_continuous_speech_never_pauses_long_enough_to_end_a_turn():
    rate = voice.FAKE_MIC_RATE
    samples = voice.continuous_voice_samples()
    # Chrome 循环播放这个文件:接缝处也不许冒出一段长静默。
    looped = samples + samples
    assert _rms(samples) > 0.15
    assert _longest_quiet_seconds(looped, rate, 0.01) < 0.1
    assert 5.5 <= len(samples) / rate <= 6.5


def test_fake_microphone_is_deterministic_and_swaps_atomically(tmp_path):
    first = tmp_path / "a.wav"
    second = tmp_path / "b.wav"
    voice.write_fake_microphone(first, voice.voice_then_hush_samples())
    voice.write_fake_microphone(second, voice.voice_then_hush_samples())
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(BrowserAcceptanceError, match="本次新建"):
        voice.write_fake_microphone(first, voice.continuous_voice_samples())
    voice.write_fake_microphone(first, voice.continuous_voice_samples(), replace=True)
    _rate, swapped = _read_wav(first)
    assert len(swapped) == len(voice.continuous_voice_samples())
    assert first.stat().st_mode & 0o777 == 0o600
    assert sorted(item.name for item in tmp_path.iterdir()) == ["a.wav", "b.wav"]


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "OFF", " No "])
def test_voice_switch_falsy_values_disable(value):
    switches = voice.configured_voice_switches({
        "NMU_AUTOPILOT_VAD": value, "NMU_AUTOPILOT_VOICE_BARGE_IN": value})
    assert switches == {"vad_enabled": False, "barge_in_enabled": False, "windows_overridden": False}


def test_voice_switches_default_to_enabled_and_flag_window_overrides():
    assert voice.configured_voice_switches({}) == {
        "vad_enabled": True, "barge_in_enabled": True, "windows_overridden": False}
    assert voice.configured_voice_switches({"NMU_AUTOPILOT_VAD": "1", "NMU_AUTOPILOT_VAD_TRAILING_MS": ""}) == {
        "vad_enabled": True, "barge_in_enabled": True, "windows_overridden": False}
    for name in ("NMU_AUTOPILOT_VAD_TRAILING_MS", "NMU_AUTOPILOT_VAD_SHORT_TRAILING_MS"):
        assert voice.configured_voice_switches({name: "5000"})["windows_overridden"] is True


def _settings(**overrides):
    value = {"vad_enabled": True, "vad_trailing_silence_ms": 3000,
             "vad_short_trailing_silence_ms": 4500, "barge_in_enabled": True}
    value.update(overrides)
    return value


def test_voice_settings_shape_is_exact():
    assert voice.voice_settings_shape_ok(_settings())
    assert not voice.voice_settings_shape_ok({**_settings(), "extra": 1})
    assert not voice.voice_settings_shape_ok({k: v for k, v in _settings().items() if k != "barge_in_enabled"})
    assert not voice.voice_settings_shape_ok(_settings(vad_enabled=1))
    assert not voice.voice_settings_shape_ok(_settings(vad_trailing_silence_ms=True))
    assert not voice.voice_settings_shape_ok(_settings(vad_trailing_silence_ms=3000.0))
    assert not voice.voice_settings_shape_ok(_settings(vad_trailing_silence_ms=1499))
    assert not voice.voice_settings_shape_ok(_settings(vad_short_trailing_silence_ms=10_001))
    assert not voice.voice_settings_shape_ok([])


_DEFAULT_SWITCHES = {"vad_enabled": True, "barge_in_enabled": True, "windows_overridden": False}


def test_expectation_without_voice_settings_is_the_tablet_default():
    expectation = voice.resolve_voice_expectation([], requested=0, switches=_DEFAULT_SWITCHES)
    assert expectation == {
        "source": "tablet_default", "vad_enabled": True, "barge_in_enabled": True,
        "trailing_ms": voice.TABLET_TRAILING_SILENCE_MS,
        "short_trailing_ms": voice.TABLET_SHORT_TRAILING_SILENCE_MS,
    }


@pytest.mark.parametrize("switches", [
    {**_DEFAULT_SWITCHES, "vad_enabled": False},
    {**_DEFAULT_SWITCHES, "barge_in_enabled": False},
    {**_DEFAULT_SWITCHES, "windows_overridden": True},
])
def test_expectation_refuses_switches_the_tablet_never_read(switches):
    with pytest.raises(BrowserAcceptanceError, match="voice-settings"):
        voice.resolve_voice_expectation([], requested=0, switches=switches)


def test_expectation_refuses_a_capture_opened_before_settings_arrived():
    with pytest.raises(BrowserAcceptanceError, match="开麦前没拿到"):
        voice.resolve_voice_expectation([], requested=1, switches=_DEFAULT_SWITCHES)


def test_expectation_follows_the_latest_server_voice_settings():
    off = {**_DEFAULT_SWITCHES, "vad_enabled": False}
    expectation = voice.resolve_voice_expectation(
        [_settings(), _settings(vad_enabled=False, vad_trailing_silence_ms=2500)],
        requested=2, switches=off)
    assert expectation == {"source": "server", "vad_enabled": False, "barge_in_enabled": True,
                           "trailing_ms": 2500, "short_trailing_ms": 4500}


def test_expectation_rejects_server_settings_that_disagree_with_the_switches():
    with pytest.raises(BrowserAcceptanceError, match="不一致"):
        voice.resolve_voice_expectation([_settings(vad_enabled=False)], requested=1,
                                        switches=_DEFAULT_SWITCHES)


def test_expectation_rejects_a_trailing_window_the_fake_silence_cannot_cover():
    with pytest.raises(BrowserAcceptanceError, match="静音段"):
        voice.resolve_voice_expectation([_settings(vad_trailing_silence_ms=7000)], requested=1,
                                        switches={**_DEFAULT_SWITCHES, "windows_overridden": True})


def test_silence_stop_budget_is_speech_plus_window_plus_slack():
    expectation = voice.resolve_voice_expectation([], requested=0, switches=_DEFAULT_SWITCHES)
    speech_ms = round(voice.VOICED_SECONDS * 1000)
    assert voice.silence_stop_budget_ms(expectation, None) == speech_ms + 3000 + 2000
    assert voice.silence_stop_budget_ms(expectation, 1200) == speech_ms + 3000 + 2000
    # 有声不足一句话时平板用长窗口,预算跟着它走。
    assert voice.silence_stop_budget_ms(expectation, 800) == speech_ms + 4500 + 2000


def _failure(method, path, status, code, *, logged_in=True, started=True, paused=False, barge_in=True):
    return voice.expected_http_failure(
        method, path, status, code, logged_in=logged_in, autopilot_started=started,
        pause_requested=paused, barge_in_enabled=barge_in)


def test_expected_http_failures_are_phase_and_code_closed():
    assert _failure("GET", "/auth/me", 401, "auth_me_not_logged_in", logged_in=False)
    assert not _failure("GET", "/auth/me", 401, "auth_me_not_logged_in")
    assert _failure("GET", "/sessions/S1/autopilot/next", 409, "autopilot_not_active", started=False)
    assert not _failure("GET", "/sessions/S1/autopilot/next", 409, "autopilot_not_active")
    assert _failure("GET", "/sessions/S1/autopilot/next", 409, "autopilot_runtime_inactive", paused=True)
    assert not _failure("GET", "/sessions/S1/autopilot/next", 409, "autopilot_runtime_inactive")
    # 暂停落在问句/线索播放期间:声音停播检测开麦后的第二次授权撞上已暂停的场次。
    path = "/sessions/S1/autopilot/commands/C1/barge-in-authorization"
    assert _failure("POST", path, 409, "autopilot_runtime_inactive", paused=True)
    assert not _failure("POST", path, 409, "autopilot_runtime_inactive")
    # 关了声音停播开关:服务端按约定 409,平板走「检测不可用」。开着时同一个 409 就是故障。
    assert _failure("POST", path, 409, "autopilot_barge_in_disabled", barge_in=False)
    assert not _failure("POST", path, 409, "autopilot_barge_in_disabled")
    assert not _failure("POST", path, 500, None, paused=True)
    assert not _failure("POST", "/sessions/S1/autopilot/commands/C1/recording-authorization", 409,
                        "autopilot_runtime_inactive", paused=True)
    assert not _failure("GET", "/sessions/S1/autopilot/voice-settings", 404, None)


def _event(event, live=0, open_=0, unclosed=0, **extra):
    return {"t": 0.0, "event": event, "liveTracks": live, "openContexts": open_,
            "unclosedContexts": unclosed, **extra}


def _clean_census(*, vad: bool = True) -> dict:
    journal = [
        _event("ack_sent", ackType="tts_started", command="q1"),
        _event("gum_called", kind="monitor"),
        _event("gum_resolved", live=1, kind="monitor", stream="m1"),
        _event("context_created", live=1, open_=1, unclosed=1, context=1),
        _event("context_tapped", live=1, open_=1, unclosed=1, context=1, stream="m1", kind="monitor"),
        _event("track_ended", live=0, open_=1, unclosed=1, kind="monitor"),
        _event("context_close_called", unclosed=1, context=1),
        _event("context_closed", context=1),
        _event("ack_sent", ackType="tts_ended", command="q1"),
        _event("gum_called", kind="record"),
        _event("gum_resolved", live=1, kind="record", stream="r1"),
    ]
    if vad:
        journal += [
            _event("context_created", live=1, open_=1, unclosed=1, context=2),
            _event("context_tapped", live=1, open_=1, unclosed=1, context=2, stream="r1", kind="record"),
        ]
    journal.append(_event("ack_sent", live=1, open_=int(vad), unclosed=int(vad),
                          ackType="record_started", command="r1"))
    if vad:
        journal += [_event("context_close_called", live=1, unclosed=1, context=2),
                    _event("context_closed", live=1, context=2)]
    journal += [
        _event("track_ended", kind="record"),
        _event("ack_sent", ackType="record_stopped", command="r1"),
        _event("ack_sent", ackType="tts_started", command="f1"),
    ]
    listeners = ([{"stream": "r1", "kind": "record", "frames": 70, "loudMs": 1100,
                   "longestQuietMs": 3000, "finalQuietMs": 3000}] if vad else [])
    return {"journal": journal, "maxLiveTracks": 1, "maxOpenContexts": 1,
            "liveTracks": 0, "openContexts": 0, "unclosedContexts": 0, "listeners": listeners}


def test_census_clean_journal_passes():
    assert voice.census_violations(_clean_census(), expect_vad=[True]) == []
    assert voice.census_violations(_clean_census(vad=False), expect_vad=[False]) == []


def test_census_flags_two_live_tracks_at_once():
    census = _clean_census()
    census["maxLiveTracks"] = 2
    assert any("2 条活麦克风轨道" in item for item in voice.census_violations(census, expect_vad=[True]))


@pytest.mark.parametrize("index, ack", [(8, "tts_ended"), (-2, "record_stopped"), (-1, "tts_started")])
@pytest.mark.parametrize("live, open_, unclosed", [(1, 0, 0), (0, 1, 1), (0, 0, 1)])
def test_census_flags_resources_alive_at_command_boundaries(index, ack, live, open_, unclosed):
    # 调过 close() 但还没真正关上(unclosed)也算没清零:命令边界要的是音频图已经不存在。
    census = _clean_census()
    census["journal"][index] = _event("ack_sent", live=live, open_=open_, unclosed=unclosed,
                                      ackType=ack, command="x")
    assert any(ack in item for item in voice.census_violations(census, expect_vad=[True]))


@pytest.mark.parametrize("live, unclosed", [(1, 0), (0, 1)])
def test_census_flags_a_monitor_still_alive_when_the_recorder_opens(live, unclosed):
    census = _clean_census()
    census["journal"][9] = _event("gum_called", live=live, unclosed=unclosed, kind="record")
    assert any("取流" in item for item in voice.census_violations(census, expect_vad=[True]))


def test_census_requires_the_recorder_track_while_recording():
    census = _clean_census()
    index = next(i for i, e in enumerate(census["journal"]) if e.get("ackType") == "record_started")
    census["journal"][index] = _event("ack_sent", live=0, open_=1, ackType="record_started", command="r1")
    assert any("record_started" in item for item in voice.census_violations(census, expect_vad=[True]))


def test_census_requires_exactly_one_vad_listener_per_capture():
    assert any("旁听" in item for item in voice.census_violations(_clean_census(vad=False), expect_vad=[True]))
    assert any("旁听" in item for item in voice.census_violations(_clean_census(), expect_vad=[False]))
    census = _clean_census()
    census["journal"].append(_event("context_tapped", context=3, stream="r1", kind="record"))
    assert any("旁听" in item for item in voice.census_violations(census, expect_vad=[True]))


def test_census_requires_one_expectation_per_record_capture():
    assert any("对不上" in item for item in voice.census_violations(_clean_census(), expect_vad=[True, True]))


@pytest.mark.parametrize("field", ["liveTracks", "openContexts", "unclosedContexts"])
def test_census_flags_leftovers_after_the_final_pause(field):
    census = _clean_census()
    census[field] = 1
    assert any("收尾" in item for item in voice.census_violations(census, expect_vad=[True]))


def test_census_without_a_journal_fails_closed():
    assert voice.census_violations({}, expect_vad=[]) != []


def _listener(**overrides):
    value = {"stream": "r1", "kind": "record", "frames": 80, "loudMs": 1100,
             "longestQuietMs": 3010, "finalQuietMs": 3010}
    value.update(overrides)
    return value


def test_listener_evidence_for_the_silence_chain():
    expectation = voice.resolve_voice_expectation([], requested=0, switches=_DEFAULT_SWITCHES)
    assert voice.listener_violations(_listener(), chain="silence", expectation=expectation) == []
    assert voice.listener_violations(_listener(loudMs=850), chain="silence", expectation=expectation)
    assert voice.listener_violations(_listener(finalQuietMs=2600), chain="silence", expectation=expectation)
    assert voice.listener_violations(None, chain="silence", expectation=expectation)


def test_listener_evidence_for_the_continuous_chain():
    expectation = voice.resolve_voice_expectation([], requested=0, switches=_DEFAULT_SWITCHES)
    heard = _listener(loudMs=9000, longestQuietMs=400, finalQuietMs=64)
    assert voice.listener_violations(heard, chain="continuous", expectation=expectation) == []
    assert voice.listener_violations({**heard, "longestQuietMs": 3000}, chain="continuous",
                                     expectation=expectation)
    # 降噪把「说话」压成静默时 VAD 也不会判停:那是空转的通过,不算证据。
    assert voice.listener_violations({**heard, "loudMs": 600}, chain="continuous", expectation=expectation)


def test_listener_must_be_absent_when_vad_is_off():
    expectation = {"source": "server", "vad_enabled": False, "barge_in_enabled": True,
                   "trailing_ms": 3000, "short_trailing_ms": 4500}
    assert voice.listener_violations(None, chain="silence", expectation=expectation) == []
    assert voice.listener_violations(_listener(), chain="continuous", expectation=expectation)


@pytest.mark.parametrize("body, vad, ok", [
    ({"stop_reason": "silence"}, True, True),
    ({"stop_reason": "max_duration"}, False, True),
    ({"stop_reason": "silence", "vad_armed": True, "vad_voiced_ms": 1200}, True, True),
    ({"stop_reason": "max_duration", "vad_armed": False}, False, True),
    ({"stop_reason": "silence", "vad_voiced_ms": 1200}, True, False),
    ({"stop_reason": "silence", "vad_armed": 1, "vad_voiced_ms": 1200}, True, False),
    ({"stop_reason": "silence", "vad_armed": True}, True, False),
    ({"stop_reason": "silence", "vad_armed": True, "vad_voiced_ms": 600_001}, True, False),
    ({"stop_reason": "silence", "vad_armed": True, "vad_voiced_ms": True}, True, False),
    ({"stop_reason": "max_duration", "vad_armed": False, "vad_voiced_ms": 0}, False, False),
    ({"stop_reason": "max_duration", "vad_armed": True, "vad_voiced_ms": 10}, False, False),
])
def test_record_stopped_optional_vad_facts(body, vad, ok):
    assert (voice.stop_fact_violations(body, vad_expected=vad) == []) is ok


def _env(root: Path) -> dict[str, str]:
    return {
        "NMU_CAREGIVER_USERNAME": "demo20-caregiver",
        "NMU_CAREGIVER_PASSWORD": "caregiver-browser-password",
        "CONSOLE_PIN": "24681357",
        "NMU_CAREGIVER_HARNESS_INSTANCE": "caregiver-browser-instance-marker-000001",
        "NMU_HARNESS_ROOT": str(root),
    }


def _result(**overrides) -> voice.VoiceChainsResult:
    fields = {name: f"{name}-value" for name in voice.VoiceChainsResult.__dataclass_fields__}
    fields.update(overrides)
    return voice.VoiceChainsResult(**fields)


def _config(tmp_path: Path):
    root = tmp_path / "nmu-caregiver-demo20.voice"
    root.mkdir(mode=0o700)
    from harness.caregiver_browser_acceptance import resolve_browser_config
    return root, resolve_browser_config("http://127.0.0.1:8815", _env(root))


def test_result_receipt_is_private_exact_and_non_overwriting(tmp_path):
    root, config = _config(tmp_path)
    result = _result()
    voice._write_receipt(config, result)
    target = root / voice.RESULT_RECEIPT_NAME
    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(target.read_text(encoding="utf-8"))["schema"] == voice.RECEIPT_SCHEMA
    assert voice._read_receipt(root) == result
    with pytest.raises(BrowserAcceptanceError, match="拒绝覆盖"):
        voice._write_receipt(config, result)


def test_result_receipt_rejects_symlink_missing_fields_and_loose_mode(tmp_path):
    root, config = _config(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (root / voice.RESULT_RECEIPT_NAME).symlink_to(outside)
    with pytest.raises(BrowserAcceptanceError):
        voice._read_receipt(root)
    (root / voice.RESULT_RECEIPT_NAME).unlink()

    voice._write_receipt(config, _result())
    target = root / voice.RESULT_RECEIPT_NAME
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["session_id"] = ""
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BrowserAcceptanceError, match="缺少字段"):
        voice._read_receipt(root)
    payload["session_id"] = "S1"
    target.write_text(json.dumps(payload), encoding="utf-8")
    target.chmod(0o644)
    with pytest.raises(BrowserAcceptanceError):
        voice._read_receipt(root)


def test_ledger_verifier_reuses_the_production_capture_proof():
    source = Path(voice.__file__).read_text(encoding="utf-8")
    assert "autopilot_ledger.verify_immutable_record_capture(" in source
    assert "_assert_imported_engine_binding(" in source
