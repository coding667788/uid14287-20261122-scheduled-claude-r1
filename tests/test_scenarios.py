"""题目明确点名的六个场景专项测试。"""

from __future__ import annotations

import unittest

from solution.compare import run as compare_run
from solution.reconstruct import run as reconstruct_run
from solution.suspect import run as suspect_run

from helpers import parse_dict


class ScenarioTests(unittest.TestCase):

    # ------------------------------------------------------------------ #
    # 1) 本地可解释但合起来冲突
    # ------------------------------------------------------------------ #
    def test_locally_explainable_jointly_conflicting(self):
        raw = {
            "case_id": "s1",
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
                {"id": "S1", "collector": "C1",
                 "barriers": {"must_include": ["T2"], "must_exclude": []},
                 "observations": [{"block": "B", "digest": "d2"}]},
                {"id": "S2", "collector": "C2",
                 "barriers": {"must_include": [], "must_exclude": ["T2"]},
                 "observations": [{"block": "B", "digest": "d1"}]}],
        }
        case = parse_dict(raw)
        from solution.engine import EvidenceEngine
        engine = EvidenceEngine(case)
        # 每个分片单独都自洽
        self.assertTrue(engine.solve(("S1",)).feasible)
        self.assertTrue(engine.solve(("S2",)).feasible)
        # 合起来无解
        result = reconstruct_run(case)
        self.assertFalse(result["feasible"])
        self.assertEqual(set(result["conflict_core"]), {"S1", "S2"})

    # ------------------------------------------------------------------ #
    # 2) 跨区事务的半截镜像
    # ------------------------------------------------------------------ #
    def test_half_atomic_transaction_mirror(self):
        raw = {
            "case_id": "s2",
            "blocks": [
                {"id": "B0", "initial_digest": "h0"},
                {"id": "B1", "initial_digest": "h1"}],
            "collectors": [{"id": "C1"}, {"id": "C2"}],
            "transactions": [
                {"id": "T1", "collector": "C1", "local_seq": 0,
                 "causal_after": [], "writes": [
                     {"block": "B0", "digest": "h0-a"},
                     {"block": "B1", "digest": "h1-a"}]},
                {"id": "T2", "collector": "C2", "local_seq": 0,
                 "causal_after": ["T1"],
                 "writes": [{"block": "B0", "digest": "h0-b"}]}],
            "shards": [
                # 声称 B0 已到 T2 后状态，却又要求 B1 停在 T1 之前：
                # 原子事务 T1 必须整体可见，半截镜像不成立
                {"id": "S1", "collector": "C2",
                 "barriers": {"must_include": ["T2"], "must_exclude": []},
                 "observations": [{"block": "B0", "digest": "h0-b"}]},
                {"id": "S2", "collector": "C1",
                 "barriers": {"must_include": [], "must_exclude": ["T1"]},
                 "observations": [{"block": "B1", "digest": "h1"}]}],
        }
        result = reconstruct_run(parse_dict(raw))
        self.assertFalse(result["feasible"])
        # 两个分片互相需要/禁止 T1，构成冲突集
        self.assertEqual(set(result["conflict_core"]), {"S1", "S2"})

    # ------------------------------------------------------------------ #
    # 3) 重回原摘要：最后写入者保留
    # ------------------------------------------------------------------ #
    def test_digest_reverted_keeps_last_writer(self):
        raw = {
            "case_id": "s3",
            "blocks": [{"id": "B", "initial_digest": "d0"}],
            "collectors": [{"id": "C1"}],
            "transactions": [
                {"id": "T1", "collector": "C1", "local_seq": 0,
                 "causal_after": [],
                 "writes": [{"block": "B", "digest": "d1"}]},
                {"id": "T2", "collector": "C1", "local_seq": 1,
                 "causal_after": ["T1"],
                 "writes": [{"block": "B", "digest": "d0"}]}],
            "shards": [
                {"id": "S1", "collector": "C1",
                 "barriers": {"must_include": ["T2"], "must_exclude": []},
                 "observations": [{"block": "B", "digest": "d0"}]}],
        }
        result = reconstruct_run(parse_dict(raw))
        self.assertTrue(result["feasible"])
        state = result["block_states"][0]
        self.assertEqual(state["digest"], "d0")          # 摘要等于初始值
        self.assertEqual(state["writer_txn"], "T2")      # 但写入者是 T2
        self.assertEqual(state["source_kind"], "transaction")

    # ------------------------------------------------------------------ #
    # 4) 新证据使可行来源收缩（摘要不变，写入者被锁定）
    # ------------------------------------------------------------------ #
    def test_new_evidence_shrinks_feasible_sources(self):
        raw = {
            "case_id": "s4",
            "blocks": [{"id": "B", "initial_digest": "d0"}],
            "collectors": [{"id": "C1"}, {"id": "C2"}],
            "transactions": [
                {"id": "T1", "collector": "C1", "local_seq": 0,
                 "causal_after": [],
                 "writes": [{"block": "B", "digest": "d1"}]},
                {"id": "T2", "collector": "C2", "local_seq": 0,
                 "causal_after": ["T1"],
                 # 与 T1 同摘要但不同写入者
                 "writes": [{"block": "B", "digest": "d1"}]}],
            "shards": [
                # 仅观察 d1：前缀 1（写入者 T1）或前缀 2（写入者 T2）都允许
                {"id": "S1", "collector": "C1",
                 "barriers": {"must_include": [], "must_exclude": []},
                 "observations": [{"block": "B", "digest": "d1"}]}],
        }
        case = parse_dict(raw)
        before = reconstruct_run(case)
        self.assertTrue(before["feasible"])
        # 最小切片只强制到 T1，写入者可以是 T1
        self.assertEqual(before["block_states"][0]["writer_txn"], "T1")

        # 追加更强证据：必须包含 T2，写入者被锁定为 T2（摘要仍是 d1）
        raw["shards"].append({
            "id": "S2", "collector": "C2",
            "barriers": {"must_include": ["T2"], "must_exclude": []},
            "observations": [{"block": "B", "digest": "d1"}]})
        after = reconstruct_run(parse_dict(raw))
        self.assertTrue(after["feasible"])
        state = after["block_states"][0]
        self.assertEqual(state["writer_txn"], "T2")
        self.assertEqual(state["digest"], "d1")
        self.assertEqual(set(after["slice"]["transaction_ids"]), {"T1", "T2"})

    # ------------------------------------------------------------------ #
    # 5) 删除廉价采集器却无解（固定证据自身冲突）
    # ------------------------------------------------------------------ #
    def test_removing_cheap_collector_still_unsolvable(self):
        raw = {
            "case_id": "s5",
            "blocks": [{"id": "B", "initial_digest": "d0"}],
            "collectors": [{"id": "C1"}, {"id": "C2"}, {"id": "C3"}],
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
                 "observations": [{"block": "B", "digest": "d1"}]},
                {"id": "S3", "collector": "C3", "revocable": True,
                 "barriers": {"must_include": [], "must_exclude": []},
                 "observations": []}],
            "suspect": {
                "isolation_costs": {"C1": 100, "C2": 100, "C3": 1},
                "fixed_evidence": ["FX1", "FX2"]},
        }
        result = suspect_run(parse_dict(raw))
        # 最便宜的 C3 隔离毫无帮助；固定证据互相冲突，整体无解
        self.assertFalse(result["feasible"])

    # ------------------------------------------------------------------ #
    # 6) 同一个历史切片不能同时实现逐块最大差异
    # ------------------------------------------------------------------ #
    def test_single_slice_cannot_sum_per_block_maxima(self):
        raw = {
            "case_id": "s6",
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
                # 证据把 BX 锁在前缀 1，BY 锁在前缀 1，且 T3 被排除
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
        result = compare_run(parse_dict(raw))
        # 逐块看，两块在“某个切片”都能出现差异
        for entry in result["block_possibilities"]:
            self.assertTrue(any(p["mismatch_with_preservation"]
                                for p in entry["possibilities"]))
        # 联合搜索在证据可行切片内给出的结果必须是单个自洽切片，
        # 而不是把两个块在不同切片的差异拼起来
        joint = result["joint_max_mismatch"]
        self.assertTrue(joint["feasible"])
        self.assertEqual(joint["max_mismatch"], 2)
        self.assertEqual(set(joint["slice"]["transaction_ids"]), {"T1", "T2"})
        # 关键：返回的是一个切片，逐块标志与该切片实际状态一致
        self.assertEqual(joint["per_block_mismatch"], {"BX": True, "BY": True})


if __name__ == "__main__":
    unittest.main()
