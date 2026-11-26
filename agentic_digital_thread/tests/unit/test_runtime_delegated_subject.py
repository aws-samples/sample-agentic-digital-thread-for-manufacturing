# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""ADR-0079: the RESERVED ``delegatedSubject`` payload key in the orchestrator runtime.

Pins:
  * ``_runtime_entrypoint`` POPS the key unconditionally — on BOTH the legacy
    and the integrity path — so it never reaches the prompt or the model;
  * the chart-injecting build wrapper passes ``delegated_subject=`` to
    ``build_orchestrator`` ONLY when the invocation carried one; every other
    build call is byte-identical (no new kwarg);
  * a following invocation without the key never inherits a previous subject.

Offline: fake legacy/integrity implementations; no AWS.
"""
from __future__ import annotations

import asyncio

import pytest

from agents import runtime_app
from agents.auth.forwarding import DELEGATED_SUBJECT_FIELD


class _Agent:
    async def stream_async(self, prompt, **kwargs):
        yield {"data": "x"}


def _run_selected(monkeypatch, payload, *, integrity: bool):
    """Drive _runtime_entrypoint; every implementation calls build() once."""
    built = []

    def real_build(**kwargs):
        built.append(kwargs)
        return _Agent()

    build = runtime_app._chart_injecting_build(real_build)
    seen_payloads = []

    async def legacy(p, context):
        seen_payloads.append(dict(p))
        build(session_id="s", user_id="u", write_memory=False, user_access_token=None)
        yield {"data": "legacy"}

    async def integrity_impl(p, context, build_orchestrator):
        seen_payloads.append(dict(p))
        build_orchestrator(session_id="s", user_id="u", write_memory=False,
                           user_access_token=None)
        yield {"data": "integrity"}

    monkeypatch.setattr(runtime_app, "_invoke_integrity_runtime", integrity_impl)
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    if integrity:
        monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    else:
        monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    selected = runtime_app._runtime_entrypoint(legacy, build_orchestrator=build)

    async def run():
        return [event async for event in selected(payload, None)]

    asyncio.run(run())
    return built, seen_payloads


@pytest.mark.parametrize("integrity", [False, True], ids=["legacy", "integrity"])
def test_subject_is_popped_and_threaded_to_the_build(monkeypatch, integrity):
    payload = {"prompt": "q", DELEGATED_SUBJECT_FIELD: "delegated-sub"}
    built, seen = _run_selected(monkeypatch, payload, integrity=integrity)
    assert DELEGATED_SUBJECT_FIELD not in payload
    assert all(DELEGATED_SUBJECT_FIELD not in p for p in seen)
    assert built == [{"session_id": "s", "user_id": "u", "write_memory": False,
                      "user_access_token": None,
                      "delegated_subject": "delegated-sub"}]


@pytest.mark.parametrize("integrity", [False, True], ids=["legacy", "integrity"])
def test_absent_subject_leaves_the_build_call_unchanged(monkeypatch, integrity):
    built, _ = _run_selected(monkeypatch, {"prompt": "q"}, integrity=integrity)
    assert built == [{"session_id": "s", "user_id": "u", "write_memory": False,
                      "user_access_token": None}]


@pytest.mark.parametrize("value", ["", 7, {"sub": "x"}, None])
def test_malformed_subject_is_popped_and_ignored(monkeypatch, value):
    payload = {"prompt": "q", DELEGATED_SUBJECT_FIELD: value}
    built, _ = _run_selected(monkeypatch, payload, integrity=False)
    assert DELEGATED_SUBJECT_FIELD not in payload
    assert "delegated_subject" not in built[0]


def test_a_later_invocation_never_inherits_the_subject(monkeypatch):
    _run_selected(monkeypatch, {"prompt": "q", DELEGATED_SUBJECT_FIELD: "a"},
                  integrity=False)
    built, _ = _run_selected(monkeypatch, {"prompt": "q"}, integrity=False)
    assert "delegated_subject" not in built[0]
