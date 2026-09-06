import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import test from 'node:test';
import ts from 'typescript';
import { PriorityActionFence } from './caregiver/priorityActionFence.ts';
import { HoldToConfirm } from './components/holdToConfirm.ts';
import { ApiError } from './apiResponse.ts';
import { capturePatientIntakeSubmission, assessDuplicateIntake } from './console/patientIntakeDuplicate.ts';
import { cloudProcessingChoiceIssue } from './console/cloudProcessingPolicy.ts';
import { parseIntegerInput } from './console/integerInput.ts';
import { receiptMatchesRapportArm } from './console/relationship/rapportRecordingAuthority.ts';
import { createAudioOutboxEntry, parseAudioOutboxEntry } from './audio/audioOutbox.ts';
import { observeRapportSpeech } from './patient/rapportSpeechCycle.ts';
import { playbackIdentity } from './rapportPlayback.ts';
import * as presentationContract from './patient/presentationContent.ts';
import { waitForRapportPlayback } from './rapportPlayback.ts';

// Execute production callbacks with deterministic I/O. This catches callback wiring
// and order-of-await bugs while avoiding microphones, cloud providers and live data.
const read = p => readFileSync(new URL(p, import.meta.url), 'utf8');
const compile = source => ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
const ast = p => ts.createSourceFile(p, read(p), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function findNode(sf, predicate) {
  let found;
  function visit(node) { if (!found && predicate(node)) found = node; if (!found) ts.forEachChild(node, visit); }
  visit(sf); assert(found, 'production node must exist'); return found;
}
function declaration(p, name) {
  const sf = ast(p);
  return findNode(sf, n => ts.isVariableDeclaration(n) && n.name.getText(sf) === name).initializer.getText(sf);
}
function callback(p, name, predicate = () => true) {
  const sf = ast(p);
  return findNode(sf, n => ts.isCallExpression(n) && n.expression.getText(sf) === name && predicate(n)).arguments[0].getText(sf);
}
function executable(source, globals) {
  const ctx = vm.createContext({ ...globals, module: { exports: {} }, exports: {}, console });
  vm.runInContext(compile('module.exports = ' + source), ctx);
  return ctx.module.exports;
}
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { resolve, promise }; };
const tick = () => new Promise(resolve => setImmediate(resolve));

function caregiverPorts(overrides = {}) {
  const events = [];
  return {
    events, current: { sessionId: 'SIM-REGRESSION', operationalDemoReady: true },
    status: { runtimeState: 'active', practiceRevision: 1 }, busyAction: null,
    actionFence: { current: new PriorityActionFence() },
    caregiverSessionIsTerminal: () => false,
    caregiverActionAvailability: () => ({ startPractice: true, pause: true, help: true }),
    setBusyAction: v => events.push(['busy', v]), setOperationProblem: v => events.push(['problem', v]),
    setNotice: v => events.push(['notice', v]), setHelpStatus: v => events.push(['help', v]),
    setHelpOpen() {}, setHelpReason() {}, clearKey() {}, keyFor: () => 'test-idempotency-key',
    adoptStatus: v => events.push(['status', v]), reportUnconfirmed: v => events.push(['uncertain', v]),
    isProviderReadinessPrewriteConflict: () => false, bus: { post: v => events.push(['stop', v]) },
    ...overrides,
  };
}

test('busy start can be preempted by pause; late start cannot overwrite paused UI', async () => {
  const start = deferred(); const paused = { runtimeState: 'paused' }; let pauseCalls = 0;
  const ports = caregiverPorts({ api: {
    startPractice: () => start.promise, pauseSession: async () => { pauseCalls++; return paused; },
  } });
  const ordinary = executable(declaration('./caregiver/CaregiverWorkspace.tsx', 'startPractice'), ports)();
  const pause = executable(declaration('./caregiver/CaregiverWorkspace.tsx', 'pausePractice'), { ...ports, busyAction: 'start-practice' });
  await pause();
  start.resolve({ runtimeState: 'active' }); await ordinary;
  assert.equal(pauseCalls, 1);
  assert.deepEqual(ports.events.filter(e => e[0] === 'status'), [['status', paused]]);
  assert.equal(ports.events.filter(e => e[0] === 'busy' && e[1] === null).length, 1);
});

test('duplicate pause is fenced, and local bus failure cannot suppress server pause', async () => {
  const response = deferred(); let requests = 0;
  const ports = caregiverPorts({ api: { pauseSession: () => { requests++; return response.promise; } },
    bus: { post() { throw new Error('storage unavailable'); } } });
  const pause = executable(declaration('./caregiver/CaregiverWorkspace.tsx', 'pausePractice'), ports);
  const first = pause(); await pause(); assert.equal(requests, 1);
  response.resolve({ runtimeState: 'paused' }); await first;
});

test('late help detail cannot overwrite the UI after safety pause', async () => {
  const help = deferred();
  const ports = caregiverPorts({ helpReason: 'needs_help', api: {
    requestHelp: async () => ({ requestId: 1, status: { runtimeState: 'active' } }),
    getHelpStatus: () => help.promise, pauseSession: async () => ({ runtimeState: 'paused' }),
  } });
  const pending = executable(declaration('./caregiver/CaregiverWorkspace.tsx', 'submitHelp'), ports)();
  await tick();
  await executable(declaration('./caregiver/CaregiverWorkspace.tsx', 'pausePractice'), ports)();
  help.resolve({ state: 'delivered' }); await pending;
  assert.deepEqual(ports.events.filter(e => e[0] === 'help'), []);
});

test('late pause in logout cannot log out after a newer safety action', async () => {
  const oldPause = deferred(); let calls = 0, logouts = 0;
  const ports = caregiverPorts({ onLogout: async () => { logouts++; }, api: {
    pauseSession: () => ++calls === 1 ? oldPause.promise : Promise.resolve({ runtimeState: 'paused' }),
  } });
  const logout = executable(declaration('./caregiver/CaregiverWorkspace.tsx', 'logoutSafely'), ports)();
  await executable(declaration('./caregiver/CaregiverWorkspace.tsx', 'pausePractice'), ports)();
  oldPause.resolve({ runtimeState: 'paused' }); await logout;
  assert.equal(logouts, 0);
});

function keyboardHarness() {
  let now = 0, next = 0, exits = 0; const timers = new Map();
  const hold = new HoldToConfirm({ showHolding() {}, showArmed() {},
    schedule: (fn, ms) => { timers.set(++next, { at: now + ms, fn }); return next; },
    cancel: id => timers.delete(id),
  });
  const globals = { hold: { current: hold }, onExit: () => { exits++; } };
  const keyDown = executable(declaration('./App.tsx', 'keyDown'), globals);
  const keyUp = executable(declaration('./App.tsx', 'keyUp'), globals);
  return { hold, exits: () => exits, keyDown, keyUp,
    advance(ms) { now += ms; for (const [id, t] of [...timers]) if (t.at <= now) { timers.delete(id); t.fn(); } },
  };
}
for (const key of ['Enter', ' ']) test(`keyboard ${JSON.stringify(key)} requires release and a second press after hold`, () => {
  const h = keyboardHarness(); const event = (repeat = false) => ({ key, repeat, preventDefault() {} });
  h.keyDown(event()); h.advance(2500);
  h.keyDown(event(true)); h.keyUp(event());
  assert.equal(h.exits(), 0, 'releasing the first hold must never confirm');
  h.keyDown(event()); h.keyUp(event()); assert.equal(h.exits(), 1);
  h.keyUp(event()); assert.equal(h.exits(), 1, 'repeat keyup cannot double-exit');
});
test('short press and expired confirmation cannot exit', () => {
  const h = keyboardHarness(), e = { key: 'Enter', repeat: false, preventDefault() {} };
  h.keyDown(e); h.advance(1000); h.keyUp(e); h.advance(3000); assert.equal(h.exits(), 0);
  h.keyDown(e); h.advance(2500); h.keyUp(e); h.advance(4000);
  h.keyDown(e); h.keyUp(e); assert.equal(h.exits(), 0);
});

test('invalid education text is rejected without stripping characters or changing values', () => {
  for (const raw of ['1.5', '-2', '+2', '1e1', ' 2', '2 ', 'abc', '31']) {
    const result = parseIntegerInput(raw, '受教育年限', 0, 30);
    assert.equal(result.value, null); assert(result.error, raw);
  }
  for (const value of [0, 1, 15, 30]) assert.deepEqual(parseIntegerInput(String(value), '受教育年限', 0, 30), { value, error: null });
  assert.deepEqual(parseIntegerInput('', '受教育年限', 0, 30), { value: null, error: null });
});

function audioHandler() {
  const writes = [], generated = [], ledger = {}; const sk = '介绍机构环境', qi = 0, pos = `${sk}#${qi}`;
  const ports = {
    session: { session_id: 'SIM-REGRESSION' }, section: { key: sk }, qIdx: qi,
    rapportTurnKey: (s, q) => `rapport:${s}:${q}`, journal: { audios: ledger }, upsertAudio: (id, row) => { ledger[id] = row; },
    watchdogFor: { current: null }, watchdog: { clear() {} }, recState: 'armed', interactionBlocked: false,
    postRapport: x => { writes.push(x); return Promise.resolve(true); }, currentBeatFields: () => ({ beat: 'ask' }),
    recSeq: { current: 2 }, armedWseq: { current: 22 }, receiptMatchesRapportArm, rapportFlags: {}, setRecState() {}, toast() {},
    autoReply: true, playbackWaitEpoch: { current: 0 }, replyBusyRef: { current: false }, lastArmedPosRef: { current: pos }, posRef: { current: pos },
    autoOpenHere: true, replyBank: {}, replyLine: null, setReplyPending() {}, applyReply() {},
    api: { rapportReplyCreate: (_s, req) => { generated.push(req); return Promise.resolve({}); } },
  };
  return { ports, writes, generated, ledger, handler: executable(callback('./console/relationship/RelationshipConsoleScreen.tsx', 'useAudioSaved'), ports),
    message: { type: 'audioSaved', sessionId: 'SIM-REGRESSION', rawAudioId: 'audio-1', turnKey: `rapport:${sk}:${qi}`, durationSeconds: 10, containsDirectIdentifier: false } };
}
test('old and legacy same-question audio receipts only update history, never close the new microphone', async () => {
  const h = audioHandler();
  h.handler({ ...h.message, recordingWseq: 11 });
  h.handler({ ...h.message, rawAudioId: 'legacy' }); await tick();
  assert.equal(Object.keys(h.ledger).length, 2); assert.equal(h.writes.length, 0); assert.equal(h.generated.length, 0);
});
test('matching capture receipt closes this arm and drives one reply; replay cannot drive another', async () => {
  const h = audioHandler(); const receipt = { ...h.message, recordingWseq: 22 };
  h.handler(receipt); await tick(); h.handler(receipt); await tick();
  assert.equal(h.writes.length, 1); assert.equal(h.writes[0].recording, 'idle'); assert.equal(h.generated.length, 1);
});
test('recording outbox preserves original arm across save/retry and rejects malformed generations', () => {
  const captured = createAudioOutboxEntry({ rawAudioId: 'audio-1', sessionId: 'SIM-REGRESSION', turnKey: 'rapport:介绍机构环境:0',
    containsDirectIdentifier: false, recordingWseq: 22, durationSeconds: 10, blob: new Blob(['voice']), nowMs: 100 });
  const restored = parseAudioOutboxEntry(structuredClone(captured)); assert.equal(restored.recordingWseq, 22);
  for (const recordingWseq of [0, -1, 1.5, '22']) assert.throws(() => parseAudioOutboxEntry({ ...captured, recordingWseq }));
});

test('explicit same-question idle generation replays, arm and save rewrites preserve speech', () => {
  let state = observeRapportSpeech(null, { content: 'ask#0', recSeq: 1, recording: 'idle', wseq: 10 }); const first = state.key;
  state = observeRapportSpeech(state, { content: 'ask#0', recSeq: 2, recording: 'armed', wseq: 11 }); assert.equal(state.key, first);
  state = observeRapportSpeech(state, { content: 'ask#0', recSeq: 2, recording: 'idle', wseq: 12 }); assert.equal(state.key, first);
  state = observeRapportSpeech(state, { content: 'ask#0', recSeq: 3, recording: 'idle', wseq: 13 }); assert.notEqual(state.key, first);
});
test('console playback wait ignores old receipts and never estimates a played result', async () => {
  const expected = { sectionKey: '认识机器人', questionIdx: 0, beat: 'ask', utteranceId: null, wseq: 22 };
  let now = 0;
  const result = await waitForRapportPlayback(expected, { now: () => now, sleep: async ms => { now += ms; }, isCurrent: () => true,
    read: async () => ({ ...expected, wseq: now < 26000 ? 21 : 22, outcome: 'played' }) });
  assert.equal(result, 'played'); assert.equal(now, 26000);
  now = 0;
  assert.equal(await waitForRapportPlayback(expected, { now: () => now, sleep: async ms => { now += ms; }, isCurrent: () => true, read: async () => null }), 'timeout');
});
test('a late successful playback read loses authority when a safety action intervenes', async () => {
  const response = deferred(); let current = true;
  const expected = { sectionKey: '认识机器人', questionIdx: 0, beat: 'ask', utteranceId: null, wseq: 22 };
  const waiting = waitForRapportPlayback(expected, { now: () => 0, sleep: async () => {}, isCurrent: () => current, read: () => response.promise });
  current = false; response.resolve({ ...expected, outcome: 'played' }); assert.equal(await waiting, 'cancelled');
});

function ttsHarness() {
  let fetchResolve; const audios = [], storage = new Map();
  class FakeAudio {
    constructor() { this.paused = true; audios.push(this); }
    play() { this.paused = false; return Promise.resolve(); }
    pause() { this.paused = true; }
  }
  const globals = {
    Audio: FakeAudio, URL: { createObjectURL: () => 'blob:fake', revokeObjectURL() {} }, DOMException,
    localStorage: { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v) },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    window: { dispatchEvent() {} }, fetch: () => new Promise(resolve => { fetchResolve = resolve; }),
  };
  const cache = new Map();
  function load(p) {
    if (cache.has(p)) return cache.get(p);
    const exports = {}, ctx = vm.createContext({ ...globals, exports, require: name => {
      if (name === '../api') return { selectDeviceCredential: () => ({ headers: {} }), handleDeviceAuthorizationFailure: () => false };
      if (name === '../security/csrf') return { csrfHeader: () => ({}) };
      return load(path.posix.normalize(path.posix.join(path.posix.dirname(p), name)) + '.ts');
    } });
    vm.runInContext(compile(read(p)), ctx); cache.set(p, exports); return exports;
  }
  const tts = load('./patient/tts.ts'); tts.setTtsContext('SIM-REGRESSION:rapport:0');
  return { tts, audios, serve: async () => { fetchResolve({ ok: true, status: 200, headers: { get: () => 'fake-engine' }, blob: async () => ({}) }); await tick(); } };
}
function gated(gate) {
  const source = read('./patient/RapportStage.tsx');
  const start = source.indexOf('  const speechInFlight =');
  const code = source.slice(start, source.indexOf('  const {\n    stopAndSave', start));
  const ctx = vm.createContext({ confirmedRobot: true, speechGate: gate, speechIdentity: 'current', rapportStep: { recording: 'armed' } });
  vm.runInContext(compile(code + '\nglobalThis.result = gatedRecording;'), ctx); return ctx.result;
}
test('actual TTS begun before a delayed arm cannot open the microphone until ended', async () => {
  const h = ttsHarness(); let gate = { identity: 'current', outcome: 'pending' };
  h.tts.onSpeechSettled((_tag, outcome) => { gate = { identity: 'current', outcome }; });
  h.tts.speak('这一句需要很久才能读完', { contextKey: 'SIM-REGRESSION:rapport:0', tag: 'run-22' });
  await h.serve(); assert.equal(h.audios[0].paused, false);
  assert.equal(gated(gate), 'idle', 'no ended event, even after old 12/16-second thresholds');
  h.audios[0].onended(); assert.equal(gated(gate), 'armed');
});
test('actual timeout closes media, keeps microphone closed, and ignores a late ended event', async () => {
  const h = ttsHarness(); let gate = { identity: 'current', outcome: 'pending' }, expire;
  h.tts.onSpeechSettled((_tag, outcome) => { if (gate.outcome === 'pending') gate = { identity: 'current', outcome }; });
  h.tts.speak('迟迟没有结束的朗读', { contextKey: 'SIM-REGRESSION:rapport:0', tag: 'run-22' }); await h.serve();
  const oldEnded = h.audios[0].onended;
  executable(callback('./patient/RapportStage.tsx', 'useEffect', n => n.getText().includes('SPEECH_SETTLE_TIMEOUT_MS')), {
    speechGate: gate, SPEECH_SETTLE_TIMEOUT_MS: 45000, stopSpeaking: h.tts.stopSpeaking,
    finishSpeechRef: { current: outcome => { gate = { identity: 'current', outcome }; } },
    window: { setTimeout: cb => { expire = cb; return 1; }, clearTimeout() {} },
  })();
  expire(); assert.equal(h.audios[0].paused, true); assert.equal(gated(gate), 'idle');
  oldEnded(); assert.equal(gate.outcome, 'failed'); assert.equal(gated(gate), 'idle');
});
test('old same-content speech tag cannot settle a new playback run', () => {
  const outcomes = [];
  const sf = ast('./patient/RapportStage.tsx');
  const listener = findNode(sf, n => ts.isCallExpression(n) && n.expression.getText(sf) === 'onSpeechSettled').arguments[0].getText(sf);
  const settle = executable(listener, { speakingTagRef: { current: 'rapport:same-content:22:2' }, finishSpeechRef: { current: value => outcomes.push(value) } });
  settle('rapport:same-content:21:1', 'played'); assert.deepEqual(outcomes, []);
  settle('rapport:same-content:22:2', 'played'); assert.deepEqual(outcomes, ['played']);
});


test('same-question stale receipt cannot clear the current save watchdog', () => {
  const h = audioHandler(); let clears = 0;
  h.ports.watchdogFor.current = h.message.turnKey;
  h.ports.watchdogWseq = { current: 22 };
  h.ports.watchdog.clear = () => { clears++; };
  // Recompile with the same production callback after supplying the watchdog port.
  const handler = executable(callback('./console/relationship/RelationshipConsoleScreen.tsx', 'useAudioSaved'), h.ports);
  handler({ ...h.message, recordingWseq: 11 }); assert.equal(clears, 0);
  handler({ ...h.message, rawAudioId: 'audio-current', recordingWseq: 22 }); assert.equal(clears, 1);
});
test('microphone start requires the exact positive Week1 arm authorization', () => {
  const sf = ast('./patient/useVoxRecorder.ts');
  const condition = findNode(sf, n => ts.isIfStatement(n) && n.expression.getText(sf).startsWith('!authorizesMicrophoneStart(authorization)')).expression.getText(sf);
  const rejected = (commandSeq, authorizedWseq, required = true) => executable(`() => (${condition})`, {
    permit: { commandSeq }, authorization: { recording_wseq: authorizedWseq }, now: { requireRecordingWseq: required },
    authorizesMicrophoneStart: () => true,
  })();
  assert.equal(rejected(22, 22), false);
  for (const pair of [[22, 21], [22, undefined], [undefined, 22], [0, 0], [1.5, 1.5]]) assert.equal(rejected(...pair), true);
  assert.equal(rejected(undefined, undefined, false), false, 'task recorder compatibility is preserved');
});
test('actual explicit go on the same question emits new idle generations for replay', async () => {
  const commands = [], recSeq = { current: 0 }, epoch = { current: 0 }; let armed = 0;
  const noop = () => {};
  const go = executable(declaration('./console/relationship/RelationshipConsoleScreen.tsx', 'go'), {
    interactionBlocked: false, cancelAfterReply: () => { epoch.current++; }, playbackWaitEpoch: epoch,
    armedWseq: { current: null }, recSeq, setPlaybackProblem: noop,
    script: { sections: [{ key: '认识机器人', speaker: '机器人', questions: [{}] }] },
    defaultRapportFlags: () => ({}), recState: 'idle', recStateRef: { current: 'idle' },
    setRecState: noop, setSectionIdx: noop, setQIdx: noop, setBeat: noop,
    replyBeatRef: { current: {} }, setSpokenReply: noop, setReplyMeta: noop, setRapportFlags: noop,
    postRapportWithReceipt: async command => { const committed = { ...command, wseq: commands.length + 10 }; commands.push(committed); return committed; },
    playbackIdentity: command => command, setRoundNote: noop, waitForSpeech: async () => true,
    replyBusyRef: { current: false }, shouldAutoArmOnEntry: () => true,
    latest: { current: { autoReply: true, armRecording: () => { armed++; } } },
  });
  await go(0, 0); await go(0, 0);
  assert.deepEqual(commands.map(c => c.recSeq), [1, 2]); assert.equal(armed, 2);
  const first = observeRapportSpeech(null, { content: 'same-question', ...commands[0] });
  const second = observeRapportSpeech(first, { content: 'same-question', ...commands[1] });
  assert.notEqual(first.key, second.key, 'second presentation must cause a new speech receipt');
});


test('patient safety bus uses stable session fields and immediately stops only its own session', () => {
  const events = [];
  const listener = executable(callback('./patient/PatientShell.tsx', 'bus.subscribe'), {
    safetySessionId: 'SIM-REGRESSION', safetyServerPaused: false,
    clearTtsContext: () => events.push('tts-stop'), stopAutopilotMediaRef: { current: () => events.push('media-stop') },
    latchBedsideSafetyStop: (own, incoming) => ({ own, incoming }),
    reconcileBedsideSafetyLatch: (latch, own, paused) => { assert.equal(own, 'SIM-REGRESSION'); assert.equal(paused, false); return latch; },
    setSafetyLatch: update => { update(); events.push('latched'); },
  });
  listener({ type: 'safetyStop', sessionId: 'OTHER' }); assert.deepEqual(events, []);
  listener({ type: 'safetyStop', sessionId: 'SIM-REGRESSION' });
  assert.deepEqual(events, ['tts-stop', 'media-stop', 'latched']);
  const sf = ast('./patient/PatientShell.tsx');
  const effect = findNode(sf, n => ts.isCallExpression(n) && n.expression.getText(sf) === 'useLayoutEffect'
    && n.arguments[0].getText(sf).startsWith('() => bus.subscribe'));
  assert.equal(effect.arguments[1].getText(sf), '[safetySessionId, safetyServerPaused, stopLocallyForPatientPause]',
    'revision/wseq-only polls must not tear down the safety subscriber');
});

function hookFunction(p, name) {
  const sf = ast(p);
  return findNode(sf, n => ts.isVariableDeclaration(n) && n.name.getText(sf) === name).initializer.arguments[0].getText(sf);
}
function hooksFrame() {
  const slots = []; let cursor = 0, pending = [];
  const hooks = {
    useRef(value) { const index = cursor++; return slots[index] ??= { current: value }; },
    useState(value) {
      const index = cursor++; if (!(index in slots)) slots[index] = typeof value === 'function' ? value() : value;
      return [slots[index], update => { slots[index] = typeof update === 'function' ? update(slots[index]) : update; }];
    },
    useEffect(fn, deps) {
      const index = cursor++, previous = slots[index];
      if (!previous || !deps || deps.some((v, i) => v !== previous.deps?.[i])) {
        const next = { deps, cleanup: previous?.cleanup }; slots[index] = next;
        pending.push(() => { next.cleanup?.(); next.cleanup = fn(); });
      }
    },
  };
  hooks.useLayoutEffect = hooks.useEffect;
  return { hooks, render(fn) { cursor = 0; return fn(); }, flush() { const work = pending; pending = []; work.forEach(fn => fn()); } };
}
function presentationGapHarness() {
  const frame = hooksFrame(), requests = []; let recorderOptions, speechTag, speechSettled;
  const api = { patientPresentation: () => { const d = deferred(); requests.push(d); return d.promise; }, reportRapportPlayback: async () => null };
  const globals = { ...frame.hooks, api, ...presentationContract,
    window: { setTimeout: () => 1, clearTimeout() {} } };
  const sf = ast('./patient/usePatientPresentation.ts');
  const usePatientPresentation = executable(findNode(sf, n => ts.isFunctionDeclaration(n) && n.name?.text === 'usePatientPresentation').getText(sf).replace(/^export /, ''), { ...globals, RETRY_MS: 1000 });
  const stageSf = ast('./patient/RapportStage.tsx');
  const stage = executable(findNode(stageSf, n => ts.isFunctionDeclaration(n) && n.name?.text === 'RapportStage').getText(stageSf).replace(/^export /, ''), {
    ...globals, usePatientPresentation, observeRapportSpeech, playbackIdentity,
    SPEECH_SETTLE_TIMEOUT_MS: 45000, RAPPORT_MAX_RECORDING_MS: 30000,
    stopSpeaking() {}, speak: (_text, opts) => { speechTag = opts.tag; }, ttsEnabled: () => true,
    onSpeechSettled: listener => { speechSettled = listener; return () => {}; },
    rapportTurnKey: (s, q) => `rapport:${s}:${q}`, Centered() {}, MicButton() {},
    audioRecorderBlockCopy: () => null,
    useVoxRecorder: opts => { recorderOptions = opts; return { blockReason: null }; },
    require: () => ({ jsx: () => null, jsxs: () => null }),
  });
  const props = { sessionId: 'SIM-REGRESSION', ttsContextKey: 'SIM-REGRESSION:rapport:0',
    rapportStep: { sectionKey: '认识机器人', questionIdx: 0, beat: 'ask', recording: 'idle', recSeq: 1, wseq: 10 } };
  return {
    props, requests, render: () => frame.render(() => stage(props)), flush: frame.flush,
    options: () => recorderOptions, ended: () => speechSettled(speechTag, 'played'),
    presentation: { schema_version: 1, mode: 'rapport', session_id: 'SIM-REGRESSION', script_version_id: 'week1-v1',
      section_key: '认识机器人', question_idx: 0, beat: 'ask', speaker: '机器人', text: '我们一起聊聊天', wseq: 10 },
  };
}
async function runActualRecorderStart(options) {
  let starts = 0, authorizations = 0;
  const ref = current => ({ current }), noop = () => {};
  const globals = {
    latest: ref({ ...options, selfStartAllowed: false }), mountedRef: ref(true), deviceLease: ref(true), outboxReady: ref(true),
    recRef: ref({ active: false, cancelPendingStart: noop, start: async () => { starts++; return true; }, discardActive: noop }),
    savingRef: ref(false), pendingSave: ref(null), startingRef: ref(false), patientPauseDiscardRequested: ref(false),
    startGeneration: ref(0), activePermit: ref(null), startTimeout: ref(null), armedMeta: ref(null),
    activeKind: ref(null), consumedRemote: ref(null), recordingLimit: ref(null),
    setStarting: noop, setMicError: noop, setRecActive: noop,
    reportStartFailure: () => assert.fail('unexpected recording failure'), classifyRecordingStartFailure: () => 'authorization',
    MIC_START_TIMEOUT_MS: 12000, MAX_RECORDING_MS: 300000,
    window: { setTimeout: () => 1 }, clearTimeout: noop, clearRecordingLimit: noop,
    api: { recordingAuthorization: async () => { authorizations++; return { allowed: true, runtime_status: 'active', recording_wseq: options.commandSeq }; } },
    authorizesMicrophoneStart: value => value.allowed && value.runtime_status === 'active',
    persistRec: async () => ({}), postInactive: noop, bus: { post: noop }, stopAndSave: noop,
  };
  globals.permitIsCurrent = executable(hookFunction('./patient/useVoxRecorder.ts', 'permitIsCurrent'), globals);
  await executable(hookFunction('./patient/useVoxRecorder.ts', 'launchStart'), globals)('remote');
  return { starts, authorizations };
}
for (const speechFinished of [false, true]) test(`real presentation hook masks old wseq while ${speechFinished ? 'completed speech allows' : 'unfinished speech blocks'} a fast microphone authorization`, async () => {
  const h = presentationGapHarness();
  h.render(); h.flush(); assert.equal(h.requests.length, 1);
  h.requests[0].resolve(h.presentation); await tick();
  h.render(); h.flush(); h.render();
  if (speechFinished) { h.ended(); h.render(); }
  h.props.rapportStep = { ...h.props.rapportStep, recording: 'armed', recSeq: 2, wseq: 11 };
  h.render(); h.flush();
  assert.equal(h.requests.length, 2, 'new presentation GET is unresolved');
  assert.equal(h.options().suspended, false, 'same content stays confirmed during wseq refresh');
  assert.equal(h.options().recording, speechFinished ? 'armed' : 'idle');
  const result = await runActualRecorderStart(h.options());
  assert.equal(result.authorizations, speechFinished ? 1 : 0);
  assert.equal(result.starts, speechFinished ? 1 : 0, 'actual recorder launch may not beat unfinished TTS');
});


for (const mode of ['auto', 'script', 'bank']) for (const navigated of [false, true]) test(`${mode} late reply ${navigated ? 'cannot replace a replay after leaving and returning' : 'still applies while its request remains current'}`, async () => {
  const h = audioHandler(), response = deferred(), applied = [];
  const ports = { ...h.ports, recState: mode === 'auto' ? 'armed' : 'idle',
    replyLine: '当前脚本回应', replyPending: false, replyCursor: { current: {} },
    replyBank: { replies: [{ group: '继续', id: 'continue' }] },
    api: { rapportReplyCreate: () => response.promise },
    applyReply: u => applied.push(u.utteranceId),
  };
  if (mode === 'auto') executable(callback('./console/relationship/RelationshipConsoleScreen.tsx', 'useAudioSaved'), ports)({ ...h.message, recordingWseq: 22 });
  else executable(declaration('./console/relationship/RelationshipConsoleScreen.tsx', mode === 'script' ? 'sayReply' : 'sayGroupReply'), ports)('继续');
  if (navigated) {
    // Both go() calls invalidate this request even though the final position is identical.
    ports.posRef.current = '另一节#0'; ports.playbackWaitEpoch.current++;
    ports.posRef.current = '介绍机构环境#0'; ports.playbackWaitEpoch.current++;
  }
  response.resolve({ utteranceId: 99 }); await tick();
  assert.deepEqual(applied, navigated ? [] : [99]);
});


for (const duplicate of [false, true]) test(`actual intake ${duplicate ? '409 review' : 'create and dedicated cloud authorization'} uses its validated submission snapshot across async edits`, async () => {
  const response = deferred(), createdPayloads = [], cloudWrites = [], reviews = [], completed = [];
  const p = { patient_id: '  P-ORIGINAL  ', is_simulation_subject: false, governance_revision: 0,
    recording_allowed: true, cloud_processing_allowed: true,
    cloud_processing_provider_id: 'untrusted-browser-provider', cloud_processing_notice_version: 'untrusted-browser-version',
    cloud_processing_consented_at: 'untrusted-browser-time', cloud_processing_revoked_at: 'untrusted-browser-time' };
  const birthYear = { value: 1948, error: null }, education = { value: 12, error: null };
  const existing = { patient_id: 'P-ORIGINAL', is_simulation_subject: false, governance_revision: 7,
    recording_allowed: true, cloud_processing_allowed: false, birth_year: 1948, education_years: 12 };
  const policy = { configured: true, provider_id: 'reviewed-provider', notice_version: 'reviewed-notice', data_categories: [] };
  const noop = () => {};
  const ports = { busy: false, p, birthYear, education, purposeConfirmed: 'research', simulationAcknowledged: false,
    setFieldErrors: noop, cloudPolicy: policy, cloudProcessingChoiceIssue, capturePatientIntakeSubmission,
    setBusy: noop, toast: noop, registry: true, ApiError, assessDuplicateIntake,
    setDuplicateReview: review => reviews.push(review), setDuplicateConfirm: noop,
    api: { createPatient: async payload => { createdPayloads.push(payload); await response.promise;
      if (duplicate) throw new ApiError(409, '编号已存在'); return existing; },
      getPatient: async id => { assert.equal(id, 'P-ORIGINAL'); return existing; },
      setPatientCloudProcessing: async (...args) => { cloudWrites.push(args); } },
  };
  const sf = ast('./console/PatientIntakeScreen.tsx');
  const fn = name => findNode(sf, n => ts.isFunctionDeclaration(n) && n.name?.text === name).getText(sf);
  ports.resolveExisting = executable(fn('resolveExisting'), ports);
  const pending = executable(fn('submit'), ports)(id => completed.push(id));
  p.patient_id = 'P-LATER'; p.recording_allowed = false; p.cloud_processing_allowed = false;
  education.value = 30; birthYear.value = 2000;
  response.resolve(); await pending;
  assert.equal(createdPayloads.length, 1);
  assert.equal(createdPayloads[0].patient_id, 'P-ORIGINAL');
  assert.equal(createdPayloads[0].education_years, 12); assert.equal(createdPayloads[0].birth_year, 1948);
  assert.equal(createdPayloads[0].recording_allowed, true);
  for (const key of ['cloud_processing_allowed', 'cloud_processing_provider_id', 'cloud_processing_notice_version',
    'cloud_processing_consented_at', 'cloud_processing_revoked_at']) assert.equal(key in createdPayloads[0], false, key);
  if (duplicate) {
    assert.equal(cloudWrites.length, 0, '409 cannot automatically grant cloud processing'); assert.deepEqual(completed, []);
    assert.equal(reviews.length, 1); assert.equal(reviews[0].submitted.patient_id, 'P-ORIGINAL');
    assert.equal(reviews[0].submitted.cloud_processing_allowed, true);
    assert.equal(reviews[0].submitted.education_years, 12); assert.equal(reviews[0].submitted.birth_year, 1948);
    assert.equal(reviews[0].assessment.cloudChangeRequested, true);
  } else {
    assert.equal(cloudWrites.length, 1); assert.equal(cloudWrites[0][0], 'P-ORIGINAL');
    assert.equal(cloudWrites[0][1], true); assert.equal(cloudWrites[0][2], existing); assert.equal(cloudWrites[0][3], policy);
    assert.deepEqual(completed, ['P-ORIGINAL']); assert.equal(reviews.length, 0);
  }
});
