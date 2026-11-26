# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The deterministic provenance lines are a report-only and annotate feature.

Under ``RESPONSE_INTEGRITY_MODE=enforce`` the answer is the server-built
typed envelope, so neither hop runs: the system agent's answer carries no
``[engine] Exact split:`` or ``[engine] Storage bound ran:`` line, and the
orchestrator's stream is not wrapped. Both module docstrings say so, so the
contract the invariant cites is the one the code keeps. Offline.
"""
from __future__ import annotations

import pytest

import agents.answer_provenance as provenance
import agents.answer_provenance_stream as stream

from .test_split_provenance_answer import _mode_a, _payload, _state_after, _tool_result

_SCOPE = "RESPONSE_INTEGRITY_MODE=enforce"


@pytest.mark.parametrize("module", [provenance, stream])
def test_each_module_states_that_enforce_mode_is_out_of_scope(module):
    doc = " ".join((module.__doc__ or "").split())
    assert _SCOPE in doc, module.__name__
    assert "report-only" in doc and "annotate" in doc, module.__name__


def test_the_system_hop_adds_no_provenance_line_under_enforce(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    answer = _mode_a("4,051,277 records were done.",
                     _state_after(_tool_result(_payload(split=True))))
    assert "[engine] Exact split:" not in answer


def test_the_stream_is_not_wrapped_under_enforce(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")

    async def raw(_prompt, **_kwargs):
        yield {"data": "x"}

    assert stream.wrap_stream(raw) is raw
