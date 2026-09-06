"""Generation migration preserves history and rejects ambiguous automatic replies."""
import sqlite3

from alembic import command
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from test_rapport_utterance_migration import (
    _config, _insert_utterance, _recovery_guard, _schema_rows,
)

HEAD = "e2a6d8f0b419"
PARENT = "d0c22a6dae2a"


def test_generation_upgrade_roundtrip_matches_current_recovery_contract(tmp_path):
    db = tmp_path / "app.db"
    config = _config(db)
    command.upgrade(config, HEAD)
    guard = _recovery_guard()
    for _ in range(2):
        with sqlite3.connect(db) as conn:
            assert guard._schema_contract_fingerprint(conn) == guard.CURRENT_RECOVERY_SCHEMA_SHA256
        command.downgrade(config, PARENT)
        assert "rapportplaybackreceipt" not in inspect(create_engine(f"sqlite:///{db}")).get_table_names()
        command.upgrade(config, HEAD)


def test_duplicate_history_blocks_upgrade_before_any_schema_change(tmp_path):
    db = tmp_path / "app.db"
    config = _config(db)
    command.upgrade(config, PARENT)
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        _insert_utterance(conn, event_seq=1)
        _insert_utterance(conn, event_seq=2)
        conn.execute(text("UPDATE rapportutteranceevent SET origin='auto', raw_audio_id='legacy-audio'"))
    before = _schema_rows(engine)
    with pytest.raises(RuntimeError, match="rapport_auto_audio_duplicate_evidence"):
        command.upgrade(config, HEAD)
    assert _schema_rows(engine) == before
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == PARENT
        assert conn.execute(text("SELECT COUNT(*) FROM rapportutteranceevent WHERE raw_audio_id='legacy-audio'")).scalar_one() == 2


def test_auto_audio_unique_index_rejects_second_evidence_row(tmp_path):
    db = tmp_path / "app.db"
    command.upgrade(_config(db), HEAD)
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        _insert_utterance(conn, event_seq=1)
        _insert_utterance(conn, event_seq=2)
        conn.execute(text("UPDATE rapportutteranceevent SET origin='auto', raw_audio_id='one-audio' WHERE event_seq=1"))
        conn.commit()
        with pytest.raises(IntegrityError):
            conn.execute(text("UPDATE rapportutteranceevent SET origin='auto', raw_audio_id='one-audio' WHERE event_seq=2"))
        conn.rollback()
        assert conn.execute(text("SELECT COUNT(*) FROM rapportutteranceevent")).scalar_one() == 2


def test_playback_evidence_blocks_destructive_downgrade(tmp_path):
    db = tmp_path / "app.db"
    config = _config(db)
    command.upgrade(config, HEAD)
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO rapportplaybackreceipt "
            "(session_id,wseq,runtime_revision,section_key,question_idx,beat,outcome,device_token_hash,received_at) "
            "VALUES ('S-MIG',1,0,'synthetic',0,'ask','played','synthetic','2026-09-06 00:00:00')"))
    before = _schema_rows(engine)
    with pytest.raises(RuntimeError, match="rapport_generation_evidence_exists"):
        command.downgrade(config, PARENT)
    assert _schema_rows(engine) == before
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD
        assert conn.execute(text("SELECT COUNT(*) FROM rapportplaybackreceipt")).scalar_one() == 1
