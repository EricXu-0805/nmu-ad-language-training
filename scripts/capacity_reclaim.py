#!/usr/bin/env python3
"""VPS 一次性回收空间：列出容量评估里的 P0 与 Tier B 候选，只删命令行逐个点名的路径。

默认只读：每个候选给出层级、类别、是否可能含受试者数据、已分配字节（同一 inode 只计一次）
和「单删这一项真正释放的字节」（还有链接在外面的 inode 不算，例如与 live 语音缓存共享
inode 的旧应用树）。挪到同一文件系统别处释放不了空间，所以这里只做真删。

--delete <路径> 可重复。删除前逐个复核：必须是本次扫描列出的候选、父目录在白名单里、
路径不经软链接、既不在也不包含 /opt/nmu/app 与 /opt/nmu/backups/daily；可能含受试者数据
的还要加 --confirm-participant-data。任何一个点名被拒，整批一个都不删。删掉的和被拒的都
逐条记进 /opt/nmu/backups/capacity-reclaim.log（0600）。apt 缓存只提示 `apt-get clean`，
本工具不删。候选清单来自 2026-10-02 的只读盘点，见 docs/CAPACITY_REHEARSAL.md。只用标准库。
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from typing import NamedTuple


class Candidate(NamedTuple):
    tier: str
    category: str
    directory: str
    pattern: str
    participant_data: bool
    note: str


CANDIDATES: tuple[Candidate, ...] = (
    Candidate("P0", "old_cron_db_copy", "opt/nmu/backups", r"^app-\d{4}-\d{2}-\d{2}\.db$",
              True, "已退役 cron 的库副本，同期内容在 daily/ 与 live"),
    Candidate("P0", "old_cron_audio_archive", "opt/nmu/backups",
              r"^audio-\d{4}-\d{2}-\d{2}\.tgz$", True,
              "已退役 cron 的录音打包，同期内容在 daily/ 与 live"),
    Candidate("P0", "proxy_access_log_archive", "opt/nmu/space-maintenance-20260924",
              r"^sing-box-access-pre-20260924\.log\.gz$", False, "代理访问日志压缩件"),
    Candidate("P0", "rollback_tarball", "opt/nmu",
              r"^app-before-deploy-\d{8}(-\d{4}|-\d{6})?\.tar\.gz$", False,
              "旧库头的回滚 tar，打包时排除了 data/"),
    Candidate("P0", "cutover_tarball", "opt/nmu", r"^app-before-cutover-20260722\.tgz$", True,
              "7/22 割接前的整棵应用树，没核实是否带 data/"),
    Candidate("P0", "staging_audit_copy", "opt/nmu/staging", r"^audit-20260907-4c3a8a2-0629$",
              False, "9/07 审计暂存"),
    Candidate("P0", "python_source_tarball", "opt/nmu", r"^Python-3\.14\.7\.tgz$", False,
              "解释器源码包；/opt/nmu/python3.14 是 venv 的底座，要留"),
    Candidate("P0", "release_candidate_tree", "opt/nmu", r"^app-candidate-20260924-[0-9a-f]{7}$",
              False, "9/24 发布候选树"),
    Candidate("P0", "stale_web_or_source", "opt/nmu", r"^(dist|src)-stale-\d{8}-\d{4,6}$", False,
              "换下来的旧前端产物与源码"),
    Candidate("P0", "rehearsal_leftover", "opt/nmu/releases-private", r"^20260924-e510a09$",
              False, "没发布的 9/24 候选"),
    Candidate("P0", "rollback_leftover", "opt/nmu/releases-private",
              r"^a1-292f1989-rollback-20260809-183140$", False, "8/09 回滚现场"),
    Candidate("P0", "rollback_tarball", "opt/nmu/releases-private",
              r"^app-before-a1-292f1989-20260809-183140\.tar\.gz$", True,
              "8/09 之前的应用树，没核实是否带 data/"),
    Candidate("P0", "pre_reset_tarball", "opt/nmu", r"^pre-reset-20260825-224807\.tar\.gz$",
              True, "8/25 重置前的打包，没核实内容"),
    Candidate("P0", "pre_reset_quarantine", "opt/nmu", r"^pre-reset-quarantine-20260825-224807$",
              False, "8/25 重置前的隔离件"),
    Candidate("P0", "quarantine_junk", "opt/nmu", r"^quarantine-junk-20260826$", False,
              "8/26 隔离杂项"),
    Candidate("P0", "stale_release_dir", "opt/nmu", r"^releases$", False,
              "冷归档后剩下的旧发布目录，删前确认没有引用"),
    Candidate("P0", "rotated_failed_login_log", "var/log", r"^btmp\.1$", False,
              "轮转下来的登录失败记录"),
    Candidate("B", "cold_archive", "opt/nmu/space-maintenance-20260924", r"^cold-archive$", True,
              "Mac 第二份 9/24 核验过；删前按 prepare.done.json 的 sha256 重算 Mac 那份"),
    Candidate("B", "release_rehearsal", "opt/nmu/releases-private",
              r"^20260924-(1ab262a|21b23ff|af0c32e|ce314df)$", True,
              "含演练恢复目录与 rotation-held 快照，先把 rotation-held 拷进 Mac release-held"),
    Candidate("B", "held_snapshot", "opt/nmu/maintenance-20260924-kernel", r"^rotation-held$",
              True, "9/17 快照，先拷进 Mac release-held；维护收据留着"),
    Candidate("B", "pre_release_app_tree", "opt/nmu",
              r"^app-before-20260924-(1ab262a|af0c32e|ce314df)$", True,
              "旧完整树；语音缓存与 live 共享 inode，删了只释放库与录音"),
    Candidate("B", "suspect_tarball", "opt/nmu", r"^app-before-deploy-20260820-170248-.+\.tar\.gz$",
              True, "疑含 data，删前先看文件清单"),
    Candidate("B", "incident_evidence", "opt/nmu/backups",
              r"^incident-retained-20260910-permissions$", True,
              "内含 8/29 快照，先拷进 Mac release-held"),
)
PROTECTED = ("opt/nmu/app", "opt/nmu/backups/daily")
APT_CACHE = "var/cache/apt/archives"
DEFAULT_LOG = "opt/nmu/backups/capacity-reclaim.log"
_DATA_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".webm", ".m4a", ".mp3", ".wav", ".ogg",
                  ".tar", ".tgz", ".tar.gz", ".zip")


def _looks_like_participant_data(name: str) -> bool:
    """库、录音、快照清单，以及看不进去的打包件，都按可能含受试者数据算。"""
    lowered = name.lower()
    return lowered == "manifest.sha256" or lowered.endswith(_DATA_SUFFIXES)


def _measure(path: Path) -> tuple[int, int, bool]:
    """(已分配字节, 单删这一项释放的字节, 目录里有没有像受试者数据的文件)；不跟随软链接。

    单个文件候选的受试者数据标记以候选表为准（盘点时按来源核过），这里只查目录内容。
    """
    files: dict[tuple[int, int], list[int]] = {}
    directories = 0
    suspicious = False

    def visit(meta: os.stat_result) -> None:
        nonlocal directories
        if stat.S_ISDIR(meta.st_mode):
            directories += meta.st_blocks * 512
            return
        record = files.setdefault((meta.st_dev, meta.st_ino),
                                  [meta.st_blocks * 512, meta.st_nlink, 0])
        record[2] += 1

    def walk_error(error: OSError) -> None:
        raise error

    top = path.lstat()
    visit(top)
    if stat.S_ISDIR(top.st_mode):
        for current, dirnames, filenames in os.walk(path, onerror=walk_error):
            for name in (*dirnames, *filenames):
                meta = (Path(current) / name).lstat()
                visit(meta)
                if not stat.S_ISDIR(meta.st_mode) and _looks_like_participant_data(name):
                    suspicious = True
    allocated = directories + sum(blocks for blocks, _, _ in files.values())
    reclaimable = directories + sum(
        blocks for blocks, links, seen in files.values() if seen == links)
    return allocated, reclaimable, suspicious


def candidates(root: Path) -> list[dict[str, object]]:
    """本次扫描到的候选；候选所在目录经过软链接的整组跳过。"""
    root = root.resolve(strict=True)
    rows: list[dict[str, object]] = []
    listed: set[Path] = set()
    for candidate in CANDIDATES:
        directory = root / candidate.directory
        try:
            if directory.resolve(strict=True) != directory:
                continue
            names = sorted(os.listdir(directory))
        except FileNotFoundError:
            continue
        pattern = re.compile(candidate.pattern)
        for name in names:
            path = directory / name
            if not pattern.fullmatch(name) or path in listed:
                continue
            if stat.S_ISLNK(path.lstat().st_mode):
                continue
            listed.add(path)
            row: dict[str, object] = {
                "tier": candidate.tier, "category": candidate.category, "path": str(path),
                "participant_data": candidate.participant_data, "note": candidate.note,
            }
            try:
                allocated, reclaimable, suspicious = _measure(path)
            except OSError:
                row.update(allocated_bytes=None, reclaimable_bytes=None,
                           participant_data=True, error="unreadable")
            else:
                row.update(allocated_bytes=allocated, reclaimable_bytes=reclaimable,
                           participant_data=candidate.participant_data or suspicious)
            rows.append(row)
    return rows


def apt_cache_bytes(root: Path) -> int:
    archives = root / APT_CACHE
    try:
        entries = list(os.scandir(archives))
    except OSError:
        return 0
    return sum(entry.stat(follow_symlinks=False).st_blocks * 512 for entry in entries
               if entry.name.endswith(".deb") and entry.is_file(follow_symlinks=False))


def _refusal(raw: str, root: Path, rows: dict[str, dict[str, object]],
             confirm_participant_data: bool) -> str | None:
    target = Path(raw)
    if not target.is_absolute():
        return "path_not_absolute"
    try:
        meta = target.lstat()
        resolved = target.parent.resolve(strict=True) / target.name
    except OSError:
        return "path_missing"
    if stat.S_ISLNK(meta.st_mode):
        return "symlink"
    if resolved != Path(os.path.normpath(target)) or ".." in target.parts:
        return "path_not_canonical"
    for protected in PROTECTED:
        guard = root / protected
        if resolved == guard or guard in resolved.parents or resolved in guard.parents:
            return "protected_path"
    if resolved.parent not in {root / candidate.directory for candidate in CANDIDATES}:
        return "parent_not_allowlisted"
    row = rows.get(str(resolved))
    if row is None:
        return "not_a_candidate"
    if row.get("error"):
        return "candidate_unreadable"
    if row["participant_data"] and not confirm_participant_data:
        return "participant_data_unconfirmed"
    return None


def _remove(path: Path) -> None:
    if stat.S_ISDIR(path.lstat().st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def delete(root: Path, targets: list[str], *, log_path: Path,
           confirm_participant_data: bool) -> int:
    root = root.resolve(strict=True)
    rows = {str(row["path"]): row for row in candidates(root)}
    descriptor = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as log:
        os.fchmod(log.fileno(), 0o600)

        def note(message: str) -> None:
            log.write(f"[{datetime.now():%F %T}] {message}\n")
            log.flush()
            os.fsync(log.fileno())

        refusals = [(raw, _refusal(raw, root, rows, confirm_participant_data))
                    for raw in targets]
        if any(reason for _, reason in refusals):
            for raw, reason in refusals:
                if reason:
                    note(f"refused reason={reason} path={raw}")
                    print(f"拒绝：{reason} {raw}", file=sys.stderr)
            print("有点名被拒，这一批一个都没删。", file=sys.stderr)
            return 1
        for raw in targets:
            row = rows[str(Path(raw).parent.resolve(strict=True) / Path(raw).name)]
            try:
                _remove(Path(raw))
            except OSError as error:
                note(f"failed reason=delete_error err={error.errno} path={raw}")
                print(f"删除失败：{raw}", file=sys.stderr)
                return 1
            note(f"removed tier={row['tier']} category={row['category']} "
                 f"participant={'yes' if row['participant_data'] else 'no'} "
                 f"allocated_bytes={row['allocated_bytes']} "
                 f"reclaimable_bytes={row['reclaimable_bytes']} path={raw}")
            print(f"已删：{raw}（释放约 {row['reclaimable_bytes']} 字节）")
    return 0


def report(root: Path, *, as_json: bool) -> int:
    root = root.resolve(strict=True)
    rows = candidates(root)
    apt = apt_cache_bytes(root)
    totals = {
        tier: {
            "allocated_bytes": sum(int(row["allocated_bytes"] or 0) for row in rows
                                   if row["tier"] == tier),
            "reclaimable_bytes": sum(int(row["reclaimable_bytes"] or 0) for row in rows
                                     if row["tier"] == tier),
        }
        for tier in ("P0", "B")
    }
    if as_json:
        print(json.dumps({"root": str(root), "candidates": rows, "totals": totals,
                          "apt_cache_bytes": apt}, ensure_ascii=False, indent=2))
        return 0
    print("只读报告，什么都没删。删除要逐个 --delete <路径>；含受试者数据的再加 "
          "--confirm-participant-data。")
    print(f"{'层级':<4} {'类别':<26} {'受试者数据':<8} {'已分配字节':>12} {'单删释放':>12}  路径")
    for row in rows:
        print(f"{row['tier']:<4} {row['category']:<26} "
              f"{'可能有' if row['participant_data'] else '无':<8} "
              f"{row['allocated_bytes'] if row['allocated_bytes'] is not None else '读不了':>12} "
              f"{row['reclaimable_bytes'] if row['reclaimable_bytes'] is not None else '读不了':>12}"
              f"  {row['path']}  # {row['note']}")
    for tier, total in totals.items():
        print(f"合计 {tier}：已分配 {total['allocated_bytes']} 字节，"
              f"单删释放 {total['reclaimable_bytes']} 字节")
    print(f"apt 缓存 {apt} 字节：用 apt-get clean 清，本工具不删。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/"),
                        help="文件系统根，测试时指向临时树")
    parser.add_argument("--delete", action="append", default=[], metavar="PATH",
                        help="要删的候选绝对路径，可重复；不给就只出报告")
    parser.add_argument("--confirm-participant-data", action="store_true",
                        help="确认点名的候选里可能含受试者数据、已按批准处置")
    parser.add_argument("--log", type=Path, default=None,
                        help=f"删除记录，默认 <root>/{DEFAULT_LOG}")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.delete:
        return report(args.root, as_json=args.json)
    log_path = args.log if args.log is not None else args.root / DEFAULT_LOG
    try:
        return delete(args.root, args.delete, log_path=log_path,
                      confirm_participant_data=args.confirm_participant_data)
    except OSError as error:
        print(f"删除记录写不进去或根目录读不了，什么都没删：err={error.errno}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
