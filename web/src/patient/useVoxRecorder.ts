import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { blobStore } from "../audio/blobStore";
import { Recorder } from "../audio/recorder";
import { bus } from "../sync/bus";
import type { RecState } from "../sync/messages";
import { newAudioId } from "../lib/ids";

interface ArmedMeta {
  sessionId: string;
  turnKey: string;
  containsDirectIdentifier: boolean;
}

interface PendingSave {
  rawAudioId: string;
  blob: Blob;
  durationSeconds: number;
  meta: ArmedMeta;
  localSaved: boolean;
  registered: boolean;
}

type StartKind = "remote" | "self";

interface StartPermit {
  generation: number;
  kind: StartKind;
  sessionId: string;
  turnKey: string;
  recSeq?: number;
  commandSeq?: number;
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
}) {
  const recRef = useRef<Recorder>(new Recorder());
  // ★元数据在 arm 时刻锁存:录音中操作端直接跳节/跳题时,stop 消息与新指针同帧到达,
  // 若用 stop 时刻的 props,这段音频会归错环节——自我介绍段还会丢 contains_direct_identifier 红线标记。
  const armedMeta = useRef<ArmedMeta | null>(null);
  // 上传失败时保留同一份 blob/id 重试，不能先报 audioSaved 再让操作端自动推进。
  const pendingSave = useRef<PendingSave | null>(null);
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
  const { sessionId, recording, recSeq, commandSeq, turnKey, containsDirectIdentifier, connectionReady = true, suspended = false } = opts;
  const latest = useRef({ sessionId, recording, recSeq, commandSeq, turnKey, containsDirectIdentifier, connectionReady, suspended });
  latest.current = { sessionId, recording, recSeq, commandSeq, turnKey, containsDirectIdentifier, connectionReady, suspended };
  const mountedRef = useRef(true);
  const startGeneration = useRef(0);
  const startTimeout = useRef<number | null>(null);
  const startingRef = useRef(false);
  const activePermit = useRef<StartPermit | null>(null);

  // 这项 effect 必须排在所有启动 effect 前：StrictMode 第二次 setup 时先恢复 mounted 真值。
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      startGeneration.current += 1;
      activePermit.current = null;
      recRef.current.cancelPendingStart();
      clearRecordingLimit();
      if (startTimeout.current != null) clearTimeout(startTimeout.current);
      startTimeout.current = null;
      startingRef.current = false;
    };
  }, [clearRecordingLimit]);

  const postRec = useCallback((active: boolean, tk: string, lockedSessionId = sessionId) => {
    const m = { active, turnKey: tk, sessionId: lockedSessionId };
    bus.post({ type: "patientRec", ...m });
    api.putLiveState("patientRec", m).catch(() => {});
  }, [sessionId]);

  const commitPending = useCallback(async (pending: PendingSave) => {
    if (!pending.localSaved) {
      await blobStore.put(pending.rawAudioId, pending.blob); // 上传失败仍有 IndexedDB 兜底副本
      pending.localSaved = true;
    }

    if (!pending.registered) {
      try {
        await api.createAudio({
          raw_audio_id: pending.rawAudioId,
          session_id: pending.meta.sessionId,
          turn_key: pending.meta.turnKey,
          contains_direct_identifier: pending.meta.containsDirectIdentifier,
        });
      } catch (e) {
        // 请求响应丢失时服务端可能已经登记；重试遇到 409 只在确认
        // 场次、环节与标识红线全部一致时才视为幂等成功。
        if (!(e instanceof ApiError && e.status === 409)) throw e;
        const existing = await api.getAudio(pending.rawAudioId);
        if (existing.session_id !== pending.meta.sessionId
            || existing.turn_key !== pending.meta.turnKey
            || existing.contains_direct_identifier !== pending.meta.containsDirectIdentifier) throw e;
      }
      pending.registered = true;
    }

    // 这里必须 await 且不得吞错。只有元数据与字节都落库成功，才允许发 audioSaved。
    await api.uploadAudioBlob(pending.rawAudioId, pending.blob);
    const saved = {
      rawAudioId: pending.rawAudioId,
      durationSeconds: pending.durationSeconds,
      turnKey: pending.meta.turnKey,
      sessionId: pending.meta.sessionId,
      containsDirectIdentifier: pending.meta.containsDirectIdentifier,
    };
    bus.post({ type: "audioSaved", ...saved });
    api.putLiveState("audioSaved", saved).catch(() => {});
    if (pendingSave.current === pending) pendingSave.current = null;
    setCanRetry(false);
    setSaveError(false);
  }, []);

  const stopAndSave = useCallback(async () => {
    if (!recRef.current.active || savingRef.current) return;
    clearRecordingLimit();
    savingRef.current = true;
    setSaving(true);
    setRecActive(false);
    const meta = armedMeta.current ?? { sessionId, turnKey, containsDirectIdentifier: containsDirectIdentifier ?? false };
    try {
      const rec = await recRef.current.stop();
      postRec(false, meta.turnKey, meta.sessionId);
      const pending: PendingSave = {
        rawAudioId: newAudioId(),
        blob: rec.blob,
        durationSeconds: rec.durationSeconds,
        meta,
        localSaved: false,
        registered: false,
      };
      pendingSave.current = pending;
      setCanRetry(true);
      await commitPending(pending);
    } catch {
      // 麦已关；若已经得到 blob，pendingSave 与 IndexedDB 副本均保留，允许重试同一段录音。
      recRef.current.discardActive();
      postRec(false, meta.turnKey, meta.sessionId);
      setSaveError(true);
    }
    finally {
      armedMeta.current = null;
      savingRef.current = false;
      setSaving(false);
    }
  }, [sessionId, turnKey, containsDirectIdentifier, postRec, commitPending, clearRecordingLimit]);

  const retrySave = useCallback(async () => {
    const pending = pendingSave.current;
    if (!pending || savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    try {
      await commitPending(pending);
    } catch {
      // 不清 pending、不清错误；下次仍重试同一 rawAudioId/blob，避免重复回答或错绑。
      setSaveError(true);
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
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

  const permitIsCurrent = useCallback((permit: StartPermit): boolean => {
    const now = latest.current;
    if (!mountedRef.current || permit.generation !== startGeneration.current || !now.connectionReady || now.suspended) return false;
    if (now.sessionId !== permit.sessionId || now.turnKey !== permit.turnKey) return false;
    if (permit.kind === "remote") {
      if (now.recSeq !== permit.recSeq || now.commandSeq !== permit.commandSeq) return false;
      return now.recording === "armed" || now.recording === "recording";
    }
    // 自助许可:发线索/服务端 wseq 重签等普通游标更新(仅 commandSeq 变)不作废老人
    // 刚按下的"开始回答"——否则权限弹窗期间来一条线索,点击就静默无响应。
    // 只有新远端录音命令(recSeq 变)、明确停止(recording 离开 idle)或换题才作废。
    if (now.recSeq !== permit.recSeq) return false;
    return (now.recording ?? "idle") === "idle";
  }, []);

  const expirePermit = useCallback((permit: StartPermit, showError: boolean) => {
    if (permit.generation !== startGeneration.current) return;
    invalidateStart();
    armedMeta.current = null;
    if (permit.kind === "remote") {
      blockedRemote.current = { recSeq: permit.recSeq };
      if (mountedRef.current) setRemoteCommandBlocked(true);
    }
    if (showError && mountedRef.current) setMicError(true);
  }, [invalidateStart]);

  const launchStart = useCallback(async (kind: StartKind) => {
    const now = latest.current;
    if (!mountedRef.current || !now.connectionReady || recRef.current.active || savingRef.current || pendingSave.current || startingRef.current) return;

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
    armedMeta.current = {
      sessionId: now.sessionId,
      turnKey: now.turnKey,
      containsDirectIdentifier: now.containsDirectIdentifier ?? false,
    };

    const timeoutId = window.setTimeout(() => {
      if (permit.generation !== startGeneration.current) return;
      expirePermit(permit, true);
    }, MIC_START_TIMEOUT_MS);
    startTimeout.current = timeoutId;

    try {
      let started = await recRef.current.start();
      if (!started && permitIsCurrent(permit)) started = await recRef.current.start();

      if (!started) {
        if (permitIsCurrent(permit)) expirePermit(permit, true);
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

      setRecActive(true);
      setMicError(false);
      if (permit.kind === "remote") consumedRemote.current = { recSeq: permit.recSeq };
      postRec(true, permit.turnKey, permit.sessionId);
      clearRecordingLimit();
      recordingLimit.current = window.setTimeout(() => {
        recordingLimit.current = null;
        if (recRef.current.active) void stopAndSave();
      }, MAX_RECORDING_MS);
    } catch {
      if (permitIsCurrent(permit)) expirePermit(permit, true);
    } finally {
      clearTimeout(timeoutId);
      if (startTimeout.current === timeoutId) startTimeout.current = null;
      if (permit.generation === startGeneration.current) {
        startingRef.current = false;
        if (activePermit.current === permit) activePermit.current = null;
        if (mountedRef.current) setStarting(false);
      }
    }
  }, [expirePermit, permitIsCurrent, postRec, clearRecordingLimit, stopAndSave]);

  // 老人端自助开录(点击"开始回答")与远端 arm 共用同一个可撤销启动门。
  const startNow = useCallback(async () => { await launchStart("self"); }, [launchStart]);

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

  return { stopAndSave, startNow, retrySave, saving, canRetry, recActive, micError, saveError, starting, remoteCommandBlocked };
}
