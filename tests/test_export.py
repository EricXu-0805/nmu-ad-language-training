import csv
import hashlib
import json
import re
from uuid import uuid4
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import (audio_store, autopilot_service, export, export_security,
                 repeat_intent)
from app.enums import AudioStatus
from app.export import DIRECT_IDENTIFIER_COLUMNS, mask_text, pseudonymize
from app.models import (
    AbnormalEvent, AttemptCaptureProcessing, AttemptEvent, AudioAssetRow,
    AudioCaptureReceipt,
    AutopilotRepeatRequest, ExportArtifact, ExportBatch, ItemEvent, Patient,
    PatientDeviceCapability, RuntimeCommand, RuntimeCommandAck, ScaleResult,
    Session as TrainSession, SessionCloseoutReport,
    SessionOutcomeSummary, SessionRuntimeState, TurnEvent,
)


@pytest.fixture
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def _export_bundle(db, session_id: str, **kwargs):
    """Call the internal export boundary with a unique authenticated test intent."""
    kwargs.setdefault("idempotency_key", f"export-test-{uuid4().hex}")
    kwargs.setdefault("actor_display_id", "TEST-DATA-STEWARD")
    kwargs.setdefault("actor_role", "data_steward")
    return export.export_session_bundle(db, session_id, **kwargs)


def _prepare_artifacts_ready(db, tmp_path, monkeypatch, key: str) -> str:
    original_commit = db.commit
    calls = 0

    def fail_before_final_commit():
        nonlocal calls
        calls += 1
        if calls == 5:
            raise RuntimeError("simulated final commit ambiguity")
        return original_commit()

    monkeypatch.setattr(db, "commit", fail_before_final_commit)
    with pytest.raises(RuntimeError, match="artifacts_ready"):
        _export_bundle(db, "S9", write_dir=tmp_path, idempotency_key=key)
    monkeypatch.setattr(db, "commit", original_commit)
    batch = db.exec(export.select(ExportBatch)).first()
    assert batch is not None and batch.status == "artifacts_ready"
    return batch.batch_id


def _leave_preintent_crash(db, tmp_path, monkeypatch, key: str) -> ExportBatch:
    """Simulate process death after all files but before manifest intent."""
    original_record_intent = export._record_manifest_intent

    def crash_before_manifest_intent(*_args, **_kwargs):
        raise SystemExit("simulated worker death before manifest intent")

    monkeypatch.setattr(
        export, "_record_manifest_intent", crash_before_manifest_intent)
    with pytest.raises(SystemExit, match="worker death"):
        _export_bundle(db, "S9", write_dir=tmp_path, idempotency_key=key)
    monkeypatch.setattr(
        export, "_record_manifest_intent", original_record_intent)
    db.expire_all()
    batch = db.exec(export.select(ExportBatch)).first()
    assert batch is not None and batch.status == "staging"
    assert batch.manifest_sha256 is None
    assert batch.staging_owner_hash is not None
    assert batch.staging_lease_expires_at is not None
    return batch


def _seed(
        s, *, include_summary=True, include_closeout=True,
        closeout_locked=True, summary_is_simulation=False,
        summary_classification="research"):
    intervention_completed_at = datetime(2026, 1, 1, 9, 0, 0)
    completed_at = intervention_completed_at + timedelta(minutes=5)
    s.add(Patient(patient_id="P77", dementia_severity="轻度", mandarin_eligible=True,
                  consent_status="已同意", secondary_use_allowed=True))
    s.add(TrainSession(
        session_id="S9", patient_id="P77", week_no=2,
        phase_type="正式训练", event_line="正式训练",
        item_bank_version_id="wk2-v1-20260707",
        item_bank_definition_digest="c" * 64,
        autopilot_protocol_version_id="autopilot-v1-20260729",
        autopilot_protocol_definition_digest="e" * 64,
        repeat_protocol_version_id=repeat_intent.ACTIVE_REPEAT_INTENT_VERSION_ID,
        repeat_protocol_definition_digest=(
            repeat_intent.ACTIVE_REPEAT_INTENT_DEFINITION_DIGEST)))
    s.add(SessionRuntimeState(
        session_id="S9", status="completed", revision=2,
        intervention_completed_at=intervention_completed_at,
        completed_at=completed_at, ended_by="R1",
        end_reason="completion_gate_passed",
    ))
    if include_summary:
        s.add(SessionOutcomeSummary(
            session_id="S9",
            schema_version="session-outcome-summary.v1",
            generator_version="server-authoritative-closeout.v1",
            item_bank_version_id="wk2-v1-20260707",
            is_simulation=summary_is_simulation,
            data_classification=summary_classification,
            expected_turns=6,
            matched_turns=6,
            completed_attempt_turns=6,
            audio_evidenced_turns=6,
            total_attempts=0,
            completed_attempts=0,
            needs_review_attempts=0,
            technical_failure_attempts=0,
            prompt_level_0_count=0,
            prompt_level_1_count=0,
            prompt_level_2_count=0,
            prompt_level_3_count=0,
            technical_pause_count=0,
            researcher_takeover_count=0,
            source_digest="a" * 64,
            generated_at=intervention_completed_at - timedelta(seconds=1),
        ))
    if include_closeout:
        s.add(SessionCloseoutReport(
            session_id="S9",
            schema_version="session-closeout.v1",
            status="no_additional_observation",
            revision=2 if closeout_locked else 1,
            last_idempotency_key="seed-closeout",
            last_request_hash="b" * 64,
            created_by="R1",
            updated_by="R1",
            locked_by="R1" if closeout_locked else None,
            locked_at=completed_at if closeout_locked else None,
        ))
    # 单要素：1 环节，锁定 final_correct=1，自发（prompt_level=0）
    se = ItemEvent(session_id="S9", item_id="SE_锚", task_type="单要素", item_set_type="训练集")
    s.add(se)
    s.commit()
    s.refresh(se)
    s.add(TurnEvent(item_event_id=se.id, turn_seq=1, response_role="命名",
                    asr_text="锚", confirmed_response_text="锚", prompt_level=0,
                    element_value=1, reviewed_score=1, score_locked=True, reviewer_id="R1"))
    # 双要素：5 环节全锁定，全对 → de_total=1.0
    de = ItemEvent(session_id="S9", item_id="DE_斧子+树", task_type="双要素", item_set_type="训练集")
    s.add(de)
    s.commit()
    s.refresh(de)
    for seq, role in enumerate(["左命名", "左作用", "右命名", "右作用", "关系识别"], start=1):
        s.add(TurnEvent(item_event_id=de.id, turn_seq=seq, response_role=role,
                        confirmed_response_text="已人工确认", prompt_level=0,
                        element_value=1, reviewed_score=1, score_locked=True, reviewer_id="R1"))
    # 含直接标识符的音频（第1周自我介绍类）——转写须被红线。
    # 音频须有实际采集字节 + 采集期 checksum，导出才有资格推进闸门。
    p_id, checksum_id = audio_store.save_blob("a_id", b"identifier-voice", "audio/wav")
    p_plain, checksum_plain = audio_store.save_blob("a_plain", b"plain-voice", "audio/wav")
    s.add(AudioAssetRow(raw_audio_id="a_id", session_id="S9", contains_direct_identifier=True,
                        audio_format=p_id.suffix.lstrip("."), checksum=checksum_id))
    s.add(AudioAssetRow(raw_audio_id="a_plain", session_id="S9",
                        audio_format=p_plain.suffix.lstrip("."), checksum=checksum_plain))
    s.add(ScaleResult(patient_id="P77", phase_type="前测", scale_name="CETI", score=42.0, assessor_id="A1"))
    s.commit()


def test_pseudonym_is_stable_and_not_raw_id():
    pseudonym = pseudonymize("P77")
    assert pseudonym == pseudonymize("P77")
    assert "P77" not in pseudonym
    assert re.fullmatch(r"SUBJ-v1-test-2026-[0-9a-f]{20}", pseudonym)
    assert pseudonym != pseudonymize("P78")


def test_export_ledger_migration_roundtrip_includes_staging_lease(tmp_path):
    db_path = tmp_path / "export-ledger-migration.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")

    def assert_export_schema() -> None:
        inspector = inspect(engine)
        columns = {
            column["name"] for column in inspector.get_columns("exportbatch")}
        assert {"staging_owner_hash", "staging_lease_expires_at"} <= columns
        batch_checks = {
            row["name"]: row["sqltext"]
            for row in inspector.get_check_constraints("exportbatch")}
        artifact_checks = {
            row["name"]: row["sqltext"]
            for row in inspector.get_check_constraints("exportartifact")}
        assert "staging_owner_hash" in batch_checks["ck_export_batch_staging_lease"]
        assert "staging_receipt" in artifact_checks["ck_export_artifact_kind"]

    assert_export_schema()
    command.check(config)
    command.downgrade(config, "e1c4a7d9b205")
    assert "exportbatch" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    command.check(config)
    assert_export_schema()
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version"
            )).scalar_one() == "6f2a9c4d8e17"


def test_mask_redacts_all_free_text_without_trusting_client_flag():
    assert mask_text("我叫张三今年85岁", True) == export_security.REDACTED_TEXT
    assert mask_text("电话13800001111", False) == export_security.REDACTED_TEXT
    assert mask_text("锚", False) == export_security.REDACTED_TEXT
    assert mask_text(None, False) is None


def test_deidentified_export_has_no_direct_identifiers(db, tmp_path):
    _seed(db)
    res = _export_bundle(db, "S9", deidentify=True, write_dir=tmp_path)
    # 任一表都不得出现直接标识列
    for name, rows in res["sheets"].items():
        for r in rows:
            assert not (DIRECT_IDENTIFIER_COLUMNS & set(r)), f"{name} 泄露直接标识"
    # patient_id 不出现在任何值里
    flat = str(res["sheets"])
    assert "P77" not in flat
    assert pseudonymize("P77") in flat
    assert export_security.pseudonymize_session("S9") in flat
    assert "crosswalk" not in res["sheets"]
    assert res["sheets"]["session"][0]["pseudonym_version"] == "v1"
    assert res["sheets"]["session"][0]["pseudonym_key_id"] == "test-2026"
    assert re.fullmatch(r"EXP-[0-9a-f]{24}", res["batch_id"])
    for rows in res["sheets"].values():
        for row in rows:
            assert "session_id" not in row
            assert "raw_audio_id" not in row
            assert "source_attempt_id" not in row
            assert "attempt_id" not in row
            assert not any(key.endswith("_at") for key in row)
            assert not any(isinstance(value, (date, datetime)) for value in row.values())
            assert not any(
                isinstance(value, str)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ].*)?", value.strip())
                for value in row.values()
            )
            assert not ({"S9", "a_id", "a_plain"} & set(row.values()))


def test_export_fails_closed_without_immutable_outcome_summary(db, tmp_path):
    _seed(db, include_summary=False)

    with pytest.raises(ValueError, match="缺少不可变自动结果汇总"):
        _export_bundle(db, "S9", deidentify=True, write_dir=tmp_path)
    assert not (tmp_path / "research").exists()


def test_export_fails_closed_when_summary_classification_disagrees(db, tmp_path):
    _seed(
        db, summary_is_simulation=True,
        summary_classification="simulation",
    )

    with pytest.raises(ValueError, match="数据分类不一致"):
        _export_bundle(db, "S9", deidentify=True, write_dir=tmp_path)
    assert not (tmp_path / "research").exists()


@pytest.mark.parametrize(
    ("include_closeout", "closeout_locked", "message"),
    [
        (False, False, "缺少独立现场收尾记录"),
        (True, False, "尚未随最终复核锁定"),
    ],
)
def test_export_fails_closed_without_locked_closeout(
        db, tmp_path, include_closeout, closeout_locked, message):
    _seed(
        db, include_closeout=include_closeout,
        closeout_locked=closeout_locked,
    )

    with pytest.raises(ValueError, match=message):
        _export_bundle(db, "S9", deidentify=True, write_dir=tmp_path)
    assert not (tmp_path / "research").exists()


def test_legacy_free_form_scale_never_enters_formal_outcome_sheet(db, tmp_path):
    _seed(db)

    result = _export_bundle(
        db, "S9", deidentify=True, write_dir=tmp_path)

    assert result["sheets"]["scales"] == []
    legacy_rows = result["sheets"]["legacy_unverified_scales"]
    assert len(legacy_rows) == 1
    assert legacy_rows[0]["verification_status"] == "legacy_unverified"
    assert legacy_rows[0]["formal_outcome_eligible"] is False
    assert legacy_rows[0]["source_schema"] == "legacy_free_form_scale_result"
    assert legacy_rows[0]["legacy_reported_label"] == "CETI"
    assert legacy_rows[0]["legacy_reported_score"] == 42.0
    assert "scale_name" not in legacy_rows[0]
    assert "score" not in legacy_rows[0]


def test_deidentified_export_removes_every_unreviewed_free_text(db, tmp_path):
    _seed(db)
    db.add(AbnormalEvent(
        session_id="S9", phase_type="正式训练", abnormal_type="其他",
        note="患者张三的电话是 13800001111",
    ))
    db.commit()

    res = _export_bundle(db, "S9", deidentify=True, write_dir=tmp_path)
    for rows in res["sheets"].values():
        for row in rows:
            for key in export_security.FREE_TEXT_COLUMNS & set(row):
                assert row[key] in (None, export_security.REDACTED_TEXT)
    serialized = str(res["sheets"])
    assert "患者张三" not in serialized
    assert "13800001111" not in serialized
    assert "已人工确认" not in serialized


def test_missing_deidentification_key_fails_closed_before_writing(
        db, tmp_path, monkeypatch):
    _seed(db)
    monkeypatch.delenv(export_security.DEIDENTIFICATION_KEY_ENV, raising=False)
    with pytest.raises(
            export_security.DeidentificationConfigurationError,
            match="DEIDENTIFICATION_KEY"):
        _export_bundle(db, "S9", deidentify=True, write_dir=tmp_path)
    assert not (tmp_path / "research").exists()
    assert db.get(AudioAssetRow, "a_id").status.value == "recorded"


def test_client_supplied_batch_id_cannot_encode_absolute_date(db, tmp_path):
    _seed(db)
    with pytest.raises(ValueError, match="含绝对日期"):
        _export_bundle(
            db, "S9", deidentify=True, batch_id="EXP-2026-07-18",
            write_dir=tmp_path,
        )
    assert not (tmp_path / "research").exists()


def test_export_reconstructs_scores_from_locked_turns(db, tmp_path):
    _seed(db)
    _export_bundle(db, "S9", deidentify=True, write_dir=tmp_path)
    scores = export._reconstruct_scores(
        list(db.exec(export.select(ItemEvent).where(ItemEvent.session_id == "S9"))),
        {it.id: list(db.exec(export.select(TurnEvent).where(TurnEvent.item_event_id == it.id)))
         for it in db.exec(export.select(ItemEvent).where(ItemEvent.session_id == "S9"))})
    assert scores["double"]["weekly_de_score_percentile"] == 100.0
    assert scores["single"]["naming_accuracy"] == 1.0
    assert scores["single"]["spontaneous_naming_accuracy"] == 1.0
    assert scores["excluded_items"] == []


def test_export_masks_identifier_transcript_and_triggers_audio_gate(db, tmp_path):
    _seed(db)
    res = _export_bundle(db, "S9", deidentify=True, write_dir=tmp_path)
    # 含标识音频对应的转写被红线（此处两条 turn 无 raw_audio_id 关联，验证 mask 规则由上一测试覆盖）
    # 音频闸门：recorded → exported，且打上批次；绝不删除
    a = db.get(AudioAssetRow, "a_id")
    assert a.status.value == "exported" and a.export_batch_id == res["batch_id"]
    audio_codes = {
        export_security.pseudonymize_audio("a_id"),
        export_security.pseudonymize_audio("a_plain"),
    }
    assert set(res["audio_touched"]) == audio_codes
    analysis_batch = tmp_path / "research" / res["batch_id"]
    controlled_batch = tmp_path / "_controlled_audio" / "research" / res["batch_id"]
    assert not (analysis_batch / "audio").exists()                  # 去标识分析包无原始声纹
    controlled_names = {path.name for path in (controlled_batch / "audio").iterdir()}
    assert controlled_names == {f"{code}.wav" for code in audio_codes}
    assert "a_id.wav" not in controlled_names                      # 受控文件名也不泄露内部 ID


def test_audio_copy_failure_does_not_commit_exported(db, tmp_path, monkeypatch):
    _seed(db)

    def fail_copy(*_args, **_kwargs):
        raise OSError("模拟存储故障")

    monkeypatch.setattr(export_security, "atomic_copy_file", fail_copy)
    with pytest.raises(OSError, match="存储故障"):
        _export_bundle(db, "S9", deidentify=True, write_dir=tmp_path)
    assert db.get(AudioAssetRow, "a_id").status.value == "recorded"
    assert db.get(AudioAssetRow, "a_plain").status.value == "recorded"
    assert not list((tmp_path / "research").glob("EXP-*"))


def test_existing_batch_collision_is_rejected_without_deleting_it(db, tmp_path):
    _seed(db)
    existing = tmp_path / "research" / "EXP-existing"
    existing.mkdir(parents=True)
    marker = existing / "keep.txt"
    marker.write_text("pre-existing", encoding="utf-8")

    with pytest.raises(FileExistsError, match="批次目录已存在"):
        _export_bundle(
            db, "S9", deidentify=True, batch_id="EXP-existing", write_dir=tmp_path)
    assert marker.read_text(encoding="utf-8") == "pre-existing"
    assert db.get(AudioAssetRow, "a_id").status.value == "recorded"


def test_active_staging_lease_blocks_retry_then_expired_owned_receipt_recovers(
        db, tmp_path, monkeypatch):
    _seed(db)
    key = "export-staging-lease-recovery-0123456789abcdef0123456789abcdef"
    batch = _leave_preintent_crash(db, tmp_path, monkeypatch, key)
    previous_owner = batch.staging_owner_hash
    analysis_batch = tmp_path / "research" / batch.batch_id
    controlled_batch = tmp_path / "_controlled_audio" / "research" / batch.batch_id
    analysis_marker = analysis_batch / "keep-analysis.txt"
    controlled_marker = controlled_batch / "keep-controlled.txt"
    analysis_marker.write_text("active", encoding="utf-8")
    controlled_marker.write_text("active", encoding="utf-8")

    with pytest.raises(export.ExportArtifactIntegrityError, match="租约仍有效"):
        _export_bundle(db, "S9", write_dir=tmp_path, idempotency_key=key)
    db.expire_all()
    still_active = db.get(ExportBatch, batch.batch_id)
    assert still_active.staging_owner_hash == previous_owner
    assert analysis_marker.read_text(encoding="utf-8") == "active"
    assert controlled_marker.read_text(encoding="utf-8") == "active"
    assert db.get(AudioAssetRow, "a_id").status == AudioStatus.recorded

    still_active.staging_lease_expires_at = (
        export._utc_now_naive() - timedelta(seconds=1))
    db.add(still_active)
    db.commit()
    recovered = _export_bundle(
        db, "S9", write_dir=tmp_path, idempotency_key=key)
    assert recovered["status"] == "published"
    assert not analysis_marker.exists()
    assert not controlled_marker.exists()
    assert any(
        row["kind"] == "staging_receipt" for row in recovered["artifacts"])
    assert db.get(ExportBatch, batch.batch_id).staging_owner_hash is None


@pytest.mark.parametrize("tamper", ["missing", "mismatch"])
def test_expired_staging_lease_refuses_cleanup_without_matching_receipt(
        db, tmp_path, monkeypatch, tamper):
    _seed(db)
    key = f"export-staging-refuse-{tamper}-0123456789abcdef0123456789abcdef"
    batch = _leave_preintent_crash(db, tmp_path, monkeypatch, key)
    previous_owner = batch.staging_owner_hash
    analysis_batch = tmp_path / "research" / batch.batch_id
    controlled_batch = tmp_path / "_controlled_audio" / "research" / batch.batch_id
    receipt = analysis_batch / export.STAGING_RECEIPT_NAME
    marker = analysis_batch / "must-survive.txt"
    controlled_marker = controlled_batch / "must-survive.txt"
    marker.write_text("do-not-delete", encoding="utf-8")
    controlled_marker.write_text("do-not-delete", encoding="utf-8")
    if tamper == "missing":
        receipt.unlink()
    else:
        receipt.write_text(
            '{"batch_id":"wrong","staging_owner_hash":"'
            + ("0" * 64) + '"}', encoding="utf-8")
    batch.staging_lease_expires_at = export._utc_now_naive() - timedelta(seconds=1)
    db.add(batch)
    db.commit()

    with pytest.raises(RuntimeError, match="未能完全清理"):
        _export_bundle(db, "S9", write_dir=tmp_path, idempotency_key=key)
    db.expire_all()
    quarantined = db.get(ExportBatch, batch.batch_id)
    assert quarantined.status == "staging"
    assert quarantined.manifest_sha256 is None
    assert quarantined.staging_owner_hash not in {None, previous_owner}
    assert marker.read_text(encoding="utf-8") == "do-not-delete"
    assert controlled_marker.read_text(encoding="utf-8") == "do-not-delete"
    assert db.get(AudioAssetRow, "a_id").status == AudioStatus.recorded


def test_withdrawal_between_file_copy_and_manifest_intent_cleans_owned_staging(
        db, tmp_path, monkeypatch):
    _seed(db)
    key = "export-withdraw-before-intent-0123456789abcdef0123456789abcdef"
    original_record_intent = export._record_manifest_intent
    withdrawal_committed = False

    def withdraw_then_record_intent(*args, **kwargs):
        nonlocal withdrawal_committed
        with Session(db.get_bind()) as withdrawal_db:
            patient = withdrawal_db.get(Patient, "P77")
            patient.withdrawal_status = "withdrawn"
            patient.consent_status = "withdrawn"
            patient.secondary_use_allowed = False
            withdrawal_db.add(patient)
            for audio in withdrawal_db.exec(export.select(AudioAssetRow).where(
                    AudioAssetRow.session_id == "S9")):
                audio.withdrawn = True
                audio.withdrawal_status = "isolated_by_subject_withdrawal"
                withdrawal_db.add(audio)
            withdrawal_db.commit()
        withdrawal_committed = True
        return original_record_intent(*args, **kwargs)

    monkeypatch.setattr(
        export, "_record_manifest_intent", withdraw_then_record_intent)
    with pytest.raises(export.ExportArtifactIntegrityError, match="撤回|失效"):
        _export_bundle(db, "S9", write_dir=tmp_path, idempotency_key=key)
    assert withdrawal_committed is True
    db.expire_all()
    batch = db.exec(export.select(ExportBatch)).one()
    assert batch.status == "staging"
    assert batch.manifest_sha256 is None
    assert batch.publication_manifest_json is None
    assert batch.staging_owner_hash is None
    assert batch.staging_lease_expires_at is None
    assert not (tmp_path / "research" / batch.batch_id).exists()
    assert not (
        tmp_path / "_controlled_audio" / "research" / batch.batch_id
    ).exists()
    assert not list(db.exec(export.select(ExportArtifact)))
    rows = list(db.exec(export.select(AudioAssetRow).where(
        AudioAssetRow.session_id == "S9")))
    assert all(row.status == AudioStatus.recorded for row in rows)
    assert all(row.withdrawn for row in rows)


def test_database_commit_failure_removes_new_artifacts_and_rolls_back(
        db, tmp_path, monkeypatch):
    _seed(db)

    def fail_commit():
        raise RuntimeError("模拟数据库提交失败")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="提交失败"):
        _export_bundle(
            db, "S9", deidentify=True, batch_id="EXP-commit-failure",
            write_dir=tmp_path,
        )
    assert not (tmp_path / "research" / "EXP-commit-failure").exists()
    assert not (
        tmp_path / "_controlled_audio" / "research" / "EXP-commit-failure"
    ).exists()
    assert db.get(AudioAssetRow, "a_id").status.value == "recorded"


def test_missing_prompt_is_excluded_not_counted_spontaneous(db):
    _seed(db)
    se = next(db.exec(export.select(ItemEvent).where(ItemEvent.item_id == "SE_锚")))
    turn = next(db.exec(export.select(TurnEvent).where(TurnEvent.item_event_id == se.id)))
    turn.prompt_level = None
    db.add(turn)
    db.commit()
    items = list(db.exec(export.select(ItemEvent).where(ItemEvent.session_id == "S9")))
    turns = {item.id: list(db.exec(export.select(TurnEvent)
                                   .where(TurnEvent.item_event_id == item.id))) for item in items}
    scores = export._reconstruct_scores(items, turns)
    assert scores["single"] is None
    assert any("prompt_level 缺失" in reason for reason in scores["excluded_items"])


def test_legacy_locked_turn_without_confirmation_is_excluded(db):
    _seed(db)
    se = next(db.exec(export.select(ItemEvent).where(ItemEvent.item_id == "SE_锚")))
    turn = next(db.exec(export.select(TurnEvent).where(TurnEvent.item_event_id == se.id)))
    turn.confirmed_response_text = None
    db.add(turn)
    db.commit()
    items = list(db.exec(export.select(ItemEvent).where(ItemEvent.session_id == "S9")))
    turns = {item.id: list(db.exec(export.select(TurnEvent)
                                   .where(TurnEvent.item_event_id == item.id))) for item in items}
    scores = export._reconstruct_scores(items, turns)
    assert scores["single"] is None
    assert any("未确认" in reason for reason in scores["excluded_items"])


def test_unlocked_turns_excluded_from_scoring(db, tmp_path):
    _seed(db)
    # 追加一道未锁定的单要素题
    ie = ItemEvent(session_id="S9", item_id="SE_花", task_type="单要素", item_set_type="训练集")
    db.add(ie)
    db.commit()
    db.refresh(ie)
    db.add(TurnEvent(item_event_id=ie.id, turn_seq=1, response_role="命名", element_value=0, score_locked=False))
    db.commit()
    scores = export._reconstruct_scores(
        list(db.exec(export.select(ItemEvent).where(ItemEvent.session_id == "S9"))),
        {it.id: list(db.exec(export.select(TurnEvent).where(TurnEvent.item_event_id == it.id)))
         for it in db.exec(export.select(ItemEvent).where(ItemEvent.session_id == "S9"))})
    assert any("SE_花" in x for x in scores["excluded_items"])
    # 已锁定的单要素仍只算 1 题（未锁定的被排除）
    assert scores["single"]["n"] == 1


def test_zero_new_audio_still_publishes_csv_manifest_and_ledger(db, tmp_path):
    _seed(db)
    for row in list(db.exec(export.select(AudioAssetRow))):
        db.delete(row)
    db.commit()

    result = _export_bundle(db, "S9", write_dir=tmp_path)

    batch = db.get(ExportBatch, result["batch_id"])
    artifacts = list(db.exec(export.select(ExportArtifact).where(
        ExportArtifact.batch_id == result["batch_id"])))
    assert batch is not None and batch.status == "published"
    assert result["audio_touched"] == []
    assert any(row.kind == "manifest" for row in artifacts)
    assert any(row.kind == "csv" for row in artifacts)
    assert not any(row.kind == "controlled_audio" for row in artifacts)
    assert all(not path.startswith("/") for path in result["files"])


def test_recorded_source_missing_fails_closed_without_publishing(db, tmp_path):
    _seed(db)
    assert audio_store.delete_blob("a_id") is True

    with pytest.raises(ValueError, match="录音源文件缺失"):
        _export_bundle(
            db, "S9", write_dir=tmp_path,
            idempotency_key="export-missing-source-0123456789abcdef0123456789abcdef",
        )

    batch = db.exec(export.select(ExportBatch)).first()
    assert batch is not None and batch.status == "staging"
    assert db.get(AudioAssetRow, "a_id").status.value == "recorded"


def test_idempotent_replay_and_conflict_use_only_key_hash(db, tmp_path):
    _seed(db)
    key = "export-replay-safe-0123456789abcdef0123456789abcdef"
    first = _export_bundle(db, "S9", write_dir=tmp_path, idempotency_key=key)
    replay = _export_bundle(db, "S9", write_dir=tmp_path, idempotency_key=key)
    assert replay["batch_id"] == first["batch_id"]
    assert replay["artifacts"] == first["artifacts"]
    rows = list(db.exec(export.select(ExportBatch)))
    assert len(rows) == 1
    assert rows[0].idempotency_key_hash == export._idempotency_hash(key)
    assert key not in str(rows[0].model_dump())
    assert rows[0].actor_display_id == "TEST-DATA-STEWARD"
    assert rows[0].actor_role == "data_steward"

    with pytest.raises(export.ExportIdempotencyConflict):
        export.export_session_bundle(
            db, "S9", write_dir=tmp_path, idempotency_key=key,
            actor_display_id="OTHER-ADMIN", actor_role="admin",
        )
    with pytest.raises(export.ExportIdempotencyConflict, match="第二批次"):
        _export_bundle(
            db, "S9", write_dir=tmp_path,
            idempotency_key="export-second-intent-0123456789abcdef0123456789abcdef",
        )
    assert len(list(db.exec(export.select(ExportBatch)))) == 1
    db.add(ExportBatch(
        batch_id="EXP-race-loser",
        idempotency_key_hash="1" * 64,
        request_fingerprint="2" * 64,
        export_scope_hash=rows[0].export_scope_hash,
        data_classification="research",
        actor_display_id="OTHER-DATA-STEWARD",
        actor_role="data_steward",
    ))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_final_commit_ambiguity_preserves_artifacts_ready_and_same_key_recovers(
        db, tmp_path, monkeypatch):
    _seed(db)
    key = "export-ambiguous-final-0123456789abcdef0123456789abcdef"
    original_commit = db.commit
    calls = 0

    def fail_before_final_commit():
        nonlocal calls
        calls += 1
        if calls == 5:
            raise RuntimeError("simulated final commit ambiguity")
        return original_commit()

    monkeypatch.setattr(db, "commit", fail_before_final_commit)
    with pytest.raises(RuntimeError, match="artifacts_ready"):
        _export_bundle(db, "S9", write_dir=tmp_path, idempotency_key=key)
    batch = db.exec(export.select(ExportBatch)).first()
    assert batch is not None and batch.status == "artifacts_ready"
    assert (tmp_path / "research" / batch.batch_id / "manifest.json").is_file()
    assert db.get(AudioAssetRow, "a_id").status.value == "recorded"

    monkeypatch.setattr(db, "commit", original_commit)
    recovered = _export_bundle(db, "S9", write_dir=tmp_path, idempotency_key=key)
    assert recovered["batch_id"] == batch.batch_id
    assert recovered["status"] == "published"
    assert db.get(AudioAssetRow, "a_id").status.value == "exported"


def test_withdrawal_committed_in_first_db_session_blocks_second_session_finalize(
        tmp_path, monkeypatch):
    """Reproduce the red-team race with two independent DB transactions."""
    engine = create_engine(f"sqlite:///{tmp_path / 'withdrawal-export-race.db'}")
    SQLModel.metadata.create_all(engine)
    key = "export-withdrawal-race-0123456789abcdef0123456789abcdef"
    with Session(engine) as preparation:
        _seed(preparation)
        batch_id = _prepare_artifacts_ready(
            preparation, tmp_path / "published-files", monkeypatch, key)

    # Transaction 1: withdrawal owns the same documented DB lock order and
    # commits before the finalizer starts.
    with Session(engine) as withdrawal_db:
        list(withdrawal_db.exec(export.select(TrainSession).where(
            TrainSession.patient_id == "P77",
        ).order_by(TrainSession.session_id).with_for_update()))
        patient = withdrawal_db.exec(export.select(Patient).where(
            Patient.patient_id == "P77").with_for_update()).one()
        withdrawal_db.exec(export.select(ExportBatch).where(
            ExportBatch.batch_id == batch_id).with_for_update()).one()
        audios = list(withdrawal_db.exec(export.select(AudioAssetRow).where(
            AudioAssetRow.session_id == "S9",
        ).order_by(AudioAssetRow.raw_audio_id).with_for_update()))
        patient.withdrawal_status = "withdrawn"
        patient.consent_status = "withdrawn"
        patient.secondary_use_allowed = False
        withdrawal_db.add(patient)
        for audio in audios:
            audio.withdrawn = True
            audio.withdrawal_status = "isolated_by_subject_withdrawal"
            withdrawal_db.add(audio)
        withdrawal_db.commit()

    # Transaction 2 must discard every pre-withdrawal snapshot and fail closed.
    with Session(engine) as finalizer_db:
        config = export_security.load_deidentification_config()
        with pytest.raises(export.ExportArtifactIntegrityError, match="撤回|失效"):
            export._finalize_export_batch(
                finalizer_db, batch_id=batch_id, session_id="S9",
                config=config, now=datetime(2026, 1, 1, 10, 0, 0),
                write_dir=tmp_path / "published-files",
            )
        finalizer_db.rollback()
        batch = finalizer_db.get(ExportBatch, batch_id)
        assert batch.status == "artifacts_ready" and batch.published_at is None
        rows = list(finalizer_db.exec(export.select(AudioAssetRow).where(
            AudioAssetRow.session_id == "S9")))
        assert all(row.status == AudioStatus.recorded for row in rows)


def test_audio_withdrawal_after_manifest_blocks_publish_without_patient_flag(
        db, tmp_path, monkeypatch):
    _seed(db)
    key = "export-audio-withdrawal-0123456789abcdef0123456789abcdef"
    batch_id = _prepare_artifacts_ready(db, tmp_path, monkeypatch, key)
    audio = db.get(AudioAssetRow, "a_id")
    audio.withdrawn = True
    audio.withdrawal_status = "isolated"
    db.add(audio)
    db.commit()

    with pytest.raises(export.ExportArtifactIntegrityError, match="音频"):
        export._finalize_export_batch(
            db, batch_id=batch_id, session_id="S9",
            config=export_security.load_deidentification_config(),
            now=datetime(2026, 1, 1, 10, 0, 0), write_dir=tmp_path,
        )
    db.rollback()
    assert db.get(ExportBatch, batch_id).status == "artifacts_ready"
    assert db.get(AudioAssetRow, "a_plain").status == AudioStatus.recorded


def test_published_batch_becomes_unreadable_after_later_withdrawal(db, tmp_path):
    _seed(db)
    result = _export_bundle(
        db, "S9", write_dir=tmp_path,
        idempotency_key="export-post-publish-withdrawal-0123456789abcdef0123456789abcdef",
    )
    patient = db.get(Patient, "P77")
    patient.withdrawal_status = "withdrawn"
    patient.consent_status = "withdrawn"
    db.add(patient)
    for audio in db.exec(export.select(AudioAssetRow).where(
            AudioAssetRow.session_id == "S9")):
        audio.withdrawn = True
        audio.withdrawal_status = "isolated_by_subject_withdrawal"
        db.add(audio)
    db.commit()

    with pytest.raises(export.ExportArtifactIntegrityError, match="撤回|失效"):
        export.get_export_batch_result(
            db, result["batch_id"], write_dir=tmp_path)
    # Policy/SOP may later revoke or delete the published material.  This code
    # deliberately does neither; it only makes new reads fail closed.
    assert (tmp_path / "research" / result["batch_id"] / "manifest.json").is_file()


def test_published_manifest_tamper_fails_closed_on_replay(db, tmp_path):
    _seed(db)
    key = "export-tamper-check-0123456789abcdef0123456789abcdef"
    result = _export_bundle(db, "S9", write_dir=tmp_path, idempotency_key=key)
    manifest_artifact = next(
        row for row in result["artifacts"] if row["kind"] == "manifest")
    (tmp_path / manifest_artifact["relative_path"]).write_bytes(b"tampered")

    with pytest.raises(export.ExportArtifactIntegrityError, match="账本不一致"):
        _export_bundle(db, "S9", write_dir=tmp_path, idempotency_key=key)


def test_manifest_and_artifact_ledger_never_store_direct_ids_or_absolute_paths(db, tmp_path):
    _seed(db)
    key = "export-privacy-ledger-0123456789abcdef0123456789abcdef"
    result = _export_bundle(db, "S9", write_dir=tmp_path, idempotency_key=key)
    batch = db.get(ExportBatch, result["batch_id"])
    artifacts = list(db.exec(export.select(ExportArtifact).where(
        ExportArtifact.batch_id == result["batch_id"])))
    serialized = batch.publication_manifest_json + str([
        row.model_dump() for row in artifacts
    ])
    for forbidden in ("P77", "S9", "a_id", "a_plain", str(tmp_path)):
        assert forbidden not in serialized
    assert all(not Path(row.relative_path).is_absolute() for row in artifacts)


# ==========================================================================
# Explicit-repeat governed export: capture-driven truth source, metadata-only.
# ==========================================================================

REPEAT_VERSION = repeat_intent.ACTIVE_REPEAT_INTENT_VERSION_ID
REPEAT_DIGEST = repeat_intent.ACTIVE_REPEAT_INTENT_DEFINITION_DIGEST
_T0 = datetime(2026, 1, 1, 8, 0, 0)


def _seed_repeat_capture(s, *, raw_audio_id="a_repeat"):
    """Build one genuine, fully valid repeat chain — no CHECK is bypassed."""
    path, checksum = audio_store.save_blob(raw_audio_id, b"repeat-voice", "audio/wav")
    s.add(AudioAssetRow(
        raw_audio_id=raw_audio_id, session_id="S9",
        audio_format=path.suffix.lstrip("."), checksum=checksum,
        byte_count=len(b"repeat-voice"), uploaded_at=_T0,
        turn_key="SE_锚#1", status=AudioStatus.recorded))
    s.add(PatientDeviceCapability(
        token_hash="t" * 64, session_id="S9", device_id_hash="d" * 64,
        active_session_key="S9", created_at=_T0 - timedelta(minutes=5),
        expires_at=_T0 + timedelta(hours=2)))
    binding = dict(
        item_bank_version_id="wk2-v1-20260707",
        item_bank_definition_digest="c" * 64,
        autopilot_protocol_version_id="autopilot-v1-20260729",
        autopilot_protocol_definition_digest="e" * 64,
        response_role="命名",
        repeat_protocol_version_id=REPEAT_VERSION,
        repeat_protocol_definition_digest=REPEAT_DIGEST,
        scope_key="p0a_sim_first_single_v1",
        control_generation=1, runner_generation=1,
        issued_capability_token_hash="t" * 64, issued_device_id_hash="d" * 64,
        session_id="S9", item_id="SE_锚", turn_seq=1, turn_key="SE_锚#1",
        attempt_seq=1, prompt_level=0,
    )
    tts_payload = json.dumps({
        "schema_version": 1, "speech_key": "p0a.question.1",
        "speech_text": "这是什么？", "purpose": "question",
        "item_id": "SE_锚", "turn_seq": 1, "cue_level": 0,
    }, ensure_ascii=False, separators=(",", ":"))
    source = RuntimeCommand(
        idempotency_key="cmd-source-0001", command_seq=1, kind="tts",
        state="succeeded", issued_at=_T0, succeeded_at=_T0 + timedelta(seconds=3),
        revision=1, payload_json=tts_payload, created_at=_T0, updated_at=_T0,
        **binding)
    s.add(source)
    s.commit()
    s.refresh(source)
    record_payload = json.dumps({
        "schema_version": 1, "raw_audio_id": raw_audio_id,
        "turn_key": "SE_锚#1", "item_id": "SE_锚", "turn_seq": 1,
        "cue_level": 0, "max_duration_seconds": 30,
        "contains_direct_identifier": False,
    }, ensure_ascii=False, separators=(",", ":"))
    record = RuntimeCommand(
        idempotency_key="cmd-record-0001", command_seq=2, kind="record",
        state="succeeded", issued_at=_T0 + timedelta(seconds=4),
        succeeded_at=_T0 + timedelta(seconds=20), revision=1,
        predecessor_command_id=source.id,
        trigger_ack_idempotency_key="ack-tts-ended-0001",
        expected_raw_audio_id=raw_audio_id, payload_json=record_payload,
        created_at=_T0, updated_at=_T0, **binding)
    replay = RuntimeCommand(
        idempotency_key=autopilot_service.repeat_replay_command_key("S9", 1),
        command_seq=3, kind="tts", state="pending",
        issued_at=_T0 + timedelta(seconds=25), revision=0,
        payload_json=tts_payload, replay_source_command_id=source.id,
        replay_ordinal=1,
        replay_source_payload_sha256=hashlib.sha256(
            tts_payload.encode("utf-8")).hexdigest(),
        created_at=_T0, updated_at=_T0, **binding)
    s.add_all([record, replay])
    s.commit()
    s.refresh(record)
    s.refresh(replay)

    s.add(RuntimeCommandAck(
        command_id=source.id, idempotency_key="ack-tts-ended-0001",
        session_id="S9", ack_type="tts_ended", command_revision=0,
        control_generation=1, runner_generation=1, device_event_seq=1,
        device_id_hash="d" * 64, capability_token_hash="t" * 64,
        payload_json='{"media_ended":true}',
        received_at=_T0 + timedelta(seconds=3)))
    receipt = AudioCaptureReceipt(
        raw_audio_id=raw_audio_id, session_id="S9", turn_key="SE_锚#1",
        received_at=_T0 + timedelta(seconds=19), duration_seconds=1.5,
        byte_count=len(b"repeat-voice"), checksum=checksum,
        data_classification="research", is_simulation=False,
        contains_direct_identifier=False)
    s.add(receipt)
    s.commit()
    s.refresh(receipt)
    s.add(RuntimeCommandAck(
        command_id=record.id, idempotency_key="ack-record-stopped-0001",
        session_id="S9", ack_type="record_stopped", command_revision=0,
        control_generation=1, runner_generation=1, device_event_seq=2,
        device_id_hash="d" * 64, capability_token_hash="t" * 64,
        payload_json='{"stop_reason":"max_duration"}',
        receipt_server_seq=receipt.server_seq, raw_audio_id=raw_audio_id,
        checksum=checksum, byte_count=len(b"repeat-voice"),
        duration_seconds=1.5, received_at=_T0 + timedelta(seconds=19)))
    capture = AttemptCaptureProcessing(
        record_command_id=record.id, predecessor_command_id=source.id,
        receipt_server_seq=receipt.server_seq, raw_audio_id=raw_audio_id,
        session_id="S9", item_id="SE_锚", turn_seq=1, proof_attempt_seq=1,
        proof_prompt_level=0, processing_status="received",
        repeat_protocol_version_id=REPEAT_VERSION,
        repeat_protocol_definition_digest=REPEAT_DIGEST,
        repeat_admission_semantics="repeat_bound",
        asr_engine_version="repeat-asr-v1", asr_confidence=0.93,
        created_at=_T0 + timedelta(seconds=20))
    s.add(capture)
    s.commit()
    s.refresh(capture)
    request = AutopilotRepeatRequest(
        capture_processing_id=capture.id, session_id="S9", item_id="SE_锚",
        turn_seq=1, attempt_seq=1, prompt_level=0, repeat_ordinal=1,
        outcome="replayed", record_command_id=record.id,
        raw_audio_id=raw_audio_id, source_tts_command_id=source.id,
        source_payload_sha256=hashlib.sha256(
            tts_payload.encode("utf-8")).hexdigest(),
        replay_command_id=replay.id, asr_engine_version="repeat-asr-v1",
        asr_confidence=0.93, repeat_protocol_version_id=REPEAT_VERSION,
        repeat_protocol_definition_digest=REPEAT_DIGEST,
        phrase_key="repeat_again",
        normalized_text_sha256=repeat_intent.normalized_text_sha256("再说一遍"),
        created_at=_T0 + timedelta(seconds=24))
    s.add(request)
    s.commit()
    s.refresh(request)
    # The terminal transition uses the same fenced Core update production uses.
    s.connection().exec_driver_sql(
        "UPDATE attemptcaptureprocessing SET processing_status = 'asr_completed', "
        "disposition = 'repeat_replayed', repeat_request_id = ?, processed_at = ? "
        "WHERE id = ?", (request.id,
                         (_T0 + timedelta(seconds=25)).isoformat(sep=" "),
                         capture.id))
    s.commit()
    return {"capture_id": capture.id, "request_id": request.id,
            "raw_audio_id": raw_audio_id, "checksum": checksum,
            "record_id": record.id, "replay_id": replay.id,
            "source_id": source.id}


def test_repeat_capture_exports_as_metadata_only_with_exactly_four_columns(
        db, tmp_path):
    """真实跑一次导出：repeat 只出现在四列清单里，别处一律查不到它。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    result = _export_bundle(db, "S9", write_dir=tmp_path)

    repeat_rows = result["sheets"]["repeat_audio_manifest"]
    assert len(repeat_rows) == 1
    row = repeat_rows[0]
    assert set(row) == {
        "capture_kind", "repeat_ordinal", "outcome", "opaque_audio_code"}
    assert row["capture_kind"] == "explicit_repeat"
    assert row["repeat_ordinal"] == 1
    assert row["outcome"] == "replayed"
    opaque = row["opaque_audio_code"]
    assert opaque and ids["raw_audio_id"] not in opaque

    # The repeat recording is not in the ordinary audio manifest, and carries
    # no AttemptEvent anywhere in the bundle.
    ordinary_codes = {r["audio_code"] for r in result["sheets"]["audio_manifest"]}
    assert opaque not in ordinary_codes
    assert result["sheets"]["attempts"] == []
    assert all(r.get("audio_code") != opaque
               for r in result["sheets"]["interactions"])

    # The controlled copy is byte-exact.
    controlled = [Path(a["relative_path"]) for a in result["artifacts"]
                  if a["kind"] == "controlled_audio"]
    copies = [p for p in controlled if p.name.startswith(f"{opaque}.")]
    assert len(copies) == 1
    copied = (tmp_path / "_controlled_audio" / copies[0])
    assert copied.exists()
    assert copied.read_bytes() == b"repeat-voice"
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == ids["checksum"]

    # Nothing anywhere in the written bundle leaks the phrase, its digests or
    # any internal identifier.
    forbidden = [
        "再说一遍", "repeat_again",
        repeat_intent.normalized_text_sha256("再说一遍"),
        ids["raw_audio_id"], f"\"{ids['capture_id']}\"",
        "capture_processing_id", "repeat_request_id", "source_tts_command_id",
        "normalized_text_sha256", "phrase_key",
    ]
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written
    for path in written:
        blob = path.read_bytes()
        for needle in forbidden:
            assert needle.encode("utf-8") not in blob, (path.name, needle)


def _snapshot_export_state(db, tmp_path) -> tuple:
    return (
        sorted((r.raw_audio_id, str(r.status), r.export_batch_id)
               for r in db.exec(export.select(AudioAssetRow))),
        len(list(db.exec(export.select(ExportBatch)))),
        len(list(db.exec(export.select(ExportArtifact)))),
        sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")),
    )


def _corrupt(db, statement: str, params: tuple) -> None:
    """One-statement corruption window; the PRAGMA is restored on the same
    connection before commit, so later CHECK negatives cannot go falsely green."""
    connection = db.connection()
    connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
    try:
        connection.exec_driver_sql(statement, params)
    finally:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")
    db.commit()
    assert db.connection().exec_driver_sql(
        "PRAGMA ignore_check_constraints").scalar() in (0, None)


def _seed_foreign_session_capture(db) -> int:
    """A real capture row in another session, for cross-session pointer tests."""
    path, checksum = audio_store.save_blob(
        "a_foreign", b"foreign-voice", "audio/wav")
    db.add(AudioAssetRow(
        raw_audio_id="a_foreign", session_id="S-OTHER",
        audio_format=path.suffix.lstrip("."), checksum=checksum))
    db.commit()
    receipt = AudioCaptureReceipt(
        raw_audio_id="a_foreign", session_id="S-OTHER", turn_key="SE_锚#1",
        received_at=_T0, duration_seconds=1.0, byte_count=len(b"foreign-voice"),
        checksum=checksum, data_classification="research", is_simulation=False)
    db.add(receipt)
    db.commit()
    db.refresh(receipt)
    capture = AttemptCaptureProcessing(
        record_command_id=1, predecessor_command_id=1,
        receipt_server_seq=receipt.server_seq,
        raw_audio_id="a_foreign", session_id="S-OTHER", item_id="SE_锚",
        turn_seq=1, proof_attempt_seq=1, proof_prompt_level=0,
        processing_status="received",
        repeat_protocol_version_id=REPEAT_VERSION,
        repeat_protocol_definition_digest=REPEAT_DIGEST,
        repeat_admission_semantics="repeat_bound", created_at=_T0)
    db.add(capture)
    db.commit()
    db.refresh(capture)
    return capture.id


@pytest.mark.parametrize("corruption", [
    "ledger_points_at_another_session",
    "capture_pointer_and_disposition_wiped",
    "ledger_capture_pointer_leaves_this_session",
    "orphan_capture_without_ledger",
    "branch_mismatch_replayed_without_replay_command",
])
def test_cross_session_and_one_sided_repeat_corruption_refuses_the_whole_export(
        db, tmp_path, corruption):
    """任何单边/跨场次腐败都必须整包拒绝，且 DB 与输出目录零变化。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    db.add(TrainSession(session_id="S-OTHER", patient_id="P77", week_no=2,
                        phase_type="正式训练", event_line="正式训练",
                        item_bank_version_id="wk2-v1-20260707"))
    db.commit()
    foreign_capture_id = _seed_foreign_session_capture(db)

    statements = {
        # R(S) -> C(T): the ledger row claims a capture in another session.
        "ledger_capture_pointer_leaves_this_session": (
            "UPDATE autopilotrepeatrequest SET capture_processing_id = ? "
            "WHERE id = ?", (foreign_capture_id, ids["request_id"])),
        # C(S) -> R(T): the ledger row moves to another session; the capture
        # still points at it, so the capture-first query must still catch it.
        "ledger_points_at_another_session": (
            "UPDATE autopilotrepeatrequest SET session_id = 'S-OTHER' "
            "WHERE id = ?", (ids["request_id"],)),
        # Both of the capture's own repeat markers wiped: only the third,
        # pointer-driven query can still see the ledger row.
        "capture_pointer_and_disposition_wiped": (
            "UPDATE attemptcaptureprocessing SET repeat_request_id = NULL, "
            "disposition = 'answer_candidate' WHERE id = ?", (ids["capture_id"],)),
        "orphan_capture_without_ledger": (
            "DELETE FROM autopilotrepeatrequest WHERE id = ?",
            (ids["request_id"],)),
        "branch_mismatch_replayed_without_replay_command": (
            "UPDATE autopilotrepeatrequest SET replay_command_id = NULL "
            "WHERE id = ?", (ids["request_id"],)),
    }
    statement, params = statements[corruption]
    _corrupt(db, statement, params)
    before = _snapshot_export_state(db, tmp_path)

    with pytest.raises(ValueError):
        _export_bundle(db, "S9", write_dir=tmp_path)

    db.rollback()
    assert _snapshot_export_state(db, tmp_path) == before


def test_repeat_audio_bound_to_an_attempt_refuses_the_whole_export(db, tmp_path):
    """同一段录音不能既是回答又是重复请求。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    db.add(AttemptEvent(
        session_id="S9", item_id="SE_锚", turn_seq=1, response_role="命名",
        attempt_seq=1, raw_audio_id=ids["raw_audio_id"], prompt_level=0,
        processing_status="completed"))
    db.commit()
    before = _snapshot_export_state(db, tmp_path)

    with pytest.raises(ValueError, match="同时绑定回答与重复请求"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    db.rollback()
    assert _snapshot_export_state(db, tmp_path) == before


def test_missing_repeat_blob_refuses_before_any_batch_or_file_exists(
        db, tmp_path):
    """源字节缺失是确定性前置拒绝：批次、artifact、音频状态、输出目录全部零变化。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    audio_store.find_blob(ids["raw_audio_id"]).unlink()
    before = _snapshot_export_state(db, tmp_path)

    with pytest.raises(ValueError, match="重复请求录音源文件缺失"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    db.rollback()
    assert _snapshot_export_state(db, tmp_path) == before


def test_repeat_blob_checksum_mismatch_refuses_before_any_batch_or_file_exists(
        db, tmp_path):
    """字节被改过但校验和没变，同样必须在建批次之前拒绝。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    # Same length, different bytes: only the digest can catch this.
    audio_store.find_blob(ids["raw_audio_id"]).write_bytes(b"foobar-voice")
    before = _snapshot_export_state(db, tmp_path)

    with pytest.raises(ValueError, match="字节与采集期校验和不一致"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    db.rollback()
    assert _snapshot_export_state(db, tmp_path) == before


def test_repeat_audio_moved_to_another_session_refuses_the_whole_export(
        db, tmp_path):
    """录音资产被挪到外场次：不能静默漏分类，必须整包拒绝。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    db.add(TrainSession(session_id="S-OTHER", patient_id="P77", week_no=2,
                        phase_type="正式训练", event_line="正式训练",
                        item_bank_version_id="wk2-v1-20260707"))
    db.commit()
    asset = db.get(AudioAssetRow, ids["raw_audio_id"])
    asset.session_id = "S-OTHER"
    db.add(asset)
    db.commit()
    before = _snapshot_export_state(db, tmp_path)

    with pytest.raises(ValueError, match="不属于本场次音频集合"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    db.rollback()
    assert _snapshot_export_state(db, tmp_path) == before


def test_written_repeat_manifest_csv_header_is_exactly_the_four_columns(
        db, tmp_path):
    """读真实写出的 CSV 文件，表头必须严格只有批准的四列。"""
    _seed(db)
    _seed_repeat_capture(db)
    result = _export_bundle(db, "S9", write_dir=tmp_path)

    written = [p for p in tmp_path.rglob("repeat_audio_manifest.csv")]
    assert len(written) == 1, [str(p) for p in tmp_path.rglob("*.csv")]
    with written[0].open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    # The approved contract fixes the order, not merely the set.
    assert rows[0] == [
        "capture_kind", "repeat_ordinal", "outcome", "opaque_audio_code"]
    assert len(rows) == 2
    assert rows[1][:3] == ["explicit_repeat", "1", "replayed"]
    assert rows[1][3] == result["sheets"]["repeat_audio_manifest"][0][
        "opaque_audio_code"]


def test_repeat_audio_bound_to_a_foreign_session_attempt_refuses_the_export(
        db, tmp_path):
    """回答绑定可能挂在别的场次上；按录音全局查，不能只看本场次。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    db.add(TrainSession(session_id="S-OTHER", patient_id="P77", week_no=2,
                        phase_type="正式训练", event_line="正式训练",
                        item_bank_version_id="wk2-v1-20260707"))
    db.commit()
    db.add(AttemptEvent(
        session_id="S-OTHER", item_id="SE_锚", turn_seq=1, response_role="命名",
        attempt_seq=1, raw_audio_id=ids["raw_audio_id"], prompt_level=0,
        processing_status="completed"))
    db.commit()
    before = _snapshot_export_state(db, tmp_path)

    with pytest.raises(ValueError, match="同时绑定回答与重复请求"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    db.rollback()
    assert _snapshot_export_state(db, tmp_path) == before


@pytest.mark.parametrize("statement,params_key,message", [
    ("UPDATE runtimecommand SET succeeded_at = '2026-01-01 07:00:00' WHERE id = ?",
     # Caught even earlier, by the shared immutable capture verifier.
     "record_id", "record success timestamp predates its terminal ACK"),
    ("UPDATE attemptcaptureprocessing SET created_at = '2026-01-01 08:00:01' "
     "WHERE id = ?", "capture_id", "capture created precedes record succeeded"),
    ("UPDATE autopilotrepeatrequest SET created_at = '2026-01-01 08:00:01' "
     "WHERE id = ?", "request_id", "request created precedes capture created"),
    ("UPDATE attemptcaptureprocessing SET processed_at = '2026-01-01 08:00:21' "
     "WHERE id = ?", "capture_id", "terminal timestamp predates"),
    ("UPDATE runtimecommand SET issued_at = '2026-01-01 08:00:01' WHERE id = ?",
     "replay_id", "replay command does not match"),
])
def test_repeat_chain_timestamp_tampering_refuses_the_export(
        db, tmp_path, statement, params_key, message):
    """完整合法基线逐项破坏时间顺序，必须命中对应的链路错误。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    _corrupt(db, statement, (ids[params_key],))
    before = _snapshot_export_state(db, tmp_path)

    with pytest.raises(ValueError, match=message):
        _export_bundle(db, "S9", write_dir=tmp_path)

    db.rollback()
    assert _snapshot_export_state(db, tmp_path) == before


def test_replay_issue_time_before_its_recording_is_caught_by_the_new_ordering(
        db, tmp_path):
    """只破坏 replay->record 的时间关系，不触发任何旧门禁，证明新检查不是死代码。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    # The replay is pending and has no ACK, so no ACK/revision gate can fire;
    # only the chain's own ordering rule can catch this.
    _corrupt(db, "UPDATE runtimecommand SET issued_at = ? WHERE id = ?",
             ("2026-01-01 08:00:05", ids["replay_id"]))
    before = _snapshot_export_state(db, tmp_path)

    with pytest.raises(ValueError, match="replay command does not match"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    db.rollback()
    assert _snapshot_export_state(db, tmp_path) == before


def test_repeat_audio_turn_key_drift_refuses_the_export(db, tmp_path):
    _seed(db)
    ids = _seed_repeat_capture(db)
    asset = db.get(AudioAssetRow, ids["raw_audio_id"])
    asset.turn_key = "SE_锚#2"
    db.add(asset)
    db.commit()
    before = _snapshot_export_state(db, tmp_path)

    with pytest.raises(ValueError, match="环节键不闭合"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    db.rollback()
    assert _snapshot_export_state(db, tmp_path) == before


def test_repeat_blob_size_drift_refuses_the_export(db, tmp_path):
    _seed(db)
    ids = _seed_repeat_capture(db)
    # Every ledger row stays consistent; only the file on disk was truncated.
    audio_store.find_blob(ids["raw_audio_id"]).write_bytes(b"short")
    before = _snapshot_export_state(db, tmp_path)

    with pytest.raises(ValueError, match="文件大小与采集账本不一致"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    db.rollback()
    assert _snapshot_export_state(db, tmp_path) == before


def test_repeat_evidence_changing_between_preflight_and_write_refuses_early(
        db, tmp_path, monkeypatch):
    """并发在两次证明之间改了绑定：必须在写出第一个文件前拒绝，且零推进。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    before = _snapshot_export_state(db, tmp_path)

    fired = []

    def _mutate_between_proofs():
        # Runs after the preflight proof and before the pre-write re-proof.
        if not fired:
            fired.append(True)
            # audio_format is part of the fingerprint but is not itself a
            # validity rule, so the second proof still succeeds and only the
            # fingerprint comparison can catch the change.
            db.connection().exec_driver_sql(
                "UPDATE audioassetrow SET audio_format = ? WHERE raw_audio_id = ?",
                ("ogg", ids["raw_audio_id"]))
            db.commit()
        return None

    monkeypatch.setattr(export, "_repeat_evidence_reproof_boundary",
                        lambda: _mutate_between_proofs())

    with pytest.raises(ValueError, match="在导出准备期间发生变化"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    assert fired
    db.rollback()
    # This refusal is past the batch-intent record, so the existing staging
    # semantics apply: an unpublished batch may exist, but no file may be
    # written and no recording may advance.
    assert not (tmp_path / "_controlled_audio").exists()
    assert not list(tmp_path.rglob("*.csv"))
    assert all(row.status != AudioStatus.exported and row.export_batch_id is None
               for row in db.exec(export.select(AudioAssetRow)))
    assert all(row.status != "published"
               for row in db.exec(export.select(ExportBatch)))
    assert len(list(db.exec(export.select(ExportArtifact)))) == before[2]


# --------------------------------------------------------------------------
# The four probes an independent audit got ACCEPTED before these gates existed.
# --------------------------------------------------------------------------

def _assert_nothing_was_published(db, tmp_path) -> None:
    assert not (tmp_path / "_controlled_audio").exists()
    assert not list(tmp_path.rglob("*.csv"))
    assert all(row.status != "published"
               for row in db.exec(export.select(ExportBatch)))
    assert list(db.exec(export.select(ExportArtifact))) == []
    assert all(row.status != AudioStatus.exported and row.export_batch_id is None
               for row in db.exec(export.select(AudioAssetRow)))


def test_probe_replay_issued_before_its_request_is_refused(db, tmp_path):
    """审计探针 1：replay.issued_at 早于 request.created_at，曾被放行。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    _corrupt(db, "UPDATE runtimecommand SET issued_at = ? WHERE id = ?",
             ("2026-01-01 08:00:21", ids["replay_id"]))

    with pytest.raises(ValueError, match="replay command does not match"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    db.rollback()
    _assert_nothing_was_published(db, tmp_path)


def test_probe_receipt_received_before_record_issuance_is_refused(db, tmp_path):
    """审计探针 2：receipt.received_at 早于 record.issued_at，曾被放行。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    _corrupt(db,
             "UPDATE audiocapturereceipt SET received_at = ? WHERE raw_audio_id = ?",
             ("2026-01-01 08:00:01", ids["raw_audio_id"]))

    with pytest.raises(ValueError, match="capture receipt precedes record issued"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    db.rollback()
    _assert_nothing_was_published(db, tmp_path)


def test_probe_repeat_asset_in_deletable_state_is_refused(db, tmp_path):
    """审计探针 3：repeat 资产处于 deletable 仍被发布（且受控副本为 0）。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    asset = db.get(AudioAssetRow, ids["raw_audio_id"])
    asset.status = AudioStatus.deletable
    db.add(asset)
    db.commit()

    with pytest.raises(ValueError, match="不处于可导出的全新采集状态"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    db.rollback()
    _assert_nothing_was_published(db, tmp_path)


def test_direct_identifier_flag_set_before_export_is_refused_at_preflight(
        db, tmp_path):
    """初始 preflight 版本（不是审计原探针的时机）。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    asset = db.get(AudioAssetRow, ids["raw_audio_id"])
    asset.contains_direct_identifier = True
    db.add(asset)
    db.commit()

    with pytest.raises(ValueError, match="不处于可导出的全新采集状态"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    db.rollback()
    _assert_nothing_was_published(db, tmp_path)


def test_replay_issued_after_the_capture_was_closed_out_is_refused(db, tmp_path):
    """上界：重播不能发生在 capture 已终态之后。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    _corrupt(db, "UPDATE runtimecommand SET issued_at = ? WHERE id = ?",
             ("2026-01-01 08:00:59", ids["replay_id"]))

    with pytest.raises(ValueError, match="replay command does not match"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    db.rollback()
    _assert_nothing_was_published(db, tmp_path)


def test_probe_direct_identifier_flipped_between_proofs_is_refused(
        db, tmp_path, monkeypatch):
    """审计探针 4（原时机）：第一次证明之后、写文件之前把 0→1。

    这正是原漏洞的窗口：sheets 已按 0 构建，第二次证明必须在写出第一个文件前
    发现并拒绝。
    """
    _seed(db)
    ids = _seed_repeat_capture(db)
    fired = []

    def _flip_between_proofs():
        if not fired:
            fired.append(True)
            db.connection().exec_driver_sql(
                "UPDATE audioassetrow SET contains_direct_identifier = 1 "
                "WHERE raw_audio_id = ?", (ids["raw_audio_id"],))
            db.commit()
        return None

    monkeypatch.setattr(export, "_repeat_evidence_reproof_boundary",
                        lambda: _flip_between_proofs())

    with pytest.raises(ValueError, match="不处于可导出的全新采集状态"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    assert fired
    db.rollback()
    assert not (tmp_path / "_controlled_audio").exists()
    assert not list(tmp_path.rglob("*.csv"))
    assert list(db.exec(export.select(ExportArtifact))) == []
    assert all(row.status != "published"
               for row in db.exec(export.select(ExportBatch)))
    assert all(row.status != AudioStatus.exported and row.export_batch_id is None
               for row in db.exec(export.select(AudioAssetRow)))


def test_same_key_retry_after_publish_recovers_the_same_repeat_batch(
        db, tmp_path):
    """首发成功后响应丢失，同键重试必须恢复同一批次，而不是被 fresh 门禁拒绝。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    key = "export-repeat-replay-0123456789abcdef0123456789abcdef"
    first = _export_bundle(db, "S9", write_dir=tmp_path, idempotency_key=key)

    with Session(db.get_bind()) as probe:
        asset = probe.get(AudioAssetRow, ids["raw_audio_id"])
        assert asset.status == AudioStatus.exported
        assert asset.export_batch_id == first["batch_id"]

    files_before = sorted(
        (str(p.relative_to(tmp_path)), p.stat().st_size,
         hashlib.sha256(p.read_bytes()).hexdigest())
        for p in tmp_path.rglob("*") if p.is_file())

    replay = _export_bundle(db, "S9", write_dir=tmp_path, idempotency_key=key)

    assert replay["batch_id"] == first["batch_id"]
    assert replay["artifacts"] == first["artifacts"]
    assert len(list(db.exec(export.select(ExportBatch)))) == 1
    files_after = sorted(
        (str(p.relative_to(tmp_path)), p.stat().st_size,
         hashlib.sha256(p.read_bytes()).hexdigest())
        for p in tmp_path.rglob("*") if p.is_file())
    assert files_after == files_before


def test_fresh_batch_still_refuses_an_already_exported_repeat_recording(
        db, tmp_path):
    """恢复分支不得放宽新批次门禁：新键遇到已导出的 repeat 录音仍必须拒绝。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    asset = db.get(AudioAssetRow, ids["raw_audio_id"])
    asset.status = AudioStatus.exported
    asset.export_batch_id = "EXP-somewhere-else"
    asset.exported_at = _T0
    db.add(asset)
    db.commit()

    with pytest.raises(ValueError, match="不处于可导出的全新采集状态"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    db.rollback()
    # The pre-seeded exported marker is the test's own setup, so assert the
    # publication facts rather than the generic audio-untouched snapshot.
    assert not (tmp_path / "_controlled_audio").exists()
    assert not list(tmp_path.rglob("*.csv"))
    assert list(db.exec(export.select(ExportArtifact))) == []
    assert all(row.status != "published"
               for row in db.exec(export.select(ExportBatch)))


def _between_proofs(monkeypatch, db, statement: str, params: tuple) -> list:
    """Inject one committed change in the window the re-proof exists to close."""
    fired: list = []

    def _hook():
        if not fired:
            fired.append(True)
            db.connection().exec_driver_sql(statement, params)
            db.commit()

    monkeypatch.setattr(export, "_repeat_evidence_reproof_boundary", _hook)
    return fired


def test_probe_capability_expiry_extended_between_proofs_is_refused(
        db, tmp_path, monkeypatch):
    """审计探针 A：两次证明之间把能力有效期 10:00→11:00，曾被 ACCEPT 并发布。

    历史 ACK 仍然合法（它们对 received_at 判定），所以只有把当前 capability 行
    纳入 fingerprint 才能发现这次变更。
    """
    _seed(db)
    _seed_repeat_capture(db)
    fired = _between_proofs(
        db=db, monkeypatch=monkeypatch,
        statement="UPDATE patientdevicecapability SET expires_at = ? "
                  "WHERE token_hash = ?",
        params=("2026-01-01 11:00:00", "t" * 64))

    with pytest.raises(ValueError, match="在导出准备期间发生变化"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    assert fired
    db.rollback()
    assert not (tmp_path / "_controlled_audio").exists()
    assert not list(tmp_path.rglob("*.csv"))
    assert list(db.exec(export.select(ExportArtifact))) == []
    assert all(row.status != "published"
               for row in db.exec(export.select(ExportBatch)))
    assert all(row.status != AudioStatus.exported and row.export_batch_id is None
               for row in db.exec(export.select(AudioAssetRow)))


@pytest.mark.parametrize("statement,params", [
    ("UPDATE patientdevicecapability SET revoked_at = ? WHERE token_hash = ?",
     ("2026-01-01 23:00:00", "t" * 64)),
    ("UPDATE patientdevicecapability SET recovery_only_at = ? "
     "WHERE token_hash = ?", ("2026-01-01 23:00:00", "t" * 64)),
    ("UPDATE patientdevicecapability SET active_session_key = ? "
     "WHERE token_hash = ?", (None, "t" * 64)),
    ("UPDATE runtimecommandack SET device_event_seq = 99 "
     "WHERE ack_type = 'record_stopped'", ()),
])
def test_governance_neutral_changes_between_proofs_are_still_refused(
        db, tmp_path, monkeypatch, statement, params):
    """这些字段不改变历史 ACK 的合法性，只有 fingerprint 能抓住。"""
    _seed(db)
    _seed_repeat_capture(db)
    fired = _between_proofs(
        db=db, monkeypatch=monkeypatch, statement=statement, params=params)

    with pytest.raises(ValueError, match="在导出准备期间发生变化"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    assert fired
    db.rollback()
    assert not (tmp_path / "_controlled_audio").exists()
    assert not list(tmp_path.rglob("*.csv"))
    assert list(db.exec(export.select(ExportArtifact))) == []
    assert all(row.status != "published"
               for row in db.exec(export.select(ExportBatch)))


@pytest.mark.parametrize("statement,params", [
    # Neither column takes part in any business rule, so only a whole-row
    # fingerprint driven by __table__.columns can notice them.
    ("UPDATE runtimecommand SET result_json = ? WHERE kind = 'record'",
     ('{"noted":true}',)),
    ("UPDATE runtimecommand SET updated_at = ? WHERE kind = 'tts'",
     ("2026-06-01 00:00:00",)),
    ("UPDATE runtimecommand SET lease_owner = ?, lease_expires_at = ? "
     "WHERE kind = 'tts' AND state = 'pending'",
     ("someone", "2026-06-01 00:00:00")),
])
def test_whole_row_fingerprint_catches_validity_neutral_column_changes(
        db, tmp_path, monkeypatch, statement, params):
    _seed(db)
    _seed_repeat_capture(db)
    fired = _between_proofs(
        db=db, monkeypatch=monkeypatch, statement=statement, params=params)

    with pytest.raises(ValueError, match="在导出准备期间发生变化"):
        _export_bundle(db, "S9", write_dir=tmp_path)

    assert fired
    db.rollback()
    assert not (tmp_path / "_controlled_audio").exists()
    assert not list(tmp_path.rglob("*.csv"))
    assert list(db.exec(export.select(ExportArtifact))) == []
    assert all(row.status != "published"
               for row in db.exec(export.select(ExportBatch)))
    assert all(row.status != AudioStatus.exported and row.export_batch_id is None
               for row in db.exec(export.select(AudioAssetRow)))


def test_row_fingerprint_covers_every_declared_column():
    """新增列不会静默掉出并发闭环。"""
    from app.models import RuntimeCommand as _Command

    row = _Command(
        idempotency_key="k", session_id="s", command_seq=1, item_id="i",
        turn_seq=1, turn_key="i#1", attempt_seq=1, prompt_level=0,
        control_generation=1, runner_generation=1,
        issued_capability_token_hash="t", issued_device_id_hash="d",
        issued_at=_T0, kind="tts")
    names = [name for name, _ in export._row_fingerprint(
        row, identity=("runtimecommand", 1))]
    assert names == [c.name for c in _Command.__table__.columns]
    assert {"result_json", "lease_owner", "lease_expires_at", "created_at",
            "started_at", "failed_at", "cancelled_at",
            "updated_at"}.issubset(set(names))
    assert export._row_fingerprint(None, identity=("x", 7)) == ("missing", ("x", 7))


def test_artifacts_ready_recovery_publishes_the_repeat_recording_once(
        db, tmp_path, monkeypatch):
    """artifacts_ready 阶段音频尚未推进；同键重试才把它推到 exported，且不重复。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    key = "export-repeat-artifacts-ready-0123456789abcdef01234567"
    batch_id = _prepare_artifacts_ready(db, tmp_path, monkeypatch, key)

    # Files and artifact rows exist, but the recording has not advanced.
    with Session(db.get_bind()) as probe:
        asset = probe.get(AudioAssetRow, ids["raw_audio_id"])
        assert asset.status == AudioStatus.recorded
        assert asset.export_batch_id is None
        assert asset.exported_at is None
    artifacts_before = sorted(
        (row.kind, row.relative_path, row.sha256, row.byte_count)
        for row in db.exec(export.select(ExportArtifact)))
    controlled_before = sorted(
        str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")
        if p.is_file() and "_controlled_audio" in str(p))
    assert controlled_before

    recovered = _export_bundle(db, "S9", write_dir=tmp_path, idempotency_key=key)

    assert recovered["batch_id"] == batch_id
    assert len(list(db.exec(export.select(ExportBatch)))) == 1
    with Session(db.get_bind()) as probe:
        asset = probe.get(AudioAssetRow, ids["raw_audio_id"])
        assert asset.status == AudioStatus.exported
        assert asset.export_batch_id == batch_id
        assert asset.exported_at is not None
        assert probe.get(ExportBatch, batch_id).status == "published"
    assert sorted(
        (row.kind, row.relative_path, row.sha256, row.byte_count)
        for row in db.exec(export.select(ExportArtifact))) == artifacts_before
    assert sorted(
        str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")
        if p.is_file() and "_controlled_audio" in str(p)) == controlled_before


@pytest.mark.parametrize("retry", ["other_actor", "other_batch_id"])
def test_same_key_different_request_after_publish_is_an_idempotency_conflict(
        db, tmp_path, retry):
    """同键不同请求必须是幂等冲突，不能先被 repeat 资产 lifecycle 拦成 ValueError。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    key = "export-repeat-conflict-0123456789abcdef0123456789ab"
    first = _export_bundle(db, "S9", write_dir=tmp_path, idempotency_key=key)

    files_before = sorted(
        (str(p.relative_to(tmp_path)), p.stat().st_size,
         hashlib.sha256(p.read_bytes()).hexdigest())
        for p in tmp_path.rglob("*") if p.is_file())
    artifacts_before = sorted(
        (row.kind, row.relative_path, row.sha256, row.byte_count)
        for row in db.exec(export.select(ExportArtifact)))
    with Session(db.get_bind()) as probe:
        asset_before = probe.get(AudioAssetRow, ids["raw_audio_id"])
        asset_state = (asset_before.status, asset_before.export_batch_id,
                       asset_before.exported_at)

    if retry == "other_actor":
        call = lambda: export.export_session_bundle(  # noqa: E731
            db, "S9", write_dir=tmp_path, idempotency_key=key,
            actor_display_id="OTHER-ADMIN", actor_role="admin")
    else:
        call = lambda: _export_bundle(  # noqa: E731
            db, "S9", write_dir=tmp_path, idempotency_key=key,
            batch_id="EXP-different-batch-0001")

    with pytest.raises(export.ExportIdempotencyConflict):
        call()

    db.rollback()
    assert len(list(db.exec(export.select(ExportBatch)))) == 1
    assert db.exec(export.select(ExportBatch)).first().batch_id == first["batch_id"]
    assert sorted(
        (row.kind, row.relative_path, row.sha256, row.byte_count)
        for row in db.exec(export.select(ExportArtifact))) == artifacts_before
    assert sorted(
        (str(p.relative_to(tmp_path)), p.stat().st_size,
         hashlib.sha256(p.read_bytes()).hexdigest())
        for p in tmp_path.rglob("*") if p.is_file()) == files_before
    with Session(db.get_bind()) as probe:
        asset_after = probe.get(AudioAssetRow, ids["raw_audio_id"])
        assert (asset_after.status, asset_after.export_batch_id,
                asset_after.exported_at) == asset_state


def test_new_key_same_session_after_publish_is_an_idempotency_conflict(
        db, tmp_path):
    """换新键重导同一场次也必须是幂等冲突，而不是 repeat 资产状态错误。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    first = _export_bundle(
        db, "S9", write_dir=tmp_path,
        idempotency_key="export-repeat-scope-first-0123456789abcdef012345")

    files_before = sorted(
        (str(p.relative_to(tmp_path)), p.stat().st_size,
         hashlib.sha256(p.read_bytes()).hexdigest())
        for p in tmp_path.rglob("*") if p.is_file())
    with Session(db.get_bind()) as probe:
        asset = probe.get(AudioAssetRow, ids["raw_audio_id"])
        asset_state = (asset.status, asset.export_batch_id, asset.exported_at)

    with pytest.raises(export.ExportIdempotencyConflict, match="必须使用原幂等键恢复"):
        _export_bundle(
            db, "S9", write_dir=tmp_path,
            idempotency_key="export-repeat-scope-second-0123456789abcdef0123")

    db.rollback()
    batches = list(db.exec(export.select(ExportBatch)))
    assert len(batches) == 1 and batches[0].batch_id == first["batch_id"]
    assert sorted(
        (str(p.relative_to(tmp_path)), p.stat().st_size,
         hashlib.sha256(p.read_bytes()).hexdigest())
        for p in tmp_path.rglob("*") if p.is_file()) == files_before
    with Session(db.get_bind()) as probe:
        asset = probe.get(AudioAssetRow, ids["raw_audio_id"])
        assert (asset.status, asset.export_batch_id,
                asset.exported_at) == asset_state


def test_published_repeat_batch_with_drifted_classification_is_an_integrity_error(
        db, tmp_path):
    """批次分类被改成另一个合法值：同键重试必须报完整性错误，不是资产状态错误。"""
    _seed(db)
    ids = _seed_repeat_capture(db)
    key = "export-repeat-classification-0123456789abcdef01234567"
    first = _export_bundle(db, "S9", write_dir=tmp_path, idempotency_key=key)

    db.connection().exec_driver_sql(
        "UPDATE exportbatch SET data_classification = 'simulation' "
        "WHERE batch_id = ?", (first["batch_id"],))
    db.commit()

    files_before = sorted(
        (str(p.relative_to(tmp_path)), p.stat().st_size,
         hashlib.sha256(p.read_bytes()).hexdigest())
        for p in tmp_path.rglob("*") if p.is_file())
    artifacts_before = sorted(
        (row.kind, row.relative_path, row.sha256, row.byte_count)
        for row in db.exec(export.select(ExportArtifact)))
    with Session(db.get_bind()) as probe:
        asset = probe.get(AudioAssetRow, ids["raw_audio_id"])
        asset_state = (asset.status, asset.export_batch_id, asset.exported_at)

    with pytest.raises(export.ExportArtifactIntegrityError,
                       match="导出批次与场次数据分类不一致"):
        _export_bundle(db, "S9", write_dir=tmp_path, idempotency_key=key)

    db.rollback()
    batches = list(db.exec(export.select(ExportBatch)))
    assert len(batches) == 1 and batches[0].batch_id == first["batch_id"]
    # The seeded corruption is the only difference; everything else is intact.
    assert batches[0].data_classification == "simulation"
    assert batches[0].status == "published"
    assert sorted(
        (row.kind, row.relative_path, row.sha256, row.byte_count)
        for row in db.exec(export.select(ExportArtifact))) == artifacts_before
    assert sorted(
        (str(p.relative_to(tmp_path)), p.stat().st_size,
         hashlib.sha256(p.read_bytes()).hexdigest())
        for p in tmp_path.rglob("*") if p.is_file()) == files_before
    with Session(db.get_bind()) as probe:
        asset = probe.get(AudioAssetRow, ids["raw_audio_id"])
        assert (asset.status, asset.export_batch_id,
                asset.exported_at) == asset_state
