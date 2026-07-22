"""M1-E 研究审计账本测试:哈希链完整性/篡改检出、锁分产生审计、红线不含患者作答文本、读口须认证。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import audio_store, auth, db, main as main_module
from app.main import app
from app.models import (AttemptEvent, AudioAssetRow, AuditLog, ResearchUser,
                        SessionRuntimeState)


@pytest.fixture
def real_db(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", eng)
    SQLModel.metadata.create_all(eng)
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    with Session(eng) as s:
        s.add_all([
            ResearchUser(username="u", display_id="丁老师",
                         password_hash=auth.hash_password("password1"),
                         role="data_steward", created_at=datetime.now()),
            ResearchUser(username="writer", display_id="研究员甲",
                         password_hash=auth.hash_password("password1"),
                         role="researcher", created_at=datetime.now()),
        ])
        s.commit()
    return eng


def _login(client, username="u"):
    assert client.post("/auth/login", json={"username": username, "password": "password1"}).status_code == 200
    token = client.cookies.get(auth.CSRF_COOKIE_NAME)
    assert token
    client.headers.update({"X-CSRF-Token": token})


def test_login_audit_exception_does_not_leak_or_block_login(
        real_db, monkeypatch, capsys):
    sentinel = (
        "LOGIN-LEAK patient=P-SENTINEL session=S-SENTINEL "
        "token=tok-sentinel path=/private/login.db"
    )

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(main_module.audit, "record", fail_audit)
    with TestClient(app) as client:
        response = client.post("/auth/login", json={
            "username": "u", "password": "password1",
        })

    assert response.status_code == 200
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == "[ops] code=login_audit_append_failed\n"
    assert sentinel not in captured.out


def _lock_a_turn(client, answer="锚"):
    client.post("/patients", json={"patient_id": "PA", "consent_status": "已同意",
                                   "consent_type": "本人同意", "mandarin_eligible": True,
                                   "recording_allowed": True,
                                   "is_simulation_subject": True})
    client.post("/sessions", json={"session_id": "SA", "patient_id": "PA", "week_no": 2,
                                   "phase_type": "正式训练", "event_line": "正式训练",
                                   "item_bank_version_id": "wk2-v1-20260707",
                                   "is_simulation": True,
                                   "trainer_id": "研究员甲"})
    ie = client.post("/sessions/SA/items", json={"item_id": "SE_锚", "task_type": "单要素"}).json()
    # Production creates the raw id through the paired-device/autopilot plane;
    # this audit fixture starts at that authoritative pre-registration boundary.
    audio_bytes = b"\x1a\x45\xdf\xa3audit-audio"
    audio_path, checksum = audio_store.save_blob(
        "audit-turn-audio", audio_bytes, "audio/webm")
    with Session(db.engine) as session:
        session.add(AudioAssetRow(
            raw_audio_id="audit-turn-audio", session_id="SA",
            turn_key="SE_锚#1", is_simulation=True,
            data_classification="simulation",
            audio_format=audio_path.suffix.lstrip("."),
            checksum=checksum,
            byte_count=len(audio_bytes),
            uploaded_at=datetime.now(),
        ))
        session.commit()
    assert client.post("/audio", json={
        "raw_audio_id": "audit-turn-audio", "session_id": "SA",
        "turn_key": "SE_锚#1",
    }).status_code == 200
    assert client.put("/audio/audit-turn-audio/blob",
                      content=audio_bytes,
                      headers={"content-type": "audio/webm"}).status_code == 200
    with Session(db.engine) as session:
        session.add(AttemptEvent(
            session_id="SA", item_id="SE_锚", turn_seq=1,
            response_role="命名", attempt_seq=1, raw_audio_id="audit-turn-audio",
            prompt_level=0, asr_text=answer, asr_confidence=.9,
            asr_engine_version="test-asr", operational_answer_type="正确",
            operational_score=1, operational_needs_review=False,
            judge_mode="规则确定式", judge_engine_version="rule-test",
            processing_status="completed", is_simulation=True,
        ))
        session.commit()
    te = client.post(f"/items/{ie['id']}/turns",
                     json={"turn_seq": 1, "response_role": "命名",
                           "raw_audio_id": "audit-turn-audio"}).json()
    with Session(db.engine) as session:
        session.add(SessionRuntimeState(
            session_id="SA", status="intervention_completed",
            revision=1, intervention_completed_at=datetime.now(),
        ))
        session.commit()
    client.patch(f"/turns/{te['id']}/confirm", json={
        "confirmed_response_text": answer, "expected_revision": 0,
        "idempotency_key": "test-audit-confirm-0001",
    })
    assert client.patch(f"/turns/{te['id']}/lock",
                        json={"reviewer_id": "x", "element_value": 1, "prompt_level": 0}).status_code == 200
    return te["id"]


def test_lock_produces_audit_entry_metadata_only(real_db):
    secret = "我是李明住在南京鼓楼"          # 患者作答里塞入可识别文本
    with TestClient(app) as client:
        _login(client, "writer")
        _lock_a_turn(client, answer=secret)
        _login(client, "u")
        rows = client.get("/audit", params={"session_id": "SA"}).json()
        locks = [r for r in rows if r["action"] == "score_lock"]
        assert len(locks) == 1
        e = locks[0]
        assert e["actor"] == "研究员甲"                     # 临床写入由 researcher 权威署名
        assert e["patient_id"] == "PA" and e["session_id"] == "SA"
        assert secret not in e["summary"]                   # 红线:审计绝不含患者作答文本


def test_audit_chain_verify_ok_then_tamper_detected(real_db):
    with TestClient(app) as client:
        _login(client, "writer")                            # login 审计
        client.post("/patients", json={"patient_id": "PB"})
        _login(client, "u")
        v = client.get("/audit/verify").json()
        assert v["ok"] is True and v["count"] >= 2
        with Session(real_db) as s:                         # 直接改一条历史记录 → 应断链
            row = s.exec(select(AuditLog).order_by(AuditLog.id)).first()
            row.summary = "被偷改的记录"
            s.add(row)
            s.commit()
        v2 = client.get("/audit/verify").json()
        assert v2["ok"] is False and v2["broken_at"] is not None


def test_audit_tail_truncation_detected(real_db):
    # 尾部截断(删最后 N 条)是裸哈希链的盲区;高水位锚点应把它检出。
    with TestClient(app) as client:
        _login(client, "writer")
        client.post("/patients", json={"patient_id": "PD"})
        _login(client, "u")
        assert client.get("/audit/verify").json()["ok"] is True
        with Session(real_db) as s:                         # 直接删掉最后一条(不更新锚点)
            last = s.exec(select(AuditLog).order_by(AuditLog.id.desc())).first()
            s.delete(last)
            s.commit()
        v = client.get("/audit/verify").json()
        assert v["ok"] is False and v["problem"] == "truncated"


def test_audit_interior_tamper_reports_chain_broken(real_db):
    with TestClient(app) as client:
        _login(client, "writer")
        client.post("/patients", json={"patient_id": "PE"})
        _login(client, "u")
        with Session(real_db) as s:
            row = s.exec(select(AuditLog).order_by(AuditLog.id)).first()
            row.summary = "偷改的记录"
            s.add(row)
            s.commit()
        v = client.get("/audit/verify").json()
        assert v["ok"] is False and v["problem"] == "chain_broken" and v["broken_at"] is not None


def test_audit_read_requires_auth(real_db):
    with TestClient(app) as client:
        assert client.get("/audit").status_code == 401      # 未登录挡
        assert client.get("/audit/verify").status_code == 401
        _login(client)
        assert client.get("/audit").status_code == 200
        assert client.get("/audit/verify").status_code == 200


def test_legacy_scale_write_is_blocked_and_not_audited(real_db):
    with TestClient(app) as client:
        _login(client, "writer")
        client.post("/patients", json={"patient_id": "PC"})
        blocked = client.post("/patients/PC/scales", json={
            "phase_type": "后测", "scale_name": "CADL", "score": 7,
        })
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "scale_protocol_not_frozen"
        _login(client, "u")
        actions = {r["action"] for r in client.get("/audit").json()}
        assert "scale_record" not in actions
