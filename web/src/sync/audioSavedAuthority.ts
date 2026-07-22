import { parseSyncPayload, type AudioSavedMsg } from "./messages.ts";

/**
 * Only a structurally valid server projection may become a delivered audioSaved event.
 * BroadcastChannel data never enters this function as authority; it only wakes the fetch
 * that supplies `serverPayload`.
 */
export function deliverAuthoritativeAudioSaved(
  serverPayload: unknown,
  seen: Set<string>,
  handler: (message: AudioSavedMsg) => void,
): boolean {
  const message = parseSyncPayload("audioSaved", serverPayload);
  if (!message || seen.has(message.rawAudioId)) return false;
  // Mark only after the full runtime schema has passed. A forged malformed message cannot
  // reserve a real rawAudioId in `seen` and suppress the later server-confirmed receipt.
  seen.add(message.rawAudioId);
  handler(message);
  return true;
}
