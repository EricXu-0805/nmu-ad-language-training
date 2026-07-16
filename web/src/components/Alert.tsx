import type { HTMLAttributes, ReactNode } from "react";

export type AlertTone = "info" | "ok" | "warn" | "danger";

export interface AlertProps extends Omit<HTMLAttributes<HTMLDivElement>, "title"> {
  tone?: AlertTone;
  title?: ReactNode;
  actions?: ReactNode;
  compact?: boolean;
  children: ReactNode;
}

/**
 * 页面内持久状态提示。warning / danger 默认以 alert 公布，普通说明以 status 公布。
 * 不内置图标，避免离线系统使用平台差异明显的 emoji。
 */
export function Alert({ tone = "info", title, actions, compact = false, className, role, children, ...rest }: AlertProps) {
  const classes = [
    "alert",
    tone !== "info" ? `alert--${tone}` : "",
    compact ? "alert--compact" : "",
    className ?? "",
  ].filter(Boolean).join(" ");

  return (
    <div {...rest} className={classes} role={role ?? (tone === "danger" || tone === "warn" ? "alert" : "status")}>
      <div className="alert__content">
        {title && <div className="alert__title">{title}</div>}
        <div className="alert__body">{children}</div>
      </div>
      {actions && <div className="alert__actions">{actions}</div>}
    </div>
  );
}
