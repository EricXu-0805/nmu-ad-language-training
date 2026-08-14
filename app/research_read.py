"""只读研究数据面的取数层。

只做投影，不做门禁解释：能不能读由 ``access_policy`` 与中间件决定；这里只保证
**读出来的东西符合去标识口径**，并在越界时 fail-closed 而不是降级。

三条硬边界：
  - 未配置 ``DEIDENTIFICATION_KEY`` 时，**一切带数据行的端点一律 503**，且
    **绝不**退化成返回明文 patient_id。``meta`` 与 ``dictionary`` 例外——
    它们完全由静态列注册表生成，一个受试者字段都不碰：拦住它们保护不了任何东西，
    只会让人在最需要弄清"这个接口到底出哪些列"的时候看不到答案。
    这个例外由测试钉住：字典的响应里出现任何一行数据即视为回归。
  - 输出列是闭集（``research_dataset`` 的注册表），多带的键会被丢弃；
  - 序列化之前统一过 ``export_security.assert_deidentified_sheets``。

分页用 keyset 而不是 offset：offset 在并发写入下会漏行或重行，而研究取数最不该
出现的就是"每次拉到的行数不一样"。

游标里装的是数据库自然键（patient_id / session_id），**那是本接口明令禁出的直接
标识符**，所以它必须是真加密而不是只签名。第一版只做了 HMAC 签名、载荷是明文
base64，`encode_cursor(["P-REAL-042"])` 解码出来就是 `["P-REAL-042"]`——签名防的是
篡改，从来不防读。现在走 AES-GCM（密钥由去标识密钥分域派生），密文与随机 nonce
一起编码；游标同时绑定数据集名，跨数据集复用会被拒而不是静默截断。
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import secrets
from typing import Any, Iterable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import and_, or_
from sqlmodel import Session as DBSession, select

from . import export_security, research_dataset, session_admission
from .models import (AttemptEvent, ItemEvent, Patient, Session,
                     SessionRuntimeState, TurnEvent)


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
_CURSOR_VERSION = b"\x02"
_CURSOR_NONCE_BYTES = 12
_CURSOR_INVALID = "分页游标无效，请从第一页重新开始"


def _cursor_key(config: export_security.DeidentificationConfig) -> bytes:
    """分域派生：游标密钥与假名密钥同源但不同域，互相拿不到对方。"""
    return hmac.new(config.key, b"nmu-research-cursor-aead:v2",
                    hashlib.sha256).digest()


def _cursor_reject() -> ResearchReadUnavailable:
    return ResearchReadUnavailable("research_cursor_invalid", _CURSOR_INVALID)


def encode_cursor(key: list[Any],
                  config: export_security.DeidentificationConfig,
                  dataset: str) -> str:
    """把自然键加密成游标。dataset 进 AAD，跨数据集复用会解不开。"""
    payload = json.dumps(key, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    nonce = secrets.token_bytes(_CURSOR_NONCE_BYTES)
    sealed = AESGCM(_cursor_key(config)).encrypt(
        nonce, payload, _CURSOR_VERSION + dataset.encode("utf-8"))
    return base64.urlsafe_b64encode(
        _CURSOR_VERSION + nonce + sealed).decode("ascii").rstrip("=")


def decode_cursor(cursor: str,
                  config: export_security.DeidentificationConfig,
                  dataset: str) -> list[Any]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        blob = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:
        raise _cursor_reject() from exc
    if len(blob) <= 1 + _CURSOR_NONCE_BYTES or blob[:1] != _CURSOR_VERSION:
        raise _cursor_reject()
    nonce = blob[1:1 + _CURSOR_NONCE_BYTES]
    try:
        payload = AESGCM(_cursor_key(config)).decrypt(
            nonce, blob[1 + _CURSOR_NONCE_BYTES:],
            _CURSOR_VERSION + dataset.encode("utf-8"))
        decoded = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise _cursor_reject() from exc
    if not isinstance(decoded, list):
        raise _cursor_reject()
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


def _is_withdrawn(patient: Patient) -> bool:
    """撤回判定直接借全仓那一个，不在这里另写一份。

    第一版在这里自建了白名单（放行 "active"/"not_withdrawn"），而且只看
    ``withdrawal_status``。两处都错：口径一分岔，同一个受试者会在别处算已撤回、
    在这里算在训，全量明细照出；而**撤回还有一支只体现在知情同意字段上**
    （见 ``main.py`` 里那句"A legacy record may express study withdrawal only
    through the consent field"），只看 withdrawal_status 会整支漏掉。
    ``session_admission.patient_content_sealed`` 是全仓的权威判据，六个别的读面
    都用它。宁可多发墓碑，不可少发。
    """
    return session_admission.patient_content_sealed(patient)


def _subject_is_research(patient: Patient) -> bool:
    return not bool(patient.is_simulation_subject)


def list_subjects(db: DBSession, *, config, data_classification: str,
                  cursor: str | None, limit: int) -> dict[str, Any]:
    after = decode_cursor(cursor, config, "subjects")[0] if cursor else None
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
        subject_code = export_security.pseudonymize_subject(
            patient.patient_id, config)
        count = session_counts.get(patient.patient_id, 0)
        if _is_withdrawn(patient):
            # 撤回者也走墓碑。第一版只把 withdrawn 置 true、其余临床属性照发，
            # 于是 secondary_use_allowed 仍是 true——那等于对 PI 说"这个人可以
            # 二次利用"，而他恰恰撤回了。场次数留着，因为分母要稳。
            rows.append(_tombstone("subjects", {
                "subject_code": subject_code,
                "withdrawn": True,
                "session_count": count,
            }))
            continue
        rows.append({
            "subject_code": subject_code,
            "dementia_severity": patient.dementia_severity,
            "mandarin_eligible": patient.mandarin_eligible,
            "is_simulation_subject": bool(patient.is_simulation_subject),
            "secondary_use_allowed": patient.secondary_use_allowed,
            "withdrawn": False,
            "session_count": count,
        })
    next_cursor = (encode_cursor([page[-1].patient_id], config, "subjects")
                   if page and has_more else None)
    return _envelope("subjects", rows, next_cursor, has_more, config)


def _withdrawn_patient_ids(db: DBSession) -> set[str]:
    withdrawn: set[str] = set()
    for patient in db.exec(select(Patient)):
        if _is_withdrawn(patient):
            withdrawn.add(patient.patient_id)
    return withdrawn


def list_sessions(db: DBSession, *, config, data_classification: str,
                  cursor: str | None, limit: int) -> dict[str, Any]:
    after = decode_cursor(cursor, config, "sessions")[0] if cursor else None
    # 字典把 runtime_status 登记成真变量，取数层却写死 null——那比不给这一列更坏，
    # PI 会照着它建分析。终态存在 SessionRuntimeState 里，一次查完。
    runtime_status = {
        state.session_id: state.status
        for state in db.exec(select(SessionRuntimeState))
    }
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
                "withdrawn": True, "pseudonym_key_id": config.key_id,
            }))
            continue
        rows.append({
            "session_code": session_code,
            "subject_code": subject_code,
            "week_no": sess.week_no,
            "phase_type": getattr(sess.phase_type, "value", sess.phase_type),
            "event_line": getattr(sess.event_line, "value", sess.event_line),
            "session_sitting_no": sess.session_sitting_no,
            "runtime_status": runtime_status.get(sess.session_id),
            "is_simulation": bool(sess.is_simulation),
            "data_classification": sess.data_classification,
            "item_bank_version_id": sess.item_bank_version_id,
            "autopilot_profile_version_id": sess.autopilot_profile_version_id,
            "withdrawn": False,
            "pseudonym_key_id": config.key_id,
        })
    next_cursor = (encode_cursor([page[-1].session_id], config, "sessions")
                   if page and has_more else None)
    return _envelope("sessions", rows, next_cursor, has_more, config)


def list_turns(db: DBSession, *, config, data_classification: str,
               cursor: str | None, limit: int) -> dict[str, Any]:
    """按 (场次, 题目, 环节序号) 的 keyset 翻页。

    第一版是在 Python 里翻：把该分区的全部场次、每个场次的全部题目、每道题的
    全部环节都取出来，再用 ``key <= after`` 逐行跳过。于是第 k 页要为前
    (k-1)×limit 行重跑一遍全部 I/O——页数越深越慢，而这套服务和床旁训练同进程、
    同一台 1 GiB 的机器。判据推进 SQL，一页就是一次带 LIMIT 的查询。
    """
    after = decode_cursor(cursor, config, "turns") if cursor else None
    statement = (
        select(TurnEvent, ItemEvent, Session)
        .join(ItemEvent, TurnEvent.item_event_id == ItemEvent.id)
        .join(Session, ItemEvent.session_id == Session.session_id)
        .where(Session.data_classification == data_classification)
        .order_by(ItemEvent.session_id, ItemEvent.item_id, TurnEvent.turn_seq)
    )
    if after is not None:
        session_after, item_after, seq_after = after
        statement = statement.where(or_(
            ItemEvent.session_id > session_after,
            and_(ItemEvent.session_id == session_after,
                 ItemEvent.item_id > item_after),
            and_(ItemEvent.session_id == session_after,
                 ItemEvent.item_id == item_after,
                 TurnEvent.turn_seq > seq_after),
        ))
    picked = list(db.exec(statement.limit(limit + 1)))
    page, has_more = _page(picked, limit)

    withdrawn = _withdrawn_patient_ids(db)
    # 只查这一页真正引用到的 attempt，别把整张表拉进来——那会把刚省下的
    # 全表扫描从 turn 挪到 attempt 上。
    attempt_ids = {turn.source_attempt_id for turn, _, _ in page
                   if turn.source_attempt_id is not None}
    attempt_seq: dict[Any, int] = {}
    if attempt_ids:
        attempt_seq = {
            attempt.id: attempt.attempt_seq
            for attempt in db.exec(select(AttemptEvent).where(
                AttemptEvent.id.in_(attempt_ids)))
        }

    rows: list[dict[str, Any]] = []
    last_key: list[Any] | None = None
    for turn, item, sess in page:
        subject_code = export_security.pseudonymize_subject(
            sess.patient_id, config)
        session_code = export_security.pseudonymize_session(
            sess.session_id, config)
        if sess.patient_id in withdrawn:
            # 自然键必须留着。全置 null 会让一个 36 环节的场次返回 36 行逐字节
            # 相同的行，`distinct()` 一跑塌成 1——而墓碑存在的唯一理由就是保住
            # 这个分母。item_id 与 turn_seq 本来就是公开列，不是标识符。
            rows.append(_tombstone("turns", {
                "session_code": session_code,
                "subject_code": subject_code,
                "item_id": item.item_id,
                "turn_seq": turn.turn_seq,
                "withdrawn": True,
            }))
        else:
            rows.append(_turn_row(
                session_code, subject_code, item, turn,
                attempt_seq.get(turn.source_attempt_id)))
        last_key = [sess.session_id, item.item_id, turn.turn_seq]

    next_cursor = (encode_cursor(last_key, config, "turns")
                   if has_more and last_key is not None else None)
    return _envelope("turns", rows, next_cursor, has_more, config)


def _turn_row(session_code: str, subject_code: str,
              item: ItemEvent, turn: TurnEvent,
              source_attempt_seq: int | None) -> dict[str, Any]:
    diff = (None if turn.reviewed_score is None or turn.ai_score is None
            else round(turn.reviewed_score - turn.ai_score, 4))
    return {
        "session_code": session_code,
        "subject_code": subject_code,
        "item_id": item.item_id,
        "task_type": getattr(item.task_type, "value", item.task_type),
        "turn_seq": turn.turn_seq,
        "response_role": getattr(turn.response_role, "value", turn.response_role),
        "source_attempt_seq": source_attempt_seq,
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
        # 在训的人必须是 False 而不是 null：SPSS 里 null 是缺失值，
        # `filter withdrawn = 0` 会把在训的人一起滤掉。
        "withdrawn": False,
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


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def render_csv(header: list[str], matrix: list[list[Any]]) -> bytes:
    """带 BOM 的 UTF-8 CSV。

    BOM 不是装饰：Windows 上的 Excel 和 SPSS 不看 BOM 就按本地代码页解，
    中文列名直接乱码。JSON 端点保持纯 UTF-8，只有 CSV 走 utf-8-sig。
    每个单元过 sanitize_csv_cell 中和公式注入（``=``/``+``/``-``/``@`` 开头的
    字符串在表格软件里会被当公式执行）。
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([export_security.sanitize_csv_cell(name) for name in header])
    for row in matrix:
        writer.writerow([export_security.sanitize_csv_cell(cell) for cell in row])
    return buffer.getvalue().encode("utf-8-sig")


def dataset_csv(payload: dict[str, Any]) -> bytes:
    header = list(payload["columns"])
    matrix = [[row.get(name) for name in header] for row in payload["rows"]]
    return render_csv(header, matrix)


def dictionary_csv() -> bytes:
    rows = research_dataset.dictionary_rows()
    header = ["dataset", "column", "disclosure", "dtype", "unit",
              "description", "source", "published"]
    return render_csv(header, [[row.get(name) for name in header] for row in rows])
