"""append-only audio local-copy disposal receipt ledger

Revision ID: f7c2e8a4d105
Revises: e4a7c1d9b206
Create Date: 2026-08-07

设备确认已物理删除本地音频副本的治理回执(收据 144)。回执是治理信号,不是门禁:
删除永不等待回执;缺回执由治理面板可见化。不回填历史——上线前已删除的本地副本
没有可信事实来源,面板如实显示"尚无设备确认"。downgrade fail-closed:已有回执
即拒绝降级,治理证据不允许被迁移悄悄抹掉。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 -- AutoString 类型


revision: str = "f7c2e8a4d105"
down_revision: Union[str, Sequence[str], None] = "e4a7c1d9b206"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audiolocalcopydisposalreceipt",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("raw_audio_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("turn_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("reason", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("checksum", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("byte_count", sa.BigInteger(), nullable=False),
        sa.Column("contains_direct_identifier", sa.Boolean(), nullable=False),
        sa.Column("reporter_device_id_hash",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("reported_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "reason IN ('deleted','withdrawn')",
            name="ck_audio_disposal_receipt_reason"),
        sa.CheckConstraint(
            "length(checksum) = 64 AND lower(checksum) = checksum",
            name="ck_audio_disposal_receipt_checksum"),
        sa.CheckConstraint(
            "byte_count > 0", name="ck_audio_disposal_receipt_bytes_positive"),
        sa.ForeignKeyConstraint(["raw_audio_id"], ["audioassetrow.raw_audio_id"]),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "raw_audio_id", "reporter_device_id_hash",
            name="uq_audio_disposal_receipt_device"),
    )
    op.create_index(
        "ix_audiolocalcopydisposalreceipt_raw_audio_id",
        "audiolocalcopydisposalreceipt", ["raw_audio_id"], unique=False)
    op.create_index(
        "ix_audiolocalcopydisposalreceipt_session_id",
        "audiolocalcopydisposalreceipt", ["session_id"], unique=False)
    op.create_index(
        "ix_audiolocalcopydisposalreceipt_reporter_device_id_hash",
        "audiolocalcopydisposalreceipt", ["reporter_device_id_hash"], unique=False)
    op.create_index(
        "ix_audio_disposal_receipt_session",
        "audiolocalcopydisposalreceipt", ["session_id", "reported_at"],
        unique=False)


def _disposal_evidence_exists() -> bool:
    bind = op.get_bind()
    row = bind.execute(sa.text(
        "SELECT 1 FROM audiolocalcopydisposalreceipt LIMIT 1")).first()
    return row is not None


def downgrade() -> None:
    if _disposal_evidence_exists():
        raise RuntimeError(
            "audiolocalcopydisposalreceipt 已有治理回执,拒绝降级抹掉证据;"
            "如确需回退,先按治理流程导出并留存回执")
    op.drop_index(
        "ix_audio_disposal_receipt_session",
        table_name="audiolocalcopydisposalreceipt")
    op.drop_index(
        "ix_audiolocalcopydisposalreceipt_reporter_device_id_hash",
        table_name="audiolocalcopydisposalreceipt")
    op.drop_index(
        "ix_audiolocalcopydisposalreceipt_session_id",
        table_name="audiolocalcopydisposalreceipt")
    op.drop_index(
        "ix_audiolocalcopydisposalreceipt_raw_audio_id",
        table_name="audiolocalcopydisposalreceipt")
    op.drop_table("audiolocalcopydisposalreceipt")
