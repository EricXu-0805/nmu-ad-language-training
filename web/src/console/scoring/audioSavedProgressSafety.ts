export type AudioSavedAutomationAction = "ignore" | "record-only" | "adjudicate";

export interface AudioSavedAutomationContext {
  sameSession: boolean;
  sameTurn: boolean;
  alreadyRecorded: boolean;
  safetyFailureLatched: boolean;
  manualTakeoverForTurn: boolean;
  adjudicationEnabled: boolean;
  interactionBlocked: boolean;
}

export const LEGACY_AUDIO_SAVED_AUTO_ADVANCE_KEY = "nmu:console:autoAdvance";

/**
 * A durable audioSaved receipt proves capture only. It can start adjudication,
 * but it is never authority to change the training position by itself.
 */
export function decideAudioSavedAutomation(
  context: AudioSavedAutomationContext,
): AudioSavedAutomationAction {
  if (!context.sameSession) return "ignore";
  if (!context.sameTurn || context.alreadyRecorded) return "record-only";
  if (context.safetyFailureLatched || context.manualTakeoverForTurn) return "record-only";
  if (!context.adjudicationEnabled || context.interactionBlocked) return "record-only";
  return "adjudicate";
}

export function retireLegacyAudioSavedAutoAdvancePreference(
  storage: Pick<Storage, "removeItem">,
): void {
  try {
    storage.removeItem(LEGACY_AUDIO_SAVED_AUTO_ADVANCE_KEY);
  } catch {
    // Private browsing and locked-down devices may reject storage mutations.
  }
}
