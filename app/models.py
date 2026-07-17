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

from sqlalchemy import BigInteger, Column
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
    ai_judge_mode: Optional[str] = None          # 规则确定式 / LLM辅助（JudgingMode,审计用）
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
    # 录音落库时即绑定运行环节；即使尚未生成 TurnEvent/转写，重启后仍可恢复映射。
    turn_key: Optional[str] = None
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


class LiveState(SQLModel, table=True):
    """仪器实时状态(单行,id=1)——跨设备同步的服务端真值。

    此前同步只靠浏览器 BroadcastChannel(仅同机双窗可用);此表让老人端平板与
    操作端电脑分居两台设备时仍能经内网同步:操作端写、两端轮询,seq 单调递增判新旧。
    只存指针(索引/级别/id),不存题目文本——内容仍以版本锁定的题库为唯一源。
    """
    id: int = Field(default=1, primary_key=True)
    seq: int = 0
    command_wseq: int = Field(
        default=0, sa_column=Column(BigInteger, nullable=False, default=0))
    session_json: Optional[str] = None       # 最新 session 握手(JSON)
    cursor_json: Optional[str] = None        # 最新游标
    rapport_json: Optional[str] = None       # 最新第1周步进
    audio_json: Optional[str] = None         # 最新老人端录音回报(audioSaved)
    patient_rec_json: Optional[str] = None   # 老人端麦克风真值上报(patientRec):自助开录操作端可见、可远程停
    # 老人端在线/画面确认只保存设备运行指针，不保存患者信息、题目文本或回答内容。
    patient_ack_session_id: Optional[str] = None
    patient_current_screen: Optional[str] = None
    patient_last_seen_at: Optional[datetime] = None
    patient_ack_seq: Optional[int] = Field(
        default=None, sa_column=Column(BigInteger, nullable=True))
    updated_at: Optional[datetime] = None


class SessionRuntimeState(SQLModel, table=True):
    """每场次可恢复的运行游标。

    与 LiveState 的“当前广播快照”分开保存，防止切换受试者/场次时覆盖上一场的续做位置。
    这里只存冻结计划中的位置和暂停状态；录音字节、回答、判分仍各走原有表和门禁。
    """
    session_id: str = Field(primary_key=True, foreign_key="session.session_id")
    status: str = "active"                   # active / paused
    cursor_json: Optional[str] = None          # 正式训练安全恢复游标（录音恒 idle）
    rapport_json: Optional[str] = None         # 关系建立安全恢复游标（录音恒 idle）
    revision: int = 0
    paused_at: Optional[datetime] = None
    resumed_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


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


class AuditLog(SQLModel, table=True):
    """只追加的研究审计账本(防篡改 tamper-evident)。记录"谁在何时对研究真值做了什么",供论文溯源与核查。

    红线:summary 只记元数据(研究编号 + 动作 + 分值/类型),【绝不写入患者回答文本或姓名】——
    审计不能变成第二个泄露患者作答的地方。

    完整性保证的边界(诚实标注,勿夸大):
      * 哈希链:entry_hash=sha256(prev_hash|各字段)。改动/中间删除任一历史行 → 从该行起断链,verify 检出。
      * 高水位锚点(AuditAnchor):记录条数与链尾 hash,verify 比对 → 尾部截断/整表清空可检出(这是裸哈希链单靠自身检不出的)。
      * prev_hash 唯一约束:并发追加撞链(多 worker)在 DB 层被挡,不会静默分叉。
      * 【已知残余】裸 sha256 无密钥、锚点与账本同库:掌握 DB 写权且懂方案者可同时重算链+改锚点伪造。
        强保证需外部密钥 HMAC/逐条签名/把链尾定期锚定到外部不可变存储(WORM/公共时间戳)——留待后续。
        威胁模型:本工具服务可信研究者小组,审计用于诚实溯源、检出意外损坏与阻吓随手篡改。
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime
    actor: str                                    # 登录账号 display_id / "PIN/本地" / "system"
    action: str                                   # score_lock/response_confirm/scale_record/abnormal/audio_delete/data_export/login…
    patient_id: Optional[str] = Field(default=None, index=True)
    session_id: Optional[str] = Field(default=None, index=True)
    turn_id: Optional[int] = None
    summary: str                                  # 短元数据(无回答文本/姓名)
    prev_hash: str = Field(unique=True)           # 唯一 → 并发追加撞链在 DB 层被挡(防多 worker 静默分叉)
    entry_hash: str


class AuditAnchor(SQLModel, table=True):
    """审计高水位锚点(单行,id=1)。每次追加同事务更新条数与链尾 hash;
    verify 比对之 → 检出裸哈希链单靠自身检不出的"尾部截断/整表清空"。"""
    id: int = Field(default=1, primary_key=True)
    count: int = 0
    tip_hash: str = "0" * 64
    updated_at: Optional[datetime] = None


class ResearchUser(SQLModel, table=True):
    """研究者账号（公网部署的真实身份层，替代裸共享 PIN）。

    display_id 才是落到 TurnEvent.reviewer_id / ScaleResult.assessor_id 的审计标识，
    与登录名 username 解耦：登录名可改，历史锁分归属仍稳定指向 display_id。
    绝不存明文密码——password_hash 是 pbkdf2_sha256$iters$salt$hash（见 app/auth.py）。
    """
    username: str = Field(primary_key=True)
    display_id: str                               # 审计身份（谁锁的分/谁评的量表）
    password_hash: str
    role: str = "researcher"                       # researcher / admin（admin 可管账号）
    disabled: bool = False                          # 停用即时生效（会话校验时否决）
    created_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None


class AuthSession(SQLModel, table=True):
    """服务端会话（可撤销：登出/停用立即失效，优于无状态 JWT）。

    表里只存 token 的 sha256（token_hash），明文 token 只在浏览器 httponly cookie 里——
    数据库即便泄露也无法据此伪造有效会话。
    """
    token_hash: str = Field(primary_key=True)
    username: str = Field(foreign_key="researchuser.username", index=True)
    created_at: datetime
    expires_at: datetime
    last_seen_at: Optional[datetime] = None
