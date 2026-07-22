import assert from "node:assert/strict";
import test from "node:test";
import { operationalReadinessPolicy } from "./operationalReadiness.ts";

test("unfrozen operational rubrics block research but allow only supervised simulation", () => {
  assert.deepEqual(operationalReadinessPolicy("research", false), {
    operationalAutopilotAllowed: false,
    blocksSessionCreation: true,
    mode: "research_blocked",
  });
  assert.deepEqual(operationalReadinessPolicy("simulation", false), {
    operationalAutopilotAllowed: false,
    blocksSessionCreation: false,
    mode: "supervised_simulation",
  });
});

test("only an explicit ready declaration enables operational autopilot", () => {
  assert.equal(operationalReadinessPolicy("research", true).operationalAutopilotAllowed, true);
  assert.equal(operationalReadinessPolicy(null, false).operationalAutopilotAllowed, false);
});
