import type { CloudProcessingPolicy, Patient } from "../types";

type PatientCloudAuthorization = Partial<Pick<Patient,
  | "cloud_processing_allowed"
  | "cloud_processing_provider_id"
  | "cloud_processing_notice_version"
  | "cloud_processing_consented_at"
  | "cloud_processing_revoked_at">>;

// 真实研究场次启动/继续自动带练前,服务端 _require_gate 的同一条判定
// (app/autopilot_service.py,不过就 409 autopilot_cloud_processing_required)。
// 前端只用来提前禁用启动按钮;服务端仍会在写入前再核一次。
export function cloudProcessingAuthorizedForAutopilot(
  patient: PatientCloudAuthorization,
  policy: CloudProcessingPolicy,
): boolean {
  return policy.configured === true
    && patient.cloud_processing_allowed === true
    && (patient.cloud_processing_provider_id ?? null) === (policy.provider_id ?? null)
    && (patient.cloud_processing_notice_version ?? null) === (policy.notice_version ?? null)
    && patient.cloud_processing_consented_at != null
    && patient.cloud_processing_revoked_at == null;
}

export type CloudAuthorizationRead =
  | { ok: true; authorized: boolean }
  | { ok: false; error: string };

// 读失败不推翻已读到的结论(单次抖动不该把能启动的按钮锁住);从没读到过就保持待核对(null)。
export function nextCloudAuthorization(
  previous: boolean | null,
  read: CloudAuthorizationRead,
): boolean | null {
  return read.ok ? read.authorized : previous;
}

const CLOUD_AUTHORIZATION_REREAD_MS = 30_000;
const CLOUD_AUTHORIZATION_FIRST_RETRY_MS = 3_000;

// 已有结论:30 s 重读一次,读失败也照旧。从没读到过:首读一次 502 不该把已授权受试者的
// 启动挡满 30 s,按 3 s 起翻倍重试,封顶 30 s。
export function cloudAuthorizationReadDelayMs(
  known: boolean | null,
  consecutiveFailures: number,
): number {
  if (known !== null) return CLOUD_AUTHORIZATION_REREAD_MS;
  return Math.min(
    CLOUD_AUTHORIZATION_FIRST_RETRY_MS * 2 ** Math.max(0, consecutiveFailures - 1),
    CLOUD_AUTHORIZATION_REREAD_MS,
  );
}

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
