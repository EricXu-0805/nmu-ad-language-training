import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { caregiverApiContract } from "./caregiverApi.ts";

const STATUS = {
  session_id: "S-CG-1",
  participant_code: "P-CG-1",
  session_sitting_no: 1,
  week_no: 2,
  phase_type: "正式训练",
  event_line: "正式训练",
  runtime_status: "active",
  runtime_revision: 4,
  is_simulation: true,
  data_classification: "simulation",
  autopilot_profile_version_id: "week2-single20-demo-v1",
  completion_scope: "demo_plan_only",
  resolved_position_count: 20,
  operational_demo_ready: true,
  active_bedside_session: true,
  patient_presence: {
    online: true,
    screen: "ready",
    last_seen_at: "2026-08-13T10:00:00",
  },
  autopilot: {
    scope_key: "disabled",
    mode: "disabled",
    status: "idle",
    state_revision: 0,
    server_owned: false,
    takeover_ready: false,
    current_command_kind: null,
    last_error_code: null,
  },
};

test("today parser projects only bedside labels and current session", () => {
  const parsed = caregiverApiContract.parseToday({
    as_of_date: "2026-08-13",
    withheld_count: 2,
    plans: [{
      plan_id: "VP-1",
      participant_code: "P-1",
      scheduled_date: "2026-08-13",
      scheduled_time: "09:30:00",
      queue_order: 1,
      session_sitting_no: 1,
      week_no: 2,
      phase_type: "正式训练",
      event_line: "正式训练",
      revision: 2,
      is_simulation: true,
      data_classification: "simulation",
      autopilot_profile_version_id: "week2-single20-demo-v1",
      completion_scope: "demo_plan_only",
      resolved_position_count: 20,
      operational_demo_ready: true,
    }],
    current_session: null,
  });
  assert.deepEqual(parsed, {
    asOfDateLabel: "2026-08-13",
    plans: [{
      planId: "VP-1",
      participantLabel: "P-1",
      scheduledDate: "2026-08-13",
      scheduledTime: "09:30:00",
      weekLabel: "第 2 周",
      phaseLabel: "正式训练",
      revision: 2,
      isSimulation: true,
      dataClassification: "simulation",
      autopilotProfileVersionId: "week2-single20-demo-v1",
      completionScope: "demo_plan_only",
      resolvedPositionCount: 20,
      operationalDemoReady: true,
    }],
    withheldCount: 2,
    currentSession: null,
  });
});

test("today parser fails closed unless every visible plan is the exact local 20-item demo", () => {
  const plan = {
    plan_id: "VP-1",
    participant_code: "P-1",
    scheduled_date: "2026-08-13",
    scheduled_time: null,
    week_no: 2,
    phase_type: "正式训练",
    revision: 2,
    is_simulation: true,
    data_classification: "simulation",
    autopilot_profile_version_id: "week2-single20-demo-v1",
    completion_scope: "demo_plan_only",
    resolved_position_count: 20,
    operational_demo_ready: true,
  };
  const today = (visiblePlan: Record<string, unknown>) => ({
    as_of_date: "2026-08-13",
    withheld_count: 0,
    plans: [visiblePlan],
    current_session: null,
  });
  assert.doesNotThrow(() => caregiverApiContract.parseToday(today(plan)));
  for (const drifted of [
    { ...plan, is_simulation: false },
    { ...plan, data_classification: "research" },
    { ...plan, autopilot_profile_version_id: null },
    { ...plan, autopilot_profile_version_id: "week2-single20-demo-v2" },
    { ...plan, completion_scope: "canonical_full_source" },
    { ...plan, resolved_position_count: 19 },
    { ...plan, operational_demo_ready: false },
  ]) {
    assert.throws(
      () => caregiverApiContract.parseToday(today(drifted)),
      /本机20题合成模拟门禁/,
    );
  }
  const { autopilot_profile_version_id: _profile, ...missingProfile } = plan;
  assert.throws(() => caregiverApiContract.parseToday(today(missingProfile)));
  assert.throws(() => caregiverApiContract.parseToday({ ...today(plan), withheld_count: -1 }));
});

test("today parser keeps a historical current session for safe closeout", () => {
  const parsed = caregiverApiContract.parseToday({
    as_of_date: "2026-08-13",
    withheld_count: 1,
    plans: [],
    current_session: {
      session_id: "S-HISTORY-1",
      participant_code: "P-HISTORY-1",
      week_no: 1,
      phase_type: "正式训练",
      is_simulation: false,
      data_classification: "research",
      autopilot_profile_version_id: null,
      completion_scope: null,
      resolved_position_count: null,
      operational_demo_ready: false,
    },
  });
  assert.deepEqual(parsed.currentSession, {
    sessionId: "S-HISTORY-1",
    participantLabel: "P-HISTORY-1",
    weekLabel: "第 1 周",
    phaseLabel: "正式训练",
    isSimulation: false,
    dataClassification: "research",
    autopilotProfileVersionId: null,
    completionScope: null,
    resolvedPositionCount: null,
    operationalDemoReady: false,
  });
});

test("status parser derives the exact safe bedside actions", () => {
  assert.deepEqual(caregiverApiContract.parseStatus(STATUS), {
    sessionId: "S-CG-1",
    runtimeState: "active",
    practiceState: "not_started",
    patientPresence: "online",
    runtimeRevision: 4,
    practiceRevision: 0,
    takeoverReady: false,
    isSimulation: true,
    dataClassification: "simulation",
    autopilotProfileVersionId: "week2-single20-demo-v1",
    completionScope: "demo_plan_only",
    resolvedPositionCount: 20,
    operationalDemoReady: true,
    allowed: {
      startPractice: true,
      pause: true,
      help: true,
      takeOver: false,
      end: true,
    },
  });

  const paused = caregiverApiContract.parseStatus({
    ...STATUS,
    runtime_status: "paused",
    runtime_revision: 5,
    autopilot: {
      ...STATUS.autopilot,
      scope_key: "p0a_sim_first_single_v1",
      mode: "autonomous",
      status: "paused",
      state_revision: 7,
      server_owned: true,
      takeover_ready: false,
    },
  });
  assert.equal(paused.allowed.startPractice, false);
  assert.equal(paused.allowed.pause, false);
  assert.equal(paused.allowed.help, true);
  assert.equal(paused.takeoverReady, false);
  assert.equal(paused.allowed.takeOver, false);
  assert.equal(paused.allowed.end, true);

  const drained = caregiverApiContract.parseStatus({
    ...STATUS,
    runtime_status: "paused",
    runtime_revision: 5,
    autopilot: {
      ...STATUS.autopilot,
      scope_key: "p0a_sim_first_single_v1",
      mode: "autonomous",
      status: "paused",
      state_revision: 8,
      server_owned: true,
      takeover_ready: true,
    },
  });
  assert.equal(drained.takeoverReady, true);
  assert.equal(drained.allowed.pause, false);
  assert.equal(drained.allowed.help, true);
  assert.equal(drained.allowed.takeOver, true);
  assert.equal(drained.allowed.end, true);

  const historical = caregiverApiContract.parseStatus({
    ...STATUS,
    is_simulation: false,
    data_classification: "research",
    autopilot_profile_version_id: null,
    completion_scope: null,
    resolved_position_count: null,
    operational_demo_ready: false,
  });
  assert.equal(historical.operationalDemoReady, false);
  assert.equal(historical.allowed.startPractice, false);
  assert.equal(historical.allowed.pause, true);
  assert.equal(historical.allowed.help, true);
  assert.equal(historical.allowed.end, true);

  const completed = caregiverApiContract.parseStatus({
    ...STATUS,
    runtime_status: "completed",
  });
  assert.deepEqual(completed.allowed, {
    startPractice: false,
    pause: false,
    help: false,
    takeOver: false,
    end: false,
  });
});

test("status parser fails closed on unknown server states", () => {
  assert.throws(() => caregiverApiContract.parseStatus({
    ...STATUS,
    runtime_status: "resumed",
  }), /场次运行状态/);
  assert.throws(() => caregiverApiContract.parseStatus({
    ...STATUS,
    autopilot: { ...STATUS.autopilot, status: "invented" },
  }), /自动练习状态/);
  assert.throws(() => caregiverApiContract.parseStatus({
    ...STATUS,
    autopilot: { ...STATUS.autopilot, takeover_ready: true },
  }), /自动练习状态/);
  const { takeover_ready: _missingTakeoverReady, ...missingTakeoverReady } = STATUS.autopilot;
  assert.throws(() => caregiverApiContract.parseStatus({
    ...STATUS,
    autopilot: missingTakeoverReady,
  }), /自动练习状态/);
  assert.throws(() => caregiverApiContract.parseStatus({
    ...STATUS,
    autopilot: { ...STATUS.autopilot, extra: "not allowed" },
  }), /自动练习状态/);
  const { operational_demo_ready: _missing, ...missingReady } = STATUS;
  assert.throws(() => caregiverApiContract.parseStatus(missingReady), /operational_demo_ready/);
  assert.throws(() => caregiverApiContract.parseStatus({
    ...STATUS,
    resolved_position_count: -1,
  }), /resolved_position_count/);
  assert.throws(() => caregiverApiContract.parseStatus({
    ...STATUS,
    data_classification: "legacy_unknown",
  }), /data_classification/);
  assert.throws(() => caregiverApiContract.parseStatus({
    ...STATUS,
    completion_scope: "unknown_scope",
  }), /completion_scope/);
});

test("adapter source contains only the approved mutation paths", () => {
  const source = readFileSync(new URL("./caregiverApi.ts", import.meta.url), "utf8");
  for (const required of [
    "/caregiver/today",
    "/caregiver/visit-plans/",
    "/activation",
    "/status",
    "/help-requests",
    "/autopilot/start",
    "/autopilot/takeover",
    "/pause",
    "/finish-intervention",
    "/abort",
  ]) assert.match(source, new RegExp(required.replaceAll("/", "\\/")));
  for (const forbidden of [
    "/resume", "/complete", "/closeout", "/content/", "/score/", "/export",
    "/audio", "/runtime/cursor", "/patient-pause",
  ]) assert.doesNotMatch(source, new RegExp(forbidden.replaceAll("/", "\\/")));
});
