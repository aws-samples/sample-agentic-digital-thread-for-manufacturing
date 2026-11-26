# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the bulk proposed-mapping admin endpoints.

  POST /api/proposed-mappings/accept-high-confidence
  POST /api/proposed-mappings/reject-degenerate
  POST /api/proposed-mappings/adjudicate-batch   (ADR-0063 D4)

No AWS, no Neptune, no network: a minimal FastAPI app registers the real
handlers from ``agents.server`` and drives them via ``TestClient`` (mirrors the
minimal-app style in ``test_correction_route.py``). ``self_learning``'s
get_pending_mappings / accept_mapping / reject_mapping are monkeypatched. The
scorer + degeneracy predicate are the REAL shared functions (single source of
truth), and D4 delegates each reloaded id to the existing single-row lifecycle
core rather than reimplementing accept/reject policy.
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import agents.proposal_batch as proposal_batch
import agents.self_learning as self_learning
import agents.server as server
from agents.proposal_scoring import family_counts, family_key
from agents.server import accept_high_confidence_mappings, reject_degenerate_mappings_route

_UUID = "12345678-1234-1234-1234-123456789abc"


def _client():
    app = FastAPI()
    app.add_api_route("/api/proposed-mappings/accept-high-confidence",
                      accept_high_confidence_mappings, methods=["POST"])
    app.add_api_route("/api/proposed-mappings/reject-degenerate",
                      reject_degenerate_mappings_route, methods=["POST"])
    return TestClient(app)


@pytest.fixture(autouse=True)
def _authoritative_batch_rows(monkeypatch):
    """Every batch id is reloaded from backend state before mutation."""
    rows: dict[str, dict] = {}

    def _get(mid):
        return rows.get(mid, {"mapping_id": mid, "status": "pending",
                              "proposed_type": "value_mapping",
                              "from_value": "left", "to_value": "right"})

    monkeypatch.setattr(self_learning, "get_mapping", _get)
    return rows


# --------------------------------------------------------------------------- #
# accept-high-confidence
# --------------------------------------------------------------------------- #
def test_accept_high_confidence_accepts_only_high_vm_and_ir(monkeypatch):
    pending = [
        # high value_mapping (UUID + conf>=0.85) → accepted
        {"mapping_id": "vm-high", "proposed_type": "value_mapping",
         "to_value": _UUID, "from_value": "x", "confidence": "0.9"},
        # medium value_mapping (semantic) → skipped
        {"mapping_id": "vm-med", "proposed_type": "value_mapping",
         "to_value": "not-uuid", "from_value": "x", "confidence": "0.82"},
        # identity_rule always scores medium → skipped
        {"mapping_id": "ir-1", "proposed_type": "identity_rule"},
        # field_annotation → wrong type, skipped even if it scored high (it can't)
        {"mapping_id": "fa-1", "proposed_type": "field_annotation", "from_field": "a.b"},
        # join_path high but NOT a permitted type → skipped
        {"mapping_id": "jp-1", "proposed_type": "join_path",
         "from_field": "a.b", "to_field": "c.d"},
    ]
    accepted_ids: list[str] = []

    def _accept(mid):
        accepted_ids.append(mid)
        return {"status": "accepted"}

    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: pending)
    monkeypatch.setattr(self_learning, "accept_mapping", _accept)
    # ADR-0051 P4: UUID targets need a passing graph check to auto-accept —
    # stubbed True here; this test pins the route wiring + score gate only.
    monkeypatch.setattr(
        self_learning, "_verify_target",
        lambda item: item.update({"verified": "true",
                                  "verified_scope": "system"}) or True)

    resp = _client().post("/api/proposed-mappings/accept-high-confidence")
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1, "failed": 0}
    assert accepted_ids == ["vm-high"]


def test_accept_high_confidence_counts_failures(monkeypatch):
    pending = [
        {"mapping_id": "vm-1", "proposed_type": "value_mapping",
         "to_value": _UUID, "from_value": "x", "confidence": "0.9"},
        {"mapping_id": "vm-2", "proposed_type": "value_mapping",
         "to_value": _UUID, "from_value": "x", "confidence": "0.95"},
    ]

    def _accept(mid):
        if mid == "vm-2":
            raise RuntimeError("neptune down")
        return {"status": "accepted"}

    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: pending)
    monkeypatch.setattr(self_learning, "accept_mapping", _accept)
    # ADR-0051 P4 verifier stubbed True — this test pins failure counting.
    monkeypatch.setattr(
        self_learning, "_verify_target",
        lambda item: item.update({"verified": "true",
                                  "verified_scope": "system"}) or True)

    resp = _client().post("/api/proposed-mappings/accept-high-confidence")
    assert resp.json() == {"accepted": 1, "failed": 1}


def test_accept_high_confidence_error_dict_counts_failed(monkeypatch):
    pending = [
        {"mapping_id": "vm-1", "proposed_type": "value_mapping",
         "to_value": _UUID, "from_value": "x", "confidence": "0.9"},
    ]
    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: pending)
    monkeypatch.setattr(self_learning, "accept_mapping",
                        lambda mid: {"error": "not found"})
    # ADR-0051 P4 verifier stubbed True — this test pins error-dict counting.
    monkeypatch.setattr(
        self_learning, "_verify_target",
        lambda item: item.update({"verified": "true",
                                  "verified_scope": "system"}) or True)
    resp = _client().post("/api/proposed-mappings/accept-high-confidence")
    assert resp.json() == {"accepted": 0, "failed": 1}


# --------------------------------------------------------------------------- #
# reject-degenerate
# --------------------------------------------------------------------------- #
def test_reject_degenerate_rejects_only_degenerate_field_annotations(monkeypatch):
    pending = [
        # degenerate: comma-list field artifact
        {"mapping_id": "fa-bad", "proposed_type": "field_annotation",
         "from_field": "orders.id, name", "to_field": "orders.id, name",
         "query_context": "Auto-analysis: these are the columns"},
        # good field_annotation → kept
        {"mapping_id": "fa-good", "proposed_type": "field_annotation",
         "from_field": "orders.status", "to_field": "orders.status",
         "query_context": "Auto-analysis: enum OPEN/CLOSED, a type gotcha"},
        # malformed value_mapping with empty endpoints → rejected
        {"mapping_id": "vm-1", "proposed_type": "value_mapping",
         "to_value": _UUID, "from_value": "x", "confidence": "0.9"},
        # degenerate: empty annotation
        {"mapping_id": "fa-empty", "proposed_type": "field_annotation",
         "from_field": "orders.status", "to_field": "orders.status",
         "query_context": "Auto-analysis: "},
    ]
    rejected_ids: list[str] = []

    def _reject(mid):
        rejected_ids.append(mid)
        return {"status": "rejected"}

    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: pending)
    monkeypatch.setattr(self_learning, "reject_mapping", _reject)

    resp = _client().post("/api/proposed-mappings/reject-degenerate")
    assert resp.status_code == 200
    assert resp.json() == {"rejected": 3}
    assert sorted(rejected_ids) == ["fa-bad", "fa-empty", "vm-1"]


def test_reject_degenerate_rejects_empty_non_insight_endpoints(monkeypatch):
    pending = [
        {"mapping_id": "vm-empty-from", "proposed_type": "value_mapping",
         "from_field": "", "to_field": "entities.id"},
        {"mapping_id": "jp-empty-to", "proposed_type": "join_path",
         "from_field": "records.id", "to_field": "  "},
        {"mapping_id": "insight-empty", "proposed_type": "insight",
         "from_field": "", "to_field": ""},
        {"mapping_id": "vm-valid", "proposed_type": "value_mapping",
         "from_field": "records.id", "to_field": "entities.id"},
    ]
    rejected_ids: list[str] = []
    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: pending)
    monkeypatch.setattr(
        self_learning, "reject_mapping",
        lambda mid: rejected_ids.append(mid) or {"status": "rejected"},
    )

    resp = _client().post("/api/proposed-mappings/reject-degenerate")

    assert resp.json() == {"rejected": 2}
    assert rejected_ids == ["vm-empty-from", "jp-empty-to"]


def test_reject_degenerate_none_pending(monkeypatch):
    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: [])
    monkeypatch.setattr(self_learning, "reject_mapping",
                        lambda mid: {"status": "rejected"})
    resp = _client().post("/api/proposed-mappings/reject-degenerate")
    assert resp.json() == {"rejected": 0}


# --------------------------------------------------------------------------- #
# adjudicate-batch (ADR-0063 D4) — EXPLICIT-LIST family adjudication.
#
# Batching is TRANSPORT: every listed id runs the SAME single-row accept/reject
# route handler, so gates, damping and tombstone semantics stay exactly where
# they already live. These tests pin that (the seam asserted is the single-row
# ``self_learning`` call), the absence of any wildcard/predicate affordance,
# the per-request bound, and honest per-id partial failure.
# --------------------------------------------------------------------------- #
def _batch_client(is_admin: bool | None = True):
    """The batch handler mounted WITH the real steward dependency, behind a
    middleware that stamps ``is_admin`` the way AuthMiddleware does. ``None``
    mounts no middleware, so the attribute is ABSENT (fail-closed posture)."""
    app = FastAPI()
    if is_admin is not None:
        @app.middleware("http")
        async def _stamp(request, call_next):  # noqa: ANN001
            request.state.is_admin = is_admin
            return await call_next(request)
    app.add_api_route("/api/proposed-mappings/adjudicate-batch",
                      server.adjudicate_mappings_batch, methods=["POST"],
                      dependencies=[Depends(server._check_steward)])
    return TestClient(app)


def _post_batch(action: str, ids, is_admin: bool | None = True, **extra):
    body = {"action": action, "mapping_ids": ids, **extra}
    return _batch_client(is_admin).post(
        "/api/proposed-mappings/adjudicate-batch", json=body)


def test_batch_reject_runs_every_listed_id_through_the_single_row_path(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        self_learning, "reject_mapping",
        lambda mid: seen.append(mid) or {"status": "rejected", "mapping_id": mid})

    resp = _post_batch("reject", ["m-1", "m-2", "m-3"])

    assert resp.status_code == 200
    body = resp.json()
    assert seen == ["m-1", "m-2", "m-3"]          # the single-row seam, per id
    assert (body["requested"], body["succeeded"], body["failed"]) == (3, 0 + 3, 0)
    assert body["failed_ids"] == []
    assert [r["mapping_id"] for r in body["results"]] == ["m-1", "m-2", "m-3"]
    assert all(r["ok"] and r["status"] == "rejected" for r in body["results"])


def test_batch_accept_runs_every_listed_id_through_the_single_row_path(monkeypatch):
    calls: list[tuple] = []

    def _accept(mid, **kwargs):
        calls.append((mid, tuple(sorted(kwargs))))
        return {"status": "accepted", "mapping_id": mid}

    monkeypatch.setattr(server, "_request_actor", lambda _request: "steward-1")
    monkeypatch.setattr(self_learning, "accept_mapping", _accept)

    body = _post_batch("accept", ["m-1", "m-2"]).json()

    assert [c[0] for c in calls] == ["m-1", "m-2"]
    # No force/override kwarg is invented by the batch — it passes exactly what
    # the single-row route passes (the provenance actor), so no gate is skipped.
    assert {c[1] for c in calls} == {("actor",)}
    assert body["succeeded"] == 2 and body["failed_ids"] == []


def test_batch_duplicate_ids_collapse(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        self_learning, "reject_mapping",
        lambda mid: seen.append(mid) or {"status": "rejected"})

    body = _post_batch("reject", ["m-1", "m-1", "m-2"]).json()

    assert seen == ["m-1", "m-2"]
    assert body["requested"] == 2


# ── No wildcard, no predicate — there is no such affordance ───────────────── #
NO_PREDICATE_BODIES = [
    {"action": "reject", "filter": {"family_key": "value_mapping|m|a->b"}},
    {"action": "reject", "family_key": "value_mapping|m|a->b"},
    {"action": "reject", "where": {"method": "semantic_similarity"}},
    {"action": "reject", "all": True},
    {"action": "reject", "mapping_ids": ["*"]},
    {"action": "reject", "mapping_ids": ["m-%"]},
    {"action": "reject", "mapping_ids": "all"},
    {"action": "reject", "mapping_ids": []},
    {"action": "reject", "mapping_ids": ["", "  "]},
    {"action": "reject", "mapping_ids": [None]},
    {"action": "tombstone", "mapping_ids": ["m-1"]},
]


@pytest.mark.parametrize("body", NO_PREDICATE_BODIES,
                         ids=[str(i) for i in range(len(NO_PREDICATE_BODIES))])
def test_batch_refuses_wildcard_or_predicate_selection(monkeypatch, body):
    def _boom(*_a, **_k):
        raise AssertionError("no row may be adjudicated by a predicate")

    monkeypatch.setattr(self_learning, "reject_mapping", _boom)
    monkeypatch.setattr(self_learning, "accept_mapping", _boom)

    resp = _batch_client().post("/api/proposed-mappings/adjudicate-batch", json=body)

    assert resp.status_code == 400, resp.text


def test_batch_body_contract_has_no_predicate_key():
    # The accepted body keys ARE the contract: adding a predicate/wildcard
    # selector would have to change this set, which this pin refuses.
    assert set(server._BATCH_BODY_KEYS) == {"action", "mapping_ids"}
    assert set(server._BATCH_ACTIONS) == {"accept", "reject"}


def test_batch_bound_is_enforced(monkeypatch):
    monkeypatch.setattr(self_learning, "reject_mapping",
                        lambda mid: {"status": "rejected"})
    limit = server._BATCH_ADJUDICATION_MAX

    at_limit = _post_batch("reject", [f"m-{i}" for i in range(limit)])
    over = _post_batch("reject", [f"m-{i}" for i in range(limit + 1)])

    assert at_limit.status_code == 200
    assert at_limit.json()["requested"] == limit
    assert over.status_code == 400
    assert over.json()["detail"] == {"code": "batch_too_large"}


def test_batch_partial_failure_is_honest_and_names_the_failures(monkeypatch):
    def _reject(mid):
        if mid == "m-missing":
            return {"error": "mapping not found"}
        if mid == "m-boom":
            raise RuntimeError("store unavailable")
        return {"status": "rejected"}

    monkeypatch.setattr(self_learning, "reject_mapping", _reject)

    body = _post_batch(
        "reject", ["m-1", "m-missing", "m-2", "m-boom", "m-3"]).json()

    assert (body["requested"], body["succeeded"], body["failed"]) == (5, 3, 2)
    assert body["failed_ids"] == ["m-missing", "m-boom"]
    outcomes = {r["mapping_id"]: r for r in body["results"]}
    assert outcomes["m-missing"] == {
        "mapping_id": "m-missing", "ok": False, "error_code": "not_found"}
    assert outcomes["m-boom"] == {
        "mapping_id": "m-boom", "ok": False, "error_code": "action_failed"}
    for good in ("m-1", "m-2", "m-3"):
        assert outcomes[good]["ok"] is True   # one bad id never fails the rest



def test_batch_failure_codes_are_a_closed_public_taxonomy():
    expected = {
        "not_found", "not_pending", "specialized_accept_required",
        "non_actionable", "eligibility_unavailable", "backend_unavailable",
        "action_rejected", "action_failed",
    }

    assert proposal_batch.RESULT_ERROR_CODES == frozenset(expected)
    assert proposal_batch._failure("m-1", "raw-backend-detail") == {
        "mapping_id": "m-1", "ok": False, "error_code": "action_failed"}

def test_batch_does_not_force_apply_a_row_the_single_path_blocks(monkeypatch):
    # The single-row accept route turns a fail-loud ValueError into a 400 with
    # the actionable message. The batch inherits that verdict verbatim: the
    # blocked row is reported as failed and NOT applied, while its siblings
    # proceed. No gate is re-implemented, so none can be bypassed here.
    applied: list[str] = []

    def _accept(mid, **_kwargs):
        if mid == "m-blocked":
            raise ValueError("corrected column not in the discovered schema")
        applied.append(mid)
        return {"status": "accepted"}

    monkeypatch.setattr(self_learning, "accept_mapping", _accept)

    body = _post_batch("accept", ["m-blocked", "m-ok"]).json()

    assert applied == ["m-ok"]
    assert body["failed_ids"] == ["m-blocked"]
    blocked = next(r for r in body["results"] if r["mapping_id"] == "m-blocked")
    assert blocked == {"mapping_id": "m-blocked", "ok": False,
                       "error_code": "action_rejected"}
    assert "discovered schema" not in str(blocked)


def test_batch_is_steward_gated(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("no adjudication behind a failed steward gate")

    monkeypatch.setattr(self_learning, "reject_mapping", _boom)
    for is_admin in (False, None):   # explicit non-admin, and NO verdict at all
        resp = _post_batch("reject", ["m-1"], is_admin=is_admin)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Steward (admin) role required"


def test_registered_batch_route_carries_the_shared_steward_dependency():
    route = next(r for r in server.app.routes
                 if getattr(r, "path", "") ==
                 "/api/proposed-mappings/adjudicate-batch")
    assert server._check_steward in [d.dependency for d in route.dependencies]


# --------------------------------------------------------------------------- #
# Grouping is COMPUTED ON READ (proposal_scoring.family_key / family_counts).
# --------------------------------------------------------------------------- #
def _row(**over):
    row = {"proposed_type": "value_mapping", "method": "semantic_similarity",
           "from_field": "records.ref", "to_field": "entities.ref",
           "status": "pending"}
    row.update(over)
    return row


def test_family_key_grain_is_type_method_and_field_pair():
    base = _row()
    assert family_key(base) == \
        "value_mapping|semantic_similarity|records.ref->entities.ref"
    # Normalized: case and surrounding whitespace do not split a family.
    assert family_key(_row(method=" Semantic_Similarity ",
                           from_field="Records.Ref ")) == family_key(base)
    # Each grain component splits the family.
    assert family_key(_row(method="hierarchy_position")) != family_key(base)
    assert family_key(_row(to_field="entities.other")) != family_key(base)
    assert family_key(_row(proposed_type="identity_rule")) != family_key(base)
    # NOT in the grain: endpoint systems and per-row values.
    assert family_key(_row(from_system="sys_a", confidence="0.9")) == family_key(base)


def test_family_counts_group_distinct_families_separately():
    rows = [
        _row(), _row(), _row(),                                  # family A ×3
        _row(method="hierarchy_position"),                       # family B ×1
        _row(to_field="entities.other"),                         # family C ×1
        _row(status="accepted"),                                 # not pending
        _row(status="rejected", method="hierarchy_position"),     # not pending
        {"method": "semantic_similarity", "from_field": "records.ref",
         "to_field": "entities.ref"},                            # legacy → pending
    ]
    counts = family_counts(rows)
    assert counts[family_key(_row())] == 4     # 3 explicit + 1 legacy no-status
    assert counts[family_key(_row(method="hierarchy_position"))] == 1
    assert counts[family_key(_row(to_field="entities.other"))] == 1
    assert sum(counts.values()) == 6


def test_family_enrichment_is_additive_and_absent_without_pending_peers():
    pending = family_counts([_row(), _row()])
    row = _row()
    server._attach_family_enrichment(row, pending)
    assert row["family_key"] == family_key(_row())
    assert row["family_count"] == 2

    lonely = _row(status="accepted", method="value_format_match")
    server._attach_family_enrichment(lonely, pending)
    assert lonely["family_key"]                 # always computed
    assert "family_count" not in lonely         # honest absence, never a 0


def test_get_route_serves_the_family_key_and_count(monkeypatch):
    """The grouping the UI renders comes from the SERVER (computed on read)."""
    import asyncio

    import agents.concept_layer as concept_layer
    import agents.schema_check as schema_check

    rows = [_row(mapping_id="map-0001"), _row(mapping_id="map-0002"),
            _row(mapping_id="map-0003", method="hierarchy_position")]
    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: rows)
    monkeypatch.setattr(self_learning, "known_systems", lambda: [])
    monkeypatch.setattr(concept_layer, "concept_descriptions", lambda _ids: {})
    monkeypatch.setattr(concept_layer, "concept_layer_of", lambda _c: "field")
    monkeypatch.setattr(schema_check, "load_system_schema", lambda sid: None)

    out = asyncio.run(server.get_proposed_mappings_route())

    by_id = {m["mapping_id"]: m for m in out["mappings"]}
    assert by_id["map-0001"]["family_key"] == by_id["map-0002"]["family_key"]
    assert by_id["map-0001"]["family_count"] == 2
    assert by_id["map-0003"]["family_key"] != by_id["map-0001"]["family_key"]
    assert by_id["map-0003"]["family_count"] == 1


# --------------------------------------------------------------------------- #
# Degenerate family grain (live-observed 2026-08-19): narrative findings carry
# no method and no field endpoints, so every one hashed to the SAME key and the
# surface offered "reject all 552 in this family" over rows sharing nothing but
# their type. A family offer is honest only when the grain names something.
# --------------------------------------------------------------------------- #
def test_rows_without_method_or_endpoints_get_no_family():
    from agents.proposal_scoring import family_counts, family_key
    finding = {"proposed_type": "insight", "method": "", "from_field": "",
               "to_field": "", "status": "pending"}
    assert family_key(finding) == ""
    # ...and such rows never accumulate a count, so no batch is ever offered.
    assert family_counts([finding, dict(finding), dict(finding)]) == {}


def test_a_single_grain_component_is_enough_to_discriminate():
    from agents.proposal_scoring import family_key
    # method alone
    assert family_key({"proposed_type": "insight", "method": "value_format_match"})
    # endpoint alone (schema_drift rows carry a field but no method)
    assert family_key({"proposed_type": "schema_drift", "from_field": "t.c"})


def test_enrichment_omits_family_fields_for_a_degenerate_row():
    from agents.server import _attach_family_enrichment
    row = {"proposed_type": "insight", "method": "", "from_field": "",
           "to_field": "", "status": "pending"}
    _attach_family_enrichment(row, {})
    assert "family_key" not in row and "family_count" not in row


def test_enrichment_still_attaches_for_a_discriminating_row():
    from agents.proposal_scoring import family_counts
    from agents.server import _attach_family_enrichment
    rows = [{"proposed_type": "value_mapping", "method": "hierarchy_position",
             "from_field": "a.b", "to_field": "c.d", "status": "pending"}
            for _ in range(3)]
    counts = family_counts(rows)
    _attach_family_enrichment(rows[0], counts)
    assert rows[0]["family_count"] == 3


def test_batch_accept_and_reject_have_separate_action_eligibility(
        monkeypatch, _authoritative_batch_rows):
    _authoritative_batch_rows["guidance-1"] = {
        "mapping_id": "guidance-1", "status": "pending",
        "proposed_type": "guidance_note"}
    accepted: list[str] = []
    rejected: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping",
                        lambda mid, **_k: accepted.append(mid) or {"status": "accepted"})
    monkeypatch.setattr(self_learning, "reject_mapping",
                        lambda mid, **_k: rejected.append(mid) or {"status": "rejected"})

    accept = _post_batch("accept", ["guidance-1"]).json()["results"][0]
    reject = _post_batch("reject", ["guidance-1"]).json()["results"][0]

    assert accept == {"mapping_id": "guidance-1", "ok": False,
                      "error_code": "specialized_accept_required"}
    assert accepted == []
    assert reject["ok"] is True and rejected == ["guidance-1"]


def test_batch_rechecks_backend_status_before_each_action(
        monkeypatch, _authoritative_batch_rows):
    _authoritative_batch_rows["already-decided"] = {
        "mapping_id": "already-decided", "status": "accepted",
        "proposed_type": "value_mapping"}
    called: list[str] = []
    monkeypatch.setattr(self_learning, "reject_mapping",
                        lambda mid, **_k: called.append(mid) or {"status": "rejected"})

    result = _post_batch("reject", ["already-decided"]).json()["results"][0]

    assert result == {"mapping_id": "already-decided", "ok": False,
                      "error_code": "not_pending"}
    assert called == []


def test_batch_never_returns_raw_handler_or_exception_text(
        monkeypatch, _authoritative_batch_rows):
    secret = "database-password=do-not-return"  # nosec B105 - a leak-detection marker, not a credential
    monkeypatch.setattr(
        self_learning, "reject_mapping",
        lambda _mid, **_k: (_ for _ in ()).throw(RuntimeError(secret)))

    result = _post_batch("reject", ["m-1"]).json()["results"][0]

    assert result == {"mapping_id": "m-1", "ok": False,
                      "error_code": "action_failed"}
    assert secret not in str(result)


def test_review_payload_adds_action_eligibility(monkeypatch):
    row = _row(mapping_id="map-eligible", from_value="left", to_value="right")
    gathered = ({}, {}, [], {}, {})
    server._enrich_review_rows([row], gathered)
    assert row["action_eligibility"] == {
        "accept": {"eligible": True}, "reject": {"eligible": True}}

    guidance = _row(mapping_id="map-guidance", proposed_type="guidance_note")
    server._enrich_review_rows([guidance], gathered)
    assert guidance["action_eligibility"]["accept"] == {
        "eligible": False, "code": "specialized_accept_required"}
    assert guidance["action_eligibility"]["reject"] == {"eligible": True}


def test_proposal_batch_functions_stay_focused():
    import inspect
    import agents.proposal_batch as proposal_batch

    offenders = {}
    for name, fn in inspect.getmembers(proposal_batch, inspect.isfunction):
        if fn.__module__ != proposal_batch.__name__:
            continue
        lines = len(inspect.getsourcelines(fn)[0])
        if lines > 50:
            offenders[name] = lines
    assert offenders == {}
