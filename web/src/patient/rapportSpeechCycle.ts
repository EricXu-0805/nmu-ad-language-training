/** A new idle recSeq is an explicit presentation request; arm/stop acknowledgments are not. */
export type RapportSpeechCommand = { content: string; recSeq: number | undefined; recording: string; wseq: number | undefined };
export type RapportSpeechCycle = { command: RapportSpeechCommand; key: string } | null;
export function observeRapportSpeech(previous: RapportSpeechCycle, command: RapportSpeechCommand): Exclude<RapportSpeechCycle, null> {
  const replay = !previous || previous.command.content !== command.content
    || (command.recording === "idle" && command.recSeq !== previous.command.recSeq);
  return {
    command,
    key: replay ? `${command.content}@${command.wseq ?? ""}` : previous.key,
  };
}
