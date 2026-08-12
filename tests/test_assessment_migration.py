"""Alembic acceptance checks for the independent assessment evidence domain."""
from __future__ import annotations

import logging

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text


PARENT = "e8a1c4b7d902"
HEAD = "b3e7c5a9d214"
RECORDING_AUTH_REVISION = "b8e5f2a91c07"
TABLES = {
    "assessmentevent",
    "assessmentinstance",
    "assessmentitemresponse",
    "assessmentscoringevidence",
    "assessmentdeferralapproval",
    "assessmenteventcloseout",
    "assessmentcommand",
}


def _config(db_path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def _constraint_names(inspector, table: str) -> set[str]:
    names = {
        row["name"] for row in inspector.get_unique_constraints(table)
        if row.get("name")
    }
    names.update(
        row["name"] for row in inspector.get_check_constraints(table)
        if row.get("name")
    )
    return names


def test_assessment_migration_is_single_head_and_roundtrips_clean_sqlite(tmp_path):
    db_path = tmp_path / "formal-assessment.sqlite"
    config = _config(db_path)
    assert ScriptDirectory.from_config(config).get_heads() == [HEAD]

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert TABLES.issubset(set(inspector.get_table_names()))
    assert "scaleresult" in inspector.get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == HEAD

    event_foreign_tables = {
        row["referred_table"] for row in inspector.get_foreign_keys(
            "assessmentevent")
    }
    assert event_foreign_tables == {"patient"}
    assert "session" not in event_foreign_tables
    assert "uq_assessment_event_active_protocol_slot" in _constraint_names(
        inspector, "assessmentevent")
    assert "uq_assessment_instance_event_category" in _constraint_names(
        inspector, "assessmentinstance")
    assert "uq_assessment_response_item_revision" in _constraint_names(
        inspector, "assessmentitemresponse")
    assert "uq_assessment_response_artifact_receipt_digest" in _constraint_names(
        inspector, "assessmentitemresponse")
    assert "uq_assessment_scoring_instance" in _constraint_names(
        inspector, "assessmentscoringevidence")
    assert "uq_assessment_closeout_event" in _constraint_names(
        inspector, "assessmenteventcloseout")
    assert "uq_assessment_command_idempotency_hash" in _constraint_names(
        inspector, "assessmentcommand")

    instance_columns = {
        row["name"] for row in inspector.get_columns("assessmentinstance")
    }
    assert {
        "definition_bundle_digest",
        "definition_digest",
        "item_set_digest",
        "administration_protocol_digest",
        "response_schema_digest",
        "result_schema_digest",
        "missingness_rule_digest",
        "stopping_rule_digest",
        "scoring_algorithm_digest",
        "score_min",
        "score_max",
        "score_direction",
        "score_rounding_rule",
        "automatic_scoring_permitted",
        "item_response_storage_permitted",
        "result_storage_permitted",
        "result_export_permitted",
    }.issubset(instance_columns)

    command.downgrade(config, PARENT)
    inspector = inspect(engine)
    assert TABLES.isdisjoint(set(inspector.get_table_names()))
    assert "scaleresult" in inspector.get_table_names()

    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == HEAD


def test_upgrade_leaves_a_preexisting_app_logger_enabled(tmp_path):
    """alembic.ini's fileConfig() used to default to
    disable_existing_loggers=True, which silently disabled any app logger
    already created before an in-process upgrade ran — masking caplog
    assertions in tests that execute later in the same process. The
    precondition below is asserted, not forced, so real ambient pollution
    (from an earlier test) surfaces as a failure instead of being hidden.
    """
    target_logger = logging.getLogger("app.tts")
    assert target_logger.disabled is False

    db_path = tmp_path / "logger-state.sqlite"
    config = _config(db_path)
    command.upgrade(config, "head")

    assert target_logger.disabled is False


def test_recording_authorization_downgrade_refuses_with_evidence(tmp_path):
    """b8e5f2a91c07 fail-closed 留在授权证据尚可解释的修订。"""
    db_path = tmp_path / "rec-auth.db"
    config = _config(db_path)
    command.upgrade(config, HEAD)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        assert "assessmentrecordingauthorization" in set(
            inspect(connection).get_table_names())

    # 无证据时允许降级到父修订,再升回。
    command.downgrade(config, "f7c2e8a4d105")
    with engine.begin() as connection:
        assert "assessmentrecordingauthorization" not in set(
            inspect(connection).get_table_names())
    command.upgrade(config, HEAD)

    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO patient (patient_id, is_simulation_subject, "
            "consent_status, recording_allowed, secondary_use_allowed) "
            "VALUES ('P-REC-AUTH', 1, '已同意', 1, 1)"))
        connection.execute(text(
            "INSERT INTO assessmentevent (event_id, patient_id, "
            "assigned_assessor_id, timepoint, scheduled_date, status, revision, "
            "is_simulation, data_classification, formal_outcome_eligible, "
            "definition_bundle_id, definition_bundle_digest, "
            "active_protocol_slot_key, created_by, created_at, updated_at) "
            "VALUES ('evt-rec-auth', 'P-REC-AUTH', 'A-1', 'pretest', "
            "'2026-08-08', 'due', 1, 1, 'simulation', 0, 'bundle-x', "
            "'sha256:" + "a" * 64 + "', 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd', 'A-1', '2026-08-08', "
            "'2026-08-08')"))
        connection.execute(text(
            "INSERT INTO assessmentinstance (instance_id, event_id, patient_id, "
            "category_key, definition_bundle_id, definition_bundle_digest, "
            "definition_id, instrument_id, instrument_version, "
            "definition_digest, item_set_digest, "
            "administration_protocol_digest, response_schema_digest, "
            "result_schema_digest, missingness_rule_digest, "
            "stopping_rule_digest, scoring_algorithm_id, "
            "scoring_algorithm_version, scoring_algorithm_digest, score_min, "
            "score_max, score_direction, score_rounding_rule, "
            "automatic_scoring_permitted, item_response_storage_permitted, "
            "result_storage_permitted, result_export_permitted, "
            "required_item_count, status, revision, is_simulation, "
            "data_classification, formal_outcome_eligible, created_at, "
            "updated_at) VALUES ('ins-rec-auth', 'evt-rec-auth', 'P-REC-AUTH', "
            "'untrained_standardized_naming', 'bundle-x', "
            "'sha256:" + "a" * 64 + "', 'def-x', 'inst-x', 'v1', "
            + ",".join(["'sha256:" + "b" * 64 + "'"] * 7) +
            ", 'alg-x', 'v1', 'sha256:" + "b" * 64 + "', 0, 10, "
            "'higher_is_better', 'integer_exact', 1, 1, 1, 0, 2, 'due', 1, 1, "
            "'simulation', 0, '2026-08-08', '2026-08-08')"))
        connection.execute(text(
            "INSERT INTO assessmentrecordingauthorization (authorization_id, "
            "event_id, instance_id, patient_id, item_key, item_revision, "
            "authorization_digest, issued_by, issued_at) VALUES "
            "('ara-x', 'evt-rec-auth', 'ins-rec-auth', 'P-REC-AUTH', "
            "'naming_01', 1, 'sha256:" + "c" * 64 + "', 'A-1', '2026-08-08')"))

    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="拒绝降级"):
        command.downgrade(config, "f7c2e8a4d105")
    with engine.begin() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar() == (
                RECORDING_AUTH_REVISION)
        assert connection.execute(text(
            "SELECT count(*) FROM assessmentrecordingauthorization"
        )).scalar() == 1
