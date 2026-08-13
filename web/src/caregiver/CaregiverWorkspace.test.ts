import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("./CaregiverWorkspace.tsx", import.meta.url), "utf8");

test("caregiver workspace receives an injected API and cannot import research or patient workspaces", () => {
  assert.match(source, /api:\s*CaregiverApi/);
  assert.doesNotMatch(source, /from\s+["']\.\.\/(?:api|types|console|patient)(?:\/|["'])/);
  assert.doesNotMatch(source, /TrainingConsoleScreen|ConsoleWorkspace|AnalysisScreen|SubjectRegistryScreen/);
});

test("pause stays available during status re-check and uses the bodyless safety endpoint", () => {
  assert.match(source, /const showPause = !status \|\| status\.runtimeState === "active"/);
  assert.match(source, /api\.pauseSession\(current\.sessionId\)/);
  assert.doesNotMatch(source, /api\.pauseSession\(current\.sessionId,\s*\{/);
});

test("takeover stays disabled until the exact server drain proof is ready", () => {
  assert.match(source, /const actions = status \? caregiverActionAvailability\(status\) : null/);
  assert.match(source, /disabled=\{busy \|\| !actions\?\.takeOver\}/);
  assert.match(source, /if \(!actions\.takeOver\) return/);
});

test("caregiver workspace contains only the approved bedside capabilities", () => {
  for (const label of [
    "今天任务",
    "开始本次",
    "开始练习",
    "暂停练习",
    "请求协助",
    "接管练习",
    "结束本次",
    "退出",
  ]) {
    assert.match(source, new RegExp(label));
  }
  for (const forbidden of ["恢复练习", "resumeSession", "completeSession", "研究工作台", "分析后台", "导出数据", "评分管理"]) {
    assert.doesNotMatch(source, new RegExp(forbidden, "i"));
  }
});

test("patient view remains a separate-window instruction rather than a mounted overlay", () => {
  assert.match(source, /两个窗口都要保持打开/);
  assert.match(source, /老人画面应单独打开在另一个窗口/);
  assert.doesNotMatch(source, /PATIENT_VIEW_EVENT|PatientViewHost|PatientShell/);
});

test("help copy never claims that another person received the local record", () => {
  assert.match(source, /本机已记录/);
  assert.match(source, /不代表对方已收到消息/);
  assert.doesNotMatch(source, /已送达|对方已收到(?!消息)/);
});

test("the local synthetic boundary stays visible in the header, alert, and every plan", () => {
  assert.match(source, /<strong>本机20题合成模拟<\/strong>/);
  assert.match(source, /CAREGIVER_DEMO_BOUNDARY_MESSAGE/);
  assert.match(source, /<StatusPill tone="warn">本机20题合成模拟<\/StatusPill>/);
  assert.match(source, /today\.withheldCount > 0/);
  assert.match(source, /有安排未通过模拟门禁/);
  assert.match(source, /照护员不能在本页开始/);
});

test("a non-ready current session exposes only safe closeout and never renders start", () => {
  assert.match(
    source,
    /const operationalDemoReady = session\.operationalDemoReady\s*&& status\?\.operationalDemoReady === true/,
  );
  assert.match(source, /const safeCloseoutOnly = !session\.operationalDemoReady/);
  assert.match(source, /safeCloseoutOnly && \(/);
  assert.match(source, /仅可安全收口/);
  assert.match(source, /不会显示“开始练习”/);
  assert.match(source, /const showStart = operationalDemoReady/);
  assert.match(source, /if \(!current\.operationalDemoReady \|\| !actions\.startPractice\) return/);
});

test("provider readiness conflicts give caregivers a deterministic administrator action", () => {
  assert.match(source, /isProviderReadinessPrewriteConflict/);
  assert.match(
    source,
    /const PROVIDER_NOT_READY_MESSAGE = "语音服务未准备，请联系管理员。"/,
  );
  assert.equal(
    source.match(/setOperationProblem\(PROVIDER_NOT_READY_MESSAGE\)/g)?.length,
    2,
  );
  assert.match(
    source,
    /title=\{operationProblem === PROVIDER_NOT_READY_MESSAGE\s*\? "暂时不能开始"\s*:\s*"这一步还没有确认"\}/,
  );
  assert.match(source, /else \{\s*reportUnconfirmed\("开始本次"\)/);
  assert.match(source, /else \{\s*reportUnconfirmed\("开始练习"\)/);
});
