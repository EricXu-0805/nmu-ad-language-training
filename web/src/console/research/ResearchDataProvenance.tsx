import type { ResearchMetaReady, ResearchPage } from "./researchDataContract";

export function ResearchDataProvenance({ meta, page }: {
  meta: ResearchMetaReady;
  page: ResearchPage | null;
}) {
  const release = page?.release;
  // 概况可能早于本页；只有完整版本绑定一致时，才可借用其中的截止日与场次数。
  const matchingRelease = release && meta.release.bound
    && release.epochSeq === meta.release.epochSeq
    && release.cohortRuleVersion === meta.release.cohortRuleVersion
    && release.aggregatePayloadSha256 === meta.release.aggregatePayloadSha256
    ? meta.release : null;
  const provenance = page ?? meta;

  return (
    <footer className="form-section" aria-label="研究数据版本说明">
      {release ? (
        <>
          <p className="muted">
            当前页数据版本：第 {release.epochSeq} 版。只有版本号与假名密钥编号都相同的导出才能直接比对。
          </p>
          {matchingRelease ? (
            <p className="muted">
              本版本包含 {matchingRelease.frozenSessionCount} 个场次，截止 {matchingRelease.asOf}。
            </p>
          ) : (
            <p className="muted">
              本页的截止日期与场次数尚未核对，暂不显示。重新打开此页可更新概况。
            </p>
          )}
        </>
      ) : page ? (
        <p className="muted">
          当前页为模拟演练数据，没有冻结版本；只用于流程与模型调试，导出以下载时的数据为准。
        </p>
      ) : (
        <p className="muted">数据版本将在当前页读取成功后显示。</p>
      )}
      <details>
        <summary>技术详情</summary>
        <p className="muted">
          数据字典版本 <code>{provenance.schemaVersion}</code> · 假名版本{" "}
          <code>{provenance.pseudonymVersion}</code> · 假名密钥编号{" "}
          <code>{provenance.pseudonymKeyId}</code>
        </p>
        {release && (
          <p className="muted">
            数据版本 <code>第 {release.epochSeq} 版</code> · 数据指纹{" "}
            <code>{release.aggregatePayloadSha256.slice(0, 12)}</code>
          </p>
        )}
        <p className="muted">{meta.note}</p>
      </details>
    </footer>
  );
}
