"""证据引擎：把分片证据编译为 Horn 公式并求最小可行切片。

摘要观察如何变成 Horn 子句
--------------------------

设区块 b 的写者按因果链为 w_1 < w_2 < ... < w_k（结构校验已保证全序），
切片中该区块的“真实前缀” p = 被纳入的写者个数（0..k），其摘要由最后一个
被纳入写者（或初始来源）决定。观察摘要 d 允许的前缀集合为
``M = {p | 前缀 p 下 b 的摘要恰为 d}``。

* 禁止前缀 p>=1：若 w_1..w_p 全部纳入则违反 ⇒ 目标子句
  ``¬x(w_1) | ... | ¬x(w_p)``；
* 禁止前缀 p=0：初始摘要就不匹配且没有任何允许前缀时，产生空目标（恒假）。

由于 w_i 沿链满足 ``x(w_{i+1}) => x(w_i)``，真实前缀只有一个，
上述目标子句集合恰好排除所有不允许的前缀。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from .horn import Formula, mask_to_sorted_vars
from .model import Case, Shard


@dataclass
class SolveResult:
    feasible: bool
    txn_mask: int
    txn_ids: list[str]
    conflict: dict | None


class EvidenceEngine:
    """持有一个已解析案例，支持对“当前生效分片集合”反复求解。

    DAG 边子句与摘要/屏障子句分开构建：
    后者以分片 ID 为键缓存，多个命令在不同分片子集上复用同一份编译结果，
    这是搜索共享的主要来源。
    """

    def __init__(self, case: Case) -> None:
        self.case = case
        self.n = len(case.txns)
        self._shard_clauses: dict[str, list[tuple[tuple[int, ...], int | None, dict]]] = {}
        self._compile_shards()

    # ------------------------------------------------------------------ #
    # 编译
    # ------------------------------------------------------------------ #

    def _dag_clauses(self, f: Formula) -> None:
        """因果边 v 依赖 u（u 先于 v）：x_v => x_u。"""
        c = self.case
        for tid in c.order:
            v = c.pos[tid]
            for p in c.index[tid].prereqs:
                f.add_clause((v,), c.pos[p],
                             {"kind": "dag", "txn": tid, "prereq": p})

    def _local_order_clauses(self, f: Formula) -> None:
        """同采集器本地序号产生的全序边（与因果边去重）。"""
        c = self.case
        seen: set[tuple[int, int]] = set()
        for tid in c.order:
            v = c.pos[tid]
            for p in c.index[tid].prereqs:
                seen.add((v, c.pos[p]))
        by_collector: dict[str, list] = {}
        for t in c.txns:
            by_collector.setdefault(t.collector, []).append(t)
        for group in by_collector.values():
            ordered = sorted(group, key=lambda x: x.seq)
            for a, b in zip(ordered, ordered[1:]):
                edge = (c.pos[b.tid], c.pos[a.tid])
                if edge not in seen:
                    seen.add(edge)
                    f.add_clause((edge[0],), edge[1],
                                 {"kind": "local_order", "txn": b.tid,
                                  "prereq": a.tid})

    def _digest_clauses(self, f: Formula, shard: Shard) -> None:
        c = self.case
        for bid, observed in shard.observations:
            writers = c.block_writers[bid]
            k = len(writers)
            # 每个前缀 p（0..k）对应的摘要
            digest_at: list[str] = [c.initial_digest[bid]]
            for w in writers:
                digest_at.append(c.index[w].writes[bid])
            allowed = {p for p, d in enumerate(digest_at) if d == observed}
            origin_base = {"kind": "digest", "shard": shard.sid, "block": bid,
                           "observed": observed}
            if not allowed:
                # 没有任何前缀产生该摘要：观察不可能满足，恒假目标
                f.add_clause((), None,
                             {**origin_base, "reason": "digest_unreachable"})
                continue
            for p in range(k + 1):
                if p in allowed:
                    continue
                if p == 0:
                    # 禁止空前缀：至少一个写者必须纳入 ⇒ 单位正子句 x(w_1)
                    f.add_clause((), c.pos[writers[0]],
                                 {**origin_base, "reason": "require_some_writer",
                                  "first_writer": writers[0]})
                elif p < k:
                    # 禁止“恰为前缀 p”：链蕴含保证前缀只取一个值。
                    # 若前 p 个写者都在而 w_{p+1} 不在，真实前缀就是 p。
                    # 强制其继续延伸：(w_1∧..∧w_p) => w_{p+1}（确定 Horn 子句）。
                    ants = tuple(c.pos[w] for w in writers[:p])
                    f.add_clause(ants, c.pos[writers[p]],
                                 {**origin_base, "reason": "forbidden_prefix_extend",
                                  "prefix": p, "next_writer": writers[p]})
                else:
                    # p == k：禁止完整前缀（全部写者纳入），用目标子句
                    ants = tuple(c.pos[w] for w in writers)
                    f.add_clause(ants, None,
                                 {**origin_base, "reason": "forbidden_full_prefix",
                                  "prefix": p, "last_writer": writers[p - 1]})

    def _barrier_clauses(self, f: Formula, shard: Shard) -> None:
        c = self.case
        for tid in shard.must_include:
            f.add_clause((), c.pos[tid],
                         {"kind": "barrier_include", "shard": shard.sid, "txn": tid})
        for tid in shard.must_exclude:
            f.add_clause((c.pos[tid],), None,
                         {"kind": "barrier_exclude", "shard": shard.sid, "txn": tid})

    def _compile_shards(self) -> None:
        """把每个分片各自的子句编译成可复用描述（不含 DAG/本地序子句）。"""
        for s in self.case.shards:
            probe = Formula(self.n)
            self._barrier_clauses(probe, s)
            self._digest_clauses(probe, s)
            self._shard_clauses[s.sid] = [
                (cl.antecedents, cl.consequent, cl.origin) for cl in probe.clauses
            ]

    def build_formula(self, active_ids: tuple[str, ...]) -> Formula:
        f = Formula(self.n)
        self._dag_clauses(f)
        self._local_order_clauses(f)
        for sid in active_ids:
            for ants, cons, origin in self._shard_clauses[sid]:
                f.add_clause(ants, cons, origin)
        return f

    # ------------------------------------------------------------------ #
    # 求解
    # ------------------------------------------------------------------ #

    @lru_cache(maxsize=None)
    def solve(self, active_ids: tuple[str, ...]) -> SolveResult:
        f = self.build_formula(active_ids)
        ok, mask, clause = f.least_model()
        if not ok:
            return SolveResult(False, 0, [], self._conflict(clause))
        ids = [self.case.order[i] for i in mask_to_sorted_vars(mask)]
        return SolveResult(True, mask, ids, None)

    def _conflict(self, clause) -> dict:
        origin = dict(clause.origin)
        c = self.case
        txns = [c.order[i] for i in clause.antecedents]
        return {"origin": origin, "antecedent_txns": txns,
                "consequent_txn": (c.order[clause.consequent]
                                   if clause.consequent is not None else None)}

    # ------------------------------------------------------------------ #
    # 切片工具
    # ------------------------------------------------------------------ #

    def slice_to_mask(self, txn_ids: list[str]) -> int:
        mask = 0
        for t in txn_ids:
            mask |= 1 << self.case.pos[t]
        return mask

    def check_ideal(self, txn_ids: list[str]) -> tuple[bool, list[str]]:
        """检查给定事务集合是否为偏序理想（纳入事务须纳入全部先决项）。"""
        c = self.case
        mask = self.slice_to_mask(txn_ids)
        missing: set[str] = set()
        for tid in txn_ids:
            u = c.pos[tid]
            need = c.down[u] & ~mask
            for v in mask_to_sorted_vars(need):
                missing.add(c.order[v])
        return (not missing), sorted(missing)

    def block_state(self, block: str, txn_mask: int) -> dict:
        """区块在给定切片下的最后写入者（或初始来源）与摘要。

        摘要即使被改回原值，最后写入者仍保留为最后一个被纳入的写事务。
        """
        c = self.case
        last_writer = None
        for w in c.block_writers[block]:
            if (txn_mask >> c.pos[w]) & 1:
                last_writer = w
        if last_writer is None:
            return {
                "block": block,
                "source_kind": "initial",
                "source": c.initial_source[block],
                "writer_txn": None,
                "collector": None,
                "digest": c.initial_digest[block],
            }
        t = c.index[last_writer]
        return {
            "block": block,
            "source_kind": "transaction",
            "source": t.actor,
            "writer_txn": last_writer,
            "collector": t.collector,
            "digest": t.writes[block],
        }

    def all_collected_blocks(self, active_ids: tuple[str, ...]) -> list[str]:
        seen: set[str] = set()
        active = set(active_ids)
        for s in self.case.shards:
            if s.sid in active:
                for b, _ in s.observations:
                    seen.add(b)
        return [b for b in self.case.block_ids if b in seen]
