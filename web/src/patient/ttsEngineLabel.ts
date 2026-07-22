/** Researcher-facing truth about the engine that produced the current bytes. */
export function ttsEngineLabel(engineTag: string | null | undefined): string | null {
  const tag = engineTag?.trim();
  if (!tag || tag === "null-0") return null;
  const [provider, model, ...voiceParts] = tag.split("/");
  const voice = voiceParts.join("/");
  if (provider === "dashscope") {
    return `小语·云端 ${model || "DashScope"}${voice ? ` · ${voice}` : ""}`;
  }
  if (provider === "piper") {
    return `小语·本地 Piper${model ? ` · ${model}` : ""}`;
  }
  return `小语·服务端语音 · ${tag}`;
}
