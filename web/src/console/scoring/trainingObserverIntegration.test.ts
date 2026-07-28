import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("./TrainingConsoleScreen.tsx", import.meta.url), "utf8");

function indexOfOrFail(haystack: string, needle: string, from = 0): number {
  const index = haystack.indexOf(needle, from);
  assert.notEqual(index, -1, `TrainingConsoleScreen 缺少「${needle}」`);
  return index;
}

test("observer lock is derived from the shared ownership state plus the synchronous release latch, not a shadow machine", () => {
  // releaseNeedsResync.current 必须直接参与锁定判定——不能只是 effect 的触发
  // 信号,否则 released 到 manualResync.status 真正提交为 pending 之间，会有
  // 一次 observerMode=false 的渲染。
  assert.match(source, /const observerMode = manualSurfaceLocked\(serverOwnership, manualResync\.status\) \|\| releaseNeedsResync\.current;/);
  assert.match(source, /const manualInteractionBlocked = interactionBlocked \|\| observerMode;/);
  // exposure 判定是所有权回调内同步调用一次的纯函数，不是靠渲染重算的影子状态。
  assert.match(source, /deriveOwnershipTransition\(\s*automationExposure\.current,\s*next,\s*manualResyncStatusRef\.current === "idle",\s*\)/);
  // 下降沿判定用的是回调里逐次捕获的 previousOwned，不是只在 effect 里更新的
  // 渲染级快照。
  assert.match(source, /manualResyncRequired\(previousOwned, next, transition\.automationExposure\)/);
});

test("observer mode unmounts the item rail, manual toolbar, wrapup entry, and work card", () => {
  const railGate = indexOfOrFail(source, "{!observerMode && (");
  assert.ok(indexOfOrFail(source, "<ItemRail", railGate) - railGate < 120,
    "ItemRail 必须由 !observerMode 直接守门");
  const ternary = indexOfOrFail(source, "{observerMode ? (");
  const observerUse = indexOfOrFail(source, "<ObserverConsole", ternary);
  const elseBranch = indexOfOrFail(source, ") : (", observerUse);
  // 人工工具栏与完整 training-work-card 都只能出现在 else 分支之后。
  const toolbar = indexOfOrFail(source, "toolbar training-toolbar");
  const workCard = indexOfOrFail(source, "training-work-card");
  assert.ok(toolbar > elseBranch, "人工工具栏必须在观察台 else 分支内");
  assert.ok(workCard > elseBranch, "training-work-card 必须在观察台 else 分支内");
  assert.equal(source.split("training-work-card").length, 2, "work card 只能有一个挂载点");
  const wrapupButton = indexOfOrFail(source, "进入场次收尾");
  const wrapupGate = source.lastIndexOf("{!observerMode && (", wrapupButton);
  assert.ok(wrapupButton - wrapupGate < 260, "进入场次收尾按钮必须由 !observerMode 守门");
});

test("manual step controls still exist for the proven no-owner path, outside the observer surface", () => {
  const elseBranch = indexOfOrFail(source, ") : (", indexOfOrFail(source, "<ObserverConsole"));
  const observerSource = readFileSync(new URL("./ObserverConsole.tsx", import.meta.url), "utf8");
  for (const control of [
    "上一环节", "下一环节", "发送轻提示", "发送明确提示", "告知答案",
    "开始老人端录音", "AI 转写并登记证据", "冻结已核验转写", "保存人工确认",
  ]) {
    // 人工分支（及其后定义的工作卡子组件）仍保留控件——观察台没有把工作台整个删掉。
    assert.notEqual(source.indexOf(control, elseBranch), -1,
      `人工分支不再包含「${control}」，工作台可能被误删`);
    assert.equal(observerSource.includes(control), false,
      `观察台组件不得包含人工控件「${control}」`);
  }
});

test("observer position and identity come from the exact-session runtime, not local indexes", () => {
  assert.match(source, /position=\{observerPlanPosition\(runtimeControl\.runtime, session\.session_id, plan\)\}/);
  assert.match(source, /patientCode=\{session\.patient_id\}/);
  assert.match(source, /phase=\{serverOwnership\.phase\}/);
});

test("regaining manual control never reuses the shared runtime poll, and is fenced per session, epoch, and live ownership", () => {
  const body = source.slice(
    indexOfOrFail(source, "const runManualResync = async ()"),
    indexOfOrFail(source, "// 观察台锁定/解锁的唯一协调点"),
  );
  // 恢复真值绝不能来自 useSessionRuntime 里那个可能复用旧 in-flight poll 的 refresh()。
  assert.doesNotMatch(body, /runtimeControl\.refresh\(\)/);
  assert.match(body, /fetchStatus: \(sid\) => api\.autopilotStatus\(sid\)/);
  assert.match(body, /fetchRuntime: \(sid\) => api\.getSessionRuntime\(sid\)/);
  assert.match(body, /fetchJournal: \(sid\) => api\.sessionJournal\(sid\)/);
  // revision 下限读自 useSessionRuntime 那个 hook 内、poll/pause/resume 回执
  // 各自 continuation 里同步维护的 ref——不是这个组件另开一份、要等渲染才更新
  // 的副本，也不是这次闭包捕获的某个快照。
  assert.match(body, /getRuntimeRevisionFloor: \(\) => runtimeControl\.getRevisionFloor\(\)/);
  assert.match(body, /reportRuntimeRevision: \(revision\) => runtimeControl\.reportRevision\(fence\.sessionId, revision\)/);
  // 已经重新证明持有：不发任何请求，直接短路。
  assert.match(body, /if \(ownershipRef\.current\.owned\) return;/);
  // guard 同时核对 fence 与"此刻"的所有权 ref，不是只信 fence 一项。
  assert.match(body, /isLive: \(\) => observerResyncResultCurrent\(fence, resyncFence\.current\) && !ownershipRef\.current\.owned/);
  // apply（hydrate/改题位）前必须再核一次 guard，不能只信事务内部最后一次核对；
  // 同样，还要再核一次 outcome.runtimeRevision 是否仍不低于当下最新的下限——
  // 事务内部最后一次核对与这里续行之间还隔着一次微任务调度，读的必须是
  // runtimeControl.getRevisionFloor() 这个 hook 级共享下限，不是本地快照。
  const hydrateIdx = indexOfOrFail(body, "hydrateFromServer(outcome.journal)");
  const lastGuardBeforeHydrate = body.lastIndexOf("if (!guard.isLive()) return;", hydrateIdx);
  assert.ok(lastGuardBeforeHydrate !== -1 && lastGuardBeforeHydrate < hydrateIdx,
    "hydrate 前必须再核一次 guard.isLive()");
  const floorRecheck = body.lastIndexOf("outcome.runtimeRevision < runtimeControl.getRevisionFloor()", hydrateIdx);
  assert.ok(floorRecheck !== -1 && lastGuardBeforeHydrate < floorRecheck && floorRecheck < hydrateIdx,
    "hydrate 前必须用当下最新的下限再核一次 outcome.runtimeRevision");
  assert.match(body, /lastAppliedTurnK\.current = null/);
  assert.match(body, /status: "failed"/);
  // 重同步期间不写游标、不启动录音、不动本地自动驾驶。
  assert.doesNotMatch(body, /postCursor\(/);
  assert.doesNotMatch(body, /armRecording|setRecState\("armed"\)|persistAutoPilot/);
  // 这个组件自己不该再另开一份 revision floor ref——单一事实源在 hook 里。
  assert.doesNotMatch(source, /runtimeRevisionFloorRef/);
});

test("resync applies journal and position only after the exact cursor is proven", () => {
  const body = source.slice(
    indexOfOrFail(source, "const runManualResync = async ()"),
    indexOfOrFail(source, "// 观察台锁定/解锁的唯一协调点"),
  );
  // failed 分支必须先于 hydrate 处理：失败不留下部分应用的 journal 或位置。
  const failedBranch = indexOfOrFail(body, 'if (outcome.kind === "failed")');
  const hydrate = indexOfOrFail(body, "hydrateFromServer(outcome.journal)");
  assert.ok(failedBranch < hydrate, "failed 分支必须先于 hydrate 处理");
  assert.ok(hydrate < indexOfOrFail(body, "setItemIdx(outcome.cursor.itemIdx)"));
  // 不允许默认到 0、clamp 或用本地 itemIdx/turnIdx 猜位置。
  assert.doesNotMatch(body, /\?\? 0/);
  assert.doesNotMatch(body, /Math\.(min|max)\(/);
  assert.doesNotMatch(body, /setItemIdx\(itemIdx\)|setTurnIdx\(turnIdx\)/);

  // 精确 cursor 与 plan 校验的真正落点在 observerResync.ts 的纯事务里：计划缺失
  // 与 cursor 不可证明都必须 failed，且都先于 applied 结果。
  const resyncSource = readFileSync(new URL("./observerResync.ts", import.meta.url), "utf8");
  const planGuard = indexOfOrFail(resyncSource, "if (!plan) {");
  const cursorGuard = indexOfOrFail(resyncSource, "exactPlanCursor(runtime, sessionId, plan)");
  const cursorReject = indexOfOrFail(resyncSource, "if (!cursor) {");
  const applied = indexOfOrFail(resyncSource, 'return { kind: "applied", cursor, journal, runtimeRevision: runtime.revision };');
  assert.ok(planGuard < applied && cursorGuard < applied && cursorReject < applied,
    "plan 与精确 cursor 校验必须全部先于 applied 结果");
  assert.doesNotMatch(resyncSource, /\?\? 0/);
  assert.doesNotMatch(resyncSource, /Math\.(min|max)\(/);
});

test("the ownership callback invalidates a pending resync synchronously, not via an effect", () => {
  const body = source.slice(
    indexOfOrFail(source, "const onServerOwnershipChange = useCallback("),
    indexOfOrFail(source, "// 从 owned/uncertain 回到明确 no-owner"),
  );
  // previousOwned 必须在覆写 ownershipRef 之前捕获——它是这一次调用的"上一次
  // 调用时"快照，不是渲染批处理后才能看到的最终 state。
  const previousOwnedCapture = indexOfOrFail(body, "const previousOwned = ownershipRef.current.owned;");
  const refWrite = indexOfOrFail(body, "ownershipRef.current = next;");
  const transition = indexOfOrFail(body, "deriveOwnershipTransition(");
  assert.ok(previousOwnedCapture < refWrite && refWrite < transition,
    "previousOwned 必须先于 ownershipRef 覆写，ownershipRef 必须在算出 transition 之前就同步落地");
  // 作废动作（fence 前进 + 重同步打回 idle）都在同一个回调体内完成，
  // 不依赖 serverOwnership 这个 state 触发的下一次 effect。
  const invalidateGate = indexOfOrFail(body, "if (transition.invalidateResync) {");
  const epochBump = indexOfOrFail(body, "resyncFence.current = { ...resyncFence.current, epoch: resyncFence.current.epoch + 1 };", invalidateGate);
  const resetIdle = indexOfOrFail(body, 'setManualResync({ status: "idle", error: null });', invalidateGate);
  const setState = indexOfOrFail(body, "setServerOwnership(next);");
  assert.ok(invalidateGate < epochBump && epochBump < setState && resetIdle < setState,
    "fence 作废与 resync 复位必须先于 setServerOwnership，回调体内一次做完");
  // 释放且需要恢复的那条分支：previousOwned 驱动的下降沿判定 + 同步 latch
  // releaseNeedsResync + 同步把 manualResync 打成 pending——真正发起请求交给
  // 下面的 effect，但"要不要锁、锁不锁得住"这件事在这里就已经落定。
  const triggerGate = indexOfOrFail(body, "manualResyncRequired(previousOwned, next, transition.automationExposure)");
  const latch = indexOfOrFail(body, "releaseNeedsResync.current = true;", triggerGate);
  const pendingSet = indexOfOrFail(body, 'setManualResync({ status: "pending", error: null });', triggerGate);
  assert.ok(triggerGate < latch && latch < pendingSet && pendingSet < setState,
    "下降沿判定为真时必须同步 latch releaseNeedsResync 并同步打成 pending，都先于 setServerOwnership");
});

test("a stale local cursor cannot be posted during the release render — the cursor-sync effect is gated by manualInteractionBlocked in both its guard and its deps", () => {
  // 这条被动 effect 一旦在 observerMode 尚未反映为锁定的那次渲染里跑过,就会
  // 用本地旧 itemIdx/turnIdx 抢先给服务器补发一次游标。它必须直接依赖
  // manualInteractionBlocked（而 manualInteractionBlocked 已经把
  // releaseNeedsResync.current 算进 observerMode 了）,不能只依赖题位本身。
  const effectStart = indexOfOrFail(source, "光标变化 → 重置工作态");
  const guardIdx = indexOfOrFail(source, "if (!item || !planTurn || manualInteractionBlocked) return;", effectStart);
  const depsIdx = indexOfOrFail(source, "[itemIdx, turnIdx, plan, manualInteractionBlocked]", guardIdx);
  assert.ok(guardIdx > effectStart && depsIdx > guardIdx, "该 effect 必须同时在守卫与依赖数组里带上 manualInteractionBlocked");
});

test("session switch clears transient observer state and no new poller is introduced", () => {
  assert.match(source, /resyncFence\.current\.sessionId !== session\.session_id/);
  assert.match(source, /automationExposure\.current = false;/);
  // previousOwned 的来源（ownershipRef）与释放-恢复 latch 都要在换场时清零，
  // 不能带着旧场次的快照进新场次。
  assert.match(source, /ownershipRef\.current = \{ owned: true, phase: "checking" \};/);
  assert.match(source, /releaseNeedsResync\.current = false;/);
  // 唯一的 setInterval 仍是既有录音授权周期核查；观察台不新增轮询器。
  assert.equal((source.match(/setInterval/g) ?? []).length, 1);
  // 恢复继续走既有 runtime 轮询与恢复门，不额外常驻请求。
  assert.match(source, /resumeBlocked=\{observerMode \|\| Boolean\(apFailure\)\}/);
});
