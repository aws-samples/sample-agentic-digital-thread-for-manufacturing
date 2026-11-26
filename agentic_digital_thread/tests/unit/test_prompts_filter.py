# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

def test_connection_type_tokens_satisfy_required_systems(monkeypatch):
    """A prompt requiring 'SiteWise' matches a system whose connection_type is
    'SiteWise (Direct)' even when its system_type is a role label (Historian).
    Regression: seeded OT prompts vanished when the type label diverged."""
    from agents import prompts as prompts_mod

    class _Cat:
        def get_onboarded(self):
            return [
                {"id": "lake-athena", "system_type": "MES", "connection_type": "Athena", "status": "onboarded"},
                {"id": "telem-sitewise", "system_type": "Historian", "connection_type": "SiteWise (Direct)", "status": "onboarded"},
            ]

    monkeypatch.setattr("agents.catalog.catalog", _Cat())
    items = [
        {"title": "ok-both", "required_systems": ["MES", "SiteWise"]},
        {"title": "ok-id", "required_systems": ["lake-athena"]},
        {"title": "unsat", "required_systems": ["UNS"]},
    ]
    kept = {i["title"] for i in prompts_mod._filter_satisfiable(items)}
    assert kept == {"ok-both", "ok-id"}
