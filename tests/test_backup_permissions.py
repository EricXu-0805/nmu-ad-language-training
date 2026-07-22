from pathlib import Path
import hashlib
import importlib.util
import shutil
import sqlite3
import stat
import subprocess
import sys

from alembic import command
from alembic.config import Config
import pytest

from app import models as _models  # noqa: F401 - registers recovery schema


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "verify_backup_snapshot_permissions_test",
    ROOT / "scripts" / "verify_backup_snapshot.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_GUARD_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_GUARD_MODULE)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _copy_backup_scripts(scripts: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "scripts"
    shutil.copy2(source / "backup.sh", scripts / "backup.sh")
    shutil.copy2(
        source / "verify_backup_snapshot.py",
        scripts / "verify_backup_snapshot.py",
    )


def _create_audio_database(path: Path, raw_audio_id: str, payload: bytes) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO audioassetrow "
            "(raw_audio_id,status,withdrawn,withdrawal_status,checksum,byte_count,"
            "audio_format,is_simulation,data_classification,patient_turn_ref_version,"
            "is_reliability_sample,contains_direct_identifier,delete_gate_passed) "
            "VALUES (?,?,?,?,?,?,?,0,'research',2,0,0,0)",
            (raw_audio_id, "recorded", 0, None,
             hashlib.sha256(payload).hexdigest(), len(payload), "webm"),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize("unsafe_root", ["", "/", "/.."])
def test_local_backup_rejects_empty_or_filesystem_root_destination(
        unsafe_root, tmp_path):
    project = tmp_path / "platform"
    scripts = project / "scripts"
    data = project / "data"
    scripts.mkdir(parents=True)
    data.mkdir()
    _copy_backup_scripts(scripts)

    completed = subprocess.run(
        ["bash", str(scripts / "backup.sh")], cwd=project,
        env={
            "PATH": "/usr/bin:/bin",
            "BACKUP_DIR": unsafe_root,
            "PYTHON_BIN": sys.executable,
        },
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode != 0
    assert "备份根目录" in completed.stderr


def test_local_backup_defaults_to_owner_only_permissions(tmp_path):
    project = tmp_path / "platform"
    scripts = project / "scripts"
    audio = project / "data" / "audio"
    scripts.mkdir(parents=True)
    audio.mkdir(parents=True)
    _copy_backup_scripts(scripts)
    payload = b"test-audio"
    (audio / "sample.webm").write_bytes(payload)
    (audio / ".sample.upload.lock").write_bytes(b"")
    (audio / ".sample.ABC123.pending").write_bytes(b"in-flight")
    _create_audio_database(project / "data" / "app.db", "sample", payload)
    destination = tmp_path / "backup-destination"

    subprocess.run(
        ["bash", str(scripts / "backup.sh")],
        cwd=project,
        env={
            "PATH": "/usr/bin:/bin",
            "BACKUP_DIR": str(destination),
            "PYTHON_BIN": sys.executable,
        },
        check=True,
        capture_output=True,
        text=True,
    )

    snapshots = list(destination.iterdir())
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert _mode(destination) == 0o700
    assert _mode(snapshot) == 0o700
    for directory in (p for p in snapshot.rglob("*") if p.is_dir()):
        assert _mode(directory) == 0o700
    for file in (p for p in snapshot.rglob("*") if p.is_file()):
        assert _mode(file) == 0o600
    assert not any(path.name.startswith(".") for path in (snapshot / "audio").iterdir())


def test_live_sqlite_backup_is_consistent_without_copy_fallback(tmp_path):
    project = tmp_path / "platform"
    scripts = project / "scripts"
    data = project / "data"
    scripts.mkdir(parents=True)
    data.mkdir()
    _copy_backup_scripts(scripts)
    database = data / "app.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")
    live = sqlite3.connect(database)
    try:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute(
            "INSERT INTO patient(patient_id,is_simulation_subject,governance_revision) "
            "VALUES (?,0,0)",
            ("SYNTHETIC-BACKUP-PATIENT",),
        )
        live.commit()

        destination = tmp_path / "backup-destination"
        completed = subprocess.run(
            ["bash", str(scripts / "backup.sh")],
            cwd=project,
            env={
                "PATH": "/usr/bin:/bin",
                "BACKUP_DIR": str(destination),
                "PYTHON_BIN": sys.executable,
            },
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        live.close()

    assert "在线快照 + 完整 integrity_check" in completed.stdout
    snapshots = list(destination.iterdir())
    assert len(snapshots) == 1
    copied = sqlite3.connect(snapshots[0] / "app.db")
    try:
        assert copied.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert copied.execute(
            "SELECT patient_id FROM patient WHERE patient_id=?",
            ("SYNTHETIC-BACKUP-PATIENT",),
        ).fetchall() == [("SYNTHETIC-BACKUP-PATIENT",)]
    finally:
        copied.close()


def test_audio_copy_failure_never_leaks_research_identifier(tmp_path):
    project = tmp_path / "platform"
    scripts = project / "scripts"
    audio = project / "data" / "audio"
    stubs = tmp_path / "bin"
    scripts.mkdir(parents=True)
    audio.mkdir(parents=True)
    stubs.mkdir()
    _copy_backup_scripts(scripts)
    synthetic_id = "SYNTHETIC-PATIENT-ID-IN-CP-ERROR"
    payload = b"synthetic-audio"
    (audio / f"{synthetic_id}.webm").write_bytes(payload)
    _create_audio_database(
        project / "data" / "app.db", synthetic_id, payload,
    )
    cp_stub = stubs / "cp"
    cp_stub.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >&2\nexit 91\n",
        encoding="utf-8",
    )
    cp_stub.chmod(0o755)

    completed = subprocess.run(
        ["bash", str(scripts / "backup.sh")],
        cwd=project,
        env={
            "PATH": f"{stubs}:/usr/bin:/bin",
            "BACKUP_DIR": str(tmp_path / "backup-destination"),
            "PYTHON_BIN": sys.executable,
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert synthetic_id not in completed.stdout
    assert synthetic_id not in completed.stderr
    assert "code=audio_copy_failed" in completed.stderr


@pytest.mark.parametrize("export_root", ["exports", "controlled-audio-exports"])
def test_unscannable_export_root_fails_closed_without_path_leak(
        export_root, tmp_path):
    project = tmp_path / "platform"
    scripts = project / "scripts"
    audio = project / "data" / "audio"
    stubs = tmp_path / "bin"
    scripts.mkdir(parents=True)
    audio.mkdir(parents=True)
    stubs.mkdir()
    _copy_backup_scripts(scripts)
    payload = b"synthetic-audio"
    (audio / "sample.webm").write_bytes(payload)
    _create_audio_database(project / "data" / "app.db", "sample", payload)
    source = project / "data" / export_root
    source.mkdir()
    (source / "SYNTHETIC-INTERNAL-PATH.bin").write_bytes(b"evidence")
    find_stub = stubs / "find"
    find_stub.write_text(
        "#!/usr/bin/env bash\n"
        f"if [[ \"$*\" == *\"data/{export_root}\"* ]]; then exit 91; fi\n"
        "exec /usr/bin/find \"$@\"\n",
        encoding="utf-8",
    )
    find_stub.chmod(0o755)

    completed = subprocess.run(
        ["bash", str(scripts / "backup.sh")], cwd=project,
        env={
            "PATH": f"{stubs}:/usr/bin:/bin",
            "BACKUP_DIR": str(tmp_path / "backup-destination"),
            "PYTHON_BIN": sys.executable,
        },
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode != 0
    assert "code=export_scan_failed" in completed.stderr
    assert "SYNTHETIC-INTERNAL-PATH" not in completed.stdout
    assert "SYNTHETIC-INTERNAL-PATH" not in completed.stderr


@pytest.mark.parametrize("export_root", ["exports", "controlled-audio-exports"])
def test_nested_empty_export_directory_is_never_silently_published(
        export_root, tmp_path):
    project = tmp_path / "platform"
    scripts = project / "scripts"
    data = project / "data"
    scripts.mkdir(parents=True)
    (data / export_root / "analysis" / "EMPTY-BATCH").mkdir(parents=True)
    _copy_backup_scripts(scripts)
    _create_audio_database(data / "app.db", "unused", b"unused")
    connection = sqlite3.connect(data / "app.db")
    try:
        connection.execute("DELETE FROM audioassetrow")
        connection.commit()
    finally:
        connection.close()
    destination = tmp_path / "backup-destination"

    completed = subprocess.run(
        ["bash", str(scripts / "backup.sh")], cwd=project,
        env={
            "PATH": "/usr/bin:/bin",
            "BACKUP_DIR": str(destination),
            "PYTHON_BIN": sys.executable,
        },
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode != 0
    assert "code=export_empty_directory" in completed.stderr
    assert not any(
        child.is_dir() and not child.name.endswith(".failed")
        for child in destination.iterdir()
    )


@pytest.mark.parametrize("database_url", [
    "postgresql+psycopg://user:secret@example.invalid/nmu",
    "sqlite:///data/another.db",
])
def test_backup_rejects_noncanonical_active_database_even_with_stale_app_db(
        database_url, tmp_path):
    project = tmp_path / "platform"
    scripts = project / "scripts"
    data = project / "data"
    scripts.mkdir(parents=True)
    data.mkdir()
    _copy_backup_scripts(scripts)
    _create_audio_database(data / "app.db", "sample", b"stale")
    destination = tmp_path / "backup-destination"

    completed = subprocess.run(
        ["bash", str(scripts / "backup.sh")],
        cwd=project,
        env={
            "PATH": "/usr/bin:/bin",
            "BACKUP_DIR": str(destination),
            "PYTHON_BIN": sys.executable,
            "DATABASE_URL": database_url,
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert not destination.exists() or not any(
        child.name[:1].isdigit() and not child.name.endswith(".failed")
        for child in destination.iterdir()
    )
    assert "secret" not in completed.stdout
    assert "secret" not in completed.stderr


@pytest.mark.parametrize("assignment", [
    "DATABASE_URL = postgresql+psycopg://user:spaced-secret@example.invalid/nmu",
    "export DATABASE_URL = 'postgresql+psycopg://user:spaced-secret@example.invalid/nmu'",
])
def test_backup_rejects_spaced_dotenv_postgres_without_leaking_url(
        assignment, tmp_path):
    project = tmp_path / "platform"
    scripts = project / "scripts"
    data = project / "data"
    scripts.mkdir(parents=True)
    data.mkdir()
    _copy_backup_scripts(scripts)
    _create_audio_database(data / "app.db", "sample", b"stale")
    (project / ".env").write_text(f"{assignment}\n", encoding="utf-8")
    destination = tmp_path / "backup-destination"
    environment = {
        "PATH": "/usr/bin:/bin",
        "BACKUP_DIR": str(destination),
        "PYTHON_BIN": sys.executable,
    }
    # Be explicit: an ambient developer DATABASE_URL must not mask the fixture.
    assert "DATABASE_URL" not in environment

    completed = subprocess.run(
        ["bash", str(scripts / "backup.sh")], cwd=project, env=environment,
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode != 0
    assert "Postgres pg_dump/restore" in completed.stderr
    assert "spaced-secret" not in completed.stdout
    assert "spaced-secret" not in completed.stderr
    assert not destination.exists()


def test_backup_rejects_blank_process_database_url(tmp_path):
    project = tmp_path / "platform"
    scripts = project / "scripts"
    data = project / "data"
    scripts.mkdir(parents=True)
    data.mkdir()
    _copy_backup_scripts(scripts)
    _create_audio_database(data / "app.db", "sample", b"stale")

    completed = subprocess.run(
        ["bash", str(scripts / "backup.sh")], cwd=project,
        env={
            "PATH": "/usr/bin:/bin",
            "BACKUP_DIR": str(tmp_path / "backup-destination"),
            "PYTHON_BIN": sys.executable,
            "DATABASE_URL": "   ",
        },
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode != 0
    assert "无法安全解析数据库备份合同" in completed.stderr


def test_backup_rejects_missing_database_and_unknown_hidden_audio(tmp_path):
    project = tmp_path / "platform"
    scripts = project / "scripts"
    audio = project / "data" / "audio"
    scripts.mkdir(parents=True)
    audio.mkdir(parents=True)
    _copy_backup_scripts(scripts)
    destination = tmp_path / "backup-destination"
    base_env = {
        "PATH": "/usr/bin:/bin",
        "BACKUP_DIR": str(destination),
        "PYTHON_BIN": sys.executable,
    }

    missing = subprocess.run(
        ["bash", str(scripts / "backup.sh")], cwd=project, env=base_env,
        check=False, capture_output=True, text=True,
    )
    assert missing.returncode != 0

    payload = b"authoritative"
    _create_audio_database(project / "data" / "app.db", "sample", payload)
    (audio / "sample.webm").write_bytes(payload)
    (audio / ".unknown-hidden").write_bytes(b"unexpected")
    hidden = subprocess.run(
        ["bash", str(scripts / "backup.sh")], cwd=project, env=base_env,
        check=False, capture_output=True, text=True,
    )
    assert hidden.returncode != 0
    assert "unknown-hidden" not in hidden.stdout
    assert "unknown-hidden" not in hidden.stderr
