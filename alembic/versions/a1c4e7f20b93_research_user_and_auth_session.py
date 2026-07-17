"""research user accounts and auth sessions (M1-D 公网部署认证)

Revision ID: a1c4e7f20b93
Revises: c8f3b8a7d901
Create Date: 2026-07-17 01:30:00

只建两张新表(账号 + 会话),不触碰既有患者/场次/录音/评分数据。真机部署只执行 upgrade。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 —— AutoString 类型


revision: str = "a1c4e7f20b93"
down_revision: Union[str, Sequence[str], None] = "c8f3b8a7d901"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "researchuser",
        sa.Column("username", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("display_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("password_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("role", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False, server_default="researcher"),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("username"),
    )
    op.create_table(
        "authsession",
        sa.Column("token_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("username", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["username"], ["researchuser.username"]),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_authsession_username", "authsession", ["username"])


def downgrade() -> None:
    # 仅供无真机数据的开发库回退；科室真机按部署 SOP 永远只向前 upgrade。
    op.drop_index("ix_authsession_username", table_name="authsession")
    op.drop_table("authsession")
    op.drop_table("researchuser")
