# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Fixture-based unit tests for agents/agents_routes.py::parse_eval_rows (wave E1.2).

The row-parsing loop of ``_get_eval_scores`` is extracted into the pure
``parse_eval_rows`` (no boto3) so the evaluation-score parse is testable
offline against the real Logs Insights result shape:
``[[{"field": ..., "value": ...}, ...], ...]``.

``EVAL_SCORE_QUERY`` aliases its output to the ``evaluator`` / ``avg_score`` /
``count`` fields, so the fixtures here differ in the evaluator VALUES
(``Builtin.``-prefixed ids, lowercase ``builtin.``, plain metric names) plus
the degenerate edge rows the loop must skip. The queries themselves, over
records in the real result shape, are tested in
``test_eval_figure_check_scores.py``. Behavior contract: byte-identical to the pre-extraction inline
loop — empty/missing cases return ``{}``.

When a deployed env produces a real payload (via /api/admin/agents/eval-debug),
capture it as an additional fixture here.

Run: pytest agentic_digital_thread/tests/unit/test_eval_scores_parse.py -q
"""
import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents.agents_routes import parse_eval_rows  # noqa: E402


def _row(evaluator, avg_score, count):
    """One Logs Insights result row (list of field/value dicts)."""
    return [
        {"field": "evaluator", "value": evaluator},
        {"field": "avg_score", "value": avg_score},
        {"field": "count", "value": count},
    ]


# ── Format 1: evaluatorId JSON → Builtin.-prefixed evaluator ids ────────────
def test_format1_builtin_prefixed_ids():
    rows = [
        _row("Builtin.Groundedness", "0.912", "34"),
        _row("Builtin.Faithfulness", "0.874", "34"),
        _row("Builtin.AnswerRelevance", "0.951", "33"),
    ]
    assert parse_eval_rows(rows) == {
        "Groundedness": {"avg": 0.912, "count": 34},
        "Faithfulness": {"avg": 0.874, "count": 34},
        "AnswerRelevance": {"avg": 0.951, "count": 33},
    }


# ── Format 2: evaluatorName/score JSON → lowercase builtin. prefix ──────────
def test_format2_lowercase_builtin_prefix():
    rows = [_row("builtin.Groundedness", "0.8", "5")]
    assert parse_eval_rows(rows) == {"Groundedness": {"avg": 0.8, "count": 5}}


# ── Format 3: flat name/value JSON → plain metric names ─────────────────────
def test_format3_plain_names_and_rounding():
    rows = [_row("Helpfulness", "0.66666", "12")]
    # avg rounds to 3 decimals; count parses through float (Insights emits "12.0")
    assert parse_eval_rows(rows) == {"Helpfulness": {"avg": 0.667, "count": 12}}


def test_count_as_float_string():
    rows = [_row("Groundedness", "0.9", "12.0")]
    assert parse_eval_rows(rows) == {"Groundedness": {"avg": 0.9, "count": 12}}


# ── Degenerate rows — must be skipped, never raise ───────────────────────────
def test_empty_rows_return_empty_dict():
    assert parse_eval_rows([]) == {}


def test_missing_evaluator_field_skipped():
    rows = [[{"field": "avg_score", "value": "0.9"}, {"field": "count", "value": "3"}]]
    assert parse_eval_rows(rows) == {}


def test_zero_string_evaluator_skipped():
    # A failed parse regex can group-capture "0" — the loop rejects it.
    rows = [_row("0", "0.9", "3")]
    assert parse_eval_rows(rows) == {}


def test_zero_average_is_a_real_score_and_kept():
    # Rows come from the documented gen_ai.evaluation.result fields, so a 0.0
    # average over counted traces means every answer scored 0. Dropping it
    # would hide the worst case from the Evals page.
    rows = [_row("Builtin.Correctness", "0.0", "3"), _row("Groundedness", "0", "2")]
    assert parse_eval_rows(rows) == {"Correctness": {"avg": 0.0, "count": 3},
                                     "Groundedness": {"avg": 0.0, "count": 2}}


def test_negative_average_or_no_counted_traces_excluded():
    rows = [_row("Faithfulness", "-0.5", "3"), _row("Correctness", "0.8", "0")]
    assert parse_eval_rows(rows) == {}


def test_unparsable_numbers_skipped_but_valid_rows_kept():
    rows = [
        _row("Groundedness", "not-a-number", "3"),
        _row("Faithfulness", "0.5", "junk"),
        _row("AnswerRelevance", "0.75", "4"),
    ]
    assert parse_eval_rows(rows) == {"AnswerRelevance": {"avg": 0.75, "count": 4}}


def test_missing_avg_is_skipped_not_read_as_zero():
    rows = [[{"field": "evaluator", "value": "Groundedness"},
             {"field": "count", "value": "3"}]]
    assert parse_eval_rows(rows) == {}


def test_builtin_prefix_stripped_mid_string_only_prefix_forms():
    # The historical behavior is a plain str.replace — pin it exactly.
    rows = [_row("Builtin.Grounded.Builtin.ness", "0.5", "1")]
    assert parse_eval_rows(rows) == {"Grounded.ness": {"avg": 0.5, "count": 1}}
