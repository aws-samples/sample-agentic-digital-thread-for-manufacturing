# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the vocabulary admin router (ADR 0050).

``fastapi.testclient`` against the router with every impure seam
monkeypatched (the ``test_scan_budget_routes.py`` idiom): the vocabulary
read model, the artifact-usage ledger, ``self_learning.assert_vocab`` (built
in parallel by W2.1 — patched with ``raising=False`` so these tests bind ONLY
to the frozen contract), ``log_proposed_mapping``, and the router's own lazy
graph seams (``_neptune_query`` / ``_neptune_write``). No AWS, no graph, no
network.

Covers (the W2.2 acceptance list):
  * 200 response shapes VERBATIM per the shared interface contract;
  * import: inserted/skipped/failed incl. a partial-failure batch;
  * 409 on retiring a mapped concept (ValueError from the apply path);
  * 400s on bad import shapes and on a PATCH body carrying ``id``;
  * import purity — no boto3/pg8000 at module import time.

Auth note: ``/api/admin/*`` is gated by ``AuthMiddleware.ADMIN_PREFIXES``
before any handler runs (pinned elsewhere); the app built here mounts the
router bare, exercising handler behavior only.
"""

import os
import subprocess  # nosec B404 - fresh-interpreter import-purity check, fixed argv
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import agents.artifact_usage as artifact_usage_mod
import agents.self_learning as self_learning_mod
import agents.vocabulary as vocabulary_mod
import agents.vocabulary_routes as vr


class _AdminInjectMiddleware(BaseHTTPMiddleware):
    """Stand-in for AuthMiddleware's ``request.state.is_admin`` (the real
    middleware also 403s non-admins on ``/api/admin`` before any handler —
    pinned in the middleware's own tests, not here)."""

    async def dispatch(self, request, call_next):
        request.state.is_admin = True
        return await call_next(request)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(vr.router)
    app.add_middleware(_AdminInjectMiddleware)
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_build_app())


@pytest.fixture
def actor(monkeypatch) -> str:
    """Pin the derived actor so assertions on the pass-through are exact."""
    monkeypatch.setattr(vr, "_request_actor", lambda request: "steward@example.com")
    return "steward@example.com"


@pytest.fixture
def assert_vocab_spy(monkeypatch):
    """Record ``assert_vocab`` kwargs; return a contract-shaped result.

    ``raising=False``: W2.1 lands the real function in parallel — this suite
    binds to the frozen contract, not to its presence."""
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return {"status": "accepted", "mapping_id": "m-123", "evidence_score": 0.87}

    monkeypatch.setattr(self_learning_mod, "assert_vocab", fake, raising=False)
    return calls


# --------------------------------------------------------------------------- #
# GET /concepts — read model + usage ledger composition
# --------------------------------------------------------------------------- #

_CONCEPT_ROW = {
    "id": "oee_score",
    "name": "OEE Score",
    "description": "Overall equipment effectiveness.",
    "synonyms": ["oee"],
    "layer": "core",
    "source": "core",
    "provenance_label": "core",
    "mapped_field_count": 2,
    "usage_30d": 5,
    "last_used_at": "2026-07-20T12:00:00+00:00",
    "retired": False,
}


def test_get_concepts_contract_shape(client, monkeypatch):
    usage = {"oee_score": {"artifact_id": "oee_score", "kind": "concept",
                           "usage_30d": 5, "last_used_at": "2026-07-20T12:00:00+00:00"}}
    seen: dict = {}

    def fake_read(days=30):
        seen["days"] = days
        return usage

    def fake_list(query=None, usage=None):
        seen["usage_passed"] = usage
        return [dict(_CONCEPT_ROW)]

    monkeypatch.setattr(artifact_usage_mod, "read_artifact_usage", fake_read)
    monkeypatch.setattr(vocabulary_mod, "list_concepts", fake_list)

    resp = client.get("/api/admin/vocabulary/concepts")
    assert resp.status_code == 200
    body = resp.json()
    # Verbatim envelope + row shape per the shared interface contract.
    assert set(body.keys()) == {"concepts", "total"}
    assert body["total"] == 1
    assert body["concepts"] == [_CONCEPT_ROW]
    assert set(body["concepts"][0].keys()) == {
        "id", "name", "description", "synonyms", "layer", "source",
        "provenance_label", "mapped_field_count", "usage_30d",
        "last_used_at", "retired",
    }
    # Composition: the ledger read (30d) is passed INTO the read model.
    assert seen["days"] == 30
    assert seen["usage_passed"] is usage


# --------------------------------------------------------------------------- #
# GET /usage — ledger projection
# --------------------------------------------------------------------------- #

def test_get_usage_contract_shape_and_order(client, monkeypatch):
    ledger = {
        "oee_score": {"artifact_id": "oee_score", "kind": "concept",
                      "usage_30d": 2, "last_used_at": "2026-07-19T00:00:00+00:00"},
        "n-rule-1": {"artifact_id": "n-rule-1", "kind": "identity_rule",
                     "usage_30d": 7, "last_used_at": "2026-07-20T00:00:00+00:00"},
    }
    monkeypatch.setattr(artifact_usage_mod, "read_artifact_usage", lambda days=30: ledger)

    resp = client.get("/api/admin/vocabulary/usage")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"artifacts"}
    # Most-used first; exactly the four contract keys per artifact.
    assert body["artifacts"] == [
        {"artifact_id": "n-rule-1", "kind": "identity_rule",
         "usage_30d": 7, "last_used_at": "2026-07-20T00:00:00+00:00"},
        {"artifact_id": "oee_score", "kind": "concept",
         "usage_30d": 2, "last_used_at": "2026-07-19T00:00:00+00:00"},
    ]


# --------------------------------------------------------------------------- #
# POST /concepts — lifecycle assertion (op=add)
# --------------------------------------------------------------------------- #

def test_post_concept_contract_shape_and_actor(client, actor, assert_vocab_spy):
    resp = client.post("/api/admin/vocabulary/concepts", json={
        "id": "line_speed",
        "name": "Line Speed",
        "description": "Units per minute on a line.",
        "synonyms": ["speed"],
        "evidence_system": "sys_mes",
        "evidence_field": "runs.speed",
        "override_evidence": True,
    })
    assert resp.status_code == 200
    # Verbatim contract shape.
    assert resp.json() == {"status": "accepted", "mapping_id": "m-123",
                           "evidence_score": 0.87}
    assert len(assert_vocab_spy) == 1
    kw = assert_vocab_spy[0]
    assert kw["op"] == "add"
    assert kw["concept"] == "line_speed"
    assert kw["name"] == "Line Speed"
    assert kw["synonyms"] == ["speed"]
    assert kw["evidence_system"] == "sys_mes"
    assert kw["evidence_field"] == "runs.speed"
    assert kw["override_evidence"] is True
    assert kw["actor"] == actor
    assert kw["is_admin"] is True


def test_post_concept_missing_id_derived_from_name(client, actor, assert_vocab_spy):
    resp = client.post("/api/admin/vocabulary/concepts",
                       json={"name": "Batch Yield"})
    assert resp.status_code == 200
    assert assert_vocab_spy[0]["concept"] == "batch_yield"


def test_post_concept_pending_null_score(client, actor, monkeypatch):
    monkeypatch.setattr(
        self_learning_mod, "assert_vocab",
        lambda **kw: {"status": "pending", "mapping_id": "m-9", "evidence_score": None},
        raising=False,
    )
    resp = client.post("/api/admin/vocabulary/concepts", json={"name": "Batch Yield"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending", "mapping_id": "m-9",
                           "evidence_score": None}


def test_post_concept_missing_name_400(client, assert_vocab_spy):
    resp = client.post("/api/admin/vocabulary/concepts", json={"description": "x"})
    assert resp.status_code == 400
    assert "error" in resp.json()
    assert assert_vocab_spy == []


def test_post_concept_value_error_maps_to_400(client, actor, monkeypatch):
    def boom(**kw):
        raise ValueError("evidence check failed: no overlap")
    monkeypatch.setattr(self_learning_mod, "assert_vocab", boom, raising=False)
    resp = client.post("/api/admin/vocabulary/concepts", json={"name": "X Rate"})
    assert resp.status_code == 400
    assert resp.json() == {"error": "evidence check failed: no overlap"}


@pytest.mark.parametrize("bad_id", [
    "line-speed",      # hyphen
    "line speed",      # whitespace
    "line.speed",      # dot
    "speed!",          # punctuation
    "vitesse_café",    # non-ascii
])
def test_post_concept_explicit_id_bad_charset_400(client, assert_vocab_spy, bad_id):
    """An explicit id must satisfy the SAME charset rule the import path
    applies ([a-z0-9_] after lowercasing) — 400, lifecycle untouched."""
    resp = client.post("/api/admin/vocabulary/concepts",
                       json={"id": bad_id, "name": "Line Speed"})
    assert resp.status_code == 400
    assert "[a-z0-9_]" in resp.json()["error"]
    assert assert_vocab_spy == []


def test_post_concept_explicit_id_lowercased(client, actor, assert_vocab_spy):
    resp = client.post("/api/admin/vocabulary/concepts",
                       json={"id": "LINE_SPEED", "name": "Line Speed"})
    assert resp.status_code == 200
    assert assert_vocab_spy[0]["concept"] == "line_speed"


# --------------------------------------------------------------------------- #
# PATCH /concepts/{id} — id immutable
# --------------------------------------------------------------------------- #

def test_patch_concept_ok(client, actor, assert_vocab_spy):
    resp = client.patch("/api/admin/vocabulary/concepts/line_speed",
                        json={"name": "Line Rate", "synonyms": ["rate"]})
    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted", "mapping_id": "m-123",
                           "evidence_score": 0.87}
    kw = assert_vocab_spy[0]
    assert kw["op"] == "edit"
    assert kw["concept"] == "line_speed"
    assert kw["name"] == "Line Rate"
    assert kw["synonyms"] == ["rate"]
    assert kw["actor"] == actor
    assert kw["is_admin"] is True


def test_patch_with_id_in_body_400(client, assert_vocab_spy):
    resp = client.patch("/api/admin/vocabulary/concepts/line_speed",
                        json={"id": "new_id", "name": "Line Rate"})
    assert resp.status_code == 400
    assert "immutable" in resp.json()["error"]
    assert assert_vocab_spy == []  # rejected at the boundary — lifecycle untouched


@pytest.mark.parametrize("body", [
    {"description": "updated only"},   # name absent entirely
    {"name": "", "description": "d"},  # present but empty
    {"name": "   ", "synonyms": []},   # present but whitespace
])
def test_patch_without_usable_name_passes_none(client, actor, assert_vocab_spy, body):
    """H2 router side: a PATCH body with no usable name must pass ``name=None``
    through to assert_vocab (the apply path preserves the existing name on
    absence) — NEVER a fabricated default and NEVER the concept id."""
    resp = client.patch("/api/admin/vocabulary/concepts/line_speed", json=body)
    assert resp.status_code == 200
    kw = assert_vocab_spy[0]
    assert kw["op"] == "edit"
    assert kw["concept"] == "line_speed"
    assert kw["name"] is None  # absence propagated — not '' / not 'line_speed'


# --------------------------------------------------------------------------- #
# POST /concepts/{id}/retire — 409 when mapped
# --------------------------------------------------------------------------- #

def test_retire_ok(client, actor, assert_vocab_spy):
    resp = client.post("/api/admin/vocabulary/concepts/line_speed/retire")
    assert resp.status_code == 200
    assert resp.json() == {"status": "retired"}
    kw = assert_vocab_spy[0]
    assert kw["op"] == "retire"
    assert kw["concept"] == "line_speed"
    assert kw["actor"] == actor
    assert kw["is_admin"] is True


def test_retire_mapped_concept_409(client, actor, monkeypatch):
    def boom(**kw):
        raise ValueError("concept 'line_speed' is mapped to 3 fields — unmap first")
    monkeypatch.setattr(self_learning_mod, "assert_vocab", boom, raising=False)
    resp = client.post("/api/admin/vocabulary/concepts/line_speed/retire")
    assert resp.status_code == 409
    assert resp.json() == {
        "error": "concept 'line_speed' is mapped to 3 fields — unmap first"
    }


# --------------------------------------------------------------------------- #
# POST /import — the pack-install generalization
# --------------------------------------------------------------------------- #

def _import_body(concepts):
    return {"metadata": {"name": "Site Vocabulary"}, "concepts": concepts}


def test_import_inserted_skipped_failed_partial_batch(client, monkeypatch):
    """One existing id (skipped), one clean insert, one MERGE failure —
    the batch never aborts and each row is reported exactly once."""
    queries: list = []

    def fake_query(cypher, params=None):
        queries.append((cypher, params))
        return [{"id": "existing_concept"}]

    writes: list = []

    def fake_write(cypher, params=None):
        if params["id"] == "bad_row":
            raise RuntimeError("neptune write failed")
        writes.append((cypher, params))

    monkeypatch.setattr(vr, "_neptune_query", fake_query)
    monkeypatch.setattr(vr, "_neptune_write", fake_write)
    # The ADR-0081 post-commit drift hook rides the import response; pin it
    # (its own behavior is covered in test_vocabulary_drift.py).
    monkeypatch.setattr(vr, "_drift_pass_report",
                        lambda slug, ids: {"stubbed": True})

    resp = client.post("/api/admin/vocabulary/import", json=_import_body([
        {"id": "existing_concept", "name": "Existing", "description": "d1"},
        {"id": "fresh_concept", "name": "Fresh", "description": "d2"},
        {"id": "bad_row", "name": "Bad Row", "description": "d3"},
    ]))
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "slug": "site_vocabulary",
        "inserted": 1,
        "skipped": 1,
        "failed": 1,
        "skipped_ids": ["existing_concept"],
        "collisions": [],
        "vocabulary_drift": {"stubbed": True},
    }
    # Pre-read: exactly ONE query, carrying ALL candidate ids.
    assert len(queries) == 1
    assert "WHERE c.id IN $ids" in queries[0][0]
    assert queries[0][1] == {"ids": ["existing_concept", "fresh_concept", "bad_row"]}
    # The MERGE carries the custom-pack provenance.
    assert len(writes) == 1
    assert writes[0][1] == {
        "id": "fresh_concept", "name": "Fresh", "description": "d2",
        "source": "pack:custom:site_vocabulary",
    }
    assert writes[0][0].startswith("MERGE (c:Concept {id: $id})")


def test_import_slugifies_ids_and_all_inserted(client, monkeypatch):
    monkeypatch.setattr(vr, "_neptune_query", lambda c, p=None: [])
    writes: list = []
    monkeypatch.setattr(vr, "_neptune_write", lambda c, p=None: writes.append(p))

    resp = client.post("/api/admin/vocabulary/import", json={
        "metadata": {"name": "My-Pack v2!"},
        "concepts": [{"id": "OEE Score", "name": "OEE", "description": ""}],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == "my_pack_v2"
    assert body["inserted"] == 1 and body["skipped"] == 0 and body["failed"] == 0
    assert body["skipped_ids"] == []
    assert writes[0]["id"] == "oee_score"
    assert writes[0]["source"] == "pack:custom:my_pack_v2"


@pytest.mark.parametrize("body", [
    {},                                                       # nothing
    {"metadata": {}, "concepts": [{"id": "a", "name": "A"}]},  # no metadata.name
    {"metadata": {"name": "P"}, "concepts": []},              # empty concepts
    {"metadata": {"name": "P"}},                              # concepts absent
    {"metadata": {"name": "!!!"}, "concepts": [{"id": "a", "name": "A"}]},  # empty slug
    {"metadata": {"name": "P"}, "concepts": [{"id": "!!!", "name": "A"}]},  # bad id
    {"metadata": {"name": "P"}, "concepts": [{"id": "a"}]},   # row missing name
])
def test_import_bad_shapes_400(client, monkeypatch, body):
    monkeypatch.setattr(vr, "_neptune_query", lambda c, p=None: [])
    writes: list = []
    monkeypatch.setattr(vr, "_neptune_write", lambda c, p=None: writes.append(p))
    resp = client.post("/api/admin/vocabulary/import", json=body)
    assert resp.status_code == 400
    assert "error" in resp.json()
    assert writes == []  # boundary rejection — nothing written


@pytest.fixture
def import_writes(monkeypatch) -> list:
    """Neptune seams patched; returns the (should-stay-empty) write list."""
    monkeypatch.setattr(vr, "_neptune_query", lambda c, p=None: [])
    writes: list = []
    monkeypatch.setattr(vr, "_neptune_write", lambda c, p=None: writes.append(p))
    return writes


def test_import_batch_cap_400(client, import_writes):
    """More rows than the batch cap → 400 with an actionable message."""
    concepts = [{"id": f"concept_{i}", "name": f"Concept {i}"} for i in range(501)]
    resp = client.post("/api/admin/vocabulary/import", json=_import_body(concepts))
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert "500" in err and "got 501" in err
    assert import_writes == []


def test_import_batch_at_cap_ok(client, import_writes):
    concepts = [{"id": f"concept_{i}", "name": f"Concept {i}"} for i in range(500)]
    resp = client.post("/api/admin/vocabulary/import", json=_import_body(concepts))
    assert resp.status_code == 200
    assert resp.json()["inserted"] == 500


@pytest.mark.parametrize("row,expect", [
    ({"id": "a" * 129, "name": "Long Id"}, "128"),
    ({"id": "ok_id", "name": "n" * 257}, "256"),
    ({"id": "ok_id", "name": "Ok", "description": "d" * 4097}, "4096"),
])
def test_import_field_length_caps_400(client, import_writes, row, expect):
    resp = client.post("/api/admin/vocabulary/import", json=_import_body([row]))
    assert resp.status_code == 400
    assert expect in resp.json()["error"]
    assert import_writes == []


def test_import_field_lengths_at_cap_ok(client, import_writes):
    row = {"id": "a" * 128, "name": "n" * 256, "description": "d" * 4096}
    resp = client.post("/api/admin/vocabulary/import", json=_import_body([row]))
    assert resp.status_code == 200
    assert resp.json()["inserted"] == 1


def test_import_missing_id_message_names_the_problem(client, import_writes):
    """A row with a name but no id must say 'missing id' — not a misleading
    generic/slugify message."""
    resp = client.post("/api/admin/vocabulary/import",
                       json=_import_body([{"name": "Orphan Row"}]))
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert "missing 'id'" in err and "Orphan Row" in err


def test_import_missing_name_message_names_the_problem(client, import_writes):
    resp = client.post("/api/admin/vocabulary/import",
                       json=_import_body([{"id": "some_id"}]))
    assert resp.status_code == 400
    assert "'name'" in resp.json()["error"]


# --------------------------------------------------------------------------- #
# POST /import — C6 normalized-id collision guard
# --------------------------------------------------------------------------- #

def test_import_hyphen_underscore_collision_skipped_and_reported(client, monkeypatch):
    """A row whose NORMALIZED id (lower, '-'→'_') matches an existing graph id
    without being an exact match is neither inserted nor MERGEd, and lands in
    the additive ``collisions`` array — NOT in ``skipped_ids``."""
    monkeypatch.setattr(vr, "_neptune_query",
                        lambda c, p=None: [{"id": "oee-score"}])
    writes: list = []
    monkeypatch.setattr(vr, "_neptune_write", lambda c, p=None: writes.append(p))
    # The ADR-0081 post-commit drift hook rides the import response; pin it
    # (its own behavior is covered in test_vocabulary_drift.py).
    monkeypatch.setattr(vr, "_drift_pass_report",
                        lambda slug, ids: {"stubbed": True})

    resp = client.post("/api/admin/vocabulary/import", json=_import_body([
        {"id": "oee_score", "name": "OEE Score", "description": "d"},
    ]))
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "slug": "site_vocabulary",
        "inserted": 0,
        "skipped": 0,
        "failed": 0,
        "skipped_ids": [],
        "collisions": [{"id": "oee_score", "existing_id": "oee-score",
                        "reason": "normalized-id collision"}],
        "vocabulary_drift": {"stubbed": True},
    }
    assert writes == []  # collision rows are neither inserted nor MERGEd


def test_import_exact_skip_stays_in_skipped_ids_not_collisions(client, monkeypatch):
    """``skipped_ids`` keeps meaning EXACT-id skips only — an exact graph
    match must never be re-labeled a collision, even when a hyphen sibling
    also exists in the graph."""
    monkeypatch.setattr(
        vr, "_neptune_query",
        lambda c, p=None: [{"id": "oee_score"}, {"id": "oee-score"}])
    writes: list = []
    monkeypatch.setattr(vr, "_neptune_write", lambda c, p=None: writes.append(p))

    resp = client.post("/api/admin/vocabulary/import", json=_import_body([
        {"id": "oee_score", "name": "OEE Score"},
    ]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["skipped_ids"] == ["oee_score"]
    assert body["collisions"] == []
    assert body["skipped"] == 1 and body["inserted"] == 0
    assert writes == []


def test_import_in_batch_dupe_is_collision_for_the_later_row(client, monkeypatch):
    """Two rows normalizing to the same key inside ONE batch: the first
    inserts, the later row is a collision against the earlier row's id."""
    monkeypatch.setattr(vr, "_neptune_query", lambda c, p=None: [])
    writes: list = []
    monkeypatch.setattr(vr, "_neptune_write", lambda c, p=None: writes.append(p))

    resp = client.post("/api/admin/vocabulary/import", json=_import_body([
        {"id": "flow_rate", "name": "Flow Rate"},
        {"id": "Flow-Rate", "name": "Flow Rate Again"},  # slugs to flow_rate
    ]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["inserted"] == 1
    assert body["skipped_ids"] == []
    assert body["collisions"] == [{"id": "flow_rate", "existing_id": "flow_rate",
                                   "reason": "normalized-id collision"}]
    assert [w["id"] for w in writes] == ["flow_rate"]


def test_import_pre_read_is_one_query_covering_normalized_keys(client, monkeypatch):
    """The collision guard must not add a second graph read: ONE pre-read
    query matching both exact ids and normalized keys."""
    queries: list = []

    def fake_query(cypher, params=None):
        queries.append((cypher, params))
        return []

    monkeypatch.setattr(vr, "_neptune_query", fake_query)
    monkeypatch.setattr(vr, "_neptune_write", lambda c, p=None: None)

    resp = client.post("/api/admin/vocabulary/import", json=_import_body([
        {"id": "line_speed", "name": "Line Speed"},
    ]))
    assert resp.status_code == 200
    assert len(queries) == 1
    cypher, params = queries[0]
    assert "WHERE c.id IN $ids" in cypher
    assert "replace(toLower(c.id), '-', '_') IN $ids" in cypher
    assert params == {"ids": ["line_speed"]}


# --------------------------------------------------------------------------- #
# POST /import/preview — C7 dry-run (shared classification, zero writes)
# --------------------------------------------------------------------------- #

def test_import_preview_rows_counts_and_zero_writes(client, monkeypatch):
    monkeypatch.setattr(
        vr, "_neptune_query",
        lambda c, p=None: [{"id": "existing_concept"}, {"id": "legacy-metric"}])
    writes: list = []
    monkeypatch.setattr(vr, "_neptune_write", lambda c, p=None: writes.append(p))

    resp = client.post("/api/admin/vocabulary/import/preview", json=_import_body([
        {"id": "existing_concept", "name": "Existing"},
        {"id": "legacy_metric", "name": "Legacy Metric"},
        {"id": "fresh_concept", "name": "Fresh"},
        {"id": "fresh-concept", "name": "Fresh Again"},  # in-batch dupe by key
    ]))
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "preview",
        "slug": "site_vocabulary",
        "rows": [
            {"id": "existing_concept", "name": "Existing",
             "action": "skip_exact", "collides_with": None},
            {"id": "legacy_metric", "name": "Legacy Metric",
             "action": "skip_collision", "collides_with": "legacy-metric"},
            {"id": "fresh_concept", "name": "Fresh",
             "action": "insert", "collides_with": None},
            {"id": "fresh_concept", "name": "Fresh Again",
             "action": "skip_collision", "collides_with": "fresh_concept"},
        ],
        "insert_count": 1,
        "skip_count": 1,
        "collision_count": 2,
    }
    assert writes == []  # dry-run: ZERO graph writes


@pytest.mark.parametrize("body", [
    {},                                                       # nothing
    {"metadata": {}, "concepts": [{"id": "a", "name": "A"}]},  # no metadata.name
    {"metadata": {"name": "P"}, "concepts": []},              # empty concepts
    {"metadata": {"name": "P"}, "concepts": [{"id": "a"}]},   # row missing name
])
def test_import_preview_same_boundary_validation_400(client, import_writes, body):
    resp = client.post("/api/admin/vocabulary/import/preview", json=body)
    assert resp.status_code == 400
    assert "error" in resp.json()
    assert import_writes == []


def test_preview_and_apply_agree_on_the_same_body(client, monkeypatch):
    """Property-style drift guard over the shared classification helper:
    preview's predicted actions must equal what apply then does — inserts
    written, exact skips in ``skipped_ids``, collisions in ``collisions``."""
    graph_rows = [{"id": "existing_concept"}, {"id": "legacy-metric"}]
    monkeypatch.setattr(vr, "_neptune_query", lambda c, p=None: list(graph_rows))
    writes: list = []
    monkeypatch.setattr(vr, "_neptune_write", lambda c, p=None: writes.append(p))
    body = _import_body([
        {"id": "existing_concept", "name": "Existing"},
        {"id": "legacy_metric", "name": "Legacy Metric"},
        {"id": "fresh_concept", "name": "Fresh"},
        {"id": "fresh-concept", "name": "Fresh Again"},
    ])

    preview = client.post("/api/admin/vocabulary/import/preview", json=body).json()
    apply = client.post("/api/admin/vocabulary/import", json=body).json()

    predicted_inserts = [r["id"] for r in preview["rows"] if r["action"] == "insert"]
    predicted_exact = [r["id"] for r in preview["rows"] if r["action"] == "skip_exact"]
    predicted_collisions = [(r["id"], r["collides_with"]) for r in preview["rows"]
                            if r["action"] == "skip_collision"]
    assert [w["id"] for w in writes] == predicted_inserts
    assert apply["inserted"] == preview["insert_count"]
    assert apply["skipped_ids"] == predicted_exact
    assert [(c["id"], c["existing_id"]) for c in apply["collisions"]] \
        == predicted_collisions


# --------------------------------------------------------------------------- #
# GET /retirement-candidates — C8 deterministic report-only sweep
# --------------------------------------------------------------------------- #

def _concept_row(cid, source="pack:custom:site_vocabulary", mapped=0, usage=0,
                 retired=False, last_used_at=None):
    return {"id": cid, "name": cid.replace("_", " ").title(), "description": "",
            "synonyms": [], "layer": "dynamic", "source": source,
            "provenance_label": source, "mapped_field_count": mapped,
            "usage_30d": usage, "last_used_at": last_used_at, "retired": retired}


def test_retirement_candidates_rule_per_clause_and_ordering(client, monkeypatch):
    """Every exclusion clause of the C8 rule, plus deterministic id order."""
    from agents.catalog import CORE_CONCEPTS
    core_id = CORE_CONCEPTS[0]
    rows = [
        _concept_row("zeta_unused"),                          # candidate
        _concept_row("alpha_unused"),                         # candidate (sorts first)
        _concept_row("core_backed", source="core"),           # excluded: core source
        _concept_row(core_id),                                # excluded: id in CORE_CONCEPTS
        _concept_row("already_retired", retired=True),        # excluded: retired
        _concept_row("still_used", usage=3,
                     last_used_at="2026-07-20T00:00:00+00:00"),  # excluded: usage>0
        _concept_row("still_mapped", mapped=2),               # excluded: mapped>0
    ]
    monkeypatch.setattr(artifact_usage_mod, "read_artifact_usage",
                        lambda days=30: {})
    monkeypatch.setattr(vocabulary_mod, "list_concepts",
                        lambda query=None, usage=None: rows)
    writes: list = []
    monkeypatch.setattr(vr, "_neptune_write", lambda c, p=None: writes.append(p))

    resp = client.get("/api/admin/vocabulary/retirement-candidates")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"candidates"}
    assert [c["id"] for c in body["candidates"]] == ["alpha_unused", "zeta_unused"]
    row = body["candidates"][0]
    # Verbatim C8 candidate shape.
    assert set(row.keys()) == {"id", "name", "source", "mapped_field_count",
                               "usage_30d", "last_used_at", "reasons"}
    assert row == {"id": "alpha_unused", "name": "Alpha Unused",
                   "source": "pack:custom:site_vocabulary",
                   "mapped_field_count": 0, "usage_30d": 0,
                   "last_used_at": None,
                   "reasons": ["0 mapped fields", "0 uses in the last 30 days"]}
    assert writes == []  # report-only — retire goes through the lifecycle route


def test_retirement_candidates_composes_usage_into_read_model(client, monkeypatch):
    """The GET /concepts idiom: the 30-day ledger read is passed INTO
    list_concepts (off-loop composition, no second ledger scan)."""
    usage = {"idle_metric": {"artifact_id": "idle_metric", "kind": "concept",
                             "usage_30d": 0, "last_used_at": None}}
    seen: dict = {}

    def fake_read(days=30):
        seen["days"] = days
        return usage

    def fake_list(query=None, usage=None):
        seen["usage_passed"] = usage
        return []

    monkeypatch.setattr(artifact_usage_mod, "read_artifact_usage", fake_read)
    monkeypatch.setattr(vocabulary_mod, "list_concepts", fake_list)

    resp = client.get("/api/admin/vocabulary/retirement-candidates")
    assert resp.status_code == 200
    assert resp.json() == {"candidates": []}  # empty state
    assert seen["days"] == 30
    assert seen["usage_passed"] is usage


# --------------------------------------------------------------------------- #
# POST /guidance/{system_id}/propose — guidance_note proposal
# --------------------------------------------------------------------------- #

def test_guidance_propose_files_lifecycle_proposal(client, monkeypatch):
    calls: list = []

    def fake_log(**kwargs):
        calls.append(kwargs)
        return "gn-42"

    _patch_catalog_systems(monkeypatch, [{"id": "sys_mes"}])
    monkeypatch.setattr(self_learning_mod, "log_proposed_mapping", fake_log)
    resp = client.post("/api/admin/vocabulary/guidance/sys_mes/propose",
                       json={"note": "Prefer the runs table for shift metrics."})
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending", "mapping_id": "gn-42"}
    kw = calls[0]
    assert kw["proposed_type"] == "guidance_note"
    assert kw["source"] == "user"
    assert kw["provenance"] == "human-asserted"
    assert kw["from_system"] == "sys_mes"
    assert kw["to_value"] == "Prefer the runs table for shift metrics."


def test_guidance_propose_empty_note_400(client, monkeypatch):
    calls: list = []
    monkeypatch.setattr(self_learning_mod, "log_proposed_mapping",
                        lambda **kw: calls.append(kw) or "x")
    resp = client.post("/api/admin/vocabulary/guidance/sys_mes/propose",
                       json={"note": "   "})
    assert resp.status_code == 400
    assert calls == []


def test_guidance_propose_dropped_at_ingestion_400(client, monkeypatch):
    _patch_catalog_systems(monkeypatch, [{"id": "sys_mes"}])
    monkeypatch.setattr(self_learning_mod, "log_proposed_mapping", lambda **kw: None)
    resp = client.post("/api/admin/vocabulary/guidance/sys_mes/propose",
                       json={"note": "a note"})
    assert resp.status_code == 400
    assert "error" in resp.json()


def _patch_catalog_systems(monkeypatch, systems):
    import agents.catalog as catalog_mod
    monkeypatch.setattr(catalog_mod.catalog, "get_all", lambda: systems)


def test_guidance_propose_unknown_system_404(client, monkeypatch):
    """Catalog HAS entries and none match → definitively unknown → 404, and
    the lifecycle is never touched."""
    _patch_catalog_systems(monkeypatch, [{"id": "sys_mes"}, {"id": "sys_erp"}])
    calls: list = []
    monkeypatch.setattr(self_learning_mod, "log_proposed_mapping",
                        lambda **kw: calls.append(kw) or "x")
    resp = client.post("/api/admin/vocabulary/guidance/no_such_system/propose",
                       json={"note": "a note"})
    assert resp.status_code == 404
    assert "no_such_system" in resp.json()["error"]
    assert calls == []


def test_guidance_propose_known_system_passes_check(client, monkeypatch):
    _patch_catalog_systems(monkeypatch, [{"id": "sys_mes"}])
    monkeypatch.setattr(self_learning_mod, "log_proposed_mapping", lambda **kw: "gn-7")
    resp = client.post("/api/admin/vocabulary/guidance/sys_mes/propose",
                       json={"note": "a note"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending", "mapping_id": "gn-7"}


@pytest.mark.parametrize("failure_mode", ["raises", "empty_catalog"])
def test_guidance_propose_lookup_failure_degrades_open(client, monkeypatch, failure_mode):
    """Availability over strictness: a lookup failure or an empty (unhydrated)
    catalog must NOT reject the proposal — the check degrades open."""
    if failure_mode == "raises":
        def boom():
            raise RuntimeError("catalog unavailable")
        import agents.catalog as catalog_mod
        monkeypatch.setattr(catalog_mod.catalog, "get_all", boom)
    else:
        _patch_catalog_systems(monkeypatch, [])
    monkeypatch.setattr(self_learning_mod, "log_proposed_mapping", lambda **kw: "gn-8")
    resp = client.post("/api/admin/vocabulary/guidance/sys_mes/propose",
                       json={"note": "a note"})
    assert resp.status_code == 200
    assert resp.json()["mapping_id"] == "gn-8"


# --------------------------------------------------------------------------- #
# Contract drift guard — the router's kwargs must bind to the REAL
# self_learning.assert_vocab signature (W2.1). Skips if W2.1 hasn't landed.
# --------------------------------------------------------------------------- #

def test_router_kwargs_bind_to_real_assert_vocab_signature(client, actor, monkeypatch):
    import inspect
    real = getattr(self_learning_mod, "assert_vocab", None)
    if real is None:
        pytest.skip("W2.1 assert_vocab not landed yet — contract-only build")
    sig = inspect.signature(real)
    calls: list[dict] = []
    monkeypatch.setattr(
        self_learning_mod, "assert_vocab",
        lambda **kw: calls.append(kw) or {"status": "accepted", "mapping_id": "m",
                                          "evidence_score": None},
    )
    client.post("/api/admin/vocabulary/concepts",
                json={"id": "line_speed", "name": "Line Speed"})
    client.patch("/api/admin/vocabulary/concepts/line_speed",
                 json={"name": "Line Rate"})
    client.post("/api/admin/vocabulary/concepts/line_speed/retire")
    assert len(calls) == 3
    for kw in calls:
        sig.bind(**kw)  # raises TypeError on kwargs drift


# --------------------------------------------------------------------------- #
# Import purity (ADR 0020/0021)
# --------------------------------------------------------------------------- #

def test_module_source_has_no_toplevel_boto3():
    src = open(vr.__file__).read()
    assert "import boto3" not in src
    assert "import pg8000" not in src and "import pgdriver" not in src


def test_module_imports_with_boto3_absent():
    """Fresh interpreter with boto3/botocore/pg8000/agents.pgdriver BOMBED still
    imports the router module (subprocess fresh-interpreter convention — see
    test_query_intelligence.py / test_vocabulary_read_model.py)."""
    code = (
        "import sys\n"
        "for _m in ('boto3', 'botocore', 'pg8000', 'agents.pgdriver'):\n"
        "    sys.modules[_m] = None\n"
        "import agents.vocabulary_routes as vr\n"
        "assert vr.router.prefix == '/api/admin/vocabulary'\n"
        "paths = {r.path for r in vr.router.routes}\n"
        "assert '/api/admin/vocabulary/concepts' in paths\n"
        "assert '/api/admin/vocabulary/import' in paths\n"
        "assert '/api/admin/vocabulary/import/preview' in paths\n"
        "assert '/api/admin/vocabulary/retirement-candidates' in paths\n"
        "assert '/api/admin/vocabulary/usage' in paths\n"
        "print('IMPORT-PURE-OK')\n"
    )
    adt_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(adt_root))
    r = subprocess.run([sys.executable, "-c", code], cwd=str(adt_root),  # nosec B603 - fixed argv, no shell
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "IMPORT-PURE-OK" in r.stdout


def test_guidance_propose_defaults_to_query_hint_scope(client, monkeypatch):
    calls: list = []
    _patch_catalog_systems(monkeypatch, [{"id": "sys_mes"}])
    monkeypatch.setattr(
        self_learning_mod, "log_proposed_mapping",
        lambda **kw: calls.append(kw) or "gn-scoped")

    resp = client.post(
        "/api/admin/vocabulary/guidance/sys_mes/propose",
        json={"note": "Use the bounded query path."})

    assert resp.status_code == 200
    assert calls[0]["guidance_scope"] == "query_hint"


# --------------------------------------------------------------------------- #
# Metric definitions — ADR-0066 read / propose / preview routes
# --------------------------------------------------------------------------- #

_METRIC_DOC = {
    "id": "line-utilization",
    "kind": "direct",
    "label": "Line Utilization",
    "synonyms": ["LU"],
    "unit": "%",
    "concept": "neutral-concept",
    "aggregation": "sum",
    "max_exclusive": False,
}


def _install_write_tripwires(monkeypatch):
    """Every route advertised as read-only must survive write seams that fail."""
    def unexpected_write(*args, **kwargs):
        raise AssertionError("read-only metric route attempted a write")

    monkeypatch.setattr(vr, "_neptune_write", unexpected_write)
    monkeypatch.setattr(self_learning_mod, "_put_item", unexpected_write)
    monkeypatch.setattr(self_learning_mod, "_update_status", unexpected_write)


def test_get_metrics_unifies_all_four_statuses_and_writes_nothing(
        client, monkeypatch):
    import agents.metric_definition as metric_definition_mod
    import agents.metric_definition_store as metric_store_mod

    accepted = dict(_METRIC_DOC, id="accepted-metric", label="Accepted Metric")
    pending = dict(_METRIC_DOC, id="pending-metric", label="Pending Metric")
    monkeypatch.setattr(metric_store_mod, "load_definitions",
                        lambda: {"accepted-metric": accepted})
    monkeypatch.setattr(metric_store_mod, "shadowed_or_invalid", lambda: [
        {"id": "shadowed-metric", "reason": "code-id collision"},
    ])
    monkeypatch.setattr(self_learning_mod, "_query_by_status", lambda *a, **kw: [
        {"mapping_id": "metric-proposal-1", "proposed_type": "metric_definition",
         "status": "pending", "source": "user", "to_value": "pending-metric",
         "query_context": metric_definition_mod.canonical_json(pending)},
        {"mapping_id": "other-proposal", "proposed_type": "value_mapping",
         "status": "pending"},
    ])
    _install_write_tripwires(monkeypatch)

    resp = client.get("/api/admin/vocabulary/metrics")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == len(body["metrics"])
    by_status = {status: [row for row in body["metrics"]
                          if row["status"] == status]
                 for status in ("code", "accepted", "pending", "shadowed")}
    assert all(by_status.values())
    accepted_row = by_status["accepted"][0]
    assert accepted_row["id"] == "accepted-metric"
    assert accepted_row["source"] == "graph"
    assert accepted_row["editable"] is True
    assert accepted_row["formula"] == "sum(concept:neutral-concept)"
    pending_row = by_status["pending"][0]
    assert pending_row["id"] == "pending-metric"
    assert pending_row["mapping_id"] == "metric-proposal-1"
    assert pending_row["source"] == "user"
    shadowed_row = by_status["shadowed"][0]
    assert shadowed_row["id"] == "shadowed-metric"
    assert shadowed_row["reason"] == "code-id collision"
    assert all(row["editable"] is False for row in by_status["code"])


def test_get_metrics_uses_read_only_pending_query_not_expiring_review_helper(
        client, monkeypatch):
    import agents.metric_definition_store as metric_store_mod

    monkeypatch.setattr(metric_store_mod, "load_definitions", lambda: {})
    monkeypatch.setattr(metric_store_mod, "shadowed_or_invalid", lambda: [])
    calls = []
    monkeypatch.setattr(
        self_learning_mod, "_query_by_status",
        lambda status, newest_first=False: calls.append(
            (status, newest_first)) or [])
    monkeypatch.setattr(
        self_learning_mod, "get_pending_mappings",
        lambda: (_ for _ in ()).throw(
            AssertionError("side-effecting generic review helper called")))
    _install_write_tripwires(monkeypatch)

    resp = client.get("/api/admin/vocabulary/metrics")

    assert resp.status_code == 200
    assert calls == [("pending", True)]


def test_propose_metric_definition_files_pending_user_proposal(
        client, monkeypatch):
    import json

    calls = []
    monkeypatch.setattr(
        self_learning_mod, "_metric_definition_context",
        lambda: (frozenset({"neutral-concept"}), frozenset(),
                 frozenset(), {}))
    monkeypatch.setattr(
        self_learning_mod, "log_proposed_mapping",
        lambda **kwargs: calls.append(kwargs) or "metric-proposal-2")
    monkeypatch.setattr(
        self_learning_mod, "accept_mapping",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("metrics/propose must never accept immediately")))

    resp = client.post("/api/admin/vocabulary/metrics/propose", json=_METRIC_DOC)

    assert resp.status_code == 200
    assert resp.json() == {"status": "pending", "mapping_id": "metric-proposal-2"}
    assert len(calls) == 1
    call = calls[0]
    assert call["proposed_type"] == "metric_definition"
    assert call["source"] == "user"
    assert call["provenance"] == "human-asserted"
    assert call["to_value"] == "line-utilization"
    assert call["from_system"] == call["from_field"] == call["from_value"] == ""
    assert call["to_system"] == call["to_field"] == call["concept"] == ""
    assert json.loads(call["query_context"])["id"] == "line-utilization"
    assert call["confidence"] == 1.0
    assert "accept" not in call


@pytest.mark.parametrize("body, offending_field", [
    ({**_METRIC_DOC, "label": ""}, "label"),
    ({**_METRIC_DOC, "id": "inspection-count"}, "id"),
])
def test_propose_metric_definition_invalid_or_code_collision_is_fail_loud_400(
        client, monkeypatch, body, offending_field):
    calls = []
    monkeypatch.setattr(
        self_learning_mod, "_metric_definition_context",
        lambda: (frozenset({"neutral-concept"}),
                 frozenset({"inspection-count"}),
                 frozenset({"inspection-count"}), {}))
    monkeypatch.setattr(
        self_learning_mod, "log_proposed_mapping",
        lambda **kwargs: calls.append(kwargs) or "must-not-file")

    resp = client.post("/api/admin/vocabulary/metrics/propose", json=body)

    assert resp.status_code == 400
    assert offending_field in resp.json()["error"]
    assert calls == []


@pytest.mark.parametrize("clause, offending_field", [
    ({"concept": "neutral-concept", "op": "in", "values": [["x"]]},
     "filter[0].values"),
    ({"concept": "neutral-concept", "op": "in", "values": [{"x": 1}]},
     "filter[0].values"),
    ({"concept": "neutral-concept", "op": "eq", "value": 10 ** 400},
     "filter[0].value"),
])
def test_propose_metric_definition_malformed_filter_literal_is_400(
        client, monkeypatch, clause, offending_field):
    """ADR-0075: a malformed predicate literal is a named 400, never a 500."""
    calls = []
    monkeypatch.setattr(
        self_learning_mod, "_metric_definition_context",
        lambda: (frozenset({"neutral-concept"}), frozenset(), frozenset(), {}))
    monkeypatch.setattr(
        self_learning_mod, "log_proposed_mapping",
        lambda **kwargs: calls.append(kwargs) or "must-not-file")
    body = {**_METRIC_DOC, "aggregation": "count", "filter": [clause]}

    resp = client.post("/api/admin/vocabulary/metrics/propose", json=body)

    assert resp.status_code == 400
    assert offending_field in resp.json()["error"]
    assert calls == []


def test_preview_metric_definition_delegates_and_writes_nothing(
        client, monkeypatch):
    expected = {
        "current": None,
        "proposed": _METRIC_DOC,
        "diff": {"formula": {"from": None,
                              "to": "sum(concept:neutral-concept)"}},
        "validation": {"valid": True, "error": ""},
        "collision": False,
    }
    calls = []
    monkeypatch.setattr(
        self_learning_mod, "preview_metric_definition_mapping",
        lambda mapping_id: calls.append(mapping_id) or expected)
    _install_write_tripwires(monkeypatch)

    resp = client.get(
        "/api/admin/vocabulary/metrics/metric-proposal-3/preview")

    assert resp.status_code == 200
    assert resp.json() == expected
    assert calls == ["metric-proposal-3"]


@pytest.mark.parametrize("helper_status, expected_status", [(404, 404), (409, 409)])
def test_preview_metric_definition_preserves_helper_error_semantics(
        client, monkeypatch, helper_status, expected_status):
    monkeypatch.setattr(
        self_learning_mod, "preview_metric_definition_mapping",
        lambda mapping_id: {"error": "not_available",
                            "http_status": helper_status})
    _install_write_tripwires(monkeypatch)

    resp = client.get("/api/admin/vocabulary/metrics/no-such/preview")

    assert resp.status_code == expected_status
    assert resp.json() == {"error": "not_available"}


def test_metric_router_adds_no_accept_path():
    metric_routes = [route for route in vr.router.routes
                     if "/metrics" in route.path]
    paths_and_methods = {
        (route.path, method)
        for route in metric_routes
        for method in (route.methods or set())
    }
    assert ("/api/admin/vocabulary/metrics", "GET") in paths_and_methods
    assert ("/api/admin/vocabulary/metrics/propose", "POST") in paths_and_methods
    assert ("/api/admin/vocabulary/metrics/{mapping_id}/preview", "GET") \
        in paths_and_methods
    assert not any("accept" in path for path, _method in paths_and_methods)
    assert not any(method in {"PUT", "PATCH", "DELETE"}
                   for _path, method in paths_and_methods)
