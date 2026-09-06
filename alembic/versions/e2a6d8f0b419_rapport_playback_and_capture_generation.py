"""Fence Week1 automatic replies and persist device playback/capture generations.

Revision ID: e2a6d8f0b419
Revises: d0c22a6dae2a

Historical audio keeps recording_wseq NULL; never invent a generation. Before
installing the auto-audio unique index, duplicates fail closed without changing
or deleting any evidence. Read-only diagnosis (run on a protected copy):

  SELECT session_id, raw_audio_id, COUNT(*) AS evidence_rows
  FROM rapportutteranceevent
  WHERE origin = 'auto' AND raw_audio_id IS NOT NULL
  GROUP BY session_id, raw_audio_id HAVING COUNT(*) > 1;

If rows exist, stop deployment. Preserve the original database and full matching
utterances/TTS evidence, and obtain a recorded governance decision for a separate
corrective migration. Do not drop rows or relabel their origin to force upgrade.
"""
from alembic import op
import sqlalchemy as sa

revision = "e2a6d8f0b419"
down_revision = "d0c22a6dae2a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(sa.text(
        "SELECT 1 FROM rapportutteranceevent "
        "WHERE origin = 'auto' AND raw_audio_id IS NOT NULL "
        "GROUP BY session_id, raw_audio_id HAVING COUNT(*) > 1 LIMIT 1"
    )).first()
    if duplicate is not None:
        raise RuntimeError(
            "rapport_auto_audio_duplicate_evidence: upgrade stopped without changing evidence; "
            "run the read-only diagnosis in migration e2a6d8f0b419 and obtain a "
            "recorded governance decision; never delete or relabel history")
    op.create_index(
        "uq_rapport_auto_audio", "rapportutteranceevent",
        ["session_id", "raw_audio_id"], unique=True,
        sqlite_where=sa.text("origin = 'auto' AND raw_audio_id IS NOT NULL"),
        postgresql_where=sa.text("origin = 'auto' AND raw_audio_id IS NOT NULL"))
    op.add_column("audioassetrow", sa.Column("recording_wseq", sa.BigInteger(), nullable=True))
    op.add_column("audiocapturereceipt", sa.Column("recording_wseq", sa.BigInteger(), nullable=True))
    op.create_table(
        "rapportplaybackreceipt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("wseq", sa.BigInteger(), nullable=False),
        sa.Column("runtime_revision", sa.Integer(), nullable=False),
        sa.Column("section_key", sa.String(), nullable=False),
        sa.Column("question_idx", sa.Integer(), nullable=False),
        sa.Column("beat", sa.String(), nullable=False),
        sa.Column("utterance_id", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("device_token_hash", sa.String(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.UniqueConstraint("session_id", "wseq", name="uq_rapport_playback_generation"),
        sa.CheckConstraint("wseq >= 1 AND runtime_revision >= 0", name="ck_rapport_playback_generation"),
        sa.CheckConstraint("outcome IN ('played','failed')", name="ck_rapport_playback_outcome"),
        sa.CheckConstraint("question_idx >= 0", name="ck_rapport_playback_question"),
    )
    op.create_index("ix_rapportplaybackreceipt_session_id", "rapportplaybackreceipt", ["session_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if (bind.execute(sa.text("SELECT 1 FROM rapportplaybackreceipt LIMIT 1")).first()
            or bind.execute(sa.text("SELECT 1 FROM audioassetrow WHERE recording_wseq IS NOT NULL LIMIT 1")).first()
            or bind.execute(sa.text("SELECT 1 FROM audiocapturereceipt WHERE recording_wseq IS NOT NULL LIMIT 1")).first()):
        raise RuntimeError("rapport_generation_evidence_exists: downgrade would erase capture/playback evidence")
    op.drop_index("ix_rapportplaybackreceipt_session_id", table_name="rapportplaybackreceipt")
    op.drop_table("rapportplaybackreceipt")
    op.drop_column("audiocapturereceipt", "recording_wseq")
    op.drop_column("audioassetrow", "recording_wseq")
    op.drop_index("uq_rapport_auto_audio", table_name="rapportutteranceevent")
