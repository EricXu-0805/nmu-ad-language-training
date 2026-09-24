import { acquireAudioDeviceLease, type AudioDeviceLease } from "../audio/audioDeviceLease.ts";
import { BARGE_IN_FRAME_MS, createBargeInDetector } from "./autopilotBargeIn.ts";
import type { NextCommandProjection } from "./autopilotProtocol.ts";

export type BargeInPhase = "monitoring" | "interrupted" | "unavailable" | "closed";
export interface BargeInHandle { close(): void; closed: Promise<void> }
export interface BargeInObservation {
  sessionId: string;
  commandKey: string;
  itemRef: string;
  turnSeq: number;
  attemptSeq: number;
  controlGeneration: number;
  runnerGeneration: number;
  phase: BargeInPhase;
}
type TtsCommand = Extract<NextCommandProjection, { kind: "tts" }>;
export interface BargeInPorts {
  permissionGranted(): Promise<boolean>;
  foreground(): boolean;
  authorize(sessionId: string, command: TtsCommand, signal: AbortSignal): Promise<void>;
  acquireLease(signal: AbortSignal): Promise<AudioDeviceLease>;
  getUserMedia(): Promise<MediaStream>;
  createContext(): AudioContext;
  now(): number;
  setInterval(callback: () => void, ms: number): number;
  clearInterval(handle: number): void;
}

export const browserBargeInPorts: BargeInPorts = {
  permissionGranted: async () => {
    // Never introduce a new permission dialog while an elderly participant is listening.
    try { return (await navigator.permissions.query({ name: "microphone" as PermissionName })).state === "granted"; }
    catch { return false; }
  },
  foreground: () => document.visibilityState === "visible",
  authorize: async (sessionId, command, signal) => {
    const [{ authorizeExactAutopilotBargeIn }, { browserAutopilotMediaDependencies }] = await Promise.all([
      import("./autopilotMediaTransport.ts"), import("./autopilotBrowserMediaDependencies.ts"),
    ]);
    await authorizeExactAutopilotBargeIn(sessionId, command, signal, browserAutopilotMediaDependencies);
  },
  acquireLease: (signal) => acquireAudioDeviceLease(undefined, signal),
  getUserMedia: () => navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    video: false,
  }),
  createContext: () => new AudioContext(),
  now: () => performance.now(),
  setInterval: (callback, ms) => window.setInterval(callback, ms),
  clearInterval: (handle) => window.clearInterval(handle),
};

/**
 * Owns a microphone only for ephemeral local analysis. No MediaRecorder, Blob,
 * transcript, persistence or upload exists here. A fresh record command remains
 * the only way to capture an answer. The UI therefore asks to repeat the start.
 */
export function startBargeInMonitor(input: {
  sessionId: string;
  command: TtsCommand;
  audio: HTMLAudioElement;
  onConfirmed(): boolean;
  onPlaybackFailed(error: unknown): void;
  observe?(observation: BargeInObservation): void;
}, ports: BargeInPorts = browserBargeInPorts): BargeInHandle {
  const abort = new AbortController();
  let finished = false;
  let lease: AudioDeviceLease | null = null;
  let stream: MediaStream | null = null;
  let context: AudioContext | null = null;
  let source: MediaStreamAudioSourceNode | null = null;
  let timer: number | null = null;
  let pausedForConfirmation = false;
  let setupPending = true;
  let leaseReleased = false;
  let resolveClosed!: () => void;
  const closed = new Promise<void>((resolve) => { resolveClosed = resolve; });
  const announce = (phase: BargeInPhase): void => {
    try { input.observe?.({ sessionId: input.sessionId, commandKey: input.command.command_key,
      itemRef: input.command.item_ref, turnSeq: input.command.turn_seq,
      attemptSeq: input.command.attempt_seq, controlGeneration: input.command.control_generation,
      runnerGeneration: input.command.runner_generation, phase }); } catch { /* UI observers cannot keep a microphone alive. */ }
  };
  const stopTracks = (value: MediaStream): void => {
    for (const track of value.getTracks()) { try { track.stop(); } catch { /* continue cleanup */ } }
  };
  const releaseLease = (): void => {
    if (!finished || setupPending || leaseReleased) return;
    leaseReleased = true;
    const ownedLease = lease;
    lease = null;
    if (ownedLease) {
      try { ownedLease.release(); } finally { void ownedLease.released.then(resolveClosed, resolveClosed); }
    } else resolveClosed();
  };
  const close = (): void => {
    if (finished) return;
    finished = true;
    abort.abort(new DOMException("提前回答检测已结束", "AbortError"));
    if (timer !== null) { try { ports.clearInterval(timer); } catch { /* late callback is fenced */ } timer = null; }
    if (stream) { stopTracks(stream); stream = null; }
    try { source?.disconnect(); } catch { /* already detached */ }
    source = null;
    try { void context?.close().catch(() => {}); } catch { /* best effort after physical microphone stop */ }
    context = null;
    // getUserMedia cannot be aborted. Retain the microphone lease and closed
    // fence until its late stream has been physically stopped in setup below.
    releaseLease();
    announce("closed");
  };
  const valid = (): boolean => !finished && ports.foreground()
    && input.audio.ended !== true;
  const resume = (): void => {
    if (!pausedForConfirmation || !valid()) return;
    pausedForConfirmation = false;
    // Same element and same currentTime: no skipped words after an echo-only candidate.
    void input.audio.play().catch((error: unknown) => { input.onPlaybackFailed(error); });
  };
  const unavailable = (): void => {
    resume();
    close();
    announce("unavailable");
  };
  void (async () => {
    try {
      if (input.command.state !== "started"
          || !["question", "cue"].includes(input.command.payload.purpose)
          || !valid() || !await ports.permissionGranted() || !valid()) { unavailable(); return; }
      const grantedLease = await ports.acquireLease(abort.signal);
      if (!valid()) { grantedLease.release(); close(); return; }
      lease = grantedLease;
      await ports.authorize(input.sessionId, input.command, abort.signal);
      if (!valid()) { close(); return; }
      const acquiredStream = await ports.getUserMedia();
      if (!valid()) { stopTracks(acquiredStream); close(); return; }
      stream = acquiredStream;
      // Browsers may ignore requested constraints. Only use this enhancement when AEC is actually enabled.
      const tracks = stream.getAudioTracks();
      if (tracks.length !== 1 || tracks[0].getSettings().echoCancellation !== true) { unavailable(); return; }
      await ports.authorize(input.sessionId, input.command, abort.signal);
      if (!valid()) { close(); return; }
      context = ports.createContext();
      if (context.state === "suspended") await context.resume();
      if (!valid() || context.state !== "running") { unavailable(); return; }
      source = context.createMediaStreamSource(stream);
      const analyser = context.createAnalyser();
      analyser.fftSize = 2048;
      analyser.smoothingTimeConstant = 0;
      source.connect(analyser);
      const wave = new Float32Array(analyser.fftSize);
      const spectrum = new Float32Array(analyser.frequencyBinCount);
      const sampleRate = context.sampleRate;
      const detector = createBargeInDetector();
      announce("monitoring");
      timer = ports.setInterval(() => {
        if (finished) return;
        if (!valid()) { close(); return; }
        if (context?.state !== "running" || tracks[0].readyState === "ended") { unavailable(); return; }
        try {
          analyser.getFloatTimeDomainData(wave);
          analyser.getFloatFrequencyData(spectrum);
          let square = 0;
          for (const value of wave) square += value * value;
          let total = 0;
          let voice = 0;
          for (let index = 1; index < spectrum.length; index += 1) {
            const energy = 10 ** (spectrum[index] / 10);
            const frequency = index * sampleRate / analyser.fftSize;
            total += energy;
            if (frequency >= 150 && frequency <= 3_800) voice += energy;
          }
          const decision = detector.push(Math.sqrt(square / wave.length), total > 0 ? voice / total : 0, ports.now());
          if (decision === "candidate") {
            input.audio.pause();
            if (!input.audio.paused) { unavailable(); return; }
            pausedForConfirmation = true;
          } else if (decision === "resume") resume();
          else if (decision === "confirmed") {
            // Synchronous physical microphone close precedes the interruption callback/ACK.
            close();
            if (input.onConfirmed()) announce("interrupted");
          }
        } catch { unavailable(); }
      }, BARGE_IN_FRAME_MS);
    } catch { if (!finished) unavailable(); }
    finally { setupPending = false; releaseLease(); }
  })();
  return { close, closed };
}
