import { useId } from "react";
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
// nullLabel 可换成场景化说法(如"暂不选择"),含义不变仍是 null。
// yesDisabled + yesDisabledReason:当"是"注定无效(如服务器未接入云服务)时
// 当场禁用并给可见原因,不让人保存时才撞错。
export function TriStateField({ label, value, onChange, nullLabel = "未评",
  yesDisabled = false, yesDisabledReason }: {
  label: string;
  value: boolean | null | undefined;
  onChange: (v: boolean | null) => void;
  nullLabel?: string;
  yesDisabled?: boolean;
  yesDisabledReason?: ReactNode;
}) {
  const labelId = useId();
  const opts: { k: string; v: boolean | null; disabled?: boolean }[] = [
    { k: "是", v: true, disabled: yesDisabled },
    { k: "否", v: false },
    { k: nullLabel, v: null },
  ];
  const cur = value === true ? "是" : value === false ? "否" : nullLabel;
  return (
    <div className="field">
      <span className="field__label" id={labelId}>{label}</span>
      <div className="segmented-control" role="group" aria-labelledby={labelId}>
        {opts.map((o) => (
          <button key={o.k} type="button" onClick={() => onChange(o.v)}
            aria-label={o.k}
            aria-pressed={cur === o.k}
            disabled={o.disabled || undefined}
            className="segmented-control__button">
            {o.k}
          </button>
        ))}
      </div>
      {yesDisabled && yesDisabledReason && (
        <span className="field__hint">{yesDisabledReason}</span>
      )}
    </div>
  );
}
