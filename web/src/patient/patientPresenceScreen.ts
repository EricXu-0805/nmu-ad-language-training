import type { PatientPresenceScreen } from "../types";
import type { AutopilotRuntimePhase } from "./autopilotRuntime.ts";
import type { PatientAutopilotMode } from "./usePatientAutopilot.ts";

/** 老人端在场画面：控制台据此判断「那台平板现在在干什么」。
 *
 * blocked 原来一律折成 "paused"，于是控制台显示「在线 · 已显示暂停提示 · 刚刚有响应」，
 * 而实际是设备卡住了等人处理。心跳与运行器完全解耦（心跳每 5 秒照发），所以三个指示
 * 全绿、两句话互相矛盾却都不报错，研究者合理推断「老人在看提示，AI 在等他」——
 * 一分钟起步的静默期就是这么来的。
 *
 * blocked 分两档：平静档是服务器主动收走 runtime（收尾/暂停/中止），那确实是暂停；
 * 告警档是设备侧判死或连不上，屏上写的是「请找工作人员」，控制台必须跟着说
 * 「等待研究者处理」——这个标签 SCREEN_LABELS 里本来就有，只是从来没人发过。
 */
export function autopilotPresenceScreen(input: {
  runtimePhase: AutopilotRuntimePhase | null | undefined;
  mode: PatientAutopilotMode;
  blockedCalm: boolean;
}): PatientPresenceScreen {
  const phase = input.runtimePhase ?? null;
  if (phase === "recording" || phase === "record_ready") return "record";
  if (phase === "tts_playing" || phase === "tts_ready") return "present";
  if (phase === "paused") return "paused";
  if (input.mode === "blocked") return input.blockedCalm ? "paused" : "error";
  return "waiting";
}
