#!/usr/bin/env python3
"""把内容组要补的东西从"看懂 JSON schema"降成"填一张表"。

`content_freeze_report.py` 已经能说清**缺什么**，但没说**怎么交**。今天内容组
要补一条判分标准，得知道 `operational_rubrics` 这个键挂在哪一层、
`decision_policy` 只认哪三个字面量、`cues` 底下必须同时有 "1" 和 "2"。
这一层门槛跟临床内容毫无关系，却是真正卡住交付的东西。

三个子命令：

  export  按当前题库导出空白表（CSV，Excel/WPS 双击就能开，utf-8-sig 不乱码）
  check   校验填好的表——判据与运行时那道 fail-closed 门**是同一套**
  merge   把填好的内容拼回题库，产出一份新的 JSON

三条硬边界，因为这个脚本碰的是临床内容：

  1. **一个字都不生成。** 空白表只有题位标识和填写说明，任何话术、可接受说法、
     判分口径都必须由内容组自己写。
  2. **merge 绝不动 qc_status、draft_revision、源文档哈希。** 填完表不等于冻结；
     冻结仍然要内容负责人与 PI 两人独立复核后签字。merge 出来的题库照样是
     draft，照样不能用于真实受试者。
  3. **merge 绝不就地覆盖 `content/item_bank_v1.json`。** 必须显式给 --out，
     且目标文件已存在就直接拒绝。

退出码：0 = 这一步做完了；1 = 还有没填/填错的；2 = 文件读不了或用法错误。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import content  # noqa: E402


LIST_SEPARATORS = ("；", ";", "|")
_LIST_SPLIT = re.compile("[" + "".join(LIST_SEPARATORS) + "]")
#: 空白表里的占位记号。它们是给人看的"这一格不用动"，绝不能被当成填进去的值。
PLACEHOLDERS = ("（已有，不用改）", "（不适用）")
RUBRIC_FILE = "01_开放回答判分标准.csv"
NAMING_FILE = "02_可接受说法与难度.csv"
README_FILE = "00_填表说明.md"

RUBRIC_COLUMNS = [
    "item_id", "task_type", "response_role",
    "rubric_version", "decision_policy",
    "acceptable_expressions", "required_concepts",
    "cue_1", "cue_2", "tell_answer",
]
NAMING_COLUMNS = [
    "item_id", "task_type", "target_word",
    "acceptable_expressions", "difficulty_level",
]


def _is_placeholder(raw: str) -> bool:
    return (raw or "").strip() in PLACEHOLDERS


def _split_list(raw: str) -> list[str]:
    """一格里的多个值：中文分号、英文分号或竖线分隔。词组里的空格要保留。"""
    if _is_placeholder(raw):
        return []
    return [part.strip() for part in re.split(_LIST_SPLIT, raw or "")
            if part.strip()]


def _typed_items(bank: content.ItemBank):
    return [
        *((item, "单要素") for item in bank.single_element),
        *((item, "双要素") for item in bank.double_element),
        *((item, "多要素") for item in bank.multi_element),
    ]


def rubric_gaps(bank: content.ItemBank) -> list[dict[str, str]]:
    """缺开放回答判分标准的题位。判据直接借运行时那一个，不另写一份。"""
    unsupported = set(content.unsupported_operational_rubrics(bank))
    rows: list[dict[str, str]] = []
    for item, task_type in _typed_items(bank):
        if task_type == "单要素":
            continue
        item_id = str(item.get("item_id") or "?")
        for role in content._planned_open_roles(item, task_type):  # noqa: SLF001
            if f"{item_id}:{role}" not in unsupported:
                continue
            row = {name: "" for name in RUBRIC_COLUMNS}
            row.update({"item_id": item_id, "task_type": task_type,
                        "response_role": role})
            rows.append(row)
    return rows


def naming_gaps(bank: content.ItemBank) -> list[dict[str, str]]:
    """缺可接受说法或难度标注的题位。"""
    rows: list[dict[str, str]] = []
    for item, task_type in _typed_items(bank):
        single = task_type == "单要素"
        needs_expressions = single and not item.get("acceptable_expressions")
        needs_difficulty = not item.get("difficulty_level")
        if not (needs_expressions or needs_difficulty):
            continue
        row = {name: "" for name in NAMING_COLUMNS}
        row.update({
            "item_id": str(item.get("item_id") or "?"),
            "task_type": task_type,
            "target_word": str(item.get("target_word")
                               or item.get("pair_title") or ""),
        })
        # 已经填过的不要求重填，但也不预填空白让人以为漏了。
        # 双要素/多要素题的模式里根本没有 acceptable_expressions 这一格，
        # 填进去会被题库结构校验直接拒——所以明说"不适用"，不是留空。
        if not single:
            row["acceptable_expressions"] = "（不适用）"
        elif not needs_expressions:
            row["acceptable_expressions"] = "（已有，不用改）"
        if not needs_difficulty:
            row["difficulty_level"] = "（已有，不用改）"
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def _write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


README_TEXT = """# 内容缺口填表说明

这份表由 `scripts/content_gap_workbook.py export` 按**当前题库**生成，
每一行都是系统今天真的跑不动的一个题位。表里一个字都不是系统写的，
全部要内容组自己填。

## 怎么填

两个 CSV 用 Excel 或 WPS 双击打开就行（已带 BOM，不会乱码）。
**多个值写在同一格里，用中文分号 `；` 隔开**，例如：

    胡萝卜；红萝卜；甘笋

填完把整个文件夹交回来，或者自己跑一次：

    python scripts/content_gap_workbook.py check <这个文件夹>

它会逐行告诉你哪一格还没填、哪一格填的值系统不认。**全部通过之前不要签字。**

## 01_开放回答判分标准.csv

双要素题的每个开放回答环节都要一套判分标准，否则系统开场就会拒绝——
这是设计，不是故障。逐列说明：

| 列 | 填什么 |
| --- | --- |
| `item_id` / `task_type` / `response_role` | **系统填好的，不要改**。改了这一行会被判为无法对应到题位 |
| `rubric_version` | 你自己的版本号，例如 `2026-08-v1`。改过口径就换一个 |
| `decision_policy` | 只能填这三个之一：`any_acceptable_expression`（说中任意一个可接受说法就算对）、`all_required_concepts`（必须覆盖全部要点）、`hybrid`（两者结合） |
| `acceptable_expressions` | 可接受的说法，`；` 分隔 |
| `required_concepts` | 必须说到的要点，`；` 分隔 |
| `cue_1` | 第一层提示的原话（对老人说的完整句子） |
| `cue_2` | 第二层提示的原话 |
| `tell_answer` | 两层提示后仍说不出时，告知答案的完整话术 |

`acceptable_expressions` 和 `required_concepts` **至少要有一个非空**。
两个都空的行会被判为没有判分依据。

## 02_可接受说法与难度.csv

| 列 | 填什么 |
| --- | --- |
| `item_id` / `task_type` / `target_word` | **系统填好的，不要改** |
| `acceptable_expressions` | 除目标词以外还算答对的说法，`；` 分隔。写着"（已有，不用改）"的就别动 |
| `difficulty_level` | 难度标注。写着"（已有，不用改）"的就别动 |

## 不在这两张表里的

还有一批缺口不是填表能解决的，需要 PI 做内容决策：源协议里尚未结构化的题位、
两张直接显示场景名称的刺激图是否去字重制、整体描述环节的完成条件、
公园人物成功分支对"小伙子"的接受口径冲突。
这些逐条列在 `scripts/content_freeze_report.py` 的输出里。

## 先说清楚：填完这两张表，"交付缺口 60" 这个数不会变

这两张表补的是**内容**缺口——判分标准、可接受说法、难度标注。
`content_freeze_report.py` 顶上那个"交付缺口合计 60"算的是另一件事：
**冻结的自动流程今天只覆盖"单要素·命名"这一类题位**，双要素题不管判分标准写得
多完整，自动执行仍然会被拒。那一条要靠扩自动流程协议来解，不是内容组的活。

填完这两张表的实际效果是：题库的告警从 82 条降到 12 条，
"缺判分标准的题位"从 30 个降到 0 个。这是真进展，只是它不体现在那个 60 上。

## 填完之后

`merge` 只把你写的内容拼回题库，**不会**把题库标成已冻结。
冻结仍然要内容负责人与 PI 两人独立复核后各自签字——这一步没有任何脚本能代替。
"""


def cmd_export(bank: content.ItemBank, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    rubrics = rubric_gaps(bank)
    namings = naming_gaps(bank)
    _write_csv(out_dir / RUBRIC_FILE, RUBRIC_COLUMNS, rubrics)
    _write_csv(out_dir / NAMING_FILE, NAMING_COLUMNS, namings)
    (out_dir / README_FILE).write_text(README_TEXT, encoding="utf-8")
    print(f"已导出到 {out_dir}")
    print(f"  {RUBRIC_FILE}  {len(rubrics)} 行（开放回答判分标准）")
    print(f"  {NAMING_FILE}  {len(namings)} 行（可接受说法与难度）")
    print(f"  {README_FILE}   填表说明")
    print("\n表里一个字都不是系统写的；填完先跑 check，全绿之前不要签字。")
    return 0


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------
def _read_csv(path: Path, columns: list[str]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [name for name in columns if name not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name} 缺少表头列：{missing}")
        return [{name: (row.get(name) or "").strip() for name in columns}
                for row in reader]


def check_rubric_rows(rows: list[dict[str, str]],
                      expected: list[dict[str, str]]) -> list[str]:
    problems: list[str] = []
    keys = {(row["item_id"], row["response_role"]) for row in expected}
    seen: set[tuple[str, str]] = set()
    for line, row in enumerate(rows, start=2):
        key = (row["item_id"], row["response_role"])
        where = f"{RUBRIC_FILE} 第 {line} 行（{row['item_id']}:{row['response_role']}）"
        if key not in keys:
            problems.append(f"{where}：对应不到题库里的题位，item_id/response_role 是不是被改过了")
            continue
        if key in seen:
            problems.append(f"{where}：同一个题位填了两次")
            continue
        seen.add(key)
        if not row["rubric_version"]:
            problems.append(f"{where}：rubric_version 还没填")
        if row["decision_policy"] not in content._RUBRIC_POLICIES:  # noqa: SLF001
            problems.append(
                f"{where}：decision_policy 只能填 "
                f"{sorted(content._RUBRIC_POLICIES)} 之一，现在是 "  # noqa: SLF001
                f"{row['decision_policy'] or '（空）'}")
        expressions = _split_list(row["acceptable_expressions"])
        concepts = _split_list(row["required_concepts"])
        if not expressions and not concepts:
            problems.append(
                f"{where}：acceptable_expressions 和 required_concepts 至少要填一个，"
                "两个都空系统没有任何判分依据")
        for field in ("cue_1", "cue_2", "tell_answer"):
            if not row[field]:
                problems.append(f"{where}：{field} 还没填")
    for key in sorted(keys - seen):
        problems.append(f"{RUBRIC_FILE}：{key[0]}:{key[1]} 这一行整行不见了")
    return problems


def check_naming_rows(rows: list[dict[str, str]],
                      expected: list[dict[str, str]]) -> list[str]:
    problems: list[str] = []
    wanted = {row["item_id"]: row for row in expected}
    seen: set[str] = set()
    for line, row in enumerate(rows, start=2):
        where = f"{NAMING_FILE} 第 {line} 行（{row['item_id']}）"
        target = wanted.get(row["item_id"])
        if target is None:
            problems.append(f"{where}：对应不到题库里的题位，item_id 是不是被改过了")
            continue
        if row["item_id"] in seen:
            problems.append(f"{where}：同一个题位填了两次")
            continue
        seen.add(row["item_id"])
        if not target["acceptable_expressions"] and not _split_list(
                row["acceptable_expressions"]):
            problems.append(f"{where}：acceptable_expressions 还没填")
        if (not target["difficulty_level"]
                and not (row["difficulty_level"]
                         and not _is_placeholder(row["difficulty_level"]))):
            problems.append(f"{where}：difficulty_level 还没填")
    for item_id in sorted(set(wanted) - seen):
        problems.append(f"{NAMING_FILE}：{item_id} 这一行整行不见了")
    return problems


def cmd_check(bank: content.ItemBank, work_dir: Path) -> int:
    try:
        rubric_rows = _read_csv(work_dir / RUBRIC_FILE, RUBRIC_COLUMNS)
        naming_rows = _read_csv(work_dir / NAMING_FILE, NAMING_COLUMNS)
    except (OSError, ValueError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    problems = (check_rubric_rows(rubric_rows, rubric_gaps(bank))
                + check_naming_rows(naming_rows, naming_gaps(bank)))
    if not problems:
        print(f"✓ 两张表都填完了：判分标准 {len(rubric_rows)} 行、"
              f"可接受说法与难度 {len(naming_rows)} 行")
        print("下一步：merge 拼回题库。注意 merge 不会把题库标成已冻结，"
              "冻结仍然要两人签字。")
        return 0
    print(f"还有 {len(problems)} 处没填好：\n")
    for problem in problems:
        print(f"  - {problem}")
    return 1


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------
FROZEN_META_KEYS = (
    "qc_status", "draft_revision", "item_bank_version_id",
    "source_document_sha256", "source_normalized_text_sha256",
    "content_schema_version",
)


def merge_into_bank(raw: dict, rubric_rows: list[dict[str, str]],
                    naming_rows: list[dict[str, str]]) -> dict:
    merged = json.loads(json.dumps(raw, ensure_ascii=False))
    by_id: dict[str, dict] = {}
    for group in ("single_element", "double_element", "multi_element"):
        for item in merged.get(group) or []:
            by_id[str(item.get("item_id"))] = item

    for row in rubric_rows:
        item = by_id[row["item_id"]]
        rubrics = item.setdefault("operational_rubrics", {})
        rubrics[row["response_role"]] = {
            "rubric_version": row["rubric_version"],
            "decision_policy": row["decision_policy"],
            "acceptable_expressions": _split_list(row["acceptable_expressions"]),
            "required_concepts": _split_list(row["required_concepts"]),
            "cues": {"1": row["cue_1"], "2": row["cue_2"]},
            "tell_answer": row["tell_answer"],
        }

    for row in naming_rows:
        item = by_id[row["item_id"]]
        expressions = _split_list(row["acceptable_expressions"])
        # 只有单要素题的模式里有这一格；写到别的题型上会让整份题库结构不合法。
        if (expressions and row["task_type"] == "单要素"
                and not item.get("acceptable_expressions")):
            item["acceptable_expressions"] = expressions
        level = row["difficulty_level"].strip()
        if (level and not _is_placeholder(level)
                and not item.get("difficulty_level")):
            item["difficulty_level"] = level

    # 冻结口径与来源指纹一律不动：填表 ≠ 冻结。
    for key in FROZEN_META_KEYS:
        if key in raw:
            merged[key] = raw[key]
    return merged


def cmd_merge(bank: content.ItemBank, bank_path: Path,
              work_dir: Path, out_path: Path) -> int:
    if cmd_check(bank, work_dir) != 0:
        print("\n✗ 表还没填完，拒绝合并。", file=sys.stderr)
        return 1
    if out_path.exists():
        print(f"✗ {out_path} 已存在，拒绝覆盖；换一个 --out", file=sys.stderr)
        return 2
    raw = json.loads(bank_path.read_text(encoding="utf-8"))
    merged = merge_into_bank(
        raw,
        _read_csv(work_dir / RUBRIC_FILE, RUBRIC_COLUMNS),
        _read_csv(work_dir / NAMING_FILE, NAMING_COLUMNS),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    staged = out_path.with_suffix(out_path.suffix + ".staged")
    staged.write_text(json.dumps(merged, ensure_ascii=False, indent=1) + "\n",
                      encoding="utf-8")
    # 先校验再改名：合并出来的题库读不了时，别在目标路径上留一份半成品，
    # 否则下一个人会以为那就是可以用的题库。
    try:
        reloaded = content.load_item_bank(staged)
    except Exception as exc:  # noqa: BLE001 —— 结构不合法要如实报，不留残件
        staged.unlink(missing_ok=True)
        print(f"\n✗ 合并出来的题库读不了，已丢弃：{exc}", file=sys.stderr)
        return 1
    staged.rename(out_path)
    readiness = content.content_readiness(reloaded)
    print(f"\n✓ 已写出 {out_path}")
    print(f"  qc_status 仍是 {reloaded.qc_status}（merge 不碰它，填表不等于冻结）")
    before = content.content_readiness(bank)
    print(f"  题库告警：{len(before['warnings'])} → {len(readiness['warnings'])}")
    print(f"  缺判分标准的题位：{len(content.unsupported_operational_rubrics(bank))}"
          f" → {len(content.unsupported_operational_rubrics(reloaded))}")
    print(f"  ready_for_research = {readiness['ready_for_research']}"
          "（qc_status 还是 draft，本来就该是 False）")
    if not readiness["operational_autopilot_ready"]:
        print("  自动执行仍未就绪：冻结的自动流程只覆盖「单要素·命名」，"
              "双要素题不管判分标准多完整都跑不了——那一条不是内容组能解的。")
    if readiness["errors"]:
        print(f"\n✗ 合并后的题库有 {len(readiness['errors'])} 处结构错误：",
              file=sys.stderr)
        for error in readiness["errors"][:20]:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("\n下一步是人的事：内容负责人与 PI 两人独立复核后各自签字，才谈得上冻结。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--bank", type=Path,
                        default=content.CONTENT_DIR / "item_bank_v1.json")
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="按当前题库导出空白表")
    export.add_argument("out_dir", type=Path)
    check = sub.add_parser("check", help="校验填好的表")
    check.add_argument("work_dir", type=Path)
    merge = sub.add_parser("merge", help="把填好的内容拼回题库")
    merge.add_argument("work_dir", type=Path)
    merge.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        bank = content.load_item_bank(args.bank)
    except Exception as exc:  # noqa: BLE001 —— 读不了内容文件就没法继续
        print(f"✗ 读不了题库 {args.bank}：{exc}", file=sys.stderr)
        return 2

    if args.command == "export":
        return cmd_export(bank, args.out_dir)
    if args.command == "check":
        return cmd_check(bank, args.work_dir)
    return cmd_merge(bank, args.bank, args.work_dir, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
