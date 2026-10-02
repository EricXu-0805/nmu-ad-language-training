/**
 * 验收草稿只存在这台设备浏览器的 localStorage 里：刷新、误点返回、标签页被系统回收都不丢
 * 勾选。刻意不上服务器——验收当天可能正好在验断网。
 *
 * 每一次读写都包在 try/catch 里：私隐模式、存储被禁、配额满都只是「存不上」，页面照常能填、
 * 能冻结、能下载。读回来的东西结构对不上就整份不认，不拼半份回来。
 * 只存当前这一份：老人端的暂停意图也在 localStorage 里，验收记录不能越积越多去挤它的配额。
 */
import { isDeviceFactsShape } from "./acceptanceDeviceFacts.ts";
import {
  ACCEPTANCE_HEADER_FIELDS,
  ACCEPTANCE_STEP_KEYS,
  emptyDraft,
  isReceiptId,
  readFrozenAcceptanceReceipt,
  type AcceptanceDraft,
} from "./acceptanceProtocol.ts";

export const ACCEPTANCE_DRAFT_STORAGE_KEY = "nmu:acceptance:draft:v1";
const STORE_VERSION = 1;

export type AcceptanceDraftStorage = Pick<Storage, "getItem" | "setItem">;

export interface AcceptanceWork {
  draft: AcceptanceDraft;
  /** 已冻结回执的原始字节；没冻结为 null。 */
  frozenJson: string | null;
}

/** 站点存储被禁时，连读 window.localStorage 这个属性都会抛。 */
export function browserAcceptanceStorage(): AcceptanceDraftStorage | null {
  try { return window.localStorage; } catch { return null; }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseDraft(value: unknown): AcceptanceDraft | null {
  if (!isRecord(value)) return null;
  const { receiptId, supersedesReceiptId, startedAtIso, header, outcomes, notes, deviceFacts } = value;
  if (!isReceiptId(receiptId)
      || !(supersedesReceiptId === null || isReceiptId(supersedesReceiptId))
      || typeof startedAtIso !== "string" || !Number.isFinite(Date.parse(startedAtIso))
      || !isRecord(header) || !isRecord(outcomes) || !isRecord(notes)
      || !(deviceFacts === null || isDeviceFactsShape(deviceFacts))) return null;
  const draft = emptyDraft(receiptId, startedAtIso);
  draft.supersedesReceiptId = supersedesReceiptId;
  draft.deviceFacts = deviceFacts;
  // 只认当前协议里的键：协议后来删掉的项丢掉，新加的项按没填、没勾补上。
  for (const field of ACCEPTANCE_HEADER_FIELDS) {
    const text = header[field.key];
    if (text === undefined) continue;
    if (typeof text !== "string") return null;
    draft.header[field.key] = text;
  }
  for (const key of ACCEPTANCE_STEP_KEYS) {
    const outcome = outcomes[key];
    if (outcome !== undefined) {
      if (outcome !== "pass" && outcome !== "fail" && outcome !== "unset") return null;
      draft.outcomes[key] = outcome;
    }
    const note = notes[key];
    if (note !== undefined) {
      if (typeof note !== "string") return null;
      draft.notes[key] = note;
    }
  }
  return draft;
}

function parseStoredWork(value: unknown): AcceptanceWork | null {
  if (!isRecord(value) || value.version !== STORE_VERSION) return null;
  const draft = parseDraft(value.draft);
  if (draft === null) return null;
  const { frozenJson } = value;
  if (frozenJson === null) return { draft, frozenJson: null };
  if (typeof frozenJson !== "string") return null;
  // 冻结的字节必须就是这份草稿的：编号对不上说明存储被动过，整份不认。
  const frozen = readFrozenAcceptanceReceipt(frozenJson);
  return frozen !== null && frozen.receipt.receiptId === draft.receiptId ? { draft, frozenJson } : null;
}

export function loadAcceptanceWork(storage: AcceptanceDraftStorage | null): AcceptanceWork | null {
  if (storage === null) return null;
  try {
    const raw = storage.getItem(ACCEPTANCE_DRAFT_STORAGE_KEY);
    return raw === null ? null : parseStoredWork(JSON.parse(raw));
  } catch {
    return null;
  }
}

export function saveAcceptanceWork(storage: AcceptanceDraftStorage | null, work: AcceptanceWork): boolean {
  if (storage === null) return false;
  try {
    storage.setItem(ACCEPTANCE_DRAFT_STORAGE_KEY,
      JSON.stringify({ version: STORE_VERSION, draft: work.draft, frozenJson: work.frozenJson }));
    return true;
  } catch {
    return false;
  }
}
