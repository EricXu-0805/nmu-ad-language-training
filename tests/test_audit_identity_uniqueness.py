"""Fail-closed audit-identity uniqueness across model, CLI and migrations."""
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import auth
from app.models import ResearchUser
from scripts import manage_users


HEAD = "a9d2e6f4c108"
PREVIOUS_HEAD = "d8f2a6c9e104"


def _memory_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _migration_config(db_path: Path) -> Config:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def _insert_user(connection, username: str, display_id: str) -> None:
    connection.execute(text(
        "INSERT INTO researchuser "
        "(username, display_id, password_hash, role, disabled) "
        "VALUES (:username, :display_id, 'test-only-hash', 'researcher', 0)"
    ), {"username": username, "display_id": display_id})


def test_model_constraint_rejects_duplicate_audit_identity():
    engine = _memory_engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(ResearchUser(
            username="reader-a", display_id="AUDIT-ONE",
            password_hash="test-only-hash"))
        session.add(ResearchUser(
            username="reader-b", display_id="AUDIT-ONE",
            password_hash="test-only-hash"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_startup_preflight_rejects_duplicate_identity_in_legacy_schema(
        monkeypatch):
    engine = _memory_engine()
    # Deliberately reproduce a pre-migration database without the new unique
    # constraint.  ORM metadata must not hide the startup preflight behavior.
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE researchuser ("
            "username VARCHAR PRIMARY KEY, display_id VARCHAR NOT NULL, "
            "password_hash VARCHAR NOT NULL, role VARCHAR NOT NULL, "
            "disabled BOOLEAN NOT NULL, created_at DATETIME, last_login_at DATETIME)"
        ))
        _insert_user(connection, "legacy-a", "LEGACY-DUPLICATE")
        _insert_user(connection, "legacy-b", "LEGACY-DUPLICATE")
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    with Session(engine) as session:
        with pytest.raises(RuntimeError, match="display_id.*重复"):
            auth.assert_deploy_credentials(session)


def test_account_cli_rejects_duplicate_before_reading_password(monkeypatch):
    engine = _memory_engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(ResearchUser(
            username="existing", display_id="AUDIT-BOUND",
            password_hash="test-only-hash"))
        session.commit()

        def must_not_read_password(_args):
            raise AssertionError("duplicate identity must fail before password input")

        monkeypatch.setattr(manage_users, "_read_password", must_not_read_password)
        args = SimpleNamespace(
            username="new-account", display_id="AUDIT-BOUND",
            role="researcher", password_stdin=False,
        )
        with pytest.raises(SystemExit, match="display_id.*已绑定"):
            manage_users.cmd_create(session, args)


@pytest.mark.parametrize("display_id", ["", "   ", " leading", "trailing ", "bad\nline"])
def test_account_cli_rejects_ambiguous_display_id_before_password(
        monkeypatch, display_id):
    engine = _memory_engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        monkeypatch.setattr(
            manage_users, "_read_password",
            lambda _args: (_ for _ in ()).throw(
                AssertionError("invalid identity must fail before password input")),
        )
        args = SimpleNamespace(
            username="new-account", display_id=display_id,
            role="researcher", password_stdin=False,
        )
        with pytest.raises(SystemExit, match="display_id"):
            manage_users.cmd_create(session, args)


def test_sqlite_forward_migration_blocks_duplicates_and_roundtrips(tmp_path):
    db_path = tmp_path / "audit-identity.sqlite"
    config = _migration_config(db_path)
    command.upgrade(config, PREVIOUS_HEAD)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _insert_user(connection, "unique-a", "AUDIT-A")
        _insert_user(connection, "unique-b", "AUDIT-B")

    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == HEAD
    assert {constraint["name"] for constraint in
            inspect(engine).get_unique_constraints("researchuser")} >= {
                "uq_research_user_display_id",
            }
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            _insert_user(connection, "unique-c", "AUDIT-A")
    command.check(config)
    command.downgrade(config, PREVIOUS_HEAD)
    assert "uq_research_user_display_id" not in {
        constraint["name"] for constraint in
        inspect(engine).get_unique_constraints("researchuser")
    }
    with engine.begin() as connection:
        _insert_user(connection, "legacy-duplicate", "AUDIT-A")

    with pytest.raises(RuntimeError, match="1 组冲突.*2 个账号"):
        command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == PREVIOUS_HEAD
        assert connection.execute(text(
            "SELECT COUNT(*) FROM researchuser")).scalar_one() == 3
    assert "uq_research_user_display_id" not in {
        constraint["name"] for constraint in
        inspect(engine).get_unique_constraints("researchuser")
    }

    with engine.begin() as connection:
        connection.execute(text(
            "DELETE FROM researchuser WHERE username='legacy-duplicate'"))
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == HEAD
    command.check(config)


def test_unique_constraint_ddl_compiles_for_postgresql_without_network():
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    operations = Operations(context)
    with operations.batch_alter_table("researchuser") as batch_op:
        batch_op.create_unique_constraint(
            "uq_research_user_display_id", ["display_id"])
    assert (
        "ALTER TABLE researchuser ADD CONSTRAINT "
        "uq_research_user_display_id UNIQUE (display_id)"
    ) in output.getvalue()
