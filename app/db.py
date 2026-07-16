"""数据库接线（本地 SQLite 开发 / PostgreSQL 部署）。"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

_DATA = Path(__file__).resolve().parent.parent / "data"
_DATA.mkdir(exist_ok=True)
DEFAULT_URL = f"sqlite:///{_DATA / 'app.db'}"


def make_engine(url: str | None = None):
    """创建与部署 URL 匹配的引擎；SQLite 专用参数不得误传给 PostgreSQL。"""
    resolved = url or os.environ.get("DATABASE_URL") or DEFAULT_URL
    kwargs = {"echo": False}
    if resolved.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    eng = create_engine(resolved, **kwargs)
    if resolved.startswith("sqlite"):
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
