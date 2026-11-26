# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Task 1a — the deterministic PRE-map vocabulary-fit report (ADR-0068).

Pins the properties the onboarding conversation depends on: the fit signal is
COMPUTED (same input twice ⇒ identical dict, no LLM anywhere), the module is
import-pure (safe in the onboarding runtime), the suggestion thresholds act
exactly at their boundaries, untrusted input size is capped, and empty input
never nags. All fixture names are placeholder-shaped (neutrality ratchet).
"""
from __future__ import annotations

import copy

from agents import vocab_fit
from agents.vocab_fit import (
    FIT_LOW_PCT,
    FIT_OK_PCT,
    MAX_COLUMNS,
    MAX_TABLES,
    compute_fit,
    extract_synonyms,
    is_generic_column,
)


def _table(name: str, column_names: list[str]) -> dict:
    """One discover-shaped table (the session store's discovered_tables row)."""
    return {
        "name": name,
        "columns": [{"name": c, "data_type": "text"} for c in column_names],
        "row_count": 10,
        "sample_values": {},
    }


def _concepts(*ids: str) -> list[dict]:
    return [{"id": i, "name": i.replace("-", " "), "description": f"The {i}."}
            for i in ids]


def _fixture_for_pct(matched: int, unmatched: int) -> tuple[list, list]:
    """Tables/concepts where exactly ``matched`` of ``matched+unmatched``
    signal columns have a vocabulary candidate."""
    cols = [f"term{i}" for i in range(matched)]
    cols += [f"nocand{i}" for i in range(unmatched)]
    return [_table("system-a-things", cols)], _concepts(
        *[f"term{i}" for i in range(matched)])


# --------------------------------------------------------------------------- #
# Determinism + shape
# --------------------------------------------------------------------------- #
def test_same_input_twice_yields_identical_dict():
    tables, concepts = _fixture_for_pct(3, 2)
    first = compute_fit(copy.deepcopy(tables), copy.deepcopy(concepts))
    second = compute_fit(copy.deepcopy(tables), copy.deepcopy(concepts))
    assert first == second


def test_inputs_are_not_mutated():
    tables, concepts = _fixture_for_pct(2, 1)
    t_snap, c_snap = copy.deepcopy(tables), copy.deepcopy(concepts)
    compute_fit(tables, concepts)
    assert tables == t_snap and concepts == c_snap


def test_report_carries_exactly_the_pinned_keys():
    tables, concepts = _fixture_for_pct(1, 1)
    assert set(compute_fit(tables, concepts)) == {
        "total_columns", "columns_with_candidate", "fit_pct",
        "vocabulary_size", "unmatched_columns", "generic_columns",
        "suggestion",
    }


def test_module_is_import_pure():
    """Importing must not pull in a cloud client, a DB driver, or the graph.

    Checked in a SUBPROCESS: asserting against ``sys.modules`` in-process is
    vacuous once any sibling test has imported boto3.
    """
    import subprocess  # nosec B404 - fixed internal argv list only
    import sys

    code = (
        "import sys;"
        "import agents.vocab_fit;"
        "bad=[m for m in ('boto3','pg8000','agents.pgdriver','botocore','strands',"
        "'agents.db','agents.catalog','agents.concept_suggest')"
        " if m in sys.modules];"
        "print('LEAKED:'+','.join(bad) if bad else 'CLEAN')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,  # nosec B603
                         text=True, cwd=".", check=False)
    assert "CLEAN" in out.stdout, out.stdout + out.stderr


# --------------------------------------------------------------------------- #
# Candidate detection (vocabulary-vs-schema token match — no name heuristics)
# --------------------------------------------------------------------------- #
def test_column_matches_concept_id_tokens():
    tables = [_table("system-a-work", ["work_state_code", "attempt_no"])]
    result = compute_fit(tables, _concepts("work-state"))
    assert result["columns_with_candidate"] == 1
    assert result["unmatched_columns"] == ["attempt_no"]


def test_column_matches_concept_name_tokens():
    tables = [_table("system-a-work", ["attempt_number_val"])]
    concepts = [{"id": "atn", "name": "Attempt Number", "description": ""}]
    assert compute_fit(tables, concepts)["columns_with_candidate"] == 1


def test_column_matches_description_synonym_clause():
    concepts = [{"id": "work-state", "name": "Work state",
                 "description": "The processing state. "
                                "Synonyms (common): stage code, phase."}]
    tables = [_table("system-a-work", ["stage_code", "phase", "widget_ref"])]
    result = compute_fit(tables, concepts)
    assert result["columns_with_candidate"] == 2
    assert result["unmatched_columns"] == ["widget_ref"]


def test_extract_synonyms_parses_the_clause():
    text = "A thing. Synonyms (common): stage code, phase; step."
    assert extract_synonyms(text) == ["stage code", "phase", "step"]
    assert extract_synonyms("No clause here.") == []
    assert extract_synonyms("") == []


# --------------------------------------------------------------------------- #
# Generic-column exclusion
# --------------------------------------------------------------------------- #
def test_generic_columns_counted_but_excluded_from_signal():
    tables = [_table("system-a-work",
                     ["id", "created_at", "updated_at", "work_state", "attempt_no"])]
    result = compute_fit(tables, _concepts("work-state"))
    assert result["generic_columns"] == 3
    assert result["total_columns"] == 5
    # signal = 2, matched = 1 -> 50%
    assert result["fit_pct"] == 50
    assert result["unmatched_columns"] == ["attempt_no"]


def test_qualified_identifier_columns_stay_in_the_signal():
    assert is_generic_column("id") and is_generic_column("created_at")
    assert not is_generic_column("widget_id")
    assert not is_generic_column("work_order_created_at_site")


def test_all_generic_columns_means_nothing_to_fit():
    tables = [_table("system-a-audit", ["id", "created_at", "updated_at"])]
    result = compute_fit(tables, _concepts("work-state"))
    assert result["fit_pct"] == 0
    assert result["suggestion"] == "ok"


# --------------------------------------------------------------------------- #
# Threshold boundaries (exact — the constants are pinned)
# --------------------------------------------------------------------------- #
def test_exactly_ok_threshold_is_ok():
    tables, concepts = _fixture_for_pct(7, 3)  # 70%
    result = compute_fit(tables, concepts)
    assert result["fit_pct"] == FIT_OK_PCT == 70
    assert result["suggestion"] == "ok"


def test_just_below_ok_threshold_asks_context():
    tables, concepts = _fixture_for_pct(69, 31)  # 69%
    result = compute_fit(tables, concepts)
    assert result["fit_pct"] == 69
    assert result["suggestion"] == "ask_context"


def test_exactly_low_threshold_asks_context():
    tables, concepts = _fixture_for_pct(4, 6)  # 40%
    result = compute_fit(tables, concepts)
    assert result["fit_pct"] == FIT_LOW_PCT == 40
    assert result["suggestion"] == "ask_context"


def test_below_low_threshold_suggests_pack():
    tables, concepts = _fixture_for_pct(39, 61)  # 39%
    result = compute_fit(tables, concepts)
    assert result["fit_pct"] == 39
    assert result["suggestion"] == "consider_pack"


# --------------------------------------------------------------------------- #
# Empty + capped input
# --------------------------------------------------------------------------- #
def test_empty_tables_never_nag():
    result = compute_fit([], _concepts("work-state", "widget"))
    assert result["total_columns"] == 0
    assert result["fit_pct"] == 0
    assert result["suggestion"] == "ok"
    assert result["unmatched_columns"] == []
    assert result["vocabulary_size"] == 2


def test_table_cap_enforced_on_oversized_input():
    tables = [_table(f"t{i}", [f"col{i}"]) for i in range(MAX_TABLES + 50)]
    result = compute_fit(tables, _concepts("work-state"))
    assert result["total_columns"] == MAX_TABLES


def test_column_cap_enforced_across_tables():
    per_table = (MAX_COLUMNS // 2) + 100
    tables = [_table("t1", [f"a{i}" for i in range(per_table)]),
              _table("t2", [f"b{i}" for i in range(per_table)])]
    result = compute_fit(tables, _concepts("work-state"))
    assert result["total_columns"] == MAX_COLUMNS


def test_unmatched_sample_is_name_only_and_capped_at_20():
    cols = [f"nocand{i}" for i in range(30)]
    result = compute_fit([_table("system-a-wide", cols)], _concepts("work-state"))
    assert len(result["unmatched_columns"]) == 20
    assert all(isinstance(n, str) for n in result["unmatched_columns"])
    assert result["unmatched_columns"][0] == "nocand0"


def test_duplicate_unmatched_names_dedupe_in_the_sample():
    tables = [_table("t1", ["widget_ref"]), _table("t2", ["widget_ref"])]
    result = compute_fit(tables, _concepts("work-state"))
    assert result["total_columns"] == 2
    assert result["unmatched_columns"] == ["widget_ref"]


# --------------------------------------------------------------------------- #
# Malformed input degrades, never raises
# --------------------------------------------------------------------------- #
def test_malformed_rows_are_ignored():
    tables = [None, "junk", {"columns": [None, {"data_type": "text"},
                                         {"name": "work_state"}]}]
    concepts = [None, "junk", {"name": "no id row"}, {"id": "work-state"}]
    result = compute_fit(tables, concepts)  # type: ignore[arg-type]
    assert result["total_columns"] == 1
    assert result["columns_with_candidate"] == 1
    assert result["vocabulary_size"] == 1


def test_none_inputs_degrade_to_empty_report():
    result = compute_fit(None, None)  # type: ignore[arg-type]
    assert result["total_columns"] == 0
    assert result["suggestion"] == "ok"


def test_synonym_regex_single_home_matches_concept_suggest():
    """vocab_fit is the ONE parser home; concept_suggest re-exports from it
    (module-level source comparison — no import of concept_suggest, which is
    not import-pure). A second ``Synonyms`` regex must never reappear there."""
    from pathlib import Path
    src = Path(vocab_fit.__file__).with_name("concept_suggest.py").read_text()
    assert "from .vocab_fit import _SYNONYM_RE, extract_synonyms" in src
    assert 're.compile(r"Synonyms' not in src  # no second parser home


# ── maintainability: the ≤50-line-function bound, pinned ────────────────────
def test_all_functions_at_most_50_lines():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(vocab_fit.__file__).read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            span = node.end_lineno - node.lineno + 1
            if span > 50:
                offenders.append(f"{node.name} ({span} lines)")
    assert not offenders, f"functions over 50 lines: {offenders}"
