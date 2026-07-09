"""数据库接线（本地 SQLite 开发 / PostgreSQL 部署）。"""
from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

_DATA = Path(__file__).resolve().parent.parent / "data"
_DATA.mkdir(exist_ok=True)
DEFAULT_URL = f"sqlite:///{_DATA / 'app.db'}"


def make_engine(url: str = DEFAULT_URL):
    return create_engine(url, echo=False, connect_args={"check_same_thread": False})


engine = make_engine()


def init_db(eng=None) -> None:
    from . import models  # noqa: F401  —— 注册所有表
    SQLModel.metadata.create_all(eng or engine)


def get_session():
    with Session(engine) as s:
        yield s
