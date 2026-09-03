// 第1周多轮对话的纯逻辑:机器人说完这句之后该干什么、大约等多久。
// 不碰 DOM/网络,便于单测;RelationshipConsoleScreen 只做调用与定时器管理。

export type AfterReplyAction = "rearm" | "advance" | "section_done" | "none";

export interface AfterReplyInput {
  autoReply: boolean;        // 研究者开着「老人说完自动回应」
  autoOpenHere: boolean;     // 本节整节开放自动回应
  final: boolean;            // 服务端判定本问已是最后一轮
  invitesMore: boolean;      // 这句把话头递回老人(现编追问/j1 请重说)
  qIdx: number;
  questionCount: number;
}

// 续麦的前提是这句真的在邀请老人接着说:收束句(「那我们聊点别的吧」「好的，谢谢您
// 告诉我」)之后开麦,老人会收到互相矛盾的指令,所以它们一律按本问聊完处理。
export function afterReplyAction(input: AfterReplyInput): AfterReplyAction {
  if (!input.autoReply || !input.autoOpenHere) return "none";
  if (!input.final && input.invitesMore) return "rearm";
  if (input.qIdx < input.questionCount - 1) return "advance";
  return "section_done";
}

// 云 TTS 1.0 倍速约 3.5 字/秒;加起播余量(老人端 800ms 轮询 + 呈现投影 + 未缓存
// 句的云合成),夹在 4~16 秒。只是等机器人把话说完再开麦的估计,不是精确同步——
// 宁可多等一秒也别掐断小语正在说的话。
export function speechDelayMs(text: string): number {
  const chars = Array.from(text).length;
  return Math.min(16000, Math.max(4000, 3000 + 300 * chars));
}

// 换问后开麦要等新问句念完。问句就在冻结脚本里,长度是已知的——用同一套估时,
// 别拿一个比任何一条问句都短的固定值(机构环境四问 15~22 字,固定 7 秒全都不够)。
export function nextQuestionArmDelayMs(nextAsk: string | null | undefined): number {
  return speechDelayMs(nextAsk ?? "");
}

export function roundLabel(round: number, maxRounds: number): string {
  return `第 ${round} / ${maxRounds} 轮`;
}
