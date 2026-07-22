/** Deterministic, secret-free fingerprint of the browser release inputs. */
import { createHash } from "node:crypto";
import { lstatSync, readFileSync, readdirSync } from "node:fs";
import { dirname, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const DEFAULT_WEB_ROOT = resolve(SCRIPT_DIR, "..");
const DEFAULT_CONTENT_ROOT = resolve(DEFAULT_WEB_ROOT, "../content");

const REQUIRED_WEB_FILES = Object.freeze([
  "index.html",
  "package.json",
  "package-lock.json",
  "tsconfig.json",
  "tsconfig.app.json",
  "tsconfig.node.json",
  "vite.config.ts",
  "scripts/build-integrity.d.mts",
  "scripts/build-integrity.mjs",
  "scripts/build-fingerprint.d.mts",
  "scripts/build-fingerprint.mjs",
]);

const FROZEN_CONTENT_FILES = Object.freeze([
  "item_bank_v1.json",
  "week1_script.json",
  "autopilot_protocol_v1.json",
]);

function bytewiseCompare(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}

function portableRelative(root, path) {
  const value = relative(root, path).split(sep).join("/");
  if (!value || value === ".." || value.startsWith("../")) {
    throw new Error(`build input escaped its declared root: ${value || "."}`);
  }
  return value;
}

function addFile(entries, root, relativePath, namespace) {
  const path = resolve(root, relativePath);
  const stat = lstatSync(path);
  if (stat.isSymbolicLink() || !stat.isFile()) {
    throw new Error(`build input must be a regular non-symlink file: ${relativePath}`);
  }
  entries.push({
    label: `${namespace}/${portableRelative(root, path)}`,
    path,
  });
}

function addDirectory(entries, root, relativeDirectory, namespace) {
  const directory = resolve(root, relativeDirectory);
  const directoryStat = lstatSync(directory);
  if (directoryStat.isSymbolicLink() || !directoryStat.isDirectory()) {
    throw new Error(`build input must be a regular non-symlink directory: ${relativeDirectory}`);
  }
  const visit = (current) => {
    for (const name of readdirSync(current).sort(bytewiseCompare)) {
      const path = resolve(current, name);
      const relativePath = portableRelative(root, path);
      const stat = lstatSync(path);
      if (stat.isSymbolicLink()) {
        throw new Error(`build input symlinks are forbidden: ${relativePath}`);
      }
      if (stat.isDirectory()) visit(path);
      else if (stat.isFile()) entries.push({ label: `${namespace}/${relativePath}`, path });
      else throw new Error(`unsupported build input type: ${relativePath}`);
    }
  };
  visit(directory);
}

export function collectBuildInputs({
  webRoot = DEFAULT_WEB_ROOT,
  contentRoot = DEFAULT_CONTENT_ROOT,
} = {}) {
  const entries = [];
  for (const path of REQUIRED_WEB_FILES) addFile(entries, webRoot, path, "web");
  addDirectory(entries, webRoot, "src", "web");
  // Vite publicDir is disabled: no mutable public/ tree participates.  These
  // three authoritative release contracts are copied into the Docker build
  // context and are deliberately bound to the browser release identity.
  for (const path of FROZEN_CONTENT_FILES) addFile(entries, contentRoot, path, "content");
  entries.sort((left, right) => bytewiseCompare(left.label, right.label));
  const labels = new Set();
  for (const entry of entries) {
    if (labels.has(entry.label)) throw new Error(`duplicate build input label: ${entry.label}`);
    labels.add(entry.label);
  }
  return entries;
}

export function hashBuildInputs(entries) {
  const hash = createHash("sha256");
  for (const { label, path } of [...entries].sort((left, right) => bytewiseCompare(left.label, right.label))) {
    if (!label || label.includes("\0") || label.startsWith("/") || label.includes("\\")) {
      throw new Error(`invalid portable build input label: ${label}`);
    }
    const bytes = readFileSync(path);
    hash.update(Buffer.from(`${label}\0${bytes.length}\0`, "utf8"));
    hash.update(bytes);
    hash.update(Buffer.from("\0", "utf8"));
  }
  return hash.digest("hex");
}

export function buildIdFromFingerprint(fingerprint) {
  if (!/^[0-9a-f]{64}$/.test(fingerprint)) {
    throw new Error("build fingerprint must be a lowercase SHA-256 hex digest");
  }
  // Preserve the historical all-digit wire format so already-open tabs from
  // the timestamp era recognise the first deterministic release as newer.
  return BigInt(`0x${fingerprint}`).toString(10);
}

export function computeBuildIdentity(options) {
  const fingerprint = hashBuildInputs(collectBuildInputs(options));
  return { fingerprint, buildId: buildIdFromFingerprint(fingerprint) };
}
