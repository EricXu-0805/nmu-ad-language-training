/**
 * 患者端自动流程的页面生命周期闩门。
 *
 * 这个模块只做一件事：把"页面不再前台"这件浏览器事实，翻译成一次、且仅一次的
 * 同步关麦决定。它不认识 controller、capture 或 ACK——那些由调用方接在
 * `shutdown` 上，所以这里可以被纯行为测试，不需要 DOM。
 *
 * 两条时序是分开的，不能混：
 * - `visibilitychange`(hidden) / `pagehide` / `freeze` 必须在**原事件同步栈**里
 *   关麦。浏览器不保证之后还会给你一个宏任务。
 * - React StrictMode 的 `setup → cleanup → setup` 与真实 unmount 在 cleanup 当下
 *   完全不可区分。所以 cleanup 只登记一个 teardown 候选并排一个 microtask；同
 *   hook instance、同 owner 代际的立即第二次 setup 精确取消它。真实 unmount 的
 *   关麦上界因此是一个 microtask，既不靠猜，也不会永久忽略第一次 cleanup。
 */

export type AutopilotLifecycleTrigger =
  | "visibility_hidden" | "pagehide" | "freeze" | "unmount";

export interface AutopilotPageLifecycleHost {
  isHidden(): boolean;
  supportsFreeze(): boolean;
  addEventListener(type: string, handler: () => void): void;
  removeEventListener(type: string, handler: () => void): void;
  scheduleMicrotask(callback: () => void): void;
}

export interface AutopilotPageLifecycleOptions {
  /** 本次 owner lease 代际；旧代际的事件不得关掉新代际的麦。 */
  ownerGeneration: number;
  host: AutopilotPageLifecycleHost;
  /**
   * 同步物理关麦闩。first-writer 赢了才调用，且整个生命周期只调用一次。
   * 它抛错不改变判定——已经赢下的那次中断不能因为清理失败而被撤销。
   */
  shutdown(trigger: AutopilotLifecycleTrigger): void;
}

/** 生产宿主：document/window 真实事件，`freeze` 不支持时自动降级。 */
export function browserAutopilotLifecycleHost(): AutopilotPageLifecycleHost {
  return {
    isHidden: () => document.visibilityState !== "visible",
    supportsFreeze: () => "onfreeze" in document,
    addEventListener: (type, handler) => {
      const target: EventTarget = type === "pagehide" ? window : document;
      target.addEventListener(type, handler);
    },
    removeEventListener: (type, handler) => {
      const target: EventTarget = type === "pagehide" ? window : document;
      target.removeEventListener(type, handler);
    },
    scheduleMicrotask: (callback) => queueMicrotask(callback),
  };
}

export class AutopilotPageLifecycle {
  readonly ownerGeneration: number;
  private readonly host: AutopilotPageLifecycleHost;
  private readonly shutdown: (trigger: AutopilotLifecycleTrigger) => void;
  private readonly abortController = new AbortController();
  private readonly handlers = new Map<string, () => void>();
  private triggerValue: AutopilotLifecycleTrigger | null = null;
  private teardownToken: object | null = null;
  private installed = false;

  constructor(options: AutopilotPageLifecycleOptions) {
    this.ownerGeneration = options.ownerGeneration;
    this.host = options.host;
    this.shutdown = options.shutdown;
  }

  get trigger(): AutopilotLifecycleTrigger | null { return this.triggerValue; }

  get aborted(): boolean { return this.triggerValue !== null; }

  /** 异步 bootstrap 与后续每个 tick 在继续之前都读它，pre-start 隐藏后本窗口彻底停。 */
  get signal(): AbortSignal { return this.abortController.signal; }

  /** 页面已隐藏就不允许再造新的 capture——录音在后台开起来是隐性采集。 */
  canCreateCapture(): boolean {
    return !this.aborted && !this.host.isHidden();
  }

  install(): void {
    if (this.installed) return;
    this.installed = true;
    this.listen("visibilitychange", () => {
      if (this.host.isHidden()) this.win("visibility_hidden");
    });
    this.listen("pagehide", () => this.win("pagehide"));
    if (this.host.supportsFreeze()) this.listen("freeze", () => this.win("freeze"));
  }

  /**
   * 换 session / 换 owner / 真实卸载时摘掉监听。可重复调用。
   * 摘监听本身不关麦：普通的依赖变化不是 `device_runtime_failed`。
   */
  uninstall(): void {
    for (const [type, handler] of this.handlers) {
      this.host.removeEventListener(type, handler);
    }
    this.handlers.clear();
    this.installed = false;
    this.teardownToken = null;
  }

  /** hook cleanup：登记一个候选，排一个 microtask，此刻不做任何判定。 */
  requestTeardown(): void {
    if (this.aborted) return;
    const token = {};
    this.teardownToken = token;
    this.host.scheduleMicrotask(() => {
      // 同代际的第二次 setup 已经把它换掉/清掉了：这是 StrictMode 探针，不是卸载。
      if (this.teardownToken !== token) return;
      this.teardownToken = null;
      this.win("unmount");
    });
  }

  /** 同 hook instance、同 owner 代际的立即第二次 setup。 */
  cancelTeardown(): void {
    this.teardownToken = null;
  }

  private listen(type: string, handler: () => void): void {
    this.handlers.set(type, handler);
    this.host.addEventListener(type, handler);
  }

  private win(trigger: AutopilotLifecycleTrigger): void {
    if (this.triggerValue !== null) return;
    this.triggerValue = trigger;
    this.teardownToken = null;
    this.abortController.abort(
      new DOMException("自动流程页面已离开前台", "AbortError"));
    try {
      this.shutdown(trigger);
    } catch {
      // 关麦闩自己失败已经无处可报；这一次中断已经赢下，不得被撤销。
    }
  }
}
