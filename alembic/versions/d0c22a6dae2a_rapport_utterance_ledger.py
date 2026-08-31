"""第1周关系建立:机器人发声账本 + TTS 服务证据扩来源

Revision ID: d0c22a6dae2a
Revises: c8e5a1f3b209
Create Date: 2026-08-31

回应句从「照冻结脚本可还原」变成「可选/可生成(LLM)」之后,机器人在回应拍
对老人说了哪句,若不落库便无法事后还原——五十九轮明示的缺口,Eric 2026-08-31
拍板要记。两件事:

1. **新表 rapportutteranceevent**:只追加发声账本。``text`` 恒为最终说出口的
   成句(槽位已回落);``asr_text`` 仅 llm 来源持有,是喂给生成器的老人转写
   ——患者数据,不进任何研究导出/研究只读接口(research_dataset 注册表闭集
   不含它,quality_release 纪元快照只迭代注册表,故三处零改动)。

2. **ttsserveevidence.source 闭集扩第三值 'rapport_utterance' + 可空
   utterance_id**:LLM 生成的回应句不在云 TTS 静态白名单里,发声走"服务端
   持久 utterance 行"的专用通道;它的服务证据若伪装成 live_speak,自由生成
   话术与通用手动合成在账本里就不可区分。绑定约束保证三种来源各自的指针
   形态(autopilot 带 command_id、live_speak 两者皆空、rapport_utterance 带
   utterance_id)。utterance_id 不设 FK:batch 重建表时给既有行补无名内联
   FK 会破坏 DDL 与模型的逐字对齐,归属由应用层在服务时校验。

Downgrade:任一新证据行存在(rapportutteranceevent 有行,或 ttsserveevidence
带 rapport_utterance 来源)即拒绝——临床发声证据不得因回滚蒸发。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 —— autogenerate 产出的 sqlmodel.AutoString 等类型需要


# revision identifiers, used by Alembic.
revision: str = 'd0c22a6dae2a'
down_revision: Union[str, Sequence[str], None] = 'c8e5a1f3b209'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "rapportutteranceevent",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("section_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("question_idx", sa.Integer(), nullable=False),
        sa.Column("source", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("origin", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("reply_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("text", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("asr_text", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("asr_engine_version",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("reply_engine_version",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("degraded_reason",
                  sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("raw_audio_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("text_sha256", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("is_simulation", sa.Boolean(), nullable=False),
        sa.CheckConstraint("event_seq >= 1",
                           name="ck_rapport_utterance_event_seq_positive"),
        sa.CheckConstraint("source IN ('script','bank','llm','fallback')",
                           name="ck_rapport_utterance_source"),
        sa.CheckConstraint("origin IN ('auto','manual')",
                           name="ck_rapport_utterance_origin"),
        sa.CheckConstraint("length(trim(text)) >= 1",
                           name="ck_rapport_utterance_text_nonempty"),
        sa.CheckConstraint("question_idx >= 0",
                           name="ck_rapport_utterance_question_idx"),
        sa.ForeignKeyConstraint(["session_id"], ["session.session_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "event_seq",
                            name="uq_rapport_utterance_session_event_seq"),
    )
    op.create_index(op.f("ix_rapportutteranceevent_session_id"),
                    "rapportutteranceevent", ["session_id"], unique=False)
    op.create_index(op.f("ix_rapportutteranceevent_source"),
                    "rapportutteranceevent", ["source"], unique=False)

    # SQLite 改 CHECK = batch 整表重建(既有证据行原样拷贝)。
    with op.batch_alter_table("ttsserveevidence") as batch:
        batch.add_column(sa.Column("utterance_id", sa.Integer(), nullable=True))
        batch.drop_constraint("ck_tts_serve_source", type_="check")
        batch.create_check_constraint(
            "ck_tts_serve_source",
            "source IN ('autopilot_command','live_speak','rapport_utterance')")
        batch.drop_constraint("ck_tts_serve_command_binding", type_="check")
        batch.create_check_constraint(
            "ck_tts_serve_command_binding",
            "(source = 'autopilot_command' AND command_id IS NOT NULL "
            "AND utterance_id IS NULL) OR "
            "(source = 'live_speak' AND command_id IS NULL "
            "AND utterance_id IS NULL) OR "
            "(source = 'rapport_utterance' AND command_id IS NULL "
            "AND utterance_id IS NOT NULL)")
    op.create_index(op.f("ix_ttsserveevidence_utterance_id"),
                    "ttsserveevidence", ["utterance_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema（有发声证据时 fail-closed）。"""
    bind = op.get_bind()
    utterances = bind.execute(sa.text(
        "SELECT COUNT(*) FROM rapportutteranceevent")).scalar_one()
    if utterances:
        raise RuntimeError(
            f"{utterances} 条机器人发声记录在账;降级会让「机器人对老人说过什么」"
            "重新变得不可还原;先导出留证再说")
    served = bind.execute(sa.text(
        "SELECT COUNT(*) FROM ttsserveevidence "
        "WHERE source = 'rapport_utterance'")).scalar_one()
    if served:
        raise RuntimeError(
            f"{served} 条回应句服务证据会被收窄后的闭集拒绝;降级会让这些行"
            "读不出来;拒绝")

    op.drop_index(op.f("ix_ttsserveevidence_utterance_id"),
                  table_name="ttsserveevidence")
    with op.batch_alter_table("ttsserveevidence") as batch:
        batch.drop_constraint("ck_tts_serve_command_binding", type_="check")
        batch.create_check_constraint(
            "ck_tts_serve_command_binding",
            "(source = 'autopilot_command' AND command_id IS NOT NULL) OR "
            "(source = 'live_speak' AND command_id IS NULL)")
        batch.drop_constraint("ck_tts_serve_source", type_="check")
        batch.create_check_constraint(
            "ck_tts_serve_source",
            "source IN ('autopilot_command','live_speak')")
        batch.drop_column("utterance_id")
    op.drop_index(op.f("ix_rapportutteranceevent_source"),
                  table_name="rapportutteranceevent")
    op.drop_index(op.f("ix_rapportutteranceevent_session_id"),
                  table_name="rapportutteranceevent")
    op.drop_table("rapportutteranceevent")
