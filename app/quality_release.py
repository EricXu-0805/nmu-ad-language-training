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
import math
import os
import secrets
from typing import Any, Mapping, Sequence

from sqlmodel import Session as DBSession, select

from . import (
    ai_quality_service,
    audio_store,
    export_security,
    quality_disclosure,
    research_dataset,
)
from .models import (
    AudioAssetRow,
    QualityDisclosureRecord,
    QualityReleaseEpoch,
    QualityReleaseEpochRowSnapshot,
    QualityReleaseEpochSession,
    Session as TrainSession,
    SessionRuntimeState,
)
from .quality_disclosure import PublishedLeaf, band, rate


COHORT_RULE_VERSION = "quality-release-cohort.v1"
REGISTRY_VERSION = "quality-release-registry.v1"
RELEASE_SCHEMA_VERSION = "ai-quality-release.v1"
PROPOSAL_SCHEMA_VERSION = "quality-release-proposal.v1"
RESEARCH_SNAPSHOT_SCHEMA_VERSION = "research-row-snapshot.v1"

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
_RELEASE_TERMINAL_SESSION_STATUSES = frozenset({"completed", "aborted", "failed"})


class ReleaseRefused(RuntimeError):
    """稳定拒绝码，不含任何数值、姓名或路径。"""

    def __init__(self, code: str, detail: str):
        super().__init__(code)
        self.code = code
        self.detail = detail


class ReleaseSnapshotUnavailable(RuntimeError):
    """后端无法提供一个稳定的切纪元事务快照。"""


@dataclass(frozen=True)
class ReleaseThresholds:
    min_subjects: int
    min_cell_subjects: int
    band_width: int
    rate_decimals: int
    entry_quarantine_days: int


@dataclass(frozen=True)
class ResearchSnapshotRow:
    dataset_key: str
    row_ordinal: int
    row_json: str
    row_sha256: str


@dataclass(frozen=True)
class ResearchSnapshot:
    manifest_json: str
    snapshot_sha256: str
    rows: tuple[ResearchSnapshotRow, ...]


@dataclass(frozen=True)
class _LiveSnapshotBinding:
    """只在切纪元事务里把活投影限制到最终纳入的场次。"""

    session_ids: frozenset[str]

    def envelope(self) -> dict[str, Any]:
        # build_research_snapshot 只消费 rows；临时信封绝不持久化或对外发送。
        return {}


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


def begin_release_transaction(s: DBSession, *, writable: bool) -> None:
    """在第一次 SELECT 前建立切纪元的唯一事务快照。

    approve 需要在同一快照内写入 epoch，所以不能直接复用只读
    dashboard 的 snapshot helper。未知后端、复用的 dirty Session 均关闭。
    """
    if s.in_transaction():
        raise ReleaseSnapshotUnavailable
    try:
        dialect = s.get_bind().dialect.name
        if dialect == "postgresql":
            connection = s.connection(execution_options={
                "isolation_level": "SERIALIZABLE" if writable
                else "REPEATABLE READ",
            })
            if not writable:
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            return
        if dialect == "sqlite":
            connection = s.connection()
            connection.exec_driver_sql("BEGIN IMMEDIATE" if writable else "BEGIN")
            return
    except ReleaseSnapshotUnavailable:
        raise
    except Exception as exc:
        raise ReleaseSnapshotUnavailable from exc
    raise ReleaseSnapshotUnavailable


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
    if state is None or state.status not in _RELEASE_TERMINAL_SESSION_STATUSES:
        return False
    updated = state.updated_at
    if updated is None:
        return False
    return updated <= cutoff


# ---------------------------------------------------------------------------
# 装载荷
# ---------------------------------------------------------------------------

_METRIC_CONTRIBUTOR_KEYS = (
    "asr_manual_correction_rate",
    "prompt_escalation_rate",
    "pause_rate",
    "takeover_rate",
    "latency_p50_band",
    "latency_p95_band",
    "agreement_rate",
    "false_positive_rate",
    "false_negative_rate",
    "reviewed_decisions_band",
)


def _record_metric_contributors(
    contributors: dict[str, set[str]], *, patient_id: str,
    evidence: Sequence[Any],
) -> None:
    """按与 ``ai_quality_metrics`` 分母相同的条件记真实贡献者。"""
    for item in evidence:
        attempts = item.attempts or ()
        if (item.eligible is True and item.asr_reviewed is True
                and type(item.asr_corrected) is bool):
            contributors["asr_manual_correction_rate"].add(patient_id)
        if any(type(attempt.prompt_level) is int
               and attempt.prompt_level in {0, 1, 2}
               for attempt in attempts):
            contributors["prompt_escalation_rate"].add(patient_id)
        if item.eligible is True:
            contributors["pause_rate"].add(patient_id)
            contributors["takeover_rate"].add(patient_id)
        if any(type(attempt.latency_ms) in {int, float}
               and attempt.latency_ms >= 0
               and math.isfinite(attempt.latency_ms)
               for attempt in attempts):
            contributors["latency_p50_band"].add(patient_id)
            contributors["latency_p95_band"].add(patient_id)

        has_completed_attempt = any(
            attempt.processing_status == "completed" for attempt in attempts)
        binary_reviewed = (
            item.eligible is True
            and item.ai_attempted is True
            and item.ai_judged is True
            and has_completed_attempt
            and type(item.ai_predicted_correct) is bool
            and item.human_truth_locked is True
            and type(item.human_truth_correct) is bool
        )
        if binary_reviewed:
            for key in ("agreement_rate", "reviewed_decisions_band"):
                contributors[key].add(patient_id)
            # 假阳性率只以真实阴性 (FP + TN) 为分母；假阴性率只以
            # 真实阳性 (FN + TP) 为分母。不能拿“有任意二元复核”的受试者
            # 同时替两个指标过披露门槛。
            if item.human_truth_correct is False:
                contributors["false_positive_rate"].add(patient_id)
            if item.human_truth_correct is True:
                contributors["false_negative_rate"].add(patient_id)

def build_payload(
    s: DBSession,
    cohort: Sequence[TrainSession],
    *,
    as_of: datetime,
    thresholds: ReleaseThresholds,
) -> tuple[dict[str, Any], dict[str, int]]:
    """把队列折成一份可发布的载荷，外加每个场次的证据水位线。

    **逐场次流式**，不是一次把整个队列丢进取数预算。Week2 一场次是 30 题、
    70 环节；完整 happy-path 证据还包含 attempt、音频、capture receipt、
    interaction 与人工复核修订，不能再用早期“约 240 行/场”的估算当容量
    下界。请求路径的 `MAX_EVIDENCE_ROWS` 只是单次资源闸，切纪元必须按场
    取证并由独立 scale harness 验证整体容量。
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
    contributor_ids: dict[str, set[str]] = {
        key: set() for key in _METRIC_CONTRIBUTOR_KEYS
    }
    source_turns = 0
    structural_invalid_records = 0
    lineage_invalid_turns = 0
    classification_bad: set[str] = set()
    structural_bad: set[str] = set()

    candidate_ids = [row.session_id for row in candidate]
    raw_audio_ids = [
        value if isinstance(value, str) else value[0]
        for value in s.exec(select(AudioAssetRow.raw_audio_id).where(
            AudioAssetRow.session_id.in_(candidate_ids)))
    ]
    # `index_blobs` 本身是一次目录扫描。原来它写在逐场循环里，240 场就把
    # 同一个音频目录完整扫 240 遍；先把最终候选的 id 合并，整个提案只扫一次。
    all_blob_index = audio_store.index_blobs(raw_audio_ids)

    for row in candidate:
        session_id = row.session_id
        ai_quality_service._preflight_evidence_budget(s, [session_id])
        loaded = ai_quality_service._load_evidence_rows(s, [session_id])
        ai_quality_service._enforce_loaded_evidence_budget(loaded)
        # 音频目录瞬时故障不能被冻成一个结论：请求路径把它降级成"物理证据
        # 不可用"，切纪元必须直接拒绝。
        blob_index = {
            item.raw_audio_id: all_blob_index[item.raw_audio_id]
            for item in loaded.audios if item.raw_audio_id in all_blob_index
        }
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
        # 水位线同时是发布成员名单。只能在分类与结构完整性都通过后
        # 写入，否则看板排除的坏场次会被研究行快照错误冻结。
        watermarks[session_id] = _watermark(loaded)
        source_turns += plans[session_id].total_turns()
        projected, invalid_records, invalid_lineage = (
            ai_quality_service._project_session(
                row, plans[session_id], grouped[session_id],
                data_classification="research", blob_index=blob_index))
        evidence.extend(projected)
        _record_metric_contributors(
            contributor_ids, patient_id=row.patient_id, evidence=projected)
        structural_invalid_records += invalid_records
        lineage_invalid_turns += invalid_lineage

    included = [
        row for row in candidate
        if row.session_id not in (classification_bad | structural_bad)]
    if not included:
        raise ReleaseRefused(
            "release_cohort_all_excluded", "队列里的场次全部被排除")

    distinct_subjects = ai_quality_service._distinct_patients(included)
    if distinct_subjects < thresholds.min_subjects:
        raise ReleaseRefused(
            "release_cohort_below_threshold",
            "通过完整性复核的受试者数未达冻结发布总门槛")

    released = ai_quality_service._released_payload(
        data_classification="research",
        visibility_scope="frozen_release_cohort",
        generated_at=(as_of.replace(tzinfo=timezone.utc)
                      if as_of.tzinfo is None or as_of.utcoffset() is None
                      else as_of.astimezone(timezone.utc)),
        threshold=ai_quality_service._Threshold(
            "configured", thresholds.min_subjects),
        distinct_patients=distinct_subjects,
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
        distinct_subjects=distinct_subjects,
        metric_contributors={
            key: len(value) for key, value in contributor_ids.items()
        },
        included_sessions=len(included),
        thresholds=thresholds,
    )
    return payload, {
        row.session_id: watermarks[row.session_id] for row in included}


def _watermark(loaded: Any) -> int:
    """一个场次冻结时的证据行数。approve 阶段拿它复核 DB 中间没动过。"""
    total = 0
    for name in ("items", "turn_pairs", "attempts", "audios", "receipts",
                 "interactions", "revision_pairs", "pause_receipts",
                 "control_events"):
        rows = getattr(loaded, name, None)
        if rows is not None:
            total += len(rows)
    return total


def _apply_disclosure_control(
    released: dict[str, Any], *, distinct_subjects: int,
    included_sessions: int, thresholds: ReleaseThresholds,
    metric_contributors: dict[str, int] | None = None,
    reviewed_subjects: int | None = None,
) -> dict[str, Any]:
    """把一行 v2 聚合收窄成 v1 发布面。**相对今天全是收紧。**

    计数全部改档或改率，四个 privacy 字段一个不动，reason_counts 整块 null
    而不是部分 null——部分 null 的位图本身就说出了哪几项非零。
    """
    row = dict(released["rows"][0])
    operational = dict(row.get("operational") or {})
    truth = dict(row.get("research_truth") or {})

    if metric_contributors is None:
        # 保留纯函数单测的简写入口；生产 build_payload 永远传逐指标真值。
        reviewed = distinct_subjects if reviewed_subjects is None else reviewed_subjects
        truth_keys = {
            "agreement_rate", "false_positive_rate",
            "false_negative_rate", "reviewed_decisions_band",
        }
        metric_contributors = {
            key: reviewed if key in truth_keys else distinct_subjects
            for key in _METRIC_CONTRIBUTOR_KEYS
        }
    if set(metric_contributors) != set(_METRIC_CONTRIBUTOR_KEYS):
        raise ReleaseRefused(
            "release_metric_contributors_incomplete",
            "指标贡献者登记与发布面不一致")
    # 这些是重叠、不可相加的分母组，不能喂给用于“同一张可加表”的
    # 互补抑制算法。每项只按自己真正的贡献者门槛决定。
    suppressed = {
        key for key, count in metric_contributors.items()
        if count < thresholds.min_cell_subjects
    }

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
    def _metric_rate(
        key: str, numerator: Any, denominator: Any,
    ) -> float | None:
        return _rate_or_none(numerator, denominator, decimals,
                             blocked=key in suppressed)

    def _metric_band(key: str, value: Any) -> str | None:
        return None if key in suppressed else band(value, width=width)

    published_operational = {
        "asr_manual_correction_rate": _metric_rate(
            "asr_manual_correction_rate",
            operational.get("asr_corrected_turns"),
            operational.get("asr_reviewed_turns")),
        "prompt_escalation_rate": _metric_rate(
            "prompt_escalation_rate",
            _sum_or_none(operational.get("prompt_level_1_count"),
                         operational.get("prompt_level_2_count")),
            escalation_denominator),
        "pause_rate": _metric_rate(
            "pause_rate",
            operational.get("technical_pause_count"),
            operational.get("eligible_turns")),
        "takeover_rate": _metric_rate(
            "takeover_rate",
            operational.get("researcher_takeover_count"),
            operational.get("eligible_turns")),
        "latency_p50_band": _metric_band(
            "latency_p50_band", operational.get("latency_p50_ms")),
        "latency_p95_band": _metric_band(
            "latency_p95_band", operational.get("latency_p95_ms")),
    }
    reviewed = truth.get("reviewed_decisions")
    negative_truth = _sum_or_none(
        truth.get("false_positive"), truth.get("true_negative"))
    positive_truth = _sum_or_none(
        truth.get("false_negative"), truth.get("true_positive"))
    published_truth = {
        # 混淆矩阵的四个原始格永远 null：一个格子小到 1 就直接指到一次判错。
        # 一致率 = (TP + TN) / 复核量。
        "agreement_rate": _metric_rate(
            "agreement_rate",
            _sum_or_none(truth.get("true_positive"), truth.get("true_negative")),
            reviewed),
        "false_positive_rate": _metric_rate(
            "false_positive_rate", truth.get("false_positive"), negative_truth),
        "false_negative_rate": _metric_rate(
            "false_negative_rate", truth.get("false_negative"), positive_truth),
        "reviewed_decisions_band": _metric_band(
            "reviewed_decisions_band", reviewed),
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


def _rows_digest(row_bytes: Sequence[bytes]) -> str:
    """长度前缀让相邻两行的边界也进入摘要，避免串接歧义。"""
    digest = hashlib.sha256()
    for raw in row_bytes:
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def _snapshot_from_rows(
    rows_by_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
) -> ResearchSnapshot:
    """把已去标识、已按注册表投影的三张行表冻成唯一字节表示。"""
    if set(rows_by_dataset) != set(research_dataset.dataset_keys()):
        raise ReleaseRefused(
            "release_research_snapshot_incomplete",
            "研究行快照没有覆盖全部登记数据集")
    stored: list[ResearchSnapshotRow] = []
    manifest_datasets: dict[str, Any] = {}
    for dataset_key in research_dataset.dataset_keys():
        dataset = research_dataset.dataset_for(dataset_key)
        assert dataset is not None
        columns = list(research_dataset.published_columns(dataset))
        encoded_rows: list[bytes] = []
        projected_rows: list[dict[str, Any]] = []
        for ordinal, raw_row in enumerate(rows_by_dataset[dataset_key], start=1):
            row = dict(raw_row)
            projected = research_dataset.project(dataset, row)
            if row != projected:
                raise ReleaseRefused(
                    "release_research_snapshot_row_invalid",
                    "研究行快照与公开列注册表不一致")
            encoded = canonical_bytes(projected)
            encoded_rows.append(encoded)
            projected_rows.append(projected)
            stored.append(ResearchSnapshotRow(
                dataset_key=dataset_key,
                row_ordinal=ordinal,
                row_json=encoded.decode("utf-8"),
                row_sha256=hashlib.sha256(encoded).hexdigest(),
            ))
        try:
            export_security.assert_deidentified_sheets(
                {dataset_key: projected_rows})
        except Exception as exc:
            raise ReleaseRefused(
                "release_research_snapshot_not_deidentified",
                "研究行快照未通过去标识边界") from exc
        manifest_datasets[dataset_key] = {
            "columns": columns,
            "row_count": len(encoded_rows),
            "rows_sha256": _rows_digest(encoded_rows),
        }
    manifest = {
        "schema_version": RESEARCH_SNAPSHOT_SCHEMA_VERSION,
        "datasets": manifest_datasets,
    }
    manifest_json = canonical_bytes(manifest).decode("utf-8")
    return ResearchSnapshot(
        manifest_json=manifest_json,
        snapshot_sha256=hashlib.sha256(
            manifest_json.encode("utf-8")).hexdigest(),
        rows=tuple(stored),
    )


def build_research_snapshot(
    s: DBSession, *, session_ids: Sequence[str], config: Any,
) -> ResearchSnapshot:
    """在切纪元的同一个稳定事务里物化三张公开行表。

    这里复用当前行面投影，而不是复制一份 Patient/Session/Turn 规则。临时 binding
    只负责把活投影限制到已经通过聚合完整性门禁的最终场次集合；不会写披露账本。
    """
    from . import research_read  # 延迟导入，避免 quality_release <-> research_read 环

    requested_ids = tuple(session_ids)
    frozen_ids = frozenset(requested_ids)
    if not frozen_ids or len(frozen_ids) != len(requested_ids):
        raise ReleaseRefused(
            "release_research_snapshot_sessions_invalid",
            "研究行快照的场次集合为空或重复")
    binding = _LiveSnapshotBinding(session_ids=frozen_ids)
    rows_by_dataset: dict[str, list[dict[str, Any]]] = {}
    # 按名字动态解析：测试给某个 reader 打替身时必须真的换得掉。
    readers = {k: research_read.reader_for(k) for k in research_dataset.dataset_keys()}
    for dataset_key in research_dataset.dataset_keys():
        cursor: str | None = None
        seen_cursors: set[str] = set()
        collected: list[dict[str, Any]] = []
        while True:
            payload = readers[dataset_key](
                s, config=config, data_classification="research",
                cursor=cursor, limit=research_read.MAX_PAGE_SIZE,
                binding=binding,
            )
            dataset = research_dataset.dataset_for(dataset_key)
            assert dataset is not None
            if (payload.get("schema_version") != research_dataset.SCHEMA_VERSION
                    or payload.get("dataset") != dataset_key
                    or payload.get("columns") != list(
                        research_dataset.published_columns(dataset))
                    or payload.get("row_count") != len(payload.get("rows", ()))):
                raise ReleaseRefused(
                    "release_research_snapshot_projection_invalid",
                    "研究行投影返回了不一致的闭集契约")
            page_rows = payload.get("rows")
            if not isinstance(page_rows, list) or not all(
                    isinstance(row, dict) for row in page_rows):
                raise ReleaseRefused(
                    "release_research_snapshot_projection_invalid",
                    "研究行投影返回了非规范行")
            collected.extend(page_rows)
            has_more = payload.get("has_more")
            next_cursor = payload.get("next_cursor")
            if has_more is False and next_cursor is None:
                break
            if (has_more is not True or not isinstance(next_cursor, str)
                    or not next_cursor or next_cursor in seen_cursors):
                raise ReleaseRefused(
                    "release_research_snapshot_pagination_invalid",
                    "研究行投影分页没有稳定向前推进")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        rows_by_dataset[dataset_key] = collected
    if len(rows_by_dataset["sessions"]) != len(frozen_ids):
        raise ReleaseRefused(
            "release_research_snapshot_session_count_mismatch",
            "冻结行面的场次数与发布队列不一致")
    return _snapshot_from_rows(rows_by_dataset)


def _is_sha256(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdef" for char in value))


def _validated_snapshot_manifest(
    manifest_json: str, snapshot_sha256: str,
) -> dict[str, Any]:
    try:
        manifest = json.loads(manifest_json)
    except (TypeError, ValueError) as exc:
        raise ReleaseRefused(
            "research_release_snapshot_corrupt",
            "冻结研究行快照的清单无法解析") from exc
    if (not isinstance(manifest, dict)
            or set(manifest) != {"schema_version", "datasets"}
            or manifest.get("schema_version") != RESEARCH_SNAPSHOT_SCHEMA_VERSION
            or not isinstance(manifest.get("datasets"), dict)
            or set(manifest["datasets"]) != set(research_dataset.dataset_keys())):
        raise ReleaseRefused(
            "research_release_snapshot_corrupt",
            "冻结研究行快照的清单版本或数据集闭集不一致")
    canonical = canonical_bytes(manifest).decode("utf-8")
    if (canonical != manifest_json or not _is_sha256(snapshot_sha256)
            or hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            != snapshot_sha256):
        raise ReleaseRefused(
            "research_release_snapshot_corrupt",
            "冻结研究行快照的清单与摘要不一致")
    for dataset_key in research_dataset.dataset_keys():
        dataset = research_dataset.dataset_for(dataset_key)
        assert dataset is not None
        facts = manifest["datasets"][dataset_key]
        if (not isinstance(facts, dict)
                or set(facts) != {"columns", "row_count", "rows_sha256"}
                or facts.get("columns") != list(
                    research_dataset.published_columns(dataset))
                or type(facts.get("row_count")) is not int
                or facts["row_count"] < 0
                or not _is_sha256(facts.get("rows_sha256"))):
            raise ReleaseRefused(
                "research_release_snapshot_corrupt",
                "冻结研究行快照的数据集清单不一致")
    return manifest


def _validate_research_snapshot(snapshot: ResearchSnapshot) -> dict[str, Any]:
    manifest = _validated_snapshot_manifest(
        snapshot.manifest_json, snapshot.snapshot_sha256)
    rows_by_dataset: dict[str, list[ResearchSnapshotRow]] = {
        key: [] for key in research_dataset.dataset_keys()
    }
    for row in snapshot.rows:
        if row.dataset_key not in rows_by_dataset:
            raise ReleaseRefused(
                "research_release_snapshot_corrupt",
                "冻结研究行快照含未登记数据集")
        rows_by_dataset[row.dataset_key].append(row)
    for dataset_key, rows in rows_by_dataset.items():
        rows.sort(key=lambda row: row.row_ordinal)
        if [row.row_ordinal for row in rows] != list(range(1, len(rows) + 1)):
            raise ReleaseRefused(
                "research_release_snapshot_corrupt",
                "冻结研究行快照序号不连续")
        encoded_rows: list[bytes] = []
        dataset = research_dataset.dataset_for(dataset_key)
        assert dataset is not None
        for row in rows:
            try:
                decoded = json.loads(row.row_json)
            except (TypeError, ValueError) as exc:
                raise ReleaseRefused(
                    "research_release_snapshot_corrupt",
                    "冻结研究行快照含不可解析行") from exc
            if (not isinstance(decoded, dict)
                    or canonical_bytes(decoded).decode("utf-8") != row.row_json
                    or hashlib.sha256(row.row_json.encode("utf-8")).hexdigest()
                    != row.row_sha256
                    or decoded != research_dataset.project(dataset, decoded)):
                raise ReleaseRefused(
                    "research_release_snapshot_corrupt",
                    "冻结研究行快照的行内容或摘要不一致")
            encoded_rows.append(row.row_json.encode("utf-8"))
        facts = manifest["datasets"][dataset_key]
        if (facts["row_count"] != len(rows)
                or facts["rows_sha256"] != _rows_digest(encoded_rows)):
            raise ReleaseRefused(
                "research_release_snapshot_corrupt",
                "冻结研究行快照的行集合与清单不一致")
    return manifest


def _validate_snapshot_membership(
    snapshot: ResearchSnapshot,
    watermarks: Mapping[str, int],
    config: Any,
) -> None:
    """Prove that every frozen row belongs to the exact released cohort.

    Counting the ``sessions`` rows is not sufficient: a caller could provide a
    perfectly canonical snapshot for a different cohort of the same size.  The
    aggregate member ledger stores pseudonyms, so compare in that same domain
    and also close the subject/turn foreign-key projection.
    """
    expected_sessions = {
        export_security.pseudonymize_session(session_id, config)
        for session_id in watermarks
    }
    decoded: dict[str, list[dict[str, Any]]] = {
        key: [] for key in research_dataset.dataset_keys()
    }
    for row in snapshot.rows:
        value = json.loads(row.row_json)
        assert isinstance(value, dict)  # `_validate_research_snapshot` ran first.
        decoded[row.dataset_key].append(value)

    session_subject: dict[str, str] = {}
    for row in decoded["sessions"]:
        session_code = row.get("session_code")
        subject_code = row.get("subject_code")
        if (not isinstance(session_code, str) or not session_code
                or not isinstance(subject_code, str) or not subject_code
                or session_code in session_subject):
            raise ReleaseRefused(
                "release_research_snapshot_membership_mismatch",
                "冻结研究行的场次或受试者关联不一致")
        session_subject[session_code] = subject_code
    if set(session_subject) != expected_sessions:
        raise ReleaseRefused(
            "release_research_snapshot_membership_mismatch",
            "冻结研究行与发布队列不是同一批场次")

    expected_subjects = set(session_subject.values())
    actual_subjects = [row.get("subject_code") for row in decoded["subjects"]]
    if (any(not isinstance(value, str) or not value for value in actual_subjects)
            or len(set(actual_subjects)) != len(actual_subjects)
            or set(actual_subjects) != expected_subjects):
        raise ReleaseRefused(
            "release_research_snapshot_membership_mismatch",
            "冻结研究行的受试者集合与场次不一致")
    for row in decoded["turns"]:
        session_code = row.get("session_code")
        if (not isinstance(session_code, str)
                or session_code not in session_subject
                or row.get("subject_code") != session_subject[session_code]):
            raise ReleaseRefused(
                "release_research_snapshot_membership_mismatch",
                "冻结训练环节不属于发布队列")


def proposal_digest(
    payload: dict[str, Any], watermarks: dict[str, int], *,
    as_of: datetime, config: Any, thresholds: ReleaseThresholds,
    builder: tuple[str, str], research_snapshot_sha256: str,
) -> str:
    """把公开载荷与精确队列水位绑成两阶段复核指纹。

    成员不以真实 session id 进摘要；用同一去标识密钥产生的
    session 假名排序。摘要只对外发 sha256，不发成员列表。
    """
    if not _is_sha256(research_snapshot_sha256):
        raise ReleaseRefused(
            "release_research_snapshot_corrupt",
            "研究行快照摘要不是规范 sha256")
    if as_of.tzinfo is not None and as_of.utcoffset() is not None:
        normalized = as_of.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        normalized = as_of
    members = sorted(
        (export_security.pseudonymize_session(session_id, config), count)
        for session_id, count in watermarks.items()
    )
    candidate = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "as_of": normalized.isoformat() + "Z",
        "payload_sha256": payload_digest(payload),
        "research_snapshot_sha256": research_snapshot_sha256,
        "evidence_watermarks": members,
        "policy": {
            "cohort_rule_version": COHORT_RULE_VERSION,
            "registry_version": REGISTRY_VERSION,
            "release_schema_version": RELEASE_SCHEMA_VERSION,
            "deidentification_key_id": config.key_id,
            "min_subjects": thresholds.min_subjects,
            "min_cell_subjects": thresholds.min_cell_subjects,
            "band_width": thresholds.band_width,
            "rate_decimals": thresholds.rate_decimals,
            "entry_quarantine_days": thresholds.entry_quarantine_days,
        },
        "builder": {"display_id": builder[0], "role": builder[1]},
    }
    return hashlib.sha256(canonical_bytes(candidate)).hexdigest()


def publish_epoch(
    s: DBSession,
    *,
    payload: dict[str, Any],
    watermarks: dict[str, int],
    research_snapshot: ResearchSnapshot,
    proposal_sha256: str,
    as_of: datetime,
    thresholds: ReleaseThresholds,
    builder: tuple[str, str],
    approver: tuple[str, str],
    idempotency_key: str,
    now: datetime | None = None,
) -> QualityReleaseEpoch:
    """把载荷冻进一行，并把上一个已发布纪元置为 superseded。"""
    manifest = _validate_research_snapshot(research_snapshot)
    if manifest["datasets"]["sessions"]["row_count"] != len(watermarks):
        raise ReleaseRefused(
            "release_research_snapshot_session_count_mismatch",
            "冻结行面的场次数与发布队列不一致")
    config = export_security.load_deidentification_config()
    _validate_snapshot_membership(research_snapshot, watermarks, config)
    if (not _is_sha256(proposal_sha256)
            or proposal_sha256 != proposal_digest(
                payload, watermarks, as_of=as_of, config=config,
                thresholds=thresholds, builder=builder,
                research_snapshot_sha256=research_snapshot.snapshot_sha256)):
        raise ReleaseRefused(
            "release_proposal_digest_invalid",
            "写入纪元的提案摘要与本次冻结事实不一致")
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
        proposal_sha256=proposal_sha256,
        entry_quarantine_days_applied=thresholds.entry_quarantine_days,
        research_snapshot_schema_version=RESEARCH_SNAPSHOT_SCHEMA_VERSION,
        research_snapshot_manifest_json=research_snapshot.manifest_json,
        research_snapshot_sha256=research_snapshot.snapshot_sha256,
    )
    s.add(epoch)
    s.flush()
    for session_id, watermark in sorted(watermarks.items()):
        s.add(QualityReleaseEpochSession(
            epoch_id=epoch.epoch_id,
            session_pseudonym=pseudonymize(session_id, config),
            evidence_watermark=watermark,
        ))
    for row in research_snapshot.rows:
        s.add(QualityReleaseEpochRowSnapshot(
            epoch_id=epoch.epoch_id,
            dataset_key=row.dataset_key,
            row_ordinal=row.row_ordinal,
            row_json=row.row_json,
            row_sha256=row.row_sha256,
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
    _append_disclosure(s, epoch_id=epoch.epoch_id, actor_id=actor_id,
                       actor_role=actor_role,
                       payload_sha256=epoch.payload_sha256)
    return payload


def _append_disclosure(
    s: DBSession, *, epoch_id: str, actor_id: str, actor_role: str,
    payload_sha256: str,
) -> None:
    """把一次读取记进披露账本，**用自己的会话提交**。

    第一版是往调用方的会话里 ``add`` + ``flush`` 就完了。单元测试里紧跟着一句
    ``db.commit()``，于是全绿；而 HTTP 上 ``get_session`` 从头到尾不 commit，
    读接口本来也没有别的写——每一行都在请求结束时随会话一起回滚。交接文档写着
    "每一次读取都往只追加的账本里写一行"，实际上账本一行都没有。

    照 ``audit.record`` 的做法：绑同一个引擎、开自己的会话、自己提交，绝不
    commit 调用方的事务。
    """
    with DBSession(s.get_bind()) as ledger:
        ledger.add(QualityDisclosureRecord(
            record_id=f"qdr_{secrets.token_urlsafe(18)}",
            epoch_id=epoch_id,
            actor_id=actor_id,
            actor_role=actor_role,
            payload_sha256=payload_sha256,
        ))
        ledger.commit()


# ---------------------------------------------------------------------------
# 行级取数面的绑定
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResearchBinding:
    """把 ``/research/v1/*`` 的研究分区钉在某一个冻结纪元上。

    在这之前，行面与聚合面算的是两个不同的队列：聚合是冻结的、过了隔离期的那批
    场次，行面是库里当下的一切。两个后果——拿到 CSV 的人复现不出看板上任何一个
    数（连"这两份说的是不是同一批人"都无从判断）；而"两次拉取之差"里正好装着
    这期间新入组的那几个人的全部明细。

    **行面的读者是 data_steward 与 admin，他们本来就能直接读库。** 所以这层绑定
    给的是可复现、可比对、可审计，不是遏制——这一点在
    ``docs/handover/研究分区披露控制_给PI.md`` §5.2 里写着，别在别处写强了。
    """
    epoch_id: str
    epoch_seq: int
    as_of: datetime
    frozen_at: datetime
    cohort_rule_version: str
    payload_sha256: str
    session_ids: frozenset[str]
    snapshot_manifest: dict[str, Any] | None = None
    research_snapshot_sha256: str | None = None

    @property
    def frozen_session_count(self) -> int:
        if self.snapshot_manifest is not None:
            return self.snapshot_manifest["datasets"]["sessions"]["row_count"]
        return len(self.session_ids)

    def envelope(self) -> dict[str, Any]:
        """进每一页响应的发布标识。

        **不放绝对时间。** 逐行禁出绝对时间是这个接口的红线，泄漏回归对整份响应
        原文做正则，信封里放一个 ISO 时刻同样会被抓住——而且没有必要：epoch_seq
        与聚合载荷的 sha256 已经唯一确定是哪一版。要看截止时刻去
        ``/research/v1/meta``，那是给运维与 PI 自诊的面，不是数据面。
        """
        return {
            "epoch_seq": self.epoch_seq,
            "cohort_rule_version": self.cohort_rule_version,
            "aggregate_payload_sha256": self.payload_sha256,
        }


def bind_research_read(
    s: DBSession, *, config: Any,
) -> ResearchBinding:
    """取当前纪元，并把它冻住的那批场次还原成本库的场次号。

    这里**不查** ``authorized_reader_roles()``：那个白名单管的是"谁可以读研究
    分区**聚合**"，是一个 PI 要单独签字的治理决定。行面的读者集合由端点自己的
    ``{data_steward, admin}`` 限定，两件事不要合成一件——合起来会让一个没配
    聚合读者的机构连行级取数也一起消失，而那不是同一个决定。
    """
    if (os.environ.get(RELEASE_MODE_ENV) or "").strip() != RELEASE_MODE_REQUIRED:
        raise ReleaseRefused(
            "research_release_not_frozen", "研究分区未启用冻结发布")
    epoch = current_epoch(s)
    if epoch is None:
        raise ReleaseRefused("research_release_not_frozen", "还没有切过纪元")
    if epoch.deidentification_key_id != config.key_id:
        # 轮换密钥之后旧纪元的假名与当前密钥算不出同一个值。不拒的话这里会
        # 静默地一个场次都匹配不上，然后返回一份零行却仍标着"纪元 N"的数据。
        raise ReleaseRefused(
            "research_release_key_rotated",
            "去标识密钥已轮换，本纪元的假名与当前密钥对不上，需重新切纪元")
    manifest = validate_epoch_research_snapshot(s, epoch)
    assert epoch.research_snapshot_sha256 is not None
    return ResearchBinding(
        epoch_id=epoch.epoch_id,
        epoch_seq=epoch.epoch_seq,
        as_of=epoch.as_of,
        frozen_at=epoch.frozen_at,
        cohort_rule_version=epoch.cohort_rule_version,
        payload_sha256=epoch.payload_sha256,
        # 新纪元读取冻结行，不再把假名反查回活 Session。保留空集合只为旧的
        # simulation/live projection 类型兼容；snapshot 分支绝不能消费它。
        session_ids=frozenset(),
        snapshot_manifest=manifest,
        research_snapshot_sha256=epoch.research_snapshot_sha256,
    )


def validate_epoch_research_snapshot(
    s: DBSession, epoch: QualityReleaseEpoch,
) -> dict[str, Any]:
    """Validate one persisted epoch against its aggregate row-set anchor.

    This is shared by the HTTP bind and receipt recovery.  A per-row hash alone
    is not an anchor because an in-place DB edit can change both ``row_json``
    and ``row_sha256``; the immutable manifest digest must be recomputed too.
    """
    snapshot_fields = (
        epoch.research_snapshot_schema_version,
        epoch.research_snapshot_manifest_json,
        epoch.research_snapshot_sha256,
    )
    if all(value is None for value in snapshot_fields):
        raise ReleaseRefused(
            "research_release_snapshot_missing",
            "这一历史纪元没有冻结研究行快照，需重新切纪元后才能取行数据")
    if (any(value is None for value in snapshot_fields)
            or epoch.research_snapshot_schema_version
            != RESEARCH_SNAPSHOT_SCHEMA_VERSION):
        raise ReleaseRefused(
            "research_release_snapshot_corrupt",
            "冻结研究行快照的纪元字段不完整或版本不一致")
    assert epoch.research_snapshot_manifest_json is not None
    assert epoch.research_snapshot_sha256 is not None
    manifest = _validated_snapshot_manifest(
        epoch.research_snapshot_manifest_json,
        epoch.research_snapshot_sha256,
    )
    _validate_persisted_snapshot_rows(s, epoch.epoch_id, manifest)
    return manifest


def _validate_persisted_snapshot_rows(
    s: DBSession, epoch_id: str, manifest: dict[str, Any],
) -> None:
    datasets = {
        value if isinstance(value, str) else value[0]
        for value in s.exec(
            select(QualityReleaseEpochRowSnapshot.dataset_key)
            .where(QualityReleaseEpochRowSnapshot.epoch_id == epoch_id)
            .distinct()
        )
    }
    if not datasets <= set(research_dataset.dataset_keys()):
        raise ReleaseRefused(
            "research_release_snapshot_corrupt",
            "冻结研究行快照含未登记数据集")
    for dataset_key in research_dataset.dataset_keys():
        rows = list(s.exec(
            select(QualityReleaseEpochRowSnapshot).where(
                QualityReleaseEpochRowSnapshot.epoch_id == epoch_id,
                QualityReleaseEpochRowSnapshot.dataset_key == dataset_key,
            ).order_by(QualityReleaseEpochRowSnapshot.row_ordinal)
        ))
        facts = manifest["datasets"][dataset_key]
        if ([row.row_ordinal for row in rows]
                != list(range(1, facts["row_count"] + 1))):
            raise ReleaseRefused(
                "research_release_snapshot_corrupt",
                "冻结研究行快照的序号或行数与清单不一致")
        encoded_rows: list[bytes] = []
        dataset = research_dataset.dataset_for(dataset_key)
        assert dataset is not None
        for row in rows:
            raw = row.row_json.encode("utf-8")
            try:
                decoded = json.loads(row.row_json)
            except (TypeError, ValueError) as exc:
                raise ReleaseRefused(
                    "research_release_snapshot_corrupt",
                    "冻结研究行快照含不可解析行") from exc
            if (not isinstance(decoded, dict)
                    or canonical_bytes(decoded) != raw
                    or hashlib.sha256(raw).hexdigest() != row.row_sha256
                    or decoded != research_dataset.project(dataset, decoded)):
                raise ReleaseRefused(
                    "research_release_snapshot_corrupt",
                    "冻结研究行快照的行内容或摘要不一致")
            encoded_rows.append(raw)
        if _rows_digest(encoded_rows) != facts["rows_sha256"]:
            raise ReleaseRefused(
                "research_release_snapshot_corrupt",
                "冻结研究行快照的行集合与清单不一致")


def _resolve_frozen_sessions(
    s: DBSession, epoch: QualityReleaseEpoch, config: Any,
) -> frozenset[str]:
    """把纪元成员表里的场次假名还原成场次号。

    成员表有意只存假名——它就是队列构成本身，比任何聚合都更直接地指向人。代价
    是行面要用它过滤就得反查：假名是 HMAC，没有逆函数，只能把本库研究口径的
    场次号逐个算出假名来比。研究期是几百个场次量级，一次单列查询加几百次 HMAC；
    真涨到几万，下游那几条 ``IN`` 子句要先改成分块。
    """
    frozen = {
        row.session_pseudonym
        for row in s.exec(select(QualityReleaseEpochSession).where(
            QualityReleaseEpochSession.epoch_id == epoch.epoch_id))
    }
    resolved: dict[str, str] = {}
    for row in s.exec(select(TrainSession.session_id).where(
            TrainSession.data_classification == "research")):
        session_id = row if isinstance(row, str) else row[0]
        pseudonym = export_security.pseudonymize_session(session_id, config)
        if pseudonym in frozen:
            resolved[pseudonym] = session_id
    if len(resolved) != len(frozen):
        # 少一个都不许发：那样发出去的是纪元 N 的一个子集，却仍旧标着纪元 N。
        raise ReleaseRefused(
            "research_release_cohort_unresolved",
            "冻结队列里有场次在本库中找不到，行级取数面已关闭")
    return frozenset(resolved.values())


def binding_state(s: DBSession, *, config: Any) -> dict[str, Any]:
    """``/research/v1/meta`` 用的自诊投影：绑上了没有，没绑是因为什么。

    这里给出**精确**的冻结场次数，而聚合面只发档位。不矛盾：meta 的读者与行面
    的读者是同一批人，他们翻完页自己就能数出来，而把这个数摆在前面是他们校验
    "我是不是拉全了"的唯一手段。别把这条当成"精确队列规模可以发布"的先例。
    """
    try:
        binding = bind_research_read(s, config=config)
    except ReleaseRefused as refused:
        return {"bound": False, "code": refused.code, "reason": refused.detail}
    return {
        "bound": True,
        **binding.envelope(),
        "as_of": binding.as_of.isoformat() + "Z",
        "frozen_at": binding.frozen_at.isoformat() + "Z",
        "frozen_session_count": binding.frozen_session_count,
    }


def record_row_disclosure(
    s: DBSession, binding: ResearchBinding, *, actor_id: str, actor_role: str,
) -> None:
    """行面的一次取数也记进披露账本。

    聚合与明细指向同一个纪元，"谁看过纪元 N"就该把两条路径记在一起；AuditLog
    记得下这次取数，但它不知道纪元是哪一个。一页一行，不是一行一行。

    这里**不是** best-effort。`_audit` 那句 try/except 是给临床写路径的——锁分不能
    因为审计库抖动而失败。取数不是临床写：记不下谁读过就不该把数据发出去。
    """
    _append_disclosure(s, epoch_id=binding.epoch_id, actor_id=actor_id,
                       actor_role=actor_role,
                       payload_sha256=binding.payload_sha256)


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
