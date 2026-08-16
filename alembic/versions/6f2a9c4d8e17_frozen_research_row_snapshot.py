"""freeze research dataset rows inside each quality release epoch

Revision ID: 6f2a9c4d8e17
Revises: 141bc30e4580
Create Date: 2026-08-17

The aggregate payload alone cannot make the research read endpoints stable:
reconstructing subjects, sessions, or turns from live tables after a cut lets an
old epoch drift.  This revision gives an epoch an optional all-or-none snapshot
manifest and stores the exact canonical JSON rows that manifest covers.

Legacy epochs intentionally keep all three manifest columns NULL.  Downgrade is
fail-closed and checks both the epoch columns and the row table before any DDL;
once a snapshot exists, neither its bytes nor the schema that explains them may
be discarded by a rollback.

The same legacy-compatible epoch extension also binds the approved proposal
digest to the entry-quarantine policy actually applied.  The pair is NULL/NULL
for old epochs and complete for new cuts, so receipt recovery can be driven by
the committed epoch even when no local pending file survived.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 -- AutoString type


revision: str = "6f2a9c4d8e17"
down_revision: Union[str, Sequence[str], None] = "141bc30e4580"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _hex64_sql(column: str) -> str:
    """Portable exact-lowercase-hex predicate; byte-identical to the model."""

    stripped = column
    for digit in "0123456789abcdef":
        stripped = f"replace({stripped}, '{digit}', '')"
    return f"(length({column}) = 64 AND {stripped} = '')"


_SNAPSHOT_COMPLETE_CHECK = (
    "((research_snapshot_schema_version IS NULL AND "
    "research_snapshot_manifest_json IS NULL AND "
    "research_snapshot_sha256 IS NULL) OR "
    "(research_snapshot_schema_version IS NOT NULL AND "
    "research_snapshot_manifest_json IS NOT NULL AND "
    "research_snapshot_sha256 IS NOT NULL))"
)

_SNAPSHOT_SCHEMA_CHECK = (
    "research_snapshot_schema_version IS NULL OR "
    "(length(trim(research_snapshot_schema_version)) > 0 AND "
    "research_snapshot_schema_version = trim(research_snapshot_schema_version))"
)

_SNAPSHOT_HASH_CHECK = (
    "research_snapshot_sha256 IS NULL OR "
    + _hex64_sql("research_snapshot_sha256")
)

_RECOVERY_EVIDENCE_COMPLETE_CHECK = (
    "((proposal_sha256 IS NULL AND entry_quarantine_days_applied IS NULL) OR "
    "(proposal_sha256 IS NOT NULL AND "
    "entry_quarantine_days_applied IS NOT NULL))"
)

_PROPOSAL_HASH_CHECK = (
    "proposal_sha256 IS NULL OR " + _hex64_sql("proposal_sha256")
)

_QUARANTINE_DAYS_CHECK = (
    "entry_quarantine_days_applied IS NULL OR "
    "(entry_quarantine_days_applied >= 0 AND "
    "entry_quarantine_days_applied <= 365)"
)


def upgrade() -> None:
    # Default-free nullable columns preserve every legacy epoch byte-for-byte.
    with op.batch_alter_table("qualityreleaseepoch") as batch_op:
        batch_op.add_column(sa.Column(
            "research_snapshot_schema_version",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ))
        batch_op.add_column(sa.Column(
            "research_snapshot_manifest_json",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ))
        batch_op.add_column(sa.Column(
            "research_snapshot_sha256",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ))
        batch_op.add_column(sa.Column(
            "proposal_sha256",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ))
        batch_op.add_column(sa.Column(
            "entry_quarantine_days_applied",
            sa.Integer(),
            nullable=True,
        ))
        batch_op.create_check_constraint(
            "ck_quality_release_epoch_research_snapshot_complete",
            _SNAPSHOT_COMPLETE_CHECK,
        )
        batch_op.create_check_constraint(
            "ck_quality_release_epoch_research_snapshot_schema",
            _SNAPSHOT_SCHEMA_CHECK,
        )
        batch_op.create_check_constraint(
            "ck_quality_release_epoch_research_snapshot_hash",
            _SNAPSHOT_HASH_CHECK,
        )
        batch_op.create_check_constraint(
            "ck_quality_release_epoch_recovery_evidence_complete",
            _RECOVERY_EVIDENCE_COMPLETE_CHECK,
        )
        batch_op.create_check_constraint(
            "ck_quality_release_epoch_proposal_hash",
            _PROPOSAL_HASH_CHECK,
        )
        batch_op.create_check_constraint(
            "ck_quality_release_epoch_quarantine_days",
            _QUARANTINE_DAYS_CHECK,
        )

    op.create_table(
        "qualityreleaseepochrowsnapshot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "epoch_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "dataset_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("row_ordinal", sa.Integer(), nullable=False),
        sa.Column(
            "row_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "row_sha256", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.ForeignKeyConstraint(
            ["epoch_id"], ["qualityreleaseepoch.epoch_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "epoch_id", "dataset_key", "row_ordinal",
            name="uq_quality_release_epoch_row_snapshot_ordinal"),
        sa.CheckConstraint(
            "dataset_key IN ('subjects','sessions','turns')",
            name="ck_quality_release_epoch_row_snapshot_dataset_closed"),
        sa.CheckConstraint(
            "row_ordinal >= 1",
            name="ck_quality_release_epoch_row_snapshot_ordinal_positive"),
        sa.CheckConstraint(
            _hex64_sql("row_sha256"),
            name="ck_quality_release_epoch_row_snapshot_hash"),
    )
    op.create_index(
        op.f("ix_qualityreleaseepochrowsnapshot_epoch_id"),
        "qualityreleaseepochrowsnapshot",
        ["epoch_id"],
        unique=False,
    )


def _snapshot_evidence_exists() -> bool:
    bind = op.get_bind()
    epoch_evidence = bind.execute(sa.text(
        "SELECT 1 FROM qualityreleaseepoch "
        "WHERE research_snapshot_schema_version IS NOT NULL "
        "OR research_snapshot_manifest_json IS NOT NULL "
        "OR research_snapshot_sha256 IS NOT NULL "
        "OR proposal_sha256 IS NOT NULL "
        "OR entry_quarantine_days_applied IS NOT NULL LIMIT 1"
    )).first()
    if epoch_evidence is not None:
        return True
    return bind.execute(sa.text(
        "SELECT 1 FROM qualityreleaseepochrowsnapshot LIMIT 1"
    )).first() is not None


def downgrade() -> None:
    # This must remain the first operation.  A refusal leaves schema, revision,
    # columns, rows, and indexes exactly as they were before the command.
    if _snapshot_evidence_exists():
        raise RuntimeError(
            "frozen quality-release evidence prevents downgrade"
        )

    op.drop_index(
        op.f("ix_qualityreleaseepochrowsnapshot_epoch_id"),
        table_name="qualityreleaseepochrowsnapshot",
    )
    op.drop_table("qualityreleaseepochrowsnapshot")

    with op.batch_alter_table("qualityreleaseepoch") as batch_op:
        batch_op.drop_constraint(
            "ck_quality_release_epoch_quarantine_days",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_quality_release_epoch_proposal_hash",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_quality_release_epoch_recovery_evidence_complete",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_quality_release_epoch_research_snapshot_hash",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_quality_release_epoch_research_snapshot_schema",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_quality_release_epoch_research_snapshot_complete",
            type_="check",
        )
        batch_op.drop_column("research_snapshot_sha256")
        batch_op.drop_column("research_snapshot_manifest_json")
        batch_op.drop_column("research_snapshot_schema_version")
        batch_op.drop_column("entry_quarantine_days_applied")
        batch_op.drop_column("proposal_sha256")
