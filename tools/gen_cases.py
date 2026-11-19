"""生成 cases.json(全部输入自行合成、离线、确定性)。

用法: python tools/gen_cases.py  (从仓库根目录运行)
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solution import casekit as ck  # noqa: E402


def build_cases():
    cases = []

    # 1. 基本合并 + 登录会话 -----------------------------------------------
    ops = [
        ck.create_identity("r1", "t1", "a1", "provA", clock=1),
        ck.create_identity("r2", "t1", "b1", "provB", clock=1),
        ck.create_identity("r3", "t1", "c1", "provC", clock=1),
        ck.associate("r4", "t1", "e1", "a1", "provA", 1, "b1", "provB", 1, 1, 100, clock=2),
        ck.associate("r5", "t1", "e2", "b1", "provB", 1, "c1", "provC", 1, 2, 100, clock=3),
        ck.login("r6", "t1", ["a1", "b1", "c1"], clock=4),
        ck.check_session("r7", "t1", "S-r6", clock=5),
        ck.get_principal("r8", "t1", "c1", clock=5),
    ]
    cases.append({"name": "basic-merge-login", "ops": ops})

    # 2. 两个单独合法的合并共同形成提供方冲突 ------------------------------
    # e1: a1-b1 合法;e2: b1-a2 单独看也合法,但合并后分量含 provA 两次 -> 拒绝
    ops = [
        ck.create_identity("r1", "t1", "a1", "provA", clock=1),
        ck.create_identity("r2", "t1", "b1", "provB", clock=1),
        ck.create_identity("r3", "t1", "a2", "provA", clock=1),
        ck.associate("r4", "t1", "e1", "a1", "provA", 1, "b1", "provB", 1, 1, 100, clock=2),
        ck.associate("r5", "t1", "e2", "b1", "provB", 1, "a2", "provA", 1, 2, 100, clock=3),
        ck.get_principal("r6", "t1", "a2", clock=4),
        ck.get_principal("r7", "t1", "a1", clock=4),
    ]
    cases.append({"name": "provider-conflict-rejected", "ops": ops})

    # 3. 撤回桥与备选路径并存 ----------------------------------------------
    # 环 a1-b1-c1-d1-a1:撤回 b1-c1 仍有备选路径,成员不变、代数不变、会话存活;
    # 再撤回 c1-d1 才真正拆分。
    ops = [
        ck.create_identity("r1", "t1", "a1", "provA", clock=1),
        ck.create_identity("r2", "t1", "b1", "provB", clock=1),
        ck.create_identity("r3", "t1", "c1", "provC", clock=1),
        ck.create_identity("r4", "t1", "d1", "provD", clock=1),
        ck.associate("r5", "t1", "e1", "a1", "provA", 1, "b1", "provB", 1, 1, 100, clock=2),
        ck.associate("r6", "t1", "e2", "b1", "provB", 1, "c1", "provC", 1, 2, 100, clock=3),
        ck.associate("r7", "t1", "e3", "c1", "provC", 1, "d1", "provD", 1, 3, 100, clock=4),
        ck.associate("r8", "t1", "e4", "d1", "provD", 1, "a1", "provA", 1, 4, 100, clock=5),
        ck.login("r9", "t1", ["a1", "b1", "c1", "d1"], clock=6),
        ck.revoke("r10", "t1", "e2", clock=7),          # 备选路径 b1-a1-d1-c1 仍在
        ck.check_session("r11", "t1", "S-r9", clock=8),  # 会话仍有效
        ck.revoke("r12", "t1", "e3", clock=9),           # 真正拆分 {a1,b1,d1} + {c1}
        ck.check_session("r13", "t1", "S-r9", clock=10),  # 会话失效
        ck.get_principal("r14", "t1", "c1", clock=10),   # c1 成单点,无主体
    ]
    cases.append({"name": "bridge-revoke-with-alternate-path", "ops": ops})

    # 4. 撤回真正的桥 -> 主体拆分 ------------------------------------------
    ops = [
        ck.create_identity("r1", "t1", "a1", "provA", clock=1),
        ck.create_identity("r2", "t1", "b1", "provB", clock=1),
        ck.create_identity("r3", "t1", "c1", "provC", clock=1),
        ck.associate("r4", "t1", "e1", "a1", "provA", 1, "b1", "provB", 1, 1, 100, clock=2),
        ck.associate("r5", "t1", "e2", "b1", "provB", 1, "c1", "provC", 1, 2, 100, clock=3),
        ck.login("r6", "t1", ["a1", "b1", "c1"], clock=4),
        ck.revoke("r7", "t1", "e2", clock=5),            # 拆成 {a1,b1} 与单点 c1
        ck.check_session("r8", "t1", "S-r6", clock=6),
        ck.get_principal("r9", "t1", "a1", clock=6),
    ]
    cases.append({"name": "bridge-revoke-splits", "ops": ops})

    # 5. 到期与新会话争用 ---------------------------------------------------
    ops = [
        ck.create_identity("r1", "t1", "a1", "provA", clock=1),
        ck.create_identity("r2", "t1", "b1", "provB", clock=1),
        ck.associate("r3", "t1", "e1", "a1", "provA", 1, "b1", "provB", 1, 1, 10, clock=5),
        ck.login("r4", "t1", ["a1", "b1"], clock=9),          # 到期前登录成功
        ck.check_session("r5", "t1", "S-r4", clock=10),        # 时钟到 10:边过期,会话失效
        ck.login("r6", "t1", ["a1", "b1"], clock=10),          # 新会话争用失败
        ck.associate("r7", "t1", "e2", "a1", "provA", 1, "b1", "provB", 1, 11, 20, clock=11),
        ck.check_session("r8", "t1", "S-r4", clock=12),        # 旧代数会话永不复活
        ck.login("r9", "t1", ["a1", "b1"], clock=12),          # 新主体代数下的新会话
        ck.check_session("r10", "t1", "S-r9", clock=13),
    ]
    cases.append({"name": "expiry-vs-new-session", "ops": ops})

    # 6. 续发:身份续发改变主体代数;边续发延长有效期;重复请求重放 ---------
    ops = [
        ck.create_identity("r1", "t1", "a1", "provA", clock=1),
        ck.create_identity("r2", "t1", "b1", "provB", clock=1),
        ck.associate("r3", "t1", "e1", "a1", "provA", 1, "b1", "provB", 1, 1, 10, clock=2),
        ck.login("r4", "t1", ["a1", "b1"], clock=3),
        ck.renew_identity("r5", "t1", "a1", clock=4),          # a1 代数 1->2,主体新代数
        ck.check_session("r6", "t1", "S-r4", clock=5),         # 旧会话失效
        ck.renew_edge("r7", "t1", "e1", "a1", "provA", 2, "b1", "provB", 1, 5, 50, clock=5),
        ck.login("r8", "t1", ["a1", "b1"], clock=6),
        ck.renew_edge("r7", "t1", "e1", "a1", "provA", 2, "b1", "provB", 1, 5, 50, clock=7),  # 重放
        ck.check_session("r9", "t1", "S-r8", clock=8),
    ]
    cases.append({"name": "renewals-and-replay", "ops": ops})

    # 7. 已清除边的重复旧请求只能重放历史回执 ------------------------------
    ops = [
        ck.create_identity("r1", "t1", "a1", "provA", clock=1),
        ck.create_identity("r2", "t1", "b1", "provB", clock=1),
        ck.associate("r3", "t1", "e1", "a1", "provA", 1, "b1", "provB", 1, 1, 100, clock=2),
        ck.revoke("r4", "t1", "e1", clock=3),
        ck.associate("r3", "t1", "e1", "a1", "provA", 1, "b1", "provB", 1, 1, 100, clock=4),  # 重放 r3
        ck.get_principal("r5", "t1", "a1", clock=4),           # 边仍清除,无主体
        ck.associate("r6", "t1", "e1", "a1", "provA", 1, "b1", "provB", 1, 4, 100, clock=5),  # 新请求重新激活
        ck.get_principal("r7", "t1", "a1", clock=6),
    ]
    cases.append({"name": "duplicate-request-replay", "ops": ops})

    # 8. 租户隔离:同编号主体在不同租户互不影响 -----------------------------
    ops = [
        ck.create_identity("r1", "t1", "a1", "provA", clock=1),
        ck.create_identity("r2", "t1", "b1", "provB", clock=1),
        ck.create_identity("r3", "t2", "a1", "provA", clock=1),
        ck.create_identity("r4", "t2", "b1", "provB", clock=1),
        ck.associate("r5", "t1", "e1", "a1", "provA", 1, "b1", "provB", 1, 1, 100, clock=2),
        ck.associate("r6", "t2", "e1", "a1", "provA", 1, "b1", "provB", 1, 1, 100, clock=2),
        ck.login("r7", "t1", ["a1", "b1"], clock=3),
        ck.login("r8", "t2", ["a1", "b1"], clock=3),
        ck.revoke("r9", "t1", "e1", clock=4),                  # 仅影响 t1
        ck.check_session("r10", "t1", "S-r7", clock=5),
        ck.check_session("r11", "t2", "S-r8", clock=5),
        ck.get_principal("r12", "t2", "a1", clock=5),
    ]
    cases.append({"name": "tenant-isolation", "ops": ops})

    # 9. 旧代数会话永不因分量变回去而复活 -----------------------------------
    ops = [
        ck.create_identity("r1", "t1", "a1", "provA", clock=1),
        ck.create_identity("r2", "t1", "b1", "provB", clock=1),
        ck.associate("r3", "t1", "e1", "a1", "provA", 1, "b1", "provB", 1, 1, 100, clock=2),
        ck.login("r4", "t1", ["a1", "b1"], clock=3),
        ck.revoke("r5", "t1", "e1", clock=4),
        ck.associate("r6", "t1", "e2", "a1", "provA", 1, "b1", "provB", 1, 5, 100, clock=5),  # 成员相同,代数全新
        ck.check_session("r7", "t1", "S-r4", clock=6),         # 不复活
        ck.login("r8", "t1", ["a1", "b1"], clock=6),
        ck.check_session("r9", "t1", "S-r8", clock=7),
    ]
    cases.append({"name": "old-generation-never-revives", "ops": ops})

    # 10. 两个分量合并:双方会话全部失效 ------------------------------------
    ops = [
        ck.create_identity("r1", "t1", "a1", "provA", clock=1),
        ck.create_identity("r2", "t1", "b1", "provB", clock=1),
        ck.create_identity("r3", "t1", "c1", "provC", clock=1),
        ck.create_identity("r4", "t1", "d1", "provD", clock=1),
        ck.associate("r5", "t1", "e1", "a1", "provA", 1, "b1", "provB", 1, 1, 100, clock=2),
        ck.associate("r6", "t1", "e2", "c1", "provC", 1, "d1", "provD", 1, 2, 100, clock=3),
        ck.login("r7", "t1", ["a1", "b1"], clock=4),
        ck.login("r8", "t1", ["c1", "d1"], clock=4),
        ck.associate("r9", "t1", "e3", "b1", "provB", 1, "c1", "provC", 1, 4, 100, clock=5),
        ck.check_session("r10", "t1", "S-r7", clock=6),
        ck.check_session("r11", "t1", "S-r8", clock=6),
        ck.login("r12", "t1", ["a1", "b1", "c1", "d1"], clock=6),
    ]
    cases.append({"name": "merge-two-components", "ops": ops})

    return cases


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "cases.json")
    data = {"cases": build_cases()}
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    print(f"wrote {out} with {len(data['cases'])} cases")


if __name__ == "__main__":
    main()
