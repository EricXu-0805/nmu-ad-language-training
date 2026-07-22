#!/usr/bin/env python3
"""Build the internal, fail-closed inventory for weekly training images.

The original ``每周图片材料`` directory is an evidence source, not a web asset
directory.  This script only reads it and writes a deterministic JSON inventory
under ``platform/content``.  It deliberately does not copy, resize, transcode,
approve, or enable any image at runtime.

Use ``--decode-verify`` when Pillow is installed to fully decode each PNG in
addition to the default signature/chunk/header checks.  Full decoding never
changes the generated manifest.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import zlib


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from app.image_id_contract import (  # noqa: E402
    IMAGE_ID_CONTRACT_VERSION,
    expected_training_image_ids,
    image_id_for_slot,
    is_training_image_id,
)

DEFAULT_SOURCE_ROOT = PLATFORM_ROOT.parent / "每周图片材料"
DEFAULT_OUTPUT = PLATFORM_ROOT / "content" / "training_asset_manifest_v1.json"

WEEK_DIRECTORIES = {
    2: "第二周",
    3: "第三周",
    4: "第四周",
    5: "第五周",
    6: "第六周",
    7: "第七周",
    8: "第八周",
}
NUMBERED_IMAGE_RE = re.compile(r"^(?P<position>\d+)\.(?P<label>.+)\.png$")
MULTI_IMAGE_RE = re.compile(r"^多要素图片(?:训练)?(?P<position>[12])\.png$")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PNG_BYTES = 25 * 1024 * 1024
MAX_PNG_PIXELS = 20_000_000
MAX_INFLATED_IDAT_BYTES = 128 * 1024 * 1024
PNG_COLOR_MODES = {
    0: "grayscale",
    2: "rgb",
    3: "indexed",
    4: "grayscale_alpha",
    6: "rgba",
}
TEXT_CHUNKS = frozenset({"tEXt", "zTXt", "iTXt"})
KNOWN_CRITICAL_CHUNKS = frozenset({"IHDR", "PLTE", "IDAT", "IEND"})
VALID_BIT_DEPTHS = {
    0: frozenset({1, 2, 4, 8, 16}),
    2: frozenset({8, 16}),
    3: frozenset({1, 2, 4, 8}),
    4: frozenset({8, 16}),
    6: frozenset({8, 16}),
}
CHANNELS_BY_COLOR_TYPE = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}

MANIFEST_SCHEMA_VERSION = "training-asset-manifest.v1"
MANIFEST_VERSION_ID = "weekly-picture-source-20260720.5"
# Release pin: changing any manifest definition field or any source byte requires
# both a new MANIFEST_VERSION_ID and a newly reviewed digest.  This is filled
# with the canonical digest after the schema below is generated and reviewed.
RELEASED_DEFINITION_DIGESTS = {
    MANIFEST_VERSION_ID: "aed9ed719ae2e85f4de4940753c58147ca1b26d34f9bba7c2d3eb80268a50d5e",
}

APPROVALS_PENDING = {
    "rights": {
        "status": "pending",
        "required_evidence": "provenance_and_research_use_authorization",
    },
    "visual": {
        "status": "pending",
        "required_evidence": "two_person_visual_qc",
    },
    "content": {
        "status": "pending",
        "required_evidence": "pi_approved_item_and_protocol_mapping",
    },
}

# These are audit observations, not silent corrections.  Every item remains
# blocked until the content owner resolves the source-of-truth decision.
REVIEW_FINDINGS = (
    {
        "finding_id": "visual-answer-text-wk2-multi-01",
        "category": "answer_text_visible",
        "severity": "blocking",
        "status": "open",
        "image_ids": ["wk2-multi-01"],
        "summary": "图中可直接读到场景答案‘动物园’，会泄露情境识别题答案。",
        "required_resolution": "PI 冻结去字版或明确调整任务，并重做视觉审核。",
    },
    {
        "finding_id": "visual-answer-text-wk2-multi-02",
        "category": "answer_text_visible",
        "severity": "blocking",
        "status": "open",
        "image_ids": ["wk2-multi-02"],
        "summary": "图中可直接读到场景答案‘公园’，会泄露情境识别题答案。",
        "required_resolution": "PI 冻结去字版或明确调整任务，并重做视觉审核。",
    },
    {
        "finding_id": "visual-answer-text-wk8-multi-02",
        "category": "answer_text_visible",
        "severity": "blocking",
        "status": "open",
        "image_ids": ["wk8-multi-02"],
        "summary": "图中可直接读到场景答案‘动物园’，会泄露情境识别题答案。",
        "required_resolution": "PI 冻结去字版或明确调整任务，并重做视觉审核。",
    },
    {
        "finding_id": "mapping-order-wk2-21",
        "category": "filename_visual_source_order_conflict",
        "severity": "blocking",
        "status": "open",
        "image_ids": ["wk2-21"],
        "summary": "文件名是‘烟灰缸+烟’，但图像和 Week 2 源稿的可执行方位是左烟、右烟灰缸。",
        "required_resolution": "以 PI 批准的左右映射为准，冻结题库、话术和图片摘要。",
    },
    {
        "finding_id": "mapping-order-wk3-29",
        "category": "filename_visual_source_order_conflict",
        "severity": "blocking",
        "status": "open",
        "image_ids": ["wk3-29"],
        "summary": "文件名是‘水壶+茶杯’，图像是左茶杯、右水壶，且源稿存在方位错误。",
        "required_resolution": "逐项核对图像方位和全部左右话术，由 PI 冻结修订。",
    },
    {
        "finding_id": "mapping-order-wk5-25",
        "category": "filename_visual_source_and_script_conflict",
        "severity": "blocking",
        "status": "open",
        "image_ids": ["wk5-25"],
        "summary": "Word 标题是‘摩托车+头盔’，图像和文件是左头盔、右摩托车，作用/关系话术还有复制错误。",
        "required_resolution": "禁止自动推进；重新冻结左右词、作用和关系评分协议。",
    },
    {
        "finding_id": "mapping-term-wk6-28",
        "category": "source_term_variant_conflict",
        "severity": "blocking",
        "status": "open",
        "image_ids": ["wk6-28"],
        "summary": "源稿使用‘锯’，文件名使用‘锯子’，尚未冻结是否为可接受同义表达。",
        "required_resolution": "PI 冻结目标词、acceptable expressions 及各提示分支。",
    },
    {
        "finding_id": "mapping-term-wk8-26",
        "category": "source_target_term_conflict",
        "severity": "blocking",
        "status": "open",
        "image_ids": ["wk8-26"],
        "summary": "Word 标题是‘戒指+食指’，图像、文件名和纠正话术则指向‘手指’。",
        "required_resolution": "PI 冻结粒度一致的目标词与接受口径。",
    },
    {
        "finding_id": "mapping-order-wk8-27",
        "category": "source_visual_and_cue_order_conflict",
        "severity": "blocking",
        "status": "open",
        "image_ids": ["wk8-27"],
        "summary": "标题是‘衣架+大衣’，图像和纠正话术是左大衣、右衣架，方位提示又互相颠倒。",
        "required_resolution": "重建左右映射并逐分支复核后由 PI 冻结。",
    },
    {
        "finding_id": "mapping-order-wk3-30",
        "category": "filename_visual_source_order_conflict",
        "severity": "blocking",
        "status": "open",
        "image_ids": ["wk3-30"],
        "summary": "文件名和标题是‘鸡+玉米’，但图像及纠正分支均为左玉米、右鸡。",
        "required_resolution": "以 PI 批准的左右映射为准，冻结标题、图片摘要和全部左右话术。",
    },
    {
        "finding_id": "mapping-order-function-wk4-21",
        "category": "filename_visual_and_function_semantics_conflict",
        "severity": "blocking",
        "status": "open",
        "image_ids": ["wk4-21"],
        "summary": "文件名和标题是‘窗户+窗帘’，图像与纠正分支为左窗帘、右窗户，但两侧作用提示的语义又彼此对调。",
        "required_resolution": "逐字段重建左右目标、作用提示和关系分支并由 PI 冻结。",
    },
    {
        "finding_id": "mapping-term-wk4-25",
        "category": "source_target_term_conflict",
        "severity": "blocking",
        "status": "open",
        "image_ids": ["wk4-25"],
        "summary": "标题和右侧图像指向‘向日葵’，纠正分支却只告知‘花’，目标概念粒度未冻结。",
        "required_resolution": "PI 冻结目标词、可接受表达及纠正答案，禁止模型自行把上位词当作精确答案。",
    },
    {
        "finding_id": "mapping-order-wk5-30",
        "category": "filename_visual_and_cue_order_conflict",
        "severity": "blocking",
        "status": "open",
        "image_ids": ["wk5-30"],
        "summary": "文件名和标题是‘手+手套’，图像与纠正分支为左手套、右手，作用提示中的左右词又反写。",
        "required_resolution": "重建左右映射和作用提示后由 PI 与临床内容双人冻结。",
    },
    {
        "finding_id": "mapping-order-wk6-23",
        "category": "filename_visual_and_cue_order_conflict",
        "severity": "blocking",
        "status": "open",
        "image_ids": ["wk6-23"],
        "summary": "标题和文件名为‘衣架+大衣’，图像与纠正分支为左大衣、右衣架，作用提示中的左右词又反写。",
        "required_resolution": "重建左右映射并逐分支复核后由 PI 冻结。",
    },
    {
        "finding_id": "mapping-order-function-wk7-21",
        "category": "filename_visual_and_function_semantics_conflict",
        "severity": "blocking",
        "status": "open",
        "image_ids": ["wk7-21"],
        "summary": "文件名和标题是‘窗户+窗帘’，图像与纠正分支为左窗帘、右窗户，但两侧作用提示的语义又彼此对调。",
        "required_resolution": "逐字段重建左右目标、作用提示和关系分支并由 PI 冻结。",
    },
    {
        "finding_id": "cue-direction-wk7-23",
        "category": "source_cue_direction_conflict",
        "severity": "blocking",
        "status": "open",
        "image_ids": ["wk7-23"],
        "summary": "图像与纠正分支为左猴子、右香蕉，但猴子作用提示写‘右边’，香蕉作用提示写‘左边’。",
        "required_resolution": "修订两侧作用提示并完成图片、目标、方位与话术双人核对。",
    },
    {
        "finding_id": "cue-direction-wk8-28",
        "category": "source_cue_direction_conflict",
        "severity": "blocking",
        "status": "open",
        "image_ids": ["wk8-28"],
        "summary": "图像与纠正分支为左猴子、右香蕉，但猴子作用提示写‘右边’，香蕉作用提示写‘左边’。",
        "required_resolution": "修订两侧作用提示并完成图片、目标、方位与话术双人核对。",
    },
    {
        "finding_id": "duplicate-scene-wk3-wk6-multi-01",
        "category": "exact_duplicate_reuse_review",
        "severity": "review",
        "status": "open",
        "image_ids": ["wk3-multi-01", "wk6-multi-01"],
        "summary": "Week 3 与 Week 6 的第一张多要素图为精确相同文件。",
        "required_resolution": "确认该复现是训练设计而非资产误配，并在周次协议中留痕。",
    },
)


def _source_entries(week_dir: Path) -> list[tuple[int, str, str, Path]]:
    """Return ``(slot, image_id_suffix, kind, path)`` in protocol order."""
    numbered: dict[int, Path] = {}
    multi: dict[int, Path] = {}
    unexpected: list[str] = []
    for path in sorted(week_dir.iterdir(), key=lambda candidate: candidate.name):
        if "\\" in path.name or any(ord(character) < 32 for character in path.name):
            unexpected.append(path.name)
            continue
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError as exc:
            raise ValueError(f"{week_dir.name}: 枚举期间文件消失 {path.name}") from exc
        if stat.S_ISLNK(mode):
            raise ValueError(f"{week_dir.name}: 禁止符号链接 {path.name}")
        if not stat.S_ISREG(mode):
            unexpected.append(path.name)
            continue
        match = NUMBERED_IMAGE_RE.fullmatch(path.name)
        if match:
            position = int(match.group("position"))
            if position in numbered:
                raise ValueError(f"{week_dir.name}: 重复编号 {position}")
            numbered[position] = path
            continue
        match = MULTI_IMAGE_RE.fullmatch(path.name)
        if match:
            position = int(match.group("position"))
            if position in multi:
                raise ValueError(f"{week_dir.name}: 重复多要素编号 {position}")
            multi[position] = path
            continue
        unexpected.append(path.name)
    if unexpected:
        raise ValueError(f"{week_dir.name}: 存在未识别项 {unexpected}")
    if set(numbered) != set(range(1, 31)):
        raise ValueError(
            f"{week_dir.name}: 数字题图必须恰好为 1..30，"
            f"实际为 {sorted(numbered)}"
        )
    if set(multi) != {1, 2}:
        raise ValueError(
            f"{week_dir.name}: 多要素题图必须恰好为 1..2，"
            f"实际为 {sorted(multi)}"
        )
    rows = [
        (position, f"{position:02d}", "single" if position <= 20 else "pair", path)
        for position, path in sorted(numbered.items())
    ]
    rows.extend(
        (30 + position, f"multi-{position:02d}", "multi_scene", path)
        for position, path in sorted(multi.items())
    )
    return rows


def _expected_inflated_idat_bytes(
    *, width: int, height: int, bit_depth: int, color_type: int, interlace: int
) -> int:
    bits_per_pixel = CHANNELS_BY_COLOR_TYPE[color_type] * bit_depth

    def pass_size(
        x_start: int, y_start: int, x_step: int, y_step: int
    ) -> int:
        if width <= x_start or height <= y_start:
            return 0
        pass_width = (width - x_start + x_step - 1) // x_step
        pass_height = (height - y_start + y_step - 1) // y_step
        row_bytes = (pass_width * bits_per_pixel + 7) // 8
        return pass_height * (1 + row_bytes)

    if interlace == 0:
        return pass_size(0, 0, 1, 1)
    # Adam7 pass geometry from the PNG specification.
    return sum(pass_size(*geometry) for geometry in (
        (0, 0, 8, 8),
        (4, 0, 8, 8),
        (0, 4, 4, 8),
        (2, 0, 4, 4),
        (0, 2, 2, 4),
        (1, 0, 2, 2),
        (0, 1, 1, 2),
    ))


def _validate_idat_zlib_stream(
    idat_parts: list[bytes], *, expected_size: int, relative_path: str
) -> None:
    if expected_size > MAX_INFLATED_IDAT_BYTES:
        raise ValueError(
            f"{relative_path}: PNG 解压扫描线预算 {expected_size} 超过上限 "
            f"{MAX_INFLATED_IDAT_BYTES}"
        )
    decoder = zlib.decompressobj()
    inflated_size = 0
    try:
        for part in idat_parts:
            pending = part
            while pending:
                previous_size = len(pending)
                remaining = expected_size - inflated_size
                # Permit one extra byte so an oversized stream is detected
                # without materializing unbounded decompressed data.
                decoded = decoder.decompress(pending, max(1, remaining + 1))
                inflated_size += len(decoded)
                if inflated_size > expected_size:
                    raise ValueError(f"{relative_path}: IDAT 解压数据超过 IHDR 预算")
                pending = decoder.unconsumed_tail
                if decoder.unused_data:
                    raise ValueError(f"{relative_path}: IDAT zlib 流后存在尾随压缩数据")
                if pending and len(pending) == previous_size and not decoded:
                    raise ValueError(f"{relative_path}: IDAT zlib 流无法继续解压")
        flushed = decoder.flush(max(1, expected_size - inflated_size + 1))
        inflated_size += len(flushed)
    except zlib.error as exc:
        raise ValueError(f"{relative_path}: IDAT zlib 流损坏") from exc
    if not decoder.eof:
        raise ValueError(f"{relative_path}: IDAT zlib 流不完整")
    if decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError(f"{relative_path}: IDAT zlib 流存在未消费数据")
    if inflated_size != expected_size:
        raise ValueError(
            f"{relative_path}: IDAT 解压扫描线长度应为 {expected_size}，"
            f"实际为 {inflated_size}"
        )


def _png_metadata(data: bytes, *, relative_path: str) -> dict:
    """Validate PNG framing, CRCs and IDAT zlib size without decoding pixels."""
    if len(data) > MAX_PNG_BYTES:
        raise ValueError(f"{relative_path}: PNG 文件超过 {MAX_PNG_BYTES} 字节上限")
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError(f"{relative_path}: 不是 PNG 签名")
    offset = len(PNG_SIGNATURE)
    chunks: list[str] = []
    idat_parts: list[bytes] = []
    ihdr: bytes | None = None
    saw_iend = False
    saw_idat = False
    idat_closed = False
    while offset < len(data):
        if len(data) - offset < 12:
            raise ValueError(f"{relative_path}: PNG chunk 尾部截断")
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        chunk_type_raw = data[offset + 4:offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            raise ValueError(f"{relative_path}: PNG chunk 越界")
        chunk_data = data[offset + 8:offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length:chunk_end])[0]
        actual_crc = zlib.crc32(chunk_type_raw)
        actual_crc = zlib.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ValueError(f"{relative_path}: PNG chunk CRC 错误")
        try:
            chunk_type = chunk_type_raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{relative_path}: PNG chunk 类型非 ASCII") from exc
        if len(chunk_type) != 4 or not chunk_type.isalpha():
            raise ValueError(f"{relative_path}: PNG chunk 类型格式非法")
        if not chunk_type[2].isupper():
            raise ValueError(f"{relative_path}: PNG chunk 保留位非法")
        if chunk_type[0].isupper() and chunk_type not in KNOWN_CRITICAL_CHUNKS:
            raise ValueError(f"{relative_path}: 未知关键 PNG chunk {chunk_type}")
        chunks.append(chunk_type)
        if len(chunks) == 1:
            if chunk_type != "IHDR" or length != 13:
                raise ValueError(f"{relative_path}: 缺合法 IHDR")
            ihdr = chunk_data
        elif chunk_type == "IHDR":
            raise ValueError(f"{relative_path}: IHDR 重复或顺序非法")
        if chunk_type == "PLTE" and saw_idat:
            raise ValueError(f"{relative_path}: PLTE 必须位于 IDAT 之前")
        if chunk_type == "IDAT":
            if idat_closed:
                raise ValueError(f"{relative_path}: IDAT chunks 必须连续")
            saw_idat = True
            idat_parts.append(chunk_data)
        elif saw_idat and chunk_type != "IEND":
            idat_closed = True
        if chunk_type == "IEND":
            if length != 0 or chunk_end != len(data):
                raise ValueError(f"{relative_path}: IEND 非法或存在尾随数据")
            saw_iend = True
            break
        offset = chunk_end
    if ihdr is None or not saw_iend or not saw_idat:
        raise ValueError(f"{relative_path}: PNG 结构不完整")
    width, height, bit_depth, color_type, compression, filtering, interlace = (
        struct.unpack(">IIBBBBB", ihdr)
    )
    if not width or not height:
        raise ValueError(f"{relative_path}: PNG 尺寸非法")
    if color_type not in PNG_COLOR_MODES:
        raise ValueError(f"{relative_path}: 未知 PNG color type {color_type}")
    if bit_depth not in VALID_BIT_DEPTHS[color_type]:
        raise ValueError(
            f"{relative_path}: color type {color_type} 不支持 bit depth {bit_depth}"
        )
    if compression != 0 or filtering != 0 or interlace not in {0, 1}:
        raise ValueError(f"{relative_path}: PNG IHDR 编码字段非法")
    if width * height > MAX_PNG_PIXELS:
        raise ValueError(
            f"{relative_path}: PNG 像素数 {width * height} 超过上限 "
            f"{MAX_PNG_PIXELS}"
        )
    if color_type == 3 and "PLTE" not in chunks:
        raise ValueError(f"{relative_path}: indexed PNG 缺少 PLTE")
    if color_type in {0, 4} and "PLTE" in chunks:
        raise ValueError(f"{relative_path}: grayscale PNG 不得含 PLTE")
    inflated_size = _expected_inflated_idat_bytes(
        width=width,
        height=height,
        bit_depth=bit_depth,
        color_type=color_type,
        interlace=interlace,
    )
    _validate_idat_zlib_stream(
        idat_parts,
        expected_size=inflated_size,
        relative_path=relative_path,
    )
    ancillary = sorted({kind for kind in chunks if kind[0].islower()})
    return {
        "width": width,
        "height": height,
        "bit_depth": bit_depth,
        "color_type": color_type,
        "color_mode": PNG_COLOR_MODES[color_type],
        "interlaced": bool(interlace),
        "pixel_count": width * height,
        "inflated_idat_bytes": inflated_size,
        "verification_level": "structure_crc_ihdr_limits_and_idat_zlib",
        "ancillary_chunk_types": ancillary,
        "has_text_metadata": bool(set(chunks) & TEXT_CHUNKS),
        "has_exif_metadata": "eXIf" in chunks,
    }


def _pillow_decode_verify(data: bytes, expected: dict, display_path: Path) -> None:
    """Decode the exact no-follow bytes already hashed by the inventory pass."""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - environment-dependent branch
        raise RuntimeError(
            "--decode-verify 需要 Pillow；未安装时请去掉该参数"
        ) from exc
    with Image.open(BytesIO(data)) as image:
        image.verify()
    with Image.open(BytesIO(data)) as image:
        image.load()
        if image.size != (expected["width"], expected["height"]):
            raise ValueError(f"{display_path}: Pillow 解码尺寸与 IHDR 不一致")


def _label_from_filename(path: Path) -> str | None:
    match = NUMBERED_IMAGE_RE.fullmatch(path.name)
    return match.group("label") if match else None


def _read_regular_file_no_follow(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"禁止读取符号链接或不可访问资产: {path}") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(f"资产必须是普通文件: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as source_file:
            data = source_file.read(MAX_PNG_BYTES + 1)
        if len(data) > MAX_PNG_BYTES:
            raise ValueError(f"{path}: PNG 文件超过 {MAX_PNG_BYTES} 字节上限")
        return data
    finally:
        os.close(descriptor)


def _assert_source_tree_is_real(source_root: Path) -> Path:
    root = source_root.absolute()
    # ``Path.lstat(root)`` alone misses an ancestor symlink such as
    # ``alias/每周图片材料`` where only ``alias`` is a link.  Walk the lexical
    # absolute path one component at a time; do not call resolve(), which would
    # erase precisely the evidence we need to reject.
    probe = Path(root.anchor)
    for component in root.parts[1:]:
        probe /= component
        try:
            component_mode = probe.lstat().st_mode
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"图片源路径不存在: {probe}") from exc
        if stat.S_ISLNK(component_mode):
            raise ValueError(f"图片源路径任一级均不得为符号链接: {probe}")
    try:
        root_mode = root.lstat().st_mode
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"图片源目录不存在: {root}") from exc
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise ValueError(f"图片源必须是真实目录且不得为符号链接: {root}")
    expected_entries = set(WEEK_DIRECTORIES.values())
    actual_entries = {entry.name for entry in root.iterdir()}
    if actual_entries != expected_entries:
        missing = sorted(expected_entries - actual_entries)
        unexpected = sorted(actual_entries - expected_entries)
        raise ValueError(
            "图片源顶层必须恰好为第 2–8 周冻结目录；"
            f"缺失={missing}，未登记={unexpected}"
        )
    for week_directory in WEEK_DIRECTORIES.values():
        week_dir = root / week_directory
        try:
            week_mode = week_dir.lstat().st_mode
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"缺周次目录: {week_dir}") from exc
        if stat.S_ISLNK(week_mode) or not stat.S_ISDIR(week_mode):
            raise ValueError(f"周次目录不得为符号链接且必须是目录: {week_dir}")
    return root


def _inventory_digest(assets: list[dict]) -> str:
    evidence = [
        {
            "image_id": asset["image_id"],
            "relative_path": asset["source"]["relative_path"],
            "sha256": asset["source"]["sha256"],
            "byte_size": asset["source"]["byte_size"],
            "png": asset["source"]["png"],
        }
        for asset in assets
    ]
    canonical = json.dumps(
        evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _definition_digest(manifest: dict) -> str:
    """Hash every manifest field except the digest's own top-level value."""
    definition = {
        key: value
        for key, value in manifest.items()
        if key != "definition_sha256"
    }
    canonical = json.dumps(
        definition,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_manifest(source_root: Path, *, decode_verify: bool = False) -> dict:
    source_root = _assert_source_tree_is_real(source_root)

    assets: list[dict] = []
    finding_ids_by_image: dict[str, list[str]] = defaultdict(list)
    for finding in REVIEW_FINDINGS:
        for image_id in finding["image_ids"]:
            finding_ids_by_image[image_id].append(finding["finding_id"])

    for week_no, week_directory in WEEK_DIRECTORIES.items():
        week_dir = source_root / week_directory
        for slot_index, _suffix, kind, path in _source_entries(week_dir):
            image_id = image_id_for_slot(week_no, slot_index)
            relative_path = path.relative_to(source_root).as_posix()
            data = _read_regular_file_no_follow(path)
            png = _png_metadata(data, relative_path=relative_path)
            if decode_verify:
                _pillow_decode_verify(data, png, path)
            assets.append({
                "image_id": image_id,
                "week_no": week_no,
                "slot_index": slot_index,
                "asset_kind": kind,
                "source": {
                    "visibility": "internal_only",
                    "relative_path": relative_path,
                    "filename": path.name,
                    "answer_bearing_label": _label_from_filename(path),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "byte_size": len(data),
                    "png": png,
                },
                "delivery": {
                    "public_path": None,
                    "status": "blocked_not_published",
                    "runtime_transport_if_approved": (
                        "session_current_asset_endpoint_only"
                    ),
                    "legacy_static_asset_status": (
                        "removed_and_route_blocked"
                        if week_no == 2 and slot_index <= 30
                        else "not_applicable"
                    ),
                    "generated_or_copied_by_this_manifest": False,
                },
                "approvals": json.loads(json.dumps(APPROVALS_PENDING)),
                "release_status": "blocked_pending_approvals",
                "review_finding_ids": sorted(finding_ids_by_image.get(image_id, [])),
            })

    digest_to_ids: dict[str, list[str]] = defaultdict(list)
    for asset in assets:
        digest_to_ids[asset["source"]["sha256"]].append(asset["image_id"])
    for asset in assets:
        ids = sorted(digest_to_ids[asset["source"]["sha256"]])
        asset["source"]["content_digest_occurrences"] = len(ids)
        asset["source"]["canonical_image_id"] = ids[0]
        asset["source"]["content_reuse"] = {
            "same_bytes_image_ids": ids,
            "semantics": (
                "distinct_protocol_slots_share_exact_source_bytes"
                if len(ids) > 1
                else "unique_source_bytes"
            ),
            "content_approval_required": len(ids) > 1,
        }

    week_counts = Counter(asset["week_no"] for asset in assets)
    weeks = [{
        "week_no": 1,
        "source_status": "no_images",
        "asset_count": 0,
        "runtime_content_status": "rapport_and_baseline_flow_no_image_library",
        "release_status": "not_applicable",
    }]
    for week_no in WEEK_DIRECTORIES:
        weeks.append({
            "week_no": week_no,
            "source_status": "source_assets_present",
            "asset_count": week_counts[week_no],
            "runtime_content_status": (
                "existing_week2_item_bank_only"
                if week_no == 2
                else "blocked_unstructured_week"
            ),
            "release_status": "blocked_pending_approvals",
        })

    manifest = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "manifest_version_id": MANIFEST_VERSION_ID,
        "image_id_contract_version": IMAGE_ID_CONTRACT_VERSION,
        "definition_sha256": "",
        "generated_by": "scripts/generate_training_asset_manifest.py",
        "purpose": "internal_inventory_only_not_a_runtime_content_definition",
        "asset_verification": {
            "default_level": "png_structure_crc_ihdr_limits_and_idat_zlib",
            "verified_by_default": [
                "regular_file_without_symlink",
                "png_signature_chunk_order_and_crc",
                "ihdr_encoding_and_pixel_budget",
                "idat_zlib_integrity_and_expected_inflated_scanline_size",
            ],
            "not_verified_by_default": [
                "scanline_filter_values_and_pixel_reconstruction",
                "rendered_pixel_or_color_semantics",
                "visual_content_correctness_or_clinical_suitability",
            ],
            "optional_decode_flag": "--decode-verify",
            "optional_decode_level": "pillow_verify_and_full_pixel_load",
            "optional_decode_result_recorded_in_manifest": False,
        },
        "safety_boundary": {
            "generator_does_not_mutate_source": True,
            "source_filesystem_immutability_verified": False,
            "copies_or_derivatives_created": False,
            "runtime_weeks_changed": False,
            "approvals_inferred_from_file_presence": False,
        },
        "source_repository": {
            "display_name": "每周图片材料",
            "expected_relative_location": "../每周图片材料",
            "handling": "external_read_only_evidence_source",
            "asset_count": len(assets),
            "unique_sha256_count": len(digest_to_ids),
            "inventory_sha256": _inventory_digest(assets),
        },
        "weeks": weeks,
        "review_findings": list(REVIEW_FINDINGS),
        "assets": assets,
    }
    manifest["definition_sha256"] = _definition_digest(manifest)
    _validate_generated_manifest(manifest)
    return manifest


def _validate_generated_manifest(manifest: dict) -> None:
    expected_definition_digest = _definition_digest(manifest)
    if manifest.get("definition_sha256") != expected_definition_digest:
        raise ValueError("definition_sha256 与清单定义不一致")
    released_digest = RELEASED_DEFINITION_DIGESTS.get(MANIFEST_VERSION_ID)
    if released_digest is None:
        raise ValueError(f"清单版本尚未登记发布摘要: {MANIFEST_VERSION_ID}")
    if expected_definition_digest != released_digest:
        raise ValueError(
            "清单定义或源图片已经变化；必须升级 MANIFEST_VERSION_ID 并经复核后"
            f"登记新 definition_sha256（当前 {expected_definition_digest}）"
        )

    expected_top_level_keys = {
        "manifest_schema_version",
        "manifest_version_id",
        "image_id_contract_version",
        "definition_sha256",
        "generated_by",
        "purpose",
        "asset_verification",
        "safety_boundary",
        "source_repository",
        "weeks",
        "review_findings",
        "assets",
    }
    if set(manifest) != expected_top_level_keys:
        raise ValueError("清单顶层字段与冻结 schema 不一致")
    if manifest["manifest_schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ValueError("清单 schema 版本不匹配")
    if manifest["manifest_version_id"] != MANIFEST_VERSION_ID:
        raise ValueError("清单版本 ID 不匹配")
    if manifest["image_id_contract_version"] != IMAGE_ID_CONTRACT_VERSION:
        raise ValueError("图片 ID 合同版本不匹配")

    assets = manifest["assets"]
    ids = [asset["image_id"] for asset in assets]
    if len(assets) != 224 or len(ids) != len(set(ids)):
        raise ValueError("清单必须包含 224 个唯一 image_id")
    if len({asset["source"]["sha256"] for asset in assets}) != 149:
        raise ValueError("当前源库应包含 149 个唯一 SHA-256")
    if any(asset["approvals"] != APPROVALS_PENDING for asset in assets):
        raise ValueError("未审批资产不得自动改变审批状态")
    expected_ids = expected_training_image_ids()
    if set(ids) != expected_ids:
        raise ValueError("清单 image_id/周次题位集合不完整")

    for asset in assets:
        if set(asset) != {
            "image_id", "week_no", "slot_index", "asset_kind", "source",
            "delivery", "approvals", "release_status", "review_finding_ids",
        }:
            raise ValueError(f"资产字段与冻结 schema 不一致: {asset.get('image_id')}")
        image_id = asset["image_id"]
        week_no = asset["week_no"]
        slot_index = asset["slot_index"]
        expected_id = image_id_for_slot(week_no, slot_index)
        if (
            image_id != expected_id
            or not is_training_image_id(image_id)
            or week_no not in WEEK_DIRECTORIES
        ):
            raise ValueError(f"资产题位绑定不一致: {image_id}")
        expected_kind = (
            "single" if slot_index <= 20
            else "pair" if slot_index <= 30
            else "multi_scene"
        )
        if asset["asset_kind"] != expected_kind:
            raise ValueError(f"资产题型与题位不一致: {image_id}")
        if asset["release_status"] != "blocked_pending_approvals":
            raise ValueError(f"未审批资产不得放行: {image_id}")

        source = asset["source"]
        if set(source) != {
            "visibility", "relative_path", "filename", "answer_bearing_label",
            "sha256", "byte_size", "png", "content_digest_occurrences",
            "canonical_image_id", "content_reuse",
        }:
            raise ValueError(f"来源字段与冻结 schema 不一致: {image_id}")
        relative_path = PurePosixPath(source["relative_path"])
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or len(relative_path.parts) != 2
            or relative_path.parts[0] != WEEK_DIRECTORIES[week_no]
            or relative_path.name != source["filename"]
        ):
            raise ValueError(f"来源路径越界或周次不一致: {image_id}")
        if source["visibility"] != "internal_only":
            raise ValueError(f"来源可见性不得放宽: {image_id}")
        if not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
            raise ValueError(f"来源 SHA-256 非法: {image_id}")

        delivery = asset["delivery"]
        if delivery != {
            "public_path": None,
            "status": "blocked_not_published",
            "runtime_transport_if_approved": "session_current_asset_endpoint_only",
            "legacy_static_asset_status": (
                "removed_and_route_blocked"
                if week_no == 2 and slot_index <= 30
                else "not_applicable"
            ),
            "generated_or_copied_by_this_manifest": False,
        }:
            raise ValueError(f"资产发布边界被放宽: {image_id}")

    if manifest["source_repository"]["inventory_sha256"] != _inventory_digest(assets):
        raise ValueError("inventory_sha256 与资产证据不一致")

    digests_to_ids: dict[str, list[str]] = defaultdict(list)
    for asset in assets:
        digests_to_ids[asset["source"]["sha256"]].append(asset["image_id"])
    for asset in assets:
        source = asset["source"]
        same_ids = sorted(digests_to_ids[source["sha256"]])
        if source["content_digest_occurrences"] != len(same_ids):
            raise ValueError(f"重复计数不一致: {asset['image_id']}")
        if source["canonical_image_id"] != same_ids[0]:
            raise ValueError(f"重复内容 canonical ID 不一致: {asset['image_id']}")
        if source["content_reuse"]["same_bytes_image_ids"] != same_ids:
            raise ValueError(f"重复内容成员不一致: {asset['image_id']}")

    known_ids = set(ids)
    for finding in manifest["review_findings"]:
        if not set(finding["image_ids"]) <= known_ids:
            raise ValueError(f"审查发现引用不存在的 image_id: {finding['finding_id']}")


def _json_bytes(manifest: dict) -> bytes:
    return (json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ) + "\n").encode("utf-8")


def _assert_output_outside_source(source_root: Path, output: Path) -> None:
    source = source_root.resolve()
    target = output.resolve()
    if target == source or source in target.parents:
        raise ValueError("输出不得写入只读图片源目录")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--decode-verify",
        action="store_true",
        help="使用可选 Pillow 完整解码验证（不会改变输出）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="仅校验已生成清单是否与源库一致，不写文件",
    )
    args = parser.parse_args(argv)
    _assert_output_outside_source(args.source_root, args.output)
    payload = _json_bytes(build_manifest(
        args.source_root,
        decode_verify=args.decode_verify,
    ))
    if args.check:
        if not args.output.is_file() or args.output.read_bytes() != payload:
            print(f"清单缺失或已过期: {args.output}", file=sys.stderr)
            return 1
        print(f"清单与 224 张源图一致: {args.output}")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    print(f"已生成 {len(json.loads(payload)['assets'])} 条资产清单: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
