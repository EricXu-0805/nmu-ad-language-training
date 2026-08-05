/**
 * Purely local presentation state for the window the server cannot see.
 *
 * Two windows, not one.  The microphone is really open from the moment the
 * browser fires `onstart`, but `record_started` still has to be staged and
 * ACKed before the server runtime says `recording`; telling the patient to
 * wait through that round trip wastes their answer window.  Symmetrically the
 * microphone is physically closed once `recorder.stop()` resolves, while the
 * server legitimately still reads `recording` during upload — saying
 * "正在听您说" there asks them to keep talking into a closed microphone.  Both
 * facts are known to the browser alone, so the browser reports them.  This is
 * a UI signal only: it never becomes a server phase, an ACK, or anything the
 * upload path reads.
 */
export interface LocalAutopilotCaptureIdentity {
  readonly sessionId: string;
  readonly commandKey: string;
  /** Which hook/owner lease generation opened this capture. */
  readonly ownerGeneration: number;
  /** Which capture within that owner. Never reused, so late events are detectable. */
  readonly captureGeneration: number;
}

export type LocalAutopilotCapturePhase =
  | (LocalAutopilotCaptureIdentity & { readonly phase: "listening" })
  | (LocalAutopilotCaptureIdentity & {
    readonly phase: "persisting";
    // Only a real successful stop carries a reason; "listening" must never
    // manufacture one for a stop that has not happened.
    readonly stopReason: "user_done" | "max_duration";
  });

export type LocalAutopilotCaptureClear =
  LocalAutopilotCaptureIdentity & { readonly phase: "cleared" };

export type LocalAutopilotCaptureEvent =
  LocalAutopilotCapturePhase | LocalAutopilotCaptureClear;

export type LocalAutopilotCaptureObserver = (
  event: LocalAutopilotCaptureEvent,
) => void;

/**
 * Does this local phase describe exactly the command now on screen?
 *
 * Both halves are compared, and `command_revision` deliberately is not: a
 * pending→started transition legitimately bumps it, and the display must not
 * flip back because of that.
 */
export function capturePhaseMatches(
  phase: LocalAutopilotCapturePhase | null | undefined,
  sessionId: string | null | undefined,
  commandKey: string | null | undefined,
): boolean {
  if (!phase || !sessionId || !commandKey) return false;
  return phase.sessionId === sessionId && phase.commandKey === commandKey;
}

/**
 * Exact identity match, all four fields.
 *
 * A clear event from an older owner or an older capture must never wipe the
 * phase a newer capture just published, and a late phase event from a dead
 * capture must never overwrite the live one.
 */
export function sameCaptureIdentity(
  left: LocalAutopilotCaptureIdentity | null | undefined,
  right: LocalAutopilotCaptureIdentity | null | undefined,
): boolean {
  if (!left || !right) return false;
  return left.sessionId === right.sessionId
    && left.commandKey === right.commandKey
    && left.ownerGeneration === right.ownerGeneration
    && left.captureGeneration === right.captureGeneration;
}

/** Is `candidate` at least as new as `current`? Late events from a dead capture are not. */
export function captureIdentityIsNewer(
  candidate: LocalAutopilotCaptureIdentity,
  current: LocalAutopilotCaptureIdentity | null | undefined,
): boolean {
  if (!current) return true;
  if (candidate.ownerGeneration !== current.ownerGeneration) {
    return candidate.ownerGeneration > current.ownerGeneration;
  }
  return candidate.captureGeneration >= current.captureGeneration;
}

/**
 * First-writer-wins stop latch, shared by the answer-window timer and the
 * patient's optional "I'm done" button.
 *
 * Whichever arrives first decides the recorded `stop_reason` and is the only
 * one that ever settles the waiter; a late timer firing just after a tap must
 * not rewrite that reason, and must not resolve a second time.  `signal`
 * reports whether this caller won, so the behaviour is observable instead of
 * being asserted from a comment.
 */
export interface StopLatch<R> {
  readonly reason: R | null;
  signal(reason: R): boolean;
  wait(): Promise<R>;
}

export function createStopLatch<R>(): StopLatch<R> {
  let settled: R | null = null;
  let resolve: ((value: R) => void) | null = null;
  return {
    get reason() { return settled; },
    signal(reason: R): boolean {
      if (settled !== null) return false;
      settled = reason;
      resolve?.(reason);
      return true;
    },
    wait(): Promise<R> {
      if (settled !== null) return Promise.resolve(settled);
      return new Promise((res) => { resolve = res; });
    },
  };
}

/**
 * Await the physical stop, validate the capture, then announce persistence.
 *
 * `stop` is the real `recorder.stop()`; nothing is announced until it resolves,
 * because before that the microphone may still be open.  `validate` is the
 * caller's real fail-closed check — empty bytes and an over-limit duration both
 * throw there — and it runs *before* the announcement, so a capture that is
 * about to be rejected never shows "已收音，正在保存" first.  The announcement
 * still precedes staging and every network round trip, so the screen changes
 * when the patient stops being recorded rather than when the upload finishes.
 *
 * The observer is a best-effort UI signal: if it throws, the capture is
 * unaffected and the recording is returned unchanged.
 */
export async function finalizeCaptureThenNotify<T>(
  stop: () => Promise<T>,
  validate: (recording: T) => void,
  phase: LocalAutopilotCaptureEvent,
  observe?: LocalAutopilotCaptureObserver,
): Promise<T> {
  const recording = await stop();
  validate(recording);
  if (observe) {
    try {
      observe(phase);
    } catch {
      // A broken screen update must never cost the recording that was made.
    }
  }
  return recording;
}
