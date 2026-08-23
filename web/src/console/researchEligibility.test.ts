import assert from "node:assert/strict";
import test from "node:test";
import { humanizeEligibilityText, researchEligibilityIssueLabel, researchEligibilityIssueText } from "./researchEligibility.ts";

test("research eligibility fields are translated into researcher-facing Chinese", () => {
  assert.equal(researchEligibilityIssueLabel("consent_status 未明确为已同意/有效"), "知情同意状态未明确为“已同意/有效”");
  assert.equal(researchEligibilityIssueLabel("consent_type 未填写"), "知情同意方式未填写");
  assert.equal(researchEligibilityIssueLabel("代理同意路径要求 proxy_consent 明确为 true"), "代理同意尚未明确确认");
  assert.equal(researchEligibilityIssueLabel("代理同意路径要求 assent_obtained 明确为 true"), "尚未明确取得受试者本人赞同");
  assert.equal(researchEligibilityIssueLabel("mandarin_eligible 必须明确为 true"), "普通话训练资格尚未明确通过");
  assert.equal(researchEligibilityIssueLabel("recording_allowed 必须明确为 true"), "研究录音授权尚未明确允许");
  assert.equal(researchEligibilityIssueLabel("withdrawal_status=已撤回"), "存在撤回/退出状态：已撤回");
});

test("unknown eligibility issues remain visible instead of being discarded", () => {
  assert.equal(researchEligibilityIssueText(["未知的新门禁"]), "未知的新门禁");
});

test("P1-1:整句消息里的字段名缺项短语被逐个翻成人话", () => {
  assert.equal(
    humanizeEligibilityText("受试者未满足真实研究准入：consent_type 未填写；recording_allowed 必须明确为 true"),
    "受试者未满足真实研究准入：知情同意方式未填写；研究录音授权尚未明确允许");
  assert.equal(
    humanizeEligibilityText("consent_status 未明确为已同意/有效"),
    "知情同意状态未明确为“已同意/有效”");
  assert.equal(
    humanizeEligibilityText("代理同意路径要求 proxy_consent 明确为 true；代理同意要求 assent_obtained=true"),
    "代理同意尚未明确确认；尚未明确取得受试者本人赞同");
  assert.equal(
    humanizeEligibilityText("mandarin_eligible 必须明确为 true"),
    "普通话训练资格尚未明确通过");
  assert.equal(humanizeEligibilityText("withdrawal_status=已撤回"), "存在撤回/退出状态：已撤回");
  assert.equal(humanizeEligibilityText("recording_allowed=false"), "研究录音授权已明确拒绝");
  // 不相关的句子原样通过。
  assert.equal(humanizeEligibilityText("训练安排已保存并审核通过"), "训练安排已保存并审核通过");
});
