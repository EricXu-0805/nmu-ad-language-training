function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

/** Authorization changed after synthesis: bytes and local-speech fallback are both stale. */
export function mustDiscardSynthesizedSpeech(status: number, responseText: string): boolean {
  if (status !== 409) return false;
  try {
    const top = record(JSON.parse(responseText));
    if (!top) return false;
    const detail = record(top.detail) ?? top;
    return detail.code === "tts_authorization_changed"
      && detail.action === "discard_synthesized_audio";
  } catch {
    return false;
  }
}
