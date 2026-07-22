import assert from "node:assert/strict";
import test from "node:test";

import { auditActionLabel, auditDisplaySummary } from "./auditPresentation.ts";

test("visit-plan audit presentation hides opaque ids from legacy summaries", () => {
  const rawPlanId = "vp_internal_secret_7c9";
  const rawSessionId = "session_internal_secret_2a1";
  const rendered = auditDisplaySummary({
    action: "visit_plan_start",
    summary: `plan=${rawPlanId} session=${rawSessionId} status=started revision=3 date=2026-07-22 week=2`,
  });

  assert.equal(rendered, "训练安排已启动 · 第 2 周 · 2026-07-22");
  assert.doesNotMatch(rendered, /vp_internal|session_internal|plan=|session=/);
  assert.equal(auditActionLabel("visit_plan_start"), "启动训练安排");
});

test("visit-plan audit presentation fails closed for malformed or future metadata", () => {
  const rendered = auditDisplaySummary({
    action: "visit_plan_reschedule",
    summary: "plan=vp_do_not_render date=vp_embedded_in_date week=vp_hidden status=private",
  });

  assert.equal(rendered, "训练安排已更新");
  assert.equal(auditActionLabel("visit_plan_reschedule"), "更新训练安排");
});

test("unrelated audit actions retain their canonical controlled summary", () => {
  assert.equal(
    auditDisplaySummary({ action: "score_lock", summary: "锡1 final=2" }),
    "锡1 final=2",
  );
});
