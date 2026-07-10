import type { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "ghost" | "danger";

const styles: Record<Variant, React.CSSProperties> = {
  primary: { background: "var(--c-primary)", color: "var(--c-on-primary)", border: "1px solid var(--c-primary)" },
  ghost: { background: "var(--c-surface)", color: "var(--c-fg)", border: "1px solid var(--c-line)" },
  danger: { background: "var(--c-danger)", color: "#fff", border: "1px solid var(--c-danger)" },
};

export function Button({ variant = "ghost", style, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      {...rest}
      style={{ padding: "var(--sp-2) var(--sp-4)", borderRadius: "var(--radius)", fontWeight: 600, ...styles[variant], ...style }}
    />
  );
}
