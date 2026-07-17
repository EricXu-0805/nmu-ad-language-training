"""append-only research audit log (M1-E)

Revision ID: b2d5f8c31e07
Revises: a1c4e7f20b93
Create Date: 2026-07-17 02:30:00

只建审计账本(哈希链防篡改)+ 高水位锚点,不触碰既有数据。真机部署只执行 upgrade。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 —— AutoString 类型


revision: str = "b2d5f8c31e07"
down_revision: Union[str, Sequence[str], None] = "a1c4e7f20b93"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "auditlog",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("actor", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("action", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("patient_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("turn_id", sa.Integer(), nullable=True),
        sa.Column("summary", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        # prev_hash 唯一 → 并发追加撞同一前驱在 DB 层被挡,防多 worker 静默链分叉。
        sa.Column("prev_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("entry_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("prev_hash", name="uq_auditlog_prev_hash"),
    )
    op.create_index("ix_auditlog_patient_id", "auditlog", ["patient_id"])
    op.create_index("ix_auditlog_session_id", "auditlog", ["session_id"])
    # 高水位锚点(单行 id=1):记录条数与链尾 hash,verify 比对以检出尾部截断/整表清空。
    op.create_table(
        "auditanchor",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tip_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default="0" * 64),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    # 仅供无真机数据的开发库回退；审计账本真机只增不减。
    op.drop_table("auditanchor")
    op.drop_index("ix_auditlog_session_id", table_name="auditlog")
    op.drop_index("ix_auditlog_patient_id", table_name="auditlog")
    op.drop_table("auditlog")
