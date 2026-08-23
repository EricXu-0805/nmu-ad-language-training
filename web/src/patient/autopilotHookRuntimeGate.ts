import type { PatientAutopilotMode } from "./autopilotAdmission.ts";
import type { AutopilotRuntimePhase, AutopilotRuntimeState } from "./autopilotRuntime.ts";

/**
 * 一次 owner load 落到 paused/scope_completed 之后，本次挂载生命周期里的任何
 * 后续 effect 都不得把它当成"还没结束"重新处理。
 */
export function isAutopilotRuntimeTerminal(phase: AutopilotRuntimePhase): boolean {
  return phase === "paused" || phase === "scope_completed";
}

/**
 * "bootstrap" 闸：媒体已放行的 effect 是否可以再取一次 /next 并考虑起 runner。
 * owner load 给出的 seed 阶段一旦是终态，这里必须整段跳过——不重取、不建
 * controller，避免用一次没有 latch 上下文的重取把已经 reconcile 好的暂停事实
 * 冲成 waiting_command。
 */
export function shouldBootstrapAutopilotRunner(seedPhase: AutopilotRuntimePhase): boolean {
  return !isAutopilotRuntimeTerminal(seedPhase);
}

/** "should-poll" 闸：未激活媒体时的被动轮询是否可以再排一次定时器。 */
export function shouldSchedulePassiveAutopilotPoll(phase: AutopilotRuntimePhase): boolean {
  return !isAutopilotRuntimeTerminal(phase);
}

/**
 * "should-start-controller" 闸：bootstrap 重取之后，是否可以真的用这份 runtime
 * 起一个 PatientAutopilotController。
 *
 * PatientAutopilotController 支持传入完整的 initialRuntime 种子，但调用方若
 * 仍然只传 initialCommand，控制器会独立调用
 * restoreAutopilotRuntime(initialCommand, seq)——命令是 null 时它只会得到
 * waiting_command，完全不知道调用方这边其实已经是 paused。调用方必须在构造
 * 控制器之前先用这个闸判断一次，不能指望控制器自己识别终态。
 */
export function canStartAutopilotRunner(restored: AutopilotRuntimeState): boolean {
  return !isAutopilotRuntimeTerminal(restored.phase);
}

/**
 * D5①:终态(paused/scope_completed)与 blocked 平静档是本次挂载的轮询死角——
 * 接管+研究者 resume 之后没有任何信号能把患者端从「我们先休息一下」解出来。
 * 权威 live 通道的 session.paused 真→假下降沿是唯一可信的 resume 事实:
 * 此时恰好放行一次重探(走既有 probeEpoch 门,不建第二套执行器)。重探要么
 * 发现服务器仍持有(回到同一终态,无环),要么拿到 autopilot_not_active 走
 * 既有 stop-legacy 收口回到 live 游标平面。
 * 活跃 runner 自有轮询;blocked 告警档是设备侧判死,留给研究者处置,不自动醒。
 */
export function shouldReprobeAfterServerResume(input: {
  previousServerPaused: boolean;
  serverPaused: boolean;
  mode: PatientAutopilotMode;
  runtimePhase: AutopilotRuntimePhase | null;
  blockedCalm: boolean;
}): boolean {
  if (!input.previousServerPaused || input.serverPaused) return false;
  if (input.mode === "server") {
    return input.runtimePhase !== null && isAutopilotRuntimeTerminal(input.runtimePhase);
  }
  return input.mode === "blocked" && input.blockedCalm;
}
