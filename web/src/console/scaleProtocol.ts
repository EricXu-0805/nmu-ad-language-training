import type {
  ScaleProtocolApprovalFact,
  ScaleProtocolReadiness,
  ScaleProtocolWorkflowPolicy,
} from "../types";

const SCHEMA_VERSION = "scale-protocol-readiness.v4";
const SHA256 = /^sha256:[0-9a-f]{64}$/;
const REQUIRED_CATEGORY_KEYS = [
  "untrained_standardized_naming",
  "functional_communication",
] as const;

const TOP_LEVEL_KEYS = [
  "schema_version", "status", "definition_bundle_id", "definition_bundle_digest",
  "definition_ready", "definition_artifact_enforcement_ready",
  "definition_artifacts_ready", "formal_result_contract_ready",
  "workflow_policy_ready", "workflow_contract_ready",
  "workflow_policy_enforcement_ready", "workflow_ready",
  "ready_for_research", "instance_creation_enabled", "automatic_scoring_enabled",
  "training_metrics_are_formal_scale_results", "categories", "workflow_policy",
  "blocking_issues",
] as const;
const CATEGORY_KEYS = [
  "category_key", "label", "required", "definition_id", "instrument_id",
  "instrument_name", "instrument_version", "definition_digest", "language", "form",
  "license_source", "license_status", "digital_presentation_permitted",
  "spoken_administration_permitted", "automatic_scoring_permitted",
  "item_response_storage_permitted", "result_storage_permitted",
  "result_export_permitted", "item_set_digest", "administration_protocol_digest",
  "response_schema_digest", "result_schema_digest", "missingness_rule_digest",
  "stopping_rule_digest", "scoring_algorithm_id", "scoring_algorithm_version",
  "scoring_algorithm_digest", "score_min", "score_max", "score_direction",
  "score_rounding_rule", "respondent_role", "assessor_role", "assessor_qualification",
  "pretest_time_window", "posttest_time_window", "followup_time_window", "pi_approval",
  "clinical_approval", "statistics_approval", "copyright_approval", "scoring_ready",
] as const;
const WORKFLOW_POLICY_KEYS = [
  "workflow_policy_id", "workflow_policy_version", "workflow_policy_digest",
  "pretest_schedule_rule_digest", "posttest_schedule_rule_digest",
  "followup_schedule_rule_digest", "deferral_authority_rule_digest",
  "reschedule_rule_digest", "closeout_rule_digest", "assessor_assignment_rule_digest",
  "pi_approval", "clinical_approval", "statistics_approval",
] as const;
const APPROVAL_KEYS = ["approved_by", "approved_at", "scope_digest"] as const;
const BLOCKER_KEYS = ["code", "category_key", "field", "message"] as const;

const CATEGORY_TEXT_FIELDS = [
  "definition_id", "instrument_id", "instrument_name", "instrument_version", "language",
  "form", "license_source", "scoring_algorithm_id", "scoring_algorithm_version",
  "score_rounding_rule", "respondent_role", "assessor_role", "assessor_qualification",
  "pretest_time_window", "posttest_time_window", "followup_time_window",
] as const;
const CATEGORY_DIGEST_FIELDS = [
  "definition_digest", "item_set_digest", "administration_protocol_digest",
  "response_schema_digest", "result_schema_digest", "missingness_rule_digest",
  "stopping_rule_digest", "scoring_algorithm_digest",
] as const;
const CATEGORY_PERMISSION_FIELDS = [
  "digital_presentation_permitted", "spoken_administration_permitted",
  "automatic_scoring_permitted", "item_response_storage_permitted",
  "result_storage_permitted", "result_export_permitted",
] as const;
const CATEGORY_APPROVAL_FIELDS = [
  "pi_approval", "clinical_approval", "statistics_approval", "copyright_approval",
] as const;
const WORKFLOW_TEXT_FIELDS = ["workflow_policy_id", "workflow_policy_version"] as const;
const WORKFLOW_DIGEST_FIELDS = [
  "workflow_policy_digest", "pretest_schedule_rule_digest", "posttest_schedule_rule_digest",
  "followup_schedule_rule_digest", "deferral_authority_rule_digest",
  "reschedule_rule_digest", "closeout_rule_digest", "assessor_assignment_rule_digest",
] as const;
const WORKFLOW_APPROVAL_FIELDS = [
  "pi_approval", "clinical_approval", "statistics_approval",
] as const;

const DEFINITION_ARTIFACT_BLOCKER = "platform.definition_artifacts.not_ready";
const DEFINITION_ARTIFACT_ENFORCEMENT_BLOCKER =
  "platform.definition_artifact_enforcement.not_ready";
const FORMAL_RESULT_CONTRACT_BLOCKER = "platform.formal_result_contract.not_ready";
const WORKFLOW_CONTRACT_BLOCKER = "platform.workflow_contract.not_ready";
const WORKFLOW_POLICY_ENFORCEMENT_BLOCKER =
  "platform.workflow_policy_enforcement.not_ready";

type UnknownRecord = Record<string, unknown>;
type ScaleCategory = ScaleProtocolReadiness["categories"][number];

function exactRecord(value: unknown, keys: readonly string[], label: string): UnknownRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label}不是对象`);
  }
  const row = value as UnknownRecord;
  const actual = Object.keys(row).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length
      || actual.some((key, index) => key !== expected[index])) {
    throw new Error(`${label}字段不完整或包含未知字段`);
  }
  return row;
}

function stringValue(row: UnknownRecord, key: string, label = "量表协议"): string {
  const value = row[key];
  if (typeof value !== "string" || !value.trim() || value !== value.trim()) {
    throw new Error(`${label}.${key} 缺失或非规范字符串`);
  }
  return value;
}

function booleanValue(row: UnknownRecord, key: string, label = "量表协议"): boolean {
  if (typeof row[key] !== "boolean") throw new Error(`${label}.${key} 必须是布尔值`);
  return row[key] as boolean;
}

function nullableStringValue(row: UnknownRecord, key: string, label: string): string | null {
  return row[key] === null ? null : stringValue(row, key, label);
}

function nullableDigestValue(row: UnknownRecord, key: string, label: string): string | null {
  const value = nullableStringValue(row, key, label);
  if (value !== null && !SHA256.test(value)) throw new Error(`${label}.${key} 必须是 sha256 摘要`);
  return value;
}

function nullableBooleanValue(row: UnknownRecord, key: string, label: string): boolean | null {
  if (row[key] === null || typeof row[key] === "boolean") return row[key] as boolean | null;
  throw new Error(`${label}.${key} 必须是布尔值或 null`);
}

function nullableFiniteNumberValue(row: UnknownRecord, key: string, label: string): number | null {
  if (row[key] === null) return null;
  if (typeof row[key] !== "number" || !Number.isFinite(row[key])) {
    throw new Error(`${label}.${key} 必须是有限数值或 null`);
  }
  return row[key] as number;
}

function nullableApprovalFact(
  row: UnknownRecord,
  key: string,
  label: string,
): ScaleProtocolApprovalFact | null {
  if (row[key] === null) return null;
  const fact = exactRecord(row[key], APPROVAL_KEYS, `${label}.${key}`);
  const approvedBy = stringValue(fact, "approved_by", `${label}.${key}`);
  const approvedAt = stringValue(fact, "approved_at", `${label}.${key}`);
  const scopeDigest = stringValue(fact, "scope_digest", `${label}.${key}`);
  if (!SHA256.test(scopeDigest)) throw new Error(`${label}.${key}.scope_digest 必须是 sha256 摘要`);
  if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(approvedAt)
      || !Number.isFinite(Date.parse(approvedAt))) {
    throw new Error(`${label}.${key}.approved_at 必须是带时区的时间`);
  }
  return { approved_by: approvedBy, approved_at: approvedAt, scope_digest: scopeDigest };
}

function nullableLicenseStatus(row: UnknownRecord, label: string): ScaleCategory["license_status"] {
  if (row.license_status === null) return null;
  if (["authorized", "pending", "denied", "expired"].includes(String(row.license_status))) {
    return row.license_status as NonNullable<ScaleCategory["license_status"]>;
  }
  throw new Error(`${label}.license_status 非法`);
}

function nullableScoreDirection(row: UnknownRecord, label: string): ScaleCategory["score_direction"] {
  if (row.score_direction === null) return null;
  if (row.score_direction === "higher_is_better" || row.score_direction === "lower_is_better") {
    return row.score_direction;
  }
  throw new Error(`${label}.score_direction 非法`);
}

function categoryBlockingFields(category: ScaleCategory): string[] {
  const blocked: string[] = [];
  for (const field of CATEGORY_TEXT_FIELDS) if (category[field] === null) blocked.push(field);
  for (const field of CATEGORY_DIGEST_FIELDS) if (category[field] === null) blocked.push(field);
  for (const field of CATEGORY_PERMISSION_FIELDS) if (category[field] !== true) blocked.push(field);
  if (category.license_status !== "authorized") blocked.push("license_status");
  if (category.score_min === null) blocked.push("score_min");
  if (category.score_max === null) blocked.push("score_max");
  if (category.score_min !== null && category.score_max !== null
      && category.score_min >= category.score_max) blocked.push("score_range");
  if (category.score_direction === null) blocked.push("score_direction");
  for (const field of CATEGORY_APPROVAL_FIELDS) {
    const fact = category[field];
    if (fact === null || fact.scope_digest !== category.definition_digest) blocked.push(field);
  }
  return blocked;
}

function parseCategory(value: unknown, index: number): ScaleCategory {
  const label = `categories[${index}]`;
  const row = exactRecord(value, CATEGORY_KEYS, label);
  const category: ScaleCategory = {
    category_key: stringValue(row, "category_key", label),
    label: stringValue(row, "label", label),
    required: booleanValue(row, "required", label),
    definition_id: nullableStringValue(row, "definition_id", label),
    instrument_id: nullableStringValue(row, "instrument_id", label),
    instrument_name: nullableStringValue(row, "instrument_name", label),
    instrument_version: nullableStringValue(row, "instrument_version", label),
    definition_digest: nullableDigestValue(row, "definition_digest", label),
    language: nullableStringValue(row, "language", label),
    form: nullableStringValue(row, "form", label),
    license_source: nullableStringValue(row, "license_source", label),
    license_status: nullableLicenseStatus(row, label),
    digital_presentation_permitted: nullableBooleanValue(row, "digital_presentation_permitted", label),
    spoken_administration_permitted: nullableBooleanValue(row, "spoken_administration_permitted", label),
    automatic_scoring_permitted: nullableBooleanValue(row, "automatic_scoring_permitted", label),
    item_response_storage_permitted: nullableBooleanValue(row, "item_response_storage_permitted", label),
    result_storage_permitted: nullableBooleanValue(row, "result_storage_permitted", label),
    result_export_permitted: nullableBooleanValue(row, "result_export_permitted", label),
    item_set_digest: nullableDigestValue(row, "item_set_digest", label),
    administration_protocol_digest: nullableDigestValue(row, "administration_protocol_digest", label),
    response_schema_digest: nullableDigestValue(row, "response_schema_digest", label),
    result_schema_digest: nullableDigestValue(row, "result_schema_digest", label),
    missingness_rule_digest: nullableDigestValue(row, "missingness_rule_digest", label),
    stopping_rule_digest: nullableDigestValue(row, "stopping_rule_digest", label),
    scoring_algorithm_id: nullableStringValue(row, "scoring_algorithm_id", label),
    scoring_algorithm_version: nullableStringValue(row, "scoring_algorithm_version", label),
    scoring_algorithm_digest: nullableDigestValue(row, "scoring_algorithm_digest", label),
    score_min: nullableFiniteNumberValue(row, "score_min", label),
    score_max: nullableFiniteNumberValue(row, "score_max", label),
    score_direction: nullableScoreDirection(row, label),
    score_rounding_rule: nullableStringValue(row, "score_rounding_rule", label),
    respondent_role: nullableStringValue(row, "respondent_role", label),
    assessor_role: nullableStringValue(row, "assessor_role", label),
    assessor_qualification: nullableStringValue(row, "assessor_qualification", label),
    pretest_time_window: nullableStringValue(row, "pretest_time_window", label),
    posttest_time_window: nullableStringValue(row, "posttest_time_window", label),
    followup_time_window: nullableStringValue(row, "followup_time_window", label),
    pi_approval: nullableApprovalFact(row, "pi_approval", label),
    clinical_approval: nullableApprovalFact(row, "clinical_approval", label),
    statistics_approval: nullableApprovalFact(row, "statistics_approval", label),
    copyright_approval: nullableApprovalFact(row, "copyright_approval", label),
    scoring_ready: booleanValue(row, "scoring_ready", label),
  };
  if (category.scoring_ready !== (categoryBlockingFields(category).length === 0)) {
    throw new Error(`量表类别 ${category.category_key} 的必需事实与计分状态矛盾`);
  }
  return category;
}

function parseWorkflowPolicy(value: unknown): {
  policy: ScaleProtocolWorkflowPolicy;
  blockedFields: string[];
} {
  const label = "workflow_policy";
  const row = exactRecord(value, WORKFLOW_POLICY_KEYS, label);
  const policy: ScaleProtocolWorkflowPolicy = {
    workflow_policy_id: nullableStringValue(row, "workflow_policy_id", label),
    workflow_policy_version: nullableStringValue(row, "workflow_policy_version", label),
    workflow_policy_digest: nullableDigestValue(row, "workflow_policy_digest", label),
    pretest_schedule_rule_digest: nullableDigestValue(row, "pretest_schedule_rule_digest", label),
    posttest_schedule_rule_digest: nullableDigestValue(row, "posttest_schedule_rule_digest", label),
    followup_schedule_rule_digest: nullableDigestValue(row, "followup_schedule_rule_digest", label),
    deferral_authority_rule_digest: nullableDigestValue(row, "deferral_authority_rule_digest", label),
    reschedule_rule_digest: nullableDigestValue(row, "reschedule_rule_digest", label),
    closeout_rule_digest: nullableDigestValue(row, "closeout_rule_digest", label),
    assessor_assignment_rule_digest: nullableDigestValue(row, "assessor_assignment_rule_digest", label),
    pi_approval: nullableApprovalFact(row, "pi_approval", label),
    clinical_approval: nullableApprovalFact(row, "clinical_approval", label),
    statistics_approval: nullableApprovalFact(row, "statistics_approval", label),
  };
  const blockedFields: string[] = [];
  for (const field of WORKFLOW_TEXT_FIELDS) if (policy[field] === null) blockedFields.push(field);
  for (const field of WORKFLOW_DIGEST_FIELDS) if (policy[field] === null) blockedFields.push(field);
  const prerequisiteReady = [...WORKFLOW_TEXT_FIELDS, ...WORKFLOW_DIGEST_FIELDS]
    .filter((field) => field !== "workflow_policy_digest")
    .every((field) => policy[field as keyof ScaleProtocolWorkflowPolicy] !== null);
  if (prerequisiteReady && policy.workflow_policy_digest !== workflowPolicyDigest(policy)
      && !blockedFields.includes("workflow_policy_digest")) {
    blockedFields.push("workflow_policy_digest");
  }
  for (const field of WORKFLOW_APPROVAL_FIELDS) {
    const fact = policy[field];
    if (fact === null || fact.scope_digest !== policy.workflow_policy_digest) blockedFields.push(field);
  }
  return { policy, blockedFields };
}

function rightRotate(value: number, bits: number): number {
  return (value >>> bits) | (value << (32 - bits));
}

const SHA256_CONSTANTS = [
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
  0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
  0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
  0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
  0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
] as const;

function sha256Digest(text: string): string {
  const source = new TextEncoder().encode(text);
  const paddedLength = Math.ceil((source.length + 9) / 64) * 64;
  const bytes = new Uint8Array(paddedLength);
  bytes.set(source);
  bytes[source.length] = 0x80;
  let bitLength = source.length * 8;
  for (let index = 0; index < 8; index += 1) {
    bytes[paddedLength - 1 - index] = bitLength & 0xff;
    bitLength = Math.floor(bitLength / 256);
  }
  const state = [
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ];
  const schedule = new Uint32Array(64);
  const view = new DataView(bytes.buffer);
  for (let offset = 0; offset < bytes.length; offset += 64) {
    for (let index = 0; index < 16; index += 1) {
      schedule[index] = view.getUint32(offset + index * 4, false);
    }
    for (let index = 16; index < 64; index += 1) {
      const a = schedule[index - 15];
      const b = schedule[index - 2];
      const s0 = rightRotate(a, 7) ^ rightRotate(a, 18) ^ (a >>> 3);
      const s1 = rightRotate(b, 17) ^ rightRotate(b, 19) ^ (b >>> 10);
      schedule[index] = (schedule[index - 16] + s0 + schedule[index - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = state;
    for (let index = 0; index < 64; index += 1) {
      const sigma1 = rightRotate(e, 6) ^ rightRotate(e, 11) ^ rightRotate(e, 25);
      const choose = (e & f) ^ (~e & g);
      const temp1 = (h + sigma1 + choose + SHA256_CONSTANTS[index] + schedule[index]) >>> 0;
      const sigma0 = rightRotate(a, 2) ^ rightRotate(a, 13) ^ rightRotate(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (sigma0 + majority) >>> 0;
      h = g; g = f; f = e; e = (d + temp1) >>> 0;
      d = c; c = b; b = a; a = (temp1 + temp2) >>> 0;
    }
    state[0] = (state[0] + a) >>> 0;
    state[1] = (state[1] + b) >>> 0;
    state[2] = (state[2] + c) >>> 0;
    state[3] = (state[3] + d) >>> 0;
    state[4] = (state[4] + e) >>> 0;
    state[5] = (state[5] + f) >>> 0;
    state[6] = (state[6] + g) >>> 0;
    state[7] = (state[7] + h) >>> 0;
  }
  return `sha256:${state.map((value) => value.toString(16).padStart(8, "0")).join("")}`;
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const row = value as UnknownRecord;
  return `{${Object.keys(row).sort().map((key) =>
    `${JSON.stringify(key)}:${canonicalJson(row[key])}`).join(",")}}`;
}

// Python's JSON encoder preserves the float nature of AssessmentDefinitionSpec
// score bounds (for example 0.0) and switches to scientific notation at the
// repr(float) thresholds. JSON.parse erases int/float identity in JavaScript,
// so the two definition-owned score fields are rendered explicitly here.
function pythonFloatJson(value: number): string {
  if (!Number.isFinite(value)) throw new Error("定义分数范围必须是有限浮点数");
  if (Object.is(value, -0)) return "-0.0";
  if (value === 0) return "0.0";
  const exponent = Math.floor(Math.log10(Math.abs(value)));
  if (exponent < -4 || exponent >= 16) {
    const [mantissa, rawExponent] = value.toExponential().split("e");
    const numericExponent = Number(rawExponent);
    const sign = numericExponent >= 0 ? "+" : "-";
    return `${mantissa}e${sign}${String(Math.abs(numericExponent)).padStart(2, "0")}`;
  }
  const plain = String(value);
  return Number.isInteger(value) ? `${plain}.0` : plain;
}

export function workflowPolicyDigest(policy: ScaleProtocolWorkflowPolicy): string {
  return sha256Digest(canonicalJson({
    schema: "formal-assessment-workflow-policy.v1",
    workflow_policy_id: policy.workflow_policy_id,
    workflow_policy_version: policy.workflow_policy_version,
    pretest_schedule_rule_digest: policy.pretest_schedule_rule_digest,
    posttest_schedule_rule_digest: policy.posttest_schedule_rule_digest,
    followup_schedule_rule_digest: policy.followup_schedule_rule_digest,
    deferral_authority_rule_digest: policy.deferral_authority_rule_digest,
    reschedule_rule_digest: policy.reschedule_rule_digest,
    closeout_rule_digest: policy.closeout_rule_digest,
    assessor_assignment_rule_digest: policy.assessor_assignment_rule_digest,
  }));
}

function runtimeDefinitionDigest(category: ScaleCategory): string {
  const projection: UnknownRecord = {
    schema: "assessment-definition.v1",
    definition_id: category.definition_id,
    category_key: category.category_key,
    instrument_id: category.instrument_id,
    instrument_version: category.instrument_version,
    item_set_digest: category.item_set_digest,
    administration_protocol_digest: category.administration_protocol_digest,
    response_schema_digest: category.response_schema_digest,
    result_schema_digest: category.result_schema_digest,
    missingness_rule_digest: category.missingness_rule_digest,
    stopping_rule_digest: category.stopping_rule_digest,
    scoring_algorithm_id: category.scoring_algorithm_id,
    scoring_algorithm_version: category.scoring_algorithm_version,
    scoring_algorithm_digest: category.scoring_algorithm_digest,
    score_min: category.score_min,
    score_max: category.score_max,
    score_direction: category.score_direction,
    score_rounding_rule: category.score_rounding_rule,
    automatic_scoring_permitted: category.automatic_scoring_permitted,
    item_response_storage_permitted: category.item_response_storage_permitted,
    result_storage_permitted: category.result_storage_permitted,
    result_export_permitted: category.result_export_permitted,
  };
  const encoded = `{${Object.keys(projection).sort().map((key) => {
    const value = projection[key];
    const rendered = key === "score_min" || key === "score_max"
      ? pythonFloatJson(value as number)
      : canonicalJson(value);
    return `${JSON.stringify(key)}:${rendered}`;
  }).join(",")}}`;
  return sha256Digest(encoded);
}

function runtimeBundleDigest(bundleId: string, categories: ScaleCategory[]): string {
  return sha256Digest(canonicalJson({
    schema: "assessment-definition-bundle.v1",
    bundle_id: bundleId,
    definitions: [...categories]
      .sort((left, right) => left.category_key.localeCompare(right.category_key))
      .map((category) => ({
        category_key: category.category_key,
        definition_id: category.definition_id,
        definition_digest: category.definition_digest,
      })),
    formal_research_approved: true,
  }));
}

export function parseScaleProtocolReadiness(value: unknown): ScaleProtocolReadiness {
  const row = exactRecord(value, TOP_LEVEL_KEYS, "量表协议就绪状态");
  if (row.schema_version !== SCHEMA_VERSION) throw new Error("量表协议清单版本不受支持");
  const status = stringValue(row, "status");
  const definitionBundleId = nullableStringValue(row, "definition_bundle_id", "量表协议");
  const definitionBundleDigest = nullableDigestValue(row, "definition_bundle_digest", "量表协议");
  const definitionReady = booleanValue(row, "definition_ready");
  const definitionArtifactEnforcementReady = booleanValue(
    row, "definition_artifact_enforcement_ready",
  );
  const definitionArtifactsReady = booleanValue(row, "definition_artifacts_ready");
  const formalResultContractReady = booleanValue(row, "formal_result_contract_ready");
  const workflowPolicyReady = booleanValue(row, "workflow_policy_ready");
  const workflowContractReady = booleanValue(row, "workflow_contract_ready");
  const workflowPolicyEnforcementReady = booleanValue(
    row, "workflow_policy_enforcement_ready",
  );
  const workflowReady = booleanValue(row, "workflow_ready");
  const readyForResearch = booleanValue(row, "ready_for_research");
  const instanceCreationEnabled = booleanValue(row, "instance_creation_enabled");
  const automaticScoringEnabled = booleanValue(row, "automatic_scoring_enabled");
  const trainingMetricsAreFormalScaleResults = booleanValue(
    row, "training_metrics_are_formal_scale_results",
  );
  if (trainingMetricsAreFormalScaleResults) throw new Error("训练指标不得被声明为正式量表结果");

  if (!Array.isArray(row.categories)) throw new Error("categories 必须是数组");
  const categories = row.categories.map(parseCategory);
  const categoryKeys = categories.map((category) => category.category_key);
  if (categoryKeys.length !== REQUIRED_CATEGORY_KEYS.length
      || new Set(categoryKeys).size !== REQUIRED_CATEGORY_KEYS.length
      || REQUIRED_CATEGORY_KEYS.some((key) => !categoryKeys.includes(key))) {
    throw new Error("量表协议必须且只能包含两个结局类别");
  }
  if (categories.some((category) => !category.required)) {
    throw new Error("两个结局类别都必须显式标记为必需");
  }
  const derivedDefinitionReady = definitionBundleId !== null
    && definitionBundleDigest !== null
    && categories.every((category) => category.scoring_ready);
  if (definitionReady !== derivedDefinitionReady) {
    throw new Error("量表定义就绪状态与定义包/分类事实矛盾");
  }

  const { policy: workflowPolicy, blockedFields: workflowBlockedFields } =
    parseWorkflowPolicy(row.workflow_policy);
  const derivedWorkflowPolicyReady = workflowBlockedFields.length === 0;
  if (workflowPolicyReady !== derivedWorkflowPolicyReady) {
    throw new Error("工作流政策就绪状态与冻结事实矛盾");
  }
  if (workflowReady !== (
    workflowPolicyReady && workflowContractReady && workflowPolicyEnforcementReady
  )) {
    throw new Error("工作流总状态必须同时由政策、平台合同与可信执行事实构成");
  }
  if (definitionArtifactsReady && (
    !definitionReady || !definitionArtifactEnforcementReady
  )) {
    throw new Error("定义或服务器可信制品执行未就绪时不得声明运行时定义制品就绪");
  }
  if (definitionArtifactsReady) {
    for (const category of categories) {
      if (category.definition_digest !== runtimeDefinitionDigest(category)) {
        throw new Error(`量表类别 ${category.category_key} 的定义摘要与冻结运行时快照不一致`);
      }
    }
    if (definitionBundleId === null || definitionBundleDigest === null
        || definitionBundleDigest !== runtimeBundleDigest(definitionBundleId, categories)) {
      throw new Error("定义包摘要与两个冻结定义不一致");
    }
  }
  const derivedReady = definitionReady && definitionArtifactEnforcementReady
    && definitionArtifactsReady && formalResultContractReady && workflowReady;
  if (readyForResearch !== derivedReady
      || instanceCreationEnabled !== derivedReady
      || automaticScoringEnabled !== derivedReady) {
    throw new Error("总体就绪、实例创建或自动计分旗标与四层事实矛盾");
  }
  const expectedStatus: ScaleProtocolReadiness["status"] = !definitionReady
    ? "awaiting_pi_definition"
    : !workflowPolicyReady
      ? "awaiting_workflow_policy"
      : !formalResultContractReady || !workflowContractReady
          || !definitionArtifactEnforcementReady || !workflowPolicyEnforcementReady
        ? "awaiting_platform_implementation"
        : !definitionArtifactsReady
          ? "awaiting_definition_artifacts"
          : "ready_for_research";
  if (status !== expectedStatus) throw new Error("量表协议状态与当前就绪层级矛盾");

  if (!Array.isArray(row.blocking_issues)) throw new Error("blocking_issues 必须是数组");
  const blockingIssues = row.blocking_issues.map((value, index) => {
    const issue = exactRecord(value, BLOCKER_KEYS, `blocking_issues[${index}]`);
    return {
      code: stringValue(issue, "code", `blocking_issues[${index}]`),
      category_key: stringValue(issue, "category_key", `blocking_issues[${index}]`),
      field: stringValue(issue, "field", `blocking_issues[${index}]`),
      message: stringValue(issue, "message", `blocking_issues[${index}]`),
    };
  });
  const expectedBlockers = new Map<string, { categoryKey: string; field: string }>();
  for (const category of categories) {
    for (const field of categoryBlockingFields(category)) {
      expectedBlockers.set(`${category.category_key}.${field}.not_ready`, {
        categoryKey: category.category_key,
        field,
      });
    }
  }
  if (definitionBundleId === null) expectedBlockers.set("platform.definition_bundle_id.not_ready", {
    categoryKey: "platform", field: "definition_bundle_id",
  });
  if (definitionBundleDigest === null) expectedBlockers.set("platform.definition_bundle_digest.not_ready", {
    categoryKey: "platform", field: "definition_bundle_digest",
  });
  for (const field of workflowBlockedFields) {
    expectedBlockers.set(`workflow_policy.${field}.not_ready`, {
      categoryKey: "workflow_policy", field,
    });
  }
  if (!definitionArtifactEnforcementReady) {
    expectedBlockers.set(DEFINITION_ARTIFACT_ENFORCEMENT_BLOCKER, {
      categoryKey: "platform", field: "definition_artifact_enforcement",
    });
  }
  if (!workflowPolicyEnforcementReady) {
    expectedBlockers.set(WORKFLOW_POLICY_ENFORCEMENT_BLOCKER, {
      categoryKey: "platform", field: "workflow_policy_enforcement",
    });
  }
  if (!definitionArtifactsReady) expectedBlockers.set(DEFINITION_ARTIFACT_BLOCKER, {
    categoryKey: "platform", field: "definition_artifacts",
  });
  if (!formalResultContractReady) expectedBlockers.set(FORMAL_RESULT_CONTRACT_BLOCKER, {
    categoryKey: "platform", field: "formal_result_contract",
  });
  if (!workflowContractReady) expectedBlockers.set(WORKFLOW_CONTRACT_BLOCKER, {
    categoryKey: "platform", field: "workflow_contract",
  });
  const actualCodes = new Set(blockingIssues.map((issue) => issue.code));
  if (actualCodes.size !== blockingIssues.length
      || actualCodes.size !== expectedBlockers.size
      || [...expectedBlockers].some(([code, expected]) => {
        const issue = blockingIssues.find((candidate) => candidate.code === code);
        return !issue || issue.category_key !== expected.categoryKey || issue.field !== expected.field;
      })) {
    throw new Error("量表协议阻断项未逐字段对应当前 v4 清单");
  }

  return {
    schema_version: SCHEMA_VERSION,
    status: expectedStatus,
    definition_bundle_id: definitionBundleId,
    definition_bundle_digest: definitionBundleDigest,
    definition_ready: definitionReady,
    definition_artifact_enforcement_ready: definitionArtifactEnforcementReady,
    definition_artifacts_ready: definitionArtifactsReady,
    formal_result_contract_ready: formalResultContractReady,
    workflow_policy_ready: workflowPolicyReady,
    workflow_contract_ready: workflowContractReady,
    workflow_policy_enforcement_ready: workflowPolicyEnforcementReady,
    workflow_ready: workflowReady,
    ready_for_research: readyForResearch,
    instance_creation_enabled: instanceCreationEnabled,
    automatic_scoring_enabled: automaticScoringEnabled,
    training_metrics_are_formal_scale_results: trainingMetricsAreFormalScaleResults,
    categories,
    workflow_policy: workflowPolicy,
    blocking_issues: blockingIssues,
  };
}
