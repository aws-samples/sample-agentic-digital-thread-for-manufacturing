# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bug A regression: the system-agent query runtime must resolve its
``connection_config`` from ``CONNECTION_SECRET_ARN`` when ``CONNECTION_CONFIG``
is empty.

ROOT CAUSE: the AgentCore query runtime for non-KnowledgeGraph systems is started
with ``CONNECTION_SECRET_ARN`` (the secret holds the real connection_config incl.
the Athena ``workgroup`` and a cross-account SiteWise ``role_arn``/``external_id``),
but ``runtime_app._run_system_agent`` read ONLY ``os.environ['CONNECTION_CONFIG']``
(empty) and passed ``{}`` to ``create_mcp_server``. The Athena branch then calls
``make_athena_tools({})`` which eagerly raises ``AthenaConfigError('requires
workgroup')`` -> the MCP server never starts -> the gateway target goes
``SYNCHRONIZE_UNSUCCESSFUL`` -> 'Unknown tool'.

FIX: in ``_run_system_agent``, when ``CONNECTION_CONFIG`` is empty AND
``CONNECTION_SECRET_ARN`` is set, resolve the config from the secret via
``system_agent._fetch_credentials`` before it reaches ``create_mcp_server``.
Generic across connection types; a populated ``CONNECTION_CONFIG`` is used
verbatim and the secret is NOT fetched.

Fully offline: ``create_mcp_server``, ``_fetch_credentials`` and ``anyio.run``
are monkeypatched — no AWS, no server, no network.
"""
from __future__ import annotations

import json

import anyio

import agents.runtime_app as runtime_app
import agents.system_agent as system_agent


def _stub_server(monkeypatch):
    """Capture the kwargs create_mcp_server receives and no-op the server run."""
    captured: dict = {}
    monkeypatch.setattr(
        system_agent, "create_mcp_server",
        lambda **kw: captured.update(kw) or object(),
    )
    # _run_system_agent ends with ``anyio.run(run)`` — make it a no-op so the
    # test stops right after create_mcp_server is called.
    monkeypatch.setattr(anyio, "run", lambda *a, **k: None)
    return captured


def test_empty_config_resolves_from_secret(monkeypatch):
    """Empty CONNECTION_CONFIG + a CONNECTION_SECRET_ARN -> the resolved config
    (workgroup + cross-account role_arn/external_id) is what reaches
    create_mcp_server."""
    captured = _stub_server(monkeypatch)
    fetched = {"arn": None}

    def _fake_fetch(arn):
        fetched["arn"] = arn
        return {
            "workgroup": "adt-wg",
            "database": "adt_db",
            "role_arn": "arn:aws:iam::111122223333:role/adt-reader",
            "external_id": "ext-123",
        }

    monkeypatch.setattr(system_agent, "_fetch_credentials", _fake_fetch)

    monkeypatch.setenv("SYSTEM_ID", "athena-sys")
    monkeypatch.setenv("CONNECTION_TYPE", "Athena")
    monkeypatch.delenv("CONNECTION_CONFIG", raising=False)  # empty -> "{}"
    monkeypatch.setenv("CONNECTION_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:111:secret:conn")

    runtime_app._run_system_agent()

    assert fetched["arn"] == "arn:aws:secretsmanager:us-east-1:111:secret:conn"
    cfg = captured["connection_config"]
    assert cfg["workgroup"] == "adt-wg"
    assert cfg["database"] == "adt_db"
    # The latent SiteWise cross-account gap: role_arn/external_id now reach the runtime.
    assert cfg["role_arn"] == "arn:aws:iam::111122223333:role/adt-reader"
    assert cfg["external_id"] == "ext-123"


def test_populated_complete_config_does_not_fetch_secret(monkeypatch):
    """When CONNECTION_CONFIG is already COMPLETE (carries both ``role_arn`` and
    ``external_id``), neither the empty-config secret resolution NOR the DRY
    cross-account merge fetches anything — the config is passed through verbatim.

    (An INCOMPLETE populated config now DOES trigger a best-effort cross-account
    resolve — see test_runtime_app_xacct_creds.py; that is the SiteWise ExternalId
    fix. This test pins the short-circuit for the already-complete case.)"""
    captured = _stub_server(monkeypatch)
    calls = {"n": 0}

    def _fake_fetch(arn):
        calls["n"] += 1
        return {"workgroup": "SHOULD-NOT-BE-USED"}

    monkeypatch.setattr(system_agent, "_fetch_credentials", _fake_fetch)

    complete = {"workgroup": "adt-wg", "database": "adt_db",
                "role_arn": "arn:aws:iam::111122223333:role/adt-reader",
                "external_id": "ext-123"}
    monkeypatch.setenv("SYSTEM_ID", "athena-sys")
    monkeypatch.setenv("CONNECTION_TYPE", "Athena")
    monkeypatch.setenv("CONNECTION_CONFIG", json.dumps(complete))
    monkeypatch.setenv("CONNECTION_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:111:secret:conn")

    runtime_app._run_system_agent()

    assert calls["n"] == 0, "a complete CONNECTION_CONFIG must not fetch the secret"
    assert captured["connection_config"] == complete


def test_no_secret_arn_leaves_empty_config(monkeypatch):
    """No CONNECTION_SECRET_ARN and empty CONNECTION_CONFIG -> the empty-config
    secret branch is skipped; the DRY cross-account merge runs best-effort but a
    local system (no ``role_arn``) is left unchanged, so the (empty) config is
    passed through unchanged (local behaviour unchanged)."""
    captured = _stub_server(monkeypatch)
    calls = {"n": 0}
    monkeypatch.setattr(
        system_agent, "_fetch_credentials",
        lambda arn: calls.__setitem__("n", calls["n"] + 1) or {},
    )
    # The DRY merge is best-effort for a local system; stub it to identity so this
    # test stays offline (no graph/secret lookup) and pins the RESULT: empty in,
    # empty out.
    monkeypatch.setattr(
        "agents.resolve_evidence._with_cross_account_credentials",
        lambda system_id, cfg: cfg,
    )

    monkeypatch.setenv("SYSTEM_ID", "pg-sys")
    monkeypatch.setenv("CONNECTION_TYPE", "PostgreSQL")
    monkeypatch.delenv("CONNECTION_CONFIG", raising=False)
    monkeypatch.delenv("CONNECTION_SECRET_ARN", raising=False)

    runtime_app._run_system_agent()

    assert calls["n"] == 0
    assert captured["connection_config"] == {}


def test_connection_type_passed_through_unchanged(monkeypatch):
    """Task 2C pass-through guarantee: ``_run_system_agent`` forwards the env
    ``CONNECTION_TYPE`` to ``create_mcp_server`` verbatim, alongside the
    secret-resolved ``connection_config``.

    This is what lets the collapsed Mode-A dispatch (Task 1A) select the per-system
    agent path — ``create_mcp_server`` branches on ``connection_type`` and then
    reads ``SYSTEM_QUERY_MODE`` from the environment itself (system_agent.py), so the
    runtime does NOT (and must not) pass a mode; it only needs to hand the connection
    identity through unchanged. Verified across representative connection types so a
    future refactor cannot silently rewrite the type on the way to the server.
    """
    for conn_type in ("Athena", "SiteWise (Direct)", "SiteWise (i3X)", "REST API", "MCP Server"):
        captured = _stub_server(monkeypatch)
        monkeypatch.setattr(
            system_agent, "_fetch_credentials",
            lambda arn: {"workgroup": "adt-wg", "database": "adt_db"},
        )

        monkeypatch.setenv("SYSTEM_ID", "sys-under-test")
        monkeypatch.setenv("CONNECTION_TYPE", conn_type)
        monkeypatch.delenv("CONNECTION_CONFIG", raising=False)
        monkeypatch.setenv(
            "CONNECTION_SECRET_ARN",
            "arn:aws:secretsmanager:us-east-1:111:secret:conn",
        )

        runtime_app._run_system_agent()

        assert captured["connection_type"] == conn_type, (
            f"connection_type must reach create_mcp_server unchanged for {conn_type!r}"
        )
        # And the secret-resolved config still rides along.
        assert captured["connection_config"] == {"workgroup": "adt-wg", "database": "adt_db"}
