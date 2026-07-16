from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.db import make_engine


def test_sqlite_engine_enables_foreign_keys_and_busy_timeout():
    eng = make_engine("sqlite://")
    with eng.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000


def test_alembic_uses_database_url_when_ini_is_blank(tmp_path, monkeypatch):
    """启动脚本的 alembic 与应用必须连同一个部署库。"""
    db_path = tmp_path / "deployment.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))

    command.upgrade(config, "head")

    assert db_path.exists()
    eng = make_engine(f"sqlite:///{db_path}")
    tables = set(inspect(eng).get_table_names())
    assert {"alembic_version", "livestate", "sessionruntimestate"} <= tables
