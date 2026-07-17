// 跨端同步消息 —— 操作端为【唯一写者】,消息只带索引/级别指针,绝不带题目文本。
// 老人端凭 (itemIdx,turnIdx,cueLevel) 自己去版本锁定的 plan/bundle 查逐字文本,保证单一内容源。

export type RecState = "idle" | "armed" | "recording" | "stopped";
export type CueLevel = 0 | 1 | 2 | 3;
export type PatientScreen = "idle" | "present" | "record" | "thanks" | "paused" | "done";

// wseq:写者(操作端)单调写序号。老人端双源(bus 秒推 + 轮询快照)竞态时,
// 凭它丢弃迟到的旧快照,防止画面被顶回上一题/上一级线索(对认知障碍老人是最糟的跳变)。
export type SyncMsg =
  | { type: "session"; sessionId: string; weekNo: number; eventLine: string; mode: "task" | "rapport"; itemBankVersionId: string; paused?: boolean; wseq?: number }
  // recSeq:每次 arm 递增。armed→armed 重发(老人自停后再示意)靠它触发老人端 effect;无它则依赖值不变、麦克风永不重开。
  // selfStart:操作端按录音资格(recording_allowed)判定后下发——老人端只有收到 true 才显示
  // "点这里,开始回答"自助开录按钮。缺省/false 一律不显示(fail-closed:合规闸门不被老人端绕过)。
  // fbKey/fbItemId/fbSeq:自动驾驶反馈——不载文本,只指向题库/协议固定话术,老人端本地查表回填(fbSeq 变化才重读)。
  | { type: "cursor"; sessionId: string; screen: PatientScreen; itemIdx: number; turnIdx: number; responseRole: string; cueLevel: CueLevel; recording: RecState; recSeq?: number; rawAudioId?: string; selfStart?: boolean; fbKey?: string; fbItemId?: string; fbSeq?: number; wseq?: number }
  | { type: "rapportStep"; sessionId: string; sectionKey: string; questionIdx: number; recording: RecState; recSeq?: number; rawAudioId?: string; assentGate?: boolean; containsDirectIdentifier?: boolean; paused?: boolean; wseq?: number }
  // sessionId:操作端凭它丢弃跨场次的迟到/残留回报(live state 里 audioSaved 存到下次握手才清)。
  | { type: "audioSaved"; rawAudioId: string; durationSeconds: number; turnKey: string; sessionId?: string; containsDirectIdentifier?: boolean }
  // 老人端麦克风真值上报(自助开录时操作端唯一的感知渠道;也用于示意录音的开麦确认)。
  // 这是"上报"不是"显示状态"——老人端仍只读游标,写者规则不变(audioSaved/patientRec 两类上报除外)。
  | { type: "patientRec"; active: boolean; turnKey: string; sessionId: string };

export type SessionMsg = Extract<SyncMsg, { type: "session" }>;
export type CursorMsg = Extract<SyncMsg, { type: "cursor" }>;
export type RapportMsg = Extract<SyncMsg, { type: "rapportStep" }>;
export type AudioSavedMsg = Extract<SyncMsg, { type: "audioSaved" }>;
export type PatientRecMsg = Extract<SyncMsg, { type: "patientRec" }>;

// 单机一条流的窗内事件(非跨窗总线):操作端 ⇄ App 路由层叠层宿主。
// 状态源在 ConsoleShell;红线守卫禁止 console/** import 老人端源码,故经事件解耦。
export const PATIENT_VIEW_EVENT = "nmu:patient-view";           // detail: { open: boolean }
export const PATIENT_VIEW_EXIT_EVENT = "nmu:patient-view-exit"; // 宿主按住返回 → 操作端收
export const PATIENT_VIEW_REC_EVENT = "nmu:patient-view-rec";   // detail: { active } 录音真值→宿主退出钮
export const CONSOLE_NOTE_EVENT = "nmu:console-note";           // detail: { count } 叠层期间暂存的提示数
