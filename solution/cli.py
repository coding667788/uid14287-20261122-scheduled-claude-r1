"""命令行入口：python -m solution <命令> CASE.json

结构错误 -> 退出码 2，错误体为确定性 UTF-8 JSON；
正常结果 -> 退出码 0，JSON 写到标准输出。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import compare, reconstruct, suspect
from .errors import InputError
from .model import parse_case

COMMANDS = ("reconstruct", "compare", "suspect")


def _emit(obj: Any, stream=sys.stdout) -> None:
    json.dump(obj, stream, ensure_ascii=False, indent=2, sort_keys=False)
    stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m solution",
        description="数字取证：分区镜像一致切片与污染判定（虚构介质建模）")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("case_file", help="案例 JSON 路径（- 表示标准输入）")
    args = parser.parse_args(argv)

    try:
        if args.case_file == "-":
            raw = json.load(sys.stdin)
        else:
            # 显式 utf-8，Windows 原生环境同样确定
            with open(args.case_file, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
    except FileNotFoundError:
        _emit({"error": {"code": "E_FILE", "path": args.case_file,
                         "message": "案例文件不存在"}}, sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        _emit({"error": {"code": "E_JSON",
                         "path": args.case_file,
                         "message": f"JSON 解析失败: 行 {exc.lineno} 列 {exc.colno}: {exc.msg}"}},
              sys.stderr)
        return 2

    try:
        case = parse_case(raw)
        if args.command == "reconstruct":
            result = reconstruct.run(case)
        elif args.command == "compare":
            result = compare.run(case)
        else:
            result = suspect.run(case)
    except InputError as exc:
        _emit(exc.to_json(), sys.stderr)
        return 2

    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
