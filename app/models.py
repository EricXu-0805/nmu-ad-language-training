"""数据表模型（M6 拥有）——共享数据契约的 DB 落地。

需 `pip install sqlmodel`。阶段0 核心逻辑（scoring/audio_gate/judging/enums）不依赖本文件，
可先无 DB 跑测试；本文件在接数据库时启用。

两条结构性硬约束在此体现：
  1. ★画像不进判分：Week1Profile 独立成表，与 ItemEvent/TurnEvent 评分链【无外键连接】，
     判分侧不得 join 本表（见 judging.py 的运行时守卫）。
  2. de_total / key_element_rate 【不落库存字段】——由 scoring.py 从分环节原始值计算，
     单一事实源；本表只存分环节锁定原始值。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlmodel import Field, SQLModel  # type: ignore

from .enums import (
    AudioStatus, ConsentType, PhaseType, EventLine, TaskType, ItemSetType,
)


class Patient(SQLModel, table=True):
    patient_id: str = Field(primary_key=True)
    dementia_severity: Optional[str] = None
    # 入组资格
    mandarin_eligible: Optional[bool] = None
    language_eligibility: Optional[str] = None
    # 合规字段（护栏2，全部 nullable 预留，SOP 未回也先留空）
    consent_status: Optional[str] = None
    consent_type: Optional[ConsentType] = None
    consent_time: Optional[datetime] = None
    consent_person: Optional[str] = None
    proxy_consent: Optional[bool] = None
    capacity_assessment_status: Optional[str] = None
    assent_obtained: Optional[bool] = None
    recording_allowed: Optional[bool] = None
    secondary_use_allowed: Optional[bool] = None
    withdrawal_status: Optional[str] = None
    ethics_approval_no: Optional[str] = None
    registration_no: Optional[str] = None


class Session(SQLModel, table=True):
    session_id: str = Field(primary_key=True)
    patient_id: str = Field(foreign_key="patient.patient_id", index=True)
    session_sitting_no: int = 1                 # 分日/暂停续做序号
    training_date: Optional[date] = None
    week_no: int                                # 1..8
    phase_type: PhaseType                        # 按事件显式绑定，不由 week_no 推导
    event_line: EventLine
    trainer_id: Optional[str] = None
    item_bank_version_id: str                    # 每场次绑冻结题库版本号


class ItemEvent(SQLModel, table=True):
    """一题一行（parent）。双/多要素的分环节在 TurnEvent。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="session.session_id", index=True)
    item_id: str = Field(index=True)
    image_id: Optional[str] = None
    task_type: TaskType
    item_set_type: ItemSetType
    difficulty_level: Optional[str] = None       # 仅记录，不据此分支
    presentation_order: Optional[int] = None
    random_seed: Optional[int] = None


class TurnEvent(SQLModel, table=True):
    """每环节一行（★ 支持双/多要素多轮）。单要素退化为单环节 turn_seq=1。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    item_event_id: int = Field(foreign_key="itemevent.id", index=True)
    turn_seq: int
    response_role: Optional[str] = None          # 左命名/左作用/关系识别/关键要素…
    # 语音 & 识别
    raw_audio_id: Optional[str] = None
    asr_text: Optional[str] = None               # 写入即只读
    asr_confidence: Optional[float] = None
    confirmed_response_text: Optional[str] = None  # 与 asr_text 物理分字段
    # 时间
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    naming_latency_ms: Optional[int] = None
    # 提示/线索
    prompt_level: Optional[int] = None
    cue_type: Optional[str] = None
    # AI 判定（仅辅助，永不覆盖锁定分）
    ai_answer_type: Optional[str] = None
    ai_score: Optional[float] = None
    ai_needs_review: Optional[bool] = None
    judge_portrait_used: bool = False            # 恒 False，审计用
    # 人工锁定评分（研究数据默认取此）
    reviewer_id: Optional[str] = None
    reviewed_score: Optional[float] = None
    score_locked: bool = False
    # 双要素分环节 0/1 或关系 1/0.5/0；多要素关键要素 0/1（原始锁定值，综合分由 scoring 算）
    element_value: Optional[float] = None


class Week1Profile(SQLModel, table=True):
    """★ 第1周画像——独立命名空间。判分链不得 join。只喂交互侧。"""
    patient_id: str = Field(primary_key=True, foreign_key="patient.patient_id")
    preferred_appellation: Optional[str] = None
    zodiac: Optional[str] = None
    interests: Optional[str] = None
    daily_activities: Optional[str] = None
    familiar_places: Optional[str] = None
    familiar_people: Optional[str] = None
    common_objects: Optional[str] = None
    willing_to_continue: Optional[bool] = None


class ScaleResult(SQLModel, table=True):
    """前后测量表结果容器——量表选型待 PI（含功能沟通量表 CETI/CADL），此容器与量表无关、通用。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    patient_id: str = Field(foreign_key="patient.patient_id", index=True)
    phase_type: PhaseType                         # 前测 / 后测 / 随访
    scale_name: str                               # 具体量表名（待 PI 定，字段先通用）
    subscale: Optional[str] = None
    score: Optional[float] = None
    assessed_at: Optional[datetime] = None
    assessor_id: Optional[str] = None


class AudioAssetRow(SQLModel, table=True):
    """音频资产（护栏1）。状态机逻辑见 audio_gate.py。"""
    raw_audio_id: str = Field(primary_key=True)
    session_id: Optional[str] = Field(default=None, foreign_key="session.session_id")
    audio_format: str = "mp3"                     # 信度/争议子集用 wav（无损/高码率）
    status: AudioStatus = AudioStatus.recorded
    is_reliability_sample: bool = False
    withdrawn: bool = False
    withdrawal_status: Optional[str] = None       # 撤回旁路：withdrawal_requested→isolated（待 SOP）
    checksum: Optional[str] = None
    contains_direct_identifier: bool = False      # 第1周自我介绍段默认置位
    export_batch_id: Optional[str] = None         # 导出批次（导出成功回写）
    delete_gate_passed: bool = False              # 闸门放行标记（审计）
    exported_at: Optional[datetime] = None


class AbnormalEvent(SQLModel, table=True):
    """异常/介入记录（phase 感知）。正式训练周的代说物品名/称呼→线索性介入、影响判分有效性。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="session.session_id", index=True)
    item_event_id: Optional[int] = Field(default=None, foreign_key="itemevent.id")
    phase_type: Optional[PhaseType] = None        # 记录发生时相位，供 phase 感知审计
    abnormal_type: Optional[str] = None           # AbnormalType 取值（如 长时间沉默/线索性介入）
    intervention_type: Optional[str] = None       # InterventionType 取值（如 代说物品名）
    affects_scoring_validity: bool = False        # 是否影响该环节/题判分有效性
    note: Optional[str] = None                    # caregiver_note / session_note
    created_at: Optional[datetime] = None
