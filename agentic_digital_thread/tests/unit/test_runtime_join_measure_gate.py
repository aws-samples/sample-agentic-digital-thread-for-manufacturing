# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""E4 — measure gate on the runtime_app query-time join emitter (Task 1.4).

``runtime_app._check_for_discoveries`` logs a ``join_path`` proposal when it
detects a directional cross-system value flow. ``join_discovery`` carries a
defensive measure gate on its OWN emit path (a ``*_count`` / ``*_quantity`` /
``sample_size`` column is never a join key), but the runtime query-time emitter
lacked it — measure-like endpoints leaked into the proposal queue (audit E4).

These tests drive ``_check_for_discoveries`` with fake tool logs (per the
``test_runtime_app_discovery_guard.py`` conventions — no AWS, no network; the
lazy ``from .self_learning import …`` / ``from .join_discovery import …`` call
sites rebind at call time, so monkeypatching the source modules is sufficient)
and prove:

- a measure-like join endpoint is blocked at the FINAL emit (INFO log, seam
  never called);
- an identifier endpoint still emits exactly once (no regression);
- the gate is best-effort: a raising predicate falls through to today's
  behavior (the emit proceeds inside the existing thin fail-loud guard).
"""
from __future__ import annotations

import logging

import agents.join_discovery as join_discovery
import agents.runtime_app as runtime_app
import agents.self_learning as self_learning

_UUID = "12345678-1234-1234-1234-123456789abc"


def _flow_fixtures(where_field: str):
    """A directional ERP→CMMS flow: ERP returns a UUID, CMMS uses it in a WHERE
    clause on ``where_field``. Satisfies the gates in ``_check_for_discoveries``
    (>=3 tool calls, >=1 explore_graph, >=3 gateway non-schema calls)."""
    tool_calls = [
        {"toolUseId": "t0", "name": "explore_graph"},
        {"toolUseId": "t1", "name": "erp___query"},
        {"toolUseId": "t2", "name": "cmms___query"},
        {"toolUseId": "t3", "name": "erp___query"},
    ]
    tool_inputs = {
        "t0": "MATCH (n) RETURN n",
        "t1": "SELECT id FROM erp_data.orders",
        "t2": f"SELECT * FROM work_orders WHERE {where_field} = '{_UUID}'",  # nosec B608 - test fixture composing expected SQL from module constants
        "t3": "SELECT id FROM erp_data.customers",
    }
    tool_results = {
        "t0": "graph result",
        "t1": f"row: {_UUID}",
        "t2": "ok",
        "t3": "more rows",
    }
    return tool_calls, tool_inputs, tool_results


def _patch_seam(monkeypatch):
    """Monkeypatch the self_learning seams; returns the recorded emit calls."""
    calls: list[dict] = []

    def _record(*args, **kwargs):
        calls.append(kwargs)
        return "mapping-id-1"

    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: [])
    monkeypatch.setattr(self_learning, "log_proposed_mapping", _record)
    return calls


def test_measure_like_endpoint_blocked_at_final_emit(monkeypatch, caplog):
    """A detected flow whose join endpoint is measure-like never reaches
    log_proposed_mapping — and the skip is logged at INFO (proving the FINAL
    emit gate fired, not an upstream recorder filter)."""
    calls = _patch_seam(monkeypatch)
    # Measure-like AND _id-suffixed so it clears the used_values recorder and
    # reaches the final emit — exactly the leak path the gate closes.
    tool_calls, tool_inputs, tool_results = _flow_fixtures("sample_size_id")

    assert join_discovery.is_measure_like("sample_size_id"), \
        "fixture must be measure-like for this test to be meaningful"

    with caplog.at_level(logging.INFO, logger="agents.runtime_app"):
        runtime_app._check_for_discoveries(
            tool_calls, tool_inputs, tool_results, "cross-system question", "sess-m1"
        )

    assert calls == [], "measure-like endpoint must NOT be proposed as a join_path"
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("measure-like" in m for m in infos), \
        "the skip must be logged at INFO by the final-emit gate"


def test_plan_literal_reject_quantity_never_emits(monkeypatch):
    """The audit's churn subject: a flow on ``work_orders.reject_quantity``
    never produces a join_path proposal from the runtime path (blocked today by
    the _id-focused recorder; the E4 gate keeps it blocked if the recorder
    ever loosens — ``is_measure_like('reject_quantity')`` is True)."""
    calls = _patch_seam(monkeypatch)
    tool_calls, tool_inputs, tool_results = _flow_fixtures("reject_quantity")

    assert join_discovery.is_measure_like("reject_quantity")

    runtime_app._check_for_discoveries(
        tool_calls, tool_inputs, tool_results, "reject quantity question", "sess-m2"
    )
    assert calls == [], "reject_quantity must never enter the proposal queue"


def test_identifier_endpoint_still_emits_once(monkeypatch):
    """No regression: the same flow on an identifier field (``wo_id``) emits
    exactly one join_path proposal."""
    calls = _patch_seam(monkeypatch)
    tool_calls, tool_inputs, tool_results = _flow_fixtures("wo_id")

    assert not join_discovery.is_measure_like("wo_id")

    runtime_app._check_for_discoveries(
        tool_calls, tool_inputs, tool_results, "identifier question", "sess-i1"
    )

    assert len(calls) == 1, "identifier flow must still emit exactly once"
    kw = calls[0]
    assert kw["proposed_type"] == "join_path"
    assert kw["from_system"] == "erp" and kw["to_system"] == "cmms"
    assert kw["from_field"] == "work_orders.wo_id"


def test_raising_predicate_falls_through_to_emit(monkeypatch):
    """Best-effort contract: if is_measure_like raises, the gate must NOT block
    the emit — today's behavior is preserved (emit inside the existing thin
    fail-loud guard)."""
    calls = _patch_seam(monkeypatch)

    def _boom(field_name):
        raise RuntimeError("predicate exploded")

    # The call site rebinds via ``from .join_discovery import is_measure_like``
    # at call time, so patching the source module attribute is sufficient.
    monkeypatch.setattr(join_discovery, "is_measure_like", _boom)

    tool_calls, tool_inputs, tool_results = _flow_fixtures("wo_id")
    runtime_app._check_for_discoveries(
        tool_calls, tool_inputs, tool_results, "identifier question", "sess-i2"
    )
    assert len(calls) == 1, "a raising gate must fall through to the emit"
