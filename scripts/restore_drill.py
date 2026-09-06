#!/usr/bin/env python3
"""把一份快照真的恢复出来，然后让应用对着它跑起来。

`verify_backup_snapshot.py verify-vps` 回答的是"这份快照自洽吗"。演练回答的是
另一个问题：**照它恢复出来的系统，能不能服务**。两者不能互相替代——manifest 全
对、`integrity_check` 通过的快照，照样可能因为没有任何研究者账号而起不来。

做的事，全部在一次性隔离目录里，一个字节都不碰 live `data/`：

  1. 先跑一次快照校验（用同一个解释器调 verify-vps）；
  2. 把 `app.db`、音频和导出产物复制进隔离工作目录；
  3. 对恢复出来的库做 `integrity_check`、`foreign_key_check`、读 alembic 头；
  4. 确认恢复完不留 `-wal` / `-shm` 旁挂文件（留了说明有连接没干净收尾，
     换台机器打开就可能丢最后一段写入）；
  5. 用临时凭据和隔离目录运行完整 lifespan，检查健康、鉴权及恢复账号查询；
  6. 报核心表行数，供人工核对"恢复出来的是不是那一天的数据"。

默认仅接受当前版本的快照，不迁移。历史快照必须显式 `--migrate-copy`，同时提供
源版本校验器及其独立可信 SHA256；先校验源快照，再升级副本并按当前合同验收。
任一步失败即非零退出。`--keep` 保留工作目录供事后取证。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import secrets
import sqlite3
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CORE_TABLES = ("patient", "session", "researchuser", "audioassetrow", "auditlog")
PAYLOAD_TREES = ("audio", "exports", "controlled-audio-exports")


class DrillFailure(RuntimeError):
    pass


def _verify_snapshot(snapshot: Path, python: str, *, source_verifier: Path | None = None,
                     source_verifier_sha256: str | None = None,
                     require_vps_config: bool = True) -> bytes:
    guard = source_verifier or ROOT / "scripts" / "verify_backup_snapshot.py"
    try:
        if guard.is_symlink() or not guard.is_file():
            raise DrillFailure("快照校验器不是普通文件")
        guard_bytes = guard.read_bytes()
        if source_verifier is not None and (
                not source_verifier_sha256
                or hashlib.sha256(guard_bytes).hexdigest() != source_verifier_sha256):
            raise DrillFailure("历史快照校验器未与指定 SHA256 绑定")
        manifest = snapshot / "MANIFEST.sha256"
        if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 64 * 1024 * 1024:
            raise DrillFailure("快照清单不存在或不是有界普通文件")
        source_manifest = manifest.read_bytes()
        # Execute the bytes that passed the pin, never reopen a replaceable
        # archival pathname at subprocess launch. The verifier is standalone.
        with tempfile.TemporaryDirectory(prefix="nmu-restore-verifier-") as guard_dir:
            frozen_guard = Path(guard_dir) / "verify_backup_snapshot.py"
            frozen_guard.write_bytes(guard_bytes)
            frozen_guard.chmod(0o600)
            result = subprocess.run(
                [python, "-I", "-B", str(frozen_guard),
                 "verify-vps" if require_vps_config else "verify", str(snapshot.resolve())],
                capture_output=True, text=True)
    except OSError as exc:
        raise DrillFailure("快照或校验器不可读取") from exc
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip().splitlines()
        raise DrillFailure(
            f"快照校验未通过：{detail[-1] if detail else result.returncode}")
    if manifest.read_bytes() != source_manifest:
        raise DrillFailure("快照清单在校验期间发生变化")
    return source_manifest


def _materialize(snapshot: Path, work: Path) -> Path:
    source_db = snapshot / "app.db"
    if not source_db.is_file():
        raise DrillFailure("快照里没有 app.db")
    restored = work / "app.db"
    shutil.copy2(source_db, restored)
    for name in PAYLOAD_TREES:
        source = snapshot / name
        if source.is_dir():
            shutil.copytree(source, work / name)
    return restored


def _verify_materialized(work: Path, source_manifest: bytes, python: str, *,
                         source_verifier: Path | None = None,
                         source_verifier_sha256: str | None = None) -> str:
    """Bind copied payload bytes to the verified receipt and its source contract."""
    if not isinstance(source_manifest, bytes):
        raise DrillFailure("恢复副本缺少已验证的源清单")
    # The source verifier has already validated these bounded manifest records.
    # Keep precisely the payload subset; live configuration is never copied
    # into, or sourced by, the isolated application.
    lines = []
    for line in source_manifest.splitlines(keepends=True):
        relative = os.fsdecode(line.rstrip(b"\r\n")[66:])
        while relative.startswith("./"):
            relative = relative[2:]
        if relative == "app.db" or any(relative.startswith(f"{name}/") for name in PAYLOAD_TREES):
            lines.append(line)
    derived_manifest = b"".join(lines)
    manifest = work / "MANIFEST.sha256"
    manifest.write_bytes(derived_manifest)
    manifest.chmod(0o600)
    _verify_snapshot(work, python, source_verifier=source_verifier,
                     source_verifier_sha256=source_verifier_sha256,
                     require_vps_config=False)
    # Migration and lifespan can legitimately change the copied DB. Do not
    # leave the pre-start manifest masquerading as a receipt for changed bytes.
    manifest.unlink()
    return hashlib.sha256(derived_manifest).hexdigest()


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
            except sqlite3.Error as exc:
                raise DrillFailure("恢复出来的库缺少可查询的核心表") from exc
    finally:
        connection.close()

    # 收尾之后还留着 sidecar，说明恢复流程里有连接没干净关掉。换台机器打开这份
    # 副本时，最后一段还在 WAL 里的写入就可能对不上。
    leftovers = [suffix for suffix in ("-wal", "-shm", "-journal")
                 if Path(str(restored) + suffix).exists()]
    if leftovers:
        raise DrillFailure(f"恢复目录残留 SQLite 旁挂文件：{leftovers}")

    return {"alembic_head": heads[0], "row_counts": counts}


def _boot(restored: Path, work: Path, *, migrate_copy: bool = False) -> str:
    """Run the real lifespan against the copy, without production credentials."""
    if not restored.is_file() or restored.is_symlink():
        raise DrillFailure("恢复数据库不存在或不是普通文件")
    work = work.resolve(strict=True)
    restored = restored.resolve(strict=True)
    if restored.parent != work or work == ROOT or ROOT in work.parents:
        raise DrillFailure("启动检查只接受仓库外的隔离恢复副本")
    environment = {
        key: value for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "LANG", "LC_ALL", "TZ"}
    }
    environment["DATABASE_URL"] = f"sqlite:///{restored}"
    environment["AUDIO_DIR"] = str(work / "audio")
    environment["REQUIRE_AUTH"] = "1"
    environment["CONSOLE_PIN"] = "".join(secrets.choice("0123456789") for _ in range(24))
    environment["TMPDIR"] = str(work)
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(Path(__file__).resolve()),
             "--_boot-worker", str(restored), str(work),
             "migrate-copy" if migrate_copy else "no-migration"],
            cwd=str(work), env=environment, capture_output=True, text=True,
            timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise DrillFailure("恢复应用启动检查超时") from exc
    if result.returncode != 0:
        # Do not echo arbitrary startup exceptions: they can contain patient data.
        if result.stderr.strip().splitlines()[-1:] == ["restore_migration_duplicate_evidence"]:
            raise DrillFailure("副本迁移被历史自动回应重复证据阻断；未删除或重写原始证据")
        raise DrillFailure("恢复应用启动或受保护数据检查未通过")
    try:
        payload = json.loads(result.stdout.strip())
    except (ValueError, TypeError) as exc:
        raise DrillFailure("恢复应用启动回执无效") from exc
    if payload != {"lifespan": True, "health": 200, "unauthenticated": 401,
                   "authenticated": 200, "core_counts_preserved": True,
                   "migration_requested": migrate_copy}:
        raise DrillFailure("恢复应用启动回执不完整")
    return "lifespan 完成；/health 200；匿名访问 401；恢复账号读取 200；核心行数一致"


def _boot_worker(restored: Path, work: Path, *, migrate_copy: bool = False) -> None:
    """Private child process: isolate every writable root and forbid networking."""
    work = work.resolve(strict=True)
    if (not restored.is_file() or restored.is_symlink()
            or restored.resolve().parent != work or work == ROOT or ROOT in work.parents):
        raise DrillFailure("启动检查只接受仓库外的隔离恢复副本")

    def confined(path) -> None:
        if isinstance(path, int):
            return
        candidate = Path(os.fsdecode(path)).resolve()
        if candidate != work and work not in candidate.parents:
            raise PermissionError("restore_write_outside_isolation")

    def audit_hook(event, args):
        if event in {"socket.connect", "socket.connect_ex", "socket.sendto",
                     "subprocess.Popen", "os.system"}:
            raise PermissionError("restore_external_operation_denied")
        if event == "open":
            _path, _mode, flags = args
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                confined(_path)
        elif event == "sqlite3.connect":
            database = os.fsdecode(args[0])
            if database != ":memory:":
                confined(unquote(urlsplit(database).path) if database.startswith("file:") else database)
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.truncate"}:
            confined(args[0])
        elif event in {"os.rename", "os.link", "os.symlink"}:
            confined(args[0])
            confined(args[1])

    sys.addaudithook(audit_hook)
    sys.path.insert(0, str(ROOT))
    before = _inspect(restored)["row_counts"]
    if migrate_copy:
        from alembic import command
        from alembic.config import Config
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "alembic"))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{restored}")
        command.upgrade(config, "head")
        from scripts.verify_backup_snapshot import verify_snapshot
        # The original receipt cannot attest changed DB bytes. Validate the
        # materialized candidate with the current schema/audio/export contract.
        verify_snapshot(work, require_manifest=False)
    from scripts.check_database_head import assert_database_at_head
    # Snapshot verifiers can recognize archived heads. Booting this release
    # must still reject them; never silently migrate or create missing tables.
    assert_database_at_head(f"sqlite:///{restored}")
    from app import asr, audio_store, auth, db, export, tts
    # These modules use code-relative defaults rather than env overrides.
    asr.SCRATCH_DIR = work / "asr-scratch"
    audio_store.AUDIO_DIR = work / "audio"
    export.EXPORT_DIR = work / "exports"
    export.CONTROLLED_AUDIO_DIR = work / "controlled-audio-exports"
    tts.DATA_DIR = work
    tts.CACHE_DIR = work / "tts-cache"
    from fastapi.testclient import TestClient
    from sqlmodel import Session, select
    from app.main import app
    from app.models import ResearchUser

    with TestClient(app) as client:
        with Session(db.engine) as session:
            user = session.exec(select(ResearchUser).where(
                ResearchUser.disabled == False,  # noqa: E712
                ResearchUser.role.in_(("admin", "researcher")),
            )).first()
            if user is None:
                raise DrillFailure("恢复库没有启用的研究者或管理员")
            # Session issuance is confined to the restored copy; no password reset,
            # external login or patient mutation is performed.
            token = auth.create_session(session, user.username)
        health = client.get("/health").status_code
        denied = client.get("/patients").status_code
        client.cookies.set(auth.COOKIE_NAME, token)
        accepted = client.get("/patients").status_code
    db.engine.dispose()
    after = _inspect(restored)["row_counts"]
    print(json.dumps({"lifespan": True, "health": health,
                      "unauthenticated": denied, "authenticated": accepted,
                      "core_counts_preserved": before == after,
                      "migration_requested": migrate_copy}))


def drill(snapshot: Path, work_parent: Path | None, *, keep: bool,
          python: str, migrate_copy: bool = False,
          source_verifier: Path | None = None,
          source_verifier_sha256: str | None = None) -> dict[str, object]:
    if migrate_copy and (source_verifier is None or source_verifier_sha256 is None):
        raise DrillFailure("迁移副本必须指定匹配源版本的历史校验器及其 SHA256")
    if not migrate_copy and (source_verifier is not None or source_verifier_sha256 is not None):
        raise DrillFailure("历史校验器只能用于显式迁移副本演练")
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
        if migrate_copy:
            source_manifest = _verify_snapshot(snapshot, python, source_verifier=source_verifier,
                                               source_verifier_sha256=source_verifier_sha256)
        else:
            source_manifest = _verify_snapshot(snapshot, python)
        restored = _materialize(snapshot, work)
        payload_manifest_sha256 = _verify_materialized(
            work, source_manifest, python, source_verifier=source_verifier,
            source_verifier_sha256=source_verifier_sha256)
        facts = _inspect(restored)
        facts["source_manifest_sha256"] = hashlib.sha256(source_manifest).hexdigest()
        facts["verified_payload_manifest_sha256"] = payload_manifest_sha256
        facts["source_alembic_head"] = facts["alembic_head"]
        facts["boot"] = (_boot(restored, work, migrate_copy=True) if migrate_copy
                         else _boot(restored, work))
        facts["alembic_head"] = _inspect(restored)["alembic_head"]
        facts["migration_result"] = "isolated_copy_upgraded_and_verified" if migrate_copy else "not_requested"
        if migrate_copy:
            facts["source_verifier_sha256"] = source_verifier_sha256
        facts["snapshot"] = snapshot.name
        facts["work_dir"] = str(work) if keep else "(已清理)"
        return facts
    except DrillFailure as exc:
        if keep:
            raise DrillFailure(f"{exc}；隔离工作目录保留于 {work}") from exc
        raise
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
    parser.add_argument("--migrate-copy", action="store_true",
                        help="只迁移隔离副本；须提供匹配旧版本的校验器及摘要，默认不迁移")
    parser.add_argument("--source-verifier", type=Path)
    parser.add_argument("--source-verifier-sha256")
    args = parser.parse_args(argv)

    try:
        facts = drill(args.snapshot, args.work_parent,
                      keep=args.keep, python=args.python, migrate_copy=args.migrate_copy,
                      source_verifier=args.source_verifier,
                      source_verifier_sha256=args.source_verifier_sha256)
    except DrillFailure as error:
        print(f"恢复演练失败：{error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(facts, ensure_ascii=False, indent=2))
    else:
        print(f"恢复演练通过：{facts['snapshot']}")
        print(f"  alembic 头  {facts['alembic_head']}")
        print(f"  源快照头    {facts['source_alembic_head']}；迁移 {facts['migration_result']}")
        print(f"  应用启动    {facts['boot']}")
        print("  核心表行数  " + "  ".join(
            f"{name}={count}" for name, count in facts["row_counts"].items()))
        print(f"  工作目录    {facts['work_dir']}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--_boot-worker":
        try:
            if sys.argv[4] not in {"migrate-copy", "no-migration"}:
                raise DrillFailure("invalid_boot_mode")
            _boot_worker(Path(sys.argv[2]), Path(sys.argv[3]), migrate_copy=sys.argv[4] == "migrate-copy")
        except Exception as exc:  # fixed diagnostics; no copied patient data in logs
            code = ("restore_migration_duplicate_evidence"
                    if "rapport_auto_audio_duplicate_evidence:" in str(exc)
                    else "restore_boot_failed")
            print(code, file=sys.stderr)
            raise SystemExit(1) from None
    else:
        raise SystemExit(main())
