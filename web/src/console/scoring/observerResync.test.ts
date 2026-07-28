import assert from "node:assert/strict";
import test from "node:test";
import type { ServerSessionJournal } from "../../hooks/useSessionJournal.ts";
import type { SessionPlan, SessionRuntimeState } from "../../types.ts";
import { manualResyncRequired, observerResyncResultCurrent } from "./observerConsoleModel.ts";
import {
  deriveOwnershipTransition,
  runManualResyncTransaction,
  type ResyncDeps,
  type ResyncGuard,
} from "./observerResync.ts";

function deferred<T>(): { promise: Promise<T>; resolve(value: T): void; reject(error: unknown): void } {
  let resolvePromise!: (value: T) => void;
  let rejectPromise!: (error: unknown) => void;
  const promise = new Promise<T>((resolve, reject) => { resolvePromise = resolve; rejectPromise = reject; });
  return { promise, resolve: resolvePromise, reject: rejectPromise };
}

const PLAN: SessionPlan = {
  item_bank_version_id: "bank-v1",
  week_no: 2,
  event_line: "正式训练",
  total_items: 1,
  total_turns: 1,
  items: [{
    item_id: "SE_胡萝卜", task_type: "单要素", image_id: null, presentation_order: 1,
    display: {}, turns: [{ turn_seq: 1, response_role: "命名", scoring_key: null }],
  }],
};

function runtimeAt(sessionId: string, itemIdx = 0, turnIdx = 0, revision = 9): SessionRuntimeState {
  return {
    sessionId, status: "active", revision,
    cursor: { sessionId, itemIdx, turnIdx }, rapportStep: null,
  };
}

function journalFor(sessionId: string): ServerSessionJournal {
  return {
    session: { session_id: sessionId } as ServerSessionJournal["session"],
    items: [], turns: [], audios: [], abnormal: [], attempts: [], interactions: [], audio_receipts: [],
  };
}

function baseDeps(overrides: Partial<ResyncDeps> = {}): ResyncDeps {
  return {
    fetchStatus: async () => ({ serverOwned: false, stateRevision: 1 }),
    fetchRuntime: async (sid) => runtimeAt(sid),
    fetchJournal: async (sid) => journalFor(sid),
    getPlan: () => PLAN,
    getRuntimeRevisionFloor: () => 0,
    ...overrides,
  };
}

// 模拟真实调用方的"当前 fence + 所有权"世界：一个不经过 React state/effect 的
// 可变量，guard 每次都同步读它——正是生产代码里 resyncFence.current 与
// ownershipRef.current 承担的角色。begin(epoch) 对应 runManualResync 里
// `resyncFence.current = fence` 这一步：必须让世界的当前 fence 与本次事务的
// fence 一致地"同时"落地，事务开始之后的任何外部改动才代表后来发生的事。
function makeWorld(sessionId: string) {
  const world = { fence: { sessionId, epoch: 0 }, owned: false };
  function begin(epoch: number): ResyncGuard {
    world.fence = { sessionId, epoch };
    return { isLive: () => observerResyncResultCurrent({ sessionId, epoch }, world.fence) && !world.owned };
  }
  return { world, begin };
}

test("1: no-owner is proven fresh before any runtime GET — an already-owned status short-circuits with zero runtime/journal calls", async () => {
  const calls: string[] = [];
  const { begin } = makeWorld("S-a");
  const deps = baseDeps({
    fetchStatus: async (sid) => { calls.push(`status:${sid}`); return { serverOwned: true, stateRevision: 3 }; },
    fetchRuntime: async (sid) => { calls.push(`runtime:${sid}`); return runtimeAt(sid); },
    fetchJournal: async (sid) => { calls.push(`journal:${sid}`); return journalFor(sid); },
  });
  const outcome = await runManualResyncTransaction("S-a", begin(1), deps);
  assert.equal(outcome.kind, "failed");
  assert.deepEqual(calls, ["status:S-a"]);
});

test("2: owner reacquired between the runtime/journal dispatch and its resolution discards the late result", async () => {
  const { world, begin } = makeWorld("S-a");
  const runtimeGate = deferred<SessionRuntimeState | null>();
  const journalGate = deferred<ServerSessionJournal>();
  const deps = baseDeps({
    fetchRuntime: async () => {
      // runtime/journal 请求已经发出的那一刻，服务器同步证明重新持有——和真实
      // ownershipRef 的更新一样，不等任何一次 React 渲染。
      world.owned = true;
      return runtimeGate.promise;
    },
    fetchJournal: async () => journalGate.promise,
  });
  const pending = runManualResyncTransaction("S-a", begin(1), deps);
  runtimeGate.resolve(runtimeAt("S-a"));
  journalGate.resolve(journalFor("S-a"));
  const outcome = await pending;
  assert.equal(outcome.kind, "stale");
});

test("3: owner true→false batched into a single render still requires resync — exposure is sticky per call, not per render", () => {
  let exposure = false;
  // 两次真实回调（owned:true 再 owned:false）发生在同一个 tick 里，React 只会
  // 提交合并后的最终 state，但 exposure 必须在第一次调用时就落定，不能被第二次
  // 调用的最终快照冲掉。
  let effect = deriveOwnershipTransition(exposure, { owned: true, phase: "running" }, true);
  exposure = effect.automationExposure;
  effect = deriveOwnershipTransition(exposure, { owned: false, phase: "idle" }, true);
  exposure = effect.automationExposure;
  assert.equal(exposure, true);
  assert.equal(manualResyncRequired(true, { owned: false, phase: "idle" }, exposure), true);
});

test("4: two resyncs race on different epochs — only the latest epoch's result may apply", async () => {
  const { begin } = makeWorld("S-a");
  const staleStatus = deferred<{ serverOwned: boolean; stateRevision: number }>();
  const freshStatus = deferred<{ serverOwned: boolean; stateRevision: number }>();
  let statusCallCount = 0;
  const deps = baseDeps({
    fetchStatus: async () => {
      statusCallCount += 1;
      return statusCallCount === 1 ? staleStatus.promise : freshStatus.promise;
    },
  });
  // 第一次重同步（epoch 1）发起后卡在 status 请求；未等它完成就又触发一次
  // （epoch 2）——begin(2) 把世界当前 fence 推进到 2，等于同步作废了 epoch 1。
  const first = runManualResyncTransaction("S-a", begin(1), deps);
  const second = runManualResyncTransaction("S-a", begin(2), deps);
  freshStatus.resolve({ serverOwned: false, stateRevision: 5 });
  const secondOutcome = await second;
  // 旧 epoch 的请求这时才姗姗来迟。
  staleStatus.resolve({ serverOwned: false, stateRevision: 5 });
  const firstOutcome = await first;
  assert.equal(firstOutcome.kind, "stale");
  assert.equal(secondOutcome.kind, "applied");
});

test("5: session A's late result arrives after switching to session B — zero application", async () => {
  const { world, begin } = makeWorld("S-a");
  const statusGate = deferred<{ serverOwned: boolean; stateRevision: number }>();
  const deps = baseDeps({ fetchStatus: async () => statusGate.promise });
  const pending = runManualResyncTransaction("S-a", begin(1), deps);
  // 切场到 B：世界的当前 fence 换了 sessionId。
  world.fence = { sessionId: "S-b", epoch: 1 };
  statusGate.resolve({ serverOwned: false, stateRevision: 1 });
  const outcome = await pending;
  assert.equal(outcome.kind, "stale");
});

test("6: runtime succeeds but journal fails (and the reverse) — zero partial application", async () => {
  const { begin } = makeWorld("S-a");
  const failJournal = baseDeps({ fetchJournal: async () => { throw new Error("journal unavailable"); } });
  const outcomeA = await runManualResyncTransaction("S-a", begin(1), failJournal);
  assert.equal(outcomeA.kind, "failed");

  const failRuntime = baseDeps({ fetchRuntime: async () => { throw new Error("runtime unavailable"); } });
  const outcomeB = await runManualResyncTransaction("S-a", begin(1), failRuntime);
  assert.equal(outcomeB.kind, "failed");
});

test("7: stable no-owner + unchanged status revision + fresh exact-session runtime + legal cursor — applies exactly once", async () => {
  const calls: string[] = [];
  const { begin } = makeWorld("S-a");
  const deps = baseDeps({
    fetchStatus: async () => { calls.push("status"); return { serverOwned: false, stateRevision: 7 }; },
    fetchRuntime: async (sid) => { calls.push("runtime"); return runtimeAt(sid); },
    fetchJournal: async (sid) => { calls.push("journal"); return journalFor(sid); },
  });
  const outcome = await runManualResyncTransaction("S-a", begin(1), deps);
  assert.equal(outcome.kind, "applied");
  if (outcome.kind === "applied") {
    assert.deepEqual(outcome.cursor, { itemIdx: 0, turnIdx: 0 });
    assert.equal(outcome.journal.session.session_id, "S-a");
  }
  // 双重 no-owner 核验：status 在 runtime/journal 之前查一次、之后再查一次。
  assert.deepEqual(calls, ["status", "runtime", "journal", "status"]);
});

test("8: reacquiring ownership invalidates synchronously via a ref-shaped flag, not by waiting for an effect", () => {
  // resync 非 idle 时重新拿到所有权：必须作废。
  const invalidated = deriveOwnershipTransition(true, { owned: true, phase: "starting" }, false);
  assert.equal(invalidated.invalidateResync, true);
  // resync 已是 idle：没有什么可作废的。
  const noop = deriveOwnershipTransition(true, { owned: true, phase: "starting" }, true);
  assert.equal(noop.invalidateResync, false);
  // 明确 no-owner：不涉及作废。
  const released = deriveOwnershipTransition(true, { owned: false, phase: "idle" }, false);
  assert.equal(released.invalidateResync, false);
});

test("9: status revision changes between the before/after check (ABA) rejects even though both say no-owner", async () => {
  const { begin } = makeWorld("S-a");
  let call = 0;
  const deps = baseDeps({
    fetchStatus: async () => { call += 1; return { serverOwned: false, stateRevision: call === 1 ? 5 : 6 }; },
  });
  const outcome = await runManualResyncTransaction("S-a", begin(1), deps);
  assert.equal(outcome.kind, "failed");
});

test("10: a foreign-session runtime, a stale-revision runtime, or an unproven cursor is rejected outright, never coerced into a position", async () => {
  const { begin } = makeWorld("S-a");
  const outcomeA = await runManualResyncTransaction("S-a", begin(1), baseDeps({
    fetchRuntime: async () => runtimeAt("S-other"),
  }));
  assert.equal(outcomeA.kind, "failed");

  const outcomeB = await runManualResyncTransaction("S-a", begin(1), baseDeps({
    fetchRuntime: async (sid) => runtimeAt(sid, 9, 9),
  }));
  assert.equal(outcomeB.kind, "failed");

  const outcomeC = await runManualResyncTransaction("S-a", begin(1), baseDeps({ getPlan: () => null }));
  assert.equal(outcomeC.kind, "failed");

  // runtime 版本落后于本页已知下限：即使 session/cursor 都合法也拒绝，不然会
  // 用一次读到的慢副本/交错写把人工位置往回拉。
  const outcomeD = await runManualResyncTransaction("S-a", begin(1), baseDeps({
    fetchRuntime: async (sid) => runtimeAt(sid, 0, 0, 3),
    getRuntimeRevisionFloor: () => 10,
  }));
  assert.equal(outcomeD.kind, "failed");
});
