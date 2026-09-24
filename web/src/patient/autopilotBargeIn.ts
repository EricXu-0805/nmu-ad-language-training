/** Local acoustic evidence only: never an answer, transcript, or recording. */
export const BARGE_IN_FRAME_MS = 64;
export const BARGE_IN_CALIBRATION_MS = 640;
export const BARGE_IN_ECHO_SETTLE_MS = 192;
export const BARGE_IN_CONFIRM_TIMEOUT_MS = 960;
export const BARGE_IN_ONSET_MS = 256;
export const BARGE_IN_MAX_FRAME_GAP_MS = 160;

export type BargeInDecision = "none" | "candidate" | "confirmed" | "resume";

/** A pause-and-confirm gate: playback echo alone must disappear in the confirmation phase. */
export function createBargeInDetector() {
  let firstAt: number | null = null;
  let lastAt: number | null = null;
  let candidateAt: number | null = null;
  let voicedMs = 0;
  let cooldownUntil = 0;
  let attempts = 0;
  let done = false;
  const baseline: number[] = [];
  let floor = 0.004;
  return {
    push(rms: number, voiceBandRatio: number, atMs: number): BargeInDecision {
      if (done || !Number.isFinite(rms) || rms < 0 || !Number.isFinite(atMs)
          || !Number.isFinite(voiceBandRatio) || voiceBandRatio < 0 || voiceBandRatio > 1) return "none";
      if (lastAt !== null && atMs <= lastAt) return "none";
      const gap = lastAt === null ? 0 : atMs - lastAt;
      lastAt = atMs;
      if (firstAt === null) firstAt = atMs;
      // A delayed animation/sample is not proof of continuous speech.
      if (gap > BARGE_IN_MAX_FRAME_GAP_MS) voicedMs = 0;
      const delta = gap <= BARGE_IN_MAX_FRAME_GAP_MS ? Math.min(gap, BARGE_IN_FRAME_MS) : 0;
      if (atMs - firstAt < BARGE_IN_CALIBRATION_MS) {
        baseline.push(rms);
        return "none";
      }
      if (baseline.length) {
        baseline.sort((a, b) => a - b);
        // The calibration can contain TTS leakage; cap it instead of making quiet voices impossible.
        floor = Math.min(0.012, baseline[Math.floor(baseline.length / 4)]);
        baseline.length = 0;
      }
      if (candidateAt !== null) {
        const elapsed = atMs - candidateAt;
        if (elapsed < BARGE_IN_ECHO_SETTLE_MS) return "none";
        const speechLike = voiceBandRatio >= 0.55 && rms >= Math.max(0.012, floor * 1.5);
        voicedMs = speechLike ? voicedMs + delta : 0;
        if (voicedMs >= BARGE_IN_ONSET_MS) {
          done = true;
          return "confirmed";
        }
        if (elapsed >= BARGE_IN_CONFIRM_TIMEOUT_MS) {
          candidateAt = null;
          voicedMs = 0;
          cooldownUntil = atMs + 1_500;
          // Repeated false detections should not keep stopping the prompt.
          if (attempts >= 2) done = true;
          return "resume";
        }
        return "none";
      }
      if (atMs < cooldownUntil) return "none";
      const speechLike = voiceBandRatio >= 0.55 && rms >= Math.max(0.018, floor * 3);
      voicedMs = speechLike ? voicedMs + delta : 0;
      if (voicedMs < BARGE_IN_ONSET_MS) return "none";
      candidateAt = atMs;
      voicedMs = 0;
      attempts += 1;
      return "candidate";
    },
  };
}
