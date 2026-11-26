# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The record-key bake reads the baked table list the way the system agent does.

Found while building the row exclusion: ``record_key.remember_from_graph``
found tables with a pattern of ``name(``, and a deployment's baked table list
writes each entry as ``name [base_table](col: type; ...)``. The pattern then
missed the table and matched only ``timestamp`` (from ``timestamp(6)``), so a
record key declared on that table was never baked: the record-key check and
its RECORD KEYS schema line were never in effect there. Both steward-declared
table facts (record key, row exclusion) now read the table names with ONE
reader, ``system_agent.desc_table_names`` (``_desc_entries``, which drops the
relation-kind marker), the last name part of each. (The row exclusion has
since moved to the system's manifest and reads no table list.)

Fully offline; synthetic names.
"""
from __future__ import annotations

import json

import pytest

import agents.record_key as rk
import agents.row_exclusion as rx
import agents.system_agent as sa

_KEY = [{"column": "id", "coalesced": False},
        {"column": "revision", "coalesced": False},
        {"column": "run_ref", "coalesced": True}]
_BRACKETED = ("job_events [base_table](id: varchar e.g. a1, b2; revision: integer e.g. 0, 1; "
              "run_ref: varchar; finished_at: timestamp(6) with time zone e.g. "
              "2026-10-01 00:00:00.000 UTC), ledger_lines [view](batch_tag: varchar; "
              "amount: decimal(12,2))")
_PLAIN = ("job_events(id: varchar; revision: integer; run_ref: varchar; "
          "finished_at: timestamp(6) with time zone), ledger_lines(batch_tag: varchar)")


@pytest.fixture(autouse=True)
def _fresh():
    rk.reset()
    rx.reset()
    yield
    rk.reset()
    rx.reset()


def _graph(asked: list, entry_names: list | None = None):
    """A graph read seam that answers the record-key read for job_events only
    and records every SchemaEntry it was asked about. It also answers the
    name-resolution read (``SYSTEM_SCHEMA_ENTRY_NAMES``) the bake now makes to
    map a terminal to its real SchemaEntry name; that read is NOT recorded in
    ``asked`` (which tracks only the per-table fact reads). With no registered
    entry names the resolver finds no match and the bake falls back to the
    terminal, so the fact read is still ``sys.<terminal>``."""
    names = entry_names or []

    def query(cypher, params):
        if "RETURN se.name AS name" in cypher and "seid" not in (params or {}):
            return [{"name": n} for n in names]
        asked.append(params["seid"])
        if params["seid"] == "sys.job_events":
            return [{"record_key": json.dumps(_KEY)}]
        return [{}]
    return query


@pytest.mark.parametrize("desc", [_BRACKETED, _PLAIN], ids=["bracketed", "plain"])
def test_the_bake_reads_every_described_table_and_nothing_else(monkeypatch, desc):
    asked: list = []
    monkeypatch.setattr(rk, "_graph_query", lambda: _graph(asked))
    rk.remember_from_graph("sys", desc)
    assert sorted(asked) == ["sys.job_events", "sys.ledger_lines"]
    assert rk.columns_for("sys", "job_events") == ["id", "revision", "run_ref"]
    assert rk.columns_for("sys", "ledger_lines") == []


def test_a_bracketed_bake_puts_the_record_keys_line_in_the_prompt(monkeypatch):
    monkeypatch.setattr(rk, "_graph_query", lambda: _graph([]))
    monkeypatch.setattr(rx, "_graph_query", lambda: (lambda *_a, **_k: [{}]))
    sa._remember_turn_time_facts("sys", [], _BRACKETED, connection_type="Athena")
    prompt = sa._compose_system_prompt("sys", "System", "lake", "", _BRACKETED, "Athena")
    assert "RECORD KEYS (facts)" in prompt
    assert "COALESCE(run_ref, '')" in prompt


def test_a_qualified_table_is_read_by_its_last_name_part(monkeypatch):
    # No registered entry names: the resolver finds no qualified match and the
    # bake falls back to the terminal, reading the fact under sys.<terminal> and
    # remembering it under the terminal the guard looks up.
    asked: list = []
    monkeypatch.setattr(rk, "_graph_query", lambda: _graph(asked))
    rk.remember_from_graph("sys", "lake.job_events [base_table](id: varchar)")
    assert asked == ["sys.job_events"]
    assert rk.columns_for("sys", "job_events") == ["id", "revision", "run_ref"]


def test_a_qualified_schema_entry_is_read_by_its_real_name(monkeypatch):
    # The live S3 Tables case: the SchemaEntry is stored under the qualified
    # name. The bake resolves the terminal from the baked list to that real
    # name, reads the fact under sys.<qualified>, and remembers it under the
    # terminal the guard looks up.
    asked: list = []

    def query(cypher, params):
        if "RETURN se.name AS name" in cypher and "seid" not in (params or {}):
            return [{"name": "lake.job_events"}]
        asked.append(params["seid"])
        if params["seid"] == "sys.lake.job_events":
            return [{"record_key": json.dumps(_KEY)}]
        return [{}]

    monkeypatch.setattr(rk, "_graph_query", lambda: query)
    rk.remember_from_graph("sys", "lake.job_events [base_table](id: varchar)")
    assert asked == ["sys.lake.job_events"]          # read under the REAL name
    assert rk.columns_for("sys", "job_events") == ["id", "revision", "run_ref"]  # terminal key


@pytest.mark.parametrize("desc", [_BRACKETED, _PLAIN, "lake.job_events(id: varchar)", ""])
def test_record_key_reads_the_table_list_and_row_exclusion_reads_its_manifest(monkeypatch,
                                                                             desc):
    """The record key asks the graph about exactly the tables
    ``system_agent.desc_table_names`` reads, in its order. The row exclusion
    reads no table list: it asks for the system's manifest, and a system whose
    manifest lists no table is asked about no table."""
    rk_asked: list = []
    rx_asked: list = []
    monkeypatch.setattr(rk, "_graph_query", lambda: _graph(rk_asked))
    monkeypatch.setattr(rx, "_graph_query",
                        lambda: (lambda _c, p: rx_asked.append(dict(p)) or [{}]))
    rk.remember_from_graph("sys", desc)
    rx.remember_from_graph("sys", desc)
    assert rk_asked == [f"sys.{t}" for t in sa.desc_table_names(desc)]
    assert rx_asked == [{"sid": "sys"}]


def test_desc_table_names_drops_the_relation_kind_marker_and_type_parentheses():
    assert sa.desc_table_names(_BRACKETED) == ["job_events", "ledger_lines"]
    assert sa.desc_table_names(_PLAIN) == ["job_events", "ledger_lines"]
    assert sa.desc_table_names("") == []
