"""Migration coverage for patient-requested pause evidence.

Every database lives under pytest's temporary directory.  The downgrade must
remain possible for an empty schema, but must refuse before deleting a durable
patient pause fact.
"""
from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, inspect, text


HEAD = "c5a8f2d91e40"
PATIENT_PAUSE_HEAD = "a9d2e6f4c108"
PARENT = "b8e5f2a91c07"


def _config(db_path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def test_patient_pause_migration_is_single_head_and_roundtrips_clean_sqlite(
        tmp_path):
    db_path = tmp_path / "patient-pause-clean.sqlite"
    config = _config(db_path)
    assert ScriptDirectory.from_config(config).get_heads() == [HEAD]

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == HEAD
    assert "patientpausereceipt" in inspect(engine).get_table_names()

    command.downgrade(config, PARENT)
    assert "patientpausereceipt" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    command.check(config)
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == HEAD


def test_patient_pause_downgrade_refuses_to_erase_append_only_evidence(tmp_path):
    db_path = tmp_path / "patient-pause-evidence.sqlite"
    config = _config(db_path)
    command.upgrade(config, "head")
    # Reach the patient-pause revision first.  The newer caregiver migration
    # is allowed to downgrade normally and must not be mistaken for part of
    # the patient-pause guard's atomicity contract.
    command.downgrade(config, PATIENT_PAUSE_HEAD)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version"
        )).scalar_one() == PATIENT_PAUSE_HEAD
    tables_before = set(inspect(engine).get_table_names())
    assert "patientpausereceipt" in tables_before
    with engine.begin() as connection:
        # The migration guard intentionally checks the durable evidence table
        # before any DDL.  A minimal synthetic row is enough to prove that it
        # refuses rather than silently deleting the new domain.
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(text(
            "INSERT INTO patientpausereceipt "
            "(session_id, interaction_event_id, idempotency_key_sha256, "
            "request_hash, capability_token_hash, runtime_revision, live_seq) "
            "VALUES ('S-MIGRATION', 1, :key, :request, :capability, 1, 1)"
        ), {"key": "a" * 64, "request": "b" * 64, "capability": "c" * 64})

    with pytest.raises(RuntimeError, match="patient pause"):
        command.downgrade(config, PARENT)

    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version"
        )).scalar_one() == PATIENT_PAUSE_HEAD
        assert connection.execute(text(
            "SELECT count(*) FROM patientpausereceipt")).scalar_one() == 1
    # The a9 -> b8 guard runs before its first DDL statement.
    assert set(inspect(engine).get_table_names()) == tables_before


def test_patient_pause_downgrade_also_refuses_an_orphaned_pause_event(tmp_path):
    """A partial/manual repair cannot erase the event just because receipt is absent."""
    db_path = tmp_path / "patient-pause-event-only.sqlite"
    config = _config(db_path)
    command.upgrade(config, "head")
    command.downgrade(config, PATIENT_PAUSE_HEAD)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version"
        )).scalar_one() == PATIENT_PAUSE_HEAD
    tables_before = set(inspect(engine).get_table_names())
    assert "patientpausereceipt" in tables_before
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(text(
            "INSERT INTO interactionevent "
            "(session_id, event_seq, event_type, payload_json) "
            "VALUES ('S-MIGRATION', 1, 'patient_requested_pause', :payload)"
        ), {"payload": '{"request_id_sha256":"' + "d" * 64 + '"}'})

    with pytest.raises(RuntimeError, match="patient pause"):
        command.downgrade(config, PARENT)

    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version"
        )).scalar_one() == PATIENT_PAUSE_HEAD
        assert connection.execute(text(
            "SELECT count(*) FROM interactionevent WHERE "
            "event_type='patient_requested_pause'")).scalar_one() == 1
    assert set(inspect(engine).get_table_names()) == tables_before
