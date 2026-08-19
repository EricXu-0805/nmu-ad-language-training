import { useEffect, useState } from "react";
import { api } from "../api";
import { Button } from "../components/Button";
import { StatusPill } from "../components/StatusPill";

// 受控音频回放:blob 走带 PIN 的鉴权取回(不能用裸 <audio src>,内网模式会 401)。
// lazy:分析后台逐环节列表用——先出"播放录音"按钮,点了才拉 blob;否则进场一次并发拉几十段录音。
export function AuthenticatedAudio({ rawAudioId, lazy = false }: { rawAudioId: string; lazy?: boolean }) {
  const [src, setSrc] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [armed, setArmed] = useState(!lazy);

  useEffect(() => {
    if (!armed) return;
    let cancelled = false;
    let objectUrl: string | null = null;
    setSrc(null);
    setError(null);
    api.getAudioBlob(rawAudioId)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause));
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [rawAudioId, retry, armed]);

  if (!armed) return <Button onClick={() => setArmed(true)}>播放录音</Button>;
  if (error) {
    return (
      <span className="row wrap">
        <StatusPill tone="warn">回放未载入</StatusPill>
        <Button onClick={() => setRetry((value) => value + 1)}>重试回放</Button>
      </span>
    );
  }
  if (!src) return <StatusPill tone="muted">正在准备回放…</StatusPill>;
  return <audio controls preload="metadata" src={src} style={{ height: 44 }} />;
}
