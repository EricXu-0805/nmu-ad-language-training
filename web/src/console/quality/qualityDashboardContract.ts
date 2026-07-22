export const AI_QUALITY_DASHBOARD_SCHEMA_VERSION = "ai-quality-dashboard.v2" as const;

export type QualityDataClassification = "research" | "simulation";
export type QualityVisibilityScope = "owner_sessions" | "terminal_sessions" | "all_sessions";
export type QualitySuppressionStatus = "released" | "not_applicable" | "suppressed";
export type QualitySuppressionReason =
  | "research_threshold_unconfigured"
  | "research_threshold_invalid"
  | "research_small_cell"
  | "research_release_not_frozen";
export type QualityDiagnosticsStatus = "complete" | "partial" | "suppressed";

export interface QualityDashboardPrivacyBoundary {
  aggregation_only: true;
  contains_patient_identifiers: false;
  contains_audio: false;
  contains_transcripts: false;
}

/** v2 首版只发布 overall 行；除数据分区外，所有自由文本维度必须为 null。 */
export interface QualityDashboardDimensions {
  data_classification: QualityDataClassification;
  week_no: null;
  phase_type: null;
  task_type: null;
  content_group: null;
  provider_id: null;
  device_profile: null;
  protocol_version: null;
  asr_engine_version: null;
  judge_engine_version: null;
}

/** AI 运行事实，绝不能充当人工研究真值。 */
export interface QualityDashboardOperationalMetrics {
  eligible_turns: number | null;
  ai_attempted_turns: number | null;
  ai_judged_turns: number | null;
  asr_reviewed_turns: number | null;
  asr_corrected_turns: number | null;
  total_attempts: number | null;
  prompt_level_0_count: number | null;
  prompt_level_1_count: number | null;
  prompt_level_2_count: number | null;
  prompt_level_3_count: number | null;
  technical_failure_attempts: number | null;
  technical_pause_count: number | null;
  researcher_takeover_count: number | null;
  latency_sample_count: number | null;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
}

/** 五项只能全 null 或全为已知计数。 */
export interface QualityDashboardResearchTruthMetrics {
  reviewed_decisions: number | null;
  true_positive: number | null;
  true_negative: number | null;
  false_positive: number | null;
  false_negative: number | null;
}

export interface QualityDashboardSuppression {
  status: QualitySuppressionStatus;
  reason: QualitySuppressionReason | null;
  minimum_distinct_subjects: number | null;
  distinct_subjects: number | null;
}

export interface QualityDashboardCoverage {
  visible_sessions: number | null;
  included_sessions: number | null;
  source_turns: number | null;
  audio_evidenced_turns: number | null;
  attempts_observed: number | null;
  prompt_level_known_attempts: number | null;
  processing_status_known_attempts: number | null;
  latency_known_attempts: number | null;
  ai_attempt_status_known_turns: number | null;
  ai_judgement_status_known_turns: number | null;
  asr_review_status_known_turns: number | null;
  human_truth_locked_turns: number | null;
  binary_eligible_reviewed_decisions: number | null;
  binary_excluded_decisions: number | null;
}

export interface QualityDashboardDiagnosticReasonCounts {
  restricted_or_withdrawn_sessions: number | null;
  classification_inconsistent_sessions: number | null;
  protocol_binding_invalid_sessions: number | null;
  structural_invalid_evidence_records: number | null;
  lineage_invalid_turns: number | null;
  audio_evidence_unavailable_turns: number | null;
  ai_attempt_status_unknown_turns: number | null;
  ai_judgement_status_unknown_turns: number | null;
  asr_review_status_unknown_turns: number | null;
  human_truth_unavailable_turns: number | null;
  binary_prediction_unavailable_turns: number | null;
  latency_unavailable_attempts: number | null;
}

export interface QualityDashboardDiagnostics {
  status: QualityDiagnosticsStatus;
  reason_counts: QualityDashboardDiagnosticReasonCounts;
}

export interface QualityDashboardGroup {
  visibility_scope: QualityVisibilityScope;
  dimensions: QualityDashboardDimensions;
  suppression: QualityDashboardSuppression;
  coverage: QualityDashboardCoverage;
  diagnostics: QualityDashboardDiagnostics;
  operational: QualityDashboardOperationalMetrics;
  research_truth: QualityDashboardResearchTruthMetrics;
}

export interface AIQualityDashboardContract {
  schema_version: typeof AI_QUALITY_DASHBOARD_SCHEMA_VERSION;
  generated_at: string;
  privacy: QualityDashboardPrivacyBoundary;
  rows: QualityDashboardGroup[];
}

type UnknownRecord = Record<string, unknown>;

const TOP_LEVEL_KEYS = new Set(["schema_version", "generated_at", "privacy", "rows"]);
const PRIVACY_KEYS = new Set([
  "aggregation_only",
  "contains_patient_identifiers",
  "contains_audio",
  "contains_transcripts",
]);
const DIMENSION_KEYS = new Set([
  "data_classification",
  "week_no",
  "phase_type",
  "task_type",
  "content_group",
  "provider_id",
  "device_profile",
  "protocol_version",
  "asr_engine_version",
  "judge_engine_version",
]);
const NULL_DIMENSION_KEYS = [
  "week_no",
  "phase_type",
  "task_type",
  "content_group",
  "provider_id",
  "device_profile",
  "protocol_version",
  "asr_engine_version",
  "judge_engine_version",
] as const;
const OPERATIONAL_KEYS = new Set([
  "eligible_turns",
  "ai_attempted_turns",
  "ai_judged_turns",
  "asr_reviewed_turns",
  "asr_corrected_turns",
  "total_attempts",
  "prompt_level_0_count",
  "prompt_level_1_count",
  "prompt_level_2_count",
  "prompt_level_3_count",
  "technical_failure_attempts",
  "technical_pause_count",
  "researcher_takeover_count",
  "latency_sample_count",
  "latency_p50_ms",
  "latency_p95_ms",
]);
const RESEARCH_TRUTH_KEYS = new Set([
  "reviewed_decisions",
  "true_positive",
  "true_negative",
  "false_positive",
  "false_negative",
]);
const SUPPRESSION_KEYS = new Set([
  "status",
  "reason",
  "minimum_distinct_subjects",
  "distinct_subjects",
]);
const COVERAGE_KEYS = new Set([
  "visible_sessions",
  "included_sessions",
  "source_turns",
  "audio_evidenced_turns",
  "attempts_observed",
  "prompt_level_known_attempts",
  "processing_status_known_attempts",
  "latency_known_attempts",
  "ai_attempt_status_known_turns",
  "ai_judgement_status_known_turns",
  "asr_review_status_known_turns",
  "human_truth_locked_turns",
  "binary_eligible_reviewed_decisions",
  "binary_excluded_decisions",
]);
const DIAGNOSTICS_KEYS = new Set(["status", "reason_counts"]);
const DIAGNOSTIC_REASON_KEYS = new Set([
  "restricted_or_withdrawn_sessions",
  "classification_inconsistent_sessions",
  "protocol_binding_invalid_sessions",
  "structural_invalid_evidence_records",
  "lineage_invalid_turns",
  "audio_evidence_unavailable_turns",
  "ai_attempt_status_unknown_turns",
  "ai_judgement_status_unknown_turns",
  "asr_review_status_unknown_turns",
  "human_truth_unavailable_turns",
  "binary_prediction_unavailable_turns",
  "latency_unavailable_attempts",
]);
const GROUP_KEYS = new Set([
  "visibility_scope",
  "dimensions",
  "suppression",
  "coverage",
  "diagnostics",
  "operational",
  "research_truth",
]);
const ISO_TIMESTAMP = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|([+-])(\d{2}):(\d{2}))$/;

function asRecord(value: unknown, path: string): UnknownRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${path} 必须是对象`);
  }
  return value as UnknownRecord;
}

function requireExactKeys(row: UnknownRecord, allowed: ReadonlySet<string>, path: string): void {
  const keys = Object.keys(row);
  if (keys.some((key) => !allowed.has(key)) || [...allowed].some((key) => !Object.hasOwn(row, key))) {
    throw new Error(`${path} 字段集合不符合 v2 聚合隐私契约，已拒绝显示`);
  }
}

function isRealTimezoneAwareTimestamp(value: string): boolean {
  const match = ISO_TIMESTAMP.exec(value);
  if (!match) return false;
  const [, yearText, monthText, dayText, hourText, minuteText, secondText, , , offsetHourText, offsetMinuteText] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  const wallClock = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
  const calendarRoundTrips = wallClock.getUTCFullYear() === year
    && wallClock.getUTCMonth() === month - 1
    && wallClock.getUTCDate() === day
    && wallClock.getUTCHours() === hour
    && wallClock.getUTCMinutes() === minute
    && wallClock.getUTCSeconds() === second;
  if (!calendarRoundTrips) return false;
  if (offsetHourText !== undefined && offsetMinuteText !== undefined) {
    const offsetHour = Number(offsetHourText);
    const offsetMinute = Number(offsetMinuteText);
    if (offsetHour > 14 || offsetMinute > 59 || (offsetHour === 14 && offsetMinute !== 0)) return false;
  }
  return !Number.isNaN(Date.parse(value));
}

function requiredLiteralBoolean<T extends boolean>(
  row: UnknownRecord,
  key: string,
  expected: T,
  path: string,
): T {
  if (row[key] !== expected) throw new Error(`${path}.${key} 必须为 ${String(expected)}`);
  return expected;
}

function nullableNonnegativeInteger(row: UnknownRecord, key: string, path: string): number | null {
  const value = row[key];
  if (value === null) return null;
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new Error(`${path}.${key} 必须是非负安全整数或 null`);
  }
  return value as number;
}

function requiredEnum<T extends string>(
  row: UnknownRecord,
  key: string,
  values: ReadonlySet<string>,
  path: string,
): T {
  const value = row[key];
  if (typeof value !== "string" || !values.has(value)) throw new Error(`${path}.${key} 不是允许的固定状态码`);
  return value as T;
}

function allNull(record: object): boolean {
  return Object.values(record).every((value) => value === null);
}

function allKnown(record: object): boolean {
  return Object.values(record).every((value) => value !== null);
}

function parsePrivacy(value: unknown): QualityDashboardPrivacyBoundary {
  const row = asRecord(value, "privacy");
  requireExactKeys(row, PRIVACY_KEYS, "privacy");
  return {
    aggregation_only: requiredLiteralBoolean(row, "aggregation_only", true, "privacy"),
    contains_patient_identifiers: requiredLiteralBoolean(row, "contains_patient_identifiers", false, "privacy"),
    contains_audio: requiredLiteralBoolean(row, "contains_audio", false, "privacy"),
    contains_transcripts: requiredLiteralBoolean(row, "contains_transcripts", false, "privacy"),
  };
}

function parseDimensions(value: unknown, path: string): QualityDashboardDimensions {
  const row = asRecord(value, path);
  requireExactKeys(row, DIMENSION_KEYS, path);
  const classification = row.data_classification;
  if (classification !== "research" && classification !== "simulation") {
    throw new Error(`${path}.data_classification 必须明确为 research 或 simulation`);
  }
  for (const key of NULL_DIMENSION_KEYS) {
    if (row[key] !== null) throw new Error(`${path}.${key} 在 v2 overall 首版必须为 null`);
  }
  return {
    data_classification: classification,
    week_no: null,
    phase_type: null,
    task_type: null,
    content_group: null,
    provider_id: null,
    device_profile: null,
    protocol_version: null,
    asr_engine_version: null,
    judge_engine_version: null,
  };
}

function parseOperational(value: unknown, path: string): QualityDashboardOperationalMetrics {
  const row = asRecord(value, path);
  requireExactKeys(row, OPERATIONAL_KEYS, path);
  const result: QualityDashboardOperationalMetrics = {
    eligible_turns: nullableNonnegativeInteger(row, "eligible_turns", path),
    ai_attempted_turns: nullableNonnegativeInteger(row, "ai_attempted_turns", path),
    ai_judged_turns: nullableNonnegativeInteger(row, "ai_judged_turns", path),
    asr_reviewed_turns: nullableNonnegativeInteger(row, "asr_reviewed_turns", path),
    asr_corrected_turns: nullableNonnegativeInteger(row, "asr_corrected_turns", path),
    total_attempts: nullableNonnegativeInteger(row, "total_attempts", path),
    prompt_level_0_count: nullableNonnegativeInteger(row, "prompt_level_0_count", path),
    prompt_level_1_count: nullableNonnegativeInteger(row, "prompt_level_1_count", path),
    prompt_level_2_count: nullableNonnegativeInteger(row, "prompt_level_2_count", path),
    prompt_level_3_count: nullableNonnegativeInteger(row, "prompt_level_3_count", path),
    technical_failure_attempts: nullableNonnegativeInteger(row, "technical_failure_attempts", path),
    technical_pause_count: nullableNonnegativeInteger(row, "technical_pause_count", path),
    researcher_takeover_count: nullableNonnegativeInteger(row, "researcher_takeover_count", path),
    latency_sample_count: nullableNonnegativeInteger(row, "latency_sample_count", path),
    latency_p50_ms: nullableNonnegativeInteger(row, "latency_p50_ms", path),
    latency_p95_ms: nullableNonnegativeInteger(row, "latency_p95_ms", path),
  };
  if (result.prompt_level_3_count !== null) {
    throw new Error(`${path}.prompt_level_3_count 必须为 null；旧驱动未持久化告知答案呈现回执`);
  }
  const publishedPromptCounts = [
    result.prompt_level_0_count,
    result.prompt_level_1_count,
    result.prompt_level_2_count,
  ];
  if (publishedPromptCounts.some((count) => count === null)
      && !publishedPromptCounts.every((count) => count === null)) {
    throw new Error(`${path} 的 0..2 级提示尝试计数必须同时已知或同时未知`);
  }
  if (publishedPromptCounts.every((count) => count !== null)
      && result.total_attempts !== null
      && publishedPromptCounts.reduce((sum, count) => sum + (count ?? 0), 0)
        > result.total_attempts) {
    throw new Error(`${path} 的 0..2 级提示尝试之和不能大于总尝试数`);
  }
  if (result.eligible_turns !== null && result.ai_attempted_turns !== null
      && result.ai_attempted_turns > result.eligible_turns) {
    throw new Error(`${path} 的 AI 尝试数不能大于可评估轮次数`);
  }
  if (result.ai_attempted_turns !== null && result.ai_judged_turns !== null
      && result.ai_judged_turns > result.ai_attempted_turns) {
    throw new Error(`${path} 的 AI 完成判定数不能大于 AI 尝试轮次数`);
  }
  if (result.eligible_turns !== null && result.ai_judged_turns !== null
      && result.ai_judged_turns > result.eligible_turns) {
    throw new Error(`${path} 的 AI 完成判定数不能大于可评估轮次数`);
  }
  if (result.asr_reviewed_turns !== null && result.asr_corrected_turns !== null
      && result.asr_corrected_turns > result.asr_reviewed_turns) {
    throw new Error(`${path} 的 ASR 修正数不能大于人工复核数`);
  }
  if (result.total_attempts !== null && result.technical_failure_attempts !== null
      && result.technical_failure_attempts > result.total_attempts) {
    throw new Error(`${path} 的技术失败数不能大于总尝试数`);
  }
  const hasLatency = result.latency_p50_ms !== null || result.latency_p95_ms !== null;
  if (result.latency_sample_count !== null && result.latency_sample_count > 0
      && !hasLatency) {
    throw new Error(`${path} 的正延迟样本数必须同时提供 P50/P95`);
  }
  if (hasLatency && (result.latency_p50_ms === null || result.latency_p95_ms === null
      || result.latency_sample_count === null || result.latency_sample_count === 0)) {
    throw new Error(`${path} 的 P50/P95 与正延迟样本数必须同时提供`);
  }
  if (result.latency_p50_ms !== null && result.latency_p95_ms !== null
      && result.latency_p50_ms > result.latency_p95_ms) {
    throw new Error(`${path} 的 P50 延迟不能大于 P95`);
  }
  return result;
}

function parseResearchTruth(value: unknown, path: string): QualityDashboardResearchTruthMetrics {
  const row = asRecord(value, path);
  requireExactKeys(row, RESEARCH_TRUTH_KEYS, path);
  const result: QualityDashboardResearchTruthMetrics = {
    reviewed_decisions: nullableNonnegativeInteger(row, "reviewed_decisions", path),
    true_positive: nullableNonnegativeInteger(row, "true_positive", path),
    true_negative: nullableNonnegativeInteger(row, "true_negative", path),
    false_positive: nullableNonnegativeInteger(row, "false_positive", path),
    false_negative: nullableNonnegativeInteger(row, "false_negative", path),
  };
  const values = [
    result.reviewed_decisions,
    result.true_positive,
    result.true_negative,
    result.false_positive,
    result.false_negative,
  ];
  if (values.every((count) => count === null)) return result;
  if (values.some((count) => count === null)) {
    throw new Error(`${path} 的人工真值五项必须全部为 null 或全部为已知计数`);
  }
  const [reviewedDecisions, ...cells] = values as [number, number, number, number, number];
  if (cells.reduce((sum, count) => sum + count, 0) !== reviewedDecisions) {
    throw new Error(`${path} 的混淆矩阵之和必须等于人工复核判定数`);
  }
  return result;
}

function parseSuppression(
  value: unknown,
  path: string,
  classification: QualityDataClassification,
): QualityDashboardSuppression {
  const row = asRecord(value, path);
  requireExactKeys(row, SUPPRESSION_KEYS, path);
  const status = requiredEnum<QualitySuppressionStatus>(
    row, "status", new Set(["released", "not_applicable", "suppressed"]), path,
  );
  const rawReason = row.reason;
  if (rawReason !== null && rawReason !== "research_threshold_unconfigured"
      && rawReason !== "research_threshold_invalid" && rawReason !== "research_small_cell"
      && rawReason !== "research_release_not_frozen") {
    throw new Error(`${path}.reason 不是允许的固定原因码`);
  }
  const reason = rawReason as QualitySuppressionReason | null;
  const minimum = nullableNonnegativeInteger(row, "minimum_distinct_subjects", path);
  const distinct = nullableNonnegativeInteger(row, "distinct_subjects", path);
  if (status === "not_applicable") {
    if (classification !== "simulation" || reason !== null || minimum !== null || distinct !== null) {
      throw new Error(`${path} 的 not_applicable 只允许用于无阈值字段的模拟分区`);
    }
  } else if (status === "released") {
    if (classification !== "research" || reason !== null || minimum === null || minimum < 2 || minimum > 100
        || distinct === null || distinct < minimum) {
      throw new Error(`${path} 的 released 必须证明达到已配置的真实研究受试者阈值`);
    }
  } else if (classification !== "research" || reason === null || distinct !== null) {
    throw new Error(`${path} 的 suppressed 必须是隐藏受试者数的真实研究固定原因`);
  } else if (reason === "research_small_cell" && (minimum === null || minimum < 2 || minimum > 100)) {
    throw new Error(`${path} 的小单元抑制必须公开 2..100 阈值但隐藏实际人数`);
  } else if (reason !== "research_small_cell" && minimum !== null) {
    throw new Error(`${path} 的无效或未配置阈值不得伪造最小人数`);
  }
  return {
    status,
    reason,
    minimum_distinct_subjects: minimum,
    distinct_subjects: distinct,
  };
}

function parseCoverage(value: unknown, path: string): QualityDashboardCoverage {
  const row = asRecord(value, path);
  requireExactKeys(row, COVERAGE_KEYS, path);
  const result: QualityDashboardCoverage = {
    visible_sessions: nullableNonnegativeInteger(row, "visible_sessions", path),
    included_sessions: nullableNonnegativeInteger(row, "included_sessions", path),
    source_turns: nullableNonnegativeInteger(row, "source_turns", path),
    audio_evidenced_turns: nullableNonnegativeInteger(row, "audio_evidenced_turns", path),
    attempts_observed: nullableNonnegativeInteger(row, "attempts_observed", path),
    prompt_level_known_attempts: nullableNonnegativeInteger(row, "prompt_level_known_attempts", path),
    processing_status_known_attempts: nullableNonnegativeInteger(row, "processing_status_known_attempts", path),
    latency_known_attempts: nullableNonnegativeInteger(row, "latency_known_attempts", path),
    ai_attempt_status_known_turns: nullableNonnegativeInteger(row, "ai_attempt_status_known_turns", path),
    ai_judgement_status_known_turns: nullableNonnegativeInteger(row, "ai_judgement_status_known_turns", path),
    asr_review_status_known_turns: nullableNonnegativeInteger(row, "asr_review_status_known_turns", path),
    human_truth_locked_turns: nullableNonnegativeInteger(row, "human_truth_locked_turns", path),
    binary_eligible_reviewed_decisions: nullableNonnegativeInteger(row, "binary_eligible_reviewed_decisions", path),
    binary_excluded_decisions: nullableNonnegativeInteger(row, "binary_excluded_decisions", path),
  };
  if (result.visible_sessions !== null && result.included_sessions !== null
      && result.included_sessions > result.visible_sessions) {
    throw new Error(`${path} 的纳入场次不能大于可见场次`);
  }
  for (const key of [
    "audio_evidenced_turns",
    "ai_attempt_status_known_turns",
    "ai_judgement_status_known_turns",
    "asr_review_status_known_turns",
    "human_truth_locked_turns",
  ] as const) {
    if (result.source_turns !== null && result[key] !== null && result[key] > result.source_turns) {
      throw new Error(`${path}.${key} 不能大于源轮次数`);
    }
  }
  for (const key of [
    "prompt_level_known_attempts",
    "processing_status_known_attempts",
    "latency_known_attempts",
  ] as const) {
    if (result.attempts_observed !== null && result[key] !== null && result[key] > result.attempts_observed) {
      throw new Error(`${path}.${key} 不能大于已观察尝试数`);
    }
  }
  if (result.human_truth_locked_turns !== null && result.binary_eligible_reviewed_decisions !== null
      && result.binary_eligible_reviewed_decisions > result.human_truth_locked_turns) {
    throw new Error(`${path} 的二分类可核查数不能大于人工锁定轮次`);
  }
  if (result.human_truth_locked_turns !== null
      && result.binary_eligible_reviewed_decisions !== null
      && result.binary_excluded_decisions !== null
      && result.binary_eligible_reviewed_decisions + result.binary_excluded_decisions
        !== result.human_truth_locked_turns) {
    throw new Error(`${path} 的二分类可核查与排除判定之和必须等于人工锁定轮次`);
  }
  return result;
}

function parseDiagnostics(
  value: unknown,
  path: string,
  suppressionStatus: QualitySuppressionStatus,
): QualityDashboardDiagnostics {
  const row = asRecord(value, path);
  requireExactKeys(row, DIAGNOSTICS_KEYS, path);
  const status = requiredEnum<QualityDiagnosticsStatus>(
    row, "status", new Set(["complete", "partial", "suppressed"]), path,
  );
  const reasonsRow = asRecord(row.reason_counts, `${path}.reason_counts`);
  requireExactKeys(reasonsRow, DIAGNOSTIC_REASON_KEYS, `${path}.reason_counts`);
  const reasonCounts: QualityDashboardDiagnosticReasonCounts = {
    restricted_or_withdrawn_sessions: nullableNonnegativeInteger(reasonsRow, "restricted_or_withdrawn_sessions", path),
    classification_inconsistent_sessions: nullableNonnegativeInteger(reasonsRow, "classification_inconsistent_sessions", path),
    protocol_binding_invalid_sessions: nullableNonnegativeInteger(reasonsRow, "protocol_binding_invalid_sessions", path),
    structural_invalid_evidence_records: nullableNonnegativeInteger(reasonsRow, "structural_invalid_evidence_records", path),
    lineage_invalid_turns: nullableNonnegativeInteger(reasonsRow, "lineage_invalid_turns", path),
    audio_evidence_unavailable_turns: nullableNonnegativeInteger(reasonsRow, "audio_evidence_unavailable_turns", path),
    ai_attempt_status_unknown_turns: nullableNonnegativeInteger(reasonsRow, "ai_attempt_status_unknown_turns", path),
    ai_judgement_status_unknown_turns: nullableNonnegativeInteger(reasonsRow, "ai_judgement_status_unknown_turns", path),
    asr_review_status_unknown_turns: nullableNonnegativeInteger(reasonsRow, "asr_review_status_unknown_turns", path),
    human_truth_unavailable_turns: nullableNonnegativeInteger(reasonsRow, "human_truth_unavailable_turns", path),
    binary_prediction_unavailable_turns: nullableNonnegativeInteger(reasonsRow, "binary_prediction_unavailable_turns", path),
    latency_unavailable_attempts: nullableNonnegativeInteger(reasonsRow, "latency_unavailable_attempts", path),
  };
  if (suppressionStatus === "suppressed") {
    if (status !== "suppressed" || !allNull(reasonCounts)) {
      throw new Error(`${path} 在隐私抑制时必须隐藏全部诊断计数`);
    }
  } else {
    if (status === "suppressed" || !allKnown(reasonCounts)) {
      throw new Error(`${path} 未抑制时必须提供完整固定原因计数`);
    }
    const totalReasons = Object.values(reasonCounts).reduce<number>((sum, count) => sum + (count ?? 0), 0);
    if ((status === "complete") !== (totalReasons === 0)) {
      throw new Error(`${path}.status 必须与固定原因计数是否为零一致`);
    }
  }
  return { status, reason_counts: reasonCounts };
}

function parseGroup(value: unknown, index: number): QualityDashboardGroup {
  const path = `rows[${index}]`;
  const row = asRecord(value, path);
  requireExactKeys(row, GROUP_KEYS, path);
  const visibilityScope = requiredEnum<QualityVisibilityScope>(
    row,
    "visibility_scope",
    new Set(["owner_sessions", "terminal_sessions", "all_sessions"]),
    path,
  );
  const dimensions = parseDimensions(row.dimensions, `${path}.dimensions`);
  const suppression = parseSuppression(row.suppression, `${path}.suppression`, dimensions.data_classification);
  const coverage = parseCoverage(row.coverage, `${path}.coverage`);
  const diagnostics = parseDiagnostics(row.diagnostics, `${path}.diagnostics`, suppression.status);
  const operational = parseOperational(row.operational, `${path}.operational`);
  const researchTruth = parseResearchTruth(row.research_truth, `${path}.research_truth`);
  if (suppression.status === "suppressed") {
    if (!allNull(coverage) || !allNull(operational) || !allNull(researchTruth)) {
      throw new Error(`${path} 被隐私抑制时不得发布覆盖、运行或研究真值计数`);
    }
  } else {
    if (!allKnown(coverage)) throw new Error(`${path} 未抑制时必须发布完整覆盖计数`);
    if (coverage.attempts_observed !== null && operational.total_attempts !== null
        && coverage.attempts_observed !== operational.total_attempts) {
      throw new Error(`${path} 的已观察尝试数必须等于运行总尝试数`);
    }
    if (coverage.latency_known_attempts !== null && operational.latency_sample_count !== null
        && coverage.latency_known_attempts !== operational.latency_sample_count) {
      throw new Error(`${path} 的延迟可计算尝试数必须等于 AI 处理延迟样本数`);
    }
    if (coverage.source_turns !== null && operational.eligible_turns !== null
        && coverage.source_turns !== operational.eligible_turns) {
      throw new Error(`${path} 的可评估轮次数必须等于冻结源轮次数`);
    }
    for (const [metric, knownCoverage, label] of [
      [operational.ai_attempted_turns, coverage.ai_attempt_status_known_turns, "AI 尝试轮次"],
      [operational.ai_judged_turns, coverage.ai_judgement_status_known_turns, "AI 判定轮次"],
      [operational.asr_reviewed_turns, coverage.asr_review_status_known_turns, "ASR 复核轮次"],
    ] as const) {
      if (metric !== null && knownCoverage !== null && metric > knownCoverage) {
        throw new Error(`${path} 的${label}不能大于对应状态可判定覆盖数`);
      }
    }
    if (operational.ai_attempted_turns !== null && coverage.audio_evidenced_turns !== null
        && operational.ai_attempted_turns > coverage.audio_evidenced_turns) {
      throw new Error(`${path} 的 AI 尝试轮次不能大于录音证据完整轮次数`);
    }
    const publishedPromptCounts = [
      operational.prompt_level_0_count,
      operational.prompt_level_1_count,
      operational.prompt_level_2_count,
    ];
    if (publishedPromptCounts.every((count) => count !== null)
        && coverage.prompt_level_known_attempts !== null
        && publishedPromptCounts.reduce((sum, count) => sum + (count ?? 0), 0)
          !== coverage.prompt_level_known_attempts) {
      throw new Error(`${path} 的 0..2 级提示尝试之和必须等于提示层级已知尝试数`);
    }
    if (researchTruth.reviewed_decisions !== null
        && researchTruth.reviewed_decisions !== coverage.binary_eligible_reviewed_decisions) {
      throw new Error(`${path} 的人工真值总数必须等于二分类可核查判定数`);
    }
  }
  return {
    visibility_scope: visibilityScope,
    dimensions,
    suppression,
    coverage,
    diagnostics,
    operational,
    research_truth: researchTruth,
  };
}

export function parseAIQualityDashboard(
  value: unknown,
  expectedClassification?: QualityDataClassification,
): AIQualityDashboardContract {
  const row = asRecord(value, "AI 质量汇总");
  requireExactKeys(row, TOP_LEVEL_KEYS, "AI 质量汇总");
  if (row.schema_version !== AI_QUALITY_DASHBOARD_SCHEMA_VERSION) {
    throw new Error(`AI 质量汇总 schema_version 必须是 ${AI_QUALITY_DASHBOARD_SCHEMA_VERSION}`);
  }
  if (typeof row.generated_at !== "string" || row.generated_at.length > 64
      || !isRealTimezoneAwareTimestamp(row.generated_at)) {
    throw new Error("AI 质量汇总 generated_at 必须是真实存在且带时区的 ISO 时间");
  }
  if (!Array.isArray(row.rows) || row.rows.length > 1) {
    throw new Error("AI 质量汇总 rows 必须是最多一个 overall 行的数组");
  }
  const rows = row.rows.map(parseGroup);
  if (expectedClassification !== undefined && rows.length !== 1) {
    throw new Error("当前数据分区必须返回一个 released 或 suppressed overall 行");
  }
  if (expectedClassification !== undefined
      && rows.some((group) => group.dimensions.data_classification !== expectedClassification)) {
    throw new Error("AI 质量汇总返回了其他数据分区，已拒绝显示");
  }
  return {
    schema_version: AI_QUALITY_DASHBOARD_SCHEMA_VERSION,
    generated_at: row.generated_at,
    privacy: parsePrivacy(row.privacy),
    rows,
  };
}
