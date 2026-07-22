/** Fail-closed browser build boundary and deterministic release evidence. */
import { createHash } from "node:crypto";
import {
  existsSync,
  lstatSync,
  readFileSync,
  readdirSync,
  realpathSync,
  writeFileSync,
} from "node:fs";
import { createRequire } from "node:module";
import { basename, dirname, extname, isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const DEFAULT_WEB_ROOT = resolve(SCRIPT_DIR, "..");
const DEFAULT_CONTENT_ROOT = resolve(DEFAULT_WEB_ROOT, "../content");

export const DIST_MANIFEST_NAME = "browser-dist-sha256.json";
export const BUILD_PROVENANCE_NAME = "build-provenance.json";

const FORBIDDEN_SOURCE_NAMES = new Set([
  "item_bank_v1.json",
  "week1_script.json",
  "autopilot_protocol_v1.json",
  "patient_asset_delivery_v1.json",
  "training_asset_manifest_v1.json",
]);

const ANSWER_DEFINITION_KEYS = new Set([
  "acceptable_expressions",
  "item_bank_version_id",
  "protocol_version_id",
  "script_version_id",
  "target_word",
  "tell_answer",
  "zodiac_closed_list",
]);

const ANSWER_VALUE_KEYS = new Set([
  "acceptable_expressions",
  "left_word",
  "related_but_inaccurate",
  "right_word",
  "target_word",
  "tell_answer",
]);

const SENSITIVE_TEXT_KEYS = new Set([
  "ask",
  "close",
  "generic_fallback_line",
  "initial_prompt",
  "line",
  "namefix_left",
  "namefix_right",
  "notes",
  "silence",
  "source",
  "success",
  "success_after_cue2",
  "success_line",
  "text",
  "unknown",
]);

const TOOLCHAIN_PACKAGES = Object.freeze([
  ["vite", "vite"],
  ["typescript", "typescript"],
  ["react_plugin", "@vitejs/plugin-react"],
]);

// These are declarations, not claims that a local build actually ran inside
// these images.  Regression tests pin them to Dockerfile/docker-compose.yml.
export const DECLARED_RELEASE_IMAGES = Object.freeze({
  browser_builder: "node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2",
  application_runtime: "python:3.12-alpine3.24@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df",
  edge_runtime: "caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648",
});

function bytewiseCompare(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function portableRelative(root, path) {
  const value = relative(root, path).split(sep).join("/");
  if (!value || value === ".." || value.startsWith("../")) {
    throw new Error(`path escaped declared root: ${value || "."}`);
  }
  return value;
}

function isInside(root, candidate) {
  const value = relative(root, candidate);
  return value === "" || (value !== ".." && !value.startsWith(`..${sep}`) && !isAbsolute(value));
}

function stripModuleDecorators(id) {
  const marker = id.search(/[?#]/u);
  return marker === -1 ? id : id.slice(0, marker);
}

function moduleFilePath(id) {
  if (!id || id.startsWith("\0") || id.startsWith("virtual:")) return null;
  const undecorated = stripModuleDecorators(id);
  if (undecorated.startsWith("file:")) return fileURLToPath(undecorated);
  if (!isAbsolute(undecorated)) return null;
  return undecorated;
}

function sensitiveJsonKeys(value, found = new Set()) {
  if (Array.isArray(value)) {
    for (const row of value) sensitiveJsonKeys(row, found);
  } else if (value && typeof value === "object") {
    for (const [key, row] of Object.entries(value)) {
      if (ANSWER_DEFINITION_KEYS.has(key)) found.add(key);
      sensitiveJsonKeys(row, found);
    }
  }
  return found;
}

function assertProjectModuleAllowed(id, canonicalWebRoot) {
  const path = moduleFilePath(id);
  if (!path || !existsSync(path)) return;
  const canonicalPath = realpathSync(path);
  if (!isInside(canonicalWebRoot, canonicalPath)) {
    throw new Error(
      `[browser-build-boundary] module graph escaped web/: ${canonicalPath}`,
    );
  }
  const relativePath = portableRelative(canonicalWebRoot, canonicalPath);
  const lowerPath = relativePath.toLowerCase();
  if (lowerPath === "public" || lowerPath.startsWith("public/")) {
    throw new Error(
      `[browser-build-boundary] public/ is disabled and cannot enter the module graph: ${relativePath}`,
    );
  }
  if (FORBIDDEN_SOURCE_NAMES.has(basename(canonicalPath).toLowerCase())) {
    throw new Error(
      `[browser-build-boundary] answer-definition filename cannot enter the module graph: ${relativePath}`,
    );
  }
  if (!lowerPath.startsWith("node_modules/") && extname(canonicalPath).toLowerCase() === ".json") {
    let parsed;
    try {
      parsed = JSON.parse(readFileSync(canonicalPath, "utf8"));
    } catch {
      // Vite/Rollup owns ordinary syntax diagnostics.  This guard only adds a
      // confidentiality decision after a JSON document is parseable.
      return;
    }
    const keys = [...sensitiveJsonKeys(parsed)].sort(bytewiseCompare);
    if (keys.length) {
      throw new Error(
        `[browser-build-boundary] answer-definition JSON cannot enter the module graph (${keys.join(", ")}): ${relativePath}`,
      );
    }
  }
}

/**
 * Allow browser modules only from web/ (or its installed node_modules).
 * realpathSync closes symlink and renamed-file aliases; load/transform/
 * moduleParsed/generateBundle provide overlapping coverage for plugin aliases,
 * ?raw/?url imports and dynamic imports.
 */
export function protectedBrowserModuleGraph({ webRoot = DEFAULT_WEB_ROOT } = {}) {
  const canonicalWebRoot = realpathSync(webRoot);
  const check = (id) => assertProjectModuleAllowed(id, canonicalWebRoot);
  return {
    name: "protected-browser-module-graph",
    enforce: "pre",
    load(id) {
      check(id);
      return null;
    },
    transform(_code, id) {
      check(id);
      return null;
    },
    moduleParsed(info) {
      check(info.id);
    },
    generateBundle() {
      for (const id of this.getModuleIds()) check(id);
    },
  };
}

function visitRegularFiles(root, excludedNames = new Set()) {
  const canonicalRoot = realpathSync(root);
  const files = [];
  const visit = (current) => {
    for (const name of readdirSync(current).sort(bytewiseCompare)) {
      const path = resolve(current, name);
      const stat = lstatSync(path);
      if (stat.isSymbolicLink()) {
        throw new Error(`symlink is forbidden in browser build output: ${path}`);
      }
      if (stat.isDirectory()) visit(path);
      else if (stat.isFile() && !excludedNames.has(portableRelative(canonicalRoot, path))) {
        files.push({ path, relativePath: portableRelative(canonicalRoot, path), size: stat.size });
      } else if (!stat.isFile()) {
        throw new Error(`unsupported browser build output type: ${path}`);
      }
    }
  };
  visit(canonicalRoot);
  files.sort((left, right) => bytewiseCompare(left.relativePath, right.relativePath));
  return files;
}

function collectSensitiveLiterals(contentFiles) {
  const values = new Map();
  const visit = (value, key = "") => {
    if (typeof value === "string") {
      // Short answers are matched as complete emitted string literals so a
      // target like “花” does not false-positive on an unrelated “花费”.
      const answerValue = ANSWER_VALUE_KEYS.has(key);
      const exact = answerValue && [...value].length < 8;
      const sensitive = answerValue
        || key.endsWith("_id")
        || key.endsWith("_sha256")
        || key === "item_id"
        || key === "raw_source_ref"
        || (SENSITIVE_TEXT_KEYS.has(key) && [...value].length >= 8);
      if (sensitive && value) {
        values.set(value, { value, exact: exact || values.get(value)?.exact === true });
      }
    } else if (Array.isArray(value)) {
      for (const row of value) visit(row, key);
    } else if (value && typeof value === "object") {
      for (const [childKey, row] of Object.entries(value)) visit(row, childKey);
    }
  };
  for (const path of contentFiles) {
    const stat = lstatSync(path);
    if (stat.isSymbolicLink() || !stat.isFile()) {
      throw new Error(`sensitive definition must be a regular non-symlink file: ${path}`);
    }
    visit(JSON.parse(readFileSync(path, "utf8")));
  }
  return [...values.values()].sort((left, right) => bytewiseCompare(left.value, right.value));
}

function quotedLiteralCandidates(value) {
  const doubleQuoted = JSON.stringify(value);
  const singleQuoted = `'${value.replaceAll("\\", "\\\\").replaceAll("'", "\\'").replaceAll("\n", "\\n").replaceAll("\r", "\\r")}'`;
  const templateQuoted = `\`${value.replaceAll("\\", "\\\\").replaceAll("`", "\\`").replaceAll("${", "\\${").replaceAll("\n", "\\n").replaceAll("\r", "\\r")}\``;
  return [doubleQuoted, singleQuoted, templateQuoted];
}

function decodeActiveJavaScriptEscapes(text) {
  let decoded = "";
  let index = 0;
  while (index < text.length) {
    if (text[index] !== "\\") {
      decoded += text[index];
      index += 1;
      continue;
    }
    let endOfRun = index;
    while (text[endOfRun] === "\\") endOfRun += 1;
    const slashCount = endOfRun - index;
    if (slashCount % 2 === 0 || endOfRun >= text.length) {
      decoded += "\\".repeat(slashCount);
      index = endOfRun;
      continue;
    }
    decoded += "\\".repeat(slashCount - 1);
    const kind = text[endOfRun];
    let escapeEnd = endOfRun + 1;
    let codePoint = null;
    if (kind === "x") {
      const digits = text.slice(endOfRun + 1, endOfRun + 3);
      if (/^[0-9a-f]{2}$/iu.test(digits)) {
        codePoint = Number.parseInt(digits, 16);
        escapeEnd = endOfRun + 3;
      }
    } else if (kind === "u" && text[endOfRun + 1] === "{") {
      const close = text.indexOf("}", endOfRun + 2);
      const digits = close === -1 ? "" : text.slice(endOfRun + 2, close);
      if (/^[0-9a-f]{1,6}$/iu.test(digits)) {
        const candidate = Number.parseInt(digits, 16);
        if (candidate <= 0x10ffff) {
          codePoint = candidate;
          escapeEnd = close + 1;
        }
      }
    } else if (kind === "u") {
      const digits = text.slice(endOfRun + 1, endOfRun + 5);
      if (/^[0-9a-f]{4}$/iu.test(digits)) {
        codePoint = Number.parseInt(digits, 16);
        escapeEnd = endOfRun + 5;
      }
    }
    if (codePoint === null) {
      decoded += "\\";
      index = endOfRun;
    } else {
      decoded += String.fromCodePoint(codePoint);
      index = escapeEnd;
    }
  }
  return decoded;
}

function leakedLiteralInText(text, literals) {
  const decoded = decodeActiveJavaScriptEscapes(text);
  return literals.find(({ value, exact }) => {
    if (exact) {
      const candidates = quotedLiteralCandidates(value);
      return candidates.some((candidate) => text.includes(candidate) || decoded.includes(candidate));
    }
    return text.includes(value) || decoded.includes(value);
  });
}

export function assertNoSensitiveContentInDist({
  distRoot = resolve(DEFAULT_WEB_ROOT, "dist"),
  contentRoot = DEFAULT_CONTENT_ROOT,
  contentFiles = [
    resolve(contentRoot, "item_bank_v1.json"),
    resolve(contentRoot, "week1_script.json"),
    resolve(contentRoot, "autopilot_protocol_v1.json"),
  ],
} = {}) {
  const literals = collectSensitiveLiterals(contentFiles);
  for (const entry of visitRegularFiles(distRoot)) {
    const bytes = readFileSync(entry.path);
    // Every emitted regular file, including unknown/binary extensions, gets a
    // raw-byte DLP pass.  Short answers remain text-literal-only to avoid a
    // one-character substring false positive in unrelated binary data.
    let leaked = literals.find(({ value }) => (
      [...value].length >= 8 && bytes.includes(Buffer.from(value, "utf8"))
    ));
    // Unknown/binary extensions can still carry JavaScript/JSON escape text
    // and later be fetched and interpreted by application code.  Decode every
    // regular output; invalid UTF-8 becomes inert replacement characters.
    if (!leaked) {
      leaked = leakedLiteralInText(bytes.toString("utf8"), literals);
    }
    if (leaked) {
      throw new Error(
        `[browser-build-boundary] sensitive frozen-content literal entered ${entry.relativePath} (sha256=${sha256(Buffer.from(leaked.value, "utf8"))}, chars=${[...leaked.value].length})`,
      );
    }
  }
}

function packageVersion(lock, name) {
  const version = lock?.packages?.[`node_modules/${name}`]?.version;
  if (typeof version !== "string" || !version) {
    throw new Error(`package-lock is missing an exact ${name} version`);
  }
  return version;
}

function observedToolchainVersions(webRoot) {
  const canonicalWebRoot = realpathSync(webRoot);
  const modulesRoot = realpathSync(resolve(canonicalWebRoot, "node_modules"));
  const requireFromWeb = createRequire(resolve(canonicalWebRoot, "package.json"));
  const versions = {};
  for (const [label, packageName] of TOOLCHAIN_PACKAGES) {
    const packageRoot = realpathSync(resolve(modulesRoot, ...packageName.split("/")));
    const resolvedEntry = realpathSync(requireFromWeb.resolve(packageName));
    if (!isInside(packageRoot, resolvedEntry)) {
      throw new Error(`resolved ${packageName} entry escaped its installed package root`);
    }
    const metadataPath = resolve(packageRoot, "package.json");
    const metadataStat = lstatSync(metadataPath);
    if (metadataStat.isSymbolicLink() || !metadataStat.isFile()) {
      throw new Error(`installed ${packageName} metadata must be a regular non-symlink file`);
    }
    const metadata = JSON.parse(readFileSync(metadataPath, "utf8"));
    if (metadata.name !== packageName || typeof metadata.version !== "string" || !metadata.version) {
      throw new Error(`installed ${packageName} metadata is incomplete`);
    }
    versions[label] = metadata.version;
  }
  return versions;
}

export function assertToolchainMatchesLock(declared, observed) {
  for (const [label] of TOOLCHAIN_PACKAGES) {
    if (typeof declared?.[label] !== "string" || typeof observed?.[label] !== "string") {
      throw new Error(`toolchain version evidence is missing ${label}`);
    }
    if (declared[label] !== observed[label]) {
      throw new Error(
        `installed ${label} version ${observed[label]} does not match package-lock ${declared[label]}`,
      );
    }
  }
}

export function writeBrowserBuildEvidence({
  distRoot = resolve(DEFAULT_WEB_ROOT, "dist"),
  webRoot = DEFAULT_WEB_ROOT,
  buildFingerprint,
  buildId,
} = {}) {
  if (!/^[0-9a-f]{64}$/u.test(buildFingerprint ?? "")) {
    throw new Error("build fingerprint must be a lowercase SHA-256 hex digest");
  }
  if (!/^\d+$/u.test(buildId ?? "")) throw new Error("build id must be decimal digits");
  const lock = JSON.parse(readFileSync(resolve(webRoot, "package-lock.json"), "utf8"));
  const declaredLockfileVersions = Object.fromEntries(
    TOOLCHAIN_PACKAGES.map(([label, packageName]) => [label, packageVersion(lock, packageName)]),
  );
  const observedVersions = observedToolchainVersions(webRoot);
  assertToolchainMatchesLock(declaredLockfileVersions, observedVersions);
  const declaredNodeMajor = Number.parseInt(
    DECLARED_RELEASE_IMAGES.browser_builder.match(/^node:(\d+)/u)?.[1] ?? "",
    10,
  );
  const observedNodeMajor = Number.parseInt(process.versions.node.split(".", 1)[0], 10);
  const provenance = {
    schema_version: "nmu.browser-build-provenance.v1",
    build_id: buildId,
    build_input_fingerprint_sha256: buildFingerprint,
    fingerprint_scope: {
      claim: "Digest of the explicitly enumerated repository input bytes only.",
      limitation: "Not a cross-environment reproducibility, base-image, dependency-advisory, SBOM, signature, or supply-chain attestation.",
    },
    declared_lockfile_versions: declaredLockfileVersions,
    observed_runtime_versions: {
      node: process.version,
      v8: process.versions.v8,
      platform: process.platform,
      arch: process.arch,
      node_major_matches_declared_release_builder: observedNodeMajor === declaredNodeMajor,
      ...observedVersions,
    },
    declared_release_images: DECLARED_RELEASE_IMAGES,
    output_evidence: {
      manifest: DIST_MANIFEST_NAME,
      algorithm: "SHA-256",
      excluded_paths: [DIST_MANIFEST_NAME],
      limitation: "The manifest identifies emitted bytes; external signing and an SBOM remain separate release gates.",
    },
  };
  writeFileSync(
    resolve(distRoot, BUILD_PROVENANCE_NAME),
    `${JSON.stringify(provenance, null, 2)}\n`,
  );

  const excludedPaths = [DIST_MANIFEST_NAME];
  const entries = visitRegularFiles(distRoot, new Set(excludedPaths)).map((entry) => {
    const bytes = readFileSync(entry.path);
    return { path: entry.relativePath, size: entry.size, sha256: sha256(bytes) };
  });
  const manifest = {
    schema_version: "nmu.browser-dist-sha256.v1",
    algorithm: "SHA-256",
    root: "dist/",
    excluded_paths: excludedPaths,
    files: entries,
  };
  writeFileSync(resolve(distRoot, DIST_MANIFEST_NAME), `${JSON.stringify(manifest, null, 2)}\n`);
  return { provenance, manifest };
}
