"""add immutable simulation-only autopilot plan profile bindings

Revision ID: e4a7c1d9b206
Revises: d3f8b5c1a704
Create Date: 2026-07-31

The empty pair remains the permanent representation of canonical full-source
plans and sessions.  This migration never guesses or backfills a profile for
historical rows.  A populated pair is restricted to simulation data, and a
profile-bound Session must retain its VisitPlan link.

Downgrade is fail-closed.  Both tables are inspected before any DDL begins; if
either profile column contains evidence, the schema and revision are left
untouched.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 -- AutoString type


revision: str = "e4a7c1d9b206"
down_revision: Union[str, Sequence[str], None] = "d3f8b5c1a704"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _hex64_sql(column: str) -> str:
    """Portable exact-lowercase-hex predicate; byte-identical to the model."""

    stripped = column
    for digit in "0123456789abcdef":
        stripped = f"replace({stripped}, '{digit}', '')"
    return f"(length({column}) = 64 AND {stripped} = '')"


_PROFILE_BINDING_CHECK = (
    "((autopilot_profile_version_id IS NULL AND "
    "autopilot_profile_definition_digest IS NULL) OR "
    "(autopilot_profile_version_id IS NOT NULL AND "
    "autopilot_profile_definition_digest IS NOT NULL AND "
    "length(trim(autopilot_profile_version_id)) > 0 AND "
    "autopilot_profile_version_id = trim(autopilot_profile_version_id) AND "
    + _hex64_sql("autopilot_profile_definition_digest") + "))"
)

_PROFILE_SIMULATION_CHECK = (
    "((autopilot_profile_version_id IS NULL AND "
    "autopilot_profile_definition_digest IS NULL) OR "
    "(autopilot_profile_version_id IS NOT NULL AND "
    "autopilot_profile_definition_digest IS NOT NULL AND "
    "is_simulation AND data_classification = 'simulation'))"
)

_PROFILE_SESSION_PLAN_LINK_CHECK = (
    "((autopilot_profile_version_id IS NULL AND "
    "autopilot_profile_definition_digest IS NULL) OR "
    "(autopilot_profile_version_id IS NOT NULL AND "
    "autopilot_profile_definition_digest IS NOT NULL AND "
    "visit_plan_id IS NOT NULL))"
)


def _add_profile_binding(
    table_name: str,
    *,
    binding_constraint: str,
    simulation_constraint: str,
    plan_link_constraint: str | None = None,
) -> None:
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.add_column(sa.Column(
            "autopilot_profile_version_id",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ))
        batch_op.add_column(sa.Column(
            "autopilot_profile_definition_digest",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ))
        batch_op.create_check_constraint(
            binding_constraint, _PROFILE_BINDING_CHECK)
        batch_op.create_check_constraint(
            simulation_constraint, _PROFILE_SIMULATION_CHECK)
        if plan_link_constraint is not None:
            batch_op.create_check_constraint(
                plan_link_constraint, _PROFILE_SESSION_PLAN_LINK_CHECK)


def _profile_evidence_tables() -> tuple[str, ...]:
    connection = op.get_bind()
    populated: list[str] = []
    for table_name in ("visitplan", "session"):
        statement = sa.text(
            f'SELECT 1 FROM "{table_name}" '
            "WHERE autopilot_profile_version_id IS NOT NULL "
            "OR autopilot_profile_definition_digest IS NOT NULL "
            "LIMIT 1"
        )
        if connection.execute(statement).first() is not None:
            populated.append(table_name)
    return tuple(populated)


def upgrade() -> None:
    """Add nullable, default-free profile evidence without backfilling."""

    _add_profile_binding(
        "visitplan",
        binding_constraint="ck_visit_plan_autopilot_profile_binding_complete",
        simulation_constraint="ck_visit_plan_autopilot_profile_simulation_only",
    )
    _add_profile_binding(
        "session",
        binding_constraint="ck_session_autopilot_profile_binding_complete",
        simulation_constraint="ck_session_autopilot_profile_simulation_only",
        plan_link_constraint="ck_session_autopilot_profile_requires_visit_plan",
    )


def downgrade() -> None:
    """Refuse evidence loss before starting either table rebuild."""

    populated = _profile_evidence_tables()
    if populated:
        raise RuntimeError(
            "autopilot profile binding evidence prevents downgrade: "
            + ",".join(populated)
        )

    with op.batch_alter_table("session") as batch_op:
        batch_op.drop_constraint(
            "ck_session_autopilot_profile_requires_visit_plan",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_session_autopilot_profile_simulation_only",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_session_autopilot_profile_binding_complete",
            type_="check",
        )
        batch_op.drop_column("autopilot_profile_definition_digest")
        batch_op.drop_column("autopilot_profile_version_id")

    with op.batch_alter_table("visitplan") as batch_op:
        batch_op.drop_constraint(
            "ck_visit_plan_autopilot_profile_simulation_only",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_visit_plan_autopilot_profile_binding_complete",
            type_="check",
        )
        batch_op.drop_column("autopilot_profile_definition_digest")
        batch_op.drop_column("autopilot_profile_version_id")
