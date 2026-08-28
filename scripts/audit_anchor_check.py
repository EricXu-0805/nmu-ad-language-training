#!/usr/bin/env python3
"""审计链的异地锚定校验:重算快照里的哈希链,并与本机累积的锚点账对前缀。

背景:app/audit.py 自己诚实标注的【已知残余】——sha256 无密钥、锚点与账本同库,
掌握 DB 写权且懂方案者可整体重算伪造。本脚本把链尾 (count, tip_hash) 记到
**异地拉取机的追加账本**里:VPS 上的人改写历史,改不动这台 Mac 已经记下的锚点。

三层判定(全部 fail-closed):
  1. 快照内:从头重算哈希链,比对行内 prev/entry 与高水位锚点
     (复刻 app/audit.py 的 _chain_hash 与 verify_chain 语义,含
     chain_broken / truncated / anchor_behind);
  2. 跨快照分叉:同一 count 在锚点账里只能对应一个 tip;
  3. 前缀延伸:锚点账里每个 count_i ≤ 当前 count 的历史 tip_i,都必须等于
     当前链在第 count_i 行的 entry_hash——历史一旦被异地记账,就不许被改写。

只用标准库:与拉取脚本同一约束,不能依赖任何可能坏掉的东西。
退出码:0=ok;1=篡改级问题;2=读不出(同样不能当健康)。
输出一行机器可读结论;--record 在 ok 时把本快照锚点追加进账本。
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
from pathlib import Path
import re
import sqlite3

GENESIS = "0" * 64
ANCHOR_LINE = re.compile(
    r"^\[(?P<ts>[^\]]+)\] snapshot=(?P<stamp>\S+) count=(?P<count>\d+) "
    r"tip=(?P<tip>[0-9a-f]{64})$")
# 库被有意重建之后,新链与重建前的锚点必然冲突,而且**每一份后续快照**都会以
# 同一条 prefix_rewritten 失败——2026-08-26 就这样让异地副本停了两天。
# 重置写成账本里的一行,而不是删掉旧行:旧锚点留在文件里可查,只是不再参与比对。
RESET_LINE = re.compile(r"^\[(?P<ts>[^\]]+)\] baseline-reset "
                        r"boundary=(?P<boundary>\d{8}-\d{6}) reason=(?P<reason>.+)$")
SNAPSHOT_STAMP = re.compile(r"^\d{8}-\d{6}$")
# 2026-08-27 16:46 写下过一条没有 boundary= 的重置行,而账本是追加式的、改不得。
# 写入侧从此强制 boundary;读取侧继续认识这一条历史形态,否则整本账会变成
# anchors_log_line_unparsable,异地链只是从"判篡改"换成"读不出",一样断。
LEGACY_RESET_LINE = re.compile(r"^\[(?P<ts>[^\]]+)\] baseline-reset reason=(?P<reason>.+)$")


def _chain_hash(prev_hash: str, ts: datetime, actor: str, action: str,
                patient_id: str | None, session_id: str | None,
                turn_id: int | None, summary: str) -> str:
    payload = "|".join([
        prev_hash, ts.isoformat(timespec="microseconds"), actor, action,
        patient_id or "", session_id or "",
        "" if turn_id is None else str(turn_id), summary,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_ts(raw) -> datetime:
    if isinstance(raw, datetime):
        return raw
    return datetime.fromisoformat(str(raw))


def read_chain(db_path: Path) -> dict:
    """返回 {rows:[entry_hash…], anchor_count, anchor_tip} 或抛 RuntimeError。"""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise RuntimeError(f"db_unreadable err={error}") from None
    try:
        try:
            rows = conn.execute(
                "SELECT ts, actor, action, patient_id, session_id, turn_id,"
                " summary, prev_hash, entry_hash FROM auditlog ORDER BY id"
            ).fetchall()
            anchor = conn.execute(
                "SELECT count, tip_hash FROM auditanchor WHERE id=1").fetchone()
        except sqlite3.Error as error:
            raise RuntimeError(f"audit_tables_unreadable err={error}") from None
    finally:
        conn.close()

    prev = GENESIS
    hashes: list[str] = []
    for index, (ts, actor, action, patient_id, session_id,
                turn_id, summary, prev_hash, entry_hash) in enumerate(rows, 1):
        expect = _chain_hash(prev, _parse_ts(ts), actor, action,
                             patient_id, session_id, turn_id, summary)
        if prev_hash != prev or entry_hash != expect:
            return {"problem": "chain_broken", "broken_at": index,
                    "count": len(rows)}
        hashes.append(entry_hash)
        prev = entry_hash

    anchor_count = anchor[0] if anchor else 0
    anchor_tip = anchor[1] if anchor else GENESIS
    tip = hashes[-1] if hashes else GENESIS
    if len(hashes) < anchor_count or (
            anchor_count and tip != anchor_tip and len(hashes) <= anchor_count):
        return {"problem": "truncated", "count": len(hashes),
                "expected_count": anchor_count}
    if len(hashes) > anchor_count:
        return {"problem": "anchor_behind", "count": len(hashes),
                "expected_count": anchor_count}
    return {"problem": None, "count": len(hashes), "tip": tip, "hashes": hashes}


def load_anchors(log_path: Path) -> dict:
    """读锚点账;任何一行不成形都算账本损坏(exit 2),不能静默跳过。

    返回 {"entries": 仍参与比对的锚点, "boundary": 最后一次重置划的世代边界}。
    """
    if not log_path.exists():
        return {"entries": [], "boundary": None}
    entries: list[dict] = []
    boundary: str | None = None
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        reset = RESET_LINE.match(line) or LEGACY_RESET_LINE.match(line)
        if reset is not None:
            # 重置行之前的锚点不再参与比对,但仍留在文件里。boundary 之前的快照
            # 属于已退役的那条链:VPS 按 KEEP=14 还会留它们十几天,不划这条界
            # 两代快照会互相判 prefix_rewritten。
            entries.clear()
            boundary = reset.groupdict().get("boundary")
            continue
        match = ANCHOR_LINE.match(line)
        if match is None:
            raise RuntimeError("anchors_log_line_unparsable")
        entries.append({"stamp": match["stamp"],
                        "count": int(match["count"]), "tip": match["tip"]})
    return {"entries": entries, "boundary": boundary}


def cross_check(current: dict, stamp: str, anchors: list[dict]) -> str | None:
    """跨快照判定。返回问题码或 None。"""
    hashes = current["hashes"]
    count = current["count"]
    tip = current["tip"] if count else GENESIS
    for entry in anchors:
        if entry["count"] == count and count > 0 and entry["tip"] != tip:
            return f"anchor_fork at_count={count} prior_snapshot={entry['stamp']}"
        if 0 < entry["count"] <= count:
            if hashes[entry["count"] - 1] != entry["tip"]:
                return (f"prefix_rewritten at_count={entry['count']} "
                        f"prior_snapshot={entry['stamp']}")
        # 按备份时间序:更晚的快照链只能更长,变短即历史蒸发。
        if entry["stamp"] < stamp and entry["count"] > count:
            return (f"chain_regressed prior_count={entry['count']} "
                    f"prior_snapshot={entry['stamp']} current_count={count}")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", type=Path, nargs="?", default=None)
    parser.add_argument("--snapshot", default=None,
                        help="快照名 YYYYMMDD-HHMMSS,用于锚点账与跨快照排序")
    parser.add_argument("--anchors-log", type=Path, default=None,
                        help="异地锚点追加账本;给了就做跨快照校验")
    parser.add_argument("--record", action="store_true",
                        help="ok 时把本快照锚点追加进账本(需 --anchors-log)")
    parser.add_argument("--reset-baseline", metavar="理由", default=None,
                        help="库被有意重建后,往账本追一条重置行并写明理由;"
                             "该行之前的锚点不再参与比对(仍留在文件里)")
    parser.add_argument("--boundary", metavar="YYYYMMDD-HHMMSS", default=None,
                        help="配合 --reset-baseline:比它早的快照属于已退役的那条链,"
                             "只做链内校验,不做跨快照比对也不记账")
    args = parser.parse_args(argv)

    if args.reset_baseline is not None:
        if args.anchors_log is None:
            print("UNEVALUABLE reset_needs_anchors_log")
            return 2
        reason = args.reset_baseline.strip()
        if not reason or "\n" in reason:
            print("UNEVALUABLE reset_reason_required")
            return 2
        boundary = (args.boundary or "").strip()
        if not SNAPSHOT_STAMP.match(boundary):
            print("UNEVALUABLE reset_boundary_malformed")
            return 2
        line = (f"[{datetime.now():%F %T}] baseline-reset "
                f"boundary={boundary} reason={reason}")
        try:
            with args.anchors_log.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            args.anchors_log.chmod(0o600)
        except OSError as error:
            print(f"UNEVALUABLE anchors_log_append err={error.errno}")
            return 2
        print(f"baseline_reset reason={reason}")
        return 0

    if args.db_path is None or args.snapshot is None:
        print("UNEVALUABLE db_path_and_snapshot_required")
        return 2

    try:
        current = read_chain(args.db_path)
    except RuntimeError as error:
        print(f"UNEVALUABLE {error}")
        return 2
    if current["problem"] is not None:
        detail = " ".join(f"{k}={v}" for k, v in current.items()
                          if k != "problem" and v is not None)
        print(f"TAMPER problem={current['problem']} {detail}")
        return 1

    if args.anchors_log is not None:
        try:
            ledger = load_anchors(args.anchors_log)
        except (RuntimeError, OSError) as error:
            print(f"UNEVALUABLE anchors_log err={error}")
            return 2
        anchors = ledger["entries"]
        boundary = ledger["boundary"]
        if boundary is not None and args.snapshot < boundary:
            # 退役世代:链内校验上面已经做过并通过了,这里只是不再跨快照比对。
            print(f"ok legacy_generation boundary={boundary} "
                  f"count={current['count']}")
            return 0
        problem = cross_check(current, args.snapshot, anchors)
        if problem is not None:
            print(f"TAMPER problem={problem}")
            return 1
        if args.record and not any(a["stamp"] == args.snapshot for a in anchors):
            line = (f"[{datetime.now():%F %T}] snapshot={args.snapshot} "
                    f"count={current['count']} "
                    f"tip={current['tip'] if current['count'] else GENESIS}")
            try:
                with args.anchors_log.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                args.anchors_log.chmod(0o600)
            except OSError as error:
                print(f"UNEVALUABLE anchors_log_append err={error.errno}")
                return 2

    print(f"ok count={current['count']} "
          f"tip={current['tip'] if current['count'] else GENESIS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
