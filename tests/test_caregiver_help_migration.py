"""Migration coverage for append-only caregiver help-request receipts."""
from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, inspect, text


GLOBAL_HEAD = "c5a8f2d91e40"
#: 本迁移自身的版本。它已不再是全局 head(求助处置转移在其上),所以下面一律
#: 显式升到 THIS 而不是 "head"——升到 head 会把后代表也建起来,再往下降时
#: 后代的 downgrade 先执行、先提交,schema 快照就对不上了。
THIS = "b3e7c5a9d214"
PARENT = "a9d2e6f4c108"


def _config(db_path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def _schema_rows(engine) -> tuple[tuple[object, ...], ...]:
    with engine.connect() as connection:
        return tuple(connection.execute(text(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )).all())


def test_caregiver_help_migration_is_single_head_and_roundtrips_empty_sqlite(
        tmp_path):
    db_path = tmp_path / "caregiver-help-clean.sqlite"
    config = _config(db_path)
    assert ScriptDirectory.from_config(config).get_heads() == [GLOBAL_HEAD]

    command.upgrade(config, THIS)
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "caregiverhelprequest" in inspector.get_table_names()
    names = {item["name"] for item in inspector.get_check_constraints(
        "caregiverhelprequest")}
    assert names == {
        "ck_caregiver_help_idempotency_hash",
        "ck_caregiver_help_reason_closed",
        "ck_caregiver_help_request_hash",
        "ck_caregiver_help_runtime_revision_nonnegative",
    }
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == THIS

    command.downgrade(config, PARENT)
    assert "caregiverhelprequest" not in inspect(engine).get_table_names()
    command.upgrade(config, THIS)
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == THIS


def test_caregiver_help_downgrade_refuses_evidence_before_any_ddl(tmp_path):
    db_path = tmp_path / "caregiver-help-evidence.sqlite"
    config = _config(db_path)
    command.upgrade(config, THIS)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(text(
            "INSERT INTO caregiverhelprequest "
            "(request_id, session_id, actor_id, reason_code, "
            "idempotency_key_sha256, request_hash, runtime_revision) "
            "VALUES ('CHR-MIGRATION', 'S-MIGRATION', 'ACTOR-MIGRATION', "
            "'technical_failure', :key, :request, 1)"
        ), {"key": "a" * 64, "request": "b" * 64})
    schema_before = _schema_rows(engine)

    with pytest.raises(RuntimeError, match="caregiver help request"):
        command.downgrade(config, PARENT)

    assert _schema_rows(engine) == schema_before
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == THIS
        assert connection.execute(text(
            "SELECT count(*) FROM caregiverhelprequest")).scalar_one() == 1
