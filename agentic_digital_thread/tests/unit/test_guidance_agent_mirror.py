# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guidance delivery is acceptance-owned; automated writers never mirror."""
from __future__ import annotations

import agents.self_learning as learning
import agents.system_guidance as guidance


class _LegacyBackend:
    def __init__(self):
        self.saved = []
        self.state = {}

    def version(self, system_id):
        return self.state.get(system_id, ("", 0))[1]

    def load(self, system_id):
        return self.state.get(system_id, ("", 0))

    def save(self, system_id, text, version):
        self.saved.append((system_id, text, version))
        self.state[system_id] = (text, version)


def test_automated_append_files_proposal_without_graph_or_mirror(monkeypatch):
    calls = []
    monkeypatch.setattr(
        learning, "log_proposed_mapping",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "proposal-1")
    monkeypatch.setattr(learning, "get_mapping", lambda mapping_id: {
        "mapping_id": mapping_id, "status": "pending",
        "proposed_type": "guidance_note"})
    monkeypatch.setattr(
        learning, "mirror_learned_notes_to_agent",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("automated append must not mirror")))
    monkeypatch.setattr(
        learning, "_persist_query_guidance",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("automated append must not write graph")))
    assert learning.append_query_guidance_note("system-a", "hint") == "filed"
    assert calls[0][1]["proposed_type"] == "guidance_note"


def test_backfill_files_scoped_proposals_without_delivery(monkeypatch):
    monkeypatch.setattr(learning, "get_all_query_guidance", lambda: [{
        "system_id": "system-a", "query_hints": ["hint"],
        "answer_requirements": ["required"], "legacy_unscoped": ["legacy"]}])
    filed = []

    def append(system_id, note, *, guidance_scope="query_hint", **_kwargs):
        filed.append((system_id, note, guidance_scope))
        return "filed"

    monkeypatch.setattr(learning, "append_query_guidance_note", append)
    monkeypatch.setattr(
        learning, "mirror_learned_notes_to_agent",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("backfill must not mirror")))
    assert learning.backfill_agent_guidance_mirror() == {
        "filed": 3, "not_filed": 0}
    assert filed == [
        ("system-a", "hint", "query_hint"),
        ("system-a", "required", "answer_requirement"),
        ("system-a", "legacy", "legacy_unscoped")]


def test_explicit_legacy_mirror_helper_projects_notes_only():
    backend = _LegacyBackend()
    prior = guidance._store
    guidance.set_store(guidance.SystemGuidanceStore(backend=backend))
    try:
        assert learning.mirror_learned_notes_to_agent(
            "system-a", "Base. LEARNED: legacy note") is True
        _system_id, text, version = backend.saved[-1]
        assert "legacy note" in text
        assert "Base." not in text
        assert version == 1
    finally:
        guidance.set_store(prior)


def test_mirror_is_noop_without_system_or_notes():
    assert learning.mirror_learned_notes_to_agent(
        "", "Base. LEARNED: x") is False
    assert learning.mirror_learned_notes_to_agent(
        "system-a", "Base only") is False


def test_all_systems_mirror_route_is_not_exposed():
    import inspect
    import agents.server as server
    assert '"/api/query-guidance/mirror-to-agents"' not in inspect.getsource(server)
