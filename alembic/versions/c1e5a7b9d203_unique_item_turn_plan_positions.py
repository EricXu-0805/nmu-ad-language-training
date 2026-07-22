"""make frozen-plan item and turn positions unique

Revision ID: c1e5a7b9d203
Revises: 9f2c6a8d4e10
Create Date: 2026-07-19

Autonomous terminal evidence is idempotent only if one session/item and one
item/turn position have a single canonical row.  Existing ambiguity is never
silently merged: the migration stops for research review instead.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1e5a7b9d203"
down_revision: Union[str, Sequence[str], None] = "9f2c6a8d4e10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ITEM_CONSTRAINT = "uq_item_event_session_item"
_TURN_CONSTRAINT = "uq_turn_event_item_turn_seq"


def _duplicate_group_count(sql: str) -> int:
    return int(op.get_bind().execute(sa.text(sql)).scalar_one())


def _assert_no_ambiguous_positions() -> None:
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError(
            "ItemEvent/TurnEvent 唯一性迁移必须联机预检，拒绝 --sql 离线模式。"
        )
    duplicate_items = _duplicate_group_count(
        "SELECT COUNT(*) FROM ("
        "SELECT session_id, item_id FROM itemevent "
        "GROUP BY session_id, item_id HAVING COUNT(*) > 1"
        ") AS duplicate_item_positions"
    )
    duplicate_turns = _duplicate_group_count(
        "SELECT COUNT(*) FROM ("
        "SELECT item_event_id, turn_seq FROM turnevent "
        "GROUP BY item_event_id, turn_seq HAVING COUNT(*) > 1"
        ") AS duplicate_turn_positions"
    )
    if duplicate_items or duplicate_turns:
        raise RuntimeError(
            "ItemEvent/TurnEvent 存在重复冻结计划位置，拒绝迁移："
            f"ItemEvent {duplicate_items} 组，TurnEvent {duplicate_turns} 组。"
            "请由数据管理员核对研究证据并明确权威行后再重试；"
            "系统不会自动删除或合并记录。"
        )


def upgrade() -> None:
    _assert_no_ambiguous_positions()
    with op.batch_alter_table("itemevent") as batch_op:
        batch_op.create_unique_constraint(
            _ITEM_CONSTRAINT, ["session_id", "item_id"])
    with op.batch_alter_table("turnevent") as batch_op:
        batch_op.create_unique_constraint(
            _TURN_CONSTRAINT, ["item_event_id", "turn_seq"])


def downgrade() -> None:
    # Development/test rollback only; research deployments migrate forward.
    with op.batch_alter_table("turnevent") as batch_op:
        batch_op.drop_constraint(_TURN_CONSTRAINT, type_="unique")
    with op.batch_alter_table("itemevent") as batch_op:
        batch_op.drop_constraint(_ITEM_CONSTRAINT, type_="unique")
