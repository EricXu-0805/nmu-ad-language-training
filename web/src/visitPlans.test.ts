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
const REPEAT_DIGEST = "3".repeat(64);
const REPEAT_VERSION = "repeat-intent-v1-20260730-proposal";

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
    autopilot_profile_version_id: null,
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
    // This fixture models a D1A modern immediate start, whose contract requires
    // the complete frozen repeat-protocol pair.
    repeat_protocol_version_id: REPEAT_VERSION,
    repeat_protocol_definition_digest: REPEAT_DIGEST,
    // Canonical full-source plan.  Both keys must be present; the strict parser
    // never defaults a missing key to null.
    autopilot_profile_version_id: null,
    autopilot_profile_definition_digest: null,
    is_simulation: true,
    data_classification: "simulation",
  };
}

/** A pre-VisitPlan session: no plan link, and every frozen binding still null. */
function directSession() {
  return {
    ...startedSession(),
    visit_plan_id: null,
    item_bank_definition_digest: null,
    autopilot_protocol_version_id: null,
    autopilot_protocol_definition_digest: null,
    repeat_protocol_version_id: null,
    repeat_protocol_definition_digest: null,
  };
}

// Literal oracles.  Deliberately not imported from the production allowlist:
// a test that shares its constant with the parser proves only self-consistency.
const DEMO_VERSION = "week2-single20-demo-v1";
const DEMO_DIGEST =
  "089c44fc5f20b541b374b24289693e066550acf6999e0b5dc382cd5f10ba71fc";

/** The immutable simulation-only D1B demo lifecycle. */
function demoDraft(planId = PLAN_A) {
  return { ...draft(planId), autopilot_profile_version_id: DEMO_VERSION };
}

function demoApproved(planId = PLAN_A) {
  return { ...approved(planId), autopilot_profile_version_id: DEMO_VERSION };
}

function demoStarted() {
  return { ...started(), autopilot_profile_version_id: DEMO_VERSION };
}

function demoStartedSession() {
  return {
    ...startedSession(),
    autopilot_profile_version_id: DEMO_VERSION,
    autopilot_profile_definition_digest: DEMO_DIGEST,
  };
}

test("exact simulation demo receipt is accepted through its coherent lifecycle", () => {
  const fromDraft = {
    ...demoDraft(), status: "cancelled", revision: 2,
    cancelled_by: "ACTOR-researcher", cancelled_at: "2026-07-19T01:03:00",
  };
  // An approved-to-cancelled row legitimately keeps its approval facts.
  const fromApproved = {
    ...demoDraft(), status: "cancelled", revision: 3,
    approved_by: "ACTOR-reviewer", approved_at: "2026-07-19T01:01:00",
    cancelled_by: "ACTOR-researcher", cancelled_at: "2026-07-19T01:03:00",
  };
  for (const receipt of [
    demoDraft(), demoApproved(), demoStarted(), fromDraft, fromApproved,
  ]) {
    const parsed = parseVisitPlanReceipt(receipt);
    assert.deepEqual(parsed, receipt);
    assert.equal(parsed.autopilot_profile_version_id, DEMO_VERSION);
  }
});

test("profile receipts reject unknown versions, real data, and wrong contexts", () => {
  const invalid = [
    // Missing key, and a leaked digest key, both break the exact key set.
    (() => {
      const { autopilot_profile_version_id: _gone, ...missing } = draft();
      return missing;
    })(),
    { ...draft(), autopilot_profile_definition_digest: DEMO_DIGEST },
    { ...demoDraft(), autopilot_profile_definition_digest: DEMO_DIGEST },
    // Malformed or unknown versions.
    { ...draft(), autopilot_profile_version_id: "" },
    { ...draft(), autopilot_profile_version_id: "   " },
    { ...draft(), autopilot_profile_version_id: ` ${DEMO_VERSION}` },
    { ...draft(), autopilot_profile_version_id: `${DEMO_VERSION} ` },
    { ...draft(), autopilot_profile_version_id: 1 },
    { ...draft(), autopilot_profile_version_id: "week2-single20-demo-v2" },
    // A demo scope is simulation-only.
    { ...demoDraft(), is_simulation: false, data_classification: "research" },
    { ...demoDraft(), week_no: 3 },
    {
      ...demoDraft(), week_no: 1, phase_type: "关系建立",
      event_line: "关系建立环节",
    },
    // A cancelled row may never carry started facts.  Coherent in revision and
    // approval history so the started pair is the only remaining defect.
    {
      ...demoDraft(), status: "cancelled", revision: 3,
      approved_by: "ACTOR-reviewer", approved_at: "2026-07-19T01:01:00",
      started_by: "ACTOR-researcher", started_at: "2026-07-19T01:02:00",
      cancelled_by: "ACTOR-researcher", cancelled_at: "2026-07-19T01:03:00",
    },
  ];
  for (const value of invalid) assert.throws(() => parseVisitPlanReceipt(value));
});

test("profile version is frozen across approve identity and start reconciliation", () => {
  const approvedReceipt = parseVisitPlanReceipt(demoApproved());
  const startedReceipt = parseVisitPlanReceipt(demoStarted());
  assert.equal(reconcileStartedVisitPlan(approvedReceipt, startedReceipt),
    startedReceipt);
  assert.equal(
    isSameApprovedVisitPlan(approvedReceipt, parseVisitPlanReceipt(demoApproved())),
    true);

  assert.throws(() => reconcileStartedVisitPlan(approvedReceipt, {
    ...startedReceipt, autopilot_profile_version_id: null,
  }));
  assert.equal(isSameApprovedVisitPlan(approvedReceipt, {
    ...approvedReceipt, autopilot_profile_version_id: null,
  }), false);
});

test("every paired-null projection keeps both profile keys present and null", () => {
  const receipt = parseVisitPlanReceipt(started());
  const outputs = [
    parseStartedVisitSession(startedSession(), receipt),
    parsePatientSessionList(
      [{ ...startedSession(), runtime_status: "paused" }], "P-VISIT-01")[0],
    parsePatientSessionList(
      [{ ...directSession(), runtime_status: "completed" }], "P-VISIT-01")[0],
  ];
  for (const output of outputs) {
    assert.ok(output);
    assert.equal(Object.hasOwn(output, "autopilot_profile_version_id"), true);
    assert.equal(
      Object.hasOwn(output, "autopilot_profile_definition_digest"), true);
    assert.equal(output.autopilot_profile_version_id, null);
    assert.equal(output.autopilot_profile_definition_digest, null);
  }
});

test("only the exact frozen demo session pair is accepted by the shared parser", () => {
  const receipt = parseVisitPlanReceipt(demoStarted());
  const complete = demoStartedSession();
  const invalid = [
    // Missing keys are never defaulted to null.
    (() => {
      const { autopilot_profile_version_id: _a, ...missing } = startedSession();
      return missing;
    })(),
    (() => {
      const { autopilot_profile_definition_digest: _b, ...missing } = startedSession();
      return missing;
    })(),
    // Both half pairs.
    { ...startedSession(), autopilot_profile_version_id: DEMO_VERSION },
    { ...startedSession(), autopilot_profile_definition_digest: DEMO_DIGEST },
    // Malformed halves.
    { ...complete, autopilot_profile_version_id: "week2-single20-demo-v2" },
    { ...complete, autopilot_profile_version_id: "" },
    { ...complete, autopilot_profile_definition_digest: DEMO_DIGEST.toUpperCase() },
    { ...complete, autopilot_profile_definition_digest: "a".repeat(64) },
    { ...complete, autopilot_profile_definition_digest: "a".repeat(63) },
    { ...complete, autopilot_profile_definition_digest: "not-hex" },
    // A complete pair still needs the plan link, exact context and simulation classification.
    { ...complete, visit_plan_id: null },
    { ...complete, is_simulation: false, data_classification: "research" },
    { ...complete, data_classification: "legacy_unknown" },
    { ...complete, week_no: 3 },
  ];
  for (const session of invalid) {
    assert.throws(() => parseStartedVisitSession(session, receipt));
    assert.throws(() => parsePatientSessionList(
      [{ ...session, runtime_status: "paused" }], "P-VISIT-01"));
  }

  assert.deepEqual(parseStartedVisitSession(complete, receipt), complete);
  assert.deepEqual(parsePatientSessionList(
    [{ ...complete, runtime_status: "paused" }], "P-VISIT-01"),
  [{ ...complete, runtime_status: "paused" }]);
});

test("started session profile version is compared against the started receipt", () => {
  const canonicalReceipt = parseVisitPlanReceipt(started());
  const demoReceipt = parseVisitPlanReceipt(demoStarted());
  assert.deepEqual(parseStartedVisitSession(startedSession(), canonicalReceipt), startedSession());
  assert.deepEqual(parseStartedVisitSession(demoStartedSession(), demoReceipt), demoStartedSession());
  assert.throws(() => parseStartedVisitSession(startedSession(), demoReceipt));
  assert.throws(() => parseStartedVisitSession(demoStartedSession(), canonicalReceipt));
});

test("pre-repeat plan-linked history recovers, but a new start must be complete", () => {
  // The backend admits NULL == NULL repeat bindings so one legacy recovery can
  // finish; recovery/list parsing must not equate a plan link with a repeat
  // pair.  A freshly returned start is still held to the stricter rule.
  const historical = {
    ...startedSession(),
    repeat_protocol_version_id: null,
    repeat_protocol_definition_digest: null,
    runtime_status: "paused",
  };
  assert.deepEqual(
    parsePatientSessionList([historical], "P-VISIT-01"), [historical]);

  const receipt = parseVisitPlanReceipt(started());
  const { runtime_status: _live, ...immediate } = historical;
  assert.throws(() => parseStartedVisitSession(immediate, receipt));
});

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
  const queue = { as_of_date: "2026-07-19", plans: [first, second], withheld_count: 0 };
  assert.deepEqual(parseVisitPlanToday(queue), queue);
  assert.equal(parseVisitPlanToday({ ...queue, withheld_count: 3 }).withheld_count, 3);
  assert.throws(() => parseVisitPlanToday({ ...queue, extra: true }));
  assert.throws(() => parseVisitPlanToday({ ...queue, withheld_count: -1 }));
  assert.throws(() => parseVisitPlanToday({ ...queue, withheld_count: 1.5 }));
  assert.throws(() => parseVisitPlanToday({ as_of_date: "2026-07-19", plans: [first] }));
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
    // This list exercises the same modern immediate-start contract: the repeat
    // pair must be complete. Half a binding is never a legacy shape.
    { ...startedSession(), repeat_protocol_definition_digest: null },
    { ...startedSession(), repeat_protocol_version_id: null },
    { ...startedSession(), repeat_protocol_version_id: null,
      repeat_protocol_definition_digest: null },
    { ...startedSession(), repeat_protocol_version_id: "bad version" },
    { ...startedSession(), repeat_protocol_version_id: "" },
    { ...startedSession(), repeat_protocol_definition_digest: "3".repeat(63) },
    { ...startedSession(), repeat_protocol_definition_digest: "F".repeat(64) },
    { ...startedSession(), repeat_protocol_definition_digest: "   " },
    (() => {
      const { repeat_protocol_version_id: _absent, ...missing } = startedSession();
      return missing;
    })(),
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

test("repeat protocol binding is absent-or-complete on direct legacy sessions", () => {
  // A pre-protocol session legitimately carries neither half.
  assert.deepEqual(
    parsePatientSessionList([{ ...directSession(), runtime_status: "completed" }],
      "P-VISIT-01"),
    [{ ...directSession(), runtime_status: "completed" }]);
  // A direct session that really was frozen under the protocol is also valid.
  const boundDirect = {
    ...directSession(),
    repeat_protocol_version_id: REPEAT_VERSION,
    repeat_protocol_definition_digest: REPEAT_DIGEST,
    runtime_status: "completed",
  };
  assert.deepEqual(parsePatientSessionList([boundDirect], "P-VISIT-01"), [boundDirect]);
  // Half a pair is refused even where the legacy null shape is allowed.
  for (const session of [
    { ...directSession(), repeat_protocol_version_id: REPEAT_VERSION,
      runtime_status: "completed" },
    { ...directSession(), repeat_protocol_definition_digest: REPEAT_DIGEST,
      runtime_status: "completed" },
    { ...directSession(), repeat_protocol_version_id: "bad version",
      repeat_protocol_definition_digest: REPEAT_DIGEST, runtime_status: "completed" },
    { ...directSession(), repeat_protocol_version_id: REPEAT_VERSION,
      repeat_protocol_definition_digest: "not-hex", runtime_status: "completed" },
  ]) {
    assert.throws(() => parsePatientSessionList([session], "P-VISIT-01"));
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
