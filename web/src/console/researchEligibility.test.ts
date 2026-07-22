import assert from "node:assert/strict";
import test from "node:test";
import { researchEligibilityIssueLabel, researchEligibilityIssueText } from "./researchEligibility.ts";

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
