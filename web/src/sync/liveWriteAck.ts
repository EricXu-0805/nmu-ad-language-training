export function requireServerWseq(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new Error("服务器未返回有效的实时指令序号");
  }
  return Math.trunc(value);
}
