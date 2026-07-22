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
export type SessionRuntimeStatus =
  | "active"
  | "paused"
  | "intervention_completed"
  | "completed"
  | "aborted"
  | "failed";

export type VisitPlanStatus = "draft" | "approved" | "started" | "cancelled";
export type VisitPlanCancelReason =
  | "schedule_changed"
  | "participant_unavailable"
  | "researcher_unavailable"
  | "protocol_correction"
  | "duplicate_plan";

export interface VisitPlanCreateRequest {
  idempotency_key: string;
  patient_id: string;
  scheduled_date: string;
  scheduled_time?: string | null;
  queue_order?: number | null;
  session_sitting_no?: number;
  week_no: number;
  phase_type: PhaseType;
  event_line: EventLine;
}

export interface VisitPlanMutationRequest {
  idempotency_key: string;
  expected_revision: number;
}

export interface VisitPlanCancelRequest extends VisitPlanMutationRequest {
  reason_code: VisitPlanCancelReason;
}

export interface VisitPlanReceipt {
  plan_id: string;
  patient_id: string;
  scheduled_date: string;
  scheduled_time: string | null;
  queue_order: number | null;
  session_sitting_no: number;
  week_no: number;
  phase_type: PhaseType;
  event_line: EventLine;
  item_bank_version_id: string;
  is_simulation: boolean;
  data_classification: "research" | "simulation";
  status: VisitPlanStatus;
  revision: number;
  created_by: string;
  created_at: string;
  approved_by: string | null;
  approved_at: string | null;
  started_by: string | null;
  started_at: string | null;
  cancelled_by: string | null;
  cancelled_at: string | null;
  session_id: string | null;
}

export interface VisitPlanToday {
  as_of_date: string;
  plans: VisitPlanReceipt[];
}

// 双要素固定 5 环节角色 + 各自锁分字段
export const DOUBLE_ROLES = ["左命名", "左作用", "右命名", "右作用", "关系识别"] as const;
export type DoubleRole = (typeof DOUBLE_ROLES)[number];

export interface Patient {
  patient_id: string;
  is_simulation_subject: boolean;
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
  cloud_processing_allowed?: boolean | null;
  cloud_processing_provider_id?: string | null;
  cloud_processing_notice_version?: string | null;
  cloud_processing_consented_at?: string | null;
  cloud_processing_revoked_at?: string | null;
  withdrawal_status?: string | null;
  governance_revision: number;
  ethics_approval_no?: string | null;
  registration_no?: string | null;
}

export interface CloudProcessingPolicy {
  configured: boolean;
  provider_id: string | null;
  notice_version: string | null;
  data_categories: string[];
}

// 受试者登记表摘要(准备区/训练台/分析后台选择列表;无姓名、无场次编号)。
export interface PatientSummary {
  patient_id: string;
  is_simulation_subject: boolean;
  dementia_severity?: string | null;
  mandarin_eligible?: boolean | null;
  consent_status?: string | null;
  consent_type?: ConsentType | null;
  recording_allowed?: boolean | null;
  cloud_processing_allowed?: boolean | null;
  cloud_processing_provider_id?: string | null;
  cloud_processing_notice_version?: string | null;
  withdrawal_status?: string | null;
  governance_revision: number;
  withdrawal_event_id?: string | null;
  withdrawal_reason_code?: WithdrawalReasonCode | null;
  withdrawal_occurred_at?: string | null;
  research_eligible?: boolean;
  research_eligibility_issues?: string[];
  session_count: number;
  unfinished_session_count?: number;
  last_training_date?: string | null;
}

export type WithdrawalReasonCode =
  | "participant_request"
  | "representative_request"
  | "clinical_safety"
  | "ethics_or_protocol";

export interface PatientWithdrawalReceipt {
  schema_version: 1;
  event_id: string;
  patient_id: string;
  withdrawal_status: "withdrawn";
  consent_status: "withdrawn";
  expected_governance_revision: number;
  governance_revision: number;
  reason_code: WithdrawalReasonCode;
  actor_display_id: string;
  actor_role: "admin";
  occurred_at: string;
  affected_session_count: number;
  affected_audio_count: number;
  request_fingerprint: string;
  idempotent: boolean;
}

export interface WithdrawnAudioGovernanceRow {
  raw_audio_id: string;
  session_id: string;
  patient_id: string;
  status: string;
  withdrawn: boolean;
  withdrawal_status: string | null;
  delete_gate_passed: boolean;
}

// 账号认证(M1-D 公网部署)。绝不携带任何凭据,只表明该显示哪种门。
export interface AuthConfig {
  auth_required: boolean;     // 认证是否生效(回环开发可为 false → 全开)
  accounts_enabled: boolean;  // 库里已有研究者账号 → console 走登录门
  pin_enabled: boolean;       // 设了 CONSOLE_PIN → 老人端/保底走 PIN
}

export interface AuthIdentity {
  display_id: string;         // 落到锁分/量表的审计身份
  role: string;               // researcher / admin / data_steward
  username: string;
}

// 研究审计账本条目(只读;只含元数据,永不含患者作答文本/姓名)。
export interface AuditEntry {
  id: number;
  ts: string;
  actor: string;              // 登录账号 display_id / "PIN/本地" / "system"
  action: string;             // score_lock / scale_record / abnormal / audio_delete / data_export / login
  patient_id?: string | null;
  session_id?: string | null;
  turn_id?: number | null;
  summary: string;
  entry_hash: string;
}

export interface AuditVerify {
  ok: boolean;
  count: number;
  broken_at: number | null;
  problem?: "chain_broken" | "truncated" | "anchor_behind" | null;
  expected_count?: number;
}

export interface Session {
  session_id: string;
  visit_plan_id?: string | null;
  patient_id: string;
  // 服务端持久化的数据边界：模拟数据永远不能被静默当成真实研究数据。
  is_simulation: boolean;
  data_classification?: "research" | "simulation" | "legacy_unknown";
  session_sitting_no?: number;
  training_date?: string | null;
  week_no: number;
  phase_type: PhaseType;
  event_line: EventLine;
  trainer_id?: string | null;
  item_bank_version_id: string;
  // 服务端冻结的题库与自动化协议定义。历史场次可为 null，
  // VisitPlan 启动的新场次必须三项完整。
  item_bank_definition_digest?: string | null;
  autopilot_protocol_version_id?: string | null;
  autopilot_protocol_definition_digest?: string | null;
  // 旧后端不携带时按 active 兼容；终态场次只读，不可续做。
  runtime_status?: SessionRuntimeStatus;
}

export interface SessionRuntimeCursor {
  sessionId?: string;
  itemIdx: number;
  turnIdx: number;
  screen?: string;
  cueLevel?: number;
  recording?: string;
  responseRole?: string;
  recSeq?: number;
  selfStart?: boolean;
  fbKey?: string;
  fbItemId?: string;
  fbSeq?: number;
  wseq?: number;
}

export interface SessionRuntimeRapportStep {
  sessionId?: string;
  sectionKey: string;
  questionIdx: number;
  recording?: string;
  recSeq?: number;
  paused?: boolean;
  assentGate?: boolean;
  containsDirectIdentifier?: boolean;
  wseq?: number;
}

export interface SessionRuntimeState {
  sessionId: string;
  status: SessionRuntimeStatus;
  revision: number;
  cursor: SessionRuntimeCursor | null;
  rapportStep: SessionRuntimeRapportStep | null;
  pausedAt?: string | null;
  resumedAt?: string | null;
  interventionCompletedAt?: string | null;
  interventionEndedBy?: string | null;
  completedAt?: string | null;
  abortedAt?: string | null;
  endedBy?: string | null;
  endReason?: string | null;
  updatedAt?: string | null;
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

// 老人端在场确认：只上报场次、当前屏幕和已看到的操作端写序号，
// 不携带题目文本、回答内容或任何画像字段。服务端时间才是在线真值。
export type PatientPresenceScreen =
  | "waiting" | "loading" | "rapport" | "present" | "record"
  | "thanks" | "paused" | "complete" | "error";

export interface PatientHeartbeatRequest {
  session_id: string;
  screen: PatientPresenceScreen;
  cursor_wseq?: number;
  client_ts?: string;
}

export interface PatientPresence {
  session_id: string | null;
  screen: PatientPresenceScreen | null;
  last_seen_at: string | null;
  online: boolean;
  cursor_wseq?: number | null;
}

export interface PatientHeartbeatResponse {
  ok: boolean;
  server_time: string;
  patientPresence: PatientPresence;
}

// 老后端没有 patientPresence；保持可选即可让新版前端继续读取旧响应。
export interface LiveStateResponse {
  seq: number;
  session: unknown;
  cursor: unknown;
  rapportStep: unknown;
  audioSaved: unknown;
  patientRec: unknown;
  patientPresence?: PatientPresence | null;
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
  source_attempt_id?: number | null;
  turn_seq: number;
  response_role?: string | null;
  raw_audio_id?: string | null;
  asr_text?: string | null; // 写入即只读——UI 绝不覆盖原文
  asr_confidence?: number | null;
  confirmed_response_text?: string | null;
  confirmation_revision: number;
  start_time?: string | null;
  end_time?: string | null;
  duration_seconds?: number | null;
  naming_latency_ms?: number | null;
  prompt_level?: number | null;
  cue_type?: string | null;
  ai_answer_type?: string | null; // 仅辅助
  ai_score?: number | null;
  ai_needs_review?: boolean | null;
  ai_judge_mode?: string | null;
  judge_portrait_used: boolean; // 恒 false
  reviewer_id?: string | null;
  reviewed_score?: number | null;
  score_locked: boolean;
  element_value?: number | null;
}

export type AttemptProcessingStatus = "received" | "asr_completed" | "completed" | "technical_failure";

export interface AttemptEvent {
  id: number;
  session_id: string;
  item_id: string;
  turn_seq: number;
  response_role: string;
  attempt_seq: number;
  raw_audio_id: string;
  prompt_level: number;
  cue_type?: string | null;
  duration_seconds?: number | null;
  asr_text?: string | null;
  asr_confidence?: number | null;
  asr_engine_version?: string | null;
  operational_answer_type?: string | null;
  operational_score?: number | null;
  operational_needs_review?: boolean | null;
  judge_mode?: string | null;
  judge_engine_version?: string | null;
  judge_reason?: string | null;
  matched_on?: string | null;
  contains_target?: boolean | null;
  judge_portrait_used: boolean;
  processing_status: AttemptProcessingStatus;
  error_code?: string | null;
  created_at: string;
  processed_at?: string | null;
  is_simulation: boolean;
}

export interface InteractionEvent {
  id: number;
  session_id: string;
  event_seq: number;
  item_id?: string | null;
  turn_seq?: number | null;
  attempt_id?: number | null;
  attempt_seq?: number | null;
  event_type: string;
  payload_json: string;
  created_at: string;
  is_simulation: boolean;
}

export interface AttemptProcessRequest {
  item_id: string;
  turn_seq: number;
  response_role: string;
  raw_audio_id: string;
  prompt_level: number;
  cue_type?: string | null;
  duration_seconds?: number | null;
}

export interface AttemptProcessResult {
  status: AttemptProcessingStatus;
  idempotent: boolean;
  truth_scope: "operational_only";
  attempt: AttemptEvent;
  interactions: InteractionEvent[];
}

export type InteractionAppendRequest =
  | { event_type: "cue_selected"; item_id: string; turn_seq: number; attempt_id?: number; prompt_level: number; cue_type?: string | null }
  | { event_type: "feedback_selected"; item_id: string; turn_seq: number; attempt_id?: number; feedback_key: string };

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
  // 新版 journal 可直接携带采集时的稳定 turn key；旧版后端缺省。
  turn_key?: string | null;
  session_id?: string | null;
  is_simulation: boolean;
  data_classification?: "research" | "simulation" | "legacy_unknown";
  audio_format: string;
  status: AudioStatus;
  is_reliability_sample: boolean;
  withdrawn: boolean;
  withdrawal_status?: string | null;
  checksum?: string | null;
  byte_count?: number | null;
  uploaded_at?: string | null;
  contains_direct_identifier: boolean;
  export_batch_id?: string | null;
  delete_gate_passed?: boolean;
  exported_at?: string | null;
}

export interface AudioCaptureReceipt {
  server_seq: number;
  raw_audio_id: string;
  session_id: string;
  turn_key: string;
  received_at: string;
  duration_seconds: number;
  byte_count: number;
  checksum: string;
  data_classification: "research" | "simulation";
  is_simulation: boolean;
  contains_direct_identifier: boolean;
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

// 正式结局评估与周训练、legacy ScaleResult 是三条独立数据线。
// 这些类型只表示服务端权威投影；浏览器不能提交总分、定义摘要或研究资格声明。
export type AssessmentTimepoint = "pretest" | "posttest" | "followup";
export type AssessmentCategoryKey =
  | "untrained_standardized_naming"
  | "functional_communication";
export type AssessmentEventStatus =
  | "due" | "in_progress" | "awaiting_closeout" | "closed" | "cancelled";
export type AssessmentInstanceStatus =
  | "due" | "in_progress" | "completed" | "approved_deferred";
export type AssessmentDeferralReason =
  | "participant_unavailable"
  | "clinical_or_safety"
  | "technical_failure"
  | "authorized_reschedule";
export type AssessmentCancellationReason =
  | "schedule_changed"
  | "participant_unavailable"
  | "protocol_correction"
  | "duplicate_event";
export type AssessmentCloseoutReportStatus =
  | "no_additional_observation"
  | "observation_recorded";

export interface AssessmentScoringEvidence {
  evidence_id: string;
  instance_id: string;
  event_id: string;
  patient_id: string;
  category_key: AssessmentCategoryKey;
  definition_digest: string;
  item_response_set_digest: string;
  scoring_algorithm_id: string;
  scoring_algorithm_version: string;
  scoring_algorithm_digest: string;
  score: number;
  result: Record<string, unknown>;
  result_digest: string;
  answered_item_count: number;
  missing_item_count: number;
  stopped_early: boolean;
  stopping_reason_code: string | null;
  scored_at: string;
  formal_outcome_eligible: boolean;
}

export interface AssessmentDeferralSummary {
  deferral_id: string;
  instance_id: string;
  event_id: string;
  patient_id: string;
  category_key: AssessmentCategoryKey;
  definition_digest: string;
  reason_code: string;
  deferred_until: string;
  approved_by: string;
  approved_role: "admin" | "local_m0";
  approved_at: string;
}

export interface AssessmentCancellationSummary {
  reason_code: AssessmentCancellationReason;
  cancelled_by: string;
  cancelled_at: string;
  switch_allowed: true;
}

export interface AssessmentCloseoutSummary {
  closeout_id: string;
  event_id: string;
  patient_id: string;
  event_revision: number;
  report_status: AssessmentCloseoutReportStatus;
  fatigue_observed: boolean;
  distress_or_discomfort_observed: boolean;
  participant_declined_to_continue: boolean;
  staff_assistance_occurred: boolean;
  environment_interruption_occurred: boolean;
  device_or_network_interruption_occurred: boolean;
  note: string | null;
  closed_by: string;
  closed_at: string;
  switch_allowed: true;
}

export interface AssessmentInstance {
  instance_id: string;
  event_id: string;
  patient_id: string;
  category_key: AssessmentCategoryKey;
  definition_bundle_id: string;
  definition_bundle_digest: string;
  definition_id: string;
  instrument_id: string;
  instrument_version: string;
  definition_digest: string;
  item_set_digest: string;
  administration_protocol_digest: string;
  response_schema_digest: string;
  result_schema_digest: string;
  missingness_rule_digest: string;
  stopping_rule_digest: string;
  scoring_algorithm_id: string;
  scoring_algorithm_version: string;
  scoring_algorithm_digest: string;
  score_min: number;
  score_max: number;
  score_direction: "higher_is_better" | "lower_is_better";
  score_rounding_rule: string;
  automatic_scoring_permitted: boolean;
  item_response_storage_permitted: boolean;
  result_storage_permitted: boolean;
  result_export_permitted: boolean;
  status: AssessmentInstanceStatus;
  revision: number;
  item_response_count: number;
  required_item_count: number;
  data_classification: "research" | "simulation";
  formal_outcome_eligible: boolean;
  scoring_evidence: AssessmentScoringEvidence | null;
  deferral: AssessmentDeferralSummary | null;
  created_at: string;
  completed_at: string | null;
  updated_at: string;
}

export interface AssessmentEvent {
  schema_version: "formal-assessment.v1";
  event_id: string;
  patient_id: string;
  assigned_assessor_id: string;
  timepoint: AssessmentTimepoint;
  scheduled_date: string;
  status: AssessmentEventStatus;
  revision: number;
  is_simulation: boolean;
  data_classification: "research" | "simulation";
  formal_outcome_eligible: boolean;
  definition_bundle_id: string;
  definition_bundle_digest: string;
  instances: AssessmentInstance[];
  closeout: AssessmentCloseoutSummary | null;
  cancellation: AssessmentCancellationSummary | null;
  created_at: string;
  updated_at: string;
}

export interface AssessmentEventsToday {
  as_of_date: string;
  events: AssessmentEvent[];
}

export interface AssessmentEventCreateRequest {
  timepoint: AssessmentTimepoint;
  scheduled_date: string;
  idempotency_key: string;
}

export interface AssessmentStartRequest {
  expected_event_revision: number;
  idempotency_key: string;
}

export interface AssessmentCancelRequest extends AssessmentStartRequest {
  reason_code: AssessmentCancellationReason;
}

export interface AssessmentResponseValue {
  value?: unknown;
  authorized_artifact_digest?: string | null;
}

export interface AssessmentItemResponseRequest {
  // 具体作答形状由已冻结 response_schema_digest 在服务端验证。
  response: AssessmentResponseValue;
  expected_event_revision: number;
  expected_instance_revision: number;
  expected_item_revision: number;
  idempotency_key: string;
}

export interface AssessmentInstanceMutationRequest {
  expected_event_revision: number;
  expected_instance_revision: number;
  idempotency_key: string;
}

export interface AssessmentDeferralRequest extends AssessmentInstanceMutationRequest {
  reason_code: AssessmentDeferralReason;
  deferred_until: string;
}

export interface AssessmentCloseRequest {
  expected_event_revision: number;
  idempotency_key: string;
  report_status: AssessmentCloseoutReportStatus;
  fatigue_observed: boolean;
  distress_or_discomfort_observed: boolean;
  participant_declined_to_continue: boolean;
  staff_assistance_occurred: boolean;
  environment_interruption_occurred: boolean;
  device_or_network_interruption_occurred: boolean;
  note: string | null;
}

export interface ScaleProtocolReadiness {
  schema_version: "scale-protocol-readiness.v4";
  status:
    | "awaiting_pi_definition"
    | "awaiting_workflow_policy"
    | "awaiting_definition_artifacts"
    | "awaiting_platform_implementation"
    | "ready_for_research";
  definition_bundle_id: string | null;
  definition_bundle_digest: string | null;
  definition_ready: boolean;
  definition_artifact_enforcement_ready: boolean;
  definition_artifacts_ready: boolean;
  formal_result_contract_ready: boolean;
  workflow_policy_ready: boolean;
  workflow_contract_ready: boolean;
  workflow_policy_enforcement_ready: boolean;
  workflow_ready: boolean;
  ready_for_research: boolean;
  instance_creation_enabled: boolean;
  automatic_scoring_enabled: boolean;
  training_metrics_are_formal_scale_results: boolean;
  categories: {
    category_key: string;
    label: string;
    required: boolean;
    definition_id: string | null;
    instrument_id: string | null;
    instrument_name: string | null;
    instrument_version: string | null;
    definition_digest: string | null;
    language: string | null;
    form: string | null;
    license_source: string | null;
    license_status: "authorized" | "pending" | "denied" | "expired" | null;
    digital_presentation_permitted: boolean | null;
    spoken_administration_permitted: boolean | null;
    automatic_scoring_permitted: boolean | null;
    item_response_storage_permitted: boolean | null;
    result_storage_permitted: boolean | null;
    result_export_permitted: boolean | null;
    item_set_digest: string | null;
    administration_protocol_digest: string | null;
    response_schema_digest: string | null;
    result_schema_digest: string | null;
    missingness_rule_digest: string | null;
    stopping_rule_digest: string | null;
    scoring_algorithm_id: string | null;
    scoring_algorithm_version: string | null;
    scoring_algorithm_digest: string | null;
    score_min: number | null;
    score_max: number | null;
    score_direction: "higher_is_better" | "lower_is_better" | null;
    score_rounding_rule: string | null;
    respondent_role: string | null;
    assessor_role: string | null;
    assessor_qualification: string | null;
    pretest_time_window: string | null;
    posttest_time_window: string | null;
    followup_time_window: string | null;
    pi_approval: ScaleProtocolApprovalFact | null;
    clinical_approval: ScaleProtocolApprovalFact | null;
    statistics_approval: ScaleProtocolApprovalFact | null;
    copyright_approval: ScaleProtocolApprovalFact | null;
    scoring_ready: boolean;
  }[];
  workflow_policy: ScaleProtocolWorkflowPolicy;
  blocking_issues: {
    code: string;
    category_key: string;
    field: string;
    message: string;
  }[];
}

export interface ScaleProtocolApprovalFact {
  approved_by: string;
  approved_at: string;
  scope_digest: string;
}

export interface ScaleProtocolWorkflowPolicy {
  workflow_policy_id: string | null;
  workflow_policy_version: string | null;
  workflow_policy_digest: string | null;
  pretest_schedule_rule_digest: string | null;
  posttest_schedule_rule_digest: string | null;
  followup_schedule_rule_digest: string | null;
  deferral_authority_rule_digest: string | null;
  reschedule_rule_digest: string | null;
  closeout_rule_digest: string | null;
  assessor_assignment_rule_digest: string | null;
  pi_approval: ScaleProtocolApprovalFact | null;
  clinical_approval: ScaleProtocolApprovalFact | null;
  statistics_approval: ScaleProtocolApprovalFact | null;
}
// 量表录入允许的相位(前/后测+随访,非训练相位)
export const SCALE_PHASES: PhaseType[] = ["前测", "后测", "随访"];

// GET /content/item-bank
export interface ItemBankInfo {
  version_id: string;
  qc_status?: "draft" | "reviewed" | "frozen" | string;
  ready_for_research?: boolean;
  // 是否所有开放回答环节均已有冻结、可机判的 operational rubric。
  // 缺省必须按未就绪处理，不能把“题库可加载”误写成“自动驾驶安全可用”。
  operational_autopilot_ready?: boolean;
  // Full server-built plan scan used by the automatic runtime admission gate.
  operational_position_count?: number;
  unsupported_operational_position_count?: number;
  unsupported_operational_positions?: string[];
  source_protocol_position_count?: number;
  source_unstructured_position_count?: number;
  delivery_unsupported_position_count?: number;
  source_unstructured_positions?: {
    source_position_key: string;
    response_role: string;
    source_paragraphs: [number, number] | number[];
    status: "awaiting_content_decision" | "awaiting_pi_rubric" | string;
  }[];
  unsupported_operational_position_counts_by_code?: Record<string, number>;
  unsupported_operational_position_gaps?: {
    item_id: string;
    turn_seq: number;
    response_role: string;
    code: string;
    detail: string;
  }[];
  source_document_sha256?: string | null;
  source_normalized_text_sha256?: string | null;
  draft_revision?: string | null;
  // Smaller content-QC subset: open-answer rubrics only. Never use this field
  // by itself to claim that the complete automatic protocol is executable.
  unsupported_operational_rubrics?: string[];
  multi_count?: number;
  // 新后端会显式声明已完成结构化、可用于开发运行的正式训练周；是否可入组另看质控状态。
  // 老后端缺此字段时，前端仅兼容既有第 2 周能力，绝不猜测 3–8 周可用。
  supported_training_weeks?: number[];
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
  status: "published";
  deidentified: boolean;
  files: string[];
  artifacts: Array<{
    realm: "research_analysis" | "research_controlled_audio" | "simulation_analysis" | "simulation_controlled_audio";
    kind: "csv" | "controlled_audio" | "manifest";
    relative_path: string;
    sha256: string;
    byte_count: number;
  }>;
  audio_touched: string[];
  excluded_items: string[];
  sheet_counts: Record<string, number>;
}
