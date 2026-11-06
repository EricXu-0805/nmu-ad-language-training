"""配置开关清单与代码对账：app/ 与 scripts/ 读的每个环境变量都要在清单里有一行，
清单里的每一行也都要还有代码在读。读法看不出变量名（拼字符串、传进来的映射）就直接红，
让人把它改成能对账的写法或者在清单里补上——不能悄悄漏掉。"""
from __future__ import annotations

import ast
import functools
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs/handover/配置开关清单.md"
SCANNED = ("app", "scripts")
_ROW = re.compile(r"^\| `([A-Z][A-Z0-9_]*)` \|", re.M)


def _is_environ(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def _walk(node: ast.AST, scope: tuple = ()):
    """带作用域遍历：scope 里依次是外层函数与 for/推导式的 (目标, 迭代对象)。"""
    yield node, scope
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        scope = (*scope, node)
    if isinstance(node, (ast.For, ast.AsyncFor)):
        yield from _walk(node.iter, scope)
        inner = (*scope, (node.target, node.iter))
        for child in (*node.body, *node.orelse):
            yield from _walk(child, inner)
        return
    if isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp)):
        for generator in node.generators:
            yield from _walk(generator.iter, scope)
            scope = (*scope, (generator.target, generator.iter))
            for condition in generator.ifs:
                yield from _walk(condition, scope)
        children = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        for child in children:
            yield from _walk(child, scope)
        return
    for child in ast.iter_child_nodes(node):
        yield from _walk(child, scope)


class _Scanner:
    def __init__(self) -> None:
        self.trees = {
            path: ast.parse(path.read_text(encoding="utf-8"))
            for directory in SCANNED
            for path in sorted((ROOT / directory).rglob("*.py"))
            if "__pycache__" not in path.parts
        }
        self.unresolved: list[str] = []
        self._aliases: dict[int, set[str]] = {}

    def _module_value(self, path: Path, name: str) -> tuple[Path, ast.AST] | None:
        for statement in self.trees[path].body:
            if isinstance(statement, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == name for t in statement.targets):
                return path, statement.value
            if (isinstance(statement, ast.AnnAssign) and statement.value is not None
                    and isinstance(statement.target, ast.Name)
                    and statement.target.id == name):
                return path, statement.value
            if isinstance(statement, ast.ImportFrom) and statement.level == 0 and statement.module:
                for alias in statement.names:
                    other = ROOT / (statement.module.replace(".", "/") + ".py")
                    if (alias.asname or alias.name) == name and other in self.trees:
                        return self._module_value(other, alias.name)
        return None

    def names(self, expr: ast.AST, path: Path, scope: tuple, seen: frozenset = frozenset()) -> set[str]:
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return {expr.value}
        if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
            return set().union(*(self.names(e, path, scope, seen) for e in expr.elts))
        if isinstance(expr, ast.Name):
            for frame in reversed(scope):
                if isinstance(frame, tuple):
                    target, iterable = frame
                    if isinstance(target, ast.Name) and target.id == expr.id:
                        return self.names(iterable, path, scope, seen)
                    continue
                arguments = (*frame.args.posonlyargs, *frame.args.args, *frame.args.kwonlyargs)
                if expr.id in [a.arg for a in arguments]:
                    return self._parameter_names(frame, expr.id, path, seen)
            found = self._module_value(path, expr.id)
            if found is not None:
                return self.names(found[1], found[0], (), seen)
        self.unresolved.append(f"{path.relative_to(ROOT)}:{expr.lineno}: {ast.unparse(expr)}")
        return set()

    def _parameter_names(self, function: ast.FunctionDef, parameter: str, path: Path,
                         seen: frozenset) -> set[str]:
        """形参读环境变量（_enabled(name) 这类小助手）：把同模块每个调用点传的实参解出来。"""
        key = (path, function.name, parameter)
        if key in seen:
            return set()
        positional = [a.arg for a in (*function.args.posonlyargs, *function.args.args)]
        result: set[str] = set()
        calls = 0
        for node, scope in _walk(self.trees[path]):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == function.name):
                continue
            calls += 1
            argument = next((k.value for k in node.keywords if k.arg == parameter), None)
            index = positional.index(parameter) if parameter in positional else len(node.args)
            if argument is None and index < len(node.args):
                argument = node.args[index]
            if argument is not None:
                result |= self.names(argument, path, scope, seen | {key})
        if not calls:
            self.unresolved.append(
                f"{path.relative_to(ROOT)}:{function.lineno}: {function.name}({parameter}) 没有调用点")
        return result

    def _env_aliases(self, scope: tuple) -> set[str]:
        """函数里绑到 os.environ 的局部名，如 `source = os.environ if env is None else env`。"""
        aliases: set[str] = set()
        for frame in scope:
            if isinstance(frame, tuple):
                continue
            if id(frame) not in self._aliases:
                self._aliases[id(frame)] = {
                    target.id
                    for node in ast.walk(frame)
                    if isinstance(node, ast.Assign)
                    and any(_is_environ(n) for n in ast.walk(node.value))
                    for target in node.targets if isinstance(target, ast.Name)
                }
            aliases |= self._aliases[id(frame)]
        return aliases

    def _read_key(self, node: ast.AST, scope: tuple) -> ast.AST | None:
        def environ_like(value: ast.AST) -> bool:
            return _is_environ(value) or (
                isinstance(value, ast.Name) and value.id in self._env_aliases(scope))

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
            if node.func.attr == "get" and environ_like(node.func.value):
                return node.args[0]
            if (node.func.attr == "getenv" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "os"):
                return node.args[0]
        if (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
                and environ_like(node.value)):
            return node.slice
        if (isinstance(node, ast.Compare) and len(node.ops) == 1
                and isinstance(node.ops[0], (ast.In, ast.NotIn))
                and environ_like(node.comparators[0])):
            return node.left
        return None

    def read_names(self) -> dict[str, set[str]]:
        found: dict[str, set[str]] = {}
        for path, tree in self.trees.items():
            for node, scope in _walk(tree):
                key = self._read_key(node, scope)
                if key is None:
                    continue
                for name in self.names(key, path, scope):
                    found.setdefault(name, set()).add(str(path.relative_to(ROOT)))
        return found


@functools.cache
def _scan() -> tuple[dict[str, set[str]], tuple[str, ...]]:
    scanner = _Scanner()
    read = scanner.read_names()
    return read, tuple(scanner.unresolved)


def _inventory_names() -> list[str]:
    return _ROW.findall(INVENTORY.read_text(encoding="utf-8"))


def test_every_env_read_in_app_and_scripts_resolves_to_a_literal_name():
    assert _scan()[1] == ()


def test_inventory_lists_exactly_the_env_vars_the_code_reads():
    read = _scan()[0]
    listed = _inventory_names()

    assert len(listed) == len(set(listed)), "清单里有重复行"
    missing = {name: sorted(read[name]) for name in sorted(set(read) - set(listed))}
    stale = sorted(set(listed) - set(read))
    assert missing == {}, "代码新读了这些变量，清单里还没有"
    assert stale == [], "清单里这些变量已经没有代码在读"


def test_scanner_sees_the_indirect_read_styles_this_codebase_uses():
    # 守住扫描器自身：常量、跨模块导入、形参助手、for/推导式、os.environ 别名各一例。
    read = _scan()[0]

    assert "app/autopilot_service.py" in read["NMU_AUTOPILOT_VAD"]
    assert "app/auth.py" in read["AUTH_PBKDF2_MAX_CONCURRENCY"]
    assert "app/provider_readiness.py" in read["DEIDENTIFICATION_KEY"]
    assert "app/autopilot_plan_profiles.py" in read["ENABLE_AUTOPILOT_P0A_SIMULATION"]
    assert "app/export_security.py" in read["DEIDENTIFICATION_KEY_ID"]
