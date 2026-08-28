import { useReducer, useRef, useState, type KeyboardEvent } from "react";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { StatusPill } from "../components/StatusPill";
import { useDialogFocusTrap } from "../components/useDialogFocusTrap";
import {
  adoptableAiDraftEntries,
  aiDraftStatusLine,
  examinerDomainOf,
  examinerDomainTotals,
  examinerEntryMaxPoints,
  examinerEntryNumber,
  examinerEntryPoints,
  finalValuesBySlot,
  lockedScoreSummary,
  missingLockEntries,
  performQuestionnaireMutation,
  questionnaireSlotKey,
  questionnaireStatusLabel,
  QUESTIONNAIRE_TRIAL_NOTICE,
  type QuestionnaireChoiceField,
  type QuestionnaireDefinition,
  type QuestionnaireExaminerDomain,
  type QuestionnaireExaminerEntry,
  type QuestionnaireExaminerItem,
  type QuestionnaireFailure,
  type QuestionnaireItem,
  type QuestionnaireRecord,
  type QuestionnaireValueWrite,
} from "./questionnaires";

// 本抽屉刻意不 import api:题面/锚点/档位全部由 client 与定义包(经认证接口)喂进来,
// 源码里没有任何题词,SSR 测试可直接装载。

export interface QuestionnaireRecordClient {
  putValues: (
    record: QuestionnaireRecord, values: QuestionnaireValueWrite[],
  ) => Promise<QuestionnaireRecord>;
  generateAiDraft: (record: QuestionnaireRecord) => Promise<QuestionnaireRecord>;
  lock: (record: QuestionnaireRecord) => Promise<QuestionnaireRecord>;
}

interface AiDraftHint {
  value: string | null;
  rationale: string | null;
}

function anchorsLegend(field: QuestionnaireChoiceField): string {
  return field.allowed.map((value) => `${value}=${field.anchors[value]}`).join(" · ");
}

function AiHint({ hint }: { hint?: AiDraftHint }) {
  if (!hint) return null;
  return (
    <p className="muted" style={{ margin: 0 }}>
      <StatusPill tone="primary" size="sm">AI 建议</StatusPill>{" "}
      {hint.value ?? "平台数据看不出来，请人工评定"}
      {hint.rationale ? ` · ${hint.rationale}` : ""}
    </p>
  );
}

function ChoiceButtons({ field, selected, disabled, fullAnchors = false, onSelect }: {
  field: QuestionnaireChoiceField;
  selected: string | null;
  disabled: boolean;
  /** true=锚点全文直接做按钮文字(严重度/频率这类必须全文可见的档位)。 */
  fullAnchors?: boolean;
  onSelect: (value: string) => void;
}) {
  return (
    <div className="segmented-control" role="group">
      {field.allowed.map((value) => (
        <button key={value} type="button" className="segmented-control__button"
          aria-pressed={selected === value} disabled={disabled}
          title={field.anchors[value]}
          onClick={() => onSelect(value)}>
          {fullAnchors && field.anchors[value] !== value
            ? `${value} ${field.anchors[value]}`
            : value}
        </button>
      ))}
    </div>
  );
}

/** 点错一格要退得回「未评」。

档位按钮本身不做「再点一次取消」——在临床表单上那种手势太容易误触，一次误触
就把已经评好的一格清掉。这里给一个只在「本题已填且未锁定」时才出现的独立按钮，
按下把该条目的**全部字段**写回 null（NPI-Q 那种一题三格的，三格一起清）。
「未评」是锁定门禁的判据；退不回未评，门禁就形同虚设。 */
function ClearItemButton({ shown, fields, onWrite }: {
  shown: boolean;
  fields: readonly QuestionnaireValueWrite[];
  onWrite: (entries: QuestionnaireValueWrite[]) => void;
}) {
  if (!shown) return null;
  return (
    <div>
      <button type="button" className="button button--quiet button--sm"
        onClick={() => onWrite(fields.map((f) => ({ ...f, value: null })))}>
        退回未评
      </button>
    </div>
  );
}

function ItemHeading({ item }: { item: QuestionnaireItem }) {
  return (
    <span>
      <strong>{`第${item.no}题`}{item.name ? ` · ${item.name}` : ""}</strong>
      {` ${item.text}`}
    </span>
  );
}

const ITEM_ROW_STYLE = {
  borderBottom: "1px solid var(--c-line)",
  paddingBottom: 10,
} as const;

function OrdinalSectionsBody({ definition, disabled, effective, hints, onWrite }: {
  definition: QuestionnaireDefinition;
  disabled: boolean;
  effective: (itemKey: string, fieldKey: string) => string | null;
  hints: ReadonlyMap<string, AiDraftHint>;
  onWrite: (entries: QuestionnaireValueWrite[]) => void;
}) {
  const valueField = definition.value_field;
  const elementField = definition.element_field;
  if (!valueField || !elementField || !definition.sections) return null;
  return (
    <>
      {definition.sections.map((section) => (
        <div className="card col" key={section.section_id}>
          <h4>{section.title}</h4>
          <p className="muted">{anchorsLegend(valueField)}</p>
          {section.items.map((item) => (
            <div className="col" key={item.item_key} style={ITEM_ROW_STYLE}>
              <ItemHeading item={item} />
              <AiHint hint={hints.get(questionnaireSlotKey(item.item_key, "value"))} />
              <ChoiceButtons field={valueField} disabled={disabled}
                selected={effective(item.item_key, "value")}
                onSelect={(value) => onWrite([
                  { item_key: item.item_key, field_key: "value", value },
                ])} />
              <ClearItemButton onWrite={onWrite}
                shown={!disabled && effective(item.item_key, "value") !== null}
                fields={[{ item_key: item.item_key, field_key: "value", value: null }]} />
            </div>
          ))}
          <h5>本节沟通要素</h5>
          <p className="muted">{anchorsLegend(elementField)}</p>
          {elementField.elements.map((element) => (
            <div className="col" key={element.element_key} style={ITEM_ROW_STYLE}>
              <span>{element.label}</span>
              <ChoiceButtons field={elementField} disabled={disabled}
                selected={effective(
                  `section:${section.section_id}`, `element:${element.element_key}`)}
                onSelect={(value) => onWrite([{
                  item_key: `section:${section.section_id}`,
                  field_key: `element:${element.element_key}`,
                  value,
                }])} />
              <ClearItemButton onWrite={onWrite}
                shown={!disabled && effective(
                  `section:${section.section_id}`,
                  `element:${element.element_key}`) !== null}
                fields={[{
                  item_key: `section:${section.section_id}`,
                  field_key: `element:${element.element_key}`,
                  value: null,
                }]} />
            </div>
          ))}
        </div>
      ))}
    </>
  );
}

function BinaryScoredBody({ definition, disabled, effective, hints, onWrite }: {
  definition: QuestionnaireDefinition;
  disabled: boolean;
  effective: (itemKey: string, fieldKey: string) => string | null;
  hints: ReadonlyMap<string, AiDraftHint>;
  onWrite: (entries: QuestionnaireValueWrite[]) => void;
}) {
  const valueField = definition.value_field;
  if (!valueField || !definition.items) return null;
  return (
    <div className="card col">
      {definition.items.map((item) => (
        <div className="col" key={item.item_key} style={ITEM_ROW_STYLE}>
          <ItemHeading item={item} />
          <AiHint hint={hints.get(questionnaireSlotKey(item.item_key, "value"))} />
          <ChoiceButtons field={valueField} disabled={disabled} fullAnchors
            selected={effective(item.item_key, "value")}
            onSelect={(value) => onWrite([
              { item_key: item.item_key, field_key: "value", value },
            ])} />
          <ClearItemButton onWrite={onWrite}
            shown={!disabled && effective(item.item_key, "value") !== null}
            fields={[{ item_key: item.item_key, field_key: "value", value: null }]} />
        </div>
      ))}
    </div>
  );
}

function SymptomTripletBody({ definition, disabled, effective, hints, onWrite }: {
  definition: QuestionnaireDefinition;
  disabled: boolean;
  effective: (itemKey: string, fieldKey: string) => string | null;
  hints: ReadonlyMap<string, AiDraftHint>;
  onWrite: (entries: QuestionnaireValueWrite[]) => void;
}) {
  const presentField = definition.present_field;
  const severityField = definition.severity_field;
  const frequencyField = definition.frequency_field;
  if (!presentField || !severityField || !frequencyField || !definition.items) return null;
  return (
    <div className="card col">
      {definition.items.map((item) => {
        const present = effective(item.item_key, "present");
        return (
          <div className="col" key={item.item_key} style={ITEM_ROW_STYLE}>
            <ItemHeading item={item} />
            <AiHint hint={hints.get(questionnaireSlotKey(item.item_key, "present"))} />
            <ChoiceButtons field={presentField} disabled={disabled} fullAnchors
              selected={present}
              onSelect={(value) => onWrite(value === "无"
                ? [
                  // 记为「无」时同步清掉严重度/频率,否则锁定会被矛盾数据拦下。
                  { item_key: item.item_key, field_key: "present", value: "无" },
                  { item_key: item.item_key, field_key: "severity", value: null },
                  { item_key: item.item_key, field_key: "frequency", value: null },
                ]
                : [{ item_key: item.item_key, field_key: "present", value }])} />
            {present === "有" && (
              <>
                <span>严重度</span>
                <ChoiceButtons field={severityField} disabled={disabled} fullAnchors
                  selected={effective(item.item_key, "severity")}
                  onSelect={(value) => onWrite([
                    { item_key: item.item_key, field_key: "severity", value },
                  ])} />
                <span>频率</span>
                <ChoiceButtons field={frequencyField} disabled={disabled} fullAnchors
                  selected={effective(item.item_key, "frequency")}
                  onSelect={(value) => onWrite([
                    { item_key: item.item_key, field_key: "frequency", value },
                  ])} />
              </>
            )}
            <ClearItemButton onWrite={onWrite} shown={!disabled && present !== null}
              fields={[
                { item_key: item.item_key, field_key: "present", value: null },
                { item_key: item.item_key, field_key: "severity", value: null },
                { item_key: item.item_key, field_key: "frequency", value: null },
              ]} />
          </div>
        );
      })}
    </div>
  );
}

function examinerScoreField(max: number): QuestionnaireChoiceField {
  const allowed = Array.from({ length: max + 1 }, (_, index) => String(index));
  return {
    allowed,
    anchors: Object.fromEntries(allowed.map((value) => [value, `${value} 分`])),
  };
}

function examinerCountHint(entry: QuestionnaireExaminerEntry, number: number | null): string {
  if (!entry.scored) return "不计分，仅记录";
  const maxPoints = examinerEntryMaxPoints(entry);
  if (entry.bins) {
    return number === null
      ? `记正确总数（0–${entry.max}），按原表分档表换算为 0–${maxPoints} 分`
      : `换算得分 ${examinerEntryPoints(entry, String(number))} / ${maxPoints}`;
  }
  return number === null ? `记总数（0–${entry.max}），总数即得分` : `计 ${number} 分`;
}

function ExaminerCountInput({ item, value, disabled, onWrite }: {
  item: QuestionnaireExaminerItem;
  value: string | null;
  disabled: boolean;
  onWrite: (entries: QuestionnaireValueWrite[]) => void;
}) {
  const entry = item.entry;
  const max = entry.max ?? 0;
  const number = examinerEntryNumber(entry, value);
  return (
    <div className="row wrap" style={{ alignItems: "center" }}>
      {/* 用 text+inputMode 而不是 type=number:后者把 "5e"/"-" 这类半截输入直接吐成空串,
          硬键盘误触一下就会把已存的总数清掉;滚轮也会改值。文本框里原始按键全可见,由正则把关。 */}
      <input type="text" className="form-control" inputMode="numeric" pattern="[0-9]*"
        style={{ width: "7em" }}
        disabled={disabled} value={value ?? ""}
        aria-label={`第${item.no}题 ${item.name}`}
        onChange={(event) => {
          const raw = event.target.value.trim();
          if (raw === "") {
            onWrite([{ item_key: item.item_key, field_key: "value", value: null }]);
            return;
          }
          // 只接受 0–max 的整数;越界或非数字的按键不落地,屏上停在上一个合法值。
          if (!/^[0-9]+$/.test(raw)) return;
          const parsed = Number(raw);
          if (parsed > max) return;
          onWrite([{ item_key: item.item_key, field_key: "value", value: String(parsed) }]);
        }} />
      <span className="muted">{examinerCountHint(entry, number)}</span>
    </div>
  );
}

function ExaminerItemRow({ item, value, disabled, onWrite }: {
  item: QuestionnaireExaminerItem;
  value: string | null;
  disabled: boolean;
  onWrite: (entries: QuestionnaireValueWrite[]) => void;
}) {
  const entry = item.entry;
  const write = (next: string) =>
    onWrite([{ item_key: item.item_key, field_key: "value", value: next }]);
  return (
    <div className="col" style={ITEM_ROW_STYLE}>
      <span>
        <strong>{`第${item.no}题 · ${item.name}`}</strong>
        {` ${item.text}`}
      </span>
      {entry.kind === "score" && entry.max !== null && (
        <ChoiceButtons field={examinerScoreField(entry.max)} disabled={disabled}
          selected={value} onSelect={write} />
      )}
      {entry.kind === "count" && (
        <ExaminerCountInput item={item} value={value} disabled={disabled} onWrite={onWrite} />
      )}
      {entry.kind === "choice" && entry.choice && (
        <ChoiceButtons field={entry.choice} disabled={disabled} fullAnchors
          selected={value} onSelect={write} />
      )}
      {/* 计数框清空输入就是未评,不需要额外按钮;分档与闭集选项那两种要。 */}
      <ClearItemButton onWrite={onWrite}
        shown={!disabled && entry.kind !== "count" && value !== null}
        fields={[{ item_key: item.item_key, field_key: "value", value: null }]} />
    </div>
  );
}

// 检查者当场判分(ACE-III / 动物流畅性):屏上顺序照原表走,相邻同域条目合成一卡;
// 各域小计与总分随录入现算,只是预览——锁定时以服务器按同一分档表核算的为准。
function ExaminerScoredBody({ definition, disabled, effective, onWrite }: {
  definition: QuestionnaireDefinition;
  disabled: boolean;
  effective: (itemKey: string, fieldKey: string) => string | null;
  hints: ReadonlyMap<string, AiDraftHint>;
  onWrite: (entries: QuestionnaireValueWrite[]) => void;
}) {
  const panel = definition.examiner_panel;
  const scoring = definition.scoring;
  if (!panel || !scoring || scoring.kind !== "examiner_sum") return null;
  const value = (itemKey: string) => effective(itemKey, "value");
  const groups: { domain: QuestionnaireExaminerDomain; items: QuestionnaireExaminerItem[] }[] = [];
  for (const item of panel.items) {
    const last = groups[groups.length - 1];
    if (last && last.domain.domain_key === item.domain_key) last.items.push(item);
    else groups.push({ domain: examinerDomainOf(panel, item), items: [item] });
  }
  const totals = examinerDomainTotals(panel, value);
  const sum = totals.reduce((acc, entry) => acc + entry.points, 0);
  const missing = totals.reduce((acc, entry) => acc + entry.missing, 0);
  return (
    <>
      {groups.map((group, index) => (
        <div className="card col" key={`${group.domain.domain_key}:${index}`}>
          <h4>{group.domain.title}</h4>
          {group.items.map((item) => (
            <ExaminerItemRow key={item.item_key} item={item} value={value(item.item_key)}
              disabled={disabled} onWrite={onWrite} />
          ))}
        </div>
      ))}
      <div className="card col">
        <h4>得分小计（随录入自动加总）</h4>
        {totals.map((entry) => (
          <div className="row wrap" key={entry.domain.domain_key}
            style={{ justifyContent: "space-between" }}>
            <span>{entry.domain.title}</span>
            <span className="mono">
              {`${entry.points}${
                entry.domain.max_score !== null ? ` / ${entry.domain.max_score}` : ""}`}
              {entry.missing > 0 ? `（还有 ${entry.missing} 项未评）` : ""}
            </span>
          </div>
        ))}
        <strong>
          {`总分 ${sum}${scoring.max_score !== null ? ` / ${scoring.max_score}` : ""}`}
          {missing > 0 ? `（${missing} 项计分条目未评）` : ""}
        </strong>
      </div>
    </>
  );
}

export function QuestionnaireDrawer({
  initialRecord, definition, definitionDrifted, client, onClose, onRecordUpdated,
}: {
  initialRecord: QuestionnaireRecord;
  definition: QuestionnaireDefinition;
  /** 定义包字节与记录创建时不一致:只读展示,写入交给服务器也会被拒。 */
  definitionDrifted: boolean;
  client: QuestionnaireRecordClient;
  onClose: () => void;
  onRecordUpdated: (record: QuestionnaireRecord) => void;
}) {
  const [record, setRecord] = useState(initialRecord);
  const recordRef = useRef(record);
  recordRef.current = record;
  const [, bumpPending] = useReducer((count: number) => count + 1, 0);
  const pendingRef = useRef(new Map<string, QuestionnaireValueWrite>());
  const flushingRef = useRef(false);
  const [saving, setSaving] = useState(false);
  const [saveFailure, setSaveFailure] = useState<QuestionnaireFailure | null>(null);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiFailure, setAiFailure] = useState<QuestionnaireFailure | null>(null);
  const [confirmLock, setConfirmLock] = useState(false);
  const [lockBusy, setLockBusy] = useState(false);
  const [lockFailure, setLockFailure] = useState<QuestionnaireFailure | null>(null);
  const panelRef = useDialogFocusTrap<HTMLElement>({
    open: true,
    onCancel: onClose,
    initialFocus: "first-button",
  });

  const editable = record.status === "draft" && !definitionDrifted;
  const pendingCount = pendingRef.current.size;

  const applyRecord = (next: QuestionnaireRecord) => {
    setRecord(next);
    onRecordUpdated(next);
  };

  const flush = async (): Promise<void> => {
    if (flushingRef.current || pendingRef.current.size === 0) return;
    flushingRef.current = true;
    setSaving(true);
    const snapshot = [...pendingRef.current.values()];
    const outcome = await performQuestionnaireMutation(
      () => client.putValues(recordRef.current, snapshot),
      (receipt) => {
        // 先 await 成功才清本地:只清与本次快照一致的项,保存期间新点的档位留下。
        for (const entry of snapshot) {
          const key = questionnaireSlotKey(entry.item_key, entry.field_key);
          const current = pendingRef.current.get(key);
          if (current && current.value === entry.value) pendingRef.current.delete(key);
        }
        applyRecord(receipt);
      },
    );
    flushingRef.current = false;
    setSaving(false);
    bumpPending();
    if (!outcome.ok) {
      setSaveFailure({ message: outcome.message, problems: outcome.problems });
      return;
    }
    setSaveFailure(null);
    if (pendingRef.current.size > 0) void flush();
  };

  const queueWrites = (entries: QuestionnaireValueWrite[]) => {
    if (!editable || entries.length === 0) return;
    for (const entry of entries) {
      pendingRef.current.set(
        questionnaireSlotKey(entry.item_key, entry.field_key), entry);
    }
    bumpPending();
    void flush();
  };

  const runAiDraft = async () => {
    if (!editable || aiBusy) return;
    setAiBusy(true);
    setAiFailure(null);
    const outcome = await performQuestionnaireMutation(
      () => client.generateAiDraft(recordRef.current), applyRecord);
    setAiBusy(false);
    if (!outcome.ok) setAiFailure({ message: outcome.message, problems: outcome.problems });
  };

  const lockNow = async () => {
    if (lockBusy) return;
    setLockBusy(true);
    setLockFailure(null);
    const outcome = await performQuestionnaireMutation(
      () => client.lock(recordRef.current), applyRecord);
    setLockBusy(false);
    setConfirmLock(false);
    if (!outcome.ok) setLockFailure({ message: outcome.message, problems: outcome.problems });
  };

  // 层叠在量表抽屉之上:Escape 只收自己这一层,拦住事件别让底下的抽屉一起关。
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    if (confirmLock) {
      if (!lockBusy) setConfirmLock(false);
      return;
    }
    onClose();
  };

  const finalMap = finalValuesBySlot(record);
  const effective = (itemKey: string, fieldKey: string): string | null => {
    const key = questionnaireSlotKey(itemKey, fieldKey);
    const pending = pendingRef.current.get(key);
    if (pending !== undefined) return pending.value;
    return finalMap.get(key) ?? null;
  };
  const hints = new Map<string, AiDraftHint>();
  for (const slot of record.values) {
    if (slot.ai_draft_value !== null || slot.ai_draft_rationale !== null) {
      hints.set(questionnaireSlotKey(slot.item_key, slot.field_key), {
        value: slot.ai_draft_value,
        rationale: slot.ai_draft_rationale,
      });
    }
  }
  const adoptable = adoptableAiDraftEntries(
    record,
    new Map([...pendingRef.current].map(([key, entry]) => [key, entry.value])),
  );
  const missing = missingLockEntries(definition, record);
  const statusLine = aiDraftStatusLine(record.ai_draft_status);
  const scoreSummary = lockedScoreSummary(record, definition);
  const disabled = !editable || lockBusy;

  const bodyProps = { definition, disabled, effective, hints, onWrite: queueWrites };

  return (
    <div className="drawer-backdrop" onKeyDown={handleKeyDown}>
      <section ref={panelRef} className="drawer-panel fade-in" role="dialog"
        aria-modal="true" aria-labelledby="questionnaire-drawer-title">
        <div className="drawer-header">
          <div>
            <div className="page-kicker">
              受试者 {record.patient_id} · {record.phase_label}
            </div>
            <h2 id="questionnaire-drawer-title">
              {definition.title}
            </h2>
          </div>
          <div className="row">
            <StatusPill tone={record.status === "locked" ? "ok" : "warn"}>
              {questionnaireStatusLabel(record)}
            </StatusPill>
            <Button onClick={onClose}>关闭</Button>
          </div>
        </div>

        <Alert tone="warn" compact title={QUESTIONNAIRE_TRIAL_NOTICE}>
          请照原表口头施测；这里只做电子记录，逐项由施测者最终判定。
        </Alert>
        <p className="muted">{definition.instruction}</p>

        {definitionDrifted && (
          <Alert tone="danger" title="量表定义已更新，这份记录只能查看">
            定义包与记录建立时不一致，不能继续填写；请新建记录后重新录入。
          </Alert>
        )}

        {record.status === "locked" && (
          <Alert tone="ok" title="记录已锁定，以下为最终结果">
            {scoreSummary ?? "此量表的源表未定义总分，以逐条目结果为准。"}
            {record.locked_by ? ` 锁定人：${record.locked_by}。` : ""}
          </Alert>
        )}

        {editable && definition.response_kind !== "examiner_scored" && (
          <div className="card col">
            <div className="row wrap" style={{ justifyContent: "space-between" }}>
              <h3>AI 初评</h3>
              <Button onClick={() => void runAiDraft()} disabled={aiBusy}>
                {aiBusy ? "正在生成…" : "AI 初评（仅供核对）"}
              </Button>
            </div>
            {statusLine && <p className="muted">{statusLine}</p>}
            {aiFailure && (
              <Alert tone="danger" title="AI 初评没有完成">{aiFailure.message}</Alert>
            )}
            {record.ai_draft_status === "generated" && adoptable.length > 0 && (
              <Button variant="primary" onClick={() => queueWrites(adoptable)}>
                采纳全部 AI 建议（{adoptable.length} 项）
              </Button>
            )}
            {record.ai_draft_status === "generated" && adoptable.length === 0 && (
              <p className="muted">AI 建议已全部采纳或被人工覆盖。</p>
            )}
          </div>
        )}

        {saveFailure && (
          <Alert tone="danger" title="作答尚未保存成功"
            actions={<Button onClick={() => void flush()}>重试保存</Button>}>
            {saveFailure.message}
            {saveFailure.problems.length > 0 && (
              <ul>
                {saveFailure.problems.map((problem) => <li key={problem}>{problem}</li>)}
              </ul>
            )}
            刚点的档位仍保留在屏幕上；请重试保存，保存成功前不要关闭。
          </Alert>
        )}

        {definition.response_kind === "ordinal_sections" && <OrdinalSectionsBody {...bodyProps} />}
        {definition.response_kind === "binary_scored" && <BinaryScoredBody {...bodyProps} />}
        {definition.response_kind === "symptom_triplet" && <SymptomTripletBody {...bodyProps} />}
        {definition.response_kind === "examiner_scored" && <ExaminerScoredBody {...bodyProps} />}

        {lockFailure && (
          <Alert tone="danger" title="锁定被拒绝">
            {lockFailure.message}
            {lockFailure.problems.length > 0 && (
              <ul>
                {lockFailure.problems.map((problem) => <li key={problem}>{problem}</li>)}
              </ul>
            )}
          </Alert>
        )}

        {editable && (
          <div className="drawer-actions">
            <div className="col" style={{ alignItems: "flex-end" }}>
              {(saving || pendingCount > 0) && (
                <small className="muted">作答正在保存，稍候才能锁定。</small>
              )}
              <Button variant="danger"
                disabled={!editable || saving || pendingCount > 0 || lockBusy}
                onClick={() => { setLockFailure(null); setConfirmLock(true); }}>
                锁定这份记录
              </Button>
            </div>
          </div>
        )}

        <ConfirmDialog open={confirmLock} title="锁定这份量表记录？"
          confirmLabel="锁定" busy={lockBusy}
          // 预检已知必被服务器拒绝时,确认钮直接禁用(P2-5)——不给"点了也白点"的按钮。
          confirmDisabled={missing.length > 0}
          onConfirm={() => void lockNow()}
          onCancel={() => setConfirmLock(false)}
          body={(
            <div className="col">
              <p>锁定后不能再修改；发现错误只能新建记录重新录入。</p>
              {missing.length > 0 ? (
                <>
                  <p>还有 {missing.length} 处未完成，暂不能锁定——请先补齐：</p>
                  <ul>
                    {missing.slice(0, 12).map((entry) => <li key={entry}>{entry}</li>)}
                    {missing.length > 12 && <li>……共 {missing.length} 处</li>}
                  </ul>
                </>
              ) : (
                <p>所有条目均已作答。</p>
              )}
            </div>
          )} />
      </section>
    </div>
  );
}
