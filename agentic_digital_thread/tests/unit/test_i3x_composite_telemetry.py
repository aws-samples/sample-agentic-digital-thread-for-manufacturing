# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""B2 — sitewise_get_asset_telemetry (knowledge-graph composite) and the
GRAPH_IDENTIFIER passthrough in AgentManager._base_env_vars.

The composite tool resolves <asset-name> -> :Property leaves via a
PARAMETERIZED openCypher query (boto3 neptune-graph, region from env — never
hardcoded), then makes ONE batched history POST and returns a claim-check
capped payload with a labels map. Structured deterministic errors for missing
GRAPH_IDENTIFIER, zero Property rows (the property_count=0 explanation), and
an empty property_filter match.

Fully synthetic — fake neptune-graph + fake _I3xClient; no AWS, no network.
"""
import json

import agents.system_agent as sa

from .test_cap_history_payload import _series, assert_no_raw_series
from .test_i3x_history_tools import FakeClient


class _FakePayload:
    def __init__(self, results):
        self._body = json.dumps({"results": results}).encode()

    def read(self):
        return self._body


class FakeNeptune:
    def __init__(self, results):
        self.results = results
        self.queries = []

    def execute_query(self, **kwargs):
        self.queries.append(kwargs)
        return {"payload": _FakePayload(self.results)}


_ROWS = [
    {"source_id": "a1:p1", "name": "inlet_pressure", "data_type": "DOUBLE",
     "asset_path": "/site/area/line/asset"},
    {"source_id": "a1:p2", "name": "motor_temperature", "data_type": "DOUBLE",
     "asset_path": "/site/area/line/asset"},
]


def _wire_neptune(monkeypatch, neptune, sink=None):
    def fake_client(service, region_name=None, **_kwargs):  # config: db's one-attempt client
        if sink is not None:
            sink.append({"service": service, "region_name": region_name})
        return neptune

    monkeypatch.setattr(sa.boto3, "client", fake_client)


def _composite(client):
    return sa._make_i3x_composite_telemetry_tool(client)._tool_func


def test_missing_graph_identifier_is_structured_error(monkeypatch):
    monkeypatch.delenv("GRAPH_IDENTIFIER", raising=False)
    client = FakeClient()
    out = json.loads(_composite(client)(
        asset_name="<asset-name>", start_time="2026-04-30T00:00:00Z",
        end_time="2026-04-30T01:00:00Z"))
    assert "GRAPH_IDENTIFIER" in out["error"]
    assert out["error_type"] == "configuration"
    assert client.calls == []


def test_zero_property_rows_explains_property_count(monkeypatch):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-unit")
    _wire_neptune(monkeypatch, FakeNeptune([]))
    client = FakeClient()
    out = json.loads(_composite(client)(
        asset_name="structural-only", start_time="2026-04-30T00:00:00Z",
        end_time="2026-04-30T01:00:00Z"))
    assert "property_count = 0" in out["error"]
    assert "sitewise_get_related_objects" in out["error"]
    assert client.calls == []


def test_property_filter_narrows_requested_ids(monkeypatch):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-unit")
    _wire_neptune(monkeypatch, FakeNeptune(_ROWS))
    client = FakeClient(response=[])
    _composite(client)(
        asset_name="<asset-name>", start_time="2026-04-30T00:00:00Z",
        end_time="2026-04-30T01:00:00Z", property_filter="PRESS")
    assert client.calls[0]["payload"]["elementIds"] == ["a1:p1"]


def test_property_filter_no_match_is_structured_error(monkeypatch):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-unit")
    _wire_neptune(monkeypatch, FakeNeptune(_ROWS))
    client = FakeClient()
    out = json.loads(_composite(client)(
        asset_name="<asset-name>", start_time="2026-04-30T00:00:00Z",
        end_time="2026-04-30T01:00:00Z", property_filter="nonexistent"))
    assert out["error"] == "No properties matched property_filter."
    assert client.calls == []


def test_success_carries_labels_count_and_capped_history(monkeypatch):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-unit")
    neptune = FakeNeptune(_ROWS)
    _wire_neptune(monkeypatch, neptune)
    client = FakeClient(response=[
        {"elementId": "a1:p1", "result": {"values": _series(60)}},
        {"elementId": "a1:p2", "result": {"values": _series(60)}},
    ])
    raw = _composite(client)(
        asset_name="<asset-name>", start_time="2026-04-30T00:00:00Z",
        end_time="2026-04-30T01:00:00Z")
    # Guaranteed charts ride as a plain-text suffix AFTER the JSON —
    # parse the JSON prefix (raw_decode), the suffix is covered by the
    # dedicated guaranteed-charts tests.
    out, _ = json.JSONDecoder().raw_decode(raw)
    assert out["property_count"] == 2
    assert out["labels"] == {"a1:p1": "inlet_pressure", "a1:p2": "motor_temperature"}
    assert out["asset_path"] == "/site/area/line/asset"
    assert out["resolved_via"] == "exact"           # fast path, contract kept
    assert out["requested_name"] == "<asset-name>"
    assert "NEVER type these values" in out["note"]
    # One batched POST with maxDepth=1 (property leaves ARE the channels).
    call = client.calls[0]
    assert call["path"] == "/objects/history"
    assert call["payload"]["maxDepth"] == 1
    assert call["body_cap"] == sa._I3X_HISTORY_BODY_CAP
    # No raw >8-point VQT arrays anywhere in the response (recursive).
    assert_no_raw_series(out)
    assert "_stats" in out["history"][0]["result"]["values"]


def test_cypher_is_parameterized_and_region_from_env(monkeypatch):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-unit")
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    neptune = FakeNeptune(_ROWS)
    sink = []
    _wire_neptune(monkeypatch, neptune, sink)
    _composite(FakeClient(response=[]))(
        asset_name="o'brien-asset", start_time="2026-04-30T00:00:00Z",
        end_time="2026-04-30T01:00:00Z")
    q = neptune.queries[0]
    # Name travels ONLY as a bound parameter — never interpolated.
    assert q["parameters"] == {"name": "o'brien-asset"}
    assert "$name" in q["queryString"]
    assert "o'brien-asset" not in q["queryString"]
    assert q["language"] == "OPEN_CYPHER"
    assert q["graphIdentifier"] == "g-unit"
    # Region comes from the env, never hardcoded.
    assert sink == [{"service": "neptune-graph", "region_name": "eu-central-1"}]


def test_missing_inputs_short_circuit_without_calls(monkeypatch):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-unit")
    calls = []
    _wire_neptune(monkeypatch, FakeNeptune(_ROWS), calls)
    client = FakeClient()
    fn = _composite(client)
    out = json.loads(fn(asset_name="", start_time="2026-04-30T00:00:00Z",
                        end_time="2026-04-30T01:00:00Z"))
    assert out["error"] == "asset_name is required."
    out = json.loads(fn(asset_name="<asset-name>", start_time="", end_time=""))
    assert out["error_type"] == "invalid_request"
    assert calls == [] and client.calls == []


def test_unexpected_exception_stays_fixed_safe(monkeypatch):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-unit")
    _wire_neptune(monkeypatch, FakeNeptune(_ROWS))
    client = FakeClient(error=ValueError("secret-internal-detail"))
    out = json.loads(_composite(client)(
        asset_name="<asset-name>", start_time="2026-04-30T00:00:00Z",
        end_time="2026-04-30T01:00:00Z"))
    assert out == {"error": "tool_unavailable", "error_type": "tool_error"}
    assert "secret-internal-detail" not in json.dumps(out)


# --------------------------------------------------------------------------- #
# AgentManager._base_env_vars — GRAPH_IDENTIFIER passthrough
# --------------------------------------------------------------------------- #

def _env(monkeypatch):
    from agents.agent_manager import AgentManager
    return AgentManager(mode="cloud")._base_env_vars(
        "sys1", "Example Corp", "public", "SiteWise (i3X)", "us-east-1", {})


def test_base_env_vars_passes_graph_identifier_from_env(monkeypatch):
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-123abc")
    assert _env(monkeypatch)["GRAPH_IDENTIFIER"] == "g-123abc"


def test_base_env_vars_graph_identifier_defaults_empty(monkeypatch):
    monkeypatch.delenv("GRAPH_IDENTIFIER", raising=False)
    assert _env(monkeypatch)["GRAPH_IDENTIFIER"] == ""
