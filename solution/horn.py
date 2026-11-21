"""Horn 子句引擎。

约束编码（n 个事务，变量 x_i 表示拓扑位 i 的事务被纳入切片）：

* 单位正子句 ``x_i``：屏障 must_include；
* 单位负子句 ``¬x_i``：屏障 must_exclude；
* DAG 边 ``u -> v`` 编码为 ``x_v => x_u``，即 ``(¬x_v | x_u)``；
* 摘要观察只产生“由链上前缀定义的 Horn 子句”（见 engine 模块）。

全部子句均为确定/负 Horn 子句，因此：

1. 可满足性可由 BFS 前向链在线性时间判定；
2. 所有可满足模型的交仍是模型，故事务数最少的切片**唯一**，
   即从单位正子句出发的最小模型，无需字典序决胜（决胜规则仍在输出层保留）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Clause:
    """至多含一个正字面量的 Horn 子句：antecedents 同时成立 => consequent 成立。

    consequent 为 None 时是纯负目标子句（antecedents 不得同时成立）。
    """

    antecedents: tuple[int, ...]
    consequent: int | None
    origin: dict


class Formula:
    def __init__(self, n: int) -> None:
        self.n = n
        self.clauses: list[Clause] = []
        # triggers[v]：变量 v 成立时需要递减计数的 (consequent, clause_index)
        self.triggers: list[list[tuple[int, int]]] = [[] for _ in range(n)]
        self.goals: list[tuple[frozenset[int], int]] = []

    def add_clause(self, antecedents: tuple[int, ...], consequent: int | None,
                   origin: dict) -> int:
        idx = len(self.clauses)
        self.clauses.append(Clause(tuple(antecedents), consequent, origin))
        if consequent is None:
            self.goals.append((frozenset(antecedents), idx))
        else:
            for a in antecedents:
                self.triggers[a].append((consequent, idx))
        return idx

    def add_unit_positive(self, var: int, origin: dict) -> int:
        return self.add_clause((), var, origin)

    def add_unit_negative(self, var: int, origin: dict) -> int:
        return self.add_clause((var,), None, origin)

    def least_model(self, forced_positive: list[int] | None = None
                    ) -> tuple[bool, int, Clause | None]:
        """从单位正子句（含外部强制纳入）出发求最小模型。

        Returns:
            (sat, true_mask, conflict_clause)
        """
        true_mask = 0
        queue: list[int] = []
        remaining = [len(c.antecedents) for c in self.clauses]

        def enqueue(var: int) -> None:
            nonlocal true_mask
            if not ((true_mask >> var) & 1):
                true_mask |= 1 << var
                queue.append(var)

        # 所有空前件的确定子句立即触发
        for idx, c in enumerate(self.clauses):
            if c.consequent is not None and not c.antecedents:
                enqueue(c.consequent)
        if forced_positive:
            for v in forced_positive:
                enqueue(v)

        head = 0
        while head < len(queue):
            v = queue[head]
            head += 1
            for consequent, idx in self.triggers[v]:
                remaining[idx] -= 1
                if remaining[idx] == 0:
                    enqueue(consequent)

        # 校验纯负目标子句
        for ants, idx in self.goals:
            if not ants:
                return False, true_mask, self.clauses[idx]
            if (true_mask & _mask_of(ants)) == _mask_of(ants):
                return False, true_mask, self.clauses[idx]
        return True, true_mask, None


def _mask_of(vars_: frozenset[int]) -> int:
    m = 0
    for v in vars_:
        m |= 1 << v
    return m


def mask_to_sorted_vars(mask: int) -> list[int]:
    out: list[int] = []
    i = 0
    while mask:
        if mask & 1:
            out.append(i)
        mask >>= 1
        i += 1
    return out
