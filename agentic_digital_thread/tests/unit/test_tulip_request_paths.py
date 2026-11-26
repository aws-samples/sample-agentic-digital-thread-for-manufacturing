# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every Tulip request stays on the three read-only Tables API paths.

A table id comes from the vendor (discovery, ``tulip_list_tables``) or from the
model (the runtime tools), so it is untrusted: an id such as ``../../admin``
would otherwise steer the stored Basic credential to another GET path on the
same host. The connector refuses any request outside
``/api/v3[/w/<workspace>]/tables[/<id>[/records]]`` before it is sent, and
discovery reads are size-capped and bounded in count.

Fully synthetic: the HTTP layer is stubbed, no network.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard_agent as oa  # noqa: E402
import agents.pinned_http as pinned_http  # noqa: E402
import agents.system_agent as sa  # noqa: E402

_BAD_IDS = [
    "../../admin",          # traversal
    "t1/../../w/other",     # traversal behind a valid prefix
    "t1/records",           # extra path segment
    "t1?limit=100000",      # query smuggling
    "t1#frag",              # fragment
    "%2e%2e",               # encoded traversal
    "t" * 65,               # overlength
    "",                     # empty
]


@pytest.fixture
def _no_network(monkeypatch):
    """Fail the test if any request reaches the HTTP layer."""
    calls = []

    def _client(*a, **k):
        calls.append((a, k))
        raise AssertionError("a request reached the network layer")

    monkeypatch.setattr(pinned_http, "client", _client)
    monkeypatch.setattr(pinned_http, "get", _client)
    return calls


@pytest.mark.parametrize("bad", _BAD_IDS)
def test_client_refuses_paths_outside_the_tables_api(_no_network, bad):
    client = sa._TulipClient("https://tulip.example", token="T", auth_scheme="basic")
    with pytest.raises(ValueError):
        client.get(f"/api/v3/tables/{bad}/records")
    assert _no_network == []


@pytest.mark.parametrize("path", ["/api/v3/admin", "/api/v2/tables", "/api/v3/tables/t1/records/x",
                                  "/api/v3/w/ws1/../tables"])
def test_client_refuses_other_api_paths(_no_network, path):
    client = sa._TulipClient("https://tulip.example", token="T", auth_scheme="basic")
    with pytest.raises(ValueError):
        client.get(path)
    assert _no_network == []


@pytest.mark.parametrize("bad", _BAD_IDS)
def test_runtime_tools_refuse_bad_ids_without_a_request(_no_network, bad):
    client = sa._TulipClient("https://tulip.example", token="T", auth_scheme="basic")
    cols = json.loads(sa._tulip_columns_impl(client, bad))
    recs = json.loads(sa._tulip_records_impl(client, bad, 10, 0, "", ""))
    assert "error" in cols and "error" in recs
    assert _no_network == []


def test_list_tables_drops_ids_that_cannot_be_queried(monkeypatch):
    monkeypatch.setattr(sa._TulipClient, "get", lambda self, path, params=None: [
        {"id": "aBc123", "label": "Orders"},
        {"id": "../../admin", "label": "Evil"},
        {"id": "t" * 65, "label": "Long"},
    ])
    client = sa._TulipClient("https://tulip.example", token="T", auth_scheme="basic")
    out = json.loads(sa._tulip_list_tables_impl(client))
    assert [t["id"] for t in out["tables"]] == ["aBc123"]


def test_valid_ids_still_reach_the_three_paths(monkeypatch):
    seen = []

    class _Stream:
        status_code = 200
        def raise_for_status(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, method, url, headers=None, params=None):
            seen.append(url)
            return _Stream()

    monkeypatch.setattr(pinned_http, "client", lambda **k: _Client())
    import agents.response_integrity_query as riq
    monkeypatch.setattr(riq, "read_http_body_capped", lambda resp, limit: (b"[]", False))
    client = sa._TulipClient("https://tulip.example", token="T", auth_scheme="basic",
                             workspace_id="ws1")
    client.get("/api/v3/tables")
    client.get("/api/v3/tables/aBc123")
    client.get("/api/v3/tables/aBc123/records", {"limit": 1})
    assert seen == ["https://tulip.example/api/v3/w/ws1/tables",
                    "https://tulip.example/api/v3/w/ws1/tables/aBc123",
                    "https://tulip.example/api/v3/w/ws1/tables/aBc123/records"]


# --------------------------------------------------------------------------- #
# Discovery: same path rules, capped bodies, bounded fan-out
# --------------------------------------------------------------------------- #

def test_discovery_skips_tables_whose_id_cannot_be_queried(monkeypatch):
    seen = []

    def _get(url, headers, params=None):
        seen.append(url)
        if url.endswith("/api/v3/tables"):
            return [{"id": "aBc123", "label": "Orders"}, {"id": "../../admin", "label": "Evil"}]
        return {"columns": [{"name": "run_number", "dataType": {"type": "string"}}]}

    monkeypatch.setattr(oa, "_tulip_discover_get", _get)
    tables = oa._tulip_tables_from_list(
        _get("https://h/api/v3/tables", {}), "https://h", {}, "")
    assert [t["table_id"] for t in tables] == ["aBc123"]
    assert not any("admin" in u for u in seen)


def test_discovery_bounds_the_number_of_tables_and_definition_reads(monkeypatch):
    calls = {"defs": 0}

    def _get(url, headers, params=None):
        calls["defs"] += 1
        return {"columns": []}

    monkeypatch.setattr(oa, "_tulip_discover_get", _get)
    listing = [{"id": f"t{i:03d}", "label": f"Table {i}"} for i in range(250)]
    tables = oa._tulip_tables_from_list(listing, "https://h", {}, "")
    assert len(tables) == oa._TULIP_DISCOVER_MAX_TABLES
    assert calls["defs"] == oa._TULIP_DISCOVER_MAX_TABLES


def test_discovery_reads_are_size_capped(monkeypatch):
    class _Stream:
        status_code = 200
        def raise_for_status(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, method, url, headers=None, params=None):
            return _Stream()

    monkeypatch.setattr(pinned_http, "client", lambda **k: _Client())
    import agents.response_integrity_query as riq
    monkeypatch.setattr(riq, "read_http_body_capped", lambda resp, limit: (b"[", True))
    with pytest.raises(ValueError):
        oa._tulip_discover_get("https://h/api/v3/tables", {"Authorization": "Basic T"})
