const AUTOPILOT_OWNER_LOCK_PREFIX = "nmu:patient-autopilot-owner:v1:";

export type AutopilotOwnerLockCallback = (lock: unknown) => Promise<void>;

/** Minimal Web Locks surface, kept injectable for deterministic tests. */
export interface AutopilotOwnerLockManager {
  request(
    name: string,
    options: { mode: "exclusive"; signal?: AbortSignal },
    callback: AutopilotOwnerLockCallback,
  ): Promise<unknown>;
}

export interface AutopilotOwnerLease {
  release(): void;
  /** Resolves only after the browser has actually released the origin lock. */
  released: Promise<void>;
}

export class AutopilotOwnerLeaseUnavailableError extends Error {
  constructor(message = "当前浏览器不支持安全的跨页面自动流程锁") {
    super(message);
    this.name = "AutopilotOwnerLeaseUnavailableError";
  }
}

export function browserAutopilotOwnerLockManager(): AutopilotOwnerLockManager | null {
  if (typeof navigator === "undefined") return null;
  const locks = navigator.locks;
  if (!locks || typeof locks.request !== "function") return null;
  return locks as unknown as AutopilotOwnerLockManager;
}

function lockName(sessionId: string): string {
  if (!sessionId || sessionId.length > 128 || /[\p{Cc}\p{Cf}]/u.test(sessionId)) {
    throw new Error("自动流程场次锁标识非法");
  }
  return `${AUTOPILOT_OWNER_LOCK_PREFIX}${sessionId}`;
}

/**
 * Hold one session's complete patient runner, not merely its microphone. This
 * prevents two same-origin tabs from speaking the same command or racing the
 * durable ACK sequence. A standby tab waits in `probing` until the owner exits.
 */
export function acquireAutopilotOwnerLease(
  sessionId: string,
  manager: AutopilotOwnerLockManager | null = browserAutopilotOwnerLockManager(),
  signal?: AbortSignal,
): Promise<AutopilotOwnerLease> {
  if (!manager) return Promise.reject(new AutopilotOwnerLeaseUnavailableError());
  if (signal?.aborted) {
    return Promise.reject(signal.reason ?? new DOMException("自动流程锁请求已取消", "AbortError"));
  }
  const name = lockName(sessionId);
  return new Promise<AutopilotOwnerLease>((resolve, reject) => {
    let granted = false;
    let releaseHold: (() => void) | null = null;
    let acknowledgeRelease: (() => void) | null = null;
    const released = new Promise<void>((done) => { acknowledgeRelease = done; });
    const options: { mode: "exclusive"; signal?: AbortSignal } = { mode: "exclusive" };
    if (signal) options.signal = signal;

    void manager.request(name, options, async () => {
      granted = true;
      let releasedByOwner = false;
      const hold = new Promise<void>((done) => { releaseHold = done; });
      resolve({
        release() {
          if (releasedByOwner) return;
          releasedByOwner = true;
          releaseHold?.();
        },
        released,
      });
      await hold;
    }).catch((error: unknown) => {
      if (!granted) reject(error);
    }).then(
      () => acknowledgeRelease?.(),
      () => { if (granted) acknowledgeRelease?.(); },
    );
  });
}
