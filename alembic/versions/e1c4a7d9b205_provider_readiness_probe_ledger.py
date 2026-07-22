"""add append-only synthetic provider readiness probe ledger

Revision ID: e1c4a7d9b205
Revises: d6a4f9b2c817
Create Date: 2026-07-19

The ledger stores only provider/configuration metadata.  It has no columns for
API keys, probe audio, transcripts, answer text, patients, sessions, or turns.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e1c4a7d9b205"
down_revision: Union[str, Sequence[str], None] = "d6a4f9b2c817"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "providerreadinessprobe",
        sa.Column("probe_id", sa.String(), nullable=False),
        sa.Column("schema_version", sa.String(), nullable=False),
        sa.Column("runtime_contract", sa.String(), nullable=False),
        sa.Column("config_fingerprint", sa.String(), nullable=False),
        sa.Column("tts_engine_version", sa.String(), nullable=False),
        sa.Column("asr_engine_version", sa.String(), nullable=False),
        sa.Column("llm_engine_version", sa.String(), nullable=False),
        sa.Column("tts_required", sa.Boolean(), nullable=False),
        sa.Column("tts_success", sa.Boolean(), nullable=False),
        sa.Column("tts_failure_code", sa.String(), nullable=True),
        sa.Column("asr_required", sa.Boolean(), nullable=False),
        sa.Column("asr_success", sa.Boolean(), nullable=False),
        sa.Column("asr_failure_code", sa.String(), nullable=True),
        sa.Column("llm_required", sa.Boolean(), nullable=False),
        sa.Column("llm_configured", sa.Boolean(), nullable=False),
        sa.Column("llm_success", sa.Boolean(), nullable=False),
        sa.Column("llm_failure_code", sa.String(), nullable=True),
        sa.Column("required_capabilities_ready", sa.Boolean(), nullable=False),
        sa.Column("all_configured_capabilities_ready", sa.Boolean(), nullable=False),
        sa.Column("probe_failure_code", sa.String(), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("actor_display_id", sa.String(), nullable=False),
        sa.CheckConstraint(
            "length(config_fingerprint) = 64",
            name="ck_provider_readiness_fingerprint_length"),
        sa.CheckConstraint(
            "expires_at > checked_at",
            name="ck_provider_readiness_expiry_after_check"),
        sa.CheckConstraint(
            "length(trim(actor_display_id)) > 0",
            name="ck_provider_readiness_actor_nonempty"),
        sa.CheckConstraint(
            "length(trim(runtime_contract)) > 0",
            name="ck_provider_readiness_contract_nonempty"),
        sa.CheckConstraint(
            "NOT required_capabilities_ready OR "
            "((NOT tts_required OR tts_success) AND "
            "(NOT asr_required OR asr_success) AND "
            "(NOT llm_required OR llm_success))",
            name="ck_provider_readiness_required_successes"),
        sa.PrimaryKeyConstraint("probe_id"),
    )
    op.create_index(
        "ix_provider_readiness_checked", "providerreadinessprobe",
        ["checked_at"], unique=False)


def downgrade() -> None:
    op.drop_index(
        "ix_provider_readiness_checked", table_name="providerreadinessprobe")
    op.drop_table("providerreadinessprobe")
