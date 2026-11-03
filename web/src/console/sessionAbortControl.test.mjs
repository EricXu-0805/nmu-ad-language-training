import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { randomUUID } from "node:crypto";
import test from "node:test";
import ts from "typescript";
import { ApiError } from "../apiResponse.ts";
import * as sessionAbort from "../sessionAbort.ts";

// Run the real control with deterministic I/O; the parent's runtime revision
// is a prop, re-rendered here the way the runtime poll re-renders it.
function harness(abortSession) {
  const slots = []; let cursor = 0, tree;
  const hooks = {
    useState(value) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = value;
      return [slots[index], update => { slots[index] = typeof update === "function" ? update(slots[index]) : update; }];
    },
  };
  const jsx = (type, props) => ({ type, props });
  const requests = [], aborted = [];
  const dependencies = {
    react: hooks,
    "react/jsx-runtime": { jsx, jsxs: jsx, Fragment: "fragment" },
    "../api": { api: { async abortSession(sid, operation) { requests.push(operation); return abortSession(requests.length, operation); } }, ApiError },
    "../components/Alert": { Alert: "Alert" },
    "../components/Button": { Button: "Button" },
    "../components/ConfirmDialog": { ConfirmDialog: "ConfirmDialog" },
    "../sessionAbort": sessionAbort,
  };
  const exports = {};
  const source = readFileSync(new URL("./SessionAbortControl.tsx", import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  vm.runInNewContext(compiled, { exports, Error, crypto: { randomUUID },
    require(name) { assert(name in dependencies, name); return dependencies[name]; } });
  const props = { sessionId: "S-ABORT", expectedRevision: 7, onAborted(runtime) { aborted.push(runtime); } };
  function render() { cursor = 0; tree = exports.SessionAbortControl(props); return tree; }
  function walk(node) {
    if (!node || typeof node !== "object") return [];
    if (Array.isArray(node)) return node.flatMap(walk);
    return [node, ...walk(node.props?.children), ...walk(node.props?.body)];
  }
  const nodes = () => walk(tree);
  const find = (type, label) => {
    const node = nodes().find(node => node.type === type && (!label || node.props.children === label));
    assert(node, `missing ${type}: ${label ?? ""}`); return node.props;
  };
  const text = () => JSON.stringify(tree);
  async function confirm() {
    find("ConfirmDialog").onConfirm();
    for (let i = 0; i < 3; i++) { await new Promise(resolve => setImmediate(resolve)); render(); }
  }
  render();
  find("Button", "中止本场").onClick(); render();
  find("select").onChange({ target: { value: "technical_failure" } }); render();
  return { props, requests, aborted, find, render, text, confirm };
}

const ABORTED = { sessionId: "S-ABORT", revision: 9, status: "aborted" };

test("a definitive revision conflict drops the frozen intent so the next confirm uses the new revision", async () => {
  const h = harness(async (attempt) => {
    if (attempt === 1) {
      throw new ApiError(409, "场次运行修订已变化，请重新核对后中止", {
        code: "session_abort_revision_conflict", message: "场次运行修订已变化，请重新核对后中止", current_revision: 8,
      });
    }
    return ABORTED;
  });
  await h.confirm();
  assert.equal(h.requests[0].expected_revision, 7);
  assert.match(h.text(), /场次状态刚刚变化（例如刚点了继续），请再点一次确认中止/);
  assert.doesNotMatch(h.text(), /重试不会重复提交/);
  assert.equal(h.find("select").disabled, false, "the closed reason stays editable after the server refused it");
  assert.equal(h.find("select").value, "technical_failure");
  h.props.expectedRevision = 8; h.render();
  await h.confirm();
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[1].expected_revision, 8);
  assert.equal(h.requests[1].reason_code, "technical_failure");
  assert.notEqual(h.requests[1].idempotency_key, h.requests[0].idempotency_key);
  assert.deepEqual(h.aborted, [ABORTED]);
});

test("an ambiguous network failure resends the identical abort intent", async () => {
  const h = harness(async (attempt) => {
    if (attempt === 1) throw new TypeError("Failed to fetch");
    return ABORTED;
  });
  await h.confirm();
  assert.match(h.text(), /重试不会重复提交/);
  assert.equal(h.find("select").disabled, true);
  h.props.expectedRevision = 8; h.render();
  await h.confirm();
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[1], h.requests[0], "revision, reason and idempotency key must stay identical");
  assert.deepEqual(h.aborted, [ABORTED]);
});
