import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  bedsideOperationIsCurrent,
  captureBedsideOperation,
  type BedsideOperationState,
} from "./bedsideOperationFence.ts";

function state(overrides: Partial<BedsideOperationState> = {}): BedsideOperationState {
  return {
    sessionId: "S-A",
    turnKey: "SE_花#1",
    epoch: 7,
    blocked: false,
    autopilotActive: true,
    ...overrides,
  };
}

test("pause, wrap-up, terminal transition, or unmount epoch invalidates slow continuations", () => {
  const original = state();
  const fence = captureBedsideOperation(original, false);

  assert.equal(bedsideOperationIsCurrent(fence, original), true);
  assert.equal(bedsideOperationIsCurrent(fence, state({ blocked: true })), false);
  assert.equal(bedsideOperationIsCurrent(fence, state({ epoch: 8 })), false);
});

test("a continuation cannot cross a session or frozen turn boundary", () => {
  const fence = captureBedsideOperation(state(), false);

  assert.equal(bedsideOperationIsCurrent(fence, state({ sessionId: "S-B" })), false);
  assert.equal(bedsideOperationIsCurrent(fence, state({ turnKey: "SE_锚#1" })), false);
});

test("autopilot continuations additionally require current ownership", () => {
  const automatic = captureBedsideOperation(state(), true);
  const manual = captureBedsideOperation(state(), false);

  assert.equal(bedsideOperationIsCurrent(automatic, state({ autopilotActive: false })), false);
  assert.equal(bedsideOperationIsCurrent(manual, state({ autopilotActive: false })), true);
});

test("training console uses the fence and atomic presentation route at every exit boundary", () => {
  const source = readFileSync(
    new URL("./TrainingConsoleScreen.tsx", import.meta.url), "utf8");

  assert.match(source, /commitInteractionPresentation\(/);
  assert.doesNotMatch(source, /api\.commitInteractionPresentation/);
  assert.doesNotMatch(source, /appendCueEvidence/);
  assert.match(source, /async function pauseTraining[\s\S]*?invalidateBedsideOperations\(\)/);
  assert.match(source, /async function enterWrapup[\s\S]*?invalidateBedsideOperations\(\)/);
  assert.match(source, /if \(!operationIsCurrent\(fence\)\) return;/);
  assert.match(source, /disabled=\{level !== cueLevel \+ 1 \|\| text == null\}/);
  assert.match(source, /idempotency_key: stablePresentationKey\(operationKey\)/);
  assert.doesNotMatch(source, /fbSeq,\s*\n\s*\}/);
  assert.match(source, /api\.commitTechnicalPause\(session\.session_id, exact\)/);
  assert.doesNotMatch(source, /api\.appendInteraction/);
  assert.match(source, /evidence\?\.alreadyRecorded \? undefined/);

  const pauseBody = source.slice(
    source.indexOf("async function pauseTraining"),
    source.indexOf("async function resumeTraining"),
  );
  assert.ok(pauseBody.indexOf("beginSafetyPause()") < pauseBody.indexOf("runtimeControl.pause()"));
  assert.ok(pauseBody.indexOf("beginSafetyPause()") < pauseBody.indexOf("commitAtomicTechnicalPause(evidence)"));
  assert.doesNotMatch(pauseBody, /flushLiveWrites/);
  assert.doesNotMatch(pauseBody, /releaseSafetyPause/);
});

test("technical pause retries reuse one exact request and never publish a slow cursor", () => {
  const source = readFileSync(
    new URL("./TrainingConsoleScreen.tsx", import.meta.url), "utf8");
  const helper = source.slice(
    source.indexOf("async function commitAtomicTechnicalPause"),
    source.indexOf("async function pauseTraining"),
  );
  assert.match(helper, /pendingTechnicalPause\.current = exact/);
  assert.equal(
    helper.match(/api\.commitTechnicalPause\(session\.session_id, exact\)/g)?.length,
    2,
  );
  assert.match(helper, /technicalPauseDispositionIsUncertain/);
  assert.doesNotMatch(helper, /postCursor|publishCommittedCursor|releaseSafetyPause/);
  assert.match(source, /pauseOperationInFlight\.current/);
  assert.match(source, /pendingTechnicalPause\.current = null[\s\S]*session\.session_id/);
});

test("a latched technical failure must be reconciled before the ordinary resume control opens", () => {
  const source = readFileSync(
    new URL("./TrainingConsoleScreen.tsx", import.meta.url), "utf8");
  // observerMode ⊇ serverOwned：服务器拥有、尚不能证伪拥有、重同步未完成都锁 resume。
  assert.match(
    source,
    /resumeBlocked=\{observerMode \|\| Boolean\(apFailure\)\}/,
  );
  assert.match(source, /reconcilePendingTechnicalPauseForTakeover\(/);
});

test("both console planes preempt the live queue before authoritative pause", () => {
  const source = readFileSync(
    new URL("../relationship/RelationshipConsoleScreen.tsx", import.meta.url), "utf8");
  const pauseBody = source.slice(
    source.indexOf("async function pauseRapport"),
    source.indexOf("async function resumeRapport"),
  );
  assert.ok(pauseBody.indexOf("beginSafetyPause()") < pauseBody.indexOf("runtimeControl.pause()"));
  assert.doesNotMatch(pauseBody, /postRapport|flushLiveWrites/);
});

test("manual evidence, confirmation, and lock continuations all recheck the bedside fence", () => {
  const source = readFileSync(
    new URL("./TrainingConsoleScreen.tsx", import.meta.url), "utf8");
  const sections = [
    ["async function ensureItemEvent", "const [busyOp"],
    ["async function processManualAudio", "async function tryLocalAsr"],
    ["async function tryLocalAsr", "async function saveTranscription"],
    ["async function saveTranscription", "async function persistWorkConfirmation"],
    ["async function persistWorkConfirmation", "async function saveConfirm"],
    ["async function saveConfirm", "const [locking"],
    ["async function doLock", "// ---------------- 自动驾驶"],
  ] as const;

  for (const [start, end] of sections) {
    const body = source.slice(source.indexOf(start), source.indexOf(end));
    assert.notEqual(body.length, 0, `${start} should be present`);
    assert.match(body, /operationIsCurrent\(fence\)/, `${start} must fence slow continuations`);
  }
  for (const start of [
    "async function tryLocalAsr",
    "async function saveTranscription",
    "async function saveConfirm",
    "async function doLock",
  ]) {
    const body = source.slice(source.indexOf(start), source.indexOf(start) + 1_800);
    assert.match(body, /const fence = captureOperation\(\)/, `${start} must capture its own epoch`);
  }
});
