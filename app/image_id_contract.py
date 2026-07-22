"""Canonical opaque identifier contract for training stimulus images.

The source inventory, frozen item bank and private runtime delivery must agree
on one grammar.  Numbered single/pair stimuli occupy positions 01..30; the two
multi-scene stimuli use explicit ``multi-01``/``multi-02`` suffixes.  Numeric
31/32 are deliberately invalid so callers cannot silently remap a multi-scene
source slot to an identifier that is absent from the source inventory.
"""
from __future__ import annotations

import re


IMAGE_ID_CONTRACT_VERSION = "training-image-id.v1"
TRAINING_WEEK_NUMBERS = tuple(range(2, 9))
NUMBERED_IMAGE_POSITIONS = tuple(range(1, 31))
MULTI_IMAGE_POSITIONS = (1, 2)

_IMAGE_ID_RE = re.compile(
    r"^wk(?P<week>[2-8])-(?:(?P<number>0[1-9]|[12][0-9]|30)"
    r"|multi-(?P<multi>0[12]))$"
)


def is_training_image_id(value: object) -> bool:
    """Return whether *value* is an exact canonical opaque image id."""
    return isinstance(value, str) and _IMAGE_ID_RE.fullmatch(value) is not None


def require_training_image_id(value: object) -> str:
    """Return a canonical id or raise without normalizing/remapping it."""
    if not is_training_image_id(value):
        raise ValueError(
            "image_id 必须是 wk2..wk8 的 01..30 或 multi-01/multi-02"
        )
    return value


def image_id_for_slot(week_no: int, slot_index: int) -> str:
    """Map the frozen source slot 1..32 to its one canonical identifier."""
    if (
        not isinstance(week_no, int)
        or isinstance(week_no, bool)
        or week_no not in TRAINING_WEEK_NUMBERS
    ):
        raise ValueError("week_no 必须是 2..8 的整数")
    if (
        not isinstance(slot_index, int)
        or isinstance(slot_index, bool)
        or slot_index < 1
        or slot_index > 32
    ):
        raise ValueError("slot_index 必须是 1..32 的整数")
    if slot_index <= 30:
        return f"wk{week_no}-{slot_index:02d}"
    return f"wk{week_no}-multi-{slot_index - 30:02d}"


def expected_training_image_ids() -> frozenset[str]:
    """Return the complete v1 source-inventory id set (7 weeks x 32 slots)."""
    return frozenset(
        image_id_for_slot(week_no, slot_index)
        for week_no in TRAINING_WEEK_NUMBERS
        for slot_index in range(1, 33)
    )
