import assert from "node:assert/strict";
import test from "node:test";
import type { AssessmentEvent, AssessmentInstance } from "../types.ts";
import {
  assessmentActionGates,
  parseAssessmentMutationFailure,
  parseResponseInput,
} from "./assessmentExecution.ts";

function instance(overrides: Partial<AssessmentInstance> = {}): AssessmentInstance {
  return {
    instance_id: "ins-1",
    event_id: "evt-1",
    patient_id: "P-1",
    category_key: "untrained_standardized_naming",
    definition_bundle_id: "bundle-1",
    definition_bundle_digest: "sha256:" + "a".repeat(64),
    definition_id: "def-1",
    instrument_id: "inst-1",
    instrument_version: "v1",
    definition_digest: "sha256:" + "a".repeat(64),
    item_set_digest: "sha256:" + "a".repeat(64),
    administration_protocol_digest: "sha256:" + "a".repeat(64),
    response_schema_digest: "sha256:" + "a".repeat(64),
    result_schema_digest: "sha256:" + "a".repeat(64),
    missingness_rule_digest: "sha256:" + "a".repeat(64),
    stopping_rule_digest: "sha256:" + "a".repeat(64),
    scoring_algorithm_id: "alg",
    scoring_algorithm_version: "v1",
    scoring_algorithm_digest: "sha256:" + "a".repeat(64),
    score_min: 0,
    score_max: 10,
    score_direction: "higher_is_better",
    score_rounding_rule: "integer_exact",
    automatic_scoring_permitted: true,
    item_response_storage_permitted: true,
    result_storage_permitted: true,
    result_export_permitted: false,
    required_item_count: 2,
    status: "in_progress",
    revision: 2,
    is_simulation: true,
    data_classification: "simulation",
    formal_outcome_eligible: false,
    item_response_count: 0,
    scoring_evidence: null,
    deferral: null,
    created_at: "2026-08-08T00:00:00Z",
    updated_at: "2026-08-08T00:00:00Z",
    completed_at: null,
    ...overrides,
  } as AssessmentInstance;
}

function event(overrides: Partial<AssessmentEvent> = {}): AssessmentEvent {
  return {
    schema_version: "formal-assessment.v1",
    event_id: "evt-1",
    patient_id: "P-1",
    assigned_assessor_id: "A-1",
    timepoint: "pretest",
    scheduled_date: "2026-08-08",
    status: "in_progress",
    revision: 2,
    is_simulation: true,
    data_classification: "simulation",
    formal_outcome_eligible: false,
    definition_bundle_id: "bundle-1",
    definition_bundle_digest: "sha256:" + "a".repeat(64),
    instances: [instance()],
    closeout: null,
    cancellation: null,
    created_at: "2026-08-08T00:00:00Z",
    updated_at: "2026-08-08T00:00:00Z",
    ...overrides,
  } as AssessmentEvent;
}

test("gates follow event/instance status exactly", () => {
  const due = assessmentActionGates(event({
    status: "due", instances: [instance({ status: "due", revision: 1 })],
  }));
  assert.equal(due.canStart, true);
  assert.equal(due.canCancel, true);
  assert.equal(due.canClose, false);
  assert.equal(due.instanceActions["ins-1"].canRespond, false);

  const active = assessmentActionGates(event());
  assert.equal(active.canStart, false);
  assert.deepEqual(active.instanceActions["ins-1"], {
    canRespond: true, canComplete: true, canDefer: true,
  });

  const closing = assessmentActionGates(event({
    status: "awaiting_closeout",
    instances: [instance({ status: "completed" })],
  }));
  assert.equal(closing.canClose, true);
  assert.equal(closing.instanceActions["ins-1"].canRespond, false);

  const closed = assessmentActionGates(event({
    status: "closed", instances: [instance({ status: "completed" })],
  }));
  assert.equal(closed.canStart, false);
  assert.equal(closed.canClose, false);
});

test("mutation failures surface readiness blockers and policy hints", () => {
  const readinessRefusal = parseAssessmentMutationFailure({
    detailData: {
      code: "formal_assessment_not_ready",
      message: "正式量表尚未全链就绪",
      readiness_status: "awaiting_workflow_policy",
      blocking_codes: [
        "workflow_policy.executable_file.not_ready",
        "some.unknown.code",
      ],
    },
  });
  assert.equal(readinessRefusal.code, "formal_assessment_not_ready");
  assert.match(readinessRefusal.hint ?? "", /全链就绪/);
  assert.deepEqual(readinessRefusal.blockingHints, [
    "可执行工作流政策文件缺失或与 manifest 不一致",
    "some.unknown.code",
  ]);

  const policyRefusal = parseAssessmentMutationFailure({
    detailData: {
      code: "assessment_workflow_policy_assessor_mismatch",
      message: "冻结政策要求由被分配评估员本人执行本命令",
    },
  });
  assert.match(policyRefusal.hint ?? "", /被分配评估员本人/);

  const artifactRefusal = parseAssessmentMutationFailure({
    detailData: {
      code: "assessment_artifact_not_authorized",
      message: "source artifact is not authorized for this assessment instance",
    },
  });
  assert.match(artifactRefusal.hint ?? "", /重新签发/);

  const fallback = parseAssessmentMutationFailure({ detail: "网关超时" });
  assert.equal(fallback.message, "网关超时");
  assert.equal(fallback.blockingHints.length, 0);
});

test("response input is validated locally before any request", () => {
  assert.deepEqual(parseResponseInput(" 2 "), { ok: true, value: 2 });
  assert.equal(parseResponseInput("").ok, false);
  assert.equal(parseResponseInput("abc").ok, false);
  assert.equal(parseResponseInput("Infinity").ok, false);
});
