"""make research-user audit identities unambiguous

Revision ID: 7e4b2c9d1a60
Revises: d8f2a6c9e104
Create Date: 2026-07-19

``display_id`` is persisted in scoring and audit records.  A duplicate would
make two login accounts indistinguishable in research provenance.  Existing
duplicates therefore stop the migration; this migration never invents or
silently rewrites a person's audit identity.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7e4b2c9d1a60"
down_revision: Union[str, Sequence[str], None] = "d8f2a6c9e104"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT_NAME = "uq_research_user_display_id"


def _assert_no_existing_duplicates() -> None:
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError(
            "display_id 唯一性迁移必须联机执行预检，拒绝 --sql 离线模式。")
    duplicate_counts = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) AS duplicate_groups, "
        "COALESCE(SUM(identity_count), 0) AS affected_accounts "
        "FROM ("
        "SELECT COUNT(*) AS identity_count FROM researchuser "
        "GROUP BY display_id HAVING COUNT(*) > 1"
        ") AS duplicate_identities"
    )).one()
    duplicate_groups = int(duplicate_counts[0])
    affected_accounts = int(duplicate_counts[1])
    if duplicate_groups:
        raise RuntimeError(
            "researchuser.display_id 存在重复，拒绝迁移："
            f"{duplicate_groups} 组冲突，涉及 {affected_accounts} 个账号。"
            "请由数据库管理员核对后手工设置唯一 display_id，"
            "再重试 alembic upgrade head；系统不会自动改名。"
        )


def upgrade() -> None:
    _assert_no_existing_duplicates()
    # Alembic batch mode rebuilds the table only on SQLite; PostgreSQL emits a
    # normal ALTER TABLE.  The same named constraint is therefore portable.
    with op.batch_alter_table("researchuser") as batch_op:
        batch_op.create_unique_constraint(
            _CONSTRAINT_NAME, ["display_id"])


def downgrade() -> None:
    # 仅供无真机数据的开发库回退；科室部署永远只向前升级。
    with op.batch_alter_table("researchuser") as batch_op:
        batch_op.drop_constraint(_CONSTRAINT_NAME, type_="unique")
