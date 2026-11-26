# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Post-run usage hook (Wave 3 Task 3.2 — item C).

``runtime_app._record_rule_usage_hook`` is the extracted, offline-testable
post-run hook (mirrors ``_check_for_discoveries``): it extracts applied-node
ids from the turn's tool results and hands them to
``self_learning.record_rule_usage``. Verified here with the record seam
monkeypatched:

- tool results containing rule ids → record called with exactly those ids;
- briefing prompts → record NOT called (same gate as the sibling hooks);
- no ids in results / empty results → record NOT called;
- the finally-block call site wraps the hook in a swallow guard (source
  tripwire, mirroring the source-inspection test convention).

Offline: no AWS, no Neptune, no network.
"""
from __future__ import annotations

import inspect

import agents.runtime_app as runtime_app
import agents.self_learning as self_learning


def _spy(calls):
    def record(ids):
        calls.append(set(ids))
        return len(ids)
    return record


def test_hook_records_ids_found_in_tool_results(monkeypatch):
    calls = []
    monkeypatch.setattr(self_learning, "record_rule_usage", _spy(calls))
    tool_results = {
        "t1": "joined via subj-a1b2c3d4e5f60718 (accepted rule)",
        "t2": "plain result, no ids",
        "t3": "legacy vm-learned-deadbeef applied",
    }
    bumped = runtime_app._record_rule_usage_hook(tool_results, "compare ERP and MES yields")
    assert calls == [{"subj-a1b2c3d4e5f60718", "vm-learned-deadbeef"}]
    assert bumped == 2


def test_briefing_prompt_never_records(monkeypatch):
    calls = []
    monkeypatch.setattr(self_learning, "record_rule_usage", _spy(calls))
    tool_results = {"t1": "subj-a1b2c3d4e5f60718"}
    assert runtime_app._record_rule_usage_hook(
        tool_results, "Generate the morning operations briefing") == 0
    assert calls == []


def test_no_ids_short_circuits_record(monkeypatch):
    calls = []
    monkeypatch.setattr(self_learning, "record_rule_usage", _spy(calls))
    assert runtime_app._record_rule_usage_hook({"t1": "no rule ids here"}, "q") == 0
    assert runtime_app._record_rule_usage_hook({}, "q") == 0
    assert runtime_app._record_rule_usage_hook(None, "q") == 0
    assert calls == []


def test_finally_call_site_is_swallow_guarded():
    # The stream must never die on a usage bump: the _run_orchestrator finally
    # block wraps the hook call in try/except (source tripwire — mirrors the
    # source-inspection pattern of test_runtime_insight_routing.py).
    src = inspect.getsource(runtime_app._run_orchestrator_base)
    call = "_record_rule_usage_hook(_tool_results, prompt"
    assert call in src
    call_idx = src.index(call)
    # A try: opens within the few lines before the call…
    assert "try:" in src[max(0, call_idx - 200):call_idx], \
        "hook call must sit inside a try block"
    # …and an except Exception closes right after it (swallow guard). Window
    # sized for the full kwarg list (route_ctx + session_id — the ADR-0052
    # fallback and the try-it ledger exclusion ride the same call).
    assert "except Exception" in src[call_idx:call_idx + 300], \
        "hook call must be followed by a swallow guard"
    # The call forwards the summary block's single extraction — the turn must
    # never re-scan results or re-run the GSI query in the finally block.
    tail = src[call_idx:call_idx + 300]
    assert "precomputed_ids=s.rule_ids" in tail, \
        "finally-block call must pass the precomputed rule ids"
    assert "prefetched_rows=s.matched_rows" in tail, \
        "finally-block call must pass the prefetched accepted rows"


# --------------------------------------------------------------------------- #
# Single-extraction wiring (Wave 2): the hook accepts the summary block's
# precomputed ids + prefetched rows so the turn pays for ONE result scan and
# ONE GSI query (shared with the human rule labels).
# --------------------------------------------------------------------------- #

def test_precomputed_ids_skip_rescan_and_forward_prefetched_rows(monkeypatch):
    import agents.usage_tracking as usage_tracking

    def _boom(*_a, **_k):
        raise AssertionError("extract_rule_node_ids must NOT run on the precomputed path")

    monkeypatch.setattr(usage_tracking, "extract_rule_node_ids", _boom)

    recorded = []

    def record(ids, **kwargs):
        recorded.append((set(ids), kwargs))
        return 1

    monkeypatch.setattr(self_learning, "record_rule_usage", record)
    rows = [{"neptune_node_id": "subj-a1b2c3d4e5f60718", "mapping_id": "m1"}]
    bumped = runtime_app._record_rule_usage_hook(
        {"t1": "subj-a1b2c3d4e5f60718"}, "compare ERP and MES yields",
        precomputed_ids={"subj-a1b2c3d4e5f60718"}, prefetched_rows=rows)
    assert bumped == 1
    # Exactly one record call, with the precomputed ids AND the rows forwarded.
    assert recorded == [({"subj-a1b2c3d4e5f60718"}, {"prefetched_rows": rows})]


def test_empty_precomputed_ids_short_circuit_without_rescan(monkeypatch):
    # An EMPTY precomputed set takes precedence over the results (no silent
    # fallback rescan) — the summary block is the single extraction point.
    import agents.usage_tracking as usage_tracking

    def _boom(*_a, **_k):
        raise AssertionError("extract_rule_node_ids must NOT run on the precomputed path")

    monkeypatch.setattr(usage_tracking, "extract_rule_node_ids", _boom)
    calls = []
    monkeypatch.setattr(self_learning, "record_rule_usage", _spy(calls))
    assert runtime_app._record_rule_usage_hook(
        {"t1": "subj-a1b2c3d4e5f60718"}, "q",
        precomputed_ids=set(), prefetched_rows=[]) == 0
    assert calls == []


def test_briefing_gate_precedes_precomputed_path(monkeypatch):
    # The briefing gate is unchanged: it fires BEFORE any id/row handling.
    calls = []
    monkeypatch.setattr(self_learning, "record_rule_usage", _spy(calls))
    assert runtime_app._record_rule_usage_hook(
        {"t1": "subj-a1b2c3d4e5f60718"},
        "Generate the morning operations briefing",
        precomputed_ids={"subj-a1b2c3d4e5f60718"},
        prefetched_rows=[{"neptune_node_id": "subj-a1b2c3d4e5f60718"}]) == 0
    assert calls == []


def test_default_path_unchanged_scans_and_omits_prefetched(monkeypatch):
    # precomputed_ids/prefetched_rows default to None → the hook scans the
    # results itself and calls record_rule_usage WITHOUT prefetched_rows
    # (pinned by the positional-only spy signature).
    recorded = []

    def record(ids):  # rejects any kwargs — the default path must pass none
        recorded.append(set(ids))
        return len(ids)

    monkeypatch.setattr(self_learning, "record_rule_usage", record)
    bumped = runtime_app._record_rule_usage_hook(
        {"t1": "joined via subj-a1b2c3d4e5f60718"}, "compare yields")
    assert bumped == 1
    assert recorded == [{"subj-a1b2c3d4e5f60718"}]


# --------------------------------------------------------------------------- #
# Query-analysis honesty: accepted rule applications and correlation writes are
# distinct outcomes, and LLM-authored no-data rows must correspond to an actual
# failed/empty tool result.
# --------------------------------------------------------------------------- #

def test_successful_correlation_writebacks_are_counted_separately():
    calls = [
        {"name": "digital-thread___save_correlation", "toolUseId": "c1"},
        {"name": "save_correlation", "toolUseId": "c2"},
        {"name": "digital-thread___save_correlation", "toolUseId": "c3"},
        {"name": "erp___query", "toolUseId": "q1"},
    ]
    results = {
        "c1": '{"status":"logged","mapping_id":"m1","type":"insight"}',
        "c2": '{"status":"skipped","reason":"need two distinct systems"}',
        "c3": "not json",
        "q1": '{"status":"logged","mapping_id":"m2","type":"insight"}',
    }
    assert runtime_app._count_correlation_writebacks(calls, results) == 1


def test_analysis_wasted_calls_keep_only_real_failed_steps():
    calls = [
        {"name": "erp___query", "toolUseId": "ok"},
        {"name": "mes___query", "toolUseId": "empty"},
        {"name": "cmms___query", "toolUseId": "error"},
    ]
    results = {
        "ok": "3 matching rows",
        "empty": "No matching data",
        "error": "Error: column not found",
    }
    raw = [
        {"step": 1, "reason": "same-system mapping is redundant"},
        {"step": 2, "reason": "no matching MES row"},
        {"step": 3, "reason": "bad column"},
        {"step": 99, "reason": "invented step"},
    ]
    assert runtime_app._validated_wasted_calls(raw, calls, results) == raw[1:3]


# --------------------------------------------------------------------------- #
# Evidence classes (B3, ADR-0052 decision (b)): the hook stamps the caller's
# honest class on the ledger bump — default "tool-text", integrity seam
# "relationship-proof".
# --------------------------------------------------------------------------- #

def test_hook_forwards_evidence_class_to_ledger(monkeypatch):
    import agents.artifact_usage as artifact_usage

    bumps = []
    monkeypatch.setattr(self_learning, "record_rule_usage",
                        lambda _ids, **_k: 1)
    monkeypatch.setattr(
        artifact_usage, "extract_artifact_refs",
        lambda *_a, **_k: {("rule", "subj-a1b2c3d4e5f60718")})
    monkeypatch.setattr(
        artifact_usage, "bump_artifact_usage",
        lambda refs, evidence="": bumps.append(evidence) or len(refs))

    runtime_app._record_rule_usage_hook(
        {"t1": "subj-a1b2c3d4e5f60718"}, "compare ERP and MES yields",
        precomputed_ids={"subj-a1b2c3d4e5f60718"}, prefetched_rows=[],
        evidence_class="relationship-proof")
    runtime_app._record_rule_usage_hook(
        {"t1": "subj-a1b2c3d4e5f60718"}, "compare ERP and MES yields",
        precomputed_ids={"subj-a1b2c3d4e5f60718"}, prefetched_rows=[])

    assert bumps == ["relationship-proof", "tool-text"]
