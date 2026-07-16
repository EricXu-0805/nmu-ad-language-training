"""session runtime state and patient presence

Revision ID: c8f3b8a7d901
Revises: 794480f21ed4
Create Date: 2026-07-16 16:30:00

只做可逆的增列/建表，不改写既有会话、题目、录音或评分数据。真机部署只执行 upgrade。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 —— AutoString 类型


revision: str = "c8f3b8a7d901"
down_revision: Union[str, Sequence[str], None] = "794480f21ed4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("livestate", sa.Column(
        "command_wseq", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("livestate", sa.Column(
        "patient_ack_session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column("livestate", sa.Column(
        "patient_current_screen", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column("livestate", sa.Column(
        "patient_last_seen_at", sa.DateTime(), nullable=True))
    op.add_column("livestate", sa.Column(
        "patient_ack_seq", sa.BigInteger(), nullable=True))

    op.create_table(
        "sessionruntimestate",
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False, server_default="active"),
        sa.Column("cursor_json", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("rapport_json", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paused_at", sa.DateTime(), nullable=True),
        sa.Column("resumed_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.add_column("audioassetrow", sa.Column(
        "turn_key", sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    # 仅供无真机数据的开发库回退；科室真机按部署 SOP 永远只向前 upgrade。
    op.drop_column("audioassetrow", "turn_key")
    op.drop_table("sessionruntimestate")
    op.drop_column("livestate", "patient_ack_seq")
    op.drop_column("livestate", "patient_last_seen_at")
    op.drop_column("livestate", "patient_current_screen")
    op.drop_column("livestate", "patient_ack_session_id")
    op.drop_column("livestate", "command_wseq")
