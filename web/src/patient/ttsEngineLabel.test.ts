import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { ttsEngineLabel } from "./ttsEngineLabel.ts";

test("云端 Qwen 音频不会被误标成本地神经语音", () => {
  assert.equal(
    ttsEngineLabel("dashscope/qwen3-tts-flash/Serena"),
    "小语·云端 qwen3-tts-flash · Serena",
  );
});

test("Piper 音频明确标记为本地来源", () => {
  assert.equal(
    ttsEngineLabel("piper/zh_CN-huayan-medium"),
    "小语·本地 Piper · zh_CN-huayan-medium",
  );
});

test("null 与空标签不伪造服务端音源", () => {
  assert.equal(ttsEngineLabel("null-0"), null);
  assert.equal(ttsEngineLabel(""), null);
});

test("未来未知引擎仍显示服务器签发的完整标签", () => {
  assert.equal(ttsEngineLabel("hospital-tts/model-a"), "小语·服务端语音 · hospital-tts/model-a");
});

test("切受试者、切运行平面和安全暂停都会在 paint 前撤销旧语音上下文", () => {
  const shell = readFileSync(new URL("./PatientShell.tsx", import.meta.url), "utf8");
  const rapport = readFileSync(new URL("./RapportStage.tsx", import.meta.url), "utf8");
  assert.match(
    shell,
    /useLayoutEffect\(\(\) => \{\s*setTtsContext\(ttsContextKey\);\s*\}, \[ttsContextKey\]\)/,
  );
  assert.match(shell, /return \(\) => \{\s*clearTtsContext\(\);/);
  assert.match(shell, /message\.type !== "safetyStop"[\s\S]*?clearTtsContext\(\);/);
  assert.match(shell, /plane:legacy\|item:\$\{cursor\.itemIdx\}\|turn:\$\{cursor\.turnIdx\}/);
  assert.match(shell, /\["paused", "complete", "loading", "waiting", "thanks"\]\.includes\(currentScreen\)/);
  assert.match(rapport, /useLayoutEffect\(\(\) => \{/);
  assert.match(rapport, /if \(!\(connectionReady[\s\S]*?ttsContextKey\)\) \{[\s\S]*?stopSpeaking\(\);/);
});
