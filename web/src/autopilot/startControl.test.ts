import assert from "node:assert/strict";
import test from "node:test";
import type { Session } from "../types.ts";
import {
  autopilotServerOwnsConsole,
  AutopilotControlOperationEpoch,
  autopilotConsoleReducer,
  buildAutopilotStartRequest,
  buildAutopilotTakeoverRequest,
  completePlanAllowsAutopilotStart,
  initialAutopilotConsoleState,
  parseAutopilotStatusReceipt,
  p0aConsoleEligibility,
  receiptAllowsAutopilotTakeover,
} from "./startControl.ts";

test("server start stays disabled until the entire operational plan is ready", () => {
  assert.equal(completePlanAllowsAutopilotStart(null), false);
  assert.equal(completePlanAllowsAutopilotStart(false), false);
  assert.equal(completePlanAllowsAutopilotStart(true), true);
});

const SIMULATION_SESSION: Session = {
  session_id: "S-a1b2c3d4",
  patient_id: "SIM-001",
  is_simulation: true,
  data_classification: "simulation",
  week_no: 2,
  phase_type: "正式训练",
  event_line: "正式训练",
  item_bank_version_id: "bank-v1",
  runtime_status: "active",
};

const safeStatus = () => ({
  scope_key: "p0a_sim_first_single_v1",
  mode: "autonomous",
  status: "waiting_tts",
  state_revision: 1,
  server_owned: true,
  takeover_ready: false,
  current_command_kind: "tts",
  last_error_code: null,
});

test("P0a console start is only offered to the exact simulation slice", () => {
  assert.deepEqual(p0aConsoleEligibility(SIMULATION_SESSION), { allowed: true });
  assert.deepEqual(
    p0aConsoleEligibility({ ...SIMULATION_SESSION, is_simulation: false, data_classification: "research" }),
    { allowed: false, reason: "research_session" },
  );
  assert.deepEqual(
    p0aConsoleEligibility({ ...SIMULATION_SESSION, data_classification: undefined }),
    { allowed: false, reason: "classification_unverified" },
  );
  assert.deepEqual(
    p0aConsoleEligibility({ ...SIMULATION_SESSION, week_no: 3 }),
    { allowed: false, reason: "scope_unsupported" },
  );
  assert.deepEqual(
    p0aConsoleEligibility(SIMULATION_SESSION, false, false),
    { allowed: false, reason: "account_required" },
  );
  assert.deepEqual(
    p0aConsoleEligibility(SIMULATION_SESSION, true),
    { allowed: false, reason: "runtime_blocked" },
  );
});

test("start request is deterministic, revision-zero, and rejects unsafe session ids", () => {
  assert.deepEqual(buildAutopilotStartRequest("S-a1b2c3d4"), {
    idempotency_key: "p0a.start.S-a1b2c3d4",
    expected_revision: 0,
  });
  assert.throws(() => buildAutopilotStartRequest("受试者\n答案"), /不能安全/);
  assert.throws(() => buildAutopilotStartRequest(`S-${"a".repeat(100)}`), /不能安全/);
});

test("takeover request is deterministic and fenced to a proven server revision", () => {
  assert.deepEqual(buildAutopilotTakeoverRequest("S-a1b2c3d4", 7), {
    idempotency_key: "p0a.takeover.S-a1b2c3d4.7",
    expected_revision: 7,
  });
  assert.throws(() => buildAutopilotTakeoverRequest("S-a1b2c3d4", 0), /状态版本/);
  assert.throws(() => buildAutopilotTakeoverRequest("S-a1b2c3d4", 1.5), /状态版本/);
  assert.throws(() => buildAutopilotTakeoverRequest("受试者\n答案", 3), /不能安全/);
});

test("account takeover stays closed until the exact server drain proof is ready", () => {
  assert.equal(receiptAllowsAutopilotTakeover(null), false);
  const paused = parseAutopilotStatusReceipt({
    ...safeStatus(), status: "paused", current_command_kind: null,
  });
  assert.equal(receiptAllowsAutopilotTakeover(paused), false);
  const drained = parseAutopilotStatusReceipt({
    ...safeStatus(), status: "paused", current_command_kind: null,
    takeover_ready: true,
  });
  assert.equal(receiptAllowsAutopilotTakeover(drained), true);
  assert.equal(receiptAllowsAutopilotTakeover({
    ...drained, serverOwned: false,
  }), false);
});

test("status/start share an exact content-free receipt", () => {
  assert.deepEqual(parseAutopilotStatusReceipt(safeStatus()), {
    scopeKey: "p0a_sim_first_single_v1",
    mode: "autonomous",
    status: "waiting_tts",
    stateRevision: 1,
    serverOwned: true,
    takeoverReady: false,
    commandKind: "tts",
    lastErrorCode: null,
  });
  assert.deepEqual(parseAutopilotStatusReceipt({
    scope_key: "disabled", mode: "disabled", status: "idle",
    state_revision: 0, server_owned: false, takeover_ready: false,
    current_command_kind: null,
    last_error_code: null,
  }).serverOwned, false);
  assert.deepEqual(parseAutopilotStatusReceipt({
    scope_key: "p0a_sim_first_single_v1", mode: "manual", status: "paused",
    state_revision: 3, server_owned: false, takeover_ready: false,
    current_command_kind: null,
    last_error_code: null,
  }), {
    scopeKey: "p0a_sim_first_single_v1",
    mode: "manual",
    status: "paused",
    stateRevision: 3,
    serverOwned: false,
    takeoverReady: false,
    commandKind: null,
    lastErrorCode: null,
  });
  assert.throws(() => parseAutopilotStatusReceipt({ ...safeStatus(), token: "secret" }));
  assert.throws(() => parseAutopilotStatusReceipt({ ...safeStatus(), current_command_kind: "record" }), /命令不一致/);
  assert.throws(() => parseAutopilotStatusReceipt({ ...safeStatus(), server_owned: false }), /所有权/);
  assert.throws(() => parseAutopilotStatusReceipt({
    ...safeStatus(), takeover_ready: true,
  }), /接管就绪/);
  assert.throws(() => parseAutopilotStatusReceipt({
    ...safeStatus(), state_revision: 0,
  }), /版本/);
  assert.throws(() => parseAutopilotStatusReceipt({
    ...safeStatus(),
    status: "idle",
    current_command_kind: null,
  }), /空闲/);
  assert.throws(() => parseAutopilotStatusReceipt({
    ...safeStatus(),
    mode: "manual",
    server_owned: false,
  }), /人工接管/);
  assert.throws(() => parseAutopilotStatusReceipt({
    ...safeStatus(),
    last_error_code: "1_invalid_backend_code",
  }));
  assert.throws(() => parseAutopilotStatusReceipt({ ...safeStatus(), last_error_code: "patient said carrot" }));
  assert.throws(() => parseAutopilotStatusReceipt({
    scope_key: "disabled", mode: "disabled", status: "idle",
    state_revision: 2, server_owned: false, takeover_ready: false,
    current_command_kind: null,
    last_error_code: null,
  }), /禁用状态/);
  const ready = parseAutopilotStatusReceipt({
    scope_key: "p0a_sim_first_single_v1",
    mode: "autonomous",
    status: "paused",
    state_revision: 3,
    server_owned: true,
    takeover_ready: true,
    current_command_kind: null,
    last_error_code: null,
  });
  assert.equal(ready.takeoverReady, true);
});

test("processing and draining keep the record command kind, and stay fail-closed", () => {
  const processing = {
    ...safeStatus(), status: "processing_attempt", current_command_kind: "record",
  };
  const draining = {
    ...safeStatus(), status: "manual_draining", current_command_kind: "record",
  };
  for (const receipt of [processing, draining]) {
    const parsed = parseAutopilotStatusReceipt(receipt);
    assert.equal(parsed.status, receipt.status);
    assert.equal(parsed.commandKind, "record");
    // 收麦/处理中仍由服务器持有，不是已释放的人工控制。
    assert.equal(parsed.serverOwned, true);
    assert.throws(
      () => parseAutopilotStatusReceipt({ ...receipt, current_command_kind: "tts" }),
      /命令不一致/,
    );
    assert.throws(
      () => parseAutopilotStatusReceipt({ ...receipt, current_command_kind: null }),
      /命令不一致/,
    );
    // 这两个状态属于 autonomous；manual 声称它们与安全释放契约冲突。
    assert.throws(
      () => parseAutopilotStatusReceipt({
        ...receipt, mode: "manual", server_owned: false,
      }),
      /人工接管/,
    );
  }
  // 最小收据里没有 command state，前端不得伪造这一层校验。
  assert.throws(() => parseAutopilotStatusReceipt({
    ...processing, command_state: "succeeded",
  }));
});

test("console starts fail-closed, ignores stale status, and only unlocks on authority", () => {
  const initial = initialAutopilotConsoleState(SIMULATION_SESSION.session_id);
  assert.equal(initial.phase, "checking");
  assert.equal(autopilotServerOwnsConsole(initial), true);
  const idleReceipt = parseAutopilotStatusReceipt({
    scope_key: "disabled", mode: "disabled", status: "idle",
    state_revision: 0, server_owned: false, takeover_ready: false,
    current_command_kind: null,
    last_error_code: null,
  });
  const idle = autopilotConsoleReducer(initial, {
    type: "status_received", sessionId: SIMULATION_SESSION.session_id, receipt: idleReceipt,
  });
  assert.equal(idle.phase, "idle");
  assert.equal(autopilotServerOwnsConsole(idle), false);
  const requested = autopilotConsoleReducer(idle, {
    type: "start_requested", sessionId: SIMULATION_SESSION.session_id,
  });
  assert.equal(requested.phase, "starting");
  const activeReceipt = parseAutopilotStatusReceipt(safeStatus());
  const started = autopilotConsoleReducer(requested, {
    type: "status_received", sessionId: SIMULATION_SESSION.session_id, receipt: activeReceipt,
  });
  assert.equal(started.phase, "waiting_tts");
  assert.equal(autopilotServerOwnsConsole(started), true);
  const stale = autopilotConsoleReducer(started, {
    type: "status_received", sessionId: SIMULATION_SESSION.session_id, receipt: idleReceipt,
  });
  assert.strictEqual(stale, started);
  const contradictorySameRevision = autopilotConsoleReducer(started, {
    type: "status_received",
    sessionId: SIMULATION_SESSION.session_id,
    receipt: parseAutopilotStatusReceipt({
      scope_key: "p0a_sim_first_single_v1",
      mode: "manual",
      status: "paused",
      state_revision: 1,
      server_owned: false,
      takeover_ready: false,
      current_command_kind: null,
      last_error_code: null,
    }),
  });
  assert.equal(contradictorySameRevision.phase, "uncertain");
  assert.equal(autopilotServerOwnsConsole(contradictorySameRevision), true);
  const uncertain = autopilotConsoleReducer(requested, {
    type: "status_uncertain", sessionId: SIMULATION_SESSION.session_id, error: "响应超时",
  });
  assert.equal(uncertain.phase, "uncertain");
  assert.equal(autopilotServerOwnsConsole(uncertain), true);
  const rejected = autopilotConsoleReducer(requested, {
    type: "start_rejected", sessionId: SIMULATION_SESSION.session_id, error: "服务器门禁关闭",
  });
  assert.equal(rejected.phase, "rejected");
  assert.equal(autopilotServerOwnsConsole(rejected), false);
});

test("a control write invalidates every status read captured before it", () => {
  const epoch = new AutopilotControlOperationEpoch();
  const oldRead = epoch.captureRead();
  assert.equal(epoch.accepts(oldRead), true);
  epoch.beginWrite();
  assert.equal(epoch.accepts(oldRead), false);
  const newRead = epoch.captureRead();
  assert.equal(epoch.accepts(newRead), true);
  epoch.invalidate();
  assert.equal(epoch.accepts(newRead), false);
});
