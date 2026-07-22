import assert from "node:assert/strict";
import test from "node:test";
import type { Session } from "../types.ts";
import { assessVisitPlanProtocol, bedsideProtocolRoute } from "./protocolAdmission.ts";

function session(overrides: Partial<Session> = {}): Session {
  return {
    session_id: "S-PROTOCOL",
    patient_id: "P-PROTOCOL",
    is_simulation: true,
    data_classification: "simulation",
    week_no: 2,
    phase_type: "正式训练",
    event_line: "正式训练",
    item_bank_version_id: "wk2-v1",
    runtime_status: "active",
    ...overrides,
  };
}

test("only the exact Week-2 simulation vertical is admitted in P0", () => {
  assert.deepEqual(
    assessVisitPlanProtocol(2, "正式训练", "正式训练", true),
    { allowed: true, reason: null },
  );
  const research = assessVisitPlanProtocol(2, "正式训练", "正式训练", false);
  assert.equal(research.allowed, false);
  assert.match(research.reason ?? "", /ready_for_research=false/);
});

test("Week-1 phases and Week 3-8 disclose their distinct missing contracts", () => {
  const rapport = assessVisitPlanProtocol(1, "关系建立", "关系建立环节", true);
  const baseline = assessVisitPlanProtocol(1, "基线测评", "基线测评窗", true);
  const pretest = assessVisitPlanProtocol(1, "前测", "基线测评窗", true);
  const weekThree = assessVisitPlanProtocol(3, "正式训练", "正式训练", true);
  assert.match(rapport.reason ?? "", /关系建立/);
  assert.match(baseline.reason ?? "", /基线测评/);
  assert.match(pretest.reason ?? "", /前测/);
  assert.match(weekThree.reason ?? "", /冻结训练计划/);
});

test("bedside route uses week, phase, event line, and data boundary together", () => {
  assert.equal(bedsideProtocolRoute(session()).screen, "training");
  assert.equal(bedsideProtocolRoute(session({ is_simulation: false })).screen, "unsupported");
  const baseline = bedsideProtocolRoute(session({
    week_no: 1,
    phase_type: "基线测评",
    event_line: "基线测评窗",
  }));
  assert.equal(baseline.screen, "unsupported");
  assert.match(baseline.reason ?? "", /不能进入关系建立页/);

  const mismatched = bedsideProtocolRoute(session({
    week_no: 1,
    phase_type: "关系建立",
    event_line: "基线测评窗",
  }));
  assert.equal(mismatched.screen, "unsupported");
  assert.match(mismatched.reason ?? "", /周次、阶段与任务线/);
});
