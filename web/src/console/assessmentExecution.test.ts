import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import type { AssessmentEvent, AssessmentInstance } from "../types.ts";
import {
  assessmentActionGates,
  parseAssessmentMutationFailure,
  parseResponseInput,
  performAssessmentMutation,
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

// ---------------------------------------------------------------------------
// U3（收据 165）：保存失败不得清空草稿；重签失败不得毁掉已有的录音授权。
// 分两层钉：源码接线神谕证明 UI 真的走了 helper，行为测试证明 helper 本身对。
// ---------------------------------------------------------------------------

const drawerSource = readFileSync(
  new URL("./AssessmentExecutionDrawer.tsx", import.meta.url), "utf8");

test("saving an item response only clears the draft after the server accepted it", () => {
  // run 必须把"成功了吗"作为布尔返回，submitResponse 必须等它。
  assert.match(drawerSource, /async function run\([\s\S]*?\): Promise<boolean>/);
  assert.match(drawerSource, /performAssessmentMutation\(/);
  assert.match(drawerSource, /const saved = await run\(/);
  assert.match(drawerSource, /if \(saved !== true\) return;/);

  // 三个清理动作必须落在 saved === true 之后，且不得挂在 .then 上——
  // 原实现用 `void run(...).then(清理)`，而 run 把失败吞成 setFailure，
  // 于是 409/403/5xx 也照样清空草稿。
  assert.doesNotMatch(drawerSource, /run\([\s\S]*?\)\.then\(/);
  const savedGuard = drawerSource.indexOf("if (saved !== true) return;");
  assert.ok(savedGuard > 0);
  for (const cleanup of ['setGrant(null)', 'setRawValue("")', "setExpectedItemRevision(0)"]) {
    const at = drawerSource.indexOf(cleanup, savedGuard);
    assert.ok(at > savedGuard, `${cleanup} 必须在 saved 守卫之后`);
  }
});

test("a failed recording-authorization re-issue keeps the previous grant", () => {
  const issue = drawerSource.slice(
    drawerSource.indexOf("async function issueGrant"),
    drawerSource.indexOf("async function submitResponse"));
  assert.ok(issue.length > 0);
  assert.match(issue, /performAssessmentMutation\(/);
  // 失败分支只能动 localError：旧 grant / revision / rawValue 一律不许碰。
  const failureBranch = issue.slice(issue.indexOf("if (!outcome.ok)"));
  assert.ok(failureBranch.length > 0, "失败必须是显式分支，不能是 catch 里顺手清空");
  assert.doesNotMatch(failureBranch, /setGrant\(/);
  assert.doesNotMatch(failureBranch, /setExpectedItemRevision\(/);
  assert.doesNotMatch(failureBranch, /setRawValue\(/);
});

test("performAssessmentMutation delivers the value once on success and never on failure", async () => {
  const delivered: string[] = [];
  const ok = await performAssessmentMutation(
    async () => "receipt", (value: string) => { delivered.push(value); });
  assert.deepEqual(ok, { ok: true });
  assert.deepEqual(delivered, ["receipt"]);

  for (const rejection of [
    { detailData: { code: "assessment_item_revision_conflict", message: "冲突" } },
    { detailData: { code: "assessment_artifact_not_authorized", message: "未授权" } },
    { detail: "服务器内部错误" },
    new TypeError("Failed to fetch"),
    { detail: "回执解析失败" },
  ]) {
    const calls: string[] = [];
    const outcome = await performAssessmentMutation(
      async () => { throw rejection; }, (value: string) => { calls.push(value); });
    assert.equal(outcome.ok, false);
    assert.equal(calls.length, 0, "失败时绝不调用成功回调");
    if (outcome.ok === false) {
      assert.ok(outcome.failure.message.length > 0);
    }
  }
});

test("a draft survives every failure shape and is only cleared on success", async () => {
  // 用内存草稿复刻 drawer 的清理动作，证明"只在 ok 之后清"这个语义本身成立。
  function makeDraft() {
    return { raw: "12", revision: 3, grant: { itemKey: "naming_01", digest: "d", revision: 4 } };
  }
  const clear = (draft: ReturnType<typeof makeDraft>) => {
    draft.grant = null as never; draft.raw = ""; draft.revision = 0;
  };

  for (const rejection of [
    { status: 409 }, { status: 403 }, { status: 500 },
    new TypeError("network"), { detail: "malformed receipt" },
  ]) {
    const draft = makeDraft();
    const outcome = await performAssessmentMutation(
      async () => { throw rejection; }, () => { clear(draft); });
    assert.equal(outcome.ok, false);
    assert.deepEqual(draft, makeDraft(), "失败后三项草稿必须原样保留");
  }

  const draft = makeDraft();
  const outcome = await performAssessmentMutation(async () => "ok", () => { clear(draft); });
  assert.deepEqual(outcome, { ok: true });
  assert.equal(draft.raw, "");
  assert.equal(draft.revision, 0);
  assert.equal(draft.grant, null);
});
