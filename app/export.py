"""去标识化导出通道 + 过程性评分重建（M6）。

三条硬约束在此落地：
  1. ★画像不进导出：导出各表【不 join、不携带】任何画像字段（profile_week1 独立、此处不读）。
  2. 去标识化默认开：对外分析导出用版本化 HMAC 假名替换 patient 直接标识；
     回答、人工确认、AI 理由与现场备注等自由文本默认全部删除，不信任客户端 PHI 标记。
  3. 评分单一事实源：de_total / 关键要素率【不落库】，导出时由 scoring 纯函数从
     **已锁定**的分环节原始值重建；未 score_locked 的环节不进正式评分统计。

导出成功 → 回写 audio_status（recorded→exported）+ 打 export_batch_id，触发删除闸门第一关；
**绝不在此删除音频**（删除须另走四闸门全绿，见 audio_gate.py）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from uuid import uuid4

from sqlalchemy import or_
from sqlmodel import Session as DBSession
from sqlmodel import select

from . import (audio_gate, audio_store, autopilot_plan_profiles,
               evidence_ledger, export_security,
               repeat_evidence,
               governance_lock, scoring)
from .enums import AudioStatus
from .models import (
    AbnormalEvent, AttemptCaptureProcessing, AttemptEvent, AudioAssetRow,
    AudioCaptureReceipt, AutopilotRepeatRequest, InteractionEvent, ItemEvent,
    AutopilotControlEvent, Patient, PatientDeviceCapability,
    PatientWithdrawalEvent, QuestionnaireItemValue, QuestionnaireRecord,
    RuntimeCommand, RuntimeCommandAck,
    ExportArtifact, ExportBatch, ScaleResult, Session as TrainSession, SessionCloseoutReport,
    SessionOutcomeSummary, SessionRuntimeState, TurnEvent,
)
from .runtime import DOUBLE_ROLE_TO_FIELD

EXPORT_DIR = Path(__file__).resolve().parent.parent / "data" / "exports"
CONTROLLED_AUDIO_DIR = Path(__file__).resolve().parent.parent / "data" / "controlled-audio-exports"
_SAFE_BATCH_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_AUDIO_SUFFIX = re.compile(r"^\.[a-z0-9]{1,10}$")
_ABSOLUTE_DATE_IN_BATCH_ID = re.compile(
    r"(?:^|[^0-9])(?:19|20)\d{2}[-_.]?(?:0[1-9]|1[0-2])[-_.]?(?:0[1-9]|[12]\d|3[01])(?:[^0-9]|$)")

# 默认去标识导出中【绝对禁止出现】的直接标识列名——用于导出后自检。
DIRECT_IDENTIFIER_COLUMNS = export_security.DIRECT_IDENTIFIER_COLUMNS
EXPORT_BATCH_SCHEMA_VERSION = "export-batch.v1"
EXPORT_MANIFEST_SCHEMA_VERSION = "export-manifest.v1"
STAGING_LEASE_DURATION = timedelta(minutes=30)
STAGING_LEASE_RENEW_WINDOW = timedelta(minutes=10)
STAGING_RECEIPT_NAME = ".staging-receipt.json"
_MAX_STAGING_RECEIPT_BYTES = 4096
_CONSENT_GRANTED = frozenset({
    "已同意", "同意", "有效", "已获取", "已取得", "已签署",
    "granted", "consented", "obtained", "signed", "active", "valid",
})
_CONSENT_DENIED = frozenset({
    "未同意", "已撤回", "拒绝", "不同意",
    "denied", "withdrawn", "refused", "declined", "rejected",
})
_SAFE_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{32,200}$")
_SAFE_RELATIVE_PART = re.compile(r"^[A-Za-z0-9._-]+$")
_MANIFEST_FORBIDDEN_KEYS = frozenset({
    "patient_id", "session_id", "raw_audio_id", "answer", "response",
    "asr_text", "confirmed_response_text", "note", "api_key", "key",
})


class ExportIdempotencyConflict(RuntimeError):
    pass


class ExportArtifactIntegrityError(RuntimeError):
    pass


class LegacyExportBatchError(RuntimeError):
    pass


class UnownedStagingPathError(FileExistsError):
    pass


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _new_staging_owner_hash() -> str:
    return hashlib.sha256(uuid4().bytes).hexdigest()


def _claim_staging_filesystem(
        db: DBSession, *, batch_id: str, session_id: str,
        config: export_security.DeidentificationConfig, owner_hash: str,
        lease_now: Optional[datetime] = None) -> tuple[ExportBatch, Optional[str]]:
    """Persist an exclusive filesystem lease before touching a batch path.

    ``FOR UPDATE`` is the cross-worker fence.  The process lock supplies the
    same semantics for SQLite and prevents a local withdrawal transaction from
    interleaving with this short authority update.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", owner_hash):
        raise ValueError("staging owner hash 无效")
    lease_now = lease_now or _utc_now_naive()
    with governance_lock.SUBJECT_EXPORT_WRITE_LOCK:
        _sess, _patient, batch, _audios = (
            _lock_and_revalidate_staging_authority(
                db, batch_id=batch_id, session_id=session_id, config=config))
        previous_owner = batch.staging_owner_hash
        previous_expiry = batch.staging_lease_expires_at
        if (previous_owner is None) != (previous_expiry is None):
            raise ExportArtifactIntegrityError("staging 租约账本不完整")
        if previous_owner is not None and previous_expiry > lease_now:
            raise ExportArtifactIntegrityError("staging 租约仍有效，另一导出工作者正在处理")
        batch.staging_owner_hash = owner_hash
        batch.staging_lease_expires_at = lease_now + STAGING_LEASE_DURATION
        db.add(batch)
        try:
            db.commit()
            db.refresh(batch)
            return batch, previous_owner
        except Exception as commit_error:
            try:
                db.rollback()
            except Exception:
                pass
            try:
                with DBSession(bind=db.get_bind()) as authority:
                    winner = authority.get(ExportBatch, batch_id)
                    if (winner is not None
                            and winner.status == "staging"
                            and winner.staging_owner_hash == owner_hash
                            and winner.staging_lease_expires_at is not None):
                        return winner, previous_owner
            except Exception:
                pass
            raise RuntimeError(
                "staging 租约提交结果不确定；未写文件，须等待租约到期后重试",
            ) from commit_error


def _check_staging_lease(
        db: DBSession, *, batch_id: str, owner_hash: str) -> None:
    """Fence every new file operation and renew a lease near expiry."""
    lease_now = _utc_now_naive()
    with governance_lock.SUBJECT_EXPORT_WRITE_LOCK:
        db.rollback()
        db.expire_all()
        batch = db.exec(select(ExportBatch).where(
            ExportBatch.batch_id == batch_id,
        ).with_for_update()).first()
        if (batch is None or batch.status != "staging"
                or batch.manifest_sha256 or batch.publication_manifest_json
                or batch.staging_owner_hash != owner_hash
                or batch.staging_lease_expires_at is None
                or batch.staging_lease_expires_at <= lease_now):
            db.rollback()
            raise ExportArtifactIntegrityError("staging 租约已失效或已被其他工作者接管")
        if batch.staging_lease_expires_at - lease_now <= STAGING_LEASE_RENEW_WINDOW:
            batch.staging_lease_expires_at = lease_now + STAGING_LEASE_DURATION
            db.add(batch)
            db.commit()
        else:
            db.rollback()


def _release_staging_filesystem(
        db: DBSession, *, batch_id: str, owner_hash: str) -> None:
    """Release only this worker's pre-intent lease after owned files are gone."""
    with governance_lock.SUBJECT_EXPORT_WRITE_LOCK:
        db.rollback()
        db.expire_all()
        batch = db.exec(select(ExportBatch).where(
            ExportBatch.batch_id == batch_id,
        ).with_for_update()).first()
        if batch is None:
            raise ExportArtifactIntegrityError("导出批次账本消失")
        if batch.manifest_sha256 or batch.publication_manifest_json:
            db.rollback()
            return
        if (batch.status != "staging"
                or batch.staging_owner_hash != owner_hash
                or batch.staging_lease_expires_at is None):
            db.rollback()
            raise ExportArtifactIntegrityError("拒绝释放不属于当前工作者的 staging 租约")
        batch.staging_owner_hash = None
        batch.staging_lease_expires_at = None
        db.add(batch)
        db.commit()


def _staging_path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _read_staging_receipt(path: Path) -> dict:
    """Read a tiny regular file without following a final-component symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ExportArtifactIntegrityError("staging 所有权凭证缺失或不安全") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_STAGING_RECEIPT_BYTES:
            raise ExportArtifactIntegrityError("staging 所有权凭证类型或大小无效")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(_MAX_STAGING_RECEIPT_BYTES + 1)
        if len(payload) > _MAX_STAGING_RECEIPT_BYTES:
            raise ExportArtifactIntegrityError("staging 所有权凭证过大")
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ExportArtifactIntegrityError("staging 所有权凭证无法解析") from exc
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise ExportArtifactIntegrityError("staging 所有权凭证结构无效")
    return value


def _cleanup_owned_staging_filesystem(
        *, csv_root: Path, controlled_root: Path, data_classification: str,
        batch_id: str, owner_hash: str) -> None:
    """Delete a pre-intent tree only when its durable owner receipt matches."""
    csv_base = csv_root / data_classification / batch_id
    controlled_batch = controlled_root / data_classification / batch_id
    csv_present = _staging_path_exists(csv_base)
    controlled_present = _staging_path_exists(controlled_batch)
    if not csv_present and not controlled_present:
        return
    if not csv_present:
        raise ExportArtifactIntegrityError("受控音频孤儿目录缺少 staging 所有权凭证")
    secure_base = export_security.find_secure_existing_directory(
        csv_root, data_classification, batch_id)
    if secure_base is None:
        raise ExportArtifactIntegrityError("staging 分析目录不安全，拒绝清理")
    receipt = _read_staging_receipt(secure_base / STAGING_RECEIPT_NAME)
    if receipt != {"batch_id": batch_id, "staging_owner_hash": owner_hash}:
        raise ExportArtifactIntegrityError("staging 所有权凭证与已过期租约不匹配，拒绝清理")
    # Remove controlled data first so the analysis receipt remains available
    # if cleanup is interrupted and must be resumed.
    export_security.safe_remove_tree(controlled_batch, controlled_root)
    export_security.safe_remove_tree(csv_base, csv_root)


def _prepare_staging_filesystem(
        *, csv_root: Path, controlled_root: Path, data_classification: str,
        batch_id: str, previous_owner_hash: Optional[str]) -> None:
    csv_base = csv_root / data_classification / batch_id
    controlled_batch = controlled_root / data_classification / batch_id
    if not (_staging_path_exists(csv_base)
            or _staging_path_exists(controlled_batch)):
        return
    if previous_owner_hash is None:
        raise UnownedStagingPathError(
            "导出批次目录已存在且无可验证的过期租约，拒绝覆盖或清理")
    _cleanup_owned_staging_filesystem(
        csv_root=csv_root, controlled_root=controlled_root,
        data_classification=data_classification, batch_id=batch_id,
        owner_hash=previous_owner_hash,
    )


def validate_export_idempotency_key(value: str) -> str:
    """Require a bounded, URL-safe token with measurable high entropy.

    A UUIDv4 (with or without an ``export-`` prefix) passes.  Human labels and
    sequential counters do not.  Only its SHA-256 digest is persisted.
    """
    if not isinstance(value, str) or not _SAFE_IDEMPOTENCY_KEY.fullmatch(value):
        raise ValueError("导出幂等键必须是 32–200 位安全随机字符")
    frequencies = {char: value.count(char) for char in set(value)}
    entropy_per_char = -sum(
        (count / len(value)) * math.log2(count / len(value))
        for count in frequencies.values()
    )
    if len(set(value)) < 8 or entropy_per_char < 3.0:
        raise ValueError("导出幂等键随机度不足")
    return value


def _idempotency_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request_fingerprint(
        config: export_security.DeidentificationConfig, *, session_id: str,
        deidentify: bool, actor_display_id: str, actor_role: str) -> str:
    canonical = json.dumps({
        "actor_display_id": actor_display_id,
        "actor_role": actor_role,
        "deidentified": deidentify,
        "schema_version": EXPORT_BATCH_SCHEMA_VERSION,
        "session_id": session_id,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        config.key, b"nmu-export-request\x00" + canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _export_scope_hash(
        config: export_security.DeidentificationConfig, session_id: str) -> str:
    return hmac.new(
        config.key,
        b"nmu-export-scope\x00" + session_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _file_receipt(path: Path, *, realm: str, kind: str, root: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ExportArtifactIntegrityError("导出文件缺失或为符号链接")
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise ExportArtifactIntegrityError("导出文件逃逸受控根目录") from exc
    digest, byte_count = audio_store.sha256_file(path)
    return {
        "realm": realm,
        "kind": kind,
        "relative_path": relative,
        "sha256": digest,
        "byte_count": byte_count,
    }


def _manifest_has_forbidden_content(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).casefold() in _MANIFEST_FORBIDDEN_KEYS
            or _manifest_has_forbidden_content(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_manifest_has_forbidden_content(item) for item in value)
    return False


def pseudonymize(patient_id: str) -> str:
    """版本化 HMAC 假名；密钥缺失或不足 32 bytes 时 fail closed。"""
    return export_security.pseudonymize_subject(patient_id)


def controlled_audio_root(data_classification: str,
                          write_dir: Optional[Path] = None) -> Path:
    """受控音频导出根目录。

    生产环境与去标识分析包 ``data/exports`` 物理分开；测试传入
    ``write_dir`` 时仍放在分析批次目录之外，防止调用方把声纹误当去标识附件。
    """
    root = CONTROLLED_AUDIO_DIR if write_dir is None else write_dir / "_controlled_audio"
    return root / data_classification


def find_exported_audio_blob(raw_audio_id: str, batch_id: str,
                             *, data_classification: str,
                             root: Optional[Path] = None) -> Path | None:
    """查找已完成批次中的受控音频副本；checksum 闸门只允许校验它。"""
    if (not audio_store.SAFE_ID.fullmatch(raw_audio_id)
            or not _SAFE_BATCH_ID.fullmatch(batch_id)
            or batch_id in {".", ".."}):
        return None
    try:
        audio_code = export_security.pseudonymize_audio(raw_audio_id)
        base = export_security.find_secure_existing_directory(
            root or CONTROLLED_AUDIO_DIR, data_classification, batch_id, "audio")
    except (export_security.DeidentificationConfigurationError, RuntimeError, ValueError):
        return None
    if base is None:
        return None
    candidates = [candidate for candidate in base.glob(f"{audio_code}.*")
                  if candidate.is_file() and not candidate.is_symlink()
                  and _SAFE_AUDIO_SUFFIX.fullmatch(candidate.suffix)]
    # 多份同 token 副本无法确定权威字节，checksum 闸门必须 fail closed。
    return candidates[0] if len(candidates) == 1 else None


def _realm_root(realm: str, write_dir: Optional[Path]) -> Path:
    if realm.endswith("_analysis"):
        return Path(write_dir or EXPORT_DIR)
    if realm.endswith("_controlled_audio"):
        return Path(
            CONTROLLED_AUDIO_DIR
            if write_dir is None else Path(write_dir) / "_controlled_audio"
        )
    raise ExportArtifactIntegrityError("导出 artifact realm 无效")


def _resolve_artifact_path(
        descriptor: ExportArtifact | dict, *, batch: ExportBatch,
        write_dir: Optional[Path]) -> Path:
    realm = descriptor.realm if isinstance(descriptor, ExportArtifact) else descriptor["realm"]
    relative = (
        descriptor.relative_path if isinstance(descriptor, ExportArtifact)
        else descriptor["relative_path"]
    )
    kind = descriptor.kind if isinstance(descriptor, ExportArtifact) else descriptor["kind"]
    if realm not in {
        f"{batch.data_classification}_analysis",
        f"{batch.data_classification}_controlled_audio",
    }:
        raise ExportArtifactIntegrityError("artifact realm 与批次分类不一致")
    parts = Path(relative).parts
    if (not parts or Path(relative).is_absolute()
            or any(not _SAFE_RELATIVE_PART.fullmatch(part) or part in {".", ".."}
                   for part in parts)
            or len(parts) < 3
            or parts[0] != batch.data_classification
            or parts[1] != batch.batch_id):
        raise ExportArtifactIntegrityError("artifact 相对路径无效")
    if kind == "controlled_audio":
        if realm != f"{batch.data_classification}_controlled_audio" or parts[2] != "audio":
            raise ExportArtifactIntegrityError("受控音频 artifact 路径/realm 不一致")
    elif realm != f"{batch.data_classification}_analysis":
        raise ExportArtifactIntegrityError("分析 artifact 路径/realm 不一致")
    root = _realm_root(realm, write_dir)
    parent = export_security.find_secure_existing_directory(root, *parts[:-1])
    if parent is None:
        raise ExportArtifactIntegrityError("artifact 父目录缺失或不安全")
    path = parent / parts[-1]
    if path.is_symlink() or not path.is_file():
        raise ExportArtifactIntegrityError("artifact 缺失或为符号链接")
    return path


def _parse_result_metadata(raw: str) -> dict:
    try:
        metadata = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ExportArtifactIntegrityError("批次结果元数据无法解析") from exc
    if (not isinstance(metadata, dict)
            or set(metadata) != {"audio_touched", "excluded_items", "sheet_counts"}
            or not isinstance(metadata["audio_touched"], list)
            or not all(isinstance(value, str) for value in metadata["audio_touched"])
            or not isinstance(metadata["excluded_items"], list)
            or not all(isinstance(value, str) for value in metadata["excluded_items"])
            or not isinstance(metadata["sheet_counts"], dict)
            or not all(isinstance(key, str) and isinstance(value, int) and value >= 0
                       for key, value in metadata["sheet_counts"].items())
            or _manifest_has_forbidden_content(metadata)):
        raise ExportArtifactIntegrityError("批次结果元数据不符合安全契约")
    return metadata


def _artifact_public(row: ExportArtifact | dict) -> dict:
    return {
        "realm": row.realm if isinstance(row, ExportArtifact) else row["realm"],
        "kind": row.kind if isinstance(row, ExportArtifact) else row["kind"],
        "relative_path": (
            row.relative_path if isinstance(row, ExportArtifact)
            else row["relative_path"]
        ),
        "sha256": row.sha256 if isinstance(row, ExportArtifact) else row["sha256"],
        "byte_count": (
            row.byte_count if isinstance(row, ExportArtifact) else row["byte_count"]
        ),
    }


def _canonical_manifest(batch: ExportBatch, descriptors: list[dict], metadata: dict) -> dict:
    manifest = {
        "schema_version": EXPORT_MANIFEST_SCHEMA_VERSION,
        "batch_id": batch.batch_id,
        "deidentified": True,
        "data_classification": batch.data_classification,
        "artifacts": sorted(
            [_artifact_public(row) for row in descriptors],
            key=lambda row: (row["realm"], row["kind"], row["relative_path"]),
        ),
        "result": metadata,
    }
    if _manifest_has_forbidden_content(manifest):
        raise ExportArtifactIntegrityError("manifest 含禁止字段")
    return manifest


def _canonical_json_bytes(value: dict) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def verify_export_batch(
        db: DBSession, batch_id: str, *, write_dir: Optional[Path] = None,
        allow_artifacts_ready: bool = False) -> dict:
    """Authoritatively verify DB receipts, every file, and the manifest set."""
    batch = db.get(ExportBatch, batch_id)
    if batch is None:
        legacy = db.exec(select(AudioAssetRow.raw_audio_id).where(
            AudioAssetRow.export_batch_id == batch_id)).first()
        if legacy is not None:
            raise LegacyExportBatchError("旧版导出批次无持久 manifest 账本，只能走 legacy 校验路径")
        raise ValueError("导出批次不存在")
    allowed = {"published", "artifacts_ready"} if allow_artifacts_ready else {"published"}
    if batch.status not in allowed:
        raise ExportArtifactIntegrityError("导出批次尚未发布")
    if not batch.manifest_sha256:
        raise ExportArtifactIntegrityError("导出批次缺 manifest 摘要")
    artifacts = list(db.exec(select(ExportArtifact).where(
        ExportArtifact.batch_id == batch_id).order_by(ExportArtifact.id)))
    manifests = [row for row in artifacts if row.kind == "manifest"]
    if len(manifests) != 1 or len(artifacts) < 2:
        raise ExportArtifactIntegrityError("导出 artifact 账本不完整")
    for row in artifacts:
        path = _resolve_artifact_path(row, batch=batch, write_dir=write_dir)
        digest, byte_count = audio_store.sha256_file(path)
        if byte_count != row.byte_count:
            raise ExportArtifactIntegrityError("artifact 字节数与账本不一致")
        if digest != row.sha256:
            raise ExportArtifactIntegrityError("artifact SHA-256 与账本不一致")
    manifest_row = manifests[0]
    if manifest_row.sha256 != batch.manifest_sha256:
        raise ExportArtifactIntegrityError("manifest 摘要与批次账本不一致")
    manifest_path = _resolve_artifact_path(
        manifest_row, batch=batch, write_dir=write_dir)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExportArtifactIntegrityError("manifest 无法读取") from exc
    metadata = _parse_result_metadata(batch.result_metadata_json)
    expected = _canonical_manifest(
        batch, [_artifact_public(row) for row in artifacts if row.kind != "manifest"],
        metadata,
    )
    if (manifest != expected
            or batch.publication_manifest_json
            != _canonical_json_bytes(expected).decode("utf-8")
            or _manifest_has_forbidden_content(manifest)):
        raise ExportArtifactIntegrityError("manifest 内容与 artifact 账本不一致")
    return {
        "batch": batch,
        "artifacts": [_artifact_public(row) for row in artifacts],
        "metadata": metadata,
    }


def _verified_result(verified: dict) -> dict:
    batch: ExportBatch = verified["batch"]
    metadata = verified["metadata"]
    return {
        "batch_id": batch.batch_id,
        "status": batch.status,
        "deidentified": batch.deidentified,
        "artifacts": verified["artifacts"],
        "files": [row["relative_path"] for row in verified["artifacts"]],
        "audio_touched": metadata["audio_touched"],
        "excluded_items": metadata["excluded_items"],
        "sheet_counts": metadata["sheet_counts"],
    }


def get_export_batch_result(
        db: DBSession, batch_id: str, *, write_dir: Optional[Path] = None) -> dict:
    batch = db.get(ExportBatch, batch_id)
    if batch is None:
        # Preserve the explicit legacy/not-found distinction in one place.
        return _verified_result(verify_export_batch(
            db, batch_id, write_dir=write_dir))
    config = export_security.load_deidentification_config()
    matches = [session_id for session_id in db.exec(
        select(TrainSession.session_id).order_by(TrainSession.session_id)
    ) if _export_scope_hash(config, session_id) == batch.export_scope_hash]
    if len(matches) != 1:
        raise ExportArtifactIntegrityError("无法将导出批次唯一绑定到当前权威场次")
    with governance_lock.SUBJECT_EXPORT_WRITE_LOCK:
        _lock_and_revalidate_export_authority(
            db, batch_id=batch_id, session_id=matches[0], config=config,
            allow_artifacts_ready=False,
        )
        return _verified_result(verify_export_batch(
            db, batch_id, write_dir=write_dir))


def mask_text(text: Optional[str], _contains_direct_identifier: bool) -> Optional[str]:
    """兼容旧调用名；默认去标识通道不信任客户端标记，所有自由文本均删除。"""
    return export_security.redact_free_text(text)


# ============ 评分重建（从已锁定分环节值）============
def _locked(turn: TurnEvent) -> bool:
    # 兼容旧库时也要重验新门禁：历史行即使 score_locked=True，缺人工确认或
    # 提示等级仍不能进入正式研究统计。
    return (bool(turn.score_locked)
            and turn.element_value is not None
            and turn.confirmed_response_text is not None)


def _reconstruct_scores(items: list[ItemEvent], turns_by_item: dict[int, list[TurnEvent]]) -> dict:
    """按 task_type 分桶，用【全部环节已锁定】的题重建综合指标。返回聚合 + 排除清单。"""
    singles: list[scoring.SingleElementItem] = []
    doubles: list[scoring.DoubleElementItem] = []
    multis: list[scoring.MultiElementItem] = []
    excluded: list[str] = []

    for it in items:
        turns = turns_by_item.get(it.id, [])
        if not turns or not all(_locked(t) for t in turns):
            excluded.append(
                f"{it.item_id}（{it.task_type}）：有未确认或未锁定环节，暂不计入正式评分"
            )
            continue
        if any(t.prompt_level is None for t in turns):
            excluded.append(f"{it.item_id}（{it.task_type}）：prompt_level 缺失，暂不计入正式评分")
            continue
        tt = str(it.task_type.value if hasattr(it.task_type, "value") else it.task_type)
        if tt == "单要素":
            t = turns[0]
            fc = int(t.element_value)
            if t.prompt_level is None:
                excluded.append(f"{it.item_id}（单要素）：prompt_level 缺失，不得按自发正确统计")
                continue
            pl = int(t.prompt_level)
            singles.append(scoring.SingleElementItem(
                item_id=it.item_id, final_correct=fc,
                spontaneous_correct=1 if (fc == 1 and pl == 0) else 0,
                prompt_level=pl, duration_seconds=t.duration_seconds))
        elif tt == "双要素":
            vals = {DOUBLE_ROLE_TO_FIELD.get(t.response_role): t.element_value
                    for t in turns if t.response_role in DOUBLE_ROLE_TO_FIELD}
            if set(vals) != set(DOUBLE_ROLE_TO_FIELD.values()):
                excluded.append(f"{it.item_id}（双要素）：环节不齐（缺 {set(DOUBLE_ROLE_TO_FIELD.values())-set(vals)}）")
                continue
            doubles.append(scoring.DoubleElementItem(
                item_id=it.item_id,
                left_name=int(vals["left_name"]), left_function=int(vals["left_function"]),
                right_name=int(vals["right_name"]), right_function=int(vals["right_function"]),
                relation=float(vals["relation"])))
        elif tt == "多要素":
            key_elements = {t.response_role: int(t.element_value) for t in turns}
            multis.append(scoring.MultiElementItem(item_id=it.item_id, key_elements=key_elements))

    out: dict = {"excluded_items": excluded}
    out["single"] = scoring.score_single_element(singles) if singles else None
    out["double"] = scoring.score_double_element(doubles) if doubles else None
    out["multi"] = scoring.score_multi_element(multis) if multis else None
    return out


# ============ 导出 ============
def _require_export_completion_evidence(
        db: DBSession, sess: TrainSession,
        runtime_state: SessionRuntimeState) -> tuple[SessionOutcomeSummary,
                                                     SessionCloseoutReport]:
    """正式导出只接受新版服务端收口链。

    ``completed`` 本身不再是足够证据：历史库中可能有在旧门禁下写入的
    终态。必须同时有不可变自动汇总和已锁定的独立现场收尾，且它们的
    场次、分类与时序必须能与终态相互印证。
    """
    if runtime_state.session_id != sess.session_id:
        raise ValueError("运行终态与场次不一致，拒绝导出")
    if (runtime_state.intervention_completed_at is None
            or runtime_state.completed_at is None
            or runtime_state.end_reason != "completion_gate_passed"):
        raise ValueError("场次缺少新版收口终态证据，拒绝导出")

    summary = db.get(SessionOutcomeSummary, sess.session_id)
    if summary is None:
        raise ValueError("场次缺少不可变自动结果汇总，拒绝导出")
    if (summary.session_id != sess.session_id
            or summary.is_simulation != sess.is_simulation
            or summary.data_classification != sess.data_classification
            or summary.item_bank_version_id != sess.item_bank_version_id):
        raise ValueError("自动结果汇总与场次版本/数据分类不一致，拒绝导出")
    if summary.generated_at > runtime_state.intervention_completed_at:
        raise ValueError("自动结果汇总晚于干预收口终态，拒绝导出")

    closeout = db.get(SessionCloseoutReport, sess.session_id)
    if closeout is None:
        raise ValueError("场次缺少独立现场收尾记录，拒绝导出")
    if closeout.session_id != sess.session_id:
        raise ValueError("现场收尾记录与场次不一致，拒绝导出")
    if closeout.locked_at is None or not (closeout.locked_by or "").strip():
        raise ValueError("现场收尾记录尚未随最终复核锁定，拒绝导出")
    if (closeout.locked_at != runtime_state.completed_at
            or closeout.locked_by != runtime_state.ended_by
            or closeout.locked_at < runtime_state.intervention_completed_at):
        raise ValueError("现场收尾锁定证据与运行终态不一致，拒绝导出")
    return summary, closeout


def _lock_export_authority_rows(
        db: DBSession, *, batch_id: str, session_id: str) -> tuple[
            TrainSession, Patient, ExportBatch, list[AudioAssetRow]]:
    """Lock the exact withdrawal-compatible authority chain."""
    db.rollback()
    db.expire_all()
    sess = db.exec(select(TrainSession).where(
        TrainSession.session_id == session_id,
    ).with_for_update()).first()
    if sess is None:
        raise ExportArtifactIntegrityError("导出场次不存在")
    patient = db.exec(select(Patient).where(
        Patient.patient_id == sess.patient_id,
    ).with_for_update()).first()
    if patient is None:
        raise ExportArtifactIntegrityError("导出场次缺少受试者档案")
    batch = db.exec(select(ExportBatch).where(
        ExportBatch.batch_id == batch_id,
    ).with_for_update()).first()
    if batch is None:
        raise ExportArtifactIntegrityError("导出批次账本不存在")
    audios = list(db.exec(select(AudioAssetRow).where(
        AudioAssetRow.session_id == session_id,
    ).order_by(AudioAssetRow.raw_audio_id).with_for_update()))
    return sess, patient, batch, audios


def _revalidate_current_export_authority(
        db: DBSession, *, sess: TrainSession, patient: Patient,
        batch: ExportBatch, audios: list[AudioAssetRow], session_id: str,
        config: export_security.DeidentificationConfig) -> None:
    if batch.export_scope_hash != _export_scope_hash(config, session_id):
        raise ExportArtifactIntegrityError("导出批次 scope 与场次不一致")
    expected_classification = "simulation" if sess.is_simulation else "research"
    if (sess.data_classification != expected_classification
            or batch.data_classification != sess.data_classification):
        raise ExportArtifactIntegrityError("导出批次与场次数据分类不一致")

    consent = (patient.consent_status or "").strip().casefold()
    if ((patient.withdrawal_status or "").strip()
            or consent in _CONSENT_DENIED
            or db.exec(select(PatientWithdrawalEvent.event_id).where(
                PatientWithdrawalEvent.patient_id == patient.patient_id,
            )).first() is not None):
        raise ExportArtifactIntegrityError("受试者已撤回或同意已失效，拒绝导出")
    if not sess.is_simulation and consent not in _CONSENT_GRANTED:
        raise ExportArtifactIntegrityError("真实研究受试者同意状态不再有效，拒绝导出")
    if patient.secondary_use_allowed is not True:
        raise ExportArtifactIntegrityError("secondary_use_allowed 已失效，拒绝导出")

    runtime_state = db.exec(select(SessionRuntimeState).where(
        SessionRuntimeState.session_id == session_id,
    )).first()
    if runtime_state is None or runtime_state.status != "completed":
        raise ExportArtifactIntegrityError("场次不再处于 completed 权威终态")
    try:
        _require_export_completion_evidence(db, sess, runtime_state)
    except ValueError as exc:
        raise ExportArtifactIntegrityError(str(exc)) from exc
    for audio in audios:
        if (audio.is_simulation != sess.is_simulation
                or audio.data_classification != sess.data_classification):
            raise ExportArtifactIntegrityError("音频与场次数据分类不一致")
        if audio.withdrawn or (audio.withdrawal_status or "").strip():
            raise ExportArtifactIntegrityError("场次含已撤回/隔离音频，拒绝导出")


def _lock_and_revalidate_staging_authority(
        db: DBSession, *, batch_id: str, session_id: str,
        config: export_security.DeidentificationConfig) -> tuple[
            TrainSession, Patient, ExportBatch, list[AudioAssetRow]]:
    """Recheck current authority before claim and again before intent."""
    sess, patient, batch, audios = _lock_export_authority_rows(
        db, batch_id=batch_id, session_id=session_id)
    if (batch.status != "staging" or batch.manifest_sha256
            or batch.publication_manifest_json):
        raise ExportArtifactIntegrityError("导出批次不再处于可写 staging 状态")
    _revalidate_current_export_authority(
        db, sess=sess, patient=patient, batch=batch, audios=audios,
        session_id=session_id, config=config)
    return sess, patient, batch, audios


def _lock_and_revalidate_export_authority(
        db: DBSession, *, batch_id: str, session_id: str,
        config: export_security.DeidentificationConfig,
        allow_artifacts_ready: bool) -> tuple[
            TrainSession, Patient, ExportBatch, list[AudioAssetRow]]:
    """Acquire the cross-worker governance fence and recheck current truth.

    The lock order is deliberately identical to subject withdrawal:
    Session -> Patient -> ExportBatch -> Audio.  The caller already owns the
    process-local companion lock; ``FOR UPDATE`` is the multi-worker authority.
    """
    sess, patient, batch, audios = _lock_export_authority_rows(
        db, batch_id=batch_id, session_id=session_id)

    allowed_statuses = (
        {"artifacts_ready", "published"}
        if allow_artifacts_ready else {"published"}
    )
    if batch.status not in allowed_statuses:
        raise ExportArtifactIntegrityError("导出批次尚未达到可验证状态")
    _revalidate_current_export_authority(
        db, sess=sess, patient=patient, batch=batch, audios=audios,
        session_id=session_id, config=config)
    return sess, patient, batch, audios


def _ensure_export_batch(
        db: DBSession, *, requested_batch_id: Optional[str], key_hash: str,
        request_fingerprint: str, export_scope_hash: str,
        data_classification: str,
        actor_display_id: str, actor_role: str, now: datetime) -> tuple[ExportBatch, bool]:
    existing = db.exec(select(ExportBatch).where(
        ExportBatch.idempotency_key_hash == key_hash)).first()
    if existing is not None:
        if (existing.request_fingerprint != request_fingerprint
                or existing.export_scope_hash != export_scope_hash
                or (requested_batch_id is not None
                    and existing.batch_id != requested_batch_id)):
            raise ExportIdempotencyConflict("导出幂等键已绑定另一请求")
        return existing, True
    scope_winner = db.exec(select(ExportBatch).where(
        ExportBatch.export_scope_hash == export_scope_hash)).first()
    if scope_winner is not None:
        raise ExportIdempotencyConflict(
            "该场次已有导出意图；必须使用原幂等键恢复，不能创建第二批次")
    chosen = requested_batch_id or ("EXP-" + uuid4().hex[:24])
    batch = ExportBatch(
        batch_id=chosen,
        schema_version=EXPORT_BATCH_SCHEMA_VERSION,
        idempotency_key_hash=key_hash,
        request_fingerprint=request_fingerprint,
        export_scope_hash=export_scope_hash,
        status="staging",
        data_classification=data_classification,
        deidentified=True,
        actor_display_id=actor_display_id,
        actor_role=actor_role,
        created_at=now,
    )
    db.add(batch)
    try:
        db.commit()
        db.refresh(batch)
        return batch, False
    except Exception as commit_error:
        try:
            db.rollback()
        except Exception:
            pass
        # A driver may raise after the server committed.  Re-read authority in
        # a fresh unit of work before deciding whether the intent exists.
        try:
            with DBSession(bind=db.get_bind()) as authority:
                winner = authority.exec(select(ExportBatch).where(
                    ExportBatch.idempotency_key_hash == key_hash)).first()
                if winner is None:
                    winner = authority.exec(select(ExportBatch).where(
                        ExportBatch.export_scope_hash == export_scope_hash)).first()
                if winner is not None:
                    if (winner.idempotency_key_hash != key_hash
                            or winner.request_fingerprint != request_fingerprint
                            or winner.export_scope_hash != export_scope_hash):
                        raise ExportIdempotencyConflict(
                            "导出幂等键已并发绑定另一请求") from commit_error
                    return winner, True
        except ExportIdempotencyConflict:
            raise
        except Exception:
            pass
        raise commit_error


def _record_manifest_intent(
        db: DBSession, batch: ExportBatch, *, metadata: dict,
        manifest: dict, manifest_sha256: str,
        staging_owner_hash: str, session_id: str,
        config: export_security.DeidentificationConfig) -> ExportBatch:
    metadata_json = json.dumps(
        metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest_json = _canonical_json_bytes(manifest).decode("utf-8")
    with governance_lock.SUBJECT_EXPORT_WRITE_LOCK:
        _sess, _patient, locked, _audios = (
            _lock_and_revalidate_staging_authority(
                db, batch_id=batch.batch_id, session_id=session_id,
                config=config))
        if (locked.staging_owner_hash != staging_owner_hash
                or locked.staging_lease_expires_at is None):
            raise ExportArtifactIntegrityError(
                "staging 租约已失效，拒绝持久化 manifest 发布意图")
        locked.result_metadata_json = metadata_json
        locked.manifest_sha256 = manifest_sha256
        locked.publication_manifest_json = manifest_json
        # Manifest intent is the durable recovery authority from this point;
        # the pre-intent filesystem lease must disappear in the same commit.
        locked.staging_owner_hash = None
        locked.staging_lease_expires_at = None
        db.add(locked)
        try:
            db.commit()
            db.refresh(locked)
            return locked
        except Exception as commit_error:
            try:
                db.rollback()
            except Exception:
                pass
            try:
                with DBSession(bind=db.get_bind()) as authority:
                    winner = authority.get(ExportBatch, batch.batch_id)
                    if (winner is not None
                            and winner.request_fingerprint == batch.request_fingerprint
                            and winner.manifest_sha256 == manifest_sha256
                            and winner.publication_manifest_json == manifest_json
                            and winner.result_metadata_json == metadata_json
                            and winner.staging_owner_hash is None
                            and winner.staging_lease_expires_at is None):
                        return winner
            except Exception:
                pass
            raise commit_error


def _persist_artifacts_ready(
        db: DBSession, batch_id: str, descriptors: list[dict], *,
        manifest_descriptor: dict, now: datetime,
        write_dir: Optional[Path]) -> ExportBatch:
    batch = db.get(ExportBatch, batch_id)
    if batch is None:
        raise ExportArtifactIntegrityError("导出批次账本消失")
    if batch.status in {"artifacts_ready", "published"}:
        verify_export_batch(
            db, batch_id, write_dir=write_dir, allow_artifacts_ready=True)
        return batch
    if batch.status != "staging" or not batch.manifest_sha256:
        raise ExportArtifactIntegrityError("导出批次不在可恢复的 staging 状态")
    if (batch.staging_owner_hash is not None
            or batch.staging_lease_expires_at is not None):
        raise ExportArtifactIntegrityError("manifest 意图与 staging 租约不得并存")
    existing = list(db.exec(select(ExportArtifact).where(
        ExportArtifact.batch_id == batch_id)))
    if existing:
        raise ExportArtifactIntegrityError("staging 批次存在不完整 artifact 账本")
    for descriptor in [*descriptors, manifest_descriptor]:
        db.add(ExportArtifact(batch_id=batch_id, created_at=now, **descriptor))
    batch.status = "artifacts_ready"
    batch.artifacts_ready_at = now
    db.add(batch)
    try:
        db.commit()
        db.refresh(batch)
        return batch
    except Exception as commit_error:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            with DBSession(bind=db.get_bind()) as authority:
                verified = verify_export_batch(
                    authority, batch_id, write_dir=write_dir,
                    allow_artifacts_ready=True)
                return verified["batch"]
        except Exception:
            # Manifest is already durably published.  Never delete it on a DB
            # uncertainty; the staging intent and its manifest hash permit an
            # exact same-key recovery.
            raise RuntimeError(
                "artifact 账本提交结果不确定；已发布文件已保留，须用相同幂等键恢复",
            ) from commit_error


def _recover_staging_batch(
        db: DBSession, *, batch: ExportBatch, session_id: str,
        config: export_security.DeidentificationConfig, now: datetime,
        write_dir: Optional[Path]) -> dict:
    """Recover an exact published file set after an uncertain DB/file return."""
    if (batch.status != "staging" or not batch.manifest_sha256
            or not batch.publication_manifest_json
            or batch.staging_owner_hash is not None
            or batch.staging_lease_expires_at is not None):
        raise ExportArtifactIntegrityError("staging 批次没有可恢复的发布意图")
    try:
        manifest = json.loads(batch.publication_manifest_json)
    except ValueError as exc:
        raise ExportArtifactIntegrityError("staging 发布意图无法解析") from exc
    if (_canonical_json_bytes(manifest).decode("utf-8")
            != batch.publication_manifest_json
            or audio_store.sha256_hex(_canonical_json_bytes(manifest))
            != batch.manifest_sha256
            or _manifest_has_forbidden_content(manifest)
            or manifest.get("batch_id") != batch.batch_id
            or manifest.get("data_classification") != batch.data_classification
            or manifest.get("schema_version") != EXPORT_MANIFEST_SCHEMA_VERSION
            or manifest.get("deidentified") is not True
            or not isinstance(manifest.get("artifacts"), list)):
        raise ExportArtifactIntegrityError("staging 发布意图摘要或结构无效")
    metadata = _parse_result_metadata(batch.result_metadata_json)
    if manifest.get("result") != metadata:
        raise ExportArtifactIntegrityError("staging manifest 结果与批次账本不一致")
    descriptors: list[dict] = []
    for raw in manifest["artifacts"]:
        if not isinstance(raw, dict) or set(raw) != {
                "realm", "kind", "relative_path", "sha256", "byte_count"}:
            raise ExportArtifactIntegrityError("staging artifact 描述无效")
        descriptor = {
            "realm": raw["realm"], "kind": raw["kind"],
            "relative_path": raw["relative_path"], "sha256": raw["sha256"],
            "byte_count": raw["byte_count"],
        }
        path = _resolve_artifact_path(descriptor, batch=batch, write_dir=write_dir)
        if (not isinstance(descriptor["sha256"], str)
                or len(descriptor["sha256"]) != 64
                or not isinstance(descriptor["byte_count"], int)
                or descriptor["byte_count"] < 0
                or audio_store.sha256_file(path)
                != (descriptor["sha256"], descriptor["byte_count"])):
            raise ExportArtifactIntegrityError("staging artifact 与发布意图不一致")
        descriptors.append(descriptor)
    analysis_base = export_security.find_secure_existing_directory(
        Path(write_dir or EXPORT_DIR), batch.data_classification, batch.batch_id)
    if analysis_base is None:
        raise ExportArtifactIntegrityError("staging 分析目录缺失")
    manifest_path = analysis_base / "manifest.json"
    if not manifest_path.exists() and not manifest_path.is_symlink():
        export_security.atomic_write_json(manifest_path, manifest)
    if (manifest_path.is_symlink() or not manifest_path.is_file()
            or audio_store.sha256_file(manifest_path)[0]
            != batch.manifest_sha256):
        raise ExportArtifactIntegrityError("staging manifest 文件与发布意图不一致")
    manifest_descriptor = _file_receipt(
        manifest_path,
        realm=f"{batch.data_classification}_analysis",
        kind="manifest",
        root=Path(write_dir or EXPORT_DIR),
    )
    _persist_artifacts_ready(
        db, batch.batch_id, descriptors,
        manifest_descriptor=manifest_descriptor, now=now,
        write_dir=write_dir,
    )
    return _finalize_export_batch(
        db, batch_id=batch.batch_id, session_id=session_id,
        config=config, now=now, write_dir=write_dir,
    )


def _finalize_export_batch(
        db: DBSession, *, batch_id: str, session_id: str,
        config: export_security.DeidentificationConfig, now: datetime,
        write_dir: Optional[Path]) -> dict:
    with governance_lock.SUBJECT_EXPORT_WRITE_LOCK:
        return _finalize_export_batch_locked(
            db, batch_id=batch_id, session_id=session_id,
            config=config, now=now, write_dir=write_dir,
        )


def _finalize_export_batch_locked(
        db: DBSession, *, batch_id: str, session_id: str,
        config: export_security.DeidentificationConfig, now: datetime,
        write_dir: Optional[Path]) -> dict:
    _sess, _patient, locked_batch, audios = _lock_and_revalidate_export_authority(
        db, batch_id=batch_id, session_id=session_id, config=config,
        allow_artifacts_ready=True,
    )
    verified = verify_export_batch(
        db, batch_id, write_dir=write_dir, allow_artifacts_ready=True)
    batch: ExportBatch = verified["batch"]
    if batch is not locked_batch:
        raise ExportArtifactIntegrityError("导出批次锁定身份不一致")
    if batch.status == "published":
        return _verified_result(verified)
    metadata = verified["metadata"]
    expected_codes = set(metadata["audio_touched"])
    matched_codes: set[str] = set()
    controlled_rows = {
        Path(row["relative_path"]).name: row
        for row in verified["artifacts"] if row["kind"] == "controlled_audio"
    }
    for audio in audios:
        code = export_security.pseudonymize_audio(audio.raw_audio_id, config)
        belongs = code in expected_codes
        if belongs:
            matched_codes.add(code)
            if audio.status == AudioStatus.exported:
                if audio.export_batch_id != batch_id:
                    raise ExportArtifactIntegrityError(
                        "录音已由另一导出批次推进，拒绝发布")
                continue
            if audio.status != AudioStatus.recorded:
                raise ExportArtifactIntegrityError("录音状态不允许由本批次推进")
            candidates = [name for name in controlled_rows if name.startswith(f"{code}.")]
            if len(candidates) != 1 or not audio.checksum:
                raise ExportArtifactIntegrityError("录音与受控 artifact 无法一一核对")
            artifact = controlled_rows[candidates[0]]
            if artifact["sha256"] != audio.checksum:
                raise ExportArtifactIntegrityError("受控录音摘要与采集账本不一致")
        elif audio.status == AudioStatus.recorded:
            raise ExportArtifactIntegrityError("发现未纳入本批次的 recorded 录音")
    if matched_codes != expected_codes:
        raise ExportArtifactIntegrityError("批次音频清单与权威录音账本不一致")
    for audio in audios:
        code = export_security.pseudonymize_audio(audio.raw_audio_id, config)
        if code not in expected_codes or audio.status == AudioStatus.exported:
            continue
        asset = audio_gate.AudioAsset(
            audio.raw_audio_id, audio.status,
            audio.is_reliability_sample, audio.withdrawn,
        )
        audio_gate.mark_exported(asset)
        audio.status = asset.status
        audio.export_batch_id = batch_id
        audio.exported_at = now
        db.add(audio)
    batch.status = "published"
    batch.published_at = now
    db.add(batch)
    try:
        db.commit()
    except Exception as commit_error:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            with DBSession(bind=db.get_bind()) as authority:
                winner = authority.get(ExportBatch, batch_id)
                if winner is not None and winner.status == "published":
                    return _verified_result(verify_export_batch(
                        authority, batch_id, write_dir=write_dir))
        except Exception:
            pass
        raise RuntimeError(
            "导出最终事务结果不确定；文件与 artifacts_ready 账本已保留，请用相同幂等键重试",
        ) from commit_error
    db.expire_all()
    return _verified_result(verify_export_batch(db, batch_id, write_dir=write_dir))


def export_session_bundle(
        db: DBSession, session_id: str, *, idempotency_key: str,
        actor_display_id: str, actor_role: str, deidentify: bool = True,
        batch_id: Optional[str] = None, now: Optional[datetime] = None,
        write_dir: Optional[Path] = None) -> dict:
    """导出一场次的多维度数据集（默认去标识）。返回 {sheets, batch_id, files, audio_touched}。

    deidentify=True（唯一开放通道）：患者以 HMAC 假名出现，直接标识列一律不产，
    所有未具备服务端 PHI-reviewed 状态的自由文本固定为 ``[REDACTED]``。
    """
    sess = db.get(TrainSession, session_id)
    if not sess:
        raise ValueError(f"场次 {session_id} 不存在")
    if deidentify is not True:
        raise ValueError("非去标识导出通道尚未开放；不得通过普通接口生成直接标识或 crosswalk")
    expected_classification = "simulation" if sess.is_simulation else "research"
    if sess.data_classification != expected_classification:
        raise ValueError("场次尚未完成真实/模拟受控分类，拒绝导出")
    runtime_state = db.get(SessionRuntimeState, session_id)
    if runtime_state is None or runtime_state.status != "completed":
        raise ValueError("只有完成服务端终态门禁的场次才可导出")
    _require_export_completion_evidence(db, sess, runtime_state)
    patient = db.get(Patient, sess.patient_id)
    if patient is None:
        raise ValueError("场次缺少受试者档案，拒绝导出")
    if (patient.withdrawal_status or "").strip():
        raise ValueError("受试者已撤回，拒绝导出")
    if patient.secondary_use_allowed is not True:
        raise ValueError("secondary_use_allowed 未明确为 true，拒绝导出")
    now = now or datetime.now()
    validate_export_idempotency_key(idempotency_key)
    actor_display_id = actor_display_id.strip()
    if not actor_display_id or actor_role not in {"admin", "data_steward"}:
        raise ValueError("导出主体必须来自具名数据治理账号")
    supplied_batch_id = batch_id is not None
    if (batch_id is not None and (
            not _SAFE_BATCH_ID.fullmatch(batch_id)
            or batch_id in {".", ".."}
            or (supplied_batch_id and _ABSOLUTE_DATE_IN_BATCH_ID.search(batch_id)))):
        raise ValueError("导出批次号格式无效或含绝对日期")
    deidentification_config = export_security.load_deidentification_config()
    request_fingerprint = _request_fingerprint(
        deidentification_config,
        session_id=session_id,
        deidentify=deidentify,
        actor_display_id=actor_display_id,
        actor_role=actor_role,
    )
    export_scope_hash = _export_scope_hash(deidentification_config, session_id)
    # Only now is the request's own identity fully validated, so an existing
    # batch can be judged.  A same-key retry is a recovery only when every
    # identity fact closes; anything else stays an idempotency conflict and must
    # not be reinterpreted as an asset-lifecycle problem.
    existing_batch = _resolve_recoverable_batch(
        db,
        key_hash=_idempotency_hash(idempotency_key),
        request_fingerprint=request_fingerprint,
        export_scope_hash=export_scope_hash,
        requested_batch_id=batch_id,
        data_classification=sess.data_classification,
    )
    # Prove the repeat evidence before any batch row, artifact or file exists,
    # so a refusal leaves no residue to reconcile later.
    repeat_by_audio = _proved_repeat_requests_by_audio(
        db, session_id, existing_batch=existing_batch)
    preflight_repeat_fingerprint = _repeat_evidence_fingerprint(
        db, repeat_by_audio)
    batch, replay = _ensure_export_batch(
        db,
        requested_batch_id=batch_id,
        key_hash=_idempotency_hash(idempotency_key),
        request_fingerprint=request_fingerprint,
        export_scope_hash=export_scope_hash,
        data_classification=sess.data_classification,
        actor_display_id=actor_display_id,
        actor_role=actor_role,
        now=now,
    )
    batch_id = batch.batch_id
    if replay and batch.status == "published":
        return _finalize_export_batch(
            db, batch_id=batch_id, session_id=session_id,
            config=deidentification_config, now=now, write_dir=write_dir,
        )
    if replay and batch.status == "artifacts_ready":
        return _finalize_export_batch(
            db, batch_id=batch_id, session_id=session_id,
            config=deidentification_config, now=now, write_dir=write_dir,
        )
    if replay and batch.status == "staging" and batch.manifest_sha256:
        return _recover_staging_batch(
            db, batch=batch, session_id=session_id,
            config=deidentification_config, now=now, write_dir=write_dir,
        )
    subj = export_security.pseudonymize_subject(sess.patient_id, deidentification_config)
    session_code = export_security.pseudonymize_session(
        session_id, deidentification_config)

    items = list(db.exec(select(ItemEvent).where(ItemEvent.session_id == session_id)))
    turns_by_item: dict[int, list[TurnEvent]] = {}
    for it in items:
        rows = list(db.exec(select(TurnEvent).where(TurnEvent.item_event_id == it.id)
                            .order_by(TurnEvent.turn_seq)))
        turns_by_item[it.id] = rows

    audios = list(db.exec(select(AudioAssetRow).where(AudioAssetRow.session_id == session_id)))
    attempts = list(db.exec(select(AttemptEvent).where(
        AttemptEvent.session_id == session_id).order_by(
            AttemptEvent.item_id, AttemptEvent.turn_seq, AttemptEvent.attempt_seq)))
    interactions = list(db.exec(select(InteractionEvent).where(
        InteractionEvent.session_id == session_id).order_by(InteractionEvent.event_seq)))
    attempts_by_id = {
        attempt.id: attempt for attempt in attempts if attempt.id is not None
    }

    def subject_cols() -> dict:
        return {"subject_code": subj}

    def session_cols() -> dict:
        return {**subject_cols(), "session_code": session_code}

    def audio_code(raw_audio_id: str) -> str:
        return export_security.pseudonymize_audio(
            raw_audio_id, deidentification_config)

    # --- session 表 ---
    profile_definition = autopilot_plan_profiles.resolve_registered_binding(
        sess.autopilot_profile_version_id,
        sess.autopilot_profile_definition_digest,
    )
    completion_scope = (
        profile_definition.completion_scope
        if profile_definition is not None
        else "canonical_full_source"
    )
    session_sheet = [{**session_cols(), "week_no": sess.week_no,
                      "is_simulation": sess.is_simulation,
                      "data_classification": sess.data_classification,
                      "phase_type": _v(sess.phase_type), "event_line": _v(sess.event_line),
                      "session_sitting_no": sess.session_sitting_no,
                      "item_bank_version_id": sess.item_bank_version_id,
                      "autopilot_profile_version_id": (
                          sess.autopilot_profile_version_id),
                      "completion_scope": completion_scope,
                      "pseudonym_version": export_security.PSEUDONYM_VERSION,
                      "pseudonym_key_id": deidentification_config.key_id,
                      "dementia_severity": getattr(patient, "dementia_severity", None),
                      "mandarin_eligible": getattr(patient, "mandarin_eligible", None),
                      "is_simulation_subject": getattr(patient, "is_simulation_subject", False)}]

    # --- turns 明细（去标识文本）---
    turn_sheet = []
    for it in items:
        for t in turns_by_item.get(it.id, []):
            asr = export_security.redact_free_text(t.asr_text)
            conf = export_security.redact_free_text(t.confirmed_response_text)
            source_attempt_seq = None
            if t.source_attempt_id is not None:
                source_attempt = attempts_by_id.get(t.source_attempt_id)
                if (source_attempt is None
                        or source_attempt.item_id != it.item_id
                        or source_attempt.turn_seq != t.turn_seq):
                    raise ValueError("环节关联的原始 attempt 不属于本场次/本环节，拒绝导出")
                source_attempt_seq = source_attempt.attempt_seq
            turn_sheet.append({
                **session_cols(), "item_id": it.item_id,
                "task_type": _v(it.task_type), "turn_seq": t.turn_seq,
                "response_role": t.response_role,
                "source_attempt_seq": source_attempt_seq,
                "asr_text": asr, "confirmed_response_text": conf,
                "asr_confidence": t.asr_confidence, "prompt_level": t.prompt_level,
                # 本环节收音时长（秒）。它是相对量，不是绝对时间，也**不是反应时**——
                # 反应时要等钱凯定「潜伏期从哪一刻起算」。原来只有 attempts.csv 带它，
                # 而那张表自己盖的章是 truth_scope=operational_only。
                "duration_seconds": t.duration_seconds,
                "ai_answer_type": t.ai_answer_type, "ai_score": t.ai_score,
                "ai_needs_review": t.ai_needs_review,
                "ai_judge_mode": t.ai_judge_mode,
                "reviewed_score": t.reviewed_score, "score_locked": t.score_locked,
                "element_value": t.element_value,
                "ai_human_diff": (None if t.reviewed_score is None or t.ai_score is None
                                  else round(t.reviewed_score - t.ai_score, 4)),
                "judge_portrait_used": t.judge_portrait_used})

    # --- 重建评分汇总 ---
    scores = _reconstruct_scores(items, turns_by_item)
    score_sheet = [_score_row(session_cols(), tt, scores[key])
                   for tt, key in (("单要素", "single"), ("双要素", "double"), ("多要素", "multi"))
                   if scores[key]]

    # --- 正式结局量表 ---
    # ScaleResult 是选型冻结前的自由名称/自由分数容器，没有工具版本、
    # 评分规则和服务端验证链。它不得冒充正式研究结局；``scales`` 在新的
    # 版本化量表模型上线前固定为空。为了不静默丢失历史数据，旧行只进
    # 明确的 legacy_unverified 工作表，且不使用 scale_name 等正式语义列名。
    scale_rows = list(db.exec(select(ScaleResult).where(ScaleResult.patient_id == sess.patient_id)))
    scale_sheet: list[dict] = []
    legacy_unverified_scale_sheet = [{
        **subject_cols(),
        "phase_type": _v(s.phase_type),
        "legacy_reported_label": s.scale_name,
        "legacy_reported_subscale": s.subscale,
        "legacy_reported_score": s.score,
        "verification_status": "legacy_unverified",
        "formal_outcome_eligible": False,
        "source_schema": "legacy_free_form_scale_result",
    } for s in scale_rows]

    # --- 量表电子记录（原型道）---
    # 撤回口径与 ScaleResult 相同：本函数入口已对撤回受试者整体拒绝导出，
    # 记录按 patient_id 归属取数，不设旁路。note / ai_draft_rationale /
    # created_by / locked_by 与全部绝对时间列一律不产；两表用同一域的
    # HMAC 假名 record_code 连接，裸 record_id 不出。
    def questionnaire_record_code(record_id: str) -> str:
        return export_security.pseudonymize_questionnaire_record(
            record_id, deidentification_config)

    questionnaire_records = list(db.exec(
        select(QuestionnaireRecord)
        .where(QuestionnaireRecord.patient_id == sess.patient_id)
        .order_by(QuestionnaireRecord.record_id)))
    questionnaire_record_sheet = [{
        **subject_cols(),
        "record_code": questionnaire_record_code(record.record_id),
        "questionnaire_id": record.questionnaire_id,
        "definition_sha256": record.definition_sha256,
        "phase_label": record.phase_label,
        # 同期别重测的两列。不发出去，拿导出包的人就分不清同为「前测」的两行
        # 哪条作数——而作废那条同样带总分，正是最容易被取错的一行。
        "phase_ordinal": record.phase_ordinal,
        "superseded_by_ordinal": record.superseded_by_ordinal,
        "status": record.status,
        "ai_draft_status": record.ai_draft_status,
        "ai_draft_engine": record.ai_draft_engine,
        "computed_total": record.computed_total,
        "cutoff_met": record.cutoff_met,
        "computed_flag": record.computed_flag,
        "scoring_rule_id": record.scoring_rule_id,
    } for record in questionnaire_records]
    questionnaire_item_value_sheet: list[dict] = []
    if questionnaire_records:
        questionnaire_values = list(db.exec(
            select(QuestionnaireItemValue)
            .where(QuestionnaireItemValue.record_id.in_(
                [record.record_id for record in questionnaire_records]))
            .order_by(QuestionnaireItemValue.record_id,
                      QuestionnaireItemValue.item_key,
                      QuestionnaireItemValue.field_key)))
        questionnaire_item_value_sheet = [{
            "record_code": questionnaire_record_code(value.record_id),
            "item_key": value.item_key,
            "field_key": value.field_key,
            "ai_draft_value": value.ai_draft_value,
            "final_value": value.final_value,
            "value_source": value.value_source,
        } for value in questionnaire_values]

    # --- 异常/介入 ---
    abn_rows = list(db.exec(select(AbnormalEvent).where(AbnormalEvent.session_id == session_id)))
    abn_sheet = [{**session_cols(), "phase_type": _v(a.phase_type),
                  "abnormal_type": a.abnormal_type, "intervention_type": a.intervention_type,
                  "affects_scoring_validity": a.affects_scoring_validity,
                  "note": export_security.redact_free_text(a.note)}
                 for a in abn_rows]

    # --- 逐次 AI operational 证据（永不冒充人工锁定研究真值）---
    attempt_sheet = []
    for attempt in attempts:
        attempt_sheet.append({
            **session_cols(),
            "item_id": attempt.item_id,
            "turn_seq": attempt.turn_seq,
            "response_role": attempt.response_role,
            "attempt_seq": attempt.attempt_seq,
            "audio_code": audio_code(attempt.raw_audio_id),
            "prompt_level": attempt.prompt_level,
            "cue_type": attempt.cue_type,
            "duration_seconds": attempt.duration_seconds,
            "asr_text": export_security.redact_free_text(attempt.asr_text),
            "asr_confidence": attempt.asr_confidence,
            "asr_engine_version": attempt.asr_engine_version,
            "operational_answer_type": attempt.operational_answer_type,
            "operational_score": attempt.operational_score,
            "operational_needs_review": attempt.operational_needs_review,
            "judge_mode": attempt.judge_mode,
            "judge_engine_version": attempt.judge_engine_version,
            "judge_reason": export_security.redact_free_text(attempt.judge_reason),
            "matched_on": attempt.matched_on,
            "contains_target": attempt.contains_target,
            "judge_portrait_used": attempt.judge_portrait_used,
            "processing_status": attempt.processing_status,
            "error_code": attempt.error_code,
            "is_simulation": attempt.is_simulation,
            "truth_scope": "operational_only",
        })

    interaction_sheet = []
    for interaction in interactions:
        # 对 DB 中既有/被篡改的 payload 重过白名单，违规就拒绝导出。
        payload = evidence_ledger.validate_stored_payload(
            interaction.event_type, interaction.payload_json)
        payload_audio_id = payload.pop("raw_audio_id", None)
        linked_attempt = None
        if interaction.attempt_id is not None:
            linked_attempt = attempts_by_id.get(interaction.attempt_id)
            if (linked_attempt is None
                    or interaction.item_id != linked_attempt.item_id
                    or interaction.turn_seq != linked_attempt.turn_seq
                    or interaction.attempt_seq != linked_attempt.attempt_seq):
                raise ValueError("interaction 关联的 attempt 不属于本场次/本环节，拒绝导出")
        elif interaction.attempt_seq is not None:
            raise ValueError("interaction 缺失权威 attempt 关联，拒绝导出")
        if payload_audio_id is not None:
            if linked_attempt is None or payload_audio_id != linked_attempt.raw_audio_id:
                raise ValueError("interaction payload 音频标识与权威 attempt 不一致，拒绝导出")
        safe_payload = evidence_ledger.encode_event_payload(
            interaction.event_type, payload)
        interaction_sheet.append({
            **session_cols(),
            "event_seq": interaction.event_seq,
            "item_id": interaction.item_id,
            "turn_seq": interaction.turn_seq,
            "attempt_seq": interaction.attempt_seq,
            "audio_code": (
                audio_code(linked_attempt.raw_audio_id) if linked_attempt else None),
            "event_type": interaction.event_type,
            "payload_json": safe_payload,
            "is_simulation": interaction.is_simulation,
            "truth_scope": "operational_only",
        })

    # --- 音频清单 + 导出候选 ---
    # 去标识分析包只放元数据清单，原始声纹始终另存受控目录。
    # 只有在源字节存在、已登记 checksum 且受控副本成功后，才会推进 recorded→exported。
    audio_sheet, touched = [], []
    repeat_audio_sheet: list[dict] = []
    export_candidates: list[tuple[AudioAssetRow, Path]] = []
    # An explicit repeat produces a real recording with no AttemptEvent.  The
    # controlled manifest classifies it with closed metadata only, so the
    # exported bytes never become an unattributed file.  Phrase keys,
    # normalized-text digests and internal command/capture ids stay out.
    for a in audios:
        if (a.is_simulation != sess.is_simulation
                or a.data_classification != sess.data_classification):
            raise ValueError(f"音频 {a.raw_audio_id} 与场次数据分类不一致，拒绝导出")
        if a.withdrawn or (a.withdrawal_status or "").strip():
            raise ValueError(f"音频 {a.raw_audio_id} 已撤回或隔离，拒绝导出")
        source_blob = audio_store.find_blob(a.raw_audio_id)
        if a.status == AudioStatus.recorded and source_blob is None:
            raise ValueError("录音源文件缺失，拒绝导出 recorded 资产")
        will_export = a.status == AudioStatus.recorded and source_blob is not None
        if will_export:
            if not a.checksum:
                raise ValueError(f"音频 {a.raw_audio_id} 缺采集期 checksum，拒绝标记导出")
            export_candidates.append((a, source_blob))
        effective_export_batch_id = batch_id if will_export else a.export_batch_id
        repeat_request = repeat_by_audio.get(a.raw_audio_id)
        if repeat_request is not None:
            # Governed projection for an explicit repeat is metadata-only and
            # is exactly the four approved fields — nothing about the session,
            # subject, turn, format or lifecycle joins it.
            repeat_audio_sheet.append({
                "capture_kind": "explicit_repeat",
                "repeat_ordinal": repeat_request.repeat_ordinal,
                "outcome": repeat_request.outcome,
                "opaque_audio_code": audio_code(a.raw_audio_id),
            })
            continue
        audio_sheet.append({"audio_code": audio_code(a.raw_audio_id),
                            "session_code": session_code,
                            "subject_code": subj,
                            "is_simulation": a.is_simulation,
                            "data_classification": a.data_classification,
                            "turn_key": a.turn_key,
                            "audio_format": a.audio_format,
                            "status": _v(AudioStatus.exported if will_export else a.status),
                            "is_reliability_sample": a.is_reliability_sample,
                            "contains_direct_identifier": a.contains_direct_identifier,
                            "export_batch_code": (
                                export_security.pseudonymize_batch(
                                    effective_export_batch_id, deidentification_config)
                                if effective_export_batch_id else None),
                            "controlled_audio_exported": will_export})

    # 表名闭集：漏声明一张表 → _write_csvs 会拒收；声明了却从来不产 → 分析者
    # 拿到的包永远少一张而没人报错。两个方向都在这里当场炸。
    sheets = {"session": session_sheet, "turns": turn_sheet, "attempts": attempt_sheet,
              "interactions": interaction_sheet, "item_scores": score_sheet,
              "scales": scale_sheet,
              "legacy_unverified_scales": legacy_unverified_scale_sheet,
              "questionnaire_records": questionnaire_record_sheet,
              "questionnaire_item_values": questionnaire_item_value_sheet,
              "abnormal": abn_sheet, "audio_manifest": audio_sheet,
              "repeat_audio_manifest": repeat_audio_sheet}
    if set(sheets) != set(SHEET_FIELDS):
        raise ValueError(
            "导出表名与 SHEET_FIELDS 不一致："
            f"多出 {sorted(set(sheets) - set(SHEET_FIELDS))}，"
            f"缺 {sorted(set(SHEET_FIELDS) - set(sheets))}")
    for row in repeat_audio_sheet:
        if set(row) != set(REPEAT_AUDIO_MANIFEST_FIELDS):
            raise ValueError("重复请求音频清单字段不等于批准的四项白名单，拒绝导出")
    _assert_no_direct_identifiers(sheets)

    # 顺序是数据保护边界：先完整写分析 CSV 与受控音频，再把预期 manifest
    # 摘要持久化，最后原子发布 manifest。artifact 账本、recorded→exported 与
    # published 状态分别由可恢复事务收口；任何中间失败都不得盲推删除闸门。
    # Close the window between preflight and the first write.  Comparing keys
    # alone would miss a swap that keeps the same recordings but changes what
    # they are bound to, so the whole fingerprint is compared; the identity map
    # is expired first so an external write cannot be masked by cached rows.
    _repeat_evidence_reproof_boundary()
    db.expire_all()
    if _repeat_evidence_fingerprint(
            db, _proved_repeat_requests_by_audio(
                db, session_id, existing_batch=existing_batch)) != (
                preflight_repeat_fingerprint):
        raise ValueError("重复请求证据集合在导出准备期间发生变化，拒绝导出")
    csv_root = Path(write_dir or EXPORT_DIR)
    controlled_root = Path(
        CONTROLLED_AUDIO_DIR if write_dir is None else write_dir / "_controlled_audio")
    csv_base = csv_root / sess.data_classification / batch_id
    audio_files: list[str] = []
    artifact_descriptors: list[dict] = []
    publication_intent_recorded = False
    staging_owner_hash = _new_staging_owner_hash()
    staging_lease_claimed = False
    try:
        batch, previous_owner_hash = _claim_staging_filesystem(
            db, batch_id=batch_id, session_id=session_id,
            config=deidentification_config, owner_hash=staging_owner_hash)
        staging_lease_claimed = True
        _prepare_staging_filesystem(
            csv_root=csv_root, controlled_root=controlled_root,
            data_classification=sess.data_classification, batch_id=batch_id,
            previous_owner_hash=previous_owner_hash,
        )

        def lease_guard() -> None:
            _check_staging_lease(
                db, batch_id=batch_id, owner_hash=staging_owner_hash)

        written_csvs, staging_receipt = _write_csvs(
            sheets, batch_id, write_dir, sess.data_classification,
            staging_owner_hash=staging_owner_hash,
            lease_guard=lease_guard,
        )
        analysis_realm = f"{sess.data_classification}_analysis"
        controlled_realm = f"{sess.data_classification}_controlled_audio"
        artifact_descriptors.append(_file_receipt(
            staging_receipt, realm=analysis_realm, kind="staging_receipt",
            root=csv_root,
        ))
        artifact_descriptors.extend(
            _file_receipt(Path(path), realm=analysis_realm, kind="csv", root=csv_root)
            for path in written_csvs
        )
        if export_candidates:
            lease_guard()
            controlled_classification = export_security.ensure_secure_directory(
                controlled_root, sess.data_classification)
            secured_batch = export_security.ensure_secure_directory(
                controlled_classification, batch_id, exclusive_final=True)
            secured_audio = export_security.ensure_secure_directory(
                secured_batch, "audio")
            for a, source in export_candidates:
                lease_guard()
                suffix = source.suffix.lower()
                if not _SAFE_AUDIO_SUFFIX.fullmatch(suffix):
                    raise RuntimeError("音频源文件扩展名不在受控范围，拒绝导出")
                exported_name = f"{audio_code(a.raw_audio_id)}{suffix}"
                dst = secured_audio / exported_name
                export_security.atomic_copy_file(source, dst)
                copied_digest, _copied_bytes = audio_store.sha256_file(dst)
                if copied_digest != a.checksum:
                    raise RuntimeError(f"音频 {a.raw_audio_id} 导出副本 checksum 不一致，拒绝推进状态")
                audio_files.append(exported_name)
                artifact_descriptors.append(_file_receipt(
                    dst, realm=controlled_realm, kind="controlled_audio",
                    root=controlled_root,
                ))
                touched.append(audio_code(a.raw_audio_id))

        lease_guard()
        metadata = {
            "audio_touched": sorted(touched),
            "excluded_items": scores["excluded_items"],
            "sheet_counts": {name: len(rows) for name, rows in sheets.items()},
        }
        manifest = _canonical_manifest(batch, artifact_descriptors, metadata)
        manifest_bytes = _canonical_json_bytes(manifest)
        manifest_sha256 = audio_store.sha256_hex(manifest_bytes)
        batch = _record_manifest_intent(
            db, batch, metadata=metadata, manifest=manifest,
            manifest_sha256=manifest_sha256,
            staging_owner_hash=staging_owner_hash,
            session_id=session_id, config=deidentification_config,
        )
        publication_intent_recorded = True
        manifest_path = csv_base / "manifest.json"
        export_security.atomic_write_json(manifest_path, manifest)
        if audio_store.sha256_file(manifest_path)[0] != manifest_sha256:
            raise ExportArtifactIntegrityError("发布后的 manifest 摘要不一致")
        manifest_descriptor = _file_receipt(
            manifest_path, realm=analysis_realm, kind="manifest", root=csv_root)
        _persist_artifacts_ready(
            db, batch_id, artifact_descriptors,
            manifest_descriptor=manifest_descriptor, now=now,
            write_dir=write_dir,
        )
        result = _finalize_export_batch(
            db, batch_id=batch_id, session_id=session_id,
            config=deidentification_config, now=now, write_dir=write_dir,
        )
        # ``sheets`` is retained only for trusted in-process analysis/tests.  The
        # HTTP projection below returns counts and relative artifact receipts.
        result.update({"sheets": sheets, "audio_files": audio_files})
        return result
    except Exception as export_error:
        # manifest intent 一经持久化就可能已发布，绝不能因 commit 回包不确定
        # 删除文件。没有 intent 时才清理本调用确认新建的半成品目录。
        try:
            db.rollback()
        except Exception:
            pass
        if not publication_intent_recorded:
            try:
                with DBSession(bind=db.get_bind()) as authority:
                    authoritative = authority.get(ExportBatch, batch_id)
                    publication_intent_recorded = bool(
                        authoritative is not None
                        and authoritative.manifest_sha256
                        and authoritative.publication_manifest_json
                    )
            except Exception:
                # Unknown DB authority means deletion is unsafe.  Preserve the
                # files for operator/retry reconciliation.
                publication_intent_recorded = True
        if publication_intent_recorded:
            raise
        cleanup_errors = []
        if staging_lease_claimed:
            try:
                if not isinstance(export_error, UnownedStagingPathError):
                    _check_staging_lease(
                        db, batch_id=batch_id, owner_hash=staging_owner_hash)
                    _cleanup_owned_staging_filesystem(
                        csv_root=csv_root, controlled_root=controlled_root,
                        data_classification=sess.data_classification,
                        batch_id=batch_id, owner_hash=staging_owner_hash,
                    )
                _release_staging_filesystem(
                    db, batch_id=batch_id, owner_hash=staging_owner_hash)
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            raise RuntimeError("导出失败且新建批次未能完全清理，须隔离检查") from export_error
        raise


# The frozen governance contract for explicit-repeat audio: metadata only, and
# exactly these four fields.  A repeat recording has no AttemptEvent, so without
# this classification its controlled bytes would export as an unattributed file.
# Phrase keys, normalized-text digests and internal command/capture/repeat ids
# are never part of it.
REPEAT_AUDIO_MANIFEST_FIELDS = (
    "capture_kind", "repeat_ordinal", "outcome", "opaque_audio_code",
)
REPEAT_AUDIO_MANIFEST_FORBIDDEN_FIELDS = frozenset({
    "phrase_key", "normalized_text_sha256", "source_payload_sha256",
    "capture_processing_id", "repeat_request_id", "record_command_id",
    "source_tts_command_id", "replay_command_id", "raw_audio_id",
    "pause_control_event_seq",
})


def _repeat_evidence_reproof_boundary() -> None:
    """Explicit seam between the two repeat proofs.

    A no-op in production.  It exists so a test can inject a concurrent change
    exactly in the window the pre-write re-proof is meant to close, without
    reordering any real validation to create that window.
    """


def _resolve_recoverable_batch(
        db, *, key_hash: str, request_fingerprint: str, export_scope_hash: str,
        requested_batch_id: Optional[str], data_classification: str,
) -> "ExportBatch | None":
    """Pure read: the existing batch this request may legitimately recover.

    A row that shares the key but not the rest of the request identity is an
    idempotency conflict, and it is raised *here*.  Deferring it to
    :func:`_ensure_export_batch` would not work: the repeat preflight runs in
    between, so an already-published recording would first fail the fresh
    capture rule and surface as an asset-lifecycle error instead — wrong
    exception type and wrong boundary.  The conditions are exactly the ones
    :func:`_ensure_export_batch` uses, just evaluated earlier.

    ``None`` means "not a recovery": either no such key, or the identity closes
    but the batch has not reached a stage where its recordings have advanced.
    """
    existing = db.exec(select(ExportBatch).where(
        ExportBatch.idempotency_key_hash == key_hash)).first()
    if existing is None:
        # Same session under a different key is the other ordering with the same
        # defect: it too must be an idempotency conflict, not a lifecycle error.
        scope_winner = db.exec(select(ExportBatch).where(
            ExportBatch.export_scope_hash == export_scope_hash)).first()
        if scope_winner is not None:
            raise ExportIdempotencyConflict(
                "该场次已有导出意图；必须使用原幂等键恢复，不能创建第二批次")
        return None
    if (existing.request_fingerprint != request_fingerprint
            or existing.export_scope_hash != export_scope_hash
            or (requested_batch_id is not None
                and existing.batch_id != requested_batch_id)):
        raise ExportIdempotencyConflict("导出幂等键已绑定另一请求")
    # Identity closes, so this really is the same request.  A batch whose
    # classification no longer matches the session is a corrupt authority row,
    # not a fresh export: report it as the integrity failure the published-read
    # path already reports, never as an asset-lifecycle error.
    if existing.data_classification != data_classification:
        raise ExportArtifactIntegrityError("导出批次与场次数据分类不一致")
    if existing.status not in {"published", "artifacts_ready"}:
        return None
    return existing


def _row_fingerprint(row, *, identity) -> tuple:
    """Every column of one row, in the table's own declared order.

    Listing columns by hand lets a newly added one silently drop out of the
    concurrency window; driving it off ``__table__.columns`` cannot.  A missing
    row still contributes an identity sentinel so its disappearance is visible.
    """
    if row is None:
        return ("missing", identity)
    table = type(row).__table__
    return tuple(
        (column.name, getattr(row, column.name, None))
        for column in table.columns
    )


def _command_fingerprint(db, command_id) -> tuple:
    return _row_fingerprint(
        None if command_id is None else db.get(RuntimeCommand, command_id),
        identity=("runtimecommand", command_id))


def _session_fingerprint(db, session_id: str) -> tuple:
    return _row_fingerprint(
        db.get(TrainSession, session_id), identity=("session", session_id))


def _pause_event_fingerprint(db, session_id: str, event_seq) -> tuple | None:
    if event_seq is None:
        return None
    row = db.exec(select(AutopilotControlEvent).where(
        AutopilotControlEvent.session_id == session_id,
        AutopilotControlEvent.event_seq == event_seq)).first()
    return _row_fingerprint(
        row, identity=("autopilotcontrolevent", session_id, event_seq))


def _acks_fingerprint(db, command_ids) -> tuple:
    rows = []
    for command_id in command_ids:
        if command_id is None:
            continue
        for ack in db.exec(select(RuntimeCommandAck).where(
                RuntimeCommandAck.command_id == command_id)):
            rows.append(_row_fingerprint(
                ack, identity=("runtimecommandack", ack.id)))
    return tuple(sorted(rows, key=repr))


def _chain_capability_tokens(db, request) -> tuple:
    tokens = []
    for command_id in (request.record_command_id, request.source_tts_command_id,
                       request.replay_command_id):
        if command_id is None:
            continue
        row = db.get(RuntimeCommand, command_id)
        if row is not None:
            tokens.append(row.issued_capability_token_hash)
        for ack in db.exec(select(RuntimeCommandAck).where(
                RuntimeCommandAck.command_id == command_id)):
            tokens.append(ack.capability_token_hash)
    return tuple(tokens)


def _capabilities_fingerprint(db, token_hashes) -> tuple:
    """The current authority rows the commands and ACKs are bound to."""
    return tuple(
        _row_fingerprint(
            db.get(PatientDeviceCapability, token_hash),
            identity=("patientdevicecapability", token_hash))
        for token_hash in sorted({h for h in token_hashes if h})
    )


def _repeat_evidence_fingerprint(
        db, indexed: dict[str, AutopilotRepeatRequest]) -> tuple:
    """A stable, order-free identity for one session's whole repeat evidence set."""
    rows = []
    for raw_audio_id, request in indexed.items():
        capture = db.get(AttemptCaptureProcessing, request.capture_processing_id)
        asset = db.get(AudioAssetRow, raw_audio_id)
        receipt = (None if capture is None
                   else db.get(AudioCaptureReceipt, capture.receipt_server_seq))
        rows.append((
            raw_audio_id,
            _row_fingerprint(
                request, identity=("autopilotrepeatrequest", request.id)),
            _row_fingerprint(
                capture,
                identity=("attemptcaptureprocessing",
                          request.capture_processing_id)),
            _row_fingerprint(asset, identity=("audioassetrow", raw_audio_id)),
            _row_fingerprint(
                receipt,
                identity=("audiocapturereceipt",
                          None if capture is None else capture.receipt_server_seq)),
            _command_fingerprint(db, request.record_command_id),
            _command_fingerprint(db, request.source_tts_command_id),
            _command_fingerprint(db, request.replay_command_id),
            _session_fingerprint(db, request.session_id),
            _pause_event_fingerprint(
                db, request.session_id, request.pause_control_event_seq),
            _acks_fingerprint(db, (request.record_command_id,
                                   request.source_tts_command_id,
                                   request.replay_command_id)),
            _capabilities_fingerprint(db, _chain_capability_tokens(db, request)),
        ))
    return tuple(sorted(rows, key=repr))


def _proved_repeat_requests_by_audio(
        db, session_id: str, *,
        existing_batch: "ExportBatch | None" = None,
) -> dict[str, AutopilotRepeatRequest]:
    """Index this session's repeat recordings, capture-first and proved both ways.

    The truth source is the session's own terminal repeat-disposition captures,
    not the ledger: a ledger row carrying a foreign ``session_id`` must never be
    able to hide a repeat capture and let its bytes export as ordinary answer
    audio.  Every capture is fully re-verified through the shared evidence
    module, and the two directions must then agree exactly — same ids, same
    counts — or the whole export refuses.
    """
    captures = list(db.exec(select(AttemptCaptureProcessing).where(
        AttemptCaptureProcessing.session_id == session_id,
        or_(
            AttemptCaptureProcessing.repeat_request_id.is_not(None),
            AttemptCaptureProcessing.disposition.in_(
                ("repeat_replayed", "repeat_limit_paused")),
        ),
    )))
    indexed: dict[str, AutopilotRepeatRequest] = {}
    capture_ids: set[int] = set()
    request_ids: set[int] = set()
    for capture in captures:
        try:
            chain = repeat_evidence.verify_repeat_capture(db, capture)
        except repeat_evidence.RepeatEvidenceError as exc:
            raise ValueError(f"重复请求证据链不成立，拒绝导出：{exc}") from exc
        request = chain.request
        if request.session_id != session_id:
            raise ValueError("重复请求账本属于另一场次，拒绝导出")
        if request.raw_audio_id in indexed:
            raise ValueError("同一录音绑定了多条重复请求账本，拒绝导出")
        indexed[request.raw_audio_id] = request
        capture_ids.add(capture.id)
        request_ids.add(request.id)

    # Two more independent directions, then all three compared exactly.
    #
    # ``by_session`` catches a ledger row that claims this session.  ``by_pointer``
    # additionally catches a ledger row belonging to *another* session whose
    # capture pointer reaches into this one — that row would otherwise stay
    # invisible here while its capture's own pointer/disposition had been wiped,
    # letting a repeat recording export as ordinary answer audio.
    by_session = list(db.exec(select(AutopilotRepeatRequest).where(
        AutopilotRepeatRequest.session_id == session_id)))
    by_pointer = list(db.exec(
        select(AutopilotRepeatRequest)
        .join(AttemptCaptureProcessing,
              AutopilotRepeatRequest.capture_processing_id
              == AttemptCaptureProcessing.id)
        .where(AttemptCaptureProcessing.session_id == session_id)))
    if ({row.id for row in by_session} != request_ids
            or {row.id for row in by_pointer} != request_ids):
        raise ValueError("重复请求账本与采集处理行不是一一对应，拒绝导出")
    if ({row.capture_processing_id for row in by_session} != capture_ids
            or {row.capture_processing_id for row in by_pointer} != capture_ids):
        raise ValueError("重复请求账本反向指针与采集处理行不一致，拒绝导出")
    if (len(by_session) != len(captures) or len(by_pointer) != len(captures)
            or len(indexed) != len(captures)):
        raise ValueError("重复请求账本与采集处理行数量不一致，拒绝导出")
    # One recording is either an answer or a repeat request, never both — and
    # the Attempt may live in any session, so this query is deliberately global.
    if indexed and db.exec(select(AttemptEvent).where(
            AttemptEvent.raw_audio_id.in_(tuple(indexed)))).first() is not None:
        raise ValueError("同一录音同时绑定回答与重复请求，拒绝导出")

    # Every repeat recording must belong to *this* session's audio set and close
    # over its receipt and its bytes.  Without this an asset moved to another
    # session would silently drop out of the audio loop below, and its repeat
    # recording would never be classified at all.
    train_session = db.get(TrainSession, session_id)
    if train_session is None:
        raise ValueError("场次不存在，拒绝导出")
    session_audio = {
        row.raw_audio_id: row for row in db.exec(select(AudioAssetRow).where(
            AudioAssetRow.session_id == session_id))
    }
    if set(indexed) - set(session_audio):
        raise ValueError("重复请求录音不属于本场次音频集合，拒绝导出")
    for raw_audio_id, request in indexed.items():
        asset = session_audio[raw_audio_id]
        capture = db.get(AttemptCaptureProcessing, request.capture_processing_id)
        if capture is None:
            raise ValueError("重复请求账本失去其采集处理行，拒绝导出")
        receipt = db.get(AudioCaptureReceipt, capture.receipt_server_seq)
        if (receipt is None or receipt.session_id != session_id
                or receipt.raw_audio_id != raw_audio_id
                or asset.checksum != receipt.checksum
                or asset.byte_count != receipt.byte_count
                or asset.data_classification != receipt.data_classification
                or asset.is_simulation != receipt.is_simulation
                or asset.data_classification != train_session.data_classification
                or asset.is_simulation != train_session.is_simulation
                or receipt.data_classification != train_session.data_classification
                or receipt.is_simulation != train_session.is_simulation):
            raise ValueError("重复请求录音与其采集回执/场次分类不闭合，拒绝导出")
        record = db.get(RuntimeCommand, request.record_command_id)
        if (record is None or asset.turn_key != receipt.turn_key
                or receipt.turn_key != record.turn_key):
            raise ValueError("重复请求录音与其环节键不闭合，拒绝导出")
        # A repeat recording must be in the exact state this export expects.
        # For a new batch that is a fresh capture; for a recovery replay of an
        # already-advanced batch it is that same batch's exported recording.
        # Neither branch relaxes the other.
        # Only a *published* batch has advanced its recordings.  An
        # ``artifacts_ready`` batch has files and artifact rows but has not
        # finalized, so its assets are still ``recorded`` and must satisfy the
        # ordinary fresh-capture rule.
        recovering = (existing_batch is not None
                      and existing_batch.status == "published")
        common = (
            asset.uploaded_at is not None,
            asset.checksum is not None,
            asset.byte_count is not None,
            asset.delete_gate_passed is False,
            asset.withdrawn is False,
            not (asset.withdrawal_status or "").strip(),
            asset.contains_direct_identifier == receipt.contains_direct_identifier,
        )
        if recovering:
            lifecycle = (
                asset.status == AudioStatus.exported,
                asset.export_batch_id == existing_batch.batch_id,
                asset.exported_at is not None,
            )
            message = "重复请求录音与其已发布批次的导出状态不一致，拒绝导出"
        else:
            lifecycle = (
                asset.status == AudioStatus.recorded,
                asset.exported_at is None,
                asset.export_batch_id is None,
            )
            message = "重复请求录音不处于可导出的全新采集状态，拒绝导出"
        if not all(common + lifecycle):
            raise ValueError(message)
        # Bytes land before the stopped ACK writes the receipt, never after.
        if asset.uploaded_at > receipt.received_at:
            raise ValueError("重复请求录音上传时间晚于其采集回执，拒绝导出")
        blob = audio_store.find_blob(raw_audio_id)
        if blob is None:
            raise ValueError("重复请求录音源文件缺失，拒绝导出")
        # One streaming pass yields both the digest and the true length.
        blob_digest, blob_size = audio_store.sha256_file(blob)
        if blob_size != asset.byte_count:
            raise ValueError("重复请求录音文件大小与采集账本不一致，拒绝导出")
        if blob_digest != asset.checksum:
            raise ValueError("重复请求录音字节与采集期校验和不一致，拒绝导出")
    return indexed


def _assert_no_direct_identifiers(sheets: dict) -> None:
    """默认包最终自检：直接标识列与未经审核自由文本均 fail closed。"""
    export_security.assert_deidentified_sheets(sheets)


# 场次级结局：七个指标各占一列，提示层级分布展成 prompt_level_0..3。
# 原来它们被 `_flat` 压成一个 `summary` 字符串列，进 SPSS/R 要自己写正则拆，
# 拆错静默出错；而分布那一项压根没落盘。
# 三种题型的指标集不同，取并集；缺的那几列在该题型的行里是空值。
SCORE_METRIC_FIELDS = (
    # 单要素
    "n", "naming_accuracy", "spontaneous_naming_accuracy", "prompt_rate",
    "total_prompt_load", "avg_time_per_item", "total_task_time",
    # 双要素
    "weekly_de_score_percentile", "spontaneous_relation_identification_rate",
    # 多要素
    "weekly_me_score_percentile",
)
SCORE_PROMPT_LEVELS = (0, 1, 2, 3)
# 逐题明细是列表，压不进一个格子。它们不属于场次级汇总表，但也**不许静默丢掉**——
# 各自单独出一列 JSON，分析者要拆得开、要查得到。
SCORE_PER_ITEM_FIELDS = ("per_item", "per_item_de_total")


def _score_row(session_cols: dict, task_type: str, metrics: dict) -> dict:
    distribution = metrics.get("prompt_level_distribution") or {}
    row = {**session_cols, "task_type": task_type}
    for field in SCORE_METRIC_FIELDS:
        row[field] = metrics.get(field)
    for level in SCORE_PROMPT_LEVELS:
        row[f"prompt_level_{level}"] = distribution.get(level)
    for field in SCORE_PER_ITEM_FIELDS:
        value = metrics.get(field)
        row[f"{field}_json"] = (None if value is None
                                else json.dumps(value, ensure_ascii=False,
                                                sort_keys=True))
    unknown = (set(metrics) - set(SCORE_METRIC_FIELDS)
               - set(SCORE_PER_ITEM_FIELDS) - {"prompt_level_distribution"})
    if unknown:
        raise ValueError(
            f"结局指标多出未登记的键 {sorted(unknown)}：先加进 SCORE_METRIC_FIELDS 再导出")
    return row

# 每张表的列契约。表头从**契约**来，不从数据推。
#
# 原来是 `cols = sorted({k for r in rows for k in r}) if rows else []`：没有异常事件的
# 场次，abnormal.csv 就是个 0 列文件。批处理里 rbind 240 个场次包时，要么当场炸、
# 要么被 dplyr::bind_rows 静默跳过——而「异常事件为零」恰是最常见的情况，被跳过的
# 是绝大多数场次。列序也跟着数据变，两个包的同名表可能列序不同。
#
# 加新列：先加到这里（放在末尾，别插在中间——列序是对外契约），再改构造代码。
_SUBJECT_COLS = ("subject_code",)
_SESSION_COLS = _SUBJECT_COLS + ("session_code",)

SHEET_FIELDS: dict[str, tuple[str, ...]] = {
    "session": _SESSION_COLS + (
        "week_no", "phase_type", "event_line", "session_sitting_no",
        "data_classification", "is_simulation", "is_simulation_subject",
        "dementia_severity", "mandarin_eligible", "item_bank_version_id",
        "autopilot_profile_version_id", "completion_scope",
        "pseudonym_key_id", "pseudonym_version",
    ),
    "turns": _SESSION_COLS + (
        "item_id", "task_type", "turn_seq", "response_role",
        "source_attempt_seq", "asr_text", "confirmed_response_text",
        "asr_confidence", "prompt_level", "ai_answer_type", "ai_score",
        "ai_needs_review", "ai_judge_mode", "reviewed_score", "score_locked",
        "element_value", "ai_human_diff", "judge_portrait_used",
        "duration_seconds",
    ),
    "attempts": _SESSION_COLS + (
        "item_id", "turn_seq", "response_role", "attempt_seq", "audio_code",
        "prompt_level", "cue_type", "duration_seconds", "asr_text",
        "asr_confidence", "asr_engine_version", "operational_answer_type",
        "operational_score", "operational_needs_review", "judge_mode",
        "judge_engine_version", "judge_reason", "matched_on",
        "contains_target", "error_code", "is_simulation", "truth_scope",
        # 这两列只在带重复请求/画像证据的场次里出现，第一版按实测列集推契约时
        # 没走到那条路径。列契约必须覆盖**所有**分支，不是抽样到的那些。
        "judge_portrait_used", "processing_status",
    ),
    "interactions": _SESSION_COLS + (
        "event_seq", "item_id", "turn_seq", "attempt_seq", "audio_code",
        "event_type", "payload_json", "is_simulation", "truth_scope",
    ),
    "item_scores": (_SESSION_COLS + ("task_type",) + SCORE_METRIC_FIELDS
                    + tuple(f"prompt_level_{level}" for level in SCORE_PROMPT_LEVELS)
                    + tuple(f"{field}_json" for field in SCORE_PER_ITEM_FIELDS)),
    # 正式结局量表：定义包由 PI 冻结之前这张表固定为空。这里只声明两列身份列——
    # 结局列随冻结契约一起进来。今天它是「只有表头、零行」，那是设计。
    "scales": _SESSION_COLS,
    "legacy_unverified_scales": _SUBJECT_COLS + (
        "phase_type", "legacy_reported_label", "legacy_reported_subscale",
        "legacy_reported_score", "source_schema", "verification_status",
        "formal_outcome_eligible",
    ),
    "questionnaire_records": _SUBJECT_COLS + (
        "record_code", "questionnaire_id", "phase_label", "status",
        "definition_sha256", "scoring_rule_id", "computed_total",
        "cutoff_met", "computed_flag", "ai_draft_status", "ai_draft_engine",
        "phase_ordinal", "superseded_by_ordinal",
    ),
    "questionnaire_item_values": (
        "record_code", "item_key", "field_key", "final_value",
        "value_source", "ai_draft_value",
    ),
    "abnormal": _SESSION_COLS + (
        "phase_type", "abnormal_type", "intervention_type", "note",
        "affects_scoring_validity",
    ),
    "audio_manifest": _SESSION_COLS + (
        "audio_code", "turn_key", "audio_format", "status",
        "data_classification", "is_simulation", "contains_direct_identifier",
        "is_reliability_sample", "controlled_audio_exported",
        "export_batch_code",
    ),
    "repeat_audio_manifest": REPEAT_AUDIO_MANIFEST_FIELDS,
}
def _write_csvs(
        sheets: dict, batch_id: str, write_dir: Optional[Path],
        data_classification: str, *, staging_owner_hash: str,
        lease_guard: Callable[[], None]) -> tuple[list[str], Path]:
    root = Path(write_dir or EXPORT_DIR)
    lease_guard()
    classification_root = export_security.ensure_secure_directory(
        root, data_classification)
    base = export_security.ensure_secure_directory(
        classification_root, batch_id, exclusive_final=True)
    staging_receipt = base / STAGING_RECEIPT_NAME
    export_security.atomic_write_json(staging_receipt, {
        "batch_id": batch_id,
        "staging_owner_hash": staging_owner_hash,
    })
    files = []
    for name, rows in sheets.items():
        lease_guard()
        if not _SAFE_BATCH_ID.fullmatch(name):
            raise ValueError("导出表名含非法路径字符")
        p = base / f"{name}.csv"
        declared = SHEET_FIELDS.get(name)
        if declared is None:
            raise ValueError(f"导出表 {name} 没有登记列契约，拒绝导出")
        cols = list(declared)
        if name == "repeat_audio_manifest":
            # The approved governance contract fixes both the field set and
            # their order; it must not inherit the generic sorted ordering.
            if any(set(r) != set(cols) for r in rows):
                raise ValueError("重复请求音频清单字段不等于批准的四项白名单，拒绝导出")
        else:
            for row in rows:
                extra = sorted(set(row) - set(cols))
                if extra:
                    raise ValueError(
                        f"导出表 {name} 出现未登记的列 {extra}："
                        "先把它加进 SHEET_FIELDS 末尾，再导出")
        # 行里缺的列补 None：表头由契约固定，空表也照写表头。
        rows = [{col: row.get(col) for col in cols} for row in rows]
        export_security.atomic_write_csv(p, cols, rows)
        files.append(str(p))
    return files, staging_receipt


def _v(x):
    return x.value if hasattr(x, "value") else x


def _flat(d: Optional[dict]) -> Optional[str]:
    """把一层平字典压成 "k=v; k=v"。

    原来这里写的是 `keep = {k: v for ... if not isinstance(v, (list, dict))}`——
    嵌套值被静默丢掉，`prompt_level_distribution` 就是这么从导出里消失的：
    不报错、不留痕，只有把判分引擎在 R 里重写一遍才看得出少了什么。
    现在遇到嵌套直接拒绝：要出就展开成列，别偷偷不出。
    """
    if not d:
        return None
    nested = sorted(k for k, v in d.items() if isinstance(v, (list, dict)))
    if nested:
        raise ValueError(
            f"结局指标含嵌套值 {nested}，不得压成字符串静默丢弃；请展开成列")
    return "; ".join(f"{k}={v}" for k, v in d.items())


