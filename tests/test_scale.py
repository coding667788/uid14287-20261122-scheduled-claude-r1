"""规模与确定性测试：80 事务、30 分片、16 可隔离采集器。"""

from __future__ import annotations

import random
import time
import unittest

from solution.compare import run as compare_run
from solution.reconstruct import run as reconstruct_run
from solution.suspect import run as suspect_run

from helpers import parse_dict


def _build_large_case(seed: int) -> dict:
    rng = random.Random(seed)
    n_txns = 80
    n_shards = 30
    n_collectors = 16
    n_blocks = 24
    blocks = [{"id": f"B{b:02d}", "initial_digest": f"init-{b}"}
              for b in range(n_blocks)]
    collectors = [{"id": f"C{c:02d}", "trust_marker": f"TM-{c}"}
                  for c in range(n_collectors)]

    txns = []
    for i in range(n_txns):
        cid = f"C{i % n_collectors:02d}"
        prereqs = []
        if i > 0:
            # 链状因果 + 少量随机跨链边，保证 DAG
            prereqs.append(f"T{i - 1:03d}")
            if i >= 3 and rng.random() < 0.25:
                j = rng.randrange(max(0, i - 5), i - 1)
                if f"T{j:03d}" not in prereqs:
                    prereqs.append(f"T{j:03d}")
        # 每个事务写 1-2 个区块；同块写者通过链天然可比
        n_writes = rng.randint(1, 2)
        chosen = rng.sample(range(n_blocks), n_writes)
        writes = [{"block": f"B{b:02d}",
                   "digest": f"d-{b:02d}-T{i:03d}"} for b in chosen]
        txns.append({
            "id": f"T{i:03d}", "collector": cid,
            "local_seq": i // n_collectors,
            "causal_after": prereqs,
            "actor": f"ACT-{cid}", "writes": writes,
        })

    shards = []
    for s in range(n_shards):
        cid = f"C{s % n_collectors:02d}"
        must_inc = [f"T{rng.randrange(n_txns):03d}"] if rng.random() < 0.5 else []
        must_exc = ([f"T{rng.randrange(n_txns):03d}"]
                    if rng.random() < 0.3 and not must_inc else [])
        obs_block = rng.randrange(n_blocks)
        # 观察摘要从该块历史中挑一个，保证大多数可满足
        history = ["init-" + str(obs_block)]
        for t in txns:
            for w in t["writes"]:
                if w["block"] == f"B{obs_block:02d}":
                    history.append(w["digest"])
        shards.append({
            "id": f"S{s:02d}", "collector": cid, "revocable": True,
            "barriers": {"must_include": must_inc, "must_exclude": must_exc},
            "observations": [{"block": f"B{obs_block:02d}",
                              "digest": rng.choice(history)}],
        })

    costs = {f"C{c:02d}": rng.randint(1, 20) for c in range(n_collectors)}
    return {
        "case_id": "large",
        "blocks": blocks, "collectors": collectors,
        "transactions": txns, "shards": shards,
        "suspect": {"isolation_costs": costs, "fixed_evidence": []},
        "compare": {
            "witness_blocks": [f"B{b:02d}" for b in range(6)],
            "slices": [[], [t["id"] for t in txns[:10]]],
            "preservation": {f"B{b:02d}": f"init-{b}" for b in range(6)}},
    }


def _build_feasible_large_case(seed: int = 7) -> dict:
    """保证整体可行的大规模案例：所有分片观察完整历史末端摘要，无屏障冲突。"""
    rng = random.Random(seed)
    n_txns, n_shards, n_collectors, n_blocks = 80, 30, 16, 24
    blocks = [{"id": f"B{b:02d}", "initial_digest": f"init-{b:02d}"}
              for b in range(n_blocks)]
    collectors = [{"id": f"C{c:02d}"} for c in range(n_collectors)]
    txns = []
    for i in range(n_txns):
        cid = f"C{i % n_collectors:02d}"
        prereqs = [f"T{i - 1:03d}"] if i > 0 else []
        b = i % n_blocks
        txns.append({
            "id": f"T{i:03d}", "collector": cid, "local_seq": i // n_collectors,
            "causal_after": prereqs, "actor": f"ACT-{cid}",
            "writes": [{"block": f"B{b:02d}", "digest": f"final-{b:02d}"}]})
    # 每块的末端摘要统一为 final-XX；观察它即允许完整前缀，整体必可行
    shards = [{
        "id": f"S{s:02d}", "collector": f"C{s % n_collectors:02d}",
        "revocable": True,
        "barriers": {"must_include": [], "must_exclude": []},
        "observations": [{"block": f"B{s % n_blocks:02d}",
                          "digest": f"final-{(s % n_blocks):02d}"}]}
        for s in range(n_shards)]
    return {
        "case_id": "large-feasible",
        "blocks": blocks, "collectors": collectors,
        "transactions": txns, "shards": shards,
        "compare": {
            "witness_blocks": [f"B{b:02d}" for b in range(6)],
            "slices": [[], [t["id"] for t in txns]],
            "preservation": {f"B{b:02d}": f"init-{b:02d}" for b in range(6)}},
    }


class ScaleTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.raw = _build_large_case(20261122)
        cls.case = parse_dict(cls.raw)

    def test_dimensions(self):
        self.assertEqual(len(self.case.txns), 80)
        self.assertEqual(len(self.case.shards), 30)
        self.assertEqual(len({s.collector for s in self.case.shards}), 16)

    def test_reconstruct_performance_and_determinism(self):
        t0 = time.perf_counter()
        r1 = reconstruct_run(self.case)
        r2 = reconstruct_run(self.case)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 10.0)
        self.assertEqual(r1["conflict_core"], r2["conflict_core"])
        self.assertEqual(r1["removal_trace"], r2["removal_trace"])
        # 移除轨迹严格按分片 ID 降序
        order = [t["shard"] for t in r1["removal_trace"]]
        self.assertEqual(order, sorted(order, reverse=True))

    def test_suspect_performance_and_exhaustive_skipped_at_scale(self):
        # 16 个可隔离采集器 > EXHAUSTIVE_LIMIT，不做完整穷举
        from solution import suspect as suspect_mod
        self.assertGreater(16, suspect_mod.EXHAUSTIVE_LIMIT)
        t0 = time.perf_counter()
        result = suspect_run(self.case)
        self.assertLess(time.perf_counter() - t0, 10.0)
        self.assertTrue(result["feasible"])
        self.assertIsNone(result["exhaustive_check"])
        # 缓存确实被复用（搜索共享生效）
        self.assertGreater(result["feasibility_cache_hits"], 1)

    def test_compare_performance(self):
        feasible_case = parse_dict(_build_feasible_large_case())
        t0 = time.perf_counter()
        result = compare_run(feasible_case)
        self.assertLess(time.perf_counter() - t0, 10.0)
        self.assertTrue(result["evidence_feasible"])
        joint = result["joint_max_mismatch"]
        self.assertTrue(joint["feasible"])
        self.assertGreaterEqual(joint["max_mismatch"], 0)
        self.assertLessEqual(joint["max_mismatch"],
                             len(result["block_possibilities"]))

    def test_no_full_permutation_iteration(self):
        # 80! 不可能遍历；冲突核引导 B&B 只访问远小于 2^16 的节点，
        # 秒级完成本身即证明未做完整排列枚举。
        from solution.suspect import run as suspect_run2
        t0 = time.perf_counter()
        result = suspect_run2(self.case)
        self.assertLess(time.perf_counter() - t0, 10.0)
        self.assertTrue(result["feasible"])


if __name__ == "__main__":
    unittest.main()
