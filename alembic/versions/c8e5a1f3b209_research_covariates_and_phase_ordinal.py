"""research covariates, questionnaire phase ordinal, drop the dead latency column

Revision ID: c8e5a1f3b209
Revises: b6d4f8a2c917
Create Date: 2026-08-27

三件事，趁库里只有 3 个演示档做——等 30 人跑起来，同一件事要在有临床数据的库上做。

1. **Patient 加四个研究协变量**（birth_year / sex / education_years / study_arm）。
   没有它们，「训练前后量表变化、年龄与受教育年限做协变量」这张表结构上做不出来。
   假名是单向 HMAC、平台又不存人口学，两边没有可用连接键——不是拉不到数，是拉到
   的数不能用，而且要等统计那天才发现。**出生年而不是出生日期**：绝对日期不进任何
   导出面，年份足够算年龄。四列全可空：存量档案没有这些值，不许瞎填。

2. **QuestionnaireRecord 加 phase_ordinal + superseded_by_ordinal，并给
   (patient_id, questionnaire_id, phase_label, phase_ordinal) 加唯一约束。**
   手册规定的改错方式是「新建一条正确的，在备注里说明」，而备注永不进导出、
   record_id 是随机串、导出还按它排序——于是导出里出现两行同为「前测」，总分一个
   32 一个 41，分析脚本取 first 或 max 都可能取到作废那条，从数据本身发现不了。
   两列都是相对量，不违反「绝对时间不出接口」。

3. **冻结纪元行快照的 dataset_key 闭集扩到 5 个**（随注册表前进）。

4. **删掉 turnevent.naming_latency_ms**。建表以来零写入点，恒为 None，却在被
   喂给量表 AI 初评当证据（`mean_naming_latency_ms: null`），还让读 schema 的人
   以为平台有反应时。真要做反应时，先定「潜伏期从哪一刻起算」，再连同写入路径
   一起加回来。

Downgrade：允许，但**只在没有任何非空协变量、也没有任何 phase_ordinal > 1 的记录时**。
一旦有人填过协变量或建过第二条同期别记录，降级会静默丢临床证据，此时拒绝。
naming_latency_ms 恒空，加回来是无损的。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 —— autogenerate 产出的 sqlmodel.AutoString 等类型需要


# revision identifiers, used by Alembic.
revision: str = 'c8e5a1f3b209'
down_revision: Union[str, Sequence[str], None] = 'b6d4f8a2c917'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # SQLite 加带 CHECK 的列必须走 batch：它整表重建，约束才落得进去。
    with op.batch_alter_table("patient") as batch:
        batch.add_column(sa.Column("birth_year", sa.Integer(), nullable=True))
        batch.add_column(sa.Column(
            "sex", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch.add_column(sa.Column("education_years", sa.Integer(), nullable=True))
        batch.add_column(sa.Column(
            "study_arm", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch.create_check_constraint(
            "ck_patient_sex", "sex IS NULL OR sex IN ('男','女','其他','未记录')")
        batch.create_check_constraint(
            "ck_patient_birth_year",
            "birth_year IS NULL OR (birth_year >= 1900 AND birth_year <= 2100)")
        batch.create_check_constraint(
            "ck_patient_education_years",
            "education_years IS NULL OR "
            "(education_years >= 0 AND education_years <= 30)")

    # phase_ordinal 非空：存量行一律补 1（它们就是各自期别的第一条）。
    # server_default 只为这一次回填存在，回填完立刻撤掉——留着会让下一个人
    # 以为「不传也行」，而应用层是显式算序号的。
    with op.batch_alter_table("questionnairerecord") as batch:
        batch.add_column(sa.Column(
            "phase_ordinal", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column(
            "superseded_by_ordinal", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            "ck_questionnaire_record_phase_ordinal", "phase_ordinal >= 1")
        batch.create_check_constraint(
            "ck_questionnaire_record_supersede_forward",
            "superseded_by_ordinal IS NULL OR superseded_by_ordinal > phase_ordinal")
        batch.create_unique_constraint(
            "uq_questionnaire_record_phase_slot",
            ["patient_id", "questionnaire_id", "phase_label", "phase_ordinal"])
    with op.batch_alter_table("questionnairerecord") as batch:
        batch.alter_column("phase_ordinal", server_default=None)

    with op.batch_alter_table("turnevent") as batch:
        batch.drop_column("naming_latency_ms")

    # 冻结纪元行快照的 dataset_key 闭集随注册表前进：加入两张量表表。
    # 不扩这条 CHECK，切纪元时写不进去（IntegrityError），而这正是它该做的。
    with op.batch_alter_table("qualityreleaseepochrowsnapshot") as batch:
        batch.drop_constraint(
            "ck_quality_release_epoch_row_snapshot_dataset_closed", type_="check")
        batch.create_check_constraint(
            "ck_quality_release_epoch_row_snapshot_dataset_closed",
            "dataset_key IN ('subjects','sessions','turns',"
            "'questionnaire_records','questionnaire_item_values')")


def downgrade() -> None:
    """Downgrade schema（有临床证据时 fail-closed）。"""
    bind = op.get_bind()
    filled = bind.execute(sa.text(
        "SELECT COUNT(*) FROM patient WHERE birth_year IS NOT NULL "
        "OR sex IS NOT NULL OR education_years IS NOT NULL "
        "OR study_arm IS NOT NULL")).scalar_one()
    if filled:
        raise RuntimeError(
            f"{filled} 位受试者已填研究协变量，降级会把它们丢掉；先导出留证再说")
    superseded = bind.execute(sa.text(
        "SELECT COUNT(*) FROM questionnairerecord "
        "WHERE phase_ordinal > 1 OR superseded_by_ordinal IS NOT NULL")).scalar_one()
    if superseded:
        raise RuntimeError(
            f"{superseded} 条量表记录带着期别序号或作废指向，降级会让"
            "「哪条作数」重新变得不可分辨；拒绝")

    stale = bind.execute(sa.text(
        "SELECT COUNT(*) FROM qualityreleaseepochrowsnapshot "
        "WHERE dataset_key IN ('questionnaire_records','questionnaire_item_values')"
    )).scalar_one()
    if stale:
        raise RuntimeError(
            f"{stale} 行已冻结的量表快照会被闭集拒绝；降级会让那个纪元读不出来")
    with op.batch_alter_table("qualityreleaseepochrowsnapshot") as batch:
        batch.drop_constraint(
            "ck_quality_release_epoch_row_snapshot_dataset_closed", type_="check")
        batch.create_check_constraint(
            "ck_quality_release_epoch_row_snapshot_dataset_closed",
            "dataset_key IN ('subjects','sessions','turns')")
    with op.batch_alter_table("turnevent") as batch:
        batch.add_column(sa.Column("naming_latency_ms", sa.INTEGER(), nullable=True))
    with op.batch_alter_table("questionnairerecord") as batch:
        batch.drop_constraint("uq_questionnaire_record_phase_slot", type_="unique")
        batch.drop_constraint(
            "ck_questionnaire_record_supersede_forward", type_="check")
        batch.drop_constraint(
            "ck_questionnaire_record_phase_ordinal", type_="check")
        batch.drop_column("superseded_by_ordinal")
        batch.drop_column("phase_ordinal")
    with op.batch_alter_table("patient") as batch:
        batch.drop_constraint("ck_patient_education_years", type_="check")
        batch.drop_constraint("ck_patient_birth_year", type_="check")
        batch.drop_constraint("ck_patient_sex", type_="check")
        batch.drop_column("study_arm")
        batch.drop_column("education_years")
        batch.drop_column("sex")
        batch.drop_column("birth_year")
