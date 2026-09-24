"""Allow append-only explicit early-answer playback interruption receipts.

Revision ID: a7c3e9d2b641
Revises: f4b2d8c1a635
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a7c3e9d2b641"
down_revision: Union[str, Sequence[str], None] = "f4b2d8c1a635"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "ack_type IN ('tts_started','tts_ended','tts_failed','record_started','record_stopped','record_failed')"
_NEW = "ack_type IN ('tts_started','tts_ended','tts_interrupted','tts_failed','record_started','record_stopped','record_failed')"


def _replace_check(value: str) -> None:
    with op.batch_alter_table("runtimecommandack") as batch:
        batch.drop_constraint("ck_runtime_command_ack_type", type_="check")
        batch.create_check_constraint("ck_runtime_command_ack_type", value)


def upgrade() -> None:
    _replace_check(_NEW)


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
            "SELECT 1 FROM runtimecommandack WHERE ack_type='tts_interrupted' LIMIT 1"
    )).first() is not None:
        raise RuntimeError("已有提前回答收据，禁止回退删除其契约")
    _replace_check(_OLD)
