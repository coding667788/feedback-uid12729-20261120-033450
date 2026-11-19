"""崩溃注入工作进程(自建进程,供测试真实终止)。

用法:
    python -m solution.crash_worker --db DB --ops OPS.json \
        [--kill-point pre_commit|post_commit|none] [--kill-request REQ] \
        [--out RECEIPTS.json]

kill-point:
  pre_commit  -- 分量更新已写入事务但尚未 COMMIT 时 os._exit(100)
  post_commit -- 事务已 COMMIT(含持久回执)但回执尚未返回调用方时 os._exit(101)
  none        -- 不注入,正常跑完

父进程重启同一命令(去掉 kill 点)后,已提交的请求按 request_id 重放历史回执。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .engine import DirectoryEngine

EXIT_PRE_COMMIT = 100
EXIT_POST_COMMIT = 101


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m solution.crash_worker")
    parser.add_argument("--db", required=True)
    parser.add_argument("--ops", required=True, help="操作列表 JSON 文件")
    parser.add_argument("--kill-point",
                        choices=["none", "pre_commit", "post_commit"],
                        default="none")
    parser.add_argument("--kill-request", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    with open(args.ops, "r", encoding="utf-8") as fh:
        ops = json.load(fh)

    engine = DirectoryEngine(args.db)
    if args.kill_point != "none" and args.kill_request is not None:
        exit_code = (EXIT_PRE_COMMIT if args.kill_point == "pre_commit"
                     else EXIT_POST_COMMIT)

        def hook(request_id, _target=args.kill_request, _code=exit_code):
            if request_id == _target:
                os._exit(_code)  # 真实终止:不展开栈、不运行清理

        engine.kill_hooks[args.kill_point] = hook

    receipts = [engine.apply(op) for op in ops]
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(receipts, fh, ensure_ascii=False, indent=2)
    engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
