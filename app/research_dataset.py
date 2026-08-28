"""只读研究数据面的列契约与行投影（纯函数，无 DB、无 FastAPI、无 I/O）。

这个模块存在的理由是"数据字典不许和实际输出漂移"：字典由下面这张注册表
生成，行也由同一张表投影，两者不可能各自演化。**故意不用 SQLModel 反射
自动生成字典**——那样将来任何新增的数据库列都会静默进入对外契约。

列契约的唯一权威是 ``app/export.py`` 里那十张导出表：接口输出的假名与列名
与它逐列对齐，PI 拿到的 CSV 和用接口取的数才能直接 join。

三条边界（与去标识导出包同一口径，由 ``export_security.assert_deidentified_sheets``
在序列化前兜底强制）：
  - 直接标识符与数据库整数主键永不出现，改用分域 HMAC 假名和自然键；
  - 转写原文、人工确认文本、AI 理由、现场备注一律不出；
  - 一切绝对时间不出，只出相对量（时长、潜伏期、序号）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence


SCHEMA_VERSION = "research-read.v1"

#: 每一列的披露方式。``forbidden`` 只用于把"想都别想"的字段写进字典，
#: 让读字典的人看见它被显式排除了，而不是以为忘了做。
Disclosure = str  # "clear" | "pseudonym" | "derived_relative" | "forbidden"


@dataclass(frozen=True)
class Column:
    name: str
    disclosure: Disclosure
    dtype: str
    description: str
    unit: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class Dataset:
    key: str
    title: str
    grain: str
    columns: tuple[Column, ...]

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)


_SUBJECTS = Dataset(
    key="subjects",
    title="受试者",
    grain="一行一位受试者",
    columns=(
        Column("subject_code", "pseudonym", "string",
               "受试者假名，与去标识导出包的 subject_code 完全一致，可直接 join",
               source="HMAC(patient_id)"),
        Column("dementia_severity", "clear", "string", "认知障碍程度"),
        Column("mandarin_eligible", "clear", "boolean", "普通话资格"),
        Column("is_simulation_subject", "clear", "boolean", "是否专用模拟档案"),
        Column("secondary_use_allowed", "clear", "boolean", "是否允许二次利用"),
        Column("withdrawn", "clear", "boolean", "是否已登记研究撤回"),
        Column("session_count", "clear", "integer", "该受试者的场次数"),
        Column("patient_id", "forbidden", "-",
               "真实研究编号：永不出现在本接口，请用 subject_code"),
        Column("consent_person", "forbidden", "-", "知情同意签署人：永不出现"),
    ),
)

_SESSIONS = Dataset(
    key="sessions",
    title="场次",
    grain="一行一个训练场次",
    columns=(
        Column("session_code", "pseudonym", "string", "场次假名", source="HMAC(session_id)"),
        Column("subject_code", "pseudonym", "string", "所属受试者假名"),
        Column("week_no", "clear", "integer", "训练周次（2–8）"),
        Column("phase_type", "clear", "string", "阶段类型"),
        Column("event_line", "clear", "string", "事件线"),
        Column("session_sitting_no", "clear", "integer", "同周内第几次"),
        Column("runtime_status", "clear", "string", "场次运行终态"),
        Column("is_simulation", "clear", "boolean", "是否模拟场次"),
        Column("data_classification", "clear", "string", "数据分区：research / simulation"),
        Column("item_bank_version_id", "clear", "string", "冻结题库版本"),
        Column("autopilot_profile_version_id", "clear", "string", "自动驾驶计划档版本"),
        Column("withdrawn", "clear", "boolean",
               "该场次的受试者是否已登记研究撤回。撤回者的行是墓碑：除编号、"
               "自然键与本列外全为空。**统计时必须先按本列过滤**——行还在是为了"
               "让分母稳定，不是为了让内容可用"),
        Column("pseudonym_key_id", "clear", "string",
               "生成本次假名的密钥标识；两次导出此值不同就不能 join"),
        Column("training_date", "forbidden", "-", "绝对日期：永不出现，只出相对量"),
    ),
)

_TURNS = Dataset(
    key="turns",
    title="环节明细",
    grain="一行一个 (场次, 题目, 环节)",
    columns=(
        Column("session_code", "pseudonym", "string", "场次假名"),
        Column("subject_code", "pseudonym", "string", "受试者假名"),
        Column("item_id", "clear", "string", "冻结题目标识"),
        Column("task_type", "clear", "string", "题型：单要素 / 双要素 / 多要素"),
        Column("turn_seq", "clear", "integer", "环节序号（自然键，替代数据库主键）"),
        Column("response_role", "clear", "string", "作答角色"),
        Column("source_attempt_seq", "clear", "integer", "关联的原始尝试序号"),
        Column("prompt_level", "clear", "integer", "提示层级 0–3"),
        Column("duration_seconds", "clear", "float",
               "本环节收音时长（秒）。相对量，不是绝对时间；**不是命名反应时**——"
               "反应时需要临床先定「潜伏期从哪一刻起算」，平台今天不产它"),
        Column("asr_confidence", "clear", "float", "语音识别置信度"),
        Column("ai_answer_type", "clear", "string", "AI 判类"),
        Column("ai_score", "clear", "float", "AI 初评（永不覆盖人工锁定分）"),
        Column("ai_needs_review", "clear", "boolean", "AI 标记需人工复核"),
        Column("ai_judge_mode", "clear", "string", "判分后端：规则 / LLM 辅助"),
        Column("reviewed_score", "clear", "float", "人工复核分（研究真值）"),
        Column("score_locked", "clear", "boolean", "是否已锁分"),
        Column("element_value", "clear", "string", "分环节要素值"),
        Column("ai_human_diff", "clear", "float", "人工分减 AI 分，两者皆有时才有值"),
        Column("judge_portrait_used", "clear", "boolean",
               "判分是否用过画像：恒为 false，这一列是★画像不进判分的审计证据"),
        Column("withdrawn", "clear", "boolean",
               "该环节的受试者是否已登记研究撤回。撤回者的行是墓碑：自然键"
               "（item_id / turn_seq）与本列保留，其余全为空。**统计时必须先按"
               "本列过滤**——行还在是为了让分母稳定，不是为了让内容可用"),
        Column("asr_text", "forbidden", "-", "语音识别原文：永不出现"),
        Column("confirmed_response_text", "forbidden", "-", "人工确认作答文本：永不出现"),
        Column("judge_reason", "forbidden", "-", "AI 判分理由：永不出现"),
    ),
)

_QUESTIONNAIRE_RECORDS = Dataset(
    key="questionnaire_records",
    title="量表记录（试用道）",
    grain="一行一份已锁定的量表记录",
    columns=(
        Column("record_code", "pseudonym", "string",
               "量表记录假名，与去标识导出包的 record_code 完全一致，"
               "可直接与 questionnaire_item_values join",
               source="HMAC(record_id)"),
        Column("subject_code", "pseudonym", "string", "受试者假名"),
        Column("questionnaire_id", "clear", "string",
               "量表标识（sfacs_v1 / gds15_v1 / npiq_v1 / ace3_v1 / aft_v1）"),
        Column("phase_label", "clear", "string", "期别：前测 / 后测 / 随访 / 其他"),
        Column("phase_ordinal", "clear", "integer",
               "同一位受试者、同一份量表、同一期别内的序号。改错的做法是新建一条，"
               "**分析前必须先按 superseded_by_ordinal 过滤掉被作废的那些**"),
        Column("superseded_by_ordinal", "clear", "integer",
               "作废本条的那条记录的序号；为空表示本条仍然作数"),
        Column("definition_sha256", "clear", "string", "创建时定义包的字节指纹"),
        Column("scoring_rule_id", "clear", "string", "计分规则标识"),
        Column("computed_total", "clear", "float", "锁定时按源表计分说明算出的总分"),
        Column("cutoff_met", "clear", "boolean", "是否达界值；无界值规则时为空"),
        Column("computed_flag", "clear", "string", "分层界值下的判定标签"),
        Column("ai_draft_status", "clear", "string",
               "AI 初评状态。初评永不覆盖人工终值，只作建议"),
        Column("ai_draft_engine", "clear", "string", "出初评的引擎标识"),
        Column("withdrawn", "clear", "boolean",
               "该受试者是否已登记研究撤回。撤回者的行是墓碑：假名与自然键保留，"
               "其余全为空。**统计时必须先按本列过滤**"),
        Column("status", "forbidden", "-",
               "只有 locked 的记录进研究面，draft 不是证据，所以这一列没有意义"),
        Column("note", "forbidden", "-", "施测备注（自由文本）：永不出现"),
        Column("locked_by", "forbidden", "-", "锁定人账号：永不出现"),
        Column("locked_at", "forbidden", "-", "绝对时间：永不出现"),
    ),
)

_QUESTIONNAIRE_ITEM_VALUES = Dataset(
    key="questionnaire_item_values",
    title="量表逐条目作答（试用道）",
    grain="一行一个 (量表记录, 条目, 字段)",
    columns=(
        Column("record_code", "pseudonym", "string", "所属量表记录假名"),
        Column("subject_code", "pseudonym", "string", "受试者假名"),
        Column("item_key", "clear", "string", "条目键（自然键，替代数据库主键）"),
        Column("field_key", "clear", "string",
               "字段键：value / present / severity / frequency / element:*"),
        Column("final_value", "clear", "string", "人工终值——研究真值就是这一列"),
        Column("value_source", "clear", "string",
               "终值来源：human_direct / ai_accepted / ai_overridden"),
        Column("ai_draft_value", "clear", "string", "AI 建议值（永不覆盖终值）"),
        Column("withdrawn", "clear", "boolean", "所属受试者是否已登记研究撤回"),
        Column("ai_draft_rationale", "forbidden", "-", "AI 理由（自由文本）：永不出现"),
        Column("updated_at", "forbidden", "-", "绝对时间：永不出现"),
    ),
)

DATASETS: tuple[Dataset, ...] = (
    _SUBJECTS, _SESSIONS, _TURNS,
    _QUESTIONNAIRE_RECORDS, _QUESTIONNAIRE_ITEM_VALUES,
)
_BY_KEY = {dataset.key: dataset for dataset in DATASETS}


def dataset_for(key: str) -> Dataset | None:
    return _BY_KEY.get(key)


def dataset_keys() -> tuple[str, ...]:
    return tuple(dataset.key for dataset in DATASETS)


def published_columns(dataset: Dataset) -> tuple[str, ...]:
    """实际会出现在行里的列（``forbidden`` 只进字典，不进行）。"""
    return tuple(column.name for column in dataset.columns
                 if column.disclosure != "forbidden")


def dictionary_rows() -> list[dict[str, Any]]:
    """数据字典。行顺序 = 各数据集的列顺序，CSV 与 JSON 共用这一份。"""
    rows: list[dict[str, Any]] = []
    for dataset in DATASETS:
        for column in dataset.columns:
            rows.append({
                "dataset": dataset.key,
                "column": column.name,
                "disclosure": column.disclosure,
                "dtype": column.dtype,
                "unit": column.unit,
                "description": column.description,
                "source": column.source,
                "published": column.disclosure != "forbidden",
            })
    return rows


def project(dataset: Dataset, raw: Mapping[str, Any]) -> dict[str, Any]:
    """按注册表把一行原始投影收敛成对外行：只留登记过的可发布列。

    多出来的键直接丢弃而不是放行——注册表是闭集，任何"顺手多带一个字段"
    都必须先进字典。缺的键补 None，让 CSV 的列宽稳定。
    """
    return {name: raw.get(name) for name in published_columns(dataset)}


def project_all(dataset: Dataset,
                rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [project(dataset, row) for row in rows]


def registry_self_check() -> list[str]:
    """启动期自检：注册表本身是否自洽。返回问题清单，空表示没问题。"""
    problems: list[str] = []
    seen_keys: set[str] = set()
    allowed = {"clear", "pseudonym", "derived_relative", "forbidden"}
    for dataset in DATASETS:
        if dataset.key in seen_keys:
            problems.append(f"数据集 {dataset.key} 重复登记")
        seen_keys.add(dataset.key)
        names: set[str] = set()
        for column in dataset.columns:
            if column.name in names:
                problems.append(f"{dataset.key}.{column.name} 列名重复")
            names.add(column.name)
            if column.disclosure not in allowed:
                problems.append(
                    f"{dataset.key}.{column.name} 的 disclosure "
                    f"{column.disclosure!r} 不在闭集内")
        if not published_columns(dataset):
            problems.append(f"数据集 {dataset.key} 没有任何可发布列")
    return problems


def make_pseudonymizer(
        tokenize: Callable[[str], str]) -> Callable[[str | None], str | None]:
    """把假名函数包成"None 进 None 出"，避免各处重复写空值判断。"""
    def apply(identifier: str | None) -> str | None:
        return None if identifier is None else tokenize(identifier)
    return apply


def csv_matrix(dataset: Dataset,
               rows: Sequence[Mapping[str, Any]]) -> tuple[list[str], list[list[Any]]]:
    """返回 (表头, 行矩阵)。表头顺序与字典里该数据集的列顺序严格一致。"""
    header = list(published_columns(dataset))
    return header, [[row.get(name) for name in header] for row in rows]
