"""Shared transaction fences for subject governance and bedside work.

Row locks protect rows that already exist, but they cannot protect a predicate
such as "all sessions for this subject" or "no open work for this actor".  A
PostgreSQL transaction advisory lock supplies that missing, cross-worker fence.
The process locks are retained for SQLite/local development, where advisory
locks do not exist, and for callers that still publish exports in-process.

Every operation that needs both identities must acquire them in this order:
subject, then actor.  The caller must keep the context open through ``commit``
or ``rollback``; PostgreSQL releases transaction advisory locks at that point.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import threading
from typing import Iterator, Protocol
import unicodedata

from sqlalchemy import text


class _TransactionSession(Protocol):
    """Small SQLAlchemy surface used here; keeps this module ORM-agnostic."""

    def get_bind(self): ...

    def execute(self, statement, params=None): ...

    def rollback(self): ...


SUBJECT_EXPORT_WRITE_LOCK = threading.RLock()
ACTOR_WORK_WRITE_LOCK = threading.RLock()

_ADVISORY_KEY_DOMAIN = "nmu-governance-fence-v1"


def stable_advisory_key(namespace: str, identity: str) -> int:
    """Return a deterministic signed int64 key namespaced for PostgreSQL.

    Python's built-in ``hash`` is deliberately process-randomized and therefore
    cannot coordinate workers.  SHA-256 plus an explicit versioned domain gives
    stable keys while keeping raw subject/actor identifiers out of SQL logs.
    """
    normalized_namespace = unicodedata.normalize("NFC", str(namespace))
    normalized_identity = unicodedata.normalize("NFC", str(identity))
    if not normalized_namespace or not normalized_identity:
        raise ValueError("advisory fence namespace and identity must be non-empty")
    payload = "\x00".join((
        _ADVISORY_KEY_DOMAIN,
        normalized_namespace,
        normalized_identity,
    )).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def acquire_transaction_fence(
        db: _TransactionSession, *, namespace: str, identity: str) -> int | None:
    """Acquire one PostgreSQL transaction advisory lock, or no-op elsewhere."""
    bind = db.get_bind()
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect_name != "postgresql":
        return None
    lock_key = stable_advisory_key(namespace, identity)
    db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": lock_key},
    )
    return lock_key


@contextmanager
def subject_fence(
        db: _TransactionSession, patient_id: str) -> Iterator[None]:
    """Serialize every subject terminal/admission write through commit."""
    with SUBJECT_EXPORT_WRITE_LOCK:
        acquire_transaction_fence(
            db, namespace="subject", identity=patient_id)
        try:
            yield
        except BaseException:
            # SQLite's authority is the process lock, so rollback must happen
            # before that lock is released. PostgreSQL also releases the xact
            # advisory lock here instead of leaving it to response cleanup.
            db.rollback()
            raise


@contextmanager
def actor_work_fence(
        db: _TransactionSession, actor_id: str) -> Iterator[None]:
    """Serialize the actor-wide active-work predicate through commit."""
    with ACTOR_WORK_WRITE_LOCK:
        acquire_transaction_fence(
            db, namespace="actor-work", identity=actor_id)
        try:
            yield
        except BaseException:
            db.rollback()
            raise


@contextmanager
def subject_actor_fence(
        db: _TransactionSession, patient_id: str,
        actor_id: str) -> Iterator[None]:
    """Acquire the canonical subject -> actor transaction fence order."""
    with SUBJECT_EXPORT_WRITE_LOCK:
        acquire_transaction_fence(
            db, namespace="subject", identity=patient_id)
        with ACTOR_WORK_WRITE_LOCK:
            acquire_transaction_fence(
                db, namespace="actor-work", identity=actor_id)
            try:
                yield
            except BaseException:
                db.rollback()
                raise
