// MediaRecorder 封装。仅在 secure context(localhost/https)可用。
// 录音先保存在设备侧并上传同源后端；启用云 ASR 时后端会把待转写音频交第三方处理。
export interface Recording {
  blob: Blob;
  durationSeconds: number;
  mimeType: string;
}

/**
 * 逐条尽力关掉 track。getTracks() 本身也可能抛（设备层已被系统回收），单条
 * track.stop() 失败更不能拖累其余的——留一条没关掉的热麦是最坏结果。
 */
export function releaseMediaStreamTracks(stream: MediaStream | null): void {
  if (!stream) return;
  let tracks: MediaStreamTrack[] = [];
  try { tracks = stream.getTracks(); } catch { return; }
  for (const track of tracks) {
    try { track.stop(); } catch { /* 这条关不掉，继续关下一条 */ }
  }
}

export class Recorder {
  private mr: MediaRecorder | null = null;
  private stream: MediaStream | null = null;
  private chunks: Blob[] = [];
  // null = 尚未真实开录。绝不能拿 0 当哨兵：performance.now() 恰好读到 0
  // （页面时间原点那一刻）是合法起点，被当成"没开始"会让整条采集莫名失败。
  private startedAt: number | null = null;
  // 单飞:getUserMedia 在途期间(权限弹窗可悬很久)再次 start 必须共用同一次,
  // 否则并发拿到两条 MediaStream,先到的那条被覆盖成无主热麦——绝不允许。
  private startingP: Promise<boolean> | null = null;
  private rejectStarting: ((reason?: unknown) => void) | null = null;
  private rejectStopping: ((reason?: unknown) => void) | null = null;
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

  get stopping(): boolean {
    return this.rejectStopping !== null;
  }

  /** Exact MediaRecorder-selected MIME after a real start event. */
  get mimeType(): string | null {
    const value = this.mr?.mimeType;
    return typeof value === "string" && value ? value : null;
  }

  /**
   * 真实录音起点的 performance.now() 读数；没有在录时为 null。
   * stop() 上报的时长就是从这一刻算起，调用方的截止时刻也必须以它为基准，
   * 不能拿"授权返回""定时器武装"这些更晚的时刻当起点。
   */
  get startedAtMs(): number | null {
    return this.startedAt;
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
          releaseMediaStreamTracks(stream);
          return false;
        }
        this.stream = stream;
        this.chunks = [];
        const mimeType = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
        this.mr = new MediaRecorder(this.stream, mimeType ? { mimeType } : undefined);
        this.mr.ondataavailable = (e) => {
          if (e.data.size > 0) this.chunks.push(e.data);
        };
        await new Promise<void>((resolve, reject) => {
          const recorder = this.mr as MediaRecorder;
          this.rejectStarting = reject;
          recorder.onstart = () => {
            this.rejectStarting = null;
            recorder.onstart = null;
            recorder.onerror = null;
            resolve();
          };
          recorder.onerror = (event) => {
            this.rejectStarting = null;
            recorder.onstart = null;
            recorder.onerror = null;
            reject((event as ErrorEvent).error ?? new Error("录音设备启动失败"));
          };
          // 计时起点紧贴下达开录指令的那一刻：往前挪会把构造/权限耗时算进时长，
          // 挪到 onstart 之后又会漏掉已经进了 blob 的开头那几十毫秒。
          this.startedAt = performance.now();
          recorder.start();
        });
        return true;
      } catch (error) {
        // 构造/启动 MediaRecorder 也可能失败；getUserMedia 已成功时必须在抛错前关掉流。
        // 先断引用再关设备：关的过程抛错也不会留下一个指着死流的字段。
        if (this.stream === acquired) this.stream = null;
        this.mr = null;
        this.chunks = [];
        this.startedAt = null;
        releaseMediaStreamTracks(acquired);
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
    this.rejectStarting?.(new DOMException("录音启动已取消", "AbortError"));
    this.rejectStarting = null;
  }

  // 启动刚落地但调用方的许可已过期时，不生成录音资产，直接物理关麦并丢弃空片段。
  discardActive(): void {
    this.startGeneration += 1;
    const mr = this.mr;
    const stream = this.stream;
    // 先把字段全部断开，再去碰设备：设备层抛错也不会留下半清理的状态。
    this.mr = null;
    this.stream = null;
    this.chunks = [];
    this.startedAt = null;
    this.rejectStopping?.(new DOMException("录音收尾已强制取消", "AbortError"));
    this.rejectStopping = null;
    if (mr && mr.state !== "inactive") {
      mr.ondataavailable = null;
      mr.onstop = null;
      try { mr.stop(); } catch { /* 设备已自行停止 */ }
    }
    releaseMediaStreamTracks(stream);
  }

  // 卸载兜底:在途的 getUserMedia 一旦落地立即关闭;已录中的由调用方 stopAndSave 收尾。
  dispose(): void {
    this.disposed = true;
    this.cancelPendingStart();
    if (!this.active) {
      const stream = this.stream;
      this.stream = null;
      this.mr = null;
      releaseMediaStreamTracks(stream);
    }
  }

  async stop(): Promise<Recording> {
    const mr = this.mr;
    if (!mr) throw new Error("未在录音");
    const startedAt = this.startedAt;
    // 没有真实起点就没有可上报的时长。宁可让这次采集失败，也不拿 0 当起点
    // 凭空造一个"从页面打开算起"的时长。
    if (startedAt === null) throw new Error("录音缺少真实起点时刻");
    const durationSeconds = (performance.now() - startedAt) / 1000;
    const done = new Promise<Blob>((resolve, reject) => {
      this.rejectStopping = reject;
      mr.onstop = () => {
        this.rejectStopping = null;
        resolve(new Blob(this.chunks, { type: mr.mimeType || "audio/webm" }));
      };
    });
    try {
      mr.stop();
      const blob = await done;
      return { blob, durationSeconds, mimeType: blob.type };
    } finally {
      this.rejectStopping = null;
      const stream = this.stream;
      this.stream = null;
      this.mr = null;
      this.chunks = [];
      this.startedAt = null;
      releaseMediaStreamTracks(stream);
    }
  }
}
