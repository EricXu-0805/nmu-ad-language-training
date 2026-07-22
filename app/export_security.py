"""Security boundary for de-identified exports.

This module owns secret-backed pseudonyms, free-text removal, spreadsheet-cell
neutralisation, and private atomic filesystem writes.  It deliberately has no
dependency on FastAPI routes or authentication policy.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Mapping, Optional

from .storage_security import ensure_private_directory, ensure_private_file

DEIDENTIFICATION_KEY_ENV = "DEIDENTIFICATION_KEY"
DEIDENTIFICATION_KEY_ID_ENV = "DEIDENTIFICATION_KEY_ID"
PSEUDONYM_VERSION = "v1"
MIN_DEIDENTIFICATION_KEY_BYTES = 32
PSEUDONYM_HEX_CHARS = 20  # 80 bits
REDACTED_TEXT = "[REDACTED]"

DIRECT_IDENTIFIER_COLUMNS = frozenset({
    "patient_id", "consent_person", "trainer_id", "reviewer_id", "assessor_id",
    # 内部可链接标识同样不得进默认去标识包。输出使用分域 HMAC
    # token，并换成 *_code 列；数据库主键直接删除。
    "session_id", "raw_audio_id", "source_attempt_id", "attempt_id",
    "item_event_id", "turn_id",
})
FREE_TEXT_COLUMNS = frozenset({
    "asr_text", "confirmed_response_text", "judge_reason", "note",
})

_SAFE_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_FORMULA_PREFIXES = frozenset("=+-@")
_ISO_ABSOLUTE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ][0-2]\d:[0-5]\d(?::[0-6]\d(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)?$")


class DeidentificationConfigurationError(RuntimeError):
    """Raised without including secret material when export keying is unsafe."""


@dataclass(frozen=True)
class DeidentificationConfig:
    key: bytes = field(repr=False)
    key_id: str


def load_deidentification_config(
        environment: Optional[Mapping[str, str]] = None) -> DeidentificationConfig:
    source = os.environ if environment is None else environment
    raw_key = source.get(DEIDENTIFICATION_KEY_ENV)
    if raw_key is None or not raw_key.strip():
        raise DeidentificationConfigurationError(
            f"{DEIDENTIFICATION_KEY_ENV} 未配置，去标识导出已关闭")
    key = raw_key.encode("utf-8")
    if len(key) < MIN_DEIDENTIFICATION_KEY_BYTES:
        raise DeidentificationConfigurationError(
            f"{DEIDENTIFICATION_KEY_ENV} 必须至少 {MIN_DEIDENTIFICATION_KEY_BYTES} bytes，去标识导出已关闭")
    key_id = (source.get(DEIDENTIFICATION_KEY_ID_ENV) or "").strip()
    if not _SAFE_KEY_ID.fullmatch(key_id):
        raise DeidentificationConfigurationError(
            f"{DEIDENTIFICATION_KEY_ID_ENV} 缺失或格式无效，去标识导出已关闭")
    return DeidentificationConfig(key=key, key_id=key_id)


def _tokenize_identifier(
        identifier: str, *, domain: str, prefix: str,
        config: Optional[DeidentificationConfig] = None) -> str:
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("待去标识的内部标识不能为空")
    active = config or load_deidentification_config()
    message = f"nmu-{domain}-pseudonym:{PSEUDONYM_VERSION}\x00".encode(
        "ascii") + identifier.encode("utf-8")
    digest = hmac.new(active.key, message, hashlib.sha256).hexdigest()
    return f"{prefix}-{PSEUDONYM_VERSION}-{active.key_id}-{digest[:PSEUDONYM_HEX_CHARS]}"


def pseudonymize_subject(
        patient_id: str, config: Optional[DeidentificationConfig] = None) -> str:
    return _tokenize_identifier(
        patient_id, domain="subject", prefix="SUBJ", config=config)


def pseudonymize_session(
        session_id: str, config: Optional[DeidentificationConfig] = None) -> str:
    return _tokenize_identifier(
        session_id, domain="session", prefix="SESS", config=config)


def pseudonymize_audio(
        raw_audio_id: str, config: Optional[DeidentificationConfig] = None) -> str:
    return _tokenize_identifier(
        raw_audio_id, domain="audio", prefix="AUDIO", config=config)


def pseudonymize_batch(
        batch_id: str, config: Optional[DeidentificationConfig] = None) -> str:
    return _tokenize_identifier(
        batch_id, domain="export-batch", prefix="BATCH", config=config)


def redact_free_text(value: Optional[str]) -> Optional[str]:
    """Default-deny PHI policy: any persisted free text is removed from exports."""
    return None if value is None else REDACTED_TEXT


def _is_absolute_time_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return (lowered in {"date", "time", "datetime", "timestamp", "ts"}
            or lowered.endswith(("_at", "_date", "_datetime", "_timestamp"))
            or key.endswith(("At", "Date", "Time", "Datetime", "Timestamp")))


def _contains_absolute_time(value: object) -> bool:
    if isinstance(value, (datetime, date)):
        return True
    if isinstance(value, str):
        return bool(_ISO_ABSOLUTE_TIME.fullmatch(value.strip()))
    if isinstance(value, Mapping):
        return any(_is_absolute_time_key(key) or _contains_absolute_time(nested)
                   for key, nested in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_absolute_time(nested) for nested in value)
    return False


def assert_deidentified_sheets(sheets: Mapping[str, Iterable[Mapping[str, object]]]) -> None:
    for sheet_name, rows in sheets.items():
        for row in rows:
            leaked = DIRECT_IDENTIFIER_COLUMNS & set(row)
            if leaked:
                raise RuntimeError(
                    f"去标识导出表 {sheet_name} 混入直接标识列 {sorted(leaked)}，拒绝导出")
            unsafe_text = {
                key for key in FREE_TEXT_COLUMNS & set(row)
                if row[key] not in (None, REDACTED_TEXT)
            }
            if unsafe_text:
                raise RuntimeError(
                    f"去标识导出表 {sheet_name} 含未经 PHI 审核的自由文本列 {sorted(unsafe_text)}，拒绝导出")
            absolute_time_columns = {
                key for key in row if _is_absolute_time_key(key)
            }
            if absolute_time_columns:
                raise RuntimeError(
                    f"去标识导出表 {sheet_name} 含绝对时间列 "
                    f"{sorted(absolute_time_columns)}，拒绝导出")
            if any(_contains_absolute_time(value) for value in row.values()):
                raise RuntimeError(
                    f"去标识导出表 {sheet_name} 含绝对日期/时间值，拒绝导出")
            payload_json = row.get("payload_json")
            if isinstance(payload_json, str):
                try:
                    payload = json.loads(payload_json)
                except ValueError as exc:
                    raise RuntimeError(
                        f"去标识导出表 {sheet_name} 的 payload_json 无效，拒绝导出") from exc
                if isinstance(payload, dict):
                    leaked_payload_keys = DIRECT_IDENTIFIER_COLUMNS & set(payload)
                    if leaked_payload_keys:
                        raise RuntimeError(
                            f"去标识导出表 {sheet_name} 的 payload_json 混入可链接标识键 "
                            f"{sorted(leaked_payload_keys)}，拒绝导出")
                    if _contains_absolute_time(payload):
                        raise RuntimeError(
                            f"去标识导出表 {sheet_name} 的 payload_json 含绝对日期/时间，拒绝导出")


def _strip_leading_ignorable(value: str) -> str:
    index = 0
    while index < len(value):
        char = value[index]
        if char.isspace() or unicodedata.category(char).startswith("C"):
            index += 1
            continue
        break
    return value[index:]


def sanitize_csv_cell(value: object) -> object:
    """Neutralise formula injection in every string-valued CSV cell.

    Leading whitespace and control/format characters are removed before the
    effective first character is inspected.  Apostrophe is the spreadsheet
    text prefix and remains literal data in standards-compliant CSV readers.
    """
    if not isinstance(value, str):
        return value
    normalized = _strip_leading_ignorable(value)
    if normalized and normalized[0] in _FORMULA_PREFIXES:
        return "'" + normalized
    return normalized


def _validate_segment(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_PATH_SEGMENT.fullmatch(value):
        raise ValueError(f"{label} 含非法路径字符")
    if value in {".", ".."}:
        raise ValueError(f"{label} 不得为路径跳转段")
    return value


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def ensure_secure_directory(
        root: Path, *segments: str, exclusive_final: bool = False) -> Path:
    root = Path(root)
    if root.exists() and root.is_symlink():
        raise RuntimeError(f"导出根目录不得是符号链接: {root}")
    ensure_private_directory(root)
    resolved_root = root.resolve(strict=True)
    current = root
    for index, raw_segment in enumerate(segments):
        segment = _validate_segment(raw_segment, "导出路径段")
        candidate = current / segment
        is_final = index == len(segments) - 1
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_symlink():
                raise RuntimeError(f"导出目录不得是符号链接: {candidate}")
            if not candidate.is_dir():
                raise RuntimeError(f"导出路径不是目录: {candidate}")
            if exclusive_final and is_final:
                raise FileExistsError(f"导出批次目录已存在: {candidate}")
        else:
            candidate.mkdir(mode=0o700)
        ensure_private_directory(candidate)
        resolved = candidate.resolve(strict=True)
        if not _is_within(resolved, resolved_root):
            raise RuntimeError("导出路径逃逸受控根目录")
        current = candidate
    return current


def find_secure_existing_directory(root: Path, *segments: str) -> Optional[Path]:
    root = Path(root)
    if not root.exists() or root.is_symlink() or not root.is_dir():
        return None
    resolved_root = root.resolve(strict=True)
    current = root
    for raw_segment in segments:
        segment = _validate_segment(raw_segment, "导出路径段")
        candidate = current / segment
        if not candidate.exists() or candidate.is_symlink() or not candidate.is_dir():
            return None
        if not _is_within(candidate.resolve(strict=True), resolved_root):
            return None
        current = candidate
    return current


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_no_replace(temporary: Path, destination: Path) -> None:
    """Atomically publish a completed same-directory temp file without clobbering.

    ``os.replace`` would overwrite a file/symlink created between our check and
    publish.  A same-filesystem hard link is atomic and fails with EEXIST;
    unlinking the temporary name afterwards leaves the completed inode at the
    destination only.
    """
    try:
        os.link(temporary, destination, follow_symlinks=False)
    except FileExistsError as exc:
        raise FileExistsError(f"导出目标在写入期间被占用: {destination}") from exc
    temporary.unlink()


def atomic_write_csv(path: Path, fieldnames: list[str], rows: Iterable[Mapping[str, object]]) -> Path:
    path = Path(path)
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise RuntimeError(f"CSV 目标目录无效: {parent}")
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"CSV 目标已存在或为符号链接: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            # 表头也是 spreadsheet cell，与数据单元格使用同一套注入防护。
            writer.writerow([sanitize_csv_cell(key) for key in fieldnames])
            for row in rows:
                writer.writerow([sanitize_csv_cell(row.get(key)) for key in fieldnames])
            stream.flush()
            os.fsync(stream.fileno())
        _publish_no_replace(temporary, path)
        ensure_private_file(path)
        _fsync_directory(parent)
        return path
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: Mapping[str, object]) -> Path:
    """Durably publish canonical UTF-8 JSON without replacing any path.

    The manifest is the batch's filesystem publication marker.  It is written
    only after every referenced artifact has been fsynced and therefore uses
    the same no-replace/symlink boundary as CSV and controlled audio files.
    """
    path = Path(path)
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise RuntimeError(f"JSON 目标目录无效: {parent}")
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"JSON 目标已存在或为符号链接: {path}")
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _publish_no_replace(temporary, path)
        ensure_private_file(path)
        _fsync_directory(parent)
        return path
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def atomic_copy_file(source: Path, destination: Path) -> Path:
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("受控导出源文件无效或为符号链接")
    parent = destination.parent
    if parent.is_symlink() or not parent.is_dir():
        raise RuntimeError(f"受控音频目标目录无效: {parent}")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"受控音频目标已存在或为符号链接: {destination}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent, prefix=f".{destination.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        os.chmod(temporary, 0o600)
        source_descriptor = os.open(source, source_flags)
        with os.fdopen(source_descriptor, "rb") as source_stream, os.fdopen(descriptor, "wb") as target_stream:
            shutil.copyfileobj(source_stream, target_stream)
            target_stream.flush()
            os.fsync(target_stream.fileno())
        _publish_no_replace(temporary, destination)
        ensure_private_file(destination)
        _fsync_directory(parent)
        return destination
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def safe_remove_tree(path: Path, root: Path) -> None:
    path, root = Path(path), Path(root)
    if not path.exists() and not path.is_symlink():
        return
    if not root.exists() or root.is_symlink() or not root.is_dir():
        raise RuntimeError("受控清理根目录无效或为符号链接")
    # 先按字面路径约束，保证即使 path 自身是外指符号链接，
    # 也只能删除受控根目录下的链接节点，不会跟随到外部。
    lexical_root = Path(os.path.abspath(root))
    lexical_path = Path(os.path.abspath(path))
    if not _is_within(lexical_path, lexical_root) or lexical_path == lexical_root:
        raise RuntimeError("拒绝清理受控根目录之外或根目录本身")
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return
    resolved_root = root.resolve(strict=True)
    if not _is_within(path.resolve(strict=True), resolved_root):
        raise RuntimeError("拒绝清理受控根目录之外的路径")
    shutil.rmtree(path)
