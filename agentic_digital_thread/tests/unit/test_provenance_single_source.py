# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Cross-cutting provenance pins.

Three invariant families, spanning BOTH emitters (runtime + local):

* **Single source** — ``agents/provenance.py`` is the ONE home for the D1
  payload, the client citation predicate (D2), and the deterministic
  ``query_ref`` derivation (D3). Both emitters import
  ``build_provenance_records`` from it; neither re-grows a private copy or
  an inline record dict. The builder itself stays import-pure (stdlib +
  ``query_intelligence`` constants only — never ``runtime_app``,
  ``chat_client``, or boto3), keeping the unit lane offline-pure.
  Source-inspection idiom mirrors ``test_intelligence_summary_single_source``.

* **Honest absence (D4)** — no capture ⇒ every record ``unverified`` with
  NOTHING fabricated (empty entity/scope, null result shape); a tool call
  whose result vanished still yields a record — NEVER silently dropped.
  Pinned end-to-end through both emitters, not just the builder.

* **No SQL on the broadcast stream (D6)** — executed-query text
  (``_adt_executed_query`` values, SQL) never surfaces in any metadata frame:
  records are built ONLY from allowlisted bounded capture surfaces, so a
  hostile result payload carrying SQL cannot reach the SSE broadcast. The
  Wave-3 ``provenance_queries`` sidecar is the ONLY place query text may
  live, and it never broadcasts (its pin lands with Task 3.3).

Offline: no AWS, no network (same harness as ``test_chat_client_provenance``).
"""
from __future__ import annotations

import ast
import json
import queue
import types
from pathlib import Path

import agents.chat_client as chat_client
import agents.self_learning as self_learning
from agents import runtime_app
from agents.chat_client import _build_local_provenance, _ToolResultHook
from agents.events import DoneEvent, MetadataEvent
from agents.provenance import build_provenance_records

_AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"
_RUNTIME_SRC = (_AGENTS_DIR / "runtime_app.py").read_text(encoding="utf-8")
_CHAT_SRC = (_AGENTS_DIR / "chat_client.py").read_text(encoding="utf-8")
_BUILDER_PATH = _AGENTS_DIR / "provenance.py"

#: Hostile executed-query fixture — synthetic identifiers only (no dataset
#: literals; the engine must never have seen these strings elsewhere).
_SQL = "SELECT sensitive_col FROM restricted_audit_table WHERE flag = 1"

SESSION = "sess-t14"


# ---------------------------------------------------------------------------
# Single source — ONE builder, both emitters, import-pure module
# ---------------------------------------------------------------------------

def test_both_emitters_import_the_one_builder():
    # runtime: the legacy emitter AND the enforce emitter each import it.
    assert _RUNTIME_SRC.count("from .provenance import build_provenance_records") == 2
    # chat_client (local + cloud pass-through host): module-level import.
    assert "from .provenance import build_provenance_records" in _CHAT_SRC
    # Both actually CALL the builder (never just import it).
    assert "build_provenance_records(" in _RUNTIME_SRC
    assert "build_provenance_records(" in _CHAT_SRC


def test_both_emitters_import_the_one_sidecar_builder():
    """The Wave-3 sidecar trio (query-input key tuple, parse-else-omit
    extractor, entry builder) has ONE home too — both sidecar emitters
    (runtime legacy + enforce) and the local path import it."""
    assert _RUNTIME_SRC.count("from .provenance import build_sidecar_entries") == 2
    assert "build_sidecar_entries" in _CHAT_SRC
    assert "build_sidecar_entries(" in _RUNTIME_SRC
    assert "build_sidecar_entries(" in _CHAT_SRC


def test_no_private_builder_copies_in_the_emitters():
    for src in (_RUNTIME_SRC, _CHAT_SRC):
        # The payload builder, the D3 handle and the D2 predicate live ONLY
        # in agents/provenance.py — a re-grown private copy fails here.
        assert "def build_provenance_records" not in src
        assert "def mint_query_ref" not in src
        assert "def citation_ordinals" not in src
        assert "def is_client_cited" not in src
        # Never an inline record/payload dict (mirrors the
        # intelligence_summary single-source posture).
        assert '"provenance": {' not in src, \
            "inline provenance payload dict — use build_provenance_records"
        assert '"citation_num"' not in src


def test_no_private_sidecar_copies_in_the_emitters():
    """The sidecar trio must never re-grow in an emitter:
    no private extractor/builder, and the query-input key tuple appears ONLY
    in agents/provenance.py (``QUERY_INPUT_FIELDS``)."""
    for src in (_RUNTIME_SRC, _CHAT_SRC):
        assert "def build_sidecar_entries" not in src
        assert "def executed_query_from_input" not in src
        # The retired private spellings stay retired.
        assert "def _sidecar_entries" not in src
        assert "def _executed_query_from_input" not in src
        assert "def _query_text_from_input" not in src
        assert '("sql", "query", "cypher")' not in src, \
            "query-input key tuple re-grown — import QUERY_INPUT_FIELDS"


def test_builder_module_is_import_pure():
    """Offline-pure by construction: stdlib + query_intelligence constants
    only. An import of runtime_app/chat_client (circular) or boto3 (I/O)
    would silently poison the unit lane."""
    tree = ast.parse(_BUILDER_PATH.read_text(encoding="utf-8"))
    allowed = {"__future__", "ast", "hashlib", "json", "typing",
               "agents.query_intelligence"}
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            seen.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            seen.add(node.module or "")
    assert seen <= allowed, f"unexpected imports in provenance.py: {seen - allowed}"


def test_all_three_emitters_enrich_through_the_one_merge_helper():
    """Wave-5 follow-up (assembly-time sidecar merge): the merge lives ONLY
    in ``agents.provenance.enrich_records_from_sidecar`` — the runtime's
    shared ``_enriched_provenance_payload`` wrapper serves BOTH runtime
    emitters (legacy + enforce), and the local emitter calls the helper
    directly. The store reader is INJECTED (``provenance_store.get_query``),
    keeping the builder module import-pure."""
    # runtime: one wrapper, two emitter call sites.
    assert _RUNTIME_SRC.count(
        "from .provenance import enrich_records_from_sidecar") == 1
    assert _RUNTIME_SRC.count("_enriched_provenance_payload(payload, ") == 2
    # chat_client: module-level import + local emitter call.
    assert "from .provenance import enrich_records_from_sidecar" in _CHAT_SRC
    assert "enrich_records_from_sidecar(" in _CHAT_SRC
    # The merge logic never re-grows in an emitter.
    for src in (_RUNTIME_SRC, _CHAT_SRC):
        assert "def enrich_records_from_sidecar" not in src
        assert "def _merge_sidecar_entry" not in src


# ---------------------------------------------------------------------------
# Honest absence (D4) — vanished results are never silently dropped
# ---------------------------------------------------------------------------

def test_builder_no_capture_yields_all_unverified_nothing_fabricated():
    payload = build_provenance_records(
        session_id=SESSION,
        tool_calls=[{"name": "erp___query_erp", "toolUseId": "t1"},
                    {"name": "mes___query_mes", "toolUseId": "t2"}],
        tool_inputs={}, tool_results_metadata={}, evidence_by_tool={},
        rules_applied=[], verdict=None, captured_at="2026-08-03T00:00:00Z",
    )
    assert len(payload["records"]) == 2  # nothing dropped
    for record in payload["records"]:
        assert record["verification"] == "unverified"
        assert record["entity"] == {"table": "", "columns": []}
        assert record["scope"] == {"filters": [], "window": None}
        assert record["result_shape"] == {"rows": None, "truncated": False,
                                          "partial_reason": ""}


def test_runtime_vanished_result_still_yields_unverified_record():
    """Legacy runtime path: a cited call whose result snippet vanished still
    produces a record — honest ``unverified``, never a silent drop."""
    state = types.SimpleNamespace(
        session_id=SESSION,
        tool_calls_log=[{"name": "erp___query_erp", "toolUseId": "t1"},
                        {"name": "mes___query_mes", "toolUseId": "t2"}],
        tool_inputs={"t1": '{"question": "count"}'},
        tool_results={"t1": '{"row_count": 1, "rows": []}'},  # t2 vanished
        rule_ids=set(), matched_rows=[],
    )
    [event] = runtime_app._provenance_events(state)
    records = {r["tool_id"]: r for r in event["provenance"]["records"]}
    assert set(records) == {"t1", "t2"}
    assert records["t2"]["verification"] == "unverified"
    assert records["t2"]["result_shape"]["rows"] is None
    assert records["t1"]["verification"] == "partial"


def test_local_vanished_result_still_yields_unverified_record():
    hook = _ToolResultHook(queue.Queue(), session_id=SESSION)
    hook.tool_calls = [{"name": "erp___query_erp", "toolUseId": "t1"},
                       {"name": "mes___query_mes", "toolUseId": "t2"}]
    hook.tool_metadata = {"t1": {"app_status": "success", "row_count": 2,
                                 "truncated": False}}  # t2 vanished
    payload = _build_local_provenance(hook, rules_applied=[])
    records = {r["tool_id"]: r for r in payload["records"]}
    assert set(records) == {"t1", "t2"}
    assert records["t2"]["verification"] == "unverified"
    assert records["t2"]["entity"] == {"table": "", "columns": []}
    assert records["t1"]["verification"] == "partial"


# ---------------------------------------------------------------------------
# No SQL on the broadcast stream (D6)
# ---------------------------------------------------------------------------

def _hostile_result() -> dict:
    """A tool result whose payload smuggles executed-query text the way the
    system agents attach it (``_adt_executed_query``) plus raw SQL rows."""
    return {"status": "success", "content": [{"json": {
        "row_count": 2, "rows": [{"note": _SQL}],
        "_adt_executed_query": _SQL,
    }}]}


class _StubOrchestrator:
    """Fires the registered hook once per fake tool call on invoke."""

    def __init__(self, tool_events):
        self._tool_events = tool_events
        self.callback_handler = None
        self._hook = None
        self.hooks = types.SimpleNamespace(add_hook=lambda h: setattr(self, "_hook", h))

    def __call__(self, prompt, **_kwargs):
        for name, tool_id, result in self._tool_events:
            self._hook._on_tool_done(types.SimpleNamespace(
                tool_use={"name": name, "toolUseId": tool_id, "input": {"q": 1}},
                result=result))
        return "final answer"


def test_local_broadcast_stream_never_carries_executed_query(monkeypatch):
    """End-to-end broadcast pin: a hostile result carrying SQL and the
    ``_adt_executed_query`` marker flows through the FULL local stream —
    no yielded SSE event may surface either (the claim-check posture)."""
    monkeypatch.setattr(chat_client, "_summarize_tool_result", lambda n, r: "SUM")
    monkeypatch.setattr(chat_client, "build_orchestrator",
                        lambda *a, **k: _StubOrchestrator(
                            [("erp___query_erp", "t1", _hostile_result())]))
    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids",
                        lambda ids: [])
    events = list(chat_client.LocalChatClient().stream(
        prompt="p", session_id=SESSION, user_id="u"))

    provs = [e for e in events
             if isinstance(e, MetadataEvent) and "provenance" in e.data]
    assert len(provs) == 1  # the pin is not vacuous — the frame IS emitted
    assert provs[0].data["provenance"]["records"][0]["result_shape"]["rows"] == 2
    assert any(isinstance(e, DoneEvent) for e in events)

    broadcast = " ".join(repr(e) for e in events)
    assert "_adt_executed_query" not in broadcast
    assert "SELECT" not in broadcast
    assert "restricted_audit_table" not in broadcast
    assert "sensitive_col" not in broadcast


def test_runtime_legacy_frame_never_carries_executed_query():
    """Legacy runtime path: the 2 KB result snippet contains the
    ``_adt_executed_query`` value — the emitted frame must not."""
    snippet = json.dumps({"row_count": 1, "rows": [],
                          "_adt_executed_query": _SQL})
    state = types.SimpleNamespace(
        session_id=SESSION,
        tool_calls_log=[{"name": "erp___query_erp", "toolUseId": "t1"}],
        tool_inputs={"t1": _SQL},  # Mode-B input text never enters records
        tool_results={"t1": snippet},
        rule_ids=set(), matched_rows=[],
    )
    [event] = runtime_app._provenance_events(state)
    serialized = json.dumps(event)
    assert "_adt_executed_query" not in serialized
    assert "SELECT" not in serialized
    assert "restricted_audit_table" not in serialized


def test_builder_rejects_query_text_by_construction():
    """The builder NEVER reads ``tool_inputs`` (D6: query text is Wave-3
    sidecar-only) — even a poisoned metadata dict cannot smuggle SQL into a
    record, because records copy only the bounded result-shape keys."""
    payload = build_provenance_records(
        session_id=SESSION,
        tool_calls=[{"name": "erp___query_erp", "toolUseId": "t1"}],
        tool_inputs={"t1": _SQL},
        tool_results_metadata={"t1": {"app_status": "success", "row_count": 3,
                                      "truncated": False,
                                      "_adt_executed_query": _SQL}},
        evidence_by_tool={}, rules_applied=[], verdict=None,
        captured_at="2026-08-03T00:00:00Z",
    )
    serialized = json.dumps(payload)
    assert "_adt_executed_query" not in serialized
    assert "SELECT" not in serialized
    assert payload["records"][0]["result_shape"]["rows"] == 3
