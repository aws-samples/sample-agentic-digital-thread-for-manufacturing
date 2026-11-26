# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Cross-derivation pin for the feedback ``cited_systems`` unit.

The frontend sends system IDS derived from the turn's citation tool names
(``frontend/src/lib/cited-systems.js::citedSystemIds``); the backend diffs
them against ids derived from the re-run's tool names
(``metrics_registry.systems_from_tool_names``). These are TWO implementations
of ONE derivation — if they drift, every exploration manufactures bogus
divergence and spams the review queue (the observed failure: display labels
on one side, lowercase ids on the other).

This pin runs the REAL frontend function (node subprocess — the
``test_analysis_util.py`` idiom) against the REAL backend derivation on the
same fixture tool names and asserts byte-equality AND that ``_diff_findings``
detects NO divergence. Kills: the frontend reintroducing label-sending
(labels are not id-shaped and never equal the backend ids); the backend
switching to display names (equality fails). Skips cleanly when node is
unavailable so the offline lane stays green — the derivation-shape halves are
additionally pinned per-side (vitest ``cited-systems.test.ts``; python
``test_feedback_exploration.py``).
"""
from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
from pathlib import Path

import pytest

import agents.feedback_exploration as fx
from agents.metrics_registry import systems_from_tool_names

_ROOT = Path(__file__).resolve().parents[2]
_UTIL_JS = _ROOT / "frontend" / "src" / "lib" / "cited-systems.js"
# The feedback wiring (citedSystems) moved from AssistantTurn.tsx into the
# answer footer (2026-10).
_ASSISTANT_TURN = _ROOT / "frontend" / "src" / "chat" / "AnswerFooter.tsx"

_NODE = shutil.which("node")
_needs_node = pytest.mark.skipif(_NODE is None, reason="node not available")

# One realistic fixture "system": gateway-prefixed tool names as the
# orchestrator sees them (ids use ``_`` for ``-`` in the prefix), plus
# orchestrator-own tools that must NOT count on either side.
_FIXTURE_TOOLS = [
    "mes_mil___query_mes_mil",
    "mes_mil___get_schema",
    "erp___query_erp",
    "lake-athena___athena_run_query",
    "explore_graph",
    "get_system_schema",
]


def _frontend_ids(tool_names: list[str]) -> list[str]:
    """Run the REAL frontend derivation via node. The frontend package.json
    ``"type":"module"`` makes the IIFE load as ESM, where the CJS
    ``module.exports`` guard is skipped but the globalThis publication still
    runs (the ``test_analysis_util.py`` idiom)."""
    script = (
        f"const m=require({json.dumps(str(_UTIL_JS))});"
        "const f=m.citedSystemIds||globalThis.citedSystemIds;"
        f"const ids=f({json.dumps(tool_names)});"
        "process.stdout.write(JSON.stringify(ids));"
    )
    out = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_util_file_exists() -> None:
    assert _UTIL_JS.is_file()


@_needs_node
def test_frontend_and_backend_derive_identical_system_ids() -> None:
    """The two halves of the derivation are byte-identical on one fixture."""
    assert _frontend_ids(_FIXTURE_TOOLS) == systems_from_tool_names(_FIXTURE_TOOLS)


@_needs_node
def test_cross_derivation_detects_no_divergence() -> None:
    """THE H1 pin: a re-run touching exactly the cited systems diffs clean.

    The frontend ids play the ``cited_systems`` role; the backend derivation
    (from the SAME tool names) plays the re-run's ``systems_queried`` role —
    the real production pairing. Any unit mismatch (labels reintroduced on
    the client, display names on the server) makes ``_diff_findings`` report
    system divergence and this fails."""
    cited = _frontend_ids(_FIXTURE_TOOLS)
    run = {"systems_queried": systems_from_tool_names(_FIXTURE_TOOLS),
           "rules_applied": []}
    diff = fx._diff_findings(run, cited, [])
    assert diff["systems_beyond_cited"] == []
    assert diff["cited_systems_not_queried"] == []
    assert fx._has_divergence(diff) is False


@_needs_node
def test_display_labels_would_be_dropped_not_diffed() -> None:
    """Regression shape of the ORIGINAL H1 bug: label-shaped cited entries
    (getSystemLabel output — spaces/newlines) are DROPPED by the server-side
    normalization, so they can neither match nor manufacture
    ``cited_systems_not_queried`` divergence."""
    labels = ["MES\nMil", "Lake Athena", "Knowledge Graph"]
    run = {"systems_queried": systems_from_tool_names(_FIXTURE_TOOLS),
           "rules_applied": []}
    diff = fx._diff_findings(run, labels, [])
    assert diff["cited_systems_not_queried"] == []
    assert diff["non_id_cited_dropped"] == 3


def test_assistant_turn_wires_ids_not_labels() -> None:
    """Source pin on the seam itself (kills label-sending at the wiring
    point even when node is unavailable)."""
    src = _ASSISTANT_TURN.read_text(encoding="utf-8")
    # ``shown``: this turn's citations plus the earlier ([E<k>]) ones the
    # answer cites, i.e. the Sources chips the footer renders.
    assert "const shown = [...current, ...earlier];" in src
    assert "citedSystems={citedSystemIds(shown.map((c) => c.tool))}" in src
    assert ".map((c) => c.label)" not in src
