# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""B1 — i3X history tools + the relaxed _I3xClient boundary.

* sitewise_get_asset_history: start/end REQUIRED with an instructive
  short-circuit (no HTTP call), max_depth default 0, response claim-check
  capped, transport errors stay FIXED-SAFE (_i3x_error).
* sitewise_get_assets_history (batch): list input, single string tolerated,
  same required-window short-circuit, ONE batched POST (best-effort
  guaranteed-chart fetches follow it — pinned in test_i3x_guaranteed_charts).
* _I3xClient.request: dict-OR-list parse with the i3X envelope stripped at
  the client boundary; error envelopes raise; per-request ``body_cap``
  overrides the default connector limit for history endpoints.

Fully synthetic: fake client objects and a fake ``pinned_http.client``; no network.
"""
import json

import pytest

import agents.system_agent as sa
from agents import pinned_http

from .test_cap_history_payload import _series, assert_no_raw_series


class FakeClient:
    """_I3xClient double recording request() calls."""

    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response
        self.error = error

    def request(self, method, path, payload, body_cap=0):
        self.calls.append({"method": method, "path": path,
                           "payload": payload, "body_cap": body_cap})
        if self.error is not None:
            raise self.error
        return self.response


# --------------------------------------------------------------------------- #
# sitewise_get_asset_history
# --------------------------------------------------------------------------- #

def test_history_missing_window_is_instructive_and_makes_no_call():
    client = FakeClient()
    hist = sa._make_i3x_history_tool(client)
    out = json.loads(hist._tool_func(element_id="e1", start_time="", end_time=""))
    assert out["error_type"] == "invalid_request"
    assert "start_time and end_time are required" in out["error"]
    assert "sitewise_get_telemetry_chart" in out["error"]
    assert client.calls == []


def test_history_missing_end_only_short_circuits():
    client = FakeClient()
    hist = sa._make_i3x_history_tool(client)
    out = json.loads(hist._tool_func(
        element_id="e1", start_time="2026-04-30T00:00:00Z", end_time=""))
    assert out["error_type"] == "invalid_request"
    assert client.calls == []


def test_history_posts_window_and_caps_response():
    client = FakeClient(response=[{"elementId": "e1",
                                   "result": {"values": _series(50)}}])
    hist = sa._make_i3x_history_tool(client)
    out = json.loads(hist._tool_func(
        element_id="e1", start_time="2026-04-30T00:00:00Z",
        end_time="2026-04-30T23:59:59Z"))
    call = client.calls[0]
    assert call["method"] == "POST" and call["path"] == "/objects/history"
    assert call["payload"] == {
        "elementIds": ["e1"], "startTime": "2026-04-30T00:00:00Z",
        "endTime": "2026-04-30T23:59:59Z", "maxDepth": 0,  # default is 0 now
    }
    # History endpoints ride the raised body cap (reduced server-side).
    assert call["body_cap"] == sa._I3X_HISTORY_BODY_CAP
    assert "_stats" in out[0]["result"]["values"]
    assert_no_raw_series(out)


def test_history_transport_error_stays_fixed_safe():
    client = FakeClient(error=RuntimeError("i3X error: Unauthorized (401): nope"))
    hist = sa._make_i3x_history_tool(client)
    out = json.loads(hist._tool_func(
        element_id="e1", start_time="2026-04-30T00:00:00Z",
        end_time="2026-04-30T01:00:00Z"))
    assert out == {"error": "tool_unavailable", "error_type": "tool_error"}
    assert "401" not in json.dumps(out)


# --------------------------------------------------------------------------- #
# sitewise_get_assets_history (batch)
# --------------------------------------------------------------------------- #

def test_batch_accepts_list_and_makes_one_batched_post():
    """The batch POST stays ONE call carrying all ids. Guaranteed
    charts add best-effort per-asset chart fetches AFTER the batch POST, so
    the old "exactly one request total" assertion became "exactly one
    BATCHED history request" (chart fetches are single-id)."""
    client = FakeClient(response=[])
    batch = sa._make_i3x_batch_history_tool(client)
    batch._tool_func(element_ids=["e1", "e2", "e3"],
                     start_time="2026-04-30T00:00:00Z",
                     end_time="2026-04-30T01:00:00Z")
    assert client.calls[0]["payload"]["elementIds"] == ["e1", "e2", "e3"]
    assert client.calls[0]["payload"]["maxDepth"] == 0
    batched = [c for c in client.calls if c["path"] == "/objects/history"
               and len(c["payload"]["elementIds"]) > 1]
    assert len(batched) == 1


def test_batch_tolerates_single_string():
    client = FakeClient(response=[])
    batch = sa._make_i3x_batch_history_tool(client)
    batch._tool_func(element_ids="only-one",
                     start_time="2026-04-30T00:00:00Z",
                     end_time="2026-04-30T01:00:00Z")
    assert client.calls[0]["payload"]["elementIds"] == ["only-one"]


def test_batch_missing_window_and_empty_ids_short_circuit():
    client = FakeClient()
    batch = sa._make_i3x_batch_history_tool(client)
    out = json.loads(batch._tool_func(element_ids=["e1"], start_time="", end_time=""))
    assert out["error_type"] == "invalid_request"
    out = json.loads(batch._tool_func(
        element_ids=[], start_time="2026-04-30T00:00:00Z",
        end_time="2026-04-30T01:00:00Z"))
    assert "non-empty list" in out["error"]
    assert client.calls == []


def test_batch_response_is_claim_check_capped():
    client = FakeClient(response=[
        {"elementId": "e1", "result": {"values": _series(40)}},
        {"elementId": "e2", "result": {"values": _series(6)}},
    ])
    batch = sa._make_i3x_batch_history_tool(client)
    raw = batch._tool_func(
        element_ids=["e1", "e2"], start_time="2026-04-30T00:00:00Z",
        end_time="2026-04-30T01:00:00Z")
    # Guaranteed charts ride as a plain-text suffix AFTER the
    # claim-checked JSON — parse the JSON prefix.
    out, _ = json.JSONDecoder().raw_decode(raw)
    assert "_stats" in out[0]["result"]["values"]
    assert len(out[1]["result"]["values"]) == 6
    assert_no_raw_series(out)


# --------------------------------------------------------------------------- #
# _I3xClient.request — dict-or-list + envelope strip + body_cap
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def raise_for_status(self):
        pass

    def iter_bytes(self):
        yield self._body


class _FakeStreamCtx:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *args):
        return False


class _FakePinnedHttp:
    """Stands in for ``pinned_http.client``, the one seam ``_I3xClient``
    sends requests through. Patching the seam, not the ``httpx`` module,
    leaves every module that reads ``httpx`` at import untouched, so the
    result does not depend on which modules an earlier test imported."""

    def __init__(self, body: bytes):
        self._body = body
        self.calls = []
        self.client_kwargs = []

    def client(self, **kwargs):
        outer = self
        outer.client_kwargs.append(kwargs)

        class _C:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def stream(self, method, url, **kw):
                outer.calls.append((method, url, kw))
                return _FakeStreamCtx(_FakeResponse(outer._body))

        return _C()


def _client_with_body(monkeypatch, body: bytes) -> tuple:
    fake = _FakePinnedHttp(body)
    monkeypatch.setattr(pinned_http, "client", fake.client)
    return sa._I3xClient("http://unit.test", "", "tok", "bearer"), fake


def test_request_accepts_list_shaped_response(monkeypatch):
    client, _ = _client_with_body(monkeypatch, b'[{"elementId": "e1"}]')
    out = client.request("POST", "/objects/list", {"elementIds": ["e1"]})
    assert out == [{"elementId": "e1"}]


def test_request_strips_success_envelope_at_boundary(monkeypatch):
    body = json.dumps({"success": True, "results": [{"elementId": "e1"}]}).encode()
    client, _ = _client_with_body(monkeypatch, body)
    out = client.request("GET", "/objects", {})
    assert out == [{"elementId": "e1"}]


def test_request_error_envelope_raises_into_fixed_safe_path(monkeypatch):
    body = json.dumps({"success": False, "responseDetail": {
        "title": "Bad Request", "status": 400, "detail": "missing window"}}).encode()
    client, _ = _client_with_body(monkeypatch, body)
    with pytest.raises(RuntimeError):
        client.request("POST", "/objects/history", {})


def test_request_rejects_scalar_json(monkeypatch):
    client, _ = _client_with_body(monkeypatch, b"42")
    with pytest.raises(ValueError, match="object or array"):
        client.request("GET", "/namespaces", {})


def test_request_body_cap_overrides_default_limit(monkeypatch):
    body = b'"' + b"x" * 30 + b'"'  # 32 bytes of valid JSON string
    client, _ = _client_with_body(monkeypatch, body)
    # Under a tiny explicit cap the stream truncates -> connector error.
    with pytest.raises(ValueError, match="exceeds connector limit"):
        client.request("POST", "/objects/history", {}, body_cap=10)


def test_request_tls_verify_toggle_reaches_httpx(monkeypatch):
    monkeypatch.setenv("I3X_TLS_VERIFY", "false")
    client, fake = _client_with_body(monkeypatch, b"{}")
    client.request("GET", "/namespaces", {})
    assert fake.client_kwargs[0]["verify"] is False


# --------------------------------------------------------------------------- #
# Tool registration — existing relative order stays stable, new tools added
# --------------------------------------------------------------------------- #

def test_make_i3x_tools_ordering_stable_with_new_tools():
    tools = sa._make_i3x_tools("http://unit.test")
    names = [t.tool_name for t in tools]
    assert names == [
        "sitewise_list_asset_models",
        "sitewise_list_assets",
        "sitewise_get_related_objects",
        "sitewise_get_asset_current_values",
        "sitewise_get_asset_history",
        "sitewise_get_assets_history",
        "sitewise_get_asset_telemetry",
        "sitewise_get_telemetry_chart",
        "sitewise_list_namespaces",
    ]
