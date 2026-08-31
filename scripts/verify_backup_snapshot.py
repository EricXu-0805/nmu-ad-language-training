#!/usr/bin/env python3
"""Fail-closed verification and atomic publication for NMU backup snapshots.

The default diagnostics intentionally contain stable reason codes only.  Backup
trees contain research identifiers in filenames, so paths and raw audio ids must
not be copied into systemd, launchd, Docker, or terminal logs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import sys
from urllib.parse import quote

from sqlalchemy import create_engine, inspect


_HEX_64 = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_AUDIO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_AUDIO_EXTENSIONS = frozenset({".webm", ".mp3", ".wav", ".m4a", ".ogg"})
_AUDIO_FORMATS = frozenset({"webm", "mp3", "wav", "m4a", "ogg"})
_AUDIO_STATUSES = frozenset({
    "recorded",
    "exported",
    "checksum_verified",
    "reliability_review_done",
    "deletable",
    "deleted",
})
_EXPORT_BATCH_STATUSES = frozenset({"staging", "artifacts_ready", "published"})
_EXPORT_CLASSIFICATIONS = frozenset({"research", "simulation"})
_EXPORT_ARTIFACT_KINDS = frozenset({
    "csv", "controlled_audio", "manifest", "staging_receipt",
})
_EXPORT_SHEET_NAMES = frozenset({
    "session", "turns", "attempts", "interactions", "item_scores", "scales",
    "legacy_unverified_scales", "abnormal", "audio_manifest",
})
_POST_EXPORT_AUDIO_STATUSES = frozenset({
    "exported", "checksum_verified", "reliability_review_done", "deletable",
    "deleted",
})
_SAFE_EXPORT_PATH_PART = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_AUDIO_EXPORT_CODE = re.compile(
    r"^AUDIO-v1-[A-Za-z0-9][A-Za-z0-9._-]{0,31}-[0-9a-f]{20}$"
)
_SAFE_EXPORTED_AUDIO_NAME = re.compile(
    r"^(AUDIO-v1-[A-Za-z0-9][A-Za-z0-9._-]{0,31}-[0-9a-f]{20})"
    r"(\.[a-z0-9]{1,10})$"
)
_MANIFEST_FORBIDDEN_KEYS = frozenset({
    "patient_id", "session_id", "raw_audio_id", "answer", "response",
    "asr_text", "confirmed_response_text", "note", "api_key", "key",
})
_REQUIRED_AUDIO_COLUMNS = frozenset({
    "raw_audio_id",
    "status",
    "withdrawn",
    "withdrawal_status",
    "checksum",
    "byte_count",
    "audio_format",
})
_CHUNK_BYTES = 1024 * 1024
MAX_AUDIO_BLOB_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_FILES = 100_000
MAX_SNAPSHOT_DIRECTORIES = 10_000
MAX_MANIFEST_ENTRIES = MAX_SNAPSHOT_FILES - 1
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_LINE_BYTES = 8 * 1024
MAX_EXPORT_JSON_BYTES = 16 * 1024 * 1024
MAX_EXPORT_BATCHES = MAX_SNAPSHOT_DIRECTORIES
MAX_EXPORT_ARTIFACTS = MAX_MANIFEST_ENTRIES
VPS_CONFIG_FILES = frozenset({
    "env",
    "Caddyfile",
    "nmu.service",
    "nmu-caddy.service",
    "nmu-backup.service",
    "nmu-backup.timer",
})
SUPPORTED_ALEMBIC_HEADS = frozenset({"d0c22a6dae2a"})
CURRENT_RECOVERY_SCHEMA_SHA256 = (
    "847c2b8db25dd910e5b0e03ad0e24c0c0803a93db7b04e4de4d084e4702c0e00"
)
REQUIRED_APPLICATION_TABLES = frozenset({
    "abnormalevent",
    "assessmentcommand",
    "assessmentdeferralapproval",
    "assessmentevent",
    "assessmenteventcloseout",
    "assessmentinstance",
    "assessmentitemresponse",
    "assessmentrecordingauthorization",
    "assessmentscoringevidence",
    "attemptcaptureprocessing",
    "attemptevent",
    "audioassetrow",
    "audiocapturereceipt",
    "audiolocalcopydisposalreceipt",
    "auditanchor",
    "auditlog",
    "authsession",
    "autopilotcontrolevent",
    "autopilotrepeatrequest",
    "caregiverhelpdisposition",
    "caregiverhelprequest",
    "exportartifact",
    "exportbatch",
    "interactionevent",
    "interactionpresentationreceipt",
    "itemevent",
    "livestate",
    "patient",
    "patientdevicecapability",
    "patientpausereceipt",
    "patientwithdrawalevent",
    "providerreadinessprobe",
    "qualitydisclosurerecord",
    "qualityreleaseepoch",
    "qualityreleaseepochrowsnapshot",
    "qualityreleaseepochsession",
    "questionnaireitemvalue",
    "questionnairerecord",
    "rapportutteranceevent",
    "researchuser",
    "runtimecommand",
    "runtimecommandack",
    "scaleresult",
    "session",
    "sessionautopilotstate",
    "sessioncloseoutreport",
    "sessionoutcomesummary",
    "sessionruntimestate",
    "technicalpausereceipt",
    "ttsserveevidence",
    "turnconfirmationrevision",
    "turnevent",
    "visitplan",
    "visitplancommand",
    "week1profile",
})
RECOVERY_SCHEMA_TABLES = REQUIRED_APPLICATION_TABLES | {"alembic_version"}


class SnapshotError(RuntimeError):
    """A stable, non-identifying snapshot rejection reason."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalize_sql_fragment(value: object) -> str:
    source = str(value)
    normalized: list[str] = []
    quote_end = ""
    pending_space = False
    index = 0
    while index < len(source):
        char = source[index]
        if quote_end:
            normalized.append(char)
            if char == quote_end:
                if quote_end != "]" and index + 1 < len(source) \
                        and source[index + 1] == quote_end:
                    normalized.append(source[index + 1])
                    index += 2
                    continue
                quote_end = ""
            index += 1
            continue
        if char.isspace():
            pending_space = True
            index += 1
            continue
        if pending_space and normalized:
            normalized.append(" ")
        pending_space = False
        normalized.append(char)
        if char in {"'", '"', "`"}:
            quote_end = char
        elif char == "[":
            quote_end = "]"
        index += 1
    return "".join(normalized)


def _canonical_table_sql(value: object) -> tuple[tuple[str, ...], str]:
    """Canonicalize stored table DDL without losing quoted literal semantics."""

    source = str(value)
    start = source.find("(")
    end = source.rfind(")")
    if start < 0 or end <= start:
        raise SnapshotError("sqlite_schema_reflection_failed")
    clauses: list[str] = []
    current: list[str] = []
    depth = 0
    quote_end = ""
    index = start + 1
    while index < end:
        char = source[index]
        if quote_end:
            current.append(char)
            if char == quote_end:
                if quote_end != "]" and index + 1 < end \
                        and source[index + 1] == quote_end:
                    current.append(source[index + 1])
                    index += 2
                    continue
                quote_end = ""
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote_end = char
            current.append(char)
        elif char == "[":
            quote_end = "]"
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            if depth < 1:
                raise SnapshotError("sqlite_schema_reflection_failed")
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            clause = _normalize_sql_fragment("".join(current))
            if not clause:
                raise SnapshotError("sqlite_schema_reflection_failed")
            clauses.append(clause)
            current = []
        else:
            current.append(char)
        index += 1
    if quote_end or depth != 0:
        raise SnapshotError("sqlite_schema_reflection_failed")
    clause = _normalize_sql_fragment("".join(current))
    if not clause:
        raise SnapshotError("sqlite_schema_reflection_failed")
    clauses.append(clause)
    suffix = _normalize_sql_fragment(source[end + 1:])
    return tuple(sorted(clauses)), suffix


def _check_constraint_contract(
        connection: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
    rows = tuple(connection.execute("PRAGMA database_list"))
    database_path = next(
        (str(row[2]) for row in rows if str(row[1]) == "main"), ""
    )
    if not database_path:
        raise SnapshotError("sqlite_schema_reflection_failed")
    encoded_path = quote(str(Path(database_path).resolve(strict=True)), safe="/")
    engine = create_engine(
        f"sqlite+pysqlite:///file:{encoded_path}?mode=ro&uri=true",
        echo=False,
        hide_parameters=True,
    )
    try:
        inspector = inspect(engine)
        return {
            table_name: sorted(
                (
                    str(item.get("name") or ""),
                    _normalize_sql_fragment(item.get("sqltext") or ""),
                )
                for item in inspector.get_check_constraints(table_name)
            )
            for table_name in sorted(RECOVERY_SCHEMA_TABLES)
        }
    except SnapshotError:
        raise
    except Exception as exc:
        raise SnapshotError("sqlite_schema_reflection_failed") from exc
    finally:
        engine.dispose()


def _schema_contract(connection: sqlite3.Connection) -> dict[str, object]:
    """Return recovery-relevant SQLite structure without DDL-order drift."""

    contract: dict[str, object] = {}
    check_constraints = _check_constraint_contract(connection)
    for table_name in sorted(RECOVERY_SCHEMA_TABLES):
        quoted_table = _quote_identifier(table_name)
        table_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        if table_row is None or table_row[0] is None:
            raise SnapshotError("sqlite_schema_reflection_failed")
        table_sql = _canonical_table_sql(table_row[0])
        columns = sorted(
            (
                int(row[0]),
                str(row[1]),
                str(row[2] or "").strip().upper(),
                int(row[3]),
                None if row[4] is None else _normalize_sql_fragment(row[4]),
                int(row[5]),
                int(row[6]),
            )
            for row in connection.execute(f"PRAGMA table_xinfo({quoted_table})")
        )

        foreign_key_groups: dict[int, list[tuple[object, ...]]] = {}
        for row in connection.execute(f"PRAGMA foreign_key_list({quoted_table})"):
            foreign_key_groups.setdefault(int(row[0]), []).append(
                (
                    int(row[1]),
                    str(row[2]),
                    str(row[3]),
                    str(row[4] or ""),
                    str(row[5]).upper(),
                    str(row[6]).upper(),
                    str(row[7]).upper(),
                )
            )
        foreign_keys = sorted(
            tuple(sorted(group, key=lambda item: int(item[0])))
            for group in foreign_key_groups.values()
        )

        indexes: list[tuple[object, ...]] = []
        for row in connection.execute(f"PRAGMA index_list({quoted_table})"):
            index_name = str(row[1])
            unique = int(row[2])
            origin = str(row[3])
            partial = int(row[4])
            if origin == "pk":
                continue
            quoted_index = _quote_identifier(index_name)
            key_parts = tuple(
                (
                    int(detail[0]),
                    None if detail[2] is None else str(detail[2]),
                    int(detail[3]),
                    str(detail[4]),
                )
                for detail in connection.execute(
                    f"PRAGMA index_xinfo({quoted_index})"
                )
                if int(detail[5]) == 1
            )
            index_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            ).fetchone()
            index_sql = (
                None
                if index_row is None or index_row[0] is None
                else _normalize_sql_fragment(index_row[0])
            )
            # SQLite assigns sqlite_autoindex_* ordinals according to creation
            # order, so their names are not a stable recovery contract.  Names
            # of explicit (origin="c") indexes are stable and migration-relevant.
            contract_name = index_name if origin == "c" else ""
            indexes.append(
                (contract_name, unique, origin, partial, index_sql, key_parts)
            )

        contract[table_name] = {
            "checks": check_constraints[table_name],
            "columns": columns,
            "foreign_keys": foreign_keys,
            "indexes": sorted(indexes),
            # PRAGMA table_xinfo omits column collation and table-level flags.
            # Canonical DDL closes those gaps (including STRICT/WITHOUT ROWID,
            # named/deferrable constraints, and other stored table semantics).
            "table_sql": table_sql,
        }
    return contract


def _schema_contract_fingerprint(connection: sqlite3.Connection) -> str:
    contract = _schema_contract(connection)
    encoded = json.dumps(
        contract,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _root(path: str | Path) -> Path:
    candidate = Path(path)
    try:
        meta = candidate.lstat()
    except OSError as exc:
        raise SnapshotError("snapshot_missing") from exc
    if stat.S_ISLNK(meta.st_mode) or not stat.S_ISDIR(meta.st_mode):
        raise SnapshotError("snapshot_root_not_directory")
    if stat.S_IMODE(meta.st_mode) != 0o700:
        raise SnapshotError("snapshot_permissions_invalid")
    try:
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise SnapshotError("snapshot_root_unresolvable") from exc


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK_BYTES):
                digest.update(chunk)
                total += len(chunk)
    except OSError as exc:
        raise SnapshotError("snapshot_file_unreadable") from exc
    return digest.hexdigest(), total


def _regular_files_and_directories(root: Path) -> tuple[dict[str, Path], set[str]]:
    files: dict[str, Path] = {}
    directories: set[str] = {"."}
    try:
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            current_rel = current_path.relative_to(root).as_posix() or "."
            directories.add(current_rel)
            if len(directories) > MAX_SNAPSHOT_DIRECTORIES:
                raise SnapshotError("snapshot_directory_limit_exceeded")
            for name in tuple(dirnames):
                path = current_path / name
                meta = path.lstat()
                if stat.S_ISLNK(meta.st_mode) or not stat.S_ISDIR(meta.st_mode):
                    raise SnapshotError("snapshot_non_directory_entry")
                if stat.S_IMODE(meta.st_mode) != 0o700:
                    raise SnapshotError("snapshot_permissions_invalid")
                directories.add(path.relative_to(root).as_posix())
                if len(directories) > MAX_SNAPSHOT_DIRECTORIES:
                    raise SnapshotError("snapshot_directory_limit_exceeded")
            for name in filenames:
                path = current_path / name
                meta = path.lstat()
                if stat.S_ISLNK(meta.st_mode) or not stat.S_ISREG(meta.st_mode):
                    raise SnapshotError("snapshot_non_regular_file")
                if meta.st_nlink != 1:
                    raise SnapshotError("snapshot_hardlinked_file")
                if stat.S_IMODE(meta.st_mode) != 0o600:
                    raise SnapshotError("snapshot_permissions_invalid")
                if len(files) >= MAX_SNAPSHOT_FILES:
                    raise SnapshotError("snapshot_file_limit_exceeded")
                files[path.relative_to(root).as_posix()] = path
    except SnapshotError:
        raise
    except OSError as exc:
        raise SnapshotError("snapshot_tree_unreadable") from exc
    return files, directories


def _manifest_entries(manifest: Path) -> dict[str, str]:
    try:
        if manifest.stat().st_size > MAX_MANIFEST_BYTES:
            raise SnapshotError("manifest_too_large")
        handle = manifest.open("rb")
    except OSError as exc:
        raise SnapshotError("manifest_unreadable") from exc
    entries: dict[str, str] = {}
    total_bytes = 0
    try:
        with handle:
            while True:
                raw_line = handle.readline(MAX_MANIFEST_LINE_BYTES + 1)
                if not raw_line:
                    break
                if len(raw_line) > MAX_MANIFEST_LINE_BYTES:
                    raise SnapshotError("manifest_record_too_long")
                total_bytes += len(raw_line)
                if total_bytes > MAX_MANIFEST_BYTES:
                    raise SnapshotError("manifest_too_large")
                if len(entries) >= MAX_MANIFEST_ENTRIES:
                    raise SnapshotError("manifest_entry_limit_exceeded")
                raw = raw_line.removesuffix(b"\n").removesuffix(b"\r")
                # Both `shasum -a 256` and `sha256sum` emit: 64 hex, space,
                # text/binary marker, relative filename.  Escaped coreutils
                # records and control-character filenames are rejected instead
                # of being ambiguously normalized.
                if (len(raw) < 67 or raw.startswith(b"\\")
                        or raw[64:66] not in {b"  ", b" *"}):
                    raise SnapshotError("manifest_record_invalid")
                try:
                    checksum = raw[:64].decode("ascii")
                    rel = os.fsdecode(raw[66:])
                except (UnicodeDecodeError, ValueError) as exc:
                    raise SnapshotError("manifest_record_invalid") from exc
                if not _HEX_64.fullmatch(checksum):
                    raise SnapshotError("manifest_checksum_invalid")
                while rel.startswith("./"):
                    rel = rel[2:]
                pure = PurePosixPath(rel)
                if (not rel or pure.is_absolute() or rel == "MANIFEST.sha256"
                        or any(part in {"", ".", ".."} for part in pure.parts)
                        or any(ord(char) < 32 or ord(char) == 127 for char in rel)):
                    raise SnapshotError("manifest_path_invalid")
                normalized = pure.as_posix()
                if normalized != rel:
                    raise SnapshotError("manifest_path_invalid")
                if normalized in entries:
                    raise SnapshotError("manifest_duplicate_path")
                entries[normalized] = checksum.lower()
    except SnapshotError:
        raise
    except OSError as exc:
        raise SnapshotError("manifest_unreadable") from exc
    return entries


def manifest_file_list(path: str | Path) -> tuple[str, ...]:
    """Return a bounded rsync allowlist without exposing unchecked paths."""

    manifest = Path(path)
    try:
        meta = manifest.lstat()
    except OSError as exc:
        raise SnapshotError("manifest_unreadable") from exc
    if (stat.S_ISLNK(meta.st_mode) or not stat.S_ISREG(meta.st_mode)
            or meta.st_nlink != 1):
        raise SnapshotError("manifest_not_regular")
    if stat.S_IMODE(meta.st_mode) != 0o600:
        raise SnapshotError("manifest_permissions_invalid")
    entries = _manifest_entries(manifest)
    return ("./MANIFEST.sha256", *(f"./{rel}" for rel in sorted(entries)))


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _manifest_has_forbidden_content(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).casefold() in _MANIFEST_FORBIDDEN_KEYS
            or _manifest_has_forbidden_content(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_manifest_has_forbidden_content(item) for item in value)
    return False


def _export_metadata(raw: str) -> dict[str, object]:
    try:
        metadata = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SnapshotError("export_metadata_invalid") from exc
    if (not isinstance(metadata, dict)
            or _canonical_json_bytes(metadata).decode("utf-8") != raw
            or set(metadata) != {"audio_touched", "excluded_items", "sheet_counts"}
            or not isinstance(metadata["audio_touched"], list)
            or not all(isinstance(value, str) for value in metadata["audio_touched"])
            or metadata["audio_touched"] != sorted(set(metadata["audio_touched"]))
            or not all(
                _SAFE_AUDIO_EXPORT_CODE.fullmatch(value)
                for value in metadata["audio_touched"]
            )
            or not isinstance(metadata["excluded_items"], list)
            or not all(isinstance(value, str) for value in metadata["excluded_items"])
            or not isinstance(metadata["sheet_counts"], dict)
            or set(metadata["sheet_counts"]) != _EXPORT_SHEET_NAMES
            or not all(
                isinstance(key, str) and type(value) is int and value >= 0
                for key, value in metadata["sheet_counts"].items()
            )
            or _manifest_has_forbidden_content(metadata)):
        raise SnapshotError("export_metadata_invalid")
    return metadata


def _export_snapshot_path(
        *, batch_id: str, data_classification: str, realm: str,
        kind: str, relative_path: str) -> str:
    expected_realms = {
        f"{data_classification}_analysis",
        f"{data_classification}_controlled_audio",
    }
    if realm not in expected_realms or kind not in _EXPORT_ARTIFACT_KINDS:
        raise SnapshotError("export_artifact_contract_invalid")
    pure = PurePosixPath(relative_path)
    parts = pure.parts
    if (not relative_path or pure.is_absolute() or pure.as_posix() != relative_path
            or len(relative_path) > 500 or len(parts) < 3
            or parts[0] != data_classification or parts[1] != batch_id
            or any(
                part in {"", ".", ".."}
                or not _SAFE_EXPORT_PATH_PART.fullmatch(part)
                for part in parts
            )):
        raise SnapshotError("export_artifact_path_invalid")
    if kind == "controlled_audio":
        if (realm != f"{data_classification}_controlled_audio"
                or parts[2] != "audio"):
            raise SnapshotError("export_artifact_contract_invalid")
        root = "controlled-audio-exports"
    else:
        if realm != f"{data_classification}_analysis":
            raise SnapshotError("export_artifact_contract_invalid")
        root = "exports"
    return f"{root}/{relative_path}"


def _verify_staging_export_intent(
        *, files: dict[str, Path], batch_id: str, data_classification: str,
        manifest_sha256: str, metadata_raw: str,
        publication_raw: str) -> tuple[set[str], set[str]]:
    metadata = _export_metadata(metadata_raw)
    try:
        manifest = json.loads(publication_raw)
    except (TypeError, ValueError) as exc:
        raise SnapshotError("export_manifest_invalid") from exc
    canonical = _canonical_json_bytes(manifest)
    if (canonical.decode("utf-8") != publication_raw
            or hashlib.sha256(canonical).hexdigest() != manifest_sha256
            or not isinstance(manifest, dict)
            or set(manifest) != {
                "schema_version", "batch_id", "deidentified",
                "data_classification", "artifacts", "result",
            }
            or manifest.get("schema_version") != "export-manifest.v1"
            or manifest.get("batch_id") != batch_id
            or manifest.get("deidentified") is not True
            or manifest.get("data_classification") != data_classification
            or manifest.get("result") != metadata
            or not isinstance(manifest.get("artifacts"), list)
            or _manifest_has_forbidden_content(manifest)):
        raise SnapshotError("export_manifest_invalid")

    expected_files: set[str] = set()
    expected_directories: set[str] = set()
    csv_names: set[str] = set()
    receipt_rows: list[tuple[dict[str, object], Path]] = []
    controlled_codes: list[str] = []
    descriptors: list[dict[str, object]] = []
    for raw in manifest["artifacts"]:
        if (not isinstance(raw, dict) or set(raw) != {
                "realm", "kind", "relative_path", "sha256", "byte_count"}):
            raise SnapshotError("export_artifact_contract_invalid")
        realm = raw["realm"]
        kind = raw["kind"]
        relative_path = raw["relative_path"]
        checksum = raw["sha256"]
        byte_count = raw["byte_count"]
        if (not isinstance(realm, str) or not isinstance(kind, str)
                or kind == "manifest" or not isinstance(relative_path, str)
                or not isinstance(checksum, str)
                or not _HEX_64.fullmatch(checksum)
                or checksum != checksum.lower()
                or type(byte_count) is not int or byte_count < 0):
            raise SnapshotError("export_artifact_contract_invalid")
        snapshot_rel = _export_snapshot_path(
            batch_id=batch_id,
            data_classification=data_classification,
            realm=realm,
            kind=kind,
            relative_path=relative_path,
        )
        if snapshot_rel in expected_files:
            raise SnapshotError("export_artifact_contract_invalid")
        expected_files.add(snapshot_rel)
        parent = PurePosixPath(snapshot_rel).parent
        while parent.as_posix() != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent
        path = files.get(snapshot_rel)
        if path is None:
            raise SnapshotError("export_artifact_missing")
        if _sha256(path) != (checksum, byte_count):
            raise SnapshotError("export_artifact_mismatch")
        descriptor = {
            "realm": realm,
            "kind": kind,
            "relative_path": relative_path,
            "sha256": checksum,
            "byte_count": byte_count,
        }
        descriptors.append(descriptor)
        parts = PurePosixPath(relative_path).parts
        if kind == "csv":
            if (len(parts) != 3 or not parts[2].endswith(".csv")
                    or parts[2][:-4] not in _EXPORT_SHEET_NAMES):
                raise SnapshotError("export_artifact_contract_invalid")
            csv_names.add(parts[2][:-4])
        elif kind == "staging_receipt":
            if relative_path != (
                    f"{data_classification}/{batch_id}/.staging-receipt.json"):
                raise SnapshotError("export_artifact_contract_invalid")
            receipt_rows.append((descriptor, path))
        elif kind == "controlled_audio":
            name_match = (
                _SAFE_EXPORTED_AUDIO_NAME.fullmatch(parts[3])
                if len(parts) == 4 else None
            )
            if name_match is None:
                raise SnapshotError("export_artifact_contract_invalid")
            controlled_codes.append(name_match.group(1))
        else:
            raise SnapshotError("export_artifact_contract_invalid")
    if (csv_names != _EXPORT_SHEET_NAMES or len(receipt_rows) != 1
            or len(descriptors) != 10 + len(controlled_codes)
            or len(set(controlled_codes)) != len(controlled_codes)
            or sorted(controlled_codes) != metadata["audio_touched"]):
        raise SnapshotError("export_artifact_contract_invalid")
    receipt_descriptor, receipt_path = receipt_rows[0]
    if receipt_descriptor["byte_count"] > 4096:
        raise SnapshotError("export_staging_receipt_invalid")
    try:
        receipt_bytes = receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes)
    except (OSError, ValueError) as exc:
        raise SnapshotError("export_staging_receipt_invalid") from exc
    if (not isinstance(receipt, dict)
            or set(receipt) != {"batch_id", "staging_owner_hash"}
            or receipt.get("batch_id") != batch_id
            or not isinstance(receipt.get("staging_owner_hash"), str)
            or not _HEX_64.fullmatch(receipt["staging_owner_hash"])
            or receipt["staging_owner_hash"]
            != receipt["staging_owner_hash"].lower()
            or receipt_bytes != _canonical_json_bytes(receipt)):
        raise SnapshotError("export_staging_receipt_invalid")

    manifest_rel = f"exports/{data_classification}/{batch_id}/manifest.json"
    manifest_path = files.get(manifest_rel)
    if manifest_path is not None:
        try:
            if manifest_path.read_bytes() != canonical:
                raise SnapshotError("export_manifest_invalid")
        except OSError as exc:
            raise SnapshotError("export_manifest_invalid") from exc
        expected_files.add(manifest_rel)
        parent = PurePosixPath(manifest_rel).parent
        while parent.as_posix() != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    return expected_files, expected_directories


def _verify_manifest(root: Path) -> tuple[dict[str, Path], set[str]]:
    files, directories = _regular_files_and_directories(root)
    manifest = files.get("MANIFEST.sha256")
    if manifest is None:
        raise SnapshotError("manifest_missing")
    entries = _manifest_entries(manifest)
    expected = set(files) - {"MANIFEST.sha256"}
    if set(entries) != expected:
        raise SnapshotError("manifest_coverage_mismatch")
    for rel, expected_hash in entries.items():
        actual_hash, _ = _sha256(files[rel])
        if actual_hash != expected_hash:
            raise SnapshotError("manifest_hash_mismatch")
    return files, directories


def _open_database(database: Path) -> sqlite3.Connection:
    try:
        uri = "file:" + quote(str(database.resolve(strict=True)), safe="/") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        connection.execute("PRAGMA query_only=ON")
        # Restore validation must cover index contents as well as table pages.
        # SQLite quick_check intentionally skips index/table consistency.
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            connection.close()
            raise SnapshotError("sqlite_integrity_check_failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            connection.close()
            raise SnapshotError("sqlite_foreign_key_check_failed")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            if not str(row[0]).startswith("sqlite_")
        }
        if "alembic_version" not in tables:
            connection.close()
            raise SnapshotError("alembic_revision_missing")
        revisions = tuple(
            str(row[0])
            for row in connection.execute("SELECT version_num FROM alembic_version")
        )
        if len(revisions) != 1 or revisions[0] not in SUPPORTED_ALEMBIC_HEADS:
            connection.close()
            raise SnapshotError("alembic_revision_unsupported")
        if tables != RECOVERY_SCHEMA_TABLES:
            connection.close()
            raise SnapshotError("recovery_schema_incomplete")
        if connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type IN ('view','trigger') LIMIT 1"
        ).fetchone() is not None:
            connection.close()
            raise SnapshotError("recovery_schema_incomplete")
        if _schema_contract_fingerprint(connection) != CURRENT_RECOVERY_SCHEMA_SHA256:
            connection.close()
            raise SnapshotError("recovery_schema_incomplete")
        return connection
    except SnapshotError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise SnapshotError("sqlite_unreadable") from exc


def _audio_files(root: Path) -> dict[str, tuple[Path, str, int]]:
    audio_root = root / "audio"
    if not audio_root.exists():
        return {}
    try:
        root_meta = audio_root.lstat()
    except OSError as exc:
        raise SnapshotError("audio_root_unreadable") from exc
    if stat.S_ISLNK(root_meta.st_mode) or not stat.S_ISDIR(root_meta.st_mode):
        raise SnapshotError("audio_root_invalid")

    result: dict[str, tuple[Path, str, int]] = {}
    try:
        entries = tuple(os.scandir(audio_root))
    except OSError as exc:
        raise SnapshotError("audio_root_unreadable") from exc
    for entry in entries:
        try:
            meta = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise SnapshotError("audio_entry_unreadable") from exc
        if entry.name.startswith("."):
            raise SnapshotError("audio_transient_file")
        if (entry.is_symlink() or not stat.S_ISREG(meta.st_mode)
                or meta.st_nlink != 1):
            raise SnapshotError("audio_non_regular_file")
        suffix = Path(entry.name).suffix.lower()
        raw_audio_id = entry.name[:-len(suffix)] if suffix else ""
        if suffix not in _AUDIO_EXTENSIONS or not _SAFE_AUDIO_ID.fullmatch(raw_audio_id):
            raise SnapshotError("audio_filename_invalid")
        if raw_audio_id in result:
            raise SnapshotError("audio_multiple_files")
        if meta.st_size <= 0 or meta.st_size > MAX_AUDIO_BLOB_BYTES:
            raise SnapshotError("audio_file_size_invalid")
        path = Path(entry.path)
        checksum, byte_count = _sha256(path)
        result[raw_audio_id] = (path, checksum, byte_count)
    return result


def _verify_audio_semantics(
        root: Path, connection: sqlite3.Connection) -> None:
    audio_files = _audio_files(root)
    unmatched_audio_files = set(audio_files)
    try:
        table = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND lower(name)='audioassetrow'"
        ).fetchone()
        if table is None:
            raise SnapshotError("audio_schema_missing")
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(audioassetrow)")
        }
        if not _REQUIRED_AUDIO_COLUMNS.issubset(columns):
            raise SnapshotError("audio_schema_incomplete")
        rows = connection.execute(
            "SELECT raw_audio_id,status,withdrawn,withdrawal_status,checksum,byte_count,"
            "audio_format "
            "FROM audioassetrow"
        )
        for (raw_id_value, status, withdrawn, withdrawal_status, checksum,
             byte_count, audio_format) in rows:
            raw_audio_id = str(raw_id_value or "")
            if not _SAFE_AUDIO_ID.fullmatch(raw_audio_id):
                raise SnapshotError("audio_database_id_invalid")
            unmatched_audio_files.discard(raw_audio_id)
            status_value = str(status or "")
            if status_value not in _AUDIO_STATUSES:
                raise SnapshotError("audio_database_status_invalid")
            if type(withdrawn) is not int or withdrawn not in {0, 1}:
                raise SnapshotError("audio_database_withdrawn_invalid")
            file_facts = audio_files.get(raw_audio_id)
            is_withdrawn = withdrawn == 1
            governed_out = (
                status_value == "deleted"
                or is_withdrawn
                or bool(str(withdrawal_status or "").strip())
            )
            if governed_out:
                if file_facts is not None:
                    raise SnapshotError("audio_governed_bytes_present")
                continue

            has_checksum = checksum is not None and str(checksum).strip() != ""
            has_byte_count = byte_count is not None
            if not has_checksum and not has_byte_count:
                # Metadata may be registered before an upload finishes.  It is
                # authoritative only after both server-observed receipt fields
                # exist; a final file before then is an unsafe cross-store race.
                if file_facts is not None:
                    raise SnapshotError("audio_unreceipted_file")
                continue
            if not has_checksum or not has_byte_count:
                raise SnapshotError("audio_receipt_incomplete")
            expected_hash = str(checksum).strip().lower()
            if not _HEX_64.fullmatch(expected_hash):
                raise SnapshotError("audio_receipt_invalid")
            if (type(byte_count) is not int or byte_count <= 0
                    or byte_count > MAX_AUDIO_BLOB_BYTES):
                raise SnapshotError("audio_receipt_invalid")
            expected_bytes = byte_count
            expected_format = str(audio_format or "").strip().lower()
            if expected_format not in _AUDIO_FORMATS:
                raise SnapshotError("audio_format_invalid")
            if file_facts is None:
                raise SnapshotError("audio_authoritative_file_missing")
            actual_path, actual_hash, actual_bytes = file_facts
            if actual_path.suffix.lower() != f".{expected_format}":
                raise SnapshotError("audio_format_mismatch")
            if actual_hash != expected_hash or actual_bytes != expected_bytes:
                raise SnapshotError("audio_authoritative_file_mismatch")

        if unmatched_audio_files:
            raise SnapshotError("audio_orphan_file")
        # The empty unmatched set proves every file has a DB row; row-state and
        # receipt checks above prove each of those rows authorizes that file.
    except SnapshotError:
        raise
    except sqlite3.Error as exc:
        raise SnapshotError("audio_database_query_failed") from exc


def _verify_export_semantics(
        files: dict[str, Path], directories: set[str],
        connection: sqlite3.Connection) -> None:
    """Prove the export ledger and both exported file realms are identical."""

    expected_files: set[str] = set()
    expected_directories: set[str] = set()
    staging_batches: list[tuple[str, str]] = []
    published_batch_ids: set[str] = set()
    try:
        batch_count = connection.execute(
            "SELECT count(*) FROM exportbatch"
        ).fetchone()[0]
        artifact_count = connection.execute(
            "SELECT count(*) FROM exportartifact"
        ).fetchone()[0]
        if (type(batch_count) is not int or batch_count < 0
                or batch_count > MAX_EXPORT_BATCHES
                or type(artifact_count) is not int or artifact_count < 0
                or artifact_count > MAX_EXPORT_ARTIFACTS):
            raise SnapshotError("export_ledger_limit_exceeded")
        audio_groups: dict[str, list[tuple[object, ...]]] = {}
        for row in connection.execute(
                "SELECT export_batch_id,status,checksum,byte_count,"
                "data_classification,exported_at FROM audioassetrow "
                "WHERE export_batch_id IS NOT NULL ORDER BY raw_audio_id"):
            batch_key = row[0]
            if not isinstance(batch_key, str):
                raise SnapshotError("export_audio_binding_invalid")
            audio_groups.setdefault(batch_key, []).append(tuple(row[1:]))
        artifact_groups: dict[str, list[tuple[object, ...]]] = {}
        for row in connection.execute(
                "SELECT batch_id,realm,kind,relative_path,sha256,byte_count "
                "FROM exportartifact ORDER BY id"):
            batch_key = row[0]
            if not isinstance(batch_key, str):
                raise SnapshotError("export_artifact_contract_invalid")
            artifact_groups.setdefault(batch_key, []).append(tuple(row[1:]))

        batch_ids: set[str] = set()
        batches = connection.execute(
            "SELECT batch_id,schema_version,status,data_classification,"
            "deidentified,manifest_sha256,staging_owner_hash,"
            "staging_lease_expires_at,artifacts_ready_at,published_at,"
            "length(CAST(result_metadata_json AS BLOB)),"
            "length(CAST(publication_manifest_json AS BLOB)) "
            "FROM exportbatch ORDER BY batch_id"
        )
        for row in batches:
            (batch_id, schema_version, status, data_classification,
             deidentified, manifest_sha256, staging_owner_hash,
             staging_lease_expires_at, artifacts_ready_at, published_at,
             metadata_bytes, publication_bytes) = row
            if (not isinstance(batch_id, str)
                    or not _SAFE_EXPORT_PATH_PART.fullmatch(batch_id)
                    or batch_id in {".", ".."}):
                raise SnapshotError("export_batch_contract_invalid")
            batch_ids.add(batch_id)
            if (schema_version != "export-batch.v1"
                    or status not in _EXPORT_BATCH_STATUSES
                    or data_classification not in _EXPORT_CLASSIFICATIONS
                    or type(deidentified) is not int or deidentified != 1):
                raise SnapshotError("export_batch_contract_invalid")
            artifacts = artifact_groups.get(batch_id, [])
            if status == "staging":
                # A staging row is either an empty/pre-intent retry marker or
                # an exact durable publication intent. Artifact-ledger rows
                # and audio bindings belong only to later states.
                if artifacts or audio_groups.get(batch_id):
                    raise SnapshotError("export_staging_unsettled")
                has_manifest_hash = manifest_sha256 is not None
                has_publication = publication_bytes is not None
                if has_manifest_hash != has_publication:
                    raise SnapshotError("export_staging_unsettled")
                if not has_manifest_hash:
                    if artifacts_ready_at is not None or published_at is not None:
                        raise SnapshotError("export_batch_contract_invalid")
                    staging_batches.append((data_classification, batch_id))
                    continue
                if (staging_owner_hash is not None
                        or staging_lease_expires_at is not None
                        or artifacts_ready_at is not None
                        or published_at is not None
                        or not isinstance(manifest_sha256, str)
                        or not _HEX_64.fullmatch(manifest_sha256)
                        or manifest_sha256 != manifest_sha256.lower()
                        or type(metadata_bytes) is not int or metadata_bytes < 2
                        or metadata_bytes > MAX_EXPORT_JSON_BYTES
                        or type(publication_bytes) is not int
                        or publication_bytes < 2
                        or publication_bytes > MAX_EXPORT_JSON_BYTES):
                    raise SnapshotError("export_staging_unsettled")
                json_row = connection.execute(
                    "SELECT result_metadata_json,publication_manifest_json "
                    "FROM exportbatch WHERE batch_id=?",
                    (batch_id,),
                ).fetchone()
                if (json_row is None or not isinstance(json_row[0], str)
                        or not isinstance(json_row[1], str)):
                    raise SnapshotError("export_staging_unsettled")
                intent_files, intent_directories = (
                    _verify_staging_export_intent(
                        files=files,
                        batch_id=batch_id,
                        data_classification=data_classification,
                        manifest_sha256=manifest_sha256,
                        metadata_raw=json_row[0],
                        publication_raw=json_row[1],
                    )
                )
                if expected_files.intersection(intent_files):
                    raise SnapshotError("export_artifact_contract_invalid")
                expected_files.update(intent_files)
                expected_directories.update(intent_directories)
                continue
            if status == "artifacts_ready" and audio_groups.get(batch_id):
                raise SnapshotError("export_audio_binding_invalid")
            if (staging_owner_hash is not None
                    or staging_lease_expires_at is not None
                    or artifacts_ready_at is None
                    or (status == "published") != (published_at is not None)
                    or not isinstance(manifest_sha256, str)
                    or not _HEX_64.fullmatch(manifest_sha256)
                    or manifest_sha256 != manifest_sha256.lower()
                    or type(metadata_bytes) is not int or metadata_bytes < 2
                    or metadata_bytes > MAX_EXPORT_JSON_BYTES
                    or type(publication_bytes) is not int or publication_bytes < 2
                    or publication_bytes > MAX_EXPORT_JSON_BYTES):
                raise SnapshotError("export_batch_contract_invalid")
            json_row = connection.execute(
                "SELECT result_metadata_json,publication_manifest_json "
                "FROM exportbatch WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if (json_row is None or not isinstance(json_row[0], str)
                    or not isinstance(json_row[1], str)):
                raise SnapshotError("export_batch_contract_invalid")
            metadata = _export_metadata(json_row[0])
            try:
                publication_manifest = json.loads(json_row[1])
            except (TypeError, ValueError) as exc:
                raise SnapshotError("export_manifest_invalid") from exc

            descriptors: list[dict[str, object]] = []
            manifest_rows: list[tuple[dict[str, object], Path]] = []
            receipt_rows: list[tuple[dict[str, object], Path]] = []
            csv_names: set[str] = set()
            controlled_receipts: list[tuple[str, int]] = []
            controlled_codes: list[str] = []
            for artifact in artifacts:
                realm, kind, relative_path, checksum, byte_count = artifact
                if (not isinstance(realm, str) or not isinstance(kind, str)
                        or not isinstance(relative_path, str)
                        or not isinstance(checksum, str)
                        or not _HEX_64.fullmatch(checksum)
                        or checksum != checksum.lower()
                        or type(byte_count) is not int or byte_count < 0):
                    raise SnapshotError("export_artifact_contract_invalid")
                snapshot_rel = _export_snapshot_path(
                    batch_id=batch_id,
                    data_classification=data_classification,
                    realm=realm,
                    kind=kind,
                    relative_path=relative_path,
                )
                if snapshot_rel in expected_files:
                    raise SnapshotError("export_artifact_contract_invalid")
                expected_files.add(snapshot_rel)
                parent = PurePosixPath(snapshot_rel).parent
                while parent.as_posix() != ".":
                    expected_directories.add(parent.as_posix())
                    parent = parent.parent
                path = files.get(snapshot_rel)
                if path is None:
                    raise SnapshotError("export_artifact_missing")
                actual_hash, actual_bytes = _sha256(path)
                if actual_hash != checksum or actual_bytes != byte_count:
                    raise SnapshotError("export_artifact_mismatch")
                descriptor: dict[str, object] = {
                    "realm": realm,
                    "kind": kind,
                    "relative_path": relative_path,
                    "sha256": checksum,
                    "byte_count": byte_count,
                }
                descriptors.append(descriptor)
                if kind == "manifest":
                    manifest_rows.append((descriptor, path))
                    if relative_path != (
                            f"{data_classification}/{batch_id}/manifest.json"):
                        raise SnapshotError("export_artifact_contract_invalid")
                elif kind == "staging_receipt":
                    receipt_rows.append((descriptor, path))
                    if relative_path != (
                            f"{data_classification}/{batch_id}/"
                            ".staging-receipt.json"):
                        raise SnapshotError("export_artifact_contract_invalid")
                elif kind == "csv":
                    parts = PurePosixPath(relative_path).parts
                    if (len(parts) != 3 or not parts[2].endswith(".csv")
                            or parts[2][:-4] not in _EXPORT_SHEET_NAMES):
                        raise SnapshotError("export_artifact_contract_invalid")
                    csv_names.add(parts[2][:-4])
                elif kind == "controlled_audio":
                    parts = PurePosixPath(relative_path).parts
                    name_match = (
                        _SAFE_EXPORTED_AUDIO_NAME.fullmatch(parts[3])
                        if len(parts) == 4 else None
                    )
                    if name_match is None:
                        raise SnapshotError("export_artifact_contract_invalid")
                    controlled_codes.append(name_match.group(1))
                    controlled_receipts.append((checksum, byte_count))

            if (len(manifest_rows) != 1 or len(receipt_rows) != 1
                    or csv_names != _EXPORT_SHEET_NAMES
                    or len(descriptors) != 11 + len(controlled_receipts)):
                raise SnapshotError("export_artifact_contract_invalid")
            receipt_descriptor, receipt_path = receipt_rows[0]
            if receipt_descriptor["byte_count"] > 4096:
                raise SnapshotError("export_staging_receipt_invalid")
            try:
                receipt_bytes = receipt_path.read_bytes()
                receipt = json.loads(receipt_bytes)
            except (OSError, ValueError) as exc:
                raise SnapshotError("export_staging_receipt_invalid") from exc
            if (not isinstance(receipt, dict)
                    or set(receipt) != {"batch_id", "staging_owner_hash"}
                    or receipt.get("batch_id") != batch_id
                    or not isinstance(receipt.get("staging_owner_hash"), str)
                    or not _HEX_64.fullmatch(receipt["staging_owner_hash"])
                    or receipt["staging_owner_hash"]
                    != receipt["staging_owner_hash"].lower()
                    or receipt_bytes != _canonical_json_bytes(receipt)):
                raise SnapshotError("export_staging_receipt_invalid")
            manifest_descriptor, manifest_path = manifest_rows[0]
            if (manifest_descriptor["sha256"] != manifest_sha256
                    or manifest_descriptor["byte_count"] > MAX_EXPORT_JSON_BYTES):
                raise SnapshotError("export_manifest_invalid")
            expected_manifest = {
                "schema_version": "export-manifest.v1",
                "batch_id": batch_id,
                "deidentified": True,
                "data_classification": data_classification,
                "artifacts": sorted(
                    [item for item in descriptors if item["kind"] != "manifest"],
                    key=lambda item: (
                        str(item["realm"]), str(item["kind"]),
                        str(item["relative_path"]),
                    ),
                ),
                "result": metadata,
            }
            expected_bytes = _canonical_json_bytes(expected_manifest)
            expected_json = expected_bytes.decode("utf-8")
            try:
                manifest_bytes = manifest_path.read_bytes()
                manifest = json.loads(manifest_bytes)
            except (OSError, UnicodeError, ValueError) as exc:
                raise SnapshotError("export_manifest_invalid") from exc
            if (manifest_bytes != expected_bytes
                    or hashlib.sha256(expected_bytes).hexdigest() != manifest_sha256
                    or manifest != expected_manifest
                    or publication_manifest != expected_manifest
                    or json_row[1] != expected_json
                    or _manifest_has_forbidden_content(manifest)):
                raise SnapshotError("export_manifest_invalid")
            if (len(set(controlled_codes)) != len(controlled_codes)
                    or sorted(controlled_codes) != metadata["audio_touched"]):
                raise SnapshotError("export_audio_binding_invalid")
            linked_audio = audio_groups.get(batch_id, [])
            if status == "published":
                published_batch_ids.add(batch_id)
                linked_receipts: list[tuple[str, int]] = []
                for (audio_status, checksum, byte_count,
                     audio_classification, exported_at) in linked_audio:
                    if (audio_status not in _POST_EXPORT_AUDIO_STATUSES
                            or not isinstance(checksum, str)
                            or not _HEX_64.fullmatch(checksum)
                            or checksum != checksum.lower()
                            or type(byte_count) is not int or byte_count <= 0
                            or audio_classification != data_classification
                            or exported_at is None):
                        raise SnapshotError("export_audio_binding_invalid")
                    linked_receipts.append((checksum, byte_count))
                if sorted(linked_receipts) != sorted(controlled_receipts):
                    raise SnapshotError("export_audio_binding_invalid")

        if set(artifact_groups) - batch_ids:
            raise SnapshotError("export_artifact_contract_invalid")
        if set(audio_groups) - published_batch_ids:
            raise SnapshotError("export_audio_binding_invalid")
    except SnapshotError:
        raise
    except sqlite3.Error as exc:
        raise SnapshotError("export_database_query_failed") from exc

    actual_directories = {
        rel for rel in directories
        if rel == "exports" or rel.startswith("exports/")
        or rel == "controlled-audio-exports"
        or rel.startswith("controlled-audio-exports/")
    }
    for classification, batch_id in staging_batches:
        staging_roots = (
            f"exports/{classification}/{batch_id}",
            f"controlled-audio-exports/{classification}/{batch_id}",
        )
        if any(
            rel == staging_root or rel.startswith(f"{staging_root}/")
            for rel in actual_directories
            for staging_root in staging_roots
        ):
            raise SnapshotError("export_staging_unsettled")
    # A current snapshot has no directory-only scaffolding: every export
    # directory is implied by an authoritative file.  This keeps local verify
    # and manifest-only offsite transfer on exactly the same tree contract.
    if actual_directories != expected_directories:
        raise SnapshotError("export_directory_set_mismatch")
    actual_files = {
        rel for rel in files
        if rel == "exports" or rel.startswith("exports/")
        or rel == "controlled-audio-exports"
        or rel.startswith("controlled-audio-exports/")
    }
    if actual_files != expected_files:
        raise SnapshotError("export_file_set_mismatch")


def _verify_vps_config(root: Path) -> None:
    config = root / "config"
    if not config.is_dir() or config.is_symlink():
        raise SnapshotError("vps_config_missing")
    try:
        entries = tuple(config.iterdir())
    except OSError as exc:
        raise SnapshotError("vps_config_unreadable") from exc
    if {entry.name for entry in entries} != VPS_CONFIG_FILES:
        raise SnapshotError("vps_config_incomplete")
    for entry in entries:
        try:
            meta = entry.lstat()
        except OSError as exc:
            raise SnapshotError("vps_config_unreadable") from exc
        if (stat.S_ISLNK(meta.st_mode) or not stat.S_ISREG(meta.st_mode)
                or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_size <= 0):
            raise SnapshotError("vps_config_invalid")


def verify_snapshot(
        path: str | Path, *, require_manifest: bool = True,
        require_vps_config: bool = False) -> Path:
    root = _root(path)
    if require_manifest:
        files, directories = _verify_manifest(root)
    else:
        files, directories = _regular_files_and_directories(root)
    database = root / "app.db"
    if not database.exists():
        raise SnapshotError("database_missing")
    connection = _open_database(database)
    try:
        _verify_audio_semantics(root, connection)
        _verify_export_semantics(files, directories, connection)
    finally:
        connection.close()
    if require_vps_config:
        _verify_vps_config(root)
    return root


def _same_bytes(left: Path, right: Path) -> bool:
    try:
        left_files, left_dirs = _regular_files_and_directories(left)
        right_files, right_dirs = _regular_files_and_directories(right)
        if left_dirs != right_dirs or set(left_files) != set(right_files):
            return False
        for rel, left_path in left_files.items():
            right_path = right_files[rel]
            left_meta = left_path.stat()
            right_meta = right_path.stat()
            if left_meta.st_size != right_meta.st_size:
                return False
            with (left_path.open("rb") as left_handle,
                  right_path.open("rb") as right_handle):
                while True:
                    left_chunk = left_handle.read(_CHUNK_BYTES)
                    right_chunk = right_handle.read(_CHUNK_BYTES)
                    if left_chunk != right_chunk:
                        return False
                    if not left_chunk:
                        break
        return True
    except SnapshotError:
        raise
    except OSError as exc:
        raise SnapshotError("snapshot_compare_unreadable") from exc


def compare_snapshots(
        left: str | Path, right: str | Path, *,
        require_vps_config: bool = False) -> bool:
    left_root = verify_snapshot(left, require_vps_config=require_vps_config)
    right_root = verify_snapshot(right, require_vps_config=require_vps_config)
    return _same_bytes(left_root, right_root)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durably_sync_tree(root: Path) -> None:
    files, directories = _regular_files_and_directories(root)
    try:
        for path in files.values():
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        for rel in sorted(
                directories,
                key=lambda value: (value.count("/"), value),
                reverse=True):
            _fsync_directory(root if rel == "." else root / rel)
    except OSError as exc:
        raise SnapshotError("publish_fsync_failed") from exc


def publish_snapshot(
        source: str | Path, destination: str | Path, *,
        require_vps_config: bool = False) -> None:
    source_root = verify_snapshot(
        source, require_vps_config=require_vps_config)
    _durably_sync_tree(source_root)
    destination_path = Path(destination)
    try:
        destination_parent = destination_path.parent.resolve(strict=True)
    except OSError as exc:
        raise SnapshotError("publish_parent_missing") from exc
    try:
        same_filesystem = source_root.stat().st_dev == destination_parent.stat().st_dev
    except OSError as exc:
        raise SnapshotError("publish_filesystem_probe_failed") from exc
    if not same_filesystem:
        raise SnapshotError("publish_cross_filesystem_forbidden")
    if destination_path.exists() or destination_path.is_symlink():
        raise SnapshotError("publish_destination_exists")
    try:
        os.rename(source_root, destination_path)
    except OSError as exc:
        raise SnapshotError("publish_atomic_rename_failed") from exc
    parents = tuple(dict.fromkeys((source_root.parent, destination_parent)))
    try:
        for parent in parents:
            _fsync_directory(parent)
    except OSError as exc:
        # A rename without a durable parent entry must never be reported as a
        # completed backup.  Restore the verified staging name when possible so
        # the caller's failure trap can quarantine it.
        try:
            os.rename(destination_path, source_root)
            for parent in parents:
                _fsync_directory(parent)
        except OSError as rollback_exc:
            raise SnapshotError("publish_durability_uncertain") from rollback_exc
        raise SnapshotError("publish_parent_fsync_failed") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("snapshot")
    verify_vps = subparsers.add_parser("verify-vps")
    verify_vps.add_argument("snapshot")
    audio = subparsers.add_parser("verify-audio")
    audio.add_argument("snapshot")
    compare = subparsers.add_parser("compare")
    compare.add_argument("left")
    compare.add_argument("right")
    compare_vps = subparsers.add_parser("compare-vps")
    compare_vps.add_argument("left")
    compare_vps.add_argument("right")
    publish = subparsers.add_parser("publish")
    publish.add_argument("source")
    publish.add_argument("destination")
    publish_vps = subparsers.add_parser("publish-vps")
    publish_vps.add_argument("source")
    publish_vps.add_argument("destination")
    manifest_files = subparsers.add_parser("manifest-files")
    manifest_files.add_argument("manifest")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            verify_snapshot(args.snapshot)
        elif args.command == "verify-vps":
            verify_snapshot(args.snapshot, require_vps_config=True)
        elif args.command == "verify-audio":
            verify_snapshot(args.snapshot, require_manifest=False)
        elif args.command == "compare":
            if not compare_snapshots(args.left, args.right):
                print("DIFFERENT", flush=True)
                return 3
        elif args.command == "compare-vps":
            if not compare_snapshots(
                    args.left, args.right, require_vps_config=True):
                print("DIFFERENT", flush=True)
                return 3
        elif args.command == "publish":
            publish_snapshot(args.source, args.destination)
        elif args.command == "publish-vps":
            publish_snapshot(
                args.source, args.destination, require_vps_config=True)
        elif args.command == "manifest-files":
            for rel in manifest_file_list(args.manifest):
                print(rel)
            return 0
        else:  # pragma: no cover - argparse owns the command set
            raise SnapshotError("command_invalid")
    except SnapshotError as exc:
        print(f"REJECTED code={exc.code}", file=sys.stderr, flush=True)
        return 2
    print("OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
