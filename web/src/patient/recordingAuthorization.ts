import type { SessionRuntimeStatus } from "../types";

export interface RecordingAuthorization {
  allowed: boolean;
  runtime_status: SessionRuntimeStatus;
  is_simulation: boolean;
}

export function authorizesMicrophoneStart(value: RecordingAuthorization): boolean {
  return value.allowed === true && value.runtime_status === "active";
}

export type RecordingRevocationAction = "none" | "invalidate-start" | "stop-and-save";

/**
 * Self-service recording has no remote recSeq edge. Derive the fail-closed
 * action explicitly so a selfStart revocation or terminal screen cannot leave
 * either an in-flight permission request or a live microphone behind.
 */
export function recordingRevocationAction(input: {
  stopRequested: boolean;
  selfStartAllowed: boolean;
  pendingKind: "self" | "remote" | null;
  activeKind: "self" | "remote" | null;
  microphoneActive: boolean;
}): RecordingRevocationAction {
  const revoked = input.stopRequested
    || (!input.selfStartAllowed && (input.pendingKind === "self" || input.activeKind === "self"));
  if (!revoked) return "none";
  return input.microphoneActive ? "stop-and-save" : "invalidate-start";
}
