"""受试者端设备能力凭据。

PIN 只用于当面配对；配对后的每个请求都使用短时、绑定单个场次的
随机 bearer。数据库只保存 bearer 和非 PII 设备标识的 SHA-256，不保存明文。
本模块不记录 bearer、PIN 或设备标识。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from contextlib import contextmanager
import hashlib
import os
import re
import secrets
import threading
from typing import Iterator

from sqlmodel import Session, select

from .models import PatientDeviceCapability


TTL_MINUTES_ENV = "DEVICE_CAPABILITY_TTL_MINUTES"
DEFAULT_TTL_MINUTES = 45
MIN_TTL_MINUTES = 5
MAX_TTL_MINUTES = 120

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_PAIR_LOCK = threading.RLock()


class DeviceCapabilityConfigurationError(RuntimeError):
    """设备能力配置非法；部署必须 fail closed。"""


class CapabilityResolution(str, Enum):
    VALID = "valid"
    RECOVERY_ONLY = "recovery_only"
    INVALID = "invalid"
    EXPIRED = "expired"
    REVOKED = "revoked"


def _utc_now_naive() -> datetime:
    """SQLite-compatible UTC timestamp; API serialization adds the explicit zone."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


@contextmanager
def serialized_mutation() -> Iterator[None]:
    """Same-process fallback for SQLite; DB row/unique locks remain authoritative."""
    with _PAIR_LOCK:
        yield


def ttl_minutes() -> int:
    raw = os.environ.get(TTL_MINUTES_ENV)
    if raw is None:
        return DEFAULT_TTL_MINUTES
    if not raw.strip():
        raise DeviceCapabilityConfigurationError(
            f"{TTL_MINUTES_ENV} 必须是 {MIN_TTL_MINUTES}..{MAX_TTL_MINUTES} 的整数")
    try:
        value = int(raw)
    except ValueError as exc:
        raise DeviceCapabilityConfigurationError(
            f"{TTL_MINUTES_ENV} 必须是整数") from exc
    if value < MIN_TTL_MINUTES or value > MAX_TTL_MINUTES:
        raise DeviceCapabilityConfigurationError(
            f"{TTL_MINUTES_ENV} 必须在 {MIN_TTL_MINUTES}..{MAX_TTL_MINUTES} 范围内")
    return value


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def device_id_hash(device_id: str) -> str:
    if not _DEVICE_ID_PATTERN.fullmatch(device_id):
        raise ValueError("device_id 格式非法")
    return hashlib.sha256(device_id.encode("ascii")).hexdigest()


def create_capability(
        s: Session, *, session_id: str, device_id: str,
        now: datetime | None = None) -> tuple[str, PatientDeviceCapability]:
    """撤销该场次旧的活跃能力，创建新能力并且只返回一次明文。

    只 stage/flush，从不提交：这是 ``/device/pair`` 唯一调用点，调用方必须在
    同一事务内完成配对随附的治理动作（例如同场次换绑时的自动驾驶/运行态暂停与
    claim 失效）后统一提交，让配对本身与其治理防护落在同一个原子快照里。调用
    方任何一步失败都必须让整个事务回滚——绝不能只重试本函数、丢弃已计划的
    治理动作后仍把这次配对当作成功返回。
    """
    issued_at = _utc_naive(now) if now is not None else _utc_now_naive()
    expires_at = issued_at + timedelta(minutes=ttl_minutes())
    hashed_device = device_id_hash(device_id)

    with _PAIR_LOCK:
        # ``SELECT FOR UPDATE`` alone cannot lock an absent row.  The nullable
        # active_session_key has a DB unique constraint, so concurrent workers
        # cannot both flush an active token undetected. A single attempt only:
        # this call already sits inside the caller's outer transaction, which
        # by now holds the LiveState/session/runtime reads and facts (e.g.
        # ``had_active_capability``) that decide whether rotation fencing
        # runs. An internal ``rollback()``-and-retry here would silently
        # invalidate those already-taken locks and snapshots without the
        # caller's knowledge, so a collision must instead propagate and let
        # the caller's *entire* transaction retry from a fresh snapshot.
        prior = list(s.exec(
            select(PatientDeviceCapability).where(
                PatientDeviceCapability.session_id == session_id,
                PatientDeviceCapability.revoked_at.is_(None),
            ).with_for_update()
        ))
        for prior_row in prior:
            prior_row.revoked_at = issued_at
            prior_row.active_session_key = None
            s.add(prior_row)
        s.flush()

        # 256-bit random bearer; collision is already negligible, but the PK
        # check keeps the function fail closed under a replaced test RNG.
        for _ in range(4):
            token = secrets.token_urlsafe(32)
            hashed = token_hash(token)
            if s.get(PatientDeviceCapability, hashed) is None:
                break
        else:  # pragma: no cover - only reachable with a broken/replaced CSPRNG
            raise RuntimeError("无法生成唯一设备能力凭据")

        row = PatientDeviceCapability(
            token_hash=hashed,
            session_id=session_id,
            device_id_hash=hashed_device,
            active_session_key=session_id,
            created_at=issued_at,
            expires_at=expires_at,
            last_seen_at=issued_at,
            recovery_only_at=None,
            revoked_at=None,
        )
        s.add(row)
        s.flush()
        return token, row


def resolve_capability(
        s: Session, token: str | None,
        *, now: datetime | None = None) -> tuple[CapabilityResolution, PatientDeviceCapability | None]:
    """解析 bearer；无效、过期、已撤销保持可区分的稳定状态。"""
    if not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
        return CapabilityResolution.INVALID, None
    return resolve_capability_hash(s, token_hash(token), now=now)


def resolve_capability_hash(
        s: Session, hashed_token: str | None,
        *, now: datetime | None = None) -> tuple[CapabilityResolution, PatientDeviceCapability | None]:
    """Resolve by persisted digest, read-only.

    Never writes.  This runs inside the authentication middleware, before any
    route has proved the bearer is even bound to the session it is addressing,
    so a valid token pointed at the wrong session must not leave a trace in the
    database.  ``last_seen_at`` used to be refreshed here; nothing reads it as
    an authorization or liveness input, so the write was pure hidden side
    effect and is gone.  Rate-limit identity is unchanged: it is derived from
    ``device_id_hash``, not from this column.
    """
    if (not isinstance(hashed_token, str) or len(hashed_token) != 64
            or any(ch not in "0123456789abcdef" for ch in hashed_token)):
        return CapabilityResolution.INVALID, None
    row = s.get(PatientDeviceCapability, hashed_token)
    if row is None:
        return CapabilityResolution.INVALID, None
    current = _utc_naive(now) if now is not None else _utc_now_naive()
    if row.revoked_at is not None:
        return CapabilityResolution.REVOKED, row
    if row.expires_at <= current:
        return CapabilityResolution.EXPIRED, row
    if row.recovery_only_at is not None:
        return CapabilityResolution.RECOVERY_ONLY, row
    return CapabilityResolution.VALID, row


def revalidate_active_for_write(
        s: Session, hashed_token: str | None, session_id: str,
        *, now: datetime | None = None) -> tuple[CapabilityResolution, PatientDeviceCapability | None]:
    """Lock and re-check a capability in the transaction that will commit a new write."""
    if (not isinstance(hashed_token, str) or len(hashed_token) != 64
            or any(ch not in "0123456789abcdef" for ch in hashed_token)):
        return CapabilityResolution.INVALID, None
    row = s.exec(select(PatientDeviceCapability).where(
        PatientDeviceCapability.token_hash == hashed_token,
    ).with_for_update()).first()
    if row is None or row.session_id != session_id:
        return CapabilityResolution.INVALID, row
    current = _utc_naive(now) if now is not None else _utc_now_naive()
    if row.revoked_at is not None:
        return CapabilityResolution.REVOKED, row
    if row.expires_at <= current:
        return CapabilityResolution.EXPIRED, row
    if row.recovery_only_at is not None or row.active_session_key != session_id:
        return CapabilityResolution.RECOVERY_ONLY, row
    return CapabilityResolution.VALID, row


def mark_session_recovery_only(
        s: Session, session_id: str | None,
        *, now: datetime | None = None) -> int:
    """Demote the session that just left LiveState without breaking exact audio ACKs."""
    if not session_id:
        return 0
    changed_at = _utc_naive(now) if now is not None else _utc_now_naive()
    rows = list(s.exec(select(PatientDeviceCapability).where(
        PatientDeviceCapability.session_id == session_id,
        PatientDeviceCapability.active_session_key == session_id,
        PatientDeviceCapability.revoked_at.is_(None),
    ).with_for_update()))
    for row in rows:
        row.active_session_key = None
        row.recovery_only_at = row.recovery_only_at or changed_at
        s.add(row)
    return len(rows)
