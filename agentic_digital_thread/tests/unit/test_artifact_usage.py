# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Artifact-usage extraction + day-bucketed ledger (W1.3, ADR-0050 usage loop).

- ``artifact_usage.extract_artifact_refs``: rule node ids via the EXISTING
  ``usage_tracking.extract_rule_node_ids`` (reuse pinned by a spy) or the
  caller's ``precomputed_ids`` (runtime-hook idiom — never re-scan); kind
  from the accepted row's ``proposed_type`` when resolvable. Concept refs
  from two TYPED paths only (never regex over free text): the accepted rows
  whose rule fired (row ``concept`` attribute — the production-live path,
  FIX-E/H3) and the optional ``evidence`` object's ``proposals[].concept``.
- ``bump_artifact_usage`` / ``read_artifact_usage``: day-bucketed atomic
  ``ADD count`` rows on the proposals table (``mapping_id = "usage#<kind>#<id>"``,
  ``discovered_at = "YYYY-MM-DD"``, NO ``status`` attribute), 60 s read cache,
  bounded begins_with scan (page cap 20). ALL backend failures swallow to
  0/{}/empty (ADR-0003 F). Behavior identical with RESPONSE_INTEGRITY_MODE
  unset (shadow mode).

Offline: no AWS, no network. Table mocked via
``monkeypatch.setattr(self_learning, "_get_table", ...)``
(``test_usage_tracking.py`` conventions).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import agents.artifact_usage as artifact_usage
import agents.runtime_app as runtime_app
import agents.self_learning as self_learning
import agents.usage_tracking as usage_tracking


NODE_ID = "subj-a1b2c3d4e5f60718"
NODE_ID_2 = "vm-learned-deadbeef"


@pytest.fixture(autouse=True)
def _fresh_cache():
    artifact_usage._reset_read_cache()
    yield
    artifact_usage._reset_read_cache()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _day(offset: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=offset)).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# extract_artifact_refs — rule ids reuse the existing extractor
# --------------------------------------------------------------------------- #
def test_extraction_reuses_usage_tracking_extractor(monkeypatch):
    """REUSE mandate: rule ids MUST come from usage_tracking.extract_rule_node_ids."""
    calls = []

    def spy(texts):
        calls.append(list(texts))
        return {NODE_ID}

    monkeypatch.setattr(usage_tracking, "extract_rule_node_ids", spy)
    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids", lambda ids: [])

    refs = artifact_usage.extract_artifact_refs({"t1": "some result text"}, None)

    assert calls == [["some result text"]]
    assert refs == {("rule", NODE_ID)}


def test_kind_resolved_from_accepted_row_proposed_type(monkeypatch):
    rows = [{"neptune_node_id": NODE_ID, "proposed_type": "value_mapping"}]
    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids", lambda ids: rows)

    refs = artifact_usage.extract_artifact_refs({"t1": f"used {NODE_ID} here"}, None)

    assert refs == {("value_mapping", NODE_ID)}


def test_unresolvable_kind_falls_back_to_rule(monkeypatch):
    # Accepted-row lookup returns a DIFFERENT node — the used one stays 'rule'.
    rows = [{"neptune_node_id": "subj-ffffffffffffffff", "proposed_type": "join_path"}]
    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids", lambda ids: rows)

    refs = artifact_usage.extract_artifact_refs({"t1": f"used {NODE_ID} here"}, None)

    assert refs == {("rule", NODE_ID)}


def test_prefetched_accepted_rows_skip_lookup(monkeypatch):
    def boom(ids):
        raise AssertionError("lookup must be skipped when rows are prefetched")

    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids", boom)
    rows = [{"neptune_node_id": NODE_ID, "proposed_type": "identity_rule"}]

    refs = artifact_usage.extract_artifact_refs(
        {"t1": f"via {NODE_ID}"}, None, accepted_rows=rows)

    assert refs == {("identity_rule", NODE_ID)}


def test_kind_lookup_failure_swallows_to_rule(monkeypatch):
    def boom(ids):
        raise RuntimeError("dynamo down")

    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids", boom)

    refs = artifact_usage.extract_artifact_refs({"t1": f"via {NODE_ID}"}, None)

    assert refs == {("rule", NODE_ID)}


# --------------------------------------------------------------------------- #
# extract_artifact_refs — concept ids from TYPED evidence fields only
# --------------------------------------------------------------------------- #
def test_concepts_from_typed_proposals_on_evidence():
    evidence = SimpleNamespace(proposals=[
        SimpleNamespace(concept="inventory_level"),
        SimpleNamespace(concept="  work_order  "),  # stripped
        SimpleNamespace(concept=""),                # empty — skipped
        SimpleNamespace(concept=None),              # untyped — skipped
    ])

    refs = artifact_usage.extract_artifact_refs({}, evidence)

    assert refs == {("concept", "inventory_level"), ("concept", "work_order")}


def test_concept_refs_derived_from_accepted_rows_typed_attr(monkeypatch):
    """FIX-E/H3: a used rule's typed ``concept`` attribute emits a concept ref."""
    def boom(ids):
        raise AssertionError("lookup must be skipped when rows are prefetched")

    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids", boom)
    rows = [{"neptune_node_id": NODE_ID, "proposed_type": "value_mapping",
             "concept": "inventory_level"}]

    refs = artifact_usage.extract_artifact_refs(
        {"t1": f"joined via {NODE_ID}"}, None, accepted_rows=rows)

    assert refs == {("value_mapping", NODE_ID), ("concept", "inventory_level")}


def test_concept_refs_derived_via_best_effort_lookup(monkeypatch):
    """No prefetched rows → the ONE best-effort lookup still yields concepts."""
    rows = [{"neptune_node_id": NODE_ID, "proposed_type": "crosswalk",
             "concept": "  work_order  "}]  # stripped
    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids", lambda ids: rows)

    refs = artifact_usage.extract_artifact_refs({"t1": f"used {NODE_ID}"}, None)

    assert refs == {("crosswalk", NODE_ID), ("concept", "work_order")}


def test_no_accepted_rows_yields_rule_only_refs(monkeypatch):
    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids", lambda ids: [])

    refs = artifact_usage.extract_artifact_refs({"t1": f"used {NODE_ID}"}, None)

    assert refs == {("rule", NODE_ID)}


def test_concept_of_a_rule_that_did_not_fire_is_not_emitted(monkeypatch):
    # A row for a DIFFERENT node id must contribute neither kind nor concept.
    rows = [{"neptune_node_id": "subj-ffffffffffffffff",
             "proposed_type": "join_path", "concept": "location"}]
    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids", lambda ids: rows)

    refs = artifact_usage.extract_artifact_refs({"t1": f"used {NODE_ID}"}, None)

    assert refs == {("rule", NODE_ID)}


def test_row_without_concept_attribute_yields_rule_ref_only(monkeypatch):
    rows = [{"neptune_node_id": NODE_ID, "proposed_type": "value_mapping"}]
    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids", lambda ids: rows)

    refs = artifact_usage.extract_artifact_refs({"t1": f"used {NODE_ID}"}, None)

    assert refs == {("value_mapping", NODE_ID)}


def test_precomputed_ids_skip_the_extractor(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("extract_rule_node_ids must NOT run with precomputed_ids")

    monkeypatch.setattr(usage_tracking, "extract_rule_node_ids", _boom)
    rows = [{"neptune_node_id": NODE_ID, "proposed_type": "value_mapping",
             "concept": "quantity"}]

    refs = artifact_usage.extract_artifact_refs(
        {"t1": "text never scanned"}, None,
        accepted_rows=rows, precomputed_ids={NODE_ID})

    assert refs == {("value_mapping", NODE_ID), ("concept", "quantity")}
    # An EMPTY precomputed set is honored — no silent rescan.
    assert artifact_usage.extract_artifact_refs(
        {"t1": f"has {NODE_ID}"}, None, precomputed_ids=set()) == set()


def test_concepts_never_extracted_from_free_text(monkeypatch):
    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids", lambda ids: [])
    # Concept words in tool-result FREE TEXT must never become refs.
    refs = artifact_usage.extract_artifact_refs(
        {"t1": "the inventory_level concept and location were used"}, None)
    assert refs == set()


def test_evidence_none_and_dict_shaped_evidence(monkeypatch):
    assert artifact_usage.extract_artifact_refs({}, None) == set()
    # Dict-shaped evidence (runtime-context dict) is accepted too.
    refs = artifact_usage.extract_artifact_refs(
        {}, {"proposals": [{"concept": "quantity"}]})
    assert refs == {("concept", "quantity")}


def test_hook_chain_runs_the_extractor_at_most_once_per_turn(monkeypatch):
    """FIX-E (medium): the rule hook forwards its computed ids to the artifact
    sibling — the turn's tool results are scanned AT MOST once end-to-end."""
    calls = []
    real_extract = usage_tracking.extract_rule_node_ids

    def counting_spy(texts):
        calls.append(list(texts))
        return real_extract(texts)

    monkeypatch.setattr(usage_tracking, "extract_rule_node_ids", counting_spy)
    rows = [{"neptune_node_id": NODE_ID, "proposed_type": "value_mapping",
             "concept": "inventory_level"}]
    monkeypatch.setattr(self_learning, "record_rule_usage",
                        lambda _ids, **_k: 1)
    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids",
                        lambda ids: rows)
    bumped_refs = []
    bumped_evidence = []
    monkeypatch.setattr(
        artifact_usage, "bump_artifact_usage",
        lambda refs, evidence="": (bumped_refs.append(set(refs)),
                                   bumped_evidence.append(evidence),
                                   len(refs))[-1])

    runtime_app._record_rule_usage_hook(
        {"t1": f"joined via {NODE_ID}"}, "compare ERP and MES yields")

    assert len(calls) == 1  # ONE scan for the whole rule+artifact sequence
    # And the concept ref went live on the default (evidence=None) path.
    assert bumped_refs == [{("value_mapping", NODE_ID),
                            ("concept", "inventory_level")}]
    # Default evidence class: the legacy tool-text extraction (ADR-0052 (b)).
    assert bumped_evidence == ["tool-text"]


def test_hook_chain_with_precomputed_ids_never_scans(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("no scan may happen on the precomputed path")

    monkeypatch.setattr(usage_tracking, "extract_rule_node_ids", _boom)
    rows = [{"neptune_node_id": NODE_ID, "proposed_type": "value_mapping",
             "concept": "quantity"}]
    monkeypatch.setattr(self_learning, "record_rule_usage",
                        lambda _ids, **_k: 1)
    bumped_refs = []
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage",
                        lambda refs, **_k: bumped_refs.append(set(refs)) or len(refs))

    runtime_app._record_rule_usage_hook(
        {"t1": f"joined via {NODE_ID}"}, "compare ERP and MES yields",
        precomputed_ids={NODE_ID}, prefetched_rows=rows)

    assert bumped_refs == [{("value_mapping", NODE_ID),
                            ("concept", "quantity")}]


def test_extraction_swallows_hostile_evidence(monkeypatch):
    class Hostile:
        @property
        def proposals(self):
            raise RuntimeError("boom")

    assert artifact_usage.extract_artifact_refs({}, Hostile()) == set()


def test_shadow_mode_env_unset_and_set_identical(monkeypatch):
    """Behavior must be identical with RESPONSE_INTEGRITY_MODE unset (shadow)."""
    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids", lambda ids: [])
    evidence = SimpleNamespace(proposals=[SimpleNamespace(concept="quantity")])
    tool_results = {"t1": f"via {NODE_ID}"}

    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    unset_refs = artifact_usage.extract_artifact_refs(tool_results, evidence)

    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    set_refs = artifact_usage.extract_artifact_refs(tool_results, evidence)

    assert unset_refs == set_refs == {("rule", NODE_ID), ("concept", "quantity")}


def test_module_is_import_pure():
    # No boto3/pg8000 at import time (ADR-0020/0021) — self_learning is
    # imported lazily inside the ledger functions only.
    src = open(artifact_usage.__file__).read()
    assert "import boto3" not in src
    assert "import pg8000" not in src and "import pgdriver" not in src


# --------------------------------------------------------------------------- #
# bump_artifact_usage — atomic ADD on today's day bucket, NO status attribute
# --------------------------------------------------------------------------- #
class RecordingTable:
    """Fake proposals table: update_item/scan spies, optional failures."""

    def __init__(self, scan_pages=None, *, raise_on_update=False, raise_on_scan=False):
        self._scan_pages = scan_pages or [[]]
        self._raise_on_update = raise_on_update
        self._raise_on_scan = raise_on_scan
        self.updates = []
        self.scan_calls = []

    def update_item(self, **kwargs):
        if self._raise_on_update:
            raise RuntimeError("update boom")
        self.updates.append(kwargs)

    def scan(self, **kwargs):
        if self._raise_on_scan:
            raise RuntimeError("scan boom")
        self.scan_calls.append(kwargs)
        page_idx = len(self.scan_calls) - 1
        items = (self._scan_pages[page_idx]
                 if page_idx < len(self._scan_pages) else [])
        out = {"Items": items}
        if page_idx < len(self._scan_pages) - 1:
            out["LastEvaluatedKey"] = {"mapping_id": f"page-{page_idx}"}
        return out


class AlwaysMoreTable(RecordingTable):
    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        return {"Items": [], "LastEvaluatedKey": {"mapping_id": "again"}}


def test_bump_writes_add_count_with_today_bucket(monkeypatch):
    table = RecordingTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    bumped = artifact_usage.bump_artifact_usage(
        {("concept", "inventory_level"), ("value_mapping", NODE_ID)})

    assert bumped == 2
    assert len(table.updates) == 2
    by_key = {u["Key"]["mapping_id"]: u for u in table.updates}
    assert set(by_key) == {
        "usage#concept#inventory_level", f"usage#value_mapping#{NODE_ID}",
    }
    for upd in table.updates:
        assert upd["Key"]["discovered_at"] == _today()
        # Atomic absent-safe counter on the literal "count" attribute.
        assert "ADD #c :one" in upd["UpdateExpression"]
        assert upd["ExpressionAttributeNames"] == {"#c": "count"}
        assert upd["ExpressionAttributeValues"][":one"] == 1
        assert "SET last_used_at = :now" in upd["UpdateExpression"]


def test_ledger_rows_carry_no_status_attribute(monkeypatch):
    """Rows must be invisible to every status-GSI read: NO status attribute."""
    table = RecordingTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    artifact_usage.bump_artifact_usage({("concept", "quantity")})

    (upd,) = table.updates
    assert "status" not in upd["Key"]
    assert "status" not in upd["UpdateExpression"]
    assert not any("status" in str(v) for v in upd["ExpressionAttributeNames"].values())
    # Only count + last_used_at are ever written.
    assert upd["UpdateExpression"] == "ADD #c :one SET last_used_at = :now"


def test_bump_empty_refs_short_circuits_no_table(monkeypatch):
    def boom():
        raise AssertionError("_get_table must not be called for empty refs")

    monkeypatch.setattr(self_learning, "_get_table", boom)
    assert artifact_usage.bump_artifact_usage(set()) == 0
    assert artifact_usage.bump_artifact_usage(None) == 0


def test_bump_swallows_backend_failure(monkeypatch):
    table = RecordingTable(raise_on_update=True)
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    assert artifact_usage.bump_artifact_usage({("concept", "quantity")}) == 0

    def raising_get_table():
        raise RuntimeError("no table")

    monkeypatch.setattr(self_learning, "_get_table", raising_get_table)
    assert artifact_usage.bump_artifact_usage({("concept", "quantity")}) == 0


# --------------------------------------------------------------------------- #
# read_artifact_usage — 30-day window, cache, bounded scan, swallow
# --------------------------------------------------------------------------- #
def _row(kind, artifact_id, day_offset, count, last_used_at=None):
    return {
        "mapping_id": f"usage#{kind}#{artifact_id}",
        "discovered_at": _day(day_offset),
        "count": count,
        "last_used_at": last_used_at or f"{_day(day_offset)}T12:00:00+00:00",
    }


def test_read_sums_exactly_thirty_day_window(monkeypatch):
    table = RecordingTable(scan_pages=[[
        _row("concept", "quantity", 0, 3),     # today — counted
        _row("concept", "quantity", 29, 2),    # boundary day 30 of 30 — counted
        _row("concept", "quantity", 30, 100),  # day 31 — SKIPPED
        _row("concept", "quantity", 90, 500),  # ancient — SKIPPED
    ]])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    usage = artifact_usage.read_artifact_usage(days=30)

    assert usage["quantity"]["usage_30d"] == 5
    assert usage["quantity"]["kind"] == "concept"
    # Latest last_used_at within the window wins.
    assert usage["quantity"]["last_used_at"] == f"{_day(0)}T12:00:00+00:00"


def test_read_scan_uses_begins_with_usage_prefix(monkeypatch):
    table = RecordingTable(scan_pages=[[]])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    artifact_usage.read_artifact_usage(days=30)

    (scan,) = table.scan_calls
    assert scan["FilterExpression"] == "begins_with(mapping_id, :prefix)"
    assert scan["ExpressionAttributeValues"] == {":prefix": "usage#"}


def test_read_skips_non_ledger_and_malformed_rows(monkeypatch):
    table = RecordingTable(scan_pages=[[
        _row("rule", NODE_ID, 1, 4),
        {"mapping_id": "m-normal-proposal", "discovered_at": _day(0), "count": 9},
        {"mapping_id": "usage#missingpart", "discovered_at": _day(0), "count": 9},
        {"mapping_id": f"usage#rule#{NODE_ID}", "discovered_at": _day(0),
         "count": "not-a-number"},  # count coerces to 0, row still merges
    ]])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    usage = artifact_usage.read_artifact_usage(days=30)

    assert set(usage) == {NODE_ID}
    assert usage[NODE_ID]["usage_30d"] == 4


def test_read_paginates_and_caps_at_twenty_pages(monkeypatch):
    table = AlwaysMoreTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    assert artifact_usage.read_artifact_usage(days=30) == {}
    assert len(table.scan_calls) == 20
    # ExclusiveStartKey threads from the second call on.
    assert "ExclusiveStartKey" not in table.scan_calls[0]
    assert table.scan_calls[1]["ExclusiveStartKey"] == {"mapping_id": "again"}


def test_read_result_cached_for_sixty_seconds(monkeypatch):
    table = RecordingTable(scan_pages=[[_row("concept", "quantity", 0, 1)]])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    first = artifact_usage.read_artifact_usage(days=30)
    second = artifact_usage.read_artifact_usage(days=30)

    assert first == second
    assert len(table.scan_calls) == 1  # served from the in-process cache

    # Expired cache → scans again.
    artifact_usage._read_cache[30] = (0.0, first)
    artifact_usage.read_artifact_usage(days=30)
    assert len(table.scan_calls) == 2


def test_read_swallows_backend_failure(monkeypatch):
    table = RecordingTable(raise_on_scan=True)
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    assert artifact_usage.read_artifact_usage(days=30) == {}

    def raising_get_table():
        raise RuntimeError("no table")

    monkeypatch.setattr(self_learning, "_get_table", raising_get_table)
    assert artifact_usage.read_artifact_usage(days=30) == {}


def test_read_failure_not_cached(monkeypatch):
    failing = RecordingTable(raise_on_scan=True)
    monkeypatch.setattr(self_learning, "_get_table", lambda: failing)
    assert artifact_usage.read_artifact_usage(days=30) == {}

    healthy = RecordingTable(scan_pages=[[_row("concept", "quantity", 0, 2)]])
    monkeypatch.setattr(self_learning, "_get_table", lambda: healthy)
    usage = artifact_usage.read_artifact_usage(days=30)
    assert usage["quantity"]["usage_30d"] == 2


def test_ledger_shadow_mode_env_identical(monkeypatch):
    """Ledger behavior identical with RESPONSE_INTEGRITY_MODE unset vs set."""
    class StaticTable(RecordingTable):
        def scan(self, **kwargs):
            self.scan_calls.append(kwargs)
            return {"Items": [_row("concept", "quantity", 0, 2)]}

    table = StaticTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    assert artifact_usage.bump_artifact_usage({("concept", "quantity")}) == 1
    unset_read = artifact_usage.read_artifact_usage(days=30)

    artifact_usage._reset_read_cache()
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    assert artifact_usage.bump_artifact_usage({("concept", "quantity")}) == 1
    set_read = artifact_usage.read_artifact_usage(days=30)

    assert unset_read == set_read
    assert unset_read["quantity"]["usage_30d"] == 2


# --------------------------------------------------------------------------- #
# Evidence classes (B3, ADR-0052 decision (b)) — additive write attr + read fold
# --------------------------------------------------------------------------- #
def test_evidence_attr_stored_nonempty_only(monkeypatch):
    """Non-empty evidence is an ADDITIVE attr; empty stays byte-identical."""
    table = RecordingTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    artifact_usage.bump_artifact_usage(
        {("join_path", NODE_ID)}, evidence="route-intersection")
    artifact_usage.bump_artifact_usage({("concept", "quantity")})  # no class

    with_class, without_class = table.updates
    assert with_class["UpdateExpression"] == (
        "ADD #c :one SET last_used_at = :now, evidence = :evidence")
    assert with_class["ExpressionAttributeValues"][":evidence"] == (
        "route-intersection")
    # The pre-B3 write shape is byte-identical when no class is passed.
    assert without_class["UpdateExpression"] == (
        "ADD #c :one SET last_used_at = :now")
    assert ":evidence" not in without_class["ExpressionAttributeValues"]


def test_read_folds_evidence_classes_legacy_tool_text(monkeypatch):
    """Read-side union of evidence classes; absent attr folds as tool-text."""
    stamped = _row("join_path", NODE_ID, 0, 2)
    stamped["evidence"] = "route-intersection"
    stamped_2 = _row("join_path", NODE_ID, 1, 1)
    stamped_2["evidence"] = "relationship-proof"
    legacy = _row("rule", NODE_ID_2, 0, 3)  # pre-B3 row: no evidence attr
    table = RecordingTable(scan_pages=[[stamped, stamped_2, legacy]])
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)

    usage = artifact_usage.read_artifact_usage(days=30)

    # Sorted union over the artifact's folded day buckets.
    assert usage[NODE_ID]["evidence_classes"] == [
        "relationship-proof", "route-intersection"]
    assert usage[NODE_ID]["usage_30d"] == 3
    # Legacy rows fold as "tool-text" — the honest pre-B3 default.
    assert usage[NODE_ID_2]["evidence_classes"] == ["tool-text"]
