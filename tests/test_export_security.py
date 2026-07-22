import csv
import re
import stat
from datetime import datetime

import pytest

from app import export_security


def _config(key: str = "a" * 32, key_id: str = "test-key"):
    return export_security.load_deidentification_config({
        export_security.DEIDENTIFICATION_KEY_ENV: key,
        export_security.DEIDENTIFICATION_KEY_ID_ENV: key_id,
    })


def test_hmac_pseudonym_is_versioned_deterministic_and_keyed():
    config = _config()
    first = export_security.pseudonymize_subject("P-001", config)
    assert first == export_security.pseudonymize_subject("P-001", config)
    assert re.fullmatch(r"SUBJ-v1-test-key-[0-9a-f]{20}", first)
    assert first != export_security.pseudonymize_subject("P-002", config)
    assert first != export_security.pseudonymize_subject("P-001", _config("b" * 32))
    session_code = export_security.pseudonymize_session("P-001", config)
    audio_code = export_security.pseudonymize_audio("P-001", config)
    batch_code = export_security.pseudonymize_batch("P-001", config)
    assert re.fullmatch(r"SESS-v1-test-key-[0-9a-f]{20}", session_code)
    assert re.fullmatch(r"AUDIO-v1-test-key-[0-9a-f]{20}", audio_code)
    assert re.fullmatch(r"BATCH-v1-test-key-[0-9a-f]{20}", batch_code)
    assert len({first, session_code, audio_code, batch_code}) == 4  # 同原值也必须分域
    assert "a" * 32 not in repr(config)


@pytest.mark.parametrize("environment, message", [
    ({}, "DEIDENTIFICATION_KEY"),
    ({
        export_security.DEIDENTIFICATION_KEY_ENV: "short",
        export_security.DEIDENTIFICATION_KEY_ID_ENV: "test-key",
    }, "32 bytes"),
    ({
        export_security.DEIDENTIFICATION_KEY_ENV: "a" * 32,
    }, "DEIDENTIFICATION_KEY_ID"),
    ({
        export_security.DEIDENTIFICATION_KEY_ENV: "a" * 32,
        export_security.DEIDENTIFICATION_KEY_ID_ENV: "../escape",
    }, "DEIDENTIFICATION_KEY_ID"),
])
def test_deidentification_configuration_fails_closed(environment, message):
    with pytest.raises(
            export_security.DeidentificationConfigurationError,
            match=re.escape(message)) as exc_info:
        export_security.load_deidentification_config(environment)
    secret = environment.get(export_security.DEIDENTIFICATION_KEY_ENV)
    if secret:
        assert secret not in str(exc_info.value)


@pytest.mark.parametrize("value, expected", [
    ("=1+1", "'=1+1"),
    (" \t=HYPERLINK(\"https://invalid\")", "'=HYPERLINK(\"https://invalid\")"),
    ("\x00\u200b@SUM(A1:A2)", "'@SUM(A1:A2)"),
    ("\r\n+cmd", "'+cmd"),
    ("  -1", "'-1"),
    ("  ordinary", "ordinary"),
    ("'already-text", "'already-text"),
    (42, 42),
])
def test_csv_formula_sanitizer_handles_leading_ignorable_characters(value, expected):
    assert export_security.sanitize_csv_cell(value) == expected


def test_atomic_csv_sanitizes_every_string_cell_and_is_owner_only(tmp_path):
    base = export_security.ensure_secure_directory(
        tmp_path / "exports", "research", "EXP-test")
    target = base / "sheet.csv"
    export_security.atomic_write_csv(
        target,
        ["=dangerous_header", "plain"],
        [{"=dangerous_header": " \x00=2+2", "plain": "\u200b@payload"}],
    )

    with target.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.reader(stream))
    assert rows == [["'=dangerous_header", "plain"], ["'=2+2", "'@payload"]]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(base.stat().st_mode) == 0o700
    assert not list(base.glob("*.tmp"))


def test_secure_directories_reject_escape_and_symlinks(tmp_path):
    root = tmp_path / "exports"
    export_security.ensure_secure_directory(root)
    with pytest.raises(ValueError, match="路径跳转"):
        export_security.ensure_secure_directory(root, "..")

    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "research").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="符号链接"):
        export_security.ensure_secure_directory(root, "research", "EXP-test")
    assert list(outside.iterdir()) == []


def test_atomic_csv_rejects_symlink_target_without_touching_outside(tmp_path):
    base = export_security.ensure_secure_directory(tmp_path / "exports", "research")
    outside = tmp_path / "outside.csv"
    outside.write_text("do-not-touch", encoding="utf-8")
    target = base / "sheet.csv"
    target.symlink_to(outside)

    with pytest.raises(FileExistsError, match="符号链接"):
        export_security.atomic_write_csv(target, ["value"], [{"value": "safe"}])
    assert outside.read_text(encoding="utf-8") == "do-not-touch"


def test_atomic_csv_does_not_clobber_target_created_during_publish(
        tmp_path, monkeypatch):
    base = export_security.ensure_secure_directory(tmp_path / "exports", "research")
    outside = tmp_path / "outside.csv"
    outside.write_text("do-not-touch", encoding="utf-8")
    target = base / "sheet.csv"
    real_link = export_security.os.link

    def race_link(source, destination, **kwargs):
        target.symlink_to(outside)
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(export_security.os, "link", race_link)
    with pytest.raises(FileExistsError, match="写入期间被占用"):
        export_security.atomic_write_csv(target, ["value"], [{"value": "safe"}])
    assert outside.read_text(encoding="utf-8") == "do-not-touch"
    assert target.is_symlink()
    assert not list(base.glob("*.tmp"))


def test_atomic_audio_copy_is_owner_only_and_rejects_symlinks(tmp_path):
    source = tmp_path / "source.wav"
    source.write_bytes(b"voice-bytes")
    destination_dir = export_security.ensure_secure_directory(
        tmp_path / "controlled", "research", "EXP-test", "audio")
    destination = destination_dir / "copy.wav"

    export_security.atomic_copy_file(source, destination)
    assert destination.read_bytes() == b"voice-bytes"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600

    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"outside")
    linked_destination = destination_dir / "linked.wav"
    linked_destination.symlink_to(outside)
    with pytest.raises(FileExistsError, match="符号链接"):
        export_security.atomic_copy_file(source, linked_destination)
    assert outside.read_bytes() == b"outside"

    linked_source = tmp_path / "linked-source.wav"
    linked_source.symlink_to(source)
    with pytest.raises(RuntimeError, match="源文件"):
        export_security.atomic_copy_file(linked_source, destination_dir / "other.wav")


def test_deidentified_sheet_guard_rejects_identifiers_and_unreviewed_text():
    with pytest.raises(RuntimeError, match="直接标识列"):
        export_security.assert_deidentified_sheets({
            "session": [{"patient_id": "P-001"}],
        })
    with pytest.raises(RuntimeError, match="PHI 审核"):
        export_security.assert_deidentified_sheets({
            "turns": [{"asr_text": "我叫张三"}],
        })
    for forbidden_column in (
            "session_id", "raw_audio_id", "source_attempt_id", "attempt_id"):
        with pytest.raises(RuntimeError, match="直接标识列"):
            export_security.assert_deidentified_sheets({
                "unsafe": [{forbidden_column: "internal-value"}],
            })
    with pytest.raises(RuntimeError, match="payload_json.*可链接标识"):
        export_security.assert_deidentified_sheets({
            "interactions": [{"payload_json": '{"raw_audio_id":"internal"}'}],
        })
    with pytest.raises(RuntimeError, match="payload_json.*绝对日期/时间"):
        export_security.assert_deidentified_sheets({
            "interactions": [{
                "payload_json": '{"processed_at":"2026-07-18T12:30:00Z"}',
            }],
        })
    with pytest.raises(RuntimeError, match="绝对时间列"):
        export_security.assert_deidentified_sheets({
            "attempts": [{"created_at": None}],
        })
    with pytest.raises(RuntimeError, match="绝对日期/时间值"):
        export_security.assert_deidentified_sheets({
            "attempts": [{"value": datetime(2026, 7, 18, 12, 30)}],
        })
    with pytest.raises(RuntimeError, match="绝对日期/时间值"):
        export_security.assert_deidentified_sheets({
            "attempts": [{"value": "2026-07-18T12:30:00+08:00"}],
        })
    export_security.assert_deidentified_sheets({
        "turns": [{"asr_text": export_security.REDACTED_TEXT,
                   "confirmed_response_text": None}],
    })


def test_safe_cleanup_never_follows_an_invalid_root_symlink(tmp_path):
    outside = tmp_path / "outside"
    batch = outside / "research" / "EXP-test"
    batch.mkdir(parents=True)
    marker = batch / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    root_link = tmp_path / "exports-link"
    root_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="清理根目录"):
        export_security.safe_remove_tree(
            root_link / "research" / "EXP-test", root_link)
    assert marker.read_text(encoding="utf-8") == "keep"
