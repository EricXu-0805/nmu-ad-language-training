import type { SessionRuntimeStatus } from "../../types";

export type AutopilotFailureKind = "microphone" | "audio" | "upload" | "asr" | "classifier" | "persistence";

export interface AutopilotPosition {
  itemIdx: number;
  turnIdx: number;
  cueLevel: number;
}

export interface AutopilotFailure extends AutopilotPosition {
  kind: AutopilotFailureKind;
  message: string;
}

export type AnswerTimeoutAction = "stop-and-await-audio" | "technical-failure";

/** A timeout is semantic silence only after a microphone was known to be live. */
export function answerTimeoutAction(microphoneWasActive: boolean): AnswerTimeoutAction {
  return microphoneWasActive ? "stop-and-await-audio" : "technical-failure";
}

/**
 * Capture the exact position at a technical failure. The caller must stop all
 * timers and the microphone, but must not mutate these progress fields.
 */
export function makeAutopilotFailure(
  position: AutopilotPosition,
  kind: AutopilotFailureKind,
  message: string,
): AutopilotFailure {
  return { ...position, kind, message };
}

/** A technical-failure latch may only be released after server-confirmed pause. */
export function canReleaseAutopilotFailure(status: SessionRuntimeStatus | null | undefined): boolean {
  return status === "paused";
}
