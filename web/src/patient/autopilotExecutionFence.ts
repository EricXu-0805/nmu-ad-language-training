/**
 * Serializes controller teardown, passive refresh/recovery, and the next active
 * controller bootstrap. React effects may switch gates faster than an upload or
 * ACK can settle; this fence keeps those generations from sharing one delivery.
 */
export class AutopilotExecutionFence {
  private shutdown: Promise<void> | null = null;
  private passive: Promise<void> | null = null;

  registerControllerShutdown(operation: Promise<void>): void {
    const before = this.shutdown;
    const combined = before && before !== operation
      ? Promise.allSettled([before, operation]).then(() => undefined)
      : operation;
    this.shutdown = combined;
    const clear = () => { if (this.shutdown === combined) this.shutdown = null; };
    void combined.then(clear, clear);
  }

  private async waitForShutdowns(): Promise<void> {
    while (this.shutdown) {
      const operation = this.shutdown;
      await operation;
      if (this.shutdown === operation) this.shutdown = null;
    }
  }

  async runPassive<T>(operation: () => Promise<T>): Promise<T> {
    const before = this.passive;
    const task = (async () => {
      if (before) await before;
      await this.waitForShutdowns();
      return operation();
    })();
    const current = task.then(() => undefined);
    // Keep rejection observable to a concurrent waiter while also preventing an
    // unhandled tail when this caller is the only observer.
    void current.catch(() => undefined);
    this.passive = current;
    try {
      return await task;
    } finally {
      if (this.passive === current) this.passive = null;
    }
  }

  /** Wait until neither an old controller nor passive recovery owns delivery. */
  async waitForActiveStart(): Promise<void> {
    while (true) {
      await this.waitForShutdowns();
      const operation = this.passive;
      if (!operation) return;
      await operation;
    }
  }
}
