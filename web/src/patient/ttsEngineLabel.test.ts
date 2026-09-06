import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { observeRapportSpeech } from "./rapportSpeechCycle.ts";
import { ttsEngineLabel, ttsToggleTitle } from "./ttsEngineLabel.ts";

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

test("legacy 平面的语音开关 tooltip 与旧行为逐字相同", () => {
  assert.equal(
    ttsToggleTitle(true, "legacy", "Ting-Ting"), "语音开 · 音色:Ting-Ting");
  assert.equal(
    ttsToggleTitle(false, "legacy", "Ting-Ting"), "语音关 · 音色:Ting-Ting");
  assert.equal(ttsToggleTitle(true, "legacy", null), "语音开 · 本机无中文语音");
  assert.equal(ttsToggleTitle(false, "legacy", null), "语音关 · 本机无中文语音");
});

test("server/probing/blocked 平面不再声称任何本机音色", () => {
  for (const mode of ["server", "probing", "blocked"]) {
    for (const voice of ["Ting-Ting", null]) {
      assert.equal(ttsToggleTitle(true, mode, voice), "语音开");
      assert.equal(ttsToggleTitle(false, mode, voice), "语音关");
    }
    // 服务端朗读由 Qwen 出声；写本机音色名或"本机无中文语音"都是错的。
    assert.doesNotMatch(ttsToggleTitle(true, mode, "Ting-Ting"), /音色:/);
    assert.doesNotMatch(ttsToggleTitle(true, mode, null), /本机无中文语音/);
  }
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
  // 断线/暂停/这一拍本就不该出声时,必须在 paint 前收声。
  const silence = rapport.match(/const mustSilence = [\s\S]*?;\n/);
  assert.ok(silence, "RapportStage 缺少 paint 前的收声判据");
  assert.match(silence[0], /!connectionReady/);
  assert.match(silence[0], /isPaused/);
  assert.match(silence[0], /ttsContextKey/);
  assert.match(rapport, /if \(mustSilence \|\|[\s\S]*?\) \{\s*stopSpeaking\(\);/);
  // 内容身份独立于写回序号；播放身份还包含显式重播意图。
  const identity = rapport.match(/const contentIdentity =[\s\S]*?;\n/);
  assert.ok(identity, "RapportStage 缺少内容身份");
  assert.doesNotMatch(identity[0], /wseq/);
  assert.match(rapport, /speechCycle\.current = observeRapportSpeech/);
});

test("开麦重签与保存不重读；同题显式重播会产生新的播放身份", () => {
  const first = observeRapportSpeech(null, {
    content: "认识机器人#0#ask", recSeq: 1, recording: "idle", wseq: 10,
  });
  const armed = observeRapportSpeech(first, {
    content: "认识机器人#0#ask", recSeq: 2, recording: "armed", wseq: 11,
  });
  assert.equal(armed.key, first.key, "开麦写不得截断或重读刚才的问句");
  const saved = observeRapportSpeech(armed, {
    content: "认识机器人#0#ask", recSeq: 2, recording: "idle", wseq: 12,
  });
  assert.equal(saved.key, first.key, "保存写不得把原问句插在老人回答后");
  const replay = observeRapportSpeech(saved, {
    content: "认识机器人#0#ask", recSeq: 3, recording: "idle", wseq: 13,
  });
  assert.notEqual(replay.key, first.key, "重新播放本问必须产生新一轮播放与回执");
});
