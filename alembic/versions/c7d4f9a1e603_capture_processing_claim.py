"""persistent capture-processing claim for deferred attempt creation

Revision ID: c7d4f9a1e603
Revises: a3c8e5f7d904
Create Date: 2026-07-29

R1-foundation: decouple audio-capture admission from AttemptEvent/attempt_seq
creation so a future repeat-classifier can inspect the transcript before an
attempt_seq is ever consumed. This batch always disposes a completed capture
as ``answer_candidate``; the repeat branch is inert until a later batch.
Answer text itself is never duplicated here — it lives only in
``attemptevent.asr_text``; this table keeps only non-text ASR metadata.

Deployment precondition: this table starts empty. A record command that
already succeeded and is sitting in ``processing_attempt`` under the
pre-R1 code has no capture-processing row and cannot be safely backfilled
(there is no way to reconstruct its proof at migration time). Drain any
in-flight ``processing_attempt`` P0a state to a terminal outcome before
deploying this migration; do not deploy over a live in-flight capture.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 —— AutoString 类型


revision: str = "c7d4f9a1e603"
down_revision: Union[str, Sequence[str], None] = "a3c8e5f7d904"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "attemptcaptureprocessing",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("record_command_id", sa.Integer(), nullable=False),
        sa.Column("predecessor_command_id", sa.Integer(), nullable=False),
        sa.Column("receipt_server_seq", sa.Integer(), nullable=False),
        sa.Column("raw_audio_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("item_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("turn_seq", sa.Integer(), nullable=False),
        sa.Column("proof_attempt_seq", sa.Integer(), nullable=False),
        sa.Column("proof_prompt_level", sa.Integer(), nullable=False),
        sa.Column("processing_status", sqlmodel.sql.sqltypes.AutoString(), nullable=False,
                  server_default="received"),
        sa.Column("processing_owner", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("processing_lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("processing_claimed_at", sa.DateTime(), nullable=True),
        sa.Column("processing_generation", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("asr_confidence", sa.Float(), nullable=True),
        sa.Column("asr_engine_version", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("disposition", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("error_code", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("final_attempt_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("is_simulation", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.CheckConstraint("proof_attempt_seq >= 1",
                           name="ck_capture_processing_proof_seq_positive"),
        sa.CheckConstraint("proof_prompt_level >= 0 AND proof_prompt_level <= 3",
                           name="ck_capture_processing_proof_prompt_level"),
        sa.CheckConstraint(
            "processing_status IN ('received','asr_completed','technical_failure')",
            name="ck_capture_processing_status"),
        sa.CheckConstraint("processing_generation >= 0",
                           name="ck_capture_processing_generation_nonnegative"),
        sa.CheckConstraint(
            "disposition IS NULL OR disposition IN ('answer_candidate')",
            name="ck_capture_processing_disposition"),
        sa.CheckConstraint(
            "((processing_status = 'asr_completed' AND disposition IS NOT NULL) OR "
            "(processing_status != 'asr_completed' AND disposition IS NULL))",
            name="ck_capture_processing_disposition_matches_status"),
        sa.CheckConstraint(
            "((processing_status IN ('asr_completed','technical_failure') "
            "AND final_attempt_id IS NOT NULL) OR "
            "(processing_status = 'received' AND final_attempt_id IS NULL))",
            name="ck_capture_processing_final_attempt_matches_status"),
        sa.CheckConstraint(
            "((processing_owner IS NULL AND processing_lease_expires_at IS NULL) OR "
            "(processing_owner IS NOT NULL AND processing_lease_expires_at IS NOT NULL))",
            name="ck_capture_processing_lease_complete"),
        sa.CheckConstraint(
            "(processing_owner IS NULL OR processing_claimed_at IS NOT NULL)",
            name="ck_capture_processing_claimed_at_when_owned"),
        sa.CheckConstraint(
            "(processing_status = 'received' OR "
            "(processing_owner IS NULL AND processing_lease_expires_at IS NULL))",
            name="ck_capture_processing_terminal_lease_released"),
        sa.CheckConstraint(
            "((processing_status = 'received' AND processed_at IS NULL) OR "
            "(processing_status != 'received' AND processed_at IS NOT NULL))",
            name="ck_capture_processing_terminal_processed_at"),
        sa.ForeignKeyConstraint(["record_command_id"], ["runtimecommand.id"]),
        sa.ForeignKeyConstraint(["predecessor_command_id"], ["runtimecommand.id"]),
        sa.ForeignKeyConstraint(["receipt_server_seq"], ["audiocapturereceipt.server_seq"]),
        sa.ForeignKeyConstraint(["raw_audio_id"], ["audioassetrow.raw_audio_id"]),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.ForeignKeyConstraint(["final_attempt_id"], ["attemptevent.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("record_command_id",
                            name="uq_capture_processing_record_command"),
        sa.UniqueConstraint("raw_audio_id",
                            name="uq_capture_processing_raw_audio_id"),
        sa.UniqueConstraint("receipt_server_seq",
                            name="uq_capture_processing_receipt_seq"),
        sa.UniqueConstraint("final_attempt_id",
                            name="uq_capture_processing_final_attempt_id"),
    )
    op.create_index(op.f("ix_attemptcaptureprocessing_record_command_id"),
                    "attemptcaptureprocessing", ["record_command_id"])
    op.create_index(op.f("ix_attemptcaptureprocessing_predecessor_command_id"),
                    "attemptcaptureprocessing", ["predecessor_command_id"])
    op.create_index(op.f("ix_attemptcaptureprocessing_receipt_server_seq"),
                    "attemptcaptureprocessing", ["receipt_server_seq"])
    op.create_index(op.f("ix_attemptcaptureprocessing_raw_audio_id"),
                    "attemptcaptureprocessing", ["raw_audio_id"])
    op.create_index(op.f("ix_attemptcaptureprocessing_session_id"),
                    "attemptcaptureprocessing", ["session_id"])
    op.create_index(op.f("ix_attemptcaptureprocessing_item_id"),
                    "attemptcaptureprocessing", ["item_id"])
    op.create_index(op.f("ix_attemptcaptureprocessing_final_attempt_id"),
                    "attemptcaptureprocessing", ["final_attempt_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_attemptcaptureprocessing_final_attempt_id"),
                  table_name="attemptcaptureprocessing")
    op.drop_index(op.f("ix_attemptcaptureprocessing_item_id"),
                  table_name="attemptcaptureprocessing")
    op.drop_index(op.f("ix_attemptcaptureprocessing_session_id"),
                  table_name="attemptcaptureprocessing")
    op.drop_index(op.f("ix_attemptcaptureprocessing_raw_audio_id"),
                  table_name="attemptcaptureprocessing")
    op.drop_index(op.f("ix_attemptcaptureprocessing_receipt_server_seq"),
                  table_name="attemptcaptureprocessing")
    op.drop_index(op.f("ix_attemptcaptureprocessing_predecessor_command_id"),
                  table_name="attemptcaptureprocessing")
    op.drop_index(op.f("ix_attemptcaptureprocessing_record_command_id"),
                  table_name="attemptcaptureprocessing")
    op.drop_table("attemptcaptureprocessing")
