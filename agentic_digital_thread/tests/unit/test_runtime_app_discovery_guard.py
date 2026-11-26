# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Thin fail-loud guard proof for the orchestrator fire-and-forget discovery
path (S3.1, Task 3.3 — requirement F).

After Wave 2, ``self_learning.log_proposed_mapping`` RAISES on backend failure
instead of swallowing. ``runtime_app._check_for_discoveries`` calls it in a
fire-and-forget guard; the guard MUST catch (so a discovery error never turns a
successful user query into a failure) AND log LOUDLY (warning, not debug).

No AWS, no Neptune, no network: the ``self_learning`` import target is
monkeypatched at the module global the call site rebinds at call time
(``from .self_learning import log_proposed_mapping, get_all_mappings``).
"""
from __future__ import annotations

import logging

import agents.runtime_app as runtime_app
import agents.self_learning as self_learning


# A tool call from ERP returns a UUID; a later CMMS query USES that UUID in a
# WHERE clause on an *_id field → a cross-system join_path discovery is detected.
# _check_for_discoveries gates on >=3 tool calls, >=1 explore_graph, and
# >=3 gateway (non-neptune, non-schema) calls — so the fixtures include those.
_UUID = "12345678-1234-1234-1234-123456789abc"
_TOOL_CALLS = [
    {"toolUseId": "t0", "name": "explore_graph"},
    {"toolUseId": "t1", "name": "erp___query"},
    {"toolUseId": "t2", "name": "cmms___query"},
    {"toolUseId": "t3", "name": "erp___query"},
]
_TOOL_INPUTS = {
    "t0": "MATCH (n) RETURN n",
    "t1": "SELECT id FROM erp_data.orders",
    "t2": f"SELECT * FROM work_orders WHERE asset_id = '{_UUID}'",  # nosec B608 - test fixture composing expected SQL from module constants
    "t3": "SELECT id FROM erp_data.customers",
}
_TOOL_RESULTS = {
    "t0": "graph result",
    "t1": f"row: {_UUID}",
    "t2": "ok",
    "t3": "more rows",
}


def test_raising_log_proposed_mapping_is_caught_and_logged_loudly(monkeypatch, caplog):
    """A now-RAISING log_proposed_mapping is swallowed by the thin guard (does
    NOT propagate out of the discovery check) and surfaced at WARNING."""
    calls = {"n": 0}

    def _boom(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("dynamo down")

    # No existing mappings → dedup does not short-circuit, so the call is reached.
    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: [])
    monkeypatch.setattr(self_learning, "log_proposed_mapping", _boom)

    with caplog.at_level(logging.WARNING, logger="agents.runtime_app"):
        # Must NOT raise — a discovery failure cannot fail the user query.
        runtime_app._check_for_discoveries(
            _TOOL_CALLS, _TOOL_INPUTS, _TOOL_RESULTS, "find cross-system flow", "sess-1"
        )

    assert calls["n"] == 1, "log_proposed_mapping should have been reached once"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "the raising failure must be logged loudly (>= WARNING)"
    assert any("Self-learning log failed" in r.getMessage() for r in warnings)


def test_no_discovery_does_not_call_log(monkeypatch):
    """Sanity: with no cross-system flow, the call site is not reached and the
    function returns cleanly (guard is inert on the happy path)."""
    calls = {"n": 0}

    def _count(*args, **kwargs):
        calls["n"] += 1
        return "id-x"

    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: [])
    monkeypatch.setattr(self_learning, "log_proposed_mapping", _count)

    # Only one system involved → no directional cross-system flow.
    runtime_app._check_for_discoveries(
        [{"toolUseId": "t1", "name": "erp___query"}],
        {"t1": "SELECT id FROM erp_data.orders"},
        {"t1": f"row: {_UUID}"},
        "single system",
        "sess-2",
    )
    assert calls["n"] == 0
