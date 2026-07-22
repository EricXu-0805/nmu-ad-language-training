"""bind plans, sessions and commands to complete content definitions

Revision ID: b5d1f7a9c304
Revises: a4c8e2f1b703
Create Date: 2026-07-19

All columns are deliberately nullable and default-free.  Historical rows are
not assigned a definition they never actually used; the service layer rejects
those rows at approval/start/automatic semantic execution.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 -- AutoString type


revision: str = "b5d1f7a9c304"
down_revision: Union[str, Sequence[str], None] = "a4c8e2f1b703"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PLAN_SESSION_BINDING_CHECK = (
    "((item_bank_definition_digest IS NULL AND "
    "autopilot_protocol_version_id IS NULL AND "
    "autopilot_protocol_definition_digest IS NULL) OR "
    "(length(item_bank_definition_digest) = 64 AND "
    "lower(item_bank_definition_digest) = item_bank_definition_digest AND "
    "length(trim(autopilot_protocol_version_id)) > 0 AND "
    "length(autopilot_protocol_definition_digest) = 64 AND "
    "lower(autopilot_protocol_definition_digest) = "
    "autopilot_protocol_definition_digest))"
)

_COMMAND_BINDING_CHECK = (
    "((item_bank_version_id IS NULL AND "
    "item_bank_definition_digest IS NULL AND "
    "autopilot_protocol_version_id IS NULL AND "
    "autopilot_protocol_definition_digest IS NULL AND "
    "response_role IS NULL) OR "
    "(length(trim(item_bank_version_id)) > 0 AND "
    "length(item_bank_definition_digest) = 64 AND "
    "lower(item_bank_definition_digest) = item_bank_definition_digest AND "
    "length(trim(autopilot_protocol_version_id)) > 0 AND "
    "length(autopilot_protocol_definition_digest) = 64 AND "
    "lower(autopilot_protocol_definition_digest) = "
    "autopilot_protocol_definition_digest AND "
    "length(trim(response_role)) > 0))"
)


def _add_plan_or_session_binding(table_name: str, constraint_name: str) -> None:
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.add_column(sa.Column(
            "item_bank_definition_digest",
            sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column(
            "autopilot_protocol_version_id",
            sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column(
            "autopilot_protocol_definition_digest",
            sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.create_check_constraint(
            constraint_name, _PLAN_SESSION_BINDING_CHECK)


def upgrade() -> None:
    _add_plan_or_session_binding(
        "visitplan", "ck_visit_plan_definition_binding_complete")
    _add_plan_or_session_binding(
        "session", "ck_session_definition_binding_complete")

    with op.batch_alter_table("runtimecommand") as batch_op:
        batch_op.add_column(sa.Column(
            "item_bank_version_id",
            sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column(
            "item_bank_definition_digest",
            sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column(
            "autopilot_protocol_version_id",
            sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column(
            "autopilot_protocol_definition_digest",
            sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column(
            "response_role",
            sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.create_check_constraint(
            "ck_runtime_command_definition_binding_complete",
            _COMMAND_BINDING_CHECK,
        )


def _drop_plan_or_session_binding(table_name: str, constraint_name: str) -> None:
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.drop_constraint(constraint_name, type_="check")
        batch_op.drop_column("autopilot_protocol_definition_digest")
        batch_op.drop_column("autopilot_protocol_version_id")
        batch_op.drop_column("item_bank_definition_digest")


def downgrade() -> None:
    with op.batch_alter_table("runtimecommand") as batch_op:
        batch_op.drop_constraint(
            "ck_runtime_command_definition_binding_complete", type_="check")
        batch_op.drop_column("response_role")
        batch_op.drop_column("autopilot_protocol_definition_digest")
        batch_op.drop_column("autopilot_protocol_version_id")
        batch_op.drop_column("item_bank_definition_digest")
        batch_op.drop_column("item_bank_version_id")

    _drop_plan_or_session_binding(
        "session", "ck_session_definition_binding_complete")
    _drop_plan_or_session_binding(
        "visitplan", "ck_visit_plan_definition_binding_complete")
