"""Pure contract tests for the optional real-Chrome caregiver acceptance."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harness import caregiver_browser_acceptance as browser


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "nmu-caregiver-demo20.contract"
    root.mkdir(mode=0o700)
    return root


def _env(root: Path) -> dict[str, str]:
    return {
        browser.CAREGIVER_USERNAME_ENV: "demo20-caregiver",
        browser.CAREGIVER_PASSWORD_ENV: "caregiver-browser-password",
        browser.CONSOLE_PIN_ENV: "24681357",
        browser.INSTANCE_MARKER_ENV: "caregiver-browser-instance-marker-000001",
        browser.HARNESS_ROOT_ENV: str(root),
    }


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:8815",
        "http://localhost:8815",
        "http://127.0.0.1:80",
        "http://user@127.0.0.1:8815",
        "http://127.0.0.1:8815/console",
        "http://127.0.0.1:8815?x=1",
    ],
)
def test_browser_config_accepts_only_exact_loopback_origin(tmp_path, origin):
    with pytest.raises(browser.BrowserAcceptanceError):
        browser.resolve_browser_config(origin, _env(_root(tmp_path)))


def test_browser_config_keeps_all_credentials_out_of_the_origin(tmp_path):
    root = _root(tmp_path)
    config = browser.resolve_browser_config("http://127.0.0.1:8815", _env(root))
    assert config.origin == "http://127.0.0.1:8815"
    assert config.harness_root == root.resolve()
    assert config.password not in config.origin
    assert config.pin not in config.origin


def test_expected_http_failures_are_phase_and_code_closed():
    expected = browser._expected_http_failure
    assert expected(
        "GET", "/auth/me", 401, "auth_me_not_logged_in",
        logged_in=False, autopilot_started=False, pause_requested=False,
    )
    assert not expected(
        "GET", "/auth/me", 401, "auth_me_not_logged_in",
        logged_in=True, autopilot_started=False, pause_requested=False,
    )
    assert expected(
        "GET", "/sessions/S1/autopilot/next", 409, "autopilot_not_active",
        logged_in=True, autopilot_started=False, pause_requested=False,
    )
    assert not expected(
        "GET", "/sessions/S1/autopilot/next", 409, "autopilot_not_active",
        logged_in=True, autopilot_started=True, pause_requested=False,
    )
    assert expected(
        "GET", "/sessions/S1/autopilot/next", 409, "autopilot_runtime_inactive",
        logged_in=True, autopilot_started=True, pause_requested=True,
    )
    assert not expected(
        "GET", "/live/state", 401, "device_pair_required",
        logged_in=False, autopilot_started=False, pause_requested=False,
    )


def _result() -> browser.BrowserResult:
    return browser.BrowserResult(
        plan_id="VP-BROWSER-1",
        session_id="S-BROWSER-1",
        first_tts_command_key="CMD-TTS-1",
        record_command_key="CMD-REC-1",
        next_command_key="CMD-TTS-2",
        next_command_seq=3,
        pause_runtime_revision=1,
        drain_command_key="CMD-TTS-2",
        drain_state_revision=5,
        native_audio_pause_observed=True,
    )


def test_result_receipt_is_exact_private_and_non_overwriting(tmp_path):
    root = _root(tmp_path)
    config = browser.resolve_browser_config("http://127.0.0.1:8815", _env(root))
    result = _result()
    browser._write_result_receipt(config, result)
    target = root / browser.RESULT_RECEIPT_NAME
    assert target.stat().st_mode & 0o777 == 0o600
    assert browser._read_result_receipt(root) == result
    with pytest.raises(browser.BrowserAcceptanceError, match="拒绝覆盖"):
        browser._write_result_receipt(config, result)


def test_result_receipt_rejects_extra_keys_and_non_private_mode(tmp_path):
    root = _root(tmp_path)
    config = browser.resolve_browser_config("http://127.0.0.1:8815", _env(root))
    browser._write_result_receipt(config, _result())
    target = root / browser.RESULT_RECEIPT_NAME
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["extra"] = True
    target.write_text(json.dumps(payload), encoding="utf-8")
    target.chmod(0o600)
    with pytest.raises(browser.BrowserAcceptanceError):
        browser._read_result_receipt(root)

    payload.pop("extra")
    target.write_text(json.dumps(payload), encoding="utf-8")
    target.chmod(0o644)
    with pytest.raises(browser.BrowserAcceptanceError):
        browser._read_result_receipt(root)


def test_result_receipt_rejects_symlink(tmp_path):
    root = _root(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (root / browser.RESULT_RECEIPT_NAME).symlink_to(outside)
    with pytest.raises(browser.BrowserAcceptanceError):
        browser._read_result_receipt(root)


def test_error_redaction_removes_all_ephemeral_secrets():
    message = browser._redacted_error(
        RuntimeError("password-1 pin-2 marker-3"),
        ("password-1", "pin-2", "marker-3"),
    )
    assert "password-1" not in message
    assert "pin-2" not in message
    assert "marker-3" not in message
    assert message.count("[已隐藏]") == 3


def test_result_receipt_reader_rejects_wrong_owner_when_supported(tmp_path, monkeypatch):
    root = _root(tmp_path)
    config = browser.resolve_browser_config("http://127.0.0.1:8815", _env(root))
    browser._write_result_receipt(config, _result())
    real_fstat = os.fstat

    def wrong_owner(fd: int):
        value = real_fstat(fd)
        fields = list(value)
        fields[4] = value.st_uid + 1
        return os.stat_result(fields)

    monkeypatch.setattr(browser.os, "fstat", wrong_owner)
    with pytest.raises(browser.BrowserAcceptanceError):
        browser._read_result_receipt(root)


class _JsonResponse:
    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


def test_stable_error_code_requires_the_exact_nested_envelope():
    valid = {"detail": {"code": "autopilot_not_active", "message": "尚未开始"}}
    assert browser._stable_error_code(_JsonResponse(valid)) == "autopilot_not_active"
    for invalid in (
        {"code": "autopilot_not_active", "message": "尚未开始"},
        {"detail": {"code": "autopilot_not_active"}},
        {"detail": {"code": "autopilot_not_active", "message": "尚未开始", "extra": 1}},
        {"detail": {"code": "autopilot_not_active", "message": " "}},
        {"detail": {"code": "autopilot_not_active", "message": "尚未开始"}, "requestId": "x"},
    ):
        assert browser._stable_error_code(_JsonResponse(invalid)) is None


def test_result_receipt_requires_native_audio_pause_evidence(tmp_path):
    root = _root(tmp_path)
    config = browser.resolve_browser_config("http://127.0.0.1:8815", _env(root))
    browser._write_result_receipt(config, _result())
    target = root / browser.RESULT_RECEIPT_NAME
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["native_audio_pause_observed"] = False
    target.write_text(json.dumps(payload), encoding="utf-8")
    target.chmod(0o600)
    with pytest.raises(browser.BrowserAcceptanceError, match="原生音频"):
        browser._read_result_receipt(root)


def test_ledger_verifier_reuses_production_authority_helpers():
    source = Path(browser.__file__).read_text(encoding="utf-8")
    for required in (
        "visit_plan_service.receipt_for(session, plan)",
        "visit_plan_service.assert_started_profile_command_chain(",
        "provider_readiness.capture_configuration(",
        "autopilot_service.legacy_expected_attempt_facts(",
        "autopilot_service.legacy_attempt_matches_expected_facts(",
        "autopilot_service.legacy_attempt_is_successfully_judged(",
        "asset.delete_gate_passed is not False",
        "status_receipt.takeover_ready is not True",
    ):
        assert required in source
