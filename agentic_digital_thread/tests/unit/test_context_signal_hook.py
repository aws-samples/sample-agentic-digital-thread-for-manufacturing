# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""ADR-0063 D5 hook wiring: the ``context_not_reflected`` accumulator must
actually be CALLED from the turn path, or the detector is dead code.

Pins the hook's contract (mirrors ``_record_empty_join_signal``'s posture,
which these tests are modelled on):
  - synthetic sessions and automated briefings NEVER write;
  - scoped to the systems THIS turn queried (``<system>___<tool>`` prefix),
    bounded by ``_CONTEXT_SIGNAL_MAX_SYSTEMS`` so a wide fan-out turn cannot
    turn a post-turn accumulator into a read storm;
  - a FAILED business-context read (``read_business_context`` returns None) is
    never read as "no context" — that system is skipped;
  - the hook NEVER raises, whatever the accumulator or the graph does;
  - the answer text handed to the detector is the turn's assembled response.

Offline: no AWS, no network.
"""
from __future__ import annotations

import agents.runtime_app as ra


class _Recorder:
    """Captures record_context_not_reflected calls; returns one subject each."""

    def __init__(self, raises: bool = False):
        self.calls: list[dict] = []
        self.raises = raises

    def __call__(self, system_id, context, answer_text, *, answer_ref, turn_id):
        if self.raises:
            raise RuntimeError("accumulator down")
        self.calls.append({
            "system_id": system_id, "context": context, "answer": answer_text,
            "answer_ref": answer_ref, "turn_id": turn_id,
        })
        return [f"{system_id}::directive"]


def _install(monkeypatch, recorder, context=None, read_raises=False):
    import agents.business_context as bc
    import agents.failure_signals as fs

    def _read(system_id):
        if read_raises:
            raise RuntimeError("graph down")
        return context
    monkeypatch.setattr(bc, "read_business_context", _read)
    monkeypatch.setattr(fs, "record_context_not_reflected", recorder)


_CONTEXT = {"business_context": "Do not call them batches.", "enabled": True,
            "updated_at": "2026-08-18T00:00:00+00:00", "context_hash": "abc123"}


def _calls(system: str = "sys-a", tool: str = "query"):
    return [{"toolUseId": "tool-1", "name": f"{system}___{tool}"}]


def test_hook_records_for_each_queried_system(monkeypatch):
    rec = _Recorder()
    _install(monkeypatch, rec, context=_CONTEXT)
    counted = ra._record_context_not_reflected_signal(
        _calls(), ["Here are the ", "batch counts."], "how many batches?", "s-1")
    assert counted == ["sys-a::directive"]
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["system_id"] == "sys-a"
    assert call["answer"] == "Here are the batch counts."
    assert call["answer_ref"] == "s-1:tool-1"
    assert call["turn_id"] == "tool-1"


def test_synthetic_sessions_never_write(monkeypatch):
    rec = _Recorder()
    _install(monkeypatch, rec, context=_CONTEXT)
    for session in ("eval-run-1", "warmup-1", "tryit-1"):
        if not ra._is_synthetic_session(session):
            continue
        assert ra._record_context_not_reflected_signal(
            _calls(), ["text"], "q", session) == []
    assert rec.calls == []


def test_automated_briefings_never_write(monkeypatch):
    rec = _Recorder()
    _install(monkeypatch, rec, context=_CONTEXT)
    assert ra._record_context_not_reflected_signal(
        _calls(), ["text"], "Morning BRIEFING for the plant", "s-1") == []
    assert rec.calls == []


def test_failed_context_read_is_skipped_not_treated_as_absent(monkeypatch):
    """read_business_context returns None ONLY when the graph read failed.
    Counting that as 'no correction exists' would be the swallowed-failure bug."""
    rec = _Recorder()
    _install(monkeypatch, rec, context=None)
    assert ra._record_context_not_reflected_signal(
        _calls(), ["text"], "q", "s-1") == []
    assert rec.calls == []


def test_read_bound_caps_per_turn_graph_reads(monkeypatch):
    rec = _Recorder()
    _install(monkeypatch, rec, context=_CONTEXT)
    many = [{"toolUseId": f"t-{i}", "name": f"sys-{i}___query"} for i in range(10)]
    counted = ra._record_context_not_reflected_signal(many, ["text"], "q", "s-1")
    assert len(rec.calls) == ra._CONTEXT_SIGNAL_MAX_SYSTEMS
    assert len(counted) == ra._CONTEXT_SIGNAL_MAX_SYSTEMS


def test_no_answer_text_records_nothing(monkeypatch):
    rec = _Recorder()
    _install(monkeypatch, rec, context=_CONTEXT)
    assert ra._record_context_not_reflected_signal(_calls(), [], "q", "s-1") == []
    assert rec.calls == []


def test_orchestrator_own_tools_are_not_a_system(monkeypatch):
    rec = _Recorder()
    _install(monkeypatch, rec, context=_CONTEXT)
    calls = [{"toolUseId": "t-1", "name": "digital-thread___explore_graph"}]
    assert ra._record_context_not_reflected_signal(
        calls, ["text"], "q", "s-1") == []
    assert rec.calls == []


def test_hook_never_raises_when_the_accumulator_fails(monkeypatch):
    _install(monkeypatch, _Recorder(raises=True), context=_CONTEXT)
    assert ra._record_context_not_reflected_signal(
        _calls(), ["text"], "q", "s-1") == []


def test_hook_never_raises_when_the_context_read_fails(monkeypatch):
    rec = _Recorder()
    _install(monkeypatch, rec, context=_CONTEXT, read_raises=True)
    assert ra._record_context_not_reflected_signal(
        _calls(), ["text"], "q", "s-1") == []
    assert rec.calls == []
