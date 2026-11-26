# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A quantity keeps its own row's unit; a current balance says nothing about earlier.

Two system-agent prompt rules, generic, checked on a schema named for nothing
in the product (a coil store whose stock table holds the mass left and its
unit, and whose draw table holds each dated draw):

1. ROW FIGURES (every connection type): each quantity is reported with the
   unit its own row returned; never one from another row, the schema's
   examples or a usual unit; with no unit returned the number goes alone and
   the answer says so.
2. CURRENT BALANCES (SQL engines): a column holding a current amount or state
   tells only what it is now. Before, after and since claims (what was left
   then, whether anything was drawn after a time) come from dated movement,
   usage or history rows.

Fully offline.
"""
from __future__ import annotations

import re

import pytest

import agents.system_agent as sa

_ALL_TYPES = ("PostgreSQL", "Athena", "SiteWise (Direct)", "SiteWise (i3X)", "MCP Server",
              "REST API", "Tulip (Tables API)", "")
_SQL = ("PostgreSQL", "Athena")
_NON_SQL = tuple(t for t in _ALL_TYPES if t not in _SQL)
_COIL_SCHEMA = ("coil_stock(coil_ref: text, mass_left: numeric, mass_uom: text). "
                "coil_draws(coil_ref: text, drawn_at: timestamp, mass_drawn: numeric, "
                "mass_uom: text)")
_BALANCE_HEADER = "CURRENT BALANCES (required)"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _coil_prompt(connection_type: str) -> str:
    return _flat(sa._compose_system_prompt(
        "coil-store", "Coil store", "store", "", _COIL_SCHEMA, connection_type))


# --------------------------------------------------------------------------- #
# 1. Units stay with their own row
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("connection_type", _ALL_TYPES)
@pytest.mark.parametrize("fragment", [
    "A quantity's unit is such a value",
    "give each quantity with the unit its own row returned",
    "never one from another row, the examples or a usual unit",
    "if no row has it, give the number alone and say the unit was not returned",
])
def test_every_quantity_keeps_its_own_rows_unit(connection_type, fragment):
    assert fragment in _flat(sa._row_figures_prompt(connection_type)), connection_type
    assert fragment in _coil_prompt(connection_type), connection_type


@pytest.mark.parametrize("connection_type", _ALL_TYPES)
def test_the_unit_clause_names_no_column_and_grants_no_extra_query(connection_type):
    rule = _flat(sa._row_figures_prompt(connection_type))
    clause = rule[rule.index("A quantity's unit"):]
    assert not re.search(r"\b[a-z]+_[a-z_]+\b", clause)
    assert "query for it" not in rule
    assert "\u2014" not in clause and "\u2013" not in clause


# --------------------------------------------------------------------------- #
# 2. A current balance is not a history
# --------------------------------------------------------------------------- #
def _balance_rule(connection_type: str) -> str:
    tail = sa._policy_tail(connection_type)
    start = tail.index(_BALANCE_HEADER)
    end = tail.find("\n\n", start)
    return tail[start:end if end >= 0 else None]


@pytest.mark.parametrize("connection_type", _SQL)
def test_sql_engines_read_before_and_after_from_dated_rows(connection_type):
    rule = _flat(_balance_rule(connection_type))
    assert "tells only what it is now" in rule
    assert "not what it was at an earlier time" in rule
    assert "whether anything was used, drawn or changed after a given time" in rule
    assert "must come from dated movement, usage or history rows" in rule
    assert "the record holds only the current value" in rule
    assert _coil_prompt(connection_type).count(_BALANCE_HEADER) == 1


@pytest.mark.parametrize("connection_type", _SQL)
def test_the_balance_rule_sits_with_the_state_time_rule(connection_type):
    tail = sa._policy_tail(connection_type)
    assert (tail.index("STATE TIME (required)") < tail.index(_BALANCE_HEADER)
            < tail.index("SUBQUERY PROJECTION (required)"))


@pytest.mark.parametrize("connection_type", _SQL)
def test_the_balance_rule_names_no_column_and_stays_plain(connection_type):
    rule = _balance_rule(connection_type)
    assert not re.search(r"\b[a-z]+_[a-z_]+\b", rule)
    assert not re.search(r"\d", rule)
    assert "\u2014" not in rule and "\u2013" not in rule
    assert len(rule) <= 650


@pytest.mark.parametrize("connection_type", _NON_SQL)
def test_non_sql_types_do_not_get_the_balance_rule(connection_type):
    assert _BALANCE_HEADER not in sa._policy_tail(connection_type)
