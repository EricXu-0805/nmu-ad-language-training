import type { AudioOutboxEntry } from "./audioOutbox.ts";

export interface AudioRecoveryUiFence {
  sessionId: string;
  generation: number;
}

/**
 * Old-session evidence is drained before the active session is evaluated.
 * Ties are stable without relying on IndexedDB implementation order.
 */
export function orderAudioRecoveryEntries(
  entries: readonly AudioOutboxEntry[],
  activeSessionId: string,
): AudioOutboxEntry[] {
  return [...entries].sort((left, right) => {
    const leftCurrent = left.sessionId === activeSessionId ? 1 : 0;
    const rightCurrent = right.sessionId === activeSessionId ? 1 : 0;
    return leftCurrent - rightCurrent
      || left.createdAtMs - right.createdAtMs
      || left.rawAudioId.localeCompare(right.rawAudioId);
  });
}

export function shouldBroadcastRecoveredAudio(
  entry: AudioOutboxEntry,
  activeSessionId: string,
): boolean {
  return entry.sessionId === activeSessionId;
}

/** Late S1 work may clean S1 evidence but can never freeze or unblock S2 UI. */
export function recoveryMayMutateActiveUi(
  operation: AudioRecoveryUiFence,
  current: AudioRecoveryUiFence,
  evidenceSessionId: string,
): boolean {
  return operation.sessionId === current.sessionId
    && operation.generation === current.generation
    && evidenceSessionId === current.sessionId;
}
