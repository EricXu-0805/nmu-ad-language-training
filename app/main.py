"""FastAPI 应用骨架——把阶段0 地基接成可运行服务。

已接：健康检查 / 建档(含合规字段) / 建场次 / 题库加载+校验 / 单双多要素评分 /
判分入口(★画像守卫在 API 边界拒绝画像键) / 音频删除闸门(DELETE 未达条件返回 409)。
未接（下一步）：ASR/LLM(本地、可关闭)、导出通道、前端。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlmodel import Session as DBSession

import os
from datetime import datetime

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse

from . import asr, audio_gate, audio_store, content, export, judging, llm_judge, rule_judge, runtime, scoring
from .db import engine, get_session, init_db
from .enums import AudioStatus
from .models import AbnormalEvent, AudioAssetRow, ItemEvent, LiveState, Patient, ScaleResult, TurnEvent
from .models import Session as TrainSession


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(engine)
    yield


app = FastAPI(title="南医大 · AI 语言沟通训练系统", version="0.0.1", lifespan=lifespan)


# ---------------- 操作端 PIN 门(内网双设备模式)----------------
# 设 CONSOLE_PIN 环境变量即启用:一切写操作(POST/PUT/PATCH/DELETE)须带 X-Console-Pin。
# 单机模式不设即零打扰。读操作(GET)不拦——老人端轮询 /live/state 无需先输 PIN。
# 两端都由研究者开机配置(平板也输一次,存 localStorage),故统一拦全部写口、不留白名单。
@app.middleware("http")
async def console_pin_guard(request: Request, call_next):
    pin = os.environ.get("CONSOLE_PIN")
    if pin and request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if request.headers.get("x-console-pin") != pin:
            return JSONResponse(status_code=401,
                                content={"detail": "需要操作端 PIN(请求头 X-Console-Pin)"})
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok", "service": "language-training-platform"}


# ---------------- 患者 / 场次 ----------------
@app.post("/patients", response_model=Patient)
def create_patient(p: Patient, s: DBSession = Depends(get_session)):
    if s.get(Patient, p.patient_id):
        raise HTTPException(409, f"patient_id {p.patient_id} 已存在")
    s.add(p); s.commit(); s.refresh(p)
    return p


@app.get("/patients/{patient_id}", response_model=Patient)
def get_patient(patient_id: str, s: DBSession = Depends(get_session)):
    p = s.get(Patient, patient_id)
    if not p:
        raise HTTPException(404, "患者不存在")
    return p


@app.post("/sessions", response_model=TrainSession)
def create_session(sess: TrainSession, s: DBSession = Depends(get_session)):
    if not s.get(Patient, sess.patient_id):
        raise HTTPException(404, "患者不存在，先建档")
    if not sess.item_bank_version_id:
        raise HTTPException(422, "场次须绑题库版本号 item_bank_version_id")
    s.add(sess); s.commit(); s.refresh(sess)
    return sess


# ---------------- 内容 ----------------
@app.get("/content/item-bank")
def get_item_bank():
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    v = content.validate_item_bank(bank)
    return {
        "version_id": bank.version_id,
        "single_count": len(bank.single_element),
        "double_count": len(bank.double_element),
        "errata_fixed": bank.errata_fixed,
        "errors": v["errors"], "warnings": v["warnings"],
    }


# ---------------- 评分（接纯函数）----------------
class SingleItemIn(BaseModel):
    item_id: str
    final_correct: int
    spontaneous_correct: int
    prompt_level: int
    duration_seconds: float | None = None


class DoubleItemIn(BaseModel):
    item_id: str
    left_name: int
    left_function: int
    right_name: int
    right_function: int
    relation: float


class MultiItemIn(BaseModel):
    item_id: str
    key_elements: dict


@app.post("/score/single")
def score_single(items: list[SingleItemIn]):
    try:
        return scoring.score_single_element(
            [scoring.SingleElementItem(**i.model_dump()) for i in items])
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/score/double")
def score_double(items: list[DoubleItemIn]):
    try:
        return scoring.score_double_element(
            [scoring.DoubleElementItem(**i.model_dump()) for i in items])
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/score/multi")
def score_multi(items: list[MultiItemIn]):
    try:
        return scoring.score_multi_element(
            [scoring.MultiElementItem(**i.model_dump()) for i in items])
    except ValueError as e:
        raise HTTPException(422, str(e))


# ---------------- 判分入口：★画像守卫在边界 ----------------
@app.post("/judge/build-input")
def judge_build_input(payload: dict):
    """构造判分输入。若载荷混入任何画像字段 → 400（画像不进判分的边界防线）。"""
    try:
        ji = judging.build_judge_input(**payload)
    except judging.PortraitLeakError as e:
        raise HTTPException(400, str(e))
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e))
    return {"resolved_text": judging.resolve_response_text(ji),
            "judge_portrait_used": ji.judge_portrait_used}


# ---------------- 音频删除闸门 ----------------
def _row_to_asset(r: AudioAssetRow) -> audio_gate.AudioAsset:
    return audio_gate.AudioAsset(raw_audio_id=r.raw_audio_id, status=r.status,
                                 is_reliability_sample=r.is_reliability_sample, withdrawn=r.withdrawn)


def _load_row(raw_audio_id: str, s: DBSession) -> AudioAssetRow:
    r = s.get(AudioAssetRow, raw_audio_id)
    if not r:
        raise HTTPException(404, "音频不存在")
    return r


class AudioIn(BaseModel):
    raw_audio_id: str
    session_id: str | None = None
    is_reliability_sample: bool = False
    contains_direct_identifier: bool = False


@app.post("/audio", response_model=AudioAssetRow)
def create_audio(a: AudioIn, s: DBSession = Depends(get_session)):
    if s.get(AudioAssetRow, a.raw_audio_id):
        raise HTTPException(409, "音频已存在")
    r = AudioAssetRow(**a.model_dump())
    s.add(r); s.commit(); s.refresh(r)
    return r


def _transition(raw_audio_id: str, s: DBSession, fn) -> AudioAssetRow:
    r = _load_row(raw_audio_id, s)
    asset = _row_to_asset(r)
    try:
        fn(asset)
    except audio_gate.AudioGateError as e:
        raise HTTPException(409, str(e))
    r.status = asset.status
    s.add(r); s.commit(); s.refresh(r)
    return r


@app.post("/audio/{raw_audio_id}/export", response_model=AudioAssetRow)
def audio_export(raw_audio_id: str, s: DBSession = Depends(get_session)):
    return _transition(raw_audio_id, s, audio_gate.mark_exported)


@app.post("/audio/{raw_audio_id}/checksum", response_model=AudioAssetRow)
def audio_checksum(raw_audio_id: str, s: DBSession = Depends(get_session)):
    """导出期校验:重算存储字节 sha256 与登记 checksum 比对,一致才推进状态(拒绝盲翻)。"""
    r = _load_row(raw_audio_id, s)
    p = audio_store.find_blob(raw_audio_id)
    if not p or not r.checksum:
        raise HTTPException(409, "无音频字节或未登记校验值,无法校验(先 PUT /audio/{id}/blob)")
    if audio_store.sha256_hex(p.read_bytes()) != r.checksum:
        raise HTTPException(409, "校验不一致:存储字节与登记 checksum 不符,禁止推进")
    return _transition(raw_audio_id, s, audio_gate.mark_checksum_verified)


@app.post("/audio/{raw_audio_id}/reliability-review", response_model=AudioAssetRow)
def audio_reliability_review(raw_audio_id: str, s: DBSession = Depends(get_session)):
    return _transition(raw_audio_id, s, audio_gate.mark_reliability_review_done)


@app.delete("/audio/{raw_audio_id}")
def audio_delete(raw_audio_id: str, source: str = "auto", s: DBSession = Depends(get_session)):
    """删除音频。未达闸门条件（导出+校验[+信度复核]）→ 409，杜绝到期盲删。"""
    r = _load_row(raw_audio_id, s)
    asset = _row_to_asset(r)
    try:
        audio_gate.request_delete(asset, source=source)
    except audio_gate.AudioGateError as e:
        raise HTTPException(409, str(e))
    r.status = asset.status
    r.delete_gate_passed = True                       # 审计:闸门放行标记
    bytes_deleted = audio_store.delete_blob(raw_audio_id)  # 放行后物理删除字节
    s.add(r); s.commit()
    return {"raw_audio_id": raw_audio_id, "status": r.status, "deleted_by": source,
            "bytes_deleted": bytes_deleted}


@app.get("/audio/{raw_audio_id}", response_model=AudioAssetRow)
def audio_get(raw_audio_id: str, s: DBSession = Depends(get_session)):
    return _load_row(raw_audio_id, s)


@app.put("/audio/{raw_audio_id}/blob")
async def audio_upload_blob(raw_audio_id: str, request: Request, s: DBSession = Depends(get_session)):
    """音频字节落库(本机磁盘 data/audio/,不上云)。登记 sha256 供导出期校验。"""
    if not audio_store.SAFE_ID.match(raw_audio_id):
        raise HTTPException(422, "非法音频 id")
    r = _load_row(raw_audio_id, s)          # 须先 POST /audio 登记元数据
    data = await request.body()
    if not data:
        raise HTTPException(422, "空音频字节")
    p, digest = audio_store.save_blob(raw_audio_id, data, request.headers.get("content-type"))
    r.checksum = digest
    r.audio_format = p.suffix.lstrip(".")
    s.add(r); s.commit()
    return {"raw_audio_id": raw_audio_id, "bytes": len(data),
            "checksum": digest, "format": r.audio_format}


@app.get("/audio/{raw_audio_id}/blob")
def audio_download_blob(raw_audio_id: str, s: DBSession = Depends(get_session)):
    """回放存储字节(操作端复核用,同源本机)。"""
    _load_row(raw_audio_id, s)
    p = audio_store.find_blob(raw_audio_id)
    if not p:
        raise HTTPException(404, "无音频字节(未上传或已删除)")
    return FileResponse(p)


# ---------------- 跨设备实时状态(内网双设备:平板老人端 + 电脑操作端)----------------
import json as _json


class LiveIn(BaseModel):
    kind: str                                # session / cursor / rapportStep / audioSaved
    payload: dict


_LIVE_SLOT = {"session": "session_json", "cursor": "cursor_json",
              "rapportStep": "rapport_json", "audioSaved": "audio_json"}


@app.put("/live/state")
def live_put(body: LiveIn, s: DBSession = Depends(get_session)):
    """写实时状态(操作端为唯一写者;老人端仅写 audioSaved 回报)。seq 单调递增。"""
    slot = _LIVE_SLOT.get(body.kind)
    if not slot:
        raise HTTPException(422, f"未知 kind {body.kind!r}")
    row = s.get(LiveState, 1) or LiveState(id=1, seq=0)
    if body.kind == "session":
        # 新场次握手 → 清掉旧游标/步进/录音回报,防老人端串到上一场
        row.cursor_json = None; row.rapport_json = None; row.audio_json = None
    setattr(row, slot, _json.dumps(body.payload, ensure_ascii=False))
    row.seq += 1
    row.updated_at = datetime.now()
    s.add(row); s.commit(); s.refresh(row)
    return {"seq": row.seq}


@app.get("/live/state")
def live_get(s: DBSession = Depends(get_session)):
    """读实时状态(两端轮询)。seq 不变即无新事,客户端可跳过处理。"""
    row = s.get(LiveState, 1)
    if not row:
        return {"seq": 0, "session": None, "cursor": None, "rapportStep": None, "audioSaved": None}
    load = lambda t: _json.loads(t) if t else None  # noqa: E731
    return {"seq": row.seq, "session": load(row.session_json), "cursor": load(row.cursor_json),
            "rapportStep": load(row.rapport_json), "audioSaved": load(row.audio_json)}


# ---------------- M3 ASR(本地、可插拔;M0=Null 引擎降级人工)----------------
@app.get("/asr/hotwords")
def asr_hotwords():
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    script = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
    hw = asr.build_hotwords(bank, script)
    return {"engine": asr.get_engine().version, "count": len(hw), "hotwords": hw}


@app.post("/asr/transcribe/{raw_audio_id}")
def asr_transcribe(raw_audio_id: str, s: DBSession = Depends(get_session)):
    """本地转写。引擎未接(null)→ degraded=true、asr_text=null,操作端走人工转写,链路不断。"""
    _load_row(raw_audio_id, s)
    p = audio_store.find_blob(raw_audio_id)
    if not p:
        raise HTTPException(404, "无音频字节,无法转写(先 PUT /audio/{id}/blob)")
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    res = asr.get_engine().transcribe(p.read_bytes(), asr.build_hotwords(bank))
    return {"raw_audio_id": raw_audio_id, "asr_text": res.asr_text,
            "asr_confidence": res.asr_confidence, "engine_version": res.engine_version,
            "degraded": res.asr_text is None}


# ---------------- R 会话编排 + 逐环节录音/判分/锁分 ----------------
def _load_bank_for_session(sess: TrainSession) -> content.ItemBank:
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    if sess.item_bank_version_id and bank.version_id != sess.item_bank_version_id:
        raise HTTPException(409, f"场次绑版本 {sess.item_bank_version_id} 与题库 {bank.version_id} 不符")
    return bank


def _find_bank_item(bank: content.ItemBank, item_id: str) -> dict | None:
    for it in list(bank.single_element) + list(bank.double_element):
        if it.get("item_id") == item_id:
            return it
    return None


def _role_target(bank_item: dict, response_role: str) -> str | None:
    """该环节的确定式判分目标词；作用/关系/关键要素类无确定式口径 → None（纯人工）。"""
    return {"命名": bank_item.get("target_word"),
            "左命名": bank_item.get("left_word"),
            "右命名": bank_item.get("right_word")}.get(response_role)


@app.get("/sessions/{session_id}/plan")
def session_plan(session_id: str, week_no: int, event_line: str,
                 max_items: int | None = None, s: DBSession = Depends(get_session)):
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "场次不存在")
    bank = _load_bank_for_session(sess)
    plan = runtime.build_session_plan(bank, week_no, event_line, max_items)
    return {"item_bank_version_id": plan.item_bank_version_id, "week_no": plan.week_no,
            "event_line": plan.event_line, "total_items": len(plan.items),
            "total_turns": plan.total_turns(),
            "items": [{"item_id": it.item_id, "task_type": it.task_type, "image_id": it.image_id,
                       "presentation_order": it.presentation_order, "display": it.display,
                       "turns": [{"turn_seq": t.turn_seq, "response_role": t.response_role,
                                  "scoring_key": t.scoring_key} for t in it.turns]}
                      for it in plan.items]}


class ItemIn(BaseModel):
    item_id: str
    task_type: str
    item_set_type: str = "训练集"
    image_id: str | None = None
    difficulty_level: str | None = None
    presentation_order: int | None = None


@app.post("/sessions/{session_id}/items", response_model=ItemEvent)
def create_item(session_id: str, body: ItemIn, s: DBSession = Depends(get_session)):
    if not s.get(TrainSession, session_id):
        raise HTTPException(404, "场次不存在")
    ie = ItemEvent(session_id=session_id, **body.model_dump())
    s.add(ie); s.commit(); s.refresh(ie)
    return ie


class TurnIn(BaseModel):
    turn_seq: int
    response_role: str | None = None
    raw_audio_id: str | None = None
    asr_text: str | None = None
    asr_confidence: float | None = None
    prompt_level: int | None = None
    duration_seconds: float | None = None


@app.post("/items/{item_event_id}/turns", response_model=TurnEvent)
def create_turn(item_event_id: int, body: TurnIn, s: DBSession = Depends(get_session)):
    if not s.get(ItemEvent, item_event_id):
        raise HTTPException(404, "题目事件不存在")
    te = TurnEvent(item_event_id=item_event_id, **body.model_dump())
    s.add(te); s.commit(); s.refresh(te)
    return te


def _load_turn(turn_id: int, s: DBSession) -> TurnEvent:
    t = s.get(TurnEvent, turn_id)
    if not t:
        raise HTTPException(404, "环节不存在")
    return t


class ConfirmIn(BaseModel):
    confirmed_response_text: str


@app.patch("/turns/{turn_id}/confirm", response_model=TurnEvent)
def confirm_turn(turn_id: int, body: ConfirmIn, s: DBSession = Depends(get_session)):
    """人工改写为 confirmed（分字段，不覆盖 asr_text 原文）。锁定后不得再改。"""
    t = _load_turn(turn_id, s)
    if t.score_locked:
        raise HTTPException(409, "已锁分，不得再改 confirmed 文本")
    t.confirmed_response_text = body.confirmed_response_text
    s.add(t); s.commit(); s.refresh(t)
    return t


@app.post("/turns/{turn_id}/ai-judge", response_model=TurnEvent)
def ai_judge_turn(turn_id: int, s: DBSession = Depends(get_session)):
    """规则确定式 AI 初评（永不锁分）。经画像守卫构造 JudgeInput；无确定式口径的环节纯人工。"""
    t = _load_turn(turn_id, s)
    if t.score_locked:
        raise HTTPException(409, "已锁分，AI 初评不再更新锁定分")
    ie = s.get(ItemEvent, t.item_event_id)
    sess = s.get(TrainSession, ie.session_id)
    bank = _load_bank_for_session(sess)
    bi = _find_bank_item(bank, ie.item_id) or {}
    target = _role_target(bi, t.response_role or "命名")
    if not target:
        # 作用/关系/关键要素等：无确定式判分口径，纯人工。
        t.ai_answer_type = None; t.ai_score = None; t.ai_needs_review = True
        s.add(t); s.commit(); s.refresh(t)
        return t
    ji = judging.build_judge_input(                       # 过画像守卫（混入画像→PortraitLeakError）
        item_id=ie.item_id, task_type=str(ie.task_type), target_word=target,
        acceptable_expressions=tuple(bi.get("acceptable_expressions", []) or []),
        upper_terms=tuple(bi.get("upper_terms", []) or []),
        dialect_synonyms=tuple(bi.get("dialect_synonyms", []) or []),
        asr_text=t.asr_text, confirmed_response_text=t.confirmed_response_text)
    # 后端二(LLM,默认 off)优先尝试;不可用/未启用 → 回退后端一(规则确定式)。两者都只产初评。
    lj = llm_judge.get_engine().judge(ji)
    if lj is not None:
        t.ai_answer_type = lj.answer_type.value
        t.ai_score = lj.ai_score
        t.ai_needs_review = lj.ai_needs_review
        t.ai_judge_mode = "LLM辅助"
    else:
        res = rule_judge.judge_rule_based(ji)
        t.ai_answer_type = res.answer_type.value if res.answer_type else res.interaction_state
        t.ai_score = res.ai_score
        t.ai_needs_review = res.ai_needs_review
        t.ai_judge_mode = "规则确定式"
    t.judge_portrait_used = False
    s.add(t); s.commit(); s.refresh(t)
    return t


class LockIn(BaseModel):
    reviewer_id: str
    element_value: float                     # 单要素 final_correct(0/1) / 双要素分环节(0/1，关系0/0.5/1) / 多要素(0/1)
    reviewed_score: float | None = None       # 缺省取 element_value
    prompt_level: int | None = None


@app.patch("/turns/{turn_id}/lock", response_model=TurnEvent)
def lock_turn(turn_id: int, body: LockIn, s: DBSession = Depends(get_session)):
    """人工锁定评分（研究数据真值）。一旦锁定不得重复锁。"""
    t = _load_turn(turn_id, s)
    if t.score_locked:
        raise HTTPException(409, "该环节已锁分，不可重复锁定")
    t.reviewer_id = body.reviewer_id
    t.element_value = body.element_value
    t.reviewed_score = body.reviewed_score if body.reviewed_score is not None else body.element_value
    if body.prompt_level is not None:
        t.prompt_level = body.prompt_level
    t.score_locked = True
    s.add(t); s.commit(); s.refresh(t)
    return t


# ---------------- 异常/介入（phase 感知）----------------
class AbnormalIn(BaseModel):
    item_event_id: int | None = None
    abnormal_type: str | None = None
    intervention_type: str | None = None
    affects_scoring_validity: bool = False
    note: str | None = None


_CUE_INTERVENTIONS = {"代说物品名", "代说称呼"}


@app.post("/sessions/{session_id}/abnormal", response_model=AbnormalEvent)
def record_abnormal(session_id: str, body: AbnormalIn, s: DBSession = Depends(get_session)):
    """记异常/介入。正式训练周的代说物品名/称呼 → 自动判为线索性介入且影响判分有效性。"""
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "场次不存在")
    data = body.model_dump()
    phase = sess.phase_type.value if hasattr(sess.phase_type, "value") else sess.phase_type
    if data.get("intervention_type") in _CUE_INTERVENTIONS and phase == "正式训练":
        data["abnormal_type"] = data.get("abnormal_type") or "线索性介入"
        data["affects_scoring_validity"] = True
    ev = AbnormalEvent(session_id=session_id, phase_type=sess.phase_type,
                       created_at=datetime.now(), **data)
    s.add(ev); s.commit(); s.refresh(ev)
    return ev


# ---------------- 前后测量表录入（scale_result 容器;量表选型待 PI,字段通用）----------------
class ScaleIn(BaseModel):
    phase_type: str                          # 前测 / 后测 / 随访
    scale_name: str                          # 具体量表名(如 CETI / CADL,待 PI 定)
    subscale: str | None = None
    score: float | None = None
    assessor_id: str | None = None


@app.post("/patients/{patient_id}/scales", response_model=ScaleResult)
def create_scale(patient_id: str, body: ScaleIn, s: DBSession = Depends(get_session)):
    if not s.get(Patient, patient_id):
        raise HTTPException(404, "患者不存在,先建档")
    row = ScaleResult(patient_id=patient_id, assessed_at=datetime.now(), **body.model_dump())
    s.add(row); s.commit(); s.refresh(row)
    return row


@app.get("/patients/{patient_id}/scales")
def list_scales(patient_id: str, s: DBSession = Depends(get_session)):
    if not s.get(Patient, patient_id):
        raise HTTPException(404, "患者不存在")
    return list(s.exec(_select(ScaleResult, ScaleResult.patient_id == patient_id)))


# ---------------- 评分重建（只读）+ 去标识化导出 ----------------
@app.get("/sessions/{session_id}/scores")
def session_scores(session_id: str, s: DBSession = Depends(get_session)):
    """从已锁定分环节值重建综合指标（单一事实源，不落库）。只读，不触发导出。"""
    if not s.get(TrainSession, session_id):
        raise HTTPException(404, "场次不存在")
    items = list(s.exec(_select(ItemEvent, ItemEvent.session_id == session_id)))
    tbi = {it.id: list(s.exec(_select(TurnEvent, TurnEvent.item_event_id == it.id))) for it in items}
    return export._reconstruct_scores(items, tbi)


@app.post("/sessions/{session_id}/export")
def session_export(session_id: str, deidentify: bool = True, s: DBSession = Depends(get_session)):
    """场次收尾去标识化导出（默认走去标识通道，不带直接标识符）。触发音频导出闸门第一关。"""
    try:
        res = export.export_session_bundle(s, session_id, deidentify=deidentify)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"batch_id": res["batch_id"], "deidentified": res["deidentified"],
            "files": res["files"], "audio_touched": res["audio_touched"],
            "excluded_items": res["excluded_items"],
            "sheet_counts": {k: len(v) for k, v in res["sheets"].items()}}


def _select(model, where):
    from sqlmodel import select
    return select(model).where(where)


# ---------------- 生产静态托管(离线部署)----------------
# 前端构建为 web/dist(纯静态,无 node 运行时);存在即由本服务同源托管,医院机器只需 Python。
# 仅当 dist 存在时挂载 → 测试/纯后端环境零影响。SPA 客户端路由(/console /patient)回退 index.html。
def _mount_spa() -> None:
    from pathlib import Path

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    dist = Path(__file__).resolve().parent.parent / "web" / "dist"
    if not dist.exists():
        return
    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        # 显式 API 路由已在上方优先匹配;此处只兜非 API 路径 → 返回 SPA 外壳。
        candidate = dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")


_mount_spa()
