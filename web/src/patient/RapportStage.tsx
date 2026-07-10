import { useWeek1Script } from "../content/bundle";
import type { RapportMsg } from "../sync/messages";

// 第1周关系建立屏:对话模式(米色暖调、机器人头像、无图无线索梯无评分光环),刻意区别于任务模式防串屏。
// 显示操作端广播的固定话术;画像采集仅落本地(patientLocalProfile),此屏不 POST。
export function RapportStage({ rapportStep }: { rapportStep?: RapportMsg }) {
  const { script } = useWeek1Script();
  const section = script?.sections.find((s) => s.section_key === rapportStep?.sectionKey) ?? script?.sections[0];
  const lines = section?.lines ?? [];
  const text = lines[Math.min(rapportStep?.questionIdx ?? 0, Math.max(0, lines.length - 1))] ?? script?.generic_fallback_line ?? "我们一起聊聊天，好吗？";

  return (
    <div style={{ minHeight: "100vh", background: "#F3ECD9", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: "var(--sp-6)", padding: "var(--sp-7)" }}>
      <div style={{ fontSize: 96 }} aria-hidden>🤖</div>
      <p className="question" style={{ maxWidth: "80vw" }}>{text}</p>
      {rapportStep?.assentGate && (
        <p className="cue" style={{ color: "var(--c-primary)" }}>（等您点头同意，我们再开始）</p>
      )}
    </div>
  );
}

// 注:此文件不 import 任何评分模块;画像写入在 patientLocalProfile(仅本地)。
