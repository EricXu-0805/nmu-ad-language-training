import assert from "node:assert/strict";
import test from "node:test";

import {
  demoProfileVersionForVisitPlan,
  operationalAutopilotReadyForSession,
} from "./autopilot/demoProfile.ts";
import { parseAccountSessionPlan } from "./sessionPlan.ts";
import type { Session } from "./types.ts";

const DEMO_VERSION = "week2-single20-demo-v1";
const DEMO_DIGEST =
  "a82bf3910e2e4f0f5a0b78eb3e4c9b8fc4d8a73f16bb570f118f1d5136311f34";
function demoPlan() {
  return {
    item_bank_version_id: "wk2-v1-20260707",
    week_no: 2,
    event_line: "正式训练",
    autopilot_profile_version_id: DEMO_VERSION,
    completion_scope: "demo_plan_only",
    resolved_position_count: 20,
    unsupported_position_count: 0,
    operational_autopilot_ready: true,
    total_items: 20,
    total_turns: 20,
    items: Array.from({ length: 20 }, (_, index) => ({
      item_id: `opaque-item-${String(index + 1).padStart(2, "0")}`,
      task_type: "单要素",
      image_id: `image-${index + 1}`,
      presentation_order: index + 1,
      display: {},
      turns: [{ turn_seq: 1, response_role: "命名", scoring_key: `score-${index + 1}` }],
    })),
  };
}

const DEMO_SESSION: Session = {
  session_id: "s_demo_session",
  patient_id: "P-SIM-01",
  visit_plan_id: "vp_demo_plan",
  session_sitting_no: 1,
  training_date: "2026-08-13",
  week_no: 2,
  phase_type: "正式训练",
  event_line: "正式训练",
  trainer_id: "ACTOR-trainer",
  item_bank_version_id: "wk2-v1-20260707",
  autopilot_profile_version_id: DEMO_VERSION,
  autopilot_profile_definition_digest: DEMO_DIGEST,
  is_simulation: true,
  data_classification: "simulation",
};

test("visit plan profile selection binds only exact simulation Week-2 formal training", () => {
  assert.equal(
    demoProfileVersionForVisitPlan(true, 2, "正式训练", "正式训练"),
    DEMO_VERSION,
  );
  assert.equal(demoProfileVersionForVisitPlan(false, 2, "正式训练", "正式训练"), undefined);
  assert.equal(demoProfileVersionForVisitPlan(true, 3, "正式训练", "正式训练"), undefined);
  assert.equal(demoProfileVersionForVisitPlan(true, 1, "关系建立", "关系建立环节"), undefined);
});

test("exact server demo projection enables its 20 positions despite canonical gaps", () => {
  const parsed = parseAccountSessionPlan(demoPlan());
  assert.equal(operationalAutopilotReadyForSession(DEMO_SESSION, parsed), true);

  const wrongTurnShape = {
    ...parsed,
    items: parsed.items.map((item, index) => index === 0
      ? { ...item, task_type: "双要素" as const }
      : item),
  };
  assert.equal(operationalAutopilotReadyForSession(DEMO_SESSION, wrongTurnShape), false);
  assert.equal(operationalAutopilotReadyForSession({
    ...DEMO_SESSION,
    autopilot_profile_definition_digest: "0".repeat(64),
  }, parsed), false);
});

test("account plan parser rejects drifted demo metadata and unexpected fields", () => {
  for (const invalid of [
    { ...demoPlan(), operational_autopilot_ready: false },
    { ...demoPlan(), unsupported_position_count: 1 },
    { ...demoPlan(), resolved_position_count: 19 },
    { ...demoPlan(), completion_scope: "canonical_full_source" },
    { ...demoPlan(), autopilot_profile_version_id: "week2-single20-demo-v2" },
    { ...demoPlan(), unexpected_field: true },
  ]) {
    assert.throws(() => parseAccountSessionPlan(invalid));
  }
});

test("paired-null plan keeps the canonical whole-source readiness gate", () => {
  const canonicalBase = {
    ...demoPlan(),
    autopilot_profile_version_id: null,
    completion_scope: "canonical_full_source",
    resolved_position_count: 20,
  };
  const blocked = parseAccountSessionPlan({
    ...canonicalBase,
    unsupported_position_count: 60,
    operational_autopilot_ready: false,
  });
  const canonicalSession = {
    ...DEMO_SESSION,
    autopilot_profile_version_id: null,
    autopilot_profile_definition_digest: null,
  };
  assert.equal(operationalAutopilotReadyForSession(canonicalSession, blocked), false);

  const notApplicable = parseAccountSessionPlan({
    ...canonicalBase,
    unsupported_position_count: 0,
    operational_autopilot_ready: false,
  });
  assert.equal(operationalAutopilotReadyForSession(canonicalSession, notApplicable), false);

  const ready = parseAccountSessionPlan({
    ...canonicalBase,
    unsupported_position_count: 0,
    operational_autopilot_ready: true,
  });
  assert.equal(operationalAutopilotReadyForSession(canonicalSession, ready), true);
});
