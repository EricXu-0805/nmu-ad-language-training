"""训练内容加载与校验（M5）。

把结构化题库/脚本从 JSON 载入并校验一致性——把「双人校对四者一致」自动化：
目标词 ↔ 成功话术 ↔ 告知话术 ↔ 线索 对不上就报出来（斧子+树 那类复制粘贴勘误即由此发现）。
纯标准库，无需装依赖即可跑测试与校验。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ItemBank:
    version_id: str
    single_element: list[dict]
    double_element: list[dict]
    multi_element: list[dict] = field(default_factory=list)
    errata_fixed: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def supported_training_weeks(self) -> tuple[int, ...]:
        """当前结构化内容真正覆盖的训练周；不得由 UI/运行时自行外推。"""
        raw = self.meta.get("supported_training_weeks", [])
        return tuple(int(w) for w in raw if isinstance(w, int) and 2 <= w <= 8)

    @property
    def qc_status(self) -> str:
        return str(self.meta.get("qc_status") or "draft")


def load_item_bank(path: str | Path) -> ItemBank:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    vid = data.get("item_bank_version_id")
    if not vid:
        raise ValueError("题库缺少 item_bank_version_id（每场次须绑冻结版本号）")
    return ItemBank(
        version_id=vid,
        single_element=data.get("single_element", []),
        double_element=data.get("double_element", []),
        multi_element=data.get("multi_element", []),
        errata_fixed=data.get("errata_fixed", []),
        meta={k: v for k, v in data.items()
              if k not in ("single_element", "double_element", "multi_element", "errata_fixed")},
    )


def load_week1_script(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not data.get("script_version_id"):
        raise ValueError("第一周脚本缺少 script_version_id")
    return data


def validate_item_bank(bank: ItemBank) -> dict[str, list[str]]:
    """两档校验，冻结前的自动校对闸：
      errors   = 缺陷/勘误（目标词↔告知话术↔标题 对不上、重复 id）——冻结前必须清零；
      warnings = 待补全（缺线索/成功/告知话术文本，如缺引号导致抽不出的线索）——内容组填。
    返回 {"errors": [...], "warnings": [...]}。
    """
    errors: list[str] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()

    if bank.qc_status not in {"draft", "reviewed", "frozen"}:
        errors.append(f"题库 qc_status 非法：{bank.qc_status!r}")
    if not bank.supported_training_weeks:
        errors.append("题库缺 supported_training_weeks，运行时不得猜测覆盖周次")
    if bank.qc_status != "frozen":
        warnings.append(f"题库尚未冻结（qc_status={bank.qc_status}），仅可用于开发/演示")

    for it in bank.single_element:
        iid = it.get("item_id", "?")
        if iid in seen_ids:
            errors.append(f"{iid}: item_id 重复")
        seen_ids.add(iid)
        tw = it.get("target_word")
        if not tw:
            errors.append(f"{iid}: 缺 target_word")
            continue
        tell = it.get("tell_answer")
        if tell and tw not in tell:
            # 四者一致核心：告知话术点到别的词 = 勘误（斧子+树/螺丝刀→衬衫 那类）
            errors.append(f"{iid}: 告知话术未含目标词“{tw}”（勘误）：{tell[:24]}…")
        elif not tell:
            warnings.append(f"{iid}: 缺 tell_answer")
        if not it.get("success_line"):
            warnings.append(f"{iid}: 缺 success_line")
        if not it.get("image_id"):
            warnings.append(f"{iid}: 缺 image_id")
        if not it.get("acceptable_expressions"):
            warnings.append(f"{iid}: acceptable_expressions 尚未由内容组确认")
        if not it.get("difficulty_level"):
            warnings.append(f"{iid}: difficulty_level 尚未标注")
        malformed = " ".join(str(x) for x in (it.get("related_but_inaccurate") or []))
        if "”“" in malformed or "\u201c\u201d" in malformed:
            warnings.append(f"{iid}: related_but_inaccurate 疑含解析引号残片")
        cues = it.get("cues", {})
        for lv in ("1", "2"):
            if not (cues.get(lv) or {}).get("text"):
                warnings.append(f"{iid}: 缺第{lv}级线索文本")

    for it in bank.double_element:
        iid = it.get("item_id", "?")
        if iid in seen_ids:
            errors.append(f"{iid}: item_id 重复")
        seen_ids.add(iid)
        title = it.get("pair_title", "")
        parts = [p for p in title.split("+") if p]
        for side in ("left_word", "right_word"):
            w = it.get(side)
            if not w:
                warnings.append(f"{iid}: 缺 {side}")
            elif w not in parts:
                errors.append(f"{iid}: {side}“{w}”不在标题“{title}”中（勘误）")
        if not it.get("image_id"):
            warnings.append(f"{iid}: 缺 image_id")
        for cue in ("left_function_cue", "right_function_cue", "relation_cue"):
            if not it.get(cue):
                warnings.append(f"{iid}: 缺 {cue}")

    for it in bank.multi_element:
        iid = it.get("item_id", "?")
        if iid in seen_ids:
            errors.append(f"{iid}: item_id 重复")
        seen_ids.add(iid)
        if not it.get("image_id"):
            warnings.append(f"{iid}: 缺 image_id")
        if not it.get("key_elements"):
            errors.append(f"{iid}: 多要素题缺 key_elements")

    if not bank.multi_element:
        warnings.append("当前结构化题库未包含多要素任务，尚未达到蓝图 M0 的完整题型闭环")

    return {"errors": errors, "warnings": warnings}


def content_readiness(bank: ItemBank) -> dict[str, object]:
    """面向 API/UI 的明确能力声明，避免把“能加载”误写成“可入组”。"""
    validation = validate_item_bank(bank)
    return {
        "qc_status": bank.qc_status,
        "supported_training_weeks": list(bank.supported_training_weeks),
        "ready_for_research": (
            bank.qc_status == "frozen"
            and not validation["errors"]
            and not validation["warnings"]
            and bool(bank.multi_element)
        ),
        "errors": validation["errors"],
        "warnings": validation["warnings"],
    }


def validate_week1_script(script: dict) -> list[str]:
    issues: list[str] = []
    zodiac = script.get("zodiac_closed_list", [])
    if len(zodiac) != 12:
        issues.append(f"属相封闭词表应为 12 个，实为 {len(zodiac)}")
    if not script.get("sections"):
        issues.append("缺 sections")
    if script.get("silence_seconds") != 10:
        issues.append("沉默触发阈值应为 10 秒（与第2–8周口径一致）")
    return issues


# 老人端前端硬编码的固定话术（PatientStage 问句 / RapportStage 兜底 / 试听句）。
# 改前端这些字符串时必须同步这里，否则云 TTS 拒合成、自动落回本地引擎。
FIXED_TTS_LINES: tuple[str, ...] = (
    "您好",
    "请看这张图片，这是什么？",
    "它是做什么用的呢？",
    "它们之间有什么关系呢？",
    "我们一起聊聊天，好吗？",
)


_SLOT_RE = re.compile(r"【[^】]*】")
_ZODIAC_SLOT = "【老人所说的属相】"


def _expand_slots(line: str, zodiac: list[str]):
    """槽位模板处理：属相是 12 生肖闭集 → 展开成具体句进白名单；
    开放槽位（兴趣/活动/称呼…）实例化后含老人自述内容=患者数据 →
    模板与实例一律不进白名单（云端拒合成，由本地引擎朗读）。"""
    if "【" not in line:
        yield line
        return
    if _SLOT_RE.findall(line) == [_ZODIAC_SLOT]:
        for z in zodiac:
            yield line.replace(_ZODIAC_SLOT, z)


def tts_allowlist(bank: ItemBank, week1_script: dict | None = None) -> frozenset[str]:
    """云 TTS 允许合成的全部文本（闭集）。

    红线：发往云端的文本只能来自题库/脚本/固定 UI 话术，永不携带患者字段。
    不在此集合的文本，云引擎一律拒合成（fail-closed，落回本地引擎/系统语音）。
    """
    lines: set[str] = set(FIXED_TTS_LINES)
    for it in bank.single_element:
        for t in (it.get("initial_prompt"), it.get("success_line"), it.get("tell_answer")):
            if t:
                lines.add(t)
        for cue in (it.get("cues") or {}).values():
            t = (cue or {}).get("text")
            if t:
                lines.add(t)
    for it in bank.double_element:
        for k in ("left_function_cue", "right_function_cue", "relation_cue"):
            t = it.get(k)
            if t:
                lines.add(t)
    if week1_script:
        zodiac = list(week1_script.get("zodiac_closed_list") or [])
        t = week1_script.get("generic_fallback_line")
        if t:
            lines.add(t)
        for sec in week1_script.get("sections") or []:
            if sec.get("speaker") != "机器人":
                continue
            if sec.get("line"):
                lines.update(_expand_slots(sec["line"], zodiac))
            for q in sec.get("questions") or []:
                for t in (q.get("ask"), q.get("success")):
                    if t:
                        lines.update(_expand_slots(t, zodiac))
        for slot in (week1_script.get("slots") or {}).values():
            t = (slot or {}).get("fallback_line")
            if t:
                lines.add(t)
    return frozenset(s.strip() for s in lines if s and s.strip())


# 内容目录默认位置
CONTENT_DIR = Path(__file__).resolve().parent.parent / "content"
