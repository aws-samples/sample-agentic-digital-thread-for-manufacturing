# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Task 1.2 — the shared SiteWise read/introspection seam (``agents.sitewise_read``).

Fully synthetic: the ``iotsitewise`` client is a stub with ARBITRARY model/property
names (never a live customer's names). No AWS, no network, no dataset literals. Asserts the
seam enumerates models+properties, describes assets, builds correct batch-history
entries, and honors cross-account config via the shared ``boto3_client_for`` helper.
"""
from __future__ import annotations

import agents.aws_session as aws_session
import agents.sitewise_read as sw


class _FakePaginator:
    def __init__(self, pages, sink=None):
        self._pages = pages
        self._sink = sink

    def paginate(self, **kwargs):
        if self._sink is not None:
            self._sink.append(kwargs)
        return iter(self._pages)


class _FakeSiteWise:
    """iotsitewise double driven by SYNTHETIC model/property fixtures."""

    def __init__(self):
        self.paginate_kwargs = []
        self.described_models = []
        self.history_calls = []
        # Two synthetic models, arbitrary names + properties.
        self._models = [
            {"id": "mdl-evt", "name": "evt_model", "status": {"state": "ACTIVE"},
             "description": "synthetic event model"},
            {"id": "mdl-met", "name": "metric_model", "status": {"state": "ACTIVE"},
             "description": "synthetic metric model"},
        ]
        self._model_props = {
            "mdl-evt": [
                {"id": "p-dur", "name": "dur", "dataType": "DOUBLE", "unit": "s"},
                {"id": "p-from", "name": "from_st", "dataType": "STRING", "unit": ""},
                {"id": "p-to", "name": "to_st", "dataType": "STRING", "unit": ""},
            ],
            "mdl-met": [
                {"id": "p-amt", "name": "amt", "dataType": "DOUBLE", "unit": "ea"},
                {"id": "p-waste", "name": "waste", "dataType": "DOUBLE", "unit": "ea"},
            ],
        }

    def get_paginator(self, name):
        if name == "list_asset_models":
            return _FakePaginator([{"assetModelSummaries": self._models}])
        if name == "list_assets":
            return _FakePaginator(
                [{"assetSummaries": [
                    {"id": "asset-1", "name": "Unit-1", "assetModelId": "mdl-evt",
                     "status": {"state": "ACTIVE"}, "hierarchies": []},
                ]}],
                self.paginate_kwargs,
            )
        raise AssertionError(name)

    def describe_asset_model(self, assetModelId):  # noqa: N803 (boto3 kwarg)
        self.described_models.append(assetModelId)
        return {
            "assetModelId": assetModelId,
            "assetModelName": next(m["name"] for m in self._models if m["id"] == assetModelId),
            "assetModelDescription": "",
            "assetModelProperties": self._model_props[assetModelId],
        }

    def describe_asset(self, assetId):  # noqa: N803
        return {
            "assetId": assetId, "assetName": "Unit-1", "assetModelId": "mdl-evt",
            "assetProperties": self._model_props["mdl-evt"],
            "assetHierarchies": [{"id": "h1", "name": "children"}],
        }

    def get_asset_property_value(self, assetId, propertyId):  # noqa: N803
        return {"propertyValue": {"value": {"doubleValue": 42.0},
                                  "timestamp": {"timeInSeconds": 111}, "quality": "GOOD"}}

    def batch_get_asset_property_value_history(self, entries, maxResults):  # noqa: N803
        self.history_calls.append({"entries": entries, "maxResults": maxResults})
        return {"successEntries": [{
            "entryId": e["entryId"],
            "assetPropertyValueHistory": [
                {"value": {"doubleValue": 1.0}, "timestamp": {"timeInSeconds": 10}, "quality": "GOOD"},
            ],
        } for e in entries], "errorEntries": []}


def _patch(monkeypatch, client, capture=None):
    def fake(service, cfg, default_region=None):
        if capture is not None:
            capture["service"] = service
            capture["config"] = cfg
            capture["default_region"] = default_region
        return client
    monkeypatch.setattr(aws_session, "boto3_client_for", fake)


def _series(out: dict) -> dict:
    """The per-series outcomes of a history result: the result's requested
    window (``HISTORY_WINDOW_KEY``) is a statement about the call, not a series."""
    assert sw.HISTORY_WINDOW_KEY in out
    return {k: v for k, v in out.items() if k != sw.HISTORY_WINDOW_KEY}


def test_list_asset_models_with_properties_enriches_each_model(monkeypatch):
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    out = sw.list_asset_models_with_properties({}, "us-east-1")
    assert [m["name"] for m in out] == ["evt_model", "metric_model"]
    evt = next(m for m in out if m["name"] == "evt_model")
    assert [p["name"] for p in evt["properties"]] == ["dur", "from_st", "to_st"]
    assert evt["properties"][0]["dataType"] == "DOUBLE"
    # describe_asset_model was called once per model.
    assert client.described_models == ["mdl-evt", "mdl-met"]


def test_list_asset_models_with_properties_respects_cap(monkeypatch):
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    out = sw.list_asset_models_with_properties({}, "us-east-1", max_models=1)
    assert len(out) == 1 and out[0]["name"] == "evt_model"


def test_describe_asset_model_returns_properties(monkeypatch):
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    detail = sw.describe_asset_model({}, "us-east-1", "mdl-met")
    assert detail["name"] == "metric_model"
    assert {p["name"] for p in detail["properties"]} == {"amt", "waste"}


def test_describe_asset_returns_properties(monkeypatch):
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    detail = sw.describe_asset({}, "us-east-1", "asset-1")
    assert detail["name"] == "Unit-1"
    assert [p["name"] for p in detail["properties"]] == ["dur", "from_st", "to_st"]
    assert detail["hierarchies"] == [{"id": "h1", "name": "children"}]


def test_list_assets_top_level_when_no_model(monkeypatch):
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    out = sw.list_assets({}, "us-east-1")
    assert client.paginate_kwargs == [{"filter": "TOP_LEVEL"}]
    assert out[0]["name"] == "Unit-1"


def test_list_assets_filters_by_model(monkeypatch):
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    sw.list_assets({}, "us-east-1", asset_model_id="mdl-evt")
    assert client.paginate_kwargs == [{"assetModelId": "mdl-evt"}]


def test_get_property_value_latest(monkeypatch):
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    out = sw.get_property_value({}, "us-east-1", "asset-1", "p-dur")
    assert out["value"] == 42.0 and out["timestamp"] == 111 and out["quality"] == "GOOD"


def test_get_property_history_builds_entries_and_maps_back(monkeypatch):
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    entries = [{"assetId": "asset-1", "propertyId": "p-dur"},
               {"assetId": "asset-1", "propertyId": "p-amt"}]
    out = sw.get_property_history({}, "us-east-1", entries, hours_back=4, max_results=50)
    # entryId is the short index; both entries were submitted.
    submitted = client.history_calls[0]["entries"]
    assert [e["entryId"] for e in submitted] == ["0", "1"]
    assert submitted[0]["timeOrdering"] == "ASCENDING"
    # Results keyed back by assetId_propertyId.
    assert "asset-1_p-dur" in out and "asset-1_p-amt" in out
    assert out["asset-1_p-dur"][0]["value"] == 1.0


def test_get_property_history_batch_error_is_structured(monkeypatch):
    class _Boom(_FakeSiteWise):
        def batch_get_asset_property_value_history(self, entries, maxResults):  # noqa: N803
            raise Exception("Throttling")
    client = _Boom()
    _patch(monkeypatch, client)
    out = sw.get_property_history({}, "us-east-1", [{"assetId": "a", "propertyId": "p"}])
    assert out["a_p"] == {
        "error": "Throttling",
        "error_type": "history_read_error",
    }


# --------------------------------------------------------------------------- #
# Absolute window (start_iso/end_iso) — the trailing-only surface could not
# address a specific past date at all; a day-targeted question read as
# missing data/retention (live 2026-08-04 misdiagnosis on present telemetry).
# --------------------------------------------------------------------------- #

_ENTRY = [{"assetId": "asset-1", "propertyId": "p-dur"}]


def test_history_absolute_window_reaches_batch_entries(monkeypatch):
    import datetime as dt
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    out = sw.get_property_history(
        {}, "us-east-1", _ENTRY,
        start_iso="2026-07-25T00:00:00Z", end_iso="2026-07-26T00:00:00Z")
    submitted = client.history_calls[0]["entries"][0]
    assert submitted["startDate"] == dt.datetime(2026, 7, 25, tzinfo=dt.UTC)
    assert submitted["endDate"] == dt.datetime(2026, 7, 26, tzinfo=dt.UTC)
    assert "asset-1_p-dur" in out


def test_history_absolute_window_ignores_hours_back(monkeypatch):
    import datetime as dt
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    # hours_back would be invalid (0) — the absolute window must win and the
    # bound must not even be evaluated.
    sw.get_property_history(
        {}, "us-east-1", _ENTRY, hours_back=0,
        start_iso="2026-07-25T06:00:00+00:00", end_iso="2026-07-25T18:00:00+00:00")
    submitted = client.history_calls[0]["entries"][0]
    assert submitted["startDate"] == dt.datetime(2026, 7, 25, 6, tzinfo=dt.UTC)


def test_history_start_only_defaults_end_to_now(monkeypatch):
    import datetime as dt
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    before = dt.datetime.now(dt.UTC)
    sw.get_property_history({}, "us-east-1", _ENTRY,
                            start_iso=before.isoformat())
    submitted = client.history_calls[0]["entries"][0]
    assert submitted["startDate"] == before
    assert before <= submitted["endDate"] <= dt.datetime.now(dt.UTC)


def test_history_naive_timestamps_treated_as_utc(monkeypatch):
    import datetime as dt
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    sw.get_property_history(
        {}, "us-east-1", _ENTRY,
        start_iso="2026-07-25T00:00:00", end_iso="2026-07-25T12:00:00")
    submitted = client.history_calls[0]["entries"][0]
    assert submitted["startDate"].tzinfo == dt.UTC
    assert submitted["startDate"] == dt.datetime(2026, 7, 25, tzinfo=dt.UTC)


def test_history_end_without_start_is_invalid(monkeypatch):
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    out = sw.get_property_history({}, "us-east-1", _ENTRY,
                                  end_iso="2026-07-26T00:00:00Z")
    assert out["error_type"] == "invalid_request"
    assert "start_iso" in out["error"]
    assert client.history_calls == []  # short-circuits before any API call


def test_history_unparseable_timestamp_is_invalid(monkeypatch):
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    out = sw.get_property_history({}, "us-east-1", _ENTRY,
                                  start_iso="last tuesday")
    assert out["error_type"] == "invalid_request"
    assert "ISO-8601" in out["error"]


def test_history_start_after_end_is_invalid(monkeypatch):
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    out = sw.get_property_history(
        {}, "us-east-1", _ENTRY,
        start_iso="2026-07-26T00:00:00Z", end_iso="2026-07-25T00:00:00Z")
    assert out["error_type"] == "invalid_request"
    assert "before" in out["error"]


def test_history_absolute_window_span_is_capped(monkeypatch):
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    out = sw.get_property_history(
        {}, "us-east-1", _ENTRY,
        start_iso="2020-01-01T00:00:00Z", end_iso="2026-07-25T00:00:00Z")
    assert out["error_type"] == "invalid_request"
    assert out["limit"] == sw.HISTORY_MAX_HOURS
    assert client.history_calls == []


def test_history_trailing_window_behavior_unchanged(monkeypatch):
    """No start/end ⇒ the pre-existing NOW-anchored hours_back window."""
    import datetime as dt
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    sw.get_property_history({}, "us-east-1", _ENTRY, hours_back=4)
    submitted = client.history_calls[0]["entries"][0]
    span = submitted["endDate"] - submitted["startDate"]
    assert span == dt.timedelta(hours=4)
    assert submitted["endDate"] <= dt.datetime.now(dt.UTC)


def test_cross_account_config_flows_to_helper(monkeypatch):
    client = _FakeSiteWise()
    captured = {}
    _patch(monkeypatch, client, capture=captured)
    cfg = {"role_arn": "arn:aws:iam::111122223333:role/sw", "external_id": "x", "region": "us-west-2"}
    sw.list_asset_models(cfg, "us-east-1")
    assert captured["service"] == "iotsitewise"
    assert captured["config"]["role_arn"] == "arn:aws:iam::111122223333:role/sw"
    assert captured["default_region"] == "us-east-1"


def test_get_property_history_chunks_and_preserves_mixed_success(monkeypatch):
    class _MixedHistory(_FakeSiteWise):
        def batch_get_asset_property_value_history(self, entries, maxResults, **kwargs):  # noqa: N803
            self.history_calls.append({"entries": entries, "maxResults": maxResults, **kwargs})
            if entries[0]["entryId"] == "16":
                raise RuntimeError("page unavailable")
            return {
                "successEntries": [{
                    "entryId": e["entryId"],
                    "assetPropertyValueHistory": [{
                        "value": {"doubleValue": 1.0},
                        "timestamp": {"timeInSeconds": 10},
                        "quality": "GOOD",
                    }],
                } for e in entries],
                "errorEntries": [],
            }

    client = _MixedHistory()
    _patch(monkeypatch, client)
    entries = [
        {"assetId": f"asset-{i}", "propertyId": "prop"}
        for i in range(18)
    ]
    out = sw.get_property_history({}, "us-east-1", entries)
    assert [len(call["entries"]) for call in client.history_calls] == [16, 2]
    assert out["asset-0_prop"][0]["value"] == 1.0
    assert out["asset-15_prop"][0]["value"] == 1.0
    assert out["asset-16_prop"] == {
        "error": "page unavailable", "error_type": "history_read_error"
    }
    assert out["asset-17_prop"] == {
        "error": "page unavailable", "error_type": "history_read_error"
    }


def test_get_property_history_paginates_and_merges_points(monkeypatch):
    class _PagedHistory(_FakeSiteWise):
        def batch_get_asset_property_value_history(
            self, entries, maxResults, nextToken=None  # noqa: N803
        ):
            self.history_calls.append({
                "entries": entries, "maxResults": maxResults, "nextToken": nextToken
            })
            point = 20 if nextToken else 10
            response = {
                "successEntries": [{
                    "entryId": entries[0]["entryId"],
                    "assetPropertyValueHistory": [{
                        "value": {"doubleValue": float(point)},
                        "timestamp": {"timeInSeconds": point},
                        "quality": "GOOD",
                    }],
                }],
                "errorEntries": [],
            }
            if nextToken is None:
                response["nextToken"] = "next-page"
            return response

    client = _PagedHistory()
    _patch(monkeypatch, client)
    out = sw.get_property_history(
        {}, "us-east-1", [{"assetId": "asset-a", "propertyId": "prop-a"}]
    )
    assert [call["nextToken"] for call in client.history_calls] == [None, "next-page"]
    assert [point["value"] for point in out["asset-a_prop-a"]] == [10.0, 20.0]


def test_describe_asset_error_message_actionable():
    msg = sw.describe_asset_error_message(
        "graph-node-id", Exception("ResourceNotFoundException: nope"))
    assert "graph-node-id" in msg and "do NOT guess" in msg


def test_describe_asset_error_message_passthrough():
    msg = sw.describe_asset_error_message("a", Exception("ThrottlingException"))
    assert msg == "ThrottlingException"


# --------------------------------------------------------------------------- #
# Adversarial history bounds and partial-result honesty
# --------------------------------------------------------------------------- #
def test_history_rejects_more_than_entry_cap_before_client_call(monkeypatch):
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    entries = [
        {"assetId": f"asset-{i}", "propertyId": "prop"}
        for i in range(sw.HISTORY_MAX_ENTRIES + 1)
    ]
    out = sw.get_property_history({}, "us-east-1", entries)
    assert out["error_type"] == "invalid_request"
    assert out["limit"] == sw.HISTORY_MAX_ENTRIES
    assert client.history_calls == []


def test_history_deduplicates_identical_entries(monkeypatch):
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    entry = {"assetId": "asset-1", "propertyId": "prop-1"}
    out = sw.get_property_history({}, "us-east-1", [entry, dict(entry), dict(entry)])
    assert len(client.history_calls) == 1
    assert len(client.history_calls[0]["entries"]) == 1
    assert list(_series(out)) == ["asset-1_prop-1"]


def test_history_rejects_oversized_or_missing_identifiers(monkeypatch):
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    too_long = "x" * (sw.SITEWISE_IDENTIFIER_MAX_CHARS + 1)
    for entries in (
        [{"assetId": too_long, "propertyId": "p"}],
        [{"assetId": "a", "propertyId": too_long}],
        [{"assetId": "a"}],
        "not-a-list",
    ):
        out = sw.get_property_history({}, "us-east-1", entries)
        assert out["error_type"] == "invalid_request"
    assert client.history_calls == []


def test_history_rejects_out_of_range_hours_and_results_before_client_call(monkeypatch):
    client = _FakeSiteWise()
    _patch(monkeypatch, client)
    entries = [{"assetId": "a", "propertyId": "p"}]
    for kwargs, field in (
        ({"hours_back": 10**9}, "hours_back"),
        ({"max_results": 10**9}, "max_results"),
    ):
        out = sw.get_property_history({}, "us-east-1", entries, **kwargs)
        assert out["error_type"] == "invalid_request"
        assert field in out["error"]
    assert client.history_calls == []


def test_history_repeated_token_terminates_with_partial_metadata(monkeypatch):
    class _Repeated(_FakeSiteWise):
        def batch_get_asset_property_value_history(self, entries, maxResults, nextToken=None):  # noqa: N803
            self.history_calls.append({"entries": entries, "maxResults": maxResults,
                                       "nextToken": nextToken})
            return {
                "successEntries": [{
                    "entryId": entries[0]["entryId"],
                    "assetPropertyValueHistory": [{
                        "value": {"doubleValue": 1.0},
                        "timestamp": {"timeInSeconds": len(self.history_calls)},
                        "quality": "GOOD",
                    }],
                }],
                "nextToken": "same-token",
            }

    client = _Repeated()
    _patch(monkeypatch, client)
    out = sw.get_property_history({}, "us-east-1", [{"assetId": "a", "propertyId": "p"}])
    assert len(client.history_calls) == 2
    item = out["a_p"]
    assert item["status"] == "partial"
    assert item["truncated"] is True
    assert item["reason"] == "repeated_token"
    assert item["points"]


def test_history_continuation_failure_after_success_is_partial(monkeypatch):
    class _ContinuationFailure(_FakeSiteWise):
        def batch_get_asset_property_value_history(self, entries, maxResults, nextToken=None):  # noqa: N803
            self.history_calls.append({"entries": entries, "maxResults": maxResults,
                                       "nextToken": nextToken})
            if nextToken:
                raise RuntimeError("Authorization: secret continuation failed")
            return {
                "successEntries": [{
                    "entryId": entries[0]["entryId"],
                    "assetPropertyValueHistory": [{
                        "value": {"doubleValue": 2.0},
                        "timestamp": {"timeInSeconds": 1},
                        "quality": "GOOD",
                    }],
                }],
                "nextToken": "next",
            }

    client = _ContinuationFailure()
    _patch(monkeypatch, client)
    out = sw.get_property_history({}, "us-east-1", [{"assetId": "a", "propertyId": "p"}])
    item = out["a_p"]
    assert item["status"] == "partial"
    assert item["truncated"] is True
    assert item["reason"] == "continuation_error"
    assert item["points"][0]["value"] == 2.0
    assert "Authorization" not in str(item)


def test_history_token_remaining_at_page_cap_is_partial(monkeypatch):
    class _AlwaysMore(_FakeSiteWise):
        def batch_get_asset_property_value_history(self, entries, maxResults, nextToken=None):  # noqa: N803
            self.history_calls.append({"entries": entries, "maxResults": maxResults,
                                       "nextToken": nextToken})
            n = len(self.history_calls)
            return {
                "successEntries": [{
                    "entryId": entries[0]["entryId"],
                    "assetPropertyValueHistory": [{
                        "value": {"doubleValue": float(n)},
                        "timestamp": {"timeInSeconds": n},
                        "quality": "GOOD",
                    }],
                }],
                "nextToken": f"token-{n}",
            }

    client = _AlwaysMore()
    _patch(monkeypatch, client)
    out = sw.get_property_history({}, "us-east-1", [{"assetId": "a", "propertyId": "p"}])
    assert len(client.history_calls) <= sw.HISTORY_MAX_PAGES_PER_CHUNK
    item = out["a_p"]
    assert item["status"] == "partial"
    assert item["reason"] in {"page_limit", "call_limit"}


def test_history_huge_page_is_incrementally_bounded_and_marked_truncated(monkeypatch):
    # Shrink the byte budget so the fake stays small (memory) while still
    # proving the per-chunk cap trips and the outcome is honest + bounded.
    monkeypatch.setattr(sw, "HISTORY_MAX_RESPONSE_BYTES", 65_536)

    class _Huge(_FakeSiteWise):
        def batch_get_asset_property_value_history(self, entries, maxResults, **kwargs):  # noqa: N803
            self.history_calls.append({"entries": entries, "maxResults": maxResults, **kwargs})
            points = [{
                "value": {"stringValue": "sensitive@example.com " + "x" * 4096},
                "timestamp": {"timeInSeconds": i},
                "quality": "GOOD",
            } for i in range(sw.HISTORY_MAX_RESPONSE_BYTES // 1024 + 500)]
            return {"successEntries": [{
                "entryId": entries[0]["entryId"],
                "assetPropertyValueHistory": points,
            }]}

    client = _Huge()
    _patch(monkeypatch, client)
    out = sw.get_property_history({}, "us-east-1", [{"assetId": "a", "propertyId": "p"}])
    item = out["a_p"]
    assert item["status"] == "partial"
    assert item["truncated"] is True
    assert item["reason"] in {"raw_point_limit", "response_size_limit"}
    assert len(item["points"]) <= sw.HISTORY_DOWNSAMPLE_POINTS
    assert len(str(out)) <= sw.HISTORY_MAX_RESPONSE_BYTES * 2


def test_history_default_128_requests_return_exactly_all_requested_keys(monkeypatch):
    """Even empty API pages must produce one terminal outcome per deduplicated key."""
    class _EmptyPages(_FakeSiteWise):
        def batch_get_asset_property_value_history(self, entries, maxResults, **kwargs):  # noqa: N803
            self.history_calls.append({"entries": entries, "maxResults": maxResults, **kwargs})
            return {"successEntries": [], "errorEntries": [], "skippedEntries": []}

    client = _EmptyPages()
    _patch(monkeypatch, client)
    entries = [
        {"assetId": f"asset-{i}", "propertyId": f"property-{i}"}
        for i in range(128)
    ]
    expected = {f"asset-{i}_property-{i}" for i in range(128)}
    out = sw.get_property_history({}, "us-east-1", entries)
    assert set(_series(out)) == expected
    assert len(_series(out)) == len(expected)
    assert all(value.get("error_type") == "missing_entry" for value in _series(out).values())


def test_history_missing_response_entry_gets_typed_terminal_error(monkeypatch):
    class _MissingOne(_FakeSiteWise):
        def batch_get_asset_property_value_history(self, entries, maxResults, **kwargs):  # noqa: N803
            return {
                "successEntries": [{
                    "entryId": entries[0]["entryId"],
                    "assetPropertyValueHistory": [],
                }],
            }

    _patch(monkeypatch, _MissingOne())
    out = sw.get_property_history({}, "us-east-1", [
        {"assetId": "a", "propertyId": "p"},
        {"assetId": "b", "propertyId": "q"},
    ])
    assert out["a_p"] == []
    assert out["b_q"]["error_type"] == "missing_entry"
    assert set(_series(out)) == {"a_p", "b_q"}


def test_history_empty_continuation_marks_every_chunk_key_partial(monkeypatch):
    class _EmptyContinuation(_FakeSiteWise):
        def batch_get_asset_property_value_history(self, entries, maxResults, nextToken=None):  # noqa: N803
            if nextToken:
                return {"successEntries": [], "errorEntries": [], "skippedEntries": []}
            return {
                "successEntries": [{
                    "entryId": entries[0]["entryId"],
                    "assetPropertyValueHistory": [],
                }],
                "nextToken": "more",
            }

    _patch(monkeypatch, _EmptyContinuation())
    out = sw.get_property_history({}, "us-east-1", [
        {"assetId": "a", "propertyId": "p"},
        {"assetId": "b", "propertyId": "q"},
    ])
    assert set(_series(out)) == {"a_p", "b_q"}
    assert all(item["status"] == "partial" for item in _series(out).values())
    assert all(item["reason"] == "empty_continuation" for item in _series(out).values())


def test_history_global_call_cap_covers_current_and_remaining_chunks(monkeypatch):
    monkeypatch.setattr(sw, "HISTORY_MAX_TOTAL_CALLS", 1)

    class _AlwaysMore(_FakeSiteWise):
        def batch_get_asset_property_value_history(self, entries, maxResults, **kwargs):  # noqa: N803
            return {"successEntries": [], "nextToken": "more"}

    _patch(monkeypatch, _AlwaysMore())
    entries = [
        {"assetId": f"a-{i}", "propertyId": f"p-{i}"}
        for i in range(18)
    ]
    out = sw.get_property_history({}, "us-east-1", entries)
    assert len(_series(out)) == 18
    assert set(_series(out)) == {f"a-{i}_p-{i}" for i in range(18)}
    assert all(item["status"] == "partial" for item in _series(out).values())
    assert all(item["reason"] == "call_limit" for item in _series(out).values())


# --------------------------------------------------------------------------- #
# Pagination-token cap (measured live): a real
# BatchGetAssetPropertyValueHistory nextToken is 1202 chars, so sharing the
# 256-char IDENTIFIER cap classified every continuation as "invalid_token" and
# truncated every multi-page history read to page 1 — evidence loss that stayed
# invisible until annotate audited grounding (ADR-0047).
# --------------------------------------------------------------------------- #
def test_service_minted_pagination_token_is_not_capped_as_an_identifier():
    token = "x" * 1202          # the length measured against real SiteWise
    assert len(token) > sw.SITEWISE_IDENTIFIER_MAX_CHARS
    got, err = sw._next_history_token(
        {"nextToken": token}, set(), 1, continuation=False, recognized=1)
    assert err == "", "a 1202-char service token must page, not abort"
    assert got == token


def test_pagination_token_is_still_bounded():
    """Bounded, just at a size the service actually produces (it is echoed
    into ``seen_tokens``, so unbounded growth stays refused)."""
    got, err = sw._next_history_token(
        {"nextToken": "x" * (sw.SITEWISE_PAGINATION_TOKEN_MAX_CHARS + 1)},
        set(), 1, continuation=False, recognized=1)
    assert (got, err) == ("", "invalid_token")


def test_identifier_cap_is_unchanged_for_user_supplied_values():
    """The identifier cap protects USER-supplied ids and must not be relaxed."""
    assert sw.SITEWISE_IDENTIFIER_MAX_CHARS == 256
    assert sw.SITEWISE_PAGINATION_TOKEN_MAX_CHARS > 1202


# --------------------------------------------------------------------------- #
# Prompt-seeding asset samples: the seeded inventory named MODELS and
# properties but never which ASSETS instantiate them, so for estates where
# event-shaped models live on CHILD component assets the agent guessed the
# machine asset, hit property-not-found, and burned its bounded turn budget.
# --------------------------------------------------------------------------- #
def test_sample_asset_names_is_bounded_and_names_only(monkeypatch):
    calls = []

    def fake_list_assets(cfg, region=None, asset_model_id=""):
        calls.append(asset_model_id)
        return [{"id": f"id-{i}", "name": f"Asset_{i}_State"} for i in range(9)]

    monkeypatch.setattr(sw, "list_assets", fake_list_assets)
    got = sw.sample_asset_names_by_model({}, "us-east-1", ["m-1"], per_model=4)
    assert got["m-1"]["names"] == [f"Asset_{i}_State" for i in range(4)]
    assert got["m-1"]["total"] == 9          # honest total, bounded sample
    assert calls == ["m-1"], "one list_assets per model"
    flat = str(got)
    assert "id-0" not in flat, "ids must never enter the prompt seed"


def test_sample_asset_names_omits_a_model_it_cannot_read(monkeypatch):
    """Absence stays absence — a failed sample must not fabricate names."""
    def boom(cfg, region=None, asset_model_id=""):
        if asset_model_id == "bad":
            raise RuntimeError("AccessDenied")
        return [{"id": "a", "name": "Good_1"}]

    monkeypatch.setattr(sw, "list_assets", boom)
    got = sw.sample_asset_names_by_model({}, "us-east-1", ["bad", "ok"])
    assert "bad" not in got and got["ok"]["names"] == ["Good_1"]


def test_sample_asset_names_skips_blank_model_ids(monkeypatch):
    monkeypatch.setattr(sw, "list_assets",
                        lambda *a, **k: [{"id": "x", "name": "N"}])
    assert sw.sample_asset_names_by_model({}, "us-east-1", ["", None]) == {}


# ---------------------------------------------------------------------------
# ISO-8601 ``time`` on every value that reaches the model (rehearsal defect:
# a raw ``timeInSeconds`` epoch was mis-converted by the LLM into a
# future-dated point). ``timestamp`` stays numeric for downstream consumers.
# ---------------------------------------------------------------------------

_EPOCH = 1_700_000_000
_EPOCH_ISO = "2023-11-14T22:13:20+00:00"


def test_latest_value_carries_iso_utc_time(monkeypatch):
    class _Latest(_FakeSiteWise):
        def get_asset_property_value(self, assetId, propertyId):  # noqa: N803
            return {"propertyValue": {"value": {"doubleValue": 1.5},
                                      "timestamp": {"timeInSeconds": _EPOCH},
                                      "quality": "GOOD"}}

    _patch(monkeypatch, _Latest())
    out = sw.get_property_value({}, "us-east-1", "asset-1", "p-dur")
    assert out == {"value": 1.5, "timestamp": _EPOCH, "time": _EPOCH_ISO,
                   "quality": "GOOD"}


def test_latest_value_without_timestamp_has_null_time():
    out = sw._parse_property_value({"value": {"doubleValue": 2.0}})
    assert out["timestamp"] is None and out["time"] is None


def test_history_points_carry_iso_utc_time(monkeypatch):
    class _One(_FakeSiteWise):
        def batch_get_asset_property_value_history(self, entries, maxResults, **kwargs):  # noqa: N803
            return {"successEntries": [{
                "entryId": entries[0]["entryId"],
                "assetPropertyValueHistory": [{
                    "value": {"doubleValue": 3.0},
                    "timestamp": {"timeInSeconds": _EPOCH}, "quality": "GOOD"}],
            }]}

    _patch(monkeypatch, _One())
    out = sw.get_property_history({}, "us-east-1", [{"assetId": "a", "propertyId": "p"}])
    assert out["a_p"] == [{"value": 3.0, "timestamp": _EPOCH, "time": _EPOCH_ISO,
                           "quality": "GOOD"}]


def test_history_time_is_added_after_downsampling_on_every_kept_point(monkeypatch):
    """Downsampling selects by numeric stamp; every KEPT point (incl. the
    restored newest point) then carries the matching ISO ``time``."""
    n = 2000

    class _Many(_FakeSiteWise):
        def batch_get_asset_property_value_history(self, entries, maxResults, **kwargs):  # noqa: N803
            return {"successEntries": [{
                "entryId": entries[0]["entryId"],
                "assetPropertyValueHistory": [{
                    "value": {"doubleValue": float(i)},
                    "timestamp": {"timeInSeconds": _EPOCH + 60 * i},
                    "quality": "GOOD"} for i in range(n)],
            }]}

    _patch(monkeypatch, _Many())
    points = sw.get_property_history(
        {}, "us-east-1", [{"assetId": "a", "propertyId": "p"}])["a_p"]
    assert 2 < len(points) <= sw.HISTORY_DOWNSAMPLE_POINTS
    assert points[0]["timestamp"] == _EPOCH
    assert points[-1]["timestamp"] == _EPOCH + 60 * (n - 1)
    for point in points:
        assert isinstance(point["timestamp"], int)
        assert point["time"] == sw._iso_from_seconds(point["timestamp"])


def test_downsampler_is_unchanged_by_the_time_field():
    """``_downsample_points`` still sees numeric stamps only (time is
    stamped on the finalized selection, never before)."""
    raw = [{"value": i, "timestamp": i, "quality": "GOOD"} for i in range(500)]
    state = sw._HistoryReadState()
    for point in raw:
        sw._append_bounded_point(state, "k", point)
    covered = state.covered_points("k")
    expected = sw._downsample_points(
        list(state.histories["k"]) + ([raw[-1]] if state.histories["k"][-1] is not raw[-1] else []))
    assert [p["timestamp"] for p in covered] == [p["timestamp"] for p in expected]
    assert all("time" not in p for p in raw), "raw accumulator points are not mutated"


def test_partial_history_points_carry_iso_time(monkeypatch):
    class _Repeat(_FakeSiteWise):
        def batch_get_asset_property_value_history(self, entries, maxResults, **kwargs):  # noqa: N803
            return {"successEntries": [{
                "entryId": entries[0]["entryId"],
                "assetPropertyValueHistory": [{
                    "value": {"doubleValue": 1.0},
                    "timestamp": {"timeInSeconds": _EPOCH}, "quality": "GOOD"}],
            }], "nextToken": "same"}

    _patch(monkeypatch, _Repeat())
    item = sw.get_property_history(
        {}, "us-east-1", [{"assetId": "a", "propertyId": "p"}])["a_p"]
    assert item["status"] == "partial"
    assert item["covered_start"] == _EPOCH_ISO
    assert all(p["time"] == _EPOCH_ISO for p in item["points"])


def test_history_with_iso_time_still_claim_check_caps():
    """Budget math holds: a Direct-shaped series WITH ``time`` longer than the
    cap is still replaced by stats + landmarks (never the raw series)."""
    import agents.system_agent as sa

    series = [{"value": float(i), "timestamp": _EPOCH + i,
               "time": sw._iso_from_seconds(_EPOCH + i), "quality": "GOOD"}
              for i in range(sw.HISTORY_DOWNSAMPLE_POINTS)]
    capped = sa._cap_history_payload({"a_p": series})
    assert set(capped["a_p"]) == {"_stats", "dataset_ref", "landmark_values"}
    assert len(capped["a_p"]["landmark_values"]) <= 8


def test_iso_from_seconds_is_total():
    assert sw._iso_from_seconds(_EPOCH) == _EPOCH_ISO
    assert sw._iso_from_seconds(None) is None
    assert sw._iso_from_seconds(True) is None
    assert sw._iso_from_seconds(10 ** 20) is None, "out-of-range stamp degrades, never raises"
