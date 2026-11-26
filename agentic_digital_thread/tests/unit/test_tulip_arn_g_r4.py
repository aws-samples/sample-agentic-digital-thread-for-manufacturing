# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The ARN credential-source marker survives provision and restart. Offline; no
AWS, no Neptune, no network.

The lifecycle, with NO hand-injected marker on a fixture:
1. provision (``_dp_agent_tulip``) persists ``credential_source="secret_arn"``
   in the node's non-secret config AND the catalog config;
2. a restart rebuilds the catalog entry WITHOUT a config, and the ``/catalog``
   serve (``_served_catalog_entry``) backfills it from the node, restoring the
   marker (Tulip is now a backfilled type);
3. the current-connection summary (``describe_connection``) reports the marker;
4. the backend readers (``_credential_source`` / ``_is_arn_sourced``) see it
   from the node.

These fail on ``a8084904`` (the marker was dropped at provision and not
backfilled for Tulip) and pass after.
"""
from __future__ import annotations

import json
import os
import types

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
from agents.connection_types import TULIP  # noqa: E402

OWN = "111122223333"
SEAT_ARN = f"arn:aws:secretsmanager:us-east-1:{OWN}:secret:customer/tulip/tulip-connection-AbCdEf"
SID = "tulip-mes"
URL = "https://acme.tulip.co"
SEAT = {"schema": 1, "mode": "mock", "base_url": URL, "workspace_id": "wsX",
        "key_scope": "instance", "api_key": "K", "api_secret": "S"}  # nosec B106


@pytest.fixture
def arn_env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ADT_SECRET_ARN_ALLOWLIST", "customer/tulip/")


def _provision_persist_args(monkeypatch):
    """Drive _dp_agent_tulip for an ARN Tulip system; return the (non_secret,
    secret_arn) it hands to _persist_node_connection."""
    captured = {}

    class _FakeMgr:
        _agents: dict = {}
        def start_agent(self, **kwargs):
            return "https://runtime.example/mcp"

    monkeypatch.setattr(onboard, "_agent_manager", _FakeMgr())
    monkeypatch.setattr(onboard, "add_system_agent", lambda *a, **k: None)
    monkeypatch.setattr(onboard, "_tulip_known_tables_desc", lambda ctx, cfg: "")
    monkeypatch.setattr(onboard, "_with_concept_mappings", lambda cfg, items, fields=None: cfg)
    monkeypatch.setattr(onboard, "_dp_tulip_workspace_id", lambda ctx: "wsX")
    monkeypatch.setattr(onboard, "_dp_tulip_key_scope", lambda ctx: "instance")

    def _persist(system_id, *, non_secret, secret=None, secret_arn=None):
        captured["non_secret"] = dict(non_secret)
        captured["secret_arn"] = secret_arn

    monkeypatch.setattr(onboard, "_persist_node_connection", _persist)
    ctx = types.SimpleNamespace(system_id=SID, system_name="Tulip MES", schema="", cm_items=[], field_descriptions=[])
    onboard._dp_agent_tulip(ctx, URL, "", SEAT_ARN)
    return captured


# ── 1. provision persists the marker on the node (no hand-injected fixture) ──

def test_provision_persists_credential_source_on_the_node(monkeypatch):
    args = _provision_persist_args(monkeypatch)
    assert args["non_secret"].get("credential_source") == "secret_arn", args["non_secret"]
    assert args["secret_arn"] == SEAT_ARN
    # And sanitize (the catalog-config path) keeps the marker.
    kept = cr.sanitize_connection_config(args["non_secret"])
    assert kept.get("credential_source") == "secret_arn", kept


def test_provision_omits_the_marker_for_an_inline_tulip(monkeypatch):
    """An inline (own adt/conn/<id>) Tulip system is NOT ARN-sourced: no marker."""
    own = f"arn:aws:secretsmanager:us-east-1:{OWN}:secret:adt/conn/{SID}-AbCdEf"
    captured = {}

    class _FakeMgr:
        _agents: dict = {}
        def start_agent(self, **kwargs):
            return "x"

    monkeypatch.setattr(onboard, "_agent_manager", _FakeMgr())
    monkeypatch.setattr(onboard, "add_system_agent", lambda *a, **k: None)
    monkeypatch.setattr(onboard, "_tulip_known_tables_desc", lambda ctx, cfg: "")
    monkeypatch.setattr(onboard, "_with_concept_mappings", lambda cfg, items, fields=None: cfg)
    monkeypatch.setattr(onboard, "_dp_tulip_workspace_id", lambda ctx: "")
    monkeypatch.setattr(onboard, "_dp_tulip_key_scope", lambda ctx: "")
    monkeypatch.setattr(onboard, "_persist_node_connection",
                        lambda system_id, *, non_secret, secret=None, secret_arn=None:
                        captured.update(non_secret=dict(non_secret)))
    ctx = types.SimpleNamespace(system_id=SID, system_name="Tulip MES", schema="", cm_items=[], field_descriptions=[])
    onboard._dp_agent_tulip(ctx, URL, "tok", own)
    assert "credential_source" not in captured["non_secret"], captured["non_secret"]


# ── 2. restart: /catalog backfills Tulip from the node, restoring the marker ──

def test_catalog_serve_backfills_the_marker_from_the_node_after_restart(monkeypatch, arn_env):
    """A restarted entry has no connection_config; the /catalog serve backfills
    it from the node (Tulip is now a backfilled type), restoring the marker."""
    # The node provision persisted (marker + seat ARN); resolve reads it.
    node = {"connection_config": json.dumps({"base_url": URL, "workspace_id": "wsX",
                                             "key_scope": "instance",
                                             "credential_source": "secret_arn"}),
            "connection_type": TULIP, "secret_arn": SEAT_ARN}
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: dict(node))
    monkeypatch.setattr(cr, "_fetch_secret", lambda arn: dict(SEAT))
    persisted = {}
    monkeypatch.setattr(onboard.catalog, "set_connection_config",
                        lambda sid, cfg, secret_arn=None: persisted.update(cfg=dict(cfg), arn=secret_arn))
    onboard._cfg_backfill_attempted.discard(SID)
    # The restarted entry: real connection_type, NO connection_config.
    entry = {"id": SID, "name": "Tulip MES", "connection_type": TULIP,
             "connection_string": "", "status": "onboarded"}
    served = onboard._served_catalog_entry(entry)
    assert served.get("connection_config", {}).get("credential_source") == "secret_arn", served
    assert persisted.get("cfg", {}).get("credential_source") == "secret_arn", persisted


def test_tulip_is_a_backfilled_type_in_the_catalog_serve():
    """Tulip is in the /catalog lazy-backfill gate (alongside Athena / SiteWise
    Direct), so a restarted Tulip entry gets its node config restored."""
    import inspect
    src = inspect.getsource(onboard._served_catalog_entry)
    assert "TULIP" in src and "ATHENA" in src and "SITEWISE_DIRECT" in src


# ── 3. the current-connection summary reports the marker ──

def test_summary_reports_the_marker_after_provision(monkeypatch, arn_env):
    node = {"connection_config": json.dumps({"base_url": URL, "workspace_id": "wsX",
                                             "key_scope": "instance",
                                             "credential_source": "secret_arn"}),
            "connection_type": TULIP, "secret_arn": SEAT_ARN}
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: dict(node))
    monkeypatch.setattr(cr, "_fetch_secret", lambda arn: dict(SEAT))
    assert cr.describe_connection(SID)["credential_source"] == "secret_arn"


# ── 4. the backend readers see the marker from the node ──

def test_credential_source_and_is_arn_sourced_read_the_node_marker():
    """Post-provision the node carries the marker + the seat pointer, so the
    readers return secret_arn / True without the catalog entry."""
    node = {"connection_config": {"base_url": URL, "credential_source": "secret_arn"},
            "secret_arn": SEAT_ARN}
    assert cr._credential_source(None, node) == "secret_arn"
    assert cr._is_arn_sourced(None, node) is True
    # A node without the marker and without a seat pointer is not ARN-sourced.
    assert cr._is_arn_sourced(None, {"connection_config": {"base_url": URL}}) is False
