import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { blobStore } from "../audio/blobStore";
import { Recorder } from "../audio/recorder";
import { bus } from "../sync/bus";
import type { RecState } from "../sync/messages";
import { newAudioId } from "../lib/ids";

// 老人端 VOX 录音共享逻辑(任务屏/关系建立屏共用):
// 研究者端 arm → 自动开始;转 idle/stopped → 停止并保存(老人无需按任何键)。
// 保存链:IndexedDB 兜底副本 → 服务端登记元数据 → 字节上传 → 双通道回报 audioSaved。
export function useVoxRecorder(opts: {
  sessionId: string;
  recording: RecState | undefined;
  recSeq?: number;
  turnKey: string;
  containsDirectIdentifier?: boolean;
}) {
  const recRef = useRef<Recorder>(new Recorder());
  // ★元数据在 arm 时刻锁存:录音中操作端直接跳节/跳题时,stop 消息与新指针同帧到达,
  // 若用 stop 时刻的 props,这段音频会归错环节——自我介绍段还会丢 contains_direct_identifier 红线标记。
  const armedMeta = useRef<{ turnKey: string; containsDirectIdentifier: boolean } | null>(null);
  const firstRun = useRef(true);
  const [saving, setSaving] = useState(false);
  const { sessionId, recording, recSeq, turnKey, containsDirectIdentifier } = opts;

  const stopAndSave = useCallback(async () => {
    if (!recRef.current.active || saving) return;
    setSaving(true);
    try {
      const meta = armedMeta.current ?? { turnKey, containsDirectIdentifier: containsDirectIdentifier ?? false };
      const rec = await recRef.current.stop();
      const rid = newAudioId();
      await blobStore.put(rid, rec.blob);                       // 本地 IndexedDB 兜底副本
      await api.createAudio({ raw_audio_id: rid, session_id: sessionId, contains_direct_identifier: meta.containsDirectIdentifier });
      await api.uploadAudioBlob(rid, rec.blob).catch(() => {}); // 字节落库(本机);失败仍有本地副本
      const saved = { rawAudioId: rid, durationSeconds: rec.durationSeconds, turnKey: meta.turnKey, containsDirectIdentifier: meta.containsDirectIdentifier };
      bus.post({ type: "audioSaved", ...saved });               // 同机秒推
      api.putLiveState("audioSaved", saved).catch(() => {});    // 跨设备回报(操作端轮询取)
    } catch { /* 录音失败对老人不可见:错误宽容 */ }
    finally { armedMeta.current = null; setSaving(false); }
  }, [sessionId, turnKey, containsDirectIdentifier, saving]);

  useEffect(() => {
    const target = recording ?? "idle";
    const isFirst = firstRun.current;
    firstRun.current = false;
    if (target === "armed" && !recRef.current.active) {
      // ★挂载首帧的 armed 是镜像/轮询恢复的陈旧快照(如老人自停后刷新页面)——
      // 绝不凭快照自动开麦(无人知晓的热麦克风)。研究者重新示意即产生新 recSeq,正常开录。
      if (isFirst) return;
      armedMeta.current = { turnKey, containsDirectIdentifier: containsDirectIdentifier ?? false };
      recRef.current.start().catch(() => { /* 权限/设备失败静默,停在当前屏 */ });
    } else if ((target === "idle" || target === "stopped") && recRef.current.active) {
      void stopAndSave();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recording, recSeq]);

  // 卸载兜底:切屏/换场次时若还在录,立即停麦并按 arm 时锁存的元数据保存——不留热麦克风,也不丢老人的话。
  const stopRef = useRef(stopAndSave);
  stopRef.current = stopAndSave;
  useEffect(() => () => { if (recRef.current.active) void stopRef.current(); }, []);

  return { stopAndSave };
}
