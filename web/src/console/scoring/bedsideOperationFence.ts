export interface BedsideOperationFence {
  sessionId: string;
  turnKey: string;
  epoch: number;
  requiresAutopilot: boolean;
}

export interface BedsideOperationState {
  sessionId: string;
  turnKey: string;
  epoch: number;
  blocked: boolean;
  autopilotActive: boolean;
}

/**
 * A promise or timer may outlive the bedside state that created it.  Capture
 * this value before the asynchronous boundary and re-check it before every
 * presentation, journal mutation, or navigation side effect.
 */
export function captureBedsideOperation(
  state: Pick<BedsideOperationState, "sessionId" | "turnKey" | "epoch">,
  requiresAutopilot: boolean,
): BedsideOperationFence {
  return { ...state, requiresAutopilot };
}

export function bedsideOperationIsCurrent(
  fence: BedsideOperationFence,
  current: BedsideOperationState,
): boolean {
  return !current.blocked
    && fence.sessionId === current.sessionId
    && fence.turnKey === current.turnKey
    && fence.epoch === current.epoch
    && (!fence.requiresAutopilot || current.autopilotActive);
}
