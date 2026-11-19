"""命令行入口: python -m solution --input cases.json --output result.json

每个 case 使用独立的 SQLite 数据库(默认临时目录,可用 --db-dir 指定),
按顺序应用操作并输出全部回执。运行完全离线、确定性、可重放。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

from .engine import DirectoryEngine


def run_cases(cases, db_dir=None):
    results = []
    for index, case in enumerate(cases):
        name = case.get("name", f"case-{index}")
        tmp = None
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
            path = os.path.join(db_dir, f"{name}.db")
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)
        else:
            tmp = tempfile.mkdtemp(prefix="fidir-")
            path = os.path.join(tmp, "case.db")
        engine = DirectoryEngine(path)
        try:
            receipts = [engine.apply(op) for op in case.get("ops", [])]
        finally:
            engine.close()
        results.append({"name": name, "results": receipts})
    return {"cases": results}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m solution")
    parser.add_argument("--input", required=True, help="cases.json 路径")
    parser.add_argument("--output", required=True, help="result.json 输出路径")
    parser.add_argument("--db-dir", default=None,
                        help="可选:持久化每个 case 的 SQLite 数据库目录")
    args = parser.parse_args(argv)
    with open(args.input, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    report = run_cases(data.get("cases", []), db_dir=args.db_dir)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
