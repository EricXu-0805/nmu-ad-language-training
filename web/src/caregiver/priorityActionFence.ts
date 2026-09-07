/** Safety pause may replace any ordinary action; its late result loses UI authority. */
export class PriorityActionFence {
  private generation = 0;
  private active: { generation: number; kind: "ordinary" | "pause" } | null = null;
  begin(kind: "ordinary" | "pause"): number | null {
    if (this.active && (kind !== "pause" || this.active.kind === "pause")) return null;
    const generation = ++this.generation;
    this.active = { generation, kind };
    return generation;
  }
  current(generation: number): boolean { return this.active?.generation === generation; }
  finish(generation: number): boolean {
    if (!this.current(generation)) return false;
    this.active = null;
    return true;
  }
}
