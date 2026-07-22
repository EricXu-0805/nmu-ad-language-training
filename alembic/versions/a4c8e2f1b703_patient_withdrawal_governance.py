"""add authoritative subject withdrawal governance ledger

Revision ID: a4c8e2f1b703
Revises: f2b7d4e9a106
Create Date: 2026-07-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4c8e2f1b703"
down_revision: Union[str, Sequence[str], None] = "f2b7d4e9a106"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("patient") as batch_op:
        batch_op.add_column(sa.Column(
            "governance_revision", sa.Integer(), nullable=False,
            server_default=sa.text("0")))
        batch_op.create_check_constraint(
            "ck_patient_governance_revision_nonnegative",
            "governance_revision >= 0",
        )

    op.create_table(
        "patientwithdrawalevent",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("patient_id", sa.String(), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("new_revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key_sha256", sa.String(), nullable=False),
        sa.Column("request_fingerprint", sa.String(), nullable=False),
        sa.Column("reason_code", sa.String(), nullable=False),
        sa.Column("actor_display_id", sa.String(), nullable=False),
        sa.Column("actor_role", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("affected_session_count", sa.Integer(), nullable=False),
        sa.Column("affected_audio_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "reason_code IN ('participant_request','representative_request',"
            "'clinical_safety','ethics_or_protocol')",
            name="ck_patient_withdrawal_reason"),
        sa.CheckConstraint(
            "actor_role = 'admin'",
            name="ck_patient_withdrawal_actor_role"),
        sa.CheckConstraint(
            "expected_revision >= 0 AND new_revision = expected_revision + 1",
            name="ck_patient_withdrawal_revision_transition"),
        sa.CheckConstraint(
            "length(idempotency_key_sha256) = 64 AND "
            "length(request_fingerprint) = 64",
            name="ck_patient_withdrawal_hash_lengths"),
        sa.CheckConstraint(
            "length(trim(actor_display_id)) > 0",
            name="ck_patient_withdrawal_actor_nonempty"),
        sa.CheckConstraint(
            "affected_session_count >= 0 AND affected_audio_count >= 0",
            name="ck_patient_withdrawal_counts_nonnegative"),
        sa.ForeignKeyConstraint(["patient_id"], ["patient.patient_id"]),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "idempotency_key_sha256",
            name="uq_patient_withdrawal_idempotency_hash"),
        sa.UniqueConstraint(
            "patient_id", "new_revision",
            name="uq_patient_withdrawal_patient_revision"),
    )
    op.create_index(
        "ix_patientwithdrawalevent_patient_id", "patientwithdrawalevent",
        ["patient_id"], unique=False)
    op.create_index(
        "ix_patient_withdrawal_patient_occurred", "patientwithdrawalevent",
        ["patient_id", "occurred_at"], unique=False)


def downgrade() -> None:
    op.drop_index(
        "ix_patient_withdrawal_patient_occurred",
        table_name="patientwithdrawalevent")
    op.drop_index(
        "ix_patientwithdrawalevent_patient_id",
        table_name="patientwithdrawalevent")
    op.drop_table("patientwithdrawalevent")
    with op.batch_alter_table("patient") as batch_op:
        batch_op.drop_constraint(
            "ck_patient_governance_revision_nonnegative", type_="check")
        batch_op.drop_column("governance_revision")
