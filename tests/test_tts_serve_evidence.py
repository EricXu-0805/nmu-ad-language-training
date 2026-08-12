"""TtsServeEvidence: append-only server TTS usage ledger + its migration."""
from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, SQLModel

from app.models import TtsServeEvidence


PARENT = "f9b2d6e4a801"
HEAD = "b3e7c5a9d214"


def _config(db_path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def test_tts_serve_migration_is_single_head_and_roundtrips(tmp_path):
    db_path = tmp_path / "tts-serve-clean.sqlite"
    config = _config(db_path)
    assert ScriptDirectory.from_config(config).get_heads() == [HEAD]
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == HEAD
    assert "ttsserveevidence" in inspect(engine).get_table_names()
    command.check(config)

    command.downgrade(config, PARENT)
    assert "ttsserveevidence" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == HEAD


def _memory_session() -> Session:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _serve_row(**overrides) -> TtsServeEvidence:
    base = dict(
        session_id=None,
        command_id=None,
        source="live_speak",
        engine_version="dashscope/qwen3-tts-flash/Serena",
        cache_hit=False,
        result="served",
        byte_count=2048,
        text_sha256="a" * 64,
        is_simulation=True,
    )
    base.update(overrides)
    return TtsServeEvidence(**base)


def test_tts_serve_evidence_is_append_only():
    with _memory_session() as session:
        row = _serve_row()
        session.add(row)
        session.commit()

        row.engine_version = "piper/rewritten"
        with pytest.raises(RuntimeError, match="只追加使用证据"):
            session.commit()
        session.rollback()

        session.delete(row)
        with pytest.raises(RuntimeError, match="只追加使用证据"):
            session.commit()
        session.rollback()


def test_tts_serve_evidence_rejects_inconsistent_rows():
    with _memory_session() as session:
        session.add(_serve_row(result="degraded", byte_count=2048))
        with pytest.raises(Exception):
            session.commit()
        session.rollback()

        session.add(_serve_row(source="autopilot_command", command_id=None))
        with pytest.raises(Exception):
            session.commit()
        session.rollback()

        session.add(_serve_row(result="degraded", byte_count=None))
        session.commit()
