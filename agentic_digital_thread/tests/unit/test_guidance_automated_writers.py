# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Automated guidance producers may file proposals, never publish guidance."""
from __future__ import annotations

import agents.self_learning as learning


def test_append_files_pending_query_hint_without_graph_or_delivery_write(monkeypatch):
    calls = []

    def file_proposal(*args, **kwargs):
        calls.append((args, kwargs))
        return "proposal-1"

    monkeypatch.setattr(learning, "log_proposed_mapping", file_proposal)
    monkeypatch.setattr(learning, "get_mapping", lambda mapping_id: {
        "mapping_id": mapping_id, "status": "pending",
        "proposed_type": "guidance_note"})
    monkeypatch.setattr(learning, "_neptune_query",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            AssertionError("append must not read graph")))
    monkeypatch.setattr(learning, "_persist_query_guidance",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            AssertionError("append must not write graph")))
    monkeypatch.setattr(learning, "mirror_learned_notes_to_agent",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            AssertionError("append must not deliver")))

    assert learning.append_query_guidance_note("system-a", "new hint") == "filed"
    assert len(calls) == 1
    _args, kwargs = calls[0]
    assert kwargs["proposed_type"] == "guidance_note"
    assert kwargs["guidance_scope"] == "query_hint"
    assert kwargs["source"] == "agent"
    assert kwargs["to_value"] == "new hint"


def test_append_returns_not_filed_and_never_claims_success(monkeypatch):
    monkeypatch.setattr(learning, "log_proposed_mapping", lambda *a, **k: None)
    assert learning.append_query_guidance_note("system-a", "new hint") == "not_filed"
    assert learning.append_query_guidance_note("", "new hint") == "not_filed"
    assert learning.append_query_guidance_note("system-a", " ") == "not_filed"



def test_append_never_reports_filed_without_pending_readback(monkeypatch):
    monkeypatch.setattr(
        learning, "log_proposed_mapping", lambda *args, **kwargs: "proposal-1")
    monkeypatch.setattr(learning, "get_mapping", lambda _mapping_id: {
        "status": "accepted", "proposed_type": "guidance_note"})
    assert learning.append_query_guidance_note(
        "system-a", "new hint") == "not_filed"

def test_backfill_files_each_legacy_note_as_pending_without_mirroring(monkeypatch):
    monkeypatch.setattr(learning, "get_all_query_guidance", lambda: [
        {"system_id": "system-a", "legacy_unscoped": ["one", "two"],
         "query_hints": ["hint"], "answer_requirements": ["required"]},
        {"system_id": "system-b", "legacy_unscoped": []},
    ])
    filed = []

    def append(system_id, note, *, guidance_scope="query_hint", **_kwargs):
        filed.append((system_id, note, guidance_scope))
        return "filed" if note != "two" else "not_filed"

    monkeypatch.setattr(learning, "append_query_guidance_note", append)
    monkeypatch.setattr(learning, "mirror_learned_notes_to_agent",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            AssertionError("backfill must not deliver")))

    result = learning.backfill_agent_guidance_mirror()

    assert result == {"filed": 3, "not_filed": 1}
    assert filed == [
        ("system-a", "hint", "query_hint"),
        ("system-a", "required", "answer_requirement"),
        ("system-a", "one", "legacy_unscoped"),
        ("system-a", "two", "legacy_unscoped"),
    ]
