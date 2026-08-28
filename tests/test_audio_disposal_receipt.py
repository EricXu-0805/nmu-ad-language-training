"""跨设备本地音频副本删除回执(收据 144):治理信号,不是门禁。

设备收到 410 audio_terminal_disposition 并物理删除本地副本后,把 410 detail 原样
回显为 kind=audioDisposalConfirmed 上报。服务端 fail-closed 校验:资产必须已终态、
回显与资产行一致、capability 场次绑定、(raw_audio_id, 设备) 幂等;账号 cookie 不得
代报(confused-deputy)。删除永不等待回执——回执只可见化,不阻断任何删除。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app import auth, db
from app.enums import AudioStatus
from app.main import app
from app.models import (
    AudioAssetRow,
    AudioLocalCopyDisposalReceipt,
    AuditLog,
    ResearchUser,
)


@pytest.fixture
def disposal_client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'disposal-receipt.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 1},
    )
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    client = TestClient(app)
    client.test_engine = engine
    yield client
    client.close()
    engine.dispose()


def _switch_live(client: TestClient, session_id: str) -> None:
    response = client.put("/live/state", json={
        "kind": "session",
        "payload": {
            "sessionId": session_id,
            "weekNo": 2,
            "eventLine": "正式训练",
            "mode": "task",
            "itemBankVersionId": "wk2-v1-20260707",
        },
    })
    assert response.status_code == 200, response.text
    cursor = client.put("/live/state", json={
        "kind": "cursor",
        "payload": {
            "sessionId": session_id, "screen": "present",
            "itemIdx": 0, "turnIdx": 0, "responseRole": "命名",
            "cueLevel": 0, "recording": "idle",
        },
    })
    assert cursor.status_code == 200, cursor.text


def _seed_sessions(client: TestClient) -> None:
    for suffix in ("ONE", "TWO"):
        patient = client.post("/patients", json={
            "patient_id": f"P-{suffix}",
            "consent_status": "已同意",
            "consent_type": "本人同意",
            "mandarin_eligible": True,
            "recording_allowed": True,
            "secondary_use_allowed": True,
            "is_simulation_subject": True,
        })
        assert patient.status_code == 200, patient.text
        session = client.post("/sessions", json={
            "session_id": f"S-{suffix}",
            "patient_id": f"P-{suffix}",
            "week_no": 2,
            "phase_type": "正式训练",
            "event_line": "正式训练",
            "item_bank_version_id": "wk2-v1-20260707",
            "is_simulation": True,
        })
        assert session.status_code == 200, session.text
    _switch_live(client, "S-ONE")


def _pair(client: TestClient, monkeypatch, *,
          device_id: str = "disposal-device-000001") -> dict[str, str]:
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    response = client.post(
        "/device/pair",
        headers={"X-Console-Pin": "24681024"},
        json={"deviceId": device_id},
    )
    assert response.status_code == 200, response.text
    return {"X-Device-Capability": response.json()["capability"]}


def _upload(
        client: TestClient, raw_id: str, *,
        headers: dict[str, str] | None = None,
        session_id: str = "S-ONE", turn_key: str = "itm-0001#1") -> dict:
    request_headers = headers or {}
    registered = client.post("/audio", headers=request_headers, json={
        "raw_audio_id": raw_id,
        "session_id": session_id,
        "turn_key": turn_key,
        "contains_direct_identifier": False,
    })
    assert registered.status_code == 200, registered.text
    uploaded = client.put(
        f"/audio/{raw_id}/blob",
        headers={**request_headers, "content-type": "audio/webm"},
        content=b"\x1a\x45\xdf\xa3" + raw_id.encode("ascii"),
    )
    assert uploaded.status_code == 200, uploaded.text
    return uploaded.json()


def _record_capture(
        client: TestClient, headers: dict[str, str], raw_id: str, upload: dict, *,
        session_id: str = "S-ONE", turn_key: str = "itm-0001#1") -> None:
    saved = client.put("/live/state", headers=headers, json={
        "kind": "audioSaved",
        "payload": {
            "rawAudioId": raw_id,
            "durationSeconds": 1.25,
            "byteCount": upload["bytes"],
            "checksum": upload["checksum"],
            "turnKey": turn_key,
            "sessionId": session_id,
            "containsDirectIdentifier": False,
        },
    })
    assert saved.status_code == 200, saved.text


def _mark_terminal(client: TestClient, raw_id: str, *, reason: str) -> None:
    with Session(client.test_engine) as session:
        row = session.get(AudioAssetRow, raw_id)
        assert row is not None
        if reason == "deleted":
            row.status = AudioStatus.deleted
            row.delete_gate_passed = True
        else:
            row.withdrawn = True
            row.withdrawal_status = "isolated"
        session.add(row)
        session.commit()


def _terminal_detail(
        client: TestClient, headers: dict[str, str], raw_id: str, upload: dict, *,
        session_id: str = "S-ONE", turn_key: str = "itm-0001#1") -> dict:
    """真实走一遍 410:重发 audioSaved,把服务端权威 detail 拿回来当回执素材。"""
    response = client.put("/live/state", headers=headers, json={
        "kind": "audioSaved",
        "payload": {
            "rawAudioId": raw_id,
            "durationSeconds": 1.25,
            "byteCount": upload["bytes"],
            "checksum": upload["checksum"],
            "turnKey": turn_key,
            "sessionId": session_id,
            "containsDirectIdentifier": False,
        },
    })
    assert response.status_code == 410, response.text
    return response.json()["detail"]


def _confirm(client: TestClient, headers: dict[str, str], detail: dict):
    return client.put("/live/state", headers=headers, json={
        "kind": "audioDisposalConfirmed",
        "payload": dict(detail),
    })


def _seed_terminal_detail(
        client: TestClient, monkeypatch, *, raw_id: str = "raw-disposal-0001",
        reason: str = "withdrawn",
        device_id: str = "disposal-device-000001") -> tuple[dict[str, str], dict]:
    _seed_sessions(client)
    headers = _pair(client, monkeypatch, device_id=device_id)
    upload = _upload(client, raw_id, headers=headers)
    _record_capture(client, headers, raw_id, upload)
    _mark_terminal(client, raw_id, reason=reason)
    detail = _terminal_detail(client, headers, raw_id, upload)
    return headers, detail


@pytest.mark.parametrize("reason", ["deleted", "withdrawn"])
def test_terminal_disposal_report_lands_receipt_and_audit(
        disposal_client, monkeypatch, reason):
    headers, detail = _seed_terminal_detail(disposal_client, monkeypatch, reason=reason)

    response = _confirm(disposal_client, headers, detail)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == "audio_disposal_recorded"
    assert body["duplicate"] is False
    with Session(disposal_client.test_engine) as session:
        rows = list(session.exec(select(AudioLocalCopyDisposalReceipt)))
        assert len(rows) == 1
        receipt = rows[0]
        assert receipt.raw_audio_id == detail["rawAudioId"]
        assert receipt.session_id == detail["sessionId"]
        assert receipt.reason == reason
        assert receipt.checksum == detail["checksum"]
        assert receipt.byte_count == detail["byteCount"]
        assert receipt.reporter_device_id_hash
        audit_rows = [row for row in session.exec(select(AuditLog))
                      if row.action == "audio_local_copy_disposed"]
        assert len(audit_rows) == 1
        assert detail["rawAudioId"] in audit_rows[0].summary


def test_duplicate_report_is_idempotent_but_second_device_lands_new_row(
        disposal_client, monkeypatch):
    headers, detail = _seed_terminal_detail(disposal_client, monkeypatch)

    first = _confirm(disposal_client, headers, detail)
    assert first.status_code == 200, first.text
    replay = _confirm(disposal_client, headers, detail)
    assert replay.status_code == 200, replay.text
    assert replay.json()["duplicate"] is True

    second_device = _pair(disposal_client, monkeypatch,
                          device_id="disposal-device-000002")
    other = _confirm(disposal_client, second_device, detail)
    assert other.status_code == 200, other.text
    assert other.json()["duplicate"] is False

    with Session(disposal_client.test_engine) as session:
        rows = list(session.exec(select(AudioLocalCopyDisposalReceipt)))
        assert len(rows) == 2
        assert len({row.reporter_device_id_hash for row in rows}) == 2
        audit_rows = [row for row in session.exec(select(AuditLog))
                      if row.action == "audio_local_copy_disposed"]
        assert len(audit_rows) == 2  # 幂等重放不重复记账


def test_non_terminal_asset_rejects_report(disposal_client, monkeypatch):
    _seed_sessions(disposal_client)
    raw_id = "raw-disposal-live-01"
    headers = _pair(disposal_client, monkeypatch)
    upload = _upload(disposal_client, raw_id, headers=headers)
    _record_capture(disposal_client, headers, raw_id, upload)

    live_detail = {
        "code": "audio_terminal_disposition",
        "schemaVersion": 1,
        "action": "discard_local_copy",
        "reason": "deleted",
        "rawAudioId": raw_id,
        "sessionId": "S-ONE",
        "turnKey": "itm-0001#1",
        "byteCount": upload["bytes"],
        "checksum": upload["checksum"],
        "containsDirectIdentifier": False,
    }
    response = _confirm(disposal_client, headers, live_detail)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "audio_disposal_not_terminal"
    with Session(disposal_client.test_engine) as session:
        assert list(session.exec(select(AudioLocalCopyDisposalReceipt))) == []


def test_deleted_without_gate_never_accepts_receipt(disposal_client, monkeypatch):
    """与 410 发放端逐字一致:deleted 而缺删除闸门的行从未被授权 410,回执必拒。"""
    headers, detail = _seed_terminal_detail(
        disposal_client, monkeypatch, reason="deleted")
    with Session(disposal_client.test_engine) as session:
        row = session.get(AudioAssetRow, detail["rawAudioId"])
        assert row is not None
        row.delete_gate_passed = False
        session.add(row)
        session.commit()

    response = _confirm(disposal_client, headers, detail)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "audio_disposal_mismatch"
    with Session(disposal_client.test_engine) as session:
        assert list(session.exec(select(AudioLocalCopyDisposalReceipt))) == []


@pytest.mark.parametrize("mutate", [
    lambda detail: {**detail, "checksum": "0" * 64},
    lambda detail: {**detail, "byteCount": detail["byteCount"] + 1},
    lambda detail: {**detail, "reason":
                    "deleted" if detail["reason"] == "withdrawn" else "withdrawn"},
    lambda detail: {**detail, "containsDirectIdentifier": True},
], ids=["checksum", "byte_count", "reason", "direct_identifier"])
def test_echo_mismatch_with_asset_row_is_rejected(
        disposal_client, monkeypatch, mutate):
    headers, detail = _seed_terminal_detail(disposal_client, monkeypatch)

    response = _confirm(disposal_client, headers, mutate(detail))

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "audio_disposal_mismatch"
    with Session(disposal_client.test_engine) as session:
        assert list(session.exec(select(AudioLocalCopyDisposalReceipt))) == []


def test_account_cookie_cannot_report_for_device(disposal_client, monkeypatch):
    headers, detail = _seed_terminal_detail(disposal_client, monkeypatch)
    admin = TestClient(app)
    with Session(disposal_client.test_engine) as session:
        session.add(ResearchUser(
            username="disposal-admin",
            display_id="DISPOSAL-ADMIN",
            password_hash=auth.hash_password("password1"),
            role="admin",
            created_at=datetime.now(),
        ))
        session.commit()
    login = admin.post("/auth/login", json={
        "username": "disposal-admin", "password": "password1"})
    assert login.status_code == 200, login.text
    admin.headers["X-CSRF-Token"] = admin.cookies.get(auth.CSRF_COOKIE_NAME)

    response = admin.put("/live/state", json={
        "kind": "audioDisposalConfirmed",
        "payload": dict(detail),
    })

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == (
        "audio_disposition_device_capability_required")
    with Session(disposal_client.test_engine) as session:
        assert list(session.exec(select(AudioLocalCopyDisposalReceipt))) == []
    admin.close()


def test_foreign_session_capability_and_unknown_id_fail_closed(
        disposal_client, monkeypatch):
    # deleted 终态:withdrawn 会同时围栏整场,阻断本测试所需的回原场切换。
    headers, detail = _seed_terminal_detail(
        disposal_client, monkeypatch, reason="deleted")

    admin = TestClient(app)
    with Session(disposal_client.test_engine) as session:
        session.add(ResearchUser(
            username="disposal-switch-admin",
            display_id="DISPOSAL-SWITCH",
            password_hash=auth.hash_password("password1"),
            role="admin",
            created_at=datetime.now(),
        ))
        session.commit()
    login = admin.post("/auth/login", json={
        "username": "disposal-switch-admin", "password": "password1"})
    assert login.status_code == 200, login.text
    admin.headers["X-CSRF-Token"] = admin.cookies.get(auth.CSRF_COOKIE_NAME)
    try:
        # 换到另一场次的能力凭据:场次绑定必须挡住跨场上报。
        _switch_live(admin, "S-TWO")
        foreign = _pair(disposal_client, monkeypatch,
                        device_id="disposal-device-000003")
        crossed = _confirm(disposal_client, foreign, detail)
        assert crossed.status_code == 409, crossed.text
        assert crossed.json()["detail"]["code"] == "device_session_mismatch"

        # 回到原场:未登记 id 与外域 id 的响应必须一致(不可枚举)。
        _switch_live(admin, "S-ONE")
        unknown = _confirm(disposal_client, headers, {
            **detail, "rawAudioId": "raw-disposal-nonexist"})
        assert unknown.status_code == 404, unknown.text
        assert unknown.json()["detail"]["code"] == "audio_disposition_unknown"
        with Session(disposal_client.test_engine) as session:
            assert list(session.exec(select(AudioLocalCopyDisposalReceipt))) == []
    finally:
        admin.close()


def test_governance_list_projects_disposal_receipts(disposal_client, monkeypatch):
    """治理面板数据源:每条撤回录音带「几台设备确认已删+最近时间」;0 回执显式为 0。"""
    headers, detail = _seed_terminal_detail(disposal_client, monkeypatch)
    assert _confirm(disposal_client, headers, detail).status_code == 200

    admin = TestClient(app)
    with Session(disposal_client.test_engine) as session:
        session.add(ResearchUser(
            username="disposal-gov-admin",
            display_id="DISPOSAL-GOV",
            password_hash=auth.hash_password("password1"),
            role="admin",
            created_at=datetime.now(),
        ))
        session.commit()
    login = admin.post("/auth/login", json={
        "username": "disposal-gov-admin", "password": "password1"})
    assert login.status_code == 200, login.text
    admin.headers["X-CSRF-Token"] = admin.cookies.get(auth.CSRF_COOKIE_NAME)
    try:
        listing = admin.get("/governance/withdrawn-audio")
        assert listing.status_code == 200, listing.text
        row = next(entry for entry in listing.json()
                   if entry["raw_audio_id"] == detail["rawAudioId"])
        assert row["local_copy_disposal_device_count"] == 1
        assert row["local_copy_disposal_last_at"]
    finally:
        admin.close()


def test_migration_downgrade_refuses_when_receipts_exist(tmp_path):
    """降级 fail-closed:已有治理回执的库拒绝抹掉证据;空表可往返。"""
    import sqlite3
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from alembic.util.exc import CommandError

    root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "disposal-migration.sqlite"
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

    command.upgrade(config, "head")
    command.downgrade(config, "e4a7c1d9b206")  # 空表:允许往返
    command.upgrade(config, "head")

    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO audiolocalcopydisposalreceipt (raw_audio_id, session_id,"
        " turn_key, reason, checksum, byte_count, contains_direct_identifier,"
        " reporter_device_id_hash, reported_at) VALUES"
        " ('r','s','t','deleted',?,1,0,'hash','2026-08-07 00:00:00')",
        ("a" * 64,))
    connection.commit()
    connection.close()

    with pytest.raises((RuntimeError, CommandError)):
        command.downgrade(config, "e4a7c1d9b206")

    # 拒绝必须发生在回执迁移自身任何 DDL 之前(SQLite 非事务 DDL,顺序
    # load-bearing):降级从当前头合法走过其上的空证据迁移后,停在拒绝的
    # f7c2e8a4d105;回执表仍在、回执行原样,绝不下穿。
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute(
            "SELECT version_num FROM alembic_version").fetchone()[0] == (
            "f7c2e8a4d105")
        rows = connection.execute(
            "SELECT raw_audio_id, reason FROM audiolocalcopydisposalreceipt"
        ).fetchall()
        assert rows == [("r", "deleted")]
    finally:
        connection.close()


def test_payload_contract_is_exact(disposal_client, monkeypatch):
    headers, detail = _seed_terminal_detail(disposal_client, monkeypatch)

    smuggled = _confirm(disposal_client, headers, {**detail, "extra": True})
    assert smuggled.status_code == 422, smuggled.text

    for missing_key in ("checksum", "reason", "rawAudioId"):
        broken = dict(detail)
        broken.pop(missing_key)
        response = _confirm(disposal_client, headers, broken)
        assert response.status_code == 422, (missing_key, response.text)

    with Session(disposal_client.test_engine) as session:
        assert list(session.exec(select(AudioLocalCopyDisposalReceipt))) == []
