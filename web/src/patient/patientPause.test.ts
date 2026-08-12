import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  acknowledgePatientPauseOutbox,
  clearPatientPauseOutboxIfMatches,
  createPatientPauseOutbox,
  loadPatientPauseOutbox,
  observePatientPauseOnServer,
  PATIENT_PAUSE_STORAGE_LOCK_NAME,
  parsePatientPauseReceipt,
  parsePatientPauseOutbox,
  patientPauseCanResolveAfterResume,
  patientPauseOutboxForStopSignal,
  patientPauseRetryDisposition,
  requirePatientPauseServerResolution,
  resolvePatientPauseOutbox,
  savePatientPauseOutbox,
} from "./patientPause.ts";

class MemoryStorage {
  private values = new Map<string, string>();
  failWrites = false;
  getItem(key: string): string | null { return this.values.get(key) ?? null; }
  setItem(key: string, value: string): void {
    if (this.failWrites) throw new Error("storage full");
    this.values.set(key, value);
  }
  removeItem(key: string): void { this.values.delete(key); }
}

const UUID = "01234567-89ab-cdef-0123-456789abcdef";

test("patient pause outbox keeps one exact key across offline retry and refresh", () => {
  const storage = new MemoryStorage();
  const pending = createPatientPauseOutbox(
    "S-ONE", () => UUID, () => new Date("2026-08-12T00:00:00Z"));
  assert.equal(pending.idempotencyKey, "patient_pause:0123456789abcdef0123456789abcdef");
  assert.equal(pending.expiresAt, "2026-08-13T00:00:00.000Z");
  assert.equal(savePatientPauseOutbox(storage, pending), true);
  assert.deepEqual(loadPatientPauseOutbox(storage, "S-ONE"), pending);
  assert.equal(loadPatientPauseOutbox(storage, "S-TWO"), null);

  const acknowledged = acknowledgePatientPauseOutbox(storage, pending);
  assert.equal(acknowledged.state, "acknowledged");
  assert.deepEqual(loadPatientPauseOutbox(storage, "S-ONE"), acknowledged);
  assert.equal(clearPatientPauseOutboxIfMatches(storage, {
    sessionId: "S-ONE", idempotencyKey: "patient_pause:ffffffffffffffffffffffffffffffff",
  }), false);
  assert.equal(clearPatientPauseOutboxIfMatches(storage, pending), true);
  assert.equal(loadPatientPauseOutbox(storage), null);
});

test("retry policy separates transient transport, re-pair, and stable authority failures", () => {
  assert.equal(patientPauseRetryDisposition({ status: 0 }), "retry");
  assert.equal(patientPauseRetryDisposition({ status: 503 }), "retry");
  assert.equal(patientPauseRetryDisposition({
    status: 409, detailData: { code: "patient_pause_concurrent_conflict" },
  }), "retry");
  assert.equal(patientPauseRetryDisposition({ status: 401 }), "wait_for_capability");
  for (const code of [
    "patient_pause_replay_superseded",
    "patient_pause_idempotency_conflict",
    "patient_pause_receipt_corrupt",
  ]) {
    assert.equal(patientPauseRetryDisposition({
      status: 409, detailData: { code },
    }), "wait_for_authority");
  }
});

test("patient pause receipt accepts only the exact authoritative shape", () => {
  const receipt = {
    sessionId: "S-ONE",
    status: "paused",
    runtimeRevision: 1,
    liveSeq: 2,
    eventSeq: 1,
    idempotent: false,
  } as const;
  assert.deepEqual(parsePatientPauseReceipt(receipt, "S-ONE"), receipt);
  assert.throws(() => parsePatientPauseReceipt({ ...receipt, extra: true }, "S-ONE"));
  const { eventSeq: _missing, ...missing } = receipt;
  assert.throws(() => parsePatientPauseReceipt(missing, "S-ONE"));
  assert.throws(() => parsePatientPauseReceipt(receipt, "S-TWO"));
});

test("tampered/cross-shape pause outbox fails closed", () => {
  assert.equal(parsePatientPauseOutbox(null), null);
  assert.equal(parsePatientPauseOutbox({
    version: 1,
    sessionId: "S-ONE",
    idempotencyKey: "patient_pause:not-hex",
    state: "pending",
    createdAt: "2026-08-12T00:00:00Z",
    expiresAt: "2026-08-13T00:00:00Z",
    pauseSessionWseq: null,
  }), null);
  assert.equal(parsePatientPauseOutbox({
    version: 1,
    sessionId: "S-ONE",
    idempotencyKey: "patient_pause:0123456789abcdef0123456789abcdef",
    state: "pending",
    createdAt: "2026-08-12T00:00:00Z",
    expiresAt: "2026-08-13T00:00:00Z",
    pauseSessionWseq: null,
    capability: "must-never-store-a-bearer",
  }), null);
});

test("an expired persistent pause never disappears or retries as a fresh intent", () => {
  const storage = new MemoryStorage();
  const pending = createPatientPauseOutbox(
    "S-ONE", () => UUID, () => new Date("2026-08-12T00:00:00Z"));
  savePatientPauseOutbox(storage, pending);

  assert.equal(loadPatientPauseOutbox(
    storage, "S-ONE", () => new Date("2026-08-13T00:00:00Z"),
  )?.state, "server_resolution_required");
  assert.notEqual(loadPatientPauseOutbox(storage, "S-ONE"), null);
});

test("a pause key remains a stop signal until paused then resumed, then becomes a tombstone", () => {
  const storage = new MemoryStorage();
  const pending = createPatientPauseOutbox(
    "S-ONE", () => UUID, () => new Date("2026-08-12T00:00:00Z"));
  savePatientPauseOutbox(storage, pending);
  assert.deepEqual(patientPauseOutboxForStopSignal(
    storage, "S-ONE", pending.idempotencyKey), pending);

  const observed = observePatientPauseOnServer(storage, pending, 200);
  assert.equal(observed.state, "server_pause_observed");
  assert.equal(observed.pauseSessionWseq, 200);
  // A lagging tab must still physically stop even if another tab has already
  // observed the paused projection.
  assert.deepEqual(patientPauseOutboxForStopSignal(
    storage, "S-ONE", pending.idempotencyKey), observed);
  assert.equal(patientPauseCanResolveAfterResume(
    observed, undefined, false, false), false);
  assert.equal(patientPauseCanResolveAfterResume(
    observed, 200, true, false), false);
  assert.equal(patientPauseCanResolveAfterResume(
    observed, 201, false, true), false);
  assert.equal(patientPauseCanResolveAfterResume(
    observed, 199, false, false), false);
  assert.equal(patientPauseCanResolveAfterResume(
    observed, 200, false, false), false);
  assert.equal(patientPauseCanResolveAfterResume(
    observed, 201, false, false), true);

  const resolved = resolvePatientPauseOutbox(storage, observed, 201, false, false);
  assert.equal(resolved.state, "resolved");
  assert.equal(patientPauseOutboxForStopSignal(
    storage, "S-ONE", pending.idempotencyKey), null);
  assert.equal(patientPauseOutboxForStopSignal(
    storage, "S-ONE", "patient_pause:ffffffffffffffffffffffffffffffff"), null);
  assert.equal(loadPatientPauseOutbox(
    storage, "S-ONE", () => new Date("2026-08-13T00:00:00Z")), null);
});

test("late tabs cannot roll a pause epoch backward or overwrite a newer key", () => {
  const storage = new MemoryStorage();
  const first = createPatientPauseOutbox(
    "S-ONE", () => UUID, () => new Date("2026-08-12T00:00:00Z"));
  savePatientPauseOutbox(storage, first);
  const observed = observePatientPauseOnServer(storage, first, 200);
  assert.deepEqual(acknowledgePatientPauseOutbox(storage, first), observed);
  assert.deepEqual(loadPatientPauseOutbox(storage, "S-ONE"), observed);

  const resolved = resolvePatientPauseOutbox(storage, observed, 201, false, false);
  assert.deepEqual(requirePatientPauseServerResolution(storage, first), resolved);
  assert.deepEqual(acknowledgePatientPauseOutbox(storage, first), resolved);

  const second = createPatientPauseOutbox(
    "S-ONE",
    () => "fedcba98-7654-3210-fedc-ba9876543210",
    () => new Date("2026-08-12T00:10:00Z"),
  );
  savePatientPauseOutbox(storage, second);
  assert.deepEqual(observePatientPauseOnServer(storage, first, 201), second);
  assert.deepEqual(resolvePatientPauseOutbox(
    storage, observed, 202, false, false), second);
  assert.deepEqual(loadPatientPauseOutbox(storage, "S-ONE"), second);
});

test("resolve rechecks the newest shared pause clock inside the serialized transition", () => {
  const storage = new MemoryStorage();
  const pending = createPatientPauseOutbox(
    "S-ONE", () => UUID, () => new Date("2026-08-12T00:00:00Z"));
  savePatientPauseOutbox(storage, pending);
  const oldObserved = observePatientPauseOnServer(storage, pending, 200);
  // Another serialized observer owns a newer paused frame before this tab's
  // old active frame attempts to resolve.
  const newerObserved = {
    ...oldObserved, pauseSessionWseq: 300,
  };
  savePatientPauseOutbox(storage, newerObserved);
  assert.deepEqual(resolvePatientPauseOutbox(
    storage, oldObserved, 201, false, false), newerObserved);
  assert.deepEqual(loadPatientPauseOutbox(storage, "S-ONE"), newerObserved);
  assert.equal(resolvePatientPauseOutbox(
    storage, newerObserved, 301, false, false).state, "resolved");
});

test("a failed durable transition throws and never returns a fictitious local stage", () => {
  const storage = new MemoryStorage();
  const pending = createPatientPauseOutbox(
    "S-ONE", () => UUID, () => new Date("2026-08-12T00:00:00Z"));
  savePatientPauseOutbox(storage, pending);
  storage.failWrites = true;
  assert.throws(
    () => observePatientPauseOnServer(storage, pending, 200),
    /无法持久保存/,
  );
  storage.failWrites = false;
  assert.deepEqual(loadPatientPauseOutbox(storage, "S-ONE"), pending);

  const observed = observePatientPauseOnServer(storage, pending, 200);
  storage.failWrites = true;
  assert.throws(
    () => resolvePatientPauseOutbox(storage, observed, 201, false, false),
    /无法持久保存/,
  );
  storage.failWrites = false;
  assert.deepEqual(loadPatientPauseOutbox(storage, "S-ONE"), observed);
});

test("a missing shared intent cannot masquerade as a successful transition", () => {
  const storage = new MemoryStorage();
  const pending = createPatientPauseOutbox(
    "S-ONE", () => UUID, () => new Date("2026-08-12T00:00:00Z"));
  assert.throws(
    () => acknowledgePatientPauseOutbox(storage, pending),
    /共享暂停状态缺失/,
  );
  assert.throws(
    () => observePatientPauseOnServer(storage, pending, 200),
    /共享暂停状态缺失/,
  );
  assert.throws(
    () => resolvePatientPauseOutbox(storage, {
      ...pending, state: "server_pause_observed", pauseSessionWseq: 200,
    }, 201, false, false),
    /共享暂停状态缺失/,
  );
});

test("pointerdown orders physical stop before storage/network and legacy path discards", () => {
  const shell = readFileSync(new URL("./PatientShell.tsx", import.meta.url), "utf8");
  const stopStart = shell.indexOf("const stopLocallyForPatientPause");
  const requestStart = shell.indexOf("const requestPatientPause", stopStart);
  const requestEnd = shell.indexOf("// A refresh or same-tab", requestStart);
  const localStop = shell.slice(stopStart, requestStart);
  const handler = shell.slice(requestStart, requestEnd);
  const ttsStop = localStop.indexOf("stopSpeaking()");
  const autopilotStop = localStop.indexOf("stopAutopilotForPatientPauseRef.current()");
  const legacyStop = localStop.indexOf("discardLegacyRecordingRef.current?.()");
  const invokeStop = handler.indexOf("stopLocallyForPatientPause(sessionId)");
  const storage = handler.indexOf("loadPatientPauseOutbox");
  const failedSave = handler.indexOf("if (!saved)");
  const failureReturn = handler.indexOf("return;", failedSave);
  const setOutboxLatch = handler.indexOf("setPatientPauseLatch(pending)");
  const broadcast = handler.indexOf('type: "patientPauseStop"');
  assert.ok(ttsStop >= 0 && autopilotStop > ttsStop && legacyStop > autopilotStop);
  assert.ok(invokeStop >= 0 && storage > invokeStop);
  assert.ok(failedSave > storage && failureReturn > failedSave);
  assert.ok(setOutboxLatch > failureReturn && broadcast > setOutboxLatch);
  assert.match(shell, /onPointerDown=\{requestPatientPause\}/);
  assert.match(shell, /type: "patientPauseStop"/);
  assert.match(shell,
    /stopLocallyForPatientPause\(message\.sessionId, session\.paused === true\)/);
  assert.match(shell, /useLayoutEffect\(\(\) => bus\.subscribe/);
  assert.match(shell, /patientPauseOutboxForStopSignal/);
  assert.match(shell, /session\?\.wseq/);
  assert.match(shell,
    /useLayoutEffect\(\(\) => \{\s*const sessionId = session\?\.sessionId;\s*setPatientPauseStorageFailed\(false\)/);
  assert.equal(PATIENT_PAUSE_STORAGE_LOCK_NAME, "nmu-patient-pause-storage:v1");
  assert.equal(
    (shell.match(/PATIENT_PAUSE_STORAGE_LOCK_NAME/g) ?? []).length,
    3,
  );
  assert.doesNotMatch(shell, /nmu-patient-pause:\$\{sessionId\}/);
  assert.doesNotMatch(shell, /catch\(\(\) => \{[\s\S]{0,500}persistOneIntent\(\)/);
  assert.match(shell, /then\(\s*\(\) => \{[\s\S]{0,300}advanceShared/);
  assert.match(shell, /window\.localStorage/);

  const css = readFileSync(new URL("../index.css", import.meta.url), "utf8");
  assert.match(css, /--patient-pause-dock-space:\s*calc\(168px/);
  assert.equal(
    (css.match(/padding:[^;]*var\(--patient-pause-dock-space\)/g) ?? []).length,
    2,
  );

  const vox = readFileSync(new URL("./useVoxRecorder.ts", import.meta.url), "utf8");
  const discardStart = vox.indexOf("const discardForPatientPause");
  const discardEnd = vox.indexOf("const permitIsCurrent", discardStart);
  const discard = vox.slice(discardStart, discardEnd);
  assert.match(discard, /discardActive\(\{ force: true \}\)/);
  assert.doesNotMatch(discard, /stopAndSave\(/);
});
