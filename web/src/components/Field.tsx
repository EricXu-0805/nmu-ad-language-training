import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";

// 表单字段外壳
export function Field({ label, hint, error, required = false, className, children }: {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  required?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <label className={["field", className ?? ""].filter(Boolean).join(" ")}>
      <span className="field__label">
        {label}{required && <span className="field__required" aria-hidden>*</span>}
      </span>
      {children}
      {hint && <span className="field__hint">{hint}</span>}
      {error && <span className="field__error" role="alert">{error}</span>}
    </label>
  );
}

export function TextInput({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={["form-control", className ?? ""].filter(Boolean).join(" ")} />;
}

// 从冻结枚举渲染下拉;禁止本地私造取值。空选项 = 未填(nullable 一等公民)。
// allowEmpty=false 用于必有值的场景(如提示等级默认 0),不渲染占位空选项。
export interface EnumSelectProps extends Omit<SelectHTMLAttributes<HTMLSelectElement>, "children" | "onChange" | "value"> {
  options: readonly string[];
  value: string | null | undefined;
  onChange: (v: string | null) => void;
  placeholder?: string;
  allowEmpty?: boolean;
}

export function EnumSelect({ options, value, onChange, placeholder = "（未选）", allowEmpty = true, className, ...rest }: EnumSelectProps) {
  return (
    <select
      {...rest}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
      className={["form-control", className ?? ""].filter(Boolean).join(" ")}
    >
      {allowEmpty && <option value="">{placeholder}</option>}
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
      <div className="segmented-control" role="group" aria-label={label}>
        {opts.map((o) => (
          <button key={o.k} type="button" onClick={() => onChange(o.v)}
            aria-pressed={cur === o.k}
            className="segmented-control__button">
            {o.k}
          </button>
        ))}
      </div>
    </Field>
  );
}
