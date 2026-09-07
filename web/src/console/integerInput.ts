export type IntegerInputResult = { value: number | null; error: string | null };

/** Keep the user's text intact. A decimal, sign or exponent is never reinterpreted. */
export function parseIntegerInput(raw: string, label: string, min: number, max: number): IntegerInputResult {
  if (raw === "") return { value: null, error: null };
  if (!/^[0-9]+$/.test(raw)) return { value: null, error: `${label}请填写整数，不要输入小数、正负号或其他字符` };
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    return { value: null, error: `${label}必须在 ${min}–${max} 之间` };
  }
  return { value, error: null };
}
