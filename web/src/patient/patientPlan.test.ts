import assert from "node:assert/strict";
import test from "node:test";
import { parsePatientSessionPlan } from "./patientPlan.ts";

const PLAN = {
  item_bank_version_id: "bank-v1",
  week_no: 2,
  event_line: "正式训练",
  total_items: 1,
  total_turns: 1,
  items: [{
    item_ref: "itm-0001",
    task_type: "单要素",
    presentation_order: 1,
    turns: [{ turn_seq: 1, response_role: "命名" }],
  }],
};

test("patient plan accepts only opaque position references", () => {
  const parsed = parsePatientSessionPlan(PLAN);
  assert.equal(parsed.items[0]?.item_ref, "itm-0001");
  assert.deepEqual(parsed.items[0]?.turns[0], { turn_seq: 1, response_role: "命名" });
});

test("patient plan rejects canonical ids, scoring keys and position substitutions", () => {
  for (const unsafe of [
    { ...PLAN, items: [{ ...PLAN.items[0], image_id: "wk2-01" }] },
    { ...PLAN, items: [{ ...PLAN.items[0], item_id: "SE_胡萝卜" }] },
    { ...PLAN, items: [{ ...PLAN.items[0], display: { target_word: "胡萝卜" } }] },
    { ...PLAN, items: [{ ...PLAN.items[0], turns: [{ ...PLAN.items[0].turns[0], scoring_key: "final_correct" }] }] },
    { ...PLAN, items: [{ ...PLAN.items[0], item_ref: "itm-0002" }] },
    { ...PLAN, total_turns: 2 },
  ]) {
    assert.throws(() => parsePatientSessionPlan(unsafe));
  }
});
