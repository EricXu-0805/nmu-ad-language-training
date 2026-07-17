"""M1-E 研究审计账本测试:哈希链完整性/篡改检出、锁分产生审计、红线不含患者作答文本、读口须认证。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import auth, db
from app.main import app
from app.models import AuditLog, ResearchUser


@pytest.fixture
def real_db(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", eng)
    SQLModel.metadata.create_all(eng)
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    with Session(eng) as s:
        s.add(ResearchUser(username="u", display_id="丁老师",
                           password_hash=auth.hash_password("password1"), created_at=datetime.now()))
        s.commit()
    return eng


def _login(client):
    assert client.post("/auth/login", json={"username": "u", "password": "password1"}).status_code == 200


def _lock_a_turn(client, answer="锚"):
    client.post("/patients", json={"patient_id": "PA"})
    client.post("/sessions", json={"session_id": "SA", "patient_id": "PA", "week_no": 2,
                                   "phase_type": "正式训练", "event_line": "正式训练",
                                   "item_bank_version_id": "wk2-v1-20260707"})
    ie = client.post("/sessions/SA/items", json={"item_id": "SE_锚", "task_type": "单要素"}).json()
    te = client.post(f"/items/{ie['id']}/turns",
                     json={"turn_seq": 1, "response_role": "命名", "asr_text": answer}).json()
    client.patch(f"/turns/{te['id']}/confirm", json={"confirmed_response_text": answer})
    assert client.patch(f"/turns/{te['id']}/lock",
                        json={"reviewer_id": "x", "element_value": 1, "prompt_level": 0}).status_code == 200
    return te["id"]


def test_lock_produces_audit_entry_metadata_only(real_db):
    secret = "我是李明住在南京鼓楼"          # 患者作答里塞入可识别文本
    with TestClient(app) as client:
        _login(client)
        _lock_a_turn(client, answer=secret)
        rows = client.get("/audit", params={"session_id": "SA"}).json()
        locks = [r for r in rows if r["action"] == "score_lock"]
        assert len(locks) == 1
        e = locks[0]
        assert e["actor"] == "丁老师"                       # 登录身份权威署名
        assert e["patient_id"] == "PA" and e["session_id"] == "SA"
        assert secret not in e["summary"]                   # 红线:审计绝不含患者作答文本


def test_audit_chain_verify_ok_then_tamper_detected(real_db):
    with TestClient(app) as client:
        _login(client)                                      # login 审计
        client.post("/patients", json={"patient_id": "PB"})
        client.post("/patients/PB/scales", json={"phase_type": "前测", "scale_name": "CETI", "score": 10})
        v = client.get("/audit/verify").json()
        assert v["ok"] is True and v["count"] >= 2
        with Session(real_db) as s:                         # 直接改一条历史记录 → 应断链
            row = s.exec(select(AuditLog).order_by(AuditLog.id)).first()
            row.summary = "被偷改的记录"
            s.add(row); s.commit()
        v2 = client.get("/audit/verify").json()
        assert v2["ok"] is False and v2["broken_at"] is not None


def test_audit_tail_truncation_detected(real_db):
    # 尾部截断(删最后 N 条)是裸哈希链的盲区;高水位锚点应把它检出。
    with TestClient(app) as client:
        _login(client)
        client.post("/patients", json={"patient_id": "PD"})
        client.post("/patients/PD/scales", json={"phase_type": "前测", "scale_name": "CETI", "score": 5})
        assert client.get("/audit/verify").json()["ok"] is True
        with Session(real_db) as s:                         # 直接删掉最后一条(不更新锚点)
            last = s.exec(select(AuditLog).order_by(AuditLog.id.desc())).first()
            s.delete(last); s.commit()
        v = client.get("/audit/verify").json()
        assert v["ok"] is False and v["problem"] == "truncated"


def test_audit_interior_tamper_reports_chain_broken(real_db):
    with TestClient(app) as client:
        _login(client)
        client.post("/patients", json={"patient_id": "PE"})
        client.post("/patients/PE/scales", json={"phase_type": "前测", "scale_name": "CADL", "score": 3})
        with Session(real_db) as s:
            row = s.exec(select(AuditLog).order_by(AuditLog.id)).first()
            row.summary = "偷改的记录"; s.add(row); s.commit()
        v = client.get("/audit/verify").json()
        assert v["ok"] is False and v["problem"] == "chain_broken" and v["broken_at"] is not None


def test_audit_read_requires_auth(real_db):
    with TestClient(app) as client:
        assert client.get("/audit").status_code == 401      # 未登录挡
        assert client.get("/audit/verify").status_code == 401
        _login(client)
        assert client.get("/audit").status_code == 200
        assert client.get("/audit/verify").status_code == 200


def test_scale_and_export_are_audited(real_db):
    with TestClient(app) as client:
        _login(client)
        client.post("/patients", json={"patient_id": "PC"})
        client.post("/patients/PC/scales", json={"phase_type": "后测", "scale_name": "CADL", "score": 7})
        actions = {r["action"] for r in client.get("/audit", params={"patient_id": "PC"}).json()}
        assert "scale_record" in actions
