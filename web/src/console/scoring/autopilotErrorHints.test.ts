import assert from "node:assert/strict";
import test from "node:test";

import {
  ADJUDICATION_PREWRITE_CODES,
  AUTOPILOT_CONTENT_GAP_CODES,
  AUTOPILOT_ERROR_HINTS,
  RESUME_PREWRITE_CODES,
  autopilotErrorHint,
  autopilotPausedTone,
  autopilotResumeRefusalHint,
} from "./autopilotErrorHints.ts";

// 服务端能写进自动带练状态 last_error_code 的码。2026-10-02 在 a2da5e7 上对 app/ 逐处 grep:
//   grep -n "last_error_code = \|last_error_code=" app/autopilot_service.py app/autopilot_orchestration.py
// 每个写入点的码从哪来、用哪条 grep 取的,写在各组上面。服务端加了新码,这里要跟着加,
// 加完这条测试会逼着给它写人话提示。
const BACKEND_LAST_ERROR_CODES = {
  // autopilot_service.py:7894 设备失败回执的 error_code(闭集):
  //   grep -n -A 10 "^_ACK_ERROR_CODES = frozenset" app/autopilot_contract.py
  deviceAckFailure: [
    "audio_playback_failed", "tts_cancelled", "microphone_denied", "microphone_unavailable",
    "recording_start_failed", "recording_runtime_failed", "recording_upload_failed",
    "device_command_timeout", "device_runtime_failed",
  ],
  // autopilot_service.py:2747 外部停止的 reason_code:
  //   grep -n -A 4 "fence_autonomous_scope_for_external_stop(" app/main.py | grep -o 'reason_code=[^,]*'
  externalStop: [
    "subject_withdrawal", "cloud_processing_revoked", "session_aborted", "patient_requested_pause",
  ],
  // 同一写入点 reason_code=payload["failureCode"](人工平面 patientRec 麦克风失败):
  //   grep -n -A 6 "failureCode: Literal\[" app/main.py
  patientRecFailure: [
    "microphone_start_timeout", "microphone_permission_denied", "microphone_not_found",
    "microphone_start_failed", "recording_authorization_failed",
  ],
  // autopilot_service.py:2580 同场次重新配对。
  deviceRotation: ["autopilot_device_rotated"],
  // autopilot_service.py:4638 / :4676 研究者裁定之后。
  adjudication: ["autopilot_scope_completed", "autopilot_adjudication_saved"],
  // autopilot_service.py:5242 与 :6486(写成 "last_error_code": gap.code)下一题内容缺口:
  //   grep -n -A 5 "^GapCode = Literal\[" app/autopilot_positions.py
  positionGap: [
    "source_field_unavailable", "operational_rubric_unavailable",
    "operational_protocol_unavailable", "interaction_package_unavailable",
  ],
  // autopilot_service.py:7439 与 autopilot_orchestration.py:1361:
  //   grep -n "^REPEAT_LIMIT_REASON_CODE\|^LEGACY_REPEAT_RECOVERY_REASON_CODE" app/autopilot_service.py
  repeat: ["explicit_repeat_limit", "legacy_repeat_recovery_complete"],
  // autopilot_service.py:5365 收尾判定失败(status=failed):
  //   grep -n "^_AUTOPILOT_AUTOFINISH_ERROR" app/main.py
  autofinish: ["intervention_completion_evidence_incomplete"],
  // autopilot_orchestration.py:1522 stage_processing_failure(error_code) 的来源一,识别/判分失败:
  //   grep -n -A 3 "finish_failure(" app/main.py | grep -o 'error_code="[a-z_]*"' | sort -u
  attemptProcessing: [
    "asr_degraded", "asr_exception", "asr_invalid_result", "asr_recovery_state_invalid",
    "cloud_processing_not_authorized", "cloud_provider_boundary_unknown",
    "judgement_exception", "operational_rubric_unavailable",
  ],
  // 来源二,worker 失败收口 _fail_closed_p0a_attempt_worker 的字面码与它转发的
  // AutopilotOrchestrationError 字面码:
  //   grep -n -A 4 "_fail_closed_p0a_attempt_worker(" app/main.py | grep -o '"autopilot_[a-z_]*"' | sort -u
  //   grep -n -A 2 "AutopilotOrchestrationError($" app/main.py | grep -o '"autopilot_[a-z_]*"' | sort -u
  //   grep -n -A 2 "_fail(" app/autopilot_orchestration.py | grep -o '"autopilot_[a-z_]*"' | sort -u
  //   grep -n "^LEGACY_RECOVERY_INVALID" app/autopilot_orchestration.py
  // 第三条里的 autopilot_failure_capture_invalid / _event_conflict / _state_busy 不算:它们在
  // stage_processing_failure 自己里面抛,抛了就不写。
  workerInternal: [
    "autopilot_attempt_result_invalid", "autopilot_attempt_route_failed",
    "autopilot_attempt_technical_failure", "autopilot_worker_exception",
    "autopilot_attempt_commit_fence_lost", "autopilot_attempt_input_mismatch",
    "autopilot_attempt_target_divergence", "autopilot_repeat_admission_not_bound",
    "autopilot_attempt_boundary_invalid", "autopilot_attempt_capture_invalid",
    "autopilot_attempt_existing_conflict", "autopilot_attempt_not_pending",
    "autopilot_attempt_plan_invalid", "autopilot_attempt_plan_mismatch",
    "autopilot_attempt_sequence_invalid", "autopilot_legacy_recovery_invalid",
  ],
} as const;
// 没有逐个列出的一类:worker 还会把 autopilot_service 门禁里 _fail 的码原样转发
// (autopilot_orchestration.py 里 `raise AutopilotOrchestrationError(exc.code, exc.message)`
// 那几处)。那是随服务端增减的开集,走 autopilotErrorHint 的兜底文案(照样显示错误码),
// 这条测试不假装覆盖它。

// 「继续」写前 409 里的 AI 服务实测与云处理授权两类:
//   grep -n -A 5 "^_STATUS_TO_CONFLICT_CODE = {" app/provider_readiness.py
//   grep -n '"provider_readiness_unavailable"' app/provider_readiness.py   (查不到状态时的兜底码)
//   grep -n "autopilot_cloud_processing_required" app/autopilot_service.py   (P0a 门禁,resume 同样走)
const BACKEND_RESUME_READINESS_CODES = [
  "provider_readiness_missing", "provider_readiness_expired",
  "provider_readiness_config_mismatch", "provider_readiness_required_capability_failed",
  "provider_readiness_unavailable",
] as const;
const BACKEND_RESUME_CLOUD_CODE = "autopilot_cloud_processing_required";

test("服务端能写进 last_error_code 的每个码都有一句人话提示,不再露出「错误码：…」", () => {
  const missing = Object.values(BACKEND_LAST_ERROR_CODES).flat()
    .filter((code) => !Object.hasOwn(AUTOPILOT_ERROR_HINTS, code));
  assert.deepEqual(missing, []);
  for (const code of Object.values(BACKEND_LAST_ERROR_CODES).flat()) {
    assert.doesNotMatch(autopilotErrorHint(code), /^错误码：/, code);
  }
});

test("「继续」与裁定的写前 409 码都有提示;AI 服务实测与云处理授权两类算写前拒绝", () => {
  for (const code of [...BACKEND_RESUME_READINESS_CODES, BACKEND_RESUME_CLOUD_CODE]) {
    assert.ok(RESUME_PREWRITE_CODES.has(code), code);
  }
  for (const code of [...RESUME_PREWRITE_CODES, ...ADJUDICATION_PREWRITE_CODES]) {
    assert.ok(Object.hasOwn(AUTOPILOT_ERROR_HINTS, code), code);
  }
  for (const code of BACKEND_RESUME_READINESS_CODES) {
    assert.equal(autopilotErrorHint(code), "AI 服务实测已过期或未通过，请管理员先点『检查 AI 服务』。");
  }
  // 云处理授权缺的是受试者档案里的授权,不是 AI 服务实测——不能让人去点「检查 AI 服务」。
  assert.match(autopilotErrorHint(BACKEND_RESUME_CLOUD_CODE), /云处理授权/);
  assert.doesNotMatch(autopilotErrorHint(BACKEND_RESUME_CLOUD_CODE), /检查 AI 服务/);
});

test("最常见的老人主动暂停与场次中止:说清该做什么", () => {
  assert.equal(autopilotErrorHint("patient_requested_pause"),
    "老人按了「暂停练习」。先确认老人状态，准备好后点「继续 AI 自动带练」。");
  assert.equal(autopilotErrorHint("session_aborted"), "本场已中止，请进入场次收尾。");
});

test("老人主动暂停用中性色,不是红色告警;其余暂停维持告警色", () => {
  assert.equal(autopilotPausedTone("patient_requested_pause"), "info");
  for (const code of ["recording_start_failed", "session_aborted", "autopilot_device_rotated", null]) {
    assert.equal(autopilotPausedTone(code), "danger", String(code));
  }
});

test("四种下一题内容缺口都按内容缺口处理(不给继续/裁定)", () => {
  assert.deepEqual([...AUTOPILOT_CONTENT_GAP_CODES].sort(), [...BACKEND_LAST_ERROR_CODES.positionGap].sort());
});

test("没有逐个写提示的码照旧把错误码给出来,方便研究团队排查", () => {
  assert.equal(autopilotErrorHint("autopilot_state_invalid"), "错误码：autopilot_state_invalid");
});

test("人工态下点「切回 AI」被技术失败挡住:不再叫人去点已经不显示的「转为人工操作」", () => {
  const manual = autopilotResumeRefusalHint("autopilot_attempt_failed", true);
  assert.doesNotMatch(manual, /转为人工操作/);
  assert.match(manual, /人工.*做完/);
  assert.match(manual, /切回 AI 自动带练/);
  // 还在 AI 托管下暂停时,原提示不变(跳过本题与转人工两条出口都在里面)。
  assert.equal(autopilotResumeRefusalHint("autopilot_attempt_failed", false),
    autopilotErrorHint("autopilot_attempt_failed"));
  assert.equal(autopilotResumeRefusalHint("autopilot_attempt_processing", true),
    autopilotErrorHint("autopilot_attempt_processing"));
});

test("识别/判分技术失败:AI 托管下给两条真出口「跳过本题」「转为人工操作」;人工态切回被拒不提跳过", () => {
  // autopilot_attempt_technical_failure 是 worker 兜底码:attempt 同样已判技术失败,
  // 服务端照样拒「继续」,不能再按其他内部码那样叫人点「继续」重试。
  for (const code of ["asr_exception", "asr_invalid_result", "asr_degraded",
    "asr_recovery_state_invalid", "judgement_exception", "autopilot_attempt_failed",
    "autopilot_attempt_technical_failure"]) {
    assert.match(autopilotErrorHint(code), /可点「跳过本题」（原因选「识别反复出错」或「其他」）/, code);
    assert.match(autopilotErrorHint(code), /「转为人工操作」/, code);
    assert.doesNotMatch(autopilotErrorHint(code), /继续 AI 自动带练/, code);
  }
  assert.match(autopilotErrorHint("autopilot_attempt_technical_failure"),
    /错误码：autopilot_attempt_technical_failure/);
  // 人工态没有「跳过本题」按钮(裁定只在 AI 自己暂停时出现)。
  assert.doesNotMatch(autopilotResumeRefusalHint("autopilot_attempt_failed", true), /跳过本题/);
});

test("裁定被云处理授权挡下也是写前拒绝:提示常驻,且不叫人去点这一态可能不显示的「继续」", () => {
  // 技术失败那一题点「跳过本题」也过同一道门禁;不在集合里,控制台会显示「待核实」直到下一次轮询。
  assert.ok(ADJUDICATION_PREWRITE_CODES.has("autopilot_cloud_processing_required"));
  assert.match(autopilotErrorHint("autopilot_cloud_processing_required"), /云处理授权/);
  assert.doesNotMatch(autopilotErrorHint("autopilot_cloud_processing_required"), /继续 AI 自动带练/);
});
