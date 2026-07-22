"""add append-only turn confirmation revision ledger

Revision ID: d6a4f9b2c817
Revises: c1e5a7b9d203
Create Date: 2026-07-19

The ledger deliberately stores only before/after SHA-256 digests, never a
second copy of participant response text.  ``turnevent.confirmation_revision``
is the CAS cursor for the current authoritative value.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d6a4f9b2c817"
down_revision: Union[str, Sequence[str], None] = "c1e5a7b9d203"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("turnevent") as batch_op:
        batch_op.add_column(sa.Column(
            "confirmation_revision", sa.Integer(), nullable=False,
            server_default=sa.text("0")))
        batch_op.create_check_constraint(
            "ck_turn_event_confirmation_revision_nonnegative",
            "confirmation_revision >= 0")

    op.create_table(
        "turnconfirmationrevision",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("turn_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("actor_display_id", sa.String(), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("before_sha256", sa.String(), nullable=False),
        sa.Column("after_sha256", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.CheckConstraint(
            "expected_revision >= 0",
            name="ck_turn_confirmation_expected_revision_nonnegative"),
        sa.CheckConstraint(
            "revision = expected_revision + 1",
            name="ck_turn_confirmation_revision_transition"),
        sa.CheckConstraint(
            "length(before_sha256) = 64 AND length(after_sha256) = 64",
            name="ck_turn_confirmation_hash_lengths"),
        sa.CheckConstraint(
            "length(trim(actor_display_id)) > 0",
            name="ck_turn_confirmation_actor_nonempty"),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 8 AND 200",
            name="ck_turn_confirmation_idempotency_key_length"),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.ForeignKeyConstraint(["turn_id"], ["turnevent.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_turn_confirmation_idempotency_key"),
        sa.UniqueConstraint(
            "turn_id", "revision", name="uq_turn_confirmation_revision"),
    )
    op.create_index(
        op.f("ix_turnconfirmationrevision_session_id"),
        "turnconfirmationrevision", ["session_id"], unique=False)
    op.create_index(
        op.f("ix_turnconfirmationrevision_turn_id"),
        "turnconfirmationrevision", ["turn_id"], unique=False)


def downgrade() -> None:
    op.drop_index(
        op.f("ix_turnconfirmationrevision_turn_id"),
        table_name="turnconfirmationrevision")
    op.drop_index(
        op.f("ix_turnconfirmationrevision_session_id"),
        table_name="turnconfirmationrevision")
    op.drop_table("turnconfirmationrevision")
    with op.batch_alter_table("turnevent") as batch_op:
        batch_op.drop_constraint(
            "ck_turn_event_confirmation_revision_nonnegative", type_="check")
        batch_op.drop_column("confirmation_revision")
