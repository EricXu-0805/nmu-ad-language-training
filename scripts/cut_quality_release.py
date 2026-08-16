#!/usr/bin/env python3
"""切一次研究分区的冻结发布纪元。两阶段、双人具名、离线执行。

**为什么不给 HTTP 入口。** 这是一个全研究期只做几次的治理动作。给它一个路由，
床旁 web 应用就永久多一个写面——而这个仓库刚被"新 GET 路由默认匿名公开"和
"授权判定走 request.url.path、路由匹配走 scope['path']"咬过两次。不值得。

两阶段的意义：

  --propose   算出载荷、打印它的 sha256 与全部闸门结论，**不写任何东西**
  --approve   由**另一个人**拿着那个 sha256 来批；载荷重算一遍，
              sha256 对不上就拒绝——两阶段之间库动过就不许发

用法::

    scripts/cut_quality_release.py --propose \\
        --builder STEWARD-A --builder-role data_steward
    scripts/cut_quality_release.py --approve \\
        --builder STEWARD-A --builder-role data_steward \\
        --approver ADMIN-B --approver-role admin \\
        --expect-sha256 <上一步打印的> --idempotency-key cut-2026-08-16

退出码：0 = 成功；1 = 被闸门拒绝；2 = 用法错误或环境不满足。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, select  # noqa: E402

from app import db, export_security, quality_release  # noqa: E402
from app.models import SessionRuntimeState  # noqa: E402


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _refuse(code: str, detail: str) -> int:
    # 只打稳定拒绝码与一句人话，不打受试者、场次或路径。
    print(f"REFUSED code={code}", file=sys.stderr)
    print(detail, file=sys.stderr)
    return 1


def _assert_no_open_session(s: Session) -> None:
    """有任何场次还开着就不切。

    两个理由：正在跑的场次证据不完整，冻进去就是把一个中间态当成结论；
    而且切纪元要逐场次读几万行，跟床旁写抢 SQLite 锁——这台机器没开 WAL，
    回滚日志模式下长读会让床旁写在 5 秒后 database is locked。
    """
    open_rows = list(s.exec(
        select(SessionRuntimeState).where(
            SessionRuntimeState.status.in_(("active", "paused")))))
    if open_rows:
        raise quality_release.ReleaseRefused(
            "release_bedside_session_open",
            f"还有 {len(open_rows)} 个场次没有结束，等它们终态之后再切")


def _build(s: Session, as_of: datetime) -> tuple[dict, dict, str]:
    thresholds = quality_release.load_thresholds()
    cohort = quality_release.derive_cohort(
        s, as_of=as_of, quarantine_days=thresholds.entry_quarantine_days)
    payload, watermarks = quality_release.build_payload(
        s, cohort, as_of=as_of, thresholds=thresholds)
    problems = quality_release.registry_problems(payload)
    if problems:
        raise quality_release.ReleaseRefused(
            "release_registry_incomplete",
            "载荷里有未登记的字段，拒绝发布：" + "、".join(problems))
    return payload, watermarks, quality_release.payload_digest(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--propose", action="store_true")
    mode.add_argument("--approve", action="store_true")
    parser.add_argument("--builder", required=True)
    parser.add_argument("--builder-role", required=True,
                        choices=("data_steward", "admin"))
    parser.add_argument("--approver")
    parser.add_argument("--approver-role", choices=("data_steward", "admin"))
    parser.add_argument("--expect-sha256")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--as-of", help="ISO 时刻；默认现在。队列是它的函数。")
    parser.add_argument("--receipt-dir", default=None,
                        help="回执落到哪个目录；不给就只打印")
    args = parser.parse_args(argv)

    if args.approve and not all(
            (args.approver, args.approver_role, args.expect_sha256,
             args.idempotency_key)):
        parser.error(
            "--approve 需要 --approver/--approver-role/--expect-sha256/"
            "--idempotency-key 四项齐全")
    if args.approve and args.approver == args.builder:
        parser.error("构建人与批准人必须是两个不同的具名账号")

    as_of = (datetime.fromisoformat(args.as_of).replace(tzinfo=None)
             if args.as_of else _utc_now_naive())

    try:
        export_security.load_deidentification_config()
    except export_security.DeidentificationConfigurationError as exc:
        return _refuse("deidentification_key_unconfigured", str(exc))

    with Session(db.engine) as s:
        try:
            _assert_no_open_session(s)
            payload, watermarks, digest = _build(s, as_of)
        except quality_release.ReleaseRefused as refused:
            return _refuse(refused.code, refused.detail)

        row = payload["rows"][0]
        summary = {
            "payload_sha256": digest,
            "as_of": as_of.isoformat() + "Z",
            "cohort_size_band": row["release"]["cohort_size_band"],
            "session_count_band": row["release"]["session_count_band"],
            "diagnostics_status": row["diagnostics"]["status"],
            "suppressed_metrics": sorted(
                key for section in ("operational", "research_truth")
                for key, value in row[section].items() if value is None),
            "frozen_sessions": len(watermarks),
        }

        if args.propose:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            print("\n下一步由另一个人执行 --approve 并带上上面的 payload_sha256。",
                  file=sys.stderr)
            return 0

        if digest != args.expect_sha256:
            return _refuse(
                "release_payload_moved",
                "重算出来的载荷与提案不一致——两阶段之间库里动过，请重新 --propose")

        try:
            epoch = quality_release.publish_epoch(
                s, payload=payload, watermarks=watermarks, as_of=as_of,
                thresholds=quality_release.load_thresholds(),
                builder=(args.builder, args.builder_role),
                approver=(args.approver, args.approver_role),
                idempotency_key=args.idempotency_key)
            s.commit()
        except quality_release.ReleaseRefused as refused:
            s.rollback()
            return _refuse(refused.code, refused.detail)

    receipt = {**summary, "epoch_seq": epoch.epoch_seq,
               "frozen_at": epoch.frozen_at.isoformat() + "Z",
               "builder": args.builder, "approver": args.approver}
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    if args.receipt_dir:
        target = Path(args.receipt_dir)
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"quality-release-{epoch.epoch_seq:03d}.json"
        path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"回执已写入 {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
