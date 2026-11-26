# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A time window over rows in one lifecycle state is bounded on that state's own time.

Measured in an answer eval: asked for the records that FAILED in a week, the
orchestrator told the SQL agent to window on a completion timestamp, and the
agent did. Most of the failed rows had no completion time at all, so more than
half of them dropped out and the answer reported a short total as complete.
The runs that windowed on the failure timestamp got the right total.

The SQL policy tail therefore carries a STATE TIME rule: bound a state's window
on the timestamp that records that state, and when the caller named another
timestamp (or the named one may be empty for that state), also return
COUNT(*) beside COUNT(<named column>) and report the rows the named window
cannot see. Generic wording with no column names. Prompt contract only.
"""
from __future__ import annotations

import re

import pytest

import agents.system_agent as sa
from agents.connection_types import ATHENA, POSTGRESQL

_HEADER = "STATE TIME (required)"
_SQL = (ATHENA, POSTGRESQL)
_NON_SQL = ("MCP Server", "SiteWise (Direct)", "SiteWise (i3X)", "REST API",
            "Tulip (Tables API)", "")


def _rule(connection_type: str) -> str:
    tail = sa._policy_tail(connection_type)
    start = tail.index(_HEADER)
    end = tail.find("\n\n", start)
    return tail[start:end if end >= 0 else None]


@pytest.mark.parametrize("connection_type", _SQL)
def test_sql_engines_bound_a_state_window_on_that_states_own_timestamp(connection_type):
    rule = _rule(connection_type).lower()
    assert "lifecycle state" in rule
    assert "own timestamp" in rule or "timestamp that records that state" in rule
    assert "bound the window on that timestamp" in rule


@pytest.mark.parametrize("connection_type", _SQL)
def test_another_named_timestamp_gets_both_counts_and_a_disclosure(connection_type):
    rule = _rule(connection_type)
    low = rule.lower()
    assert "caller named another timestamp" in low
    assert "null" in low
    assert "COUNT(*)" in rule and "COUNT(<named column>)" in rule
    assert "cannot see" in low and "report" in low


@pytest.mark.parametrize("connection_type", _SQL)
def test_the_rule_names_no_column_and_stays_plain(connection_type):
    rule = _rule(connection_type)
    assert not re.search(r"\b[a-z]+_[a-z_]+\b", rule)   # no physical column names
    assert "\u2014" not in rule and "\u2013" not in rule
    assert len(rule) <= 650


@pytest.mark.parametrize("connection_type", _SQL)
def test_it_rides_with_the_date_filter_rule(connection_type):
    tail = sa._policy_tail(connection_type)
    assert tail.index("DATE FILTERS (required)") < tail.index(_HEADER) < tail.index(
        "SUBQUERY PROJECTION (required)")


@pytest.mark.parametrize("connection_type", _NON_SQL)
def test_non_sql_connection_types_do_not_get_it(connection_type):
    assert _HEADER not in sa._policy_tail(connection_type)
