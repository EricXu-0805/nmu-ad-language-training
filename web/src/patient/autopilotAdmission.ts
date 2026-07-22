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
