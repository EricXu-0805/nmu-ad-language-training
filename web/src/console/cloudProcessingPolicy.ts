import type { CloudProcessingPolicy } from "../types";

export function cloudProcessingChoiceIssue(
  allowed: boolean | null | undefined,
  policy: CloudProcessingPolicy | null,
): string | null {
  if (allowed !== true) return null;
  if (!policy?.configured || !policy.provider_id || !policy.notice_version) {
    return "服务器还没接入云端 AI 服务，现在不能记录允许——请先选“否”或保持“暂不选择”，接入后再回来补记。";
  }
  return null;
}

export function cloudProcessingDisclosure(policy: CloudProcessingPolicy | null): string {
  if (!policy?.configured) {
    return "这台服务器还没接入云端 AI 服务，现在不会、也不能把任何录音或文字发出去。";
  }
  return `如选择允许，原始回答音频（可能包含声纹）及回答转写文本可发送至 ${policy.provider_id}，按告知版本 ${policy.notice_version} 处理。`;
}

// 「是」按钮的禁用原因(可见短句)。null = 配置齐全,可以授权。
// 服务器未接入时选「是」注定失败,与其保存时才撞 409,不如当场禁用并说明白。
export function cloudProcessingAllowDisabledReason(
  policy: CloudProcessingPolicy | null,
  policyError: string | null,
): string | null {
  if (policy?.configured && policy.provider_id && policy.notice_version) return null;
  if (policy === null && !policyError) return "正在读取云端服务配置，请稍候再选";
  return "服务器还没接入云端 AI 服务，现在无法授权——可先选“否”或保持“暂不选择”，接入后再回来补记";
}
