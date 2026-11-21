"""结构校验与错误码测试。"""

from __future__ import annotations

import copy
import unittest

from solution.errors import InputError
from solution.model import parse_case

from helpers import load_example


def base_raw():
    case = load_example("case_reconstruct.json")
    return copy.deepcopy(case.raw)


class StructureTests(unittest.TestCase):

    def test_valid_case_parses(self):
        case = load_example("case_reconstruct.json")
        self.assertEqual(case.case_id, "demo-reconstruct")
        # 拓扑序是确定性的
        self.assertEqual(case.order, sorted(case.order, key=lambda t: case.pos[t]))

    def test_missing_field(self):
        raw = base_raw()
        del raw["blocks"][0]["id"]
        with self.assertRaises(InputError) as ctx:
            parse_case(raw)
        self.assertEqual(ctx.exception.code, "E_ID")
        self.assertIn("blocks[0]", ctx.exception.path)

    def test_unknown_reference_txn(self):
        raw = base_raw()
        raw["transactions"][0]["causal_after"] = ["NOPE"]
        with self.assertRaises(InputError) as ctx:
            parse_case(raw)
        self.assertEqual(ctx.exception.code, "E_UNKNOWN_REF")

    def test_unknown_block_in_write(self):
        raw = base_raw()
        raw["transactions"][0]["writes"].append({"block": "GHOST", "digest": "x"})
        with self.assertRaises(InputError) as ctx:
            parse_case(raw)
        self.assertEqual(ctx.exception.code, "E_UNKNOWN_REF")

    def test_duplicate_txn_id(self):
        raw = base_raw()
        raw["transactions"][1]["id"] = raw["transactions"][0]["id"]
        with self.assertRaises(InputError) as ctx:
            parse_case(raw)
        self.assertEqual(ctx.exception.code, "E_DUP_ID")

    def test_local_seq_conflict(self):
        raw = base_raw()
        raw["transactions"][1]["local_seq"] = raw["transactions"][0]["local_seq"]
        with self.assertRaises(InputError) as ctx:
            parse_case(raw)
        self.assertEqual(ctx.exception.code, "E_LOCAL_SEQ_CONFLICT")

    def test_causal_cycle(self):
        raw = base_raw()
        # T1 依赖 T5，而 T5 已（间接）依赖 T1
        raw["transactions"][0]["causal_after"] = ["T5"]
        with self.assertRaises(InputError) as ctx:
            parse_case(raw)
        self.assertEqual(ctx.exception.code, "E_CAUSAL_CYCLE")

    def test_write_incomparable(self):
        raw = base_raw()
        # 去掉 T4 对 T2 的因果声明，使 T2/T4 同写 B0 却不可比
        raw["transactions"][3]["causal_after"] = ["T3"]
        with self.assertRaises(InputError) as ctx:
            parse_case(raw)
        self.assertEqual(ctx.exception.code, "E_WRITE_INCOMPARABLE")

    def test_barrier_self_conflict(self):
        raw = base_raw()
        raw["shards"][0]["barriers"]["must_exclude"] = ["T1"]
        with self.assertRaises(InputError) as ctx:
            parse_case(raw)
        self.assertEqual(ctx.exception.code, "E_BARRIER_CONFLICT")

    def test_empty_writes_rejected(self):
        raw = base_raw()
        raw["transactions"][0]["writes"] = []
        with self.assertRaises(InputError) as ctx:
            parse_case(raw)
        self.assertEqual(ctx.exception.code, "E_EMPTY_WRITES")

    def test_cost_missing_for_revocable(self):
        raw = base_raw()
        raw["suspect"] = {"isolation_costs": {"C1": 1}, "fixed_evidence": []}
        with self.assertRaises(InputError) as ctx:
            parse_case(raw)
        self.assertEqual(ctx.exception.code, "E_COST_MISSING")

    def test_fixed_revocable_rejected(self):
        raw = base_raw()
        raw["shards"][0]["revocable"] = True
        raw["suspect"] = {
            "isolation_costs": {"C1": 1, "C2": 1, "C3": 1},
            "fixed_evidence": ["S1"],
        }
        with self.assertRaises(InputError) as ctx:
            parse_case(raw)
        self.assertEqual(ctx.exception.code, "E_FIXED_REVOCABLE")

    def test_bad_type(self):
        raw = base_raw()
        raw["transactions"][0]["local_seq"] = "zero"
        with self.assertRaises(InputError) as ctx:
            parse_case(raw)
        self.assertEqual(ctx.exception.code, "E_TYPE")

    def test_digest_unreachable_is_semantic_not_syntax(self):
        # 观察到任何前缀都不会出现的摘要是语义不可行，不是结构错误
        raw = base_raw()
        raw["shards"][0]["observations"][0]["digest"] = "IMPOSSIBLE-DIGEST"
        case = parse_case(raw)
        from solution.reconstruct import run
        result = run(case)
        self.assertFalse(result["feasible"])
        self.assertTrue(result["conflict_core"])


if __name__ == "__main__":
    unittest.main()
