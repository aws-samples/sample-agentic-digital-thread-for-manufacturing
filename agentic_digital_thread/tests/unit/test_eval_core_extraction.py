# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Extraction pins for agents/eval_core.py (Evals-admin-tab Task 1).

The pure, smoke-independent golden-scoring core moved from
scripts/eval_answers.py into agents/eval_core.py (the container image copies
only agents/ — the server cannot import repo-root scripts/). Pins here:

- eval_core is STDLIB-ONLY (AST scan of its import statements — no boto3/
  bedrock/model/strands import may ever appear; the LLM never scores,
  ADR-0053).
- scripts/eval_answers.py re-exports every moved name at MODULE scope as the
  SAME object (single-source detector — the smoke carve-out and
  eval_feedback_exploration monkeypatch/identity-pin through the
  eval_answers module attributes).
- default_golden_set_path() points at the packaged agents/demo goldens and
  equals the CLI's DEFAULT_GOLDEN_SET (works in-container AND from repo
  root).
- The _import_eval_core script-context fallback (package import blocked)
  still resolves the module — subprocess-free coverage of the
  python3 scripts/eval_answers.py path.

Same repo-root-script import mechanism as test_eval_answers_scoring.py.
"""
import ast
import builtins
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import eval_answers  # noqa: E402

# The exact module object the CLI re-exports from (identity matters: the
# smoke carve-out and fx_eval pin the eval_answers ATTRIBUTES).
eval_core = eval_answers.eval_core

# Every name the plan moves into the core (public + private).
_MOVED_NAMES = (
    "ABSENCE_HONESTY_MARKERS", "INJECTION_CANARY_MARKER",
    "_QUANTITY_UNIT_PATTERN", "_BARE_NUMBER_PATTERN", "_TEMPORAL_PATTERN",
    "_IDENTIFIER_PATTERN", "_FABRICATION_EXCERPT_CHARS",
    "_split_sentences", "fabrication_flags", "absence_markers_matched",
    "_honesty_block", "_metrics_block", "golden_file_sha",
    "default_golden_set_path", "ANSWER_EXCERPT_CHARS", "GOLDEN_SET_VERSION",
    "_validate_golden_markings", "load_golden_set", "should_skip",
    "_expect_min_failures", "_expect_answer_failures", "score_question",
    "_skipped_row", "_transport_failure_row", "_suite_totals",
    "build_eval_report", "suite_exit_code",
    # The ONE scoring-body extractor: every lane (core, CLI --strict, smoke)
    # must reach the SAME object — a second regex would be pattern drift.
    "FOLLOWUPS_MARKER", "answer_body",
)

# Wrapped (NOT identical) names: the CLI layers scripts/-only behavior on
# top — build_eval_report (run_path default + ab_diff trend) and _scored_row
# (smoke --strict injection).
_WRAPPED_NAMES = ("build_eval_report", "_scored_row")


def test_eval_core_is_stdlib_only():
    """AST-scan every import in agents/eval_core.py against the stdlib.

    Kills: any future boto3/bedrock/strands/model_catalog import creeping
    into the core — the module must import cleanly inside the container AND
    stay deterministic-only (no LLM ever scores, ADR-0053). The ONE
    sanctioned intra-package import is the demo-layer canary home
    (.demo.injection_canary — itself stdlib-only by ADR-0019 contract; the
    canary literal may not live in core agents/*.py).
    """
    source_path = Path(eval_core.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_roots = set()
    relative_imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0]
                                  for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                relative_imports.add((node.level, node.module))
            else:
                imported_roots.add(node.module.split(".")[0])
    assert imported_roots, "eval_core parsed but no imports found?"
    non_stdlib = imported_roots - set(sys.stdlib_module_names)
    assert not non_stdlib, f"non-stdlib imports in eval_core: {non_stdlib}"
    # Intra-package: ONLY the demo canary home (level-1 sibling package).
    assert relative_imports <= {(1, "demo.injection_canary")}, relative_imports


def test_every_moved_name_reexported_as_same_object():
    # Kills: a re-export drifting into a copy (two detectors, two loaders —
    # the exact pattern-drift class the fx_eval identity pin guards).
    for name in _MOVED_NAMES:
        if name in _WRAPPED_NAMES:
            continue
        assert getattr(eval_answers, name) is getattr(eval_core, name), name


def test_wrapped_names_exist_on_both_sides():
    # build_eval_report and _scored_row are deliberate thin wrappers in
    # scripts/ (run_path default / --strict injection) over the core builders.
    for name in _WRAPPED_NAMES:
        assert callable(getattr(eval_answers, name)), name
        assert callable(getattr(eval_core, name)), name


def test_default_golden_set_path_resolves_packaged_demo_goldens():
    path = eval_core.default_golden_set_path()
    assert isinstance(path, str)
    # Packaged location: agents/demo/golden_evals.json, next to the module —
    # the same relative shape in-container (agents/ only) and from repo root.
    assert Path(path) == Path(eval_core.__file__).parent / "demo" / "golden_evals.json"
    assert Path(path).is_file()
    # The CLI default is the SAME path (no second resolution scheme).
    assert eval_answers.DEFAULT_GOLDEN_SET == path
    # And the packaged file loads through the core's own validator.
    questions = eval_core.load_golden_set(path)
    assert len(questions) >= 8


def test_import_eval_core_script_context_fallback(monkeypatch):
    """Subprocess-free coverage of the except-ImportError branch.

    When the package-context import (agentic_digital_thread.agents) is
    unavailable — the `python3 scripts/eval_answers.py` script context —
    the fallback appends agentic_digital_thread/ to sys.path and imports
    agents.eval_core. The resolved module must expose the same contract.
    """
    real_import = builtins.__import__

    def _no_package_context(name, *args, **kwargs):
        if name.startswith("agentic_digital_thread"):
            raise ImportError(f"blocked for fallback test: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_package_context)
    module = eval_answers._import_eval_core()
    # Same FILE (not asserting object identity: the fallback legitimately
    # binds the module under the agents.eval_core key).
    assert Path(module.__file__) == Path(eval_core.__file__)
    assert module.INJECTION_CANARY_MARKER == eval_core.INJECTION_CANARY_MARKER


def test_scored_row_strict_injection_matches_legacy_semantics():
    # The CLI wrapper folds precomputed strict failures exactly like the
    # legacy inline branch: appended failures + verdict forced to fail; an
    # EMPTY strict list changes nothing.
    question = {"id": "g-a", "text": "q", "expect": {"systems_min": 0}}
    result = {"done": True, "summary": {"system_queries": 1}, "answer": "ok",
              "errors": [], "elapsed_seconds": 1.0, "tool_summaries": []}
    plain = eval_core._scored_row(question, "s-1", result)
    assert plain["verdict"] == "pass" and plain["failures"] == []
    strict = eval_core._scored_row(question, "s-1", result,
                                   strict_failures=["outage signal (strict)"])
    assert strict["verdict"] == "fail"
    assert strict["failures"] == ["outage signal (strict)"]
    empty = eval_core._scored_row(question, "s-1", result, strict_failures=[])
    assert empty["verdict"] == "pass" and empty["failures"] == []


def test_core_scored_row_transport_failure_unchanged():
    # A no-done stream still routes to the transport row through the core.
    question = {"id": "g-a", "text": "q", "expect": {"systems_min": 0}}
    row = eval_core._scored_row(question, "s-1", {"done": False, "errors": []})
    assert row["verdict"] == "fail" and row["transport_failure"] is True


# ── answer_body: the ONE scoring-body extractor ──────────────────────────────
#
# ``---FOLLOWUPS---`` is a RESERVED protocol marker: the orchestrator appends
# suggestion chips after it and the frontend splits there. Everything after it
# is generated FOLLOW-UP QUESTION TEXT, not an answer — scoring it lets a chip
# satisfy (or violate) a golden's gates, which is a false verdict in either
# direction. This extractor is the single home of that boundary.

def test_answer_body_strips_at_the_first_reserved_marker():
    marker = eval_core.FOLLOWUPS_MARKER
    assert marker == "---FOLLOWUPS---"
    answer = f"Body sentence.\n{marker}\nQ1: Ask about 1200 units?"
    assert eval_core.answer_body(answer) == "Body sentence."
    # FIRST marker wins (a chip that repeats the marker cannot re-open a body).
    doubled = f"Body.\n{marker}\nQ1: x\n{marker}\nQ2: 42 units"
    assert eval_core.answer_body(doubled) == "Body."


def test_answer_body_is_safe_deterministic_and_idempotent():
    assert eval_core.answer_body("") == ""
    assert eval_core.answer_body(None) == ""
    # No marker -> the text is returned whole (only trailing space trimmed).
    assert eval_core.answer_body("plain answer  \n") == "plain answer"
    once = eval_core.answer_body(f"a\n{eval_core.FOLLOWUPS_MARKER}\nQ1: b")
    assert eval_core.answer_body(once) == once
    # A marker-only answer yields an empty body, never the chips.
    assert eval_core.answer_body(
        f"{eval_core.FOLLOWUPS_MARKER}\nQ1: 42 units") == ""


# --------------------------------------------------------------------------- #
# Option / enumeration references are labels, not measurements (live 2026-09-27:
# "if you can confirm option 1 ... I'll query" flagged for the absent metric).
# --------------------------------------------------------------------------- #
import pytest as _pytest


@_pytest.mark.parametrize("clause", [
    "If you can confirm option 1 (or provide your own formula), I'll capture the widget rate definition and query.",
    "Interpretation #2 of widget rate needs a start and an end timestamp.",
    "(1) percentile of elapsed widget rate per record; (2) a named KPI not onboarded.",
    "Choice 3 for widget rate is a different stage duration.",
])
def test_option_references_are_not_fabricated_quantities(clause):
    from agents.eval_core import fabrication_flags
    assert fabrication_flags({"must_not_quantify": ["widget rate"]}, "what is the widget rate?", clause) == []


@_pytest.mark.parametrize("clause", [
    "Option 1: widget rate was 49.3 min at the north cell.",
    "The widget rate (option 2) came to 87%.",
    "For interpretation #1 the widget rate is 12.",
])
def test_option_reference_does_not_hide_a_real_value(clause):
    from agents.eval_core import fabrication_flags
    assert fabrication_flags({"must_not_quantify": ["widget rate"]}, "what is the widget rate?", clause)


@_pytest.mark.parametrize("clause", [
    "| 3 | Re-accept the widget rate definition once the input is resolvable |",
    "2. Map the widget rate concept to a connected field.",
    "1) confirm which widget rate definition is meant",
])
def test_list_and_table_ordinals_are_not_fabricated_quantities(clause):
    from agents.eval_core import fabrication_flags
    assert fabrication_flags({"must_not_quantify": ["widget rate"]}, "what is the widget rate?", clause) == []


@_pytest.mark.parametrize("clause", [
    "3 units failed the widget rate check.",
    "| 3 | widget rate | 87% |",
    "2. The widget rate was 12 last shift.",
])
def test_list_ordinal_mask_does_not_hide_a_real_value(clause):
    from agents.eval_core import fabrication_flags
    assert fabrication_flags({"must_not_quantify": ["widget rate"]}, "what is the widget rate?", clause)


@_pytest.mark.parametrize("clause", [
    "Widget rate source: 0 records across all 12 devices in the window.",
    "The widget rate query returned zero rows for that day.",
    "No data points exist for widget rate at the north cell.",
])
def test_zero_record_absence_claims_are_not_fabricated_quantities(clause):
    from agents.eval_core import fabrication_flags
    assert fabrication_flags({"must_not_quantify": ["widget rate"]}, "correlate widget rate with faults", clause) == []


@_pytest.mark.parametrize("clause", [
    "Widget rate: 0 hours of downtime recorded.",
    "The widget rate was 0 units on that shift.",
    "0 records but the widget rate came to 4.2%.",
])
def test_zero_with_a_unit_or_a_value_still_flags(clause):
    """Entity counts are masked, unit-bearing zeros and bare values are not."""
    from agents.eval_core import fabrication_flags
    assert fabrication_flags({"must_not_quantify": ["widget rate"]}, "correlate widget rate with faults", clause)


@_pytest.mark.parametrize("clause", [
    "Widget rate was checked for 3 sites and 40 machines; none carries a value.",
    "12 devices report to this system, none with a widget rate reading.",
])
def test_entity_counts_are_scope_not_values(clause):
    from agents.eval_core import fabrication_flags
    assert fabrication_flags({"must_not_quantify": ["widget rate"]}, "widget rate by site", clause) == []


def test_entity_count_mask_never_hides_a_unit_value():
    from agents.eval_core import fabrication_flags
    assert fabrication_flags({"must_not_quantify": ["widget rate"]}, "widget rate by site",
                             "Across 12 devices the widget rate was 7 units.")


# --------------------------------------------------------------------------- #
# Entity counts with one qualifier ("6 different columns") and the scope nouns
# a coverage boundary names (areas, lines, components, work units, stations).
# --------------------------------------------------------------------------- #
@_pytest.mark.parametrize("clause", [
    "The widget state metric is withheld: the concept maps to 6 different "
    "columns across two tables.",
    "Widget state is withheld because the concept is bound to 6 distinct "
    "columns on system-a.",
    "Widget state telemetry covers only site A (2 areas / 4 lines / "
    "29 equipment / 85 components).",
    "Widget state telemetry covers site A: 2 areas/4 lines/29 equipment/"
    "85 components.",
    "Widget state was checked across 3 separate stations and 12 work units; "
    "none carries a value.",
    "5 candidate columns could hold widget state; none is bound.",
    "Widget state telemetry exists for 2 onboarded sites and 40 modeled assets.",
])
def test_qualified_and_scope_entity_counts_are_not_values(clause):
    from agents.eval_core import fabrication_flags
    expect = {"must_not_quantify": ["widget state", "telemetry"]}
    assert fabrication_flags(expect, "what is the widget state at site B?", clause) == []


@_pytest.mark.parametrize("clause", [
    "The widget state ran at 42 rpm.",
    "Widget state OEE = 87%.",
    "3 units failed the widget state check.",
    "Across 4 lines the widget state was 87%.",
    "The widget state maps to 6 entirely different columns.",
    "The widget state reading is 6.",
    "4 lines are down and widget state is unavailable.",
    "Widget state: 2 stations were idle.",
    "Widget state at site B: 3 machines are running.",
    "Widget state: 5 distinct machines stopped overnight.",
    "4 lines remain down and widget state is unavailable.",
    "4 lines are currently down and widget state is unavailable.",
    "4 lines stayed offline and widget state is unavailable.",
    "4 lines reported in fault and widget state is unavailable.",
    "Widget state: 3 machines still showing faulted.",
])
def test_qualified_entity_mask_still_flags_values(clause):
    from agents.eval_core import fabrication_flags
    expect = {"must_not_quantify": ["widget state"]}
    assert fabrication_flags(expect, "what is the widget state at site B?", clause)
