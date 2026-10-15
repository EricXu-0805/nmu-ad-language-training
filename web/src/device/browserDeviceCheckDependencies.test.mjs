import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import ts from 'typescript';

const compile = source => ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
} }).outputText;
const read = file => readFileSync(new URL(file, import.meta.url), 'utf8');
const deferred = () => {
  let resolve;
  const promise = new Promise(yes => { resolve = yes; });
  return { promise, resolve };
};
function media() {
  const track = { stopped: false, stop() { this.stopped = true; }, getSettings: () => ({ sampleRate: 48000 }) };
  return { track, stream: { getTracks: () => [track], getAudioTracks: () => [track] } };
}
function harness() {
  const requests = [];
  let contexts = 0;
  const ctx = vm.createContext({ exports: {}, DOMException, __BUILD_ID__: '1792000000000',
    navigator: { userAgent: 'SIM-iPad', locks: {}, mediaDevices: {
      getUserMedia: () => { const request = deferred(); requests.push(request); return request.promise; },
    } },
    window: { isSecureContext: true, innerWidth: 1024, innerHeight: 768, devicePixelRatio: 2,
      AudioContext: class { constructor() { contexts++; } state = 'running'; resume() { return Promise.resolve(); } close() { return Promise.resolve(); } },
    },
    URL: { createObjectURL() {}, revokeObjectURL() {} }, fetch() {},
  });
  vm.runInContext(compile(read('./browserDeviceCheckDependencies.ts')), ctx);
  return { deps: ctx.exports.createBrowserDeviceCheckDependencies(), requests, contexts: () => contexts };
}

test('leaving device check while microphone permission is pending closes the late stream', async () => {
  const h = harness(), m = media();
  const acquisition = h.deps.acquireMicrophone();
  h.deps.dispose();
  h.requests[0].resolve(m.stream);
  await assert.rejects(acquisition, { name: 'AbortError' });
  assert.equal(m.track.stopped, true);
  assert.equal(h.deps.playBeep(), false);
  h.deps.armAudioSync();
  assert.equal(h.contexts(), 0, 'a disposed check cannot recreate an audio context');
});

test('release cancels an in-flight acquisition and a new check owns only its fresh stream', async () => {
  const h = harness(), old = media(), current = media();
  const oldAcquisition = h.deps.acquireMicrophone();
  h.deps.releaseMicrophone();
  const currentAcquisition = h.deps.acquireMicrophone();
  h.requests[1].resolve(current.stream);
  await currentAcquisition;
  h.requests[0].resolve(old.stream);
  await assert.rejects(oldAcquisition, { name: 'AbortError' });
  assert.equal(old.track.stopped, true);
  assert.equal(current.track.stopped, false);
  h.deps.releaseMicrophone();
  assert.equal(current.track.stopped, true);
});

test('overlapping acquisitions cannot overwrite the newest stream or leave the old one live', async () => {
  const h = harness(), old = media(), current = media();
  const oldAcquisition = h.deps.acquireMicrophone();
  const currentAcquisition = h.deps.acquireMicrophone();
  h.requests[0].resolve(old.stream);
  await assert.rejects(oldAcquisition, { name: 'AbortError' });
  h.requests[1].resolve(current.stream);
  const info = await currentAcquisition;
  assert.equal(info.sampleRate, 48000);
  h.deps.dispose();
  assert.equal(old.track.stopped, true);
  assert.equal(current.track.stopped, true);
  await assert.rejects(h.deps.acquireMicrophone(), { name: 'AbortError' });
  assert.equal(h.requests.length, 2);
});

function startCallback(globals) {
  const source = read('./DeviceCheckScreen.tsx');
  const sf = ts.createSourceFile('DeviceCheckScreen.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let callback;
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(sf) === 'start') callback = node.initializer.getText(sf);
    ts.forEachChild(node, visit);
  }
  visit(sf);
  assert(callback);
  const ctx = vm.createContext({ ...globals, module: { exports: {} } });
  vm.runInContext(compile('module.exports = ' + callback), ctx);
  return ctx.module.exports;
}
for (const samplingFails of [false, true]) {
  test(`actual device-check sequence closes microphone before beep (sample failure: ${samplingFails})`, async () => {
    let microphone = false, heard = false;
    const running = { current: false }, events = [];
    const noop = () => {};
    const deps = {
      armAudioSync: noop,
      sampleNoise: async () => {
        assert.equal(microphone, true);
        if (samplingFails) throw new Error('sample interrupted');
        return { rmsDbfs: -50, peakDbfs: -20 };
      },
      releaseMicrophone: () => { microphone = false; events.push('released'); },
      playBeep: () => { assert.equal(microphone, false, 'speaker check cannot share the capture route'); events.push('beep'); return true; },
    };
    const start = startCallback({ running, deps, setResults: noop, setCopied: noop, setFinishedAt: noop,
      setBeepPlayed: value => { heard = value; }, setStage: noop, push: noop,
      runAutoChecks: async () => { microphone = true; return { results: [{ id: 'microphone', status: 'pass' }] }; },
      classifyNoiseFloor: noop, classifySpeechLevel: noop, describeSampleFailure: () => 'failed',
    });
    await start();
    assert.equal(heard, true);
    assert.equal(running.current, false);
    assert.deepEqual(events, ['released', 'beep', 'released']);
  });
}

test('effect reactivation permits fresh checks without authorizing an old pending microphone', async () => {
  const h = harness(), old = media(), current = media();
  h.deps.activate();
  const pending = h.deps.acquireMicrophone();
  h.deps.dispose();
  h.deps.activate();
  const fresh = h.deps.acquireMicrophone();
  h.requests[0].resolve(old.stream);
  await assert.rejects(pending, { name: 'AbortError' });
  h.requests[1].resolve(current.stream);
  await fresh;
  h.deps.armAudioSync();
  assert.equal(h.contexts(), 1);
  assert.equal(old.track.stopped, true);
  assert.equal(current.track.stopped, false);
  h.deps.dispose();
  assert.equal(current.track.stopped, true);
});
