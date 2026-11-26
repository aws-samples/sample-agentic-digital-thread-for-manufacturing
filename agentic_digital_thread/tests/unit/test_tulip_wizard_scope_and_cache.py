# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Tulip connector's wizard path: workspace scope, table names and cache.

Workspace scope: the wizard REST discover carries a REGISTERED workspace_id into
the Tulip adapter, so an instance-wide Service Account key is sent to
``/api/v3/w/<ws>/tables`` (not ``/api/v3/tables``). A request body does not
override a stored scope.

Table names: the live name->id map enforces the 100-table bound client-side,
matches on the raw exact name (the same string ``_discover_tulip`` saves), and
fails closed for ambiguous names rather than taking the first id.

Cache: the name->id cache is scoped to the connection's credential identity (a
credential change refetches), does NOT cache request failures as empty maps (a
transient failure is retried), and expires.

Fully synthetic — ``_TulipClient.get``, ``_tulip_discover_get`` and the
onboarding-session store are stubbed; no network, no AWS.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.identity_affinity as ia  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as onboard_agent  # noqa: E402
import agents.resolve_evidence as re_mod  # noqa: E402
import agents.system_agent as sa  # noqa: E402
from agents.connection_types import TULIP  # noqa: E402
from agents.models import DiscoverRequest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    re_mod._reset_sample_memo()
    re_mod._reset_tulip_table_id_memo()
    yield
    re_mod._reset_sample_memo()
    re_mod._reset_tulip_table_id_memo()


def _patch(saved, obj, name, value):
    saved.append((obj, name, getattr(obj, name)))
    setattr(obj, name, value)


def _restore(saved):
    for obj, name, value in reversed(saved):
        setattr(obj, name, value)


# =========================================================================== #
# Workspace scope: the wizard REST discover carries a registered workspace_id
# =========================================================================== #

class _FakeCatalog:
    def __init__(self, entry):
        self._entry = entry

    def get(self, system_id):
        return self._entry if system_id == self._entry.get("id") else None


def _run_tulip_discover(resolved, entry, capture):
    """Drive onboard_discover for a Tulip system; capture every discover GET URL
    (the module-level _tulip_discover_get, which carries the /w/<ws> prefix)."""
    def _fake_discover_get(url, headers, params=None):
        capture.append(url)
        if url.endswith("/tables"):
            return [{"id": "t1", "label": "Orders"}]
        return {"columns": [{"name": "run_number", "dataType": "string"}]}

    saved = []
    try:
        _patch(saved, onboard, "catalog", _FakeCatalog(entry))
        _patch(saved, onboard_agent, "catalog", _FakeCatalog(entry))
        _patch(saved, cr, "resolve_connection", lambda sid: dict(resolved))
        _patch(saved, onboard_agent, "_tulip_discover_get", _fake_discover_get)
        _patch(saved, onboard, "bedrock_call", lambda *a, **k: "ai analysis")
        body = DiscoverRequest(system_id="tulip", connection_type=TULIP,
                               connection_config={"workspace_id": "evil-override"})
        return asyncio.run(onboard.onboard_discover(body))
    finally:
        _restore(saved)


def test_wizard_discover_carries_registered_workspace_id():
    """A registered instance-wide key: the discover LIST goes to
    /api/v3/w/ws1/tables, and a request-body workspace_id does NOT override it."""
    cap = []
    resolved = {"base_url": "https://h", "token": "T", "auth_scheme": "basic",
                "workspace_id": "ws1", "secret_arn": "adt/conn/tulip"}
    entry = {"id": "tulip", "connection_type": TULIP, "schema": "rest",
             "connection_config": {"base_url": "https://h", "workspace_id": "ws1"}}
    result = _run_tulip_discover(resolved, entry, cap)
    assert "error" not in result, result
    list_calls = [u for u in cap if u.endswith("/tables")]
    assert list_calls and all("/api/v3/w/ws1/tables" in u for u in list_calls), cap
    # the request-body "evil-override" never reaches a URL
    assert not any("evil-override" in u for u in cap), cap


def test_wizard_discover_no_registered_scope_is_unscoped():
    """With no stored workspace_id, discover is unscoped (/api/v3/tables), and a
    request-body workspace_id still cannot inject a scope."""
    cap = []
    resolved = {"base_url": "https://h", "token": "T", "auth_scheme": "basic",
                "secret_arn": "adt/conn/tulip"}
    entry = {"id": "tulip", "connection_type": TULIP, "schema": "rest",
             "connection_config": {"base_url": "https://h"}}
    result = _run_tulip_discover(resolved, entry, cap)
    assert "error" not in result, result
    list_calls = [u for u in cap if u.endswith("/tables")]
    assert list_calls and all(u.endswith("/api/v3/tables") for u in list_calls), cap
    assert not any("/w/" in u for u in cap), cap


# =========================================================================== #
# Table names: ambiguous names, raw-name match, the 100-table bound
# =========================================================================== #

def _map(monkeypatch, tables):
    monkeypatch.setattr(sa._TulipClient, "get",
                        lambda self, path, params=None: tables)
    return re_mod.tulip_name_id_map("tulip", {"base_url": "https://h", "secret_arn": "arn"})


def test_duplicate_names_fail_closed(monkeypatch):
    m = _map(monkeypatch, [{"id": "t1", "label": "Orders"},
                           {"id": "t2", "label": "Orders"}])
    assert m.get("Orders", "") == "", m   # ambiguous -> not resolvable, never first-wins


def test_whitespace_distinct_names_are_distinct(monkeypatch):
    # "Orders" and "Orders " are different raw names (matching _discover_tulip,
    # which does not strip), so neither collapses onto the other.
    m = _map(monkeypatch, [{"id": "t1", "label": "Orders"},
                           {"id": "t2", "label": "Orders "}])
    assert m.get("Orders") == "t1" and m.get("Orders ") == "t2", m


def test_case_only_difference_is_distinct(monkeypatch):
    m = _map(monkeypatch, [{"id": "t1", "label": "Orders"},
                           {"id": "t2", "label": "orders"}])
    assert m.get("Orders") == "t1" and m.get("orders") == "t2", m


def test_invalid_id_is_omitted(monkeypatch):
    m = _map(monkeypatch, [{"id": "../etc", "label": "Orders"},
                           {"id": "t2", "label": "Scrap"}])
    assert "Orders" not in m and m.get("Scrap") == "t2", m


def test_server_ignoring_limit_is_bounded_client_side(monkeypatch):
    over = [{"id": f"t{i}", "label": f"Table{i}"} for i in range(150)]
    m = _map(monkeypatch, over)
    assert len(m) == re_mod._TULIP_LIST_MAX_TABLES, len(m)


def test_raw_name_matches_discovered_name(monkeypatch, _reset=None):
    # The sampler resolves the id by the SAME raw name _discover_tulip saves.
    monkeypatch.setattr(re_mod, "_tulip_connection_config",
                        lambda s, c: {"base_url": "https://h", "secret_arn": "arn", **(c or {})})
    from agents.onboarding_session import SessionNotFoundError

    class _NoSession:
        def get_session(self, sid):
            raise SessionNotFoundError(sid)
    monkeypatch.setattr("agents.onboard_agent._store", lambda: _NoSession())

    def _get(self, path, params=None):
        if self._scoped(path).endswith("/tables"):
            return [{"id": "t1", "label": "Work Orders"}]   # a name with a space
        return [{"run_number": "WO-1"}]
    monkeypatch.setattr(sa._TulipClient, "get", _get)
    vals = re_mod._sample_tulip_records(
        "tulip", {"base_url": "https://h", "secret_arn": "arn"},
        limit=5, field="run_number", table="Work Orders")
    assert vals == ["WO-1"], vals


# =========================================================================== #
# Cache: credential scoping, no caching of failures, expiry
# =========================================================================== #

def _list_stub(monkeypatch, counter, tables=None, fail=False):
    tables = tables if tables is not None else [{"id": "Orders-id", "label": "Orders"}]

    def _get(self, path, params=None):
        counter["n"] += 1
        if fail:
            raise RuntimeError("tulip down")
        return tables
    monkeypatch.setattr(sa._TulipClient, "get", _get)


def test_credential_change_refetches(monkeypatch):
    counter = {"n": 0}
    monkeypatch.setattr(sa._TulipClient, "get",
                        lambda self, path, params=None: (
                            counter.__setitem__("n", counter["n"] + 1)
                            or [{"id": "new-id", "label": "Orders"}]))
    cfg_a = {"base_url": "https://h", "secret_arn": "arn", "token": "TOK-A"}
    cfg_b = {"base_url": "https://h", "secret_arn": "arn", "token": "TOK-B"}
    re_mod._tulip_name_id_map_get("tulip", cfg_a)
    re_mod._tulip_name_id_map_get("tulip", cfg_b)   # same system/url/ws, new token
    assert counter["n"] == 2, "a credential change must refetch, not reuse the cache"


def test_same_credential_is_cached(monkeypatch):
    counter = {"n": 0}
    _list_stub(monkeypatch, counter)
    cfg = {"base_url": "https://h", "secret_arn": "arn", "token": "TOK"}
    re_mod._tulip_name_id_map_get("tulip", cfg)
    re_mod._tulip_name_id_map_get("tulip", cfg)
    assert counter["n"] == 1, "same credential/url/ws must be served from the cache"


def test_transient_failure_is_not_cached(monkeypatch):
    counter = {"n": 0}
    _list_stub(monkeypatch, counter, fail=True)
    cfg = {"base_url": "https://h", "secret_arn": "arn", "token": "TOK"}
    assert re_mod._tulip_name_id_map_get("tulip", cfg) == {}
    # success on the retry
    _list_stub(monkeypatch, counter, fail=False)
    assert re_mod._tulip_name_id_map_get("tulip", cfg) == {"Orders": "Orders-id"}
    assert counter["n"] == 2, "a failure must be retried, not cached for the TTL"


def test_cache_expires(monkeypatch):
    counter = {"n": 0}
    _list_stub(monkeypatch, counter)
    cfg = {"base_url": "https://h", "secret_arn": "arn", "token": "TOK"}
    clock = {"t": 1000.0}
    monkeypatch.setattr(re_mod, "_memo_clock", lambda: clock["t"])
    re_mod._tulip_name_id_map_get("tulip", cfg)
    clock["t"] += re_mod.SAMPLE_MEMO_TTL_SECONDS + 1
    re_mod._tulip_name_id_map_get("tulip", cfg)
    assert counter["n"] == 2, "an expired entry must refetch"


def test_successful_empty_map_is_cached(monkeypatch):
    # list answered with nothing URL-safe -> {} is a SUCCESS and is cached.
    counter = {"n": 0}
    _list_stub(monkeypatch, counter, tables=[{"id": "../bad", "label": "Orders"}])
    cfg = {"base_url": "https://h", "secret_arn": "arn", "token": "TOK"}
    assert re_mod._tulip_name_id_map_get("tulip", cfg) == {}
    assert re_mod._tulip_name_id_map_get("tulip", cfg) == {}
    assert counter["n"] == 1, "a successful empty listing is cached (not a failure)"


def test_cache_key_holds_no_secret(monkeypatch):
    counter = {"n": 0}
    _list_stub(monkeypatch, counter)
    cfg = {"base_url": "https://h", "secret_arn": "arn", "token": "SUPERSECRET-TOKEN"}
    re_mod._tulip_name_id_map_get("tulip", cfg)
    keys_repr = repr(list(re_mod._tulip_table_id_memo.keys()))
    assert "SUPERSECRET-TOKEN" not in keys_repr, keys_repr
