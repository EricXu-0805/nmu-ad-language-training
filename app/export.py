"""去标识化导出通道 + 过程性评分重建（M6）。

三条硬约束在此落地：
  1. ★画像不进导出：导出各表【不 join、不携带】任何画像字段（profile_week1 独立、此处不读）。
  2. 去标识化默认开：对外分析导出用假名替换 patient 直接标识；含直接标识符的转写做掩码。
     直接标识↔假名的对照表(crosswalk)仅在受控内部导出(deidentify=False)产出，绝不进默认包。
  3. 评分单一事实源：de_total / 关键要素率【不落库】，导出时由 scoring 纯函数从
     **已锁定**的分环节原始值重建；未 score_locked 的环节不进正式评分统计。

导出成功 → 回写 audio_status（recorded→exported）+ 打 export_batch_id，触发删除闸门第一关；
**绝不在此删除音频**（删除须另走四闸门全绿，见 audio_gate.py）。
"""
from __future__ import annotations

import csv
import hashlib
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlmodel import Session as DBSession
from sqlmodel import select

from . import audio_gate, audio_store, scoring
from .models import (
    AbnormalEvent, AudioAssetRow, ItemEvent, Patient, ScaleResult,
    Session as TrainSession, TurnEvent,
)
from .runtime import DOUBLE_ROLE_TO_FIELD

EXPORT_DIR = Path(__file__).resolve().parent.parent / "data" / "exports"
_REDACTION = "〔含直接标识·已去标识〕"
_DIGIT_RUN = re.compile(r"\d{2,}")               # 掩掉 2 位以上连续数字（年龄/电话/证件片段）

# 默认去标识导出中【绝对禁止出现】的直接标识列名——用于导出后自检。
DIRECT_IDENTIFIER_COLUMNS = frozenset({
    "patient_id", "consent_person", "trainer_id", "reviewer_id", "assessor_id",
})


def pseudonymize(patient_id: str) -> str:
    """确定式假名（跨导出稳定，支持前后测纵向连接）。反查须经受控 crosswalk。

    口径注：M0 用 sha256 前缀；正式部署应换 HMAC+每部署盐 + 受控对照表留存，待数据治理 SOP。
    """
    return "SUBJ-" + hashlib.sha256(patient_id.encode("utf-8")).hexdigest()[:8]


def mask_text(text: Optional[str], contains_direct_identifier: bool) -> Optional[str]:
    """转写去标识：含直接标识符整段红线；其余文本对连续数字做保守掩码。"""
    if text is None:
        return None
    if contains_direct_identifier:
        return _REDACTION
    return _DIGIT_RUN.sub("##", text)


# ============ 评分重建（从已锁定分环节值）============
def _locked(turn: TurnEvent) -> bool:
    return bool(turn.score_locked) and turn.element_value is not None


def _reconstruct_scores(items: list[ItemEvent], turns_by_item: dict[int, list[TurnEvent]]) -> dict:
    """按 task_type 分桶，用【全部环节已锁定】的题重建综合指标。返回聚合 + 排除清单。"""
    singles: list[scoring.SingleElementItem] = []
    doubles: list[scoring.DoubleElementItem] = []
    multis: list[scoring.MultiElementItem] = []
    excluded: list[str] = []

    for it in items:
        turns = turns_by_item.get(it.id, [])
        if not turns or not all(_locked(t) for t in turns):
            excluded.append(f"{it.item_id}（{it.task_type}）：有未锁定环节，暂不计入正式评分")
            continue
        tt = str(it.task_type.value if hasattr(it.task_type, "value") else it.task_type)
        if tt == "单要素":
            t = turns[0]
            fc = int(t.element_value)
            pl = int(t.prompt_level or 0)
            singles.append(scoring.SingleElementItem(
                item_id=it.item_id, final_correct=fc,
                spontaneous_correct=1 if (fc == 1 and pl == 0) else 0,
                prompt_level=pl, duration_seconds=t.duration_seconds))
        elif tt == "双要素":
            vals = {DOUBLE_ROLE_TO_FIELD.get(t.response_role): t.element_value
                    for t in turns if t.response_role in DOUBLE_ROLE_TO_FIELD}
            if set(vals) != set(DOUBLE_ROLE_TO_FIELD.values()):
                excluded.append(f"{it.item_id}（双要素）：环节不齐（缺 {set(DOUBLE_ROLE_TO_FIELD.values())-set(vals)}）")
                continue
            doubles.append(scoring.DoubleElementItem(
                item_id=it.item_id,
                left_name=int(vals["left_name"]), left_function=int(vals["left_function"]),
                right_name=int(vals["right_name"]), right_function=int(vals["right_function"]),
                relation=float(vals["relation"])))
        elif tt == "多要素":
            key_elements = {t.response_role: int(t.element_value) for t in turns}
            multis.append(scoring.MultiElementItem(item_id=it.item_id, key_elements=key_elements))

    out: dict = {"excluded_items": excluded}
    out["single"] = scoring.score_single_element(singles) if singles else None
    out["double"] = scoring.score_double_element(doubles) if doubles else None
    out["multi"] = scoring.score_multi_element(multis) if multis else None
    return out


# ============ 导出 ============
def export_session_bundle(db: DBSession, session_id: str, *, deidentify: bool = True,
                          batch_id: Optional[str] = None, now: Optional[datetime] = None,
                          write_dir: Optional[Path] = None) -> dict:
    """导出一场次的多维度数据集（默认去标识）。返回 {sheets, batch_id, files, audio_touched}。

    deidentify=True（默认对外通道）：患者以假名出现、直接标识列一律不产、含标识转写红线；
    deidentify=False（受控内部）：保留 patient_id 与 crosswalk，供内网复核，**不得对外**。
    """
    sess = db.get(TrainSession, session_id)
    if not sess:
        raise ValueError(f"场次 {session_id} 不存在")
    patient = db.get(Patient, sess.patient_id)
    now = now or datetime.now()
    batch_id = batch_id or ("EXP-" + now.strftime("%Y%m%d-%H%M%S"))
    subj = pseudonymize(sess.patient_id)

    items = list(db.exec(select(ItemEvent).where(ItemEvent.session_id == session_id)))
    turns_by_item: dict[int, list[TurnEvent]] = {}
    for it in items:
        rows = list(db.exec(select(TurnEvent).where(TurnEvent.item_event_id == it.id)
                            .order_by(TurnEvent.turn_seq)))
        turns_by_item[it.id] = rows

    # 音频含标识映射，供转写掩码
    audios = list(db.exec(select(AudioAssetRow).where(AudioAssetRow.session_id == session_id)))
    direct_flag = {a.raw_audio_id: a.contains_direct_identifier for a in audios}

    def subject_cols() -> dict:
        # 默认通道只放假名；受控内部才附直接标识
        return {"patient_id": sess.patient_id, "subject_code": subj} if not deidentify \
            else {"subject_code": subj}

    # --- session 表 ---
    session_sheet = [{**subject_cols(),
                      "session_id": sess.session_id, "week_no": sess.week_no,
                      "phase_type": _v(sess.phase_type), "event_line": _v(sess.event_line),
                      "session_sitting_no": sess.session_sitting_no,
                      "item_bank_version_id": sess.item_bank_version_id,
                      "dementia_severity": getattr(patient, "dementia_severity", None),
                      "mandarin_eligible": getattr(patient, "mandarin_eligible", None)}]

    # --- turns 明细（去标识文本）---
    turn_sheet = []
    for it in items:
        for t in turns_by_item.get(it.id, []):
            cdi = direct_flag.get(t.raw_audio_id, False) if t.raw_audio_id else False
            asr = mask_text(t.asr_text, cdi) if deidentify else t.asr_text
            conf = mask_text(t.confirmed_response_text, cdi) if deidentify else t.confirmed_response_text
            turn_sheet.append({
                **subject_cols(), "session_id": session_id, "item_id": it.item_id,
                "task_type": _v(it.task_type), "turn_seq": t.turn_seq,
                "response_role": t.response_role,
                "asr_text": asr, "confirmed_response_text": conf,
                "asr_confidence": t.asr_confidence, "prompt_level": t.prompt_level,
                "ai_answer_type": t.ai_answer_type, "ai_score": t.ai_score,
                "ai_needs_review": t.ai_needs_review,
                "reviewed_score": t.reviewed_score, "score_locked": t.score_locked,
                "element_value": t.element_value,
                "ai_human_diff": (None if t.reviewed_score is None or t.ai_score is None
                                  else round(t.reviewed_score - t.ai_score, 4)),
                "judge_portrait_used": t.judge_portrait_used})

    # --- 重建评分汇总 ---
    scores = _reconstruct_scores(items, turns_by_item)
    score_sheet = [{**subject_cols(), "session_id": session_id, "task_type": tt,
                    "summary": _flat(scores[key])}
                   for tt, key in (("单要素", "single"), ("双要素", "double"), ("多要素", "multi"))
                   if scores[key]]

    # --- 量表结果 ---（assessor_id 属直接标识，去标识通道不产该列）
    scale_rows = list(db.exec(select(ScaleResult).where(ScaleResult.patient_id == sess.patient_id)))
    scale_sheet = [{**subject_cols(), "phase_type": _v(s.phase_type), "scale_name": s.scale_name,
                    "subscale": s.subscale, "score": s.score,
                    **({} if deidentify else {"assessor_id": s.assessor_id})} for s in scale_rows]

    # --- 异常/介入 ---
    abn_rows = list(db.exec(select(AbnormalEvent).where(AbnormalEvent.session_id == session_id)))
    abn_sheet = [{**subject_cols(), "session_id": session_id, "phase_type": _v(a.phase_type),
                  "abnormal_type": a.abnormal_type, "intervention_type": a.intervention_type,
                  "affects_scoring_validity": a.affects_scoring_validity,
                  "note": (mask_text(a.note, True) if deidentify and a.note else a.note)}
                 for a in abn_rows]

    # --- 音频清单（mp3 打包引用）+ 触发导出闸门 ---
    audio_sheet, touched = [], []
    for a in audios:
        if a.status == a.status.recorded:      # recorded→exported，绝不在此删
            try:
                asset = audio_gate.AudioAsset(a.raw_audio_id, a.status,
                                              a.is_reliability_sample, a.withdrawn)
                audio_gate.mark_exported(asset)
                a.status = asset.status
                a.export_batch_id = batch_id
                a.exported_at = now
                db.add(a); touched.append(a.raw_audio_id)
            except audio_gate.AudioGateError:
                pass
        audio_sheet.append({"raw_audio_id": a.raw_audio_id, "session_id": session_id,
                            "audio_format": a.audio_format, "status": _v(a.status),
                            "is_reliability_sample": a.is_reliability_sample,
                            "contains_direct_identifier": a.contains_direct_identifier,
                            "export_batch_id": a.export_batch_id})
    if touched:
        db.commit()

    # --- 音频字节真打包(有存储字节才拷,进导出包 audio/ 子目录;无字节仅出清单行)---
    base = (write_dir or EXPORT_DIR) / batch_id
    audio_files: list[str] = []
    for a in audios:
        p = audio_store.find_blob(a.raw_audio_id)
        if p:
            dst = base / "audio" / p.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            audio_files.append(p.name)

    sheets = {"session": session_sheet, "turns": turn_sheet, "item_scores": score_sheet,
              "scales": scale_sheet, "abnormal": abn_sheet, "audio_manifest": audio_sheet}
    if not deidentify:
        sheets["crosswalk"] = [{"subject_code": subj, "patient_id": sess.patient_id}]

    if deidentify:
        _assert_no_direct_identifiers(sheets)

    files = _write_csvs(sheets, batch_id, write_dir)
    return {"batch_id": batch_id, "deidentified": deidentify, "sheets": sheets,
            "files": files, "audio_touched": touched, "audio_files": audio_files,
            "excluded_items": scores["excluded_items"]}


def _assert_no_direct_identifiers(sheets: dict) -> None:
    """默认去标识包的最后一道自检：任一表出现直接标识列即抛错（防回归）。"""
    for name, rows in sheets.items():
        for r in rows:
            leaked = DIRECT_IDENTIFIER_COLUMNS & set(r)
            if leaked:
                raise RuntimeError(f"去标识导出表 {name} 混入直接标识列 {sorted(leaked)}，拒绝导出")


def _write_csvs(sheets: dict, batch_id: str, write_dir: Optional[Path]) -> list[str]:
    base = (write_dir or EXPORT_DIR) / batch_id
    base.mkdir(parents=True, exist_ok=True)
    files = []
    for name, rows in sheets.items():
        p = base / f"{name}.csv"
        cols = sorted({k for r in rows for k in r}) if rows else []
        with p.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        files.append(str(p))
    return files


def _v(x):
    return x.value if hasattr(x, "value") else x


def _flat(d: Optional[dict]) -> Optional[str]:
    if not d:
        return None
    keep = {k: v for k, v in d.items() if not isinstance(v, (list, dict))}
    return "; ".join(f"{k}={v}" for k, v in keep.items())
