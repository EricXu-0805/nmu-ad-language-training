import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

from app import audio_capture, audio_store, auth, db
from app.db import get_session
from app.main import app
from app.models import (
    AudioAssetRow, AudioCaptureReceipt, ResearchUser, SessionRuntimeState,
)


@pytest.fixture
def receipt_client(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'audio-receipts.sqlite'}",
        connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    client = TestClient(app)
    client.test_engine = engine
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
        client.close()


def _seed_session(client: TestClient, *, sid: str = "S-RECEIPT", pid: str = "P-RECEIPT") -> None:
    patient = client.post("/patients", json={
        "patient_id": pid,
        "is_simulation_subject": True,
        "consent_status": "已同意",
        "consent_type": "本人同意",
        "mandarin_eligible": True,
        "recording_allowed": True,
        "secondary_use_allowed": True,
    })
    assert patient.status_code == 200, patient.text
    session = client.post("/sessions", json={
        "session_id": sid,
        "patient_id": pid,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "item_bank_version_id": "wk2-v1-20260707",
        "is_simulation": True,
        "trainer_id": "RECEIPT-READER",
    })
    assert session.status_code == 200, session.text
    live = client.put("/live/state", json={
        "kind": "session",
        "payload": {
            "sessionId": sid,
            "weekNo": 2,
            "eventLine": "正式训练",
            "mode": "task",
            "itemBankVersionId": "wk2-v1-20260707",
        },
    })
    assert live.status_code == 200, live.text


def _register_upload(
        client: TestClient, raw_id: str, *, sid: str = "S-RECEIPT",
        turn_key: str = "SE_锚#1", payload: bytes | None = None,
        contains_identifier: bool = False) -> dict:
    body = payload or (b"\x1a\x45\xdf\xa3" + raw_id.encode())
    registered = client.post("/audio", json={
        "raw_audio_id": raw_id,
        "session_id": sid,
        "turn_key": turn_key,
        "contains_direct_identifier": contains_identifier,
    })
    assert registered.status_code == 200, registered.text
    uploaded = client.put(
        f"/audio/{raw_id}/blob", content=body,
        headers={"content-type": "audio/webm"})
    assert uploaded.status_code == 200, uploaded.text
    return uploaded.json()


def _report(
        client: TestClient, raw_id: str, upload: dict, *, sid: str = "S-RECEIPT",
        turn_key: str = "SE_锚#1", duration: float = 1.25,
        contains_identifier: bool = False):
    payload = {
        "rawAudioId": raw_id,
        "durationSeconds": duration,
        "byteCount": upload["bytes"],
        "checksum": upload["checksum"],
        "turnKey": turn_key,
        "sessionId": sid,
        "containsDirectIdentifier": contains_identifier,
    }
    return client.put("/live/state", json={"kind": "audioSaved", "payload": payload})


def _login_researcher(client: TestClient) -> None:
    with Session(client.test_engine) as session:
        session.add(ResearchUser(
            username="receipt-reader", display_id="RECEIPT-READER",
            password_hash=auth.hash_password("password1"), role="researcher"))
        session.commit()
    login = client.post("/auth/login", json={
        "username": "receipt-reader", "password": "password1"})
    assert login.status_code == 200, login.text


def test_checksum_null_or_tampered_file_cannot_be_reported(receipt_client):
    _seed_session(receipt_client)
    assert receipt_client.post("/audio", json={
        "raw_audio_id": "orphan", "session_id": "S-RECEIPT", "turn_key": "SE_锚#1",
    }).status_code == 200
    orphan = b"\x1a\x45\xdf\xa3orphan"
    path, checksum = audio_store.save_blob("orphan", orphan, "audio/webm")
    rejected = _report(receipt_client, "orphan", {
        "bytes": len(orphan), "checksum": checksum})
    assert rejected.status_code == 409
    assert "不一致" in rejected.json()["detail"] or "完整" in rejected.json()["detail"]

    # 同一字节重放可把崩溃窗口中的 checksum-null 孤儿安全收口，不覆盖原件。
    recovered = receipt_client.put(
        "/audio/orphan/blob", content=orphan, headers={"content-type": "audio/webm"})
    assert recovered.status_code == 200 and recovered.json()["idempotent"] is True
    assert path.read_bytes() == orphan
    assert _report(receipt_client, "orphan", recovered.json()).status_code == 200

    upload = _register_upload(
        receipt_client, "tampered", turn_key="SE_树#1")
    audio_store.find_blob("tampered").write_bytes(b"\x1a\x45\xdf\xa3changed")
    tampered = _report(
        receipt_client, "tampered", upload, turn_key="SE_树#1")
    assert tampered.status_code == 409
    with Session(receipt_client.test_engine) as session:
        assert session.exec(select(AudioCaptureReceipt).where(
            AudioCaptureReceipt.raw_audio_id == "tampered")).first() is None


def test_receipt_is_atomic_append_only_idempotent_and_conflicts_fail(receipt_client):
    _seed_session(receipt_client)
    upload = _register_upload(
        receipt_client, "receipt-one", contains_identifier=True)
    first = _report(
        receipt_client, "receipt-one", upload, contains_identifier=True)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["audioReceipt"]["serverSeq"] == 1
    assert first_body["audioReceipt"]["idempotent"] is False

    repeated = _report(
        receipt_client, "receipt-one", upload, contains_identifier=True)
    assert repeated.status_code == 200
    assert repeated.json()["seq"] == first_body["seq"]
    assert repeated.json()["audioReceipt"] == {
        "serverSeq": 1, "rawAudioId": "receipt-one", "idempotent": True}
    conflict = _report(
        receipt_client, "receipt-one", upload, duration=9.0,
        contains_identifier=True)
    assert conflict.status_code == 409

    with Session(receipt_client.test_engine) as session:
        receipts = list(session.exec(select(AudioCaptureReceipt)))
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt.byte_count == upload["bytes"]
        assert receipt.checksum == upload["checksum"]
        assert receipt.contains_direct_identifier is True
        receipt.duration_seconds = 9.0
        with pytest.raises(RuntimeError, match="只追加"):
            session.commit()
        session.rollback()
        receipt = session.get(AudioCaptureReceipt, 1)
        session.delete(receipt)
        with pytest.raises(RuntimeError, match="只追加"):
            session.commit()


def test_same_raw_upload_and_receipt_are_idempotent_under_real_concurrency(receipt_client):
    _seed_session(receipt_client)
    assert receipt_client.post("/audio", json={
        "raw_audio_id": "concurrent-one", "session_id": "S-RECEIPT",
        "turn_key": "SE_锚#1",
    }).status_code == 200
    raw = b"\x1a\x45\xdf\xa3concurrent-same-bytes"

    def upload_once():
        return receipt_client.put(
            "/audio/concurrent-one/blob", content=raw,
            headers={"content-type": "audio/webm"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        upload_responses = [future.result(timeout=10) for future in (
            pool.submit(upload_once), pool.submit(upload_once))]
    assert [response.status_code for response in upload_responses] == [200, 200]
    assert sorted(response.json()["idempotent"] for response in upload_responses) == [False, True]
    upload = upload_responses[0].json()

    def report_once():
        return _report(receipt_client, "concurrent-one", upload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipt_responses = [future.result(timeout=10) for future in (
            pool.submit(report_once), pool.submit(report_once))]
    assert [response.status_code for response in receipt_responses] == [200, 200]
    assert sorted(
        response.json()["audioReceipt"]["idempotent"]
        for response in receipt_responses) == [False, True]
    assert len({
        response.json()["audioReceipt"]["serverSeq"]
        for response in receipt_responses}) == 1
    with Session(receipt_client.test_engine) as session:
        assert len(list(session.exec(select(AudioCaptureReceipt)))) == 1
        row = session.get(AudioAssetRow, "concurrent-one")
        assert row.byte_count == len(raw) and row.checksum == upload["checksum"]


def test_lost_ack_replay_survives_terminal_and_session_switch_without_live_mutation(
        receipt_client):
    _seed_session(receipt_client)
    receipt_raw = b"\x1a\x45\xdf\xa3terminal-receipt"
    receipt_upload = _register_upload(
        receipt_client, "terminal-receipt", payload=receipt_raw,
        contains_identifier=True)
    first = _report(
        receipt_client, "terminal-receipt", receipt_upload,
        contains_identifier=True)
    assert first.status_code == 200, first.text
    first_server_seq = first.json()["audioReceipt"]["serverSeq"]

    upload_raw = b"\x1a\x45\xdf\xa3terminal-upload-only"
    upload_only = _register_upload(
        receipt_client, "terminal-upload-only", payload=upload_raw)

    next_session = receipt_client.post("/sessions", json={
        "session_id": "S-NEXT",
        "patient_id": "P-RECEIPT",
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "item_bank_version_id": "wk2-v1-20260707",
        "is_simulation": True,
    })
    assert next_session.status_code == 200, next_session.text
    with Session(receipt_client.test_engine) as session:
        session.add(SessionRuntimeState(
            session_id="S-RECEIPT", status="completed", revision=1))
        session.commit()
    switched = receipt_client.put("/live/state", json={
        "kind": "session",
        "payload": {
            "sessionId": "S-NEXT",
            "weekNo": 2,
            "eventLine": "正式训练",
            "mode": "task",
            "itemBankVersionId": "wk2-v1-20260707",
        },
    })
    assert switched.status_code == 200, switched.text
    live_before = receipt_client.get("/live/console-state").json()
    assert live_before["session"]["sessionId"] == "S-NEXT"
    assert live_before["audioSaved"] is None

    # 旧客户端可未声明直接标识字段；缺失不等于 false，改由
    # 不可变 receipt + asset row 互证。终态且已切场仍应只返 ACK。
    replay_payload = {
        "rawAudioId": "terminal-receipt",
        "durationSeconds": 1.25,
        "byteCount": receipt_upload["bytes"],
        "checksum": receipt_upload["checksum"],
        "turnKey": "SE_锚#1",
        "sessionId": "S-RECEIPT",
    }
    replay = receipt_client.put(
        "/live/state", json={"kind": "audioSaved", "payload": replay_payload})
    assert replay.status_code == 200, replay.text
    assert replay.json() == {
        "seq": live_before["seq"],
        "audioReceipt": {
            "serverSeq": first_server_seq,
            "rawAudioId": "terminal-receipt",
            "idempotent": True,
        },
    }
    live_after = receipt_client.get("/live/console-state").json()
    assert {
        key: live_after[key] for key in ("seq", "session", "audioSaved")
    } == {
        key: live_before[key] for key in ("seq", "session", "audioSaved")
    }

    # 显式说错 direct-id 或改动时长，都不得借幂等路径混过。
    wrong_identifier = receipt_client.put("/live/state", json={
        "kind": "audioSaved",
        "payload": {**replay_payload, "containsDirectIdentifier": False},
    })
    assert wrong_identifier.status_code == 409
    wrong_duration = receipt_client.put("/live/state", json={
        "kind": "audioSaved",
        "payload": {**replay_payload, "durationSeconds": 9.0},
    })
    assert wrong_duration.status_code == 409
    unchanged = receipt_client.get("/live/console-state").json()
    assert (unchanged["seq"], unchanged["session"], unchanged["audioSaved"]) == (
        live_before["seq"], live_before["session"], live_before["audioSaved"])

    # 登记和字节上传的 HTTP ACK 丢失也同理：完全相同可在切场后
    # 恢复 outbox；不同字节永远不覆盖原件。
    repeated_registration = receipt_client.post("/audio", json={
        "raw_audio_id": "terminal-upload-only",
        "session_id": "S-RECEIPT",
        "turn_key": "SE_锚#1",
        "contains_direct_identifier": False,
    })
    assert repeated_registration.status_code == 200, repeated_registration.text
    repeated_upload = receipt_client.put(
        "/audio/terminal-upload-only/blob", content=upload_raw,
        headers={"content-type": "audio/webm"})
    assert repeated_upload.status_code == 200, repeated_upload.text
    assert repeated_upload.json()["idempotent"] is True
    assert repeated_upload.json()["checksum"] == upload_only["checksum"]
    conflicting_upload = receipt_client.put(
        "/audio/terminal-upload-only/blob",
        content=b"\x1a\x45\xdf\xa3different-after-switch",
        headers={"content-type": "audio/webm"})
    assert conflicting_upload.status_code == 409
    final_live = receipt_client.get("/live/console-state").json()
    assert (final_live["seq"], final_live["session"], final_live["audioSaved"]) == (
        live_before["seq"], live_before["session"], live_before["audioSaved"])


def test_after_seq_returns_every_receipt_in_server_order_and_journal_includes_them(
        receipt_client):
    _seed_session(receipt_client)
    first_upload = _register_upload(receipt_client, "poll-one", turn_key="SE_锚#1")
    second_upload = _register_upload(
        receipt_client, "poll-two", turn_key="SE_树#1")
    assert _report(receipt_client, "poll-one", first_upload).status_code == 200
    assert _report(
        receipt_client, "poll-two", second_upload,
        turn_key="SE_树#1").status_code == 200

    # 新端点即使开发环境默认开放，也坚持具名账号边界。
    assert receipt_client.get(
        "/sessions/S-RECEIPT/audio-receipts?after_seq=0").status_code == 403
    _login_researcher(receipt_client)
    page = receipt_client.get(
        "/sessions/S-RECEIPT/audio-receipts?after_seq=0")
    assert page.status_code == 200, page.text
    rows = page.json()["receipts"]
    assert [row["raw_audio_id"] for row in rows] == ["poll-one", "poll-two"]
    assert [row["server_seq"] for row in rows] == sorted(
        row["server_seq"] for row in rows)
    between = receipt_client.get(
        f"/sessions/S-RECEIPT/audio-receipts?after_seq={rows[0]['server_seq']}").json()
    assert [row["raw_audio_id"] for row in between["receipts"]] == ["poll-two"]
    assert receipt_client.get(
        f"/sessions/S-RECEIPT/audio-receipts?after_seq={rows[-1]['server_seq']}").json()[
            "receipts"] == []
    journal = receipt_client.get("/sessions/S-RECEIPT/journal").json()
    assert [row["raw_audio_id"] for row in journal["audio_receipts"]] == [
        "poll-one", "poll-two"]


def test_registration_byte_and_process_concurrency_quotas_are_fail_closed(
        receipt_client, monkeypatch):
    _seed_session(receipt_client)
    monkeypatch.setenv(audio_capture.MAX_REGISTRATIONS_PER_TURN_ENV, "1")
    monkeypatch.setenv(audio_capture.MAX_ASSETS_PER_SESSION_ENV, "2")
    first_registration = {
        "raw_audio_id": "quota-one", "session_id": "S-RECEIPT", "turn_key": "SE_锚#1"}
    assert receipt_client.post("/audio", json=first_registration).status_code == 200
    assert receipt_client.post("/audio", json=first_registration).status_code == 200
    assert receipt_client.post("/audio", json={
        **first_registration, "raw_audio_id": "quota-turn-over"}).status_code == 409
    second_registration = {
        "raw_audio_id": "quota-two", "session_id": "S-RECEIPT",
        "turn_key": "SE_树#1"}
    assert receipt_client.post("/audio", json=second_registration).status_code == 200
    assert receipt_client.post("/audio", json={
        "raw_audio_id": "quota-session-over", "session_id": "S-RECEIPT",
        "turn_key": "SE_树#1"}).status_code == 409

    first_bytes = b"\x1a\x45\xdf\xa3quota-one"
    first = receipt_client.put(
        "/audio/quota-one/blob", content=first_bytes,
        headers={"content-type": "audio/webm"})
    assert first.status_code == 200
    monkeypatch.setenv(
        audio_capture.MAX_BYTES_PER_SESSION_ENV, str(len(first_bytes) + 2))
    # 强制覆盖预检以验证“发布后最终配额失败”会删除刚发布字节、且不污染 DB。
    monkeypatch.setattr(audio_capture, "assert_declared_byte_quota", lambda *_args: None)
    over = receipt_client.put(
        "/audio/quota-two/blob", content=b"\x1a\x45\xdf\xa3quota-two",
        headers={"content-type": "audio/webm"})
    assert over.status_code == 409
    assert audio_store.find_blob("quota-two") is None
    with Session(receipt_client.test_engine) as session:
        row = session.get(AudioAssetRow, "quota-two")
        assert row.checksum is None and row.byte_count is None and row.uploaded_at is None

    monkeypatch.setenv(audio_capture.MAX_CONCURRENT_UPLOADS_ENV, "1")
    with audio_capture.upload_slot():
        busy = receipt_client.put(
            "/audio/quota-two/blob", content=b"\x1a\x45\xdf\xa3busy",
            headers={"content-type": "audio/webm"})
    assert busy.status_code == 429 and busy.headers["retry-after"] == "2"
    assert audio_store.find_blob("quota-two") is None


def test_streaming_limit_and_source_failure_leave_no_temp_or_final_files():
    async def too_large():
        yield b"\x1a\x45\xdf\xa3"
        yield b"0123456789"

    with pytest.raises(audio_store.AudioBlobTooLarge):
        asyncio.run(audio_store.save_blob_stream_atomic(
            "stream-over", too_large(), "audio/webm", max_bytes=8))
    assert audio_store.find_blob("stream-over") is None
    assert not list(audio_store.AUDIO_DIR.glob(".stream-over.*.pending"))

    async def fails_mid_stream():
        yield b"\x1a\x45\xdf\xa3partial"
        raise RuntimeError("client disconnected")

    with pytest.raises(RuntimeError, match="disconnected"):
        asyncio.run(audio_store.save_blob_stream_atomic(
            "stream-fail", fails_mid_stream(), "audio/webm"))
    assert audio_store.find_blob("stream-fail") is None
    assert not list(audio_store.AUDIO_DIR.glob(".stream-fail.*.pending"))


def test_migration_preserves_old_audio_rows_without_inventing_upload_facts(tmp_path):
    db_path = tmp_path / "audio-receipt-migration.sqlite"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "c2e8a4d7f901")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO patient (patient_id, is_simulation_subject) "
            "VALUES ('P-OLD-AUDIO', 1)"))
        connection.execute(text(
            "INSERT INTO session "
            "(session_id, patient_id, session_sitting_no, week_no, phase_type, event_line, "
            " item_bank_version_id, is_simulation, data_classification) "
            "VALUES ('S-OLD-AUDIO', 'P-OLD-AUDIO', 1, 2, '正式训练', '正式训练', "
            " 'wk2-v1-20260707', 1, 'simulation')"))
        connection.execute(text(
            "INSERT INTO audioassetrow "
            "(raw_audio_id, session_id, is_simulation, data_classification, turn_key, "
            " audio_format, status, is_reliability_sample, withdrawn, checksum, "
            " contains_direct_identifier, delete_gate_passed) "
            "VALUES ('old-audio', 'S-OLD-AUDIO', 1, 'simulation', 'SE_锚#1', "
            " 'webm', 'recorded', 0, 0, :checksum, 0, 0)"),
            {"checksum": "a" * 64})

    command.upgrade(config, "head")
    columns = {column["name"] for column in inspect(engine).get_columns("audioassetrow")}
    assert {"byte_count", "uploaded_at"} <= columns
    assert "audiocapturereceipt" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        old = connection.execute(text(
            "SELECT checksum, byte_count, uploaded_at, turn_key "
            "FROM audioassetrow WHERE raw_audio_id='old-audio'" )).one()
        count = connection.execute(text(
            "SELECT count(*) FROM audiocapturereceipt")).scalar_one()
    assert old.checksum == "a" * 64 and old.turn_key == "SE_锚#1"
    assert old.byte_count is None and old.uploaded_at is None and count == 0
