"""append-only caregiver help-request receipts

Revision ID: b3e7c5a9d214
Revises: a9d2e6f4c108
Create Date: 2026-08-13

The named caregiver action pauses an owned bedside session and records only a
closed reason code.  It is intentionally separate from the device-only
patient-requested pause evidence.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "b3e7c5a9d214"
down_revision: Union[str, Sequence[str], None] = "a9d2e6f4c108"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _hex64_sql(column: str) -> str:
    stripped = column
    for digit in "0123456789abcdef":
        stripped = f"replace({stripped}, '{digit}', '')"
    return f"(length({column}) = 64 AND {stripped} = '')"


def upgrade() -> None:
    op.create_table(
        "caregiverhelprequest",
        sa.Column(
            "request_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "actor_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "reason_code", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "idempotency_key_sha256",
            sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "request_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("runtime_revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            _hex64_sql("idempotency_key_sha256"),
            name="ck_caregiver_help_idempotency_hash"),
        sa.CheckConstraint(
            _hex64_sql("request_hash"),
            name="ck_caregiver_help_request_hash"),
        sa.CheckConstraint(
            "reason_code IN ('participant_distress','participant_request',"
            "'clinical_concern','technical_failure','other_staff_needed')",
            name="ck_caregiver_help_reason_closed"),
        sa.CheckConstraint(
            "runtime_revision >= 0",
            name="ck_caregiver_help_runtime_revision_nonnegative"),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.PrimaryKeyConstraint("request_id"),
        sa.UniqueConstraint(
            "session_id", "idempotency_key_sha256",
            name="uq_caregiver_help_session_idempotency"),
    )
    op.create_index(
        op.f("ix_caregiverhelprequest_session_id"),
        "caregiverhelprequest", ["session_id"])
    op.create_index(
        op.f("ix_caregiverhelprequest_actor_id"),
        "caregiverhelprequest", ["actor_id"])
    op.create_index(
        op.f("ix_caregiverhelprequest_idempotency_key_sha256"),
        "caregiverhelprequest", ["idempotency_key_sha256"])


def downgrade() -> None:
    bind = op.get_bind()
    evidence = bind.execute(sa.text(
        "SELECT 1 FROM caregiverhelprequest LIMIT 1")).first()
    if evidence is not None:
        raise RuntimeError(
            "caregiver help request 已有只追加证据，拒绝降级抹掉床旁呼叫事实")

    op.drop_index(
        op.f("ix_caregiverhelprequest_idempotency_key_sha256"),
        table_name="caregiverhelprequest")
    op.drop_index(
        op.f("ix_caregiverhelprequest_actor_id"),
        table_name="caregiverhelprequest")
    op.drop_index(
        op.f("ix_caregiverhelprequest_session_id"),
        table_name="caregiverhelprequest")
    op.drop_table("caregiverhelprequest")
