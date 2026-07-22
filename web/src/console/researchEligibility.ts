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
