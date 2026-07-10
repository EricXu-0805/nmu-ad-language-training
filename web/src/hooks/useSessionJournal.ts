// 操作端作业日志(localStorage)。后端无"按场次列 items/turns"GET,靠它做 id 映射 + 续做位 + 防重复建。
// 单机本地优先正解;若后端将来加 GET /sessions/{sid}/items?embed=turns,可切成从服务器权威重建。
import { useCallback, useState } from "react";
import { turnKey } from "../lib/ids";

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

const key = (sid: string) => `nmu:journal:${sid}`;

function load(sid: string): SessionJournal {
  try {
    const raw = localStorage.getItem(key(sid));
    if (raw) return JSON.parse(raw) as SessionJournal;
  } catch { /* 损坏则重建 */ }
  return { sessionId: sid, itemEvents: {}, turns: {}, audios: {}, cursor: { itemIdx: 0, turnIdx: 0 } };
}

export function useSessionJournal(sessionId: string) {
  const [journal, setJournal] = useState<SessionJournal>(() => load(sessionId));

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

  return { journal, persist, upsertItem, upsertTurn, upsertAudio, recordCueLevel, setCursor };
}
