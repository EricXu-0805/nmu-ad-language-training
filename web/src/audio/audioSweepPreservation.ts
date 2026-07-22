/**
 * A later caller can add a preserve claim while an earlier sweep is waiting on
 * the server. Ref-counting prevents either caller's finally block from dropping
 * the other's claim.
 */
export class AudioSweepPreservationRegistry {
  readonly #claims = new Map<string, number>();

  claim(rawAudioIds: ReadonlySet<string>): { release(): void } {
    const ids = [...rawAudioIds];
    for (const rawAudioId of ids) {
      this.#claims.set(rawAudioId, (this.#claims.get(rawAudioId) ?? 0) + 1);
    }
    let released = false;
    return {
      release: () => {
        if (released) return;
        released = true;
        for (const rawAudioId of ids) {
          const next = (this.#claims.get(rawAudioId) ?? 0) - 1;
          if (next <= 0) this.#claims.delete(rawAudioId);
          else this.#claims.set(rawAudioId, next);
        }
      },
    };
  }

  isPreserved(rawAudioId: string): boolean {
    return this.#claims.has(rawAudioId);
  }
}
