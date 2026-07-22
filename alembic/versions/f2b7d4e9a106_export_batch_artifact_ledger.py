"""add recoverable export batch and artifact ledger

Revision ID: f2b7d4e9a106
Revises: e1c4a7d9b205
Create Date: 2026-07-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f2b7d4e9a106"
down_revision: Union[str, Sequence[str], None] = "e1c4a7d9b205"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "exportbatch",
        sa.Column("batch_id", sa.String(), nullable=False),
        sa.Column("schema_version", sa.String(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(), nullable=False),
        sa.Column("request_fingerprint", sa.String(), nullable=False),
        sa.Column("export_scope_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("data_classification", sa.String(), nullable=False),
        sa.Column("deidentified", sa.Boolean(), nullable=False),
        sa.Column("actor_display_id", sa.String(), nullable=False),
        sa.Column("actor_role", sa.String(), nullable=False),
        sa.Column("result_metadata_json", sa.String(), nullable=False),
        sa.Column("manifest_sha256", sa.String(), nullable=True),
        sa.Column("publication_manifest_json", sa.String(), nullable=True),
        sa.Column("staging_owner_hash", sa.String(), nullable=True),
        sa.Column("staging_lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("artifacts_ready_at", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('staging','artifacts_ready','published')",
            name="ck_export_batch_status"),
        sa.CheckConstraint(
            "data_classification IN ('research','simulation')",
            name="ck_export_batch_classification"),
        sa.CheckConstraint(
            "actor_role IN ('data_steward','admin')",
            name="ck_export_batch_actor_role"),
        sa.CheckConstraint(
            "length(idempotency_key_hash) = 64 AND "
            "length(request_fingerprint) = 64 AND "
            "length(export_scope_hash) = 64",
            name="ck_export_batch_hash_lengths"),
        sa.CheckConstraint(
            "manifest_sha256 IS NULL OR length(manifest_sha256) = 64",
            name="ck_export_batch_manifest_hash_length"),
        sa.CheckConstraint(
            "((staging_owner_hash IS NULL AND "
            "staging_lease_expires_at IS NULL) OR "
            "(status = 'staging' AND staging_owner_hash IS NOT NULL AND "
            "length(staging_owner_hash) = 64 AND "
            "staging_lease_expires_at IS NOT NULL))",
            name="ck_export_batch_staging_lease"),
        sa.CheckConstraint(
            "length(trim(actor_display_id)) > 0",
            name="ck_export_batch_actor_nonempty"),
        sa.CheckConstraint(
            "((status = 'staging' AND artifacts_ready_at IS NULL AND "
            "published_at IS NULL) OR "
            "(status = 'artifacts_ready' AND artifacts_ready_at IS NOT NULL AND "
            "published_at IS NULL) OR "
            "(status = 'published' AND artifacts_ready_at IS NOT NULL AND "
            "published_at IS NOT NULL))",
            name="ck_export_batch_status_times"),
        sa.PrimaryKeyConstraint("batch_id"),
        sa.UniqueConstraint(
            "idempotency_key_hash",
            name="uq_export_batch_idempotency_key_hash"),
        sa.UniqueConstraint(
            "export_scope_hash",
            name="uq_export_batch_export_scope_hash"),
    )
    op.create_index(
        "ix_export_batch_status_created", "exportbatch",
        ["status", "created_at"], unique=False)
    op.create_table(
        "exportartifact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.String(), nullable=False),
        sa.Column("realm", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("relative_path", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("byte_count", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('csv','controlled_audio','manifest','staging_receipt')",
            name="ck_export_artifact_kind"),
        sa.CheckConstraint(
            "realm IN ('research_analysis','research_controlled_audio',"
            "'simulation_analysis','simulation_controlled_audio')",
            name="ck_export_artifact_realm"),
        sa.CheckConstraint("length(sha256) = 64",
                           name="ck_export_artifact_sha256_length"),
        sa.CheckConstraint("byte_count >= 0",
                           name="ck_export_artifact_byte_count"),
        sa.CheckConstraint(
            "length(relative_path) BETWEEN 1 AND 500 AND "
            "substr(relative_path, 1, 1) != '/'",
            name="ck_export_artifact_relative_path"),
        sa.ForeignKeyConstraint(["batch_id"], ["exportbatch.batch_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id", "realm", "relative_path",
            name="uq_export_artifact_batch_realm_path"),
    )
    op.create_index(
        "ix_export_artifact_batch_id", "exportartifact", ["batch_id"],
        unique=False)
    op.create_index(
        "ix_export_artifact_batch_kind", "exportartifact",
        ["batch_id", "kind"], unique=False)


def downgrade() -> None:
    op.drop_index(
        "ix_export_artifact_batch_kind", table_name="exportartifact")
    op.drop_index(
        "ix_export_artifact_batch_id", table_name="exportartifact")
    op.drop_table("exportartifact")
    op.drop_index(
        "ix_export_batch_status_created", table_name="exportbatch")
    op.drop_table("exportbatch")
