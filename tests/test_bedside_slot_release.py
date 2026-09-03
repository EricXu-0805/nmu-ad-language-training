"""床旁槽的释放规则(2026-09-03 生产实证的雷 + 四路对抗复核处置):

- 遗弃只看控制台侧 live 写(updated_at),**不含老人端被动心跳**——两设备部署里
  平板固定床旁一直心跳,研究者关的是笔记本控制台(复核 P1)。
- 账号/受试者准入闸与握手层口径一致:遗弃/被接管的场不超前拦死(复核 P2,共享账号)。
- 终态直接让位、遗弃安全暂停让位、新鲜维持保护;两条让位都留审计(复核 P2b)。
- stale 阈值 0/负数=关闭(永不自动让位),非数字落回 15(复核 P3)。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import auth, db
from app.main import app
from app.models import (
    AuditLog, LiveState, ResearchUser, SessionRuntimeState,
)


@pytest.fixture
def slot_client(monkeypatch):
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
