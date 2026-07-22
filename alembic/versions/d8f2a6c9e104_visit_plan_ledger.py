"""add visit plan scheduling and append-only command ledger

Revision ID: d8f2a6c9e104
Revises: 8c3d7e1a5b20
Create Date: 2026-07-19

The migration is additive.  Existing sessions retain a NULL ``visit_plan_id``;
only sessions started from the new plan workflow receive the unique back-link.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import sqlmodel  # noqa: F401 -- AutoString type


revision: str = "d8f2a6c9e104"
down_revision: Union[str, Sequence[str], None] = "8c3d7e1a5b20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Reuse the enum types created by the baseline migration on PostgreSQL.  The
# PostgreSQL enum class also compiles to VARCHAR on SQLite, preserving the local
# and test migration path without attempting to recreate an existing PG type.
_PHASE_TYPE = postgresql.ENUM(
    "关系建立", "基线测评", "正式训练", "前测", "后测", "随访", "探针测评",
    name="phasetype",
    create_type=False,
)
_EVENT_LINE = postgresql.ENUM(
    "关系建立环节", "基线测评窗", "正式训练",
    name="eventline",
    create_type=False,
)


def upgrade() -> None:
    op.create_table(
        "visitplan",
        sa.Column(
            "plan_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "protocol_slot_key", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column(
            "patient_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("scheduled_date", sa.Date(), nullable=False),
        sa.Column("scheduled_time", sa.Time(), nullable=True),
        sa.Column("queue_order", sa.Integer(), nullable=True),
        sa.Column("session_sitting_no", sa.Integer(), nullable=False),
        sa.Column("week_no", sa.Integer(), nullable=False),
        sa.Column("phase_type", _PHASE_TYPE, nullable=False),
        sa.Column("event_line", _EVENT_LINE, nullable=False),
        sa.Column(
            "item_bank_version_id",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
        ),
        sa.Column("is_simulation", sa.Boolean(), nullable=False),
        sa.Column(
            "data_classification",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
        ),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("approved_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("started_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft','approved','started','cancelled')",
            name="ck_visit_plan_status",
        ),
        sa.CheckConstraint(
            "revision >= 1", name="ck_visit_plan_revision_positive"),
        sa.CheckConstraint(
            "status = 'cancelled' OR protocol_slot_key IS NOT NULL",
            name="ck_visit_plan_active_slot",
        ),
        sa.CheckConstraint(
            "queue_order IS NULL OR (queue_order >= 0 AND queue_order <= 100000)",
            name="ck_visit_plan_queue_order_range",
        ),
        sa.CheckConstraint(
            "session_sitting_no >= 1", name="ck_visit_plan_sitting_positive"),
        sa.CheckConstraint(
            "week_no >= 1 AND week_no <= 8", name="ck_visit_plan_week"),
        sa.CheckConstraint(
            "data_classification IN ('research','simulation')",
            name="ck_visit_plan_data_classification",
        ),
        sa.ForeignKeyConstraint(["patient_id"], ["patient.patient_id"]),
        sa.PrimaryKeyConstraint("plan_id"),
        sa.UniqueConstraint(
            "protocol_slot_key", name="uq_visit_plan_protocol_slot_key"),
    )
    op.create_index(
        op.f("ix_visitplan_patient_id"),
        "visitplan",
        ["patient_id"],
        unique=False,
    )
    op.create_index(
        "ix_visit_plan_queue",
        "visitplan",
        ["status", "scheduled_date", "queue_order", "scheduled_time"],
        unique=False,
    )
    op.create_index(
        "ix_visit_plan_patient_status",
        "visitplan",
        ["patient_id", "status"],
        unique=False,
    )

    op.create_table(
        "visitplancommand",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plan_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column(
            "idempotency_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("command_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("request_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("actor_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("resulting_revision", sa.Integer(), nullable=False),
        sa.Column("reason_code", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "command_type IN ('create','approve','start','cancel')",
            name="ck_visit_plan_command_type",
        ),
        sa.CheckConstraint(
            "event_seq >= 1", name="ck_visit_plan_command_event_seq_positive"),
        sa.CheckConstraint(
            "expected_revision >= 0",
            name="ck_visit_plan_command_expected_revision_nonnegative",
        ),
        sa.CheckConstraint(
            "resulting_revision = expected_revision + 1",
            name="ck_visit_plan_command_revision_transition",
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["visitplan.plan_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_visit_plan_command_idempotency_key",
        ),
        sa.UniqueConstraint(
            "plan_id", "event_seq",
            name="uq_visit_plan_command_plan_event_seq",
        ),
    )
    op.create_index(
        op.f("ix_visitplancommand_plan_id"),
        "visitplancommand",
        ["plan_id"],
        unique=False,
    )
    op.create_index(
        "ix_visit_plan_command_plan_created",
        "visitplancommand",
        ["plan_id", "created_at"],
        unique=False,
    )

    # Batch mode rebuilds SQLite's session table safely.  The nullable column is
    # intentionally default-free, so every legacy/direct session remains NULL.
    with op.batch_alter_table("session") as batch_op:
        batch_op.add_column(sa.Column(
            "visit_plan_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.create_foreign_key(
            "fk_session_visit_plan_id_visitplan",
            "visitplan",
            ["visit_plan_id"],
            ["plan_id"],
        )
        batch_op.create_unique_constraint(
            "uq_session_visit_plan_id", ["visit_plan_id"])


def downgrade() -> None:
    with op.batch_alter_table("session") as batch_op:
        batch_op.drop_constraint("uq_session_visit_plan_id", type_="unique")
        batch_op.drop_constraint(
            "fk_session_visit_plan_id_visitplan", type_="foreignkey")
        batch_op.drop_column("visit_plan_id")

    op.drop_index(
        "ix_visit_plan_command_plan_created", table_name="visitplancommand")
    op.drop_index(
        op.f("ix_visitplancommand_plan_id"), table_name="visitplancommand")
    op.drop_table("visitplancommand")

    op.drop_index("ix_visit_plan_patient_status", table_name="visitplan")
    op.drop_index("ix_visit_plan_queue", table_name="visitplan")
    op.drop_index(op.f("ix_visitplan_patient_id"), table_name="visitplan")
    op.drop_table("visitplan")
