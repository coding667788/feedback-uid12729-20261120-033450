"""独立参考模型(小模型):纯内存重放同一操作流。

与 solution.engine 完全独立实现(无 SQLite、BFS 建分量、字典状态),
供测试逐步对比引擎的主体代数与会话状态。确认令牌格式属于规格本身,
此处独立重复定义,不引用 engine 的实现。
"""
from __future__ import annotations

import copy
from collections import defaultdict, deque


def _confirmation(tenant, edge_id, provider, identity, generation, valid_until):
    return "|".join(
        ["MOCKCONF", str(tenant), str(edge_id), str(provider), str(identity),
         str(generation), str(valid_until)]
    )


class ModelReject(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _new_tenant():
    return {
        "clock": 0,
        "counter": 0,
        "identities": {},          # identity -> [provider, generation]
        "edges": {},               # edge_id -> dict
        "sessions": {},            # session_id -> dict
        "requests": {},            # request_id -> receipt
        "principals": [],          # [{members, gen_map, pno, gen}], 按 pno 排序
    }


class ReferenceModel:
    def __init__(self):
        self.tenants = {}

    # ------------------------------------------------------------- 基础设施

    def _tenant(self, name):
        t = self.tenants.get(name)
        if t is None:
            t = _new_tenant()
            self.tenants[name] = t
        return t

    def _components(self, t):
        """BFS 连通分量;返回 [(members_tuple, gen_map_tuple)],仅 >=2 成员,按最
        小身份排序。"""
        adj = defaultdict(set)
        for e in t["edges"].values():
            if e["state"] == "active":
                adj[e["a"]].add(e["b"])
                adj[e["b"]].add(e["a"])
        seen = set()
        out = []
        for iid in t["identities"]:
            if iid in seen:
                continue
            group = []
            queue = deque([iid])
            seen.add(iid)
            while queue:
                x = queue.popleft()
                group.append(x)
                for y in adj[x]:
                    if y not in seen:
                        seen.add(y)
                        queue.append(y)
            if len(group) >= 2:
                members = tuple(sorted(group))
                gen_map = tuple((m, t["identities"][m][1]) for m in members)
                out.append((members, gen_map))
        out.sort(key=lambda item: item[0][0])
        return out

    def _recompute(self, t):
        comps = self._components(t)
        old = {(p["members"], p["gen_map"]): p["gen"] for p in t["principals"]}
        new = []
        changes = []
        for members, gen_map in comps:
            gen = old.get((members, gen_map))
            if gen is None:
                t["counter"] += 1
                gen = t["counter"]
                changes.append(
                    {"principal_no": members[0], "generation": gen,
                     "members": list(members)}
                )
            new.append({"members": members, "gen_map": gen_map,
                        "pno": members[0], "gen": gen})
        t["principals"] = new
        current = {(p["members"], p["gen_map"]): (p["pno"], p["gen"]) for p in new}
        invalidated = []
        for sid in sorted(t["sessions"]):
            s = t["sessions"][sid]
            if s["valid"] and current.get((s["members"], s["gen_map"])) != (
                s["pno"], s["gen"]
            ):
                s["valid"] = False
                invalidated.append(sid)
        return changes, invalidated

    def _sweep(self, t, clock):
        swept = False
        for e in t["edges"].values():
            if e["state"] == "active" and e["valid_until"] <= clock:
                e["state"] = "expired"
                e["cleared_clock"] = clock
                swept = True
        if swept:
            return self._recompute(t)[1]
        return []

    def _principal_of(self, t, identity):
        for p in t["principals"]:
            if identity in p["members"]:
                return {
                    "principal_no": p["pno"],
                    "generation": p["gen"],
                    "members": list(p["members"]),
                    "gen_map": dict(p["gen_map"]),
                }
        return None

    # ------------------------------------------------------------- 入口

    def apply(self, op):
        optype = op["op"]
        tenant_name = op["tenant"]
        request_id = op.get("request_id")
        t = self._tenant(tenant_name)
        if request_id is not None and request_id in t["requests"]:
            receipt = copy.deepcopy(t["requests"][request_id])
            receipt["replayed"] = True
            return receipt
        clock = max(t["clock"], int(op.get("clock", 0)))
        t["clock"] = clock
        sweep_invalidated = self._sweep(t, clock)
        handler = getattr(self, "_op_" + optype, None)
        if handler is None:
            raise ValueError(f"unknown op: {optype!r}")
        try:
            receipt = handler(t, tenant_name, op, clock)
            receipt["status"] = "ok"
            receipt["error"] = None
        except ModelReject as rj:
            receipt = {"status": "rejected",
                       "error": {"code": rj.code, "message": rj.message}}
        merged = set(receipt.get("invalidated_sessions") or [])
        merged |= set(sweep_invalidated)
        receipt["invalidated_sessions"] = sorted(merged)
        receipt["request_id"] = request_id
        receipt["op"] = optype
        receipt["tenant"] = tenant_name
        receipt["clock"] = clock
        receipt["replayed"] = False
        if request_id is not None:
            t["requests"][request_id] = copy.deepcopy(receipt)
        return receipt

    # ------------------------------------------------------------- 操作

    def _op_create_identity(self, t, tenant, op, clock):
        identity = op["identity"]
        provider = op["provider"]
        for token in (tenant, identity, provider):
            if "|" in str(token):
                raise ModelReject("E_MALFORMED_INPUT", "标识符不得包含 '|'")
        if identity in t["identities"]:
            raise ModelReject("E_IDENTITY_EXISTS", f"身份已存在: {identity}")
        t["identities"][identity] = [provider, 1]
        return {"identity": identity, "provider": provider,
                "identity_generation": 1, "invalidated_sessions": []}

    def _op_renew_identity(self, t, tenant, op, clock):
        identity = op["identity"]
        if identity not in t["identities"]:
            raise ModelReject("E_IDENTITY_NOT_FOUND", f"身份不存在: {identity}")
        t["identities"][identity][1] += 1
        changes, invalidated = self._recompute(t)
        return {
            "identity": identity,
            "identity_generation": t["identities"][identity][1],
            "principal": self._principal_of(t, identity),
            "changes": changes,
            "invalidated_sessions": invalidated,
        }

    def _check_edge_payload(self, t, tenant, op, clock, a, b, edge_id):
        valid_from = int(op["valid_from"])
        valid_until = int(op["valid_until"])
        if not (valid_from <= clock < valid_until):
            raise ModelReject(
                "E_VALIDITY_WINDOW",
                f"有效窗口 [{valid_from},{valid_until}) 不覆盖当前时钟 {clock}",
            )
        gen_a = t["identities"][a][1]
        gen_b = t["identities"][b][1]
        if op.get("gen_a") != gen_a or op.get("gen_b") != gen_b:
            raise ModelReject(
                "E_STALE_GENERATION",
                f"确认钉扎的代数 ({op.get('gen_a')},{op.get('gen_b')}) "
                f"与当前 ({gen_a},{gen_b}) 不符",
            )
        exp_a = _confirmation(tenant, edge_id, t["identities"][a][0], a, gen_a, valid_until)
        exp_b = _confirmation(tenant, edge_id, t["identities"][b][0], b, gen_b, valid_until)
        if op.get("confirm_a") != exp_a or op.get("confirm_b") != exp_b:
            raise ModelReject("E_CONFIRMATION_INVALID", "提供方确认令牌校验失败")
        return valid_from, valid_until

    def _op_associate(self, t, tenant, op, clock):
        edge_id = op["edge_id"]
        a, b = op["a"], op["b"]
        if a == b:
            raise ModelReject("E_SAME_IDENTITY", "关联两端不能是同一身份")
        if a not in t["identities"] or b not in t["identities"]:
            missing = a if a not in t["identities"] else b
            raise ModelReject("E_IDENTITY_NOT_FOUND", f"身份不存在: {missing}")
        if t["identities"][a][0] == t["identities"][b][0]:
            raise ModelReject("E_SAME_PROVIDER", "关联两端属于同一提供方")
        existing = t["edges"].get(edge_id)
        if existing is not None and existing["state"] == "active":
            raise ModelReject("E_EDGE_EXISTS", f"边已存在且活动: {edge_id}")
        valid_from, valid_until = self._check_edge_payload(
            t, tenant, op, clock, a, b, edge_id
        )
        # 提供方冲突检查:合并后的分量
        adj = defaultdict(set)
        for e in t["edges"].values():
            if e["state"] == "active":
                adj[e["a"]].add(e["b"])
                adj[e["b"]].add(e["a"])
        adj[a].add(b)
        adj[b].add(a)
        merged = set()
        queue = deque([a])
        merged.add(a)
        while queue:
            x = queue.popleft()
            for y in adj[x]:
                if y not in merged:
                    merged.add(y)
                    queue.append(y)
        seen = {}
        for m in sorted(merged):
            p = t["identities"][m][0]
            if p in seen:
                raise ModelReject(
                    "E_PROVIDER_CONFLICT",
                    f"合并后分量将同时包含提供方 {p} 的身份 {seen[p]} 与 {m}",
                )
            seen[p] = m
        t["edges"][edge_id] = {
            "a": a, "b": b,
            "gen_a": t["identities"][a][1], "gen_b": t["identities"][b][1],
            "valid_from": valid_from, "valid_until": valid_until,
            "state": "active", "cleared_clock": None,
        }
        changes, invalidated = self._recompute(t)
        principal = self._principal_of(t, a)
        return {
            "edge_id": edge_id,
            "principal_no": principal["principal_no"],
            "principal_generation": principal["generation"],
            "members": principal["members"],
            "changes": changes,
            "invalidated_sessions": invalidated,
        }

    def _op_renew_edge(self, t, tenant, op, clock):
        edge_id = op["edge_id"]
        e = t["edges"].get(edge_id)
        if e is None:
            raise ModelReject("E_EDGE_NOT_FOUND", f"边不存在: {edge_id}")
        if e["state"] != "active":
            raise ModelReject("E_EDGE_CLEARED", f"边已清除({e['state']}): {edge_id}")
        valid_from, valid_until = self._check_edge_payload(
            t, tenant, op, clock, e["a"], e["b"], edge_id
        )
        e["gen_a"] = t["identities"][e["a"]][1]
        e["gen_b"] = t["identities"][e["b"]][1]
        e["valid_from"] = valid_from
        e["valid_until"] = valid_until
        changes, invalidated = self._recompute(t)
        return {"edge_id": edge_id, "valid_until": valid_until,
                "changes": changes, "invalidated_sessions": invalidated}

    def _op_revoke(self, t, tenant, op, clock):
        edge_id = op["edge_id"]
        e = t["edges"].get(edge_id)
        if e is None:
            raise ModelReject("E_EDGE_NOT_FOUND", f"边不存在: {edge_id}")
        if e["state"] != "active":
            raise ModelReject("E_EDGE_CLEARED", f"边已清除({e['state']}): {edge_id}")
        e["state"] = "revoked"
        e["cleared_clock"] = clock
        changes, invalidated = self._recompute(t)
        principals = [
            {"principal_no": p["pno"], "generation": p["gen"],
             "members": list(p["members"])}
            for p in t["principals"]
        ]
        return {"edge_id": edge_id, "resulting_principals": principals,
                "changes": changes, "invalidated_sessions": invalidated}

    def _op_login(self, t, tenant, op, clock):
        request_id = op.get("request_id")
        if request_id is None:
            raise ModelReject("E_MALFORMED_INPUT", "登录必须携带 request_id")
        members = tuple(sorted(set(op["identities"])))
        current = {p["members"]: p for p in t["principals"]}
        if members not in current:
            raise ModelReject(
                "E_SESSION_MISMATCH",
                f"身份集合 {sorted(members)} 不是任何当前主体的确切成员集",
            )
        p = current[members]
        session_id = "S-" + request_id
        t["sessions"][session_id] = {
            "pno": p["pno"], "gen": p["gen"],
            "members": members, "gen_map": p["gen_map"],
            "valid": True, "created_clock": clock,
        }
        return {
            "session_id": session_id,
            "principal_no": p["pno"],
            "principal_generation": p["gen"],
            "members": list(members),
            "invalidated_sessions": [],
        }

    def _op_check_session(self, t, tenant, op, clock):
        session_id = op["session_id"]
        s = t["sessions"].get(session_id)
        if s is None:
            raise ModelReject("E_SESSION_NOT_FOUND", f"会话不存在: {session_id}")
        return {
            "session_id": session_id,
            "valid": s["valid"],
            "principal_no": s["pno"],
            "principal_generation": s["gen"],
            "invalidated_sessions": [],
        }

    def _op_get_principal(self, t, tenant, op, clock):
        identity = op["identity"]
        if identity not in t["identities"]:
            raise ModelReject("E_IDENTITY_NOT_FOUND", f"身份不存在: {identity}")
        return {"identity": identity,
                "principal": self._principal_of(t, identity),
                "invalidated_sessions": []}

    # ------------------------------------------------------------- 导出

    def dump_state(self):
        dump = {}
        for name in sorted(self.tenants):
            t = self.tenants[name]
            dump[name] = {
                "clock": t["clock"],
                "version_counter": t["counter"],
                "identities": [
                    {"identity": iid, "provider": v[0], "generation": v[1]}
                    for iid, v in sorted(t["identities"].items())
                ],
                "edges": [
                    {"edge_id": eid, "state": e["state"], "a": e["a"], "b": e["b"]}
                    for eid, e in sorted(t["edges"].items())
                ],
                "principals": [
                    {"principal_no": p["pno"], "generation": p["gen"],
                     "members": list(p["members"]), "gen_map": dict(p["gen_map"])}
                    for p in t["principals"]
                ],
                "sessions": [
                    {"session_id": sid, "valid": s["valid"],
                     "principal_no": s["pno"], "generation": s["gen"]}
                    for sid, s in sorted(t["sessions"].items())
                ],
            }
        return dump
