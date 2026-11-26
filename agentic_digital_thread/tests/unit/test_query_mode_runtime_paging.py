# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The query-mode scripts read EVERY ``list_agent_runtimes`` page.

One call returns a single page plus a ``nextToken``; an account with more
runtimes than one page holds made ``runtime_id_by_name`` report a page-2
runtime absent and ``set_query_mode.py --all`` silently skip the systems on
page 2. Both now go through the app's paginated lister
(``runtime_provision_helpers.list_all_agent_runtimes``).

Fully offline: a scripted stub client, no boto3 call.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ADT_ROOT.parent / "scripts"
for _p in (str(_ADT_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import set_query_mode  # noqa: E402
from _query_mode_lib import list_runtime_summaries, runtime_id_by_name  # noqa: E402

# Ten system runtimes fill page 1; the orchestrator and two systems land on page 2.
_PAGE_ONE = [f"adt_sys{n:02d}" for n in range(10)]
_PAGE_TWO = ["adt_orchestrator", "adt_late_one", "adt_late_two"]


class _PagedRuntimes:
    """bedrock-agentcore-control stand-in that pages runtimes 10 at a time."""

    PAGE = 10

    def __init__(self, names):
        self.runtimes = [{"agentRuntimeName": n, "agentRuntimeId": f"{n}-id"} for n in names]
        self.calls: list[dict] = []

    def list_agent_runtimes(self, **kwargs):
        self.calls.append(kwargs)
        start = int(kwargs.get("nextToken", 0))
        resp = {"agentRuntimes": self.runtimes[start:start + self.PAGE]}
        if start + self.PAGE < len(self.runtimes):
            resp["nextToken"] = str(start + self.PAGE)
        return resp


def test_list_runtime_summaries_reads_every_page():
    control = _PagedRuntimes(_PAGE_ONE + _PAGE_TWO)
    names = [rt["agentRuntimeName"] for rt in list_runtime_summaries(control)]
    assert names == _PAGE_ONE + _PAGE_TWO
    assert control.calls == [{}, {"nextToken": "10"}]


def test_runtime_id_by_name_finds_a_page_two_runtime():
    control = _PagedRuntimes(_PAGE_ONE + _PAGE_TWO)
    assert runtime_id_by_name(control, "adt_orchestrator") == "adt_orchestrator-id"
    assert runtime_id_by_name(control, "adt_missing") == ""


def test_all_systems_selection_includes_page_two_systems():
    control = _PagedRuntimes(_PAGE_ONE + _PAGE_TWO)
    ids = set_query_mode.system_ids_to_recreate(control, None, True)
    assert ids == sorted([n[len("adt_"):] for n in _PAGE_ONE] + ["late_one", "late_two"])
    assert "orchestrator" not in ids


def test_explicit_system_ids_are_kept_in_order_without_listing():
    control = _PagedRuntimes(_PAGE_ONE)
    assert set_query_mode.system_ids_to_recreate(control, ["b", "a"], False) == ["b", "a"]
    assert control.calls == []


def test_run_all_recreates_the_page_two_systems(monkeypatch):
    seen: list[list[str]] = []

    def _recreate(control, mode, ids, **_kwargs):
        seen.append(list(ids))
        return {sid: {} for sid in ids}

    monkeypatch.setattr(set_query_mode, "recreate_system_runtimes", _recreate)
    report = set_query_mode.run(
        _PagedRuntimes(_PAGE_ONE + _PAGE_TWO), None, "agent", gateway_id="gw-1",
        region="us-east-1", system_ids=None, all_systems=True, skip_ecs=True,
        recreate_only=True, log=lambda _m: None)
    (ids,) = seen
    assert {"late_one", "late_two"} <= set(ids)
    assert report["systems"]["late_two"] == "recreated"
