"""自动带练占床:平板还在答题时,这张床不能被开场或握手悄悄换走。

两床彩排(2026-11-27 stale2)实证:自动带练期间场次活动时钟 SessionRuntimeState.updated_at
一直不动,生产进程 TZ=Asia/Shanghai 下开场写的 UTC 时间又让新场一开就像放了 8 小时;
同一受试者或同一账号再开场,握手走 protected 分支把正在跑的床换走,原场不暂停、正在录的
回答被掐断。N=1 一样。这里全走产品路径:VisitPlan create/approve/start -> 控制台握手 ->
autopilot/start -> 平板 next / acks / blob / audioSaved。时间用 ShiftedDatetime 整体快进
(写入方和判据一起走),NMU_BEDSIDE_STALE_MINUTES=1。
"""
from __future__ import annotations

import datetime as _dt
import os
import sys

from dataclasses import dataclass, field
from datetime import timedelta
import secrets
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, select

from app import (auth, autopilot_orchestration, autopilot_plan_profiles,
                 content, db, device_capability,
                 provider_readiness, session_admission, visit_plan_service)
from app import main as main_module
from app.main import app
from app.models import (AuditLog, LiveState, Patient, ProviderReadinessProbe,
                        ResearchUser, SessionAutopilotState, SessionRuntimeState)

BANK = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
DEMO_VERSION = autopilot_plan_profiles.WEEK2_SINGLE20_DEMO_VERSION
TODAY = visit_plan_service._research_today()
ACCOUNTS = {"res-a": "researcher", "res-b": "researcher",
            "res-c": "researcher", "admin-x": "admin"}
PATIENTS = ("P-A", "P-B", "P-C", "P-D")
THRESHOLD_S = 60


@dataclass
class World:
    engine: object
    clock: object
    client: dict[str, TestClient]
    device: TestClient
    tablet: dict[str, dict] = field(default_factory=dict, repr=False)  # 临时设备凭据不进失败输出
    seq: dict[str, int] = field(default_factory=dict)
    submitted: list[str] = field(default_factory=list)


def _detail_code(resp) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return detail.get("code")
    return detail if isinstance(detail, str) else None


def _make_world(monkeypatch, tmp_path, clock, slots: str | None) -> World:
    if slots is None:
        monkeypatch.delenv("NMU_BEDSIDE_SLOTS", raising=False)
    else:
        monkeypatch.setenv("NMU_BEDSIDE_SLOTS", slots)
    monkeypatch.setenv("NMU_BEDSIDE_STALE_MINUTES", "1")
    monkeypatch.setenv("ENABLE_AUTOPILOT_P0A_SIMULATION", "1")
    monkeypatch.setenv("ALLOW_SIMULATION_DATA", "1")
    monkeypatch.setattr(visit_plan_service, "_research_today", lambda: TODAY)
    engine = db.make_engine(f"sqlite:///{tmp_path / 'stale.sqlite'}")
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    world = World(engine=engine, clock=clock, client={}, device=TestClient(app))
    monkeypatch.setattr(autopilot_orchestration, "submit",
                        lambda sid, _worker: world.submitted.append(sid) or True)
    readiness = provider_readiness.capture_configuration()
    monkeypatch.setattr(provider_readiness, "capture_configuration",
                        lambda **_kw: readiness)
    password = secrets.token_urlsafe(18)  # 临时号，不落盘、不打印
    now_utc = provider_readiness._utc_now_naive()
    with Session(engine) as s:
        for username, role in ACCOUNTS.items():
            s.add(ResearchUser(
                username=username, display_id=f"ACTOR-{username.upper()}",
                password_hash=auth.hash_password(password), role=role,
                created_at=clock.now()))
        for pid in PATIENTS:
            s.add(Patient(patient_id=pid, is_simulation_subject=True,
                          consent_status="已同意", consent_type="本人同意",
                          mandarin_eligible=True, recording_allowed=True,
                          secondary_use_allowed=True))
        s.add(ProviderReadinessProbe(
            probe_id="prb_stale_experiment",
            schema_version=provider_readiness.SCHEMA_VERSION,
            runtime_contract=provider_readiness.RUNTIME_CONTRACT,
            config_fingerprint=readiness.fingerprint,
            tts_engine_version=readiness.tts_engine_version,
            asr_engine_version=readiness.asr_engine_version,
            llm_engine_version=readiness.llm_engine_version,
            tts_required=True, tts_success=True, asr_required=True, asr_success=True,
            llm_required=False, llm_configured=readiness.llm_configured,
            llm_success=readiness.llm_configured,
            llm_failure_code=(None if readiness.llm_configured
                              else "llm_not_required_not_configured"),
            required_capabilities_ready=True, all_configured_capabilities_ready=True,
            checked_at=now_utc,
            expires_at=now_utc + timedelta(hours=1),
            actor_display_id="ACTOR-RES-A"))
        s.commit()
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    for username in ACCOUNTS:
        c = TestClient(app)
        r = c.post("/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.status_code
        c.headers["X-CSRF-Token"] = c.cookies.get(auth.CSRF_COOKIE_NAME)
        world.client[username] = c
    return world


@pytest.fixture
def world(monkeypatch, tmp_path, clock):
    w = _make_world(monkeypatch, tmp_path, clock, "2")
    yield w
    for c in (w.device, *w.client.values()):
        c.close()
    w.engine.dispose()


@pytest.fixture(params=[None, "2"], ids=["slots=1", "slots=2"])
def any_world(request, monkeypatch, tmp_path, clock):
    w = _make_world(monkeypatch, tmp_path, clock, request.param)
    yield w
    for c in (w.device, *w.client.values()):
        c.close()
    w.engine.dispose()


# ------------------------------------------------------------ product path ---

def start_plan(w: World, who: str, patient_id: str, sitting: int = 1):
    c = w.client[who]
    key = f"{who}-{patient_id}-{sitting}-{secrets.token_hex(3)}"
    created = c.post("/visit-plans", json={
        "patient_id": patient_id, "scheduled_date": TODAY.isoformat(),
        "scheduled_time": "09:30:00", "queue_order": sitting,
        "session_sitting_no": sitting, "week_no": 2, "phase_type": "正式训练",
        "event_line": "正式训练", "idempotency_key": f"create-{key}",
        "autopilot_profile_version_id": DEMO_VERSION})
    assert created.status_code == 200, created.text
    plan = created.json()
    approved = c.post(f"/visit-plans/{plan['plan_id']}/approve", json={
        "idempotency_key": f"approve-{key}", "expected_revision": plan["revision"]})
    assert approved.status_code == 200, approved.text
    return c.post(f"/visit-plans/{plan['plan_id']}/start", json={
        "idempotency_key": f"start-{key}",
        "expected_revision": approved.json()["revision"]})


def handshake(w: World, who: str, sid: str):
    return w.client[who].put("/live/state", json={"kind": "session", "payload": {
        "sessionId": sid, "weekNo": 2, "eventLine": "正式训练", "mode": "task",
        "itemBankVersionId": BANK.version_id}})


def open_autopilot_bed(w: World, who: str, patient_id: str, sitting: int = 1) -> str:
    started = start_plan(w, who, patient_id, sitting)
    assert started.status_code == 200, started.text
    sid = started.json()["session_id"]
    hs = handshake(w, who, sid)
    assert hs.status_code == 200, hs.text
    with Session(w.engine) as s:
        token, _row = device_capability.create_capability(
            s, session_id=sid, device_id=f"tablet-{sid[-10:]}")
        s.commit()
    w.tablet[sid] = {"X-Device-Capability": token}
    w.seq[sid] = 0
    ap = w.client[who].post(f"/sessions/{sid}/autopilot/start", json={
        "idempotency_key": f"ap-{sid}", "expected_revision": 0})
    assert ap.status_code == 200, ap.text
    assert (ap.json()["mode"], ap.json()["status"]) == ("autonomous", "waiting_tts")
    return sid


def _ack(w: World, sid: str, command: dict, ack_type: str, **facts):
    w.seq[sid] += 1
    return w.device.post(
        f"/sessions/{sid}/autopilot/commands/{command['command_key']}/acks",
        headers=w.tablet[sid], json={
            "idempotency_key": f"ack-{sid[-8:]}-{w.seq[sid]}", "ack_type": ack_type,
            "control_generation": command["control_generation"],
            "runner_generation": command["runner_generation"],
            "command_revision": command["command_revision"],
            "device_event_seq": w.seq[sid], **facts})


def tablet_question(w: World, sid: str) -> dict:
    """平板拉到提问并念完：只有 GET next / ack，没有任何 /live/state 写。"""
    nxt = w.device.get(f"/sessions/{sid}/autopilot/next", headers=w.tablet[sid])
    assert nxt.status_code == 200, nxt.text
    tts = nxt.json()
    assert tts["kind"] == "tts"
    started = _ack(w, sid, tts, "tts_started")
    assert started.status_code == 200, started.text
    ended = _ack(w, sid, started.json()["command"], "tts_ended",
                 media_ended=True, media_duration_ms=900)
    assert ended.status_code == 200, ended.text
    record = ended.json()["command"]
    assert record["kind"] == "record"
    return record


def tablet_answer(w: World, sid: str, record: dict) -> None:
    """平板录一段、上传、audioSaved（唯一的 /live/state 写）、收麦回执。"""
    raw = record["payload"]["raw_audio_id"]
    auth_r = w.device.post(
        f"/sessions/{sid}/autopilot/commands/{record['command_key']}/recording-authorization",
        headers=w.tablet[sid])
    assert auth_r.status_code == 200, auth_r.text
    rec_started = _ack(w, sid, record, "record_started", mime_type="audio/webm",
                       sample_rate_hz=48_000, channels=1)
    assert rec_started.status_code == 200, rec_started.text
    up = w.device.put(f"/audio/{raw}/blob",
                      headers={**w.tablet[sid], "content-type": "audio/webm"},
                      content=b"\x1a\x45\xdf\xa3stale-experiment-" + raw.encode())
    assert up.status_code == 200, up.text
    fact = up.json()
    saved = w.device.put("/live/state", headers=w.tablet[sid], json={
        "kind": "audioSaved", "payload": {
            "rawAudioId": raw, "durationSeconds": 1.5, "byteCount": fact["bytes"],
            "checksum": fact["checksum"], "turnKey": record["payload"]["turn_ref"],
            "sessionId": sid, "containsDirectIdentifier": False}})
    assert saved.status_code == 200, saved.text
    stopped = _ack(w, sid, rec_started.json()["command"], "record_stopped",
                   stop_reason="silence", raw_audio_id=raw,
                   receipt_server_seq=saved.json()["audioReceipt"]["serverSeq"],
                   checksum=fact["checksum"], byte_count=fact["bytes"],
                   duration_seconds=1.5)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "processing_attempt"


def heartbeat(w: World, sid: str):
    return w.device.post("/live/patient-heartbeat", headers=w.tablet[sid],
                         json={"session_id": sid, "screen": "record"})


# -------------------------------------------------------------- observing ----

def slot_of(w: World, sid: str) -> int | None:
    with Session(w.engine) as s:
        for row in s.exec(select(LiveState)):
            if session_admission.live_holder_session_id(row) == sid:
                return row.id
    return None


def snap(w: World, sid: str) -> dict:
    with Session(w.engine) as s:
        rt = s.get(SessionRuntimeState, sid)
        ap = s.get(SessionAutopilotState, sid)
        slot = slot_of(w, sid)
        live = s.get(LiveState, slot) if slot else None
        return {
            "rt_status": rt.status, "rt_rev": rt.revision, "rt_at": rt.updated_at,
            "ap": (ap.mode, ap.status) if ap else None,
            "ap_at": ap.updated_at if ap else None,
            "slot": slot, "live_at": live.updated_at if live else None,
            "rt_stale": session_admission.runtime_stale(rt),
            "live_stale": session_admission.live_slot_stale(live) if live else None,
        }


def audits(w: World) -> list[str]:
    with Session(w.engine) as s:
        return [row.action for row in s.exec(select(AuditLog).order_by(AuditLog.id))]


def _two_running_beds(w: World, *, a_answer_at: int, b_answer_at: int | None,
                      end_at: int) -> tuple[str, str | None]:
    """A（1 号床）、B（2 号床，可省）各开一场自动带练；之后只有平板在动。

    时间轴（秒，相对开场）：问题在 +20 念完；答案在 a_answer_at / b_answer_at 上传；
    最后停在 end_at。全程没有任何控制台请求。
    """
    sa = open_autopilot_bed(w, "res-a", "P-A")
    sb = open_autopilot_bed(w, "res-b", "P-B") if b_answer_at is not None else None
    assert slot_of(w, sa) == 1 and (sb is None or slot_of(w, sb) == 2)
    t = 0
    w.clock.advance(20)
    t = 20
    rec = {sa: tablet_question(w, sa)}
    if sb:
        rec[sb] = tablet_question(w, sb)
    for sid, at in sorted(((sa, a_answer_at), (sb, b_answer_at)),
                          key=lambda p: p[1] if p[1] is not None else 1e9):
        if sid is None:
            continue
        w.clock.advance(at - t)
        t = at
        tablet_answer(w, sid, rec[sid])
        assert heartbeat(w, sid).status_code == 200
    w.clock.advance(end_at - t)
    return sa, sb



# ---------------------------------------------------------------- clock ------
_REAL = _dt.datetime


class _ShiftMeta(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, _REAL)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _REAL)


class ShiftedDatetime(_REAL, metaclass=_ShiftMeta):
    """datetime whose now()/utcnow() run ``offset`` ahead of the wall clock.

    Swapped into every app.* module that bound ``datetime`` at import, so all
    writers (main, visit_plan_service, autopilot_*, device_capability,
    provider_readiness ...) and the staleness judges (session_admission) move
    together. Instances stay real datetimes.
    """
    offset = _dt.timedelta(0)

    @classmethod
    def now(cls, tz=None):
        return _REAL.now(tz) + cls.offset

    @classmethod
    def utcnow(cls):
        return _REAL.utcnow() + cls.offset


class Clock:
    def advance(self, seconds: float) -> None:
        ShiftedDatetime.offset += _dt.timedelta(seconds=seconds)

    def now(self) -> _dt.datetime:
        return ShiftedDatetime.now()


@pytest.fixture
def clock(monkeypatch):
    import app.main  # noqa: F401  (load every app module first)
    patched = []
    for name, module in list(sys.modules.items()):
        if (name == "app" or name.startswith("app.")) and \
                getattr(module, "datetime", None) is _REAL:
            monkeypatch.setattr(module, "datetime", ShiftedDatetime)
            patched.append(name)
    assert {"app.main", "app.session_admission", "app.visit_plan_service",
            "app.autopilot_service", "app.device_capability"} <= set(patched), patched
    ShiftedDatetime.offset = _dt.timedelta(0)
    yield Clock()
    ShiftedDatetime.offset = _dt.timedelta(0)




@pytest.fixture
def shanghai_process():
    """生产进程的时区(/proc/<nmu>/environ 里是 TZ=Asia/Shanghai,主机本身是 UTC)。"""
    before = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    yield
    if before is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = before
    time.tzset()


# ============================================== 1. 场次活动时钟要跟着自动带练走 ====

@pytest.mark.parametrize("slots", [None, "2"], ids=["slots=1", "slots=2"])
def test_brand_new_session_is_fresh_in_the_production_timezone(
        monkeypatch, tmp_path, clock, shanghai_process, slots):
    w = _make_world(monkeypatch, tmp_path, clock, slots)
    first = start_plan(w, "res-a", "P-A")
    assert first.status_code == 200, first.text
    sid = first.json()["session_id"]
    assert snap(w, sid)["rt_stale"] is False
    again = start_plan(w, "res-a", "P-D")
    assert (again.status_code, _detail_code(again)) == (409, "visit_plan_actor_session_open")


def test_tablet_acks_keep_a_running_autopilot_fresh(any_world):
    w = any_world
    sa, _ = _two_running_beds(w, a_answer_at=80, b_answer_at=None, end_at=85)
    before = snap(w, sa)
    assert before["rt_stale"] is False and before["rt_rev"] == 0   # 续鲜不动 revision
    same_subject = start_plan(w, "admin-x", "P-A", sitting=2)
    assert (same_subject.status_code, _detail_code(same_subject)) == (
        409, "visit_plan_patient_session_open")
    same_account = start_plan(w, "res-a", "P-D")
    assert (same_account.status_code, _detail_code(same_account)) == (
        409, "visit_plan_actor_session_open")
    after = snap(w, sa)
    assert after["slot"] == 1 and after["ap"] == ("autonomous", "processing_attempt")


def test_a_bed_whose_tablet_went_silent_is_still_paused_and_released(world, capsys):
    w = world
    sa, sb = _two_running_beds(w, a_answer_at=20, b_answer_at=80, end_at=85)
    assert snap(w, sa)["rt_stale"] is True and snap(w, sa)["live_stale"] is True
    started = start_plan(w, "res-c", "P-C")
    assert started.status_code == 200, started.text
    sc = started.json()["session_id"]
    capsys.readouterr()
    t0 = time.monotonic()
    hs = handshake(w, "res-c", sc)
    elapsed = time.monotonic() - t0
    ops = capsys.readouterr().out
    assert hs.status_code == 200, hs.text
    a = snap(w, sa)
    assert slot_of(w, sc) == 1 and a["slot"] is None
    assert a["rt_status"] == "paused" and a["ap"] == ("autonomous", "paused")
    assert snap(w, sb)["slot"] == 2 and snap(w, sb)["rt_status"] == "active"
    # 自动带练的 fence 在握手事务里先写了库;审计要等提交后再记,否则独立会话等满
    # busy_timeout 才丢掉审计,期间两张床的 live 写都在排队。
    assert "audit_append_failed" not in ops and elapsed < 3
    assert audits(w).count("bedside_slot_stale_takeover") == 1


# ======================================= 2. 还在跑的自动带练不能被握手换走 ====

def test_rehandshake_of_an_older_session_cannot_displace_a_running_autopilot(any_world):
    """恢复入口路径不经准入:A 先给 P-D 开场、暂停、离开过阈值,再给 P-A 开自动带练,
    自动带练进行中又从 P-D 的恢复入口重新握手。"""
    w = any_world
    old = start_plan(w, "res-a", "P-D")
    assert old.status_code == 200, old.text
    sd = old.json()["session_id"]
    assert handshake(w, "res-a", sd).status_code == 200
    assert w.client["res-a"].post(f"/sessions/{sd}/pause").status_code == 200
    w.clock.advance(THRESHOLD_S + 10)
    sa = open_autopilot_bed(w, "res-a", "P-A")
    w.clock.advance(20)
    tablet_answer(w, sa, tablet_question(w, sa))
    hs = handshake(w, "res-a", sd)
    assert (hs.status_code, _detail_code(hs)) == (409, "bedside_autopilot_running")
    a = snap(w, sa)
    assert a["slot"] == 1 and a["rt_status"] == "active"
    assert a["ap"] == ("autonomous", "processing_attempt")
    assert slot_of(w, sd) is None
    nxt = w.device.get(f"/sessions/{sa}/autopilot/next", headers=w.tablet[sa])
    assert nxt.status_code == 200, nxt.text


def test_paused_autopilot_can_still_be_switched_away(any_world):
    """先暂停再换,这条路照旧能走:拒的只是「还在跑」。"""
    w = any_world
    old = start_plan(w, "res-a", "P-D")
    sd = old.json()["session_id"]
    assert handshake(w, "res-a", sd).status_code == 200
    assert w.client["res-a"].post(f"/sessions/{sd}/pause").status_code == 200
    w.clock.advance(THRESHOLD_S + 10)
    sa = open_autopilot_bed(w, "res-a", "P-A")
    w.clock.advance(20)
    tablet_answer(w, sa, tablet_question(w, sa))
    assert w.client["res-a"].post(f"/sessions/{sa}/pause").status_code == 200
    hs = handshake(w, "res-a", sd)
    assert hs.status_code == 200, hs.text
    assert slot_of(w, sd) == 1 and snap(w, sa)["slot"] is None


def test_admin_cannot_open_another_subject_onto_the_single_running_bed(any_world):
    """单床时管理员开别的受试者,开场一步就拒,不留没有床位的场次。两床时去 2 号床。"""
    w = any_world
    sa, _ = _two_running_beds(w, a_answer_at=80, b_answer_at=None, end_at=85)
    started = start_plan(w, "admin-x", "P-C")
    if session_admission.bedside_slot_count() == 2:
        assert started.status_code == 200, started.text
        sc = started.json()["session_id"]
        assert handshake(w, "admin-x", sc).status_code == 200
        assert slot_of(w, sc) == 2 and snap(w, sa)["slot"] == 1
        return
    assert (started.status_code, _detail_code(started)) == (409, "bedside_autopilot_running")
    with Session(w.engine) as s:
        assert s.exec(select(main_module.TrainSession).where(
            main_module.TrainSession.patient_id == "P-C")).first() is None
    a = snap(w, sa)
    assert a["slot"] == 1 and a["ap"] == ("autonomous", "processing_attempt")


# ============================================= 3. 两床满员时开场一步就拒 ====

def test_full_house_refuses_the_start_before_creating_a_session(world):
    w = world
    sa, sb = _two_running_beds(w, a_answer_at=80, b_answer_at=80, end_at=85)
    before = (snap(w, sa), snap(w, sb))
    started = start_plan(w, "res-c", "P-C")
    assert (started.status_code, _detail_code(started)) == (409, "bedside_slots_full")
    assert "两个床位都在使用中" in started.json()["detail"]["message"]
    with Session(w.engine) as s:
        assert s.exec(select(main_module.TrainSession).where(
            main_module.TrainSession.patient_id == "P-C")).first() is None
    plans = w.client["res-c"].get("/visit-plans", params={"patient_id": "P-C"})
    assert plans.status_code == 200, plans.text
    assert [p["status"] for p in plans.json()] == ["approved"]
    assert (snap(w, sa), snap(w, sb)) == before


# ===================================== 4. 管理员读控制台快照只读本场所在的床 ====

def test_admin_console_state_never_falls_back_to_the_other_bed(world):
    w = world
    sa, _ = _two_running_beds(w, a_answer_at=80, b_answer_at=None, end_at=85)
    started = start_plan(w, "admin-x", "P-C")
    assert started.status_code == 200, started.text
    sc = started.json()["session_id"]                      # 还没握手,不在任何床
    admin = w.client["admin-x"]
    for params in ({"session_id": sc}, {}):
        r = admin.get("/live/console-state", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["session"] is None and body["cursor"] is None
        assert body["audioSaved"] is None and body["patientRec"] is None
        assert sa not in r.text
    own = admin.get("/live/console-state", params={"session_id": sa})
    assert own.status_code == 200 and own.json()["session"]["sessionId"] == sa
