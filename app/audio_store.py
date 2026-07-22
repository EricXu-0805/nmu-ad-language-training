"""音频采集原件的私有、流式、不可覆盖存储。

上传路径只在同目录临时文件中增量写入和计算 SHA-256，完成容器签名检查、
``fsync`` 后才在按 ``raw_audio_id`` 的进程间文件锁内发布。相同 id/相同字节
是幂等重放；相同 id/不同字节永不覆盖。
"""
from __future__ import annotations

from collections.abc import AsyncIterable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
import asyncio
import fcntl
import hashlib
import os
from pathlib import Path
import re
import tempfile


_DEFAULT_AUDIO_DIR = Path(__file__).resolve().parent.parent / "data" / "audio"
# 生产默认仍是项目受控 data/audio；测试/容器可显式隔离到独立挂载，避免触碰真机录音。
AUDIO_DIR = Path(os.environ.get("AUDIO_DIR") or _DEFAULT_AUDIO_DIR)

# 防路径穿越：音频 id 只允许安全字符（前端生成 aud-<uuid>，天然满足）。
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
MAX_AUDIO_BLOB_BYTES = 64 * 1024 * 1024

_EXT_BY_MIME = {
    "audio/webm": ".webm", "audio/mpeg": ".mp3", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/mp4": ".m4a", "audio/ogg": ".ogg",
}
_SUPPORTED_EXTENSIONS = frozenset(_EXT_BY_MIME.values())


class AudioBlobConflict(RuntimeError):
    """同一 raw_audio_id 已有不同字节；采集原件不可覆盖。"""


class AudioStoreIntegrityError(RuntimeError):
    """磁盘上出现软链、多副本或数据库无法信任的状态。"""


class AudioBlobTooLarge(ValueError):
    """流式读取过程中超过服务端硬上限。"""


class AudioBlobMutationBusy(RuntimeError):
    """Another publish/delete currently owns this raw id's short final lock."""


@dataclass(frozen=True)
class AudioBlobFacts:
    path: Path
    checksum: str
    byte_count: int


@dataclass(frozen=True)
class AudioBlobSaveResult(AudioBlobFacts):
    idempotent: bool


@dataclass(frozen=True)
class AudioBlobPending:
    path: Path
    checksum: str
    byte_count: int
    normalized_mime: str


@dataclass
class AudioBlobMutationLease:
    """Held cross-process raw-id lock; release exactly once."""

    _handle: object | None

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def ext_for(mime: str | None) -> str:
    if not mime:
        return ".webm"
    return _EXT_BY_MIME.get(mime.split(";")[0].strip().lower(), ".webm")


def normalize_audio_mime(mime: str | None) -> str:
    value = (mime or "").split(";", 1)[0].strip().lower()
    if value not in _EXT_BY_MIME:
        raise ValueError("仅接受明确支持的音频 Content-Type")
    return value


def _container_signature_valid(prefix: bytes, byte_count: int, normalized: str) -> bool:
    if normalized == "audio/webm":
        return prefix.startswith(b"\x1a\x45\xdf\xa3")
    if normalized == "audio/ogg":
        return prefix.startswith(b"OggS")
    if normalized in {"audio/wav", "audio/x-wav"}:
        return byte_count >= 12 and prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE"
    if normalized == "audio/mpeg":
        return (prefix.startswith(b"ID3")
                or (len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0))
    if normalized == "audio/mp4":
        return byte_count >= 12 and prefix[4:8] == b"ftyp"
    return False


def validate_audio_container(data: bytes, mime: str | None) -> str:
    """兼容的小字节入口；生产上传使用流式签名检查。"""
    normalized = normalize_audio_mime(mime)
    if not _container_signature_valid(data[:32], len(data), normalized):
        raise ValueError(f"上传字节与 {normalized} 容器签名不一致")
    return normalized


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(
        path: Path, *, chunk_size: int = 1024 * 1024,
        max_bytes: int | None = None,
) -> tuple[str, int]:
    """常量内存计算普通文件的 SHA-256 与长度。"""
    if path.is_symlink() or not path.is_file():
        raise AudioStoreIntegrityError("音频路径不是可信普通文件")
    if (max_bytes is not None
            and (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
                 or max_bytes < 0)):
        raise ValueError("max_bytes must be a nonnegative integer")
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise AudioBlobTooLarge("音频原件超过本次完整性校验字节上限")
    return digest.hexdigest(), total


def _ensure_audio_root() -> None:
    if AUDIO_DIR.exists() and (AUDIO_DIR.is_symlink() or not AUDIO_DIR.is_dir()):
        raise AudioStoreIntegrityError("音频存储根目录不是可信目录，拒绝读写")
    AUDIO_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if AUDIO_DIR.is_symlink() or not AUDIO_DIR.is_dir():
        raise AudioStoreIntegrityError("音频存储根目录不是可信目录，拒绝读写")


def _existing_blobs(raw_audio_id: str) -> list[Path]:
    rows = sorted(AUDIO_DIR.glob(f"{raw_audio_id}.*")) if AUDIO_DIR.exists() else []
    if any(row.is_symlink() for row in rows):
        raise AudioStoreIntegrityError("音频路径出现软链接，拒绝读写")
    if any(not row.is_file() for row in rows):
        raise AudioStoreIntegrityError("音频路径不是普通文件，拒绝读写")
    if len(rows) > 1:
        raise AudioStoreIntegrityError("同一音频 id 出现多个物理文件，拒绝猜测权威原件")
    return rows


def blob_facts(
        raw_audio_id: str, *, max_bytes: int | None = None,
) -> AudioBlobFacts | None:
    """读取权威物理原件事实；完整性异常不降级成“文件不存在”。"""
    if not SAFE_ID.fullmatch(raw_audio_id):
        raise ValueError("非法音频 id")
    if not AUDIO_DIR.exists():
        return None
    if AUDIO_DIR.is_symlink() or not AUDIO_DIR.is_dir():
        raise AudioStoreIntegrityError("音频存储根目录不是可信目录")
    rows = _existing_blobs(raw_audio_id)
    if not rows:
        return None
    digest, byte_count = sha256_file(rows[0], max_bytes=max_bytes)
    return AudioBlobFacts(rows[0], digest, byte_count)


def index_blobs(raw_audio_ids: Iterable[str]) -> dict[str, Path]:
    """Index requested immutable blobs with one storage-directory scan.

    This is intended for bounded batch verification. It preserves the same
    duplicate/symlink/non-file rejection as per-id lookup without turning N
    requested audio ids into N full directory scans.
    """
    requested = set(raw_audio_ids)
    if any(not isinstance(value, str) or SAFE_ID.fullmatch(value) is None
           for value in requested):
        raise ValueError("非法音频 id")
    if not requested or not AUDIO_DIR.exists():
        return {}
    if AUDIO_DIR.is_symlink() or not AUDIO_DIR.is_dir():
        raise AudioStoreIntegrityError("音频存储根目录不是可信目录")
    indexed: dict[str, Path] = {}
    with os.scandir(AUDIO_DIR) as entries:
        for entry in entries:
            suffix = Path(entry.name).suffix
            matching_ids = {
                entry.name[:index]
                for index, character in enumerate(entry.name)
                if character == "." and entry.name[:index] in requested
            }
            if not matching_ids:
                continue
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                raise AudioStoreIntegrityError("音频路径不是可信普通文件")
            for raw_audio_id in matching_ids:
                if (suffix not in _SUPPORTED_EXTENSIONS
                        or entry.name != f"{raw_audio_id}{suffix}"):
                    raise AudioStoreIntegrityError(
                        "音频路径使用了未批准的文件类型")
                if raw_audio_id in indexed:
                    raise AudioStoreIntegrityError("同一音频 id 出现多个物理文件")
                indexed[raw_audio_id] = Path(entry.path)
    return indexed


def blob_facts_for_path(
        raw_audio_id: str, path: Path, *, max_bytes: int | None = None,
) -> AudioBlobFacts:
    """Hash one path previously returned by :func:`index_blobs`."""
    if SAFE_ID.fullmatch(raw_audio_id) is None:
        raise ValueError("非法音频 id")
    candidate = Path(path)
    if (candidate.absolute().parent != AUDIO_DIR.absolute()
            or candidate.suffix not in _SUPPORTED_EXTENSIONS
            or candidate.name != f"{raw_audio_id}{candidate.suffix}"):
        raise AudioStoreIntegrityError("音频索引路径与 id 不一致")
    checksum, byte_count = sha256_file(candidate, max_bytes=max_bytes)
    return AudioBlobFacts(candidate, checksum, byte_count)


def _lock_path(raw_audio_id: str) -> Path:
    path = AUDIO_DIR / f".{raw_audio_id}.upload.lock"
    if path.is_symlink():
        raise AudioStoreIntegrityError("音频上传锁路径是软链接，拒绝写入")
    return path


def _open_locked_mutation_file(raw_audio_id: str):
    if not SAFE_ID.fullmatch(raw_audio_id):
        raise ValueError("非法音频 id")
    _ensure_audio_root()
    handle = _lock_path(raw_audio_id).open("a+b")
    os.chmod(handle.fileno(), 0o600)
    return handle


@contextmanager
def blob_mutation_lock(raw_audio_id: str):
    """Serialize publish/delete for one raw id across threads and workers."""
    handle = _open_locked_mutation_file(raw_audio_id)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


async def acquire_blob_mutation_lease(
        raw_audio_id: str, *, wait_seconds: float = 2.0) -> AudioBlobMutationLease:
    """Wait briefly for the short final lock using only nonblocking probes.

    Identical concurrent replays retain their 200/idempotent contract, while a
    wedged final section is bounded and never blocks the event-loop thread.
    """
    handle = _open_locked_mutation_file(raw_audio_id)
    deadline = asyncio.get_running_loop().time() + max(0.0, wait_seconds)
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return AudioBlobMutationLease(handle)
            except BlockingIOError as exc:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AudioBlobMutationBusy(
                        "同一录音正在完成上传或删除，请稍后重试") from exc
                await asyncio.sleep(0.01)
    except BaseException:
        handle.close()
        raise


def _publish_temp(raw_audio_id: str, tmp_path: Path, *, normalized_mime: str,
                  digest: str, byte_count: int,
                  mutation_lock_held: bool = False) -> AudioBlobSaveResult:
    """在文件锁内幂等发布已落盘临时文件，绝不以 rename 覆盖既有路径。"""
    target = AUDIO_DIR / f"{raw_audio_id}{ext_for(normalized_mime)}"
    def publish_locked() -> AudioBlobSaveResult:
        existing = _existing_blobs(raw_audio_id)
        if existing:
            existing_digest, existing_bytes = sha256_file(existing[0])
            if (existing_digest == digest and existing_bytes == byte_count
                    and existing[0].suffix == target.suffix):
                return AudioBlobSaveResult(
                    existing[0], existing_digest, existing_bytes, True)
            raise AudioBlobConflict("同一音频 id 已存在不同字节或格式，禁止覆盖原始证据")
        published = False
        try:
            try:
                # 同目录 hard-link 是不可覆盖的原子发布；目标哪怕刚被创建也只会 EEXIST。
                os.link(tmp_path, target)
                published = True
            except FileExistsError as exc:
                raise AudioBlobConflict(
                    "同一音频 id 的目标文件已存在，禁止覆盖") from exc
            os.chmod(target, 0o600)
            directory_fd = os.open(AUDIO_DIR, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return AudioBlobSaveResult(target, digest, byte_count, False)
        except BaseException:
            # hard-link succeeded but chmod/fsync/cancellation failed before the
            # caller could receive a SaveResult.  No DB fact can yet reference this
            # new inode, so remove it while the raw-id lease is still held.
            if published:
                target.unlink(missing_ok=True)
            raise
    if mutation_lock_held:
        return publish_locked()
    with blob_mutation_lock(raw_audio_id):
        return publish_locked()


async def stage_blob_stream(
        raw_audio_id: str, chunks: AsyncIterable[bytes], mime: str | None, *,
        max_bytes: int = MAX_AUDIO_BLOB_BYTES) -> AudioBlobPending:
    """流式写隐藏 pending 并验签；此阶段绝不发布也不持 raw-id 锁。"""
    if not SAFE_ID.fullmatch(raw_audio_id):
        raise ValueError("非法音频 id")
    if max_bytes <= 0:
        raise ValueError("音频上限配置非法")
    normalized = normalize_audio_mime(mime)
    _ensure_audio_root()
    tmp_path: Path | None = None
    try:
        digest = hashlib.sha256()
        byte_count = 0
        prefix = bytearray()
        with tempfile.NamedTemporaryFile(
                mode="wb", dir=AUDIO_DIR, prefix=f".{raw_audio_id}.",
                suffix=".pending", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            os.chmod(tmp.fileno(), 0o600)
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise ValueError("上传流包含非法字节块")
                if not chunk:
                    continue
                byte_count += len(chunk)
                if byte_count > max_bytes:
                    raise AudioBlobTooLarge(f"音频超过 {max_bytes} 字节上限")
                if len(prefix) < 32:
                    prefix.extend(chunk[:32 - len(prefix)])
                digest.update(chunk)
                tmp.write(chunk)
            if byte_count <= 0:
                raise ValueError("空音频字节")
            if not _container_signature_valid(bytes(prefix), byte_count, normalized):
                raise ValueError(f"上传字节与 {normalized} 容器签名不一致")
            tmp.flush()
            os.fsync(tmp.fileno())
        pending = AudioBlobPending(
            path=tmp_path,
            checksum=digest.hexdigest(),
            byte_count=byte_count,
            normalized_mime=normalized,
        )
        tmp_path = None  # caller owns cleanup after a successful stage
        return pending
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def publish_staged_blob(
        raw_audio_id: str, pending: AudioBlobPending, *,
        mutation_lock_held: bool = False) -> AudioBlobSaveResult:
    """Publish a completed pending file only after the caller's final DB gates."""
    if (pending.path.parent != AUDIO_DIR or pending.path.is_symlink()
            or not pending.path.is_file()):
        raise AudioStoreIntegrityError("待发布音频不是受控目录中的普通文件")
    return _publish_temp(
        raw_audio_id, pending.path, normalized_mime=pending.normalized_mime,
        digest=pending.checksum, byte_count=pending.byte_count,
        mutation_lock_held=mutation_lock_held)


def discard_staged_blob(pending: AudioBlobPending | None) -> None:
    if pending is not None:
        pending.path.unlink(missing_ok=True)


async def save_blob_stream_atomic(
        raw_audio_id: str, chunks: AsyncIterable[bytes], mime: str | None, *,
        max_bytes: int = MAX_AUDIO_BLOB_BYTES,
        mutation_lock_held: bool = False) -> AudioBlobSaveResult:
    """兼容入口；业务路由应 stage 后在最终门禁内显式 publish。"""
    pending = await stage_blob_stream(raw_audio_id, chunks, mime, max_bytes=max_bytes)
    try:
        return publish_staged_blob(
            raw_audio_id, pending, mutation_lock_held=mutation_lock_held)
    finally:
        discard_staged_blob(pending)


def save_blob_atomic(raw_audio_id: str, data: bytes,
                     mime: str | None) -> tuple[Path, str, bool]:
    """内部兼容入口；调用者已持有完整字节时仍遵守原件不可覆盖。"""
    if not SAFE_ID.fullmatch(raw_audio_id):
        raise ValueError("非法音频 id")
    normalized = normalize_audio_mime(mime)
    _ensure_audio_root()
    digest = sha256_hex(data)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="wb", dir=AUDIO_DIR, prefix=f".{raw_audio_id}.",
                suffix=".pending", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            os.chmod(tmp.fileno(), 0o600)
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        result = _publish_temp(
            raw_audio_id, tmp_path, normalized_mime=normalized,
            digest=digest, byte_count=len(data))
        return result.path, result.checksum, result.idempotent
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def save_blob(raw_audio_id: str, data: bytes, mime: str | None) -> tuple[Path, str]:
    path, digest, _idempotent = save_blob_atomic(raw_audio_id, data, mime)
    return path, digest


def find_blob(raw_audio_id: str) -> Path | None:
    if not SAFE_ID.fullmatch(raw_audio_id) or not AUDIO_DIR.exists():
        return None
    try:
        rows = _existing_blobs(raw_audio_id)
    except AudioStoreIntegrityError:
        return None
    return rows[0] if rows else None


def delete_blob_if_matches(
        raw_audio_id: str, checksum: str, byte_count: int, *,
        mutation_lock_held: bool = False) -> bool:
    """仅清理本次刚发布且事实完全相同的文件，绝不误删竞争者原件。"""
    if not SAFE_ID.fullmatch(raw_audio_id) or not AUDIO_DIR.exists():
        return False
    def delete_locked() -> bool:
        facts = blob_facts(raw_audio_id)
        if facts is None or facts.checksum != checksum or facts.byte_count != byte_count:
            return False
        facts.path.unlink()
        return True
    if mutation_lock_held:
        return delete_locked()
    with blob_mutation_lock(raw_audio_id):
        return delete_locked()


def delete_blob(raw_audio_id: str, *, mutation_lock_held: bool = False) -> bool:
    """物理删除字节并 fsync 目录。

    只应在删除闸门的 DB 终态已成功提交后调用（见
    ``main.audio_delete``）。完整性异常或 unlink/fsync 失败必须向上抛出，
    不得伪装成“字节已不存在”。
    """
    def delete_locked() -> bool:
        if not SAFE_ID.fullmatch(raw_audio_id):
            raise ValueError("非法音频 id")
        facts = blob_facts(raw_audio_id)
        removed = facts is not None
        if facts is not None:
            facts.path.unlink()
        # 即使是“已不存在”的幂等重试也要 fsync：上一次可能已
        # unlink 但在目录 fsync 时失败。未重新确认耐久性前不得回 200。
        directory_fd = os.open(AUDIO_DIR, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return removed
    if mutation_lock_held:
        return delete_locked()
    with blob_mutation_lock(raw_audio_id):
        return delete_locked()
