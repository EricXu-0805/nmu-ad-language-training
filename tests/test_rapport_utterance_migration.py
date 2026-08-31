"""发声账本迁移 d0c22a6dae2a 的 Alembic 验收。

上行：rapportutteranceevent 新表(精确命名约束) + ttsserveevidence 扩
source 闭集与 utterance_id 绑定臂；下行 fail-closed：任一发声证据行存在
即拒绝，schema 与版本号原样保留。
"""
from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


HEAD = "d0c22a6dae2a"      # 本层=当前全仓头
PARENT = "c8e5a1f3b209"


def _config(db_path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def _schema_rows(engine) -> tuple[tuple[object, ...], ...]:
    with engine.connect() as connection:
        return tuple(connection.execute(text(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )).all())


def _insert_utterance(connection, *, event_seq: int = 1,
                      source: str = "bank", origin: str = "manual",
                      question_idx: int = 3, txt: str = "好的，谢谢您告诉我。") -> None:
    connection.execute(text(
        "INSERT INTO rapportutteranceevent "
        "(session_id, event_seq, section_key, question_idx, source, origin, "
        "text, text_sha256, created_at, is_simulation) VALUES "
        "('S-MIG', :seq, '自我介绍', :qidx, :source, :origin, :txt, :sha, "
        "'2026-08-31 00:00:00', 0)"
    ), {"seq": event_seq, "qidx": question_idx, "source": source,
        "origin": origin, "txt": txt, "sha": "a" * 64})


def test_migration_reaches_exactly_one_repo_head(tmp_path):
    config = _config(tmp_path / "app.db")
    assert ScriptDirectory.from_config(config).get_heads() == [HEAD]


def test_upgrade_creates_ledger_with_exact_constraints(tmp_path):
    db = tmp_path / "app.db"
    config = _config(db)
    command.upgrade(config, HEAD)
    engine = create_engine(f"sqlite:///{db}")
    assert "rapportutteranceevent" in inspect(engine).get_table_names()

    with engine.connect() as c:
        _insert_utterance(c, event_seq=1)
        c.commit()
        # 每条命名约束逐个咬合
        for kwargs in (
            dict(event_seq=0),                       # ck_..._event_seq_positive
            dict(event_seq=2, source="invented"),    # ck_..._source
            dict(event_seq=2, origin="robot"),       # ck_..._origin
            dict(event_seq=2, txt="   "),            # ck_..._text_nonempty
            dict(event_seq=2, question_idx=-1),      # ck_..._question_idx
            dict(event_seq=1),                       # uq_...(session,event_seq)
        ):
            with pytest.raises(IntegrityError):
                _insert_utterance(c, **kwargs)
            c.rollback()


def test_tts_serve_evidence_source_arms_are_exact(tmp_path):
    db = tmp_path / "app.db"
    command.upgrade(_config(db), HEAD)
    engine = create_engine(f"sqlite:///{db}")

    def insert(c, *, source, command_id, utterance_id):
        c.execute(text(
            "INSERT INTO ttsserveevidence "
            "(source, engine_version, cache_hit, result, byte_count, "
            "text_sha256, is_simulation, created_at, command_id, utterance_id) "
            "VALUES (:source, 'test/1', 0, 'served', 10, :sha, 0, "
            "'2026-08-31 00:00:00', :command_id, :utterance_id)"
        ), {"source": source, "sha": "b" * 64,
            "command_id": command_id, "utterance_id": utterance_id})

    with engine.connect() as c:
        _insert_utterance(c)
        insert(c, source="rapport_utterance", command_id=None, utterance_id=1)
        insert(c, source="live_speak", command_id=None, utterance_id=None)
        c.commit()
        for bad in (
            dict(source="rapport_utterance", command_id=None, utterance_id=None),
            dict(source="live_speak", command_id=None, utterance_id=1),
            dict(source="autopilot_command", command_id=None, utterance_id=None),
            dict(source="somewhere_else", command_id=None, utterance_id=None),
        ):
            with pytest.raises(IntegrityError):
                insert(c, **bad)
            c.rollback()


@pytest.mark.parametrize("seed", ["utterance", "serve_evidence"])
def test_downgrade_refuses_while_any_voice_evidence_exists(tmp_path, seed):
    db = tmp_path / "app.db"
    config = _config(db)
    command.upgrade(config, HEAD)
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as c:
        _insert_utterance(c)
        if seed == "serve_evidence":
            c.execute(text(
                "INSERT INTO ttsserveevidence "
                "(source, engine_version, cache_hit, result, byte_count, "
                "text_sha256, is_simulation, created_at, command_id, utterance_id) "
                "VALUES ('rapport_utterance', 'test/1', 0, 'served', 10, "
                ":sha, 0, '2026-08-31 00:00:00', NULL, 1)"), {"sha": "b" * 64})
            c.execute(text("DELETE FROM rapportutteranceevent"))
        c.commit()
    before = _schema_rows(engine)

    with pytest.raises(RuntimeError):
        command.downgrade(config, PARENT)

    assert _schema_rows(engine) == before
    with engine.connect() as c:
        assert c.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == HEAD


def _recovery_guard():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "rapport_mig_guard", Path("scripts/verify_backup_snapshot.py"))
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    return guard


def test_clean_downgrade_roundtrips(tmp_path):
    """往返的判据=生产恢复指纹(canonical 化 DDL),不是 sqlite_master 原文。

    batch 整表重建在「降级再升级」循环里会重排 DDL 文本(约束/外键顺序),
    但备份校验器量的 canonical 契约不许变——那才是生产在验的东西。
    """
    import sqlite3
    db = tmp_path / "app.db"
    config = _config(db)
    command.upgrade(config, HEAD)
    engine = create_engine(f"sqlite:///{db}")
    guard = _recovery_guard()

    def fingerprint() -> str:
        conn = sqlite3.connect(db)
        try:
            return guard._schema_contract_fingerprint(conn)
        finally:
            conn.close()

    first = fingerprint()
    # 首次升级出来的库必须逐字符匹配钉死的恢复指纹(生产要走的正是这条)。
    assert first == guard.CURRENT_RECOVERY_SCHEMA_SHA256
    names_before = set(inspect(engine).get_table_names())

    command.downgrade(config, PARENT)
    assert "rapportutteranceevent" not in inspect(engine).get_table_names()
    with engine.connect() as c:
        # 闭集收窄回两值：rapport_utterance 必须被拒
        with pytest.raises(IntegrityError):
            c.execute(text(
                "INSERT INTO ttsserveevidence "
                "(source, engine_version, cache_hit, result, byte_count, "
                "text_sha256, is_simulation, created_at, command_id) "
                "VALUES ('rapport_utterance', 'test/1', 0, 'served', 10, "
                ":sha, 0, '2026-08-31 00:00:00', NULL)"), {"sha": "b" * 64})
        c.rollback()

    command.upgrade(config, HEAD)
    assert set(inspect(engine).get_table_names()) == names_before
    assert fingerprint() == first


def test_orm_ledger_is_append_only(tmp_path):
    db = tmp_path / "app.db"
    command.upgrade(_config(db), HEAD)
    from sqlmodel import Session as DBSession

    from app.models import RapportUtteranceEvent
    engine = create_engine(f"sqlite:///{db}")
    with DBSession(engine) as s:
        row = RapportUtteranceEvent(
            session_id="S-MIG", event_seq=1, section_key="自我介绍",
            question_idx=3, source="bank", origin="manual",
            text="好的，谢谢您告诉我。", text_sha256="a" * 64)
        s.add(row)
        s.commit()
        s.refresh(row)
        row.text = "被改写"
        with pytest.raises(RuntimeError):
            s.commit()
        s.rollback()
        s.delete(row)
        with pytest.raises(RuntimeError):
            s.commit()
        s.rollback()


def test_upgrade_preserves_existing_serve_evidence_rows(tmp_path):
    """生产要走的正是「表里已有证据行再 batch 重建」这条路——空库升级证明不了它。"""
    db = tmp_path / "app.db"
    config = _config(db)
    command.upgrade(config, PARENT)
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as c:
        for source, command_id in (("live_speak", None), ("autopilot_command", 1)):
            c.execute(text(
                "INSERT INTO ttsserveevidence "
                "(source, engine_version, cache_hit, result, byte_count, "
                "text_sha256, is_simulation, created_at, command_id) "
                "VALUES (:source, 'legacy/1', 1, 'served', 10, :sha, 0, "
                "'2026-08-30 00:00:00', :command_id)"
            ), {"source": source, "sha": "d" * 64, "command_id": command_id})
        c.commit()

    command.upgrade(config, HEAD)

    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT source, command_id, utterance_id, engine_version, "
            "byte_count FROM ttsserveevidence ORDER BY id")).all()
        assert [tuple(r) for r in rows] == [
            ("live_speak", None, None, "legacy/1", 10),
            ("autopilot_command", 1, None, "legacy/1", 10),
        ]
        _insert_utterance(c)
        c.execute(text(
            "INSERT INTO ttsserveevidence "
            "(source, engine_version, cache_hit, result, byte_count, "
            "text_sha256, is_simulation, created_at, command_id, utterance_id) "
            "VALUES ('rapport_utterance', 'new/1', 0, 'served', 10, :sha, 0, "
            "'2026-08-31 00:00:00', NULL, 1)"), {"sha": "d" * 64})
        c.commit()
