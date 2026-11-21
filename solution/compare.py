"""compare：两个候选切片的逐块核查 + 同一切片下的联合最大差异。

关键区别
--------

逐块枚举“可能来源/摘要”后直接相加是错误的：不同块的选择由同一组事务变量
耦合（原子多区块事务、因果边、共享写者）。本模块在**同一个可行切片**上做
联合优化：

* 决策变量只取见证区块的“写者链前缀”，每次决策同时给出强制纳入的正变量
  与一个 Horn 目标子句（禁止更深的前缀）；
* 每个节点调用 Horn 最小模型做闭包传播与冲突检测，冲突即剪枝；
* 记忆化（已强制变量集合 + 已加目标子句）消除等价分支；
* 优先探索“与保全镜像不一致”的分支以尽早建立上界；
* 决胜：差异数最多 → 切片事务数最少 → 事务 ID 列表字典序最小。
"""

from __future__ import annotations

from .engine import EvidenceEngine
from .horn import Formula, mask_to_sorted_vars
from .model import Case


def _prefix_options(case: Case, block: str, observed_target: str | None):
    """返回该区块每个前缀 p 的 (正变量, 禁止更深前缀的目标, 是否不一致)。"""
    writers = case.block_writers[block]
    digest_at = [case.initial_digest[block]]
    for w in writers:
        digest_at.append(case.index[w].writes[block])
    options = []
    for p in range(len(writers) + 1):
        pos_vars = tuple(case.pos[w] for w in writers[:p])
        neg_goal = (tuple(case.pos[w] for w in writers[: p + 1])
                    if p < len(writers) else ())
        mismatch = observed_target is not None and digest_at[p] != observed_target
        options.append({"prefix": p, "pos": pos_vars, "neg_goal": neg_goal,
                        "mismatch": mismatch, "digest": digest_at[p]})
    return options


def run(case: Case) -> dict:
    cfg = case.compare_cfg
    if cfg is None:
        return {"command": "compare", "case_id": case.case_id,
                "error": "输入缺少 compare 段"}

    engine = EvidenceEngine(case)
    active = tuple(sorted(s.sid for s in case.shards))
    base = engine.solve(active)
    witness = cfg["witness_blocks"]
    preservation = cfg["preservation"]

    # ---------------------------------------------------------------- #
    # 1) 两个候选切片核查（理想性 + 在当前证据下是否可行）
    # ---------------------------------------------------------------- #
    formula = engine.build_formula(active)
    candidate_reports = []
    for idx, txn_ids in enumerate(cfg["slices"]):
        is_ideal, missing = engine.check_ideal(txn_ids)
        mask = engine.slice_to_mask(txn_ids)
        satisfies = is_ideal and _mask_satisfies(formula, mask)
        blocks = []
        for b in witness:
            st = dict(engine.block_state(b, mask))
            st["matches_preservation"] = (
                preservation.get(b) is None or st["digest"] == preservation[b])
            blocks.append(st)
        candidate_reports.append({
            "slice_index": idx,
            "transaction_ids": list(txn_ids),
            "is_ideal": is_ideal,
            "ideal_violations": missing,
            "satisfies_evidence": satisfies,
            "witness_block_states": blocks,
        })

    # ---------------------------------------------------------------- #
    # 2) 逐块“可出现的”来源与摘要（在任意可行切片中）
    # ---------------------------------------------------------------- #
    block_possibilities = []
    for b in witness:
        opts = _prefix_options(case, b, preservation.get(b))
        possibilities = []
        for o in opts:
            if o["prefix"] == 0:
                src = {"source_kind": "initial",
                       "source": case.initial_source[b],
                       "writer_txn": None, "collector": None}
            else:
                w = case.block_writers[b][o["prefix"] - 1]
                t = case.index[w]
                src = {"source_kind": "transaction", "source": t.actor,
                       "writer_txn": w, "collector": t.collector}
            possibilities.append({
                "prefix": o["prefix"], "digest": o["digest"],
                "mismatch_with_preservation": o["mismatch"], **src,
            })
        block_possibilities.append({"block": b, "possibilities": possibilities})

    # ---------------------------------------------------------------- #
    # 3) 联合最大差异搜索（同一切片）
    # ---------------------------------------------------------------- #
    joint = _joint_search(case, engine, formula, witness, preservation, base.feasible)

    naive_sum = sum(
        1 for entry in block_possibilities
        if any(p["mismatch_with_preservation"] for p in entry["possibilities"]))

    return {
        "command": "compare",
        "case_id": case.case_id,
        "evidence_feasible": base.feasible,
        "candidate_slices": candidate_reports,
        "block_possibilities": block_possibilities,
        "naive_per_block_mismatch_sum": naive_sum,
        "joint_max_mismatch": joint,
    }


def _mask_satisfies(f: Formula, mask: int) -> bool:
    """检查给定真值赋值是否满足公式的全部子句。"""
    for c in f.clauses:
        ants_true = all((mask >> a) & 1 for a in c.antecedents)
        if not ants_true:
            continue
        if c.consequent is None:
            return False
        if not ((mask >> c.consequent) & 1):
            return False
    return True


def _joint_search(case: Case, engine: EvidenceEngine, formula: Formula,
                  witness: list[str], preservation: dict[str, str],
                  evidence_feasible: bool) -> dict:
    if not evidence_feasible:
        return {"feasible": False, "max_mismatch": None, "slice": None,
                "per_block_mismatch": None,
                "note": "当前证据本身无可行切片，联合差异未定义"}

    # 每块候选前缀：不一致优先、深前缀优先，尽快建立高上界
    block_options: list[tuple[str, list[dict]]] = []
    for b in witness:
        opts = sorted(_prefix_options(case, b, preservation.get(b)),
                      key=lambda o: (not o["mismatch"], -o["prefix"]))
        block_options.append((b, opts))

    # 记忆化：状态 = (已强制正变量, 已附加目标子句)；
    # 值 = (最大差异数, 对应最小模型 mask)，不可行记 None。
    memo: dict[tuple[frozenset[int], tuple[tuple[int, ...], ...]],
               tuple[int, int] | None] = {}

    def dfs(idx: int, forced_pos: frozenset[int],
            goals: tuple[tuple[int, ...], ...],
            mismatch: int) -> tuple[int, int] | None:
        key = (forced_pos, goals)
        if key in memo:
            return memo[key]

        if idx == len(witness):
            f = _formula_with_goals(formula, goals)
            ok, mask, _c = f.least_model(forced_positive=list(forced_pos))
            memo[key] = (mismatch, mask) if ok else None
            return memo[key]

        best: tuple[int, int] | None = None
        _block, opts = block_options[idx]
        for o in opts:
            new_pos = forced_pos | frozenset(o["pos"])
            new_goals = goals + ((o["neg_goal"],) if o["neg_goal"] else ())
            f = _formula_with_goals(formula, new_goals)
            ok, _m, _c = f.least_model(forced_positive=list(new_pos))
            if not ok:
                continue
            sub = dfs(idx + 1, new_pos, new_goals,
                      mismatch + (1 if o["mismatch"] else 0))
            if sub is not None:
                best = _max_tie(best, sub)
        memo[key] = best
        return best

    result = dfs(0, frozenset(), (), 0)
    if result is None:
        return {"feasible": False, "max_mismatch": None, "slice": None,
                "per_block_mismatch": None,
                "note": "联合搜索未找到可行切片"}

    max_mismatch, mask = result
    txn_ids = [case.order[i] for i in mask_to_sorted_vars(mask)]

    per_block: dict[str, bool] = {}
    for b in witness:
        st = engine.block_state(b, mask)
        per_block[b] = (preservation.get(b) is not None
                        and st["digest"] != preservation[b])

    return {
        "feasible": True,
        "max_mismatch": max_mismatch,
        "slice": {
            "transaction_count": len(txn_ids),
            "transaction_ids": txn_ids,
            "tie_break": "max_mismatch_then_min_txns_then_lex",
        },
        "per_block_mismatch": per_block,
    }


def _max_tie(a: tuple[int, int] | None, b: tuple[int, int]) -> tuple[int, int]:
    if a is None:
        return b
    if b[0] != a[0]:
        return a if a[0] > b[0] else b
    if b[1].bit_count() != a[1].bit_count():
        return a if a[1].bit_count() < b[1].bit_count() else b
    return a if mask_to_sorted_vars(a[1]) <= mask_to_sorted_vars(b[1]) else b


def _formula_with_goals(base: Formula,
                        extra_goals: tuple[tuple[int, ...], ...]) -> Formula:
    """在基础公式上附加额外目标子句（复用确定子句的触发结构）。"""
    f = Formula(base.n)
    f.clauses = list(base.clauses)
    f.triggers = [list(t) for t in base.triggers]
    f.goals = list(base.goals)
    for g in extra_goals:
        f.add_clause(g, None, {"kind": "search_decision"})
    return f
