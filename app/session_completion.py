"""场次终态判定。

前端只能请求完成，不能自行宣布完成。这里把冻结计划与服务端事实逐项对齐，
在逐尝试 AI 事件账本落地前，正式训练暂以已锁定研究真值作为最严格的完成证据。
后续 AI operational result 会作为另一条独立证据加入，而不会覆盖研究真值。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable, Iterable, Protocol

from .runtime import SessionPlan


class ItemRow(Protocol):
    id: int | None
    item_id: str
    task_type: object


class TurnRow(Protocol):
    item_event_id: int
    turn_seq: int
    response_role: str | None
    score_locked: bool
    confirmed_response_text: str | None
    raw_audio_id: str | None
    source_attempt_id: int | None


class AudioRow(Protocol):
    raw_audio_id: str
    session_id: str | None
    turn_key: str | None
    withdrawn: bool
    withdrawal_status: str | None
    is_simulation: bool
    data_classification: str


class AttemptRow(Protocol):
    id: int | None
    session_id: str
    item_id: str
    turn_seq: int
    response_role: str
    raw_audio_id: str
    processing_status: str


@dataclass(frozen=True)
class CompletionIssue:
    code: str
    detail: str
    item_id: str | None = None
    turn_seq: int | None = None
    response_role: str | None = None


@dataclass(frozen=True)
class CompletionAssessment:
    expected_turns: int
    matched_turns: int
    locked_turns: int
    issues: tuple[CompletionIssue, ...]
    audio_evidenced_turns: int = 0

    @property
    def ready(self) -> bool:
        return self.expected_turns > 0 and self.locked_turns == self.expected_turns and not self.issues

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "expected_turns": self.expected_turns,
            "matched_turns": self.matched_turns,
            "locked_turns": self.locked_turns,
            "audio_evidenced_turns": self.audio_evidenced_turns,
            "issues": [asdict(issue) for issue in self.issues],
        }


@dataclass(frozen=True)
class InterventionCompletionAssessment:
    """床旁干预终止门禁，不把事后人工研究真值混进运行流程。"""

    expected_turns: int
    matched_turns: int
    completed_attempt_turns: int
    audio_evidenced_turns: int
    issues: tuple[CompletionIssue, ...]

    @property
    def ready(self) -> bool:
        return (
            self.expected_turns > 0
            and self.matched_turns == self.expected_turns
            and self.completed_attempt_turns == self.expected_turns
            and self.audio_evidenced_turns == self.expected_turns
            and not self.issues
        )

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "expected_turns": self.expected_turns,
            "matched_turns": self.matched_turns,
            "completed_attempt_turns": self.completed_attempt_turns,
            "audio_evidenced_turns": self.audio_evidenced_turns,
            "issues": [asdict(issue) for issue in self.issues],
        }


def _text(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)


def assess_locked_research_truth(
    plan: SessionPlan,
    item_rows: Iterable[ItemRow],
    turn_rows: Iterable[TurnRow],
) -> CompletionAssessment:
    """核对计划内每个环节是否有且仅有一条已锁定研究真值。

    该函数故意 fail-closed：重复题、重复环节、计划外数据、题型错配、无确认文本
    都会阻止 ``completed``。第 1 周空评分计划也不会被当作天然完成；关系建立需要
    独立的脚本/录音完成口径后才能接入终态 API。
    """
    items = list(item_rows)
    turns = list(turn_rows)
    expected_turns = plan.total_turns()
    issues: list[CompletionIssue] = []
    matched_turns = 0
    locked_turns = 0

    if expected_turns == 0:
        issues.append(CompletionIssue(
            code="unsupported_empty_plan",
            detail="该场次没有评分计划，须使用对应流程的独立完成口径",
        ))

    expected_item_ids = {item.item_id for item in plan.items}
    for row in items:
        if row.item_id not in expected_item_ids:
            issues.append(CompletionIssue(
                code="unexpected_item",
                detail="发现冻结计划之外的题目事件",
                item_id=row.item_id,
            ))

    items_by_id: dict[str, list[ItemRow]] = {}
    for row in items:
        items_by_id.setdefault(row.item_id, []).append(row)

    for plan_item in plan.items:
        candidates = items_by_id.get(plan_item.item_id, [])
        if not candidates:
            for plan_turn in plan_item.turns:
                issues.append(CompletionIssue(
                    code="missing_turn",
                    detail="计划环节尚未建立服务端记录",
                    item_id=plan_item.item_id,
                    turn_seq=plan_turn.turn_seq,
                    response_role=plan_turn.response_role,
                ))
            continue
        if len(candidates) != 1:
            issues.append(CompletionIssue(
                code="duplicate_item",
                detail=f"同一计划题存在 {len(candidates)} 条题目事件，无法确定权威记录",
                item_id=plan_item.item_id,
            ))
            continue

        item_row = candidates[0]
        if _text(item_row.task_type) != plan_item.task_type:
            issues.append(CompletionIssue(
                code="task_type_mismatch",
                detail="题目事件类型与冻结计划不一致",
                item_id=plan_item.item_id,
            ))

        if item_row.id is None:
            issues.append(CompletionIssue(
                code="missing_item_identity",
                detail="题目事件尚未获得数据库主键",
                item_id=plan_item.item_id,
            ))
            continue

        item_turns = [row for row in turns if row.item_event_id == item_row.id]
        expected_keys = {(row.turn_seq, row.response_role) for row in plan_item.turns}
        for row in item_turns:
            key = (row.turn_seq, row.response_role or "")
            if key not in expected_keys:
                issues.append(CompletionIssue(
                    code="unexpected_turn",
                    detail="发现冻结计划之外的环节记录",
                    item_id=plan_item.item_id,
                    turn_seq=row.turn_seq,
                    response_role=row.response_role,
                ))

        for plan_turn in plan_item.turns:
            matches = [row for row in item_turns
                       if row.turn_seq == plan_turn.turn_seq
                       and (row.response_role or "") == plan_turn.response_role]
            if not matches:
                issues.append(CompletionIssue(
                    code="missing_turn",
                    detail="计划环节尚未建立服务端记录",
                    item_id=plan_item.item_id,
                    turn_seq=plan_turn.turn_seq,
                    response_role=plan_turn.response_role,
                ))
                continue
            if len(matches) != 1:
                issues.append(CompletionIssue(
                    code="duplicate_turn",
                    detail=f"同一计划环节存在 {len(matches)} 条记录，无法确定权威记录",
                    item_id=plan_item.item_id,
                    turn_seq=plan_turn.turn_seq,
                    response_role=plan_turn.response_role,
                ))
                continue

            matched_turns += 1
            turn = matches[0]
            if not turn.score_locked:
                issues.append(CompletionIssue(
                    code="unlocked_turn",
                    detail="环节尚无锁定研究真值",
                    item_id=plan_item.item_id,
                    turn_seq=plan_turn.turn_seq,
                    response_role=plan_turn.response_role,
                ))
                continue
            if not (turn.confirmed_response_text or "").strip():
                issues.append(CompletionIssue(
                    code="locked_without_confirmation",
                    detail="环节已锁定但缺少人工确认文本，数据不一致",
                    item_id=plan_item.item_id,
                    turn_seq=plan_turn.turn_seq,
                    response_role=plan_turn.response_role,
                ))
                continue
            locked_turns += 1

    return CompletionAssessment(
        expected_turns=expected_turns,
        matched_turns=matched_turns,
        locked_turns=locked_turns,
        issues=tuple(issues),
    )


def assess_completion_with_audio(
    plan: SessionPlan,
    item_rows: Iterable[ItemRow],
    turn_rows: Iterable[TurnRow],
    audio_rows: Iterable[AudioRow],
    attempt_rows: Iterable[AttemptRow],
    *,
    session_id: str,
    is_simulation: bool,
    data_classification: str,
    blob_exists: Callable[[str], bool],
) -> CompletionAssessment:
    """在锁定研究真值之上，核对每个计划环节的原始录音证据。

    录音豁免将来必须是独立结构化字段；当前 schema 没有它，所以缺音频一律
    fail-closed。AI 技术失败的 attempt 不能冒充有效录音证据。
    """
    items = list(item_rows)
    turns = list(turn_rows)
    audios = {row.raw_audio_id: row for row in audio_rows}
    attempts_by_id = {
        row.id: row for row in attempt_rows if row.id is not None
    }
    base = assess_locked_research_truth(plan, items, turns)
    issues = list(base.issues)
    evidenced = 0

    items_by_id: dict[str, list[ItemRow]] = {}
    for row in items:
        items_by_id.setdefault(row.item_id, []).append(row)

    for plan_item in plan.items:
        candidates = items_by_id.get(plan_item.item_id, [])
        if len(candidates) != 1 or candidates[0].id is None:
            continue
        item_row = candidates[0]
        item_turns = [row for row in turns if row.item_event_id == item_row.id]
        for plan_turn in plan_item.turns:
            matches = [row for row in item_turns
                       if row.turn_seq == plan_turn.turn_seq
                       and (row.response_role or "") == plan_turn.response_role]
            if len(matches) != 1:
                continue
            turn = matches[0]
            raw_audio_id = (turn.raw_audio_id or "").strip()
            issue_kwargs = {
                "item_id": plan_item.item_id,
                "turn_seq": plan_turn.turn_seq,
                "response_role": plan_turn.response_role,
            }
            if not raw_audio_id:
                issues.append(CompletionIssue(
                    code="missing_audio_reference",
                    detail="环节终值未绑定 raw_audio_id，且当前无结构化录音豁免",
                    **issue_kwargs,
                ))
                continue
            if turn.source_attempt_id is None:
                issues.append(CompletionIssue(
                    code="missing_source_attempt",
                    detail="环节终值未绑定已完成的权威 AI attempt",
                    **issue_kwargs,
                ))
                continue
            attempt = attempts_by_id.get(turn.source_attempt_id)
            if attempt is None:
                issues.append(CompletionIssue(
                    code="missing_source_attempt",
                    detail="TurnEvent.source_attempt_id 没有对应 attempt",
                    **issue_kwargs,
                ))
                continue
            if attempt.processing_status != "completed":
                issues.append(CompletionIssue(
                    code=("technical_failure_attempt"
                          if attempt.processing_status == "technical_failure"
                          else "incomplete_attempt"),
                    detail="权威 source attempt 未完成，不得计为有效完成证据",
                    **issue_kwargs,
                ))
                continue
            if (attempt.session_id != session_id
                    or attempt.item_id != plan_item.item_id
                    or attempt.turn_seq != plan_turn.turn_seq
                    or attempt.response_role != plan_turn.response_role
                    or attempt.raw_audio_id != raw_audio_id):
                issues.append(CompletionIssue(
                    code="source_attempt_binding_mismatch",
                    detail="权威 source attempt 与场次/题目/环节/角色/录音引用不一致",
                    **issue_kwargs,
                ))
                continue
            audio = audios.get(raw_audio_id)
            if audio is None:
                issues.append(CompletionIssue(
                    code="missing_audio_asset",
                    detail="raw_audio_id 没有对应 AudioAsset",
                    **issue_kwargs,
                ))
                continue
            expected_turn_key = f"{plan_item.item_id}#{plan_turn.turn_seq}"
            if audio.session_id != session_id or audio.turn_key != expected_turn_key:
                issues.append(CompletionIssue(
                    code="audio_binding_mismatch",
                    detail="AudioAsset 的 session/turn_key 与计划环节不一致",
                    **issue_kwargs,
                ))
                continue
            if (audio.is_simulation != is_simulation
                    or audio.data_classification != data_classification):
                issues.append(CompletionIssue(
                    code="audio_classification_mismatch",
                    detail="AudioAsset 的真实/模拟分类与场次不一致",
                    **issue_kwargs,
                ))
                continue
            if audio.withdrawn or (audio.withdrawal_status or "").strip():
                issues.append(CompletionIssue(
                    code="audio_withdrawn",
                    detail="AudioAsset 已进入撤回/隔离流程",
                    **issue_kwargs,
                ))
                continue
            if not blob_exists(raw_audio_id):
                issues.append(CompletionIssue(
                    code="missing_audio_blob",
                    detail="AudioAsset 存在但原始录音字节缺失",
                    **issue_kwargs,
                ))
                continue
            evidenced += 1

    return CompletionAssessment(
        expected_turns=base.expected_turns,
        matched_turns=base.matched_turns,
        locked_turns=base.locked_turns,
        issues=tuple(issues),
        audio_evidenced_turns=evidenced,
    )


def assess_intervention_completion(
    plan: SessionPlan,
    item_rows: Iterable[ItemRow],
    turn_rows: Iterable[TurnRow],
    audio_rows: Iterable[AudioRow],
    attempt_rows: Iterable[AttemptRow],
    *,
    session_id: str,
    is_simulation: bool,
    data_classification: str,
    blob_exists: Callable[[str], bool],
) -> InterventionCompletionAssessment:
    """核对床旁自动化流程是否真的走完，但不要求人工确认或锁分。

    ``completed`` 是研究数据终态，仍由 :func:`assess_completion_with_audio`
    严格要求人工确认文本与锁分。本门禁只证明冻结计划的每个环节都有唯一 Turn、
    已完成的权威 Attempt 与仍可用的原始录音，因此可以结束老人端交互并转入异步复核。
    """
    items = list(item_rows)
    turns = list(turn_rows)
    audios = {row.raw_audio_id: row for row in audio_rows}
    attempts_by_id = {
        row.id: row for row in attempt_rows if row.id is not None
    }

    # 复用成熟的计划/行对齐判定，只移除明确属于“事后人工研究真值”的两类问题。
    # 题目/环节缺失、重复、越界、题型错配和空计划仍然全部 fail-closed。
    alignment = assess_locked_research_truth(plan, items, turns)
    issues = [
        issue for issue in alignment.issues
        if issue.code not in {"unlocked_turn", "locked_without_confirmation"}
    ]
    completed_attempts = 0
    evidenced = 0

    items_by_id: dict[str, list[ItemRow]] = {}
    for row in items:
        items_by_id.setdefault(row.item_id, []).append(row)

    for plan_item in plan.items:
        candidates = items_by_id.get(plan_item.item_id, [])
        if len(candidates) != 1 or candidates[0].id is None:
            continue
        item_row = candidates[0]
        item_turns = [row for row in turns if row.item_event_id == item_row.id]
        for plan_turn in plan_item.turns:
            matches = [
                row for row in item_turns
                if row.turn_seq == plan_turn.turn_seq
                and (row.response_role or "") == plan_turn.response_role
            ]
            if len(matches) != 1:
                continue
            turn = matches[0]
            issue_kwargs = {
                "item_id": plan_item.item_id,
                "turn_seq": plan_turn.turn_seq,
                "response_role": plan_turn.response_role,
            }
            raw_audio_id = (turn.raw_audio_id or "").strip()
            if not raw_audio_id:
                issues.append(CompletionIssue(
                    code="missing_audio_reference",
                    detail="环节终值未绑定 raw_audio_id，不能结束床旁干预",
                    **issue_kwargs,
                ))
                continue
            if turn.source_attempt_id is None:
                issues.append(CompletionIssue(
                    code="missing_source_attempt",
                    detail="环节终值未绑定已完成的权威 AI attempt",
                    **issue_kwargs,
                ))
                continue
            attempt = attempts_by_id.get(turn.source_attempt_id)
            if attempt is None:
                issues.append(CompletionIssue(
                    code="missing_source_attempt",
                    detail="TurnEvent.source_attempt_id 没有对应 attempt",
                    **issue_kwargs,
                ))
                continue
            if attempt.processing_status != "completed":
                issues.append(CompletionIssue(
                    code=("technical_failure_attempt"
                          if attempt.processing_status == "technical_failure"
                          else "incomplete_attempt"),
                    detail="权威 source attempt 未完成，不得宣布床旁干预结束",
                    **issue_kwargs,
                ))
                continue
            if (attempt.session_id != session_id
                    or attempt.item_id != plan_item.item_id
                    or attempt.turn_seq != plan_turn.turn_seq
                    or attempt.response_role != plan_turn.response_role
                    or attempt.raw_audio_id != raw_audio_id):
                issues.append(CompletionIssue(
                    code="source_attempt_binding_mismatch",
                    detail="权威 source attempt 与场次/题目/环节/角色/录音引用不一致",
                    **issue_kwargs,
                ))
                continue
            completed_attempts += 1

            audio = audios.get(raw_audio_id)
            if audio is None:
                issues.append(CompletionIssue(
                    code="missing_audio_asset",
                    detail="raw_audio_id 没有对应 AudioAsset",
                    **issue_kwargs,
                ))
                continue
            expected_turn_key = f"{plan_item.item_id}#{plan_turn.turn_seq}"
            if audio.session_id != session_id or audio.turn_key != expected_turn_key:
                issues.append(CompletionIssue(
                    code="audio_binding_mismatch",
                    detail="AudioAsset 的 session/turn_key 与计划环节不一致",
                    **issue_kwargs,
                ))
                continue
            if (audio.is_simulation != is_simulation
                    or audio.data_classification != data_classification):
                issues.append(CompletionIssue(
                    code="audio_classification_mismatch",
                    detail="AudioAsset 的真实/模拟分类与场次不一致",
                    **issue_kwargs,
                ))
                continue
            if audio.withdrawn or (audio.withdrawal_status or "").strip():
                issues.append(CompletionIssue(
                    code="audio_withdrawn",
                    detail="AudioAsset 已进入撤回/隔离流程",
                    **issue_kwargs,
                ))
                continue
            if not blob_exists(raw_audio_id):
                issues.append(CompletionIssue(
                    code="missing_audio_blob",
                    detail="AudioAsset 存在但原始录音字节缺失",
                    **issue_kwargs,
                ))
                continue
            evidenced += 1

    return InterventionCompletionAssessment(
        expected_turns=alignment.expected_turns,
        matched_turns=alignment.matched_turns,
        completed_attempt_turns=completed_attempts,
        audio_evidenced_turns=evidenced,
        issues=tuple(issues),
    )
