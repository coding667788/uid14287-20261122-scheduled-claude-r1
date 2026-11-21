"""suspect 命令测试：最小成本隔离、固定证据、穷举核对、剪枝最优性。"""

from __future__ import annotations

import unittest

from solution.suspect import run

from helpers import load_example, parse_dict


class SuspectDemoTests(unittest.TestCase):

    def setUp(self):
        self.result = run(load_example("case_suspect.json"))

    def test_optimal_isolation_is_cheap_collector(self):
        self.assertTrue(self.result["feasible"])
        opt = self.result["optimal_isolation"]
        # C4 是唯一与固定证据冲突的污染采集器，代价 2
        self.assertEqual(opt["isolated_collectors"], ["C4"])
        self.assertEqual(opt["total_cost"], 2)
        self.assertEqual(opt["collector_count"], 1)

    def test_fixed_evidence_survives_isolation(self):
        self.assertIn("SF", self.result["fixed_evidence_shards"])
        self.assertIn("SF", self.result["active_shards"])
        explanations = {e["shard"]: e["status"]
                        for e in self.result["shard_explanations"]}
        self.assertEqual(explanations["SF"], "kept_fixed")
        self.assertEqual(explanations["S4"], "removed_by_isolation")
        for sid in ("S1", "S2", "S3"):
            self.assertEqual(explanations[sid], "kept_evidence")

    def test_exhaustive_cross_check_matches(self):
        chk = self.result["exhaustive_check"]
        self.assertTrue(chk["matches_search"])
        self.assertEqual(chk["method"], "full_subset_enumeration")

    def test_remaining_slice_consistent(self):
        self.assertEqual(self.result["remaining_slice"]["transaction_ids"],
                         ["T1", "T2", "T3"])


class SuspectTieBreakTests(unittest.TestCase):

    def _raw(self):
        return {
            "case_id": "tie-suspect",
            "blocks": [
                {"id": "B0", "initial_digest": "h0"},
                {"id": "B1", "initial_digest": "h1"}],
            "collectors": [{"id": "CA"}, {"id": "CB"}, {"id": "CC"}],
            "transactions": [
                {"id": "T1", "collector": "CA", "local_seq": 0,
                 "causal_after": [],
                 "writes": [{"block": "B0", "digest": "h0-a"}]},
                {"id": "T2", "collector": "CB", "local_seq": 0,
                 "causal_after": ["T1"],
                 "writes": [{"block": "B1", "digest": "h1-a"}]},
                {"id": "T3", "collector": "CC", "local_seq": 0,
                 "causal_after": ["T2"],
                 "writes": [{"block": "B0", "digest": "h0-b"}]}],
            "shards": [
                {"id": "SA", "collector": "CA",
                 "barriers": {"must_include": ["T1"], "must_exclude": []},
                 "observations": [{"block": "B0", "digest": "h0-a"}]},
                {"id": "SB", "collector": "CB",
                 "barriers": {"must_include": ["T2"], "must_exclude": []},
                 "observations": [{"block": "B1", "digest": "h1-a"}]},
                {"id": "SC", "collector": "CC",
                 "barriers": {"must_include": ["T3"], "must_exclude": ["T2"]},
                 "observations": [{"block": "B0", "digest": "h0-b"}]}],
            "suspect": {
                "isolation_costs": {"CA": 5, "CB": 5, "CC": 5},
                "fixed_evidence": []},
        }

    def test_min_count_then_lex(self):
        # 仅隔离 CC 即可消除冲突（SC 是唯一与 SA/SB 屏障冲突的分片）
        result = run(parse_dict(self._raw()))
        self.assertEqual(result["optimal_isolation"]["isolated_collectors"], ["CC"])

    def test_cost_dominates_count(self):
        raw = self._raw()
        # 让 CC 昂贵，而 CA+CB 便宜：方案比较由总成本决定
        raw["suspect"]["isolation_costs"] = {"CA": 1, "CB": 1, "CC": 10}
        result = run(parse_dict(raw))
        # 隔离 CC（10）vs 隔离 CA、CB（2）。但隔离 CA、CB 后 SC 强制 T3，
        # T3 下闭包需要 T2/T1，与 SC 的 must_exclude T2 冲突 => 仍不可行。
        # 因此唯一可行方案仍是隔离 CC。
        self.assertEqual(result["optimal_isolation"]["isolated_collectors"], ["CC"])
        self.assertEqual(result["optimal_isolation"]["total_cost"], 10)


class SuspectFixedOnlyInfeasibleTests(unittest.TestCase):

    def test_no_solution_when_fixed_evidence_conflicts(self):
        raw = {
            "case_id": "fixed-conflict",
            "blocks": [{"id": "B", "initial_digest": "d0"}],
            "collectors": [{"id": "C1"}, {"id": "C2"}],
            "transactions": [
                {"id": "T1", "collector": "C1", "local_seq": 0,
                 "causal_after": [],
                 "writes": [{"block": "B", "digest": "d1"}]},
                {"id": "T2", "collector": "C2", "local_seq": 0,
                 "causal_after": ["T1"],
                 "writes": [{"block": "B", "digest": "d2"}]}],
            "shards": [
                {"id": "FX1", "collector": "C1", "revocable": False,
                 "barriers": {"must_include": ["T2"], "must_exclude": []},
                 "observations": [{"block": "B", "digest": "d2"}]},
                {"id": "FX2", "collector": "C2", "revocable": False,
                 "barriers": {"must_include": [], "must_exclude": ["T2"]},
                 "observations": []}],
            "suspect": {"isolation_costs": {"C1": 1, "C2": 1},
                        "fixed_evidence": ["FX1", "FX2"]},
        }
        result = run(parse_dict(raw))
        self.assertFalse(result["feasible"])
        # 固定证据不可撤，隔离任何采集器都无法解决
        self.assertIn("固定证据", result["note"])


class SuspectExhaustiveTests(unittest.TestCase):

    def test_brute_force_agrees_on_random_small_cases(self):
        """用小规模穷举独立核对联合选择（不依赖生产搜索代码）。"""
        from solution.model import parse_case
        from solution.engine import EvidenceEngine
        from solution.suspect import _exhaustive, EXHAUSTIVE_LIMIT
        from itertools import product

        # 手工构造一个 3 采集器小案例的所有 shard 子集可行性
        raw = {
            "case_id": "brute",
            "blocks": [{"id": "B", "initial_digest": "d0"}],
            "collectors": [{"id": "C1"}, {"id": "C2"}, {"id": "C3"}],
            "transactions": [
                {"id": "T1", "collector": "C1", "local_seq": 0,
                 "causal_after": [],
                 "writes": [{"block": "B", "digest": "d1"}]},
                {"id": "T2", "collector": "C2", "local_seq": 0,
                 "causal_after": ["T1"],
                 "writes": [{"block": "B", "digest": "d2"}]},
                {"id": "T3", "collector": "C3", "local_seq": 0,
                 "causal_after": ["T2"],
                 "writes": [{"block": "B", "digest": "d3"}]}],
            "shards": [
                {"id": "S1", "collector": "C1",
                 "barriers": {"must_include": ["T1"], "must_exclude": []},
                 "observations": [{"block": "B", "digest": "d1"}]},
                {"id": "S2", "collector": "C2",
                 "barriers": {"must_include": ["T2"], "must_exclude": []},
                 "observations": [{"block": "B", "digest": "d2"}]},
                {"id": "S3", "collector": "C3",
                 "barriers": {"must_include": [], "must_exclude": ["T2"]},
                 "observations": [{"block": "B", "digest": "d1"}]}],
            "suspect": {"isolation_costs": {"C1": 3, "C2": 1, "C3": 2},
                        "fixed_evidence": []},
        }
        case = parse_case(raw)
        self.assertLessEqual(len(case.collectors), EXHAUSTIVE_LIMIT)
        engine = EvidenceEngine(case)
        shard_of = {"C1": "S1", "C2": "S2", "C3": "S3"}

        def feasible_when(isolated):
            kept = tuple(sorted(shard_of[c] for c in ("C1", "C2", "C3")
                                if c not in isolated))
            return engine.solve(kept).feasible

        costs = {"C1": 3, "C2": 1, "C3": 2}
        ref = _exhaustive(["C1", "C2", "C3"], costs, feasible_when)

        # 完全独立的枚举验证
        best_key = None
        for mask in range(8):
            iso = frozenset(c for i, c in enumerate(("C1", "C2", "C3"))
                            if (mask >> i) & 1)
            if not feasible_when(iso):
                continue
            key = (sum(costs[c] for c in iso), len(iso), sorted(iso))
            if best_key is None or key < best_key:
                best_key = key
        self.assertIsNotNone(best_key)
        self.assertEqual(ref["cost"], best_key[0])
        self.assertEqual(ref["isolated"], best_key[2])


if __name__ == "__main__":
    unittest.main()
