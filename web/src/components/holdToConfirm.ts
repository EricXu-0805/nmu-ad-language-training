/** Two distinct gestures are required, even if focus moves while a key is held. */
export class HoldToConfirm {
  private timer: number | null = null;
  private expiry: number | null = null;
  private held = false;
  private armed = false;
  private confirmationPressed = false;
  private readonly ports: {
    schedule: (fn: () => void, ms: number) => number;
    cancel: (timer: number) => void;
    showHolding: (value: boolean) => void;
    showArmed: (value: boolean) => void;
  };
  constructor(ports: HoldToConfirm["ports"]) { this.ports = ports; }
  press(): void {
    if (this.armed) {
      if (!this.held) this.confirmationPressed = true;
      return;
    }
    if (this.held) return;
    this.held = true;
    this.ports.showHolding(true);
    this.timer = this.ports.schedule(() => {
      this.timer = null;
      this.armed = true;
      this.ports.showHolding(false);
      this.ports.showArmed(true);
      this.expiry = this.ports.schedule(() => this.reset(), 4000);
    }, 2500);
  }
  release(): void {
    this.held = false;
    if (this.timer !== null) this.ports.cancel(this.timer);
    this.timer = null;
    this.ports.showHolding(false);
  }
  confirm(): boolean {
    if (!this.armed || this.held || !this.confirmationPressed) return false;
    this.reset();
    return true;
  }
  reset(): void {
    this.release();
    if (this.expiry !== null) this.ports.cancel(this.expiry);
    this.expiry = null;
    this.armed = false;
    this.confirmationPressed = false;
    this.ports.showArmed(false);
  }
}
