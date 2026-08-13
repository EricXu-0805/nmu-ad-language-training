#!/usr/bin/env python3
"""告诉 PI/内容组："你交的这三份文件，放进去到底能不能用"。

工程侧已经把正式量表的整条链做完了，剩下的全部卡在三份**只能由 PI 冻结**的
文件上。问题是那三份文件的合同写在代码里，PI 没法自己判断填对了没有——只能
放进去、重启、看会不会 503，出了错还看不出是哪一项。

这个脚本用**真实的装载器**（不是另写一份宽松的校验）跑一遍，逐项报告：
文件在不在、能不能装、装完之后就绪面还差什么。因为用的是同一套代码，
它说"能过"就是真能过。

工程侧不生成、不猜、不代填任何内容——那是临床与版权决策。

退出码：0 = 三件套齐备且就绪；1 = 还差东西（正常状态）；2 = 文件存在但结构坏了。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import (  # noqa: E402
    assessment_bundles,
    assessment_workflow_policy,
    scale_protocol,
)


PIECES = (
    ("量表 manifest（PI 冻结事实的载体）",
     scale_protocol.SCALE_PROTOCOL_MANIFEST_FILE,
     "两类正式结局工具各自的工具名、版本、语言/版式、许可、可否数字化呈现、"
     "可否口头施测、可否自动计分、分值范围与方向、施测者资质、时间窗"),
    ("定义包索引（逐题内容与计分规则）",
     f"{assessment_bundles.ASSESSMENT_BUNDLE_DIR}/"
     f"{assessment_bundles.ASSESSMENT_BUNDLE_INDEX_FILE}",
     "每题必须带词；索引条目必须带整包的 content_sha256——线上契约有意不携带"
     "词表，所以语义载荷只能靠这个字节钉兜住"),
    ("冻结工作流政策（谁能在什么时候做什么）",
     assessment_workflow_policy.ASSESSMENT_WORKFLOW_POLICY_FILE,
     "施测时间窗、延期是否需要管理员批准与上限、是否只允许被分配的评估员本人执行"),
)


def _present(content_dir: Path, relative: str) -> bool:
    return (content_dir / relative).exists()


def inspect(content_dir: Path) -> dict[str, object]:
    files = [
        {"title": title, "path": relative, "present": _present(content_dir, relative),
         "what_it_must_contain": needs}
        for title, relative, needs in PIECES
    ]

    loaders: list[dict[str, object]] = []
    try:
        policy = assessment_workflow_policy.load_workflow_policy(content_dir)
        loaders.append({
            "piece": assessment_workflow_policy.ASSESSMENT_WORKFLOW_POLICY_FILE,
            "loaded": policy is not None,
            "detail": "文件不存在（尚未交付，不是错误）" if policy is None else "结构合法",
        })
    except Exception as error:  # noqa: BLE001 — 坏档要原样报出来，不能吞
        loaders.append({
            "piece": assessment_workflow_policy.ASSESSMENT_WORKFLOW_POLICY_FILE,
            "loaded": False,
            "detail": f"文件存在但结构不合法：{type(error).__name__}",
        })

    readiness = scale_protocol.scale_protocol_readiness()
    return {
        "content_dir": str(content_dir),
        "files": files,
        "loaders": loaders,
        "readiness_status": readiness.get("status"),
        "ready_for_research": bool(readiness.get("ready_for_research")),
        "blocking_issues": readiness.get("blocking_issues"),
        "gates": {
            key: bool(readiness.get(key))
            for key in ("definition_ready", "definition_artifacts_ready",
                        "definition_artifact_enforcement_ready",
                        "workflow_ready", "workflow_policy_ready",
                        "workflow_policy_enforcement_ready",
                        "formal_result_contract_ready",
                        "instance_creation_enabled",
                        "automatic_scoring_enabled")
        },
    }


def render(report: dict[str, object]) -> str:
    lines = ["正式量表三件套检查", f"  内容目录 {report['content_dir']}", ""]
    files = report["files"]
    assert isinstance(files, list)
    for entry in files:
        mark = "✅ 已交付" if entry["present"] else "⛔ 缺席"
        lines.append(f"{mark}  {entry['title']}")
        lines.append(f"        文件：{entry['path']}")
        if not entry["present"]:
            lines.append(f"        需要包含：{entry['what_it_must_contain']}")
        lines.append("")

    loaders = report["loaders"]
    assert isinstance(loaders, list)
    for entry in loaders:
        lines.append(f"装载检查  {entry['piece']}：{entry['detail']}")
    lines.append("")

    lines.append(f"就绪状态  {report['readiness_status']}")
    lines.append(f"可用于真实受试者：{'是' if report['ready_for_research'] else '否'}")
    gates = report["gates"]
    assert isinstance(gates, dict)
    lines.append("逐道闸：")
    for key, ok in gates.items():
        lines.append(f"     {'✅' if ok else '⛔'} {key}")
    issues = report["blocking_issues"]
    if issues:
        # 按类别收成字段名清单——逐条倒出 90 个 dict 没人看得下去，
        # PI 真正需要的是"这张表还有哪几项没填"。
        grouped: dict[str, list[str]] = {}
        for issue in (issues if isinstance(issues, list) else [issues]):
            if isinstance(issue, dict):
                grouped.setdefault(
                    str(issue.get("category_key", "?")), []).append(
                        str(issue.get("field", issue.get("code", "?"))))
            else:
                grouped.setdefault("?", []).append(str(issue))
        lines.append("")
        lines.append("还没有形成可验证冻结事实的项（按表分组）：")
        for category, fields in grouped.items():
            lines.append(f"     [{category}] 共 {len(fields)} 项")
            for index in range(0, len(fields), 4):
                lines.append("         " + "、".join(fields[index:index + 4]))

    lines.append("")
    lines.append("说明：工程侧不生成、不猜、不代填任何内容——量表的工具名、版本、"
                 "许可、逐题内容与计分规则都是临床与版权决策。")
    lines.append("三份文件放进内容目录并重启服务后，正式评估录入与自动计分即开，"
                 "不需要改任何代码。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--content-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "content")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = inspect(args.content_dir)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(report))

    loaders = report["loaders"]
    assert isinstance(loaders, list)
    files = report["files"]
    assert isinstance(files, list)
    broken = any(
        entry["loaded"] is False and "结构不合法" in str(entry["detail"])
        for entry in loaders)
    if broken:
        return 2
    return 0 if report["ready_for_research"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
