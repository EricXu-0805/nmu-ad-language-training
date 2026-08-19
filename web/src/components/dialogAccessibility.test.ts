import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

function source(path: string): string {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

test("all bedside governance dialogs share keyboard focus containment", () => {
  const confirmDialog = source("./ConfirmDialog.tsx");
  const pinPrompt = source("./PinPrompt.tsx");
  const withdrawal = source("../console/SubjectWithdrawalPanel.tsx");
  const scaleDrawer = source("../console/ScaleDrawer.tsx");
  const abnormalDrawer = source("../console/abnormal/AbnormalDrawer.tsx");
  const focusTrap = source("./useDialogFocusTrap.ts");

  assert.match(focusTrap, /event\.key === "Escape"/);
  assert.match(focusTrap, /event\.key !== "Tab"/);
  assert.match(focusTrap, /target\?\.isConnected/);
  assert.match(focusTrap, /"summary"/);
  assert.match(confirmDialog, /useDialogFocusTrap<HTMLDivElement>/);
  assert.match(pinPrompt, /useDialogFocusTrap<HTMLFormElement>/);
  assert.match(withdrawal, /useDialogFocusTrap<HTMLElement>/);
  assert.match(scaleDrawer, /useDialogFocusTrap<HTMLElement>/);
  assert.match(abnormalDrawer, /useDialogFocusTrap<HTMLElement>/);
});

test("abnormal drawer cannot be dismissed while its irreversible write is pending", () => {
  const abnormalDrawer = source("../console/abnormal/AbnormalDrawer.tsx");

  assert.match(abnormalDrawer, /const cancelSafely = \(\) => \{ if \(!busy\) onClose\(\); \};/);
  assert.match(abnormalDrawer, /onCancel: cancelSafely/);
  assert.match(abnormalDrawer, /className="drawer-backdrop" onClick=\{cancelSafely\}/);
  assert.match(abnormalDrawer, /aria-busy=\{busy \|\| undefined\} tabIndex=\{-1\}/);
  assert.match(abnormalDrawer, /<Button disabled=\{busy\} onClick=\{cancelSafely\}>关闭<\/Button>/);
});

test("rich confirmation content is never placed inside a paragraph", () => {
  const confirmDialog = source("./ConfirmDialog.tsx");
  assert.doesNotMatch(confirmDialog, /<p className="muted">\{body\}<\/p>/);
  assert.match(confirmDialog, /<div className="muted">\{body\}<\/div>/);
});

test("research withdrawal uses one modal surface for its two confirmation steps", () => {
  const withdrawal = source("../console/SubjectWithdrawalPanel.tsx");
  assert.doesNotMatch(withdrawal, /<ConfirmDialog\b/);
  assert.match(withdrawal, /confirmOpen \? "最终确认：受试者退出整个研究？"/);
  assert.match(withdrawal, /返回核对/);
});

test("bedside direction and manual-mode explanation match the rendered controls", () => {
  const patientStage = source("../patient/PatientAutopilotStage.tsx");
  const trainingConsole = source("../console/scoring/TrainingConsoleScreen.tsx");

  assert.match(patientStage, /右上角的「朗读」开关/);
  assert.doesNotMatch(patientStage, /右下角/);
  // 2026-08-19 UI 整改:页头说明由整段机制解释改为随 observerMode 切换的两句短句,
  // 钉「屏上说明与真实控制权模式一致」——两个分支都必须在。
  assert.match(trainingConsole, /observerMode \? "AI 正在自动带练，你可以随时安全暂停并接管。" : "当前为人工模式，由你逐步操作。"/);
});

test("research review identity and evidence context are server-owned", () => {
  const trainingConsole = source("../console/scoring/TrainingConsoleScreen.tsx");
  const researchReview = source("../console/ResearchReviewPanel.tsx");
  const api = source("../api.ts");

  // 2026-08-19 UI 整改删除了「复核身份由服务器签发」免责 pill;
  // 身份服务器所有的真正保护是下面三条负钉 + api.lockTurn 载荷形状,全部保留。
  assert.doesNotMatch(trainingConsole, /启用辅助初评|完全由人工判分|评分人/);
  assert.doesNotMatch(trainingConsole, /reviewer_id\s*:/);
  assert.doesNotMatch(researchReview, /reviewer_id\s*:/);
  assert.match(api, /lockTurn: \(turnId: number, body: \{ element_value: number \}\)/);
});
