"""reconstruct：整组分片能否来自同一切片。

冲突集提取（按题目指定的确定性贪心）：

1. 以全部分片为当前集合，事务日志与既定偏序始终保留，绝不删除；
2. 按分片 ID 降序，逐个尝试移除：若移除后**仍然无解**，则永久移除；
   若移除后变得有解，则恢复该分片；
3. 若全程有解，冲突集为空；否则最终仍保留的分片构成一个包含极小
   （inclusion-minimal）冲突集——每一个被永久移除的分片都经过
   “移除它依旧冲突”的验证，因此最终集合中的分片缺一不可。

可行时返回最小切片。由于约束全部是 Horn 子句，事务数最少的切片唯一，
“同数取字典序最小事务 ID 列表”的决胜规则自然满足。
"""

from __future__ import annotations

from .engine import EvidenceEngine
from .model import Case


def run(case: Case) -> dict:
    engine = EvidenceEngine(case)
    all_ids = sorted((s.sid for s in case.shards), reverse=True)  # 降序尝试
    retained = list(all_ids)
    removed_trace: list[dict] = []

    def feasible(ids: list[str]) -> bool:
        return engine.solve(tuple(sorted(ids))).feasible

    initially_feasible = feasible(retained)

    if initially_feasible:
        conflict_core: list[str] = []
    else:
        for sid in all_ids:
            trial = [x for x in retained if x != sid]
            if not feasible(trial):
                # 移除后依然无解：永久移除
                retained = trial
                removed_trace.append({"shard": sid, "permanently_removed": True,
                                      "still_infeasible": True})
            else:
                # 移除后有解：恢复，该分片属于冲突集
                removed_trace.append({"shard": sid, "permanently_removed": False,
                                      "still_infeasible": False})
        conflict_core = sorted(retained)

    final_ids = tuple(sorted(retained))
    result = engine.solve(final_ids)

    response: dict = {
        "command": "reconstruct",
        "case_id": case.case_id,
        "feasible": result.feasible,
        "conflict_core": conflict_core,
        "removal_trace": removed_trace,
        "retained_shards": sorted(retained),
        "dropped_shards": sorted(set(all_ids) - set(retained)),
    }

    if result.feasible:
        collected = engine.all_collected_blocks(final_ids)
        response["slice"] = {
            "transaction_count": len(result.txn_ids),
            "transaction_ids": result.txn_ids,
            "tie_break": "lex_min",   # Horn 最小模型唯一，决胜自动满足
        }
        response["block_states"] = [
            engine.block_state(b, result.txn_mask) for b in collected
        ]
    else:
        # retained 仍无解：返回可复现的冲突解释（子句溯源）
        response["slice"] = None
        response["conflict_detail"] = result.conflict
    return response
