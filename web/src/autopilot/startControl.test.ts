import assert from "node:assert/strict";
import test from "node:test";
import { ApiError } from "../apiResponse.ts";
import type { Session } from "../types.ts";
import {
  autopilotServerOwnsConsole,
  AutopilotControlOperationEpoch,
  autopilotConsoleReducer,
  buildAutopilotStartRequest,
  buildAutopilotTakeoverRequest,
  completePlanAllowsAutopilotStart,
  initialAutopilotConsoleState,
  isPrewriteStartRejection,
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
  position_item_id: "SE_花",
  position_turn_seq: 1,
  last_error_code: null,
});

test("P0a console start is offered only to provably classified sessions", () => {
  assert.deepEqual(p0aConsoleEligibility(SIMULATION_SESSION), { allowed: true });
  // 严格真实研究对现在同样可启动——服务端仍 fail-closed 重验全部门禁。
  assert.deepEqual(
    p0aConsoleEligibility({ ...SIMULATION_SESSION, is_simulation: false, data_classification: "research" }),
    { allowed: true },
  );
  // 任何不可证明的分类组合都拒绝:缺失、legacy、错配。
  assert.deepEqual(
    p0aConsoleEligibility({ ...SIMULATION_SESSION, data_classification: undefined }),
    { allowed: false, reason: "classification_unverified" },
  );
  assert.deepEqual(
    p0aConsoleEligibility({ ...SIMULATION_SESSION, data_classification: "legacy_unknown" }),
    { allowed: false, reason: "classification_unverified" },
  );
  assert.deepEqual(
    p0aConsoleEligibility({ ...SIMULATION_SESSION, is_simulation: false }),
    { allowed: false, reason: "classification_unverified" },
  );
  assert.deepEqual(
    p0aConsoleEligibility({ ...SIMULATION_SESSION, data_classification: "research" }),
    { allowed: false, reason: "classification_unverified" },
  );
  // 周次闸与引擎一致放宽到 2..8;1 与 9 仍拒。
  for (const weekNo of [3, 8]) {
    assert.deepEqual(
      p0aConsoleEligibility({ ...SIMULATION_SESSION, week_no: weekNo }),
      { allowed: true },
    );
  }
  for (const weekNo of [1, 9]) {
    assert.deepEqual(
      p0aConsoleEligibility({ ...SIMULATION_SESSION, week_no: weekNo }),
      { allowed: false, reason: "scope_unsupported" },
    );
  }
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
    positionItemId: "SE_花",
    positionTurnSeq: 1,
    lastErrorCode: null,
  });
  // 位置是只读展示投影:半个位置、非法序号、disabled 带位置都拒绝。
  assert.throws(() => parseAutopilotStatusReceipt({
    ...safeStatus(), position_turn_seq: null,
  }), /位置/);
  assert.throws(() => parseAutopilotStatusReceipt({
    ...safeStatus(), position_turn_seq: 0,
  }), /位置/);
  assert.throws(() => parseAutopilotStatusReceipt({
    ...safeStatus(), position_item_id: "", position_turn_seq: null,
  }), /位置/);
  const positionless = parseAutopilotStatusReceipt({
    ...safeStatus(), position_item_id: null, position_turn_seq: null,
  });
  assert.equal(positionless.positionItemId, null);
  assert.equal(positionless.positionTurnSeq, null);
  assert.deepEqual(parseAutopilotStatusReceipt({
    scope_key: "disabled", mode: "disabled", status: "idle",
    state_revision: 0, server_owned: false, takeover_ready: false,
    current_command_kind: null,
    position_item_id: null,
    position_turn_seq: null,
    last_error_code: null,
  }).serverOwned, false);
  assert.deepEqual(parseAutopilotStatusReceipt({
    scope_key: "p0a_sim_first_single_v1", mode: "manual", status: "paused",
    state_revision: 3, server_owned: false, takeover_ready: false,
    current_command_kind: null,
    position_item_id: "SE_花",
    position_turn_seq: 1,
    last_error_code: null,
  }), {
    scopeKey: "p0a_sim_first_single_v1",
    mode: "manual",
    status: "paused",
    stateRevision: 3,
    serverOwned: false,
    takeoverReady: false,
    commandKind: null,
    positionItemId: "SE_花",
    positionTurnSeq: 1,
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
    position_item_id: null,
    position_turn_seq: null,
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
    position_item_id: "SE_花",
    position_turn_seq: 1,
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
    position_item_id: null,
    position_turn_seq: null,
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
      position_item_id: null,
      position_turn_seq: null,
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

test("D1:启动拒因常驻——权威 no-owner 轮询降级为持久提示,再次点启动或服务器持有才清除", () => {
  const idleReceipt = parseAutopilotStatusReceipt({
    scope_key: "disabled", mode: "disabled", status: "idle",
    state_revision: 0, server_owned: false, takeover_ready: false,
    current_command_kind: null,
    position_item_id: null,
    position_turn_seq: null,
    last_error_code: null,
  });
  const base = autopilotConsoleReducer(
    initialAutopilotConsoleState(SIMULATION_SESSION.session_id),
    { type: "status_received", sessionId: SIMULATION_SESSION.session_id, receipt: idleReceipt },
  );
  const requested = autopilotConsoleReducer(base, {
    type: "start_requested", sessionId: SIMULATION_SESSION.session_id,
  });
  const rejected = autopilotConsoleReducer(requested, {
    type: "start_rejected", sessionId: SIMULATION_SESSION.session_id, error: "服务器门禁关闭",
  });
  assert.equal(rejected.phase, "rejected");
  assert.equal(rejected.error, "服务器门禁关闭");
  assert.equal(rejected.lastStartRejection, "服务器门禁关闭");
  // 2.5s 轮询的权威 no-owner 回执不许无痕抹掉拒因:强横幅可降级,持久提示必须在。
  const polled = autopilotConsoleReducer(rejected, {
    type: "status_received", sessionId: SIMULATION_SESSION.session_id, receipt: idleReceipt,
  });
  assert.equal(polled.phase, "idle");
  assert.equal(polled.lastStartRejection, "服务器门禁关闭");
  // 连续轮询也不清。
  const polledAgain = autopilotConsoleReducer(polled, {
    type: "status_received", sessionId: SIMULATION_SESSION.session_id, receipt: idleReceipt,
  });
  assert.equal(polledAgain.lastStartRejection, "服务器门禁关闭");
  // 用户再次点启动 → 清除。
  const retried = autopilotConsoleReducer(polledAgain, {
    type: "start_requested", sessionId: SIMULATION_SESSION.session_id,
  });
  assert.equal(retried.phase, "starting");
  assert.equal(retried.lastStartRejection, null);
  // 服务器真的持有(拒因条件消失)→ 清除。
  const rejectedTwice = autopilotConsoleReducer(autopilotConsoleReducer(retried, {
    type: "start_rejected", sessionId: SIMULATION_SESSION.session_id, error: "again",
  }), {
    type: "status_received",
    sessionId: SIMULATION_SESSION.session_id,
    receipt: parseAutopilotStatusReceipt(safeStatus()),
  });
  assert.equal(rejectedTwice.phase, "waiting_tts");
  assert.equal(rejectedTwice.lastStartRejection, null);
  // reset(换场)回到初始:无残留拒因。
  const reset = autopilotConsoleReducer(rejected, {
    type: "reset", sessionId: "S-other111",
  });
  assert.equal(reset.lastStartRejection, null);
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

test("D1:部署门禁 409 是确定的写前拒绝——按拒因处理,不再折成会被轮询抹掉的 uncertain", () => {
  const gate = (code: string) => new ApiError(
    409, "gate", { code, message: "gate" }, "nested-detail");
  for (const code of [
    "autopilot_p0a_disabled",
    "autopilot_real_sessions_disabled",
    "autopilot_cloud_processing_required",
    "autopilot_recording_not_allowed",
    "autopilot_consent_denied",
    "autopilot_subject_withdrawn",
    "autopilot_scope_unsupported",
    "autopilot_classification_invalid",
    "autopilot_plan_not_fully_supported",
    "autopilot_runtime_inactive",
  ]) {
    assert.equal(isPrewriteStartRejection(gate(code)), true, code);
  }
  // 幂等/并发/所有权类 409 可能已有写入,必须继续 fail-closed 等权威核实。
  assert.equal(isPrewriteStartRejection(gate("autopilot_idempotency_conflict")), false);
  assert.equal(isPrewriteStartRejection(gate("autopilot_revision_conflict")), false);
  assert.equal(isPrewriteStartRejection(gate("autopilot_takeover_cas_conflict")), false);
  // 非 409/无码/网络失败都不是可证明的写前拒绝。
  assert.equal(isPrewriteStartRejection(new ApiError(500, "boom")), false);
  assert.equal(isPrewriteStartRejection(new ApiError(409, "no-code", "x", "direct")), false);
  assert.equal(isPrewriteStartRejection(new TypeError("net")), false);
  // 带 context 附加字段的门禁错误(plan_not_fully_supported 会带统计)同样认得出。
  assert.equal(isPrewriteStartRejection(new ApiError(
    409, "gate",
    { code: "autopilot_plan_not_fully_supported", message: "m", unsupported_position_count: 3 },
    "nested-detail")), true);
});
