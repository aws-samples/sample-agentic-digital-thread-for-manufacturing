# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Query-side Tulip tools — the Tulip Tables API short-circuit at query time.

A ``Tulip (Tables API)`` system is a generic Tables API, not an i3X asset
server, so the system agent must build the Tulip tools (list/columns/records)
instead of the i3X toolset — the query-side twin of
``onboard_agent._discover_tulip``. (routing is by the declared
connection type, NOT the host.) Fully synthetic: ``_TulipClient.get`` is
stubbed, no network. Also pins the ``_direct_system_tools`` routing (Tulip vs
i3X by connection type) and the self-recovery introspection hint.
"""
import json
import os
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105 - offline test fixture

import agents.system_agent as sa  # noqa: E402


class _FakeTulip:
    """Stub for _TulipClient.get — records (path, params); returns canned data."""

    def __init__(self, routes, error=None):
        self.routes = routes  # path-substring -> payload
        self.error = error
        self.calls = []

    def get(self, path, params=None):
        self.calls.append({"path": path, "params": params or {}})
        if self.error is not None:
            raise self.error
        for key, payload in self.routes.items():
            if key in path:
                return payload
        return []


def _tools(monkeypatch, fake):
    monkeypatch.setattr(sa._TulipClient, "get",
                        lambda self, path, params=None: fake.get(path, params))
    tools = sa._make_tulip_tools("https://your-instance.tulip.co",
                                 token="dG9rZW4=", auth_scheme="basic")  # nosec B106 - fake base64 token
    return {t.tool_name: t for t in tools}


_TABLES = [{"id": "t1", "label": "*Stations"}, {"id": "t2", "label": "*Products"}]


# --------------------------------------------------------------------------- #
# tool behaviour
# --------------------------------------------------------------------------- #

def test_list_tables_returns_id_and_label(monkeypatch):
    fake = _FakeTulip({"/api/v3/tables": _TABLES})
    tools = _tools(monkeypatch, fake)
    out = json.loads(tools["tulip_list_tables"]._tool_func())
    assert out["count"] == 2
    assert {t["id"] for t in out["tables"]} == {"t1", "t2"}
    assert fake.calls[0]["path"].endswith("/api/v3/tables")
    assert fake.calls[0]["params"].get("limit") == 100


def test_get_table_columns_handles_nested_and_flat_types(monkeypatch):
    fake = _FakeTulip({"/api/v3/tables/t1": {"columns": [
        {"name": "c_str", "type": {"type": "string"}},
        {"name": "c_num", "dataType": "float"},
    ]}})
    tools = _tools(monkeypatch, fake)
    out = json.loads(tools["tulip_get_table_columns"]._tool_func(table_id="t1"))
    cols = {c["name"]: c["data_type"] for c in out["columns"]}
    assert cols == {"c_str": "string", "c_num": "float"}


def test_query_records_shapes_rows_and_passes_filters(monkeypatch):
    rows = [{"id": "r1", "vebke_name": "Coater-1"},
            {"id": "r2", "vebke_name": "Blender-2"}]
    fake = _FakeTulip({"/api/v3/tables/t1/records": rows})
    tools = _tools(monkeypatch, fake)
    filt = json.dumps([{"field": "col", "functionType": "equal", "arg": "x"}])
    out = json.loads(tools["tulip_query_records"]._tool_func(
        table_id="t1", limit=5, filters=filt))
    assert out["row_count"] == 2
    assert "vebke_name" in out["columns"]
    call = fake.calls[0]
    assert call["path"].endswith("/api/v3/tables/t1/records")
    assert call["params"]["limit"] == 5
    assert call["params"]["filters"] == filt  # JSON-encoded passthrough


def test_query_records_clamps_limit_and_rejects_bad_filters(monkeypatch):
    fake = _FakeTulip({"/records": []})
    tools = _tools(monkeypatch, fake)
    tools["tulip_query_records"]._tool_func(table_id="t1", limit=9999)
    assert fake.calls[0]["params"]["limit"] == 100
    # invalid filters JSON -> fixed-safe error, no call appended past the first
    out = json.loads(tools["tulip_query_records"]._tool_func(
        table_id="t1", filters="{not json"))
    assert out["error_type"] == "tool_error"


def test_tools_are_fixed_safe_on_transport_error(monkeypatch):
    fake = _FakeTulip({}, error=RuntimeError("401 Unauthorized secret-ish"))
    tools = _tools(monkeypatch, fake)
    cases = {
        "tulip_list_tables": {},
        "tulip_get_table_columns": {"table_id": "t1"},
        "tulip_query_records": {"table_id": "t1"},
    }
    for name, kwargs in cases.items():
        out = json.loads(tools[name]._tool_func(**kwargs))
        assert out == {"error": "tool_unavailable", "error_type": "tool_error"}
        assert "401" not in json.dumps(out)  # never leak the raw error


# --------------------------------------------------------------------------- #
# _direct_system_tools routing (Tulip vs i3X by base_url)
# --------------------------------------------------------------------------- #

def _tool_names(result):
    tools, _desc = result
    return {getattr(t, "tool_name", getattr(t, "name", "")) for t in tools}


def test_direct_tools_routes_tulip_type_to_tulip_tools():
    # A Tulip system gets the Tulip tools by its DECLARED connection
    # type ("Tulip (Tables API)"), not a host sniff.
    from agents.connection_types import TULIP
    result = sa._direct_system_tools(
        "tulip", TULIP, "",
        {"base_url": "https://your-instance.tulip.co", "token": "dG9rZW4=",  # nosec B106
         "auth_scheme": "basic"})
    names = _tool_names(result)
    assert any(n.startswith("tulip_") for n in names), names
    assert not any(n.startswith("sitewise_") for n in names), names


def test_direct_tools_tulip_on_internal_host_no_entry():
    # A self-hosted/proxied Tulip on a NON-tulip.co
    # host still gets the Tulip tools at RUNTIME. resolve_vendor keys on the
    # TYPE string, so the runtime path never needs the catalog entry — the
    # provision-succeeds/query-fails split (host sniff without the entry) is
    # gone. Regression guard for that exact bug.
    from agents.connection_types import TULIP
    tools, _ = sa._direct_system_tools(
        "tulip", TULIP, "",
        {"base_url": "https://tulip.internal.example.com", "token": "dG9r",  # nosec B106
         "auth_scheme": "basic"})
    names = {getattr(t, "tool_name", getattr(t, "name", "")) for t in tools}
    assert any(n.startswith("tulip_") for n in names), names


def test_direct_tools_generic_rest_raises_unsupported():
    # A GENERIC REST system (REST API on a non-Tulip host)
    # has no runtime toolset — it must fail HONESTLY with RestVendorUnsupported,
    # never silently fabricate i3X/Tulip tools. This restores the generic-REST
    # agent-level coverage that was lost when the fixture was repointed to Tulip.
    from agents.rest_vendor_registry import RestVendorUnsupported
    with pytest.raises(RestVendorUnsupported):
        sa._direct_system_tools(
            "gen", "REST API", "", {"base_url": "https://api.example.com"})


def test_i3x_type_gets_i3x_tools_even_on_tulip_host():
    # i3X is TYPE-identified, not host-identified. An i3X-typed system gets the
    # i3X toolset regardless of URL (a real Tulip system is typed
    # "Tulip (Tables API)", never "SiteWise (i3X)"). Type-string routing.
    result = sa._direct_system_tools(
        "sw", "SiteWise (i3X)", "",
        {"base_url": "https://your-instance.tulip.co", "token": "t",  # nosec B106
         "auth_scheme": "bearer"})
    names = _tool_names(result)
    assert any(n.startswith("sitewise_") for n in names), names
    assert not any(n.startswith("tulip_") for n in names), names


def test_direct_tools_routes_non_tulip_rest_to_i3x_tools():
    result = sa._direct_system_tools(
        "sw", "SiteWise (i3X)", "",
        {"base_url": "https://i3x.example.com", "token": "t",  # nosec B106
         "auth_scheme": "bearer"})
    names = _tool_names(result)
    assert any(n.startswith("sitewise_") for n in names), names
    assert not any(n.startswith("tulip_") for n in names), names


def test_introspection_hint_names_tulip_for_tulip_type():
    # Tulip is a first-class type, so its self-recovery hint
    # names the tulip_* tools directly. A plain "REST API" system is generic and
    # must NOT be told to call Tulip-specific tools.
    from agents.connection_types import TULIP
    tulip_hint = sa._introspection_hint(TULIP)
    assert "tulip_list_tables" in tulip_hint
    assert "tulip_get_table_columns" in tulip_hint
    assert "tulip" not in sa._introspection_hint("REST API")


def test_discovery_tools_hint_names_tulip_for_tulip_type():
    # The "schema already provided" directive names the right discovery
    # tools per type — tulip_* for a Tulip system, generic for plain REST.
    from agents.connection_types import TULIP
    assert "tulip_list_tables" in sa._discovery_tools_hint(TULIP)
    assert "tulip" not in sa._discovery_tools_hint("REST API")


# --------------------------------------------------------------------------- #
# Optional workspace scope (item 1): _TulipClient._scoped inserts /w/<id> after
# /api/v3; an empty/invalid id leaves the path byte-identical to today.
# --------------------------------------------------------------------------- #

def test_tulip_client_scoped_with_workspace():
    c = sa._TulipClient("https://host", workspace_id="ws9")
    assert c._scoped("/api/v3/tables") == "/api/v3/w/ws9/tables"
    assert c._scoped("/api/v3/tables/t1/records") == "/api/v3/w/ws9/tables/t1/records"


def test_tulip_client_scoped_without_workspace_is_identical():
    c = sa._TulipClient("https://host")
    assert c._scoped("/api/v3/tables") == "/api/v3/tables"
    assert c._scoped("api/v3/tables") == "/api/v3/tables"  # normalises the leading slash


def test_tulip_client_invalid_workspace_is_not_injected():
    # a malformed id never reaches the URL — it falls back to the unscoped path
    c = sa._TulipClient("https://host", workspace_id="bad/seg")
    assert c._scoped("/api/v3/tables") == "/api/v3/tables"


def test_make_tulip_tools_passes_workspace_to_client(monkeypatch):
    captured = {}

    def _capture_get(self, path, params=None):
        captured["workspace_id"] = self.workspace_id
        captured["scoped"] = self._scoped(path)
        return []

    monkeypatch.setattr(sa._TulipClient, "get", _capture_get)
    tools = {t.tool_name: t
             for t in sa._make_tulip_tools("https://host", token="dG9r",  # nosec B106
                                           auth_scheme="basic", workspace_id="wsX")}
    tools["tulip_list_tables"]._tool_func()
    assert captured["workspace_id"] == "wsX"
    assert captured["scoped"] == "/api/v3/w/wsX/tables"


def test_tulip_runtime_tools_reads_workspace_from_connection_config(monkeypatch):
    # The registry hook must thread workspace_id, key_scope, the system id AND
    # the credential-source marker from the non-secret connection_config into
    # _make_tulip_tools (end-to-end at runtime).
    seen = {}
    monkeypatch.setattr(
        sa, "_make_tulip_tools",
        lambda base_url, secret_arn="", token="", auth_scheme="basic",  # nosec B107
        workspace_id="", key_scope="", system_id="", credential_source="": seen.update(
            workspace_id=workspace_id, key_scope=key_scope,
            system_id=system_id, credential_source=credential_source) or [])
    from agents.rest_vendor_registry import _tulip_runtime_tools
    _tulip_runtime_tools("https://host", "", "tok", "basic",
                         connection_config={"base_url": "https://host",
                                            "workspace_id": "wsZ", "key_scope": "instance",
                                            "system_id": "sysZ",
                                            "credential_source": "secret_arn"})
    assert seen["workspace_id"] == "wsZ"
    assert seen["key_scope"] == "instance"
    assert seen["system_id"] == "sysZ"
    assert seen["credential_source"] == "secret_arn"
