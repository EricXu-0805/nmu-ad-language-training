"""会话编排运行时 R（薄层，归属操作端）。

职责边界（开发计划§R）：把零件接成能跑的链——按 (patient, week, event_line, item, turn)
从 M5 题库取内容、组装成操作端可逐环节推进的**会话计划**。
铁律：R **不判分、不持有内容权威、不存对错**；内容源永远是 M5，判分永远是 M4，真值永远是 M2 锁定分。
v1 由操作端人工/半自动触发推进；这里只产出「计划 + 游标」这类纯结构。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .content import ItemBank

# 双要素的固定环节序（逐环节判分/锁分依此顺序）。关系识别在最后。
DOUBLE_ROLES = ("左命名", "左作用", "右命名", "右作用", "关系识别")
# 环节角色 → 双要素评分字段（重建 de_total 时用）。
DOUBLE_ROLE_TO_FIELD = {
    "左命名": "left_name", "左作用": "left_function",
    "右命名": "right_name", "右作用": "right_function", "关系识别": "relation",
}


@dataclass(frozen=True)
class PlanTurn:
    turn_seq: int
    response_role: str                        # 单要素="命名"；双要素∈DOUBLE_ROLES；多要素=关键要素名
    scoring_key: Optional[str] = None          # left_name/relation/key_element_id…；关系建立周为 None


@dataclass(frozen=True)
class PlanItem:
    item_id: str
    task_type: str                             # 单要素 / 双要素 / 多要素 / 关系建立
    image_id: Optional[str]
    presentation_order: int
    # 呈现所需的内容引用（取自 M5，R 只搬运不改写）
    display: dict = field(default_factory=dict)
    turns: tuple[PlanTurn, ...] = ()


@dataclass(frozen=True)
class SessionPlan:
    item_bank_version_id: str
    week_no: int
    event_line: str
    items: tuple[PlanItem, ...]

    def total_turns(self) -> int:
        return sum(len(it.turns) for it in self.items)


def _single_plan_item(it: dict, order: int) -> PlanItem:
    return PlanItem(
        item_id=it.get("item_id", f"SE_{order}"),
        task_type="单要素",
        image_id=it.get("image_id"),
        presentation_order=order,
        display={"target_word": it.get("target_word"),
                 "cues": {k: (v or {}).get("text") for k, v in (it.get("cues") or {}).items()},
                 "tell_answer": it.get("tell_answer"),
                 "success_line": it.get("success_line")},
        turns=(PlanTurn(turn_seq=1, response_role="命名", scoring_key="final_correct"),),
    )


def _double_plan_item(it: dict, order: int) -> PlanItem:
    turns = tuple(PlanTurn(turn_seq=i + 1, response_role=role,
                           scoring_key=DOUBLE_ROLE_TO_FIELD[role])
                  for i, role in enumerate(DOUBLE_ROLES))
    return PlanItem(
        item_id=it.get("item_id", f"DE_{order}"),
        task_type="双要素",
        image_id=it.get("image_id"),
        presentation_order=order,
        display={"pair_title": it.get("pair_title"),
                 "left_word": it.get("left_word"), "right_word": it.get("right_word"),
                 "relation_cue": it.get("relation_cue")},
        turns=turns,
    )


def build_session_plan(bank: ItemBank, week_no: int, event_line: str,
                       max_items: Optional[int] = None) -> SessionPlan:
    """从冻结题库组装一次会话的可跑序列。

    第2–8周（正式训练）：单要素各 1 环节、双要素各 5 环节（逐环节判分/锁分）。
    第1周（关系建立）：judging 旁路——不在此展开评分题，交由 week1 脚本驱动（走通用降级）。
    """
    if week_no == 1:
        # 关系建立周不产评分题目；返回空评分计划，交互侧走 week1_script。
        return SessionPlan(bank.version_id, week_no, event_line, items=())

    items: list[PlanItem] = []
    order = 0
    for raw in bank.single_element:
        order += 1
        items.append(_single_plan_item(raw, order))
    for raw in bank.double_element:
        order += 1
        items.append(_double_plan_item(raw, order))
    if max_items is not None:
        items = items[:max_items]
    return SessionPlan(bank.version_id, week_no, event_line, tuple(items))


@dataclass
class PlanCursor:
    """会话推进游标（v1 操作端人工/半自动触发 advance）。纯位置状态，不含判分。"""
    plan: SessionPlan
    item_idx: int = 0
    turn_idx: int = 0

    def current(self) -> Optional[tuple[PlanItem, PlanTurn]]:
        if self.item_idx >= len(self.plan.items):
            return None
        item = self.plan.items[self.item_idx]
        if self.turn_idx >= len(item.turns):
            return None
        return item, item.turns[self.turn_idx]

    def advance(self) -> Optional[tuple[PlanItem, PlanTurn]]:
        """推进到下一环节：先在当前 item 内走 turn，走完跳下一 item。返回新的当前环节。"""
        item = self.plan.items[self.item_idx] if self.item_idx < len(self.plan.items) else None
        if item is not None and self.turn_idx + 1 < len(item.turns):
            self.turn_idx += 1
        else:
            self.item_idx += 1
            self.turn_idx = 0
        return self.current()

    def done(self) -> bool:
        return self.current() is None
