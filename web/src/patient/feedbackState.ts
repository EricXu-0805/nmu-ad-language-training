export interface FeedbackPlaybackState {
  connectedBaselineSeen: boolean;
  lastSeq: number | null;
}

export interface FeedbackPlaybackTransition {
  state: FeedbackPlaybackState;
  shouldSpeak: boolean;
}

export const initialFeedbackPlaybackState = (): FeedbackPlaybackState => ({
  connectedBaselineSeen: false,
  lastSeq: null,
});

/**
 * Distinguish a restored cursor from a feedback event that arrived after the
 * patient view was connected. The first connected snapshot is a baseline and
 * must never replay historical speech; every later sequence change is new.
 */
export function advanceFeedbackPlayback(
  previous: FeedbackPlaybackState,
  connected: boolean,
  seq: number | null | undefined,
): FeedbackPlaybackTransition {
  const nextSeq = typeof seq === "number" && Number.isFinite(seq) ? seq : null;

  if (!connected) {
    return {
      state: { ...previous, lastSeq: nextSeq ?? previous.lastSeq },
      shouldSpeak: false,
    };
  }

  if (!previous.connectedBaselineSeen) {
    return {
      state: { connectedBaselineSeen: true, lastSeq: nextSeq ?? previous.lastSeq },
      shouldSpeak: false,
    };
  }

  if (nextSeq == null || nextSeq === previous.lastSeq) {
    return { state: previous, shouldSpeak: false };
  }

  return {
    state: { connectedBaselineSeen: true, lastSeq: nextSeq },
    shouldSpeak: true,
  };
}
