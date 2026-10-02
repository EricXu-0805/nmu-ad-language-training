# ruff: noqa: F811 -- pytest injects the imported isolated fixture.
"""平板声音设置:部署开关的解析口径,与只读设备端点的围栏。"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from app import autopilot_service
from app.models import (AuditLog, LiveState, PatientDeviceCapability, RuntimeCommand,
                        RuntimeCommandAck, SessionAutopilotState, SessionRuntimeState)
from test_autopilot_api import (  # noqa: F401
    OTHER_SESSION_ID, SESSION_ID, _enable_p0a, _start, api_clients,
)


VOICE_ENVS = (
    "NMU_AUTOPILOT_VAD",
    "NMU_AUTOPILOT_VAD_TRAILING_MS",
    "NMU_AUTOPILOT_VAD_SHORT_TRAILING_MS",
    "NMU_AUTOPILOT_VOICE_BARGE_IN",
)
DEFAULTS = {
    "vad_enabled": True,
    "vad_trailing_silence_ms": 3000,
    "vad_short_trailing_silence_ms": 4500,
    "barge_in_enabled": True,
}


def _settings(monkeypatch, **env: str | None) -> dict:
    for name in VOICE_ENVS:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        if value is not None:
            monkeypatch.setenv(name, value)
    return autopilot_service.voice_settings().model_dump()


def test_unset_switches_are_the_documented_defaults(monkeypatch):
    assert _settings(monkeypatch) == DEFAULTS


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "OFF", " False ", "No"])
@pytest.mark.parametrize(("name", "key"), [
    ("NMU_AUTOPILOT_VAD", "vad_enabled"),
    ("NMU_AUTOPILOT_VOICE_BARGE_IN", "barge_in_enabled"),
])
def test_each_off_value_disables_only_its_own_switch(monkeypatch, name, key, value):
    assert _settings(monkeypatch, **{name: value}) == {**DEFAULTS, key: False}


@pytest.mark.parametrize("value", ["", "1", "true", "on", "yes", "enabled", "disabled"])
def test_anything_but_the_four_off_values_keeps_switches_on(monkeypatch, value):
    assert _settings(monkeypatch, NMU_AUTOPILOT_VAD=value,
                     NMU_AUTOPILOT_VOICE_BARGE_IN=value) == DEFAULTS


@pytest.mark.parametrize(("raw", "expected"), [
    ("1500", 1500), ("8000", 8000), (" 2500 ", 2500), ("02500", 2500),
    ("1499", 3000), ("8001", 3000), ("0", 3000), ("-2000", 3000), ("+2500", 3000),
    ("2500.0", 3000), ("3e3", 3000), ("2_500", 3000), ("3000ms", 3000),
    ("", 3000), ("abc", 3000),
])
def test_trailing_silence_outside_bounds_or_unparseable_falls_back(monkeypatch, raw, expected):
    settings = _settings(monkeypatch, NMU_AUTOPILOT_VAD_TRAILING_MS=raw)
    assert settings["vad_trailing_silence_ms"] == expected
    assert settings["vad_short_trailing_silence_ms"] == max(4500, expected)


@pytest.mark.parametrize(("trailing", "short", "expected"), [
    (None, "3000", 3000),
    (None, "10000", 10000),
    (None, "10001", 4500),
    (None, "2999", 4500),
    (None, "4500.0", 4500),
    (None, "x", 4500),
    ("2000", "2000", 2000),
    ("2000", "1800", 4500),
    ("8000", "8000", 8000),
    ("6000", "9000", 9000),
    # trailing 越界先回 3000,短话窗口再按回落后的 trailing 判:9500 仍合法。
    ("9000", "9500", 9500),
    # 默认 4500 比运维调长的 trailing 还短时取 trailing:「短话多等」不能倒挂成短话少等。
    ("6000", None, 6000),
    ("6000", "5000", 6000),
    ("6000", "oops", 6000),
])
def test_short_trailing_silence_is_bounded_and_never_shorter_than_trailing(
        monkeypatch, trailing, short, expected):
    settings = _settings(monkeypatch, NMU_AUTOPILOT_VAD_TRAILING_MS=trailing,
                         NMU_AUTOPILOT_VAD_SHORT_TRAILING_MS=short)
    assert settings["vad_short_trailing_silence_ms"] == expected
    assert settings["vad_short_trailing_silence_ms"] >= settings["vad_trailing_silence_ms"]


@pytest.mark.parametrize("trailing", [None, "0", "1500", "3000", "4500", "4501", "8000", "8001"])
@pytest.mark.parametrize("short", [None, "0", "1500", "4499", "4500", "8000", "10000", "10001"])
def test_published_windows_always_satisfy_the_interface_bounds(monkeypatch, trailing, short):
    settings = _settings(monkeypatch, NMU_AUTOPILOT_VAD_TRAILING_MS=trailing,
                         NMU_AUTOPILOT_VAD_SHORT_TRAILING_MS=short)
    trailing_ms = settings["vad_trailing_silence_ms"]
    short_ms = settings["vad_short_trailing_silence_ms"]
    assert 1500 <= trailing_ms <= 8000
    assert trailing_ms <= short_ms <= 10000
    assert all(type(settings[key]) is int
               for key in ("vad_trailing_silence_ms", "vad_short_trailing_silence_ms"))


def test_switches_are_read_on_every_call(monkeypatch):
    assert _settings(monkeypatch) == DEFAULTS
    monkeypatch.setenv("NMU_AUTOPILOT_VAD", "off")
    monkeypatch.setenv("NMU_AUTOPILOT_VAD_TRAILING_MS", "2000")
    assert autopilot_service.voice_settings().model_dump() == {
        **DEFAULTS, "vad_enabled": False, "vad_trailing_silence_ms": 2000}


def _voice(client, headers=None, session_id=SESSION_ID):
    return client.get(f"/sessions/{session_id}/autopilot/voice-settings",
                      headers=headers or {})


def _ledger(engine) -> dict:
    with Session(engine) as db:
        return {
            "live": db.get(LiveState, 1).model_dump(),
            "runtime": db.get(SessionRuntimeState, SESSION_ID).model_dump(),
            "autopilot": [row.model_dump() for row in db.exec(select(SessionAutopilotState))],
            "commands": [row.model_dump() for row in db.exec(
                select(RuntimeCommand).order_by(RuntimeCommand.id))],
            "acks": [row.model_dump() for row in db.exec(select(RuntimeCommandAck))],
            "devices": [row.model_dump() for row in db.exec(select(PatientDeviceCapability))],
            "audit": [row.model_dump() for row in db.exec(select(AuditLog).order_by(AuditLog.id))],
        }


def test_paired_device_reads_exactly_the_four_keys_without_writing(api_clients, monkeypatch):
    # 平板在 runner 起步时就取,这时可能还没有任何自动驾驶命令。
    before = _ledger(api_clients.engine)
    response = _voice(api_clients.device, api_clients.device_headers)
    assert response.status_code == 200, response.text
    assert response.json() == DEFAULTS
    assert list(response.json()) == list(DEFAULTS)
    assert response.headers["cache-control"] == "private, no-store"
    assert _ledger(api_clients.engine) == before

    monkeypatch.setenv("NMU_AUTOPILOT_VAD", "0")
    monkeypatch.setenv("NMU_AUTOPILOT_VAD_TRAILING_MS", "2500")
    monkeypatch.setenv("NMU_AUTOPILOT_VAD_SHORT_TRAILING_MS", "6000")
    monkeypatch.setenv("NMU_AUTOPILOT_VOICE_BARGE_IN", "false")
    changed = _voice(api_clients.device, api_clients.device_headers)
    assert changed.status_code == 200, changed.text
    assert changed.json() == {
        "vad_enabled": False,
        "vad_trailing_silence_ms": 2500,
        "vad_short_trailing_silence_ms": 6000,
        "barge_in_enabled": False,
    }

    _enable_p0a(monkeypatch)
    assert _start(api_clients).status_code == 200
    started = _ledger(api_clients.engine)
    during = _voice(api_clients.device, api_clients.device_headers)
    assert during.status_code == 200, during.text
    assert _ledger(api_clients.engine) == started


def _response_code(response) -> str | None:
    body = response.json()
    detail = body.get("detail")
    return detail.get("code") if isinstance(detail, dict) else body.get("code")


def _update_capability(engine, **values) -> None:
    with Session(engine) as db:
        row = db.exec(select(PatientDeviceCapability)).one()
        for name, value in values.items():
            setattr(row, name, value)
        db.add(row)
        db.commit()


def _switch_live_session(engine, session_id: str) -> None:
    with Session(engine) as db:
        live = db.get(LiveState, 1)
        payload = json.loads(live.session_json)
        payload["sessionId"] = session_id
        live.session_json = json.dumps(payload, ensure_ascii=False)
        db.add(live)
        db.commit()


@pytest.mark.parametrize(("scenario", "status", "code"), [
    ("account_cookie", 403, "device_capability_required"),
    ("anonymous", 401, "device_pair_required"),
    ("other_session", 409, "device_session_mismatch"),
    ("revoked", 401, "device_capability_revoked"),
    ("expired", 401, "device_capability_expired"),
    ("recovery_only", 401, "device_capability_recovery_only"),
    ("active_slot_released", 401, "device_capability_recovery_only"),
    ("live_switched", 409, "device_session_changed"),
    ("visit_plan_not_started", 409, "session_visit_plan_admission_required"),
])
def test_voice_settings_has_exactly_the_autopilot_next_fences(
        api_clients, monkeypatch, scenario, status, code):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    client, headers, session_id = api_clients.device, api_clients.device_headers, SESSION_ID
    if scenario == "account_cookie":
        client, headers = api_clients.account, None
    elif scenario == "anonymous":
        client, headers = api_clients.anonymous, None
    elif scenario == "other_session":
        session_id = OTHER_SESSION_ID
    elif scenario == "revoked":
        _update_capability(api_clients.engine, revoked_at=now - timedelta(seconds=1))
    elif scenario == "expired":
        _update_capability(api_clients.engine, expires_at=now - timedelta(seconds=1))
    elif scenario == "recovery_only":
        _update_capability(api_clients.engine, active_session_key=None,
                           recovery_only_at=now - timedelta(seconds=1))
    elif scenario == "active_slot_released":
        # 中间件只看 recovery_only_at;槽位已释放要靠处理器在锁内复核才拦得住。
        _update_capability(api_clients.engine, active_session_key=None)
    elif scenario == "live_switched":
        _switch_live_session(api_clients.engine, OTHER_SESSION_ID)
    else:
        monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE")
    before = _ledger(api_clients.engine)
    voice = _voice(client, headers, session_id)
    assert (voice.status_code, _response_code(voice)) == (status, code), voice.text
    assert "vad_enabled" not in voice.text
    assert _ledger(api_clients.engine) == before
    next_command = client.get(f"/sessions/{session_id}/autopilot/next", headers=headers or {})
    assert (next_command.status_code, _response_code(next_command)) == (status, code)
