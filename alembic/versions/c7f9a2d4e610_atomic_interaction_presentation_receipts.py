"""durable atomic interaction-presentation receipts

Revision ID: c7f9a2d4e610
Revises: b5d1f7a9c304
Create Date: 2026-07-19

The immutable interaction ledger remains unchanged.  This companion table
stores the exact server receipt required for idempotent HTTP replay and applies
a database-level one-terminal-presentation-per-attempt fence for new writes.
Historical interaction rows are deliberately not invented or rewritten.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "c7f9a2d4e610"
down_revision: Union[str, Sequence[str], None] = "b5d1f7a9c304"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "interactionpresentationreceipt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("interaction_event_id", sa.Integer(), nullable=False),
        sa.Column("attempt_id", sa.Integer(), nullable=True),
        sa.Column(
            "idempotency_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "request_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "cursor_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("live_seq", sa.Integer(), nullable=False),
        sa.Column("command_wseq", sa.Integer(), nullable=False),
        sa.Column("runtime_revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "length(trim(idempotency_key)) BETWEEN 16 AND 200",
            name="ck_interaction_presentation_idempotency_length"),
        sa.CheckConstraint(
            "length(request_hash) = 64 AND lower(request_hash) = request_hash",
            name="ck_interaction_presentation_request_hash"),
        sa.CheckConstraint(
            "live_seq >= 1",
            name="ck_interaction_presentation_live_seq_positive"),
        sa.CheckConstraint(
            "command_wseq >= 0",
            name="ck_interaction_presentation_wseq_nonnegative"),
        sa.CheckConstraint(
            "runtime_revision >= 1",
            name="ck_interaction_presentation_runtime_revision_positive"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["session.session_id"]),
        sa.ForeignKeyConstraint(
            ["interaction_event_id"], ["interactionevent.id"]),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["attemptevent.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "idempotency_key",
            name="uq_interaction_presentation_session_idempotency"),
        sa.UniqueConstraint(
            "interaction_event_id",
            name="uq_interaction_presentation_event"),
        sa.UniqueConstraint(
            "attempt_id",
            name="uq_interaction_presentation_attempt"),
    )
    op.create_index(
        op.f("ix_interactionpresentationreceipt_session_id"),
        "interactionpresentationreceipt", ["session_id"])
    op.create_index(
        op.f("ix_interactionpresentationreceipt_interaction_event_id"),
        "interactionpresentationreceipt", ["interaction_event_id"])
    op.create_index(
        op.f("ix_interactionpresentationreceipt_attempt_id"),
        "interactionpresentationreceipt", ["attempt_id"])
    op.create_index(
        op.f("ix_interactionpresentationreceipt_idempotency_key"),
        "interactionpresentationreceipt", ["idempotency_key"])


def downgrade() -> None:
    op.drop_index(
        op.f("ix_interactionpresentationreceipt_idempotency_key"),
        table_name="interactionpresentationreceipt")
    op.drop_index(
        op.f("ix_interactionpresentationreceipt_attempt_id"),
        table_name="interactionpresentationreceipt")
    op.drop_index(
        op.f("ix_interactionpresentationreceipt_interaction_event_id"),
        table_name="interactionpresentationreceipt")
    op.drop_index(
        op.f("ix_interactionpresentationreceipt_session_id"),
        table_name="interactionpresentationreceipt")
    op.drop_table("interactionpresentationreceipt")
