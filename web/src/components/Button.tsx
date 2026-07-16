import type { ButtonHTMLAttributes } from "react";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "quiet" | "danger";
export type ButtonSize = "sm" | "md" | "lg";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** ghost 保留为旧版 outline 按钮别名；新代码优先写 secondary。 */
  variant?: ButtonVariant;
  size?: ButtonSize;
  fullWidth?: boolean;
}

export function Button({ variant = "ghost", size = "md", fullWidth = false, className, ...rest }: ButtonProps) {
  const classes = [
    "button",
    `button--${variant}`,
    `button--${size}`,
    fullWidth ? "button--full" : "",
    className ?? "",
  ].filter(Boolean).join(" ");
  return (
    <button {...rest} className={classes} />
  );
}
