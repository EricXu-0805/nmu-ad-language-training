import assert from "node:assert/strict";
import test from "node:test";
import {
  classifyAttachOutcome,
  shouldAttemptAttach,
} from "./bindingAttachPolicy.ts";

test("200 即接上;能力入库后交回既有 live 流程", () => {
  assert.equal(classifyAttachOutcome(200, null), "attached");
});

test("绑定真死才放弃:验签失败、已撤回、部署未启用", () => {
  assert.equal(classifyAttachOutcome(401, "device_binding_invalid"), "drop_binding");
  assert.equal(classifyAttachOutcome(401, "device_binding_revoked"), "drop_binding");
  assert.equal(classifyAttachOutcome(503, "patient_binding_unavailable"), "drop_binding");
});

test("没有本人场次/别人的场次/限速/网络抖动一律安静重试,绝不打扰问候页", () => {
  assert.equal(classifyAttachOutcome(409, "device_attach_no_session"), "quiet_retry");
  assert.equal(classifyAttachOutcome(429, "auth_locked"), "quiet_retry");
  assert.equal(classifyAttachOutcome(0, null), "quiet_retry");
  assert.equal(classifyAttachOutcome(500, null), "quiet_retry");
  // 401 但不是绑定死亡代码(如未知代码):保守当作瞬时问题,不销毁长期绑定。
  assert.equal(classifyAttachOutcome(401, "something_else"), "quiet_retry");
  assert.equal(classifyAttachOutcome(503, null), "quiet_retry");
});

test("只有『有绑定且无能力』的设备才轮询 attach", () => {
  assert.equal(shouldAttemptAttach(true, false), true);
  assert.equal(shouldAttemptAttach(true, true), false);
  assert.equal(shouldAttemptAttach(false, false), false);
  assert.equal(shouldAttemptAttach(false, true), false);
});
