"""server-side TTS serve evidence ledger

Revision ID: a3c8e5f7d904
Revises: f9b2d6e4a801
Create Date: 2026-07-27

Append-only ledger of what the TTS path actually returned per request:
engine_version + cache_hit + command binding, written by the server after the
post-provider authorization recheck.  Frontend-asserted engine facts are never
accepted; text is stored only as SHA-256.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 —— autogenerate 产出的 sqlmodel.AutoString 等类型需要


# revision identifiers, used by Alembic.
revision: str = 'a3c8e5f7d904'
down_revision: Union[str, Sequence[str], None] = 'f9b2d6e4a801'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'ttsserveevidence',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('command_id', sa.Integer(), nullable=True),
        sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('engine_version', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('cache_hit', sa.Boolean(), nullable=False),
        sa.Column('result', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('byte_count', sa.Integer(), nullable=True),
        sa.Column('text_sha256', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('is_simulation', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "source IN ('autopilot_command','live_speak')",
            name='ck_tts_serve_source'),
        sa.CheckConstraint(
            "result IN ('served','degraded')",
            name='ck_tts_serve_result'),
        sa.CheckConstraint(
            'length(text_sha256) = 64 AND lower(text_sha256) = text_sha256',
            name='ck_tts_serve_text_sha256'),
        sa.CheckConstraint(
            "(result = 'served' AND byte_count > 0) OR "
            "(result = 'degraded' AND byte_count IS NULL)",
            name='ck_tts_serve_bytes_match_result'),
        sa.CheckConstraint(
            "(source = 'autopilot_command' AND command_id IS NOT NULL) OR "
            "(source = 'live_speak' AND command_id IS NULL)",
            name='ck_tts_serve_command_binding'),
        sa.ForeignKeyConstraint(['command_id'], ['runtimecommand.id']),
        sa.ForeignKeyConstraint(['session_id'], ['session.session_id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_ttsserveevidence_session_id'),
                    'ttsserveevidence', ['session_id'], unique=False)
    op.create_index(op.f('ix_ttsserveevidence_command_id'),
                    'ttsserveevidence', ['command_id'], unique=False)
    op.create_index('ix_tts_serve_session_created',
                    'ttsserveevidence', ['session_id', 'created_at'],
                    unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_tts_serve_session_created', table_name='ttsserveevidence')
    op.drop_index(op.f('ix_ttsserveevidence_command_id'),
                  table_name='ttsserveevidence')
    op.drop_index(op.f('ix_ttsserveevidence_session_id'),
                  table_name='ttsserveevidence')
    op.drop_table('ttsserveevidence')
