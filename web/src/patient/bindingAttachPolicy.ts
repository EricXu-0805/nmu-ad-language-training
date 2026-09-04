// 自动跟场轮询的纯决策层:对 /device/attach 的每种结果给出唯一处置。
// 老人端永不报错、永不闪红;这里只区分"接上了/安静重试/放弃绑定"。

export const ATTACH_POLL_MS = 2000;

export type AttachDisposition =
  | "attached"          // 200:能力已到手,停止轮询交回既有 live 流程
  | "quiet_retry"       // 无场次/别人的场次/限速/网络抖动:保持问候页安静重试
  | "drop_binding";     // 绑定本身已死(验签失败/已撤回/功能未启用):清除并停

export function classifyAttachOutcome(
  status: number,
  code: string | null,
): AttachDisposition {
  if (status >= 200 && status < 300) return "attached";
  if (status === 401
      && (code === "device_binding_invalid" || code === "device_binding_revoked")) {
    return "drop_binding";
  }
  // 未启用按受试者配对的部署:令牌永远换不到能力,留着只会空转。
  if (status === 503 && code === "patient_binding_unavailable") return "drop_binding";
  // 409(当前没有可自动连接的场次)、429(限速)、网络错误(status 0)与其余
  // 服务器状态一律安静重试——绝不因为别人在训练就打扰问候页。
  return "quiet_retry";
}

// 问候页给工作人员看的一句实话。busy 只对本人绑定成立(服务端只对同一受试者的
// 令牌区分「别的设备连着」),其余 409 仍是「这位受试者现在没有场次」。
export type AttachHint = "busy" | "no_session" | null;

export function attachHintFor(status: number, code: string | null): AttachHint {
  if (status !== 409) return null;
  return code === "device_attach_device_busy" ? "busy" : "no_session";
}

// 只有"没有场次能力、但有绑定"的老人端才轮询 attach;有能力时 live 轮询接管。
export function shouldAttemptAttach(
  hasBinding: boolean,
  hasCapability: boolean,
): boolean {
  return hasBinding && !hasCapability;
}
