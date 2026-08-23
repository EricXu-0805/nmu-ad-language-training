export function researchEligibilityIssueLabel(issue: string): string {
  const value = issue.trim();
  if (value.startsWith("consent_status")) return "知情同意状态未明确为“已同意/有效”";
  if (value.startsWith("consent_type")) return "知情同意方式未填写";
  if (value.includes("proxy_consent")) return "代理同意尚未明确确认";
  if (value.includes("assent_obtained")) return "尚未明确取得受试者本人赞同";
  if (value.startsWith("mandarin_eligible")) return "普通话训练资格尚未明确通过";
  if (value.startsWith("recording_allowed")) return "研究录音授权尚未明确允许";
  if (value.startsWith("withdrawal_status=")) {
    const status = value.slice("withdrawal_status=".length).trim();
    return status ? `存在撤回/退出状态：${status}` : "存在撤回/退出状态";
  }
  return value || "未说明的准入缺项";
}

export function researchEligibilityIssueText(issues: string[]): string {
  return issues.map(researchEligibilityIssueLabel).join("；");
}

// 服务器会把准入缺项按字段名拼进整句(如"受试者未满足真实研究准入：consent_type 未填写")。
// 这里在整句内替换全部已知缺项短语——toast/Alert 出口统一接线,字段名不再上屏。
// 短语来自 app/main.py:_research_eligibility_issues 与 app/session_admission.py 两处源头。
const RAW_ISSUE_PATTERNS: readonly [RegExp, string][] = [
  [/consent_status 未明确为已同意\/有效/g, "知情同意状态未明确为“已同意/有效”"],
  [/consent_type 未填写/g, "知情同意方式未填写"],
  [/代理同意(?:路径)?要求 proxy_consent(?:=| 明确为 )true/g, "代理同意尚未明确确认"],
  [/代理同意(?:路径)?要求 assent_obtained(?:=| 明确为 )true/g, "尚未明确取得受试者本人赞同"],
  [/mandarin_eligible 必须明确为 true/g, "普通话训练资格尚未明确通过"],
  [/recording_allowed 必须明确为 true/g, "研究录音授权尚未明确允许"],
  [/recording_allowed=false/g, "研究录音授权已明确拒绝"],
  [/is_simulation_subject=true 的模拟档案不得进入真实研究/g, "模拟档案不能进入真实研究"],
  [/withdrawal_status=(\S+)/g, "存在撤回/退出状态：$1"],
];

export function humanizeEligibilityText(text: string): string {
  let out = text;
  for (const [pattern, human] of RAW_ISSUE_PATTERNS) {
    out = out.replace(pattern, human);
  }
  return out;
}
