"""server-authoritative audio capture receipt ledger

Revision ID: e3d7a9c5f204
Revises: c2e8a4d7f901
Create Date: 2026-07-18

旧音频行只增加 nullable 长度/上传时间，不从磁盘猜测或回填；既有 checksum、
状态和归属全部原样保留。新收据只会在应用逐次核对 DB 与物理原件后追加。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 -- AutoString 类型


revision: str = "e3d7a9c5f204"
down_revision: Union[str, Sequence[str], None] = "c2e8a4d7f901"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("audioassetrow") as batch_op:
        batch_op.add_column(sa.Column("byte_count", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("uploaded_at", sa.DateTime(), nullable=True))

    op.create_table(
        "audiocapturereceipt",
        sa.Column("server_seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("raw_audio_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("turn_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("byte_count", sa.BigInteger(), nullable=False),
        sa.Column("checksum", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("data_classification", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("is_simulation", sa.Boolean(), nullable=False),
        sa.Column("contains_direct_identifier", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "byte_count > 0", name="ck_audio_capture_receipt_bytes_positive"),
        sa.CheckConstraint(
            "duration_seconds >= 0 AND duration_seconds <= 21600",
            name="ck_audio_capture_receipt_duration"),
        sa.CheckConstraint(
            "data_classification IN ('research','simulation')",
            name="ck_audio_capture_receipt_classification"),
        sa.ForeignKeyConstraint(["raw_audio_id"], ["audioassetrow.raw_audio_id"]),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.PrimaryKeyConstraint("server_seq"),
        sa.UniqueConstraint(
            "raw_audio_id", name="uq_audio_capture_receipt_raw_audio_id"),
    )
    op.create_index(
        "ix_audiocapturereceipt_raw_audio_id", "audiocapturereceipt",
        ["raw_audio_id"], unique=False)
    op.create_index(
        "ix_audiocapturereceipt_session_id", "audiocapturereceipt",
        ["session_id"], unique=False)
    op.create_index(
        "ix_audio_capture_receipt_session_seq", "audiocapturereceipt",
        ["session_id", "server_seq"], unique=False)


def downgrade() -> None:
    op.drop_index(
        "ix_audio_capture_receipt_session_seq", table_name="audiocapturereceipt")
    op.drop_index(
        "ix_audiocapturereceipt_session_id", table_name="audiocapturereceipt")
    op.drop_index(
        "ix_audiocapturereceipt_raw_audio_id", table_name="audiocapturereceipt")
    op.drop_table("audiocapturereceipt")
    with op.batch_alter_table("audioassetrow") as batch_op:
        batch_op.drop_column("uploaded_at")
        batch_op.drop_column("byte_count")
