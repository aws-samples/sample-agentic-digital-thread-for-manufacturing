# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""System agents write times in the same fixed UTC form as the answer.

A system agent's answer reaches the user in the investigation trace and the
tool-result markdown, and the orchestrator quotes from it. Its context line
has no user zone, and that line only said to keep a handed time as given, so
it could write ``14:17 UTC`` with no date or split date and time, which the
page leaves unconverted. The policy tail (every connection type, after any
custom registration prompt) now asks for ``YYYY-MM-DD HH:MM UTC`` with its
date, and never says a screen converts it: Quick shows text as written.

Fully offline (no Bedrock, no AWS, no sockets).
"""
from __future__ import annotations

import pytest

import agents.system_agent as sa
from agents.connection_types import TULIP
from agents.time_context import render_time_context

_TYPES = ["PostgreSQL", "Athena", "SiteWise (Direct)", "SiteWise (i3X)", TULIP,
          "REST API", "MCP Server", ""]
_RULE = ("TIMES IN YOUR ANSWER (required): write every time you report in UTC as "
         "YYYY-MM-DD HH:MM UTC (seconds only when the record has them), always with its date "
         "(each end of a range written in full, with its own date and UTC), "
         "and never convert it to another zone. A time a record stores with a different "
         "offset is given as stored, with that offset.")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _compose(connection_type: str, custom: str = "") -> str:
    return sa._compose_system_prompt("sys-a", "System A", "public", custom, "", connection_type)


@pytest.mark.parametrize("connection_type", _TYPES)
def test_composed_prompt_carries_the_answer_form_once(monkeypatch, connection_type):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    flat = _flat(_compose(connection_type))
    assert flat.count(_RULE) == 1, connection_type
    assert _RULE in _flat(sa._policy_tail(connection_type))


@pytest.mark.parametrize("connection_type", _TYPES)
def test_a_custom_registration_prompt_cannot_displace_it(monkeypatch, connection_type):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    custom = "Custom registration prompt. Write times however you like."
    flat = _flat(_compose(connection_type, custom))
    assert flat.index(custom) < flat.index(_RULE)


def test_neither_the_rule_nor_the_no_zone_line_says_a_screen_converts():
    for text in (sa._answer_times_prompt(), render_time_context()):
        assert "screen" not in text.lower()
        assert "local zone" not in text.lower()
