import assert from "node:assert/strict";
import test from "node:test";

import { nextSittingNo, pickOpenSession, runNextTask, type NextTaskClient } from "./nextTask.ts";
import { localToday } from "./quickDrill.ts";
import type { Session, VisitPlanCreateRequest, VisitPlanMutationRequest, VisitPlanReceipt } from "../types.ts";

function receipt(overrides: Partial<VisitPlanReceipt>): VisitPlanReceipt {
  return {
    plan_id: "vp-1",
    patient_id: "P-SIM-01",
    scheduled_date: localToday(),
    scheduled_time: null,
    queue_order: null,
    session_sitting_no: 1,
    week_no: 2,
    phase_type: "正式训练",
    event_line: "正式训练",
    item_bank_version_id: "v1",
    autopilot_profile_version_id: null,
    is_simulation: true,
    data_classification: "simulation",
    status: "draft",
    revision: 1,
    created_by: "ACTOR",
    created_at: "2026-07-22T00:00:00",
    approved_by: null,
    approved_at: null,
    started_by: null,
    started_at: null,
    cancelled_by: null,
    cancelled_at: null,
    session_id: null,
    ...overrides,
  };
}

interface Recorder {
  create: VisitPlanCreateRequest[];
  approve: { planId: string; body: VisitPlanMutationRequest }[];
  start: { revision: number; body: VisitPlanMutationRequest }[];
  getSession: VisitPlanReceipt[];
}

function fakeClient(
  rec: Recorder,
  facts: { sessions?: Session[]; plans?: VisitPlanReceipt[] } = {},
): NextTaskClient {
  return {
    patientSessions: async () => facts.sessions ?? [],
    listPatientVisitPlans: async () => facts.plans ?? [],
    createVisitPlan: async (body) => {
      rec.create.push(body);
      return receipt({ status: "draft", revision: 1 });
    },
    approveVisitPlan: async (planId, body) => {
      rec.approve.push({ planId, body });
      return receipt({ status: "approved", revision: 2, approved_by: "ACTOR" });
    },
    startVisitPlan: async (approvedPlan, body) => {
      rec.start.push({ revision: approvedPlan.revision, body });
      return receipt({ status: "started", revision: 3, started_by: "ACTOR", session_id: "s-1" });
    },
    getStartedVisitSession: async (r) => {
      rec.getSession.push(r);
      return { session_id: r.session_id, patient_id: r.patient_id, runtime_status: "active" } as Session;
    },
  };
}

test("next task resumes an open session before touching any plan", async () => {
  const rec: Recorder = { create: [], approve: [], start: [], getSession: [] };
  const open = { session_id: "s-open", patient_id: "P-SIM-01", runtime_status: "paused" } as Session;
  const outcome = await runNextTask(fakeClient(rec, { sessions: [open] }), "P-SIM-01", { autoApproveAndCreate: true }, "seed10");

  assert.equal(outcome.kind, "resumed");
  assert.equal(outcome.session.session_id, "s-open");
  assert.equal(rec.create.length, 0);
  assert.equal(rec.start.length, 0);
});

test("next task starts a due approved plan without creating a duplicate", async () => {
  const rec: Recorder = { create: [], approve: [], start: [], getSession: [] };
  const approved = receipt({ plan_id: "vp-due", status: "approved", revision: 4 });
  const outcome = await runNextTask(fakeClient(rec, { plans: [approved] }), "P-SIM-01", { autoApproveAndCreate: true }, "seed11");

  assert.equal(outcome.kind, "started");
  assert.equal(outcome.kind === "started" && outcome.via, "approved_plan");
  assert.equal(rec.create.length, 0);
  assert.equal(rec.approve.length, 0);
  assert.equal(rec.start.length, 1);
  assert.equal(rec.start[0].body.expected_revision, 4);
  assert.equal(rec.start[0].body.idempotency_key, "nt-start-seed11");
});

test("next task approves then starts a due draft in the runnable vertical", async () => {
  const rec: Recorder = { create: [], approve: [], start: [], getSession: [] };
  const draft = receipt({ plan_id: "vp-draft", status: "draft", revision: 2 });
  const outcome = await runNextTask(fakeClient(rec, { plans: [draft] }), "P-SIM-01", { autoApproveAndCreate: true }, "seed12");

  assert.equal(outcome.kind === "started" && outcome.via, "draft_plan");
  assert.equal(rec.create.length, 0);
  assert.equal(rec.approve.length, 1);
  assert.equal(rec.approve[0].planId, "vp-draft");
  assert.equal(rec.approve[0].body.expected_revision, 2);
  assert.equal(rec.start[0].body.expected_revision, 2);
  assert.equal(rec.start[0].body.idempotency_key, "nt-start-seed12");
});

test("next task creates a new plan on the next free sitting, never reusing occupied slots", async () => {
  const rec: Recorder = { create: [], approve: [], start: [], getSession: [] };
  const plans = [
    receipt({ plan_id: "vp-old", status: "started", session_sitting_no: 1 }),
    receipt({ plan_id: "vp-cancelled", status: "cancelled", session_sitting_no: 9 }),
  ];
  const outcome = await runNextTask(fakeClient(rec, { plans }), "P-SIM-01", { autoApproveAndCreate: true }, "seed13");

  assert.equal(outcome.kind === "started" && outcome.via, "new_plan");
  assert.equal(rec.create.length, 1);
  // 已开训的 sitting 1 仍占槽,取消的 sitting 9 不占;下一个自由槽位 = 2。
  assert.equal(rec.create[0].session_sitting_no, 2);
  assert.equal(rec.create[0].week_no, 2);
});

test("next task surfaces the server block instead of silently switching paths", async () => {
  const rec: Recorder = { create: [], approve: [], start: [], getSession: [] };
  const client: NextTaskClient = {
    ...fakeClient(rec),
    approveVisitPlan: async () => { throw new Error("当前题库未达到真实研究冻结/质控门禁"); },
  };
  await assert.rejects(
    () => runNextTask(client, "P-RES-01", { autoApproveAndCreate: true }, "seed14"),
    /冻结\/质控门禁/);
  assert.equal(rec.start.length, 0);
  assert.equal(rec.getSession.length, 0);
});

test("helpers: open-session pick and sitting derivation are deterministic", () => {
  assert.equal(pickOpenSession([]), null);
  assert.equal(
    pickOpenSession([{ session_id: "a", runtime_status: "completed" } as Session]), null);
  assert.equal(
    pickOpenSession([
      { session_id: "a", runtime_status: "completed" } as Session,
      { session_id: "b", runtime_status: "intervention_completed" } as Session,
    ])?.session_id,
    "b");
  assert.equal(nextSittingNo([]), 1);
  assert.equal(nextSittingNo([
    receipt({ status: "started", session_sitting_no: 3 }),
    receipt({ status: "cancelled", session_sitting_no: 8 }),
    receipt({ status: "draft", session_sitting_no: 2, week_no: 1, phase_type: "关系建立", event_line: "关系建立环节" }),
  ]), 4);
});

test("research profiles never get auto-approve or auto-create: blocked with honest reasons", async () => {
  const recDraft: Recorder = { create: [], approve: [], start: [], getSession: [] };
  const draft = receipt({ plan_id: "vp-research-draft", status: "draft", is_simulation: false, data_classification: "research" });
  const blockedOnDraft = await runNextTask(
    fakeClient(recDraft, { plans: [draft] }), "P-RES-01", { autoApproveAndCreate: false }, "seed15");
  assert.equal(blockedOnDraft.kind, "blocked");
  assert.match(blockedOnDraft.kind === "blocked" ? blockedOnDraft.reason : "", /人工审核通过/);
  assert.equal(recDraft.approve.length, 0);
  assert.equal(recDraft.create.length, 0);

  const recEmpty: Recorder = { create: [], approve: [], start: [], getSession: [] };
  const blockedEmpty = await runNextTask(
    fakeClient(recEmpty), "P-RES-01", { autoApproveAndCreate: false }, "seed16");
  assert.equal(blockedEmpty.kind, "blocked");
  assert.match(blockedEmpty.kind === "blocked" ? blockedEmpty.reason : "", /创建并审核训练安排/);
  assert.equal(recEmpty.create.length, 0);
});

test("research profiles may still start a due approved plan and resume open sessions", async () => {
  const rec: Recorder = { create: [], approve: [], start: [], getSession: [] };
  const approved = receipt({ plan_id: "vp-res-approved", status: "approved", revision: 6, is_simulation: false, data_classification: "research" });
  const outcome = await runNextTask(
    fakeClient(rec, { plans: [approved] }), "P-RES-01", { autoApproveAndCreate: false }, "seed17");
  assert.equal(outcome.kind === "started" && outcome.via, "approved_plan");
  assert.equal(rec.start[0].body.expected_revision, 6);
  assert.equal(rec.approve.length, 0);
});
