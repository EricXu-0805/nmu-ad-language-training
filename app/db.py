"""数据库接线（本地 SQLite 开发 / PostgreSQL 部署）。"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote

from sqlalchemy import event
from sqlalchemy.engine import URL, make_url
from sqlmodel import Session, SQLModel, create_engine

from .storage_security import (ensure_private_directory, ensure_private_file,
                               install_private_umask)

# 本进程创建的数据库、录音、导出和临时证据默认仅运行账号可读写。
# 不依赖启动 shell 的 umask；共享宿主上的 0644/0755 会直接暴露声纹和回答文本。
install_private_umask()
_DATA = Path(__file__).resolve().parent.parent / "data"
_DEFAULT_DB_FILE = _DATA / "app.db"
DEFAULT_URL = f"sqlite:///{_DEFAULT_DB_FILE}"


def _sqlite_file_path(url: URL) -> Path | None:
    """Return the real filesystem target for a file-backed SQLite URL.

    In-memory SQLite URLs have no storage target.  URI-mode SQLite keeps the
    filename behind a ``file:`` prefix; ``mode=memory`` remains non-file-backed.
    Other database backends must never be interpreted as local paths.
    """
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database
    if not database or database == ":memory:":
        return None
    uri_mode = str(url.query.get("uri", "")).strip().lower() in {
        "1", "true", "yes",
    }
    if uri_mode and database.startswith("file:"):
        if (str(url.query.get("mode", "")).strip().lower() == "memory"
                or database.startswith("file::memory:")):
            return None
        database = unquote(database.removeprefix("file:"))
        if not database:
            return None
    return Path(database)


def _secure_sqlite_storage(path: Path) -> None:
    """Secure only the selected SQLite file, never the unused default path.

    The process umask makes newly-created SQLite files owner-only.  Existing
    files are tightened before SQLAlchemy can connect.  Missing dedicated
    parent directories are created privately; pre-existing shared parents such
    as ``/tmp`` are deliberately not chmod'ed.
    """
    parent = path.parent
    if path == _DEFAULT_DB_FILE:
        # Preserve the historical default: platform/data is an application-owned
        # research-data directory and should itself remain owner-only.
        ensure_private_directory(parent)
    elif not parent.exists():
        ensure_private_directory(parent)
    elif parent.is_symlink():
        raise RuntimeError(f"数据库目录不得是符号链接: {parent}")
    ensure_private_file(path)


def make_engine(url: str | None = None):
    """创建与部署 URL 匹配的引擎；SQLite 专用参数不得误传给 PostgreSQL。"""
    resolved = url or os.environ.get("DATABASE_URL") or DEFAULT_URL
    parsed = make_url(resolved)
    sqlite_path = _sqlite_file_path(parsed)
    if sqlite_path is not None:
        _secure_sqlite_storage(sqlite_path)
    # SQLAlchemy exception strings otherwise include bound values.  Those values
    # can contain subject identifiers or response text and may reach Uvicorn's
    # error log on an unexpected database failure.
    kwargs = {"echo": False, "hide_parameters": True}
    if parsed.get_backend_name() == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False}
    eng = create_engine(resolved, **kwargs)
    if parsed.get_backend_name() == "sqlite":
        @event.listens_for(eng, "connect")
        def _sqlite_integrity(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
    return eng


engine = make_engine()


def init_db(eng=None) -> None:
    from . import models  # noqa: F401  —— 注册所有表
    SQLModel.metadata.create_all(eng or engine)


def get_session():
    with Session(engine) as s:
        yield s
