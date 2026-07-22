import { StatusPill } from "../components/StatusPill";
import {
  DATA_CLASSIFICATIONS,
  DATA_CLASSIFICATION_META,
  type DataClassification,
  type DataClassificationCounts,
} from "./dataClassification";

export function DataBoundaryFilter({ value, counts, onChange, label = "数据分区" }: {
  value: DataClassification;
  counts: DataClassificationCounts;
  onChange: (value: DataClassification) => void;
  label?: string;
}) {
  return (
    <div className="segmented-control" role="group" aria-label={label}>
      {DATA_CLASSIFICATIONS.map((classification) => (
        <button
          key={classification}
          type="button"
          className="segmented-control__button"
          aria-pressed={value === classification}
          onClick={() => onChange(classification)}
        >
          {DATA_CLASSIFICATION_META[classification].label} · {counts[classification]}
        </button>
      ))}
    </div>
  );
}

export function DataBoundaryBadge({ classification, entity = "session" }: {
  classification: DataClassification;
  entity?: "patient" | "plan" | "session";
}) {
  const meta = DATA_CLASSIFICATION_META[classification];
  return (
    <StatusPill tone={meta.tone} size="sm">
      {entity === "patient" ? meta.patientLabel
        : entity === "plan" ? meta.planLabel
          : meta.sessionLabel}
    </StatusPill>
  );
}
