import assert from "node:assert/strict";
import test from "node:test";
import {
  qualityDashboardRequestClassification,
  qualityDashboardRequestPath,
} from "./qualityDashboardRequestPolicy.ts";

test("quality aggregation is requested only for explicit research or simulation boundaries", () => {
  assert.equal(qualityDashboardRequestClassification("research"), "research");
  assert.equal(qualityDashboardRequestClassification("simulation"), "simulation");
  assert.equal(qualityDashboardRequestClassification("legacy_unknown"), null);
});

test("quality request binds the selected data classification in the query", () => {
  assert.equal(
    qualityDashboardRequestPath("research"),
    "/quality/ai-metrics?data_classification=research",
  );
  assert.equal(
    qualityDashboardRequestPath("simulation"),
    "/quality/ai-metrics?data_classification=simulation",
  );
});
