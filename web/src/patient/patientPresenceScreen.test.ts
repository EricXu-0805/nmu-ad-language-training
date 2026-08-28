import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { autopilotPresenceScreen } from "./patientPresenceScreen.ts";

const base = { runtimePhase: null, mode: "server" as const, blockedCalm: false };

test("运行中的三种画面照实翻译", () => {
  assert.equal(autopilotPresenceScreen({ ...base, runtimePhase: "recording" }), "record");
  assert.equal(autopilotPresenceScreen({ ...base, runtimePhase: "record_ready" }), "record");
  assert.equal(autopilotPresenceScreen({ ...base, runtimePhase: "tts_playing" }), "present");
  assert.equal(autopilotPresenceScreen({ ...base, runtimePhase: "tts_ready" }), "present");
  assert.equal(autopilotPresenceScreen({ ...base, runtimePhase: "paused" }), "paused");
  assert.equal(autopilotPresenceScreen(base), "waiting");
});

test("卡住的设备不许显示成「已显示暂停提示」", () => {
  // 告警档:屏上写的是「请找工作人员」,控制台必须说同一件事。
  assert.equal(autopilotPresenceScreen(
    { ...base, mode: "blocked", blockedCalm: false }), "error");
  // 平静档:服务器主动收走 runtime,那确实是暂停。
  assert.equal(autopilotPresenceScreen(
    { ...base, mode: "blocked", blockedCalm: true }), "paused");
  // runtime 明确是 paused 时以它为准,与 blocked 无关。
  assert.equal(autopilotPresenceScreen(
    { ...base, runtimePhase: "paused", mode: "blocked", blockedCalm: false }), "paused");
});

test("源码接线守卫:PatientShell 真的用了这个函数,而不是自己又折一次", () => {
  const source = readFileSync(new URL("./PatientShell.tsx", import.meta.url), "utf8");
  assert.match(source, /autopilotPresenceScreen\(/);
  // 旧的内联三元里那句 `autopilot.mode === "blocked" ? "paused"` 不许再出现。
  assert.equal(/mode === "blocked"\s*$/m.test(source)
    && /\?\s*"paused"/.test(source), false,
    "PatientShell 里还留着把 blocked 折成 paused 的内联分支");
});
