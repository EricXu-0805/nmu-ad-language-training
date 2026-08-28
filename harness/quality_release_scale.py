"""Isolated mechanical-capacity acceptance for a frozen quality release.

This harness deliberately repeats the *same valid Week-2 contract*.  The full
profile is 30 synthetic subjects and 240 Week-2 sessions; it is not evidence
that Weeks 1 or 3-8 exist, are clinically approved, or have been exercised.

The acceptance path is intentionally the production path rather than a scalar
stand-in::

    derive_cohort -> build_payload -> build_research_snapshot
        -> proposal_digest -> publish_epoch -> frozen row pagination

Every run owns a migrated temporary SQLite database and a temporary audio
root.  No repository ``data/`` path is read, listed, or written.

Run the release-sized profile explicitly::

    ./.venv/bin/python -m harness.quality_release_scale --profile full \
        --receipt /path/to/private-0700-dir/quality-release-scale.json

The command prints one JSON receipt.  ``smoke`` is for routine pytest coverage;
it uses the exact same 660-row/session evidence shape at a smaller cardinality.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform as stdlib_platform
import resource
import secrets
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
AS_OF = datetime(2026, 8, 17, 0, 0, 0)
SETTLED_AT = AS_OF - timedelta(days=30)
ITEMS_PER_WEEK2_SESSION = 32
TURNS_PER_WEEK2_SESSION = 78
ATTEMPTS_PER_TURN = 2
# item + turn + 2 attempts + 2 audio rows + 2 capture receipts + confirmation
# revision + one interaction = 32 + 78 * 9 = 734.
EVIDENCE_ROWS_PER_SESSION = 734
_SOURCE_IDENTITY_EXACT_FILES = (
    "alembic.ini",
    "pyproject.toml",
    "harness/__init__.py",
    "scripts/cut_quality_release.py",
    "harness/quality_release_scale.py",
)
_SOURCE_IDENTITY_TREES = (
    ("app", frozenset({".py"})),
    ("alembic", frozenset({".py", ".mako"})),
    # Frozen content definitions and referenced assets are part of the Week-2
    # contract exercised by this acceptance.  Hash every regular file so a new
    # extension cannot silently fall outside the receipt.
    ("content", None),
)


class ScaleAcceptanceError(RuntimeError):
    """A stable, non-sensitive failure from the isolated acceptance run."""


@dataclass(frozen=True)
class ScaleProfile:
    name: str
    session_count: int
    subject_count: int
    turn_page_size: int
    max_wall_seconds: float
    max_peak_rss_mb: float

    @property
    def expected_turn_rows(self) -> int:
        return self.session_count * TURNS_PER_WEEK2_SESSION

    @property
    def expected_evidence_rows(self) -> int:
        return self.session_count * EVIDENCE_ROWS_PER_SESSION

    @property
    def expected_audio_files(self) -> int:
        return (
            self.session_count * TURNS_PER_WEEK2_SESSION
            * ATTEMPTS_PER_TURN
        )

    @property
    def expected_turn_pages(self) -> int:
        return math.ceil(self.expected_turn_rows / self.turn_page_size)


FULL_PROFILE = ScaleProfile(
    name="full",
    session_count=240,
    subject_count=30,
    turn_page_size=1000,
    max_wall_seconds=900.0,
    max_peak_rss_mb=2048.0,
)
SMOKE_PROFILE = ScaleProfile(
    name="smoke",
    session_count=4,
    subject_count=2,
    turn_page_size=64,
    max_wall_seconds=180.0,
    max_peak_rss_mb=2048.0,
)
PROFILES = {profile.name: profile for profile in (FULL_PROFILE, SMOKE_PROFILE)}


@dataclass(frozen=True)
class HttpClientResponse:
    """Small transport-neutral response used by the pure-client 429 test."""

    status_code: int
    headers: Mapping[str, str]
    body: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class HttpPaginationResult:
    rows: tuple[Mapping[str, Any], ...]
    page_count: int
    retry_count: int


def collect_http_pages_with_backoff(
    request: Callable[[str | None], HttpClientResponse],
    *,
    sleep: Callable[[float], None],
    max_retries: int = 5,
) -> HttpPaginationResult:
    """Collect keyset pages and honor numeric ``Retry-After`` on HTTP 429.

    This contains no server, socket, or framework dependency.  A unit test can
    therefore prove that the documented client retries the *same cursor* before
    advancing, without pretending that an in-process HTTP service was started.
    """
    cursor: str | None = None
    seen_cursors: set[str] = set()
    rows: list[Mapping[str, Any]] = []
    pages = 0
    retries = 0
    retries_for_page = 0
    while True:
        response = request(cursor)
        if response.status_code == 429:
            if retries_for_page >= max_retries:
                raise ScaleAcceptanceError("http_429_retry_budget_exhausted")
            raw_delay = response.headers.get("Retry-After", "")
            try:
                delay = float(raw_delay)
            except (TypeError, ValueError):
                delay = min(2.0 ** retries_for_page, 60.0)
            if not math.isfinite(delay) or delay < 0 or delay > 60:
                raise ScaleAcceptanceError("http_429_retry_after_invalid")
            sleep(delay)
            retries += 1
            retries_for_page += 1
            continue
        if response.status_code != 200 or not isinstance(response.body, Mapping):
            raise ScaleAcceptanceError("http_page_failed")
        body = response.body
        page_rows = body.get("rows")
        has_more = body.get("has_more")
        next_cursor = body.get("next_cursor")
        if not isinstance(page_rows, list) or not all(
            isinstance(row, Mapping) for row in page_rows
        ):
            raise ScaleAcceptanceError("http_page_rows_invalid")
        rows.extend(page_rows)
        pages += 1
        retries_for_page = 0
        if has_more is False and next_cursor is None:
            break
        if (
            has_more is not True
            or not isinstance(next_cursor, str)
            or not next_cursor
            or next_cursor in seen_cursors
        ):
            raise ScaleAcceptanceError("http_page_cursor_invalid")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return HttpPaginationResult(tuple(rows), pages, retries)


def _exercise_http_429_contract() -> tuple[int, tuple[float, ...]]:
    calls: list[str | None] = []
    delays: list[float] = []
    responses = iter((
        HttpClientResponse(429, {"Retry-After": "0.25"}),
        HttpClientResponse(200, {}, {
            "rows": [{"ordinal": 1}],
            "has_more": True,
            "next_cursor": "page-2",
        }),
        HttpClientResponse(200, {}, {
            "rows": [{"ordinal": 2}],
            "has_more": False,
            "next_cursor": None,
        }),
    ))

    def request(cursor: str | None) -> HttpClientResponse:
        calls.append(cursor)
        return next(responses)

    result = collect_http_pages_with_backoff(
        request, sleep=delays.append, max_retries=2)
    if (
        calls != [None, None, "page-2"]
        or delays != [0.25]
        or [row["ordinal"] for row in result.rows] != [1, 2]
        or result.page_count != 2
        or result.retry_count != 1
    ):
        raise ScaleAcceptanceError("http_429_client_contract_failed")
    return result.retry_count, tuple(delays)


@contextmanager
def _environment(database_url: str) -> Iterator[None]:
    values = {
        "DATABASE_URL": database_url,
        "DEIDENTIFICATION_KEY": "quality-scale-test-key-32-bytes-minimum-only",
        "DEIDENTIFICATION_KEY_ID": "quality-scale-v1",
        "AI_QUALITY_RESEARCH_RELEASE_MODE": "frozen_epoch",
        "AI_QUALITY_RESEARCH_MIN_SUBJECTS": "2",
    }
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _private_root(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    root.chmod(0o700)
    resolved = root.resolve()
    if (
        resolved == PLATFORM_ROOT
        or resolved.is_relative_to(PLATFORM_ROOT)
        or stat.S_IMODE(resolved.stat().st_mode) & 0o077
    ):
        raise ScaleAcceptanceError("scale_root_not_private_or_isolated")
    return resolved


def _source_identity_files() -> tuple[str, ...]:
    """Return the complete non-data source closure used by the scale run."""
    selected = set(_SOURCE_IDENTITY_EXACT_FILES)

    def fail_walk(error: OSError) -> None:
        raise ScaleAcceptanceError("source_identity_tree_unavailable") from error

    for relative_root, allowed_suffixes in _SOURCE_IDENTITY_TREES:
        tree_root = PLATFORM_ROOT / relative_root
        try:
            root_facts = tree_root.lstat()
        except OSError as exc:
            raise ScaleAcceptanceError(
                "source_identity_tree_unavailable") from exc
        if stat.S_ISLNK(root_facts.st_mode) or not stat.S_ISDIR(root_facts.st_mode):
            raise ScaleAcceptanceError("source_identity_tree_unsafe")
        for current_root, directory_names, file_names in os.walk(
                tree_root, followlinks=False, onerror=fail_walk):
            directory_names.sort()
            file_names.sort()
            current = Path(current_root)
            for directory_name in directory_names:
                directory = current / directory_name
                try:
                    directory_facts = directory.lstat()
                except OSError as exc:
                    raise ScaleAcceptanceError(
                        "source_identity_tree_unavailable") from exc
                if stat.S_ISLNK(directory_facts.st_mode):
                    raise ScaleAcceptanceError("source_identity_tree_unsafe")
            for file_name in file_names:
                path = current / file_name
                if allowed_suffixes is not None and path.suffix not in allowed_suffixes:
                    continue
                relative = path.relative_to(PLATFORM_ROOT).as_posix()
                if relative.startswith("data/") or "/data/" in relative:
                    raise ScaleAcceptanceError("source_identity_data_boundary")
                selected.add(relative)
    return tuple(sorted(selected))


def _selected_dynamic_tree_path(relative: str) -> bool:
    for relative_root, allowed_suffixes in _SOURCE_IDENTITY_TREES:
        prefix = f"{relative_root}/"
        if not relative.startswith(prefix):
            continue
        return allowed_suffixes is None or Path(relative).suffix in allowed_suffixes
    return False


def _head_source_identity_files() -> frozenset[str]:
    """Read the HEAD-side tree closure so pre-run deletions cannot disappear."""
    try:
        completed = subprocess.run(
            [
                "git", "ls-tree", "-r", "--name-only", "-z", "HEAD", "--",
                *[root for root, _suffixes in _SOURCE_IDENTITY_TREES],
            ],
            cwd=PLATFORM_ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        paths = (
            raw.decode("utf-8")
            for raw in completed.stdout.split(b"\0") if raw
        )
        return frozenset(path for path in paths if _selected_dynamic_tree_path(path))
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        raise ScaleAcceptanceError("source_revision_unavailable") from exc


def _source_identity() -> dict[str, Any]:
    """Bind a receipt to exact source bytes without traversing repository data."""
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=PLATFORM_ROOT,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=10,
        ).stdout.decode("ascii").strip()
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        raise ScaleAcceptanceError("source_revision_unavailable") from exc
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise ScaleAcceptanceError("source_revision_invalid")

    current_files = _source_identity_files()
    hashes: dict[str, str] = {}
    selected_files_dirty = bool(
        _head_source_identity_files() - set(current_files))
    for relative in current_files:
        path = PLATFORM_ROOT / relative
        try:
            facts = path.lstat()
            if not stat.S_ISREG(facts.st_mode) or stat.S_ISLNK(facts.st_mode):
                raise ScaleAcceptanceError("source_identity_file_unsafe")
            current = path.read_bytes()
        except OSError as exc:
            raise ScaleAcceptanceError("source_identity_file_unavailable") from exc
        hashes[relative] = hashlib.sha256(current).hexdigest()
        try:
            committed = subprocess.run(
                ["git", "show", f"HEAD:{relative}"], cwd=PLATFORM_ROOT,
                check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ScaleAcceptanceError("source_revision_unavailable") from exc
        if committed.returncode != 0 or committed.stdout != current:
            selected_files_dirty = True
    encoded_hashes = json.dumps(
        hashes, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "git_revision": revision,
        "selected_files_dirty": selected_files_dirty,
        "selected_file_sha256": hashes,
        "selected_source_tree_sha256": hashlib.sha256(encoded_hashes).hexdigest(),
    }


def _runtime_fingerprint() -> dict[str, Any]:
    import sqlalchemy

    return {
        "python": stdlib_platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "sqlalchemy": sqlalchemy.__version__,
        "os": {
            "system": stdlib_platform.system(),
            "release": stdlib_platform.release(),
            "machine": stdlib_platform.machine(),
        },
    }


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> Path:
    """Atomically create one no-overwrite 0600 receipt in a private directory."""
    target = Path(path)
    parent = target.parent
    try:
        parent_facts = parent.lstat()
    except OSError as exc:
        raise ScaleAcceptanceError("receipt_directory_unavailable") from exc
    if (stat.S_ISLNK(parent_facts.st_mode)
            or not stat.S_ISDIR(parent_facts.st_mode)
            or parent_facts.st_uid != os.getuid()
            or stat.S_IMODE(parent_facts.st_mode) & 0o077):
        raise ScaleAcceptanceError("receipt_directory_unsafe")
    body = (json.dumps(
        receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    pending = parent / f".{target.name}.{secrets.token_urlsafe(18)}.pending"
    fd: int | None = None
    try:
        fd = os.open(pending, flags, 0o600)
        os.fchmod(fd, 0o600)
        view = memoryview(body)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short receipt write")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.link(pending, target, follow_symlinks=False)
        pending.unlink()
        directory_fd = os.open(
            parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            pass
        # Once the hard link is published the target contains the complete,
        # fsynced payload.  A later directory-fsync/cleanup error is reported,
        # but the complete evidence must never be deleted or overwritten.
        raise ScaleAcceptanceError("receipt_write_failed") from exc
    finally:
        if fd is not None:
            os.close(fd)
    return target


def _migrate(database_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(PLATFORM_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PLATFORM_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def _insert(connection: Any, model: type, rows: list[dict[str, Any]]) -> None:
    if rows:
        connection.execute(model.__table__.insert(), rows)


@dataclass(frozen=True)
class SeedFacts:
    audio_bytes: int
    audio_files: int


def _seed_week2_contracts(engine: Any, audio_root: Path,
                          profile: ScaleProfile) -> SeedFacts:
    """Materialize valid Week-2 evidence; no quality projection is patched."""
    from app import content
    from app.models import (
        AttemptEvent,
        AudioAssetRow,
        AudioCaptureReceipt,
        InteractionEvent,
        ItemEvent,
        Patient,
        Session as TrainSession,
        SessionRuntimeState,
        TurnConfirmationRevision,
        TurnEvent,
    )

    bank = content.load_item_bank_for_week(2)
    bank_digest = content.item_bank_definition_digest(bank)
    protocol = content.load_autopilot_protocol(
        content.CONTENT_DIR / "autopilot_protocol_v1.json")
    protocol_digest = content.autopilot_protocol_definition_digest(protocol)
    from app import runtime

    plan = runtime.build_session_plan(bank, 2, "正式训练")
    if len(plan.items) != ITEMS_PER_WEEK2_SESSION:
        raise ScaleAcceptanceError("week2_item_contract_changed")
    if plan.total_turns() != TURNS_PER_WEEK2_SESSION:
        raise ScaleAcceptanceError("week2_turn_contract_changed")

    audio_root.mkdir(mode=0o700)
    patients = [{
        "patient_id": f"scale-p-{index:03d}",
        "is_simulation_subject": False,
        "dementia_severity": "mechanical-fixture",
        "mandarin_eligible": True,
        "consent_status": "已同意",
        "consent_type": "本人同意",
        "recording_allowed": True,
        "secondary_use_allowed": True,
        "governance_revision": 0,
    } for index in range(1, profile.subject_count + 1)]
    sessions: list[dict[str, Any]] = []
    states: list[dict[str, Any]] = []
    for session_index in range(1, profile.session_count + 1):
        session_id = f"scale-s-{session_index:04d}"
        patient_index = ((session_index - 1) % profile.subject_count) + 1
        sessions.append({
            "session_id": session_id,
            "patient_id": f"scale-p-{patient_index:03d}",
            "session_sitting_no": ((session_index - 1) // profile.subject_count) + 1,
            "training_date": date(2026, 7, 1),
            "week_no": 2,
            "phase_type": "正式训练",
            "event_line": "正式训练",
            "trainer_id": "SCALE-RESEARCHER",
            "item_bank_version_id": bank.version_id,
            "item_bank_definition_digest": bank_digest,
            "autopilot_protocol_version_id": protocol["protocol_version_id"],
            "autopilot_protocol_definition_digest": protocol_digest,
            "is_simulation": False,
            "data_classification": "research",
        })
        states.append({
            "session_id": session_id,
            "status": "completed",
            "revision": 1,
            "completed_at": SETTLED_AT,
            "updated_at": SETTLED_AT,
        })

    total_audio_bytes = 0
    total_audio_files = 0
    before_hash = hashlib.sha256(b"\x00NULL").hexdigest()
    answer = "week2-mechanical-answer"
    after_hash = hashlib.sha256(
        b"\x01TEXT" + answer.encode("utf-8")).hexdigest()

    with engine.begin() as connection:
        _insert(connection, Patient, patients)
        _insert(connection, TrainSession, sessions)
        _insert(connection, SessionRuntimeState, states)

        # A bounded batch keeps the full profile's seed memory independent of
        # the 158,400-row total while retaining genuine FK-linked rows.
        session_batch = 8
        for batch_start in range(1, profile.session_count + 1, session_batch):
            items: list[dict[str, Any]] = []
            audios: list[dict[str, Any]] = []
            receipts: list[dict[str, Any]] = []
            attempts: list[dict[str, Any]] = []
            turns: list[dict[str, Any]] = []
            revisions: list[dict[str, Any]] = []
            interactions: list[dict[str, Any]] = []
            batch_end = min(
                batch_start + session_batch, profile.session_count + 1)
            for session_index in range(batch_start, batch_end):
                session_id = f"scale-s-{session_index:04d}"
                turn_in_session = 0
                for item_position, item in enumerate(plan.items, start=1):
                    item_id = (
                        (session_index - 1) * ITEMS_PER_WEEK2_SESSION
                        + item_position
                    )
                    items.append({
                        "id": item_id,
                        "session_id": session_id,
                        "item_id": item.item_id,
                        "image_id": item.image_id,
                        "task_type": item.task_type,
                        "item_set_type": "训练集",
                        "presentation_order": item.presentation_order,
                    })
                    for planned_turn in item.turns:
                        turn_in_session += 1
                        global_turn = (
                            (session_index - 1) * TURNS_PER_WEEK2_SESSION
                            + turn_in_session
                        )
                        source_attempt_id = global_turn * 2
                        source_audio_id = f"aud-{source_attempt_id:08d}"
                        for attempt_seq in (1, 2):
                            attempt_id = global_turn * 2 - 2 + attempt_seq
                            raw_audio_id = f"aud-{attempt_id:08d}"
                            blob = b"\x1a\x45\xdf\xa3" + raw_audio_id.encode("ascii")
                            blob_path = audio_root / f"{raw_audio_id}.webm"
                            blob_path.write_bytes(blob)
                            checksum = hashlib.sha256(blob).hexdigest()
                            total_audio_files += 1
                            total_audio_bytes += len(blob)
                            audios.append({
                                "raw_audio_id": raw_audio_id,
                                "session_id": session_id,
                                "is_simulation": False,
                                "data_classification": "research",
                                "turn_key": (
                                    f"{item.item_id}#{planned_turn.turn_seq}"
                                ),
                                "audio_format": "webm",
                                "status": "recorded",
                                "withdrawn": False,
                                "checksum": checksum,
                                "byte_count": len(blob),
                                "uploaded_at": SETTLED_AT,
                                "contains_direct_identifier": False,
                            })
                            receipts.append({
                                "server_seq": attempt_id,
                                "raw_audio_id": raw_audio_id,
                                "session_id": session_id,
                                "turn_key": (
                                    f"{item.item_id}#{planned_turn.turn_seq}"
                                ),
                                "received_at": SETTLED_AT,
                                "duration_seconds": 1.0,
                                "byte_count": len(blob),
                                "checksum": checksum,
                                "data_classification": "research",
                                "is_simulation": False,
                                "contains_direct_identifier": False,
                            })
                            attempts.append({
                                "id": attempt_id,
                                "session_id": session_id,
                                "item_id": item.item_id,
                                "turn_seq": planned_turn.turn_seq,
                                "response_role": planned_turn.response_role,
                                "attempt_seq": attempt_seq,
                                "raw_audio_id": raw_audio_id,
                                "prompt_level": attempt_seq - 1,
                                "asr_text": answer,
                                "asr_confidence": 1.0,
                                "asr_engine_version": "scale-asr-v1",
                                "operational_answer_type": "正确",
                                "operational_score": 1.0,
                                "operational_needs_review": False,
                                "judge_mode": "规则确定式",
                                "judge_engine_version": "scale-judge-v1",
                                "judge_portrait_used": False,
                                "processing_status": "completed",
                                "processing_generation": 0,
                                "created_at": SETTLED_AT,
                                "processed_at": SETTLED_AT + timedelta(
                                    milliseconds=100 + attempt_seq),
                                "is_simulation": False,
                            })
                        turns.append({
                            "id": global_turn,
                            "item_event_id": item_id,
                            "source_attempt_id": source_attempt_id,
                            "turn_seq": planned_turn.turn_seq,
                            "response_role": planned_turn.response_role,
                            "raw_audio_id": source_audio_id,
                            "asr_text": answer,
                            "asr_confidence": 1.0,
                            "confirmed_response_text": answer,
                            "confirmation_revision": 1,
                            "prompt_level": 1,
                            "ai_answer_type": "正确",
                            "ai_score": 1.0,
                            "ai_needs_review": False,
                            "ai_judge_mode": "规则确定式",
                            "judge_portrait_used": False,
                            "reviewer_id": "SCALE-REVIEWER",
                            "reviewed_score": 1.0,
                            "score_locked": True,
                            "element_value": 1.0,
                        })
                        revisions.append({
                            "id": global_turn,
                            "turn_id": global_turn,
                            "session_id": session_id,
                            "revision": 1,
                            "expected_revision": 0,
                            "actor_display_id": "SCALE-REVIEWER",
                            "changed_at": SETTLED_AT,
                            "before_sha256": before_hash,
                            "after_sha256": after_hash,
                            "idempotency_key": f"scale-rev-{global_turn:08d}",
                        })
                        interactions.append({
                            "id": global_turn,
                            "session_id": session_id,
                            "event_seq": turn_in_session,
                            "item_id": item.item_id,
                            "turn_seq": planned_turn.turn_seq,
                            "attempt_id": source_attempt_id,
                            "attempt_seq": 2,
                            "event_type": "judgement_completed",
                            "payload_json": "{}",
                            "created_at": SETTLED_AT,
                            "is_simulation": False,
                        })
            _insert(connection, ItemEvent, items)
            _insert(connection, AudioAssetRow, audios)
            _insert(connection, AudioCaptureReceipt, receipts)
            _insert(connection, AttemptEvent, attempts)
            _insert(connection, TurnEvent, turns)
            _insert(connection, TurnConfirmationRevision, revisions)
            _insert(connection, InteractionEvent, interactions)

    if total_audio_files != profile.expected_audio_files:
        raise ScaleAcceptanceError("seed_audio_count_mismatch")
    return SeedFacts(total_audio_bytes, total_audio_files)


def _database_evidence_counts(engine: Any) -> dict[str, int]:
    from sqlalchemy import func
    from sqlmodel import Session as DBSession, select

    from app.models import (
        AttemptEvent,
        AudioAssetRow,
        AudioCaptureReceipt,
        AutopilotControlEvent,
        InteractionEvent,
        ItemEvent,
        TechnicalPauseReceipt,
        TurnConfirmationRevision,
        TurnEvent,
    )

    models = {
        "items": ItemEvent,
        "turns": TurnEvent,
        "attempts": AttemptEvent,
        "audios": AudioAssetRow,
        "receipts": AudioCaptureReceipt,
        "interactions": InteractionEvent,
        "revisions": TurnConfirmationRevision,
        "pause_receipts": TechnicalPauseReceipt,
        "control_events": AutopilotControlEvent,
    }
    with DBSession(engine) as session:
        return {
            key: int(session.exec(
                select(func.count()).select_from(model)).one() or 0)
            for key, model in models.items()
        }


@dataclass
class _IoFacts:
    audio_directory_scans: int = 0
    audio_hashed_files: int = 0
    audio_hashed_bytes: int = 0


@contextmanager
def _instrument_quality_io(audio_root: Path) -> Iterator[_IoFacts]:
    from app import audio_store

    facts = _IoFacts()
    prior_root = audio_store.AUDIO_DIR
    prior_index = audio_store.index_blobs
    prior_hash = audio_store.sha256_file

    def counted_index(raw_audio_ids: Any) -> dict[str, Path]:
        facts.audio_directory_scans += 1
        return prior_index(raw_audio_ids)

    def counted_hash(path: Path, *, chunk_size: int = 1024 * 1024,
                     max_bytes: int | None = None) -> tuple[str, int]:
        digest, byte_count = prior_hash(
            path, chunk_size=chunk_size, max_bytes=max_bytes)
        facts.audio_hashed_files += 1
        facts.audio_hashed_bytes += byte_count
        return digest, byte_count

    audio_store.AUDIO_DIR = audio_root
    audio_store.index_blobs = counted_index
    audio_store.sha256_file = counted_hash
    try:
        yield facts
    finally:
        audio_store.AUDIO_DIR = prior_root
        audio_store.index_blobs = prior_index
        audio_store.sha256_file = prior_hash


def _peak_rss_mb() -> float:
    raw = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # Darwin reports bytes; Linux and the CI containers report KiB.
    divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return raw / divisor


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _read_frozen_turns_twice(
    session: Any, *, binding: Any, config: Any, page_size: int,
) -> tuple[int, int, bool, bool]:
    from app import research_read

    def read_once() -> tuple[list[bytes], list[tuple[str, str, int]]]:
        cursor: str | None = None
        pages: list[bytes] = []
        keys: list[tuple[str, str, int]] = []
        while True:
            payload = research_read.list_turns(
                session,
                config=config,
                data_classification="research",
                cursor=cursor,
                limit=page_size,
                binding=binding,
            )
            pages.append(_json_bytes(payload))
            for row in payload["rows"]:
                keys.append((
                    row["session_code"], row["item_id"], row["turn_seq"]
                ))
            cursor = payload["next_cursor"]
            if cursor is None:
                if payload["has_more"] is not False:
                    raise ScaleAcceptanceError("turn_last_page_invalid")
                break
            if payload["has_more"] is not True:
                raise ScaleAcceptanceError("turn_intermediate_page_invalid")
        return pages, keys

    first_pages, first_keys = read_once()
    second_pages, second_keys = read_once()
    if len(set(first_keys)) != len(first_keys):
        raise ScaleAcceptanceError("turn_pagination_duplicate")
    if first_keys != second_keys:
        raise ScaleAcceptanceError("turn_pagination_not_repeatable")
    # The deterministic cursor is inside each page body, so exact page bytes
    # prove both cursor and body determinism.
    body_deterministic = first_pages == second_pages
    cursor_deterministic = body_deterministic
    return (
        len(first_pages), len(first_keys),
        cursor_deterministic, body_deterministic,
    )


def _run_in_root(root: Path, profile: ScaleProfile) -> dict[str, Any]:
    from sqlalchemy import event, func
    from sqlmodel import Session as DBSession, select

    database_path = root / "quality-scale.db"
    database_url = f"sqlite:///{database_path}"
    audio_root = root / "audio"
    started = time.perf_counter()
    run_at_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    source_identity = _source_identity()
    runtime_fingerprint = _runtime_fingerprint()
    with _environment(database_url):
        _migrate(database_url)
        from app import (
            export_security,
            quality_release,
            research_dataset,
        )
        from app.db import make_engine
        from app.models import QualityReleaseEpochRowSnapshot

        engine = make_engine(database_url)
        select_count = 0

        def count_selects(
            _connection: Any, _cursor: Any, statement: str,
            _parameters: Any, _context: Any, _executemany: bool,
        ) -> None:
            nonlocal select_count
            if statement.lstrip().upper().startswith("SELECT"):
                select_count += 1

        event.listen(engine, "before_cursor_execute", count_selects)
        try:
            seed = _seed_week2_contracts(engine, audio_root, profile)
            evidence_counts = _database_evidence_counts(engine)
            evidence_total = sum(evidence_counts.values())
            if evidence_total != profile.expected_evidence_rows:
                raise ScaleAcceptanceError("database_evidence_total_mismatch")

            min_subjects = min(5, profile.subject_count)
            thresholds = quality_release.ReleaseThresholds(
                min_subjects=min_subjects,
                min_cell_subjects=min_subjects,
                band_width=10,
                rate_decimals=2,
                entry_quarantine_days=14,
            )
            config = export_security.load_deidentification_config()
            with _instrument_quality_io(audio_root) as io_facts:
                with DBSession(engine) as session:
                    quality_release.begin_release_transaction(
                        session, writable=True)
                    cohort = quality_release.derive_cohort(
                        session,
                        as_of=AS_OF,
                        quarantine_days=thresholds.entry_quarantine_days,
                    )
                    if len(cohort) != profile.session_count:
                        raise ScaleAcceptanceError("derived_cohort_count_mismatch")
                    if len({row.patient_id for row in cohort}) != profile.subject_count:
                        raise ScaleAcceptanceError("derived_subject_count_mismatch")
                    if {row.week_no for row in cohort} != {2}:
                        raise ScaleAcceptanceError("derived_contract_not_week2_only")
                    payload, watermarks = quality_release.build_payload(
                        session, cohort, as_of=AS_OF, thresholds=thresholds)
                    if (
                        len(watermarks) != profile.session_count
                        or set(watermarks.values())
                        != {EVIDENCE_ROWS_PER_SESSION}
                    ):
                        raise ScaleAcceptanceError("evidence_watermark_mismatch")
                    snapshot = quality_release.build_research_snapshot(
                        session,
                        session_ids=tuple(watermarks),
                        config=config,
                    )
                    manifest = json.loads(snapshot.manifest_json)
                    proposal = quality_release.proposal_digest(
                        payload,
                        watermarks,
                        as_of=AS_OF,
                        config=config,
                        thresholds=thresholds,
                        builder=("SCALE-BUILDER", "data_steward"),
                        research_snapshot_sha256=snapshot.snapshot_sha256,
                    )
                    epoch = quality_release.publish_epoch(
                        session,
                        payload=payload,
                        watermarks=watermarks,
                        research_snapshot=snapshot,
                        proposal_sha256=proposal,
                        as_of=AS_OF,
                        thresholds=thresholds,
                        builder=("SCALE-BUILDER", "data_steward"),
                        approver=("SCALE-APPROVER", "admin"),
                        idempotency_key=f"quality-scale-{profile.name}-v1",
                        now=AS_OF,
                    )
                    epoch_id = epoch.epoch_id
                    session.commit()

                # 这套 harness 只造训练侧的数据，不造量表记录——所以量表那两张表
                # 期望是 0 行。写成「注册表里其余的都期望 0」，加数据集时不用再改这里，
                # 而一旦某天 harness 真开始造量表数据，它会立刻红出来。
                expected_snapshot = {
                    key: 0 for key in research_dataset.dataset_keys()}
                expected_snapshot.update({
                    "subjects": profile.subject_count,
                    "sessions": profile.session_count,
                    "turns": profile.expected_turn_rows,
                })
                snapshot_counts = {
                    key: manifest["datasets"][key]["row_count"]
                    for key in research_dataset.dataset_keys()
                }
                if snapshot_counts != expected_snapshot:
                    raise ScaleAcceptanceError("snapshot_manifest_count_mismatch")

                with DBSession(engine) as session:
                    persisted_snapshot_rows = int(session.exec(
                        select(func.count())
                        .select_from(QualityReleaseEpochRowSnapshot)
                        .where(QualityReleaseEpochRowSnapshot.epoch_id == epoch_id)
                    ).one() or 0)
                    binding = quality_release.bind_research_read(
                        session, config=config)
                    (
                        turn_page_count,
                        unique_turn_rows,
                        cursor_deterministic,
                        body_deterministic,
                    ) = _read_frozen_turns_twice(
                        session,
                        binding=binding,
                        config=config,
                        page_size=profile.turn_page_size,
                    )

            if io_facts.audio_directory_scans != 1:
                raise ScaleAcceptanceError("audio_directory_scan_count_mismatch")
            if io_facts.audio_hashed_files != profile.expected_audio_files:
                raise ScaleAcceptanceError("audio_hash_file_count_mismatch")
            if io_facts.audio_hashed_bytes != seed.audio_bytes:
                raise ScaleAcceptanceError("audio_hash_byte_count_mismatch")
            if persisted_snapshot_rows != sum(expected_snapshot.values()):
                raise ScaleAcceptanceError("persisted_snapshot_count_mismatch")
            if unique_turn_rows != profile.expected_turn_rows:
                raise ScaleAcceptanceError("turn_pagination_missing_rows")
            if turn_page_count != profile.expected_turn_pages:
                raise ScaleAcceptanceError("turn_page_count_mismatch")
            if not cursor_deterministic or not body_deterministic:
                raise ScaleAcceptanceError("frozen_turn_pages_not_deterministic")

            retry_count, retry_delays = _exercise_http_429_contract()
            wall_seconds = time.perf_counter() - started
            peak_rss_mb = _peak_rss_mb()
            if wall_seconds > profile.max_wall_seconds:
                raise ScaleAcceptanceError("scale_wall_time_exceeded")
            if peak_rss_mb > profile.max_peak_rss_mb:
                raise ScaleAcceptanceError("scale_peak_rss_exceeded")
            if select_count <= 0 or select_count > profile.session_count * 40 + 250:
                raise ScaleAcceptanceError("sql_select_budget_exceeded")
            if _source_identity() != source_identity:
                raise ScaleAcceptanceError("source_changed_during_acceptance")

            return {
                "schema_version": "quality-release-scale-receipt.v2",
                "status": "passed",
                "run_at_utc": run_at_utc,
                "source_identity": source_identity,
                "runtime_fingerprint": runtime_fingerprint,
                "profile": asdict(profile),
                "scope": {
                    "label": "mechanical_capacity_repeated_valid_week2_contracts",
                    "week2_contract_repetitions": profile.session_count,
                    "eight_week_content_complete": False,
                    "clinical_study_readiness_claimed": False,
                    "database": "temporary_migrated_sqlite",
                    "audio_root": "temporary_private_directory",
                    "repository_data_touched": False,
                    "representative_audio_volume": False,
                },
                "cohort": {
                    "subjects": profile.subject_count,
                    "sessions": profile.session_count,
                    "week_numbers": [2],
                    "items_per_session": ITEMS_PER_WEEK2_SESSION,
                    "turns_per_session": TURNS_PER_WEEK2_SESSION,
                },
                "evidence": {
                    "rows_per_session": EVIDENCE_ROWS_PER_SESSION,
                    "total_rows": evidence_total,
                    "database_counts": evidence_counts,
                    "watermark_rows_per_session": EVIDENCE_ROWS_PER_SESSION,
                },
                "snapshot": {
                    **snapshot_counts,
                    "persisted_rows": persisted_snapshot_rows,
                    "sha256": snapshot.snapshot_sha256,
                },
                "io": {
                    "sql_select_count": select_count,
                    "audio_directory_scans": io_facts.audio_directory_scans,
                    "audio_hashed_files": io_facts.audio_hashed_files,
                    "audio_hashed_bytes": io_facts.audio_hashed_bytes,
                    "average_audio_bytes_per_file": round(
                        io_facts.audio_hashed_bytes / io_facts.audio_hashed_files,
                        3,
                    ),
                },
                "frozen_turn_pagination": {
                    "page_size": profile.turn_page_size,
                    "page_count": turn_page_count,
                    "unique_rows": unique_turn_rows,
                    "missing_rows": 0,
                    "duplicate_rows": 0,
                    "cursor_deterministic": cursor_deterministic,
                    "body_deterministic": body_deterministic,
                },
                "http_429_client": {
                    "tested_without_server": True,
                    "retry_count": retry_count,
                    "retry_delays_seconds": list(retry_delays),
                    "same_cursor_retried_before_advance": True,
                },
                "resources": {
                    "wall_seconds": round(wall_seconds, 3),
                    "peak_rss_mb": round(peak_rss_mb, 3),
                },
            }
        finally:
            event.remove(engine, "before_cursor_execute", count_selects)
            engine.dispose()


def run_profile(profile: ScaleProfile = FULL_PROFILE, *,
                root: Path | None = None) -> dict[str, Any]:
    """Run one acceptance profile and return its machine-readable receipt."""
    if profile.subject_count < 2 or profile.session_count < profile.subject_count:
        raise ScaleAcceptanceError("profile_cardinality_invalid")
    if profile.session_count % profile.subject_count != 0:
        raise ScaleAcceptanceError("profile_sessions_must_evenly_repeat_subjects")
    if profile.turn_page_size < 1 or profile.turn_page_size > 1000:
        raise ScaleAcceptanceError("profile_page_size_invalid")
    if root is not None:
        return _run_in_root(_private_root(Path(root)), profile)
    with tempfile.TemporaryDirectory(prefix="nmu-quality-scale-") as raw_root:
        resolved = Path(raw_root).resolve()
        resolved.chmod(0o700)
        if resolved.is_relative_to(PLATFORM_ROOT):
            raise ScaleAcceptanceError("temporary_root_inside_repository")
        return _run_in_root(resolved, profile)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "冻结质量发布的隔离机械容量验收；full 只代表 240 个"
            "重复合法 Week2 合同，不代表八周内容完整。"
        ))
    parser.add_argument(
        "--profile", choices=tuple(PROFILES), default="full")
    parser.add_argument(
        "--receipt",
        help="把成功回执原子写入已存在的私有目录（0600、不覆盖）",
    )
    args = parser.parse_args(argv)
    if args.profile == "full" and not args.receipt:
        parser.error("--profile full 必须同时给出 --receipt 留存可追溯回执")
    try:
        receipt = run_profile(PROFILES[args.profile])
        if (args.profile == "full"
                and receipt["source_identity"]["selected_files_dirty"]):
            raise ScaleAcceptanceError("source_identity_dirty")
        receipt_path = (
            _write_receipt(Path(args.receipt), receipt)
            if args.receipt else None
        )
    except Exception as exc:
        code = (
            str(exc) if isinstance(exc, ScaleAcceptanceError)
            else type(exc).__name__
        )
        print(json.dumps({
            "schema_version": "quality-release-scale-receipt.v2",
            "status": "failed",
            "profile": args.profile,
            "code": code,
            "eight_week_content_complete": False,
        }, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    if receipt_path is not None:
        print(f"回执已写入 {receipt_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
