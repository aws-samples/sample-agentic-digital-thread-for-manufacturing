# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit test for the pure ``buildDistributionSegments`` helper.

The helper lives in ``frontend/src/lib/analysis-util.js`` and powers the neutral
"How this answer was assembled" stacked bar in the chat Agent Analysis panel.
It is DOM-free and node-requirable, so we exercise the real function via a small
node subprocess and assert the composition maths (segment order, counts, whole
percentages, neutral palette cycling — never red-for-low).

Skips cleanly when node is unavailable so the offline lane stays green.
Fully offline: no AWS / DB / network.
"""
from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_UTIL_JS = _ROOT / "frontend" / "src" / "lib" / "analysis-util.js"

_NODE = shutil.which("node")
_needs_node = pytest.mark.skipif(_NODE is None, reason="node not available")


def _run(seq, neptune_count, palette=None):
    """Invoke buildDistributionSegments in node and return the parsed result."""
    args = json.dumps([seq, neptune_count] + ([palette] if palette is not None else []))
    # The frontend package.json "type":"module" makes the IIFE load as ESM,
    # where the CJS module.exports guard is skipped but the globalThis
    # publication still runs.
    script = (
        f"const m=require({json.dumps(str(_UTIL_JS))});"
        "const f=m.buildDistributionSegments||globalThis.buildDistributionSegments;"
        f"const a={args};"
        "process.stdout.write(JSON.stringify(f.apply(null,a)));"
    )
    out = subprocess.run(  # nosec B603 - fixed argv list, no shell, internal inputs only
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


# --- Module surface ----------------------------------------------------------

def test_util_file_exists() -> None:
    assert _UTIL_JS.is_file()


def test_palette_is_neutral_no_red_for_low() -> None:
    # The palette must not encode a good/bad signal — no pure/danger red.
    src = _UTIL_JS.read_text(encoding="utf-8")
    assert "DISTRIBUTION_PALETTE" in src
    assert "buildDistributionSegments" in src
    for banned in ("#ff0000", "#f85149", "var(--red)"):
        assert banned not in src, f"palette must not use red-for-low ({banned})"


# --- Composition maths (via node) --------------------------------------------

@_needs_node
def test_empty_input_returns_no_segments() -> None:
    assert _run([], 0) == []
    assert _run(None, 0) == []


@_needs_node
def test_graph_only_is_full_bar() -> None:
    segs = _run([], 3)
    assert len(segs) == 1
    assert segs[0]["label"] == "Knowledge Graph"
    assert segs[0]["count"] == 3
    assert segs[0]["pct"] == 100


@_needs_node
def test_graph_first_then_systems_by_descending_count() -> None:
    seq = [{"label": "ERP"}, {"label": "MES"}, {"label": "ERP"}, {"label": "ERP"}]
    segs = _run(seq, 2)
    labels = [s["label"] for s in segs]
    # Knowledge Graph anchors first, then systems by descending count.
    assert labels[0] == "Knowledge Graph"
    assert labels[1] == "ERP"  # 3 calls
    assert labels[2] == "MES"  # 1 call
    counts = {s["label"]: s["count"] for s in segs}
    assert counts == {"Knowledge Graph": 2, "ERP": 3, "MES": 1}
    # Percentages are whole numbers of a 6-call total.
    assert {s["pct"] for s in segs} == {33, 50, 17}
    assert all(isinstance(s["pct"], int) for s in segs)


@_needs_node
def test_percentages_are_share_of_total_tool_calls() -> None:
    seq = [{"label": "ERP"}, {"label": "ERP"}, {"label": "ERP"}]
    segs = _run(seq, 1)  # total = 4
    counts = {s["label"]: s for s in segs}
    assert counts["Knowledge Graph"]["pct"] == 25
    assert counts["ERP"]["pct"] == 75


@_needs_node
def test_colors_cycle_from_the_neutral_palette() -> None:
    seq = [{"label": "A"}, {"label": "B"}, {"label": "C"}, {"label": "D"},
           {"label": "E"}, {"label": "F"}]
    # 6 systems + graph = 7 segments → palette (len 6) must wrap, never crash.
    segs = _run(seq, 1)
    assert len(segs) == 7
    colors = [s["color"] for s in segs]
    assert all(c.startswith("#") for c in colors)
    # Wrap-around: 7th segment reuses the first palette color.
    assert colors[6] == colors[0]


@_needs_node
def test_custom_palette_is_respected() -> None:
    segs = _run([{"label": "ERP"}], 1, palette=["#111111", "#222222"])
    assert segs[0]["color"] == "#111111"
    assert segs[1]["color"] == "#222222"


@_needs_node
def test_blank_labels_fall_back_and_group() -> None:
    seq = [{"label": ""}, {"label": None}, {"label": "  ERP  "}, {"label": "ERP"}]
    segs = _run(seq, 0)
    counts = {s["label"]: s["count"] for s in segs}
    # Blank/None collapse to "System"; whitespace-trimmed "ERP" groups together.
    assert counts == {"ERP": 2, "System": 2}
