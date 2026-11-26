# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for scripts/smoke_prompts.py prompt grouping.

Covers the two additive run-time grouping flags:

- ``--prompts-file`` — source questions from a golden-set-shaped JSON file
  (the SAME shape eval_answers.py --golden-set reads, so one file drives both
  lanes without drift). Malformed files raise, never run an empty suite.
- ``--category`` — case-insensitive equality filter over sourced prompts.
  Grouping is a run-time selection ONLY: the deployed prompt library is never
  modified, and blank category means no filtering (existing behavior pinned).

Also pins the dual-lane contract: ONE golden-set file (with markings and
non-gating `expected_grounding` reference text) loads through BOTH lanes —
eval_answers.load_golden_set (markings enforced via require_markings) and
smoke_prompts.load_prompts_file — using a synthetic fixture. Customer-specific
golden sets live OUTSIDE this repo (adjacent folder) per the
customer-neutrality rule; no dataset literals here.
"""
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import eval_answers  # noqa: E402
from scripts.smoke_prompts import (  # noqa: E402
    build_parser,
    filter_prompts_by_category,
    load_prompts_file,
)


def _write_golden(tmp_path, questions, version=1):
    path = tmp_path / "goldens.json"
    path.write_text(json.dumps({"version": version, "questions": questions}))
    return str(path)


# ---------------------------------------------------------------------------
# load_prompts_file
# ---------------------------------------------------------------------------

def test_load_prompts_file_maps_golden_shape_to_prompts(tmp_path):
    path = _write_golden(tmp_path, [
        {"id": "g-a", "text": "question a?", "category": "site-golden",
         "expect": {"systems_min": 1}},
        {"id": "g-b", "text": "question b?"},
    ])
    prompts = load_prompts_file(path)
    assert prompts == [
        {"title": "g-a", "text": "question a?", "category": "site-golden",
         "graph_only": False},
        {"title": "g-b", "text": "question b?", "category": "", "graph_only": False},
    ]


def test_load_prompts_file_declared_graph_only_requires_both_bounds(tmp_path):
    """graph_only is an EXPLICIT declaration: systems_min == 0 AND neptune_min >= 1."""
    path = _write_golden(tmp_path, [
        {"id": "g-graph", "text": "q?", "expect": {"systems_min": 0, "neptune_min": 1}},
        {"id": "g-sys", "text": "q?", "expect": {"systems_min": 1, "neptune_min": 1}},
        {"id": "g-nograph", "text": "q?", "expect": {"systems_min": 0}},
        {"id": "g-noexpect", "text": "q?"},
    ])
    flags = {p["title"]: p["graph_only"] for p in load_prompts_file(path)}
    assert flags == {"g-graph": True, "g-sys": False, "g-nograph": False,
                     "g-noexpect": False}


def test_assess_grades_declared_graph_only_on_neptune_queries():
    """The smoke gate agrees with the eval lane on a systems_min == 0 golden."""
    from scripts.smoke_prompts import _grounding, assess
    base = {"done": True, "errors": [], "answer": "grounded answer " * 6,
            "tool_summaries": [], "tool_starts": 3, "elapsed_seconds": 1.0}
    graph_hit = dict(base, summary={"total_tool_calls": 3, "system_queries": 0,
                                    "neptune_queries": 3})
    assert assess("t", graph_hit, None, graph_only=True) == []
    assert _grounding(0, None, True, 3) == "graph-grounded"
    # Same result WITHOUT the declaration still fails the default gate.
    assert any("system_queries < 1" in f for f in assess("t", graph_hit, None))
    assert _grounding(0, None, False, 3) == "ungrounded"
    # A declared graph-only prompt that never touched the graph fails closed.
    no_graph = dict(base, summary={"total_tool_calls": 1, "system_queries": 0,
                                   "neptune_queries": 0})
    assert any("neptune_queries < 1" in f for f in assess("t", no_graph, None, graph_only=True))
    # A system query still wins the label, declaration or not.
    assert _grounding(2, None, True, 0) == "system-grounded"


def test_load_prompts_file_has_no_280_char_cap(tmp_path):
    long_text = "why? " * 100  # 500 chars — registry skim would drop this
    path = _write_golden(tmp_path, [{"id": "g-long", "text": long_text}])
    prompts = load_prompts_file(path)
    assert prompts[0]["text"] == long_text


def test_load_prompts_file_count_limits(tmp_path):
    path = _write_golden(tmp_path, [
        {"id": f"g-{i}", "text": f"q{i}?"} for i in range(4)])
    assert len(load_prompts_file(path, 2)) == 2
    assert len(load_prompts_file(path, 0)) == 4
    assert len(load_prompts_file(path, None)) == 4


@pytest.mark.parametrize("payload, needle", [
    ({"version": 2, "questions": [{"id": "g", "text": "q?"}]}, "version"),
    ({"version": 1, "questions": []}, "no questions"),
    ({"version": 1}, "no questions"),
    ({"version": 1, "questions": [{"id": "g"}]}, "missing id/text"),
    ({"version": 1, "questions": [{"text": "q?"}]}, "missing id/text"),
])
def test_load_prompts_file_rejects_malformed(tmp_path, payload, needle):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=needle):
        load_prompts_file(str(path))


# ---------------------------------------------------------------------------
# filter_prompts_by_category
# ---------------------------------------------------------------------------

def _prompts():
    return [
        {"title": "a", "text": "qa", "category": "site-golden"},
        {"title": "b", "text": "qb", "category": "cross-system"},
        {"title": "c", "text": "qc", "category": "Site-Golden"},
        {"title": "d", "text": "qd", "category": ""},
    ]


def test_category_filter_is_case_insensitive_equality():
    got = filter_prompts_by_category(_prompts(), "site-golden")
    assert [p["title"] for p in got] == ["a", "c"]


def test_blank_category_disables_filtering():
    prompts = _prompts()
    assert filter_prompts_by_category(prompts, "") is prompts
    assert filter_prompts_by_category(prompts, "   ") is prompts


def test_category_filter_never_mutates_input():
    prompts = _prompts()
    filter_prompts_by_category(prompts, "site-golden")
    assert len(prompts) == 4  # library/list untouched — selection only


# ---------------------------------------------------------------------------
# parser defaults (env fallbacks)
# ---------------------------------------------------------------------------

def test_parser_defaults_are_empty_without_env():
    args = build_parser(environ={}).parse_args([])
    assert args.prompts_file == ""
    assert args.category == ""


def test_parser_env_fallbacks():
    args = build_parser(environ={
        "SMOKE_PROMPTS_FILE": "/tmp/g.json",
        "SMOKE_CATEGORY": "site-golden",
    }).parse_args([])
    assert args.prompts_file == "/tmp/g.json"
    assert args.category == "site-golden"


# ---------------------------------------------------------------------------
# dual-lane contract — ONE golden-set file drives BOTH lanes
# ---------------------------------------------------------------------------

def _dual_lane_file(tmp_path):
    """Synthetic golden set exercising markings + expected_grounding keys."""
    path = tmp_path / "external_goldens.json"
    path.write_text(json.dumps({
        "version": 1,
        "require_markings": True,
        "questions": [
            {"id": "g-ext-1", "text": "site question one?",
             "category": "customer-golden",
             "expect": {"systems_min": 1,
                        "answer_must_match": ["(?i)pattern"]},
             "improvable": True, "leak_class": "fact",
             "treatment_phase": "query-time",
             "expected_grounding": ["reference answer text (non-gating)"]},
            {"id": "g-ext-2", "text": "site question two?",
             "category": "customer-golden",
             "expect": {},
             "improvable": True, "leak_class": "semantic",
             "treatment_phase": "map-time",
             "expected_grounding": ["another reference"]},
        ],
    }))
    return str(path)


def test_external_golden_set_loads_via_eval_lane(tmp_path):
    path = _dual_lane_file(tmp_path)
    questions = eval_answers.load_golden_set(path)
    assert [q["id"] for q in questions] == ["g-ext-1", "g-ext-2"]
    for question in questions:
        assert question["improvable"] is True
        assert question["leak_class"] in ("fact", "semantic")
        assert question["treatment_phase"] in ("query-time", "map-time")
        # Reference groundings survive the loader for human gap analysis.
        assert question["expected_grounding"]


def test_external_golden_set_sources_smoke_prompts(tmp_path):
    path = _dual_lane_file(tmp_path)
    prompts = load_prompts_file(path)
    assert [p["title"] for p in prompts] == ["g-ext-1", "g-ext-2"]
    grouped = filter_prompts_by_category(prompts, "customer-golden")
    assert len(grouped) == 2
