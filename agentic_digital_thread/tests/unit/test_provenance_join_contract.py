# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Cross-side F1 join contract (Wave 5 — orchestrator ⇄ system agent ⇄ D5).

ONE module importing BOTH halves so drift on either side breaks it here:

* the orchestrator wrapper's merged arg (B1), fed through
  ``parse_provenance_context`` (A1), yields EXACTLY the key the system-agent
  writer (B2) stores and the D5 route reads;
* ``mint_query_ref`` and ``RESERVED_PROVENANCE_ARG`` each have exactly ONE
  definition home (``agents/provenance.py``) — every consumer imports them
  (grep-style AST pin);
* the D5 route returns the F2 additive fields UNTOUCHED for a stored entry
  (byte-passthrough — the route needed zero changes).
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

import agents.provenance_store as provenance_store
import agents.system_agent as sa
from agents.job_store import JobStore
from agents.provenance import (
    RESERVED_PROVENANCE_ARG,
    mint_query_ref,
    parse_provenance_context,
    parse_write_token,
    provenance_context,
)
from agents.runtime_asgi import provenance_context_var, provenance_write_token_var
from agents.tool_result_capture import SIDECAR_STATEMENTS_STATE_KEY
from tests.unit._shared_table import SharedTable

orch = pytest.importorskip("agents.orchestrator")

AGENTS_DIR = Path(sa.__file__).resolve().parent
SID = "join-sess-1"
TOOL_USE_ID = "tooluse_JOIN"
SQL = "SELECT part_no FROM orders"


@pytest.fixture(autouse=True)
def _memory_store(monkeypatch):
    # Shared-table stores so the orchestrator wrapper can issue a DURABLE
    # capability (put_durable refuses memory-only) and the join resolves.
    data = SharedTable()
    monkeypatch.setattr(provenance_store, "_job_store",
                        JobStore(provenance_store._KIND, table=data))
    monkeypatch.setattr(provenance_store, "_cap_store",
                        JobStore(provenance_store._CAP_KIND, table=SharedTable()))
    token = provenance_context_var.set(None)
    wtoken = provenance_write_token_var.set(None)
    yield
    provenance_context_var.reset(token)
    provenance_write_token_var.reset(wtoken)


class _FakeClient:
    def __init__(self):
        self.calls = []
        self.list_tools_sync = lambda *a, **k: []

        async def _call(tool_use_id, name, arguments=None,
                        read_timeout_seconds=None, meta=None):
            self.calls.append(arguments)
            return {"status": "success", "content": [{"text": "ok"}]}

        self.call_tool_async = _call


def test_full_join_orchestrator_arg_to_stored_key_to_d5_read():
    """The whole chain, one test: B1 mints → A1 parses → B2 writes → D5 reads."""
    # 1. orchestrator half: the wrapper merges the reserved arg
    client = _FakeClient()
    wrapped = orch._wrap_gateway_client_with_provenance_context(client, SID)
    asyncio.run(wrapped.call_tool_async(TOOL_USE_ID, "erp___query_erp", {"q": 1}))
    sent = client.calls[0][RESERVED_PROVENANCE_ARG]
    # 2. runtime half: parse yields the validated pair
    parsed = parse_provenance_context(sent)
    assert parsed == (SID, mint_query_ref(SID, TOOL_USE_ID))
    # 3. emit site writes under the exact D5 key, with the issued capability
    provenance_context_var.set(parsed)
    provenance_write_token_var.set(parse_write_token(sent))
    state = {SIDECAR_STATEMENTS_STATE_KEY: [
        {"query_text": SQL, "table": "orders", "columns": ["part_no"],
         "row_count": 2, "queried_at": "2026-08-04T17:00:00Z"}]}
    sa._write_provenance_sidecar(state, "erp")
    # 4. the D5 store read (same call the route makes) returns it
    entry = provenance_store.get_query(SID, mint_query_ref(SID, TOOL_USE_ID))
    assert entry is not None and entry["origin"] == "system-agent"
    assert entry["executed_query"] == SQL


def test_d5_route_returns_f2_additive_fields_untouched():
    """Route byte-passthrough pin: get_provenance_query returns the STORED
    dict as-is — the F2 additive fields ride with zero route changes."""
    from agents.agents_trace_routes import get_provenance_query

    ref = mint_query_ref(SID, TOOL_USE_ID)
    provenance_store.put_turn_queries(SID, {ref: {
        "system_id": "erp", "tool_name": "query_erp", "executed_query": SQL,
        "kg_artifacts": [], "captured_at": "2026-08-04T17:00:00Z",
        "entity": {"table": "orders", "columns": ["part_no"]},
        "row_count": 2, "statement_count": 1, "origin": "system-agent",
    }})

    class _Req:
        class state:
            is_admin = True

    body = get_provenance_query(ref, _Req(), session_id=SID)
    assert body == provenance_store.get_query(SID, ref)
    assert body["entity"] == {"table": "orders", "columns": ["part_no"]}
    assert body["row_count"] == 2
    assert body["statement_count"] == 1
    assert body["origin"] == "system-agent"


def _py_sources() -> list[Path]:
    return [p for p in AGENTS_DIR.rglob("*.py") if "__pycache__" not in p.parts]


def test_reserved_arg_defined_once_and_always_imported():
    """Grep-style pin: the literal '_adt_provenance' string appears as an
    ASSIGNMENT only in provenance.py; every other agents/ consumer references
    the RESERVED_PROVENANCE_ARG name (import), never a re-spelled literal."""
    assignment_re = re.compile(r"^RESERVED_PROVENANCE_ARG\s*=", re.MULTILINE)
    literal = '"_adt_provenance"'
    defining, respelling = [], []
    for path in _py_sources():
        text = path.read_text(encoding="utf-8")
        if assignment_re.search(text):
            defining.append(path.name)
        elif literal in text:
            respelling.append(path.name)
    assert defining == ["provenance.py"], (
        f"RESERVED_PROVENANCE_ARG must be defined ONCE in provenance.py, "
        f"found assignments in: {defining}")
    assert not respelling, (
        f"re-spelled '_adt_provenance' literal (import the constant): {respelling}")


def test_mint_query_ref_defined_in_exactly_one_home():
    definers = [p.name for p in _py_sources()
                if re.search(r"^def mint_query_ref\(", p.read_text(encoding="utf-8"),
                             re.MULTILINE)]
    assert definers == ["provenance.py"]


def test_both_sides_import_the_contract_from_provenance():
    """Deleting either side's contract import breaks the join here."""
    orch_src = Path(orch.__file__).read_text(encoding="utf-8")
    asgi_src = (AGENTS_DIR / "runtime_asgi.py").read_text(encoding="utf-8")
    assert "RESERVED_PROVENANCE_ARG" in orch_src
    assert "mint_query_ref" in orch_src
    assert "parse_provenance_context" in asgi_src
    assert "RESERVED_PROVENANCE_ARG" in asgi_src


def test_context_builder_and_parser_are_inverse():
    ref = mint_query_ref(SID, TOOL_USE_ID)
    assert parse_provenance_context(provenance_context(SID, ref)) == (SID, ref)


# --------------------------------------------------------------------------- #
# Wave 6: the investigation-trace half of the SAME join (W6-F1)
# --------------------------------------------------------------------------- #

def test_full_join_orchestrator_arg_to_trace_key_to_route_read(monkeypatch):
    """The SAME chain for the Wave-6 family: B1 mints → A1 parses → the
    trace writer stores → the /investigations/ route reads — one key."""
    import agents.investigation_trace as it
    import agents.investigation_trace_store as its
    from agents.agents_trace_routes import get_investigation_trace

    monkeypatch.setattr(its, "_job_store", JobStore(its._KIND, table=None))
    client = _FakeClient()
    wrapped = orch._wrap_gateway_client_with_provenance_context(client, SID)
    asyncio.run(wrapped.call_tool_async(TOOL_USE_ID, "erp___query_erp", {"q": 1}))
    sent = client.calls[0][RESERVED_PROVENANCE_ARG]
    parsed = parse_provenance_context(sent)
    assert parsed == (SID, mint_query_ref(SID, TOOL_USE_ID))
    # emit site writes under the exact key, with the issued capability
    provenance_context_var.set(parsed)
    provenance_write_token_var.set(parse_write_token(sent))
    state: dict = {}
    it.record_step(state, tool_use={"toolUseId": "t-1", "name": "execute_sql",
                                    "input": {"sql": SQL}},
                   result={"status": "success", "content": [{"text": "ok"}]},
                   exception=None, retry=False, messages=[])
    sa._write_investigation_trace(state, "erp", "How many orders?", "Seven.")
    # the route read (same call the frontend makes) returns it
    class _Req:
        class state:
            is_admin = True

    body = get_investigation_trace(mint_query_ref(SID, TOOL_USE_ID), _Req(),
                                   session_id=SID)
    assert body["origin"] == "system-agent"
    assert body["final_answer"] == "Seven."
    assert body["steps"][0]["tool_name"] == "execute_sql"


def test_trace_caps_have_exactly_one_home_and_are_imported():
    """The W6 caps constants are ASSIGNED only in investigation_trace.py;
    the store imports that module (never re-spells a cap), and the route
    imports the store."""
    cap_names = ("MAX_TRACE_STEPS", "MAX_STEP_INPUT_BYTES",
                 "MAX_STEP_OUTPUT_BYTES", "MAX_REASONING_BYTES",
                 "MAX_QUESTION_BYTES", "MAX_FINAL_ANSWER_BYTES",
                 "MAX_TRACE_ENTRY_BYTES")
    for name in cap_names:
        assignment_re = re.compile(rf"^{name}\s*=", re.MULTILINE)
        definers = [p.name for p in _py_sources()
                    if assignment_re.search(p.read_text(encoding="utf-8"))]
        assert definers == ["investigation_trace.py"], (
            f"{name} must be defined ONCE in investigation_trace.py, "
            f"found assignments in: {definers}")
    store_src = (AGENTS_DIR / "investigation_trace_store.py").read_text(
        encoding="utf-8")
    assert "investigation_trace" in store_src        # store imports the home
    route_src = (AGENTS_DIR / "agents_trace_routes.py").read_text(
        encoding="utf-8")
    assert "investigation_trace_store" in route_src  # route imports the store
