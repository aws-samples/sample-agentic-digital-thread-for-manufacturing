# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`save_correlation` emits *insight*, and deploy.sh [2f]
refreshes the adt_digital_thread runtime image.

Deployed-verification found the last ungated emitter: the `digital-thread` MCP
tool `save_correlation` logged ``proposed_type="identity_rule"`` with the
narrative sentence crammed into ``from_field``/``to_field`` (``pattern[:80]``)
— the same sentence-as-field bug Wave 2.2 fixed in ``runtime_app``. Narrative
correlations are FINDINGS: they must land as ``insight`` with EMPTY field
pair and the full text preserved in ``query_context``
(``Auto-analysis: ...``, 500-char cap), mirroring
``runtime_app._extract_correlation_insights``.

It compounds with a deploy gap: ``deploy.sh`` [2f] excluded
``adt_digital_thread`` from the per-system image refresh and NO other step
updates its image, so the runtime served stale code indefinitely. [2f] must
now refresh it (adt_orchestrator / adt_onboarding stay excluded — they have
their own update steps §2c/§2e).

Tripwire tests read the source files as text so a regression cannot land
silently.
"""

import json
from pathlib import Path
from unittest.mock import patch

from agents import digital_thread_server as dt_server

_ADT_ROOT = Path(__file__).resolve().parents[2]  # agentic_digital_thread/


# --- save_correlation emits insight, not identity_rule -----------------------

def test_save_correlation_emits_insight_with_empty_fields():
    """A narrative sentence is never a table.column pair: from_field/to_field
    MUST be empty, the systems pair preserved, concept stays 'correlation'."""
    with patch.object(dt_server.self_learning, "log_proposed_mapping", return_value="c-42") as m:
        out = json.loads(dt_server.save_correlation(
            pattern="downtime in CMMS correlates with quality drops in MES",
            observed="3 of 4 downtime events preceded a pass-rate dip",
            confidence="high", systems=["cmms", "mes-mil"],
        ))
    kwargs = m.call_args.kwargs
    assert kwargs["proposed_type"] == "insight"
    assert kwargs["from_field"] == "" and kwargs["to_field"] == ""
    assert kwargs["from_value"] == "" and kwargs["to_value"] == ""
    assert kwargs["from_system"] == "cmms" and kwargs["to_system"] == "mes-mil"
    assert kwargs["concept"] == "correlation"
    assert kwargs["confidence"] == 0.9  # "high" → 0.9, unchanged mapping
    assert kwargs["source"] == "agent"
    # Return contract keeps status/mapping_id shape; type now says insight.
    assert out == {"status": "logged", "mapping_id": "c-42", "type": "insight"}


def test_save_correlation_context_carries_pattern_and_observed():
    """query_context preserves BOTH the pattern and the observation in the
    Wave 2.2 'Auto-analysis: <text>' convention with the 500-char cap."""
    pattern = "downtime in CMMS correlates with quality drops in MES"
    observed = "3 of 4 downtime events preceded a pass-rate dip"
    with patch.object(dt_server.self_learning, "log_proposed_mapping", return_value="c-1") as m:
        dt_server.save_correlation(
            pattern=pattern, observed=observed, systems=["cmms", "mes-mil"],
        )
    ctx = m.call_args.kwargs["query_context"]
    assert ctx.startswith("Auto-analysis: ")
    assert pattern in ctx and observed in ctx
    assert len(ctx) <= 500


def test_save_correlation_context_without_observed_still_carries_pattern():
    with patch.object(dt_server.self_learning, "log_proposed_mapping", return_value="c-2") as m:
        dt_server.save_correlation(pattern="A tracks B", systems=["erp", "plm"])
    assert m.call_args.kwargs["query_context"] == "Auto-analysis: A tracks B"


def test_save_correlation_context_is_capped_at_500_chars():
    with patch.object(dt_server.self_learning, "log_proposed_mapping", return_value="c-3") as m:
        dt_server.save_correlation(
            pattern="p" * 400, observed="o" * 400, systems=["erp", "plm"],
        )
    assert len(m.call_args.kwargs["query_context"]) == 500


def test_save_correlation_still_skips_with_fewer_than_two_systems():
    with patch.object(dt_server.self_learning, "log_proposed_mapping") as m:
        out_one = json.loads(dt_server.save_correlation(pattern="x", systems=["cmms"]))
        out_none = json.loads(dt_server.save_correlation(pattern="x"))
    assert out_one["status"] == "skipped"
    assert out_none["status"] == "skipped"
    m.assert_not_called()


# --- source tripwire: the sentence-as-field bug must not come back -----------

def test_source_tripwire_no_pattern_as_field_in_digital_thread_server():
    """The Wave 2.2 bug shape — ``from_field=pattern`` / ``to_field=pattern``
    (with or without a slice) — must be absent from digital_thread_server.py."""
    src = Path(dt_server.__file__).read_text()
    assert "from_field=pattern" not in src
    assert "to_field=pattern" not in src


# --- deploy.sh tripwire: [2f] must refresh adt_digital_thread -----------------

def test_deploy_sh_2f_filter_refreshes_digital_thread_runtime():
    """The [2f] per-system refresh query must exclude ONLY the two infra
    runtimes with their own update steps (adt_orchestrator §2c / adt_onboarding
    §2e). adt_digital_thread has NO other image-update step: excluding it left
    the runtime on a stale image indefinitely."""
    deploy_sh = (_ADT_ROOT / "deploy.sh").read_text()
    # Locate the [2f] list-agent-runtimes query filter line.
    filter_lines = [
        line for line in deploy_sh.splitlines()
        if "starts_with(agentRuntimeName, 'adt_')" in line
    ]
    assert filter_lines, "deploy.sh [2f] runtime query filter not found"
    for line in filter_lines:
        assert "agentRuntimeName != 'adt_orchestrator'" in line
        assert "agentRuntimeName != 'adt_onboarding'" in line
        assert "adt_digital_thread" not in line, (
            "[2f] must NOT exclude adt_digital_thread — no other step updates "
            "its image"
        )
