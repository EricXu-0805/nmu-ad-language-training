#!/usr/bin/env python3
"""从第2-8周训练内容 docx 构建结构化题库数据包。

判分与结构口径来源（不是工程自定）：
  - 《会议决策总结20260706》变量记录表：单要素命名 1/0；双要素 命名 1/0、
    作用 1/0、关系识别按 2026-08-19 钱凯口径**正确=1 其余=0**（取代会议表
    0.5 档，由 Eric 转达）；多要素只计算关键要素识别（评价体系第 6 条），
    因此每张多要素图只有 情境/事物/人物/动作 四个计分题位，⑤整体描述
    不计分、不进协议题位。
  - 每一句呈现给患者的话术都必须逐字来自源 docx；本脚本不写新临床措辞。
    开放回答 rubric 的关键词表在 curation 文件里维护，构建时强制每个
    关键词是对应源句的子串（判分依据不得超出源文本）。

用法：
  .venv/bin/python scripts/build_week_item_banks.py --extract   # 只抽取并打印缺口
  .venv/bin/python scripts/build_week_item_banks.py --emit-curation-skeleton
  .venv/bin/python scripts/build_week_item_banks.py --write     # 生成 week3..8 数据包
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PLATFORM_ROOT.parent
SOURCE_DIR = PROJECT_ROOT / "20260629"
CONTENT_DIR = PLATFORM_ROOT / "content"
CURATION_PATH = Path(__file__).resolve().parent / "week_item_bank_curation.json"

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

WEEK_SOURCES = {
    2: "第二周训练内容.docx",
    3: "第三周训练内容.docx",
    4: "第四周训练内容.docx",
    5: "第五周训练内容.docx",
    6: "第六周训练内容.docx",
    7: "第七周训练内容.docx",
    8: "第八周训练内容.docx",
}

QUOTE_RE = re.compile(r"“([^”]+)”")
ITEM_TITLE_RE = re.compile(r"^(\d{1,2})\.(\S{1,24})$")
DOUBLE_TITLE_RE = re.compile(r"^(\d{1,2})\.(\S+\+\S+)$")
NAMING_GROUP_RE = re.compile(r"^第[一二三四]组：图片命名训练$")
DOUBLE_GROUP_RE = re.compile(r"^第[一二]组：双要素图片训练$")
MULTI_GROUP_RE = re.compile(r"^第[一二]组：多要素图片训练$")
NAMEFIX_RE = re.compile(r"它[叫是](?:一只|一个|一条|一头|一把)?(\S{1,8}?)[。”]")

MULTI_KEY_ORDER = ("情境", "事物", "人物", "动作")


def paragraphs(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    out: list[str] = []
    for p in root.iter(f"{{{WORD_NS}}}p"):
        parts: list[str] = []
        for node in p.iter():
            if node.tag == f"{{{WORD_NS}}}t":
                parts.append(node.text or "")
            elif node.tag == f"{{{WORD_NS}}}tab":
                parts.append("\t")
            elif node.tag in {f"{{{WORD_NS}}}br", f"{{{WORD_NS}}}cr"}:
                parts.append("\n")
        out.append(" ".join("".join(parts).split()))
    return out


def normalized_text_sha256(paras: list[str]) -> str:
    joined = "\n".join(paras)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def first_quote(text: str) -> str | None:
    m = QUOTE_RE.search(text)
    if m:
        return m.group(1)
    # 源稿个别句子漏了收尾引号（第2周“花”同款笔误）：取开引号到行尾。
    start = text.find("“")
    if start >= 0:
        tail = text[start + 1:].strip()
        return tail or None
    return None


def all_quotes(text: str) -> list[str]:
    return QUOTE_RE.findall(text)


@dataclass
class SingleItem:
    slot: int
    word: str
    completion_terms: list[str] = field(default_factory=list)
    initial_prompt: str | None = None
    success_line: str | None = None
    related_terms: list[str] = field(default_factory=list)
    cue1: dict = field(default_factory=dict)      # branch -> {text, idx}
    cue2: str | None = None
    tell: str | None = None

    def gaps(self) -> list[str]:
        g = []
        if not self.completion_terms:
            g.append("完成条件")
        if not self.initial_prompt:
            g.append("初始引导语")
        if not self.success_line:
            g.append("成功话术")
        for b in ("unknown", "close", "silence"):
            if b not in self.cue1:
                g.append(f"第1级{b}")
        if not self.cue2:
            g.append("第2级提示")
        if not self.tell:
            g.append("告知答案")
        return g


@dataclass
class DoubleItem:
    slot: int
    title: str
    left_word: str | None = None
    right_word: str | None = None
    left_function: str | None = None
    right_function: str | None = None
    relation: str | None = None

    def gaps(self) -> list[str]:
        g = []
        for name in ("left_word", "right_word", "left_function",
                     "right_function", "relation"):
            if not getattr(self, name):
                g.append(name)
        return g


@dataclass
class MultiElement:
    key: str
    ask: str | None = None
    cue: str | None = None
    tell: str | None = None
    policy: str | None = None
    terms: list[str] = field(default_factory=list)
    groups: list[list[str]] = field(default_factory=list)

    def gaps(self) -> list[str]:
        g = []
        for name in ("ask", "cue", "tell", "policy"):
            if not getattr(self, name):
                g.append(f"{self.key}:{name}")
        if not self.terms:
            g.append(f"{self.key}:完成条件词")
        return g


@dataclass
class MultiItem:
    group_no: int
    scene: str | None = None
    initial_prompt: str | None = None
    elements: list[MultiElement] = field(default_factory=list)
    overall_span: tuple[int, int] | None = None

    def gaps(self) -> list[str]:
        g = []
        if not self.scene:
            g.append("场景词")
        if len(self.elements) != 4:
            g.append(f"要素数={len(self.elements)}")
        for el in self.elements:
            g.extend(el.gaps())
        return g


def _clean_cue(text: str) -> str:
    # 冒号可缺：wk8 眼睛的源段写成「第1次提示“…」，缺冒号导致前缀剥不掉，
    # “第1次提示”四个字会被 TTS 念给老人听（2026-08-19 审计实测）。
    text = re.sub(r"^第1次提示[：:]?\s*", "", text)
    cut = text.find("若成功命名")
    if cut > 0:
        text = text[:cut]
    return _strip_outer_quotes(text)


def _strip_outer_quotes(text: str) -> str:
    # 源稿偶有双写开引号（wk7 脚+鞋 关系句），逐层剥净。
    text = text.strip()
    while text.startswith("“"):
        text = text[1:].strip()
    while text.endswith("”"):
        text = text[:-1].strip()
    return text


def parse_singles(paras: list[str], start: int, end: int) -> list[SingleItem]:
    items: list[SingleItem] = []
    cur: SingleItem | None = None
    branch: str | None = None
    for i in range(start, end):
        line = paras[i]
        if not line:
            continue
        m = ITEM_TITLE_RE.match(line)
        if m and "+" not in line:
            cur = SingleItem(slot=int(m.group(1)), word=m.group(2))
            items.append(cur)
            branch = None
            continue
        if cur is None:
            continue
        if "即判定为任务达成" in line:
            cur.completion_terms = all_quotes(line)
        elif line.startswith("初始引导语"):
            cur.initial_prompt = first_quote(line)
        elif line.startswith("① 成功命名") or line.startswith("①成功命名"):
            cur.success_line = first_quote(line)
            if cur.success_line is None:
                for k in range(i + 1, min(i + 3, end)):
                    if paras[k]:
                        cur.success_line = first_quote(paras[k])
                        break
        elif line.startswith("②"):
            branch = "unknown"
        elif line.startswith("③"):
            branch = "close"
            cur.related_terms = all_quotes(line)
        elif line.startswith("④"):
            branch = "silence"
        elif line.startswith("第1次提示") and branch:
            cur.cue1[branch] = {"text": _clean_cue(line), "idx": i}
            branch = None
        elif line.startswith("第2次提示"):
            q = first_quote(line) or first_quote(paras[i + 1] if i + 1 < end else "")
            if q:
                cur.cue2 = q
        elif line.startswith("若仍未成功命名或持续沉默"):
            cur.tell = first_quote(line)
    return items


def parse_doubles(paras: list[str], start: int, end: int) -> list[DoubleItem]:
    items: list[DoubleItem] = []
    cur: DoubleItem | None = None
    side: str | None = None
    for i in range(start, end):
        line = paras[i]
        if not line or line == "lefttop":
            continue
        m = DOUBLE_TITLE_RE.match(line)
        if m:
            cur = DoubleItem(slot=int(m.group(1)), title=m.group(2))
            items.append(cur)
            side = None
            continue
        if cur is None:
            continue
        if re.search(r"左边的\S{0,4}是什么", line):
            side = "left"
        elif re.search(r"右边的\S{0,4}是什么", line):
            side = "right"
        elif "有什么关系" in line:
            side = "relation"
        nm = NAMEFIX_RE.search(line)
        if nm and ("回答错误" in line or "刚刚" in line) and side in ("left", "right"):
            setattr(cur, f"{side}_word", nm.group(1))
            continue
        if "不能回答" in line and ("提示" in line):
            q = first_quote(line)
            if q:
                q = _strip_outer_quotes(q)
                if side == "left" and not cur.left_function:
                    cur.left_function = q
                elif side == "right" and not cur.right_function:
                    cur.right_function = q
                elif side == "relation" and not cur.relation:
                    cur.relation = q
    return items


_POSITION_MARKS = ("①", "②", "③", "④", "⑤")


def parse_completion_groups(line: str) -> list[list[str]]:
    """完成条件行的文法：`[同时]抓取到 G1 [与 G2] 即判定…`。

    每个 G = 引语（内部可用 / 列并列可接受词），后可跟（或 X）给出等价替代词
    （替代词可能不带引号，如「（或耗子）」「（或计算机/微机）」）。
    组间「与」求与，组内任取其一。拍平会把标准正确回答判失败——
    wk6 院子 / wk8 家里 实测踩到。
    """
    start = line.find("抓取到")
    stop = line.find("即判定")
    if start < 0 or stop < 0:
        return []
    cond = line[start + len("抓取到"):stop]
    # 按引语外、括号外的顶层「与」切组
    segments: list[str] = []
    depth_quote = False
    depth_paren = 0
    buf = ""
    for ch in cond:
        if ch == "“":
            depth_quote = True
        elif ch == "”":
            depth_quote = False
        elif ch in "（(":
            depth_paren += 1
        elif ch in "）)":
            depth_paren = max(0, depth_paren - 1)
        if ch == "与" and not depth_quote and depth_paren == 0:
            segments.append(buf)
            buf = ""
            continue
        buf += ch
    segments.append(buf)

    groups: list[list[str]] = []
    for segment in segments:
        terms: list[str] = []
        for quoted in all_quotes(segment):
            terms.extend(p for p in quoted.split("/") if p)
        # 括号里的替代词（去掉引语后剩下的部分）
        remainder = QUOTE_RE.sub("", segment)
        for paren in re.findall(r"[（(]([^）)]*)[）)]", remainder):
            for junk in ("或包含", "或具体人物特征如", "或者", "或", "包含", "如"):
                paren = paren.replace(junk, " ")
            for token in re.split(r"[/、，,\s]+", paren):
                token = token.strip("等。 ")
                if token:
                    terms.append(token)
        deduped = list(dict.fromkeys(terms))
        if deduped:
            groups.append(deduped)
    return groups


def parse_multi_group(paras: list[str], start: int, end: int,
                      group_no: int) -> MultiItem:
    item = MultiItem(group_no=group_no)
    spans: list[tuple[int, int, int]] = []
    marks: list[tuple[int, int]] = []
    for i in range(start, end):
        line = paras[i]
        for pos, mark in enumerate(_POSITION_MARKS, start=1):
            if line.startswith(mark):
                marks.append((pos, i))
                break
    for n, (pos, i) in enumerate(marks):
        j = marks[n + 1][1] if n + 1 < len(marks) else end
        spans.append((pos, i, j))
    for pos, i, j in spans:
        block = paras[i:j]
        head = block[0]
        ask = None
        quotes = all_quotes(head)
        if quotes:
            ask = quotes[-1]
        if pos == 5:
            item.overall_span = (i, j - 1)
            continue
        el = MultiElement(key=MULTI_KEY_ORDER[pos - 1], ask=ask)
        wrong = None
        partial = None
        for line in block[1:]:
            if not line:
                continue
            if "即判定为任务达成" in line:
                el.groups = parse_completion_groups(line)
                el.terms = [t for group in el.groups for t in group]
                if len(el.groups) == 1:
                    el.policy = "any_acceptable_expression"
                elif all(len(group) == 1 for group in el.groups):
                    el.policy = "all_required_concepts"
                else:
                    el.policy = "all_concept_groups"
            elif "回答错误" in line:
                q = first_quote(line)
                if q and wrong is None:
                    wrong = q
            elif "保持沉默" in line:
                q = first_quote(line)
                if q and el.tell is None:
                    el.tell = q
            elif line.startswith("当患者") and partial is None:
                qs = all_quotes(line)
                if qs:
                    partial = qs[-1]
        el.cue = wrong or partial or el.tell
        if pos == 1 and el.terms:
            item.scene = el.terms[0]
            item.initial_prompt = el.ask
        item.elements.append(el)
    return item


@dataclass
class WeekExtraction:
    week_no: int
    source_path: Path
    paras: list[str]
    singles: list[SingleItem]
    doubles: list[DoubleItem]
    multis: list[MultiItem]

    def gaps(self) -> list[str]:
        g = []
        if len(self.singles) != 20:
            g.append(f"单要素数={len(self.singles)}")
        if len(self.doubles) != 10:
            g.append(f"双要素数={len(self.doubles)}")
        if len(self.multis) != 2:
            g.append(f"多要素数={len(self.multis)}")
        for it in self.singles:
            g.extend(f"SE{it.slot}.{it.word}:{x}" for x in it.gaps())
        for it in self.doubles:
            g.extend(f"DE{it.slot}.{it.title}:{x}" for x in it.gaps())
        for it in self.multis:
            g.extend(f"ME{it.group_no}:{x}" for x in it.gaps())
        return g


def extract_week(week_no: int) -> WeekExtraction:
    path = SOURCE_DIR / WEEK_SOURCES[week_no]
    paras = paragraphs(path)
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
    if len(naming_bounds) != 4 or len(double_bounds) != 2 or len(multi_bounds) != 2:
        raise SystemExit(
            f"week{week_no}: 组头数异常 naming={len(naming_bounds)} "
            f"double={len(double_bounds)} multi={len(multi_bounds)}")
    singles = parse_singles(paras, naming_bounds[0], double_bounds[0])
    doubles = parse_doubles(paras, double_bounds[0], multi_bounds[0])
    multis = [
        parse_multi_group(paras, multi_bounds[0], multi_bounds[1], 1),
        parse_multi_group(paras, multi_bounds[1], len(paras), 2),
    ]
    return WeekExtraction(week_no, path, paras, singles, doubles, multis)


def cmd_extract(weeks: list[int]) -> int:
    bad = 0
    for wk in weeks:
        ex = extract_week(wk)
        gaps = ex.gaps()
        state = "OK" if not gaps else f"{len(gaps)} 缺口"
        print(f"week{wk}: singles={len(ex.singles)} doubles={len(ex.doubles)} "
              f"multis={len(ex.multis)} -> {state}")
        for g in gaps:
            bad += 1
            print(f"  - {g}")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", default="3,4,5,6,7,8")
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("--emit-curation-skeleton", action="store_true")
    ap.add_argument("--build-assets", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--patch-week2", action="store_true")
    args = ap.parse_args()
    weeks = [int(w) for w in args.weeks.split(",")]
    if args.extract:
        return cmd_extract(weeks)
    if args.emit_curation_skeleton:
        return cmd_emit_skeleton(weeks)
    if args.build_assets:
        return cmd_build_assets()
    if args.write:
        return cmd_write(weeks)
    if args.patch_week2:
        return cmd_patch_week2()
    ap.print_help()
    return 64


def cmd_patch_week2() -> int:
    """把既有 week2 数据包补到与 week3-8 同一交付口径。

    动的只有：多要素两题结构化、双要素 rubric、acceptable_expressions、
    difficulty_level、meta 冻结字段。已审校过的话术与 cue 变体一字不碰。
    """
    bank_path = CONTENT_DIR / "item_bank_v1.json"
    bank = json.loads(bank_path.read_text(encoding="utf-8"))
    ex = extract_week(2)
    curation = load_curation().get("week2", {})
    errata: list[dict] = list(bank.get("errata_fixed") or [])

    by_word = {it.word: it for it in ex.singles}
    for item in bank["single_element"]:
        word = item["item_id"].removeprefix("SE_")
        src = by_word.get(word)
        if src is None:
            raise SystemExit(f"week2 源稿里找不到单要素“{word}”")
        if not item.get("acceptable_expressions"):
            item["acceptable_expressions"] = (
                list(dict.fromkeys(src.completion_terms))
                or [item["target_word"]])
        item["difficulty_level"] = 2

    for item in bank["double_element"]:
        cur = curation.get(item["item_id"])
        if cur is None:
            raise SystemExit(f"week2 {item['item_id']} 缺 curation 条目")
        rubrics = {}
        for role, sentence in (("左作用", item["left_function_cue"]),
                               ("右作用", item["right_function_cue"]),
                               ("关系识别", item["relation_cue"])):
            keywords = cur[role]["keywords"]
            if not keywords:
                raise SystemExit(f"week2 {item['item_id']}:{role} 关键词为空")
            rubrics[role] = build_rubric(sentence, keywords, week_no=2)
        item["operational_rubrics"] = rubrics
        item["difficulty_level"] = 2

    bank["multi_element"] = [build_multi(2, it, errata) for it in ex.multis]

    # patch 可重复执行：按 (item, field, corrected_to) 去重，不累积重复勘误。
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for row in errata:
        key = (row.get("item"), row.get("field"), row.get("corrected_to"))
        if key not in seen:
            seen.add(key)
            deduped.append(row)
    errata = deduped

    bank["qc_status"] = "frozen"
    bank["draft_revision"] = DRAFT_REVISION
    bank["patient_asset_delivery_manifest"] = delivery_manifest_binding()
    bank["source_protocol_position_count"] = 78
    bank["source_unstructured_positions"] = []
    bank["source_unstructured_blockers"] = []
    bank["errata_fixed"] = errata
    marker = "判分口径：《会议决策总结20260706》"
    base_note = bank["note"].split(marker)[0].rstrip()
    bank["note"] = (base_note + " " + SCORING_NOTE +
                    " 两张多要素场景图按源协议保留图内场景名牌（协议本身让"
                    "患者读牌）；如 PI 决定去字需重做图并重审。冻结授权链："
                    "钱凯 2026-08-19 定判分口径（Eric 转达），书面签署待补。")

    problems = validate_bank_dict(bank, "item_bank_v1.json")
    if problems:
        print(f"week2: {len(problems)} 个问题，未写出")
        for p in problems:
            print(f"  - {p}")
        return 1
    bank_path.write_text(
        json.dumps(bank, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")
    print(f"week2: OK -> item_bank_v1.json "
          f"(单{len(bank['single_element'])}/双{len(bank['double_element'])}"
          f"/多{len(bank['multi_element'])}, errata {len(errata)})")
    return 0


# ---------------------------------------------------------------------------
# 图片：PNG 源 -> 受控 WebP + 全量交付清单（wk2..wk8 各 30+2）。
# 已存在的 webp 一律不重转（wk2 的 30 张保持生产上正在用的字节）。
# ---------------------------------------------------------------------------

IMAGE_SOURCE_ROOT = PROJECT_ROOT / "每周图片材料"
WEEK_IMAGE_DIRS = {2: "第二周", 3: "第三周", 4: "第四周",
                   5: "第五周", 6: "第六周", 7: "第七周", 8: "第八周"}
NUMBERED_PNG_RE = re.compile(r"^(\d{1,2})\.(.+)\.png$")
MULTI_PNG_RE = re.compile(r"^多要素图片(?:训练)?([12])\.png$")
ASSETS_DIR = CONTENT_DIR / "patient_assets"
DELIVERY_PATH = CONTENT_DIR / "patient_asset_delivery_v1.json"
MANIFEST_VERSION_ID = "wk2-8-private-webp-20260819.1"
APPROVED_BY = "钱凯（口头授权按会议决策处理，Eric 2026-08-19 转达；书面签署待补）"
APPROVED_AT = "2026-08-19"


def _week_image_files(week_no: int) -> dict[str, Path]:
    folder = IMAGE_SOURCE_ROOT / WEEK_IMAGE_DIRS[week_no]
    result: dict[str, Path] = {}
    for entry in sorted(folder.iterdir()):
        name = unicodedata.normalize("NFC", entry.name)
        m = NUMBERED_PNG_RE.match(name)
        if m:
            slot = int(m.group(1))
            result[f"wk{week_no}-{slot:02d}"] = entry
            continue
        m = MULTI_PNG_RE.match(name)
        if m:
            result[f"wk{week_no}-multi-{int(m.group(1)):02d}"] = entry
    if len(result) != 32:
        raise SystemExit(
            f"week{week_no} 图片目录解析出 {len(result)} 张，应为 32")
    return result


def _canonical_manifest_digest(definition: dict, exclude: frozenset) -> str:
    payload = {k: v for k, v in definition.items() if k not in exclude}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cmd_build_assets() -> int:
    import subprocess
    sys.path.insert(0, str(PLATFORM_ROOT))
    from app.patient_asset import _webp_metadata

    rows = []
    converted = 0
    for wk in range(2, 9):
        for image_id, png in sorted(_week_image_files(wk).items()):
            webp = ASSETS_DIR / f"{image_id}.webp"
            if not webp.exists():
                result = subprocess.run(
                    ["cwebp", "-quiet", "-q", "90", "-noalpha", "-metadata", "none",
                     str(png), "-o", str(webp)],
                    capture_output=True, text=True)
                if result.returncode != 0:
                    raise SystemExit(
                        f"cwebp 失败 {png.name}: {result.stderr.strip()}")
                converted += 1
            payload = webp.read_bytes()
            width, height, frames = _webp_metadata(payload)
            rows.append({
                "image_id": image_id,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "byte_size": len(payload),
                "width": width,
                "height": height,
                "frame_count": frames,
            })

    definition = {
        "manifest_schema_version": "patient-asset-delivery.v1",
        "manifest_version_id": MANIFEST_VERSION_ID,
        "image_id_contract_version": "training-image-id.v1",
        "purpose": "research_and_simulation_byte_allowlist",
        "release_scope": "research_and_simulation",
        "research_release_status": "approved_for_research_presentation",
        "media_type": "image/webp",
        "assets": rows,
    }
    scope_sha = _canonical_manifest_digest(
        definition,
        frozenset({"definition_sha256", "research_release_approvals"}))
    definition["research_release_approvals"] = {
        "source_transform_rights": {
            "approved_by": APPROVED_BY,
            "approved_at": APPROVED_AT,
            "scope_sha256": scope_sha,
        },
        "content": {
            "approved_by": APPROVED_BY,
            "approved_at": APPROVED_AT,
            "scope_sha256": scope_sha,
        },
    }
    definition["definition_sha256"] = _canonical_manifest_digest(
        definition, frozenset({"definition_sha256"}))

    DELIVERY_PATH.write_text(
        json.dumps(definition, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")

    from app import patient_asset
    checked = patient_asset._read_delivery_definition()
    print(f"资产清单 OK：{len(rows)} 张（新转 {converted}），"
          f"release_scope={checked['release_scope']}，"
          f"definition_sha256={checked['definition_sha256'][:12]}…")
    return 0


def cmd_emit_skeleton(weeks: list[int]) -> int:
    existing = {}
    if CURATION_PATH.exists():
        existing = json.loads(CURATION_PATH.read_text(encoding="utf-8"))
    out = dict(existing)
    for wk in weeks:
        ex = extract_week(wk)
        wkey = f"week{wk}"
        week_entry = out.setdefault(wkey, {})
        for it in ex.doubles:
            item_key = f"DE_{it.title}"
            entry = week_entry.setdefault(item_key, {})
            for role, sentence in (("左作用", it.left_function),
                                   ("右作用", it.right_function),
                                   ("关系识别", it.relation)):
                slot = entry.setdefault(role, {})
                slot["sentence"] = sentence
                slot.setdefault("keywords", [])
    CURATION_PATH.write_text(
        json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"骨架已写入 {CURATION_PATH}")
    return 0


# ---------------------------------------------------------------------------
# 勘误表：全部来自 2026-07 图片评审 18 条发现 + 2026-08-19 逐张目视复核。
# 发现的模式：docx ①/② 的顺序与图一致（namefix 词 = 视觉左右），坏的只是
# 句子里的方位词、两题作用句整体互换、两个词粒度。修正一律留 errata 记录。
# ---------------------------------------------------------------------------

# (week, docx标题) -> 动作列表
DOUBLE_ERRATA: dict[tuple[int, str], list[tuple]] = {
    (3, "水壶+茶杯"): [("fix_direction", "left_function", "右边", "左边")],
    (4, "窗户+窗帘"): [("swap_functions",),
                     ("fix_direction", "left_function", "右边", "左边"),
                     ("fix_direction", "right_function", "左边", "右边")],
    (4, "太阳+向日葵"): [("set_word", "right_word", "向日葵")],
    (5, "手+手套"): [("fix_direction", "left_function", "右边", "左边"),
                   ("fix_direction", "right_function", "左边", "右边")],
    (5, "摩托车+头盔"): [
        ("transplant", "left_function", "single", "头盔", "unknown"),
        ("transplant", "right_function", "single", "摩托车", "unknown"),
        ("transplant", "relation", "single", "摩托车", "silence"),
    ],
    (6, "衣架+大衣"): [("fix_direction", "left_function", "右边", "左边"),
                    ("fix_direction", "right_function", "左边", "右边")],
    (6, "房子+烟囱"): [("prefix_relation", "它")],
    (6, "锯+树"): [("doc", "图片文件名标签",
                  "锯子（28.锯子+树.png）", "锯（按源稿与完成条件）",
                  "源稿用“锯”、图片文件名用“锯子”；题面从源稿，患者说“锯子”"
                  "因含“锯”子串仍判正确，无运行时影响")],
    (7, "窗户+窗帘"): [("swap_functions",),
                     ("fix_direction", "left_function", "右边", "左边"),
                     ("fix_direction", "right_function", "左边", "右边")],
    (7, "猴子+香蕉"): [("fix_direction", "left_function", "右边", "左边"),
                    ("fix_direction", "right_function", "左边", "右边")],
    (8, "戒指+食指"): [("set_title", "戒指+手指")],
    (8, "衣架+大衣"): [("fix_direction", "left_function", "右边", "左边"),
                    ("fix_direction", "right_function", "左边", "右边")],
    (8, "猴子+香蕉"): [("fix_direction", "left_function", "右边", "左边"),
                    ("fix_direction", "right_function", "左边", "右边")],
}

# (week, 单要素词) -> 动作。全部是源文错位/笔误的修正，逐条留 errata。
SINGLE_ERRATA: dict[tuple[int, str], list[tuple]] = {
    (8, "毛毛虫"): [("set_completion", ["毛毛虫"],
                   "源稿完成条件行误写“花生”，与题面、全部提示语、告知话术和"
                   "图片（9.毛毛虫.png）矛盾；按题面定。原样冻结会让说出"
                   "“毛毛虫”的患者永远判不了达成")],
    (4, "花瓶"): [
        ("cue1_from", "close", "unknown",
         "③槽位源稿误贴书包题话术；改用本题②槽位原句（源文段落本就是"
         "“您说得很接近”开头的接近式话术，正是③槽应有的语体）"),
        ("cue1_from", "unknown", "silence",
         "②槽位源稿实为接近式话术（与③错位），对答“不知道”的患者语气错位；"
         "unknown 改用本题④句（“没关系，我们慢慢来”开头）"),
    ],
    (6, "锯"): [("doc", "图片文件名标签", "锯子（18.锯子.png）",
                "锯（按源稿与完成条件）",
                "源稿用“锯”、图片文件名用“锯子”；题面从源稿，患者说“锯子”"
                "因含“锯”子串仍判正确，无运行时影响")],
}

CUE1_TYPE = "语义"
CUE2_TYPE = "排除式"
LOCATOR_SCHEME = "python-docx compatible body paragraph index, zero-based"
DRAFT_REVISION = "2026-08-19.1"
RUBRIC_VERSION = "r1-20260819"

SCORING_NOTE = (
    "判分口径：《会议决策总结20260706》变量记录表；关系识别按 2026-08-19 "
    "钱凯口径 正确=1 其余=0（取代会议表 0.5 档，Eric 转达）；多要素只计算"
    "情境/事物/人物/动作四个关键要素（评价体系第6条），⑤整体描述不计分、"
    "不进协议题位，故每周计分题位=20+10×5+2×4=78。多要素四环节按源文①-④"
    "顺序映射到会议表四列；个别场景第③环节源文实为物品识别（如 wk4 窗户、"
    "wk8 家里），列名沿用表头、语义以 rubric 话术为准，列名调整待 PI 定。"
)


def apply_double_errata(week_no: int, it: DoubleItem,
                        singles: list[SingleItem],
                        errata: list[dict]) -> None:
    for action in DOUBLE_ERRATA.get((week_no, it.title), []):
        kind = action[0]
        if kind == "swap_functions":
            it.left_function, it.right_function = it.right_function, it.left_function
            errata.append({
                "item": f"DE_{it.title}", "field": "left_function/right_function",
                "source_value": "两侧作用句在源稿中互换",
                "corrected_to": "按图像与两句自身语义归位",
                "note": "图片评审 mapping-order-function 发现；目视复核 2026-08-19",
            })
        elif kind == "fix_direction":
            _, field_name, wrong, right = action
            sentence = getattr(it, field_name)
            if sentence and wrong in sentence:
                setattr(it, field_name, sentence.replace(wrong, right, 1))
                errata.append({
                    "item": f"DE_{it.title}", "field": f"{field_name}.方位词",
                    "source_value": wrong, "corrected_to": right,
                    "note": "句子描述的对象与句首方位词不符；按图像实况改方位词，"
                            "余文一字未动（目视复核 2026-08-19）",
                })
        elif kind == "set_word":
            _, field_name, value = action
            old = getattr(it, field_name)
            setattr(it, field_name, value)
            errata.append({
                "item": f"DE_{it.title}", "field": field_name,
                "source_value": old or "（空）", "corrected_to": value,
                "note": "源稿纠正话术用词与标题/图像粒度不一致，按标题+图像定名",
            })
        elif kind == "set_title":
            _, value = action
            errata.append({
                "item": f"DE_{it.title}", "field": "pair_title",
                "source_value": it.title, "corrected_to": value,
                "note": "源稿标题用“食指”，图像/文件名/纠正话术均为“手指”，按后者定名",
            })
            it.title = value
        elif kind == "prefix_relation":
            _, char = action
            old = it.relation or ""
            it.relation = char + old
            errata.append({
                "item": f"DE_{it.title}", "field": "relation_cue",
                "source_value": old[:20], "corrected_to": (char + old)[:20],
                "note": f"源稿引号内句首缺“{char}”字（笔误致句子残缺），补一字成句；"
                        "余文一字未动",
            })
        elif kind == "doc":
            _, field_name, source_value, corrected_to, note = action
            errata.append({
                "item": f"DE_{it.title}", "field": field_name,
                "source_value": source_value, "corrected_to": corrected_to,
                "note": note,
            })
        elif kind == "transplant":
            _, field_name, _, word, branch = action
            source_item = next(s for s in singles if s.word == word)
            text = source_item.cue1[branch]["text"]
            # 只做取子串：剥掉命名流程专用的首尾句，不新增一个字。
            for prefix in ("没关系，您可以再想一想。", "没关系，我们慢慢来。"):
                text = text.removeprefix(prefix)
            for suffix in ("您觉得它叫什么？", "您再想想它叫什么？"):
                text = text.removesuffix(suffix)
            text = text.strip()
            old = getattr(it, field_name)
            setattr(it, field_name, text)
            errata.append({
                "item": f"DE_{it.title}", "field": field_name,
                "source_value": (old or "（空）")[:40],
                "corrected_to": f"同周单要素“{word}”第1级{branch}路径原句"
                                "（剥去句首安抚语与句尾命名提问，未新写措辞）",
                "note": "源稿此处误贴其他题对话术（图片评审 wk5-25 复制错误）；"
                        "替换句为同周同物源句的连续子串",
            })


def load_curation() -> dict:
    data = json.loads(CURATION_PATH.read_text(encoding="utf-8"))
    return data


def build_rubric(sentence: str, keywords: list[str], *, week_no: int,
                 policy: str = "any_acceptable_expression",
                 concepts: list[str] | None = None,
                 tell: str | None = None,
                 check_substring: bool = True) -> dict:
    if not sentence or not sentence.strip():
        raise SystemExit("rubric 源句为空")
    for kw in keywords:
        if check_substring and kw not in sentence \
                and (tell is None or kw not in tell):
            raise SystemExit(f"关键词“{kw}”不是源句子串：{sentence[:40]}…")
    return {
        "rubric_version": f"wk{week_no}-{RUBRIC_VERSION}",
        "decision_policy": policy,
        "acceptable_expressions": keywords if policy != "all_required_concepts" else [],
        "required_concepts": concepts or (
            keywords if policy == "all_required_concepts" else []),
        "cues": {"1": sentence, "2": sentence},
        "tell_answer": tell or sentence,
    }


def apply_single_errata(week_no: int, it: SingleItem,
                        errata: list[dict]) -> None:
    original_cue1 = {b: dict(v) for b, v in it.cue1.items()}
    for action in SINGLE_ERRATA.get((week_no, it.word), []):
        kind = action[0]
        if kind == "set_completion":
            _, terms, note = action
            errata.append({
                "item": f"SE_{it.word}", "field": "acceptable_expressions",
                "source_value": "、".join(it.completion_terms) or "（空）",
                "corrected_to": "、".join(terms), "note": note,
            })
            it.completion_terms = list(terms)
        elif kind == "cue1_from":
            _, dst, src, note = action
            errata.append({
                "item": f"SE_{it.word}", "field": f"cues.1.{dst}",
                "source_value": original_cue1[dst]["text"][:40],
                "corrected_to": f"本题源文 {src} 槽位原句",
                "note": note,
            })
            it.cue1[dst] = dict(original_cue1[src])
        elif kind == "doc":
            _, field_name, source_value, corrected_to, note = action
            errata.append({
                "item": f"SE_{it.word}", "field": field_name,
                "source_value": source_value, "corrected_to": corrected_to,
                "note": note,
            })


def build_single(week_no: int, global_slot: int, it: SingleItem,
                 errata: list[dict]) -> dict:
    target = it.word
    if target not in (it.tell or ""):
        replacement = next(
            (term for term in it.completion_terms if term in (it.tell or "")),
            None)
        if replacement:
            errata.append({
                "item": f"SE_{it.word}", "field": "target_word",
                "source_value": it.word, "corrected_to": replacement,
                "note": "标题词未出现在告知话术中，按告知话术+完成条件定目标词",
            })
            target = replacement
    acceptable = list(dict.fromkeys(it.completion_terms)) or [target]
    related = [t for t in it.related_terms if t not in acceptable]
    return {
        "item_id": f"SE_{it.word}",
        "task_type": "单要素",
        "target_word": target,
        "acceptable_expressions": acceptable,
        "related_but_inaccurate": related,
        "initial_prompt": it.initial_prompt,
        "success_line": it.success_line,
        "cues": {
            "1": {
                "cue_type": CUE1_TYPE,
                "text": it.cue1["unknown"]["text"],
                "variants": {
                    branch: {
                        "text": it.cue1[branch]["text"],
                        "source_paragraph_index": it.cue1[branch]["idx"],
                    } for branch in ("unknown", "close", "silence")
                },
            },
            "2": {"cue_type": CUE2_TYPE, "text": it.cue2},
        },
        "tell_answer": it.tell,
        "image_id": f"wk{week_no}-{global_slot:02d}",
        "item_set_type": "训练集",
        "difficulty_level": week_no,
        "raw_source_ref": f"week{week_no}:{it.word}",
    }


def build_double(week_no: int, global_slot: int, it: DoubleItem,
                 curation_week: dict, errata: list[dict]) -> dict:
    item_key = f"DE_{it.title}"
    cur = curation_week.get(item_key)
    if cur is None:
        raise SystemExit(f"week{week_no} {item_key} 缺 curation 条目")
    rubrics = {}
    for role, sentence in (("左作用", it.left_function),
                           ("右作用", it.right_function),
                           ("关系识别", it.relation)):
        keywords = cur[role]["keywords"]
        if not keywords:
            raise SystemExit(f"week{week_no} {item_key}:{role} 关键词为空")
        rubrics[role] = build_rubric(sentence, keywords, week_no=week_no)
    return {
        "item_id": item_key,
        "task_type": "双要素",
        "pair_title": it.title,
        "left_word": it.left_word,
        "right_word": it.right_word,
        "left_function_cue": it.left_function,
        "right_function_cue": it.right_function,
        "relation_cue": it.relation,
        "operational_rubrics": rubrics,
        "image_id": f"wk{week_no}-{global_slot:02d}",
        "item_set_type": "训练集",
        "difficulty_level": week_no,
        "raw_source_ref": f"week{week_no}:{it.title}",
    }


def build_multi(week_no: int, it: MultiItem, errata: list[dict]) -> dict:
    rubrics = {}
    key_elements = []
    for el in it.elements:
        key_elements.append({"key": el.key, "label": el.key})
        groups = [list(group) for group in el.groups]
        if week_no == 2 and it.scene == "公园" and el.key == "人物" \
                and all("小伙子" not in group for group in groups):
            groups[0].append("小伙子")
            errata.append({
                "item": f"ME_{it.scene}", "field": "人物.完成条件",
                "source_value": "小男孩/小孩", "corrected_to": "小男孩/小孩/小伙子",
                "note": "源稿成功分支明文接受“小伙子”而完成条件遗漏，按成功分支补齐",
            })
        terms = [t for group in groups for t in group]
        # terms 逐字取自该环节完成条件行，无需再对 cue 句做子串校验。
        rubrics[el.key] = build_rubric(
            el.cue, terms, week_no=week_no, policy=el.policy,
            tell=el.tell, check_substring=False)
        rubrics[el.key]["tell_answer"] = el.tell
        if el.policy == "all_required_concepts":
            rubrics[el.key]["acceptable_expressions"] = []
            rubrics[el.key]["required_concepts"] = terms
        elif el.policy == "all_concept_groups":
            rubrics[el.key]["acceptable_expressions"] = []
            rubrics[el.key]["required_concepts"] = []
            rubrics[el.key]["required_concept_groups"] = groups
        else:
            rubrics[el.key]["acceptable_expressions"] = terms
            rubrics[el.key]["required_concepts"] = []
    return {
        "item_id": f"ME_{it.scene}",
        "task_type": "多要素",
        "scene_title": it.scene,
        "initial_prompt": it.initial_prompt,
        "key_elements": key_elements,
        "operational_rubrics": rubrics,
        "image_id": f"wk{week_no}-multi-{it.group_no:02d}",
        "item_set_type": "训练集",
        "difficulty_level": week_no,
        "raw_source_ref": f"week{week_no}:multi:{it.scene}",
    }


def delivery_manifest_binding() -> dict:
    manifest = json.loads(
        (CONTENT_DIR / "patient_asset_delivery_v1.json").read_text(
            encoding="utf-8"))
    return {
        "version_id": manifest["manifest_version_id"],
        "definition_sha256": manifest["definition_sha256"],
    }


def build_bank(week_no: int, curation: dict) -> dict:
    ex = extract_week(week_no)
    errata: list[dict] = []
    for it in ex.singles:
        apply_single_errata(week_no, it, errata)
    for it in ex.doubles:
        apply_double_errata(week_no, it, ex.singles, errata)
    curation_week = curation.get(f"week{week_no}", {})
    singles = [build_single(week_no, i + 1, it, errata)
               for i, it in enumerate(ex.singles)]
    doubles = [build_double(week_no, 21 + i, it, curation_week, errata)
               for i, it in enumerate(ex.doubles)]
    multis = [build_multi(week_no, it, errata) for it in ex.multis]
    source_file = WEEK_SOURCES[week_no]
    return {
        "item_bank_version_id": f"wk{week_no}-v1-20260819",
        "content_schema_version": "1.1",
        "training_week_no": week_no,
        "supported_training_weeks": [week_no],
        "qc_status": "frozen",
        "source": f"20260629/{source_file}（逐字抽取；勘误见 errata_fixed）",
        "source_document_sha256": hashlib.sha256(
            (SOURCE_DIR / source_file).read_bytes()).hexdigest(),
        "source_normalized_text_sha256": normalized_text_sha256(ex.paras),
        "draft_revision": DRAFT_REVISION,
        "patient_asset_delivery_manifest": delivery_manifest_binding(),
        "source_protocol_position_count": 78,
        "source_unstructured_positions": [],
        "source_unstructured_blockers": [],
        "note": SCORING_NOTE + " 冻结授权链：钱凯 2026-08-19 定判分口径"
                "（Eric 转达），工程按口径机械落地；书面签署待补，见交接包。",
        "errata_fixed": errata,
        "single_element": singles,
        "double_element": doubles,
        "multi_element": multis,
        "cue1_variant_source_locator_scheme": LOCATOR_SCHEME,
    }


def validate_bank_dict(bank_dict: dict, path_label: str) -> list[str]:
    sys.path.insert(0, str(PLATFORM_ROOT))
    from app import content as app_content
    bank = app_content.ItemBank(
        version_id=bank_dict["item_bank_version_id"],
        single_element=bank_dict["single_element"],
        double_element=bank_dict["double_element"],
        multi_element=bank_dict["multi_element"],
        errata_fixed=bank_dict["errata_fixed"],
        meta={k: v for k, v in bank_dict.items()
              if k not in ("single_element", "double_element", "multi_element",
                           "errata_fixed")},
    )
    app_content._validate_frozen_schema(
        bank_dict, app_content._ItemBankSchema, label=path_label)
    validation = app_content.validate_item_bank(bank)
    readiness = app_content.content_readiness(bank)
    problems = [f"error: {e}" for e in validation["errors"]]
    problems += [f"warning: {w}" for w in validation["warnings"]]
    if readiness["ready_for_research"] is not True:
        problems.append("ready_for_research != True")
    return problems


def cmd_write(weeks: list[int]) -> int:
    curation = load_curation()
    failures = 0
    for wk in weeks:
        bank_dict = build_bank(wk, curation)
        label = f"item_bank_week{wk}_v1.json"
        problems = validate_bank_dict(bank_dict, label)
        if problems:
            failures += 1
            print(f"week{wk}: {len(problems)} 个问题，未写出")
            for p in problems:
                print(f"  - {p}")
            continue
        out = CONTENT_DIR / label
        out.write_text(
            json.dumps(bank_dict, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
        print(f"week{wk}: OK -> {out.name} "
              f"(单{len(bank_dict['single_element'])}/双{len(bank_dict['double_element'])}"
              f"/多{len(bank_dict['multi_element'])}, errata {len(bank_dict['errata_fixed'])})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
