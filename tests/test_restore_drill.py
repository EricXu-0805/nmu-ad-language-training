from __future__ import annotations

import importlib.util
import hashlib
from pathlib import Path
import shutil
import sqlite3
import sys

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "restore_drill",
    Path(__file__).resolve().parents[1] / "scripts" / "restore_drill.py")
assert _SPEC is not None and _SPEC.loader is not None
drill = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(drill)


def _restored(tmp_path: Path, *, head: str = "c8e5a1f3b209",
              heads: list[str] | None = None) -> Path:
    path = tmp_path / "app.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
    for value in (heads if heads is not None else [head]):
        connection.execute("INSERT INTO alembic_version VALUES (?)", (value,))
    for table in drill.CORE_TABLES:
        connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO patient VALUES (1)")
    connection.commit()
    connection.close()
    return path


def test_a_clean_restore_reports_head_and_row_counts(tmp_path):
    facts = drill._inspect(_restored(tmp_path))

    assert facts["alembic_head"] == "c8e5a1f3b209"
    assert facts["row_counts"]["patient"] == 1
    assert facts["row_counts"]["session"] == 0


def test_a_missing_core_table_fails_the_restore(tmp_path):
    path = _restored(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE auditlog")
    connection.commit()
    connection.close()

    with pytest.raises(drill.DrillFailure, match="核心表"):
        drill._inspect(path)


def test_two_alembic_rows_fail_the_drill(tmp_path):
    # 多头的库不能拿来恢复：不知道该配哪个版本的应用。
    with pytest.raises(drill.DrillFailure) as excinfo:
        drill._inspect(_restored(tmp_path, heads=["a" * 12, "b" * 12]))

    assert "单一值" in str(excinfo.value)


def test_a_foreign_key_violation_fails_the_drill(tmp_path):
    path = tmp_path / "app.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
    connection.execute("INSERT INTO alembic_version VALUES ('c8e5a1f3b209')")
    connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    connection.execute(
        "CREATE TABLE child (id INTEGER PRIMARY KEY, "
        "parent_id INTEGER REFERENCES parent(id))")
    connection.execute("INSERT INTO child VALUES (1, 999)")
    connection.commit()
    connection.close()

    with pytest.raises(drill.DrillFailure) as excinfo:
        drill._inspect(path)

    assert "外键违例" in str(excinfo.value)


def test_a_leftover_sqlite_sidecar_fails_the_drill(tmp_path):
    # 收尾后还留着 -wal/-shm，说明有连接没干净关。换台机器打开这份副本时，
    # 最后一段还在 WAL 里的写入就可能对不上——这正是"恢复出来少了几分钟数据"
    # 的经典形态，光看 integrity_check 是绿的。
    path = _restored(tmp_path)
    Path(str(path) + "-wal").write_bytes(b"")

    with pytest.raises(drill.DrillFailure) as excinfo:
        drill._inspect(path)

    assert "-wal" in str(excinfo.value)


def test_materialize_copies_audio_when_the_snapshot_has_it(tmp_path):
    snapshot = tmp_path / "snap"
    (snapshot / "audio").mkdir(parents=True)
    (snapshot / "audio" / "a.webm").write_bytes(b"x")
    _restored(snapshot)
    work = tmp_path / "work"
    work.mkdir()

    restored = drill._materialize(snapshot, work)

    assert restored.is_file()
    assert (work / "audio" / "a.webm").read_bytes() == b"x"


def test_a_snapshot_without_a_database_fails_before_anything_else(tmp_path):
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    work = tmp_path / "work"
    work.mkdir()

    with pytest.raises(drill.DrillFailure) as excinfo:
        drill._materialize(snapshot, work)

    assert "没有 app.db" in str(excinfo.value)


def test_a_failing_snapshot_verification_stops_the_drill(tmp_path, monkeypatch):
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    _restored(snapshot)

    def refuse(_snapshot, _python):
        raise drill.DrillFailure("快照校验未通过：code=alembic_revision_unsupported")

    monkeypatch.setattr(drill, "_verify_snapshot", refuse)

    with pytest.raises(drill.DrillFailure) as excinfo:
        drill.drill(snapshot, tmp_path, keep=False, python="python3")

    assert "alembic_revision_unsupported" in str(excinfo.value)


def test_the_work_directory_is_removed_unless_kept(tmp_path, monkeypatch):
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    _restored(snapshot)
    monkeypatch.setattr(drill, "_verify_snapshot", lambda *_: b"")
    monkeypatch.setattr(drill, "_verify_materialized", lambda *_, **__: "fixture")
    monkeypatch.setattr(drill, "_boot", lambda *_: "/health 200 stub")

    facts = drill.drill(snapshot, tmp_path, keep=False, python="python3")

    assert facts["work_dir"] == "(已清理)"
    assert not [child for child in tmp_path.iterdir()
                if child.name.startswith("nmu-restore-drill-")]


def test_keep_leaves_the_work_directory_for_forensics(tmp_path, monkeypatch):
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    _restored(snapshot)
    monkeypatch.setattr(drill, "_verify_snapshot", lambda *_: b"")
    monkeypatch.setattr(drill, "_verify_materialized", lambda *_, **__: "fixture")
    monkeypatch.setattr(drill, "_boot", lambda *_: "/health 200 stub")

    facts = drill.drill(snapshot, tmp_path, keep=True, python="python3")

    kept = Path(str(facts["work_dir"]))
    assert (kept / "app.db").is_file()
    assert kept.stat().st_mode & 0o777 == 0o700


def test_cli_reports_nonzero_on_a_missing_snapshot(tmp_path, capsys):
    code = drill.main([str(tmp_path / "nope")])

    assert code == 1
    assert "恢复演练失败" in capsys.readouterr().err


def _boot_database(tmp_path, *, has_user=True, disabled=False):
    from sqlmodel import Session, SQLModel, create_engine
    from app.models import ResearchUser
    from scripts.check_database_head import expected_head
    path = tmp_path / "boot.db"
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        if has_user:
            session.add(ResearchUser(username="restore-fixture", display_id="restore-fixture",
                                     password_hash="unused-fixture-password-hash",
                                     role="researcher", disabled=disabled))
            session.commit()
    engine.dispose()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
        connection.execute("INSERT INTO alembic_version VALUES (?)", (expected_head(),))
    return path


def test_boot_does_not_claim_success_for_a_nonexistent_database(tmp_path):
    missing = tmp_path / "never-created.db"
    with pytest.raises(drill.DrillFailure):
        drill._boot(missing, tmp_path)
    assert not missing.exists()


@pytest.mark.parametrize("has_user,disabled", [(False, False), (True, True)])
def test_real_lifespan_and_readiness_refuse_no_enabled_operator(tmp_path, has_user, disabled):
    path = _boot_database(tmp_path, has_user=has_user, disabled=disabled)
    with pytest.raises(drill.DrillFailure, match="启动或受保护"):
        drill._boot(path, tmp_path)


def test_real_boot_uses_restored_account_and_confines_all_writes(tmp_path, monkeypatch):
    path = _boot_database(tmp_path)
    # Host secrets/invalid live deployment flags must not enter the isolated child.
    monkeypatch.setenv("CONSOLE_PIN", "invalid-live-pin")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "never-forward-this-key")
    monkeypatch.setenv("DEVICE_CAPABILITY_TTL_MINUTES", "invalid-live-value")
    result = drill._boot(path, tmp_path)
    assert "lifespan 完成" in result
    assert "恢复账号读取 200" in result
    assert "核心行数一致" in result


def test_boot_refuses_archived_database_head_before_startup_can_repair_schema(tmp_path):
    path = _boot_database(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE alembic_version SET version_num='archived-head'")
    with pytest.raises(drill.DrillFailure):
        drill._boot(path, tmp_path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("archived-head",)


def test_copy_migration_requires_an_explicit_pinned_historical_verifier(tmp_path):
    with pytest.raises(drill.DrillFailure, match="历史校验器"):
        drill.drill(tmp_path, tmp_path, keep=False, python=sys.executable, migrate_copy=True)
    with pytest.raises(drill.DrillFailure, match="显式迁移"):
        drill.drill(tmp_path, tmp_path, keep=False, python=sys.executable,
                    source_verifier=tmp_path / "guard.py", source_verifier_sha256="a" * 64)


def test_a_wrong_historical_verifier_pin_fails_before_execution(tmp_path):
    guard = tmp_path / "guard.py"
    guard.write_text("raise AssertionError('must not run')\n")
    with pytest.raises(drill.DrillFailure, match="SHA256"):
        drill._verify_snapshot(tmp_path, sys.executable, source_verifier=guard,
                               source_verifier_sha256="a" * 64)


def _archived_database(directory):
    from alembic import command
    from alembic.config import Config
    from sqlmodel import Session, create_engine
    from app.models import ResearchUser
    directory.mkdir(mode=0o700)
    path = directory / "app.db"
    config = Config(str(drill.ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(drill.ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "d0c22a6dae2a")
    engine = create_engine(f"sqlite:///{path}")
    with Session(engine) as session:
        session.add(ResearchUser(username="restore-fixture", display_id="restore-fixture",
                                 password_hash="unused-fixture-password-hash",
                                 role="researcher", disabled=False))
        session.commit()
    engine.dispose()
    path.chmod(0o600)
    return path


def test_archived_copy_is_rejected_by_default_and_migrated_only_when_explicit(tmp_path):
    from scripts.check_database_head import expected_head
    source = _archived_database(tmp_path / "source")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    restored = work / "app.db"
    shutil.copy2(source, restored)
    with pytest.raises(drill.DrillFailure):
        drill._boot(restored, work)
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == before
    assert "lifespan 完成" in drill._boot(restored, work, migrate_copy=True)
    assert drill._inspect(restored)["alembic_head"] == expected_head()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def _snapshot_with_audio(tmp_path, *, archived):
    from alembic import command
    from alembic.config import Config
    from sqlmodel import Session, create_engine
    from app.models import ResearchUser
    from tests.test_backup_snapshot_integrity import _database, _manifest, _harden, _digest, _GUARD_MODULE
    snapshot = tmp_path / "snapshot"
    (snapshot / "audio").mkdir(parents=True)
    payload = b"authoritative synthetic audio"
    audio = snapshot / "audio" / "aud-normal.webm"
    audio.write_bytes(payload)
    _database(snapshot, [("aud-normal", "recorded", 0, None, _digest(payload), len(payload), "webm")])
    engine = create_engine(f"sqlite:///{snapshot / 'app.db'}")
    with Session(engine) as session:
        session.add(ResearchUser(username="restore-fixture", display_id="restore-fixture",
                                 password_hash="unused-fixture", role="researcher", disabled=False))
        session.commit()
    engine.dispose()
    kwargs = {}
    if archived:
        config = Config(str(drill.ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(drill.ROOT / "alembic"))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{snapshot / 'app.db'}")
        command.downgrade(config, "d0c22a6dae2a")
        # Synthetic archived verifier with the explicitly pinned historical
        # head/table/schema contract. All manifest/audio/export logic is real.
        source = (drill.ROOT / "scripts/verify_backup_snapshot.py").read_text()
        source = source.replace('frozenset({"e2a6d8f0b419"})', 'frozenset({"d0c22a6dae2a"})')
        source = source.replace('    "rapportplaybackreceipt",\n', '')
        source = source.replace(_GUARD_MODULE.CURRENT_RECOVERY_SCHEMA_SHA256,
                                _GUARD_MODULE.LEGACY_RECOVERY_SCHEMA_SHA256)
        guard = tmp_path / "synthetic-archived-verifier.py"
        guard.write_text(source)
        kwargs = {"migrate_copy": True, "source_verifier": guard,
                  "source_verifier_sha256": hashlib.sha256(guard.read_bytes()).hexdigest()}
    (snapshot / "config").mkdir()
    for name in _GUARD_MODULE.VPS_CONFIG_FILES:
        (snapshot / "config" / name).write_text("synthetic config\n")
    _manifest(snapshot)
    _harden(snapshot)
    return snapshot, audio, kwargs


@pytest.mark.parametrize("archived", [False, True])
def test_verified_payload_copy_really_boots_without_modifying_source(tmp_path, archived):
    from scripts.check_database_head import expected_head
    snapshot, _, kwargs = _snapshot_with_audio(tmp_path, archived=archived)
    before = {p.relative_to(snapshot): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in snapshot.rglob("*") if p.is_file()}
    facts = drill.drill(snapshot, tmp_path, keep=True, python=sys.executable, **kwargs)
    assert "lifespan 完成" in facts["boot"]
    assert facts["alembic_head"] == expected_head()
    assert facts["source_alembic_head"] == ("d0c22a6dae2a" if archived else expected_head())
    assert len(facts["source_manifest_sha256"]) == len(facts["verified_payload_manifest_sha256"]) == 64
    assert before == {p.relative_to(snapshot): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in snapshot.rglob("*") if p.is_file()}
    assert not (Path(facts["work_dir"]) / "config").exists()
    assert not (Path(facts["work_dir"]) / "MANIFEST.sha256").exists()


@pytest.mark.parametrize("archived", [False, True])
def test_source_audio_changed_after_real_source_verification_never_boots(tmp_path, monkeypatch, archived):
    snapshot, audio, kwargs = _snapshot_with_audio(tmp_path, archived=archived)
    original_verify = drill._verify_snapshot
    verified = []
    booted = []
    def verify_then_change(path, *args, **options):
        manifest = original_verify(path, *args, **options)
        if path == snapshot:
            verified.append(True)
            audio.write_bytes(b"changed after successful real source verification")
        return manifest
    monkeypatch.setattr(drill, "_verify_snapshot", verify_then_change)
    monkeypatch.setattr(drill, "_boot", lambda *_, **__: booted.append(True))
    with pytest.raises(drill.DrillFailure, match="manifest_hash_mismatch"):
        drill.drill(snapshot, tmp_path, keep=True, python=sys.executable, **kwargs)
    assert verified == [True]
    assert booted == []


def test_source_verifier_executes_pinned_bytes_when_archival_path_changes(tmp_path, monkeypatch):
    snapshot, _, _ = _snapshot_with_audio(tmp_path, archived=False)
    guard = tmp_path / "approved-verifier.py"
    approved = (drill.ROOT / "scripts/verify_backup_snapshot.py").read_bytes()
    guard.write_bytes(approved)
    digest = hashlib.sha256(approved).hexdigest()
    original_run = drill.subprocess.run
    executions = []
    def swap_original_then_execute(command, **kwargs):
        guard.write_text("raise RuntimeError('unapproved replacement must not execute')\n")
        executed = Path(command[3])
        executions.append(executed.read_bytes())
        assert executed != guard
        assert executed.stat().st_mode & 0o777 == 0o600
        assert executed.parent.stat().st_mode & 0o777 == 0o700
        return original_run(command, **kwargs)
    monkeypatch.setattr(drill.subprocess, "run", swap_original_then_execute)
    manifest = drill._verify_snapshot(snapshot, sys.executable,
                                      source_verifier=guard, source_verifier_sha256=digest)
    assert manifest == (snapshot / "MANIFEST.sha256").read_bytes()
    assert executions == [approved]
    assert hashlib.sha256(guard.read_bytes()).hexdigest() != digest
