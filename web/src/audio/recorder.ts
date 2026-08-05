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

/**
 * 冻结状态机。两阶段的意义全在 prepared 与 recording 之间那条线：权限已经拿到、
 * 设备还没开录，调用方可以在这里做完第二次服务端授权再决定开不开麦。
 */
export type RecorderState =
  | "idle" | "preparing" | "prepared" | "starting" | "recording" | "stopping" | "disposed";

export class Recorder {
  private mr: MediaRecorder | null = null;
  private stream: MediaStream | null = null;
  private chunks: Blob[] = [];
  // null = 尚未真实开录。绝不能拿 0 当哨兵：performance.now() 恰好读到 0
  // （页面时间原点那一刻）是合法起点，被当成"没开始"会让整条采集莫名失败。
  private startedAt: number | null = null;
  // 单飞:getUserMedia 在途期间(权限弹窗可悬很久)再次 prepare 必须共用同一次,
  // 否则并发拿到两条 MediaStream,先到的那条被覆盖成无主热麦——绝不允许。
  private preparingP: Promise<boolean> | null = null;
  private startingP: Promise<boolean> | null = null;
  private stoppingP: Promise<Recording> | null = null;
  private rejectStarting: ((reason?: unknown) => void) | null = null;
  private rejectStopping: ((reason?: unknown) => void) | null = null;
  // getUserMedia 本身不可可靠 abort；每次启动捕获代际，暂停/断线/超时只需推进代际。
  // 若权限流晚到，旧代际会在创建 MediaRecorder 前物理关掉全部 track。
  private startGeneration = 0;
  private stateValue: RecorderState = "idle";
  // dispose 落在 recording/stopping 上时不能就地拆设备，只能记下诉求，
  // 等这条捕获自己收口后再完成。
  private disposeRequested = false;

  get state(): RecorderState { return this.stateValue; }

  get active(): boolean {
    return this.stateValue === "recording";
  }

  get pending(): boolean {
    return this.preparingP !== null || this.startingP !== null;
  }

  get stopping(): boolean {
    return this.stateValue === "stopping";
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

  /**
   * 只取流、只造 recorder。绝不调用 `MediaRecorder.start()`：调用方要在这之后
   * 再做一次服务端授权，那次授权没过就不该产生任何录音字节。
   */
  prepare(): Promise<boolean> {
    if (this.stateValue === "disposed") return Promise.resolve(false);
    if (this.stateValue === "prepared" || this.stateValue === "recording") {
      return Promise.resolve(true);
    }
    if (this.preparingP) return this.preparingP;
    if (this.stateValue !== "idle") {
      return Promise.reject(new Error("录音器当前状态不允许准备麦克风"));
    }
    const generation = ++this.startGeneration;
    this.stateValue = "preparing";
    this.preparingP = (async () => {
      let acquired: MediaStream | null = null;
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        acquired = stream;
        if (this.stateValue === "disposed" || generation !== this.startGeneration) {
          // 组件卸载、暂停、断线或超时后才拿到流：立即物理关麦，绝不留无主热麦。
          releaseMediaStreamTracks(stream);
          if (this.stateValue === "preparing") this.stateValue = "idle";
          return false;
        }
        this.stream = stream;
        this.chunks = [];
        const mimeType = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
        this.mr = new MediaRecorder(this.stream, mimeType ? { mimeType } : undefined);
        this.mr.ondataavailable = (e) => {
          if (e.data.size > 0) this.chunks.push(e.data);
        };
        this.stateValue = "prepared";
        return true;
      } catch (error) {
        // 构造 MediaRecorder 也可能失败；getUserMedia 已成功时必须在抛错前关掉流。
        // 先断引用再关设备：关的过程抛错也不会留下一个指着死流的字段。
        if (this.stream === acquired) this.stream = null;
        this.mr = null;
        this.chunks = [];
        this.startedAt = null;
        if (this.stateValue === "preparing") this.stateValue = "idle";
        releaseMediaStreamTracks(acquired);
        throw error;
      } finally {
        this.preparingP = null;
      }
    })();
    return this.preparingP;
  }

  /**
   * 只接受当前代际已准备好的流。本方法在返回 Promise 之前**同步**下达开录指令，
   * 中间一个 `await` 都没有——否则页面隐藏或权限撤销还能挤进"已允许开录"与
   * "真的开录"之间。返回的 Promise 只等真实 `onstart`，单调起点绑的就是那个事件。
   */
  startPrepared(): Promise<boolean> {
    if (this.stateValue === "disposed") return Promise.resolve(false);
    if (this.stateValue === "recording") return Promise.resolve(true);
    if (this.startingP) return this.startingP;
    const recorder = this.mr;
    if (this.stateValue !== "prepared" || !recorder) {
      return Promise.reject(new Error("录音器尚未准备好，拒绝开录"));
    }
    const generation = this.startGeneration;
    this.stateValue = "starting";
    let rejectStart!: (reason?: unknown) => void;
    const started = new Promise<boolean>((resolve, reject) => {
      rejectStart = reject;
      this.rejectStarting = reject;
      recorder.onstart = () => {
        this.rejectStarting = null;
        recorder.onstart = null;
        recorder.onerror = null;
        if (this.stateValue !== "starting" || generation !== this.startGeneration) {
          // 迟到的 onstart：这条捕获已经被丢弃，绝不能把它复活成 recording。
          resolve(false);
          return;
        }
        this.startedAt = performance.now();
        this.stateValue = "recording";
        resolve(true);
      };
      recorder.onerror = (event) => {
        this.rejectStarting = null;
        recorder.onstart = null;
        recorder.onerror = null;
        if (this.stateValue === "starting") this.stateValue = "prepared";
        reject((event as ErrorEvent).error ?? new Error("录音设备启动失败"));
      };
    });
    let guarded!: Promise<boolean>;
    guarded = started.finally(() => {
      if (this.startingP === guarded) this.startingP = null;
    });
    this.startingP = guarded;
    try {
      recorder.start();
    } catch (error) {
      this.rejectStarting = null;
      recorder.onstart = null;
      recorder.onerror = null;
      if (this.stateValue === "starting") this.stateValue = "prepared";
      rejectStart(error);
    }
    return guarded;
  }

  /**
   * legacy/generic 调用者的向后兼容包装：一次调用完成开麦，外部语义不变。
   *
   * prepare() 之后**不再**重复判 disposed：拿流期间被 dispose 的那条流由
   * prepare() 自己关掉并返回 false，流已到手之后才 dispose 的则由
   * startPrepared() 挡下。多加一次比较既拦不到新东西，也会让人误以为
   * 这里才是最后一道闸——真正的 fail-closed 闸只有 startPrepared()。
   */
  async start(): Promise<boolean> {
    if (this.active) return true;
    if (this.stateValue === "disposed") return false;
    if (!await this.prepare()) return false;
    return this.startPrepared();
  }

  // 使当前 getUserMedia 请求失效。请求仍可能由浏览器晚到，但 prepare() 会立刻关掉其 track。
  cancelPendingStart(): void {
    if (this.stateValue === "stopping") return;
    this.startGeneration += 1;
    this.rejectStarting?.(new DOMException("录音启动已取消", "AbortError"));
    this.rejectStarting = null;
  }

  /**
   * 启动刚落地但调用方的许可已过期时，不生成录音资产，直接物理关麦并丢弃空片段。
   *
   * 已经进入 stopping 的捕获默认不动：成功停止一旦同步调用，chunks、`onstop`
   * 和那条 stop Promise 就是这次采集唯一的收口路径，生命周期清理只能 join 它。
   * `force` 留给控制器自己的收尾超时——那条路径必须能物理拆掉悬死的设备。
   */
  discardActive(options: { force?: boolean } = {}): void {
    if (this.stateValue === "disposed") return;
    if (this.stateValue === "stopping" && !options.force) return;
    this.startGeneration += 1;
    const mr = this.mr;
    const stream = this.stream;
    // 先把字段全部断开，再去碰设备：设备层抛错也不会留下半清理的状态。
    this.mr = null;
    this.stream = null;
    this.chunks = [];
    this.startedAt = null;
    this.stateValue = this.disposeRequested ? "disposed" : "idle";
    this.rejectStarting?.(new DOMException("录音启动已取消", "AbortError"));
    this.rejectStarting = null;
    this.rejectStopping?.(new DOMException("录音收尾已强制取消", "AbortError"));
    this.rejectStopping = null;
    if (mr && mr.state !== "inactive") {
      mr.ondataavailable = null;
      mr.onstop = null;
      try { mr.stop(); } catch { /* 设备已自行停止 */ }
    }
    releaseMediaStreamTracks(stream);
  }

  // 卸载兜底:在途的 getUserMedia 一旦落地立即关闭;已录中/收尾中的由那条链自己收口。
  dispose(): void {
    this.disposeRequested = true;
    if (this.stateValue === "stopping" || this.stateValue === "recording") return;
    this.cancelPendingStart();
    const stream = this.stream;
    const mr = this.mr;
    this.stream = null;
    this.mr = null;
    this.chunks = [];
    this.startedAt = null;
    this.stateValue = "disposed";
    if (mr && mr.state !== "inactive") {
      mr.ondataavailable = null;
      mr.onstop = null;
      try { mr.stop(); } catch { /* 设备已自行停止 */ }
    }
    releaseMediaStreamTracks(stream);
  }

  async stop(): Promise<Recording> {
    if (this.stoppingP) return this.stoppingP;
    const mr = this.mr;
    if (!mr || this.stateValue !== "recording") throw new Error("未在录音");
    const startedAt = this.startedAt;
    // 没有真实起点就没有可上报的时长。宁可让这次采集失败，也不拿 0 当起点
    // 凭空造一个"从页面打开算起"的时长。
    if (startedAt === null) throw new Error("录音缺少真实起点时刻");
    this.stateValue = "stopping";
    const durationSeconds = (performance.now() - startedAt) / 1000;
    const done = new Promise<Blob>((resolve, reject) => {
      this.rejectStopping = reject;
      mr.onstop = () => {
        this.rejectStopping = null;
        resolve(new Blob(this.chunks, { type: mr.mimeType || "audio/webm" }));
      };
    });
    this.stoppingP = (async () => {
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
        this.stoppingP = null;
        this.stateValue = this.disposeRequested ? "disposed" : "idle";
        releaseMediaStreamTracks(stream);
      }
    })();
    return this.stoppingP;
  }
}
