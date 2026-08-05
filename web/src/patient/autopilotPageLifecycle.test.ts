import assert from "node:assert/strict";
import test from "node:test";
import {
  AutopilotPageLifecycle,
  type AutopilotLifecycleTrigger,
} from "./autopilotPageLifecycle.ts";

/** 可控宿主：测试自己决定页面可见性、事件何时来、microtask 何时跑。 */
function hostDouble(options: { hidden?: boolean; freeze?: boolean } = {}) {
  const listeners = new Map<string, Set<() => void>>();
  const microtasks: Array<() => void> = [];
  let hidden = options.hidden ?? false;
  return {
    get listenerCount() {
      let total = 0;
      for (const set of listeners.values()) total += set.size;
      return total;
    },
    types: () => [...listeners.keys()].sort(),
    hide() { hidden = true; },
    show() { hidden = false; },
    fire(type: string) {
      for (const handler of [...(listeners.get(type) ?? [])]) handler();
    },
    /** 把排着的 microtask 跑完，模拟 React 让出那一拍。 */
    flush() {
      while (microtasks.length > 0) (microtasks.shift() as () => void)();
    },
    get pendingMicrotasks() { return microtasks.length; },
    host: {
      isHidden: () => hidden,
      supportsFreeze: () => options.freeze ?? true,
      addEventListener(type: string, handler: () => void) {
        const set = listeners.get(type) ?? new Set();
        set.add(handler);
        listeners.set(type, set);
      },
      removeEventListener(type: string, handler: () => void) {
        listeners.get(type)?.delete(handler);
        if (listeners.get(type)?.size === 0) listeners.delete(type);
      },
      scheduleMicrotask(callback: () => void) { microtasks.push(callback); },
    },
  };
}

function build(options: { hidden?: boolean; freeze?: boolean } = {}) {
  const doubles = hostDouble(options);
  const shutdowns: AutopilotLifecycleTrigger[] = [];
  const lifecycle = new AutopilotPageLifecycle({
    ownerGeneration: 5,
    host: doubles.host,
    shutdown: (trigger) => { shutdowns.push(trigger); },
  });
  return { doubles, shutdowns, lifecycle };
}

test("install 只装一次监听；uninstall 全部摘干净且可重复调用", () => {
  const { doubles, lifecycle, shutdowns } = build();

  lifecycle.install();
  assert.deepEqual(doubles.types(), ["freeze", "pagehide", "visibilitychange"]);
  assert.equal(doubles.listenerCount, 3);

  lifecycle.install();                       // 幂等：不会装出第二套
  assert.equal(doubles.listenerCount, 3);

  lifecycle.uninstall();
  assert.equal(doubles.listenerCount, 0);
  assert.doesNotThrow(() => lifecycle.uninstall());
  // 摘监听不是失败：普通的 session/owner 变化不得被当成 device_runtime_failed。
  assert.deepEqual(shutdowns, []);
});

test("visibilitychange 转 hidden 在原事件同步栈里关麦，不排 microtask", () => {
  const { doubles, lifecycle, shutdowns } = build();
  lifecycle.install();

  doubles.hide();
  doubles.fire("visibilitychange");

  assert.deepEqual(shutdowns, ["visibility_hidden"]);   // 同步就已经发生
  assert.equal(doubles.pendingMicrotasks, 0);
  assert.equal(lifecycle.aborted, true);
  assert.equal(lifecycle.signal.aborted, true);
});

test("仍然可见时的 visibilitychange 什么都不做", () => {
  const { doubles, lifecycle, shutdowns } = build();
  lifecycle.install();

  doubles.fire("visibilitychange");

  assert.deepEqual(shutdowns, []);
  assert.equal(lifecycle.aborted, false);
});

test("重复的生命周期事件只第一个赢，之后一律 no-op", () => {
  const { doubles, lifecycle, shutdowns } = build();
  lifecycle.install();

  doubles.fire("pagehide");
  doubles.fire("pagehide");
  doubles.hide();
  doubles.fire("visibilitychange");
  doubles.fire("freeze");

  assert.deepEqual(shutdowns, ["pagehide"]);
  assert.equal(lifecycle.trigger, "pagehide");
});

test("hidden 之后重新 visible 不自动恢复：abort 信号不会被撤回", () => {
  const { doubles, lifecycle, shutdowns } = build();
  lifecycle.install();

  doubles.hide();
  doubles.fire("visibilitychange");
  doubles.show();
  doubles.fire("visibilitychange");

  assert.deepEqual(shutdowns, ["visibility_hidden"]);
  assert.equal(lifecycle.aborted, true);
  assert.equal(lifecycle.canCreateCapture(), false);
});

test("浏览器不支持 freeze 时不装它，visibilitychange/pagehide 照常工作", () => {
  const { doubles, lifecycle, shutdowns } = build({ freeze: false });
  lifecycle.install();

  assert.deepEqual(doubles.types(), ["pagehide", "visibilitychange"]);
  doubles.fire("pagehide");
  assert.deepEqual(shutdowns, ["pagehide"]);
});

test("shutdown 自己抛错也不撤销这次中断", () => {
  const doubles = hostDouble();
  const lifecycle = new AutopilotPageLifecycle({
    ownerGeneration: 1,
    host: doubles.host,
    shutdown: () => { throw new Error("关麦时设备已被系统回收"); },
  });
  lifecycle.install();

  assert.doesNotThrow(() => doubles.fire("pagehide"));
  assert.equal(lifecycle.aborted, true);
  assert.equal(lifecycle.trigger, "pagehide");
});

test("StrictMode setup→cleanup→setup：同代际第二次 setup 精确取消候选，零误报", () => {
  const { doubles, lifecycle, shutdowns } = build();
  lifecycle.install();

  // 第一次 cleanup：此刻它和真卸载完全不可区分，所以只登记候选。
  lifecycle.requestTeardown();
  assert.equal(doubles.pendingMicrotasks, 1);
  assert.deepEqual(shutdowns, []);

  // 紧接着的第二次 setup：精确取消刚才那个 token。
  lifecycle.cancelTeardown();
  doubles.flush();

  assert.deepEqual(shutdowns, []);          // 没有误报成卸载
  assert.equal(lifecycle.aborted, false);
  assert.equal(doubles.listenerCount, 3);   // 监听也没被顺手摘掉
});

test("没有后继 setup 的真卸载：一个 microtask 之内执行唯一一次关麦", () => {
  const { doubles, lifecycle, shutdowns } = build();
  lifecycle.install();

  lifecycle.requestTeardown();
  assert.deepEqual(shutdowns, []);           // 登记当下不判定

  doubles.flush();                           // 一个 microtask 就是上界

  assert.deepEqual(shutdowns, ["unmount"]);
  assert.equal(lifecycle.aborted, true);
});

test("连续两次 cleanup 只留最后一个候选，关麦仍然只发生一次", () => {
  const { doubles, lifecycle, shutdowns } = build();
  lifecycle.install();

  lifecycle.requestTeardown();
  lifecycle.requestTeardown();
  doubles.flush();

  assert.deepEqual(shutdowns, ["unmount"]);
});

test("已经被 pagehide 赢下之后，卸载候选不再登记也不再关第二次", () => {
  const { doubles, lifecycle, shutdowns } = build();
  lifecycle.install();

  doubles.fire("pagehide");
  lifecycle.requestTeardown();
  doubles.flush();

  assert.deepEqual(shutdowns, ["pagehide"]);
  assert.equal(doubles.pendingMicrotasks, 0);
});

test("页面已隐藏时不允许创建新的 capture", () => {
  const { lifecycle } = build({ hidden: true });
  lifecycle.install();
  assert.equal(lifecycle.canCreateCapture(), false);
});
