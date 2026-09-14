"""复原求解器（纯函数，不依赖 FastAPI / 数据库）。

模型
----
- 外观（刻印+颜色+形状）相同的药片视为不可区分项，按"外观组"独立求解。
- 对某个外观组，每格的计划内容 = 必填粒数之和 + 允许空格药品的粒数子集。
- 决策：每格最终该外观的总粒数 t[c]，满足
    t[c] ∈ 该格可达集合 achievable(c)，t[c] >= 残留 r[c]，
    且全周闭合：sum(t[c]) = sum(r) + 散落 s  （即散落的每粒都必须归位）。
- 回溯枚举全部全局一致方案（带上限保护）；对每个 (组, 格) 统计散落入格粒数
  x[c] = t[c] - r[c] 在所有方案中的取值集合。

可归位判定（min 规则）
----------------------
药片不可区分，因此某格"确定可归位"的粒数 = 所有方案中 x[c] 的最小值
（每个方案都至少有这么多粒进入该格，放入这些粒与任何方案都不冲突）。
其余散落药片一律进入隔离清单。
"""

from __future__ import annotations

from dataclasses import dataclass, field

SLOTS = ("morning", "noon", "evening")
SLOT_LABELS = {"morning": "早", "noon": "中", "evening": "晚"}
_SLOT_INDEX = {s: i for i, s in enumerate(SLOTS)}

DEFAULT_MAX_SOLUTIONS = 20_000
DEFAULT_MAX_NODES = 2_000_000


def cell_index(day: int, slot: str) -> int:
    """(day 1..7, slot) -> 0..20"""
    return (day - 1) * 3 + _SLOT_INDEX[slot]


def cell_ref(index: int) -> dict:
    return {"day": index // 3 + 1, "slot": SLOTS[index % 3]}


@dataclass(frozen=True)
class Appearance:
    imprint: str
    color: str
    shape: str


@dataclass
class CellDemand:
    """某格内某外观组的需求：必填粒数 + 允许空格药品各自的粒数。"""

    required: int = 0
    optional: list[int] = field(default_factory=list)

    def achievable(self) -> list[int]:
        """该格该外观最终总粒数的所有可达取值（升序去重）。"""
        sums = {self.required}
        for dose in self.optional:
            sums |= {s + dose for s in list(sums)}
        return sorted(sums)


@dataclass
class GroupInput:
    appearance: Appearance
    med_ids: list[str]
    demands: dict[int, CellDemand]      # 格 -> 需求（仅含计划到该外观的格）
    residual: dict[int, int]            # 格 -> 残留粒数
    scattered: int                      # 散落粒数
    has_optional: bool = False          # 是否含允许空格（用于歧义原因说明）


@dataclass
class GroupOutcome:
    appearance: Appearance
    feasible: bool
    truncated: bool                     # 枚举超上限，x_values 不完整、不可信
    solution_count: int | None          # 截断时为 None
    x_values: dict[int, list[int]]      # 格 -> 散落入格粒数的取值集合（升序）
    min_total: int                      # 全周该外观可达总量下界（用于错误说明）
    max_total: int                      # 全周该外观可达总量上界


def solve_group(
    group: GroupInput,
    max_solutions: int = DEFAULT_MAX_SOLUTIONS,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> GroupOutcome:
    cells = sorted(set(group.demands) | set(group.residual))
    choices: list[tuple[int, list[int]]] = []
    for c in cells:
        demand = group.demands.get(c, CellDemand())
        res = group.residual.get(c, 0)
        achievable = [t for t in demand.achievable() if t >= res]
        if not achievable:
            # 格级不可行（残留超过容量 / 残留落在无计划的格），调用方通常已先拦截
            return GroupOutcome(group.appearance, False, False, None, {}, 0, 0)
        choices.append((c, achievable))

    target = sum(group.residual.values()) + group.scattered  # 全周该外观总粒数
    n = len(choices)
    suffix_min = [0] * (n + 1)
    suffix_max = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_min[i] = suffix_min[i + 1] + choices[i][1][0]
        suffix_max[i] = suffix_max[i + 1] + choices[i][1][-1]
    min_total, max_total = suffix_min[0], suffix_max[0]
    if not (min_total <= target <= max_total):
        return GroupOutcome(
            group.appearance, False, False, None, {}, min_total, max_total
        )

    x_acc: dict[int, set[int]] = {c: set() for c, _ in choices}
    path = [0] * n
    count = 0
    nodes = 0
    truncated = False

    def backtrack(i: int, remaining: int) -> None:
        nonlocal count, nodes, truncated
        if truncated:
            return
        nodes += 1
        if nodes > max_nodes:
            truncated = True
            return
        if i == n:
            if remaining != 0:
                return
            count += 1
            if count > max_solutions:
                truncated = True
                return
            for (c, _), t in zip(choices, path):
                x_acc[c].add(t - group.residual.get(c, 0))
            return
        if remaining < suffix_min[i] or remaining > suffix_max[i]:
            return
        c, achievable = choices[i]
        for t in achievable:
            if t > remaining:
                break  # achievable 升序，后续更大
            path[i] = t
            backtrack(i + 1, remaining - t)
            if truncated:
                return

    backtrack(0, target)

    if truncated:
        return GroupOutcome(
            group.appearance, True, True, None, {}, min_total, max_total
        )
    if count == 0:
        # 总数落在 [min_total, max_total] 区间内，但无法按每次粒数整组
        # 闭合到各药格（如每次 2 粒却找到 3 粒）——同样属于数量不闭合。
        return GroupOutcome(
            group.appearance, False, False, None, {}, min_total, max_total
        )
    return GroupOutcome(
        group.appearance,
        True,
        False,
        count,
        {c: sorted(vals) for c, vals in x_acc.items()},
        min_total,
        max_total,
    )
