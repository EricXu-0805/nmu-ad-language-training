"""FastAPI 应用骨架——把阶段0 地基接成可运行服务。

已接：健康检查 / 建档(含合规字段) / 建场次 / 题库加载+校验 / 单双多要素评分 /
判分入口(★画像守卫在 API 边界拒绝画像键) / 音频删除闸门(DELETE 未达条件返回 409)。
未接（下一步）：ASR/LLM(本地、可关闭)、导出通道、前端。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import math
import secrets
import threading
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field as PydanticField, model_validator
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session as DBSession, select

import os
from datetime import datetime, timedelta

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse

from fastapi.responses import Response as PlainResponse

from . import asr, audio_gate, audio_store, content, export, judging, llm_judge, rule_judge, runtime, scoring, tts
from .db import engine, get_session, init_db
from .enums import AudioStatus
from .models import (AbnormalEvent, AudioAssetRow, ItemEvent, LiveState, Patient, ScaleResult,
                     SessionRuntimeState, TurnEvent)
from .models import Session as TrainSession


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(engine)
    asr.cleanup_scratch()   # 清扫上次进程异常终止残留的云转写临时音频副本
    yield


app = FastAPI(title="南医大 · AI 语言沟通训练系统", version="0.0.1", lifespan=lifespan)


# ---------------- 操作端 PIN 门(内网双设备模式)----------------
# 设 CONSOLE_PIN 环境变量即启用：写操作与含研究数据的读口须带 X-Console-Pin。
# 老人端必需的 live/plan/TTS 与健康、静态内容保持可读。
def _sensitive_get(path: str) -> bool:
    if path == "/live/console-state":
        return True
    if path.startswith("/patients/") or path.startswith("/audio/"):
        return True
    if path.startswith("/sessions/"):
        return not path.endswith("/plan")
    return False


@app.middleware("http")
async def console_pin_guard(request: Request, call_next):
    pin = os.environ.get("CONSOLE_PIN")
    needs_pin = request.method in ("POST", "PUT", "PATCH", "DELETE") \
        or (request.method == "GET" and _sensitive_get(request.url.path))
    if pin and needs_pin:
        supplied = request.headers.get("x-console-pin", "")
        if not secrets.compare_digest(supplied, pin):
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


@app.get("/patients")
def list_patients(s: DBSession = Depends(get_session)):
    """受试者登记表(准备区/训练台/分析后台的选择列表)。只出研究编号与合规摘要,无姓名。
    附场次数与最近训练日,供研究者按编号选人——绝不显示场次编号(那是后台概念)。"""
    patients = list(s.exec(select(Patient).order_by(Patient.patient_id)))
    sessions = list(s.exec(select(TrainSession)))
    by_patient: dict[str, list] = {}
    for sess in sessions:
        by_patient.setdefault(sess.patient_id, []).append(sess)
    out = []
    for p in patients:
        rows = by_patient.get(p.patient_id, [])
        dates = [r.training_date for r in rows if r.training_date]
        out.append({
            "patient_id": p.patient_id,
            "dementia_severity": p.dementia_severity,
            "mandarin_eligible": p.mandarin_eligible,
            "consent_type": p.consent_type.value if hasattr(p.consent_type, "value") else p.consent_type,
            "recording_allowed": p.recording_allowed,
            "withdrawal_status": p.withdrawal_status,
            "session_count": len(rows),
            "last_training_date": max(dates).isoformat() if dates else None,
        })
    return out


@app.get("/patients/{patient_id}", response_model=Patient)
def get_patient(patient_id: str, s: DBSession = Depends(get_session)):
    p = s.get(Patient, patient_id)
    if not p:
        raise HTTPException(404, "患者不存在")
    return p


@app.get("/patients/{patient_id}/sessions")
def list_patient_sessions(patient_id: str, s: DBSession = Depends(get_session)):
    """只读恢复入口：列出患者既有场次，便于异常中断后取回续做。"""
    if not s.get(Patient, patient_id):
        raise HTTPException(404, "患者不存在")
    return list(s.exec(select(TrainSession)
                       .where(TrainSession.patient_id == patient_id)
                       .order_by(TrainSession.training_date, TrainSession.session_sitting_no,
                                 TrainSession.session_id)))


@app.post("/sessions", response_model=TrainSession)
def create_session(sess: TrainSession, s: DBSession = Depends(get_session)):
    if s.get(TrainSession, sess.session_id):
        raise HTTPException(409, f"session_id {sess.session_id} 已存在(可取回续做)")
    if not s.get(Patient, sess.patient_id):
        raise HTTPException(404, "患者不存在，先建档")
    if not sess.item_bank_version_id:
        raise HTTPException(422, "场次须绑题库版本号 item_bank_version_id")
    if not 1 <= sess.week_no <= 8:
        raise HTTPException(422, "week_no 必须在 1..8")
    phase = sess.phase_type.value if hasattr(sess.phase_type, "value") else str(sess.phase_type)
    event = sess.event_line.value if hasattr(sess.event_line, "value") else str(sess.event_line)
    allowed_context = (
        (sess.week_no == 1 and phase == "关系建立" and event == "关系建立环节")
        or (sess.week_no == 1 and phase in {"基线测评", "前测"} and event == "基线测评窗")
        or (2 <= sess.week_no <= 8 and phase == "正式训练" and event == "正式训练")
    )
    if not allowed_context:
        raise HTTPException(422, "week_no / phase_type / event_line 组合不符合已定事件线")
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    if sess.item_bank_version_id != bank.version_id:
        raise HTTPException(409, f"场次版本 {sess.item_bank_version_id} 与当前题库 {bank.version_id} 不符")
    if phase == "正式训练" and sess.week_no not in bank.supported_training_weeks:
        raise HTTPException(409, f"第{sess.week_no}周材料尚未结构化并双人校对，禁止建正式训练场次")
    s.add(sess); s.commit(); s.refresh(sess)
    return sess


@app.get("/sessions/{session_id}", response_model=TrainSession)
def get_train_session(session_id: str, s: DBSession = Depends(get_session)):
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "场次不存在")
    return sess


# ---------------- 内容 ----------------
@app.get("/content/item-bank")
def get_item_bank():
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    readiness = content.content_readiness(bank)
    return {
        "version_id": bank.version_id,
        "single_count": len(bank.single_element),
        "double_count": len(bank.double_element),
        "multi_count": len(bank.multi_element),
        "supported_training_weeks": list(bank.supported_training_weeks),
        "qc_status": bank.qc_status,
        "ready_for_research": readiness["ready_for_research"],
        "errata_fixed": bank.errata_fixed,
        "errors": readiness["errors"], "warnings": readiness["warnings"],
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
    turn_key: str | None = PydanticField(default=None, min_length=1, max_length=200,
                                         pattern=r"^[^\r\n\x00]+$")
    is_reliability_sample: bool = False
    contains_direct_identifier: bool = False


def _ensure_recording_allowed_for_session(session_id: str | None, s: DBSession) -> None:
    if not session_id:
        return
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "音频关联场次不存在")
    patient = s.get(Patient, sess.patient_id)
    if patient and patient.recording_allowed is False:
        raise HTTPException(409, "受试者档案 recording_allowed=false，禁止登记或上传音频")


@app.post("/audio", response_model=AudioAssetRow)
def create_audio(a: AudioIn, s: DBSession = Depends(get_session)):
    if s.get(AudioAssetRow, a.raw_audio_id):
        raise HTTPException(409, "音频已存在")
    _ensure_recording_allowed_for_session(a.session_id, s)
    if a.turn_key is not None and not a.turn_key.strip():
        raise HTTPException(422, "turn_key 不得为空白")
    _validate_audio_turn_key(a.session_id, a.turn_key, s)
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
    _load_row(raw_audio_id, s)
    raise HTTPException(409, "不允许脱离场次导出盲标 exported；请调用 POST /sessions/{session_id}/export")


@app.post("/audio/{raw_audio_id}/checksum", response_model=AudioAssetRow)
def audio_checksum(raw_audio_id: str, s: DBSession = Depends(get_session)):
    """导出期校验：只校验本批次的受控导出副本，不以采集源文件冒充。"""
    r = _load_row(raw_audio_id, s)
    if not r.export_batch_id:
        raise HTTPException(409, "音频尚未经场次导出，无受控副本可校验")
    p = export.find_exported_audio_blob(raw_audio_id, r.export_batch_id)
    if not p or not r.checksum:
        raise HTTPException(409, "本批次受控音频副本不存在或未登记校验值")
    if audio_store.sha256_hex(p.read_bytes()) != r.checksum:
        raise HTTPException(409, "校验不一致：受控导出副本与登记 checksum 不符，禁止推进")
    return _transition(raw_audio_id, s, audio_gate.mark_checksum_verified)


@app.post("/audio/{raw_audio_id}/reliability-review", response_model=AudioAssetRow)
def audio_reliability_review(raw_audio_id: str, s: DBSession = Depends(get_session)):
    return _transition(raw_audio_id, s, audio_gate.mark_reliability_review_done)


@app.delete("/audio/{raw_audio_id}")
def audio_delete(raw_audio_id: str, source: str = "auto", s: DBSession = Depends(get_session)):
    """删除音频。未达闸门条件（导出+校验[+信度复核]）→ 409，杜绝到期盲删。"""
    r = _load_row(raw_audio_id, s)
    if os.environ.get("ENABLE_AUDIO_DELETE") != "1":
        raise HTTPException(409, "物理删除默认禁用；须在受控环境显式设置 ENABLE_AUDIO_DELETE=1")
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
    _ensure_recording_allowed_for_session(r.session_id, s)
    if r.status != AudioStatus.recorded:
        raise HTTPException(409, "音频已进入导出/校验/删除流程，禁止覆盖采集字节")
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


_LIVE_SLOT = {"session": "session_json", "cursor": "cursor_json",
              "rapportStep": "rapport_json", "audioSaved": "audio_json",
              "patientRec": "patient_rec_json"}

_CURSOR_SCREENS = {"idle", "present", "record", "thanks", "paused", "done"}
_RECORDING_STATES = {"idle", "armed", "recording", "stopped"}
PATIENT_ONLINE_TTL_SECONDS = 15
_LIVE_WRITE_LOCK = threading.RLock()
_SERVER_WSEQ = 0


class PatientHeartbeatIn(BaseModel):
    """老人端最小回执；配置 CONSOLE_PIN 时同样须认证，且拒绝额外敏感字段。"""
    model_config = ConfigDict(extra="forbid")

    session_id: str = PydanticField(min_length=1, max_length=128)
    screen: Literal[
        "idle", "waiting", "loading", "rapport", "present", "record", "thanks",
        "paused", "complete", "done", "error",
    ]
    cursor_wseq: int | None = PydanticField(default=None, ge=0)
    # 仅为旧/弱网客户端诊断兼容；在线真值一律采用服务器收件时间，绝不信任客户端时钟。
    client_ts: datetime | None = None


class RuntimeCursorIn(BaseModel):
    """受 PIN 保护的正式训练游标写入契约，不接受内容文本或回答。
    自动驾驶反馈同样不载文本:fbKey 只指向题库/协议里的固定话术,老人端本地查表回填。"""
    model_config = ConfigDict(extra="forbid")

    screen: Literal["idle", "present", "record", "thanks", "done"]
    itemIdx: int = PydanticField(ge=0)
    turnIdx: int = PydanticField(ge=0)
    responseRole: str | None = PydanticField(default=None, max_length=64)
    cueLevel: int | None = PydanticField(default=None, ge=0, le=3)
    recording: Literal["idle", "armed", "recording", "stopped"] = "idle"
    recSeq: int | None = PydanticField(default=None, ge=0)
    rawAudioId: str | None = PydanticField(default=None, max_length=160)
    selfStart: bool | None = None
    fbKey: Literal["self", "cued1_unknown", "cued1_close", "cued1_silence",
                   "cued2", "namefix_l", "namefix_r"] | None = None
    fbItemId: str | None = PydanticField(default=None, max_length=160)
    fbSeq: int | None = PydanticField(default=None, ge=0)
    wseq: int | None = PydanticField(default=None, ge=0)
    sessionId: str | None = PydanticField(default=None, min_length=1, max_length=128)
    session_id: str | None = PydanticField(default=None, min_length=1, max_length=128)
    # 乐观并发:带上调用方已知的 revision,与服务端不一致即 409——旧标签页/旧设备
    # 不能凭过期位置静默把老人端游标倒回旧题。不带则跳过检查(兼容脚本化调用)。
    expected_revision: int | None = PydanticField(default=None, ge=0)


class LiveSessionPayload(BaseModel):
    """患者端可见的场次握手；不接收姓名、画像或其他业务字段。"""
    model_config = ConfigDict(extra="forbid")

    sessionId: str | None = PydanticField(default=None, min_length=1, max_length=128)
    session_id: str | None = PydanticField(default=None, min_length=1, max_length=128)
    weekNo: int | None = PydanticField(default=None, ge=1, le=8)
    eventLine: str | None = PydanticField(default=None, min_length=1, max_length=64)
    mode: Literal["task", "rapport"] | None = None
    itemBankVersionId: str | None = PydanticField(default=None, min_length=1, max_length=128)
    wseq: int | None = PydanticField(default=None, ge=0)


class LiveRapportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sessionId: str | None = PydanticField(default=None, min_length=1, max_length=128)
    session_id: str | None = PydanticField(default=None, min_length=1, max_length=128)
    sectionKey: str = PydanticField(min_length=1, max_length=100,
                                    pattern=r"^[^\r\n\x00]+$")
    questionIdx: int = PydanticField(ge=0)
    recording: Literal["idle", "armed", "recording", "stopped"] = "idle"
    recSeq: int | None = PydanticField(default=None, ge=0)
    rawAudioId: str | None = PydanticField(default=None, max_length=160)
    assentGate: bool | None = None
    containsDirectIdentifier: bool | None = None
    wseq: int | None = PydanticField(default=None, ge=0)


class LiveAudioSavedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rawAudioId: str = PydanticField(min_length=1, max_length=160,
                                    pattern=r"^[^\r\n\x00]+$")
    durationSeconds: float = PydanticField(ge=0, le=21_600)
    turnKey: str = PydanticField(min_length=1, max_length=200,
                                 pattern=r"^[^\r\n\x00]+$")
    sessionId: str | None = PydanticField(default=None, min_length=1, max_length=128)
    session_id: str | None = PydanticField(default=None, min_length=1, max_length=128)
    containsDirectIdentifier: bool | None = None


class LivePatientRecPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool
    turnKey: str = PydanticField(min_length=1, max_length=200,
                                 pattern=r"^[^\r\n\x00]+$")
    sessionId: str | None = PydanticField(default=None, min_length=1, max_length=128)
    session_id: str | None = PydanticField(default=None, min_length=1, max_length=128)


_LIVE_PAYLOAD_MODELS = {
    "session": LiveSessionPayload,
    "cursor": RuntimeCursorIn,
    "rapportStep": LiveRapportPayload,
    "audioSaved": LiveAudioSavedPayload,
    "patientRec": LivePatientRecPayload,
}


class LiveIn(BaseModel):
    """实时写入边界：先按 kind 收紧 payload，再进入场次/计划语义校验。"""
    model_config = ConfigDict(extra="forbid")

    kind: Literal["session", "cursor", "rapportStep", "audioSaved", "patientRec"]
    payload: dict

    @model_validator(mode="after")
    def validate_payload_contract(self):
        payload_model = _LIVE_PAYLOAD_MODELS[self.kind]
        self.payload = payload_model.model_validate(self.payload).model_dump(exclude_none=True)
        return self


def _json_load(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        value = _json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _live_session_id(row: LiveState | None) -> str | None:
    payload = _json_load(row.session_json) if row else None
    value = (payload or {}).get("sessionId") or (payload or {}).get("session_id")
    return value if isinstance(value, str) and value else None


def _payload_session_id(payload: dict) -> str | None:
    camel = payload.get("sessionId")
    snake = payload.get("session_id")
    if camel is not None and snake is not None and camel != snake:
        raise HTTPException(422, "sessionId 与 session_id 不一致")
    value = camel if camel is not None else snake
    return value if isinstance(value, str) and value else None


_PUBLIC_LIVE_FIELDS = {
    "session": {"sessionId", "weekNo", "eventLine", "mode", "itemBankVersionId",
                "paused", "wseq"},
    "cursor": {"sessionId", "screen", "itemIdx", "turnIdx", "responseRole",
               "cueLevel", "recording", "recSeq", "selfStart",
               "fbKey", "fbItemId", "fbSeq", "wseq"},
    "rapportStep": {"sessionId", "sectionKey", "questionIdx", "recording", "recSeq",
                    "assentGate", "containsDirectIdentifier", "paused", "wseq"},
}


def _public_live_projection(kind: str, text: str | None) -> dict | None:
    """即使数据库里留有旧版/异常额外键，患者免 PIN 读口也只返回呈现白名单。"""
    payload = _json_load(text)
    if payload is None:
        return None
    allowed = _PUBLIC_LIVE_FIELDS[kind]
    return {key: value for key, value in payload.items() if key in allowed}


def _live_row_for_update(s: DBSession) -> LiveState | None:
    return s.exec(select(LiveState).where(LiveState.id == 1).with_for_update()).first()


def _wseq_from(payload: dict | None) -> int | None:
    value = (payload or {}).get("wseq")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _stamp_command_source(payload: dict, _previous: dict | None, _kind: str) -> None:
    """仅留存客户端序号供诊断，不让它参与服务端顺序判定。

    不同设备、不同场次的本地计数器不在同一个时钟域内；强制比较会把合法的
    切场、重连或客户端重启误判为回退。命令总序只由 ``_allocate_live_wseq``
    在服务端接收顺序上生成。
    """
    payload.pop("sourceWseq", None)
    incoming = _wseq_from(payload)
    if incoming is not None:
        payload["sourceWseq"] = incoming


def _allocate_live_wseq(row: LiveState) -> int:
    """由服务端分配唯一递增命令序号；客户端传入值不参与决策，避免旧/快钟设备污染。"""
    global _SERVER_WSEQ
    floors = [int(datetime.now().timestamp() * 1000), row.command_wseq or 0, _SERVER_WSEQ]
    for payload in (_json_load(row.session_json), _json_load(row.cursor_json),
                    _json_load(row.rapport_json)):
        value = _wseq_from(payload)
        if value is not None:
            floors.append(value)
    value = max(floors) + 1
    _SERVER_WSEQ = value
    row.command_wseq = value
    return value


def _allocate_runtime_wseq(state: SessionRuntimeState) -> int:
    """场次不在当前 LiveState 时仍给其安全恢复指针分配新的服务端序号。"""
    global _SERVER_WSEQ
    floors = [int(datetime.now().timestamp() * 1000), _SERVER_WSEQ]
    for payload in (_json_load(state.cursor_json), _json_load(state.rapport_json)):
        value = _wseq_from(payload)
        if value is not None:
            floors.append(value)
    value = max(floors) + 1
    _SERVER_WSEQ = value
    return value


def _prime_live_wseq_from_runtime(row: LiveState, state: SessionRuntimeState | None) -> None:
    """进程重启后，非当前场次 runtime 可能比 LiveState 更新；握手前先纳入序号下界。"""
    if state is None:
        return
    for payload in (_json_load(state.cursor_json), _json_load(state.rapport_json)):
        value = _wseq_from(payload)
        if value is not None:
            row.command_wseq = max(row.command_wseq or 0, value)


def _presence_payload(row: LiveState | None, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    last_seen = row.patient_last_seen_at if row else None
    online = bool(last_seen and now - last_seen <= timedelta(seconds=PATIENT_ONLINE_TTL_SECONDS))
    return {
        "session_id": row.patient_ack_session_id if row else None,
        "screen": row.patient_current_screen if row else None,
        "last_seen_at": last_seen,
        "online": online,
        "cursor_wseq": row.patient_ack_seq if row else None,
    }


def _runtime_row(session_id: str, s: DBSession) -> SessionRuntimeState:
    row = s.get(SessionRuntimeState, session_id)
    if row is None:
        row = SessionRuntimeState(session_id=session_id, status="active", revision=0)
    return row


def _runtime_payload(session_id: str, row: SessionRuntimeState | None) -> dict:
    return {
        "sessionId": session_id,
        "status": row.status if row else "active",
        "revision": row.revision if row else 0,
        "cursor": _json_load(row.cursor_json) if row else None,
        "rapportStep": _json_load(row.rapport_json) if row else None,
        "pausedAt": row.paused_at if row else None,
        "resumedAt": row.resumed_at if row else None,
        "updatedAt": row.updated_at if row else None,
    }


def _safe_cursor(payload: dict) -> dict:
    """恢复时绝不自动重开麦克风，也不携带在途音频指针。"""
    safe = dict(payload)
    if safe.get("screen") in {"record", "paused"}:
        safe["screen"] = "present"
    safe["recording"] = "idle"
    safe["selfStart"] = False
    safe.pop("rawAudioId", None)
    return safe


def _safe_rapport(payload: dict) -> dict:
    safe = dict(payload)
    safe["recording"] = "idle"
    safe.pop("rawAudioId", None)
    safe.pop("paused", None)
    return safe


def _session_plan_for_runtime(sess: TrainSession) -> runtime.SessionPlan:
    bank = _load_bank_for_session(sess)
    event = sess.event_line.value if hasattr(sess.event_line, "value") else str(sess.event_line)
    try:
        return runtime.build_session_plan(bank, sess.week_no, event)
    except ValueError as e:
        raise HTTPException(409, str(e))


def _validate_session_handshake(sess: TrainSession, payload: dict) -> None:
    event = sess.event_line.value if hasattr(sess.event_line, "value") else str(sess.event_line)
    checks = (
        ("weekNo", sess.week_no),
        ("eventLine", event),
        ("itemBankVersionId", sess.item_bank_version_id),
    )
    for key, expected in checks:
        if key == "weekNo" and key in payload and (
                not isinstance(payload[key], int) or isinstance(payload[key], bool)):
            raise HTTPException(422, "session payload 的 weekNo 必须是整数")
        if key in payload and payload[key] != expected:
            raise HTTPException(409, f"session payload 的 {key} 与数据库场次不一致")
    expected_mode = "rapport" if sess.week_no == 1 else "task"
    if "mode" in payload and payload["mode"] != expected_mode:
        raise HTTPException(409, "session payload 的 mode 与数据库场次不一致")


def _require_live_payload_session(payload: dict, row: LiveState, s: DBSession) -> TrainSession:
    session_id = _payload_session_id(payload)
    if not session_id:
        raise HTTPException(422, "live payload 必须显式携带 sessionId 或 session_id")
    if session_id != _live_session_id(row):
        raise HTTPException(409, "live payload 场次与当前操作端场次不一致")
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "live payload 场次不存在")
    return sess


def _validate_audio_turn_key(session_id: str | None, turn_key: str | None,
                             s: DBSession) -> None:
    if turn_key is None:
        return
    if not session_id:
        raise HTTPException(422, "turn_key 必须绑定 session_id")
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "音频关联场次不存在")
    if sess.week_no == 1:
        script = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
        allowed = {f"关系建立·{section.get('key')}" for section in script.get("sections", [])
                   if section.get("key")}
    else:
        plan = _session_plan_for_runtime(sess)
        allowed = {f"{item.item_id}#{turn.turn_seq}"
                   for item in plan.items for turn in item.turns}
    if turn_key not in allowed:
        raise HTTPException(422, "turn_key 不属于该场次绑定的冻结计划/脚本")


def _turn_is_locked(s: DBSession, session_id: str, item_id: str, turn_seq: int) -> bool:
    items = list(s.exec(select(ItemEvent).where(
        ItemEvent.session_id == session_id, ItemEvent.item_id == item_id)))
    for item in items:
        if item.id is None:
            continue
        turns = s.exec(select(TurnEvent).where(
            TurnEvent.item_event_id == item.id, TurnEvent.turn_seq == turn_seq))
        if any(turn.score_locked for turn in turns):
            return True
    return False


def _validate_cursor(sess: TrainSession, payload: dict, s: DBSession) -> None:
    item_idx = payload.get("itemIdx")
    turn_idx = payload.get("turnIdx")
    if (not isinstance(item_idx, int) or isinstance(item_idx, bool)
            or not isinstance(turn_idx, int) or isinstance(turn_idx, bool)):
        raise HTTPException(422, "itemIdx/turnIdx 必须是非负整数")
    if item_idx < 0 or turn_idx < 0:
        raise HTTPException(422, "itemIdx/turnIdx 不得小于 0")
    if payload.get("screen") not in _CURSOR_SCREENS:
        raise HTTPException(422, "未知患者画面 screen")
    if payload.get("recording", "idle") not in _RECORDING_STATES:
        raise HTTPException(422, "未知 recording 状态")

    plan = _session_plan_for_runtime(sess)
    if item_idx >= len(plan.items):
        raise HTTPException(422, "itemIdx 超出场次冻结计划")
    item = plan.items[item_idx]
    if turn_idx >= len(item.turns):
        raise HTTPException(422, "turnIdx 超出题目冻结计划")
    turn = item.turns[turn_idx]
    supplied_role = payload.get("responseRole")
    if supplied_role is not None and supplied_role != turn.response_role:
        raise HTTPException(422, "responseRole 与冻结计划当前位置不一致")

    asks_to_record = payload.get("recording") in {"armed", "recording"} or payload.get("selfStart") is True
    if asks_to_record:
        patient = s.get(Patient, sess.patient_id)
        if patient and patient.recording_allowed is False:
            raise HTTPException(409, "受试者 recording_allowed=false，禁止下发录音状态")
        if _turn_is_locked(s, sess.session_id, item.item_id, turn.turn_seq):
            raise HTTPException(409, "当前位置已锁分，禁止重新下发录音状态")


def _validate_rapport(sess: TrainSession, payload: dict, s: DBSession) -> None:
    if sess.week_no != 1:
        raise HTTPException(409, "rapportStep 仅属于第1周关系建立场次")
    section_key = payload.get("sectionKey")
    question_idx = payload.get("questionIdx")
    if not isinstance(section_key, str) or not section_key:
        raise HTTPException(422, "sectionKey 不得为空")
    if not isinstance(question_idx, int) or isinstance(question_idx, bool) or question_idx < 0:
        raise HTTPException(422, "questionIdx 必须是非负整数")
    script = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
    section = next((row for row in script.get("sections", []) if row.get("key") == section_key), None)
    if section is None:
        raise HTTPException(422, "sectionKey 不在冻结关系建立脚本中")
    questions = section.get("questions") or []
    if (questions and question_idx >= len(questions)) or (not questions and question_idx != 0):
        raise HTTPException(422, "questionIdx 超出冻结关系建立脚本")
    recording = payload.get("recording", "idle")
    if recording not in _RECORDING_STATES:
        raise HTTPException(422, "未知 recording 状态")
    if recording in {"armed", "recording"}:
        patient = s.get(Patient, sess.patient_id)
        if patient and patient.recording_allowed is False:
            raise HTTPException(409, "受试者 recording_allowed=false，禁止下发录音状态")


def _pause_projection(payload: dict, wseq: int) -> dict:
    paused = _safe_cursor(payload)
    paused["screen"] = "paused"
    paused["recording"] = "stopped"
    paused["wseq"] = wseq
    return paused


def _set_live_session_paused(row: LiveState, paused: bool) -> int | None:
    """暂停是场次级状态；即使尚无 cursor/rapport，患者端也必须立即收到休息指令。"""
    payload = _json_load(row.session_json)
    if payload is None:
        return None
    payload["paused"] = paused
    wseq = _allocate_live_wseq(row)
    payload["wseq"] = wseq
    row.session_json = _json.dumps(payload, ensure_ascii=False)
    return wseq


def _restore_runtime_to_live(row: LiveState, state: SessionRuntimeState | None) -> None:
    """把持久位置投影回 live；每个恢复快照重新分配高于所有旧快照的服务端 wseq。"""
    row.cursor_json = None
    row.rapport_json = None
    if state is None:
        return
    changed = False
    cursor = _json_load(state.cursor_json)
    if cursor:
        cursor = _safe_cursor(cursor)
        cursor["sessionId"] = state.session_id
        cursor.pop("session_id", None)
        wseq = _allocate_live_wseq(row)
        cursor["wseq"] = wseq
        state.cursor_json = _json.dumps(cursor, ensure_ascii=False)
        projected = (_pause_projection(cursor, wseq)
                     if state.status == "paused" else cursor)
        row.cursor_json = _json.dumps(projected, ensure_ascii=False)
        changed = True

    rapport = _json_load(state.rapport_json)
    if rapport:
        rapport = _safe_rapport(rapport)
        rapport["sessionId"] = state.session_id
        rapport.pop("session_id", None)
        wseq = _allocate_live_wseq(row)
        rapport["wseq"] = wseq
        state.rapport_json = _json.dumps(rapport, ensure_ascii=False)
        projected = dict(rapport)
        if state.status == "paused":
            projected.update({"recording": "stopped", "paused": True})
        row.rapport_json = _json.dumps(projected, ensure_ascii=False)
        changed = True

    if changed:
        state.revision += 1
        state.updated_at = datetime.now()


@app.put("/live/state")
def live_put(body: LiveIn, s: DBSession = Depends(get_session)):
    """写实时状态；服务端重签 wseq，并把游标同步到当前场次恢复行。"""
    slot = _LIVE_SLOT.get(body.kind)
    if not slot:
        raise HTTPException(422, f"未知 kind {body.kind!r}")

    def apply(row: LiveState) -> tuple[LiveState, int | None]:
        payload = dict(body.payload)
        command_wseq: int | None = None
        if body.kind == "session":
            previous_session_id = _live_session_id(row)
            session_id = _payload_session_id(payload)
            if not session_id:
                raise HTTPException(422, "session payload 缺 sessionId")
            sess = s.get(TrainSession, session_id)
            if not sess:
                raise HTTPException(404, "session payload 场次不存在")
            _validate_session_handshake(sess, payload)
            _stamp_command_source(payload, _json_load(row.session_json), "session")
            payload["sessionId"] = session_id
            payload.pop("session_id", None)
            state = s.get(SessionRuntimeState, session_id)
            _prime_live_wseq_from_runtime(row, state)
            command_wseq = _allocate_live_wseq(row)
            payload["wseq"] = command_wseq
            if state and state.status == "paused":
                # pause 可能在首次 live 握手之前已落 runtime。后到握手必须继承休息状态，
                # 否则老人端会因“无游标”停在加载页，而不是明确的暂停屏。
                payload["paused"] = True
            # 新场次握手清瞬时回报，但从该场次自己的 runtime 行恢复安全位置，避免串场或覆盖。
            row.audio_json = None
            row.patient_rec_json = None
            if previous_session_id != session_id:
                row.patient_ack_session_id = None
                row.patient_current_screen = None
                row.patient_last_seen_at = None
                row.patient_ack_seq = None
            setattr(row, slot, _json.dumps(payload, ensure_ascii=False))
            _restore_runtime_to_live(row, state)
            if state:
                s.add(state)
            command_wseq = row.command_wseq
        elif body.kind in {"cursor", "rapportStep"}:
            sess = _require_live_payload_session(payload, row, s)
            _stamp_command_source(payload, _json_load(getattr(row, slot)), body.kind)
            session_id = sess.session_id
            payload["sessionId"] = session_id
            payload.pop("session_id", None)
            state = _runtime_row(session_id, s)
            if state.status == "paused":
                raise HTTPException(409, "场次已暂停；须先恢复后才能推进游标")
            if body.kind == "cursor":
                _validate_cursor(sess, payload, s)
            else:
                _validate_rapport(sess, payload, s)
            command_wseq = _allocate_live_wseq(row)
            payload["wseq"] = command_wseq
            if body.kind == "cursor":
                state.cursor_json = _json.dumps(_safe_cursor(payload), ensure_ascii=False)
            else:
                state.rapport_json = _json.dumps(_safe_rapport(payload), ensure_ascii=False)
            state.revision += 1
            state.updated_at = datetime.now()
            s.add(state)
            setattr(row, slot, _json.dumps(payload, ensure_ascii=False))
        else:
            # 瞬时老人端回报也必须显式绑定当前场次，避免切场竞态污染新场 journal。
            sess = _require_live_payload_session(payload, row, s)
            if body.kind == "audioSaved":
                asset = _load_row(payload["rawAudioId"], s)
                if asset.session_id != sess.session_id or asset.turn_key != payload["turnKey"]:
                    raise HTTPException(409, "audioSaved 与已登记音频的场次/环节不一致")
                if not audio_store.find_blob(asset.raw_audio_id):
                    raise HTTPException(409, "audioSaved 缺少已落库的音频字节")
                reported_identifier = payload.get("containsDirectIdentifier")
                if (reported_identifier is not None
                        and reported_identifier != asset.contains_direct_identifier):
                    raise HTTPException(409, "audioSaved 直接标识标记与音频登记不一致")
                payload["containsDirectIdentifier"] = asset.contains_direct_identifier
            else:
                _validate_audio_turn_key(sess.session_id, payload["turnKey"], s)
            setattr(row, slot, _json.dumps(payload, ensure_ascii=False))
        row.seq += 1
        row.updated_at = datetime.now()
        s.add(row)
        return row, command_wseq

    with _LIVE_WRITE_LOCK:
        row, command_wseq = apply(_live_row_for_update(s) or LiveState(id=1, seq=0))
        try:
            s.commit()
        except IntegrityError:
            # 多进程空库首写竞态：回滚并在已落库单例上重放，服务端序号只会继续前进。
            s.rollback()
            existing = _live_row_for_update(s)
            if existing is None:
                raise
            row, command_wseq = apply(existing)
            s.commit()
        s.refresh(row)
    result = {"seq": row.seq}
    if command_wseq is not None:
        result["wseq"] = command_wseq
    return result


@app.get("/live/state")
def live_get(s: DBSession = Depends(get_session)):
    """患者端最小读快照：仅含呈现所需 session/cursor/rapportStep。"""
    row = s.get(LiveState, 1)
    if not row:
        return {"seq": 0, "session": None, "cursor": None, "rapportStep": None}
    return {"seq": row.seq,
            "session": _public_live_projection("session", row.session_json),
            "cursor": _public_live_projection("cursor", row.cursor_json),
            "rapportStep": _public_live_projection("rapportStep", row.rapport_json)}


@app.get("/live/console-state")
def live_console_get(s: DBSession = Depends(get_session)):
    """研究者端完整实时快照；配置 CONSOLE_PIN 时由中间件保护。"""
    row = s.get(LiveState, 1)
    if not row:
        return {"seq": 0, "session": None, "cursor": None, "rapportStep": None,
                "audioSaved": None, "patientRec": None,
                "patientPresence": _presence_payload(None)}
    return {"seq": row.seq, "session": _json_load(row.session_json),
            "cursor": _json_load(row.cursor_json),
            "rapportStep": _json_load(row.rapport_json),
            "audioSaved": _json_load(row.audio_json),
            "patientRec": _json_load(row.patient_rec_json),
            "patientPresence": _presence_payload(row)}


@app.post("/live/patient-heartbeat")
def patient_heartbeat(body: PatientHeartbeatIn, s: DBSession = Depends(get_session)):
    """老人端最小在线/当前画面回执；不改变操作端命令 seq。"""
    with _LIVE_WRITE_LOCK:
        row = _live_row_for_update(s)
        if not row or _live_session_id(row) != body.session_id:
            raise HTTPException(409, "heartbeat 场次不是当前操作端场次")
        if not s.get(TrainSession, body.session_id):
            raise HTTPException(404, "heartbeat 场次不存在")

        # ack 序号只作展示线索,不参与顺序判定,照单保留:同机部署下患者端显示的游标
        # 常来自 BroadcastChannel(客户端时钟域戳),在操作端 HTTP 写落库前会"超前"于
        # row.command_wseq——按超前拒绝会让每次推进后的第一拍心跳都被 409,在场判定滞后。

        now = datetime.now()
        row.patient_ack_session_id = body.session_id
        row.patient_current_screen = body.screen
        row.patient_last_seen_at = now
        row.patient_ack_seq = body.cursor_wseq
        s.add(row); s.commit(); s.refresh(row)
    return {"ok": True, "server_time": now,
            "patientPresence": _presence_payload(row, now)}


@app.get("/sessions/{session_id}/runtime")
def get_session_runtime(session_id: str, s: DBSession = Depends(get_session)):
    if not s.get(TrainSession, session_id):
        raise HTTPException(404, "场次不存在")
    return _runtime_payload(session_id, s.get(SessionRuntimeState, session_id))


@app.put("/sessions/{session_id}/runtime/cursor")
def put_session_runtime_cursor(session_id: str, body: RuntimeCursorIn,
                               s: DBSession = Depends(get_session)):
    with _LIVE_WRITE_LOCK:
        sess = s.get(TrainSession, session_id)
        if not sess:
            raise HTTPException(404, "场次不存在")
        payload = body.model_dump(exclude_none=True)
        expected_revision = payload.pop("expected_revision", None)
        supplied_session_id = _payload_session_id(payload)
        if supplied_session_id and supplied_session_id != session_id:
            raise HTTPException(409, "runtime cursor payload 与路径场次不一致")
        payload["sessionId"] = session_id
        payload.pop("session_id", None)
        state = _runtime_row(session_id, s)
        if expected_revision is not None and expected_revision != state.revision:
            raise HTTPException(409, "runtime revision 已变化;请先刷新场次状态再写入")
        _stamp_command_source(payload, _json_load(state.cursor_json), "runtime cursor")
        _validate_cursor(sess, payload, s)
        if state.status == "paused":
            raise HTTPException(409, "场次已暂停；须先恢复后才能推进游标")

        live = _live_row_for_update(s)
        if live and _live_session_id(live) == session_id:
            payload["wseq"] = _allocate_live_wseq(live)
            live.cursor_json = _json.dumps(payload, ensure_ascii=False)
            live.seq += 1
            live.updated_at = datetime.now()
            s.add(live)
        else:
            payload["wseq"] = _allocate_runtime_wseq(state)
        state.cursor_json = _json.dumps(_safe_cursor(payload), ensure_ascii=False)
        state.revision += 1
        state.updated_at = datetime.now()
        s.add(state)
        s.commit(); s.refresh(state)
    return _runtime_payload(session_id, state)


@app.post("/sessions/{session_id}/pause")
def pause_session(session_id: str, s: DBSession = Depends(get_session)):
    with _LIVE_WRITE_LOCK:
        if not s.get(TrainSession, session_id):
            raise HTTPException(404, "场次不存在")
        state = _runtime_row(session_id, s)
        if state.status != "paused":
            live = _live_row_for_update(s)
            live_is_current = bool(live and _live_session_id(live) == session_id)
            cursor = _json_load(state.cursor_json) or (
                _json_load(live.cursor_json) if live_is_current and live else None)
            if cursor:
                cursor = _safe_cursor(cursor)
                cursor["sessionId"] = session_id
                cursor.pop("session_id", None)
                wseq = (_allocate_live_wseq(live) if live_is_current and live
                        else _allocate_runtime_wseq(state))
                cursor["wseq"] = wseq
                state.cursor_json = _json.dumps(cursor, ensure_ascii=False)
                if live_is_current and live:
                    live.cursor_json = _json.dumps(_pause_projection(cursor, wseq), ensure_ascii=False)

            rapport = _json_load(state.rapport_json) or (
                _json_load(live.rapport_json) if live_is_current and live else None)
            if rapport:
                rapport = _safe_rapport(rapport)
                rapport["sessionId"] = session_id
                rapport.pop("session_id", None)
                wseq = (_allocate_live_wseq(live) if live_is_current and live
                        else _allocate_runtime_wseq(state))
                rapport["wseq"] = wseq
                state.rapport_json = _json.dumps(rapport, ensure_ascii=False)
                if live_is_current and live:
                    projected = dict(rapport)
                    projected.update({"recording": "stopped", "paused": True})
                    live.rapport_json = _json.dumps(projected, ensure_ascii=False)

            if live_is_current and live:
                _set_live_session_paused(live, True)

            state.status = "paused"
            state.paused_at = datetime.now()
            state.revision += 1
            state.updated_at = state.paused_at
            s.add(state)
            if live_is_current and live:
                live.seq += 1
                live.updated_at = datetime.now()
                s.add(live)
            s.commit(); s.refresh(state)
    return _runtime_payload(session_id, state)


@app.post("/sessions/{session_id}/resume")
def resume_session(session_id: str, s: DBSession = Depends(get_session)):
    with _LIVE_WRITE_LOCK:
        sess = s.get(TrainSession, session_id)
        if not sess:
            raise HTTPException(404, "场次不存在")
        state = _runtime_row(session_id, s)
        cursor = _json_load(state.cursor_json)
        if cursor:
            _validate_cursor(sess, cursor, s)  # 题库/版本/位置失配时 fail-closed，不盲恢复。
        rapport = _json_load(state.rapport_json)
        if rapport:
            _validate_rapport(sess, rapport, s)
        if state.status == "paused":
            live = _live_row_for_update(s)
            live_is_current = bool(live and _live_session_id(live) == session_id)
            if cursor:
                cursor = _safe_cursor(cursor)
                cursor["sessionId"] = session_id
                cursor.pop("session_id", None)
                wseq = (_allocate_live_wseq(live) if live_is_current and live
                        else _allocate_runtime_wseq(state))
                cursor["wseq"] = wseq
                state.cursor_json = _json.dumps(cursor, ensure_ascii=False)
                if live_is_current and live:
                    live.cursor_json = state.cursor_json
            if rapport:
                rapport = _safe_rapport(rapport)
                rapport["sessionId"] = session_id
                rapport.pop("session_id", None)
                wseq = (_allocate_live_wseq(live) if live_is_current and live
                        else _allocate_runtime_wseq(state))
                rapport["wseq"] = wseq
                state.rapport_json = _json.dumps(rapport, ensure_ascii=False)
                if live_is_current and live:
                    live.rapport_json = state.rapport_json
            if live_is_current and live:
                _set_live_session_paused(live, False)
            state.status = "active"
            state.resumed_at = datetime.now()
            state.revision += 1
            state.updated_at = state.resumed_at
            s.add(state)
            if live_is_current and live:
                live.seq += 1
                live.updated_at = datetime.now()
                s.add(live)
            s.commit(); s.refresh(state)
    return _runtime_payload(session_id, state)


# ---------------- M3 ASR(可插拔;默认 auto:有 Key 走云端 qwen3-asr,无则降级人工)----------------
@app.get("/asr/hotwords")
def asr_hotwords():
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    script = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
    hw = asr.build_hotwords(bank, script)
    return {"engine": asr.get_engine().version, "count": len(hw), "hotwords": hw}


@app.post("/asr/transcribe/{raw_audio_id}")
def asr_transcribe(raw_audio_id: str, s: DBSession = Depends(get_session)):
    """转写(云端 qwen3-asr 上下文偏置/降级 null)。引擎不可用 → degraded=true、asr_text=null,操作端走人工转写,链路不断。"""
    _load_row(raw_audio_id, s)
    p = audio_store.find_blob(raw_audio_id)
    if not p:
        raise HTTPException(404, "无音频字节,无法转写(先 PUT /audio/{id}/blob)")
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    try:
        script = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
    except Exception:
        script = None       # 脚本文件坏只影响属相热词,转写链路不断
    res = asr.get_engine().transcribe(p.read_bytes(), asr.build_hotwords(bank, script))
    return {"raw_audio_id": raw_audio_id, "asr_text": res.asr_text,
            "asr_confidence": res.asr_confidence, "engine_version": res.engine_version,
            "degraded": res.asr_text is None}


# ---------------- 神经 TTS(小语的声音;云端白名单闭集优先,本地 piper 降级)----------------
@app.get("/tts/speak")
def tts_speak(text: str):
    """合成一句固定话术。GET(读语义,老人端免 PIN);引擎未接/模型缺失 → 204,前端回退系统语音。
    云引擎只合成白名单文本(题库/脚本/固定话术闭集)——患者字段永不出网,见 tts.cloud_text_allowed。"""
    text = text.strip()
    if not text:
        raise HTTPException(422, "text 为空")
    if len(text) > 500:
        raise HTTPException(422, "text 超长(>500 字),话术不应这么长")
    data, version, cached = tts.speak(text)
    if data is None:
        # 204 显式禁缓存:补装模型后老人端刷新要立刻吃到 200,不能被启发式缓存钉死在降级态
        return PlainResponse(status_code=204, headers={"X-Tts-Engine": version, "Cache-Control": "no-store"})
    return PlainResponse(content=data, media_type="audio/wav",
                         headers={"X-Tts-Engine": version, "X-Tts-Cache": "hit" if cached else "miss",
                                  "Cache-Control": "no-store"})


# ---------------- R 会话编排 + 逐环节录音/判分/锁分 ----------------
def _load_bank_for_session(sess: TrainSession) -> content.ItemBank:
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    if sess.item_bank_version_id and bank.version_id != sess.item_bank_version_id:
        raise HTTPException(409, f"场次绑版本 {sess.item_bank_version_id} 与题库 {bank.version_id} 不符")
    return bank


def _find_bank_item(bank: content.ItemBank, item_id: str) -> dict | None:
    for it in list(bank.single_element) + list(bank.double_element) + list(bank.multi_element):
        if it.get("item_id") == item_id:
            return it
    return None


def _role_target(bank_item: dict, response_role: str) -> str | None:
    """该环节的确定式判分目标词；作用/关系/关键要素类无确定式口径 → None（纯人工）。"""
    return {"命名": bank_item.get("target_word"),
            "左命名": bank_item.get("left_word"),
            "右命名": bank_item.get("right_word")}.get(response_role)


@app.get("/sessions/{session_id}/plan")
def session_plan(session_id: str, week_no: int | None = None, event_line: str | None = None,
                 max_items: int | None = None, s: DBSession = Depends(get_session)):
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "场次不存在")
    persisted_event_line = sess.event_line.value if hasattr(sess.event_line, "value") else str(sess.event_line)
    if week_no is not None and week_no != sess.week_no:
        raise HTTPException(409, f"请求 week_no={week_no} 与场次已持久化周次 {sess.week_no} 不符")
    if event_line is not None and event_line != persisted_event_line:
        raise HTTPException(409, f"请求 event_line={event_line!r} 与场次已持久化事件线 {persisted_event_line!r} 不符")
    if max_items is not None and max_items < 0:
        raise HTTPException(422, "max_items 不得小于 0")
    bank = _load_bank_for_session(sess)
    try:
        plan = runtime.build_session_plan(bank, sess.week_no, persisted_event_line, max_items)
    except ValueError as e:
        # 数据约束违反是 422；未结构化/未校对的周次是当前资源状态冲突，fail-closed。
        status = 422 if "1..8" in str(e) else 409
        raise HTTPException(status, str(e))
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


class ClassifyIn(BaseModel):
    """自动驾驶逐轮判类输入:题号+环节角色+该轮 ASR 文本,无画像、无患者字段。"""
    model_config = ConfigDict(extra="forbid")

    item_id: str = PydanticField(min_length=1, max_length=160)
    response_role: str = PydanticField(default="命名", max_length=64)
    text: str | None = PydanticField(default=None, max_length=2000)


@app.post("/judge/classify")
def judge_classify(body: ClassifyIn):
    """自动驾驶逐轮判类(只读):同一环节可听多轮,每轮判一次;**不建 turn、不写库、永不锁分**。
    turn 行只在环节了结时由操作端建一次(带最后一轮音频/转写/prompt_level);
    中间轮音频已按 turnKey 落库,中间轮转写文本 v1 暂不入库(分析可离线重转写)。"""
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    found = _task_type_for_bank_item(bank, body.item_id)
    if not found:
        raise HTTPException(404, f"题库无此题:{body.item_id}")
    task_type, bi = found
    target = _role_target(bi, body.response_role or "命名")
    if not target:
        # 作用/关系/关键要素:无确定式判分口径,自动驾驶按"能答即继续"处理,对错人工事后
        return {"answer_type": None, "ai_score": None, "needs_review": True,
                "judge_mode": "无确定式口径", "contains_target": False}
    # 是否说全了目标词(自动驾驶据此区分:"是胡萝卜"含目标词=说对了→表扬;
    # "萝卜"只是目标词子串→部分正确、不含全词→按不准确升级提示)。含可接受表达也算说对。
    text = body.text or ""
    contains = target in text or any(a and a in text for a in (bi.get("acceptable_expressions") or []))
    ji = judging.build_judge_input(                       # 过画像守卫(混入画像→PortraitLeakError)
        item_id=body.item_id, task_type=task_type, target_word=target,
        acceptable_expressions=tuple(bi.get("acceptable_expressions", []) or []),
        upper_terms=tuple(bi.get("upper_terms", []) or []),
        dialect_synonyms=tuple(bi.get("dialect_synonyms", []) or []),
        asr_text=body.text)
    lj = llm_judge.get_engine().judge(ji)
    if lj is not None:
        return {"answer_type": lj.answer_type.value, "ai_score": lj.ai_score,
                "needs_review": lj.ai_needs_review, "judge_mode": "LLM辅助", "contains_target": contains}
    res = rule_judge.judge_rule_based(ji)
    return {"answer_type": res.answer_type.value if res.answer_type else res.interaction_state,
            "ai_score": res.ai_score, "needs_review": res.ai_needs_review,
            "judge_mode": "规则确定式", "contains_target": contains}


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


def _task_type_for_bank_item(bank: content.ItemBank, item_id: str) -> tuple[str, dict] | None:
    for task_type, rows in (("单要素", bank.single_element),
                            ("双要素", bank.double_element),
                            ("多要素", bank.multi_element)):
        for row in rows:
            if row.get("item_id") == item_id:
                return task_type, row
    return None


def _allowed_lock_values(task_type: str, response_role: str) -> set[float]:
    if task_type == "双要素" and response_role == "关系识别":
        return {0.0, 0.5, 1.0}
    return {0.0, 1.0}


@app.patch("/turns/{turn_id}/lock", response_model=TurnEvent)
def lock_turn(turn_id: int, body: LockIn, s: DBSession = Depends(get_session)):
    """人工锁定评分（研究数据真值）。一旦锁定不得重复锁。"""
    t = _load_turn(turn_id, s)
    if t.score_locked:
        raise HTTPException(409, "该环节已锁分，不可重复锁定")

    if t.confirmed_response_text is None:
        raise HTTPException(409, "须先人工确认 confirmed_response_text 才能锁分")
    reviewer_id = body.reviewer_id.strip()
    if not reviewer_id:
        raise HTTPException(422, "reviewer_id 不得为空")

    ie = s.get(ItemEvent, t.item_event_id)
    sess = s.get(TrainSession, ie.session_id) if ie else None
    if not ie or not sess:
        raise HTTPException(409, "环节缺少可追溯的题目/场次，禁止锁分")
    phase = sess.phase_type.value if hasattr(sess.phase_type, "value") else str(sess.phase_type)
    event = sess.event_line.value if hasattr(sess.event_line, "value") else str(sess.event_line)
    if not 2 <= sess.week_no <= 8 or phase != "正式训练" or event != "正式训练":
        raise HTTPException(409, "仅允许在第2–8周正式训练事件中锁定评分")

    prompt_level = body.prompt_level if body.prompt_level is not None else t.prompt_level
    if prompt_level not in (0, 1, 2, 3):
        raise HTTPException(422, "prompt_level 必须明确且为 0..3")

    bank = _load_bank_for_session(sess)
    if sess.week_no not in bank.supported_training_weeks:
        raise HTTPException(409, f"第{sess.week_no}周不在场次绑定题库的支持范围内，禁止锁分")
    found = _task_type_for_bank_item(bank, ie.item_id)
    if not found:
        raise HTTPException(409, f"题目 {ie.item_id} 不在场次绑定题库中")
    expected_task_type, _bank_item = found
    actual_task_type = ie.task_type.value if hasattr(ie.task_type, "value") else str(ie.task_type)
    if actual_task_type != expected_task_type:
        raise HTTPException(409, "题目事件 task_type 与绑定题库不一致")

    response_role = t.response_role or ""
    if expected_task_type == "单要素":
        valid_role = t.turn_seq == 1 and response_role == "命名"
    elif expected_task_type == "双要素":
        valid_role = (1 <= t.turn_seq <= len(runtime.DOUBLE_ROLES)
                      and response_role == runtime.DOUBLE_ROLES[t.turn_seq - 1])
    else:
        try:
            plan = runtime.build_session_plan(bank, sess.week_no, event)
        except ValueError as e:
            raise HTTPException(409, str(e))
        plan_item = next((item for item in plan.items if item.item_id == ie.item_id), None)
        plan_turn = next((turn for turn in (plan_item.turns if plan_item else ())
                          if turn.turn_seq == t.turn_seq), None)
        valid_role = plan_turn is not None and response_role == plan_turn.response_role
    if not valid_role:
        raise HTTPException(422, "turn_seq/response_role 与题库规定的评分环节不一致")

    allowed = _allowed_lock_values(expected_task_type, response_role)
    reviewed = body.reviewed_score if body.reviewed_score is not None else body.element_value
    if (not math.isfinite(body.element_value) or body.element_value not in allowed
            or not math.isfinite(reviewed) or reviewed not in allowed):
        raise HTTPException(422, f"该环节评分只允许 {sorted(allowed)}")

    t.reviewer_id = reviewer_id
    t.element_value = body.element_value
    t.reviewed_score = reviewed
    t.prompt_level = prompt_level
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
@app.get("/sessions/{session_id}/journal")
def session_journal(session_id: str, s: DBSession = Depends(get_session)):
    """只读场次日志：一次取回场次及其题目、环节、音频元数据和异常记录。"""
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "场次不存在")
    items = list(s.exec(select(ItemEvent)
                        .where(ItemEvent.session_id == session_id)
                        .order_by(ItemEvent.presentation_order, ItemEvent.id)))
    item_ids = [item.id for item in items if item.id is not None]
    turns = []
    if item_ids:
        turns = list(s.exec(select(TurnEvent)
                            .where(TurnEvent.item_event_id.in_(item_ids))
                            .order_by(TurnEvent.item_event_id, TurnEvent.turn_seq, TurnEvent.id)))
    audios = list(s.exec(select(AudioAssetRow)
                         .where(AudioAssetRow.session_id == session_id)
                         .order_by(AudioAssetRow.raw_audio_id)))
    abnormal = list(s.exec(select(AbnormalEvent)
                           .where(AbnormalEvent.session_id == session_id)
                           .order_by(AbnormalEvent.id)))
    return {"session": sess, "items": items, "turns": turns,
            "audios": audios, "abnormal": abnormal}


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
    if not s.get(TrainSession, session_id):
        raise HTTPException(404, "场次不存在")
    try:
        res = export.export_session_bundle(s, session_id, deidentify=deidentify)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, str(e))
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
