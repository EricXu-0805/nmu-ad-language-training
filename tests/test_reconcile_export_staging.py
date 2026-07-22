from __future__ import annotations

import csv
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine
from sqlmodel import Session

from app.enums import AudioStatus, EventLine, PhaseType
from app.models import (
    AudioAssetRow,
    AudioCaptureReceipt,
    ExportArtifact,
    ExportBatch,
    Patient,
    PatientWithdrawalEvent,
    Session as TrainSession,
)
from scripts import reconcile_export_staging as reconcile
from scripts.verify_backup_snapshot import verify_snapshot


NOW = datetime(2026, 7, 20, 0, 0, 0)
BATCH_ID = "EXP-0123456789abcdef01234567"
OWNER_HASH = "a" * 64
RAW_AUDIO_ID = "aud-synthetic-reconcile"
SESSION_ID = "S-SYNTHETIC-RECONCILE"
TEST_KEY = b"test-only-deidentification-key-32-bytes-minimum"
TEST_KEY_ID = "test-2026"
CODE = "AUDIO-v1-test-2026-" + hmac.new(
    TEST_KEY,
    b"nmu-audio-pseudonym:v1\x00" + RAW_AUDIO_ID.encode("utf-8"),
    hashlib.sha256,
).hexdigest()[:20]
SCOPE_HASH = hmac.new(
    TEST_KEY,
    b"nmu-export-scope\x00" + SESSION_ID.encode("utf-8"),
    hashlib.sha256,
).hexdigest()
SHEETS = {
    "session",
    "turns",
    "attempts",
    "interactions",
    "item_scores",
    "scales",
    "legacy_unverified_scales",
    "abnormal",
    "audio_manifest",
}


@pytest.fixture(scope="module")
def migrated_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("reconcile-current-schema")
    database = directory / "app.db"
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")
    os.chmod(database, 0o600)
    return database


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _private_bytes(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    os.chmod(path, 0o600)
    return path


def _canonical(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _audio_manifest(
    path: Path,
    *,
    controlled: bool,
    code: str = CODE,
) -> None:
    fieldnames = [
        "audio_code",
        "audio_format",
        "controlled_audio_exported",
        "data_classification",
        "is_simulation",
        "status",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        if controlled:
            writer.writerow({
                "audio_code": code,
                "audio_format": "webm",
                "controlled_audio_exported": "True",
                "data_classification": "simulation",
                "is_simulation": "True",
                "status": "exported",
            })
    os.chmod(path, 0o600)


def _fixture(
    tmp_path: Path,
    migrated_database: Path,
    *,
    controlled: bool = True,
) -> dict[str, Path]:
    data = _private_directory(tmp_path / "data")
    quarantine = _private_directory(tmp_path / "quarantine")
    database = data / "app.db"
    shutil.copy2(migrated_database, database)
    os.chmod(database, 0o600)
    analysis = _private_directory(
        data / "exports" / "simulation" / BATCH_ID
    )
    for sheet in SHEETS - {"audio_manifest"}:
        _private_bytes(
            analysis / f"{sheet}.csv",
            b"synthetic\n",
        )
    _audio_manifest(analysis / "audio_manifest.csv", controlled=controlled)
    _private_bytes(
        analysis / ".staging-receipt.json",
        _canonical({
            "batch_id": BATCH_ID,
            "staging_owner_hash": OWNER_HASH,
        }),
    )

    engine = create_engine(f"sqlite:///{database}")
    with Session(engine) as session:
        session.add(Patient(
            patient_id="P-SYNTHETIC-RECONCILE",
            is_simulation_subject=True,
        ))
        session.add(TrainSession(
            session_id=SESSION_ID,
            patient_id="P-SYNTHETIC-RECONCILE",
            week_no=2,
            phase_type=PhaseType.正式训练,
            event_line=EventLine.正式训练,
            item_bank_version_id="synthetic-version",
            is_simulation=True,
            data_classification="simulation",
        ))
        session.add(ExportBatch(
            batch_id=BATCH_ID,
            idempotency_key_hash="1" * 64,
            request_fingerprint="2" * 64,
            export_scope_hash=SCOPE_HASH,
            status="staging",
            data_classification="simulation",
            deidentified=True,
            actor_display_id="synthetic-steward",
            actor_role="data_steward",
            staging_owner_hash=OWNER_HASH,
            staging_lease_expires_at=NOW - timedelta(minutes=1),
            created_at=NOW - timedelta(hours=1),
        ))
        if controlled:
            payload = b"synthetic-controlled-audio"
            checksum = hashlib.sha256(payload).hexdigest()
            session.add(AudioAssetRow(
                raw_audio_id=RAW_AUDIO_ID,
                session_id=SESSION_ID,
                is_simulation=True,
                data_classification="simulation",
                turn_key="item-1:turn-1",
                audio_format="webm",
                status=AudioStatus.recorded,
                checksum=checksum,
                byte_count=len(payload),
            ))
            session.add(AudioCaptureReceipt(
                server_seq=1,
                raw_audio_id=RAW_AUDIO_ID,
                session_id=SESSION_ID,
                turn_key="item-1:turn-1",
                duration_seconds=1.0,
                byte_count=len(payload),
                checksum=checksum,
                data_classification="simulation",
                is_simulation=True,
                contains_direct_identifier=False,
            ))
        session.commit()
    engine.dispose()
    os.chmod(database, 0o600)

    controlled_batch = data / "controlled-audio-exports" / "simulation" / BATCH_ID
    raw_audio = data / "audio" / f"{RAW_AUDIO_ID}.webm"
    if controlled:
        payload = b"synthetic-controlled-audio"
        _private_bytes(
            _private_directory(controlled_batch / "audio") / f"{CODE}.webm",
            payload,
        )
        _private_bytes(
            _private_directory(data / "audio") / raw_audio.name,
            payload,
        )
    return {
        "analysis": analysis,
        "controlled": controlled_batch,
        "data": data,
        "database": database,
        "quarantine": quarantine,
        "raw_audio": raw_audio,
    }


def _run(fixture: dict[str, Path], **kwargs) -> reconcile.ReconciliationResult:
    return reconcile.reconcile_preintent_staging(
        data_root=fixture["data"],
        quarantine_root=fixture["quarantine"],
        batch_id=BATCH_ID,
        confirm_offline=True,
        now=NOW,
        **kwargs,
    )


def _mark_subject_withdrawn(
    fixture: dict[str, Path],
    *,
    deleted: bool,
    with_receipt: bool = True,
) -> None:
    engine = create_engine(f"sqlite:///{fixture['database']}")
    with Session(engine) as session:
        patient = session.get(Patient, "P-SYNTHETIC-RECONCILE")
        audio = session.get(AudioAssetRow, RAW_AUDIO_ID)
        assert patient is not None and audio is not None
        patient.withdrawal_status = "withdrawn"
        patient.consent_status = "withdrawn"
        patient.governance_revision = 1
        audio.withdrawn = True
        audio.withdrawal_status = "isolated_by_subject_withdrawal"
        if deleted:
            audio.status = AudioStatus.deleted
            audio.delete_gate_passed = True
        session.add(patient)
        session.add(audio)
        if with_receipt:
            session.add(PatientWithdrawalEvent(
                event_id="withdrawal-synthetic-reconcile",
                patient_id=patient.patient_id,
                expected_revision=0,
                new_revision=1,
                idempotency_key_sha256="8" * 64,
                request_fingerprint="9" * 64,
                reason_code="participant_request",
                actor_display_id="synthetic-admin",
                actor_role="admin",
                occurred_at=NOW,
                affected_session_count=1,
                affected_audio_count=1,
            ))
        session.commit()
    engine.dispose()
    if deleted:
        fixture["raw_audio"].unlink()


@pytest.mark.parametrize("controlled", [False, True])
def test_reconciles_only_real_preintent_layout_and_keeps_batch(
    tmp_path: Path,
    migrated_database: Path,
    controlled: bool,
) -> None:
    fixture = _fixture(tmp_path, migrated_database, controlled=controlled)
    result = _run(fixture)
    assert result.status == "reconciled"
    assert result.bundle_name is not None
    bundle = fixture["quarantine"] / result.bundle_name
    assert not fixture["analysis"].exists()
    assert (bundle / "analysis").is_dir()
    if controlled:
        assert not fixture["controlled"].exists()
        assert (bundle / "controlled-audio" / "audio").is_dir()
        assert fixture["raw_audio"].read_bytes() == b"synthetic-controlled-audio"
    else:
        assert not (bundle / "controlled-audio").exists()
    assert not (fixture["data"] / "exports").exists()
    assert not (fixture["data"] / "controlled-audio-exports").exists()
    receipt = json.loads(
        (bundle / "reconciliation-receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["database_owner_and_lease_cleared"] is True
    serialized = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    assert "P-SYNTHETIC" not in serialized
    assert RAW_AUDIO_ID not in serialized
    assert BATCH_ID not in serialized
    with sqlite3.connect(fixture["database"]) as connection:
        row = connection.execute(
            "SELECT status,staging_owner_hash,staging_lease_expires_at "
            "FROM exportbatch WHERE batch_id=?",
            (BATCH_ID,),
        ).fetchone()
    assert row == ("staging", None, None)
    assert verify_snapshot(
        fixture["data"],
        require_manifest=False,
    ) == fixture["data"].resolve()


def test_dry_run_makes_no_change(
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    result = _run(fixture, dry_run=True)
    assert result == reconcile.ReconciliationResult(
        "eligible",
        hashlib.sha256(BATCH_ID.encode("ascii")).hexdigest(),
        None,
    )
    assert fixture["analysis"].is_dir()
    assert fixture["controlled"].is_dir()
    assert fixture["analysis"].parent.is_dir()
    assert fixture["analysis"].parent.parent.is_dir()
    assert fixture["controlled"].parent.is_dir()
    assert fixture["controlled"].parent.parent.is_dir()
    assert stat.S_IMODE(fixture["analysis"].parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(fixture["controlled"].parent.stat().st_mode) == 0o700
    assert not tuple(fixture["quarantine"].iterdir())
    with sqlite3.connect(fixture["database"]) as connection:
        owner = connection.execute(
            "SELECT staging_owner_hash FROM exportbatch WHERE batch_id=?",
            (BATCH_ID,),
        ).fetchone()[0]
    assert owner == OWNER_HASH


def test_confirmation_is_mandatory(
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    with pytest.raises(
        reconcile.ReconciliationError,
        match="offline_confirmation_required",
    ):
        reconcile.reconcile_preintent_staging(
            data_root=fixture["data"],
            quarantine_root=fixture["quarantine"],
            batch_id=BATCH_ID,
            confirm_offline=False,
            now=NOW,
        )


def test_rejects_noncurrent_schema_before_touching_files(
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    with sqlite3.connect(fixture["database"]) as connection:
        connection.execute("CREATE TABLE unexpected_schema_object (id INTEGER)")
        connection.commit()
    with pytest.raises(
        reconcile.ReconciliationError,
        match="current_schema_required",
    ):
        _run(fixture)
    assert fixture["analysis"].is_dir()
    assert fixture["controlled"].is_dir()
    assert not tuple(fixture["quarantine"].iterdir())


def test_rejects_nested_quarantine_root(
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    nested = _private_directory(fixture["data"] / "quarantine")
    with pytest.raises(
        reconcile.ReconciliationError,
        match="quarantine_root_not_separate",
    ):
        reconcile.reconcile_preintent_staging(
            data_root=fixture["data"],
            quarantine_root=nested,
            batch_id=BATCH_ID,
            confirm_offline=True,
            now=NOW,
        )
    assert fixture["analysis"].is_dir()


def test_rejects_parent_symlink_alias_for_data_root(
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    real_parent = _private_directory(tmp_path / "real-parent")
    fixture = _fixture(real_parent, migrated_database)
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(
        reconcile.ReconciliationError,
        match="data_root_invalid",
    ):
        reconcile.reconcile_preintent_staging(
            data_root=alias_parent / "data",
            quarantine_root=fixture["quarantine"],
            batch_id=BATCH_ID,
            confirm_offline=True,
            now=NOW,
        )
    assert fixture["analysis"].is_dir()


def test_rejects_hardlinked_database(
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    os.link(fixture["database"], tmp_path / "unexpected-database-link")
    with pytest.raises(
        reconcile.ReconciliationError,
        match="database_file_invalid",
    ):
        _run(fixture)
    assert fixture["analysis"].is_dir()


def test_competing_database_writer_blocks_operation(
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    blocker = sqlite3.connect(fixture["database"], isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(
            reconcile.ReconciliationError,
            match="database_not_offline",
        ):
            _run(fixture)
    finally:
        blocker.rollback()
        blocker.close()
    assert fixture["analysis"].is_dir()


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            "active_lease",
            "staging_lease_active",
        ),
        (
            "partial_manifest",
            "not_preintent_staging",
        ),
        (
            "artifact",
            "export_artifact_binding_present",
        ),
        (
            "audio_binding",
            "export_audio_binding_present",
        ),
    ],
)
def test_rejects_non_preintent_or_bound_ledger_state(
    tmp_path: Path,
    migrated_database: Path,
    mutation: str,
    code: str,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    engine = create_engine(f"sqlite:///{fixture['database']}")
    with Session(engine) as session:
        batch = session.get(ExportBatch, BATCH_ID)
        assert batch is not None
        if mutation == "active_lease":
            batch.staging_lease_expires_at = NOW + timedelta(minutes=1)
        elif mutation == "partial_manifest":
            batch.manifest_sha256 = "4" * 64
        elif mutation == "artifact":
            session.add(ExportArtifact(
                batch_id=BATCH_ID,
                realm="simulation_analysis",
                kind="csv",
                relative_path=f"simulation/{BATCH_ID}/session.csv",
                sha256="5" * 64,
                byte_count=1,
                created_at=NOW,
            ))
        elif mutation == "audio_binding":
            audio = session.get(AudioAssetRow, RAW_AUDIO_ID)
            assert audio is not None
            audio.export_batch_id = BATCH_ID
        session.add(batch)
        session.commit()
    engine.dispose()
    with pytest.raises(reconcile.ReconciliationError, match=code):
        _run(fixture)
    assert fixture["analysis"].is_dir()
    assert fixture["controlled"].is_dir()


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_analysis_file",
        "noncanonical_receipt",
        "wrong_owner",
        "analysis_symlink",
        "analysis_hardlink",
        "empty_controlled_audio",
        "unknown_controlled_file",
    ],
)
def test_rejects_unsafe_or_noncanonical_filesystem_layout(
    tmp_path: Path,
    migrated_database: Path,
    mutation: str,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    if mutation == "unknown_analysis_file":
        _private_bytes(fixture["analysis"] / "unknown.csv", b"x")
    elif mutation == "noncanonical_receipt":
        _private_bytes(
            fixture["analysis"] / ".staging-receipt.json",
            json.dumps({
                "batch_id": BATCH_ID,
                "staging_owner_hash": OWNER_HASH,
            }).encode("utf-8"),
        )
    elif mutation == "wrong_owner":
        _private_bytes(
            fixture["analysis"] / ".staging-receipt.json",
            _canonical({
                "batch_id": BATCH_ID,
                "staging_owner_hash": "b" * 64,
            }),
        )
    elif mutation == "analysis_symlink":
        target = fixture["analysis"] / "session.csv"
        target.unlink()
        target.symlink_to(fixture["analysis"] / "turns.csv")
    elif mutation == "analysis_hardlink":
        target = fixture["analysis"] / "session.csv"
        target.unlink()
        os.link(fixture["analysis"] / "turns.csv", target)
    elif mutation == "empty_controlled_audio":
        next((fixture["controlled"] / "audio").iterdir()).unlink()
    elif mutation == "unknown_controlled_file":
        _private_bytes(fixture["controlled"] / "audio" / "unknown.webm", b"x")
    with pytest.raises(reconcile.ReconciliationError):
        _run(fixture)
    assert fixture["analysis"].is_dir()


@pytest.mark.parametrize(
    "mutation",
    [
        "controlled_tamper",
        "raw_tamper",
        "capture_receipt_missing",
        "classification_mismatch",
    ],
)
def test_rejects_incomplete_controlled_audio_receipt_closure(
    tmp_path: Path,
    migrated_database: Path,
    mutation: str,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    controlled_file = next((fixture["controlled"] / "audio").iterdir())
    if mutation == "controlled_tamper":
        _private_bytes(controlled_file, b"different-controlled-audio")
    elif mutation == "raw_tamper":
        _private_bytes(fixture["raw_audio"], b"different-raw-audio")
    else:
        with sqlite3.connect(fixture["database"]) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            if mutation == "capture_receipt_missing":
                connection.execute(
                    "DELETE FROM audiocapturereceipt WHERE raw_audio_id=?",
                    (RAW_AUDIO_ID,),
                )
            elif mutation == "classification_mismatch":
                connection.execute(
                    "UPDATE audiocapturereceipt SET data_classification='research' "
                    "WHERE raw_audio_id=?",
                    (RAW_AUDIO_ID,),
                )
            connection.commit()
    with pytest.raises(reconcile.ReconciliationError):
        _run(fixture)
    assert fixture["analysis"].is_dir()


@pytest.mark.parametrize("deleted", [False, True])
def test_subject_withdrawal_after_preintent_can_be_quarantined(
    tmp_path: Path,
    migrated_database: Path,
    deleted: bool,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    _mark_subject_withdrawn(fixture, deleted=deleted)
    if not deleted:
        with pytest.raises(
            reconcile.ReconciliationError,
            match="withdrawn_audio_cleanup_required",
        ):
            _run(fixture)
        assert fixture["analysis"].is_dir()
        assert fixture["controlled"].is_dir()
        assert fixture["raw_audio"].is_file()
        return
    result = _run(fixture)
    assert result.status == "reconciled"
    bundle = fixture["quarantine"] / str(result.bundle_name)
    assert (bundle / "controlled-audio" / "audio" / f"{CODE}.webm").is_file()
    assert not fixture["raw_audio"].exists()
    assert verify_snapshot(
        fixture["data"],
        require_manifest=False,
    ) == fixture["data"].resolve()


def test_withdrawal_flags_without_immutable_receipt_are_rejected(
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    _mark_subject_withdrawn(fixture, deleted=False, with_receipt=False)
    with pytest.raises(
        reconcile.ReconciliationError,
        match="raw_audio_receipt_invalid",
    ):
        _run(fixture)
    assert fixture["analysis"].is_dir()
    assert fixture["controlled"].is_dir()


def test_arbitrary_valid_shape_audio_token_is_rejected(
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    fake_code = "AUDIO-v1-test-2026-ffffffffffffffffffff"
    original = fixture["controlled"] / "audio" / f"{CODE}.webm"
    replacement = fixture["controlled"] / "audio" / f"{fake_code}.webm"
    original.rename(replacement)
    _audio_manifest(
        fixture["analysis"] / "audio_manifest.csv",
        controlled=True,
        code=fake_code,
    )
    with pytest.raises(
        reconcile.ReconciliationError,
        match="raw_audio_receipt_invalid",
    ):
        _run(fixture)
    assert replacement.is_file()


def test_cross_session_controlled_audio_substitution_is_rejected(
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    other_raw_id = "aud-other-session"
    other_session_id = "S-OTHER-SYNTHETIC"
    other_payload = b"synthetic-other-controlled-audio"
    other_checksum = hashlib.sha256(other_payload).hexdigest()
    other_code = "AUDIO-v1-test-2026-" + hmac.new(
        TEST_KEY,
        b"nmu-audio-pseudonym:v1\x00" + other_raw_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:20]
    engine = create_engine(f"sqlite:///{fixture['database']}")
    with Session(engine) as session:
        session.add(Patient(
            patient_id="P-OTHER-SYNTHETIC",
            is_simulation_subject=True,
        ))
        session.add(TrainSession(
            session_id=other_session_id,
            patient_id="P-OTHER-SYNTHETIC",
            week_no=2,
            phase_type=PhaseType.正式训练,
            event_line=EventLine.正式训练,
            item_bank_version_id="synthetic-version",
            is_simulation=True,
            data_classification="simulation",
        ))
        session.add(AudioAssetRow(
            raw_audio_id=other_raw_id,
            session_id=other_session_id,
            is_simulation=True,
            data_classification="simulation",
            turn_key="item-other:turn-1",
            audio_format="webm",
            status=AudioStatus.recorded,
            checksum=other_checksum,
            byte_count=len(other_payload),
        ))
        session.add(AudioCaptureReceipt(
            server_seq=2,
            raw_audio_id=other_raw_id,
            session_id=other_session_id,
            turn_key="item-other:turn-1",
            duration_seconds=1.0,
            byte_count=len(other_payload),
            checksum=other_checksum,
            data_classification="simulation",
            is_simulation=True,
            contains_direct_identifier=False,
        ))
        session.commit()
    engine.dispose()
    controlled_audio = next((fixture["controlled"] / "audio").iterdir())
    controlled_audio.unlink()
    _private_bytes(
        fixture["controlled"] / "audio" / f"{other_code}.webm",
        other_payload,
    )
    _private_bytes(
        fixture["data"] / "audio" / f"{other_raw_id}.webm",
        other_payload,
    )
    _audio_manifest(
        fixture["analysis"] / "audio_manifest.csv",
        controlled=True,
        code=other_code,
    )
    with pytest.raises(
        reconcile.ReconciliationError,
        match="raw_audio_receipt_invalid",
    ):
        _run(fixture)


def test_controlled_audio_aggregate_limit_is_fail_closed(
    tmp_path: Path,
    migrated_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    monkeypatch.setattr(reconcile, "_MAX_CONTROLLED_TOTAL_BYTES", 1)
    with pytest.raises(
        reconcile.ReconciliationError,
        match="controlled_audio_size_limit_exceeded",
    ):
        _run(fixture)
    assert fixture["analysis"].is_dir()


def test_second_rename_failure_rolls_first_move_back(
    tmp_path: Path,
    migrated_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    real_rename = reconcile.os.rename

    def fail_controlled(source, destination):
        if Path(source) == fixture["controlled"]:
            raise OSError("synthetic rename failure")
        return real_rename(source, destination)

    monkeypatch.setattr(reconcile.os, "rename", fail_controlled)
    with pytest.raises(
        reconcile.ReconciliationError,
        match="quarantine_atomic_rename_failed",
    ):
        _run(fixture)
    assert fixture["analysis"].is_dir()
    assert fixture["controlled"].is_dir()
    assert not tuple(fixture["quarantine"].iterdir())


def test_ambiguous_database_commit_retains_quarantine_evidence(
    tmp_path: Path,
    migrated_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    real_commit = reconcile._commit_release

    def uncertain(_connection):
        raise sqlite3.OperationalError("synthetic ambiguous commit")

    monkeypatch.setattr(reconcile, "_commit_release", uncertain)
    with pytest.raises(
        reconcile.ReconciliationError,
        match="database_commit_uncertain",
    ):
        _run(fixture)
    assert not fixture["analysis"].exists()
    assert not fixture["controlled"].exists()
    bundles = tuple(fixture["quarantine"].iterdir())
    assert len(bundles) == 1
    assert (bundles[0] / "quarantine-intent.json").is_file()
    assert (bundles[0] / "database-commit-uncertain.json").is_file()
    serialized = (bundles[0] / "quarantine-intent.json").read_text(
        encoding="utf-8"
    )
    assert RAW_AUDIO_ID not in serialized
    with sqlite3.connect(fixture["database"]) as connection:
        owner = connection.execute(
            "SELECT staging_owner_hash FROM exportbatch WHERE batch_id=?",
            (BATCH_ID,),
        ).fetchone()[0]
    assert owner == OWNER_HASH
    # A second named operator may resume the same deterministic bundle only
    # after confirming that the first ambiguous commit in fact rolled back.
    monkeypatch.setattr(reconcile, "_commit_release", real_commit)
    result = _run(fixture)
    assert result.status == "reconciled"
    assert result.bundle_name == bundles[0].name
    assert (bundles[0] / "reconciliation-receipt.json").is_file()
    assert (bundles[0] / "database-commit-uncertain.json").is_file()


def test_commit_that_succeeds_then_raises_can_finalize_on_retry(
    tmp_path: Path,
    migrated_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    real_commit = reconcile._commit_release

    def committed_then_raised(connection):
        real_commit(connection)
        raise sqlite3.OperationalError("synthetic lost commit acknowledgement")

    monkeypatch.setattr(reconcile, "_commit_release", committed_then_raised)
    with pytest.raises(
        reconcile.ReconciliationError,
        match="database_commit_uncertain",
    ):
        _run(fixture)
    with sqlite3.connect(fixture["database"]) as connection:
        assert connection.execute(
            "SELECT staging_owner_hash,staging_lease_expires_at "
            "FROM exportbatch WHERE batch_id=?",
            (BATCH_ID,),
        ).fetchone() == (None, None)
    monkeypatch.setattr(reconcile, "_commit_release", real_commit)
    resumed = _run(fixture)
    assert resumed.status == "reconciled"
    bundle = fixture["quarantine"] / str(resumed.bundle_name)
    assert (bundle / "reconciliation-receipt.json").is_file()
    assert (bundle / "database-commit-uncertain.json").is_file()


def test_receipt_write_failure_after_commit_can_finalize_on_retry(
    tmp_path: Path,
    migrated_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    real_write = reconcile._atomic_write_json

    def fail_receipt(path, value):
        if Path(path).name == "reconciliation-receipt.json":
            raise reconcile.ReconciliationError("synthetic_receipt_failure")
        return real_write(path, value)

    monkeypatch.setattr(reconcile, "_atomic_write_json", fail_receipt)
    with pytest.raises(
        reconcile.ReconciliationError,
        match="synthetic_receipt_failure",
    ):
        _run(fixture)
    with sqlite3.connect(fixture["database"]) as connection:
        assert connection.execute(
            "SELECT staging_owner_hash FROM exportbatch WHERE batch_id=?",
            (BATCH_ID,),
        ).fetchone() == (None,)
    monkeypatch.setattr(reconcile, "_atomic_write_json", real_write)
    resumed = _run(fixture)
    assert resumed.status == "reconciled"
    assert (
        fixture["quarantine"]
        / str(resumed.bundle_name)
        / "reconciliation-receipt.json"
    ).is_file()


def test_postcommit_probe_failure_can_finalize_on_retry(
    tmp_path: Path,
    migrated_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    real_probe = reconcile._postcommit_verify

    def fail_probe(_data, _batch_id):
        raise reconcile.ReconciliationError("synthetic_postcommit_probe_failure")

    monkeypatch.setattr(reconcile, "_postcommit_verify", fail_probe)
    with pytest.raises(
        reconcile.ReconciliationError,
        match="synthetic_postcommit_probe_failure",
    ):
        _run(fixture)
    bundles = tuple(fixture["quarantine"].iterdir())
    assert len(bundles) == 1
    assert not (bundles[0] / "reconciliation-receipt.json").exists()
    monkeypatch.setattr(reconcile, "_postcommit_verify", real_probe)
    resumed = _run(fixture)
    assert resumed.status == "reconciled"
    assert (bundles[0] / "reconciliation-receipt.json").is_file()


def test_cli_json_error_is_stable_and_non_identifying(
    tmp_path: Path,
    migrated_database: Path,
) -> None:
    fixture = _fixture(tmp_path, migrated_database)
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "scripts/reconcile_export_staging.py",
            "--json",
            "--data-root",
            str(fixture["data"]),
            "--quarantine-root",
            str(fixture["quarantine"]),
            "--batch-id",
            BATCH_ID,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert json.loads(completed.stdout) == {
        "code": "offline_confirmation_required",
        "status": "error",
    }
    assert completed.stderr == ""
    assert RAW_AUDIO_ID not in completed.stdout
