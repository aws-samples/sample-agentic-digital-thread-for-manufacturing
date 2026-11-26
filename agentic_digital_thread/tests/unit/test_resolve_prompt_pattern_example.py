# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The chat resolve prompt's pattern example can be stored as a rule.

The chat onboarding agent's resolve prompt (``onboard_agent._build_resolve_prompt``)
once suggested glob pairs such as ``LINE* -> *-L*``. Those name no
``{placeholder}``, so the one check every IdentityRule writer uses
(``resolve.identity_pattern_problem``) refuses them: a model that copied the
example wrote a rule that could never be stored. Every example pair the prompt
offers must pass that check and translate a value through the runtime reader.
"""
from __future__ import annotations

import re

from agents import onboard_agent as oa
from agents.identifier_translation import apply
from agents.resolve import identity_pattern_problem

# A pattern token: no space, quote or bracket, holding a slot ('{') or a glob ('*').
_TOKEN = r"['\"]?([^\s'\"(),;]*[*{][^\s'\"(),;]*)['\"]?"
_PAIR = re.compile(_TOKEN + r"\s+(?:→|->|to)\s+" + _TOKEN)
_SLOT = re.compile(r"\{(\w+)\}")


def _example_pairs(prompt: str) -> list[tuple[str, str]]:
    return [(a.rstrip(".,"), b.rstrip(".,")) for a, b in _PAIR.findall(prompt)]


def _prompt() -> str:
    return oa._build_resolve_prompt("plant-x-hub", "m", "e", "s", "t")


def test_every_pattern_example_in_the_chat_resolve_prompt_is_storable():
    pairs = _example_pairs(_prompt())
    assert pairs, "the prompt offers no pattern example pair"
    for source, target in pairs:
        assert identity_pattern_problem(source, target) is None, (source, target)


def test_every_pattern_example_translates_a_value_it_describes():
    for source, target in _example_pairs(_prompt()):
        value = _SLOT.sub("7", source)
        assert apply(source, target, value) == _SLOT.sub("7", target), (source, target)
