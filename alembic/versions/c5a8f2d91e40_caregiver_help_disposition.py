"""append-only caregiver help disposition ledger（求助四态）

Revision ID: c5a8f2d91e40
Revises: b3e7c5a9d214
Create Date: 2026-08-15

求助从"已登记"往后走的只追加转移。四态按顺序是
recorded → delivered → acknowledged → resolved，**可跳过**：
没有通知通道时 delivered 永远到不了，但工作人员当面走过来仍然能直接到
acknowledged——那是真的发生了，不能因为没配通道就否认。

之所以要独立一张表而不是给 CaregiverHelpRequest 加列：那张表是严格只追加的
（模型上挂了拒绝 UPDATE/DELETE 的监听器），加列就得改行。

delivered 只能由通知通道带着回执写入，任何人都不能声称它。放行清单原话：
「不能假装工作人员已收到求助」。

downgrade fail-closed：已有转移即拒绝降级——谁接的、谁处理的是追责证据，
不允许被迁移悄悄抹掉。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 -- AutoString 类型


revision: str = "c5a8f2d91e40"
down_revision: Union[str, Sequence[str], None] = "b3e7c5a9d214"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "caregiverhelpdisposition",
        sa.Column("disposition_id", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("request_id", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("state", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("actor_id", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("evidence_sha256", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_id"], ["caregiverhelprequest.request_id"]),
        sa.PrimaryKeyConstraint("disposition_id"),
        sa.UniqueConstraint(
            "request_id", "state",
            name="uq_caregiver_help_disposition_request_state"),
        sa.CheckConstraint(
            "state IN ('delivered','acknowledged','resolved')",
            name="ck_caregiver_help_disposition_state_closed"),
        sa.CheckConstraint(
            "length(evidence_sha256) = 64 "
            "AND lower(evidence_sha256) = evidence_sha256 "
            "AND evidence_sha256 GLOB '[0-9a-f]*'",
            name="ck_caregiver_help_disposition_evidence_hash"),
    )
    op.create_index(
        op.f("ix_caregiverhelpdisposition_request_id"),
        "caregiverhelpdisposition", ["request_id"])
    op.create_index(
        op.f("ix_caregiverhelpdisposition_actor_id"),
        "caregiverhelpdisposition", ["actor_id"])


def _disposition_evidence_exists() -> bool:
    bind = op.get_bind()
    row = bind.execute(sa.text(
        "SELECT 1 FROM caregiverhelpdisposition LIMIT 1")).first()
    return row is not None


def downgrade() -> None:
    if _disposition_evidence_exists():
        raise RuntimeError(
            "caregiverhelpdisposition 已有求助处置转移，拒绝降级抹掉证据；"
            "谁接的、谁处理的是追责事实，如确需回退先按治理流程导出留存")
    op.drop_index(
        op.f("ix_caregiverhelpdisposition_actor_id"),
        table_name="caregiverhelpdisposition")
    op.drop_index(
        op.f("ix_caregiverhelpdisposition_request_id"),
        table_name="caregiverhelpdisposition")
    op.drop_table("caregiverhelpdisposition")
