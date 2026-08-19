import assert from "node:assert/strict";
import test from "node:test";
import type { Session } from "../types.ts";
import { runPickerPresentation, sessionsVisibleForRunPicker } from "./runPickerAccess.ts";

function session(sessionId: string, runtimeStatus: Session["runtime_status"]): Session {
  return {
    session_id: sessionId,
    patient_id: "P-001",
    is_simulation: false,
    week_no: 2,
    phase_type: "正式训练",
    event_line: "正式训练",
    item_bank_version_id: "bank-v1",
    runtime_status: runtimeStatus,
  };
}

test("data steward presentation opens a completed-session governance entry", () => {
  const presentation = runPickerPresentation({ canStart: false, canViewCompleted: true });
  assert.equal(presentation.completedOnly, true);
  assert.match(presentation.title, /已完成场次/);
  assert.match(presentation.historyTitle, /受控导出/);
  assert.match(presentation.readOnlyDescription, /自动展开/);
});

test("training roles keep the reviewed queue and recovery wording", () => {
  const presentation = runPickerPresentation({ canStart: true, canViewCompleted: true });
  assert.equal(presentation.completedOnly, false);
  assert.equal(presentation.title, "今日训练");
  assert.match(presentation.description, /先处理待办评估/);
  assert.equal(presentation.historyTitle, "异常恢复与未完复核");
});

test("completed-only entry never exposes active or review-pending sessions", () => {
  const sessions = [
    session("active", "active"),
    session("review", "intervention_completed"),
    session("done", "completed"),
  ];
  assert.deepEqual(
    sessionsVisibleForRunPicker(sessions, true).map((item) => item.session_id),
    ["done"],
  );
  assert.equal(sessionsVisibleForRunPicker(sessions, false).length, 3);
});
