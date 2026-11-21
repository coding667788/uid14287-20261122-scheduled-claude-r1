"""测试公共工具：构造案例、调用命令。"""

from __future__ import annotations

import json
import os

from solution.model import parse_case

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "examples")


def load_example(name: str):
    with open(os.path.join(EXAMPLES, name), "r", encoding="utf-8") as fh:
        return parse_case(json.load(fh))


def parse_dict(raw: dict):
    return parse_case(raw)
