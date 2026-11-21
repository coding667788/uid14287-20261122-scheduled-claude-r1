"""suspect：推断可能受污染的采集器集合，求最小成本隔离方案。

隔离语义
--------

* 隔离采集器 c：仅去除 c 名下的**可撤分片**；固定证据（revocable=false，
  含 fixed_evidence 名单）永远生效，与采集器是否隔离无关；
* 目标：让剩余分片存在同一切片（reconstruct 意义下可行）；
* 决胜：隔离总成本最小 → 隔离台数最少 → 隔离 ID 列表字典序最小。

搜索与最优性
------------

可行性关于“保留的分片集合”单调：移除分片只会把不可行变为可行。搜索采用
**冲突核引导的分支限界**，保证完备且不枚举完整事件排列：

1. 对当前保留分片集合求一个包含极小（inclusion-minimal）冲突核：按分片 ID
   降序贪心移除，移除后仍不可行的永久移除，保留者即冲突核（与 reconstruct
   同一确定性过程，结果被缓存复用）；
2. 要恢复可行性，至少要隔离一个“拥有冲突核中可撤分片”的采集器，因此只在
   这些采集器上做 0/1 分支，其余采集器无需考虑——这是强剪枝但不丢解：
   任何可行隔离方案都必须切断当前冲突核；
3. 冲突核只含固定证据时，任何隔离都无法恢复 => 该分支立即判死；
4. 界剪枝：累计成本超过已知最优（同成本且台数不更少）时剪枝；
5. 全部可行性/冲突核查询带缓存，跨分支共享。

小规模（可隔离采集器 <= 12）时另做一次完全独立的子集穷举核对，
两者不一致直接断言失败。
"""

from __future__ import annotations

from itertools import combinations

from .engine import EvidenceEngine
from .model import Case

EXHAUSTIVE_LIMIT = 12


def run(case: Case) -> dict:
    cfg = case.suspect_cfg
    if cfg is None:
        return {"command": "suspect", "case_id": case.case_id,
                "error": "输入缺少 suspect 段"}

    engine = EvidenceEngine(case)
    costs: dict[str, int] = cfg["costs"]
    fixed_ids = frozenset(s.sid for s in case.shards if not s.revocable)
    fixed_evidence = frozenset(cfg["fixed_evidence"]) | fixed_ids

    # 采集器 -> 名下可撤分片（frozenset，便于 O(台数) 集合运算）
    shards_by_collector: dict[str, frozenset[str]] = {}
    owner_of_shard: dict[str, str] = {}
    for s in case.shards:
        if s.revocable:
            shards_by_collector.setdefault(s.collector, set()).add(s.sid)
            owner_of_shard[s.sid] = s.collector
    shards_by_collector = {c: frozenset(v) for c, v in shards_by_collector.items()}
    isolatable = sorted(shards_by_collector)
    cost_of = {c: costs[c] for c in isolatable}
    all_revocable = frozenset(owner_of_shard)

    # 确定性的降序分片顺序，冲突核计算复用
    descending_shards = sorted(all_revocable, reverse=True)

    feasibility_cache: dict[frozenset[str], bool] = {}
    core_cache: dict[frozenset[str], frozenset[str]] = {}

    def active_set(isolated: frozenset[str]) -> frozenset[str]:
        kept = frozenset().union(*(
            shards_by_collector[c] for c in isolatable if c not in isolated)) \
            if isolatable else frozenset()
        return fixed_evidence | kept

    def feasible(active: frozenset[str]) -> bool:
        if active not in feasibility_cache:
            feasibility_cache[active] = engine.solve(tuple(sorted(active))).feasible
        return feasibility_cache[active]

    def conflict_core(active: frozenset[str]) -> frozenset[str]:
        """对当前活跃分片求包含极小冲突核（降序贪心，确定性）。"""
        if active in core_cache:
            return core_cache[active]
        retained = set(active)
        for sid in descending_shards:
            if sid not in retained:
                continue
            trial = retained - {sid}
            if not feasible(frozenset(trial)):
                retained.discard(sid)  # 移除后仍不可行：永久移除
        core = frozenset(retained)
        core_cache[active] = core
        return core

    fixed_only_active = fixed_evidence
    fixed_only_feasible = feasible(fixed_only_active)

    best: dict | None = None

    def better(cost: int, isolated: tuple[str, ...]) -> bool:
        if best is None:
            return True
        if cost != best["cost"]:
            return cost < best["cost"]
        if len(isolated) != len(best["isolated"]):
            return len(isolated) < len(best["isolated"])
        return list(isolated) < best["isolated"]

    def dfs(isolated: frozenset[str], cost: int, count: int) -> None:
        nonlocal best
        # 界剪枝：代价非负，累计成本一旦严格超过已知最优成本，后续只会更差。
        # 注意不能在“同成本”时按台数剪枝：零代价隔离不增加成本，后续分支
        # 仍可能以相同成本得到更少台数或更优字典序。
        if best is not None and cost > best["cost"]:
            return

        active = active_set(isolated)
        if feasible(active):
            # 单调性：继续隔离只会增加成本/台数，当前即该分支最优
            iso_sorted = sorted(isolated)
            if better(cost, tuple(iso_sorted)):
                best = {"isolated": iso_sorted, "cost": cost,
                        "collector_count": count}
            return

        core = conflict_core(active)
        # 冲突核中可撤分片的拥有者，按 ID 排序保证确定性
        candidate_collectors = sorted({
            owner_of_shard[sid] for sid in core if sid in owner_of_shard})
        if not candidate_collectors:
            # 冲突核全是固定证据：隔离无法恢复，判死
            return

        for collector in candidate_collectors:
            if collector in isolated:
                continue
            dfs(isolated | {collector},
                cost + cost_of[collector], count + 1)

    if fixed_only_feasible:
        dfs(frozenset(), 0, 0)

    # ---------------------------------------------------------------- #
    # 小规模独立穷举核对（联合选择的完整枚举，仅用于校验搜索正确性）
    # ---------------------------------------------------------------- #
    def feasible_when(isolated: frozenset[str]) -> bool:
        return feasible(active_set(isolated))

    exhaustive = None
    if len(isolatable) <= EXHAUSTIVE_LIMIT:
        ref = _exhaustive(isolatable, cost_of, feasible_when)
        exhaustive = {
            "method": "full_subset_enumeration",
            "isolated": ref["isolated"] if ref else None,
            "cost": ref["cost"] if ref else None,
            "matches_search": (
                ref is not None and best is not None
                and ref["isolated"] == best["isolated"]
                and ref["cost"] == best["cost"])
                or (ref is None and best is None)}
        if not exhaustive["matches_search"]:
            raise AssertionError(
                f"suspect 搜索与独立穷举不一致: search={best}, exhaustive={ref}")

    if best is None:
        return {
            "command": "suspect",
            "case_id": case.case_id,
            "feasible": False,
            "note": "即使隔离全部可隔离采集器，固定证据仍无同一切片",
            "fixed_evidence_shards": sorted(fixed_evidence),
            "feasibility_cache_hits": len(feasibility_cache),
            "exhaustive_check": exhaustive,
        }

    isolated_set = frozenset(best["isolated"])
    active = active_set(isolated_set)
    solve = engine.solve(tuple(sorted(active)))

    shard_explanations = []
    for s in sorted(case.shards, key=lambda x: x.sid):
        if not s.revocable:
            status = "kept_fixed"
        elif s.collector in isolated_set:
            status = "removed_by_isolation"
        else:
            status = "kept_evidence"
        shard_explanations.append({
            "shard": s.sid, "collector": s.collector,
            "revocable": s.revocable, "status": status,
        })

    return {
        "command": "suspect",
        "case_id": case.case_id,
        "feasible": True,
        "optimal_isolation": {
            "isolated_collectors": best["isolated"],
            "total_cost": best["cost"],
            "collector_count": best["collector_count"],
            "tie_break": "min_cost_then_min_count_then_lex",
        },
        "remaining_slice": {
            "transaction_count": len(solve.txn_ids),
            "transaction_ids": solve.txn_ids,
        },
        "active_shards": sorted(active),
        "fixed_evidence_shards": sorted(fixed_evidence),
        "shard_explanations": shard_explanations,
        "feasibility_cache_hits": len(feasibility_cache),
        "exhaustive_check": exhaustive,
    }


def _exhaustive(isolatable: list[str], cost_of: dict[str, int],
                feasible_when) -> dict | None:
    """完全独立的子集穷举（按决胜序枚举），用于交叉核对。"""
    best = None
    n = len(isolatable)
    for r in range(n + 1):
        for combo in combinations(isolatable, r):
            iso = frozenset(combo)
            if not feasible_when(iso):
                continue
            cost = sum(cost_of[c] for c in combo)
            key = (cost, r, list(combo))
            if best is None or key < best[0]:
                best = (key, {"isolated": list(combo), "cost": cost})
    return best[1] if best else None
