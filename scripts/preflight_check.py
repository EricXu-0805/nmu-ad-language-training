#!/usr/bin/env python3
"""真机点穿之前，把部署这一侧能自动查的东西一次查完。

D2 走查那天真正只能靠人的，是麦克风、噪声、断网和老人的普通话。除此之外——
迁移头对不对、备份新不新、公网红线还在不在、受保护路由是不是真的要登录——
都不该等到人坐在设备前面才发现。

三组检查各自可跳过，任一硬失败即非零退出：
  数据库   代码的 alembic 头与库里的头是否一致（只读，一条 SELECT）
  备份     复用 backup_health_check 的判据
  公网     /health 200、四条红线 404、五个安全头齐、受保护路由 401

不打任何供应商的云 API：那要花钱、要把文本发出去，得由人明确发起。
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import ssl
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="要核对迁移头的 SQLite")
    parser.add_argument("--backup-root", type=Path, default=None)
    parser.add_argument("--base-url", default=None, help="公网入口，例如 https://…")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    checks: list[Check] = []
    checks.append(check_migration_head(args.db) if args.db
                  else Check("迁移头一致", None, "未给 --db"))
    checks.append(check_backup(args.backup_root) if args.backup_root
                  else Check("备份新鲜度", None, "未给 --backup-root"))
    if args.base_url:
        checks.extend(check_public_surface(args.base_url))
    else:
        checks.append(Check("公网表面", None, "未给 --base-url"))

    if args.json:
        print(json.dumps(
            [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
            ensure_ascii=False, indent=2))
    else:
        for check in checks:
            print(check.line())

    return 1 if any(check.ok is False for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
