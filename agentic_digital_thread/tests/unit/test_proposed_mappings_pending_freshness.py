# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pending-list freshness for ``GET /api/proposed-mappings`` (ADR-0065, 2026-09-18).

Live finding: after a guidance proposal was accepted (delivered) and another
rejected, the review list kept showing them as pending; the header badge on a
fresh deployment read "186 learning proposals awaiting review".

Two verifiable defects behind the first symptom:

* ``get_all_mappings`` scanned with the DynamoDB default (eventually
  consistent). ``accept``/``reject`` DO flip ``status`` on the row, but the
  client re-reads the list IMMEDIATELY after the mutation resolves, so an
  eventually-consistent scan can return the pre-write row — and the
  frontend then caches that snapshot (30 s ``staleTime``, no polling).
  Base-table scans support ``ConsistentRead``; the store is small.
* The route accepted no ``status`` query parameter, so a caller asking for
  ``?status=pending`` silently received every row regardless of status.

Placeholder ids only — no demo literals (ADR-0044).
"""
from __future__ import annotations

import asyncio

import pytest

import agents.schema_check as schema_check
import agents.self_learning as self_learning


class _RecordingTable:
    def __init__(self, items):
        self.items = list(items)
        self.scan_calls: list[dict] = []

    def scan(self, **kwargs):
        self.scan_calls.append(dict(kwargs))
        return {"Items": list(self.items)}


def _rows():
    return [
        {"mapping_id": "map-1", "discovered_at": "t1", "status": "pending",
         "proposed_type": "guidance_note", "from_system": "sysA", "to_value": "n1"},
        {"mapping_id": "map-2", "discovered_at": "t2", "status": "accepted",
         "proposed_type": "guidance_note", "from_system": "sysA", "to_value": "n2"},
        {"mapping_id": "map-3", "discovered_at": "t3", "status": "rejected",
         "proposed_type": "guidance_note", "from_system": "sysA", "to_value": "n3"},
        {"mapping_id": "map-4", "discovered_at": "t4", "status": "pending",
         "proposed_type": "insight", "from_system": "sysB", "to_value": "finding"},
    ]


def test_get_all_mappings_reads_the_base_table_strongly_consistent(monkeypatch):
    table = _RecordingTable(_rows())
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    out = self_learning.get_all_mappings()
    assert len(out) == 4
    assert table.scan_calls and all(c.get("ConsistentRead") is True for c in table.scan_calls)


def _run_route(monkeypatch, mappings, **query):
    import agents.concept_layer as concept_layer
    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: mappings)
    monkeypatch.setattr(self_learning, "known_systems", lambda: [])
    monkeypatch.setattr(concept_layer, "concept_descriptions", lambda _ids: {})
    monkeypatch.setattr(concept_layer, "concept_layer_of", lambda _c: "field")
    monkeypatch.setattr(schema_check, "load_system_schema", lambda sid: None)
    from agents.server import get_proposed_mappings_route
    return asyncio.run(get_proposed_mappings_route(**query))


def test_route_without_status_returns_every_row(monkeypatch):
    out = _run_route(monkeypatch, _rows())
    assert out["count"] == 4
    assert {m["mapping_id"] for m in out["mappings"]} == {"map-1", "map-2", "map-3", "map-4"}


def test_route_status_pending_excludes_accepted_and_rejected(monkeypatch):
    out = _run_route(monkeypatch, _rows(), status="pending")
    assert out["count"] == 2
    assert {m["mapping_id"] for m in out["mappings"]} == {"map-1", "map-4"}
    assert all(m["status"] == "pending" for m in out["mappings"])
    assert out["status"] == "pending"


@pytest.mark.parametrize("status", ["accepted", "rejected"])
def test_route_status_filter_is_exact(monkeypatch, status):
    out = _run_route(monkeypatch, _rows(), status=status)
    assert [m["status"] for m in out["mappings"]] == [status]


def test_route_rejects_unknown_status_value():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agents.server import get_proposed_mappings_route
    # Bare app with the same handler: the production app's auth middleware
    # answers 401 first, and its lifespan reaches the network. Validation
    # rejects the value before the handler (and any read) runs.
    bare = FastAPI()
    bare.get("/api/proposed-mappings")(get_proposed_mappings_route)
    response = TestClient(bare).get("/api/proposed-mappings", params={"status": "DROP TABLE"})
    assert response.status_code == 422


def test_route_proposed_type_filter_is_exact_and_combines_with_status(monkeypatch):
    out = _run_route(monkeypatch, _rows(), proposed_type="guidance_note")
    assert {m["mapping_id"] for m in out["mappings"]} == {"map-1", "map-2", "map-3"}
    assert out["proposed_type"] == "guidance_note"
    out = _run_route(monkeypatch, _rows(), status="pending", proposed_type="guidance_note")
    assert [m["mapping_id"] for m in out["mappings"]] == ["map-1"]


def test_untyped_rows_count_as_value_mapping_like_the_frontend(monkeypatch):
    rows = _rows() + [{"mapping_id": "map-5", "discovered_at": "t5", "status": "pending",
                       "from_system": "sysA", "to_value": "v"}]
    out = _run_route(monkeypatch, rows, proposed_type="value_mapping")
    assert [m["mapping_id"] for m in out["mappings"]] == ["map-5"]


def test_route_rejects_malformed_proposed_type():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agents.server import get_proposed_mappings_route
    bare = FastAPI()
    bare.get("/api/proposed-mappings")(get_proposed_mappings_route)
    response = TestClient(bare).get("/api/proposed-mappings",
                                    params={"proposed_type": "x' OR 1=1"})
    assert response.status_code == 422


# ── Expired rows are excluded by default (2026-09-28) ────────────────────────
# Live: the unfiltered GET served 1,850 rows / 3.2 MB (673 expired) and the
# review panel never painted. Expired rows are not actionable.

def _rows_with_expired():
    return _rows() + [
        {"mapping_id": "map-6", "discovered_at": "t6", "status": "expired",
         "proposed_type": "guidance_note", "from_system": "sysA", "to_value": "n6"},
        {"mapping_id": "map-7", "discovered_at": "t7", "status": "expired",
         "proposed_type": "insight", "from_system": "sysB", "to_value": "n7"},
    ]


def test_route_without_status_excludes_expired_by_default(monkeypatch):
    out = _run_route(monkeypatch, _rows_with_expired())
    assert {m["mapping_id"] for m in out["mappings"]} == {"map-1", "map-2", "map-3", "map-4"}
    assert out["count"] == 4


@pytest.mark.parametrize("flag", ["true", "1", "yes", "TRUE"])
def test_include_expired_true_serves_every_row(monkeypatch, flag):
    out = _run_route(monkeypatch, _rows_with_expired(), include_expired=flag)
    assert out["count"] == 6
    assert {"map-6", "map-7"} <= {m["mapping_id"] for m in out["mappings"]}


@pytest.mark.parametrize("flag", ["false", "0", "no", "on", "", "y", "truthy"])
def test_include_expired_parse_is_strict(monkeypatch, flag):
    out = _run_route(monkeypatch, _rows_with_expired(), include_expired=flag)
    assert all(m["status"] != "expired" for m in out["mappings"])


def test_status_expired_still_returns_expired_rows(monkeypatch):
    out = _run_route(monkeypatch, _rows_with_expired(), status="expired")
    assert [m["mapping_id"] for m in out["mappings"]] == ["map-6", "map-7"]
    assert out["status"] == "expired"


def test_explicit_status_ignores_include_expired(monkeypatch):
    out = _run_route(monkeypatch, _rows_with_expired(), status="pending",
                     include_expired="true")
    assert {m["mapping_id"] for m in out["mappings"]} == {"map-1", "map-4"}


def test_direct_call_default_query_objects_exclude_expired(monkeypatch):
    # Direct (non-HTTP) callers pass the Query default objects, not values.
    import agents.concept_layer as concept_layer
    from fastapi import Query
    monkeypatch.setattr(self_learning, "get_all_mappings", _rows_with_expired)
    monkeypatch.setattr(self_learning, "known_systems", lambda: [])
    monkeypatch.setattr(concept_layer, "concept_descriptions", lambda _ids: {})
    monkeypatch.setattr(concept_layer, "concept_layer_of", lambda _c: "field")
    monkeypatch.setattr(schema_check, "load_system_schema", lambda sid: None)
    from agents.server import get_proposed_mappings
    out = get_proposed_mappings(Query(None), Query(None), Query(None))
    assert out["count"] == 4


def test_status_counts_cover_every_status_over_the_type_filter(monkeypatch):
    rows = _rows_with_expired() + [
        {"mapping_id": "gap-1", "discovered_at": "t8", "status": "pending",
         "proposed_type": "guidance_note", "from_field": "detected_gap"},
        {"mapping_id": "staged#x", "discovered_at": "t9", "proposed_type": "guidance_note"},
    ]
    out = _run_route(monkeypatch, rows, status="accepted")
    assert out["status_counts"] == {"pending": 2, "accepted": 1, "rejected": 1,
                                    "superseded": 0, "expired": 2}
    out = _run_route(monkeypatch, rows, proposed_type="guidance_note")
    assert out["status_counts"] == {"pending": 1, "accepted": 1, "rejected": 1,
                                    "superseded": 0, "expired": 1}


def test_include_expired_is_length_bounded():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agents.server import get_proposed_mappings_route
    bare = FastAPI()
    bare.get("/api/proposed-mappings")(get_proposed_mappings_route)
    response = TestClient(bare).get("/api/proposed-mappings",
                                    params={"include_expired": "x" * 64})
    assert response.status_code == 422
