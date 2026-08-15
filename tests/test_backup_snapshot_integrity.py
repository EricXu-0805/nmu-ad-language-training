from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlmodel import SQLModel

from app import models as _models  # noqa: F401 - registers all recovery tables


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "verify_backup_snapshot.py"
_SPEC = importlib.util.spec_from_file_location("verify_backup_snapshot", GUARD)
assert _SPEC is not None and _SPEC.loader is not None
_GUARD_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_GUARD_MODULE)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _database(snapshot: Path, rows: list[tuple] = ()) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{snapshot / 'app.db'}")
    command.upgrade(config, "head")
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        connection.executemany(
            "INSERT INTO audioassetrow "
            "(raw_audio_id,status,withdrawn,withdrawal_status,checksum,byte_count,"
            "audio_format,is_simulation,data_classification,patient_turn_ref_version,"
            "is_reliability_sample,contains_direct_identifier,delete_gate_passed) "
            "VALUES (?,?,?,?,?,?,?,0,'research',2,0,0,0)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def _published_export_fixture(
        snapshot: Path, *, status: str = "published") -> dict[str, Path]:
    batch_id = "BATCH-SYNTHETIC"
    classification = "simulation"
    sheet_names = (
        "session", "turns", "attempts", "interactions", "item_scores",
        "scales", "legacy_unverified_scales", "abnormal", "audio_manifest",
    )
    audio_code = "AUDIO-v1-test-key-0123456789abcdef0123"
    raw_audio_id = "raw-synthetic-export"
    controlled_relative = (
        f"{classification}/{batch_id}/audio/{audio_code}.webm"
    )
    controlled_path = (
        snapshot / "controlled-audio-exports" / controlled_relative
    )
    controlled_path.parent.mkdir(parents=True)
    controlled_path.write_bytes(b"synthetic-controlled-audio")
    raw_audio_path = snapshot / "audio" / f"{raw_audio_id}.webm"
    raw_audio_path.parent.mkdir()
    raw_audio_path.write_bytes(controlled_path.read_bytes())
    descriptors = []
    analysis_path = snapshot / "exports" / classification / batch_id / "session.csv"
    analysis_path.parent.mkdir(parents=True)
    for sheet_name in sheet_names:
        path = analysis_path.parent / f"{sheet_name}.csv"
        path.write_bytes(f"synthetic,{sheet_name}\n".encode("utf-8"))
        descriptors.append({
            "realm": "simulation_analysis",
            "kind": "csv",
            "relative_path": f"{classification}/{batch_id}/{sheet_name}.csv",
            "sha256": _digest(path.read_bytes()),
            "byte_count": path.stat().st_size,
        })
    receipt = {
        "batch_id": batch_id,
        "staging_owner_hash": "4" * 64,
    }
    receipt_bytes = json.dumps(
        receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    receipt_relative = f"{classification}/{batch_id}/.staging-receipt.json"
    receipt_path = snapshot / "exports" / receipt_relative
    receipt_path.write_bytes(receipt_bytes)
    descriptors.append({
        "realm": "simulation_analysis",
        "kind": "staging_receipt",
        "relative_path": receipt_relative,
        "sha256": _digest(receipt_bytes),
        "byte_count": len(receipt_bytes),
    })
    descriptors.append({
        "realm": "simulation_controlled_audio",
        "kind": "controlled_audio",
        "relative_path": controlled_relative,
        "sha256": _digest(controlled_path.read_bytes()),
        "byte_count": controlled_path.stat().st_size,
    })
    metadata = {
        "audio_touched": [audio_code],
        "excluded_items": [],
        "sheet_counts": {name: 1 for name in sheet_names},
    }
    manifest = {
        "schema_version": "export-manifest.v1",
        "batch_id": batch_id,
        "deidentified": True,
        "data_classification": classification,
        "artifacts": sorted(
            descriptors,
            key=lambda item: (
                item["realm"], item["kind"], item["relative_path"],
            ),
        ),
        "result": metadata,
    }
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    manifest_relative = f"{classification}/{batch_id}/manifest.json"
    manifest_path = snapshot / "exports" / manifest_relative
    manifest_path.write_bytes(manifest_bytes)
    manifest_descriptor = {
        "realm": "simulation_analysis",
        "kind": "manifest",
        "relative_path": manifest_relative,
        "sha256": _digest(manifest_bytes),
        "byte_count": len(manifest_bytes),
    }
    now = "2026-07-19 00:00:00"
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        connection.execute(
            "INSERT INTO exportbatch "
            "(batch_id,schema_version,idempotency_key_hash,request_fingerprint,"
            "export_scope_hash,status,data_classification,deidentified,"
            "actor_display_id,actor_role,result_metadata_json,manifest_sha256,"
            "publication_manifest_json,created_at,artifacts_ready_at,published_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                batch_id,
                "export-batch.v1",
                "1" * 64,
                "2" * 64,
                "3" * 64,
                status,
                classification,
                1,
                "synthetic-data-steward",
                "data_steward",
                json.dumps(metadata, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")),
                _digest(manifest_bytes),
                manifest_bytes.decode("utf-8"),
                now,
                None if status == "staging" else now,
                now if status == "published" else None,
            ),
        )
        connection.executemany(
            "INSERT INTO exportartifact "
            "(batch_id,realm,kind,relative_path,sha256,byte_count,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (
                    batch_id,
                    item["realm"],
                    item["kind"],
                    item["relative_path"],
                    item["sha256"],
                    item["byte_count"],
                    now,
                )
                for item in [*descriptors, manifest_descriptor]
            ],
        )
        connection.execute(
            "INSERT INTO audioassetrow "
            "(raw_audio_id,status,withdrawn,withdrawal_status,checksum,byte_count,"
            "audio_format,is_simulation,data_classification,patient_turn_ref_version,"
            "is_reliability_sample,contains_direct_identifier,delete_gate_passed,"
            "export_batch_id,exported_at) "
            "VALUES (?,?,?,?,?,?,?,1,?,2,0,0,0,?,?)",
            (
                raw_audio_id,
                "exported" if status == "published" else "recorded",
                0,
                None,
                _digest(raw_audio_path.read_bytes()),
                raw_audio_path.stat().st_size,
                "webm",
                classification,
                batch_id if status == "published" else None,
                now if status == "published" else None,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "analysis": analysis_path,
        "controlled": controlled_path,
        "manifest": manifest_path,
        "receipt": receipt_path,
        "raw_audio": raw_audio_path,
    }


def _manifest(snapshot: Path) -> None:
    lines: list[str] = []
    for path in sorted(snapshot.rglob("*")):
        if path.is_file() and path.name != "MANIFEST.sha256":
            rel = path.relative_to(snapshot).as_posix()
            lines.append(f"{_digest(path.read_bytes())}  ./{rel}\n")
    (snapshot / "MANIFEST.sha256").write_text("".join(lines), encoding="utf-8")


def _harden(snapshot: Path) -> None:
    if not snapshot.exists():
        return
    for current, directories, files in os.walk(snapshot):
        os.chmod(current, 0o700)
        for name in directories:
            os.chmod(Path(current) / name, 0o700)
        for name in files:
            os.chmod(Path(current) / name, 0o600)


def _run(command: str, *paths: Path, harden: bool = True) -> subprocess.CompletedProcess[str]:
    if harden:
        for path in paths:
            _harden(path)
    return subprocess.run(
        [sys.executable, "-I", str(GUARD), command, *(str(path) for path in paths)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_complete_authoritative_audio_snapshot_passes(tmp_path):
    snapshot = tmp_path / "snapshot"
    audio = snapshot / "audio"
    audio.mkdir(parents=True)
    payload = b"authoritative-final-audio"
    (audio / "aud-normal.webm").write_bytes(payload)
    _database(snapshot, [
        ("aud-normal", "recorded", 0, None, _digest(payload), len(payload), "webm"),
        ("aud-registered-only", "recorded", 0, None, None, None, "mp3"),
    ])
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "OK"


def test_empty_manifest_only_snapshot_is_rejected(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "MANIFEST.sha256").write_text("", encoding="utf-8")

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=database_missing" in completed.stderr


def test_decoy_sqlite_without_current_audio_schema_is_rejected(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        connection.execute("CREATE TABLE decoy (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=alembic_revision_missing" in completed.stderr


def test_validator_recovery_contract_tracks_models_and_single_alembic_head():
    assert _GUARD_MODULE.REQUIRED_APPLICATION_TABLES == frozenset(
        SQLModel.metadata.tables
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    assert _GUARD_MODULE.SUPPORTED_ALEMBIC_HEADS == frozenset(
        ScriptDirectory.from_config(config).get_heads()
    )


def test_table_ddl_canonicalization_is_order_stable_and_literal_exact():
    first = (
        "CREATE TABLE sample (value TEXT DEFAULT 'two  spaces', "
        "CONSTRAINT ck_value CHECK (value IN ('a,b', 'c'))) WITHOUT ROWID"
    )
    reordered = (
        "CREATE TABLE sample (CONSTRAINT ck_value CHECK (value IN ('a,b', 'c')), "
        "value TEXT DEFAULT 'two  spaces') WITHOUT ROWID"
    )
    changed_literal = reordered.replace("two  spaces", "two spaces")

    assert _GUARD_MODULE._canonical_table_sql(first) \
        == _GUARD_MODULE._canonical_table_sql(reordered)
    assert _GUARD_MODULE._canonical_table_sql(first) \
        != _GUARD_MODULE._canonical_table_sql(changed_literal)


def test_real_alembic_upgrade_head_schema_is_accepted(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{snapshot / 'app.db'}")
    command.upgrade(config, "head")
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "OK"


@pytest.mark.parametrize("status", ["artifacts_ready", "published"])
def test_export_ledger_and_both_file_realms_close_exactly(status, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    _published_export_fixture(snapshot, status=status)
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "OK"


@pytest.mark.parametrize("realm", ["analysis", "controlled"])
def test_export_ledger_missing_file_is_rejected(realm, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    paths = _published_export_fixture(snapshot)
    paths[realm].unlink()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=export_artifact_missing" in completed.stderr


@pytest.mark.parametrize("realm", ["analysis", "controlled"])
def test_export_ledger_tampered_file_is_rejected(realm, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    paths = _published_export_fixture(snapshot)
    paths[realm].write_bytes(b"tampered-after-publication")
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=export_artifact_mismatch" in completed.stderr


@pytest.mark.parametrize(
    ("root_name", "relative"),
    [
        ("exports", "simulation/BATCH-SYNTHETIC/orphan.csv"),
        (
            "controlled-audio-exports",
            "simulation/BATCH-SYNTHETIC/audio/orphan.webm",
        ),
    ],
)
def test_export_orphan_file_in_either_realm_is_rejected(
        root_name, relative, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    _published_export_fixture(snapshot)
    orphan = snapshot / root_name / relative
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"unreceipted-export")
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=export_file_set_mismatch" in completed.stderr


def test_materialized_staging_export_must_settle_before_snapshot(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    _published_export_fixture(snapshot, status="staging")
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=export_staging_unsettled" in completed.stderr


@pytest.mark.parametrize("manifest_present", [True, False])
def test_exact_staging_publication_intent_is_recoverable_snapshot(
        manifest_present, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    paths = _published_export_fixture(snapshot, status="staging")
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        # Publication intent is durable before the append-only artifact ledger.
        connection.execute("DELETE FROM exportartifact")
        connection.commit()
    finally:
        connection.close()
    if not manifest_present:
        paths["manifest"].unlink()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "OK"


def test_staging_publication_intent_requires_every_described_artifact(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    paths = _published_export_fixture(snapshot, status="staging")
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        connection.execute("DELETE FROM exportartifact")
        connection.commit()
    finally:
        connection.close()
    paths["analysis"].unlink()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=export_artifact_missing" in completed.stderr


def test_staging_publication_intent_must_not_keep_filesystem_lease(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    _published_export_fixture(snapshot, status="staging")
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        connection.execute("DELETE FROM exportartifact")
        connection.execute(
            "UPDATE exportbatch SET staging_owner_hash=?,"
            "staging_lease_expires_at=? WHERE batch_id=?",
            ("5" * 64, "2026-07-19 00:30:00", "BATCH-SYNTHETIC"),
        )
        connection.commit()
    finally:
        connection.close()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=export_staging_unsettled" in completed.stderr


def test_staging_publication_intent_hash_and_json_are_atomic(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    _published_export_fixture(snapshot, status="staging")
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        connection.execute("DELETE FROM exportartifact")
        connection.execute(
            "UPDATE exportbatch SET publication_manifest_json=NULL "
            "WHERE batch_id=?",
            ("BATCH-SYNTHETIC",),
        )
        connection.commit()
    finally:
        connection.close()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=export_staging_unsettled" in completed.stderr


@pytest.mark.parametrize(
    "relative",
    [
        "exports",
        "exports/simulation",
        "exports/simulation/BATCH-ORPHAN",
        "controlled-audio-exports",
        "controlled-audio-exports/research",
        "controlled-audio-exports/research/BATCH-ORPHAN/audio",
    ],
)
def test_empty_export_scaffolding_or_orphan_directory_is_rejected(
        relative, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    (snapshot / relative).mkdir(parents=True)
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=export_directory_set_mismatch" in completed.stderr


def test_published_controlled_artifact_requires_audio_ledger_binding(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    paths = _published_export_fixture(snapshot)
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        connection.execute("DELETE FROM audioassetrow")
        connection.commit()
    finally:
        connection.close()
    paths["raw_audio"].unlink()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=export_audio_binding_invalid" in completed.stderr


def test_artifacts_ready_must_not_prebind_audio_rows(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    _published_export_fixture(snapshot, status="artifacts_ready")
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        connection.execute(
            "UPDATE audioassetrow SET status='exported',export_batch_id=?,"
            "exported_at=?",
            ("BATCH-SYNTHETIC", "2026-07-19 00:00:00"),
        )
        connection.commit()
    finally:
        connection.close()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=export_audio_binding_invalid" in completed.stderr


def test_export_manifest_file_must_be_canonical_bytes(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    paths = _published_export_fixture(snapshot)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    pretty_bytes = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, indent=2,
    ).encode("utf-8")
    paths["manifest"].write_bytes(pretty_bytes)
    checksum = _digest(pretty_bytes)
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        connection.execute(
            "UPDATE exportbatch SET manifest_sha256=? WHERE batch_id=?",
            (checksum, "BATCH-SYNTHETIC"),
        )
        connection.execute(
            "UPDATE exportartifact SET sha256=?,byte_count=? "
            "WHERE batch_id=? AND kind='manifest'",
            (checksum, len(pretty_bytes), "BATCH-SYNTHETIC"),
        )
        connection.commit()
    finally:
        connection.close()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=export_manifest_invalid" in completed.stderr


def test_export_result_metadata_must_be_canonical_bytes(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    _published_export_fixture(snapshot)
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        raw = connection.execute(
            "SELECT result_metadata_json FROM exportbatch WHERE batch_id=?",
            ("BATCH-SYNTHETIC",),
        ).fetchone()[0]
        pretty = json.dumps(
            json.loads(raw), ensure_ascii=False, sort_keys=True, indent=2,
        )
        connection.execute(
            "UPDATE exportbatch SET result_metadata_json=? WHERE batch_id=?",
            (pretty, "BATCH-SYNTHETIC"),
        )
        connection.commit()
    finally:
        connection.close()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=export_metadata_invalid" in completed.stderr


@pytest.mark.parametrize("terminal_state", ["deleted", "withdrawn"])
def test_published_controlled_copy_survives_raw_terminal_disposition(
        terminal_state, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    paths = _published_export_fixture(snapshot)
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        if terminal_state == "deleted":
            connection.execute("UPDATE audioassetrow SET status='deleted'")
        else:
            connection.execute(
                "UPDATE audioassetrow SET withdrawn=1,withdrawal_status=?",
                ("isolated_by_subject_withdrawal",),
            )
        connection.commit()
    finally:
        connection.close()
    paths["raw_audio"].unlink()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 0, completed.stderr
    assert paths["controlled"].is_file()


def test_same_column_names_with_weakened_constraints_are_rejected(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        table = "providerreadinessprobe"
        columns = [
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})")
        ]
        connection.execute(f"DROP TABLE {table}")
        declaration = ",".join(f'"{name}" TEXT' for name in columns)
        connection.execute(f"CREATE TABLE {table} ({declaration})")
        connection.commit()
    finally:
        connection.close()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=recovery_schema_incomplete" in completed.stderr


def test_removed_check_constraint_is_rejected(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        table = "patientdevicecapability"
        original_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()[0]
        check_clause = (
            ", \n\tCONSTRAINT ck_patient_device_capability_autopilot_seq_nonnegative "
            "CHECK (last_autopilot_event_seq >= 0)"
        )
        weakened_sql = original_sql.replace(check_clause, "")
        assert weakened_sql != original_sql
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
            (weakened_sql, table),
        )
        connection.execute("PRAGMA writable_schema=OFF")
        connection.execute(f"PRAGMA schema_version={schema_version + 1}")
        connection.commit()
    finally:
        connection.close()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=recovery_schema_incomplete" in completed.stderr


def test_removed_runtime_replay_requires_repeat_binding_check_is_rejected(
        tmp_path):
    """删掉 replay->repeat 绑定 CHECK 后，即使重算 manifest 也必须被 schema 层拒绝。

    这条约束是"重播命令必须带完整重复请求绑定"的唯一数据库级保证；一份把它
    悄悄去掉的恢复快照在字节层面完全自洽，只能由 schema 校验器挡下来。
    """
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        table = "runtimecommand"
        constraint = "ck_runtime_command_replay_requires_repeat_binding"
        original_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()[0]
        check_clause = (
            ", \n\tCONSTRAINT " + constraint + " CHECK ("
            + _models.REPLAY_REQUIRES_REPEAT_BINDING_CHECK + ")"
        )
        assert original_sql.count(check_clause) == 1
        weakened_sql = original_sql.replace(check_clause, "", 1)
        assert weakened_sql != original_sql
        assert constraint not in weakened_sql
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
            (weakened_sql, table),
        )
        connection.execute("PRAGMA writable_schema=OFF")
        connection.execute(f"PRAGMA schema_version={schema_version + 1}")
        connection.commit()
    finally:
        connection.close()
    # Recomputed on purpose: without it the run would stop at
    # manifest_hash_mismatch and never reach the schema verifier under test.
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert completed.stderr.strip() == "REJECTED code=recovery_schema_incomplete"


def test_generated_hidden_column_is_rejected(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        connection.execute(
            "ALTER TABLE patient ADD COLUMN injected_generated TEXT "
            "GENERATED ALWAYS AS (patient_id) VIRTUAL"
        )
        connection.commit()
    finally:
        connection.close()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=recovery_schema_incomplete" in completed.stderr


def test_column_collation_drift_is_rejected(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        table = "patient"
        original_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()[0]
        changed_sql = original_sql.replace(
            "dementia_severity VARCHAR,",
            "dementia_severity VARCHAR COLLATE NOCASE,",
            1,
        )
        assert changed_sql != original_sql
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
            (changed_sql, table),
        )
        connection.execute("PRAGMA writable_schema=OFF")
        connection.execute(f"PRAGMA schema_version={schema_version + 1}")
        connection.commit()
    finally:
        connection.close()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=recovery_schema_incomplete" in completed.stderr


def test_foreign_key_deferrability_drift_is_rejected(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        table = "session"
        original_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()[0]
        fk_sql = "FOREIGN KEY(patient_id) REFERENCES patient (patient_id)"
        changed_sql = original_sql.replace(
            fk_sql,
            f"{fk_sql} DEFERRABLE INITIALLY DEFERRED",
            1,
        )
        assert changed_sql != original_sql
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
            (changed_sql, table),
        )
        connection.execute("PRAGMA writable_schema=OFF")
        connection.execute(f"PRAGMA schema_version={schema_version + 1}")
        connection.commit()
    finally:
        connection.close()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=recovery_schema_incomplete" in completed.stderr


def test_same_index_columns_under_a_different_name_are_rejected(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        connection.execute("DROP INDEX ix_abnormalevent_session_id")
        connection.execute(
            "CREATE INDEX renamed_index ON abnormalevent (session_id)"
        )
        connection.commit()
    finally:
        connection.close()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=recovery_schema_incomplete" in completed.stderr


@pytest.mark.parametrize("ddl", [
    "CREATE TABLE injected_extra (id INTEGER PRIMARY KEY)",
    "CREATE VIEW injected_extra AS SELECT patient_id FROM patient",
    (
        "CREATE TRIGGER injected_extra AFTER DELETE ON patient "
        "BEGIN SELECT 1; END"
    ),
])
def test_unexpected_persistent_schema_object_is_rejected(ddl, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        connection.execute(ddl)
        connection.commit()
    finally:
        connection.close()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=recovery_schema_incomplete" in completed.stderr


def test_vps_contract_requires_complete_nonempty_config_set(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    _manifest(snapshot)
    assert _run("verify", snapshot).returncode == 0

    missing = _run("verify-vps", snapshot)
    assert missing.returncode == 2
    assert "code=vps_config_missing" in missing.stderr

    config = snapshot / "config"
    config.mkdir()
    for name in _GUARD_MODULE.VPS_CONFIG_FILES:
        (config / name).write_text(f"synthetic {name}\n", encoding="utf-8")
    _manifest(snapshot)
    assert _run("verify-vps", snapshot).returncode == 0

    (config / "nmu-backup.timer").write_bytes(b"")
    _manifest(snapshot)
    incomplete = _run("verify-vps", snapshot)
    assert incomplete.returncode == 2
    assert "code=vps_config_invalid" in incomplete.stderr


@pytest.mark.parametrize(("mutation", "code"), [
    ("missing", "alembic_revision_missing"),
    ("unsupported", "alembic_revision_unsupported"),
])
def test_missing_or_unsupported_alembic_revision_is_rejected(mutation, code, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        if mutation == "missing":
            connection.execute("DROP TABLE alembic_version")
        else:
            connection.execute("UPDATE alembic_version SET version_num='old-revision'")
            connection.execute("DROP TABLE week1profile")
        connection.commit()
    finally:
        connection.close()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert f"code={code}" in completed.stderr


def test_foreign_key_violation_is_rejected(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("CREATE TABLE fk_parent (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE fk_child (parent_id INTEGER REFERENCES fk_parent(id))"
        )
        connection.execute("INSERT INTO fk_child(parent_id) VALUES (99)")
        connection.commit()
    finally:
        connection.close()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=sqlite_foreign_key_check_failed" in completed.stderr


def test_physical_index_content_corruption_is_rejected(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    database = snapshot / "app.db"
    connection = sqlite3.connect(database)
    try:
        connection.executemany(
            "INSERT INTO abnormalevent "
            "(session_id,abnormal_type,affects_scoring_validity) VALUES (?,?,0)",
            (("session-a", "type-z"), ("session-b", "type-a")),
        )
        original_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            ("ix_abnormalevent_session_id",),
        ).fetchone()[0]
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='index' AND name=?",
            (
                "CREATE INDEX ix_abnormalevent_session_id "
                "ON abnormalevent (abnormal_type)",
                "ix_abnormalevent_session_id",
            ),
        )
        connection.execute("PRAGMA writable_schema=OFF")
        connection.execute(f"PRAGMA schema_version={schema_version + 1}")
        connection.commit()
    finally:
        connection.close()

    connection = sqlite3.connect(database)
    try:
        connection.execute("REINDEX ix_abnormalevent_session_id")
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='index' AND name=?",
            (original_sql, "ix_abnormalevent_session_id"),
        )
        connection.execute("PRAGMA writable_schema=OFF")
        connection.execute(f"PRAGMA schema_version={schema_version + 1}")
        connection.commit()
    finally:
        connection.close()

    probe = sqlite3.connect(database)
    try:
        assert probe.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert probe.execute("PRAGMA integrity_check").fetchone() != ("ok",)
    finally:
        probe.close()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=sqlite_integrity_check_failed" in completed.stderr


def test_delete_between_database_snapshot_and_audio_copy_fails_closed(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    payload = b"file-was-deleted-after-db-snapshot"
    _database(snapshot, [
        ("aud-race", "recorded", 0, None, _digest(payload), len(payload), "webm"),
    ])

    completed = _run("verify-audio", snapshot)

    assert completed.returncode == 2
    assert "code=audio_authoritative_file_missing" in completed.stderr


@pytest.mark.parametrize("name", [
    ".aud-live.random.pending",
    ".aud-live.upload.lock",
])
def test_pending_and_lock_files_are_rejected(name, tmp_path):
    snapshot = tmp_path / "snapshot"
    audio = snapshot / "audio"
    audio.mkdir(parents=True)
    (audio / name).write_bytes(b"not-authoritative")
    _database(snapshot)

    completed = _run("verify-audio", snapshot)

    assert completed.returncode == 2
    assert "code=audio_transient_file" in completed.stderr


def test_orphan_audio_file_is_rejected_without_identifier_leak(tmp_path):
    snapshot = tmp_path / "snapshot"
    audio = snapshot / "audio"
    audio.mkdir(parents=True)
    synthetic_secret_id = "SYNTHETIC-PATIENT-SECRET"
    (audio / f"{synthetic_secret_id}.webm").write_bytes(b"orphan")
    _database(snapshot)

    completed = _run("verify-audio", snapshot)

    assert completed.returncode == 2
    assert "code=audio_orphan_file" in completed.stderr
    assert synthetic_secret_id not in completed.stdout
    assert synthetic_secret_id not in completed.stderr


@pytest.mark.parametrize("withdrawn,status,withdrawal_status", [
    (0, "deleted", None),
    (1, "recorded", None),
    (0, "recorded", "isolated_by_subject_withdrawal"),
])
def test_deleted_or_withdrawn_rows_must_not_retain_bytes(
        withdrawn, status, withdrawal_status, tmp_path):
    snapshot = tmp_path / "snapshot"
    audio = snapshot / "audio"
    audio.mkdir(parents=True)
    payload = b"governed-out"
    (audio / "aud-governed.webm").write_bytes(payload)
    _database(snapshot, [
        ("aud-governed", status, withdrawn, withdrawal_status,
         _digest(payload), len(payload), "webm"),
    ])

    completed = _run("verify-audio", snapshot)

    assert completed.returncode == 2
    assert "code=audio_governed_bytes_present" in completed.stderr


def test_wrong_authoritative_hash_is_rejected(tmp_path):
    snapshot = tmp_path / "snapshot"
    audio = snapshot / "audio"
    audio.mkdir(parents=True)
    payload = b"actual"
    (audio / "aud-mismatch.webm").write_bytes(payload)
    _database(snapshot, [
        ("aud-mismatch", "recorded", 0, None, _digest(b"expected"), len(payload),
         "webm"),
    ])

    completed = _run("verify-audio", snapshot)

    assert completed.returncode == 2
    assert "code=audio_authoritative_file_mismatch" in completed.stderr


def test_fractional_byte_count_is_not_truncated(tmp_path):
    snapshot = tmp_path / "snapshot"
    audio = snapshot / "audio"
    audio.mkdir(parents=True)
    payload = b"x"
    (audio / "aud-fraction.webm").write_bytes(payload)
    _database(snapshot, [
        ("aud-fraction", "recorded", 0, None, _digest(payload), 1.5, "webm"),
    ])

    completed = _run("verify-audio", snapshot)

    assert completed.returncode == 2
    assert "code=audio_receipt_invalid" in completed.stderr


def test_audio_larger_than_upload_contract_is_rejected_before_hashing(tmp_path):
    snapshot = tmp_path / "snapshot"
    audio = snapshot / "audio"
    audio.mkdir(parents=True)
    oversized = audio / "aud-oversized.webm"
    with oversized.open("wb") as handle:
        handle.truncate(_GUARD_MODULE.MAX_AUDIO_BLOB_BYTES + 1)
    _database(snapshot)

    completed = _run("verify-audio", snapshot)

    assert completed.returncode == 2
    assert "code=audio_file_size_invalid" in completed.stderr


@pytest.mark.parametrize(("row", "code"), [
    (("bad/id", "recorded", 0, None, None, None, "webm"),
     "audio_database_id_invalid"),
    (("aud-status", "invented", 0, None, None, None, "webm"),
     "audio_database_status_invalid"),
    (("aud-withdrawn", "recorded", 2, None, None, None, "webm"),
     "audio_database_withdrawn_invalid"),
])
def test_database_identity_and_state_are_closed_sets(row, code, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot, [row])

    completed = _run("verify-audio", snapshot)

    assert completed.returncode == 2
    assert f"code={code}" in completed.stderr


def test_final_suffix_must_match_database_audio_format(tmp_path):
    snapshot = tmp_path / "snapshot"
    audio = snapshot / "audio"
    audio.mkdir(parents=True)
    payload = b"same-bytes-wrong-extension"
    (audio / "aud-format.mp3").write_bytes(payload)
    _database(snapshot, [
        ("aud-format", "recorded", 0, None, _digest(payload), len(payload), "webm"),
    ])

    completed = _run("verify-audio", snapshot)

    assert completed.returncode == 2
    assert "code=audio_format_mismatch" in completed.stderr


def test_manifest_rejects_ambiguous_normalized_path(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database(snapshot)
    checksum = _digest((snapshot / "app.db").read_bytes())
    (snapshot / "MANIFEST.sha256").write_text(
        f"{checksum}  ./nested//../app.db\n",
        encoding="utf-8",
    )

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=manifest_path_invalid" in completed.stderr


def test_sparse_oversized_manifest_is_rejected_before_reading(tmp_path):
    manifest = tmp_path / "MANIFEST.sha256"
    with manifest.open("wb") as handle:
        handle.truncate(_GUARD_MODULE.MAX_MANIFEST_BYTES + 1)

    with pytest.raises(_GUARD_MODULE.SnapshotError) as caught:
        _GUARD_MODULE._manifest_entries(manifest)

    assert caught.value.code == "manifest_too_large"


def test_manifest_record_length_is_bounded(tmp_path):
    manifest = tmp_path / "MANIFEST.sha256"
    manifest.write_bytes(b"0" * (_GUARD_MODULE.MAX_MANIFEST_LINE_BYTES + 1))

    with pytest.raises(_GUARD_MODULE.SnapshotError) as caught:
        _GUARD_MODULE._manifest_entries(manifest)

    assert caught.value.code == "manifest_record_too_long"


def test_manifest_entry_count_is_bounded(monkeypatch, tmp_path):
    manifest = tmp_path / "MANIFEST.sha256"
    manifest.write_text(
        f"{'0' * 64}  ./one\n{'1' * 64}  ./two\n",
        encoding="ascii",
    )
    monkeypatch.setattr(_GUARD_MODULE, "MAX_MANIFEST_ENTRIES", 1)

    with pytest.raises(_GUARD_MODULE.SnapshotError) as caught:
        _GUARD_MODULE._manifest_entries(manifest)

    assert caught.value.code == "manifest_entry_limit_exceeded"


def test_manifest_files_command_emits_only_bounded_normalized_allowlist(tmp_path):
    manifest = tmp_path / "MANIFEST.sha256"
    manifest.write_text(
        f"{'1' * 64}  ./audio/synthetic.webm\n"
        f"{'0' * 64}  ./app.db\n",
        encoding="ascii",
    )
    manifest.chmod(0o600)

    completed = subprocess.run(
        [sys.executable, "-I", str(GUARD), "manifest-files", str(manifest)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "./MANIFEST.sha256",
        "./app.db",
        "./audio/synthetic.webm",
    ]


def test_snapshot_file_walk_is_bounded(monkeypatch, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir(mode=0o700)
    for name in ("one", "two"):
        path = snapshot / name
        path.write_bytes(name.encode("ascii"))
        path.chmod(0o600)
    monkeypatch.setattr(_GUARD_MODULE, "MAX_SNAPSHOT_FILES", 1)

    with pytest.raises(_GUARD_MODULE.SnapshotError) as caught:
        _GUARD_MODULE._regular_files_and_directories(snapshot)

    assert caught.value.code == "snapshot_file_limit_exceeded"


def test_snapshot_directory_walk_is_bounded(monkeypatch, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir(mode=0o700)
    (snapshot / "nested").mkdir(mode=0o700)
    monkeypatch.setattr(_GUARD_MODULE, "MAX_SNAPSHOT_DIRECTORIES", 1)

    with pytest.raises(_GUARD_MODULE.SnapshotError) as caught:
        _GUARD_MODULE._regular_files_and_directories(snapshot)

    assert caught.value.code == "snapshot_directory_limit_exceeded"


def test_old_audio_schema_with_bytes_fails_closed(tmp_path):
    snapshot = tmp_path / "snapshot"
    audio = snapshot / "audio"
    audio.mkdir(parents=True)
    (audio / "aud-old.webm").write_bytes(b"legacy")
    _database(snapshot)
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        connection.execute("ALTER TABLE audioassetrow DROP COLUMN byte_count")
        connection.commit()
    finally:
        connection.close()

    completed = _run("verify-audio", snapshot)

    assert completed.returncode == 2
    assert "code=recovery_schema_incomplete" in completed.stderr


def test_compare_checks_complete_tree_not_only_manifest(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    _database(left)
    (right / "app.db").write_bytes((left / "app.db").read_bytes())
    _manifest(left)
    _manifest(right)
    assert _run("compare", left, right).returncode == 0

    (right / "empty-extra-directory").mkdir()
    completed = _run("compare", left, right)

    assert completed.returncode == 3
    assert completed.stdout.strip() == "DIFFERENT"


def test_permissive_snapshot_permissions_are_rejected(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir(mode=0o755)
    _database(snapshot)
    _manifest(snapshot)
    os.chmod(snapshot, 0o755)
    os.chmod(snapshot / "app.db", 0o644)
    os.chmod(snapshot / "MANIFEST.sha256", 0o644)

    completed = _run("verify", snapshot, harden=False)

    assert completed.returncode == 2
    assert "code=snapshot_permissions_invalid" in completed.stderr


def test_publish_is_atomic_across_sibling_staging_directory(tmp_path):
    incoming = tmp_path / "incoming"
    daily = tmp_path / "daily"
    incoming.mkdir()
    daily.mkdir()
    source = incoming / "snapshot"
    source.mkdir()
    _database(source)
    _manifest(source)
    destination = daily / "20260719-010101"

    completed = _run("publish", source, destination)

    assert completed.returncode == 0, completed.stderr
    assert destination.is_dir()
    assert not source.exists()


def test_publish_parent_fsync_failure_rolls_back_to_staging(monkeypatch, tmp_path):
    incoming = tmp_path / "incoming"
    daily = tmp_path / "daily"
    incoming.mkdir()
    daily.mkdir()
    source = incoming / "snapshot"
    source.mkdir()
    _database(source)
    _manifest(source)
    _harden(source)
    destination = daily / "20260719-010101"
    original = _GUARD_MODULE._fsync_directory
    failed_once = False

    def fail_destination_parent_once(path):
        nonlocal failed_once
        if Path(path) == daily and destination.exists() and not failed_once:
            failed_once = True
            raise OSError("synthetic fsync failure")
        original(Path(path))

    monkeypatch.setattr(
        _GUARD_MODULE, "_fsync_directory", fail_destination_parent_once
    )

    with pytest.raises(_GUARD_MODULE.SnapshotError) as caught:
        _GUARD_MODULE.publish_snapshot(source, destination)

    assert caught.value.code == "publish_parent_fsync_failed"
    assert source.is_dir()
    assert not destination.exists()


# --------------------------------------------------------------------------
# Current head: c5a8f2d91e40 (caregiver help dispositions) recovery contract,
# plus the patient-pause/assessment/disposal/profile layers it builds on.
# --------------------------------------------------------------------------


CURRENT_HEAD = "c5a8f2d91e40"
DISPOSAL_HEAD = "f7c2e8a4d105"
PROFILE_HEAD = "e4a7c1d9b206"
PRE_PROFILE_HEAD = "d3f8b5c1a704"
PROFILE_COLUMNS = (
    "autopilot_profile_version_id",
    "autopilot_profile_definition_digest",
)
PROFILE_CHECKS = (
    ("visitplan", "ck_visit_plan_autopilot_profile_binding_complete"),
    ("visitplan", "ck_visit_plan_autopilot_profile_simulation_only"),
    ("session", "ck_session_autopilot_profile_binding_complete"),
    ("session", "ck_session_autopilot_profile_simulation_only"),
    ("session", "ck_session_autopilot_profile_requires_visit_plan"),
)


def _alembic_config(db_path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def _database_at(snapshot: Path, revision: str) -> Path:
    db_path = snapshot / "app.db"
    command.upgrade(_alembic_config(db_path), revision)
    return db_path


def _rewrite_stored_table_sql(db_path: Path, table: str, transform) -> None:
    """Forge schema drift the way a tampered restore would present it."""
    connection = sqlite3.connect(db_path)
    try:
        original = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()[0]
        replacement = transform(original)
        assert replacement != original, "drift fixture did not change the DDL"
        version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
            (replacement, table),
        )
        connection.execute(f"PRAGMA schema_version={int(version) + 1}")
        connection.execute("PRAGMA writable_schema=OFF")
        connection.commit()
    finally:
        connection.close()


def _drop_check_clause(sql: str, name: str) -> str:
    marker = f"CONSTRAINT {name} CHECK ("
    start = sql.index(marker)
    index = start + len(marker)
    depth = 1
    while depth:
        if sql[index] == "(":
            depth += 1
        elif sql[index] == ")":
            depth -= 1
        index += 1
    prefix = sql[:start].rstrip()
    assert prefix.endswith(","), "unexpected constraint position"
    return prefix[:-1] + sql[index:]


def _drop_profile_column(
        db_path: Path, table: str, column: str) -> tuple[str, ...]:
    """Remove only a profile column and the CHECKs that depend on it."""
    dependent = tuple(
        name for owner, name in PROFILE_CHECKS if owner == table
    )
    expected = (
        (
            "ck_visit_plan_autopilot_profile_binding_complete",
            "ck_visit_plan_autopilot_profile_simulation_only",
        )
        if table == "visitplan"
        else (
            "ck_session_autopilot_profile_binding_complete",
            "ck_session_autopilot_profile_simulation_only",
            "ck_session_autopilot_profile_requires_visit_plan",
        )
    )
    assert dependent == expected

    def without_dependent_checks(sql: str) -> str:
        for name in dependent:
            sql = _drop_check_clause(sql, name)
        return sql

    _rewrite_stored_table_sql(db_path, table, without_dependent_checks)
    connection = sqlite3.connect(db_path)
    try:
        quoted_table = _GUARD_MODULE._quote_identifier(table)
        quoted_column = _GUARD_MODULE._quote_identifier(column)
        connection.execute(
            f"ALTER TABLE {quoted_table} DROP COLUMN {quoted_column}"
        )
        connection.commit()
    finally:
        connection.close()
    return dependent


def test_recovery_contract_pins_the_current_head_only():
    assert _GUARD_MODULE.SUPPORTED_ALEMBIC_HEADS == frozenset({CURRENT_HEAD})
    assert DISPOSAL_HEAD not in _GUARD_MODULE.SUPPORTED_ALEMBIC_HEADS
    assert PROFILE_HEAD not in _GUARD_MODULE.SUPPORTED_ALEMBIC_HEADS
    assert PRE_PROFILE_HEAD not in _GUARD_MODULE.SUPPORTED_ALEMBIC_HEADS


def test_recovery_fingerprint_literal_matches_a_fresh_current_head(tmp_path):
    """The constant is a hardcoded literal, recomputed here from a fresh head."""
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    db_path = _database_at(snapshot, "head")

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == CURRENT_HEAD
        computed = _GUARD_MODULE._schema_contract_fingerprint(connection)
    finally:
        connection.close()

    assert computed == _GUARD_MODULE.CURRENT_RECOVERY_SCHEMA_SHA256


@pytest.mark.parametrize("stale_head", [PRE_PROFILE_HEAD, PROFILE_HEAD, DISPOSAL_HEAD])
def test_real_stale_head_snapshot_is_rejected_as_unsupported_revision(
        tmp_path, stale_head):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database_at(snapshot, stale_head)
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=alembic_revision_unsupported" in completed.stderr


def test_stale_schema_with_forged_head_revision_is_rejected(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    db_path = _database_at(snapshot, DISPOSAL_HEAD)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "UPDATE alembic_version SET version_num=?", (CURRENT_HEAD,))
        connection.commit()
    finally:
        connection.close()
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=recovery_schema_incomplete" in completed.stderr


@pytest.mark.parametrize("table", ["visitplan", "session"])
@pytest.mark.parametrize("column", PROFILE_COLUMNS)
def test_profile_column_removal_is_rejected(tmp_path, table, column):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    db_path = _database_at(snapshot, "head")
    connection = sqlite3.connect(db_path)
    try:
        before = _GUARD_MODULE._schema_contract(connection)
    finally:
        connection.close()

    dependent = _drop_profile_column(db_path, table, column)

    connection = sqlite3.connect(db_path)
    try:
        after = _GUARD_MODULE._schema_contract(connection)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    finally:
        connection.close()

    for other_table in set(before) - {table}:
        assert after[other_table] == before[other_table]

    before_table = before[table]
    after_table = after[table]
    assert [row[1:] for row in after_table["columns"]] == [
        row[1:] for row in before_table["columns"] if row[1] != column
    ]
    assert after_table["checks"] == [
        row for row in before_table["checks"] if row[0] not in dependent
    ]
    assert after_table["foreign_keys"] == before_table["foreign_keys"]
    assert after_table["indexes"] == before_table["indexes"]

    old_clauses, old_suffix = before_table["table_sql"]
    removed = {
        clause for clause in old_clauses
        if clause.startswith(f"{column} ")
        or any(
            clause.startswith(f"CONSTRAINT {name} CHECK (")
            for name in dependent
        )
    }
    assert len(removed) == 1 + len(dependent)
    assert after_table["table_sql"] == (
        tuple(clause for clause in old_clauses if clause not in removed),
        old_suffix,
    )
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert completed.stderr.strip() == "REJECTED code=recovery_schema_incomplete"


@pytest.mark.parametrize(
    "table,constraint", PROFILE_CHECKS,
    ids=[name for _table, name in PROFILE_CHECKS],
)
def test_named_profile_check_removal_is_rejected(tmp_path, table, constraint):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    db_path = _database_at(snapshot, "head")
    _rewrite_stored_table_sql(
        db_path, table, lambda sql: _drop_check_clause(sql, constraint))
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 2
    assert "code=recovery_schema_incomplete" in completed.stderr


def test_wrong_recovery_fingerprint_is_rejected(monkeypatch, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    _database_at(snapshot, "head")
    _manifest(snapshot)
    _harden(snapshot)

    wrong = "0" * 64
    assert wrong != _GUARD_MODULE.CURRENT_RECOVERY_SCHEMA_SHA256
    monkeypatch.setattr(
        _GUARD_MODULE,
        "CURRENT_RECOVERY_SCHEMA_SHA256",
        wrong,
    )

    with pytest.raises(_GUARD_MODULE.SnapshotError) as caught:
        _GUARD_MODULE.verify_snapshot(snapshot)

    assert caught.value.code == "recovery_schema_incomplete"


def test_fresh_current_head_snapshot_still_passes_end_to_end(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    audio = snapshot / "audio"
    audio.mkdir(parents=True)
    payload = b"profile-head-authoritative-audio"
    (audio / "aud-profile.webm").write_bytes(payload)
    _database(snapshot, [
        ("aud-profile", "recorded", 0, None,
         _digest(payload), len(payload), "webm"),
    ])
    _manifest(snapshot)

    completed = _run("verify", snapshot)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "OK"
    connection = sqlite3.connect(snapshot / "app.db")
    try:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == CURRENT_HEAD
    finally:
        connection.close()
