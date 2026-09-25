"""Execute the entire rehearsal in a fresh child, never bind this process to its DB."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from harness import isolated_workflow_rehearsal as rehearsal
from harness.tts_ack_harness import PLATFORM_ROOT


def _env() -> dict[str, str]:
    # Do not inherit production credentials, database locations, or provider configuration.
    return {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "en_US.UTF-8"}


def _run(root: Path, *options: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, *options, "-m", "harness.isolated_workflow_rehearsal",
                           "--root", str(root)], cwd=PLATFORM_ROOT, env=_env(),
                          capture_output=True, text=True, timeout=90, check=False)


def test_complete_simulation_start_at_19_through_reviewed_export(tmp_path):
    root = tmp_path / "isolated"
    root.mkdir(mode=0o700)
    result = _run(root)
    assert result.returncode == 0, result.stderr[-6000:]
    receipt = json.loads(result.stdout.splitlines()[-1])
    assert receipt == json.loads((root / "result.json").read_text())
    assert receipt["status"] == "passed"
    assert receipt["evidence_level"] == "isolated_http_testclient"
    assert receipt["browser_or_physical_tablet_validated"] is False
    assert receipt["clinical_approval_created"] is False
    assert receipt["actual_answer_positions"] == [19, 20]
    assert receipt["explicitly_skipped_positions"] == list(range(1, 19))
    assert receipt["locked_reviewed_turns"] == 2
    assert receipt["original_ai_answer_type"] == "沉默"
    assert receipt["raw_asr_preserved_after_adjudication"] is True
    assert receipt["export_replay_same_batch"] is True
    assert receipt["export_sheet_counts"]["adjudications"] == 19
    assert receipt["export_sheet_counts"]["turns"] == 2
    assert receipt["export_sheet_counts"]["attempts"] == 2
    assert receipt["completed_status"] == "completed"
    with sqlite3.connect(f"file:{root / 'workflow.db'}?mode=ro", uri=True) as db:
        assert db.execute("SELECT count(*) FROM exportbatch").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM turnevent WHERE score_locked=1").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM session WHERE is_simulation=0").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM audiocapturereceipt").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM turnconfirmationrevision").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM visitplancommand").fetchone()[0] == 3
    events = json.loads((root / "http-events.json").read_text())
    assert len(events) == receipt["http_request_count"]
    assert all(row["status"] == 200 for row in events)
    assert sum(row["path"].endswith("/export") for row in events) == 2
    assert any(row["path"].endswith("/autopilot/resume") for row in events)
    assert any(row["path"].endswith("/autopilot/adjudicate") for row in events)
    # Second invocation cannot silently reuse, overwrite or append to the first run.
    before = (root / "workflow.db").read_bytes()
    refused = _run(root)
    assert refused.returncode == 1 and "root must be empty" in refused.stderr
    assert (root / "workflow.db").read_bytes() == before


def test_embedded_audio_fixture_is_bounded_and_digest_pinned():
    payload = base64.b64decode(rehearsal.SYNTHETIC_WEBM_BASE64, validate=True)
    assert len(payload) == 2578
    assert payload.startswith(b"\x1a\x45\xdf\xa3")
    assert hashlib.sha256(payload).hexdigest() == rehearsal.SYNTHETIC_WEBM_SHA256


@pytest.mark.parametrize("unsafe", ["world-readable", "root-symlink", "audio-symlink", "preexisting-db"])
def test_unsafe_storage_refused_before_any_database_creation(tmp_path, unsafe):
    target = tmp_path / "external"
    target.mkdir(mode=0o700)
    sentinel = target / "sentinel.txt"
    sentinel.write_text("must remain unchanged")
    root = tmp_path / "rehearsal"
    if unsafe == "root-symlink":
        root.symlink_to(target, target_is_directory=True)
    else:
        root.mkdir(mode=0o700)
        if unsafe == "world-readable":
            root.chmod(0o755)
        elif unsafe == "audio-symlink":
            (root / "audio").symlink_to(target, target_is_directory=True)
        elif unsafe == "preexisting-db":
            (root / "workflow.db").write_bytes(b"pre-existing isolated database")
    result = _run(root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "INFO  [alembic" not in result.stderr
    assert sorted(path.name for path in target.iterdir()) == ["sentinel.txt"]
    assert sentinel.read_text() == "must remain unchanged"
    if unsafe == "preexisting-db":
        assert (root / "workflow.db").read_bytes() == b"pre-existing isolated database"
    else:
        assert not (root / "workflow.db").exists()


def test_rehearsal_refuses_preimported_app_module_without_touching_default_database(tmp_path):
    root = tmp_path / "preimport"
    root.mkdir(mode=0o700)
    code = """
import sys, types
from pathlib import Path
from harness.isolated_workflow_rehearsal import configure_private_root
from harness.tts_ack_harness import HarnessConfigError
sys.modules['app.tts'] = types.ModuleType('app.tts')
try:
    configure_private_root(Path(sys.argv[1]))
except HarnessConfigError as error:
    assert 'fresh process' in str(error)
else:
    raise AssertionError('preimported application state was accepted')
assert 'app.db' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", code, str(root)], cwd=PLATFORM_ROOT,
                            env=_env(), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert list(root.iterdir()) == []


def test_optimized_python_cannot_disable_rehearsal_assertions(tmp_path):
    root = tmp_path / "optimized"
    root.mkdir(mode=0o700)
    result = _run(root, "-O")
    assert result.returncode == 1 and "without -O" in result.stderr
    assert list(root.iterdir()) == []
