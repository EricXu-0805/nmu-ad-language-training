import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// P0-6 回归:「暂不配对」→「点一下,开始」之后不许死屏。
// 问候页必须有常驻找回入口重新弹配对框,且不依赖整页刷新。
const shell = readFileSync(new URL("./PatientShell.tsx", import.meta.url), "utf8");
const pinPrompt = readFileSync(
  new URL("../components/PinPrompt.tsx", import.meta.url), "utf8");

test("未配对问候页有常驻「连接这台平板」入口,点击走手动配对事件", () => {
  assert.match(shell, /\{!devicePaired && \(/);
  assert.match(shell, /className="patient-pair-entry"/);
  assert.match(shell, /new Event\(PIN_PROMPT_MANUAL_OPEN_EVENT\)/);
  assert.match(shell, /连接这台平板/);
  // 入口与老人主线区分:说明这是给工作人员的。
  assert.match(shell, /请工作人员点这里完成配对/);
});

test("配对状态来自绑定或设备能力任一,并跟随 capabilityEpoch 重新计算", () => {
  assert.match(shell,
    /const devicePaired = patientBindingActive \|\| getDeviceCapability\(\) !== null;/);
  // capabilityEpoch 变化触发重渲染,使 getDeviceCapability() 的读取保持新鲜。
  assert.match(shell, /void capabilityEpoch;/);
});

test("PinPrompt 的手动打开路径绕过 getPatientBinding 守卫(人明确要求配对)", () => {
  assert.match(pinPrompt, /export const PIN_PROMPT_MANUAL_OPEN_EVENT/);
  
  assert.match(pinPrompt,
    /window\.addEventListener\(PIN_PROMPT_MANUAL_OPEN_EVENT, openNow\)/);
  // 自动事件仍有绑定守卫;手动事件直接 openNow,不经过 getPatientBinding。
  const showBody = pinPrompt.slice(
    pinPrompt.indexOf("const show = () => {"),
    pinPrompt.indexOf("window.addEventListener(DEVICE_PAIR_REQUIRED_EVENT"));
  assert.match(showBody, /getPatientBinding\(\)/);
  const openNowBody = pinPrompt.slice(
    pinPrompt.indexOf("const openNow = () => {"),
    pinPrompt.indexOf("const show = () => {"));
  assert.doesNotMatch(openNowBody, /getPatientBinding/);
});
