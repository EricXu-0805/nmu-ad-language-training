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
from uuid import uuid4

from sqlmodel import Session as DBSession
from sqlmodel import select

from . import audio_gate, audio_store, scoring
from .enums import AudioStatus
from .models import (
    AbnormalEvent, AudioAssetRow, ItemEvent, Patient, ScaleResult,
    Session as TrainSession, TurnEvent,
)
from .runtime import DOUBLE_ROLE_TO_FIELD

EXPORT_DIR = Path(__file__).resolve().parent.parent / "data" / "exports"
CONTROLLED_AUDIO_DIR = Path(__file__).resolve().parent.parent / "data" / "controlled-audio-exports"
_REDACTION = "〔含直接标识·已去标识〕"
_DIGIT_RUN = re.compile(r"\d{2,}")               # 掩掉 2 位以上连续数字（年龄/电话/证件片段）
_SAFE_BATCH_ID = re.compile(r"^[A-Za-z0-9._-]+$")

# 默认去标识导出中【绝对禁止出现】的直接标识列名——用于导出后自检。
DIRECT_IDENTIFIER_COLUMNS = frozenset({
    "patient_id", "consent_person", "trainer_id", "reviewer_id", "assessor_id",
})


def pseudonymize(patient_id: str) -> str:
    """确定式假名（跨导出稳定，支持前后测纵向连接）。反查须经受控 crosswalk。

    口径注：M0 用 sha256 前缀；正式部署应换 HMAC+每部署盐 + 受控对照表留存，待数据治理 SOP。
    """
    return "SUBJ-" + hashlib.sha256(patient_id.encode("utf-8")).hexdigest()[:8]


def controlled_audio_root(write_dir: Optional[Path] = None) -> Path:
    """受控音频导出根目录。

    生产环境与去标识分析包 ``data/exports`` 物理分开；测试传入
    ``write_dir`` 时仍放在分析批次目录之外，防止调用方把声纹误当去标识附件。
    """
    return CONTROLLED_AUDIO_DIR if write_dir is None else write_dir / "_controlled_audio"


def find_exported_audio_blob(raw_audio_id: str, batch_id: str,
                             *, root: Optional[Path] = None) -> Path | None:
    """查找已完成批次中的受控音频副本；checksum 闸门只允许校验它。"""
    if (not audio_store.SAFE_ID.match(raw_audio_id)
            or not _SAFE_BATCH_ID.match(batch_id)):
        return None
    base = (root or CONTROLLED_AUDIO_DIR) / batch_id / "audio"
    if not base.exists():
        return None
    return next(base.glob(f"{raw_audio_id}.*"), None)


def mask_text(text: Optional[str], contains_direct_identifier: bool) -> Optional[str]:
    """转写去标识：含直接标识符整段红线；其余文本对连续数字做保守掩码。"""
    if text is None:
        return None
    if contains_direct_identifier:
        return _REDACTION
    return _DIGIT_RUN.sub("##", text)


# ============ 评分重建（从已锁定分环节值）============
def _locked(turn: TurnEvent) -> bool:
    # 兼容旧库时也要重验新门禁：历史行即使 score_locked=True，缺人工确认或
    # 提示等级仍不能进入正式研究统计。
    return (bool(turn.score_locked)
            and turn.element_value is not None
            and turn.confirmed_response_text is not None)


def _reconstruct_scores(items: list[ItemEvent], turns_by_item: dict[int, list[TurnEvent]]) -> dict:
    """按 task_type 分桶，用【全部环节已锁定】的题重建综合指标。返回聚合 + 排除清单。"""
    singles: list[scoring.SingleElementItem] = []
    doubles: list[scoring.DoubleElementItem] = []
    multis: list[scoring.MultiElementItem] = []
    excluded: list[str] = []

    for it in items:
        turns = turns_by_item.get(it.id, [])
        if not turns or not all(_locked(t) for t in turns):
            excluded.append(
                f"{it.item_id}（{it.task_type}）：有未确认或未锁定环节，暂不计入正式评分"
            )
            continue
        if any(t.prompt_level is None for t in turns):
            excluded.append(f"{it.item_id}（{it.task_type}）：prompt_level 缺失，暂不计入正式评分")
            continue
        tt = str(it.task_type.value if hasattr(it.task_type, "value") else it.task_type)
        if tt == "单要素":
            t = turns[0]
            fc = int(t.element_value)
            if t.prompt_level is None:
                excluded.append(f"{it.item_id}（单要素）：prompt_level 缺失，不得按自发正确统计")
                continue
            pl = int(t.prompt_level)
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
    batch_id = batch_id or ("EXP-" + now.strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:12])
    if not _SAFE_BATCH_ID.match(batch_id):
        raise ValueError("导出批次号含非法字符")
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

    # --- 音频清单 + 导出候选 ---
    # 去标识分析包只放元数据清单，原始声纹始终另存受控目录。
    # 只有在源字节存在、已登记 checksum 且受控副本成功后，才会推进 recorded→exported。
    audio_sheet, touched = [], []
    export_candidates: list[tuple[AudioAssetRow, Path]] = []
    for a in audios:
        source_blob = audio_store.find_blob(a.raw_audio_id)
        will_export = a.status == AudioStatus.recorded and source_blob is not None
        if will_export:
            if not a.checksum:
                raise ValueError(f"音频 {a.raw_audio_id} 缺采集期 checksum，拒绝标记导出")
            export_candidates.append((a, source_blob))
        audio_sheet.append({"raw_audio_id": a.raw_audio_id, "session_id": session_id,
                            "turn_key": a.turn_key,
                            "audio_format": a.audio_format,
                            "status": _v(AudioStatus.exported if will_export else a.status),
                            "is_reliability_sample": a.is_reliability_sample,
                            "contains_direct_identifier": a.contains_direct_identifier,
                            "export_batch_id": (batch_id if will_export else a.export_batch_id),
                            "controlled_audio_exported": will_export})

    sheets = {"session": session_sheet, "turns": turn_sheet, "item_scores": score_sheet,
              "scales": scale_sheet, "abnormal": abn_sheet, "audio_manifest": audio_sheet}
    if not deidentify:
        sheets["crosswalk"] = [{"subject_code": subj, "patient_id": sess.patient_id}]

    if deidentify:
        _assert_no_direct_identifiers(sheets)

    # 顺序是数据保护边界：先完整写分析 CSV，再复制并校验受控音频副本，
    # 最后才在 DB 提交 exported。任一 IO 步失败都不得推进删除闸门。
    csv_base = (write_dir or EXPORT_DIR) / batch_id
    controlled_base = controlled_audio_root(write_dir) / batch_id / "audio"
    audio_files: list[str] = []
    try:
        files = _write_csvs(sheets, batch_id, write_dir)
        for a, source in export_candidates:
            dst = controlled_base / source.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dst)
            copied_digest = audio_store.sha256_hex(dst.read_bytes())
            if copied_digest != a.checksum:
                raise RuntimeError(f"音频 {a.raw_audio_id} 导出副本 checksum 不一致，拒绝推进状态")
            audio_files.append(source.name)
    except Exception:
        # 中途失败不能留下"看似完整"的批次目录:manifest 里已预标 exported、受控声纹
        # 只有半套——分析侧会当真、backup.sh 会把孤儿副本永久收编。两个目录都以本次
        # batch_id 独占命名,整目录移除;DB 状态未提交,音频删除闸门不受任何影响。
        shutil.rmtree(csv_base, ignore_errors=True)
        shutil.rmtree(controlled_base.parent, ignore_errors=True)
        raise

    for a, _source in export_candidates:
        asset = audio_gate.AudioAsset(a.raw_audio_id, a.status,
                                      a.is_reliability_sample, a.withdrawn)
        audio_gate.mark_exported(asset)
        a.status = asset.status
        a.export_batch_id = batch_id
        a.exported_at = now
        db.add(a)
        touched.append(a.raw_audio_id)
    if touched:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {"batch_id": batch_id, "deidentified": deidentify, "sheets": sheets,
            "files": files, "audio_touched": touched, "audio_files": audio_files,
            "controlled_audio_dir": str(controlled_base.parent) if audio_files else None,
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
