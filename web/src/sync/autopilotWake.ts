// 服务器取得控制权后的同窗一次性唤醒。
//
// 时序问题:患者端配对后先探测 /autopilot/next,权威 autopilot_not_active 会被
// stop-legacy 闩住(防 1.5 秒 409 请求风暴)——闩住语义为"停止轮询,直到发生明确的
// serverOwned 唤醒或 capability/session epoch 改变"。console 随后启动服务器 AI
// 时,患者 hook 的 sessionId/capabilityKey/probeKey/probeEpoch 都不变,老人端会
// 停在 legacy 收不到第一条服务器命令。此模块补上那一次明确唤醒。
//
// 唤醒不是授权、不是服务器真值的替身、不是使用证据(不入 /ai-usage、不写 TTS
// 账本);患者端被唤醒后也只是对当前 exact session/capability 重新探测一次
// /autopilot/next,一切仍由设备 capability 与服务端命令接口权威裁决。

export const PATIENT_AUTOPILOT_WAKE_EVENT = "nmu:autopilot-wake"; // detail: AutopilotWakeMsg

export interface AutopilotWakeMsg {
  sessionId: string;
  stateRevision: number;
}

const CONTROL_CHARACTER = /[\p{Cc}\p{Cf}]/u;

/** Strict boundary for the window-event payload. Extra or malformed fields fail closed. */
export function parseAutopilotWake(value: unknown): AutopilotWakeMsg | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  const keys = Object.keys(row).sort();
  if (keys.length !== 2 || keys[0] !== "sessionId" || keys[1] !== "stateRevision") return null;
  if (typeof row.sessionId !== "string" || row.sessionId.length === 0
      || row.sessionId.length > 128 || CONTROL_CHARACTER.test(row.sessionId)) return null;
  if (typeof row.stateRevision !== "number" || !Number.isSafeInteger(row.stateRevision)
      || row.stateRevision < 1) return null;
  return { sessionId: row.sessionId, stateRevision: row.stateRevision };
}

export function autopilotWakeToken(msg: AutopilotWakeMsg): string {
  return `${msg.sessionId}\u0000${msg.stateRevision}`;
}

/**
 * Console side. Only an authoritative receipt that proves server ownership of
 * the exact current session may wake the bedside runner. POST /start 的正常回执
 * 与响应丢失后 GET /status 的对账回执走同一条 acceptReceipt 收口,天然同时覆盖;
 * serverOwned=false 与 checking/rejected/uncertain(根本没有权威回执)结构上发不出
 * 唤醒。同 session+同权威版本的重复轮询靠 token 去重,不反复唤醒。
 */
export function nextServerOwnershipWake(
  lastEmittedToken: string | null,
  sessionId: string,
  receipt: { serverOwned: boolean; stateRevision: number },
): AutopilotWakeMsg | null {
  if (receipt.serverOwned !== true) return null;
  const msg = parseAutopilotWake({ sessionId, stateRevision: receipt.stateRevision });
  if (msg === null) return null;
  if (autopilotWakeToken(msg) === lastEmittedToken) return null;
  return msg;
}

/**
 * 跨设备路径。同窗 CustomEvent 只在 console 与患者端同一个浏览器里成立;真实验收
 * 是两台设备/两个独立浏览器,console 那条 window 事件根本到不了患者端。患者端已有的
 * 每 1.5 秒 /live/state 轮询于是成为唯一有界的服务端权威通道。
 *
 * 这里只做严格解析与"同一响应内自洽"两件事:唤醒必须指向这次快照里那个 session,
 * 否则就是旧场次/串场的投影,一律拒绝。重复同 token 不在这里去重——首个 live 响应
 * 可能早于患者 hook 挂载,提前把 token 记成已发会把真正可消费的那一次丢掉;去重由
 * PatientProbeWakeCoordinator 的 pending/handled 负责。
 */
export function liveSnapshotWake(
  projection: unknown,
  liveSessionId: string | null | undefined,
): AutopilotWakeMsg | null {
  const msg = parseAutopilotWake(projection);
  if (msg === null) return null;
  if (!liveSessionId || msg.sessionId !== liveSessionId) return null;
  return msg;
}

// 与患者端 PatientAutopilotMode 同构;字面重复是有意的,sync 层不得 import
// 老人端源码,否则 console 经 sync 会传递性越过 console↛patient 红线。
export type PatientWakeMode = "legacy" | "probing" | "server" | "blocked";

/**
 * Patient side: strict one-shot re-probe coordinator. Latches at most one
 * pending wake for the exact current session, refuses foreign/stale/malformed
 * and already-handled tokens, and releases exactly one probe-epoch bump — only
 * while the resolved mode is still the latched legacy runner. A wake that finds
 * the server runner already mounted is marked served without side effects, so a
 * new stateRevision can never rebuild the media runner.
 */
export class PatientProbeWakeCoordinator {
  #pending: string | null = null;
  #handled = new Set<string>();

  reset(): void {
    this.#pending = null;
    this.#handled.clear();
  }

  /** Latch a raw window-event payload. True only when a new pending wake appears. */
  receive(value: unknown, currentSessionId: string | null): boolean {
    const msg = parseAutopilotWake(value);
    if (msg === null || currentSessionId === null || currentSessionId === ""
        || msg.sessionId !== currentSessionId) return false;
    const token = autopilotWakeToken(msg);
    if (this.#handled.has(token) || this.#pending === token) return false;
    this.#pending = token;
    return true;
  }

  /**
   * Decide on every relevant state change. True (= bump probeEpoch once) is
   * returned at most once per token, and only when the current epoch's probe
   * has already resolved to legacy; an in-flight probe holds the wake instead
   * of dropping it, closing the start-vs-initial-probe race.
   */
  consume(input: { mode: PatientWakeMode; probeResolved: boolean }): boolean {
    if (this.#pending === null) return false;
    if (input.mode === "server") {
      this.#handled.add(this.#pending);
      this.#pending = null;
      return false;
    }
    if (input.mode !== "legacy" || !input.probeResolved) return false;
    this.#handled.add(this.#pending);
    this.#pending = null;
    return true;
  }
}
