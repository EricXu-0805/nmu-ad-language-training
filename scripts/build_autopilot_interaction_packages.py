#!/usr/bin/env python3
"""从第2-8周训练内容 docx 构建自动带练交互协议数据包（双要素+多要素）。

铁律与 build_week_item_banks.py 相同：每一句呈现给患者的话术必须逐字来自
源 docx（本脚本内置逐句子串断言），或引用既有冻结题库字段/协议纠正模板；
本脚本不写任何新临床措辞。唯一允许的"新增"是结构冻结（分支闭集、阶梯深度、
无专属源句分支的复用规则），逐条写进数据包 structure_freezes 待钱凯/丁老师
书面确认。歧义映射（成功反馈选句等）可用 autopilot_interaction_curation.json
按段落索引逐处覆盖，缺省走确定性启发式并把落选句记进 frozen_out_source_lines。

用法：
  .venv/bin/python scripts/build_autopilot_interaction_packages.py --extract
  .venv/bin/python scripts/build_autopilot_interaction_packages.py --write
      # 生成 7 份数据包 + 同步协议 interaction_packages 字节钉 + 重钉演示 profile
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from build_week_item_banks import (
    CONTENT_DIR, DOUBLE_GROUP_RE, DOUBLE_TITLE_RE, MULTI_GROUP_RE,
    NAMEFIX_RE, NAMING_GROUP_RE, QUOTE_RE, SOURCE_DIR, WEEK_SOURCES,
    first_quote, normalized_text_sha256, paragraphs, _strip_outer_quotes,
)

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

CURATION_PATH = Path(__file__).resolve().parent / "autopilot_interaction_curation.json"
PROTOCOL_PATH = CONTENT_DIR / "autopilot_protocol_v1.json"
PROFILE_PATH = CONTENT_DIR / "autopilot_demo_profiles_v1.json"

PACKAGE_DRAFT_REVISION = "2026-08-22.1"
PROTOCOL_VERSION_ID = "autopilot-v2-20260821"
PROTOCOL_DRAFT_REVISION = "2026-08-22.1"

PURE_QUOTE_RE = re.compile(r"^“[^“”]+”$")
MARK_PREFIX_RE = re.compile(r"^(?:提示词\s*\(Prompt\)\s*)?[①②③④⑤]\s*")
_POSITION_MARKS = ("①", "②", "③", "④", "⑤")
DOUBLE_ROLES = ("左命名", "左作用", "右命名", "右作用", "关系识别")

# 协议 v2 相对 v1 的追加说明（v1 notes 原样保留，本清单幂等追加）。
PROTOCOL_V2_NOTES = [
    "v2 新增 interaction_packages：第2-8周双要素/多要素交互协议数据包按周登记，"
    "身份=数据包文件字节 sha256（协议 digest 链式覆盖全部新话术）；"
    "数据包内话术来源闭集=源 docx 逐字句/冻结题库字段/本协议纠正模板展开，"
    "结构冻结清单在各数据包 structure_freezes，待钱凯/丁老师书面确认。",
    "v2 起 v1 notes 中「双要素…推进规则未冻结」「多要素位置尚未结构化」两条的"
    "工程缺口由 interaction_packages 承接（临床批准状态不变，qc 仍为 draft）。",
]

MULTI_CONCEPT_POLICIES = {"all_required_concepts", "all_concept_groups", "hybrid"}


@dataclass
class Verbatim:
    text: str
    idx: int

    def as_speech(self) -> dict:
        return {"source": "verbatim", "text": self.text,
                "source_paragraph_index": self.idx}

    def as_question(self) -> dict:
        return self.as_speech()


@dataclass
class DoubleQuestions:
    slot: int
    title: str
    left_q: Verbatim | None = None
    left_fn_q: Verbatim | None = None
    right_q: Verbatim | None = None
    right_fn_q: Verbatim | None = None
    relation_q: Verbatim | None = None
    # 源稿该题的专属命名纠正句（「它叫X。」「它是X。」两种都认）。
    left_fix: Verbatim | None = None
    right_fix: Verbatim | None = None

    def gaps(self) -> list[str]:
        return [name for name in ("left_q", "left_fn_q", "right_q",
                                  "right_fn_q", "relation_q")
                if getattr(self, name) is None]


@dataclass
class ElementInteraction:
    key: str
    ask: Verbatim | None = None
    success: Verbatim | None = None
    partials: list[Verbatim] = field(default_factory=list)
    wrong: Verbatim | None = None
    tell: Verbatim | None = None
    completion_terms: list[str] = field(default_factory=list)
    frozen_out: list[tuple[int, str]] = field(default_factory=list)  # (idx, reason)

    def gaps(self) -> list[str]:
        g = []
        if self.ask is None:
            g.append(f"{self.key}:ask")
        if self.tell is None:
            g.append(f"{self.key}:tell")
        return g


@dataclass
class MultiInteraction:
    group_no: int
    elements: list[ElementInteraction] = field(default_factory=list)

    def gaps(self) -> list[str]:
        g = []
        if len(self.elements) != 4:
            g.append(f"ME{self.group_no}:要素数={len(self.elements)}")
        for el in self.elements:
            g.extend(f"ME{self.group_no}:{x}" for x in el.gaps())
        return g


@dataclass
class WeekInteraction:
    week_no: int
    paras: list[str]
    doubles: list[DoubleQuestions]
    multis: list[MultiInteraction]

    def gaps(self) -> list[str]:
        g = []
        if len(self.doubles) != 10:
            g.append(f"双要素问句组数={len(self.doubles)}")
        for it in self.doubles:
            g.extend(f"DE{it.slot}.{it.title}:{x}" for x in it.gaps())
        for it in self.multis:
            g.extend(it.gaps())
        return g


def parse_double_questions(paras: list[str], start: int, end: int,
                           slot: int, title: str) -> DoubleQuestions:
    out = DoubleQuestions(slot=slot, title=title)
    fn_questions: list[Verbatim] = []
    side: str | None = None
    for i in range(start, end):
        line = paras[i]
        if not line:
            continue
        if re.search(r"左边的\S{0,4}是什么", line):
            side = "left"
            q = first_quote(line)
            if q:
                out.left_q = Verbatim(_strip_outer_quotes(q), i)
        elif re.search(r"右边的\S{0,4}是什么", line):
            side = "right"
            q = first_quote(line)
            if q is None:
                # 源稿②行整行缺开引号（wk2-8 同款排版笔误）：剥序号前缀与残余
                # 收尾引号取问句——结果仍是源段落的连续子串，逐字未动。
                q = _strip_outer_quotes(MARK_PREFIX_RE.sub("", line, count=1))
            else:
                q = _strip_outer_quotes(q)
            out.right_q = Verbatim(q, i)
        elif "有什么关系" in line:
            side = None
            q = first_quote(line)
            if q:
                out.relation_q = Verbatim(_strip_outer_quotes(q), i)
        elif PURE_QUOTE_RE.match(line) and ("作用" in line or "特点" in line):
            fn_questions.append(Verbatim(_strip_outer_quotes(line), i))
        else:
            # 该题的专属命名纠正句（build_week_item_banks NAMEFIX_RE 同款
            # 抽取范式，「它叫…」「它是…」都认），逐字整句留存。
            q = first_quote(line)
            if (q and side is not None
                    and getattr(out, f"{side}_fix") is None
                    and NAMEFIX_RE.search(q)
                    and ("回答错误" in line or "刚刚" in line)):
                setattr(out, f"{side}_fix", Verbatim(_strip_outer_quotes(q), i))
    if len(fn_questions) == 2:
        out.left_fn_q, out.right_fn_q = fn_questions
        ordered = [out.left_q, out.left_fn_q, out.right_q, out.right_fn_q,
                   out.relation_q]
        if any(v is None for v in ordered) or [v.idx for v in ordered] != sorted(
                v.idx for v in ordered):
            out.left_fn_q = out.right_fn_q = None
    return out


def _last_quote_span(line: str):
    matches = list(QUOTE_RE.finditer(line))
    if not matches:
        return None
    last = matches[-1]
    return last.group(1), line[:last.start()]


def parse_multi_elements(paras: list[str], start: int, end: int,
                         group_no: int) -> MultiInteraction:
    from build_week_item_banks import MULTI_KEY_ORDER, parse_completion_groups
    item = MultiInteraction(group_no=group_no)
    marks: list[tuple[int, int]] = []
    for i in range(start, end):
        for pos, mark in enumerate(_POSITION_MARKS, start=1):
            if paras[i].startswith(mark):
                marks.append((pos, i))
                break
    for n, (pos, i) in enumerate(marks):
        j = marks[n + 1][1] if n + 1 < len(marks) else end
        if pos == 5:  # ⑤整体描述不计分、不进协议题位
            continue
        el = ElementInteraction(key=MULTI_KEY_ORDER[pos - 1])
        head_quotes = QUOTE_RE.findall(paras[i])
        if head_quotes:
            el.ask = Verbatim(head_quotes[-1], i)
        pool: list[tuple[Verbatim, str]] = []  # (feedback, condition prefix)
        for k in range(i + 1, j):
            line = paras[k]
            if not line:
                continue
            if "即判定为任务达成" in line:
                groups = parse_completion_groups(line)
                el.completion_terms = [t for group in groups for t in group]
                continue
            span = _last_quote_span(line)
            if span is None or not re.match(r"^(?:当)?患者", line):
                continue
            feedback, condition = span
            if "保持沉默" in condition:
                el.tell = Verbatim(feedback, k)
            elif "回答错误" in condition:
                el.wrong = Verbatim(feedback, k)
            elif "只回答" in condition:
                el.partials.append(Verbatim(feedback, k))
            elif "回答" in condition:
                pool.append((Verbatim(feedback, k), condition))
        el.success = _choose_success(el, pool, group_no)
        item.elements.append(el)
    return item


def _choose_success(el: ElementInteraction,
                    pool: list[tuple[Verbatim, str]],
                    group_no: int) -> Verbatim | None:
    """成功反馈选句：排除仍在追问的句子后，取条件与完成条件词重合度最高者。

    类别驱动的分支（full/partial/wrong/silence）表达不了源稿按具体关键词
    选反馈的写法；未入选的源句全部记 frozen_out 待临床复核，不静默丢弃。
    """
    candidates: list[tuple[int, int, Verbatim]] = []
    for feedback, condition in pool:
        if feedback.text.endswith("？"):
            el.frozen_out.append(
                (feedback.idx,
                 "追问式反馈（句尾提问要求继续作答），类别驱动分支无法表达"))
            continue
        score = sum(1 for term in el.completion_terms if term in condition)
        candidates.append((score, feedback.idx, feedback))
    if not candidates:
        return None
    candidates.sort()
    chosen = candidates[-1][2]
    for score, idx, feedback in candidates[:-1]:
        el.frozen_out.append(
            (idx, "按完成条件词重合度冻结成功反馈选句，此句未入选"))
    return chosen


def load_curation() -> dict:
    if CURATION_PATH.exists():
        return json.loads(CURATION_PATH.read_text(encoding="utf-8"))
    return {}


def apply_curation(week_no: int, item: MultiInteraction,
                   paras: list[str], curation: dict) -> None:
    """按段落索引逐处覆盖成功反馈选句（歧义映射的人工出口）。"""
    week_entry = curation.get(f"week{week_no}", {})
    for el in item.elements:
        override = (week_entry.get(f"ME{item.group_no}", {}) or {}).get(el.key)
        if not override:
            continue
        idx = override["success_paragraph_index"]
        span = _last_quote_span(paras[idx])
        if span is None:
            raise SystemExit(
                f"week{week_no} ME{item.group_no}:{el.key} curation 段落 {idx} 无引语")
        el.success = Verbatim(span[0], idx)


def extract_week_interaction(week_no: int, curation: dict) -> WeekInteraction:
    paras = paragraphs(SOURCE_DIR / WEEK_SOURCES[week_no])
    naming_bounds: list[int] = []
    double_bounds: list[int] = []
    multi_bounds: list[int] = []
    for i, line in enumerate(paras):
        if NAMING_GROUP_RE.match(line):
            naming_bounds.append(i)
        elif DOUBLE_GROUP_RE.match(line):
            double_bounds.append(i)
        elif MULTI_GROUP_RE.match(line):
            multi_bounds.append(i)
    if len(double_bounds) != 2 or len(multi_bounds) != 2:
        raise SystemExit(
            f"week{week_no}: 组头数异常 double={len(double_bounds)} "
            f"multi={len(multi_bounds)}")
    titles: list[tuple[int, int, str]] = []
    for i in range(double_bounds[0], multi_bounds[0]):
        m = DOUBLE_TITLE_RE.match(paras[i])
        if m:
            titles.append((i, int(m.group(1)), m.group(2)))
    doubles = []
    for n, (i, slot, title) in enumerate(titles):
        j = titles[n + 1][0] if n + 1 < len(titles) else multi_bounds[0]
        doubles.append(parse_double_questions(paras, i, j, slot, title))
    multis = [
        parse_multi_elements(paras, multi_bounds[0], multi_bounds[1], 1),
        parse_multi_elements(paras, multi_bounds[1], len(paras), 2),
    ]
    for item in multis:
        apply_curation(week_no, item, paras, curation)
    return WeekInteraction(week_no, paras, doubles, multis)


def _speech_branch(kind: str, speech: dict | None) -> dict:
    branch: dict = {"kind": kind}
    if speech is not None:
        branch["speech"] = speech
    return branch


def _word_erratum_covering(errata: list[dict], item_id: str, side: str,
                           source_sentence: str, expansion: str) -> dict | None:
    """源稿专属句与模板展开的差异是否恰为题库登记的 {side}_word 勘误。"""
    for entry in errata:
        if entry.get("field") != f"{side}_word":
            continue
        if entry.get("item") not in (item_id, item_id.removeprefix("DE_")):
            continue
        src, fixed = entry.get("source_value"), entry.get("corrected_to")
        if (isinstance(src, str) and src and isinstance(fixed, str)
                and source_sentence.replace(src, fixed) == expansion):
            return entry
    return None


def _namefix_speech(bank_item: dict, side: str, turn_seq: int,
                    fix: Verbatim | None, protocol_double: dict,
                    errata: list[dict], freezes: list[str]) -> dict:
    """命名纠正句：优先源稿该题专属句 verbatim 入包，模板只在两种情形许用——
    专属句与模板展开逐字一致，或差异恰为题库登记的目标词勘误（逐处挂号）。"""
    item_id = bank_item["item_id"]
    template = protocol_double.get(f"namefix_{side}") or ""
    word = bank_item.get(f"{side}_word") or ""
    expansion = template.replace("【物品名】", word)
    if fix is None:
        freezes.append(
            f"{item_id}#{turn_seq} 源稿无专属命名纠正句，"
            "纠正句冻结为协议 namefix 模板+题库目标词展开")
        return {"source": "protocol_namefix", "side": side}
    if fix.text == expansion:
        return {"source": "protocol_namefix", "side": side}
    entry = _word_erratum_covering(errata, item_id, side, fix.text, expansion)
    if entry is not None:
        freezes.append(
            f"{item_id}#{turn_seq} 命名纠正句引用协议模板展开：与源稿专属句"
            f"的差异恰为题库登记勘误（{side}_word "
            f"{entry['source_value']}→{entry['corrected_to']}），"
            "随题库勘误口径（勘误本身仍待书面批准）")
        return {"source": "protocol_namefix", "side": side}
    return fix.as_speech()


def build_double_item(bank_item: dict, questions: DoubleQuestions,
                      protocol_double: dict, errata: list[dict],
                      freezes: list[str]) -> dict:
    turns = []
    specs = (
        ("左命名", questions.left_q,
         _namefix_speech(bank_item, "left", 1, questions.left_fix,
                         protocol_double, errata, freezes)),
        ("左作用", questions.left_fn_q,
         {"source": "bank_field", "field": "left_function_cue"}),
        ("右命名", questions.right_q,
         _namefix_speech(bank_item, "right", 3, questions.right_fix,
                         protocol_double, errata, freezes)),
        ("右作用", questions.right_fn_q,
         {"source": "bank_field", "field": "right_function_cue"}),
        ("关系识别", questions.relation_q,
         {"source": "bank_field", "field": "relation_cue"}),
    )
    for seq, (role, question, correction) in enumerate(specs, start=1):
        turns.append({
            "turn_seq": seq,
            "response_role": role,
            "max_recordings": 1,
            "question": question.as_question(),
            "branches": {
                "full": {"kind": "advance_silent"},
                "wrong": _speech_branch("speak_then_advance", correction),
                "silence": _speech_branch("speak_then_advance", correction),
            },
        })
    return {"item_id": bank_item["item_id"], "task_type": "双要素",
            "turns": turns}


def build_multi_item(bank_item: dict, extraction: MultiInteraction,
                     freezes: list[str], frozen_out: list[dict]) -> dict:
    item_id = bank_item["item_id"]
    rubrics = bank_item.get("operational_rubrics") or {}
    turns = []
    for seq, el in enumerate(extraction.elements, start=1):
        policy = (rubrics.get(el.key) or {}).get("decision_policy")
        multi_concept = policy in MULTI_CONCEPT_POLICIES
        where = f"{item_id}#{seq}({el.key})"
        tell = el.tell.as_speech()
        if el.success is not None:
            success_branch = _speech_branch(
                "speak_then_advance", el.success.as_speech())
        else:
            success_branch = _speech_branch("speak_then_advance", tell)
            freezes.append(
                f"{where} 源稿无专属成功反馈句，full 分支冻结为复用告知句")
        if el.wrong is not None:
            wrong_branch = _speech_branch(
                "speak_then_rerecord", el.wrong.as_speech())
        else:
            wrong_branch = _speech_branch("speak_then_advance", tell)
            freezes.append(
                f"{where} 源稿无回答错误定向提示句，wrong 分支冻结为复用告知句后推进"
                "（不再二次录音）")
        branches = {
            "full": success_branch,
            "wrong": wrong_branch,
            "silence": _speech_branch("speak_then_advance", tell),
        }
        if multi_concept:
            if el.partials:
                branches["partial"] = _speech_branch(
                    "speak_then_rerecord", el.partials[0].as_speech())
                for extra in el.partials[1:]:
                    frozen_out.append({
                        "item_id": item_id, "turn_seq": seq,
                        "source_paragraph_index": extra.idx,
                        "reason": "同环节多条部分命中反馈按段落序冻结取首句，此句未入选",
                    })
                    freezes.append(
                        f"{where} 源稿有多条部分命中反馈（按缺失要素分句），"
                        "类别驱动分支冻结取段落序首句")
            else:
                branches["partial"] = _speech_branch("speak_then_advance", tell)
                freezes.append(
                    f"{where} 源稿无部分命中反馈句，partial 分支冻结为复用告知句后推进"
                    "（不再二次录音）")
        else:
            for extra in el.partials:
                frozen_out.append({
                    "item_id": item_id, "turn_seq": seq,
                    "source_paragraph_index": extra.idx,
                    "reason": "单概念 rubric 下引擎不产出 partial 类别，"
                              "源稿只回答分支无法进入类别驱动协议",
                })
        for idx, reason in el.frozen_out:
            frozen_out.append({
                "item_id": item_id, "turn_seq": seq,
                "source_paragraph_index": idx, "reason": reason,
            })
        has_rerecord = any(
            branch["kind"] == "speak_then_rerecord"
            for branch in branches.values())
        turn = {
            "turn_seq": seq,
            "response_role": el.key,
            "max_recordings": 2 if has_rerecord else 1,
            "question": el.ask.as_question(),
            "branches": branches,
        }
        if has_rerecord:
            turn["after_rerecord"] = {
                "full": success_branch,
                "otherwise": _speech_branch("speak_then_advance", tell),
            }
        turns.append(turn)
    return {"item_id": item_id, "task_type": "多要素", "turns": turns}


STRUCTURE_FREEZES_COMMON = [
    "分支闭集冻结：branches 键 ∈ {full, partial, wrong, silence}=引擎判分结果类别；"
    "partial 仅存在于题库 rubric 为多概念策略的多要素环节。",
    "阶梯深度冻结：双要素每环节恰 1 次录音；多要素每环节至多 2 次录音，"
    "第 2 次录音后 full→成功反馈推进、其余（含沉默）→告知句推进。",
    "双要素命中冻结为无口头反馈直接推进；未命中与沉默共用同一纠正/讲解句后推进"
    "（源稿只写「回答错误/不能回答」分支，沉默并入为结构冻结）。",
    "双要素命名纠正句优先逐字取源稿该题专属句（verbatim 入包）；仅当专属句与"
    "协议 namefix 模板+题库目标词展开逐字一致、或差异恰为题库登记勘误（逐处"
    "另列冻结条目）时引用模板。作用/关系讲解句引用题库冻结字段（随题库勘误"
    "口径，勘误本身仍待书面批准），不在本包重复冻结字节。",
    "多要素沉默分支冻结为告知句后推进（源稿沉默>10秒告知口径）。",
]


def build_package(week_no: int, curation: dict, bank) -> tuple[dict, list[str]]:
    sys.path.insert(0, str(PLATFORM_ROOT))
    from app import content as app_content

    extraction = extract_week_interaction(week_no, curation)
    gaps = extraction.gaps()
    if len(bank.double_element) != len(extraction.doubles):
        gaps.append("题库双要素条目数与源稿问句组数不一致")
    if len(bank.multi_element) != len(extraction.multis):
        gaps.append("题库多要素条目数与源稿组数不一致")
    if gaps:
        return {}, gaps

    protocol = app_content.load_autopilot_protocol(PROTOCOL_PATH)
    freezes = list(STRUCTURE_FREEZES_COMMON)
    frozen_out: list[dict] = []
    items = [build_double_item(bank_item, questions,
                               protocol.get("double") or {},
                               bank.errata_fixed, freezes)
             for bank_item, questions in zip(bank.double_element,
                                             extraction.doubles)]
    items += [build_multi_item(bank_item, ex, freezes, frozen_out)
              for bank_item, ex in zip(bank.multi_element, extraction.multis)]

    source_file = WEEK_SOURCES[week_no]
    package = {
        "interaction_schema_version": "autopilot-interaction.v1",
        "package_version_id": f"wk{week_no}-interaction-v1-20260821",
        "week_no": week_no,
        "qc_status": "draft",
        "draft_revision": PACKAGE_DRAFT_REVISION,
        "source_file": f"20260629/{source_file}",
        "source_document_sha256": hashlib.sha256(
            (SOURCE_DIR / source_file).read_bytes()).hexdigest(),
        "source_normalized_text_sha256": normalized_text_sha256(
            extraction.paras),
        "parent_item_bank_version_id": bank.version_id,
        "parent_item_bank_definition_digest":
            app_content.item_bank_definition_digest(bank),
        "silence_seconds": 10,
        "items": items,
        "structure_freezes": freezes,
        "frozen_out_source_lines": frozen_out,
        "notes": [
            "本数据包不含任何新写临床措辞：verbatim 句逐字取自源 docx（构建时"
            "强制子串断言），bank_field/protocol_namefix 引用既有冻结题库字段与"
            "协议模板。structure_freezes 为工程结构冻结清单，待钱凯/丁老师书面确认。",
            "frozen_out_source_lines 是源稿中按具体关键词选反馈、类别驱动分支"
            "表达不了的句子清单（未丢弃，留待临床复核扩展协议）。",
            "已知源稿勘误沿用题库 errata_fixed 口径：引用题库字段/模板展开的"
            "话术随题库勘误口径（勘误本身仍待钱凯/丁老师书面批准），本包不复制"
            "也不另立勘误。",
        ],
    }

    # 铁律断言：每句 verbatim 话术都是源 docx 规范化文本的子串。
    joined = "\n".join(extraction.paras)
    for item in items:
        for turn in item["turns"]:
            texts = [turn["question"]["text"]]
            for branch in turn["branches"].values():
                speech = branch.get("speech")
                if speech and speech["source"] == "verbatim":
                    texts.append(speech["text"])
            for branch in (turn.get("after_rerecord") or {}).values():
                speech = branch.get("speech")
                if speech and speech["source"] == "verbatim":
                    texts.append(speech["text"])
            for text in texts:
                if text not in joined:
                    raise SystemExit(
                        f"week{week_no} {item['item_id']}#{turn['turn_seq']} "
                        f"话术不是源文子串：{text[:40]}…")

    problems = app_content.validate_autopilot_interaction_package(
        package, bank, protocol)
    app_content._validate_frozen_schema(  # noqa: SLF001
        package, app_content._AutopilotInteractionSchema,  # noqa: SLF001
        label=f"week{week_no} 交互数据包")
    return package, problems


def cmd_extract(weeks: list[int]) -> int:
    curation = load_curation()
    bad = 0
    for wk in weeks:
        ex = extract_week_interaction(wk, curation)
        gaps = ex.gaps()
        state = "OK" if not gaps else f"{len(gaps)} 缺口"
        print(f"week{wk}: doubles={len(ex.doubles)} "
              f"multis={sum(len(m.elements) for m in ex.multis)}要素 -> {state}")
        for g in gaps:
            bad += 1
            print(f"  - {g}")
        for m in ex.multis:
            for el in m.elements:
                marks = []
                if el.success is None:
                    marks.append("无成功句")
                if el.wrong is None:
                    marks.append("无错误句")
                if el.partials:
                    marks.append(f"partial×{len(el.partials)}")
                if el.frozen_out:
                    marks.append(f"frozen_out×{len(el.frozen_out)}")
                if marks:
                    print(f"    ME{m.group_no}:{el.key} {' '.join(marks)}")
    return 1 if bad else 0


def _dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1) + "\n"


def sync_protocol(package_files: dict[int, str]) -> str:
    from app import content as app_content

    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    protocol["protocol_version_id"] = PROTOCOL_VERSION_ID
    protocol["draft_revision"] = PROTOCOL_DRAFT_REVISION
    protocol["supported_training_weeks"] = sorted(package_files)
    entries = {
        str(week): {
            "file": file_name,
            "sha256": hashlib.sha256(
                (CONTENT_DIR / file_name).read_bytes()).hexdigest(),
        }
        for week, file_name in sorted(package_files.items())
    }
    ordered = {}
    for key, value in protocol.items():
        if key == "interaction_packages":
            continue
        ordered[key] = value
        if key == "double":
            ordered["interaction_packages"] = entries
    if "interaction_packages" not in ordered:
        ordered["interaction_packages"] = entries
    ordered["notes"] = [n for n in ordered["notes"]
                        if n not in PROTOCOL_V2_NOTES] + PROTOCOL_V2_NOTES
    issues = app_content.validate_autopilot_protocol(ordered)
    if issues:
        raise SystemExit(f"协议升版校验失败：{issues}")
    PROTOCOL_PATH.write_text(_dump(ordered), encoding="utf-8")
    loaded = app_content.load_autopilot_protocol(PROTOCOL_PATH)
    return app_content.autopilot_protocol_definition_digest(loaded)


def repin_profile(protocol_digest: str) -> str:
    from app.autopilot_plan_profiles import _canonical_profile_digest

    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    profile["parent_autopilot_protocol_version_id"] = PROTOCOL_VERSION_ID
    profile["parent_autopilot_protocol_definition_digest"] = protocol_digest
    profile["profile_definition_digest"] = _canonical_profile_digest(profile)
    PROFILE_PATH.write_text(_dump(profile), encoding="utf-8")
    return profile["profile_definition_digest"]


def cmd_write(weeks: list[int]) -> int:
    from app import content as app_content

    curation = load_curation()
    package_files: dict[int, str] = {}
    for wk in weeks:
        bank = app_content.load_item_bank_for_week(wk)
        package, problems = build_package(wk, curation, bank)
        if problems:
            print(f"week{wk}: {len(problems)} 个问题，未写出")
            for p in problems:
                print(f"  - {p}")
            return 1
        file_name = f"autopilot_interaction_week{wk}_v1.json"
        (CONTENT_DIR / file_name).write_text(_dump(package), encoding="utf-8")
        package_files[wk] = file_name
        spoken = sum(
            1 for _ in app_content._iter_interaction_speeches(package))  # noqa: SLF001
        print(f"week{wk}: OK -> {file_name} "
              f"(items {len(package['items'])}, turns "
              f"{sum(len(it['turns']) for it in package['items'])}, "
              f"speeches {spoken}, freezes {len(package['structure_freezes'])}, "
              f"frozen_out {len(package['frozen_out_source_lines'])})")
    if sorted(package_files) != [2, 3, 4, 5, 6, 7, 8]:
        print("非全周次写出，跳过协议同步（协议钉必须覆盖 2..8 全部周）")
        return 0
    protocol_digest = sync_protocol(package_files)
    print(f"协议已升版 {PROTOCOL_VERSION_ID}，definition_digest={protocol_digest}")
    profile_digest = repin_profile(protocol_digest)
    print(f"演示 profile 已重钉，profile_definition_digest={profile_digest}")
    print("代码钉需手工同步：app/autopilot_plan_profiles.py "
          "WEEK2_SINGLE20_DEMO_DIGEST / web/src/autopilot/demoProfile.ts / 测试字面量")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", default="2,3,4,5,6,7,8")
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    weeks = [int(w) for w in args.weeks.split(",")]
    if args.extract:
        return cmd_extract(weeks)
    if args.write:
        return cmd_write(weeks)
    ap.print_help()
    return 64


if __name__ == "__main__":
    sys.exit(main())
