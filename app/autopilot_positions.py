"""Pure, fail-closed traversal of version-bound autopilot protocol positions.

The server must never infer a clinical interaction merely because a turn exists in
the session plan.  A position is executable only when the frozen item-bank row has
the complete operational material consumed by the current state machine.  Missing
material is returned as an explicit gap; callers must pause at that boundary rather
than skip the turn or manufacture generic prompts/feedback.

For the current v1 content this deliberately admits the complete single-element
naming positions.  Double/multi-element positions remain visible in the cursor but
are not executable until the research team freezes both a versioned operational
rubric and the corresponding automatic interaction protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from . import content, runtime


GapCode = Literal[
    "source_field_unavailable",
    "operational_rubric_unavailable",
    "operational_protocol_unavailable",
    "interaction_package_unavailable",
]


@dataclass(frozen=True)
class ProtocolPosition:
    """One canonical turn in a server-built, version-bound session plan."""

    item_index: int
    item_id: str
    task_type: str
    image_id: str | None
    turn_seq: int
    response_role: str

    @property
    def position_key(self) -> str:
        return f"{self.item_id}#{self.turn_seq}"


@dataclass(frozen=True)
class PositionGap:
    """A controlled content gap, never a fallback instruction."""

    position: ProtocolPosition
    code: GapCode
    detail: str


@dataclass(frozen=True)
class PositionDecision:
    """The next sequential plan result: runnable, blocked, or complete."""

    position: ProtocolPosition | None = None
    gap: PositionGap | None = None
    completed: bool = False

    def __post_init__(self) -> None:
        populated = sum((self.position is not None, self.gap is not None, self.completed))
        if populated != 1:
            raise ValueError("position decision must have exactly one outcome")


def plan_positions(plan: runtime.SessionPlan) -> tuple[ProtocolPosition, ...]:
    """Flatten the frozen plan without changing its declared item/turn order."""
    return tuple(
        ProtocolPosition(
            item_index=item_index,
            item_id=item.item_id,
            task_type=item.task_type,
            image_id=item.image_id,
            turn_seq=turn.turn_seq,
            response_role=turn.response_role,
        )
        for item_index, item in enumerate(plan.items)
        for turn in item.turns
    )


def build_positions(
    bank: content.ItemBank,
    *,
    week_no: int,
    event_line: str,
) -> tuple[ProtocolPosition, ...]:
    """Build the only legal cursor order from the version-bound item bank."""
    return plan_positions(runtime.build_session_plan(bank, week_no, event_line))


def find_position(
    positions: tuple[ProtocolPosition, ...],
    *,
    item_id: str,
    turn_seq: int,
) -> ProtocolPosition:
    matches = tuple(
        position for position in positions
        if position.item_id == item_id and position.turn_seq == turn_seq
    )
    if len(matches) != 1:
        raise ValueError(f"{item_id}#{turn_seq} is not one unique frozen plan position")
    return matches[0]


def _bank_item(bank: content.ItemBank, position: ProtocolPosition) -> dict | None:
    rows = {
        "单要素": bank.single_element,
        "双要素": bank.double_element,
        "多要素": bank.multi_element,
    }.get(position.task_type, [])
    matches = [row for row in rows if row.get("item_id") == position.item_id]
    return matches[0] if len(matches) == 1 else None


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def interaction_turn(
    interaction_package: dict | None,
    position: ProtocolPosition,
) -> dict | None:
    """The one package turn frozen for this exact plan position, or None."""
    if not isinstance(interaction_package, dict):
        return None
    for item in interaction_package.get("items") or []:
        if not isinstance(item, dict) or item.get("item_id") != position.item_id:
            continue
        for turn in item.get("turns") or []:
            if (isinstance(turn, dict)
                    and turn.get("turn_seq") == position.turn_seq
                    and turn.get("response_role") == position.response_role):
                return turn
    return None


def _interaction_coverage_gap(
    interaction_package: dict | None,
    position: ProtocolPosition,
) -> PositionGap | None:
    """Fail closed unless a validated interaction package covers this turn.

    The package's structure/binding gates live in ``content``; this check only
    answers "does the frozen package speak for this exact position".  A missing
    or uncovering package is an independent, precisely attributable gap — never
    a silent fallback to the naming state machine or generic prompts.
    """
    if interaction_package is None:
        return PositionGap(
            position,
            "interaction_package_unavailable",
            f"{position.position_key}:{position.response_role} "
            "缺本周自动交互数据包（未装载或未通过绑定校验）",
        )
    if interaction_turn(interaction_package, position) is None:
        return PositionGap(
            position,
            "interaction_package_unavailable",
            f"{position.position_key}:{position.response_role} "
            "不在本周自动交互数据包冻结环节内",
        )
    return None


def readiness_gap(
    bank: content.ItemBank,
    position: ProtocolPosition,
    *,
    interaction_package: dict | None = None,
) -> PositionGap | None:
    """Return why a position cannot use the current automatic protocol.

    Single-element naming has an explicit target and the complete two-cue/tell
    sequence consumed by the existing state machine.  Double/multi-element turns
    additionally require the week's validated automatic interaction package
    (question/branch/after-rerecord structure); open answers also require the
    versioned operational rubric that drives their branch classification.
    """
    raw = _bank_item(bank, position)
    if raw is None:
        return PositionGap(
            position,
            "operational_rubric_unavailable",
            f"{position.position_key}:{position.response_role} 不属于题库唯一冻结位置",
        )

    if position.task_type == "单要素" and position.response_role == "命名":
        cues = raw.get("cues")
        cue1 = cues.get("1") if isinstance(cues, dict) else None
        cue2 = cues.get("2") if isinstance(cues, dict) else None
        required = {
            "target_word": _text(raw.get("target_word")),
            "image_id": _text(raw.get("image_id")),
            "initial_prompt": _text(raw.get("initial_prompt")),
            "success_line": _text(raw.get("success_line")),
            "cue1.text": _text(cue1.get("text") if isinstance(cue1, dict) else None),
            "cue1.cue_type": _text(
                cue1.get("cue_type") if isinstance(cue1, dict) else None),
            "cue2.text": _text(cue2.get("text") if isinstance(cue2, dict) else None),
            "cue2.cue_type": _text(
                cue2.get("cue_type") if isinstance(cue2, dict) else None),
            "tell_answer": _text(raw.get("tell_answer")),
        }
        cue1_variants = (
            cue1.get("variants") if isinstance(cue1, dict) else None
        )
        for branch in ("unknown", "close", "silence"):
            variant = (
                cue1_variants.get(branch)
                if isinstance(cue1_variants, dict) else None
            )
            required[f"cue1.variants.{branch}.text"] = _text(
                variant.get("text") if isinstance(variant, dict) else None
            )
            source_index = (
                variant.get("source_paragraph_index")
                if isinstance(variant, dict) else None
            )
            required[f"cue1.variants.{branch}.source_paragraph_index"] = (
                str(source_index)
                if isinstance(source_index, int)
                and not isinstance(source_index, bool)
                and source_index >= 0
                else None
            )
        missing = tuple(key for key, value in required.items() if value is None)
        if not missing:
            return None
        return PositionGap(
            position,
            "source_field_unavailable",
            f"{position.position_key}:{position.response_role} 缺冻结运行字段: "
            + ",".join(missing),
        )

    if position.task_type == "双要素" and position.response_role in {
        "左命名", "右命名",
    }:
        target_key = "left_word" if position.response_role == "左命名" else "right_word"
        required = {
            target_key: _text(raw.get(target_key)),
            "image_id": _text(raw.get("image_id")),
        }
        missing = tuple(key for key, value in required.items() if value is None)
        if missing:
            return PositionGap(
                position,
                "source_field_unavailable",
                f"{position.position_key}:{position.response_role} 缺冻结命名目标字段: "
                + ",".join(missing),
            )
        # The target word is a closed operational rubric; the executable
        # question/one-shot-correction/silent-advance contract comes only from
        # the week's frozen interaction package.
        return _interaction_coverage_gap(interaction_package, position)

    rubric = content.operational_rubric_for(
        bank, position.item_id, position.response_role)
    if rubric is None:
        return PositionGap(
            position,
            "operational_rubric_unavailable",
            f"{position.position_key}:{position.response_role} "
            "缺版本化 operational rubric",
        )
    return _interaction_coverage_gap(interaction_package, position)


def decision_for_position(
    bank: content.ItemBank,
    position: ProtocolPosition,
    *,
    interaction_package: dict | None = None,
) -> PositionDecision:
    gap = readiness_gap(bank, position, interaction_package=interaction_package)
    return PositionDecision(gap=gap) if gap else PositionDecision(position=position)


def first_position_decision(
    bank: content.ItemBank,
    *,
    week_no: int,
    event_line: str,
    interaction_package: dict | None = None,
) -> PositionDecision:
    positions = build_positions(bank, week_no=week_no, event_line=event_line)
    if not positions:
        return PositionDecision(completed=True)
    return decision_for_position(
        bank, positions[0], interaction_package=interaction_package)


def next_position_decision(
    bank: content.ItemBank,
    *,
    week_no: int,
    event_line: str,
    current_item_id: str,
    current_turn_seq: int,
    interaction_package: dict | None = None,
) -> PositionDecision:
    """Advance exactly one turn; never jump over an unsupported position."""
    positions = build_positions(bank, week_no=week_no, event_line=event_line)
    current = find_position(
        positions, item_id=current_item_id, turn_seq=current_turn_seq)
    index = positions.index(current)
    if index + 1 >= len(positions):
        return PositionDecision(completed=True)
    return decision_for_position(
        bank, positions[index + 1], interaction_package=interaction_package)
