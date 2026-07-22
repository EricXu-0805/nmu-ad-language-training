import assert from "node:assert/strict";
import test from "node:test";

import "./subjectWithdrawal.test.ts";
import {
  partitionByDataClassification,
  patientDataClassification,
  sessionCreationPolicy,
  sessionDataClassification,
  sessionMatchesPatientBoundary,
} from "./dataClassification.ts";

test("patient classification fails closed when the persisted flag is missing", () => {
  assert.equal(patientDataClassification({ is_simulation_subject: false }), "research");
  assert.equal(patientDataClassification({ is_simulation_subject: true }), "simulation");
  assert.equal(patientDataClassification({}), "legacy_unknown");
});

test("session classification never guesses from legacy is_simulation", () => {
  assert.equal(sessionDataClassification({ data_classification: "research" }), "research");
  assert.equal(sessionDataClassification({ data_classification: "simulation" }), "simulation");
  assert.equal(sessionDataClassification({}), "legacy_unknown");
  assert.equal(sessionDataClassification({ data_classification: "invalid" }), "legacy_unknown");
});

test("research, simulation and legacy records are partitioned without mixing", () => {
  const rows = [
    { id: "R", data_classification: "research" },
    { id: "S", data_classification: "simulation" },
    { id: "L" },
  ];
  const sections = partitionByDataClassification(rows, sessionDataClassification);
  assert.deepEqual(sections.research.map((row) => row.id), ["R"]);
  assert.deepEqual(sections.simulation.map((row) => row.id), ["S"]);
  assert.deepEqual(sections.legacy_unknown.map((row) => row.id), ["L"]);
});

test("draft content blocks real patients instead of silently downgrading them", () => {
  assert.deepEqual(sessionCreationPolicy("research", false), {
    allowed: false,
    isSimulation: false,
    reason: "当前题库尚未通过真实研究质控，真实受试者不得开场",
  });
  assert.deepEqual(sessionCreationPolicy("simulation", false), {
    allowed: true,
    isSimulation: true,
    reason: null,
  });
  assert.equal(sessionCreationPolicy("legacy_unknown", true).allowed, false);
});

test("a session can enter a patient workflow only when both explicit boundaries match", () => {
  assert.equal(sessionMatchesPatientBoundary("research", "research"), true);
  assert.equal(sessionMatchesPatientBoundary("simulation", "simulation"), true);
  assert.equal(sessionMatchesPatientBoundary("research", "simulation"), false);
  assert.equal(sessionMatchesPatientBoundary("legacy_unknown", "legacy_unknown"), false);
});
