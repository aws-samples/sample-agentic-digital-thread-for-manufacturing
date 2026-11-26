# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for ``agents.concept_layer`` (S5.2 Wave 2, ADR 0021).

All tests are pure: the single graph-read seam is injected (``query=...``) as a fake
callable routed by cypher shape. No AWS / Neptune / pg8000 / LLM is touched. The
card/metric backing reuses the code-owned (pure) metric & card layer seeds.
"""
from __future__ import annotations

import os
import pathlib
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
import sys

import agents.concept_layer as cl


# --------------------------------------------------------------------------
# Fake graph seam — routes by cypher shape so one callable serves every read.
# --------------------------------------------------------------------------

def make_query(*, systems=None, precedent=None, dynamic=None):
    """Build a fake ``neptune_query`` honoring the three locked cyphers."""
    systems = systems or []
    precedent = precedent or []
    dynamic = dynamic or []

    def _q(cypher, parameters=None):
        if "c.source <> 'core'" in cypher:
            return [dict(r) for r in dynamic]
        if "HAS_FIELD]->(:Field)" in cypher:
            return [{"id": cid} for cid in precedent]
        if "(s:System)-[:HAS_SCHEMA]" in cypher:
            return [{"id": sid} for sid in systems]
        return []

    return _q


# --------------------------------------------------------------------------
# field_earns_concept — the MINTING gate (ADR 0021), never gates mapping.
# --------------------------------------------------------------------------

def test_field_earns_concept_false_for_generic_column():
    # Generic/audit column never mints, regardless of sharing/cards.
    q = make_query(systems=["cmms", "cmms2"])  # would otherwise be shared
    assert cl.field_earns_concept("asset", "cmms", "created_at", "asset", query=q) is False
    assert cl.field_earns_concept("asset", "cmms", "asset_id", "asset", query=q) is False


def test_field_earns_concept_false_for_single_system_non_card_column():
    # Not generic, single system (no other maps the entity), concept backs no metric/card.
    q = make_query(systems=["cmms"])  # only the candidate maps it → not shared
    assert cl.field_earns_concept(
        "asset", "cmms", "warranty_months", "specification", query=q) is False


def test_field_earns_concept_true_for_shared_entity_column():
    # Not generic; entity shared by candidate + another system → earned.
    q = make_query(systems=["cmms", "cmms2"])
    assert cl.field_earns_concept(
        "asset", "cmms", "warranty_months", "specification", query=q) is True


def test_field_earns_concept_true_for_card_feeding_column():
    # Not generic; single system (not shared) BUT concept backs a real seed metric/card
    # (mro-inventory → spare-parts-on-hand metric → spare-parts-on-hand card).
    q = make_query(systems=["cmms"])
    assert cl.field_earns_concept(
        "spare-part", "cmms", "quantity_on_hand", "mro-inventory", query=q) is True


# --------------------------------------------------------------------------
# feeds_card_or_metric — code-owned layers, injectable.
# --------------------------------------------------------------------------

def test_feeds_card_or_metric_real_seed():
    assert cl.feeds_card_or_metric("mro-inventory") is True   # spare-parts metric+card
    assert cl.feeds_card_or_metric("availability") is True    # oee input metric
    assert cl.feeds_card_or_metric("supplier") is False       # no metric


def test_feeds_card_or_metric_injected_layers():
    class _ML:
        def metrics_for_concept(self, cid):
            return frozenset({"m1"}) if cid == "demo" else frozenset()

    class _CL:
        def cards_for_metric(self, mid):
            return ["card"] if mid == "m1" else []

    assert cl.feeds_card_or_metric("demo", metric_layer=_ML(), card_layer=_CL()) is True
    assert cl.feeds_card_or_metric("nope", metric_layer=_ML(), card_layer=_CL()) is False


# --------------------------------------------------------------------------
# entity sharing helpers
# --------------------------------------------------------------------------

def test_systems_for_entity_dedup_and_exclude():
    q = make_query(systems=["cmms", "cmms", "cmms2"])
    assert cl.systems_for_entity("asset", query=q) == {"cmms", "cmms2"}
    assert cl.systems_for_entity("asset", exclude_system="cmms", query=q) == {"cmms2"}


def test_entity_is_shared():
    assert cl.entity_is_shared("asset", "cmms", query=make_query(systems=["cmms"])) is False
    assert cl.entity_is_shared(
        "asset", "cmms", query=make_query(systems=["cmms", "cmms2"])) is True
    # no-graph safe: empty read → not shared
    assert cl.entity_is_shared("asset", "cmms", query=lambda *_a, **_k: []) is False


# --------------------------------------------------------------------------
# reuse_first_suggestions — entity-scoped ranking precedent > affinity > name.
# --------------------------------------------------------------------------

def test_reuse_first_ranks_precedent_over_affinity_over_name():
    # precedent: lot-number (NOT in CMMS affinity, NOT a name match for the column)
    # affinity: CMMS prior includes failure-mode
    # name-only: column 'shipment_id' substring-matches the 'shipment' concept only
    q = make_query(precedent=["lot-number"])
    out = cl.reuse_first_suggestions("asset", "CMMS", "shipment_id", query=q)

    assert out, "expected ranked suggestions"
    # top is the precedent
    assert out[0]["concept"] == "lot-number"
    assert out[0]["score"] == 3.0
    assert out[0]["reason"].startswith("precedent")

    by_concept = {d["concept"]: d for d in out}
    assert by_concept["failure-mode"]["score"] == 2.0
    assert "affinity" in by_concept["failure-mode"]["reason"]
    assert by_concept["shipment"]["score"] == 1.0
    assert "name match" in by_concept["shipment"]["reason"]

    # scores are monotonically non-increasing (precedent ≥ affinity ≥ name)
    scores = [d["score"] for d in out]
    assert scores == sorted(scores, reverse=True)
    # ordering proof: precedent before any affinity before the name-only
    idx = {d["concept"]: i for i, d in enumerate(out)}
    assert idx["lot-number"] < idx["failure-mode"] < idx["shipment"]


def test_reuse_first_highest_signal_wins_per_concept():
    # A concept that is BOTH precedent and affinity keeps the precedent (higher) score.
    q = make_query(precedent=["failure-mode"])  # failure-mode also in CMMS affinity
    out = cl.reuse_first_suggestions("asset", "CMMS", "x", query=q)
    fm = next(d for d in out if d["concept"] == "failure-mode")
    assert fm["score"] == 3.0
    assert fm["reason"].startswith("precedent")


def test_reuse_first_empty_when_nothing_fits():
    q = make_query()  # no precedent, no dynamic
    out = cl.reuse_first_suggestions("asset", "UNKNOWN-TYPE", "qzx", query=q)
    assert out == []


def test_reuse_first_says_which_suggestions_the_column_supports():
    """Precedent and affinity are entity-wide: they rank, but only a name/alias
    match on the column itself supports a label (``column_match``)."""
    q = make_query(precedent=["cost", "lot-number"])
    by = {d["concept"]: d for d in cl.reuse_first_suggestions(
        "service-call", "CMMS", "ship_to_street", query=q)}
    assert by["cost"]["column_match"] is False
    assert by["lot-number"]["column_match"] is False

    by = {d["concept"]: d for d in cl.reuse_first_suggestions(
        "service-call", "CMMS", "unit_cost", query=q)}
    # Precedent score kept (highest signal wins), and the column supports it.
    assert by["cost"]["score"] == 3.0 and by["cost"]["column_match"] is True
    assert by["lot-number"]["column_match"] is False


def test_one_shared_token_does_not_make_a_column_support_a_concept():
    """'quantity' alone doesn't make a good or reject count an actual-quantity:
    every token of the concept has to be in the column (or the concept lists it)."""
    for column in ("good_quantity", "reject_quantity", "quantity_at_event"):
        assert cl.column_support(column, "actual-quantity", "Actual Quantity") == 0, column
    assert cl.column_support("actual_quantity", "actual-quantity", "Actual Quantity") > 0
    assert cl.column_support("reorder_point", "mro-inventory") == cl._LISTED_FIELD_SUPPORT


def test_the_most_specific_supported_concept_wins_and_a_tie_labels_nothing():
    q = make_query(precedent=["inspection", "inspection-result", "actual-quantity"])
    out = cl.reuse_first_suggestions("quality-check", "MES", "inspection_result", query=q)
    assert cl.supported_reuse_choice(out)["concept"] == "inspection-result"
    out = cl.reuse_first_suggestions("quality-check", "MES", "reject_quantity", query=q)
    # Not actual-quantity (the precedent with the top score): scrap-waste lists it.
    assert cl.supported_reuse_choice(out)["concept"] == "scrap-waste"
    out = cl.reuse_first_suggestions("quality-check", "MES", "quantity_at_event", query=q)
    assert cl.supported_reuse_choice(out) is None
    tie = [{"concept": "a-b", "score": 3.0, "column_match": True, "column_support": 2},
           {"concept": "b-a", "score": 1.0, "column_match": True, "column_support": 2}]
    assert cl.supported_reuse_choice(tie) is None


def test_a_listed_field_is_suggested_without_precedent_or_affinity():
    """A field a concept's own description lists is a reuse candidate on any
    system type, even when no precedent or affinity put the concept forward."""
    q = make_query()
    out = cl.reuse_first_suggestions("stock-item", "UNKNOWN-TYPE", "quantity_on_hand", query=q)
    choice = cl.supported_reuse_choice(out)
    assert choice is not None and choice["concept"] == "mro-inventory", out
    assert choice["column_support"] == cl._LISTED_FIELD_SUPPORT


def test_a_one_token_concept_needs_the_column_to_be_that_word():
    """'cost' in 'cost_center' is a qualifier, not the thing the column holds."""
    for column, cid in (("cost_center", "cost"), ("batch_status", "batch"),
                        ("shift_notes", "shift")):
        assert cl.column_support(column, cid, cl.CONCEPT_NAME.get(cid, "")) == 0, column
    assert cl.column_support("cost", "cost", "Cost / Price") > 0
    assert cl.column_support("shift", "shift", "Shift") > 0
    # A listed alias still counts: unit_cost is in cost's own field list.
    assert cl.column_support("unit_cost", "cost", "Cost / Price") == cl._LISTED_FIELD_SUPPORT
    # Multi-token concepts keep the all-tokens rule.
    assert cl.column_support("actual_quantity_units", "actual-quantity", "Actual Quantity") == 2


def test_common_one_token_aliases_are_listed_so_reuse_can_still_label_them():
    """With exact matching for one-token concepts, a common alias only labels
    through the concept's own field list, and only one concept may list it."""
    from agents.catalog import CORE_CONCEPT_DEFINITIONS
    for column, cid in (("shift_number", "shift"), ("acquisition_cost", "cost")):
        assert cl.column_support(column, cid) == cl._LISTED_FIELD_SUPPORT, column
        listing = [c for c in CORE_CONCEPT_DEFINITIONS if column in cl._listed_fields(c)]
        assert listing == [cid], (column, listing)


# --------------------------------------------------------------------------
# flat_index — derived projection over core ∪ dynamic, layer-tagged.
# --------------------------------------------------------------------------

def test_flat_index_core_rows_tagged():
    rows = {r["id"]: r for r in cl.flat_index(query=lambda *_a, **_k: [])}
    assert rows["asset"] == {"id": "asset", "name": "Asset",
                             "layer": "entity", "source": "core"}
    assert rows["mro-inventory"]["layer"] == "field"
    assert rows["mro-inventory"]["source"] == "core"


def test_flat_index_merges_faked_dynamic_concept():
    dynamic = [{"id": "eu-mro-inventory", "name": "EU MRO Inventory",
                "source": "onboard:erp"}]
    rows = {r["id"]: r for r in cl.flat_index(query=make_query(dynamic=dynamic))}
    assert rows["eu-mro-inventory"] == {
        "id": "eu-mro-inventory", "name": "EU MRO Inventory",
        "layer": "field", "source": "onboard:erp"}
    # core still present and tagged
    assert rows["asset"]["layer"] == "entity"


def test_flat_index_core_wins_on_id_collision():
    dynamic = [{"id": "asset", "name": "Hijacked", "source": "dynamic"}]
    rows = [r for r in cl.flat_index(query=make_query(dynamic=dynamic)) if r["id"] == "asset"]
    assert len(rows) == 1
    assert rows[0] == {"id": "asset", "name": "Asset", "layer": "entity", "source": "core"}


# --------------------------------------------------------------------------
# Catalog accessors (no graph)
# --------------------------------------------------------------------------

def test_entity_field_accessors():
    assert "asset" in cl.entity_concepts()
    assert cl.is_entity("asset") is True
    assert cl.is_entity("mro-inventory") is False
    assert "mro-inventory" in cl.field_concepts()
    # dimension dual-roles are entity-capable
    assert cl.is_entity("production-line") is True


# --------------------------------------------------------------------------
# concept_layer_of — canonical rendering layer (no graph).
# --------------------------------------------------------------------------

def test_concept_layer_of_core_entity_and_field():
    assert cl.concept_layer_of("asset") == "entity"
    assert cl.concept_layer_of("mro-inventory") == "field"


def test_concept_layer_of_minted_defaults_field_and_empty():
    # A non-core (minted/dynamic) id is field-layer by construction (ADR 0021).
    assert cl.concept_layer_of("eu-mro-inventory") == "field"
    # Empty id → empty layer.
    assert cl.concept_layer_of("") == ""


def test_concept_layer_of_sentinels_have_no_layer():
    # Non-concept sentinels used by field_annotation / unmatched proposals are NOT
    # real concepts — they must not get a stray 'field' layer badge in the Review UI.
    assert cl.concept_layer_of("schema-annotation") == ""
    assert cl.concept_layer_of("needs_review") == ""
    assert cl.concept_layer_of("unknown") == ""


# --------------------------------------------------------------------------
# concept_description — core text (no graph) / minted graph value / absent → ''.
# --------------------------------------------------------------------------

def test_concept_description_core_returns_definition():
    from agents.catalog import CORE_CONCEPT_DEFINITIONS
    # Core id returns its curated definition WITHOUT touching the graph seam.
    def _boom(*_a, **_k):
        raise AssertionError("core lookup must not hit the graph")
    assert cl.concept_description("mro-inventory", query=_boom) == \
        CORE_CONCEPT_DEFINITIONS["mro-inventory"]


def test_concept_description_minted_returns_graph_value():
    q = lambda c, p=None: [{"description": "Authored minted definition."}]  # noqa: E731
    assert cl.concept_description("eu-mro-inventory", query=q) == \
        "Authored minted definition."


def test_concept_description_absent_returns_empty():
    # Unknown id, graph has no row → ''.
    assert cl.concept_description("never-minted", query=lambda *_a, **_k: []) == ""
    # Empty id → '' (no graph read).
    assert cl.concept_description("", query=lambda *_a, **_k: [{"description": "x"}]) == ""
    # Row present but null description → ''.
    assert cl.concept_description(
        "eu-x", query=lambda *_a, **_k: [{"description": None}]) == ""


# --------------------------------------------------------------------------
# concept_descriptions (batch) — core ids need no graph; ALL distinct non-core
# ids resolve in AT MOST ONE graph read; absent ids → ''.
# --------------------------------------------------------------------------

def test_concept_descriptions_core_only_does_no_graph_read():
    from agents.catalog import CORE_CONCEPT_DEFINITIONS

    def _boom(*_a, **_k):
        raise AssertionError("all-core batch must not hit the graph")

    out = cl.concept_descriptions(["mro-inventory", "asset"], query=_boom)
    assert out == {
        "mro-inventory": CORE_CONCEPT_DEFINITIONS["mro-inventory"],
        "asset": CORE_CONCEPT_DEFINITIONS["asset"],
    }


def test_concept_descriptions_batches_non_core_in_one_read():
    calls = []

    def _q(cypher, params=None):
        calls.append((cypher, params))
        return [
            {"id": "eu-mro-inventory", "description": "Authored EU definition."},
            {"id": "eu-lead-time", "description": None},  # null → ''
        ]

    out = cl.concept_descriptions(
        # duplicates + a core id + two distinct non-core + an absent non-core
        ["eu-mro-inventory", "eu-mro-inventory", "mro-inventory",
         "eu-lead-time", "never-minted"],
        query=_q,
    )
    # EXACTLY ONE graph round-trip regardless of id count, over the DISTINCT non-core ids.
    assert len(calls) == 1
    _cypher, params = calls[0]
    assert sorted(params["ids"]) == ["eu-lead-time", "eu-mro-inventory", "never-minted"]
    from agents.catalog import CORE_CONCEPT_DEFINITIONS
    assert out["mro-inventory"] == CORE_CONCEPT_DEFINITIONS["mro-inventory"]
    assert out["eu-mro-inventory"] == "Authored EU definition."
    assert out["eu-lead-time"] == ""        # null description → ''
    assert out["never-minted"] == ""        # absent from graph → ''


def test_concept_descriptions_skips_empty_ids_and_empty_input():
    # Empty/None ids are ignored; an all-empty input needs no graph read.
    def _boom(*_a, **_k):
        raise AssertionError("no non-core ids → no graph read")

    assert cl.concept_descriptions(["", None], query=_boom) == {}
    assert cl.concept_descriptions([], query=_boom) == {}



# --------------------------------------------------------------------------
# entity_suggestions — entity-layer analogue of reuse_first (ADR 0040).
# Dedicated fake seam: the entity-precedent cypher carries "$fcs"/"ec.id AS id".
# --------------------------------------------------------------------------

def make_entity_query(*, entity_precedent=None):
    entity_precedent = entity_precedent or []

    def _q(cypher, parameters=None):
        if "ec.id AS id" in cypher:
            return [{"id": cid} for cid in entity_precedent]
        return []

    return _q


def test_entity_suggestions_ranks_precedent_over_affinity_over_name():
    # precedent: 'supplier' (co-occurs with the given field concepts) — NOT in CMMS
    #   affinity, NOT a name match for 'invoice_header'.
    # affinity: CMMS prior includes the entity 'asset'.
    # name-only: 'invoice_header' substring-matches the 'invoice' entity, which is NOT
    #   in the CMMS affinity set (so its ONLY signal is the name match).
    q = make_entity_query(entity_precedent=["supplier"])
    out = cl.entity_suggestions(["payment"], "CMMS", "invoice_header", query=q)

    assert out, "expected ranked entity suggestions"
    assert out[0]["entity"] == "supplier"
    assert out[0]["score"] == 3.0
    assert out[0]["reason"].startswith("precedent")

    by = {d["entity"]: d for d in out}
    assert by["asset"]["score"] == 2.0
    assert "affinity" in by["asset"]["reason"]
    assert by["invoice"]["score"] == 1.0
    assert "name match" in by["invoice"]["reason"]

    scores = [d["score"] for d in out]
    assert scores == sorted(scores, reverse=True)
    idx = {d["entity"]: i for i, d in enumerate(out)}
    assert idx["supplier"] < idx["asset"] < idx["invoice"]


def test_entity_suggestions_candidates_are_entity_layer_only():
    # A precedent row that is NOT an entity concept is dropped (field concept can never
    # be a table's entity). 'mro-inventory' is a field concept.
    q = make_entity_query(entity_precedent=["mro-inventory", "asset"])
    out = cl.entity_suggestions(["cost"], "CMMS", "zzz", query=q)
    entities = {d["entity"] for d in out}
    assert "mro-inventory" not in entities
    assert "asset" in entities  # kept (entity, precedent)
    assert all(cl.is_entity(d["entity"]) for d in out)


def test_entity_suggestions_highest_signal_wins_per_entity():
    # 'asset' is BOTH precedent AND CMMS affinity → keeps the higher precedent score.
    q = make_entity_query(entity_precedent=["asset"])
    out = cl.entity_suggestions(["criticality"], "CMMS", "x", query=q)
    a = next(d for d in out if d["entity"] == "asset")
    assert a["score"] == 3.0
    assert a["reason"].startswith("precedent")


def test_entity_suggestions_no_precedent_read_when_no_field_concepts():
    def _boom(*_a, **_k):
        raise AssertionError("empty field_concepts must not hit the graph")

    # Unknown type + non-matching name → nothing fits, and no graph read happened.
    assert cl.entity_suggestions([], "UNKNOWN-TYPE", "qzx", query=_boom) == []


def test_entity_suggestions_name_match_only():
    # No precedent, unknown affinity → only the name-match signal fires.
    out = cl.entity_suggestions([], "UNKNOWN-TYPE", "asset", query=lambda *_a, **_k: [])
    by = {d["entity"]: d for d in out}
    assert "asset" in by
    assert by["asset"]["score"] == 1.0
    assert "name match" in by["asset"]["reason"]


# --------------------------------------------------------------------------
# identity_field_candidates — pure, deterministic, no graph (ADR 0040).
# --------------------------------------------------------------------------

def test_identity_prefers_role_identifier():
    cols = ["id", "barcode_id", "qty"]
    mappings = [{"field": "qty", "role": "measure"},
                {"field": "barcode_id", "role": "identifier"}]
    assert cl.identity_field_candidates(cols, mappings) == ["barcode_id"]


def test_identity_role_identifier_accepts_nested_map_shape():
    # The map-prompt shape nests roles under a "mappings" list.
    cols = ["work_order_number", "status"]
    mappings = [{"field": "work_order_number",
                 "mappings": [{"concept": "work-order", "role": "identifier"}]}]
    assert cl.identity_field_candidates(cols, mappings) == ["work_order_number"]


def test_identity_falls_back_to_business_key_over_bare_id():
    # No identifier-role mapping → business-key names win, bare 'id' excluded.
    cols = ["id", "barcode_id", "created_at"]
    assert cl.identity_field_candidates(cols, []) == ["barcode_id"]


def test_identity_business_key_variants_in_column_order():
    cols = ["id", "part_number", "notes", "asset_code", "serial_no", "device_serial"]
    assert cl.identity_field_candidates(cols, []) == [
        "part_number", "asset_code", "serial_no", "device_serial"]


def test_identity_empty_for_all_generic_table():
    # Only a bare surrogate + audit columns → no identity key.
    assert cl.identity_field_candidates(["id", "created_at", "notes", "is_active"], []) == []
    assert cl.identity_field_candidates([], []) == []


def test_identity_role_identifier_is_case_insensitive_on_column():
    cols = ["WorkOrderId", "x"]
    mappings = [{"field": "workorderid", "role": "identifier"}]
    # role match is case-insensitive on the column name; original casing preserved.
    assert cl.identity_field_candidates(cols, mappings) == ["WorkOrderId"]


# --------------------------------------------------------------------------
# Import purity (ADR 0001 / ADR 0020): no boto3/pg8000 needed at import; the
# only impure seam is the lazily-invoked, injectable neptune_query reference.
# --------------------------------------------------------------------------

def test_module_is_import_pure_symbols():
    assert not hasattr(cl, "boto3")
    assert not hasattr(cl, "pgdriver")
    assert callable(cl.neptune_query)


def test_imports_with_no_pg8000_or_aws():
    """Fresh interpreter with pg8000/agents.pgdriver/boto3/botocore BLOCKED still imports
    the module, and the lazy seam degrades to [] (no-graph safe) when the deps are absent."""
    code = (
        "import sys\n"
        "for _m in ('pg8000', 'agents.pgdriver', 'boto3', 'botocore'):\n"
        "    sys.modules[_m] = None\n"
        "import agents.concept_layer as cl\n"
        "assert callable(cl.neptune_query)\n"
        "assert cl.neptune_query('MATCH (c:Concept) RETURN c') == []\n"
        "assert cl.flat_index(), 'core projection must be non-empty'\n"
        "print('IMPORT-PURE-OK')\n"
    )
    adt_root = pathlib.Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(adt_root))
    r = subprocess.run([sys.executable, "-c", code], cwd=str(adt_root),  # nosec B603 - fixed argv list, no shell, internal inputs only
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "IMPORT-PURE-OK" in r.stdout
