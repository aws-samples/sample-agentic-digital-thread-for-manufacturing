# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for ``agents.vocabulary`` (W1.2, ADR 0050).

All tests are pure: the single graph-read seam is injected (``query=...``) as a fake
callable routed by cypher shape (the ``test_concept_layer`` idiom). No AWS / Neptune /
pg8000 / LLM is touched — the lane runs under ``--disable-socket``.
"""
from __future__ import annotations

import os
import pathlib
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
import sys

import agents.vocabulary as vocab

_ROW_KEYS = {
    "id", "name", "description", "synonyms", "layer", "source", "provenance_label",
    "mapped_field_count", "usage_30d", "last_used_at", "retired",
}


# --------------------------------------------------------------------------
# Fake graph seam — routes by cypher shape so one callable serves both reads
# (flat_index's dynamic-concepts read + the vocabulary detail read).
# --------------------------------------------------------------------------

def make_query(*, dynamic=None, detail=None):
    """Build a fake ``neptune_query`` honoring the two locked cyphers."""
    dynamic = dynamic or []
    detail = detail or []

    def _q(cypher, parameters=None):
        if "count(m)" in cypher:
            return [dict(r) for r in detail]
        if "c.source <> 'core'" in cypher:
            return [dict(r) for r in dynamic]
        return []

    return _q


def _raising_query(*_a, **_k):
    raise RuntimeError("graph unavailable")


def _by_id(rows):
    return {r["id"]: r for r in rows}


# --------------------------------------------------------------------------
# Merge: core ∪ minted ∪ pack — flat_index projection enriched with detail.
# --------------------------------------------------------------------------

def test_merges_core_minted_and_pack_concepts():
    dynamic = [
        {"id": "eu-lead-time", "name": "EU Lead Time", "source": "minted"},
        {"id": "shift-pattern", "name": "Shift Pattern", "source": "pack:ops_extras"},
    ]
    rows = _by_id(vocab.list_concepts(query=make_query(dynamic=dynamic)))

    # Core concepts present with their catalog layer/source.
    assert rows["asset"]["layer"] == "entity"
    assert rows["asset"]["source"] == "core"
    # Minted + pack concepts merged in, field-layer by construction (ADR 0021).
    assert rows["eu-lead-time"]["name"] == "EU Lead Time"
    assert rows["eu-lead-time"]["layer"] == "field"
    assert rows["eu-lead-time"]["source"] == "minted"
    assert rows["shift-pattern"]["source"] == "pack:ops_extras"
    # Every row carries the full contract shape.
    for r in rows.values():
        assert set(r) == _ROW_KEYS


def test_core_wins_on_id_collision():
    dynamic = [{"id": "asset", "name": "Hijacked", "source": "pack:x"}]
    rows = [r for r in vocab.list_concepts(query=make_query(dynamic=dynamic))
            if r["id"] == "asset"]
    assert len(rows) == 1
    assert rows[0]["name"] == "Asset"
    assert rows[0]["source"] == "core"


# --------------------------------------------------------------------------
# Mapped-field counts — from the ONE detail read; absent → 0.
# --------------------------------------------------------------------------

def test_mapped_field_counts_merged_and_default_zero():
    dynamic = [{"id": "eu-lead-time", "name": "EU Lead Time", "source": "minted"}]
    detail = [
        {"id": "asset", "mapped_field_count": 7},
        {"id": "eu-lead-time", "mapped_field_count": 2},
    ]
    rows = _by_id(vocab.list_concepts(query=make_query(dynamic=dynamic, detail=detail)))
    assert rows["asset"]["mapped_field_count"] == 7
    assert rows["eu-lead-time"]["mapped_field_count"] == 2
    # A concept absent from the detail read (or with a null count) → 0.
    assert rows["mro-inventory"]["mapped_field_count"] == 0


# --------------------------------------------------------------------------
# Retired flag passthrough — graph truthy → True; absent/null → False.
# --------------------------------------------------------------------------

def test_retired_flag_passthrough():
    dynamic = [{"id": "old-code", "name": "Old Code", "source": "custom"}]
    detail = [
        {"id": "old-code", "retired": True},
        {"id": "asset", "retired": None},
    ]
    rows = _by_id(vocab.list_concepts(query=make_query(dynamic=dynamic, detail=detail)))
    assert rows["old-code"]["retired"] is True
    assert rows["asset"]["retired"] is False
    assert rows["mro-inventory"]["retired"] is False  # not in detail at all


# --------------------------------------------------------------------------
# Provenance labels — all five source shapes (ADR 0050).
# --------------------------------------------------------------------------

def test_provenance_label_all_five_shapes():
    assert vocab.provenance_label("core") == "core"
    assert vocab.provenance_label("pack:ops_extras") == "pack:ops_extras"
    assert vocab.provenance_label("pack:custom:site_terms") == "pack:custom:site_terms"
    assert vocab.provenance_label("minted") == "minted"
    assert vocab.provenance_label("custom") == "custom"
    # Unknown/legacy/missing sources bucket to 'custom' (never a stray label).
    assert vocab.provenance_label("dynamic") == "custom"
    assert vocab.provenance_label(None) == "custom"
    assert vocab.provenance_label("") == "custom"


def test_provenance_label_applied_to_rows():
    dynamic = [
        {"id": "a1", "name": "A1", "source": "minted"},
        {"id": "b2", "name": "B2", "source": "pack:erp_pack"},
        {"id": "c3", "name": "C3", "source": "pack:custom:my_upload"},
        {"id": "d4", "name": "D4", "source": "custom"},
    ]
    rows = _by_id(vocab.list_concepts(query=make_query(dynamic=dynamic)))
    assert rows["asset"]["provenance_label"] == "core"
    assert rows["a1"]["provenance_label"] == "minted"
    assert rows["b2"]["provenance_label"] == "pack:erp_pack"
    assert rows["c3"]["provenance_label"] == "pack:custom:my_upload"
    assert rows["d4"]["provenance_label"] == "custom"


# --------------------------------------------------------------------------
# Usage merge — caller-composed ledger data lands on the rows; absent → 0/None.
# --------------------------------------------------------------------------

def test_usage_merge():
    dynamic = [{"id": "eu-lead-time", "name": "EU Lead Time", "source": "minted"}]
    usage = {
        "asset": {"usage_30d": 12, "last_used_at": "2026-07-20"},
        "eu-lead-time": {"usage_30d": 3, "last_used_at": "2026-07-18"},
        "not-a-concept": {"usage_30d": 99, "last_used_at": "2026-07-01"},
    }
    rows = _by_id(vocab.list_concepts(query=make_query(dynamic=dynamic), usage=usage))
    assert rows["asset"]["usage_30d"] == 12
    assert rows["asset"]["last_used_at"] == "2026-07-20"
    assert rows["eu-lead-time"]["usage_30d"] == 3
    # Concepts without usage entries default honestly.
    assert rows["mro-inventory"]["usage_30d"] == 0
    assert rows["mro-inventory"]["last_used_at"] is None
    # Usage for unknown artifact ids never invents a row.
    assert "not-a-concept" not in rows


def test_usage_omitted_defaults():
    rows = vocab.list_concepts(query=make_query())
    assert all(r["usage_30d"] == 0 and r["last_used_at"] is None for r in rows)


# --------------------------------------------------------------------------
# Descriptions + synonyms — core curated text wins; graph fills the rest.
# --------------------------------------------------------------------------

def test_description_core_curated_and_graph_for_dynamic():
    from agents.catalog import CORE_CONCEPT_DEFINITIONS
    dynamic = [{"id": "eu-lead-time", "name": "EU Lead Time", "source": "minted"}]
    detail = [
        {"id": "eu-lead-time", "description": "Authored definition.",
         "synonyms": ["lead time", "replenishment time"]},
        # A stray graph description for a core id must NOT beat the curated text.
        {"id": "mro-inventory", "description": "graph-side override"},
    ]
    rows = _by_id(vocab.list_concepts(query=make_query(dynamic=dynamic, detail=detail)))
    assert rows["mro-inventory"]["description"] == \
        CORE_CONCEPT_DEFINITIONS["mro-inventory"]
    assert rows["eu-lead-time"]["description"] == "Authored definition."
    assert rows["eu-lead-time"]["synonyms"] == ["lead time", "replenishment time"]


def test_synonyms_normalize_string_and_absent():
    dynamic = [
        {"id": "s1", "name": "S1", "source": "custom"},
        {"id": "s2", "name": "S2", "source": "custom"},
    ]
    detail = [
        {"id": "s1", "synonyms": "alpha, beta , ,gamma"},  # comma string → list
        {"id": "s2", "synonyms": None},                     # null → []
    ]
    rows = _by_id(vocab.list_concepts(query=make_query(dynamic=dynamic, detail=detail)))
    assert rows["s1"]["synonyms"] == ["alpha", "beta", "gamma"]
    assert rows["s2"]["synonyms"] == []
    assert rows["asset"]["synonyms"] == []  # absent from detail entirely


# --------------------------------------------------------------------------
# Graph-failure degradation — a raising query yields the CORE-ONLY list
# (mirroring flat_index degradation); never raises.
# --------------------------------------------------------------------------

def test_graph_failure_degrades_to_core_only():
    rows = vocab.list_concepts(query=_raising_query)
    assert rows, "core projection must survive a dead graph"
    assert all(r["source"] == "core" for r in rows)
    assert all(r["provenance_label"] == "core" for r in rows)
    assert all(r["mapped_field_count"] == 0 for r in rows)
    assert all(r["retired"] is False for r in rows)
    ids = {r["id"] for r in rows}
    assert "asset" in ids and "mro-inventory" in ids
    for r in rows:
        assert set(r) == _ROW_KEYS


def test_graph_failure_still_merges_usage():
    usage = {"asset": {"usage_30d": 5, "last_used_at": "2026-07-19"}}
    rows = _by_id(vocab.list_concepts(query=_raising_query, usage=usage))
    assert rows["asset"]["usage_30d"] == 5
    assert rows["asset"]["last_used_at"] == "2026-07-19"


def test_detail_only_failure_still_lists_dynamic_concepts():
    # flat_index's read succeeds; only the detail read raises → dynamic rows kept,
    # metadata zeroed.
    dynamic = [{"id": "eu-lead-time", "name": "EU Lead Time", "source": "minted"}]

    def _q(cypher, parameters=None):
        if "count(m)" in cypher:
            raise RuntimeError("detail read failed")
        if "c.source <> 'core'" in cypher:
            return [dict(r) for r in dynamic]
        return []

    rows = _by_id(vocab.list_concepts(query=_q))
    assert "eu-lead-time" in rows
    assert rows["eu-lead-time"]["mapped_field_count"] == 0
    assert rows["eu-lead-time"]["retired"] is False


# --------------------------------------------------------------------------
# Import purity (ADR 0020/0021): no boto3/pg8000 at import; the lazy seam
# degrades to [] (no-graph safe) when runtime deps are absent.
# --------------------------------------------------------------------------

def test_module_is_import_pure_symbols():
    assert not hasattr(vocab, "boto3")
    assert not hasattr(vocab, "pgdriver")
    assert callable(vocab.neptune_query)


def test_imports_with_no_pg8000_or_aws():
    """Fresh interpreter with pg8000/agents.pgdriver/boto3/botocore BLOCKED still imports
    the module, and list_concepts serves the core projection with no graph."""
    code = (
        "import sys\n"
        "for _m in ('pg8000', 'agents.pgdriver', 'boto3', 'botocore'):\n"
        "    sys.modules[_m] = None\n"
        "import agents.vocabulary as v\n"
        "assert callable(v.neptune_query)\n"
        "assert v.neptune_query('MATCH (c:Concept) RETURN c') == []\n"
        "rows = v.list_concepts()\n"
        "assert rows, 'core projection must be non-empty'\n"
        "assert all(r['source'] == 'core' for r in rows)\n"
        "print('IMPORT-PURE-OK')\n"
    )
    adt_root = pathlib.Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(adt_root))
    r = subprocess.run([sys.executable, "-c", code], cwd=str(adt_root),  # nosec B603 - fixed argv list, no shell, internal inputs only
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "IMPORT-PURE-OK" in r.stdout
