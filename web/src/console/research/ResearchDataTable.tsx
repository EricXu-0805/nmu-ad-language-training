import { Alert } from "../../components/Alert";
import { Button } from "../../components/Button";
import { StatusPill } from "../../components/StatusPill";
import type { ResearchCell, ResearchPage } from "./researchDataContract";

function cellText(value: ResearchCell): string {
  if (value === null) return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  return String(value);
}

export function ResearchTable({
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
