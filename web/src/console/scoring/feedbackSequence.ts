let feedbackSequence = Date.now();

export function observeFeedbackSequence(value: unknown): void {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return;
  feedbackSequence = Math.max(feedbackSequence, Math.trunc(value));
}

export function nextFeedbackSequence(...observed: unknown[]): number {
  observed.forEach(observeFeedbackSequence);
  feedbackSequence = Math.max(feedbackSequence, Date.now()) + 1;
  return feedbackSequence;
}
