import type { NextCommandProjection } from "./autopilotProtocol.ts";

interface PendingWaiter {
  resolve(): void;
  reject(error: Error): void;
  signal: AbortSignal;
  onAbort(): void;
}

type GateState = "loading" | "ready" | "failed";

export class PatientAssetGateError extends Error {
  constructor(message = "当前题目图片未通过显示门禁") {
    super(message);
    this.name = "PatientAssetGateError";
  }
}

function abortError(signal: AbortSignal): Error {
  return signal.reason instanceof Error
    ? signal.reason
    : new DOMException("媒体图片门禁已取消", "AbortError");
}

/**
 * Bridges the React image presentation with the serialized media controller.
 * Keys are current-command capabilities only; old/future commands can never
 * satisfy the exact waiter that owns the microphone or TTS continuation.
 */
export class PatientAssetMediaGate {
  private currentKey: string | null = null;
  private state: GateState = "loading";
  private waiters = new Set<PendingWaiter>();

  report(commandKey: string, state: GateState): void {
    if (this.currentKey !== commandKey) {
      this.rejectAll(new PatientAssetGateError("题目已切换，旧图片门禁失效"));
      this.currentKey = commandKey;
      this.state = state;
    } else {
      this.state = state;
    }
    if (state === "ready") this.resolveAll();
    if (state === "failed") this.rejectAll(new PatientAssetGateError());
  }

  waitFor(command: NextCommandProjection, signal: AbortSignal): Promise<void> {
    const commandKey = command.command_key;
    if (signal.aborted) return Promise.reject(abortError(signal));
    if (this.currentKey !== commandKey) {
      this.rejectAll(new PatientAssetGateError("题目已切换，旧图片门禁失效"));
      this.currentKey = commandKey;
      this.state = "loading";
    }
    if (this.state === "ready") return Promise.resolve();
    if (this.state === "failed") return Promise.reject(new PatientAssetGateError());
    return new Promise<void>((resolve, reject) => {
      const waiter: PendingWaiter = {
        resolve,
        reject,
        signal,
        onAbort: () => {
          this.waiters.delete(waiter);
          reject(abortError(signal));
        },
      };
      this.waiters.add(waiter);
      signal.addEventListener("abort", waiter.onAbort, { once: true });
    });
  }

  reset(reason = "图片门禁已重置"): void {
    this.rejectAll(new PatientAssetGateError(reason));
    this.currentKey = null;
    this.state = "loading";
  }

  private resolveAll(): void {
    for (const waiter of this.waiters) {
      waiter.signal.removeEventListener("abort", waiter.onAbort);
      waiter.resolve();
    }
    this.waiters.clear();
  }

  private rejectAll(error: Error): void {
    for (const waiter of this.waiters) {
      waiter.signal.removeEventListener("abort", waiter.onAbort);
      waiter.reject(error);
    }
    this.waiters.clear();
  }
}
