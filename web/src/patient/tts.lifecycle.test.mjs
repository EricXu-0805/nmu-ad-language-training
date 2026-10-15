import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import test from 'node:test';
import ts from 'typescript';

const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const contextKey = 'SIM-TTS:task:0';

// Run the actual singleton playback module against controllable browser events.
// No network, media devices, or patient text are used.
function harness() {
  const fetches = [], playResults = [], utterances = [], events = [], audios = [];
  const storage = new Map(), listeners = new Map(), timers = new Map();
  let now = 0, timerId = 0;
  let blobIndex = 0;
  class FakeAudio {
    constructor() { this.paused = true; audios.push(this); }
    play() { this.paused = false; return playResults.shift() ?? Promise.resolve(); }
    pause() { this.paused = true; }
  }
  const speechSynthesis = {
    speaking: false, pending: false,
    getVoices: () => [{ name: 'Tingting', lang: 'zh-CN', localService: true }],
    speak: utterance => utterances.push(utterance), cancel() {},
  };
  const globals = {
    Audio: FakeAudio, DOMException, AbortController, atob, Blob,
    setTimeout: (callback, ms) => { timers.set(++timerId, { callback, at: now + ms }); return timerId; },
    clearTimeout: id => timers.delete(id),
    SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } },
    URL: { createObjectURL: () => `blob:test-${++blobIndex}`, revokeObjectURL() {} },
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    window: { speechSynthesis, dispatchEvent: event => events.push(event) },
    document: { addEventListener: (name, callback) => listeners.set(name, callback) },
    fetch: (_input, init) => { const result = { ...deferred(), signal: init.signal }; fetches.push(result); return result.promise; },
  };
  const cache = new Map();
  function load(file) {
    if (cache.has(file)) return cache.get(file);
    const exports = {};
    const ctx = vm.createContext({ ...globals, exports, require: name => {
      if (name === '../api') return {
        selectDeviceCredential: () => ({ headers: {} }),
        handleDeviceAuthorizationFailure: status => status === 401,
      };
      if (name === '../security/csrf') return { csrfHeader: () => ({}) };
      return load(path.posix.normalize(path.posix.join(path.posix.dirname(file), name)) + '.ts');
    } });
    const source = readFileSync(new URL(file, import.meta.url), 'utf8');
    vm.runInContext(ts.transpileModule(source, { compilerOptions: {
      target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
    } }).outputText, ctx);
    cache.set(file, exports);
    return exports;
  }
  const tts = load('./tts.ts');
  tts.setTtsContext(contextKey);
  const outcomes = [];
  tts.onSpeechSettled((tag, outcome) => outcomes.push([tag, outcome]));
  const speak = (text, enqueue = false) => tts.speak(text, { contextKey, tag: text, enqueue });
  const serve = async (index, overrides = {}) => {
    fetches[index].resolve({ ok: true, status: 200, headers: { get: () => 'test-engine' }, blob: async () => ({}), ...overrides });
    await tick();
  };
  const advance = ms => {
    now += ms;
    for (const [id, timer] of [...timers]) {
      if (timer.at <= now) { timers.delete(id); timer.callback(); }
    }
  };
  return { tts, speak, serve, fetches, playResults, utterances, events, audios, outcomes, speechSynthesis, storage, advance, timers,
    gesture: name => listeners.get(name)?.(),
  };
}

test('old authorization response body cannot stop a replacement prompt', async () => {
  const h = harness(), body = deferred();
  h.speak('old prompt');
  await h.serve(0, { ok: false, status: 401, text: () => body.promise });
  h.speak('current prompt');
  await h.serve(1);
  assert.equal(h.audios[0].paused, false);
  body.resolve('authorization expired');
  await tick();
  assert.equal(h.audios[0].paused, false, 'late old error must not pause current audio');
  h.audios[0].onended();
  assert.deepEqual(h.outcomes, [['old prompt', 'interrupted'], ['current prompt', 'played']]);
});

test('late media callbacks from a completed prompt cannot settle or replay its queued cue', async () => {
  const h = harness();
  h.speak('question');
  h.speak('cue', true);
  await h.serve(0);
  const oldEnded = h.audios[0].onended, oldError = h.audios[0].onerror;
  oldEnded();
  await h.serve(1);
  oldEnded();
  oldError();
  assert.deepEqual(h.outcomes, [['question', 'played']]);
  assert.equal(h.utterances.length, 0, 'stale audio error must not replay the old question');
  h.audios[0].onended();
  assert.deepEqual(h.outcomes, [['question', 'played'], ['cue', 'played']]);
});

test('error event plus rejected play promise falls back exactly once and ignores stale ended', async () => {
  const h = harness(), play = deferred();
  h.playResults.push(play.promise);
  h.speak('fallback prompt');
  await h.serve(0);
  const staleEnded = h.audios[0].onended;
  h.audios[0].onerror();
  play.reject(new Error('decode failed'));
  await tick();
  assert.equal(h.utterances.length, 1);
  staleEnded();
  assert.deepEqual(h.outcomes, [], 'failed media cannot report a played result while fallback is speaking');
  h.utterances[0].onend();
  assert.deepEqual(h.outcomes, [['fallback prompt', 'played']]);
});

test('old play resolution cannot hide the current prompt gesture requirement', async () => {
  const h = harness(), oldPlay = deferred();
  h.playResults.push(oldPlay.promise);
  h.speak('old prompt');
  await h.serve(0);
  h.speak('current prompt');
  h.playResults.push(Promise.reject(new DOMException('gesture required', 'NotAllowedError')));
  await h.serve(1);
  assert.equal(h.events.at(-1).detail.needed, true);
  oldPlay.resolve();
  await tick();
  assert.equal(h.events.at(-1).detail.needed, true);
  const audit = JSON.parse(h.storage.get('nmu:tts:log'));
  assert.equal(audit.some(event => event.ev.startsWith('start@') && event.tag === 'old prompt'), false);
});

test('browser speech rejection exposes the same gesture recovery and stale events cannot complete a retry', async () => {
  const h = harness();
  h.speak('local prompt');
  await h.serve(0, { status: 204, headers: { get: () => 'null-0' } });
  const old = h.utterances[0];
  old.onerror({ error: 'not-allowed' });
  assert.equal(h.events.at(-1).detail.needed, true);
  // A voices update also retries the same queued line without changing its text/tag.
  h.speechSynthesis.onvoiceschanged();
  assert.equal(h.utterances.length, 2);
  old.onend();
  assert.deepEqual(h.outcomes, []);
  h.utterances[1].onstart();
  assert.equal(h.events.at(-1).detail.needed, false);
  h.utterances[1].onend();
  h.utterances[1].onend();
  assert.deepEqual(h.outcomes, [['local prompt', 'played']]);
});

test('failed browser speech is never later reported played by a duplicate terminal callback', async () => {
  const h = harness();
  h.speak('local prompt');
  await h.serve(0, { status: 204, headers: { get: () => 'null-0' } });
  h.utterances[0].onerror({ error: 'audio-busy' });
  h.utterances[0].onend();
  assert.deepEqual(h.outcomes, [['local prompt', 'failed']]);
});

for (const phase of ['request', 'body', 'play', 'ended']) {
  test(`a stalled ${phase} fails, cancels its media, and cannot resurrect from late completion`, async () => {
    const h = harness(), body = deferred(), play = deferred();
    if (phase === 'play') h.playResults.push(play.promise);
    h.speak('stalled prompt');
    if (phase !== 'request') await h.serve(0, phase === 'body' ? { blob: () => body.promise } : {});
    const oldEnded = h.audios[0].onended;
    h.advance(45_000);
    assert.deepEqual(h.outcomes, [['stalled prompt', 'failed']]);
    assert.equal(h.fetches[0].signal.aborted, true);
    assert.equal(h.audios[0].paused, true);
    assert.equal(h.timers.size, 0);
    if (phase === 'request') await h.serve(0);
    if (phase === 'body') { body.resolve({}); await tick(); }
    if (phase === 'play') { play.resolve(); await tick(); }
    oldEnded?.();
    h.speechSynthesis.onvoiceschanged();
    assert.equal(h.audios[0].paused, true);
    assert.equal(h.fetches.length, 1);
    assert.equal(h.utterances.length, 0);
    assert.deepEqual(h.outcomes, [['stalled prompt', 'failed']]);
  });
}

test('slow synthesis does not consume a normal long recording playback budget', async () => {
  const h = harness();
  h.speak('long prompt');
  h.advance(44_000);
  h.audios[0].duration = 60;
  await h.serve(0);
  h.advance(59_000);
  assert.deepEqual(h.outcomes, []);
  assert.equal(h.audios[0].paused, false);
  h.audios[0].onended();
  h.advance(180_000);
  assert.deepEqual(h.outcomes, [['long prompt', 'played']]);
  assert.equal(h.timers.size, 0);
});

test('local speech with no terminal event fails and a late ended callback remains inert', async () => {
  const h = harness();
  h.speak('local stalled prompt');
  await h.serve(0, { status: 204, headers: { get: () => 'null-0' } });
  h.utterances[0].onstart();
  h.advance(45_000);
  h.utterances[0].onend();
  assert.deepEqual(h.outcomes, [['local stalled prompt', 'failed']]);
});

test('stop cancels a pending request and its timer without an extra failed result', async () => {
  const h = harness();
  h.speak('cancelled prompt');
  h.tts.stopSpeaking();
  assert.equal(h.fetches[0].signal.aborted, true);
  h.advance(180_000);
  await h.serve(0);
  assert.equal(h.audios[0].paused, true);
  assert.equal(h.timers.size, 0);
  assert.deepEqual(h.outcomes, [['cancelled prompt', 'interrupted']]);
});

for (const gestures of [['click'], ['pointerdown', 'click']]) {
  test(`${gestures.join(' + ')} retries gesture-blocked speech once without resetting a live attempt`, async () => {
    const h = harness();
    h.playResults.push(Promise.reject(new DOMException('gesture required', 'NotAllowedError')));
    h.speak('gesture prompt');
    await h.serve(0);
    h.audios[0].paused = true;
    h.gesture(gestures[0]);
    const timerBefore = [...h.timers.keys()];
    for (const gesture of gestures.slice(1)) h.gesture(gesture);
    assert.equal(h.fetches.length, 2);
    assert.deepEqual([...h.timers.keys()], timerBefore);
    await h.serve(1);
    h.audios[0].onended();
    assert.deepEqual(h.outcomes, [['gesture prompt', 'played']]);
  });
}

test('click after cancellation cannot replay a gesture-blocked sentence', async () => {
  const h = harness();
  h.playResults.push(Promise.reject(new DOMException('gesture required', 'NotAllowedError')));
  h.speak('cancelled gesture prompt');
  await h.serve(0);
  h.tts.stopSpeaking();
  h.gesture('click');
  assert.equal(h.fetches.length, 1);
  assert.equal(h.utterances.length, 0);
  assert.equal(h.timers.size, 0);
});

test('play rejection before late media error falls back once without reporting the failed audio played', async () => {
  const h = harness(), play = deferred();
  h.playResults.push(play.promise);
  h.speak('fallback prompt');
  await h.serve(0);
  const oldEnded = h.audios[0].onended, oldError = h.audios[0].onerror;
  play.reject(new Error('decode failed'));
  await tick();
  oldError();
  oldEnded();
  assert.equal(h.utterances.length, 1);
  assert.equal(h.audios[0].paused, true);
  assert.deepEqual(h.outcomes, []);
  h.utterances[0].onend();
  assert.deepEqual(h.outcomes, [['fallback prompt', 'played']]);
});

test('missing local voice failure stays terminal when voices arrive or the user clicks elsewhere', async () => {
  const h = harness();
  h.speechSynthesis.getVoices = () => [];
  h.speak('unavailable voice prompt');
  await h.serve(0, { status: 204, headers: { get: () => 'null-0' } });
  assert.deepEqual(h.outcomes, [['unavailable voice prompt', 'failed']]);
  h.speechSynthesis.getVoices = () => [{ name: 'Tingting', lang: 'zh-CN', localService: true }];
  h.speechSynthesis.onvoiceschanged();
  h.gesture('click');
  assert.equal(h.utterances.length, 0);
  assert.equal(h.timers.size, 0);
  // The visible current sentence is still available for deliberate replay.
  h.tts.speakSample(contextKey);
  assert.equal(h.utterances.length, 1);
});

test('failed local speech cannot be automatically replayed, and its queued cue is preserved', async () => {
  const h = harness();
  h.speak('question');
  h.speak('cue', true);
  await h.serve(0, { status: 204, headers: { get: () => 'null-0' } });
  h.utterances[0].onerror({ error: 'audio-busy' });
  assert.equal(h.utterances.length, 2);
  assert.equal(h.utterances[1].text, 'cue');
  h.utterances[1].onend();
  h.gesture('click');
  h.speechSynthesis.onvoiceschanged();
  assert.equal(h.utterances.length, 2);
  assert.deepEqual(h.outcomes, [['question', 'failed'], ['cue', 'played']]);
});

test('a terminal failure during gesture retry removes the no-longer-actionable recovery overlay', async () => {
  const h = harness();
  h.speak('gesture fallback prompt');
  await h.serve(0, { status: 204, headers: { get: () => 'null-0' } });
  h.utterances[0].onerror({ error: 'not-allowed' });
  assert.equal(h.events.at(-1).detail.needed, true);
  h.speechSynthesis.getVoices = () => [];
  h.gesture('click');
  assert.deepEqual(h.outcomes, [['gesture fallback prompt', 'failed']]);
  assert.equal(h.events.at(-1).detail.needed, false);
  assert.equal(h.timers.size, 0);
});

test('stopping while waiting for a gesture settles the pending utterance as interrupted', async () => {
  const h = harness();
  h.playResults.push(Promise.reject(new DOMException('gesture required', 'NotAllowedError')));
  h.speak('waiting prompt');
  await h.serve(0);
  h.tts.stopSpeaking();
  assert.deepEqual(h.outcomes, [['waiting prompt', 'interrupted']]);
  assert.equal(h.events.at(-1).detail.needed, false);
  assert.equal(h.timers.size, 0);
});
