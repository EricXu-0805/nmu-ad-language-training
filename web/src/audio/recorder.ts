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
  // 单飞:getUserMedia 在途期间(权限弹窗可悬很久)再次 start 必须共用同一次,
  // 否则并发拿到两条 MediaStream,先到的那条被覆盖成无主热麦——绝不允许。
  private startingP: Promise<void> | null = null;
  private disposed = false;

  get active(): boolean {
    return this.mr?.state === "recording";
  }

  get pending(): boolean {
    return this.startingP !== null;
  }

  async start(): Promise<void> {
    if (this.active) return;
    if (this.startingP) return this.startingP;
    this.startingP = (async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        if (this.disposed) {
          // 组件已卸载才拿到流:立即物理关麦,绝不留无主热麦
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        this.stream = stream;
        this.chunks = [];
        const mimeType = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
        this.mr = new MediaRecorder(this.stream, mimeType ? { mimeType } : undefined);
        this.mr.ondataavailable = (e) => {
          if (e.data.size > 0) this.chunks.push(e.data);
        };
        this.startedAt = performance.now();
        this.mr.start();
      } finally {
        this.startingP = null;
      }
    })();
    return this.startingP;
  }

  // 卸载兜底:在途的 getUserMedia 一旦落地立即关闭;已录中的由调用方 stopAndSave 收尾。
  dispose(): void {
    this.disposed = true;
    if (!this.active) {
      this.stream?.getTracks().forEach((t) => t.stop());
      this.stream = null;
      this.mr = null;
    }
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
