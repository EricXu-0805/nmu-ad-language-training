import assert from "node:assert/strict";
import test from "node:test";
import { createBargeInDetector } from "./autopilotBargeIn.ts";
import { startBargeInMonitor, type BargeInPorts } from "./autopilotBrowserBargeIn.ts";
import type { NextCommandProjection } from "./autopilotProtocol.ts";

function driver() {
  const detector = createBargeInDetector();
  let now = 0;
  const decisions: string[] = [];
  return { decisions, feed(ms: number, rms: number, ratio = 0.9) {
    for (let elapsed = 0; elapsed < ms; elapsed += 64) {
      now += 64;
      const decision = detector.push(rms, ratio, now);
      if (decision !== "none") decisions.push(decision);
    }
  }, jump(ms: number) { now += ms; } };
}

test("silence, low frequency hum and brief impacts never interrupt", () => {
  const d = driver();
  d.feed(1024, 0.002);
  d.feed(4000, 0.002);
  d.feed(4000, 0.15, 0.1);
  d.feed(128, 0.2);
  d.feed(1000, 0.002);
  assert.deepEqual(d.decisions, []);
});
test("TTS echo candidate disappears after pausing and resumes without confirming", () => {
  const d = driver();
  d.feed(1024, 0.002);
  d.feed(256, 0.06);
  d.feed(1200, 0.002);
  assert.deepEqual(d.decisions, ["candidate", "resume"]);
});
test("persistent voice after echo-settling confirms exactly once", () => {
  const d = driver();
  d.feed(1024, 0.002);
  d.feed(3000, 0.06);
  assert.deepEqual(d.decisions, ["candidate", "confirmed"]);
});
test("delayed frames cannot count as continuous onset", () => {
  const d = driver();
  d.feed(1024, 0.002);
  for (let i = 0; i < 12; i++) { d.jump(1000); d.feed(64, 0.2); }
  assert.deepEqual(d.decisions, []);
});

const command = { schema_version: 1, command_key: "cmd-monitor-test-0001", command_seq: 1,
  kind: "tts", state: "started", command_revision: 1, control_generation: 1, runner_generation: 1,
  item_ref: "itm-0001", turn_seq: 1, attempt_seq: 1, prompt_level: 0,
  payload: { speech_key: "test.question", speech_text: "请回答", purpose: "question" },
} as Extract<NextCommandProjection, {kind: "tts"}>;
async function flush() { for (let i = 0; i < 20; i++) await Promise.resolve(); }
function setup(options: { permission?: boolean; aec?: boolean; media?: Promise<MediaStream>; authFails?: number } = {}) {
  let callback: (() => void) | null = null;
  let clock = 0;
  let level = 0.002;
  let foreground = true;
  let stopped = 0;
  let released = 0;
  let auth = 0;
  let mediaCalls = 0;
  let confirms = 0;
  let resumeCalls = 0;
  let closedContext = 0;
  const phases: string[] = [];
  const stream = { getTracks: () => [{stop: () => { stopped++; }}],
    getAudioTracks: () => [{readyState: "live", getSettings: () => ({echoCancellation: options.aec ?? true})}],
  } as unknown as MediaStream;
  const audio = { ended: false, paused: false, pause() { this.paused = true; },
    play() { this.paused = false; resumeCalls++; return Promise.resolve(); } };
  const analyser = { fftSize: 2048, frequencyBinCount: 1024, smoothingTimeConstant: 0,
    getFloatTimeDomainData(values: Float32Array) { values.fill(level); },
    getFloatFrequencyData(values: Float32Array) { values.fill(-100); values[30] = -5; } };
  const ports: BargeInPorts = {
    permissionGranted: async () => options.permission ?? true,
    foreground: () => foreground,
    authorize: async () => { auth++; if (auth === options.authFails) throw new Error("revoked"); },
    acquireLease: async () => ({release() { released++; }, released: Promise.resolve()}),
    getUserMedia: async () => { mediaCalls++; return options.media ?? stream; },
    createContext: () => ({ state: "running", sampleRate: 48000,
      createMediaStreamSource: () => ({connect() {}, disconnect() {}}),
      createAnalyser: () => analyser,
      close: async () => { closedContext++; },
    }) as unknown as AudioContext,
    now: () => clock, setInterval: (cb) => { callback = cb; return 1; },
    clearInterval: () => { callback = null; },
  };
  const handle = startBargeInMonitor({sessionId: "test-session", command, audio: audio as HTMLAudioElement,
    onConfirmed: () => { assert.equal(stopped, 1); confirms++; return true; },
    onPlaybackFailed: () => { throw new Error("unexpected playback error"); },
    observe: (o) => phases.push(o.phase),
  }, ports);
  return {handle, stream, phases, audio, facts: () => ({stopped,released,auth,mediaCalls,confirms,resumeCalls,closedContext}),
    hide() { foreground = false; },
    feed(ms: number, value: number) { level = value; for (let i = 0; i < ms; i += 64) {clock += 64; callback?.();} },
  };
}
test("monitor authorizes twice, physically stops before interrupt, then releases lease", async () => {
  const h = setup(); await flush();
  assert.equal(h.facts().auth, 2);
  h.feed(1024, .002); h.feed(2000, .08); await h.handle.closed;
  assert.deepEqual(h.facts(), {stopped:1,released:1,auth:2,mediaCalls:1,confirms:1,resumeCalls:0,closedContext:1});
  assert.equal(h.phases.at(-1), "interrupted");
});
test("ungranted permission never prompts or acquires microphone", async () => {
  const h = setup({permission:false}); await h.handle.closed;
  assert.equal(h.facts().mediaCalls, 0);
  assert.equal(h.facts().auth, 0);
});
test("browser ignoring AEC and revoked second authorization both fall back cleanly", async () => {
  for (const options of [{aec:false}, {authFails:2}]) {
    const h = setup(options); await h.handle.closed;
    assert.equal(h.facts().stopped, 1); assert.equal(h.facts().confirms, 0);
    assert.equal(h.phases.at(-1), "unavailable");
  }
});
test("echo-only resumes same playback and page background closes microphone", async () => {
  const h = setup(); await flush();
  h.feed(1024,.002); h.feed(256,.08); assert.equal(h.audio.paused,true);
  h.feed(1200,.002); assert.equal(h.facts().resumeCalls,1);
  h.hide(); h.feed(64,.1); await h.handle.closed;
  assert.equal(h.facts().stopped,1); assert.equal(h.facts().confirms,0);
});
test("cancel during getUserMedia retains lease until late stream is stopped", async () => {
  let resolve!: (stream: MediaStream) => void;
  const media = new Promise<MediaStream>(r => {resolve=r;});
  const h = setup({media}); await flush();
  h.handle.close(); let finished = false; void h.handle.closed.then(() => {finished=true;}); await flush();
  assert.equal(finished,false); assert.equal(h.facts().released,0);
  resolve(h.stream); await h.handle.closed;
  assert.equal(h.facts().stopped,1); assert.equal(h.facts().released,1); assert.equal(h.facts().confirms,0);
});
