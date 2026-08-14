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

function cellText(value: string | number | boolean | null): string {
  if (value === null) return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  return String(value);
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
        : researchCsvFilename(dataset, classification));
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
            这里看到的每一行都已经去标识：受试者与场次用分域假名，绝对时间、转写原文、
            AI 理由、录音一律不出现。导出的 CSV 就是屏上这一页的同一份投影。
          </p>
        </div>
        <Button variant="ghost" onClick={onBack}>返回受试者列表</Button>
      </header>

      {metaError?.forbidden && (
        <Alert tone="warn" title="当前账号无权读取研究数据接口">
          只有 <strong>data_steward</strong>（数据管理员）和 <strong>admin</strong> 能读这一面。
          研究者账号被 trainer 隔离约束，看不到跨受试者的汇总——这是设计。找系统负责人开一个
          数据管理员账号，不要共用别人的账号。
        </Alert>
      )}
      {metaError && !metaError.forbidden && (
        <Alert tone="danger" title="研究数据接口状态读取失败">{metaError.message}</Alert>
      )}
      {!meta && !metaError && <StatusPill tone="muted">正在读取接口状态…</StatusPill>}

      {meta?.configured === false && (
        <Alert tone="warn" title="研究数据接口尚未开启">
          <p>{meta.reason}</p>
          <p className="muted">
            这不是故障：在服务器配置去标识密钥之前，本接口一行数据也不会返回，
            也<strong>绝不会</strong>退化成返回明文受试者编号。配置由数据管理员在服务器完成，
            密钥不要贴进聊天或邮件。
          </p>
        </Alert>
      )}

      {meta?.configured === true && (
        <>
          <section className="form-section">
            <div className="form-section-header">
              <div>
                <h3>选数据集与分区</h3>
                <p className="muted">
                  三张长表可按受试者编号相互 join；分区一次只看一个，真实与模拟永不混。
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
            <p className="muted">
              数据字典版本 <code>{meta.schemaVersion}</code> · 假名版本{" "}
              <code>{meta.pseudonymVersion}</code> · 假名密钥编号{" "}
              <code>{meta.pseudonymKeyId}</code>
            </p>
            <p className="muted">{meta.note}</p>
          </footer>
        </>
      )}
    </div>
  );
}

function ResearchTable({
  page, downloading, onExportPage, onExportDictionary,
  canGoBack, onBack, onNext, pageNo,
}: {
  page: ResearchPage;
  downloading: boolean;
  onExportPage: () => void;
  onExportDictionary: () => void;
  canGoBack: boolean;
  onBack: () => void;
  onNext: () => void;
  pageNo: number;
}) {
  return (
    <section className="form-section" aria-label="研究数据当前页">
      <div className="form-section-header">
        <div>
          <h3>第 {pageNo} 页 · {page.rowCount} 行 · 粒度 {page.grain}</h3>
          <p className="muted">
            分页是 keyset 而不是页码偏移，所以并发写入时不会漏行或重行。
            导出的是<strong>当前这一页</strong>；要全量请照《研究数据接口使用说明》按游标逐页拉。
          </p>
        </div>
        <div className="form-actions">
          <Button disabled={downloading} onClick={onExportPage}>
            {downloading ? "正在导出…" : "导出本页 CSV"}
          </Button>
          <Button variant="ghost" disabled={downloading} onClick={onExportDictionary}>
            导出数据字典 CSV
          </Button>
        </div>
      </div>

      {page.rowCount === 0 ? (
        <Alert tone="info" title="本分区暂无数据">
          零行是真的零行，不是被过滤掉了——其它分区的数据不会混进来。
        </Alert>
      ) : (
        <div className="research-table-region" tabIndex={0}>
          <table className="research-table">
            <thead>
              <tr>{page.columns.map((column) => (
                <th key={column} scope="col" className="mono">{column}</th>
              ))}</tr>
            </thead>
            <tbody>
              {page.rows.map((row, index) => (
                <tr key={`${page.dataset}-${pageNo}-${index}`}>
                  {row.map((cell, cellIndex) => (
                    <td key={page.columns[cellIndex]} className="mono">{cellText(cell)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="form-actions">
        <Button variant="ghost" disabled={!canGoBack} onClick={onBack}>上一页</Button>
        <Button variant="ghost" disabled={!page.hasMore} onClick={onNext}>下一页</Button>
        {!page.hasMore && <StatusPill tone="muted">已经是最后一页</StatusPill>}
      </div>
    </section>
  );
}
