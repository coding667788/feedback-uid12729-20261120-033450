"""构造合成操作(op dict)的辅助函数,供 cases 生成器与测试共用。"""
from __future__ import annotations

from .engine import make_confirmation


def create_identity(request_id, tenant, identity, provider, clock=0):
    return {
        "op": "create_identity",
        "request_id": request_id,
        "tenant": tenant,
        "identity": identity,
        "provider": provider,
        "clock": clock,
    }


def renew_identity(request_id, tenant, identity, clock):
    return {
        "op": "renew_identity",
        "request_id": request_id,
        "tenant": tenant,
        "identity": identity,
        "clock": clock,
    }


def associate(request_id, tenant, edge_id, a, provider_a, gen_a,
              b, provider_b, gen_b, valid_from, valid_until, clock):
    return {
        "op": "associate",
        "request_id": request_id,
        "tenant": tenant,
        "edge_id": edge_id,
        "a": a,
        "b": b,
        "gen_a": gen_a,
        "gen_b": gen_b,
        "confirm_a": make_confirmation(tenant, edge_id, provider_a, a, gen_a, valid_until),
        "confirm_b": make_confirmation(tenant, edge_id, provider_b, b, gen_b, valid_until),
        "valid_from": valid_from,
        "valid_until": valid_until,
        "clock": clock,
    }


def renew_edge(request_id, tenant, edge_id, a, provider_a, gen_a,
               b, provider_b, gen_b, valid_from, valid_until, clock):
    return {
        "op": "renew_edge",
        "request_id": request_id,
        "tenant": tenant,
        "edge_id": edge_id,
        "gen_a": gen_a,
        "gen_b": gen_b,
        "confirm_a": make_confirmation(tenant, edge_id, provider_a, a, gen_a, valid_until),
        "confirm_b": make_confirmation(tenant, edge_id, provider_b, b, gen_b, valid_until),
        "valid_from": valid_from,
        "valid_until": valid_until,
        "clock": clock,
    }


def revoke(request_id, tenant, edge_id, clock):
    return {
        "op": "revoke",
        "request_id": request_id,
        "tenant": tenant,
        "edge_id": edge_id,
        "clock": clock,
    }


def login(request_id, tenant, identities, clock):
    return {
        "op": "login",
        "request_id": request_id,
        "tenant": tenant,
        "identities": list(identities),
        "clock": clock,
    }


def check_session(request_id, tenant, session_id, clock):
    return {
        "op": "check_session",
        "request_id": request_id,
        "tenant": tenant,
        "session_id": session_id,
        "clock": clock,
    }


def get_principal(request_id, tenant, identity, clock):
    return {
        "op": "get_principal",
        "request_id": request_id,
        "tenant": tenant,
        "identity": identity,
        "clock": clock,
    }
