"""录音终态本地处置与物理删除的 fail-closed 协议。"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app import audio_store, auth, db, main as main_module
from app.enums import AudioStatus
from app.main import app
from app.models import (
    AttemptEvent,
    AudioAssetRow,
    AudioCaptureReceipt,
    AuditLog,
    InteractionEvent,
    LiveState,
    ResearchUser,
    SessionRuntimeState,
)


@pytest.fixture
def disposition_client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'terminal-disposition.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 1},
    )
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    client = TestClient(app)
    client.test_engine = engine
    yield client
    client.close()
    engine.dispose()


def _switch_live(client: TestClient, session_id: str,
                 headers: dict[str, str] | None = None) -> None:
    response = client.put("/live/state", headers=headers or {}, json={
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
    cursor = client.put("/live/state", headers=headers or {}, json={
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


def _pair(client: TestClient, *, device_id: str = "terminal-device-000001") -> dict[str, str]:
    response = client.post(
        "/device/pair",
        headers={"X-Console-Pin": "24681024"},
        json={"deviceId": device_id},
    )
    assert response.status_code == 200, response.text
    return {"X-Device-Capability": response.json()["capability"]}


def _admin_client(engine, *, username: str = "terminal-admin") -> TestClient:
    with Session(engine) as session:
        session.add(ResearchUser(
            username=username,
            display_id=username.upper(),
            password_hash=auth.hash_password("password1"),
            role="admin",
            created_at=datetime.now(),
        ))
        session.commit()
    client = TestClient(app)
    login = client.post("/auth/login", json={
        "username": username,
        "password": "password1",
    })
    assert login.status_code == 200, login.text
    client.headers["X-CSRF-Token"] = client.cookies.get(auth.CSRF_COOKIE_NAME)
    return client


def _upload(
        client: TestClient, raw_id: str, *, headers: dict[str, str] | None = None,
        session_id: str = "S-ONE", turn_key: str = "SE_锚#1",
        contains_identifier: bool = False) -> tuple[bytes, dict]:
    request_headers = headers or {}
    registered = client.post("/audio", headers=request_headers, json={
        "raw_audio_id": raw_id,
        "session_id": session_id,
        "turn_key": turn_key,
        "contains_direct_identifier": contains_identifier,
    })
    assert registered.status_code == 200, registered.text
    content = b"\x1a\x45\xdf\xa3" + raw_id.encode("ascii")
    uploaded = client.put(
        f"/audio/{raw_id}/blob",
        headers={**request_headers, "content-type": "audio/webm"},
        content=content,
    )
    assert uploaded.status_code == 200, uploaded.text
    return content, uploaded.json()


def _audio_saved(
        raw_id: str, upload: dict, *, session_id: str = "S-ONE",
        turn_key: str = "SE_锚#1", duration: float = 1.25,
        contains_identifier: bool | None = False) -> dict:
    payload = {
        "rawAudioId": raw_id,
        "durationSeconds": duration,
        "byteCount": upload["bytes"],
        # 大写输入也必须在授权响应中标准化为小写。
        "checksum": upload["checksum"].upper(),
        "turnKey": turn_key,
        "sessionId": session_id,
    }
    if contains_identifier is not None:
        payload["containsDirectIdentifier"] = contains_identifier
    return {"kind": "audioSaved", "payload": payload}


def _mark_terminal(
        client: TestClient, raw_id: str, *, reason: str,
        history_complete: bool = True) -> None:
    with Session(client.test_engine) as session:
        row = session.get(AudioAssetRow, raw_id)
        assert row is not None
        if reason == "deleted":
            row.status = AudioStatus.deleted
            row.delete_gate_passed = True
        else:
            row.withdrawn = True
            row.withdrawal_status = "isolated"
        if not history_complete:
            row.uploaded_at = None
        session.add(row)
        session.commit()


def _assert_device_no_store(response) -> None:
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"


def _runtime_snapshot(runtime: SessionRuntimeState | None):
    if runtime is None:
        return None
    return (
        runtime.status,
        runtime.revision,
        runtime.cursor_json,
        runtime.rapport_json,
    )


@pytest.mark.parametrize("reason", ["deleted", "withdrawn"])
def test_exact_terminal_disposition_is_strict_410_without_state_mutation(
        disposition_client, reason):
    _seed_sessions(disposition_client)
    _content, uploaded = _upload(
        disposition_client, f"exact-{reason}", contains_identifier=True)
    _mark_terminal(disposition_client, f"exact-{reason}", reason=reason)
    payload = _audio_saved(
        f"exact-{reason}", uploaded, contains_identifier=True)

    with Session(disposition_client.test_engine) as session:
        live = session.get(LiveState, 1)
        runtime = session.get(SessionRuntimeState, "S-ONE")
        before = {
            "live_seq": live.seq,
            "runtime": _runtime_snapshot(runtime),
            "attempts": len(list(session.exec(select(AttemptEvent)))),
            "interactions": len(list(session.exec(select(InteractionEvent)))),
            "receipts": len(list(session.exec(select(AudioCaptureReceipt)))),
        }

    response = disposition_client.put("/live/state", json=payload)
    assert response.status_code == 410, response.text
    _assert_device_no_store(response)
    assert response.json()["detail"] == {
        "code": "audio_terminal_disposition",
        "schemaVersion": 1,
        "action": "discard_local_copy",
        "reason": reason,
        "rawAudioId": f"exact-{reason}",
        "sessionId": "S-ONE",
        "turnKey": "SE_锚#1",
        "byteCount": uploaded["bytes"],
        "checksum": uploaded["checksum"],
        "containsDirectIdentifier": True,
    }

    with Session(disposition_client.test_engine) as session:
        live = session.get(LiveState, 1)
        runtime = session.get(SessionRuntimeState, "S-ONE")
        after = {
            "live_seq": live.seq,
            "runtime": _runtime_snapshot(runtime),
            "attempts": len(list(session.exec(select(AttemptEvent)))),
            "interactions": len(list(session.exec(select(InteractionEvent)))),
            "receipts": len(list(session.exec(select(AudioCaptureReceipt)))),
        }
    assert after == before


def test_unknown_mismatch_and_incomplete_history_never_authorize_discard(
        disposition_client):
    _seed_sessions(disposition_client)
    _content, uploaded = _upload(disposition_client, "integrity-terminal")
    _mark_terminal(disposition_client, "integrity-terminal", reason="withdrawn")
    exact = _audio_saved("integrity-terminal", uploaded)

    unknown = _audio_saved("never-registered", uploaded)
    unknown_response = disposition_client.put("/live/state", json=unknown)
    assert unknown_response.status_code == 404
    assert unknown_response.json()["detail"]["code"] == "audio_disposition_unknown"
    assert "action" not in unknown_response.json()["detail"]
    _assert_device_no_store(unknown_response)

    wrong_session = _audio_saved(
        "integrity-terminal", uploaded, session_id="S-TWO")
    foreign = disposition_client.put("/live/state", json=wrong_session)
    assert foreign.status_code == 404
    assert foreign.json() == unknown_response.json()
    assert "action" not in foreign.json()["detail"]

    variants = []
    wrong_turn = _audio_saved(
        "integrity-terminal", uploaded, turn_key="SE_树#1")
    variants.append(wrong_turn)
    wrong_bytes = _audio_saved("integrity-terminal", uploaded)
    wrong_bytes["payload"]["byteCount"] += 1
    variants.append(wrong_bytes)
    wrong_checksum = _audio_saved("integrity-terminal", uploaded)
    wrong_checksum["payload"]["checksum"] = "0" * 64
    variants.append(wrong_checksum)
    variants.append(_audio_saved(
        "integrity-terminal", uploaded, contains_identifier=True))
    variants.append(_audio_saved(
        "integrity-terminal", uploaded, contains_identifier=None))

    for body in variants:
        rejected = disposition_client.put("/live/state", json=body)
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"]["code"] == (
            "audio_disposition_integrity_failure")
        assert "action" not in rejected.json()["detail"]
        _assert_device_no_store(rejected)

    with Session(disposition_client.test_engine) as session:
        row = session.get(AudioAssetRow, "integrity-terminal")
        row.uploaded_at = None
        session.add(row)
        session.commit()
    incomplete = disposition_client.put("/live/state", json=exact)
    assert incomplete.status_code == 409
    assert incomplete.json()["detail"]["code"] == (
        "audio_disposition_integrity_failure")
    assert "action" not in incomplete.json()["detail"]


@pytest.mark.parametrize("raw_id", ["../escape", "contains space", ".hidden", "slash/id"])
def test_audio_saved_rejects_unsafe_raw_id_before_file_lock(
        disposition_client, raw_id):
    _seed_sessions(disposition_client)
    body = {
        "kind": "audioSaved",
        "payload": {
            "rawAudioId": raw_id,
            "durationSeconds": 1,
            "byteCount": 8,
            "checksum": "0" * 64,
            "turnKey": "SE_锚#1",
            "sessionId": "S-ONE",
            "containsDirectIdentifier": False,
        },
    }
    rejected = disposition_client.put("/live/state", json=body)
    assert rejected.status_code == 422
    assert "discard_local_copy" not in rejected.text


def test_existing_receipt_is_cross_checked_before_terminal_disposition(
        disposition_client):
    _seed_sessions(disposition_client)
    _content, uploaded = _upload(disposition_client, "receipt-terminal")
    exact = _audio_saved("receipt-terminal", uploaded, duration=2.5)
    assert disposition_client.put("/live/state", json=exact).status_code == 200
    _mark_terminal(disposition_client, "receipt-terminal", reason="withdrawn")

    wrong_duration = _audio_saved(
        "receipt-terminal", uploaded, duration=2.75)
    rejected = disposition_client.put("/live/state", json=wrong_duration)
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == (
        "audio_disposition_integrity_failure")
    assert "action" not in rejected.json()["detail"]
    exact_terminal = disposition_client.put("/live/state", json=exact)
    assert exact_terminal.status_code == 410


def test_deleted_without_governance_gate_never_authorizes_local_discard(
        disposition_client):
    _seed_sessions(disposition_client)
    _content, uploaded = _upload(disposition_client, "deleted-without-gate")
    with Session(disposition_client.test_engine) as session:
        row = session.get(AudioAssetRow, "deleted-without-gate")
        assert row is not None
        row.status = AudioStatus.deleted
        row.delete_gate_passed = False
        session.add(row)
        session.commit()

    rejected = disposition_client.put(
        "/live/state", json=_audio_saved("deleted-without-gate", uploaded))
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == (
        "audio_disposition_integrity_failure")
    assert "action" not in rejected.json()["detail"]
    assert "discard_local_copy" not in rejected.text


def test_recovery_capability_can_get_exact_410_but_account_or_bad_token_cannot(
        disposition_client, monkeypatch):
    _seed_sessions(disposition_client)
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    capability = _pair(disposition_client)
    _content, uploaded = _upload(
        disposition_client, "recovery-terminal", headers=capability,
        turn_key="itm-0001#1")
    _mark_terminal(disposition_client, "recovery-terminal", reason="withdrawn")
    body = _audio_saved(
        "recovery-terminal", uploaded, turn_key="itm-0001#1")

    admin = _admin_client(disposition_client.test_engine)
    try:
        _switch_live(admin, "S-TWO")
        recovery = disposition_client.put(
            "/live/state", headers=capability, json=body)
        assert recovery.status_code == 410, recovery.text
        assert recovery.json()["detail"]["action"] == "discard_local_copy"

        account_only = admin.put("/live/state", json=body)
        assert account_only.status_code == 403
        assert account_only.json()["detail"]["code"] == (
            "audio_disposition_device_capability_required")
        assert "action" not in account_only.json()["detail"]

        account_unknown_body = _audio_saved("account-never-registered", uploaded)
        account_unknown = admin.put("/live/state", json=account_unknown_body)
        assert account_unknown.status_code == account_only.status_code
        assert account_unknown.json() == account_only.json()

        bad_bearer = admin.put(
            "/live/state",
            headers={"X-Device-Capability": "x" * 43},
            json=body,
        )
        assert bad_bearer.status_code == 401
        assert bad_bearer.json()["code"] == "device_capability_invalid"
        assert "discard_local_copy" not in bad_bearer.text

        cross_session = dict(body)
        cross_session["payload"] = {**body["payload"], "sessionId": "S-TWO"}
        cross = disposition_client.put(
            "/live/state", headers=capability, json=cross_session)
        assert cross.status_code == 409
        assert cross.json()["detail"]["code"] == "device_session_mismatch"
        assert "action" not in cross.json()["detail"]
        for response in (recovery, account_only, bad_bearer, cross):
            _assert_device_no_store(response)
    finally:
        admin.close()


def test_capability_cannot_enumerate_foreign_session_raw_audio_ids(
        disposition_client, monkeypatch):
    _seed_sessions(disposition_client)
    # Seed an S-TWO asset through the open test setup before protecting the app
    # and pairing S-ONE. Its terminal/nonterminal status must not change the result.
    _switch_live(disposition_client, "S-TWO")
    _content, foreign_upload = _upload(
        disposition_client, "foreign-secret-id",
        session_id="S-TWO", turn_key="SE_锚#1")
    _switch_live(disposition_client, "S-ONE")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    s1_capability = _pair(
        disposition_client, device_id="enumeration-device-0002")

    foreign_probe = _audio_saved(
        "foreign-secret-id", foreign_upload,
        session_id="S-ONE", turn_key="SE_锚#1")
    absent_probe = _audio_saved(
        "foreign-absent-id", foreign_upload,
        session_id="S-ONE", turn_key="SE_锚#1")
    foreign = disposition_client.put(
        "/live/state", headers=s1_capability, json=foreign_probe)
    absent = disposition_client.put(
        "/live/state", headers=s1_capability, json=absent_probe)
    assert foreign.status_code == absent.status_code == 404
    assert foreign.json() == absent.json() == {
        "detail": {
            "code": "audio_disposition_unknown",
            "message": "服务端没有该录音的登记事实，禁止删除本地副本",
        },
    }
    assert "discard_local_copy" not in foreign.text


def test_runtime_terminal_without_audio_terminal_never_grants_discard(
        disposition_client):
    _seed_sessions(disposition_client)
    _content, uploaded = _upload(disposition_client, "runtime-only-terminal")
    with Session(disposition_client.test_engine) as session:
        runtime = session.get(SessionRuntimeState, "S-ONE")
        if runtime is None:
            runtime = SessionRuntimeState(session_id="S-ONE")
        runtime.status = "completed"
        session.add(runtime)
        session.commit()
    response = disposition_client.put(
        "/live/state", json=_audio_saved("runtime-only-terminal", uploaded))
    assert response.status_code == 409
    assert "discard_local_copy" not in response.text


def test_delete_commit_failure_never_unlinks(
        disposition_client, monkeypatch):
    _seed_sessions(disposition_client)
    _content, _uploaded = _upload(disposition_client, "commit-failure-audio")
    with Session(disposition_client.test_engine) as session:
        row = session.get(AudioAssetRow, "commit-failure-audio")
        row.status = AudioStatus.deletable
        session.add(row)
        session.commit()
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    monkeypatch.setenv("ENABLE_AUDIO_DELETE", "1")
    # This test isolates DB-before-unlink ordering. Export authority is covered
    # independently by the legacy-ledger deletion-gate API regression.
    monkeypatch.setattr(
        main_module, "_require_authoritative_export_copy", lambda *_args: None)
    admin = _admin_client(disposition_client.test_engine, username="commit-admin")
    original_commit = Session.commit
    original_delete = audio_store.delete_blob
    physical_called = False

    def fail_logical_commit(session):
        if any(
                isinstance(item, AudioAssetRow)
                and item.raw_audio_id == "commit-failure-audio"
                and item.status == AudioStatus.deleted
                and item.delete_gate_passed
                for item in session.dirty):
            raise RuntimeError("simulated logical delete commit failure")
        return original_commit(session)

    def observe_physical(*args, **kwargs):
        nonlocal physical_called
        physical_called = True
        return original_delete(*args, **kwargs)

    monkeypatch.setattr(Session, "commit", fail_logical_commit)
    monkeypatch.setattr(audio_store, "delete_blob", observe_physical)
    try:
        with pytest.raises(RuntimeError, match="logical delete commit failure"):
            admin.delete("/audio/commit-failure-audio?source=manual&session_id=S-ONE")
    finally:
        admin.close()
        monkeypatch.setattr(Session, "commit", original_commit)
        monkeypatch.setattr(audio_store, "delete_blob", original_delete)
    assert physical_called is False
    assert audio_store.find_blob("commit-failure-audio") is not None
    with Session(disposition_client.test_engine) as session:
        row = session.get(AudioAssetRow, "commit-failure-audio")
        assert row.status == AudioStatus.deletable
        assert row.delete_gate_passed is False


def test_delete_source_is_closed_enum_and_cannot_pollute_audit(
        disposition_client, monkeypatch):
    _seed_sessions(disposition_client)
    _content, _uploaded = _upload(disposition_client, "source-enum-audio")
    with Session(disposition_client.test_engine) as session:
        row = session.get(AudioAssetRow, "source-enum-audio")
        row.status = AudioStatus.deletable
        session.add(row)
        session.commit()
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    monkeypatch.setenv("ENABLE_AUDIO_DELETE", "1")
    admin = _admin_client(disposition_client.test_engine, username="source-admin")
    try:
        rejected = admin.delete(
            "/audio/source-enum-audio",
            params={"source": "manual\npatient free text"},
        )
        assert rejected.status_code == 422
        missing_session = admin.delete(
            "/audio/source-enum-audio", params={"source": "manual"})
        assert missing_session.status_code == 422
        assert missing_session.json()["detail"]["code"] == (
            "audio_delete_session_required")
        foreign_session = admin.delete(
            "/audio/source-enum-audio",
            params={"source": "manual", "session_id": "S-TWO"},
        )
        assert foreign_session.status_code == 409
        assert foreign_session.json()["detail"]["code"] == (
            "audio_delete_session_mismatch")
        assert audio_store.find_blob("source-enum-audio") is not None
        with Session(disposition_client.test_engine) as session:
            row = session.get(AudioAssetRow, "source-enum-audio")
            assert row.status == AudioStatus.deletable
            assert row.delete_gate_passed is False
            assert not list(session.exec(select(AuditLog).where(
                AuditLog.action == "audio_delete")))
    finally:
        admin.close()


def test_unlink_failure_keeps_deleted_and_retry_finishes_cleanup(
        disposition_client, monkeypatch):
    _seed_sessions(disposition_client)
    _content, _uploaded = _upload(disposition_client, "unlink-failure-audio")
    with Session(disposition_client.test_engine) as session:
        row = session.get(AudioAssetRow, "unlink-failure-audio")
        row.status = AudioStatus.deletable
        session.add(row)
        session.commit()
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    monkeypatch.setenv("ENABLE_AUDIO_DELETE", "1")
    monkeypatch.setattr(
        main_module, "_require_authoritative_export_copy", lambda *_args: None)
    admin = _admin_client(disposition_client.test_engine, username="unlink-admin")
    original_delete = audio_store.delete_blob

    def fail_unlink(*_args, **_kwargs):
        raise OSError("simulated unlink failure")

    monkeypatch.setattr(audio_store, "delete_blob", fail_unlink)
    try:
        failed = admin.delete("/audio/unlink-failure-audio?source=manual&session_id=S-ONE")
        assert failed.status_code == 500, failed.text
        assert failed.json()["detail"]["code"] == "audio_physical_cleanup_pending"
        assert audio_store.find_blob("unlink-failure-audio") is not None
        with Session(disposition_client.test_engine) as session:
            row = session.get(AudioAssetRow, "unlink-failure-audio")
            assert row.status == AudioStatus.deleted
            assert row.delete_gate_passed is True

        monkeypatch.setattr(audio_store, "delete_blob", original_delete)
        retried = admin.delete("/audio/unlink-failure-audio?source=manual&session_id=S-ONE")
        assert retried.status_code == 200, retried.text
        assert retried.json()["bytes_deleted"] is True
        assert audio_store.find_blob("unlink-failure-audio") is None
        with Session(disposition_client.test_engine) as session:
            actions = [row.action for row in session.exec(select(AuditLog))]
        assert "audio_delete_cleanup_pending" in actions
        assert "audio_delete" in actions
    finally:
        admin.close()


def test_directory_fsync_failure_is_reported_and_absent_bytes_retry_is_idempotent(
        disposition_client, monkeypatch):
    _seed_sessions(disposition_client)
    _content, _uploaded = _upload(disposition_client, "fsync-failure-audio")
    with Session(disposition_client.test_engine) as session:
        row = session.get(AudioAssetRow, "fsync-failure-audio")
        row.status = AudioStatus.deletable
        session.add(row)
        session.commit()
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    monkeypatch.setenv("ENABLE_AUDIO_DELETE", "1")
    monkeypatch.setattr(
        main_module, "_require_authoritative_export_copy", lambda *_args: None)
    admin = _admin_client(disposition_client.test_engine, username="fsync-admin")
    original_fsync = audio_store.os.fsync

    def fail_directory_fsync(_fd):
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(audio_store.os, "fsync", fail_directory_fsync)
    try:
        failed = admin.delete("/audio/fsync-failure-audio?source=manual&session_id=S-ONE")
        assert failed.status_code == 500, failed.text
        assert failed.json()["detail"]["code"] == "audio_physical_cleanup_pending"
        # unlink 已发生但目录耐久性未确认；绝不在首次响应伪称成功。
        assert audio_store.find_blob("fsync-failure-audio") is None
        with Session(disposition_client.test_engine) as session:
            row = session.get(AudioAssetRow, "fsync-failure-audio")
            assert row.status == AudioStatus.deleted
            assert row.delete_gate_passed is True

        # 字节已不存在也不能直接 200；只要目录 fsync 仍失败，
        # 幂等重试必须继续明确报 cleanup_pending。
        still_pending = admin.delete("/audio/fsync-failure-audio?source=manual&session_id=S-ONE")
        assert still_pending.status_code == 500, still_pending.text
        assert still_pending.json()["detail"]["code"] == (
            "audio_physical_cleanup_pending")

        monkeypatch.setattr(audio_store.os, "fsync", original_fsync)
        retried = admin.delete("/audio/fsync-failure-audio?source=manual&session_id=S-ONE")
        assert retried.status_code == 200, retried.text
        assert retried.json()["bytes_deleted"] is False
    finally:
        admin.close()


# ---------------- 同一台平板换到同一位受试者的新场次:旧场次孤儿录音作废 ----------------
# 2026-09-13 钱凯演示卡住的根因:outbox 里 9/6 那场没传完的录音一直在,新场次一到录音
# 就被「本机存在待恢复录音」挡住;补传又被 device_session_mismatch 409 拒,永远清不掉。


def _seed_second_session_for_patient(client: TestClient, session_id: str, patient_id: str) -> None:
    session = client.post("/sessions", json={
        "session_id": session_id,
        "patient_id": patient_id,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "item_bank_version_id": "wk2-v1-20260707",
        "is_simulation": True,
    })
    assert session.status_code == 200, session.text


def _register_only(client: TestClient, raw_id: str, headers: dict[str, str], *,
                   session_id: str, turn_key: str = "itm-0001#1") -> None:
    registered = client.post("/audio", headers=headers, json={
        "raw_audio_id": raw_id, "session_id": session_id, "turn_key": turn_key,
        "contains_direct_identifier": False,
    })
    assert registered.status_code == 200, registered.text


def _orphan_saved(raw_id: str, *, session_id: str, turn_key: str = "itm-0001#1") -> dict:
    return {"kind": "audioSaved", "payload": {
        "rawAudioId": raw_id, "durationSeconds": 2.5, "byteCount": 4321,
        "checksum": "AB" * 32, "turnKey": turn_key, "sessionId": session_id,
        "containsDirectIdentifier": False,
    }}


def _superseded_scene(client: TestClient, monkeypatch, *, device_id: str = "tablet-0000000000000001"):
    """A 场次登记了录音槽但字节没传上来;同一台平板配到同一受试者的 B 场次。
    档案/场次先建好再设 PIN(PIN 模式下登记档案要具名账号)。"""
    _seed_sessions(client)
    _seed_second_session_for_patient(client, "S-ONE-B", "P-ONE")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    old_headers = _pair(client, device_id=device_id)
    _register_only(client, "raw-orphan-1", old_headers, session_id="S-ONE")
    admin = _admin_client(client.test_engine, username=f"orphan-admin-{device_id[-4:]}")
    try:
        _switch_live(admin, "S-ONE-B")
    finally:
        admin.close()
    new_headers = _pair(client, device_id=device_id)
    return old_headers, new_headers


def test_orphan_capture_of_previous_session_gets_exact_410_on_same_patient_device(
        disposition_client, monkeypatch):
    client = disposition_client
    _, new_headers = _superseded_scene(client, monkeypatch)
    saved = _orphan_saved("raw-orphan-1", session_id="S-ONE")
    response = client.put("/live/state", headers=new_headers, json=saved)
    assert response.status_code == 410, response.text
    assert response.json()["detail"] == {
        "code": "audio_terminal_disposition", "schemaVersion": 1,
        "action": "discard_local_copy", "reason": "deleted",
        "rawAudioId": "raw-orphan-1", "sessionId": "S-ONE", "turnKey": "itm-0001#1",
        "byteCount": 4321, "checksum": "ab" * 32, "containsDirectIdentifier": False,
    }
    _assert_device_no_store(response)
    with Session(client.test_engine) as session:
        row = session.get(AudioAssetRow, "raw-orphan-1")
        assert row.status == AudioStatus.deleted and row.delete_gate_passed is True
        assert row.checksum is None and row.byte_count is None and row.uploaded_at is None
        audits = session.exec(select(AuditLog).where(
            AuditLog.action == "audio_capture_superseded")).all()
        assert len(audits) == 1 and "raw-orphan-1" in audits[0].summary
    # 重放同一回报:同样的 410,不再记第二条审计。
    again = client.put("/live/state", headers=new_headers, json=saved)
    assert again.status_code == 410 and again.json()["detail"]["reason"] == "deleted"
    with Session(client.test_engine) as session:
        assert len(session.exec(select(AuditLog).where(
            AuditLog.action == "audio_capture_superseded")).all()) == 1
    # 设备删掉本地副本后的回执要能落账:这类槽服务端没有字节事实,记的是设备自己的事实。
    confirmed = client.put("/live/state", headers=new_headers, json={
        "kind": "audioDisposalConfirmed", "payload": {
            "code": "audio_terminal_disposition", "schemaVersion": 1,
            "action": "discard_local_copy", "reason": "deleted",
            "rawAudioId": "raw-orphan-1", "sessionId": "S-ONE", "turnKey": "itm-0001#1",
            "byteCount": 4321, "checksum": "ab" * 32, "containsDirectIdentifier": False,
        }})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json() == {"code": "audio_disposal_recorded",
                                "rawAudioId": "raw-orphan-1", "duplicate": False}


@pytest.mark.parametrize("variant", [
    "other_device", "other_patient", "bytes_already_persisted", "same_session_bound",
])
def test_orphan_supersede_only_fires_for_the_exact_same_patient_device_case(
        disposition_client, monkeypatch, variant):
    """别的平板、别的受试者、已经有字节事实的录音、以及绑在同一场次的正常路径:全都不作废。"""
    client = disposition_client
    if variant == "other_device":
        _, _ = _superseded_scene(client, monkeypatch, device_id="tablet-0000000000000001")
        stranger = _pair(client, device_id="tablet-0000000000000002")   # 绑到 S-ONE-B,从没配过 S-ONE
        response = client.put("/live/state", headers=stranger,
                              json=_orphan_saved("raw-orphan-1", session_id="S-ONE"))
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "device_session_mismatch"
    elif variant == "other_patient":
        _seed_sessions(client)
        monkeypatch.setenv("CONSOLE_PIN", "24681024")
        headers = _pair(client, device_id="tablet-0000000000000001")
        _register_only(client, "raw-orphan-2", headers, session_id="S-ONE")
        admin = _admin_client(client.test_engine, username="orphan-admin-two")
        try:
            _switch_live(admin, "S-TWO")                       # 另一位受试者的场次
        finally:
            admin.close()
        moved = _pair(client, device_id="tablet-0000000000000001")
        response = client.put("/live/state", headers=moved,
                              json=_orphan_saved("raw-orphan-2", session_id="S-ONE"))
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "device_session_mismatch"
    elif variant == "bytes_already_persisted":
        _seed_sessions(client)
        _seed_second_session_for_patient(client, "S-ONE-B", "P-ONE")
        monkeypatch.setenv("CONSOLE_PIN", "24681024")
        headers = _pair(client, device_id="tablet-0000000000000001")
        _content, upload = _upload(client, "raw-kept-1", headers=headers, session_id="S-ONE",
                                   turn_key="itm-0001#1")
        admin = _admin_client(client.test_engine, username="orphan-admin-kept")
        try:
            _switch_live(admin, "S-ONE-B")
        finally:
            admin.close()
        moved = _pair(client, device_id="tablet-0000000000000001")
        response = client.put("/live/state", headers=moved,
                              json=_audio_saved("raw-kept-1", upload, session_id="S-ONE",
                                                turn_key="itm-0001#1"))
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "device_session_mismatch"
    else:
        _seed_sessions(client)
        monkeypatch.setenv("CONSOLE_PIN", "24681024")
        headers = _pair(client, device_id="tablet-0000000000000001")
        _register_only(client, "raw-orphan-3", headers, session_id="S-ONE")
        response = client.put("/live/state", headers=headers,
                              json=_orphan_saved("raw-orphan-3", session_id="S-ONE"))
        assert response.status_code != 410, response.text
    with Session(client.test_engine) as session:
        for raw_id in ("raw-orphan-1", "raw-orphan-2", "raw-kept-1", "raw-orphan-3"):
            row = session.get(AudioAssetRow, raw_id)
            if row is not None:
                assert row.status == AudioStatus.recorded, raw_id
        assert session.exec(select(AuditLog).where(
            AuditLog.action == "audio_capture_superseded")).all() == []


# ---- 复核补齐:每一道守卫都要有一条能杀掉「删掉它」的测试 ----


def _register_orphan_for_stranger(client, monkeypatch):
    """P-TWO 的场次上登记一个没字节的槽(用配到 S-TWO 的设备)。"""
    admin = _admin_client(client.test_engine, username="orphan-admin-stranger")
    try:
        _switch_live(admin, "S-TWO")
    finally:
        admin.close()
    stranger = _pair(client, device_id="tablet-0000000000000009")
    _register_only(client, "raw-two-orphan", stranger, session_id="S-TWO")
    admin = _admin_client(client.test_engine, username="orphan-admin-back")
    try:
        _switch_live(admin, "S-ONE")
    finally:
        admin.close()


def test_orphan_supersede_never_touches_a_row_registered_under_another_session(
        disposition_client, monkeypatch):
    """报的是自己旧场次 A 的 sessionId,rawAudioId 却是别人场次登记的槽:照旧 409,那一行不动。"""
    client = disposition_client
    _seed_sessions(client)
    _seed_second_session_for_patient(client, "S-ONE-B", "P-ONE")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    _register_orphan_for_stranger(client, monkeypatch)
    mine = _pair(client, device_id="tablet-0000000000000001")
    _register_only(client, "raw-orphan-1", mine, session_id="S-ONE")
    admin = _admin_client(client.test_engine, username="orphan-admin-b")
    try:
        _switch_live(admin, "S-ONE-B")
    finally:
        admin.close()
    moved = _pair(client, device_id="tablet-0000000000000001")
    response = client.put("/live/state", headers=moved,
                          json=_orphan_saved("raw-two-orphan", session_id="S-ONE"))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "device_session_mismatch"
    with Session(client.test_engine) as session:
        assert session.get(AudioAssetRow, "raw-two-orphan").status == AudioStatus.recorded


@pytest.mark.parametrize("flag", ["withdrawn", "withdrawal_status"])
def test_orphan_supersede_leaves_withdrawn_rows_on_their_existing_path(
        disposition_client, monkeypatch, flag):
    client = disposition_client
    _, new_headers = _superseded_scene(client, monkeypatch)
    with Session(client.test_engine) as session:
        row = session.get(AudioAssetRow, "raw-orphan-1")
        if flag == "withdrawn":
            row.withdrawn = True
        else:
            row.withdrawal_status = "isolated_by_subject_withdrawal"
        session.add(row)
        session.commit()
    response = client.put("/live/state", headers=new_headers,
                          json=_orphan_saved("raw-orphan-1", session_id="S-ONE"))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "device_session_mismatch"
    with Session(client.test_engine) as session:
        row = session.get(AudioAssetRow, "raw-orphan-1")
        assert row.status == AudioStatus.recorded and row.delete_gate_passed is False


@pytest.mark.parametrize("evidence", ["db_upload_facts", "blob_on_disk", "capture_receipt"])
def test_orphan_supersede_refuses_when_the_server_holds_any_byte_evidence(
        disposition_client, monkeypatch, evidence):
    """三道「服务端有字节事实」守卫各自独立:库里有上传事实 / 盘上有原件 / 有采集收据。"""
    client = disposition_client
    _, new_headers = _superseded_scene(client, monkeypatch)
    if evidence == "db_upload_facts":
        with Session(client.test_engine) as session:
            row = session.get(AudioAssetRow, "raw-orphan-1")
            row.byte_count = 4321
            row.checksum = "ab" * 32
            row.uploaded_at = datetime.now()
            session.add(row)
            session.commit()
    elif evidence == "blob_on_disk":
        audio_store.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        (audio_store.AUDIO_DIR / "raw-orphan-1.webm").write_bytes(b"\x1a\x45\xdf\xa3late")
    else:
        with Session(client.test_engine) as session:
            session.add(AudioCaptureReceipt(
                raw_audio_id="raw-orphan-1", session_id="S-ONE", turn_key="SE_锚#1",
                duration_seconds=2.5, byte_count=4321, checksum="ab" * 32,
                data_classification="simulation", is_simulation=True,
                contains_direct_identifier=False))
            session.commit()
    response = client.put("/live/state", headers=new_headers,
                          json=_orphan_saved("raw-orphan-1", session_id="S-ONE"))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "device_session_mismatch"
    with Session(client.test_engine) as session:
        assert session.get(AudioAssetRow, "raw-orphan-1").status == AudioStatus.recorded


def test_orphan_supersede_refuses_a_turn_ref_that_is_not_the_registered_slot(
        disposition_client, monkeypatch):
    client = disposition_client
    _, new_headers = _superseded_scene(client, monkeypatch)
    response = client.put("/live/state", headers=new_headers,
                          json=_orphan_saved("raw-orphan-1", session_id="S-ONE", turn_key="itm-0002#1"))
    assert response.status_code == 409, response.text
    with Session(client.test_engine) as session:
        assert session.get(AudioAssetRow, "raw-orphan-1").status == AudioStatus.recorded


@pytest.mark.parametrize("flag_value", [True, None])
def test_orphan_supersede_needs_the_exact_identifier_flag(
        disposition_client, monkeypatch, flag_value):
    """标记不一致或干脆没带:不发不可逆的 410。"""
    client = disposition_client
    _, new_headers = _superseded_scene(client, monkeypatch)
    body = _orphan_saved("raw-orphan-1", session_id="S-ONE")
    if flag_value is None:
        del body["payload"]["containsDirectIdentifier"]
    else:
        body["payload"]["containsDirectIdentifier"] = flag_value
    response = client.put("/live/state", headers=new_headers, json=body)
    assert response.status_code == 409, response.text
    with Session(client.test_engine) as session:
        assert session.get(AudioAssetRow, "raw-orphan-1").status == AudioStatus.recorded


def test_orphan_supersede_via_the_old_session_recovery_token(disposition_client, monkeypatch):
    """路径 (b):平板拿旧场次 A 自己的 recovery-only 凭据回报——它已配到同一受试者的新场次。"""
    client = disposition_client
    old_headers, _new_headers = _superseded_scene(client, monkeypatch)
    response = client.put("/live/state", headers=old_headers,
                          json=_orphan_saved("raw-orphan-1", session_id="S-ONE"))
    assert response.status_code == 410, response.text
    assert response.json()["detail"]["reason"] == "deleted"
    with Session(client.test_engine) as session:
        row = session.get(AudioAssetRow, "raw-orphan-1")
        assert row.status == AudioStatus.deleted and row.delete_gate_passed is True


def test_recovery_token_alone_does_not_supersede_without_a_newer_pairing(
        disposition_client, monkeypatch):
    """旧场次的 recovery 凭据但这台平板没配到别的场次:不作废(研究者可能还要回来续这一场)。"""
    client = disposition_client
    _seed_sessions(client)
    _seed_second_session_for_patient(client, "S-ONE-B", "P-ONE")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    headers = _pair(client, device_id="tablet-0000000000000001")
    _register_only(client, "raw-orphan-1", headers, session_id="S-ONE")
    admin = _admin_client(client.test_engine, username="orphan-admin-switch")
    try:
        _switch_live(admin, "S-ONE-B")      # 床旁槽切走,A 的凭据降为 recovery-only;平板没有再配对
    finally:
        admin.close()
    response = client.put("/live/state", headers=headers,
                          json=_orphan_saved("raw-orphan-1", session_id="S-ONE"))
    assert response.status_code != 410, response.text
    with Session(client.test_engine) as session:
        assert session.get(AudioAssetRow, "raw-orphan-1").status == AudioStatus.recorded


def test_superseded_slot_replays_the_410_after_re_pairing_to_the_old_session(
        disposition_client, monkeypatch):
    """路径 (c):作废之后平板又配回 A,同一段录音再报一次,仍是 410,本地副本能清掉。"""
    client = disposition_client
    _, new_headers = _superseded_scene(client, monkeypatch)
    first = client.put("/live/state", headers=new_headers,
                       json=_orphan_saved("raw-orphan-1", session_id="S-ONE"))
    assert first.status_code == 410, first.text
    admin = _admin_client(client.test_engine, username="orphan-admin-return")
    try:
        _switch_live(admin, "S-ONE")
    finally:
        admin.close()
    back = _pair(client, device_id="tablet-0000000000000001")
    replay = client.put("/live/state", headers=back,
                        json=_orphan_saved("raw-orphan-1", session_id="S-ONE"))
    assert replay.status_code == 410, replay.text
    assert replay.json()["detail"]["reason"] == "deleted"
    with Session(client.test_engine) as session:
        assert len(session.exec(select(AuditLog).where(
            AuditLog.action == "audio_capture_superseded")).all()) == 1


def test_orphan_supersede_also_clears_slots_of_an_aborted_old_session(
        disposition_client, monkeypatch):
    """研究者把出故障的旧场次中止了再开新场:没字节的槽照样作废,平板不能因此卡死。"""
    client = disposition_client
    _, new_headers = _superseded_scene(client, monkeypatch)
    with Session(client.test_engine) as session:
        state = session.get(SessionRuntimeState, "S-ONE")
        if state is None:
            state = SessionRuntimeState(session_id="S-ONE", status="aborted", revision=1)
        else:
            state.status = "aborted"
        session.add(state)
        session.commit()
    response = client.put("/live/state", headers=new_headers,
                          json=_orphan_saved("raw-orphan-1", session_id="S-ONE"))
    assert response.status_code == 410, response.text


@pytest.mark.parametrize("reporter", ["stranger_device", "deleted_with_bytes"])
def test_orphan_disposal_receipt_stays_on_the_strict_path_for_everyone_else(
        disposition_client, monkeypatch, reporter):
    client = disposition_client
    if reporter == "stranger_device":
        _, _ = _superseded_scene(client, monkeypatch)
        first = client.put("/live/state", headers=_pair(client, device_id="tablet-0000000000000001"),
                           json=_orphan_saved("raw-orphan-1", session_id="S-ONE"))
        assert first.status_code == 410, first.text
        stranger = _pair(client, device_id="tablet-0000000000000002")
        response = client.put("/live/state", headers=stranger, json={
            "kind": "audioDisposalConfirmed", "payload": {
                "code": "audio_terminal_disposition", "schemaVersion": 1,
                "action": "discard_local_copy", "reason": "deleted",
                "rawAudioId": "raw-orphan-1", "sessionId": "S-ONE", "turnKey": "itm-0001#1",
                "byteCount": 4321, "checksum": "ab" * 32, "containsDirectIdentifier": False,
            }})
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "device_session_mismatch"
    else:
        _seed_sessions(client)
        _seed_second_session_for_patient(client, "S-ONE-B", "P-ONE")
        monkeypatch.setenv("CONSOLE_PIN", "24681024")
        headers = _pair(client, device_id="tablet-0000000000000001")
        _content, upload = _upload(client, "raw-kept-2", headers=headers, session_id="S-ONE",
                                   turn_key="itm-0001#1")
        _mark_terminal(client, "raw-kept-2", reason="deleted")
        admin = _admin_client(client.test_engine, username="orphan-admin-kept2")
        try:
            _switch_live(admin, "S-ONE-B")
        finally:
            admin.close()
        moved = _pair(client, device_id="tablet-0000000000000001")
        response = client.put("/live/state", headers=moved, json={
            "kind": "audioDisposalConfirmed", "payload": {
                "code": "audio_terminal_disposition", "schemaVersion": 1,
                "action": "discard_local_copy", "reason": "deleted",
                "rawAudioId": "raw-kept-2", "sessionId": "S-ONE", "turnKey": "itm-0001#1",
                "byteCount": upload["bytes"], "checksum": upload["checksum"].lower(),
                "containsDirectIdentifier": False,
            }})
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "device_session_mismatch"


def test_new_pairing_path_yields_to_a_still_valid_pairing_on_the_old_session(
        disposition_client, monkeypatch):
    """这台平板在 A 上还有一把有效凭据(重新配回了 A):A 的录音该用它正常补传,
    拿 B 的凭据来报不作废。"""
    client = disposition_client
    _seed_sessions(client)
    _seed_second_session_for_patient(client, "S-ONE-B", "P-ONE")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    cap_a = _pair(client, device_id="tablet-0000000000000001")
    _register_only(client, "raw-orphan-1", cap_a, session_id="S-ONE")
    admin = _admin_client(client.test_engine, username="orphan-admin-valid-a")
    try:
        _switch_live(admin, "S-ONE-B")
        cap_b = _pair(client, device_id="tablet-0000000000000001")
        _switch_live(admin, "S-ONE")
        cap_a2 = _pair(client, device_id="tablet-0000000000000001")   # A 上又有了有效凭据
    finally:
        admin.close()
    response = client.put("/live/state", headers=cap_b,
                          json=_orphan_saved("raw-orphan-1", session_id="S-ONE"))
    assert response.status_code != 410, response.text
    with Session(client.test_engine) as session:
        assert session.get(AudioAssetRow, "raw-orphan-1").status == AudioStatus.recorded
    # A 的有效凭据照常能把字节传上来。
    uploaded = client.put("/audio/raw-orphan-1/blob", headers={**cap_a2, "content-type": "audio/webm"},
                          content=b"\x1a\x45\xdf\xa3raw-orphan-1")
    assert uploaded.status_code == 200, uploaded.text


def test_never_registered_capture_of_previous_session_gets_410_with_a_deleted_tombstone(
        disposition_client, monkeypatch):
    """登记请求当时就没到服务端(outbox 停在 captured):服务端一个事实都没有。同一位
    受试者、同一台平板换到新场次后回报它,同样 410,并落一条 deleted 墓碑行——删除回执、
    治理面板、重配回旧场次的重放都靠这一行(对抗复核 2026-09-13)。"""
    client = disposition_client
    old_headers, new_headers = _superseded_scene(client, monkeypatch)
    saved = _orphan_saved("raw-never-registered-1", session_id="S-ONE")
    response = client.put("/live/state", headers=new_headers, json=saved)
    assert response.status_code == 410, response.text
    assert response.json()["detail"] == {
        "code": "audio_terminal_disposition", "schemaVersion": 1,
        "action": "discard_local_copy", "reason": "deleted",
        "rawAudioId": "raw-never-registered-1", "sessionId": "S-ONE", "turnKey": "itm-0001#1",
        "byteCount": 4321, "checksum": "ab" * 32, "containsDirectIdentifier": False,
    }
    _assert_device_no_store(response)
    with Session(client.test_engine) as session:
        row = session.get(AudioAssetRow, "raw-never-registered-1")
        assert row is not None and row.session_id == "S-ONE"
        assert row.status == AudioStatus.deleted and row.delete_gate_passed is True
        assert row.checksum is None and row.byte_count is None and row.uploaded_at is None
        # 墓碑行存的是规范题位键(与正常登记同款),不是设备回报的不透明引用。
        assert row.turn_key == "SE_胡萝卜#1" and row.contains_direct_identifier is False
        audits = session.exec(select(AuditLog).where(
            AuditLog.action == "audio_capture_superseded")).all()
        assert len(audits) == 1 and "raw-never-registered-1" in audits[0].summary
        assert "unregistered=1" in audits[0].summary
    again = client.put("/live/state", headers=new_headers, json=saved)
    assert again.status_code == 410 and again.json()["detail"]["reason"] == "deleted"
    with Session(client.test_engine) as session:
        assert len(session.exec(select(AuditLog).where(
            AuditLog.action == "audio_capture_superseded")).all()) == 1
    confirmed = client.put("/live/state", headers=new_headers, json={
        "kind": "audioDisposalConfirmed", "payload": {
            "code": "audio_terminal_disposition", "schemaVersion": 1,
            "action": "discard_local_copy", "reason": "deleted",
            "rawAudioId": "raw-never-registered-1", "sessionId": "S-ONE", "turnKey": "itm-0001#1",
            "byteCount": 4321, "checksum": "ab" * 32, "containsDirectIdentifier": False,
        }})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["duplicate"] is False
    # 墓碑行不是登记:新场次照常登记同一题位不受它影响。
    fresh = client.post("/audio", headers=new_headers, json={
        "raw_audio_id": "raw-b-fresh-1", "session_id": "S-ONE-B", "turn_key": "itm-0001#1",
        "contains_direct_identifier": False})
    assert fresh.status_code == 200, fresh.text


@pytest.mark.parametrize("variant", ["other_device", "bad_turn_key", "bad_raw_id", "replay_without_row"])
def test_never_registered_capture_is_not_tombstoned_outside_the_exact_case(
        disposition_client, monkeypatch, variant):
    client = disposition_client
    old_headers, new_headers = _superseded_scene(client, monkeypatch)
    if variant == "other_device":
        headers = _pair(client, device_id="tablet-0000000000000002")
        saved = _orphan_saved("raw-never-registered-2", session_id="S-ONE")
    elif variant == "bad_turn_key":
        headers = new_headers
        saved = _orphan_saved("raw-never-registered-2", session_id="S-ONE", turn_key="itm-9999#1")
    elif variant == "bad_raw_id":
        headers = new_headers
        saved = _orphan_saved("raw never registered", session_id="S-ONE")
    else:
        # 重配回旧场次 A:没有已作废的行可重放,不凭空造墓碑。
        admin = _admin_client(client.test_engine, username="orphan-admin-back")
        try:
            _switch_live(admin, "S-ONE")
        finally:
            admin.close()
        headers = _pair(client, device_id="tablet-0000000000000001")
        saved = _orphan_saved("raw-never-registered-2", session_id="S-ONE")
    response = client.put("/live/state", headers=headers, json=saved)
    assert response.status_code != 410, response.text
    with Session(client.test_engine) as session:
        assert session.get(AudioAssetRow, "raw-never-registered-2") is None
        assert session.get(AudioAssetRow, "raw never registered") is None
        assert session.exec(select(AuditLog).where(
            AuditLog.action == "audio_capture_superseded")).all() == []


def test_never_registered_tombstones_are_bounded_by_the_registration_quota(
        disposition_client, monkeypatch):
    """墓碑行与正常登记同受配额:每题位上限到了就不再作废、不再造行(对抗复核 2026-09-13)。"""
    client = disposition_client
    monkeypatch.setenv("AUDIO_MAX_REGISTRATIONS_PER_TURN", "2")
    _, new_headers = _superseded_scene(client, monkeypatch)   # raw-orphan-1 已占 1 条
    first = client.put("/live/state", headers=new_headers,
                       json=_orphan_saved("raw-never-registered-q1", session_id="S-ONE"))
    assert first.status_code == 410, first.text
    second = client.put("/live/state", headers=new_headers,
                        json=_orphan_saved("raw-never-registered-q2", session_id="S-ONE"))
    assert second.status_code != 410, second.text
    with Session(client.test_engine) as session:
        assert session.get(AudioAssetRow, "raw-never-registered-q1") is not None
        assert session.get(AudioAssetRow, "raw-never-registered-q2") is None
        assert len(session.exec(select(AuditLog).where(
            AuditLog.action == "audio_capture_superseded")).all()) == 1
