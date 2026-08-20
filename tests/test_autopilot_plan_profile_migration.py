"""Migration coverage for the immutable simulation-only plan profile bindings.

These tests never touch the default database: every engine is built inside the
pytest-provided temporary directory.  They also never call a provider — the
whole matrix is DDL, CHECK constraints and Alembic revisions.

The empty pair is the permanent representation of a canonical full-source plan
or session.  A populated pair is restricted to simulation data, and a
profile-bound Session must keep its VisitPlan link.  Downgrade must refuse
before any DDL when either table still carries profile evidence.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable
from sqlmodel import SQLModel

from app import models


ROOT = Path(__file__).resolve().parents[1]
PARENT = "d3f8b5c1a704"
HEAD = "e4a7c1d9b206"

PROFILE_COLUMNS = (
    "autopilot_profile_version_id",
    "autopilot_profile_definition_digest",
)
PLAN_PROFILE_CHECKS = (
    "ck_visit_plan_autopilot_profile_binding_complete",
    "ck_visit_plan_autopilot_profile_simulation_only",
)
SESSION_PROFILE_CHECKS = (
    "ck_session_autopilot_profile_binding_complete",
    "ck_session_autopilot_profile_simulation_only",
    "ck_session_autopilot_profile_requires_visit_plan",
)

VERSION = "week2-single20-demo-v1"
DIGEST = "0123456789abcdef" * 4

_MIGRATION_PATH = (
    ROOT / "alembic" / "versions"
    / "e4a7c1d9b206_autopilot_plan_profile_bindings.py"
)


def _migration_module():
    spec = importlib.util.spec_from_file_location(
        "e4a7c1d9b206_profile_bindings", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(db_path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _normalize_check_sql(sql: str) -> str:
    """Ignore whitespace and only redundant parentheses around the whole SQL."""
    value = "".join(sql.split())
    while value.startswith("(") and value.endswith(")"):
        depth = 0
        wraps_all = True
        for index, character in enumerate(value):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(value) - 1:
                    wraps_all = False
                    break
        if not wraps_all or depth != 0:
            break
        value = value[1:-1]
    return value


def _snapshot(db_path: Path, table: str) -> dict[str, object]:
    """Full structural identity of one table, order-independent."""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        return {
            "columns": sorted(
                (
                    column["name"],
                    str(column["type"]),
                    bool(column["nullable"]),
                    repr(column.get("default")),
                )
                for column in inspector.get_columns(table)
            ),
            "foreign_keys": sorted(
                repr(sorted(
                    (key, value) for key, value in fk.items()
                    if key != "options"
                ))
                for fk in inspector.get_foreign_keys(table)
            ),
            "unique_constraints": sorted(
                repr(sorted(unique.items()))
                for unique in inspector.get_unique_constraints(table)
            ),
            "check_constraints": sorted(
                (
                    str(check["name"]),
                    " ".join(str(check["sqltext"]).split()),
                )
                for check in inspector.get_check_constraints(table)
            ),
            "indexes": sorted(
                repr(sorted(index.items()))
                for index in inspector.get_indexes(table)
            ),
            "primary_key": repr(sorted(
                inspector.get_pk_constraint(table).items())),
        }
    finally:
        engine.dispose()


def _revision(db_path: Path) -> str:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        connection.close()


def _seed_patients(connection: sqlite3.Connection) -> None:
    connection.executemany(
        "INSERT INTO patient (patient_id,is_simulation_subject,"
        "governance_revision) VALUES (?,?,0)",
        [("sim-subject", 1), ("real-subject", 0)],
    )


_PLAN_COLUMNS = (
    "plan_id", "protocol_slot_key", "patient_id", "scheduled_date",
    "session_sitting_no", "week_no", "phase_type", "event_line",
    "item_bank_version_id", "is_simulation", "data_classification",
    "status", "revision", "created_by", "created_at", "updated_at",
    "autopilot_profile_version_id", "autopilot_profile_definition_digest",
)
_SESSION_COLUMNS = (
    "session_id", "patient_id", "visit_plan_id", "session_sitting_no",
    "week_no", "phase_type", "event_line", "item_bank_version_id",
    "is_simulation", "data_classification",
    "autopilot_profile_version_id", "autopilot_profile_definition_digest",
)


def _insert_plan(
    connection: sqlite3.Connection,
    *,
    plan_id: str = "vp_matrix",
    is_simulation: int = 1,
    data_classification: str = "simulation",
    version: str | None = None,
    digest: str | None = None,
    columns: tuple[str, ...] = _PLAN_COLUMNS,
) -> None:
    values = {
        "plan_id": plan_id,
        "protocol_slot_key": f"slot-{plan_id}",
        "patient_id": "sim-subject" if is_simulation else "real-subject",
        "scheduled_date": "2026-08-01",
        "session_sitting_no": 1,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "item_bank_version_id": "wk2-v1-20260707",
        "is_simulation": is_simulation,
        "data_classification": data_classification,
        "status": "draft",
        "revision": 1,
        "created_by": "RESEARCHER-1",
        "created_at": "2026-08-01 00:00:00",
        "updated_at": "2026-08-01 00:00:00",
        "autopilot_profile_version_id": version,
        "autopilot_profile_definition_digest": digest,
    }
    names = [name for name in columns if name in values]
    connection.execute(
        f"INSERT INTO visitplan ({','.join(names)}) "
        f"VALUES ({','.join('?' * len(names))})",
        [values[name] for name in names],
    )


def _insert_session(
    connection: sqlite3.Connection,
    *,
    session_id: str = "s_matrix",
    visit_plan_id: str | None = "vp_matrix",
    is_simulation: int = 1,
    data_classification: str = "simulation",
    version: str | None = None,
    digest: str | None = None,
    columns: tuple[str, ...] = _SESSION_COLUMNS,
) -> None:
    values = {
        "session_id": session_id,
        "patient_id": "sim-subject" if is_simulation else "real-subject",
        "visit_plan_id": visit_plan_id,
        "session_sitting_no": 1,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "item_bank_version_id": "wk2-v1-20260707",
        "is_simulation": is_simulation,
        "data_classification": data_classification,
        "autopilot_profile_version_id": version,
        "autopilot_profile_definition_digest": digest,
    }
    names = [name for name in columns if name in values]
    connection.execute(
        f"INSERT INTO session ({','.join(names)}) "
        f"VALUES ({','.join('?' * len(names))})",
        [values[name] for name in names],
    )


def _head_database(tmp_path: Path, name: str = "head.sqlite") -> Path:
    db_path = tmp_path / name
    command.upgrade(_config(db_path), HEAD)
    return db_path


# --------------------------------------------------------------------------
# Revision graph and delta
# --------------------------------------------------------------------------


def test_fresh_upgrade_reaches_exactly_one_new_head(tmp_path):
    db_path = _head_database(tmp_path)
    config = _config(db_path)

    # 全局单一 head 不变量；本迁移必须仍在当前冻结研究行
    # 快照迁移的祖先链上。
    heads = list(ScriptDirectory.from_config(config).get_heads())
    assert heads == ["b6d4f8a2c917"]
    assert _revision(db_path) == HEAD


def test_migration_declares_the_expected_single_parent():
    module = _migration_module()

    assert module.revision == HEAD
    assert module.down_revision == PARENT
    assert module.branch_labels is None
    assert module.depends_on is None


@pytest.mark.parametrize(
    "table,added_checks",
    [
        ("visitplan", PLAN_PROFILE_CHECKS),
        ("session", SESSION_PROFILE_CHECKS),
    ],
)
def test_d3_to_e4_adds_only_profile_columns_and_named_checks(
        tmp_path, table, added_checks):
    db_path = tmp_path / "delta.sqlite"
    config = _config(db_path)
    command.upgrade(config, PARENT)
    before = _snapshot(db_path, table)

    command.upgrade(config, HEAD)
    after = _snapshot(db_path, table)

    added_columns = set(after["columns"]) - set(before["columns"])
    assert {name for name, *_ in added_columns} == set(PROFILE_COLUMNS)
    assert not set(before["columns"]) - set(after["columns"])

    added = set(after["check_constraints"]) - set(before["check_constraints"])
    assert {name for name, _ in added} == set(added_checks)
    assert not (
        set(before["check_constraints"]) - set(after["check_constraints"]))

    for key in ("foreign_keys", "unique_constraints", "indexes", "primary_key"):
        assert before[key] == after[key], key

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.parametrize("table", ["visitplan", "session"])
def test_profile_columns_are_nullable_without_default_and_unindexed(
        tmp_path, table):
    db_path = _head_database(tmp_path)
    connection = sqlite3.connect(db_path)
    try:
        rows = {
            str(row[1]): row
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        for name in PROFILE_COLUMNS:
            assert name in rows
            _cid, _name, _type, notnull, default, primary_key = rows[name]
            assert str(_type).upper() == "VARCHAR"
            assert notnull == 0
            assert default is None
            assert primary_key == 0
    finally:
        connection.close()

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        indexed = {
            column
            for index in inspect(engine).get_indexes(table)
            for column in index["column_names"]
        }
    finally:
        engine.dispose()
    assert indexed.isdisjoint(PROFILE_COLUMNS)


def test_upgrade_never_backfills_historical_rows(tmp_path):
    db_path = tmp_path / "backfill.sqlite"
    config = _config(db_path)
    command.upgrade(config, PARENT)

    legacy_plan = tuple(
        name for name in _PLAN_COLUMNS if name not in PROFILE_COLUMNS)
    legacy_session = tuple(
        name for name in _SESSION_COLUMNS if name not in PROFILE_COLUMNS)
    connection = _connect(db_path)
    try:
        _seed_patients(connection)
        _insert_plan(connection, columns=legacy_plan)
        _insert_session(
            connection,
            data_classification="legacy_unknown",
            columns=legacy_session,
        )
        connection.commit()
    finally:
        connection.close()

    command.upgrade(config, HEAD)

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute(
            "SELECT autopilot_profile_version_id,"
            "autopilot_profile_definition_digest FROM visitplan"
        ).fetchall() == [(None, None)]
        assert connection.execute(
            "SELECT autopilot_profile_version_id,"
            "autopilot_profile_definition_digest,data_classification "
            "FROM session"
        ).fetchall() == [(None, None, "legacy_unknown")]
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Accepted pairs
# --------------------------------------------------------------------------


def test_null_pair_is_accepted_on_both_tables(tmp_path):
    db_path = _head_database(tmp_path)
    connection = _connect(db_path)
    try:
        _seed_patients(connection)
        _insert_plan(connection, is_simulation=0,
                     data_classification="research")
        _insert_session(connection, is_simulation=0,
                        data_classification="research")
        connection.commit()

        assert connection.execute(
            "SELECT count(*) FROM visitplan WHERE "
            "autopilot_profile_version_id IS NULL AND "
            "autopilot_profile_definition_digest IS NULL").fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM session WHERE "
            "autopilot_profile_version_id IS NULL AND "
            "autopilot_profile_definition_digest IS NULL").fetchone()[0] == 1
    finally:
        connection.close()


def test_legal_simulation_pair_is_accepted_on_both_tables(tmp_path):
    db_path = _head_database(tmp_path)
    connection = _connect(db_path)
    try:
        _seed_patients(connection)
        _insert_plan(connection, version=VERSION, digest=DIGEST)
        _insert_session(connection, version=VERSION, digest=DIGEST)
        connection.commit()

        assert connection.execute(
            "SELECT autopilot_profile_version_id,"
            "autopilot_profile_definition_digest FROM visitplan"
        ).fetchone() == (VERSION, DIGEST)
        assert connection.execute(
            "SELECT autopilot_profile_version_id,"
            "autopilot_profile_definition_digest FROM session"
        ).fetchone() == (VERSION, DIGEST)
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Rejected pairs
# --------------------------------------------------------------------------


def _expect_check_failure(
        connection: sqlite3.Connection, constraint: str, insert) -> None:
    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        insert()
    assert constraint in str(excinfo.value)
    connection.rollback()


@pytest.mark.parametrize("version,digest", [(VERSION, None), (None, DIGEST)])
@pytest.mark.parametrize("table", ["visitplan", "session"])
def test_half_pair_is_rejected(tmp_path, table, version, digest):
    db_path = _head_database(tmp_path)
    connection = _connect(db_path)
    try:
        _seed_patients(connection)
        _insert_plan(connection)
        connection.commit()
        insert = _insert_plan if table == "visitplan" else _insert_session
        identity = (
            {"plan_id": "vp_half"} if table == "visitplan"
            else {"session_id": "s_half"}
        )
        _expect_check_failure(
            connection,
            f"ck_{'visit_plan' if table == 'visitplan' else 'session'}"
            "_autopilot_profile_binding_complete",
            lambda: insert(
                connection, version=version, digest=digest, **identity),
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "version", ["", "   ", " week2-single20-demo-v1", "week2-single20-demo-v1 "])
def test_blank_or_padded_version_is_rejected(tmp_path, version):
    db_path = _head_database(tmp_path)
    connection = _connect(db_path)
    try:
        _seed_patients(connection)
        _expect_check_failure(
            connection,
            "ck_visit_plan_autopilot_profile_binding_complete",
            lambda: _insert_plan(
                connection, plan_id="vp_version",
                version=version, digest=DIGEST),
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "digest",
    [
        "0123456789abcdef" * 3 + "0123456789abcde",     # 63
        "0123456789abcdef" * 4 + "0",                    # 65
        ("0123456789abcdef" * 4).upper(),                # uppercase
        "g" * 64,                                        # non-hex letter
        "z" * 64,                                        # non-hex letter
    ],
)
def test_malformed_digest_is_rejected(tmp_path, digest):
    db_path = _head_database(tmp_path)
    connection = _connect(db_path)
    try:
        _seed_patients(connection)
        _expect_check_failure(
            connection,
            "ck_visit_plan_autopilot_profile_binding_complete",
            lambda: _insert_plan(
                connection, plan_id="vp_digest",
                version=VERSION, digest=digest),
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "is_simulation,data_classification",
    [(0, "research"), (1, "research"), (0, "simulation")],
)
def test_plan_paired_set_requires_simulation_classification(
        tmp_path, is_simulation, data_classification):
    db_path = _head_database(tmp_path)
    connection = _connect(db_path)
    try:
        _seed_patients(connection)
        _expect_check_failure(
            connection,
            "ck_visit_plan_autopilot_profile_simulation_only",
            lambda: _insert_plan(
                connection, plan_id="vp_class",
                is_simulation=is_simulation,
                data_classification=data_classification,
                version=VERSION, digest=DIGEST),
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "is_simulation,data_classification",
    [(0, "research"), (1, "research"), (0, "legacy_unknown"),
     (1, "legacy_unknown"), (0, "simulation")],
)
def test_session_paired_set_requires_simulation_classification(
        tmp_path, is_simulation, data_classification):
    db_path = _head_database(tmp_path)
    connection = _connect(db_path)
    try:
        _seed_patients(connection)
        _insert_plan(connection)
        connection.commit()
        _expect_check_failure(
            connection,
            "ck_session_autopilot_profile_simulation_only",
            lambda: _insert_session(
                connection, session_id="s_class",
                is_simulation=is_simulation,
                data_classification=data_classification,
                version=VERSION, digest=DIGEST),
        )
    finally:
        connection.close()


def test_session_paired_set_requires_visit_plan_link(tmp_path):
    db_path = _head_database(tmp_path)
    connection = _connect(db_path)
    try:
        _seed_patients(connection)
        _expect_check_failure(
            connection,
            "ck_session_autopilot_profile_requires_visit_plan",
            lambda: _insert_session(
                connection, session_id="s_nolink", visit_plan_id=None,
                version=VERSION, digest=DIGEST),
        )
    finally:
        connection.close()


def test_all_five_named_profile_checks_exist_at_head(tmp_path):
    db_path = _head_database(tmp_path)

    plan_checks = {
        name for name, _ in _snapshot(db_path, "visitplan")["check_constraints"]
    }
    session_checks = {
        name for name, _ in _snapshot(db_path, "session")["check_constraints"]
    }

    assert set(PLAN_PROFILE_CHECKS) <= plan_checks
    assert set(SESSION_PROFILE_CHECKS) <= session_checks
    assert len(PLAN_PROFILE_CHECKS) + len(SESSION_PROFILE_CHECKS) == 5


# --------------------------------------------------------------------------
# Model / migration parity and PostgreSQL compilation
# --------------------------------------------------------------------------


def test_model_and_migration_profile_sql_are_byte_identical():
    module = _migration_module()

    assert module._PROFILE_BINDING_CHECK == models.AUTOPILOT_PROFILE_BINDING_CHECK
    assert module._PROFILE_SIMULATION_CHECK == (
        models.AUTOPILOT_PROFILE_SIMULATION_CHECK)
    assert module._PROFILE_SESSION_PLAN_LINK_CHECK == (
        models.AUTOPILOT_PROFILE_SESSION_PLAN_LINK_CHECK)
    for column in PROFILE_COLUMNS:
        assert module._hex64_sql(column) == models._hex64_sql(column)


@pytest.mark.parametrize(
    "table,expected",
    [
        (
            "visitplan",
            {
                "ck_visit_plan_autopilot_profile_binding_complete":
                    models.AUTOPILOT_PROFILE_BINDING_CHECK,
                "ck_visit_plan_autopilot_profile_simulation_only":
                    models.AUTOPILOT_PROFILE_SIMULATION_CHECK,
            },
        ),
        (
            "session",
            {
                "ck_session_autopilot_profile_binding_complete":
                    models.AUTOPILOT_PROFILE_BINDING_CHECK,
                "ck_session_autopilot_profile_simulation_only":
                    models.AUTOPILOT_PROFILE_SIMULATION_CHECK,
                "ck_session_autopilot_profile_requires_visit_plan":
                    models.AUTOPILOT_PROFILE_SESSION_PLAN_LINK_CHECK,
            },
        ),
    ],
)
def test_installed_profile_check_sql_matches_the_model(tmp_path, table, expected):
    """Parity covers the DDL Alembic actually installed, not just constants."""
    db_path = _head_database(tmp_path)
    installed = dict(_snapshot(db_path, table)["check_constraints"])

    assert {
        name: _normalize_check_sql(installed[name])
        for name in expected
    } == {
        name: _normalize_check_sql(expression)
        for name, expression in expected.items()
    }


def test_profile_checks_compile_for_postgresql():
    dialect = postgresql.dialect()
    tables = SQLModel.metadata.tables

    plan_ddl = str(CreateTable(tables["visitplan"]).compile(dialect=dialect))
    session_ddl = str(CreateTable(tables["session"]).compile(dialect=dialect))

    for name in PLAN_PROFILE_CHECKS:
        assert name in plan_ddl
    for name in SESSION_PROFILE_CHECKS:
        assert name in session_ddl

    module = _migration_module()
    assert module._PROFILE_BINDING_CHECK in plan_ddl
    assert module._PROFILE_SIMULATION_CHECK in plan_ddl
    assert module._PROFILE_BINDING_CHECK in session_ddl
    assert module._PROFILE_SIMULATION_CHECK in session_ddl
    assert module._PROFILE_SESSION_PLAN_LINK_CHECK in session_ddl

    for column in PROFILE_COLUMNS:
        assert f"{column} VARCHAR" in plan_ddl
        assert f"{column} VARCHAR" in session_ddl
    assert "NOT NULL" not in plan_ddl.split("autopilot_profile_version_id")[1][:40]


# --------------------------------------------------------------------------
# Downgrade
# --------------------------------------------------------------------------


def test_legacy_null_only_roundtrip_preserves_every_old_field_and_constraint(
        tmp_path):
    db_path = tmp_path / "legacy-roundtrip.sqlite"
    config = _config(db_path)
    command.upgrade(config, PARENT)

    legacy_plan_columns = tuple(
        name for name in _PLAN_COLUMNS if name not in PROFILE_COLUMNS)
    legacy_session_columns = tuple(
        name for name in _SESSION_COLUMNS if name not in PROFILE_COLUMNS)
    connection = _connect(db_path)
    try:
        _seed_patients(connection)
        _insert_plan(
            connection,
            plan_id="vp_legacy",
            is_simulation=1,
            data_classification="simulation",
            columns=legacy_plan_columns,
        )
        _insert_session(
            connection,
            session_id="s_legacy",
            visit_plan_id="vp_legacy",
            is_simulation=1,
            data_classification="legacy_unknown",
            columns=legacy_session_columns,
        )
        _insert_plan(
            connection,
            plan_id="vp_research",
            is_simulation=0,
            data_classification="research",
            columns=legacy_plan_columns,
        )
        _insert_session(
            connection,
            session_id="s_research",
            visit_plan_id="vp_research",
            is_simulation=0,
            data_classification="research",
            columns=legacy_session_columns,
        )
        connection.commit()
    finally:
        connection.close()

    tables = ("visitplan", "session")
    legacy_schema = {table: _snapshot(db_path, table) for table in tables}
    legacy_columns: dict[str, tuple[str, ...]] = {}
    legacy_rows: dict[str, list[tuple[object, ...]]] = {}
    connection = sqlite3.connect(db_path)
    try:
        for table in tables:
            columns = tuple(
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            )
            legacy_columns[table] = columns
            primary_key = "plan_id" if table == "visitplan" else "session_id"
            legacy_rows[table] = connection.execute(
                f"SELECT {','.join(columns)} FROM {table} "
                f"ORDER BY {primary_key}"
            ).fetchall()
    finally:
        connection.close()

    command.upgrade(config, HEAD)
    assert _revision(db_path) == HEAD
    first_head_schema = {table: _snapshot(db_path, table) for table in tables}

    def assert_old_rows_and_null_profiles() -> None:
        connection = sqlite3.connect(db_path)
        try:
            for table in tables:
                primary_key = (
                    "plan_id" if table == "visitplan" else "session_id")
                assert connection.execute(
                    f"SELECT {','.join(legacy_columns[table])} FROM {table} "
                    f"ORDER BY {primary_key}"
                ).fetchall() == legacy_rows[table]
                assert connection.execute(
                    "SELECT autopilot_profile_version_id,"
                    f"autopilot_profile_definition_digest FROM {table} "
                    f"ORDER BY {primary_key}"
                ).fetchall() == [(None, None), (None, None)]
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            connection.close()

    assert_old_rows_and_null_profiles()

    command.downgrade(config, PARENT)
    assert _revision(db_path) == PARENT
    assert {table: _snapshot(db_path, table) for table in tables} == legacy_schema
    connection = sqlite3.connect(db_path)
    try:
        for table in tables:
            primary_key = "plan_id" if table == "visitplan" else "session_id"
            columns = tuple(
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            )
            assert columns == legacy_columns[table]
            assert connection.execute(
                f"SELECT {','.join(columns)} FROM {table} "
                f"ORDER BY {primary_key}"
            ).fetchall() == legacy_rows[table]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()

    command.upgrade(config, HEAD)
    assert _revision(db_path) == HEAD
    assert {table: _snapshot(db_path, table) for table in tables} == (
        first_head_schema)
    assert_old_rows_and_null_profiles()
    # 本迁移已不再是全局 head(b3e7c5a9d214 等在其上),up-to-date 检查不再适用;
    # 往返完整性由上面的 revision/schema/行快照断言承担。


def _corrupt(db_path: Path, table: str, column: str) -> None:
    """Write half-pair evidence past the CHECK, as a corrupted restore would."""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        value = VERSION if column.endswith("version_id") else DIGEST
        connection.execute(f"UPDATE {table} SET {column} = ?", (value,))
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize(
    "populate",
    [
        pytest.param(("visitplan",), id="visitplan-only"),
        pytest.param(("session",), id="session-only"),
        pytest.param(("visitplan", "session"), id="both-tables"),
    ],
)
def test_populated_downgrade_is_refused(tmp_path, populate):
    db_path = tmp_path / "populated.sqlite"
    config = _config(db_path)
    command.upgrade(config, HEAD)
    connection = _connect(db_path)
    try:
        _seed_patients(connection)
        _insert_plan(
            connection,
            version=VERSION if "visitplan" in populate else None,
            digest=DIGEST if "visitplan" in populate else None,
        )
        _insert_session(
            connection,
            version=VERSION if "session" in populate else None,
            digest=DIGEST if "session" in populate else None,
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError) as excinfo:
        command.downgrade(config, PARENT)

    message = str(excinfo.value)
    assert "autopilot profile binding evidence prevents downgrade" in message
    for table in populate:
        assert table in message
    assert _revision(db_path) == HEAD


@pytest.mark.parametrize(
    "table,column",
    [
        ("visitplan", "autopilot_profile_version_id"),
        ("visitplan", "autopilot_profile_definition_digest"),
        ("session", "autopilot_profile_version_id"),
        ("session", "autopilot_profile_definition_digest"),
    ],
)
def test_half_pair_corruption_also_blocks_downgrade(tmp_path, table, column):
    db_path = tmp_path / "corrupt.sqlite"
    config = _config(db_path)
    command.upgrade(config, HEAD)
    connection = _connect(db_path)
    try:
        _seed_patients(connection)
        _insert_plan(connection)
        _insert_session(connection)
        connection.commit()
    finally:
        connection.close()
    _corrupt(db_path, table, column)

    with pytest.raises(RuntimeError) as excinfo:
        command.downgrade(config, PARENT)

    assert table in str(excinfo.value)
    assert _revision(db_path) == HEAD


def test_refused_downgrade_leaves_revision_schema_and_rows_untouched(tmp_path):
    db_path = tmp_path / "refusal.sqlite"
    config = _config(db_path)
    command.upgrade(config, HEAD)
    connection = _connect(db_path)
    try:
        _seed_patients(connection)
        _insert_plan(connection, version=VERSION, digest=DIGEST)
        _insert_session(connection, version=VERSION, digest=DIGEST)
        connection.commit()
    finally:
        connection.close()

    before = {table: _snapshot(db_path, table)
              for table in ("visitplan", "session")}
    connection = sqlite3.connect(db_path)
    try:
        plan_rows = connection.execute(
            "SELECT * FROM visitplan").fetchall()
        session_rows = connection.execute("SELECT * FROM session").fetchall()
    finally:
        connection.close()

    with pytest.raises(RuntimeError):
        command.downgrade(config, PARENT)

    assert _revision(db_path) == HEAD
    assert before == {table: _snapshot(db_path, table)
                      for table in ("visitplan", "session")}
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT * FROM visitplan").fetchall() \
            == plan_rows
        assert connection.execute("SELECT * FROM session").fetchall() \
            == session_rows
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_downgrade_inspects_both_tables_before_touching_either(tmp_path):
    """VisitPlan-only evidence must still spare the first-rebuilt table.

    ``downgrade`` rebuilds ``session`` before ``visitplan``.  If the preflight
    were interleaved with the DDL, session would already have lost its profile
    columns by the time the VisitPlan evidence was noticed.
    """
    db_path = tmp_path / "preflight.sqlite"
    config = _config(db_path)
    command.upgrade(config, HEAD)
    connection = _connect(db_path)
    try:
        _seed_patients(connection)
        _insert_plan(connection, version=VERSION, digest=DIGEST)
        _insert_session(connection)
        connection.commit()
    finally:
        connection.close()
    session_before = _snapshot(db_path, "session")

    with pytest.raises(RuntimeError) as excinfo:
        command.downgrade(config, PARENT)

    assert "visitplan" in str(excinfo.value)
    assert "session" not in str(excinfo.value)
    assert _snapshot(db_path, "session") == session_before
    connection = sqlite3.connect(db_path)
    try:
        session_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(session)")
        }
    finally:
        connection.close()
    assert set(PROFILE_COLUMNS) <= session_columns
