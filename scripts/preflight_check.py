#!/usr/bin/env python3
"""真机点穿之前，把部署这一侧能自动查的东西一次查完。

D2 走查那天真正只能靠人的，是麦克风、噪声、断网和老人的普通话。除此之外——
迁移头对不对、备份新不新、公网红线还在不在、受保护路由是不是真的要登录——
都不该等到人坐在设备前面才发现。

五组自动检查各自可跳过，任一硬失败即非零退出：
  数据库   代码的 alembic 头与库里的头是否一致（只读，一条 SELECT）
  备份     复用 backup_health_check 的判据
  依赖     已安装依赖与带哈希锁是否一致
  操作系统 显式启用时核对安全更新和重启状态
  公网     /health 200、四条红线 404、五个安全头齐、受保护路由 401

不打任何供应商的云 API：那要花钱、要把文本发出去，得由人明确发起。
``--release`` 另加证据字节绑定；外部类型验证与具名批准者
合同冻结前，它始终拒绝正式发布。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import ssl
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import release_evidence

REDLINE_PATHS = ("/content/item_bank_v1.json", "/openapi.json", "/docs", "/redoc")
PROTECTED_PATHS = ("/patients", "/sessions", "/items", "/ai/provider-readiness")
REQUIRED_HEADERS = (
    "content-security-policy",
    "strict-transport-security",
    "x-frame-options",
    "x-content-type-options",
    "permissions-policy",
)


class Check:
    def __init__(self, name: str, ok: bool | None, detail: str) -> None:
        self.name = name
        self.ok = ok  # None = 跳过
        self.detail = detail

    def line(self) -> str:
        mark = "SKIP" if self.ok is None else ("PASS" if self.ok else "FAIL")
        return f"[{mark}] {self.name}: {self.detail}"


def _code_head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[1]
    script = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    heads = script.get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"代码里不是单一 head：{heads}")
    return heads[0]


def check_migration_head(db_path: Path) -> Check:
    name = "迁移头一致"
    try:
        expected = _code_head()
    except Exception as error:  # noqa: BLE001
        return Check(name, False, f"读不到代码 head：{error}")
    if not db_path.is_file():
        return Check(name, False, f"数据库不存在：{db_path}")
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = [str(row[0]) for row in
                    connection.execute("SELECT version_num FROM alembic_version")]
        finally:
            connection.close()
    except sqlite3.Error as error:
        return Check(name, False, f"读库失败：{error}")
    if rows != [expected]:
        return Check(name, False, f"代码 {expected}，库里 {rows}")
    return Check(name, True, expected)


def check_backup(backup_root: Path) -> Check:
    name = "备份新鲜度"
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "backup_health_check", Path(__file__).resolve().parent / "backup_health_check.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        problems = module.evaluate(
            backup_root / "backup.log", backup_root / "daily", datetime.now(),
            max_age_hours=30.0, max_consecutive_failures=1, min_free_mb=1024)
    except module.Unevaluable as error:
        return Check(name, False, str(error))
    if problems:
        return Check(name, False, "; ".join(problems))
    return Check(name, True, "最新快照在窗口内，且真的在盘上")


def check_supply_chain(lock_path: Path, python_executable: str) -> Check:
    """跑着这台机器的解释器，装的东西和哈希锁对不对得上。

    2026-08-06 查出生产 venv 比锁多 20 个包、9 处版本漂移——没人审过，装的时候
    也没有哈希被校验。锁只在安装那一刻起作用，之后靠这条守。
    """
    name = "依赖与哈希锁一致"
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "supply_chain_check", Path(__file__).resolve().parent / "supply_chain_check.py")
    assert spec is not None and spec.loader is not None
    supply_chain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supply_chain)
    try:
        entries, floor = supply_chain.parse_lock(lock_path.read_text(encoding="utf-8"))
        snapshot = supply_chain.probe(python_executable)
    except (OSError, ValueError, RuntimeError) as error:
        return Check(name, False, f"对不成账：{error}")
    failures, _, stats = supply_chain.compare(entries, floor, snapshot)
    if failures:
        counts: dict[str, int] = {}
        for failure in failures:
            counts[failure.kind] = counts.get(failure.kind, 0) + 1
        summary = "、".join(f"{kind} {count}" for kind, count in sorted(counts.items()))
        return Check(name, False,
                     f"{len(failures)} 处对不上（{summary}）；"
                     f"跑 scripts/supply_chain_check.py --python {python_executable} 看明细")
    # 把解释器版本一并报出来:裸机那台是自编译的,没有 apt 会替我们打补丁,
    # 每次上线都看一眼它是几,是唯一不额外花钱的提醒。
    return Check(name, True, f"Python {stats['python']}，"
                             f"{stats['expected_here']} 个包逐个对上，无锁外来客")


def check_os_security(max_age_days: int = 3) -> Check:
    """操作系统欠的安全补丁。2026-08-06 查出这台机器积了 93 个没人看。

    只在装了 apt 的机器上有意义（生产裸机是 Ubuntu）；Mac 上跑会失败在
    "查不动 apt"，所以这组默认关、由 --os 显式打开。
    """
    name = "OS 安全补丁"
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "os_security_check", Path(__file__).resolve().parent / "os_security_check.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        pending = module.parse_simulation(module.run_simulation())
    except (OSError, RuntimeError) as error:
        return Check(name, False, f"查不动 apt：{error}")
    age = module.lists_age(module.APT_LISTS, datetime.now())
    failures, notes = module.evaluate(
        pending, age, timedelta(days=max_age_days),
        module.REBOOT_REQUIRED.exists())
    if failures:
        return Check(name, False, "；".join(failures + notes))
    detail = "安全更新积压为 0"
    if notes:
        detail += "（" + "；".join(notes) + "）"
    return Check(name, True, detail)


def _default_fetch(url: str, timeout: float):
    context = ssl.create_default_context()
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}


def check_public_surface(base_url: str, fetch=_default_fetch, timeout: float = 15.0
                         ) -> list[Check]:
    base = base_url.rstrip("/")
    checks: list[Check] = []

    try:
        status, headers = fetch(f"{base}/health", timeout)
    except Exception as error:  # noqa: BLE001
        return [Check("公网可达", False, f"{error}")]
    checks.append(Check("/health", status == 200, str(status)))

    missing = [h for h in REQUIRED_HEADERS if h not in headers]
    checks.append(Check("安全头", not missing, "齐" if not missing else f"缺 {missing}"))

    leaked = []
    for path in REDLINE_PATHS:
        try:
            status, _ = fetch(f"{base}{path}", timeout)
        except Exception as error:  # noqa: BLE001
            leaked.append(f"{path}({error})")
            continue
        if status != 404:
            leaked.append(f"{path}={status}")
    checks.append(Check("红线 404", not leaked, "全 404" if not leaked else str(leaked)))

    open_routes = []
    for path in PROTECTED_PATHS:
        try:
            status, _ = fetch(f"{base}{path}", timeout)
        except Exception as error:  # noqa: BLE001
            open_routes.append(f"{path}({error})")
            continue
        if status not in {401, 403}:
            open_routes.append(f"{path}={status}")
    checks.append(Check("受保护路由要登录", not open_routes,
                        "全 401/403" if not open_routes else str(open_routes)))
    return checks


def check_release_evidence(
        evidence_index: Path | None, release_id: str | None) -> Check:
    """Bind this release run to exact receipt bytes without judging approvals."""
    name = "发布证据精确绑定"
    if evidence_index is None or release_id is None:
        return Check(
            name, False,
            "--release 必须同时给 --evidence-index 和 --release-id")
    try:
        binding = release_evidence.verify_evidence_index(
            evidence_index, release_id)
    except release_evidence.ReleaseEvidenceError as error:
        return Check(name, False, f"code={error.code}")
    return Check(
        name,
        True,
        f"{binding['receipt_count']} 份 JSON 回执已逐字节绑定；"
        f"index_sha256={binding['index_sha256']}；"
        "只证明字节归属同一 release，不代表外部审批有效",
    )


def check_release_authorization_policy() -> Check:
    """Keep direct-use release fail-closed until external authorities are bound."""
    return Check(
        "正式发布批准",
        False,
        "尚未冻结具名养老院、PI、伦理/隐私、法规和运维批准的类型专属验证器；"
        "证据字节绑定不能替代这些批准",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="要核对迁移头的 SQLite")
    parser.add_argument("--backup-root", type=Path, default=None)
    parser.add_argument("--lock", type=Path, default=None,
                        help="哈希锁；给了就把跑这个脚本的解释器和它对一遍账")
    parser.add_argument("--os", action="store_true",
                        help="查操作系统安全补丁积压（只在装了 apt 的机器上有意义）")
    parser.add_argument("--base-url", default=None, help="公网入口，例如 https://…")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--require-all", action="store_true",
        help="严格门禁：任何未提供/未执行的检查也判失败")
    parser.add_argument(
        "--release", action="store_true",
        help=("正式发布预检：包含 --require-all 和证据字节绑定；"
              "外部批准验证器冻结前始终拒绝正式发布"))
    parser.add_argument(
        "--evidence-index", type=Path, default=None,
        help="--release 使用的 nmu.release-evidence-index.v1 JSON")
    parser.add_argument(
        "--release-id", default=None,
        help="--release 期望的 64 位小写 hex release id")
    args = parser.parse_args(argv)

    checks: list[Check] = []
    evidence_args_present = (
        args.evidence_index is not None or args.release_id is not None)
    if evidence_args_present and not args.release:
        checks.append(Check(
            "发布证据参数",
            False,
            "--evidence-index/--release-id 只能与 --release 同时使用，不能静默忽略",
        ))
    checks.append(check_migration_head(args.db) if args.db
                  else Check("迁移头一致", None, "未给 --db"))
    checks.append(check_backup(args.backup_root) if args.backup_root
                  else Check("备份新鲜度", None, "未给 --backup-root"))
    checks.append(check_supply_chain(args.lock, sys.executable) if args.lock
                  else Check("依赖与哈希锁一致", None, "未给 --lock"))
    checks.append(check_os_security() if args.os
                  else Check("OS 安全补丁", None, "未给 --os"))
    if args.base_url:
        checks.extend(check_public_surface(args.base_url))
    else:
        checks.append(Check("公网表面", None, "未给 --base-url"))
    if args.release:
        checks.append(check_release_evidence(
            args.evidence_index, args.release_id))
        checks.append(check_release_authorization_policy())

    if args.json:
        print(json.dumps(
            [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
            ensure_ascii=False, indent=2))
    else:
        for check in checks:
            print(check.line())

    strict = args.require_all or args.release
    failed = any(check.ok is False for check in checks)
    skipped = any(check.ok is None for check in checks)
    return 1 if failed or (strict and skipped) else 0


if __name__ == "__main__":
    raise SystemExit(main())
