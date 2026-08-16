/**
 * 冻结发布纪元的载荷契约（研究分区）。
 *
 * 它**不是** v2 聚合的一个变体，所以单独一个模块、单独一个 schema_version：
 * v2 那份契约靠"未抑制时必须发布完整覆盖计数"和一串恒等式来保证前后端说的是
 * 同一件事，而冻结发布恰恰把那些精确计数全部拿掉了。把两种形状塞进同一个
 * 解析器，只能靠在恒等式上开一堆 if——那些 if 迟早会被误用到 v2 上。
 *
 * 一条硬规则：**null 一律渲染成「已抑制」，绝不渲染成 0。**
 * 把 false_negative: null 显示成 0，是在说"AI 一次假阴性都没有"，
 * 比不发布严重得多。
 */

export const AI_QUALITY_RELEASE_SCHEMA_VERSION = "ai-quality-release.v1" as const;

export type QualityReleaseSuppressionStatus = "released" | "suppressed";

export interface QualityReleasePrivacyBoundary {
  aggregation_only: true;
  contains_patient_identifiers: false;
  contains_audio: false;
  contains_transcripts: false;
}

/** 比率：向下截断到冻结时定下的小数位。null = 已抑制，不是 0。 */
export interface QualityReleaseOperationalRates {
  asr_manual_correction_rate: number | null;
  prompt_escalation_rate: number | null;
  pause_rate: number | null;
  takeover_rate: number | null;
  latency_p50_band: string | null;
  latency_p95_band: string | null;
}

/** 混淆矩阵的四个原始格永远不出现在这里，只有三个率。 */
export interface QualityReleaseTruthRates {
  agreement_rate: number | null;
  false_positive_rate: number | null;
  false_negative_rate: number | null;
  reviewed_decisions_band: string | null;
}

export interface QualityReleaseProvenance {
  cohort_size_band: string | null;
  session_count_band: string | null;
  registry_version: string;
  cohort_rule_version: string;
}

export interface QualityReleaseRow {
  visibility_scope: "frozen_release_cohort";
  dimensions: { data_classification: "research" } & Record<string, null | string>;
  suppression: {
    status: QualityReleaseSuppressionStatus;
    reason: string | null;
    minimum_distinct_subjects: number | null;
    distinct_subjects: null;
  };
  coverage: Record<string, null>;
  diagnostics: { status: string; reason_counts: null };
  operational: QualityReleaseOperationalRates;
  research_truth: QualityReleaseTruthRates;
  release: QualityReleaseProvenance;
}

export interface AIQualityReleaseContract {
  schema_version: typeof AI_QUALITY_RELEASE_SCHEMA_VERSION;
  generated_at: string;
  privacy: QualityReleasePrivacyBoundary;
  rows: QualityReleaseRow[];
}

type UnknownRecord = Record<string, unknown>;

const TOP_LEVEL_KEYS = new Set(["schema_version", "generated_at", "privacy", "rows"]);
const ROW_KEYS = new Set([
  "visibility_scope",
  "dimensions",
  "suppression",
  "coverage",
  "diagnostics",
  "operational",
  "research_truth",
  "release",
]);
const SUPPRESSION_KEYS = new Set([
  "status",
  "reason",
  "minimum_distinct_subjects",
  "distinct_subjects",
]);
const OPERATIONAL_KEYS = new Set([
  "asr_manual_correction_rate",
  "prompt_escalation_rate",
  "pause_rate",
  "takeover_rate",
  "latency_p50_band",
  "latency_p95_band",
]);
const TRUTH_KEYS = new Set([
  "agreement_rate",
  "false_positive_rate",
  "false_negative_rate",
  "reviewed_decisions_band",
]);
const RELEASE_KEYS = new Set([
  "cohort_size_band",
  "session_count_band",
  "registry_version",
  "cohort_rule_version",
]);
const PRIVACY_KEYS = new Set([
  "aggregation_only",
  "contains_patient_identifiers",
  "contains_audio",
  "contains_transcripts",
]);
/** 档位只有这一种形状。放任自由字符串等于给后端开一个渲染注入口。 */
const BAND = /^(0|[1-9]\d{0,6})-(0|[1-9]\d{0,6})$/;
const ISO_TIMESTAMP =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;

function asRecord(value: unknown, path: string): UnknownRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${path} 必须是对象`);
  }
  return value as UnknownRecord;
}

function requireExactKeys(row: UnknownRecord, allowed: ReadonlySet<string>, path: string): void {
  const keys = Object.keys(row);
  if (keys.some((key) => !allowed.has(key)) || [...allowed].some((key) => !Object.hasOwn(row, key))) {
    throw new Error(`${path} 字段集合不符合冻结发布契约，已拒绝显示`);
  }
}

/** 比率必须在 [0,1] 且小数位不超过 3。多出来的位数本身就是一条泄漏。 */
function nullableRate(row: UnknownRecord, key: string, path: string): number | null {
  const value = row[key];
  if (value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1) {
    throw new Error(`${path}.${key} 必须是 0 到 1 之间的比率或 null`);
  }
  if (Math.abs(value * 1000 - Math.round(value * 1000)) > 1e-9) {
    throw new Error(`${path}.${key} 的小数位超出冻结发布允许的精度`);
  }
  return value;
}

function nullableBand(row: UnknownRecord, key: string, path: string): string | null {
  const value = row[key];
  if (value === null) return null;
  if (typeof value !== "string" || !BAND.test(value)) {
    throw new Error(`${path}.${key} 必须是「下界-上界」形式的档位或 null`);
  }
  const [low, high] = value.split("-").map(Number);
  if (low > high) throw new Error(`${path}.${key} 的档位上下界颠倒`);
  return value;
}

function requiredShortString(row: UnknownRecord, key: string, path: string): string {
  const value = row[key];
  if (typeof value !== "string" || value.length === 0 || value.length > 64) {
    throw new Error(`${path}.${key} 必须是非空短字符串`);
  }
  return value;
}

function allNull(record: object): boolean {
  return Object.values(record).every((value) => value === null);
}

function parsePrivacy(value: unknown): QualityReleasePrivacyBoundary {
  const row = asRecord(value, "冻结发布.privacy");
  requireExactKeys(row, PRIVACY_KEYS, "冻结发布.privacy");
  if (row.aggregation_only !== true || row.contains_patient_identifiers !== false
      || row.contains_audio !== false || row.contains_transcripts !== false) {
    throw new Error("冻结发布 privacy 边界被改动，已拒绝显示");
  }
  return {
    aggregation_only: true,
    contains_patient_identifiers: false,
    contains_audio: false,
    contains_transcripts: false,
  };
}

function parseRow(value: unknown, index: number): QualityReleaseRow {
  const path = `rows[${index}]`;
  const row = asRecord(value, path);
  requireExactKeys(row, ROW_KEYS, path);
  if (row.visibility_scope !== "frozen_release_cohort") {
    throw new Error(`${path}.visibility_scope 必须是冻结队列`);
  }

  const dimensions = asRecord(row.dimensions, `${path}.dimensions`);
  if (dimensions.data_classification !== "research") {
    throw new Error(`${path} 冻结发布只用于研究分区`);
  }
  for (const [key, dimension] of Object.entries(dimensions)) {
    if (key !== "data_classification" && dimension !== null) {
      throw new Error(`${path}.dimensions.${key} 在 v1 只发整体，必须为 null`);
    }
  }

  const suppression = asRecord(row.suppression, `${path}.suppression`);
  requireExactKeys(suppression, SUPPRESSION_KEYS, `${path}.suppression`);
  const status = suppression.status;
  if (status !== "released" && status !== "suppressed") {
    throw new Error(`${path}.suppression.status 只能是 released 或 suppressed`);
  }
  if (suppression.distinct_subjects !== null) {
    throw new Error(`${path} 冻结发布不得携带精确受试者数`);
  }

  const coverage = asRecord(row.coverage, `${path}.coverage`);
  if (!allNull(coverage)) {
    throw new Error(`${path}.coverage 在冻结发布里必须整块为 null`);
  }

  const diagnostics = asRecord(row.diagnostics, `${path}.diagnostics`);
  requireExactKeys(diagnostics, new Set(["status", "reason_counts"]), `${path}.diagnostics`);
  if (diagnostics.reason_counts !== null) {
    // 部分 null 的位图本身就说出了哪几项非零，所以只接受整块 null。
    throw new Error(`${path}.diagnostics.reason_counts 必须整块为 null`);
  }

  const operationalRow = asRecord(row.operational, `${path}.operational`);
  requireExactKeys(operationalRow, OPERATIONAL_KEYS, `${path}.operational`);
  const truthRow = asRecord(row.research_truth, `${path}.research_truth`);
  requireExactKeys(truthRow, TRUTH_KEYS, `${path}.research_truth`);
  const releaseRow = asRecord(row.release, `${path}.release`);
  requireExactKeys(releaseRow, RELEASE_KEYS, `${path}.release`);

  const operational: QualityReleaseOperationalRates = {
    asr_manual_correction_rate: nullableRate(operationalRow, "asr_manual_correction_rate", path),
    prompt_escalation_rate: nullableRate(operationalRow, "prompt_escalation_rate", path),
    pause_rate: nullableRate(operationalRow, "pause_rate", path),
    takeover_rate: nullableRate(operationalRow, "takeover_rate", path),
    latency_p50_band: nullableBand(operationalRow, "latency_p50_band", path),
    latency_p95_band: nullableBand(operationalRow, "latency_p95_band", path),
  };
  const researchTruth: QualityReleaseTruthRates = {
    agreement_rate: nullableRate(truthRow, "agreement_rate", path),
    false_positive_rate: nullableRate(truthRow, "false_positive_rate", path),
    false_negative_rate: nullableRate(truthRow, "false_negative_rate", path),
    reviewed_decisions_band: nullableBand(truthRow, "reviewed_decisions_band", path),
  };
  if (status === "suppressed" && !(allNull(operational) && allNull(researchTruth))) {
    throw new Error(`${path} 声明已抑制却仍带着指标`);
  }
  // 三个率同分母，只发一部分等于把剩下那个用减法送出去。
  const truthRates = [
    researchTruth.agreement_rate,
    researchTruth.false_positive_rate,
    researchTruth.false_negative_rate,
  ];
  if (truthRates.some((rate) => rate === null) && truthRates.some((rate) => rate !== null)) {
    throw new Error(`${path}.research_truth 的三个率必须同时发布或同时抑制`);
  }

  return {
    visibility_scope: "frozen_release_cohort",
    dimensions: dimensions as QualityReleaseRow["dimensions"],
    suppression: {
      status,
      reason: suppression.reason === null ? null : String(suppression.reason).slice(0, 64),
      minimum_distinct_subjects:
        typeof suppression.minimum_distinct_subjects === "number"
          ? suppression.minimum_distinct_subjects
          : null,
      distinct_subjects: null,
    },
    coverage: coverage as Record<string, null>,
    diagnostics: { status: String(diagnostics.status).slice(0, 32), reason_counts: null },
    operational,
    research_truth: researchTruth,
    release: {
      cohort_size_band: nullableBand(releaseRow, "cohort_size_band", `${path}.release`),
      session_count_band: nullableBand(releaseRow, "session_count_band", `${path}.release`),
      registry_version: requiredShortString(releaseRow, "registry_version", `${path}.release`),
      cohort_rule_version: requiredShortString(releaseRow, "cohort_rule_version", `${path}.release`),
    },
  };
}

export function isFrozenReleasePayload(value: unknown): boolean {
  return (
    value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && (value as UnknownRecord).schema_version === AI_QUALITY_RELEASE_SCHEMA_VERSION
  );
}

export function parseAIQualityRelease(value: unknown): AIQualityReleaseContract {
  const row = asRecord(value, "冻结发布");
  requireExactKeys(row, TOP_LEVEL_KEYS, "冻结发布");
  if (row.schema_version !== AI_QUALITY_RELEASE_SCHEMA_VERSION) {
    throw new Error(`冻结发布 schema_version 必须是 ${AI_QUALITY_RELEASE_SCHEMA_VERSION}`);
  }
  if (typeof row.generated_at !== "string" || row.generated_at.length > 64
      || !ISO_TIMESTAMP.test(row.generated_at)) {
    throw new Error("冻结发布 generated_at 必须是带时区的 ISO 时间");
  }
  if (!Array.isArray(row.rows) || row.rows.length !== 1) {
    throw new Error("冻结发布必须正好返回一个 overall 行");
  }
  return {
    schema_version: AI_QUALITY_RELEASE_SCHEMA_VERSION,
    generated_at: row.generated_at,
    privacy: parsePrivacy(row.privacy),
    rows: row.rows.map(parseRow),
  };
}
