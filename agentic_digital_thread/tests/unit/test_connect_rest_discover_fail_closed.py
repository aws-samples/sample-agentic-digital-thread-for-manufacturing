# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generic REST API / i3X discover fails closed on an unreadable source.

``onboard._rest_family_discover`` read the System node with
``resolve_connection``, which is fail-soft: a stored secret it could not
fetch came back as ``_secret_unresolved`` with no token, and a graph read
failure came back as ``_unconfigured`` (indistinguishable from "no node").
Either way the one-stored-URL rule saw no token, so the request URL ran
tokenless: a caller-chosen URL was fetched for a system whose stored
connection could not be checked. Now an unreadable stored secret is refused
before any URL is chosen, and an unconfigured answer for a known system is
re-checked with the CHECKED node read, so a graph outage is refused too.
No HTTP request is made in either case. A system with no node yet (a fresh
connect) and a readable node are unchanged. Offline.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.db as db  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.rest_vendor_registry as registry  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import cat, no_dns  # noqa: E402,F401
from tests.unit.test_connect_rest_discover_one_stored_url import (  # noqa: E402,F401
    BASE, LOOKALIKE, SID, SPEC, _legacy_node, http_get)

TYPES = ["REST API", "SiteWise (i3X)"]


@pytest.fixture
def vendor_calls(monkeypatch):
    """Every vendor discover hook  records instead of calling out."""
    calls: list = []
    real = registry.resolve_vendor

    def _resolve(ctype):
        adapter = real(ctype)
        if adapter is None:
            return None

        class _Spy:
            def discover(self, *a, **k):
                calls.append(a)
                return {"tables": [], "spy": True}
        return _Spy()
    monkeypatch.setattr(registry, "resolve_vendor", _resolve)
    return calls


def _discover(url: str, ctype: str):
    return onboard._rest_family_discover(SID, url, "", ctype, {})


@pytest.mark.parametrize("ctype", TYPES)
def test_an_unreadable_stored_secret_is_refused_before_any_request(
        cat, monkeypatch, http_get, vendor_calls, ctype):
    _legacy_node(cat, monkeypatch, {"base_url": BASE}, ctype)
    monkeypatch.setattr(cr, "_fetch_secret",
                        lambda arn: (_ for _ in ()).throw(RuntimeError("Throttling")))
    result, _ = _discover(LOOKALIKE, ctype)
    assert "error" in result and "could not be read" in result["error"], result
    assert http_get == [] and vendor_calls == [], (http_get, vendor_calls)


@pytest.mark.parametrize("ctype", TYPES)
def test_a_graph_outage_is_refused_before_any_request(
        cat, monkeypatch, http_get, vendor_calls, ctype):
    cat.add({"id": SID, "name": "Legacy REST", "connection_type": ctype, "schema": "",
             "status": "onboarded"})
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: None)   # unchecked read degrades

    def _down(cypher, parameters=None):
        raise db.GraphQueryError("ThrottlingException", "graph unavailable")
    monkeypatch.setattr(db, "neptune_query_checked", _down)
    result, _ = _discover(SPEC, ctype)
    assert "error" in result and "could not be read" in result["error"], result
    assert "retry" in result["error"].lower()
    assert http_get == [] and vendor_calls == [], (http_get, vendor_calls)


def test_guard_a_fresh_connect_with_no_node_still_discovers(cat, monkeypatch, http_get):
    cat.add({"id": SID, "name": "New REST", "connection_type": "REST API", "schema": "",
             "status": "registered"})
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: None)   # checked read: [] too
    result, _ = _discover(SPEC, "REST API")
    assert "error" not in result, result
    assert [u for u, _auth in http_get] == [SPEC]


def test_guard_a_readable_node_still_discovers_with_its_token(cat, monkeypatch, http_get):
    _legacy_node(cat, monkeypatch, {"base_url": BASE}, "REST API")
    result, _ = _discover(SPEC, "REST API")
    assert "error" not in result, result
    assert http_get and http_get[0][0] == SPEC
