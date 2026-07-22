const MAX_UI_RETRY_AFTER_SECONDS = 3_600;

export function qualityRetryDeadlineMs(error: unknown, nowMs: number): number | null {
  if (error === null || typeof error !== "object" || Array.isArray(error)
      || (error as { status?: unknown }).status !== 429) return null;
  const supplied = (error as { retryAfterSeconds?: unknown }).retryAfterSeconds;
  const bounded = typeof supplied === "number" && Number.isSafeInteger(supplied)
    && supplied >= 0
    ? supplied
    : null;
  const seconds = Math.min(
    MAX_UI_RETRY_AFTER_SECONDS,
    Math.max(1, bounded ?? 1),
  );
  return nowMs + seconds * 1_000;
}

export function qualityRetryRemainingSeconds(
  retryAtMs: number | null,
  nowMs: number,
): number {
  if (retryAtMs === null || !Number.isFinite(retryAtMs) || !Number.isFinite(nowMs)) return 0;
  return Math.max(0, Math.ceil((retryAtMs - nowMs) / 1_000));
}
