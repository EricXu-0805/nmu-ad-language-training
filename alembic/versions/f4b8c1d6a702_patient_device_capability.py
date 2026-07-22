"""session-bound patient device capabilities

Revision ID: f4b8c1d6a702
Revises: e3d7a9c5f204
Create Date: 2026-07-18

Only SHA-256 digests of the one-time bearer and random non-PII device id are
stored.  Existing patient, session, audio, and evidence rows are untouched.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 -- AutoString type


revision: str = "f4b8c1d6a702"
down_revision: Union[str, Sequence[str], None] = "e3d7a9c5f204"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "patientdevicecapability",
        sa.Column("token_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("device_id_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("active_session_key", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("recovery_only_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.PrimaryKeyConstraint("token_hash"),
        sa.UniqueConstraint(
            "active_session_key",
            name="uq_patient_device_capability_active_session"),
    )
    op.create_index(
        "ix_patientdevicecapability_session_id",
        "patientdevicecapability", ["session_id"], unique=False)


def downgrade() -> None:
    # Development-only rollback.  Production databases only migrate forward.
    op.drop_index(
        "ix_patientdevicecapability_session_id",
        table_name="patientdevicecapability")
    op.drop_table("patientdevicecapability")
