import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { blobStore } from "../audio/blobStore";
import { Recorder } from "../audio/recorder";
import { MicButton } from "../components/MicButton";
import { ImagePane, type Spotlight } from "../components/ImagePane";
import { findDouble, findSingle, useItemBankBundle } from "../content/bundle";
import { newAudioId, turnKey } from "../lib/ids";
import { bus } from "../sync/bus";
import type { CursorMsg } from "../sync/messages";
import type { SessionPlan } from "../types";
import { Centered } from "./Centered";

// 一屏一图一环节:呈现→录音→沉默后逐级线索。超大/高对比/无倒计时/无对错。
// ★组件树无任何画像来源;线索文本只从版本锁定的 bundle 查表,null 就留空,绝不拼接兜底。
export function PatientStage({ plan, cursor, sessionId }: { plan: SessionPlan | null; cursor?: CursorMsg; sessionId: string }) {
  const { bundle } = useItemBankBundle();
  const recRef = useRef<Recorder>(new Recorder());
  const [saving, setSaving] = useState(false);

  const item = plan && cursor ? plan.items[cursor.itemIdx] : undefined;
  const planTurn = item && cursor ? item.turns[cursor.turnIdx] : undefined;
  const role = planTurn?.response_role ?? "命名";
  const tk = item && planTurn ? turnKey(item.item_id, planTurn.turn_seq) : "";

  const stopAndSave = useCallback(async () => {
    if (!recRef.current.active || saving) return;
    setSaving(true);
    try {
      const rec = await recRef.current.stop();
      const rid = newAudioId();
      await blobStore.put(rid, rec.blob);                       // 本地 IndexedDB 兜底副本
      await api.createAudio({ raw_audio_id: rid, session_id: sessionId });
      await api.uploadAudioBlob(rid, rec.blob).catch(() => {}); // 字节落库(本机);失败仍有本地副本
      bus.post({ type: "audioSaved", rawAudioId: rid, durationSeconds: rec.durationSeconds, turnKey: tk });
    } catch { /* 录音失败对老人不可见:错误宽容 */ }
    finally { setSaving(false); }
  }, [sessionId, tk, saving]);

  // 研究者端 arm → 自动开始;转 idle/stopped → 停止并保存(老人无需按任何键)
  useEffect(() => {
    const target = cursor?.recording ?? "idle";
    if (target === "armed" && !recRef.current.active) {
      recRef.current.start().catch(() => { /* 权限/设备失败静默,停在任务屏 */ });
    } else if ((target === "idle" || target === "stopped") && recRef.current.active) {
      void stopAndSave();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cursor?.recording]);

  if (!item || !planTurn) {
    return <Centered><p className="question">请稍候…</p></Centered>;
  }

  const spotlight: Spotlight = role.startsWith("左") ? "left" : role.startsWith("右") ? "right" : role === "关系识别" ? "both" : "none";
  const question = role.includes("作用") ? "它是做什么用的呢？" : role === "关系识别" ? "它们之间有什么关系呢？" : "请看这张图片，这是什么？";
  const cueText = lookupCue(bundle, item.item_id, item.task_type, role, cursor?.cueLevel ?? 0);

  return (
    <Centered>
      <ImagePane imageId={item.image_id} spotlight={spotlight} alt="题目图片" />
      <p className="question">{question}</p>
      {cueText && <p className="cue card" style={{ maxWidth: "80vw" }}>{cueText}</p>}
      <MicButton state={cursor?.recording ?? "idle"} onStop={stopAndSave} />
    </Centered>
  );
}

// 线索查表(纯):单要素 1→cues1 / 2→cues2 / 3→tell_answer;双要素作用→left/right_function_cue,关系→relation_cue。
function lookupCue(bundle: ReturnType<typeof useItemBankBundle>["bundle"], itemId: string, taskType: string, role: string, level: number): string | null {
  if (!bundle || level < 1) return null;
  if (taskType === "单要素") {
    const s = findSingle(bundle, itemId);
    if (!s) return null;
    if (level === 1) return s.cues["1"]?.text ?? null;
    if (level === 2) return s.cues["2"]?.text ?? null;
    return s.tell_answer ?? null; // level 3
  }
  if (taskType === "双要素") {
    const d = findDouble(bundle, itemId);
    if (!d) return null;
    if (role === "左作用") return d.left_function_cue ?? null;
    if (role === "右作用") return d.right_function_cue ?? null;
    if (role === "关系识别") return d.relation_cue ?? null;
    return null; // 命名环节无预置线索文本
  }
  return null;
}
