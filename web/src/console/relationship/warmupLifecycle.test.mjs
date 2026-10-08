import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
import { ApiError } from '../../apiResponse.ts';
import { warmupIdentity, warmupTargetIdentity } from '../../week1Warmup.ts';

const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const tick = () => new Promise((resolve) => setImmediate(resolve));
function frame() {
  const slots = []; let cursor = 0, pending = [];
  const hooks = {
    useRef(value) { const i = cursor++; return slots[i] ??= { current: value }; },
    useState(value) { const i = cursor++; if (!(i in slots)) slots[i] = typeof value === 'function' ? value() : value; return [slots[i], (update) => { slots[i] = typeof update === 'function' ? update(slots[i]) : update; }]; },
    useEffect(fn, deps) { const i = cursor++, previous = slots[i]; if (!previous || !deps || deps.some((v, k) => v !== previous.deps?.[k])) { const next = { deps, cleanup: previous?.cleanup }; slots[i] = next; pending.push(() => { next.cleanup?.(); next.cleanup = fn(); }); } },
  };
  hooks.useLayoutEffect = hooks.useEffect;
  return { hooks, render(fn) { cursor = 0; return fn(); }, flush() { const work = pending; pending = []; work.forEach((fn) => fn()); }, unmount() { for (const slot of slots) slot?.cleanup?.(); } };
}
const jsx = (type, props) => ({ type, props });
function load(relative, mocks, globals = {}) {
  const source = readFileSync(new URL(relative, import.meta.url), 'utf8');
  const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const exports = {};
  const ctx = vm.createContext({ exports, console, ...globals, require: (id) => { if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx }; assert(id in mocks, `unexpected dependency ${id}`); return mocks[id]; } });
  vm.runInContext(compiled, ctx);
  return exports;
}
const warmup = { versionId: 'wk1-warmup-v1', topic: 'weather', beat: 'ask', seq: 11 };
const step = { type: 'rapportStep', sessionId: 'SIM-WARMUP', sectionKey: '认识机器人', questionIdx: 0, recording: 'idle', beat: 'ask', wseq: 11, warmup };
const receipt = { seq: 12, wseq: 11, runtimeRevision: 4, rapportStep: step };
function consoleHarness(overrides = {}) {
  const f = frame(), writes = [], broadcasts = [], invalidations = [];
  let runtime = { sessionId: 'SIM-WARMUP', revision: 3, status: 'active', rapportStep: { ...step, wseq: 10, warmup: undefined } };
  const api = { week1WarmupContent: async () => ({ versionId: 'wk1-warmup-v1', topics: [] }), getSessionRuntime: async () => runtime, week1Warmup: (sid, body) => { writes.push({ sid, body }); return Promise.resolve(receipt); }, ...overrides };
  const hook = load('./useWeek1Warmup.ts', { react: f.hooks, '../../api': { api, ApiError }, '../../sync/bus': { bus: { post: (m) => broadcasts.push(m) } }, '../../sync/wseq': { observeWseq() {} }, '../../sync/useCursorWriter': { flushLiveWrites: async () => {} }, '../../week1Warmup': { warmupTargetIdentity } }, { crypto: { randomUUID: () => 'stable-uuid' } }).useWeek1Warmup;
  let current;
  const render = () => { current = f.render(() => hook('SIM-WARMUP', runtime)); return current; };
  render(); f.flush(); render();
  return { f, render, api, writes, broadcasts, invalidations, before: () => invalidations.push('cancel-old-formal'), get current() { return current; }, setRuntime(value) { runtime = value; } };
}
test('production warmup hook never broadcasts or activates before server acknowledgement; it locks same-frame double clicks', async () => {
  const pending = deferred();
  const h = consoleHarness();
  h.api.week1Warmup = (sid, body) => { h.writes.push({ sid, body }); return pending.promise; };
  const first = h.current.act('start', 'weather', h.before);
  const second = h.current.act('start', 'food', h.before);
  assert.equal(h.invalidations.length, 1);
  await tick(); assert.equal(h.writes.length, 1); assert.equal(h.broadcasts.length, 0);
  h.render(); assert.equal(h.current.warmup, undefined); assert.equal(h.current.blocking, true);
  pending.resolve(receipt); await first; await second; h.render();
  assert.equal(h.broadcasts.length, 1); assert.deepEqual(h.current.warmup, warmup);
});
test('unknown write outcome retries the exact original CAS body and key, and blocks new topic selection', async () => {
  const h = consoleHarness(); let attempt = 0;
  h.api.week1Warmup = async (sid, body) => { h.writes.push({ sid, body }); if (++attempt === 1) throw new Error('timeout'); return receipt; };
  await h.current.act('start', 'weather', h.before); h.render();
  assert.equal(h.current.uncertain, true); assert.equal(h.broadcasts.length, 0);
  await h.current.act('start', 'food', h.before); assert.equal(h.writes.length, 1);
  await h.current.retry(h.before); h.render();
  assert.equal(h.writes.length, 2); assert.equal(h.writes[0].body, h.writes[1].body);
  assert.equal(h.current.uncertain, false); assert.equal(h.broadcasts.length, 1);
});
for (const boundary of ['pause', 'unmount']) test(`late accepted warmup cannot publish after ${boundary}`, async () => {
  const pending = deferred(); const h = consoleHarness({ week1Warmup: () => pending.promise });
  const work = h.current.act('start', 'weather', h.before); await tick();
  if (boundary === 'pause') h.current.cancel(); else h.f.unmount();
  pending.resolve(receipt); assert.equal(await work, null); assert.equal(h.broadcasts.length, 0);
});
test('a rejected conflict does not turn into an endlessly retried stale warmup request', async () => {
  const h = consoleHarness({ week1Warmup: async () => { throw new ApiError(409, '位置已变化'); } });
  await h.current.act('start', 'weather', h.before); h.render();
  assert.equal(h.current.uncertain, false); assert.equal(h.current.blocking, false);
  assert.equal(await h.current.retry(h.before), null); assert.equal(h.broadcasts.length, 0);
});
test('newer runtime pause/done wins over a prior active warmup receipt', async () => {
  const h = consoleHarness(); await h.current.act('start', 'weather', h.before); h.render();
  h.setRuntime({ sessionId: 'SIM-WARMUP', revision: 5, status: 'paused', rapportStep: { ...step, wseq: 12, warmup: { ...warmup, beat: 'done', seq: 12 } } }); h.render();
  assert.equal(h.current.warmup.beat, 'done'); assert.equal(h.current.blocking, true);
});
function stageHarness() {
  const f = frame(), spoken = [], listeners = new Map(), timers = new Map(); let stopped = 0, immediate, settled;
  const projection = { mode: 'rapport', speaker: '机器人', text: '今天天气怎么样？', warmup };
  let result = { presentation: projection, error: null };
  const stage = load('../../patient/WarmupStage.tsx', {
    react: f.hooks, '../week1Warmup': { warmupIdentity }, './Centered': { Centered: 'Centered' },
    './usePatientPresentation': { usePatientPresentation: () => result },
    './tts': { speak: (text, options) => spoken.push({ text, options }), stopSpeaking: () => { stopped++; }, ttsEnabled: () => true, onSpeechSettled: (listener) => { settled = listener; return () => {}; } },
  }, {
    document: { visibilityState: 'visible', addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: (name) => listeners.delete(name) },
    window: { addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: (name) => listeners.delete(name), setTimeout: (fn) => { timers.set(1, fn); return 1; }, clearTimeout: (id) => timers.delete(id) },
  }).WarmupStage;
  const props = { sessionId: 'SIM-WARMUP', rapportStep: step, ttsContextKey: 'session:SIM-WARMUP|plane:warmup|seq:11', allowedIdentity: warmupIdentity(warmup), connectionReady: true, sessionPaused: false, registerImmediateDiscard: (fn) => { immediate = fn; } };
  const render = () => { const tree = f.render(() => stage(props)); f.flush(); return tree; };
  return { f, props, spoken, render, listeners, timers, setResult: (v) => { result = v; }, get stopped() { return stopped; }, stop: () => immediate(), settle: (outcome) => settled(spoken.at(-1)?.options.tag, outcome) };
}
test('production warmup stage waits for exact projected content and calls only fixed TTS, never microphone or legacy receipt APIs', () => {
  const h = stageHarness(); h.setResult({ presentation: null, error: null }); h.render(); assert.equal(h.spoken.length, 0);
  h.setResult({ presentation: { mode: 'rapport', speaker: '机器人', text: '今天天气怎么样？', warmup }, error: null }); h.render();
  assert.equal(h.spoken.length, 1); assert.equal(h.spoken[0].text, '今天天气怎么样？');
  h.render(); assert.equal(h.spoken.length, 1);
  h.settle('played'); h.render(); assert.equal(h.spoken.length, 1, 'played cannot advance or rearm anything');
});
test('production warmup stage displays restored content silently and consumes explicit replay once', () => {
  const h = stageHarness(); h.props.allowedIdentity = null; h.render(); assert.equal(h.spoken.length, 0);
  const next = { ...warmup, seq: 12 };
  h.props.rapportStep = { ...step, warmup: next, wseq: 12 }; h.props.allowedIdentity = warmupIdentity(next);
  h.setResult({ presentation: { mode: 'rapport', speaker: '机器人', text: '今天天气怎么样？', warmup: next }, error: null }); h.render();
  assert.equal(h.spoken.length, 1); h.render(); assert.equal(h.spoken.length, 1);
});
for (const boundary of ['pause', 'disconnect', 'local-stop', 'pagehide', 'unmount']) test(`production warmup speech stops at ${boundary}`, () => {
  const h = stageHarness(); h.render(); assert.equal(h.spoken.length, 1); const prior = h.stopped;
  if (boundary === 'pause') { h.props.sessionPaused = true; h.render(); }
  if (boundary === 'disconnect') { h.props.connectionReady = false; h.render(); }
  if (boundary === 'local-stop') h.stop();
  if (boundary === 'pagehide') h.listeners.get('pagehide')();
  if (boundary === 'unmount') h.f.unmount();
  assert(h.stopped > prior);
  if (boundary !== 'unmount') { h.props.sessionPaused = false; h.props.connectionReady = true; h.render(); assert.equal(h.spoken.length, 1); }
});

for (const action of ['start', 'reply', 'replay', 'end', 'continue']) test(`production ${action} refuses to rebind a click to a newer remote position`, async () => {
  const response = deferred();
  const h = consoleHarness({ getSessionRuntime: () => response.promise });
  const work = h.current.act(action, action === 'start' ? 'weather' : undefined, h.before);
  await tick();
  response.resolve({ sessionId: 'SIM-WARMUP', revision: 8, status: 'active', rapportStep: { ...step, wseq: 18, warmup: { ...warmup, topic: 'food', seq: 18 } } });
  assert.equal(await work, null); h.render();
  assert.equal(h.writes.length, 0); assert.equal(h.broadcasts.length, 0);
  assert.match(h.current.problem, /位置已变化/);
});

function recordingNoticeTree(optionalWarmup, recordingStatus = 'denied') {
  const f = frame();
  const hooks = { ...f.hooks, useEffect() {}, useState(initial) {
    return f.hooks.useState(initial === 'loading' ? recordingStatus : initial);
  } };
  const noop = () => {};
  const screen = load('./RelationshipConsoleScreen.tsx', {
    react: hooks,
    './Week1WarmupCard': { Week1WarmupCard: 'WarmupCard' },
    './useWeek1Warmup': { useWeek1Warmup: () => ({ warmup: optionalWarmup, blocking: Boolean(optionalWarmup) }) },
    './rapportRecordingAuthority': { receiptMatchesRapportArm: () => false },
    '../../hooks/runtimePollPolicy': {},
    '../../rapportPlayback': {},
    '../../api': { api: {}, ApiError },
    '../../components/Alert': { Alert: 'Alert' },
    '../../components/Button': { Button: 'Button' },
    '../../components/StatusPill': { StatusPill: 'StatusPill' },
    '../../components/ToastContext': { useToast: () => noop },
    '../../content/bundle': {
      useWeek1Script: () => ({ script: { sections: [{ key: '认识机器人', speaker: '机器人', questions: [{ ask: '您好' }] }] } }),
      useWeek1ReplyBank: () => ({ bank: null }),
    },
    '../../hooks/useSessionJournal': { useSessionJournal: () => ({ journal: { audios: {} } }) },
    '../../hooks/useSessionRuntime': { useSessionRuntime: () => ({ paused: false, terminal: false, runtime: { sessionId: 'SIM-WARMUP', revision: 4, rapportStep: { ...step, warmup: optionalWarmup } } }) },
    '../../sync/useCursorWriter': {
      useAudioSaved: noop, useCursorWriter: () => ({}), usePatientRec: () => null,
      useSaveWatchdog: () => ({ clear: noop, start: noop }),
    },
    '../../sync/messages': { isPatientRecFailure: () => false },
    './rapportRounds': { defaultRapportFlags: () => ({}), isIdentityQuestion: () => false },
    '../SessionControlBar': { SessionControlBar: 'SessionControlBar' },
    '../SessionAbortControl': { SessionAbortControl: 'SessionAbortControl' },
  }).RelationshipConsoleScreen;
  return f.render(() => screen({ session: { session_id: 'SIM-WARMUP', week_no: 1 }, onWrapup: noop }));
}
function findRenderedNode(tree, predicate) {
  if (!tree || typeof tree !== 'object') return null;
  if (Array.isArray(tree)) return tree.map((node) => findRenderedNode(node, predicate)).find(Boolean) ?? null;
  return predicate(tree) ? tree : findRenderedNode(tree.props?.children, predicate);
}
for (const beat of ['ask', 'reply', 'done']) test(`production console does not label the intentional ${beat} warmup microphone block as lost authorization`, () => {
  const tree = recordingNoticeTree({ ...warmup, beat });
  assert.equal(findRenderedNode(tree, (node) => node.type === 'Alert' && node.props.title === '本场录音已禁用'), null);
  assert.ok(findRenderedNode(tree, (node) => node.type === 'WarmupCard'), 'the optional module keeps its own no-recording explanation');
});
test('production console retains the real recording-denied alert after leaving the optional module', () => {
  const tree = recordingNoticeTree(undefined);
  const warning = findRenderedNode(tree, (node) => node.type === 'Alert' && node.props.title === '本场录音已禁用');
  assert.equal(warning?.props.tone, 'danger');
  assert.equal(findRenderedNode(recordingNoticeTree(undefined, 'allowed'), (node) => node.type === 'Alert' && node.props.title === '本场录音已禁用'), null);
});
