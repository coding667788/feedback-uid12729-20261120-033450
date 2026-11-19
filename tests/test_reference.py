"""独立参考模型逐步对照:每个 case 的每个操作后,对比引擎与模型的
回执(状态/错误码/主体编号/主体代数/失效会话)以及全量状态导出。"""
from __future__ import annotations

import json
import os
import unittest

from solution.engine import DirectoryEngine
from solution.reference import ReferenceModel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES_PATH = os.path.join(ROOT, "cases.json")


def normalize(receipt):
    """去掉错误消息文本(两实现措辞可不同),其余字段逐一相等。"""
    r = dict(receipt)
    if r.get("error") is not None:
        r["error"] = {"code": r["error"]["code"]}
    return r


class ReferenceComparisonTest(unittest.TestCase):
    def test_all_cases_step_by_step(self):
        with open(CASES_PATH, "r", encoding="utf-8") as fh:
            cases = json.load(fh)["cases"]
        self.assertGreaterEqual(len(cases), 10)
        for case in cases:
            with self.subTest(case=case["name"]):
                engine = DirectoryEngine(":memory:")
                model = ReferenceModel()
                try:
                    for index, op in enumerate(case["ops"]):
                        eng_receipt = engine.apply(op)
                        ref_receipt = model.apply(op)
                        self.assertEqual(
                            normalize(eng_receipt), normalize(ref_receipt),
                            msg=f"receipt mismatch at op #{index} ({op.get('request_id')})",
                        )
                        self.assertEqual(
                            engine.dump_state(), model.dump_state(),
                            msg=f"state mismatch after op #{index} ({op.get('request_id')})",
                        )
                finally:
                    engine.close()

    def test_rejection_leaves_no_partial_principal(self):
        """模型与引擎在拒绝前后状态逐位相等(不留半个新主体)。"""
        with open(CASES_PATH, "r", encoding="utf-8") as fh:
            cases = json.load(fh)["cases"]
        case = next(c for c in cases if c["name"] == "provider-conflict-rejected")
        engine = DirectoryEngine(":memory:")
        model = ReferenceModel()
        try:
            for op in case["ops"][:4]:
                engine.apply(op)
                model.apply(op)
            eng_before = engine.dump_state()
            ref_before = model.dump_state()
            rejected = engine.apply(case["ops"][4])
            model.apply(case["ops"][4])
            self.assertEqual(rejected["status"], "rejected")
            self.assertEqual(engine.dump_state(), eng_before)
            self.assertEqual(model.dump_state(), ref_before)
        finally:
            engine.close()


if __name__ == "__main__":
    unittest.main()
