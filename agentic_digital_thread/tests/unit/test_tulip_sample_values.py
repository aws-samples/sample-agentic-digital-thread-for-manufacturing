# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tulip sample-values hook — the cross-system identity-rule evidence path.

A Tulip system writes NO graph Asset nodes, so its join keys live only in Tulip
Table columns. The ``tulip`` registry adapter supplies a ``sample_values`` hook
so the resolve step reaches those values the SAME way: one bounded
read-only GET of a table's records. Fully synthetic — ``_TulipClient.get`` and
the onboarding-session store are stubbed, no network, no AWS.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.resolve_evidence as re_mod  # noqa: E402
import agents.system_agent as sa  # noqa: E402
from agents.connection_types import TULIP  # noqa: E402
from agents.rest_vendor_registry import resolve_vendor  # noqa: E402


_RECORDS = [
    {"run_number": "WO-9921", "qty": 10},
    {"run_number": "WO-9922", "qty": 20},
    {"run_number": "WO-9921", "qty": 30},   # duplicate value
    {"run_number": "", "qty": 40},          # empty value dropped
    {"run_number": None, "qty": 50},        # null value dropped
]


@pytest.fixture
def _stub_discovery(monkeypatch):
    """Stub the onboarding-session store so the table NAME 'Orders' resolves to
    table id 't1' (the shape _discover_tulip saves)."""
    class _FakeStore:
        def get_session(self, system_id):
            return {"discovered_tables": [
                {"name": "Orders", "table_id": "t1",
                 "columns": [{"name": "run_number"}]},
            ]}

    monkeypatch.setattr("agents.onboard_agent._store", lambda: _FakeStore())


@pytest.fixture
def _stub_conn(monkeypatch):
    """A resolved connection carrying base_url + secret_arn + workspace, so no
    node/catalog lookup fires (passed config is already complete)."""
    def _cfg(system_id, cfg):
        return {**cfg}
    monkeypatch.setattr(re_mod, "_tulip_connection_config", _cfg)


def _stub_records(monkeypatch, rows, capture=None):
    def _get(self, path, params=None):
        if capture is not None:
            capture.append({"path": path, "scoped": self._scoped(path),
                            "params": params or {}})
        return rows
    monkeypatch.setattr(sa._TulipClient, "get", _get)


def test_registry_tulip_adapter_has_sample_values_hook():
    adapter = resolve_vendor(TULIP)
    assert adapter is not None and adapter.sample_values is not None


def test_sample_returns_distinct_non_empty_values(monkeypatch, _stub_discovery, _stub_conn):
    _stub_records(monkeypatch, _RECORDS)
    vals = re_mod._sample_tulip_records(
        "tulip", {"base_url": "https://h", "secret_arn": "arn"},
        limit=5, field="Orders.run_number", table="Orders")
    assert vals == ["WO-9921", "WO-9922"]   # distinct, order-preserving, no ""/None


def test_sample_strips_table_prefix_and_resolves_id(monkeypatch, _stub_discovery, _stub_conn):
    cap = []
    _stub_records(monkeypatch, _RECORDS, cap)
    re_mod._sample_tulip_records(
        "tulip", {"base_url": "https://h", "secret_arn": "arn"},
        limit=5, field="Orders.run_number", table="Orders")
    assert cap[0]["path"] == "/api/v3/tables/t1/records"


def test_sample_scopes_records_read_to_workspace(monkeypatch, _stub_discovery, _stub_conn):
    cap = []
    _stub_records(monkeypatch, _RECORDS, cap)
    re_mod._sample_tulip_records(
        "tulip", {"base_url": "https://h", "secret_arn": "arn", "workspace_id": "ws1"},
        limit=5, field="run_number", table="Orders")
    assert cap[0]["scoped"] == "/api/v3/w/ws1/tables/t1/records"


def test_sample_unknown_table_returns_empty(monkeypatch, _stub_discovery, _stub_conn):
    _stub_records(monkeypatch, _RECORDS)
    vals = re_mod._sample_tulip_records(
        "tulip", {"base_url": "https://h", "secret_arn": "arn"},
        limit=5, field="run_number", table="NotDiscovered")
    assert vals == []


def test_sample_no_base_url_returns_empty(monkeypatch, _stub_discovery):
    # no base_url anywhere ⇒ [] (never a fabricated overlap)
    monkeypatch.setattr(re_mod, "_tulip_connection_config", lambda s, c: dict(c))
    _stub_records(monkeypatch, _RECORDS)
    vals = re_mod._sample_tulip_records("tulip", {}, limit=5,
                                        field="run_number", table="Orders")
    assert vals == []


def test_sample_blank_field_returns_empty(monkeypatch, _stub_discovery, _stub_conn):
    _stub_records(monkeypatch, _RECORDS)
    assert re_mod._sample_tulip_records(
        "tulip", {"base_url": "https://h"}, limit=5, field="", table="Orders") == []


def test_sample_respects_limit(monkeypatch, _stub_discovery, _stub_conn):
    _stub_records(monkeypatch, _RECORDS)
    vals = re_mod._sample_tulip_records(
        "tulip", {"base_url": "https://h", "secret_arn": "arn"},
        limit=1, field="run_number", table="Orders")
    assert vals == ["WO-9921"]


def test_sample_handles_records_wrapped_in_dict(monkeypatch, _stub_discovery, _stub_conn):
    _stub_records(monkeypatch, {"records": _RECORDS})
    vals = re_mod._sample_tulip_records(
        "tulip", {"base_url": "https://h", "secret_arn": "arn"},
        limit=5, field="run_number", table="Orders")
    assert vals == ["WO-9921", "WO-9922"]


def test_backend_dispatch_routes_tulip_through_hook(monkeypatch, _stub_discovery, _stub_conn):
    # sample_field_values -> _sample_backend -> adapter.sample_values(table=...)
    _stub_records(monkeypatch, _RECORDS)
    vals = re_mod.sample_field_values(
        system_id="tulip", connection_type=TULIP, schema="",
        table="Orders", field="Orders.run_number",
        connection_config={"base_url": "https://h", "secret_arn": "arn"})
    assert vals == ["WO-9921", "WO-9922"]
