"""只读部署树校验器的判据测试。

这个校验器存在的理由写在 `scripts/verify_deployed_tree.py` 的模块注释里：
2026-08-17 那次只读复核发现，机器上唯一记录"跑的是哪个版本"的
`last-deploy.state` 比现实旧六天。真判据只能是逐文件指纹。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import verify_deployed_tree as verifier  # noqa: E402


EMPTY = verifier.EMPTY_SHA256
GOOD = "a" * 64


def _manifest(*lines: str) -> str:
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# 清单解析：宁可拒绝，也不要把"没读到"当成"读到了空文件"
# --------------------------------------------------------------------------

def test_an_empty_manifest_is_refused_not_treated_as_zero_differences():
    """空清单最危险：逐行比对零条，看起来就是"全都一致"。"""
    with pytest.raises(verifier.VerifyError) as caught:
        verifier.parse_manifest("")
    assert caught.value.code == "manifest_empty"


def test_a_hash_of_nothing_is_refused_by_name():
    """★ 这条测试是为一个真犯过的错写的。

    2026-08-17 我把一条产生空输出的命令的结果当成了文件指纹，得出
    "生产代码不对应任何提交"的错误结论。空串的 sha256 是一个**定值**，
    出现它就说明这一行量的是"什么都没有"，不是量到了一个空文件。
    """
    with pytest.raises(verifier.VerifyError) as caught:
        verifier.parse_manifest(_manifest(f"app/main.py {EMPTY}"))
    assert caught.value.code == "manifest_hash_of_nothing"


def test_a_duplicate_path_is_refused():
    with pytest.raises(verifier.VerifyError) as caught:
        verifier.parse_manifest(_manifest(
            f"app/main.py {GOOD}", f"app/main.py {'b' * 64}"))
    assert caught.value.code == "manifest_duplicate_path"


@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside.py", "app/../../x"])
def test_paths_that_escape_the_tree_are_refused(bad):
    with pytest.raises(verifier.VerifyError) as caught:
        verifier.parse_manifest(_manifest(f"{bad} {GOOD}"))
    assert caught.value.code == "manifest_path_unsafe"


@pytest.mark.parametrize("bad", ["short", "z" * 64, "a" * 63, "a" * 65])
def test_a_malformed_hash_is_refused(bad):
    with pytest.raises(verifier.VerifyError) as caught:
        verifier.parse_manifest(_manifest(f"app/main.py {bad}"))
    assert caught.value.code == "manifest_hash_malformed"


def test_a_well_formed_manifest_parses():
    parsed = verifier.parse_manifest(_manifest(
        f"app/main.py {GOOD}", f"scripts/x.py {'b' * 64}"))
    assert parsed == {"app/main.py": GOOD, "scripts/x.py": "b" * 64}


# --------------------------------------------------------------------------
# 与某个提交比对
# --------------------------------------------------------------------------

def _git(tmp_path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=tmp_path, check=True,
        stdout=subprocess.PIPE, text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "app" / "other.py").write_text("print(2)\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return tmp_path


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def test_an_identical_tree_reports_a_clean_match(repo):
    report = verifier.compare(
        {"app/main.py": _sha("print(1)\n"), "app/other.py": _sha("print(2)\n")},
        revision="HEAD", repo_root=repo)
    assert report.identical == 2
    assert report.differing == [] and report.absent_in_revision == []
    assert report.matches is True


def test_one_changed_byte_is_reported_as_differing(repo):
    report = verifier.compare(
        {"app/main.py": _sha("print(999)\n"), "app/other.py": _sha("print(2)\n")},
        revision="HEAD", repo_root=repo)
    assert report.differing == ["app/main.py"]
    assert report.matches is False


def test_a_file_the_revision_never_had_is_reported_separately(repo):
    """部署树上多出来的文件不能与"内容不同"混为一谈——它可能是手改上去的。"""
    report = verifier.compare(
        {"app/main.py": _sha("print(1)\n"), "app/ghost.py": GOOD},
        revision="HEAD", repo_root=repo)
    assert report.absent_in_revision == ["app/ghost.py"]
    assert report.matches is False


def test_an_unknown_revision_is_refused_rather_than_reported_as_all_absent(repo):
    """未知提交必须报错。否则每个文件都"在该提交里不存在"，读起来像发现了漂移。"""
    with pytest.raises(verifier.VerifyError) as caught:
        verifier.compare({"app/main.py": GOOD},
                         revision="deadbeef", repo_root=repo)
    assert caught.value.code == "revision_unknown"


# --------------------------------------------------------------------------
# 这个工具永远不许改任何东西
# --------------------------------------------------------------------------

def _called_names(source: str) -> set[str]:
    """脚本里**真正被调用**的函数名，注释与文档串不算。

    第一版这条测试是对源码做子串匹配的，结果被自己的一句注释
    「已经取消了源码目录的 rsync/覆盖发布路径」判红——散文里出现一个词，
    不等于代码干了那件事。改成走 AST，量的才是行为。
    """
    import ast

    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            parts = []
            while isinstance(target, ast.Attribute):
                parts.append(target.attr)
                target = target.value
            if isinstance(target, ast.Name):
                parts.append(target.id)
            names.add(".".join(reversed(parts)))
    return names


def test_the_verifier_never_calls_anything_that_writes():
    """只读是这个脚本存在的前提：它绝不能长出第二条发布路径。"""
    called = _called_names(Path(verifier.__file__).read_text(encoding="utf-8"))
    forbidden = {
        "open", "shutil.copy", "shutil.copy2", "shutil.move", "shutil.rmtree",
        "os.remove", "os.unlink", "os.rename", "os.makedirs", "os.rmdir",
        "subprocess.Popen", "subprocess.call", "subprocess.check_call",
    }
    assert called & forbidden == set(), f"只读校验器调用了会写的东西：{called & forbidden}"
    for name in called:
        assert not name.endswith((".write_text", ".write_bytes", ".mkdir",
                                  ".unlink", ".touch")), f"不该调用 {name}"


def test_the_only_subprocess_it_runs_is_git():
    """真去看每一个 subprocess.run 的第一个实参，而不是相信注释。"""
    import ast

    source = Path(verifier.__file__).read_text(encoding="utf-8")
    runs = [node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"]
    assert runs, "一个 subprocess.run 都没有——这条测试就成了空转"
    for call in runs:
        first = call.args[0]
        assert isinstance(first, ast.List), "命令必须是字面量列表，不能拼字符串"
        assert isinstance(first.elts[0], ast.Constant) and first.elts[0].value == "git"
        keywords = {kw.arg for kw in call.keywords}
        assert "check" in keywords, "必须显式 check，否则失败会被当成空输出"


def test_the_allowed_git_subcommands_are_all_read_only():
    assert set(verifier._ALLOWED_GIT_SUBCOMMANDS) <= {
        "cat-file", "show", "rev-parse"}


def test_an_uppercase_digest_is_accepted_and_normalised():
    """`sha256sum` 给小写，别的工具可能给大写。大小写不该成为拒绝理由。"""
    parsed = verifier.parse_manifest(f"app/main.py {'A' * 64}\n")
    assert parsed == {"app/main.py": "a" * 64}
