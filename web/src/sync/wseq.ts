// 跨刷新/跨研究者设备的写序号下界。服务端可能在暂停/恢复时生成比本机更大的 wseq，
// 写者必须先观察再递增，否则患者端会把恢复后的第一条研究者指令当作旧消息丢弃。
let counter = Date.now();

export function observeWseq(value: unknown): void {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return;
  counter = Math.max(counter, Math.trunc(value));
}

export function nextWseq(...observed: unknown[]): number {
  observed.forEach(observeWseq);
  counter = Math.max(counter, Date.now()) + 1;
  return counter;
}
