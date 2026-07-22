"""session terminal lifecycle

Revision ID: e6f3b1a8c204
Revises: d4a9e2c7f611
Create Date: 2026-07-18

只向场次运行表增加终态元数据，不改写既有游标、评分或录音。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 -- AutoString 类型


revision: str = "e6f3b1a8c204"
down_revision: Union[str, Sequence[str], None] = "d4a9e2c7f611"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sessionruntimestate", sa.Column(
        "completed_at", sa.DateTime(), nullable=True))
    op.add_column("sessionruntimestate", sa.Column(
        "aborted_at", sa.DateTime(), nullable=True))
    op.add_column("sessionruntimestate", sa.Column(
        "ended_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column("sessionruntimestate", sa.Column(
        "end_reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    # 仅供无真机数据的开发库回退；真机部署只向前 upgrade。
    op.drop_column("sessionruntimestate", "end_reason")
    op.drop_column("sessionruntimestate", "ended_by")
    op.drop_column("sessionruntimestate", "aborted_at")
    op.drop_column("sessionruntimestate", "completed_at")
