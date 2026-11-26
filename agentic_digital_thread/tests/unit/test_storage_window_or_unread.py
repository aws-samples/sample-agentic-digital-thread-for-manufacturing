# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The window rules' OR-of-ranges read stays out of the storage-window rule.

The state-time and record-own-time rules read a date window written as an OR
of ranges on one column (``test_state_time_window_or``). The storage-window
rule (``storage_bound_rule.decide_window``) runs in the same guarded Mode A
check and must keep reading bounds exactly as before: a window written ONLY as
an OR of ranges on a temporal storage column is not read by it, so it is not
refused, and the single-range form still is.

Fully offline; synthetic names.
"""
from __future__ import annotations

from agents import storage_bound_rule

from .test_storage_bound_refusal import _Lake
from .test_storage_window_refusal import _STORAGE_ONLY, _ask, _fresh  # noqa: F401

_R1 = ("created >= TIMESTAMP '2026-01-05 00:00:00' "
       "AND created < TIMESTAMP '2026-01-06 00:00:00'")
_R2 = ("created >= TIMESTAMP '2026-01-12 00:00:00' "
       "AND created < TIMESTAMP '2026-01-13 00:00:00'")
_STORAGE_OR_ONLY = (f"SELECT state, COUNT(*) AS n FROM events WHERE (({_R1}) OR ({_R2})) "
                    "GROUP BY state")


def test_an_or_only_storage_window_is_not_read_by_the_storage_window_rule(monkeypatch):
    lake = _Lake()
    payload, = _ask(monkeypatch, lake, [_STORAGE_OR_ONLY])
    assert payload.get("reason") != storage_bound_rule.REASON_WINDOW_ON_STORAGE, payload
    assert "error" not in payload, payload
    assert lake.data_queries()


def test_the_single_range_storage_window_is_still_refused(monkeypatch):
    lake = _Lake()
    payload, = _ask(monkeypatch, lake, [_STORAGE_ONLY])
    assert payload["reason"] == storage_bound_rule.REASON_WINDOW_ON_STORAGE
    assert lake.data_queries() == []
