import { useEffect, useState } from "react";
import { api } from "../api";
import {
  parsePatientPresentation,
  presentationExpectationKey,
  presentationMatches,
  type PatientPresentation,
  type PatientPresentationExpectation,
} from "./presentationContent";

const RETRY_MS = 1_000;

// 弱网/游标竞态下保持安静重试。任何旧转位响应即使后到，也只会被
// exact expectation 拒绝，不会把上一题/上一级提示闪回老人屏幕。
export function usePatientPresentation(expected: PatientPresentationExpectation | null): {
  presentation: PatientPresentation | null;
  error: string | null;
} {
  const key = presentationExpectationKey(expected);
  const [loaded, setLoaded] = useState<PatientPresentation | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (expected === null) {
      setLoaded(null);
      setError(null);
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    setLoaded(null);
    setError(null);
    const load = () => {
      api.patientPresentation(expected.sessionId)
        .then((raw) => {
          if (cancelled) return;
          const parsed = parsePatientPresentation(raw);
          if (!presentationMatches(parsed, expected)) {
            throw new Error("老人端呈现响应与当前服务端游标不一致");
          }
          setLoaded(parsed);
          setError(null);
        })
        .catch((reason) => {
          if (cancelled) return;
          setLoaded(null);
          setError(String(reason));
          timer = window.setTimeout(load, RETRY_MS);
        });
    };
    load();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
    // key 包含全部稳定期望字段；直接依赖每次 render 新建的 expected 会循环请求。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return {
    presentation: loaded && expected && presentationMatches(loaded, expected) ? loaded : null,
    error,
  };
}
