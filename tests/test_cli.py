"""CLI 端到端测试：退出码、UTF-8 JSON、错误码、确定性输出。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "examples")


class CliTests(unittest.TestCase):

    def _run(self, *args):
        proc = subprocess.run(
            [sys.executable, "-m", "solution", *args],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        return proc

    def test_reconstruct_stdout_json(self):
        proc = self._run("reconstruct",
                         os.path.join(EXAMPLES, "case_reconstruct.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertTrue(data["feasible"])

    def test_conflict_exit_zero_but_infeasible(self):
        # 语义无解不是结构错误：退出码仍是 0
        proc = self._run("reconstruct",
                         os.path.join(EXAMPLES, "case_conflict.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(json.loads(proc.stdout)["feasible"])

    def test_structure_error_exit_code_two(self):
        bad = os.path.join(ROOT, ".runtime-tmp", "bad_case.json")
        os.makedirs(os.path.dirname(bad), exist_ok=True)
        with open(bad, "w", encoding="utf-8") as fh:
            json.dump({"blocks": "not-a-list"}, fh)
        proc = self._run("reconstruct", bad)
        self.assertEqual(proc.returncode, 2)
        err = json.loads(proc.stderr)
        self.assertIn("code", err["error"])
        self.assertIn("path", err["error"])

    def test_missing_file(self):
        proc = self._run("reconstruct", "does-not-exist.json")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stderr)["error"]["code"], "E_FILE")

    def test_invalid_json(self):
        bad = os.path.join(ROOT, ".runtime-tmp", "broken.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        proc = self._run("reconstruct", bad)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stderr)["error"]["code"], "E_JSON")

    def test_utf8_chinese_passthrough(self):
        case = {
            "case_id": "中文标识",
            "blocks": [{"id": "块甲", "initial_digest": "摘要0"}],
            "collectors": [{"id": "采1", "trust_marker": "可信标记-α"}],
            "transactions": [{
                "id": "事1", "collector": "采1", "local_seq": 0,
                "causal_after": [], "actor": "行为者甲",
                "writes": [{"block": "块甲", "digest": "摘要1"}]}],
            "shards": [{
                "id": "片1", "collector": "采1",
                "barriers": {"must_include": ["事1"], "must_exclude": []},
                "observations": [{"block": "块甲", "digest": "摘要1"}]}],
        }
        path = os.path.join(ROOT, ".runtime-tmp", "utf8.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(case, fh, ensure_ascii=False)
        proc = self._run("reconstruct", path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("中文标识", proc.stdout)
        self.assertIn("行为者甲", proc.stdout)

    def test_deterministic_output_across_runs(self):
        path = os.path.join(EXAMPLES, "case_suspect.json")
        a = self._run("suspect", path).stdout
        b = self._run("suspect", path).stdout
        self.assertEqual(a, b)

    def test_compare_and_suspect_commands(self):
        for cmd, name in (("compare", "case_compare.json"),
                          ("suspect", "case_suspect.json")):
            proc = self._run(cmd, os.path.join(EXAMPLES, name))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["command"], cmd)

    def test_unknown_command_rejected(self):
        proc = self._run("frobnicate",
                         os.path.join(EXAMPLES, "case_reconstruct.json"))
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
