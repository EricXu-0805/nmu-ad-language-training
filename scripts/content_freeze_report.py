#!/usr/bin/env python3
"""把"题库还差什么才能冻结"打成一份可以直接交给 PI / 内容组签字的清单。

运行时早就有 `content.content_readiness()` 在按合同 fail-closed，但它的输出是
给程序看的：82 条 warning 混在一起，谁该补哪一条看不出来。这个脚本只做一件
事——按**谁能动手**把阻断项分组，给出确切的题位标识和条数，让内容组知道要
补的是哪 30 条 rubric、哪 10 个题位，而不是"题库还是 draft"。

工程侧不得代填任何临床话术或判分规则，所以这里只统计和呈现，一个字不生成。

退出码：0 = 题库已满足真实受试者交付条件；1 = 仍有阻断项；2 = 内容文件读不了。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import content, runtime  # noqa: E402


CONTENT_TEAM = "PI / 内容组（工程侧不得代笔）"
ENGINEERING = "工程侧"



def delivery_gap(bank: content.ItemBank) -> dict[str, object]:
    """算出 README/DEPLOY 里那个「交付缺口」总数，别让它只活在措辞里。

    口径：**计划内跑不动的题位 + 源协议里还没结构化的题位**。
    "跑不动"的判据与自动驾驶实际用的那一条完全相同——冻结自动协议目前只覆盖
    单要素/命名，其余题位（双要素命名缺自动协议、开放回答缺 rubric）在开场时
    一律 fail-closed。

    这个数以前只写在六份文档的正文里，靠人手同步；任何一次内容交付都可能让
    它悄悄失真。现在它由题库自己算出来。
    """
    readiness = content.content_readiness(bank)
    unstructured = int(readiness["source_unstructured_position_count"])
    plan = runtime.build_session_plan(bank, 2, "正式训练")
    runnable = 0
    in_plan = 0
    breakdown: dict[str, int] = {}
    for item in plan.items:
        task_type = getattr(item.task_type, "value", item.task_type)
        for turn in item.turns:
            role = getattr(turn.response_role, "value", turn.response_role)
            in_plan += 1
            if task_type == "单要素" and role == "命名":
                runnable += 1
            else:
                breakdown[f"{task_type}:{role}"] = (
                    breakdown.get(f"{task_type}:{role}", 0) + 1)
    return {
        "in_plan_positions": in_plan,
        "runnable_by_frozen_automation": runnable,
        "in_plan_gaps": in_plan - runnable,
        "unstructured_source_positions": unstructured,
        "delivery_gap_total": (in_plan - runnable) + unstructured,
        "in_plan_gap_breakdown": dict(sorted(breakdown.items())),
    }


def collect(bank: content.ItemBank) -> dict[str, object]:
    readiness = content.content_readiness(bank)
    meta = bank.meta

    unstructured = readiness["source_unstructured_position_count"]
    rubrics = list(readiness["unsupported_operational_rubrics"])
    blockers = meta.get("source_unstructured_blockers")
    blockers = list(blockers) if isinstance(blockers, list) else []

    groups: list[dict[str, object]] = []

    if bank.qc_status != "frozen":
        groups.append({
            "owner": CONTENT_TEAM,
            "title": f"题库整体仍是 {bank.qc_status}，未完成双人冻结",
            "count": 1,
            "items": [f"item_bank_version_id={meta.get('item_bank_version_id')} "
                      f"draft_revision={meta.get('draft_revision')}"],
        })

    if unstructured:
        groups.append({
            "owner": CONTENT_TEAM,
            "title": "源协议题位尚未结构化，自动执行对这些题位一律 fail-closed",
            "count": unstructured,
            "items": [
                str(row.get("source_position_key"))
                for row in readiness["source_unstructured_positions"]
                if isinstance(row, dict)
            ],
        })

    if rubrics:
        groups.append({
            "owner": CONTENT_TEAM,
            "title": "题位缺可自动判分的 rubric（正确 / 部分 / 错误三档口径）",
            "count": len(rubrics),
            "items": rubrics,
        })

    if blockers:
        groups.append({
            "owner": CONTENT_TEAM,
            "title": "源脚本自身遗留的内容决策（题库里已登记为 blocker）",
            "count": len(blockers),
            "items": blockers,
        })

    if readiness["errors"]:
        groups.append({
            "owner": ENGINEERING,
            "title": "题库结构性错误（不是临床决策，工程侧可直接修）",
            "count": len(readiness["errors"]),
            "items": list(readiness["errors"]),
        })

    return {
        "delivery_gap": delivery_gap(bank),
        "item_bank_version_id": meta.get("item_bank_version_id"),
        "draft_revision": meta.get("draft_revision"),
        "qc_status": bank.qc_status,
        "source_protocol_position_count": readiness["source_protocol_position_count"],
        "ready_for_research": readiness["ready_for_research"],
        "operational_autopilot_ready": readiness["operational_autopilot_ready"],
        "blocking_groups": groups,
    }


def render(report: dict[str, object], *, max_items: int) -> str:
    gap = report["delivery_gap"]
    assert isinstance(gap, dict)
    lines = [
        "题库冻结阻断清单",
        f"  版本 {report['item_bank_version_id']}"
        f"  修订 {report['draft_revision']}"
        f"  qc_status={report['qc_status']}",
        f"  源协议题位总数 {report['source_protocol_position_count']}",
        f"  **交付缺口合计 {gap['delivery_gap_total']}**"
        f"（计划内跑不动 {gap['in_plan_gaps']}"
        f" + 源协议未结构化 {gap['unstructured_source_positions']}）",
        f"  计划内 {gap['in_plan_positions']} 个题位里，"
        f"冻结自动化能跑的只有 {gap['runnable_by_frozen_automation']} 个",
        f"  可用于真实受试者：{'是' if report['ready_for_research'] else '否'}",
        "",
    ]
    groups = report["blocking_groups"]
    assert isinstance(groups, list)
    if not groups:
        lines.append("没有阻断项。")
        return "\n".join(lines)

    for index, group in enumerate(groups, start=1):
        items = group["items"]
        assert isinstance(items, list)
        lines.append(f"{index}. [{group['owner']}] {group['title']}（{group['count']} 项）")
        for entry in items[:max_items]:
            lines.append(f"     - {entry}")
        if len(items) > max_items:
            lines.append(f"     … 另有 {len(items) - max_items} 项，用 --json 取全量")
        lines.append("")

    lines.append("签字栏（两人独立复核后各自签署，日期精确到日）：")
    lines.append("  内容负责人 ____________  日期 __________")
    lines.append("  PI        ____________  日期 __________")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank", type=Path,
                        default=content.CONTENT_DIR / "item_bank_v1.json")
    parser.add_argument("--json", action="store_true", help="输出机器可读全量")
    parser.add_argument("--max-items", type=int, default=12,
                        help="文本模式下每组最多列出几项")
    args = parser.parse_args(argv)

    try:
        bank = content.load_item_bank(args.bank)
    except Exception as error:  # noqa: BLE001 - 读不了内容就是读不了，原样报出来
        print(f"内容文件无法加载：{error}", file=sys.stderr)
        return 2

    report = collect(bank)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report, max_items=args.max_items))
    return 0 if report["ready_for_research"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
