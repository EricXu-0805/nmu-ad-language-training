from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlmodel import create_engine as real_create_engine

from app import db
from app.db import make_engine
from app.storage_security import ensure_private_directory as real_private_directory
from app.storage_security import ensure_private_file as real_private_file


def test_sqlite_engine_enables_foreign_keys_and_busy_timeout():
    eng = make_engine("sqlite://")
    with eng.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000


def test_explicit_database_url_never_touches_unused_default_path(tmp_path, monkeypatch):
    """An isolated run must secure only its selected SQLite file.

    Both paths live under pytest's temporary root so this regression can prove
    the unused default receives no permission helper call or mode change without
    consulting the repository's real ``data/app.db``.
    """
    selected_dir = tmp_path / "selected"
    selected_dir.mkdir(mode=0o700)
    selected = selected_dir / "e2e.sqlite"
    selected.write_bytes(b"")
    selected.chmod(0o644)

    unused_default_dir = tmp_path / "unused-default"
    unused_default_dir.mkdir(mode=0o700)
    unused_default = unused_default_dir / "app.db"
    unused_default.write_bytes(b"sentinel")
    unused_default.chmod(0o644)

    monkeypatch.setattr(db, "_DATA", unused_default_dir)
    monkeypatch.setattr(db, "_DEFAULT_DB_FILE", unused_default)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{selected}")
    calls: list[tuple[str, Path]] = []

    def record_directory(path: Path) -> Path:
        calls.append(("directory", Path(path)))
        return real_private_directory(Path(path))

    def record_file(path: Path) -> Path:
        calls.append(("file", Path(path)))
        return real_private_file(Path(path))

    monkeypatch.setattr(db, "ensure_private_directory", record_directory)
    monkeypatch.setattr(db, "ensure_private_file", record_file)

    eng = db.make_engine()
    eng.dispose()

    assert calls == [("file", selected)]
    assert selected.stat().st_mode & 0o777 == 0o600
    assert unused_default.read_bytes() == b"sentinel"
    assert unused_default.stat().st_mode & 0o777 == 0o644


def test_default_database_keeps_private_directory_and_file_behavior(tmp_path, monkeypatch):
    default_dir = tmp_path / "default-data"
    default = default_dir / "app.db"
    default_dir.mkdir(mode=0o755)
    default.write_bytes(b"")
    default.chmod(0o644)

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "_DATA", default_dir)
    monkeypatch.setattr(db, "_DEFAULT_DB_FILE", default)
    monkeypatch.setattr(db, "DEFAULT_URL", f"sqlite:///{default}")

    eng = db.make_engine()
    eng.dispose()

    assert default_dir.stat().st_mode & 0o777 == 0o700
    assert default.stat().st_mode & 0o777 == 0o600


def test_non_sqlite_and_memory_urls_never_invoke_file_security(monkeypatch):
    calls: list[Path] = []
    monkeypatch.setattr(db, "ensure_private_directory", lambda path: calls.append(Path(path)))
    monkeypatch.setattr(db, "ensure_private_file", lambda path: calls.append(Path(path)))

    created: list[tuple[str, dict]] = []

    class DummyEngine:
        pass

    def fake_create_engine(url: str, **kwargs):
        created.append((url, kwargs))
        return DummyEngine()

    monkeypatch.setattr(db, "create_engine", fake_create_engine)
    postgres = "postgresql+psycopg://user:pass@127.0.0.1/dbname"
    assert isinstance(db.make_engine(postgres), DummyEngine)
    assert calls == []
    assert created == [(postgres, {"echo": False, "hide_parameters": True})]
    # For an in-memory SQLite URL the SQLite connection guards still apply, but
    # there is no filesystem target to secure.
    monkeypatch.setattr(db, "create_engine", real_create_engine)
    memory = db.make_engine("sqlite://")
    try:
        with memory.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    finally:
        memory.dispose()
    assert calls == []


def test_database_engine_hides_bound_parameters_from_error_logs():
    engine = make_engine("sqlite://")
    try:
        assert engine.hide_parameters is True
    finally:
        engine.dispose()


def test_alembic_uses_database_url_when_ini_is_blank(tmp_path, monkeypatch):
    """启动脚本的 alembic 与应用必须连同一个部署库。"""
    db_path = tmp_path / "deployment.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))

    command.upgrade(config, "head")

    assert db_path.exists()
    eng = make_engine(f"sqlite:///{db_path}")
    tables = set(inspect(eng).get_table_names())
    assert {"alembic_version", "livestate", "sessionruntimestate"} <= tables
