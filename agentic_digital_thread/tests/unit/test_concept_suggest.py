# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for evidence-based concept suggestion at map time (value-overlap classifier).

All fixtures use SYNTHETIC identifiers that mirror the SHAPE of real data (coded
site tokens, disjoint populations, free-text/timestamp columns) without any
literal customer/dataset value. Two layers:
  * UNIT — the pure helpers (uncertainty gate, identifier-ish gate, overlap
    strength, evidence confidence, synonym extraction, ranking).
  * COMPONENT-INTEGRATION — the real ``suggest_concepts_by_value_overlap`` wiring
    with only the external I/O boundaries (graph query + sampling + catalog)
    mocked, asserting the suggestion, the skips, the bootstrap no-op, and ordering.
"""
from unittest.mock import patch

from agents import concept_suggest as cs


# =========================================================================
# UNIT — is_uncertain
# =========================================================================

def test_uncertain_needs_review_and_sentinels():
    assert cs.is_uncertain({"concept": "needs_review", "confidence": 0.9})
    assert cs.is_uncertain({"concept": "unknown", "confidence": 0.9})
    assert cs.is_uncertain({"concept": "", "confidence": 0.9})
    assert cs.is_uncertain({"concept": None})


def test_uncertain_low_confidence():
    assert cs.is_uncertain({"concept": "site", "confidence": 0.2})
    assert not cs.is_uncertain({"concept": "site", "confidence": 0.4})
    assert not cs.is_uncertain({"concept": "site", "confidence": 0.95})


def test_uncertain_bad_input():
    assert not cs.is_uncertain("not-a-dict")
    assert cs.is_uncertain({"concept": "site", "confidence": "junk"})


# =========================================================================
# UNIT — looks_identifier_ish (skip free-text / timestamps)
# =========================================================================

def test_identifier_ish_accepts_coded_tokens():
    assert cs.looks_identifier_ish(["SITE-A", "SITE-B", "SITE-C"])
    assert cs.looks_identifier_ish(["IN_PROGRESS", "COMPLETED"])  # one space allowed via underscore form


def test_identifier_ish_rejects_timestamps():
    assert not cs.looks_identifier_ish(["2026-07-01", "2026-07-02", "2026-07-03T14:23:00"])


def test_identifier_ish_rejects_free_text():
    assert not cs.looks_identifier_ish([
        "diameter drift detected by the SPC subsystem",
        "bearing replaced during the night shift maintenance window",
    ])


def test_identifier_ish_empty_is_false():
    assert not cs.looks_identifier_ish([])
    assert not cs.looks_identifier_ish([None, "", "   "])


def test_identifier_ish_majority_rule():
    # 2 coded + 1 timestamp -> majority identifier-ish -> True
    assert cs.looks_identifier_ish(["A1", "B2", "2026-07-01"])


# =========================================================================
# UNIT — strong_overlap + evidence_confidence
# =========================================================================

def test_strong_overlap_requires_support_and_ratio():
    assert cs.strong_overlap({"overlap_count": 3, "ratio": 0.75})
    assert not cs.strong_overlap({"overlap_count": 1, "ratio": 1.0})   # support too low
    assert not cs.strong_overlap({"overlap_count": 5, "ratio": 0.2})   # ratio too low
    assert not cs.strong_overlap({})


def test_evidence_confidence_band_and_cap():
    # Never reaches 1.0 (false-link safety); scales with ratio; within the band.
    c_full = cs.evidence_confidence({"ratio": 1.0})
    c_half = cs.evidence_confidence({"ratio": 0.5})
    assert c_full == cs._EVIDENCE_CONF_MAX < 1.0
    assert cs._EVIDENCE_CONF_MIN <= c_half < c_full


# =========================================================================
# UNIT — synonym extraction + isa95 candidates
# =========================================================================

_DEFS = {
    "site": "A site or plant. Synonyms (ISA-95 site level): plant, facility, location. Fields: site_id.",
    "production-line": "A line. Synonyms (ISA-95 work-center level, BELOW site): line, work cell, cell, station, work center. Fields: line_id.",
    "equipment-id": "Equipment. Fields: asset_id. (no synonym clause here)",
}


def test_extract_synonyms_parses_clause():
    assert cs.extract_synonyms(_DEFS["site"]) == ["plant", "facility", "location"]
    assert cs.extract_synonyms(_DEFS["equipment-id"]) == []


def test_isa95_candidate_matches_multiword_synonym():
    # column "work_center" tokenizes to {work, center}; synonym "work center" matches.
    assert "production-line" in cs.isa95_synonym_candidates("work_center", _DEFS)
    # column "cell" matches the single-token synonym.
    assert cs.isa95_synonym_candidates("cell", _DEFS) == ["production-line"]
    # a column with no synonym match -> []
    assert cs.isa95_synonym_candidates("random_col", _DEFS) == []


# =========================================================================
# UNIT — rank_candidates ordering
# =========================================================================

def test_rank_prefers_strong_value_evidence_over_weak_semantic():
    ranked = cs.rank_candidates(
        semantic={"concept": "location", "confidence": 0.3},
        isa95=[],
        value_overlap=[{"concept": "site", "confidence": 0.8}],
    )
    assert ranked[0]["concept"] == "site"
    assert ranked[0]["basis"] == "value_overlap"
    # the weak semantic guess is still SURFACED (not dropped)
    assert any(c["concept"] == "location" and c["basis"] == "semantic" for c in ranked)


def test_rank_orders_value_isa95_semantic():
    ranked = cs.rank_candidates(
        semantic={"concept": "c-sem", "confidence": 0.2},
        isa95=["c-isa"],
        value_overlap=[{"concept": "c-val", "confidence": 0.7}],
    )
    assert [c["concept"] for c in ranked] == ["c-val", "c-isa", "c-sem"]


def test_rank_dedups_same_concept_keeping_strongest_basis():
    ranked = cs.rank_candidates(
        semantic={"concept": "site", "confidence": 0.35},
        isa95=[],
        value_overlap=[{"concept": "site", "confidence": 0.8}],
    )
    assert len(ranked) == 1
    assert ranked[0]["basis"] == "value_overlap"


def test_rank_empty_inputs():
    assert cs.rank_candidates() == []
    assert cs.rank_candidates(semantic={"concept": "needs_review", "confidence": 0.9}) == []


# =========================================================================
# COMPONENT-INTEGRATION — suggest_concepts_by_value_overlap wiring
# =========================================================================
#
# Synthetic topology (mirrors the shape of a real cross-system layout):
#   NEW system "sys-new" (PostgreSQL) has an UNCERTAIN column records.cell whose
#   sampled values overlap the value population a PEER system already mapped to the
#   "site" concept. With ZERO name assumptions, the classifier should suggest
#   "site" for records.cell on the strength of the value overlap alone.

_NEW = "sys-new"

# Peer graph rows: a peer system "sys-peer" maps nodes.zone -> site as the
# site's identifier (the role is what makes its values identity evidence; the
# column name carries no assumption).
_PEER_ROWS = [
    {"sys_id": "sys-peer", "sys_schema": "peer", "table_name": "nodes",
     "field_name": "zone", "concept": "site", "role": "identifier"},
]
# The site population the peer exposes (obviously-fictitious coded tokens — NOT
# real facility codes, so no literal dataset value leaks into the fixtures).
_SITE_POP = ["SITE-A", "SITE-B", "SITE-C", "SITE-D"]


def _catalog_get(sid):
    return {
        _NEW: {"connection_type": "PostgreSQL", "schema": "newsch", "connection_config": {}},
        "sys-peer": {"connection_type": "PostgreSQL", "schema": "peer", "connection_config": {}},
    }.get(sid, {})


def _make_sampler(new_values_by_col):
    """Return a sample_field_values stand-in: peer 'zone' -> the site population;
    new-system columns -> the per-column synthetic values supplied by the test."""
    def _sample(**kw):
        sid, field = kw.get("system_id"), kw.get("field")
        if sid == "sys-peer" and field == "zone":
            return list(_SITE_POP)
        if sid == _NEW:
            return list(new_values_by_col.get(field, []))
        return []
    return _sample


def test_suggest_upgrades_overlapping_column_to_peer_concept():
    # records.cell values overlap the peer site population -> suggest 'site'.
    mappings = [{"table": "records", "field": "records.cell",
                 "concept": "needs_review", "confidence": 0, "mappings": []}]
    sampler = _make_sampler({"records.cell": ["SITE-A", "SITE-B", "SITE-C"]})

    with patch("agents.concept_suggest.neptune_query", return_value=_PEER_ROWS), \
            patch("agents.concept_suggest.catalog") as mock_cat, \
            patch("agents.concept_suggest.sample_field_values", side_effect=sampler):
        mock_cat.get.side_effect = _catalog_get
        cs.suggest_concepts_by_value_overlap(_NEW, mappings)

    m = mappings[0]
    assert m["concept"] == "site"
    assert m["suggestion_basis"] == "value_overlap"
    top = m["candidates"][0]
    assert top["basis"] == "value_overlap" and top["concept"] == "site"
    assert top["overlap_count"] >= cs._MIN_DISTINCT_SUPPORT
    assert "sys-peer" in top["peer_systems"]


def test_suggest_skips_non_overlapping_column():
    # records.widget values are disjoint from the site population -> no suggestion.
    mappings = [{"table": "records", "field": "records.widget",
                 "concept": "needs_review", "confidence": 0, "mappings": []}]
    sampler = _make_sampler({"records.widget": ["ZZZ1", "ZZZ2", "ZZZ3"]})

    with patch("agents.concept_suggest.neptune_query", return_value=_PEER_ROWS), \
            patch("agents.concept_suggest.catalog") as mock_cat, \
            patch("agents.concept_suggest.sample_field_values", side_effect=sampler):
        mock_cat.get.side_effect = _catalog_get
        cs.suggest_concepts_by_value_overlap(_NEW, mappings)

    assert mappings[0]["concept"] == "needs_review"
    assert "suggestion_basis" not in mappings[0]


def test_suggest_skips_timestamp_column():
    # An uncertain column whose values are raw timestamps must be gated out even if
    # the population happened to include timestamp-shaped tokens.
    mappings = [{"table": "records", "field": "records.event_ts",
                 "concept": "needs_review", "confidence": 0, "mappings": []}]
    sampler = _make_sampler({"records.event_ts": ["2026-07-01T00:00:00", "2026-07-02T00:00:00"]})

    with patch("agents.concept_suggest.neptune_query", return_value=_PEER_ROWS), \
            patch("agents.concept_suggest.catalog") as mock_cat, \
            patch("agents.concept_suggest.sample_field_values", side_effect=sampler):
        mock_cat.get.side_effect = _catalog_get
        cs.suggest_concepts_by_value_overlap(_NEW, mappings)

    assert mappings[0]["concept"] == "needs_review"
    assert "candidates" not in mappings[0]


def test_suggest_bootstrapping_no_op_when_no_peers():
    # No peer populations in the graph -> no-op, mappings byte-identical.
    mappings = [{"table": "records", "field": "records.cell",
                 "concept": "needs_review", "confidence": 0, "mappings": []}]
    import copy
    before = copy.deepcopy(mappings)

    with patch("agents.concept_suggest.neptune_query", return_value=[]), \
            patch("agents.concept_suggest.catalog") as mock_cat, \
            patch("agents.concept_suggest.sample_field_values") as mock_sample:
        mock_cat.get.side_effect = _catalog_get
        cs.suggest_concepts_by_value_overlap(_NEW, mappings)

    assert mappings == before
    mock_sample.assert_not_called()  # no peers -> no sampling of the new system


def test_suggest_leaves_confident_columns_untouched():
    # A confident (non-uncertain) mapping is never sampled or altered.
    mappings = [{"table": "records", "field": "records.serial",
                 "concept": "equipment-id", "confidence": 0.95, "mappings": []}]
    import copy
    before = copy.deepcopy(mappings)

    with patch("agents.concept_suggest.neptune_query") as mock_q, \
            patch("agents.concept_suggest.sample_field_values") as mock_sample:
        cs.suggest_concepts_by_value_overlap(_NEW, mappings)

    assert mappings == before
    mock_q.assert_not_called()      # early return before any IO
    mock_sample.assert_not_called()


def test_suggest_ranked_candidates_ordering_end_to_end():
    # A weak semantic guess ("location") on a column whose values ALSO overlap the
    # peer "site" population -> ranked candidates put value_overlap 'site' first,
    # weak semantic 'location' still surfaced.
    mappings = [{"table": "records", "field": "records.cell",
                 "concept": "location", "confidence": 0.3, "mappings": []}]
    sampler = _make_sampler({"records.cell": ["SITE-A", "SITE-B", "SITE-C"]})

    with patch("agents.concept_suggest.neptune_query", return_value=_PEER_ROWS), \
            patch("agents.concept_suggest.catalog") as mock_cat, \
            patch("agents.concept_suggest.sample_field_values", side_effect=sampler):
        mock_cat.get.side_effect = _catalog_get
        cs.suggest_concepts_by_value_overlap(_NEW, mappings)

    cands = mappings[0]["candidates"]
    assert cands[0]["concept"] == "site" and cands[0]["basis"] == "value_overlap"
    assert any(c["concept"] == "location" and c["basis"] == "semantic" for c in cands)
    assert mappings[0]["concept"] == "site"  # strong evidence wins the upgrade


def _derive_provision_maps_to_concepts(m):
    """Faithful replica of onboard.py provision's Field-[:MAPS_TO]->Concept
    derivation for one mapping: role_mappings come from m["mappings"], falling
    back to the top-level concept ONLY when mappings is EMPTY. Returns the set of
    concept ids that would receive a MAPS_TO edge. Kept in lockstep with the
    provision graph-write (onboard.py b2 block)."""
    concept = m.get("concept", "")
    role_mappings = m.get("mappings", [])
    if not role_mappings and concept and concept not in ("needs_review", "unknown"):
        role_mappings = [{"concept": concept, "role": "identifier",
                          "confidence": m.get("confidence", 0.5)}]
    return {
        rm.get("concept")
        for rm in role_mappings
        if rm.get("concept") and rm.get("concept") not in ("needs_review", "unknown")
    }


def test_upgrade_rewrites_mappings_so_provision_edge_lands_on_suggested_concept():
    """Regression for the dropped-upgrade bug: an uncertain column whose role-
    mappings are NON-empty-but-all-low-confidence (the needs_review condition) must
    have BOTH its top-level concept AND its m["mappings"]/m["concepts"] rewritten to
    the value-overlap suggestion — otherwise provision (which derives MAPS_TO from
    m["mappings"]) would persist the OLD low-confidence concept and resolve would
    never see the suggestion."""
    mappings = [{
        "table": "records", "field": "records.cell",
        "concept": "needs_review", "confidence": 0.0,
        # The bug condition: a non-empty role-mapping list whose confidences are all
        # below the needs_review floor (so provision does NOT fall back to the
        # top-level concept — it consumes THIS list).
        "mappings": [{"concept": "asset-class", "role": "identifier", "confidence": 0.3}],
        "concepts": [{"concept": "asset-class", "confidence": 0.3}],
    }]
    sampler = _make_sampler({"records.cell": ["SITE-A", "SITE-B", "SITE-C"]})

    with patch("agents.concept_suggest.neptune_query", return_value=_PEER_ROWS), \
            patch("agents.concept_suggest.catalog") as mock_cat, \
            patch("agents.concept_suggest.sample_field_values", side_effect=sampler):
        mock_cat.get.side_effect = _catalog_get
        cs.suggest_concepts_by_value_overlap(_NEW, mappings)

    m = mappings[0]
    conf = m["confidence"]
    # Top-level concept upgraded.
    assert m["concept"] == "site" and m["suggestion_basis"] == "value_overlap"
    # m["mappings"] / m["concepts"] rewritten to the suggested concept (role identifier).
    assert m["mappings"] == [{"concept": "site", "role": "identifier", "confidence": conf}]
    assert m["concepts"] == [{"concept": "site", "confidence": conf}]
    # The stale low-confidence concept is gone from the edge-source list.
    assert all(rm["concept"] != "asset-class" for rm in m["mappings"])
    # Provision would now write the MAPS_TO edge to the SUGGESTED concept, not the old one.
    edge_concepts = _derive_provision_maps_to_concepts(m)
    assert edge_concepts == {"site"}
    assert "asset-class" not in edge_concepts


def test_suggest_best_effort_on_graph_error():
    # A graph error inside population gathering must never break mapping.
    mappings = [{"table": "records", "field": "records.cell",
                 "concept": "needs_review", "confidence": 0, "mappings": []}]
    with patch("agents.concept_suggest.neptune_query", side_effect=RuntimeError("boom")), \
            patch("agents.concept_suggest.catalog") as mock_cat:
        mock_cat.get.side_effect = _catalog_get
        result = cs.suggest_concepts_by_value_overlap(_NEW, mappings)
    assert result is mappings
    assert mappings[0]["concept"] == "needs_review"


# =========================================================================
# Value overlap is IDENTITY evidence only (live 2026-10-16: a re-onboard put an
# event-row counter the mapper had deliberately left unmapped back under a
# peer's units-produced concept, tagged role 'identifier', because both columns
# hold the same small integers). Synthetic names, none from the seed schemas.
# =========================================================================

_QTY_POP = ["0", "120", "240", "360"]


def _qty_sampler(new_values_by_col, peer_field="produced"):
    def _sample(**kw):
        sid, field = kw.get("system_id"), kw.get("field")
        if sid == "sys-peer" and field == peer_field:
            return list(_QTY_POP)
        if sid == _NEW:
            return list(new_values_by_col.get(field, []))
        return []
    return _sample


def _run_suggest(mappings, peer_rows, sampler):
    with patch("agents.concept_suggest.neptune_query", return_value=peer_rows), \
            patch("agents.concept_suggest.catalog") as mock_cat, \
            patch("agents.concept_suggest.sample_field_values", side_effect=sampler) as mock_sample:
        mock_cat.get.side_effect = _catalog_get
        cs.suggest_concepts_by_value_overlap(_NEW, mappings)
    return mock_sample


def test_suggest_never_upgrades_a_measure_named_column():
    # The new column is a count by name. Even when its values overlap a peer
    # population exactly (and the peer edge says 'identifier'), a count is not
    # a join key, so it is never sampled and stays unmapped.
    peer_rows = [{"sys_id": "sys-peer", "sys_schema": "peer", "table_name": "totals",
                  "field_name": "produced", "concept": "actual-quantity",
                  "role": "identifier"}]
    mappings = [{"table": "steps", "field": "steps.piece_count",
                 "concept": "needs_review", "confidence": 0, "mappings": []}]
    mock_sample = _run_suggest(mappings, peer_rows,
                               _qty_sampler({"steps.piece_count": list(_QTY_POP)}))

    m = mappings[0]
    assert m["concept"] == "needs_review"
    assert m["mappings"] == []
    assert "suggestion_basis" not in m
    assert not any(c.get("basis") == "value_overlap" for c in m.get("candidates", []))


def test_suggest_ignores_a_peer_measure_population():
    # The peer maps the concept as a MEASURE (a quantity). Overlapping integers
    # with that population say nothing about identity, so a neutral-named column
    # gets no value-overlap candidate and no upgrade.
    peer_rows = [{"sys_id": "sys-peer", "sys_schema": "peer", "table_name": "totals",
                  "field_name": "produced", "concept": "actual-quantity",
                  "role": "measure"}]
    mappings = [{"table": "steps", "field": "steps.reading",
                 "concept": "needs_review", "confidence": 0, "mappings": []}]
    _run_suggest(mappings, peer_rows, _qty_sampler({"steps.reading": list(_QTY_POP)}))

    m = mappings[0]
    assert m["concept"] == "needs_review"
    assert "suggestion_basis" not in m
    assert not any(c.get("basis") == "value_overlap" for c in m.get("candidates", []))


def test_suggest_still_upgrades_from_identifier_and_correlate_peers():
    # Identity populations keep working whatever role tag the peer edge carries,
    # as long as the peer column is named like a key.
    for role in ("identifier", "correlate"):
        peer_rows = [{"sys_id": "sys-peer", "sys_schema": "peer", "table_name": "nodes",
                      "field_name": "zone_code", "concept": "site", "role": role}]

        def _sample(**kw):
            if kw.get("system_id") == "sys-peer" and kw.get("field") == "zone_code":
                return list(_SITE_POP)
            if kw.get("system_id") == _NEW:
                return ["SITE-A", "SITE-B", "SITE-C"]
            return []

        mappings = [{"table": "records", "field": "records.cell",
                     "concept": "needs_review", "confidence": 0, "mappings": []}]
        _run_suggest(mappings, peer_rows, _sample)
        assert mappings[0]["concept"] == "site", role
        assert mappings[0]["suggestion_basis"] == "value_overlap", role


def test_peer_populations_keep_measures_for_other_callers():
    # The human-assertion evidence score (self_learning) reads every peer
    # population, measures included; only the map-time suggestion filters them.
    peer_rows = [{"sys_id": "sys-peer", "sys_schema": "peer", "table_name": "totals",
                  "field_name": "produced", "concept": "actual-quantity",
                  "role": "measure"}]
    with patch("agents.concept_suggest.neptune_query", return_value=peer_rows), \
            patch("agents.concept_suggest.catalog") as mock_cat, \
            patch("agents.concept_suggest.sample_field_values",
                  side_effect=_qty_sampler({})):
        mock_cat.get.side_effect = _catalog_get
        every = cs._peer_concept_populations(_NEW)
        identity = cs._peer_concept_populations(_NEW, identity_only=True)
    assert every["actual-quantity"]["values"] == _QTY_POP
    assert "actual-quantity" not in identity


_STATES = ["RUNNING", "PAUSED", "COMPLETE"]


def _state_sampler(peer_field, new_values_by_col):
    def _sample(**kw):
        sid, field = kw.get("system_id"), kw.get("field")
        if sid == "sys-peer" and field == peer_field:
            return list(_STATES)
        if sid == _NEW:
            return list(new_values_by_col.get(field, []))
        return []
    return _sample


def test_suggest_ignores_a_descriptive_correlate_population():
    # A peer's status column is a correlate of its record (it describes it).
    # Sharing RUNNING/PAUSED/COMPLETE with it says nothing about identity, so an
    # uncertain state column is not rewritten to the peer's record concept.
    for role in ("correlate", None):
        row = {"sys_id": "sys-peer", "sys_schema": "peer", "table_name": "runs",
               "field_name": "status", "concept": "production-run"}
        if role:
            row["role"] = role
        mappings = [{"table": "events", "field": "events.machine_status",
                     "concept": "needs_review", "confidence": 0, "mappings": []}]
        _run_suggest(mappings, [row],
                     _state_sampler("status", {"events.machine_status": list(_STATES)}))
        assert mappings[0]["concept"] == "needs_review", role
        assert "suggestion_basis" not in mappings[0], role


def test_suggest_keeps_identifier_named_correlate_populations():
    # A peer key column the mapper tagged as a correlate (a line or site code on
    # an event table) still names the thing, so its values remain evidence.
    peer_rows = [{"sys_id": "sys-peer", "sys_schema": "peer", "table_name": "nodes",
                  "field_name": "site_code", "concept": "site", "role": "correlate"}]

    def _sample(**kw):
        if kw.get("system_id") == "sys-peer" and kw.get("field") == "site_code":
            return list(_SITE_POP)
        if kw.get("system_id") == _NEW:
            return ["SITE-A", "SITE-B", "SITE-C"]
        return []

    mappings = [{"table": "records", "field": "records.cell",
                 "concept": "needs_review", "confidence": 0, "mappings": []}]
    _run_suggest(mappings, peer_rows, _sample)
    assert mappings[0]["concept"] == "site"


def test_suggest_samples_code_named_columns_that_contain_a_measure_word():
    # rate_code, size_code and total_id are codes, not measures: a code column's
    # overlap with a peer code population is the evidence this pass is for.
    peer_rows = [{"sys_id": "sys-peer", "sys_schema": "peer", "table_name": "grades",
                  "field_name": "grade_code", "concept": "product", "role": "identifier"}]
    codes = ["GR-S", "GR-M", "GR-L"]
    for col in ("rate_code", "size_code", "total_id"):
        def _sample(col=col, **kw):
            if kw.get("system_id") == "sys-peer" and kw.get("field") == "grade_code":
                return list(codes)
            if kw.get("system_id") == _NEW and kw.get("field") == f"items.{col}":
                return list(codes)
            return []
        mappings = [{"table": "items", "field": f"items.{col}",
                     "concept": "needs_review", "confidence": 0, "mappings": []}]
        _run_suggest(mappings, peer_rows, _sample)
        assert mappings[0]["concept"] == "product", col


def test_suggest_still_skips_plain_count_and_size_columns():
    peer_rows = [{"sys_id": "sys-peer", "sys_schema": "peer", "table_name": "totals",
                  "field_name": "produced", "concept": "actual-quantity",
                  "role": "identifier"}]
    for col in ("sample_size", "lot_count", "qty_total"):
        mappings = [{"table": "steps", "field": f"steps.{col}",
                     "concept": "needs_review", "confidence": 0, "mappings": []}]
        _run_suggest(mappings, peer_rows, _qty_sampler({f"steps.{col}": list(_QTY_POP)}))
        assert mappings[0]["concept"] == "needs_review", col


def test_a_measure_named_peer_is_not_identity_evidence_even_as_identifier():
    # The live shape: an event-row counter that an earlier pass wrote as an
    # 'identifier' edge. Its numbers must not reach a neutral-named new reading.
    peer_rows = [{"sys_id": "sys-peer", "sys_schema": "peer", "table_name": "steps",
                  "field_name": "quantity_at_step", "concept": "actual-quantity",
                  "role": "identifier"}]
    mappings = [{"table": "events", "field": "events.reading",
                 "concept": "needs_review", "confidence": 0, "mappings": []}]
    _run_suggest(mappings, peer_rows,
                 _qty_sampler({"events.reading": list(_QTY_POP)}, peer_field="quantity_at_step"))
    assert mappings[0]["concept"] == "needs_review"
    assert "suggestion_basis" not in mappings[0]


def test_code_named_peer_populations_count_in_any_key_role():
    # One predicate on both sides: a size or rate code is key-named whether it is
    # the new column or a peer's correlate or role-less column.
    codes = ["GR-S", "GR-M", "GR-L"]
    for peer_field in ("size_code", "rate_code"):
        for role in ("correlate", None):
            row = {"sys_id": "sys-peer", "sys_schema": "peer", "table_name": "grades",
                   "field_name": peer_field, "concept": "product"}
            if role:
                row["role"] = role

            def _sample(peer_field=peer_field, **kw):
                if kw.get("system_id") == "sys-peer" and kw.get("field") == peer_field:
                    return list(codes)
                if kw.get("system_id") == _NEW:
                    return list(codes)
                return []

            mappings = [{"table": "items", "field": "items.grade",
                         "concept": "needs_review", "confidence": 0, "mappings": []}]
            _run_suggest(mappings, [row], _sample)
            assert mappings[0]["concept"] == "product", (peer_field, role)


def test_key_named_predicates_agree():
    for name in ("size_code", "rate_code", "total_id", "site_code", "line_id"):
        assert not cs.is_measure_named(name), name
        assert cs.is_identity_edge("correlate", name), name
    for name in ("sample_size", "lot_count", "qty_total", "quantity_at_event", "size"):
        assert cs.is_measure_named(name), name
        for role in ("correlate", None):
            assert not cs.is_identity_edge(role, name), (name, role)
    assert not cs.is_identity_edge("correlate", "status")
    assert cs.is_identity_edge("identifier", "zone")


def test_numeric_values_under_a_measure_name_are_not_identity_evidence():
    nums = ["0", "120", "240.5", "360"]
    codes = ["S", "M", "L", "XL"]
    assert cs.numeric_measure("quantity_at_event", nums)
    assert cs.numeric_measure("size", nums)
    assert not cs.numeric_measure("size", codes)           # a size code, not a size
    assert not cs.numeric_measure("reading", nums)         # name doesn't say measure
    assert not cs.numeric_measure("total_id", nums)        # key-named


def test_categorical_size_column_still_gets_value_evidence():
    # A bare 'size' column holding S/M/L codes is a classification, so its
    # overlap with a peer's size codes is evidence, as on main. A numeric 'size'
    # is a dimension and stays out.
    codes = ["S", "M", "L"]
    peer_rows = [{"sys_id": "sys-peer", "sys_schema": "peer", "table_name": "grades",
                  "field_name": "size_code", "concept": "product", "role": "identifier"}]
    for values, upgraded in ((codes, True), (["10", "20", "30"], False)):
        def _sample(values=values, **kw):
            if kw.get("system_id") == "sys-peer" and kw.get("field") == "size_code":
                return list(codes) if upgraded else ["10", "20", "30"]
            if kw.get("system_id") == _NEW:
                return list(values)
            return []
        mappings = [{"table": "items", "field": "items.size",
                     "concept": "needs_review", "confidence": 0, "mappings": []}]
        _run_suggest(mappings, peer_rows, _sample)
        assert (mappings[0]["concept"] == "product") is upgraded, values


def test_identifier_role_peer_with_coded_values_under_a_measure_name_counts():
    # The peer column is called 'size' but the earlier pass named it an
    # identifier and its values are codes: that population is evidence.
    codes = ["S", "M", "L"]
    peer_rows = [{"sys_id": "sys-peer", "sys_schema": "peer", "table_name": "grades",
                  "field_name": "size", "concept": "product", "role": "identifier"}]

    def _sample(**kw):
        if kw.get("system_id") == "sys-peer" and kw.get("field") == "size":
            return list(codes)
        if kw.get("system_id") == _NEW:
            return list(codes)
        return []

    mappings = [{"table": "items", "field": "items.grade",
                 "concept": "needs_review", "confidence": 0, "mappings": []}]
    _run_suggest(mappings, peer_rows, _sample)
    assert mappings[0]["concept"] == "product"
