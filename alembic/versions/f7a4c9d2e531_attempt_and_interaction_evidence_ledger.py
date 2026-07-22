"""attempt and interaction evidence ledger

Revision ID: f7a4c9d2e531
Revises: e6f3b1a8c204
Create Date: 2026-07-18

旧 Session/Audio 一律回填 legacy_unknown，不依 is_simulation=False 猜测为已确认研究数据。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "f7a4c9d2e531"
down_revision: Union[str, Sequence[str], None] = "e6f3b1a8c204"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("patient", sa.Column(
        "is_simulation_subject", sa.Boolean(), nullable=False,
        server_default=sa.false()))
    op.add_column("session", sa.Column(
        "data_classification", sqlmodel.sql.sqltypes.AutoString(), nullable=False,
        server_default="legacy_unknown"))
    op.add_column("audioassetrow", sa.Column(
        "data_classification", sqlmodel.sql.sqltypes.AutoString(), nullable=False,
        server_default="legacy_unknown"))

    op.create_table(
        "attemptevent",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("item_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("turn_seq", sa.Integer(), nullable=False),
        sa.Column("response_role", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("attempt_seq", sa.Integer(), nullable=False),
        sa.Column("raw_audio_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("prompt_level", sa.Integer(), nullable=False),
        sa.Column("cue_type", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("asr_text", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("asr_confidence", sa.Float(), nullable=True),
        sa.Column("asr_engine_version", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("operational_answer_type", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("operational_score", sa.Float(), nullable=True),
        sa.Column("operational_needs_review", sa.Boolean(), nullable=True),
        sa.Column("judge_mode", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("judge_engine_version", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("judge_reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("matched_on", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("contains_target", sa.Boolean(), nullable=True),
        sa.Column("judge_portrait_used", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("processing_status", sqlmodel.sql.sqltypes.AutoString(), nullable=False,
                  server_default="received"),
        sa.Column("error_code", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("is_simulation", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.CheckConstraint("attempt_seq >= 1", name="ck_attempt_seq_positive"),
        sa.CheckConstraint("prompt_level >= 0 AND prompt_level <= 3",
                           name="ck_attempt_prompt_level"),
        sa.CheckConstraint(
            "processing_status IN ('received','asr_completed','completed','technical_failure')",
            name="ck_attempt_processing_status"),
        sa.ForeignKeyConstraint(["raw_audio_id"], ["audioassetrow.raw_audio_id"]),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("raw_audio_id", name="uq_attempt_raw_audio_id"),
        sa.UniqueConstraint("session_id", "item_id", "turn_seq", "attempt_seq",
                            name="uq_attempt_session_item_turn_seq"),
    )
    op.create_index(op.f("ix_attemptevent_session_id"), "attemptevent", ["session_id"])
    op.create_index(op.f("ix_attemptevent_item_id"), "attemptevent", ["item_id"])
    op.create_index(op.f("ix_attemptevent_raw_audio_id"), "attemptevent", ["raw_audio_id"])

    op.create_table(
        "interactionevent",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("item_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("turn_seq", sa.Integer(), nullable=True),
        sa.Column("attempt_id", sa.Integer(), nullable=True),
        sa.Column("attempt_seq", sa.Integer(), nullable=True),
        sa.Column("event_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("payload_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False,
                  server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("is_simulation", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.CheckConstraint("event_seq >= 1", name="ck_interaction_event_seq_positive"),
        sa.CheckConstraint(
            "event_type IN ('attempt_received','asr_completed','asr_failed',"
            "'judgement_completed','judgement_failed','cue_selected',"
            "'feedback_selected','technical_pause','researcher_takeover')",
            name="ck_interaction_event_type"),
        sa.ForeignKeyConstraint(["attempt_id"], ["attemptevent.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "event_seq",
                            name="uq_interaction_session_event_seq"),
    )
    op.create_index(op.f("ix_interactionevent_session_id"), "interactionevent", ["session_id"])
    op.create_index(op.f("ix_interactionevent_item_id"), "interactionevent", ["item_id"])
    op.create_index(op.f("ix_interactionevent_attempt_id"), "interactionevent", ["attempt_id"])
    op.create_index(op.f("ix_interactionevent_event_type"), "interactionevent", ["event_type"])

    # Attempt 表存在后再给 TurnEvent 加权威来源引用。batch 兼容 SQLite。
    with op.batch_alter_table("turnevent") as batch_op:
        batch_op.add_column(sa.Column("source_attempt_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_turnevent_source_attempt_id_attemptevent",
            "attemptevent", ["source_attempt_id"], ["id"])
        batch_op.create_unique_constraint(
            "uq_turn_source_attempt_id", ["source_attempt_id"])
    op.create_index(op.f("ix_turnevent_source_attempt_id"), "turnevent", ["source_attempt_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_interactionevent_event_type"), table_name="interactionevent")
    op.drop_index(op.f("ix_interactionevent_attempt_id"), table_name="interactionevent")
    op.drop_index(op.f("ix_interactionevent_item_id"), table_name="interactionevent")
    op.drop_index(op.f("ix_interactionevent_session_id"), table_name="interactionevent")
    op.drop_table("interactionevent")
    op.drop_index(op.f("ix_turnevent_source_attempt_id"), table_name="turnevent")
    with op.batch_alter_table("turnevent") as batch_op:
        batch_op.drop_constraint("uq_turn_source_attempt_id", type_="unique")
        batch_op.drop_constraint(
            "fk_turnevent_source_attempt_id_attemptevent", type_="foreignkey")
        batch_op.drop_column("source_attempt_id")
    op.drop_index(op.f("ix_attemptevent_raw_audio_id"), table_name="attemptevent")
    op.drop_index(op.f("ix_attemptevent_item_id"), table_name="attemptevent")
    op.drop_index(op.f("ix_attemptevent_session_id"), table_name="attemptevent")
    op.drop_table("attemptevent")
    op.drop_column("audioassetrow", "data_classification")
    op.drop_column("session", "data_classification")
    op.drop_column("patient", "is_simulation_subject")
