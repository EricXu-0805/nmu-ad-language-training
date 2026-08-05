#!/usr/bin/env python3
"""把一份快照真的恢复出来，然后让应用对着它跑起来。

`verify_backup_snapshot.py verify-vps` 回答的是"这份快照自洽吗"。演练回答的是
另一个问题：**照它恢复出来的系统，能不能服务**。两者不能互相替代——manifest 全
对、`integrity_check` 通过的快照，照样可能因为没有任何研究者账号而起不来。

做的事，全部在一次性隔离目录里，一个字节都不碰 live `data/`：

  1. 先跑一次快照校验（用同一个解释器调 verify-vps）；
  2. 把 `app.db` 和 `audio/` 复制进隔离工作目录；
  3. 对恢复出来的库做 `integrity_check`、`foreign_key_check`、读 alembic 头；
  4. 确认恢复完不留 `-wal` / `-shm` 旁挂文件（留了说明有连接没干净收尾，
     换台机器打开就可能丢最后一段写入）；
  5. 用 `DATABASE_URL` 指向它，把应用真的启动起来打一次 `/health`；
  6. 报核心表行数，供人工核对"恢复出来的是不是那一天的数据"。

任一步失败即非零退出。`--keep` 保留工作目录供事后取证。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CORE_TABLES = ("patient", "session", "researchuser", "audioassetrow", "auditlog")


class DrillFailure(RuntimeError):
    pass


def _verify_snapshot(snapshot: Path, python: str) -> None:
    guard = ROOT / "scripts" / "verify_backup_snapshot.py"
    result = subprocess.run(
        [python, "-I", str(guard), "verify-vps", str(snapshot)],
        capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip().splitlines()
        raise DrillFailure(
            f"快照校验未通过：{detail[-1] if detail else result.returncode}")


def _materialize(snapshot: Path, work: Path) -> Path:
    source_db = snapshot / "app.db"
    if not source_db.is_file():
        raise DrillFailure("快照里没有 app.db")
    restored = work / "app.db"
    shutil.copy2(source_db, restored)
    source_audio = snapshot / "audio"
    if source_audio.is_dir():
        shutil.copytree(source_audio, work / "audio")
    return restored


def _inspect(restored: Path) -> dict[str, object]:
    connection = sqlite3.connect(restored, timeout=30)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise DrillFailure(f"恢复出来的库 integrity_check 失败：{integrity}")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise DrillFailure(f"恢复出来的库有 {len(violations)} 处外键违例")
        heads = [str(row[0]) for row in
                 connection.execute("SELECT version_num FROM alembic_version")]
        if len(heads) != 1:
            raise DrillFailure(f"alembic 头不是单一值：{heads}")
        counts: dict[str, int] = {}
        for table in CORE_TABLES:
            try:
                counts[table] = int(
                    connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            except sqlite3.Error:
                counts[table] = -1
    finally:
        connection.close()

    # 收尾之后还留着 sidecar，说明恢复流程里有连接没干净关掉。换台机器打开这份
    # 副本时，最后一段还在 WAL 里的写入就可能对不上。
    leftovers = [suffix for suffix in ("-wal", "-shm", "-journal")
                 if Path(str(restored) + suffix).exists()]
    if leftovers:
        raise DrillFailure(f"恢复目录残留 SQLite 旁挂文件：{leftovers}")

    return {"alembic_head": heads[0], "row_counts": counts}


def _boot(restored: Path, work: Path) -> str:
    """真的把应用起起来打一次 /health。返回一句人能读的结论。"""
    environment = dict(os.environ)
    environment["DATABASE_URL"] = f"sqlite:///{restored}"
    environment["AUDIO_DIR"] = str(work / "audio")
    environment.pop("ALLOW_SIMULATION_DATA", None)
    script = (
        "import json, sys;"
        "from fastapi.testclient import TestClient;"
        "from app.main import app;"
        "c=TestClient(app);"
        "r=c.get('/health');"
        "print(json.dumps({'status': r.status_code, 'body': r.json()}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=str(ROOT), env=environment,
        capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()
        raise DrillFailure(
            f"应用对着恢复出来的库起不来：{tail[-1] if tail else result.returncode}")
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    if payload["status"] != 200:
        raise DrillFailure(f"/health 返回 {payload['status']}")
    return f"/health 200 {payload['body'].get('service')}"


def drill(snapshot: Path, work_parent: Path | None, *, keep: bool,
          python: str) -> dict[str, object]:
    if not snapshot.is_dir() or snapshot.is_symlink():
        raise DrillFailure(f"快照目录不可用：{snapshot}")
    live = (ROOT / "data").resolve()
    work = Path(tempfile.mkdtemp(
        prefix="nmu-restore-drill-",
        dir=str(work_parent) if work_parent else None))
    os.chmod(work, 0o700)
    if live == work.resolve() or live in work.resolve().parents:
        shutil.rmtree(work, ignore_errors=True)
        raise DrillFailure("演练目录不得位于 live data/ 之内")

    try:
        _verify_snapshot(snapshot, python)
        restored = _materialize(snapshot, work)
        facts = _inspect(restored)
        facts["boot"] = _boot(restored, work)
        facts["snapshot"] = snapshot.name
        facts["work_dir"] = str(work) if keep else "(已清理)"
        return facts
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--work-parent", type=Path, default=None,
                        help="隔离工作目录的上级；默认系统临时目录")
    parser.add_argument("--keep", action="store_true", help="保留工作目录供取证")
    parser.add_argument("--python", default=sys.executable,
                        help="跑快照校验器的解释器（必须能 import sqlalchemy）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        facts = drill(args.snapshot, args.work_parent,
                      keep=args.keep, python=args.python)
    except DrillFailure as error:
        print(f"恢复演练失败：{error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(facts, ensure_ascii=False, indent=2))
    else:
        print(f"恢复演练通过：{facts['snapshot']}")
        print(f"  alembic 头  {facts['alembic_head']}")
        print(f"  应用启动    {facts['boot']}")
        print("  核心表行数  " + "  ".join(
            f"{name}={count}" for name, count in facts["row_counts"].items()))
        print(f"  工作目录    {facts['work_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
