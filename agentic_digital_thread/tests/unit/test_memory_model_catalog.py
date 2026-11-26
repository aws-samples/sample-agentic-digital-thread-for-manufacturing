# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The memory extraction models in the model catalog (the one home).

The Admin LLM settings page offers a third model, the one AgentCore Memory
uses to extract Facts, Summaries and Preferences. The selectable set is the
five models AgentCore Memory accepted as a custom-strategy model in the
2026-10-26 us-east-1 comparison, Claude Haiku 5.5 first and the default; no
Mantle model (AgentCore Memory does not serve them). ``catalog()`` adds a
``memory`` list; the orchestrator and agent lists stay as they were.
"""
import pytest

from agents import model_catalog as mc
from agents.model_catalog import AGENT, ORCHESTRATOR

HAIKU_55 = "anthropic.claude-haiku-5-5"
SONNET_55 = "anthropic.claude-sonnet-5-5"
NOVA_PRO = "amazon.nova-pro-v1:0"
SONNET_5 = "anthropic.claude-sonnet-5"
HAIKU_45 = "anthropic.claude-haiku-4-5-20251001-v1:0"
SONNET_46 = "anthropic.claude-sonnet-4-6"

EXPECTED = (HAIKU_55, SONNET_55, NOVA_PRO, SONNET_5, HAIKU_45)


@pytest.fixture(autouse=True)
def _no_allowlist(monkeypatch):
    monkeypatch.delenv(mc.ALLOWED_MODELS_ENV, raising=False)


def test_memory_families_are_the_five_accepted_models_in_order():
    assert mc.MEMORY_EXTRACTION_FAMILIES == EXPECTED
    assert mc.MEMORY_DEFAULT_FAMILY == HAIKU_55
    assert mc.MEMORY == "memory"


def test_every_memory_family_is_a_catalog_model_and_none_is_mantle():
    by_family = {m.family: m for m in mc.MODELS}
    for family in mc.MEMORY_EXTRACTION_FAMILIES:
        assert family in by_family, family
        assert not by_family[family].mantle, family


def test_memory_families_without_an_allowlist_is_every_memory_family():
    assert mc.memory_families(None) == frozenset(EXPECTED)


def test_memory_families_intersect_the_allowlist():
    assert mc.memory_families(frozenset({SONNET_5, HAIKU_45})) == frozenset({SONNET_5, HAIKU_45})
    assert mc.memory_families(frozenset({SONNET_46})) == frozenset()


@pytest.mark.parametrize("model_id, allowed, expected", [
    (f"global.{HAIKU_55}", None, True),
    (f"us.{NOVA_PRO}", None, True),
    (NOVA_PRO, None, True),
    (f"eu.{NOVA_PRO}", None, True),
    (f"global.{SONNET_46}", None, False),          # a catalog model, not a memory one
    ("openai.gpt-5.6-luna", None, False),          # Mantle
    ("made.up-model", None, False),
    ("", None, False),
    (f"global.{SONNET_5}", frozenset({SONNET_5, HAIKU_45}), True),
    (f"global.{HAIKU_55}", frozenset({SONNET_5, HAIKU_45}), False),
])
def test_is_memory_model_allowed(model_id, allowed, expected):
    assert mc.is_memory_model_allowed(model_id, allowed) is expected


def test_catalog_adds_a_memory_list_of_the_memory_families():
    cat = mc.catalog("us-east-1")
    assert [e["family"] for e in cat[mc.MEMORY]] == list(EXPECTED)
    by_family = {e["family"]: e for e in cat[mc.MEMORY]}
    assert by_family[HAIKU_55]["id"] == f"global.{HAIKU_55}"
    assert by_family[NOVA_PRO]["id"] == f"us.{NOVA_PRO}"
    assert [e["family"] for e in cat[mc.MEMORY] if e["default"]] == [HAIKU_55]
    # Same entry fields as the other lists, and no allowlist flag without one.
    assert set(cat[mc.MEMORY][0]) == set(cat[ORCHESTRATOR][0])
    assert "allowed" not in cat[mc.MEMORY][0]


def test_catalog_memory_ids_are_region_resolved():
    by_family = {e["family"]: e for e in mc.catalog("eu-west-1")[mc.MEMORY]}
    assert by_family[NOVA_PRO]["id"] == f"eu.{NOVA_PRO}"
    assert by_family[HAIKU_55]["id"] == f"global.{HAIKU_55}"


def test_catalog_memory_entries_carry_availability():
    available = {f"global.{HAIKU_55}"}
    by_family = {e["family"]: e for e in mc.catalog("us-east-1", available)[mc.MEMORY]}
    assert by_family[HAIKU_55]["available"] is True
    assert by_family[NOVA_PRO]["available"] is False


def test_catalog_memory_entries_under_an_allowlist(monkeypatch):
    monkeypatch.setenv(mc.ALLOWED_MODELS_ENV, f"{SONNET_5},{HAIKU_45}")
    cat = mc.catalog("us-east-1")
    allowed = {e["family"] for e in cat[mc.MEMORY] if e["allowed"]}
    assert allowed == {SONNET_5, HAIKU_45}
    # Every memory model is still listed (greyed out, never hidden), and the
    # default marker stays on the deploy default.
    assert len(cat[mc.MEMORY]) == len(EXPECTED)
    assert [e["family"] for e in cat[mc.MEMORY] if e["default"]] == [HAIKU_55]


def test_the_role_lists_are_unchanged_by_the_memory_list():
    cat = mc.catalog("us-east-1")
    for role in (ORCHESTRATOR, AGENT):
        assert [e["family"] for e in cat[role]] == [m.family for m in mc.MODELS]
    assert set(cat) == {ORCHESTRATOR, AGENT, mc.MEMORY, "region"}


# -- the saved memory model as the next deploy resolves it (one helper) ---------------

@pytest.mark.parametrize("value, region, allowed, expected", [
    (f"us.{NOVA_PRO}", "eu-west-1", None, f"eu.{NOVA_PRO}"),
    (NOVA_PRO, "ap-southeast-2", None, f"apac.{NOVA_PRO}"),
    (f"us.{HAIKU_55}", "us-east-1", None, f"global.{HAIKU_55}"),
    (f" global.{SONNET_5} ", "eu-west-1", None, f"global.{SONNET_5}"),
    (f"global.{SONNET_46}", "us-east-1", None, ""),
    ("openai.gpt-5.6-luna", "us-east-1", None, ""),
    ("made.up-model", "us-east-1", None, ""),
    ("", "us-east-1", None, ""),
    (f"global.{HAIKU_55}", "us-east-1", frozenset({SONNET_5, HAIKU_45}), ""),
    (f"global.{SONNET_5}", "us-east-1", frozenset({SONNET_5, HAIKU_45}), f"global.{SONNET_5}"),
])
def test_effective_memory_model(value, region, allowed, expected):
    assert mc.effective_memory_model(value, region, allowed) == expected


def test_effective_memory_model_is_valid_exactly_when_is_memory_model_allowed():
    spellings = [p + m.family for m in mc.MODELS for p in ("", "global.", "us.", "eu.")]
    for allowed in (None, frozenset({SONNET_5, HAIKU_45}), frozenset({SONNET_46})):
        for value in spellings + ["", "made.up-model"]:
            assert (bool(mc.effective_memory_model(value, "us-east-1", allowed))
                    is mc.is_memory_model_allowed(value, allowed)), (value, allowed)
