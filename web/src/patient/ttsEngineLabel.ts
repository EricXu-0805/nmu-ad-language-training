/**
 * Tooltip for the corner speech toggle.
 *
 * Only the legacy plane speaks with the device's own voice, so only there may
 * the tooltip name one — or admit there is none.  On the server-hosted planes
 * the audio is synthesized server-side and streamed back, so naming a local
 * voice (or claiming the machine has none) describes something that is not
 * producing the sound the patient hears.
 */
export function ttsToggleTitle(
  on: boolean,
  mode: string,
  currentVoiceName: string | null | undefined,
): string {
  const state = `语音${on ? "开" : "关"}`;
  if (mode !== "legacy") return state;
  return `${state}${currentVoiceName ? ` · 音色:${currentVoiceName}` : " · 本机无中文语音"}`;
}

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
