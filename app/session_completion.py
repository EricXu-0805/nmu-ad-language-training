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


class RapportAudioRow(Protocol):
    raw_audio_id: str
    session_id: str | None
    turn_key: str | None
    withdrawn: bool
    withdrawal_status: str | None
    is_simulation: bool
    data_classification: str
    contains_direct_identifier: bool
    byte_count: int | None
    checksum: str | None


# 冻结脚本中承载直接身份信息的节；该节录音必须整段标记 contains_direct_identifier，
# 导出红线据此整段受控。完成合同依赖它存在，缺失即判脚本结构不合法。
RAPPORT_IDENTITY_SECTION_KEY = "自我介绍"


@dataclass(frozen=True)
class RapportCompletionAssessment:
    """第 1 周关系建立独立完成口径。

    关系建立不判分、无锁分层，床旁结束与最终完成共用同一证据判定：
    服务端 rapport 位置停在道别节、录音指令已收回、全部已落库录音
    通过绑定/分类/红线/字节核验，且场次不存在任何评分行。
    录音数量无下限——录音是可选证据，同意与授权由录音授权闸门单独把守。
    """

    at_farewell: bool
    recording_idle: bool
    audio_total: int
    audio_verified: int
    issues: tuple[CompletionIssue, ...]

    @property
    def ready(self) -> bool:
        return self.at_farewell and self.recording_idle and not self.issues

    # 周 2 形状的四个 turn 制计数恒为 0：关系建立没有冻结评分环节，
    # 场次自动汇总(SessionOutcomeSummary)按空计划如实记 0，录音证据留在
    # AudioAsset 行；这让终态端点与汇总交叉核对零改动复用。
    @property
    def expected_turns(self) -> int:
        return 0

    @property
    def matched_turns(self) -> int:
        return 0

    @property
    def completed_attempt_turns(self) -> int:
        return 0

    @property
    def audio_evidenced_turns(self) -> int:
        return 0

    @property
    def locked_turns(self) -> int:
        return 0

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "protocol": "rapport",
            "at_farewell": self.at_farewell,
            "recording_idle": self.recording_idle,
            "audio_total": self.audio_total,
            "audio_verified": self.audio_verified,
            "issues": [asdict(issue) for issue in self.issues],
        }


def assess_rapport_completion(
    script: dict,
    rapport_position: dict | None,
    live_recording_state: str | None,
    audio_rows: Iterable[RapportAudioRow],
    scoring_item_count: int,
    *,
    session_id: str,
    is_simulation: bool,
    data_classification: str,
    blob_exists: Callable[[str], bool],
    attempt_count: int = 0,
    patient_rec_active: bool = False,
    recording_truth_readable: bool = True,
) -> RapportCompletionAssessment:
    """核对第 1 周关系建立场次是否满足独立完成合同。

    只钉终点位置（道别节），不钉遍历路径：脚本允许第 3 节整节跳过直入道别，
    研究者控导航，遍历痕迹由审计承载。提前结束不经此判定，走 abort 通道。
    只登记过、从未携带任何上传事实的音频行（崩溃在字节上传之前）不构成
    服务端证据，也不永久卡死完成——设备侧副本由删除回执治理承接。
    """
    issues: list[CompletionIssue] = []
    audios = [
        row for row in audio_rows
        if row.byte_count is not None or (row.checksum or "").strip()
    ]

    sections = [row for row in script.get("sections", [])
                if isinstance(row, dict) and row.get("key")]
    section_keys = {f"关系建立·{row['key']}" for row in sections}
    farewell = sections[-1] if sections else None
    script_valid = bool(
        farewell
        and farewell.get("speaker") == "机器人"
        and (farewell.get("line") or "").strip()
        and any(row.get("key") == RAPPORT_IDENTITY_SECTION_KEY for row in sections)
    )
    if not script_valid:
        issues.append(CompletionIssue(
            code="rapport_script_invalid",
            detail="冻结关系建立脚本缺少机器人道别节或自我介绍节，禁止据此完成场次",
        ))

    at_farewell = False
    recording_idle = False
    if rapport_position is None:
        issues.append(CompletionIssue(
            code="rapport_position_missing",
            detail="服务端尚无关系建立位置真值，不能宣布完成",
        ))
    elif script_valid:
        section_key = rapport_position.get("sectionKey")
        question_idx = rapport_position.get("questionIdx")
        at_farewell = (
            section_key == farewell.get("key") and question_idx in (0, None)
        )
        if not at_farewell:
            issues.append(CompletionIssue(
                code="rapport_not_at_farewell",
                detail="关系建立须停在道别节才能结束；请先播报道别话术",
            ))

    # 指令面(操作端下发)与设备面(老人端麦克风真值回报)都必须收口。
    # 收麦证据只存在于 live 槽(耐久快照恒为 idle):槽不属于本场次时真值不可读,
    # 不可读按未收口处理——须先把本场次接回 live 再结束,不得凭缺席宣布安全。
    recording_idle = (
        recording_truth_readable
        and live_recording_state not in {"armed", "recording"}
        and not patient_rec_active
    )
    if not recording_idle:
        issues.append(CompletionIssue(
            code="rapport_recording_active",
            detail=(
                "受试者端录音指令或麦克风收口证据不可读或未收回；"
                "请在当前操作端接管本场次并确认收麦后再结束"
            ),
        ))

    if scoring_item_count > 0 or attempt_count > 0:
        issues.append(CompletionIssue(
            code="rapport_unexpected_scoring_rows",
            detail="第 1 周不判分，但场次存在题目/AI 处理记录；数据不一致，禁止完成",
        ))

    verified = 0
    for audio in audios:
        issue_kwargs = {"item_id": audio.turn_key or audio.raw_audio_id}
        if audio.session_id != session_id or (audio.turn_key or "") not in section_keys:
            issues.append(CompletionIssue(
                code="rapport_audio_key_invalid",
                detail="录音未绑定冻结关系建立脚本的节位置",
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
        if (audio.turn_key == f"关系建立·{RAPPORT_IDENTITY_SECTION_KEY}"
                and audio.contains_direct_identifier is not True):
            issues.append(CompletionIssue(
                code="rapport_identity_flag_missing",
                detail="自我介绍节录音必须整段标记含直接标识，否则导出红线失效",
                **issue_kwargs,
            ))
            continue
        if not blob_exists(audio.raw_audio_id):
            issues.append(CompletionIssue(
                code="missing_audio_blob",
                detail="AudioAsset 存在但原始录音字节或采集回执未通过核验",
                **issue_kwargs,
            ))
            continue
        verified += 1

    return RapportCompletionAssessment(
        at_farewell=at_farewell,
        recording_idle=recording_idle,
        audio_total=len(audios),
        audio_verified=verified,
        issues=tuple(issues),
    )


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
