import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("./ConsoleShell.tsx", import.meta.url), "utf8");

test("caregiver workspace is conditionally loaded and returned before research workspace", () => {
  assert.match(source, /lazy\(async \(\) =>/);
  assert.match(source, /import\("\.\.\/caregiver\/CaregiverWorkspaceEntry"\)/);
  const caregiverBranch = source.indexOf("if (identityIsCaregiverOperator(auth.identity))");
  const researchMount = source.indexOf("<ConsoleWorkspace", caregiverBranch);
  assert.notEqual(caregiverBranch, -1);
  assert.notEqual(researchMount, -1);
  assert.ok(caregiverBranch < researchMount);
  const branch = source.slice(caregiverBranch, researchMount);
  assert.match(branch, /return\s*\(/);
  assert.match(branch, /<CaregiverWorkspaceEntry/);
});

test("caregiver entry injects only the isolated caregiver API", () => {
  const entry = readFileSync(
    new URL("../caregiver/CaregiverWorkspaceEntry.tsx", import.meta.url),
    "utf8",
  );
  assert.match(entry, /api=\{caregiverApi\}/);
  assert.doesNotMatch(entry, /ConsoleWorkspace|TrainingConsoleScreen|AnalysisScreen|PatientShell/);
});
