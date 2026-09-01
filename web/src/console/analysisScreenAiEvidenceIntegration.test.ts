import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const apiSource = readFileSync(new URL("../api.ts", import.meta.url), "utf8");
const screenSource = readFileSync(new URL("./AnalysisScreen.tsx", import.meta.url), "utf8");

function sliceBetween(source: string, startNeedle: string, endNeedle: string, from = 0): string {
  const start = source.indexOf(startNeedle, from);
  assert.notEqual(start, -1, `缺少「${startNeedle}」`);
  const end = source.indexOf(endNeedle, start);
  assert.notEqual(end, -1, `缺少「${endNeedle}」（起点之后）`);
  return source.slice(start, end);
}

test("api.ts: sessionJournal is exact-session no-store and accepts an abort signal, not the old cached-by-default signature", () => {
  const block = sliceBetween(apiSource, "sessionJournal: (sid: string", "confirmation_revisions: unknown;\n  }>(");
  assert.match(block, /signal\?: AbortSignal/);
  const afterHeader = apiSource.slice(apiSource.indexOf("confirmation_revisions: unknown;\n  }>("));
  const call = afterHeader.slice(0, afterHeader.indexOf(");"));
  assert.match(call, /noStore: true, signal/);
});

test("api.ts: getSessionAiUsage follows the req<unknown>+parser pattern with no-store and an abort signal", () => {
  const block = sliceBetween(apiSource, "getSessionAiUsage:", "),\n  ");
  assert.match(block, /parseSessionAiUsage\(await req<unknown>\(/);
  assert.match(block, /noStore: true, signal/);
  assert.match(block, /ai-usage/);
});

test("AnalysisScreen: SessionAnalysis is force-remounted per exact patient+session via key, not reused across sessions", () => {
  const block = sliceBetween(screenSource, "<SessionAnalysis", "/>");
  assert.match(block, /key=\{`\$\{patientId\}:\$\{sessionId\}`\}/);
});

test("AnalysisScreen: the journal fetch is fenced by an AbortController, resets prior state, and cleans up on session change", () => {
  const block = sliceBetween(screenSource, "useEffect(() => {\n    let current = true;", "}, [sessionId, patientId]);");
  assert.match(block, /new AbortController\(\)/);
  assert.match(block, /setData\(null\)/);
  assert.match(block, /setErr\(null\)/);
  assert.match(block, /setContractError\(null\)/);
  assert.match(block, /api\.sessionJournal\(sessionId, controller\.signal\)/);
  assert.match(block, /return \(\) => \{\s*current = false;\s*controller\.abort\(\);\s*\};/);
});

test("AnalysisScreen: a journal response is fenced to the exact requested session id before it is trusted", () => {
  const block = sliceBetween(screenSource, "api.sessionJournal(sessionId, controller.signal)", "setData({");
  assert.match(block, /if \(!current\) return;/);
  assert.match(block, /j\.session\.session_id !== sessionId/);
});

test("AnalysisScreen: a non-tombstone journal response must also match the current patientId, not just the session id", () => {
  const block = sliceBetween(screenSource, "api.sessionJournal(sessionId, controller.signal)", "setData({");
  assert.match(block, /content_state !== "withdrawn_tombstone" && j\.session\.patient_id !== patientId/);
});

test("AnalysisScreen: /ai-usage is fetched independently per session with its own AbortController and cleanup", () => {
  const block = sliceBetween(screenSource, "const [aiUsageState, setAiUsageState]", "}, [sessionId]);");
  assert.match(block, /new AbortController\(\)/);
  assert.match(block, /api\.getSessionAiUsage\(sessionId, controller\.signal\)/);
  assert.match(block, /if \(!current\) return;/);
  assert.match(block, /return \(\) => \{\s*current = false;\s*controller\.abort\(\);\s*\};/);
});

test("AnalysisScreen: ai-usage error handling distinguishes withdrawn (409 subject_withdrawn_content_unavailable) from forbidden (403) and generic network errors", () => {
  const block = sliceBetween(screenSource, "const [aiUsageState, setAiUsageState]", "}, [sessionId]);");
  assert.match(block, /subject_withdrawn_content_unavailable/);
  assert.match(block, /error\.status === 409/);
  assert.match(block, /error\.status === 403/);
  assert.match(block, /status: "network-error"/);
  assert.match(block, /status: "contract-error"/);
});

test("AnalysisScreen: withdrawn tombstones never feed buildEvidenceTimeline and never render the normal week/phase header", () => {
  const sessionAnalysisStart = screenSource.indexOf("function SessionAnalysis(");
  assert.notEqual(sessionAnalysisStart, -1, "缺少 SessionAnalysis 组件");
  const withdrawnDecl = screenSource.indexOf(
    'const withdrawn = data?.session.content_state === "withdrawn_tombstone";', sessionAnalysisStart,
  );
  assert.notEqual(withdrawnDecl, -1, "缺少 withdrawn 派生");
  const timelineDecl = screenSource.slice(withdrawnDecl, withdrawnDecl + 400);
  assert.match(timelineDecl, /const timeline = data && !withdrawn \? buildEvidenceTimeline\(data\) : null;/);
  const header = sliceBetween(screenSource, "<h2 className=\"page-title\">", "</h2>", withdrawnDecl);
  assert.match(header, /withdrawn \? "场次内容已撤回"/);
});

test("AnalysisScreen: the withdrawn banner itself never leaks a '0 次' zero-count claim, and the normal stat/section block only renders when !withdrawn", () => {
  const banner = sliceBetween(screenSource, "{withdrawn && (", "{!withdrawn && (");
  assert.match(banner, /受试者内容已撤回/);
  assert.doesNotMatch(banner, /0 次/);
  assert.doesNotMatch(banner, /timeline\.summary/);
  const normalBlock = sliceBetween(screenSource, "{!withdrawn && (", "</>\n      )}");
  assert.match(normalBlock, /本场证据概览/);
  assert.match(normalBlock, /TtsServeEvidenceSectionView/);
  assert.match(normalBlock, /AiUsageSectionView/);
});

test("AnalysisScreen: the TTS serve list uses the responsive evidence-detail-grid card pattern, not the fixed 6-column registry-row", () => {
  const block = sliceBetween(screenSource, "export function TtsServeEvidenceSectionView(", "\n}\n");
  assert.doesNotMatch(block, /registry-row/);
  assert.match(block, /evidence-attempt-section/);
  assert.match(block, /evidence-detail-grid/);
});

test("AnalysisScreen: the AI usage section only renders once the journal has verified this exact session (not merely once its own fetch resolves)", () => {
  assert.match(screenSource, /\{data && <AiUsageSectionView state=\{aiUsageState\} \/>\}/);
});

test("AnalysisScreen: a confirmation-revision contract violation surfaces a page-level danger alert, not a silent disappearance", () => {
  const block = sliceBetween(screenSource, 'confirmationSection?.status === "contract-error"', "</Alert>\n          )}");
  assert.match(block, /修订历史无法核实/);
  assert.match(block, /confirmationSection\.message/);
  assert.match(block, /人工确认文本与锁分仍可查看/);
});

test("AnalysisScreen: TTS evidence and confirmation revisions run through the strict fail-closed parsers, not a TypeScript cast", () => {
  assert.match(screenSource, /parseTtsServeEvidenceList\(data\.ttsServesRaw, data\.session\.session_id, data\.session\.is_simulation\)/);
  assert.match(screenSource, /parseConfirmationRevisionsByTurn\(\s*data\.confirmationRevisionsRaw,/);
});

test("AnalysisScreen: parser failures for TTS/confirmation sections degrade to an isolated contract-error state, not a thrown render crash", () => {
  const ttsBlock = sliceBetween(screenSource, "const ttsSection: TtsServeEvidenceSectionViewModel | null = useMemo(", "}, [data, withdrawn]);");
  assert.match(ttsBlock, /catch \(error\) \{/);
  assert.match(ttsBlock, /status: "contract-error"/);
  const confirmBlock = sliceBetween(screenSource, "const confirmationSection: ConfirmationRevisionSectionViewModel | null = useMemo(", "}, [data, withdrawn]);");
  assert.match(confirmBlock, /catch \(error\) \{/);
  assert.match(confirmBlock, /status: "contract-error"/);
});

test("AnalysisScreen: the AI-usage drift note is present so short-lived cross-snapshot count drift is not read as data damage", () => {
  assert.match(screenSource, /本场实际调用 AI 服务的次数汇总/);
  assert.match(screenSource, /此处计数可能与上方略有延迟差异/);
});

test("AnalysisScreen: 旧后端 journal 无 rapport_utterances 键时按无记录隐藏,不渲染契约红警", () => {
  const block = sliceBetween(screenSource, "const rapportSection", "}, [data, withdrawn]);");
  assert.match(block, /rapportUtterancesRaw === undefined\) return \{ status: "empty" \}/);
});
