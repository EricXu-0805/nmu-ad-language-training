import { useEffect, useState } from "react";
import { api, ApiError } from "../../api";
import { Alert } from "../../components/Alert";
import { Button } from "../../components/Button";
import { StatusPill } from "../../components/StatusPill";
import {
  RESEARCH_DATASET_KEYS,
  researchCsvFilename,
  type ResearchDataClassification,
  type ResearchDatasetKey,
  type ResearchMeta,
  type ResearchPage,
} from "./researchDataContract";
import { ResearchTable } from "./ResearchDataTable";

// 一屏两件事：给 PI 与合作者看"数据长什么样"，以及把当前这一页原样导出。
// 屏上渲染的行与导出的 CSV 是同一个 query（只差 .csv 后缀），所以不可能出现
// "网页上看到的和导出的不一样"，也不可能把标识列带出去——屏幕只能渲染
// 响应自己声明的那些列。

const CLASSIFICATIONS: { key: ResearchDataClassification; label: string; hint: string }[] = [
  { key: "research", label: "真实研究", hint: "纳入研究结论的数据" },
  { key: "simulation", label: "模拟演练", hint: "只用于流程与模型调试，不得并入研究结论" },
];

type PageState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; page: ResearchPage }
  | { status: "error"; message: string; forbidden: boolean };

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.detail;
  return error instanceof Error ? error.message : String(error);
}

function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export function ResearchDataScreen({ onBack }: { onBack: () => void }) {
  const [meta, setMeta] = useState<ResearchMeta | null>(null);
  const [metaError, setMetaError] = useState<{ message: string; forbidden: boolean } | null>(null);
  const [dataset, setDataset] = useState<ResearchDatasetKey>("subjects");
  const [classification, setClassification] = useState<ResearchDataClassification>("research");
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([null]);
  const [pageState, setPageState] = useState<PageState>({ status: "idle" });
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);

  const cursor = cursorStack[cursorStack.length - 1] ?? null;
  const ready = meta?.configured === true;

  useEffect(() => {
    const controller = new AbortController();
    api.researchMeta(controller.signal)
      .then((value) => { setMeta(value); setMetaError(null); })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setMetaError({
          message: errorText(error),
          forbidden: error instanceof ApiError && (error.status === 403 || error.status === 401),
        });
      });
    return () => controller.abort();
  }, []);

  // 换数据集或换分区就回到第一页：游标是按自然键签名的，跨数据集复用必然被拒。
  useEffect(() => { setCursorStack([null]); }, [dataset, classification]);

  useEffect(() => {
    if (!ready) return;
    const controller = new AbortController();
    setPageState({ status: "loading" });
    setDownloadError(null);
    api.researchDataset({ dataset, classification, cursor, signal: controller.signal })
      .then((page) => setPageState({ status: "ready", page }))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setPageState({
          status: "error",
          message: errorText(error),
          forbidden: error instanceof ApiError && error.status === 403,
        });
      });
    return () => controller.abort();
  }, [ready, dataset, classification, cursor]);

  async function download(kind: "page" | "dictionary") {
    setDownloading(true);
    setDownloadError(null);
    try {
      const blob = kind === "dictionary"
        ? await api.researchDictionaryCsv()
        : await api.researchCsv({ dataset, classification, cursor });
      saveBlob(blob, kind === "dictionary"
        ? "nmu-research-dictionary.csv"
        : researchCsvFilename(
            dataset, classification,
            pageState.status === "ready" ? pageState.page.release : null));
    } catch (error: unknown) {
      setDownloadError(errorText(error));
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div className="page-shell page-shell--wide">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">分析后台 · 研究数据</p>
          <h2 className="page-title">去标识研究数据总览</h2>
          <p className="page-description">
            所有数据已去标识：受试者与场次只用编号代替，不含姓名、绝对时间、转写原文和录音。
            导出的 CSV 与屏上显示完全一致。
          </p>
        </div>
        <Button variant="ghost" onClick={onBack}>返回受试者列表</Button>
      </header>

      {metaError?.forbidden && (
        <Alert tone="warn" title="当前账号无权查看研究数据">
          <p>此页仅数据管理员和系统管理员可见。请让系统负责人为你开通数据管理员账号，不要共用别人的账号。</p>
          <details>
            <summary>技术详情</summary>
            <p className="muted">
              需要 <code>data_steward</code>（数据管理员）或 <code>admin</code>（系统管理员）角色；
              研究者账号受隔离约束，看不到跨受试者的汇总。
            </p>
          </details>
        </Alert>
      )}
      {metaError && !metaError.forbidden && (
        <Alert tone="danger" title="研究数据接口状态读取失败">{metaError.message}</Alert>
      )}
      {!meta && !metaError && <StatusPill tone="muted">正在读取接口状态…</StatusPill>}

      {meta?.configured === false && (
        <Alert tone="warn" title="研究数据尚未开放">
          <p>服务器还没有完成去标识配置，配置完成前这里不显示任何数据。请联系数据管理员。</p>
          <details>
            <summary>技术详情</summary>
            <p className="muted">{meta.reason}</p>
            <p className="muted">
              在服务器配置去标识密钥之前，本接口一行数据也不会返回，
              也不会退化成返回明文受试者编号。配置由数据管理员在服务器完成，
              密钥不要贴进聊天或邮件。
            </p>
          </details>
        </Alert>
      )}

      {meta?.configured === true && (
        <>
          <section className="form-section">
            <div className="form-section-header">
              <div>
                <h3>选数据集与分区</h3>
                <p className="muted">
                  三张表可按受试者编号相互对照；真实与模拟数据分开查看，不会混在一起。
                </p>
              </div>
            </div>
            <div className="form-actions" role="group" aria-label="数据集">
              {RESEARCH_DATASET_KEYS.map((key) => {
                const info = meta.datasets.find((entry) => entry.key === key);
                return (
                  <Button
                    key={key}
                    variant={dataset === key ? "primary" : "ghost"}
                    onClick={() => setDataset(key)}
                  >
                    {info?.title ?? key}
                    <span className="muted">（{info?.columns.length ?? 0} 列）</span>
                  </Button>
                );
              })}
            </div>
            <div className="form-actions" role="group" aria-label="数据分区">
              {CLASSIFICATIONS.map((entry) => (
                <Button
                  key={entry.key}
                  variant={classification === entry.key ? "primary" : "ghost"}
                  onClick={() => setClassification(entry.key)}
                  title={entry.hint}
                >
                  {entry.label}
                </Button>
              ))}
            </div>
            {classification === "simulation" && (
              <Alert tone="warn" title="模拟演练分区">
                本区数据只用于流程和模型调试，不得并入真实研究结果或写进论文。
              </Alert>
            )}
            {classification === "research" && meta.release.bound === false && (
              <Alert tone="warn" title="真实研究分区还没有可发布的冻结版本">
                <p>
                  这不是故障：研究数据需先由数据管理员发布一个冻结版本，发布后这里才会显示数据。
                </p>
                <details>
                  <summary>技术详情</summary>
                  <p className="muted">{meta.release.reason}</p>
                  <p className="muted">
                    冻结版本由数据管理员与管理员两个具名的人各跑一次
                    <code>scripts/cut_quality_release.py</code>（一个提议、另一个拿着
                    sha256 批准）生成，保证同一版数据反复拉取逐字节相同。
                    拒绝码 <code>{meta.release.code}</code>。
                  </p>
                </details>
              </Alert>
            )}
          </section>

          {pageState.status === "loading" && (
            <StatusPill role="status" tone="muted">正在取数…</StatusPill>
          )}
          {pageState.status === "error" && (
            <Alert
              tone={pageState.forbidden ? "warn" : "danger"}
              title={pageState.forbidden ? "当前账号无权读取这一页" : "取数失败，已拒绝显示"}
            >
              {pageState.message}
            </Alert>
          )}
          {pageState.status === "ready" && (
            <ResearchTable
              page={pageState.page}
              downloading={downloading}
              onExportPage={() => void download("page")}
              onExportDictionary={() => void download("dictionary")}
              canGoBack={cursorStack.length > 1}
              onBack={() => setCursorStack((stack) => stack.slice(0, -1))}
              onNext={() => setCursorStack((stack) => [...stack, pageState.page.nextCursor])}
              pageNo={cursorStack.length}
            />
          )}
          {downloadError && (
            <Alert tone="danger" title="导出失败">{downloadError}</Alert>
          )}

          <footer className="form-section">
            {meta.release.bound && (
              <p className="muted">
                数据版本：第 {meta.release.epochSeq} 版 · {meta.release.frozenSessionCount} 个场次 ·
                截止 {meta.release.asOf}。两份导出的版本号相同才能直接比对。
              </p>
            )}
            <details>
              <summary>技术详情</summary>
              <p className="muted">
                数据字典版本 <code>{meta.schemaVersion}</code> · 假名版本{" "}
                <code>{meta.pseudonymVersion}</code> · 假名密钥编号{" "}
                <code>{meta.pseudonymKeyId}</code>
              </p>
              {meta.release.bound && (
                <p className="muted">
                  数据版本 <code>第 {meta.release.epochSeq} 版</code> · 数据指纹{" "}
                  <code>{meta.release.aggregatePayloadSha256.slice(0, 12)}</code>
                </p>
              )}
              <p className="muted">{meta.note}</p>
            </details>
          </footer>
        </>
      )}
    </div>
  );
}
