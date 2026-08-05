import assert from "node:assert/strict";
import test from "node:test";

import { runQuickDrill, type QuickDrillClient } from "./quickDrill.ts";
import type { Session, VisitPlanCreateRequest, VisitPlanMutationRequest, VisitPlanReceipt } from "../types.ts";

function receipt(overrides: Partial<VisitPlanReceipt>): VisitPlanReceipt {
  return {
    plan_id: "vp-1",
    patient_id: "P-SIM-01",
    scheduled_date: "2026-07-22",
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

function fakeClient(rec: Recorder): QuickDrillClient {
  return {
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

test("quick drill orchestrates create→approve→start on the only startable sim vertical", async () => {
  const rec: Recorder = { create: [], approve: [], start: [], getSession: [] };
  const session = await runQuickDrill(fakeClient(rec), "P-SIM-01", "seed01");

  // 只创建当前唯一可开场的第2周正式训练垂直。
  assert.equal(rec.create.length, 1);
  assert.equal(rec.create[0].patient_id, "P-SIM-01");
  assert.equal(rec.create[0].week_no, 2);
  assert.equal(rec.create[0].phase_type, "正式训练");
  assert.equal(rec.create[0].event_line, "正式训练");
  assert.match(rec.create[0].idempotency_key, /^qd-create-seed01$/);

  // 修订号严格串联:approve 用 create 的 revision,start 用 approve 的 revision。
  assert.equal(rec.approve.length, 1);
  assert.equal(rec.approve[0].body.expected_revision, 1);
  assert.equal(rec.start.length, 1);
  assert.equal(rec.start[0].body.expected_revision, 2);

  // 幂等键成套稳定,同一 seed 三条命令各自可重放。
  assert.equal(rec.approve[0].body.idempotency_key, "qd-approve-seed01");
  assert.equal(rec.start[0].body.idempotency_key, "qd-start-seed01");

  // 只从 started receipt 取权威场次。
  assert.equal(rec.getSession.length, 1);
  assert.equal(rec.getSession[0].status, "started");
  assert.equal(session.session_id, "s-1");
  assert.equal(session.runtime_status, "active");
});

test("quick drill stops at the failing step and never starts a session it could not approve", async () => {
  const rec: Recorder = { create: [], approve: [], start: [], getSession: [] };
  const client: QuickDrillClient = {
    ...fakeClient(rec),
    approveVisitPlan: async () => { throw new Error("visit_plan_context_invalid"); },
  };
  await assert.rejects(() => runQuickDrill(client, "P-SIM-01", "seed02"), /visit_plan_context_invalid/);
  assert.equal(rec.create.length, 1);
  assert.equal(rec.start.length, 0);
  assert.equal(rec.getSession.length, 0);
});
