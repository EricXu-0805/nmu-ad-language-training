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

const DOC = readFileSync(
  new URL("../../../docs/handover/真机验收记录表.md", import.meta.url), "utf8");

test("十三项与记录表逐条对应，且子场景一个不少", () => {
  assert.equal(ACCEPTANCE_ITEMS.length, 13);
  assert.deepEqual(ACCEPTANCE_ITEMS.map((item) => item.no),
    [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]);
  // 第 5 项五个失败场景、第 7 项四个中断场景，第 10、12、13 项各两个，其余八项各一个
  // ——摊平后 23 个可勾项。
  assert.equal(ACCEPTANCE_ITEMS[4]!.steps.length, 5);
  assert.equal(ACCEPTANCE_ITEMS[6]!.steps.length, 4);
  for (const index of [9, 11, 12]) assert.equal(ACCEPTANCE_ITEMS[index]!.steps.length, 2);
  assert.equal(ACCEPTANCE_STEP_KEYS.length, 23);
  assert.equal(new Set(ACCEPTANCE_STEP_KEYS).size, 23, "勾选键不能重复");
});

test("十三项标题与 docs 里那份记录表不会各说各话", () => {
  for (const item of ACCEPTANCE_ITEMS) {
    assert.ok(DOC.includes(`### ${item.no}.`), `记录表缺第 ${item.no} 项`);
  }
  assert.equal((DOC.match(/^### \d+\./gm) ?? []).length, 13);
  // 新增的五项,标题两边逐字一致。
  for (const item of ACCEPTANCE_ITEMS.slice(8)) {
    assert.ok(DOC.includes(`### ${item.no}. ${item.title}`), `记录表第 ${item.no} 项标题不一致`);
  }
});

test("第 1 项不再要求「麦克风不开了又关又开」:提前回答检测在播报中可以开麦,但不保存声音", () => {
  const first = ACCEPTANCE_ITEMS[0]!;
  const text = [first.title, ...first.steps.flatMap((step) => [step.how, step.criterion])].join("\n");
  assert.doesNotMatch(text, /开了又关又开/);
  assert.match(text, /「正在听您说」只出现一次/);
  assert.match(text, /播报中系统麦克风指示可以亮，但不保存声音/);
  const section = DOC.slice(DOC.indexOf("### 1."), DOC.indexOf("### 2."));
  assert.doesNotMatch(section, /开了又关又开/);
  assert.match(section, /「正在听您说」只出现一次/);
  assert.match(section, /播报中系统麦克风指示可以亮，但不保存声音/);
});

test("新增的现场项目:现在回答、停播误停与漏停、找词停顿、老人已答对/跳过本题、指定起点", () => {
  assert.deepEqual(ACCEPTANCE_ITEMS.slice(8).map((item) => item.title), [
    "现在回答",
    "停播误停与漏停",
    "找词停顿不被静音收麦截断",
    "老人已答对 / 跳过本题",
    "指定起点（含中止后同周第二场从第 N 题开始）",
  ]);
  for (const item of ACCEPTANCE_ITEMS.slice(8)) {
    for (const step of item.steps) {
      assert.ok(step.how.trim() && step.criterion.trim(), `第 ${item.no} 项 ${step.key} 缺做法或判据`);
    }
  }
  // 中止后同周第二场从第 N 题开始,单独一个勾选项。
  assert.ok(ACCEPTANCE_ITEMS[12]!.steps.some((step) => /中止/.test(step.title) && /第二场/.test(step.title)));
});

test("结论与免责小字说的是十三项,不再是八项", () => {
  assert.equal(ACCEPTANCE_ITEMS.length, 13);
  const receipt = buildAcceptanceReceipt(allPass(filledHeader(emptyDraft())), MACHINE);
  assert.doesNotMatch(receipt.caveat, /八项/);
  assert.match(receipt.caveat, /十三项全部通过/);
  assert.match(formatAcceptanceText(receipt), /十三项全部勾了通过/);
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
  assert.equal(receipt.steps.length, 23);
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
