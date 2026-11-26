# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A quoted code is counted against its stored form, not a near miss.

Measured on an analytics deployment: asked for a daily count of one quoted
defect code, the orchestrator's second delegation asked for records whose
code is "exactly" the quoted text, "not a broader LIKE match". The SQL agent
ran equality on the bare code against two columns, found 0 and 1 rows, and
reported them. The code is stored with a station prefix, thousands of times
a day, in another column. No system-agent rule covered a count that matched a
handful of rows (IDENTIFIER-MISS covers a match of nothing), and the
orchestrator had decided the match operator itself.

* System agent (SQL engines): before reporting a zero or small count for a
  quoted code or value, or a match found only outside the concept-linked
  column, read how the value is stored (a case-insensitive LIKE on the quoted
  text, grouped by stored value, under the same filters, bounded) across the
  candidate columns, and report against the stored form.
* Orchestrator: pass a quoted code as the user wrote it and leave the match
  operator to the system agent.
"""
from __future__ import annotations

import re

import pytest

import agents.orchestrator as orch
import agents.system_agent as sa
from agents.workflow_guidance import compose_quick_system_prompt

MODES = ("agent", "orchestrator")


def _compose(connection_type: str) -> str:
    return sa._compose_system_prompt(
        system_id="sys-a", system_name="System A", schema="public", system_prompt="",
        tables_desc="", connection_type=connection_type)


def _system_rule() -> str:
    fn = getattr(sa, "_quoted_value_prompt", None)
    return fn(sa.ATHENA) if fn else "<missing _quoted_value_prompt>"


@pytest.mark.parametrize("connection_type", [sa.ATHENA, "PostgreSQL"])
def test_sql_agents_read_the_stored_form_before_reporting_a_near_miss(connection_type, monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    prompt = _compose(connection_type)
    rule = sa._quoted_value_prompt(connection_type)

    assert rule and rule in prompt
    low = " ".join(rule.split()).lower()
    for part in ("quoted", "zero rows", "fewer than 10", "strpos", "group by", "stored form",
                 "concept"):
        assert part in low, part
    # It comes before the IDENTIFIER-MISS protocol, which is unchanged.
    assert prompt.index(rule) < prompt.index("IDENTIFIER-MISS (required protocol)")
    assert prompt.endswith(sa._identifier_miss_prompt())


def test_the_stored_form_read_is_a_literal_substring_test_not_a_pattern():
    """A code holding an underscore or a percent sign is a literal: a LIKE
    pattern would read those characters as wildcards and match other codes."""
    rule = _system_rule()
    low = " ".join(rule.split()).lower()

    assert "strpos(lower(<column>), lower('<quoted text>')) > 0" in low
    assert "single quote" in low and "doubled" in low
    assert re.search(r"\blike\b", low) is None


def test_the_read_replaces_only_the_equality_predicate():
    low = " ".join(_system_rule().split()).lower()

    assert "in place of the equality predicate on the quoted value" in low
    assert "every other filter of the request stays" in low


def test_the_rule_covers_a_match_outside_the_concept_linked_column():
    low = " ".join(_system_rule().split()).lower()
    assert "only in a column other than" in low
    assert "linked" in low


def test_a_longer_code_is_never_folded_into_the_count():
    low = " ".join(_system_rule().split()).lower()
    assert "separate" in low and "never add" in low


def test_engines_without_sql_get_no_such_rule():
    assert getattr(sa, "_quoted_value_prompt")(sa.ATHENA)
    for connection_type in ("MCP", "SiteWise", "REST"):
        assert sa._quoted_value_prompt(connection_type) == ""


def _orch_rule() -> str:
    return getattr(orch, "_QUOTED_VALUE_RULE", "<missing _QUOTED_VALUE_RULE>")


@pytest.mark.parametrize("kind", ("standing", "quick"))
@pytest.mark.parametrize("mode", MODES)
def test_every_orchestrator_prompt_carries_the_rule_after_the_field_values_rule(kind, mode):
    prompt = (compose_quick_system_prompt(mode) if kind == "quick"
              else orch._build_orchestrator_prompt(mode))

    assert (orch._FIELD_VALUES_RULE + _orch_rule()) in prompt
    assert prompt.count(_orch_rule()) == 1


def test_the_orchestrator_leaves_the_match_operator_to_the_system_agent():
    low = " ".join(_orch_rule().split()).lower()
    assert "as the user wrote it" in low
    assert "system agent" in low
    never = low[low.index("never"):]
    for part in ("exact match", "like"):
        assert part in never, part


def test_both_rules_are_short_and_use_no_em_or_en_dashes():
    for rule in (_system_rule(), _orch_rule()):
        assert not rule.startswith("<missing"), rule
        assert "\u2014" not in rule and "\u2013" not in rule
    assert len(_system_rule()) <= 1200, len(_system_rule())
    assert len(_orch_rule()) <= 450, len(_orch_rule())
