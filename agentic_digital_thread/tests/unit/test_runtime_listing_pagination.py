# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pagination of AgentCore runtime listings (>10 runtimes regression).

``list_agent_runtimes`` returns one page (10 items observed) plus a
``nextToken``. Raw calls silently truncated once the account crossed 10
runtimes: the 11th runtime pushed an existing one (adt_cmms, live incident)
onto page 2, ``_runtime_is_ready`` reported it ABSENT, and the startup fast
path "repaired" a phantom dangling target by deleting/recreating that runtime
on EVERY ECS task start — aborting the catalog restore loop before the
direct-source sweep. These tests pin the ONE sanctioned enumerator
(:func:`runtime_provision_helpers.list_all_agent_runtimes`) and the fast-path
check that regressed.
"""
from __future__ import annotations

import pytest

from agents.runtime_provision_helpers import (
    RuntimeListingCapped,
    iter_agent_runtime_pages,
    list_all_agent_runtimes,
)


class _PagedControl:
    """Fake control-plane client returning runtimes in pages of ``page_size``."""

    def __init__(self, names, page_size=10, key="agentRuntimes"):
        self._items = [{"agentRuntimeName": n, "status": "READY",
                        "agentRuntimeId": f"id-{n}",
                        "agentRuntimeArn": f"arn:aws:x/{n}"} for n in names]
        self._page = page_size
        self._key = key
        self.calls = 0
        self.kwargs: list[dict] = []

    def list_agent_runtimes(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        start = int(kwargs.get("nextToken") or 0)
        page = self._items[start:start + self._page]
        resp = {self._key: page}
        if start + self._page < len(self._items):
            resp["nextToken"] = str(start + self._page)
        return resp


def test_all_pages_collected_beyond_ten():
    names = [f"adt_sys_{i:02d}" for i in range(23)]
    control = _PagedControl(names)
    got = [r["agentRuntimeName"] for r in list_all_agent_runtimes(control)]
    assert got == names          # every page, original order
    assert control.calls == 3    # 10 + 10 + 3


def test_single_page_no_token_one_call():
    control = _PagedControl(["adt_a", "adt_b"])
    assert len(list_all_agent_runtimes(control)) == 2
    assert control.calls == 1


def test_list_all_sends_no_max_results():
    """The bounded pager added beside it leaves every existing caller's
    request unchanged: no maxResults, nextToken only after page 1."""
    control = _PagedControl([f"adt_{i}" for i in range(12)])
    list_all_agent_runtimes(control)
    assert control.kwargs == [{}, {"nextToken": "10"}]


def test_iter_pages_sends_max_results_and_yields_each_page():
    control = _PagedControl([f"adt_{i}" for i in range(5)], page_size=2)
    pages = list(iter_agent_runtime_pages(control, max_results=100))
    assert [[r["agentRuntimeName"] for r in p] for p in pages] == [
        ["adt_0", "adt_1"], ["adt_2", "adt_3"], ["adt_4"]]
    assert control.kwargs == [{"maxResults": 100},
                              {"maxResults": 100, "nextToken": "2"},
                              {"maxResults": 100, "nextToken": "4"}]


def test_iter_pages_calls_only_when_the_next_page_is_asked_for():
    control = _PagedControl([f"adt_{i}" for i in range(30)])
    pages = iter_agent_runtime_pages(control)
    next(pages)
    pages.close()
    assert control.calls == 1


def test_iter_pages_cap_raises_instead_of_another_call():
    control = _PagedControl([f"adt_{i}" for i in range(31)])   # 4 pages of 10
    pages = iter_agent_runtime_pages(control, max_pages=3)
    assert len([next(pages) for _ in range(3)]) == 3
    with pytest.raises(RuntimeListingCapped, match="3 pages"):
        next(pages)
    assert control.calls == 3


def test_iter_pages_cap_reached_exactly_by_the_last_page_is_complete():
    control = _PagedControl([f"adt_{i}" for i in range(30)])   # 3 pages of 10
    pages = list(iter_agent_runtime_pages(control, max_pages=3))
    assert sum(len(p) for p in pages) == 30
    assert control.calls == 3


def test_summaries_key_normalized():
    control = _PagedControl(["adt_a"], key="agentRuntimeSummaries")
    assert [r["agentRuntimeName"] for r in list_all_agent_runtimes(control)] == ["adt_a"]


def test_runtime_is_ready_finds_page_two_runtime():
    """The live incident: the 11th runtime pushed adt_cmms to page 2 and the
    fast path declared it absent. Pin that _runtime_is_ready sees page 2."""
    from agents.agent_manager import AgentManager

    names = [f"adt_filler_{i}" for i in range(10)] + ["adt_cmms"]
    control = _PagedControl(names)
    mgr = AgentManager.__new__(AgentManager)  # no __init__ side effects needed
    assert mgr._runtime_is_ready(control, "cmms") is True
    assert control.calls >= 2  # required page 2 to find it
