import assert from "node:assert/strict";
import test from "node:test";
import {
  parsePatientPresentation,
  presentationMatches,
  type PatientRapportPresentation,
  type PatientTaskPresentation,
  type TaskPresentationExpectation,
} from "./presentationContent.ts";

const TASK: PatientTaskPresentation = {
  schema_version: 1,
  mode: "task",
  session_id: "S-CURRENT",
  item_bank_version_id: "bank-v1",
  item_idx: 2,
  turn_idx: 0,
  item_ref: "itm-0003",
  turn_seq: 1,
  response_role: "命名",
  cue_level: 1,
  cue_text: "这是当前已下发的第一级线索。",
  feedback_key: "cued1_close",
  feedback_item_ref: "itm-0003",
  feedback_seq: 7,
  feedback_text: "这是当前已下发的反馈。",
  wseq: 104,
};

const EXPECTED: TaskPresentationExpectation = {
  mode: "task",
  sessionId: "S-CURRENT",
  itemBankVersionId: "bank-v1",
  itemIdx: 2,
  turnIdx: 0,
  itemRef: "itm-0003",
  turnSeq: 1,
  responseRole: "命名",
  cueLevel: 1,
  feedbackKey: "cued1_close",
  feedbackItemRef: "itm-0003",
  feedbackSeq: 7,
  wseq: 104,
};

test("task projection accepts only the exact current cursor identity", () => {
  const parsed = parsePatientPresentation(TASK);
  assert.equal(parsed.mode, "task");
  assert.equal(presentationMatches(parsed, EXPECTED), true);

  for (const changed of [
    { ...EXPECTED, sessionId: "S-FOREIGN" },
    { ...EXPECTED, itemBankVersionId: "bank-v2" },
    { ...EXPECTED, itemIdx: 3 },
    { ...EXPECTED, turnIdx: 1 },
    { ...EXPECTED, cueLevel: 2 as const },
    { ...EXPECTED, feedbackSeq: 8 },
    { ...EXPECTED, wseq: 105 },
  ]) {
    assert.equal(presentationMatches(parsed, changed), false);
  }
});

test("strict parser rejects an accidental full-answer payload or partial feedback pointer", () => {
  for (const unsafe of [
    { ...TASK, target_word: "不应下发的答案" },
    { ...TASK, cues: { 2: "未到达的后续线索" } },
    { ...TASK, tell_answer: "未到达的告知答案" },
    { ...TASK, feedback_seq: null },
    { ...TASK, feedback_key: null },
    { ...TASK, feedback_item_ref: null },
    { ...TASK, item_id: "SE_不应泄露" },
    { ...TASK, cue_level: 4 },
  ]) {
    assert.throws(() => parsePatientPresentation(unsafe));
  }
});

test("rapport projection contains and matches only the current section/question", () => {
  const rapport: PatientRapportPresentation = {
    schema_version: 1,
    mode: "rapport",
    session_id: "S-RAPPORT",
    script_version_id: "script-v1",
    section_key: "自我介绍",
    question_idx: 2,
    speaker: "机器人",
    text: "您属什么呀？",
    wseq: 55,
  };
  const parsed = parsePatientPresentation(rapport);
  assert.equal(presentationMatches(parsed, {
    mode: "rapport",
    sessionId: "S-RAPPORT",
    sectionKey: "自我介绍",
    questionIdx: 2,
    wseq: 55,
  }), true);
  assert.equal(presentationMatches(parsed, {
    mode: "rapport",
    sessionId: "S-RAPPORT",
    sectionKey: "介绍机构环境",
    questionIdx: 0,
    wseq: 55,
  }), false);
  assert.throws(() => parsePatientPresentation({
    ...rapport,
    sections: [{ key: "道别", line: "不应同时下发的后续话术" }],
  }));
  assert.throws(() => parsePatientPresentation({
    ...rapport,
    speaker: "未知角色",
    text: null,
  }));
});
