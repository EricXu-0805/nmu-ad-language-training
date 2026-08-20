"""量表 AI 初评引擎（原型）：聚合训练统计 → 逐条目建议等级。

红线口径：出网载荷只有量表条目文本（工具文本，非患者数据）与聚合数字统计
（人工锁定分的计数/均值、提示层级分布、周次覆盖），不含转写、姓名、日期、
题目 ID、自由文本。逐受试者云处理授权（cloud_processing_allowed）是硬前置。

初评永不落终值列：产物只写 ai_draft_value / ai_draft_rationale，
由施测者逐项核验后另行落 final_value。没有依据的条目建议为 null——
这不是失败，是「平台数据看不出来」的诚实答案。

只对 observer + ordinal_sections 的量表（今天即 SFACS）生成；
GDS-15 的答案只能问受试者本人，NPI-Q 的症状平台无可观察证据，均判 not_applicable。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Optional

from sqlmodel import Session as DBSession, select

from .models import ItemEvent, Patient, Session as TrainSession, TurnEvent
from .questionnaires import QuestionnaireDefinition

DRAFT_ENGINE_ID = "dashscope/qwen-plus/questionnaire-draft.v1"
_MODEL = "qwen-plus"
_MAX_RATIONALE_CHARS = 200


@dataclass(frozen=True)
class DraftItem:
    value: Optional[str]
    rationale: str


@dataclass(frozen=True)
class DraftOutcome:
    status: str  # generated / not_applicable / unavailable_no_data / unavailable_not_authorized / failed
    engine: Optional[str]
    items: dict[str, DraftItem] = field(default_factory=dict)


def _cloud_authorized(patient: Patient) -> bool:
    return (patient.cloud_processing_allowed is True
            and patient.cloud_processing_revoked_at is None)


def build_evidence(s: DBSession, patient_id: str) -> dict | None:
    """人工锁定分的聚合统计；没有任何锁定证据返回 None。全部为数字/闭集类别。"""
    base = (
        select(TurnEvent, ItemEvent.task_type, TrainSession.week_no)
        .join(ItemEvent, TurnEvent.item_event_id == ItemEvent.id)
        .join(TrainSession, ItemEvent.session_id == TrainSession.session_id)
        .where(
            TrainSession.patient_id == patient_id,
            TrainSession.is_simulation == False,  # noqa: E712 — SQL 布尔列
            TrainSession.data_classification == "research",
            TurnEvent.score_locked == True,  # noqa: E712
        )
    )
    rows = list(s.exec(base))
    if not rows:
        return None
    by_group: dict[tuple[str, str], list[float]] = {}
    weeks: set[int] = set()
    prompt_levels: dict[str, int] = {}
    latencies: list[int] = []
    for turn, task_type, week_no in rows:
        weeks.add(int(week_no))
        role = turn.response_role or "未标注"
        key = (str(task_type.value if hasattr(task_type, "value") else task_type),
               role)
        if turn.element_value is not None:
            by_group.setdefault(key, []).append(float(turn.element_value))
        level = "未记录" if turn.prompt_level is None else str(int(turn.prompt_level))
        prompt_levels[level] = prompt_levels.get(level, 0) + 1
        if turn.naming_latency_ms is not None:
            latencies.append(int(turn.naming_latency_ms))
    groups = [
        {
            "task_type": task_type,
            "response_role": role,
            "locked_turns": len(scores),
            "mean_locked_score": round(sum(scores) / len(scores), 3),
        }
        for (task_type, role), scores in sorted(by_group.items())
        if scores
    ]
    return {
        "locked_turn_count": len(rows),
        "weeks_covered": sorted(weeks),
        "score_groups": groups,
        "prompt_level_counts": dict(sorted(prompt_levels.items())),
        "mean_naming_latency_ms": (
            round(sum(latencies) / len(latencies)) if latencies else None),
    }


def _build_prompt(definition: QuestionnaireDefinition, evidence: dict) -> str:
    assert definition.value_field is not None
    items = [
        {"item_key": item.item_key, "text": item.text}
        for item in definition.all_items()
    ]
    payload = {
        "scale_title": definition.title,
        "value_anchors": definition.value_field.anchors,
        "items": items,
        "aggregate_training_stats": evidence,
    }
    return (
        "你是临床研究助理。下面是一份沟通量表的条目，以及一位受试者在语言训练"
        "平台上的聚合训练统计（不含任何个人信息）。请仅基于这些统计，为每个"
        "能找到直接依据的条目给出建议等级；没有依据的条目 value 必须是 null，"
        "不许猜。这是未经验证的辅助初评，将由施测者逐项人工核验。\n"
        "统计口径：mean_locked_score 是人工锁定的 0/1 得分均值；"
        "prompt_level 越高表示需要越多提示；weeks_covered 是训练周次。\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n"
        "输出严格 JSON，形如 {\"drafts\": {\"<item_key>\": {\"value\": "
        "\"7\"|\"6\"|\"5\"|\"4\"|\"3\"|\"2\"|\"1\"|\"N\"|null, "
        "\"rationale\": \"不超过60字的中文理由\"}}}，"
        "drafts 必须覆盖全部条目键，只输出该 JSON。"
    )


def _call_llm(prompt: str) -> str | None:
    from dashscope import Generation
    resp = Generation.call(model=_MODEL,
                           messages=[{"role": "user", "content": prompt}],
                           result_format="message",
                           response_format={"type": "json_object"},
                           temperature=0.1,
                           request_timeout=30)
    out = getattr(resp, "output", None)
    choices = getattr(out, "choices", None) if out is not None else None
    if not choices:
        return None
    return choices[0].message.content


def _parse_drafts(raw: object,
                  definition: QuestionnaireDefinition) -> dict[str, DraftItem] | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or set(data) != {"drafts"}:
        return None
    drafts = data["drafts"]
    if not isinstance(drafts, dict):
        return None
    assert definition.value_field is not None
    allowed = set(definition.value_field.allowed)
    known_keys = {item.item_key for item in definition.all_items()}
    parsed: dict[str, DraftItem] = {}
    for item_key, entry in drafts.items():
        if item_key not in known_keys:
            continue  # 未知键丢弃：闭集之外的建议一律不落库
        if not isinstance(entry, dict) or set(entry) != {"value", "rationale"}:
            return None
        value = entry["value"]
        rationale = entry["rationale"]
        if value is not None and (not isinstance(value, str) or value not in allowed):
            return None
        if not isinstance(rationale, str) or len(rationale) > _MAX_RATIONALE_CHARS:
            return None
        parsed[item_key] = DraftItem(value=value, rationale=rationale)
    if not parsed:
        return None
    return parsed


def generate_draft(s: DBSession, patient: Patient,
                   definition: QuestionnaireDefinition) -> DraftOutcome:
    if (definition.response_kind != "ordinal_sections"
            or definition.respondent != "observer"):
        return DraftOutcome(status="not_applicable", engine=None)
    if not _cloud_authorized(patient):
        return DraftOutcome(status="unavailable_not_authorized", engine=None)
    if not os.environ.get("DASHSCOPE_API_KEY"):
        return DraftOutcome(status="failed", engine=DRAFT_ENGINE_ID)
    evidence = build_evidence(s, patient.patient_id)
    if evidence is None:
        return DraftOutcome(status="unavailable_no_data", engine=None)
    try:
        raw = _call_llm(_build_prompt(definition, evidence))
    except Exception:  # noqa: BLE001 — 云端任何异常都只降级为 failed，不冒充结果
        return DraftOutcome(status="failed", engine=DRAFT_ENGINE_ID)
    parsed = _parse_drafts(raw, definition)
    if parsed is None:
        return DraftOutcome(status="failed", engine=DRAFT_ENGINE_ID)
    return DraftOutcome(status="generated", engine=DRAFT_ENGINE_ID, items=parsed)
