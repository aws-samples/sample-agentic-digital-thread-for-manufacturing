# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for lambda-sitewise-feed/handler.py (workshop OT feed).

Offline — a fake SiteWise client stands in for boto3 (which the handler
only imports inside the entrypoint). Covers:

- series generator: window coverage, strict ordering, grid alignment,
  the 1.8 -> 2.8 mm/s degrading shape, determinism (the idempotency
  contract), and the loud >7-day guard;
- batch chunking: BatchPutAssetPropertyValue limits (10 entries/request,
  10 values/entry) with zero value loss;
- name->id resolution: model-missing fail-loud path, asset creation when
  absent (the no-HighByte data path), found-by-name reuse, and the
  missing-property fail-loud path;
- publish: per-entry errorEntries surfaced as raised errors, never
  silently dropped.
"""

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HANDLER_PATH = _REPO_ROOT / "lambda-sitewise-feed" / "handler.py"

_spec = importlib.util.spec_from_file_location("sitewise_feed_handler", _HANDLER_PATH)
feed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(feed)

END_EPOCH = 1_770_000_000  # fixed anchor: deterministic tests


# ---------------------------------------------------------------------------
# Fake SiteWise client
# ---------------------------------------------------------------------------

class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return iter(self._pages)


class FakeSitewiseClient:
    """Minimal iotsitewise stand-in for the resolution + publish paths."""

    def __init__(self, models=None, assets=None, asset_properties=None,
                 error_entries=None, create_failed_state=False):
        # models: list of {"name", "id"}; assets: {model_id: [{"name","id"}]}
        self.models = models if models is not None else [
            {"name": "CNC", "id": "model-cnc-1"},
            {"name": "Oven", "id": "model-oven-1"},
        ]
        self.assets = assets if assets is not None else {}
        self.asset_properties = asset_properties  # None -> full CNC schema
        self.error_entries = error_entries or []
        self.create_failed_state = create_failed_state
        self.created_assets = []
        self.put_requests = []

    def get_paginator(self, op):
        if op == "list_asset_models":
            return _FakePaginator([{"assetModelSummaries": self.models}])
        if op == "list_assets":
            class _AssetPaginator:
                def __init__(inner, outer):
                    inner._outer = outer

                def paginate(inner, assetModelId):
                    return iter([{"assetSummaries": inner._outer.assets.get(assetModelId, [])}])
            return _AssetPaginator(self)
        raise AssertionError(f"unexpected paginator {op}")

    def create_asset(self, assetName, assetModelId):
        asset_id = f"created-{assetName}"
        self.created_assets.append((assetName, assetModelId))
        self.assets.setdefault(assetModelId, []).append({"name": assetName, "id": asset_id})
        return {"assetId": asset_id}

    def describe_asset(self, assetId):
        state = "FAILED" if self.create_failed_state else "ACTIVE"
        if self.asset_properties is not None:
            props = self.asset_properties
        else:
            names = list(feed.VIBRATION_PROPERTIES) + [feed.TEMP_PROPERTY]
            props = [{"name": n, "id": f"prop-{n}"} for n in names]
        return {"assetStatus": {"state": state, "error": {"code": "InternalFailure"}},
                "assetProperties": props}

    def batch_put_asset_property_value(self, entries):
        self.put_requests.append(entries)
        return {"errorEntries": self.error_entries}


# ---------------------------------------------------------------------------
# Series generator
# ---------------------------------------------------------------------------

class TestGenerateSeries:
    def test_properties_present(self):
        series = feed.generate_series(END_EPOCH)
        assert set(series) == set(feed.VIBRATION_PROPERTIES) | {feed.TEMP_PROPERTY}

    def test_backfill_window_coverage(self):
        series = feed.generate_series(END_EPOCH, hours=72)
        points = series["Vibration_X"]
        assert len(points) == 72 * 3600 // feed.INTERVAL_SECONDS  # 864
        span = points[-1][0] - points[0][0]
        assert span == 72 * 3600 - feed.INTERVAL_SECONDS

    def test_timestamps_strictly_ascending_and_grid_aligned(self):
        for points in feed.generate_series(END_EPOCH).values():
            timestamps = [ts for ts, _ in points]
            assert timestamps == sorted(set(timestamps))
            assert all(ts % feed.INTERVAL_SECONDS == 0 for ts in timestamps)
            diffs = {b - a for a, b in zip(timestamps, timestamps[1:])}
            assert diffs == {feed.INTERVAL_SECONDS}

    def test_degrading_shape_climbs_to_2_8(self):
        points = feed.generate_series(END_EPOCH)["Vibration_X"]
        first, last = points[0][1], points[-1][1]
        assert first == pytest.approx(1.8, abs=0.1)
        assert last == pytest.approx(2.8, abs=0.1)
        # Trend, not noise: averaged quarters must be monotonically increasing.
        values = [v for _, v in points]
        q = len(values) // 4
        quarter_means = [sum(values[i * q:(i + 1) * q]) / q for i in range(4)]
        assert quarter_means == sorted(quarter_means)

    def test_axis_scaling_y_z_below_x(self):
        series = feed.generate_series(END_EPOCH)
        assert series["Vibration_Y"][-1][1] < series["Vibration_X"][-1][1]
        assert series["Vibration_Z"][-1][1] < series["Vibration_Y"][-1][1]

    def test_temperature_climbs(self):
        points = feed.generate_series(END_EPOCH)[feed.TEMP_PROPERTY]
        assert points[0][1] == pytest.approx(68.0, abs=1.0)
        assert points[-1][1] == pytest.approx(84.0, abs=1.0)

    def test_deterministic_idempotency_contract(self):
        # Same anchor -> byte-identical series; a re-run overwrites, never forks.
        assert feed.generate_series(END_EPOCH) == feed.generate_series(END_EPOCH)

    def test_grid_alignment_makes_offset_invocations_overlap(self):
        # A second click 90s later reuses the SAME grid timestamps (upsert).
        a = feed.generate_series(END_EPOCH)["Vibration_X"]
        b = feed.generate_series(END_EPOCH + 90)["Vibration_X"]
        overlap = set(dict(a)) & set(dict(b))
        assert overlap
        for ts in overlap:
            assert dict(a)[ts] == dict(b)[ts]

    def test_rerun_in_a_later_bucket_reanchors_the_ramp_and_extends(self):
        # The idempotency is NARROW (handler docstring): a re-run one bucket
        # later shares timestamps but rewrites them with a re-anchored ramp,
        # and adds a newer point that carries the peak.
        a = dict(feed.generate_series(END_EPOCH)["Vibration_X"])
        b = dict(feed.generate_series(END_EPOCH + feed.INTERVAL_SECONDS)["Vibration_X"])
        overlap = set(a) & set(b)
        assert overlap and max(b) > max(a)
        assert any(a[ts] != b[ts] for ts in overlap)
        assert "re-anchored ramp" in feed.__doc__

    def test_backfill_hours_over_limit_fails_loud(self):
        with pytest.raises(feed.SitewiseFeedError, match="7 days"):
            feed.generate_series(END_EPOCH, hours=200)

    def test_backfill_hours_zero_fails_loud(self):
        with pytest.raises(feed.SitewiseFeedError):
            feed.generate_series(END_EPOCH, hours=0)


# ---------------------------------------------------------------------------
# Batch chunking
# ---------------------------------------------------------------------------

class TestBuildBatches:
    def _prop_ids(self, series):
        return {name: f"prop-{name}" for name in series}

    def test_api_limits_respected(self):
        series = feed.generate_series(END_EPOCH)
        batches = feed.build_batches("asset-1", self._prop_ids(series), series)
        for batch in batches:
            assert len(batch) <= feed.MAX_ENTRIES_PER_REQUEST
            for entry in batch:
                assert len(entry["propertyValues"]) <= feed.MAX_VALUES_PER_ENTRY

    def test_no_value_lost_or_duplicated(self):
        series = feed.generate_series(END_EPOCH)
        batches = feed.build_batches("asset-1", self._prop_ids(series), series)
        total = sum(len(e["propertyValues"]) for b in batches for e in b)
        assert total == sum(len(points) for points in series.values())
        entry_ids = [e["entryId"] for b in batches for e in b]
        assert len(entry_ids) == len(set(entry_ids))

    def test_values_ascending_within_entry(self):
        series = feed.generate_series(END_EPOCH)
        batches = feed.build_batches("asset-1", self._prop_ids(series), series)
        for batch in batches:
            for entry in batch:
                ts = [v["timestamp"]["timeInSeconds"] for v in entry["propertyValues"]]
                assert ts == sorted(ts)

    def test_tvq_shape(self):
        series = {"Vibration_X": [(600, 1.8)]}
        [batch] = feed.build_batches("asset-1", {"Vibration_X": "p1"}, series)
        [entry] = batch
        assert entry["assetId"] == "asset-1"
        assert entry["propertyId"] == "p1"
        assert entry["propertyValues"] == [
            {"value": {"doubleValue": 1.8},
             "timestamp": {"timeInSeconds": 600},
             "quality": "GOOD"}
        ]


# ---------------------------------------------------------------------------
# Name -> id resolution
# ---------------------------------------------------------------------------

class TestEnsureAsset:
    def test_model_missing_fails_loud(self):
        client = FakeSitewiseClient(models=[{"name": "Oven", "id": "m1"}])
        with pytest.raises(feed.SitewiseFeedError, match="DeploySitewiseModels"):
            feed.ensure_asset(client)
        assert client.created_assets == []

    def test_asset_missing_is_created_from_model(self):
        client = FakeSitewiseClient()  # CNC model exists, no assets
        target = feed.ensure_asset(client)
        assert target["created"] is True
        assert client.created_assets == [(feed.ASSET_NAME, "model-cnc-1")]
        assert target["asset_id"] == f"created-{feed.ASSET_NAME}"

    def test_existing_asset_reused_never_recreated(self):
        client = FakeSitewiseClient(
            assets={"model-cnc-1": [{"name": feed.ASSET_NAME, "id": "asset-existing"}]}
        )
        target = feed.ensure_asset(client)
        assert target["created"] is False
        assert target["asset_id"] == "asset-existing"
        assert client.created_assets == []

    def test_property_ids_resolved_by_name(self):
        client = FakeSitewiseClient(
            assets={"model-cnc-1": [{"name": feed.ASSET_NAME, "id": "asset-1"}]}
        )
        target = feed.ensure_asset(client)
        assert target["properties"]["Vibration_X"] == "prop-Vibration_X"
        assert target["properties"][feed.TEMP_PROPERTY] == f"prop-{feed.TEMP_PROPERTY}"

    def test_missing_property_fails_loud_listing_available(self):
        client = FakeSitewiseClient(
            assets={"model-cnc-1": [{"name": feed.ASSET_NAME, "id": "asset-1"}]},
            asset_properties=[{"name": "SpindleSpeed", "id": "p9"}],
        )
        with pytest.raises(feed.SitewiseFeedError, match="Vibration_X") as exc:
            feed.ensure_asset(client)
        assert "SpindleSpeed" in str(exc.value)

    def test_failed_asset_state_fails_loud(self):
        client = FakeSitewiseClient(create_failed_state=True)
        with pytest.raises(feed.SitewiseFeedError, match="FAILED"):
            feed.ensure_asset(client)


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------

class TestPublishSeries:
    def test_success_counts_all_values(self):
        series = feed.generate_series(END_EPOCH, hours=2)
        prop_ids = {name: f"prop-{name}" for name in series}
        client = FakeSitewiseClient()
        written = feed.publish_series(client, "asset-1", prop_ids, series)
        assert written == sum(len(p) for p in series.values())
        for request in client.put_requests:
            assert len(request) <= feed.MAX_ENTRIES_PER_REQUEST

    def test_error_entries_raise_with_context(self):
        series = {"Vibration_X": [(600, 1.8)]}
        client = FakeSitewiseClient(error_entries=[
            {"entryId": "Vibration_X-600",
             "errors": [{"errorCode": "AccessDeniedException", "errorMessage": "nope"}]}
        ])
        with pytest.raises(feed.SitewiseFeedError, match="AccessDeniedException") as exc:
            feed.publish_series(client, "asset-1", {"Vibration_X": "p1"}, series)
        assert "Vibration_X-600" in str(exc.value)

    def test_calls_are_serial_single_client_loop(self):
        # Structural pin: publish_series drives ONE client, batch by batch —
        # no executor/fan-out (telemetry-serial invariant).
        import inspect
        src = inspect.getsource(feed.publish_series)
        assert "ThreadPool" not in src and "concurrent" not in src


# ---------------------------------------------------------------------------
# Per-data-stream throttling (test event 13, 2026-09-14: ThrottlingException
# after ~500 values when entries were grouped by property, 10 per request)
# ---------------------------------------------------------------------------
class TestThrottlePacing:
    def test_requests_interleave_streams(self):
        series = feed.generate_series(END_EPOCH, hours=6)
        prop_ids = {name: f"prop-{name}" for name in series}
        batches = feed.build_batches("asset-1", prop_ids, series)
        streams = len(series)
        cap = -(-feed.MAX_ENTRIES_PER_REQUEST // streams)  # ceil
        for batch in batches:
            per_prop = {}
            for e in batch:
                per_prop[e["propertyId"]] = per_prop.get(e["propertyId"], 0) + 1
            assert max(per_prop.values()) <= cap, per_prop
        # No value lost or duplicated by the interleave.
        ids = [e["entryId"] for b in batches for e in b]
        assert len(ids) == len(set(ids))
        assert sum(len(e["propertyValues"]) for b in batches for e in b) == \
            sum(len(p) for p in series.values())

    def test_publish_paces_requests(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(feed.time, "sleep", lambda s: sleeps.append(s))
        series = feed.generate_series(END_EPOCH, hours=2)
        prop_ids = {name: f"prop-{name}" for name in series}
        client = FakeSitewiseClient()
        feed.publish_series(client, "asset-1", prop_ids, series)
        assert len(sleeps) == len(client.put_requests)
        assert all(s == feed.MIN_SECONDS_BETWEEN_REQUESTS for s in sleeps)

    def test_throttled_entries_are_retried_not_dropped(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(feed.time, "sleep", lambda s: sleeps.append(s))
        series = {"Vibration_X": [(600, 1.8), (900, 1.9)], "Coolant_Temp": [(600, 68.0)]}
        client = FakeSitewiseClient()
        calls = {"n": 0}

        def flaky(entries):
            client.put_requests.append(entries)
            calls["n"] += 1
            if calls["n"] == 1:  # throttle the vibration entry once
                return {"errorEntries": [{"entryId": "Vibration_X-600", "errors": [
                    {"errorCode": feed.THROTTLE_ERROR_CODE, "errorMessage": "rate"}]}]}
            return {"errorEntries": []}
        client.batch_put_asset_property_value = flaky
        prop_ids = {"Vibration_X": "p1", "Coolant_Temp": "p2"}
        written = feed.publish_series(client, "asset-1", prop_ids, series)
        assert written == 3
        assert [e["entryId"] for e in client.put_requests[1]] == ["Vibration_X-600"]
        assert feed.THROTTLE_BACKOFF_SECONDS in sleeps

    def test_persistent_throttling_fails_loud(self, monkeypatch):
        monkeypatch.setattr(feed.time, "sleep", lambda s: None)
        series = {"Vibration_X": [(600, 1.8)]}
        client = FakeSitewiseClient(error_entries=[
            {"entryId": "Vibration_X-600",
             "errors": [{"errorCode": feed.THROTTLE_ERROR_CODE, "errorMessage": "rate"}]}])
        with pytest.raises(feed.SitewiseFeedError, match="kept throttling"):
            feed.publish_series(client, "asset-1", {"Vibration_X": "p1"}, series)
        assert len(client.put_requests) == feed.THROTTLE_RETRY_ATTEMPTS + 1
