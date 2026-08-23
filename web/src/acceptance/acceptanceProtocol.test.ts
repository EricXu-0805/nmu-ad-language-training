import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import {
  ACCEPTANCE_HEADER_FIELDS,
  ACCEPTANCE_ITEMS,
  ACCEPTANCE_STEP_KEYS,
  buildAcceptanceReceipt,
  emptyDraft,
  failedSteps,
  formatAcceptanceText,
  missingHeaderFields,
  receiptFilename,
  shortBuildId,
  summarizeAcceptance,
  type AcceptanceDraft,
  type MachineFacts,
} from "./acceptanceProtocol.ts";

const MACHINE: MachineFacts = {
  userAgent: "Mozilla/5.0 (iPad; CPU OS 18_5)",
  buildId: "1755100000000",
  screen: "1180×820 @2x",
  finishedAtIso: "2026-08-20T02:30:00.000Z",
};

function filledHeader(draft: AcceptanceDraft): AcceptanceDraft {
  for (const field of ACCEPTANCE_HEADER_FIELDS) draft.header[field.key] = "填了";
  return draft;
}

function allPass(draft: AcceptanceDraft): AcceptanceDraft {
  for (const key of ACCEPTANCE_STEP_KEYS) draft.outcomes[key] = "pass";
  return draft;
}

test("八项与记录表逐条对应，且子场景一个不少", () => {
  assert.equal(ACCEPTANCE_ITEMS.length, 8);
  assert.deepEqual(ACCEPTANCE_ITEMS.map((item) => item.no), [1, 2, 3, 4, 5, 6, 7, 8]);
  // 第 5 项五个失败场景、第 7 项四个中断场景，其余六项各一个——摊平后 15 个可勾项。
  assert.equal(ACCEPTANCE_ITEMS[4]!.steps.length, 5);
  assert.equal(ACCEPTANCE_ITEMS[6]!.steps.length, 4);
  assert.equal(ACCEPTANCE_STEP_KEYS.length, 15);
  assert.equal(new Set(ACCEPTANCE_STEP_KEYS).size, 15, "勾选键不能重复");
});

test("八项标题与 docs 里那份记录表不会各说各话", () => {
  const doc = readFileSync(
    new URL("../../../docs/handover/真机验收记录表.md", import.meta.url), "utf8");
  for (const item of ACCEPTANCE_ITEMS) {
    assert.ok(doc.includes(`### ${item.no}.`), `记录表缺第 ${item.no} 项`);
  }
  assert.equal((doc.match(/^### \d\./gm) ?? []).length, 8);
});

test("空白记录是未完成，而且先报表头没填", () => {
  const draft = emptyDraft();
  assert.equal(summarizeAcceptance(draft), "incomplete-header");
  assert.equal(missingHeaderFields(draft).length, ACCEPTANCE_HEADER_FIELDS.length);
});

test("表头填全但还有没勾的项，仍然是未完成", () => {
  const draft = filledHeader(emptyDraft());
  assert.equal(summarizeAcceptance(draft), "incomplete-steps");
  draft.outcomes["5c"] = "pass";
  assert.equal(summarizeAcceptance(draft), "incomplete-steps");
});

test("勾全了但表头缺一格，绝不因为「都通过了」就放行", () => {
  const draft = allPass(filledHeader(emptyDraft()));
  draft.header.browser = "   ";
  assert.equal(summarizeAcceptance(draft), "incomplete-header");
  assert.deepEqual(missingHeaderFields(draft), ["浏览器与版本"]);
});

test("任何一个子场景未通过，整份记录就是有未通过项", () => {
  const draft = allPass(filledHeader(emptyDraft()));
  draft.outcomes["7c"] = "fail";
  assert.equal(summarizeAcceptance(draft), "recorded-with-failures");
  assert.deepEqual(failedSteps(draft), ["7c"]);
});

test("全部通过也只说「现场记录已齐」，不说这台设备可以用", () => {
  const draft = allPass(filledHeader(emptyDraft()));
  assert.equal(summarizeAcceptance(draft), "recorded-all-pass");
  const receipt = buildAcceptanceReceipt(draft, MACHINE);
  assert.match(receipt.caveat, /不构成任何批准/);
  assert.match(receipt.caveat, /不等于这台设备/);
  assert.match(receipt.caveat, /不得沿用本次结果/);
});

test("屏上显示 8 位短版本号，回执与纯文本保留完整编号", () => {
  // 1755100000000 = 0x198a41caf00，短号取十六进制前 8 位。
  assert.equal(shortBuildId("1755100000000"), "198a41ca");
  const longDecimal = BigInt("0x" + "ab".repeat(32)).toString(10); // 77 位十进制
  assert.equal(shortBuildId(longDecimal), "abababab");
  assert.equal(shortBuildId(longDecimal).length, 8);
  // 短号只改显示：回执和可粘贴纯文本里仍是完整编号。
  const draft = allPass(filledHeader(emptyDraft()));
  const receipt = buildAcceptanceReceipt(draft, MACHINE);
  assert.equal(receipt.machine.buildId, "1755100000000");
  assert.match(formatAcceptanceText(receipt), /页面版本号：1755100000000/);
});

test("回执带上机器说得出的事实，尤其是构建编号", () => {
  const draft = allPass(filledHeader(emptyDraft()));
  const receipt = buildAcceptanceReceipt(draft, MACHINE);
  assert.equal(receipt.schema, "nmu-device-acceptance.v1");
  assert.equal(receipt.machine.buildId, "1755100000000");
  assert.equal(receipt.steps.length, 15);
  assert.deepEqual(receipt.steps.map((step) => step.key), [...ACCEPTANCE_STEP_KEYS]);
});

test("备注跟着勾选一起进回执，两头空格被去掉", () => {
  const draft = allPass(filledHeader(emptyDraft()));
  draft.notes["5d"] = "  断网后 3 秒才关麦  ";
  const receipt = buildAcceptanceReceipt(draft, MACHINE);
  assert.equal(receipt.steps.find((step) => step.key === "5d")!.note,
               "断网后 3 秒才关麦");
});

test("纯文本把未勾的项显式标出来，不留白让人以为通过了", () => {
  const draft = filledHeader(emptyDraft());
  draft.outcomes["1"] = "pass";
  draft.outcomes["2"] = "fail";
  const text = formatAcceptanceText(buildAcceptanceReceipt(draft, MACHINE));
  assert.match(text, /\[1\] ✓ 通过/);
  assert.match(text, /\[2\] ✗ 未通过/);
  assert.match(text, /\[8\] · 未勾/);
  assert.match(text, /未完成：还有没勾的项/);
});

test("文件名带设备与日期，且不含路径分隔符", () => {
  const draft = allPass(filledHeader(emptyDraft()));
  draft.header.device = "iPad/第10代";
  draft.header.date = "2026-08-20";
  const name = receiptFilename(buildAcceptanceReceipt(draft, MACHINE));
  assert.ok(!name.includes("/"), name);
  assert.match(name, /2026-08-20\.json$/);
});
