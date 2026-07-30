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

from app import audio_store, export, export_security
from app.enums import AudioStatus
from app.export import DIRECT_IDENTIFIER_COLUMNS, mask_text, pseudonymize
from app.models import (
    AbnormalEvent, AudioAssetRow, ExportArtifact, ExportBatch, ItemEvent, Patient,
    ScaleResult, Session as TrainSession, SessionCloseoutReport,
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
    s.add(TrainSession(session_id="S9", patient_id="P77", week_no=2,
                       phase_type="正式训练", event_line="正式训练", item_bank_version_id="wk2-v1-20260707"))
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
            )).scalar_one() == "c7d4f9a1e603"


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
