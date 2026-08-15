"""Migration coverage for one canonical frozen-plan item/turn row."""
from __future__ import annotations

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text


PARENT = "9f2c6a8d4e10"
HEAD = "c5a8f2d91e40"


def _config(db_path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def _unique_names(engine, table: str) -> set[str]:
    return {
        row["name"] for row in inspect(engine).get_unique_constraints(table)
        if row.get("name")
    }


def test_terminal_evidence_constraints_roundtrip_on_clean_sqlite(tmp_path):
    db_path = tmp_path / "terminal-evidence-clean.sqlite"
    config = _config(db_path)
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == HEAD
    assert "uq_item_event_session_item" in _unique_names(engine, "itemevent")
    assert "uq_turn_event_item_turn_seq" in _unique_names(engine, "turnevent")

    command.downgrade(config, PARENT)
    assert "uq_item_event_session_item" not in _unique_names(engine, "itemevent")
    assert "uq_turn_event_item_turn_seq" not in _unique_names(engine, "turnevent")

    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == HEAD
    assert "uq_item_event_session_item" in _unique_names(engine, "itemevent")
    assert "uq_turn_event_item_turn_seq" in _unique_names(engine, "turnevent")


@pytest.mark.parametrize(
    ("duplicate_sql", "expected_counts"),
    [
        (
            "INSERT INTO itemevent"
            "(session_id,item_id,task_type,item_set_type) VALUES"
            "('S-DUP','I-DUP','单要素','训练集'),"
            "('S-DUP','I-DUP','单要素','训练集')",
            (1, 0),
        ),
        (
            "INSERT INTO itemevent"
            "(id,session_id,item_id,task_type,item_set_type) VALUES"
            "(1,'S-DUP','I-DUP','单要素','训练集');"
            "INSERT INTO turnevent"
            "(item_event_id,turn_seq,response_role,judge_portrait_used,score_locked) "
            "VALUES(1,1,'命名',0,0),(1,1,'其他角色',0,0)",
            (0, 1),
        ),
    ],
)
def test_terminal_evidence_migration_preserves_and_blocks_ambiguous_rows(
        tmp_path, duplicate_sql, expected_counts):
    db_path = tmp_path / "terminal-evidence-duplicates.sqlite"
    config = _config(db_path)
    command.upgrade(config, PARENT)
    engine = create_engine(f"sqlite:///{db_path}")
    statements = [statement for statement in duplicate_sql.split(";") if statement]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))

    with pytest.raises(RuntimeError, match=(
            rf"ItemEvent {expected_counts[0]} 组，TurnEvent {expected_counts[1]} 组")):
        command.upgrade(config, "head")

    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == PARENT
        item_count = connection.execute(text(
            "SELECT COUNT(*) FROM itemevent")).scalar_one()
        turn_count = connection.execute(text(
            "SELECT COUNT(*) FROM turnevent")).scalar_one()
    assert item_count == 2 - expected_counts[1]
    assert turn_count == 2 * expected_counts[1]
