import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { extname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const sourceRoot = fileURLToPath(new URL(".", import.meta.url));
const retiredSessionCreateScreen = new URL("./console/SessionCreateScreen.tsx", import.meta.url);
const directSessionRequest = /\breq(?:<[^>]+>)?\s*\(\s*["'`]POST["'`]\s*,\s*["'`]\/sessions["'`]/;
const directSessionFetch = /\bfetch\s*\(\s*["'`]\/sessions["'`]\s*,\s*\{[\s\S]{0,500}?\bmethod\s*:\s*["'`]POST["'`]/;

function frontendSources(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return frontendSources(path);
    if (!entry.isFile() || ![".ts", ".tsx"].includes(extname(entry.name)) || entry.name.endsWith(".test.ts")) {
      return [];
    }
    return [path];
  });
}

test("browser session admission is only exposed through an approved VisitPlan", () => {
  assert.equal(
    existsSync(retiredSessionCreateScreen),
    false,
    "the retired bedside SessionCreateScreen must not be restored",
  );

  const apiSource = readFileSync(new URL("./api.ts", import.meta.url), "utf8");
  assert.doesNotMatch(apiSource, /\bcreateSession\s*:/, "api must not expose raw session creation");
  assert.match(apiSource, /\bstartVisitPlan\s*:/, "VisitPlan start must remain the browser admission path");

  for (const path of frontendSources(sourceRoot)) {
    const source = readFileSync(path, "utf8");
    assert.doesNotMatch(source, directSessionRequest, `raw POST /sessions request is forbidden in ${path}`);
    assert.doesNotMatch(source, directSessionFetch, `raw POST /sessions fetch is forbidden in ${path}`);
  }
});
