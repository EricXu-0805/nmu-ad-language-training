from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import sys

_SPEC = importlib.util.spec_from_file_location(
    "preflight_check",
    Path(__file__).resolve().parents[1] / "scripts" / "preflight_check.py")
assert _SPEC is not None and _SPEC.loader is not None
preflight = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(preflight)


def _db(tmp_path: Path, revision: str | None) -> Path:
    path = tmp_path / "app.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
    if revision is not None:
        connection.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
    connection.commit()
    connection.close()
    return path


def test_matching_head_passes(tmp_path):
    check = preflight.check_migration_head(_db(tmp_path, preflight._code_head()))

    assert check.ok is True


def test_lagging_database_head_fails_with_both_values(tmp_path):
    # 上线只 rsync 代码、忘了 alembic upgrade 就是这个状态：服务起得来，
    # 结构不对，等到写数据才炸。
    check = preflight.check_migration_head(_db(tmp_path, "f9b2d6e4a801"))

    assert check.ok is False
    assert preflight._code_head() in check.detail
    assert "f9b2d6e4a801" in check.detail


def test_missing_database_fails(tmp_path):
    check = preflight.check_migration_head(tmp_path / "nope.db")

    assert check.ok is False
    assert "不存在" in check.detail


def _fetcher(status_by_path: dict[str, int], headers: dict[str, str] | None = None):
    complete = {name: "x" for name in preflight.REQUIRED_HEADERS}
    if headers is not None:
        complete = headers

    def fetch(url: str, timeout: float):
        path = url.split("://", 1)[1].split("/", 1)[1]
        return status_by_path.get("/" + path, 500), complete

    return fetch


def _healthy_paths() -> dict[str, int]:
    paths = {"/health": 200}
    paths.update({path: 404 for path in preflight.REDLINE_PATHS})
    paths.update({path: 401 for path in preflight.PROTECTED_PATHS})
    return paths


def test_healthy_public_surface_passes_every_check():
    checks = preflight.check_public_surface("https://example.invalid",
                                            fetch=_fetcher(_healthy_paths()))

    assert [c.ok for c in checks] == [True, True, True, True]


def test_a_reachable_answer_bundle_fails_the_redline_check():
    paths = _healthy_paths()
    paths["/content/item_bank_v1.json"] = 200

    checks = preflight.check_public_surface("https://example.invalid",
                                            fetch=_fetcher(paths))

    redline = next(c for c in checks if c.name == "红线 404")
    assert redline.ok is False
    assert "item_bank_v1.json=200" in redline.detail


def test_an_unauthenticated_patient_list_fails_the_auth_check():
    paths = _healthy_paths()
    paths["/patients"] = 200

    checks = preflight.check_public_surface("https://example.invalid",
                                            fetch=_fetcher(paths))

    protected = next(c for c in checks if c.name == "受保护路由要登录")
    assert protected.ok is False
    assert "/patients=200" in protected.detail


def test_a_missing_security_header_is_named():
    headers = {name: "x" for name in preflight.REQUIRED_HEADERS
               if name != "strict-transport-security"}

    checks = preflight.check_public_surface(
        "https://example.invalid", fetch=_fetcher(_healthy_paths(), headers))

    header_check = next(c for c in checks if c.name == "安全头")
    assert header_check.ok is False
    assert "strict-transport-security" in header_check.detail


def test_unreachable_host_is_one_failure_not_four():
    def boom(url: str, timeout: float):
        raise OSError("connection refused")

    checks = preflight.check_public_surface("https://example.invalid", fetch=boom)

    assert len(checks) == 1
    assert checks[0].ok is False


def _lock(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "lock.txt"
    path.write_text(
        "#    uv pip compile --python-version 3.10 --generate-hashes\n" + body,
        encoding="utf-8")
    return path


def test_supply_chain_group_passes_on_the_interpreter_that_runs_it(tmp_path):
    """拿当前解释器真装着的东西当锁，就该对上——证明这条不是恒假。"""
    import importlib.metadata as metadata

    lines = []
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            lines.append(f"{name}=={dist.version} \\\n    --hash=sha256:x\n")
    check = preflight.check_supply_chain(_lock(tmp_path, "".join(lines)), sys.executable)

    assert check.ok is True, check.detail


def test_supply_chain_group_names_what_went_wrong(tmp_path):
    check = preflight.check_supply_chain(
        _lock(tmp_path, "alembic==1.18.5 \\\n    --hash=sha256:x\n"), sys.executable)

    assert check.ok is False
    assert "unlocked" in check.detail                       # 装了一堆锁里没有的
    assert "supply_chain_check.py" in check.detail          # 告诉人去哪看明细


def test_supply_chain_group_fails_loudly_when_it_cannot_look(tmp_path):
    check = preflight.check_supply_chain(tmp_path / "no-such-lock.txt", sys.executable)

    assert check.ok is False and "对不成账" in check.detail


def test_skipped_groups_do_not_fail_the_run(capsys):
    code = preflight.main([])

    assert code == 0
    assert capsys.readouterr().out.count("[SKIP]") == 4


def test_any_hard_failure_makes_the_run_nonzero(tmp_path, capsys):
    code = preflight.main(["--db", str(_db(tmp_path, "wrong-head"))])

    assert code == 1
    assert "[FAIL]" in capsys.readouterr().out
