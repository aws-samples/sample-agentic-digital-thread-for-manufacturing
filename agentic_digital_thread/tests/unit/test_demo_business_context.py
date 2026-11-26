# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Demo/workshop business context is CONTENT in agents/demo/ (ADR-0044)."""
import re

import pytest

from agents.business_context import (MAX_BUSINESS_CONTEXT_LEN,
                                     sanitize_business_context,
                                     validate_business_context)
from agents.demo.business_context import (DEMO_BUSINESS_CONTEXT,
                                          demo_business_context,
                                          demo_business_context_systems)
from agents.demo.systems import DEMO_SYSTEMS


def test_covers_every_demo_sql_system():
    assert set(demo_business_context_systems()) == {s["id"] for s in DEMO_SYSTEMS}


@pytest.mark.parametrize("system_id", sorted(DEMO_BUSINESS_CONTEXT))
def test_each_text_passes_the_real_validator(system_id):
    """Seeded content must clear the SAME gate a steward's text clears — a demo
    that seeds text the product would reject is not demonstrating the product."""
    text = sanitize_business_context(DEMO_BUSINESS_CONTEXT[system_id])
    assert validate_business_context(text) is None
    assert len(text) <= MAX_BUSINESS_CONTEXT_LEN


@pytest.mark.parametrize("system_id", sorted(DEMO_BUSINESS_CONTEXT))
def test_each_text_answers_the_intake_questions(system_id):
    """Content is judged against docs/domain-context-intake.md, not vibes:
    it must state a unit of work, an authority boundary, and a limit."""
    text = DEMO_BUSINESS_CONTEXT[system_id].lower()
    assert "unit of work" in text
    assert "authoritative" in text
    assert re.search(r"\bnot\b", text)


def test_no_credentials_or_secret_shapes():
    for text in DEMO_BUSINESS_CONTEXT.values():
        assert validate_business_context(sanitize_business_context(text)) is None


def test_unknown_system_returns_empty_not_none():
    assert demo_business_context("nope") == ""


def test_mes_systems_share_one_meaning():
    """Three plants, one semantic model — divergent copies would rot."""
    texts = {demo_business_context(s) for s in ("mes-mil", "mes-det", "mes-cr")}
    assert len(texts) == 1
