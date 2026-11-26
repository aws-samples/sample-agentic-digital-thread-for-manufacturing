# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0069 follow-up 3 — discover dispatch PARITY between the two dispatchers.

The onboarding CHAT (the ``adt_onboarding`` AgentCore runtime) runs
``ONBOARD_TOOLS.discover`` through ``onboard_agent._discover_backend_result``;
the WIZARD runs the same discovery through ``onboard.py::_onboard_discover_base``.
The two had diverged: the wizard implemented 'Athena' and 'SiteWise (Direct)'
while the runtime dispatcher returned ``{'error': 'Unsupported connection type:
Athena'}`` — the SAME tool had two behaviors depending on which process ran it
(verified live: "discover does not yet support the Athena connection type" for
an onboarded Athena system). The ONE implementation per connection type now
lives in ``onboard_agent`` (``_discover_athena_schema`` /
``_discover_sitewise_direct_model``) and the wizard branches are THIN wrappers
adding only body-derived config + app-only catalog persistence.

Pins here:
  (a) the dispatch SETS are equal (exact pin incl. 'Athena' + 'SiteWise (Direct)');
  (b) the Athena core returns the pinned shape through BOTH dispatchers;
  (c) an AthenaConfigError becomes ``{"error": ...}`` — never a raise;
  (d) the SiteWise (Direct) core returns the pinned shape.

Fully synthetic — no AWS, no network, placeholder ids only.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105 - offline test fixture, not a real secret

import agents.athena_tools as athena_tools  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as onboard_agent  # noqa: E402
import agents.resolve_evidence as resolve_evidence  # noqa: E402
import agents.sitewise_read as sitewise_read  # noqa: E402
from agents.models import DiscoverRequest  # noqa: E402
from agents.onboarding_session import SessionNotFoundError  # noqa: E402

# The EXACT set both dispatchers must support (the parity pin). 'Athena' and
# 'SiteWise (Direct)' are the two types the live defect was about.
KNOWN_TYPES = {
    "PostgreSQL",
    "SiteWise (Direct)",
    "SiteWise (i3X)",
    "REST API",
    "MCP Server",
    "Athena",
    "Tulip (Tables API)",  # Tulip: first-class REST-family vendor
}

_AGENT_BACKENDS = (
    "_discover_postgres",
    "_discover_i3x_data_model",
    "_discover_openapi",
    "_discover_mcp",
    "_discover_athena_schema",
    "_discover_sitewise_direct_model",
    # Tulip routes through resolve_vendor().discover, whose delegate is
    # this backend — stub it so the chat dispatcher resolves it as supported.
    "_discover_tulip",
)
_AGENT_ENTRY_BUILDERS = ("_discover_i3x_entry", "_discover_rest_entry",
                         "_discover_mcp_entry")

_WIZARD_BRANCHES = (
    "_discover_sitewise_direct",
    "_discover_athena",
    "_discover_rest_or_i3x",
    "_discover_mcp_server",
    "_discover_postgres_schema",
)


# --------------------------------------------------------------------------- #
# (a) dispatch-set parity
# --------------------------------------------------------------------------- #

def _agent_dispatch_set(monkeypatch) -> set[str]:
    """Types the runtime dispatcher supports, derived by DRIVING it with stubs:
    supported == does not return the 'Unsupported connection type' error."""
    for name in _AGENT_BACKENDS:
        monkeypatch.setattr(onboard_agent, name, lambda *a, **k: {"tables": []})
    for name in _AGENT_ENTRY_BUILDERS:
        monkeypatch.setattr(onboard_agent, name, lambda *a, **k: {})
    supported = set()
    for ct in sorted(KNOWN_TYPES | {"Bogus Type"}):
        res = onboard_agent._discover_backend_result("system-a", {}, ct, {})
        if not str(res.get("error", "")).startswith("Unsupported connection type"):
            supported.add(ct)
    return supported


def _wizard_dispatch_map(monkeypatch) -> dict[str, str]:
    """connection_type -> wizard branch function name, derived by DRIVING
    ``_onboard_discover_base`` with async stubs."""
    calls: dict[str, str] = {}

    def _mk(name):
        async def _stub(body, system_id, conn_str, schema, connection_type):
            calls[connection_type] = name
            return {"system_id": system_id, "tables": []}
        return _stub

    for name in _WIZARD_BRANCHES:
        monkeypatch.setattr(onboard, name, _mk(name))
    for ct in sorted(KNOWN_TYPES | {"Bogus Type"}):
        asyncio.run(onboard._onboard_discover_base(
            DiscoverRequest(system_id="", connection_type=ct)))
    return calls


def test_agent_dispatch_set_is_pinned(monkeypatch):
    assert _agent_dispatch_set(monkeypatch) == KNOWN_TYPES


def test_wizard_and_agent_dispatch_sets_are_equal(monkeypatch):
    branch_by_type = _wizard_dispatch_map(monkeypatch)
    # The wizard routes every known type; PostgreSQL is its default branch and
    # unknown types fall through to it (pre-existing wizard behavior).
    specialized = {ct for ct, br in branch_by_type.items()
                   if br != "_discover_postgres_schema"}
    assert specialized == KNOWN_TYPES - {"PostgreSQL"}
    assert branch_by_type["PostgreSQL"] == "_discover_postgres_schema"
    # THE parity statement: the runtime dispatcher supports exactly the types
    # the wizard dispatches (PostgreSQL via the wizard's default branch).
    agent_set = _agent_dispatch_set(monkeypatch)
    assert agent_set == set(branch_by_type) - {"Bogus Type"} == KNOWN_TYPES
    assert agent_set - {"PostgreSQL"} == specialized
    # The two previously-diverged types route to the thin wrappers.
    assert branch_by_type["Athena"] == "_discover_athena"
    assert branch_by_type["SiteWise (Direct)"] == "_discover_sitewise_direct"


# --------------------------------------------------------------------------- #
# (b) Athena — pinned shape through BOTH dispatchers
# --------------------------------------------------------------------------- #

_ATHENA_CFG = {"workgroup": "wg-a", "database": "db-a",
               "catalog": "AwsDataCatalog", "region": "us-east-1"}


def _athena_entry() -> dict:
    return {"id": "lake-athena", "connection_type": "Athena", "schema": "",
            "connection_config": dict(_ATHENA_CFG)}


def _fake_run_athena_query(sql, connection_config, **kwargs):
    if "information_schema.tables" in sql:
        return {"columns": ["table_name"], "rows": [["widget_orders"]]}
    if "information_schema.columns" in sql:
        return {"columns": ["column_name", "data_type"],
                "rows": [["order_id", "bigint"], ["qty", "double"]]}
    # The bounded LIMIT-5 sample query: dedup, drop None/"", cap 3 distinct.
    return {"columns": ["order_id", "qty"],
            "rows": [[1, 2.5], [1, 3.5], [2, None], [3, ""]]}


_EXPECTED_ATHENA = {
    "system_id": "lake-athena",
    "schema_name": "db-a",
    "tables": [{
        "name": "widget_orders",
        "columns": [{"name": "order_id", "data_type": "bigint"},
                    {"name": "qty", "data_type": "double"}],
        "row_count": 0,
        "sample_values": {"order_id": [1, 2, 3], "qty": [2.5, 3.5]},
    }],
    "connection_type": "Athena",
}


def _patch_athena(monkeypatch):
    monkeypatch.setattr(athena_tools, "run_athena_query", _fake_run_athena_query)
    # Identity: the cross-account merge seam is covered by its own tests.
    monkeypatch.setattr(resolve_evidence, "_with_cross_account_credentials",
                        lambda _sid, cfg: dict(cfg))


def test_athena_core_returns_pinned_shape(monkeypatch):
    _patch_athena(monkeypatch)
    res = onboard_agent._discover_athena_schema("lake-athena", _athena_entry(), {}, None)
    assert set(res) == {"system_id", "schema_name", "tables",
                        "ai_analysis", "connection_type"}
    ai = res.pop("ai_analysis")
    assert res == _EXPECTED_ATHENA
    assert "wg-a" in ai and "db-a" in ai  # deterministic — no LLM in discovery


class _FakeStore:
    def get_session(self, system_id):
        raise SessionNotFoundError(system_id)

    def create_session(self, system_id, **kwargs):
        return None

    def advance_status(self, system_id, from_status, to_status, **kwargs):
        return None


class _FakeAgentCatalog:
    def __init__(self, entry):
        self._entry = entry

    def lookup(self, system_id):
        return self._entry if system_id == self._entry["id"] else None


def test_discover_tool_supports_athena(monkeypatch):
    """The live defect: the CHAT runtime's ``discover`` tool returned
    'Unsupported connection type: Athena' for an onboarded Athena system."""
    _patch_athena(monkeypatch)
    monkeypatch.setattr(onboard_agent, "catalog", _FakeAgentCatalog(_athena_entry()))
    monkeypatch.setattr(onboard_agent, "_store", lambda: _FakeStore())
    monkeypatch.setattr(cr, "resolve_connection", lambda _sid: {})
    res = onboard_agent.discover("lake-athena")
    assert "error" not in res, res
    assert res["connection_type"] == "Athena"
    assert res["tables"] == [{"name": "widget_orders", "row_count": 0,
                              "column_count": 2}]
    assert res["total_tables"] == 1


def test_wizard_wrapper_equals_core_for_athena(monkeypatch):
    """The same fixture through the wizard wrapper yields an EQUAL dict — the
    wrapper adds nothing but body-derived config + app-only persistence."""
    _patch_athena(monkeypatch)
    entry = _athena_entry()
    core = onboard_agent._discover_athena_schema("lake-athena", entry, {}, None)

    class _Cat:
        def get(self, system_id):
            return entry if system_id == "lake-athena" else None

        def set_connection_config(self, system_id, connection_config):
            return True

    monkeypatch.setattr(onboard, "catalog", _Cat())
    body = DiscoverRequest(system_id="lake-athena", connection_type="Athena")
    wiz = asyncio.run(onboard._discover_athena(body, "lake-athena", "", "", "Athena"))
    assert wiz == core


# --------------------------------------------------------------------------- #
# (c) AthenaConfigError -> {"error": ...}, never a raise
# --------------------------------------------------------------------------- #

def test_athena_config_error_returns_error_dict(monkeypatch):
    monkeypatch.setattr(resolve_evidence, "_with_cross_account_credentials",
                        lambda _sid, cfg: dict(cfg))
    res = onboard_agent._discover_athena_schema(
        "lake-athena", {"id": "lake-athena"}, {}, None)
    assert set(res) == {"error"}
    assert "workgroup" in res["error"]


# --------------------------------------------------------------------------- #
# (d) SiteWise (Direct) — pinned shape through BOTH dispatchers
# --------------------------------------------------------------------------- #

_SW_MODELS = [
    {"id": "mdl-a", "name": "widget_press", "description": "", "status": "ACTIVE",
     "properties": [
         {"id": "p-1", "name": "rate", "dataType": "DOUBLE", "unit": "ea"},
         {"id": "p-2", "name": "state", "dataType": "STRING", "unit": ""},
     ]},
]


def _patch_sitewise(monkeypatch):
    monkeypatch.setattr(sitewise_read, "list_asset_models_with_properties",
                        lambda cfg, region, max_models=None: [dict(m) for m in _SW_MODELS])
    monkeypatch.setattr(sitewise_read, "list_assets",
                        lambda cfg, region, asset_model_id="": [])
    monkeypatch.setattr(resolve_evidence, "_with_cross_account_credentials",
                        lambda _sid, cfg: dict(cfg))


def test_sitewise_direct_core_returns_pinned_shape(monkeypatch):
    _patch_sitewise(monkeypatch)
    res = onboard_agent._discover_sitewise_direct_model(
        "system-a", {}, {}, {"region": "us-west-2"})
    assert res["system_id"] == "system-a"
    assert res["schema_name"] == "sitewise-direct"
    assert res["connection_type"] == "SiteWise (Direct)"
    by_name = {t["name"]: t for t in res["tables"]}
    assert set(by_name) == {"widget_press", "asset_hierarchy"}
    cols = {c["name"]: c["data_type"] for c in by_name["widget_press"]["columns"]}
    assert cols["rate"] == "DOUBLE (ea) — sitewise property"
    assert cols["state"] == "STRING — sitewise property"
    assert by_name["widget_press"]["row_count"] == 0
    assert by_name["widget_press"]["sample_values"] == {}  # no live assets stubbed
    assert by_name["asset_hierarchy"]["sample_values"]["asset_model"] == ["widget_press"]
    assert "1 asset model" in res["ai_analysis"]


def test_backend_dispatcher_routes_sitewise_direct(monkeypatch):
    _patch_sitewise(monkeypatch)
    entry = {"id": "system-a", "connection_type": "SiteWise (Direct)",
             "connection_config": {"region": "us-west-2"}}
    res = onboard_agent._discover_backend_result(
        "system-a", entry, "SiteWise (Direct)", {})
    assert res["connection_type"] == "SiteWise (Direct)"
    assert {t["name"] for t in res["tables"]} == {"widget_press", "asset_hierarchy"}


def test_wizard_wrapper_equals_core_for_sitewise(monkeypatch):
    _patch_sitewise(monkeypatch)
    core = onboard_agent._discover_sitewise_direct_model(
        "system-a", {}, {}, {"region": "us-west-2"})

    class _Cat:
        def get(self, system_id):
            return None

        def set_connection_config(self, system_id, connection_config):
            return True

    monkeypatch.setattr(onboard, "catalog", _Cat())
    body = DiscoverRequest(system_id="system-a", connection_type="SiteWise (Direct)",
                           connection_config={"region": "us-west-2"})
    wiz = asyncio.run(onboard._discover_sitewise_direct(
        body, "system-a", "", "", "SiteWise (Direct)"))
    assert wiz == core


# ---------------------------------------------------------------------------
# Discover routing: an UNDECLARED body type must not shadow the catalog identity
# (test event 12, 2026-09-14: the seed's system_id-only discover of the
# SiteWise (Direct) system fell into the PostgreSQL branch because the model's
# default "PostgreSQL" always won the ``body or entry`` fallback).
# ---------------------------------------------------------------------------
def _route_probe(monkeypatch, entry):
    seen = {}

    async def fake_branch(name):
        async def _b(body, system_id, conn_str, schema, connection_type):
            seen["branch"] = name
            seen["type"] = connection_type
            return {"tables": [], "branch": name}
        return _b

    for fn, name in (("_discover_sitewise_direct", "sitewise"),
                     ("_discover_postgres_schema", "postgres"),
                     ("_discover_athena", "athena"),
                     ("_discover_rest_or_i3x", "rest"),
                     ("_discover_mcp_server", "mcp")):
        monkeypatch.setattr(onboard, fn, asyncio.run(fake_branch(name)))

    class _Cat:
        def get(self, system_id):
            return dict(entry) if entry and entry["id"] == system_id else None

    monkeypatch.setattr(onboard, "catalog", _Cat())
    monkeypatch.setattr(cr, "resolve_connection", lambda sid: {"connection_string": ""})
    return seen


def test_undeclared_type_falls_back_to_catalog_identity(monkeypatch):
    seen = _route_probe(monkeypatch, {"id": "sitewise-direct", "connection_type": "SiteWise (Direct)", "schema": ""})
    body = DiscoverRequest(system_id="sitewise-direct", connection_string="iotsitewise://iotsitewise.us-east-1.amazonaws.com")
    assert "connection_type" not in body.model_fields_set
    asyncio.run(onboard._onboard_discover_base(body))
    assert seen == {"branch": "sitewise", "type": "SiteWise (Direct)"}


def test_declared_type_still_wins_over_catalog(monkeypatch):
    seen = _route_probe(monkeypatch, {"id": "sys-a", "connection_type": "PostgreSQL", "schema": "s"})
    body = DiscoverRequest(system_id="sys-a", connection_type="SiteWise (Direct)")
    asyncio.run(onboard._onboard_discover_base(body))
    assert seen["branch"] == "sitewise"


def test_no_system_id_and_no_type_keeps_the_postgres_default(monkeypatch):
    seen = _route_probe(monkeypatch, None)
    body = DiscoverRequest(connection_string="postgresql://u:p@h/db", schema="s")
    asyncio.run(onboard._onboard_discover_base(body))
    assert seen == {"branch": "postgres", "type": "PostgreSQL"}


# ---------------------------------------------------------------------------
# /api/thread/learn stamps steward provenance on a taught IdentityRule so the
# route seed ranks it tier 0 (R3) instead of trimming it as an anonymous row.
# ---------------------------------------------------------------------------
def test_thread_learn_identity_rule_is_stamped_user_confirmed(monkeypatch):
    import agents.resolve as resolve
    from agents.models import LearnRequest
    writes = []
    monkeypatch.setattr(resolve, "neptune_query", lambda q, p=None: [{"s.id": "sys-b"}])
    monkeypatch.setattr(resolve, "get_schema", lambda sid: "")
    monkeypatch.setattr(resolve, "neptune_write", lambda q, p=None: writes.append((q, p)) or True)
    # The Thread panel's request: the proposal with the identifier it was
    # learned from and the value it matched (a request without them is
    # user_unverified, test_thread_learn_unverified.py).
    body = LearnRequest(concept="batch", source_system="mes-ind", source_field="batch_number",
                        source_pattern="B-{id}", target_system="sys-b",
                        target_field="lot_ref", target_pattern="LOT-2026-0{id}",
                        confidence=0.95, example_identifier="B-1234",
                        example_matched_value="LOT-2026-01234")
    out = resolve.thread_learn(body)
    assert out["status"] == "created"
    create_q, params = writes[0]
    assert "method: $method" in create_q and params["method"] == "user_confirmed"
    assert "provenance: 'human-asserted'" in create_q
    assert params["created_at"]
    assert params["src_pat"] == "B-{id}" and params["tgt_pat"] == "LOT-2026-0{id}"
