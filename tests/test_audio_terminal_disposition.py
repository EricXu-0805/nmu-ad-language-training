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


def _pair(client: TestClient, *, device_id: str = "terminal-device-000001") -> dict[str, str]:
    response = client.post(
        "/device/pair",
        headers={"X-Console-Pin": "246810"},
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
    monkeypatch.setenv("CONSOLE_PIN", "246810")
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
    monkeypatch.setenv("CONSOLE_PIN", "246810")
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
    monkeypatch.setenv("CONSOLE_PIN", "246810")
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
    monkeypatch.setenv("CONSOLE_PIN", "246810")
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
    monkeypatch.setenv("CONSOLE_PIN", "246810")
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
    monkeypatch.setenv("CONSOLE_PIN", "246810")
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
