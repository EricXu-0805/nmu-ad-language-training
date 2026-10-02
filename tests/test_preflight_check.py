from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

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


def _backup_app(tmp_path: Path) -> Path:
    app = tmp_path / "application"
    (app / "scripts").mkdir(parents=True)
    for relative in preflight.BACKUP_EXECUTABLES:
        path = app / relative
        # 此检查只能观察权限，不得为了验收而真正执行备份。
        path.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        path.chmod(0o750)
    return app


def test_backup_execution_accepts_owner_and_group_executable_scripts(tmp_path):
    app = _backup_app(tmp_path)

    assert preflight.check_backup_execution(app).ok is True


@pytest.mark.parametrize("relative", preflight.BACKUP_EXECUTABLES)
def test_backup_execution_rejects_640_for_each_dependency(tmp_path, relative):
    app = _backup_app(tmp_path)
    (app / relative).chmod(0o640)

    check = preflight.check_backup_execution(app)

    assert check.ok is False
    assert f"{relative}: not_executable mode=0640" in check.detail


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink"])
def test_backup_execution_rejects_missing_or_nonregular_script(tmp_path, kind):
    app = _backup_app(tmp_path)
    path = app / "scripts/vps-backup-daily.sh"
    path.unlink()
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        path.symlink_to(app / "scripts/backup.sh")

    assert preflight.check_backup_execution(app).ok is False


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


WAITING = ("NextElapseUSecRealtime=Fri 2026-10-02 10:10:00 UTC\nSubState=waiting\n"
           "UnitFileState=enabled\n")


def _systemctl(show: dict[str, str], installed: list[str] | None = None):
    """假 systemctl：list-unit-files 报出已装的定时器，show 按单元名回放属性。"""
    names = list(show) if installed is None else installed

    def run(argv, **_kwargs):
        if argv[:2] == ["systemctl", "list-unit-files"]:
            listing = "".join(f"{name} enabled enabled\n" for name in names)
            return SimpleNamespace(returncode=0, stdout=listing, stderr="")
        if argv[:2] == ["systemctl", "show"]:
            return SimpleNamespace(returncode=0, stdout=show[argv[2]], stderr="")
        raise AssertionError(f"没预料到的命令：{argv}")

    return run


def test_timer_check_passes_when_every_installed_timer_is_waiting():
    check = preflight.check_timers_scheduled(run=_systemctl({
        "nmu-backup.timer": WAITING, "nmu-health.timer": WAITING}))

    assert check.ok is True, check.detail
    assert "2 个" in check.detail


def test_an_elapsed_timer_is_named_although_it_still_shows_active():
    # 2026-09-30 重启后 nmu-health.timer 的原样：active，但再也不会触发。
    check = preflight.check_timers_scheduled(run=_systemctl({
        "nmu-backup.timer": WAITING,
        "nmu-health.timer": ("ActiveState=active\nNextElapseUSecRealtime=\n"
                             "SubState=elapsed\nUnitFileState=enabled\n"),
    }))

    assert check.ok is False
    assert "nmu-health.timer" in check.detail and "elapsed" in check.detail
    assert "nmu-backup.timer" not in check.detail


@pytest.mark.parametrize("shown", [
    "NextElapseUSecRealtime=\nSubState=waiting\nUnitFileState=enabled\n",
    "SubState=waiting\nUnitFileState=enabled\n",  # 空属性有的 systemctl 版本干脆不打印
])
def test_a_waiting_timer_without_a_next_elapse_is_dead(shown):
    check = preflight.check_timers_scheduled(run=_systemctl({
        "nmu-backup.timer": WAITING, "nmu-health.timer": shown}))

    assert check.ok is False
    assert "nmu-health.timer" in check.detail
    assert "nmu-backup.timer" not in check.detail


def test_a_disabled_timer_is_named_while_it_is_still_waiting():
    # 停用而没带 --now：此刻还排着，下次开机就不会再起来，也没有任何东西因此失败。
    check = preflight.check_timers_scheduled(run=_systemctl({
        "nmu-backup.timer": WAITING,
        "nmu-health.timer": WAITING.replace("=enabled", "=disabled"),
    }))

    assert check.ok is False
    assert "nmu-health.timer" in check.detail and "disabled" in check.detail
    assert "nmu-backup.timer" not in check.detail


def test_missing_systemctl_fails_the_timer_check():
    def run(argv, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "systemctl")

    check = preflight.check_timers_scheduled(run=run)

    assert check.ok is False
    assert "systemctl" in check.detail


def test_no_installed_timer_at_all_is_a_failure_not_a_pass():
    check = preflight.check_timers_scheduled(run=_systemctl({}, installed=[]))

    assert check.ok is False


def _touch(path: Path, mode: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    path.chmod(mode)
    return path


def _nmu_root(tmp_path: Path) -> Path:
    root = tmp_path / "opt-nmu"
    (root / "app").mkdir(parents=True)
    (root / "backups").mkdir()
    # 目录模式写死:导入 app.db 会把进程 umask 设成 077,全量跑时目录就成了 0700,
    # other 进不去,文件再 0644 也不算暴露。这里要的是 other 进得去的布局。
    for directory in (root, root / "app", root / "backups"):
        directory.chmod(0o755)
    return root


def test_sensitive_files_closed_to_other_pass(tmp_path):
    root = _nmu_root(tmp_path)
    _touch(root / "app-before-deploy-20260831-183746.tar.gz", 0o600)
    _touch(root / "ops-alert.env", 0o600)
    _touch(root / "app" / ".env", 0o640)
    _touch(root / "app" / "README.md", 0o644)  # 不在名单里，谁读都行

    check = preflight.check_sensitive_file_modes(root)

    assert check.ok is True, check.detail


@pytest.mark.parametrize("relative", [
    "app-before-deploy-20260831-183746.tar.gz", "app/release.tgz", "app/app.db",
    "app/.env", "app/.env.before-20260823-fpkey", "ops-alert.env",
])
def test_a_sensitive_file_readable_by_other_is_named(tmp_path, relative):
    root = _nmu_root(tmp_path)
    _touch(root / relative, 0o644)

    check = preflight.check_sensitive_file_modes(root)

    assert check.ok is False
    assert "1 个" in check.detail
    assert str(root / relative) in check.detail


def test_sensitive_file_check_reports_the_count_and_at_most_five_paths(tmp_path):
    root = _nmu_root(tmp_path)
    paths = [_touch(root / f"app-before-deploy-{index}.tar.gz", 0o604) for index in range(7)]

    check = preflight.check_sensitive_file_modes(root)

    assert check.ok is False
    assert "7 个" in check.detail
    assert sum(str(path) in check.detail for path in paths) == 5


def test_sensitive_file_check_looks_two_levels_deep_and_no_further(tmp_path):
    root = _nmu_root(tmp_path)
    _touch(root / "app" / "data" / "app.db", 0o644)  # 第三层：不在这条的范围里

    assert preflight.check_sensitive_file_modes(root).ok is True


def test_the_tracked_env_template_is_not_treated_as_a_secret(tmp_path):
    # 入库的模板，rsync -a 每次都按本机的 644 带过去；里面没有秘密。
    root = _nmu_root(tmp_path)
    _touch(root / "app" / ".env.example", 0o644)

    assert preflight.check_sensitive_file_modes(root).ok is True


def test_a_readable_file_inside_a_directory_other_cannot_enter_is_not_exposed(tmp_path):
    # 生产上旧 cron 留在 /opt/nmu/backups(0700)里的 app-*.db 是 0644：other 进不了目录，读不到。
    root = _nmu_root(tmp_path)
    _touch(root / "backups" / "app-2026-10-01.db", 0o644)
    (root / "backups").chmod(0o700)

    assert preflight.check_sensitive_file_modes(root).ok is True


def test_nothing_under_a_root_other_cannot_enter_is_exposed(tmp_path):
    root = _nmu_root(tmp_path)
    _touch(root / "app-before-deploy-20260831-183746.tar.gz", 0o644)
    root.chmod(0o750)
    try:
        assert preflight.check_sensitive_file_modes(root).ok is True
    finally:
        root.chmod(0o755)


def test_sensitive_file_check_fails_when_the_root_cannot_be_listed(tmp_path):
    check = preflight.check_sensitive_file_modes(tmp_path / "no-such-root")

    assert check.ok is False


def _etc(tmp_path: Path) -> Path:
    etc = tmp_path / "etc"
    for name in ("cron.d", "cron.daily", "cron.hourly", "cron.weekly"):
        (etc / name).mkdir(parents=True)
    return etc


def test_cron_without_nmu_entries_passes(tmp_path):
    etc = _etc(tmp_path)
    _touch(etc / "cron.d" / "e2scrub_all", 0o644)
    _touch(etc / "cron.daily" / "logrotate", 0o755)

    assert preflight.check_no_nmu_cron(etc).ok is True


@pytest.mark.parametrize("directory", ["cron.d", "cron.daily", "cron.hourly", "cron.weekly"])
def test_an_nmu_cron_entry_is_named(tmp_path, directory):
    etc = _etc(tmp_path)
    _touch(etc / directory / "nmu-backup", 0o755)

    check = preflight.check_no_nmu_cron(etc)

    assert check.ok is False
    assert str(etc / directory / "nmu-backup") in check.detail
    assert "nmu-backup.timer" in check.detail


def test_absent_cron_directories_mean_no_leftovers(tmp_path):
    assert preflight.check_no_nmu_cron(tmp_path / "etc").ok is True


def test_skipped_groups_do_not_fail_the_run(capsys):
    code = preflight.main([])

    assert code == 0
    assert capsys.readouterr().out.count("[SKIP]") == 5


def test_require_all_and_release_fail_when_any_group_is_skipped(capsys):
    assert preflight.main(["--require-all"]) == 1
    assert capsys.readouterr().out.count("[SKIP]") == 5
    assert preflight.main(["--release"]) == 1
    assert capsys.readouterr().out.count("[SKIP]") == 5


def _all_pass_args(tmp_path: Path) -> list[str]:
    return [
        "--db", str(tmp_path / "app.db"),
        "--backup-root", str(tmp_path / "backups"),
        "--lock", str(tmp_path / "requirements.lock"),
        "--os",
        "--base-url", "https://example.invalid",
    ]


def _make_all_automatic_groups_pass(monkeypatch) -> None:
    monkeypatch.setattr(
        preflight, "check_migration_head",
        lambda _path: preflight.Check("迁移头一致", True, "head"))
    monkeypatch.setattr(
        preflight, "check_backup",
        lambda _path: preflight.Check("备份新鲜度", True, "fresh"))
    monkeypatch.setattr(
        preflight, "check_backup_execution",
        lambda _path: preflight.Check("备份脚本可执行", True, "executable"))
    monkeypatch.setattr(
        preflight, "check_supply_chain",
        lambda _path, _python: preflight.Check("依赖与哈希锁一致", True, "locked"))
    monkeypatch.setattr(
        preflight, "check_os_security",
        lambda: preflight.Check("OS 安全补丁", True, "current"))
    monkeypatch.setattr(
        preflight, "check_timers_scheduled",
        lambda: preflight.Check("定时器在排程", True, "waiting"))
    monkeypatch.setattr(
        preflight, "check_sensitive_file_modes",
        lambda: preflight.Check("敏感文件不对 other 可读", True, "closed"))
    monkeypatch.setattr(
        preflight, "check_no_nmu_cron",
        lambda: preflight.Check("无 nmu-* cron 条目", True, "none"))
    monkeypatch.setattr(
        preflight, "check_public_surface",
        lambda _url: [preflight.Check("公网表面", True, "closed")])


def test_require_all_needs_no_release_evidence_index(tmp_path, monkeypatch):
    _make_all_automatic_groups_pass(monkeypatch)

    assert preflight.main([*_all_pass_args(tmp_path), "--require-all"]) == 0


def test_os_flag_also_checks_that_the_timers_are_scheduled(monkeypatch, capsys):
    _make_all_automatic_groups_pass(monkeypatch)
    monkeypatch.setattr(
        preflight, "check_timers_scheduled",
        lambda: preflight.Check(
            "定时器在排程", False, "nmu-health.timer(SubState=elapsed, NEXT=空)"))

    code = preflight.main(["--os"])

    assert code == 1
    output = capsys.readouterr().out
    assert "[PASS] OS 安全补丁" in output
    assert "[FAIL] 定时器在排程" in output
    assert "[PASS] 敏感文件不对 other 可读" in output
    assert "[PASS] 无 nmu-* cron 条目" in output


@pytest.mark.parametrize("mode, expected", [(0o640, 1), (0o750, 0)])
def test_require_all_checks_execution_even_with_a_fresh_backup(
        tmp_path, monkeypatch, capsys, mode, expected):
    app = _backup_app(tmp_path)
    for relative in preflight.BACKUP_EXECUTABLES:
        (app / relative).chmod(mode)
    execution_check = preflight.check_backup_execution
    _make_all_automatic_groups_pass(monkeypatch)
    monkeypatch.setattr(preflight, "check_backup_execution", execution_check)

    code = preflight.main([
        *_all_pass_args(tmp_path), "--require-all", "--app-root", str(app),
    ])

    assert code == expected
    output = capsys.readouterr().out
    assert "[PASS] 备份新鲜度" in output
    assert f"[{'FAIL' if expected else 'PASS'}] 备份脚本可执行" in output


def test_default_app_root_checks_the_deployed_preflight_tree(tmp_path, monkeypatch):
    app = _backup_app(tmp_path)
    (app / "scripts/vps-backup-daily.sh").chmod(0o640)
    execution_check = preflight.check_backup_execution
    _make_all_automatic_groups_pass(monkeypatch)
    monkeypatch.setattr(preflight, "check_backup_execution", execution_check)
    monkeypatch.setattr(preflight, "__file__", str(app / "scripts/preflight_check.py"))

    assert preflight.main([*_all_pass_args(tmp_path), "--require-all"]) == 1


def test_release_fails_without_evidence_even_when_every_automatic_group_passes(
        tmp_path, monkeypatch, capsys):
    _make_all_automatic_groups_pass(monkeypatch)

    code = preflight.main([
        *_all_pass_args(tmp_path),
        "--release", "--release-id", "a" * 64,
    ])

    assert code == 1
    output = capsys.readouterr().out
    assert "[FAIL] 发布证据精确绑定" in output
    assert "--evidence-index" in output


def test_release_binds_exact_bytes_but_cannot_replace_external_approval(
        tmp_path, monkeypatch, capsys):
    _make_all_automatic_groups_pass(monkeypatch)
    release_id = "a" * 64
    receipt = tmp_path / "receipt.json"
    receipt_bytes = (json.dumps({
        "schema_version": "nmu.synthetic-test-receipt.v1",
        "release_id": release_id,
    }, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    receipt.write_bytes(receipt_bytes)
    index = tmp_path / "evidence-index.json"
    index.write_text(json.dumps({
        "schema_version": "nmu.release-evidence-index.v1",
        "release_id": release_id,
        "entries": [{
            "kind": "synthetic-check",
            "path": "receipt.json",
            "sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        }],
    }), encoding="utf-8")

    code = preflight.main([
        *_all_pass_args(tmp_path), "--release",
        "--release-id", release_id,
        "--evidence-index", str(index),
    ])

    assert code == 1
    output = capsys.readouterr().out
    assert "[PASS] 发布证据精确绑定" in output
    assert "不代表外部审批有效" in output
    assert "[FAIL] 正式发布批准" in output
    assert "不能替代这些批准" in output


def test_evidence_arguments_without_release_are_never_silently_ignored(
        tmp_path, capsys):
    code = preflight.main([
        "--evidence-index", str(tmp_path / "missing.json"),
        "--release-id", "a" * 64,
    ])

    assert code == 1
    output = capsys.readouterr().out
    assert "[FAIL] 发布证据参数" in output
    assert "不能静默忽略" in output


def test_any_hard_failure_makes_the_run_nonzero(tmp_path, capsys):
    code = preflight.main(["--db", str(_db(tmp_path, "wrong-head"))])

    assert code == 1
    assert "[FAIL]" in capsys.readouterr().out
