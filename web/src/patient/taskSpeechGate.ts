import type { SpeechOutcome } from "./tts";

export interface TaskSpeechRequest {
  scope: string;
  key: string;
  enabled: boolean;
  ready: boolean;
  question: string;
  cue: string | null;
  cueLevel: number;
  feedback: string | null;
  feedbackSeq?: number;
}
interface Line { tag: string; text: string; issued: boolean; outcome: SpeechOutcome | null }
export type TaskSpeechStatus = "waiting" | "played" | "failed" | "muted";

let nextGateInstance = 0;

/** Playback receipts are per utterance and per run, never a shared turn tag. */
export class TaskSpeechGate {
  private readonly instance = ++nextGateInstance;
  private scope: string | null = null;
  private request: TaskSpeechRequest | null = null;
  private lines: Line[] = [];
  private serial = 0;
  private muted = false;
  private hasFeedback = false;

  clear(): void {
    this.scope = null;
    this.request = null;
    this.lines = [];
    this.muted = false;
    this.hasFeedback = false;
  }

  /** Returns whether the caller must stop the old audio before dispatching. */
  update(next: TaskSpeechRequest | null, replay = false): boolean {
    if (!next) {
      const hadScope = this.scope !== null;
      this.clear();
      return hadScope;
    }
    const reset = next.scope !== this.scope || next.enabled === this.muted || replay;
    const previous = this.request;
    const replayFeedback = replay && this.hasFeedback;
    if (reset) {
      this.lines = [];
      this.request = null;
      this.hasFeedback = false;
      this.scope = next.scope;
      this.muted = !next.enabled;
    }
    if (!next.enabled) {
      this.request = next;
      return reset;
    }
    if (!next.ready || this.request?.key === next.key) return reset;
    if (!this.request) {
      this.add(next.question);
      this.add(next.cue);
      if (replayFeedback) { this.add(next.feedback); this.hasFeedback = !!next.feedback; }
    } else {
      if (next.cueLevel !== previous?.cueLevel || next.cue !== previous?.cue) this.add(next.cue);
      // The first connected projection is a historical baseline, not a new feedback event.
      if (next.feedbackSeq != null && next.feedbackSeq !== previous?.feedbackSeq && next.feedback) {
        this.add(next.feedback);
        this.hasFeedback = true;
      }
    }
    this.request = next;
    return reset;
  }

  private add(text: string | null): void {
    if (text) this.lines.push({ tag: `task:${this.instance}:${this.scope}:${++this.serial}`, text, issued: false, outcome: null });
  }

  status(key: string, enabled: boolean): TaskSpeechStatus {
    if (this.request?.key !== key || this.muted === enabled) return "waiting";
    if (this.muted) return "muted";
    if (this.lines.some(line => line.outcome === "failed" || line.outcome === "interrupted")) return "failed";
    return this.lines.every(line => line.outcome === "played") ? "played" : "waiting";
  }

  takePending(): { tag: string; text: string }[] {
    if (this.muted || this.lines.some(line => line.outcome === "failed" || line.outcome === "interrupted")) return [];
    const pending = this.lines.filter(line => !line.issued);
    for (const line of pending) line.issued = true;
    return pending.map(({ tag, text }) => ({ tag, text }));
  }

  settle(tag: string, outcome: SpeechOutcome): boolean {
    const line = this.lines.find(line => line.tag === tag && line.issued);
    if (!line || line.outcome !== null) return false;
    line.outcome = outcome;
    return true;
  }
}
