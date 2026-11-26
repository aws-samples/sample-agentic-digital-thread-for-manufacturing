# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""System-agent identity-claim verification + reference enforcement (ADR-0049, I2.2/I2.3).

Offline pins:

  * the reserved arg is POPPED UNCONDITIONALLY (enforcement on or off, valid or
    garbage value) — external callers can never smuggle it through to tools;
  * enforcement policy is LIVE whenever SYSTEM_REQUIRED_GROUP is set (unset
    group ⇒ allow-all, engine-generic); group set WITHOUT the signing-key ARN
    fails CLOSED (honest deny), consistent with the unreadable-key path;
  * allow/deny paths: valid claim + group ⇒ allow; group-miss, missing claim,
    invalid/expired claim, unreadable key ⇒ HONEST deny naming the system;
  * MARKER NON-OVERLAP (the hard constraint): every deny string this runtime can
    emit must NEVER substring-match the orchestrator's retry markers
    (``_AUTH_TOOL_ERROR_MARKERS`` / ``_TRANSIENT_TOOL_ERROR_MARKERS``) — a
    denial the orchestrator silently retries would mask a genuine authz
    decision. Imports BOTH marker tuples and asserts against the exact deny
    strings;
  * the ASGI middleware pops/denies at the transport seam before FastMCP;
  * ``AgentManager._base_env_vars`` carries SYSTEM_IDENTITY_CLAIM_SECRET_ARN
    (propagated, never SYSTEM_REQUIRED_GROUP — that stays a per-system operator
    choice).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agents import runtime_app as ra
from agents import runtime_asgi as rasgi
from agents.auth import identity_claim as ic

SECRET = "runtime-test-key"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
SYSTEM = "erp"
GROUP = "manufacturing-ops"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(ra.REQUIRED_GROUP_ENV, raising=False)
    monkeypatch.delenv(ra.IDENTITY_CLAIM_SECRET_ARN_ENV, raising=False)
    ra.reset_identity_claim_secret_cache()
    yield
    ra.reset_identity_claim_secret_cache()


@pytest.fixture()
def enforcing(monkeypatch):
    """Enforcement ON: both envs set, signing key readable."""
    monkeypatch.setenv(ra.REQUIRED_GROUP_ENV, GROUP)
    monkeypatch.setenv(ra.IDENTITY_CLAIM_SECRET_ARN_ENV, "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/identity-claim-key-AbCdEf")
    monkeypatch.setattr(rasgi, "_fetch_identity_claim_secret", lambda: SECRET)


# --------------------------------------------------------------------------- #
# MARKER NON-OVERLAP — the pinned hard constraint
# --------------------------------------------------------------------------- #
def test_deny_texts_never_match_retry_markers():
    """Every deny string vs BOTH orchestrator marker tuples: zero overlap.

    The orchestrator classifies retryable errors by lowercased substring match
    (``_is_auth_tool_error`` / ``_is_transient_tool_error``); a deny text
    containing any marker would be silently retried and the genuine denial
    masked. Checked against realistic AND template values so wording drift in
    either place fails this test.
    """
    from agents.orchestrator import (
        _AUTH_TOOL_ERROR_MARKERS,
        _TRANSIENT_TOOL_ERROR_MARKERS,
    )

    all_markers = _AUTH_TOOL_ERROR_MARKERS + _TRANSIENT_TOOL_ERROR_MARKERS
    assert "authorization error when sending message" in all_markers  # sanity: right tuple imported

    deny_variants = (
        ra.identity_deny_texts()
        + ra.identity_deny_texts(system=SYSTEM, group=GROUP)
        + ra.identity_deny_texts(system="digital-thread", group="plant-managers")
        + (
            ra.IDENTITY_DENY_MISSING,
            ra.IDENTITY_DENY_INVALID,
            ra.IDENTITY_DENY_GROUP,
            ra.IDENTITY_DENY_KEY_UNREADABLE,
            ra.IDENTITY_DENY_KEY_UNCONFIGURED,
        )
    )
    for deny in deny_variants:
        lowered = deny.lower()
        for marker in all_markers:
            assert marker not in lowered, (
                f"deny text would be silently retried by the orchestrator "
                f"(marker {marker!r} found): {deny!r}"
            )


def test_deny_texts_name_the_system():
    for deny in ra.identity_deny_texts(system=SYSTEM, group=GROUP):
        assert f"'{SYSTEM}'" in deny


# --------------------------------------------------------------------------- #
# pop-always (enforcement OFF)
# --------------------------------------------------------------------------- #
def test_reserved_arg_popped_when_enforcement_disabled():
    args = {"sql": "SELECT 1", ic.RESERVED_CLAIM_ARG: "smuggled-value"}
    deny = ra.enforce_identity_claim(args, SYSTEM)
    assert deny is None
    assert ic.RESERVED_CLAIM_ARG not in args
    assert args == {"sql": "SELECT 1"}


def test_reserved_arg_popped_even_with_secret_env_but_no_group(monkeypatch):
    monkeypatch.setenv(ra.IDENTITY_CLAIM_SECRET_ARN_ENV, "arn:aws:secretsmanager:us-east-1:111122223333:secret:x")
    args = {ic.RESERVED_CLAIM_ARG: "whatever"}
    assert ra.enforce_identity_claim(args, SYSTEM) is None
    assert args == {}


def test_group_without_secret_env_fails_closed(monkeypatch):
    """Half-configured (group but no key ARN): fail CLOSED, still pop.

    Gate finding: a freshly provisioned runtime carries ARN="" until the next
    deploy's env refresh — silently allowing there would void the operator's
    explicit group restriction. Consistent with the key-unreadable deny path.
    """
    monkeypatch.setenv(ra.REQUIRED_GROUP_ENV, GROUP)
    args = {ic.RESERVED_CLAIM_ARG: "x", "q": 1}
    deny = ra.enforce_identity_claim(args, SYSTEM)
    assert deny == ra.IDENTITY_DENY_KEY_UNCONFIGURED.format(system=SYSTEM)
    assert f"'{SYSTEM}'" in deny
    assert args == {"q": 1}


# --------------------------------------------------------------------------- #
# enforcement allow/deny paths
# --------------------------------------------------------------------------- #
def test_valid_claim_with_required_group_allows(enforcing):
    token = ic.mint_claim("user-1", ["other", GROUP], SECRET)
    args = {"sql": "SELECT 1", ic.RESERVED_CLAIM_ARG: token}
    assert ra.enforce_identity_claim(args, SYSTEM) is None
    assert ic.RESERVED_CLAIM_ARG not in args


def test_group_miss_denies(enforcing):
    token = ic.mint_claim("user-1", ["viewer"], SECRET)
    args = {ic.RESERVED_CLAIM_ARG: token}
    deny = ra.enforce_identity_claim(args, SYSTEM)
    assert deny == ra.IDENTITY_DENY_GROUP.format(system=SYSTEM, group=GROUP)
    assert ic.RESERVED_CLAIM_ARG not in args


def test_missing_claim_with_enforcement_denies_honestly(enforcing):
    deny = ra.enforce_identity_claim({"sql": "SELECT 1"}, SYSTEM)
    assert deny == ra.IDENTITY_DENY_MISSING.format(system=SYSTEM, group=GROUP)
    assert f"'{SYSTEM}'" in deny


def test_invalid_claim_denies(enforcing):
    args = {ic.RESERVED_CLAIM_ARG: "garbage.token"}
    assert ra.enforce_identity_claim(args, SYSTEM) == ra.IDENTITY_DENY_INVALID.format(system=SYSTEM)


def test_expired_claim_denies(enforcing):
    token = ic.mint_claim("user-1", [GROUP], SECRET, now=1_000)  # long past
    args = {ic.RESERVED_CLAIM_ARG: token}
    assert ra.enforce_identity_claim(args, SYSTEM) == ra.IDENTITY_DENY_INVALID.format(system=SYSTEM)


def test_wrong_key_claim_denies(enforcing):
    token = ic.mint_claim("user-1", [GROUP], "a-different-key")
    args = {ic.RESERVED_CLAIM_ARG: token}
    assert ra.enforce_identity_claim(args, SYSTEM) == ra.IDENTITY_DENY_INVALID.format(system=SYSTEM)


def test_unreadable_key_denies(monkeypatch):
    monkeypatch.setenv(ra.REQUIRED_GROUP_ENV, GROUP)
    monkeypatch.setenv(ra.IDENTITY_CLAIM_SECRET_ARN_ENV, "arn:aws:secretsmanager:us-east-1:111122223333:secret:x")
    monkeypatch.setattr(rasgi, "_fetch_identity_claim_secret", lambda: None)
    token = ic.mint_claim("user-1", [GROUP], SECRET)
    args = {ic.RESERVED_CLAIM_ARG: token}
    assert ra.enforce_identity_claim(args, SYSTEM) == ra.IDENTITY_DENY_KEY_UNREADABLE.format(system=SYSTEM)


# --------------------------------------------------------------------------- #
# MCP body processing (transport seam)
# --------------------------------------------------------------------------- #
def _tools_call_body(arguments, rpc_id=7):
    return json.dumps(
        {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
         "params": {"name": "run_query", "arguments": arguments}}
    ).encode()


def test_body_pop_strips_reserved_arg():
    raw = _tools_call_body({"sql": "SELECT 1", ic.RESERVED_CLAIM_ARG: "x"})
    new_body, deny, rpc_id = ra._pop_identity_from_mcp_body(raw, SYSTEM)
    assert deny is None
    parsed = json.loads(new_body)
    assert ic.RESERVED_CLAIM_ARG not in parsed["params"]["arguments"]
    assert parsed["params"]["arguments"] == {"sql": "SELECT 1"}


def test_body_without_reserved_arg_passes_through_byte_identical():
    raw = _tools_call_body({"sql": "SELECT 1"})
    new_body, deny, _ = ra._pop_identity_from_mcp_body(raw, SYSTEM)
    assert deny is None
    assert new_body == raw  # no rewrite when nothing changed and no enforcement


def test_non_tools_call_and_non_json_pass_through():
    for raw in (
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
        b"\x00not-json",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(),
    ):
        new_body, deny, _ = ra._pop_identity_from_mcp_body(raw, SYSTEM)
        assert deny is None
        assert new_body == raw


def test_body_enforcement_denies_with_rpc_id(enforcing):
    raw = _tools_call_body({"sql": "SELECT 1"}, rpc_id=42)
    _, deny, rpc_id = ra._pop_identity_from_mcp_body(raw, SYSTEM)
    assert deny == ra.IDENTITY_DENY_MISSING.format(system=SYSTEM, group=GROUP)
    assert rpc_id == 42


# --------------------------------------------------------------------------- #
# ASGI middleware (no sockets — direct scope/receive/send driving)
# --------------------------------------------------------------------------- #
def _http_scope(path="/mcp", method="POST", body=b""):
    return {
        "type": "http", "method": method, "path": path,
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
    }


def _drive(middleware, scope, body):
    received_by_app = {}
    sent = []

    async def downstream(scope_, receive_, send_):
        chunks = []
        while True:
            m = await receive_()
            chunks.append(m.get("body", b""))
            if not m.get("more_body"):
                break
        received_by_app["body"] = b"".join(chunks)
        received_by_app["headers"] = dict(scope_.get("headers", []))
        await send_({"type": "http.response.start", "status": 200, "headers": []})
        await send_({"type": "http.response.body", "body": b"downstream"})

    middleware.app = downstream

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    return received_by_app, sent


def test_middleware_pops_arg_before_fastmcp_and_fixes_content_length():
    mw = ra.IdentityClaimASGIMiddleware(None, system_id=SYSTEM)
    body = _tools_call_body({"sql": "SELECT 1", ic.RESERVED_CLAIM_ARG: "smuggled"})
    received, _ = _drive(mw, _http_scope(body=body), body)
    parsed = json.loads(received["body"])
    assert ic.RESERVED_CLAIM_ARG not in parsed["params"]["arguments"]
    assert received["headers"][b"content-length"] == str(len(received["body"])).encode()


def test_middleware_denies_without_reaching_downstream(enforcing):
    mw = ra.IdentityClaimASGIMiddleware(None, system_id=SYSTEM)
    body = _tools_call_body({"sql": "SELECT 1"}, rpc_id=9)
    received, sent = _drive(mw, _http_scope(body=body), body)
    assert "body" not in received  # downstream never invoked
    payload = json.loads(b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"))
    assert payload["id"] == 9
    assert payload["result"]["isError"] is True
    assert payload["result"]["content"][0]["text"] == ra.IDENTITY_DENY_MISSING.format(system=SYSTEM, group=GROUP)


def test_middleware_ignores_other_paths_and_methods():
    for scope in (_http_scope(path="/ping", method="GET"),
                  _http_scope(path="/invocations"),
                  _http_scope(path="/mcp", method="GET")):
        mw = ra.IdentityClaimASGIMiddleware(None, system_id=SYSTEM)
        body = b'{"anything": true}'
        received, _ = _drive(mw, scope, body)
        assert received["body"] == body


def test_middleware_handles_mcp_trailing_slash():
    mw = ra.IdentityClaimASGIMiddleware(None, system_id=SYSTEM)
    body = _tools_call_body({ic.RESERVED_CLAIM_ARG: "x"})
    received, _ = _drive(mw, _http_scope(path="/mcp/", body=body), body)
    assert ic.RESERVED_CLAIM_ARG not in json.loads(received["body"])["params"]["arguments"]


# --------------------------------------------------------------------------- #
# env plumbing (I2.3)
# --------------------------------------------------------------------------- #
def test_base_env_vars_carries_secret_arn_but_never_required_group(monkeypatch):
    from agents.agent_manager import AgentManager

    arn = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/identity-claim-key-AbCdEf"
    monkeypatch.setenv("SYSTEM_IDENTITY_CLAIM_SECRET_ARN", arn)
    env = AgentManager(mode="cloud")._base_env_vars("sys1", "Example Corp", "public", "Athena", "us-east-1", {})
    assert env["SYSTEM_IDENTITY_CLAIM_SECRET_ARN"] == arn
    # SYSTEM_REQUIRED_GROUP is a PER-SYSTEM operator choice — never set by tooling.
    assert "SYSTEM_REQUIRED_GROUP" not in env

    monkeypatch.delenv("SYSTEM_IDENTITY_CLAIM_SECRET_ARN", raising=False)
    env = AgentManager(mode="cloud")._base_env_vars("sys1", "Example Corp", "public", "Athena", "us-east-1", {})
    assert env["SYSTEM_IDENTITY_CLAIM_SECRET_ARN"] == ""


def test_runtime_fetch_returns_none_when_env_unset(monkeypatch):
    monkeypatch.delenv(ra.IDENTITY_CLAIM_SECRET_ARN_ENV, raising=False)
    assert ra._fetch_identity_claim_secret() is None


def test_runtime_app_reexports_the_identity_surface():
    """runtime_app stays the entry-point surface for the pop/enforce contract:
    its names ARE the runtime_asgi implementations (no drift possible)."""
    assert ra.enforce_identity_claim is rasgi.enforce_identity_claim
    assert ra._pop_identity_from_mcp_body is rasgi._pop_identity_from_mcp_body
    assert ra.IdentityClaimASGIMiddleware is rasgi.IdentityClaimASGIMiddleware
    assert ra.identity_deny_texts is rasgi.identity_deny_texts
    assert ra.IDENTITY_CLAIM_ARG == ic.RESERVED_CLAIM_ARG


# --------------------------------------------------------------------------- #
# ADR-0079: identity_claim_var exposes the RAW popped claim (per-request reset)
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _reset_identity_claim_var():
    token = rasgi.identity_claim_var.set(None)
    yield
    rasgi.identity_claim_var.reset(token)


def test_identity_claim_var_is_set_to_the_popped_claim_and_reset():
    raw = _tools_call_body({"sql": "SELECT 1", ic.RESERVED_CLAIM_ARG: "raw.claim"})
    new_body, deny, _ = ra._pop_identity_from_mcp_body(raw, SYSTEM)
    assert deny is None
    assert rasgi.identity_claim_var.get() == "raw.claim"
    assert ic.RESERVED_CLAIM_ARG not in json.loads(new_body)["params"]["arguments"]
    # the next tools/call without a claim resets it
    ra._pop_identity_from_mcp_body(_tools_call_body({"sql": "SELECT 2"}), SYSTEM)
    assert rasgi.identity_claim_var.get() is None


@pytest.mark.parametrize("value", [None, "", 42, {"sub": "x"}])
def test_identity_claim_var_ignores_non_string_values(value):
    rasgi.identity_claim_var.set("stale")
    ra._pop_identity_from_mcp_body(
        _tools_call_body({"sql": "SELECT 1", ic.RESERVED_CLAIM_ARG: value}), SYSTEM)
    assert rasgi.identity_claim_var.get() is None


def test_identity_claim_var_untouched_by_non_tools_call():
    token = rasgi.identity_claim_var.set("kept")
    try:
        ra._pop_identity_from_mcp_body(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(), SYSTEM)
        assert rasgi.identity_claim_var.get() == "kept"
    finally:
        rasgi.identity_claim_var.reset(token)


def test_identity_claim_var_does_not_change_enforcement(enforcing):
    claim = ic.mint_claim("u1", [GROUP], SECRET)
    _, deny, _ = ra._pop_identity_from_mcp_body(
        _tools_call_body({ic.RESERVED_CLAIM_ARG: claim}), SYSTEM)
    assert deny is None
    assert rasgi.identity_claim_var.get() == claim
    _, deny, _ = ra._pop_identity_from_mcp_body(
        _tools_call_body({ic.RESERVED_CLAIM_ARG: "garbage"}), SYSTEM)
    assert deny == ra.IDENTITY_DENY_INVALID.format(system=SYSTEM)
