import type { CloudProcessingPolicy } from "../types";

export function cloudProcessingChoiceIssue(
  allowed: boolean | null | undefined,
  policy: CloudProcessingPolicy | null,
): string | null {
  if (allowed !== true) return null;
  if (!policy?.configured || !policy.provider_id || !policy.notice_version) {
    return "当前服务器未配置完整云处理机构与告知版本，不能记录允许；请联系管理员。";
  }
  return null;
}

export function cloudProcessingDisclosure(policy: CloudProcessingPolicy | null): string {
  if (!policy?.configured) return "当前服务器尚未启用可授权的第三方云处理 policy。";
  return `如选择允许，原始回答音频（可能包含声纹）及回答转写文本可发送至 ${policy.provider_id}，按告知版本 ${policy.notice_version} 处理。`;
}
