export type TtsPlaybackContextKey = string;

export interface TtsReplayLine {
  text: string;
  tag: string;
  contextKey: TtsPlaybackContextKey;
}

export interface TtsContextState {
  activeContextKey: TtsPlaybackContextKey | null;
  lastText: TtsReplayLine | null;
}

export const EMPTY_TTS_CONTEXT_STATE: TtsContextState = {
  activeContextKey: null,
  lastText: null,
};

/**
 * A patient/control-plane transition is a revocation boundary, not navigation
 * history.  Re-entering the same textual key after a pause therefore starts
 * empty once the context has first been cleared to null.
 */
export function transitionTtsContext(
  state: TtsContextState,
  nextContextKey: TtsPlaybackContextKey | null,
): { state: TtsContextState; changed: boolean } {
  const normalized = nextContextKey?.trim() || null;
  if (normalized === state.activeContextKey) return { state, changed: false };
  return {
    state: { activeContextKey: normalized, lastText: null },
    changed: true,
  };
}

export function rememberTtsLine(
  state: TtsContextState,
  line: TtsReplayLine,
): TtsContextState {
  if (state.activeContextKey === null || line.contextKey !== state.activeContextKey) {
    return state;
  }
  return { ...state, lastText: line };
}

/** A sample may only replay text painted by this exact active bedside context. */
export function replayLineForContext(
  state: TtsContextState,
  expectedContextKey: TtsPlaybackContextKey | null,
): TtsReplayLine | null {
  if (!expectedContextKey || state.activeContextKey !== expectedContextKey) return null;
  return state.lastText?.contextKey === expectedContextKey ? state.lastText : null;
}
