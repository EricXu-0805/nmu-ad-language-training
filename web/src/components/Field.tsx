import type { ReactNode } from "react";

// 表单字段外壳
export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="col" style={{ gap: 4 }}>
      <span style={{ fontWeight: 600 }}>{label}</span>
      {children}
      {hint && <span className="muted" style={{ fontSize: "0.85em" }}>{hint}</span>}
    </label>
  );
}

const inputStyle: React.CSSProperties = {
  padding: "var(--sp-2) var(--sp-3)", borderRadius: "var(--radius)",
  border: "1px solid var(--c-line)", background: "var(--c-surface)", minHeight: "var(--touch)",
};

export function TextInput(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} style={{ ...inputStyle, ...props.style }} />;
}

// 从冻结枚举渲染下拉;禁止本地私造取值。空选项 = 未填(nullable 一等公民)。
export function EnumSelect({ options, value, onChange, placeholder = "（未选）", disabled }: {
  options: readonly string[];
  value: string | null | undefined;
  onChange: (v: string | null) => void;
  placeholder?: string;
  disabled?: boolean;
}) {
  return (
    <select
      disabled={disabled}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
      style={inputStyle}
    >
      <option value="">{placeholder}</option>
      {options.map((o) => <option key={o} value={o}>{o}</option>)}
    </select>
  );
}

// 三态 是/否/未评 —— 禁止把"未评"静默当"否"(护栏2:合规字段 nullable 一等公民)。
export function TriStateField({ label, value, onChange }: {
  label: string;
  value: boolean | null | undefined;
  onChange: (v: boolean | null) => void;
}) {
  const opts: { k: string; v: boolean | null }[] = [
    { k: "是", v: true }, { k: "否", v: false }, { k: "未评", v: null },
  ];
  const cur = value === true ? "是" : value === false ? "否" : "未评";
  return (
    <Field label={label}>
      <div className="row" role="radiogroup" aria-label={label}>
        {opts.map((o) => (
          <button key={o.k} type="button" onClick={() => onChange(o.v)}
            aria-pressed={cur === o.k}
            style={{
              padding: "var(--sp-2) var(--sp-4)", borderRadius: "var(--radius)",
              border: cur === o.k ? "2px solid var(--c-primary)" : "1px solid var(--c-line)",
              background: cur === o.k ? "var(--c-primary)" : "var(--c-surface)",
              color: cur === o.k ? "#fff" : "var(--c-fg)", fontWeight: 600,
            }}>
            {o.k}
          </button>
        ))}
      </div>
    </Field>
  );
}
