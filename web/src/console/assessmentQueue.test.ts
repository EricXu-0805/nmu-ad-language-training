import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import type { AssessmentEvent, AssessmentEventsToday } from "../types.ts";
import {
  assessmentInstanceStatusLabel,
  assessmentQueueStatusPresentation,
  assessmentQueueView,
  visibleAssessmentQueueEvents,
} from "./assessmentQueue.ts";

function event(
  eventId: string,
  status: AssessmentEvent["status"],
  scheduledDate = "2026-07-19",
): AssessmentEvent {
  return {
    event_id: eventId,
    patient_id: `P-${eventId}`,
    status,
    scheduled_date: scheduledDate,
    data_classification: "research",
    instances: [],
  } as unknown as AssessmentEvent;
}

function today(events: AssessmentEvent[]): AssessmentEventsToday {
  return { as_of_date: "2026-07-19", events };
}

test("assessment queue orders closeout, in-progress and due work without mutating input", () => {
  const input = [
    event("due", "due"),
    event("active", "in_progress"),
    event("closeout", "awaiting_closeout"),
  ];
  assert.deepEqual(
    visibleAssessmentQueueEvents(input).map((candidate) => candidate.event_id),
    ["closeout", "active", "due"],
  );
  assert.deepEqual(input.map((candidate) => candidate.event_id), ["due", "active", "closeout"]);
});

test("terminal, cancelled and server-omitted withdrawn events cannot become visible work", () => {
  assert.deepEqual(
    visibleAssessmentQueueEvents([
      event("closed", "closed"),
      event("cancelled", "cancelled"),
    ]),
    [],
  );
  assert.deepEqual(assessmentQueueView(today([]), null), {
    kind: "empty",
    asOfDate: "2026-07-19",
  });
});

test("loading, isolated error and ready queue states are explicit", () => {
  assert.deepEqual(assessmentQueueView(null, null), { kind: "loading" });
  assert.deepEqual(assessmentQueueView(null, "assessment unavailable"), {
    kind: "error",
    detail: "assessment unavailable",
  });
  const ready = assessmentQueueView(today([event("due", "due")]), null);
  assert.equal(ready.kind, "ready");
  if (ready.kind === "ready") assert.equal(ready.events.length, 1);
});

test("status wording distinguishes closeout, continuation, overdue and planned review", () => {
  assert.match(
    assessmentQueueStatusPresentation(
      event("closeout", "awaiting_closeout") as never,
      "2026-07-19",
    ).statusLabel,
    /待受控收尾/,
  );
  assert.match(
    assessmentQueueStatusPresentation(
      event("active", "in_progress") as never,
      "2026-07-19",
    ).nextStep,
    /独立评估流程/,
  );
  assert.match(
    assessmentQueueStatusPresentation(
      event("late", "due", "2026-07-18") as never,
      "2026-07-19",
    ).statusLabel,
    /逾期/,
  );
  assert.match(
    assessmentQueueStatusPresentation(
      event("planned", "due") as never,
      "2026-07-19",
    ).nextStep,
    /不得在床旁执行或录入/,
  );
});

test("instance summaries expose workflow evidence state but never a score", () => {
  assert.equal(assessmentInstanceStatusLabel("due", 0, 10), "待开始");
  assert.equal(assessmentInstanceStatusLabel("in_progress", 3, 10), "进行中·已记录 3/10 项");
  assert.equal(assessmentInstanceStatusLabel("completed", 10, 10), "服务端已生成计分证据");
  assert.equal(assessmentInstanceStatusLabel("approved_deferred", 0, 10), "已审批延期");
});

test("RunPicker integrates an independently loaded read-only assessment child queue", () => {
  const panel = readFileSync(new URL("./AssessmentQueuePanel.tsx", import.meta.url), "utf8");
  const picker = readFileSync(new URL("./RunPickerScreen.tsx", import.meta.url), "utf8");
  assert.match(picker, /<AssessmentQueuePanel \/>/);
  assert.match(picker, /<h3>今日训练队列<\/h3>/);
  assert.match(panel, /api\.listTodayAssessmentEvents\(\)/);
  assert.match(panel, /评估接口异常不会清空或锁住/);
  assert.match(panel, /已撤回受试者的事件不会在此重新出现/);
  assert.doesNotMatch(panel, /api\.(create|start|cancel|submit|complete|approve|close)Assessment/);
  assert.doesNotMatch(panel, /scoring_evidence\.score/);
  assert.doesNotMatch(panel, /listTodayVisitPlans/);
});
