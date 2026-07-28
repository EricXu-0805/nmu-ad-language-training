import assert from "node:assert/strict";
import test from "node:test";
import type { ServerSessionJournal } from "../../hooks/useSessionJournal.ts";
import { ratchetRevisionFloor, type RuntimeRevisionFloor } from "../../hooks/runtimeRevisionFloor.ts";
import type { SessionPlan, SessionRuntimeState } from "../../types.ts";
import { manualResyncRequired, manualSurfaceLocked, observerResyncResultCurrent } from "./observerConsoleModel.ts";
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
    reportRuntimeRevision: () => {},
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

test("3: owner true→false batched into a single render still requires resync — previousOwned must come from a ref latched per call, not a render-level snapshot", () => {
  // 模拟真实 onServerOwnershipChange 的逐次调用：previousOwned 取自"上一次调用"
  // 时的 ref 快照，在覆写 ref 之前捕获——这正是生产代码里 ownershipRef 的角色，
  // 不是 React 批处理之后才能看到的最终 state。
  const ownershipRef = { current: { owned: false, phase: "idle" } };
  let exposure = false;
  function call(next: { owned: boolean; phase: string }) {
    const previousOwned = ownershipRef.current.owned;
    ownershipRef.current = next;
    const transition = deriveOwnershipTransition(exposure, next, true);
    exposure = transition.automationExposure;
    return manualResyncRequired(previousOwned, next, exposure);
  }
  // 同一个 tick 里两次真实回调：先 owned:true(running)，再 owned:false(idle)。
  // React 只会把这两次 setState 合并渲染成最终的 false，但下降沿必须在第二次
  // 调用时就被同步捕捉到。
  const triggeredOnFirstCall = call({ owned: true, phase: "running" });
  const triggeredOnSecondCall = call({ owned: false, phase: "idle" });
  assert.equal(triggeredOnFirstCall, false, "刚变为 owned 不该触发恢复");
  assert.equal(triggeredOnSecondCall, true, "同一个 tick 内的下降沿必须被逐次捕捉到，不能等渲染后的快照");
});

test("3b: the release-needs-resync latch must gate the lock directly, not merely trigger an effect", () => {
  // 复现 onServerOwnershipChange 释放时的真实序列：previousOwned 来自逐次调用
  // 的 ref，manualResyncRequired 为真时才会同步 latch releaseNeedsResync。
  const ownershipRef = { current: { owned: true, phase: "running" as const } };
  let exposure = true; // 之前已经真正暴露过
  const previousOwned = ownershipRef.current.owned;
  const next = { owned: false, phase: "idle" as const };
  ownershipRef.current = next;
  const transition = deriveOwnershipTransition(exposure, next, true);
  exposure = transition.automationExposure;
  const releaseNeedsResync = !transition.invalidateResync && manualResyncRequired(previousOwned, next, exposure);
  assert.equal(releaseNeedsResync, true, "这就是回调决定要触发恢复、同步 latch 的那一刻");

  // 这一刻，manualResync 的 setState 还没被 React 提交渲染——如果只信
  // manualSurfaceLocked(state, status)，这一次渲染会显示"解锁"。
  const stateOnlyObserverMode = manualSurfaceLocked(next, "idle");
  assert.equal(stateOnlyObserverMode, false,
    "只用尚未提交的 state 快照判断，released 后这一渲染会误判成解锁");

  // 加上 releaseNeedsResync 这个同步 latch 之后才对：这一渲染必须仍然锁定，
  // 否则挂在人工分支下、依赖 manualInteractionBlocked 的被动 effect（例如按
  // itemIdx/turnIdx 补发游标那个）可能在这一拍抢先用本地旧位置写一次游标。
  const observerMode = stateOnlyObserverMode || releaseNeedsResync;
  assert.equal(observerMode, true);
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
    assert.equal(outcome.runtimeRevision, 9);
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

test("11: a floor that rises while statusAfter is provably still in flight rejects, even though the runtime passed at fetch time", async () => {
  const { begin } = makeWorld("S-a");
  let floor = 10;
  // statusAfterDispatched 让测试能确定性地等到"第二次 fetchStatus 已经被调用、
  // 正处于挂起状态"这个精确时刻——不能只是在事务刚起步时随手改 floor，那样
  // 证明不了"floor 是在 statusAfter 还没返回期间才变"这件事本身。
  const statusAfterDispatched = deferred<void>();
  const statusAfterGate = deferred<{ serverOwned: boolean; stateRevision: number }>();
  let statusCall = 0;
  const deps = baseDeps({
    fetchStatus: async () => {
      statusCall += 1;
      if (statusCall === 1) return { serverOwned: false, stateRevision: 1 };
      statusAfterDispatched.resolve();
      return statusAfterGate.promise;
    },
    fetchRuntime: async (sid) => runtimeAt(sid, 0, 0, 10), // 取到时 revision=10，当时和下限打平
    getRuntimeRevisionFloor: () => floor,
  });
  const pending = runManualResyncTransaction("S-a", begin(1), deps);
  // 证明顺序：runtime 已经取到(10) → statusAfter 已被派发、仍在挂起 → 这时才把
  // 下限推到 11 → 才放行 statusAfter。
  await statusAfterDispatched.promise;
  floor = 11;
  statusAfterGate.resolve({ serverOwned: false, stateRevision: 1 });
  const outcome = await pending;
  assert.equal(outcome.kind, "failed");
});

test("12: every session-matched runtime read is reported via a continuation independent of Promise.all, even when the parallel journal request later rejects", async () => {
  const { begin } = makeWorld("S-a");
  const reported: number[] = [];
  const reportedSignal = deferred<void>();
  const journalGate = deferred<ServerSessionJournal>();
  const outcome = runManualResyncTransaction("S-a", begin(1), baseDeps({
    fetchRuntime: async (sid) => runtimeAt(sid, 0, 0, 42),
    fetchJournal: async () => journalGate.promise,
    reportRuntimeRevision: (revision) => { reported.push(revision); reportedSignal.resolve(); },
  }));
  // 确定性地等到 runtime 的 revision 已经被棘轮进下限——这个上报必须独立于
  // Promise.all 会不会因为 journal 那一路 reject 而整体短路，不能靠"两条微任务
  // 链谁先跑完"这种不确定的时序去赌。
  await reportedSignal.promise;
  assert.deepEqual(reported, [42]);
  // 现在才让 journal 那一路真正失败（保留原本"journal throws"这个回归场景），
  // 证明稍后失败完全不影响已经发生的上报。
  journalGate.reject(new Error("journal unavailable"));
  const result = await outcome;
  assert.equal(result.kind, "failed");
});

test("13: a background poll's own continuation ratchets the shared floor before any render — a resync direct-read of an older revision is rejected", async () => {
  // 共享 floor：用真实的 ratchetRevisionFloor（与 useSessionRuntime 里
  // reportRevision 用的同一个函数），模拟 hook 内的 revisionFloorRef。
  let floor: RuntimeRevisionFloor = { sessionId: "S-a", revision: 8 };
  const reportRevision = (sessionId: string, revision: number) => {
    floor = ratchetRevisionFloor(floor, sessionId, revision);
  };
  const { begin } = makeWorld("S-a");

  const runtimeDispatched = deferred<void>();
  const runtimeGate = deferred<SessionRuntimeState>();
  const statusAfterDispatched = deferred<void>();
  const statusAfterGate = deferred<{ serverOwned: boolean; stateRevision: number }>();
  let statusCall = 0;

  const outcome = runManualResyncTransaction("S-a", begin(1), {
    fetchStatus: async () => {
      statusCall += 1;
      if (statusCall === 1) return { serverOwned: false, stateRevision: 1 };
      statusAfterDispatched.resolve();
      return statusAfterGate.promise;
    },
    fetchRuntime: async () => {
      runtimeDispatched.resolve();
      return runtimeGate.promise;
    },
    fetchJournal: async (sid) => journalFor(sid),
    getPlan: () => PLAN,
    getRuntimeRevisionFloor: () => floor.revision,
    reportRuntimeRevision: (revision) => reportRevision("S-a", revision),
  });

  // 1. 等到重同步自己的直读已经发出。
  await runtimeDispatched.promise;
  // 2. 直读拿到 r9——早于背景 poll 即将观测到的 r10。
  runtimeGate.resolve(runtimeAt("S-a", 0, 0, 9));
  // 3. 等到 statusAfter 也已经发出：这意味着 runtime 那一路已经跑完，事务内部
  //    的 reportRuntimeRevision(9) 已经同步落地（floor: 8 → 9）。
  await statusAfterDispatched.promise;
  assert.equal(floor.revision, 9);
  // 4. 这时"背景 poll"才在它自己独立的 continuation 里解出 r10 并同步棘轮——
  //    完全不经过任何 React 渲染，纯粹是另一个 Promise 决议后的同步赋值，正是
  //    useSessionRuntime 里 reportRevision 在 poll continuation 内扮演的角色。
  reportRevision("S-a", 10);
  assert.equal(floor.revision, 10, "poll 的棘轮必须在它自己的 continuation 内同步完成，不等任何渲染");
  // 5. 才放行 statusAfter；事务这时候才做最后核验，读到的已经是 10。
  statusAfterGate.resolve({ serverOwned: false, stateRevision: 1 });
  const result = await outcome;
  assert.equal(result.kind, "failed", "r9 早于已经同步棘轮的 r10，必须拒绝，不得 hydrate/unlock");
});
