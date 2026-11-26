# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure query-intelligence primitives (rules-applied metric, Wave 1 Task 1).

- ``is_graph_query``: the ``runtime_app._is_explore_graph`` semantics —
  bare AND gateway-prefixed spellings (mirrors
  ``test_neptune_queries_counter.py``).
- ``is_failed_result`` / ``count_wasted_calls``: the FAILED-envelope markers
  moved verbatim from the runtime agent-analysis block — matching envelopes,
  never the bare word "error" (legit in manufacturing results).
- ``format_rule_label``: defensive human label from an accepted proposal row.
- ``build_intelligence_summary``: the ONE payload shape (Decision 2) —
  neptune-only coverage, ``system_queries = total − neptune``, capped/
  truncated ``rules_applied``, and NO ``deterministic_pct`` /
  ``graph_guided_steps`` on the legacy call. The optional ``determinism``
  kwarg adds the ADDITIVE ``grounded_steps`` / ``deterministic_pct``
  keys (mechanically computed) and overrides ``wasted_calls`` with the
  hard-evidence count.

Offline: no AWS, no Neptune, no network.
"""
from __future__ import annotations

import os
import subprocess  # nosec B404 - fresh-interpreter purity check, fixed argv
import sys
from pathlib import Path

import agents.query_intelligence as qi
from agents.query_intelligence import (
    MAX_LABEL_LEN,
    MAX_RULES_APPLIED,
    build_intelligence_summary,
    count_wasted_calls,
    format_rule_label,
    is_failed_result,
    is_graph_query,
)


# --------------------------------------------------------------------------- #
# (a) is_graph_query — bare + gateway-prefixed spellings
# --------------------------------------------------------------------------- #
def test_is_graph_query_matches_bare_and_gateway_prefixed():
    # Mode-B / in-process orchestrator spelling.
    assert is_graph_query("explore_graph") is True
    # Agent-mode gateway spelling.
    assert is_graph_query("digital-thread___explore_graph") is True
    # Not the graph tool — schema fetches and system queries.
    assert is_graph_query("get_system_schema") is False
    assert is_graph_query("digital-thread___get_system_schema") is False
    assert is_graph_query("erp___query") is False


# --------------------------------------------------------------------------- #
# (b) is_failed_result — envelope markers, NOT the bare word "error"
# --------------------------------------------------------------------------- #
def test_failed_result_matches_error_and_empty_envelopes():
    assert is_failed_result("Error: connection refused") is True
    assert is_failed_result("no results") is True


def test_legit_error_word_in_data_is_not_failed():
    # "error" appears legitimately in manufacturing results (defect reasons,
    # error codes, error_rate columns) — envelope markers must not fire.
    assert is_failed_result("top defect reason: error code E42") is False


def test_failed_result_defensive_on_non_string():
    assert is_failed_result(None) is False  # type: ignore[arg-type]
    assert is_failed_result(42) is False  # type: ignore[arg-type]


def test_count_wasted_calls_skips_non_strings():
    results = [
        "Error: connection refused",   # wasted
        "query failed at step 2",      # wasted
        "42 rows returned",            # fine
        None, 42,                      # skipped defensively
    ]
    assert count_wasted_calls(results) == 2
    assert count_wasted_calls([]) == 0
    assert count_wasted_calls(None) == 0  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# (c) payload has EXACTLY the Decision-2 keys — removed keys stay removed
# --------------------------------------------------------------------------- #
def test_payload_shape_is_exactly_decision_2():
    summary = build_intelligence_summary(
        tool_names=["explore_graph", "erp___query"],
        wasted_calls=1,
        rules_applied=[{"id": "subj-0123456789abcdef", "label": "a rule"}],
        new_proposed_mappings=2,
    )
    assert set(summary) == {
        "total_tool_calls", "neptune_queries", "system_queries",
        "graph_coverage_pct", "wasted_calls", "rules_applied",
        "new_proposed_mappings",
    }
    # Deleted, not aliased (Decision 2).
    assert "deterministic_pct" not in summary
    assert "graph_guided_steps" not in summary
    assert summary["wasted_calls"] == 1
    assert summary["new_proposed_mappings"] == 2
    assert summary["rules_applied"] == [
        {"id": "subj-0123456789abcdef", "label": "a rule"}
    ]


# --------------------------------------------------------------------------- #
# (d) graph_coverage_pct is neptune-only — schema calls do not count
# --------------------------------------------------------------------------- #
def test_coverage_is_neptune_only_schema_calls_excluded():
    summary = build_intelligence_summary(
        tool_names=[
            "explore_graph",                        # neptune
            "digital-thread___explore_graph",       # neptune (gateway)
            "get_system_schema",                    # NOT neptune
            "digital-thread___get_system_schema",   # NOT neptune
            "erp___query",                          # NOT neptune
        ],
        wasted_calls=0,
        rules_applied=[],
    )
    assert summary["neptune_queries"] == 2
    assert summary["total_tool_calls"] == 5
    assert summary["graph_coverage_pct"] == 40  # round(100 * 2 / 5)


# --------------------------------------------------------------------------- #
# (e) system_queries == total − neptune
# --------------------------------------------------------------------------- #
def test_system_queries_is_total_minus_neptune():
    summary = build_intelligence_summary(
        tool_names=["explore_graph", "get_system_schema", "erp___query",
                    "mes___query"],
        wasted_calls=0,
        rules_applied=[],
    )
    assert summary["total_tool_calls"] == 4
    assert summary["neptune_queries"] == 1
    assert summary["system_queries"] == 3
    assert (summary["system_queries"]
            == summary["total_tool_calls"] - summary["neptune_queries"])


# --------------------------------------------------------------------------- #
# (f) rules_applied capped at 20 entries; labels truncated to 160 chars
# --------------------------------------------------------------------------- #
def test_rules_applied_capped_at_max():
    rules = [{"id": f"subj-{i:016x}", "label": f"rule {i}"} for i in range(25)]
    summary = build_intelligence_summary(
        tool_names=["explore_graph"], wasted_calls=0, rules_applied=rules)
    assert MAX_RULES_APPLIED == 20
    assert len(summary["rules_applied"]) == 20
    # First 20 in order — deterministic cap, not sampling.
    assert summary["rules_applied"][0]["id"] == "subj-" + "0" * 16
    assert summary["rules_applied"][-1]["id"] == f"subj-{19:016x}"


def test_labels_truncated_to_max_len():
    long_label = "x" * 300
    summary = build_intelligence_summary(
        tool_names=[], wasted_calls=0,
        rules_applied=[{"id": "subj-0123456789abcdef", "label": long_label}])
    assert MAX_LABEL_LEN == 160
    assert len(summary["rules_applied"][0]["label"]) == 160


def test_missing_label_falls_back_to_id():
    # Decision 3: when lookup fails or a row is missing, label == id.
    summary = build_intelligence_summary(
        tool_names=[], wasted_calls=0,
        rules_applied=[{"id": "subj-0123456789abcdef"}])
    assert summary["rules_applied"][0]["label"] == "subj-0123456789abcdef"


# --------------------------------------------------------------------------- #
# (g) empty tool list → totals 0, pct 0, no ZeroDivisionError
# --------------------------------------------------------------------------- #
def test_empty_input_is_zero_safe():
    summary = build_intelligence_summary(
        tool_names=[], wasted_calls=0, rules_applied=[])
    assert summary["total_tool_calls"] == 0
    assert summary["neptune_queries"] == 0
    assert summary["system_queries"] == 0
    assert summary["graph_coverage_pct"] == 0
    assert summary["wasted_calls"] == 0
    assert summary["rules_applied"] == []
    assert summary["new_proposed_mappings"] == 0
    # None inputs tolerated too (defensive callers).
    summary = build_intelligence_summary(
        tool_names=None, wasted_calls=None, rules_applied=None)  # type: ignore[arg-type]
    assert summary["total_tool_calls"] == 0
    assert summary["graph_coverage_pct"] == 0


# --------------------------------------------------------------------------- #
# format_rule_label — defensive, one-sided rows, truncation
# --------------------------------------------------------------------------- #
def test_label_for_two_sided_join_path():
    label = format_rule_label({
        "proposed_type": "join_path",
        "from_system": "erp", "from_field": "customer_id",
        "to_system": "mes", "to_field": "cust_ref",
    })
    assert label == "join path: erp.customer_id -> mes.cust_ref"


def test_label_for_value_mapping_with_values_and_concept():
    label = format_rule_label({
        "proposed_type": "value_mapping",
        "from_system": "erp", "from_field": "supplier",
        "from_value": "ANYCOMPANY-01",
        "to_system": "cmms", "to_field": "vendor", "to_value": "A01",
        "concept": "supplier",
    })
    assert label == ("value mapping: erp.supplier 'ANYCOMPANY-01' -> "
                     "cmms.vendor 'A01' (concept: supplier)")


def test_label_for_one_sided_field_annotation():
    label = format_rule_label({
        "proposed_type": "field_annotation",
        "from_system": "erp", "from_field": "wo_num",
    })
    assert label == "field annotation: erp.wo_num"


def test_label_defensive_on_junk_rows():
    assert format_rule_label({}) == "rule"
    assert format_rule_label(None) == ""  # type: ignore[arg-type]
    assert format_rule_label("not a dict") == ""  # type: ignore[arg-type]
    # Non-string field values stringified, never raising.
    assert "42" in format_rule_label({"proposed_type": 42})


def test_label_truncated_to_max_len():
    label = format_rule_label({
        "proposed_type": "join_path",
        "from_system": "s" * 200, "from_field": "f" * 200,
    })
    assert len(label) <= MAX_LABEL_LEN


# --------------------------------------------------------------------------- #
# (i) determinism kwarg — additive keys, mechanical wasted override
# --------------------------------------------------------------------------- #
def test_determinism_adds_keys_and_overrides_wasted_calls():
    det = {"per_call": [{}, {}, {}], "wasted_steps": [{"step": 2}],
           "deterministic_calls": 2, "deterministic_pct": 66}
    summary = build_intelligence_summary(
        tool_names=["explore_graph", "erp___query", "erp___query"],
        wasted_calls=9,  # envelope-scan count — OVERRIDDEN by the mechanical one
        rules_applied=[],
        determinism=det,
    )
    assert summary["grounded_steps"] == 2
    assert summary["deterministic_pct"] == 66
    assert summary["wasted_calls"] == 1  # len(wasted_steps), not the legacy 9
    # Additive: every legacy key still present.
    assert {"total_tool_calls", "neptune_queries", "system_queries",
            "graph_coverage_pct", "rules_applied",
            "new_proposed_mappings"} <= set(summary)


def test_determinism_absent_is_byte_identical_to_legacy():
    kwargs = dict(tool_names=["explore_graph", "erp___query"], wasted_calls=1,
                  rules_applied=[{"id": "subj-0123456789abcdef"}],
                  new_proposed_mappings=2)
    legacy = build_intelligence_summary(**kwargs)
    explicit_none = build_intelligence_summary(**kwargs, determinism=None)
    assert legacy == explicit_none
    assert "grounded_steps" not in legacy
    assert "deterministic_pct" not in legacy
    assert legacy["wasted_calls"] == 1  # legacy count untouched


def test_determinism_zero_safe_on_missing_fields():
    summary = build_intelligence_summary(
        tool_names=[], wasted_calls=0, rules_applied=[], determinism={})
    assert summary["grounded_steps"] == 0
    assert summary["deterministic_pct"] == 0
    assert summary["wasted_calls"] == 0


# --------------------------------------------------------------------------- #
# (h) import purity — stdlib-only (ADR-0020/0021)
# --------------------------------------------------------------------------- #
def test_module_is_import_pure():
    # Stdlib-only (ADR-0020/0021): query_intelligence must not import boto3
    # or pg8000 — mirrors the import-purity assertion in
    # test_usage_tracking.py.
    src = open(qi.__file__).read()
    assert "import boto3" not in src
    assert "import pg8000" not in src and "import pgdriver" not in src


def test_module_imports_with_boto3_absent():
    """Fresh interpreter with boto3 BOMBED must still import the module
    (subprocess fresh-interpreter convention — see test_demo_db_routing.py)."""
    code = (
        "import sys\n"
        "sys.modules['boto3'] = None\n"  # import boto3 → ImportError
        "import agents.query_intelligence as qi\n"
        "assert qi.is_graph_query('explore_graph') is True\n"
        "print('IMPORT-PURE-OK')\n"
    )
    adt_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(adt_root))
    r = subprocess.run([sys.executable, "-c", code], cwd=str(adt_root),  # nosec B603 - fixed argv, no shell
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "IMPORT-PURE-OK" in r.stdout


# --------------------------------------------------------------------------- #
# planned_with — ADDITIVE optional key (Task A1 / D3): absent unless provided
# --------------------------------------------------------------------------- #
def test_planned_with_absent_when_not_provided():
    summary = build_intelligence_summary(
        tool_names=["explore_graph"], wasted_calls=0, rules_applied=[])
    assert "planned_with" not in summary


def test_planned_with_absent_when_none_or_empty():
    for value in (None, {}):
        summary = build_intelligence_summary(
            tool_names=[], wasted_calls=0, rules_applied=[],
            planned_with=value)
        assert "planned_with" not in summary


def test_planned_with_present_and_capped():
    summary = build_intelligence_summary(
        tool_names=["explore_graph"], wasted_calls=0, rules_applied=[],
        planned_with={"systems": 3, "shared_concepts": ["c" * 200] * 15,
                      "join_paths": 4, "value_mappings": 5,
                      "identity_rules": 6},
    )
    planned = summary["planned_with"]
    assert planned["systems"] == 3
    assert planned["join_paths"] == 4
    assert planned["value_mappings"] == 5
    assert planned["identity_rules"] == 6
    assert len(planned["shared_concepts"]) == qi.MAX_PLANNED_CONCEPTS == 10
    assert all(len(c) == qi.MAX_PLANNED_CONCEPT_LEN == 80
               for c in planned["shared_concepts"])
    # The rest of the payload keeps the pinned Decision-2 key set.
    assert set(summary) - {"planned_with"} == {
        "total_tool_calls", "neptune_queries", "system_queries",
        "graph_coverage_pct", "wasted_calls", "rules_applied",
        "new_proposed_mappings",
    }


def test_planned_with_coerces_hostile_fields():
    summary = build_intelligence_summary(
        tool_names=[], wasted_calls=0, rules_applied=[],
        planned_with={"systems": -2, "shared_concepts": [None, "", 7, "ok"],
                      "join_paths": "many", "value_mappings": True},
    )
    planned = summary["planned_with"]
    # Negative / non-numeric / bool counts coerce to 0; missing key too.
    assert planned["systems"] == 0
    assert planned["join_paths"] == 0
    assert planned["value_mappings"] == 0
    assert planned["identity_rules"] == 0
    # Only non-empty strings survive the concept filter.
    assert planned["shared_concepts"] == ["ok"]


# --------------------------------------------------------------------------- #
# planned_with.authority_rules — T1.6 additive, HONEST-NULL (ADR-0053/0045):
# present only when the runtime measured it; unknown stays ABSENT, never 0.
# --------------------------------------------------------------------------- #
def _planned(planned_with: dict) -> dict:
    return build_intelligence_summary(
        tool_names=[], wasted_calls=0, rules_applied=[],
        planned_with=planned_with)["planned_with"]


def test_planned_with_authority_rules_present_when_measured():
    assert _planned({"systems": 1, "authority_rules": 2})["authority_rules"] == 2
    # 0 is an honest measurement (seed consulted authority, none loaded).
    assert _planned({"systems": 1, "authority_rules": 0})["authority_rules"] == 0
    # Negative coerces to 0 like every other count.
    assert _planned({"systems": 1, "authority_rules": -3})["authority_rules"] == 0


def test_planned_with_authority_rules_absent_when_unknown():
    # Missing input (legacy seeds / failed authority read) ⇒ key ABSENT —
    # never a fabricated zero; the pre-T1.6 five-key shape is unchanged.
    planned = _planned({"systems": 1, "value_mappings": 12})
    assert "authority_rules" not in planned
    assert set(planned) == {"systems", "shared_concepts", "join_paths",
                            "value_mappings", "identity_rules"}
    # Hostile non-numeric / bool / None values are unknown too ⇒ ABSENT.
    for hostile in (None, "many", True, [1, 2], {}):
        assert "authority_rules" not in _planned(
            {"systems": 1, "authority_rules": hostile})
