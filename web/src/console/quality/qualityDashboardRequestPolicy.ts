import type { DataClassification } from "../dataClassification";
import type { QualityDataClassification } from "./qualityDashboardContract";

export function qualityDashboardRequestClassification(
  classification: DataClassification,
): QualityDataClassification | null {
  return classification === "research" || classification === "simulation"
    ? classification
    : null;
}

export function qualityDashboardRequestPath(classification: QualityDataClassification): string {
  const query = new URLSearchParams({ data_classification: classification });
  return `/quality/ai-metrics?${query.toString()}`;
}
