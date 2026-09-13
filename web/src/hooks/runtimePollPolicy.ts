// 背景轮询失败的呈现决策。界面一亮错就会按「恢复失败」锁死人工面板,并向老人端
// 撤销游标——正在进行的自助录音会被掐断,所以单次瞬时丢包绝不能直接触发这条链。
// 宽限窗锚在「距上一次成功」而不是「距首次失败」:悬挂型断网(请求 12s 超时才失败)
// 首次失败时距上次成功早已超窗,立即亮错,不会比改前多等一轮;快速失败的单次抖动
// (距上次成功不足宽限窗)则被吸收。对齐 useLiveCursor 的 RECONNECT_GRACE_MS。
// 前台请求(首载/用户主动刷新)失败仍立即呈现,用户在等一个明确答案。
export const RUNTIME_ERROR_GRACE_MS = 4_500;

export interface PollHealth { lastSuccessMs: number }

export function pollSucceeded(nowMs: number): PollHealth {
  return { lastSuccessMs: nowMs };
}

// 宽限窗必须比轮询间隔长,否则「距上次成功」在第一次失败时就已超窗,一次抖动
// 照样亮错(5 秒一轮的授权轮询用 4.5 秒窗就是这样,对抗复核 2026-09-13)。
// 间隔长于宽限的轮询传 interval + RUNTIME_ERROR_GRACE_MS:吸收一次抖动,连着两次才亮。
export function pollFailed(
  health: PollHealth,
  nowMs: number,
  foreground: boolean,
  graceMs: number = RUNTIME_ERROR_GRACE_MS,
): boolean {
  return foreground || nowMs - health.lastSuccessMs >= graceMs;
}
