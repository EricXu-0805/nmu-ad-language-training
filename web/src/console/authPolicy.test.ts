import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  handleConfirmedAccountAuthenticationLoss,
  identityCanExportResearchData,
  identityCanOperateTraining,
  identityCanPhysicallyDeleteAudio,
  isUnauthenticatedResponse,
  parseAuthConfig,
  parseAuthIdentity,
  shouldMountConsoleWorkspace,
} from "./authPolicy.ts";

test("auth config and identity require complete typed payloads", () => {
  assert.deepEqual(parseAuthConfig({ auth_required: true, accounts_enabled: true, pin_enabled: false }), {
    auth_required: true,
    accounts_enabled: true,
    pin_enabled: false,
  });
  assert.deepEqual(parseAuthIdentity({ display_id: "R1", role: "researcher", username: "reader" }), {
    display_id: "R1",
    role: "researcher",
    username: "reader",
  });
  assert.throws(() => parseAuthConfig({ accounts_enabled: false }));
  assert.throws(() => parseAuthIdentity({ display_id: "R1" }));
});

test("data governance and irreversible audio deletion remain separate roles", () => {
  const researcher = { display_id: "R1", role: "researcher", username: "r" };
  const steward = { display_id: "D1", role: "data_steward", username: "d" };
  const admin = { display_id: "A1", role: "admin", username: "a" };
  assert.equal(identityCanOperateTraining(researcher), true);
  assert.equal(identityCanOperateTraining(steward), false);
  assert.equal(identityCanExportResearchData(steward), true);
  assert.equal(identityCanPhysicallyDeleteAudio(steward), false);
  assert.equal(identityCanExportResearchData(admin), true);
  assert.equal(identityCanPhysicallyDeleteAudio(admin), true);
});

test("only an explicit 401 means login; network and parse failures stay errors", () => {
  assert.equal(isUnauthenticatedResponse({ status: 401 }), true);
  assert.equal(isUnauthenticatedResponse({ status: 0 }), false);
  assert.equal(isUnauthenticatedResponse(new SyntaxError("bad JSON")), false);
});

test("confirmed account loss clears sensitive state before login or refresh", () => {
  const order: string[] = [];
  const cleared = handleConfirmedAccountAuthenticationLoss(
    () => { order.push("transition"); },
    () => { order.push("clear"); return 7; },
  );
  assert.equal(cleared, 7);
  assert.deepEqual(order, ["clear", "transition"]);

  const source = readFileSync(new URL("./useConsoleAuth.ts", import.meta.url), "utf8");
  const initial401 = source.slice(
    source.indexOf("if (isUnauthenticatedResponse(error))"),
    source.indexOf("} else {", source.indexOf("if (isUnauthenticatedResponse(error))")),
  );
  const reauthEvent = source.slice(
    source.indexOf("const onReauth"),
    source.indexOf("window.addEventListener", source.indexOf("const onReauth")),
  );
  assert.match(initial401, /handleConfirmedAccountAuthenticationLoss/);
  assert.match(reauthEvent, /handleConfirmedAccountAuthenticationLoss/);
});

test("the research workspace mounts only after an explicit successful auth check", () => {
  assert.equal(shouldMountConsoleWorkspace("ok"), true);
  assert.equal(shouldMountConsoleWorkspace("loading"), false);
  assert.equal(shouldMountConsoleWorkspace("login"), false);
  assert.equal(shouldMountConsoleWorkspace("error"), false);
});
