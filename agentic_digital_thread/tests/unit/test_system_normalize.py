# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for ``agents.system_normalize`` (pure, offline).

``normalize_system_id`` resolves free-text system references to canonical
catalog ids WITHOUT ever guessing: exact id → unique name/id match → unique
system_type match → passthrough; knowledge-graph tokens → ``None`` sentinel
(caller drops). Import-pure — stdlib only, no boto3/pg8000, no catalog
import at module level (the caller injects ``known``).
"""
from __future__ import annotations

import os
import pathlib
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
import sys

from agents.system_normalize import (
    VERDICT_AMBIGUOUS,
    VERDICT_EMPTY,
    VERDICT_KNOWLEDGE_GRAPH,
    VERDICT_MATCHED,
    VERDICT_NO_MATCH,
    ambiguous_candidates,
    normalize_system_id,
    resolve_system_id,
)

try:
    from unit._demo_catalog import DEMO_SYSTEMS
except ImportError:  # pragma: no cover - alternate invocation path
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from _demo_catalog import DEMO_SYSTEMS


# --------------------------------------------------------------------------- #
# Canonical ids pass through unchanged
# --------------------------------------------------------------------------- #
def test_exact_id_passes_through():
    assert normalize_system_id("mes-mil", DEMO_SYSTEMS) == "mes-mil"
    assert normalize_system_id("erp", DEMO_SYSTEMS) == "erp"


# --------------------------------------------------------------------------- #
# Display-name → canonical id
# --------------------------------------------------------------------------- #
def test_display_name_resolves_to_canonical_id():
    assert normalize_system_id("Milwaukee MES", DEMO_SYSTEMS) == "mes-mil"


def test_name_match_is_case_insensitive_and_separator_tolerant():
    assert normalize_system_id("milwaukee mes", DEMO_SYSTEMS) == "mes-mil"
    assert normalize_system_id("MILWAUKEE-MES", DEMO_SYSTEMS) == "mes-mil"


def test_normalized_id_form_resolves():
    # "MES-MIL" is not an exact id but normalizes onto the id token.
    assert normalize_system_id("MES-MIL", DEMO_SYSTEMS) == "mes-mil"


def test_exact_name_beats_ambiguous_type():
    # Two catalog entries have system_type CMMS ("cmms" + "cmms2"), but the
    # name "CMMS" uniquely matches the first entry's name.
    assert normalize_system_id("CMMS", DEMO_SYSTEMS) == "cmms"


# --------------------------------------------------------------------------- #
# system_type resolution — unique only, never guess
# --------------------------------------------------------------------------- #
def test_unique_system_type_resolves():
    assert normalize_system_id("ERP", DEMO_SYSTEMS) == "erp"
    # "Historian" matches no name/id but exactly one entry's system_type.
    assert normalize_system_id("Historian", DEMO_SYSTEMS) == "sitewise-direct"


def test_ambiguous_system_type_passes_through_unchanged():
    # Three MES systems in the demo catalog — a bare "MES" must never guess.
    assert normalize_system_id("MES", DEMO_SYSTEMS) == "MES"


def test_ambiguous_type_with_custom_catalog():
    known = [
        {"id": "a1", "name": "Alpha One", "plant": "P1", "system_type": "MES"},
        {"id": "a2", "name": "Alpha Two", "plant": "P2", "system_type": "MES"},
    ]
    assert normalize_system_id("MES", known) == "MES"
    assert normalize_system_id("Alpha One", known) == "a1"


# --------------------------------------------------------------------------- #
# Knowledge-graph tokens → None sentinel (caller drops)
# --------------------------------------------------------------------------- #
def test_knowledge_graph_display_name_is_none():
    assert normalize_system_id("Knowledge Graph", DEMO_SYSTEMS) is None


def test_digital_thread_runtime_id_is_none():
    assert normalize_system_id("digital-thread", DEMO_SYSTEMS) is None


def test_knowledge_graph_display_variant_is_none():
    assert normalize_system_id("Digital Thread (knowledge graph)", DEMO_SYSTEMS) is None


# --------------------------------------------------------------------------- #
# Unknown systems pass through unchanged (base-mode systems must survive)
# --------------------------------------------------------------------------- #
def test_unknown_system_passes_through():
    assert normalize_system_id("some-custom-sys", DEMO_SYSTEMS) == "some-custom-sys"


def test_empty_catalog_passes_through():
    assert normalize_system_id("Milwaukee MES", []) == "Milwaukee MES"


def test_empty_and_none_values_pass_through():
    assert normalize_system_id("", DEMO_SYSTEMS) == ""
    assert normalize_system_id(None, DEMO_SYSTEMS) is None


def test_ambiguous_name_match_passes_through():
    # Two entries whose names normalize identically — never guess between them.
    known = [
        {"id": "s1", "name": "Line One", "plant": "P1", "system_type": "MES"},
        {"id": "s2", "name": "line-one", "plant": "P2", "system_type": "SCADA"},
    ]
    assert normalize_system_id("LINE ONE", known) == "LINE ONE"


# --------------------------------------------------------------------------- #
# resolve_system_id — the verdict-exposing sibling: callers
# can distinguish an AMBIGUOUS passthrough from a NO-MATCH passthrough.
# --------------------------------------------------------------------------- #
def test_resolve_matched_verdicts():
    assert resolve_system_id("mes-mil", DEMO_SYSTEMS) == ("mes-mil", VERDICT_MATCHED)
    assert resolve_system_id("Milwaukee MES", DEMO_SYSTEMS) == ("mes-mil", VERDICT_MATCHED)
    assert resolve_system_id("ERP", DEMO_SYSTEMS) == ("erp", VERDICT_MATCHED)


def test_resolve_ambiguous_type_verdict():
    # Three MES systems — passthrough, and the verdict says WHY.
    assert resolve_system_id("MES", DEMO_SYSTEMS) == ("MES", VERDICT_AMBIGUOUS)


def test_resolve_ambiguous_name_verdict():
    known = [
        {"id": "s1", "name": "Line One", "plant": "P1", "system_type": "MES"},
        {"id": "s2", "name": "line-one", "plant": "P2", "system_type": "SCADA"},
    ]
    assert resolve_system_id("LINE ONE", known) == ("LINE ONE", VERDICT_AMBIGUOUS)


def test_resolve_no_match_verdict():
    assert resolve_system_id("some-custom-sys", DEMO_SYSTEMS) == (
        "some-custom-sys", VERDICT_NO_MATCH)
    # The handoff's live junk shapes are all no-match against the demo catalog.
    for junk in ("us.amazon.nova-2-lite-v1:0", "None",
                 "2baf4c3a-0000-0000-0000-000000000000", "SITE1"):
        assert resolve_system_id(junk, DEMO_SYSTEMS) == (junk, VERDICT_NO_MATCH)


def test_resolve_knowledge_graph_and_empty_verdicts():
    assert resolve_system_id("Knowledge Graph", DEMO_SYSTEMS) == (
        None, VERDICT_KNOWLEDGE_GRAPH)
    assert resolve_system_id("", DEMO_SYSTEMS) == ("", VERDICT_EMPTY)
    assert resolve_system_id(None, DEMO_SYSTEMS) == (None, VERDICT_EMPTY)


def test_normalize_is_a_thin_wrapper_over_resolve():
    # The wrapper's resolved value must stay byte-identical to resolve()[0]
    # for every resolution class.
    for value in ("mes-mil", "Milwaukee MES", "ERP", "MES", "some-custom-sys",
                  "Knowledge Graph", "", None):
        assert normalize_system_id(value, DEMO_SYSTEMS) == \
            resolve_system_id(value, DEMO_SYSTEMS)[0]


# --------------------------------------------------------------------------- #
# Import purity (ADR 0001 / ADR 0020/0021): stdlib only, no catalog import.
# --------------------------------------------------------------------------- #
def test_module_is_import_pure_symbols():
    import agents.system_normalize as sn
    assert not hasattr(sn, "boto3")
    assert not hasattr(sn, "pgdriver")
    assert not hasattr(sn, "catalog")


def test_imports_with_no_pg8000_or_aws():
    """Fresh interpreter with pg8000/agents.pgdriver/boto3/botocore BLOCKED still
    imports the module and resolves — proving zero AWS/DB at import time."""
    code = (
        "import sys\n"
        "for _m in ('pg8000', 'agents.pgdriver', 'boto3', 'botocore'):\n"
        "    sys.modules[_m] = None\n"
        "from agents.system_normalize import ambiguous_candidates, normalize_system_id\n"
        "known = [{'id': 'erp', 'name': 'ERP', 'plant': 'All Sites', 'system_type': 'ERP'}]\n"
        "assert normalize_system_id('ERP', known) == 'erp'\n"
        "assert normalize_system_id('Knowledge Graph', known) is None\n"
        "two_mes = [{'id': 'a1', 'name': 'Alpha One', 'system_type': 'MES'},\n"
        "           {'id': 'a2', 'name': 'Alpha Two', 'system_type': 'MES'}]\n"
        "assert ambiguous_candidates('MES', two_mes) == ['a1', 'a2']\n"
        "print('IMPORT-PURE-OK')\n"
    )
    adt_root = pathlib.Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(adt_root))
    r = subprocess.run([sys.executable, "-c", code], cwd=str(adt_root),  # nosec B603 - fixed argv list, no shell, internal inputs only
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "IMPORT-PURE-OK" in r.stdout


# --------------------------------------------------------------------------- #
# Token-SET name matching (F7) — order-insensitive, ambiguity preserved
# --------------------------------------------------------------------------- #
def test_reversed_token_order_resolves():
    # The concatenated normal form is order-sensitive ("mesmilwaukee" !=
    # "milwaukeemes") — before F7 this was a silent no_match drop for
    # agent-sourced proposals. The token SET {mes, milwaukee} matches.
    resolved, verdict = resolve_system_id("MES Milwaukee", DEMO_SYSTEMS)
    assert (resolved, verdict) == ("mes-mil", VERDICT_MATCHED)


def test_original_token_order_still_resolves():
    resolved, verdict = resolve_system_id("Milwaukee MES", DEMO_SYSTEMS)
    assert (resolved, verdict) == ("mes-mil", VERDICT_MATCHED)


def test_token_set_match_is_casefold_and_separator_tolerant():
    assert normalize_system_id("mes, milwaukee", DEMO_SYSTEMS) == "mes-mil"
    assert normalize_system_id("MES-MILWAUKEE", DEMO_SYSTEMS) == "mes-mil"


def test_ambiguous_token_set_passes_through_unchanged():
    # Two systems whose names are token-set-equal to the input ({alpha, mes})
    # → AMBIGUOUS passthrough, never guess (same semantics as the normal-form
    # rung: 2+ candidates through EITHER form is ambiguous).
    known = [
        {"id": "mes-a", "name": "MES Alpha", "system_type": "MES"},
        {"id": "mes-b", "name": "Alpha MES", "system_type": "MES"},
    ]
    resolved, verdict = resolve_system_id("alpha mes", known)
    assert (resolved, verdict) == ("alpha mes", VERDICT_AMBIGUOUS)


def test_token_set_does_not_match_subsets():
    # {milwaukee} is a strict subset of {milwaukee, mes} — sets must be EQUAL,
    # a partial mention must not resolve (that would be guessing).
    known = [{"id": "mes-mil", "name": "Milwaukee MES", "system_type": "MES"}]
    resolved, verdict = resolve_system_id("Milwaukee", known)
    assert (resolved, verdict) == ("Milwaukee", VERDICT_NO_MATCH)


# --------------------------------------------------------------------------- #
# ambiguous_candidates (R1 primitive) — the sorted canonical ids an AMBIGUOUS
# reference matches; empty for every other verdict. Shares the ladder's
# matching internals (``_resolve``), so it can never drift from the verdict.
# --------------------------------------------------------------------------- #
def test_ambiguous_type_yields_sorted_candidates():
    # Three MES systems in the demo catalog — the candidates name all three.
    assert ambiguous_candidates("MES", DEMO_SYSTEMS) == ["mes-cr", "mes-det", "mes-mil"]


def test_matched_reference_yields_no_candidates():
    # "ERP" is a UNIQUE type match → matched, not ambiguous.
    assert ambiguous_candidates("ERP", DEMO_SYSTEMS) == []
    # Exact canonical id and unique display name are matched too.
    assert ambiguous_candidates("mes-mil", DEMO_SYSTEMS) == []
    assert ambiguous_candidates("Milwaukee MES", DEMO_SYSTEMS) == []


def test_no_match_reference_yields_no_candidates():
    assert ambiguous_candidates("garbage-xyz", DEMO_SYSTEMS) == []


def test_empty_and_knowledge_graph_yield_no_candidates():
    assert ambiguous_candidates("", DEMO_SYSTEMS) == []
    assert ambiguous_candidates(None, DEMO_SYSTEMS) == []
    assert ambiguous_candidates("Knowledge Graph", DEMO_SYSTEMS) == []


def test_ambiguous_name_rung_yields_candidates():
    # Rung-3 ambiguity (two names normalizing identically) surfaces both ids,
    # sorted — not just the type rung.
    known = [
        {"id": "s2", "name": "line-one", "plant": "P2", "system_type": "SCADA"},
        {"id": "s1", "name": "Line One", "plant": "P1", "system_type": "MES"},
    ]
    assert ambiguous_candidates("LINE ONE", known) == ["s1", "s2"]


def test_candidates_agree_with_resolve_verdict():
    # Contract: non-empty candidates IFF the verdict is AMBIGUOUS.
    for value in ("mes-mil", "Milwaukee MES", "ERP", "MES", "CMMS",
                  "some-custom-sys", "Knowledge Graph", "", None):
        _, verdict = resolve_system_id(value, DEMO_SYSTEMS)
        candidates = ambiguous_candidates(value, DEMO_SYSTEMS)
        if verdict == VERDICT_AMBIGUOUS:
            assert candidates, f"{value!r}: ambiguous but no candidates"
            assert candidates == sorted(candidates)
        else:
            assert candidates == [], f"{value!r}: {verdict} yielded {candidates}"


# --------------------------------------------------------------------------- #
# Plant resolution (rung 4b) — live incident 2026-07-14: the analyzer emitted
# proposals whose from_system was a PLANT/site name, which no earlier rung
# matched (silent no_match). A plant hosting exactly ONE system resolves to
# that system's id; a plant hosting SEVERAL is AMBIGUOUS (candidates exposed)
# — never guess.
# --------------------------------------------------------------------------- #
_PLANT_CATALOG = [
    {"id": "siteb-athena", "name": "Site B Athena", "plant": "SITEB",
     "system_type": "Data Lake"},
    {"id": "siteb-sitewise", "name": "Site B SiteWise", "plant": "SITEB",
     "system_type": "Historian"},
    {"id": "sitec-hist", "name": "Site C Historian", "plant": "Site C",
     "system_type": "Historian"},
]


def test_unique_plant_resolves_to_its_single_system():
    resolved, verdict = resolve_system_id("Site C", _PLANT_CATALOG)
    assert (resolved, verdict) == ("sitec-hist", VERDICT_MATCHED)


def test_plant_match_is_casefold_and_separator_tolerant():
    assert normalize_system_id("site c", _PLANT_CATALOG) == "sitec-hist"
    assert normalize_system_id("SITE-C", _PLANT_CATALOG) == "sitec-hist"


def test_multi_system_plant_is_ambiguous_with_candidates():
    # The incident shape: a plant hosting TWO systems — passthrough, verdict
    # AMBIGUOUS, and the candidates name both onboarded ids so review (and the
    # display-time block) can state what to confirm.
    resolved, verdict = resolve_system_id("SITEB", _PLANT_CATALOG)
    assert (resolved, verdict) == ("SITEB", VERDICT_AMBIGUOUS)
    assert ambiguous_candidates("SITEB", _PLANT_CATALOG) == [
        "siteb-athena", "siteb-sitewise"]


def test_demo_catalog_unique_plant_resolves():
    # "Milwaukee" names the plant of exactly one demo system.
    assert normalize_system_id("Milwaukee", DEMO_SYSTEMS) == "mes-mil"


def test_demo_catalog_shared_plant_is_ambiguous():
    # Several demo systems share the "All Sites" plant — never guess.
    resolved, verdict = resolve_system_id("All Sites", DEMO_SYSTEMS)
    assert (resolved, verdict) == ("All Sites", VERDICT_AMBIGUOUS)


def test_type_rung_beats_plant_rung():
    # A value matching BOTH a unique system_type and a plant resolves through
    # the earlier type rung (ladder order preserved: first hit wins).
    known = [
        {"id": "h1", "name": "Hist One", "plant": "Depot", "system_type": "Historian"},
        {"id": "d1", "name": "Depot DB", "plant": "Historian", "system_type": "SQL"},
    ]
    assert normalize_system_id("Historian", known) == "h1"


def test_plant_absent_from_entries_still_no_match():
    # Catalog entries without a plant key never match through the plant rung.
    known = [{"id": "s1", "name": "Sys One", "system_type": "MES"}]
    assert resolve_system_id("SITEB", known) == ("SITEB", VERDICT_NO_MATCH)
