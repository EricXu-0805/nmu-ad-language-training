import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  getRecoveryDeviceCapability,
  removeRecoveryDeviceCapabilityIfMatches,
} from "../api";
import { ApiError } from "../apiResponse";
import {
  acquireAudioDeviceLease,
  AudioDeviceLeaseUnavailableError,
  type AudioDeviceLease,
} from "../audio/audioDeviceLease";
import { advanceAudioOutbox, createAudioOutboxEntry, type AudioOutboxEntry } from "../audio/audioOutbox";
import {
  buildAudioSavedPayload,
  finalizeAudioSavedRequest,
  type AudioSavedFinalizationOutcome,
} from "../audio/audioTerminalDisposition";
import {
  orderAudioRecoveryEntries,
  recoveryMayMutateActiveUi,
  shouldBroadcastRecoveredAudio,
  type AudioRecoveryUiFence,
} from "../audio/audioRecoveryQueue";
import { completeAudioOutboxAfterServerAck, sha256Blob, validateAudioUploadReceipt } from "../audio/audioUploadReceipt";
import { blobStore } from "../audio/blobStore";
import { Recorder } from "../audio/recorder";
import { bus } from "../sync/bus";
import type { PatientRecFailureCode, PatientRecMsg, RecState } from "../sync/messages";
import { newAudioId } from "../lib/ids";
import { authorizesMicrophoneStart, recordingRevocationAction } from "./recordingAuthorization";
import type { AudioRecorderBlockReason } from "./audioRecorderBlock";
import { claimRecordingStartFailure, classifyRecordingStartFailure } from "./recordingStartFailure";

interface ArmedMeta {
  sessionId: string;
  turnKey: string;
  containsDirectIdentifier: boolean;
}

interface PendingSave {
  rawAudioId: string;
  blob: Blob | null;
  durationSeconds: number;
  meta: ArmedMeta;
  entry: AudioOutboxEntry;
  localSaved: boolean;
  registered: boolean;
  uploaded: boolean;
}

function pendingFromOutbox(entry: AudioOutboxEntry, blob: Blob): PendingSave {
  return {
    rawAudioId: entry.rawAudioId,
    blob,
    durationSeconds: entry.durationSeconds,
    meta: {
      sessionId: entry.sessionId,
      turnKey: entry.turnKey,
      containsDirectIdentifier: entry.containsDirectIdentifier,
    },
    entry,
    localSaved: true,
    registered: entry.phase !== "captured",
    uploaded: entry.phase === "uploaded",
  };
}

type StartKind = "remote" | "self";

interface StartPermit {
  generation: number;
  kind: StartKind;
  sessionId: string;
  turnKey: string;
  recSeq?: number;
  commandSeq?: number;
  failureId?: string;
}

const MIC_START_TIMEOUT_MS = 12_000;
const MAX_RECORDING_MS = 5 * 60_000;

// 老人端 VOX 录音共享逻辑(任务屏/关系建立屏共用):
// 研究者端 arm → 自动开始;转 idle/stopped → 停止并保存(老人无需按任何键);
// 自助模式:老人自己点"开始回答"(startNow)/"我说好了"(stopAndSave)。
// 保存链:IndexedDB 兜底副本 → 服务端登记元数据 → 字节上传 → 双通道回报 audioSaved。
// 麦克风开/关随时经 patientRec 上报——自助开录时这是操作端唯一的感知与远程停止依据。
export function useVoxRecorder(opts: {
  sessionId: string;
  recording: RecState | undefined;
  recSeq?: number;
  commandSeq?: number;
  turnKey: string;
  containsDirectIdentifier?: boolean;
  connectionReady?: boolean;
  suspended?: boolean; // 场次级暂停等"无游标边沿"的挂起:必须立即封存许可+停麦(热麦红线)
  selfStartAllowed?: boolean; // 自助开录的当前授权；变 false 时在途许可与活麦一并收回
  stopRequested?: boolean; // thanks/done/终态/收尾等无关 recSeq 的强制停麦信号
  maxRecordingMs?: number; // 单段录音上限,到点自动停并保存(老人没按「我说好了」流程也走得下去)
}) {
  const recRef = useRef<Recorder>(new Recorder());
  // ★元数据在 arm 时刻锁存:录音中操作端直接跳节/跳题时,stop 消息与新指针同帧到达,
  // 若用 stop 时刻的 props,这段音频会归错环节——自我介绍段还会丢 contains_direct_identifier 红线标记。
  const armedMeta = useRef<ArmedMeta | null>(null);
  // 上传失败时保留同一份 blob/id 重试，不能先报 audioSaved 再让操作端自动推进。
  const pendingSave = useRef<PendingSave | null>(null);
  // 恢复扫描完成前一律不许开新麦；否则旧 outbox 尚未接管，新回答就会覆盖现场注意力。
  const outboxReady = useRef(false);
  const outboxRecovery = useRef<Promise<void> | null>(null);
  // Web Locks is the only cross-tab ownership primitive. A JS-only fallback
  // would recreate the exact split-brain race this lease is meant to prevent.
  const deviceLease = useRef<AudioDeviceLease | null>(null);
  const shutdownRef = useRef<() => Promise<void>>(async () => {});
  const saveOperation = useRef<Promise<void> | null>(null);
  const savingRef = useRef(false);
  const recordingLimit = useRef<number | null>(null);
  const clearRecordingLimit = useCallback(() => {
    if (recordingLimit.current != null) {
      clearTimeout(recordingLimit.current);
      recordingLimit.current = null;
    }
  }, []);
  // 记录挂载时的远端快照；React StrictMode 会重复执行 effect，不能只靠“第一次”布尔值防陈旧 armed 开麦。
  const initialRemote = useRef({ recording: opts.recording, recSeq: opts.recSeq, commandSeq: opts.commandSeq });
  const [saving, setSaving] = useState(false);
  const [canRetry, setCanRetry] = useState(false);
  // recActive:录音器活跃态的响应式镜像。老人端自助开录不经过操作端游标,
  // MicButton 不能只看 cursor.recording,还要看本地真在录没有。
  const [recActive, setRecActive] = useState(false);
  const [micError, setMicError] = useState(false);
  const [starting, setStarting] = useState(false);
  // 断线/超时时把当时的远端录音许可封存为陈旧；恢复连接不能凭同一条
  // armed 快照重新开麦。发线索等普通游标更新也会改 wseq，只有新 recSeq 才代表新的开麦意图。
  const [remoteCommandBlocked, setRemoteCommandBlocked] = useState(false);
  const blockedRemote = useRef<{ recSeq?: number } | null>(null);
  // 一条远端 arm 只能启动一次。保存完成时服务端 idle 回写可能还在路上，
  // 若不记已消费 recSeq，saving true→false 会把同一条 armed 再开一次麦。
  const consumedRemote = useRef<{ recSeq?: number } | null>(null);
  // 保存失败必须对老人可见(平和措辞):否则自助模式下回答静默丢失、无人知晓。
  const [saveError, setSaveError] = useState(false);
  const [outboxRecoveryEpoch, setOutboxRecoveryEpoch] = useState(0);
  const [leaseEpoch, setLeaseEpoch] = useState(0);
  const [blockReason, setBlockReason] = useState<AudioRecorderBlockReason | null>("lease-waiting");
  const {
    sessionId, recording, recSeq, commandSeq, turnKey, containsDirectIdentifier,
    connectionReady = true, suspended = false, selfStartAllowed = false, stopRequested = false,
    maxRecordingMs,
  } = opts;
  const sessionUiFence = useRef<AudioRecoveryUiFence>({ sessionId, generation: 0 });
  if (sessionUiFence.current.sessionId !== sessionId) {
    sessionUiFence.current = {
      sessionId,
      generation: sessionUiFence.current.generation + 1,
    };
  }
  const latest = useRef({
    sessionId, recording, recSeq, commandSeq, turnKey, containsDirectIdentifier,
    connectionReady, suspended, selfStartAllowed, stopRequested, maxRecordingMs,
  });
  latest.current = {
    sessionId, recording, recSeq, commandSeq, turnKey, containsDirectIdentifier,
    connectionReady, suspended, selfStartAllowed, stopRequested, maxRecordingMs,
  };
  const mountedRef = useRef(true);
  const startGeneration = useRef(0);
  const startTimeout = useRef<number | null>(null);
  const startingRef = useRef(false);
  const activePermit = useRef<StartPermit | null>(null);
  const activeKind = useRef<StartKind | null>(null);
  const patientPauseDiscardRequested = useRef(false);

  // 这项 effect 必须排在所有启动 effect 前：StrictMode 第二次 setup 时先恢复 mounted 真值。
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      startGeneration.current += 1;
      activePermit.current = null;
      activeKind.current = null;
      recRef.current.cancelPendingStart();
      clearRecordingLimit();
      if (startTimeout.current != null) clearTimeout(startTimeout.current);
      startTimeout.current = null;
      startingRef.current = false;
    };
  }, [clearRecordingLimit]);

  // One patient page owns recording/recovery for the entire origin. On
  // unmount, release is delayed until shutdownRef drains stop→stage→commit;
  // the next session/tab therefore cannot scan an empty outbox in between.
  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    let ownedLease: AudioDeviceLease | null = null;
    const shutdown = shutdownRef.current;
    outboxReady.current = false;
    setBlockReason("lease-waiting");
    void acquireAudioDeviceLease(undefined, controller.signal).then((lease) => {
      if (cancelled) {
        lease.release();
        return;
      }
      ownedLease = lease;
      deviceLease.current = lease;
      setBlockReason("storage-checking");
      setLeaseEpoch((value) => value + 1);
    }).catch((error: unknown) => {
      if (cancelled || (error instanceof DOMException && error.name === "AbortError")) return;
      outboxReady.current = false;
      setBlockReason(error instanceof AudioDeviceLeaseUnavailableError ? "lease-unavailable" : "storage-error");
    });
    return () => {
      cancelled = true;
      const lease = ownedLease;
      if (!lease) {
        controller.abort();
        return;
      }
      // React cleanup cannot await, but the Web Lock callback can. Keeping the
      // lock held is the actual cross-session barrier.
      void (async () => {
        try {
          await shutdown();
        } finally {
          deviceLease.current = null;
          lease.release();
          await lease.released;
        }
      })();
    };
  }, []);

  const persistRec = useCallback(async (
    active: boolean,
    tk: string,
    lockedSessionId = sessionId,
    failure?: { failureCode: PatientRecFailureCode; failureId: string },
  ): Promise<PatientRecMsg | null> => {
    const payload = failure
      ? { active: false as const, turnKey: tk, sessionId: lockedSessionId, ...failure }
      : { active, turnKey: tk, sessionId: lockedSessionId };
    try {
      // patientRec 也以服务器为真值；尤其 active 不得在终态 409 之前先欺骗操作端。
      await api.putLiveState("patientRec", payload);
      return { type: "patientRec", ...payload } as PatientRecMsg;
    } catch {
      return null;
    }
  }, [sessionId]);

  const postInactive = useCallback((tk: string, lockedSessionId: string) => {
    void persistRec(false, tk, lockedSessionId).then((committed) => {
      if (committed) bus.post(committed);
    });
  }, [persistRec]);

  const commitPending = useCallback(async (
    pending: PendingSave,
    options: { broadcastSaved?: boolean } = {},
  ): Promise<AudioSavedFinalizationOutcome> => {
    const operationUiFence = { ...sessionUiFence.current };
    if (!deviceLease.current) throw new Error("未持有录音设备锁，拒绝保存");
    if (!pending.blob || pending.blob.size !== pending.entry.blobBytes
        || (pending.blob.type || "audio/webm") !== pending.entry.mimeType) {
      throw new Error("待上传录音与 outbox 快照不一致");
    }
    if (!pending.localSaved) {
      if (!pending.blob) throw new Error("待上传录音缺少本机字节");
      // blob + 恢复元数据同事务封存；刷新后仍能从相同阶段继续。
      try {
        await blobStore.stageOutbox(pending.entry, pending.blob);
      } catch (stageError) {
        // IndexedDB 事务已提交但页面在收到 oncomplete 前关闭时，重试会撞 add。
        // 只有元数据和实际 blob 哈希都完全相同才认作幂等恢复。
        const existingEntry = await blobStore.getOutbox(pending.rawAudioId);
        const existingBlob = await blobStore.get(pending.rawAudioId);
        if (!existingEntry || !existingBlob
            || JSON.stringify(existingEntry) !== JSON.stringify(pending.entry)
            || await sha256Blob(existingBlob) !== await sha256Blob(pending.blob)) {
          throw stageError;
        }
        pending.entry = existingEntry;
        pending.blob = existingBlob;
      }
      pending.localSaved = true;
    }

    const finalize = async (checksum: string): Promise<AudioSavedFinalizationOutcome> => {
      const blob = pending.blob;
      if (!blob) throw new Error("录音终态核对缺少本机字节");
      const saved = buildAudioSavedPayload(pending.entry, checksum);
      const outcome = await finalizeAudioSavedRequest(
        {
          entry: pending.entry,
          blob,
          saved,
          // Evaluate after the network/IndexedDB awaits, not before them: a late
          // S2 receipt must not emit into a newly active S3 generation.
          broadcastSaved: () => mountedRef.current
            && (options.broadcastSaved ?? true)
            && recoveryMayMutateActiveUi(
              operationUiFence,
              sessionUiFence.current,
              pending.meta.sessionId,
            ),
        },
        {
          putAudioSaved: (payload) => api.putAudioSaved(payload),
          completeServerSaved: (response) => completeAudioOutboxAfterServerAck(
            response,
            pending.rawAudioId,
            () => blobStore.completeOutbox(pending.rawAudioId),
          ).then(() => undefined),
          discardTerminalOutbox: async (disposition) => {
            // The component owns the origin-wide Web Lock until shutdown drains
            // this operation. Re-check immediately before the destructive call.
            if (!deviceLease.current) throw new Error("录音锁已释放，拒绝终态本机清理");
            await blobStore.discardTerminalOutbox(pending.entry, disposition);
          },
          broadcastAudioSaved: (payload) => {
            // Server receipt is already authoritative; a failed local wake hint
            // must not resurrect a completed outbox.
            try { bus.post({ type: "audioSaved", ...payload }); }
            catch { /* BroadcastChannel/localStorage hint is best effort */ }
          },
          // 治理回执:本地删除已完成后如实上报;finalize 内部吞错,永不阻断终态。
          reportLocalCopyDisposal: (disposition) =>
            api.putAudioDisposalConfirmed(disposition).then(() => undefined),
        },
      );
      pending.blob = null;
      pending.localSaved = false;
      if (pendingSave.current === pending) pendingSave.current = null;
      if (pending.meta.sessionId !== sessionUiFence.current.sessionId) {
        // The session may have changed while the request was in flight. If this
        // was the last local item for that old session, prune only its exact
        // recovery token; no active-session event is emitted.
        const recoveryCredential = getRecoveryDeviceCapability(pending.meta.sessionId);
        if (recoveryCredential) {
          try {
            const remainingEntries = await blobStore.outboxEntries();
            if (pending.meta.sessionId !== sessionUiFence.current.sessionId
                && !remainingEntries.some((entry) => entry.sessionId === pending.meta.sessionId)) {
              removeRecoveryDeviceCapabilityIfMatches(recoveryCredential);
            }
          } catch { /* stale recovery credential expires naturally; active UI stays isolated */ }
        }
      }
      const mayAffectActiveUi = recoveryMayMutateActiveUi(
        operationUiFence,
        sessionUiFence.current,
        pending.meta.sessionId,
      );
      if (mountedRef.current && mayAffectActiveUi) {
        setCanRetry(false);
        setSaveError(false);
      }
      if (outcome.kind === "terminal-discarded" && mayAffectActiveUi) {
        // Governance discard is deliberately not equivalent to saved: keep the
        // device stopped until a researcher verifies the terminal session.
        outboxReady.current = false;
        if (mountedRef.current) setBlockReason("terminal-discarded");
      }
      return outcome;
    };

    const probeTerminalMetadataAfterConflict = async (): Promise<AudioSavedFinalizationOutcome> => {
      // Exactly one metadata outcome call per 409. There is no recursion or
      // status-poll loop; any non-2xx other than exact 410 preserves the bytes.
      const checksum = await sha256Blob(pending.blob as Blob);
      return finalize(checksum);
    };

    // Uploaded outboxes prioritize the authoritative audioSaved ledger ACK.
    // Re-uploading first would turn a lost ACK + later withdrawal into a loop.
    if (pending.entry.phase === "uploaded") {
      if (!pending.entry.checksum) throw new Error("已上传录音缺少本机 checksum 收据");
      if (await sha256Blob(pending.blob) !== pending.entry.checksum) {
        throw new Error("已上传录音的本机字节与 outbox checksum 不一致");
      }
      return finalize(pending.entry.checksum);
    }

    if (!pending.registered) {
      // POST /audio 对完全相同的登记是服务端幂等的；409 只代表字段冲突，绝不由 PIN
      // 旁路读取元数据后自行猜测成功。
      try {
        await api.createAudio({
          raw_audio_id: pending.rawAudioId,
          session_id: pending.meta.sessionId,
          turn_key: pending.meta.turnKey,
          contains_direct_identifier: pending.meta.containsDirectIdentifier,
        });
      } catch (error) {
        if (error instanceof ApiError && error.status === 409) {
          return probeTerminalMetadataAfterConflict();
        }
        throw error;
      }
      pending.entry = advanceAudioOutbox(pending.entry, "registered");
      await blobStore.putOutbox(pending.entry);
      pending.registered = true;
    }

    if (!pending.uploaded) {
      if (!pending.blob) throw new Error("待上传录音缺少本机字节");
      // 这里必须 await 且不得吞错。receipt 必须证明服务端保存的是同一份字节。
      const localChecksum = await sha256Blob(pending.blob);
      let receipt: unknown;
      try {
        receipt = await api.uploadAudioBlob(
          pending.rawAudioId, pending.blob, pending.meta.sessionId);
      } catch (error) {
        if (error instanceof ApiError && error.status === 409) {
          return probeTerminalMetadataAfterConflict();
        }
        // Authentication, network, 422, 429 and 5xx all preserve this exact
        // registered outbox. They are never reinterpreted as deletion consent.
        throw error;
      }
      validateAudioUploadReceipt(receipt, {
        rawAudioId: pending.rawAudioId,
        bytes: pending.blob.size,
        checksum: localChecksum,
      });
      // 先持久标记 uploaded，再删 blob；两步间崩溃时恢复链可据 checksum 补回执并清副本。
      pending.entry = advanceAudioOutbox(
        pending.entry, "uploaded", { checksum: localChecksum });
      await blobStore.putOutbox(pending.entry);
      pending.uploaded = true;
    }
    if (!pending.entry.checksum) throw new Error("已上传录音缺少本机 checksum 收据");
    return finalize(pending.entry.checksum);
  }, []);

  // 刷新/重启恢复：相同 rawAudioId 从已持久阶段继续，绝不要求老人重答。
  useEffect(() => {
    if (!deviceLease.current || leaseEpoch === 0) return;
    if (outboxRecovery.current) return;
    outboxReady.current = false;
    const lockedSessionId = sessionId;
    const recoveryUiFence = { ...sessionUiFence.current };
    const recovery = (async () => {
      let terminalDiscarded = false;
      const usedRecoveryCredentials = new Map<string, NonNullable<ReturnType<typeof getRecoveryDeviceCapability>>>();
      try {
        const snapshot = await blobStore.recoverySnapshot();
        if (snapshot.invalidBlobKeyCount > 0) {
          if (mountedRef.current && recoveryMayMutateActiveUi(
            recoveryUiFence, sessionUiFence.current, lockedSessionId)) setBlockReason("storage-invalid");
          return;
        }
        if (snapshot.legacyOrphans.length > 0) {
          // PIN-only 设备不能读服务器录音元数据，也绝不猜测归属/静默删除。
          if (mountedRef.current && recoveryMayMutateActiveUi(
            recoveryUiFence, sessionUiFence.current, lockedSessionId)) setBlockReason("legacy-audio");
          return;
        }
        const entries = orderAudioRecoveryEntries(snapshot.entries, lockedSessionId);
        const remainingBySession = new Map<string, number>();
        for (const entry of entries) {
          remainingBySession.set(entry.sessionId, (remainingBySession.get(entry.sessionId) ?? 0) + 1);
        }
        if (entries.length === 0) {
          if (recoveryMayMutateActiveUi(
            recoveryUiFence, sessionUiFence.current, lockedSessionId)) {
            outboxReady.current = true;
            if (mountedRef.current) setBlockReason(null);
          }
          return;
        }
        // recoverySnapshot is deterministically ordered by creation then raw ID.
        // Drain old sessions first only when their own recovery credential can
        // authorize an exact register/blob/audioSaved chain. No old-session
        // success is broadcast into the currently active UI.
        if (mountedRef.current && recoveryMayMutateActiveUi(
          recoveryUiFence, sessionUiFence.current, lockedSessionId)) {
          setBlockReason(null);
          setSaving(true);
        }
        savingRef.current = true;
        try {
          for (const entry of entries) {
            const isForeign = entry.sessionId !== lockedSessionId;
            if (isForeign) {
              const recoveryCredential = getRecoveryDeviceCapability(entry.sessionId);
              if (recoveryCredential) {
                usedRecoveryCredentials.set(entry.sessionId, recoveryCredential);
              }
            }
            const blob = await blobStore.get(entry.rawAudioId);
            if (!blob || blob.size !== entry.blobBytes
                || (blob.type || "audio/webm") !== entry.mimeType
                || (entry.phase === "uploaded" && await sha256Blob(blob) !== entry.checksum)) {
              if (mountedRef.current && recoveryMayMutateActiveUi(
                recoveryUiFence, sessionUiFence.current, lockedSessionId)) setBlockReason("storage-invalid");
              return;
            }
            const pending = pendingFromOutbox(entry, blob);
            pendingSave.current = pending;
            if (mountedRef.current && recoveryMayMutateActiveUi(
              recoveryUiFence, sessionUiFence.current, lockedSessionId)) {
              setCanRetry(!isForeign);
              setSaveError(false);
            }
            try {
              const outcome = await commitPending(pending, {
                broadcastSaved: shouldBroadcastRecoveredAudio(entry, lockedSessionId),
              });
              if (outcome.kind === "terminal-discarded" && recoveryMayMutateActiveUi(
                recoveryUiFence, sessionUiFence.current, entry.sessionId)) terminalDiscarded = true;
              const remaining = (remainingBySession.get(entry.sessionId) ?? 1) - 1;
              remainingBySession.set(entry.sessionId, remaining);
              if (isForeign && remaining === 0) {
                // S1 may drain successfully before a later S2 item fails. Prune
                // S1's exact recovery slot now, after a same-lease outbox re-read,
                // so stale capability cleanup never depends on S2's outcome.
                const credential = usedRecoveryCredentials.get(entry.sessionId);
                if (credential) {
                  try {
                    const remainingEntries = await blobStore.outboxEntries();
                    if (!remainingEntries.some((candidate) => candidate.sessionId === entry.sessionId)) {
                      removeRecoveryDeviceCapabilityIfMatches(credential);
                      usedRecoveryCredentials.delete(entry.sessionId);
                    }
                  } catch { /* retaining an expired recovery slot is safer than disturbing active UI */ }
                }
              }
            } catch {
              // Unknown/network/auth/integrity failures keep both stores intact.
              // A foreign item has no retry button in the current session: the
              // copy tells the researcher to re-pair/switch to its origin.
              if (mountedRef.current && recoveryMayMutateActiveUi(
                recoveryUiFence, sessionUiFence.current, lockedSessionId)) {
                setCanRetry(!isForeign);
                setSaveError(!isForeign);
                setBlockReason(isForeign ? "pending-other-session" : null);
              }
              return;
            }
          }

          // Re-read under the same origin lease before pruning old credentials.
          // Exact-token removal is silent and cannot blank the active session.
          const after = await blobStore.recoverySnapshot();
          if (after.invalidBlobKeyCount > 0) {
            if (mountedRef.current && recoveryMayMutateActiveUi(
              recoveryUiFence, sessionUiFence.current, lockedSessionId)) setBlockReason("storage-invalid");
            return;
          }
          if (after.legacyOrphans.length > 0) {
            if (mountedRef.current && recoveryMayMutateActiveUi(
              recoveryUiFence, sessionUiFence.current, lockedSessionId)) setBlockReason("legacy-audio");
            return;
          }
          for (const [recoveredSessionId, credential] of usedRecoveryCredentials) {
            if (!after.entries.some((entry) => entry.sessionId === recoveredSessionId)) {
              removeRecoveryDeviceCapabilityIfMatches(credential);
            }
          }
          if (after.entries.length > 0) {
            if (mountedRef.current && recoveryMayMutateActiveUi(
              recoveryUiFence, sessionUiFence.current, lockedSessionId)) setBlockReason("pending-other-session");
            return;
          }
          if (recoveryMayMutateActiveUi(
            recoveryUiFence, sessionUiFence.current, lockedSessionId)) {
            if (terminalDiscarded) {
              outboxReady.current = false;
              if (mountedRef.current) setBlockReason("terminal-discarded");
            } else {
              outboxReady.current = true;
              if (mountedRef.current) setBlockReason(null);
            }
          }
        } finally {
          savingRef.current = false;
          if (mountedRef.current) setSaving(false);
        }
      } catch {
        // IndexedDB 不可用、条目被篡改或网络失败都保持 fail-closed；pending 若已恢复可人工重试。
        if (mountedRef.current && recoveryMayMutateActiveUi(
          recoveryUiFence, sessionUiFence.current, lockedSessionId)) {
          if (pendingSave.current) {
            const isForeign = pendingSave.current.meta.sessionId !== lockedSessionId;
            setBlockReason(isForeign ? "pending-other-session" : null);
            setCanRetry(!isForeign);
            setSaveError(!isForeign);
          } else {
            setBlockReason("storage-error");
          }
        }
      }
    })();
    const tracked = recovery.finally(() => {
      if (outboxRecovery.current === tracked) outboxRecovery.current = null;
      // 极少数情况下切场发生在旧场次恢复未完成时；旧任务退场后主动唤醒新场次扫描。
      if (mountedRef.current && latest.current.sessionId !== lockedSessionId) {
        setOutboxRecoveryEpoch((value) => value + 1);
      }
    });
    outboxRecovery.current = tracked;
  }, [commitPending, sessionId, outboxRecoveryEpoch, leaseEpoch]);

  const stopAndSave = useCallback((): Promise<void> => {
    if (!recRef.current.active || savingRef.current) {
      return saveOperation.current ?? Promise.resolve();
    }
    if (!deviceLease.current) {
      // Defensive only: launchStart cannot create a recorder without the lock.
      // If ownership is nevertheless lost, close the hot microphone immediately.
      recRef.current.discardActive();
      if (mountedRef.current) setBlockReason("storage-error");
      return Promise.resolve();
    }
    clearRecordingLimit();
    savingRef.current = true;
    if (mountedRef.current) {
      setSaving(true);
      setRecActive(false);
    }
    const meta = armedMeta.current ?? { sessionId, turnKey, containsDirectIdentifier: containsDirectIdentifier ?? false };
    const operation = (async () => {
      try {
        const rec = await recRef.current.stop();
        postInactive(meta.turnKey, meta.sessionId);
        const rawAudioId = newAudioId();
        const entry = createAudioOutboxEntry({
          rawAudioId,
          sessionId: meta.sessionId,
          turnKey: meta.turnKey,
          containsDirectIdentifier: meta.containsDirectIdentifier,
          durationSeconds: rec.durationSeconds,
          blob: rec.blob,
        });
        const pending: PendingSave = {
          rawAudioId,
          blob: rec.blob,
          durationSeconds: rec.durationSeconds,
          meta,
          entry,
          localSaved: false,
          registered: false,
          uploaded: false,
        };
        pendingSave.current = pending;
        if (mountedRef.current) setCanRetry(true);
        await commitPending(pending);
      } catch {
        // 麦已关；若已经得到 blob，pendingSave 与 IndexedDB 副本均保留，允许重试同一段录音。
        recRef.current.discardActive();
        postInactive(meta.turnKey, meta.sessionId);
        if (mountedRef.current && !patientPauseDiscardRequested.current) setSaveError(true);
      } finally {
        armedMeta.current = null;
        activeKind.current = null;
        savingRef.current = false;
        if (mountedRef.current) setSaving(false);
      }
    })();
    saveOperation.current = operation;
    void operation.then(
      () => { if (saveOperation.current === operation) saveOperation.current = null; },
      () => { if (saveOperation.current === operation) saveOperation.current = null; },
    );
    return operation;
  }, [sessionId, turnKey, containsDirectIdentifier, postInactive, commitPending, clearRecordingLimit]);

  const retrySave = useCallback((): Promise<void> => {
    const pending = pendingSave.current;
    if (!pending || savingRef.current) return saveOperation.current ?? Promise.resolve();
    if (!deviceLease.current) {
      setBlockReason("storage-error");
      return Promise.resolve();
    }
    savingRef.current = true;
    setSaving(true);
    const operation = (async () => {
      try {
        const outcome = await commitPending(pending, {
          broadcastSaved: pending.meta.sessionId === latest.current.sessionId,
        });
        if (outcome.kind === "server-saved") {
          // A prior multi-entry recovery may still have later items. Re-scan
          // under the same lease instead of directly reopening the microphone.
          outboxReady.current = false;
          setOutboxRecoveryEpoch((value) => value + 1);
        }
      } catch {
        // 不清 pending、不清错误；下次仍重试同一 rawAudioId/blob，避免重复回答或错绑。
        if (mountedRef.current) setSaveError(true);
      } finally {
        savingRef.current = false;
        if (mountedRef.current) setSaving(false);
      }
    })();
    saveOperation.current = operation;
    void operation.then(
      () => { if (saveOperation.current === operation) saveOperation.current = null; },
      () => { if (saveOperation.current === operation) saveOperation.current = null; },
    );
    return operation;
  }, [commitPending]);

  const invalidateStart = useCallback(() => {
    startGeneration.current += 1;
    activePermit.current = null;
    recRef.current.cancelPendingStart();
    if (startTimeout.current != null) {
      clearTimeout(startTimeout.current);
      startTimeout.current = null;
    }
    startingRef.current = false;
    if (mountedRef.current) setStarting(false);
  }, []);

  /**
   * 老人主动点击暂停：同一同步调用栈里撤掉许可、关麦、丢弃当前字节。
   * 这不是远程 idle/stopped 边沿，不能经 stopAndSave 生成一段
   * 被强制中断的“有效作答”。已在此前完成的 durable outbox 不动。
   */
  const discardForPatientPause = useCallback(() => {
    patientPauseDiscardRequested.current = true;
    const meta = armedMeta.current;
    blockedRemote.current = { recSeq: latest.current.recSeq };
    setRemoteCommandBlocked(true);
    clearRecordingLimit();
    invalidateStart();
    // force 同时覆盖 MediaRecorder 已进入 stopping 但尚未产生 blob
    // 的窄窗；Recorder 会清 chunks、拒绝 stop Promise 并关所有 track。
    recRef.current.discardActive({ force: true });
    armedMeta.current = null;
    activeKind.current = null;
    if (mountedRef.current) {
      setRecActive(false);
      setStarting(false);
      setMicError(false);
      setSaveError(false);
    }
    if (meta) postInactive(meta.turnKey, meta.sessionId);
  }, [clearRecordingLimit, invalidateStart, postInactive]);

  const permitIsCurrent = useCallback((permit: StartPermit): boolean => {
    const now = latest.current;
    if (!mountedRef.current || permit.generation !== startGeneration.current || !now.connectionReady || now.suspended || now.stopRequested) return false;
    if (now.sessionId !== permit.sessionId || now.turnKey !== permit.turnKey) return false;
    if (permit.kind === "remote") {
      if (now.recSeq !== permit.recSeq || now.commandSeq !== permit.commandSeq) return false;
      return now.recording === "armed" || now.recording === "recording";
    }
    // 自助许可:发线索/服务端 wseq 重签等普通游标更新(仅 commandSeq 变)不作废老人
    // 刚按下的"开始回答"——否则权限弹窗期间来一条线索,点击就静默无响应。
    // 只有新远端录音命令(recSeq 变)、明确停止(recording 离开 idle)或换题才作废。
    if (now.recSeq !== permit.recSeq) return false;
    return now.selfStartAllowed && (now.recording ?? "idle") === "idle";
  }, []);

  const expirePermit = useCallback((permit: StartPermit, showError: boolean) => {
    if (permit.generation !== startGeneration.current) return;
    if (recRef.current.active) recRef.current.discardActive();
    activeKind.current = null;
    invalidateStart();
    armedMeta.current = null;
    if (permit.kind === "remote") {
      blockedRemote.current = { recSeq: permit.recSeq };
      if (mountedRef.current) setRemoteCommandBlocked(true);
    }
    if (mountedRef.current) {
      setRecActive(false);
      if (showError) setMicError(true);
    }
  }, [invalidateStart]);

  const reportStartFailure = useCallback((permit: StartPermit, failureCode: PatientRecFailureCode) => {
    const failure = claimRecordingStartFailure(permit, permitIsCurrent(permit), failureCode);
    if (!failure) return;
    // 老人端先物理关麦并显示平和提示；只有服务器 ACK 后，BC 才作为控制台拉取投影的唤醒信号。
    expirePermit(permit, true);
    void persistRec(false, permit.turnKey, permit.sessionId, failure).then((committed) => {
      if (committed) bus.post(committed);
    });
  }, [expirePermit, permitIsCurrent, persistRec]);

  const launchStart = useCallback(async (kind: StartKind) => {
    const now = latest.current;
    if (!mountedRef.current || !deviceLease.current || !outboxReady.current || !now.connectionReady
        || recRef.current.active || savingRef.current || pendingSave.current || startingRef.current) return;
    patientPauseDiscardRequested.current = false;

    // 新许可接管旧的 getUserMedia 单飞。旧流晚到会在 Recorder 内按代际立即关 track；
    // 新许可若复用了旧 promise，待其安全退场后最多再发起一次新的 getUserMedia。
    recRef.current.cancelPendingStart();
    const permit: StartPermit = {
      generation: ++startGeneration.current,
      kind,
      sessionId: now.sessionId,
      turnKey: now.turnKey,
      recSeq: now.recSeq,
      commandSeq: now.commandSeq,
    };
    if (!permitIsCurrent(permit)) return;
    activePermit.current = permit;

    startingRef.current = true;
    setStarting(true);
    setMicError(false);

    const timeoutId = window.setTimeout(() => {
      reportStartFailure(permit, classifyRecordingStartFailure({ kind: "timeout" }));
    }, MIC_START_TIMEOUT_MS);
    startTimeout.current = timeoutId;

    let failurePhase: "authorization" | "microphone" = "authorization";
    try {
      // 每一次真正调用 getUserMedia 前都向服务器重查：撤回、授权变更、
      // 场次暂停/终态或准入失效均不得弹出浏览器麦克风权限。
      const authorization = await api.recordingAuthorization(permit.sessionId, { device: true });
      if (!permitIsCurrent(permit)) return;
      if (!authorizesMicrophoneStart(authorization)) {
        reportStartFailure(permit, classifyRecordingStartFailure({ kind: "authorization" }));
        return;
      }
      armedMeta.current = {
        sessionId: now.sessionId,
        turnKey: now.turnKey,
        containsDirectIdentifier: now.containsDirectIdentifier ?? false,
      };
      failurePhase = "microphone";
      let started = await recRef.current.start();
      if (!started && permitIsCurrent(permit)) started = await recRef.current.start();

      if (!started) {
        reportStartFailure(permit, "microphone_start_failed");
        return;
      }
      if (!permitIsCurrent(permit)) {
        // 权限流恰在 React 应用 pause/stopped/断线的同一拍落地：不登记、不保存，直接关麦。
        // 若另一路已进入 stopAndSave，MediaRecorder.state 会先变 inactive；此时让保存链收尾，
        // 不用 discardActive 清空它正在等待 onstop 的 chunks。
        if (recRef.current.active) recRef.current.discardActive();
        if (activePermit.current === permit) activePermit.current = null;
        return;
      }

      const activeCommitted = await persistRec(true, permit.turnKey, permit.sessionId);
      if (!activeCommitted) {
        reportStartFailure(permit, classifyRecordingStartFailure({ kind: "authorization" }));
        return;
      }
      if (!permitIsCurrent(permit)) {
        // 许可在 active 请求往返期间被撤回时，不得把过期 active 发到本机快通道；
        // 若服务器已落 active，再排一个 false 保证压过并发的早期收麦请求。
        postInactive(permit.turnKey, permit.sessionId);
        if (recRef.current.active) recRef.current.discardActive();
        return;
      }
      bus.post(activeCommitted);
      setRecActive(true);
      activeKind.current = permit.kind;
      setMicError(false);
      if (permit.kind === "remote") consumedRemote.current = { recSeq: permit.recSeq };
      clearRecordingLimit();
      recordingLimit.current = window.setTimeout(() => {
        recordingLimit.current = null;
        if (recRef.current.active) void stopAndSave();
      }, latest.current.maxRecordingMs ?? MAX_RECORDING_MS);
    } catch (error) {
      reportStartFailure(permit, classifyRecordingStartFailure(
        failurePhase === "authorization" ? { kind: "authorization" } : { kind: "microphone", error },
      ));
    } finally {
      clearTimeout(timeoutId);
      if (startTimeout.current === timeoutId) startTimeout.current = null;
      if (permit.generation === startGeneration.current) {
        startingRef.current = false;
        if (activePermit.current === permit) activePermit.current = null;
        if (mountedRef.current) setStarting(false);
      }
    }
  }, [permitIsCurrent, persistRec, postInactive, clearRecordingLimit, reportStartFailure, stopAndSave]);

  // 老人端自助开录(点击"开始回答")与远端 arm 共用同一个可撤销启动门。
  const startNow = useCallback(async () => { await launchStart("self"); }, [launchStart]);

  // 自助录音不会产生 remote recording/recSeq 边沿；授权撤回、锁分或进入
  // thanks/wrapup 时必须独立撤销许可并保存已录内容，不能等下一个远程命令。
  useEffect(() => {
    const action = recordingRevocationAction({
      stopRequested,
      selfStartAllowed,
      pendingKind: activePermit.current?.kind ?? null,
      activeKind: activeKind.current,
      microphoneActive: recRef.current.active,
    });
    if (action === "none") return;
    invalidateStart();
    if (action === "stop-and-save") void stopAndSave();
  }, [invalidateStart, selfStartAllowed, stopAndSave, stopRequested]);

  // 先处理换题，再处理同一 render 中的新远端命令；否则旧题的清理 effect 会误杀新题启动。
  useEffect(() => {
    invalidateStart();
    if (recRef.current.active && armedMeta.current && armedMeta.current.turnKey !== turnKey) {
      void stopAndSave();
    }
    setMicError(false);
    // 上传失败不能因操作端跳题而消失；只有同一 pending 真正上传成功才能清错误。
    if (!pendingSave.current) setSaveError(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turnKey]);

  // 停麦是"真值边沿"不是"电平":自助录音期间 cursor.recording 恒为 idle,发线索/服务端
  // wseq 重签/快照重发都只改 commandSeq——若按电平处理,这些普通更新会把正在进行的自助
  // 录音掐断入库(半段/静默音频进转写判分链,复审确证)。只有 recording 值或 recSeq 真正
  // 变化才是研究者的新停止意图。
  const lastRemoteEdge = useRef<{ recording: string; recSeq?: number }>({
    recording: opts.recording ?? "idle", recSeq: opts.recSeq,
  });
  useEffect(() => {
    const target = recording ?? "idle";
    const prevEdge = lastRemoteEdge.current;
    const isEdge = target !== prevEdge.recording || recSeq !== prevEdge.recSeq;
    lastRemoteEdge.current = { recording: target, recSeq };
    if (target === "idle" || target === "stopped") {
      if (!isEdge) return;
      // 已消费/已封存的 recSeq 保持记账(consumedRemote/blockedRemote 不清):同一条
      // armed 哪怕经旧快照还魂重现也永远不能再开麦;新 arm 必带新 recSeq。
      setRemoteCommandBlocked(false);
      invalidateStart();
      if (recRef.current.active) void stopAndSave();
      return;
    }
    if (!connectionReady) return;
    const blocked = blockedRemote.current;
    if (blocked) {
      if (blocked.recSeq === recSeq) return;
      blockedRemote.current = null;
      setRemoteCommandBlocked(false);
    }
    if (consumedRemote.current?.recSeq === recSeq) return;
    if ((target === "armed" || target === "recording") && !recRef.current.active && !savingRef.current && !pendingSave.current) {
      // 挂载首帧的录音快照可能已陈旧；只有研究者的新 recSeq/wseq 才能开麦。
      if (recording === initialRemote.current.recording && recSeq === initialRemote.current.recSeq && commandSeq === initialRemote.current.commandSeq) return;
      const pendingPermit = activePermit.current;
      if (pendingPermit && pendingPermit.kind === "remote"
          && (pendingPermit.recSeq !== recSeq || pendingPermit.commandSeq !== commandSeq || pendingPermit.turnKey !== turnKey)) {
        invalidateStart();
      }
      void launchStart("remote");
    }
  }, [recording, recSeq, commandSeq, turnKey, connectionReady, saving,
      invalidateStart, launchStart, stopAndSave]);

  // 场次级暂停(session.paused,无游标边沿可依):同断线一样立即封存许可并停麦(热麦红线)。
  useEffect(() => {
    if (!suspended) return;
    blockedRemote.current = { recSeq };
    setRemoteCommandBlocked(true);
    setMicError(false);
    invalidateStart();
    if (recRef.current.active) void stopAndSave();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [suspended, invalidateStart, stopAndSave]);

  // 链路中断时不隐藏热麦克风：在途启动立即失效，已录内容关麦后走原保存链。
  useEffect(() => {
    if (connectionReady) return;
    blockedRemote.current = { recSeq };
    setRemoteCommandBlocked(true);
    setMicError(false);
    invalidateStart();
    if (recRef.current.active) void stopAndSave();
  }, [connectionReady, recSeq, commandSeq, invalidateStart, stopAndSave]);

  // 卸载兜底:切屏/换场次时若还在录,立即停麦并按 arm 时锁存的元数据保存;
  // getUserMedia 在途的由 dispose 落地即关——两条路都不留热麦克风。
  const stopRef = useRef(stopAndSave);
  stopRef.current = stopAndSave;
  shutdownRef.current = async () => {
    outboxReady.current = false;
    startGeneration.current += 1;
    activePermit.current = null;
    activeKind.current = null;
    recRef.current.cancelPendingStart();
    clearRecordingLimit();
    if (recRef.current.active) await stopRef.current();
    while (saveOperation.current || outboxRecovery.current) {
      const save = saveOperation.current;
      const recovery = outboxRecovery.current;
      await Promise.allSettled([save, recovery].filter((value): value is Promise<void> => value !== null));
      if (saveOperation.current === save) saveOperation.current = null;
      if (outboxRecovery.current === recovery) outboxRecovery.current = null;
    }
    recRef.current.dispose();
  };
  useEffect(() => {
    const rec = recRef.current;
    return () => {
      if (rec.active) void stopRef.current();
      else {
        rec.dispose();
        // React StrictMode 会执行一次 setup→cleanup→setup；给第二次 setup 留一个未 dispose 的录音器。
        if (recRef.current === rec) recRef.current = new Recorder();
      }
    };
  }, []);

  return {
    stopAndSave, discardForPatientPause, startNow, retrySave, saving, canRetry, recActive, micError,
    saveError, starting, remoteCommandBlocked, blockReason,
  };
}
