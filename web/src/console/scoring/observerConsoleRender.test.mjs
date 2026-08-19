import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

// 观察台锁定期间绝不允许出现在 DOM 里的人工逐步操作文案。
const FORBIDDEN_MANUAL_CONTROLS = [
  "上一环节",
  "下一环节",
  "发送轻提示",
  "发送明确提示",
  "发送提示",
  "告知答案",
  "开始老人端录音",
  "AI 转写并登记证据",
  "冻结已核验转写",
  "保存人工确认",
  "确认锁定",
  "进入场次收尾",
];

const LOCKED_PHASES = [
  "checking", "starting", "uncertain", "running", "waiting_tts",
  "waiting_recording", "processing_attempt", "manual_draining",
  "paused", "scope_completed", "failed",
];

const POSITION = {
  itemOrdinal: 3,
  itemTotal: 12,
  turnOrdinal: 1,
  turnTotal: 2,
  itemLabel: "胡萝卜",
  taskType: "单要素",
  responseRole: "命名",
};

test("real observer console markup shows server truth and mounts no manual step controls", async (context) => {
  const vite = await createServer({
    root: process.cwd(),
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  context.after(() => vite.close());
  const { ObserverConsole } = await vite.ssrLoadModule(
    "/src/console/scoring/ObserverConsole.tsx",
  );

  for (const phase of LOCKED_PHASES) {
    const markup = renderToStaticMarkup(React.createElement(ObserverConsole, {
      patientCode: "SIM-001",
      phase,
      resyncStatus: "idle",
      resyncError: null,
      position: POSITION,
      onRetryResync: () => {},
    }));
    for (const control of FORBIDDEN_MANUAL_CONTROLS) {
      assert.equal(markup.includes(control), false, `${phase} 泄漏了人工控件「${control}」`);
    }
    assert.match(markup, /受试者编号 SIM-001/);
    assert.match(markup, /当前任务：胡萝卜 · 命名/);
    assert.match(markup, /计划位置：第 3\/12 题 · 环节 1\/2/);
    assert.match(markup, /此进度仅供观察，评分以事后复核为准/);
  }

  // 「服务端已证实拥有」与「为安全锁定但尚未证实」必须可区分。
  const running = renderToStaticMarkup(React.createElement(ObserverConsole, {
    patientCode: "SIM-001", phase: "running", resyncStatus: "idle",
    resyncError: null, position: POSITION, onRetryResync: () => {},
  }));
  assert.match(running, /AI 控制中/);
  assert.match(running, /服务端托管执行中/);
  assert.match(running, /status-pill--ok/);
  for (const phase of ["checking", "starting", "uncertain"]) {
    const markup = renderToStaticMarkup(React.createElement(ObserverConsole, {
      patientCode: "SIM-001", phase, resyncStatus: "idle",
      resyncError: null, position: POSITION, onRetryResync: () => {},
    }));
    assert.match(markup, /等待服务器确认/, phase);
    assert.doesNotMatch(markup, /AI 控制中|服务端托管执行中/, phase);
  }
  const uncertain = renderToStaticMarkup(React.createElement(ObserverConsole, {
    patientCode: "SIM-001", phase: "uncertain", resyncStatus: "idle",
    resyncError: null, position: POSITION, onRetryResync: () => {},
  }));
  assert.match(uncertain, /状态待核实/);
  // 未证实且服务器状态互相矛盾：呈 danger，不是笼统的 warn。
  assert.match(uncertain, /status-pill--danger/);
  const starting = renderToStaticMarkup(React.createElement(ObserverConsole, {
    patientCode: "SIM-001", phase: "starting", resyncStatus: "idle",
    resyncError: null, position: POSITION, onRetryResync: () => {},
  }));
  assert.match(starting, /正在等待服务器启动确认/);

  // 位置未同步时显示待同步，不显示估计进度。
  const unsynced = renderToStaticMarkup(React.createElement(ObserverConsole, {
    patientCode: "SIM-001", phase: "waiting_recording", resyncStatus: "idle",
    resyncError: null, position: null, onRetryResync: () => {},
  }));
  assert.match(unsynced, /进度同步中/);
  assert.doesNotMatch(unsynced, /计划位置：第/);

  // 重同步中与失败：保持锁定叙述；失败提供重试且仍不出现人工控件。
  const pending = renderToStaticMarkup(React.createElement(ObserverConsole, {
    patientCode: "SIM-001", phase: "idle", resyncStatus: "pending",
    resyncError: null, position: POSITION, onRetryResync: () => {},
  }));
  assert.match(pending, /正在恢复人工控制/);
  const failed = renderToStaticMarkup(React.createElement(ObserverConsole, {
    patientCode: "SIM-001", phase: "idle", resyncStatus: "failed",
    resyncError: "网络中断", position: POSITION, onRetryResync: () => {},
  }));
  assert.match(failed, /恢复人工控制失败/);
  assert.match(failed, /网络中断/);
  // 重同步失败：呈 danger，不是笼统的 warn。
  assert.match(failed, /status-pill--danger/);
  assert.match(failed, /重试恢复人工控制/);
  for (const control of FORBIDDEN_MANUAL_CONTROLS) {
    assert.equal(pending.includes(control), false, `重同步中泄漏「${control}」`);
    assert.equal(failed.includes(control), false, `重同步失败泄漏「${control}」`);
  }
});
