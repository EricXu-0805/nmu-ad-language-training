// 操作端作业日志：localStorage 保留未提交编辑与快路径，GET /sessions/{sid}/journal
// 负责从服务器权威重建已提交 item/turn/audio；两者合并后支持刷新或换研究者设备继续。
import { useCallback, useEffect, useState } from "react";
import { turnKey } from "../lib/ids";
import type { AudioAsset, ItemEvent, Session, TurnEvent } from "../types";

export interface JournalItem { itemEventId: number; taskType: string; imageId: string | null }
export interface JournalTurn {
  turnId: number; responseRole: string;
  asrSaved: boolean; confirmed: boolean; aiJudged: boolean; locked: boolean;
  elementValue?: number; promptLevel?: number;
  asrText?: string; confirmedText?: string;   // 缓存文本,供回访环节回填(后端无按 turn 回读)
  cueLevel?: number;                          // 已发线索级:回访环节恢复,不清老人端线索、prompt_level 建议不记错
}
export interface JournalAudio {
  turnKey: string; containsDirectIdentifier: boolean; isReliabilitySample: boolean; lastStatus: string;
  durationSeconds?: number;                   // 刷新后凭 turnKey 反查恢复回放条
}
export interface SessionJournal {
  sessionId: string;
  itemEvents: Record<string, JournalItem>;
  turns: Record<string, JournalTurn>;
  audios: Record<string, JournalAudio>;
  cueLevels?: Record<string, number>;  // turnKey → 已发线索级(turn 未建时也要能记,故独立于 turns)
  cursor: { itemIdx: number; turnIdx: number };
}

export interface ServerSessionJournal {
  session: Session;
  items: ItemEvent[];
  turns: TurnEvent[];
  audios: AudioAsset[];
}

// 旧后端既无 AudioAsset.turn_key、又可能缺关联 TurnEvent。不能因此把服务端音频从
// 收尾闸门中丢掉；用内部占位键保留，但绝不把它当成训练环节回放映射。
export const UNBOUND_AUDIO_TURN_KEY_PREFIX = "__unbound_audio__:";
export const isBoundJournalTurnKey = (value: unknown): value is string =>
  typeof value === "string" && value.length > 0 && !value.startsWith(UNBOUND_AUDIO_TURN_KEY_PREFIX);

const key = (sid: string) => `nmu:journal:${sid}`;

function load(sid: string): SessionJournal {
  try {
    const raw = localStorage.getItem(key(sid));
    if (raw) return JSON.parse(raw) as SessionJournal;
  } catch { /* 损坏则重建 */ }
  return { sessionId: sid, itemEvents: {}, turns: {}, audios: {}, cursor: { itemIdx: 0, turnIdx: 0 } };
}

// 服务端是已提交研究记录的真值，本机 journal 仍保留未提交的转写草稿、线索级和回放映射。
// 合并只升级完成状态，绝不因一次旧快照把本机已知的 locked/confirmed 降回 false。
function mergeServerJournal(local: SessionJournal, remote: ServerSessionJournal): SessionJournal {
  const items = { ...local.itemEvents };
  const itemByDbId = new Map<number, ItemEvent>();
  for (const item of remote.items) {
    itemByDbId.set(item.id, item);
    items[item.item_id] = {
      itemEventId: item.id,
      taskType: item.task_type,
      imageId: item.image_id ?? null,
    };
  }

  const turns = { ...local.turns };
  const remoteTurnKey = new Map<number, string>();
  for (const turn of remote.turns) {
    const item = itemByDbId.get(turn.item_event_id);
    if (!item) continue;
    const k = turnKey(item.item_id, turn.turn_seq);
    remoteTurnKey.set(turn.id, k);
    const prev = turns[k];
    const confirmedText = turn.confirmed_response_text ?? prev?.confirmedText;
    const promptLevel = turn.prompt_level ?? prev?.promptLevel;
    turns[k] = {
      turnId: turn.id,
      responseRole: turn.response_role ?? prev?.responseRole ?? "",
      asrSaved: true,
      confirmed: Boolean(confirmedText?.trim()) || prev?.confirmed === true,
      aiJudged: turn.ai_answer_type != null || turn.ai_score != null || prev?.aiJudged === true,
      locked: turn.score_locked || prev?.locked === true,
      elementValue: turn.element_value ?? prev?.elementValue,
      promptLevel,
      asrText: turn.asr_text ?? prev?.asrText,
      confirmedText,
      cueLevel: Math.max(prev?.cueLevel ?? 0, promptLevel ?? 0),
    };
  }

  const audios = { ...local.audios };
  const turnByAudioId = new Map<string, TurnEvent>();
  for (const turn of remote.turns) {
    if (turn.raw_audio_id) turnByAudioId.set(turn.raw_audio_id, turn);
  }
  for (const audio of remote.audios) {
    const turn = turnByAudioId.get(audio.raw_audio_id);
    const suppliedTurnKey = typeof audio.turn_key === "string" && audio.turn_key.trim()
      ? audio.turn_key.trim()
      : undefined;
    const k = (turn ? remoteTurnKey.get(turn.id) : undefined)
      ?? suppliedTurnKey
      ?? audios[audio.raw_audio_id]?.turnKey
      ?? `${UNBOUND_AUDIO_TURN_KEY_PREFIX}${audio.raw_audio_id}`;
    audios[audio.raw_audio_id] = {
      turnKey: k,
      containsDirectIdentifier: audio.contains_direct_identifier,
      isReliabilitySample: audio.is_reliability_sample,
      lastStatus: audio.status,
      durationSeconds: turn?.duration_seconds ?? audios[audio.raw_audio_id]?.durationSeconds,
    };
  }

  const cueLevels = { ...(local.cueLevels ?? {}) };
  for (const [k, turn] of Object.entries(turns)) {
    const level = Math.max(cueLevels[k] ?? 0, turn.cueLevel ?? 0, turn.promptLevel ?? 0);
    if (level > 0) cueLevels[k] = level;
  }

  return { ...local, itemEvents: items, turns, audios, cueLevels };
}

export function useSessionJournal(sessionId: string) {
  const [journal, setJournal] = useState<SessionJournal>(() => load(sessionId));

  useEffect(() => { setJournal(load(sessionId)); }, [sessionId]);

  const persist = useCallback((next: SessionJournal) => {
    localStorage.setItem(key(next.sessionId), JSON.stringify(next));
    setJournal(next);
  }, []);

  const upsertItem = useCallback((itemId: string, v: JournalItem) => {
    setJournal((j) => { const n = { ...j, itemEvents: { ...j.itemEvents, [itemId]: v } }; localStorage.setItem(key(n.sessionId), JSON.stringify(n)); return n; });
  }, []);

  const upsertTurn = useCallback((itemId: string, turnSeq: number, patch: Partial<JournalTurn> & Pick<JournalTurn, "turnId" | "responseRole">) => {
    setJournal((j) => {
      const k = turnKey(itemId, turnSeq);
      const prev = j.turns[k] ?? { turnId: patch.turnId, responseRole: patch.responseRole, asrSaved: false, confirmed: false, aiJudged: false, locked: false };
      const n = { ...j, turns: { ...j.turns, [k]: { ...prev, ...patch } } };
      localStorage.setItem(key(n.sessionId), JSON.stringify(n)); return n;
    });
  }, []);

  const upsertAudio = useCallback((rawAudioId: string, v: JournalAudio) => {
    setJournal((j) => { const n = { ...j, audios: { ...j.audios, [rawAudioId]: v } }; localStorage.setItem(key(n.sessionId), JSON.stringify(n)); return n; });
  }, []);

  const recordCueLevel = useCallback((turnK: string, level: number) => {
    setJournal((j) => { const n = { ...j, cueLevels: { ...(j.cueLevels ?? {}), [turnK]: level } }; localStorage.setItem(key(n.sessionId), JSON.stringify(n)); return n; });
  }, []);

  const setCursor = useCallback((itemIdx: number, turnIdx: number) => {
    setJournal((j) => { const n = { ...j, cursor: { itemIdx, turnIdx } }; localStorage.setItem(key(n.sessionId), JSON.stringify(n)); return n; });
  }, []);

  const hydrateFromServer = useCallback((remote: ServerSessionJournal) => {
    setJournal((j) => {
      if (remote.session.session_id !== j.sessionId) return j;
      const n = mergeServerJournal(j, remote);
      localStorage.setItem(key(n.sessionId), JSON.stringify(n));
      return n;
    });
  }, []);

  return { journal, persist, upsertItem, upsertTurn, upsertAudio, recordCueLevel, setCursor, hydrateFromServer };
}
