import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  ASSESSMENT_TIMEPOINTS,
  checkAssessmentCreateDraft,
  classifyAssessmentCreateFailure,
  newAssessmentIdempotencyKey,
} from "./assessmentCreate.ts";

test("only impossible-to-send drafts are refused locally", () => {
  const good = checkAssessmentCreateDraft({
    patientId: " P-1 ", timepoint: "pretest", scheduledDate: "2026-08-20",
  });
  assert.deepEqual(good, {
    ok: true, patientId: "P-1", timepoint: "pretest", scheduledDate: "2026-08-20",
  });

  for (const [draft, pattern] of [
    [{ patientId: "  ", timepoint: "pretest", scheduledDate: "2026-08-20" }, /研究编号/],
    [{ patientId: "P-1", timepoint: "", scheduledDate: "2026-08-20" }, /评估时点/],
    [{ patientId: "P-1", timepoint: "baseline", scheduledDate: "2026-08-20" }, /评估时点/],
    [{ patientId: "P-1", timepoint: "pretest", scheduledDate: "2026/08/20" }, /YYYY-MM-DD/],
    [{ patientId: "P-1", timepoint: "pretest", scheduledDate: "2026-02-30" }, /真实存在/],
  ] as const) {
    const checked = checkAssessmentCreateDraft(draft);
    assert.equal(checked.ok, false);
    if (checked.ok === false) assert.match(checked.reason, pattern);
  }
});

test("the three frozen timepoints are the closed set", () => {
  assert.deepEqual(ASSESSMENT_TIMEPOINTS.map(([key]) => key),
    ["pretest", "posttest", "followup"]);
});

test("an ambiguous failure is never reported as 'not created'", () => {
  // 这几类里服务器可能已经落库：绝不能让研究者以为没建成而换个键再建一次。
  for (const error of [
    { status: 0, detail: "无法连接服务器" },
    { status: 500, detail: "内部错误" },
    { status: 502, detail: "网关错误" },
    { status: 408, detail: "超时" },
    { status: 429, detail: "太频繁" },
    new TypeError("Failed to fetch"),
  ]) {
    const outcome = classifyAssessmentCreateFailure(error);
    assert.equal(outcome.kind, "unknown", JSON.stringify(error));
    assert.equal(outcome.retrySameKey, true);
  }

  // 明确拒绝：服务器说了它没建，可以改输入后用新键重来。
  for (const error of [
    { status: 400, detail: "排期不在冻结政策允许的时点窗口内" },
    { status: 403, detail: "冻结政策要求被分配评估员本人执行" },
    { status: 409, detail: "该受试者还有未收尾的评估事件" },
    { status: 422, detail: "字段不合法" },
  ]) {
    const outcome = classifyAssessmentCreateFailure(error);
    assert.equal(outcome.kind, "rejected");
    assert.equal(outcome.retrySameKey, false);
    assert.equal(outcome.message, error.detail);
  }
});

test("idempotency keys are opaque, unique and long enough for the server contract", () => {
  const first = newAssessmentIdempotencyKey();
  const second = newAssessmentIdempotencyKey();
  assert.notEqual(first, second);
  assert.ok(first.length >= 8 && first.length <= 200);
  assert.match(first, /^[A-Za-z0-9._:-]+$/, "服务端 OPAQUE_KEY_PATTERN 只收这些字符");
});

test("the queue panel gates the create entry on server readiness and stays read-only itself", () => {
  const panel = readFileSync(
    new URL("./AssessmentQueuePanel.tsx", import.meta.url), "utf8");
  // 入口只在服务端 ready_for_research 为真时出现——本地不另造一套就绪判定。
  assert.match(panel, /executionOpen && <AssessmentEventCreateForm/);
  // 面板自己仍然是只读投影：写调用留在独立组件里，与 AssessmentExecutionDrawer 同一模式。
  assert.doesNotMatch(panel, /api\.createAssessmentEvent/);
});

test("an unknown create outcome keeps the same idempotency key", () => {
  const form = readFileSync(
    new URL("./AssessmentEventCreateForm.tsx", import.meta.url), "utf8");
  assert.match(form, /checkAssessmentCreateDraft\(/);
  assert.match(form, /classifyAssessmentCreateFailure\(/);
  assert.match(form, /api\.createAssessmentEvent\(/);
  // 结果未知时不得换幂等键——换了就可能建出第二个事件。
  assert.match(form, /if \(!failure\.retrySameKey\)[\s\S]{0,120}newAssessmentIdempotencyKey\(\)/);
  // 未知结局不得渲染成"没建成"。
  assert.match(form, /结果未知/);
});
