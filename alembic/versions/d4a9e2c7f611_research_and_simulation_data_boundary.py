"""persist research/simulation boundary for sessions and audio

Revision ID: d4a9e2c7f611
Revises: b2d5f8c31e07
Create Date: 2026-07-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4a9e2c7f611"
down_revision: Union[str, Sequence[str], None] = "b2d5f8c31e07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 既有数据不能被静默当成模拟：默认/回填一律 False，之后须显式建立模拟场次。
    op.add_column("session", sa.Column(
        "is_simulation", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("audioassetrow", sa.Column(
        "is_simulation", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("audioassetrow", "is_simulation")
    op.drop_column("session", "is_simulation")
