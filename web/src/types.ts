// 契约类型——逐字镜像后端共享数据契约(app/enums.py + app/models.py + API 响应)。
// ★画像字段(preferred_appellation/zodiac/interests…)刻意不在任何判分/评分相关类型里出现。

export type PhaseType = "关系建立" | "基线测评" | "正式训练" | "前测" | "后测" | "随访" | "探针测评";
export type EventLine = "关系建立环节" | "基线测评窗" | "正式训练";
export type TaskType = "单要素" | "双要素" | "多要素" | "关系建立";
export type ItemSetType = "训练集" | "探针集" | "画像采集";
export type ConsentType = "本人同意" | "代理同意加本人赞同";
export type AnswerType = "正确" | "部分正确" | "上位词或相关词" | "偏题" | "重复" | "未识别";
export type AudioStatus =
  | "recorded" | "exported" | "checksum_verified"
  | "reliability_review_done" | "deletable" | "deleted";

// 双要素固定 5 环节角色 + 各自锁分字段
export const DOUBLE_ROLES = ["左命名", "左作用", "右命名", "右作用", "关系识别"] as const;
export type DoubleRole = (typeof DOUBLE_ROLES)[number];

export interface Patient {
  patient_id: string;
  dementia_severity?: string | null;
  mandarin_eligible?: boolean | null;
  language_eligibility?: string | null;
  consent_status?: string | null;
  consent_type?: ConsentType | null;
  consent_person?: string | null;
  proxy_consent?: boolean | null;
  capacity_assessment_status?: string | null;
  assent_obtained?: boolean | null;
  recording_allowed?: boolean | null;
  secondary_use_allowed?: boolean | null;
  withdrawal_status?: string | null;
  ethics_approval_no?: string | null;
  registration_no?: string | null;
}

export interface Session {
  session_id: string;
  patient_id: string;
  session_sitting_no?: number;
  training_date?: string | null;
  week_no: number;
  phase_type: PhaseType;
  event_line: EventLine;
  trainer_id?: string | null;
  item_bank_version_id: string;
}

// GET /sessions/{sid}/plan
export interface PlanTurn {
  turn_seq: number;
  response_role: string; // "命名" | DoubleRole | 关键要素名
  scoring_key: string | null;
}
export interface PlanItem {
  item_id: string;
  task_type: TaskType;
  image_id: string | null;
  presentation_order: number;
  display: Record<string, unknown>; // {target_word|pair_title|left_word|right_word|cues|tell_answer|success_line}
  turns: PlanTurn[];
}
export interface SessionPlan {
  item_bank_version_id: string;
  week_no: number;
  event_line: string;
  total_items: number;
  total_turns: number;
  items: PlanItem[];
}

export interface ItemEvent {
  id: number;
  session_id: string;
  item_id: string;
  image_id?: string | null;
  task_type: TaskType;
  item_set_type: ItemSetType;
  difficulty_level?: string | null;
  presentation_order?: number | null;
}

export interface TurnEvent {
  id: number;
  item_event_id: number;
  turn_seq: number;
  response_role?: string | null;
  raw_audio_id?: string | null;
  asr_text?: string | null; // 写入即只读——UI 绝不覆盖原文
  asr_confidence?: number | null;
  confirmed_response_text?: string | null;
  start_time?: string | null;
  end_time?: string | null;
  duration_seconds?: number | null;
  naming_latency_ms?: number | null;
  prompt_level?: number | null;
  cue_type?: string | null;
  ai_answer_type?: string | null; // 仅辅助
  ai_score?: number | null;
  ai_needs_review?: boolean | null;
  judge_portrait_used: boolean; // 恒 false
  reviewer_id?: string | null;
  reviewed_score?: number | null;
  score_locked: boolean;
  element_value?: number | null;
}

export interface AbnormalEvent {
  id: number;
  session_id: string;
  item_event_id?: number | null;
  phase_type?: PhaseType | null;
  abnormal_type?: string | null;
  intervention_type?: string | null;
  affects_scoring_validity: boolean;
  note?: string | null;
  created_at?: string | null;
}

export interface AudioAsset {
  raw_audio_id: string;
  session_id?: string | null;
  audio_format: string;
  status: AudioStatus;
  is_reliability_sample: boolean;
  withdrawn: boolean;
  withdrawal_status?: string | null;
  checksum?: string | null;
  contains_direct_identifier: boolean;
  export_batch_id?: string | null;
  delete_gate_passed?: boolean;
  exported_at?: string | null;
}

// 判分侧运行时守卫用:这些键一旦出现在 lock 载荷/判分视图 = 违反『画像不进判分』。
export const PORTRAIT_FIELDS = [
  "preferred_appellation", "zodiac", "interests", "daily_activities",
  "familiar_places", "familiar_people", "common_objects", "willing_to_continue",
] as const;

// 冻结枚举的运行时取值(下拉用;禁止各处本地私造)。语义与 app/enums.py 一致。
export const PHASE_TYPES: PhaseType[] = ["关系建立", "基线测评", "正式训练", "前测", "后测", "随访", "探针测评"];
export const EVENT_LINES: EventLine[] = ["关系建立环节", "基线测评窗", "正式训练"];
export const CONSENT_TYPES: ConsentType[] = ["本人同意", "代理同意加本人赞同"];
export const INTERVENTION_TYPES = [
  "操作设备", "安抚情绪", "帮助听清任务", "重复系统已说题目", "修正ASR转写",
  "暂停或结束训练", "代说物品名", "代说称呼",
] as const;
export const ABNORMAL_TYPES = [
  "拒绝继续", "明显疲劳", "长时间沉默", "激越烦躁", "情绪低落",
  "线索性介入", "环境噪声", "设备或网络中断", "ASR严重错误", "中途结束",
] as const;
// 正式训练周属越界的线索性介入(与后端 phase 感知规则一致)
export const CUE_INTERVENTIONS = ["代说物品名", "代说称呼"] as const;

// 量表结果(容器通用,量表选型待 PI)
export interface ScaleResult {
  id?: number;
  patient_id: string;
  phase_type: PhaseType;
  scale_name: string;
  subscale?: string | null;
  score?: number | null;
  assessed_at?: string | null;
  assessor_id?: string | null;
}
// 量表录入允许的相位(前/后测+随访,非训练相位)
export const SCALE_PHASES: PhaseType[] = ["前测", "后测", "随访"];

// GET /content/item-bank
export interface ItemBankInfo {
  version_id: string;
  single_count: number;
  double_count: number;
  errata_fixed: { item: string; corrected_to: string }[];
  errors: string[];
  warnings: string[];
}

// GET /sessions/{sid}/scores（重建评分,可空表示该类无已锁定题）
export interface ScoreReconstruction {
  excluded_items: string[];
  single: Record<string, unknown> | null;
  double: Record<string, unknown> | null;
  multi: Record<string, unknown> | null;
}

// POST /sessions/{sid}/export
export interface ExportResult {
  batch_id: string;
  deidentified: boolean;
  files: string[];
  audio_touched: string[];
  excluded_items: string[];
  sheet_counts: Record<string, number>;
}
