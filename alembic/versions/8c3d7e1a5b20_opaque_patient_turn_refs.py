"""mark legacy canonical device turn refs before opaque migration

Revision ID: 8c3d7e1a5b20
Revises: 6a1d9c3e7f24
Create Date: 2026-07-18

The marker is not a credential or secret.  Existing audio registrations are
version 1 so their already-captured browser outboxes can finish exact recovery;
new registrations default to version 2 and can never use canonical device keys.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8c3d7e1a5b20"
down_revision: Union[str, Sequence[str], None] = "6a1d9c3e7f24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "audioassetrow",
        sa.Column(
            "patient_turn_ref_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    # Backfill is supplied by the add-column default.  Flip only the default for
    # registrations created after this migration; application writes v2 as well.
    with op.batch_alter_table("audioassetrow") as batch_op:
        batch_op.alter_column(
            "patient_turn_ref_version",
            existing_type=sa.Integer(),
            server_default=sa.text("2"),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("audioassetrow") as batch_op:
        batch_op.drop_column("patient_turn_ref_version")
