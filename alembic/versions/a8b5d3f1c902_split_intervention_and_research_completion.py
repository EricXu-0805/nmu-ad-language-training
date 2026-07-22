"""split intervention and research completion

Revision ID: a8b5d3f1c902
Revises: f7a4c9d2e531
Create Date: 2026-07-18

床旁干预结束后允许切换受试者，研究员可在之后异步确认和锁定研究真值。
既有 completed 场次回填其干预结束时间，避免升级后出现伪缺失状态。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 -- AutoString 类型


revision: str = "a8b5d3f1c902"
down_revision: Union[str, Sequence[str], None] = "f7a4c9d2e531"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sessionruntimestate", sa.Column(
        "intervention_completed_at", sa.DateTime(), nullable=True))
    op.add_column("sessionruntimestate", sa.Column(
        "intervention_ended_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.execute(
        "UPDATE sessionruntimestate "
        "SET intervention_completed_at = completed_at "
        "WHERE status = 'completed' AND intervention_completed_at IS NULL"
    )


def downgrade() -> None:
    # 仅供无真机数据的开发库回退；真机部署只向前 upgrade。
    op.drop_column("sessionruntimestate", "intervention_ended_by")
    op.drop_column("sessionruntimestate", "intervention_completed_at")
