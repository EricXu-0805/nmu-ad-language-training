import type { NextCommandProjection } from "./autopilotProtocol.ts";

export interface ExactAutopilotDisplayText {
  text: string;
  commandSeq: number;
  controlGeneration: number;
  runnerGeneration: number;
  itemRef: string;
  turnSeq: number;
  attemptSeq: number;
  promptLevel: 0 | 1 | 2 | 3;
  retainForRecord: boolean;
}

/** Keep only server-signed text; a record command never invents client copy. */
export function resolveExactAutopilotDisplayText(
  previous: ExactAutopilotDisplayText | null,
  command: NextCommandProjection | null,
): ExactAutopilotDisplayText | null {
  // A short server-processing gap must not erase the exact prompt before its
  // record command arrives. Session/ownership changes remount or reset the stage.
  if (command === null) return previous;
  if (command.kind === "record") {
    // The record projection carries the already-spoken predecessor text itself.
    // This keeps refresh-during-record exact without consulting a browser cache.
    return {
      text: command.payload.presentation_speech_text,
      commandSeq: command.command_seq,
      controlGeneration: command.control_generation,
      runnerGeneration: command.runner_generation,
      itemRef: command.item_ref,
      turnSeq: command.turn_seq,
      attemptSeq: command.attempt_seq,
      promptLevel: command.prompt_level,
      retainForRecord: true,
    };
  }
  return {
    text: command.payload.speech_text,
    commandSeq: command.command_seq,
    controlGeneration: command.control_generation,
    runnerGeneration: command.runner_generation,
    itemRef: command.item_ref,
    turnSeq: command.turn_seq,
    attemptSeq: command.attempt_seq,
    promptLevel: command.prompt_level,
    retainForRecord: command.payload.purpose === "question"
      || command.payload.purpose === "cue",
  };
}
