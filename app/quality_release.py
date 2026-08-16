"""研究分区的冻结发布纪元：一次冻结、逐字节复读。

`ai_quality_service.build_ai_quality_dashboard` 里那段整体抑制的注释自己写明了
缺什么——总人数门槛既挡不住稀疏单元，也挡不住重复查询做差分。这个模块解决
第二件，`app/quality_disclosure.py` 解决第一件。

**读路径不计算任何东西。** 一次读 = 取纪元行、校验 sha256、``json.loads``
返回。不碰 Session、不碰 Patient、不碰任何证据表，连撤回闸都不查。同一纪元
两次读之间的差恒为零——不是"大概率相等"，是同一串字节，连 ``generated_at``
都取 ``frozen_at``。撤回只作为**下一次**切纪元的输入。

读时查撤回闸看起来更"及时"，实际上是把撤回做成了一个可以 1 Hz 轮询的单人
探针：盯着某人撤回前后聚合变没变，就知道他是不是撤了。

**冻的是场次集合，不是受试者集合。** 只冻人的话，训练师给已在册的人追加新
场次就能让聚合动，纪元内的差分立刻复活。

**队列是 as_of 的函数，不是人选出来的。** 没有 --include/--exclude，否则
leave-one-out（切两次、第二次少一个人）在任何路线上都成立。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import secrets
from typing import Any, Sequence

from sqlmodel import Session as DBSession, select

from . import ai_quality_service, audio_store, export_security, quality_disclosure
from .models import (
    QualityDisclosureRecord,
    QualityReleaseEpoch,
    QualityReleaseEpochSession,
    Session as TrainSession,
    SessionRuntimeState,
)
from .quality_disclosure import Cell, PublishedLeaf, band, plan_suppression, rate


COHORT_RULE_VERSION = "quality-release-cohort.v1"
REGISTRY_VERSION = "quality-release-registry.v1"
RELEASE_SCHEMA_VERSION = "ai-quality-release.v1"

RELEASE_MODE_ENV = "AI_QUALITY_RESEARCH_RELEASE_MODE"
RELEASE_MODE_REQUIRED = "frozen_epoch"
MIN_CELL_SUBJECTS_ENV = "AI_QUALITY_RESEARCH_MIN_CELL_SUBJECTS"
BAND_WIDTH_ENV = "AI_QUALITY_RESEARCH_BAND_WIDTH"
RATE_DECIMALS_ENV = "AI_QUALITY_RESEARCH_RATE_DECIMALS"
ENTRY_QUARANTINE_DAYS_ENV = "AI_QUALITY_RESEARCH_ENTRY_QUARANTINE_DAYS"
READER_ROLES_ENV = "AI_QUALITY_RESEARCH_RELEASE_READER_ROLES"

#: 上限类配置配错方向是 fail-open（"允许更多纪元"），所以给保守端回退值而不是
#: 整体抑制——一次纯运维疏忽不该把看板打死。阈值类配置相反，未配即拒。
#: 这两类不要被下一个人统一成一种写法。
DEFAULT_ENTRY_QUARANTINE_DAYS = 14

#: 角色白名单**不给默认值**：谁能看研究分区是治理决定，不是工程默认。
#: 未配置 = 谁都看不到。这一条要 Eric 与 PI 明确签字之后才配上去。
ALLOWED_READER_ROLES = frozenset({"researcher", "data_steward", "admin"})


class ReleaseRefused(RuntimeError):
    """稳定拒绝码，不含任何数值、姓名或路径。"""

    def __init__(self, code: str, detail: str):
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ReleaseThresholds:
    min_subjects: int
    min_cell_subjects: int
    band_width: int
    rate_decimals: int
    entry_quarantine_days: int


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _bounded_int(name: str, *, low: int, high: int,
                 fallback: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        if fallback is not None:
            return fallback
        raise ReleaseRefused(
            "release_threshold_unconfigured", f"{name} 未配置")
    if raw != raw.strip() or not raw.isdigit():
        raise ReleaseRefused("release_threshold_invalid", f"{name} 不是整数")
    value = int(raw)
    if value < low or value > high:
        raise ReleaseRefused("release_threshold_invalid", f"{name} 超出范围")
    return value


def load_thresholds() -> ReleaseThresholds:
    """切纪元时读一次，抄进纪元行。**读路径永不读环境变量。**

    切完之后有人改 env，旧纪元里"已满足 k=5"的声明就变成假话。
    """
    threshold = ai_quality_service._parse_threshold()
    if threshold.status != "configured" or threshold.minimum is None:
        raise ReleaseRefused(
            "release_threshold_unconfigured"
            if threshold.status == "unconfigured"
            else "release_threshold_invalid",
            f"{ai_quality_service.RESEARCH_MIN_SUBJECTS_ENV} 未配置或非法")
    return ReleaseThresholds(
        min_subjects=threshold.minimum,
        min_cell_subjects=_bounded_int(MIN_CELL_SUBJECTS_ENV, low=2, high=100),
        band_width=_bounded_int(BAND_WIDTH_ENV, low=5, high=100),
        rate_decimals=_bounded_int(RATE_DECIMALS_ENV, low=1, high=3),
        entry_quarantine_days=_bounded_int(
            ENTRY_QUARANTINE_DAYS_ENV, low=0, high=365,
            fallback=DEFAULT_ENTRY_QUARANTINE_DAYS),
    )


def authorized_reader_roles() -> frozenset[str]:
    """谁能读研究分区。未配置返回空集——谁都读不到，这是设计不是故障。"""
    raw = (os.environ.get(READER_ROLES_ENV) or "").strip()
    if not raw:
        return frozenset()
    roles = {part.strip() for part in raw.split(",") if part.strip()}
    if not roles or not roles <= ALLOWED_READER_ROLES:
        # 出现闭集之外的角色名整条判非法，不做"过滤掉不认识的"——那会让一个
        # 拼写错误静默地把范围改小或改大。
        return frozenset()
    return frozenset(roles)


# ---------------------------------------------------------------------------
# 队列
# ---------------------------------------------------------------------------

def derive_cohort(
    s: DBSession, *, as_of: datetime, quarantine_days: int,
) -> list[TrainSession]:
    """队列 = as_of 时刻全部研究口径、已终态结束满隔离期的场次。

    没有参数能让调用者手工增删成员。隔离期的作用是让"正常撤回窗内的撤回"
    永远没有已发布的字节需要收回。
    """
    cutoff = as_of - timedelta(days=quarantine_days)
    rows = list(s.exec(
        select(TrainSession).where(
            TrainSession.data_classification == "research",
        ).order_by(TrainSession.session_id)))
    admitted = [
        row for row in rows
        if _session_settled_before(s, row, cutoff)
    ]
    included, _restricted, _bad = ai_quality_service._preproject_sessions(
        s, admitted, data_classification="research")
    return included


def _session_settled_before(
        s: DBSession, session: TrainSession, cutoff: datetime) -> bool:
    """场次是否已终态结束且早于隔离期截止点。

    终态判据取自 SessionRuntimeState——它是本仓对"这场还开着没有"的权威。
    读不到状态行就当没结束：宁可漏收一场，不可把在跑的场次冻进纪元。
    """
    state = s.get(SessionRuntimeState, session.session_id)
    if state is None or state.status in {"active", "paused"}:
        return False
    updated = state.updated_at
    if updated is None:
        return False
    return updated <= cutoff


# ---------------------------------------------------------------------------
# 装载荷
# ---------------------------------------------------------------------------

def build_payload(
    s: DBSession,
    cohort: Sequence[TrainSession],
    *,
    as_of: datetime,
    thresholds: ReleaseThresholds,
) -> tuple[dict[str, Any], dict[str, int]]:
    """把队列折成一份可发布的载荷，外加每个场次的证据水位线。

    **逐场次流式**，不是一次把整个队列丢进取数预算。实测：week2 一场次 30 题
    70 环节 ⇒ 证据行地板约 240 行，而 `MAX_EVIDENCE_ROWS` 是 20000——83 场次
    就撞上限。30 人 × 8 周 = 240 场次 ≈ 57600 行，走现有请求路径必然打不出来。
    这个洞与披露控制无关，但会先炸，所以这里一场次一场次地取。
    """
    if not cohort:
        raise ReleaseRefused("release_cohort_empty", "队列里一个场次都没有")

    plans, definition_bad = ai_quality_service._plans_for_sessions(cohort)
    candidate = [
        row for row in cohort if row.session_id not in definition_bad]
    if not candidate:
        raise ReleaseRefused(
            "release_cohort_no_bound_definition", "队列里没有内容定义完整的场次")

    evidence: list[Any] = []
    watermarks: dict[str, int] = {}
    #: 复核格的贡献者要真数，不能拿队列人数顶替。逐场次走的时候顺手记下来，
    #: 因为 TurnQualityEvidence 本身有意不带受试者键，事后无从统计。
    reviewed_contributors: set[str] = set()
    source_turns = 0
    structural_invalid_records = 0
    lineage_invalid_turns = 0
    classification_bad: set[str] = set()
    structural_bad: set[str] = set()

    for row in candidate:
        session_id = row.session_id
        ai_quality_service._preflight_evidence_budget(s, [session_id])
        loaded = ai_quality_service._load_evidence_rows(s, [session_id])
        ai_quality_service._enforce_loaded_evidence_budget(loaded)
        watermarks[session_id] = _watermark(loaded)
        # 音频目录瞬时故障不能被冻成一个结论：请求路径把它降级成"物理证据
        # 不可用"，切纪元必须直接拒绝。
        blob_index = audio_store.index_blobs(
            item.raw_audio_id for item in loaded.audios)
        bad = ai_quality_service._classification_inconsistent_sessions(
            loaded, expected_simulation=False, data_classification="research")
        if bad:
            classification_bad |= bad
            continue
        grouped = ai_quality_service._group_evidence(loaded)
        invalid = ai_quality_service._structural_evidence_invalid(
            plans[session_id], grouped[session_id])
        if invalid:
            structural_bad.add(session_id)
            structural_invalid_records += invalid
            continue
        source_turns += plans[session_id].total_turns()
        projected, invalid_records, invalid_lineage = (
            ai_quality_service._project_session(
                row, plans[session_id], grouped[session_id],
                data_classification="research", blob_index=blob_index))
        evidence.extend(projected)
        if any(item.human_truth_locked is True for item in projected):
            reviewed_contributors.add(row.patient_id)
        structural_invalid_records += invalid_records
        lineage_invalid_turns += invalid_lineage

    included = [
        row for row in candidate
        if row.session_id not in (classification_bad | structural_bad)]
    if not included:
        raise ReleaseRefused(
            "release_cohort_all_excluded", "队列里的场次全部被排除")

    released = ai_quality_service._released_payload(
        data_classification="research",
        visibility_scope="frozen_release_cohort",
        generated_at=as_of,
        threshold=ai_quality_service._Threshold(
            "configured", thresholds.min_subjects),
        distinct_patients=ai_quality_service._distinct_patients(included),
        visible_sessions=len(cohort),
        included_sessions=len(included),
        source_turns=source_turns,
        evidence=evidence,
        restricted_sessions=0,
        classification_bad_sessions=len(classification_bad),
        protocol_binding_invalid_sessions=len(definition_bad),
        structural_invalid_evidence_records=structural_invalid_records,
        lineage_invalid_turns=lineage_invalid_turns,
    )
    payload = _apply_disclosure_control(
        released,
        distinct_subjects=ai_quality_service._distinct_patients(included),
        reviewed_subjects=len(reviewed_contributors),
        included_sessions=len(included),
        thresholds=thresholds,
    )
    return payload, {
        row.session_id: watermarks[row.session_id] for row in included}


def _watermark(loaded: Any) -> int:
    """一个场次冻结时的证据行数。approve 阶段拿它复核 DB 中间没动过。"""
    total = 0
    for name in ("items", "turns", "attempts", "audios", "capture_receipts",
                 "interactions", "confirmations", "pauses", "controls"):
        rows = getattr(loaded, name, None)
        if rows is not None:
            total += len(rows)
    return total


def _apply_disclosure_control(
    released: dict[str, Any], *, distinct_subjects: int,
    reviewed_subjects: int, included_sessions: int,
    thresholds: ReleaseThresholds,
) -> dict[str, Any]:
    """把一行 v2 聚合收窄成 v1 发布面。**相对今天全是收紧。**

    计数全部改档或改率，四个 privacy 字段一个不动，reason_counts 整块 null
    而不是部分 null——部分 null 的位图本身就说出了哪几项非零。
    """
    row = dict(released["rows"][0])
    operational = dict(row.get("operational") or {})
    truth = dict(row.get("research_truth") or {})

    # 两个格由不同人群支撑：队列格是全部人，复核格只有真被锁过分的人。
    # 复核格常常小得多——它是这份载荷里最先触发抑制的那一格。
    #
    # 这里**不声明 linked**：只有两个格时互补抑制已经必然把另一个也拖下水，
    # 链接一条都不会改变结果，写上去就是一条永远不被执行的声明。往
    # `_dimensions()` 里加维度、格数变多的那天要回来补——那时互补规则只会挑
    # "剩下里最小的那个"，不保证挑中同一批人支撑的那个。
    cells = [
        Cell("cohort", distinct_subjects),
        Cell("reviewed", reviewed_subjects),
    ]
    suppressed = plan_suppression(cells, minimum=thresholds.min_cell_subjects)

    decimals = thresholds.rate_decimals
    width = thresholds.band_width
    coverage = dict(row.get("coverage") or {})
    # 提示升级率的分母有意用 level 0..2 之和而不是 prompt_level_known_attempts：
    # 后者把 level 3 也计进已知，而 level 3 的计数被服务层显式置成 None
    # （床旁没有终态告知答案的留存回执，报 0 是假陈述）。用它当分母，减法就能
    # 把 level 3 的真值解出来。
    escalation_denominator = _sum_or_none(
        operational.get("prompt_level_0_count"),
        operational.get("prompt_level_1_count"),
        operational.get("prompt_level_2_count"))
    # 每个率只由支撑它的那一格决定，不做全有全无——那样连接闭包就成了摆设，
    # 而闭包正是"复核格被抑制、队列侧也得跟着灭"这件事的唯一实现。
    def _op(numerator: Any, denominator: Any) -> float | None:
        return _rate_or_none(numerator, denominator, decimals,
                             blocked="cohort" in suppressed)

    def _truth(numerator: Any, denominator: Any) -> float | None:
        return _rate_or_none(numerator, denominator, decimals,
                             blocked="reviewed" in suppressed)

    published_operational = {
        "asr_manual_correction_rate": _op(
            operational.get("asr_corrected_turns"),
            operational.get("asr_reviewed_turns")),
        "prompt_escalation_rate": _op(
            _sum_or_none(operational.get("prompt_level_1_count"),
                         operational.get("prompt_level_2_count")),
            escalation_denominator),
        "pause_rate": _op(
            operational.get("technical_pause_count"),
            operational.get("eligible_turns")),
        "takeover_rate": _op(
            operational.get("researcher_takeover_count"),
            operational.get("eligible_turns")),
        "latency_p50_band": band(
            operational.get("latency_p50_ms"), width=width),
        "latency_p95_band": band(
            operational.get("latency_p95_ms"), width=width),
    }
    reviewed = truth.get("reviewed_decisions")
    published_truth = {
        # 混淆矩阵的四个原始格永远 null：一个格子小到 1 就直接指到一次判错。
        # 一致率 = (TP + TN) / 复核量。
        "agreement_rate": _truth(
            _sum_or_none(truth.get("true_positive"), truth.get("true_negative")),
            reviewed),
        "false_positive_rate": _truth(truth.get("false_positive"), reviewed),
        "false_negative_rate": _truth(truth.get("false_negative"), reviewed),
        "reviewed_decisions_band": (
            None if "reviewed" in suppressed else band(reviewed, width=width)),
    }
    return {
        "schema_version": RELEASE_SCHEMA_VERSION,
        # 取冻结时刻，不取当前时刻：一个随每次请求变的时间戳既打破字节同一性，
        # 本身也是一条（弱）侧信道。
        "generated_at": released["generated_at"],
        "privacy": released.get("privacy"),
        "rows": [{
            "dimensions": row.get("dimensions"),
            "visibility_scope": "frozen_release_cohort",
            "suppression": {
                "status": "released",
                "reason": None,
                "minimum_distinct_subjects": thresholds.min_subjects,
                # 精确人数是一条排班表差分信道，只发档位。
                "distinct_subjects": None,
            },
            # coverage 十四项在研究行一律 null：它们全是精确计数。
            "coverage": {key: None for key in coverage},
            "diagnostics": {
                "status": row["diagnostics"]["status"],
                "reason_counts": None,
            },
            "operational": published_operational,
            "research_truth": published_truth,
            "release": {
                "cohort_size_band": band(distinct_subjects, width=width),
                "session_count_band": band(included_sessions, width=width),
                "registry_version": REGISTRY_VERSION,
                "cohort_rule_version": COHORT_RULE_VERSION,
            },
        }],
    }


def _sum_or_none(*values: Any) -> int | None:
    """任一端未知就整体未知。把 None 当 0 加进去等于凭空造一个确定的分母。"""
    total = 0
    for value in values:
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        total += value
    return total


def _rate_or_none(numerator: Any, denominator: Any, decimals: int, *,
                  blocked: bool) -> float | None:
    if blocked:
        return None
    if not isinstance(numerator, int) or not isinstance(denominator, int):
        return None
    return rate(numerator, denominator, decimals=decimals)


# ---------------------------------------------------------------------------
# 冻结与复读
# ---------------------------------------------------------------------------

def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """哈希前的唯一写法。载荷里出现 float 会让"字节同一性"变成一句谎话，
    所以率必须已经被截断成定小数位——那件事在 `_apply_disclosure_control` 做完。
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def publish_epoch(
    s: DBSession,
    *,
    payload: dict[str, Any],
    watermarks: dict[str, int],
    as_of: datetime,
    thresholds: ReleaseThresholds,
    builder: tuple[str, str],
    approver: tuple[str, str],
    idempotency_key: str,
    now: datetime | None = None,
) -> QualityReleaseEpoch:
    """把载荷冻进一行，并把上一个已发布纪元置为 superseded。"""
    config = export_security.load_deidentification_config()
    pseudonymize = export_security.pseudonymize_session
    previous = current_epoch(s)
    epoch_seq = 1 if previous is None else previous.epoch_seq + 1
    digest = payload_digest(payload)
    distinct = payload["rows"][0]["release"]

    epoch = QualityReleaseEpoch(
        epoch_id=f"qre_{secrets.token_urlsafe(18)}",
        epoch_seq=epoch_seq,
        status="published",
        as_of=as_of,
        frozen_at=now or _utc_now_naive(),
        cohort_rule_version=COHORT_RULE_VERSION,
        registry_version=REGISTRY_VERSION,
        schema_version=RELEASE_SCHEMA_VERSION,
        cohort_size_band=distinct["cohort_size_band"],
        session_count_band=distinct["session_count_band"],
        payload_json=canonical_bytes(payload).decode("utf-8"),
        payload_sha256=digest,
        min_subjects_applied=thresholds.min_subjects,
        min_cell_subjects_applied=thresholds.min_cell_subjects,
        band_width_applied=thresholds.band_width,
        rate_decimals_applied=thresholds.rate_decimals,
        diagnostics_status=payload["rows"][0]["diagnostics"]["status"],
        deidentification_key_id=config.key_id,
        builder_actor_display_id=builder[0],
        builder_actor_role=builder[1],
        approver_actor_display_id=approver[0],
        approver_actor_role=approver[1],
        idempotency_key_sha256=hashlib.sha256(
            idempotency_key.encode("utf-8")).hexdigest(),
    )
    s.add(epoch)
    s.flush()
    for session_id, watermark in sorted(watermarks.items()):
        s.add(QualityReleaseEpochSession(
            epoch_id=epoch.epoch_id,
            session_pseudonym=pseudonymize(session_id, config),
            evidence_watermark=watermark,
        ))
    if previous is not None:
        previous.status = "superseded"
        previous.superseded_at = epoch.frozen_at
        s.add(previous)
    s.flush()
    return epoch


def current_epoch(s: DBSession) -> QualityReleaseEpoch | None:
    return s.exec(
        select(QualityReleaseEpoch)
        .where(QualityReleaseEpoch.status == "published")
        .order_by(QualityReleaseEpoch.epoch_seq.desc())).first()


def serve(
    s: DBSession, *, actor_id: str, actor_role: str,
) -> dict[str, Any]:
    """复读冻结的那串字节。**这个函数不计算任何东西。**

    它不查 Patient、不查 Session、不查撤回闸——查了就等于给了攻击者一个
    1 Hz 的单人探针。当前 env 的阈值比冻结时更严则整体抑制（fail-closed），
    更松则照冻结时的报。
    """
    if actor_role not in authorized_reader_roles():
        raise ReleaseRefused(
            "research_release_reader_not_authorized",
            "本机构还没有指定谁可以读研究分区聚合")
    if (os.environ.get(RELEASE_MODE_ENV) or "").strip() != RELEASE_MODE_REQUIRED:
        raise ReleaseRefused(
            "research_release_not_frozen", "研究分区未启用冻结发布")
    epoch = current_epoch(s)
    if epoch is None:
        raise ReleaseRefused(
            "research_release_not_frozen", "还没有切过纪元")
    threshold = ai_quality_service._parse_threshold()
    if (threshold.status == "configured" and threshold.minimum is not None
            and threshold.minimum > epoch.min_subjects_applied):
        raise ReleaseRefused(
            "research_release_threshold_raised",
            "当前门槛比这一纪元冻结时更严，需重新切纪元")
    payload = json.loads(epoch.payload_json)
    if payload_digest(payload) != epoch.payload_sha256:
        raise ReleaseRefused(
            "research_release_payload_corrupt", "冻结载荷与其指纹对不上")
    s.add(QualityDisclosureRecord(
        record_id=f"qdr_{secrets.token_urlsafe(18)}",
        epoch_id=epoch.epoch_id,
        actor_id=actor_id,
        actor_role=actor_role,
        payload_sha256=epoch.payload_sha256,
    ))
    s.flush()
    return payload


def registry_problems(payload: dict[str, Any]) -> list[str]:
    """载荷里有没有未登记的叶子。**默认抑制**靠的就是这个差集。"""
    return quality_disclosure.schema_gaps(payload, PUBLICATION_SCHEMA)


def _leaf(path: tuple[str, ...], mode: str, note: str) -> PublishedLeaf:
    return PublishedLeaf(path, mode, note)  # type: ignore[arg-type]


_ROW = ("rows", "0")

#: coverage 十四项在研究行一律 null——它们全是精确计数。仍要逐个在册：
#: 将来有人往里塞值，差集才发现得了。
_COVERAGE_LEAVES = (
    "ai_attempt_status_known_turns", "ai_judgement_status_known_turns",
    "asr_review_status_known_turns", "attempts_observed",
    "audio_evidenced_turns", "binary_eligible_reviewed_decisions",
    "binary_excluded_decisions", "human_truth_locked_turns",
    "included_sessions", "latency_known_attempts",
    "processing_status_known_attempts", "prompt_level_known_attempts",
    "source_turns", "visible_sessions",
)
#: v1 只有一张 overall 表，所以除 data_classification 外全是 null。
#: 往这里加维度的那天，稀疏单元才真正开始存在——那时 MIN_CELL_SUBJECTS
#: 与互补抑制会同时开始咬合，别把它们当成今天多余的东西删掉。
_DIMENSION_LEAVES = (
    "asr_engine_version", "content_group", "device_profile",
    "judge_engine_version", "phase_type", "protocol_version", "provider_id",
    "task_type", "week_no",
)

PUBLICATION_SCHEMA: tuple[PublishedLeaf, ...] = (
    _leaf(("schema_version",), "clear", "载荷契约版本"),
    _leaf(("generated_at",), "clear", "取冻结时刻，不取当前时刻"),
    *(_leaf(_ROW + ("coverage", name), "suppressed", "精确计数，研究行恒 null")
      for name in _COVERAGE_LEAVES),
    *(_leaf(_ROW + ("dimensions", name), "suppressed", "v1 只发整体，无分组")
      for name in _DIMENSION_LEAVES),
    _leaf(_ROW + ("dimensions", "data_classification"), "clear", "分区名"),
    _leaf(_ROW + ("visibility_scope",), "clear", "恒为冻结队列，不按角色分别物化"),
    _leaf(_ROW + ("suppression", "status"), "clear", "发布或抑制"),
    _leaf(_ROW + ("suppression", "reason"), "clear", "稳定拒绝码"),
    _leaf(_ROW + ("suppression", "minimum_distinct_subjects"), "clear",
          "冻结时生效的门槛"),
    _leaf(_ROW + ("suppression", "distinct_subjects"), "suppressed",
          "精确人数是排班表差分信道"),
    _leaf(_ROW + ("diagnostics", "status"), "clear", "冻结时的完整性"),
    _leaf(_ROW + ("diagnostics", "reason_counts"), "suppressed",
          "整块抑制；部分 null 的位图本身会说出哪几项非零"),
    _leaf(_ROW + ("operational", "asr_manual_correction_rate"), "rate", "校正率"),
    _leaf(_ROW + ("operational", "prompt_escalation_rate"), "rate", "提示升级率"),
    _leaf(_ROW + ("operational", "pause_rate"), "rate", "暂停率"),
    _leaf(_ROW + ("operational", "takeover_rate"), "rate", "接管率"),
    _leaf(_ROW + ("operational", "latency_p50_band"), "band", "时延中位档"),
    _leaf(_ROW + ("operational", "latency_p95_band"), "band", "时延 p95 档"),
    _leaf(_ROW + ("research_truth", "agreement_rate"), "rate", "人机一致率"),
    _leaf(_ROW + ("research_truth", "false_positive_rate"), "rate", "假阳性率"),
    _leaf(_ROW + ("research_truth", "false_negative_rate"), "rate", "假阴性率"),
    _leaf(_ROW + ("research_truth", "reviewed_decisions_band"), "band", "复核量档"),
    _leaf(_ROW + ("release", "cohort_size_band"), "band", "队列规模档"),
    _leaf(_ROW + ("release", "session_count_band"), "band", "场次数档"),
    _leaf(_ROW + ("release", "registry_version"), "clear", "登记表版本"),
    _leaf(_ROW + ("release", "cohort_rule_version"), "clear", "队列规则版本"),
    _leaf(("privacy", "aggregation_only"), "clear", "隐私契约，四字段不动"),
    _leaf(("privacy", "contains_patient_identifiers"), "clear", "隐私契约"),
    _leaf(("privacy", "contains_audio"), "clear", "隐私契约"),
    _leaf(("privacy", "contains_transcripts"), "clear", "隐私契约"),
)
