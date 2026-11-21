"""reconstruct 命令测试：最小切片、重回原摘要、极小冲突集、降序移除。"""

from __future__ import annotations

import copy
import unittest

from solution.reconstruct import run

from helpers import load_example, parse_dict


class ReconstructFeasibleTests(unittest.TestCase):

    def setUp(self):
        self.case = load_example("case_reconstruct.json")
        self.result = run(self.case)

    def test_feasible_and_minimal_slice(self):
        self.assertTrue(self.result["feasible"])
        self.assertEqual(self.result["conflict_core"], [])
        # 最小切片：T5 的下闭包 = T1..T5，T6 被 S4 排除
        self.assertEqual(self.result["slice"]["transaction_ids"],
                         ["T1", "T2", "T3", "T4", "T5"])
        self.assertEqual(self.result["slice"]["transaction_count"], 5)

    def test_reversion_keeps_last_writer(self):
        # B0 摘要被 T4 改回 h0-a，但最后写入者必须是 T4 而非初始来源
        b0 = next(b for b in self.result["block_states"] if b["block"] == "B0")
        self.assertEqual(b0["digest"], "h0-a")
        self.assertEqual(b0["writer_txn"], "T4")
        self.assertEqual(b0["source"], "ACT-GAMMA")
        self.assertEqual(b0["source_kind"], "transaction")

    def test_uncollected_block_not_reported(self):
        reported = {b["block"] for b in self.result["block_states"]}
        # 所有块都被至少一个保留分片采集
        self.assertEqual(reported, {"B0", "B1", "B2", "B3"})

    def test_initial_source_when_no_writer_included(self):
        raw = copy.deepcopy(self.case.raw)
        # 只保留一个观察初始摘要的新分片
        raw["shards"] = [{
            "id": "SX", "collector": "C1", "revocable": True,
            "barriers": {"must_include": [], "must_exclude": ["T1"]},
            "observations": [{"block": "B3", "digest": "h3"}]}]
        case = parse_dict(raw)
        result = run(case)
        self.assertTrue(result["feasible"])
        b3 = next(b for b in result["block_states"] if b["block"] == "B3")
        self.assertEqual(b3["source_kind"], "initial")
        self.assertIsNone(b3["writer_txn"])
        self.assertEqual(b3["digest"], "h3")


class ReconstructConflictTests(unittest.TestCase):

    def setUp(self):
        self.case = load_example("case_conflict.json")
        self.result = run(self.case)

    def test_infeasible_reports_minimal_conflict_core(self):
        self.assertFalse(self.result["feasible"])
        # S1 强制 T2，S2 排除 T2，二者构成极小冲突
        self.assertEqual(self.result["conflict_core"], ["S1", "S2"])
        self.assertIsNone(self.result["slice"])
        self.assertIsNotNone(self.result["conflict_detail"])

    def test_removal_trace_is_descending_and_deterministic(self):
        trace = self.result["removal_trace"]
        shard_order = [t["shard"] for t in trace]
        self.assertEqual(shard_order, sorted(shard_order, reverse=True))
        # S3 移除后仍无解，被永久移除
        s3 = next(t for t in trace if t["shard"] == "S3")
        self.assertTrue(s3["permanently_removed"])
        self.assertTrue(s3["still_infeasible"])
        # 冲突集中的分片移除后有解，被保留
        for sid in ("S1", "S2"):
            t = next(x for x in trace if x["shard"] == sid)
            self.assertFalse(t["permanently_removed"])

    def test_conflict_core_is_inclusion_minimal(self):
        # 冲突集中每个分片单独移除后都应变得可行
        from solution.engine import EvidenceEngine
        engine = EvidenceEngine(self.case)
        core = set(self.result["conflict_core"])
        for sid in core:
            remaining = tuple(sorted(core - {sid}))
            self.assertTrue(engine.solve(remaining).feasible,
                            f"冲突集非包含极小：移除 {sid} 后仍不可行")


class ReconstructTieBreakTests(unittest.TestCase):

    def test_least_horn_model_is_unique_minimum(self):
        # 两个独立无约束事务：最小切片必须为空（事务数 0）
        raw = {
            "case_id": "tie", "blocks": [{"id": "B", "initial_digest": "d"}],
            "collectors": [{"id": "C"}],
            "transactions": [
                {"id": "TA", "collector": "C", "local_seq": 0,
                 "causal_after": [], "writes": [{"block": "B", "digest": "d1"}]},
                {"id": "TB", "collector": "C", "local_seq": 1,
                 "causal_after": ["TA"], "writes": [{"block": "B", "digest": "d2"}]}],
            "shards": [],
        }
        result = run(parse_dict(raw))
        self.assertTrue(result["feasible"])
        self.assertEqual(result["slice"]["transaction_ids"], [])


if __name__ == "__main__":
    unittest.main()
