"""Execute the entire rehearsal in a fresh child, never bind this process to its DB."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import shutil
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


def _verify_backup_and_restore(root: Path, work: Path) -> None:
    """Real export output must survive the unmodified local backup/restore path."""
    source = work / "source"
    scripts = source / "scripts"
    scripts.mkdir(parents=True)
    data = source / "data"
    data.mkdir()
    for name in ("backup.sh", "verify_backup_snapshot.py"):
        shutil.copy2(PLATFORM_ROOT / "scripts" / name, scripts / name)
    original = sqlite3.connect((root / "workflow.db").as_uri() + "?mode=ro", uri=True)
    try:
        copied = sqlite3.connect(data / "app.db")
        try:
            original.backup(copied)
        finally:
            copied.close()
    finally:
        original.close()
    trees = ("audio", "exports", "controlled-audio-exports")
    for name in trees:
        shutil.copytree(root / name, data / name)
    environment = _env() | {"PYTHON_BIN": sys.executable,
                            "DATABASE_URL": f"sqlite:///{data / 'app.db'}",
                            "BACKUP_DIR": str(work / "snapshots")}
    backed_up = subprocess.run(["bash", str(scripts / "backup.sh")], env=environment,
                               capture_output=True, text=True, timeout=90, check=False)
    assert backed_up.returncode == 0, backed_up.stdout + backed_up.stderr
    snapshots = list((work / "snapshots").iterdir())
    assert len(snapshots) == 1
    spec = importlib.util.spec_from_file_location(
        "workflow_restore_drill", PLATFORM_ROOT / "scripts" / "restore_drill.py")
    drill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drill)
    # backup.sh is a data-only local snapshot. Do not fabricate VPS configuration
    # to satisfy the stronger restore_drill CLI contract.
    manifest = drill._verify_snapshot(snapshots[0], sys.executable, require_vps_config=False)
    restored_root = work / "restored"
    restored_root.mkdir(mode=0o700)
    restored = drill._materialize(snapshots[0], restored_root)
    drill._verify_materialized(restored_root, manifest, sys.executable)
    before = drill._inspect(restored)
    drill._boot(restored, restored_root)
    assert drill._inspect(restored) == before
    for name in trees:
        expected = {p.relative_to(root / name): p.read_bytes()
                    for p in (root / name).rglob("*") if p.is_file()
                    and not (name == "audio" and p.name.startswith("."))}
        actual = {p.relative_to(restored_root / name): p.read_bytes()
                  for p in (restored_root / name).rglob("*") if p.is_file()}
        assert actual == expected
    with sqlite3.connect(restored) as database:
        assert database.execute("SELECT count(*) FROM exportbatch").fetchone()[0] == 1
        assert database.execute("SELECT count(*) FROM turnevent WHERE score_locked=1").fetchone()[0] == 2
        assert database.execute("SELECT count(*) FROM autopilotpositionadjudication").fetchone()[0] == 19


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
    _verify_backup_and_restore(root, tmp_path / "backup-restore")
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
