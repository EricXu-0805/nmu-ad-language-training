"""GitHub CI 的每个 job，本机门要么覆盖它、要么显式承认覆盖不了。

2026-08-25 起 `image` job 连红三天（底座 openssl 的 CVE-2026-14456），而
`scripts/ci_gate.sh` 六关一直全绿——本机根本没有这一关。于是"门禁全过"被当成
了"CI 全绿"，带着一个红 job 就往生产推。

这条闸不能替你跑 trivy，它只做一件事：**CI 里多一个 job 而本机门既没接上、
也没在名单里承认，就红**。承认也算通过——但必须写出为什么本机跑不了。
"""
from __future__ import annotations

import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
GATE = ROOT / "scripts" / "ci_gate.sh"
PROTECTION = ROOT / ".github" / "branch-protection.json"

# job 名 → 本机对应的 ci_gate.sh 阶段；None = 本机跑不了，值是原因。
LOCAL_COVERAGE: dict[str, str | None] = {
    "backend": "--only backend（ruff + pytest）",
    "frontend": "--only frontend",
    "supply-chain": "--only supply-chain（SBOM + OSV + 锁自洽）",
    "image": None,   # 需要 docker 守护进程 + trivy；开发机上不常驻，别为它装
}
UNCOVERED_REASON = {
    "image": "容器构建+trivy 扫描，只在 GitHub CI 上跑",
}


def _job_names() -> list[str]:
    text = WORKFLOW.read_text(encoding="utf-8")
    body = text[text.index("\njobs:") + len("\njobs:"):]
    return re.findall(r"^  ([A-Za-z][\w-]*):$", body, re.M)


def _job_blocks() -> dict[str, str]:
    text = WORKFLOW.read_text(encoding="utf-8")
    body = text[text.index("\njobs:") + len("\njobs:"):]
    parts = re.split(r"^  ([A-Za-z][\w-]*):$", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def test_every_ci_job_is_either_covered_locally_or_named_as_not():
    jobs = set(_job_names())
    assert jobs, "从 ci.yml 里一个 job 都没解析出来——这条断言此刻在空转"
    unknown = jobs - set(LOCAL_COVERAGE)
    assert not unknown, (
        f"CI 里多了 job {sorted(unknown)}，而本机门没接上也没承认。"
        "接进 scripts/ci_gate.sh，或者登记进 LOCAL_COVERAGE 并写清为什么跑不了。")
    stale = set(LOCAL_COVERAGE) - jobs
    assert not stale, f"LOCAL_COVERAGE 里这些 job 已经不在 ci.yml 里了：{sorted(stale)}"


def test_the_gate_script_says_out_loud_what_it_does_not_cover():
    """否则本机绿会被读成 CI 绿——这正是 2026-08-27 那次误判。

    只查「字符串在文件里出现过」是不够的：解释这条闸为什么存在的注释里就有这些字，
    删掉那行 `echo` 只留注释，那样的断言照样绿（2026-08-28 回退验活当场抓到）。
    所以只看**会真的打印出来的行**。
    """
    printed = [line.strip() for line in GATE.read_text(encoding="utf-8").splitlines()
               if line.strip().startswith("echo ")]
    assert printed, "ci_gate.sh 里一行 echo 都没有——这条断言此刻在空转"
    for job, reason in UNCOVERED_REASON.items():
        assert LOCAL_COVERAGE.get(job, "missing") is None, (
            f"{job} 登记成本机能跑，却又写了跑不了的理由，两处对不上")
        assert any(job in line and reason in line for line in printed), (
            f"ci_gate.sh 的汇总里没有一行会**打印**出 `{job}`（{reason}）——"
            "跑完看到六个 PASS 的人不会知道还有一关没跑；写在注释里不算")


def test_pr_runs_report_every_required_check_under_its_exact_name():
    """分支保护按检查名放行：PR run 少报一个名字，合并就永远等不到它。

    main 的 push 与每周定时会跳过 backend/frontend。job 级 `if` 先于矩阵展开求值，
    跳过时只剩一条名为 `backend` 的 skipped，没有 `backend (3.12)`/`(3.14)`。这不碍事：
    必需检查只在更新 main 的那一刻核对被推的 SHA，那时 main 的 push run 还没开始。
    PR run 却必须原样报出这五个名字；被跳过的 job 又会被分支保护当成通过，
    所以 job 级 `if` 只许写成「跳过这些事件」，而且永远不许跳过 pull_request。
    """
    protection = json.loads(PROTECTION.read_text(encoding="utf-8"))
    required = {check["context"] for check in protection["required_status_checks"]["checks"]}
    reported = set()
    for job, block in _job_blocks().items():
        assert not re.search(r"^    name:", block, re.M), f"{job} 设了 job 级 name，检查名会跟着变"
        for condition in re.findall(r"^    if: (.+)$", block, re.M):
            assert re.fullmatch(
                r"github\.event_name != '[a-z_]+'(?: && github\.event_name != '[a-z_]+')*",
                condition), f"{job} 的 job 级 if 不是「跳过这些事件」的写法：{condition}"
            assert "'pull_request'" not in condition, f"{job} 在 PR 上被跳过，会被当成通过"
        matrix = re.search(r"^        python: (\[.+\])$", block, re.M)
        if matrix:
            reported |= {f"{job} ({version})" for version in json.loads(matrix.group(1))}
        else:
            reported.add(job)
    assert reported == required
