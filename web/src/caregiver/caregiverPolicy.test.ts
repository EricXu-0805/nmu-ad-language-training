import assert from "node:assert/strict";
import test from "node:test";
import {
  CAREGIVER_END_REASONS,
  CAREGIVER_DEMO_BOUNDARY_MESSAGE,
  CAREGIVER_HELP_REASONS,
  CAREGIVER_VISIBLE_ACTION_LABELS,
  caregiverActionAvailability,
  caregiverStatusPresentation,
  identityIsCaregiverOperator,
  makeCaregiverEndRequest,
  type CaregiverSessionStatus,
} from "./caregiverPolicy.ts";

function status(overrides: Partial<CaregiverSessionStatus> = {}): CaregiverSessionStatus {
  return {
    sessionId: "S-CG-1",
    runtimeState: "active",
    practiceState: "not_started",
    patientPresence: "online",
    runtimeRevision: 5,
    practiceRevision: 3,
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
    ...overrides,
  };
}

test("the caregiver role is exact and does not absorb neighboring account roles", () => {
  assert.equal(identityIsCaregiverOperator("caregiver_operator"), true);
  assert.equal(identityIsCaregiverOperator("researcher"), false);
  assert.equal(identityIsCaregiverOperator("admin"), false);
  assert.equal(identityIsCaregiverOperator("data_steward"), false);
  assert.equal(identityIsCaregiverOperator(null), false);
});

test("visible actions remain the closed bedside-only set", () => {
  assert.deepEqual(CAREGIVER_VISIBLE_ACTION_LABELS, [
    "今天任务",
    "开始本次",
    "开始练习",
    "暂停练习",
    "请求协助",
    "接管练习",
    "结束本次",
    "退出",
  ]);
  assert.equal(CAREGIVER_VISIBLE_ACTION_LABELS.some((label) => /resume|complete|恢复|完成归档/i.test(label)), false);
});

test("the local synthetic demo boundary is explicit and stable", () => {
  assert.equal(
    CAREGIVER_DEMO_BOUNDARY_MESSAGE,
    "仅本机20题合成模拟，不得用于真实老人、养老院、正式研究，20题不等于80",
  );
});

test("start practice fails closed and can never become a restart after pause", () => {
  assert.equal(caregiverActionAvailability(status()).startPractice, true);
  assert.equal(caregiverActionAvailability(status({ patientPresence: "offline" })).startPractice, false);
  assert.equal(caregiverActionAvailability(status({ practiceState: "running" })).startPractice, false);
  const historical = status({
    isSimulation: false,
    dataClassification: "research",
    autopilotProfileVersionId: null,
    completionScope: null,
    resolvedPositionCount: null,
    operationalDemoReady: false,
  });
  assert.deepEqual(caregiverActionAvailability(historical), {
    startPractice: false,
    pause: true,
    help: true,
    takeOver: false,
    end: true,
  });
  assert.deepEqual(caregiverStatusPresentation(historical), {
    title: "仅可安全收口",
    detail: "该场次未通过本机20题合成模拟门禁，不会显示开始练习。",
    tone: "warn",
  });
  assert.equal(caregiverActionAvailability(status({
    runtimeState: "paused",
    practiceState: "paused",
    allowed: {
      startPractice: true,
      pause: false,
      help: true,
      takeOver: true,
      end: true,
    },
  })).startPractice, false);
});

test("take-over appears only after the exact paused state and terminal states expose no actions", () => {
  const paused = status({
    runtimeState: "paused",
    practiceState: "paused",
    takeoverReady: true,
    allowed: {
      startPractice: false,
      pause: false,
      help: true,
      takeOver: true,
      end: true,
    },
  });
  assert.deepEqual(caregiverActionAvailability(paused), {
    startPractice: false,
    pause: false,
    help: true,
    takeOver: true,
    end: true,
  });
  assert.deepEqual(caregiverActionAvailability(status({
    runtimeState: "paused",
    practiceState: "paused",
    takeoverReady: false,
    allowed: {
      startPractice: false,
      pause: false,
      help: true,
      takeOver: true,
      end: true,
    },
  })), {
    startPractice: false,
    pause: false,
    help: true,
    takeOver: false,
    end: true,
  });
  assert.deepEqual(caregiverStatusPresentation(status({
    runtimeState: "paused",
    practiceState: "paused",
    takeoverReady: false,
  })), {
    title: "练习已暂停",
    detail: "设备正在安全收尾，请稍候",
    tone: "warn",
  });
  assert.deepEqual(caregiverActionAvailability(status({ runtimeState: "intervention_completed" })), {
    startPractice: false,
    pause: false,
    help: false,
    takeOver: false,
    end: false,
  });
  assert.deepEqual(caregiverActionAvailability(status({ runtimeState: "completed" })), {
    startPractice: false,
    pause: false,
    help: false,
    takeOver: false,
    end: false,
  });
});

test("help and early-end reasons are closed lists without free text", () => {
  assert.deepEqual(CAREGIVER_HELP_REASONS.map((reason) => reason.code), [
    "participant_distress",
    "participant_request",
    "clinical_concern",
    "technical_failure",
    "other_staff_needed",
  ]);
  assert.deepEqual(CAREGIVER_END_REASONS.map((reason) => reason.code), [
    "finish",
    "participant_declined",
    "clinical_safety",
    "technical_failure",
  ]);
});

test("end request maps only to finish or the three approved abort reasons", () => {
  const current = status({ runtimeRevision: 12 });
  assert.deepEqual(makeCaregiverEndRequest("finish", current, "unused"), { kind: "finish" });
  assert.deepEqual(makeCaregiverEndRequest("participant_declined", current, "cg-request-1"), {
    kind: "abort",
    reasonCode: "participant_declined",
    expectedRevision: 12,
    idempotencyKey: "cg-request-1",
  });
  assert.deepEqual(makeCaregiverEndRequest("clinical_safety", current, "cg-request-2"), {
    kind: "abort",
    reasonCode: "clinical_safety",
    expectedRevision: 12,
    idempotencyKey: "cg-request-2",
  });
  assert.deepEqual(makeCaregiverEndRequest("technical_failure", current, "cg-request-3"), {
    kind: "abort",
    reasonCode: "technical_failure",
    expectedRevision: 12,
    idempotencyKey: "cg-request-3",
  });
});
