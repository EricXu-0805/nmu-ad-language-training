#!/usr/bin/env python3
"""Fail closed unless the selected database is at the image's sole head."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import quote

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_URL = f"sqlite:///{ROOT / 'data' / 'app.db'}"


class DatabaseHeadError(RuntimeError):
    """A stable rejection that never includes a URL, path, or database row."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def expected_head(root: Path = ROOT) -> str:
    try:
        config = Config(str(root / "alembic.ini"))
        heads = tuple(ScriptDirectory.from_config(config).get_heads())
    except Exception as exc:
        raise DatabaseHeadError("migration_graph_unreadable") from exc
    if len(heads) != 1 or not heads[0]:
        raise DatabaseHeadError("migration_head_ambiguous")
    return str(heads[0])


def _safe_sqlite_path(url: URL) -> Path:
    database = url.database
    if (not database or database == ":memory:" or database.startswith("file:")
            or str(url.query.get("mode", "")).lower() == "memory"):
        raise DatabaseHeadError("database_url_unsupported")
    candidate = Path(database)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.absolute()
    cursor = Path(candidate.anchor)
    try:
        for part in candidate.parts[1:]:
            cursor = cursor / part
            if cursor.is_symlink():
                raise DatabaseHeadError("database_file_invalid")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            raise DatabaseHeadError("database_file_invalid")
    except FileNotFoundError as exc:
        raise DatabaseHeadError("database_missing") from exc
    except OSError as exc:
        raise DatabaseHeadError("database_file_invalid") from exc
    return resolved


def _sqlite_revisions(url: URL) -> tuple[str, ...]:
    path = _safe_sqlite_path(url)
    encoded = quote(str(path), safe="/")
    try:
        connection = sqlite3.connect(
            f"file:{encoded}?mode=ro", uri=True, timeout=30)
        try:
            connection.execute("PRAGMA query_only=ON")
            rows = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT version_num FROM alembic_version")
            )
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise DatabaseHeadError("database_revision_unreadable") from exc
    return rows


def _server_revisions(url: URL) -> tuple[str, ...]:
    engine = None
    try:
        engine = create_engine(
            url, echo=False, hide_parameters=True, poolclass=NullPool)
        with engine.connect() as connection:
            return tuple(
                str(row[0])
                for row in connection.execute(
                    text("SELECT version_num FROM alembic_version"))
            )
    except Exception as exc:
        raise DatabaseHeadError("database_revision_unreadable") from exc
    finally:
        if engine is not None:
            engine.dispose()


def current_revisions(database_url: str) -> tuple[str, ...]:
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise DatabaseHeadError("database_url_invalid") from exc
    if url.get_backend_name() == "sqlite":
        return _sqlite_revisions(url)
    return _server_revisions(url)


def assert_database_at_head(
        database_url: str | None = None, *, root: Path = ROOT) -> str:
    expected = expected_head(root)
    revisions = current_revisions(database_url or DEFAULT_DATABASE_URL)
    if not revisions:
        raise DatabaseHeadError("database_revision_missing")
    if len(revisions) != 1 or not revisions[0]:
        raise DatabaseHeadError("database_revision_ambiguous")
    if revisions[0] != expected:
        raise DatabaseHeadError("database_revision_not_head")
    return expected


def main() -> int:
    try:
        assert_database_at_head(os.environ.get("DATABASE_URL"))
    except DatabaseHeadError as exc:
        print(f"REJECTED code={exc.code}", file=sys.stderr, flush=True)
        return 78
    print("OK database_at_head", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
