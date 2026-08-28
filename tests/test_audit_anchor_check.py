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


# ---------------------------------------------------------------------------
# 基线重置
# ---------------------------------------------------------------------------
# 2026-08-26 有人按收据 224 清库重建了生产库,审计链从头开始。锚点账里记着重建
# 前第 12 行的 tip,而新链第 12 行不可能再等于它 —— 于是**此后每一份快照**都以
# 同一条 prefix_rewritten 失败,异地副本从 8-25 起停止推进,且不会自愈。
# 重置不是删历史:往同一本追加账里写一条 reset 行,它之前的锚点不再参与比对,
# 但仍然留在文件里可查;reset 行必须带理由,空理由拒绝。


def test_reset_baseline_requires_a_reason(tmp_path):
    log = tmp_path / "audit-anchors.log"
    done = _run("--anchors-log", str(log), "--reset-baseline", "   ",
                "--boundary", "20260101-000000")
    assert done.returncode == 2
    assert "reset_reason_required" in done.stdout


def test_reset_line_makes_prior_anchors_stop_being_compared(tmp_path):
    """重建后的新链与旧锚点必然冲突;写下 reset 行之后才重新可用。"""
    db_old = tmp_path / "old.db"
    db_new = tmp_path / "new.db"
    log = tmp_path / "audit-anchors.log"
    _make_chain(db_old, 4, actor="alpha")
    assert _run(str(db_old), "--snapshot", "20260101-000000",
                "--anchors-log", str(log), "--record").returncode == 0

    _make_chain(db_new, 6, actor="beta")          # 清库重建:同位哈希全变
    blocked = _run(str(db_new), "--snapshot", "20260102-000000",
                   "--anchors-log", str(log))
    assert blocked.returncode == 1
    assert "problem=prefix_rewritten" in blocked.stdout

    reset = _run("--anchors-log", str(log), "--reset-baseline", "收据 224 清库重建",
                 "--boundary", "20260102-000000")
    assert reset.returncode == 0, reset.stdout + reset.stderr
    assert "baseline_reset" in reset.stdout

    freed = _run(str(db_new), "--snapshot", "20260102-000000",
                 "--anchors-log", str(log), "--record")
    assert freed.returncode == 0, freed.stdout + freed.stderr

    # 旧锚点一行都没被删掉——重置留痕,不是抹掉证据。
    text = log.read_text(encoding="utf-8")
    assert "snapshot=20260101-000000" in text
    assert "收据 224 清库重建" in text


def test_anchors_recorded_after_reset_still_guard_the_prefix(tmp_path):
    """重置只放过重置前那些锚点;之后记下的照样挡住改写。"""
    db_new = tmp_path / "new.db"
    db_forged = tmp_path / "forged.db"
    log = tmp_path / "audit-anchors.log"
    # 这一句必须真的成功:第一版忘了断言,reset 失败也照样绿——空转断言。
    assert _run("--anchors-log", str(log), "--reset-baseline", "库重建",
                "--boundary", "20260101-000000").returncode == 0
    _make_chain(db_new, 3, actor="beta")
    assert _run(str(db_new), "--snapshot", "20260201-000000",
                "--anchors-log", str(log), "--record").returncode == 0
    _make_chain(db_forged, 5, actor="gamma")
    done = _run(str(db_forged), "--snapshot", "20260202-000000",
                "--anchors-log", str(log))
    assert done.returncode == 1
    assert "problem=prefix_rewritten at_count=3" in done.stdout


def test_reset_line_is_not_a_parse_error_for_the_ledger_reader(tmp_path):
    """reset 行必须被账本读取器认识;认不出就会变成 anchors_log_line_unparsable。"""
    db = tmp_path / "app.db"
    log = tmp_path / "audit-anchors.log"
    _make_chain(db, 1)
    assert _run("--anchors-log", str(log), "--reset-baseline", "理由",
                "--boundary", "20260101-000000").returncode == 0
    assert "baseline-reset" in log.read_text(encoding="utf-8")
    done = _run(str(db), "--snapshot", "20260101-000000",
                "--anchors-log", str(log))
    assert done.returncode == 0, done.stdout


# 重置只解决一半:VPS 上按 KEEP=14 还留着重建**之前**的快照,它们属于已退役的
# 那条链。不划世代边界的话,新旧两代快照会互相判 prefix_rewritten——2026-08-27
# 第一次重置后立刻出现了这个形态(20260825/20260824 撞上新记的 count=77 锚点)。


def test_reset_boundary_is_required_to_be_a_snapshot_stamp(tmp_path):
    log = tmp_path / "audit-anchors.log"
    done = _run("--anchors-log", str(log), "--reset-baseline", "理由",
                "--boundary", "不是快照名")
    assert done.returncode == 2
    assert "reset_boundary_malformed" in done.stdout


def test_snapshots_before_the_boundary_are_checked_but_not_cross_compared(tmp_path):
    """旧世代快照:链内校验照做,跨快照比对与记账都跳过。"""
    db_old = tmp_path / "old.db"
    db_new = tmp_path / "new.db"
    log = tmp_path / "audit-anchors.log"
    _make_chain(db_old, 9, actor="alpha")
    _make_chain(db_new, 3, actor="beta")
    assert _run("--anchors-log", str(log), "--reset-baseline", "库重建",
                "--boundary", "20260201-000000").returncode == 0
    # 新世代先记账
    assert _run(str(db_new), "--snapshot", "20260202-000000",
                "--anchors-log", str(log), "--record").returncode == 0
    # 旧世代快照仍在 VPS 上,每晚都会被重新校验:必须放行且不进账本
    old = _run(str(db_old), "--snapshot", "20260131-235959",
               "--anchors-log", str(log), "--record")
    assert old.returncode == 0, old.stdout + old.stderr
    assert "legacy_generation" in old.stdout
    assert "snapshot=20260131-235959" not in log.read_text(encoding="utf-8")


def test_boundary_does_not_excuse_a_broken_chain_inside_an_old_snapshot(tmp_path):
    """边界只免除跨快照比对,链内篡改照抓——否则重置就成了万能赦免。"""
    db = tmp_path / "old.db"
    log = tmp_path / "audit-anchors.log"
    _make_chain(db, 4, actor="alpha")
    assert _run("--anchors-log", str(log), "--reset-baseline", "库重建",
                "--boundary", "20260201-000000").returncode == 0
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE auditlog SET summary='forged' "
                     "WHERE id=(SELECT MIN(id) FROM auditlog)")
    done = _run(str(db), "--snapshot", "20260131-000000",
                "--anchors-log", str(log))
    assert done.returncode == 1
    assert "problem=chain_broken" in done.stdout


def test_new_generation_snapshots_still_guard_each_other(tmp_path):
    """边界之后的快照之间,前缀延伸照旧生效。"""
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    log = tmp_path / "audit-anchors.log"
    assert _run("--anchors-log", str(log), "--reset-baseline", "库重建",
                "--boundary", "20260201-000000").returncode == 0
    _make_chain(db_a, 3, actor="beta")
    assert _run(str(db_a), "--snapshot", "20260202-000000",
                "--anchors-log", str(log), "--record").returncode == 0
    _make_chain(db_b, 5, actor="gamma")
    done = _run(str(db_b), "--snapshot", "20260203-000000",
                "--anchors-log", str(log))
    assert done.returncode == 1
    assert "problem=prefix_rewritten at_count=3" in done.stdout


def test_reader_accepts_the_boundaryless_reset_line_left_by_the_first_attempt(tmp_path):
    """2026-08-27 16:46 那条 reset 行没有 boundary=,而账本是追加式的、改不得。

    写入侧从此强制 boundary;读取侧必须继续认识这一条历史形态,否则整本账变成
    anchors_log_line_unparsable(exit 2),异地链会从"判篡改"换成"读不出",一样断。
    """
    db = tmp_path / "app.db"
    log = tmp_path / "audit-anchors.log"
    _make_chain(db, 2)
    log.write_text("[2026-08-27 16:46:37] baseline-reset reason=历史形态\n",
                   encoding="utf-8")
    done = _run(str(db), "--snapshot", "20260101-000000",
                "--anchors-log", str(log), "--record")
    assert done.returncode == 0, done.stdout + done.stderr
    # 无边界的重置只清锚点,不划世代:这一份仍要照常记账。
    assert "snapshot=20260101-000000" in log.read_text(encoding="utf-8")
