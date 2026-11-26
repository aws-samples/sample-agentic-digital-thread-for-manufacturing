# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit test for the pure ``selectFinalAnswer`` helper.

The helper lives in ``frontend/src/lib/analysis-util.js`` and reconstructs the
assistant's final answer from the per-turn text runs the orchestrator streams
(runs are separated by tool calls). It fixes the P0 "missing answer" bug: the
model often writes the full report FIRST, then makes a trailing housekeeping
tool call (e.g. save_correlation), then a short epilogue — so "keep only the
text after the LAST tool call" dropped the entire report. The helper keeps the
LONGEST run plus anything after it, and drops shorter inter-tool reasoning runs.

DOM-free and node-requirable, exercised via a small node subprocess. Skips
cleanly when node is unavailable so the offline lane stays green.
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


def _run(segments):
    args = json.dumps([segments])
    # The frontend package.json "type":"module" makes the IIFE load as ESM,
    # where the CJS module.exports guard is skipped but the globalThis
    # publication still runs.
    script = (
        f"const m=require({json.dumps(str(_UTIL_JS))});"
        "const f=m.selectFinalAnswer||globalThis.selectFinalAnswer;"
        f"const a={args};"
        "process.stdout.write(JSON.stringify(f.apply(null,a)));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)  # nosec B603 - fixed argv list, no shell, internal inputs only
    return json.loads(out.stdout)


def test_helper_is_exported() -> None:
    src = _UTIL_JS.read_text(encoding="utf-8")
    assert "selectFinalAnswer" in src


@_needs_node
def test_empty_and_blank_return_empty() -> None:
    assert _run([]) == ""
    assert _run(None) == ""
    assert _run(["   ", "\n"]) == ""


@_needs_node
def test_single_segment_is_the_answer() -> None:
    assert _run(["The final answer."]) == "The final answer."


@_needs_node
def test_longest_run_wins_over_inter_tool_reasoning() -> None:
    # Short reasoning runs BEFORE the report are dropped; the report is kept.
    report = "# Report\n\n" + ("row line. " * 80)
    segs = [
        "Let me query MES.",                 # reasoning (short)
        "Now I'll query ERP and CMMS.",      # reasoning (short)
        report,                              # the real answer (longest)
    ]
    assert _run(segs) == report.strip()


@_needs_node
def test_trailing_epilogue_after_housekeeping_tool_is_appended() -> None:
    # The P0 case: full report, then a trailing save_correlation tool call, then
    # a short epilogue. Report is longest → kept; epilogue follows → appended.
    report = "# Cross-System Report\n\n" + ("data. " * 100)
    epilogue = "Two correlations saved for review."
    segs = ["Analyzing systems.", report, epilogue]
    out = _run(segs)
    assert out.startswith("# Cross-System Report")
    assert out.endswith(epilogue)
    assert "Analyzing systems." not in out   # pre-report reasoning dropped
