"""append-only per-item assessment recording authorization receipts

Revision ID: b8e5f2a91c07
Revises: f7c2e8a4d105
Create Date: 2026-08-08

逐题录音授权收据(收据 150 S3):服务端签发、绑定 patient/event/instance/item/
item_revision、一次性消费的 authorized_artifact_digest 来源。响应表上的全局
UNIQUE 是第二道复用 fence。downgrade fail-closed:已有授权收据即拒绝降级。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 -- AutoString 类型


revision: str = "b8e5f2a91c07"
down_revision: Union[str, Sequence[str], None] = "f7c2e8a4d105"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assessmentrecordingauthorization",
        sa.Column("authorization_id",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("event_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("instance_id",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("patient_id",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("item_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("item_revision", sa.Integer(), nullable=False),
        sa.Column("authorization_digest",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("issued_by", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "length(authorization_digest) = 71",
            name="ck_assessment_rec_auth_digest_length"),
        sa.CheckConstraint(
            "item_revision >= 1",
            name="ck_assessment_rec_auth_revision_positive"),
        sa.ForeignKeyConstraint(["event_id"], ["assessmentevent.event_id"]),
        sa.ForeignKeyConstraint(
            ["instance_id"], ["assessmentinstance.instance_id"]),
        sa.ForeignKeyConstraint(["patient_id"], ["patient.patient_id"]),
        sa.PrimaryKeyConstraint("authorization_id"),
        sa.UniqueConstraint(
            "authorization_digest", name="uq_assessment_rec_auth_digest"),
    )
    op.create_index(
        "ix_assessmentrecordingauthorization_event_id",
        "assessmentrecordingauthorization", ["event_id"], unique=False)
    op.create_index(
        "ix_assessmentrecordingauthorization_instance_id",
        "assessmentrecordingauthorization", ["instance_id"], unique=False)
    op.create_index(
        "ix_assessmentrecordingauthorization_patient_id",
        "assessmentrecordingauthorization", ["patient_id"], unique=False)
    op.create_index(
        "ix_assessment_rec_auth_instance",
        "assessmentrecordingauthorization", ["instance_id", "item_key"],
        unique=False)


def _authorization_evidence_exists() -> bool:
    bind = op.get_bind()
    row = bind.execute(sa.text(
        "SELECT 1 FROM assessmentrecordingauthorization LIMIT 1")).first()
    return row is not None


def downgrade() -> None:
    if _authorization_evidence_exists():
        raise RuntimeError(
            "assessmentrecordingauthorization 已有授权收据,拒绝降级抹掉证据;"
            "如确需回退,先按治理流程导出并留存收据")
    op.drop_index(
        "ix_assessment_rec_auth_instance",
        table_name="assessmentrecordingauthorization")
    op.drop_index(
        "ix_assessmentrecordingauthorization_patient_id",
        table_name="assessmentrecordingauthorization")
    op.drop_index(
        "ix_assessmentrecordingauthorization_instance_id",
        table_name="assessmentrecordingauthorization")
    op.drop_index(
        "ix_assessmentrecordingauthorization_event_id",
        table_name="assessmentrecordingauthorization")
    op.drop_table("assessmentrecordingauthorization")
