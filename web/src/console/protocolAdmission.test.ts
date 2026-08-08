import assert from "node:assert/strict";
import test from "node:test";
import type { Session } from "../types.ts";
import {
  assessVisitPlanProtocol,
  bedsideProtocolRoute,
  type TrainingContentStatus,
} from "./protocolAdmission.ts";

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

const WEEK2_ONLY: TrainingContentStatus = {
  structuredWeeks: [2],
  researchReadyWeeks: [],
};
const WEEK23_RESEARCH: TrainingContentStatus = {
  structuredWeeks: [2, 3],
  researchReadyWeeks: [2, 3],
};

test("structured weeks admit simulation; research follows per-week readiness", () => {
  assert.deepEqual(
    assessVisitPlanProtocol(2, "正式训练", "正式训练", true, WEEK2_ONLY),
    { allowed: true, draftAllowed: true, reason: null },
  );
  const research = assessVisitPlanProtocol(2, "正式训练", "正式训练", false, WEEK2_ONLY);
  assert.equal(research.allowed, false);
  assert.match(research.reason ?? "", /ready_for_research=false/);
  assert.deepEqual(
    assessVisitPlanProtocol(3, "正式训练", "正式训练", false, WEEK23_RESEARCH),
    { allowed: true, draftAllowed: true, reason: null },
  );
});

test("missing signals fail closed for both partitions", () => {
  const checkingContent = assessVisitPlanProtocol(2, "正式训练", "正式训练", false, null);
  assert.equal(checkingContent.allowed, false);
  assert.match(checkingContent.reason ?? "", /核对/);
  const checkingPartition = assessVisitPlanProtocol(2, "正式训练", "正式训练", null, WEEK2_ONLY);
  assert.equal(checkingPartition.allowed, false);
  assert.match(checkingPartition.reason ?? "", /研究\/模拟分区/);
  // 模拟路径同样必须等到逐周内容信号:未核对时不得放行任何周。
  assert.equal(assessVisitPlanProtocol(2, "正式训练", "正式训练", true, null).allowed, false);
});

test("unstructured training weeks disclose the content blocker for every partition", () => {
  const weekThree = assessVisitPlanProtocol(3, "正式训练", "正式训练", true, WEEK2_ONLY);
  assert.equal(weekThree.allowed, false);
  assert.match(weekThree.reason ?? "", /第3周材料尚未结构化/);
  const weekEight = assessVisitPlanProtocol(8, "正式训练", "正式训练", false, WEEK2_ONLY);
  assert.equal(weekEight.allowed, false);
  assert.match(weekEight.reason ?? "", /第8周材料尚未结构化/);
});

test("Week-1 rapport is admitted; research anchors the default bank readiness", () => {
  assert.deepEqual(
    assessVisitPlanProtocol(1, "关系建立", "关系建立环节", true, WEEK2_ONLY),
    { allowed: true, draftAllowed: true, reason: null },
  );
  const research = assessVisitPlanProtocol(1, "关系建立", "关系建立环节", false, WEEK2_ONLY);
  assert.equal(research.allowed, false);
  assert.match(research.reason ?? "", /ready_for_research=false/);
  assert.deepEqual(
    assessVisitPlanProtocol(1, "关系建立", "关系建立环节", false, WEEK23_RESEARCH),
    { allowed: true, draftAllowed: true, reason: null },
  );
});

test("draft mirror follows the server create/approve split", () => {
  // 未结构化周/信号未核对/基线组合:可留草稿、不可审核(服务端 create 只验蓝图)。
  for (const decision of [
    assessVisitPlanProtocol(4, "正式训练", "正式训练", true, WEEK2_ONLY),
    assessVisitPlanProtocol(2, "正式训练", "正式训练", true, null),
    assessVisitPlanProtocol(2, "正式训练", "正式训练", null, WEEK2_ONLY),
    assessVisitPlanProtocol(1, "基线测评", "基线测评窗", true, WEEK2_ONLY),
    assessVisitPlanProtocol(1, "关系建立", "关系建立环节", false, WEEK2_ONLY),
  ]) {
    assert.equal(decision.allowed, false);
    assert.equal(decision.draftAllowed, true);
  }
  // 蓝图外组合连草稿都不留。
  const invalid = assessVisitPlanProtocol(1, "关系建立", "基线测评窗", true, WEEK23_RESEARCH);
  assert.equal(invalid.allowed, false);
  assert.equal(invalid.draftAllowed, false);
});

test("Week-1 baseline routes to the formal assessment domain permanently", () => {
  const baseline = assessVisitPlanProtocol(1, "基线测评", "基线测评窗", true, WEEK23_RESEARCH);
  const pretest = assessVisitPlanProtocol(1, "前测", "基线测评窗", false, WEEK23_RESEARCH);
  assert.equal(baseline.allowed, false);
  assert.match(baseline.reason ?? "", /正式量表评估事件工作流/);
  assert.equal(pretest.allowed, false);
  assert.match(pretest.reason ?? "", /前测/);
});

test("bedside route trusts server admission for partition on existing sessions", () => {
  assert.equal(bedsideProtocolRoute(session()).screen, "training");
  // 真实场次能存在,说明 approve/start 已通过服务端题库+分区门禁;
  // 床旁路由只判断该协议组合有没有已实现的界面,不再重复分区/内容判定。
  assert.equal(bedsideProtocolRoute(session({ is_simulation: false })).screen, "training");
  assert.equal(bedsideProtocolRoute(session({ week_no: 5 })).screen, "training");
  assert.equal(bedsideProtocolRoute(session({
    week_no: 1,
    phase_type: "关系建立",
    event_line: "关系建立环节",
  })).screen, "relationship");
  const baseline = bedsideProtocolRoute(session({
    week_no: 1,
    phase_type: "基线测评",
    event_line: "基线测评窗",
  }));
  assert.equal(baseline.screen, "unsupported");
  assert.match(baseline.reason ?? "", /正式量表评估事件工作流/);

  const mismatched = bedsideProtocolRoute(session({
    week_no: 1,
    phase_type: "关系建立",
    event_line: "基线测评窗",
  }));
  assert.equal(mismatched.screen, "unsupported");
  assert.match(mismatched.reason ?? "", /周次、阶段与任务线/);
});
