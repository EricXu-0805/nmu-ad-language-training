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
  private startingP: Promise<boolean> | null = null;
  // getUserMedia 本身不可可靠 abort；每次启动捕获代际，暂停/断线/超时只需推进代际。
  // 若权限流晚到，旧代际会在创建 MediaRecorder 前物理关掉全部 track。
  private startGeneration = 0;
  private disposed = false;

  get active(): boolean {
    return this.mr?.state === "recording";
  }

  get pending(): boolean {
    return this.startingP !== null;
  }

  async start(): Promise<boolean> {
    if (this.active) return true;
    if (this.disposed) return false;
    if (this.startingP) return this.startingP;
    const generation = ++this.startGeneration;
    this.startingP = (async () => {
      let acquired: MediaStream | null = null;
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        acquired = stream;
        if (this.disposed || generation !== this.startGeneration) {
          // 组件卸载、暂停、断线或超时后才拿到流：立即物理关麦，绝不留无主热麦。
          stream.getTracks().forEach((t) => t.stop());
          return false;
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
        return true;
      } catch (error) {
        // 构造/启动 MediaRecorder 也可能失败；getUserMedia 已成功时必须在抛错前关掉流。
        acquired?.getTracks().forEach((t) => t.stop());
        if (this.stream === acquired) this.stream = null;
        this.mr = null;
        this.chunks = [];
        this.startedAt = 0;
        throw error;
      } finally {
        this.startingP = null;
      }
    })();
    return this.startingP;
  }

  // 使当前 getUserMedia 请求失效。请求仍可能由浏览器晚到，但 start() 会立刻关掉其 track。
  cancelPendingStart(): void {
    this.startGeneration += 1;
  }

  // 启动刚落地但调用方的许可已过期时，不生成录音资产，直接物理关麦并丢弃空片段。
  discardActive(): void {
    this.startGeneration += 1;
    const mr = this.mr;
    this.mr = null;
    if (mr && mr.state !== "inactive") {
      mr.ondataavailable = null;
      mr.onstop = null;
      try { mr.stop(); } catch { /* 设备已自行停止 */ }
    }
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    this.chunks = [];
    this.startedAt = 0;
  }

  // 卸载兜底:在途的 getUserMedia 一旦落地立即关闭;已录中的由调用方 stopAndSave 收尾。
  dispose(): void {
    this.disposed = true;
    this.cancelPendingStart();
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
    this.chunks = [];
    this.startedAt = 0;
    return { blob, durationSeconds, mimeType: blob.type };
  }
}
