export interface RapportPlaybackIdentity {
  sectionKey: string;
  questionIdx: number;
  beat: "ask" | "reply";
  utteranceId: number | null;
  wseq: number;
}
export interface RapportPlaybackReceipt extends RapportPlaybackIdentity { outcome: "played" | "failed" }
export function playbackIdentity(step: {
  sectionKey: string; questionIdx: number; beat?: "ask" | "reply";
  utteranceId?: number; wseq?: number;
}): RapportPlaybackIdentity | null {
  if (!Number.isSafeInteger(step.wseq) || Number(step.wseq) < 1) return null;
  return { sectionKey: step.sectionKey, questionIdx: step.questionIdx, beat: step.beat ?? "ask",
    utteranceId: step.utteranceId ?? null, wseq: Number(step.wseq) };
}
export function samePlayback(a: RapportPlaybackIdentity, b: RapportPlaybackIdentity): boolean {
  return a.sectionKey === b.sectionKey && a.questionIdx === b.questionIdx && a.beat === b.beat
    && a.utteranceId === b.utteranceId && a.wseq === b.wseq;
}
export function parsePlaybackReceipt(value: unknown): RapportPlaybackReceipt | null {
  if (!value || typeof value !== "object" || !("receipt" in value)) throw new Error("播放回执不完整");
  const receipt = (value as { receipt: unknown }).receipt;
  if (receipt === null) return null;
  if (!receipt || typeof receipt !== "object") throw new Error("播放回执不完整");
  const row = receipt as Record<string, unknown>;
  if (typeof row.sectionKey !== "string" || !Number.isSafeInteger(row.questionIdx) || Number(row.questionIdx) < 0
    || (row.beat !== "ask" && row.beat !== "reply")
    || (row.utteranceId !== null && (!Number.isSafeInteger(row.utteranceId) || Number(row.utteranceId) < 1))
    || !Number.isSafeInteger(row.wseq) || Number(row.wseq) < 1
    || (row.outcome !== "played" && row.outcome !== "failed")) throw new Error("播放回执字段无效");
  return row as unknown as RapportPlaybackReceipt;
}

/** Wait for the exact device fact, never estimate playback from character count. */
export async function waitForRapportPlayback(expected: RapportPlaybackIdentity, ports: {
  read: () => Promise<RapportPlaybackReceipt | null>;
  isCurrent: () => boolean;
  sleep: (ms: number) => Promise<void>;
  now: () => number;
}): Promise<"played" | "failed" | "cancelled" | "timeout"> {
  const deadline = ports.now() + 60_000;
  while (ports.isCurrent()) {
    try {
      const receipt = await ports.read();
      if (!ports.isCurrent()) return "cancelled";
      if (receipt && samePlayback(expected, receipt)) return receipt.outcome;
    } catch { /* A temporary read failure is not a claim that speech finished. */ }
    if (!ports.isCurrent()) return "cancelled";
    if (ports.now() >= deadline) return "timeout";
    await ports.sleep(500);
  }
  return "cancelled";
}
