# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deterministic-score computation — mechanical, evidence-based, never LLM-judged.

Semantics (replaces the old graph_calls/total style formulas that capped clean
runs below 100%):

  deterministic call = a knowledge-graph read (Neptune traversal / schema
    lookup), OR a call whose input is grounded in values returned by an
    EARLIER call (an equipment tag from the graph, a telemetry UUID,
    timestamps from a work-order row, ...).
  wasted call = HARD evidence only: the result errored/came back empty AND the
    same tool was re-invoked afterward (a retry), or the call was an exact
    duplicate of a previous invocation.

All calls grounded + zero waste => 100%. An LLM may EXPLAIN mechanically
detected waste, but it never decides what was wasted and never produces the
score.

Deviations from the reference implementation this module was ported from
(the pre-rename ``agents/determinism.py``) — both deliberate:

* KG membership is NOT a hardcoded tool-name tuple (the ref's
  ``query_neptune_graph`` does not exist here). It delegates to
  ``query_intelligence.is_graph_query`` (bare ``explore_graph`` AND the
  gateway-prefixed ``…___explore_graph`` spelling) plus the schema-lookup
  tool in both spellings — see ``_is_kg_call``.
* Failure detection delegates to ``query_intelligence.is_failed_result``
  (the shared error/empty ENVELOPE markers). This repo deliberately rejected
  the ref's bare-word ``"error"`` marker: "error" appears legitimately in
  manufacturing results (defect reasons, error codes, error_rate columns)
  and would mislabel successful queries as failed.

Pure, stdlib-only (import purity per ADR-0020/0021 — no boto3, no I/O).
"""
from __future__ import annotations

import re

from . import query_intelligence


def _is_kg_call(name: str) -> bool:
    """True for a knowledge-graph read: the Neptune traversal tool (via the
    shared ``is_graph_query`` predicate — bare and gateway-prefixed) or the
    schema-lookup tool (same two spellings)."""
    return (query_intelligence.is_graph_query(name)
            or name == "get_system_schema"
            or name.endswith("___get_system_schema"))


def value_tokens(text: str) -> set[str]:
    """Distinctive value tokens: alnum runs >=6 chars normalized, plus a
    digits-only form (>=8 digits) feeding the ``_tokens_ground`` substring
    pass so timestamps match across formats."""
    toks: set[str] = set()
    for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-:.]{5,}", text or ""):
        norm = re.sub(r"[^a-z0-9]", "", t.lower())
        if len(norm) >= 6:
            toks.add(norm)
        digits = re.sub(r"[^0-9]", "", t)
        if len(digits) >= 8:
            toks.add(digits)
    return toks


def _tokens_ground(input_tokens: set[str], evidence: set[str]) -> bool:
    """True when the input shares evidence with earlier results.

    Exact token intersection first; then a digits-substring pass for long
    (>=8 digit) numeric tokens so timestamp FORMAT drift still grounds — the
    ISO form tokenizes as one 14-digit run ('20260719171950') while the
    spaced form splits into date + time runs ('20260719', '171950'); pure
    set intersection would miss that the date is a fragment of the full
    timestamp already in evidence.
    """
    if input_tokens & evidence:
        return True
    ev_digits = [t for t in evidence if t.isdigit() and len(t) >= 8]
    return any(t in e or e in t
               for t in input_tokens if t.isdigit() and len(t) >= 8
               for e in ev_digits)


def _mark_retries(per_call: list[dict]) -> None:
    """Annotate each call with ``retried_later`` — True when the SAME tool
    name is invoked again at any later step (in place)."""
    names_after: dict[str, int] = {}
    for c in reversed(per_call):
        c["retried_later"] = names_after.get(c["name"], 0) > 0
        names_after[c["name"]] = names_after.get(c["name"], 0) + 1


def score_tool_calls(tool_calls: list, tool_inputs: dict, tool_results: dict) -> dict:
    """Compute the deterministic score for a session's tool-call trace.

    Args:
        tool_calls: ``[{"toolUseId": ..., "name": ...}, ...]`` in call order
        tool_inputs: toolUseId -> input string
        tool_results: toolUseId -> result snippet string
    Returns:
        ``{"per_call": [...], "wasted_steps": [...],
           "deterministic_calls": int, "deterministic_pct": int}``
    """
    evidence: set[str] = set()
    per_call: list[dict] = []
    seen_inputs: set[tuple] = set()
    for i, tc in enumerate(tool_calls or []):
        tid = str(tc.get("toolUseId") or "")
        name = str(tc.get("name") or "")
        inp = tool_inputs.get(tid, "") or ""
        res = tool_results.get(tid, "") or ""
        failed = query_intelligence.is_failed_result(res)
        duplicate = (name, inp) in seen_inputs
        seen_inputs.add((name, inp))
        grounded = _is_kg_call(name) or _tokens_ground(value_tokens(inp), evidence)
        per_call.append({"step": i + 1, "name": name, "grounded": grounded,
                         "failed": failed, "duplicate": duplicate})
        evidence |= value_tokens(res)

    # Wasted: failed AND retried later with the same tool, or exact duplicate.
    _mark_retries(per_call)
    wasted_steps = [c for c in per_call
                    if (c["failed"] and c["retried_later"]) or c["duplicate"]]
    wasted_ids = {id(c) for c in wasted_steps}
    deterministic_calls = sum(1 for c in per_call
                              if c["grounded"] and id(c) not in wasted_ids)
    total = max(len(per_call), 1)
    return {
        "per_call": per_call,
        "wasted_steps": wasted_steps,
        "deterministic_calls": deterministic_calls,
        "deterministic_pct": int((deterministic_calls / total) * 100),
    }
