// SessionBus = BroadcastChannel('nmu-session') + localStorage 镜像。
// 实时靠 channel;老人窗刷新/迟到订阅靠镜像立即恢复到当前 session/cursor/rapportStep。
// 这是同机双窗的低延迟快路径；跨设备与可恢复真值另由同源后端同步。
import {
  parseSyncMsg,
  type CursorMsg,
  type RapportMsg,
  type SessionMsg,
  type SyncMsg,
} from "./messages";

const CHANNEL = "nmu-session";
const K_SESSION = "nmu:session";
const K_CURSOR = "nmu:cursor";
const K_RAPPORT = "nmu:rapport";
const K_REDUCTION = "nmu:reduction-signal";

export interface BusSnapshot {
  session?: SessionMsg;
  cursor?: CursorMsg;
  rapportStep?: RapportMsg;
}

function readMirror(key: string, type: "session"): SessionMsg | undefined;
function readMirror(key: string, type: "cursor"): CursorMsg | undefined;
function readMirror(key: string, type: "rapportStep"): RapportMsg | undefined;
function readMirror(key: string, type: SyncMsg["type"]): SyncMsg | undefined {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return undefined;
    const parsed = parseSyncMsg(JSON.parse(raw) as unknown);
    return parsed?.type === type ? parsed : undefined;
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
  private reductionNonce = 0;

  constructor() {
    if (typeof window === "undefined") return;
    window.addEventListener("storage", (event) => {
      if (this.ch !== null || event.key !== K_REDUCTION || !event.newValue) return;
      try {
        const envelope = JSON.parse(event.newValue) as unknown;
        if (envelope === null || typeof envelope !== "object" || Array.isArray(envelope)) return;
        const row = envelope as Record<string, unknown>;
        if (Object.keys(row).length !== 2 || !Object.hasOwn(row, "nonce")
            || !Object.hasOwn(row, "message") || typeof row.nonce !== "string") return;
        const parsed = parseSyncMsg(row.message);
        if (parsed?.type !== "safetyStop" && parsed?.type !== "patientPauseStop") return;
        this.local.forEach((handler) => handler(parsed));
      } catch { /* malformed or blocked storage remains fail-closed */ }
    });
  }

  post(msg: SyncMsg): void {
    // 即使发件方是本地 TypeScript，也在跨窗边界再做一次 runtime 校验。
    // any/旧缓存/被注入页面不能把额外字段或非有限数送进状态机。
    const safe = parseSyncMsg(msg);
    if (!safe) return;
    // 仅持久化权威可恢复投影；audioSaved 与减权 safetyStop 都是瞬时事件，不进镜像。
    if (safe.type === "session") localStorage.setItem(K_SESSION, JSON.stringify(safe));
    else if (safe.type === "cursor") localStorage.setItem(K_CURSOR, JSON.stringify(safe));
    else if (safe.type === "rapportStep") localStorage.setItem(K_RAPPORT, JSON.stringify(safe));
    if (this.ch) {
      this.ch.postMessage(safe);
    } else if (safe.type === "safetyStop" || safe.type === "patientPauseStop") {
      // BroadcastChannel 不可用时，localStorage 只做瞬时唤醒。不保留减权信号，
      // 避免研究者明确恢复后旧消息又停一次；服务端投影仍是唯一真值。
      try {
        const nonce = `${Date.now()}-${this.reductionNonce += 1}`;
        localStorage.setItem(K_REDUCTION, JSON.stringify({ nonce, message: safe }));
        localStorage.removeItem(K_REDUCTION);
      } catch { /* sender still performs its synchronous local stop below */ }
    }
    // 微任务投递:订阅方 setState 不在发送方调用栈里重入。
    queueMicrotask(() => this.local.forEach((h) => h(safe)));
  }

  subscribe(handler: (msg: SyncMsg) => void): () => void {
    this.local.add(handler);
    const listener = (e: MessageEvent) => {
      const parsed = parseSyncMsg(e.data);
      if (parsed) handler(parsed);
    };
    this.ch?.addEventListener("message", listener);
    return () => {
      this.local.delete(handler);
      this.ch?.removeEventListener("message", listener);
    };
  }

  snapshot(): BusSnapshot {
    return {
      session: readMirror(K_SESSION, "session"),
      cursor: readMirror(K_CURSOR, "cursor"),
      rapportStep: readMirror(K_RAPPORT, "rapportStep"),
    };
  }

  // Mirror a server-authoritative patient projection without broadcasting it as
  // a new console command.  Missing fields are removed, so refresh cannot revive
  // an old session/cursor after auth loss, server reset, or a session switch.
  replaceSnapshot(snapshot: BusSnapshot): void {
    const session = snapshot.session;
    const cursor = session && snapshot.cursor?.sessionId === session.sessionId
      ? snapshot.cursor : undefined;
    const rapport = session && snapshot.rapportStep?.sessionId === session.sessionId
      ? snapshot.rapportStep : undefined;
    if (session) localStorage.setItem(K_SESSION, JSON.stringify(session));
    else localStorage.removeItem(K_SESSION);
    if (cursor) localStorage.setItem(K_CURSOR, JSON.stringify(cursor));
    else localStorage.removeItem(K_CURSOR);
    if (rapport) localStorage.setItem(K_RAPPORT, JSON.stringify(rapport));
    else localStorage.removeItem(K_RAPPORT);
  }

  // 新场次开始前清掉旧镜像,避免老人端串到上一场。
  reset(): void {
    this.replaceSnapshot({});
  }
}

export const bus = new SessionBus();
