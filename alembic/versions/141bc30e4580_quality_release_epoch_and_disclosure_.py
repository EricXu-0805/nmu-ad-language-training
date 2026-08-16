"""quality release epoch, frozen cohort and disclosure ledger（研究分区披露控制）

Revision ID: 141bc30e4580
Revises: c5a8f2d91e40
Create Date: 2026-08-16

研究分区的 AI 质量聚合此前是**整体抑制**的，代码里那段注释自己写明了理由：
总人数门槛既挡不住稀疏单元，也挡不住"今天读一次、明天读一次求差"。这次迁移
建的是补上那两块所需要的持久化。

三张表：

- ``qualityreleaseepoch``：一次治理性"切纪元"，存的是**实际对外的那段字节**。
  读路径不重算任何东西、不查任何活表，取行、校验 sha256、json.loads 返回。
  同一纪元两次读之间的差恒为零。
- ``qualityreleaseepochsession``：纪元冻住的**场次**集合（只存假名与水位线）。
  冻场次而不是冻人：只冻人的话，给已在册的人追加新场次就能让聚合动。
- ``qualitydisclosurerecord``：谁读过哪一版。**追责，不是预算**——按纪元记，
  新纪元重置，没有任何次数上界。

downgrade fail-closed：已发布过纪元或已有取数记录即拒绝降级。已经发出去的
聚合和谁读过它，都是伦理材料要引用的事实。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 -- AutoString 类型


revision: str = "141bc30e4580"
down_revision: Union[str, Sequence[str], None] = "c5a8f2d91e40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _hex64(column: str) -> str:
    return (f"length({column}) = 64 AND lower({column}) = {column} "
            f"AND {column} GLOB '[0-9a-f]*'")


def upgrade() -> None:
    op.create_table(
        "qualityreleaseepoch",
        sa.Column("epoch_id", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("epoch_seq", sa.Integer(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("as_of", sa.DateTime(), nullable=False),
        sa.Column("frozen_at", sa.DateTime(), nullable=False),
        sa.Column("cohort_rule_version", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("registry_version", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("schema_version", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("cohort_size_band", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("session_count_band", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("payload_json", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("payload_sha256", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("min_subjects_applied", sa.Integer(), nullable=False),
        sa.Column("min_cell_subjects_applied", sa.Integer(), nullable=False),
        sa.Column("band_width_applied", sa.Integer(), nullable=False),
        sa.Column("rate_decimals_applied", sa.Integer(), nullable=False),
        sa.Column("diagnostics_status", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("deidentification_key_id",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("builder_actor_display_id",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("builder_actor_role", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("approver_actor_display_id",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("approver_actor_role", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("idempotency_key_sha256",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoke_reason_code", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=True),
        sa.PrimaryKeyConstraint("epoch_id"),
        sa.UniqueConstraint("epoch_seq", name="uq_quality_release_epoch_seq"),
        sa.UniqueConstraint("idempotency_key_sha256",
                            name="uq_quality_release_epoch_idempotency_hash"),
        sa.CheckConstraint(
            "status IN ('published','superseded','revoked')",
            name="ck_quality_release_epoch_status_closed"),
        sa.CheckConstraint(
            "epoch_seq >= 1", name="ck_quality_release_epoch_seq_positive"),
        sa.CheckConstraint(
            _hex64("payload_sha256"),
            name="ck_quality_release_epoch_payload_hash"),
        sa.CheckConstraint(
            _hex64("idempotency_key_sha256"),
            name="ck_quality_release_epoch_idempotency_hash_shape"),
        sa.CheckConstraint(
            "builder_actor_role IN ('data_steward','admin') "
            "AND approver_actor_role IN ('data_steward','admin')",
            name="ck_quality_release_epoch_actor_roles"),
        sa.CheckConstraint(
            "builder_actor_display_id <> approver_actor_display_id",
            name="ck_quality_release_epoch_two_person"),
        sa.CheckConstraint(
            "min_subjects_applied >= 2 AND min_cell_subjects_applied >= 2 "
            "AND band_width_applied >= 5 AND rate_decimals_applied >= 1",
            name="ck_quality_release_epoch_thresholds_sane"),
    )
    op.create_index(op.f("ix_qualityreleaseepoch_epoch_seq"),
                    "qualityreleaseepoch", ["epoch_seq"])
    op.create_index("ix_quality_release_epoch_status_seq",
                    "qualityreleaseepoch", ["status", "epoch_seq"])

    op.create_table(
        "qualityreleaseepochsession",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("epoch_id", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("session_pseudonym", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("evidence_watermark", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["epoch_id"], ["qualityreleaseepoch.epoch_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("epoch_id", "session_pseudonym",
                            name="uq_quality_release_epoch_session"),
    )
    op.create_index(op.f("ix_qualityreleaseepochsession_epoch_id"),
                    "qualityreleaseepochsession", ["epoch_id"])

    op.create_table(
        "qualitydisclosurerecord",
        sa.Column("record_id", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("epoch_id", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("actor_id", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("actor_role", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("payload_sha256", sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["epoch_id"], ["qualityreleaseepoch.epoch_id"]),
        sa.PrimaryKeyConstraint("record_id"),
        sa.CheckConstraint(
            _hex64("payload_sha256"),
            name="ck_quality_disclosure_payload_hash"),
    )
    op.create_index(op.f("ix_qualitydisclosurerecord_epoch_id"),
                    "qualitydisclosurerecord", ["epoch_id"])
    op.create_index("ix_quality_disclosure_epoch_actor",
                    "qualitydisclosurerecord", ["epoch_id", "actor_id"])


def _released_evidence_exists() -> bool:
    bind = op.get_bind()
    for table in ("qualityreleaseepoch", "qualitydisclosurerecord"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            return True
    return False


def downgrade() -> None:
    if _released_evidence_exists():
        raise RuntimeError(
            "已发布过研究分区纪元或已有取数记录，拒绝降级抹掉证据；"
            "发出去的聚合与谁读过它都要写进伦理材料，如确需回退先按治理流程导出")
    op.drop_index("ix_quality_disclosure_epoch_actor",
                  table_name="qualitydisclosurerecord")
    op.drop_index(op.f("ix_qualitydisclosurerecord_epoch_id"),
                  table_name="qualitydisclosurerecord")
    op.drop_table("qualitydisclosurerecord")
    op.drop_index(op.f("ix_qualityreleaseepochsession_epoch_id"),
                  table_name="qualityreleaseepochsession")
    op.drop_table("qualityreleaseepochsession")
    op.drop_index("ix_quality_release_epoch_status_seq",
                  table_name="qualityreleaseepoch")
    op.drop_index(op.f("ix_qualityreleaseepoch_epoch_seq"),
                  table_name="qualityreleaseepoch")
    op.drop_table("qualityreleaseepoch")
