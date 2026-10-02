// 暂停原因用人话说一遍:研究者对着一串错误码不知道该去平板上看什么。
// 2026-09-13 演示:平板里留着上一场没传完的录音,自动带练一开麦就 recording_start_failed,
// 屏上只剩「已安全暂停」,钱凯只能说「进行不了训练」。
//
// 服务端能写进 last_error_code 的码在 autopilotErrorHints.test.ts 里按 app/ 的 grep 列成
// 清单逐个钉住。服务端加了新码,先按那几条 grep 把它补进清单,测试再逼着这里补提示。

const RECORDING_START_FAILED_HINT = "老人端麦克风没能启动。常见原因：平板里还留着上一场没传完的录音（系统会在下一次开麦前自动清掉），或浏览器没给麦克风权限。点「继续 AI 自动带练」再试一次；还不行就让老人端页面刷新一下。";
const MICROPHONE_DENIED_HINT = "老人端浏览器拒绝了麦克风权限，请在平板上允许后再点「继续 AI 自动带练」。";
const MICROPHONE_UNAVAILABLE_HINT = "老人端找不到麦克风设备，请检查平板后再继续。";
const CONTENT_GAP_HINT = "下一题缺少自动训练内容，AI 已停下，不会跳题。请点「转为人工操作」继续。";
const ASR_FAILURE_HINT = "AI 没能识别刚才那段录音（语音识别出错），已停下。这一题 AI 不能接着做：可点「跳过本题」（原因选「识别反复出错」或「其他」），或点「转为人工操作」人工完成。";
const PROVIDER_READINESS_HINT = "AI 服务实测已过期或未通过，请管理员先点『检查 AI 服务』。";

/**
 * 处理录音时的内部校验失败:具体是哪一道闸只有研究团队看得懂,提示里把码一起给出。
 * 录音此时已经保存,「继续」会用已保存录音再处理一次;处理状态已判失败的,服务端会
 * 用 autopilot_attempt_failed 拒绝,那条提示让人跳过本题或转人工。
 */
const WORKER_INTERNAL_CODES = [
  "autopilot_worker_exception",
  "autopilot_attempt_result_invalid",
  "autopilot_attempt_route_failed",
  "autopilot_attempt_commit_fence_lost",
  "autopilot_attempt_input_mismatch",
  "autopilot_attempt_target_divergence",
  "autopilot_repeat_admission_not_bound",
  "autopilot_attempt_boundary_invalid",
  "autopilot_attempt_capture_invalid",
  "autopilot_attempt_existing_conflict",
  "autopilot_attempt_not_pending",
  "autopilot_attempt_plan_invalid",
  "autopilot_attempt_plan_mismatch",
  "autopilot_attempt_sequence_invalid",
  "autopilot_legacy_recovery_invalid",
] as const;

const PROVIDER_READINESS_CODES = [
  "provider_readiness_missing",
  "provider_readiness_expired",
  "provider_readiness_config_mismatch",
  "provider_readiness_required_capability_failed",
  "provider_readiness_unavailable",
] as const;

export const AUTOPILOT_ERROR_HINTS: Readonly<Record<string, string>> = Object.freeze({
  recording_start_failed: RECORDING_START_FAILED_HINT,
  microphone_denied: MICROPHONE_DENIED_HINT,
  microphone_unavailable: MICROPHONE_UNAVAILABLE_HINT,
  recording_upload_failed: "老人端录音上传或采集收据没有完成，检查网络后再点「继续 AI 自动带练」。",
  recording_runtime_failed: "老人端录音过程中出错（浏览器录音器异常），让老人端页面刷新一下再点「继续 AI 自动带练」。",
  device_command_timeout:
    "老人端没有在限定时间内回应。多半是老人端还在等点屏（屏上显示「点一下，接着听」），让老人点一下屏幕再点「继续 AI 自动带练」；如果屏幕黑了或页面关了，重新打开老人端页面。网络慢时也可能是老人端还在上传上一场没传完的录音，稍等再点「继续 AI 自动带练」。",
  audio_playback_failed: "老人端放不出这句引导语（拿不到音频、解码失败或浏览器拒绝播放）。检查平板音量和网络，让老人点一下屏幕，再点「继续 AI 自动带练」。",
  tts_cancelled: "引导语播放被打断（换页、切场或老人端被暂停）。确认老人端还在这一场，再点「继续 AI 自动带练」。",
  device_runtime_failed: "老人端自动流程内部出错，让老人端页面刷新一下再点「继续 AI 自动带练」。",
  // 老人端点「暂停练习」(生产暂停里最常见的一种)与场次中止,都是外部停止写进来的。
  patient_requested_pause: "老人按了「暂停练习」。先确认老人状态，准备好后点「继续 AI 自动带练」。",
  session_aborted: "本场已中止，请进入场次收尾。",
  subject_withdrawal: "受试者已退出研究，本场不能再继续 AI 自动带练，请按退出流程处理本场。",
  cloud_processing_revoked: "受试者撤回了云处理授权，AI 不能再把录音送去云端识别和判分，已停下。请在受试者档案里核对授权；本场要继续请点「转为人工操作」。",
  // 人工平面麦克风失败(patientRec)经同一条外部停止写进来,与设备回执的几种同义。
  microphone_start_timeout: "老人端麦克风迟迟没有启动（常见于权限弹窗没点，或麦克风被别的程序占着）。处理后让老人端页面刷新一下，再点「继续 AI 自动带练」。",
  microphone_permission_denied: MICROPHONE_DENIED_HINT,
  microphone_not_found: MICROPHONE_UNAVAILABLE_HINT,
  microphone_start_failed: RECORDING_START_FAILED_HINT,
  recording_authorization_failed: "老人端开麦前没拿到服务器的录音授权（多半是场次状态刚变过或网络断了一下）。确认老人端还在这一场，再点「继续 AI 自动带练」。",
  source_field_unavailable: CONTENT_GAP_HINT,
  operational_rubric_unavailable: CONTENT_GAP_HINT,
  operational_protocol_unavailable: CONTENT_GAP_HINT,
  interaction_package_unavailable: CONTENT_GAP_HINT,
  asr_exception: ASR_FAILURE_HINT,
  asr_invalid_result: ASR_FAILURE_HINT,
  asr_degraded: ASR_FAILURE_HINT,
  asr_recovery_state_invalid: ASR_FAILURE_HINT,
  judgement_exception: "AI 判分出错，已停下。这一题 AI 不能接着做：可点「跳过本题」（原因选「识别反复出错」或「其他」），或点「转为人工操作」人工完成。",
  cloud_provider_boundary_unknown: "AI 识别/判分服务的数据去向无法确认（服务器配置问题），为保护受试者数据 AI 已停下。这一题请点「转为人工操作」人工完成，并请管理员检查 AI 服务配置。",
  cloud_processing_not_authorized: "这位受试者没有有效的云处理授权，AI 不能把录音送去云端识别，已停下。这一题请点「转为人工操作」人工完成；要继续 AI 带练，请先在受试者档案完成云处理授权。",
  ...Object.fromEntries(WORKER_INTERNAL_CODES.map((code) => [code,
    `AI 处理刚才那段录音时内部出错，已停下（录音已保存）。可以点「继续 AI 自动带练」再试；仍不行请点「转为人工操作」，并把这段提示截图发给研究团队（错误码：${code}）。`])),
  // worker 兜底码:attempt 已判技术失败(只是没带错误码),「继续」会被 autopilot_attempt_failed 拒。
  autopilot_attempt_technical_failure: "AI 处理刚才那段录音时出错，已停下（录音已保存）。这一题 AI 不能接着做：可点「跳过本题」（原因选「识别反复出错」或「其他」），或点「转为人工操作」人工完成；并把这段提示截图发给研究团队（错误码：autopilot_attempt_technical_failure）。",
  intervention_completion_evidence_incomplete: "这一题的证据不完整（录音或判定没有落账），AI 不能替它宣布完成。点「转为人工操作」把这一题人工做完。",
  explicit_repeat_limit: "老人已多次要求重听，达到协议上限，AI 停下等你决定。点「继续 AI 自动带练」AI 会接着这一题（已经答过的，按最后一次回答给下一级提示或反馈）；也可以点「转为人工操作」人工完成。",
  legacy_repeat_recovery_complete: "本场开始于「再说一遍」规则上线之前，AI 已处理完遗留的那段录音并就此停下，本场不能再继续带练；请直接进入场次收尾。",
  autopilot_device_rotated: "老人端换了设备或重新配对，AI 已停下。确认新平板已配对到这一场后，点「继续 AI 自动带练」由 AI 接着当前题目；也可以「老人已答对」「跳过本题」，或「转为人工操作」。",
  // 研究者裁定(老人已答对/跳过本题)的写前 409:服务端在任何写入之前拒绝,
  // 提示常驻在卡片里,不折成会被下一次轮询抹掉的 uncertain。
  autopilot_adjudication_requires_pause: "先暂停，等平板收麦后再点。",
  autopilot_adjudication_saved: "现场决定已保存。AI 服务暂不可用，训练保持暂停；恢复后可继续 AI，或转为人工操作。",
  autopilot_adjudication_already_recorded: "这个环节的现场决定已经保存，不能重复裁定。请继续 AI 自动带练，或转为人工操作。",
  autopilot_adjudication_attempt_required: "这一题还没有录到老人的回答，AI 判不了「答对」；可以点「跳过本题」，或点「继续 AI 自动带练」让 AI 重问这一题。",
  autopilot_attempt_processing: "上一段录音还在判分，几秒后再点。",
  autopilot_attempt_failed: "上一段录音判分失败，AI 不能接着弹这一题，也判不了「答对」；可点「跳过本题」（原因选「识别反复出错」或「其他」），或点「转为人工操作」把这一题人工完成。",
  autopilot_attempt_abandoned: "上一段录音的判分被暂停打断；可点「继续 AI 自动带练」，用已保存录音接着处理，或转为人工操作。",
  autopilot_resume_position_unresumable: "人工接管期间答过的这一题，AI 不能接着弹；请人工完成这一题后再切回。",
  autopilot_scope_completed: "本场可自动带练的题目已全部练完或裁定完毕；请直接进入场次收尾。",
  autopilot_revision_conflict: "服务器状态已更新，请核对当前题目与环节后重新操作。",
  autopilot_activation_requires_pause: "请先暂停本场，等待老人端收麦后再继续。",
  autopilot_activation_processing: "本场上一段回答尚未完成安全暂停，请等待后台处理停止后再继续。",
  autopilot_activation_bedside_busy: "另一场训练还未安全停止。请先暂停当前床旁场次，等待收麦和回答处理结束，再回到这里继续。",
  autopilot_takeover_drain_required: "还没有收到老人端停止播放、关闭麦克风的回执，请保持平板在线，收麦完成后再继续。",
  autopilot_patient_microphone_active: "当前床旁麦克风仍可能在录音，请先完成收麦再继续。",
  autopilot_device_not_paired: "本场已切回并保持暂停。请在老人端重新连接这场训练，确认配对后再点「继续 AI 自动带练」。",
  autopilot_device_not_active: "老人端的连接已失效，请重新连接这场训练，确认配对后再继续。",
  ...Object.fromEntries(PROVIDER_READINESS_CODES.map((code) => [code, PROVIDER_READINESS_HINT])),
  autopilot_cloud_processing_required: "AI 判分要用云端转写，这位受试者还没有有效的云处理授权。请先在受试者档案完成云处理授权再回来操作；也可以点「转为人工操作」。",
});

export function autopilotErrorHint(code: string): string {
  return Object.hasOwn(AUTOPILOT_ERROR_HINTS, code) ? AUTOPILOT_ERROR_HINTS[code] : `错误码：${code}`;
}

// 「继续 / 切回 AI」被拒时的提示。技术失败那一题在 AI 托管下只能跳过或转人工；已经在人工态时
// 「转为人工操作」按钮不再显示，同一个码的意思变成「这一题还没人工做完」。
export function autopilotResumeRefusalHint(code: string, manual: boolean): string {
  if (manual && code === "autopilot_attempt_failed") {
    return "这一题上一段录音判分失败，还没有人工做完；先在下方人工面板把这一题做完，再点「切回 AI 自动带练」。";
  }
  return autopilotErrorHint(code);
}

// 裁定写前拒绝码:服务端明确未写入,可安全回退到刚重取的权威回执并常驻提示。
export const ADJUDICATION_PREWRITE_CODES: ReadonlySet<string> = new Set([
  "autopilot_adjudication_requires_pause",
  "autopilot_adjudication_attempt_required",
  "autopilot_adjudication_already_recorded",
  "autopilot_attempt_processing",
  "autopilot_attempt_failed",
  "autopilot_attempt_abandoned",
  "autopilot_scope_completed",
  "autopilot_revision_conflict",
  // 云处理授权(P0a 门禁)同样在写入前拒绝;技术失败那一题点「跳过本题」也会撞上它。
  "autopilot_cloud_processing_required",
]);
// 「继续」的写前拒绝码(收据 260 起题内续弹新增):同样在任何写入之前拒绝,提示要常驻,
// 不能折成 2.5 s 后被轮询抹掉的 uncertain。AI 服务实测(provider_readiness.require_resume_ready)
// 与云处理授权(P0a 门禁)也是写前拒绝:场次保持暂停,等管理员或授权补齐。
export const RESUME_PREWRITE_CODES: ReadonlySet<string> = new Set([
  "autopilot_attempt_processing",
  "autopilot_attempt_failed",
  "autopilot_attempt_abandoned",
  "autopilot_resume_position_unresumable",
  "autopilot_scope_completed",
  "autopilot_revision_conflict",
  "autopilot_activation_requires_pause",
  "autopilot_activation_processing",
  "autopilot_activation_bedside_busy",
  "autopilot_takeover_drain_required",
  "autopilot_patient_microphone_active",
  "autopilot_device_not_paired",
  "autopilot_device_not_active",
  ...PROVIDER_READINESS_CODES,
  "autopilot_cloud_processing_required",
]);

/** 下一题缺内容:AI 不会跳题,卡片收起继续与裁定,只留转人工。 */
export const AUTOPILOT_CONTENT_GAP_CODES: ReadonlySet<string> = new Set([
  "source_field_unavailable",
  "operational_rubric_unavailable",
  "operational_protocol_unavailable",
  "interaction_package_unavailable",
]);

/**
 * 暂停卡片的色调。老人自己按了「暂停练习」(生产上最常见的暂停原因)不是故障,
 * 用中性色;其余暂停仍是告警色,让研究者先看原因。
 */
export function autopilotPausedTone(lastErrorCode: string | null): "info" | "danger" {
  return lastErrorCode === "patient_requested_pause" ? "info" : "danger";
}
