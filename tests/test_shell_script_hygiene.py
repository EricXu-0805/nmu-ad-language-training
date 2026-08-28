"""运维 shell 脚本里那两类"只在故障路径上才炸"的写法。

两条都不是假想：

  - `$rc。` —— 裸变量后面紧跟全角标点。`c99d3ad` 修过一次（全角逗号），
    2026-08-14 又在 `install-macos-offsite-pull.sh` 里发现第二处（全角句号）。
    后果特别坏：它在 `set -u` 下把**告警分支**变成 "unbound variable" 崩溃，
    于是拉取失败时既没拉到、也没告警——正好是那个脚本当初要解决的问题。
  - 生成出来的脚本本身语法不合法。模板是拿 heredoc 拼的，改坏了要到
    launchd 真跑那一刻才知道。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SHELL_SCRIPTS = sorted(SCRIPTS.glob("*.sh"))

# 裸 $var 或 \$var 紧跟一个非 ASCII 字符。${var} 与 "$var" 后跟空白都不算。
BARE_VAR_BEFORE_WIDE = re.compile(r"\$\{?\\?\$?[A-Za-z_][A-Za-z0-9_]*(?![A-Za-z0-9_}])[^\x00-\x7F]")


def test_there_are_shell_scripts_to_check():
    assert SHELL_SCRIPTS, "scripts/ 下一个 .sh 都没有，这套断言就成了空转"


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_no_bare_variable_immediately_before_a_full_width_character(script: Path):
    offenders = []
    for lineno, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
        for match in BARE_VAR_BEFORE_WIDE.finditer(line):
            offenders.append(f"{script.name}:{lineno}: {match.group(0)!r}")
    assert not offenders, (
        "裸变量后面紧跟全角字符，shell 会把变量名连着标点一起解析；"
        "set -u 下这会在故障路径上炸成 unbound variable。改成 ${var}。\n"
        + "\n".join(offenders))


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_script_itself_parses(script: Path):
    result = subprocess.run(["bash", "-n", str(script)],
                            capture_output=True, text=True)
    assert result.returncode == 0, f"{script.name} 语法不合法：{result.stderr.strip()}"


def test_the_generated_offsite_runner_also_parses():
    """安装器用 heredoc 拼出 run-pull.sh，模板改坏了到真跑才发现。"""
    installer = SCRIPTS / "install-macos-offsite-pull.sh"
    text = installer.read_text(encoding="utf-8")
    start = text.index('cat > "$ROOT/run-pull.sh"')
    body = text[text.index("\n", start) + 1:]
    body = body[:body.index("\nEOF\n") + 1]
    # heredoc 未加引号：反斜杠只在 \$ \` \\ 之前有意义，其余原样保留。
    # 只还原 \$ 是不够的——那样 `printf '%s\\n'` 会被当成两个字符留在这里，
    # 与真正写到盘上的 `printf '%s\\\\n'` 不是同一份文本。
    generated = re.sub(r"\\([$`\\])", r"\1", body)
    result = subprocess.run(["bash", "-n"], input=generated,
                            capture_output=True, text=True)
    assert result.returncode == 0, (
        f"安装器生成出来的 run-pull.sh 语法不合法：{result.stderr.strip()}")
    for match in BARE_VAR_BEFORE_WIDE.finditer(generated):
        pytest.fail(f"生成出来的 run-pull.sh 里有裸变量接全角字符：{match.group(0)!r}")
    # 告警正文不能靠「第一条 FAIL」——盘上常驻若干份等人工处置的 legacy 快照，
    # 每轮都各写一行同样的 code，永远占着第一条，真故障被挤到看不见的地方。
    assert "grep -m1 ' FAIL '" not in generated, (
        "run-pull.sh 又退回「只端第一条 FAIL」了；稳态失败会永远占住那个位置")
    assert "by_code=" in generated and "examples=" in generated, (
        "告警正文要按 FAIL 的 code 分组并各给一条样例，新出现的 code 才不会被淹掉")


def test_the_verifier_fingerprint_records_the_installed_copy_not_the_repo_file():
    """`verifier.sha256` 必须量**装好的那份**，不是仓库里那份。

    两者在安装当刻字节相同，所以第一列的值一样；差别在第二列的路径。写成仓库
    路径会有一个具体的坏后果：这个文件是 `shasum -c` 格式，而任何人真去
    `shasum -c verifier.sha256` 时，校验的是**仓库里那份**——仓库一往前走就
    FAILED，而那句 FAILED 跟"异地这份有没有被改过"毫无关系。

    2026-08-17 实测：仓库已前进三个迁移头，`shasum -c` 当场报 FAILED，
    而真正要守的不变量（异地副本 == 生产部署的那份）其实是成立的。
    偏偏 `RELEASE_STATE.md` 让人在迁移窗口里核对这个文件——那正是最不该
    出现假警报的时刻。
    """
    installer = SCRIPTS / "install-macos-offsite-pull.sh"
    text = installer.read_text(encoding="utf-8")
    # 注释行要排除：解释这条规则的注释里同样出现 shasum 与 verifier.sha256，
    # 第一版这个选择器就选中了注释，于是打完补丁测试照红。
    line = next(row for row in text.splitlines()
                if "verifier.sha256" in row and "shasum" in row
                and not row.lstrip().startswith("#"))
    assert '"$ROOT/runtime/scripts/verify_backup_snapshot.py"' in line, (
        "verifier.sha256 应当量装好的那份副本；量仓库那份会让 shasum -c 校验"
        f"一个一直在动的目标。当前那行是：{line.strip()}")


# ---------------------------------------------------------------------------
# 前端测试清单
# ---------------------------------------------------------------------------
# web/package.json 的 test/pretest 是**显式文件清单**而不是 glob。
# 2026-08-16 实测：新写的 qualityReleaseContract.test.ts 忘了加进去，
# `npm test` 全绿而那 16 条断言一次都没跑过——和"断言在空转"是同一个病，
# 只是换到了运行器这一层。

WEB = Path(__file__).resolve().parents[1] / "web"


def test_every_frontend_test_file_is_actually_in_the_run_list():
    package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    listed = set()
    for script in ("test", "pretest"):
        listed |= {
            part for part in package["scripts"][script].split()
            if part.endswith((".test.ts", ".test.mjs"))
        }
    on_disk = {
        str(path.relative_to(WEB))
        for pattern in ("src/**/*.test.ts", "src/**/*.test.mjs",
                        "scripts/*.test.mjs")
        for path in WEB.glob(pattern)
    }

    # 集合差，不是精确相等——清单里多列一个已删除的文件是另一类问题，
    # 由 node --test 自己报错，不该让这条断言跟着红。
    assert on_disk - listed == set(), (
        "这些前端测试文件存在但不在 npm test/pretest 清单里，等于没写：\n"
        + "\n".join(sorted(on_disk - listed)))


# ---------------------------------------------------------------------------
# 只有测试在调用的导出函数
# ---------------------------------------------------------------------------
# 2026-08-27 扫出 7 个：最典型的是 formalAssessment.ts 里那三个闸，
# 测试断言「正式测评进行中前端会拦住切换受试者」，而没有任何屏幕调用它们——
# 测试给的是假的安全感。这不等于都该删：有的是漏接线，有的是真死代码，
# 逐个拍板，别一刀切。白名单里每一条都要写清为什么留着。

# 已判定过的例外。删空这个字典之前请先看每条的理由。
_TEST_ONLY_EXPORT_ALLOWLIST = {
    # 2026-08-27 立案，本轮不处置——判断要动屏上交互或要确认真死，都不该顺手做。
    # 三个正式测评闸：测试断言「正式测评进行中前端会拦住切换受试者/收尾」，
    # 而没有任何屏调用它们。看名字都该接在 SessionCreateScreen 与收尾屏上，
    # 属于**漏接线**而不是死代码；接线要动那两屏的交互，单独一轮做。
    # ⚠️ 在接上之前，别把这三条测试当成「切换受试者有人拦」的证据。
    "assessmentEventAllowsSwitch": "漏接线，待接 SessionCreateScreen",
    "assessmentEventAllowsCloseout": "漏接线，待接收尾屏",
    "assessmentWorkflowAllowsPatientSwitch": "漏接线，待接 SessionCreateScreen",
    "operationalReadinessPolicy": "漏接线，待接今日队列屏",
    "sessionCreationPolicy": "漏接线，待接 SessionCreateScreen",
    # 这两个更像真死代码，删之前要确认没有别的入口在用（含动态取名）。
    "nextFeedbackSequence": "疑似真死代码，待确认后删",
    "clearPatientPauseOutboxIfMatches": "疑似真死代码，待确认后删",
}


def test_no_new_frontend_export_is_called_only_by_its_own_test():
    import re as _re
    src_root = WEB / "src"
    sources = [p for p in src_root.rglob("*.ts") if ".test." not in p.name]
    sources += [p for p in src_root.rglob("*.tsx") if ".test." not in p.name]
    tests = [p for p in src_root.rglob("*.test.ts")]
    tests += [p for p in src_root.rglob("*.test.tsx")]
    prod_text = "\n".join(p.read_text(encoding="utf-8") for p in sources)
    test_text = "\n".join(p.read_text(encoding="utf-8") for p in tests)

    offenders = []
    for path in sources:
        for name in _re.findall(r"^export function ([A-Za-z0-9_]+)",
                                path.read_text(encoding="utf-8"), _re.M):
            if name in _TEST_ONLY_EXPORT_ALLOWLIST:
                continue
            uses = len(_re.findall(rf"\b{_re.escape(name)}\b", prod_text))
            if uses <= 1 and _re.search(rf"\b{_re.escape(name)}\b", test_text):
                offenders.append(f"{path.relative_to(WEB)}::{name}")
    assert not offenders, (
        "这些导出函数只有测试在调用，生产代码一处也没接——测试给的是假的安全感：\n"
        + "\n".join(sorted(offenders))
        + "\n要么接上，要么删掉；确实要暂留就加进 _TEST_ONLY_EXPORT_ALLOWLIST 并写明理由。")
