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
// 只够探针用的 IndexedDB：put/get/delete 各一请求一事务；refuseBlobs 复现 WebKit 非持久会话。
function probeIndexedDb({ refuseBlobs = false, readBack } = {}) {
  const log = { ops: [], opened: [], deleted: [], connections: 0 };
  const databases = new Map();
  const indexedDB = {
    open(name) {
      const request = {};
      setTimeout(() => {
        const fresh = !databases.has(name);
        if (fresh) databases.set(name, new Map());
        const stores = databases.get(name);
        log.opened.push(name);
        log.connections += 1;
        request.result = {
          createObjectStore: (store) => { stores.set(store, new Map()); },
          close: () => { log.connections -= 1; },
          transaction(store) {
            const transaction = { error: null };
            const rows = stores.get(store);
            let op = () => {};
            const queue = (name, run) => { const req = {}; op = () => { log.ops.push(name); req.result = run(); }; return req; };
            transaction.objectStore = () => ({
              put: (value, key) => queue('put', () => {
                if (refuseBlobs && value instanceof Blob) {
                  throw new DOMException('Error preparing Blob/File data to be stored in object store', 'UnknownError');
                }
                rows.set(key, structuredClone(value));
              }),
              get: (key) => queue('get', () => readBack ? readBack(rows.get(key)) : structuredClone(rows.get(key))),
              delete: (key) => queue('delete', () => { rows.delete(key); }),
            });
            setTimeout(() => {
              try { op(); } catch (error) { transaction.error = error; transaction.onabort?.(); return; }
              transaction.oncomplete?.();
            });
            return transaction;
          },
        };
        if (fresh) request.onupgradeneeded?.();
        request.onsuccess?.();
      });
      return request;
    },
    deleteDatabase(name) { databases.delete(name); log.deleted.push(name); return {}; },
  };
  return { indexedDB, log, databases };
}

function harness(indexedDB) {
  const requests = [];
  let contexts = 0;
  // 外层 Blob/ArrayBuffer/Uint8Array 传进沙箱，instanceof 才与假 IndexedDB 的克隆值同一 realm。
  const ctx = vm.createContext({ exports: {}, DOMException, Blob, ArrayBuffer, Uint8Array, __BUILD_ID__: '1792000000000',
    crypto: { randomUUID: () => '00000000-0000-4000-8000-00000000c0de' },
    navigator: { userAgent: 'SIM-iPad', locks: {}, mediaDevices: {
      getUserMedia: () => { const request = deferred(); requests.push(request); return request.promise; },
    } },
    window: { isSecureContext: true, innerWidth: 1024, innerHeight: 768, devicePixelRatio: 2, indexedDB,
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

test('storage probe writes, reads back and deletes a real Blob in a throwaway database', async () => {
  const fake = probeIndexedDb();
  await harness(fake.indexedDB).deps.indexedDbRoundTrip('blob');
  assert.deepEqual(fake.log.ops, ['put', 'get', 'delete']);
  assert.deepEqual(fake.log.opened, ['nmu-probe-00000000-0000-4000-8000-00000000c0de']);
  assert.deepEqual(fake.log.deleted, fake.log.opened, '探针库用完即删，绝不碰 nmu-audio');
  assert.equal(fake.log.connections, 0);
});

test('a Blob-refusing session fails only the Blob probe; the ArrayBuffer probe still round-trips', async () => {
  const fake = probeIndexedDb({ refuseBlobs: true });
  const { deps } = harness(fake.indexedDB);
  await assert.rejects(deps.indexedDbRoundTrip('blob'), { name: 'UnknownError' });
  await deps.indexedDbRoundTrip('array-buffer');
  assert.deepEqual(fake.log.ops, ['put', 'put', 'get', 'delete']);
  assert.equal(fake.log.opened.length, 2);
  assert.deepEqual(fake.log.deleted, fake.log.opened);
  assert.equal(fake.databases.size, 0);
  assert.equal(fake.log.connections, 0);
});

test('a probe whose read-back differs from what it wrote is a failure, not a pass', async () => {
  for (const [kind, readBack] of [
    ['blob', () => new Blob([new Uint8Array([0x1a, 0x45, 0xdf, 0x00])])],
    ['blob', (stored) => stored.arrayBuffer()],
    ['array-buffer', () => undefined],
    ['array-buffer', () => new Blob([new Uint8Array([0x1a, 0x45, 0xdf, 0xa3])])],
  ]) {
    const fake = probeIndexedDb({ readBack });
    await assert.rejects(harness(fake.indexedDB).deps.indexedDbRoundTrip(kind), /读回内容与写入不一致/);
    assert.deepEqual(fake.log.deleted, fake.log.opened);
    assert.equal(fake.log.connections, 0);
  }
});
