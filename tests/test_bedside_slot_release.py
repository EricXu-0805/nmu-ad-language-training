"""床旁槽的释放规则(2026-09-03 生产实证的雷 + 四路对抗复核处置):

- 遗弃只看控制台侧 live 写(updated_at),**不含老人端被动心跳**——两设备部署里
  平板固定床旁一直心跳,研究者关的是笔记本控制台(复核 P1)。
- 账号/受试者准入闸与握手层口径一致:遗弃/被接管的场不超前拦死(复核 P2,共享账号)。
- 终态直接让位、遗弃安全暂停让位、新鲜维持保护;两条让位都留审计(复核 P2b)。
- stale 阈值 0/负数=关闭(永不自动让位),非数字落回 15(复核 P3)。
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import auth, db
from app.main import app
from app.models import (
    AuditLog, LiveState, ResearchUser, SessionRuntimeState,
)
from test_bedside_two_slot_gates import _raw_slot


@pytest.fixture
def slot_client(monkeypatch, request, tmp_path):
    if getattr(request, "param", "memory") == "file":
        # 与生产同一个引擎工厂与 pragma(busy_timeout=5000):StaticPool 内存库
        # 所有会话共用一条连接,看不出「事务内另开会话写审计」会不会被自己的写锁挡住。
        eng = db.make_engine(f"sqlite:///{tmp_path / 'slot.sqlite'}")
    else:
        eng = create_engine(
            "sqlite://", connect_args={"check_same_thread": False},
            poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", eng)
    SQLModel.metadata.create_all(eng)
    client = TestClient(app)
    client.test_engine = eng
    yield client
    client.close()


def _login(engine, username: str, role: str = "researcher") -> TestClient:
    with Session(engine) as session:
        session.add(ResearchUser(
            username=username,
            display_id=auth.validate_display_id(username),
            password_hash=auth.hash_password("password1"),
            role=role,
            created_at=datetime.now(),
        ))
        session.commit()
    client = TestClient(app)
    login = client.post("/auth/login", json={
        "username": username, "password": "password1"})
    assert login.status_code == 200, login.text
    client.headers["X-CSRF-Token"] = client.cookies.get(auth.CSRF_COOKIE_NAME)
    return client


def _seed_session(client: TestClient, *, session_id: str, patient_id: str,
                  trainer: str) -> None:
    if client.get(f"/patients/{patient_id}").status_code != 200:
        assert client.post("/patients", json={
            "patient_id": patient_id, "is_simulation_subject": True,
        }).status_code == 200
    response = client.post("/sessions", json={
        "session_id": session_id, "patient_id": patient_id,
        "week_no": 1, "phase_type": "关系建立", "event_line": "关系建立环节",
        "item_bank_version_id": "wk2-v1-20260707", "is_simulation": True,
        "trainer_id": trainer,
    })
    assert response.status_code == 200, response.text


def _claim(client: TestClient, session_id: str):
    return client.put("/live/state", json={
        "kind": "session",
        "payload": {
            "sessionId": session_id, "weekNo": 1,
            "eventLine": "关系建立环节", "mode": "rapport",
            "itemBankVersionId": "wk2-v1-20260707",
        },
    })


def _set_runtime(engine, session_id: str, status: str) -> None:
    with Session(engine) as s:
        row = s.get(SessionRuntimeState, session_id)
        if row is None:
            row = SessionRuntimeState(session_id=session_id)
        row.status = status
        s.add(row)
        s.commit()


def _age_console(engine, minutes: int, *, patient_fresh: bool = False) -> None:
    """把控制台侧 live 写调旧 minutes 分钟。

    patient_fresh=True 时保留老人端心跳为当前时刻——复现两设备部署里
    "平板还在床旁心跳、研究者笔记本已关"的场景,验证它仍被判遗弃。
    """
    with Session(engine) as s:
        row = s.exec(select(LiveState)).one()
        row.updated_at = datetime.now() - timedelta(minutes=minutes)
        row.patient_last_seen_at = datetime.now() if patient_fresh else None
        s.add(row)
        s.commit()


@pytest.fixture
def occupied(slot_client):
    """other 占住槽(S-PREV 活跃),mine 想开 S-NEXT。"""
    eng = slot_client.test_engine
    _seed_session(slot_client, session_id="S-PREV", patient_id="P-PREV",
                  trainer="other")
    _seed_session(slot_client, session_id="S-NEXT", patient_id="P-NEXT",
                  trainer="mine")
    other = _login(eng, "other")
    mine = _login(eng, "mine")
    assert _claim(other, "S-PREV").status_code == 200, "占槽失败"
    yield slot_client, other, mine


def test_fresh_active_previous_still_guards_the_slot(occupied):
    client, other, mine = occupied
    denied = _claim(mine, "S-NEXT")
    assert denied.status_code in (403, 404, 409), denied.text
    with Session(client.test_engine) as s:
        row = s.get(SessionRuntimeState, "S-PREV")
    assert row is None or row.status == "active"


def test_terminal_previous_releases_with_audit(occupied):
    client, other, mine = occupied
    _set_runtime(client.test_engine, "S-PREV", "aborted")
    granted = _claim(mine, "S-NEXT")
    assert granted.status_code == 200, granted.text
    with Session(client.test_engine) as s:
        actions = [r.action for r in s.exec(select(AuditLog))]
    assert "bedside_slot_terminal_takeover" in actions


def test_stale_active_is_safety_paused_and_released_with_audit(occupied):
    client, other, mine = occupied
    _age_console(client.test_engine, minutes=16)
    granted = _claim(mine, "S-NEXT")
    assert granted.status_code == 200, granted.text
    with Session(client.test_engine) as s:
        assert s.get(SessionRuntimeState, "S-PREV").status == "paused"
        actions = [r.action for r in s.exec(select(AuditLog))]
    assert "bedside_slot_stale_takeover" in actions


@pytest.mark.parametrize("slot_client", ["file"], indirect=True)
@pytest.mark.parametrize("prev_status", ["active", "aborted"])
def test_takeover_audit_lands_promptly_on_a_file_backed_db(occupied, capsys, prev_status):
    """让位审计在握手事务里另开会话写:文件库上若被本请求自己的写锁挡住,会卡满
    busy_timeout(5 秒)再丢审计,只剩一行 audit_append_failed。"""
    client, other, mine = occupied
    if prev_status == "active":
        _age_console(client.test_engine, minutes=16)
    else:
        _set_runtime(client.test_engine, "S-PREV", prev_status)
    capsys.readouterr()
    started = time.monotonic()
    granted = _claim(mine, "S-NEXT")
    elapsed = time.monotonic() - started
    assert granted.status_code == 200, granted.text
    # 界放在 busy_timeout 之下但留足并行负载余量;真正判红的是下面两条确定性断言。
    assert elapsed < 4, f"握手用了 {elapsed:.1f} 秒"
    assert "audit_append_failed" not in capsys.readouterr().out
    with Session(client.test_engine) as s:
        expected_status = "paused" if prev_status == "active" else prev_status
        assert s.get(SessionRuntimeState, "S-PREV").status == expected_status
        actions = [r.action for r in s.exec(select(AuditLog))]
    expected = ("bedside_slot_stale_takeover" if prev_status == "active"
                else "bedside_slot_terminal_takeover")
    assert actions.count(expected) == 1


def test_patient_heartbeat_fresh_does_not_keep_abandoned_slot(occupied):
    """复核 P1:平板还在心跳但控制台已关 → 仍判遗弃、仍让位。"""
    client, other, mine = occupied
    _age_console(client.test_engine, minutes=16, patient_fresh=True)
    granted = _claim(mine, "S-NEXT")
    assert granted.status_code == 200, granted.text
    with Session(client.test_engine) as s:
        assert s.get(SessionRuntimeState, "S-PREV").status == "paused"


def test_stale_paused_releases_without_repausing(occupied):
    client, other, mine = occupied
    _set_runtime(client.test_engine, "S-PREV", "paused")
    _age_console(client.test_engine, minutes=16)
    granted = _claim(mine, "S-NEXT")
    assert granted.status_code == 200, granted.text
    with Session(client.test_engine) as s:
        assert s.get(SessionRuntimeState, "S-PREV").status == "paused"


def test_fresh_paused_still_guards_the_slot(occupied):
    client, other, mine = occupied
    _set_runtime(client.test_engine, "S-PREV", "paused")
    denied = _claim(mine, "S-NEXT")
    assert denied.status_code in (403, 404, 409), denied.text


def test_fresh_intervention_completed_still_guards_the_slot(occupied):
    client, other, mine = occupied
    _set_runtime(client.test_engine, "S-PREV", "intervention_completed")
    denied = _claim(mine, "S-NEXT")
    assert denied.status_code in (403, 404, 409), denied.text


def test_stale_intervention_completed_releases_without_pausing(occupied):
    client, other, mine = occupied
    _set_runtime(client.test_engine, "S-PREV", "intervention_completed")
    _age_console(client.test_engine, minutes=16)
    granted = _claim(mine, "S-NEXT")
    assert granted.status_code == 200, granted.text
    with Session(client.test_engine) as s:
        assert s.get(
            SessionRuntimeState, "S-PREV").status == "intervention_completed"


def test_disabled_threshold_never_releases(occupied, monkeypatch):
    client, other, mine = occupied
    monkeypatch.setenv("NMU_BEDSIDE_STALE_MINUTES", "0")
    _age_console(client.test_engine, minutes=999)
    denied = _claim(mine, "S-NEXT")
    assert denied.status_code in (403, 404, 409), denied.text


def test_negative_threshold_disabled_like_zero(occupied, monkeypatch):
    client, other, mine = occupied
    monkeypatch.setenv("NMU_BEDSIDE_STALE_MINUTES", "-5")
    _age_console(client.test_engine, minutes=999)
    denied = _claim(mine, "S-NEXT")
    assert denied.status_code in (403, 404, 409), denied.text


def test_garbage_threshold_falls_back_to_15(occupied, monkeypatch):
    client, other, mine = occupied
    monkeypatch.setenv("NMU_BEDSIDE_STALE_MINUTES", "not-a-number")
    _age_console(client.test_engine, minutes=16)
    granted = _claim(mine, "S-NEXT")
    assert granted.status_code == 200, granted.text


def test_configurable_threshold(occupied, monkeypatch):
    client, other, mine = occupied
    monkeypatch.setenv("NMU_BEDSIDE_STALE_MINUTES", "30")
    _age_console(client.test_engine, minutes=16)
    assert _claim(mine, "S-NEXT").status_code in (403, 404, 409)
    monkeypatch.setenv("NMU_BEDSIDE_STALE_MINUTES", "10")
    assert _claim(mine, "S-NEXT").status_code == 200


# ── 账号准入闸的 stale 豁免(复核 P2:共享账号是医院真实用法) ──────────────

def _seed_plan(client: TestClient, *, patient_id: str, actor: str) -> str:
    """建一个已审核的第1周计划,返回 plan_id;走服务层不绕门禁。"""
    from app import visit_plan_service
    from app.visit_plan_contract import VisitPlanCreateIn, VisitPlanMutationIn
    import hashlib
    with Session(client.test_engine) as s:
        suffix = hashlib.sha256(patient_id.encode()).hexdigest()[:12]
        r = visit_plan_service.create_plan(s, body=VisitPlanCreateIn(
            idempotency_key=f"c-{suffix}", patient_id=patient_id,
            scheduled_date=visit_plan_service._research_today(),
            week_no=1, phase_type="关系建立", event_line="关系建立环节"),
            actor_id=actor)
        r = visit_plan_service.approve_plan(s, plan_id=r.plan_id,
            body=VisitPlanMutationIn(idempotency_key=f"a-{suffix}",
                                     expected_revision=r.revision),
            actor_id=actor)
        s.commit()
        return r.plan_id


def test_actor_gate_blocks_on_fresh_own_session(slot_client):
    """共享账号:上一场仍新鲜时,同账号开新场仍被账号闸挡。"""
    from app import visit_plan_service
    eng = slot_client.test_engine
    _seed_session(slot_client, session_id="S-A", patient_id="P-A",
                  trainer="shared")
    shared = _login(eng, "shared", role="admin")
    assert _claim(shared, "S-A").status_code == 200
    with Session(eng) as s:
        with pytest.raises(Exception):
            visit_plan_service.assert_actor_ready_for_new_work(s, "shared")


def test_actor_gate_exempts_stale_own_session(slot_client):
    """复核 P2:共享账号关页签走人留下弃场,同账号开新场不被账号闸超前挡死。"""
    from app import visit_plan_service
    eng = slot_client.test_engine
    _seed_session(slot_client, session_id="S-A", patient_id="P-A",
                  trainer="shared")
    shared = _login(eng, "shared", role="admin")
    assert _claim(shared, "S-A").status_code == 200
    _age_console(eng, minutes=16, patient_fresh=True)
    with Session(eng) as s:
        # 不抛=放行,交给握手层接管。
        visit_plan_service.assert_actor_ready_for_new_work(s, "shared")


def _set_runtime_updated(engine, session_id: str, status: str, minutes_ago: int):
    from datetime import datetime, timedelta
    with Session(engine) as s:
        row = s.get(SessionRuntimeState, session_id) or SessionRuntimeState(
            session_id=session_id)
        row.status = status
        row.updated_at = datetime.now() - timedelta(minutes=minutes_ago)
        s.add(row)
        s.commit()


def test_actor_gate_blocks_on_fresh_runtime_clock(slot_client):
    """有 runtime 活动时钟且新鲜(刚开的场)→ 账号闸拦。"""
    from app import visit_plan_service
    eng = slot_client.test_engine
    _seed_session(slot_client, session_id="S-RT", patient_id="P-RT",
                  trainer="shared")
    _set_runtime_updated(eng, "S-RT", "active", minutes_ago=0)
    with Session(eng) as s:
        with pytest.raises(Exception):
            visit_plan_service.assert_actor_ready_for_new_work(s, "shared")


def test_actor_gate_exempts_stale_runtime_clock(slot_client):
    """runtime 活动时钟放置超时(关页签走人)→ 账号闸放行,交握手层接管。"""
    from app import visit_plan_service
    eng = slot_client.test_engine
    _seed_session(slot_client, session_id="S-RT", patient_id="P-RT",
                  trainer="shared")
    _set_runtime_updated(eng, "S-RT", "active", minutes_ago=16)
    with Session(eng) as s:
        visit_plan_service.assert_actor_ready_for_new_work(s, "shared")


# ── 两床孪生(NMU_BEDSIDE_SLOTS=2) ──────────────────────────────────────────
# 上面「新鲜槽挡住另一个账号」「遗弃槽被接管」两类用例只在单槽下成立。两槽时选槽先取空槽
# (规则 4 在遗弃槽之前),另一个账号进 2 号槽,1 号槽不让位、不暂停、不留让位审计;
# 两槽都被占了,今天的让位规则才原样作用在 1 号槽上。

_TAKEOVER_ACTIONS = ("bedside_slot_stale_takeover", "bedside_slot_terminal_takeover")


def _holder(engine, slot_id: int) -> str | None:
    with Session(engine) as s:
        row = s.get(LiveState, slot_id)
        return json.loads(row.session_json or "{}").get("sessionId") if row else None


def _age_slot(engine, slot_id: int, minutes: int, *, patient_fresh: bool = False) -> None:
    with Session(engine) as s:
        row = s.get(LiveState, slot_id)
        row.updated_at = datetime.now() - timedelta(minutes=minutes)
        row.patient_last_seen_at = datetime.now() if patient_fresh else None
        s.add(row)
        s.commit()


def _actions(engine) -> list[str]:
    with Session(engine) as s:
        return [r.action for r in s.exec(select(AuditLog))]


@pytest.fixture
def two_slots_occupied(slot_client, monkeypatch):
    """开关设为 2 之后 other 才占槽;mine 想开 S-NEXT,third 备用去占 2 号槽。"""
    monkeypatch.setenv("NMU_BEDSIDE_SLOTS", "2")
    eng = slot_client.test_engine
    for session_id, patient_id, trainer in (
            ("S-PREV", "P-PREV", "other"), ("S-NEXT", "P-NEXT", "mine"),
            ("S-THIRD", "P-THIRD", "third")):
        _seed_session(slot_client, session_id=session_id, patient_id=patient_id,
                      trainer=trainer)
    other, mine, third = (_login(eng, name) for name in ("other", "mine", "third"))
    assert _claim(other, "S-PREV").status_code == 200, "占槽失败"
    assert _holder(eng, 1) == "S-PREV"
    yield slot_client, other, mine, third


# 每组对应上面一条单槽用例,注释写的是它的名字(去掉 test_ 前缀)。
@pytest.mark.parametrize("prev_status, minutes, patient_fresh, threshold", [
    (None, 0, False, None),                       # fresh_active_previous_still_guards_the_slot
    ("paused", 0, False, None),                   # fresh_paused_still_guards_the_slot
    ("intervention_completed", 0, False, None),   # fresh_intervention_completed_still_guards…
    (None, 16, False, None),                      # stale_active_is_safety_paused…, takeover_audit…
    (None, 16, True, None),                       # patient_heartbeat_fresh_does_not_keep…
    ("paused", 16, False, None),                  # stale_paused_releases_without_repausing
    ("intervention_completed", 16, False, None),  # stale_intervention_completed_releases…
    (None, 999, False, "0"),                      # disabled_threshold_never_releases
    (None, 999, False, "-5"),                     # negative_threshold_disabled_like_zero
    (None, 16, False, "30"),                      # configurable_threshold
    (None, 16, False, "not-a-number"),            # garbage_threshold_falls_back_to_15
])
def test_two_slots_other_account_takes_the_free_slot_without_touching_slot_one(
        two_slots_occupied, monkeypatch, prev_status, minutes, patient_fresh, threshold):
    client, other, mine, third = two_slots_occupied
    eng = client.test_engine
    if threshold is not None:
        monkeypatch.setenv("NMU_BEDSIDE_STALE_MINUTES", threshold)
    if prev_status is not None:
        _set_runtime(eng, "S-PREV", prev_status)
    if minutes:
        _age_slot(eng, 1, minutes, patient_fresh=patient_fresh)
    slot_one = _raw_slot(eng, 1)

    granted = _claim(mine, "S-NEXT")

    assert granted.status_code == 200, granted.text
    assert _raw_slot(eng, 1) == slot_one
    assert _holder(eng, 2) == "S-NEXT"
    with Session(eng) as s:
        assert s.get(LiveState, 2).seq == s.get(LiveState, 1).seq + 1
        prev = s.get(SessionRuntimeState, "S-PREV")
    assert (prev.status if prev is not None else None) == prev_status
    assert not set(_TAKEOVER_ACTIONS) & set(_actions(eng))


@pytest.mark.parametrize("slot_client", ["memory", "file"], indirect=True)
@pytest.mark.parametrize("case", [
    "fresh", "fresh_paused", "stale", "stale_patient_heartbeat", "stale_paused",
    "stale_intervention_completed", "stale_threshold_disabled", "terminal"])
def test_two_slots_full_house_keeps_todays_release_rules_on_slot_one(
        two_slots_occupied, monkeypatch, capsys, case):
    client, other, mine, third = two_slots_occupied
    eng = client.test_engine
    assert _claim(third, "S-THIRD").status_code == 200
    assert _holder(eng, 2) == "S-THIRD"
    prev_status = {"fresh_paused": "paused", "stale_paused": "paused",
                   "stale_intervention_completed": "intervention_completed",
                   "terminal": "aborted"}.get(case)
    if prev_status is not None:
        _set_runtime(eng, "S-PREV", prev_status)
    if case.startswith("stale"):
        _age_slot(eng, 1, 999 if case == "stale_threshold_disabled" else 16,
                  patient_fresh=case == "stale_patient_heartbeat")
    if case == "stale_threshold_disabled":
        monkeypatch.setenv("NMU_BEDSIDE_STALE_MINUTES", "0")
    slot_two = _raw_slot(eng, 2)
    capsys.readouterr()

    started = time.monotonic()
    response = _claim(mine, "S-NEXT")
    elapsed = time.monotonic() - started

    assert _raw_slot(eng, 2) == slot_two
    with Session(eng) as s:
        prev = s.get(SessionRuntimeState, "S-PREV")
    if case in ("fresh", "fresh_paused", "stale_threshold_disabled"):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "bedside_slots_full"
        assert _holder(eng, 1) == "S-PREV"
        assert (prev.status if prev is not None else None) == prev_status
        assert not set(_TAKEOVER_ACTIONS) & set(_actions(eng))
        return
    assert response.status_code == 200, response.text
    assert elapsed < 4, f"握手用了 {elapsed:.1f} 秒"
    assert "audit_append_failed" not in capsys.readouterr().out
    assert _holder(eng, 1) == "S-NEXT"
    expected_status = {"stale": "paused", "stale_patient_heartbeat": "paused"}.get(
        case, prev_status)
    assert prev.status == expected_status
    expected_action = ("bedside_slot_terminal_takeover" if case == "terminal"
                       else "bedside_slot_stale_takeover")
    assert _actions(eng).count(expected_action) == 1
