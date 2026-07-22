import type {
  AssessmentCategoryKey,
  AssessmentCancellationReason,
  AssessmentCancellationSummary,
  AssessmentCloseoutSummary,
  AssessmentDeferralReason,
  AssessmentDeferralSummary,
  AssessmentEvent,
  AssessmentEventsToday,
  AssessmentEventStatus,
  AssessmentInstance,
  AssessmentInstanceStatus,
  AssessmentScoringEvidence,
  AssessmentTimepoint,
  ScaleProtocolReadiness,
  SessionRuntimeStatus,
} from "../types";

const ASSESSMENT_SCHEMA_VERSION = "formal-assessment.v1";
const SHA256 = /^sha256:[0-9a-f]{64}$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const ZONED_TIME = /(?:Z|[+-]\d{2}:\d{2})$/;

export const ASSESSMENT_CATEGORY_KEYS = [
  "untrained_standardized_naming",
  "functional_communication",
] as const satisfies readonly AssessmentCategoryKey[];

const EVENT_KEYS = [
  "schema_version", "event_id", "patient_id", "assigned_assessor_id", "timepoint",
  "scheduled_date", "status", "revision", "is_simulation", "data_classification",
  "formal_outcome_eligible", "definition_bundle_id", "definition_bundle_digest",
  "instances", "closeout", "cancellation", "created_at", "updated_at",
] as const;
const INSTANCE_KEYS = [
  "instance_id", "event_id", "patient_id", "category_key", "definition_bundle_id",
  "definition_bundle_digest", "definition_id",
  "instrument_id", "instrument_version", "definition_digest", "item_set_digest",
  "administration_protocol_digest", "response_schema_digest", "result_schema_digest",
  "missingness_rule_digest", "stopping_rule_digest", "scoring_algorithm_id",
  "scoring_algorithm_version", "scoring_algorithm_digest", "score_min", "score_max",
  "score_direction", "score_rounding_rule", "automatic_scoring_permitted",
  "item_response_storage_permitted", "result_storage_permitted",
  "result_export_permitted", "status", "revision",
  "item_response_count", "required_item_count", "formal_outcome_eligible",
  "data_classification", "scoring_evidence", "deferral", "created_at", "completed_at",
  "updated_at",
] as const;
const EVIDENCE_KEYS = [
  "evidence_id", "instance_id", "event_id", "patient_id", "category_key",
  "definition_digest", "item_response_set_digest", "scoring_algorithm_id",
  "scoring_algorithm_version", "scoring_algorithm_digest", "score", "result",
  "result_digest", "answered_item_count", "missing_item_count", "stopped_early",
  "stopping_reason_code", "formal_outcome_eligible", "scored_at",
] as const;
const DEFERRAL_KEYS = [
  "deferral_id", "instance_id", "event_id", "patient_id", "category_key",
  "definition_digest", "reason_code", "deferred_until", "approved_by", "approved_role",
  "approved_at",
] as const;
const CLOSEOUT_KEYS = [
  "closeout_id", "event_id", "patient_id", "event_revision", "report_status",
  "fatigue_observed", "distress_or_discomfort_observed",
  "participant_declined_to_continue", "staff_assistance_occurred",
  "environment_interruption_occurred", "device_or_network_interruption_occurred",
  "note", "closed_by", "closed_at", "switch_allowed",
] as const;
const CANCELLATION_KEYS = [
  "reason_code", "cancelled_by", "cancelled_at", "switch_allowed",
] as const;
const TODAY_KEYS = ["as_of_date", "events"] as const;

type UnknownRecord = Record<string, unknown>;

export interface AssessmentBindingExpectation {
  patientId: string;
  eventId?: string;
  instanceId?: string;
  categoryKey?: AssessmentCategoryKey;
  definitionDigest?: string;
  formalOutcomeEligible?: boolean;
}

export interface AssessmentEventExpectation {
  patientId: string;
  eventId?: string;
  timepoint?: AssessmentTimepoint;
  definitionDigests?: Partial<Record<AssessmentCategoryKey, string>>;
}

function exactRecord(
  value: unknown,
  keys: readonly string[],
  label: string,
): UnknownRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label}不是对象`);
  }
  const row = value as UnknownRecord;
  const actual = Object.keys(row).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length
      || actual.some((key, index) => key !== expected[index])) {
    throw new Error(`${label}字段不完整或包含未允许字段`);
  }
  return row;
}

function stringValue(row: UnknownRecord, key: string, label: string): string {
  const value = row[key];
  if (typeof value !== "string" || !value || value !== value.trim()) {
    throw new Error(`${label}.${key}必须是规范非空字符串`);
  }
  return value;
}

function nullableStringValue(row: UnknownRecord, key: string, label: string): string | null {
  if (row[key] === null) return null;
  return stringValue(row, key, label);
}

function booleanValue(row: UnknownRecord, key: string, label: string): boolean {
  const value = row[key];
  if (typeof value !== "boolean") throw new Error(`${label}.${key}必须是布尔值`);
  return value;
}

function integerValue(
  row: UnknownRecord,
  key: string,
  label: string,
  minimum = 0,
): number {
  const value = row[key];
  if (!Number.isInteger(value) || (value as number) < minimum) {
    throw new Error(`${label}.${key}必须是不小于 ${minimum} 的整数`);
  }
  return value as number;
}

function finiteNumberValue(row: UnknownRecord, key: string, label: string): number {
  const value = row[key];
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${label}.${key}必须是有限数值`);
  }
  return value;
}

function digestValue(row: UnknownRecord, key: string, label: string): string {
  const value = stringValue(row, key, label);
  if (!SHA256.test(value)) throw new Error(`${label}.${key}必须是 sha256 摘要`);
  return value;
}

function dateValue(row: UnknownRecord, key: string, label: string): string {
  const value = stringValue(row, key, label);
  const parsed = new Date(`${value}T00:00:00Z`);
  if (!DATE.test(value) || !Number.isFinite(parsed.getTime())
      || parsed.toISOString().slice(0, 10) !== value) {
    throw new Error(`${label}.${key}必须是真实 YYYY-MM-DD 日期`);
  }
  return value;
}

function timeValue(row: UnknownRecord, key: string, label: string): string {
  const value = stringValue(row, key, label);
  if (!ZONED_TIME.test(value) || !Number.isFinite(Date.parse(value))) {
    throw new Error(`${label}.${key}必须是带时区的有效时间`);
  }
  return value;
}

function nullableTimeValue(row: UnknownRecord, key: string, label: string): string | null {
  if (row[key] === null) return null;
  return timeValue(row, key, label);
}

function enumValue<T extends string>(
  row: UnknownRecord,
  key: string,
  values: readonly T[],
  label: string,
): T {
  const value = row[key];
  if (typeof value !== "string" || !values.includes(value as T)) {
    throw new Error(`${label}.${key}不在冻结枚举中`);
  }
  return value as T;
}

function finiteJson(value: unknown, seen = new Set<object>()): boolean {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (typeof value !== "object") return false;
  if (seen.has(value)) return false;
  seen.add(value);
  if (Array.isArray(value)) return value.every((item) => finiteJson(item, seen));
  return Object.entries(value as UnknownRecord).every(
    ([key, item]) => Boolean(key) && finiteJson(item, seen),
  );
}

function jsonObjectValue(row: UnknownRecord, key: string, label: string): UnknownRecord {
  const value = row[key];
  if (value === null || typeof value !== "object" || Array.isArray(value) || !finiteJson(value)) {
    throw new Error(`${label}.${key}必须是有限 JSON 对象`);
  }
  return value as UnknownRecord;
}

function assertBinding(
  actual: AssessmentBindingExpectation,
  expected: AssessmentBindingExpectation,
  label: string,
): void {
  for (const key of [
    "patientId", "eventId", "instanceId", "categoryKey", "definitionDigest",
    "formalOutcomeEligible",
  ] as const) {
    if (expected[key] !== undefined && actual[key] !== expected[key]) {
      throw new Error(`${label}与本次请求的 ${key} 绑定不一致`);
    }
  }
}

export function parseAssessmentScoringEvidence(
  value: unknown,
  expected: AssessmentBindingExpectation,
): AssessmentScoringEvidence {
  const label = "ScoringEvidence";
  const row = exactRecord(value, EVIDENCE_KEYS, label);
  const parsed: AssessmentScoringEvidence = {
    evidence_id: stringValue(row, "evidence_id", label),
    instance_id: stringValue(row, "instance_id", label),
    event_id: stringValue(row, "event_id", label),
    patient_id: stringValue(row, "patient_id", label),
    category_key: enumValue(row, "category_key", ASSESSMENT_CATEGORY_KEYS, label),
    definition_digest: digestValue(row, "definition_digest", label),
    item_response_set_digest: digestValue(row, "item_response_set_digest", label),
    scoring_algorithm_id: stringValue(row, "scoring_algorithm_id", label),
    scoring_algorithm_version: stringValue(row, "scoring_algorithm_version", label),
    scoring_algorithm_digest: digestValue(row, "scoring_algorithm_digest", label),
    score: finiteNumberValue(row, "score", label),
    result: jsonObjectValue(row, "result", label),
    result_digest: digestValue(row, "result_digest", label),
    answered_item_count: integerValue(row, "answered_item_count", label),
    missing_item_count: integerValue(row, "missing_item_count", label),
    stopped_early: booleanValue(row, "stopped_early", label),
    stopping_reason_code: nullableStringValue(row, "stopping_reason_code", label),
    formal_outcome_eligible: booleanValue(row, "formal_outcome_eligible", label),
    scored_at: timeValue(row, "scored_at", label),
  };
  assertBinding({
    patientId: parsed.patient_id,
    eventId: parsed.event_id,
    instanceId: parsed.instance_id,
    categoryKey: parsed.category_key,
    definitionDigest: parsed.definition_digest,
    formalOutcomeEligible: parsed.formal_outcome_eligible,
  }, expected, label);
  if (parsed.stopped_early !== (parsed.stopping_reason_code !== null)) {
    throw new Error("ScoringEvidence 的提前停止与原因必须同时成立或同时缺失");
  }
  return parsed;
}

export function parseAssessmentDeferral(
  value: unknown,
  expected: AssessmentBindingExpectation,
): AssessmentDeferralSummary {
  const label = "Deferral";
  const row = exactRecord(value, DEFERRAL_KEYS, label);
  const parsed: AssessmentDeferralSummary = {
    deferral_id: stringValue(row, "deferral_id", label),
    instance_id: stringValue(row, "instance_id", label),
    event_id: stringValue(row, "event_id", label),
    patient_id: stringValue(row, "patient_id", label),
    category_key: enumValue(row, "category_key", ASSESSMENT_CATEGORY_KEYS, label),
    definition_digest: digestValue(row, "definition_digest", label),
    reason_code: enumValue<AssessmentDeferralReason>(row, "reason_code", [
      "participant_unavailable", "clinical_or_safety", "technical_failure",
      "authorized_reschedule",
    ], label),
    deferred_until: dateValue(row, "deferred_until", label),
    approved_by: stringValue(row, "approved_by", label),
    approved_role: enumValue(row, "approved_role", ["admin", "local_m0"], label),
    approved_at: timeValue(row, "approved_at", label),
  };
  assertBinding({
    patientId: parsed.patient_id,
    eventId: parsed.event_id,
    instanceId: parsed.instance_id,
    categoryKey: parsed.category_key,
    definitionDigest: parsed.definition_digest,
  }, expected, label);
  return parsed;
}

export function parseAssessmentCancellation(value: unknown): AssessmentCancellationSummary {
  const label = "Cancellation";
  const row = exactRecord(value, CANCELLATION_KEYS, label);
  return {
    reason_code: enumValue<AssessmentCancellationReason>(row, "reason_code", [
      "schedule_changed", "participant_unavailable", "protocol_correction", "duplicate_event",
    ], label),
    cancelled_by: stringValue(row, "cancelled_by", label),
    cancelled_at: timeValue(row, "cancelled_at", label),
    switch_allowed: row.switch_allowed === true ? true : (() => {
      throw new Error("Cancellation.switch_allowed 必须是服务端签发的 true");
    })(),
  };
}

export function parseAssessmentCloseout(
  value: unknown,
  expected: Pick<AssessmentBindingExpectation, "patientId" | "eventId"> & {
    eventRevision?: number;
  },
): AssessmentCloseoutSummary {
  const label = "Closeout";
  const row = exactRecord(value, CLOSEOUT_KEYS, label);
  const parsed: AssessmentCloseoutSummary = {
    closeout_id: stringValue(row, "closeout_id", label),
    event_id: stringValue(row, "event_id", label),
    patient_id: stringValue(row, "patient_id", label),
    event_revision: integerValue(row, "event_revision", label, 1),
    report_status: enumValue(row, "report_status", [
      "no_additional_observation", "observation_recorded",
    ], label),
    fatigue_observed: booleanValue(row, "fatigue_observed", label),
    distress_or_discomfort_observed: booleanValue(
      row, "distress_or_discomfort_observed", label,
    ),
    participant_declined_to_continue: booleanValue(
      row, "participant_declined_to_continue", label,
    ),
    staff_assistance_occurred: booleanValue(row, "staff_assistance_occurred", label),
    environment_interruption_occurred: booleanValue(
      row, "environment_interruption_occurred", label,
    ),
    device_or_network_interruption_occurred: booleanValue(
      row, "device_or_network_interruption_occurred", label,
    ),
    note: nullableStringValue(row, "note", label),
    closed_by: stringValue(row, "closed_by", label),
    closed_at: timeValue(row, "closed_at", label),
    switch_allowed: row.switch_allowed === true ? true : (() => {
      throw new Error("Closeout.switch_allowed 必须是服务端签发的 true");
    })(),
  };
  assertBinding({ patientId: parsed.patient_id, eventId: parsed.event_id }, expected, label);
  if (expected.eventRevision !== undefined
      && parsed.event_revision !== expected.eventRevision) {
    throw new Error("Closeout 与事件修订号不一致");
  }
  const observations = [
    parsed.fatigue_observed,
    parsed.distress_or_discomfort_observed,
    parsed.participant_declined_to_continue,
    parsed.staff_assistance_occurred,
    parsed.environment_interruption_occurred,
    parsed.device_or_network_interruption_occurred,
  ];
  if (parsed.report_status === "no_additional_observation"
      ? observations.some(Boolean) || parsed.note !== null
      : !observations.some(Boolean) && parsed.note === null) {
    throw new Error("Closeout 现场观察状态与标记或备注矛盾");
  }
  return parsed;
}

export function isAssessmentInstanceTerminal(
  status: AssessmentInstanceStatus,
): status is "completed" | "approved_deferred" {
  return status === "completed" || status === "approved_deferred";
}

export function parseAssessmentInstance(
  value: unknown,
  expected: AssessmentBindingExpectation,
): AssessmentInstance {
  const label = "AssessmentInstance";
  const row = exactRecord(value, INSTANCE_KEYS, label);
  const instanceId = stringValue(row, "instance_id", label);
  const eventId = stringValue(row, "event_id", label);
  const patientId = stringValue(row, "patient_id", label);
  const categoryKey = enumValue(row, "category_key", ASSESSMENT_CATEGORY_KEYS, label);
  const definitionDigest = digestValue(row, "definition_digest", label);
  const formalOutcomeEligible = booleanValue(row, "formal_outcome_eligible", label);
  assertBinding({
    patientId,
    eventId,
    instanceId,
    categoryKey,
    definitionDigest,
    formalOutcomeEligible,
  }, expected, label);
  const status = enumValue<AssessmentInstanceStatus>(row, "status", [
    "due", "in_progress", "completed", "approved_deferred",
  ], label);
  const revision = integerValue(row, "revision", label, 1);
  const itemResponseCount = integerValue(row, "item_response_count", label);
  const requiredItemCount = integerValue(row, "required_item_count", label, 1);
  if (itemResponseCount > requiredItemCount) {
    throw new Error("AssessmentInstance 作答数超过冻结条目数");
  }
  const scoringAlgorithmId = stringValue(row, "scoring_algorithm_id", label);
  const scoringAlgorithmVersion = stringValue(row, "scoring_algorithm_version", label);
  const scoringAlgorithmDigest = digestValue(row, "scoring_algorithm_digest", label);
  const scoreMin = finiteNumberValue(row, "score_min", label);
  const scoreMax = finiteNumberValue(row, "score_max", label);
  if (scoreMin >= scoreMax) throw new Error("AssessmentInstance 计分范围矛盾");
  const automaticScoringPermitted = booleanValue(row, "automatic_scoring_permitted", label);
  const itemResponseStoragePermitted = booleanValue(
    row, "item_response_storage_permitted", label,
  );
  const resultStoragePermitted = booleanValue(row, "result_storage_permitted", label);
  const resultExportPermitted = booleanValue(row, "result_export_permitted", label);
  if (!automaticScoringPermitted || !itemResponseStoragePermitted || !resultStoragePermitted) {
    throw new Error("AssessmentInstance 缺少正式运行所需的冻结计分/存储许可");
  }
  const dataClassification = enumValue(row, "data_classification", [
    "research", "simulation",
  ], label);
  if (dataClassification === "simulation" && formalOutcomeEligible) {
    throw new Error("AssessmentInstance 模拟数据不得声明正式结局资格");
  }
  const scoringEvidence = row.scoring_evidence === null ? null : parseAssessmentScoringEvidence(
    row.scoring_evidence,
    { patientId, eventId, instanceId, categoryKey, definitionDigest, formalOutcomeEligible },
  );
  const deferral = row.deferral === null ? null : parseAssessmentDeferral(
    row.deferral,
    { patientId, eventId, instanceId, categoryKey, definitionDigest },
  );
  const createdAt = timeValue(row, "created_at", label);
  const completedAt = nullableTimeValue(row, "completed_at", label);
  const updatedAt = timeValue(row, "updated_at", label);
  if (Date.parse(updatedAt) < Date.parse(createdAt)
      || (completedAt !== null && (
        Date.parse(completedAt) < Date.parse(createdAt)
        || Date.parse(completedAt) > Date.parse(updatedAt)
      ))) {
    throw new Error("AssessmentInstance 时间顺序矛盾");
  }
  if (status === "completed") {
    if (scoringEvidence === null || deferral !== null || completedAt === null) {
      throw new Error("completed AssessmentInstance 必须有计分证据且不能有延期");
    }
    if (scoringEvidence.scoring_algorithm_id !== scoringAlgorithmId
        || scoringEvidence.scoring_algorithm_version !== scoringAlgorithmVersion
        || scoringEvidence.scoring_algorithm_digest !== scoringAlgorithmDigest) {
      throw new Error("ScoringEvidence 与 AssessmentInstance 冻结计分算法不一致");
    }
    if (scoringEvidence.score < scoreMin || scoringEvidence.score > scoreMax) {
      throw new Error("ScoringEvidence 分数超出 AssessmentInstance 冻结范围");
    }
    if (scoringEvidence.answered_item_count > requiredItemCount
        || scoringEvidence.missing_item_count > requiredItemCount
        || scoringEvidence.answered_item_count + scoringEvidence.missing_item_count
          !== requiredItemCount) {
      throw new Error("ScoringEvidence 条目计数与 AssessmentInstance 不一致");
    }
  } else if (status === "approved_deferred") {
    if (deferral === null || scoringEvidence !== null || completedAt !== null) {
      throw new Error("approved_deferred AssessmentInstance 必须仅有已审批延期证据");
    }
  } else if (scoringEvidence !== null || deferral !== null || completedAt !== null) {
    throw new Error("未终态 AssessmentInstance 不能携带计分、延期或完成时间");
  }
  return {
    instance_id: instanceId,
    event_id: eventId,
    patient_id: patientId,
    category_key: categoryKey,
    definition_bundle_id: stringValue(row, "definition_bundle_id", label),
    definition_bundle_digest: digestValue(row, "definition_bundle_digest", label),
    definition_id: stringValue(row, "definition_id", label),
    instrument_id: stringValue(row, "instrument_id", label),
    instrument_version: stringValue(row, "instrument_version", label),
    definition_digest: definitionDigest,
    item_set_digest: digestValue(row, "item_set_digest", label),
    administration_protocol_digest: digestValue(row, "administration_protocol_digest", label),
    response_schema_digest: digestValue(row, "response_schema_digest", label),
    result_schema_digest: digestValue(row, "result_schema_digest", label),
    missingness_rule_digest: digestValue(row, "missingness_rule_digest", label),
    stopping_rule_digest: digestValue(row, "stopping_rule_digest", label),
    scoring_algorithm_id: scoringAlgorithmId,
    scoring_algorithm_version: scoringAlgorithmVersion,
    scoring_algorithm_digest: scoringAlgorithmDigest,
    score_min: scoreMin,
    score_max: scoreMax,
    score_direction: enumValue(row, "score_direction", [
      "higher_is_better", "lower_is_better",
    ], label),
    score_rounding_rule: stringValue(row, "score_rounding_rule", label),
    automatic_scoring_permitted: automaticScoringPermitted,
    item_response_storage_permitted: itemResponseStoragePermitted,
    result_storage_permitted: resultStoragePermitted,
    result_export_permitted: resultExportPermitted,
    status,
    revision,
    item_response_count: itemResponseCount,
    required_item_count: requiredItemCount,
    data_classification: dataClassification,
    formal_outcome_eligible: formalOutcomeEligible,
    scoring_evidence: scoringEvidence,
    deferral,
    created_at: createdAt,
    completed_at: completedAt,
    updated_at: updatedAt,
  };
}

export function assessmentEventAllowsCloseout(event: AssessmentEvent): boolean {
  return event.status === "awaiting_closeout"
    && event.closeout === null
    && event.instances.length === ASSESSMENT_CATEGORY_KEYS.length
    && event.instances.every((instance) => isAssessmentInstanceTerminal(instance.status));
}

export function assessmentEventAllowsSwitch(event: AssessmentEvent): boolean {
  if (event.status === "cancelled") {
    return event.cancellation?.switch_allowed === true
      && event.closeout === null
      && event.instances.length === ASSESSMENT_CATEGORY_KEYS.length
      && event.instances.every((instance) => instance.status === "due");
  }
  return event.status === "closed"
    && event.cancellation === null
    && event.closeout?.switch_allowed === true
    && event.closeout.event_revision === event.revision
    && event.instances.length === ASSESSMENT_CATEGORY_KEYS.length
    && event.instances.every((instance) => isAssessmentInstanceTerminal(instance.status));
}

export function assessmentWorkflowAllowsPatientSwitch(
  events: readonly AssessmentEvent[],
  trainingStatuses: readonly SessionRuntimeStatus[],
): boolean {
  const assessmentOccupied = events.some(
    (event) => event.status === "in_progress" || event.status === "awaiting_closeout",
  );
  const trainingOccupied = trainingStatuses.some(
    (status) => status === "active" || status === "paused"
      || status === "intervention_completed",
  );
  return !assessmentOccupied && !trainingOccupied;
}

export function parseAssessmentEvent(
  value: unknown,
  expected: AssessmentEventExpectation,
): AssessmentEvent {
  const label = "AssessmentEvent";
  const row = exactRecord(value, EVENT_KEYS, label);
  if (row.schema_version !== ASSESSMENT_SCHEMA_VERSION) {
    throw new Error("AssessmentEvent schema_version 不受支持");
  }
  const eventId = stringValue(row, "event_id", label);
  const patientId = stringValue(row, "patient_id", label);
  const timepoint = enumValue<AssessmentTimepoint>(row, "timepoint", [
    "pretest", "posttest", "followup",
  ], label);
  if (patientId !== expected.patientId
      || (expected.eventId !== undefined && eventId !== expected.eventId)
      || (expected.timepoint !== undefined && timepoint !== expected.timepoint)) {
    throw new Error("AssessmentEvent 与本次请求的 patient/event/timepoint 不一致");
  }
  const formalOutcomeEligible = booleanValue(row, "formal_outcome_eligible", label);
  const isSimulation = booleanValue(row, "is_simulation", label);
  const dataClassification = enumValue(row, "data_classification", [
    "research", "simulation",
  ], label);
  if (isSimulation !== (dataClassification === "simulation")
      || (isSimulation && formalOutcomeEligible)) {
    throw new Error("AssessmentEvent 模拟/研究数据边界矛盾");
  }
  if (!Array.isArray(row.instances) || row.instances.length !== ASSESSMENT_CATEGORY_KEYS.length) {
    throw new Error("AssessmentEvent 必须恰好包含两个正式类别实例");
  }
  const instances = row.instances.map((candidate) => {
    const category = (candidate as { category_key?: unknown })?.category_key;
    const categoryKey = typeof category === "string"
      && ASSESSMENT_CATEGORY_KEYS.includes(category as AssessmentCategoryKey)
      ? category as AssessmentCategoryKey
      : undefined;
    return parseAssessmentInstance(candidate, {
      patientId,
      eventId,
      ...(categoryKey === undefined ? {} : { categoryKey }),
      ...(categoryKey === undefined || expected.definitionDigests?.[categoryKey] === undefined
        ? {}
        : { definitionDigest: expected.definitionDigests[categoryKey] }),
      formalOutcomeEligible,
    });
  });
  const categories = instances.map((instance) => instance.category_key);
  if (new Set(categories).size !== ASSESSMENT_CATEGORY_KEYS.length
      || ASSESSMENT_CATEGORY_KEYS.some((key) => !categories.includes(key))) {
    throw new Error("AssessmentEvent 两个实例类别必须唯一且完整");
  }
  const definitionBundleId = stringValue(row, "definition_bundle_id", label);
  const definitionBundleDigest = digestValue(row, "definition_bundle_digest", label);
  if (instances.some((instance) => instance.definition_bundle_id !== definitionBundleId
      || instance.definition_bundle_digest !== definitionBundleDigest
      || instance.data_classification !== dataClassification)) {
    throw new Error("AssessmentInstance 与事件的 definition bundle/数据分类绑定不一致");
  }
  const revision = integerValue(row, "revision", label, 1);
  const closeout = row.closeout === null ? null : parseAssessmentCloseout(row.closeout, {
    patientId,
    eventId,
    eventRevision: revision,
  });
  const cancellation = row.cancellation === null
    ? null
    : parseAssessmentCancellation(row.cancellation);
  const createdAt = timeValue(row, "created_at", label);
  const updatedAt = timeValue(row, "updated_at", label);
  if (Date.parse(updatedAt) < Date.parse(createdAt)
      || instances.some((instance) => Date.parse(instance.created_at) < Date.parse(createdAt)
        || Date.parse(instance.updated_at) > Date.parse(updatedAt))) {
    throw new Error("AssessmentEvent 与实例的时间顺序矛盾");
  }
  const status = enumValue<AssessmentEventStatus>(row, "status", [
    "due", "in_progress", "awaiting_closeout", "closed", "cancelled",
  ], label);
  const allTerminal = instances.every((instance) => isAssessmentInstanceTerminal(instance.status));
  const anyStarted = instances.some((instance) => instance.status !== "due");
  if ((status === "due" && (anyStarted || closeout !== null || cancellation !== null))
      || (status === "in_progress" && (
        allTerminal || closeout !== null || cancellation !== null
      ))
      || (status === "awaiting_closeout" && (
        !allTerminal || closeout !== null || cancellation !== null
      ))
      || (status === "closed" && (
        !allTerminal || closeout === null || cancellation !== null
      ))
      || (status === "cancelled" && (
        anyStarted || closeout !== null || cancellation === null
      ))) {
    throw new Error("AssessmentEvent 状态与实例/closeout/cancellation 矛盾");
  }
  const parsed: AssessmentEvent = {
    schema_version: ASSESSMENT_SCHEMA_VERSION,
    event_id: eventId,
    patient_id: patientId,
    assigned_assessor_id: stringValue(row, "assigned_assessor_id", label),
    timepoint,
    scheduled_date: dateValue(row, "scheduled_date", label),
    status,
    revision,
    is_simulation: isSimulation,
    data_classification: dataClassification,
    formal_outcome_eligible: formalOutcomeEligible,
    definition_bundle_id: definitionBundleId,
    definition_bundle_digest: definitionBundleDigest,
    instances,
    closeout,
    cancellation,
    created_at: createdAt,
    updated_at: updatedAt,
  };
  if (parsed.closeout !== null && Date.parse(parsed.closeout.closed_at) > Date.parse(updatedAt)) {
    throw new Error("Closeout 时间不能晚于 AssessmentEvent 更新时间");
  }
  if (parsed.cancellation !== null
      && Date.parse(parsed.cancellation.cancelled_at) > Date.parse(updatedAt)) {
    throw new Error("Cancellation 时间不能晚于 AssessmentEvent 更新时间");
  }
  return parsed;
}

export function parseAssessmentEventList(
  value: unknown,
  expected: Omit<AssessmentEventExpectation, "eventId" | "timepoint">,
): AssessmentEvent[] {
  if (!Array.isArray(value)) throw new Error("AssessmentEvent 列表必须是数组");
  const events = value.map((event) => parseAssessmentEvent(event, expected));
  const ids = events.map((event) => event.event_id);
  if (new Set(ids).size !== ids.length) throw new Error("AssessmentEvent 列表包含重复事件");
  return events;
}

export function parseAssessmentEventsToday(value: unknown): AssessmentEventsToday {
  const label = "AssessmentToday";
  const row = exactRecord(value, TODAY_KEYS, label);
  const asOfDate = dateValue(row, "as_of_date", label);
  if (!Array.isArray(row.events)) throw new Error("AssessmentToday.events 必须是数组");
  const events = row.events.map((candidate) => {
    const patientId = (candidate as { patient_id?: unknown })?.patient_id;
    if (typeof patientId !== "string") throw new Error("AssessmentToday 事件缺少 patient_id");
    return parseAssessmentEvent(candidate, { patientId });
  });
  const ids = events.map((event) => event.event_id);
  if (new Set(ids).size !== ids.length) throw new Error("AssessmentToday 包含重复事件");
  return { as_of_date: asOfDate, events };
}

export type FormalAssessmentMutation =
  | "create" | "start" | "cancel" | "response" | "complete" | "approve_defer" | "close";

export type FormalAssessmentMutationReadiness = Pick<ScaleProtocolReadiness,
  | "schema_version"
  | "definition_ready"
  | "definition_artifact_enforcement_ready"
  | "definition_artifacts_ready"
  | "formal_result_contract_ready"
  | "workflow_policy_ready"
  | "workflow_contract_ready"
  | "workflow_policy_enforcement_ready"
  | "workflow_ready"
  | "ready_for_research"
  | "instance_creation_enabled"
  | "automatic_scoring_enabled"
  | "training_metrics_are_formal_scale_results"
>;

// 网络写方法在进入 fetch 前调用，并逐项镜像服务端 v4 写入门槛。
// 缺字段、非布尔伪值、legacy v3 或任一层未就绪都必须在本地 fail closed。
export function assertFormalAssessmentMutationReady(
  readiness: FormalAssessmentMutationReadiness,
  mutation: FormalAssessmentMutation,
): void {
  const platformReady = readiness.schema_version === "scale-protocol-readiness.v4"
    && readiness.training_metrics_are_formal_scale_results === false
    && readiness.definition_ready === true
    && readiness.definition_artifact_enforcement_ready === true
    && readiness.definition_artifacts_ready === true
    && readiness.formal_result_contract_ready === true
    && readiness.workflow_contract_ready === true
    && readiness.workflow_policy_ready === true
    && readiness.workflow_policy_enforcement_ready === true
    && readiness.workflow_ready === true
    && readiness.ready_for_research === true
    && readiness.instance_creation_enabled === true;
  const permitted = platformReady
    && (mutation !== "complete" || readiness.automatic_scoring_enabled === true);
  if (!permitted) {
    throw new Error("正式评估定义、平台合同或工作流政策尚未就绪，已阻止写操作");
  }
}
