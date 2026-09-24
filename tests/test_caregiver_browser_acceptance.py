"""Pure contract tests for the optional real-Chrome caregiver acceptance."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

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
        help_request_id="CHR-BROWSER-1",
        help_states_walked=True,
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


def test_result_receipt_requires_help_states_evidence(tmp_path):
    """求助四态没走完的收据不许被后续核验当成通过。

    这一条与上面那条同形：收据里少一样证据，就不该有人能拿它去过账本核验。
    """
    root = _root(tmp_path)
    config = browser.resolve_browser_config("http://127.0.0.1:8815", _env(root))
    browser._write_result_receipt(config, _result())
    target = root / browser.RESULT_RECEIPT_NAME
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["help_states_walked"] = False
    target.write_text(json.dumps(payload), encoding="utf-8")
    target.chmod(0o600)
    with pytest.raises(browser.BrowserAcceptanceError, match="求助四态"):
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
        "_require_completed_attempt_review(attempt, answer_now=answer_now)",
        "_interrupted_browser_judgement_is_legal(session, attempt)",
        "autopilot_ledger.verify_interrupted_tts_ack(",
        "autopilot_service._require_completed_operational_attempt(",
    ):
        assert required in source


@pytest.mark.parametrize("answer_now", [False, True])
def test_completed_attempt_requires_the_exact_playback_review_flag(answer_now):
    browser._require_completed_attempt_review(SimpleNamespace(
        processing_status="completed", operational_needs_review=answer_now,
    ), answer_now=answer_now)
    for wrong_flag in (not answer_now, None, int(answer_now), str(answer_now)):
        with pytest.raises(browser.BrowserAcceptanceError, match="复核标记"):
            browser._require_completed_attempt_review(SimpleNamespace(
                processing_status="completed", operational_needs_review=wrong_flag,
            ), answer_now=answer_now)


@pytest.mark.parametrize("answer_now", [False, True])
@pytest.mark.parametrize("status", [
    None, "pending", "processing", "failed", "abandoned",
    "received", "asr_completed", "technical_failure",
])
def test_review_flag_cannot_substitute_for_completed_recognition_and_judgment(answer_now, status):
    with pytest.raises(browser.BrowserAcceptanceError, match="完成识别与自动判定"):
        browser._require_completed_attempt_review(SimpleNamespace(
            processing_status=status, operational_needs_review=answer_now,
        ), answer_now=answer_now)


def _interrupted_judgement_fixture():
    from app import evidence_ledger

    attempt = SimpleNamespace(
        id=1, session_id="SIM-REVIEW", item_id="itm-0001", turn_seq=1,
        attempt_seq=1, is_simulation=True, raw_audio_id="raw-synthetic-review",
        prompt_level=0, cue_type=None, duration_seconds=0.5,
        asr_text="螺母", asr_engine_version="harness-synthetic-asr/1", asr_confidence=1.0,
        operational_answer_type="正确", operational_score=1.0, operational_needs_review=True,
        judge_mode="规则确定式", judge_engine_version="rule-1", matched_on="target",
        judge_reason=None, judge_portrait_used=False, contains_target=True,
    )
    payloads = [
        ("attempt_received", {"raw_audio_id": attempt.raw_audio_id, "prompt_level": 0,
                              "cue_type": None, "duration_seconds": 0.5, "processing_status": "received"}),
        ("asr_completed", {"asr_engine_version": attempt.asr_engine_version,
                           "asr_confidence": 1.0, "degraded": False, "hotword_hit": True}),
        ("judgement_completed", {"answer_type": "正确", "score": 1.0, "needs_review": True,
                                 "judge_mode": "规则确定式", "judge_engine_version": "rule-1",
                                 "matched_on": "target", "contains_target": True,
                                 "truth_scope": "operational_only"}),
    ]
    rows = [SimpleNamespace(
        event_type=kind, item_id=attempt.item_id, turn_seq=1, attempt_seq=1,
        is_simulation=True, payload_json=evidence_ledger.encode_event_payload(kind, payload),
    ) for kind, payload in payloads]
    # Queries stay local; production legality and exact event-payload validators
    # run unchanged against the ordered synthetic result set.
    session = SimpleNamespace(exec=lambda _query: rows)
    return attempt, session, rows


def test_interrupted_judgement_keeps_true_review_on_the_actual_rows():
    attempt, session, rows = _interrupted_judgement_fixture()
    before = [row.payload_json for row in rows]
    assert browser._interrupted_browser_judgement_is_legal(session, attempt)
    assert attempt.operational_needs_review is True
    assert [row.payload_json for row in rows] == before


@pytest.mark.parametrize("fault", [
    "wrong_score", "wrong_classification", "missing_asr", "missing_judgement",
    "event_review_false", "attempt_review_false", "wrong_engine", "wrong_target_match",
])
def test_interrupted_review_cannot_bypass_rule_legality_or_exact_interactions(fault):
    from app import evidence_ledger

    attempt, session, rows = _interrupted_judgement_fixture()
    if fault == "wrong_score":
        attempt.operational_score = 0.5
    elif fault == "wrong_classification":
        attempt.operational_answer_type = "无效分类"
    elif fault == "missing_asr":
        rows.pop(1)
    elif fault == "missing_judgement":
        rows.pop()
    elif fault == "event_review_false":
        payload = json.loads(rows[-1].payload_json)
        payload["needs_review"] = False
        rows[-1].payload_json = evidence_ledger.encode_event_payload("judgement_completed", payload)
    elif fault == "attempt_review_false":
        attempt.operational_needs_review = False
    elif fault == "wrong_engine":
        attempt.judge_engine_version = "unverified-engine"
    elif fault == "wrong_target_match":
        attempt.contains_target = False
    assert not browser._interrupted_browser_judgement_is_legal(session, attempt)
