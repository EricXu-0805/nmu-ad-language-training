import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { blobStore } from "../audio/blobStore";
import { Recorder } from "../audio/recorder";
import { bus } from "../sync/bus";
import type { RecState } from "../sync/messages";
import { newAudioId } from "../lib/ids";

// 老人端 VOX 录音共享逻辑(任务屏/关系建立屏共用):
// 研究者端 arm → 自动开始;转 idle/stopped → 停止并保存(老人无需按任何键);
// 自助模式:老人自己点"开始回答"(startNow)/"我说好了"(stopAndSave)。
// 保存链:IndexedDB 兜底副本 → 服务端登记元数据 → 字节上传 → 双通道回报 audioSaved。
// 麦克风开/关随时经 patientRec 上报——自助开录时这是操作端唯一的感知与远程停止依据。
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
  // recActive:录音器活跃态的响应式镜像。老人端自助开录不经过操作端游标,
  // MicButton 不能只看 cursor.recording,还要看本地真在录没有。
  const [recActive, setRecActive] = useState(false);
  const [micError, setMicError] = useState(false);
  // 保存失败必须对老人可见(平和措辞):否则自助模式下回答静默丢失、无人知晓。
  const [saveError, setSaveError] = useState(false);
  const { sessionId, recording, recSeq, turnKey, containsDirectIdentifier } = opts;

  const postRec = useCallback((active: boolean, tk: string) => {
    const m = { active, turnKey: tk, sessionId };
    bus.post({ type: "patientRec", ...m });
    api.putLiveState("patientRec", m).catch(() => {});
  }, [sessionId]);

  const stopAndSave = useCallback(async () => {
    if (!recRef.current.active || saving) return;
    setSaving(true);
    setRecActive(false);
    const meta = armedMeta.current ?? { turnKey, containsDirectIdentifier: containsDirectIdentifier ?? false };
    try {
      const rec = await recRef.current.stop();
      postRec(false, meta.turnKey);
      const rid = newAudioId();
      await blobStore.put(rid, rec.blob);                       // 本地 IndexedDB 兜底副本
      await api.createAudio({ raw_audio_id: rid, session_id: sessionId, contains_direct_identifier: meta.containsDirectIdentifier });
      await api.uploadAudioBlob(rid, rec.blob).catch(() => {}); // 字节落库(本机);失败仍有本地副本
      const saved = { rawAudioId: rid, durationSeconds: rec.durationSeconds, turnKey: meta.turnKey, sessionId, containsDirectIdentifier: meta.containsDirectIdentifier };
      bus.post({ type: "audioSaved", ...saved });               // 同机秒推
      api.putLiveState("audioSaved", saved).catch(() => {});    // 跨设备回报(操作端轮询取)
      setSaveError(false);
    } catch {
      // 停麦成功但落库/登记失败:麦已关(不留热麦),给老人平和提示以便再说一次。
      postRec(false, meta.turnKey);
      setSaveError(true);
    }
    finally { armedMeta.current = null; setSaving(false); }
  }, [sessionId, turnKey, containsDirectIdentifier, saving, postRec]);

  // 老人端自助开录(点击"开始回答"):等价于一次本地 arm。开麦失败给可见提示——
  // "点了没反应"对测试者和老人都是最糟反馈;但提示语平和,不红不闪。
  // starting:getUserMedia 在途(浏览器权限弹窗期间可长可久),按钮必须立刻有反馈;
  // 12 秒还没落地按失败提示(权限框被忽略/设备卡死,不能让老人对着静止按钮干等)。
  const [starting, setStarting] = useState(false);
  const startNow = useCallback(async () => {
    if (recRef.current.active || saving || starting) return;
    setMicError(false);
    setSaveError(false);
    setStarting(true);
    armedMeta.current = { turnKey, containsDirectIdentifier: containsDirectIdentifier ?? false };
    const timeout = window.setTimeout(() => { setStarting(false); setMicError(true); }, 12000);
    try {
      await recRef.current.start();
      clearTimeout(timeout);
      if (recRef.current.active) {
        setRecActive(true);
        setMicError(false);
        postRec(true, turnKey);
      }
    } catch {
      clearTimeout(timeout);
      armedMeta.current = null;
      setMicError(true);
    } finally {
      setStarting(false);
    }
  }, [turnKey, containsDirectIdentifier, saving, starting, postRec]);

  useEffect(() => {
    const target = recording ?? "idle";
    const isFirst = firstRun.current;
    firstRun.current = false;
    if (target === "armed" && !recRef.current.active) {
      // ★挂载首帧的 armed 是镜像/轮询恢复的陈旧快照(如老人自停后刷新页面)——
      // 绝不凭快照自动开麦(无人知晓的热麦克风)。研究者重新示意即产生新 recSeq,正常开录。
      if (isFirst) return;
      const tk = turnKey;
      armedMeta.current = { turnKey: tk, containsDirectIdentifier: containsDirectIdentifier ?? false };
      recRef.current.start().then(() => {
        if (recRef.current.active) { setRecActive(true); setMicError(false); postRec(true, tk); }
      }).catch(() => { /* 权限/设备失败静默,停在当前屏;研究者端有 8 秒看门狗 */ });
    } else if ((target === "idle" || target === "stopped") && recRef.current.active) {
      void stopAndSave();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recording, recSeq]);

  // 自助录音跨题守卫:自助开录不改游标,操作端跳题时 recording 值 idle→idle 不变、
  // 上面的 effect 不会触发——这里按 turnKey 变化强制收尾,录音绝不跨题延续。
  // (元数据用的是 armedMeta 锁存值,音频仍归属开录时的环节。)
  // 换题同时清掉上一题的失败提示,不让"麦克风没有打开"驻留到新题。
  useEffect(() => {
    if (recRef.current.active && armedMeta.current && armedMeta.current.turnKey !== turnKey) {
      void stopAndSave();
    }
    setMicError(false);
    setSaveError(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turnKey]);

  // 卸载兜底:切屏/换场次时若还在录,立即停麦并按 arm 时锁存的元数据保存;
  // getUserMedia 在途的由 dispose 落地即关——两条路都不留热麦克风。
  const stopRef = useRef(stopAndSave);
  stopRef.current = stopAndSave;
  useEffect(() => {
    const rec = recRef.current;
    return () => {
      if (rec.active) void stopRef.current();
      else rec.dispose();
    };
  }, []);

  return { stopAndSave, startNow, saving, recActive, micError, saveError, starting };
}
