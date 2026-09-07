"""逐受试者云处理授权与 provider 数据边界。

API Key 只决定 provider 是否技术可用，绝不等于受试者授权。任何原始音频或
回答文本外发前，都必须同时满足当前部署 policy 与受试者档案中的版本化授权。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import hashlib
import os
import stat
import threading
from typing import Protocol


PROVIDER_ID_ENV = "CLOUD_PROCESSING_PROVIDER_ID"
NOTICE_VERSION_ENV = "CLOUD_PROCESSING_NOTICE_VERSION"


# A provider call and a successful consent revocation must have a total order for
# each subject.  PostgreSQL row locks provide the cross-worker half of that
# contract (the caller holds the Patient row while invoking the provider); the
# striped process locks plus SQLite file locks provide the local boundary.
# A fixed stripe set avoids retaining patient identifiers
# or growing a lock registry indefinitely.  A collision only serializes two
# unrelated subjects; it never weakens the privacy fence.
_SUBJECT_EGRESS_LOCKS = tuple(threading.RLock() for _ in range(257))
_HELD_SQLITE_EGRESS_LOCKS = threading.local()


@contextmanager
def _sqlite_egress_file_fence(bind, stripe: int):
    """Cross-process privacy fence without holding SQLite's clinical writer.

    File-backed SQLite uses a bounded set of private advisory lock files beside
    the canonical database. In-memory SQLite exists only within its process;
    its explicit contract is the process lock. Unknown file-lock platforms fail
    closed. Lock files are coordination state, never patient data or evidence.
    """
    if bind.dialect.name != "sqlite":
        yield
        return
    from .db import _sqlite_file_path
    database = _sqlite_file_path(bind.url)
    if database is None:
        # Private or shared-cache memory databases cannot be shared by processes.
        yield
        return
    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeError("sqlite_cloud_egress_requires_posix_file_lock") from exc
    from .storage_security import ensure_private_directory
    db_path = database.resolve()
    db_key = hashlib.sha256(str(db_path).encode("utf-8")).hexdigest()[:20]
    lock_dir = ensure_private_directory(db_path.parent / f".cloud-egress-{db_key}")
    lock_path = lock_dir / f"stripe-{stripe:03d}.lock"
    key = str(lock_path)
    held = getattr(_HELD_SQLITE_EGRESS_LOCKS, "paths", None)
    if held is None:
        held = _HELD_SQLITE_EGRESS_LOCKS.paths = set()
    if key in held:
        # flock locks independent open descriptions; do not reopen recursively.
        yield
        return
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("sqlite_cloud_egress_requires_nofollow")
    fd = os.open(lock_path, flags | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("sqlite_cloud_egress_lock_must_be_regular")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def serialized_subject_egress(patient_id: str, *, bind):
    """Serialize patient-data egress against grant/revoke/withdrawal writes.

    Callers must acquire this lock *before* their database governance locks and
    retain it from the final authorization read until the provider invocation has
    returned.  Revocation and withdrawal use the same order, so once either API
    returns successfully no older request can begin a new provider call.
    """
    normalized = patient_id.strip() if isinstance(patient_id, str) else ""
    if not normalized:
        raise ValueError("patient_id 必须是非空字符串")
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    stripe = int.from_bytes(digest[:4], "big") % len(_SUBJECT_EGRESS_LOCKS)
    with _SUBJECT_EGRESS_LOCKS[stripe], _sqlite_egress_file_fence(bind, stripe):
        yield


class DataBoundary(str, Enum):
    LOCAL = "local"
    CLOUD = "cloud"
    UNKNOWN = "unknown"


class BoundedProvider(Protocol):
    data_boundary: str
    provider_id: str | None


@dataclass(frozen=True)
class CloudProcessingPolicy:
    provider_id: str | None
    notice_version: str | None

    @property
    def configured(self) -> bool:
        return bool(self.provider_id and self.notice_version)


def current_policy() -> CloudProcessingPolicy:
    provider_id = os.environ.get(PROVIDER_ID_ENV, "").strip() or None
    notice_version = os.environ.get(NOTICE_VERSION_ENV, "").strip() or None
    return CloudProcessingPolicy(provider_id=provider_id, notice_version=notice_version)


def provider_boundary(provider: object) -> DataBoundary:
    raw = getattr(provider, "data_boundary", None)
    if raw == DataBoundary.LOCAL.value:
        return DataBoundary.LOCAL
    if raw == DataBoundary.CLOUD.value:
        return DataBoundary.CLOUD
    return DataBoundary.UNKNOWN


def provider_id(provider: object) -> str | None:
    value = getattr(provider, "provider_id", None)
    return value.strip() if isinstance(value, str) and value.strip() else None


def authorization_issues(patient: object, provider: object) -> list[str]:
    """返回云外呼门禁问题；本地 provider 不需要云授权。"""
    boundary = provider_boundary(provider)
    if boundary is DataBoundary.LOCAL:
        return []
    if boundary is DataBoundary.UNKNOWN:
        return ["provider 未声明 local/cloud 数据边界"]

    policy = current_policy()
    actual_provider_id = provider_id(provider)
    issues: list[str] = []
    if not policy.configured:
        issues.append("部署未配置云处理 provider id 或告知版本")
    if not actual_provider_id:
        issues.append("云 provider 未声明 provider_id")
    elif policy.provider_id != actual_provider_id:
        issues.append("云 provider 与当前部署 policy 不匹配")
    if getattr(patient, "cloud_processing_allowed", None) is not True:
        issues.append("受试者未明确允许云处理")
    if getattr(patient, "cloud_processing_provider_id", None) != policy.provider_id:
        issues.append("受试者授权 provider 版本不匹配")
    if getattr(patient, "cloud_processing_notice_version", None) != policy.notice_version:
        issues.append("受试者授权告知版本不匹配")
    if getattr(patient, "cloud_processing_consented_at", None) is None:
        issues.append("受试者云处理授权缺少服务器时间")
    if getattr(patient, "cloud_processing_revoked_at", None) is not None:
        issues.append("受试者已撤销云处理授权")
    return issues
