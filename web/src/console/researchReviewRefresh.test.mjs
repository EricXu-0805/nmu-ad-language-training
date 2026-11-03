import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { randomUUID } from "node:crypto";
import test from "node:test";
import ts from "typescript";
import { ApiError } from "../apiResponse.ts";
import { turnKey } from "../lib/ids.ts";
import * as itemNumbering from "./itemNumbering.ts";
import * as reviewGate from "./researchReviewGate.ts";

const KEY = turnKey("S-01", 1);
const PLAN = {
  item_bank_version_id: "bank-synthetic-v1",
  items: [{
    item_id: "S-01", task_type: "单要素", presentation_order: 1, display: {},
    turns: [{ turn_seq: 1, response_role: "命名", scoring_key: "final_correct" }],
  }],
};
const ROW = {
  turnId: 41, responseRole: "命名", sourceAttemptId: 9, asrText: "雨伞", confirmed: true,
  confirmedText: "雨伞", confirmationRevision: 3, locked: false, elementValue: null, promptLevel: 0,
};

// Run the real panel, its row and audio children with deterministic I/O.
// onJournalStale swaps in the server journal before the next render, as the
// wrapup screen's journal reload does.
function harness({ confirmTurn, lockTurn, reloadedRow }) {
  const slots = [], setters = []; let cursor = 0, effects = [], tree, dirty = false;
  const hooks = {
    useRef(value) { return slots[cursor++] ??= { current: value }; },
    useState(value) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = typeof value === "function" ? value() : value;
      setters[index] ??= update => {
        const next = typeof update === "function" ? update(slots[index]) : update;
        if (!Object.is(next, slots[index])) { slots[index] = next; dirty = true; }
      };
      return [slots[index], setters[index]];
    },
    useEffect(effect, deps) {
      const index = cursor++, previous = slots[index];
      if (!previous || deps.some((value, i) => !Object.is(value, previous.deps[i]))) {
        const current = { deps, cleanup: previous?.cleanup }; slots[index] = current;
        effects.push(() => { current.cleanup?.(); current.cleanup = effect(); });
      }
    },
  };
  const jsx = (type, props) => ({ type, props });
  const calls = { confirm: [], lock: [], stale: 0, changed: 0, toasts: [] };
  const api = {
    async confirmTurn(turnId, body) { calls.confirm.push({ turnId, body }); return confirmTurn(calls.confirm.length, body); },
    async lockTurn(turnId, body) { calls.lock.push({ turnId, body }); return lockTurn(calls.lock.length, body); },
    async getAudioBlob() { return { synthetic: true }; },
  };
  const dependencies = {
    react: hooks,
    "react/jsx-runtime": { jsx, jsxs: jsx, Fragment: "fragment" },
    "../api": { api, ApiError },
    "../components/Button": { Button: "Button" },
    "../components/ConfirmDialog": { ConfirmDialog: "ConfirmDialog" },
    "../components/Field": { Field: "Field" },
    "../components/StatusPill": { StatusPill: "StatusPill" },
    "../components/ToastContext": { useToast: () => (message, tone) => calls.toasts.push({ message, tone }) },
    "../lib/ids": { turnKey },
    "./itemNumbering": itemNumbering,
    "./researchReviewGate": reviewGate,
    "./scoring/vm": { assertPortraitFree() {} },
  };
  const exports = {};
  const source = readFileSync(new URL("./ResearchReviewPanel.tsx", import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  vm.runInNewContext(compiled, {
    exports, Error, crypto: { randomUUID },
    URL: { createObjectURL: () => "blob:synthetic", revokeObjectURL() {} },
    require(name) { assert(name in dependencies, name); return dependencies[name]; },
  });
  const props = {
    plan: PLAN, turns: { [KEY]: ROW }, reviewerId: "REVIEWER-01", readOnly: false,
    rawAudioIdsByTurnKey: { [KEY]: "audio-synthetic-01" },
    onUpsertTurn(itemId, turnSeq, patch) {
      const key = turnKey(itemId, turnSeq);
      props.turns = { ...props.turns, [key]: { ...props.turns[key], ...patch } };
    },
    onChanged() { calls.changed++; },
    onJournalStale() { calls.stale++; props.turns = { [KEY]: reloadedRow }; },
  };
  function expand(node) {
    if (Array.isArray(node)) return node.map(expand);
    if (!node || typeof node !== "object") return node;
    if (typeof node.type === "function") return expand(node.type(node.props));
    if (node.props?.children === undefined) return node;
    return { ...node, props: { ...node.props, children: expand(node.props.children) } };
  }
  function render() {
    let count = 0;
    do {
      assert(++count < 20, "render effects must settle"); dirty = false; cursor = 0;
      tree = expand(exports.ResearchReviewPanel(props));
      const pending = effects; effects = []; pending.forEach(effect => effect());
    } while (dirty);
    return tree;
  }
  function walk(node) {
    if (!node || typeof node !== "object") return [];
    if (Array.isArray(node)) return node.flatMap(walk);
    return [node, ...walk(node.props?.children)];
  }
  const nodes = () => walk(tree);
  const find = (predicate, label) => {
    const node = nodes().find(predicate);
    assert(node, `missing ${label}`); return node.props;
  };
  const byLabel = label => find(node => node.props?.["aria-label"] === label, label);
  const byText = (type, text) => find(node => node.type === type && node.props.children === text, `${type}: ${text}`);
  async function settle() { for (let i = 0; i < 5; i++) { await new Promise(resolve => setImmediate(resolve)); render(); } }
  render();
  return { calls, props, nodes, find, byLabel, byText, render, settle };
}

function revisionConflict() {
  return new ApiError(409, "确认文本已被其他人更新", {
    code: "turn_confirmation_revision_conflict", message: "确认文本已被其他人更新",
  });
}

async function editAndSave(h, text) {
  h.find(node => node.type === "textarea", "textarea").onChange({ target: { value: text } }); h.render();
  h.byLabel("保存 S-01 · 命名 的人工确认文本").onClick(); await h.settle();
}

test("a confirmation revision conflict reloads the authoritative journal exactly once", async () => {
  const h = harness({
    confirmTurn: async () => { throw revisionConflict(); },
    reloadedRow: { ...ROW, confirmedText: "其他复核人已确认的文本", confirmationRevision: 4 },
  });
  await editAndSave(h, "本人修改的确认文本");
  assert.equal(h.calls.confirm.length, 1);
  assert.equal(h.calls.stale, 1, "the server refused this revision; the journal must be reloaded");
  assert.equal(h.find(node => node.type === "textarea", "textarea").value, "其他复核人已确认的文本");
  await editAndSave(h, "再次修改的确认文本");
  assert.equal(h.calls.confirm[1].body.expected_revision, 4);
  assert.notEqual(h.calls.confirm[1].body.idempotency_key, h.calls.confirm[0].body.idempotency_key);
});

test("a lock refused with 409 reloads the journal and shows the server's locked state", async () => {
  const h = harness({
    lockTurn: async () => { throw new ApiError(409, "该环节已锁分，不可重复锁定"); },
    reloadedRow: { ...ROW, locked: true, elementValue: 1 },
  });
  h.byText("Button", "加载并核对录音").onClick(); await h.settle();
  h.find(node => node.type === "audio", "audio").onLoadedMetadata(); h.render();
  h.byText("button", "命名正确（1）").onClick(); h.render();
  h.byLabel("锁定 S-01 · 命名 的研究评分").onClick(); h.render();
  h.find(node => node.type === "ConfirmDialog", "ConfirmDialog").onConfirm(); await h.settle();
  assert.equal(h.calls.lock.length, 1);
  assert.equal(h.calls.stale, 1);
  assert.deepEqual(h.calls.toasts.at(-1), { message: "该环节已锁分，不可重复锁定", tone: "danger" });
  h.byText("StatusPill", "研究评分已锁定");
  assert.equal(h.find(node => node.type === "ConfirmDialog", "ConfirmDialog").open, false);
  assert(!h.nodes().some(node => node.props?.["aria-label"] === "锁定 S-01 · 命名 的研究评分"));
});

test("an ambiguous network failure keeps the exact confirmation retry and does not reload", async () => {
  const h = harness({
    confirmTurn: async (attempt, body) => {
      if (attempt === 1) throw new TypeError("Failed to fetch");
      return { confirmed_response_text: body.confirmed_response_text, confirmation_revision: 4 };
    },
  });
  await editAndSave(h, "本人修改的确认文本");
  assert.equal(h.calls.stale, 0);
  assert.equal(h.calls.changed, 0);
  h.byLabel("保存 S-01 · 命名 的人工确认文本").onClick(); await h.settle();
  assert.equal(h.calls.confirm.length, 2);
  assert.deepEqual(h.calls.confirm[1].body, h.calls.confirm[0].body, "revision, text and idempotency key must stay identical");
  assert.equal(h.calls.stale, 0);
  assert.equal(h.props.turns[KEY].confirmationRevision, 4);
});

test("the wrapup screen wires a stale journal to a journal reload plus a scores reload", () => {
  const source = readFileSync(new URL("./SessionWrapupScreen.tsx", import.meta.url), "utf8");
  const panel = source.slice(source.indexOf("<ResearchReviewPanel"), source.indexOf("/>", source.indexOf("<ResearchReviewPanel")));
  assert.match(panel, /onJournalStale=\{\(\) => \{\s*setJournalRetry\(\(value\) => value \+ 1\);\s*setScoresRetryNonce\(\(value\) => value \+ 1\);\s*\}\}/);
});
