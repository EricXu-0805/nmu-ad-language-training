export interface RuntimeRevisionFloor {
  sessionId: string;
  revision: number;
}

/**
 * 跨 session 隔离的单调棘轮：候选 revision 必须属于当前记录的 sessionId 才会
 * 生效，且只升不降。旧 session 的迟到回执因为 sessionId 对不上会被直接忽略，
 * 不会污染新 session 的下限。纯函数，调用方（Promise continuation 或渲染体）
 * 决定何时调用——它自己不引入任何异步或渲染时机。
 */
export function ratchetRevisionFloor(
  current: RuntimeRevisionFloor,
  candidateSessionId: string,
  revision: number,
): RuntimeRevisionFloor {
  if (current.sessionId !== candidateSessionId) return current;
  return revision > current.revision ? { sessionId: candidateSessionId, revision } : current;
}
