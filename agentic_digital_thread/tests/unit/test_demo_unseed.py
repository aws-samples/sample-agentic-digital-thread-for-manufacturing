# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for ``prompts.unseed_registry`` (S4.4b demo runtime-toggle).

Disabling the demo at runtime must physically remove the seeded
``source=='registry'`` prompts while leaving user-authored prompts untouched.
Mirrors the ``_FakePromptStore`` + ``monkeypatch.setattr(prompts, "_prompt_store",
...)`` pattern from ``test_prompts_demo_gate.py`` — no AWS, no DynamoDB.
"""

import pytest

from agents import prompts


class _FakePromptStore:
    """Tiny in-memory stand-in for PromptTemplateStorage."""

    def __init__(self):
        self._items = {}

    def list_all(self, source=None):
        if source is None:
            return list(self._items.values())
        return [v for v in self._items.values() if v.get("source") == source]

    def save(self, item):
        self._items[item["template_id"]] = item

    def delete(self, template_id):
        self._items.pop(template_id, None)

    def get(self, template_id):
        return self._items.get(template_id)


@pytest.fixture
def fake_store(monkeypatch):
    store = _FakePromptStore()
    monkeypatch.setattr(prompts, "_prompt_store", store)
    return store


def test_unseed_removes_only_registry_leaves_user(fake_store):
    fake_store.save({"template_id": "r1", "source": "registry", "text": "demo a"})
    fake_store.save({"template_id": "r2", "source": "registry", "text": "demo b"})
    fake_store.save({"template_id": "u1", "source": "user", "text": "mine"})

    prompts.unseed_registry()

    remaining = fake_store.list_all()
    assert len(remaining) == 1
    assert remaining[0]["template_id"] == "u1"
    assert remaining[0]["source"] == "user"
    assert fake_store.list_all(source="registry") == []


def test_unseed_is_noop_when_no_registry_items(fake_store):
    fake_store.save({"template_id": "u1", "source": "user", "text": "mine"})
    prompts.unseed_registry()
    # User prompt is untouched; nothing to remove.
    assert len(fake_store.list_all()) == 1
    assert fake_store.list_all(source="user")[0]["template_id"] == "u1"


def test_unseed_is_noop_when_store_is_none(monkeypatch):
    monkeypatch.setattr(prompts, "_prompt_store", None)
    # Must return safely without raising when no store is configured (offline).
    prompts.unseed_registry()
