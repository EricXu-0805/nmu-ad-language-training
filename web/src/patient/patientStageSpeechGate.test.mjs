import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import ts from 'typescript';
import { TaskSpeechGate } from './taskSpeechGate.ts';

function harness() {
  const slots = [], spoken = [], events = []; let index = 0, pending = [], dirty = false, tree, listener, options;
  const hooks = {
    useRef(value) { return slots[index++] ??= { current: value }; },
    useState(value) { const at = index++; if (!(at in slots)) slots[at] = typeof value === 'function' ? value() : value;
      return [slots[at], update => { const next = typeof update === 'function' ? update(slots[at]) : update; if (next !== slots[at]) dirty = true; slots[at] = next; }]; },
    useEffect(fn, deps) { const at = index++, previous = slots[at];
      if (!previous || deps.some((v, i) => v !== previous.deps[i])) { const next = slots[at] = { deps, cleanup: previous?.cleanup };
        pending.push(() => { next.cleanup?.(); next.cleanup = fn(); }); } },
    useCallback(fn, deps) { const at = index++, previous = slots[at];
      if (!previous || deps.some((v, i) => v !== previous.deps[i])) slots[at] = { deps, fn }; return slots[at].fn; },
  };
  hooks.useLayoutEffect = hooks.useEffect;
  const h = { presentation: { mode: 'task', cue_text: null, feedback_text: null }, assetReady: true,
    recorder: { recActive: false, starting: false, saving: false },
    props: { sessionId: 'SIM-TASK', ttsContextKey: 'SIM-TASK:turn1', ttsOn: true,
      plan: { item_bank_version_id: 'SIM-V1', items: [{ item_ref: 'itm-1', turns: [{ turn_seq: 1, response_role: '命名' }] }] },
      cursor: { itemIdx: 0, turnIdx: 0, screen: 'present', cueLevel: 0, fbSeq: 1, recording: 'idle', recSeq: 1, wseq: 3, selfStart: true } },
  };
  const source = readFileSync(new URL('./PatientStage.tsx', import.meta.url), 'utf8');
  const sf = ts.createSourceFile('PatientStage.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const fn = sf.statements.find(n => ts.isFunctionDeclaration(n) && n.name.text === 'PatientStage').getText(sf).replace(/^export /, '');
  const globals = { ...hooks, TaskSpeechGate, module: { exports: {} }, exports: {},
    turnKey: (item, turn) => `${item}#${turn}`, ttsEnabled: () => true,
    usePatientPresentation: () => ({ presentation: h.presentation, error: h.error ?? null }), patientAssetRequirementReady: () => h.assetReady,
    onSpeechSettled: fn => { listener = fn; return () => {}; },
    speak: (text, opts) => { spoken.push({ text, ...opts }); events.push('speak'); }, stopSpeaking: () => { events.push('stop'); },
    useVoxRecorder: opts => { options = opts; return { ...h.recorder, stopAndSave() {}, discardForPatientPause() {}, startNow() {}, retrySave() {} }; },
    audioRecorderBlockCopy: () => null, Centered() {}, MicButton() {}, MicButtonSpace() {}, ImagePane() {},
    require: () => ({ jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) }),
  };
  vm.runInNewContext(ts.transpileModule('module.exports = ' + fn, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText, globals);
  return Object.assign(h, { spoken, events, options: () => options, tree: () => tree,
    settle: (line, outcome = 'played') => listener(line.tag, outcome),
    render() { let limit = 12; do { dirty = false; index = 0; tree = globals.module.exports(h.props); const work = pending; pending = []; work.forEach(fn => fn()); } while (dirty && --limit); assert(limit > 0, 'render must converge'); return tree; },
  });
}
function button(node, label) {
  if (Array.isArray(node)) return node.map(n => button(n, label)).find(Boolean);
  if (!node || typeof node !== 'object') return undefined;
  if (node.type === 'button' && node.props.children === label) return node;
  return button(node.props?.children, label);
}
const arm = h => { h.render(); h.props.cursor.recording = 'armed'; h.props.cursor.recSeq = 2; h.props.cursor.wseq++; h.render(); };
const ready = h => { arm(h); for (const line of h.spoken) h.settle(line); h.render(); };

test('armed waits for complete question and cue receipts; the same arm is restored without suspension', () => {
  const h = harness(); h.props.cursor.cueLevel = 1; h.presentation.cue_text = '它能装水。'; arm(h);
  assert.equal(h.options().recording, 'idle'); assert.equal(h.options().suspended, false); assert.equal(h.options().selfStartAllowed, false);
  assert.equal(h.spoken.length, 2); assert.notEqual(h.spoken[0].tag, h.spoken[1].tag);
  h.settle(h.spoken[0]); h.render(); assert.equal(h.options().recording, 'idle');
  h.settle(h.spoken[1]); h.render(); assert.equal(h.options().recording, 'armed'); assert.equal(h.options().recSeq, 2); assert.equal(h.options().selfStartAllowed, true);
});
test('cue arriving during the question waits for both, without interrupting or repeating the question', () => {
  const h = harness(); arm(h); const question = h.spoken[0]; const stops = h.events.filter(e => e === 'stop').length;
  h.props.cursor.cueLevel = 1; h.presentation = null; h.render(); assert.equal(h.options().suspended, false);
  h.presentation = { mode: 'task', cue_text: '用来装水。' }; h.render();
  assert.equal(h.spoken.length, 2); assert(h.spoken.every(line => line.enqueue)); assert.equal(h.events.filter(e => e === 'stop').length, stops);
  h.settle(h.spoken[1]); h.render(); assert.equal(h.options().recording, 'idle');
  h.settle(question); h.render(); assert.equal(h.options().recording, 'armed');
});
test('new feedback cannot release a still-playing question or cue early', () => {
  const h = harness(); arm(h); h.props.cursor.fbSeq = 2; h.props.cursor.fbKey = 'correct'; h.presentation.feedback_text = '很好。'; h.render();
  assert.equal(h.spoken.length, 2); h.settle(h.spoken[1]); h.render(); assert.equal(h.options().recording, 'idle');
  h.settle(h.spoken[0]); h.render(); assert.equal(h.options().recording, 'armed');
});
test('the first connected snapshot does not replay historical feedback', () => {
  const h = harness(); h.props.connectionReady = false;
  h.props.cursor.fbSeq = 7; h.props.cursor.fbKey = 'correct'; h.presentation.feedback_text = '历史反馈。';
  h.render(); assert.equal(h.spoken.length, 0);
  h.props.connectionReady = true; h.render();
  assert.deepEqual(h.spoken.map(line => line.text), ['请看这张图片，这是什么？']);
  h.settle(h.spoken[0]); h.render(); h.props.cursor.wseq++; h.render();
  assert.equal(h.spoken.length, 1);
});
test('the first feedback after a connected baseline without feedback plays and holds capture until its receipt', () => {
  const h = harness(); h.props.cursor.fbSeq = undefined; ready(h);
  h.props.cursor.fbSeq = 1; h.props.cursor.fbKey = 'correct'; h.presentation.feedback_text = '第一次新反馈。'; h.render();
  assert.deepEqual(h.spoken.map(line => line.text), ['请看这张图片，这是什么？', '第一次新反馈。']);
  assert.equal(h.options().recording, 'idle'); assert.equal(h.options().stopRequested, true);
  h.settle(h.spoken[1]); h.render(); assert.equal(h.options().recording, 'armed');
});
test('duplicate feedback projections do not replay, but the next sequence plays the same text again', () => {
  const h = harness(); h.props.cursor.fbSeq = undefined; ready(h);
  h.props.cursor.fbSeq = 1; h.props.cursor.fbKey = 'correct'; h.presentation.feedback_text = '很好。'; h.render();
  h.settle(h.spoken[1]); h.render();
  h.props.cursor = { ...h.props.cursor, wseq: h.props.cursor.wseq + 1 };
  h.presentation = { ...h.presentation }; h.render();
  assert.equal(h.spoken.length, 2); assert.equal(h.options().recording, 'armed');
  h.props.cursor.fbSeq = 2; h.render();
  assert.deepEqual(h.spoken.slice(1).map(line => line.text), ['很好。', '很好。']);
  assert.notEqual(h.spoken[1].tag, h.spoken[2].tag); assert.equal(h.options().recording, 'idle');
  h.settle(h.spoken[2]); h.render(); assert.equal(h.options().recording, 'armed');
});
test('failure stays closed and replays the full authorized question plus cue with fresh tags', () => {
  const h = harness(); h.props.cursor.cueLevel = 1; h.presentation.cue_text = '用来装水。'; arm(h); const old = [...h.spoken];
  h.settle(old[0], 'failed'); h.settle(old[1]); h.render(); assert.equal(h.options().recording, 'idle');
  button(h.tree(), '重新听一遍').props.onClick(); h.render(); assert.deepEqual(h.spoken.slice(2).map(x => x.text), old.map(x => x.text));
  for (const line of old) h.settle(line); h.render(); assert.equal(h.options().recording, 'idle');
  for (const line of h.spoken.slice(2)) h.settle(line); h.render(); assert.equal(h.options().recording, 'armed');
});
test('late old turn receipts cannot open a new turn; pause/resume also needs fresh playback', () => {
  const h = harness(); arm(h); const old = h.spoken[0];
  h.props.ttsContextKey = 'SIM-TASK:turn2'; h.props.plan.items[0].turns[0].turn_seq = 2; h.render();
  h.settle(old); h.render(); assert.equal(h.options().recording, 'idle');
  h.props.sessionPaused = true; h.render(); const paused = h.spoken.at(-1); h.settle(paused); h.render();
  h.props.sessionPaused = false; h.render(); assert.equal(h.options().recording, 'idle'); assert.notEqual(h.spoken.at(-1).tag, paused.tag);
  h.settle(h.spoken.at(-1)); h.render(); assert.equal(h.options().recording, 'armed');
});
test('closing TTS stops it before allowing manual speech; reopening requires a full fresh prompt', () => {
  const h = harness(); arm(h); const old = h.spoken[0]; h.props.ttsOn = false; h.render();
  assert.equal(h.events.at(-1), 'stop'); assert.equal(h.options().recording, 'armed'); assert.equal(h.options().suspended, false);
  h.settle(old); h.props.ttsOn = true; h.render(); assert.equal(h.options().recording, 'idle');
  assert.equal(h.spoken.length, 2); assert.notEqual(h.spoken[1].tag, old.tag); h.settle(h.spoken[1]); h.render(); assert.equal(h.options().recording, 'armed');
});
test('new prompts wait for starting, active capture and physical close/save to finish', () => {
  const h = harness(); ready(h); h.recorder.recActive = true; h.props.cursor.cueLevel = 1; h.presentation.cue_text = '用来装水。'; h.render();
  assert.equal(h.options().stopRequested, true); assert.equal(h.spoken.length, 1);
  h.recorder.recActive = false; h.recorder.saving = true; h.render(); assert.equal(h.spoken.length, 1);
  h.recorder.saving = false; h.recorder.starting = true; h.render(); assert.equal(h.spoken.length, 1);
  h.recorder.starting = false; h.render(); assert.equal(h.spoken.length, 2);
});
test('missing initial projection/image never starts speech or capture; a late current projection gets one full prompt', () => {
  const h = harness(); h.presentation = null; arm(h); assert.equal(h.spoken.length, 0); assert.equal(h.options().recording, 'idle');
  h.presentation = { mode: 'task', cue_text: null }; h.assetReady = false; h.render(); assert.equal(h.spoken.length, 0);
  h.assetReady = true; h.render(); assert.equal(h.spoken.length, 1); h.settle(h.spoken[0]); h.render(); assert.equal(h.options().recording, 'armed');
});
test('ordinary recording screen/wseq updates neither replay nor close a completed prompt', () => {
  const h = harness(); ready(h); h.props.cursor.screen = 'record'; h.props.cursor.wseq++; h.render();
  assert.equal(h.spoken.length, 1); assert.equal(h.options().recording, 'armed'); assert.equal(h.options().stopRequested, false);
});

test('a historical arm present on mount is never revived just because playback finished', () => {
  const h = harness(); h.props.cursor.recording = 'armed'; h.props.cursor.recSeq = 2; h.render();
  h.settle(h.spoken[0]); h.render(); assert.equal(h.options().recording, 'idle');
  h.props.cursor.wseq++; h.render(); assert.equal(h.options().recording, 'armed');
});

test('a retained retry handler cannot replay after pause or a position change', () => {
  const h = harness(); arm(h); h.settle(h.spoken[0], 'failed'); h.render();
  const retry = button(h.tree(), '重新听一遍').props.onClick;
  h.props.sessionPaused = true; h.render(); retry(); h.render(); assert.equal(h.spoken.length, 1);
  h.props.sessionPaused = false; h.props.ttsContextKey = 'SIM-TASK:turn2';
  h.props.plan.items[0].turns[0].turn_seq = 2; h.render();
  retry(); h.render(); assert.equal(h.spoken.length, 2);
});

function visibleText(node) {
  if (typeof node === 'string') return node;
  if (Array.isArray(node)) return node.map(visibleText).join(' ');
  return node && typeof node === 'object' ? visibleText(node.props?.children) : '';
}
test('same-position cue projection refresh retains picture/question shell but clears old cue and holds capture', () => {
  const h = harness(); h.props.cursor.cueLevel = 1; h.presentation.cue_text = '旧线索。'; ready(h);
  h.props.cursor.cueLevel = 2; h.presentation = null;
  const tree = h.render(); assert.equal(tree.type, 'div'); assert.equal(tree.props.className, 'patient-stage');
  assert.match(visibleText(tree), /请看这张图片/); assert.doesNotMatch(visibleText(tree), /旧线索/);
  assert.equal(h.options().recording, 'idle'); assert.equal(h.options().suspended, false); assert.equal(h.spoken.length, 2);
  h.presentation = { mode: 'task', cue_text: '新线索。' }; h.render(); assert.equal(h.spoken.length, 3);
  assert.equal(h.spoken[2].text, '新线索。'); assert.equal(h.options().recording, 'idle');
  h.settle(h.spoken[2]); h.render(); assert.equal(h.options().recording, 'armed');
});
for (const boundary of ['bank', 'turn', 'context', 'error']) test(`display-only shell is revoked at ${boundary}, and old audio is stopped`, () => {
  const h = harness(); arm(h); const stops = h.events.filter(e => e === 'stop').length;
  h.presentation = null;
  if (boundary === 'bank') h.props.plan.item_bank_version_id = 'SIM-V2';
  if (boundary === 'turn') h.props.plan.items[0].turns[0].turn_seq = 2;
  if (boundary === 'context') h.props.ttsContextKey = 'SIM-OTHER';
  if (boundary === 'error') h.error = 'projection mismatch';
  const tree = h.render(); assert.equal(tree.type.name, 'Centered'); assert.doesNotMatch(visibleText(tree), /请看这张图片/);
  assert.equal(h.options().recording, 'idle'); assert(h.events.filter(e => e === 'stop').length > stops);
});

test('the first real arm arriving after an empty mount remains historical until a newer command', () => {
  const h = harness(); const firstCursor = { ...h.props.cursor, recording: 'armed', recSeq: 2 };
  h.props.cursor = undefined; h.presentation = null; h.render(); assert.equal(h.spoken.length, 0);
  h.props.cursor = firstCursor; h.presentation = { mode: 'task', cue_text: null }; h.render();
  h.settle(h.spoken[0]); h.render(); assert.equal(h.options().recording, 'idle');
  h.props.cursor.wseq++; h.render(); assert.equal(h.options().recording, 'armed');
});
