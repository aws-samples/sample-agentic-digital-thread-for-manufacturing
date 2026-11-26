# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""UI onboarding payload sequence for the typed connectors (audit + bug 1).

Simulates the EXACT payload sequence the onboarding UI
(``frontend/src/views/onboard/`` — ``flow.ts``/``payloads.ts``, the port of the
legacy ``obStart``) sends for the two
typed connectors — Amazon Athena and AWS IoT SiteWise (Direct):

  1. connect  — POST /api/onboard/connect body with ``connection_config``
                including ``role_arn`` + ``external_id`` (the modal fields);
  2. discover — body carries the SAME ``connection_config`` (obStart's connCfg);
  3. provision — body carries the SAME ``connection_config`` (provBody).

Asserts the credential-routing contract:
  * the catalog entry NEVER contains ``external_id`` (allowlist sanitization —
    secrets ride the wizard bodies and/or the conn/<id> secret, never the catalog);
  * provision mints/updates the ``conn/<id>`` secret with ``{'external_id': ...}``
    (``write_connection`` secret path) and keeps ``role_arn`` in the non-secret
    node config;
  * the agent-config payload passed to ``start_agent`` carries BOTH ``role_arn``
    and ``external_id``.

Bug 1 (live incident, 2026-07-14): the SiteWise (Direct) provision branch passed
the body/catalog snapshot straight to ``start_agent`` without merging the
cross-account credentials from the persisted connection — so a RE-onboard whose
body carries no config (catalog fallback is SANITIZED) minted a credential-less
runtime + agent-config secret. The re-onboard tests below drive provision with NO
body config and a sanitized catalog entry, and assert the secret-sourced
``external_id`` still lands in ``start_agent``'s connection_config (the
``_with_cross_account_credentials`` merge). The Athena provision branch had the
SAME gap (audit finding) — fixed and covered identically.

Offline — no AWS, no Neptune, no network.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.athena_tools as athena_tools  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as onboard_agent  # noqa: E402
import agents.sitewise_read as sitewise_read  # noqa: E402
from agents.models import DiscoverRequest, ProvisionRequest  # noqa: E402

_ROLE = "arn:aws:iam::111122223333:role/ExampleReadOnlyRole"
_XID = "example-external-id-123"


class _FakeCatalog:
    """Catalog double: registration + connection_config persistence capture."""

    def __init__(self):
        self.entries: dict[str, dict] = {}
        self.persisted: list[dict] = []

    def get(self, system_id):
        return self.entries.get(system_id)

    lookup = get  # the hydrating seam; in-memory fake has nothing to hydrate

    def add(self, entry):
        self.entries[entry["id"]] = dict(entry)

    def update_status(self, system_id, status):
        pass

    def set_connection_config(self, system_id, connection_config, *, secret_arn=None):
        self.persisted.append({"system_id": system_id,
                               "connection_config": dict(connection_config or {}),
                               "secret_arn": secret_arn})
        return True

    def get_onboarded_ids(self):
        return set()


class _CaptureAgentManager:
    def __init__(self):
        self._agents = {}
        self.calls: list[dict] = []

    def start_agent(self, **kwargs):
        self.calls.append(dict(kwargs))
        return "mcp://test-runtime/invoke"

    def get_mcp_url(self, _system_id):
        return "mcp://test-runtime/invoke"


class _Boom:
    def __getattr__(self, _name):
        raise RuntimeError("AWS disabled in unit tests")


def _no_external_id_anywhere(cfg: dict):
    assert "external_id" not in (cfg or {}), f"external_id leaked into catalog: {cfg}"
    assert _XID not in repr(cfg), f"external_id VALUE leaked into catalog: {cfg}"


# --------------------------------------------------------------------------- #
# Shared harness
# --------------------------------------------------------------------------- #
def _install(monkeypatch, fake_catalog):
    """Neutralize impure seams; capture write_connection + start_agent."""
    wc_calls: list[dict] = []

    def _fake_write_connection(system_id, *, non_secret, secret=None, secret_arn=None,
                               require_node=False, strict=False, allow_schema_change=False):
        wc_calls.append({"system_id": system_id, "non_secret": dict(non_secret or {}),
                         "secret": dict(secret) if secret else secret,
                         "secret_arn": secret_arn})
        return secret_arn or ("arn:aws:secretsmanager:test:conn" if secret else "")

    mgr = _CaptureAgentManager()
    monkeypatch.setattr(onboard, "catalog", fake_catalog)
    monkeypatch.setattr(onboard_agent, "catalog", fake_catalog)
    # Registration writes its credentials to adt/conn/<id> (a user registration
    # whose write fails is not registered), so Secrets Manager works here.
    monkeypatch.setattr(cr, "_store_connection_secret", lambda sid, secret: (
        f"arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/{sid}-AbCdEf"))
    monkeypatch.setattr(cr, "invalidate_secret", lambda arn: None)
    monkeypatch.setattr(onboard, "neptune_write", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "bedrock_call", lambda *_a, **_k: "stub")
    monkeypatch.setattr(onboard, "pg", lambda *_a, **_k: {"rows": []})
    monkeypatch.setattr(onboard, "add_system_agent", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_detect_internal_joinpaths", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "_agent_manager", mgr)
    monkeypatch.setattr(cr, "write_connection", _fake_write_connection)
    monkeypatch.setattr("boto3.client", lambda *_a, **_k: _Boom())
    monkeypatch.setattr("agents.aws_session.boto3_client_for",
                        lambda *_a, **_k: _Boom(), raising=False)
    import agents.self_learning as self_learning
    monkeypatch.setattr(self_learning, "get_accepted_for_system", lambda *_a, **_k: [])
    return mgr, wc_calls


def _connect(connection_type: str, connection_config: dict) -> dict:
    """Step 1 — the exact connect body startCustomOnboard() sends."""
    return onboard_agent.connect_new_system(
        name="Example System", system_type="Custom", plant="",
        connection_type=connection_type, connection_config=dict(connection_config),
    )


def _provision_body(system_id: str, connection_type: str, conn_cfg: dict | None) -> ProvisionRequest:
    """Step 3 — the exact provBody obStart() sends (connCfg threaded through).
    ``conn_cfg=None`` models the re-onboard driver case (no config in the body)."""
    kwargs = dict(
        system_id=system_id, system_name="Example System", plant="",
        schema="", rules=[], mappings=[],
        connection_type=connection_type, connection_string="",
    )
    if conn_cfg is not None:
        kwargs["connection_config"] = dict(conn_cfg)
    return ProvisionRequest(**kwargs)


# --------------------------------------------------------------------------- #
# SiteWise (Direct)
# --------------------------------------------------------------------------- #
_SW_CFG = {"region": "us-east-1", "role_arn": _ROLE, "external_id": _XID}


def _sw_discover(monkeypatch, system_id, conn_cfg):
    """Step 2 — the exact discover body obStart() sends; capture the reader cfg."""
    seen = {}

    def _capture_models(cfg, region, max_models=None):
        seen["cfg"] = dict(cfg)
        return []

    monkeypatch.setattr(sitewise_read, "list_asset_models_with_properties", _capture_models)
    monkeypatch.setattr(sitewise_read, "list_assets",
                        lambda cfg, region, asset_model_id="": [])
    body = DiscoverRequest(system_id=system_id, connection_type="SiteWise (Direct)",
                           connection_config=dict(conn_cfg))
    asyncio.run(onboard.onboard_discover(body))
    return seen


def test_sitewise_ui_sequence_routes_external_id_to_secret_and_agent(monkeypatch):
    cat = _FakeCatalog()
    mgr, wc_calls = _install(monkeypatch, cat)

    # 1) connect — catalog entry is sanitized (role_arn kept, external_id NEVER).
    res = _connect("SiteWise (Direct)", _SW_CFG)
    sid = res["system_id"]
    entry = cat.entries[sid]
    assert entry["connection_config"]["role_arn"] == _ROLE
    _no_external_id_anywhere(entry["connection_config"])

    # 2) discover — the body config (incl. external_id) reaches the SiteWise
    #    reader; any catalog persistence stays sanitized.
    seen = _sw_discover(monkeypatch, sid, _SW_CFG)
    assert seen["cfg"].get("role_arn") == _ROLE
    assert seen["cfg"].get("external_id") == _XID
    for p in cat.persisted:
        _no_external_id_anywhere(p["connection_config"])

    # 3) provision — same body config.
    onboard._do_provision(_provision_body(sid, "SiteWise (Direct)", _SW_CFG))

    # start_agent's connection_config carries BOTH cross-account keys.
    sw_call = next(c for c in mgr.calls if c["connection_type"] == "SiteWise (Direct)")
    assert sw_call["connection_config"].get("role_arn") == _ROLE
    assert sw_call["connection_config"].get("external_id") == _XID

    # conn/<id> secret receives external_id; role_arn stays non-secret.
    wc = next(c for c in wc_calls if c["system_id"] == sid)
    assert wc["secret"] == {"external_id": _XID}
    assert wc["non_secret"].get("role_arn") == _ROLE
    _no_external_id_anywhere(wc["non_secret"])

    # Catalog entry/persistence NEVER carries external_id.
    for p in cat.persisted:
        _no_external_id_anywhere(p["connection_config"])
    _no_external_id_anywhere(cat.entries[sid].get("connection_config", {}))


def test_sitewise_reonboard_merges_secret_credentials_into_start_agent(monkeypatch):
    """Bug 1: NO body config (re-onboard) + SANITIZED catalog entry → the merge
    must recover external_id from the persisted connection (conn/<id> secret via
    resolve_connection) so the runtime + agent-config secret are NOT credential-less."""
    cat = _FakeCatalog()
    mgr, wc_calls = _install(monkeypatch, cat)
    sid = "example-sw"
    cat.entries[sid] = {
        "id": sid, "name": "Example SW", "connection_type": "SiteWise (Direct)",
        "status": "registered",
        # sanitized snapshot — external_id stripped by design.
        "connection_config": {"region": "us-east-1", "role_arn": _ROLE},
    }
    monkeypatch.setattr(cr, "resolve_connection", lambda _sid: {
        "system_id": sid, "connection_type": "SiteWise (Direct)",
        "region": "us-east-1", "role_arn": _ROLE, "external_id": _XID,
    })

    onboard._do_provision(_provision_body(sid, "SiteWise (Direct)", None))

    sw_call = next(c for c in mgr.calls if c["connection_type"] == "SiteWise (Direct)")
    assert sw_call["connection_config"].get("role_arn") == _ROLE
    assert sw_call["connection_config"].get("external_id") == _XID
    # And the conn/<id> secret is re-minted/updated from the merged value.
    wc = next(c for c in wc_calls if c["system_id"] == sid)
    assert wc["secret"] == {"external_id": _XID}


# --------------------------------------------------------------------------- #
# Amazon Athena
# --------------------------------------------------------------------------- #
_ATH_CFG = {"workgroup": "wg", "database": "example_db", "catalog": "AwsDataCatalog",
            "region": "us-east-1", "role_arn": _ROLE, "external_id": _XID}


def _ath_discover(monkeypatch, system_id, conn_cfg):
    seen = {}

    def _capture_resolve(cfg):
        seen["cfg"] = dict(cfg)
        return dict(cfg)

    monkeypatch.setattr(athena_tools, "_resolve_config", _capture_resolve)
    monkeypatch.setattr(athena_tools, "run_athena_query",
                        lambda *_a, **_k: {"rows": [], "columns": []})
    body = DiscoverRequest(system_id=system_id, connection_type="Athena",
                           connection_config=dict(conn_cfg))
    asyncio.run(onboard.onboard_discover(body))
    return seen


def test_athena_ui_sequence_routes_external_id_to_secret_and_agent(monkeypatch):
    cat = _FakeCatalog()
    mgr, wc_calls = _install(monkeypatch, cat)

    # 1) connect
    res = _connect("Athena", _ATH_CFG)
    sid = res["system_id"]
    entry = cat.entries[sid]
    assert entry["connection_config"]["role_arn"] == _ROLE
    assert entry["connection_config"]["workgroup"] == "wg"
    _no_external_id_anywhere(entry["connection_config"])

    # 2) discover — the body config (incl. external_id) reaches the Athena client.
    seen = _ath_discover(monkeypatch, sid, _ATH_CFG)
    assert seen["cfg"].get("role_arn") == _ROLE
    assert seen["cfg"].get("external_id") == _XID
    for p in cat.persisted:
        _no_external_id_anywhere(p["connection_config"])

    # 3) provision — same body config.
    onboard._do_provision(_provision_body(sid, "Athena", _ATH_CFG))

    ath_call = next(c for c in mgr.calls if c["connection_type"] == "Athena")
    assert ath_call["connection_config"].get("role_arn") == _ROLE
    assert ath_call["connection_config"].get("external_id") == _XID

    wc = next(c for c in wc_calls if c["system_id"] == sid)
    assert wc["secret"] == {"external_id": _XID}
    assert wc["non_secret"].get("role_arn") == _ROLE
    _no_external_id_anywhere(wc["non_secret"])
    for p in cat.persisted:
        _no_external_id_anywhere(p["connection_config"])


def test_athena_reonboard_merges_secret_credentials_into_start_agent(monkeypatch):
    """Audit finding: the Athena provision branch had the SAME missing merge as
    SiteWise (Direct) — a re-onboard with no body config fell back to the
    SANITIZED catalog entry and minted a credential-less runtime. Covered by the
    mirrored fix."""
    cat = _FakeCatalog()
    mgr, wc_calls = _install(monkeypatch, cat)
    sid = "example-ath"
    cat.entries[sid] = {
        "id": sid, "name": "Example Athena", "connection_type": "Athena",
        "status": "registered",
        "connection_config": {"workgroup": "wg", "database": "example_db",
                              "region": "us-east-1", "role_arn": _ROLE},
    }
    monkeypatch.setattr(cr, "resolve_connection", lambda _sid: {
        "system_id": sid, "connection_type": "Athena",
        "workgroup": "wg", "database": "example_db", "region": "us-east-1",
        "role_arn": _ROLE, "external_id": _XID,
    })

    onboard._do_provision(_provision_body(sid, "Athena", None))

    ath_call = next(c for c in mgr.calls if c["connection_type"] == "Athena")
    assert ath_call["connection_config"].get("role_arn") == _ROLE
    assert ath_call["connection_config"].get("external_id") == _XID
    wc = next(c for c in wc_calls if c["system_id"] == sid)
    assert wc["secret"] == {"external_id": _XID}


def test_sitewise_provisioning_passes_context_without_query_policy(monkeypatch):
    cat = _FakeCatalog()
    mgr, _wc_calls = _install(monkeypatch, cat)
    sid = "example-sw-policy"
    cat.entries[sid] = {
        "id": sid,
        "name": "Example SW",
        "connection_type": "SiteWise (Direct)",
        "status": "registered",
        "connection_config": {"region": "us-east-1"},
    }
    onboard._do_provision(_provision_body(sid, "SiteWise (Direct)", {"region": "us-east-1"}))
    sw_call = next(c for c in mgr.calls if c["connection_type"] == "SiteWise (Direct)")
    base_prompt = sw_call["system_prompt"]
    assert "AWS IoT SiteWise agent" in base_prompt
    assert "exact asset models, properties, and values" in base_prompt
    assert "sitewise_list_asset_models" not in base_prompt
    assert "sitewise_list_assets" not in base_prompt
    assert "TRAVERSAL BUDGET" not in base_prompt
