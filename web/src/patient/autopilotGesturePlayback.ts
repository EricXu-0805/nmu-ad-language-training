/** Retry on the next real user gesture; the caller owns physical audio teardown. */
export function playAutopilotOnNextGesture(
  audio: Pick<HTMLAudioElement, "play">,
  signal: AbortSignal,
  host: EventTarget,
): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) { reject(signal.reason); return; }
    let settled = false;
    const finish = (outcome: { ok: true } | { ok: false; error: unknown }) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (outcome.ok) resolve();
      else reject(outcome.error);
    };
    const onGesture = () => {
      if (settled || signal.aborted) return;
      removeGestures();
      // Must run synchronously in the gesture handler to retain user activation.
      // Do not resolve with the native promise: once adopted, an abort cannot
      // reject this wait while the browser's play() is still pending.
      try {
        void audio.play().then(
          () => finish({ ok: true }),
          (error: unknown) => finish({ ok: false, error }),
        );
      } catch (error) { finish({ ok: false, error }); }
    };
    const onAbort = () => finish({ ok: false, error: signal.reason });
    const removeGestures = () => {
      host.removeEventListener("pointerdown", onGesture, { capture: true });
      host.removeEventListener("click", onGesture, { capture: true });
    };
    const cleanup = () => {
      removeGestures();
      signal.removeEventListener("abort", onAbort);
    };
    // Keyboard and assistive activation can emit click without pointerdown.
    // Either event consumes both listeners before play, so one touch cannot retry twice.
    host.addEventListener("pointerdown", onGesture, { capture: true, once: true });
    host.addEventListener("click", onGesture, { capture: true, once: true });
    signal.addEventListener("abort", onAbort, { once: true });
  });
}
