import assert from "node:assert/strict";
import test from "node:test";
import {
  isFrozenReleasePayload,
  parseAIQualityRelease,
} from "./qualityReleaseContract.ts";

const COVERAGE_KEYS = [
  "visible_sessions", "included_sessions", "source_turns",
  "audio_evidenced_turns", "attempts_observed", "prompt_level_known_attempts",
  "processing_status_known_attempts", "latency_known_attempts",
  "ai_attempt_status_known_turns", "ai_judgement_status_known_turns",
  "asr_review_status_known_turns", "human_truth_locked_turns",
  "binary_eligible_reviewed_decisions", "binary_excluded_decisions",
] as const;

function payload(): Record<string, unknown> {
  return {
    schema_version: "ai-quality-release.v1",
    generated_at: "2026-08-16T00:00:00Z",
    privacy: {
      aggregation_only: true,
      contains_patient_identifiers: false,
      contains_audio: false,
      contains_transcripts: false,
    },
    rows: [{
      visibility_scope: "frozen_release_cohort",
      dimensions: {
        data_classification: "research",
        week_no: null, phase_type: null, task_type: null, content_group: null,
        provider_id: null, device_profile: null, protocol_version: null,
        asr_engine_version: null, judge_engine_version: null,
      },
      suppression: {
        status: "released",
        reason: null,
        minimum_distinct_subjects: 5,
        distinct_subjects: null,
      },
      coverage: Object.fromEntries(COVERAGE_KEYS.map((key) => [key, null])),
      diagnostics: { status: "partial", reason_counts: null },
      operational: {
        asr_manual_correction_rate: 0.31,
        prompt_escalation_rate: 0.44,
        pause_rate: 0.02,
        takeover_rate: 0.01,
        latency_p50_band: "100-109",
        latency_p95_band: "180-189",
      },
      research_truth: {
        agreement_rate: 0.87,
        false_positive_rate: 0.07,
        false_negative_rate: 0.06,
        reviewed_decisions_band: "600-609",
      },
      release: {
        cohort_size_band: "30-39",
        session_count_band: "240-249",
        registry_version: "quality-release-registry.v1",
        cohort_rule_version: "quality-release-cohort.v1",
      },
    }],
  };
}

function firstRow(value: Record<string, unknown>): Record<string, unknown> {
  return (value.rows as Record<string, unknown>[])[0];
}

test("一份合规的冻结发布能被解析出来", () => {
  const parsed = parseAIQualityRelease(payload());
  assert.equal(parsed.rows[0].operational.asr_manual_correction_rate, 0.31);
  assert.equal(parsed.rows[0].release.cohort_size_band, "30-39");
  assert.equal(parsed.rows[0].suppression.distinct_subjects, null);
});

test("按 schema_version 认得出它不是 v2 聚合", () => {
  assert.equal(isFrozenReleasePayload(payload()), true);
  assert.equal(isFrozenReleasePayload({ schema_version: "ai-quality-dashboard.v2" }), false);
  assert.equal(isFrozenReleasePayload(null), false);
  assert.equal(isFrozenReleasePayload([payload()]), false);
});

test("多出一个字段就整份拒收，而不是忽略它", () => {
  const smuggled = payload();
  firstRow(smuggled).exact_subject_count = 30;
  assert.throws(() => parseAIQualityRelease(smuggled), /字段集合不符合冻结发布契约/);
});

test("精确受试者数一旦出现就拒收", () => {
  const leaked = payload();
  (firstRow(leaked).suppression as Record<string, unknown>).distinct_subjects = 30;
  assert.throws(() => parseAIQualityRelease(leaked), /不得携带精确受试者数/);
});

test("覆盖计数只要有一个非 null 就拒收", () => {
  const leaked = payload();
  (firstRow(leaked).coverage as Record<string, unknown>).source_turns = 16800;
  assert.throws(() => parseAIQualityRelease(leaked), /coverage 在冻结发布里必须整块为 null/);
});

test("诊断计数部分 null 也拒收——那张位图本身会说话", () => {
  const partial = payload();
  (firstRow(partial).diagnostics as Record<string, unknown>).reason_counts = {
    lineage_invalid_turns: 3,
  };
  assert.throws(() => parseAIQualityRelease(partial), /reason_counts 必须整块为 null/);
});

test("比率超出精度就拒收——多出来的小数位本身是一条泄漏", () => {
  const precise = payload();
  (firstRow(precise).operational as Record<string, unknown>).pause_rate = 0.0234567;
  assert.throws(() => parseAIQualityRelease(precise), /小数位超出冻结发布允许的精度/);
});

test("比率越界就拒收", () => {
  for (const bad of [-0.1, 1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
    const broken = payload();
    (firstRow(broken).operational as Record<string, unknown>).pause_rate = bad;
    assert.throws(() => parseAIQualityRelease(broken), /必须是 0 到 1 之间的比率或 null/);
  }
});

test("档位必须是「下界-上界」且不能颠倒", () => {
  for (const bad of ["30", "30-", "a-b", "<script>", "39-30"]) {
    const broken = payload();
    (firstRow(broken).release as Record<string, unknown>).cohort_size_band = bad;
    assert.throws(() => parseAIQualityRelease(broken));
  }
});

test("三个真值率必须同时发布或同时抑制", () => {
  const half = payload();
  (firstRow(half).research_truth as Record<string, unknown>).false_negative_rate = null;
  assert.throws(
    () => parseAIQualityRelease(half),
    /三个率必须同时发布或同时抑制/,
  );
});

test("声明已抑制却仍带着指标，属于自相矛盾，拒收", () => {
  const lying = payload();
  (firstRow(lying).suppression as Record<string, unknown>).status = "suppressed";
  assert.throws(() => parseAIQualityRelease(lying), /声明已抑制却仍带着指标/);
});

test("全部抑制的那一份是合法的", () => {
  const suppressed = payload();
  (firstRow(suppressed).suppression as Record<string, unknown>).status = "suppressed";
  (firstRow(suppressed).suppression as Record<string, unknown>).reason =
    "research_release_reader_not_authorized";
  firstRow(suppressed).operational = {
    asr_manual_correction_rate: null, prompt_escalation_rate: null,
    pause_rate: null, takeover_rate: null,
    latency_p50_band: null, latency_p95_band: null,
  };
  firstRow(suppressed).research_truth = {
    agreement_rate: null, false_positive_rate: null,
    false_negative_rate: null, reviewed_decisions_band: null,
  };
  const parsed = parseAIQualityRelease(suppressed);
  assert.equal(parsed.rows[0].suppression.status, "suppressed");
  assert.equal(parsed.rows[0].operational.pause_rate, null);
});

test("隐私边界被改动就整份拒收", () => {
  for (const key of ["contains_audio", "contains_transcripts", "contains_patient_identifiers"]) {
    const broken = payload();
    (broken.privacy as Record<string, unknown>)[key] = true;
    assert.throws(() => parseAIQualityRelease(broken), /privacy 边界被改动/);
  }
});

test("非研究分区不得走冻结发布这条路", () => {
  const wrong = payload();
  (firstRow(wrong).dimensions as Record<string, unknown>).data_classification = "simulation";
  assert.throws(() => parseAIQualityRelease(wrong), /冻结发布只用于研究分区/);
});

test("v1 只发整体，任何分组维度带值都拒收", () => {
  const grouped = payload();
  (firstRow(grouped).dimensions as Record<string, unknown>).week_no = 2;
  assert.throws(() => parseAIQualityRelease(grouped), /必须为 null/);
});

test("行数不是正好一行就拒收", () => {
  for (const rows of [[], [firstRow(payload()), firstRow(payload())]]) {
    const broken = payload();
    broken.rows = rows;
    assert.throws(() => parseAIQualityRelease(broken), /正好返回一个 overall 行/);
  }
});
