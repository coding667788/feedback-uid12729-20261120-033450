"""本地联邦身份目录引擎(SQLite 持久化,单事务保证)。

规则概要(详见 README.md):
- 主体 = 同一租户内活动关联边的连通分量(仅含 >=2 个身份的分量)。
- 一个分量不得包含同一提供方的两个身份。
- 主体公开编号 = 分量内最小身份 ID(字符串序)。
- 任何成员集合或身份代数变化都分配新的主体代数(租户内单调计数器,永不复用)。
- 登录会话担保一组确切已确认身份、主体编号与当时主体代数;旧代数会话永不复活。
- 每个变更操作(含端点检查、合并/拆分、会话失效、持久回执)在同一个 SQLite 事务内完成。
- 事务内按租户非递减时钟重验确认(过期清扫)与分量。
- 重复 request_id 只重放历史回执,不重复执行。
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict

CONF_PREFIX = "MOCKCONF"


def make_confirmation(tenant, edge_id, provider, identity, generation, valid_until):
    """构造模拟提供方确认令牌(确定性、可离线校验)。"""
    return "|".join(
        [CONF_PREFIX, str(tenant), str(edge_id), str(provider), str(identity),
         str(generation), str(valid_until)]
    )


class Reject(Exception):
    """业务拒绝:携带错误码;事务内不得留下任何主体变更。"""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant TEXT PRIMARY KEY,
    clock INTEGER NOT NULL DEFAULT 0,
    version_counter INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS identities (
    tenant TEXT NOT NULL,
    identity TEXT NOT NULL,
    provider TEXT NOT NULL,
    generation INTEGER NOT NULL,
    PRIMARY KEY (tenant, identity)
);
CREATE TABLE IF NOT EXISTS edges (
    tenant TEXT NOT NULL,
    edge_id TEXT NOT NULL,
    a TEXT NOT NULL,
    b TEXT NOT NULL,
    gen_a INTEGER NOT NULL,
    gen_b INTEGER NOT NULL,
    confirm_a TEXT NOT NULL,
    confirm_b TEXT NOT NULL,
    valid_from INTEGER NOT NULL,
    valid_until INTEGER NOT NULL,
    state TEXT NOT NULL,               -- active | revoked | expired
    cleared_clock INTEGER,
    PRIMARY KEY (tenant, edge_id)
);
CREATE TABLE IF NOT EXISTS current_components (
    tenant TEXT NOT NULL,
    principal_no TEXT NOT NULL,
    generation INTEGER NOT NULL,
    members TEXT NOT NULL,             -- JSON 有序身份列表
    gen_map TEXT NOT NULL,             -- JSON {identity: generation}
    PRIMARY KEY (tenant, principal_no)
);
CREATE TABLE IF NOT EXISTS sessions (
    tenant TEXT NOT NULL,
    session_id TEXT NOT NULL,
    principal_no TEXT NOT NULL,
    generation INTEGER NOT NULL,
    members TEXT NOT NULL,
    gen_map TEXT NOT NULL,
    created_clock INTEGER NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant, session_id)
);
CREATE TABLE IF NOT EXISTS requests (
    tenant TEXT NOT NULL,
    request_id TEXT NOT NULL,
    receipt TEXT NOT NULL,
    PRIMARY KEY (tenant, request_id)
);
"""


class DirectoryEngine:
    """联邦身份目录引擎。所有变更在单个 SQLite 事务内完成。"""

    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, isolation_level=None, timeout=30.0)
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass  # :memory: 不支持 WAL,忽略
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)
        # 崩溃注入点: "pre_commit" / "post_commit" -> callable(request_id)
        self.kill_hooks = {}
        self._OPS = {
            "create_identity": DirectoryEngine._op_create_identity,
            "renew_identity": DirectoryEngine._op_renew_identity,
            "associate": DirectoryEngine._op_associate,
            "renew_edge": DirectoryEngine._op_renew_edge,
            "revoke": DirectoryEngine._op_revoke,
            "login": DirectoryEngine._op_login,
            "check_session": DirectoryEngine._op_check_session,
            "get_principal": DirectoryEngine._op_get_principal,
        }

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------------ 入口

    def apply(self, op):
        """应用一个操作,返回回执 dict。数据库锁冲突时有限重试。"""
        attempt = 0
        while True:
            try:
                return self._apply_once(op)
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() and attempt < 100:
                    attempt += 1
                    time.sleep(0.01 * attempt)
                    continue
                raise

    def _apply_once(self, op):
        optype = op.get("op")
        handler = self._OPS.get(optype)
        if handler is None:
            raise ValueError(f"unknown op: {optype!r}")
        tenant = op.get("tenant")
        if not tenant:
            raise ValueError("op missing tenant")
        request_id = op.get("request_id")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 幂等重放:同一 (tenant, request_id) 只返回历史回执
            if request_id is not None:
                row = conn.execute(
                    "SELECT receipt FROM requests WHERE tenant=? AND request_id=?",
                    (tenant, request_id),
                ).fetchone()
                if row is not None:
                    conn.execute("ROLLBACK")
                    receipt = json.loads(row[0])
                    receipt["replayed"] = True
                    return receipt
            # 租户非递减时钟 + 事务内重验(过期清扫)
            clock = self._advance_clock(tenant, int(op.get("clock", 0)))
            sweep_invalidated = self._sweep(tenant, clock)
            try:
                receipt = handler(self, tenant, op, clock)
                receipt["status"] = "ok"
                receipt["error"] = None
            except Reject as rj:
                # 拒绝:操作本身不产生任何变更(清扫结果仍属正常重验,保留)
                receipt = {
                    "status": "rejected",
                    "error": {"code": rj.code, "message": rj.message},
                }
            merged = set(receipt.get("invalidated_sessions") or [])
            merged |= set(sweep_invalidated)
            receipt["invalidated_sessions"] = sorted(merged)
            receipt["request_id"] = request_id
            receipt["op"] = optype
            receipt["tenant"] = tenant
            receipt["clock"] = clock
            receipt["replayed"] = False
            self._run_hook("pre_commit", request_id)
            if request_id is not None:
                conn.execute(
                    "INSERT INTO requests (tenant, request_id, receipt) VALUES (?,?,?)",
                    (tenant, request_id, json.dumps(receipt, sort_keys=True)),
                )
            conn.execute("COMMIT")
            self._run_hook("post_commit", request_id)
            return receipt
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def _run_hook(self, point, request_id):
        hook = self.kill_hooks.get(point)
        if hook is not None:
            hook(request_id)

    # ------------------------------------------------------------------ 基础设施

    def _advance_clock(self, tenant, req_clock):
        row = self.conn.execute(
            "SELECT clock FROM tenants WHERE tenant=?", (tenant,)
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO tenants (tenant, clock, version_counter) VALUES (?,?,0)",
                (tenant, req_clock),
            )
            return req_clock
        clock = max(row[0], req_clock)
        if clock != row[0]:
            self.conn.execute("UPDATE tenants SET clock=? WHERE tenant=?", (clock, tenant))
        return clock

    def _next_version(self, tenant):
        self.conn.execute(
            "UPDATE tenants SET version_counter=version_counter+1 WHERE tenant=?",
            (tenant,),
        )
        return self.conn.execute(
            "SELECT version_counter FROM tenants WHERE tenant=?", (tenant,)
        ).fetchone()[0]

    def _sweep(self, tenant, clock):
        """过期边清扫;有变化则重建分量并失效会话。返回被失效的会话 ID 列表。"""
        cur = self.conn.execute(
            "UPDATE edges SET state='expired', cleared_clock=? "
            "WHERE tenant=? AND state='active' AND valid_until<=?",
            (clock, tenant, clock),
        )
        if cur.rowcount:
            return self._recompute(tenant, clock)[1]
        return []

    def _identities(self, tenant):
        rows = self.conn.execute(
            "SELECT identity, provider, generation FROM identities "
            "WHERE tenant=? ORDER BY rowid",
            (tenant,),
        ).fetchall()
        return {r[0]: {"provider": r[1], "generation": r[2]} for r in rows}

    def _components_now(self, tenant):
        """当前全部分量(含单点)。返回 (sorted_members_list, identities)。"""
        ids = self._identities(tenant)
        parent = {i: i for i in ids}

        def find(x):
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:
                parent[x], x = root, parent[x]
            return root

        rows = self.conn.execute(
            "SELECT a, b FROM edges WHERE tenant=? AND state='active'", (tenant,)
        ).fetchall()
        for a, b in rows:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
        groups = defaultdict(list)
        for i in ids:
            groups[find(i)].append(i)
        return [sorted(m) for m in groups.values()], ids

    def _recompute(self, tenant, clock):
        """重建当前分量:分配新代数、失效受影响会话。

        返回 (changes, invalidated_session_ids)。
        """
        comps, ids = self._components_now(tenant)
        principals = []
        for members in comps:
            if len(members) < 2:
                continue  # 单点身份不是主体
            gen_map = {m: ids[m]["generation"] for m in members}
            principals.append((members, gen_map))
        principals.sort(key=lambda item: item[0][0])
        old = {}
        for pno, gen, members_json, genmap_json in self.conn.execute(
            "SELECT principal_no, generation, members, gen_map "
            "FROM current_components WHERE tenant=?",
            (tenant,),
        ).fetchall():
            old[(members_json, genmap_json)] = (pno, gen)
        changes = []
        new_rows = []
        for members, gen_map in principals:
            members_json = json.dumps(members)
            genmap_json = json.dumps(gen_map, sort_keys=True)
            pno = members[0]
            key = (members_json, genmap_json)
            if key in old:
                gen = old[key][1]
            else:
                gen = self._next_version(tenant)  # 成员或身份代数变化 -> 新主体代数
                changes.append(
                    {"principal_no": pno, "generation": gen, "members": members}
                )
            new_rows.append((tenant, pno, gen, members_json, genmap_json))
        self.conn.execute("DELETE FROM current_components WHERE tenant=?", (tenant,))
        self.conn.executemany(
            "INSERT INTO current_components "
            "(tenant, principal_no, generation, members, gen_map) VALUES (?,?,?,?,?)",
            new_rows,
        )
        current = {(r[3], r[4]): (r[1], r[2]) for r in new_rows}
        invalidated = []
        for sid, pno, gen, members_json, genmap_json in self.conn.execute(
            "SELECT session_id, principal_no, generation, members, gen_map "
            "FROM sessions WHERE tenant=? AND valid=1",
            (tenant,),
        ).fetchall():
            if current.get((members_json, genmap_json)) != (pno, gen):
                self.conn.execute(
                    "UPDATE sessions SET valid=0 WHERE tenant=? AND session_id=?",
                    (tenant, sid),
                )
                invalidated.append(sid)
        return changes, sorted(invalidated)

    def _principal_of(self, tenant, identity):
        for pno, gen, members_json, genmap_json in self.conn.execute(
            "SELECT principal_no, generation, members, gen_map "
            "FROM current_components WHERE tenant=?",
            (tenant,),
        ).fetchall():
            members = json.loads(members_json)
            if identity in members:
                return {
                    "principal_no": pno,
                    "generation": gen,
                    "members": members,
                    "gen_map": json.loads(genmap_json),
                }
        return None

    # ------------------------------------------------------------------ 操作

    def _op_create_identity(self, tenant, op, clock):
        identity = op["identity"]
        provider = op["provider"]
        for token in (tenant, identity, provider):
            if "|" in str(token):
                raise Reject("E_MALFORMED_INPUT", "标识符不得包含 '|'")
        row = self.conn.execute(
            "SELECT 1 FROM identities WHERE tenant=? AND identity=?",
            (tenant, identity),
        ).fetchone()
        if row is not None:
            raise Reject("E_IDENTITY_EXISTS", f"身份已存在: {identity}")
        self.conn.execute(
            "INSERT INTO identities (tenant, identity, provider, generation) "
            "VALUES (?,?,?,1)",
            (tenant, identity, provider),
        )
        return {
            "identity": identity,
            "provider": provider,
            "identity_generation": 1,
            "invalidated_sessions": [],
        }

    def _op_renew_identity(self, tenant, op, clock):
        identity = op["identity"]
        row = self.conn.execute(
            "SELECT generation FROM identities WHERE tenant=? AND identity=?",
            (tenant, identity),
        ).fetchone()
        if row is None:
            raise Reject("E_IDENTITY_NOT_FOUND", f"身份不存在: {identity}")
        new_gen = row[0] + 1
        self.conn.execute(
            "UPDATE identities SET generation=? WHERE tenant=? AND identity=?",
            (new_gen, tenant, identity),
        )
        changes, invalidated = self._recompute(tenant, clock)
        return {
            "identity": identity,
            "identity_generation": new_gen,
            "principal": self._principal_of(tenant, identity),
            "changes": changes,
            "invalidated_sessions": invalidated,
        }

    def _check_edge_payload(self, tenant, op, clock, ids, a, b, edge_id):
        """公共校验:有效窗口、代数钉扎、双方确认令牌。返回 (valid_from, valid_until)。"""
        valid_from = int(op["valid_from"])
        valid_until = int(op["valid_until"])
        if not (valid_from <= clock < valid_until):
            raise Reject(
                "E_VALIDITY_WINDOW",
                f"有效窗口 [{valid_from},{valid_until}) 不覆盖当前时钟 {clock}",
            )
        gen_a = ids[a]["generation"]
        gen_b = ids[b]["generation"]
        if op.get("gen_a") != gen_a or op.get("gen_b") != gen_b:
            raise Reject(
                "E_STALE_GENERATION",
                f"确认钉扎的代数 ({op.get('gen_a')},{op.get('gen_b')}) "
                f"与当前 ({gen_a},{gen_b}) 不符",
            )
        exp_a = make_confirmation(
            tenant, edge_id, ids[a]["provider"], a, gen_a, valid_until
        )
        exp_b = make_confirmation(
            tenant, edge_id, ids[b]["provider"], b, gen_b, valid_until
        )
        if op.get("confirm_a") != exp_a or op.get("confirm_b") != exp_b:
            raise Reject("E_CONFIRMATION_INVALID", "提供方确认令牌校验失败")
        return valid_from, valid_until

    def _op_associate(self, tenant, op, clock):
        edge_id = op["edge_id"]
        a, b = op["a"], op["b"]
        if a == b:
            raise Reject("E_SAME_IDENTITY", "关联两端不能是同一身份")
        ids = self._identities(tenant)
        if a not in ids or b not in ids:
            missing = a if a not in ids else b
            raise Reject("E_IDENTITY_NOT_FOUND", f"身份不存在: {missing}")
        if ids[a]["provider"] == ids[b]["provider"]:
            raise Reject("E_SAME_PROVIDER", "关联两端属于同一提供方")
        row = self.conn.execute(
            "SELECT state FROM edges WHERE tenant=? AND edge_id=?",
            (tenant, edge_id),
        ).fetchone()
        if row is not None and row[0] == "active":
            raise Reject("E_EDGE_EXISTS", f"边已存在且活动: {edge_id}")
        valid_from, valid_until = self._check_edge_payload(
            tenant, op, clock, ids, a, b, edge_id
        )
        gen_a = ids[a]["generation"]
        gen_b = ids[b]["generation"]
        # 提供方冲突检查:合并后的分量不得含同一提供方的两个身份
        comps, _ = self._components_now(tenant)
        merged = set()
        for comp in comps:
            if a in comp or b in comp:
                merged.update(comp)
        seen = {}
        for m in sorted(merged):
            p = ids[m]["provider"]
            if p in seen:
                raise Reject(
                    "E_PROVIDER_CONFLICT",
                    f"合并后分量将同时包含提供方 {p} 的身份 {seen[p]} 与 {m}",
                )
            seen[p] = m
        if row is None:
            self.conn.execute(
                "INSERT INTO edges (tenant, edge_id, a, b, gen_a, gen_b, "
                "confirm_a, confirm_b, valid_from, valid_until, state, cleared_clock) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,'active',NULL)",
                (tenant, edge_id, a, b, gen_a, gen_b, op["confirm_a"],
                 op["confirm_b"], valid_from, valid_until),
            )
        else:
            # 已清除的边可由新 request_id 重新激活(旧 request_id 只能重放)
            self.conn.execute(
                "UPDATE edges SET a=?, b=?, gen_a=?, gen_b=?, confirm_a=?, "
                "confirm_b=?, valid_from=?, valid_until=?, state='active', "
                "cleared_clock=NULL WHERE tenant=? AND edge_id=?",
                (a, b, gen_a, gen_b, op["confirm_a"], op["confirm_b"],
                 valid_from, valid_until, tenant, edge_id),
            )
        changes, invalidated = self._recompute(tenant, clock)
        principal = self._principal_of(tenant, a)
        return {
            "edge_id": edge_id,
            "principal_no": principal["principal_no"],
            "principal_generation": principal["generation"],
            "members": principal["members"],
            "changes": changes,
            "invalidated_sessions": invalidated,
        }

    def _op_renew_edge(self, tenant, op, clock):
        edge_id = op["edge_id"]
        row = self.conn.execute(
            "SELECT a, b, state FROM edges WHERE tenant=? AND edge_id=?",
            (tenant, edge_id),
        ).fetchone()
        if row is None:
            raise Reject("E_EDGE_NOT_FOUND", f"边不存在: {edge_id}")
        if row[2] != "active":
            raise Reject("E_EDGE_CLEARED", f"边已清除({row[2]}): {edge_id}")
        a, b = row[0], row[1]
        ids = self._identities(tenant)
        valid_from, valid_until = self._check_edge_payload(
            tenant, op, clock, ids, a, b, edge_id
        )
        self.conn.execute(
            "UPDATE edges SET gen_a=?, gen_b=?, confirm_a=?, confirm_b=?, "
            "valid_from=?, valid_until=? WHERE tenant=? AND edge_id=?",
            (ids[a]["generation"], ids[b]["generation"], op["confirm_a"],
             op["confirm_b"], valid_from, valid_until, tenant, edge_id),
        )
        changes, invalidated = self._recompute(tenant, clock)
        return {
            "edge_id": edge_id,
            "valid_until": valid_until,
            "changes": changes,
            "invalidated_sessions": invalidated,
        }

    def _op_revoke(self, tenant, op, clock):
        edge_id = op["edge_id"]
        row = self.conn.execute(
            "SELECT state FROM edges WHERE tenant=? AND edge_id=?",
            (tenant, edge_id),
        ).fetchone()
        if row is None:
            raise Reject("E_EDGE_NOT_FOUND", f"边不存在: {edge_id}")
        if row[0] != "active":
            raise Reject("E_EDGE_CLEARED", f"边已清除({row[0]}): {edge_id}")
        self.conn.execute(
            "UPDATE edges SET state='revoked', cleared_clock=? "
            "WHERE tenant=? AND edge_id=?",
            (clock, tenant, edge_id),
        )
        changes, invalidated = self._recompute(tenant, clock)
        principals = [
            {"principal_no": r[0], "generation": r[1], "members": json.loads(r[2])}
            for r in self.conn.execute(
                "SELECT principal_no, generation, members FROM current_components "
                "WHERE tenant=? ORDER BY principal_no",
                (tenant,),
            ).fetchall()
        ]
        return {
            "edge_id": edge_id,
            "resulting_principals": principals,
            "changes": changes,
            "invalidated_sessions": invalidated,
        }

    def _op_login(self, tenant, op, clock):
        request_id = op.get("request_id")
        if request_id is None:
            raise Reject("E_MALFORMED_INPUT", "登录必须携带 request_id")
        members = sorted(set(op["identities"]))
        comps, _ = self._components_now(tenant)
        principals = [c for c in comps if len(c) >= 2]
        if members not in principals:
            raise Reject(
                "E_SESSION_MISMATCH",
                f"身份集合 {members} 不是任何当前主体的确切成员集",
            )
        principal = self._principal_of(tenant, members[0])
        session_id = "S-" + request_id
        gen_map = {m: self._identities(tenant)[m]["generation"] for m in members}
        self.conn.execute(
            "INSERT INTO sessions (tenant, session_id, principal_no, generation, "
            "members, gen_map, created_clock, valid) VALUES (?,?,?,?,?,?,?,1)",
            (tenant, session_id, principal["principal_no"], principal["generation"],
             json.dumps(members), json.dumps(gen_map, sort_keys=True), clock),
        )
        return {
            "session_id": session_id,
            "principal_no": principal["principal_no"],
            "principal_generation": principal["generation"],
            "members": members,
            "invalidated_sessions": [],
        }

    def _op_check_session(self, tenant, op, clock):
        session_id = op["session_id"]
        row = self.conn.execute(
            "SELECT valid, principal_no, generation FROM sessions "
            "WHERE tenant=? AND session_id=?",
            (tenant, session_id),
        ).fetchone()
        if row is None:
            raise Reject("E_SESSION_NOT_FOUND", f"会话不存在: {session_id}")
        return {
            "session_id": session_id,
            "valid": bool(row[0]),
            "principal_no": row[1],
            "principal_generation": row[2],
            "invalidated_sessions": [],
        }

    def _op_get_principal(self, tenant, op, clock):
        identity = op["identity"]
        ids = self._identities(tenant)
        if identity not in ids:
            raise Reject("E_IDENTITY_NOT_FOUND", f"身份不存在: {identity}")
        return {
            "identity": identity,
            "principal": self._principal_of(tenant, identity),
            "invalidated_sessions": [],
        }

    # ------------------------------------------------------------------ 导出

    def dump_state(self):
        """导出全量确定性状态(供测试与参考模型对比)。"""
        dump = {}
        rows = self.conn.execute("SELECT tenant FROM tenants ORDER BY tenant").fetchall()
        for (tenant,) in rows:
            clock, counter = self.conn.execute(
                "SELECT clock, version_counter FROM tenants WHERE tenant=?", (tenant,)
            ).fetchone()
            identities = [
                {"identity": r[0], "provider": r[1], "generation": r[2]}
                for r in self.conn.execute(
                    "SELECT identity, provider, generation FROM identities "
                    "WHERE tenant=? ORDER BY identity",
                    (tenant,),
                ).fetchall()
            ]
            edges = [
                {"edge_id": r[0], "state": r[1], "a": r[2], "b": r[3]}
                for r in self.conn.execute(
                    "SELECT edge_id, state, a, b FROM edges "
                    "WHERE tenant=? ORDER BY edge_id",
                    (tenant,),
                ).fetchall()
            ]
            principals = [
                {"principal_no": r[0], "generation": r[1],
                 "members": json.loads(r[2]), "gen_map": json.loads(r[3])}
                for r in self.conn.execute(
                    "SELECT principal_no, generation, members, gen_map "
                    "FROM current_components WHERE tenant=? ORDER BY principal_no",
                    (tenant,),
                ).fetchall()
            ]
            sessions = [
                {"session_id": r[0], "valid": bool(r[1]),
                 "principal_no": r[2], "generation": r[3]}
                for r in self.conn.execute(
                    "SELECT session_id, valid, principal_no, generation "
                    "FROM sessions WHERE tenant=? ORDER BY session_id",
                    (tenant,),
                ).fetchall()
            ]
            dump[tenant] = {
                "clock": clock,
                "version_counter": counter,
                "identities": identities,
                "edges": edges,
                "principals": principals,
                "sessions": sessions,
            }
        return dump
