# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Scan-limit guidance never says "add a LIMIT" without saying what a LIMIT bounds.

Measured in an answer eval: after an Athena query was stopped for reading too
much data, the guidance the agent got said to aggregate in SQL "and add a
LIMIT". A LIMIT cuts only the rows a query returns; an aggregate still scans
everything its WHERE clause lets through, so the advice does not shrink the
scan. It pushed the agent toward a LIMIT sample instead, the method that left a
mid-sized identifier family out of a format answer. Every scan-limit message
now says to narrow the question's own time window (never to add a partition
bound the question did not ask for) and that a LIMIT bounds only the
rows returned. Offline: fakes only.
"""
from __future__ import annotations

import re

import pytest

from agents import athena_tools as at


class _StoppingAthena:
    def stop_query_execution(self, QueryExecutionId):  # noqa: N803 - boto3 shape
        return {}


def _budget_error() -> at.AthenaScanBudgetError:
    with pytest.raises(at.AthenaScanBudgetError) as exc:
        at._raise_scan_budget(_StoppingAthena(), "q1", 5000, 1000)
    return exc.value


def _partition_hint(monkeypatch, columns) -> str:
    monkeypatch.setattr(at, "_referenced_tables", lambda _sql: ["table_x"])
    monkeypatch.setattr(at, "_introspect_columns", lambda *_a: columns)
    return at._partition_hint_from_tables(object(), {}, "SELECT col_a FROM table_x")


def _texts(monkeypatch) -> dict[str, str]:
    error = _budget_error()
    return {
        "engine scan limit": at._ENGINE_SCAN_LIMIT_GUIDANCE,
        "app budget message": str(error),
        "app budget guidance": " ".join(error.guidance),
        "partition hint, known columns": _partition_hint(
            monkeypatch, [("col_d", "string", "partition key"), ("col_a", "string", "")]),
        "partition hint, generic": _partition_hint(monkeypatch, []),
    }


_UNCONDITIONAL = re.compile(r"\b(?:add|and|put) a LIMIT\b", re.I)


def test_no_scan_limit_text_says_add_a_limit(monkeypatch):
    for name, text in _texts(monkeypatch).items():
        assert not _UNCONDITIONAL.search(text), (name, text)


def test_each_text_that_names_a_limit_says_it_bounds_only_the_rows_returned(monkeypatch):
    for name, text in _texts(monkeypatch).items():
        if "limit" in text.lower().replace("scan limit", ""):
            assert "LIMIT bounds only the rows returned" in text, (name, text)


def test_the_guidance_says_to_narrow_the_questions_window(monkeypatch):
    texts = _texts(monkeypatch)
    assert "time column the question asks about" in texts["engine scan limit"]
    assert "not what an aggregate scans" in texts["engine scan limit"]
    assert "narrow the time range" in texts["app budget guidance"].lower()
    assert "not what an aggregate scans" in texts["app budget guidance"]
    for name in ("partition hint, known columns", "partition hint, generic"):
        assert "WHERE filter on the time column the question asks about" in texts[name], name
    assert "col_d" in texts["partition hint, known columns"]


def test_the_app_budget_path_still_gives_no_partition_advice(monkeypatch):
    texts = _texts(monkeypatch)
    for name in ("app budget message", "app budget guidance"):
        assert "partition" not in texts[name].lower(), name


def test_the_new_wording_uses_no_em_or_en_dashes(monkeypatch):
    narrow = next(item for item in _budget_error().guidance if "Narrow the time range" in item)
    for text in (at._ENGINE_SCAN_LIMIT_GUIDANCE, narrow):
        assert "\u2014" not in text and "\u2013" not in text, text
