# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The PostgreSQL query tool refuses a JOIN whose ON condition is always true (offline).

Workshop rehearsal, 2026-11-12: a system agent joined ``... JOIN t2 b ON
a.k = a.k``. Both sides are one column of one alias, so every row of the
joined table matched, the record set held every record instead of the one
the request named, and a total over all of them came back as the answer.

The tool now refuses such a JOIN before it runs, with a fixed note and no
data, and runs everything else as before: a real link (including a
self-join of two aliases of one table), a redundant self-comparison next to
a real link, a filter-only ON, CROSS JOIN, ``LATERAL (...) ON TRUE``,
DISTINCT ON, OR / BETWEEN / CASE / subqueries in the condition, and SQL the
tokenizer cannot read for certain.

Synthetic schemas (crates, pallets, bins, nodes); no dataset names.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agents import sql_tautology_join as tj
from agents import system_agent as sa


# --------------------------------------------------------------------------- #
# Refused: the condition is always true and links nothing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sql", [
    # the rehearsal shape: one column of one alias on both sides
    "SELECT DISTINCT c.crate_id FROM crates c JOIN pallet_slots p ON c.crate_id = c.crate_id "
    "WHERE p.slot_code = 'S-1'",
    # the same inside a CTE, as the rehearsal wrote it
    "WITH picked AS (SELECT c.crate_id FROM crates c JOIN pallet_slots p "
    "ON c.crate_id = c.crate_id WHERE p.slot_code IN ('S-1')) SELECT * FROM picked",
    # the joined side's own column compared with itself
    "SELECT * FROM crates c JOIN pallet_slots p ON p.crate_ref = p.crate_ref",
    "SELECT * FROM crates c JOIN pallet_slots p ON 1 = 1",
    "SELECT * FROM crates c JOIN pallet_slots p ON 1=1",
    "SELECT * FROM crates c INNER JOIN pallet_slots p ON TRUE",
    "SELECT * FROM crates c LEFT JOIN pallet_slots p ON 'x' = 'x'",
    # an unqualified column compared with itself is still one column
    "SELECT * FROM crates JOIN bins ON bin_code = bin_code",
    # quoted and unquoted spellings of one alias are one alias
    'SELECT * FROM crates c JOIN pallet_slots p ON "c".crate_id = C.crate_id',
    # an always-true conjunct next to a filter on one side still links nothing
    "SELECT * FROM crates c JOIN pallet_slots p ON c.crate_id = c.crate_id "
    "AND p.slot_code = 'S-1'",
    "SELECT * FROM crates c JOIN pallet_slots p ON (c.crate_id = c.crate_id) AND p.qty > 0",
    # schema-qualified tables, the second join is the bad one
    "SELECT * FROM wh.crates c JOIN wh.pallet_slots p ON p.crate_ref = c.crate_id "
    "JOIN wh.bins b ON b.bin_id = b.bin_id",
    # parentheses around an operand, nested too, are peeled
    "SELECT * FROM crates c JOIN pallet_slots p ON (c.crate_id) = (c.crate_id)",
    "SELECT * FROM crates c JOIN pallet_slots p ON ((c.crate_id)) = ((c.crate_id))",
    "SELECT * FROM crates c JOIN pallet_slots p ON (c.crate_id) = ((c.crate_id))",
    "SELECT * FROM crates c JOIN pallet_slots p ON (1) = (1)",
    "SELECT * FROM crates c JOIN pallet_slots p ON ((c.crate_id) = (c.crate_id)) "
    "AND (p.qty) > 0",
])
def test_an_always_true_join_is_refused(sql):
    out = tj.check(sql)
    assert out is not None, sql
    assert out["status"] == "not_run"
    assert out["join_check"]["kind"] == "always_true_join"
    assert out["note"] == tj.NOTE


def test_the_refusal_holds_only_fixed_fields(caplog):
    caplog.set_level("INFO", logger=tj.__name__)
    out = tj.check("SELECT * FROM crates c JOIN pallet_slots p ON c.crate_id   =\n c.crate_id "
                   "WHERE p.qty > 1")
    assert out == {"status": "not_run", "join_check": {"kind": "always_true_join"},
                   "note": tj.NOTE}
    assert out == tj.REFUSAL and out is not tj.REFUSAL
    assert "CROSS JOIN" in out["note"] and not re.search(r"\d", tj.NOTE)
    # the log names the condition's kind, never its text
    assert "self_comparison" in caplog.text and "crate_id" not in caplog.text


@pytest.mark.parametrize("sql, kind", [
    ("SELECT * FROM crates c JOIN bins b ON 37400 = 37400", "constant_comparison"),
    ("SELECT * FROM crates c JOIN bins b ON 'KX-4471-002' = 'KX-4471-002'",
     "constant_comparison"),
    ("SELECT * FROM crates c JOIN bins b ON TRUE", "true_literal"),
])
def test_the_log_names_the_kind_and_no_value(caplog, sql, kind):
    caplog.set_level("INFO", logger=tj.__name__)
    assert tj.check(sql) == tj.REFUSAL
    assert kind in caplog.text
    assert "37400" not in caplog.text and "4471" not in caplog.text


@pytest.mark.parametrize("sql", [
    "SELECT * FROM crates c JOIN pallet_slots p ON c.crate_id = c.crate_id /* open",
    "SELECT * FROM crates c JOIN pallet_slots p ON c.code = 'KX-4471",
    "SELECT * FROM crates c JOIN pallet_slots p ON (c.crate_id = c.crate_id",
    "SELECT * FROM crates c JOIN pallet_slots p ON c.crate_id = c.crate_id)",
])
def test_unreadable_sql_runs_and_is_logged_without_its_text(caplog, sql):
    caplog.set_level("INFO", logger=tj.__name__)
    assert tj.check(sql) is None
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "not read for certain" in warnings[0].getMessage()
    assert "crate_id" not in caplog.text and "4471" not in caplog.text


# --------------------------------------------------------------------------- #
# Run as before
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sql", [
    # a real link
    "SELECT * FROM crates c JOIN pallet_slots p ON p.crate_ref = c.crate_id",
    # parenthesised real links
    "SELECT * FROM crates c JOIN pallet_slots p ON (p.crate_ref) = (c.crate_id)",
    "SELECT * FROM crates c JOIN pallet_slots p ON ((p.crate_ref)) = ((c.crate_id))",
    "SELECT a.node_id FROM nodes a JOIN nodes b ON (a.parent_id) = ((b.node_id))",
    "SELECT * FROM crates c JOIN pallet_slots p ON ((p.crate_ref) = (c.crate_id)) "
    "AND (c.crate_id) = (c.crate_id)",
    # (a) = (b) with different columns of one alias is not a self-comparison
    "SELECT * FROM crates c JOIN pallet_slots p ON (c.crate_id) = (c.code) "
    "AND p.crate_ref = c.crate_id",
    # a self-join: two aliases of ONE table, a column of each side
    "SELECT a.node_id, b.node_id FROM nodes a JOIN nodes b ON a.parent_id = b.node_id",
    "SELECT * FROM nodes a JOIN nodes b ON a.node_id = b.node_id AND a.kind = b.kind",
    # a redundant self-comparison next to a real link
    "SELECT * FROM crates c JOIN pallet_slots p ON p.crate_ref = c.crate_id "
    "AND c.crate_id = c.crate_id",
    # a filter-only ON (no always-true part): not this check's business
    "SELECT * FROM crates c JOIN pallet_slots p ON p.slot_code = 'S-1'",
    # a cross product written as such
    "SELECT * FROM crates c CROSS JOIN totals t",
    # LATERAL links inside its subquery
    "SELECT * FROM crates c LEFT JOIN LATERAL (SELECT * FROM pallet_slots p "
    "WHERE p.crate_ref = c.crate_id LIMIT 1) l ON TRUE",
    # a joined subquery or function: fail open
    "SELECT * FROM crates c JOIN (SELECT crate_ref FROM pallet_slots) p ON TRUE",
    "SELECT * FROM crates c JOIN unnest(c.tags) u ON TRUE",
    # DISTINCT ON is not a join condition
    "SELECT DISTINCT ON (c.crate_id) c.crate_id FROM crates c ORDER BY c.crate_id",
    # OR, BETWEEN, CASE or a subquery in the condition: fail open
    "SELECT * FROM crates c JOIN pallet_slots p ON c.crate_id = c.crate_id OR p.qty > 0",
    "SELECT * FROM crates c JOIN pallet_slots p ON c.crate_id = c.crate_id "
    "AND p.qty BETWEEN 1 AND 5",
    "SELECT * FROM crates c JOIN pallet_slots p ON CASE WHEN 1 = 1 THEN TRUE END",
    "SELECT * FROM crates c JOIN pallet_slots p ON c.crate_id = c.crate_id "
    "AND p.crate_ref IN (SELECT crate_id FROM crates)",
    # an unqualified column next to an always-true part may be either side
    "SELECT * FROM crates c JOIN pallet_slots p ON c.crate_id = c.crate_id AND crate_ref = 4",
    # functions or casts of two columns are not read as the same operand
    "SELECT * FROM crates c JOIN pallet_slots p ON lower(c.code) = lower(p.code)",
    "SELECT * FROM crates c JOIN pallet_slots p ON c.code::text = p.code::text",
    # USING
    "SELECT * FROM crates JOIN pallet_slots USING (crate_id)",
    # no join, or a WHERE self-comparison (not a join condition)
    "SELECT * FROM crates WHERE crate_id = crate_id",
    # left( / right( are functions here, not joins
    "SELECT left(c.code, 2) FROM crates c JOIN pallet_slots p ON p.crate_ref = c.crate_id",
    "", "   ", None,
])
def test_everything_else_runs(sql):
    assert tj.check(sql) is None, sql


def test_a_guard_error_is_logged_and_never_blocks(monkeypatch, caplog):
    monkeypatch.setattr(tj, "check", lambda *_a, **_k: 1 / 0)
    assert tj.guard_query("SELECT * FROM crates c JOIN bins b ON 1 = 1") is None
    assert "SQL_TAUTOLOGY_JOIN guard failed" in caplog.text


def test_the_engine_names_no_dataset():
    source = Path(tj.__file__).read_text(encoding="utf-8")
    for word in ("batch", "batch_id", "shipment", "lot", "lot_id", "lot_mes_tracking",
                 "material_lots", "work_order", "erp", "mes", "supplier"):
        assert not re.search(rf"\b{word}\b", source, re.IGNORECASE), word
    assert not re.search(r"\b(WO|RUN)-", source)


# --------------------------------------------------------------------------- #
# Wiring: the system agent's PostgreSQL tool
# --------------------------------------------------------------------------- #

class _Cursor:
    def __init__(self, columns, rows):
        self.columns, self.rows = columns, rows
        self.description = None
        self._it = iter(())

    def execute(self, sql, *_a, **_k):
        if str(sql).lstrip().upper().startswith(("SELECT", "WITH")):
            self.description = [(c,) for c in self.columns]
            self._it = iter([tuple(r) for r in self.rows])

    def fetchone(self):
        return next(self._it, None)


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def set_session(self, **_k):
        return None

    def cursor(self):
        return self._cursor

    def close(self):
        return None


_DESC = "Tables:\n- crates: crate_id (uuid), code (text)\n" \
        "- pallet_slots: slot_id (uuid), crate_ref (uuid), slot_code (text)\n"


def _tool(monkeypatch):
    cursor = _Cursor(["crate_id"], [["c-1"]])
    connects = []

    def connect(**kw):
        connects.append(kw)
        return _Conn(cursor)

    monkeypatch.setattr(sa.pgdriver, "connect", connect)
    tools, _desc = sa._system_tools("sys-a", "app", _DESC, {"host": "h"}, "PostgreSQL", None,
                                    concept_items=[])
    return tools[0], connects


def test_the_tool_does_not_run_an_always_true_join(monkeypatch):
    tool, connects = _tool(monkeypatch)
    out = json.loads(tool._tool_func(
        sql="SELECT c.crate_id FROM crates c JOIN pallet_slots p ON c.crate_id = c.crate_id"))
    assert out["status"] == "not_run" and out["join_check"]["kind"] == "always_true_join"
    assert connects == []


def test_the_tool_runs_a_linked_join(monkeypatch):
    tool, connects = _tool(monkeypatch)
    out = json.loads(tool._tool_func(
        sql="SELECT c.crate_id FROM crates c JOIN pallet_slots p ON p.crate_ref = c.crate_id"))
    assert out["rows"] == [["c-1"]] and len(connects) == 1


def test_a_guard_error_runs_the_query(monkeypatch):
    tool, connects = _tool(monkeypatch)
    monkeypatch.setattr(tj, "check", lambda *_a, **_k: 1 / 0)
    out = json.loads(tool._tool_func(sql="SELECT c.crate_id FROM crates c JOIN pallet_slots p "
                                         "ON 1 = 1"))
    assert out["rows"] == [["c-1"]] and len(connects) == 1


# --------------------------------------------------------------------------- #
# Capture: a refused query's literals are no evidence of anything
# --------------------------------------------------------------------------- #

_FIGURE, _TOKEN, _NAME = "37400", "KX-4471-002", "Kestrel Mill"
_REFUSED_SQL = [
    f"SELECT c.crate_id FROM crates c JOIN pallet_slots p ON {_FIGURE} = {_FIGURE}",
    f"SELECT c.crate_id FROM crates c JOIN pallet_slots p ON '{_TOKEN}' = '{_TOKEN}'",
    f"SELECT c.crate_id FROM crates c JOIN pallet_slots p ON '{_NAME}' = '{_NAME}'",
    f"SELECT c.crate_id FROM crates c JOIN pallet_slots p ON c.crate_id = c.crate_id "
    f"AND p.slot_code = '{_TOKEN}' AND p.qty = {_FIGURE}",
]


def _captured(sql, result_text):
    """The call's invocation state after the capture hook read one result of
    ``sql`` whose text is ``result_text``, with schema example names and
    identifier leads that match the literals (as the readers' own tests set
    them up)."""
    from agents import answer_ids, answer_names
    from agents import tool_result_capture as trc
    from types import SimpleNamespace
    capture = trc.InvocationToolResultCapture(system_id="sys-a")
    state: dict = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    agent = SimpleNamespace(messages=[], system_prompt="")
    setattr(agent, answer_names.SCHEMA_NAMES_ATTR, (_NAME,))
    setattr(agent, answer_ids.SCHEMA_LEADS_ATTR, frozenset({"kx"}))
    result = {"toolUseId": "t-1", "status": "success", "content": [{"text": result_text}]}
    capture._on_after_tool_call(SimpleNamespace(
        selected_tool=None,
        tool_use={"toolUseId": "t-1", "name": "query_app", "input": {"sql": sql}},
        invocation_state=state, result=result, exception=None, retry=False, agent=agent))
    return state


def _evidence(state) -> dict:
    """What each reader holds from the call's results."""
    from decimal import Decimal

    from agents import answer_figures, answer_ids, answer_names
    from agents import result_identifiers as ri
    values = state.get(answer_figures.VALUES_STATE_KEY) or {}
    figures = set().union(*(values.get(k) or set() for k in ("raw", "data", "derived")))
    ids = state.get(answer_ids.RESULT_IDS_STATE_KEY) or {}
    names = state.get(answer_names.RESULT_NAMES_STATE_KEY) or {}
    return {"figure": Decimal(_FIGURE) in figures,
            "id": any("4471" in t for t in ids.get("tokens") or ()),
            "name": bool(names.get("proven")),
            "result_id": bool(ri.returned(state, f"Crate {_TOKEN} was found."))}


def test_the_readers_would_read_these_literals_from_a_result():
    # Control: the same literals in a result's own text (as the first build's
    # "condition" field carried them) DO count as returned evidence.
    leaked = json.dumps({"status": "not_run", "join_check": {
        "condition": f"{_FIGURE} = {_FIGURE} AND '{_TOKEN}' = '{_NAME}'"}})
    found = _evidence(_captured(_REFUSED_SQL[0], leaked))
    assert found["figure"] and found["id"] and found["name"]


@pytest.mark.parametrize("sql", _REFUSED_SQL)
def test_a_refused_querys_literals_enter_no_evidence(monkeypatch, sql):
    tool, connects = _tool(monkeypatch)
    text = tool._tool_func(sql=sql)
    assert connects == [] and json.loads(text) == tj.REFUSAL
    for literal in (_FIGURE, _TOKEN, _NAME, "4471"):
        assert literal not in text
    state = _captured(sql, text)
    assert _evidence(state) == {"figure": False, "id": False, "name": False,
                                "result_id": False}
