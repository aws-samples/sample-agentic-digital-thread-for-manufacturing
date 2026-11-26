# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for ``sitewise_read.batch_get_property_values`` — the
event-loop fix's batch latest-value seam (replaces N sequential
``GetAssetPropertyValue`` round-trips with paged ``BatchGetAssetPropertyValue``).

Fully synthetic: the ``iotsitewise`` client is a stub (no AWS, no network).
Pins the diagnosed contract:

  • paging: 39 entries → ceil(39/16) = 3 batch calls of 16/16/7;
  • entryId → ``"<assetId>_<propertyId>"`` key mapping (order-independent);
  • per-entry error TOLERANCE: an errorEntry/skippedEntry — or a whole failed
    page — yields a per-key ``{"error": ...}`` and never sinks the batch;
  • nextToken continuation within one page's request.
"""
from __future__ import annotations

import agents.aws_session as aws_session
import agents.sitewise_read as sw


def _ok_entry(entry_id: str, value: str) -> dict:
    return {
        "entryId": entry_id,
        "assetPropertyValue": {
            "value": {"stringValue": value},
            "timestamp": {"timeInSeconds": 100},
            "quality": "GOOD",
        },
    }


class _FakeBatchSiteWise:
    """iotsitewise double for BatchGetAssetPropertyValue; per-call responder."""

    def __init__(self, responder=None):
        self.calls: list[dict] = []

        def _default(kwargs):
            return {"successEntries": [
                _ok_entry(e["entryId"], f"v-{e['assetId']}-{e['propertyId']}")
                for e in kwargs["entries"]
            ]}

        self._responder = responder or _default

    def batch_get_asset_property_value(self, **kwargs):
        self.calls.append(kwargs)
        return self._responder(kwargs)


def _patch(monkeypatch, client):
    monkeypatch.setattr(
        aws_session, "boto3_client_for",
        lambda service, cfg, default_region=None: client,
    )


def _entries(n: int) -> list[dict]:
    return [{"assetId": f"a{i}", "propertyId": "p"} for i in range(n)]


def test_39_entries_page_into_3_batch_calls_of_16(monkeypatch):
    """The diagnosed sweep: 39 machines → exactly ceil(39/16)=3 API calls
    (page size 16 = the BatchGetAssetPropertyValue cap), not 39."""
    client = _FakeBatchSiteWise()
    _patch(monkeypatch, client)

    results = sw.batch_get_property_values({}, "us-east-1", _entries(39))

    assert len(client.calls) == 3
    assert [len(c["entries"]) for c in client.calls] == [16, 16, 7]
    assert len(results) == 39
    # Values parse to the exact get_property_value shape (interchangeable seam).
    assert results["a0_p"] == {"value": "v-a0-p", "timestamp": 100,
                               "time": "1970-01-01T00:01:40+00:00", "quality": "GOOD"}
    assert results["a38_p"]["value"] == "v-a38-p"


def test_entry_ids_map_back_by_id_not_position(monkeypatch):
    """Results are keyed by entryId lookup — a responder returning entries in
    REVERSE order still maps every value to the right (asset, property)."""
    def responder(kwargs):
        return {"successEntries": [
            _ok_entry(e["entryId"], f"v-{e['assetId']}")
            for e in reversed(kwargs["entries"])
        ]}

    _patch(monkeypatch, _FakeBatchSiteWise(responder))
    results = sw.batch_get_property_values({}, None, _entries(5))
    for i in range(5):
        assert results[f"a{i}_p"]["value"] == f"v-a{i}"


def test_partial_error_and_skipped_entries_do_not_sink_the_batch(monkeypatch):
    """errorEntries / skippedEntries become per-key {"error": ...}; the other
    entries in the SAME page still succeed (a bad asset skips only itself)."""
    def responder(kwargs):
        entries = kwargs["entries"]
        return {
            "successEntries": [
                _ok_entry(e["entryId"], f"v-{e['assetId']}") for e in entries[2:]
            ],
            "errorEntries": [{
                "entryId": entries[0]["entryId"],
                "errorCode": "ResourceNotFoundException",
                "errorMessage": "asset gone",
            }],
            "skippedEntries": [{
                "entryId": entries[1]["entryId"],
                "completionStatus": "ERROR",
            }],
        }

    _patch(monkeypatch, _FakeBatchSiteWise(responder))
    results = sw.batch_get_property_values({}, None, _entries(6))

    assert results["a0_p"] == {"error": "asset gone"}
    assert results["a1_p"] == {"error": "ERROR"}
    for i in range(2, 6):
        assert results[f"a{i}_p"]["value"] == f"v-a{i}"


def test_page_level_failure_isolated_to_that_page(monkeypatch):
    """A whole page raising (throttle/permission) yields {"error": ...} for THAT
    page's 16 keys only — the other pages still return values (never raises)."""
    calls = {"n": 0}

    def responder(kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ThrottlingException: slow down")
        return {"successEntries": [
            _ok_entry(e["entryId"], f"v-{e['assetId']}") for e in kwargs["entries"]
        ]}

    _patch(monkeypatch, _FakeBatchSiteWise(responder))
    results = sw.batch_get_property_values({}, None, _entries(39))

    assert len(results) == 39
    errored = [k for k, v in results.items() if isinstance(v, dict) and v.get("error")]
    assert len(errored) == 16                       # exactly the failed page
    assert all(results[f"a{i}_p"].get("error") for i in range(16))
    assert results["a20_p"]["value"] == "v-a20"     # later pages unaffected


def test_next_token_continuation_within_a_page(monkeypatch):
    """A response carrying nextToken is continued (same entries + the token)
    until the token is exhausted; results merge across continuations."""
    def responder(kwargs):
        entries = kwargs["entries"]
        if "nextToken" not in kwargs:
            return {
                "successEntries": [
                    _ok_entry(e["entryId"], f"v-{e['assetId']}") for e in entries[:2]
                ],
                "nextToken": "tok-1",
            }
        assert kwargs["nextToken"] == "tok-1"
        return {"successEntries": [
            _ok_entry(e["entryId"], f"v-{e['assetId']}") for e in entries[2:]
        ]}

    client = _FakeBatchSiteWise(responder)
    _patch(monkeypatch, client)
    results = sw.batch_get_property_values({}, None, _entries(4))

    assert len(client.calls) == 2
    assert all(results[f"a{i}_p"]["value"] == f"v-a{i}" for i in range(4))


def test_empty_entries_no_api_call(monkeypatch):
    client = _FakeBatchSiteWise()
    _patch(monkeypatch, client)
    assert sw.batch_get_property_values({}, None, []) == {}
    assert client.calls == []
