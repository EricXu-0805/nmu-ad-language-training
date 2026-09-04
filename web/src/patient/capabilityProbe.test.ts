import assert from "node:assert/strict";
import test from "node:test";
import type { SyncMsg } from "../sync/messages.ts";
import { probeOtherTabs } from "./bindingAttachPolicy.ts";

function fakeBus() {
  const handlers = new Set<(msg: SyncMsg) => void>();
  const posted: SyncMsg[] = [];
  return {
    posted,
    handlers,
    post: (msg: SyncMsg) => { posted.push(msg); },
    subscribe: (handler: (msg: SyncMsg) => void) => {
      handlers.add(handler);
      return () => { handlers.delete(handler); };
    },
    reply: (msg: SyncMsg) => { for (const h of [...handlers]) h(msg); },
  };
}

const NONCE = "0123456789abcdef0123456789abcdef";

test("别的页签用同一 nonce 应答 → 拿到它连着的场次,并已退订", async () => {
  const b = fakeBus();
  const pending = probeOtherTabs(b.post, b.subscribe, 1000, NONCE);
  assert.deepEqual(b.posted, [{ type: "capabilityProbe", nonce: NONCE }]);
  b.reply({ type: "capabilityHeld", nonce: NONCE, sessionId: "S-HELD" });
  assert.equal(await pending, "S-HELD");
  assert.equal(b.handlers.size, 0, "应答后必须退订,不留监听");
});

test("超时无人应答 → null(本机没有别的页签连着,照常去 attach)", async () => {
  const b = fakeBus();
  const started = Date.now();
  assert.equal(await probeOtherTabs(b.post, b.subscribe, 30, NONCE), null);
  assert.ok(Date.now() - started >= 25);
  assert.equal(b.handlers.size, 0);
});

test("别的 nonce 的应答/别的类型的消息一律不算数", async () => {
  const b = fakeBus();
  const pending = probeOtherTabs(b.post, b.subscribe, 40, NONCE);
  b.reply({ type: "capabilityHeld", nonce: "ffffffffffffffffffffffffffffffff", sessionId: "S-X" });
  b.reply({ type: "safetyStop", sessionId: "S-X" });
  assert.equal(await pending, null);
});

test("第一个有效应答之后的应答被忽略(只结一次)", async () => {
  const b = fakeBus();
  const pending = probeOtherTabs(b.post, b.subscribe, 1000, NONCE);
  b.reply({ type: "capabilityHeld", nonce: NONCE, sessionId: "S-1" });
  b.reply({ type: "capabilityHeld", nonce: NONCE, sessionId: "S-2" });
  assert.equal(await pending, "S-1");
});
