"""只读研究数据面的取数层。

只做投影，不做门禁解释：能不能读由 ``access_policy`` 与中间件决定；这里只保证
**读出来的东西符合去标识口径**，并在越界时 fail-closed 而不是降级。

三条硬边界：
  - 未配置 ``DEIDENTIFICATION_KEY`` 时，除 ``meta`` 外一律拒绝，且**绝不**退化成
    返回明文 patient_id；
  - 输出列是闭集（``research_dataset`` 的注册表），多带的键会被丢弃；
  - 序列化之前统一过 ``export_security.assert_deidentified_sheets``。

分页用 keyset 而不是 offset：offset 在并发写入下会漏行或重行，而研究取数最不该
出现的就是"每次拉到的行数不一样"。游标对外不透明，且用去标识密钥签名——它编码
的是自然键，签名只是防止有人手改游标去试探不属于自己的范围。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Iterable

from sqlmodel import Session as DBSession, select

from . import export_security, research_dataset
from .models import ItemEvent, Patient, Session, TurnEvent


MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 200


class ResearchReadUnavailable(RuntimeError):
    """稳定、无路径、无密钥内容的拒绝。"""

    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code = code
        self.message = message


def load_config() -> export_security.DeidentificationConfig:
    try:
        return export_security.load_deidentification_config()
    except export_security.DeidentificationConfigurationError as exc:
        raise ResearchReadUnavailable(
            "research_deidentification_unavailable",
            "去标识密钥未配置，研究数据接口已关闭；请由数据管理员在服务器配置后重试",
        ) from exc


# ---------------------------------------------------------------------------
# 游标
# ---------------------------------------------------------------------------
def _sign(payload: bytes, config: export_security.DeidentificationConfig) -> str:
    digest = hmac.new(config.key, b"nmu-research-cursor:v1\x00" + payload,
                      hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(payload + digest).decode("ascii").rstrip("=")


def encode_cursor(key: list[Any],
                  config: export_security.DeidentificationConfig) -> str:
    return _sign(json.dumps(key, ensure_ascii=False,
                            separators=(",", ":")).encode("utf-8"), config)


def decode_cursor(cursor: str,
                  config: export_security.DeidentificationConfig) -> list[Any]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        blob = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:
        raise ResearchReadUnavailable(
            "research_cursor_invalid", "分页游标无效，请从第一页重新开始") from exc
    if len(blob) <= 16:
        raise ResearchReadUnavailable(
            "research_cursor_invalid", "分页游标无效，请从第一页重新开始")
    payload, digest = blob[:-16], blob[-16:]
    expected = hmac.new(config.key, b"nmu-research-cursor:v1\x00" + payload,
                        hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(digest, expected):
        raise ResearchReadUnavailable(
            "research_cursor_invalid", "分页游标无效，请从第一页重新开始")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise ResearchReadUnavailable(
            "research_cursor_invalid", "分页游标无效，请从第一页重新开始") from exc
    if not isinstance(decoded, list):
        raise ResearchReadUnavailable(
            "research_cursor_invalid", "分页游标无效，请从第一页重新开始")
    return decoded


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE_SIZE
    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise ResearchReadUnavailable(
            "research_limit_invalid",
            f"每页行数必须在 1 到 {MAX_PAGE_SIZE} 之间")
    return limit


# ---------------------------------------------------------------------------
# 投影
# ---------------------------------------------------------------------------
def _page(rows: list[Any], limit: int) -> tuple[list[Any], bool]:
    """多取一行判断有没有下一页，避免再打一次 count。"""
    return (rows[:limit], len(rows) > limit)


def _subject_is_research(patient: Patient) -> bool:
    return not bool(patient.is_simulation_subject)


def list_subjects(db: DBSession, *, config, data_classification: str,
                  cursor: str | None, limit: int) -> dict[str, Any]:
    after = decode_cursor(cursor, config)[0] if cursor else None
    statement = select(Patient).order_by(Patient.patient_id)
    if after is not None:
        statement = statement.where(Patient.patient_id > after)
    wanted_research = data_classification == "research"
    picked: list[Patient] = []
    for patient in db.exec(statement):
        if _subject_is_research(patient) is not wanted_research:
            continue
        picked.append(patient)
        if len(picked) > limit:
            break
    page, has_more = _page(picked, limit)

    session_counts: dict[str, int] = {}
    for row in db.exec(select(Session.patient_id)):
        pid = row if isinstance(row, str) else row[0]
        session_counts[pid] = session_counts.get(pid, 0) + 1

    rows = []
    for patient in page:
        withdrawn = str(patient.withdrawal_status or "").lower() not in {
            "", "none", "active", "not_withdrawn"}
        rows.append({
            "subject_code": export_security.pseudonymize_subject(
                patient.patient_id, config),
            "dementia_severity": patient.dementia_severity,
            "mandarin_eligible": patient.mandarin_eligible,
            "is_simulation_subject": bool(patient.is_simulation_subject),
            "secondary_use_allowed": patient.secondary_use_allowed,
            "withdrawn": withdrawn,
            "session_count": session_counts.get(patient.patient_id, 0),
        })
    next_cursor = (encode_cursor([page[-1].patient_id], config)
                   if page and has_more else None)
    return _envelope("subjects", rows, next_cursor, has_more, config)


def _withdrawn_patient_ids(db: DBSession) -> set[str]:
    withdrawn: set[str] = set()
    for patient in db.exec(select(Patient)):
        if str(patient.withdrawal_status or "").lower() not in {
                "", "none", "active", "not_withdrawn"}:
            withdrawn.add(patient.patient_id)
    return withdrawn


def list_sessions(db: DBSession, *, config, data_classification: str,
                  cursor: str | None, limit: int) -> dict[str, Any]:
    after = decode_cursor(cursor, config)[0] if cursor else None
    statement = select(Session).where(
        Session.data_classification == data_classification,
    ).order_by(Session.session_id)
    if after is not None:
        statement = statement.where(Session.session_id > after)
    picked = list(db.exec(statement.limit(limit + 1)))
    page, has_more = _page(picked, limit)
    withdrawn = _withdrawn_patient_ids(db)

    rows = []
    for sess in page:
        subject_code = export_security.pseudonymize_subject(
            sess.patient_id, config)
        session_code = export_security.pseudonymize_session(
            sess.session_id, config)
        if sess.patient_id in withdrawn:
            # 撤回的受试者不能直接从长表消失：两次拉取之差会变成"谁撤回了"
            # 的旁路。发一行无内容的墓碑行，分母保持稳定。
            rows.append(_tombstone("sessions", {
                "session_code": session_code, "subject_code": subject_code,
                "pseudonym_key_id": config.key_id,
            }))
            continue
        rows.append({
            "session_code": session_code,
            "subject_code": subject_code,
            "week_no": sess.week_no,
            "phase_type": getattr(sess.phase_type, "value", sess.phase_type),
            "event_line": getattr(sess.event_line, "value", sess.event_line),
            "session_sitting_no": sess.session_sitting_no,
            "runtime_status": None,
            "is_simulation": bool(sess.is_simulation),
            "data_classification": sess.data_classification,
            "item_bank_version_id": sess.item_bank_version_id,
            "autopilot_profile_version_id": sess.autopilot_profile_version_id,
            "pseudonym_key_id": config.key_id,
        })
    next_cursor = (encode_cursor([page[-1].session_id], config)
                   if page and has_more else None)
    return _envelope("sessions", rows, next_cursor, has_more, config)


def list_turns(db: DBSession, *, config, data_classification: str,
               cursor: str | None, limit: int) -> dict[str, Any]:
    after = decode_cursor(cursor, config) if cursor else None
    sessions = {
        sess.session_id: sess
        for sess in db.exec(select(Session).where(
            Session.data_classification == data_classification))
    }
    withdrawn = _withdrawn_patient_ids(db)
    rows: list[dict[str, Any]] = []
    last_key: list[Any] | None = None
    has_more = False

    for session_id in sorted(sessions):
        sess = sessions[session_id]
        items = list(db.exec(select(ItemEvent).where(
            ItemEvent.session_id == session_id).order_by(ItemEvent.item_id)))
        subject_code = export_security.pseudonymize_subject(
            sess.patient_id, config)
        session_code = export_security.pseudonymize_session(session_id, config)
        for item in items:
            turns = list(db.exec(select(TurnEvent).where(
                TurnEvent.item_event_id == item.id).order_by(TurnEvent.turn_seq)))
            for turn in turns:
                key = [session_id, item.item_id, turn.turn_seq]
                if after is not None and key <= after:
                    continue
                if len(rows) >= limit:
                    has_more = True
                    break
                if sess.patient_id in withdrawn:
                    rows.append(_tombstone("turns", {
                        "session_code": session_code,
                        "subject_code": subject_code,
                    }))
                else:
                    rows.append(_turn_row(
                        session_code, subject_code, item, turn))
                last_key = key
            if has_more:
                break
        if has_more:
            break

    next_cursor = (encode_cursor(last_key, config)
                   if has_more and last_key is not None else None)
    return _envelope("turns", rows, next_cursor, has_more, config)


def _turn_row(session_code: str, subject_code: str,
              item: ItemEvent, turn: TurnEvent) -> dict[str, Any]:
    diff = (None if turn.reviewed_score is None or turn.ai_score is None
            else round(turn.reviewed_score - turn.ai_score, 4))
    return {
        "session_code": session_code,
        "subject_code": subject_code,
        "item_id": item.item_id,
        "task_type": getattr(item.task_type, "value", item.task_type),
        "turn_seq": turn.turn_seq,
        "response_role": getattr(turn.response_role, "value", turn.response_role),
        "source_attempt_seq": None,
        "prompt_level": turn.prompt_level,
        "asr_confidence": turn.asr_confidence,
        "ai_answer_type": getattr(turn.ai_answer_type, "value", turn.ai_answer_type),
        "ai_score": turn.ai_score,
        "ai_needs_review": turn.ai_needs_review,
        "ai_judge_mode": turn.ai_judge_mode,
        "reviewed_score": turn.reviewed_score,
        "score_locked": turn.score_locked,
        "element_value": getattr(turn.element_value, "value", turn.element_value),
        "ai_human_diff": diff,
        "judge_portrait_used": turn.judge_portrait_used,
    }


def _tombstone(dataset_key: str, known: dict[str, Any]) -> dict[str, Any]:
    dataset = research_dataset.dataset_for(dataset_key)
    assert dataset is not None
    row: dict[str, Any] = {name: None for name in
                           research_dataset.published_columns(dataset)}
    row.update(known)
    return row


def _envelope(dataset_key: str, rows: Iterable[dict[str, Any]],
              next_cursor: str | None, has_more: bool,
              config) -> dict[str, Any]:
    dataset = research_dataset.dataset_for(dataset_key)
    assert dataset is not None
    projected = research_dataset.project_all(dataset, rows)
    # 序列化前的最后一道闸：与去标识导出包共用同一份断言。
    export_security.assert_deidentified_sheets({dataset_key: projected})
    return {
        "schema_version": research_dataset.SCHEMA_VERSION,
        "dataset": dataset_key,
        "grain": dataset.grain,
        "pseudonym_version": export_security.PSEUDONYM_VERSION,
        "pseudonym_key_id": config.key_id,
        "columns": list(research_dataset.published_columns(dataset)),
        "rows": projected,
        "row_count": len(projected),
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


def build_meta(config_error: ResearchReadUnavailable | None,
               config) -> dict[str, Any]:
    """密钥缺失时**仍然可读**——否则取数的人只拿到一个裸 503，无法自诊。"""
    if config_error is not None:
        return {
            "schema_version": research_dataset.SCHEMA_VERSION,
            "deidentification": {"configured": False,
                                 "reason": config_error.message},
            "datasets": [],
            "note": "配置去标识密钥后本接口才会返回数据集；此前一行也不会返回。",
        }
    return {
        "schema_version": research_dataset.SCHEMA_VERSION,
        "deidentification": {"configured": True,
                             "pseudonym_version": export_security.PSEUDONYM_VERSION,
                             "pseudonym_key_id": config.key_id},
        "datasets": [
            {"key": dataset.key, "title": dataset.title, "grain": dataset.grain,
             "columns": list(research_dataset.published_columns(dataset))}
            for dataset in research_dataset.DATASETS
        ],
        "page": {"default_limit": DEFAULT_PAGE_SIZE, "max_limit": MAX_PAGE_SIZE,
                 "style": "keyset"},
        "note": ("轮换去标识密钥会让所有假名改变，已交付的 CSV 无法与新拉取 join；"
                 "pseudonym_key_id 是唯一的检测手段。"),
    }
