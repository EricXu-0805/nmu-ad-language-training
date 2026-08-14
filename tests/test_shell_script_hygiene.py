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
    # heredoc 未加引号，转义的 \$ 在生成时会还原成 $。
    generated = body.replace("\\$", "$")
    result = subprocess.run(["bash", "-n"], input=generated,
                            capture_output=True, text=True)
    assert result.returncode == 0, (
        f"安装器生成出来的 run-pull.sh 语法不合法：{result.stderr.strip()}")
    for match in BARE_VAR_BEFORE_WIDE.finditer(generated):
        pytest.fail(f"生成出来的 run-pull.sh 里有裸变量接全角字符：{match.group(0)!r}")
