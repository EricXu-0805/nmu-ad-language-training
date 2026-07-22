export interface BedsideSafetyLatch {
  sessionId: string;
  observedServerPause: boolean;
}

/** Accept only a stop addressed to the patient session currently on screen. */
export function latchBedsideSafetyStop(
  activeSessionId: string | null,
  stoppedSessionId: string,
): BedsideSafetyLatch | null {
  return activeSessionId !== null && activeSessionId === stoppedSessionId
    ? { sessionId: stoppedSessionId, observedServerPause: false }
    : null;
}

/**
 * A local stop is reduction-only and cannot be undone by an old active frame.
 * It releases only after this exact session first observes the authoritative
 * paused projection and subsequently observes the authoritative resumed one.
 */
export function reconcileBedsideSafetyLatch(
  latch: BedsideSafetyLatch | null,
  activeSessionId: string | null,
  serverPaused: boolean,
): BedsideSafetyLatch | null {
  if (!latch || latch.sessionId !== activeSessionId) return null;
  if (serverPaused) {
    return latch.observedServerPause
      ? latch : { ...latch, observedServerPause: true };
  }
  return latch.observedServerPause ? null : latch;
}

export function bedsideSafetyIsLatched(
  latch: BedsideSafetyLatch | null,
  activeSessionId: string | null,
): boolean {
  return latch !== null && latch.sessionId === activeSessionId;
}
