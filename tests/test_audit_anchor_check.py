"""audit_anchor_check.py:异地锚定校验器与 app/audit.py 的语义一致性。

链由真实的 audit.record 生成(不是手搓行),确保校验器复刻的 _chain_hash
与生产写入逐字节一致;篡改/截断/分叉/前缀改写各有一条红相。
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import subprocess
import sys

from sqlmodel import SQLModel, Session as DBSession, create_engine

from app import audit
from app.models import AuditAnchor, AuditLog  # noqa: F401 —— 注册表进 metadata

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "audit_anchor_check.py"


def _make_chain(path: Path, entries: int, actor: str = "tester") -> None:
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    with DBSession(engine) as session:
        for index in range(entries):
            audit.record(session, actor=actor, action="score_lock",
                         summary=f"synthetic-{index}", patient_id="SYN-A")
    engine.dispose()


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(CHECKER), *argv],
        capture_output=True, text=True)


def test_empty_chain_is_ok(tmp_path):
    db = tmp_path / "app.db"
    _make_chain(db, 0)
    done = _run(str(db), "--snapshot", "20260101-000000")
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.startswith("ok count=0 tip=" + "0" * 64)


def test_real_chain_is_ok_and_tip_matches_anchor(tmp_path):
    db = tmp_path / "app.db"
    _make_chain(db, 3)
    done = _run(str(db), "--snapshot", "20260101-000000")
    assert done.returncode == 0, done.stdout
    conn = sqlite3.connect(db)
    count, tip = conn.execute(
        "SELECT count, tip_hash FROM auditanchor WHERE id=1").fetchone()
    conn.close()
    assert done.stdout.strip() == f"ok count={count} tip={tip}"


def test_tampered_middle_row_is_chain_broken(tmp_path):
    db = tmp_path / "app.db"
    _make_chain(db, 3)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE auditlog SET summary='forged' WHERE id="
                 "(SELECT id FROM auditlog ORDER BY id LIMIT 1 OFFSET 1)")
    conn.commit()
    conn.close()
    done = _run(str(db), "--snapshot", "20260101-000000")
    assert done.returncode == 1
    assert "problem=chain_broken" in done.stdout
    assert "broken_at=2" in done.stdout


def test_deleted_tail_is_truncated(tmp_path):
    db = tmp_path / "app.db"
    _make_chain(db, 3)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM auditlog WHERE id="
                 "(SELECT MAX(id) FROM auditlog)")
    conn.commit()
    conn.close()
    done = _run(str(db), "--snapshot", "20260101-000000")
    assert done.returncode == 1
    assert "problem=truncated" in done.stdout


def test_anchor_behind_ledger_is_flagged(tmp_path):
    db = tmp_path / "app.db"
    _make_chain(db, 3)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE auditanchor SET count=2 WHERE id=1")
    conn.commit()
    conn.close()
    done = _run(str(db), "--snapshot", "20260101-000000")
    assert done.returncode == 1
    assert "problem=anchor_behind" in done.stdout


def test_record_appends_once_and_replay_is_idempotent(tmp_path):
    db = tmp_path / "app.db"
    log = tmp_path / "audit-anchors.log"
    _make_chain(db, 2)
    for _ in range(2):
        done = _run(str(db), "--snapshot", "20260101-000000",
                    "--anchors-log", str(log), "--record")
        assert done.returncode == 0, done.stdout
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    assert "snapshot=20260101-000000 count=2 tip=" in lines[0]


def test_same_count_different_tip_is_fork(tmp_path):
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    log = tmp_path / "audit-anchors.log"
    _make_chain(db_a, 2, actor="alpha")
    _make_chain(db_b, 2, actor="beta")   # 独立链,同长不同 tip
    first = _run(str(db_a), "--snapshot", "20260101-000000",
                 "--anchors-log", str(log), "--record")
    assert first.returncode == 0
    done = _run(str(db_b), "--snapshot", "20260102-000000",
                "--anchors-log", str(log))
    assert done.returncode == 1
    assert "problem=anchor_fork" in done.stdout


def test_rewritten_prefix_is_detected_even_when_longer(tmp_path):
    """更长但改写了历史前缀的链必须被抓住——这正是异地锚定存在的意义。"""
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    log = tmp_path / "audit-anchors.log"
    _make_chain(db_a, 2, actor="alpha")
    _make_chain(db_b, 4, actor="beta")   # 内部自洽,但第 2 行 tip 与已记账不同
    first = _run(str(db_a), "--snapshot", "20260101-000000",
                 "--anchors-log", str(log), "--record")
    assert first.returncode == 0
    done = _run(str(db_b), "--snapshot", "20260102-000000",
                "--anchors-log", str(log))
    assert done.returncode == 1
    assert "problem=prefix_rewritten at_count=2" in done.stdout


def test_later_snapshot_with_shorter_chain_is_regression(tmp_path):
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    log = tmp_path / "audit-anchors.log"
    _make_chain(db_a, 4, actor="alpha")
    first = _run(str(db_a), "--snapshot", "20260101-000000",
                 "--anchors-log", str(log), "--record")
    assert first.returncode == 0
    # 同一条链的旧状态不可能凭空回到更短——用独立短链模拟"历史蒸发"。
    _make_chain(db_b, 1, actor="alpha")
    done = _run(str(db_b), "--snapshot", "20260102-000000",
                "--anchors-log", str(log))
    assert done.returncode == 1
    assert "problem=chain_regressed" in done.stdout


def test_corrupt_anchors_log_is_unevaluable_not_ok(tmp_path):
    db = tmp_path / "app.db"
    log = tmp_path / "audit-anchors.log"
    _make_chain(db, 1)
    log.write_text("garbage line\n", encoding="utf-8")
    done = _run(str(db), "--snapshot", "20260101-000000",
                "--anchors-log", str(log))
    assert done.returncode == 2
    assert done.stdout.startswith("UNEVALUABLE")
