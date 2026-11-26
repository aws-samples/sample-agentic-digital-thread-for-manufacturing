# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline guards for the Query Intelligence panel (rules-applied metric).

The panel is browser-rendered (no server template), so we assert source-level
invariants of the redesign — a future edit that resurrects the judged
"% deterministic" metric (or drops the escaping/guards) fails fast in the
offline lane:

  • The judged metric is GONE from the panel surface: no ``% deterministic``
    text, no ``deterministic_pct`` read in arch-store.ts, no
    ``graph_guided_steps`` payload reads, no ``pctColor`` red/amber/green
    judgment. (``deterministic_pct`` in the chat.ts payload TYPE is the
    COMPUTED metric — allowed, but only with its never-LLM-judged
    doc contract; see ``test_percent_deterministic_removed``.)
  • The replacement is present: ``Learned rules applied: N`` with the
    RULES_TIP tooltip and an expandable native ``<details>`` rule list.
  • Rule labels are XSS-safe: the panel assigns ``textContent`` (text nodes —
    inherently escaped, zero-innerHTML policy).
  • The neutral coverage line is gated on ``graph_coverage_pct`` presence
    (deploy-skew guard) and its tooltip carries the rewritten neutral wording
    (no 'schema lookups' language, no raise-it-over-time framing).
  • Field reads are defensive (``|| 0`` / ``|| []``) so an OLD runtime payload
    renders without NaN/undefined.

The panel lives in ``frontend/src/lib/arch-store.ts``
(``onIntelligenceSummary``) with the payload type in
``frontend/src/api/types/chat.ts``. (The legacy static UI monolith was
removed 2026-07-31.)

Fully offline: reads the sources from disk. No AWS / DB / network.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

_FE_ARCH_STORE = _ROOT / "frontend" / "src" / "lib" / "arch-store.ts"
_FE_TYPES = _ROOT / "frontend" / "src" / "api" / "types" / "chat.ts"
_FE_METRIC_COPY = _ROOT / "frontend" / "src" / "lib" / "metric-copy.ts"


@pytest.fixture(scope="module")
def panel_src() -> str:
    """The Query Intelligence surface: the SPA modules."""
    return "\n".join(
        p.read_text(encoding="utf-8") for p in (_FE_ARCH_STORE, _FE_TYPES)
    )


@pytest.fixture(scope="module")
def panel_region(panel_src: str) -> str:
    """Just the intelligence-summary block (anchored on landmarks, not lines).

    The ``onIntelligenceSummary`` function through the final
    ``responseEl.appendChild(box)``.
    """
    src = _FE_ARCH_STORE.read_text(encoding="utf-8")
    start = src.index("function onIntelligenceSummary")
    end = src.index("responseEl.appendChild(box);", start)
    return src[start : end + len("responseEl.appendChild(box);")]


# --- The replacement metric ---------------------------------------------------

def test_learned_rules_applied_present(panel_region: str) -> None:
    assert "Learned rules applied" in panel_region
    # T1.6 fired-vs-loaded: the tooltip is the ONE shared constant
    # (lib/metric-copy.ts, reused by the chat analysis panel and the
    # T3.5 scorecard) — the panel references it, and the module carries
    # the "used in this answer" vs "loaded at planning" wording.
    assert "RULES_APPLIED_TIP" in panel_region
    copy_src = _FE_METRIC_COPY.read_text(encoding="utf-8")
    assert "Used in this answer" in copy_src
    assert "loaded at planning" in copy_src
    assert "Grows as you accept Self-Learning Discoveries" in copy_src


def test_rules_list_is_expandable_details(panel_region: str) -> None:
    # Native <details> disclosure, only built when there are rules.
    assert "rules.length > 0" in panel_region
    assert "document.createElement('details')" in panel_region
    assert "show rules" in panel_region


# --- The judged metric is dead -------------------------------------------------

def test_percent_deterministic_removed(panel_src: str) -> None:
    """The LLM-JUDGED percent metric is dead. NARROWED: a later
    change deliberately re-added `deterministic_pct` to the payload type
    (frontend/src/api/types/chat.ts) as a COMPUTED metric — mechanical count,
    never LLM-judged (see the doc comment there). The pin's intent was banning
    the LLM-era judged "% deterministic" panel metric, so the key ban now
    targets the PANEL surface (arch-store.ts) only, and chat.ts must keep
    documenting the re-added key as computed."""
    assert "% deterministic" not in panel_src
    assert "graph_guided_steps" not in panel_src
    assert "Graph-guided steps" not in panel_src
    # Panel surface: the Query Intelligence panel must not read the key.
    arch_store_src = _FE_ARCH_STORE.read_text(encoding="utf-8")
    assert "deterministic_pct" not in arch_store_src
    # Payload type: the re-added key must carry the computed-not-judged
    # doc contract (a bare re-add without it resurrects the ambiguity).
    types_src = _FE_TYPES.read_text(encoding="utf-8")
    if "deterministic_pct" in types_src:
        assert "never LLM-judged" in types_src


def test_no_color_judgment(panel_src: str) -> None:
    # No red/amber/green pass-fail coloring anywhere in the surface.
    assert "pctColor" not in panel_src


def test_trace_entry_drops_deterministic(panel_src: str) -> None:
    # The investigation-trace summary line uses the new metric.
    assert "learned rules applied" in panel_src


# --- XSS safety of rule labels --------------------------------------------------

def test_rule_labels_escaped(panel_region: str) -> None:
    # zero-innerHTML policy: labels are text nodes via textContent; the
    # module has no HTML-string sink at all (the word innerHTML appears
    # only in the "zero-innerHTML port" comment — assert the sinks).
    assert "li.textContent = String((r && (r.label || r.id)) || '')" in panel_region
    assert ".innerHTML" not in panel_region
    assert "insertAdjacentHTML" not in panel_region
    assert "dangerouslySetInnerHTML" not in panel_region


# --- Neutral coverage line (deploy-skew guarded) --------------------------------

def test_coverage_tip_is_neutral(panel_src: str) -> None:
    # The rewritten tooltip: no 'schema lookups' language, no
    # raise-it-over-time framing, explicitly not a quality score.
    assert "schema lookups" not in panel_src
    assert "raises this over time" not in panel_src
    assert "not a quality score" in panel_src


def test_coverage_line_gated_on_field_presence(panel_region: str) -> None:
    # Deploy-skew guard: an old runtime payload (no graph_coverage_pct) must
    # not render the coverage line.
    assert "s.graph_coverage_pct !== undefined" in panel_region
    assert "knowledge-graph queries" in panel_region


# --- Wasted-calls line ----------------------------------------------------------

def test_wasted_calls_line_guarded(panel_region: str) -> None:
    assert "Exploration steps with no matching data" in panel_region
    assert "(s.wasted_calls || 0) > 0" in panel_region


# --- Defensive reads (old payloads render clean) --------------------------------

def test_guarded_reads_present(panel_region: str) -> None:
    assert "s.rules_applied || []" in panel_region
    assert "s.total_tool_calls || 0" in panel_region
    assert "s.neptune_queries || 0" in panel_region
    assert "s.system_queries || 0" in panel_region
    assert "s.wasted_calls || 0" in panel_region


# --- Kept surfaces ---------------------------------------------------------------

def test_tool_calls_counts_line_kept(panel_region: str) -> None:
    assert "Tool calls: " in panel_region
    assert "Neptune" in panel_region and "system queries" in panel_region
