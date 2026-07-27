export type PatientAutopilotMode = "legacy" | "probing" | "server" | "blocked";

/**
 * A non-rapport patient page cannot know which control plane owns the session
 * until an exact active capability has probed `/autopilot/next`. Never mount the
 * legacy stage merely because a fresh tab has not paired yet.
 */
export function resolvePatientAutopilotVisibleMode(input: {
  hasSession: boolean;
  hasExactCapability: boolean;
  probeKey: string;
  resolvedProbeKey: string;
  resolvedMode: PatientAutopilotMode;
}): PatientAutopilotMode {
  if (!input.hasSession) return "legacy";
  if (!input.hasExactCapability || input.probeKey !== input.resolvedProbeKey) {
    return "probing";
  }
  return input.resolvedMode;
}

export function canProbePatientAutopilot(input: {
  hasSession: boolean;
  hasExactCapability: boolean;
  sessionTerminal: boolean;
}): boolean {
  return input.hasSession && input.hasExactCapability && !input.sessionTerminal;
}

/**
 * One bedside activation signal per (session, activation lifecycle). It fires
 * only after the elder's explicit browser click with TTS on and the live
 * connection ready. The click is captured with the sessionId it happened in
 * (`activatedForSessionId`); a session switch can therefore never reuse the
 * previous subject's activation, even for the one commit where stale React
 * state still says "activated". It is a local trigger condition for the
 * same-window console — never a research authorization, never server truth,
 * and never usage evidence.
 */
export function canSignalBedsideActivation(input: {
  sessionId: string | null;
  sessionMode: "task" | "rapport" | null;
  sessionTerminal: boolean;
  activatedForSessionId: string | null;
  ttsOn: boolean;
  connectionReady: boolean;
  alreadySignaledSessionId: string | null;
}): boolean {
  return input.sessionId !== null
    && input.sessionId !== ""
    && input.sessionMode === "task"
    && !input.sessionTerminal
    && input.activatedForSessionId === input.sessionId
    && input.ttsOn
    && input.connectionReady
    && input.alreadySignaledSessionId !== input.sessionId;
}

export function canRunPatientAutopilotMedia(input: {
  serverOwned: boolean;
  hasDurableDelivery: boolean;
  activated: boolean;
  ttsOn: boolean;
  connectionReady: boolean;
  sessionPaused: boolean;
  sessionTerminal: boolean;
}): boolean {
  return input.serverOwned
    && input.hasDurableDelivery
    && input.activated
    && input.ttsOn
    && input.connectionReady
    && !input.sessionPaused
    && !input.sessionTerminal;
}
