import assert from "node:assert/strict";
import test from "node:test";
import {
  evaluateResearchReviewGate,
  planDisplayFacts,
  resolveResearchScoringContract,
} from "./researchReviewGate.ts";

const VALID_GATE_INPUT = {
  taskType: "单要素",
  scoringKey: "final_correct",
  responseRole: "命名",
  confirmedText: "雨伞",
  reviewerId: "REVIEWER-01",
  promptLevel: 0,
  rawAudioId: "audio-synthetic-01",
  audioStatus: "ready" as const,
  selectedScore: "1",
};

test("known task_type + scoring_key + role contract enables the frozen score options", () => {
  const contract = resolveResearchScoringContract("单要素", "final_correct", "命名");
  assert.equal(contract.ok, true);
  if (!contract.ok) return;
  assert.equal(contract.contract.id, "single.final_correct.v1");
  assert.deepEqual(contract.contract.options.map((option) => option.value), ["1", "0"]);

  const gate = evaluateResearchReviewGate(VALID_GATE_INPUT);
  assert.equal(gate.canSaveConfirmation, true);
  assert.equal(gate.canLockScore, true);
  assert.deepEqual(gate.blockers, []);
});

test("relation role alone cannot manufacture the three-value scoring enum", () => {
  const contract = resolveResearchScoringContract("双要素", "unknown_relation_key", "关系识别");
  assert.equal(contract.ok, false);

  const gate = evaluateResearchReviewGate({
    ...VALID_GATE_INPUT,
    taskType: "双要素",
    scoringKey: "unknown_relation_key",
    responseRole: "关系识别",
    selectedScore: "0.5",
  });
  assert.equal(gate.canSaveConfirmation, false);
  assert.equal(gate.canLockScore, false);
  assert.equal(gate.blockers.some((blocker) => blocker.code === "unknown_scoring_contract"), true);
});

test("the exact double-element relation contract allows 0.5", () => {
  const gate = evaluateResearchReviewGate({
    ...VALID_GATE_INPUT,
    taskType: "双要素",
    scoringKey: "relation",
    responseRole: "关系识别",
    selectedScore: "0.5",
  });
  assert.equal(gate.canLockScore, true);
});

test("missing or failed authenticated audio blocks lock even with a known contract", () => {
  const missing = evaluateResearchReviewGate({
    ...VALID_GATE_INPUT,
    rawAudioId: null,
    audioStatus: "missing",
  });
  assert.equal(missing.canSaveConfirmation, true);
  assert.equal(missing.canLockScore, false);
  assert.equal(missing.blockers.some((blocker) => blocker.code === "missing_audio_reference"), true);

  const failed = evaluateResearchReviewGate({
    ...VALID_GATE_INPUT,
    audioStatus: "error",
  });
  assert.equal(failed.canLockScore, false);
  assert.equal(failed.blockers.some((blocker) => blocker.code === "audio_load_failed"), true);
});

test("multi-element dynamic keys remain closed until a verifiable contract is added", () => {
  const gate = evaluateResearchReviewGate({
    ...VALID_GATE_INPUT,
    taskType: "多要素",
    scoringKey: "dynamic-key-from-plan",
    responseRole: "拿起水杯",
  });
  assert.equal(gate.canSaveConfirmation, false);
  assert.equal(gate.canLockScore, false);
  assert.equal(gate.blockers[0]?.code, "unknown_scoring_contract");
});

test("plan display facts contain only values actually present in plan.display", () => {
  assert.deepEqual(planDisplayFacts({
    target_word: "雨伞",
    cues: { "1": "挡雨用的", "2": null },
    optional: [],
  }), [
    { path: "target_word", value: "雨伞" },
    { path: "cues.1", value: "挡雨用的" },
    { path: "cues.2", value: "null" },
    { path: "optional", value: "[]" },
  ]);
});
