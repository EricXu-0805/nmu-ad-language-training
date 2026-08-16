"""Frozen research-row snapshot model and Alembic acceptance coverage."""
from __future__ import annotations

from datetime import datetime

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from app.models import QualityReleaseEpoch, QualityReleaseEpochRowSnapshot


HEAD = "6f2a9c4d8e17"
PARENT = "141bc30e4580"


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


def _insert_epoch(
        connection, *, epoch_id: str, snapshot: bool,
        recovery: bool = False) -> None:
    columns = [
        "epoch_id", "epoch_seq", "status", "as_of", "frozen_at",
        "cohort_rule_version", "registry_version", "schema_version",
        "cohort_size_band", "session_count_band", "payload_json",
        "payload_sha256", "min_subjects_applied",
        "min_cell_subjects_applied", "band_width_applied",
        "rate_decimals_applied", "diagnostics_status",
        "deidentification_key_id", "builder_actor_display_id",
        "builder_actor_role", "approver_actor_display_id",
        "approver_actor_role", "idempotency_key_sha256",
    ]
    values: dict[str, object] = {
        "epoch_id": epoch_id,
        "epoch_seq": 1,
        "status": "published",
        "as_of": "2026-08-17 00:00:00",
        "frozen_at": "2026-08-17 00:00:00",
        "cohort_rule_version": "cohort-v1",
        "registry_version": "registry-v1",
        "schema_version": "quality-release.v1",
        "cohort_size_band": "5-9",
        "session_count_band": "5-9",
        "payload_json": "{}",
        "payload_sha256": "a" * 64,
        "min_subjects_applied": 5,
        "min_cell_subjects_applied": 5,
        "band_width_applied": 5,
        "rate_decimals_applied": 2,
        "diagnostics_status": "complete",
        "deidentification_key_id": "research-test-v1",
        "builder_actor_display_id": "BUILDER-1",
        "builder_actor_role": "data_steward",
        "approver_actor_display_id": "APPROVER-1",
        "approver_actor_role": "admin",
        "idempotency_key_sha256": "b" * 64,
    }
    if snapshot:
        columns.extend([
            "research_snapshot_schema_version",
            "research_snapshot_manifest_json",
            "research_snapshot_sha256",
        ])
        values.update({
            "research_snapshot_schema_version": "research-snapshot.v1",
            "research_snapshot_manifest_json": "{}",
            "research_snapshot_sha256": "c" * 64,
        })
    if recovery:
        columns.extend([
            "proposal_sha256",
            "entry_quarantine_days_applied",
        ])
        values.update({
            "proposal_sha256": "e" * 64,
            "entry_quarantine_days_applied": 14,
        })
    connection.execute(text(
        f"INSERT INTO qualityreleaseepoch ({','.join(columns)}) "
        f"VALUES ({','.join(':' + column for column in columns)})"
    ), values)


def _insert_snapshot_row(
        connection, *, epoch_id: str, dataset_key: str = "subjects",
        row_ordinal: int = 1, row_sha256: str | None = None) -> None:
    connection.execute(text(
        "INSERT INTO qualityreleaseepochrowsnapshot "
        "(epoch_id,dataset_key,row_ordinal,row_json,row_sha256) VALUES "
        "(:epoch_id,:dataset_key,:row_ordinal,:row_json,:row_sha256)"
    ), {
        "epoch_id": epoch_id,
        "dataset_key": dataset_key,
        "row_ordinal": row_ordinal,
        "row_json": "{}",
        "row_sha256": row_sha256 or "d" * 64,
    })


def test_snapshot_migration_is_single_head_and_roundtrips_empty_sqlite(tmp_path):
    db_path = tmp_path / "research-row-snapshot-clean.sqlite"
    config = _config(db_path)
    assert ScriptDirectory.from_config(config).get_heads() == [HEAD]

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "qualityreleaseepochrowsnapshot" in inspector.get_table_names()
    assert {
        "research_snapshot_schema_version",
        "research_snapshot_manifest_json",
        "research_snapshot_sha256",
        "proposal_sha256",
        "entry_quarantine_days_applied",
    }.issubset({
        column["name"]
        for column in inspector.get_columns("qualityreleaseepoch")
    })
    epoch_checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints(
            "qualityreleaseepoch")
    }
    assert {
        "ck_quality_release_epoch_recovery_evidence_complete",
        "ck_quality_release_epoch_proposal_hash",
        "ck_quality_release_epoch_quarantine_days",
    }.issubset(epoch_checks)
    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints(
            "qualityreleaseepochrowsnapshot")
    } == {
        "ck_quality_release_epoch_row_snapshot_dataset_closed",
        "ck_quality_release_epoch_row_snapshot_hash",
        "ck_quality_release_epoch_row_snapshot_ordinal_positive",
    }
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            "qualityreleaseepochrowsnapshot")
    } == {"uq_quality_release_epoch_row_snapshot_ordinal"}
    assert {
        foreign_key["referred_table"]
        for foreign_key in inspector.get_foreign_keys(
            "qualityreleaseepochrowsnapshot")
    } == {"qualityreleaseepoch"}
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == HEAD

    command.downgrade(config, PARENT)
    inspector = inspect(engine)
    assert "qualityreleaseepochrowsnapshot" not in inspector.get_table_names()
    assert {
        "research_snapshot_schema_version",
        "research_snapshot_manifest_json",
        "research_snapshot_sha256",
        "proposal_sha256",
        "entry_quarantine_days_applied",
    }.isdisjoint({
        column["name"]
        for column in inspector.get_columns("qualityreleaseepoch")
    })

    command.upgrade(config, "head")
    command.check(config)
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == HEAD


def test_upgrade_and_downgrade_preserve_a_legacy_epoch_with_null_snapshot(
        tmp_path):
    db_path = tmp_path / "legacy-quality-release.sqlite"
    config = _config(db_path)
    command.upgrade(config, PARENT)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _insert_epoch(connection, epoch_id="EPOCH-LEGACY", snapshot=False)

    command.upgrade(config, HEAD)
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT research_snapshot_schema_version, "
            "research_snapshot_manifest_json, research_snapshot_sha256, "
            "proposal_sha256, entry_quarantine_days_applied "
            "FROM qualityreleaseepoch WHERE epoch_id='EPOCH-LEGACY'"
        )).one() == (None, None, None, None, None)

    command.downgrade(config, PARENT)
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT count(*) FROM qualityreleaseepoch"
        )).scalar_one() == 1
    assert "qualityreleaseepochrowsnapshot" not in inspect(engine).get_table_names()

    command.upgrade(config, HEAD)
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == HEAD


@pytest.mark.parametrize("evidence_kind", ["manifest", "row", "recovery"])
def test_downgrade_refuses_any_snapshot_evidence_before_any_ddl(
        tmp_path, evidence_kind):
    db_path = tmp_path / f"snapshot-evidence-{evidence_kind}.sqlite"
    config = _config(db_path)
    command.upgrade(config, HEAD)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _insert_epoch(
            connection,
            epoch_id="EPOCH-EVIDENCE",
            snapshot=evidence_kind == "manifest",
            recovery=evidence_kind == "recovery",
        )
        if evidence_kind == "row":
            _insert_snapshot_row(connection, epoch_id="EPOCH-EVIDENCE")
    schema_before = _schema_rows(engine)

    with pytest.raises(RuntimeError, match="quality-release evidence prevents"):
        command.downgrade(config, PARENT)

    assert _schema_rows(engine) == schema_before
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == HEAD
        expected_rows = 1 if evidence_kind == "row" else 0
        assert connection.execute(text(
            "SELECT count(*) FROM qualityreleaseepochrowsnapshot"
        )).scalar_one() == expected_rows


def test_database_rejects_partial_manifest_and_malformed_snapshot_rows(tmp_path):
    db_path = tmp_path / "snapshot-constraints.sqlite"
    config = _config(db_path)
    command.upgrade(config, HEAD)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _insert_epoch(connection, epoch_id="EPOCH-CONSTRAINT", snapshot=False)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE qualityreleaseepoch SET "
                "research_snapshot_schema_version='research-snapshot.v1' "
                "WHERE epoch_id='EPOCH-CONSTRAINT'"
            ))

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE qualityreleaseepoch SET proposal_sha256=:proposal "
                "WHERE epoch_id='EPOCH-CONSTRAINT'"
            ), {"proposal": "e" * 64})

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE qualityreleaseepoch SET proposal_sha256=:proposal, "
                "entry_quarantine_days_applied=14 "
                "WHERE epoch_id='EPOCH-CONSTRAINT'"
            ), {"proposal": "G" * 64})

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE qualityreleaseepoch SET proposal_sha256=:proposal, "
                "entry_quarantine_days_applied=366 "
                "WHERE epoch_id='EPOCH-CONSTRAINT'"
            ), {"proposal": "e" * 64})

    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE qualityreleaseepoch SET "
            "research_snapshot_schema_version='research-snapshot.v1', "
            "research_snapshot_manifest_json='{}', "
            "research_snapshot_sha256=:digest "
            "WHERE epoch_id='EPOCH-CONSTRAINT'"
        ), {"digest": "c" * 64})

    invalid_rows = (
        {"dataset_key": "patients", "row_ordinal": 1, "row_sha256": "d" * 64},
        {"dataset_key": "subjects", "row_ordinal": 0, "row_sha256": "d" * 64},
        {"dataset_key": "subjects", "row_ordinal": 1, "row_sha256": "G" * 64},
    )
    for values in invalid_rows:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _insert_snapshot_row(
                    connection,
                    epoch_id="EPOCH-CONSTRAINT",
                    **values,
                )

    with engine.begin() as connection:
        _insert_snapshot_row(connection, epoch_id="EPOCH-CONSTRAINT")
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_snapshot_row(connection, epoch_id="EPOCH-CONSTRAINT")


def _model_epoch() -> QualityReleaseEpoch:
    return QualityReleaseEpoch(
        epoch_id="EPOCH-MODEL",
        epoch_seq=1,
        status="published",
        as_of=datetime(2026, 8, 17),
        frozen_at=datetime(2026, 8, 17),
        cohort_rule_version="cohort-v1",
        registry_version="registry-v1",
        schema_version="quality-release.v1",
        cohort_size_band="5-9",
        session_count_band="5-9",
        payload_json="{}",
        payload_sha256="a" * 64,
        min_subjects_applied=5,
        min_cell_subjects_applied=5,
        band_width_applied=5,
        rate_decimals_applied=2,
        diagnostics_status="complete",
        deidentification_key_id="research-test-v1",
        builder_actor_display_id="BUILDER-1",
        builder_actor_role="data_steward",
        approver_actor_display_id="APPROVER-1",
        approver_actor_role="admin",
        idempotency_key_sha256="b" * 64,
        research_snapshot_schema_version="research-snapshot.v1",
        research_snapshot_manifest_json="{}",
        research_snapshot_sha256="c" * 64,
        proposal_sha256="e" * 64,
        entry_quarantine_days_applied=14,
    )


def test_snapshot_row_model_rejects_update_and_delete():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(_model_epoch())
        session.add(QualityReleaseEpochRowSnapshot(
            epoch_id="EPOCH-MODEL",
            dataset_key="subjects",
            row_ordinal=1,
            row_json="{}",
            row_sha256="d" * 64,
        ))
        session.commit()
        row = session.exec(select(QualityReleaseEpochRowSnapshot)).one()

        row.row_json = '{"changed":true}'
        with pytest.raises(RuntimeError, match="冻结的研究数据行"):
            session.commit()
        session.rollback()

        row = session.exec(select(QualityReleaseEpochRowSnapshot)).one()
        session.delete(row)
        with pytest.raises(RuntimeError, match="冻结的研究数据行"):
            session.commit()
