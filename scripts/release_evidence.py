#!/usr/bin/env python3
"""Bind a release id to the exact bytes of a bounded set of JSON receipts.

This module deliberately proves only byte binding.  It does not turn a JSON
field named ``pass`` into clinical, legal, operational, or device approval.
Those receipts still need their own kind-specific validation and authorized
review; this boundary only prevents evidence from another release being
silently reused.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any


SCHEMA_VERSION = "nmu.release-evidence-index.v1"
MAX_INDEX_BYTES = 64 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_ENTRIES = 128

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_KIND = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_INDEX_KEYS = frozenset({"schema_version", "release_id", "entries"})
_ENTRY_KEYS = frozenset({"kind", "path", "sha256"})


class ReleaseEvidenceError(ValueError):
    """Stable, path-free rejection for a malformed or mismatched evidence set."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def validate_release_id(value: object) -> str:
    if not isinstance(value, str) or _HEX_64.fullmatch(value) is None:
        raise ReleaseEvidenceError("release_id_invalid")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseEvidenceError("json_duplicate_key")
        result[key] = value
    return result


_DIRECTORY_FLAGS = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0))
_FILE_FLAGS = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
               | getattr(os, "O_NOFOLLOW", 0))
_SECURE_OPEN_SUPPORTED = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
)


def _open_directory_at(parent_fd: int, name: str, *, error_code: str) -> int:
    try:
        directory_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except (OSError, NotImplementedError) as exc:
        raise ReleaseEvidenceError(error_code) from exc
    try:
        try:
            metadata = os.fstat(directory_fd)
        except OSError as exc:
            raise ReleaseEvidenceError(error_code) from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise ReleaseEvidenceError(error_code)
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _open_directory_chain(path: Path, *, error_code: str) -> int:
    """Open every absolute path component without following a symlink."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        current_fd = os.open(os.sep, _DIRECTORY_FLAGS)
    except OSError as exc:  # pragma: no cover - unusable host filesystem
        raise ReleaseEvidenceError(error_code) from exc
    try:
        for part in absolute.parts[1:]:
            next_fd = _open_directory_at(
                current_fd, part, error_code=error_code)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _read_regular_at(
        directory_fd: int, name: str, *, maximum: int, error_code: str) -> bytes:
    """Open, size-check and bounded-read one regular file through one fd."""
    try:
        file_fd = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
    except (OSError, NotImplementedError) as exc:
        raise ReleaseEvidenceError(error_code) from exc
    try:
        try:
            metadata = os.fstat(file_fd)
        except OSError as exc:
            raise ReleaseEvidenceError(error_code) from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ReleaseEvidenceError(error_code)
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining > 0:
            try:
                chunk = os.read(file_fd, min(64 * 1024, remaining))
            except OSError as exc:
                raise ReleaseEvidenceError(error_code) from exc
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum:
            raise ReleaseEvidenceError(error_code)
        return data
    finally:
        os.close(file_fd)


def _decode_json(data: bytes, *, error_code: str) -> object:
    try:
        return json.loads(
            data.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except ReleaseEvidenceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ReleaseEvidenceError(error_code) from exc


def _safe_relative_path(value: object) -> PurePosixPath:
    if (not isinstance(value, str) or not value or len(value) > 512
            or "\\" in value or "\x00" in value
            or any(ord(character) < 32 or ord(character) == 127
                   for character in value)):
        raise ReleaseEvidenceError("evidence_path_invalid")
    path = PurePosixPath(value)
    if (path.is_absolute() or not path.parts or str(path) != value
            or any(part in {"", ".", ".."} for part in path.parts)):
        raise ReleaseEvidenceError("evidence_path_invalid")
    return path


def _read_receipt(root_fd: int, relative: PurePosixPath) -> bytes:
    """Read a receipt relative to the already-open index directory."""
    current_fd = os.dup(root_fd)
    try:
        for part in relative.parts[:-1]:
            next_fd = _open_directory_at(
                current_fd,
                part,
                error_code="evidence_receipt_unreadable",
            )
            os.close(current_fd)
            current_fd = next_fd
        return _read_regular_at(
            current_fd,
            relative.parts[-1],
            maximum=MAX_RECEIPT_BYTES,
            error_code="evidence_receipt_unreadable",
        )
    finally:
        os.close(current_fd)


def verify_evidence_index(
        index_path: str | os.PathLike[str], expected_release_id: object) -> dict[str, object]:
    """Validate one exact index and return non-secret binding evidence.

    Every referenced file is a JSON receipt whose top-level ``release_id`` must
    equal both the index and the caller's expected id.  The receipt's remaining
    semantics are intentionally outside this generic byte-binding layer.
    """
    if not _SECURE_OPEN_SUPPORTED:
        raise ReleaseEvidenceError("secure_open_unsupported")
    expected = validate_release_id(expected_release_id)
    index = Path(index_path)
    root_fd = _open_directory_chain(
        index.parent, error_code="evidence_index_unreadable")
    try:
        index_bytes = _read_regular_at(
            root_fd,
            index.name,
            maximum=MAX_INDEX_BYTES,
            error_code="evidence_index_unreadable",
        )
        raw = _decode_json(index_bytes, error_code="evidence_index_json_invalid")
        if not isinstance(raw, dict) or set(raw) != _INDEX_KEYS:
            raise ReleaseEvidenceError("evidence_index_schema_invalid")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise ReleaseEvidenceError("evidence_index_schema_invalid")
        index_release_id = validate_release_id(raw.get("release_id"))
        if index_release_id != expected:
            raise ReleaseEvidenceError("evidence_index_release_mismatch")

        entries = raw.get("entries")
        if (not isinstance(entries, list) or not entries
                or len(entries) > MAX_ENTRIES):
            raise ReleaseEvidenceError("evidence_entries_invalid")

        kinds: set[str] = set()
        paths: set[str] = set()
        verified: list[dict[str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
                raise ReleaseEvidenceError("evidence_entry_schema_invalid")
            kind = entry.get("kind")
            if not isinstance(kind, str) or _KIND.fullmatch(kind) is None:
                raise ReleaseEvidenceError("evidence_kind_invalid")
            if kind in kinds:
                raise ReleaseEvidenceError("evidence_kind_duplicate")
            kinds.add(kind)

            relative = _safe_relative_path(entry.get("path"))
            portable_path = str(relative)
            if portable_path in paths:
                raise ReleaseEvidenceError("evidence_path_duplicate")
            paths.add(portable_path)
            declared_sha256 = entry.get("sha256")
            if (not isinstance(declared_sha256, str)
                    or _HEX_64.fullmatch(declared_sha256) is None):
                raise ReleaseEvidenceError("evidence_sha256_invalid")

            receipt_bytes = _read_receipt(root_fd, relative)
            actual_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
            if actual_sha256 != declared_sha256:
                raise ReleaseEvidenceError("evidence_sha256_mismatch")
            receipt = _decode_json(
                receipt_bytes, error_code="evidence_receipt_json_invalid")
            if not isinstance(receipt, dict):
                raise ReleaseEvidenceError("evidence_receipt_schema_invalid")
            try:
                receipt_release_id = validate_release_id(receipt.get("release_id"))
            except ReleaseEvidenceError as exc:
                raise ReleaseEvidenceError("evidence_receipt_release_invalid") from exc
            if receipt_release_id != expected:
                raise ReleaseEvidenceError("evidence_receipt_release_mismatch")
            verified.append({
                "kind": kind,
                "path": portable_path,
                "sha256": actual_sha256,
            })

        return {
            "schema_version": SCHEMA_VERSION,
            "release_id": expected,
            "index_sha256": hashlib.sha256(index_bytes).hexdigest(),
            "receipt_count": len(verified),
            "receipts": verified,
        }
    finally:
        os.close(root_fd)
