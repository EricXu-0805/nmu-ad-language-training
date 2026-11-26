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
})

_MAIN, _VISIT = "app/main.py", "app/visit_plan_service.py"

# TODO(两床第 3 步):afbe766 上还写死 1 号槽的读法,共 53 处。第 3a-3g 步每改一处就删一行
# 或减数,删到空为止;改了不删,下面的对账同样会红。仓库没有 xfail 的惯例,所以先把今天的
# 读法全记在这里,套件保持绿,新增的未登记读法照样拦。行尾数字是 afbe766 上的行号,
# 分组照计划「实施顺序」;「未分组」是计划没点名的,改到相邻那组时一并定:改走本场槽,
# 还是确属锚行用法、进 SLOT_HELPERS 并写明理由。
PENDING: dict[tuple[str, str, str], int] = {
    # 3c 自动带练
    ("app/autopilot_service.py", "_require_live_binding", _GET): 1,  # 843
    ("app/autopilot_orchestration.py", "_legacy_live_binding", _GET): 1,  # 681
    (_MAIN, "_revalidate_tts_after_provider", _CONTENT_READ): 1,  # 9782
    (_MAIN, "tts_speak", _CONTENT_READ): 1,  # 9878
    (_MAIN, "autopilot_command_tts", _CONTENT_READ): 1,  # 9967
    (_MAIN, "rapport_utterance_tts", _CONTENT_READ): 1,  # 10032
    (_MAIN, "autopilot_start", _CONTENT_READ): 1,  # 10370
    (_MAIN, "autopilot_drain_ack", _CONTENT_READ): 1,  # 10451
    (_MAIN, "autopilot_drain_target", _CONTENT_READ): 1,  # 10486
    (_MAIN, "autopilot_resume", _CONTENT_READ): 1,  # 10748
    (_MAIN, "autopilot_adjudicate", _CONTENT_READ): 1,  # 10829
    (_MAIN, "autopilot_next", _CONTENT_READ): 1,  # 10916
    (_MAIN, "autopilot_voice_settings", _CONTENT_READ): 1,  # 11046
    # 3d 回执重放
    (_MAIN, "_technical_pause_replay", _CONTENT_READ): 1,  # 12351
    (_MAIN, "_interaction_presentation_replay", _CONTENT_READ): 1,  # 12447
    (_MAIN, "append_interaction_presentation", _CONTENT_READ): 1,  # 14940
    (_MAIN, "commit_patient_pause", _CONTENT_READ): 1,  # 15284
    (_MAIN, "commit_technical_pause", _CONTENT_READ): 1,  # 15405
    # 3e 握手、自动场切回、照护员
    (_MAIN, "live_put.authorize_and_apply", _CONTENT_READ): 1,  # 7681
    (_MAIN, "live_put.authorize_and_apply", _NEW): 1,  # 7681 的 LiveState(id=1, seq=0)
    (_MAIN, "live_put", _CONTENT_READ): 1,  # 7712,建行撞唯一约束后的重读
    (_MAIN, "caregiver_activate_session", _CONTENT_READ): 1,  # 7763
    (_MAIN, "caregiver_session_status", _GET): 1,  # 7815
    (_MAIN, "autopilot_activate", _GET): 1,  # 10581
    (_MAIN, "autopilot_activate", _CONTENT_READ): 1,  # 10607
    (_MAIN, "autopilot_activate", _NEW): 1,  # 10685
    # 3f 配对
    (_MAIN, "_issue_live_session_capability", _CONTENT_READ): 1,  # 5185
    # 3g 准入闸、收麦真值
    (_VISIT, "assert_patient_ready_for_new_work", _GET): 1,  # 1445
    (_VISIT, "assert_actor_ready_for_new_work", _GET): 1,  # 1495
    (_MAIN, "_assess_rapport_completion", _SELECT): 1,  # 8411
    (_MAIN, "_assess_rapport_completion", _ID): 1,  # 8411
    # 未分组
    (_MAIN, "warmup_action", _CONTENT_READ): 1,  # 5688
    (_MAIN, "report_rapport_playback", _CONTENT_READ): 1,  # 6712
    (_MAIN, "get_rapport_playback", _CONTENT_READ): 1,  # 6755
    (_MAIN, "live_console_get", _GET): 1,  # 8060,计划「选型」:不带参数时回本人那一槽
    (_MAIN, "put_session_runtime_cursor", _CONTENT_READ): 1,  # 8172
    (_MAIN, "resume_session", _CONTENT_READ): 1,  # 8312
}


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
