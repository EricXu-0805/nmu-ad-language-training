import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { randomUUID } from "node:crypto";
import test from "node:test";
import ts from "typescript";
import * as contract from "./sessionCloseout.ts";

// Run the real component, handlers and revision effects with deterministic I/O.
// onReconcile updates parent props before resolving, as the real wrapper does.
function harness(reconciled) {
  const slots = []; let cursor = 0, effects = [], tree, gate, dirty = false;
  const hooks = {
    useId() { return `field-${cursor++}`; },
    useRef(value) { return slots[cursor++] ??= { current: value }; },
    useState(value) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = typeof value === "function" ? value() : value;
      return [slots[index], update => {
        const next = typeof update === "function" ? update(slots[index]) : update;
        if (!Object.is(next, slots[index])) { slots[index] = next; dirty = true; }
      }];
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
  const exports = {};
  const dependencies = {
    react: hooks,
    "react/jsx-runtime": { jsx, jsxs: jsx, Fragment: "fragment" },
    "./sessionCloseout": contract,
    "../components/Alert": { Alert: "Alert" },
    "../components/Button": { Button: "Button" },
    "../components/StatusPill": { StatusPill: "StatusPill" },
  };
  const source = readFileSync(new URL("./SessionCloseoutPanel.tsx", import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  vm.runInNewContext(compiled, { exports, crypto: { randomUUID }, Error,
    require(name) { assert(name in dependencies, name); return dependencies[name]; } });
  const original = { ...contract.EMPTY_SESSION_CLOSEOUT_FLAGS, session_id: "S-CLOSEOUT",
    schema_version: "session-closeout.v1", revision: 2, report_status: "observation_recorded",
    note: "原来已保存的备注", locked: false };
  const requests = [];
  const props = { outcomeSummary: {}, closeout: original,
    onGateChange(value) { gate = value; },
    async onSave(request) {
      requests.push(request);
      if (requests.length === 1) throw new Error("保存响应丢失");
      props.closeout = { ...original, ...request, revision: request.expected_revision + 1 };
      render(); return props.closeout;
    },
    async onReconcile() { props.closeout = reconciled(original); render(); return props.closeout; },
  };
  function render() {
    let count = 0;
    do {
      assert(++count < 20, "render effects must settle"); dirty = false; cursor = 0;
      tree = exports.SessionCloseoutPanel(props);
      const pending = effects; effects = []; pending.forEach(effect => effect());
    } while (dirty);
    return tree;
  }
  function walk(node) {
    if (!node || typeof node !== "object") return [];
    if (Array.isArray(node)) return node.flatMap(walk);
    return [node, ...walk(node.props?.children), ...walk(node.props?.actions)];
  }
  const nodes = () => walk(tree);
  function find(type, label) {
    const node = nodes().find(node => node.type === type && (!label || node.props.children === label));
    assert(node, `missing ${type}: ${label ?? ""}`); return node.props;
  }
  async function settle() { for (let i = 0; i < 5; i++) { await new Promise(resolve => setImmediate(resolve)); render(); } }
  render();
  return { requests, props, find, render, settle, gate: () => gate, nodes,
    async saveDraft() {
      find("textarea").onChange({ target: { value: "本次尚未保存的新备注" } }); render();
      find("form").onSubmit({ preventDefault() {} }); await settle();
    },
  };
}

test("lost edit with unchanged server revision offers exact retry instead of claiming recovery", async () => {
  const h = harness(record => record); await h.saveDraft();
  assert.equal(h.find("textarea").value, "本次尚未保存的新备注");
  assert.equal(h.gate().reconciliation_required, true);
  assert.equal(h.gate().dirty, true);
  assert(!h.nodes().some(node => node.props?.tone === "ok"));
  h.find("Button", "用原内容安全重试").onClick(); await h.settle();
  assert.equal(h.requests.length, 2);
  assert.deepEqual(h.requests[1], h.requests[0], "revision, payload and idempotency key must stay identical");
  assert.equal(h.gate().dirty, false);
  assert.equal(h.gate().reconciliation_required, false);
});

for (const keep of [false, true]) test(`concurrent edit preserves the draft until explicit ${keep ? "keep" : "adopt"} choice`, async () => {
  const h = harness(record => ({ ...record, revision: 3, note: "其他工作人员已保存的备注" }));
  await h.saveDraft();
  assert.equal(h.find("textarea").value, "本次尚未保存的新备注");
  assert.equal(h.gate().reconciliation_required, true);
  assert.equal(h.gate().dirty, true);
  h.find("Button", keep ? "保留草稿继续编辑" : "采用服务器记录").onClick(); h.render();
  assert.equal(h.gate().reconciliation_required, false);
  assert.equal(h.gate().dirty, keep);
  if (keep) {
    h.find("form").onSubmit({ preventDefault() {} }); await h.settle();
    assert.equal(h.requests[1].expected_revision, 3);
    assert.notEqual(h.requests[1].idempotency_key, h.requests[0].idempotency_key);
    assert.equal(h.requests[1].note, "本次尚未保存的新备注");
  } else assert.equal(h.find("textarea").value, "其他工作人员已保存的备注");
});

test("matching newer server content resolves the uncertain write without a failure alert", async () => {
  const h = harness(record => ({ ...record, revision: 3, note: "本次尚未保存的新备注" }));
  await h.saveDraft();
  assert.equal(h.gate().dirty, false);
  assert.equal(h.gate().reconciliation_required, false);
  assert(!h.nodes().some(node => node.type === "Alert" && node.props.tone === "danger"));
});
