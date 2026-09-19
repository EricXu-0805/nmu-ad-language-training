"""append-only researcher adjudications of autopilot positions（现场裁定收据）

Revision ID: f4b2d8c1a635
Revises: e2a6d8f0b419
Create Date: 2026-09-19

养老院实测(2026-09-17)后研究者要能不开口就裁定一题:老人其实答对(超时后/识别错)、
或跳过本题。裁定具名、带闭集原因、只追加;AI 判类与研究真值(事后锁分)都不动。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "f4b2d8c1a635"
down_revision: Union[str, Sequence[str], None] = "e2a6d8f0b419"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "autopilotpositionadjudication",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("item_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("turn_seq", sa.Integer(), nullable=False),
        sa.Column("kind", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("reason_code", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("note", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("actor_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_attempt_id", sa.Integer(), nullable=True),
        sa.Column("turn_event_id", sa.Integer(), nullable=True),
        sa.Column("control_generation", sa.Integer(), nullable=False),
        sa.Column("state_revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("is_simulation", sa.Boolean(), nullable=False),
        sa.CheckConstraint("turn_seq >= 1", name="ck_position_adjudication_turn_positive"),
        sa.CheckConstraint(
            "state_revision >= 0",
            name="ck_position_adjudication_revision_nonnegative"),
        sa.CheckConstraint(
            "kind IN ('confirmed_correct','terminated_no_verdict','skipped')",
            name="ck_position_adjudication_kind"),
        sa.CheckConstraint(
            "reason_code IN ('late_correct_after_window','asr_misrecognized',"
            "'staff_judged_correct','participant_declined','asr_repeatedly_failed',"
            "'trained_in_prior_sitting','other')",
            name="ck_position_adjudication_reason"),
        sa.CheckConstraint(
            "(kind = 'skipped' AND source_attempt_id IS NULL AND turn_event_id IS NULL)"
            " OR (kind <> 'skipped' AND source_attempt_id IS NOT NULL"
            " AND turn_event_id IS NOT NULL)",
            name="ck_position_adjudication_evidence_matches_kind"),
        sa.CheckConstraint(
            "length(trim(actor_id)) > 0",
            name="ck_position_adjudication_actor_nonempty"),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 8 AND 128",
            name="ck_position_adjudication_idempotency_length"),
        sa.CheckConstraint(
            "note IS NULL OR length(note) <= 200",
            name="ck_position_adjudication_note_length"),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.ForeignKeyConstraint(["source_attempt_id"], ["attemptevent.id"]),
        sa.ForeignKeyConstraint(["turn_event_id"], ["turnevent.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "item_id", "turn_seq",
            name="uq_position_adjudication_position"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_position_adjudication_idempotency"),
        sa.UniqueConstraint(
            "turn_event_id", name="uq_position_adjudication_turn_event"),
    )
    op.create_index(
        "ix_position_adjudication_session_created",
        "autopilotpositionadjudication", ["session_id", "created_at"])
    op.create_index(
        op.f("ix_autopilotpositionadjudication_session_id"),
        "autopilotpositionadjudication", ["session_id"])
    op.create_index(
        op.f("ix_autopilotpositionadjudication_item_id"),
        "autopilotpositionadjudication", ["item_id"])
    op.create_index(
        op.f("ix_autopilotpositionadjudication_actor_id"),
        "autopilotpositionadjudication", ["actor_id"])


def downgrade() -> None:
    op.drop_index(
        op.f("ix_autopilotpositionadjudication_actor_id"),
        table_name="autopilotpositionadjudication")
    op.drop_index(
        op.f("ix_autopilotpositionadjudication_item_id"),
        table_name="autopilotpositionadjudication")
    op.drop_index(
        op.f("ix_autopilotpositionadjudication_session_id"),
        table_name="autopilotpositionadjudication")
    op.drop_index(
        "ix_position_adjudication_session_created",
        table_name="autopilotpositionadjudication")
    op.drop_table("autopilotpositionadjudication")
