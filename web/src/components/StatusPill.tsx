import type { HTMLAttributes } from "react";

export type StatusTone = "ok" | "warn" | "danger" | "muted" | "primary";
export type StatusSize = "sm" | "md";

export interface StatusPillProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: StatusTone;
  size?: StatusSize;
}

export function StatusPill({ tone = "muted", size = "md", className, ...rest }: StatusPillProps) {
  return (
    <span
      {...rest}
      className={["status-pill", `status-pill--${tone}`, `status-pill--${size}`, className ?? ""].filter(Boolean).join(" ")}
    />
  );
}
