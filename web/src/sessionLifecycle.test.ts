import assert from "node:assert/strict";
import test from "node:test";
import {
  canExportCompletedSession,
  isSessionTerminalStatus,
  sessionStatusMeta,
  shouldAutoRouteTrainingToWrapup,
  wrapupMode,
} from "./sessionLifecycle.ts";

test("intervention completion is terminal for training but routes to research review", () => {
  assert.equal(isSessionTerminalStatus("intervention_completed"), true);
  assert.equal(sessionStatusMeta("intervention_completed").action, "review");
  assert.equal(wrapupMode("intervention_completed"), "review");
});

test("every server terminal fact routes an open training page to the correct wrapup mode", () => {
  assert.equal(shouldAutoRouteTrainingToWrapup("active"), false);
  assert.equal(shouldAutoRouteTrainingToWrapup("paused"), false);
  assert.equal(shouldAutoRouteTrainingToWrapup("intervention_completed"), true);
  assert.equal(shouldAutoRouteTrainingToWrapup("completed"), true);
  assert.equal(shouldAutoRouteTrainingToWrapup("aborted"), true);
  assert.equal(shouldAutoRouteTrainingToWrapup("failed"), true);
  assert.equal(wrapupMode("intervention_completed"), "review");
  assert.equal(wrapupMode("completed"), "completed");
  assert.equal(wrapupMode("aborted"), "closed");
  assert.equal(shouldAutoRouteTrainingToWrapup(undefined), false);
});

test("only active and paused sessions resume training", () => {
  assert.equal(sessionStatusMeta("active").action, "resume");
  assert.equal(sessionStatusMeta("paused").action, "resume");
  assert.equal(sessionStatusMeta("completed").action, "view");
  assert.equal(sessionStatusMeta("aborted").action, null);
});

test("export requires completed runtime, strict gate, and data role", () => {
  assert.equal(canExportCompletedSession({
    status: "intervention_completed", strictCompletionGatePassed: true, roleAllowsExport: true,
  }), false);
  assert.equal(canExportCompletedSession({
    status: "completed", strictCompletionGatePassed: false, roleAllowsExport: true,
  }), false);
  assert.equal(canExportCompletedSession({
    status: "completed", strictCompletionGatePassed: true, roleAllowsExport: false,
  }), false);
  assert.equal(canExportCompletedSession({
    status: "completed", strictCompletionGatePassed: true, roleAllowsExport: true,
  }), true);
});
