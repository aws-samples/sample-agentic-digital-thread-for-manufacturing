# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fuzzy asset resolution in sitewise_get_asset_telemetry.

Exact-name match stays the fast path (one graph query, contract unchanged).
On a miss, the input is tokenized (stop words dropped) and AND-matched
against name + hierarchy path + asset-model of every telemetry-bearing
asset; the score counts name+model hits only (path hits are weaker by
design). A unique best match resolves via source_id and the response carries
``resolved_via: "fuzzy"`` + ``requested_name``; a tie returns a structured
``candidates`` list (<=10, name+path — re-call with the exact name); a total
miss returns a bounded ``known_assets`` sample (<=20).

Fully synthetic — fake neptune-graph routed per query shape + fake
_I3xClient; no AWS, no network. Fixture assets use generic equipment
vocabulary only.
"""
import json

import agents.system_agent as sa

from .test_i3x_composite_telemetry import _FakePayload
from .test_i3x_history_tools import FakeClient


class RoutingNeptune:
    """neptune-graph double routing on queryString substrings."""

    def __init__(self, routes):
        self.routes = routes  # list of (substring, results)
        self.queries = []

    def execute_query(self, **kwargs):
        self.queries.append(kwargs)
        for sub, results in self.routes:
            if sub in kwargs["queryString"]:
                return {"payload": _FakePayload(results)}
        return {"payload": _FakePayload([])}


_EXACT_Q = "Asset {name: $name}"
_FUZZY_Q = "INSTANCE_OF"
_BY_SID_Q = "Asset {source_id: $sid}"

_ASSETS = [
    {"name": "Line1Pump", "path": "/siteA/area1/line1/pump",
     "source_id": "a-l1p", "model": "Pump"},
    {"name": "Line2Pump", "path": "/siteA/area1/line2/pump",
     "source_id": "a-l2p", "model": "Pump"},
    {"name": "Line1Conveyor", "path": "/siteA/area1/line1/conveyor",
     "source_id": "a-l1c", "model": "Conveyor"},
]

_ROWS = [
    {"source_id": "a-l1p:p1", "name": "inlet_pressure", "data_type": "DOUBLE",
     "asset_path": "/siteA/area1/line1/pump"},
]


def _wire(monkeypatch, neptune):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-unit")
    monkeypatch.setattr(sa.boto3, "client", lambda *a, **k: neptune)


def _composite(client):
    return sa._make_i3x_composite_telemetry_tool(client)._tool_func


def _call(client, name):
    return _composite(client)(
        asset_name=name, start_time="2026-04-30T00:00:00Z",
        end_time="2026-04-30T01:00:00Z")


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def test_fuzzy_tokens_drop_stop_words_and_lowercase():
    tokens = sa._i3x_fuzzy_tokens("Show me the telemetry for Pump on Line 1")
    assert tokens == ["pump", "line", "1"]
    assert sa._i3x_fuzzy_tokens("") == []
    assert sa._i3x_fuzzy_tokens("the a an of") == []


def test_fuzzy_match_requires_all_tokens_and_scores_name_model_over_path():
    matches = sa._i3x_fuzzy_match(["pump", "line", "1"], _ASSETS)
    by_name = {a["name"]: score for score, a in matches}
    # Conveyor lacks "pump" — AND-match excludes it entirely.
    assert "Line1Conveyor" not in by_name
    # Line2Pump only matches "1" through its path (area1) — weaker score.
    assert by_name["Line1Pump"] == 3
    assert by_name["Line2Pump"] == 2


def test_fuzzy_match_empty_tokens_match_nothing():
    assert sa._i3x_fuzzy_match([], _ASSETS) == []


# --------------------------------------------------------------------------- #
# Resolution paths through the composite tool
# --------------------------------------------------------------------------- #

def test_exact_hit_bypasses_fuzzy_entirely(monkeypatch):
    neptune = RoutingNeptune([(_EXACT_Q, _ROWS)])
    _wire(monkeypatch, neptune)
    raw = _call(FakeClient(response=[]), "Line1Pump")
    out, _ = json.JSONDecoder().raw_decode(raw)
    assert out["resolved_via"] == "exact"
    # ONE graph query — no fuzzy candidate sweep was issued.
    assert len(neptune.queries) == 1
    assert _FUZZY_Q not in neptune.queries[0]["queryString"]


def test_descriptive_phrase_resolves_uniquely_via_fuzzy(monkeypatch):
    neptune = RoutingNeptune([
        (_EXACT_Q, []), (_FUZZY_Q, _ASSETS), (_BY_SID_Q, _ROWS)])
    _wire(monkeypatch, neptune)
    raw = _call(FakeClient(response=[]), "pump on line 1")
    out, _ = json.JSONDecoder().raw_decode(raw)
    assert out["resolved_via"] == "fuzzy"
    assert out["asset_name"] == "Line1Pump"
    assert out["requested_name"] == "pump on line 1"
    assert out["asset_path"] == "/siteA/area1/line1/pump"
    # The property fetch was keyed by the RESOLVED source_id, parameterized.
    sid_q = [q for q in neptune.queries if _BY_SID_Q in q["queryString"]]
    assert sid_q and sid_q[0]["parameters"] == {"sid": "a-l1p"}


def test_ambiguous_phrase_returns_candidates_contract(monkeypatch):
    neptune = RoutingNeptune([(_EXACT_Q, []), (_FUZZY_Q, _ASSETS)])
    _wire(monkeypatch, neptune)
    client = FakeClient()
    out = json.loads(_call(client, "pump"))  # both pumps tie at score 1
    assert out["error_type"] == "ambiguous"
    assert "Re-call" in out["error"]
    assert {c["name"] for c in out["candidates"]} == {"Line1Pump", "Line2Pump"}
    assert all(set(c) == {"name", "path"} for c in out["candidates"])
    assert client.calls == []  # no history POST on ambiguity


def test_candidates_list_capped_at_ten(monkeypatch):
    many = [{"name": f"Pump{i:02d}", "path": f"/siteA/area{i}/pump",
             "source_id": f"a-{i}", "model": "Pump"} for i in range(12)]
    neptune = RoutingNeptune([(_EXACT_Q, []), (_FUZZY_Q, many)])
    _wire(monkeypatch, neptune)
    out = json.loads(_call(FakeClient(), "pump"))
    assert len(out["candidates"]) == 10


def test_no_match_returns_bounded_known_assets_sample(monkeypatch):
    many = [{"name": f"Conveyor{i:02d}", "path": f"/siteA/area{i}/conveyor",
             "source_id": f"a-{i}", "model": "Conveyor"} for i in range(25)]
    neptune = RoutingNeptune([(_EXACT_Q, []), (_FUZZY_Q, many)])
    _wire(monkeypatch, neptune)
    client = FakeClient()
    out = json.loads(_call(client, "turbine"))
    assert out["error_type"] == "not_found"
    assert "property_count = 0" in out["error"]
    assert "sitewise_get_related_objects" in out["error"]
    assert len(out["known_assets"]) == 20  # bounded sample of 25
    assert client.calls == []


def test_fuzzy_resolved_asset_without_properties_is_structured_error(monkeypatch):
    neptune = RoutingNeptune([
        (_EXACT_Q, []), (_FUZZY_Q, _ASSETS), (_BY_SID_Q, [])])
    _wire(monkeypatch, neptune)
    out = json.loads(_call(FakeClient(), "pump on line 1"))
    assert out["error_type"] == "not_found"
    assert out["resolved_name"] == "Line1Pump"
    assert "no Property nodes" in out["error"]


def test_fuzzy_candidate_query_only_covers_telemetry_bearing_assets(monkeypatch):
    """The candidate sweep is hierarchy-constrained by construction: only
    assets that OWN :Property leaves qualify (HAS_PROPERTY match), with the
    model joined optionally."""
    neptune = RoutingNeptune([(_EXACT_Q, []), (_FUZZY_Q, _ASSETS),
                              (_BY_SID_Q, _ROWS)])
    _wire(monkeypatch, neptune)
    _call(FakeClient(response=[]), "pump on line 1")
    fuzzy_q = [q for q in neptune.queries if _FUZZY_Q in q["queryString"]][0]
    assert "HAS_PROPERTY" in fuzzy_q["queryString"]
    assert "OPTIONAL MATCH" in fuzzy_q["queryString"]
    # Nothing user-controlled interpolated. The shared graph client
    # (db.neptune_execute) omits an empty parameter map rather than sending {}.
    assert fuzzy_q.get("parameters", {}) == {}
