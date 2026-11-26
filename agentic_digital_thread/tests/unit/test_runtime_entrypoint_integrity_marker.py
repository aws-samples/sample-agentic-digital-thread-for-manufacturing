# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A-lite response-integrity marker at the ``_runtime_entrypoint`` seam (C3).

Base envs (``RESPONSE_INTEGRITY_MODE`` unset ⇒ report-only, ADR-0047 opt-in)
stream NO integrity verdict fields by design, so the smoke scorecard could not
tell "enforcement not enabled on this env" from "integrity failed". The seam —
the sanctioned outside-the-frozen-loop hook — appends ONE trailing additive
``{"response_integrity": {"mode": ...}}`` event after the legacy generator
completes; the gate-frozen legacy invoke body stays byte-identical. The
enforce path carries ``mode`` inside final_events' ``integrity_metadata``.

Offline: no AWS, no network. Enforce-path tests monkeypatch the env
(conventions.md).
"""
from __future__ import annotations

import asyncio

import pytest

from agents import runtime_app

LEGACY_EVENTS = [
    {"data": "hello"},
    {"intelligence_summary": {"total_tool_calls": 1}},
]
INTEGRITY_EVENTS = [
    {"data": "gated"},
    {"response_integrity": {"verdict": "accepted", "mode": "enforce"}},
]


def _drain(monkeypatch) -> list[dict]:
    """Run the selected entrypoint against fake implementations; return events."""
    async def legacy(payload, context):
        for event in LEGACY_EVENTS:
            yield event

    async def integrity(payload, context, build_orchestrator):
        for event in INTEGRITY_EVENTS:
            yield event

    monkeypatch.setattr(runtime_app, "_invoke_integrity_runtime", integrity)
    selected = runtime_app._runtime_entrypoint(legacy, build_orchestrator=object())
    events: list[dict] = []

    async def run():
        async for event in selected({}, None):
            events.append(event)

    asyncio.run(run())
    return events


def _set_mode(monkeypatch, mode: str | None) -> None:
    if mode is None:
        monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    else:
        monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", mode)


# ---------------------------------------------------------------------------
# Legacy selection — byte-identical stream + exactly one trailing marker
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env_value,marker_mode", [
    (None, "report-only"),           # unset ⇒ default
    ("report-only", "report-only"),
    ("annotate", "annotate"),
])
def test_legacy_stream_byte_identical_plus_one_trailing_marker(
        monkeypatch, env_value, marker_mode):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    _set_mode(monkeypatch, env_value)
    events = _drain(monkeypatch)
    # The legacy stream is byte-identical to the fake generator's output …
    assert events[:len(LEGACY_EVENTS)] == LEGACY_EVENTS
    # … followed by exactly ONE trailing additive marker, nothing else.
    assert events[len(LEGACY_EVENTS):] == [
        {"response_integrity": {"mode": marker_mode}},
    ]
    assert sum(1 for e in events if "response_integrity" in e) == 1


def test_mode_b_enforce_takes_legacy_path_with_env_mode_marker(monkeypatch):
    """Mode B never routes to the integrity runtime; the marker reports the
    env's configured mode truthfully (enforce set but not effective here)."""
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    events = _drain(monkeypatch)
    assert events[:len(LEGACY_EVENTS)] == LEGACY_EVENTS
    assert events[-1] == {"response_integrity": {"mode": "enforce"}}


# ---------------------------------------------------------------------------
# Enforce selection — integrity runtime, no seam-appended marker
# ---------------------------------------------------------------------------

def test_enforce_routes_to_integrity_runtime_without_extra_marker(monkeypatch):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    events = _drain(monkeypatch)
    # Pinned existing behavior: the integrity implementation handles the turn;
    # its stream passes through unchanged (mode rides final_events, not the seam).
    assert events == INTEGRITY_EVENTS
    assert sum(1 for e in events if "response_integrity" in e) == 1


# ---------------------------------------------------------------------------
# final_events — enforce metadata carries the additive mode key
# ---------------------------------------------------------------------------

def test_final_events_integrity_metadata_carries_enforce_mode(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    monkeypatch.setattr(
        "agents.runtime_integrity.persist_typed_proposals", lambda *a, **k: 0)
    invocation = runtime_app._RuntimeIntegrityInvocation("hello")
    events = invocation.final_events()
    integrity = next(
        e["response_integrity"] for e in events if "response_integrity" in e)
    assert integrity["mode"] == "enforce"
    # Existing verdict fields remain alongside (additive only).
    for key in ("verdict", "completeness", "claim_count", "reason_codes"):
        assert key in integrity


def test_public_scalar_metadata_allowlist_simply_omits_mode():
    """The bounded-capture allowlists tolerate the new key by omission."""
    from agents.runtime_integrity import public_scalar_metadata
    safe = public_scalar_metadata(
        {"verdict": "accepted", "mode": "enforce"}, integrity=True)
    assert safe.get("verdict") == "accepted"
    assert "mode" not in safe
