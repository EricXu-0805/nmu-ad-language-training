export interface AuthorizationProbe {
  generation: number;
  capability: string | null;
}

/** Rejects queued responses issued under an older active-device credential. */
export class LiveAuthorizationFence {
  private generation = 0;

  capture(capability: string | null): AuthorizationProbe {
    return { generation: this.generation, capability };
  }

  invalidate(): void {
    this.generation += 1;
  }

  accepts(probe: AuthorizationProbe, selectedCapability: string | null): boolean {
    return probe.generation === this.generation
      && probe.capability === selectedCapability;
  }
}
