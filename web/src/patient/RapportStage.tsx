import { useEffect } from "react";
import { useWeek1Script } from "../content/bundle";
import { MicButton } from "../components/MicButton";
import type { RapportMsg } from "../sync/messages";
import { speak } from "./tts";
import { useVoxRecorder } from "./useVoxRecorder";

// 第1周关系建立屏:对话模式(米色暖调、机器人头像、无图无线索梯无评分光环),刻意区别于任务模式防串屏。
// 显示操作端广播的固定话术;画像采集仅落本地(patientLocalProfile),此屏不 POST。
// 录音同任务屏 VOX:操作端 arm 才录;自我介绍段 containsDirectIdentifier 随消息带入登记(导出侧整段红线)。
// TTS 只给 speaker="机器人" 的节开口;"研究者"节是当面话术,屏上只显欢迎语、不朗读。
export function RapportStage({ rapportStep, sessionId }: { rapportStep?: RapportMsg; sessionId: string }) {
  const { script, error } = useWeek1Script();
  const section = script?.sections.find((s) => s.key === rapportStep?.sectionKey);
  const isRobot = section?.speaker === "机器人";
  const qIdx = rapportStep?.questionIdx ?? 0;
  const text = isRobot
    ? section?.questions?.[Math.min(qIdx, Math.max(0, (section.questions?.length ?? 1) - 1))]?.ask
      ?? section?.line
      ?? script?.generic_fallback_line
      ?? ""
    : "我们一起聊聊天，好吗？";

  // 小语开口:机器人节话术变了就读;研究者节/脚本未就绪/校验失败一律不读。
  useEffect(() => {
    if (script && isRobot && text) speak(text, { tag: `rapport:${section?.key ?? ""}:${qIdx}` });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [script, isRobot, text]);

  const recording = rapportStep?.recording ?? "idle";
  const { stopAndSave } = useVoxRecorder({
    sessionId,
    recording: rapportStep?.recording,
    recSeq: rapportStep?.recSeq,
    turnKey: `关系建立·${rapportStep?.sectionKey ?? ""}`,
    containsDirectIdentifier: rapportStep?.containsDirectIdentifier ?? false,
  });

  if (error) {
    // fail-closed:脚本 schema 校验失败,拒绝凭错误内容开场(对老人仍显平静文案)
    return (
      <div style={{ minHeight: "100vh", background: "#F3ECD9", display: "flex", alignItems: "center", justifyContent: "center" }}>
        <p className="question">请稍等一下，马上就好。</p>
      </div>
    );
  }

  return (
    <div style={{ minHeight: "100vh", background: "#F3ECD9", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: "var(--sp-6)", padding: "var(--sp-7)" }}>
      <div style={{ fontSize: 96 }} aria-hidden>🤖</div>
      <p className="question" style={{ maxWidth: "80vw" }}>{text}</p>
      {rapportStep?.assentGate && (
        <p className="cue" style={{ color: "var(--c-primary)" }}>（等您点头同意，我们再开始）</p>
      )}
      {(recording === "armed" || recording === "recording") && (
        <MicButton state={recording} onStop={stopAndSave} />
      )}
    </div>
  );
}

// 注:此文件不 import 任何评分模块;画像写入在 patientLocalProfile(仅本地)。
