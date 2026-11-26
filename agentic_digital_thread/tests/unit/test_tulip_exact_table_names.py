# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Tulip connector matches a discovered table NAME exactly as discovery saved
it, at the table-id lookup.

``_tulip_table_id_for`` and its saved-session read used to strip the requested
name (and each saved name) before matching, so two Tulip tables named
``"Orders"`` and ``"Orders "`` (whitespace-distinct, both legal Tulip labels)
resolved to the same id, and the sampler, probe and provision seed could read
the wrong table. The session path also took the first of two same-named tables.

The composed test drives the REAL wizard discover route
(``onboard.onboard_discover`` with no chat session), feeds its tables into the
mapping shape the map step returns, and samples through the affinity pass, so
the seams between discover, map and resolve are exercised together.

Fully synthetic: ``_TulipClient.get``, ``_tulip_discover_get``, the catalog and
the session store are stubbed. No network, no AWS.
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

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as onboard_agent  # noqa: E402
import agents.resolve_evidence as re_mod  # noqa: E402
import agents.system_agent as sa  # noqa: E402
from agents.connection_types import TULIP  # noqa: E402
from agents.models import DiscoverRequest  # noqa: E402
from agents.onboarding_session import SessionNotFoundError  # noqa: E402

_TABLES = [{"id": "t1", "label": "Orders"}, {"id": "t2", "label": "Orders "}]
_VALUES = {"t1": ["RUN-A-00001", "RUN-A-00002"], "t2": ["RUN-B-00001", "RUN-B-00002"]}
_CFG = {"base_url": "https://h", "secret_arn": "arn"}


@pytest.fixture(autouse=True)
def _reset():
    re_mod._reset_sample_memo()
    re_mod._reset_tulip_table_id_memo()
    yield
    re_mod._reset_sample_memo()
    re_mod._reset_tulip_table_id_memo()


class _Store:
    def __init__(self, tables=None):
        self._tables = tables

    def get_session(self, system_id):
        if self._tables is None:
            raise SessionNotFoundError(system_id)
        return {"discovered_tables": self._tables}


def _tulip_get(self, path, params=None):
    scoped = self._scoped(path)
    if scoped.endswith("/tables"):
        return list(_TABLES)
    for tid, values in _VALUES.items():
        if f"/tables/{tid}/records" in scoped:
            return [{"lzwyu_erp_order_id": v} for v in values]
    return []


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(onboard_agent, "_store", lambda: _Store(None))
    monkeypatch.setattr(re_mod, "_tulip_connection_config", lambda s, c: {**_CFG, **(c or {})})
    monkeypatch.setattr(sa._TulipClient, "get", _tulip_get)


@pytest.mark.parametrize("name,want", [("Orders", "t1"), ("Orders ", "t2")])
def test_live_lookup_keeps_the_exact_discovered_name(live, name, want):
    assert re_mod._tulip_table_id_for("tulip", name, dict(_CFG)) == want


def test_a_whitespace_only_name_resolves_to_nothing(live):
    assert re_mod._tulip_table_id_for("tulip", "   ", dict(_CFG)) == ""


@pytest.mark.parametrize("name,want", [("Orders", "t1"), ("Orders ", "t2")])
def test_session_lookup_keeps_the_exact_discovered_name(monkeypatch, name, want):
    saved = [{"name": "Orders", "table_id": "t1"}, {"name": "Orders ", "table_id": "t2"}]
    monkeypatch.setattr(onboard_agent, "_store", lambda: _Store(saved))
    assert re_mod._tulip_table_id_for("tulip", name) == want


def test_session_lookup_fails_closed_on_two_tables_with_one_name(monkeypatch):
    saved = [{"name": "Orders", "table_id": "t1"}, {"name": "Orders", "table_id": "t9"}]
    monkeypatch.setattr(onboard_agent, "_store", lambda: _Store(saved))
    assert re_mod._tulip_table_id_for("tulip", "Orders") == ""


def test_session_lookup_keeps_one_name_listed_twice_with_one_id(monkeypatch):
    saved = [{"name": "Orders", "table_id": "t1"}, {"name": "Orders", "table_id": "t1"}]
    monkeypatch.setattr(onboard_agent, "_store", lambda: _Store(saved))
    assert re_mod._tulip_table_id_for("tulip", "Orders") == "t1"


@pytest.mark.parametrize("name", ["Orders", "Orders "])
def test_sampler_reads_the_table_the_name_names(live, name):
    want = _VALUES["t1" if name == "Orders" else "t2"]
    got = re_mod._sample_tulip_records("tulip", dict(_CFG), limit=5,
                                       field="lzwyu_erp_order_id", table=name)
    assert got == want


# --------------------------------------------------------------------------- #
# Composed: the real wizard discover -> the map step's shape -> the affinity pass
# --------------------------------------------------------------------------- #

class _FakeCatalog:
    def __init__(self, entries):
        self._entries = entries

    def get(self, system_id):
        return self._entries.get(system_id)

    def get_onboarded_ids(self):
        return {"erp"}


def test_wizard_discover_through_resolve_samples_each_discovered_table(monkeypatch, live):
    """The wizard's discover route (no chat session) saves each table under its
    exact Tulip label; a mapping built from that output samples the table the
    label names, for both whitespace-distinct tables."""
    entry = {"id": "tulip", "connection_type": TULIP, "schema": "rest",
             "connection_config": {"base_url": "https://h"}}
    fake_catalog = _FakeCatalog({"tulip": entry})
    monkeypatch.setattr(onboard, "catalog", fake_catalog)
    monkeypatch.setattr(onboard_agent, "catalog", fake_catalog)
    monkeypatch.setattr(cr, "resolve_connection", lambda sid: {
        "base_url": "https://h", "token": "T", "auth_scheme": "basic", "secret_arn": "arn"})
    monkeypatch.setattr(onboard, "bedrock_call", lambda *a, **k: "ai analysis")

    def _discover_get(url, headers, params=None):
        if url.endswith("/tables"):
            return list(_TABLES)
        return {"columns": [{"name": "lzwyu_erp_order_id", "label": "ERP Order ID",
                             "dataType": {"type": "string"}}]}
    monkeypatch.setattr(onboard_agent, "_tulip_discover_get", _discover_get)

    disc = asyncio.run(onboard.onboard_discover(
        DiscoverRequest(system_id="tulip", connection_type=TULIP)))
    assert "error" not in disc, disc
    names = [t["name"] for t in disc["tables"]]
    assert sorted(names) == ["Orders", "Orders "], names

    # The map step's output shape for each discovered table's run-number column.
    mappings = [{"table": n, "field": f"{n}.lzwyu_erp_order_id", "concept": "production-run",
                 "role": "identifier",
                 "concepts": [{"concept": "production-run", "role": "identifier"}]}
                for n in names]
    fields = {f.table: f.values for f in onboard._affinity_new_fields("tulip", mappings)}
    assert fields == {"Orders": tuple(_VALUES["t1"]), "Orders ": tuple(_VALUES["t2"])}
