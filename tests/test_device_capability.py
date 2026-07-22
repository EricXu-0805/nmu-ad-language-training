"""Session-bound patient-device capability and recovery concurrency checks."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime
import hashlib
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app import (audio_capture, audio_store, auth, db, device_capability,
                 main as main_module)
from app.enums import AudioStatus
from app.main import app
from app.models import (AudioAssetRow, AudioCaptureReceipt,
                        PatientDeviceCapability, ResearchUser)


@pytest.fixture
def capability_client(tmp_path, monkeypatch):
    # File-backed SQLite gives concurrent requests independent connections while
    # remaining wholly isolated from the developer/default database.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'capability.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 1},
    )
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    client = TestClient(app)
    client.test_engine = engine
    yield client
    client.close()
    engine.dispose()


def _seed_two_sessions(client: TestClient) -> None:
    for suffix in ("ONE", "TWO"):
        assert client.post("/patients", json={
            "patient_id": f"P-{suffix}",
            "consent_status": "已同意",
            "consent_type": "本人同意",
            "mandarin_eligible": True,
            "recording_allowed": True,
            "is_simulation_subject": True,
            "secondary_use_allowed": True,
        }).status_code == 200
        assert client.post("/sessions", json={
            "session_id": f"S-{suffix}",
            "patient_id": f"P-{suffix}",
            "week_no": 2,
            "phase_type": "正式训练",
            "event_line": "正式训练",
            "item_bank_version_id": "wk2-v1-20260707",
            "is_simulation": True,
        }).status_code == 200
    _switch_live(client, "S-ONE")


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


def _pair(client: TestClient, device_id: str) -> tuple[dict[str, str], dict]:
    response = client.post("/device/pair", headers={"X-Console-Pin": "246810"}, json={
        "deviceId": device_id,
    })
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    body = response.json()
    assert set(body) == {"capability", "sessionId", "expiresAt"}
    assert body["expiresAt"].endswith("Z")
    return {"X-Device-Capability": body["capability"]}, body


def _admin_client(engine) -> TestClient:
    with Session(engine) as session:
        session.add(ResearchUser(
            username="admin-cap",
            display_id="ADMIN-CAP",
            password_hash=auth.hash_password("password1"),
            role="admin",
            created_at=datetime.now(),
        ))
        session.commit()
    client = TestClient(app)
    login = client.post("/auth/login", json={
        "username": "admin-cap", "password": "password1",
    })
    assert login.status_code == 200, login.text
    client.headers["X-CSRF-Token"] = client.cookies.get(auth.CSRF_COOKIE_NAME)
    return client


def _register_and_upload(client: TestClient, capability: dict[str, str], raw_id: str):
    registration = {
        "raw_audio_id": raw_id,
        "session_id": "S-ONE",
        "turn_key": "itm-0001#1",
    }
    created = client.post("/audio", headers=capability, json=registration)
    assert created.status_code == 200, created.text
    content = b"\x1a\x45\xdf\xa3device-capability-audio"
    uploaded = client.put(
        f"/audio/{raw_id}/blob",
        headers={**capability, "content-type": "audio/webm"},
        content=content,
    )
    assert uploaded.status_code == 200, uploaded.text
    return registration, content, uploaded.json()


def test_pair_hashes_token_and_switch_back_never_reactivates_old_capability(
        capability_client, monkeypatch):
    _seed_two_sessions(capability_client)
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    first_headers, first = _pair(capability_client, "device-capability-000001")

    with Session(capability_client.test_engine) as session:
        row = session.exec(select(PatientDeviceCapability)).one()
        assert row.token_hash == hashlib.sha256(
            first["capability"].encode("ascii")).hexdigest()
        assert row.token_hash != first["capability"]
        assert row.device_id_hash == hashlib.sha256(
            b"device-capability-000001").hexdigest()
        assert row.session_id == row.active_session_key == "S-ONE"

    registration = {
        "raw_audio_id": "recovery-registration",
        "session_id": "S-ONE",
        "turn_key": "itm-0001#1",
    }
    assert capability_client.post(
        "/audio", headers=first_headers, json=registration).status_code == 200

    admin = _admin_client(capability_client.test_engine)
    try:
        _switch_live(admin, "S-TWO")
        denied = capability_client.get("/live/state", headers=first_headers)
        assert denied.status_code == 401
        assert denied.json()["code"] == "device_capability_recovery_only"
        # Exact old fact is recoverable, but no new fact can be created.
        assert capability_client.post(
            "/audio", headers=first_headers, json=registration).status_code == 200
        new_fact = capability_client.post("/audio", headers=first_headers, json={
            **registration, "raw_audio_id": "recovery-new-forbidden",
        })
        assert new_fact.status_code == 409
        assert new_fact.json()["detail"]["code"] == "device_capability_recovery_only"

        _switch_live(admin, "S-ONE")
        # Switching back never promotes the old recovery token.
        assert capability_client.get("/live/state", headers=first_headers).status_code == 401
        replacement_headers, _ = _pair(
            capability_client, "device-capability-000002")
        revoked = capability_client.get("/live/state", headers=first_headers)
        assert revoked.status_code == 401
        assert revoked.json()["code"] == "device_capability_revoked"
        assert capability_client.get(
            "/live/state", headers=replacement_headers).status_code == 200
    finally:
        admin.close()


def test_stalled_staging_does_not_block_delete_or_resurrect_voice(
        capability_client, monkeypatch):
    _seed_two_sessions(capability_client)
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    monkeypatch.setenv("ENABLE_AUDIO_DELETE", "1")
    # Concurrency-only test: immutable export authority has dedicated API tests.
    monkeypatch.setattr(
        main_module, "_require_authoritative_export_copy", lambda *_args: None)
    capability, _ = _pair(capability_client, "device-delete-race-00001")
    _registration, content, uploaded = _register_and_upload(
        capability_client, capability, "delete-race-audio")

    # Make the existing blob administratively deletable; the replay is an exact
    # ACK-loss recovery and therefore reaches the staged path.
    with Session(capability_client.test_engine) as session:
        row = session.get(AudioAssetRow, "delete-race-audio")
        row.status = AudioStatus.deletable
        session.add(row)
        session.commit()

    admin = _admin_client(capability_client.test_engine)
    original_stage = audio_store.stage_blob_stream
    staged = threading.Event()
    release = threading.Event()

    async def delayed_stage(*args, **kwargs):
        pending = await original_stage(*args, **kwargs)
        staged.set()
        await asyncio.to_thread(release.wait, 5)
        return pending

    monkeypatch.setattr(audio_store, "stage_blob_stream", delayed_stage)
    result: dict[str, object] = {}

    def replay_upload() -> None:
        result["response"] = capability_client.put(
            "/audio/delete-race-audio/blob",
            headers={**capability, "content-type": "audio/webm"},
            content=content,
        )

    worker = threading.Thread(target=replay_upload, daemon=True)
    worker.start()
    assert staged.wait(3), "upload did not reach hidden staging"
    started = time.monotonic()
    try:
        deleted = admin.delete("/audio/delete-race-audio?source=manual&session_id=S-ONE")
        assert deleted.status_code == 200, deleted.text
        # A leaked SQLite read transaction would hit the configured one-second DB
        # timeout instead of committing promptly while the request body is paused.
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
        worker.join(5)
        admin.close()
    assert not worker.is_alive()
    replay = result["response"]
    assert replay.status_code == 409, replay.text
    assert audio_store.find_blob("delete-race-audio") is None
    assert not list(audio_store.AUDIO_DIR.glob(".delete-race-audio.*.pending"))
    with Session(capability_client.test_engine) as session:
        row = session.get(AudioAssetRow, "delete-race-audio")
        assert row.status == AudioStatus.deleted
        # Historical DB upload facts remain audit evidence; physical voice bytes do not.
        assert row.checksum == uploaded["checksum"]


def test_terminal_audio_receipt_returns_disposition_but_blob_upload_stays_rejected(
        capability_client, monkeypatch):
    _seed_two_sessions(capability_client)
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    capability, _ = _pair(capability_client, "device-terminal-000001")
    _registration, content, uploaded = _register_and_upload(
        capability_client, capability, "terminal-audio")
    audio_saved = {
        "kind": "audioSaved",
        "payload": {
            "rawAudioId": "terminal-audio",
            "durationSeconds": 1,
            "byteCount": uploaded["bytes"],
            "checksum": uploaded["checksum"],
            "containsDirectIdentifier": False,
            "turnKey": "itm-0001#1",
            "sessionId": "S-ONE",
        },
    }
    assert capability_client.put(
        "/live/state", headers=capability, json=audio_saved).status_code == 200
    with Session(capability_client.test_engine) as session:
        row = session.get(AudioAssetRow, "terminal-audio")
        row.withdrawn = True
        session.add(row)
        session.commit()
    assert audio_store.find_blob("terminal-audio") is not None

    receipt_ack = capability_client.put(
        "/live/state", headers=capability, json=audio_saved)
    assert receipt_ack.status_code == 410
    assert receipt_ack.json()["detail"]["code"] == "audio_terminal_disposition"
    assert receipt_ack.json()["detail"]["action"] == "discard_local_copy"
    blob_ack = capability_client.put(
        "/audio/terminal-audio/blob",
        headers={**capability, "content-type": "audio/webm"},
        content=content,
    )
    assert blob_ack.status_code == 409


def test_explicit_bad_or_recovery_capability_never_falls_back_to_account(
        capability_client, monkeypatch):
    _seed_two_sessions(capability_client)
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    active, _ = _pair(capability_client, "device-deputy-0000001")
    assert capability_client.post("/audio", headers=active, json={
        "raw_audio_id": "deputy-unuploaded",
        "session_id": "S-ONE",
        "turn_key": "itm-0001#1",
    }).status_code == 200
    admin = _admin_client(capability_client.test_engine)
    try:
        invalid = {"X-Device-Capability": "x" * 43}
        invalid_cases = (
            admin.get("/live/state", headers=invalid),
            admin.post("/audio", headers=invalid, json={
                "raw_audio_id": "deputy-invalid", "session_id": "S-ONE",
                "turn_key": "itm-0001#1"}),
            admin.put("/audio/deputy-unuploaded/blob", headers={
                **invalid, "content-type": "audio/webm"},
                content=b"\x1a\x45\xdf\xa3invalid"),
            admin.put("/live/state", headers=invalid, json={
                "kind": "patientRec", "payload": {
                    "active": False, "turnKey": "itm-0001#1", "sessionId": "S-ONE"}}),
        )
        assert all(response.status_code == 401 for response in invalid_cases)
        assert all(response.json()["code"] == "device_capability_invalid"
                   for response in invalid_cases)

        _switch_live(admin, "S-TWO")
        recovery_cases = (
            admin.get("/live/state", headers=active),
            admin.post("/audio", headers=active, json={
                "raw_audio_id": "deputy-recovery-new", "session_id": "S-ONE",
                "turn_key": "itm-0001#1"}),
            admin.put("/audio/deputy-unuploaded/blob", headers={
                **active, "content-type": "audio/webm"},
                content=b"\x1a\x45\xdf\xa3recovery"),
            admin.put("/live/state", headers=active, json={
                "kind": "patientRec", "payload": {
                    "active": False, "turnKey": "itm-0001#1", "sessionId": "S-ONE"}}),
        )
        assert all(response.status_code in {401, 409} for response in recovery_cases)
        assert all("recovery_only" in response.text for response in recovery_cases)
        # With no explicit cap, the same account remains usable; the bad bearer was
        # never silently upgraded to this authority.
        assert admin.get("/live/state").status_code == 200
    finally:
        admin.close()


def test_successful_upload_then_admin_delete_ends_deleted_with_no_physical_blob(
        capability_client, monkeypatch):
    _seed_two_sessions(capability_client)
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    monkeypatch.setenv("ENABLE_AUDIO_DELETE", "1")
    monkeypatch.setattr(
        main_module, "_require_authoritative_export_copy", lambda *_args: None)
    capability, _ = _pair(capability_client, "device-upload-wins-0001")
    _register_and_upload(capability_client, capability, "upload-wins-audio")
    with Session(capability_client.test_engine) as session:
        row = session.get(AudioAssetRow, "upload-wins-audio")
        row.status = AudioStatus.deletable
        session.add(row)
        session.commit()
    admin = _admin_client(capability_client.test_engine)
    try:
        deleted = admin.delete("/audio/upload-wins-audio?source=manual&session_id=S-ONE")
        assert deleted.status_code == 200, deleted.text
    finally:
        admin.close()
    assert audio_store.find_blob("upload-wins-audio") is None
    with Session(capability_client.test_engine) as session:
        assert session.get(AudioAssetRow, "upload-wins-audio").status == AudioStatus.deleted


def test_audio_saved_receipt_and_delete_are_raw_id_linearized(
        capability_client, monkeypatch):
    _seed_two_sessions(capability_client)
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    monkeypatch.setenv("ENABLE_AUDIO_DELETE", "1")
    monkeypatch.setattr(
        main_module, "_require_authoritative_export_copy", lambda *_args: None)
    capability, _ = _pair(capability_client, "device-receipt-race-001")
    admin = _admin_client(capability_client.test_engine)

    def prepare(raw_id: str):
        _registration, _content, uploaded = _register_and_upload(
            capability_client, capability, raw_id)
        with Session(capability_client.test_engine) as session:
            row = session.get(AudioAssetRow, raw_id)
            row.status = AudioStatus.deletable
            session.add(row)
            session.commit()
        return {
            "kind": "audioSaved",
            "payload": {
                "rawAudioId": raw_id,
                "durationSeconds": 1,
                "byteCount": uploaded["bytes"],
                "checksum": uploaded["checksum"],
                "containsDirectIdentifier": False,
                "turnKey": "itm-0001#1",
                "sessionId": "S-ONE",
            },
        }

    try:
        # Receipt owns raw-id first: DELETE waits, then runs after receipt/live commit
        # and remains the final state with no physical bytes.
        receipt_payload = prepare("receipt-wins-race")
        original_append = audio_capture.append_receipt
        append_entered = threading.Event()
        append_release = threading.Event()

        def delayed_append(*args, **kwargs):
            append_entered.set()
            assert append_release.wait(3)
            return original_append(*args, **kwargs)

        monkeypatch.setattr(audio_capture, "append_receipt", delayed_append)
        receipt_result: dict[str, object] = {}
        receipt_thread = threading.Thread(target=lambda: receipt_result.update(
            response=capability_client.put(
                "/live/state", headers=capability, json=receipt_payload)), daemon=True)
        receipt_thread.start()
        assert append_entered.wait(3)
        timer = threading.Timer(0.1, append_release.set)
        timer.start()
        deleted = admin.delete("/audio/receipt-wins-race?source=manual&session_id=S-ONE")
        receipt_thread.join(5)
        timer.cancel()
        assert receipt_result["response"].status_code == 200
        assert deleted.status_code == 200, deleted.text
        assert audio_store.find_blob("receipt-wins-race") is None

        # DELETE owns raw-id first: the late report waits, then proves the exact
        # terminal disposition without committing a receipt for missing voice bytes.
        monkeypatch.setattr(audio_capture, "append_receipt", original_append)
        delete_payload = prepare("delete-wins-receipt-race")
        original_delete_blob = audio_store.delete_blob
        delete_entered = threading.Event()
        delete_release = threading.Event()

        def delayed_delete_blob(*args, **kwargs):
            delete_entered.set()
            assert delete_release.wait(3)
            return original_delete_blob(*args, **kwargs)

        monkeypatch.setattr(audio_store, "delete_blob", delayed_delete_blob)
        delete_result: dict[str, object] = {}
        delete_thread = threading.Thread(target=lambda: delete_result.update(
            response=admin.delete(
                "/audio/delete-wins-receipt-race?source=manual&session_id=S-ONE")), daemon=True)
        delete_thread.start()
        assert delete_entered.wait(3)
        late_result: dict[str, object] = {}
        late_thread = threading.Thread(target=lambda: late_result.update(
            response=capability_client.put(
                "/live/state", headers=capability, json=delete_payload)), daemon=True)
        late_thread.start()
        delete_release.set()
        delete_thread.join(5)
        late_thread.join(5)
        assert delete_result["response"].status_code == 200
        assert late_result["response"].status_code == 410
        assert late_result["response"].json()["detail"]["code"] == (
            "audio_terminal_disposition")
        assert audio_store.find_blob("delete-wins-receipt-race") is None
        with Session(capability_client.test_engine) as session:
            assert session.exec(select(AudioCaptureReceipt).where(
                AudioCaptureReceipt.raw_audio_id == "delete-wins-receipt-race"
            )).first() is None
    finally:
        admin.close()


def test_create_audio_and_audio_saved_obey_global_lock_order_under_barrier(
        capability_client, monkeypatch):
    """Concurrent endpoints must both follow registration -> live -> capability.

    The barrier places POST /audio at the registration boundary while audioSaved
    reaches the live boundary.  The rank checker is deliberately evaluated before
    a real lock acquisition: the former cap -> live implementation therefore fails
    deterministically instead of leaving two test workers deadlocked.
    """
    _seed_two_sessions(capability_client)
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    capability, _ = _pair(capability_client, "device-lock-order-0001")
    _registration, _content, uploaded = _register_and_upload(
        capability_client, capability, "lock-order-receipt")
    audio_saved = {
        "kind": "audioSaved",
        "payload": {
            "rawAudioId": "lock-order-receipt",
            "durationSeconds": 1,
            "byteCount": uploaded["bytes"],
            "checksum": uploaded["checksum"],
            "containsDirectIdentifier": False,
            "turnKey": "itm-0001#1",
            "sessionId": "S-ONE",
        },
    }
    registration = {
        "raw_audio_id": "lock-order-registration",
        "session_id": "S-ONE",
        "turn_key": "itm-0001#1",
    }

    original_registration = audio_capture.registration_lock
    original_live = main_module._LIVE_WRITE_LOCK
    original_capability = device_capability.serialized_mutation
    rendezvous = threading.Barrier(2, timeout=3)
    rank_state = threading.local()
    observations: list[tuple[int, str, int]] = []
    observation_lock = threading.Lock()
    live_calls = 0

    def enter_rank(name: str, rank: int) -> None:
        stack = getattr(rank_state, "stack", [])
        if stack and rank < stack[-1][1]:
            raise AssertionError(
                f"lock order inversion: {stack[-1][0]} -> {name}")
        stack.append((name, rank))
        rank_state.stack = stack
        with observation_lock:
            observations.append((threading.get_ident(), name, rank))

    def exit_rank(name: str) -> None:
        stack = getattr(rank_state, "stack", [])
        assert stack and stack[-1][0] == name
        stack.pop()

    @contextmanager
    def ordered_registration():
        enter_rank("registration", 1)
        try:
            with original_registration():
                # POST /audio cannot proceed to live until audioSaved has reached
                # that boundary; no timing sleeps or scheduler assumptions needed.
                rendezvous.wait()
                yield
        finally:
            exit_rank("registration")

    class OrderedLiveLock:
        def __enter__(self):
            nonlocal live_calls
            with observation_lock:
                call_index = live_calls
                live_calls += 1
            # POST /audio is still inside ordered_registration, so the first live
            # entrant is necessarily the concurrent audioSaved request.
            if call_index == 0:
                rendezvous.wait()
            enter_rank("live", 2)
            original_live.acquire()
            return self

        def __exit__(self, exc_type, exc, traceback):
            original_live.release()
            exit_rank("live")
            return False

    @contextmanager
    def ordered_capability():
        enter_rank("capability", 3)
        try:
            with original_capability():
                yield
        finally:
            exit_rank("capability")

    monkeypatch.setattr(audio_capture, "registration_lock", ordered_registration)
    monkeypatch.setattr(main_module, "_LIVE_WRITE_LOCK", OrderedLiveLock())
    monkeypatch.setattr(
        device_capability, "serialized_mutation", ordered_capability)

    responses: dict[str, object] = {}
    errors: dict[str, BaseException] = {}

    def request(name: str, fn) -> None:
        try:
            responses[name] = fn()
        except BaseException as exc:  # surfaced lock-order assertion belongs here
            errors[name] = exc

    create_thread = threading.Thread(
        target=request,
        args=("create", lambda: capability_client.post(
            "/audio", headers=capability, json=registration)),
        daemon=True,
    )
    receipt_thread = threading.Thread(
        target=request,
        args=("receipt", lambda: capability_client.put(
            "/live/state", headers=capability, json=audio_saved)),
        daemon=True,
    )
    create_thread.start()
    receipt_thread.start()
    create_thread.join(5)
    receipt_thread.join(5)

    assert not create_thread.is_alive() and not receipt_thread.is_alive()
    assert errors == {}
    assert responses["create"].status_code == 200, responses["create"].text
    assert responses["receipt"].status_code == 200, responses["receipt"].text

    by_worker: dict[int, list[int]] = {}
    for worker, _name, rank in observations:
        by_worker.setdefault(worker, []).append(rank)
    assert sorted(by_worker.values()) == [[1, 2, 3], [2, 3]]


def test_baseexception_after_publish_removes_uncommitted_voice_bytes(
        capability_client, monkeypatch):
    _seed_two_sessions(capability_client)
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    capability, _ = _pair(capability_client, "device-cancel-0000001")
    created = capability_client.post("/audio", headers=capability, json={
        "raw_audio_id": "cancel-after-publish",
        "session_id": "S-ONE",
        "turn_key": "itm-0001#1",
    })
    assert created.status_code == 200, created.text

    class SimulatedCancellation(BaseException):
        pass

    original_fsync = audio_store.os.fsync
    fsync_calls = 0

    def cancel_during_publish(fd):
        nonlocal fsync_calls
        fsync_calls += 1
        # First fsync seals the hidden pending file.  The second follows hard-link
        # publication and simulates worker cancellation before a SaveResult/DB commit.
        if fsync_calls == 2:
            raise SimulatedCancellation()
        return original_fsync(fd)

    monkeypatch.setattr(audio_store.os, "fsync", cancel_during_publish)
    # Starlette's task-group middleware wraps endpoint BaseException on Python
    # 3.14; the storage assertions below are the security property under test.
    with pytest.raises(BaseException):
        capability_client.put(
            "/audio/cancel-after-publish/blob",
            headers={**capability, "content-type": "audio/webm"},
            content=b"\x1a\x45\xdf\xa3cancelled-audio",
        )
    assert audio_store.find_blob("cancel-after-publish") is None
    assert not list(audio_store.AUDIO_DIR.glob(".cancel-after-publish.*.pending"))
    with Session(capability_client.test_engine) as session:
        row = session.get(AudioAssetRow, "cancel-after-publish")
        assert row.checksum is None and row.byte_count is None and row.uploaded_at is None


def test_audio_rollback_cleanup_exception_logs_only_stable_code(
        capability_client, monkeypatch, capsys):
    _seed_two_sessions(capability_client)
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    capability, _ = _pair(capability_client, "device-log-privacy-0001")
    raw_id = "raw-audio-sentinel-0001"
    created = capability_client.post("/audio", headers=capability, json={
        "raw_audio_id": raw_id,
        "session_id": "S-ONE",
        "turn_key": "itm-0001#1",
    })
    assert created.status_code == 200, created.text

    original_publish = audio_store.publish_staged_blob
    original_blob_facts = audio_store.blob_facts
    published = False

    def publish_then_mark(*args, **kwargs):
        nonlocal published
        result = original_publish(*args, **kwargs)
        published = True
        return result

    def fail_post_publish_verification(target_raw_id):
        if published:
            raise RuntimeError(
                "ORIGINAL-EXCEPTION patient=P-SENTINEL session=S-SENTINEL "
                "token=tok-sentinel path=/private/original.wav"
            )
        return original_blob_facts(target_raw_id)

    def fail_cleanup(*_args, **_kwargs):
        raise RuntimeError(
            "CLEANUP-EXCEPTION turn=T-SENTINEL path=/private/cleanup.wav"
        )

    monkeypatch.setattr(audio_store, "publish_staged_blob", publish_then_mark)
    monkeypatch.setattr(audio_store, "blob_facts", fail_post_publish_verification)
    monkeypatch.setattr(audio_store, "delete_blob_if_matches", fail_cleanup)

    # The original post-publication error still escapes; cleanup remains
    # best-effort and may not replace or mask that primary failure.
    with pytest.raises(BaseException):
        capability_client.put(
            f"/audio/{raw_id}/blob",
            headers={**capability, "content-type": "audio/webm"},
            content=b"\x1a\x45\xdf\xa3privacy-regression-audio",
        )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == "[ops] code=audio_rollback_cleanup_failed\n"
    assert raw_id not in captured.out
    assert "ORIGINAL-EXCEPTION" not in captured.out
    assert "CLEANUP-EXCEPTION" not in captured.out
    assert not list(audio_store.AUDIO_DIR.glob(f".{raw_id}.*.pending"))
    with Session(capability_client.test_engine) as session:
        row = session.get(AudioAssetRow, raw_id)
        assert row.checksum is None and row.byte_count is None and row.uploaded_at is None
