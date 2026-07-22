import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build as viteBuild } from "vite";

import {
  BUILD_PROVENANCE_NAME,
  DIST_MANIFEST_NAME,
  assertNoSensitiveContentInDist,
  assertToolchainMatchesLock,
  protectedBrowserModuleGraph,
  writeBrowserBuildEvidence,
} from "./build-integrity.mjs";

const ACTUAL_WEB_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function fixture() {
  // macOS exposes /var as a symlink to /private/var.  Use the canonical root
  // so Vite's emitted HTML name stays inside its configured project root.
  const root = realpathSync(mkdtempSync(join(tmpdir(), "nmu-browser-boundary-")));
  const webRoot = join(root, "web");
  mkdirSync(join(webRoot, "src"), { recursive: true });
  writeFileSync(join(webRoot, "index.html"), '<script type="module" src="/src/main.js"></script>\n');
  return { root, webRoot };
}

async function buildFixture(webRoot) {
  return viteBuild({
    configFile: false,
    root: webRoot,
    publicDir: false,
    logLevel: "silent",
    plugins: [protectedBrowserModuleGraph({ webRoot })],
    build: { outDir: "dist", emptyOutDir: true, assetsInlineLimit: 0 },
  });
}

test("real Vite build allows ordinary web-local modules", async () => {
  const { webRoot } = fixture();
  writeFileSync(join(webRoot, "src", "safe.json"), '{"label":"ordinary local setting"}\n');
  writeFileSync(join(webRoot, "src", "main.js"), 'import row from "./safe.json"; document.body.textContent = row.label;\n');
  await buildFixture(webRoot);
  assert.match(readFileSync(join(webRoot, "dist", "index.html"), "utf8"), /assets\/index-/u);
});

test("real Vite build rejects a renamed direct import from repository content", async () => {
  const { root, webRoot } = fixture();
  mkdirSync(join(root, "content"));
  writeFileSync(join(root, "content", "innocent-name.json"), '{"label":"renamed answer payload"}\n');
  writeFileSync(join(webRoot, "src", "main.js"), 'import payload from "../../content/innocent-name.json"; console.log(payload);\n');
  await assert.rejects(
    buildFixture(webRoot),
    /\[browser-build-boundary\] module graph escaped web\//u,
  );
});

test("real Vite build rejects a renamed answer-definition JSON copied under web", async () => {
  const { webRoot } = fixture();
  writeFileSync(
    join(webRoot, "src", "ordinary-name.json"),
    '{"task":{"target_word":"private answer"}}\n',
  );
  writeFileSync(join(webRoot, "src", "main.js"), 'import payload from "./ordinary-name.json"; console.log(payload);\n');
  await assert.rejects(
    buildFixture(webRoot),
    /answer-definition JSON cannot enter the module graph \(target_word\)/u,
  );
});

test("real Vite build rejects raw imports from protected repository paths", async () => {
  const { root, webRoot } = fixture();
  mkdirSync(join(root, "content"));
  writeFileSync(join(root, "content", "renamed.txt"), "private answer bytes\n");
  writeFileSync(join(webRoot, "src", "main.js"), 'import payload from "../../content/renamed.txt?raw"; console.log(payload);\n');
  await assert.rejects(
    buildFixture(webRoot),
    /\[browser-build-boundary\] module graph escaped web\//u,
  );
});

test("final-output scan fails on a frozen sensitive literal and passes clean output", () => {
  const root = mkdtempSync(join(tmpdir(), "nmu-sensitive-dist-"));
  const distRoot = join(root, "dist");
  const contentRoot = join(root, "content");
  mkdirSync(join(distRoot, "assets"), { recursive: true });
  mkdirSync(contentRoot);
  const definition = join(contentRoot, "bank.json");
  const secret = "这是一条足够长且不得进入浏览器的冻结答案话术";
  writeFileSync(definition, JSON.stringify({ tell_answer: secret }));
  writeFileSync(join(distRoot, "assets", "app.js"), `const leaked=${JSON.stringify(secret)};\n`);
  assert.throws(
    () => assertNoSensitiveContentInDist({ distRoot, contentFiles: [definition] }),
    /sensitive frozen-content literal entered assets\/app\.js/u,
  );
  writeFileSync(join(distRoot, "assets", "app.js"), 'const label="clean";\n');
  assert.doesNotThrow(
    () => assertNoSensitiveContentInDist({ distRoot, contentFiles: [definition] }),
  );
});

test("final-output scan catches an exact short target without substring false positives", () => {
  const root = mkdtempSync(join(tmpdir(), "nmu-sensitive-short-dist-"));
  const distRoot = join(root, "dist");
  const contentRoot = join(root, "content");
  mkdirSync(join(distRoot, "assets"), { recursive: true });
  mkdirSync(contentRoot);
  const definition = join(contentRoot, "bank.json");
  writeFileSync(definition, JSON.stringify({ target_word: "锚" }));
  writeFileSync(join(distRoot, "assets", "app.js"), 'const unrelated="锚定状态";\n');
  assert.doesNotThrow(
    () => assertNoSensitiveContentInDist({ distRoot, contentFiles: [definition] }),
  );
  writeFileSync(join(distRoot, "assets", "app.js"), 'const leaked="锚";\n');
  assert.throws(
    () => assertNoSensitiveContentInDist({ distRoot, contentFiles: [definition] }),
    /sensitive frozen-content literal entered assets\/app\.js/u,
  );
});

test("text DLP decodes active BMP, code-point, surrogate-pair, hex, and mixed escapes", () => {
  const root = mkdtempSync(join(tmpdir(), "nmu-sensitive-escaped-dist-"));
  const distRoot = join(root, "dist");
  const contentRoot = join(root, "content");
  mkdirSync(join(distRoot, "assets"), { recursive: true });
  mkdirSync(contentRoot);
  const definition = join(contentRoot, "bank.json");
  const longAnswer = "这是需要保密的完整长答案";
  writeFileSync(definition, JSON.stringify({
    target_word: "锚",
    left_word: "😀",
    acceptable_expressions: ["AI"],
    tell_answer: longAnswer,
  }));
  const output = join(distRoot, "assets", "app.js");
  for (const escapedLeak of [
    'const leaked="\\u951a";\n',
    'const leaked="\\u{951a}";\n',
    'const leaked="\\uD83D\\uDE00";\n',
    'const leaked="\\u{1F600}";\n',
    'const leaked="\\x41I";\n',
    'const leaked="这是需要保密的完整长答\\u6848";\n',
  ]) {
    writeFileSync(output, escapedLeak);
    assert.throws(
      () => assertNoSensitiveContentInDist({ distRoot, contentFiles: [definition] }),
      /sensitive frozen-content literal entered assets\/app\.js/u,
    );
  }
});

test("text DLP leaves even-backslash Unicode text literal and unrelated substrings alone", () => {
  const root = mkdtempSync(join(tmpdir(), "nmu-sensitive-even-slash-dist-"));
  const distRoot = join(root, "dist");
  const contentRoot = join(root, "content");
  mkdirSync(join(distRoot, "assets"), { recursive: true });
  mkdirSync(contentRoot);
  const definition = join(contentRoot, "bank.json");
  writeFileSync(definition, JSON.stringify({ target_word: "锚" }));
  writeFileSync(
    join(distRoot, "assets", "app.js"),
    'const literal="\\\\u951a"; const unrelated="锚定状态";\n',
  );
  assert.doesNotThrow(
    () => assertNoSensitiveContentInDist({ distRoot, contentFiles: [definition] }),
  );
});

test("real Vite renamed binary assets reject raw and escaped short/long leaks", async () => {
  const cases = [
    {
      definition: { tell_answer: "二进制资产中不得出现的完整冻结答案话术" },
      bytes: "prefix:二进制资产中不得出现的完整冻结答案话术:suffix",
    },
    { definition: { target_word: "锚" }, bytes: 'const leaked="\\u951a";' },
    {
      definition: { tell_answer: "这是二进制里的完整长答案" },
      bytes: 'const leaked="这是二进制里的完整长答\\u6848";',
    },
  ];
  for (const row of cases) {
    const { root, webRoot } = fixture();
    const definition = join(root, "bank.json");
    writeFileSync(definition, JSON.stringify(row.definition));
    writeFileSync(join(webRoot, "src", "ordinary-data.bin"), Buffer.from(row.bytes, "utf8"));
    writeFileSync(
      join(webRoot, "src", "main.js"),
      'import url from "./ordinary-data.bin?url"; document.body.dataset.asset = url;\n',
    );
    await buildFixture(webRoot);
    assert.throws(
      () => assertNoSensitiveContentInDist({
        distRoot: join(webRoot, "dist"), contentFiles: [definition],
      }),
      (error) => (
        /sensitive frozen-content literal entered assets\//u.test(error.message)
        && /sha256=[0-9a-f]{64}/u.test(error.message)
        && !Object.values(row.definition).some((value) => error.message.includes(value))
      ),
    );
  }
});

test("toolchain lock comparison accepts equality and rejects a forged mismatch", () => {
  const observed = { vite: "8.1.4", typescript: "6.0.3", react_plugin: "6.0.3" };
  assert.doesNotThrow(() => assertToolchainMatchesLock({ ...observed }, observed));
  assert.throws(
    () => assertToolchainMatchesLock({ ...observed, vite: "0.0.0-forged" }, observed),
    /does not match package-lock/u,
  );
});

test("build evidence compares a fake lock against actually resolved installed packages", () => {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "nmu-toolchain-evidence-")));
  const webRoot = join(root, "web");
  const distRoot = join(webRoot, "dist");
  mkdirSync(distRoot, { recursive: true });
  writeFileSync(join(distRoot, "index.html"), "<!doctype html>\n");
  writeFileSync(join(webRoot, "package.json"), '{"private":true,"type":"module"}\n');
  symlinkSync(join(ACTUAL_WEB_ROOT, "node_modules"), join(webRoot, "node_modules"), "dir");
  const actualLock = JSON.parse(readFileSync(join(ACTUAL_WEB_ROOT, "package-lock.json"), "utf8"));
  const packages = {
    "node_modules/vite": { ...actualLock.packages["node_modules/vite"] },
    "node_modules/typescript": { ...actualLock.packages["node_modules/typescript"] },
    "node_modules/@vitejs/plugin-react": {
      ...actualLock.packages["node_modules/@vitejs/plugin-react"],
    },
  };
  packages["node_modules/vite"].version = "0.0.0-forged";
  writeFileSync(join(webRoot, "package-lock.json"), JSON.stringify({ packages }));
  const options = {
    distRoot,
    webRoot,
    buildFingerprint: "b".repeat(64),
    buildId: "1234567890",
  };
  assert.throws(() => writeBrowserBuildEvidence(options), /does not match package-lock/u);
  packages["node_modules/vite"].version = actualLock.packages["node_modules/vite"].version;
  writeFileSync(join(webRoot, "package-lock.json"), JSON.stringify({ packages }));
  assert.doesNotThrow(() => writeBrowserBuildEvidence(options));
});

test("dist SHA-256 manifest is deterministic, covers provenance, and excludes only itself", () => {
  const root = mkdtempSync(join(tmpdir(), "nmu-dist-evidence-"));
  const distRoot = join(root, "dist");
  mkdirSync(join(distRoot, "assets"), { recursive: true });
  writeFileSync(join(distRoot, "index.html"), "<!doctype html>\n");
  writeFileSync(join(distRoot, "assets", "app.js"), "export{};\n");
  const options = {
    distRoot,
    webRoot: ACTUAL_WEB_ROOT,
    buildFingerprint: "a".repeat(64),
    buildId: "1234567890",
  };
  writeBrowserBuildEvidence(options);
  const first = readFileSync(join(distRoot, DIST_MANIFEST_NAME), "utf8");
  writeBrowserBuildEvidence(options);
  const second = readFileSync(join(distRoot, DIST_MANIFEST_NAME), "utf8");
  assert.equal(second, first);

  const manifest = JSON.parse(first);
  assert.deepEqual(manifest.excluded_paths, [DIST_MANIFEST_NAME]);
  assert.deepEqual(
    manifest.files.map((row) => row.path),
    ["assets/app.js", BUILD_PROVENANCE_NAME, "index.html"],
  );
  for (const row of manifest.files) {
    assert.match(row.sha256, /^[0-9a-f]{64}$/u);
    assert.equal(Number.isInteger(row.size), true);
    const bytes = readFileSync(join(distRoot, row.path));
    assert.equal(row.size, bytes.length);
    assert.equal(row.sha256, createHash("sha256").update(bytes).digest("hex"));
  }
  const provenance = JSON.parse(readFileSync(join(distRoot, BUILD_PROVENANCE_NAME), "utf8"));
  assert.equal(provenance.observed_runtime_versions.node, process.version);
  assert.deepEqual(
    provenance.declared_lockfile_versions,
    {
      vite: provenance.observed_runtime_versions.vite,
      typescript: provenance.observed_runtime_versions.typescript,
      react_plugin: provenance.observed_runtime_versions.react_plugin,
    },
  );
  assert.match(provenance.fingerprint_scope.limitation, /Not a cross-environment/u);
});
