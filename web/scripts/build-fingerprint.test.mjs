import assert from "node:assert/strict";
import { mkdtempSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { buildIdFromFingerprint, hashBuildInputs } from "./build-fingerprint.mjs";

test("build fingerprint is stable for identical bytes and ignores mtime", () => {
  const root = mkdtempSync(join(tmpdir(), "nmu-build-fingerprint-"));
  const first = join(root, "first.ts");
  const second = join(root, "frozen.json");
  writeFileSync(first, "export const value = 1\n");
  writeFileSync(second, '{"version":"v1"}\n');
  const entries = [
    { label: "web/src/first.ts", path: first },
    { label: "content/frozen.json", path: second },
  ];

  const initial = hashBuildInputs(entries);
  const repeated = hashBuildInputs([...entries].reverse());
  utimesSync(first, new Date(1_000), new Date(2_000));
  const touched = hashBuildInputs(entries);

  assert.match(initial, /^[0-9a-f]{64}$/);
  assert.equal(repeated, initial);
  assert.equal(touched, initial);
  assert.match(buildIdFromFingerprint(initial), /^\d{10,}$/);
});

test("changing one build input changes the full SHA-256 identity", () => {
  const root = mkdtempSync(join(tmpdir(), "nmu-build-fingerprint-change-"));
  const source = join(root, "main.tsx");
  const entries = [{ label: "web/src/main.tsx", path: source }];
  writeFileSync(source, "export const version = 1\n");
  const before = hashBuildInputs(entries);

  writeFileSync(source, "export const version = 2\n");
  const after = hashBuildInputs(entries);

  assert.notEqual(after, before);
  assert.notEqual(buildIdFromFingerprint(after), buildIdFromFingerprint(before));
});
