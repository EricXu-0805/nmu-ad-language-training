"""量表电子记录（原型道）迁移 b6d4f8a2c917 的 Alembic 验收。

上行：两表 + 精确命名的 CHECK/唯一约束；下行 fail-closed：任一表存在
一行量表证据即拒绝，schema 与版本号原样保留。
"""
from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


# 这个文件专测量表那条迁移，靶子就是它本身，不是全仓最新头。
# 直接 upgrade 到 "head" 会把后面的迁移也带上，于是「被拒绝的降级」
# 变得不原子：后一条先降完了，才轮到这一条拒绝。
HEAD = "b6d4f8a2c917"
REPO_HEAD = "d0c22a6dae2a"
PARENT = "6f2a9c4d8e17"


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


def _insert_record(connection, *, record_id: str = "QR-MIG-1",
                   status: str = "draft", phase_label: str = "前测",
                   ai_draft_status: str = "none",
                   locked_by: str | None = None,
                   locked_at: str | None = None) -> None:
    connection.execute(text(
        "INSERT INTO questionnairerecord "
        "(record_id, patient_id, questionnaire_id, definition_sha256, "
        "phase_label, status, created_by, created_at, locked_by, locked_at, "
        "ai_draft_status) VALUES "
        "(:record_id, 'P-MIG', 'gds15_v1', :sha, :phase_label, :status, "
        "'RESEARCH-A', '2026-08-20 00:00:00', :locked_by, :locked_at, "
        ":ai_draft_status)"
    ), {
        "record_id": record_id,
        "sha": "a" * 64,
        "phase_label": phase_label,
        "status": status,
        "locked_by": locked_by,
        "locked_at": locked_at,
        "ai_draft_status": ai_draft_status,
    })


def _insert_value(connection, *, record_id: str = "QR-MIG-1",
                  item_key: str = "gds_01", field_key: str = "value",
                  value_source: str | None = None) -> None:
    connection.execute(text(
        "INSERT INTO questionnaireitemvalue "
        "(record_id, item_key, field_key, value_source, updated_at) VALUES "
        "(:record_id, :item_key, :field_key, :value_source, "
        "'2026-08-20 00:00:00')"
    ), {
        "record_id": record_id,
        "item_key": item_key,
        "field_key": field_key,
        "value_source": value_source,
    })


def test_upgrade_creates_both_tables_with_exact_constraints_and_roundtrips(
        tmp_path):
    db_path = tmp_path / "questionnaire-clean.sqlite"
    config = _config(db_path)
    assert ScriptDirectory.from_config(config).get_heads() == [REPO_HEAD]

    command.upgrade(config, HEAD)
    # 这里不 check：库此刻停在中间那一版，而模型在最新头，两者本来就不一致。
    # 「模型与库没有漂移」那条断言挪到本用例末尾、走到最新头之后再做。
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "questionnairerecord" in tables
    assert "questionnaireitemvalue" in tables

    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints("questionnairerecord")
    } == {
        "ck_questionnaire_record_status",
        "ck_questionnaire_record_lock_complete",
        "ck_questionnaire_record_ai_draft_status",
        "ck_questionnaire_record_phase_label",
    }
    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints(
            "questionnaireitemvalue")
    } == {"ck_questionnaire_item_value_source"}
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            "questionnaireitemvalue")
    } == {"uq_questionnaire_item_value_slot"}
    assert {
        foreign_key["referred_table"]
        for foreign_key in inspector.get_foreign_keys("questionnairerecord")
    } == {"patient"}
    assert {
        foreign_key["referred_table"]
        for foreign_key in inspector.get_foreign_keys("questionnaireitemvalue")
    } == {"questionnairerecord"}
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == HEAD

    # 空表时 downgrade 合法：两表干净删除，再 upgrade 回到 HEAD。
    command.downgrade(config, PARENT)
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "questionnairerecord" not in tables
    assert "questionnaireitemvalue" not in tables

    command.upgrade(config, HEAD)
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == HEAD
    # 「模型与库没有漂移」这条只有在全仓最新头上才成立——这个文件停在中间那一版，
    # 所以最后再往前走到最新头再查一次，两件事都不放过。
    command.upgrade(config, REPO_HEAD)
    command.check(config)
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == REPO_HEAD


@pytest.mark.parametrize("evidence_kind", ["record", "value"])
def test_downgrade_refuses_while_any_questionnaire_evidence_exists(
        tmp_path, evidence_kind):
    db_path = tmp_path / f"questionnaire-evidence-{evidence_kind}.sqlite"
    config = _config(db_path)
    command.upgrade(config, HEAD)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _insert_record(connection)
        if evidence_kind == "value":
            _insert_value(connection)
    schema_before = _schema_rows(engine)

    with pytest.raises(RuntimeError, match="回滚不得丢弃已录入作答"):
        command.downgrade(config, PARENT)

    assert _schema_rows(engine) == schema_before
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == HEAD
        assert connection.execute(text(
            "SELECT count(*) FROM questionnairerecord")).scalar_one() == 1
        expected_values = 1 if evidence_kind == "value" else 0
        assert connection.execute(text(
            "SELECT count(*) FROM questionnaireitemvalue"
        )).scalar_one() == expected_values


def test_database_rejects_rows_violating_the_named_constraints(tmp_path):
    db_path = tmp_path / "questionnaire-constraints.sqlite"
    config = _config(db_path)
    command.upgrade(config, HEAD)
    engine = create_engine(f"sqlite:///{db_path}")

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_record(connection, record_id="QR-BAD-STATUS",
                           status="final")
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            # locked 却没有 locked_by/locked_at：违反 lock_complete。
            _insert_record(connection, record_id="QR-BAD-LOCK",
                           status="locked")
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_record(connection, record_id="QR-BAD-PHASE",
                           phase_label="基线")
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_record(connection, record_id="QR-BAD-DRAFT",
                           ai_draft_status="pending")

    with engine.begin() as connection:
        _insert_record(connection)
        _insert_record(connection, record_id="QR-MIG-LOCKED",
                       status="locked", locked_by="RESEARCH-A",
                       locked_at="2026-08-20 01:00:00")
        _insert_value(connection, value_source="human_direct")

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_value(connection, item_key="gds_02",
                          value_source="ai_guessed")
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            # 同 (record_id, item_key, field_key) 槽位唯一。
            _insert_value(connection, value_source=None)
