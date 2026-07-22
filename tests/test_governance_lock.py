from __future__ import annotations

import threading

import pytest

from app import governance_lock


class _Dialect:
    def __init__(self, name: str):
        self.name = name


class _Bind:
    def __init__(self, name: str):
        self.dialect = _Dialect(name)


class _FakeSession:
    def __init__(self, dialect_name: str):
        self.bind = _Bind(dialect_name)
        self.executed: list[tuple[str, dict[str, int]]] = []
        self.rollbacks = 0

    def get_bind(self):
        return self.bind

    def execute(self, statement, params=None):
        self.executed.append((str(statement), dict(params or {})))

    def rollback(self):
        self.rollbacks += 1


def test_stable_advisory_key_is_process_stable_namespaced_signed_int64():
    first = governance_lock.stable_advisory_key("subject", "P-001")
    assert first == governance_lock.stable_advisory_key("subject", "P-001")
    assert first != governance_lock.stable_advisory_key("actor-work", "P-001")
    assert first != governance_lock.stable_advisory_key("subject", "P-002")
    assert -(2 ** 63) <= first < 2 ** 63

    # Canonically equivalent Unicode must identify the same governance scope.
    assert governance_lock.stable_advisory_key("subject", "e\u0301") == (
        governance_lock.stable_advisory_key("subject", "\u00e9"))


def test_transaction_fence_uses_postgres_advisory_xact_lock_only_on_postgres():
    postgres = _FakeSession("postgresql")
    key = governance_lock.acquire_transaction_fence(
        postgres, namespace="subject", identity="P-001")
    assert key == governance_lock.stable_advisory_key("subject", "P-001")
    assert postgres.executed == [(
        "SELECT pg_advisory_xact_lock(:lock_key)",
        {"lock_key": key},
    )]

    sqlite = _FakeSession("sqlite")
    assert governance_lock.acquire_transaction_fence(
        sqlite, namespace="subject", identity="P-001") is None
    assert sqlite.executed == []


def test_subject_actor_fence_has_one_canonical_acquisition_order(monkeypatch):
    observed: list[tuple[str, str]] = []

    def record(_db, *, namespace: str, identity: str):
        observed.append((namespace, identity))
        return None

    monkeypatch.setattr(governance_lock, "acquire_transaction_fence", record)
    with governance_lock.subject_actor_fence(
            _FakeSession("sqlite"), "P-001", "researcher-1"):
        observed.append(("body", "entered"))
    assert observed == [
        ("subject", "P-001"),
        ("actor-work", "researcher-1"),
        ("body", "entered"),
    ]


def test_subject_fence_serializes_sqlite_process_writers():
    session = _FakeSession("sqlite")
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first_writer():
        with governance_lock.subject_fence(session, "P-001"):
            first_entered.set()
            assert release_first.wait(timeout=2)

    def second_writer():
        assert first_entered.wait(timeout=2)
        with governance_lock.subject_fence(session, "P-001"):
            second_entered.set()

    first = threading.Thread(target=first_writer)
    second = threading.Thread(target=second_writer)
    first.start()
    second.start()
    assert first_entered.wait(timeout=2)
    assert not second_entered.wait(timeout=0.05)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()
    assert second_entered.is_set()


def test_subject_fence_rolls_back_before_propagating_failure():
    session = _FakeSession("sqlite")
    with pytest.raises(RuntimeError, match="injected"):
        with governance_lock.subject_fence(session, "P-001"):
            raise RuntimeError("injected")
    assert session.rollbacks == 1
