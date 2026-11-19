"""引擎核心规则的具体断言测试。"""
from __future__ import annotations

import unittest

from solution import casekit as ck
from solution.engine import DirectoryEngine


def fresh_engine():
    return DirectoryEngine(":memory:")


class BasicFlowTest(unittest.TestCase):
    def setUp(self):
        self.eng = fresh_engine()

    def tearDown(self):
        self.eng.close()

    def test_merge_login_check(self):
        eng = self.eng
        eng.apply(ck.create_identity("r1", "t1", "a1", "provA", 1))
        eng.apply(ck.create_identity("r2", "t1", "b1", "provB", 1))
        eng.apply(ck.create_identity("r3", "t1", "c1", "provC", 1))
        r = eng.apply(ck.associate("r4", "t1", "e1", "a1", "provA", 1,
                                   "b1", "provB", 1, 1, 100, 2))
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["principal_no"], "a1")
        self.assertEqual(r["principal_generation"], 1)
        self.assertEqual(r["members"], ["a1", "b1"])
        r = eng.apply(ck.associate("r5", "t1", "e2", "b1", "provB", 1,
                                   "c1", "provC", 1, 2, 100, 3))
        self.assertEqual(r["principal_generation"], 2)
        r = eng.apply(ck.login("r6", "t1", ["c1", "a1", "b1"], 4))
        self.assertEqual(r["session_id"], "S-r6")
        self.assertEqual(r["principal_no"], "a1")
        self.assertEqual(r["principal_generation"], 2)
        self.assertEqual(r["members"], ["a1", "b1", "c1"])
        r = eng.apply(ck.check_session("r7", "t1", "S-r6", 5))
        self.assertTrue(r["valid"])

    def test_login_requires_exact_member_set(self):
        eng = self.eng
        eng.apply(ck.create_identity("r1", "t1", "a1", "provA", 1))
        eng.apply(ck.create_identity("r2", "t1", "b1", "provB", 1))
        eng.apply(ck.associate("r3", "t1", "e1", "a1", "provA", 1,
                               "b1", "provB", 1, 1, 100, 2))
        # 子集不是确切成员集
        r = eng.apply(ck.login("r4", "t1", ["a1"], 3))
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["code"], "E_SESSION_MISMATCH")
        # 超集也不是
        r = eng.apply(ck.login("r5", "t1", ["a1", "b1", "zz"], 3))
        self.assertEqual(r["error"]["code"], "E_SESSION_MISMATCH")


class ErrorCodeTest(unittest.TestCase):
    def setUp(self):
        self.eng = fresh_engine()
        self.eng.apply(ck.create_identity("c1", "t1", "a1", "provA", 1))
        self.eng.apply(ck.create_identity("c2", "t1", "b1", "provB", 1))
        self.eng.apply(ck.create_identity("c3", "t1", "a2", "provA", 1))

    def tearDown(self):
        self.eng.close()

    def test_duplicate_identity(self):
        r = self.eng.apply(ck.create_identity("x1", "t1", "a1", "provA", 2))
        self.assertEqual(r["error"]["code"], "E_IDENTITY_EXISTS")

    def test_missing_identity(self):
        r = self.eng.apply(ck.associate("x2", "t1", "e9", "a1", "provA", 1,
                                        "zz", "provZ", 1, 1, 100, 2))
        self.assertEqual(r["error"]["code"], "E_IDENTITY_NOT_FOUND")

    def test_same_identity_endpoints(self):
        r = self.eng.apply(ck.associate("x3", "t1", "e9", "a1", "provA", 1,
                                        "a1", "provA", 1, 1, 100, 2))
        self.assertEqual(r["error"]["code"], "E_SAME_IDENTITY")

    def test_same_provider_endpoints(self):
        r = self.eng.apply(ck.associate("x4", "t1", "e9", "a1", "provA", 1,
                                        "a2", "provA", 1, 1, 100, 2))
        self.assertEqual(r["error"]["code"], "E_SAME_PROVIDER")

    def test_validity_window(self):
        # 窗口尚未开始
        r = self.eng.apply(ck.associate("x5", "t1", "e9", "a1", "provA", 1,
                                        "b1", "provB", 1, 50, 100, 2))
        self.assertEqual(r["error"]["code"], "E_VALIDITY_WINDOW")
        # 窗口已结束
        r = self.eng.apply(ck.associate("x6", "t1", "e9", "a1", "provA", 1,
                                        "b1", "provB", 1, 1, 5, 10))
        self.assertEqual(r["error"]["code"], "E_VALIDITY_WINDOW")

    def test_stale_generation(self):
        r = self.eng.apply(ck.associate("x7", "t1", "e9", "a1", "provA", 9,
                                        "b1", "provB", 1, 1, 100, 2))
        self.assertEqual(r["error"]["code"], "E_STALE_GENERATION")

    def test_bad_confirmation(self):
        op = ck.associate("x8", "t1", "e9", "a1", "provA", 1,
                          "b1", "provB", 1, 1, 100, 2)
        op["confirm_b"] = "MOCKCONF|forged"
        r = self.eng.apply(op)
        self.assertEqual(r["error"]["code"], "E_CONFIRMATION_INVALID")

    def test_edge_exists_and_not_found_and_cleared(self):
        eng = self.eng
        eng.apply(ck.associate("a1", "t1", "e1", "a1", "provA", 1,
                               "b1", "provB", 1, 1, 100, 2))
        r = eng.apply(ck.associate("a2", "t1", "e1", "a1", "provA", 1,
                                   "b1", "provB", 1, 1, 100, 3))
        self.assertEqual(r["error"]["code"], "E_EDGE_EXISTS")
        r = eng.apply(ck.revoke("a3", "t1", "nope", 3))
        self.assertEqual(r["error"]["code"], "E_EDGE_NOT_FOUND")
        eng.apply(ck.revoke("a4", "t1", "e1", 4))
        r = eng.apply(ck.revoke("a5", "t1", "e1", 5))
        self.assertEqual(r["error"]["code"], "E_EDGE_CLEARED")
        r = eng.apply(ck.renew_edge("a6", "t1", "e1", "a1", "provA", 1,
                                    "b1", "provB", 1, 5, 200, 5))
        self.assertEqual(r["error"]["code"], "E_EDGE_CLEARED")

    def test_session_not_found(self):
        r = self.eng.apply(ck.check_session("x9", "t1", "S-ghost", 2))
        self.assertEqual(r["error"]["code"], "E_SESSION_NOT_FOUND")


class ProviderConflictTest(unittest.TestCase):
    """两个单独合法的合并共同形成提供方冲突;拒绝不得留下半个新主体。"""

    def test_conflict_rejected_atomically(self):
        eng = fresh_engine()
        eng.apply(ck.create_identity("c1", "t1", "a1", "provA", 1))
        eng.apply(ck.create_identity("c2", "t1", "b1", "provB", 1))
        eng.apply(ck.create_identity("c3", "t1", "a2", "provA", 1))
        eng.apply(ck.associate("m1", "t1", "e1", "a1", "provA", 1,
                               "b1", "provB", 1, 1, 100, 2))
        before = eng.dump_state()
        r = eng.apply(ck.associate("m2", "t1", "e2", "b1", "provB", 1,
                                   "a2", "provA", 1, 2, 100, 3))
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["code"], "E_PROVIDER_CONFLICT")
        after = eng.dump_state()
        # 拒绝后状态完全不变:没有半个新主体、没有新代数、没有新边
        self.assertEqual(before, after)
        principals = after["t1"]["principals"]
        self.assertEqual(len(principals), 1)
        self.assertEqual(principals[0]["members"], ["a1", "b1"])
        self.assertEqual(principals[0]["generation"], 1)
        self.assertEqual(after["t1"]["version_counter"], 1)
        eng.close()


class GenerationRulesTest(unittest.TestCase):
    def test_old_generation_never_revives(self):
        eng = fresh_engine()
        eng.apply(ck.create_identity("c1", "t1", "a1", "provA", 1))
        eng.apply(ck.create_identity("c2", "t1", "b1", "provB", 1))
        eng.apply(ck.associate("m1", "t1", "e1", "a1", "provA", 1,
                               "b1", "provB", 1, 1, 100, 2))
        eng.apply(ck.login("l1", "t1", ["a1", "b1"], 3))
        eng.apply(ck.revoke("v1", "t1", "e1", 4))
        # 成员集合变回去 -> 仍是全新代数
        r = eng.apply(ck.associate("m2", "t1", "e2", "a1", "provA", 1,
                                   "b1", "provB", 1, 5, 100, 5))
        self.assertEqual(r["principal_generation"], 2)
        r = eng.apply(ck.check_session("k1", "t1", "S-l1", 6))
        self.assertFalse(r["valid"])
        eng.close()

    def test_identity_renewal_bumps_principal_generation(self):
        eng = fresh_engine()
        eng.apply(ck.create_identity("c1", "t1", "a1", "provA", 1))
        eng.apply(ck.create_identity("c2", "t1", "b1", "provB", 1))
        eng.apply(ck.associate("m1", "t1", "e1", "a1", "provA", 1,
                               "b1", "provB", 1, 1, 100, 2))
        eng.apply(ck.login("l1", "t1", ["a1", "b1"], 3))
        r = eng.apply(ck.renew_identity("n1", "t1", "a1", 4))
        self.assertEqual(r["identity_generation"], 2)
        self.assertEqual(r["principal"]["generation"], 2)
        self.assertEqual(r["invalidated_sessions"], ["S-l1"])
        r = eng.apply(ck.check_session("k1", "t1", "S-l1", 5))
        self.assertFalse(r["valid"])
        eng.close()

    def test_redundant_edge_keeps_generation(self):
        eng = fresh_engine()
        for i, (iid, p) in enumerate([("a1", "provA"), ("b1", "provB"),
                                      ("c1", "provC"), ("d1", "provD")]):
            eng.apply(ck.create_identity(f"c{i}", "t1", iid, p, 1))
        eng.apply(ck.associate("m1", "t1", "e1", "a1", "provA", 1, "b1", "provB", 1, 1, 100, 2))
        eng.apply(ck.associate("m2", "t1", "e2", "b1", "provB", 1, "c1", "provC", 1, 2, 100, 3))
        eng.apply(ck.associate("m3", "t1", "e3", "c1", "provC", 1, "d1", "provD", 1, 3, 100, 4))
        r = eng.apply(ck.associate("m4", "t1", "e4", "d1", "provD", 1, "a1", "provA", 1, 4, 100, 5))
        # 成员与身份代数未变 -> 主体代数不变
        self.assertEqual(r["principal_generation"], 3)
        self.assertEqual(r["changes"], [])
        eng.close()


class ReplayTest(unittest.TestCase):
    def test_duplicate_request_only_replays(self):
        eng = fresh_engine()
        eng.apply(ck.create_identity("c1", "t1", "a1", "provA", 1))
        eng.apply(ck.create_identity("c2", "t1", "b1", "provB", 1))
        eng.apply(ck.associate("m1", "t1", "e1", "a1", "provA", 1,
                               "b1", "provB", 1, 1, 100, 2))
        eng.apply(ck.revoke("v1", "t1", "e1", 3))
        # 已清除边的重复旧请求:只重放历史回执,不重建边
        r = eng.apply(ck.associate("m1", "t1", "e1", "a1", "provA", 1,
                                   "b1", "provB", 1, 1, 100, 4))
        self.assertTrue(r["replayed"])
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["principal_generation"], 1)
        r = eng.apply(ck.get_principal("g1", "t1", "a1", 4))
        self.assertIsNone(r["principal"])
        # 新 request_id 可以重新激活同一条边
        r = eng.apply(ck.associate("m2", "t1", "e1", "a1", "provA", 1,
                                   "b1", "provB", 1, 4, 100, 5))
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["principal_generation"], 2)
        eng.close()

    def test_login_replay_returns_same_session(self):
        eng = fresh_engine()
        eng.apply(ck.create_identity("c1", "t1", "a1", "provA", 1))
        eng.apply(ck.create_identity("c2", "t1", "b1", "provB", 1))
        eng.apply(ck.associate("m1", "t1", "e1", "a1", "provA", 1,
                               "b1", "provB", 1, 1, 100, 2))
        eng.apply(ck.login("l1", "t1", ["a1", "b1"], 3))
        r = eng.apply(ck.login("l1", "t1", ["a1", "b1"], 4))
        self.assertTrue(r["replayed"])
        self.assertEqual(r["session_id"], "S-l1")
        sessions = eng.dump_state()["t1"]["sessions"]
        self.assertEqual(len(sessions), 1)
        eng.close()


class TenantIsolationTest(unittest.TestCase):
    def test_same_principal_no_isolated_per_tenant(self):
        eng = fresh_engine()
        for t in ("t1", "t2"):
            eng.apply(ck.create_identity(f"c1-{t}", t, "a1", "provA", 1))
            eng.apply(ck.create_identity(f"c2-{t}", t, "b1", "provB", 1))
            eng.apply(ck.associate(f"m1-{t}", t, "e1", "a1", "provA", 1,
                                   "b1", "provB", 1, 1, 100, 2))
            eng.apply(ck.login(f"l1-{t}", t, ["a1", "b1"], 3))
        eng.apply(ck.revoke("v1", "t1", "e1", 4))
        r1 = eng.apply(ck.check_session("k1", "t1", "S-l1-t1", 5))
        r2 = eng.apply(ck.check_session("k2", "t2", "S-l1-t2", 5))
        self.assertFalse(r1["valid"])
        self.assertTrue(r2["valid"])
        state = eng.dump_state()
        self.assertEqual(state["t1"]["principals"], [])
        self.assertEqual(state["t2"]["principals"][0]["principal_no"], "a1")
        eng.close()


class ExpiryTest(unittest.TestCase):
    def test_expiry_invalidates_and_blocks_new_login(self):
        eng = fresh_engine()
        eng.apply(ck.create_identity("c1", "t1", "a1", "provA", 1))
        eng.apply(ck.create_identity("c2", "t1", "b1", "provB", 1))
        eng.apply(ck.associate("m1", "t1", "e1", "a1", "provA", 1,
                               "b1", "provB", 1, 1, 10, 5))
        r = eng.apply(ck.login("l1", "t1", ["a1", "b1"], 9))
        self.assertEqual(r["status"], "ok")
        r = eng.apply(ck.check_session("k1", "t1", "S-l1", 10))
        self.assertFalse(r["valid"])
        self.assertEqual(r["invalidated_sessions"], ["S-l1"])
        r = eng.apply(ck.login("l2", "t1", ["a1", "b1"], 10))
        self.assertEqual(r["error"]["code"], "E_SESSION_MISMATCH")
        # 时钟非递减:旧时钟请求不回头
        r = eng.apply(ck.get_principal("g1", "t1", "a1", 3))
        self.assertEqual(r["clock"], 10)
        eng.close()


if __name__ == "__main__":
    unittest.main()
