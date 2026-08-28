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

分页用 keyset 而不是 offset：仿真活读用数据库自然键，冻结研究行用发布时的
连续行序号。offset 在并发写入下会漏行或重行，而研究取数最不该出现的就是
"每次拉到的行数不一样"。

仿真活读游标里装数据库自然键（patient_id / session_id），**那是本接口明令
禁出的直接标识符**，所以必须用 AES-GCM 真加密。冻结研究游标只装行序号，
用确定性 HMAC 绑定纪元、快照、数据集与分区；因此同一页能逐字节复读，也不能
跨纪元或跨数据集复用。
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
from .models import (
    AttemptEvent,
    ItemEvent,
    Patient,
    QualityReleaseEpochRowSnapshot,
    QuestionnaireItemValue,
    QuestionnaireRecord,
    Session,
    SessionRuntimeState,
    TurnEvent,
)


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
_SNAPSHOT_CURSOR_VERSION = b"\x03"
_SNAPSHOT_CURSOR_ORDINAL_BYTES = 8
_SNAPSHOT_CURSOR_TAG_BYTES = hashlib.sha256().digest_size
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


def _snapshot_cursor_key(
        config: export_security.DeidentificationConfig) -> bytes:
    """冻结行面不再携带数据库自然键，游标只表示行序号。

    仍然单独分域：不复用活读 AES 游标的密钥，也不复用受试者假名密钥的
    语义。行序号不是标识符，因此只需要防篡改，不需要随机加密；确定性正是
    “同一纪元同一页逐字节相同”的一部分。
    """
    return hmac.new(
        config.key,
        b"nmu-research-snapshot-cursor-hmac:v1",
        hashlib.sha256,
    ).digest()


def _snapshot_cursor_message(
    *, binding: Any, dataset: str, data_classification: str, ordinal: int,
) -> bytes:
    """用规范 JSON 解决变长字段拼接歧义。

    epoch + snapshot + dataset + classification 全进 MAC：旧纪元、另一张表或
    仿真分区拿到的游标都不能被当成一个“合法位置”静默接受。
    """
    return json.dumps(
        [
            binding.epoch_id,
            binding.research_snapshot_sha256,
            dataset,
            data_classification,
            ordinal,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def encode_snapshot_cursor(
    ordinal: int,
    config: export_security.DeidentificationConfig,
    *,
    binding: Any,
    dataset: str,
    data_classification: str,
) -> str:
    if type(ordinal) is not int or ordinal < 1:
        raise _cursor_reject()
    try:
        ordinal_bytes = ordinal.to_bytes(
            _SNAPSHOT_CURSOR_ORDINAL_BYTES, "big", signed=False)
    except OverflowError as exc:
        raise _cursor_reject() from exc
    tag = hmac.new(
        _snapshot_cursor_key(config),
        _snapshot_cursor_message(
            binding=binding,
            dataset=dataset,
            data_classification=data_classification,
            ordinal=ordinal,
        ),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(
        _SNAPSHOT_CURSOR_VERSION + ordinal_bytes + tag
    ).decode("ascii").rstrip("=")


def decode_snapshot_cursor(
    cursor: str,
    config: export_security.DeidentificationConfig,
    *,
    binding: Any,
    dataset: str,
    data_classification: str,
) -> int:
    if (not cursor
            or any(char not in
                   "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                   for char in cursor)):
        raise _cursor_reject()
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        blob = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:
        raise _cursor_reject() from exc
    if (base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")
            != cursor):
        raise _cursor_reject()
    expected_size = (
        1 + _SNAPSHOT_CURSOR_ORDINAL_BYTES + _SNAPSHOT_CURSOR_TAG_BYTES
    )
    if len(blob) != expected_size or blob[:1] != _SNAPSHOT_CURSOR_VERSION:
        # v2 的活读游标也在这里拒绝：它装的是自然键，不是冻结行序号。
        raise _cursor_reject()
    ordinal = int.from_bytes(
        blob[1:1 + _SNAPSHOT_CURSOR_ORDINAL_BYTES], "big", signed=False)
    if ordinal < 1:
        raise _cursor_reject()
    expected_tag = hmac.new(
        _snapshot_cursor_key(config),
        _snapshot_cursor_message(
            binding=binding,
            dataset=dataset,
            data_classification=data_classification,
            ordinal=ordinal,
        ),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(
            blob[1 + _SNAPSHOT_CURSOR_ORDINAL_BYTES:], expected_tag):
        raise _cursor_reject()
    return ordinal


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
                  cursor: str | None, limit: int,
                  binding: Any = None) -> dict[str, Any]:
    if _has_frozen_snapshot(binding):
        return _list_frozen_snapshot(
            db,
            dataset_key="subjects",
            config=config,
            data_classification=data_classification,
            cursor=cursor,
            limit=limit,
            binding=binding,
        )
    after = decode_cursor(cursor, config, "subjects")[0] if cursor else None
    statement = select(Patient).order_by(Patient.patient_id)
    if after is not None:
        statement = statement.where(Patient.patient_id > after)

    # 场次数必须只数冻结进纪元的那些。数全部的话这一列就是个活计数器：新做一
    # 场训练，同一个纪元里同一个人的 session_count 就变了——纪元内两次拉取之差
    # 恒为零这句话立刻不成立，而且它和 sessions 表里真发出去的行对不上。
    counted = select(Session.patient_id)
    cohort_patients: set[str] | None = None
    if binding is not None:
        counted = counted.where(Session.session_id.in_(binding.session_ids))
        cohort_patients = set()
        for row in db.exec(select(Session.patient_id).where(
                Session.session_id.in_(binding.session_ids))):
            cohort_patients.add(row if isinstance(row, str) else row[0])
        # 队列外的受试者整个不出现。留着（哪怕零场次）等于把"谁入组了"做成一条
        # 每次拉取都在更新的名册，而入组本身就是要保护的事实。
        statement = statement.where(Patient.patient_id.in_(cohort_patients))

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
    for row in db.exec(counted):
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
    return _envelope("subjects", rows, next_cursor, has_more, config, binding)


def _withdrawn_patient_ids(db: DBSession) -> set[str]:
    withdrawn: set[str] = set()
    for patient in db.exec(select(Patient)):
        if _is_withdrawn(patient):
            withdrawn.add(patient.patient_id)
    return withdrawn


def list_sessions(db: DBSession, *, config, data_classification: str,
                  cursor: str | None, limit: int,
                  binding: Any = None) -> dict[str, Any]:
    if _has_frozen_snapshot(binding):
        return _list_frozen_snapshot(
            db,
            dataset_key="sessions",
            config=config,
            data_classification=data_classification,
            cursor=cursor,
            limit=limit,
            binding=binding,
        )
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
    if binding is not None:
        statement = statement.where(Session.session_id.in_(binding.session_ids))
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
    return _envelope("sessions", rows, next_cursor, has_more, config, binding)


def list_turns(db: DBSession, *, config, data_classification: str,
               cursor: str | None, limit: int,
               binding: Any = None) -> dict[str, Any]:
    """按 (场次, 题目, 环节序号) 的 keyset 翻页。

    第一版是在 Python 里翻：把该分区的全部场次、每个场次的全部题目、每道题的
    全部环节都取出来，再用 ``key <= after`` 逐行跳过。于是第 k 页要为前
    (k-1)×limit 行重跑一遍全部 I/O——页数越深越慢，而这套服务和床旁训练同进程、
    同一台 1 GiB 的机器。判据推进 SQL，一页就是一次带 LIMIT 的查询。
    """
    if _has_frozen_snapshot(binding):
        return _list_frozen_snapshot(
            db,
            dataset_key="turns",
            config=config,
            data_classification=data_classification,
            cursor=cursor,
            limit=limit,
            binding=binding,
        )
    after = decode_cursor(cursor, config, "turns") if cursor else None
    statement = (
        select(TurnEvent, ItemEvent, Session)
        .join(ItemEvent, TurnEvent.item_event_id == ItemEvent.id)
        .join(Session, ItemEvent.session_id == Session.session_id)
        .where(Session.data_classification == data_classification)
        .order_by(ItemEvent.session_id, ItemEvent.item_id, TurnEvent.turn_seq)
    )
    if binding is not None:
        statement = statement.where(
            ItemEvent.session_id.in_(binding.session_ids))
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
    return _envelope("turns", rows, next_cursor, has_more, config, binding)


def _questionnaire_patient_ids(db: DBSession, binding: Any) -> set[str] | None:
    """冻结纪元下，量表面覆盖哪些受试者。

    纪元是一个**场次**集合，而量表记录挂在受试者上、不挂场次。取「这批场次
    对应的受试者」是唯一不引入新治理概念的口径。None = 不绑定（仿真分区）。
    """
    if binding is None:
        return None
    rows = db.exec(select(Session.patient_id).where(
        Session.session_id.in_(binding.session_ids))).all()
    return {row if isinstance(row, str) else row[0] for row in rows}


def list_questionnaire_records(db: DBSession, *, config, data_classification: str,
                               cursor: str | None, limit: int,
                               binding: Any = None) -> dict[str, Any]:
    """按 record_id 的 keyset 翻页。**只发已锁定的记录**——draft 还不是证据。"""
    if _has_frozen_snapshot(binding):
        return _list_frozen_snapshot(
            db, dataset_key="questionnaire_records", config=config,
            data_classification=data_classification, cursor=cursor,
            limit=limit, binding=binding)
    after = decode_cursor(cursor, config, "questionnaire_records") if cursor else None
    patient_ids = _questionnaire_patient_ids(db, binding)
    statement = (
        select(QuestionnaireRecord)
        .where(QuestionnaireRecord.status == "locked")
        .order_by(QuestionnaireRecord.record_id)
    )
    if patient_ids is not None:
        if not patient_ids:
            return _envelope("questionnaire_records", [], None, False, config, binding)
        statement = statement.where(
            QuestionnaireRecord.patient_id.in_(patient_ids))
    if after is not None:
        statement = statement.where(QuestionnaireRecord.record_id > after[0])
    picked = list(db.exec(statement.limit(limit + 1)))
    page, has_more = _page(picked, limit)

    withdrawn = _withdrawn_patient_ids(db)
    rows: list[dict[str, Any]] = []
    last_key: list[Any] | None = None
    for record in page:
        record_code = export_security.pseudonymize_questionnaire_record(
            record.record_id, config)
        subject_code = export_security.pseudonymize_subject(
            record.patient_id, config)
        if record.patient_id in withdrawn:
            rows.append(_tombstone("questionnaire_records", {
                "record_code": record_code,
                "subject_code": subject_code,
                "questionnaire_id": record.questionnaire_id,
                "phase_label": record.phase_label,
                "phase_ordinal": record.phase_ordinal,
                "withdrawn": True,
            }))
        else:
            rows.append({
                "record_code": record_code,
                "subject_code": subject_code,
                "questionnaire_id": record.questionnaire_id,
                "phase_label": record.phase_label,
                "phase_ordinal": record.phase_ordinal,
                "superseded_by_ordinal": record.superseded_by_ordinal,
                "definition_sha256": record.definition_sha256,
                "scoring_rule_id": record.scoring_rule_id,
                "computed_total": record.computed_total,
                "cutoff_met": record.cutoff_met,
                "computed_flag": record.computed_flag,
                "ai_draft_status": record.ai_draft_status,
                "ai_draft_engine": record.ai_draft_engine,
                "withdrawn": False,
            })
        last_key = [record.record_id]

    next_cursor = (encode_cursor(last_key, config, "questionnaire_records")
                   if has_more and last_key is not None else None)
    return _envelope("questionnaire_records", rows, next_cursor, has_more,
                     config, binding)


def list_questionnaire_item_values(db: DBSession, *, config,
                                   data_classification: str,
                                   cursor: str | None, limit: int,
                                   binding: Any = None) -> dict[str, Any]:
    """按 (record_id, item_key, field_key) 的 keyset 翻页；同样只跟已锁定的记录走。"""
    if _has_frozen_snapshot(binding):
        return _list_frozen_snapshot(
            db, dataset_key="questionnaire_item_values", config=config,
            data_classification=data_classification, cursor=cursor,
            limit=limit, binding=binding)
    after = (decode_cursor(cursor, config, "questionnaire_item_values")
             if cursor else None)
    patient_ids = _questionnaire_patient_ids(db, binding)
    statement = (
        select(QuestionnaireItemValue, QuestionnaireRecord)
        .join(QuestionnaireRecord,
              QuestionnaireItemValue.record_id == QuestionnaireRecord.record_id)
        .where(QuestionnaireRecord.status == "locked")
        .order_by(QuestionnaireItemValue.record_id,
                  QuestionnaireItemValue.item_key,
                  QuestionnaireItemValue.field_key)
    )
    if patient_ids is not None:
        if not patient_ids:
            return _envelope("questionnaire_item_values", [], None, False,
                             config, binding)
        statement = statement.where(
            QuestionnaireRecord.patient_id.in_(patient_ids))
    if after is not None:
        record_after, item_after, field_after = after
        statement = statement.where(or_(
            QuestionnaireItemValue.record_id > record_after,
            and_(QuestionnaireItemValue.record_id == record_after,
                 QuestionnaireItemValue.item_key > item_after),
            and_(QuestionnaireItemValue.record_id == record_after,
                 QuestionnaireItemValue.item_key == item_after,
                 QuestionnaireItemValue.field_key > field_after),
        ))
    picked = list(db.exec(statement.limit(limit + 1)))
    page, has_more = _page(picked, limit)

    withdrawn = _withdrawn_patient_ids(db)
    rows: list[dict[str, Any]] = []
    last_key: list[Any] | None = None
    for value, record in page:
        record_code = export_security.pseudonymize_questionnaire_record(
            value.record_id, config)
        subject_code = export_security.pseudonymize_subject(
            record.patient_id, config)
        if record.patient_id in withdrawn:
            rows.append(_tombstone("questionnaire_item_values", {
                "record_code": record_code,
                "subject_code": subject_code,
                "item_key": value.item_key,
                "field_key": value.field_key,
                "withdrawn": True,
            }))
        else:
            rows.append({
                "record_code": record_code,
                "subject_code": subject_code,
                "item_key": value.item_key,
                "field_key": value.field_key,
                "final_value": value.final_value,
                "value_source": value.value_source,
                "ai_draft_value": value.ai_draft_value,
                "withdrawn": False,
            })
        last_key = [value.record_id, value.item_key, value.field_key]

    next_cursor = (encode_cursor(last_key, config, "questionnaire_item_values")
                   if has_more and last_key is not None else None)
    return _envelope("questionnaire_item_values", rows, next_cursor, has_more,
                     config, binding)


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
        "duration_seconds": turn.duration_seconds,
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


def _has_frozen_snapshot(binding: Any) -> bool:
    """只要 binding 声明了快照，就绝不允许回退到活表。

    一个属性缺失属于绑定层的损坏；读取层仍然 fail-closed，避免并行升级期
    出现“manifest 有了、sha 还没有，先查活表”的短暂漏口。
    """
    if binding is None:
        return False
    manifest = getattr(binding, "snapshot_manifest", None)
    snapshot_sha256 = getattr(binding, "research_snapshot_sha256", None)
    if (manifest is None) != (snapshot_sha256 is None):
        raise ResearchReadUnavailable(
            "research_release_snapshot_corrupt",
            "冻结研究行快照不完整，行级取数面已关闭",
        )
    return manifest is not None


def _snapshot_dataset_manifest(binding: Any, dataset_key: str) -> dict[str, Any]:
    manifest = binding.snapshot_manifest
    datasets = manifest.get("datasets") if isinstance(manifest, dict) else None
    entry = datasets.get(dataset_key) if isinstance(datasets, dict) else None
    dataset = research_dataset.dataset_for(dataset_key)
    expected_columns = (
        list(research_dataset.published_columns(dataset))
        if dataset is not None else None
    )
    if not isinstance(entry, dict):
        raise ResearchReadUnavailable(
            "research_release_snapshot_corrupt",
            "冻结研究行快照与清单对不上，行级取数面已关闭",
        )
    row_count = entry.get("row_count")
    if (type(row_count) is not int or row_count < 0
            or entry.get("columns") != expected_columns):
        raise ResearchReadUnavailable(
            "research_release_snapshot_corrupt",
            "冻结研究行快照与清单对不上，行级取数面已关闭",
        )
    return entry


def _decode_snapshot_row(
    snapshot: QualityReleaseEpochRowSnapshot, *, dataset_key: str,
) -> dict[str, Any]:
    raw = snapshot.row_json.encode("utf-8")
    if not hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(), snapshot.row_sha256):
        raise ResearchReadUnavailable(
            "research_release_snapshot_corrupt",
            "冻结研究行快照与行指纹对不上，行级取数面已关闭",
        )
    try:
        row = json.loads(snapshot.row_json)
    except (TypeError, ValueError) as exc:
        raise ResearchReadUnavailable(
            "research_release_snapshot_corrupt",
            "冻结研究行快照不可解析，行级取数面已关闭",
        ) from exc
    if not isinstance(row, dict):
        raise ResearchReadUnavailable(
            "research_release_snapshot_corrupt",
            "冻结研究行快照不是数据行，行级取数面已关闭",
        )
    # 快照写入时约定的就是这串规范字节；重新规范化后不等即表示
    # 库内行被修改过，不能解析后“看起来还一样”就继续发。
    canonical = json.dumps(
        row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    dataset = research_dataset.dataset_for(dataset_key)
    assert dataset is not None
    if (canonical != snapshot.row_json
            or set(row) != set(research_dataset.published_columns(dataset))):
        raise ResearchReadUnavailable(
            "research_release_snapshot_corrupt",
            "冻结研究行快照与发布列契约对不上，行级取数面已关闭",
        )
    return row


def _list_frozen_snapshot(
    db: DBSession,
    *,
    dataset_key: str,
    config: export_security.DeidentificationConfig,
    data_classification: str,
    cursor: str | None,
    limit: int,
    binding: Any,
) -> dict[str, Any]:
    """从纪元的冻结行表复读一页，整条路径不触碰临床活表。"""
    if data_classification != "research":
        # 正常 HTTP 处理器只会给 research 分区传 binding。这道内层闸
        # 防止以后的调用方把冻结真人行误标成 simulation 发出。
        raise ResearchReadUnavailable(
            "research_release_snapshot_partition_invalid",
            "冻结研究行快照不属于请求的数据分区",
        )
    manifest = _snapshot_dataset_manifest(binding, dataset_key)
    total = manifest["row_count"]
    after = (
        decode_snapshot_cursor(
            cursor,
            config,
            binding=binding,
            dataset=dataset_key,
            data_classification=data_classification,
        )
        if cursor else 0
    )
    if after > total:
        raise ResearchReadUnavailable(
            "research_release_snapshot_corrupt",
            "冻结研究行快照页码超出清单，行级取数面已关闭",
        )

    fetched = list(db.exec(
        select(QualityReleaseEpochRowSnapshot)
        .where(
            QualityReleaseEpochRowSnapshot.epoch_id == binding.epoch_id,
            QualityReleaseEpochRowSnapshot.dataset_key == dataset_key,
            QualityReleaseEpochRowSnapshot.row_ordinal > after,
        )
        .order_by(QualityReleaseEpochRowSnapshot.row_ordinal)
        .limit(limit + 1)
    ))
    expected_fetched = min(limit + 1, total - after)
    if len(fetched) != expected_fetched:
        raise ResearchReadUnavailable(
            "research_release_snapshot_corrupt",
            "冻结研究行快照行数与清单对不上，行级取数面已关闭",
        )
    expected_ordinals = list(range(after + 1, after + 1 + len(fetched)))
    if [row.row_ordinal for row in fetched] != expected_ordinals:
        raise ResearchReadUnavailable(
            "research_release_snapshot_corrupt",
            "冻结研究行快照序号不连续，行级取数面已关闭",
        )

    page = fetched[:limit]
    rows = [
        _decode_snapshot_row(row, dataset_key=dataset_key)
        for row in page
    ]
    has_more = after + len(page) < total
    next_cursor = (
        encode_snapshot_cursor(
            page[-1].row_ordinal,
            config,
            binding=binding,
            dataset=dataset_key,
            data_classification=data_classification,
        )
        if page and has_more else None
    )
    return _envelope(
        dataset_key, rows, next_cursor, has_more, config, binding)


#: dataset_key → 取数函数**的名字**。**唯一的一份**。
#: 2026-08-27 之前这张表在三个地方各写了一遍（HTTP 取数、冻结纪元、测试夹具），
#: 加两个数据集就得改三处，漏一处的表现是 KeyError 或纪元少冻两张表。
#: 存名字不存函数对象：函数对象在导入那一刻就被抓死，测试 monkeypatch 模块属性
#: 换不掉它，于是打了替身却仍然走真实 DB。
READERS: dict[str, str] = {
    "subjects": "list_subjects",
    "sessions": "list_sessions",
    "turns": "list_turns",
    "questionnaire_records": "list_questionnaire_records",
    "questionnaire_item_values": "list_questionnaire_item_values",
}


def reader_for(dataset_key: str):
    name = READERS.get(dataset_key)
    if name is None:
        raise KeyError(f"数据集 {dataset_key} 没有登记取数函数")
    import sys
    return getattr(sys.modules[__name__], name)


def _envelope(dataset_key: str, rows: Iterable[dict[str, Any]],
              next_cursor: str | None, has_more: bool,
              config, binding: Any = None) -> dict[str, Any]:
    dataset = research_dataset.dataset_for(dataset_key)
    assert dataset is not None
    projected = research_dataset.project_all(dataset, rows)
    # 序列化前的最后一道闸：与去标识导出包共用同一份断言。
    export_security.assert_deidentified_sheets({dataset_key: projected})
    return {
        "schema_version": research_dataset.SCHEMA_VERSION,
        "dataset": dataset_key,
        "grain": dataset.grain,
        # 研究分区必有；仿真分区恒 null——仿真数据不冻纪元，也不该看起来像冻过。
        "release": binding.envelope() if binding is not None else None,
        "pseudonym_version": export_security.PSEUDONYM_VERSION,
        "pseudonym_key_id": config.key_id,
        "columns": list(research_dataset.published_columns(dataset)),
        "rows": projected,
        "row_count": len(projected),
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


def build_meta(config_error: ResearchReadUnavailable | None,
               config, release_state: dict[str, Any] | None = None) -> dict[str, Any]:
    """密钥缺失时**仍然可读**——否则取数的人只拿到一个裸 503，无法自诊。"""
    if config_error is not None:
        return {
            "schema_version": research_dataset.SCHEMA_VERSION,
            "deidentification": {"configured": False,
                                 "reason": config_error.message},
            "research_release": {"bound": False,
                                 "code": config_error.code,
                                 "reason": config_error.message},
            "datasets": [],
            "note": "配置去标识密钥后本接口才会返回数据集；此前一行也不会返回。",
        }
    return {
        "schema_version": research_dataset.SCHEMA_VERSION,
        "deidentification": {"configured": True,
                             "pseudonym_version": export_security.PSEUDONYM_VERSION,
                             "pseudonym_key_id": config.key_id},
        # 研究分区的行面绑在冻结纪元上。没绑上时这里说明是哪一道闸拦的——否则
        # 取数的人只看到 503，分不清"还没切纪元"和"密钥换过了"。
        "research_release": release_state or {},
        "datasets": [
            {"key": dataset.key, "title": dataset.title, "grain": dataset.grain,
             "columns": list(research_dataset.published_columns(dataset))}
            for dataset in research_dataset.DATASETS
        ],
        "page": {"default_limit": DEFAULT_PAGE_SIZE, "max_limit": MAX_PAGE_SIZE,
                 "style": "keyset"},
        "note": ("轮换去标识密钥会让所有假名改变，已交付的 CSV 无法与新拉取 join；"
                 "pseudonym_key_id 是唯一的检测手段。研究分区只发冻结纪元里的"
                 "那批场次，纪元号写在每一页的 release 里，也写在 CSV 文件名上。"),
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


assert set(READERS) == set(research_dataset.dataset_keys()), (
    "登记的数据集与取数函数对不上："
    + str(set(READERS) ^ set(research_dataset.dataset_keys())))
for _name in READERS.values():
    assert _name in globals(), f"READERS 指向了不存在的函数 {_name}"
