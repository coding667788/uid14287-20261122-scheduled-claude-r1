"""compare 命令测试：逐块可能性、联合最大差异、理想性核查。"""

from __future__ import annotations

import unittest

from solution.compare import run

from helpers import load_example


class CompareDemoTests(unittest.TestCase):

    def setUp(self):
        self.result = run(load_example("case_compare.json"))

    def test_evidence_feasible(self):
        self.assertTrue(self.result["evidence_feasible"])

    def test_candidate_ideal_and_satisfaction(self):
        s0, s1 = self.result["candidate_slices"]
        self.assertTrue(s0["is_ideal"])
        self.assertFalse(s0["satisfies_evidence"])  # [T1] 不满足 S1/S2 的强制屏障
        self.assertTrue(s1["is_ideal"])
        self.assertTrue(s1["satisfies_evidence"])

    def test_block_possibilities_include_reversion(self):
        bx = self.result["block_possibilities"][0]
        digests = [(p["prefix"], p["digest"], p["writer_txn"])
                   for p in bx["possibilities"]]
        # 前缀 0 与前缀 2 摘要同为 x0，但来源不同（初始 vs T2）
        self.assertEqual(digests[0], (0, "x0", None))
        self.assertEqual(digests[2], (2, "x0", "T2"))

    def test_joint_search_uses_single_slice(self):
        # 证据可行模型中两块都被锁定到匹配前缀 => 联合最大差异为 0，
        # 而逐块朴素可能性之和为 2（差异状态在别的切片存在但证据不允许）
        self.assertEqual(self.result["naive_per_block_mismatch_sum"], 2)
        joint = self.result["joint_max_mismatch"]
        self.assertTrue(joint["feasible"])
        self.assertEqual(joint["max_mismatch"], 0)
        self.assertEqual(joint["slice"]["transaction_ids"],
                         ["T1", "T2", "T3", "T4"])

    def test_per_block_mismatch_flags(self):
        flags = self.result["joint_max_mismatch"]["per_block_mismatch"]
        self.assertEqual(flags, {"BX": False, "BY": False})


class CompareJointCouplingTests(unittest.TestCase):
    """构造证据，使联合优化不能由逐块独立选择拼凑。"""

    def _raw(self):
        return {
            "case_id": "coupling",
            "blocks": [
                {"id": "BX", "initial_digest": "x0"},
                {"id": "BY", "initial_digest": "y0"}],
            "collectors": [{"id": "C1"}, {"id": "C2"}],
            "transactions": [
                {"id": "T1", "collector": "C1", "local_seq": 0,
                 "causal_after": [],
                 "writes": [{"block": "BX", "digest": "x1"}]},
                {"id": "T2", "collector": "C2", "local_seq": 0,
                 "causal_after": [],
                 "writes": [{"block": "BY", "digest": "y1"}]},
                {"id": "T3", "collector": "C1", "local_seq": 1,
                 "causal_after": ["T1", "T2"],
                 "writes": [{"block": "BX", "digest": "x2"}]}],
            "shards": [
                {"id": "S1", "collector": "C1",
                 "barriers": {"must_include": [], "must_exclude": ["T3"]},
                 "observations": [{"block": "BX", "digest": "x1"}]},
                {"id": "S2", "collector": "C2",
                 "barriers": {"must_include": [], "must_exclude": []},
                 "observations": [{"block": "BY", "digest": "y1"}]}],
            "compare": {
                "witness_blocks": ["BX", "BY"],
                "slices": [[], ["T1", "T2"]],
                "preservation": {"BX": "x0", "BY": "y0"}},
        }

    def test_joint_mismatch_respects_evidence(self):
        from helpers import parse_dict
        result = run(parse_dict(self._raw()))
        joint = result["joint_max_mismatch"]
        self.assertTrue(joint["feasible"])
        # 可行切片：{T1,T2}（BX=x1 差异, BY=y1 差异，T3 被排除）
        self.assertEqual(joint["max_mismatch"], 2)
        self.assertEqual(set(joint["slice"]["transaction_ids"]), {"T1", "T2"})

    def test_non_ideal_candidate_rejected(self):
        from helpers import parse_dict
        raw = self._raw()
        # 切片 [T3] 不是理想（缺 T1/T2）
        raw["compare"]["slices"] = [["T3"], []]
        result = run(parse_dict(raw))
        self.assertFalse(result["candidate_slices"][0]["is_ideal"])
        self.assertIn("T1", result["candidate_slices"][0]["ideal_violations"])


class CompareInfeasibleEvidenceTests(unittest.TestCase):

    def test_joint_undefined_when_evidence_infeasible(self):
        raw = {
            "case_id": "inf",
            "blocks": [{"id": "B", "initial_digest": "d0"}],
            "collectors": [{"id": "C1"}, {"id": "C2"}],
            "transactions": [
                {"id": "T1", "collector": "C1", "local_seq": 0,
                 "causal_after": [], "writes": [{"block": "B", "digest": "d1"}]},
                {"id": "T2", "collector": "C2", "local_seq": 0,
                 "causal_after": ["T1"],
                 "writes": [{"block": "B", "digest": "d2"}]}],
            "shards": [
                {"id": "S1", "collector": "C1",
                 "barriers": {"must_include": ["T2"], "must_exclude": []},
                 "observations": [{"block": "B", "digest": "d2"}]},
                {"id": "S2", "collector": "C2",
                 "barriers": {"must_include": [], "must_exclude": ["T2"]},
                 "observations": []}],
            "compare": {
                "witness_blocks": ["B"], "slices": [[], ["T1"]],
                "preservation": {"B": "d0"}},
        }
        from helpers import parse_dict
        result = run(parse_dict(raw))
        self.assertFalse(result["evidence_feasible"])
        self.assertIsNone(result["joint_max_mismatch"]["max_mismatch"])


if __name__ == "__main__":
    unittest.main()
