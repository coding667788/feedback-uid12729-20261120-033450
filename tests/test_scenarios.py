"""用独立进程固定三个关键场景:
1. 两个单独合法的合并共同形成提供方冲突;
2. 撤回桥与备选路径并存;
3. 到期与新会话争用。
每个场景在单独的子进程中执行,断言其退出码与逐条回执。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from solution import casekit as ck

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_worker(ops):
    """在独立进程中执行 ops,返回 (returncode, receipts)。"""
    tmp = tempfile.mkdtemp(prefix="fidir-scenario-")
    ops_path = os.path.join(tmp, "ops.json")
    out_path = os.path.join(tmp, "receipts.json")
    db_path = os.path.join(tmp, "case.db")
    with open(ops_path, "w", encoding="utf-8") as fh:
        json.dump(ops, fh)
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "solution.crash_worker",
         "--db", db_path, "--ops", ops_path, "--out", out_path],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    receipts = None
    if os.path.exists(out_path):
        with open(out_path, "r", encoding="utf-8") as fh:
            receipts = json.load(fh)
    return proc.returncode, receipts, proc.stderr


def by_id(receipts):
    return {r["request_id"]: r for r in receipts}


class TwoLegalMergesConflictTest(unittest.TestCase):
    """e1(a1-b1) 合法,e2(b1-a2) 单独看也合法,合并后 provA 出现两次。"""

    def test_scenario(self):
        ops = [
            ck.create_identity("r1", "t1", "a1", "provA", 1),
            ck.create_identity("r2", "t1", "b1", "provB", 1),
            ck.create_identity("r3", "t1", "a2", "provA", 1),
            ck.associate("r4", "t1", "e1", "a1", "provA", 1, "b1", "provB", 1, 1, 100, 2),
            ck.associate("r5", "t1", "e2", "b1", "provB", 1, "a2", "provA", 1, 2, 100, 3),
            ck.get_principal("r6", "t1", "a1", 4),
            ck.get_principal("r7", "t1", "a2", 4),
        ]
        code, receipts, err = run_worker(ops)
        self.assertEqual(code, 0, err)
        r = by_id(receipts)
        self.assertEqual(r["r4"]["status"], "ok")
        self.assertEqual(r["r4"]["principal_generation"], 1)
        self.assertEqual(r["r5"]["status"], "rejected")
        self.assertEqual(r["r5"]["error"]["code"], "E_PROVIDER_CONFLICT")
        # 不留半个新主体:a1 仍在原主体,a2 无主体
        self.assertEqual(r["r6"]["principal"]["members"], ["a1", "b1"])
        self.assertEqual(r["r6"]["principal"]["generation"], 1)
        self.assertIsNone(r["r7"]["principal"])


class BridgeRevokeWithAlternatePathTest(unittest.TestCase):
    """环上撤回一条边:备选路径保持连通,成员与代数不变,会话存活;
    再撤一条才拆分并失效会话。"""

    def test_scenario(self):
        ops = [
            ck.create_identity("r1", "t1", "a1", "provA", 1),
            ck.create_identity("r2", "t1", "b1", "provB", 1),
            ck.create_identity("r3", "t1", "c1", "provC", 1),
            ck.create_identity("r4", "t1", "d1", "provD", 1),
            ck.associate("r5", "t1", "e1", "a1", "provA", 1, "b1", "provB", 1, 1, 100, 2),
            ck.associate("r6", "t1", "e2", "b1", "provB", 1, "c1", "provC", 1, 2, 100, 3),
            ck.associate("r7", "t1", "e3", "c1", "provC", 1, "d1", "provD", 1, 3, 100, 4),
            ck.associate("r8", "t1", "e4", "d1", "provD", 1, "a1", "provA", 1, 4, 100, 5),
            ck.login("r9", "t1", ["a1", "b1", "c1", "d1"], 6),
            ck.revoke("r10", "t1", "e2", 7),
            ck.check_session("r11", "t1", "S-r9", 8),
            ck.revoke("r12", "t1", "e3", 9),
            ck.check_session("r13", "t1", "S-r9", 10),
        ]
        code, receipts, err = run_worker(ops)
        self.assertEqual(code, 0, err)
        r = by_id(receipts)
        self.assertEqual(r["r9"]["principal_generation"], 3)
        # 撤回 e2:备选路径 b1-a1-d1-c1 保持连通,代数不变、无会话失效
        self.assertEqual(r["r10"]["status"], "ok")
        self.assertEqual(r["r10"]["resulting_principals"],
                         [{"principal_no": "a1", "generation": 3,
                           "members": ["a1", "b1", "c1", "d1"]}])
        self.assertEqual(r["r10"]["invalidated_sessions"], [])
        self.assertTrue(r["r11"]["valid"])
        # 撤回 e3:拆分为 {a1,b1,d1}(新代数 4)与单点 c1,会话失效
        self.assertEqual(r["r12"]["resulting_principals"],
                         [{"principal_no": "a1", "generation": 4,
                           "members": ["a1", "b1", "d1"]}])
        self.assertEqual(r["r12"]["invalidated_sessions"], ["S-r9"])
        self.assertFalse(r["r13"]["valid"])


class ExpiryRaceTest(unittest.TestCase):
    """到期与新会话争用:时钟跨过 valid_until 后,旧会话失效、新登录被拒。"""

    def test_scenario(self):
        ops = [
            ck.create_identity("r1", "t1", "a1", "provA", 1),
            ck.create_identity("r2", "t1", "b1", "provB", 1),
            ck.associate("r3", "t1", "e1", "a1", "provA", 1, "b1", "provB", 1, 1, 10, 5),
            ck.login("r4", "t1", ["a1", "b1"], 9),
            ck.check_session("r5", "t1", "S-r4", 10),
            ck.login("r6", "t1", ["a1", "b1"], 10),
        ]
        code, receipts, err = run_worker(ops)
        self.assertEqual(code, 0, err)
        r = by_id(receipts)
        self.assertEqual(r["r4"]["status"], "ok")
        self.assertEqual(r["r4"]["principal_generation"], 1)
        self.assertFalse(r["r5"]["valid"])
        self.assertEqual(r["r5"]["invalidated_sessions"], ["S-r4"])
        self.assertEqual(r["r6"]["status"], "rejected")
        self.assertEqual(r["r6"]["error"]["code"], "E_SESSION_MISMATCH")


if __name__ == "__main__":
    unittest.main()
