"""patient-requested safety-pause evidence and exact replay receipts

Revision ID: a9d2e6f4c108
Revises: b8e5f2a91c07
Create Date: 2026-08-12

The patient action is a pause only.  It neither aborts the visit nor records a
study withdrawal.  One append-only InteractionEvent and one digest-only
receipt are committed with the authoritative runtime/live stop.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "a9d2e6f4c108"
down_revision: Union[str, Sequence[str], None] = "b8e5f2a91c07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OLD_EVENT_TYPES = (
    "event_type IN ('attempt_received','asr_completed','asr_failed',"
    "'judgement_completed','judgement_failed','cue_selected',"
    "'feedback_selected','technical_pause','researcher_takeover')"
)
_NEW_EVENT_TYPES = (
    "event_type IN ('attempt_received','asr_completed','asr_failed',"
    "'judgement_completed','judgement_failed','cue_selected',"
    "'feedback_selected','technical_pause','researcher_takeover',"
    "'patient_requested_pause')"
)


def _hex64_sql(column: str) -> str:
    stripped = column
    for digit in "0123456789abcdef":
        stripped = f"replace({stripped}, '{digit}', '')"
    return f"(length({column}) = 64 AND {stripped} = '')"


def upgrade() -> None:
    with op.batch_alter_table("interactionevent") as batch_op:
        batch_op.drop_constraint("ck_interaction_event_type", type_="check")
        batch_op.create_check_constraint(
            "ck_interaction_event_type", _NEW_EVENT_TYPES)

    op.create_table(
        "patientpausereceipt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("interaction_event_id", sa.Integer(), nullable=False),
        sa.Column(
            "idempotency_key_sha256",
            sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "request_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "capability_token_hash",
            sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("runtime_revision", sa.Integer(), nullable=False),
        sa.Column("live_seq", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            _hex64_sql("idempotency_key_sha256"),
            name="ck_patient_pause_idempotency_hash"),
        sa.CheckConstraint(
            _hex64_sql("request_hash"),
            name="ck_patient_pause_request_hash"),
        sa.CheckConstraint(
            _hex64_sql("capability_token_hash"),
            name="ck_patient_pause_capability_hash"),
        sa.CheckConstraint(
            "runtime_revision >= 1",
            name="ck_patient_pause_runtime_revision_positive"),
        sa.CheckConstraint(
            "live_seq >= 1",
            name="ck_patient_pause_live_seq_positive"),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.ForeignKeyConstraint(
            ["interaction_event_id"], ["interactionevent.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "idempotency_key_sha256",
            name="uq_patient_pause_session_idempotency"),
        sa.UniqueConstraint(
            "interaction_event_id",
            name="uq_patient_pause_interaction_event"),
    )
    op.create_index(
        op.f("ix_patientpausereceipt_session_id"),
        "patientpausereceipt", ["session_id"])
    op.create_index(
        op.f("ix_patientpausereceipt_interaction_event_id"),
        "patientpausereceipt", ["interaction_event_id"])
    op.create_index(
        op.f("ix_patientpausereceipt_idempotency_key_sha256"),
        "patientpausereceipt", ["idempotency_key_sha256"])


def downgrade() -> None:
    bind = op.get_bind()
    receipt = bind.execute(sa.text(
        "SELECT 1 FROM patientpausereceipt LIMIT 1")).first()
    event = bind.execute(sa.text(
        "SELECT 1 FROM interactionevent "
        "WHERE event_type = 'patient_requested_pause' LIMIT 1")).first()
    if receipt is not None or event is not None:
        raise RuntimeError(
            "patient pause 已有只追加证据，拒绝降级抹掉安全事实")

    op.drop_index(
        op.f("ix_patientpausereceipt_idempotency_key_sha256"),
        table_name="patientpausereceipt")
    op.drop_index(
        op.f("ix_patientpausereceipt_interaction_event_id"),
        table_name="patientpausereceipt")
    op.drop_index(
        op.f("ix_patientpausereceipt_session_id"),
        table_name="patientpausereceipt")
    op.drop_table("patientpausereceipt")

    with op.batch_alter_table("interactionevent") as batch_op:
        batch_op.drop_constraint("ck_interaction_event_type", type_="check")
        batch_op.create_check_constraint(
            "ck_interaction_event_type", _OLD_EVENT_TYPES)
