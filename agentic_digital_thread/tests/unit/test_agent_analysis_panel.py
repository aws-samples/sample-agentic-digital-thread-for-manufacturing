# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline guards for the collapsible, denoised Agent Analysis panel.

The panel is browser-rendered (no server template), so there is no Python
behaviour to unit-test. Instead we assert the source-level invariants of the
redesign so a future edit that reintroduces the noise (or drops the collapse)
fails fast in the offline lane:

  • ONE collapsible ``<details class="agent-analysis-details">`` per answer,
    collapsed by default (no ``open`` attribute set), with a matching
    ``.agent-analysis-summary`` styled in ``dashboard.css``.
  • The noisy developer jargon is gone: no rendered ``"% deterministic"`` label,
    no ``"Wasted Calls"`` header, no always-on ``"Fix:"``/``"Impact:"`` fills,
    no inline enum ``system.table.field`` recommendation cards.
  • The user-friendly replacements are present: ``"Graph-guided share"``,
    ``"Exploration steps that found no matching data"``, and the demoted
    ``"schema hint"`` link into Self-Learning Discoveries.
  • Untrusted LLM analysis fields render as JSX TEXT children (escaped by
    construction) — no raw ``${a.summary}`` / ``${w.reason}`` interpolation and
    no HTML-string sink in the panel module.

The panel lives in ``frontend/src/chat/analysis-panel.tsx``; the done-handler
wiring / error paths live in ``frontend/src/chat/useAskStream.ts`` (helpers:
``md.ts`` escHtml re-export, ``thread-clicks.ts`` delegated Self-Learning
navigation). (The legacy static UI monolith was removed 2026-07-31.)

Fully offline: reads the sources from disk. No AWS / DB / network.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND = _ROOT / "frontend" / "src"

_CSS = _FRONTEND / "styles" / "dashboard.css"
# The frontend chat surface — the modules that hold the analysis code.
# zero-innerHTML: the panel is a React component (analysis-panel.tsx,
# JSX — no HTML strings), and the red error paragraph renders in
# AssistantTurn.tsx (the transport stores plain message text).
_FE_PANEL = _FRONTEND / "chat" / "analysis-panel.tsx"
_FE_STREAM = _FRONTEND / "chat" / "useAskStream.ts"
_FE_TURN = _FRONTEND / "chat" / "AssistantTurn.tsx"
_FE_MD = _FRONTEND / "chat" / "md.ts"
_FE_NAV = _FRONTEND / "chat" / "thread-clicks.ts"
# DRY consolidation: the escape implementation lives in the shared helper
# (md.ts re-exports it as the legacy escHtml name).
_FE_ESC = _FRONTEND / "lib" / "esc.ts"
_FE_SEGMENTS = _FRONTEND / "chat" / "answer-segments.ts"


@pytest.fixture(scope="module")
def chat_js() -> str:
    """The chat analysis surface: the ported frontend modules concatenated."""
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in (_FE_PANEL, _FE_STREAM, _FE_TURN, _FE_MD, _FE_NAV, _FE_ESC)
    )


@pytest.fixture(scope="module")
def dashboard_css() -> str:
    return _CSS.read_text(encoding="utf-8")


def _panel_region(chat_js: str) -> str:
    """The block that builds the single collapsible panel.

    The whole panel module (analysis-panel.tsx IS the region).
    """
    return _FE_PANEL.read_text(encoding="utf-8")


# --- Collapsible panel -------------------------------------------------------

def test_single_collapsible_details_panel(chat_js: str) -> None:
    assert "agent-analysis-details" in chat_js
    assert "agent-analysis-summary" in chat_js
    # zero-innerHTML: the panel is a JSX <details>/<summary> element
    # (native collapse) — exactly one details of this class in the surface.
    assert chat_js.count('<details className="agent-analysis-details">') == 1
    assert '<summary className="agent-analysis-summary">' in chat_js


def test_panel_collapsed_by_default(chat_js: str) -> None:
    # Within the panel-building block, the <details> is never forced open.
    region = _panel_region(chat_js)
    assert "details.open" not in region
    assert "setAttribute('open'" not in region


def test_summary_is_data_driven_and_xss_safe(chat_js: str) -> None:
    # The one-line summary is built from counts only and rendered safely:
    # it renders as JSX TEXT (escaped by construction — never an HTML-string
    # sink).
    assert '<summary className="agent-analysis-summary">{summaryText}</summary>' in chat_js
    assert "🔍 Agent Analysis —" in chat_js
    region = _panel_region(chat_js)
    for literal in ("Milwaukee", "Detroit", "Cedar Rapids", "filler", "pump"):
        assert literal.lower() not in region.lower(), (
            f"panel must stay generic — found hardcoded literal {literal!r}"
        )


def test_css_collapse_matches_existing_pattern(dashboard_css: str) -> None:
    assert ".agent-analysis-details" in dashboard_css
    assert ".agent-analysis-summary" in dashboard_css
    # Rotating disclosure marker mirroring .persona-summary.
    assert ".agent-analysis-summary::before" in dashboard_css
    assert ".agent-analysis-details[open] > .agent-analysis-summary::before" in dashboard_css


# --- Denoise -----------------------------------------------------------------

def test_developer_jargon_removed(chat_js: str) -> None:
    # The unexplained "% deterministic" label, the "Wasted Calls" header, and the
    # always-on "Fix:"/"Impact:" fills are gone from the rendered markup.
    assert "% deterministic</span>" not in chat_js
    assert "⚠️ Wasted Calls" not in chat_js
    assert "Fix:</span>" not in chat_js
    assert "Impact: ${" not in chat_js


def test_enum_recommendation_cards_demoted(chat_js: str) -> None:
    # No inline enum system.table.field recommendation cards in the chat view.
    assert "${r.system || ''}.${r.table || ''}.${r.field || ''}" not in chat_js
    assert "💡 Recommendations (" not in chat_js


def test_user_friendly_wording_present(chat_js: str) -> None:
    # Composition surface replaces the old red "% deterministic" bar.
    assert "How this answer was assembled" in chat_js
    assert "Exploration steps that found no matching data" in chat_js
    # Recommendations demoted to a single link into Self-Learning Discoveries.
    assert "schema hint" in chat_js
    # CSP-safe delegated handler — the panel emits data-nav="self-learning"
    # anchors and thread-clicks.ts routes them to switchArchTab('proposed').
    assert 'data-nav="self-learning"' in chat_js
    assert "switchArchTab?.('proposed')" in chat_js


def test_na_fill_hidden(chat_js: str) -> None:
    # A guard exists so unusable "N/A" suggestions never render.
    assert "naRe" in chat_js
    assert "n\\/?a" in chat_js  # the /^\s*n\/?a\s*$/i regex source


# --- XSS-safety of untrusted LLM fields --------------------------------------

def test_llm_fields_escaped(chat_js: str) -> None:
    # The shared lib/esc.ts helper (escapeHtml) is re-exported as escHtml by
    # md.ts (DRY consolidation).
    assert "function escHtml(" in chat_js or "function escapeHtml(" in chat_js
    # zero-innerHTML: the analysis fields render as JSX TEXT children
    # (escaped by construction) — and the panel module has NO string sink.
    panel = _FE_PANEL.read_text(encoding="utf-8")
    assert "{a.summary}" in panel
    assert "{w.fix}" in panel
    assert "dangerouslySetInnerHTML" not in panel
    assert "${a.summary}" not in chat_js
    assert "${w.reason || ''}" not in chat_js


# --- De-duplication + neutral distribution bar (redesign) --------------------

def test_analysis_util_loaded_before_chat_js() -> None:
    # The pure distribution helper must be defined before the streaming handler
    # consumes it — the ES module graph guarantees it; assert the wiring
    # (answer-segments.ts side-effect-imports the lib; the panel + stream
    # import from it).
    segments = _FE_SEGMENTS.read_text(encoding="utf-8")
    assert "import '../lib/analysis-util.js'" in segments
    panel = _FE_PANEL.read_text(encoding="utf-8")
    assert "buildDistributionSegments" in panel
    assert "from './answer-segments'" in panel
    # W3.1: the streaming surface split into the transport (useAskStream)
    # and the store reducers (thread-store) — the reducers now consume the
    # panel builders on `done`, and the transport dispatches into the store.
    store = (_FRONTEND / "chat" / "thread-store.ts").read_text(encoding="utf-8")
    assert "from './analysis-panel'" in store
    stream = _FE_STREAM.read_text(encoding="utf-8")
    assert "from './thread-store'" in stream


def test_single_agent_analysis_title(chat_js: str) -> None:
    # Exactly ONE "🔍 Agent Analysis" heading — the collapsed panel summary.
    assert chat_js.count("🔍 Agent Analysis") == 1


def test_query_intelligence_box_removed(chat_js: str) -> None:
    # The separate "📊 Query Intelligence" box is folded away — no duplicate
    # counts / no third redundant analysis surface.
    assert "📊 Query Intelligence" not in chat_js


def test_distribution_bar_uses_pure_helper(chat_js: str) -> None:
    region = _panel_region(chat_js)
    assert "buildDistributionSegments(" in region
    assert "How this answer was assembled" in region
    # Stacked composition, not a good/bad percentage bar.
    assert "seg.pct" in region and "seg.color" in region and "seg.label" in region


def test_no_red_for_low_score(chat_js: str) -> None:
    # The only red on the chat surface is the error paragraph — no red-for-low
    # "graph-guided"/"deterministic" score bar survives anywhere in the surface.
    # The transport stores PLAIN message text and the SINGLE red paragraph
    # renders as JSX in AssistantTurn.tsx (zero-innerHTML), coloured by its
    # class in chat/answer.css (inline styles moved to stylesheets, 2026-10).
    assert "var(--red)" not in chat_js
    assert 'className="turn-error-text"' in _FE_TURN.read_text(encoding="utf-8")
    answer_css = (_FRONTEND / "chat" / "answer.css").read_text(encoding="utf-8")
    assert ".turn-error-text { color: var(--red); }" in answer_css
    panel_css = (_FRONTEND / "chat" / "analysis-panel.css").read_text(encoding="utf-8")
    assert "var(--red)" not in panel_css
    # And the graph-guided count is presented as a neutral stat, never as a
    # red/amber/green pass-fail pctColor next to the composition.
    region = _panel_region(chat_js)
    assert "pctColor" not in region


def test_distribution_labels_are_escaped(chat_js: str) -> None:
    # Segment labels (system display names) render safely as JSX text children.
    region = _panel_region(chat_js)
    assert "{seg.label}" in region
    assert "dangerouslySetInnerHTML" not in region
