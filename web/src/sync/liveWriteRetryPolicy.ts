export type LiveWriteRetryKind = "session" | "cursor" | "rapportStep";

export type LiveWriteRetryDecision = "retry_exact" | "restart_context";

/**
 * Replaying a long-lived session handshake is not a harmless retry: the server
 * intentionally clears transient patientRec/audio slots when a new bedside
 * context is established. An ambiguous handshake therefore requires an
 * explicit context restart; only position writes may be replayed in place.
 */
export function liveWriteRetryDecision(
  kind: LiveWriteRetryKind,
): LiveWriteRetryDecision {
  return kind === "session" ? "restart_context" : "retry_exact";
}

/**
 * A refused handshake leaves this console off every bedside slot, so each
 * later cursor/rapport write is refused as a session mismatch. Keep showing
 * the handshake's own reason (e.g. both beds in use) instead of that echo.
 */
export function rejectedLiveWriteMessage(
  kind: LiveWriteRetryKind,
  message: string,
  handshakeRefusal: string | null,
): string {
  return kind === "session" ? message : handshakeRefusal ?? message;
}
