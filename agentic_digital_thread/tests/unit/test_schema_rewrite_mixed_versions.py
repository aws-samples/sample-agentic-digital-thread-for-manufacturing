# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Mixed versions during a rolling deploy see each other's schema rewrites.

The app's tasks are replaced one at a time, so for a while a task of the
previous version (b4182b45: the mark and the failed marker are System-node
properties, nothing else) and a task of this version run side by side, and
either can re-measure a rule against a system the other is rewriting.

This release is phase 1 of a two-release move of the mark into the shared
store (``schema_rewrite_state``): every writer sets the mark in the store AND
on the System node, with the System-node statements b4182b45 used, so a
previous-version reader still sees it; every reader honours both. Phase 2
stops the System-node writes and reads once no previous-version task can run.

The previous version's statements are copied here verbatim from b4182b45
(``BASE_*``), so these tests keep checking against that version whatever
this module later becomes. The fake applies a statement to the System node's
properties as openCypher does (its WHERE, then SET and REMOVE, then RETURN);
``tests/integration/test_schema_rewrite_mixed_versions_memgraph.py`` runs the
same directions on a real engine. Neutral names: system ``hub-1``, table
``t``, field ``k``.
"""
from __future__ import annotations

import os
import re
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402

SID, TABLE, FIELD = "hub-1", "t", "k"

# ── b4182b45, verbatim ──────────────────────────────────────────────────────
BASE_MARK = ("MATCH (s:System {id: $sid}) SET s.schema_rewrite_started = $started "
             "RETURN s.schema_rewrite_started AS started")
BASE_SETTLED = (
    "MATCH (s:System {id: $sid}) "
    "WHERE s.schema_rewrite_started IS NULL OR s.schema_rewrite_started = $token "
    "REMOVE s.schema_rewrite_started, s.schema_rewrite_failed")
BASE_FAILED = (
    "MATCH (s:System {id: $sid}) "
    "WHERE s.schema_rewrite_started IS NULL OR s.schema_rewrite_started = $token "
    "SET s.schema_rewrite_failed = $failed REMOVE s.schema_rewrite_started")
BASE_CLEAR_STALE = (
    "MATCH (s:System {id: $sid}) WHERE s.schema_rewrite_started = $started "
    "REMOVE s.schema_rewrite_started")
BASE_SNAPSHOT = (
    "MATCH (s:System {id: $sid}) "
    "OPTIONAL MATCH (s)-[:HAS_SCHEMA]->(:SchemaEntry {name: $tbl})-[:HAS_FIELD]->(f:Field {name: $fld}) "
    "OPTIONAL MATCH (f)-[r:MAPS_TO]->(c:Concept) "
    "RETURN s.schema_rewrite_started AS started, s.schema_rewrite_failed AS failed, "
    "count(DISTINCT f) AS fields, "
    "collect(DISTINCT CASE WHEN c IS NULL THEN null "
    "ELSE {concept: c.id, confidence: r.confidence} END) AS edges")


class _Graph:
    """One System node (a property map) with one field ``t.k`` mapping
    ``c1``. A statement naming ``schema_rewrite_*`` properties of
    ``(s:System {id: $sid})`` is applied: WHERE (``IS NULL`` and ``= $p``
    conditions, OR inside an AND), SET, REMOVE, then RETURN of those
    properties under their aliases. The field read answers the same aliases
    beside the field."""

    def __init__(self):
        self.node = {"id": SID}

    def _holds(self, condition: str, params: dict) -> bool:
        m = re.fullmatch(r"s\.(schema_rewrite_\w+) IS NULL", condition)
        if m:
            return self.node.get(m.group(1)) is None
        m = re.fullmatch(r"s\.(schema_rewrite_\w+) = \$(\w+)", condition)
        if m:
            value = self.node.get(m.group(1))
            return value is not None and value == params.get(m.group(2))
        raise AssertionError(f"unmodelled condition: {condition}")

    def _returned(self, cypher: str) -> dict:
        return {alias: self.node.get(prop)
                for prop, alias in re.findall(r"s\.(schema_rewrite_\w+) AS (\w+)", cypher)}

    def write_returning(self, cypher, params=None):
        params = params or {}
        if params.get("sid") != SID or "schema_rewrite_" not in cypher:
            return []
        where = re.search(r"\bWHERE (.*?)(?: SET | REMOVE | RETURN |$)", cypher)
        for clause in where.group(1).split(" AND ") if where else ():
            if not any(self._holds(alt.strip().strip("()"), params) for alt in clause.split(" OR ")):
                return []
        sets = re.search(r"\bSET (.*?)(?: REMOVE | RETURN |$)", cypher)
        for prop, param in re.findall(r"s\.(schema_rewrite_\w+) = \$(\w+)", sets.group(1) if sets else ""):
            self.node[prop] = params[param]
        removes = re.search(r"\bREMOVE (.*?)(?: RETURN |$)", cypher)
        for prop in re.findall(r"s\.(schema_rewrite_\w+)", removes.group(1) if removes else ""):
            self.node.pop(prop, None)
        return [self._returned(cypher)]

    def write(self, cypher, params=None):
        self.write_returning(cypher, params)
        return True

    def query(self, cypher, params=None):
        if "count(DISTINCT f) AS fields" in cypher and (params or {}).get("sid") == SID:
            return [{**self._returned(cypher), "fields": 1,
                     "edges": [{"concept": "c1", "confidence": 0.9}]}]
        return []


@pytest.fixture
def graph(monkeypatch):
    g = _Graph()
    monkeypatch.setattr(onboard, "neptune_write", g.write)
    monkeypatch.setattr(onboard, "neptune_write_returning", g.write_returning)
    monkeypatch.setattr(onboard, "neptune_query_checked", g.query)
    monkeypatch.setattr(onboard, "neptune_query", g.query)
    monkeypatch.setattr(onboard, "_schema_rewrite_sleep", lambda _s: None)
    return g


def _base_reader(graph) -> dict:
    """What a b4182b45 reader reads: its own snapshot statement."""
    (row,) = graph.query(BASE_SNAPSHOT, {"sid": SID, "tbl": TABLE, "fld": FIELD})
    return row


def _new_reader():
    return onboard._settled_target_concepts(SimpleNamespace(schema_wait_budget_s=0.0),
                                            SID, TABLE, FIELD)


# ── new writer, previous-version reader ─────────────────────────────────────

def test_a_new_writer_marks_the_store_and_the_system_node_alike(graph):
    """Phase 1: the same token in the store's record and on the System
    node, and the same end in both. Until phase 2 a model query looping over
    ``keys(s)`` can still read the System-node copy (it holds no answer
    data); phase 2 removes it."""
    store = onboard._SCHEMA_REWRITE_STATES
    token = onboard._mark_schema_rewrite(SID)
    assert onboard.schema_rewrite_state.read(store, SID)["started"] == token
    assert graph.node["schema_rewrite_started"] == token
    onboard._finish_schema_rewrite(SID, token, settled=False)
    record = onboard.schema_rewrite_state.read(store, SID)
    assert record["started"] is None and record["failed"] == graph.node["schema_rewrite_failed"]
    assert "schema_rewrite_started" not in graph.node


def test_a_new_writers_rewrite_is_seen_by_the_base_reader(graph):
    """REPRODUCTION. fa47db8e marked only the store,
    so a previous-version task read the field mid-rewrite as settled. Now the
    System node carries the same mark through the rewrite, and its end
    leaves what b4182b45's end left."""
    token = onboard._mark_schema_rewrite(SID)
    assert _base_reader(graph)["started"] == token
    onboard._finish_schema_rewrite(SID, token, settled=False)
    row = _base_reader(graph)
    assert row["started"] is None and isinstance(row["failed"], int)
    token = onboard._mark_schema_rewrite(SID)
    assert _base_reader(graph)["started"] == token
    onboard._finish_schema_rewrite(SID, token, settled=True)
    row = _base_reader(graph)
    assert row["started"] is None and row["failed"] is None


def test_a_base_readers_stale_clear_never_removes_a_new_writers_mark(graph):
    """A previous-version reader clears a stale mark by its exact value; a
    new writer's fresh mark holds another value and stays."""
    token = onboard._mark_schema_rewrite(SID)
    graph.write(BASE_CLEAR_STALE, {"sid": SID, "started": 1_000})
    assert _base_reader(graph)["started"] == token


def test_a_mark_the_store_refuses_leaves_no_system_node_mark(graph, monkeypatch):
    """The System-node mark lands first; when the store then refuses its
    mark, b0 stops and the System-node mark is taken back (its own value
    only), so a previous-version reader is not held by a rewrite that never
    started."""
    from agents.job_store import JobStore
    from tests.unit._shared_table import SharedTable
    table = SharedTable()
    table.fail_puts = True
    monkeypatch.setattr(onboard, "_SCHEMA_REWRITE_STATES", JobStore("schema-rewrite", table=table))
    with pytest.raises(onboard.SchemaRewriteMarkError):
        onboard._mark_schema_rewrite(SID)
    assert _base_reader(graph)["started"] is None


# ── previous-version writer, new reader ─────────────────────────────────────

def test_a_base_writers_rewrite_is_seen_by_the_new_reader(graph):
    """The previous version marks only the System node: the new reader
    waits on its mark, reports its failed marker, and reads the field once it
    settles."""
    now = int(time.time() * 1000)
    graph.write(BASE_MARK, {"sid": SID, "started": now})
    concepts, why = _new_reader()
    assert concepts is None and "being rewritten" in why
    graph.write(BASE_FAILED, {"sid": SID, "token": now, "failed": now})
    concepts, why = _new_reader()
    assert concepts is None and "left unsettled" in why
    graph.write(BASE_SETTLED, {"sid": SID, "token": now})
    concepts, why = _new_reader()
    assert why is None and concepts[0] == frozenset({"c1"})


def test_a_base_writers_end_never_removes_a_new_writers_newer_mark(graph):
    """A previous-version run of the same system ends after a new run
    started: its owner-checked end leaves the new mark, and the new reader
    still waits on it."""
    old = int(time.time() * 1000) - 5_000
    graph.write(BASE_MARK, {"sid": SID, "started": old})
    token = onboard._mark_schema_rewrite(SID)
    graph.write(BASE_SETTLED, {"sid": SID, "token": old})
    assert _base_reader(graph)["started"] == token
    concepts, why = _new_reader()
    assert concepts is None and "being rewritten" in why


def test_a_stale_store_mark_does_not_hide_a_base_writers_fresh_mark(graph):
    """A new run stopped long ago and left a stale mark in the store; a
    previous-version run is rewriting now and has marked only the System
    node. The new reader must wait on the fresh mark, whichever it meets
    first, and clears the stale one by its value."""
    store = onboard._SCHEMA_REWRITE_STATES
    onboard.schema_rewrite_state.begin(store, SID, 1_000)
    graph.write(BASE_MARK, {"sid": SID, "started": int(time.time() * 1000)})
    concepts, why = _new_reader()
    assert concepts is None and "being rewritten" in why
    assert onboard.schema_rewrite_state.read(store, SID)["started"] is None
