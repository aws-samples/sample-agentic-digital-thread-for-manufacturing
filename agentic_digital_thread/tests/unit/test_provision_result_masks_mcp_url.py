# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A provision's ``mcp_url`` is served masked.

For an MCP Server or REST API provision, provision sets the result's
``mcp_url`` from the agent manager's URL
or, when it has none, from the request's ``connection_string``
(``_dp_start_pg_agent``). Nothing masked it, so a URL carrying userinfo or a
``token=`` query parameter was kept in the job record and came back from
``GET /api/onboard/provision/status/{job_id}`` and ``POST /provision/confirm``.
The provision result (``_dp_result``, the one payload both routes and the job
store share) now runs the URL through ``connection_security.mask_stored_url``;
the ``mcp_url`` key and the URL's scheme, host and path stay.

These tests drive the real router, the real provision coordinator, its agent
step and its result builder; the graph-writing phases are no-ops. Synthetic
identifiers only. Offline: no sockets, no AWS.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("GRAPH_IDENTIFIER", "g-unittest")

import agents.onboard as onboard  # noqa: E402
from agents.job_store import JobStore  # noqa: E402

SID = "relay-west"
# nosec B105: offline test fixture, not a credential.
SECRET_URL = "https://relay-user:relay-pw-321@relay.example.internal:8443/mcp?token=relay-tok-654&site=west"
_SECRETS = ("relay-user", "relay-pw-321", "relay-tok-654")
_KEPT = ("https://", "relay.example.internal:8443", "/mcp", "site=west")

#: Provision phases that write the graph or read a source; the test keeps the
#: coordinator, the agent step (``_dp_start_pg_agent``) and the result builder.
_NO_OP_PHASES = (
    "_dp_collect_tables", "_dp_discover_athena_physical", "_dp_schema_entries",
    "_dp_field_mappings", "_dp_athena_physical_fields", "_dp_discovery_completeness",
    "_dp_reapply_user_corrections", "_dp_identity_key_edges", "_dp_mint_unmapped",
    "_dp_internal_joinpaths", "_dp_mirrored_measure_tags", "_dp_settle_field_concepts",
    "_dp_valueoverlap_joinpaths",
    "_dp_samerow_joinpaths", "_dp_identity_rules", "_dp_rule_joinpaths",
    "_dp_cross_system_vms", "_dp_equipment_vms_legacy", "_dp_authority_rules",
    "_dp_onboarding_log", "_dp_finalize", "_log_provision_summary",
    "_persist_schema_relation_kinds", "_post_provision_schema_shape",
)


class _AgentManager:
    def __init__(self, url: str):
        self._agents: dict = {}
        self._url = url

    def get_mcp_url(self, _sid):
        return self._url


@pytest.fixture
def provision_world(monkeypatch):
    store = JobStore("provision", table=None)
    monkeypatch.setattr(onboard, "_provision_jobs", store)
    monkeypatch.setattr(onboard, "_trusted_provision_body", lambda body: body)
    monkeypatch.setattr(onboard, "_reset_conflict_or_none", lambda _sid: None)
    # The coordinator itself; the knowledge post-passes are another concern.
    monkeypatch.setattr(onboard, "_do_provision", onboard._do_provision_base)
    monkeypatch.setattr(onboard, "_dp_connection_phase", lambda ctx: None)
    for name in _NO_OP_PHASES:
        monkeypatch.setattr(onboard, name, lambda *a, **k: None)
    monkeypatch.setattr(onboard, "_dp_written_artifacts", lambda _sid: ([], []))
    monkeypatch.setattr(onboard, "_dp_reapply_self_learned", lambda _sid: 0)
    monkeypatch.setattr(onboard, "_dp_mapped_field_count", lambda _sid: None)
    monkeypatch.setattr(onboard, "_post_provision_mining", lambda _sid: {})
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: [])
    monkeypatch.setattr(onboard, "neptune_query", lambda *a, **k: [])
    monkeypatch.setattr(onboard, "neptune_write", lambda *a, **k: True)
    import agents.coverage_gap_analyzer as gap
    import agents.schema_drift as drift
    import agents.dashboard as dashboard
    monkeypatch.setattr(gap, "run_coverage_gap_check", lambda *_a, **_k: None)
    monkeypatch.setattr(drift, "sweep_resolved_drift", lambda *_a, **_k: None)
    monkeypatch.setattr(dashboard, "invalidate_dashboard_caches", lambda: None)
    app = FastAPI()
    app.include_router(onboard.router)
    app.dependency_overrides[onboard.require_onboard_access] = lambda: None
    return TestClient(app), store, monkeypatch


def _body(connection_type: str = "MCP Server") -> dict:
    return {"system_id": SID, "system_name": "Relay West", "plant": "Site West",
            "schema": "", "mappings": [{"table": "get_tags", "field": "get_tags.a",
                                        "concept": "unknown"}],
            "connection_type": connection_type, "connection_string": SECRET_URL}


def _assert_masked(payload) -> None:
    text = json.dumps(payload)
    leaked = [s for s in _SECRETS if s in text]
    assert not leaked, f"provision served a credential: {leaked}"


def _assert_url_kept(url: str) -> None:
    assert "://***@" in url, url
    for part in _KEPT:
        assert part in url, (part, url)


def _poll(client: TestClient, job_id: str) -> dict:
    end = time.monotonic() + 30
    while time.monotonic() < end:
        res = client.get(f"/api/onboard/provision/status/{job_id}").json()
        if res.get("status") != "running":
            return res
        time.sleep(0.02)
    raise AssertionError("provision job did not finish")


@pytest.mark.parametrize("manager_url", ["", SECRET_URL],
                         ids=["request-url-fallback", "agent-manager-url"])
def test_provision_status_serves_the_mcp_url_masked(provision_world, manager_url):
    client, store, mp = provision_world
    mp.setattr(onboard, "_agent_manager", _AgentManager(manager_url))
    started = client.post("/api/onboard/provision", json=_body())
    assert started.status_code == 200, started.text
    job_id = started.json()["job_id"]
    done = _poll(client, job_id)
    assert done["status"] == "done", done
    assert "mcp_url" in done
    _assert_masked(done)
    _assert_url_kept(done["mcp_url"])
    # The job record holds the masked value too, not only the response.
    _assert_masked(store.get(job_id))


def test_provision_confirm_serves_the_mcp_url_masked(provision_world):
    client, _store, mp = provision_world
    mp.setattr(onboard, "_agent_manager", _AgentManager(""))
    resp = client.post("/api/onboard/provision/confirm", json={"provision_body": _body("REST API")})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The confirm spreads the result after its own "done", so the result's
    # "provisioned" is what it answers (no client calls this route today).
    assert "error" not in body and body["system_id"] == SID, body
    _assert_masked(body)
    _assert_url_kept(body["mcp_url"])


def test_a_url_without_credentials_is_served_unchanged(provision_world):
    client, _store, mp = provision_world
    plain = "https://relay.example.internal:8443/mcp?site=west"
    mp.setattr(onboard, "_agent_manager", _AgentManager(plain))
    resp = client.post("/api/onboard/provision/confirm", json={"provision_body": _body()})
    assert resp.json()["mcp_url"] == plain
