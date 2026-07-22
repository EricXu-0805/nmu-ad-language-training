#!/usr/bin/env python3
"""Offline, fail-closed quarantine for abandoned pre-intent export staging.

This is deliberately not a general export repair tool.  It accepts only the
one narrow crash window where the current SQLite ledger still has an expired
staging lease but no publication intent, artifact receipt, or exported-audio
binding.  It moves the already-written private files to an external quarantine
bundle and then clears only the lease fields; the immutable ExportBatch remains.

Diagnostics are stable reason codes and never include filesystem paths, raw
audio identifiers, patient identifiers, free text, or key material.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
from types import ModuleType
from typing import Iterable, Mapping
from urllib.parse import quote


_SAFE_BATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_RAW_AUDIO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_SAFE_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_CONTROLLED_AUDIO_NAME = re.compile(
    r"^(AUDIO-v1-[A-Za-z0-9][A-Za-z0-9._-]{0,31}-[0-9a-f]{20})"
    r"\.(webm|mp3|wav|m4a|ogg)$"
)
_AUDIO_FORMATS = frozenset({"webm", "mp3", "wav", "m4a", "ogg"})
_CLASSIFICATIONS = frozenset({"research", "simulation"})
_SHEETS = frozenset({
    "session",
    "turns",
    "attempts",
    "interactions",
    "item_scores",
    "scales",
    "legacy_unverified_scales",
    "abnormal",
    "audio_manifest",
})
_ANALYSIS_NAMES = frozenset(
    {f"{name}.csv" for name in _SHEETS} | {".staging-receipt.json"}
)
_INTENT_NAME = "quarantine-intent.json"
_RECEIPT_NAME = "reconciliation-receipt.json"
_UNCERTAIN_NAME = "database-commit-uncertain.json"
_BUNDLE_NAME = re.compile(r"^preintent-reconciliation-[0-9a-f]{32}$")
_INTENT_SCHEMA = "nmu-export-staging-quarantine-intent.v1"
_RECEIPT_SCHEMA = "nmu-export-staging-reconciliation.v1"
_MAX_RECEIPT_BYTES = 4096
_MAX_CSV_BYTES = 64 * 1024 * 1024
_MAX_AUDIO_BYTES = 64 * 1024 * 1024
_MAX_AUDIO_FILES = 10_000
_MAX_CONTROLLED_TOTAL_BYTES = 64 * 1024 * 1024 * 1024
_MAX_AUDIO_MANIFEST_ROWS = 100_000
_MAX_QUARANTINE_BUNDLES = 10_000
_CHUNK_BYTES = 1024 * 1024


class ReconciliationError(RuntimeError):
    """A non-identifying, stable rejection reason."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TreeSummary:
    sha256: str
    file_count: int
    byte_count: int

    def public(self) -> dict[str, int | str]:
        return {
            "byte_count": self.byte_count,
            "file_count": self.file_count,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ReconciliationResult:
    status: str
    batch_id_sha256: str
    bundle_name: str | None

    def public(self) -> dict[str, str | None]:
        return {
            "batch_id_sha256": self.batch_id_sha256,
            "bundle_name": self.bundle_name,
            "status": self.status,
        }


@dataclass(frozen=True)
class StorageLayout:
    exports_root: Path
    analysis_parent: Path
    controlled_root: Path | None
    controlled_parent: Path


@dataclass(frozen=True)
class DeidentificationConfig:
    key: bytes = field(repr=False)
    key_id: str


def _deidentification_config() -> DeidentificationConfig:
    raw_key = os.environ.get("DEIDENTIFICATION_KEY")
    key_id = (os.environ.get("DEIDENTIFICATION_KEY_ID") or "").strip()
    if (
        raw_key is None
        or len(raw_key.encode("utf-8")) < 32
        or not _SAFE_KEY_ID.fullmatch(key_id)
    ):
        raise ReconciliationError("deidentification_key_unavailable")
    return DeidentificationConfig(raw_key.encode("utf-8"), key_id)


def _audio_code(raw_audio_id: str, config: DeidentificationConfig) -> str:
    message = (
        b"nmu-audio-pseudonym:v1\x00" + raw_audio_id.encode("utf-8")
    )
    digest = hmac.new(config.key, message, hashlib.sha256).hexdigest()
    return f"AUDIO-v1-{config.key_id}-{digest[:20]}"


def _scope_hash(session_id: str, config: DeidentificationConfig) -> str:
    return hmac.new(
        config.key,
        b"nmu-export-scope\x00" + session_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _backup_guard() -> ModuleType:
    """Load the current recovery schema contract from the sibling validator."""

    path = Path(__file__).resolve().with_name("verify_backup_snapshot.py")
    spec = importlib.util.spec_from_file_location("_nmu_backup_guard", path)
    if spec is None or spec.loader is None:
        raise ReconciliationError("schema_guard_unavailable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ReconciliationError("schema_guard_unavailable") from exc
    return module


def _normalize_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_regular_file(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ReconciliationError("staging_fsync_failed")
        os.fsync(descriptor)
    except ReconciliationError:
        raise
    except OSError as exc:
        raise ReconciliationError("staging_fsync_failed") from exc
    finally:
        try:
            os.close(descriptor)
        except UnboundLocalError:
            pass


def _durably_sync_tree(root: Path) -> None:
    """Fsync every private regular file and directory before quarantine."""

    directories: list[Path] = []
    file_count = 0
    try:
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            _secure_directory(current_path, "staging_fsync_failed")
            directories.append(current_path)
            if len(directories) > 8:
                raise ReconciliationError("staging_fsync_failed")
            for name in dirnames:
                _secure_directory(
                    current_path / name,
                    "staging_fsync_failed",
                )
            for name in filenames:
                file_count += 1
                if file_count > _MAX_AUDIO_FILES + len(_ANALYSIS_NAMES):
                    raise ReconciliationError("staging_fsync_failed")
                _fsync_regular_file(current_path / name)
        for directory in sorted(
            directories,
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            _fsync_directory(directory)
    except ReconciliationError:
        raise
    except OSError as exc:
        raise ReconciliationError("staging_fsync_failed") from exc


def _atomic_write_json(path: Path, value: Mapping[str, object]) -> None:
    parent = path.parent
    if path.exists() or path.is_symlink():
        raise ReconciliationError("quarantine_evidence_collision")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise ReconciliationError("quarantine_evidence_collision") from exc
        temporary.unlink()
        _fsync_directory(parent)
    except ReconciliationError:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise ReconciliationError("quarantine_evidence_write_failed") from exc


def _read_canonical_json(path: Path, *, maximum: int) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReconciliationError("quarantine_evidence_invalid") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size <= 0
            or info.st_size > maximum
        ):
            raise ReconciliationError("quarantine_evidence_invalid")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum:
            raise ReconciliationError("quarantine_evidence_invalid")
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ReconciliationError("quarantine_evidence_invalid") from exc
    finally:
        os.close(descriptor)
    if (
        not isinstance(value, dict)
        or payload != _canonical_json_bytes(value)
    ):
        raise ReconciliationError("quarantine_evidence_invalid")
    return value


def _secure_directory(path: Path, code: str) -> Path:
    lexical = Path(os.path.abspath(path))
    current = Path(lexical.anchor)
    try:
        for part in lexical.parts[1:]:
            current /= part
            if stat.S_ISLNK(current.lstat().st_mode):
                raise ReconciliationError(code)
    except ReconciliationError:
        raise
    except OSError as exc:
        raise ReconciliationError(code) from exc
    try:
        info = lexical.lstat()
    except OSError as exc:
        raise ReconciliationError(code) from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ReconciliationError(code)
    try:
        return lexical.resolve(strict=True)
    except OSError as exc:
        raise ReconciliationError(code) from exc


def _optional_secure_directory(path: Path, code: str) -> Path | None:
    if not path.exists() and not path.is_symlink():
        return None
    return _secure_directory(path, code)


def _secure_regular_file(
    path: Path,
    *,
    maximum: int,
    allow_empty: bool,
    code: str,
) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReconciliationError(code) from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or info.st_size > maximum
        or (not allow_empty and info.st_size <= 0)
    ):
        raise ReconciliationError(code)
    return info


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            os.close(descriptor)
            raise ReconciliationError("staging_file_unreadable")
        with os.fdopen(descriptor, "rb") as handle:
            while chunk := handle.read(_CHUNK_BYTES):
                digest.update(chunk)
                total += len(chunk)
    except ReconciliationError:
        raise
    except OSError as exc:
        raise ReconciliationError("staging_file_unreadable") from exc
    return digest.hexdigest(), total


def _tree_summary(files: Iterable[tuple[str, Path]]) -> TreeSummary:
    digest = hashlib.sha256()
    file_count = 0
    byte_count = 0
    for relative, path in sorted(files):
        file_sha, size = _sha256(path)
        digest.update(relative.encode("ascii"))
        digest.update(b"\x00")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\x00")
        digest.update(file_sha.encode("ascii"))
        digest.update(b"\n")
        file_count += 1
        byte_count += size
    return TreeSummary(digest.hexdigest(), file_count, byte_count)


def _verify_current_schema(
    connection: sqlite3.Connection,
    guard: ModuleType,
) -> None:
    try:
        integrity = tuple(
            tuple(row) for row in connection.execute("PRAGMA integrity_check")
        )
        if integrity != (("ok",),):
            raise ReconciliationError("sqlite_integrity_check_failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ReconciliationError("sqlite_foreign_key_check_failed")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            if not str(row[0]).startswith("sqlite_")
        }
        revisions = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT version_num FROM alembic_version"
            )
        )
        if (
            tables != guard.RECOVERY_SCHEMA_TABLES
            or len(revisions) != 1
            or revisions[0] not in guard.SUPPORTED_ALEMBIC_HEADS
            or connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type IN ('view','trigger') LIMIT 1"
            ).fetchone()
            is not None
            or guard._schema_contract_fingerprint(connection)
            != guard.CURRENT_RECOVERY_SCHEMA_SHA256
        ):
            raise ReconciliationError("current_schema_required")
    except ReconciliationError:
        raise
    except sqlite3.Error as exc:
        raise ReconciliationError("current_schema_required") from exc


def _parse_lease(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ReconciliationError("staging_lease_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReconciliationError("staging_lease_invalid") from exc
    if parsed.tzinfo is not None:
        raise ReconciliationError("staging_lease_invalid")
    return parsed


def _load_batch(
    connection: sqlite3.Connection,
    *,
    batch_id: str,
    now: datetime,
    allow_released: bool = False,
) -> sqlite3.Row:
    try:
        row = connection.execute(
            "SELECT batch_id,schema_version,export_scope_hash,status,"
            "data_classification,"
            "deidentified,result_metadata_json,manifest_sha256,"
            "publication_manifest_json,staging_owner_hash,"
            "staging_lease_expires_at,artifacts_ready_at,published_at "
            "FROM exportbatch WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise ReconciliationError("export_batch_missing")
        owner = row["staging_owner_hash"]
        lease_raw = row["staging_lease_expires_at"]
        if (
            row["schema_version"] != "export-batch.v1"
            or not isinstance(row["export_scope_hash"], str)
            or not _HEX_64.fullmatch(row["export_scope_hash"])
            or row["status"] != "staging"
            or row["data_classification"] not in _CLASSIFICATIONS
            or row["deidentified"] != 1
            or row["result_metadata_json"] != "{}"
            or row["manifest_sha256"] is not None
            or row["publication_manifest_json"] is not None
            or row["artifacts_ready_at"] is not None
            or row["published_at"] is not None
        ):
            raise ReconciliationError("not_preintent_staging")
        released = owner is None and lease_raw is None
        if released:
            if not allow_released:
                raise ReconciliationError("staging_lease_invalid")
        else:
            if (
                not isinstance(owner, str)
                or not _HEX_64.fullmatch(owner)
                or lease_raw is None
            ):
                raise ReconciliationError("staging_lease_invalid")
            if _parse_lease(lease_raw) > now:
                raise ReconciliationError("staging_lease_active")
        if connection.execute(
            "SELECT 1 FROM exportartifact WHERE batch_id=? LIMIT 1",
            (batch_id,),
        ).fetchone() is not None:
            raise ReconciliationError("export_artifact_binding_present")
        if connection.execute(
            "SELECT 1 FROM audioassetrow WHERE export_batch_id=? LIMIT 1",
            (batch_id,),
        ).fetchone() is not None:
            raise ReconciliationError("export_audio_binding_present")
        return row
    except ReconciliationError:
        raise
    except sqlite3.Error as exc:
        raise ReconciliationError("export_ledger_unreadable") from exc


def _validate_staging_receipt(
    path: Path,
    *,
    batch_id: str,
    owner_hash: str,
) -> None:
    value = _read_canonical_json(path, maximum=_MAX_RECEIPT_BYTES)
    if value != {
        "batch_id": batch_id,
        "staging_owner_hash": owner_hash,
    }:
        raise ReconciliationError("staging_receipt_mismatch")


def _analysis_files(
    batch_dir: Path,
    *,
    batch_id: str,
    owner_hash: str,
) -> tuple[TreeSummary, list[dict[str, str]]]:
    resolved = _secure_directory(batch_dir, "analysis_staging_invalid")
    try:
        entries = tuple(os.scandir(resolved))
    except OSError as exc:
        raise ReconciliationError("analysis_staging_invalid") from exc
    if {entry.name for entry in entries} != _ANALYSIS_NAMES:
        raise ReconciliationError("analysis_layout_invalid")
    files: list[tuple[str, Path]] = []
    for name in sorted(_ANALYSIS_NAMES):
        path = resolved / name
        maximum = _MAX_RECEIPT_BYTES if name.startswith(".") else _MAX_CSV_BYTES
        _secure_regular_file(
            path,
            maximum=maximum,
            allow_empty=False,
            code="analysis_file_invalid",
        )
        files.append((name, path))
    _validate_staging_receipt(
        resolved / ".staging-receipt.json",
        batch_id=batch_id,
        owner_hash=owner_hash,
    )
    rows = _read_audio_manifest(resolved / "audio_manifest.csv")
    return _tree_summary(files), rows


def _read_audio_manifest(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, strict=True)
            if reader.fieldnames is None:
                return []
            if len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise ReconciliationError("audio_manifest_invalid")
            rows: list[dict[str, str]] = []
            for row in reader:
                if len(rows) >= _MAX_AUDIO_MANIFEST_ROWS:
                    raise ReconciliationError("audio_manifest_invalid")
                if None in row or any(value is None for value in row.values()):
                    raise ReconciliationError("audio_manifest_invalid")
                rows.append({str(key): str(value) for key, value in row.items()})
            return rows
    except ReconciliationError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReconciliationError("audio_manifest_invalid") from exc


def _controlled_files(batch_dir: Path) -> tuple[TreeSummary, dict[str, Path]]:
    resolved = _secure_directory(batch_dir, "controlled_staging_invalid")
    try:
        batch_entries = tuple(os.scandir(resolved))
    except OSError as exc:
        raise ReconciliationError("controlled_staging_invalid") from exc
    if len(batch_entries) != 1 or batch_entries[0].name != "audio":
        raise ReconciliationError("controlled_layout_invalid")
    audio_dir = _secure_directory(resolved / "audio", "controlled_layout_invalid")
    try:
        entries = tuple(os.scandir(audio_dir))
    except OSError as exc:
        raise ReconciliationError("controlled_layout_invalid") from exc
    if not entries or len(entries) > _MAX_AUDIO_FILES:
        raise ReconciliationError("controlled_layout_invalid")
    files: list[tuple[str, Path]] = []
    codes: dict[str, Path] = {}
    for entry in sorted(entries, key=lambda item: item.name):
        match = _CONTROLLED_AUDIO_NAME.fullmatch(entry.name)
        if match is None:
            raise ReconciliationError("controlled_audio_name_invalid")
        path = audio_dir / entry.name
        _secure_regular_file(
            path,
            maximum=_MAX_AUDIO_BYTES,
            allow_empty=False,
            code="controlled_audio_file_invalid",
        )
        code = match.group(1)
        if code in codes:
            raise ReconciliationError("controlled_audio_duplicate")
        codes[code] = path
        files.append((f"audio/{entry.name}", path))
    summary = _tree_summary(files)
    if summary.byte_count > _MAX_CONTROLLED_TOTAL_BYTES:
        raise ReconciliationError("controlled_audio_size_limit_exceeded")
    return summary, codes


def _validate_raw_audio_source(
    data_root: Path,
    *,
    raw_audio_id: str,
    audio_format: str,
    checksum: str,
    byte_count: int,
) -> None:
    if (
        not _SAFE_RAW_AUDIO_ID.fullmatch(raw_audio_id)
        or audio_format not in _AUDIO_FORMATS
    ):
        raise ReconciliationError("raw_audio_receipt_invalid")
    audio_root = _optional_secure_directory(
        data_root / "audio",
        "audio_root_invalid",
    )
    if audio_root is None:
        raise ReconciliationError("raw_audio_receipt_invalid")
    try:
        candidates = tuple(audio_root.glob(f"{raw_audio_id}.*"))
    except OSError as exc:
        raise ReconciliationError("raw_audio_receipt_invalid") from exc
    expected = audio_root / f"{raw_audio_id}.{audio_format}"
    if candidates != (expected,):
        raise ReconciliationError("raw_audio_receipt_invalid")
    info = _secure_regular_file(
        expected,
        maximum=_MAX_AUDIO_BYTES,
        allow_empty=False,
        code="raw_audio_receipt_invalid",
    )
    actual_sha, actual_bytes = _sha256(expected)
    if (
        info.st_size != byte_count
        or actual_bytes != byte_count
        or actual_sha != checksum
    ):
        raise ReconciliationError("raw_audio_receipt_invalid")


def _require_raw_audio_absent(data_root: Path, raw_audio_id: str) -> None:
    if not _SAFE_RAW_AUDIO_ID.fullmatch(raw_audio_id):
        raise ReconciliationError("raw_audio_receipt_invalid")
    audio_root = _optional_secure_directory(
        data_root / "audio",
        "audio_root_invalid",
    )
    if audio_root is None:
        return
    try:
        candidates = tuple(audio_root.glob(f"{raw_audio_id}.*"))
    except OSError as exc:
        raise ReconciliationError("raw_audio_receipt_invalid") from exc
    if candidates:
        raise ReconciliationError("withdrawn_audio_cleanup_required")


def _validate_controlled_closure(
    connection: sqlite3.Connection,
    *,
    data_root: Path,
    classification: str,
    export_scope_hash: str,
    manifest_rows: list[dict[str, str]],
    controlled_dir: Path | None,
) -> TreeSummary | None:
    expected_rows: dict[str, dict[str, str]] = {}
    for row in manifest_rows:
        marker = row.get("controlled_audio_exported")
        if marker not in {"True", "False"}:
            raise ReconciliationError("audio_manifest_invalid")
        if marker == "False":
            continue
        required = {
            "audio_code",
            "audio_format",
            "status",
            "is_simulation",
            "data_classification",
        }
        if not required <= set(row):
            raise ReconciliationError("audio_manifest_invalid")
        code = row["audio_code"]
        if code in expected_rows or not _CONTROLLED_AUDIO_NAME.fullmatch(
            f"{code}.{row['audio_format']}"
        ):
            raise ReconciliationError("audio_manifest_invalid")
        if (
            row["status"] != "exported"
            or row["audio_format"] not in _AUDIO_FORMATS
            or row["data_classification"] != classification
            or row["is_simulation"]
            != ("True" if classification == "simulation" else "False")
        ):
            raise ReconciliationError("audio_manifest_invalid")
        expected_rows[code] = row

    if controlled_dir is None:
        if expected_rows:
            raise ReconciliationError("controlled_audio_closure_invalid")
        return None

    summary, files = _controlled_files(controlled_dir)
    if set(files) != set(expected_rows):
        raise ReconciliationError("controlled_audio_closure_invalid")
    matched_raw_ids: set[str] = set()
    config = _deidentification_config()
    for code, path in sorted(files.items()):
        match = _CONTROLLED_AUDIO_NAME.fullmatch(path.name)
        if match is None:
            raise ReconciliationError("controlled_audio_name_invalid")
        audio_format = match.group(2)
        if expected_rows[code]["audio_format"] != audio_format:
            raise ReconciliationError("controlled_audio_closure_invalid")
        checksum, byte_count = _sha256(path)
        try:
            candidate_rows = tuple(connection.execute(
                "SELECT a.raw_audio_id,a.session_id,a.audio_format,a.checksum,"
                "a.byte_count,"
                "a.status,a.withdrawn,a.withdrawal_status,"
                "a.delete_gate_passed,p.withdrawal_status AS patient_withdrawal,"
                "p.consent_status,p.governance_revision,"
                "EXISTS(SELECT 1 FROM patientwithdrawalevent AS w "
                "WHERE w.patient_id=p.patient_id "
                "AND w.new_revision=p.governance_revision) "
                "AS has_withdrawal_receipt "
                "FROM audioassetrow AS a "
                "JOIN audiocapturereceipt AS r "
                "ON r.raw_audio_id=a.raw_audio_id "
                "JOIN session AS s ON s.session_id=a.session_id "
                "JOIN patient AS p ON p.patient_id=s.patient_id "
                "WHERE a.export_batch_id IS NULL "
                "AND s.data_classification=a.data_classification "
                "AND s.is_simulation=a.is_simulation "
                "AND a.data_classification=? AND a.is_simulation=? "
                "AND a.audio_format=? AND a.checksum=? AND a.byte_count=? "
                "AND a.session_id IS NOT NULL AND a.turn_key IS NOT NULL "
                "AND r.session_id=a.session_id AND r.turn_key=a.turn_key "
                "AND r.checksum=a.checksum AND r.byte_count=a.byte_count "
                "AND r.data_classification=a.data_classification "
                "AND r.is_simulation=a.is_simulation "
                "AND r.contains_direct_identifier="
                "a.contains_direct_identifier",
                (
                    classification,
                    1 if classification == "simulation" else 0,
                    audio_format,
                    checksum,
                    byte_count,
                ),
            ))
        except sqlite3.Error as exc:
            raise ReconciliationError("raw_audio_receipt_invalid") from exc
        candidates: list[sqlite3.Row] = []
        withdrawal_cleanup_pending = False
        for candidate in candidate_rows:
            withdrawal_status = str(candidate["withdrawal_status"] or "").strip()
            patient_withdrawal = str(candidate["patient_withdrawal"] or "").strip()
            consent_status = str(candidate["consent_status"] or "").strip()
            normal = (
                candidate["status"] == "recorded"
                and candidate["withdrawn"] == 0
                and not withdrawal_status
                and candidate["delete_gate_passed"] == 0
                and not patient_withdrawal
                and candidate["has_withdrawal_receipt"] == 0
            )
            withdrawn = (
                candidate["withdrawn"] == 1
                and withdrawal_status == "isolated_by_subject_withdrawal"
                and patient_withdrawal == "withdrawn"
                and consent_status == "withdrawn"
                and candidate["has_withdrawal_receipt"] == 1
                and candidate["status"] == "deleted"
                and candidate["delete_gate_passed"] == 1
            )
            raw_audio_id = str(candidate["raw_audio_id"])
            session_id = str(candidate["session_id"])
            identity_matches = (
                hmac.compare_digest(_audio_code(raw_audio_id, config), code)
                and hmac.compare_digest(
                    _scope_hash(session_id, config),
                    export_scope_hash,
                )
            )
            if identity_matches:
                if normal or withdrawn:
                    candidates.append(candidate)
                elif (
                    candidate["withdrawn"] == 1
                    and withdrawal_status
                    == "isolated_by_subject_withdrawal"
                    and patient_withdrawal == "withdrawn"
                    and consent_status == "withdrawn"
                    and candidate["has_withdrawal_receipt"] == 1
                    and candidate["status"] == "recorded"
                    and candidate["delete_gate_passed"] == 0
                ):
                    withdrawal_cleanup_pending = True
        if withdrawal_cleanup_pending:
            raise ReconciliationError("withdrawn_audio_cleanup_required")
        if len(candidates) != 1:
            raise ReconciliationError("raw_audio_receipt_invalid")
        raw_audio_id = str(candidates[0]["raw_audio_id"])
        if raw_audio_id in matched_raw_ids:
            raise ReconciliationError("raw_audio_receipt_invalid")
        matched_raw_ids.add(raw_audio_id)
        if candidates[0]["status"] == "deleted":
            _require_raw_audio_absent(data_root, raw_audio_id)
        else:
            _validate_raw_audio_source(
                data_root,
                raw_audio_id=raw_audio_id,
                audio_format=audio_format,
                checksum=checksum,
                byte_count=byte_count,
            )
    return summary


def _path_state(source: Path, destination: Path) -> str:
    source_present = source.exists() or source.is_symlink()
    destination_present = destination.exists() or destination.is_symlink()
    if source_present and destination_present:
        raise ReconciliationError("quarantine_path_collision")
    if source_present:
        return "source"
    if destination_present:
        return "destination"
    return "missing"


def _storage_layout(data: Path, classification: str) -> StorageLayout:
    exports_root_path = data / "exports"
    exports_root_existing = _optional_secure_directory(
        exports_root_path,
        "exports_root_invalid",
    )
    exports_root = (
        exports_root_path
        if exports_root_existing is None
        else exports_root_existing
    )
    analysis_parent_path = exports_root / classification
    analysis_parent_existing = _optional_secure_directory(
        analysis_parent_path,
        "exports_classification_root_invalid",
    )
    analysis_parent = (
        analysis_parent_path
        if analysis_parent_existing is None
        else analysis_parent_existing
    )
    controlled_root = _optional_secure_directory(
        data / "controlled-audio-exports",
        "controlled_root_invalid",
    )
    if controlled_root is None:
        controlled_parent = data / "controlled-audio-exports" / classification
    else:
        optional_parent = _optional_secure_directory(
            controlled_root / classification,
            "controlled_classification_root_invalid",
        )
        controlled_parent = (
            controlled_root / classification
            if optional_parent is None
            else optional_parent
        )
    return StorageLayout(
        exports_root=exports_root,
        analysis_parent=analysis_parent,
        controlled_root=controlled_root,
        controlled_parent=controlled_parent,
    )


def _summary_matches(actual: TreeSummary | None, expected: object) -> bool:
    if actual is None:
        return expected is None
    return isinstance(expected, dict) and actual.public() == expected


def _intent_payload(
    *,
    batch_id_sha256: str,
    classification: str,
    owner_hash: str,
    lease_expires_at: str,
    analysis: TreeSummary,
    controlled: TreeSummary | None,
) -> dict[str, object]:
    return {
        "analysis": analysis.public(),
        "batch_id_sha256": batch_id_sha256,
        "controlled_audio": None if controlled is None else controlled.public(),
        "data_classification": classification,
        "operation": "quarantine_abandoned_preintent_export_staging",
        "schema_version": _INTENT_SCHEMA,
        "staging_lease_expires_at": lease_expires_at,
        "staging_owner_hash": owner_hash,
    }


def _summary_from_public(value: object, *, controlled: bool) -> TreeSummary:
    if (
        not isinstance(value, dict)
        or set(value) != {"byte_count", "file_count", "sha256"}
        or type(value["byte_count"]) is not int
        or type(value["file_count"]) is not int
        or not isinstance(value["sha256"], str)
        or not _HEX_64.fullmatch(value["sha256"])
        or value["byte_count"] <= 0
        or (
            controlled
            and value["byte_count"] > _MAX_CONTROLLED_TOTAL_BYTES
        )
    ):
        raise ReconciliationError("quarantine_intent_invalid")
    file_count = int(value["file_count"])
    if controlled:
        if not 1 <= file_count <= _MAX_AUDIO_FILES:
            raise ReconciliationError("quarantine_intent_invalid")
    elif file_count != len(_ANALYSIS_NAMES):
        raise ReconciliationError("quarantine_intent_invalid")
    return TreeSummary(
        str(value["sha256"]),
        file_count,
        int(value["byte_count"]),
    )


def _validate_intent(
    value: dict[str, object],
    *,
    batch_id: str,
    batch_id_sha256: str,
    classification: str,
) -> tuple[str, TreeSummary, TreeSummary | None]:
    if set(value) != {
        "analysis",
        "batch_id_sha256",
        "controlled_audio",
        "data_classification",
        "operation",
        "schema_version",
        "staging_lease_expires_at",
        "staging_owner_hash",
    }:
        raise ReconciliationError("quarantine_intent_invalid")
    owner = value["staging_owner_hash"]
    if (
        value["schema_version"] != _INTENT_SCHEMA
        or value["operation"]
        != "quarantine_abandoned_preintent_export_staging"
        or value["batch_id_sha256"] != batch_id_sha256
        or value["data_classification"] != classification
        or not isinstance(owner, str)
        or not _HEX_64.fullmatch(owner)
    ):
        raise ReconciliationError("quarantine_intent_invalid")
    _parse_lease(value["staging_lease_expires_at"])
    analysis = _summary_from_public(value["analysis"], controlled=False)
    controlled_value = value["controlled_audio"]
    controlled = (
        None
        if controlled_value is None
        else _summary_from_public(controlled_value, controlled=True)
    )
    expected_name = "preintent-reconciliation-" + hashlib.sha256(
        f"{batch_id}\x00{owner}".encode("ascii")
    ).hexdigest()[:32]
    if not _BUNDLE_NAME.fullmatch(expected_name):
        raise ReconciliationError("quarantine_intent_invalid")
    return owner, analysis, controlled


def _ensure_canonical_evidence(
    path: Path,
    value: Mapping[str, object],
) -> None:
    if path.exists() or path.is_symlink():
        if _read_canonical_json(path, maximum=_MAX_RECEIPT_BYTES) != value:
            raise ReconciliationError("quarantine_evidence_collision")
        return
    _atomic_write_json(path, value)


def _completion_receipt_matches(
    value: dict[str, object],
    intent: Mapping[str, object],
) -> bool:
    if set(value) != (set(intent) | {
        "database_owner_and_lease_cleared",
        "reconciled_at",
    }):
        return False
    reconciled_at = value.get("reconciled_at")
    if not isinstance(reconciled_at, str) or len(reconciled_at) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(reconciled_at)
    except ValueError:
        return False
    if parsed.tzinfo is not None:
        return False
    expected = {
        **intent,
        "database_owner_and_lease_cleared": True,
        "reconciled_at": reconciled_at,
        "schema_version": _RECEIPT_SCHEMA,
    }
    return value == expected


def _find_completion_bundle(
    quarantine_root: Path,
    *,
    batch_id: str,
    batch_id_sha256: str,
    classification: str,
) -> tuple[Path, dict[str, object], str, TreeSummary, TreeSummary | None]:
    try:
        entries = tuple(os.scandir(quarantine_root))
    except OSError as exc:
        raise ReconciliationError("quarantine_bundle_unreadable") from exc
    if len(entries) > _MAX_QUARANTINE_BUNDLES:
        raise ReconciliationError("quarantine_bundle_limit_exceeded")
    matches: list[
        tuple[Path, dict[str, object], str, TreeSummary, TreeSummary | None]
    ] = []
    for entry in entries:
        if not _BUNDLE_NAME.fullmatch(entry.name):
            continue
        bundle = _secure_directory(
            quarantine_root / entry.name,
            "quarantine_bundle_invalid",
        )
        intent_path = bundle / _INTENT_NAME
        if not intent_path.exists() and not intent_path.is_symlink():
            raise ReconciliationError("quarantine_bundle_invalid")
        intent = _read_canonical_json(
            intent_path,
            maximum=_MAX_RECEIPT_BYTES,
        )
        if intent.get("batch_id_sha256") != batch_id_sha256:
            continue
        owner, analysis, controlled = _validate_intent(
            intent,
            batch_id=batch_id,
            batch_id_sha256=batch_id_sha256,
            classification=classification,
        )
        expected_name = "preintent-reconciliation-" + hashlib.sha256(
            f"{batch_id}\x00{owner}".encode("ascii")
        ).hexdigest()[:32]
        if bundle.name != expected_name:
            raise ReconciliationError("quarantine_intent_invalid")
        matches.append((bundle, intent, owner, analysis, controlled))
    if len(matches) != 1:
        raise ReconciliationError("released_staging_evidence_missing")
    return matches[0]


def _bundle_entries(bundle: Path) -> set[str]:
    try:
        return {entry.name for entry in os.scandir(bundle)}
    except OSError as exc:
        raise ReconciliationError("quarantine_bundle_invalid") from exc


def _prepare_bundle(
    quarantine_root: Path,
    *,
    bundle_name: str,
    expected_intent: Mapping[str, object],
    dry_run: bool,
) -> tuple[Path, bool]:
    bundle = quarantine_root / bundle_name
    if not bundle.exists() and not bundle.is_symlink():
        if dry_run:
            return bundle, False
        try:
            bundle.mkdir(mode=0o700)
            _fsync_directory(quarantine_root)
            _atomic_write_json(bundle / _INTENT_NAME, expected_intent)
        except ReconciliationError:
            try:
                bundle.rmdir()
                _fsync_directory(quarantine_root)
            except OSError:
                pass
            raise
        except OSError as exc:
            raise ReconciliationError("quarantine_bundle_create_failed") from exc
        return bundle, True

    _secure_directory(bundle, "quarantine_bundle_invalid")
    entries = _bundle_entries(bundle)
    if not entries <= {
        _INTENT_NAME,
        _RECEIPT_NAME,
        _UNCERTAIN_NAME,
        "analysis",
        "controlled-audio",
    }:
        raise ReconciliationError("quarantine_bundle_invalid")
    if _RECEIPT_NAME in entries:
        raise ReconciliationError("quarantine_bundle_state_conflict")
    actual_intent = _read_canonical_json(
        bundle / _INTENT_NAME,
        maximum=_MAX_RECEIPT_BYTES,
    )
    if actual_intent != expected_intent:
        raise ReconciliationError("quarantine_intent_mismatch")
    return bundle, False


def _move_into_bundle(source: Path, destination: Path) -> bool:
    state = _path_state(source, destination)
    if state == "destination":
        return False
    if state == "missing":
        raise ReconciliationError("staging_directory_missing")
    try:
        os.rename(source, destination)
        _fsync_directory(source.parent)
        _fsync_directory(destination.parent)
    except OSError as exc:
        raise ReconciliationError("quarantine_atomic_rename_failed") from exc
    return True


def _prune_empty_ancestors(
    classification_root: Path,
    storage_root: Path,
    data_root: Path,
    *,
    pruned: list[Path],
) -> None:
    """Remove only known-empty scaffolding with rmdir, never recursive delete."""

    for path, parent in (
        (classification_root, storage_root),
        (storage_root, data_root),
    ):
        try:
            path.rmdir()
        except OSError as exc:
            # A non-empty directory belongs to another authoritative batch and
            # must remain untouched.  Every other failure is operationally
            # significant and cannot be hidden before the DB lease is cleared.
            if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                continue
            raise ReconciliationError("staging_scaffold_prune_failed") from exc
        pruned.append(path)
        try:
            _fsync_directory(parent)
        except OSError as exc:
            raise ReconciliationError("staging_scaffold_prune_failed") from exc


def _restore_pruned_ancestors(pruned: Iterable[Path]) -> None:
    """Recreate only scaffolding removed by this invocation, outermost first."""

    try:
        for path in sorted(set(pruned), key=lambda item: len(item.parts)):
            if path.exists() or path.is_symlink():
                _secure_directory(path, "filesystem_rollback_uncertain")
                continue
            path.mkdir(mode=0o700)
            os.chmod(path, 0o700)
            _fsync_directory(path.parent)
    except (OSError, ReconciliationError) as exc:
        raise ReconciliationError("filesystem_rollback_uncertain") from exc


def _rollback_filesystem(
    *,
    bundle: Path,
    analysis_source: Path,
    analysis_destination: Path,
    controlled_source: Path,
    controlled_destination: Path,
    controlled_expected: bool,
    pruned_ancestors: Iterable[Path],
) -> None:
    try:
        _restore_pruned_ancestors(pruned_ancestors)
        if controlled_expected and _path_state(
            controlled_source, controlled_destination
        ) == "destination":
            os.rename(controlled_destination, controlled_source)
            _fsync_directory(controlled_destination.parent)
            _fsync_directory(controlled_source.parent)
        if _path_state(analysis_source, analysis_destination) == "destination":
            os.rename(analysis_destination, analysis_source)
            _fsync_directory(analysis_destination.parent)
            _fsync_directory(analysis_source.parent)
        for name in (_UNCERTAIN_NAME, _RECEIPT_NAME, _INTENT_NAME):
            path = bundle / name
            if path.exists() and not path.is_symlink():
                path.unlink()
        bundle.rmdir()
        _fsync_directory(bundle.parent)
    except (OSError, ReconciliationError) as exc:
        raise ReconciliationError("filesystem_rollback_uncertain") from exc


def _commit_release(connection: sqlite3.Connection) -> None:
    """Small seam used to test the ambiguous-commit safety contract."""

    connection.commit()


def _postcommit_verify(data_root: Path, batch_id: str) -> None:
    database = data_root / "app.db"
    uri = "file:" + quote(str(database), safe="/") + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        row = connection.execute(
            "SELECT status,manifest_sha256,publication_manifest_json,"
            "staging_owner_hash,staging_lease_expires_at "
            "FROM exportbatch WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise ReconciliationError("database_postcommit_verification_failed") from exc
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass
    if row != ("staging", None, None, None, None):
        raise ReconciliationError("database_postcommit_verification_failed")


def _resume_postcommit_completion(
    connection: sqlite3.Connection,
    *,
    data: Path,
    quarantine: Path,
    batch_id: str,
    classification: str,
    export_scope_hash: str,
    now: datetime,
    dry_run: bool,
) -> ReconciliationResult:
    """Finish durable evidence after the DB lease-clear commit already won."""

    batch_id_sha256 = hashlib.sha256(batch_id.encode("ascii")).hexdigest()
    bundle, intent, owner_hash, expected_analysis, expected_controlled = (
        _find_completion_bundle(
            quarantine,
            batch_id=batch_id,
            batch_id_sha256=batch_id_sha256,
            classification=classification,
        )
    )
    entries = _bundle_entries(bundle)
    if not entries <= {
        _INTENT_NAME,
        _RECEIPT_NAME,
        _UNCERTAIN_NAME,
        "analysis",
        "controlled-audio",
    }:
        raise ReconciliationError("quarantine_bundle_invalid")
    layout = _storage_layout(data, classification)
    analysis_source = layout.analysis_parent / batch_id
    controlled_source = layout.controlled_parent / batch_id
    analysis_destination = bundle / "analysis"
    controlled_destination = bundle / "controlled-audio"
    if _path_state(analysis_source, analysis_destination) != "destination":
        raise ReconciliationError("released_staging_evidence_incomplete")
    controlled_state = _path_state(controlled_source, controlled_destination)
    if (
        (expected_controlled is None and controlled_state != "missing")
        or (
            expected_controlled is not None
            and controlled_state != "destination"
        )
    ):
        raise ReconciliationError("released_staging_evidence_incomplete")

    analysis_summary, manifest_rows = _analysis_files(
        analysis_destination,
        batch_id=batch_id,
        owner_hash=owner_hash,
    )
    controlled_summary = _validate_controlled_closure(
        connection,
        data_root=data,
        classification=classification,
        export_scope_hash=export_scope_hash,
        manifest_rows=manifest_rows,
        controlled_dir=(
            controlled_destination if expected_controlled is not None else None
        ),
    )
    if (
        analysis_summary != expected_analysis
        or controlled_summary != expected_controlled
    ):
        raise ReconciliationError("released_staging_evidence_incomplete")
    if dry_run:
        return ReconciliationResult(
            "completion_pending",
            batch_id_sha256,
            bundle.name,
        )
    _durably_sync_tree(analysis_destination)
    if expected_controlled is not None:
        _durably_sync_tree(controlled_destination)
    _fsync_directory(bundle)

    pruned: list[Path] = []
    if layout.analysis_parent.exists():
        _prune_empty_ancestors(
            layout.analysis_parent,
            layout.exports_root,
            data,
            pruned=pruned,
        )
    if (
        expected_controlled is not None
        and layout.controlled_parent.exists()
    ):
        if layout.controlled_root is None:
            raise ReconciliationError("controlled_root_invalid")
        _prune_empty_ancestors(
            layout.controlled_parent,
            layout.controlled_root,
            data,
            pruned=pruned,
        )

    _postcommit_verify(data, batch_id)
    receipt_path = bundle / _RECEIPT_NAME
    if receipt_path.exists() or receipt_path.is_symlink():
        receipt = _read_canonical_json(
            receipt_path,
            maximum=_MAX_RECEIPT_BYTES,
        )
        if not _completion_receipt_matches(receipt, intent):
            raise ReconciliationError("reconciliation_receipt_invalid")
    else:
        receipt = {
            **intent,
            "database_owner_and_lease_cleared": True,
            "reconciled_at": now.isoformat(timespec="microseconds"),
            "schema_version": _RECEIPT_SCHEMA,
        }
        _atomic_write_json(receipt_path, receipt)
    return ReconciliationResult(
        "reconciled",
        batch_id_sha256,
        bundle.name,
    )


def reconcile_preintent_staging(
    *,
    data_root: Path,
    quarantine_root: Path,
    batch_id: str,
    confirm_offline: bool,
    now: datetime | None = None,
    dry_run: bool = False,
) -> ReconciliationResult:
    """Validate and quarantine one abandoned pre-intent staging batch."""

    if not confirm_offline:
        raise ReconciliationError("offline_confirmation_required")
    if (
        not isinstance(batch_id, str)
        or not _SAFE_BATCH_ID.fullmatch(batch_id)
    ):
        raise ReconciliationError("batch_id_invalid")
    effective_now = _normalize_now(now)
    data = _secure_directory(Path(data_root), "data_root_invalid")
    quarantine = _secure_directory(
        Path(quarantine_root),
        "quarantine_root_invalid",
    )
    if data == quarantine or data in quarantine.parents or quarantine in data.parents:
        raise ReconciliationError("quarantine_root_not_separate")
    if data.stat().st_dev != quarantine.stat().st_dev:
        raise ReconciliationError("quarantine_cross_filesystem_forbidden")

    database = data / "app.db"
    db_info = _secure_regular_file(
        database,
        maximum=1 << 50,
        allow_empty=False,
        code="database_file_invalid",
    )
    if db_info.st_dev != quarantine.stat().st_dev:
        raise ReconciliationError("quarantine_cross_filesystem_forbidden")
    uri = "file:" + quote(str(database), safe="/") + "?mode=rw"
    try:
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=0")
        # IMMEDIATE is the SQLite write fence: no export worker or migration
        # can acquire a competing writer after this point.  Read-only schema
        # reflection remains possible so the shared current-schema contract can
        # independently inspect CHECK constraints on the same frozen state.
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        try:
            connection.close()
        except UnboundLocalError:
            pass
        raise ReconciliationError("database_not_offline") from exc

    bundle: Path | None = None
    pruned_ancestors: list[Path] = []
    release_attempted = False
    committed = False
    try:
        guard = _backup_guard()
        _verify_current_schema(connection, guard)
        batch = _load_batch(
            connection,
            batch_id=batch_id,
            now=effective_now,
            allow_released=True,
        )
        classification = str(batch["data_classification"])
        if batch["staging_owner_hash"] is None:
            result = _resume_postcommit_completion(
                connection,
                data=data,
                quarantine=quarantine,
                batch_id=batch_id,
                classification=classification,
                export_scope_hash=str(batch["export_scope_hash"]),
                now=effective_now,
                dry_run=dry_run,
            )
            connection.rollback()
            return result
        owner_hash = str(batch["staging_owner_hash"])
        lease_expires_at = str(batch["staging_lease_expires_at"])

        layout = _storage_layout(data, classification)
        exports_root = layout.exports_root
        analysis_parent = layout.analysis_parent
        controlled_root = layout.controlled_root
        controlled_parent = layout.controlled_parent
        analysis_source = analysis_parent / batch_id
        controlled_source = controlled_parent / batch_id

        digest = hashlib.sha256(
            f"{batch_id}\x00{owner_hash}".encode("ascii")
        ).hexdigest()[:32]
        batch_id_sha256 = hashlib.sha256(batch_id.encode("ascii")).hexdigest()
        bundle_name = f"preintent-reconciliation-{digest}"
        analysis_destination = quarantine / bundle_name / "analysis"
        controlled_destination = quarantine / bundle_name / "controlled-audio"

        analysis_state = _path_state(analysis_source, analysis_destination)
        if analysis_state == "missing":
            raise ReconciliationError("analysis_staging_missing")
        analysis_dir = (
            analysis_source if analysis_state == "source" else analysis_destination
        )
        analysis_summary, manifest_rows = _analysis_files(
            analysis_dir,
            batch_id=batch_id,
            owner_hash=owner_hash,
        )

        controlled_state = _path_state(controlled_source, controlled_destination)
        controlled_dir = None
        if controlled_state != "missing":
            controlled_dir = (
                controlled_source
                if controlled_state == "source"
                else controlled_destination
            )
        controlled_summary = _validate_controlled_closure(
            connection,
            data_root=data,
            classification=classification,
            export_scope_hash=str(batch["export_scope_hash"]),
            manifest_rows=manifest_rows,
            controlled_dir=controlled_dir,
        )
        if controlled_summary is None and controlled_state != "missing":
            raise ReconciliationError("controlled_orphan_structure")
        if controlled_summary is not None and controlled_state == "missing":
            raise ReconciliationError("controlled_audio_closure_invalid")

        intent = _intent_payload(
            batch_id_sha256=batch_id_sha256,
            classification=classification,
            owner_hash=owner_hash,
            lease_expires_at=lease_expires_at,
            analysis=analysis_summary,
            controlled=controlled_summary,
        )
        bundle, bundle_created = _prepare_bundle(
            quarantine,
            bundle_name=bundle_name,
            expected_intent=intent,
            dry_run=dry_run,
        )
        if dry_run:
            connection.rollback()
            return ReconciliationResult("eligible", batch_id_sha256, None)

        try:
            _durably_sync_tree(analysis_dir)
            if controlled_dir is not None:
                _durably_sync_tree(controlled_dir)
            _move_into_bundle(analysis_source, analysis_destination)
            if controlled_summary is not None:
                _move_into_bundle(controlled_source, controlled_destination)
            elif _path_state(controlled_source, controlled_destination) != "missing":
                raise ReconciliationError("controlled_orphan_structure")

            moved_analysis, moved_rows = _analysis_files(
                analysis_destination,
                batch_id=batch_id,
                owner_hash=owner_hash,
            )
            moved_controlled = _validate_controlled_closure(
                connection,
                data_root=data,
                classification=classification,
                export_scope_hash=str(batch["export_scope_hash"]),
                manifest_rows=moved_rows,
                controlled_dir=(
                    controlled_destination
                    if controlled_summary is not None
                    else None
                ),
            )
            if (
                moved_analysis != analysis_summary
                or moved_controlled != controlled_summary
            ):
                raise ReconciliationError("quarantine_postmove_mismatch")
            _fsync_directory(bundle)

            if analysis_parent.exists():
                _prune_empty_ancestors(
                    analysis_parent,
                    exports_root,
                    data,
                    pruned=pruned_ancestors,
                )
            if controlled_summary is not None:
                if controlled_parent.exists():
                    if controlled_root is None:
                        raise ReconciliationError("controlled_root_invalid")
                    _prune_empty_ancestors(
                        controlled_parent,
                        controlled_root,
                        data,
                        pruned=pruned_ancestors,
                    )

            authoritative = _load_batch(
                connection,
                batch_id=batch_id,
                now=effective_now,
            )
            if (
                authoritative["staging_owner_hash"] != owner_hash
                or str(authoritative["staging_lease_expires_at"])
                != lease_expires_at
            ):
                raise ReconciliationError("staging_authority_changed")
            cursor = connection.execute(
                "UPDATE exportbatch SET staging_owner_hash=NULL,"
                "staging_lease_expires_at=NULL "
                "WHERE batch_id=? AND status='staging' "
                "AND manifest_sha256 IS NULL "
                "AND publication_manifest_json IS NULL "
                "AND staging_owner_hash=? "
                "AND staging_lease_expires_at=?",
                (batch_id, owner_hash, lease_expires_at),
            )
            if cursor.rowcount != 1:
                raise ReconciliationError("staging_authority_changed")
        except Exception:
            connection.rollback()
            if bundle_created:
                _rollback_filesystem(
                    bundle=bundle,
                    analysis_source=analysis_source,
                    analysis_destination=analysis_destination,
                    controlled_source=controlled_source,
                    controlled_destination=controlled_destination,
                    controlled_expected=controlled_summary is not None,
                    pruned_ancestors=pruned_ancestors,
                )
            raise

        release_attempted = True
        try:
            _commit_release(connection)
            committed = True
        except Exception as exc:
            uncertain = {
                "batch_id_sha256": batch_id_sha256,
                "database_commit_state": "uncertain",
                "operation": "clear_expired_staging_owner_and_lease",
                "schema_version": _RECEIPT_SCHEMA,
            }
            try:
                _ensure_canonical_evidence(bundle / _UNCERTAIN_NAME, uncertain)
            except ReconciliationError as marker_error:
                raise ReconciliationError(
                    "database_commit_uncertain_marker_failed"
                ) from marker_error
            raise ReconciliationError("database_commit_uncertain") from exc

        receipt = {
            **intent,
            "database_owner_and_lease_cleared": True,
            "reconciled_at": effective_now.isoformat(timespec="microseconds"),
            "schema_version": _RECEIPT_SCHEMA,
        }
        _postcommit_verify(data, batch_id)
        _ensure_canonical_evidence(bundle / _RECEIPT_NAME, receipt)
        return ReconciliationResult(
            "reconciled",
            batch_id_sha256,
            bundle_name,
        )
    except ReconciliationError:
        if not release_attempted:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
        raise
    except (OSError, sqlite3.Error) as exc:
        if not release_attempted:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
        raise ReconciliationError("reconciliation_failed") from exc
    finally:
        try:
            connection.close()
        except sqlite3.Error:
            if committed:
                raise ReconciliationError("database_close_uncertain")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline quarantine for one expired, pre-intent export staging batch."
        )
    )
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--quarantine-root", required=True, type=Path)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument(
        "--confirm-offline",
        action="store_true",
        help="attest that the app, export workers, backup jobs, and writers are stopped",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate eligibility under a database write fence without moving files",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = reconcile_preintent_staging(
            data_root=args.data_root,
            quarantine_root=args.quarantine_root,
            batch_id=args.batch_id,
            confirm_offline=args.confirm_offline,
            dry_run=args.dry_run,
        )
    except ReconciliationError as exc:
        if args.json:
            print(json.dumps(
                {"code": exc.code, "status": "error"},
                sort_keys=True,
                separators=(",", ":"),
            ))
        else:
            print(f"reconcile_export_staging: {exc.code}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(
            result.public(),
            sort_keys=True,
            separators=(",", ":"),
        ))
    else:
        print(result.status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
