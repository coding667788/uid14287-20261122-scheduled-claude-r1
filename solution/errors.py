"""结构化输入错误：错误码 + 字段位置。"""

from __future__ import annotations

from typing import Any


class InputError(Exception):
    """可报告给调用方的输入结构错误。

    Attributes:
        code: 机器可读错误码（见 README“错误码”一节）。
        path: 出错字段在输入 JSON 中的位置，形如 ``shards[2].barriers.must_include``。
        message: 人类可读的确定性说明。
    """

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(f"{code} at {path}: {message}")
        self.code = code
        self.path = path
        self.message = message

    def to_json(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "path": self.path, "message": self.message}}
