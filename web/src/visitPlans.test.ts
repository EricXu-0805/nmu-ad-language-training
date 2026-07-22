import assert from "node:assert/strict";
import test from "node:test";
import {
  LatestVisitPlanHistoryRequest,
  PendingVisitPlanCommandKeys,
  isSameApprovedVisitPlan,
  parsePatientSessionList,
  parseStartedVisitSession,
  parseVisitPlanList,
  parseVisitPlanReceipt,
  parseVisitPlanToday,
  reconcileStartedVisitPlan,
  startedVisitSessionId,
} from "./visitPlans.ts";

const PLAN_A = `vp_${"A".repeat(24)}`;
const PLAN_B = `vp_${"B".repeat(24)}`;
const SESSION_A = `s_${"C".repeat(24)}`;
const BANK_DIGEST = "1".repeat(64);
const PROTOCOL_DIGEST = "2".repeat(64);

function draft(planId = PLAN_A) {
  return {
    plan_id: planId,
    patient_id: "P-VISIT-01",
    scheduled_date: "2026-07-19",
    scheduled_time: "09:30:00",
    queue_order: 1,
    session_sitting_no: 1,
    week_no: 2,
    phase_type: "正式训练",
    event_line: "正式训练",
    item_bank_version_id: "wk2-v1-20260707",
    is_simulation: true,
    data_classification: "simulation",
    status: "draft",
    revision: 1,
    created_by: "ACTOR-researcher",
    created_at: "2026-07-19T01:00:00.123456",
    approved_by: null,
    approved_at: null,
    started_by: null,
    started_at: null,
    cancelled_by: null,
    cancelled_at: null,
    session_id: null,
  };
}

function approved(planId = PLAN_A) {
  return {
    ...draft(planId),
    status: "approved",
    revision: 2,
    approved_by: "ACTOR-reviewer",
    approved_at: "2026-07-19T01:01:00",
  };
}

function started() {
  return {
    ...approved(),
    status: "started",
    revision: 3,
    started_by: "ACTOR-researcher",
    started_at: "2026-07-19T01:02:00",
    session_id: SESSION_A,
  };
}

function startedSession() {
  return {
    session_id: SESSION_A,
    patient_id: "P-VISIT-01",
    visit_plan_id: PLAN_A,
    session_sitting_no: 1,
    training_date: "2026-07-19",
    week_no: 2,
    phase_type: "正式训练",
    event_line: "正式训练",
    trainer_id: "ACTOR-researcher",
    item_bank_version_id: "wk2-v1-20260707",
    item_bank_definition_digest: BANK_DIGEST,
    autopilot_protocol_version_id: "autopilot-v1-20260717",
    autopilot_protocol_definition_digest: PROTOCOL_DIGEST,
    is_simulation: true,
    data_classification: "simulation",
  };
}

test("strict receipt parser accepts every coherent VisitPlan state", () => {
  const cancelledDraft = {
    ...draft(), status: "cancelled", revision: 2,
    cancelled_by: "ACTOR-researcher", cancelled_at: "2026-07-19T01:03:00",
  };
  const cancelledApproved = {
    ...approved(), status: "cancelled", revision: 3,
    cancelled_by: "ACTOR-researcher", cancelled_at: "2026-07-19T01:03:00",
  };
  for (const receipt of [draft(), approved(), started(), cancelledDraft, cancelledApproved]) {
    assert.deepEqual(parseVisitPlanReceipt(receipt), receipt);
  }
  assert.equal(startedVisitSessionId(parseVisitPlanReceipt(started())), SESSION_A);
});

test("receipt parser rejects unknown or missing fields and malformed scalar facts", () => {
  const base = draft();
  const { created_at: _removed, ...missing } = base;
  const invalid = [
    { ...base, leaked_name: "某某" },
    missing,
    { ...base, plan_id: "client-plan" },
    { ...base, patient_id: "bad/id" },
    { ...base, scheduled_date: "2026-02-30" },
    { ...base, scheduled_time: "24:00:00" },
    { ...base, scheduled_time: "09:30" },
    { ...base, queue_order: 1.5 },
    { ...base, session_sitting_no: 0 },
    { ...base, week_no: 9 },
    { ...base, phase_type: "随便训练" },
    { ...base, item_bank_version_id: "bad version" },
    { ...base, data_classification: "research" },
    { ...base, status: "running" },
    { ...base, revision: 1.5 },
    { ...base, created_by: "actor\nleak" },
    { ...base, created_by: "   " },
    { ...base, created_by: " padded-actor " },
    { ...base, created_at: "2026-07-19T25:00:00" },
  ];
  for (const value of invalid) assert.throws(() => parseVisitPlanReceipt(value));
  assert.throws(() => parseVisitPlanReceipt(base, { planId: PLAN_B }));
  assert.throws(() => parseVisitPlanReceipt(base, { patientId: "P-VISIT-OTHER" }));
  assert.throws(() => parseVisitPlanReceipt(base, { status: "approved" }));
});

test("status, revision, actor timestamps, and server session id are one coherent fact", () => {
  const invalid = [
    { ...draft(), revision: 2 },
    { ...approved(), approved_at: null },
    { ...approved(), session_id: SESSION_A },
    { ...started(), session_id: null },
    { ...started(), session_id: "S-client-selected" },
    { ...started(), approved_by: null, approved_at: null },
    { ...started(), started_at: "2026-07-19T00:59:00" },
    {
      ...approved(), status: "cancelled", revision: 3,
      cancelled_by: "ACTOR-researcher", cancelled_at: "2026-07-19T01:00:30",
    },
    { ...draft(), cancelled_by: "ACTOR-x", cancelled_at: null },
    { ...draft(), week_no: 1 },
  ];
  for (const value of invalid) assert.throws(() => parseVisitPlanReceipt(value));
  assert.throws(() => startedVisitSessionId(parseVisitPlanReceipt(approved())));
});

test("today parser accepts only due approved unique plans in server queue order", () => {
  const first = {
    ...approved(PLAN_A), scheduled_date: "2026-07-18", scheduled_time: null, queue_order: null,
  };
  const second = {
    ...approved(PLAN_B), scheduled_date: "2026-07-19", scheduled_time: "08:00:00", queue_order: 2,
  };
  const queue = { as_of_date: "2026-07-19", plans: [first, second] };
  assert.deepEqual(parseVisitPlanToday(queue), queue);
  assert.throws(() => parseVisitPlanToday({ ...queue, extra: true }));
  assert.throws(() => parseVisitPlanToday({ ...queue, plans: [second, first] }));
  assert.throws(() => parseVisitPlanToday({ ...queue, plans: [first, first] }));
  assert.throws(() => parseVisitPlanToday({ ...queue, plans: [draft()] }));
  assert.throws(() => parseVisitPlanToday({
    ...queue, plans: [{ ...second, scheduled_date: "2026-07-20" }],
  }));
});

test("patient list parser binds every receipt to the requested patient without caching", () => {
  const rows = [draft(PLAN_A), approved(PLAN_B)];
  assert.deepEqual(parseVisitPlanList(rows, "P-VISIT-01"), rows);
  assert.throws(() => parseVisitPlanList([rows[0], rows[0]], "P-VISIT-01"));
  assert.throws(() => parseVisitPlanList([
    { ...rows[0], patient_id: "P-VISIT-OTHER" },
  ], "P-VISIT-01"));
  assert.throws(() => parseVisitPlanList({}, "P-VISIT-01"));
});

test("start receipt preserves the exact approved snapshot before exposing a session id", () => {
  const approvedReceipt = parseVisitPlanReceipt(approved());
  const startedReceipt = parseVisitPlanReceipt(started());
  assert.equal(reconcileStartedVisitPlan(approvedReceipt, startedReceipt), startedReceipt);
  assert.equal(isSameApprovedVisitPlan(approvedReceipt, parseVisitPlanReceipt(approved())), true);
  assert.equal(isSameApprovedVisitPlan(approvedReceipt, startedReceipt), false);

  for (const changed of [
    { ...started(), patient_id: "P-VISIT-OTHER" },
    { ...started(), week_no: 3 },
    { ...started(), session_sitting_no: 2 },
    { ...started(), item_bank_version_id: "wk2-other" },
    { ...started(), is_simulation: false, data_classification: "research" },
    { ...started(), created_at: "2026-07-19T01:00:01" },
  ]) {
    const parsed = parseVisitPlanReceipt(changed);
    assert.throws(() => reconcileStartedVisitPlan(approvedReceipt, parsed));
  }
});

test("started session parser binds every available server fact to the started receipt", () => {
  const receipt = parseVisitPlanReceipt(started());
  assert.deepEqual(parseStartedVisitSession(startedSession(), receipt), startedSession());
  const invalid = [
    { ...startedSession(), session_id: `s_${"D".repeat(24)}` },
    { ...startedSession(), visit_plan_id: PLAN_B },
    { ...startedSession(), patient_id: "P-VISIT-OTHER" },
    { ...startedSession(), week_no: 3 },
    { ...startedSession(), session_sitting_no: 2 },
    { ...startedSession(), item_bank_version_id: "wk2-other" },
    { ...startedSession(), item_bank_definition_digest: "A".repeat(64) },
    { ...startedSession(), autopilot_protocol_version_id: "bad version" },
    { ...startedSession(), autopilot_protocol_definition_digest: null },
    { ...startedSession(), item_bank_definition_digest: null,
      autopilot_protocol_version_id: null, autopilot_protocol_definition_digest: null },
    { ...startedSession(), is_simulation: false, data_classification: "research" },
    { ...startedSession(), data_classification: "legacy_unknown" },
    { ...startedSession(), trainer_id: "ACTOR-other" },
    { ...startedSession(), training_date: null },
    { ...startedSession(), training_date: "2026-07-18" },
    { ...startedSession(), runtime_status: "active" },
    (() => {
      const { item_bank_definition_digest: _missing, ...missing } = startedSession();
      return missing;
    })(),
    { ...startedSession(), unexpected_definition: "must-fail-closed" },
  ];
  for (const session of invalid) {
    assert.throws(() => parseStartedVisitSession(session, receipt));
  }
});

test("recovery session list is strict, patient-bound, unique, and runtime-authoritative", () => {
  const session = { ...startedSession(), runtime_status: "paused" };
  assert.deepEqual(parsePatientSessionList([session], "P-VISIT-01"), [session]);
  assert.throws(() => parsePatientSessionList([session, session], "P-VISIT-01"));
  assert.throws(() => parsePatientSessionList([
    { ...session, patient_id: "P-VISIT-OTHER" },
  ], "P-VISIT-01"));
  assert.throws(() => parsePatientSessionList([
    { ...session, runtime_status: "mystery" },
  ], "P-VISIT-01"));
  assert.throws(() => parsePatientSessionList([
    { ...session, leaked_answer: "不应出现" },
  ], "P-VISIT-01"));
});

test("command keys retry unknown results but rotate after an accepted receipt", () => {
  let serial = 0;
  const keys = new PendingVisitPlanCommandKeys((scope) => `${scope}-key-${serial += 1}`);
  const fingerprint = "same-protocol-slot";
  const first = keys.acquire("create", fingerprint);
  assert.equal(keys.acquire("create", fingerprint), first, "network-unknown retry must reuse the key");
  assert.equal(keys.settle("create", fingerprint, "different-key"), false);
  assert.equal(keys.acquire("create", fingerprint), first, "a mismatched receipt cannot rotate the key");
  assert.equal(keys.settle("create", fingerprint, first), true);
  const recreated = keys.acquire("create", fingerprint);
  assert.notEqual(recreated, first, "a cancelled slot can be recreated as a new command");
  const rejected = keys.acquire("create", "another-slot");
  assert.equal(rejected, "create-key-3");
  assert.equal(keys.release("create", "another-slot", rejected), true);
  assert.equal(keys.acquire("create", "another-slot"), "create-key-4",
    "a definitive rejection is resolved rather than treated as network-unknown");
});

test("history request gate is latest-wins and invalidates work after unmount", () => {
  const gate = new LatestVisitPlanHistoryRequest();
  const first = gate.begin();
  const second = gate.begin();
  assert.equal(gate.isLatest(first), false);
  assert.equal(gate.isLatest(second), true);
  gate.invalidate();
  assert.equal(gate.isLatest(second), false);
});
