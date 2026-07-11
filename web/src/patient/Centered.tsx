import type { ReactNode } from "react";

// 老人端居中舞台容器:超大留白、垂直居中、高对比。
export function Centered({ children }: { children: ReactNode }) {
  return (
    <div style={{ minHeight: "100dvh", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: "var(--sp-6)", padding: "var(--sp-6)", textAlign: "center" }}>
      {children}
    </div>
  );
}
