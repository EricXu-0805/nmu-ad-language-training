from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "restore_drill",
    Path(__file__).resolve().parents[1] / "scripts" / "restore_drill.py")
assert _SPEC is not None and _SPEC.loader is not None
drill = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(drill)


def _restored(tmp_path: Path, *, head: str = "6f2a9c4d8e17",
              heads: list[str] | None = None) -> Path:
    path = tmp_path / "app.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
    for value in (heads if heads is not None else [head]):
        connection.execute("INSERT INTO alembic_version VALUES (?)", (value,))
    for table in drill.CORE_TABLES:
        connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO patient VALUES (1)")
    connection.commit()
    connection.close()
    return path


def test_a_clean_restore_reports_head_and_row_counts(tmp_path):
    facts = drill._inspect(_restored(tmp_path))

    assert facts["alembic_head"] == "6f2a9c4d8e17"
    assert facts["row_counts"]["patient"] == 1
    assert facts["row_counts"]["session"] == 0


def test_a_missing_core_table_is_reported_as_minus_one_not_a_crash(tmp_path):
    path = _restored(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE auditlog")
    connection.commit()
    connection.close()

    facts = drill._inspect(path)

    assert facts["row_counts"]["auditlog"] == -1


def test_two_alembic_rows_fail_the_drill(tmp_path):
    # 多头的库不能拿来恢复：不知道该配哪个版本的应用。
    with pytest.raises(drill.DrillFailure) as excinfo:
        drill._inspect(_restored(tmp_path, heads=["a" * 12, "b" * 12]))

    assert "单一值" in str(excinfo.value)


def test_a_foreign_key_violation_fails_the_drill(tmp_path):
    path = tmp_path / "app.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
    connection.execute("INSERT INTO alembic_version VALUES ('6f2a9c4d8e17')")
    connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    connection.execute(
        "CREATE TABLE child (id INTEGER PRIMARY KEY, "
        "parent_id INTEGER REFERENCES parent(id))")
    connection.execute("INSERT INTO child VALUES (1, 999)")
    connection.commit()
    connection.close()

    with pytest.raises(drill.DrillFailure) as excinfo:
        drill._inspect(path)

    assert "外键违例" in str(excinfo.value)


def test_a_leftover_sqlite_sidecar_fails_the_drill(tmp_path):
    # 收尾后还留着 -wal/-shm，说明有连接没干净关。换台机器打开这份副本时，
    # 最后一段还在 WAL 里的写入就可能对不上——这正是"恢复出来少了几分钟数据"
    # 的经典形态，光看 integrity_check 是绿的。
    path = _restored(tmp_path)
    Path(str(path) + "-wal").write_bytes(b"")

    with pytest.raises(drill.DrillFailure) as excinfo:
        drill._inspect(path)

    assert "-wal" in str(excinfo.value)


def test_materialize_copies_audio_when_the_snapshot_has_it(tmp_path):
    snapshot = tmp_path / "snap"
    (snapshot / "audio").mkdir(parents=True)
    (snapshot / "audio" / "a.webm").write_bytes(b"x")
    _restored(snapshot)
    work = tmp_path / "work"
    work.mkdir()

    restored = drill._materialize(snapshot, work)

    assert restored.is_file()
    assert (work / "audio" / "a.webm").read_bytes() == b"x"


def test_a_snapshot_without_a_database_fails_before_anything_else(tmp_path):
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    work = tmp_path / "work"
    work.mkdir()

    with pytest.raises(drill.DrillFailure) as excinfo:
        drill._materialize(snapshot, work)

    assert "没有 app.db" in str(excinfo.value)


def test_a_failing_snapshot_verification_stops_the_drill(tmp_path, monkeypatch):
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    _restored(snapshot)

    def refuse(_snapshot, _python):
        raise drill.DrillFailure("快照校验未通过：code=alembic_revision_unsupported")

    monkeypatch.setattr(drill, "_verify_snapshot", refuse)

    with pytest.raises(drill.DrillFailure) as excinfo:
        drill.drill(snapshot, tmp_path, keep=False, python="python3")

    assert "alembic_revision_unsupported" in str(excinfo.value)


def test_the_work_directory_is_removed_unless_kept(tmp_path, monkeypatch):
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    _restored(snapshot)
    monkeypatch.setattr(drill, "_verify_snapshot", lambda *_: None)
    monkeypatch.setattr(drill, "_boot", lambda *_: "/health 200 stub")

    facts = drill.drill(snapshot, tmp_path, keep=False, python="python3")

    assert facts["work_dir"] == "(已清理)"
    assert not [child for child in tmp_path.iterdir()
                if child.name.startswith("nmu-restore-drill-")]


def test_keep_leaves_the_work_directory_for_forensics(tmp_path, monkeypatch):
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    _restored(snapshot)
    monkeypatch.setattr(drill, "_verify_snapshot", lambda *_: None)
    monkeypatch.setattr(drill, "_boot", lambda *_: "/health 200 stub")

    facts = drill.drill(snapshot, tmp_path, keep=True, python="python3")

    kept = Path(str(facts["work_dir"]))
    assert (kept / "app.db").is_file()
    assert kept.stat().st_mode & 0o777 == 0o700


def test_cli_reports_nonzero_on_a_missing_snapshot(tmp_path, capsys):
    code = drill.main([str(tmp_path / "nope")])

    assert code == 1
    assert "恢复演练失败" in capsys.readouterr().err
