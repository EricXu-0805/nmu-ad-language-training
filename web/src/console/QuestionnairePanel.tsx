import { useEffect, useState } from "react";
import { api } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { StatusPill } from "../components/StatusPill";
import {
  QuestionnaireDrawer,
  type QuestionnaireRecordClient,
} from "./QuestionnaireDrawer";
import {
  lockedScoreSummary,
  questionnaireFailureText,
  questionnaireStatusLabel,
  QUESTIONNAIRE_PHASE_LABELS,
  QUESTIONNAIRE_TRIAL_NOTICE,
  type QuestionnaireCatalogEntry,
  type QuestionnairePhaseLabel,
  type QuestionnaireRecord,
} from "./questionnaires";

const client: QuestionnaireRecordClient = {
  putValues: (record, values) => api.putQuestionnaireValues(record, values),
  generateAiDraft: (record) => api.generateQuestionnaireAiDraft(record),
  lock: (record) => api.lockQuestionnaireRecord(record),
};

// 人工评价量表(SFACS/GDS-15/NPI-Q)的电子记录入口,挂在量表抽屉里。
// 题词经认证接口随定义包下发,本文件不含任何题词。
export function QuestionnairePanel({ patientId }: { patientId: string }) {
  const [catalog, setCatalog] = useState<QuestionnaireCatalogEntry[] | null>(null);
  const [records, setRecords] = useState<QuestionnaireRecord[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [selectedQuestionnaire, setSelectedQuestionnaire] = useState<string | null>(null);
  const [selectedPhase, setSelectedPhase] = useState<QuestionnairePhaseLabel | null>(null);
  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [openRecordId, setOpenRecordId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setCatalog(null);
    setRecords(null);
    setLoadError(null);
    setOpenRecordId(null);
    void (async () => {
      try {
        const [nextCatalog, nextRecords] = await Promise.all([
          api.listQuestionnaireDefinitions(),
          api.listQuestionnaireRecords(patientId),
        ]);
        if (cancelled) return;
        setCatalog(nextCatalog);
        setRecords(nextRecords);
      } catch (reason) {
        if (cancelled) return;
        setLoadError(questionnaireFailureText(reason).message);
      }
    })();
    return () => { cancelled = true; };
  }, [patientId, retry]);

  const definitionById = new Map(
    (catalog ?? []).map((entry) => [entry.definition.questionnaire_id, entry]));
  const openRecord = openRecordId === null
    ? null
    : records?.find((record) => record.record_id === openRecordId) ?? null;
  const openEntry = openRecord
    ? definitionById.get(openRecord.questionnaire_id) ?? null
    : null;

  const create = async () => {
    if (!selectedQuestionnaire || !selectedPhase || createBusy) return;
    setCreateBusy(true);
    setCreateError(null);
    try {
      const record = await api.createQuestionnaireRecord(patientId, {
        questionnaire_id: selectedQuestionnaire,
        phase_label: selectedPhase,
      });
      setRecords((previous) => [...(previous ?? []), record]);
      setOpenRecordId(record.record_id);
    } catch (reason) {
      setCreateError(questionnaireFailureText(reason).message);
    }
    setCreateBusy(false);
  };

  const onRecordUpdated = (next: QuestionnaireRecord) => {
    setRecords((previous) => previous === null
      ? previous
      : previous.map((record) =>
        record.record_id === next.record_id ? next : record));
  };

  return (
    <div className="card col">
      <div className="row wrap" style={{ justifyContent: "space-between" }}>
        <h3>人工评价量表（试用）</h3>
        <StatusPill tone="warn">试用</StatusPill>
      </div>
      <Alert tone="warn" compact title={QUESTIONNAIRE_TRIAL_NOTICE}>
        请照原表口头施测；这里只做电子记录与 AI 初评核对。
      </Alert>

      {loadError && (
        <Alert tone="danger" title="量表记录读取失败"
          actions={
            <Button onClick={() => setRetry((value) => value + 1)}>重新加载</Button>
          }>
          {loadError}
        </Alert>
      )}
      {!loadError && (catalog === null || records === null) && (
        <p className="muted">正在读取量表记录…</p>
      )}

      {records && records.length === 0 && (
        <p className="muted">这位受试者还没有量表电子记录。</p>
      )}
      {records && records.map((record) => {
        const entry = definitionById.get(record.questionnaire_id) ?? null;
        return (
          <div className="row wrap" key={record.record_id}
            style={{
              justifyContent: "space-between",
              borderBottom: "1px solid var(--c-line)",
              paddingBottom: 8,
            }}>
            <span>
              <strong>
                {entry
                  ? entry.definition.title
                  : record.questionnaire_id}
              </strong>
              {` · ${record.phase_label}`}
            </span>
            <span className="row wrap">
              <StatusPill tone={record.status === "locked" ? "ok" : "warn"}>
                {questionnaireStatusLabel(record)}
              </StatusPill>
              <span className="mono">
                {lockedScoreSummary(record, entry?.definition ?? null) ?? "—"}
              </span>
              {entry ? (
                <Button size="sm" onClick={() => setOpenRecordId(record.record_id)}>
                  {record.status === "draft" ? "继续填写" : "查看"}
                </Button>
              ) : (
                <small className="muted">定义包已不含此量表，暂时打不开</small>
              )}
            </span>
          </div>
        );
      })}

      {catalog && catalog.length === 0 && (
        <p className="muted">量表定义包还没安装，暂时不能建立记录。</p>
      )}
      {catalog && catalog.length > 0 && records && (
        <div className="col">
          <h4>新建记录</h4>
          <div className="segmented-control" role="group" aria-label="选择量表">
            {catalog.map((entry) => (
              <button key={entry.definition.questionnaire_id} type="button"
                className="segmented-control__button"
                aria-pressed={selectedQuestionnaire === entry.definition.questionnaire_id}
                title={entry.definition.title}
                onClick={() =>
                  setSelectedQuestionnaire(entry.definition.questionnaire_id)}>
                {entry.definition.short_name}
              </button>
            ))}
          </div>
          {selectedQuestionnaire && (
            <small className="muted">
              {catalog.find((entry) =>
                entry.definition.questionnaire_id === selectedQuestionnaire)
                ?.definition.title}
            </small>
          )}
          <div className="segmented-control" role="group" aria-label="选择期别">
            {QUESTIONNAIRE_PHASE_LABELS.map((phase) => (
              <button key={phase} type="button" className="segmented-control__button"
                aria-pressed={selectedPhase === phase}
                onClick={() => setSelectedPhase(phase)}>
                {phase}
              </button>
            ))}
          </div>
          {createError && (
            <Alert tone="danger" title="记录没有建立成功">{createError}</Alert>
          )}
          {(!selectedQuestionnaire || !selectedPhase) && (
            <small className="muted">先选量表和期别，再建立记录。</small>
          )}
          <Button variant="primary"
            disabled={!selectedQuestionnaire || !selectedPhase || createBusy}
            onClick={() => void create()}>
            {createBusy ? "正在建立…" : "建立记录并开始填写"}
          </Button>
        </div>
      )}

      {openRecord && openEntry && (
        <QuestionnaireDrawer key={openRecord.record_id}
          initialRecord={openRecord}
          definition={openEntry.definition}
          definitionDrifted={openRecord.definition_sha256 !== openEntry.content_sha256}
          client={client}
          onClose={() => setOpenRecordId(null)}
          onRecordUpdated={onRecordUpdated} />
      )}
    </div>
  );
}
