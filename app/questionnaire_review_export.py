"""Single-subject operational CSVs, independent of training-session cohorts.

These trial questionnaire records are never a frozen research release.  Routes
must authorize a governance account and hold the subject withdrawal fence while
building and auditing the payload.  Only closed-domain definition values leave
this module; profile labels, raw answers and notes are intentionally absent.
"""
from __future__ import annotations

import csv
import io
from typing import Literal

from sqlmodel import Session, select

from . import export_security, questionnaires
from .models import Patient, QuestionnaireItemValue, QuestionnaireRecord

Dataset = Literal["records", "items"]
EXPORT_KIND = "operational_questionnaire_review"
_COMMON = (
    "export_kind", "is_frozen", "questionnaire_status", "data_classification",
    "pseudonym_key_id", "subject_code", "record_code", "questionnaire_id",
    "phase_label", "phase_ordinal", "superseded_by_ordinal", "definition_sha256",
)
_COLUMNS = {
    "records": _COMMON + (
        "scoring_rule_id", "computed_total", "cutoff_met", "computed_flag",
    ),
    "items": _COMMON + ("item_number", "item_key", "field_key", "final_value"),
}


class ReviewExportUnavailable(ValueError):
    code = "questionnaire_review_unavailable"


def build_csv(
        db: Session, *, patient: Patient, dataset: Dataset,
        config: export_security.DeidentificationConfig) -> bytes:
    if dataset not in _COLUMNS:
        raise ReviewExportUnavailable("请选择量表总分或逐条目核对表")
    if ((patient.withdrawal_status or "").strip()
            or (patient.consent_status or "").strip().casefold()
            in {"withdrawn", "denied", "declined", "refused", "已撤回", "未同意", "不同意"}):
        raise ReviewExportUnavailable("受试者已撤回或拒绝，不能导出量表核对表")
    records = list(db.exec(select(QuestionnaireRecord).where(
        QuestionnaireRecord.patient_id == patient.patient_id,
        QuestionnaireRecord.status == "locked",
    ).order_by(QuestionnaireRecord.questionnaire_id,
               QuestionnaireRecord.phase_label,
               QuestionnaireRecord.phase_ordinal)))
    registry = questionnaires.load_questionnaire_registry()
    rows: list[dict] = []
    for record in records:
        loaded = registry.get(record.questionnaire_id)
        if loaded is None or loaded.content_sha256 != record.definition_sha256:
            raise ReviewExportUnavailable("量表定义与锁定记录不一致，请先核对原始版本")
        if record.phase_label not in {"前测", "后测", "随访", "其他"}:
            raise ReviewExportUnavailable("量表期别异常，请先核对记录")
        values = list(db.exec(select(QuestionnaireItemValue).where(
            QuestionnaireItemValue.record_id == record.record_id)))
        definition = loaded.definition
        final_values: dict[tuple[str, str], str] = {}
        try:
            for value in values:
                questionnaires.validate_value_write(
                    definition, value.item_key, value.field_key, value.final_value)
                if value.final_value is not None:
                    final_values[(value.item_key, value.field_key)] = value.final_value
            questionnaires.assert_lock_complete(definition, final_values)
            expected_score = questionnaires.compute_scoring(definition, final_values)
        except questionnaires.QuestionnaireValidationError as exc:
            raise ReviewExportUnavailable("锁定量表内容异常，请先核对原始记录") from exc
        # Persisted totals are the recorded evidence. Never silently repair a
        # corrupt record while exporting, or trust an arbitrary stored label.
        score_keys = ("scoring_rule_id", "computed_total", "cutoff_met", "computed_flag")
        if any(getattr(record, key) != (expected_score or {}).get(key)
               for key in score_keys):
            raise ReviewExportUnavailable("锁定量表分数与原表规则不一致，请先核对记录")
        common = {
            "export_kind": EXPORT_KIND,
            "is_frozen": False,
            "questionnaire_status": "prototype",
            "data_classification": "simulation" if patient.is_simulation_subject else "research",
            "pseudonym_key_id": config.key_id,
            "subject_code": export_security.pseudonymize_subject(patient.patient_id, config),
            "record_code": export_security.pseudonymize_questionnaire_record(record.record_id, config),
            "questionnaire_id": record.questionnaire_id,
            "phase_label": record.phase_label,
            "phase_ordinal": record.phase_ordinal,
            "superseded_by_ordinal": record.superseded_by_ordinal,
            "definition_sha256": record.definition_sha256,
        }
        if dataset == "records":
            rows.append({**common, **{key: getattr(record, key) for key in score_keys}})
        else:
            item_numbers = {item.item_key: item.no for item in definition.all_items()}
            # Section-wide SFACS elements have no question number. Keep it empty
            # rather than inventing a numeric item that could be mistaken for a
            # source questionnaire question.
            for (item_key, field_key), final_value in sorted(final_values.items()):
                rows.append({**common, "item_number": item_numbers.get(item_key),
                             "item_key": item_key, "field_key": field_key,
                             "final_value": final_value})
    export_security.assert_deidentified_sheets({dataset: rows})
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    columns = _COLUMNS[dataset]
    writer.writerow(columns)
    for row in rows:
        writer.writerow([export_security.sanitize_csv_cell(row.get(key)) for key in columns])
    return stream.getvalue().encode("utf-8-sig")
