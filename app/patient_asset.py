"""受试者端当前题图片的服务端资产边界。

这里故意不接受 URL、文件名或相对路径。运行时只会把冻结题库里的
``image_id`` 与受控资产目录的实际 WebP 文件求交集，再返回完整字节。
因此 HTTP 输入无法参与路径拼接，题库中的越界值和资产目录中的符号链接
也都会 fail closed。
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .content import ItemBank
from .image_id_contract import (
    IMAGE_ID_CONTRACT_VERSION,
    is_training_image_id,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 运行图片与前端 public/dist 物理分离。Docker 会将 content 复制为
# root-owned 只读目录，应用进程不能把新图替换进正在执行的试验。
RUNTIME_ASSET_DIRS: tuple[Path, ...] = (
    PROJECT_ROOT / "content" / "patient_assets",
)
DELIVERY_MANIFEST_PATH = PROJECT_ROOT / "content" / "patient_asset_delivery_v1.json"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_ASSET_BYTES = 16 * 1024 * 1024
_MAX_MANIFEST_BYTES = 128 * 1024
_MAX_ASSET_DIMENSION = 8192
_MAX_ASSET_PIXELS = 20_000_000

# release_scope 与 purpose/status/审批事实严格配对;不在配对表里的组合一律拒绝。
# 研究发布不是改一个字段:必须同时携带"来源转换权利"与"内容"两份可校验审批事实,
# 且清单摘要变化会连带要求冻结题库重新绑定——签字工件本身就是门禁。
_BASE_MANIFEST_KEYS = frozenset({
    "manifest_schema_version", "manifest_version_id", "definition_sha256",
    "image_id_contract_version",
    "purpose", "release_scope", "research_release_status",
    "media_type", "assets",
})
_SIMULATION_PURPOSE = "simulation_private_byte_allowlist_not_research_asset_definition"
_SIMULATION_RELEASE_STATUS = (
    "blocked_pending_source_transform_rights_and_content_approvals")
_RESEARCH_PURPOSE = "research_and_simulation_byte_allowlist"
_RESEARCH_RELEASE_STATUS = "approved_for_research_presentation"
_RESEARCH_APPROVAL_KEYS = frozenset({"source_transform_rights", "content"})
_APPROVED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PatientAssetError(RuntimeError):
    """资产定义或物理字节不可安全使用。"""


@dataclass(frozen=True)
class RuntimeAsset:
    path: Path
    expected_sha256: str
    expected_byte_size: int
    expected_width: int
    expected_height: int
    expected_frame_count: int
    media_type: str = "image/webp"


def _strict_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise PatientAssetError("图片交付清单含重复 JSON 键")
        result[key] = value
    return result


def _canonical_digest(definition: dict, *, exclude: frozenset[str]) -> str:
    payload = {
        key: value for key, value in definition.items()
        if key not in exclude
    }
    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PatientAssetError("图片交付清单无法规范化") from exc
    return hashlib.sha256(canonical).hexdigest()


def _validate_research_release_approvals(definition: dict) -> None:
    """研究发布审批事实:两份具名签署,scope 摘要必须钉住被批准的清单内容本身。"""
    approvals = definition.get("research_release_approvals")
    if (not isinstance(approvals, dict)
            or set(approvals) != _RESEARCH_APPROVAL_KEYS):
        raise PatientAssetError("图片交付清单研究审批事实不符合封闭契约")
    expected_scope = _canonical_digest(
        definition,
        exclude=frozenset({"definition_sha256", "research_release_approvals"}))
    for fact in approvals.values():
        if not isinstance(fact, dict) or set(fact) != {
            "approved_by", "approved_at", "scope_sha256",
        }:
            raise PatientAssetError("图片交付清单研究审批事实字段非法")
        approved_by = fact.get("approved_by")
        approved_at = fact.get("approved_at")
        if (not isinstance(approved_by, str) or not approved_by.strip()
                or not isinstance(approved_at, str)
                or _APPROVED_AT_RE.fullmatch(approved_at) is None
                or fact.get("scope_sha256") != expected_scope):
            raise PatientAssetError("图片交付清单研究审批事实值非法")


def _read_delivery_definition() -> dict:
    """Load and self-verify the delivery manifest contract (no bank binding)."""
    try:
        raw = DELIVERY_MANIFEST_PATH.read_bytes()
    except OSError as exc:
        raise PatientAssetError("图片交付清单不可读") from exc
    if not raw or len(raw) > _MAX_MANIFEST_BYTES:
        raise PatientAssetError("图片交付清单大小异常")
    try:
        definition = json.loads(raw, object_pairs_hook=_strict_object)
    except PatientAssetError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PatientAssetError("图片交付清单不是合法 JSON") from exc
    if not isinstance(definition, dict):
        raise PatientAssetError("图片交付清单字段不符合封闭契约")
    scope = definition.get("release_scope")
    if scope == "simulation_only":
        allowed_keys = _BASE_MANIFEST_KEYS
        expected_purpose = _SIMULATION_PURPOSE
        expected_status = _SIMULATION_RELEASE_STATUS
    elif scope == "research_and_simulation":
        allowed_keys = _BASE_MANIFEST_KEYS | {"research_release_approvals"}
        expected_purpose = _RESEARCH_PURPOSE
        expected_status = _RESEARCH_RELEASE_STATUS
    else:
        raise PatientAssetError("图片交付清单版本、范围或媒体类型非法")
    if set(definition) != allowed_keys:
        raise PatientAssetError("图片交付清单字段不符合封闭契约")
    if (definition.get("manifest_schema_version") != "patient-asset-delivery.v1"
            or not isinstance(definition.get("manifest_version_id"), str)
            or not definition["manifest_version_id"].strip()
            or definition.get("image_id_contract_version")
            != IMAGE_ID_CONTRACT_VERSION
            or definition.get("purpose") != expected_purpose
            or definition.get("research_release_status") != expected_status
            or definition.get("media_type") != "image/webp"):
        raise PatientAssetError("图片交付清单版本、范围或媒体类型非法")
    if scope == "research_and_simulation":
        _validate_research_release_approvals(definition)
    claimed_digest = definition.get("definition_sha256")
    if not isinstance(claimed_digest, str) or _HEX64_RE.fullmatch(claimed_digest) is None:
        raise PatientAssetError("图片交付清单定义摘要非法")
    if _canonical_digest(
            definition,
            exclude=frozenset({"definition_sha256"})) != claimed_digest:
        raise PatientAssetError("图片交付清单定义摘要不匹配")
    return definition


def research_release_approved() -> bool:
    """当前图片交付清单是否已批准真实研究呈现;任何异常一律 False(fail-closed)。"""
    try:
        definition = _read_delivery_definition()
    except PatientAssetError:
        return False
    return definition["release_scope"] == "research_and_simulation"


def _delivery_manifest(bank: ItemBank) -> dict[str, dict]:
    """Load and self-verify the immutable opaque-id-to-byte-digest allowlist."""
    definition = _read_delivery_definition()
    claimed_digest = definition["definition_sha256"]

    binding = bank.meta.get("patient_asset_delivery_manifest")
    if (not isinstance(binding, dict)
            or set(binding) != {"version_id", "definition_sha256"}
            or binding.get("version_id") != definition["manifest_version_id"]
            or binding.get("definition_sha256") != claimed_digest):
        raise PatientAssetError("冻结题库未绑定当前图片交付清单定义")

    rows = definition.get("assets")
    if not isinstance(rows, list) or not rows:
        raise PatientAssetError("图片交付清单缺少资产")
    result: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "image_id", "sha256", "byte_size", "width", "height",
            "frame_count",
        }:
            raise PatientAssetError("图片交付资产字段不符合封闭契约")
        image_id = row.get("image_id")
        sha256 = row.get("sha256")
        byte_size = row.get("byte_size")
        width = row.get("width")
        height = row.get("height")
        frame_count = row.get("frame_count")
        if (not is_training_image_id(image_id)
                or not isinstance(sha256, str) or _HEX64_RE.fullmatch(sha256) is None
                or not isinstance(byte_size, int) or isinstance(byte_size, bool)
                or byte_size <= 12 or byte_size > _MAX_ASSET_BYTES
                or not isinstance(width, int) or isinstance(width, bool)
                or not isinstance(height, int) or isinstance(height, bool)
                or width < 1 or height < 1
                or width > _MAX_ASSET_DIMENSION
                or height > _MAX_ASSET_DIMENSION
                or width * height > _MAX_ASSET_PIXELS
                or not isinstance(frame_count, int)
                or isinstance(frame_count, bool)
                or frame_count != 1):
            raise PatientAssetError("图片交付资产值非法")
        if image_id in result:
            raise PatientAssetError("图片交付清单含重复 opaque image id")
        result[image_id] = row
    return result


def _bank_image_ids(bank: ItemBank) -> frozenset[str]:
    """Return only schema-shaped ids from the server-controlled item bank."""
    ids: set[str] = set()
    for item in (*bank.single_element, *bank.double_element, *bank.multi_element):
        value = item.get("image_id") if isinstance(item, dict) else None
        if is_training_image_id(value):
            ids.add(value)
    return frozenset(ids)


def _asset_map(bank: ItemBank) -> dict[str, RuntimeAsset]:
    """Enumerate controlled directories; never derive a path from a request value."""
    allowed_ids = _bank_image_ids(bank)
    delivered = _delivery_manifest(bank)
    assets: dict[str, RuntimeAsset] = {}
    for configured_root in RUNTIME_ASSET_DIRS:
        try:
            root = configured_root.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError):
            continue
        if not root.is_dir():
            continue
        try:
            entries = tuple(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            # A release image tree is immutable.  Refuse links even when their
            # resolved target would currently remain below root, eliminating a
            # later retarget/TOCTOU path in writable development workspaces.
            if entry.is_symlink() or entry.suffix.lower() != ".webp":
                continue
            image_id = entry.stem
            if (image_id not in allowed_ids or image_id not in delivered
                    or image_id in assets):
                continue
            try:
                resolved = entry.resolve(strict=True)
            except (FileNotFoundError, OSError, RuntimeError):
                continue
            if not resolved.is_relative_to(root) or not resolved.is_file():
                continue
            delivery = delivered[image_id]
            assets[image_id] = RuntimeAsset(
                path=resolved,
                expected_sha256=delivery["sha256"],
                expected_byte_size=delivery["byte_size"],
                expected_width=delivery["width"],
                expected_height=delivery["height"],
                expected_frame_count=delivery["frame_count"],
            )
    return assets


def resolve_runtime_asset(bank: ItemBank, image_id: str) -> RuntimeAsset:
    """把冻结计划中的图片标识映射到受控物理文件。

    ``image_id`` 必须已经来自服务端冻结计划，不得将请求参数传入。
    即便调用方误传了越界值，这里也只做字典查找而不做路径运算。
    """
    if not is_training_image_id(image_id):
        raise PatientAssetError("当前题图片定义不合法")
    asset = _asset_map(bank).get(image_id)
    if asset is None:
        raise PatientAssetError("当前题图片尚未安全部署")
    return asset


def _webp_metadata(payload: bytes) -> tuple[int, int, int]:
    """Parse the one-frame simple VP8 profile accepted by this release."""
    if (
        len(payload) < 30
        or payload[:4] != b"RIFF"
        or payload[8:12] != b"WEBP"
        or int.from_bytes(payload[4:8], "little") != len(payload) - 8
    ):
        raise PatientAssetError("当前题图片格式不是受控 WebP")
    offset = 12
    chunks: list[tuple[bytes, bytes]] = []
    while offset < len(payload):
        if len(payload) - offset < 8:
            raise PatientAssetError("当前题图片 WebP chunk 截断")
        kind = payload[offset:offset + 4]
        chunk_size = int.from_bytes(payload[offset + 4:offset + 8], "little")
        data_start = offset + 8
        data_end = data_start + chunk_size
        padded_end = data_end + (chunk_size & 1)
        if data_end > len(payload) or padded_end > len(payload):
            raise PatientAssetError("当前题图片 WebP chunk 越界")
        chunks.append((kind, payload[data_start:data_end]))
        offset = padded_end
    if offset != len(payload) or len(chunks) != 1 or chunks[0][0] != b"VP8 ":
        # Deliberately reject animation, extended containers, metadata and
        # lossless variants until their decoder budgets receive a new review.
        raise PatientAssetError("当前题图片不是单帧受控 VP8 WebP")
    frame = chunks[0][1]
    if (
        len(frame) < 10
        or int.from_bytes(frame[:3], "little") & 1
        or frame[3:6] != b"\x9d\x01\x2a"
    ):
        raise PatientAssetError("当前题图片 VP8 帧头非法")
    raw_width = int.from_bytes(frame[6:8], "little")
    raw_height = int.from_bytes(frame[8:10], "little")
    if raw_width & 0xC000 or raw_height & 0xC000:
        raise PatientAssetError("当前题图片 VP8 缩放位不受支持")
    width = raw_width & 0x3FFF
    height = raw_height & 0x3FFF
    if (
        width < 1
        or height < 1
        or width > _MAX_ASSET_DIMENSION
        or height > _MAX_ASSET_DIMENSION
        or width * height > _MAX_ASSET_PIXELS
    ):
        raise PatientAssetError("当前题图片解码尺寸超过安全预算")
    return width, height, 1


def read_runtime_asset(asset: RuntimeAsset) -> bytes:
    """Read one regular WebP without following a last-moment symlink swap."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(asset.path, flags)
    except OSError as exc:
        raise PatientAssetError("当前题图片无法安全读取") from exc
    try:
        facts = os.fstat(fd)
        if not stat.S_ISREG(facts.st_mode):
            raise PatientAssetError("当前题图片不是常规文件")
        if (facts.st_size != asset.expected_byte_size
                or facts.st_size <= 12 or facts.st_size > _MAX_ASSET_BYTES):
            raise PatientAssetError("当前题图片字节大小异常")
        chunks: list[bytes] = []
        remaining = _MAX_ASSET_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise PatientAssetError("当前题图片无法完整读取") from exc
    finally:
        os.close(fd)
    payload = b"".join(chunks)
    if len(payload) != facts.st_size or len(payload) > _MAX_ASSET_BYTES:
        raise PatientAssetError("当前题图片在读取期间发生变化")
    if hashlib.sha256(payload).hexdigest() != asset.expected_sha256:
        raise PatientAssetError("当前题图片字节摘要与交付清单不匹配")
    width, height, frame_count = _webp_metadata(payload)
    if (
        width != asset.expected_width
        or height != asset.expected_height
        or frame_count != asset.expected_frame_count
    ):
        raise PatientAssetError("当前题图片解码事实与交付清单不匹配")
    return payload
