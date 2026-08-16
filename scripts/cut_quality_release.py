#!/usr/bin/env python3
"""切一次研究分区的冻结发布纪元。两阶段、双人具名、离线执行。

**这个动作同时决定两个面能看到什么**：`/quality/ai-metrics` 的研究分区聚合，以及
`/research/v1/{subjects,sessions,turns}` 的逐环节明细。两个面读同一个纪元、同一批
场次；行表是稳定、成员对齐的研究摘录，但不承诺单靠三张 CSV 复算所有质量指标。
反过来说——**不切纪元，研究分区两个面都是关着的**，这是设计不是故障。

**为什么不给 HTTP 入口。** 这是一个全研究期只做几次的治理动作。给它一个路由，
床旁 web 应用就永久多一个写面——而这个仓库刚被"新 GET 路由默认匿名公开"和
"授权判定走 request.url.path、路由匹配走 scope['path']"咬过两次。不值得。

两阶段的意义：

  --propose   算出聚合、行快照与提案 sha256，**不写任何东西**
  --approve   由**另一个人**拿着提案 sha256 来批；全部证据重算一遍，
              指纹对不上就拒绝——两阶段之间库动过就不许发

用法::

    scripts/cut_quality_release.py --propose \\
        --builder STEWARD-A --builder-role data_steward
    scripts/cut_quality_release.py --approve \\
        --builder STEWARD-A --builder-role data_steward \\
        --approver ADMIN-B --approver-role admin \\
        --as-of <上一步打印的> \\
        --expect-proposal-sha256 <上一步打印的> \\
        --idempotency-key cut-2026-08-16

    scripts/cut_quality_release.py --recover-receipt \\
        <原 approve 的全部身份、as-of、proposal 与 idempotency 参数> \\
        --receipt-dir <批准前已存在且含 pending 回执的目录>

``--recover-receipt`` 会从新数据库会话按幂等键核对已发布纪元，并重构提案指纹。
有 pending 就逐字节核对后公布；没有 pending 就只凭纪元内的完整恢复证据补回执。
它不会重发纪元，旧纪元缺恢复证据时也不会猜。

退出码：0 = 成功；1 = 被闸门拒绝；2 = 用法错误或环境不满足。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, select  # noqa: E402

from app import db, export_security, quality_release  # noqa: E402
from app.models import (  # noqa: E402
    QualityReleaseEpoch,
    QualityReleaseEpochSession,
    SessionRuntimeState,
)


_PENDING_TOKEN_RE = re.compile(r"^[0-9a-f]{24}$")


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


def _build(
    s: Session, as_of: datetime, config, builder: tuple[str, str],
) -> tuple[dict, dict, object, object, str, str]:
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
    research_snapshot = quality_release.build_research_snapshot(
        s, session_ids=list(watermarks), config=config)
    payload_sha256 = quality_release.payload_digest(payload)
    proposal_sha256 = quality_release.proposal_digest(
        payload, watermarks, as_of=as_of, config=config,
        thresholds=thresholds, builder=builder,
        research_snapshot_sha256=research_snapshot.snapshot_sha256)
    return (payload, watermarks, thresholds, research_snapshot,
            payload_sha256, proposal_sha256)


def _parse_as_of(raw: str | None) -> datetime:
    if raw is None:
        return _utc_now_naive()
    parsed = datetime.fromisoformat(raw.removesuffix("Z") + (
        "+00:00" if raw.endswith("Z") else ""))
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _receipt_root(raw: str | None) -> Path | None:
    if raw is None:
        return None
    root = Path(raw)
    try:
        facts = root.lstat()
    except OSError as exc:
        raise quality_release.ReleaseRefused(
            "release_receipt_directory_unavailable",
            "回执目录不存在或无法读取；发布前请先安全建好目录") from exc
    if stat.S_ISLNK(facts.st_mode) or not stat.S_ISDIR(facts.st_mode):
        raise quality_release.ReleaseRefused(
            "release_receipt_directory_unsafe",
            "回执目录必须是普通目录，不能是软链接")
    return root


def _fsync_directory(root: Path) -> None:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(root, directory_flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _stage_receipt(
    root: Path, receipt: dict, *, epoch_seq: int,
) -> tuple[Path, Path]:
    """在 DB commit 前把完整回执写成同目录的 0600 pending 文件。"""
    final = root / f"quality-release-{epoch_seq:03d}.json"
    try:
        final.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise quality_release.ReleaseRefused(
            "release_receipt_target_unavailable", "无法检查回执目标") from exc
    else:
        raise quality_release.ReleaseRefused(
            "release_receipt_exists", "该纪元回执已存在，拒绝覆盖")

    pending = root / f".{final.name}.{secrets.token_hex(12)}.pending"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    body = (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode()
    fd: int | None = None
    try:
        fd = os.open(pending, flags, 0o600)
        os.fchmod(fd, 0o600)
        view = memoryview(body)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short receipt write")
            view = view[written:]
        os.fsync(fd)
        _fsync_directory(root)
    except OSError as exc:
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            pass
        raise quality_release.ReleaseRefused(
            "release_receipt_stage_failed",
            "回执未能在数据库提交前完整落盘，本次不发布") from exc
    finally:
        if fd is not None:
            os.close(fd)
    return pending, final


def _discard_staged_receipt(staged: tuple[Path, Path] | None) -> None:
    if staged is None:
        return
    try:
        staged[0].unlink(missing_ok=True)
    except OSError:
        pass


def _finalize_receipt(staged: tuple[Path, Path]) -> Path:
    """DB 已提交后用不覆盖的 hard-link 公布 pending 回执。"""
    pending, final = staged
    os.link(pending, final, follow_symlinks=False)
    pending.unlink()
    _fsync_directory(final.parent)
    return final


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is not None and value.utcoffset() is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat() + "Z"


def _read_receipt_file(path: Path) -> tuple[dict, bytes]:
    """无跟随地读取一个小型 0600 普通回执文件。"""
    try:
        before = path.lstat()
    except OSError as exc:
        raise quality_release.ReleaseRefused(
            "release_receipt_unavailable", "无法读取待恢复回执") from exc
    if (not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600):
        raise quality_release.ReleaseRefused(
            "release_receipt_unsafe",
            "待恢复回执必须是权限 0600 的普通文件，不能是软链接")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise quality_release.ReleaseRefused(
            "release_receipt_unavailable", "无法安全打开待恢复回执") from exc
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (before.st_dev, before.st_ino)):
            raise quality_release.ReleaseRefused(
                "release_receipt_unsafe", "读取期间回执目标发生了变化")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > 1024 * 1024:
                raise quality_release.ReleaseRefused(
                    "release_receipt_unsafe", "待恢复回执体积异常")
            chunks.append(chunk)
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise quality_release.ReleaseRefused(
            "release_receipt_corrupt", "待恢复回执不是有效 JSON") from exc
    if not isinstance(decoded, dict):
        raise quality_release.ReleaseRefused(
            "release_receipt_corrupt", "待恢复回执必须是 JSON 对象")
    return decoded, raw


def _pending_receipts(root: Path, *, epoch_seq: int) -> list[Path]:
    final_name = f"quality-release-{epoch_seq:03d}.json"
    prefix = f".{final_name}."
    suffix = ".pending"
    try:
        children = list(root.iterdir())
    except OSError as exc:
        raise quality_release.ReleaseRefused(
            "release_receipt_directory_unavailable",
            "无法枚举回执目录") from exc
    matches: list[Path] = []
    for child in children:
        if not (child.name.startswith(prefix) and child.name.endswith(suffix)):
            continue
        token = child.name[len(prefix):-len(suffix)]
        if _PENDING_TOKEN_RE.fullmatch(token) is None:
            raise quality_release.ReleaseRefused(
                "release_receipt_pending_ambiguous",
                "发现命名异常的 pending 回执，拒绝猜测")
        matches.append(child)
    return sorted(matches)


def _recovery_source(
    root: Path, *, epoch_seq: int,
) -> tuple[dict | None, Path | None, Path, bool]:
    """返回已核验可读的来源、来源路径、最终路径、最终路径是否已存在。"""
    final = root / f"quality-release-{epoch_seq:03d}.json"
    pending = _pending_receipts(root, epoch_seq=epoch_seq)
    try:
        final.lstat()
    except FileNotFoundError:
        if len(pending) > 1:
            raise quality_release.ReleaseRefused(
                "release_receipt_pending_ambiguous",
                "发现多份 pending 回执，拒绝猜测")
        if not pending:
            return None, None, final, False
        receipt, _ = _read_receipt_file(pending[0])
        return receipt, pending[0], final, False
    except OSError as exc:
        raise quality_release.ReleaseRefused(
            "release_receipt_target_unavailable", "无法检查最终回执") from exc

    receipt, final_raw = _read_receipt_file(final)
    if len(pending) > 1:
        raise quality_release.ReleaseRefused(
            "release_receipt_pending_ambiguous",
            "最终回执旁存在多份 pending 回执，拒绝猜测")
    if pending:
        _, pending_raw = _read_receipt_file(pending[0])
        if pending_raw != final_raw:
            raise quality_release.ReleaseRefused(
                "release_receipt_conflict",
                "最终回执与 pending 回执不同，拒绝覆盖")
        # hard-link 已成功、但 pending unlink/fsync 失败的中间态。
        # 把 pending 交给恢复路径清理，不要只返“已成功”。
        return receipt, pending[0], final, True
    return receipt, final, final, True


def _expected_recovery_receipt(
    s: Session, *, epoch: QualityReleaseEpoch,
    as_of: datetime, builder: tuple[str, str], approver: tuple[str, str],
    expected_proposal_sha256: str,
) -> dict:
    """仅凭冻结库证据和预写回执，重构并核对批准契约。"""
    if epoch.status not in ("published", "superseded"):
        raise quality_release.ReleaseRefused(
            "release_receipt_epoch_not_publishable",
            "该幂等键对应的纪元已撤销，拒绝生成发布回执")
    if _utc_iso(epoch.as_of) != _utc_iso(as_of):
        raise quality_release.ReleaseRefused(
            "release_receipt_binding_mismatch", "--as-of 与已发布纪元不一致")
    if (epoch.builder_actor_display_id, epoch.builder_actor_role) != builder:
        raise quality_release.ReleaseRefused(
            "release_receipt_binding_mismatch", "构建人身份与已发布纪元不一致")
    if (epoch.approver_actor_display_id, epoch.approver_actor_role) != approver:
        raise quality_release.ReleaseRefused(
            "release_receipt_binding_mismatch", "批准人身份与已发布纪元不一致")

    try:
        payload = json.loads(epoch.payload_json)
    except json.JSONDecodeError as exc:
        raise quality_release.ReleaseRefused(
            "release_receipt_epoch_corrupt", "纪元载荷不是有效 JSON") from exc
    if (not isinstance(payload, dict)
            or quality_release.canonical_bytes(payload).decode("utf-8")
            != epoch.payload_json
            or quality_release.payload_digest(payload) != epoch.payload_sha256):
        raise quality_release.ReleaseRefused(
            "release_receipt_epoch_corrupt", "纪元载荷与其指纹不一致")

    quarantine_days = epoch.entry_quarantine_days_applied
    stored_proposal_sha256 = epoch.proposal_sha256
    if (not isinstance(quarantine_days, int)
            or isinstance(quarantine_days, bool)
            or not 0 <= quarantine_days <= 365
            or not isinstance(stored_proposal_sha256, str)
            or len(stored_proposal_sha256) != 64
            or any(char not in "0123456789abcdef"
                   for char in stored_proposal_sha256)):
        raise quality_release.ReleaseRefused(
            "release_receipt_recovery_evidence_missing",
            "该纪元没有完整的提案恢复证据，不能补回执")
    expected_policy = {
        "cohort_rule_version": epoch.cohort_rule_version,
        "registry_version": epoch.registry_version,
        "release_schema_version": epoch.schema_version,
        "deidentification_key_id": epoch.deidentification_key_id,
        "min_subjects": epoch.min_subjects_applied,
        "min_cell_subjects": epoch.min_cell_subjects_applied,
        "band_width": epoch.band_width_applied,
        "rate_decimals": epoch.rate_decimals_applied,
        "entry_quarantine_days": quarantine_days,
    }
    members = list(s.exec(
        select(QualityReleaseEpochSession)
        .where(QualityReleaseEpochSession.epoch_id == epoch.epoch_id)
        .order_by(QualityReleaseEpochSession.session_pseudonym)))
    snapshot_sha256 = epoch.research_snapshot_sha256
    if (not isinstance(snapshot_sha256, str) or len(snapshot_sha256) != 64
            or any(char not in "0123456789abcdef"
                   for char in snapshot_sha256)):
        raise quality_release.ReleaseRefused(
            "release_receipt_epoch_corrupt", "纪元缺少研究行快照指纹")
    # 恢复的是“这份已发布冻结事实”的回执，不是只为一个孤立 sha
    # 补文件。行缺失、行内 hash 自洽篡改或 manifest 不匹配都必须拒绝。
    quality_release.validate_epoch_research_snapshot(s, epoch)
    proposal = {
        "schema_version": quality_release.PROPOSAL_SCHEMA_VERSION,
        "as_of": _utc_iso(epoch.as_of),
        "payload_sha256": epoch.payload_sha256,
        "research_snapshot_sha256": snapshot_sha256,
        "evidence_watermarks": [
            (member.session_pseudonym, member.evidence_watermark)
            for member in members
        ],
        "policy": expected_policy,
        "builder": {"display_id": builder[0], "role": builder[1]},
    }
    rebuilt_proposal_sha256 = hashlib.sha256(
        quality_release.canonical_bytes(proposal)).hexdigest()
    if (rebuilt_proposal_sha256 != stored_proposal_sha256
            or stored_proposal_sha256 != expected_proposal_sha256):
        raise quality_release.ReleaseRefused(
            "release_receipt_proposal_mismatch",
            "提案指纹无法由已发布纪元完整重构")

    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) != 1:
        raise quality_release.ReleaseRefused(
            "release_receipt_epoch_corrupt", "纪元载荷缺少发布行")
    row = rows[0]
    try:
        release = row["release"]
        diagnostics = row["diagnostics"]
        metric_sections = (row["operational"], row["research_truth"])
        if (not isinstance(row, dict)
                or not all(isinstance(section, dict)
                           for section in metric_sections)
                or release["cohort_size_band"] != epoch.cohort_size_band
                or release["session_count_band"] != epoch.session_count_band
                or diagnostics["status"] != epoch.diagnostics_status):
            raise KeyError
    except (KeyError, TypeError):
        raise quality_release.ReleaseRefused(
            "release_receipt_epoch_corrupt",
            "纪元载荷与冻结摘要字段不一致") from None
    return {
        "payload_sha256": epoch.payload_sha256,
        "proposal_sha256": expected_proposal_sha256,
        "research_snapshot_sha256": snapshot_sha256,
        "as_of": _utc_iso(epoch.as_of),
        "builder": {"display_id": builder[0], "role": builder[1]},
        "policy": expected_policy,
        "cohort_size_band": release["cohort_size_band"],
        "session_count_band": release["session_count_band"],
        "diagnostics_status": diagnostics["status"],
        "suppressed_metrics": sorted(
            key for section in metric_sections
            for key, value in section.items() if value is None),
        "frozen_sessions": len(members),
        "epoch_seq": epoch.epoch_seq,
        "frozen_at": _utc_iso(epoch.frozen_at),
        "approver": {"display_id": approver[0], "role": approver[1]},
    }


def _recover_receipt(
    args: argparse.Namespace, receipt_root: Path, *, as_of: datetime,
) -> int:
    key_hash = hashlib.sha256(args.idempotency_key.encode("utf-8")).hexdigest()
    with Session(db.engine) as s:
        try:
            quality_release.begin_release_transaction(s, writable=False)
            epochs = list(s.exec(
                select(QualityReleaseEpoch).where(
                    QualityReleaseEpoch.idempotency_key_sha256 == key_hash)))
            if len(epochs) != 1:
                raise quality_release.ReleaseRefused(
                    "release_receipt_epoch_not_found",
                    "数据库中没有且仅有一个与该幂等键对应的已发布纪元")
            epoch = epochs[0]
            expected_receipt = _expected_recovery_receipt(
                s, epoch=epoch, as_of=as_of,
                builder=(args.builder, args.builder_role),
                approver=(args.approver, args.approver_role),
                expected_proposal_sha256=args.expect_proposal_sha256)
            receipt, source, final, already_final = _recovery_source(
                receipt_root, epoch_seq=epoch.epoch_seq)
            epoch_seq = epoch.epoch_seq
            if receipt is not None and receipt != expected_receipt:
                raise quality_release.ReleaseRefused(
                    "release_receipt_conflict",
                    "已有回执与已发布纪元不是同一份发布契约")
            s.rollback()
        except quality_release.ReleaseSnapshotUnavailable:
            s.rollback()
            return _refuse(
                "release_snapshot_unavailable",
                "无法建立稳定的回执恢复核查快照")
        except quality_release.ReleaseRefused as refused:
            s.rollback()
            return _refuse(refused.code, refused.detail)

    if already_final:
        try:
            if source is not None and source != final:
                source.unlink()
            # 即使没有 stale pending，恢复成功也要补一次目录耐久化。
            _fsync_directory(receipt_root)
        except OSError:
            return _refuse(
                "release_receipt_cleanup_failed",
                "最终回执已核验，但 pending 清理或目录落盘失败")
        print(json.dumps(expected_receipt, ensure_ascii=False, indent=2))
        print(f"回执已存在且核验一致：{final}", file=sys.stderr)
        return 0
    if source is None:
        try:
            source, final = _stage_receipt(
                receipt_root, expected_receipt, epoch_seq=epoch_seq)
        except quality_release.ReleaseRefused as refused:
            return _refuse(refused.code, refused.detail)
    try:
        assert source is not None
        path = _finalize_receipt((source, final))
    except OSError:
        return _refuse(
            "release_receipt_finalize_failed",
            "核验已通过，但最终回执仍未能原子发布；pending 文件保持不动")
    print(json.dumps(expected_receipt, ensure_ascii=False, indent=2))
    print(f"回执已恢复到 {path}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--propose", action="store_true")
    mode.add_argument("--approve", action="store_true")
    mode.add_argument(
        "--recover-receipt", action="store_true",
        help="提交结果不确定或 final 回执缺失时，从冻结纪元核验并恢复")
    parser.add_argument("--builder", required=True)
    parser.add_argument("--builder-role", required=True,
                        choices=("data_steward", "admin"))
    parser.add_argument("--approver")
    parser.add_argument("--approver-role", choices=("data_steward", "admin"))
    parser.add_argument(
        "--expect-proposal-sha256", "--expect-sha256",
        dest="expect_proposal_sha256",
        help="提案的 proposal_sha256；--expect-sha256 仅作旧名兼容")
    parser.add_argument("--idempotency-key")
    parser.add_argument(
        "--as-of",
        help="ISO 时刻；提案可默认现在，批准必须带回提案打印的值")
    parser.add_argument("--receipt-dir", default=None,
                        help="回执落到哪个目录；不给就只打印")
    args = parser.parse_args(argv)

    second_phase = args.approve or args.recover_receipt
    if second_phase and not all(
            (args.approver, args.approver_role, args.expect_proposal_sha256,
             args.idempotency_key, args.as_of)):
        parser.error(
            "--approve/--recover-receipt 需要 --approver/--approver-role/--as-of/"
            "--expect-proposal-sha256/--idempotency-key 五项齐全")
    if args.recover_receipt and not args.receipt_dir:
        parser.error("--recover-receipt 还需要 --receipt-dir")
    if second_phase and args.approver == args.builder:
        parser.error("构建人与批准人必须是两个不同的具名账号")

    try:
        as_of = _parse_as_of(args.as_of)
    except ValueError:
        parser.error("--as-of 必须是 ISO 时刻")
    if as_of > _utc_now_naive():
        return _refuse(
            "release_as_of_in_future",
            "--as-of 不能在当前 UTC 时刻之后，否则会绕过入队隔离期")

    if args.recover_receipt:
        try:
            receipt_root = _receipt_root(args.receipt_dir)
        except quality_release.ReleaseRefused as refused:
            return _refuse(refused.code, refused.detail)
        assert receipt_root is not None
        return _recover_receipt(args, receipt_root, as_of=as_of)

    try:
        config = export_security.load_deidentification_config()
    except export_security.DeidentificationConfigurationError as exc:
        return _refuse("deidentification_key_unconfigured", str(exc))
    try:
        receipt_root = _receipt_root(args.receipt_dir) if args.approve else None
    except quality_release.ReleaseRefused as refused:
        return _refuse(refused.code, refused.detail)

    staged_receipt: tuple[Path, Path] | None = None
    with Session(db.engine) as s:
        try:
            quality_release.begin_release_transaction(
                s, writable=bool(args.approve))
            _assert_no_open_session(s)
            previous = quality_release.current_epoch(s)
            if previous is not None and as_of < previous.as_of:
                raise quality_release.ReleaseRefused(
                    "release_as_of_regressed",
                    "新纪元的 --as-of 不得早于当前已发布纪元")
            (payload, watermarks, thresholds, research_snapshot, digest,
             proposal_sha256) = _build(
                s, as_of, config, (args.builder, args.builder_role))
        except quality_release.ReleaseSnapshotUnavailable:
            s.rollback()
            return _refuse(
                "release_snapshot_unavailable",
                "无法建立稳定的切纪元事务快照")
        except quality_release.ReleaseRefused as refused:
            s.rollback()
            return _refuse(refused.code, refused.detail)

        row = payload["rows"][0]
        summary = {
            "payload_sha256": digest,
            "proposal_sha256": proposal_sha256,
            "research_snapshot_sha256": research_snapshot.snapshot_sha256,
            "as_of": as_of.isoformat() + "Z",
            "builder": {"display_id": args.builder,
                        "role": args.builder_role},
            "policy": {
                "cohort_rule_version": quality_release.COHORT_RULE_VERSION,
                "registry_version": quality_release.REGISTRY_VERSION,
                "release_schema_version": quality_release.RELEASE_SCHEMA_VERSION,
                "deidentification_key_id": config.key_id,
                "min_subjects": thresholds.min_subjects,
                "min_cell_subjects": thresholds.min_cell_subjects,
                "band_width": thresholds.band_width,
                "rate_decimals": thresholds.rate_decimals,
                "entry_quarantine_days": thresholds.entry_quarantine_days,
            },
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
            print("\n下一步由另一个人执行 --approve，并带回上面的 as_of 和 proposal_sha256。",
                  file=sys.stderr)
            s.rollback()
            return 0

        if proposal_sha256 != args.expect_proposal_sha256:
            s.rollback()
            return _refuse(
                "release_payload_moved",
                "重算出来的载荷或证据水位与提案不一致，请重新 --propose")

        try:
            epoch = quality_release.publish_epoch(
                s, payload=payload, watermarks=watermarks, as_of=as_of,
                thresholds=thresholds, research_snapshot=research_snapshot,
                builder=(args.builder, args.builder_role),
                approver=(args.approver, args.approver_role),
                idempotency_key=args.idempotency_key,
                proposal_sha256=proposal_sha256)
            epoch_seq = epoch.epoch_seq
            frozen_at = epoch.frozen_at
            receipt = {**summary, "epoch_seq": epoch_seq,
                       "frozen_at": frozen_at.isoformat() + "Z",
                       "approver": {"display_id": args.approver,
                                    "role": args.approver_role}}
            if receipt_root is not None:
                staged_receipt = _stage_receipt(
                    receipt_root, receipt, epoch_seq=epoch_seq)
            try:
                s.commit()
            except Exception:
                s.rollback()
                return _refuse(
                    "release_publish_outcome_unknown",
                    ("数据库提交结果无法确认；保留 pending 回执，并用原参数执行 "
                     "--recover-receipt，不得盲目重试" if staged_receipt is not None
                     else "数据库提交结果无法确认；请用原参数和一个安全回执目录执行 "
                     "--recover-receipt，不得盲目重试"))
        except quality_release.ReleaseRefused as refused:
            s.rollback()
            _discard_staged_receipt(staged_receipt)
            return _refuse(refused.code, refused.detail)
        except Exception:
            s.rollback()
            _discard_staged_receipt(staged_receipt)
            return _refuse(
                "release_publish_failed",
                "数据库提交前的纪元写入失败；事务已回滚，本次没有发布")

    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    if staged_receipt is not None:
        try:
            path = _finalize_receipt(staged_receipt)
        except OSError:
            return _refuse(
                "release_published_receipt_pending",
                "纪元已在数据库发布，但回执发布失败；请保留 pending 文件并执行 "
                "--recover-receipt，不得重切纪元")
        print(f"回执已写入 {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
