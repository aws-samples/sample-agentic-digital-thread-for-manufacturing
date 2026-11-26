# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tulip membership probe: lets the deterministic exact-agreement pass confirm
a Tulip <-> ERP join.

``identity_affinity.exact_shared`` needs a probe that answers "which of these
values exist in that column" on BOTH sides; ``None`` (cannot probe) fails the
pair closed. ``resolve_evidence.probe_field_values`` was PostgreSQL-only, so a
Tulip column could never be confirmed. The ``tulip`` registry adapter now
supplies a ``membership_probe`` hook: bounded, read-only record reads filtered
with ``equal`` filters combined by ``filterAggregator=any``. Fully synthetic:
``_TulipClient.get`` and the onboarding-session store are stubbed, no network.
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

import agents.identity_affinity as ia  # noqa: E402
import agents.resolve_evidence as re_mod  # noqa: E402
import agents.system_agent as sa  # noqa: E402
from agents.connection_types import TULIP  # noqa: E402
from agents.rest_vendor_registry import resolve_vendor  # noqa: E402

#: What the stubbed Tulip table holds in its run_number column.
_PRESENT = {"WO-9921", "WO-9922", "WO-9930"}


@pytest.fixture
def _stub_discovery(monkeypatch):
    class _FakeStore:
        def get_session(self, system_id):
            return {"discovered_tables": [
                {"name": "Orders", "table_id": "t1", "columns": [{"name": "run_number"}]},
            ]}

    monkeypatch.setattr("agents.onboard_agent._store", lambda: _FakeStore())


@pytest.fixture
def _stub_conn(monkeypatch):
    monkeypatch.setattr(re_mod, "_tulip_connection_config",
                        lambda system_id, cfg: {"base_url": "https://h", "secret_arn": "arn",
                                                **(cfg or {})})


def _fake_tulip(monkeypatch, present=_PRESENT, capture=None, extra_rows=(), fail=False):
    """A records endpoint that honors equal filters joined by filterAggregator=any."""
    def _get(self, path, params=None):
        params = params or {}
        if capture is not None:
            capture.append({"path": path, "scoped": self._scoped(path), "params": dict(params)})
        if fail:
            raise RuntimeError("tulip down")
        filters = json.loads(params.get("filters") or "[]")
        assert params.get("filterAggregator") == "any"
        wanted = {f["arg"] for f in filters if f.get("functionType") == "equal"}
        rows = [{"id": v, "run_number": v} for v in sorted(present) if v in wanted]
        return rows + list(extra_rows)
    monkeypatch.setattr(sa._TulipClient, "get", _get)


def _probe(values, table="Orders", field="Orders.run_number"):
    return re_mod.probe_field_values(system_id="tulip", connection_type=TULIP, schema="",
                                     table=table, field=field, values=values)


def test_tulip_adapter_has_membership_probe_hook():
    adapter = resolve_vendor(TULIP)
    assert adapter is not None and adapter.membership_probe is not None


def test_probe_returns_only_values_present(monkeypatch, _stub_discovery, _stub_conn):
    _fake_tulip(monkeypatch)
    assert sorted(_probe(["WO-9921", "WO-0001", "WO-9930"])) == ["WO-9921", "WO-9930"]


def test_probe_found_nothing_is_empty_list_not_none(monkeypatch, _stub_discovery, _stub_conn):
    _fake_tulip(monkeypatch)
    assert _probe(["WO-0001"]) == []


def test_probe_ignores_rows_outside_the_asked_values(monkeypatch, _stub_discovery, _stub_conn):
    # A server that returns an unrelated row must not make a value look present.
    _fake_tulip(monkeypatch, extra_rows=[{"id": "x", "run_number": "WO-7777"}])
    assert _probe(["WO-9921", "WO-0001"]) == ["WO-9921"]


def test_probe_uses_equal_filters_any_aggregator_and_bounded_requests(
        monkeypatch, _stub_discovery, _stub_conn):
    cap = []
    _fake_tulip(monkeypatch, capture=cap)
    vals = [f"WO-{i:04d}" for i in range(500)]
    _probe(vals)
    asked = [f for c in cap for f in json.loads(c["params"]["filters"])]
    assert all(f["field"] == "run_number" and f["functionType"] == "equal" for f in asked)
    assert len({f["arg"] for f in asked}) == re_mod.PROBE_MAX_VALUES
    assert len(cap) <= re_mod.TULIP_PROBE_MAX_REQUESTS
    assert all(c["path"] == "/api/v3/tables/t1/records" for c in cap)
    assert all(1 <= c["params"]["limit"] <= 100 for c in cap)


def test_probe_is_workspace_scoped(monkeypatch, _stub_discovery):
    monkeypatch.setattr(re_mod, "_tulip_connection_config",
                        lambda s, c: {"base_url": "https://h", "secret_arn": "arn",
                                      "workspace_id": "ws1"})
    cap = []
    _fake_tulip(monkeypatch, capture=cap)
    _probe(["WO-9921"])
    assert cap and all(c["scoped"] == "/api/v3/w/ws1/tables/t1/records" for c in cap)


def test_probe_any_request_failure_is_none(monkeypatch, _stub_discovery, _stub_conn):
    _fake_tulip(monkeypatch, fail=True)
    assert _probe(["WO-9921"]) is None


def test_probe_unknown_table_or_no_connection_is_none(monkeypatch, _stub_discovery):
    monkeypatch.setattr(re_mod, "_tulip_connection_config", lambda s, c: dict(c or {}))
    _fake_tulip(monkeypatch)
    assert _probe(["WO-9921"]) is None                      # no base_url
    monkeypatch.setattr(re_mod, "_tulip_connection_config",
                        lambda s, c: {"base_url": "https://h", "secret_arn": "arn"})
    assert _probe(["WO-9921"], table="NotDiscovered") is None
    assert _probe(["WO-9921"], field="") is None


def test_full_page_probes_the_next_page(monkeypatch, _stub_discovery, _stub_conn):
    # One value with more matching rows than a page holds: a value first seen on
    # page 2 is still found, and paging stops once every asked value is found.
    cap = []

    def _get(self, path, params=None):
        cap.append(dict(params or {}))
        off = int(params.get("offset", 0))
        if off == 0:
            return [{"run_number": "WO-9921"}] * int(params["limit"])
        return [{"run_number": "WO-9922"}]
    monkeypatch.setattr(sa._TulipClient, "get", _get)
    assert sorted(_probe(["WO-9921", "WO-9922"])) == ["WO-9921", "WO-9922"]
    assert [c.get("offset", 0) for c in cap] == [0, int(cap[0]["limit"])]


def test_non_rest_types_still_cannot_probe():
    for ct in ("Athena", "MCP Server", "SiteWise (Direct)"):
        assert re_mod.probe_field_values(system_id="x", connection_type=ct, schema="s",
                                         table="t", field="f", values=["a"]) is None


def test_exact_pass_can_now_confirm_a_tulip_erp_pair(monkeypatch, _stub_discovery, _stub_conn):
    """End to end through identity_affinity.exact_shared with the real probe
    dispatch: the Tulip side answers instead of failing the pair closed."""
    shared = [f"RUN-{i:06d}" for i in range(40)]
    _fake_tulip(monkeypatch, present=set(shared))
    tulip = ia.SampledField("tulip", "Orders", "run_number", "run-id", tuple(shared[:30]),
                            concepts=frozenset({"run-id"}))
    erp = ia.SampledField("erp", "production_runs", "run_number", "run-id", tuple(shared[10:]),
                          concepts=frozenset({"run-id"}))

    def probe(field, values):
        if field.system_id == "tulip":
            return re_mod.probe_field_values(system_id="tulip", connection_type=TULIP,
                                             schema="", table=field.table, field=field.field,
                                             values=values)
        return [v for v in values if v in shared]  # the ERP side (PostgreSQL) stubbed
    got = ia.exact_shared(tulip, erp, probe)
    assert got is not None and got == frozenset(shared)


# --------------------------------------------------------------------------- #
# Exact membership and the None vs [] contract
# --------------------------------------------------------------------------- #

def _rows_tulip(monkeypatch, payload):
    """A records endpoint that returns ``payload`` whatever was asked."""
    monkeypatch.setattr(sa._TulipClient, "get", lambda self, path, params=None: payload)


def test_padded_row_value_does_not_match_an_unpadded_ask(monkeypatch, _stub_discovery, _stub_conn):
    _rows_tulip(monkeypatch, [{"id": "1", "run_number": " A "}, {"id": "2", "run_number": "B"}])
    assert _probe(["A", "B"]) == ["B"]


def test_five_padded_values_cannot_confirm_an_exact_pair(monkeypatch, _stub_discovery, _stub_conn):
    shared = [f"WO-{i}" for i in range(5)]
    _rows_tulip(monkeypatch, [{"id": str(i), "run_number": f" {v} "} for i, v in enumerate(shared)])
    assert _probe(shared) == []


@pytest.mark.parametrize("payload", [
    {"rows": []},                         # object without a records list
    {"records": "nope"},                  # records not a list
    [1, 2, 3],                            # list of non-objects
    [{"id": "1", "run_number": "WO-9921"}, "junk"],   # mixed list
    "WO-9921",                            # JSON scalar
])
def test_unexpected_envelope_is_none_not_empty(monkeypatch, _stub_discovery, _stub_conn, payload):
    _rows_tulip(monkeypatch, payload)
    assert _probe(["WO-9921"]) is None


def test_valid_empty_envelopes_are_empty_list(monkeypatch, _stub_discovery, _stub_conn):
    for payload in ([], {"records": []}):
        _rows_tulip(monkeypatch, payload)
        assert _probe(["WO-9921"]) == []
