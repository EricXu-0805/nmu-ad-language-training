"""recoverable attempt processing claim lease

Revision ID: b9c6e4f2d013
Revises: a8b5d3f1c902
Create Date: 2026-07-18

既有 received/asr_completed 行不伪造 owner，升级后视为无租约、可立即接管；
终态行不改变。generation 是防止已过期 worker 回写的 fencing token。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 -- AutoString 类型


revision: str = "b9c6e4f2d013"
down_revision: Union[str, Sequence[str], None] = "a8b5d3f1c902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("attemptevent") as batch_op:
        batch_op.add_column(sa.Column(
            "processing_owner", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column(
            "processing_lease_expires_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column(
            "processing_claimed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column(
            "processing_generation", sa.Integer(), nullable=False,
            server_default=sa.text("0")))
        batch_op.create_check_constraint(
            "ck_attempt_processing_generation_nonnegative",
            "processing_generation >= 0")


def downgrade() -> None:
    # 仅供无真机数据的开发库回退；真机部署只向前 upgrade。
    with op.batch_alter_table("attemptevent") as batch_op:
        batch_op.drop_constraint(
            "ck_attempt_processing_generation_nonnegative", type_="check")
        batch_op.drop_column("processing_generation")
        batch_op.drop_column("processing_claimed_at")
        batch_op.drop_column("processing_lease_expires_at")
        batch_op.drop_column("processing_owner")
