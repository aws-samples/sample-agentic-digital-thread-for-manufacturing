# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Base-API replay regression for the provenance write capability.

This module imports ONLY symbols that exist before the capability layer
(``put_turn_queries``, ``get_query``, ``put_trace``, ``get_trace``,
``mint_query_ref``) so it collects and runs against both the pre-capability
code and the current code. It drives the exploit directly: the owner of a
completed answer replays its own public ``(session_id, query_ref)`` in a later
direct system-tool call.

With the capability layer the replay is harmless, because the legitimate write
used a capability the replay does not hold (set up through whatever issuing
helper is present, discovered reflectively so this file needs no new import).
Without the capability layer there is no capability at all: the replay lands on
the same key the reader reads, and the assertion FAILS at the exploit line
(``get_query`` returns the attacker's text), which is the required failure on
the pre-capability code.

Generic names only.
"""
from __future__ import annotations

import pytest

import agents.investigation_trace_store as its
import agents.provenance_store as ps
from agents.investigation_trace import build_trace_entry
from agents.job_store import JobStore
from agents.provenance import mint_query_ref

try:  # the fix's shared-table test harness; absent is fine on the base
    from tests.unit._shared_table import SharedTable
except Exception:  # pragma: no cover - base has it too, but stay defensive
    SharedTable = None

SID = "session-1700000000000-abcd1234"
REF = mint_query_ref(SID, "tooluse-legit-0001")
LEGIT = "SELECT part_no FROM orders WHERE qty > 5"


def _q_entry(text):
    return {"system_id": "erp", "tool_name": "query_erp", "executed_query": text,
            "kg_artifacts": [], "captured_at": "2026-08-04T17:00:00Z"}


def _t_entry(answer):
    return build_trace_entry(session_id=SID, query_ref=REF, system_id="erp",
                             question="q", kg_context_attached=False, steps=[],
                             steps_dropped=0, final_answer=answer)


@pytest.fixture(autouse=True)
def _stores(monkeypatch):
    # Shared-table stores so the fix's durable capability can be issued; on the
    # base there is no capability layer and these are just the data stores.
    data = SharedTable() if SharedTable else None
    monkeypatch.setattr(ps, "_job_store", JobStore(ps._KIND, table=data))
    monkeypatch.setattr(its, "_job_store", JobStore(its._KIND, table=data))
    cap_attr = getattr(ps, "_CAP_KIND", None)
    if cap_attr is not None and SharedTable is not None:
        monkeypatch.setattr(ps, "_cap_store", JobStore(cap_attr, table=SharedTable()))
    yield


def _legitimate_write():
    """Write the legitimate entry the way the running code does: with a
    capability when the fix provides one, plain otherwise (the base)."""
    issue = getattr(ps, "issue_provenance_arg", None)
    token = None
    if issue is not None:
        token = issue(SID, REF).get("write_token")
    if token is not None:
        ps.put_turn_queries(SID, {REF: _q_entry(LEGIT)}, write_token=token)
        its.put_trace(SID, REF, _t_entry("Seven open orders."), write_token=token)
    else:
        ps.put_turn_queries(SID, {REF: _q_entry(LEGIT)})
        its.put_trace(SID, REF, _t_entry("Seven open orders."))


def test_base_api_replay_cannot_overwrite_admin_visible_evidence():
    _legitimate_write()
    # The owner replays its own public pair directly, holding no capability.
    ps.put_turn_queries(SID, {REF: _q_entry("ATTACKER")})
    its.put_trace(SID, REF, _t_entry("ATTACKER ANSWER"))
    # Exploit assertion: without the capability layer these return the
    # attacker's text and FAIL (the replay wrote the key the reader reads).
    assert ps.get_query(SID, REF)["executed_query"] == LEGIT
    assert its.get_trace(SID, REF)["final_answer"] == "Seven open orders."
