// SessionBus = BroadcastChannel('nmu-session') + localStorage 镜像。
// 实时靠 channel;老人窗刷新/迟到订阅靠镜像立即恢复到当前 session/cursor/rapportStep。
// 全本地、同源、无网络往返、无轮询(v1 同机双窗)。
import type { CursorMsg, RapportMsg, SessionMsg, SyncMsg } from "./messages";

const CHANNEL = "nmu-session";
const K_SESSION = "nmu:session";
const K_CURSOR = "nmu:cursor";
const K_RAPPORT = "nmu:rapport";

export interface BusSnapshot {
  session?: SessionMsg;
  cursor?: CursorMsg;
  rapportStep?: RapportMsg;
}

function readMirror<T>(key: string): T | undefined {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : undefined;
  } catch {
    return undefined;
  }
}

class SessionBus {
  private ch: BroadcastChannel | null =
    typeof BroadcastChannel !== "undefined" ? new BroadcastChannel(CHANNEL) : null;
  // BroadcastChannel 不把消息回投给发出它的同一个实例,而单机一条流(受试者画面
  // 与操作端同页叠层)靠这条总线双向即时送达 cursor/patientRec——故本页订阅者另行直投。
  private local = new Set<(msg: SyncMsg) => void>();

  post(msg: SyncMsg): void {
    // 持久状态镜像到 localStorage(session/cursor/rapportStep);audioSaved 是瞬时事件不镜像。
    if (msg.type === "session") localStorage.setItem(K_SESSION, JSON.stringify(msg));
    else if (msg.type === "cursor") localStorage.setItem(K_CURSOR, JSON.stringify(msg));
    else if (msg.type === "rapportStep") localStorage.setItem(K_RAPPORT, JSON.stringify(msg));
    this.ch?.postMessage(msg);
    // 微任务投递:订阅方 setState 不在发送方调用栈里重入。
    queueMicrotask(() => this.local.forEach((h) => h(msg)));
  }

  subscribe(handler: (msg: SyncMsg) => void): () => void {
    this.local.add(handler);
    const listener = (e: MessageEvent) => handler(e.data as SyncMsg);
    this.ch?.addEventListener("message", listener);
    return () => {
      this.local.delete(handler);
      this.ch?.removeEventListener("message", listener);
    };
  }

  snapshot(): BusSnapshot {
    return {
      session: readMirror<SessionMsg>(K_SESSION),
      cursor: readMirror<CursorMsg>(K_CURSOR),
      rapportStep: readMirror<RapportMsg>(K_RAPPORT),
    };
  }

  // 新场次开始前清掉旧镜像,避免老人端串到上一场。
  reset(): void {
    localStorage.removeItem(K_SESSION);
    localStorage.removeItem(K_CURSOR);
    localStorage.removeItem(K_RAPPORT);
  }
}

export const bus = new SessionBus();
