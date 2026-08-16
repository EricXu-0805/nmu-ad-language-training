"""研究分区聚合的披露控制：登记表、单元格抑制、分档与率截断。

这一层**没有数据库、没有 FastAPI**，全是纯函数，因为它要能被单独证伪：给它
一组单元格就能问"抑制完了还能不能倒推出被抑制的那一格"。

为什么要有它：`ai_quality_service.build_ai_quality_dashboard` 里那段注释自己
写明了缺什么——总人数门槛既挡不住稀疏单元，也挡不住重复查询做差分。冻结纪元
（`app/quality_release.py`）解决"可重复性"，这个模块解决"单次发布内的减法"。
两件事分开做，因为它们的失效方式不一样：纪元失效是数据变了，抑制失效是算术。

**登记表覆盖的是全部叶子，不只是数值叶子。** 字符串 `status` 和"哪些字段是
null"这张位图都是实证过的侧信道：把 12 项 reason_counts 里的 3 项置 null、
9 项留值，位图本身就说出了那 3 项非零。所以 research 行的 reason_counts 要么
整块 null，要么整块出——不做部分 null。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal


#: 一个叶子字段怎么对外。
#: - ``clear``：原样发（周次、题型这类研究设计常量）
#: - ``rate``：只发比率，按 ``rate_decimals`` 截断，原始分子分母不发
#: - ``band``：只发所在档，按 ``band_width`` 向下取整
#: - ``suppressed``：恒 null
Disclosure = Literal["clear", "rate", "band", "suppressed"]


@dataclass(frozen=True)
class PublishedLeaf:
    path: tuple[str, ...]
    disclosure: Disclosure
    note: str


class DisclosureRegistryError(RuntimeError):
    """登记表与实际载荷对不上。稳定 code，不含任何数值。"""

    def __init__(self, code: str, detail: str):
        super().__init__(code)
        self.code = code
        self.detail = detail


def leaves(payload: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """列出载荷里的全部叶子路径。列表按下标展开，因为报表是定长的。"""
    if isinstance(payload, dict):
        found: list[tuple[str, ...]] = []
        for key in sorted(payload):
            found.extend(leaves(payload[key], prefix + (str(key),)))
        return found
    if isinstance(payload, list):
        found = []
        for index, item in enumerate(payload):
            found.extend(leaves(item, prefix + (str(index),)))
        return found
    return [prefix]


def schema_gaps(payload: Any, registry: Iterable[PublishedLeaf]) -> list[str]:
    """载荷里有、登记表里没有的叶子。**默认抑制**靠的就是这个差集。

    只报单向差：登记表里多出来的条目不算问题（报表册可以只渲染其中一部分），
    载荷里多出来的才是问题——那是一个没人审过就发出去的字段。
    """
    registered = {leaf.path for leaf in registry}
    return ["/".join(path) for path in leaves(payload)
            if path not in registered]


def band(value: int | None, *, width: int) -> str | None:
    """把计数换成档位字符串。``None`` 进 ``None`` 出。

    发 ``"20-29"`` 而不是 ``25``：两次发布之间的差从一个精确整数变成一个区间。
    这**不消掉**边界差分，只是把它钝化——honest_limits 里写着这一条。
    """
    if value is None:
        return None
    if width <= 0:
        raise DisclosureRegistryError("band_width_invalid", "档宽必须为正")
    low = (int(value) // width) * width
    return f"{low}-{low + width - 1}"


def rate(numerator: int | None, denominator: int | None, *,
         decimals: int) -> float | None:
    """比率，按位数截断。分母为 0 或任一端未知都返回 ``None``。

    截断不是四舍五入：``round`` 会让 0.4449 和 0.4451 落到不同的两位数，而
    两者之差可能正好是一个人。向下截断让同一档内的所有值坍缩成一个。
    """
    if numerator is None or not denominator:
        return None
    if decimals < 1:
        raise DisclosureRegistryError("rate_decimals_invalid", "小数位必须 >= 1")
    scale = 10 ** decimals
    return int((numerator / denominator) * scale) / scale


@dataclass(frozen=True)
class Cell:
    """一个待发布的单元格。``key`` 只用于关联，绝不进载荷。"""

    key: str
    contributors: int
    linked: frozenset[str] = frozenset()


def plan_suppression(cells: Iterable[Cell], *, minimum: int) -> set[str]:
    """算出必须抑制哪些单元格的键。

    三步，缺任何一步都能被减法倒推：

    1. **主抑制**：贡献人数 < minimum 的格。
    2. **互补抑制**：一张表里只抑制了一个格时，总计减去其余格就把它还原了。
       所以只要有抑制，同表至少要抑制两个——不够就把剩下里贡献最少的那个
       也抑制掉（牺牲信息量换不可倒推，方向不能反）。
    3. **连接闭包**：格之间有 ``linked`` 关系（同一批人出现在两张表里）时，
       抑制要沿着连接传播到不动点，否则跨表的减法照样成立。

    返回键的集合。函数不改输入，也不碰载荷——套用在哪由调用者决定。
    """
    if minimum < 2:
        raise DisclosureRegistryError(
            "min_cell_subjects_invalid", "单元格门槛必须 >= 2")
    pool = list(cells)
    by_key = {cell.key: cell for cell in pool}
    if len(by_key) != len(pool):
        raise DisclosureRegistryError("cell_key_duplicated", "单元格键重复")

    suppressed = {cell.key for cell in pool if cell.contributors < minimum}

    # 互补：整张表只抑制了一个 → 再抑制一个。
    if len(suppressed) == 1 and len(pool) > 1:
        remaining = sorted(
            (cell for cell in pool if cell.key not in suppressed),
            key=lambda cell: (cell.contributors, cell.key))
        suppressed.add(remaining[0].key)

    # 连接闭包：沿 linked 传播到不动点。
    while True:
        grown = set(suppressed)
        for key in suppressed:
            grown |= by_key[key].linked & by_key.keys()
        if grown == suppressed:
            return suppressed
        suppressed = grown


def registry_self_check(registry: Iterable[PublishedLeaf]) -> list[str]:
    """登记表自身是否自洽。返回问题清单，空表示没问题。"""
    problems: list[str] = []
    seen: set[tuple[str, ...]] = set()
    allowed = {"clear", "rate", "band", "suppressed"}
    for leaf in registry:
        if leaf.path in seen:
            problems.append(f"{'/'.join(leaf.path)} 重复登记")
        seen.add(leaf.path)
        if leaf.disclosure not in allowed:
            problems.append(
                f"{'/'.join(leaf.path)} 的 disclosure "
                f"{leaf.disclosure!r} 不在闭集内")
        if not leaf.note.strip():
            problems.append(f"{'/'.join(leaf.path)} 没写理由")
    if not seen:
        problems.append("登记表是空的——那样任何字段都会被判成未登记")
    return problems
