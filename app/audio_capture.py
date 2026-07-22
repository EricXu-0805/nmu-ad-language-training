"""录音上传配额、完整性验证与服务端收据账本。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import threading
from typing import Iterator

from sqlalchemy import func
from sqlmodel import Session, select

from . import audio_store
from .enums import AudioStatus
from .models import AudioAssetRow, AudioCaptureReceipt


MAX_REGISTRATIONS_PER_TURN_ENV = "AUDIO_MAX_REGISTRATIONS_PER_TURN"
MAX_ASSETS_PER_SESSION_ENV = "AUDIO_MAX_ASSETS_PER_SESSION"
MAX_BYTES_PER_SESSION_ENV = "AUDIO_MAX_BYTES_PER_SESSION"
MAX_CONCURRENT_UPLOADS_ENV = "AUDIO_MAX_CONCURRENT_UPLOADS"

# 四级提示、重录和少量技术失败均有余量；整场默认可容纳数百条、每条最长
# 5 分钟/64 MiB 的保守上限，但会阻止失控客户端无限占盘。
DEFAULT_MAX_REGISTRATIONS_PER_TURN = 12
DEFAULT_MAX_ASSETS_PER_SESSION = 512
DEFAULT_MAX_BYTES_PER_SESSION = 8 * 1024 * 1024 * 1024
DEFAULT_MAX_CONCURRENT_UPLOADS = 4

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_REGISTRATION_LOCK = threading.RLock()
_BYTE_QUOTA_LOCK = threading.RLock()
_UPLOAD_COUNTER_LOCK = threading.Lock()
_active_uploads = 0


class AudioQuotaExceeded(RuntimeError):
    """持久配额已满；重试相同 id 仍可走幂等读取。"""


class AudioUploadBusy(RuntimeError):
    """进程内并发上传槽已满。"""


class AudioCaptureIntegrityError(RuntimeError):
    """数据库、客户端回报和磁盘原件不一致。"""


@dataclass(frozen=True)
class AudioTerminalDisposition:
    """服务端可证明的录音终态，只用于通知设备丢弃本地副本。"""

    reason: str
    raw_audio_id: str
    session_id: str
    turn_key: str
    byte_count: int
    checksum: str
    contains_direct_identifier: bool


class AudioLimitConfigurationError(RuntimeError):
    """部署配额配置非法；必须 fail closed，不能悄悄变成无限。"""


def _positive_limit(name: str, default: int, *, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise AudioLimitConfigurationError(f"{name} 必须是正整数") from exc
    if value <= 0 or value > maximum:
        raise AudioLimitConfigurationError(f"{name} 必须在 1..{maximum} 范围内")
    return value


def max_registrations_per_turn() -> int:
    return _positive_limit(
        MAX_REGISTRATIONS_PER_TURN_ENV, DEFAULT_MAX_REGISTRATIONS_PER_TURN,
        maximum=10_000)


def max_assets_per_session() -> int:
    return _positive_limit(
        MAX_ASSETS_PER_SESSION_ENV, DEFAULT_MAX_ASSETS_PER_SESSION,
        maximum=1_000_000)


def max_bytes_per_session() -> int:
    return _positive_limit(
        MAX_BYTES_PER_SESSION_ENV, DEFAULT_MAX_BYTES_PER_SESSION,
        maximum=1024 * 1024 * 1024 * 1024)


def max_concurrent_uploads() -> int:
    return _positive_limit(
        MAX_CONCURRENT_UPLOADS_ENV, DEFAULT_MAX_CONCURRENT_UPLOADS,
        maximum=1_000)


@contextmanager
def registration_lock() -> Iterator[None]:
    """同进程串行化“查配额 + 插入登记”；数据库唯一键仍处理多进程竞态。"""
    with _REGISTRATION_LOCK:
        yield


def assert_registration_quota(
        s: Session, session_id: str | None, turn_key: str | None) -> None:
    if session_id is None:
        return
    session_count = s.exec(
        select(func.count()).select_from(AudioAssetRow).where(
            AudioAssetRow.session_id == session_id)).one()
    if session_count >= max_assets_per_session():
        raise AudioQuotaExceeded("本场录音登记条数已达服务端上限，禁止创建新录音")
    turn_count = s.exec(
        select(func.count()).select_from(AudioAssetRow).where(
            AudioAssetRow.session_id == session_id,
            AudioAssetRow.turn_key == turn_key)).one()
    if turn_count >= max_registrations_per_turn():
        raise AudioQuotaExceeded("当前环节重录/尝试次数已达服务端上限")


def _session_stored_bytes(s: Session, session_id: str) -> int:
    value = s.exec(select(func.coalesce(func.sum(AudioAssetRow.byte_count), 0)).where(
        AudioAssetRow.session_id == session_id)).one()
    return int(value or 0)


def assert_declared_byte_quota(
        s: Session, row: AudioAssetRow, declared_bytes: int | None) -> None:
    """有 Content-Length 时在接收前快速拒绝；最终事实仍须再次串行校验。"""
    if row.session_id is None or declared_bytes is None:
        return
    current = _session_stored_bytes(s, row.session_id)
    projected = current - int(row.byte_count or 0) + declared_bytes
    if projected > max_bytes_per_session():
        raise AudioQuotaExceeded("本场录音累计字节将超过服务端上限")


@contextmanager
def byte_quota_lock() -> Iterator[None]:
    with _BYTE_QUOTA_LOCK:
        yield


def assert_final_byte_quota(s: Session, row: AudioAssetRow, byte_count: int) -> None:
    if row.session_id is None:
        return
    current = _session_stored_bytes(s, row.session_id)
    projected = current - int(row.byte_count or 0) + byte_count
    if projected > max_bytes_per_session():
        raise AudioQuotaExceeded("本场录音累计字节已达服务端上限")


@contextmanager
def upload_slot() -> Iterator[None]:
    """进程级并发上限；只持有计数锁，不在异步流读取期间阻塞线程锁。"""
    global _active_uploads
    limit = max_concurrent_uploads()
    with _UPLOAD_COUNTER_LOCK:
        if _active_uploads >= limit:
            raise AudioUploadBusy("服务器正在保存其他录音，请稍后用同一录音编号重试")
        _active_uploads += 1
    try:
        yield
    finally:
        with _UPLOAD_COUNTER_LOCK:
            _active_uploads -= 1


def _normalized_checksum(value: str | None) -> str | None:
    normalized = value.lower() if isinstance(value, str) else None
    return normalized if normalized and _HEX_64.fullmatch(normalized) else None


def _reject_terminal_or_withdrawn(row: AudioAssetRow) -> None:
    if (row.status == AudioStatus.deleted or row.withdrawn
            or bool((row.withdrawal_status or "").strip())):
        raise AudioCaptureIntegrityError(
            "音频已删除或撤回，禁止创建或恢复采集收据")


def verify_persisted_audio(
        row: AudioAssetRow, *, max_bytes: int | None = None,
        indexed_path: Path | None = None,
) -> audio_store.AudioBlobFacts:
    """live 回报前逐次核对 DB 完整收据与磁盘真实 size/hash。"""
    checksum = _normalized_checksum(row.checksum)
    if checksum is None or row.byte_count is None or row.byte_count <= 0 or row.uploaded_at is None:
        raise AudioCaptureIntegrityError("音频尚无完整的服务端上传收据")
    try:
        facts = (
            audio_store.blob_facts_for_path(
                row.raw_audio_id, indexed_path, max_bytes=max_bytes)
            if indexed_path is not None
            else audio_store.blob_facts(row.raw_audio_id, max_bytes=max_bytes)
        )
    except (ValueError, audio_store.AudioStoreIntegrityError) as exc:
        raise AudioCaptureIntegrityError(str(exc)) from exc
    if facts is None:
        raise AudioCaptureIntegrityError("服务端音频原件不存在")
    if facts.checksum != checksum or facts.byte_count != row.byte_count:
        raise AudioCaptureIntegrityError("服务端音频原件与数据库 checksum/byte_count 不一致")
    return facts


def verify_known_audio_state(row: AudioAssetRow) -> audio_store.AudioBlobFacts | None:
    """上传重放前核对已有 DB 字段；允许全 NULL 的旧行/崩溃孤儿被同字节收口。"""
    has_db_facts = row.checksum is not None or row.byte_count is not None or row.uploaded_at is not None
    try:
        facts = audio_store.blob_facts(row.raw_audio_id)
    except (ValueError, audio_store.AudioStoreIntegrityError) as exc:
        raise AudioCaptureIntegrityError(str(exc)) from exc
    if has_db_facts and facts is None:
        raise AudioCaptureIntegrityError("数据库已有上传事实但服务端音频原件缺失")
    if row.checksum is not None:
        checksum = _normalized_checksum(row.checksum)
        if checksum is None or facts is None or facts.checksum != checksum:
            raise AudioCaptureIntegrityError("数据库 checksum 与服务端音频原件不一致")
    if row.byte_count is not None:
        if row.byte_count <= 0 or facts is None or facts.byte_count != row.byte_count:
            raise AudioCaptureIntegrityError("数据库 byte_count 与服务端音频原件不一致")
    if row.uploaded_at is not None and (row.checksum is None or row.byte_count is None):
        raise AudioCaptureIntegrityError("数据库 uploaded_at 缺少配套 checksum/byte_count")
    return facts


def append_receipt(
        s: Session, *, row: AudioAssetRow, session_id: str, turn_key: str,
        duration_seconds: float, byte_count: int, checksum: str) -> tuple[AudioCaptureReceipt, bool]:
    """按 raw id 幂等追加收据；返回 ``(receipt, idempotent)``。"""
    _reject_terminal_or_withdrawn(row)
    normalized = _normalized_checksum(checksum)
    if normalized is None or byte_count <= 0:
        raise AudioCaptureIntegrityError("audioSaved checksum/byteCount 非法")
    if not math.isfinite(duration_seconds) or duration_seconds < 0 or duration_seconds > 21_600:
        raise AudioCaptureIntegrityError("audioSaved durationSeconds 非法")
    if (row.session_id != session_id or row.turn_key != turn_key
            or row.byte_count != byte_count or _normalized_checksum(row.checksum) != normalized):
        raise AudioCaptureIntegrityError("audioSaved 与服务端录音登记/上传收据不一致")
    if row.data_classification not in {"research", "simulation"}:
        raise AudioCaptureIntegrityError("音频数据分类不允许进入采集收据账本")
    if row.is_simulation != (row.data_classification == "simulation"):
        raise AudioCaptureIntegrityError("音频 simulation 标志与数据分类不一致")
    verify_persisted_audio(row)

    existing = s.exec(select(AudioCaptureReceipt).where(
        AudioCaptureReceipt.raw_audio_id == row.raw_audio_id)).first()
    if existing is not None:
        same = (
            existing.session_id == session_id
            and existing.turn_key == turn_key
            and existing.duration_seconds == duration_seconds
            and existing.byte_count == byte_count
            and existing.checksum == normalized
            and existing.data_classification == row.data_classification
            and existing.is_simulation == row.is_simulation
            and existing.contains_direct_identifier == row.contains_direct_identifier
        )
        if not same:
            raise AudioCaptureIntegrityError("同一 raw_audio_id 已有不同的服务端采集收据")
        return existing, True

    receipt = AudioCaptureReceipt(
        raw_audio_id=row.raw_audio_id,
        session_id=session_id,
        turn_key=turn_key,
        duration_seconds=duration_seconds,
        byte_count=byte_count,
        checksum=normalized,
        data_classification=row.data_classification,
        is_simulation=row.is_simulation,
        contains_direct_identifier=row.contains_direct_identifier,
    )
    s.add(receipt)
    return receipt, False


def existing_receipt_ack(
        s: Session, *, raw_audio_id: str, session_id: str, turn_key: str,
        duration_seconds: float, byte_count: int, checksum: str,
        contains_direct_identifier: bool | None) -> AudioCaptureReceipt | None:
    """ACK 丢失后的纯读取路径；只有全部已落事实精确匹配才返回既有收据。"""
    existing = s.exec(select(AudioCaptureReceipt).where(
        AudioCaptureReceipt.raw_audio_id == raw_audio_id)).first()
    if existing is None:
        return None
    row = s.get(AudioAssetRow, raw_audio_id)
    if row is not None:
        _reject_terminal_or_withdrawn(row)
    normalized = _normalized_checksum(checksum)
    same = (
        row is not None
        and normalized is not None
        and math.isfinite(duration_seconds)
        and existing.session_id == session_id
        and existing.turn_key == turn_key
        and existing.duration_seconds == duration_seconds
        and existing.byte_count == byte_count
        and existing.checksum == normalized
        # 旧客户端未声明该字段时，不把“缺失”偷换成 false；
        # 改由不可变 receipt 与 asset row 相互印证。显式声明时则必须精确匹配。
        and (contains_direct_identifier is None
             or existing.contains_direct_identifier == contains_direct_identifier)
        and row.session_id == existing.session_id
        and row.turn_key == existing.turn_key
        and row.byte_count == existing.byte_count
        and _normalized_checksum(row.checksum) == existing.checksum
        and row.data_classification == existing.data_classification
        and row.is_simulation == existing.is_simulation
        and row.contains_direct_identifier == existing.contains_direct_identifier
    )
    if not same:
        raise AudioCaptureIntegrityError(
            "同一 raw_audio_id 已有不同的服务端采集收据")
    verify_persisted_audio(row)
    return existing


def verify_terminal_disposition(
        s: Session, *, row: AudioAssetRow, raw_audio_id: str,
        session_id: str, turn_key: str, duration_seconds: float,
        byte_count: int, checksum: str,
        contains_direct_identifier: bool | None) -> AudioTerminalDisposition | None:
    """验证已删除/已撤回录音的不可变历史事实。

    这条路径不读物理原件：删除成功时它本就应不存在。只有 DB
    上传事实完整、客户端全字段精确匹配，且已有 receipt（如果有）
    也与两者一致时，才能授权浏览器删掉本地声音副本。
    """
    terminal_reason: str | None = None
    if row.status == AudioStatus.deleted:
        # ``deleted`` without the governance gate is an internally inconsistent
        # row (the DELETE retry path rejects the same state).  It must not become
        # an irreversible browser-cleanup authorization merely because the enum
        # was changed by legacy/manual data repair.
        if not row.delete_gate_passed:
            raise AudioCaptureIntegrityError(
                "录音已标记 deleted 但缺少删除闸门证据")
        terminal_reason = "deleted"
    elif row.withdrawn or bool((row.withdrawal_status or "").strip()):
        terminal_reason = "withdrawn"
    if terminal_reason is None:
        return None

    normalized_request = _normalized_checksum(checksum)
    normalized_history = _normalized_checksum(row.checksum)
    historical_complete = (
        normalized_history is not None
        and row.byte_count is not None
        and row.byte_count > 0
        and row.uploaded_at is not None
    )
    exact = (
        historical_complete
        and row.raw_audio_id == raw_audio_id
        and row.session_id == session_id
        and row.turn_key == turn_key
        and row.byte_count == byte_count
        and normalized_request is not None
        and normalized_history == normalized_request
        # 本地丢弃是不可逆动作；旧客户端缺省该字段不能猜。
        and contains_direct_identifier is not None
        and row.contains_direct_identifier == contains_direct_identifier
    )
    if not exact:
        raise AudioCaptureIntegrityError(
            "录音终态处置与服务端历史上传事实不一致")
    assert normalized_request is not None

    receipt = s.exec(select(AudioCaptureReceipt).where(
        AudioCaptureReceipt.raw_audio_id == raw_audio_id)).first()
    if receipt is not None:
        receipt_exact = (
            math.isfinite(duration_seconds)
            and receipt.raw_audio_id == row.raw_audio_id
            and receipt.session_id == row.session_id == session_id
            and receipt.turn_key == row.turn_key == turn_key
            and receipt.duration_seconds == duration_seconds
            and receipt.byte_count == row.byte_count == byte_count
            and _normalized_checksum(receipt.checksum) == normalized_history
            and receipt.contains_direct_identifier
            == row.contains_direct_identifier
            == contains_direct_identifier
            and receipt.data_classification == row.data_classification
            and receipt.is_simulation == row.is_simulation
        )
        if not receipt_exact:
            raise AudioCaptureIntegrityError(
                "录音终态处置与服务端采集收据不一致")

    return AudioTerminalDisposition(
        reason=terminal_reason,
        raw_audio_id=row.raw_audio_id,
        session_id=session_id,
        turn_key=turn_key,
        byte_count=byte_count,
        checksum=normalized_request,
        contains_direct_identifier=contains_direct_identifier,
    )


def list_receipts(
        s: Session, session_id: str, *, after_seq: int = 0,
        limit: int = 500) -> list[AudioCaptureReceipt]:
    return list(s.exec(select(AudioCaptureReceipt).where(
        AudioCaptureReceipt.session_id == session_id,
        AudioCaptureReceipt.server_seq > after_seq,
    ).order_by(AudioCaptureReceipt.server_seq).limit(limit)))
