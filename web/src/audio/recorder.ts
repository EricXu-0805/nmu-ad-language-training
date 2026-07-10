// MediaRecorder 封装。仅在 secure context(localhost/https)可用——部署收敛到本机 localhost。
// 采集的字节只落本地 IndexedDB(blobStore),经后端仅登记元数据,声纹绝不上云。
export interface Recording {
  blob: Blob;
  durationSeconds: number;
  mimeType: string;
}

export class Recorder {
  private mr: MediaRecorder | null = null;
  private stream: MediaStream | null = null;
  private chunks: Blob[] = [];
  private startedAt = 0;

  get active(): boolean {
    return this.mr?.state === "recording";
  }

  async start(): Promise<void> {
    if (this.active) return;
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    this.chunks = [];
    const mimeType = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
    this.mr = new MediaRecorder(this.stream, mimeType ? { mimeType } : undefined);
    this.mr.ondataavailable = (e) => {
      if (e.data.size > 0) this.chunks.push(e.data);
    };
    this.startedAt = performance.now();
    this.mr.start();
  }

  async stop(): Promise<Recording> {
    const mr = this.mr;
    if (!mr) throw new Error("未在录音");
    const durationSeconds = (performance.now() - this.startedAt) / 1000;
    const done = new Promise<Blob>((resolve) => {
      mr.onstop = () => resolve(new Blob(this.chunks, { type: mr.mimeType || "audio/webm" }));
    });
    mr.stop();
    const blob = await done;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    this.mr = null;
    return { blob, durationSeconds, mimeType: blob.type };
  }
}
