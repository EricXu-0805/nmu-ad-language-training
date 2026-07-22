import assert from "node:assert/strict";
import test from "node:test";
import { parseAIQualityDashboard } from "./qualityDashboardContract.ts";

const DIAGNOSTIC_KEYS = [
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
] as const;

function diagnosticCounts(value: number | null = 0): Record<string, number | null> {
  return Object.fromEntries(DIAGNOSTIC_KEYS.map((key) => [key, value]));
}

function validPayload(classification: "research" | "simulation" = "research"): Record<string, unknown> {
  return {
    schema_version: "ai-quality-dashboard.v2",
    generated_at: "2026-07-22T10:00:00+08:00",
    privacy: {
      aggregation_only: true,
      contains_patient_identifiers: false,
      contains_audio: false,
      contains_transcripts: false,
    },
    rows: [{
      visibility_scope: "all_sessions",
      dimensions: {
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
      },
      suppression: classification === "research" ? {
        status: "released",
        reason: null,
        minimum_distinct_subjects: 5,
        distinct_subjects: 7,
      } : {
        status: "not_applicable",
        reason: null,
        minimum_distinct_subjects: null,
        distinct_subjects: null,
      },
      coverage: {
        visible_sessions: 3,
        included_sessions: 2,
        source_turns: 10,
        audio_evidenced_turns: 9,
        attempts_observed: 10,
        prompt_level_known_attempts: 10,
        processing_status_known_attempts: 10,
        latency_known_attempts: 8,
        ai_attempt_status_known_turns: 10,
        ai_judgement_status_known_turns: 10,
        asr_review_status_known_turns: 10,
        human_truth_locked_turns: 8,
        binary_eligible_reviewed_decisions: 8,
        binary_excluded_decisions: 0,
      },
      diagnostics: {
        status: "complete",
        reason_counts: diagnosticCounts(),
      },
      operational: {
        eligible_turns: 10,
        ai_attempted_turns: 9,
        ai_judged_turns: 8,
        asr_reviewed_turns: 5,
        asr_corrected_turns: 2,
        total_attempts: 10,
        prompt_level_0_count: 5,
        prompt_level_1_count: 3,
        prompt_level_2_count: 2,
        prompt_level_3_count: null,
        technical_failure_attempts: 1,
        technical_pause_count: 1,
        researcher_takeover_count: 0,
        latency_sample_count: 8,
        latency_p50_ms: 800,
        latency_p95_ms: 2100,
      },
      research_truth: {
        reviewed_decisions: 8,
        true_positive: 3,
        true_negative: 2,
        false_positive: 1,
        false_negative: 2,
      },
    }],
  };
}

function firstRow(payload: Record<string, unknown>): Record<string, unknown> {
  return (payload.rows as Array<Record<string, unknown>>)[0]!;
}

function nullEveryValue(record: Record<string, unknown>): void {
  for (const key of Object.keys(record)) record[key] = null;
}

function suppress(payload: Record<string, unknown>): void {
  const row = firstRow(payload);
  row.suppression = {
    status: "suppressed",
    reason: "research_small_cell",
    minimum_distinct_subjects: 5,
    distinct_subjects: null,
  };
  nullEveryValue(row.coverage as Record<string, unknown>);
  row.diagnostics = { status: "suppressed", reason_counts: diagnosticCounts(null) };
  nullEveryValue(row.operational as Record<string, unknown>);
  nullEveryValue(row.research_truth as Record<string, unknown>);
}

test("v2 parser accepts one exact overall row and binds it to the requested classification", () => {
  const parsed = parseAIQualityDashboard(validPayload(), "research");

  assert.equal(parsed.schema_version, "ai-quality-dashboard.v2");
  assert.equal(parsed.rows.length, 1);
  assert.equal(parsed.rows[0]?.dimensions.data_classification, "research");
  assert.equal(parsed.rows[0]?.visibility_scope, "all_sessions");
  assert.equal(parsed.rows[0]?.dimensions.device_profile, null);
  assert.equal(parsed.rows[0]?.coverage.binary_eligible_reviewed_decisions, 8);
  assert.equal(parsed.rows[0]?.suppression.status, "released");
});

test("exact field sets reject patient, session, audio, transcript, and omitted fields without echoing values", () => {
  for (const [scope, key] of [
    ["dimensions", "patient_id"],
    ["dimensions", "session_id"],
    ["operational", "audio_url"],
    ["research_truth", "transcript"],
  ] as const) {
    const payload = validPayload();
    (firstRow(payload)[scope] as Record<string, unknown>)[key] = "forbidden-cleartext";
    assert.throws(() => parseAIQualityDashboard(payload), /字段集合不符合 v2 聚合隐私契约/);
  }
  const omitted = validPayload();
  delete (firstRow(omitted).coverage as Record<string, unknown>).source_turns;
  assert.throws(() => parseAIQualityDashboard(omitted), /字段集合不符合 v2/);
});

test("visibility scope is required and restricted to the fixed role-derived enum", () => {
  for (const scope of ["current_patient", "owner_sessions ", "", null]) {
    const value = validPayload();
    firstRow(value).visibility_scope = scope;
    assert.throws(() => parseAIQualityDashboard(value), /visibility_scope/);
  }

  const omitted = validPayload();
  delete firstRow(omitted).visibility_scope;
  assert.throws(() => parseAIQualityDashboard(omitted), /字段集合不符合/);

  for (const scope of ["owner_sessions", "terminal_sessions", "all_sessions"]) {
    const value = validPayload();
    firstRow(value).visibility_scope = scope;
    assert.equal(parseAIQualityDashboard(value).rows[0]?.visibility_scope, scope);
  }
});

test("privacy envelope and calendar timestamp fail closed", () => {
  const unsafePrivacy = validPayload();
  (unsafePrivacy.privacy as Record<string, unknown>).contains_transcripts = true;
  assert.throws(() => parseAIQualityDashboard(unsafePrivacy), /contains_transcripts 必须为 false/);

  for (const timestamp of [
    "2026-07-22T10:00:00",
    "2026-02-30T10:00:00Z",
    "2026-07-22T10:00:00+14:01",
    "2026-07-22T25:00:00Z",
  ]) {
    const payload = validPayload();
    payload.generated_at = timestamp;
    assert.throws(() => parseAIQualityDashboard(payload), /真实存在且带时区/);
  }
  const edgeOffset = validPayload();
  edgeOffset.generated_at = "2026-07-22T10:00:00+14:00";
  assert.doesNotThrow(() => parseAIQualityDashboard(edgeOffset));
});

test("overall-only dimensions reject singleton PII and every free-text version field", () => {
  for (const [key, value] of [
    ["content_group", "张三"],
    ["provider_id", "patient-001"],
    ["device_profile", "病房3床12号"],
    ["protocol_version", "week2-v1"],
    ["asr_engine_version", "qwen-asr"],
    ["judge_engine_version", "judge-v1"],
  ]) {
    const payload = validPayload();
    (firstRow(payload).dimensions as Record<string, unknown>)[key] = value;
    assert.throws(() => parseAIQualityDashboard(payload), /overall 首版必须为 null/);
  }
});

test("research truth is either all null or all known with an exact matrix sum", () => {
  const cellsWithoutReviewed = validPayload();
  (firstRow(cellsWithoutReviewed).research_truth as Record<string, unknown>).reviewed_decisions = null;
  assert.throws(() => parseAIQualityDashboard(cellsWithoutReviewed), /五项必须全部为 null 或全部为已知/);

  const partialCells = validPayload();
  (firstRow(partialCells).research_truth as Record<string, unknown>).false_negative = null;
  assert.throws(() => parseAIQualityDashboard(partialCells), /五项必须全部为 null 或全部为已知/);

  const excessiveKnownCells = validPayload();
  (firstRow(excessiveKnownCells).research_truth as Record<string, unknown>).reviewed_decisions = 2;
  assert.throws(() => parseAIQualityDashboard(excessiveKnownCells), /混淆矩阵之和/);
});

test("binary eligible plus excluded exactly accounts for every locked reference", () => {
  const payload = validPayload();
  (firstRow(payload).coverage as Record<string, unknown>).binary_excluded_decisions = 1;
  assert.throws(() => parseAIQualityDashboard(payload), /可核查与排除判定之和必须等于人工锁定轮次/);
});

test("research small cells suppress counts, actual subject cardinality, diagnostics, and truth", () => {
  const payload = validPayload();
  suppress(payload);
  const parsed = parseAIQualityDashboard(payload, "research");
  assert.equal(parsed.rows[0]?.suppression.status, "suppressed");
  assert.equal(parsed.rows[0]?.suppression.distinct_subjects, null);
  assert.equal(parsed.rows[0]?.operational.eligible_turns, null);
  assert.equal(parsed.rows[0]?.diagnostics.reason_counts.human_truth_unavailable_turns, null);

  const leaking = structuredClone(payload);
  (firstRow(leaking).coverage as Record<string, unknown>).source_turns = 1;
  assert.throws(() => parseAIQualityDashboard(leaking), /不得发布覆盖、运行或研究真值计数/);

  const disclosedCellSize = structuredClone(payload);
  (firstRow(disclosedCellSize).suppression as Record<string, unknown>).distinct_subjects = 2;
  assert.throws(() => parseAIQualityDashboard(disclosedCellSize), /隐藏受试者数/);
});

test("an unfrozen research release suppresses the entire row", () => {
  const payload = validPayload();
  suppress(payload);
  const suppression = firstRow(payload).suppression as Record<string, unknown>;
  suppression.reason = "research_release_not_frozen";
  suppression.minimum_distinct_subjects = null;
  const parsed = parseAIQualityDashboard(payload, "research");
  assert.equal(parsed.rows[0]?.suppression.reason, "research_release_not_frozen");

  const leaking = structuredClone(payload);
  (firstRow(leaking).operational as Record<string, unknown>).total_attempts = 0;
  assert.throws(() => parseAIQualityDashboard(leaking), /不得发布覆盖、运行或研究真值计数/);

  const unknownReason = structuredClone(payload);
  (firstRow(unknownReason).suppression as Record<string, unknown>).reason = "future_reason";
  assert.throws(() => parseAIQualityDashboard(unknownReason), /reason/);
});

test("simulation cannot claim research release and research cannot bypass threshold proof", () => {
  const simulationReleased = validPayload("simulation");
  firstRow(simulationReleased).suppression = {
    status: "released",
    reason: null,
    minimum_distinct_subjects: 1,
    distinct_subjects: 1,
  };
  assert.throws(() => parseAIQualityDashboard(simulationReleased), /released 必须证明/);

  const researchNotApplicable = validPayload();
  firstRow(researchNotApplicable).suppression = {
    status: "not_applicable",
    reason: null,
    minimum_distinct_subjects: null,
    distinct_subjects: null,
  };
  assert.throws(() => parseAIQualityDashboard(researchNotApplicable), /not_applicable 只允许/);

  for (const minimum of [1, 101]) {
    const invalidThreshold = validPayload();
    (firstRow(invalidThreshold).suppression as Record<string, unknown>).minimum_distinct_subjects = minimum;
    assert.throws(() => parseAIQualityDashboard(invalidThreshold), /released 必须证明/);
  }
});

test("diagnostic status cannot disguise nonzero reasons or suppressed values", () => {
  const disguisedPartial = validPayload();
  const diagnostics = firstRow(disguisedPartial).diagnostics as Record<string, unknown>;
  (diagnostics.reason_counts as Record<string, unknown>).latency_unavailable_attempts = 1;
  assert.throws(() => parseAIQualityDashboard(disguisedPartial), /status 必须与固定原因计数/);

  const suppressedWithZeros = validPayload();
  suppress(suppressedWithZeros);
  (firstRow(suppressedWithZeros).diagnostics as Record<string, unknown>).reason_counts = diagnosticCounts(0);
  assert.throws(() => parseAIQualityDashboard(suppressedWithZeros), /必须隐藏全部诊断计数/);
});

test("classification mismatch and more than one overall row fail closed", () => {
  assert.throws(() => parseAIQualityDashboard(validPayload("simulation"), "research"), /其他数据分区/);

  const duplicate = validPayload();
  duplicate.rows = [
    ...(duplicate.rows as unknown[]),
    structuredClone((duplicate.rows as unknown[])[0]),
  ];
  assert.throws(() => parseAIQualityDashboard(duplicate), /最多一个 overall 行/);

  const empty = validPayload();
  empty.rows = [];
  assert.throws(
    () => parseAIQualityDashboard(empty, "research"),
    /必须返回一个 released 或 suppressed overall 行/,
  );
});

test("operational contradictions still fail before rendering derived rates", () => {
  const moreJudgementsThanAttempts = validPayload();
  (firstRow(moreJudgementsThanAttempts).operational as Record<string, unknown>).ai_judged_turns = 10;
  assert.throws(() => parseAIQualityDashboard(moreJudgementsThanAttempts), /完成判定数不能大于/);

  const reversedLatency = validPayload();
  (firstRow(reversedLatency).operational as Record<string, unknown>).latency_p50_ms = 3000;
  assert.throws(() => parseAIQualityDashboard(reversedLatency), /P50 延迟不能大于 P95/);

  const missingLatencyPercentiles = validPayload();
  const missingLatency = firstRow(missingLatencyPercentiles).operational as Record<string, unknown>;
  missingLatency.latency_p50_ms = null;
  missingLatency.latency_p95_ms = null;
  assert.throws(() => parseAIQualityDashboard(missingLatencyPercentiles), /正延迟样本数必须同时提供/);

  const unreceiptedTellAnswer = validPayload();
  (firstRow(unreceiptedTellAnswer).operational as Record<string, unknown>).prompt_level_3_count = 1;
  assert.throws(() => parseAIQualityDashboard(unreceiptedTellAnswer), /告知答案呈现回执/);
});

test("operational turn counts cannot exceed their source and known-status coverage", () => {
  const sourceDrift = validPayload();
  (firstRow(sourceDrift).operational as Record<string, unknown>).eligible_turns = 11;
  assert.throws(() => parseAIQualityDashboard(sourceDrift), /必须等于冻结源轮次数/);

  const statusDrift = validPayload();
  (firstRow(statusDrift).coverage as Record<string, unknown>).ai_attempt_status_known_turns = 8;
  assert.throws(() => parseAIQualityDashboard(statusDrift), /不能大于对应状态可判定覆盖数/);

  const audioDrift = validPayload();
  (firstRow(audioDrift).coverage as Record<string, unknown>).audio_evidenced_turns = 8;
  assert.throws(() => parseAIQualityDashboard(audioDrift), /不能大于录音证据完整轮次数/);

  const promptDrift = validPayload();
  const promptOperational = firstRow(promptDrift).operational as Record<string, unknown>;
  promptOperational.prompt_level_0_count = 50;
  promptOperational.prompt_level_1_count = 30;
  promptOperational.prompt_level_2_count = 20;
  assert.throws(() => parseAIQualityDashboard(promptDrift), /提示尝试之和不能大于总尝试数/);

  const promptCoverageDrift = validPayload();
  (firstRow(promptCoverageDrift).coverage as Record<string, unknown>).prompt_level_known_attempts = 9;
  assert.throws(() => parseAIQualityDashboard(promptCoverageDrift), /必须等于提示层级已知尝试数/);
});

test("cross-section attempt and AI-processing-latency totals must agree", () => {
  const attemptDrift = validPayload();
  (firstRow(attemptDrift).coverage as Record<string, unknown>).attempts_observed = 11;
  assert.throws(() => parseAIQualityDashboard(attemptDrift), /已观察尝试数必须等于运行总尝试数/);

  const latencyDrift = validPayload();
  (firstRow(latencyDrift).coverage as Record<string, unknown>).latency_known_attempts = 7;
  assert.throws(() => parseAIQualityDashboard(latencyDrift), /延迟可计算尝试数必须等于 AI 处理延迟样本数/);

  const unknownOperationalTotals = validPayload();
  const operational = firstRow(unknownOperationalTotals).operational as Record<string, unknown>;
  operational.total_attempts = null;
  operational.latency_sample_count = null;
  operational.latency_p50_ms = null;
  operational.latency_p95_ms = null;
  assert.doesNotThrow(() => parseAIQualityDashboard(unknownOperationalTotals));
});
