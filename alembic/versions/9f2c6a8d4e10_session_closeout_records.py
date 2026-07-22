"""add immutable session outcome summaries and closeout reports

Revision ID: 9f2c6a8d4e10
Revises: 7e4b2c9d1a60
Create Date: 2026-07-19

Both tables are additive and one-to-one with an existing session.  No legacy
facts are invented or backfilled by this migration.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 -- AutoString type


revision: str = "9f2c6a8d4e10"
down_revision: Union[str, Sequence[str], None] = "7e4b2c9d1a60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sessionoutcomesummary",
        sa.Column(
            "session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "schema_version", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "generator_version", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "item_bank_version_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("is_simulation", sa.Boolean(), nullable=False),
        sa.Column(
            "data_classification",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
        ),
        sa.Column("expected_turns", sa.Integer(), nullable=False),
        sa.Column("matched_turns", sa.Integer(), nullable=False),
        sa.Column("completed_attempt_turns", sa.Integer(), nullable=False),
        sa.Column("audio_evidenced_turns", sa.Integer(), nullable=False),
        sa.Column("total_attempts", sa.Integer(), nullable=False),
        sa.Column("completed_attempts", sa.Integer(), nullable=False),
        sa.Column("needs_review_attempts", sa.Integer(), nullable=False),
        sa.Column("technical_failure_attempts", sa.Integer(), nullable=False),
        sa.Column("prompt_level_0_count", sa.Integer(), nullable=False),
        sa.Column("prompt_level_1_count", sa.Integer(), nullable=False),
        sa.Column("prompt_level_2_count", sa.Integer(), nullable=False),
        sa.Column("prompt_level_3_count", sa.Integer(), nullable=False),
        sa.Column("technical_pause_count", sa.Integer(), nullable=False),
        sa.Column("researcher_takeover_count", sa.Integer(), nullable=False),
        sa.Column(
            "source_digest", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "data_classification IN ('research','simulation')",
            name="ck_session_outcome_summary_classification",
        ),
        sa.CheckConstraint(
            "((is_simulation AND data_classification = 'simulation') OR "
            "((NOT is_simulation) AND data_classification = 'research'))",
            name="ck_session_outcome_summary_simulation_boundary",
        ),
        sa.CheckConstraint(
            "expected_turns >= 0 AND matched_turns >= 0 AND "
            "completed_attempt_turns >= 0 AND audio_evidenced_turns >= 0 AND "
            "total_attempts >= 0 AND completed_attempts >= 0 AND "
            "needs_review_attempts >= 0 AND technical_failure_attempts >= 0 AND "
            "prompt_level_0_count >= 0 AND prompt_level_1_count >= 0 AND "
            "prompt_level_2_count >= 0 AND prompt_level_3_count >= 0 AND "
            "technical_pause_count >= 0 AND researcher_takeover_count >= 0",
            name="ck_session_outcome_summary_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "matched_turns <= expected_turns AND "
            "completed_attempt_turns <= matched_turns AND "
            "audio_evidenced_turns <= matched_turns",
            name="ck_session_outcome_summary_turn_count_order",
        ),
        sa.CheckConstraint(
            "completed_attempts <= total_attempts AND "
            "needs_review_attempts <= total_attempts AND "
            "technical_failure_attempts <= total_attempts",
            name="ck_session_outcome_summary_attempt_count_order",
        ),
        sa.CheckConstraint(
            "prompt_level_0_count + prompt_level_1_count + "
            "prompt_level_2_count + prompt_level_3_count = total_attempts",
            name="ck_session_outcome_summary_prompt_total",
        ),
        sa.CheckConstraint(
            "length(schema_version) BETWEEN 1 AND 128 AND "
            "length(generator_version) BETWEEN 1 AND 128 AND "
            "length(item_bank_version_id) BETWEEN 1 AND 256",
            name="ck_session_outcome_summary_versions_nonempty",
        ),
        sa.CheckConstraint(
            "length(source_digest) = 64",
            name="ck_session_outcome_summary_digest_length",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.PrimaryKeyConstraint("session_id"),
    )

    op.create_table(
        "sessioncloseoutreport",
        sa.Column(
            "session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "schema_version", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("fatigue_observed", sa.Boolean(), nullable=False),
        sa.Column(
            "distress_or_discomfort_observed", sa.Boolean(), nullable=False),
        sa.Column(
            "participant_declined_to_continue", sa.Boolean(), nullable=False),
        sa.Column("staff_assistance_occurred", sa.Boolean(), nullable=False),
        sa.Column(
            "environment_interruption_occurred", sa.Boolean(), nullable=False),
        sa.Column(
            "device_or_network_interruption_occurred",
            sa.Boolean(),
            nullable=False,
        ),
        sa.Column("note", sa.String(length=2000), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "last_idempotency_key",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
        ),
        sa.Column(
            "last_request_hash",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
        ),
        sa.Column("created_by", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("locked_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('no_additional_observation','observation_recorded')",
            name="ck_session_closeout_report_status",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_session_closeout_report_revision_positive",
        ),
        sa.CheckConstraint(
            "length(schema_version) BETWEEN 1 AND 128",
            name="ck_session_closeout_report_schema_version",
        ),
        sa.CheckConstraint(
            "length(last_idempotency_key) BETWEEN 1 AND 200 AND "
            "length(last_request_hash) = 64",
            name="ck_session_closeout_report_idempotency_metadata",
        ),
        sa.CheckConstraint(
            "length(trim(created_by)) > 0 AND length(trim(updated_by)) > 0",
            name="ck_session_closeout_report_actor_nonempty",
        ),
        sa.CheckConstraint(
            "note IS NULL OR (length(note) <= 2000 AND length(trim(note)) > 0)",
            name="ck_session_closeout_report_note",
        ),
        sa.CheckConstraint(
            "status != 'no_additional_observation' OR "
            "((NOT fatigue_observed) AND "
            "(NOT distress_or_discomfort_observed) AND "
            "(NOT participant_declined_to_continue) AND "
            "(NOT staff_assistance_occurred) AND "
            "(NOT environment_interruption_occurred) AND "
            "(NOT device_or_network_interruption_occurred) AND note IS NULL)",
            name="ck_session_closeout_report_no_observation_empty",
        ),
        sa.CheckConstraint(
            "status != 'observation_recorded' OR "
            "(fatigue_observed OR distress_or_discomfort_observed OR "
            "participant_declined_to_continue OR staff_assistance_occurred OR "
            "environment_interruption_occurred OR "
            "device_or_network_interruption_occurred OR "
            "(note IS NOT NULL AND length(trim(note)) > 0))",
            name="ck_session_closeout_report_observation_present",
        ),
        sa.CheckConstraint(
            "((locked_by IS NULL AND locked_at IS NULL) OR "
            "(locked_by IS NOT NULL AND locked_at IS NOT NULL))",
            name="ck_session_closeout_report_lock_tuple",
        ),
        sa.CheckConstraint(
            "locked_by IS NULL OR length(trim(locked_by)) > 0",
            name="ck_session_closeout_report_locker_nonempty",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.PrimaryKeyConstraint("session_id"),
    )


def downgrade() -> None:
    op.drop_table("sessioncloseoutreport")
    op.drop_table("sessionoutcomesummary")
