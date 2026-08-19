import assert from "node:assert/strict";
import test from "node:test";
import { parseAIQualityDashboard } from "./qualityDashboardContract.ts";
import { buildAIQualityDashboardViewModel } from "./qualityDashboardViewModel.ts";

const REASONS = {
  restricted_or_withdrawn_sessions: 0,
  classification_inconsistent_sessions: 0,
  protocol_binding_invalid_sessions: 0,
  structural_invalid_evidence_records: 0,
  lineage_invalid_turns: 0,
  audio_evidence_unavailable_turns: 0,
  ai_attempt_status_unknown_turns: 0,
  ai_judgement_status_unknown_turns: 0,
  asr_review_status_unknown_turns: 0,
  human_truth_unavailable_turns: 0,
  binary_prediction_unavailable_turns: 0,
  latency_unavailable_attempts: 0,
};

function payload(classification: "research" | "simulation" = "research"): Record<string, unknown> {
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
      diagnostics: { status: "complete", reason_counts: { ...REASONS } },
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

function firstRow(value: Record<string, unknown>): Record<string, unknown> {
  return (value.rows as Array<Record<string, unknown>>)[0]!;
}

function metric(
  rows: Array<{ key: string; value: string; state: string; detail: string }>,
  key: string,
): { key: string; value: string; state: string; detail: string } {
  const result = rows.find((row) => row.key === key);
  assert.ok(result, `missing metric ${key}`);
  return result;
}

test("view model derives operational, prompt, safety, and latency metrics without mixing truth", () => {
  const group = buildAIQualityDashboardViewModel(parseAIQualityDashboard(payload())).groups[0]!;

  assert.equal(metric(group.operationalMetrics, "coverage").value, "80.0%");
  assert.equal(metric(group.operationalMetrics, "asr-correction-rate").value, "40.0%");
  assert.equal(metric(group.promptMetrics, "prompt-escalation-rate").value, "50.0%");
  assert.equal(metric(group.promptMetrics, "tell-answer-rate").value, "未知");
  assert.equal(metric(group.promptMetrics, "prompt-level-3").value, "未知");
  assert.match(metric(group.promptMetrics, "tell-answer-rate").detail, /旧版本未记录此项/);
  assert.match(metric(group.promptMetrics, "prompt-level-0").detail, /录音尝试所处提示上下文/);
  assert.equal(metric(group.safetyMetrics, "technical-failure-rate").value, "10.0%");
  assert.equal(metric(group.latencyMetrics, "latency-p50").value, "800 毫秒");
  assert.equal(metric(group.latencyMetrics, "latency-p95").value, "2,100 毫秒");
  assert.match(metric(group.latencyMetrics, "latency-p50").label, /^AI 处理延迟/);
  assert.match(metric(group.latencyMetrics, "latency-p50").detail, /不包含床旁播放、采集和上传阶段/);
});

test("role-derived visibility scope is explicit and warns against cross-role comparison", () => {
  const expected = {
    owner_sessions: /本人负责场次/,
    terminal_sessions: /已进入终态的场次/,
    all_sessions: /全部授权场次/,
  } as const;

  for (const [scope, title] of Object.entries(expected)) {
    const value = payload();
    firstRow(value).visibility_scope = scope;
    const notice = buildAIQualityDashboardViewModel(parseAIQualityDashboard(value)).groups[0]!.visibilityNotice;
    assert.match(notice.title, title);
    assert.match(notice.text, /overall/);
    assert.match(notice.text, /不可.*直接对比/);
  }
});

test("coverage and binary eligibility/exclusion remain visible beside fixed diagnostics", () => {
  const group = buildAIQualityDashboardViewModel(parseAIQualityDashboard(payload())).groups[0]!;

  assert.equal(metric(group.coverageMetrics, "visible-sessions").value, "3");
  assert.equal(metric(group.coverageMetrics, "binary-eligible").value, "8");
  assert.equal(metric(group.coverageMetrics, "binary-excluded").value, "0");
  assert.equal(metric(group.diagnosticMetrics, "diagnostic-latency_unavailable_attempts").value, "0");
  assert.equal(group.diagnosticsNotice.title, "当前固定覆盖诊断为完整");
});

test("research comparison computes confusion, FP, and FN rates but never uses a green availability state", () => {
  const truth = buildAIQualityDashboardViewModel(parseAIQualityDashboard(payload())).groups[0]!.researchTruth;

  assert.equal(truth.availability, "available");
  assert.equal(truth.comparisonKind, "research");
  assert.equal(truth.sectionTitle, "人工锁定研究真值（复核口径）");
  assert.equal(truth.notice.tone, "info");
  assert.equal(truth.matrix.falsePositive.value, "1");
  assert.equal(truth.matrix.falseNegative.value, "2");
  assert.equal(truth.agreementRate.value, "62.5%");
  assert.equal(truth.falsePositiveRate.value, "33.3%");
  assert.equal(truth.falseNegativeRate.value, "40.0%");
});

test("simulation uses only simulation-reference wording and explicitly rejects research use", () => {
  const group = buildAIQualityDashboardViewModel(parseAIQualityDashboard(payload("simulation"))).groups[0]!;
  const truthText = [
    group.researchTruth.sectionTitle,
    group.researchTruth.metricsTitle,
    group.researchTruth.notice.title,
    group.researchTruth.notice.text,
  ].join(" ");

  assert.equal(group.researchTruth.comparisonKind, "simulation");
  assert.match(truthText, /模拟复核/);
  assert.match(truthText, /不可|不得/);
  assert.doesNotMatch(truthText, /人工研究真值|人工锁定研究真值/);
});

test("partial evidence explains unknown through diagnostics instead of calling every null missing", () => {
  const value = payload();
  const row = firstRow(value);
  for (const key of Object.keys(row.operational as Record<string, unknown>)) {
    (row.operational as Record<string, unknown>)[key] = null;
  }
  row.research_truth = {
    reviewed_decisions: null,
    true_positive: null,
    true_negative: null,
    false_positive: null,
    false_negative: null,
  };
  row.coverage = {
    ...(row.coverage as Record<string, unknown>),
    human_truth_locked_turns: 0,
    binary_eligible_reviewed_decisions: 0,
    binary_excluded_decisions: 0,
  };
  row.diagnostics = {
    status: "partial",
    reason_counts: { ...REASONS, human_truth_unavailable_turns: 10 },
  };
  const group = buildAIQualityDashboardViewModel(parseAIQualityDashboard(value)).groups[0]!;

  const coverage = metric(group.operationalMetrics, "coverage");
  assert.equal(coverage.value, "未知");
  assert.match(coverage.detail, /证据不足，暂无法计算/);
  assert.doesNotMatch(coverage.detail, /源数据缺失/);
  assert.equal(group.researchTruth.availability, "unknown");
  assert.equal(group.diagnosticsNotice.tone, "warn");
});

test("privacy-suppressed rows render suppressed rather than zero or generic missing", () => {
  const value = payload();
  const row = firstRow(value);
  row.suppression = {
    status: "suppressed",
    reason: "research_small_cell",
    minimum_distinct_subjects: 5,
    distinct_subjects: null,
  };
  for (const section of ["coverage", "operational", "research_truth"] as const) {
    for (const key of Object.keys(row[section] as Record<string, unknown>)) {
      (row[section] as Record<string, unknown>)[key] = null;
    }
  }
  row.diagnostics = {
    status: "suppressed",
    reason_counts: Object.fromEntries(Object.keys(REASONS).map((key) => [key, null])),
  };
  const group = buildAIQualityDashboardViewModel(parseAIQualityDashboard(value)).groups[0]!;

  assert.equal(group.suppressionNotice.tone, "warn");
  assert.match(group.suppressionNotice.title, /隐私抑制/);
  assert.match(group.suppressionNotice.text, /受试者数与各项计数暂不显示/);
  assert.doesNotMatch(group.suppressionNotice.text, /不发布任何人数/);
  assert.equal(metric(group.coverageMetrics, "source-turns").state, "suppressed");
  assert.equal(metric(group.coverageMetrics, "source-turns").value, "已抑制");
  assert.match(metric(group.operationalMetrics, "coverage").detail, /隐私规则抑制/);
  assert.equal(group.researchTruth.availability, "suppressed");
  assert.equal(group.researchTruth.notice.tone, "warn");
});

test("unfrozen research release explains the durable privacy gate", () => {
  const value = payload();
  const row = firstRow(value);
  row.suppression = {
    status: "suppressed",
    reason: "research_release_not_frozen",
    minimum_distinct_subjects: null,
    distinct_subjects: null,
  };
  for (const section of ["coverage", "operational", "research_truth"] as const) {
    for (const key of Object.keys(row[section] as Record<string, unknown>)) {
      (row[section] as Record<string, unknown>)[key] = null;
    }
  }
  row.diagnostics = {
    status: "suppressed",
    reason_counts: Object.fromEntries(Object.keys(REASONS).map((key) => [key, null])),
  };
  const group = buildAIQualityDashboardViewModel(parseAIQualityDashboard(value)).groups[0]!;
  assert.match(group.suppressionNotice.text, /发布批次尚未冻结/);
  assert.match(group.suppressionNotice.text, /待研究数据冻结版本发布后开放/);
  assert.doesNotMatch(group.suppressionNotice.text, /通过隐私门槛/);
});

test("zero reviewed comparisons stay empty and never claim perfect accuracy", () => {
  const value = payload();
  const row = firstRow(value);
  row.coverage = {
    ...(row.coverage as Record<string, unknown>),
    human_truth_locked_turns: 0,
    binary_eligible_reviewed_decisions: 0,
    binary_excluded_decisions: 0,
  };
  row.research_truth = {
    reviewed_decisions: 0,
    true_positive: 0,
    true_negative: 0,
    false_positive: 0,
    false_negative: 0,
  };
  const truth = buildAIQualityDashboardViewModel(parseAIQualityDashboard(value)).groups[0]!.researchTruth;

  assert.equal(truth.availability, "empty");
  assert.equal(truth.notice.tone, "warn");
  assert.equal(truth.agreementRate.value, "未知");
  assert.equal(truth.falsePositiveRate.value, "未知");
  assert.equal(truth.falseNegativeRate.value, "未知");
});

test("overall null dimensions are described as intentionally unpublished, not silently omitted", () => {
  const dimensions = buildAIQualityDashboardViewModel(parseAIQualityDashboard(payload())).groups[0]!.dimensions;

  assert.match(dimensions.find((item) => item.key === "device_profile")!.value, /设备画像覆盖未知/);
  assert.match(dimensions.find((item) => item.key === "provider_id")!.value, /提供方覆盖未知/);
  assert.match(dimensions.find((item) => item.key === "protocol_version")!.value, /overall 首版不按此维度分组/);
});
