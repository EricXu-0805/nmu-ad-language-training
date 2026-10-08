import { Alert } from "../../components/Alert";
import { Button } from "../../components/Button";
import { StatusPill } from "../../components/StatusPill";
import type { WarmupAction, WarmupTopic, Week1Warmup, Week1WarmupContent } from "../../week1Warmup";

export function Week1WarmupCard({ content, contentError, reloadContent, warmup, disabled, busy,
  uncertain, problem, act, retry }: {
  content: Week1WarmupContent | null; contentError: string | null; reloadContent: () => void;
  warmup?: Week1Warmup; disabled: boolean; busy: boolean; uncertain: boolean; problem: string | null;
  act: (action: WarmupAction, topic?: WarmupTopic) => void; retry: () => void;
}) {
  const selected = content?.topics.find((topic) => topic.id === warmup?.topic);
  const blocked = disabled || busy || uncertain;
  return <section className="form-section rapport-console-card" aria-label="训练前互动（可选）">
    <div className="form-section-header"><div><p className="page-kicker">独立可选 · 手动开始</p><h3>训练前互动</h3></div>
      <StatusPill tone={warmup && warmup.beat !== "done" ? "primary" : "muted"}>{warmup?.beat === "done" ? "互动已结束" : warmup ? "互动中" : "按需使用"}</StatusPill>
    </div>
    <p>需要时，选一个话题和老人轻松聊一句。不需要可以直接进行下方关系建立。</p>
    <p className="muted">由工作人员听取回答，再决定是否回应或结束。此模块不录音、不转写、不判分；不会自动开始或进入下一题。</p>
    {contentError && <Alert tone="warn" title="互动内容暂不可用" actions={<Button onClick={reloadContent}>重新加载互动内容</Button>}>{contentError}</Alert>}
    {!content && !contentError && <p role="status">正在加载互动话题…</p>}
    {content && <div className="row wrap">{content.topics.map((topic) => <Button key={topic.id}
      variant={selected?.id === topic.id ? "primary" : "secondary"} disabled={blocked || Boolean(warmup && warmup.beat !== "done")}
      onClick={() => act("start", topic.id)}>{topic.label}</Button>)}</div>}
    {warmup && warmup.beat !== "done" && <p className="muted">想换个话题时，请先结束当前互动。</p>}
    {warmup && selected && <div role="status" aria-live="polite">
      <p><strong>{selected.label}：</strong>{warmup.beat === "reply" ? selected.reply : selected.ask}</p>
      <p className="muted">{warmup.beat === "done" ? "正式对话的位置没有改变。确认准备好后，再继续关系建立。"
        : "已向老人端发送。若平板刚刷新、断线或没有听见，请点“再播放当前这句”。"}</p>
    </div>}
    {warmup && <div className="row wrap">
      {warmup.beat !== "done" ? <>
        <Button disabled={blocked} onClick={() => act("replay")}>再播放当前这句</Button>
        <Button disabled={blocked} onClick={() => act("reply")}>说一句简短回应</Button>
        <Button disabled={blocked} onClick={() => act("end")}>结束互动</Button>
      </> : <Button variant="primary" disabled={blocked} onClick={() => act("continue")}>继续关系建立</Button>}
    </div>}
    {busy && <p role="status">正在确认互动操作…</p>}
    {problem && <Alert tone="warn" title="互动操作需要核对" actions={uncertain
      ? <Button disabled={disabled || busy} onClick={retry}>核对并重试刚才的操作</Button> : undefined}>{problem}</Alert>}
  </section>;
}
