"""本机 WebKit 合成交接走查的判定与回执（不起浏览器；真跑见 harness/webkit_handoff.py）。"""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

from harness import webkit_handoff as wk


ROOT = Path(__file__).resolve().parents[1]


def _passing_result() -> dict:
    # 2026-10-23 WebKit 持久会话全流程通过时的结果形状。
    return {
        "syntheticOnly": True,
        "browser": "AppleWebKit/605.1.15 Version/26.6 Safari/605.1.15",
        "handoffMs": 54,
        "events": list(wk.EXPECTED_EVENTS),
        "nativeBytes": 4498,
        "receipt": 77,
        "foreignBytesAndMetadataUnchanged": True,
        "remainingEntries": 1,
        "tracksEnded": True,
    }


def _receipt(**overrides) -> dict:
    values = {
        "handoff": _passing_result(), "handoff_error": None,
        "nonpersistent_probe": None, "persistent_probe": None,
        "browser_version": "26.4", "playwright_version": "1.60.0",
        "source": {"git_revision": "0" * 40, "web_src_or_harness_dirty": False},
        "page_errors": [],
    }
    values.update(overrides)
    return wk.build_receipt(**values)


def test_the_10_23_passing_shape_passes():
    assert wk.handoff_failures(_passing_result()) == []


def test_baseline_drain_that_skips_the_audio_saved_probe_fails():
    # 10-23 基线外场清理：上传 404 就放弃，不再问 audioSaved。其余不变量照样成立，
    # 只有事件顺序能把它认出来。
    result = _passing_result()
    result["events"] = [e for e in wk.EXPECTED_EVENTS if e != "foreign-refused-kept"]

    assert wk.handoff_failures(result) == ["event_sequence_mismatch"]


@pytest.mark.parametrize(("key", "value", "failure"), [
    ("nativeBytes", 0, "new_recording_has_no_bytes"),
    ("nativeBytes", True, "new_recording_has_no_bytes"),
    ("receipt", None, "audio_receipt_server_seq_missing"),
    ("foreignBytesAndMetadataUnchanged", False, "old_entry_changed"),
    ("remainingEntries", 2, "outbox_entry_count_mismatch"),
    ("tracksEnded", False, "tracks_not_ended"),
    ("handoffMs", 20_000, "handoff_too_slow"),
    ("syntheticOnly", None, "synthetic_only_flag_missing"),
])
def test_each_handoff_invariant_is_checked(key, value, failure):
    result = _passing_result()
    result[key] = value

    assert wk.handoff_failures(result) == [failure]


def test_a_thrown_handoff_fails_and_keeps_the_scope_flags():
    receipt = _receipt(handoff=None, handoff_error="AutopilotMediaError: 麦克风权限复核晚于录音截止时刻")

    assert receipt["status"] == "failed"
    assert receipt["failures"] == ["handoff_threw:AutopilotMediaError: 麦克风权限复核晚于录音截止时刻"]
    assert receipt["synthetic_only"] is True
    assert receipt["actual_iOS_or_WeChat_device_test"] is False
    assert receipt["real_microphone_permission_or_hardware_test"] is False
    assert receipt["production_training_test"] is False


def test_storage_probe_is_an_observation_not_a_pass_or_fail():
    nonpersistent = {"indexedDB": [{"kind": "blob-webm", "put": False, "error": "UnknownError"}]}

    receipt = _receipt(nonpersistent_probe=nonpersistent)

    assert receipt["status"] == "passed"
    assert receipt["nonpersistent_storage_observation"] == nonpersistent


def test_page_and_runner_agree_on_the_isolated_origin():
    page = (ROOT / "web/harness/webkitHandoff.tsx").read_text(encoding="utf-8")

    assert f'const ORIGIN = "{wk.ORIGIN}";' in page
    assert page.count("if (location.origin !== ORIGIN)") == 2
    for event in wk.EXPECTED_EVENTS:
        assert f'"{event}"' in page


def test_harness_page_stays_out_of_the_production_build_inputs():
    app_config = (ROOT / "web/tsconfig.app.json").read_text(encoding="utf-8")
    production_entry = (ROOT / "web/index.html").read_text(encoding="utf-8")

    assert '"include": ["src"]' in app_config
    assert "harness" not in production_entry


def test_receipt_writer_needs_a_private_directory_and_never_overwrites(tmp_path):
    private = tmp_path / "receipts"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    target = private / "webkit-handoff.json"

    assert wk.write_receipt(target, _receipt()) == target
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        wk.write_receipt(target, _receipt())

    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    with pytest.raises(wk.HandoffHarnessError):
        wk.write_receipt(shared / "webkit-handoff.json", _receipt())


def test_page_build_failure_is_not_reported_as_missing_environment(tmp_path, monkeypatch, capsys):
    # web/harness 不进 tsc -b；web/src 改名导出后最先在这里断，不能读成「环境不满足」。
    vite = tmp_path / "node_modules/.bin/vite"
    vite.parent.mkdir(parents=True)
    vite.touch()
    monkeypatch.setattr(wk, "WEB", tmp_path)
    monkeypatch.setattr(wk.subprocess, "run", lambda *_a, **_k: wk.subprocess.CompletedProcess(
        [], 1, "", '"playOnce" is not exported by "src/audio/player.ts"'))
    with pytest.raises(wk.HandoffPageBuildError):
        wk._build_site(tmp_path / "site")

    def broken(_root):
        raise wk.HandoffPageBuildError('"playOnce" is not exported by "src/audio/player.ts"')
    monkeypatch.setattr(wk, "run", broken)

    assert wk.main([]) == 1
    err = capsys.readouterr().err
    assert "不是环境问题" in err
    assert "环境不满足" not in err


def test_receipt_write_failure_after_a_finished_run_says_so(tmp_path, monkeypatch, capsys):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    monkeypatch.setattr(wk, "run", lambda _root: _receipt())

    assert wk.main(["--receipt", str(shared / "webkit-handoff.json")]) == 2
    out, err = capsys.readouterr()
    assert '"status": "passed"' in out
    assert "回执没写成" in err
    assert "环境不满足" not in err
