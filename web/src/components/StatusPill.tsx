type Tone = "ok" | "warn" | "danger" | "muted" | "primary";

const bg: Record<Tone, string> = {
  ok: "var(--c-ok)", warn: "var(--c-warn)", danger: "var(--c-danger)",
  muted: "var(--c-muted)", primary: "var(--c-primary)",
};

export function StatusPill({ tone = "muted", children }: { tone?: Tone; children: React.ReactNode }) {
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 4, padding: "2px 10px",
      borderRadius: 999, background: bg[tone], color: "#fff", fontSize: "0.85em", fontWeight: 600, whiteSpace: "nowrap",
    }}>
      {children}
    </span>
  );
}
