"""patient-scoped versioned cloud processing consent

Revision ID: c2e8a4d7f901
Revises: b9c6e4f2d013
Create Date: 2026-07-18

既有档案全部保持未授权；迁移绝不根据共享 API Key 或既有录音授权推断云授权。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 -- AutoString 类型


revision: str = "c2e8a4d7f901"
down_revision: Union[str, Sequence[str], None] = "b9c6e4f2d013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("patient") as batch_op:
        batch_op.add_column(sa.Column(
            "cloud_processing_allowed", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column(
            "cloud_processing_provider_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column(
            "cloud_processing_notice_version", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column(
            "cloud_processing_consented_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column(
            "cloud_processing_revoked_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("patient") as batch_op:
        batch_op.drop_column("cloud_processing_revoked_at")
        batch_op.drop_column("cloud_processing_consented_at")
        batch_op.drop_column("cloud_processing_notice_version")
        batch_op.drop_column("cloud_processing_provider_id")
        batch_op.drop_column("cloud_processing_allowed")
