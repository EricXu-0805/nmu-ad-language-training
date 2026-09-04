// 自动跟场轮询的纯决策层:对 /device/attach 的每种结果给出唯一处置。
// 老人端永不报错、永不闪红;这里只区分"接上了/安静重试/放弃绑定"。
import type { SyncMsg } from "../sync/messages";

export const ATTACH_POLL_MS = 2000;

// 同一浏览器再开一个老人端页签:两页共用同一份绑定与 deviceId,新页一去 attach 就把
// 旧页的能力轮换掉并让自动带练安全暂停(2026-09-04 生产 autopilot_device_rotated)。
// 所以先在同源页签间问一句,有人应答就不去 attach,由问候页告诉工作人员。
export const OTHER_TAB_PROBE_MS = 400;

/** 等 timeoutMs 内有没有别的页签用同一 nonce 回「我连着 sessionId」;没有就 null。 */
export function probeOtherTabs(
  post: (msg: SyncMsg) => void,
  subscribe: (handler: (msg: SyncMsg) => void) => () => void,
  timeoutMs: number,
  nonce: string,
): Promise<string | null> {
  return new Promise((resolve) => {
    let done = false;
    let unsubscribe = () => {};
    const finish = (held: string | null) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      unsubscribe();
      resolve(held);
    };
    unsubscribe = subscribe((msg) => {
      if (msg.type === "capabilityHeld" && msg.nonce === nonce) finish(msg.sessionId);
    });
    const timer = setTimeout(() => finish(null), timeoutMs);
    post({ type: "capabilityProbe", nonce });
  });
}

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
// 令牌区分「别的设备连着」),其余 409 仍是「这位受试者现在没有场次」;
// other_tab 是本机判的:同一浏览器另一个页签已经连着场次,这个页签根本没去 attach。
export type AttachHint = "busy" | "no_session" | "other_tab" | null;

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
