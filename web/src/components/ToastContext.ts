import { createContext, useContext } from "react";

export type ToastTone = "info" | "ok" | "warn" | "danger";

export const ToastContext = createContext<(text: string, tone?: ToastTone) => void>(() => {});

export function useToast() {
  return useContext(ToastContext);
}
