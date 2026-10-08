import { warmupIdentity, type Week1Warmup } from "../week1Warmup.ts";
export interface WarmupPlaybackFence {
  sessionId: string;
  observedStep: boolean;
  blockedThroughSeq: number;
}
/** A restored, paused or disconnected command never gains a new playback permit. */
export function observeWarmupPlayback(previous: WarmupPlaybackFence | null, input: {
  sessionId: string | undefined;
  hasStep: boolean;
  warmup?: Week1Warmup;
  connectionReady: boolean;
  paused: boolean;
  terminal: boolean;
}): { state: WarmupPlaybackFence | null; allowedIdentity: string | null } {
  if (!input.sessionId) return { state: null, allowedIdentity: null };
  const state = previous?.sessionId === input.sessionId ? { ...previous }
    : { sessionId: input.sessionId, observedStep: false, blockedThroughSeq: 0 };
  if (input.warmup && (!state.observedStep || !input.connectionReady || input.paused || input.terminal)) {
    state.blockedThroughSeq = Math.max(state.blockedThroughSeq, input.warmup.seq);
  }
  if (input.hasStep) state.observedStep = true;
  return { state, allowedIdentity: input.warmup && input.warmup.beat !== "done"
    && input.warmup.seq > state.blockedThroughSeq && input.connectionReady && !input.paused && !input.terminal
    ? warmupIdentity(input.warmup) : null };
}
