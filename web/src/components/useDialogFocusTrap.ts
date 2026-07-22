import { useLayoutEffect, useRef, type RefObject } from "react";

const FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "summary",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

type InitialDialogFocus = "first-control" | "first-button" | "panel";

function visibleFocusableElements(panel: HTMLElement): HTMLElement[] {
  return Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
    .filter((node) => !node.hidden && node.getAttribute("aria-hidden") !== "true");
}

function focusDialog(panel: HTMLElement | null, initialFocus: InitialDialogFocus): void {
  if (!panel) return;
  if (initialFocus === "panel") {
    panel.focus();
    return;
  }
  const selector = initialFocus === "first-button"
    ? "button:not([disabled])"
    : FOCUSABLE_SELECTOR;
  panel.querySelector<HTMLElement>(selector)?.focus();
  if (!panel.contains(document.activeElement)) panel.focus();
}

/**
 * Shared modal keyboard contract: move focus inside on open, contain Tab,
 * support Escape through the caller's safety policy, and restore the trigger.
 */
export function useDialogFocusTrap<T extends HTMLElement>({
  open,
  onCancel,
  initialFocus = "first-control",
  focusKey,
}: {
  open: boolean;
  onCancel: () => void;
  initialFocus?: InitialDialogFocus;
  focusKey?: string | number | boolean;
}): RefObject<T | null> {
  const panelRef = useRef<T>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const cancelRef = useRef(onCancel);
  const initialFocusRef = useRef(initialFocus);
  cancelRef.current = onCancel;
  initialFocusRef.current = initialFocus;

  useLayoutEffect(() => {
    if (!open) return;
    returnFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    focusDialog(panelRef.current, initialFocusRef.current);

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        cancelRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const panel = panelRef.current;
      if (!panel) return;
      const focusable = visibleFocusableElements(panel);
      if (focusable.length === 0) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (!panel.contains(active)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      const target = returnFocusRef.current;
      if (target?.isConnected) target.focus();
      returnFocusRef.current = null;
    };
  }, [open]);

  useLayoutEffect(() => {
    if (!open || focusKey === undefined) return;
    focusDialog(panelRef.current, initialFocusRef.current);
  }, [focusKey, open]);

  return panelRef;
}
