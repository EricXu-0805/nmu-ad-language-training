"""atomic researcher technical-pause receipts

Revision ID: e8a1c4b7d902
Revises: c7f9a2d4e610
Create Date: 2026-07-19

The interaction ledger remains immutable.  This companion table binds one
technical-pause event to the exact active runtime/live snapshot consumed by the
same stop transaction and provides durable exact-replay semantics.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "e8a1c4b7d902"
down_revision: Union[str, Sequence[str], None] = "c7f9a2d4e610"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "technicalpausereceipt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("interaction_event_id", sa.Integer(), nullable=False),
        sa.Column(
            "idempotency_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "request_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("expected_runtime_revision", sa.Integer(), nullable=False),
        sa.Column("expected_live_wseq", sa.Integer(), nullable=False),
        sa.Column("runtime_revision", sa.Integer(), nullable=False),
        sa.Column("paused_cursor_wseq", sa.Integer(), nullable=False),
        sa.Column("live_seq", sa.Integer(), nullable=False),
        sa.Column(
            "cursor_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "length(trim(idempotency_key)) BETWEEN 16 AND 200",
            name="ck_technical_pause_idempotency_length"),
        sa.CheckConstraint(
            "length(request_hash) = 64 AND lower(request_hash) = request_hash",
            name="ck_technical_pause_request_hash"),
        sa.CheckConstraint(
            "expected_runtime_revision >= 0 AND runtime_revision >= 1",
            name="ck_technical_pause_runtime_revisions"),
        sa.CheckConstraint(
            "expected_live_wseq >= 0 AND paused_cursor_wseq >= 0",
            name="ck_technical_pause_wseqs_nonnegative"),
        sa.CheckConstraint(
            "live_seq >= 1",
            name="ck_technical_pause_live_seq_positive"),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.ForeignKeyConstraint(
            ["interaction_event_id"], ["interactionevent.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "idempotency_key",
            name="uq_technical_pause_session_idempotency"),
        sa.UniqueConstraint(
            "interaction_event_id",
            name="uq_technical_pause_interaction_event"),
        sa.UniqueConstraint(
            "session_id", "expected_runtime_revision", "expected_live_wseq",
            name="uq_technical_pause_source_snapshot"),
    )
    op.create_index(
        op.f("ix_technicalpausereceipt_session_id"),
        "technicalpausereceipt", ["session_id"])
    op.create_index(
        op.f("ix_technicalpausereceipt_interaction_event_id"),
        "technicalpausereceipt", ["interaction_event_id"])
    op.create_index(
        op.f("ix_technicalpausereceipt_idempotency_key"),
        "technicalpausereceipt", ["idempotency_key"])


def downgrade() -> None:
    op.drop_index(
        op.f("ix_technicalpausereceipt_idempotency_key"),
        table_name="technicalpausereceipt")
    op.drop_index(
        op.f("ix_technicalpausereceipt_interaction_event_id"),
        table_name="technicalpausereceipt")
    op.drop_index(
        op.f("ix_technicalpausereceipt_session_id"),
        table_name="technicalpausereceipt")
    op.drop_table("technicalpausereceipt")
