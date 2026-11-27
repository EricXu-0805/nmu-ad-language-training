"""床旁槽读法守卫:app/ 里按编号取 LiveState 行,只许出现在白名单 helper 里。

两床并行后 id=1 只是全局锁锚兼 1 号槽;写死 id=1 去读内容,N=2 时就会读到另一床。
扫描认三类读法:
- 运行时用到 LiveState 类(注解和 import 不算):get(LiveState, …)、LiveState.id、
  LiveState(…)、select(LiveState…),以及 update/delete/isinstance 等别的用法;
  import 别名和 models.LiveState 一并认。
- 拿 _live_row_for_update(…) 的返回值来用:赋值、or、return、条件、当实参都算;
  只取锁的裸调用语句不限。
- 字符串里的表名 livestate / LIVESTATE(原生 SQL)。
位置按「文件 + 外层函数」记,不记行号,后面的步骤挪动行号不会红。
"""
from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path
from typing import NamedTuple

import pytest


ROOT = Path(__file__).resolve().parents[1]
_SQL_TABLE = re.compile(r"\b(?:livestate|LIVESTATE)\b")
_LOCK_HELPER = "_live_row_for_update"
_CONTENT_READ = f"取内容 {_LOCK_HELPER}("
_GET, _SELECT, _ID, _NEW = "get(LiveState", "select(LiveState", "LiveState.id", "LiveState("
_OTHER, _SQL = "LiveState 其它用法", "SQL 文本 livestate"

# 只有这些 helper 可以按编号取槽行。
SLOT_HELPERS = frozenset({
    # 全局锁锚;tests/test_autopilot_api.py 和 test_autopilot_control_concurrency.py
    # 按名字 monkeypatch 它装锁序探针,不能删也不能改名。
    ("app/main.py", _LOCK_HELPER),
    ("app/session_admission.py", "live_row_for_session"),
    # 停的路径用的不报违例版本:同一场写在两槽时取第一份,不在槽里回锚行。
    ("app/session_admission.py", "first_live_row_for_session"),
    # 握手选槽、配对、console-state 和清影子要把各槽一起看。
    ("app/session_admission.py", "bedside_slot_rows"),
    # 槽第一次被占时建行。
    ("app/main.py", "_new_bedside_slot_row"),
})

# 第 3 步把 afbe766 上写死 1 号槽的 53 处读法都改走了上面的 helper。往后真有暂时改不了的
# 读法才登记进来,改掉就删;改了不删,下面的对账同样会红。
PENDING: dict[tuple[str, str, str], int] = {}


class Site(NamedTuple):
    path: str
    function: str
    kind: str
    line: int


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _annotation_node_ids(tree: ast.AST) -> set[int]:
    annotations: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = node.args
            for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                             arguments.vararg, arguments.kwarg):
                if argument is not None and argument.annotation is not None:
                    annotations.append(argument.annotation)
            if node.returns is not None:
                annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
    return {id(inner) for annotation in annotations for inner in ast.walk(annotation)}


def _function_of(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names: list[str] = []
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    return ".".join(reversed(names)) or "<module>"


def _live_state_kind(reference: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    parent = parents[reference]
    if isinstance(parent, ast.Attribute) and parent.attr == "id":
        return _ID
    if isinstance(parent, ast.Call) and parent.func is reference:
        return _NEW
    if isinstance(parent, ast.Call) and _call_name(parent) == "get":
        return _GET
    if isinstance(parent, ast.Call) and _call_name(parent) == "select":
        return _SELECT
    return _OTHER


def _sites(source: str, path: str) -> list[Site]:
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    in_annotation = _annotation_node_ids(tree)
    live_state_names = {"LiveState"} | {
        alias.asname
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        for alias in node.names if alias.name == "LiveState" and alias.asname
    }
    found: list[Site] = []
    for node in ast.walk(tree):
        kind = None
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if (name in live_state_names if isinstance(node, ast.Name) else name == "LiveState"):
                if id(node) not in in_annotation:
                    kind = _live_state_kind(node, parents)
            elif name == _LOCK_HELPER:
                call = parents[node]
                bare = (isinstance(call, ast.Call) and call.func is node
                        and isinstance(parents.get(call), ast.Expr))
                if not bare:
                    kind = _CONTENT_READ
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and _SQL_TABLE.search(node.value)):
            kind = _SQL
        if kind is not None:
            found.append(Site(path, _function_of(node, parents), kind, node.lineno))
    return found


def _app_sites() -> list[Site]:
    return [
        site
        for file in sorted((ROOT / "app").rglob("*.py")) if "__pycache__" not in file.parts
        for site in _sites(file.read_text(encoding="utf-8"), str(file.relative_to(ROOT)))
    ]


def _defined_functions(path: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return {node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_slot_helpers_on_the_whitelist_still_exist():
    missing = sorted(f"{path}::{function}" for path, function in SLOT_HELPERS
                     if function not in _defined_functions(path))
    assert missing == [], "白名单里这些 helper 已经不在了"


def test_hardcoded_slot_reads_in_app_are_exactly_the_pending_list():
    outside = [site for site in _app_sites() if (site.path, site.function) not in SLOT_HELPERS]
    counts = Counter((site.path, site.function, site.kind) for site in outside)
    unlisted = [
        f"{site.path}:{site.line} {site.function} {site.kind}"
        for site in sorted(outside, key=lambda site: (site.path, site.line, site.kind))
        if counts[(site.path, site.function, site.kind)] > PENDING.get(
            (site.path, site.function, site.kind), 0)
    ]
    done = sorted(f"{key} 记 {count} 处,现在 {counts[key]} 处"
                  for key, count in PENDING.items() if counts[key] < count)
    assert not unlisted, (
        f"{len(unlisted)} 处写死槽位的读法不在 PENDING 里;改走 live_row_for_session"
        "(或新的白名单 helper),只取锁就写成裸调用:\n" + "\n".join(unlisted))
    assert not done, "这些读法已经改掉了,把 PENDING 里对应的行删掉或减数:\n" + "\n".join(done)


_HELPER_FREE = """
from app.models import LiveState


def f(s: DBSession, row: LiveState | None) -> LiveState | None:
    '''LiveState 单槽说明,不是 SQL。'''
    _live_row_for_update(s)
    kept: LiveState | None = None
    return row
"""


@pytest.mark.parametrize(("source", "expected"), [
    ("def f(s):\n    return s.get(LiveState, 1)\n", [("f", _GET)]),
    ("def f(db):\n    return db.get(\n        LiveState,\n        2)\n", [("f", _GET)]),
    ("from app.models import LiveState as Row\n\n\ndef f(db):\n    return db.get(Row, 2)\n",
     [("f", _GET)]),
    ("from app import models\n\n\ndef f(db):\n    return db.get(models.LiveState, 1)\n",
     [("f", _GET)]),
    ("def f(s):\n    return s.exec(select(LiveState).where(LiveState.id == 2)).first()\n",
     [("f", _ID), ("f", _SELECT)]),
    ("def f():\n    return LiveState(id=2, seq=0)\n", [("f", _NEW)]),
    ("def f(s):\n    s.exec(update(LiveState).values(seq=0))\n", [("f", _OTHER)]),
    ("def f(s):\n    live = _live_row_for_update(s)\n", [("f", _CONTENT_READ)]),
    ("def f(s):\n    return _live_row_for_update(s) or LiveState(id=1, seq=0)\n",
     [("f", _NEW), ("f", _CONTENT_READ)]),
    ("def f(s):\n    if _live_row_for_update(s) is None:\n        return None\n",
     [("f", _CONTENT_READ)]),
    ("def f(s):\n    g(_live_row_for_update(s))\n", [("f", _CONTENT_READ)]),
    ("lock = _live_row_for_update\n", [("<module>", _CONTENT_READ)]),
    ("def f(s):\n    s.exec(text('select seq from livestate where id = 1'))\n",
     [("f", _SQL)]),
    ("def outer():\n    def inner(s):\n        return s.get(LiveState, 1)\n    return inner\n",
     [("outer.inner", _GET)]),
    (_HELPER_FREE, []),
])
def test_scanner_recognises_slot_reads(source, expected):
    assert sorted((site.function, site.kind) for site in _sites(source, "app/x.py")) == sorted(expected)
