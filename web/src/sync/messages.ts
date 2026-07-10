// 跨端同步消息 —— 操作端为【唯一写者】,消息只带索引/级别指针,绝不带题目文本。
// 老人端凭 (itemIdx,turnIdx,cueLevel) 自己去版本锁定的 plan/bundle 查逐字文本,保证单一内容源。

export type RecState = "idle" | "armed" | "recording" | "stopped";
export type CueLevel = 0 | 1 | 2 | 3;
export type PatientScreen = "idle" | "present" | "record" | "thanks" | "done";

// wseq:写者(操作端)单调写序号。老人端双源(bus 秒推 + 轮询快照)竞态时,
// 凭它丢弃迟到的旧快照,防止画面被顶回上一题/上一级线索(对认知障碍老人是最糟的跳变)。
export type SyncMsg =
  | { type: "session"; sessionId: string; weekNo: number; eventLine: string; mode: "task" | "rapport"; itemBankVersionId: string; wseq?: number }
  // recSeq:每次 arm 递增。armed→armed 重发(老人自停后再示意)靠它触发老人端 effect;无它则依赖值不变、麦克风永不重开。
  | { type: "cursor"; screen: PatientScreen; itemIdx: number; turnIdx: number; responseRole: string; cueLevel: CueLevel; recording: RecState; recSeq?: number; rawAudioId?: string; wseq?: number }
  | { type: "rapportStep"; sectionKey: string; questionIdx: number; recording: RecState; recSeq?: number; rawAudioId?: string; assentGate?: boolean; containsDirectIdentifier?: boolean; wseq?: number }
  | { type: "audioSaved"; rawAudioId: string; durationSeconds: number; turnKey: string; containsDirectIdentifier?: boolean };

export type SessionMsg = Extract<SyncMsg, { type: "session" }>;
export type CursorMsg = Extract<SyncMsg, { type: "cursor" }>;
export type RapportMsg = Extract<SyncMsg, { type: "rapportStep" }>;
export type AudioSavedMsg = Extract<SyncMsg, { type: "audioSaved" }>;
