# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline guards for the map-step full-coverage guarantee (C3/C4 — M1, M2, M4).

The hard contract: after ``_run_map_job`` finishes, EVERY discovered input column
(keyed ``(real_table, full_column_name)``) has at least one mapping row. Columns
the LLM never returned after a bounded re-queue (≤2 rounds) are synthesized as
``reconciled: True`` needs_review rows BEFORE ``_select_field_concepts`` — no
input column ever vanishes silently. Batches are built by cumulative COLUMN
budget (``_MAP_BATCH_COLUMN_BUDGET``), every map call carries an explicit
``model=`` (the map step's own model, never ``bedrock_call``'s default
first-try path), and the
malformed-JSON recovery re-prompt carries the FULL prompt (field list included).

All fixtures are SYNTHETIC/generic (a 150-column ``wide_registry`` table with
multi-dot flattened names) — no dataset/demo literals. LLM behavior is exercised
via the mocked ``bedrock_call`` seam only (the ``test_onboard_virtual_table_remap``
idiom): no AWS, no graph, no DB, no LLM.
"""
from __future__ import annotations

import inspect
import json
import math
import re

import agents.onboard as onboard

# ─────────────────────────────────────────────────────────────────────────────
# Generic 150-column fixture — includes multi-dot flattened column names
# ─────────────────────────────────────────────────────────────────────────────
_TABLE = "wide_registry"
_COLUMN_NAMES = (
    [f"metric_{i:03d}_code" for i in range(118)]
    + ["line_a.cell_3.temperature_c", "group_a.sub.code"]
    + [f"line_b.cell_{i}.pressure_kpa" for i in range(30)]
)
assert len(_COLUMN_NAMES) == 150


def _wide_tables():
    return [{
        "name": _TABLE, "relation_kind": "base_table", "schema_revision": "b" * 32,
        "columns": [{"name": n, "data_type": "varchar"} for n in _COLUMN_NAMES],
        "row_count": 150, "sample_values": {},
    }]


def _prompt_columns(prompt: str) -> list[tuple[str, str]]:
    """Parse the (table, column) pairs a map prompt carries (the
    ``_format_fields_summary`` shape: '  <table>:' then '    - <column> …')."""
    pairs, current = [], None
    for line in prompt.splitlines():
        tbl = re.match(r"^\s{2}(\S+):\s*$", line)
        if tbl:
            current = tbl.group(1)
            continue
        col = re.match(r"^\s{4}- (\S+)", line)
        if col and current:
            pairs.append((current, col.group(1)))
    return pairs


class _SubsetEchoMock:
    """Echoes mappings for only the FIRST TWO columns of each prompt (a truncated
    LLM response) and records per-call column counts + the model kwarg."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, prompt, timeout=90, model=None):
        pairs = _prompt_columns(prompt)
        self.calls.append({"n_columns": len(pairs), "model": model,
                           "is_recovery": prompt.startswith("Return ONLY a valid JSON array")})
        return json.dumps([
            {"field": f"{t}.{c}", "table": t,
             "mappings": [{"concept": "equipment-id", "role": "identifier",
                           "confidence": 0.9}]}
            for t, c in pairs[:2]
        ])


def _install_map_seams(monkeypatch, fake_bedrock, *, no_escalation=True):
    monkeypatch.setattr(onboard, "bedrock_call", fake_bedrock)
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: ["equipment-id"])
    monkeypatch.setattr(onboard.catalog, "get",
                        lambda sid: {"connection_type": "PostgreSQL"})
    if no_escalation:
        monkeypatch.setattr(onboard, "_maybe_escalate_batch",
                            lambda bm, *_a, **_k: (bm, False, False))


def _run_job(monkeypatch, fake_bedrock, tables=None, job_id="reconjob"):
    _install_map_seams(monkeypatch, fake_bedrock)
    onboard._map_jobs.pop(job_id, None)
    onboard._run_map_job(job_id, "example-sys", tables or _wide_tables())
    job = onboard._map_jobs.pop(job_id)
    assert job["status"] == "done", job
    return job["result"]


# ─────────────────────────────────────────────────────────────────────────────
# The full-coverage guarantee, end-to-end (truncated-response simulation)
# ─────────────────────────────────────────────────────────────────────────────
def test_truncated_responses_reconcile_to_full_coverage(monkeypatch):
    mock = _SubsetEchoMock()
    result = _run_job(monkeypatch, mock)
    mappings = result["mappings"]
    # EVERY input column → exactly one mapping row (multi-dot names included).
    assert sorted(m["field"] for m in mappings) == sorted(
        f"{_TABLE}.{c}" for c in _COLUMN_NAMES)
    # The echoed subset is confidently mapped; the remainder is synthesized.
    synthesized = [m for m in mappings if m.get("reconciled")]
    assert synthesized, "reconciliation must synthesize rows for the truncated remainder"
    for m in synthesized:
        assert m["concept"] == "needs_review"
        assert m["reasoning"] == "not returned by mapper"
    mapped = [m for m in mappings if m["concept"] == "equipment-id"]
    assert mapped and len(mapped) + len(synthesized) == 150


def test_no_prompt_exceeds_column_budget_and_rounds_are_bounded(monkeypatch):
    mock = _SubsetEchoMock()
    _run_job(monkeypatch, mock)
    # C4: no prompt (primary or reconciliation) carried more than the budget…
    # (primary chunks are ~50 columns via the virtual-table split; re-queue
    # chunks are capped at the budget by _missing_column_tables)
    assert all(c["n_columns"] <= onboard._MAP_BATCH_COLUMN_BUDGET for c in mock.calls)
    # …and the re-queue is bounded: primary batches + ≤2 rounds of at most
    # ceil(150 / budget) calls each (escalation seam is stubbed out here).
    primary = math.ceil(150 / 50)  # the virtual-table CHUNK of 50
    per_round = math.ceil(150 / onboard._MAP_BATCH_COLUMN_BUDGET)
    assert len(mock.calls) <= primary + onboard._MAX_MAP_RECONCILE_ROUNDS * per_round


def test_every_map_call_carries_explicit_model(monkeypatch):
    """C4/M1b: no map call may fall back to bedrock_call's default first-try path."""
    mock = _SubsetEchoMock()
    monkeypatch.setenv("MAP_MODEL", "vendor.example-map-model")
    _run_job(monkeypatch, mock)
    assert mock.calls, "no map calls recorded"
    assert all(c["model"] == "vendor.example-map-model" for c in mock.calls)


def test_empty_mapper_still_yields_all_needs_review_rows(monkeypatch):
    """A mapper that ALWAYS returns [] yields 150 synthesized needs_review rows —
    never an error, never a silent drop — and they survive provenance attach."""
    result = _run_job(monkeypatch, lambda prompt, timeout=90, model=None: "[]")
    mappings = result["mappings"]
    assert len(mappings) == 150
    for m in mappings:
        assert m["concept"] == "needs_review"
        assert m["reconciled"] is True
        assert m["table"] == _TABLE
        # _attach_map_provenance re-joined the synthesized row to its REAL
        # physical column (C3 holds by construction) instead of dropping it.
        assert m["data_type"] == "varchar"
        assert m["relation_kind"] == "base_table"
        assert m["schema_revision"] == "b" * 32


def test_malformed_json_recovery_reprompt_carries_field_list(monkeypatch):
    """M2: the strict-JSON re-prompt sends the FULL prompt — the old
    prompt[-2000:] slice was all rules text with zero field names."""
    columns = ["metric_001_code", "group_a.sub.code", "line_a.cell_3.temperature_c"]
    tables = [{"name": "small_registry", "relation_kind": "base_table",
               "schema_revision": "c" * 32, "row_count": 3,
               "columns": [{"name": n, "data_type": "varchar"} for n in columns]}]
    recovery_prompts: list[str] = []

    def _fake(prompt, timeout=90, model=None):
        if prompt.startswith("Return ONLY a valid JSON array"):
            recovery_prompts.append(prompt)
            return json.dumps([
                {"field": f"small_registry.{c}", "table": "small_registry",
                 "mappings": [{"concept": "equipment-id", "role": "identifier",
                               "confidence": 0.9}]}
                for c in columns
            ])
        return "no json in this response"

    result = _run_job(monkeypatch, _fake, tables=tables, job_id="m2job")
    assert recovery_prompts, "the malformed-JSON re-prompt never fired"
    for column in columns:  # the actual field list is present in the re-prompt
        assert column in recovery_prompts[0]
    assert sorted(m["field"] for m in result["mappings"]) == sorted(
        f"small_registry.{c}" for c in columns)


# ─────────────────────────────────────────────────────────────────────────────
# C4 batcher — whole tables, cumulative column budget
# ─────────────────────────────────────────────────────────────────────────────
def test_build_map_batches_packs_by_cumulative_column_budget():
    def t(name, n):
        return {"name": name, "columns": [{"name": f"{name}_c{i}"} for i in range(n)]}
    batches = onboard._build_map_batches([t("a", 30), t("b", 25), t("c", 20), t("d", 5)])
    # a+b = 55 ≤ 60; adding c (20) would exceed → new batch; c+d = 25 ≤ 60.
    assert [[x["name"] for x in b] for b in batches] == [["a", "b"], ["c", "d"]]


def test_build_map_batches_never_splits_a_table():
    wide = {"name": "w", "columns": [{"name": f"c{i}"} for i in range(80)]}
    small = {"name": "s", "columns": [{"name": "x"}]}
    batches = onboard._build_map_batches([wide, small])
    # An over-budget table still forms ONE whole-table batch (the >100-column
    # virtual split bounds oversize tables before batching).
    assert [[x["name"] for x in b] for b in batches] == [["w"], ["s"]]
    assert onboard._build_map_batches([]) == []


# ─────────────────────────────────────────────────────────────────────────────
# C5 — vocab_fit counters
# ─────────────────────────────────────────────────────────────────────────────
def _fit_fixture():
    tables = [{"name": "t", "columns": [{"name": f"c{i}"} for i in range(4)]}]
    mappings = [
        {"field": "t.c0", "table": "t", "concept": "equipment-id", "confidence": 0.9},
        {"field": "t.c1", "table": "t", "concept": "site", "confidence": 0.8},
        {"field": "t.c2", "table": "t", "concept": "needs_review", "confidence": 0},
        {"field": "t.c3", "table": "t", "concept": "needs_review", "confidence": 0,
         "reconciled": True},
    ]
    return tables, mappings


def test_vocab_fit_counters_and_coverage_arithmetic(monkeypatch):
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: ["equipment-id", "site"])
    monkeypatch.delenv("RESOLVER_AFFINITY", raising=False)
    tables, mappings = _fit_fixture()
    result = onboard._map_job_result("sys", mappings, tables=tables)
    fit = result["vocab_fit"]
    assert fit == {
        "total_columns": 4, "rows_emitted": 4, "mapped_confident": 2,
        "needs_review": 2, "reconciled": 1, "distinct_concepts": 2,
        "vocabulary_size": 2, "coverage_pct": 50,
    }


def test_vocab_fit_absent_when_tables_not_passed(monkeypatch):
    monkeypatch.delenv("RESOLVER_AFFINITY", raising=False)
    _tables, mappings = _fit_fixture()
    result = onboard._map_job_result("sys", mappings)
    assert "vocab_fit" not in result  # old callers ⇒ frontend hides the banner


# ─────────────────────────────────────────────────────────────────────────────
# Source gate — the new reconciliation/batching helpers stay generic (repo pattern)
# ─────────────────────────────────────────────────────────────────────────────
def test_reconciliation_helper_sources_have_no_demo_literals():
    src = "".join(inspect.getsource(fn) for fn in (
        onboard._map_model, onboard._build_map_batches,
        onboard._map_input_column_keys, onboard._emitted_column_keys,
        onboard._missing_column_tables, onboard._requeue_missing_columns,
        onboard._reconcile_map_coverage, onboard._vocab_fit_block,
        onboard._mappable_column_count, onboard._is_low_coverage_batch,
    ))
    for token in ("AnyCompany", "Milwaukee", "Detroit", "Bottling", "highbyte",
                  "Line1Filler", "WasherPump"):
        assert token.lower() not in src.lower()
